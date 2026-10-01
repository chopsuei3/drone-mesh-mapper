"""SQLite storage for flights, detections and drone identity.

The connection settings mirror the pattern already proven for MBTiles in
mesh-mapper.py:248 (`_mbtiles_get`) - WAL, NORMAL sync, a generous busy_timeout
and check_same_thread=False - because the same shape of access applies here:
several serial reader threads writing while Flask request threads read.

All writes go through `write_lock()`. SQLite serializes writers anyway; holding
an explicit lock keeps the batched inserts in flights.py atomic as a unit.
"""
import os
import sqlite3
import threading

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT);

CREATE TABLE IF NOT EXISTS groups (
  id        INTEGER PRIMARY KEY,
  name      TEXT UNIQUE NOT NULL,
  kind      TEXT,
  color     TEXT,
  parent_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
  notes     TEXT);

CREATE TABLE IF NOT EXISTS drones (
  id         INTEGER PRIMARY KEY,
  basic_id   TEXT UNIQUE,
  label      TEXT,
  tag        TEXT,
  group_id   INTEGER REFERENCES groups(id) ON DELETE SET NULL,
  faa_json   TEXT,
  notes      TEXT,
  first_seen REAL,
  last_seen  REAL);

CREATE TABLE IF NOT EXISTS drone_macs (
  mac        TEXT PRIMARY KEY,
  drone_id   INTEGER NOT NULL REFERENCES drones(id) ON DELETE CASCADE,
  first_seen REAL,
  last_seen  REAL);

CREATE TABLE IF NOT EXISTS flights (
  id           INTEGER PRIMARY KEY,
  drone_id     INTEGER NOT NULL REFERENCES drones(id) ON DELETE CASCADE,
  mac          TEXT,
  node_id      TEXT,
  started_at   REAL NOT NULL,
  ended_at     REAL,
  close_reason TEXT,
  det_count    INTEGER NOT NULL DEFAULT 0,
  gps_count    INTEGER NOT NULL DEFAULT 0,
  suspect_count INTEGER NOT NULL DEFAULT 0,
  path_len_m   REAL NOT NULL DEFAULT 0,
  max_alt_m    REAL,
  min_alt_m    REAL,
  max_speed_ms REAL,
  start_lat    REAL, start_lon REAL,
  end_lat      REAL, end_lon   REAL,
  bbox_n REAL, bbox_s REAL, bbox_e REAL, bbox_w REAL,
  pilot_lat    REAL, pilot_lon REAL,
  max_rssi     INTEGER,
  min_rssi     INTEGER,
  simplified_path TEXT,
  label        TEXT,
  notes        TEXT);

CREATE TABLE IF NOT EXISTS detections (
  id        INTEGER PRIMARY KEY,
  flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
  ts        REAL NOT NULL,
  lat REAL, lon REAL, alt REAL,
  pilot_lat REAL, pilot_lon REAL,
  rssi INTEGER,
  band TEXT,
  channel INTEGER,
  source TEXT,
  speed_ms REAL,
  heading_deg REAL,
  suspect INTEGER NOT NULL DEFAULT 0);

CREATE INDEX IF NOT EXISTS ix_flights_started ON flights(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_flights_drone   ON flights(drone_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_flights_open    ON flights(ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_flights_bbox    ON flights(bbox_s, bbox_n, bbox_w, bbox_e);
CREATE INDEX IF NOT EXISTS ix_det_flight      ON detections(flight_id, ts);
CREATE INDEX IF NOT EXISTS ix_det_ts          ON detections(ts);
CREATE INDEX IF NOT EXISTS ix_macs_drone      ON drone_macs(drone_id);

-- Guards the migration's idempotency: the same (mac, ts) can only land once.
CREATE UNIQUE INDEX IF NOT EXISTS ux_det_import ON detections(flight_id, ts, lat, lon);
"""

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'flightlog.db')

# Columns added after databases were already in use. CREATE TABLE IF NOT EXISTS
# leaves an existing table exactly as it is, so a deployed database only gains a
# new column through an explicit ALTER - done once, at startup, by _migrate.
ADDED_COLUMNS = (
    ('drones', 'color', 'TEXT'),       # per-drone colour: table swatch and map path
    # FAA registry result, summarized out of faa_json so the table can show and
    # search make/model without parsing a JSON blob per row. services/faa.py.
    ('drones', 'faa_status', 'TEXT'),        # match | no_match | error
    ('drones', 'faa_checked_at', 'REAL'),
    ('drones', 'faa_make', 'TEXT'),
    ('drones', 'faa_model', 'TEXT'),
    # 'DJI' for a drone heard through DJI's proprietary Wi-Fi DroneID; NULL for
    # ASTM Remote ID. Decides "home point" vs "pilot" labels and the FAA skip.
    ('drones', 'id_type', 'TEXT'),
)


class Database:
    def __init__(self, path: str = None):
        # ':memory:' and other sqlite URIs must not be path-normalized.
        self.path = path if path in (':memory:',) else os.path.abspath(path or DEFAULT_DB)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        cur = self._conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- schema ---------------------------------------------------------------
    def _migrate(self):
        with self._lock:
            self._conn.executescript(DDL)
            for table, col, decl in ADDED_COLUMNS:
                have = {r[1] for r in self._conn.execute('PRAGMA table_info(%s)' % table)}
                if col not in have:
                    self._conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),))

    # -- access ---------------------------------------------------------------
    @property
    def conn(self):
        return self._conn

    def write_lock(self):
        return self._lock

    def query(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq):
        with self._lock:
            return self._conn.executemany(sql, seq)

    def close(self):
        try:
            self._conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self._conn.close()
