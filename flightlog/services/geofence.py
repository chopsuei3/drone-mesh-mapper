"""Geofencing: polygon / circle fences with enter-exit alerts and webhooks.

The geometry and validation are lifted from mesh-mapper.py (:955-1222). Three
things changed, all consequences of the new data model:

  * State is keyed by **drone_id**, not MAC. The legacy `GEOFENCE_STATE[fid][mac]`
    re-fired an "enter" alert every time a drone rotated its MAC inside a fence,
    because the new MAC had no prior state.
  * Alerts are persisted to `geofence_events` with the flight id, instead of
    living in a 200-entry in-memory ring. That is what makes "which flights
    came near this fence" a join rather than a scan.
  * The drone's label and tag come from the drone record rather than from
    per-MAC alias/tag dicts.
"""
import json
import logging
import threading
import time

from ..geo import haversine_m

logger = logging.getLogger(__name__)

MAX_EVENTS_RETURNED = 500


def point_in_polygon(lat, lon, ring):
    """Ray casting. `ring` is a list of [lat, lon]; not closed."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        ai_lat, ai_lon = ring[i][0], ring[i][1]
        aj_lat, aj_lon = ring[j][0], ring[j][1]
        if (ai_lon > lon) != (aj_lon > lon):
            slope = ((aj_lat - ai_lat) / (aj_lon - ai_lon)) if (aj_lon != ai_lon) else float('inf')
            x_int = ai_lat + slope * (lon - ai_lon)
            if lat < x_int:
                inside = not inside
        j = i
    return inside


def point_in_fence(lat, lon, fence):
    geom = fence.get('geometry') or {}
    if fence.get('type') == 'circle':
        c = geom.get('center') or [0, 0]
        r = float(geom.get('radius_m') or 0)
        return r > 0 and haversine_m(lat, lon, c[0], c[1]) <= r
    if fence.get('type') == 'polygon':
        return point_in_polygon(lat, lon, geom.get('points') or [])
    return False


def validate(data):
    """Validate and canonicalize a fence. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError('fence must be an object')
    name = (data.get('name') or '').strip()
    if not name or len(name) > 80:
        raise ValueError('name is required (<=80 chars)')
    ftype = data.get('type')
    if ftype not in ('polygon', 'circle'):
        raise ValueError("type must be 'polygon' or 'circle'")

    geom = data.get('geometry') or {}
    if ftype == 'polygon':
        pts = geom.get('points') or []
        if not isinstance(pts, list) or len(pts) < 3:
            raise ValueError('polygon needs >=3 points')
        ring = []
        for p in pts:
            if not (isinstance(p, list) and len(p) == 2):
                raise ValueError('polygon points must be [lat, lon] pairs')
            try:
                lat, lon = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                raise ValueError('polygon point coords must be numeric')
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('polygon point out of range')
            ring.append([lat, lon])
        geom = {'points': ring}
    else:
        c, r = geom.get('center'), geom.get('radius_m')
        if not (isinstance(c, list) and len(c) == 2):
            raise ValueError('circle needs center [lat, lon]')
        try:
            clat, clon, rad = float(c[0]), float(c[1]), float(r or 0)
        except (TypeError, ValueError):
            raise ValueError('circle coords/radius must be numeric')
        if not (-90 <= clat <= 90 and -180 <= clon <= 180):
            raise ValueError('circle center out of range')
        if not (0 < rad <= 1000000):
            raise ValueError('circle radius_m must be 0 < r <= 1,000,000')
        geom = {'center': [clat, clon], 'radius_m': rad}

    tags = data.get('alert_tags') or []
    if tags and not isinstance(tags, list):
        raise ValueError('alert_tags must be a list')
    tags = [str(t).lower().strip() for t in tags if t]

    color = (data.get('color') or '#ff3333').strip()
    if len(color) > 16:
        raise ValueError('color too long')
    hook = (data.get('webhook_url') or '').strip()
    if hook and not hook.startswith(('http://', 'https://')):
        raise ValueError('webhook_url must start with http:// or https://')

    return {
        'name': name, 'type': ftype, 'geometry': geom,
        'alert_on_enter': bool(data.get('alert_on_enter', True)),
        'alert_on_exit': bool(data.get('alert_on_exit', True)),
        'alert_tags': tags, 'color': color, 'webhook_url': hook,
        'enabled': bool(data.get('enabled', True)),
    }


