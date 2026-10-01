"""Event bus + Server-Sent Events stream.

The legacy app broadcast full state on every detection - all of tracked_pairs,
every path rebuilt, and the entire cumulative CSV re-read from disk and shipped
to every client (mesh-mapper.py:13481). This does the opposite: subscribers get
deltas only, and position updates are coalesced on a fixed tick so the message
rate is bounded by the tick, not by the detection rate.

SSE rather than WebSockets because the traffic is entirely one-way. It needs no
extra dependency (flask-socketio + simple-websocket are not installed, and
without the latter socket.io silently degrades to long-polling anyway), it
survives proxies, and the browser reconnects on its own.
"""
import json
import queue
import threading
import time

TICK_HZ = 4.0
QUEUE_MAX = 200                # per subscriber; a slow client is dropped, not buffered
HEARTBEAT_S = 20.0             # keeps proxies from closing an idle stream


class EventBus:
    def __init__(self, tick_hz=TICK_HZ):
        self._subs = set()
        self._lock = threading.Lock()
        self._pending = {}         # flight_id -> latest position payload
        self._plock = threading.Lock()
        self._stop = threading.Event()
        self._interval = 1.0 / tick_hz
        self._thread = threading.Thread(target=self._loop, daemon=True, name='flightlog-bus')
        self._thread.start()

    # -- subscriptions --------------------------------------------------------
    def subscribe(self):
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    @property
    def subscribers(self):
        with self._lock:
            return len(self._subs)

    # -- publishing -----------------------------------------------------------
    def publish(self, name, payload):
        """Send an event to every subscriber immediately."""
        msg = (name, payload)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # A client that cannot keep up loses updates rather than making
                # the writer block. It re-syncs on its next /api/live fetch.
                pass

    def publish_position(self, payload):
        """Coalesce a position update; only the newest per flight survives a tick."""
        fid = payload.get('flight_id')
        if fid is None:
            return
        with self._plock:
            self._pending[fid] = payload

    def _loop(self):
        last_beat = time.time()
        while not self._stop.wait(self._interval):
            with self._plock:
                batch = list(self._pending.values())
                self._pending.clear()
            if batch:
                self.publish('positions', {'t': time.time(), 'flights': batch})
            elif time.time() - last_beat > HEARTBEAT_S:
                self.publish('ping', {'t': time.time()})
                last_beat = time.time()
            if batch:
                last_beat = time.time()

    def stop(self):
        self._stop.set()

    # -- SSE ------------------------------------------------------------------
    def sse(self):
        """Generator for a text/event-stream response."""
        q = self.subscribe()
        try:
            yield 'retry: 3000\n\n'
            yield self._frame('hello', {'t': time.time()})
            while not self._stop.is_set():
                try:
                    name, payload = q.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                yield self._frame(name, payload)
        finally:
            self.unsubscribe(q)

    @staticmethod
    def _frame(name, payload):
        return 'event: %s\ndata: %s\n\n' % (name, json.dumps(payload, default=str))
