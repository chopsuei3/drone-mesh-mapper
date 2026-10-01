"""Push alerts when a drone takes off: Discord webhooks and Pushbullet.

The trigger is a flight opening - a drone the node has just started hearing. A
flight that resumes after an RF dropout is the same flight continuing, so it
never alerts.

How it stays out of the way of ingest. The sessionizer calls `flight_opened`
and `flight_point` from its own thread, under its lock; both only touch an
in-memory dict and a heap, and never block or raise. One worker thread does
everything slow - reading the database, building the message, HTTP - when each
alert falls due.

When an alert falls due. A drone's first packets often carry no position yet,
and an alert without one is much less useful. So an alert is scheduled
`notify.settle_s` after takeoff, and the flight's first GPS fix brings it
forward to that moment: it goes out as soon as there is a position, or without
one if none arrives. A fix often comes with the very first packet, though, which
would beat the FAA lookup started by the same takeoff - so an alert that falls
due while that lookup is still running waits for it, but never past the settle
deadline. A new drone's first alert then carries its make and model.

Who gets it. Each channel has a filter - any drone (minus exclusions), or only
listed drones, groups and tags - evaluated when the alert is sent, so moving a
drone between groups takes effect at once. A per-channel, per-drone cooldown
stops a drone that relaunches, or splits flights at the edge of range, from
alerting again straight away.

Where it can go. Discord URLs must be Discord webhook URLs and the Pushbullet
endpoint is fixed, so unlike a generic webhook a channel cannot be pointed at an
arbitrary host. Secrets are masked whenever a channel is read back.

What it is told about. Each channel lists its `events`: 'takeoff' (the default)
and/or 'nodes' - a receiver going offline, its XIAO falling silent or
crashing, and coming back (nodes.py decides when; this module only delivers).
Node alerts ignore the drone filter, and repeat for the same node and kind at
most once per NODE_COOLDOWN_S per channel, so a flapping link does not flood.
"""
import collections
import heapq
import itertools
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from ..colors import display_color, to_int

logger = logging.getLogger(__name__)

TYPES = ('discord', 'pushbullet')
SECRET_KEY = {'discord': 'webhook_url', 'pushbullet': 'access_token'}
CONFIG_KEYS = {'discord': ('webhook_url', 'username', 'mention'),
               'pushbullet': ('access_token', 'device_iden', 'channel_tag')}
PUSHBULLET_API = 'https://api.pushbullet.com/v2/pushes'
DEFAULT_COOLDOWN_S = 900
MAX_COOLDOWN_S = 7 * 24 * 3600
HTTP_TIMEOUT_S = 10
HOLD_RECHECK_S = 0.25              # how often a held alert looks again
MAX_RETRY_AFTER_S = 30             # Discord 429: wait this long at most, once
LOG_KEEP = 1000
EVENTS = ('takeoff', 'nodes')
NODE_COOLDOWN_S = 600              # same node, same kind of alert, same channel

DISCORD_URL = re.compile(
    r'https://(?:(?:ptb|canary)\.)?(?:discord\.com|discordapp\.com)'
    r'/api/(?:v\d+/)?webhooks/\d{5,25}/[A-Za-z0-9_-]{20,120}(?:\?thread_id=\d{5,25})?')
MENTION = re.compile(r'@here|@everyone|<@&\d{5,25}>|<@!?\d{5,25}>')
PB_TOKEN = re.compile(r'[A-Za-z0-9._-]{10,200}')
PB_IDEN = re.compile(r'[A-Za-z0-9]{5,64}')
PB_TAG = re.compile(r'[A-Za-z0-9_-]{1,64}')

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_channels (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  type       TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  config     TEXT NOT NULL,
  filter     TEXT NOT NULL,
  cooldown_s REAL NOT NULL DEFAULT 900,
  created_at REAL);
CREATE TABLE IF NOT EXISTS notify_log (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  channel_id INTEGER,
  drone_id   INTEGER,
  flight_id  INTEGER,
  status     TEXT NOT NULL,
  detail     TEXT);
