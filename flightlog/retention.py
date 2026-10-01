"""Retention and maintenance.

Flight summary rows and their cached paths are kept forever - they are small and
they are the record. What gets aged out is the raw `detections` rows behind old,
closed flights, which is where the volume is.

Deletion is chunked so it never takes a long write lock: on an SD card a single
unbounded DELETE can stall ingest for seconds.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

CHUNK = 5000
SWEEP_INTERVAL_S = 6 * 3600


def db_stats(db):
    """Row counts and on-disk size, for the maintenance panel."""
    page_size = db.one('PRAGMA page_size')[0]
    page_count = db.one('PRAGMA page_count')[0]
    return {
        'flights': db.one('SELECT COUNT(*) AS n FROM flights')['n'],
        'detections': db.one('SELECT COUNT(*) AS n FROM detections')['n'],
        'drones': db.one('SELECT COUNT(*) AS n FROM drones')['n'],
        'bytes': page_size * page_count,
        'path': db.path,
        'oldest': db.one('SELECT MIN(started_at) AS t FROM flights')['t'],
        'thinned_flights': db.one(
            "SELECT COUNT(*) AS n FROM flights f WHERE f.det_count > 0"
            " AND NOT EXISTS (SELECT 1 FROM detections d WHERE d.flight_id=f.id)")['n'],
    }


def prune(db, days, dry_run=False):
    """Delete detection rows for closed flights older than `days`.

    A flight is only eligible once it has a cached `simplified_path`, so the
    track survives the loss of its raw points. `det_count` and every summary
    statistic stay on the flight row, so the table and exports are unaffected.
    """
    if not days or days <= 0:
        return {'deleted': 0, 'eligible': 0, 'skipped': 'retention disabled'}
    cutoff = time.time() - (days * 86400)

    eligible = db.one(
        "SELECT COUNT(*) AS n FROM detections WHERE flight_id IN ("
        "  SELECT id FROM flights WHERE ended_at IS NOT NULL"
        "   AND ended_at < ? AND simplified_path IS NOT NULL)", (cutoff,))['n']
    if dry_run:
        return {'deleted': 0, 'eligible': eligible, 'cutoff': cutoff}

    deleted = 0
    while True:
        cur = db.execute(
            "DELETE FROM detections WHERE id IN ("
            "  SELECT d.id FROM detections d JOIN flights f ON f.id = d.flight_id"
            "   WHERE f.ended_at IS NOT NULL AND f.ended_at < ?"
            "     AND f.simplified_path IS NOT NULL LIMIT ?)", (cutoff, CHUNK))
        n = cur.rowcount or 0
        deleted += n
        if n < CHUNK:
            break
        time.sleep(0.05)          # let ingest through between chunks
    # The per-node coverage samples behind the same flights go too; like the
    # detections they are raw points, and flight_nodes keeps the summary.
    while True:
        cur = db.execute(
            "DELETE FROM node_samples WHERE rowid IN ("
            "  SELECT s.rowid FROM node_samples s JOIN flights f ON f.id = s.flight_id"
            "   WHERE f.ended_at IS NOT NULL AND f.ended_at < ? LIMIT ?)", (cutoff, CHUNK))
        if (cur.rowcount or 0) < CHUNK:
            break
        time.sleep(0.05)
    if deleted:
        try:
            db.execute('PRAGMA incremental_vacuum(1000)')
        except Exception:
            pass
        logger.info('retention: removed %d detection rows older than %d days', deleted, days)
    return {'deleted': deleted, 'eligible': eligible, 'cutoff': cutoff}


def vacuum(db):
    db.execute('VACUUM')
    db.execute('ANALYZE')
    return db_stats(db)


class RetentionJob:
    """Background sweeper. Off unless `days` is set."""

    def __init__(self, db, days=0):
        self.db = db
        self.days = days
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None or not self.days:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='flightlog-retention')
        self._thread.start()

    def _loop(self):
        # One sweep shortly after boot, then on a long interval.
        if self._stop.wait(120):
            return
        while not self._stop.is_set():
            try:
                prune(self.db, self.days)
            except Exception as e:
                logger.error('retention sweep failed: %s', e)
            if self._stop.wait(SWEEP_INTERVAL_S):
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
