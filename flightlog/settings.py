"""Runtime settings, changed over the API rather than by editing files on the Pi.

Stored as JSON values in the existing `meta` table, under namespaced keys, so
they survive restarts and travel with the database. Every key has a default,
so an untouched install behaves sensibly and a missing row is never an error.
"""
import json
import re
import threading

DEFAULTS = {
    'faa.auto': True,              # look each new serial up in the FAA registry
    'notify.enabled': True,        # master switch; nothing sends until a channel exists
    'notify.base_url': '',         # e.g. http://raspberrypi.local:5001, for links in alerts
    'notify.settle_s': 15.0,       # wait up to this long for a GPS fix before alerting
}

_URL = re.compile(r'https?://[^\s/]+(:\d+)?(/\S*)?')


def _bool(key, v):
    if not isinstance(v, bool):
        raise ValueError('%s must be true or false' % key)
    return v


def _base_url(key, v):
    v = (v or '').strip()
    if v and not _URL.fullmatch(v):
        raise ValueError('%s must be empty or an http(s):// URL' % key)
    return v.rstrip('/')


def _settle(key, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 120:
        raise ValueError('%s must be a number of seconds from 0 to 120' % key)
    return float(v)


VALIDATORS = {
    'faa.auto': _bool,
    'notify.enabled': _bool,
    'notify.base_url': _base_url,
    'notify.settle_s': _settle,
}


class Settings:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        rows = db.query("SELECT key, value FROM meta WHERE key IN (%s)"
                        % ','.join('?' * len(DEFAULTS)), list(DEFAULTS))
        for r in rows:
            try:
                self._values[r['key']] = VALIDATORS[r['key']](r['key'], json.loads(r['value']))
            except (ValueError, TypeError):
                pass                       # a bad stored value falls back to the default

    def get(self, key):
        with self._lock:
            return self._values[key]

    def all(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        """Validate every change first, then apply them all - a bad value never
        half-applies a request. Unknown keys are refused rather than ignored, so
        a typo is reported instead of silently doing nothing."""
        if not isinstance(changes, dict):
            raise ValueError('expected a JSON object')
        unknown = sorted(set(changes) - set(DEFAULTS))
        if unknown:
            raise ValueError('unknown setting(s): %s' % ', '.join(unknown))
        clean = {k: VALIDATORS[k](k, v) for k, v in changes.items()}
        with self._lock:
            for k, v in clean.items():
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                                (k, json.dumps(v)))
                self._values[k] = v
            return dict(self._values)