CREATE INDEX IF NOT EXISTS ix_notify_log_cd ON notify_log(channel_id, drone_id, ts);
-- Cooldown state, kept apart from the log: the log is trimmed to LOG_KEEP rows,
-- and a busy site could otherwise trim away the very row a cooldown depends on.
CREATE TABLE IF NOT EXISTS notify_last (
  channel_id INTEGER NOT NULL,
  drone_id   INTEGER NOT NULL,
  ts         REAL NOT NULL,
  PRIMARY KEY (channel_id, drone_id));
"""


# -- validation ----------------------------------------------------------------
def _ids(v, key):
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, int) and not isinstance(x, bool)
                                          and x > 0 for x in v):
        raise ValueError('filter.%s must be a list of ids' % key)
    return sorted(set(v))


def validate_filter(f):
    """Canonicalize a filter. 'any' keeps only its exclusions, 'only' only its
    inclusions, so a stored filter never carries lists that do nothing."""
    f = f if f is not None else {'mode': 'any'}
    if not isinstance(f, dict):
        raise ValueError('filter must be an object')
    mode = f.get('mode', 'any')
    if mode == 'any':
        return {'mode': 'any', 'exclude_drone_ids': _ids(f.get('exclude_drone_ids'),
                                                          'exclude_drone_ids')}
    if mode != 'only':
        raise ValueError('filter.mode must be "any" or "only"')
    tags = f.get('tags') or []
    if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
        raise ValueError('filter.tags must be a list of tag names')
    out = {'mode': 'only',
           'drone_ids': _ids(f.get('drone_ids'), 'drone_ids'),
           'group_ids': _ids(f.get('group_ids'), 'group_ids'),
           'tags': sorted({t.strip() for t in tags})}
    if not (out['drone_ids'] or out['group_ids'] or out['tags']):
        raise ValueError('filter mode "only" needs at least one drone, group or tag')
    return out


def _mask(ctype, value):
    """Enough of a secret to tell two apart, never enough to use."""
    value = str(value or '')
    if not value:
        return ''
    if ctype == 'discord':
        head, _, token = value.split('?')[0].rpartition('/')
        return head + '/…' + token[-4:]
    return '…' + value[-4:]


def _validate_config(ctype, given, old):
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ValueError('config must be an object')
    unknown = sorted(set(given) - set(CONFIG_KEYS[ctype]))
    if unknown:
        raise ValueError('unknown config key(s) for %s: %s' % (ctype, ', '.join(unknown)))
    cfg = dict(old)
    secret = SECRET_KEY[ctype]
    for k, v in given.items():
        v = '' if v is None else v
        if not isinstance(v, str):
            raise ValueError('config.%s must be a string' % k)
        v = v.strip()
        # Reading a channel back and PATCHing the same object must not replace
        # the stored secret with its own mask.
        if k == secret and old.get(k) and v == _mask(ctype, old[k]):
            continue
        cfg[k] = v
    if not cfg.get(secret):
        raise ValueError('config.%s is required' % secret)

    if ctype == 'discord':
        if not DISCORD_URL.fullmatch(cfg['webhook_url']):
            raise ValueError('config.webhook_url must be a Discord webhook URL '
                             '(https://discord.com/api/webhooks/<id>/<token>)')
        if len(cfg.get('username') or '') > 80:
            raise ValueError('config.username must be 80 characters or fewer')
        if cfg.get('mention') and not MENTION.fullmatch(cfg['mention']):
            raise ValueError('config.mention must be @here, @everyone, <@&role_id> or <@user_id>')
    else:
        if not PB_TOKEN.fullmatch(cfg['access_token']):
            raise ValueError('config.access_token does not look like a Pushbullet access token')
        if cfg.get('device_iden') and not PB_IDEN.fullmatch(cfg['device_iden']):
            raise ValueError('config.device_iden must be a Pushbullet device iden')
        if cfg.get('channel_tag') and not PB_TAG.fullmatch(cfg['channel_tag']):
            raise ValueError('config.channel_tag must be a Pushbullet channel tag')
        if cfg.get('device_iden') and cfg.get('channel_tag'):
            raise ValueError('set device_iden or channel_tag, not both')
    return {k: v for k, v in cfg.items() if v}


def validate(data, existing=None):
    """Canonicalize a channel. Raises ValueError.

    On an update, `existing` supplies every field the request leaves out -
    including the stored secret, so a PATCH never has to resend it.
    """
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object')
    base = existing or {}
    ctype = data.get('type', base.get('type'))
    if ctype not in TYPES:
        raise ValueError('type must be "discord" or "pushbullet"')
    if existing and ctype != existing['type']:
        raise ValueError('type cannot be changed; create a new channel instead')
    name = data.get('name', base.get('name'))
    name = (name if isinstance(name, str) else '').strip()[:80] or ctype
    enabled = data.get('enabled', base.get('enabled', True))
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be true or false')
    cooldown = data.get('cooldown_s', base.get('cooldown_s', DEFAULT_COOLDOWN_S))
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) \
            or not 0 <= cooldown <= MAX_COOLDOWN_S:
        raise ValueError('cooldown_s must be a number of seconds from 0 to %d' % MAX_COOLDOWN_S)
    events = data.get('events', base.get('events', ['takeoff']))
    if not isinstance(events, list) or not events or not all(e in EVENTS for e in events):
        raise ValueError('events must be a non-empty list of: %s' % ', '.join(EVENTS))
    return {
        'name': name, 'type': ctype, 'enabled': enabled, 'cooldown_s': float(cooldown),
        'config': _validate_config(ctype, data.get('config'), base.get('config') or {}),
        'filter': validate_filter(data.get('filter', base.get('filter'))),
        'events': [e for e in EVENTS if e in events],
    }


def matches(filt, drone):
    """Whether a channel's filter selects this drone (a dict: id, group_id, tag)."""
    if filt.get('mode') == 'only':
        return (drone['id'] in filt.get('drone_ids', ())
                or (drone.get('group_id') is not None
                    and drone['group_id'] in filt.get('group_ids', ()))
                # untagged drones show as "unknown" everywhere else in the app
                or (drone.get('tag') or 'unknown') in filt.get('tags', ()))
    return drone['id'] not in filt.get('exclude_drone_ids', ())


