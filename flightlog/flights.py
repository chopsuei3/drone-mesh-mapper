"""Live flight sessionization.

The legacy app rebuilt "flights" from scratch on every KML regeneration by
rescanning the whole cumulative CSV (mesh-mapper.py:1603) and then threw the
grouping away. Here a flight is a persisted row that is opened, updated
incrementally and closed as detections stream in - nothing is ever rescanned.

Cost per detection is constant: one dict update, one appended tuple. Rows are
written by a batching flusher, so per-detection work does not grow with the
size of the session. That is the specific defect that made the old UI degrade
as it ran.

Several receivers. Detections carry `rx_node`, the receiver that heard them:
the home Pi's own XIAO, or a relay at another site feeding /api/ingest. Three
things follow from more than one receiver, handled below:

  * Two nodes hear the same broadcast. Same drone, same position and altitude,
    different RSSI - and the plain duplicate rule keys on RSSI, so both would
    be kept. A reading another node already delivered is a *reception*: it is
    counted in flight_nodes (who heard the flight, how well) and nowhere else.
  * Nodes deliver in batches, so two nodes' points arrive interleaved and
    slightly out of order. Drawn as they arrive, the path would zig-zag
    between the two streams. A fix older than the newest one applied is
    presence only.
  * A node that was offline delivers its backlog later. Those detections are
    placed at their real times, form past flights where nothing else was
    tracking the drone, and are receptions where a flight already covers them.
    How late a detection arrived decides whether it may alert or reach the
    live map (app.py); this module only passes that on.

Lateness is det['delay']: seconds from being heard to reaching the server,
set by the relay ingest (nodes.py). Local serial, HTTP posts and imports carry
none, and everything below treats them exactly as before nodes existed.
"""
import collections
import json
import logging
import threading
import time

from .geo import haversine_m, bearing_deg, simplify, decimate

logger = logging.getLogger(__name__)

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
# withheld rather than fabricated. (Detections relayed over IP by a remote node
# are not marked: the relay stamps each line with its age, so their timing is
# good.)
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
# were byte-identical). One matching the same node's previous detection in
# position, altitude and RSSI within this window carries no information and is
# dropped rather than inflating the point count.
DUP_WINDOW_S = 1.0

# The same position and altitude from a *different* node within this window is
# one broadcast heard twice. Wider than DUP_WINDOW_S because the two copies are
# stamped by different relays, each off by its own network delay.
CROSS_DUP_WINDOW_S = 2.0
# How far back (in detection time) those copies are looked for. A relay lagging
# further than this behind another delivers presence rows instead.
RECENT_KEYS_S = 60.0

# A fix older than the newest one already applied by more than this is
# presence only: it keeps the flight alive and counts as a reception, but adds
# nothing to the path, speed, bounding box or altitude extremes.
REORDER_TOLERANCE_S = 0.5

# A flight is closed when its newest detection is older than the gap - but a
# flight fed by a relay is not closed while detections are still arriving for
# it. A node's backlog arrives minutes late in batches, and closing the flight
# it is still building between two batches would split it.
FEED_IDLE_S = 10.0
# A relay-fed flight stays in memory this long after its last detection
# arrived, so a backlog interrupted mid-delivery (a relay backing off) picks it
# up again.
RECENT_KEEP_S = 300.0

# Coverage samples (node_samples): at most one per flight and node this often.
SAMPLE_EVERY_S = 2.0

_FLIGHT_NODES_UPSERT = """
INSERT INTO flight_nodes(flight_id, node_id, receptions, max_rssi, min_rssi, first_ts, last_ts)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(flight_id, node_id) DO UPDATE SET
  receptions = receptions + excluded.receptions,
  max_rssi = MAX(COALESCE(max_rssi, excluded.max_rssi), COALESCE(excluded.max_rssi, max_rssi)),
  min_rssi = MIN(COALESCE(min_rssi, excluded.min_rssi), COALESCE(excluded.min_rssi, min_rssi)),
  first_ts = MIN(COALESCE(first_ts, excluded.first_ts), COALESCE(excluded.first_ts, first_ts)),
  last_ts = MAX(COALESCE(last_ts, excluded.last_ts), COALESCE(excluded.last_ts, last_ts))"""


