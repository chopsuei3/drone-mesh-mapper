"""FAA UAS registry lookup, keyed by RemoteID serial.

The HTTP client is lifted from mesh-mapper.py (:2062-2092, :4135-4155) - the
browser-shaped headers, the cookie warm-up against /listdocs and the retry
policy are all load-bearing against that endpoint.

One thing changed: the legacy cache was keyed `(mac, remote_id)` and searched
with three linear scans per detection (:1822-1832). The registry is per serial,
so the cache is keyed on `basic_id` alone and the result is stored directly on
the drone record.

What it can tell you. The endpoint is the FAA's UAS Declaration of Compliance
database: given a Standard Remote ID serial it returns the make, model, series,
DOC tracking number and compliance category the manufacturer declared. That
identifies the *type* of aircraft, not who owns it - registrant details are not
public, and the FAA shares them only with law enforcement. The search is exact:
a serial prefix matches nothing. FCC data is keyed by FCC ID, which Remote ID
does not broadcast, so there is no FCC equivalent to query.

Lookups run on one worker thread, spaced out, and never on the ingest path. A
drone is looked up the first time it flies; after that only when the last
answer is stale - see `due()`.
"""
import json
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

ENDPOINT = 'https://uasdoc.faa.gov/api/v1/serialNumbers'
HOMEPAGE = 'https://uasdoc.faa.gov/listdocs'
MIN_REFETCH_S = 24 * 3600          # a serial the FAA did not know: ask again after a day
ERROR_RETRY_S = 3600               # a request that failed: try again after an hour
REQUEST_SPACING_S = 2.0            # between requests to the FAA, automatic or not

# ANSI/CTA-2063-A length character: 1-9 then A-F, for 1-15 characters.
_LEN_CODES = '123456789ABCDEF'


class FaaError(Exception):
    pass


def _session(retries=3, backoff_factor=2):
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update({
        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) '
                       'Gecko/20100101 Firefox/137.0'),
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': HOMEPAGE,
        'client': 'external',
    })
    retry = Retry(total=retries, read=retries, connect=retries,
                  backoff_factor=backoff_factor,
                  status_forcelist=(502, 503, 504), raise_on_status=False)
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


# -- reading a response -------------------------------------------------------
def _load(data):
    if isinstance(data, (str, bytes)):
        try:
            return json.loads(data)
        except ValueError:
            return None
    return data


def classify(data):
    """'match', 'no_match' or 'error' for an FAA response (dict or JSON text)."""
    data = _load(data)
    if not isinstance(data, dict):
        return 'error'
    err = data.get('error')
    if isinstance(err, dict) and (err.get('code') or err.get('message')):
        return 'error'
    try:
        items = data['data']['items']
    except (KeyError, TypeError):
        return 'error'
    if not isinstance(items, list):
        return 'error'
    return 'match' if items else 'no_match'


def first_item(data):
    """The first registry record in an FAA response, or None."""
    data = _load(data)
    try:
        items = data['data']['items']
    except (KeyError, TypeError):
        return None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _text(v):
    """A registry field as display text. The shapes are not documented, so
    lists and objects are flattened rather than trusted to be strings."""
    if isinstance(v, dict):
        v = next((v[k] for k in ('name', 'label', 'value', 'description') if v.get(k)), None)
    if isinstance(v, (list, tuple)):
        v = ', '.join(p for p in (_text(x) for x in v) if p)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def summarize(item):
    """The fields worth showing from one registry record - the same six the
    legacy UI displayed, which is what they were proven against."""
    item = item or {}
    return {
        'make': _text(item.get('makeName')),
        'model': _text(item.get('modelName')),
        'series': _text(item.get('series')),
        'tracking_number': _text(item.get('trackingNumber')),
        'compliance': _text(item.get('complianceCategories')),
        'updated_at': _text(item.get('updatedAt')),
    }


def faa_summary(raw, row):
    """What the drone page shows, from the stored blob plus the drone row."""
    status = row.get('faa_status') or ('unchecked' if raw is None else classify(raw))
    out = {'status': status, 'checked_at': row.get('faa_checked_at')}
    item = first_item(raw) if raw is not None else None
    if item:
        out.update(summarize(item))
    return out


