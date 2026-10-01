"""Turn a raw serial line into a normalized detection dict, or None.

The tolerance here is deliberate and is carried over from mesh-mapper.py's
serial_reader (:12951-12999). The device emits, on the same wire as detections:

  - several {"heartbeat": ...} shapes
  - {"status":"active","mode":"dual-band","bands":[...]}
  - plain-text banners ("Scanning.", "[HOME] Stats: ...", ASCII art)
  - Meshtastic-prefixed JSON, e.g.  77b4: {"mac":"..."}
  - one heartbeat that is not valid JSON at all:
        {"   [+] Device is active and scanning..."}

so anything that fails to parse is dropped silently rather than logged as an
error - otherwise a healthy node produces a continuous stream of warnings.

Normalization applied here (and nowhere else):
  * `remote_id` -> `basic_id`
  * coordinates of exactly 0 -> None. The firmware encodes "no fix" as 0, which
    is what forces `!== 0` checks through the whole legacy frontend. One
    conversion at the boundary means the rest of the app can trust NULL.
  * empty/missing `basic_id` -> None (node-mode omits the key entirely)
"""
import json

# A line must carry at least one of these to be considered a detection.
_DETECTION_KEYS = ('mac', 'drone_lat', 'pilot_lat', 'basic_id', 'remote_id')


# ODID_ID_SIZE is 20 in the firmware's opendroneid.h; anything longer is either
# corrupt or an injection artifact. The standard/dualcore build snprintf()s the
# raw over-the-air UAS ID straight into the JSON (remoteid-mesh-dualcore/src/
# main.cpp:130) with no sanitizing - only node-mode sanitizes on-device - so the
# host has to do it too.
#
# Exactly 20 is valid and common, not an edge case: a CTA-2063A serial is a
# 4-char manufacturer code, a length character and up to 15 serial characters,
# so every DJI airframe with a full serial (1581F...) is 20 long. The length
# test below must stay `> MAX_BASIC_ID_LEN` - `>=` would silently drop them all.
MAX_BASIC_ID_LEN = 20

# A basic_id shorter than this is not a real CTA-2063A serial (those are 16-20
# chars). Rejecting is the safe direction: an unrecognized serial just falls
# back to MAC-keyed identity, whereas accepting a junk value merges every drone
# that broadcasts it into a single record - silently, and painfully to unwind.
MIN_BASIC_ID_LEN = 6

_BASIC_ID_ALLOWED = set(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:')

JUNK_BASIC_IDS = {
    'NONE', 'NULL', 'N/A', 'NA', 'NIL', 'UNKNOWN', 'UNDEFINED', 'TEST',
    'SERIAL', 'SERIALNUMBER', 'ANSI', 'DEFAULT', 'DRONEID', '1234567890',
}


def norm_basic_id(value):
    """Normalize a wire basic_id, returning None for anything untrustworthy."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    s = ''.join(ch for ch in s.strip() if ch in _BASIC_ID_ALLOWED)
    if not s or len(s) > MAX_BASIC_ID_LEN or len(s) < MIN_BASIC_ID_LEN:
        return None
    u = s.upper()
    if u in JUNK_BASIC_IDS:
        return None
    if len(set(u)) == 1:          # '000000000000', '################'
        return None
    return u


def _coord(value):
    """Coerce a coordinate to float, mapping 0 and junk to None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f == 0.0:
        return None
    if not (-180.0 <= f <= 180.0):
        return None
    return f


def _int(value):
    if value is None or value == '':
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_json(line: str):
    """Parse the first JSON object in `line`, or None.

    Slicing from the first '{' is what tolerates the Meshtastic "77b4: " prefix
    and any boot noise that shares the line.
    """
    if not line:
        return None
    if '{' in line:
        line = line[line.find('{'):]
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def normalize(raw: dict, source: str = None):
    """Map a parsed device object to a detection dict, or None if it isn't one.

    Returns keys: mac, rssi, lat, lon, alt, pilot_lat, pilot_lon, basic_id,
    node_id, band, channel, source, id_type.

    id_type is 'DJI' for DJI DroneID detections, else None. DJI DroneID carries
    no live operator position, only the takeoff point (home_lat/home_long); it
    is stored in the operator fields - where the pilot usually stands - and the
    drone is marked DJI so every view can call it a home point, not a pilot fix.
    """
    if not isinstance(raw, dict):
        return None
    # Heartbeats are normal traffic, not detections.
    if 'heartbeat' in raw:
        return None
    # Anything without a detection key is a status/banner frame. This is what
    # drops the C5 build's {"status":"active","mode":"dual-band",...} - keying
    # off the presence of `status` instead would also drop real detections,
    # which carry a status field of their own from the legacy pipeline and from
    # mapper_test.py.
    if not any(k in raw for k in _DETECTION_KEYS):
        return None

    basic_id = raw.get('basic_id')
    if basic_id is None:
        basic_id = raw.get('remote_id')       # legacy alias; no in-tree firmware sends it
    basic_id = norm_basic_id(basic_id)

    mac = raw.get('mac')
    if isinstance(mac, str):
        mac = mac.strip().lower() or None

    id_type = 'DJI' if raw.get('id_type') == 'DJI' else None
    plat, plon = _coord(raw.get('pilot_lat')), _coord(raw.get('pilot_long'))
    if plat is None and plon is None and id_type == 'DJI':
        plat, plon = _coord(raw.get('home_lat')), _coord(raw.get('home_long'))
    if plat is None or plon is None:
        plat = plon = None                    # half a position is no position

    return {
        'mac': mac,
        'rssi': _int(raw.get('rssi')),
        'lat': _coord(raw.get('drone_lat')),
        'lon': _coord(raw.get('drone_long')),
        'alt': _int(raw.get('drone_altitude')),
        'pilot_lat': plat,
        'pilot_lon': plon,
        'basic_id': basic_id,
        'node_id': raw.get('node_id') or None,
        'band': raw.get('band') or None,
        'channel': _int(raw.get('channel')),
        'source': source,
        'id_type': id_type,
    }


class LineParser:
    """Stateful per-source parser.

    Carries the last-seen MAC forward so a fragmented record that arrives with
    no `mac` is still attributable, matching `last_mac_by_port` in the legacy
    reader (:12985).
    """

    def __init__(self, source: str = None):
        self.source = source
        self._last_mac = None

    def feed(self, line: str):
        """Return a normalized detection dict for `line`, or None."""
        det = normalize(extract_json(line), self.source)
        if det is None:
            return None
        if det['mac']:
            self._last_mac = det['mac']
        elif self._last_mac:
            det['mac'] = self._last_mac
        else:
            return None          # no MAC and nothing to carry forward - unusable
        return det
