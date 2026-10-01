"""Write operations that change flight or drone structure.

`recompute` is the shared primitive: it rebuilds a flight's stored statistics
from its detection rows. Merging, splitting and crash recovery all reduce to
"reassign some rows, then recompute", so there is exactly one implementation of
how a flight's numbers are derived and it cannot drift from the live path.
"""
import json

from .colors import display_color
from .geo import haversine_m, simplify, decimate
from .services.faa import faa_summary, serial_format
from .flights import (MAX_PLAUSIBLE_SPEED_MS, MAX_PLAUSIBLE_STEP_M,
                      MIN_SPEED_DT_S, SIMPLIFY_TOLERANCE, MAX_STORED_POINTS)


def recompute(db, flight_id):
    """Rebuild every derived column on a flight from its detection rows.

    Applies the same outlier rules as the live sessionizer, so a recomputed
    flight and a live one agree.
    """
    rows = db.query(
        "SELECT ts, lat, lon, alt, pilot_lat, pilot_lon, rssi FROM detections"
        " WHERE flight_id=? ORDER BY ts", (flight_id,))
    if not rows:
        return None

    det_count = len(rows)
    gps = suspect = 0
    dist = 0.0
    max_alt = min_alt = max_speed = None
    max_rssi = min_rssi = None
    start = end = None
    bn = bs = be = bw = None
    pilot = None
    pts = []
    prev = None                      # (lat, lon, ts)

    for r in rows:
        if r['rssi'] is not None:
            max_rssi = r['rssi'] if max_rssi is None else max(max_rssi, r['rssi'])
            min_rssi = r['rssi'] if min_rssi is None else min(min_rssi, r['rssi'])
        if pilot is None and r['pilot_lat'] is not None:
            pilot = (r['pilot_lat'], r['pilot_lon'])

        lat, lon, ts = r['lat'], r['lon'], r['ts']
        if lat is None or lon is None:
            continue

        step = dt = None
        if prev is not None:
            step = haversine_m(prev[0], prev[1], lat, lon)
            dt = ts - prev[2]
            bad = step > MAX_PLAUSIBLE_STEP_M
            if not bad and dt >= MIN_SPEED_DT_S:
                bad = (step / dt) > MAX_PLAUSIBLE_SPEED_MS
            if bad:
                suspect += 1
                continue

        gps += 1
        if r['alt'] is not None:
            max_alt = r['alt'] if max_alt is None else max(max_alt, r['alt'])
            min_alt = r['alt'] if min_alt is None else min(min_alt, r['alt'])
        if start is None:
            start = (lat, lon)
            bn = bs = lat
            be = bw = lon
        else:
            bn, bs = max(bn, lat), min(bs, lat)
            be, bw = max(be, lon), min(bw, lon)
        end = (lat, lon)
        if step is not None:
            dist += step
            if dt is not None and dt >= MIN_SPEED_DT_S:
                v = step / dt
                max_speed = v if max_speed is None else max(max_speed, v)
        pts.append([lat, lon])
        prev = (lat, lon, ts)

    path = simplify(pts, SIMPLIFY_TOLERANCE) if len(pts) > 2 else list(pts)
    if len(path) > MAX_STORED_POINTS:
        path = decimate(path, MAX_STORED_POINTS)

    db.execute(
        "UPDATE flights SET started_at=?, ended_at=?, det_count=?, gps_count=?,"
        " suspect_count=?, path_len_m=?, max_alt_m=?, min_alt_m=?, max_speed_ms=?,"
        " start_lat=?, start_lon=?, end_lat=?, end_lon=?,"
        " bbox_n=?, bbox_s=?, bbox_e=?, bbox_w=?, pilot_lat=?, pilot_lon=?,"
        " max_rssi=?, min_rssi=?, simplified_path=? WHERE id=?",
        (rows[0]['ts'], rows[-1]['ts'], det_count, gps, suspect, dist,
         max_alt, min_alt, max_speed,
         start[0] if start else None, start[1] if start else None,
         end[0] if end else None, end[1] if end else None,
         bn, bs, be, bw,
         pilot[0] if pilot else None, pilot[1] if pilot else None,
         max_rssi, min_rssi,
         json.dumps([[round(p[0], 6), round(p[1], 6)] for p in path]), flight_id))
    return flight_id


