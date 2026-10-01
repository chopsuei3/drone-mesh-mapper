"""Live flight sessionization.

The legacy app rebuilt "flights" from scratch on every KML regeneration by
rescanning the whole cumulative CSV (mesh-mapper.py:1603) and then threw the
grouping away. Here a flight is a persisted row that is opened, updated
incrementally and closed as detections stream in - nothing is ever rescanned.

Cost per detection is constant: one dict update, one appended tuple. Rows are
written by a batching flusher, so per-detection work does not grow with the
size of the session. That is the specific defect that made the old UI degrade
as it ran.
"""
import json
import threading
import time

from .geo import haversine_m, bearing_deg, simplify, decimate

# A gap longer than this ends a flight. 60s matches the legacy staleThreshold
# so migrated history lines up with what the old KML produced.
DEFAULT_GAP_S = 60.0

BATCH_ROWS = 50           # flush after this many detections
FLUSH_INTERVAL_S = 1.0    # ...or this often, whichever comes first
JANITOR_INTERVAL_S = 5.0
MAX_LIVE_POINTS = 20000   # guard: simplify an open flight's point buffer past this
SIMPLIFY_TOLERANCE = 0.00002
MAX_STORED_POINTS = 2000

# The outlier gate has two halves, because timing alone is not trustworthy.
#
# Speed: only meaningful when the two fixes are far enough apart in receive time.
# ~430 km/h covers any consumer or racing airframe with margin.
MAX_PLAUSIBLE_SPEED_MS = 120.0
#
# Distance: a hard ceiling that applies no matter the timing. Detections arrive
# in bursts - the home node flushes queued mesh packets back-to-back, a drained
# serial buffer does the same, and an import replays a whole file at once - so
# dt is frequently near zero for perfectly good consecutive fixes. Judging those
# by speed would reject the entire track. RemoteID reception range is a couple
# of km, and even 60s (the gap) at MAX_PLAUSIBLE_SPEED_MS is 7.2 km, so a step
# beyond this is a bad fix rather than flight.
MAX_PLAUSIBLE_STEP_M = 10000.0

# Detections relayed over LoRa arrive seconds to tens of seconds late, and the
# delay varies, so speed derived from receive time is meaningless for them. The
# positions are still good, so distance still accumulates - only speed is
# withheld rather than fabricated.
MIN_SPEED_DT_S = 0.2

# Weak-signal drones at the edge of range are heard sparsely. In a real XIAO
# capture (DJI, -84 to -93 dBm) whole 60 s heartbeat windows held a single
# detection, so consecutive fixes from one continuous flight can be well over
# DEFAULT_GAP_S apart - and the firmware's own heartbeat is also 60 s. A
# detection that returns within DEFAULT_RESUME_S from the same operator position
# is an RF dropout, not a new flight: the RemoteID System message carries the
# operator's location, which does not move between the halves of one session
# (0.2 m spread across that whole capture). Drones that broadcast no operator
# position fall back to the plain gap rule. 0 disables resuming.
DEFAULT_RESUME_S = 180.0
PILOT_MATCH_M = 50.0

# The dualcore firmware's WiFi callback decodes both NAN action frames and
# beacon frames and queues every decode to the printer task with no dedupe, so
# identical detections can arrive back-to-back (2 of 22 lines in that capture
# were byte-identical). One matching the previous detection in position,
# altitude and RSSI within this window carries no information and is dropped
# rather than inflating the point count.
DUP_WINDOW_S = 1.0