def _hi(a, b):
    return b if a is None else a if b is None else max(a, b)


def _lo(a, b):
    return b if a is None else a if b is None else min(a, b)


class FlightState:
    """In-memory rolling state for one flight being built."""
    __slots__ = ('flight_id', 'drone_id', 'mac', 'node_id', 'started_at', 'last_ts',
                 'det_count', 'gps_count', 'path_len_m', 'max_alt', 'min_alt',
                 'max_speed', 'start_lat', 'start_lon', 'end_lat', 'end_lon', 'suspect_count',
                 'bbox_n', 'bbox_s', 'bbox_e', 'bbox_w', 'pilot_lat', 'pilot_lon',
                 'max_rssi', 'min_rssi', 'points', 'prev_lat', 'prev_lon', 'prev_ts',
                 'max_ts', 'fed_at', 'delayed', 'past', 'last_keys', 'recent', 'recent_q',
                 'sample_ts')

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
        self.max_ts = None              # newest fix applied to the path
        self.fed_at = time.time()       # wall time a detection last arrived for it
        self.delayed = False            # fed by a relay (detections carry a delay)
        self.past = False               # built from a backlog, behind a live flight
        self.last_keys = {}             # rx_node -> (position+rssi, ts): same-node duplicates
        self.recent = {}                # (lat, lon, alt) -> (ts, rx_node): cross-node duplicates
        self.recent_q = collections.deque()
        self.sample_ts = {}             # rx_node -> ts of its last coverage sample

    def covers(self, ts, gap_s):
        """Whether a detection at `ts` falls within this flight's span, give or take a gap."""
        return self.started_at - gap_s <= ts <= self.last_ts + gap_s

    def copy_of(self, pos, ts, node):
        """True if another node already delivered this reading. See CROSS_DUP_WINDOW_S."""
        seen = self.recent.get(pos)
        return (seen is not None and seen[1] != node
                and abs(ts - seen[0]) <= CROSS_DUP_WINDOW_S)

    def remember(self, pos, ts, node):
        self.recent[pos] = (ts, node)
        q = self.recent_q
        q.append((ts, pos))
        horizon = (self.max_ts if self.max_ts is not None else ts) - RECENT_KEYS_S
        while q and q[0][0] < horizon:
            old_ts, old = q.popleft()
            seen = self.recent.get(old)
            if seen is not None and seen[0] == old_ts:
                del self.recent[old]

    def add(self, det, ts):
        """Fold one detection into the rolling stats. Returns (speed, heading, applied).

        `applied` is False for a fix that arrived out of order (presence only).
        Order matters here: the outlier gate runs before any bookkeeping, so a
        bad fix cannot leave a footprint in the bbox, the altitude extremes or
        the end position on its way to being rejected.
        """
        self.det_count += 1
        if ts > self.last_ts:
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
            return None, None, True

        if self.max_ts is not None and ts < self.max_ts - REORDER_TOLERANCE_S:
            return None, None, False   # arrived out of order; see REORDER_TOLERANCE_S

        # Detections relayed over LoRa arrive late by a variable amount, so any
        # speed derived from receive time is meaningless - and that also makes
        # the outlier gate unusable for them. Positions are still trustworthy.
        relayed = bool(det.get('node_id'))

        step = dt = None
        if self.prev_lat is not None:
            step = haversine_m(self.prev_lat, self.prev_lon, lat, lon)
            dt = max(0.0, ts - self.prev_ts)
            implausible = step > MAX_PLAUSIBLE_STEP_M
            if not implausible and not relayed and dt >= MIN_SPEED_DT_S:
                implausible = (step / dt) > MAX_PLAUSIBLE_SPEED_MS
            if implausible:
                self.suspect_count += 1
                return None, None, True  # rejected before touching any statistic

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
        if self.max_ts is None or ts > self.max_ts:
            self.max_ts = ts

        self.points.append([lat, lon])
        if len(self.points) > MAX_LIVE_POINTS:
            self.points = simplify(self.points, SIMPLIFY_TOLERANCE)
        return speed, heading, True

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


