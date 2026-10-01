"""Serial port lifecycle and raw-line keeping, shared by the server and the relay.

The DTR/RTS handling and the one-reader-per-port guard are lifted from
mesh-mapper.py (:12871 and :13077). Both encode hardware behaviour that was
learned the hard way and would be easy to reintroduce as a bug:

  * pyserial asserts DTR and RTS on open. On an ESP32-S3 talking over its native
    USB those lines are wired into the reset logic, so a plain serial.Serial()
    reboots the board - and the reconnect loop then reboots it again on every
    retry.
  * Two readers on one port steal bytes from each other, producing truncated
    JSON and an endless connect/disconnect cycle in the UI.

`SerialReader` owns the ports and knows nothing about what a line means: every
line goes to a callback. The server parses and ingests it (serial_source.py);
the relay on a remote Pi spools it for sending (relay.py). Nothing here may
import Flask - the relay runs on Pis that do not have it.

`RawLog` keeps the raw traffic - every line read and every line written - in a
small per-source ring buffer (the "Raw serial output" view) and a size-capped
log file, because "is the node saying anything at all?" is the first question
when detections stop, and the parser silently drops everything that is not a
detection. Sources are names like `home/ttyACM0` or `north/ttyACM0`, so a
remote node's lines sit beside the local ones.
"""
import collections
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

from ..parse import extract_json

logger = logging.getLogger(__name__)

BAUD_RATE = 115200
PORT_MONITOR_INTERVAL = 10.0
MAX_CONNECTION_ATTEMPTS = 5
BACKOFF_LONG_S = 30.0

# Espressif's USB vendor id: the XIAO ESP32-S3's native USB enumerates with it,
# which is how a relay set to `auto` tells the node from any other serial device.
ESPRESSIF_VID = 0x303A

# Raw output kept per source for the raw view. A busy node emits a few lines a
# second and the heartbeat is once a minute, so 500 covers minutes of traffic
# while costing well under a megabyte even with several sources.
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


def short_port(port):
    """/dev/ttyACM0 -> ttyACM0, so a source reads north/ttyACM0, not north//dev/..."""
    port = str(port)
    return port[5:] if port.startswith('/dev/') else port


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
                    'hwid': getattr(p, 'hwid', '') or '',
                    'vid': getattr(p, 'vid', None), 'pid': getattr(p, 'pid', None)})
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


class SerialReader:
    """One reader thread per port, plus an optional monitor for hot-plugging.

    on_line(port, line, t_monotonic) runs on the port's own thread for every
    non-empty line. An exception from it is logged and the reader pauses a
    second, as it always has; it never kills the thread. on_sent(port, line)
    runs after each successful write - the WATCHDOG_RESET sent on connect and
    every send() - so the raw view can show host-to-device traffic too.
    `opener` replaces open_serial_no_reset, for tests with a fake port.
    """

    def __init__(self, on_line, on_status=None, on_sent=None, opener=None):
        self.on_line = on_line
        self.on_status = on_status or (lambda statuses: None)
        self.on_sent = on_sent or (lambda port, line: None)
        self._open = opener or open_serial_no_reset
        self.status = {}                # device -> bool connected
        self._threads = {}              # device -> Thread
        self._sers = {}                 # device -> Serial
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor = None

    def _set_status(self, port, connected):
        self.status[port] = connected
        try:
            self.on_status(dict(self.status))
        except Exception:
            pass

    # -- control --------------------------------------------------------------
    def ports(self):
        """Ports with a reader started (and not stopped since)."""
        with self._lock:
            return list(self._threads)

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

    def start_monitor(self, wanted, interval=PORT_MONITOR_INTERVAL, lister=None):
        """Start or stop readers as devices come and go.

        wanted(available) gets list_ports()'s dicts and returns the devices that
        should have a reader. Checked every `interval` seconds, acting only when
        the set of present devices changes: a wanted device that appears gets a
        reader, and a connected one that vanishes is marked down and stopped.
        """
        if self._monitor is not None:
            return
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         args=(wanted, interval, lister or list_ports),
                                         daemon=True, name='serial-monitor')
        self._monitor.start()

    def _monitor_loop(self, wanted, interval, lister):
        last = set()
        while not self._stop.wait(interval):
            try:
                available = lister()
                current = {p['device'] for p in available}
                if current == last:
                    continue
                logger.info("port availability changed: %s", sorted(current))
                for port in wanted(available):
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

    def send(self, port, command):
        """Write one line to a device. False when the port is not open or the
        write failed. Only node-mode home firmware parses anything it is sent
        (WATCHDOG_RESET, STATUS); the dualcore build ignores serial input."""
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
        self._sent(port, line)
        return True

    def _sent(self, port, line):
        try:
            self.on_sent(port, line)
        except Exception:
            pass

    # -- the reader -----------------------------------------------------------
    def _reader(self, port):
        ser = None
        attempts = 0
        logger.info("starting serial reader for %s", port)

        while not self._stop.is_set():
            with self._lock:
                if self._threads.get(port) is not threading.current_thread():
                    break                      # superseded or deselected

            if ser is None or not getattr(ser, 'is_open', False):
                try:
                    ser = self._open(port)
                    attempts = 0
                    with self._lock:
                        self._sers[port] = ser
                    self._set_status(port, True)
                    logger.info("opened %s at %d baud", port, BAUD_RATE)
                    try:
                        time.sleep(0.5)
                        ser.write(b'WATCHDOG_RESET\n')
                        self._sent(port, 'WATCHDOG_RESET')
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
                self.on_line(port, line, time.monotonic())
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
        logger.info("serial reader for %s stopped", port)