class FlightState:
    """In-memory rolling state for one open flight."""
    __slots__ = ('flight_id', 'drone_id', 'mac', 'node_id', 'started_at', 'last_ts',
                 'det_count', 'gps_count', 'path_len_m', 'max_alt', 'min_alt',
                 'max_speed', 'start_lat', 'start_lon', 'end_lat', 'end_lon', 'suspect_count',
                 'bbox_n', 'bbox_s', 'bbox_e', 'bbox_w', 'pilot_lat', 'pilot_lon',
                 'max_rssi', 'min_rssi', 'points', 'prev_lat', 'prev_lon', 'prev_ts',
                 'last_key', 'last_key_ts')

    def __init__(self, flight_id, drone_id, mac, node_id, ts):
        self.flight_id = flight_id
        self.drone_id = drone_id
        self.mac = mac
        self.node_id = node_id
        self.started_at = ts
        self.last_ts = ts
        self.det_count = 0
        self.gps_count = 0
        self.suspect_count = 0
        self.path_len_m = 0.0
        self.max_alt = self.min_alt = None
        self.max_speed = None
        self.start_lat = self.start_lon = None
        self.end_lat = self.end_lon = None
        self.bbox_n = self.bbox_s = self.bbox_e = self.bbox_w = None
        self.pilot_lat = self.pilot_lon = None
        self.max_rssi = self.min_rssi = None
        self.points = []
        self.prev_lat = self.prev_lon = self.prev_ts = None
        self.last_key = None
        self.last_key_ts = None

    def add(self, det, ts):
        """Fold one detection into the rolling stats. Returns (speed, heading).

        Order matters here: the outlier gate runs before any bookkeeping, so a
        bad fix cannot leave a footprint in the bbox, the altitude extremes or
        the end position on its way to being rejected.
        """
        self.det_count += 1
        self.last_ts = ts

        rssi = det.get('rssi')
        if rssi is not None:
            self.max_rssi = rssi if self.max_rssi is None else max(self.max_rssi, rssi)
            self.min_rssi = rssi if self.min_rssi is None else min(self.min_rssi, rssi)

        # Operator position: keep the first one we ever get for this flight.
        if self.pilot_lat is None and det.get('pilot_lat') is not None:
            self.pilot_lat = det['pilot_lat']
            self.pilot_lon = det['pilot_lon']

        lat, lon = det.get('lat'), det.get('lon')
        if lat is None or lon is None:
            # No fix. The detection still counts as presence and keeps the
            # flight alive, but contributes nothing to path length or bbox.
            return None, None

        # Detections relayed over LoRa arrive late by a variable amount, so any
        # speed derived from receive time is meaningless - and that also makes
        # the outlier gate unusable for them. Positions are still trustworthy.
        relayed = bool(det.get('node_id'))

        step = dt = None
        if self.prev_lat is not None:
            step = haversine_m(self.prev_lat, self.prev_lon, lat, lon)
            dt = ts - self.prev_ts
            implausible = step > MAX_PLAUSIBLE_STEP_M
            if not implausible and not relayed and dt >= MIN_SPEED_DT_S:
                implausible = (step / dt) > MAX_PLAUSIBLE_SPEED_MS
            if implausible:
                self.suspect_count += 1
                return None, None      # rejected before touching any statistic

        self.gps_count += 1
        alt = det.get('alt')
        if alt is not None:
            self.max_alt = alt if self.max_alt is None else max(self.max_alt, alt)
            self.min_alt = alt if self.min_alt is None else min(self.min_alt, alt)

        if self.start_lat is None:
            self.start_lat, self.start_lon = lat, lon
            self.bbox_n = self.bbox_s = lat
            self.bbox_e = self.bbox_w = lon
        else:
            self.bbox_n = max(self.bbox_n, lat)
            self.bbox_s = min(self.bbox_s, lat)
            self.bbox_e = max(self.bbox_e, lon)
            self.bbox_w = min(self.bbox_w, lon)
        self.end_lat, self.end_lon = lat, lon

        speed = heading = None
        if step is not None:
            self.path_len_m += step
            if not relayed and dt is not None and dt >= MIN_SPEED_DT_S:
                speed = step / dt
                self.max_speed = speed if self.max_speed is None else max(self.max_speed, speed)
            if step > 0:
                heading = bearing_deg(self.prev_lat, self.prev_lon, lat, lon)
        self.prev_lat, self.prev_lon, self.prev_ts = lat, lon, ts

        self.points.append([lat, lon])
        if len(self.points) > MAX_LIVE_POINTS:
            self.points = simplify(self.points, SIMPLIFY_TOLERANCE)
        return speed, heading

    def stored_path(self):
        """Decimated path cached on the flight row so the map never has to read
        the detections table to draw a track."""
        pts = simplify(self.points, SIMPLIFY_TOLERANCE) if len(self.points) > 2 else list(self.points)
        if len(pts) > MAX_STORED_POINTS:
            pts = decimate(pts, MAX_STORED_POINTS)
        return json.dumps([[round(p[0], 6), round(p[1], 6)] for p in pts])

    def duration_s(self):
        return max(0.0, self.last_ts - self.started_at)


_FLIGHT_UPDATE_SQL = """
UPDATE flights SET ended_at=?, det_count=?, gps_count=?, suspect_count=?, path_len_m=?,
  max_alt_m=?, min_alt_m=?, max_speed_ms=?, start_lat=?, start_lon=?,
  end_lat=?, end_lon=?, bbox_n=?, bbox_s=?, bbox_e=?, bbox_w=?,
  pilot_lat=?, pilot_lon=?, max_rssi=?, min_rssi=?, mac=?, node_id=?
WHERE id=?"""


