"""flightlog relay: a remote site's XIAO, forwarded to the home server.

Runs on the Raspberry Pi the XIAO is plugged into, and reaches the server over
Tailscale:

    python -m flightlog.relay                       # reads ./flightlog_relay.json
    python -m flightlog.relay --server http://homepi:5001 --token flr_... --ports auto
    python -m flightlog.relay --status              # spool depth, last ack, ports, server
    python -m flightlog.relay --replay capture.txt  # a capture instead of a XIAO

RPI/install_relay.py writes the config and runs this as the flightlog-relay
service.

Every serial line - detections, status lines, everything - goes into a local
SQLite spool first (flightlog_relay.db), and leaves it only once the server has
acknowledged storing it. A dropped LTE link, a server restart or a reboot of
this Pi loses nothing; the backlog is delivered when the link returns, and the
server places it at the times the lines were heard.

Times are carried as an *age* - how long ago, on this Pi's monotonic clock,
the line was read - and the server subtracts it from its own clock. A Pi has no
real-time clock and may boot an hour or a day out until NTP catches up; ages
are immune to that. Only lines spooled before this relay restarted (whose
monotonic readings belong to a previous boot) carry wall-clock time instead.

This file must never import Flask: a relay Pi has only pyserial and requests.
"""
import argparse
import gzip
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
import time

from .ingest.reader import (SerialReader, list_ports, ESPRESSIF_VID, PORT_MONITOR_INTERVAL,
                            short_port)

logger = logging.getLogger('flightlog.relay')

VERSION = '1'
CONFIG_NAME = 'flightlog_relay.json'
SPOOL_NAME = 'flightlog_relay.db'

SPOOL_MAX_AGE_S = 24 * 3600     # the oldest lines go first past either cap...
SPOOL_MAX_LINES = 200000        # ...and are counted as dropped
SEND_EVERY_S = 1.0              # send whatever is waiting this often,
SEND_AT_LINES = 200             # or at once when this many are waiting
BATCH_LINES = 500
BATCH_MAX_CHARS = 1500000       # the server refuses over 1 MB compressed; stay far below
MAX_LINE_CHARS = 2000           # a line with no newline (wrong baud, a crash) is cut here
HEARTBEAT_S = 10.0              # an empty batch this often says "relay alive, XIAO quiet"
BACKOFF_MAX_S = 60.0
HTTP_TIMEOUT_S = 20.0
STATUS_EVERY_S = 10.0           # how often the running relay records itself for --status


class Spool:
    """Durable FIFO of serial lines, in SQLite.

    `seq` is AUTOINCREMENT so it never goes backwards, even after every row is
    deleted: the server skips anything at or below the last seq it acked, and a
    reused number would be silently discarded. `spool_id` names this file; a
    new file (relay reinstalled, spool deleted) starts a new sequence, which the
    server recognises by the changed id.
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False,
                                    isolation_level=None, timeout=30.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS lines (
              seq  INTEGER PRIMARY KEY AUTOINCREMENT,
              port TEXT NOT NULL,
              mono REAL NOT NULL,
              boot TEXT NOT NULL,
              wall REAL NOT NULL,
              line TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        sid = self.get('spool_id')
        if sid is None:
            sid = os.urandom(8).hex()
            self.set('spool_id', sid)
        self.spool_id = sid
        self.dropped = int(self.get('dropped') or 0)
        self._depth = self.conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    def get(self, key):
        with self._lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set(self, key, value):
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    def add(self, port, mono, boot, wall, line):
        with self._lock:
            self.conn.execute("INSERT INTO lines(port, mono, boot, wall, line) VALUES(?,?,?,?,?)",
                              (port, mono, boot, wall, line[:MAX_LINE_CHARS]))
            self._depth += 1
            over = self._depth - SPOOL_MAX_LINES
        if over > 0:
            self._drop("seq IN (SELECT seq FROM lines ORDER BY seq LIMIT ?)", (over,))

    def depth(self):
        return self._depth

    def oldest_wall(self):
        with self._lock:
            r = self.conn.execute("SELECT wall FROM lines ORDER BY seq LIMIT 1").fetchone()
        return r[0] if r else None

    def batch(self, limit=BATCH_LINES, max_chars=BATCH_MAX_CHARS):
        """The oldest waiting lines, as (seq, port, mono, boot, wall, line)."""
        with self._lock:
            rows = self.conn.execute("SELECT seq, port, mono, boot, wall, line FROM lines"
                                     " ORDER BY seq LIMIT ?", (limit,)).fetchall()
        out, size = [], 0
        for r in rows:
            size += len(r[5]) + 64
            if out and size > max_chars:
                break
            out.append(r)
        return out

    def ack(self, seq):
        """The server stored everything up to `seq`: forget it."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM lines WHERE seq <= ?", (seq,))
            self._depth = max(0, self._depth - (cur.rowcount or 0))

    def trim(self, now_wall=None):
        """Drop lines older than SPOOL_MAX_AGE_S. Returns how many went."""
        cutoff = (now_wall or time.time()) - SPOOL_MAX_AGE_S
        return self._drop("wall < ?", (cutoff,))

    def _drop(self, where, args):
        with self._lock:
            cur = self.conn.execute("DELETE FROM lines WHERE " + where, args)
            n = cur.rowcount or 0
            if n:
                self._depth = max(0, self._depth - n)
                self.dropped += n
                self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('dropped', ?)",
                                  (str(self.dropped),))
        if n:
            logger.warning("spool full: dropped the %d oldest lines (%d in total)", n, self.dropped)
        return n

    def close(self):
        self.conn.close()