def merge_flights(db, ids):
    """Merge several flights into the earliest one.

    The 60s gap rule splits a single real flight whenever the drone drops out of
    RF range for longer than that, which is common at the edge of a node's
    range. This is the repair. Flights must belong to the same drone - merging
    across drones would be an identity assertion, not a flight repair, and
    that is a different (and destructive) operation.
    """
    ids = sorted(set(int(i) for i in ids))
    if len(ids) < 2:
        return {'error': 'need at least two flights'}

    marks = ','.join('?' * len(ids))
    rows = db.query(
        "SELECT id, drone_id, started_at FROM flights WHERE id IN ({0})".format(marks), ids)
    if len(rows) != len(ids):
        return {'error': 'one or more flights not found'}
    drone_ids = {r['drone_id'] for r in rows}
    if len(drone_ids) > 1:
        return {'error': 'flights belong to different drones; '
                         'reassign the drone first if they are really the same aircraft'}

    keep = min(rows, key=lambda r: r['started_at'])['id']
    others = [r['id'] for r in rows if r['id'] != keep]
    om = ','.join('?' * len(others))
    db.execute("UPDATE detections SET flight_id=? WHERE flight_id IN ({0})".format(om),
               [keep] + others)
    db.execute("DELETE FROM flights WHERE id IN ({0})".format(om), others)
    recompute(db, keep)
    db.execute("UPDATE flights SET close_reason='merged' WHERE id=?", (keep,))
    return {'merged_into': keep, 'removed': others}


def reassign_flight(db, flight_id, drone_id):
    """Move a flight to a different drone, then refresh both drones' counters."""
    row = db.one("SELECT drone_id FROM flights WHERE id=?", (flight_id,))
    if row is None:
        return {'error': 'flight not found'}
    if db.one("SELECT id FROM drones WHERE id=?", (drone_id,)) is None:
        return {'error': 'drone not found'}
    db.execute("UPDATE flights SET drone_id=? WHERE id=?", (drone_id, flight_id))
    db.execute("UPDATE detections SET flight_id=flight_id WHERE flight_id=?", (flight_id,))
    return {'flight_id': flight_id, 'drone_id': drone_id, 'was': row['drone_id']}


def merge_drones(db, into_id, from_ids):
    """Fold several drone records into one.

    Auto-resolution deliberately refuses to merge two drones that each hold a
    different valid serial, so this is the manual escape hatch for when it gets
    it wrong. Not reversible.
    """
    from_ids = [int(i) for i in from_ids if int(i) != int(into_id)]
    if not from_ids:
        return {'error': 'nothing to merge'}
    if db.one("SELECT id FROM drones WHERE id=?", (into_id,)) is None:
        return {'error': 'target drone not found'}
    marks = ','.join('?' * len(from_ids))
    db.execute("UPDATE flights SET drone_id=? WHERE drone_id IN ({0})".format(marks),
               [into_id] + from_ids)
    db.execute("UPDATE drone_macs SET drone_id=? WHERE drone_id IN ({0})".format(marks),
               [into_id] + from_ids)
    db.execute("DELETE FROM drones WHERE id IN ({0})".format(marks), from_ids)
    db.execute(
        "UPDATE drones SET first_seen=(SELECT MIN(started_at) FROM flights WHERE drone_id=?),"
        " last_seen=(SELECT MAX(COALESCE(ended_at,started_at)) FROM flights WHERE drone_id=?)"
        " WHERE id=?", (into_id, into_id, into_id))
    return {'merged_into': into_id, 'removed': from_ids}


def drone_summary(db, drone_id):
    """Lifetime rollups for the per-drone page."""
    d = db.one("SELECT d.*, g.name AS group_name, g.color AS group_color"
               " FROM drones d LEFT JOIN groups g ON g.id=d.group_id WHERE d.id=?", (drone_id,))
    if d is None:
        return None
    out = dict(d)
    agg = db.one(
        "SELECT COUNT(*) AS flights, COALESCE(SUM(path_len_m),0) AS distance_m,"
        " COALESCE(SUM(det_count),0) AS detections,"
        " COALESCE(SUM(COALESCE(ended_at,started_at)-started_at),0) AS airtime_s,"
        " MAX(max_alt_m) AS max_alt_m, MAX(max_speed_ms) AS max_speed_ms,"
        " MIN(started_at) AS first_flight, MAX(started_at) AS last_flight"
        " FROM flights WHERE drone_id=?", (drone_id,))
    out.update(dict(agg))
    out['macs'] = [dict(r) for r in db.query(
        "SELECT mac, first_seen, last_seen FROM drone_macs WHERE drone_id=? ORDER BY last_seen DESC",
        (drone_id,))]
    out['display_color'] = display_color(out['color'], out['group_color'], out['id'])
    # The registry blob is replaced by its summary, plus what the serial's own
    # structure says - which needs no network at all.
    out['faa'] = faa_summary(out.pop('faa_json', None), out)
    out['serial_format'] = serial_format(out['basic_id'])
    return out