class Sessionizer:
    """Owns open flights and the batched writer.

    on_event(name, payload) is an optional callback used to publish deltas
    (flight:open / flight:point / flight:close). Kept as a plain callable so
    this module has no dependency on Flask or Socket.IO.
    """

    def __init__(self, db, identity, gap_s=DEFAULT_GAP_S, on_event=None, live=True,
                 resume_s=DEFAULT_RESUME_S):
        self.db = db
        self.identity = identity
        self.gap_s = gap_s
        self.resume_s = resume_s or 0.0
        self._recent = {}               # drone_id -> FlightState closed by the gap, resumable
        self.on_event = on_event or (lambda name, payload: None)
        self.open_flights = {}          # drone_id -> FlightState
        self._by_flight = {}            # flight_id -> FlightState
        self._pending = []              # detection rows awaiting insert
        self._dirty = set()             # flight_ids whose row needs updating
        self._lock = threading.RLock()
        self._last_flush = time.time()
        self._stop = threading.Event()
        self._janitor = None
        if live:
            self.recover()
            self.start_janitor()

    # -- startup --------------------------------------------------------------
    def recover(self):
        """Close flights left open by a previous process.

        A restart is a real discontinuity - we did not observe the aircraft in
        between - so these are closed at their last detection rather than
        resumed, and marked so the record stays honest.
        """
        rows = self.db.query("SELECT id, started_at FROM flights WHERE ended_at IS NULL")
        for r in rows:
            last = self.db.one(
                "SELECT MAX(ts) AS t FROM detections WHERE flight_id=?", (r['id'],))
            ended = (last['t'] if last and last['t'] is not None else r['started_at'])
            self.db.execute(
                "UPDATE flights SET ended_at=?, close_reason='restart' WHERE id=?",
                (ended, r['id']))
        return len(rows)

    def start_janitor(self):
        if self._janitor is not None:
            return
        self._janitor = threading.Thread(target=self._janitor_loop, daemon=True,
                                         name='flightlog-janitor')
        self._janitor.start()

    def _janitor_loop(self):
        while not self._stop.wait(JANITOR_INTERVAL_S):
            try:
                self.close_idle()
                self.flush()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._janitor is not None:
            self._janitor.join(timeout=2.0)
            self._janitor = None
        self.close_all('shutdown')
        self.flush()

    # -- ingest ---------------------------------------------------------------
    def ingest(self, det, ts=None):
        """Fold one normalized detection (see parse.normalize) into a flight."""
        if not det or not det.get('mac'):
            return None
        ts = ts if ts is not None else time.time()

        with self._lock:
            drone_id = self.identity.resolve(det['mac'], det.get('basic_id'), ts)
            # Before any flight opens, so the FAA skip and the alert's
            # "home point" label already know how this drone is heard.
            self.identity.mark_type(drone_id, det.get('id_type'))
            st = self.open_flights.get(drone_id)

            if st is not None and (ts - st.last_ts) > self.gap_s:
                if not self._can_resume(st, det, ts):
                    self._close(st, 'gap')
                    st = None
                # otherwise an RF dropout from the same operator position: continue
            if st is None:
                prev = self._recent.pop(drone_id, None)
                if prev is not None and self._can_resume(prev, det, ts):
                    st = self._reopen(prev, ts)
                if st is None:
                    st = self._open(drone_id, det, ts)

            key = (det.get('lat'), det.get('lon'), det.get('alt'), det.get('rssi'))
            if st.last_key == key and (ts - st.last_key_ts) < DUP_WINDOW_S:
                return st.flight_id         # identical re-emit; see DUP_WINDOW_S
            st.last_key, st.last_key_ts = key, ts

            speed, heading = st.add(det, ts)
            self._pending.append((
                st.flight_id, ts, det.get('lat'), det.get('lon'), det.get('alt'),
                det.get('pilot_lat'), det.get('pilot_lon'), det.get('rssi'),
                det.get('band'), det.get('channel'),
                det.get('source') or det.get('node_id'), speed, heading))
            self._dirty.add(st.flight_id)

            if det.get('node_id') and not st.node_id:
                st.node_id = det['node_id']
            if det.get('mac') and det['mac'] != st.mac:
                st.mac = det['mac']       # MAC rotated mid-flight; flight continues

            self.on_event('flight:point', {
                'flight_id': st.flight_id, 'drone_id': drone_id,
                'lat': det.get('lat'), 'lon': det.get('lon'), 'alt': det.get('alt'),
                'rssi': det.get('rssi'), 'ts': ts,
                'speed_ms': speed, 'heading_deg': heading})

            if len(self._pending) >= BATCH_ROWS or (time.time() - self._last_flush) > FLUSH_INTERVAL_S:
                self._flush_locked()
            return st.flight_id

    def _open(self, drone_id, det, ts):
        cur = self.db.execute(
            "INSERT INTO flights(drone_id, mac, node_id, started_at) VALUES(?,?,?,?)",
            (drone_id, det.get('mac'), det.get('node_id'), ts))
        st = FlightState(cur.lastrowid, drone_id, det.get('mac'), det.get('node_id'), ts)
        self.open_flights[drone_id] = st
        self._by_flight[st.flight_id] = st
        self.on_event('flight:open', {
            'flight_id': st.flight_id, 'drone_id': drone_id,
            'mac': det.get('mac'), 'started_at': ts,
            # how it was first heard - the alert on takeoff reports it
            'band': det.get('band'), 'channel': det.get('channel')})
        return st

    def _can_resume(self, st, det, ts):
        """True when a stale flight should continue rather than end.

        Needs an operator position on both sides that has not moved, and a
        return inside the resume window. See DEFAULT_RESUME_S.
        """
        if not self.resume_s or st.pilot_lat is None:
            return False
        plat, plon = det.get('pilot_lat'), det.get('pilot_lon')
        if plat is None or plon is None or (ts - st.last_ts) > self.resume_s:
            return False
        return haversine_m(st.pilot_lat, st.pilot_lon, plat, plon) <= PILOT_MATCH_M

    def _reopen(self, st, ts):
        """Resume a flight the janitor closed during an RF dropout.

        Returns None if the row changed after it closed - merged, deleted, or
        otherwise no longer a plain gap close - so an edit made in the UI is
        never silently undone by a late detection. The cached path is cleared
        so the map rebuilds the track from detection rows while it is open.
        """
        row = self.db.one("SELECT close_reason FROM flights WHERE id=?", (st.flight_id,))
        if row is None or row['close_reason'] != 'gap':
            return None
        self.db.execute("UPDATE flights SET ended_at=NULL, close_reason=NULL,"
                        " simplified_path=NULL WHERE id=?", (st.flight_id,))
        self.open_flights[st.drone_id] = st
        self._by_flight[st.flight_id] = st
        self.on_event('flight:open', {
            'flight_id': st.flight_id, 'drone_id': st.drone_id, 'mac': st.mac,
            'started_at': st.started_at, 'resumed': True, 'dropout_s': ts - st.last_ts})
        return st

    def _close(self, st, reason):
        self._flush_locked()
        with self.db.write_lock():
            self.db.conn.execute(
                _FLIGHT_UPDATE_SQL,
                (st.last_ts, st.det_count, st.gps_count, st.suspect_count, st.path_len_m,
                 st.max_alt, st.min_alt, st.max_speed, st.start_lat, st.start_lon,
                 st.end_lat, st.end_lon, st.bbox_n, st.bbox_s, st.bbox_e, st.bbox_w,
                 st.pilot_lat, st.pilot_lon, st.max_rssi, st.min_rssi,
                 st.mac, st.node_id, st.flight_id))
            self.db.conn.execute(
                "UPDATE flights SET close_reason=?, simplified_path=? WHERE id=?",
                (reason, st.stored_path(), st.flight_id))
        self.open_flights.pop(st.drone_id, None)
        self._by_flight.pop(st.flight_id, None)
        self._dirty.discard(st.flight_id)
        if reason == 'gap' and self.resume_s and st.pilot_lat is not None:
            self._recent[st.drone_id] = st
        else:
            self._recent.pop(st.drone_id, None)
        self.on_event('flight:close', {
            'flight_id': st.flight_id, 'drone_id': st.drone_id,
            'ended_at': st.last_ts, 'close_reason': reason,
            'det_count': st.det_count, 'gps_count': st.gps_count,
            'path_len_m': st.path_len_m, 'duration_s': st.duration_s()})

    def close_idle(self, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            for st in list(self.open_flights.values()):
                if (now - st.last_ts) > self.gap_s:
                    self._close(st, 'gap')
            for did, st in list(self._recent.items()):
                if (now - st.last_ts) > self.resume_s:
                    del self._recent[did]

    def close_all(self, reason='shutdown'):
        with self._lock:
            for st in list(self.open_flights.values()):
                self._close(st, reason)

    # -- writer ---------------------------------------------------------------
    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._pending and not self._dirty:
            return
        with self.db.write_lock():
            if self._pending:
                self.db.conn.executemany(
                    "INSERT OR IGNORE INTO detections"
                    "(flight_id, ts, lat, lon, alt, pilot_lat, pilot_lon, rssi, band, channel,"
                    " source, speed_ms, heading_deg)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", self._pending)
                self._pending.clear()
            for fid in self._dirty:
                st = self._by_flight.get(fid)
                if st is None:
                    continue
                self.db.conn.execute(
                    _FLIGHT_UPDATE_SQL,
                    (None, st.det_count, st.gps_count, st.suspect_count, st.path_len_m,
                     st.max_alt, st.min_alt, st.max_speed, st.start_lat, st.start_lon,
                     st.end_lat, st.end_lon, st.bbox_n, st.bbox_s, st.bbox_e, st.bbox_w,
                     st.pilot_lat, st.pilot_lon, st.max_rssi, st.min_rssi,
                     st.mac, st.node_id, fid))
            self._dirty.clear()
        self._last_flush = time.time()