class Relay:
    """Serial ports -> spool -> server.

    `ports` is 'auto' (every Espressif USB device: the XIAO's native USB) or a
    list of device names. `http` stands in for a requests.Session in tests.
    """

    def __init__(self, server, token, spool_path, ports='auto', name=None, http=None,
                 opener=None, lister=None):
        self.server = server.rstrip('/')
        self.token = token
        self.name = name
        self.ports = ports
        self.boot = os.urandom(4).hex()
        self.started = time.monotonic()
        self.spool = Spool(spool_path)
        if http is None:
            import requests
            http = requests.Session()
        self.http = http
        self._lister = lister or list_ports
        self.reader = SerialReader(self._on_line, opener=opener)
        self.port_last = {}             # port -> monotonic time of its last line
        self._results = []              # command results for the next batch
        self._rlock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_ack = None            # wall time of the last acknowledged batch
        self.last_error = None
        self.auth_failed = False
        self._last_status = 0.0

    # -- input ---------------------------------------------------------------------
    def _on_line(self, port, line, mono):
        self.spool.add(port, mono, self.boot, time.time(), line)
        self.port_last[port] = mono
        if self.spool.depth() >= SEND_AT_LINES:
            self._wake.set()

    def add_line(self, port, line, mono=None):
        """Spool a line that did not come from a port (replay)."""
        self._on_line(port, line, mono if mono is not None else time.monotonic())

    def _wanted(self, available):
        if self.ports == 'auto':
            return [p['device'] for p in available if p.get('vid') == ESPRESSIF_VID]
        present = {p['device'] for p in available}
        return [p for p in self.ports if p in present]

    def start_ports(self):
        """Open the configured ports now, then follow them as they come and go."""
        try:
            started = self._wanted(self._lister())
        except Exception as e:
            logger.error("cannot list serial ports: %s", e)
            started = []
        for p in started:
            self.reader.start(p)
        self.reader.start_monitor(self._wanted, interval=PORT_MONITOR_INTERVAL,
                                  lister=self._lister)
        if not started:
            logger.warning("no %s yet; waiting for one to be plugged in",
                           'Espressif (XIAO) device' if self.ports == 'auto'
                           else ', '.join(self.ports))
        return started

    # -- what the relay says about itself -------------------------------------------
    def status(self):
        now_m, now_w = time.monotonic(), time.time()
        oldest = self.spool.oldest_wall()
        ports = {}
        for p in set(self.reader.status) | set(self.port_last):
            last = self.port_last.get(p)
            ports[p] = {'connected': bool(self.reader.status.get(p)),
                        'line_age_s': round(now_m - last, 1) if last is not None else None}
        return {'version': VERSION, 'host': socket.gethostname(), 'boot': self.boot,
                'uptime_s': round(now_m - self.started, 1), 'wall': now_w,
                'spool_depth': self.spool.depth(), 'dropped': self.spool.dropped,
                'oldest_age_s': round(now_w - oldest, 1) if oldest else None,
                'last_ack_age_s': round(now_w - self.last_ack, 1) if self.last_ack else None,
                'ports': ports}

    def _record_status(self):
        """Leave a note for `--status`, which cannot ask this process directly."""
        if time.time() - self._last_status < STATUS_EVERY_S:
            return
        self._last_status = time.time()
        st = self.status()
        st.update(last_ack=self.last_ack, last_error=self.last_error, server=self.server,
                  name=self.name, pid=os.getpid())
        try:
            self.spool.set('status', json.dumps(st))
        except sqlite3.Error:
            pass

    # -- sending --------------------------------------------------------------------
    def _body(self, rows, results):
        now = time.monotonic()
        lines = []
        for seq, port, mono, boot, wall, line in rows:
            item = {'seq': seq, 'port': port, 'line': line}
            if boot == self.boot:
                # Unrounded: two lines read in the same millisecond must keep
                # distinct times, or the database takes them for one reading.
                item['age'] = max(0.0, now - mono)
            else:
                item['wall'] = wall     # read before this relay restarted
            lines.append(item)
        return {'spool_id': self.spool.spool_id, 'boot': self.boot, 'name': self.name,
                'relay': self.status(), 'lines': lines, 'results': results}

    def _reachable(self):
        """After a failure, a cheap request first: reconnecting can take a second
        or more, and a batch's ages are fixed when it is built, so building it
        before the link is back would place every line late by that long."""
        try:
            resp = self.http.get(self.server + '/api/ingest/hello', timeout=HTTP_TIMEOUT_S,
                                 headers={'Authorization': 'Bearer ' + self.token})
        except Exception as e:
            self._failed([], 'cannot reach %s: %s' % (self.server, type(e).__name__))
            return False
        if resp.status_code != 200:
            self.auth_failed = resp.status_code in (401, 403)
            self._failed([], 'HTTP %d' % resp.status_code)
            return False
        return True

    def send_once(self):
        """Send one batch (possibly empty). True when the server acknowledged it.

        Ages are measured as the batch is built, so each line's time is late by
        the batch's transit to the server - normally tens of milliseconds over
        a kept-alive connection."""
        if self.last_error and not self._reachable():
            return False
        rows = self.spool.batch()
        with self._rlock:
            results, self._results = self._results, []
        body = self._body(rows, results)
        data = gzip.compress(json.dumps(body, separators=(',', ':')).encode('utf-8'))
        try:
            resp = self.http.post(self.server + '/api/ingest', data=data, timeout=HTTP_TIMEOUT_S,
                                  headers={'Authorization': 'Bearer ' + self.token,
                                           'Content-Type': 'application/json',
                                           'Content-Encoding': 'gzip'})
        except Exception as e:
            self._failed(results, 'cannot reach %s: %s' % (self.server, type(e).__name__))
            return False
        if resp.status_code != 200:
            try:
                why = resp.json().get('error') or ''
            except Exception:
                why = ''
            self.auth_failed = resp.status_code in (401, 403)
            self._failed(results, 'HTTP %d%s' % (resp.status_code, ': ' + why if why else ''))
            return False
        try:
            reply = resp.json()
            ack = int(reply.get('ack_seq') or 0)
        except Exception:
            self._failed(results, 'unreadable reply from the server')
            return False
        if rows and ack >= rows[0][0]:
            self.spool.ack(ack)
        if self.last_error:
            logger.info("server reachable again; %d lines waiting", self.spool.depth())
        self.last_error, self.auth_failed = None, False
        self.last_ack = time.time()
        for cmd in reply.get('commands') or []:
            self._run_command(cmd)
        return True

    def _failed(self, results, why):
        with self._rlock:
            self._results = results + self._results
        if why != self.last_error:
            logger.warning("send failed (%s); %d lines waiting", why, self.spool.depth())
        self.last_error = why

    def _run_command(self, cmd):
        try:
            cid, port, command = int(cmd['id']), str(cmd['port']), str(cmd['command'])
        except (KeyError, TypeError, ValueError):
            return
        ok = self.reader.send(port, command)
        detail = ('written to %s' % port) if ok else ('%s is not connected' % port)
        logger.info("command %s for %s: %s", command, port, detail)
        with self._rlock:
            self._results.append({'id': cid, 'ok': ok, 'detail': detail})
        self._wake.set()

    def run(self):
        """Send until stopped: every SEND_EVERY_S while lines wait (at once past
        SEND_AT_LINES), an empty batch every HEARTBEAT_S, exponential back-off
        to BACKOFF_MAX_S while the server cannot be reached."""
        backoff = 0.0
        last_sent = 0.0
        last_trim = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_trim > 60:
                last_trim = now
                self.spool.trim()
            if self.spool.depth() or self._results or now - last_sent >= HEARTBEAT_S:
                if self.send_once():
                    backoff, last_sent = 0.0, now
                    self._record_status()
                    if self.spool.depth() >= SEND_AT_LINES:
                        continue            # more than a batch waiting: keep going
                else:
                    backoff = BACKOFF_MAX_S if self.auth_failed else \
                        min(BACKOFF_MAX_S, max(1.0, backoff * 2))
                    self._record_status()
                    self._stop.wait(backoff)
                    continue
            self._wake.wait(SEND_EVERY_S)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.reader.stop()

    def stopped(self):
        return self._stop.is_set()


