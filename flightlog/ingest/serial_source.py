"""USB serial ingest.

The DTR/RTS handling and the one-reader-per-port guard are lifted from
mesh-mapper.py (:12871 and :13077). Both encode hardware behaviour that was
learned the hard way and would be easy to reintroduce as a bug:

  * pyserial asserts DTR and RTS on open. On an ESP32-S3 talking over its native
    USB those lines are wired into the reset logic, so a plain serial.Serial()
    reboots the board - and the reconnect loop then reboots it again on every
    retry.
  * Two readers on one port steal bytes from each other, producing truncated
    JSON and an endless connect/disconnect cycle in the UI.

Line parsing lives in flightlog.parse; this module only owns the port.

It also keeps the raw traffic - every line read and every line written - in a
small per-port ring buffer (the Sources page's "Raw serial output") and a
size-capped log file, because "is the node saying anything at all?" is the first
question when detections stop, and the parser silently drops everything that is
not a detection.
"""
import collections
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time

import serial
import serial.tools.list_ports

from ..parse import LineParser, extract_json

logger = logging.getLogger(__name__)

BAUD_RATE = 115200
PORT_MONITOR_INTERVAL = 10.0
MAX_CONNECTION_ATTEMPTS = 5
BACKOFF_LONG_S = 30.0

# Raw output kept per port for the Sources page. A busy node emits a few lines a
# second and the heartbeat is once a minute, so 500 covers minutes of traffic
# while costing well under a megabyte even with several ports.
RAW_BUFFER_LINES = 500
# A line without a newline (wrong baud rate, a board mid-crash) comes back from
# readline() only at the 1 s timeout, so it can be ~11 KB. Cut long ones: the
# point is to see what is arriving, and one junk line must not evict the rest.
RAW_MAX_CHARS = 500
RAW_KINDS = ('detection', 'heartbeat', 'other', 'sent')

RAW_LOG_NAME = 'flightlog_serial.log'
RAW_LOG_MAX_BYTES = 1024 * 1024
RAW_LOG_BACKUPS = 2
# Lines waiting for the log writer thread. A Pi's SD card can stall a single
# write for hundreds of milliseconds; this absorbs minutes of a busy node's
# traffic before lines are dropped - from the file only, never from ingest or
# from the page.
RAW_LOG_QUEUE = 2000

# Dedicated and non-propagating: raw lines go to the serial log file only, never
# to the console or whatever handler the root logger has.
raw_logger = logging.getLogger('flightlog.serial_raw')
raw_logger.propagate = False
raw_logger.setLevel(logging.INFO)

# Everything on this wire is ultimately over-the-air input - the dualcore build
# snprintf()s the raw UAS ID into its JSON unescaped - so a broadcaster can put
# ESC sequences on our serial line. `tail -f` on the log would hand those to the
# terminal, so control characters (and the C1 range) are made visible instead.
_CONTROL_RE = re.compile(r'[\x00-\x08\x0a-\x1f\x7f-\x9f]')


def _escape_control(m):
    return '\\x%02x' % ord(m.group())


def _clip(text):
    """Bound a raw line's length and neutralize control characters."""
    extra = len(text) - RAW_MAX_CHARS
    if extra > 0:
        text = text[:RAW_MAX_CHARS]
    text = _CONTROL_RE.sub(_escape_control, text)
    if extra > 0:
        text += ' ...[+%d chars]' % extra
    return text


def classify_line(line):
    """'heartbeat' for a node's liveness/status chatter, otherwise 'other'.

    Only called for lines the parser rejected, which are rare, so re-parsing is
    affordable. Covers what the in-tree firmware prints: {"heartbeat": ...}
    (remoteid-mesh, node-mode remote and home), the C5 build's
    {"status":"active",...}, the dualcore build's not-quite-JSON
    {"   [+] Device is active and scanning..."}, and node-mode home's periodic
    "[HOME] Stats/Health/Host" dump. Boot warnings ("[WARN] Boot #3 after a
    BROWNOUT") deliberately stay 'other': dimming them would hide the very lines
    worth reading.
    """
    low = line.lower()
    if 'heartbeat' in low or 'device is active' in low or low.startswith('[home]'):
        return 'heartbeat'
    obj = extract_json(line)
    if obj is not None and 'status' in obj:
        return 'heartbeat'
    return 'other'