class GeofenceEngine:
    def __init__(self, db, on_alert=None, webhook_url=None):
        self.db = db
        self.on_alert = on_alert or (lambda payload: None)
        self.webhook_url = webhook_url
        self._lock = threading.Lock()
        self._state = {}          # fence_id -> {drone_id: inside_bool}
        self._ensure_schema()

    def _ensure_schema(self):
        self.db.execute("""CREATE TABLE IF NOT EXISTS geofences (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
            geometry TEXT NOT NULL, color TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            alert_on_enter INTEGER NOT NULL DEFAULT 1,
            alert_on_exit INTEGER NOT NULL DEFAULT 1,
            alert_tags TEXT, webhook_url TEXT, created_at REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS geofence_events (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            fence_id INTEGER NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
            drone_id INTEGER, flight_id INTEGER, transition TEXT NOT NULL,
            lat REAL, lon REAL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_gfe_fence ON geofence_events(fence_id, ts DESC)")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_gfe_flight ON geofence_events(flight_id)")

    # -- CRUD -----------------------------------------------------------------
    def list(self):
        out = []
        for r in self.db.query("SELECT * FROM geofences ORDER BY name"):
            d = dict(r)
            d['geometry'] = json.loads(d['geometry'])
            d['alert_tags'] = json.loads(d['alert_tags'] or '[]')
            d['enabled'] = bool(d['enabled'])
            out.append(d)
        return out

    def create(self, data):
        f = validate(data)
        cur = self.db.execute(
            "INSERT INTO geofences(name,type,geometry,color,enabled,alert_on_enter,"
            "alert_on_exit,alert_tags,webhook_url,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f['name'], f['type'], json.dumps(f['geometry']), f['color'],
             int(f['enabled']), int(f['alert_on_enter']), int(f['alert_on_exit']),
             json.dumps(f['alert_tags']), f['webhook_url'], time.time()))
        return self.get(cur.lastrowid)

    def get(self, fid):
        r = self.db.one("SELECT * FROM geofences WHERE id=?", (fid,))
        if r is None:
            return None
        d = dict(r)
        d['geometry'] = json.loads(d['geometry'])
        d['alert_tags'] = json.loads(d['alert_tags'] or '[]')
        d['enabled'] = bool(d['enabled'])
        return d

    def update(self, fid, data):
        cur = self.get(fid)
        if cur is None:
            return None
        merged = dict(cur)
        merged.update(data or {})
        f = validate(merged)
        self.db.execute(
            "UPDATE geofences SET name=?,type=?,geometry=?,color=?,enabled=?,"
            "alert_on_enter=?,alert_on_exit=?,alert_tags=?,webhook_url=? WHERE id=?",
            (f['name'], f['type'], json.dumps(f['geometry']), f['color'],
             int(f['enabled']), int(f['alert_on_enter']), int(f['alert_on_exit']),
             json.dumps(f['alert_tags']), f['webhook_url'], fid))
        with self._lock:
            self._state.pop(fid, None)      # geometry may have moved
        return self.get(fid)

    def delete(self, fid):
        self.db.execute("DELETE FROM geofences WHERE id=?", (fid,))
        with self._lock:
            self._state.pop(fid, None)

    # -- evaluation -----------------------------------------------------------
    def check(self, drone_id, lat, lon, flight_id=None, tag=None, label=None):
        """Run one position through every enabled fence, emitting transitions."""
        if drone_id is None or lat is None or lon is None:
            return []
        fired = []
        for fence in self.list():
            if not fence['enabled']:
                continue
            tags = fence['alert_tags']
            if tags and (tag or 'unknown') not in tags:
                continue
            inside = point_in_fence(lat, lon, fence)
            with self._lock:
                st = self._state.setdefault(fence['id'], {})
                prev = st.get(drone_id)
                st[drone_id] = inside
            if prev is None:
                continue                    # first observation, no transition
            if inside and not prev and fence['alert_on_enter']:
                fired.append(self._emit(fence, drone_id, flight_id, 'enter', lat, lon, label))
            elif prev and not inside and fence['alert_on_exit']:
                fired.append(self._emit(fence, drone_id, flight_id, 'exit', lat, lon, label))
        return fired

    def _emit(self, fence, drone_id, flight_id, transition, lat, lon, label):
        ts = time.time()
        self.db.execute(
            "INSERT INTO geofence_events(ts,fence_id,drone_id,flight_id,transition,lat,lon)"
            " VALUES(?,?,?,?,?,?,?)", (ts, fence['id'], drone_id, flight_id, transition, lat, lon))
        payload = {'ts': ts, 'fence_id': fence['id'], 'fence': fence['name'],
                   'drone_id': drone_id, 'flight_id': flight_id, 'label': label,
                   'transition': transition, 'lat': lat, 'lon': lon}
        try:
            self.on_alert(payload)
        except Exception:
            pass
        url = fence.get('webhook_url') or self.webhook_url
        if url:
            self._post(url, payload)
        return payload

    def _post(self, url, payload):
        """Best-effort webhook. Never blocks ingest and never raises."""
        def send():
            try:
                import requests
                requests.post(url, json=payload, timeout=5)
            except Exception as e:
                logger.debug('webhook failed: %s', e)
        threading.Thread(target=send, daemon=True).start()

    def events(self, limit=100, fence_id=None):
        limit = max(1, min(int(limit), MAX_EVENTS_RETURNED))
        sql = ("SELECT e.*, f.name AS fence_name, f.color,"
               " COALESCE(d.label, d.basic_id) AS drone_label"
               " FROM geofence_events e JOIN geofences f ON f.id=e.fence_id"
               " LEFT JOIN drones d ON d.id=e.drone_id")
        args = []
        if fence_id:
            sql += " WHERE e.fence_id=?"
            args.append(int(fence_id))
        sql += " ORDER BY e.ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.query(sql, args)]