def serial_format(basic_id):
    """What an ANSI/CTA-2063-A serial says about itself, with no network.

    Four characters of manufacturer code, one length character (1-9, A-F for
    1-15), then the manufacturer's own serial of exactly that length. A Remote
    ID that does not fit is more likely a session ID or a registration number.
    """
    s = (basic_id or '').strip().upper()
    out = {'well_formed': False, 'mfr_code': None, 'length_code': None,
           'expected_len': None, 'serial': None}
    if len(s) < 6 or not s.isalnum():
        return out
    idx = _LEN_CODES.find(s[4])
    out.update(mfr_code=s[:4], length_code=s[4], serial=s[5:])
    if idx >= 0:
        out['expected_len'] = idx + 1
        out['well_formed'] = len(s) - 5 == idx + 1
    return out


def due(row, now):
    """Whether a drone's registry answer is worth asking for again.

    A match is final - declarations do not change often enough to re-ask, and
    the page's button forces a refresh. A serial the FAA did not know is
    retried daily (a manufacturer may file it later); a failed request hourly.
    """
    status, checked = row['faa_status'], row['faa_checked_at']
    if status == 'match':
        return False
    if status is None or checked is None:
        return True
    wait = MIN_REFETCH_S if status == 'no_match' else ERROR_RETRY_S
    return now - checked >= wait