def _update_args(st, ended):
    return (ended, st.det_count, st.gps_count, st.suspect_count, st.path_len_m,
            st.max_alt, st.min_alt, st.max_speed, st.start_lat, st.start_lon,
            st.end_lat, st.end_lon, st.bbox_n, st.bbox_s, st.bbox_e, st.bbox_w,
            st.pilot_lat, st.pilot_lon, st.max_rssi, st.min_rssi,
            st.mac, st.node_id, st.flight_id)


class Sessionizer:
    """Owns open flights and the batched writer.

    on_event(name, payload) is an optional callback used to publish deltas
    (flight:open / flight:point / flight:close). Kept as a plain callable so
    this module has no dependency on Flask or Socket.IO. Every payload carries
    the detection time it describes (`started_at`, `ts`, `ended_at`), so the
    receiver can tell a backlog from live traffic; flights built behind a live
    one are marked `past`.
    """

    def __init__(self, db, identity, gap_s=DEFAULT_GAP_S, on_event=None, live=True,
                 resume_s=DEFAULT_RESUME_S):
        self.db = db
        self.identity = identity
        self.gap_s = gap_s
        self.resume_s = resume_s or 0.0
        self._recent = {}               # drone_id -> FlightState closed by the gap
        self._past = {}                 # drone_id -> FlightState built from a backlog
        self._cover = {}                # drone_id -> (flight_id, lo, hi) found covering a backlog
        self.on_event = on_event or (lambda name, payload: None)
        self.open_flights = {}          # drone_id -> FlightState
        self._by_flight = {}            # flight_id -> FlightState
        self._pending = []              # detection rows awaiting insert
        self._dirty = set()             # flight_ids whose row needs updating
        self._rx = {}                   # (flight_id, rx_node) -> [n, max_rssi, min_rssi, first, last]
        self._samples = []              # node_samples rows awaiting insert
        self._cover_sample_ts = {}      # (flight_id, rx_node) -> ts, for flights not in memory
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
            except Exception as e:
                logger.warning('janitor: %s', e)

    def stop(self):
        self._stop.set()
        if self._janitor is not None:
            self._janitor.join(timeout=2.0)
            self._janitor = None
        self.close_all('shutdown')
        self.flush()

    def batch(self):
        """Hold the lock across a run of ingest() calls - one node's batch - so
        the janitor cannot close a flight between two lines of it."""
        return self._lock

    # -- ingest ---------------------------------------------------------------
    def ingest(self, det, ts=None):
        """Fold one normalized detection (see parse.normalize) into a flight.

        `ts` is when the drone was heard (default: now). det['rx_node'] names
        the receiver, if any. Returns the flight id it was counted against.
        """
        if not det or not det.get('mac'):
            return None
        now = time.time()
        ts = ts if ts is not None else now

        with self._lock:
            drone_id = self.identity.resolve(det['mac'], det.get('basic_id'), ts)
            # Before any flight opens, so the FAA skip and the alert's
            # "home point" label already know how this drone is heard.
            self.identity.mark_type(drone_id, det.get('id_type'))
            st = self._route(drone_id, det, ts)
            if not isinstance(st, FlightState):
                self._reception(st, det, ts)
                return st                   # receptions only, for a flight already closed
            fid = self._apply(st, drone_id, det, ts, now)
            if len(self._pending) >= BATCH_ROWS or (time.time() - self._last_flush) > FLUSH_INTERVAL_S:
                self._flush_locked()
            return fid

    def _late(self, det):
        """Delivered too late to be live: part of a relay's backlog."""
        return (det.get('delay') or 0.0) > self.gap_s

    def _route(self, drone_id, det, ts):
        """The flight a detection belongs to: a FlightState, or the id of a
        closed flight that already covers its time (receptions only)."""
        late = self._late(det)
        st = self.open_flights.get(drone_id)
        if st is not None and (ts >= st.started_at - self.gap_s or not late):
            if (ts - st.last_ts) <= self.gap_s or self._can_resume(st, det, ts):
                return st                   # continues (a dropout, if over the gap)
            self._close(st, 'gap')
            st = None

        # No open flight - or a backlog detection from before the open one began.
        lane = self._past.get(drone_id)
        if lane is not None and lane.covers(ts, self.gap_s):
            return lane
        if st is None:
            prev = self._recent.pop(drone_id, None)
            # Closed by the janitor while a relay's detections were still on
            # their way: they continue it, gap or not. (Unrelayed detections
            # arrive as they are heard, so for them only the dropout rule applies.)
            relayed = prev is not None and (prev.delayed or det.get('delay') is not None)
            if prev is not None and ((relayed and prev.covers(ts, self.gap_s))
                                     or self._can_resume(prev, det, ts)):
                reopened = self._reopen(prev, ts)
                if reopened is not None:
                    return reopened
            if not late:
                return self._open(drone_id, det, ts)
        # Too late to be live: another node may already have recorded this flight.
        fid = self._covering(drone_id, ts)
        if fid is not None:
            return fid
        if st is None:
            return self._open(drone_id, det, ts)
        return self._open_past(drone_id, det, ts)

    def _apply(self, st, drone_id, det, ts, now):
        node = det.get('rx_node')
        st.fed_at = now
        if det.get('delay') is not None:
            st.delayed = True
        pos = (det.get('lat'), det.get('lon'), det.get('alt'))

        # The same node's identical re-emit; see DUP_WINDOW_S.
        key = pos + (det.get('rssi'),)
        lk = st.last_keys.get(node)
        if lk is not None and lk[0] == key and abs(ts - lk[1]) < DUP_WINDOW_S:
            return st.flight_id
        st.last_keys[node] = (key, ts)

        self._reception(st.flight_id, det, ts, st)
        if st.copy_of(pos, ts, node):
            if ts > st.last_ts:
                st.last_ts = ts             # heard, so still alive - and nothing more
            return st.flight_id
        st.remember(pos, ts, node)

        speed, heading, applied = st.add(det, ts)
        self._pending.append((
            st.flight_id, ts, det.get('lat'), det.get('lon'), det.get('alt'),
            det.get('pilot_lat'), det.get('pilot_lon'), det.get('rssi'),
            det.get('band'), det.get('channel'),
            det.get('source') or det.get('node_id'), speed, heading, node))
        self._dirty.add(st.flight_id)

        if det.get('node_id') and not st.node_id:
            st.node_id = det['node_id']
        if det.get('mac') and det['mac'] != st.mac:
            st.mac = det['mac']           # MAC rotated mid-flight; flight continues

        if applied:
            self.on_event('flight:point', {
                'flight_id': st.flight_id, 'drone_id': drone_id,
                'lat': det.get('lat'), 'lon': det.get('lon'), 'alt': det.get('alt'),
                'rssi': det.get('rssi'), 'ts': ts, 'rx_node': node,
                'delay': det.get('delay'), 'past': st.past,
                'speed_ms': speed, 'heading_deg': heading})
        return st.flight_id

    def _reception(self, fid, det, ts, st=None):
        """Count one reception by det['rx_node'] towards flight_nodes, and keep
        a coverage sample now and then. `st` is the flight's state when it is
        in memory."""
        node = det.get('rx_node')
        if node is None:
            return
        rssi = det.get('rssi')
        r = self._rx.get((fid, node))
        if r is None:
            self._rx[(fid, node)] = [1, rssi, rssi, ts, ts]
        else:
            r[0] += 1
            r[1], r[2] = _hi(r[1], rssi), _lo(r[2], rssi)
            r[3], r[4] = min(r[3], ts), max(r[4], ts)
        lat, lon = det.get('lat'), det.get('lon')
        if lat is None or lon is None:
            return
        seen, k = (st.sample_ts, node) if st is not None else (self._cover_sample_ts, (fid, node))
        last = seen.get(k)
        if last is None or abs(ts - last) >= SAMPLE_EVERY_S:
            seen[k] = ts
            self._samples.append((node, fid, ts, lat, lon, det.get('alt'), rssi))

    def _covering(self, drone_id, ts):
        """A closed flight of this drone whose span (plus a gap) covers `ts`."""
        c = self._cover.get(drone_id)
        if c is not None and c[1] <= ts <= c[2]:
            return c[0]
        r = self.db.one(
            "SELECT id, started_at, COALESCE(ended_at, started_at) AS ended FROM flights"
            " WHERE drone_id=? AND started_at <= ? AND COALESCE(ended_at, started_at) >= ?"
            " ORDER BY started_at DESC LIMIT 1", (drone_id, ts + self.gap_s, ts - self.gap_s))
        if r is None or r['id'] in self._by_flight:
            return None
        self._cover[drone_id] = (r['id'], r['started_at'] - self.gap_s, r['ended'] + self.gap_s)
        return r['id']

    def _open(self, drone_id, det, ts):
        cur = self.db.execute(
            "INSERT INTO flights(drone_id, mac, node_id, started_at) VALUES(?,?,?,?)",
            (drone_id, det.get('mac'), det.get('node_id'), ts))
        st = FlightState(cur.lastrowid, drone_id, det.get('mac'), det.get('node_id'), ts)
        self.open_flights[drone_id] = st
        self._by_flight[st.flight_id] = st
        self._cover.pop(drone_id, None)
        self.on_event('flight:open', {
            'flight_id': st.flight_id, 'drone_id': drone_id,
            'mac': det.get('mac'), 'started_at': ts, 'rx_node': det.get('rx_node'),
            'delay': det.get('delay'),
            # how it was first heard - the alert on takeoff reports it
            'band': det.get('band'), 'channel': det.get('channel')})
        return st

    def _open_past(self, drone_id, det, ts):
        """A backlog flight from before the drone's current one. Stored closed
        from the start (ended_at kept current as it grows), never live."""
        cur = self.db.execute(
            "INSERT INTO flights(drone_id, mac, node_id, started_at, ended_at, close_reason)"
            " VALUES(?,?,?,?,?,'backlog')",
            (drone_id, det.get('mac'), det.get('node_id'), ts, ts))
        st = FlightState(cur.lastrowid, drone_id, det.get('mac'), det.get('node_id'), ts)
        st.past = True
        old = self._past.pop(drone_id, None)
        if old is not None:
            self._retire(old)
        self._past[drone_id] = st
        self._by_flight[st.flight_id] = st
        self.on_event('flight:open', {
            'flight_id': st.flight_id, 'drone_id': drone_id, 'mac': det.get('mac'),
            'started_at': ts, 'rx_node': det.get('rx_node'), 'past': True,
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
        """Resume a flight the janitor closed - an RF dropout, or a backlog
        whose delivery paused.

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
        self._cover.pop(st.drone_id, None)
        self.on_event('flight:open', {
            'flight_id': st.flight_id, 'drone_id': st.drone_id, 'mac': st.mac,
            'started_at': st.started_at, 'resumed': True, 'dropout_s': ts - st.last_ts})
        return st

    def _close(self, st, reason):
        self._flush_locked()
        with self.db.write_lock():
            self.db.conn.execute(_FLIGHT_UPDATE_SQL, _update_args(st, st.last_ts))
            self.db.conn.execute(
                "UPDATE flights SET close_reason=?, simplified_path=? WHERE id=?",
                (reason, st.stored_path(), st.flight_id))
        self.open_flights.pop(st.drone_id, None)
        self._by_flight.pop(st.flight_id, None)
        self._dirty.discard(st.flight_id)
        if reason == 'gap':
            self._recent[st.drone_id] = st
        else:
            self._recent.pop(st.drone_id, None)
        self.on_event('flight:close', {
            'flight_id': st.flight_id, 'drone_id': st.drone_id,
            'ended_at': st.last_ts, 'close_reason': reason,
            'det_count': st.det_count, 'gps_count': st.gps_count,
            'path_len_m': st.path_len_m, 'duration_s': st.duration_s()})

    def _retire(self, st):
        """Finish a past (backlog) flight: final stats and its cached path."""
        self._flush_locked()
        with self.db.write_lock():
            self.db.conn.execute(_FLIGHT_UPDATE_SQL, _update_args(st, st.last_ts))
            self.db.conn.execute("UPDATE flights SET simplified_path=? WHERE id=?",
                                 (st.stored_path(), st.flight_id))
        if self._past.get(st.drone_id) is st:
            del self._past[st.drone_id]
        self._by_flight.pop(st.flight_id, None)
        self._dirty.discard(st.flight_id)
        self.on_event('flight:close', {
            'flight_id': st.flight_id, 'drone_id': st.drone_id,
            'ended_at': st.last_ts, 'close_reason': 'backlog', 'past': True,
            'det_count': st.det_count, 'gps_count': st.gps_count,
            'path_len_m': st.path_len_m, 'duration_s': st.duration_s()})

    def close_idle(self, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            for st in list(self.open_flights.values()):
                if (now - st.last_ts) > self.gap_s and not (
                        st.delayed and (now - st.fed_at) <= FEED_IDLE_S):
                    self._close(st, 'gap')
            for did, st in list(self._recent.items()):
                if (now - st.last_ts) > self.resume_s and not (
                        st.delayed and (now - st.fed_at) <= RECENT_KEEP_S):
                    del self._recent[did]
            for st in list(self._past.values()):
                if (now - st.fed_at) > RECENT_KEEP_S:
                    self._retire(st)
            self._cover.clear()             # a merge or delete may have changed them

    def close_all(self, reason='shutdown'):
        with self._lock:
            for st in list(self.open_flights.values()):
                self._close(st, reason)
            for st in list(self._past.values()):
                self._retire(st)

    # -- writer ---------------------------------------------------------------
    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not (self._pending or self._dirty or self._rx or self._samples):
            return
        with self.db.write_lock():
            if self._pending:
                self.db.conn.executemany(
                    "INSERT OR IGNORE INTO detections"
                    "(flight_id, ts, lat, lon, alt, pilot_lat, pilot_lon, rssi, band, channel,"
                    " source, speed_ms, heading_deg, rx_node)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self._pending)
                self._pending.clear()
            for fid in self._dirty:
                st = self._by_flight.get(fid)
                if st is None:
                    continue
                self.db.conn.execute(_FLIGHT_UPDATE_SQL,
                                     _update_args(st, st.last_ts if st.past else None))
            self._dirty.clear()
            # Who-heard-it bookkeeping must never cost the detections above, so
            # a failure here is logged and that batch of it dropped.
            rx, self._rx = self._rx, {}
            samples, self._samples = self._samples, []
            try:
                if rx:
                    self.db.conn.executemany(_FLIGHT_NODES_UPSERT, [
                        (fid, node, r[0], r[1], r[2], r[3], r[4])
                        for (fid, node), r in rx.items()])
                if samples:
                    self.db.conn.executemany(
                        "INSERT INTO node_samples(node_id, flight_id, ts, lat, lon, alt, rssi)"
                        " VALUES(?,?,?,?,?,?,?)", samples)
            except Exception as e:
                logger.warning('flight_nodes write failed: %s', e)
        self._last_flush = time.time()