def _stamp(ts):
    """Local time with milliseconds, e.g. 2026-09-18 12:00:01.123."""
    return '%s.%03d' % (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)),
                        int((ts % 1) * 1000))


def _backup_name(default):
    """flightlog_serial.log.1 -> flightlog_serial.1.log

    The stdlib's default backup names end in .1/.2, which slip past the repo's
    `*.log` ignore rule - and these files hold positions of real aircraft.
    """
    root, n = default.rsplit('.', 1)
    base, ext = os.path.splitext(root)
    return '%s.%s%s' % (base, n, ext)


class _RawLogHandler(logging.handlers.RotatingFileHandler):
    """Size-capped raw serial log whose backups keep the .log suffix."""

    def __init__(self, path):
        super().__init__(path, maxBytes=RAW_LOG_MAX_BYTES,
                         backupCount=RAW_LOG_BACKUPS, encoding='utf-8')
        self.namer = _backup_name
        self.setFormatter(logging.Formatter('%(message)s'))
        self._warned = False

    def handleError(self, record):
        # The stdlib prints a full traceback per failed record. A full disk, or a
        # rollover refused on Windows because an editor holds the file, would do
        # that for every serial line; one warning is enough, and the next write
        # simply tries again.
        if not self._warned:
            self._warned = True
            logger.warning("serial log %s: write failed (%s); further errors suppressed",
                           self.baseFilename, sys.exc_info()[1])


def list_ports():
    out = []
    for p in serial.tools.list_ports.comports():
        out.append({'device': p.device, 'description': p.description or '',
                    'hwid': getattr(p, 'hwid', '') or ''})
    return out


