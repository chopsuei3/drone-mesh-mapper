"""Read queries for the flight table and the drill-down views.

Every list query here is bounded and parameterized. The flight table is a single
indexed read over `flights` joined to `drones`/`groups` - it never touches the
`detections` table, which is what keeps it fast as history grows. Paths come
from the cached `simplified_path` blob on the flight row, on a separate call.
"""
import json

from .colors import display_color

# Columns the table may sort by, mapped to real SQL. Whitelisted so a query
# parameter can never reach the SQL text.
SORTS = {
    'started_at': 'f.started_at',
    'ended_at': 'f.ended_at',
    'duration': '(COALESCE(f.ended_at, f.started_at) - f.started_at)',
    'distance': 'f.path_len_m',
    'max_alt': 'f.max_alt_m',
    'max_speed': 'f.max_speed_ms',
    'points': 'f.det_count',
    'rssi': 'f.max_rssi',
    'drone': 'COALESCE(d.label, d.basic_id, f.mac)',
    'group': 'g.name',
}

FLIGHT_COLUMNS = """
  f.id, f.drone_id, f.mac, f.node_id, f.started_at, f.ended_at, f.close_reason,
  f.det_count, f.gps_count, f.suspect_count, f.path_len_m, f.max_alt_m, f.min_alt_m,
  f.max_speed_ms, f.start_lat, f.start_lon, f.end_lat, f.end_lon,
  f.bbox_n, f.bbox_s, f.bbox_e, f.bbox_w, f.pilot_lat, f.pilot_lon,
  f.max_rssi, f.min_rssi, f.label,
  d.basic_id AS drone_basic_id, d.label AS drone_label, d.tag AS drone_tag,
  d.color AS drone_color, d.faa_make, d.faa_model, d.id_type AS drone_id_type,
  g.id AS group_id, g.name AS group_name, g.color AS group_color
"""

FROM_JOIN = """
  FROM flights f
  JOIN drones d ON d.id = f.drone_id
  LEFT JOIN groups g ON g.id = d.group_id
"""


def _filters(p):
    """Build the shared WHERE clause from a params dict. Returns (sql, args)."""
    where, args = [], []

    if p.get('from') is not None:
        where.append("f.started_at >= ?"); args.append(float(p['from']))
    if p.get('to') is not None:
        where.append("f.started_at <= ?"); args.append(float(p['to']))
    if p.get('drone_id'):
        where.append("f.drone_id = ?"); args.append(int(p['drone_id']))
    if p.get('group_id'):
        where.append("d.group_id = ?"); args.append(int(p['group_id']))
    if p.get('tag'):
        where.append("d.tag = ?"); args.append(p['tag'])
    if p.get('rx_node'):
        # Heard by this receiver - copies of another node's readings included,
        # which only flight_nodes records.
        where.append("f.id IN (SELECT flight_id FROM flight_nodes WHERE node_id = ?)")
        args.append(int(p['rx_node']))
    if p.get('status') == 'open':
        where.append("f.ended_at IS NULL")
    elif p.get('status') == 'closed':
        where.append("f.ended_at IS NOT NULL")
    if p.get('has_track') == '1':
        where.append("f.gps_count > 0")
    elif p.get('has_track') == '0':
        where.append("f.gps_count = 0")
    if p.get('min_distance'):
        where.append("f.path_len_m >= ?"); args.append(float(p['min_distance']))
    if p.get('min_duration'):
        where.append("(COALESCE(f.ended_at, f.started_at) - f.started_at) >= ?")
        args.append(float(p['min_duration']))

    bbox = p.get('bbox')
    if bbox:
        # bbox = w,s,e,n. Overlap test against the flight's own bounding box.
        try:
            w, s, e, n = [float(x) for x in str(bbox).split(',')]
            where.append("f.bbox_s IS NOT NULL AND f.bbox_s <= ? AND f.bbox_n >= ?"
                         " AND f.bbox_w <= ? AND f.bbox_e >= ?")
            args += [n, s, e, w]
        except (ValueError, TypeError):
            pass

    q = (p.get('q') or '').strip()
    if q:
        like = '%' + q.lower() + '%'
        where.append("(LOWER(COALESCE(d.label,'')) LIKE ?"
                     " OR LOWER(COALESCE(d.basic_id,'')) LIKE ?"
                     " OR LOWER(COALESCE(f.mac,'')) LIKE ?"
                     " OR LOWER(COALESCE(g.name,'')) LIKE ?"
                     " OR LOWER(COALESCE(d.faa_make,'') || ' ' || COALESCE(d.faa_model,'')) LIKE ?"
                     " OR f.drone_id IN (SELECT drone_id FROM drone_macs WHERE mac LIKE ?))")
        args += [like, like, like, like, like, like]

    return (' WHERE ' + ' AND '.join(where) if where else ''), args