# -- message building --------------------------------------------------------------
def _maps(lat, lon):
    if lat is None or lon is None:
        return None
    return 'https://maps.google.com/?q=%.6f,%.6f' % (lat, lon)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def compose(drone, flight, base_url=''):
    """A neutral description of one alert, rendered per service below.

    `drone` is the drone row (with group name/colour); `flight` holds the
    positions and signal known when the alert fell due.
    """
    name = (drone.get('label') or drone.get('basic_id') or flight.get('mac')
            or 'drone #%s' % drone['id'])
    make_model = ' '.join(x for x in (drone.get('faa_make'), drone.get('faa_model')) if x)
    prior = flight.get('prior_flights') or 0
    return {
        'title': 'Drone airborne: %s' % name,
        'description': None,
        'serial': drone.get('basic_id'),
        'make_model': make_model or None,
        'group': drone.get('group_name'),
        'tag': drone.get('tag'),
        'history': ('first time seen' if not prior else
                    'seen on %d earlier flight%s' % (prior, '' if prior == 1 else 's')),
        'lat': flight.get('lat'), 'lon': flight.get('lon'), 'alt': flight.get('alt'),
        'pilot_lat': flight.get('pilot_lat'), 'pilot_lon': flight.get('pilot_lon'),
        'rssi': flight.get('rssi'), 'band': flight.get('band'), 'channel': flight.get('channel'),
        'started_at': flight.get('started_at'),
        'color': to_int(display_color(drone.get('color'), drone.get('group_color'), drone['id']),
                        drone['id']),
        'map_url': _maps(flight.get('lat'), flight.get('lon')),
        'pilot_url': _maps(flight.get('pilot_lat'), flight.get('pilot_lon')),
        # DJI DroneID gives the takeoff point, not a live operator fix.
        'pilot_label': 'Home point' if drone.get('id_type') == 'DJI' else 'Pilot',
        'via': 'DJI DroneID' if drone.get('id_type') == 'DJI' else None,
        'app_url': ('%s/drone/%s' % (base_url, drone['id'])) if base_url else None,
    }


def _span(s):
    s = max(0, int(s or 0))
    if s < 90:
        return '%d s' % s
    if s < 5400:
        return '%d min' % round(s / 60.0)
    if s < 172800:
        return '%.1f h' % (s / 3600.0)
    return '%d days' % round(s / 86400.0)


