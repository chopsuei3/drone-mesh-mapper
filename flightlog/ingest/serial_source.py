"""USB serial ingest on the server: which ports are selected, and what their
lines mean.

Opening, reconnecting and writing to ports is SerialReader's job, and keeping
the raw traffic RawLog's (ingest/reader.py) - the relay on a remote Pi shares
both. This module adds what only the server needs: the saved port selection,
parsing every line into a detection and ingesting it, and the per-port
counters the Sources page shows.

Line parsing lives in flightlog.parse; this module never interprets a line
itself.
"""
import json
import logging
import os
import time

from ..parse import LineParser
from .reader import (  # noqa: F401 - re-exported; older code imports them from here
    BAUD_RATE, PORT_MONITOR_INTERVAL, MAX_CONNECTION_ATTEMPTS, BACKOFF_LONG_S,
    RAW_BUFFER_LINES, RAW_MAX_CHARS, RAW_KINDS, RAW_LOG_NAME, RAW_LOG_MAX_BYTES,
    RAW_LOG_BACKUPS, RAW_LOG_QUEUE, raw_logger, _clip, _backup_name, _RawLogHandler,
    classify_line, list_ports, open_serial_no_reset, RawLog, SerialReader)

logger = logging.getLogger(__name__)


class SerialManager:
    """The server's own ports: selection, parsing and ingest.

    `rawlog` shares one RawLog with the rest of the app, so remote nodes' lines
    appear beside these; without one the manager keeps its own. `source_for`
    maps a port to its name in the raw view (default: the port itself).
    `on_line(port, line, ts, det)` runs after every line, detection or not.
    """

    def __init__(self, sessionizer, state_path=None, on_status=None,
                 raw_log=True, raw_log_path=None, rawlog=None, source_for=None,
                 on_line=None, opener=None):
        self.sess = sessionizer
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'flightlog_ports.json')
        self.counts = {}                # device -> detections accepted
        self.last_line = {}             # device -> time any line arrived (heartbeats count)
        self.last_detection = {}        # device -> time a detection arrived
        self._parsers = {}              # device -> LineParser
        self.selected = self._load()
        self.source_for = source_for or (lambda port: port)
        self._hook = on_line

        self._owns_raw = rawlog is None
        if rawlog is None:
            rawlog = RawLog((raw_log_path or os.path.join(
                os.path.dirname(self.state_path), RAW_LOG_NAME)) if raw_log else None)
        self.raw = rawlog
        self.reader = SerialReader(self._on_line, on_status=on_status,
                                   on_sent=lambda port, line: self._record(port, 'sent', line),
                                   opener=opener)

    # The reader's state, under the names the API and tests have always used.
    @property
    def status(self):
        return self.reader.status

    @property
    def raw_boot(self):
        return self.raw.boot

    @property
    def raw_log_path(self):
        return self.raw.path

    @property
    def raw_log_dropped(self):
        return self.raw.dropped

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

    # -- control --------------------------------------------------------------
    def select(self, ports):
        """Replace the selected port set, starting/stopping readers to match."""
        ports = [p for p in (ports or []) if p]
        self.selected = ports
        self._save()
        for p in ports:
            self.start(p)
        for p in self.reader.ports():
            if p not in ports:
                self.stop_port(p)
        return self.selected

    def start(self, port):
        self.counts.setdefault(port, 0)
        return self.reader.start(port)

    def stop_port(self, port):
        self.reader.stop_port(port)

    def stop(self):
        self.reader.stop()
        if self._owns_raw:
            self.raw.stop()

    def autostart(self):
        """Connect to any saved port that is currently present."""
        available = {p['device'] for p in list_ports()}
        started = [p for p in self.selected if p in available]
        for p in started:
            self.start(p)
        self.start_monitor()
        return started

    def start_monitor(self):
        """Reconnect saved ports when they reappear; mark vanished ones down."""
        self.reader.start_monitor(
            lambda available: [p for p in self.selected
                               if p in {a['device'] for a in available}])

    def send(self, port, command):
        """Write a line to a device. Only the node-mode home firmware parses
        anything (WATCHDOG_RESET, STATUS); older builds ignore it."""
        return self.reader.send(port, command)

    # -- lines ----------------------------------------------------------------
    def _on_line(self, port, line, t_mono):
        # Any line proves the node is alive. With nothing in range the
        # firmware's once-a-minute status line is the only traffic, and
        # without tracking it a working node and a wedged one look the same.
        now = time.time()
        self.last_line[port] = now
        parser = self._parsers.get(port)
        if parser is None:
            parser = self._parsers[port] = LineParser(source=port)
        det = parser.feed(line)
        try:
            if det is not None:
                self.sess.ingest(det)
                self.counts[port] = self.counts.get(port, 0) + 1
                self.last_detection[port] = time.time()
        finally:
            # After ingest, so keeping diagnostics can never delay a
            # detection; in a finally, so the line is still kept when
            # ingest raises.
            self._record(port, 'detection' if det is not None else None, line)
            if self._hook is not None:
                try:
                    self._hook(port, line, now, det)
                except Exception as e:
                    logger.debug("line hook failed on %s: %s", port, e)

    def health(self):
        """Seconds since each port last produced any line, and a detection."""
        now = time.time()
        return {'line_age_s': {p: now - t for p, t in self.last_line.items()},
                'detection_age_s': {p: now - t for p, t in self.last_detection.items()}}

    # -- raw output -----------------------------------------------------------
    def _record(self, port, kind, text):
        """Keep one raw line; see RawLog.record."""
        self.raw.record(self.source_for(port), kind, text)

    def raw_lines(self, port=None, since=None, limit=200):
        """Recent raw lines from this app's sources; see RawLog.lines. `port`
        is a source name, as listed in `ports`."""
        return self.raw.lines(source=port, since=since, limit=limit,
                              sources=[self.source_for(p) for p in self.selected
                                       if isinstance(p, str)])