def list_flights(db, p):
    """Paged, sorted flight summaries. Never includes path data."""
    where, args = _filters(p)
    sort = SORTS.get(p.get('sort') or 'started_at', SORTS['started_at'])
    order = 'ASC' if (p.get('order') or 'desc').lower() == 'asc' else 'DESC'
    try:
        limit = max(1, min(int(p.get('limit') or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(p.get('offset') or 0))
    except (TypeError, ValueError):
        offset = 0

    total = db.one("SELECT COUNT(*) AS n" + FROM_JOIN + where, args)['n']
    rows = db.query(
        "SELECT" + FLIGHT_COLUMNS + FROM_JOIN + where +
        " ORDER BY {0} {1}, f.id {1} LIMIT ? OFFSET ?".format(sort, order),
        args + [limit, offset])
    out = [flight_row(r) for r in rows]
    heard = heard_by(db, [f['id'] for f in out])
    for f in out:
        f['heard_by'] = heard.get(f['id'], [])
    return {'total': total, 'limit': limit, 'offset': offset, 'rows': out}


def heard_by(db, flight_ids):
    """flight id -> the receivers that heard it, best signal first."""
    out = {}
    ids = list(flight_ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        rows = db.query(
            "SELECT fn.flight_id, fn.node_id, n.name, fn.receptions, fn.max_rssi, fn.first_ts,"
            " fn.last_ts FROM flight_nodes fn JOIN nodes n ON n.id = fn.node_id"
            " WHERE fn.flight_id IN ({0})"
            " ORDER BY fn.flight_id, COALESCE(fn.max_rssi, -999) DESC, n.name".format(
                ','.join('?' * len(chunk))), chunk)
        for r in rows:
            out.setdefault(r['flight_id'], []).append({
                'id': r['node_id'], 'name': r['name'], 'receptions': r['receptions'],
                'max_rssi': r['max_rssi'], 'first_ts': r['first_ts'], 'last_ts': r['last_ts']})
    return out


def flight_row(r):
    """Shape one flight for the table. Duration is derived, not stored twice."""
    ended = r['ended_at']
    started = r['started_at']
    return {
        'id': r['id'],
        'drone_id': r['drone_id'],
        'drone_label': r['drone_label'],
        'basic_id': r['drone_basic_id'],
        'tag': r['drone_tag'],
        'group_id': r['group_id'],
        'group_name': r['group_name'],
        'group_color': r['group_color'],
        'drone_color': r['drone_color'],
        # What to draw it in. drone_color stays raw: null still means "not
        # chosen", which the drones page shows as an automatic colour.
        'display_color': display_color(r['drone_color'], r['group_color'], r['drone_id']),
        'faa_make': r['faa_make'],
        'faa_model': r['faa_model'],
        'id_type': r['drone_id_type'],
        'mac': r['mac'],
        'node_id': r['node_id'],
        'started_at': started,
        'ended_at': ended,
        'duration_s': (ended - started) if ended is not None else None,
        'open': ended is None,
        'close_reason': r['close_reason'],
        'det_count': r['det_count'],
        'gps_count': r['gps_count'],
        'suspect_count': r['suspect_count'],
        'distance_m': r['path_len_m'],
        'max_alt_m': r['max_alt_m'],
        'min_alt_m': r['min_alt_m'],
        'max_speed_ms': r['max_speed_ms'],
        'start_lat': r['start_lat'], 'start_lon': r['start_lon'],
        'end_lat': r['end_lat'], 'end_lon': r['end_lon'],
        'pilot_lat': r['pilot_lat'], 'pilot_lon': r['pilot_lon'],
        'max_rssi': r['max_rssi'], 'min_rssi': r['min_rssi'],
        'has_track': (r['gps_count'] or 0) > 0,
        'label': r['label'],
    }


def get_flight(db, flight_id):
    r = db.one("SELECT" + FLIGHT_COLUMNS + FROM_JOIN + " WHERE f.id = ?", (flight_id,))
    if r is None:
        return None
    out = flight_row(r)
    macs = db.query(
        "SELECT mac, first_seen, last_seen FROM drone_macs WHERE drone_id = ? ORDER BY last_seen",
        (out['drone_id'],))
    out['drone_macs'] = [dict(m) for m in macs]
    out['heard_by'] = heard_by(db, [flight_id]).get(flight_id, [])
    return out


def flight_paths(db, ids):
    """Return cached simplified paths for the given flight ids.

    Open flights have no cached blob yet (it is written at close), so their path
    is built from their detection rows on demand - there are only ever a handful
    of open flights, so this stays cheap.
    """
    if not ids:
        return {}
    out = {}
    marks = ','.join('?' * len(ids))
    rows = db.query(
        "SELECT f.id, f.drone_id, f.simplified_path, f.ended_at, f.gps_count,"
        " d.color AS drone_color, g.color AS group_color"
        " FROM flights f JOIN drones d ON d.id=f.drone_id"
        " LEFT JOIN groups g ON g.id=d.group_id"
        " WHERE f.id IN ({0})".format(marks), list(ids))
    for r in rows:
        if not r['gps_count']:
            continue
        if r['simplified_path']:
            pts = json.loads(r['simplified_path'])
        else:
            pts = [[p['lat'], p['lon']] for p in db.query(
                "SELECT lat, lon FROM detections"
                " WHERE flight_id=? AND lat IS NOT NULL AND suspect=0 ORDER BY ts",
                (r['id'],))]
        if pts:
            # Same resolution as the table's swatch, so the two always match.
            out[str(r['id'])] = {'points': pts, 'color': display_color(
                r['drone_color'], r['group_color'], r['drone_id'])}
    return out


def flight_detections(db, flight_id, limit=1000, offset=0):
    limit = max(1, min(int(limit), 5000))
    rows = db.query(
        "SELECT ts, lat, lon, alt, pilot_lat, pilot_lon, rssi, band, channel,"
        " source, speed_ms, heading_deg, suspect"
        " FROM detections WHERE flight_id=? ORDER BY ts LIMIT ? OFFSET ?",
        (flight_id, limit, offset))
    total = db.one("SELECT COUNT(*) AS n FROM detections WHERE flight_id=?", (flight_id,))['n']
    return {'total': total, 'rows': [dict(r) for r in rows]}


def list_drones(db, p):
    where, args = [], []
    if p.get('group_id'):
        where.append("d.group_id = ?"); args.append(int(p['group_id']))
    if p.get('tag'):
        where.append("d.tag = ?"); args.append(p['tag'])
    q = (p.get('q') or '').strip()
    if q:
        like = '%' + q.lower() + '%'
        where.append("(LOWER(COALESCE(d.label,'')) LIKE ? OR LOWER(COALESCE(d.basic_id,'')) LIKE ?"
                     " OR LOWER(COALESCE(d.faa_make,'') || ' ' || COALESCE(d.faa_model,'')) LIKE ?"
                     " OR d.id IN (SELECT drone_id FROM drone_macs WHERE mac LIKE ?))")
        args += [like, like, like, like]
    w = ' WHERE ' + ' AND '.join(where) if where else ''
    rows = db.query(
        "SELECT d.*, g.name AS group_name, g.color AS group_color,"
        " (SELECT COUNT(*) FROM flights f WHERE f.drone_id=d.id) AS flight_count,"
        " (SELECT COUNT(*) FROM drone_macs m WHERE m.drone_id=d.id) AS mac_count,"
        " (SELECT COALESCE(SUM(path_len_m),0) FROM flights f WHERE f.drone_id=d.id) AS total_distance_m"
        " FROM drones d LEFT JOIN groups g ON g.id=d.group_id" + w +
        " ORDER BY d.last_seen DESC LIMIT 500", args)
    out = []
    for r in rows:
        d = dict(r)
        d.pop('faa_json', None)          # raw registry blob; the summary is on /summary
        d['display_color'] = display_color(d['color'], d['group_color'], d['id'])
        out.append(d)
    return out


def list_groups(db):
    rows = db.query(
        "SELECT g.*,"
        " (SELECT COUNT(*) FROM drones d WHERE d.group_id=g.id) AS drone_count,"
        " (SELECT COUNT(*) FROM flights f JOIN drones d ON d.id=f.drone_id"
        "   WHERE d.group_id=g.id) AS flight_count"
        " FROM groups g ORDER BY g.name")
    return [dict(r) for r in rows]


def stats(db):
    """Headline numbers for the table header."""
    row = db.one(
        "SELECT COUNT(*) AS flights,"
        " COALESCE(SUM(path_len_m),0) AS distance_m,"
        " COALESCE(SUM(det_count),0) AS detections,"
        " SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS open_flights,"
        " MIN(started_at) AS first_seen, MAX(started_at) AS last_seen"
        " FROM flights")
    out = dict(row)
    out['drones'] = db.one("SELECT COUNT(*) AS n FROM drones")['n']
    out['groups'] = db.one("SELECT COUNT(*) AS n FROM groups")['n']
    # Where the data is, so the map opens on the node's area rather than on a
    # hard-coded default.
    c = db.one("SELECT start_lat, start_lon FROM flights"
               " WHERE start_lat IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    out['center'] = [c['start_lat'], c['start_lon']] if c else None
    return out


def activity(db, bucket='hour'):
    """Flights per hour-of-day or day-of-week, for the analysis view."""
    fmt = '%H' if bucket == 'hour' else '%w'
    rows = db.query(
        "SELECT CAST(STRFTIME(?, started_at, 'unixepoch', 'localtime') AS INTEGER) AS k,"
        " COUNT(*) AS flights, COUNT(DISTINCT drone_id) AS drones"
        " FROM flights GROUP BY k ORDER BY k", (fmt,))
    return [dict(r) for r in rows]


def launch_points(db, cell_deg=0.0025, p=None):
    """Grid-snapped flight start points for the launch heatmap."""
    where, args = _filters(p or {})
    extra = "f.start_lat IS NOT NULL"
    where = (where + " AND " + extra) if where else (" WHERE " + extra)
    rows = db.query(
        "SELECT ROUND(f.start_lat/?)*? AS lat, ROUND(f.start_lon/?)*? AS lon,"
        " COUNT(*) AS n, COUNT(DISTINCT f.drone_id) AS drones, MAX(f.started_at) AS last"
        + FROM_JOIN + where +
        " GROUP BY lat, lon ORDER BY n DESC LIMIT 5000",
        [cell_deg, cell_deg, cell_deg, cell_deg] + args)
    return [dict(r) for r in rows]