# -- replay ------------------------------------------------------------------------------
def read_capture(path):
    """(seconds from the start, port, line) for each line of a capture.

    Reads flightlog_serial.log's format ("2026-09-18 12:00:01.123 COM3 < {...}",
    host-to-device '>' lines skipped) with its timing, or a plain file of
    serial lines, which play one per --interval.
    """
    out = []
    t0 = None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            raw = raw.rstrip('\r\n')
            if not raw.strip():
                continue
            parts = raw.split(' ', 4)
            ts = None
            if len(parts) == 5 and parts[3] in ('<', '>'):
                try:
                    ts = time.mktime(time.strptime(parts[0] + ' ' + parts[1][:8],
                                                   '%Y-%m-%d %H:%M:%S'))
                    ts += float('0' + parts[1][8:]) if len(parts[1]) > 8 else 0.0
                except ValueError:
                    ts = None
            if ts is not None:
                if parts[3] == '>':
                    continue
                t0 = ts if t0 is None else t0
                out.append((ts - t0, parts[2], parts[4]))
            else:
                out.append((None, None, raw))
    return out


def replay(relay, path, speed=1.0, interval=1.0, port='replay'):
    """Feed a capture into the spool at real-time pace (divided by `speed`)."""
    start = time.monotonic()
    k = 0
    for offset, p, line in read_capture(path):
        if offset is None:
            offset = k * interval
            k += 1
        due = start + offset / max(speed, 1e-6)
        while not relay.stopped() and time.monotonic() < due:
            time.sleep(min(0.2, max(0.0, due - time.monotonic())))
        if relay.stopped():
            return
        relay.add_line(p or port, line)


