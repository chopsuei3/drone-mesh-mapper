"""Receiver nodes: the home Pi's own XIAO and the relays at other sites.

Each remote site has a XIAO on a Raspberry Pi running `python -m
flightlog.relay`, which spools every serial line and posts it here over
Tailscale (POST /api/ingest). This module is the server's side of that:

  * NodeRegistry - the nodes table, tokens, live status (last contact, last
    line, the XIAO's own status line), queued commands, and the monitor that
    turns a node going quiet or crashing into an alert.
  * BatchIngest - one relay batch: skip what was already stored, place each
    line at the time it was heard, and feed it through the same parser and
    sessionizer as the local XIAO's lines.

The local XIAO is a node too (kind 'local', named 'home'), so every view
treats all receivers alike. It has no token and no relay; its lines come
from SerialManager.

Status lives in memory and is written back every few seconds; only the
delivery cursor (spool_id, ack_seq) is written at once, because a resent
batch must never be stored twice.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time

from .ingest.reader import classify_line, short_port
from .parse import LineParser, extract_json

logger = logging.getLogger(__name__)

LOCAL_KIND, RELAY_KIND = 'local', 'relay'
LOCAL_NAME = 'home'
TOKEN_PREFIX = 'flr_'
NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,31}')
COMMANDS = ('STATUS', 'WATCHDOG_RESET')
COMMAND_TTL_S = 600            # a command its relay has not picked up by then expires
COMMAND_KEEP = 200             # per node
# Heartbeat `reset` values that mean the XIAO crashed rather than restarted
# normally (see reset_reason_str in remoteid-mesh-dualcore/src/main.cpp).
BAD_RESETS = ('panic', 'int_watchdog', 'task_watchdog', 'watchdog', 'brownout')
# A board first heard with less uptime than this, and a bad reset, has just crashed.
FRESH_BOOT_S = 300

PERSIST_EVERY_S = 10.0
CHECK_EVERY_S = 30.0

# One relay batch. The relay sends at most 500 lines; these bound a hostile one.
MAX_BODY_BYTES = 1024 * 1024
MAX_INFLATED_BYTES = 8 * 1024 * 1024
MAX_LINES = 2000
MAX_LINE_CHARS = 8192
# The relay's spool keeps 24 h. A line claiming to be older than this, or from
# the future, is clamped rather than trusted.
MAX_BACKLOG_S = 26 * 3600

STATES = ('online', 'xiao_silent', 'relay_offline', 'waiting', 'disabled')


def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _scalars(obj, max_keys=24, max_str=64):
    """A bounded copy of a status object: scalar values, short keys and strings.
    These come off the radio side of the wire, so nothing is trusted to be small."""
    out = {}
    for k, v in obj.items():
        if len(out) >= max_keys:
            break
        if not isinstance(k, str) or len(k) > 32:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:max_str]
    return out


def _relay_status(raw):
    """What a relay reports about itself, reduced to known, bounded fields."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, n in (('version', 32), ('host', 64), ('boot', 32)):
        if isinstance(raw.get(k), str):
            out[k] = raw[k][:n]
    for k in ('uptime_s', 'spool_depth', 'dropped', 'oldest_age_s', 'wall', 'last_ack_age_s'):
        if _num(raw.get(k)) is not None:
            out[k] = raw[k]
    ports = raw.get('ports')
    if isinstance(ports, dict):
        out['ports'] = {}
        for p, st in list(ports.items())[:8]:
            if isinstance(p, str) and isinstance(st, dict):
                out['ports'][p[:64]] = {'connected': bool(st.get('connected')),
                                        'line_age_s': _num(st.get('line_age_s'))}
    return out