NODE_COLORS = {'relay_offline': 0xFF6B7A, 'xiao_silent': 0xFFB454, 'restarted': 0xFF6B7A,
               'recovered': 0x6EE7A8}


def compose_node(kind, node, detail, base_url='', now=None):
    """A node alert, in the same shape compose() returns, so both services
    render it with their usual payload builders."""
    now = now or time.time()
    name = node.get('name') or 'node #%s' % node.get('id')
    where = ' (%s)' % node['notes'] if node.get('notes') else ''
    if kind == 'relay_offline':
        title = 'Node offline: %s' % name
        desc = ('No contact from %s%s for %s. Whatever its XIAO hears is kept on the relay'
                ' and arrives when the link is back.'
                % (name, where, _span(now - (node.get('last_contact_at') or now))))
    elif kind == 'xiao_silent':
        title = 'XIAO silent: %s' % name
        desc = ('%s%s has printed nothing for %s, though %s. Check its USB cable and power;'
                ' a wedged XIAO needs unplugging.'
                % (name, where, _span(now - (node.get('last_line_at') or now)),
                   'its relay is checking in' if node.get('kind') == 'relay' else 'this server is up'))
    elif kind == 'restarted':
        d = detail or {}
        title = 'XIAO restarted: %s' % name
        desc = ('The XIAO on %s%s%s restarted after a %s, about %s ago. "panic" or a'
                ' watchdog reset is a firmware crash; "brownout" is a power dip.'
                % (name, where, (' (%s)' % d['port'].replace('/dev/', '')) if d.get('port') else '',
                   d.get('reset') or 'crash', _span(now - (d.get('at') or now))))
    else:
        title = 'Node back: %s' % name
        desc = '%s%s is online again.' % (name, where)
    return {'title': title, 'description': desc, 'color': NODE_COLORS.get(kind, 0x4FC3F7),
            'started_at': now,
            'app_url': ('%s/nodes#%s' % (base_url, node.get('id'))) if base_url else None}


def _signal(a):
    parts = []
    if a.get('rssi') is not None:
        parts.append('%s dBm' % a['rssi'])
    if a.get('band'):
        parts.append(a['band'] + (' ch %s' % a['channel'] if a.get('channel') else ''))
    return ' · '.join(parts) or None


def _position(a):
    if a.get('map_url') is None:
        return None
    s = '%.5f, %.5f' % (a['lat'], a['lon'])
    return s + (' at %d m MSL' % a['alt'] if a.get('alt') is not None else '')


def discord_payload(a, cfg):
    fields = []

    def add(name, value, inline=True):
        if value:
            fields.append({'name': name, 'value': str(value)[:1024], 'inline': inline})

    add('Serial', a.get('serial') and '`%s`' % a['serial'])
    add('Heard via', a.get('via'))
    add('Make / model', a.get('make_model'))
    add('Group', a.get('group'))
    add('Tag', a.get('tag'))
    add('History', a.get('history'))
    add('Signal', _signal(a))
    pos = _position(a)
    if pos:
        add('Drone', '[%s](%s)' % (pos, a['map_url']), inline=False)
    elif a.get('history'):
        add('Drone', 'no GPS fix yet', inline=False)
    if a.get('pilot_url'):
        add(a.get('pilot_label') or 'Pilot',
            '[%.5f, %.5f](%s)' % (a['pilot_lat'], a['pilot_lon'], a['pilot_url']), inline=False)
    embed = {'title': a['title'][:256], 'color': a.get('color') or 0, 'fields': fields}
    if a.get('description'):
        embed['description'] = a['description'][:4096]
    if a.get('app_url'):
        embed['url'] = a['app_url']
    if a.get('started_at'):
        embed['timestamp'] = _iso(a['started_at'])
    body = {'embeds': [embed], 'allowed_mentions': {'parse': []}}
    mention = cfg.get('mention')
    if mention:
        body['content'] = mention
        if mention in ('@here', '@everyone'):
            body['allowed_mentions'] = {'parse': ['everyone']}
        elif mention.startswith('<@&'):
            body['allowed_mentions'] = {'parse': [], 'roles': [mention[3:-1]]}
        else:
            body['allowed_mentions'] = {'parse': [], 'users': [mention.strip('<@!>')]}
    if cfg.get('username'):
        body['username'] = cfg['username']
    return body


