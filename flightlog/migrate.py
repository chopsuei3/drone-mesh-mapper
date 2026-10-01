"""Import legacy state into the flight database.

The importer feeds rows through the *same* Sessionizer the live path uses, with
record time substituted for wall-clock time. Two implementations of the
flight-splitting rule would drift apart, and then imported history would not
agree with live history for reasons nobody could reconstruct.

  python -m flightlog.migrate --csv cumulative_detections.csv
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

from .db import Database
from .identity import IdentityResolver
from .flights import Sessionizer, DEFAULT_GAP_S, DEFAULT_RESUME_S
from .parse import norm_basic_id

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _f(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coord(v):
    f = _f(v)
    return None if (f is None or f == 0.0) else f


def _ts(v):
    """The legacy CSV writes datetime.now().isoformat() - local time, no zone."""
    try:
        return datetime.fromisoformat(v).timestamp()
    except (TypeError, ValueError):
        return None


def stage_csv(db, path):
    """Load the CSV into a staging table so it can be replayed in time order.

    The file is in arrival order and, with several serial ports feeding it, rows
    from different drones interleave. The sessionizer needs per-drone monotonic
    time, and a global sort by timestamp guarantees that.
    """
    db.execute("DROP TABLE IF EXISTS _import")
    db.execute("CREATE TABLE _import (ts REAL, mac TEXT, basic_id TEXT, rssi INTEGER,"
               " lat REAL, lon REAL, alt REAL, plat REAL, plon REAL, alias TEXT, faa TEXT)")
    rows, skipped, batch = 0, 0, []
    with open(path, newline='', encoding='utf-8', errors='replace') as fh:
        for r in csv.DictReader(fh):
            ts = _ts(r.get('timestamp'))
            mac = (r.get('mac') or '').strip().lower()
            if ts is None or not mac:
                skipped += 1
                continue
            batch.append((ts, mac, norm_basic_id(r.get('basic_id')), _f(r.get('rssi')),
                          _coord(r.get('drone_lat')), _coord(r.get('drone_long')),
                          _f(r.get('drone_altitude')),
                          _coord(r.get('pilot_lat')), _coord(r.get('pilot_long')),
                          (r.get('alias') or '').strip() or None,
                          r.get('faa_data') or None))
            if len(batch) >= 5000:
                db.executemany("INSERT INTO _import VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
                rows += len(batch)
                batch = []
    if batch:
        db.executemany("INSERT INTO _import VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
        rows += len(batch)
    db.execute("CREATE INDEX _import_ts ON _import(ts)")
    return rows, skipped


def replay(db, gap_s=DEFAULT_GAP_S, resume_s=DEFAULT_RESUME_S):
    ident = IdentityResolver(db)
    sess = Sessionizer(db, ident, gap_s=gap_s, live=False, resume_s=resume_s)
    n = 0
    for r in db.conn.execute("SELECT * FROM _import ORDER BY ts, rowid"):
        sess.ingest({'mac': r['mac'], 'basic_id': r['basic_id'], 'rssi': r['rssi'],
                     'lat': r['lat'], 'lon': r['lon'], 'alt': r['alt'],
                     'pilot_lat': r['plat'], 'pilot_lon': r['plon'],
                     'node_id': None, 'band': None, 'channel': None,
                     'source': 'import'}, r['ts'])
        n += 1
    sess.close_all('imported')
    sess.flush()
    return n


def import_state(db):
    """Apply aliases.json / drone_tags.json / faa_cache.csv onto drone records."""
    out = {'labels': 0, 'tags': 0, 'faa': 0, 'stubs': 0}

    def drone_for(mac):
        row = db.one("SELECT drone_id FROM drone_macs WHERE mac=?", (mac,))
        return row['drone_id'] if row else None

    def stub(mac):
        cur = db.execute("INSERT INTO drones(first_seen,last_seen) VALUES(0,0)")
        did = cur.lastrowid
        db.execute("INSERT OR REPLACE INTO drone_macs(mac,drone_id,first_seen,last_seen)"
                   " VALUES(?,?,0,0)", (mac, did))
        out['stubs'] += 1
        return did

    for fname, col, key in (('aliases.json', 'label', 'labels'),
                            ('drone_tags.json', 'tag', 'tags')):
        path = os.path.join(BASE_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            continue
        for mac, value in (data or {}).items():
            mac = (mac or '').strip().lower()
            if not mac or not value:
                continue
            did = drone_for(mac) or stub(mac)
            db.execute(
                "UPDATE drones SET {0}=? WHERE id=? AND ({0} IS NULL OR {0}='')".format(col),
                (value, did))
            out[key] += 1

    faa = os.path.join(BASE_DIR, 'faa_cache.csv')
    if os.path.exists(faa):
        # The legacy cache is keyed (mac, remote_id) and appended without dedupe,
        # so later rows win. The registry is per serial, so it is re-keyed here.
        by_bid = {}
        with open(faa, newline='', encoding='utf-8', errors='replace') as fh:
            for r in csv.DictReader(fh):
                bid = norm_basic_id(r.get('remote_id'))
                if bid and r.get('faa_response'):
                    by_bid[bid] = r['faa_response']
        for bid, blob in by_bid.items():
            cur = db.execute("UPDATE drones SET faa_json=? WHERE basic_id=?", (blob, bid))
            out['faa'] += cur.rowcount
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=None)
    ap.add_argument('--csv', default=os.path.join(BASE_DIR, 'cumulative_detections.csv'))
    ap.add_argument('--gap', type=float, default=DEFAULT_GAP_S)
    ap.add_argument('--resume-window', type=float, default=DEFAULT_RESUME_S)
    ap.add_argument('--force', action='store_true', help='import even if flights already exist')
    ap.add_argument('--skip-state', action='store_true')
    args = ap.parse_args(argv)

    db = Database(args.db)
    existing = db.one("SELECT COUNT(*) AS n FROM flights")['n']
    if existing and not args.force:
        print("refusing to import: {0} flights already present (use --force)".format(existing))
        return 1
    if not os.path.exists(args.csv):
        print("no such CSV: {0}".format(args.csv))
        return 1

    rows, skipped = stage_csv(db, args.csv)
    print("staged   {0} rows ({1} skipped as malformed)".format(rows, skipped))
    replayed = replay(db, args.gap, args.resume_window)
    db.execute("DROP TABLE IF EXISTS _import")

    state = {} if args.skip_state else import_state(db)
    flights = db.one("SELECT COUNT(*) AS n FROM flights")['n']
    notrack = db.one("SELECT COUNT(*) AS n FROM flights WHERE gps_count=0")['n']
    drones = db.one("SELECT COUNT(*) AS n FROM drones")['n']
    suspect = db.one("SELECT COALESCE(SUM(suspect_count),0) AS n FROM flights")['n']
    print("replayed {0} detections".format(replayed))
    print("  drones  {0}   flights {1}   no-track {2}   suspect points {3}".format(
        drones, flights, notrack, suspect))
    if state:
        print("  labels  {0}  tags {1}  faa {2}  stub drones {3}".format(
            state['labels'], state['tags'], state['faa'], state['stubs']))
    db.execute("ANALYZE")
    db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
