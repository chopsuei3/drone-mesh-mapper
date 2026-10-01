"""Offline MBTiles serving.

Lifted from mesh-mapper.py (:161-370, :2096-2172). The connection pooling, the
name whitelist and the XYZ->TMS row flip are carried over unchanged - they are
the fiddly parts, and getting the Y flip wrong produces a map that looks right
at z0 and is upside down everywhere else.

This module SERVES tiles; it does not download them. The `tiles/` directory is
shared with the legacy app, so use that app's "Cache This Area" to populate it
and both read the same files.
"""
import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TILES_DIR = os.path.join(BASE_DIR, 'tiles')
STYLES_DIR = os.path.join(BASE_DIR, 'static', 'styles')

_conns = {}
_locks = {}
_pool_lock = threading.Lock()
_NAME_MAX_LEN = 64


def mbtiles_path(name):
    """Resolve a safe path inside TILES_DIR. Rejects traversal-shaped names."""
    if not isinstance(name, str):
        raise ValueError('name must be a string')
    name = name.strip()
    if not name or len(name) > _NAME_MAX_LEN:
        raise ValueError('name must be 1-%d chars' % _NAME_MAX_LEN)
    if not all(c.isalnum() or c in ('-', '_') for c in name):
        raise ValueError('name must be alphanumeric / dash / underscore only')
    return os.path.join(TILES_DIR, name + '.mbtiles')


def get_conn(name):
    """Pooled read connection for one mbtiles file, or None if absent."""
    path = mbtiles_path(name)
    if not os.path.exists(path):
        return None
    with _pool_lock:
        existing = _conns.get(name)
        if existing is not None:
            try:
                existing.execute('SELECT 1').fetchone()
                return existing
            except sqlite3.Error:
                logger.warning('stale mbtiles connection for %s, rebuilding', name)
                try:
                    existing.close()
                except Exception:
                    pass
                _conns.pop(name, None)
                _locks.pop(name, None)
        conn = sqlite3.connect(path, check_same_thread=False,
                               isolation_level=None, timeout=30.0)
        conn.execute('PRAGMA busy_timeout=10000')
        _conns[name] = conn
        _locks[name] = threading.Lock()
        return conn


def list_layers():
    """Discover .mbtiles in TILES_DIR and describe each for the UI."""
    out = []
    if not os.path.isdir(TILES_DIR):
        return out
    for fn in sorted(os.listdir(TILES_DIR)):
        if not fn.endswith('.mbtiles'):
            continue
        name = fn[:-len('.mbtiles')]
        entry = {'name': name, 'format': 'png', 'vector': False,
                 'bytes': 0, 'minzoom': None, 'maxzoom': None, 'bounds': None}
        try:
            entry['bytes'] = os.path.getsize(os.path.join(TILES_DIR, fn))
        except OSError:
            pass
        try:
            conn = get_conn(name)
            if conn is None:
                continue
            meta = {r[0]: r[1] for r in conn.execute('SELECT name, value FROM metadata')}
            fmt = (meta.get('format') or 'png').lower()
            entry['format'] = fmt
            entry['vector'] = fmt in ('pbf', 'mvt')
            for k in ('minzoom', 'maxzoom'):
                if meta.get(k) is not None:
                    try:
                        entry[k] = int(meta[k])
                    except (TypeError, ValueError):
                        pass
            if meta.get('bounds'):
                try:
                    entry['bounds'] = [float(x) for x in meta['bounds'].split(',')]
                except (TypeError, ValueError):
                    pass
        except sqlite3.Error as e:
            logger.debug('could not read metadata for %s: %s', name, e)
        out.append(entry)
    return out


def get_tile(name, z, x, y):
    """Return (blob, mime, gzipped) for one XYZ tile, or (None, None, False)."""
    try:
        conn = get_conn(name)
    except ValueError:
        return None, None, False
    if conn is None:
        return None, None, False
    # MBTiles stores rows in TMS order; XYZ counts Y from the top.
    tms_y = (1 << z) - 1 - y
    row = conn.execute(
        'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1',
        (z, x, tms_y)).fetchone()
    if not row:
        return None, None, False
    blob = row[0]
    meta_fmt = 'png'
    try:
        r = conn.execute("SELECT value FROM metadata WHERE name='format'").fetchone()
        if r:
            meta_fmt = (r[0] or 'png').lower()
    except sqlite3.Error:
        pass
    if meta_fmt in ('pbf', 'mvt'):
        mime = 'application/x-protobuf'
    elif meta_fmt in ('jpg', 'jpeg'):
        mime = 'image/jpeg'
    elif meta_fmt == 'webp':
        mime = 'image/webp'
    else:
        mime = 'image/png'
    # Vector tiles are stored gzipped per the 1.3 spec; the client needs to know.
    gzipped = (meta_fmt in ('pbf', 'mvt') and len(blob) >= 2
               and blob[0] == 0x1f and blob[1] == 0x8b)
    return blob, mime, gzipped


def style_json(name, base_url):
    """MapLibre style for a vector mbtiles, from the bundled default-dark.json."""
    import json
    path = os.path.join(STYLES_DIR, 'default-dark.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as fh:
        style = json.load(fh)
    for src in (style.get('sources') or {}).values():
        if src.get('type') == 'vector':
            src.pop('url', None)
            src['tiles'] = ['%s/tiles/%s/{z}/{x}/{y}.pbf' % (base_url.rstrip('/'), name)]
            src.setdefault('minzoom', 0)
            src.setdefault('maxzoom', 14)
    return style


def close_all():
    with _pool_lock:
        for c in _conns.values():
            try:
                c.close()
            except Exception:
                pass
        _conns.clear()
        _locks.clear()
