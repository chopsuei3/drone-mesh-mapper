"""Aggregates for the Analysis page.

Every view is built from the same WHERE clause - queries._filters over
queries.FROM_JOIN - so the tiles, the heatmap, the tables, the map and the
radio breakdown always describe the same slice of flights, and their numbers
agree with each other.

Times are bucketed with SQLite's 'localtime', i.e. the timezone of the machine
running flightlog - the Pi, which is also where the drones were heard.
"""
import math
from collections import defaultdict

from .colors import display_color
from .geo import haversine_m
from .queries import FROM_JOIN, _filters

DUR = "(COALESCE(f.ended_at, f.started_at) - f.started_at)"
HOUR = "CAST(STRFTIME('%H', f.started_at, 'unixepoch', 'localtime') AS INTEGER)"
DOW = "CAST(STRFTIME('%w', f.started_at, 'unixepoch', 'localtime') AS INTEGER)"
DAY = "DATE(f.started_at, 'unixepoch', 'localtime')"

MAX_DRONES = 500           # rows of per-drone detail returned
HOME_CELL_DEG = 0.001      # ~100 m: what counts as "the same spot" for a home launch
TOP_MODELS = 10
MAX_CELLS = 5000
POPUP_DRONES = 8
MIN_CELL_DEG, MAX_CELL_DEG = 0.0001, 0.5
MERGE_CELLS = 0.8          # cells closer than this (in cell widths) become one circle
ID_CHUNK = 400             # keeps IN (...) lists well under SQLite's variable limit
RANGE_SAMPLES = 20000      # newest coverage samples per node that a range is measured over


def _where(p, extra=None):
    where, args = _filters(p or {})
    if extra:
        where = (where + ' AND ' + extra) if where else (' WHERE ' + extra)
    return where, list(args)


def _num(p, key):
    v = (p or {}).get(key)
    return float(v) if v not in (None, '') else None


def _name(label, basic_id, drone_id):
    return label or basic_id or ('drone #%d' % drone_id)


def _chunks(ids):
    for i in range(0, len(ids), ID_CHUNK):
        yield ids[i:i + ID_CHUNK]


def _in(where, args, ids):
    """The shared WHERE plus `f.drone_id IN (...)` for one chunk of ids."""
    marks = ','.join('?' * len(ids))
    extra = 'f.drone_id IN (%s)' % marks
    return ((where + ' AND ' + extra) if where else (' WHERE ' + extra)), args + list(ids)