def pushbullet_payload(a, cfg):
    lines = []
    if a.get('description'):
        lines.append(a['description'])
    for label, key in (('Serial', 'serial'), ('Model', 'make_model'),
                       ('Group', 'group'), ('Tag', 'tag')):
        if a.get(key):
            lines.append('%s: %s' % (label, a[key]))
    if a.get('history'):
        lines.append(a['history'][0].upper() + a['history'][1:])
    pos = _position(a)
    if pos:
        lines.append('Drone: %s' % pos)
    elif a.get('history'):
        lines.append('Drone: no GPS fix yet')
    if a.get('pilot_url'):
        lines.append('%s: %s' % (a.get('pilot_label') or 'Pilot', a['pilot_url']))
    if a.get('via'):
        lines.append('Heard via %s' % a['via'])
    sig = _signal(a)
    if sig:
        lines.append('Signal: %s' % sig)
    if a.get('app_url') and a.get('map_url'):
        lines.append(a['app_url'])
    url = a.get('map_url') or a.get('app_url')
    body = {'type': 'link' if url else 'note', 'title': a['title'], 'body': '\n'.join(lines)}
    if url:
        body['url'] = url
    if cfg.get('device_iden'):
        body['device_iden'] = cfg['device_iden']
    if cfg.get('channel_tag'):
        body['channel_tag'] = cfg['channel_tag']
    return body


def _error_text(resp):
    """The service's own explanation of a failure, never the request's secrets."""
    try:
        j = resp.json()
    except ValueError:
        return 'HTTP %s' % resp.status_code
    msg = None
    if isinstance(j, dict):
        err = j.get('error')
        msg = (err.get('message') if isinstance(err, dict) else None) or j.get('message')
    return 'HTTP %s%s' % (resp.status_code, ': %s' % msg if msg else '')