class RawLog:
    """Recent raw lines per source, plus the size-capped file log.

    record() runs on reader and request threads for every line, so it stays
    cheap and swallows its own errors: losing a line of diagnostics is fine,
    stalling or killing ingest is not. The file write happens on one writer
    thread; record() only queues it. `path=None` keeps lines in memory only.
    """

    def __init__(self, path=None):
        self._raw = {}                  # source -> deque of (seq, ts, kind, text)
        self._lock = threading.Lock()
        self._seq = 0                   # last sequence number handed out, all sources
        self._evicted = {}              # source -> seq of the newest line pushed out
        # Sequence numbers restart with the process, so a page's cursor is only
        # meaningful alongside the process it came from.
        self.boot = os.urandom(4).hex()
        self.dropped = 0                # lines the file writer could not keep up with
        self._stop = threading.Event()
        self._handler = None
        self._q = None
        self._writer = None
        self.path = None                # None when the file log is off or unwritable
        if path:
            self._open(path)

    def _open(self, path):
        path = os.path.abspath(path)
        try:
            # Opened now rather than on first write, so a read-only SD card or a
            # root-owned file left by an earlier `sudo` run shows up once at
            # startup - and so `tail -f` has a file to follow before any traffic.
            handler = _RawLogHandler(path)
        except Exception as e:
            logger.warning("serial log disabled: cannot open %s (%s)", path, e)
            return
        # One log per process in practice. Replacing rather than adding keeps a
        # second one in the same process (tests) from writing every line into
        # two files.
        for old in list(raw_logger.handlers):
            raw_logger.removeHandler(old)
            old.close()
        raw_logger.addHandler(handler)
        self._handler = handler
        self.path = path
        self._q = queue.Queue(maxsize=RAW_LOG_QUEUE)
        self._writer = threading.Thread(target=self._loop, args=(self._q,),
                                        daemon=True, name='serial-raw-log')
        self._writer.start()

    def _loop(self, q):
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

    def stop(self):
        self._stop.set()
        q, self._q = self._q, None
        if q is not None:
            try:
                q.put_nowait(None)      # write out what is queued, then exit
            except queue.Full:
                pass                    # the writer also exits on _stop once idle
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.join(timeout=2.0)
        handler, self._handler = self._handler, None
        # A writer still stuck in a write keeps its handler; it is a daemon
        # thread, so it cannot hold up process exit.
        if handler is not None and (writer is None or not writer.is_alive()):
            raw_logger.removeHandler(handler)
            handler.close()

    def record(self, source, kind, text, ts=None):
        """Keep one raw line (kind is one of RAW_KINDS) for the view and the file.

        kind=None means "the parser rejected it - classify it here", inside the
        guard. `ts` is when the line was read; it defaults to now and is used for
        display only - ordering is by sequence number.
        """
        try:
            if kind is None:
                kind = classify_line(text)
            text = _clip(text)
            if ts is None:
                ts = time.time()
            with self._lock:
                self._seq += 1
                seq = self._seq
                buf = self._raw.get(source)
                if buf is None:
                    buf = self._raw[source] = collections.deque(maxlen=RAW_BUFFER_LINES)
                elif len(buf) == buf.maxlen:
                    # The oldest line is about to go. Remember how far that has
                    # reached, so a page whose cursor is older is told lines were
                    # skipped instead of silently never seeing them.
                    self._evicted[source] = buf[0][0]
                buf.append((seq, ts, kind, text))
            q = self._q
            if q is not None:
                try:
                    q.put_nowait('%s %s %s %s' % (_stamp(ts), source,
                                                  '>' if kind == 'sent' else '<', text))
                except queue.Full:
                    self.dropped += 1
                    if self.dropped == 1:
                        logger.warning("serial log is not keeping up (slow storage?); "
                                       "dropping lines from the file until it catches up")
        except Exception:
            pass

    def lines(self, source=None, since=None, limit=200, sources=()):
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
        skipped rather than pretend the stream is continuous. `ports` lists
        every source with buffered output plus `sources` (ones expected to
        talk, such as selected ports), for the page's picker.
        """
        limit = max(1, min(int(limit), RAW_BUFFER_LINES))
        picked, more = [], False
        with self._lock:
            # `sources` can come from a JSON body; only strings can be names.
            names = sorted(set(self._raw) | {s for s in sources if isinstance(s, str)})
            bufs = ([(source, self._raw.get(source, ()))] if source
                    else list(self._raw.items()))
            for p, buf in bufs:
                if since is not None and self._evicted.get(p, 0) > since:
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
            latest = self._seq
        picked.sort(key=lambda e: e[0])
        if len(picked) > limit:
            picked, more = picked[-limit:], True
        return {'lines': [{'seq': seq, 't': ts, 'port': p, 'kind': kind, 'text': text}
                          for seq, ts, p, kind, text in picked],
                'more': more, 'ports': names, 'seq': latest, 'boot': self.boot}