# -- overview -------------------------------------------------------------------
def overview(db, p):
    """Tiles, the hour x weekday heatmap, groups, drones and models - one slice."""
    where, args = _where(p)

    t = db.one("SELECT COUNT(*) AS flights, COUNT(DISTINCT f.drone_id) AS drones,"
               " COALESCE(SUM(" + DUR + "), 0) AS airtime_s,"
               " COALESCE(SUM(f.path_len_m), 0) AS distance_m" + FROM_JOIN + where, args)

    # Distinct drones cannot be summed from cells, so each margin asks for itself.
    heat = {
        'cells': [dict(r) for r in db.query(
            "SELECT " + DOW + " AS dow, " + HOUR + " AS hour, COUNT(*) AS flights,"
            " COUNT(DISTINCT f.drone_id) AS drones" + FROM_JOIN + where +
            " GROUP BY dow, hour", args)],
        'by_dow': [dict(r) for r in db.query(
            "SELECT " + DOW + " AS dow, COUNT(*) AS flights, COUNT(DISTINCT f.drone_id) AS drones"
            + FROM_JOIN + where + " GROUP BY dow", args)],
        'by_hour': [dict(r) for r in db.query(
            "SELECT " + HOUR + " AS hour, COUNT(*) AS flights, COUNT(DISTINCT f.drone_id) AS drones"
            + FROM_JOIN + where + " GROUP BY hour", args)],
    }

    # Every drone in the slice: groups roll up from these, and "new" counts them all.
    rows = [dict(r) for r in db.query(
        "SELECT f.drone_id, d.label, d.basic_id, d.tag, d.color, d.faa_make, d.faa_model, d.id_type,"
        " d.group_id, g.name AS group_name, g.color AS group_color,"
        " COUNT(*) AS flights, COUNT(DISTINCT " + DAY + ") AS days,"
        " COALESCE(SUM(" + DUR + "), 0) AS airtime_s,"
        " COALESCE(SUM(f.path_len_m), 0) AS distance_m, MAX(f.max_alt_m) AS max_alt_m,"
        " MIN(f.started_at) AS first_seen, MAX(f.started_at) AS last_seen"
        + FROM_JOIN + where + " GROUP BY f.drone_id ORDER BY flights DESC, last_seen DESC", args)]

    # First flight *ever*, independent of the date range: that is what makes a drone new.
    first_ever = {}
    ids = [r['drone_id'] for r in rows]
    for chunk in _chunks(ids):
        marks = ','.join('?' * len(chunk))
        for r in db.query("SELECT drone_id, MIN(started_at) AS t FROM flights"
                          " WHERE drone_id IN (%s) GROUP BY drone_id" % marks, chunk):
            first_ever[r['drone_id']] = r['t']
    lo, hi = _num(p, 'from'), _num(p, 'to')

    def is_new(did):
        t0 = first_ever.get(did)
        return t0 is not None and (lo is None or t0 >= lo) and (hi is None or t0 <= hi)

    new_drones = sum(1 for d in ids if is_new(d))

    top = rows[:MAX_DRONES]
    top_ids = [r['drone_id'] for r in top]
    hours = defaultdict(lambda: [0] * 24)
    spots = defaultdict(list)
    for chunk in _chunks(top_ids):
        w, a = _in(where, args, chunk)
        for r in db.query("SELECT f.drone_id, " + HOUR + " AS hour, COUNT(*) AS n"
                          + FROM_JOIN + w + " GROUP BY f.drone_id, hour", a):
            hours[r['drone_id']][r['hour']] = r['n']
        w2, a2 = _in(where + (' AND' if where else ' WHERE') + ' f.start_lat IS NOT NULL', args, chunk)
        for r in db.query(
                "SELECT f.drone_id, ROUND(f.start_lat / ?) AS gy, ROUND(f.start_lon / ?) AS gx,"
                " AVG(f.start_lat) AS lat, AVG(f.start_lon) AS lon, COUNT(*) AS n"
                + FROM_JOIN + w2 + " GROUP BY f.drone_id, gy, gx",
                [HOME_CELL_DEG, HOME_CELL_DEG] + a2):
            spots[r['drone_id']].append(r)

    drones = []
    for r in top:
        did = r['drone_id']
        cells_ = spots.get(did) or []
        home = None
        if cells_:
            best = max(cells_, key=lambda c: c['n'])
            home = {'lat': best['lat'], 'lon': best['lon'], 'n': best['n'],
                    'of': sum(c['n'] for c in cells_)}
        model = ' '.join(x for x in (r['faa_make'], r['faa_model']) if x) or None
        drones.append({
            'id': did, 'name': _name(r['label'], r['basic_id'], did),
            'label': r['label'], 'basic_id': r['basic_id'], 'tag': r['tag'],
            'color': display_color(r['color'], r['group_color'], did),
            'group_id': r['group_id'], 'group_name': r['group_name'], 'model': model,
            'id_type': r['id_type'],
            'flights': r['flights'], 'days': r['days'], 'airtime_s': r['airtime_s'],
            'distance_m': r['distance_m'], 'max_alt_m': r['max_alt_m'],
            'first_seen': r['first_seen'], 'last_seen': r['last_seen'],
            'first_ever': first_ever.get(did), 'new': is_new(did),
            'hours': hours[did] if did in hours else [0] * 24, 'home': home,
        })

    return {
        'tiles': {'flights': t['flights'], 'drones': t['drones'], 'new_drones': new_drones,
                  'airtime_s': t['airtime_s'], 'distance_m': t['distance_m']},
        'heatmap': heat,
        'groups': _groups(rows),
        'drones': drones,
        'drones_total': len(rows),
        'models': _models(rows),
    }


def _groups(rows):
    """Roll the per-drone rows up by group, so the numbers match the drone table."""
    acc = {}
    for r in rows:
        k = r['group_id']
        g = acc.setdefault(k, {'id': k, 'name': r['group_name'] if k else None,
                               'color': r['group_color'] if k else None,
                               'drones': 0, 'flights': 0, 'airtime_s': 0.0, 'last_seen': None})
        g['drones'] += 1
        g['flights'] += r['flights']
        g['airtime_s'] += r['airtime_s']
        g['last_seen'] = max(g['last_seen'] or 0, r['last_seen'] or 0) or None
    # Named groups by activity; "no group" last, however big it is.
    return (sorted((g for k, g in acc.items() if k is not None),
                   key=lambda g: (-g['flights'], g['name'] or ''))
            + [g for k, g in acc.items() if k is None])


def _models(rows):
    """Distinct drones and flights per FAA make/model, top N + Other + not identified."""
    acc, unknown = {}, {'drones': 0, 'flights': 0}
    for r in rows:
        name = ' '.join(x for x in (r['faa_make'], r['faa_model']) if x)
        if not name:
            unknown['drones'] += 1
            unknown['flights'] += r['flights']
            continue
        m = acc.setdefault(name, {'name': name, 'drones': 0, 'flights': 0})
        m['drones'] += 1
        m['flights'] += r['flights']
    ranked = sorted(acc.values(), key=lambda m: (-m['drones'], -m['flights'], m['name']))
    rest = ranked[TOP_MODELS:]
    other = ({'models': len(rest), 'drones': sum(m['drones'] for m in rest),
              'flights': sum(m['flights'] for m in rest)} if rest else None)
    return {'rows': ranked[:TOP_MODELS], 'other': other,
            'unidentified': unknown if unknown['drones'] else None}