# -- status ----------------------------------------------------------------------------------
def _ago(s):
    if s is None:
        return 'never'
    s = int(s)
    return '%ds ago' % s if s < 120 else '%dm ago' % (s // 60) if s < 7200 else '%dh ago' % (s // 3600)


def print_status(cfg, spool_path):
    """What the running relay last recorded, plus a fresh look at ports and server."""
    print('relay config:   %s' % (cfg.get('_path') or '(command line)'))
    print('server:         %s' % (cfg.get('server') or '(not set)'))
    print('node name:      %s' % (cfg.get('name') or '(not set)'))
    if os.path.exists(spool_path):
        sp = Spool(spool_path)
        now = time.time()
        oldest = sp.oldest_wall()
        st = json.loads(sp.get('status') or '{}')
        print('spool:          %s' % sp.path)
        print('  waiting:      %d lines%s' % (sp.depth(), (', oldest from %s' % _ago(now - oldest))
                                           if oldest else ''))
        print('  dropped:      %d (spool over %d h or %d lines)'
              % (sp.dropped, SPOOL_MAX_AGE_S // 3600, SPOOL_MAX_LINES))
        if st:
            print('  last ack:     %s' % _ago(now - st['last_ack'] if st.get('last_ack') else None))
            if st.get('last_error'):
                print('  last error:   %s' % st['last_error'])
            print('  recorded:     %s by pid %s' % (_ago(now - st.get('wall', now)), st.get('pid')))
            for p, ps in sorted((st.get('ports') or {}).items()):
                print('  port %-12s %s, last line %s' % (
                    short_port(p), 'connected' if ps.get('connected') else 'not connected',
                    _ago(ps.get('line_age_s'))))
        else:
            print('  (the relay has not recorded a status yet)')
        sp.close()
    else:
        print('spool:          %s (not created yet - the relay has not run)' % spool_path)
    try:
        devs = [p for p in list_ports() if p.get('vid') == ESPRESSIF_VID]
        print('XIAO devices:   %s' % (', '.join(p['device'] for p in devs) or 'none plugged in'))
    except Exception as e:
        print('XIAO devices:   cannot list ports (%s)' % e)
    if cfg.get('server') and cfg.get('token'):
        ok, msg = check_server(cfg['server'], cfg['token'])
        print('server check:   %s' % msg)
        return 0 if ok else 1
    return 0


def check_server(server, token, timeout=10):
    """(ok, message) from the server's /api/ingest/hello for this token."""
    import requests
    try:
        r = requests.get(server.rstrip('/') + '/api/ingest/hello', timeout=timeout,
                         headers={'Authorization': 'Bearer ' + token})
    except Exception as e:
        return False, 'cannot reach %s (%s) - is Tailscale up on both machines?' % (
            server, type(e).__name__)
    if r.status_code == 200:
        j = r.json()
        return True, 'ok - the server knows this relay as "%s"' % j.get('node')
    if r.status_code == 401:
        return False, 'token rejected (401) - rotated or deleted on the Nodes page?'
    if r.status_code == 403:
        return False, 'node disabled on the server (403)'
    return False, 'unexpected HTTP %d' % r.status_code


# -- entry point -------------------------------------------------------------------------------
def load_config(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as fh:
        cfg = json.load(fh)
    cfg['_path'] = os.path.abspath(path)
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description='flightlog relay: forward a remote XIAO to the server')
    ap.add_argument('--config', default=CONFIG_NAME,
                    help='JSON config written by RPI/install_relay.py (default: ./%s)' % CONFIG_NAME)
    ap.add_argument('--server', help='e.g. http://homepi:5001 (its Tailscale name)')
    ap.add_argument('--token', help='the token the Nodes page showed for this node')
    ap.add_argument('--name', help='this node\'s name (informational)')
    ap.add_argument('--ports', help='"auto" (Espressif USB devices) or a comma-separated list')
    ap.add_argument('--spool', help='spool file (default: %s beside the config)' % SPOOL_NAME)
    ap.add_argument('--status', action='store_true', help='print spool, ports and server state')
    ap.add_argument('--replay', metavar='FILE', help='feed a capture instead of serial ports')
    ap.add_argument('--speed', type=float, default=1.0, help='replay speed-up factor')
    ap.add_argument('--interval', type=float, default=1.0,
                    help='seconds between lines of a capture without timestamps')
    ap.add_argument('--port-name', default='replay', help='port name for replayed lines')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(args.config)
    for k in ('server', 'token', 'name', 'spool'):
        if getattr(args, k):
            cfg[k] = getattr(args, k)
    if args.ports:
        cfg['ports'] = 'auto' if args.ports == 'auto' else [p.strip() for p in args.ports.split(',')
                                                            if p.strip()]
    base = os.path.dirname(cfg['_path']) if cfg.get('_path') else os.getcwd()
    spool_path = cfg.get('spool') or os.path.join(base, SPOOL_NAME)

    if args.status:
        return print_status(cfg, spool_path)
    if not cfg.get('server') or not cfg.get('token'):
        ap.error('--server and --token are required (or a config file holding them)')

    relay = Relay(cfg['server'], cfg['token'], spool_path, ports=cfg.get('ports') or 'auto',
                  name=cfg.get('name'))

    def _term(signum, frame):
        logger.info("stopping (signal %s); %d lines stay spooled", signum, relay.spool.depth())
        relay.stop()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    logger.info("flightlog relay %s -> %s (spool %s, %d lines waiting)",
                cfg.get('name') or '', relay.server, relay.spool.path, relay.spool.depth())
    if args.replay:
        sender = threading.Thread(target=relay.run, daemon=True, name='relay-send')
        sender.start()
        replay(relay, args.replay, args.speed, args.interval, args.port_name)
        end = time.monotonic() + 120        # then deliver what is left, briefly
        while relay.spool.depth() and time.monotonic() < end and not relay.stopped():
            time.sleep(0.2)
        done = relay.spool.depth() == 0
        relay.stop()
        sender.join(timeout=HTTP_TIMEOUT_S + 2)
        logger.info("replay finished; %s", 'all lines delivered' if done
                    else '%d lines still spooled' % relay.spool.depth())
        return 0 if done else 1
    relay.start_ports()
    relay.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