def open_serial_no_reset(port, baudrate=BAUD_RATE, timeout=1):
    """Open a port WITHOUT rebooting the board on the other end.

    Setting dtr/rts False before open() stores the desired line state, which
    open() then applies - leaving the chip out of reset and running.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate
    ser.timeout = timeout
    try:
        ser.dtr = False
        ser.rts = False
    except Exception as e:
        # Some platforms refuse line-state changes before open; the port is
        # still usable, it may just reset the board on connect.
        logger.debug("could not pre-clear DTR/RTS for %s: %s", port, e)
    ser.open()
    return ser


class SerialManager:
    """Owns the reader threads, one per port, and the port monitor."""

    def __init__(self, sessionizer, state_path=None, on_status=None,
                 raw_log=True, raw_log_path=None):
        self.sess = sessionizer
        self.on_status = on_status or (lambda statuses: None)
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'flightlog_ports.json')
        self.status = {}                # device -> bool connected
        self.counts = {}                # device -> detections accepted
        self.last_line = {}             # device -> time any line arrived (heartbeats count)
        self.last_detection = {}        # device -> time a detection arrived
        self._threads = {}              # device -> Thread
        self._sers = {}                 # device -> Serial
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor = None
        self.selected = self._load()

        self._raw = {}                  # device -> deque of (seq, ts, kind, text)
        self._raw_lock = threading.Lock()
        self._raw_seq = 0               # last sequence number handed out, all ports
        self._raw_evicted = {}          # device -> seq of the newest line pushed out
        # Sequence numbers restart with the process, so a page's cursor is only
        # meaningful alongside the process it came from.
        self.raw_boot = os.urandom(4).hex()
        self.raw_log_dropped = 0        # lines the file writer could not keep up with
        self._raw_handler = None
        self._raw_q = None
        self._raw_writer = None
        self.raw_log_path = None        # None when the file log is off or unwritable
        if raw_log:
            self._open_raw_log(raw_log_path or os.path.join(
                os.path.dirname(self.state_path), RAW_LOG_NAME))

    def _open_raw_log(self, path):
        path = os.path.abspath(path)
        try:
            # Opened now rather than on first write, so a read-only SD card or a
            # root-owned file left by an earlier `sudo` run shows up once at
            # startup - and so `tail -f` has a file to follow before any traffic.
            handler = _RawLogHandler(path)
        except Exception as e:
            logger.warning("serial log disabled: cannot open %s (%s)", path, e)
            return
        # One manager per process in practice. Replacing rather than adding keeps
        # a second manager in the same process (tests) from writing every line
        # into two files.
        for old in list(raw_logger.handlers):
            raw_logger.removeHandler(old)
            old.close()
        raw_logger.addHandler(handler)
        self._raw_handler = handler
        self.raw_log_path = path
        self._raw_q = queue.Queue(maxsize=RAW_LOG_QUEUE)
        self._raw_writer = threading.Thread(target=self._raw_log_loop, args=(self._raw_q,),
                                            daemon=True, name='serial-raw-log')
        self._raw_writer.start()

    def _raw_log_loop(self, q):
        """Write queued raw lines to the file, on this thread only.

        A hand-rolled loop rather than logging's QueueListener, whose stop()
        blocks forever when the queue is full or a write is stuck - exactly the
        stalled-SD-card case this thread exists for.
        """
        while True:
            try:
                msg = q.get(timeout=1.0)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if msg is None:
                return
            raw_logger.info('%s', msg)

    # -- persistence ----------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path, encoding='utf-8') as fh:
                data = json.load(fh)
            return [d for d in data.get('ports', []) if d]
        except (OSError, ValueError):
            return []

    def _save(self):
        try:
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'ports': self.selected}, fh, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.debug("could not save selected ports: %s", e)

    def _set_status(self, port, connected):
        self.status[port] = connected
        try:
            self.on_status(dict(self.status))
        except Exception:
            pass

    # -- control --------------------------------------------------------------
    def select(self, ports):
        """Replace the selected port set, starting/stopping readers to match."""
        ports = [p for p in (ports or []) if p]
        with self._lock:
            self.selected = ports
            self._save()
        for p in ports:
            self.start(p)
        for p in list(self._threads):
            if p not in ports:
                self.stop_port(p)
        return self.selected

    def start(self, port):
        """Start the reader for `port` unless one is already running."""
        with self._lock:
            t = self._threads.get(port)
            if t is not None and t.is_alive():
                logger.info("reader already running for %s", port)
                return t
            self.status.setdefault(port, False)
            t = threading.Thread(target=self._reader, args=(port,), daemon=True,
                                 name='serial:' + str(port))
            self._threads[port] = t
            t.start()
            return t

    def stop_port(self, port):
        with self._lock:
            self._threads.pop(port, None)
            ser = self._sers.pop(port, None)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._set_status(port, False)

    def stop(self):
        self._stop.set()
        for p in list(self._threads):
            self.stop_port(p)
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        q, self._raw_q = self._raw_q, None
        if q is not None:
            try:
                q.put_nowait(None)      # write out what is queued, then exit
            except queue.Full:
                pass                    # the writer also exits on _stop once idle
        writer, self._raw_writer = self._raw_writer, None
        if writer is not None:
            writer.join(timeout=2.0)
        handler, self._raw_handler = self._raw_handler, None
        # A writer still stuck in a write keeps its handler; it is a daemon
        # thread, so it cannot hold up process exit.
        if handler is not None and (writer is None or not writer.is_alive()):
            raw_logger.removeHandler(handler)
            handler.close()

    def autostart(self):
        """Connect to any saved port that is currently present."""
        available = {p['device'] for p in list_ports()}
        started = [p for p in self.selected if p in available]
        for p in started:
            self.start(p)
        self.start_monitor()
        return started

    def start_monitor(self):
        if self._monitor is not None:
            return
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True,
                                         name='serial-monitor')
        self._monitor.start()

    def _monitor_loop(self):
        """Reconnect saved ports when they reappear; mark vanished ones down."""
        last = set()
        while not self._stop.wait(PORT_MONITOR_INTERVAL):
            try:
                current = {p['device'] for p in list_ports()}
                if current == last:
                    continue
                logger.info("port availability changed: %s", sorted(current))
                for port in self.selected:
                    if port in current:
                        t = self._threads.get(port)
                        if t is None or not t.is_alive():
                            self.start(port)
                for port in list(self.status):
                    if port not in current and self.status.get(port):
                        logger.warning("port %s disconnected", port)
                        self.stop_port(port)
                last = current
            except Exception as e:
                logger.error("port monitor error: %s", e)
                self._stop.wait(5)

    # -- the reader -----------------------------------------------------------
    def _reader(self, port):
        ser = None
        attempts = 0
        parser = LineParser(source=port)
        self.counts.setdefault(port, 0)
        logger.info("starting serial reader for %s", port)

        while not self._stop.is_set():
            with self._lock:
                if self._threads.get(port) is not threading.current_thread():
                    break                      # superseded or deselected

            if ser is None or not getattr(ser, 'is_open', False):
                try:
                    ser = open_serial_no_reset(port)
                    attempts = 0
                    with self._lock:
                        self._sers[port] = ser
                    self._set_status(port, True)
                    logger.info("opened %s at %d baud", port, BAUD_RATE)
                    try:
                        time.sleep(0.5)
                        ser.write(b'WATCHDOG_RESET\n')
                        self._record(port, 'sent', 'WATCHDOG_RESET')
                    except Exception as e:
                        logger.debug("watchdog write failed on %s: %s", port, e)
                except Exception as e:
                    attempts += 1
                    self._set_status(port, False)
                    logger.error("cannot open %s (attempt %d): %s", port, attempts, e)
                    if attempts >= MAX_CONNECTION_ATTEMPTS:
                        logger.warning("backing off on %s for %ds", port, BACKOFF_LONG_S)
                        self._stop.wait(BACKOFF_LONG_S)
                        attempts = 0
                    else:
                        self._stop.wait(1.0)
                    continue

            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                # Any line proves the node is alive. With nothing in range the
                # firmware's once-a-minute status line is the only traffic, and
                # without tracking it a working node and a wedged one look the same.
                self.last_line[port] = time.time()
                det = parser.feed(line)
                try:
                    if det is None:
                        continue                # heartbeat, banner, or junk
                    self.sess.ingest(det)
                    self.counts[port] = self.counts.get(port, 0) + 1
                    self.last_detection[port] = time.time()
                finally:
                    # After ingest, so keeping diagnostics can never delay a
                    # detection; in a finally, so the line is still kept when
                    # ingest raises.
                    self._record(port, 'detection' if det is not None else None, line)
            except (serial.SerialException, OSError) as e:
                logger.error("serial error on %s: %s", port, e)
                self._set_status(port, False)
                try:
                    if ser and ser.is_open:
                        ser.close()
                except Exception:
                    pass
                ser = None
                with self._lock:
                    self._sers.pop(port, None)
                self._stop.wait(1.0)
            except Exception as e:
                logger.error("unexpected error on %s: %s", port, e)
                self._stop.wait(1.0)

        try:
            if ser and ser.is_open:
                ser.close()
        except Exception:
            pass
        with self._lock:
            self._sers.pop(port, None)
        self._set_status(port, False)
        logger.info("serial reader for %s stopped (%d detections)", port, self.counts.get(port, 0))

    def health(self):
        """Seconds since each port last produced any line, and a detection."""
        now = time.time()
        return {'line_age_s': {p: now - t for p, t in self.last_line.items()},
                'detection_age_s': {p: now - t for p, t in self.last_detection.items()}}

    # -- raw output -----------------------------------------------------------
    def _record(self, port, kind, text):
        """Keep one raw line (kind is one of RAW_KINDS) for the page and the log.

        kind=None means "the parser rejected it - classify it here", inside the
        guard. Runs on the reader thread for every line, so it stays cheap and
        swallows its own errors: losing a line of diagnostics is fine, stalling
        or killing ingest is not. The file write itself happens on the writer
        thread; this only queues it.
        """
        try:
            if kind is None:
                kind = classify_line(text)
            text = _clip(text)
            ts = time.time()        # real wall-clock time, for display only
            with self._raw_lock:
                self._raw_seq += 1
                seq = self._raw_seq
                buf = self._raw.get(port)
                if buf is None:
                    buf = self._raw[port] = collections.deque(maxlen=RAW_BUFFER_LINES)
                elif len(buf) == buf.maxlen:
                    # The oldest line is about to go. Remember how far that has
                    # reached, so a page whose cursor is older is told lines were
                    # skipped instead of silently never seeing them.
                    self._raw_evicted[port] = buf[0][0]
                buf.append((seq, ts, kind, text))
            q = self._raw_q
            if q is not None:
                try:
                    q.put_nowait('%s %s %s %s' % (_stamp(ts), port,
                                                  '>' if kind == 'sent' else '<', text))
                except queue.Full:
                    self.raw_log_dropped += 1
                    if self.raw_log_dropped == 1:
                        logger.warning("serial log is not keeping up (slow storage?); "
                                       "dropping lines from the file until it catches up")
        except Exception:
            pass

    def raw_lines(self, port=None, since=None, limit=200):
        """Recent raw lines, oldest first: the newest `limit` after cursor `since`.

        `since` is a line sequence number, not a time. Each kept line takes the
        next number, so a page polling with the last `seq` it has sees every
        line exactly once whatever the wall clock does - and on a Pi with no RTC
        and often no NTP, the clock can sit hours out and then jump. Numbers
        restart with the process; `boot` says which process they belong to, and
        the API route discards a cursor from a different one.

        `more` is True when lines after the cursor are missing from this answer
        - cut by `limit`, or already pushed out of a full buffer (a page paused
        for a few minutes on a busy node) - so the page can say lines were
        skipped rather than pretend the stream is continuous. `ports` is every
        port with buffered output or a selection, for the page's port picker.
        """
        limit = max(1, min(int(limit), RAW_BUFFER_LINES))
        picked, more = [], False
        with self._raw_lock:
            # `selected` comes from a JSON body; only strings can be port names.
            ports = sorted(set(self._raw) | {p for p in self.selected if isinstance(p, str)})
            bufs = ([(port, self._raw.get(port, ()))] if port
                    else list(self._raw.items()))
            for p, buf in bufs:
                if since is not None and self._raw_evicted.get(p, 0) > since:
                    more = True
                n = 0
                for seq, ts, kind, text in reversed(buf):
                    if since is not None and seq <= since:
                        break
                    if n >= limit:
                        more = True
                        break
                    picked.append((seq, ts, p, kind, text))
                    n += 1
            latest = self._raw_seq
        picked.sort(key=lambda e: e[0])
        if len(picked) > limit:
            picked, more = picked[-limit:], True
        return {'lines': [{'seq': seq, 't': ts, 'port': p, 'kind': kind, 'text': text}
                          for seq, ts, p, kind, text in picked],
                'more': more, 'ports': ports, 'seq': latest, 'boot': self.raw_boot}

    def send(self, port, command):
        """Write a line to a device. Only the node-mode home firmware parses
        anything (WATCHDOG_RESET, STATUS); older builds ignore it."""
        with self._lock:
            ser = self._sers.get(port)
        if ser is None or not ser.is_open:
            return False
        try:
            line = command.rstrip('\n')
            ser.write((line + '\n').encode('utf-8'))
        except Exception as e:
            logger.error("send failed on %s: %s", port, e)
            return False
        self._record(port, 'sent', line)
        return True