# -- map --------------------------------------------------------------------------
def cells(db, p, kind='launch', cell_deg=0.0025):
    """Launch points (or operator positions) snapped to a grid.

    Each cell is placed at the mean of its real positions, not the grid corner,
    so a coarse grid still puts the circle where the drones actually were.

    A spot that straddles a grid line splits into two cells whose means land
    almost on top of each other - two circles, labels colliding, each popup
    counting half the spot. So cells closer than MERGE_CELLS widths to a busier
    one are folded into it, with exact per-drone counts carried across.
    """
    lat, lon = ('f.pilot_lat', 'f.pilot_lon') if kind == 'pilot' else ('f.start_lat', 'f.start_lon')
    try:
        c = min(max(float(cell_deg), MIN_CELL_DEG), MAX_CELL_DEG)
    except (TypeError, ValueError):
        c = 0.0025
    where, args = _where(p, '%s IS NOT NULL AND %s IS NOT NULL' % (lat, lon))
    rows = db.query(
        "SELECT ROUND({lat} / ?) AS gy, ROUND({lon} / ?) AS gx, f.drone_id,"
        " d.label, d.basic_id, d.color, g.color AS group_color,"
        " COUNT(*) AS n, SUM({lat}) AS slat, SUM({lon}) AS slon, MAX(f.started_at) AS last"
        .format(lat=lat, lon=lon) + FROM_JOIN + where + " GROUP BY gy, gx, f.drone_id",
        [c, c] + args)
    acc = {}
    for r in rows:
        cell = acc.setdefault((r['gy'], r['gx']), {'n': 0, 'slat': 0.0, 'slon': 0.0,
                                                   'last': None, 'drones': {}})
        cell['n'] += r['n']
        cell['slat'] += r['slat']
        cell['slon'] += r['slon']
        cell['last'] = max(cell['last'] or 0, r['last'] or 0) or None
        cell['drones'][r['drone_id']] = {
            'id': r['drone_id'], 'n': r['n'],
            'name': _name(r['label'], r['basic_id'], r['drone_id']),
            'color': display_color(r['color'], r['group_color'], r['drone_id'])}

    kept = _merge_close(sorted(acc.values(), key=lambda x: -x['n']), c)
    out = []
    for cell in sorted(kept, key=lambda x: -x['n'])[:MAX_CELLS]:
        ds = sorted(cell['drones'].values(), key=lambda d: (-d['n'], d['name']))
        out.append({'lat': cell['slat'] / cell['n'], 'lon': cell['slon'] / cell['n'],
                    'n': cell['n'], 'drones': len(ds), 'last': cell['last'],
                    'top': ds[:POPUP_DRONES]})
    return {'kind': 'pilot' if kind == 'pilot' else 'launch', 'cell_deg': c, 'cells': out}


def _merge_close(cells_, c):
    """Greedy, busiest first: fold each cell into a kept one within MERGE_CELLS.

    Distances are in cell widths with longitude scaled by latitude, found through
    a one-cell spatial hash so this stays linear in the number of cells.
    """
    if not cells_:
        return []
    lat_mid = sum(x['slat'] for x in cells_) / sum(x['n'] for x in cells_)
    kx_scale = math.cos(math.radians(lat_mid))
    kept, grid = [], {}
    for cell in cells_:
        lat0, lon0 = cell['slat'] / cell['n'], cell['slon'] / cell['n']
        by, bx = int(math.floor(lat0 / c)), int(math.floor(lon0 * kx_scale / c))
        target = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for k in grid.get((by + dy, bx + dx), ()):
                    klat, klon = k['slat'] / k['n'], k['slon'] / k['n']
                    if math.hypot((klat - lat0) / c, (klon - lon0) * kx_scale / c) < MERGE_CELLS:
                        target = k
                        break
                if target:
                    break
            if target:
                break
        if target is None:
            kept.append(cell)
            grid.setdefault((by, bx), []).append(cell)
            continue
        target['n'] += cell['n']
        target['slat'] += cell['slat']
        target['slon'] += cell['slon']
        target['last'] = max(target['last'] or 0, cell['last'] or 0) or None
        for did, d in cell['drones'].items():
            if did in target['drones']:
                target['drones'][did]['n'] += d['n']
            else:
                target['drones'][did] = dict(d)
    return kept


