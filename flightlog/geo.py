"""Geodesy helpers.

`haversine_m` is lifted verbatim from mesh-mapper.py:955 (`_haversine_m`) so the
distances this app reports match the ones the legacy app and its geofences have
always produced.
"""
import math

# Mean Earth radius (m) - same constant the legacy geofence code uses.
_R = 6371008.8


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two lat/lon points in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial great-circle bearing in degrees, 0-360 (0 = north).

    The firmware decodes ODID `Direction` but never serializes it, so heading has
    to be derived from successive fixes.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _perp_distance(pt, start, end) -> float:
    """Perpendicular distance from `pt` to the segment start-end, in degrees.

    Longitude is scaled by cos(lat) so the simplification stays roughly
    isotropic on the ground instead of over-preserving detail near the poles.
    """
    scale = math.cos(math.radians(start[0])) or 1e-9
    px, py = pt[1] * scale, pt[0]
    ax, ay = start[1] * scale, start[0]
    bx, by = end[1] * scale, end[0]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points, tolerance_deg: float = 0.00002):
    """Ramer-Douglas-Peucker, iterative so a long flight can't blow the stack.

    `points` is a list of [lat, lon]. The default tolerance is roughly 2 m and is
    only used to shrink a stored path for map drawing - the raw detection rows
    remain the source of truth.
    """
    n = len(points)
    if n < 3:
        return list(points)

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, worst_i = -1.0, -1
        for i in range(first + 1, last):
            d = _perp_distance(points[i], points[first], points[last])
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance_deg and worst_i != -1:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))

    return [points[i] for i in range(n) if keep[i]]


def decimate(points, max_points: int):
    """Cap a path at `max_points`, keeping the first and last point.

    Used only as a backstop after `simplify` when a path is still huge. Unlike
    the strided slicing in the drone-mesh-mapper-map prototype this is applied
    after RDP, so the points that survive are the ones that carry the shape.
    """
    n = len(points)
    if max_points < 2 or n <= max_points:
        return list(points)
    step = (n - 1) / (max_points - 1)
    out = [points[int(round(i * step))] for i in range(max_points)]
    out[-1] = points[-1]
    return out
