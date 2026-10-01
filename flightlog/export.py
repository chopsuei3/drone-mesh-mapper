"""Exports, streamed.

All three generators yield incrementally off a cursor, so a large export never
materializes in memory. That is deliberately unlike the legacy
get_cumulative_log_for_emit(), which read an entire unbounded CSV into a list.

flights_csv is the summary export: one row per flight with duration, distance,
start/end coordinates and altitude - and no path data at all.
"""
import csv
import io
import json
from datetime import datetime, timezone

from . import queries

SUMMARY_COLUMNS = [
    'flight_id', 'drone_label', 'basic_id', 'group', 'tag', 'mac', 'node_id',
    'started_at', 'ended_at', 'duration_s', 'distance_m',
    'start_lat', 'start_lon', 'end_lat', 'end_lon',
    'max_alt_m_msl', 'min_alt_m_msl', 'max_speed_ms',
    'pilot_lat', 'pilot_lon', 'det_count', 'gps_count', 'suspect_count',
    'max_rssi', 'min_rssi', 'close_reason',
    'heard_by',            # receivers, "north:412;home:380" (name:receptions), best signal first
]


def _iso(ts):
    if ts is None:
        return ''
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _iter_flights(db, params, chunk=500):
    """Page through the filtered flight set without holding it all in memory."""
    p = dict(params)
    offset = 0
    while True:
        p['limit'] = str(chunk)
        p['offset'] = str(offset)
        page = queries.list_flights(db, p)
        rows = page['rows']
        if not rows:
            return
        for r in rows:
            yield r
        offset += len(rows)
        if offset >= page['total']:
            return


def flights_csv(db, params):
    """Summary rows only - no flight path. Altitudes are MSL, as the firmware
    reports geodetic altitude (AltitudeGeo), not height above ground."""
    buf = io.StringIO()
    w = csv.writer(buf)

    def drain():
        v = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return v

    w.writerow(SUMMARY_COLUMNS)
    yield drain()

    for f in _iter_flights(db, params):
        w.writerow([
            f['id'], f['drone_label'] or '', f['basic_id'] or '',
            f['group_name'] or '', f['tag'] or '', f['mac'] or '', f['node_id'] or '',
            _iso(f['started_at']), _iso(f['ended_at']),
            round(f['duration_s'], 1) if f['duration_s'] is not None else '',
            round(f['distance_m'] or 0, 1),
            f['start_lat'] if f['start_lat'] is not None else '',
            f['start_lon'] if f['start_lon'] is not None else '',
            f['end_lat'] if f['end_lat'] is not None else '',
            f['end_lon'] if f['end_lon'] is not None else '',
            f['max_alt_m'] if f['max_alt_m'] is not None else '',
            f['min_alt_m'] if f['min_alt_m'] is not None else '',
            round(f['max_speed_ms'], 2) if f['max_speed_ms'] is not None else '',
            f['pilot_lat'] if f['pilot_lat'] is not None else '',
            f['pilot_lon'] if f['pilot_lon'] is not None else '',
            f['det_count'], f['gps_count'], f['suspect_count'],
            f['max_rssi'] if f['max_rssi'] is not None else '',
            f['min_rssi'] if f['min_rssi'] is not None else '',
            f['close_reason'] or '',
            ';'.join('%s:%d' % (h['name'], h['receptions']) for h in f.get('heard_by') or []),
        ])
        yield drain()


def _xml_escape(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _name_for(f):
    who = f['drone_label'] or f['basic_id'] or f['mac'] or 'unknown'
    return "{0} - {1}".format(who, _iso(f['started_at'])[:19])


def flights_kml(db, params):
    """One Placemark per flight, grouped into a Folder per drone."""
    yield ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
           '<name>Flights</name>\n')
    for f in _iter_flights(db, params):
        paths = queries.flight_paths(db, [f['id']])
        pts = (paths.get(str(f['id'])) or {}).get('points') or []
        yield '<Placemark>\n<name>{0}</name>\n'.format(_xml_escape(_name_for(f)))
        yield ('<description>{0}</description>\n'.format(_xml_escape(
            'distance {0:.0f} m, duration {1}, max alt {2} m MSL'.format(
                f['distance_m'] or 0,
                '{0:.0f} s'.format(f['duration_s']) if f['duration_s'] else 'open',
                f['max_alt_m'] if f['max_alt_m'] is not None else '-'))))
        if len(pts) > 1:
            coords = ' '.join('{0},{1},0'.format(p[1], p[0]) for p in pts)
            yield ('<LineString><tessellate>1</tessellate>'
                   '<coordinates>{0}</coordinates></LineString>\n'.format(coords))
        elif pts:
            yield '<Point><coordinates>{0},{1},0</coordinates></Point>\n'.format(
                pts[0][1], pts[0][0])
        yield '</Placemark>\n'
    yield '</Document>\n</kml>\n'


def flights_gpx(db, params):
    yield ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<gpx version="1.1" creator="flightlog" '
           'xmlns="http://www.topografix.com/GPX/1/1">\n')
    for f in _iter_flights(db, params):
        paths = queries.flight_paths(db, [f['id']])
        pts = (paths.get(str(f['id'])) or {}).get('points') or []
        if not pts:
            continue
        yield '<trk><name>{0}</name><trkseg>\n'.format(_xml_escape(_name_for(f)))
        for p in pts:
            yield '<trkpt lat="{0}" lon="{1}"></trkpt>\n'.format(p[0], p[1])
        yield '</trkseg></trk>\n'
    yield '</gpx>\n'