# -- receivers -----------------------------------------------------------------------
def _pct(sorted_vals, q):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(math.ceil(q * len(sorted_vals))) - 1))
    return sorted_vals[k]


def nodes(db, p):
    """Per receiver, over the filtered flights: what it heard, and from how far.

    Counts come from flight_nodes, so a reading another node delivered first
    still counts for every node that heard it. Range is the distance from the
    node's location to the drone positions it heard (node_samples, which keep
    those copies too), over the newest RANGE_SAMPLES per node - typical (p50),
    far (p95) and farthest. A node without a location has no range.
    """
    where, args = _where(p)
    stats = {r['node_id']: dict(r) for r in db.query(
        "SELECT fn.node_id, COUNT(DISTINCT fn.flight_id) AS flights,"
        " COUNT(DISTINCT f.drone_id) AS drones, SUM(fn.receptions) AS receptions,"
        " MAX(fn.max_rssi) AS max_rssi, MAX(fn.last_ts) AS last_heard"
        " FROM flight_nodes fn JOIN flights f ON f.id = fn.flight_id"
        " JOIN drones d ON d.id = f.drone_id LEFT JOIN groups g ON g.id = d.group_id"
        + where + " GROUP BY fn.node_id", args)}
    out = []
    for n in db.query("SELECT id, name, kind, lat, lon FROM nodes ORDER BY kind <> 'local', name"):
        s = stats.get(n['id'], {})
        row = {'id': n['id'], 'name': n['name'], 'kind': n['kind'], 'lat': n['lat'], 'lon': n['lon'],
               'flights': s.get('flights', 0), 'drones': s.get('drones', 0),
               'receptions': s.get('receptions', 0), 'max_rssi': s.get('max_rssi'),
               'last_heard': s.get('last_heard'), 'range': None}
        if n['lat'] is not None and row['flights']:
            w, a = _where(p, 's.node_id = ?')
            pts = db.query(
                "SELECT s.lat, s.lon FROM node_samples s JOIN flights f ON f.id = s.flight_id"
                " JOIN drones d ON d.id = f.drone_id LEFT JOIN groups g ON g.id = d.group_id"
                + w + " ORDER BY s.ts DESC LIMIT ?", a + [n['id'], RANGE_SAMPLES])
            dists = sorted(haversine_m(n['lat'], n['lon'], r['lat'], r['lon']) for r in pts)
            if dists:
                row['range'] = {'samples': len(dists), 'p50_m': _pct(dists, 0.5),
                                'p95_m': _pct(dists, 0.95), 'max_m': dists[-1]}
        out.append(row)
    return {'nodes': out, 'max_samples': RANGE_SAMPLES}


# -- radio -------------------------------------------------------------------------
_BAND_ORDER = {'BLE': 0, '2.4GHz': 1, '5GHz': 2}


def radio(db, p):
    """How drones were heard: detections per band and channel.

    The only view that reads the detections table, so the page loads it on its
    own. Detections recorded before the firmware reported band/channel - or
    pruned by retention - are counted as "not reported" or not at all.
    """
    where, args = _where(p)
    rows = db.query(
        "SELECT det.band AS band, det.channel AS channel, d.id_type AS id_type,"
        " COUNT(*) AS detections, COUNT(DISTINCT f.drone_id) AS drones"
        " FROM detections det JOIN flights f ON f.id = det.flight_id"
        " JOIN drones d ON d.id = f.drone_id LEFT JOIN groups g ON g.id = d.group_id"
        + where + " GROUP BY det.band, det.channel, d.id_type", args)
    out, missing = [], None
    for r in rows:
        band, ch = r['band'], r['channel']
        if not band:
            missing = {'detections': (missing or {}).get('detections', 0) + r['detections'],
                       'drones': max((missing or {}).get('drones', 0), r['drones'])}
            continue
        if band == 'BLE':
            label = 'BLE'
        elif band == '2.4GHz':
            label = 'Wi-Fi ch %s' % ch if ch else 'Wi-Fi'
        elif band == '5GHz':
            label = '5 GHz ch %s' % ch if ch else '5 GHz'
        else:
            label = '%s ch %s' % (band, ch) if ch else str(band)
        if r['id_type'] == 'DJI':
            label = 'DJI DroneID ' + label.replace('Wi-Fi ', '')
        out.append({'label': label, 'band': band, 'channel': ch, 'id_type': r['id_type'],
                    'detections': r['detections'], 'drones': r['drones']})
    # Remote ID first, DJI DroneID after; each by band then channel.
    out.sort(key=lambda x: (x['id_type'] == 'DJI', _BAND_ORDER.get(x['band'], 3),
                            x['channel'] or 0, x['label']))
    return {'rows': out, 'not_reported': missing}
