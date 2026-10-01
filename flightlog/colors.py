"""Per-drone display colours.

A drone with no chosen colour, and no group colour, still needs one of its own.
The old fallback hashed the *flight* id by summing its character codes, so
flights 100 and 101 came out one degree of hue apart: every new drone looked the
same, and one drone's own flights each drifted slightly.

Stepping the hue by the golden angle per drone id puts consecutive drones about
137 degrees apart without ever repeating exactly, so the first few are as far
apart as they can be and later ones fill the gaps. It is resolved here, on the
server, so the table, the map, the live view and notifications all agree.
"""
import colorsys
import re

GOLDEN_ANGLE = 137.50776405003785
SATURATION = 0.80
LIGHTNESS = 0.58

_HEX = re.compile(r'#[0-9a-fA-F]{6}')


def default_color(drone_id):
    """The automatic colour for a drone, as #rrggbb."""
    hue = ((drone_id or 0) * GOLDEN_ANGLE) % 360.0
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, LIGHTNESS, SATURATION)
    return '#%02x%02x%02x' % (round(r * 255), round(g * 255), round(b * 255))


def display_color(drone_color, group_color, drone_id):
    """What to draw a drone in: its own colour, else its group's, else automatic."""
    return drone_color or group_color or default_color(drone_id)


def to_int(color, drone_id=None):
    """#rrggbb as an integer (Discord's embed colour).

    Group colours are not validated on the way in, so anything that is not a
    plain hex colour falls back to the drone's automatic one.
    """
    if not (isinstance(color, str) and _HEX.fullmatch(color)):
        color = default_color(drone_id)
    return int(color[1:], 16)