# -- the service ------------------------------------------------------------------
class Notifier:
    def __init__(self, db, settings, sess=None, http=None, hold=None):
        self.db = db
        self.settings = settings
        self.sess = sess
        self._http = http
        # hold(drone_id) -> True while something worth waiting for is still on
        # its way (the FAA lookup). Checked on the worker only.
        self._hold = hold
        self._cv = threading.Condition()
        self._jobs = {}                 # flight_id -> pending alert
        self._heap = []                 # (due, seq, flight_id)
        self._seq = itertools.count()
        self._stop = threading.Event()
        self._thread = None
        self._node_q = collections.deque(maxlen=200)    # node alerts waiting for the worker
        self._node_last = {}            # (channel, node, kind) -> when it last went out
        with db.write_lock():
            db.conn.executescript(SCHEMA)
            # Columns added with node alerts; a channel from before them has
            # events NULL, which means takeoff alerts only, as it always did.
            for table, col, decl in (('notify_channels', 'events', 'TEXT'),
                                     ('notify_log', 'event', 'TEXT'),
                                     ('notify_log', 'node_id', 'INTEGER')):
                have = {r[1] for r in db.conn.execute('PRAGMA table_info(%s)' % table)}
                if col not in have:
                    db.conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))

    # -- channels -----------------------------------------------------------------
    @staticmethod
    def _decode(r):
        try:
            events = json.loads(r['events']) if r['events'] else ['takeoff']
        except ValueError:
            events = ['takeoff']
        return {'id': r['id'], 'name': r['name'], 'type': r['type'],
                'enabled': bool(r['enabled']), 'cooldown_s': r['cooldown_s'],
                'config': json.loads(r['config']), 'filter': json.loads(r['filter']),
                'events': events, 'created_at': r['created_at']}

    def _get(self, cid):
        r = self.db.one("SELECT * FROM notify_channels WHERE id=?", (cid,))
        return self._decode(r) if r else None

    @staticmethod
    def public(ch):
        """A channel as the API shows it: secrets masked."""
        out = dict(ch)
        cfg = dict(ch['config'])
        key = SECRET_KEY[ch['type']]
        out['has_secret'] = bool(cfg.get(key))
        if cfg.get(key):
            cfg[key] = _mask(ch['type'], cfg[key])
        out['config'] = cfg
        return out

    def list(self):
        rows = self.db.query("SELECT * FROM notify_channels ORDER BY id")
        last = {r['channel_id']: dict(r) for r in self.db.query(
            "SELECT l.channel_id, l.ts, l.status, l.detail FROM notify_log l"
            " JOIN (SELECT channel_id, MAX(id) AS id FROM notify_log"
            "       WHERE status <> 'suppressed' GROUP BY channel_id) m ON m.id = l.id")}
        out = []
        for r in rows:
            ch = self.public(self._decode(r))
            ch['last'] = last.get(ch['id'])
            out.append(ch)
        return out

    def get(self, cid):
        ch = self._get(cid)
        return self.public(ch) if ch else None

    def create(self, data):
        c = validate(data)
        cur = self.db.execute(
            "INSERT INTO notify_channels(name, type, enabled, config, filter, cooldown_s,"
            " events, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (c['name'], c['type'], int(c['enabled']), json.dumps(c['config']),
             json.dumps(c['filter']), c['cooldown_s'], json.dumps(c['events']), time.time()))
        return self.get(cur.lastrowid)

    def update(self, cid, data):
        old = self._get(cid)
        if old is None:
            return None
        c = validate(data, existing=old)
        self.db.execute(
            "UPDATE notify_channels SET name=?, enabled=?, config=?, filter=?, cooldown_s=?,"
            " events=? WHERE id=?",
            (c['name'], int(c['enabled']), json.dumps(c['config']), json.dumps(c['filter']),
             c['cooldown_s'], json.dumps(c['events']), cid))
        return self.get(cid)

    def delete(self, cid):
        self.db.execute("DELETE FROM notify_channels WHERE id=?", (cid,))
        self.db.execute("DELETE FROM notify_log WHERE channel_id=?", (cid,))
        self.db.execute("DELETE FROM notify_last WHERE channel_id=?", (cid,))

    def log(self, limit=100):
        try:
            limit = max(1, min(int(limit), LOG_KEEP))
        except (TypeError, ValueError):
            limit = 100
        rows = self.db.query(
            "SELECT l.*, c.name AS channel_name, c.type AS channel_type,"
            " d.label AS drone_label, d.basic_id, n.name AS node_name"
            " FROM notify_log l"
            " LEFT JOIN notify_channels c ON c.id = l.channel_id"
            " LEFT JOIN drones d ON d.id = l.drone_id"
            " LEFT JOIN nodes n ON n.id = l.node_id"
            " ORDER BY l.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def _log(self, cid, drone_id, flight_id, status, detail, event=None, node_id=None):
        """Record a delivery; a real 'sent' also starts that drone's cooldown.
        One transaction, so no reader ever sees the log over its cap."""
        now = time.time()
        with self.db.write_lock():
            conn = self.db.conn
            conn.execute('BEGIN')
            try:
                conn.execute(
                    "INSERT INTO notify_log(ts, channel_id, drone_id, flight_id, status, detail,"
                    " event, node_id) VALUES(?,?,?,?,?,?,?,?)",
                    (now, cid, drone_id, flight_id, status, detail, event, node_id))
                if status == 'sent' and cid is not None and drone_id is not None:
                    conn.execute("INSERT OR REPLACE INTO notify_last(channel_id, drone_id, ts)"
                                 " VALUES(?,?,?)", (cid, drone_id, now))
                conn.execute(
                    "DELETE FROM notify_log WHERE id <= (SELECT id FROM notify_log"
                    " ORDER BY id DESC LIMIT 1 OFFSET ?)", (LOG_KEEP,))
                conn.execute('COMMIT')
            except Exception:
                conn.execute('ROLLBACK')
                raise

    # -- sending ------------------------------------------------------------------
    def _client(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    def send(self, ch, alert):
        """Deliver one alert to one channel. Returns (ok, detail); never raises."""
        cfg = ch['config']
        try:
            http = self._client()
            if ch['type'] == 'discord':
                body = discord_payload(alert, cfg)
                resp = http.post(cfg['webhook_url'], json=body, timeout=HTTP_TIMEOUT_S)
                if resp.status_code == 429:
                    try:
                        wait = float(resp.json().get('retry_after', 1))
                    except (ValueError, AttributeError, TypeError):
                        wait = 1.0
                    time.sleep(min(max(wait, 0.0), MAX_RETRY_AFTER_S))
                    resp = http.post(cfg['webhook_url'], json=body, timeout=HTTP_TIMEOUT_S)
                ok = resp.status_code in (200, 204)
            else:
                body = pushbullet_payload(alert, cfg)
                resp = http.post(PUSHBULLET_API, json=body, timeout=HTTP_TIMEOUT_S,
                                 headers={'Access-Token': cfg['access_token']})
                ok = resp.status_code == 200
        except Exception as e:
            # requests puts the URL in some messages, and a Discord URL is the
            # secret - so report the kind of failure, not the text.
            return False, type(e).__name__
        return ok, ('HTTP %s' % resp.status_code) if ok else _error_text(resp)

    def test(self, cid):
        ch = self._get(cid)
        if ch is None:
            return None
        base = self.settings.get('notify.base_url')
        alert = {
            'title': 'flightlog test alert',
            'description': 'If you can read this, %s alerts from flightlog are working.'
                           % ('Discord' if ch['type'] == 'discord' else 'Pushbullet'),
            'color': 0x4FC3F7, 'app_url': (base + '/') if base else None,
            'started_at': time.time(),
        }
        ok, detail = self.send(ch, alert)
        self._log(cid, None, None, 'sent' if ok else 'failed', 'test: ' + detail)
        return {'ok': ok, 'detail': detail}

    # -- ingest hooks (sessionizer thread: cheap, never block, never raise) ---------------
    def flight_opened(self, p):
        if self._thread is None or p.get('resumed') or not self.settings.get('notify.enabled'):
            return
        due = time.time() + self.settings.get('notify.settle_s')
        job = {'flight_id': p['flight_id'], 'drone_id': p['drone_id'], 'due': due,
               'deadline': due,
               'started_at': p.get('started_at'), 'mac': p.get('mac'),
               'band': p.get('band'), 'channel': p.get('channel'), 'fix': None}
        with self._cv:
            self._jobs[job['flight_id']] = job
            heapq.heappush(self._heap, (due, next(self._seq), job['flight_id']))
            self._cv.notify()

    def node_event(self, kind, node, detail=None):
        """A receiver changed state (see nodes.NodeRegistry.check). Queued for
        the worker; never blocks the monitor or ingest thread that calls it."""
        if self._thread is None or not self.settings.get('notify.enabled'):
            return
        with self._cv:
            self._node_q.append({'kind': kind, 'node': node, 'detail': detail or {},
                                 'at': time.time()})
            self._cv.notify()

    def flight_point(self, p):
        """A detection with a position. The first one sends the alert now."""
        with self._cv:
            job = self._jobs.get(p.get('flight_id'))
            if job is None:
                return
            first = job['fix'] is None
            job['fix'] = {'lat': p.get('lat'), 'lon': p.get('lon'),
                          'alt': p.get('alt'), 'rssi': p.get('rssi')}
            if first:
                job['due'] = time.time()
                heapq.heappush(self._heap, (job['due'], next(self._seq), job['flight_id']))
                self._cv.notify()

    # -- worker -------------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name='notify')
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify()

    def _next_job(self):
        with self._cv:
            while not self._stop.is_set():
                if self._node_q:
                    return self._node_q.popleft()
                now = time.time()
                if self._heap and self._heap[0][0] <= now:
                    due, _, fid = heapq.heappop(self._heap)
                    job = self._jobs.get(fid)
                    # A job brought forward leaves its original entry behind.
                    if job is None or job['due'] != due:
                        continue
                    if now < job['deadline'] and self._held(job):
                        job['due'] = min(now + HOLD_RECHECK_S, job['deadline'])
                        heapq.heappush(self._heap, (job['due'], next(self._seq), fid))
                        continue
                    del self._jobs[fid]
                    return job
                self._cv.wait(self._heap[0][0] - now if self._heap else None)
        return None

    def _held(self, job):
        try:
            return bool(self._hold and self._hold(job['drone_id']))
        except Exception:
            return False

    def _run(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            try:
                if 'kind' in job:
                    self.deliver_node(job)
                else:
                    self.deliver(job)
            except Exception as e:                    # never let the worker die
                logger.warning('alert %s failed: %s', job.get('flight_id') or job.get('kind'), e)

    def _drone(self, drone_id):
        r = self.db.one(
            "SELECT d.id, d.basic_id, d.label, d.tag, d.group_id, d.color, d.faa_make,"
            " d.faa_model, d.id_type, g.name AS group_name, g.color AS group_color"
            " FROM drones d LEFT JOIN groups g ON g.id = d.group_id WHERE d.id=?", (drone_id,))
        return dict(r) if r else None

    def _flight(self, job):
        """Positions and signal as of now: live state while the flight is open,
        the flight row if it already closed."""
        out = {'started_at': job['started_at'], 'mac': job['mac'],
               'band': job['band'], 'channel': job['channel']}
        st = self.sess.open_flights.get(job['drone_id']) if self.sess is not None else None
        if st is not None and st.flight_id == job['flight_id']:
            out.update(lat=st.end_lat, lon=st.end_lon, alt=None, rssi=st.max_rssi,
                       pilot_lat=st.pilot_lat, pilot_lon=st.pilot_lon)
        else:
            r = self.db.one("SELECT end_lat, end_lon, pilot_lat, pilot_lon, max_rssi"
                            " FROM flights WHERE id=?", (job['flight_id'],))
            if r is not None:
                out.update(lat=r['end_lat'], lon=r['end_lon'], alt=None, rssi=r['max_rssi'],
                           pilot_lat=r['pilot_lat'], pilot_lon=r['pilot_lon'])
        fix = job.get('fix')
        if fix and fix.get('lat') is not None:
            out.update({k: v for k, v in fix.items() if v is not None})
        out['prior_flights'] = self.db.one(
            "SELECT COUNT(*) AS n FROM flights WHERE drone_id=? AND id<?",
            (job['drone_id'], job['flight_id']))['n']
        return out

    def deliver(self, job):
        """Send one takeoff alert to every channel whose filter selects the drone."""
        if not self.settings.get('notify.enabled'):
            return []
        channels = [ch for ch in (self._decode(r) for r in self.db.query(
            "SELECT * FROM notify_channels WHERE enabled=1 ORDER BY id"))
            if 'takeoff' in ch['events']]
        drone = self._drone(job['drone_id']) if channels else None
        if drone is None:                         # no channels, or drone merged away
            return []
        results, alert, now = [], None, time.time()
        for ch in channels:
            if not matches(ch['filter'], drone):
                continue
            last = self.db.one("SELECT ts FROM notify_last WHERE channel_id=? AND drone_id=?",
                               (ch['id'], drone['id']))
            if last is not None and now - last['ts'] < ch['cooldown_s']:
                detail = 'cooldown: alerted %d s ago' % (now - last['ts'])
                self._log(ch['id'], drone['id'], job['flight_id'], 'suppressed', detail)
                results.append((ch['id'], 'suppressed'))
                continue
            if alert is None:
                alert = compose(drone, self._flight(job), self.settings.get('notify.base_url'))
            ok, detail = self.send(ch, alert)
            self._log(ch['id'], drone['id'], job['flight_id'], 'sent' if ok else 'failed', detail)
            results.append((ch['id'], 'sent' if ok else 'failed'))
        return results

    def deliver_node(self, job):
        """Send one node alert to every channel that takes them."""
        if not self.settings.get('notify.enabled'):
            return []
        kind, node = job['kind'], job['node']
        channels = [ch for ch in (self._decode(r) for r in self.db.query(
            "SELECT * FROM notify_channels WHERE enabled=1 ORDER BY id"))
            if 'nodes' in ch['events']]
        results, alert, now = [], None, time.time()
        for ch in channels:
            key = (ch['id'], node.get('id'), kind)
            last = self._node_last.get(key)
            if last is not None and now - last < NODE_COOLDOWN_S:
                self._log(ch['id'], None, None, 'suppressed',
                          'node cooldown: same alert %d s ago' % (now - last), kind, node.get('id'))
                results.append((ch['id'], 'suppressed'))
                continue
            if alert is None:
                alert = compose_node(kind, node, job.get('detail'),
                                     self.settings.get('notify.base_url'), now)
            ok, detail = self.send(ch, alert)
            if ok:
                self._node_last[key] = now
            self._log(ch['id'], None, None, 'sent' if ok else 'failed', detail, kind, node.get('id'))
            results.append((ch['id'], 'sent' if ok else 'failed'))
        return results