class NodeRegistry:
    """Every receiver, its credentials and its live state.

    on_event(kind, node, detail) is called for alert-worthy changes: kind is
    'relay_offline', 'xiao_silent', 'recovered' or 'restarted', and node is the
    public dict. It runs on the monitor or ingest thread and must not block.
    """

    def __init__(self, db, settings=None, on_event=None):
        self.db = db
        self.settings = settings
        self.on_event = on_event or (lambda kind, node, detail: None)
        self.local_send = None          # (port, command) -> bool, set by the app
        self.local_ports = None         # () -> {port: {'connected': bool}}, set by the app
        self._lock = threading.RLock()
        self._ingest_locks = {}
        self._nodes = {}
        self._dirty = set()
        self.started_at = time.time()
        self._stop = threading.Event()
        self._thread = None
        self._load()

    # -- storage ----------------------------------------------------------------
    def _load(self):
        for r in self.db.query("SELECT * FROM nodes"):
            self._nodes[r['id']] = self._from_row(r)

    @staticmethod
    def _from_row(r):
        n = dict(r)
        n['enabled'] = bool(n['enabled'])
        for k, col in (('xiao', 'xiao_status'), ('relay', 'relay_status')):
            try:
                n[k] = json.loads(n.pop(col) or '{}')
            except ValueError:
                n[k] = {}
            if not isinstance(n[k], dict):
                n[k] = {}
        return n

    def persist(self):
        """Write back the runtime fields of nodes that changed."""
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            rows = [(n['last_contact_at'], n['last_line_at'], n['last_detection_at'],
                     json.dumps(n['xiao']), json.dumps(n['relay']), n['clock_offset_s'],
                     n['alert_state'], n['id'])
                    for n in (self._nodes.get(i) for i in dirty) if n is not None]
        if rows:
            self.db.executemany(
                "UPDATE nodes SET last_contact_at=?, last_line_at=?, last_detection_at=?,"
                " xiao_status=?, relay_status=?, clock_offset_s=?, alert_state=? WHERE id=?", rows)

    # -- reading ------------------------------------------------------------------
    def get(self, node_id):
        n = self._nodes.get(node_id)
        return self.public(n) if n else None

    def name(self, node_id):
        n = self._nodes.get(node_id)
        return n['name'] if n else None

    def list(self):
        with self._lock:
            nodes = sorted(self._nodes.values(), key=lambda n: (n['kind'] != LOCAL_KIND, n['name']))
            return [self.public(n) for n in nodes]

    def public(self, n, now=None):
        now = now or time.time()
        out = {k: n[k] for k in ('id', 'name', 'kind', 'enabled', 'token_hint', 'lat', 'lon',
                                 'notes', 'created_at', 'last_contact_at', 'last_line_at',
                                 'last_detection_at', 'clock_offset_s', 'ack_seq')}
        out['xiao'] = n['xiao']
        relay = dict(n['relay'])
        if n['kind'] == LOCAL_KIND and self.local_ports is not None:
            try:
                relay = {'ports': self.local_ports()}
            except Exception:
                relay = {}
        out['relay'] = relay
        ports = set(n['xiao']) | set(relay.get('ports') or {})
        out['ports'] = sorted(ports)
        out['status'] = self.state(n, now)
        return out

    def state(self, n, now=None):
        """online | xiao_silent | relay_offline | waiting | disabled.

        'waiting' is the grace after this server or the node started: nothing
        has been heard yet, and it is too soon to call that a fault. Silence is
        measured from when the server started, so a server that was itself down
        does not report every relay offline the moment it comes back.
        """
        now = now or time.time()
        if not n['enabled']:
            return 'disabled'
        thr = self.settings.get('nodes.offline_after_s') if self.settings else 600.0
        ref = max(self.started_at, n['created_at'] or 0)
        if n['kind'] == RELAY_KIND:
            c = n['last_contact_at']
            if c is None or c < ref:
                return 'waiting' if now - ref <= thr else 'relay_offline'
            if now - c > thr:
                return 'relay_offline'
        line = n['last_line_at']
        if line is None or line < ref:
            if now - ref <= thr:
                return 'online' if n['kind'] == RELAY_KIND else 'waiting'
            return 'xiao_silent'
        return 'xiao_silent' if now - line > thr else 'online'

    # -- managing ----------------------------------------------------------------------
    def _check_name(self, name, exclude=None):
        name = (name or '').strip() if isinstance(name, str) else ''
        if not NAME_RE.fullmatch(name):
            raise ValueError('name must be 1-32 letters, digits, ".", "_" or "-", '
                             'starting with a letter or digit')
        for n in self._nodes.values():
            if n['name'].lower() == name.lower() and n['id'] != exclude:
                raise ValueError('a node called %s already exists' % n['name'])
        return name

    @staticmethod
    def _position(data, n=None):
        lat = data.get('lat', n['lat'] if n else None)
        lon = data.get('lon', n['lon'] if n else None)
        if lat is None and lon is None:
            return None, None
        if _num(lat) is None or _num(lon) is None or not (-90 <= lat <= 90) \
                or not (-180 <= lon <= 180):
            raise ValueError('lat and lon must be numbers (or both null)')
        return float(lat), float(lon)

    @staticmethod
    def _new_token():
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        return token, hash_token(token), token[-4:]

    def ensure_local(self):
        """The id of the built-in local node, created on first start."""
        with self._lock:
            for n in self._nodes.values():
                if n['kind'] == LOCAL_KIND:
                    return n['id']
            name, i = LOCAL_NAME, 1
            while any(n['name'].lower() == name for n in self._nodes.values()):
                i += 1
                name = '%s%d' % (LOCAL_NAME, i)
            cur = self.db.execute(
                "INSERT INTO nodes(name, kind, created_at) VALUES(?,?,?)",
                (name, LOCAL_KIND, time.time()))
            self._nodes[cur.lastrowid] = self._from_row(
                self.db.one("SELECT * FROM nodes WHERE id=?", (cur.lastrowid,)))
            return cur.lastrowid

    def create(self, data):
        """A new relay node. Returns (node, token) - the only time the token exists."""
        if not isinstance(data, dict):
            raise ValueError('expected a JSON object')
        with self._lock:
            name = self._check_name(data.get('name'))
            lat, lon = self._position(data)
            notes = data.get('notes') if isinstance(data.get('notes'), str) else None
            token, th, hint = self._new_token()
            cur = self.db.execute(
                "INSERT INTO nodes(name, kind, token_hash, token_hint, lat, lon, notes, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (name, RELAY_KIND, th, hint, lat, lon, (notes or '')[:500] or None, time.time()))
            n = self._nodes[cur.lastrowid] = self._from_row(
                self.db.one("SELECT * FROM nodes WHERE id=?", (cur.lastrowid,)))
            return self.public(n), token

    def update(self, node_id, data):
        if not isinstance(data, dict):
            raise ValueError('expected a JSON object')
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None:
                return None
            name = self._check_name(data['name'], exclude=node_id) if 'name' in data else n['name']
            lat, lon = self._position(data, n)
            enabled = data.get('enabled', n['enabled'])
            if not isinstance(enabled, bool):
                raise ValueError('enabled must be true or false')
            notes = data.get('notes', n['notes'])
            notes = (notes[:500] or None) if isinstance(notes, str) else None
            self.db.execute("UPDATE nodes SET name=?, lat=?, lon=?, enabled=?, notes=? WHERE id=?",
                            (name, lat, lon, int(enabled), notes, node_id))
            if enabled != n['enabled']:
                n['alert_state'] = None     # re-enabling starts a fresh baseline
                self._dirty.add(node_id)
            n.update(name=name, lat=lat, lon=lon, enabled=enabled, notes=notes)
            return self.public(n)

    def rotate(self, node_id):
        """A new token for a relay; the old one stops working at once."""
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None or n['kind'] != RELAY_KIND:
                return None
            token, th, hint = self._new_token()
            self.db.execute("UPDATE nodes SET token_hash=?, token_hint=? WHERE id=?",
                            (th, hint, node_id))
            n.update(token_hash=th, token_hint=hint)
            return token

    def delete(self, node_id):
        """Remove a relay and what only it explains. Flights and detections stay;
        their points just no longer name a receiver."""
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None or n['kind'] != RELAY_KIND:
                return False
            for sql in ("DELETE FROM flight_nodes WHERE node_id=?",
                        "DELETE FROM node_samples WHERE node_id=?",
                        "DELETE FROM node_commands WHERE node_id=?",
                        "UPDATE detections SET rx_node=NULL WHERE rx_node=?",
                        "DELETE FROM nodes WHERE id=?"):
                self.db.execute(sql, (node_id,))
            del self._nodes[node_id]
            self._dirty.discard(node_id)
            return True

    def authenticate(self, token):
        """The node a bearer token belongs to (enabled or not), or None."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return None
        th = hash_token(token)
        found = None
        with self._lock:
            for n in self._nodes.values():
                if n['token_hash'] and hmac.compare_digest(n['token_hash'], th):
                    found = n
        return found

    def ingest_lock(self, node_id):
        with self._lock:
            lk = self._ingest_locks.get(node_id)
            if lk is None:
                lk = self._ingest_locks[node_id] = threading.Lock()
            return lk

    # -- what nodes report ---------------------------------------------------------------
    def contact(self, node_id, relay, received_at):
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None:
                return
            n['last_contact_at'] = received_at
            status = _relay_status(relay)
            if status.get('wall') is not None:
                n['clock_offset_s'] = round(received_at - status['wall'], 3)
            n['relay'] = status
            self._dirty.add(node_id)

    def set_cursor(self, node_id, spool_id, ack_seq):
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None:
                return
            if n['spool_id'] == spool_id and n['ack_seq'] == ack_seq:
                return
            n['spool_id'], n['ack_seq'] = spool_id, ack_seq
        self.db.execute("UPDATE nodes SET spool_id=?, ack_seq=? WHERE id=?",
                        (spool_id, ack_seq, node_id))

    def line_seen(self, node_id, port, ts, line, kind):
        """Any line from a node's XIAO: it is alive. A status line also updates
        what the XIAO says about itself, and may reveal a crash."""
        with self._lock:
            n = self._nodes.get(node_id)
            if n is None:
                return
            if n['last_line_at'] is None or ts > n['last_line_at']:
                n['last_line_at'] = ts
            if kind == 'detection' and (n['last_detection_at'] is None
                                        or ts > n['last_detection_at']):
                n['last_detection_at'] = ts
            self._dirty.add(node_id)
            if kind != 'heartbeat':
                return
            obj = extract_json(line)
            if not obj:
                return
            snap = _scalars(obj)
            snap['t'] = ts
            prev = n['xiao'].get(port)
            if prev is not None and ts < (prev.get('t') or 0):
                return                      # an older line from a backlog
            restarted = False
            up = _num(snap.get('uptime_s'))
            if up is not None:
                prev_up = _num((prev or {}).get('uptime_s'))
                if prev_up is not None:
                    # Uptime went backwards: the board restarted in between.
                    restarted = up < prev_up - 1
                else:
                    restarted = up < FRESH_BOOT_S
                if restarted:
                    snap['booted_at'] = ts - up
                elif prev and prev.get('booted_at'):
                    snap['booted_at'] = prev['booted_at']
            if port not in n['xiao'] and len(n['xiao']) >= 8:
                return
            n['xiao'][port] = snap
            reset = snap.get('reset')
            event = restarted and reset in BAD_RESETS and n['enabled']
            node = self.public(n) if event else None
        if event:
            self._emit('restarted', node, {'port': port, 'reset': reset,
                                           'uptime_s': up, 'at': ts - up})

    # -- commands -------------------------------------------------------------------------
    def queue_command(self, node_id, port, command):
        n = self._nodes.get(node_id)
        if n is None:
            return None
        if command not in COMMANDS:
            raise ValueError('command must be one of %s' % ', '.join(COMMANDS))
        if not isinstance(port, str) or not port or len(port) > 64:
            raise ValueError('port is required')
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO node_commands(node_id, port, command, created_at) VALUES(?,?,?,?)",
            (node_id, port, command, now))
        cid = cur.lastrowid
        self.db.execute(
            "DELETE FROM node_commands WHERE node_id=? AND id <= (SELECT id FROM node_commands"
            " WHERE node_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)", (node_id, node_id, COMMAND_KEEP))
        if n['kind'] == LOCAL_KIND:
            # The local XIAO is on this machine's own port: write it now.
            ok = bool(self.local_send and self.local_send(port, command))
            self.db.execute("UPDATE node_commands SET delivered_at=?, done_at=?, ok=?, result=?"
                            " WHERE id=?", (now, time.time(), int(ok),
                                            'written to the port' if ok else 'port not connected',
                                            cid))
        return self._command(cid)

    def _command(self, cid):
        r = self.db.one("SELECT * FROM node_commands WHERE id=?", (cid,))
        if r is None:
            return None
        d = dict(r)
        d['ok'] = None if d['ok'] is None else bool(d['ok'])
        d['state'] = ('done' if d['done_at'] else 'delivered' if d['delivered_at'] else 'queued')
        return d

    def commands(self, node_id, limit=20):
        rows = self.db.query("SELECT id FROM node_commands WHERE node_id=? ORDER BY id DESC LIMIT ?",
                             (node_id, max(1, min(int(limit), COMMAND_KEEP))))
        return [self._command(r['id']) for r in rows]

    def take_commands(self, node_id, now=None):
        """Commands waiting for this relay, marked delivered. Stale ones expire."""
        now = now or time.time()
        self.db.execute("UPDATE node_commands SET done_at=?, ok=0, result='expired: the relay"
                        " did not pick it up' WHERE node_id=? AND delivered_at IS NULL"
                        " AND created_at < ?", (now, node_id, now - COMMAND_TTL_S))
        rows = self.db.query("SELECT id, port, command FROM node_commands WHERE node_id=?"
                             " AND delivered_at IS NULL AND done_at IS NULL ORDER BY id",
                             (node_id,))
        if rows:
            self.db.executemany("UPDATE node_commands SET delivered_at=? WHERE id=?",
                                [(now, r['id']) for r in rows])
        return [{'id': r['id'], 'port': r['port'], 'command': r['command']} for r in rows]

    def command_result(self, node_id, cid, ok, detail):
        self.db.execute("UPDATE node_commands SET done_at=?, ok=?, result=? WHERE id=? AND node_id=?"
                        " AND done_at IS NULL",
                        (time.time(), int(bool(ok)), str(detail or '')[:200], cid, node_id))

    # -- monitor --------------------------------------------------------------------------
    def _emit(self, kind, node, detail):
        try:
            self.on_event(kind, node, detail)
        except Exception as e:
            logger.warning('node event %s failed: %s', kind, e)

    def check(self, now=None):
        """Compare each node's state with the last one alerted on; report changes.

        The first state seen for a node is its baseline (no alert). Going from
        online to offline or silent alerts; coming back alerts 'recovered'.
        'waiting' and 'disabled' never alert and leave the baseline alone.
        """
        now = now or time.time()
        events = []
        with self._lock:
            for n in self._nodes.values():
                st = self.state(n, now)
                if st in ('waiting', 'disabled'):
                    continue
                prev = n['alert_state']
                if prev == st:
                    continue
                n['alert_state'] = st
                self._dirty.add(n['id'])
                if prev is None:
                    continue
                if st in ('relay_offline', 'xiao_silent'):
                    events.append((st, self.public(n, now)))
                elif st == 'online' and prev in ('relay_offline', 'xiao_silent'):
                    events.append(('recovered', self.public(n, now)))
        for kind, node in events:
            self._emit(kind, node, {})
        return events

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name='nodes')
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.persist()

    def _loop(self):
        last_check = time.time()
        while not self._stop.wait(PERSIST_EVERY_S):
            try:
                if time.time() - last_check >= CHECK_EVERY_S:
                    last_check = time.time()
                    self.check()
                self.persist()
            except Exception as e:
                logger.warning('node monitor: %s', e)


class BatchIngest:
    """One relay batch -> raw view, node status, parser and sessionizer."""

    def __init__(self, registry, sess, rawlog):
        self.reg = registry
        self.sess = sess
        self.raw = rawlog
        self._parsers = {}              # (node_id, port) -> LineParser

    @staticmethod
    def validate(body):
        """Raise ValueError unless `body` is a well-formed batch."""
        if not isinstance(body, dict):
            raise ValueError('expected a JSON object')
        lines = body.get('lines', [])
        if not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise ValueError('lines must be a list of at most %d' % MAX_LINES)
        for it in lines:
            if not isinstance(it, dict):
                raise ValueError('each line must be an object')
            seq = it.get('seq')
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                raise ValueError('seq must be a positive integer')
            port = it.get('port')
            if not isinstance(port, str) or not port or len(port) > 64:
                raise ValueError('port must be a short string')
            if not isinstance(it.get('line'), str):
                raise ValueError('line must be a string')
            for k in ('age', 'wall'):
                if it.get(k) is not None and _num(it[k]) is None:
                    raise ValueError('%s must be a number' % k)
        spool = body.get('spool_id')
        if lines and (not isinstance(spool, str) or not 1 <= len(spool) <= 64):
            raise ValueError('spool_id is required with lines')
        results = body.get('results', [])
        if not isinstance(results, list) or len(results) > 100 \
                or not all(isinstance(r, dict) for r in results):
            raise ValueError('results must be a list of objects')

    @staticmethod
    def heard_at(item, received_at):
        """(time the line was heard, how late it arrived).

        A line read during the relay's current run carries its age on the
        relay's monotonic clock, which no wall-clock error on the Pi can touch.
        A line spooled before the relay restarted carries the Pi's wall-clock
        time instead, which is trusted only within the spool's lifetime.
        """
        age = _num(item.get('age'))
        if age is not None:
            age = min(max(age, 0.0), MAX_BACKLOG_S)
            return received_at - age, age
        wall = _num(item.get('wall'))
        if wall is not None:
            ts = max(min(wall, received_at), received_at - MAX_BACKLOG_S)
            return ts, received_at - ts
        return received_at, 0.0

    def handle(self, node, body, received_at=None):
        """Store a validated batch for `node` (a registry dict). Returns the reply."""
        received_at = received_at or time.time()
        nid = node['id']
        with self.reg.ingest_lock(nid):
            for r in body.get('results') or []:
                if isinstance(r.get('id'), int):
                    self.reg.command_result(nid, r['id'], r.get('ok'), r.get('detail'))
            self.reg.contact(nid, body.get('relay'), received_at)
            spool = body.get('spool_id')
            ack = node['ack_seq'] if spool and spool == node['spool_id'] else 0
            lines = sorted(body.get('lines') or [], key=lambda it: it['seq'])
            stored = 0
            if lines:
                with self.sess.batch():
                    for it in lines:
                        if it['seq'] <= ack:
                            continue            # already stored: a resent batch
                        try:
                            self._line(node, it, received_at)
                        except Exception as e:
                            # Acked anyway: a line that cannot be stored now never
                            # will be, and resending it forever would wedge the relay.
                            logger.warning('node %s: line %s not stored: %s',
                                           node['name'], it['seq'], e)
                        ack = it['seq']
                        stored += 1
                    self.sess.flush()
                self.reg.set_cursor(nid, spool, ack)
            return {'ack_seq': ack, 'stored': stored, 'node': node['name'],
                    'server_time': received_at, 'commands': self.reg.take_commands(nid)}

    def _line(self, node, it, received_at):
        port, line = it['port'], it['line'][:MAX_LINE_CHARS].strip()
        if not line:
            return
        ts, delay = self.heard_at(it, received_at)
        src = '%s/%s' % (node['name'], short_port(port))
        key = (node['id'], port)
        parser = self._parsers.get(key)
        if parser is None:
            parser = self._parsers[key] = LineParser(source=src)
        det = parser.feed(line)
        kind = 'detection' if det is not None else classify_line(line)
        try:
            if det is not None:
                det['rx_node'] = node['id']
                det['delay'] = delay
                self.sess.ingest(det, ts)
        finally:
            self.raw.record(src, kind, line, ts)
            self.reg.line_seen(node['id'], port, ts, line, kind)