class FaaLookup:
    def __init__(self, db, spacing_s=REQUEST_SPACING_S, clock=time.time):
        self.db = db
        self._lock = threading.Lock()            # one request to the FAA at a time
        self._sess = None
        self._warmed = False
        self._spacing = spacing_s
        self._last_request = 0.0
        self._clock = clock
        self._q = queue.Queue()
        self._pending = set()
        self._plock = threading.Lock()
        self._thread = None
        self._backfill()

    def _backfill(self):
        """Summarize registry blobs stored before the summary columns existed.

        The legacy faa_cache.csv import wrote raw responses into faa_json. Their
        check time is unknown, so it stays NULL: a match is kept as final, and
        anything else is simply looked up again the next time that drone flies.
        """
        rows = self.db.query("SELECT id, faa_json FROM drones"
                             " WHERE faa_json IS NOT NULL AND faa_status IS NULL")
        for r in rows:
            status = classify(r['faa_json'])
            s = summarize(first_item(r['faa_json'])) if status == 'match' else {}
            self.db.execute("UPDATE drones SET faa_status=?, faa_make=?, faa_model=? WHERE id=?",
                            (status, s.get('make'), s.get('model'), r['id']))

    # -- HTTP -------------------------------------------------------------------
    def _ensure_session(self):
        if self._sess is None:
            self._sess = _session()
        if not self._warmed:
            try:
                self._sess.get(HOMEPAGE, timeout=30)
                self._warmed = True
            except Exception as e:
                logger.debug('FAA cookie warm-up failed: %s', e)
        return self._sess

    def _fetch(self, basic_id):
        """One registry request. Returns the parsed response or raises FaaError."""
        params = {
            'itemsPerPage': 8, 'pageIndex': 0,
            'orderBy[0]': 'updatedAt', 'orderBy[1]': 'DESC',
            'findBy': 'serialNumber', 'serialNumber': basic_id,
        }
        try:
            sess = self._ensure_session()
            resp = sess.get(ENDPOINT, params=params, timeout=30)
            if resp.status_code != 200:
                # A stale cookie shows up as a non-200; warm up once and retry.
                self._warmed = False
                self._ensure_session()
                resp = sess.get(ENDPOINT, params=params, timeout=30)
        except Exception as e:
            raise FaaError(str(e))
        if resp.status_code != 200:
            raise FaaError('HTTP %s' % resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise FaaError('bad JSON from FAA')

    # -- lookups ----------------------------------------------------------------
    def _row(self, basic_id):
        return self.db.one("SELECT id, faa_json, faa_status, faa_checked_at FROM drones"
                           " WHERE basic_id=?", (basic_id,))

    def lookup(self, basic_id, force=False):
        """Return registry data for a serial, using the stored copy while it is fresh."""
        if not basic_id:
            return {'error': 'no serial'}
        if not force:
            row = self._row(basic_id)
            if row is not None and row['faa_json'] and not due(row, self._clock()):
                data = _load(row['faa_json'])
                if data is not None:
                    return {'cached': True, 'data': data}

        with self._lock:
            wait = self._last_request + self._spacing - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                data = self._fetch(basic_id)
                err = None
            except FaaError as e:
                data, err = None, str(e)
            finally:
                self._last_request = time.monotonic()

        now = self._clock()
        status = classify(data) if err is None else 'error'
        if status == 'error':
            logger.warning('FAA lookup failed for %s: %s', basic_id, err or 'error response')
            # A failure never downgrades a match already on file.
            self.db.execute(
                "UPDATE drones SET faa_status='error', faa_checked_at=?"
                " WHERE basic_id=? AND COALESCE(faa_status,'') <> 'match'", (now, basic_id))
            return {'error': err or 'FAA returned an error'}
        s = summarize(first_item(data)) if status == 'match' else {}
        self.db.execute(
            "UPDATE drones SET faa_json=?, faa_status=?, faa_checked_at=?, faa_make=?, faa_model=?"
            " WHERE basic_id=?",
            (json.dumps(data), status, now, s.get('make'), s.get('model'), basic_id))
        return {'cached': False, 'data': data}

    # -- automatic lookups ------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name='faa-lookup')
            self._thread.start()

    def request(self, drone_id):
        """Queue a lookup for a drone if one is due. Called on the ingest path:
        constant time, never blocks. The due check itself runs on the worker."""
        if self._thread is None:
            return False
        with self._plock:
            if drone_id in self._pending:
                return False
            self._pending.add(drone_id)
        self._q.put(drone_id)
        return True

    def sweep(self):
        """Queue every drone with a serial that has never been looked up - the
        ones already in the database when automatic lookups arrived, or seen
        while they were switched off. Newest first; spaced like any other
        lookup, so a large backlog trickles out rather than bursting."""
        rows = self.db.query("SELECT id FROM drones WHERE basic_id IS NOT NULL"
                             " AND faa_status IS NULL AND COALESCE(id_type,'') <> 'DJI'"
                             " ORDER BY last_seen DESC")
        return sum(1 for r in rows if self.request(r['id']))

    def pending(self, drone_id):
        """True while a lookup for this drone is queued or running."""
        with self._plock:
            return drone_id in self._pending

    def awaiting(self, drone_id):
        """True while a lookup that will actually ask the FAA is queued or running
        for this drone - what an alert should wait for. A drone that is already
        identified is queued only to have that confirmed; waiting on it would let
        one slow request elsewhere delay alerts for drones that need nothing."""
        if not self.pending(drone_id):
            return False
        row = self.db.one("SELECT basic_id, faa_status, faa_checked_at, id_type FROM drones"
                          " WHERE id=?", (drone_id,))
        return bool(row and row['basic_id'] and row['id_type'] != 'DJI'
                    and due(row, self._clock()))

    def _run(self):
        while True:
            drone_id = self._q.get()
            try:
                self.auto(drone_id)
            except Exception as e:                    # never let the worker die
                logger.warning('FAA auto-lookup crashed for drone %s: %s', drone_id, e)
            finally:
                with self._plock:
                    self._pending.discard(drone_id)

    def auto(self, drone_id):
        """Look a drone up if it has a serial and its last answer is stale."""
        row = self.db.one("SELECT basic_id, faa_status, faa_checked_at, id_type FROM drones"
                          " WHERE id=?", (drone_id,))
        # A DJI DroneID serial is DJI's own, not a Remote ID serial: the FAA's
        # declaration database cannot match it, so don't ask.
        if (row is None or not row['basic_id'] or row['id_type'] == 'DJI'
                or not due(row, self._clock())):
            return None
        return self.lookup(row['basic_id'], force=True)
