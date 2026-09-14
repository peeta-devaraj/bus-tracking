"""Geospatial helpers for route snapping and distance work.

Everything here is pure Python with no Azure dependency so it can be unit
tested on its own. Distances are metres, angles are degrees.

At town scale (a few tens of km) we project lat/lon onto a local flat plane
centred on the segment being tested. The error from ignoring curvature is
well under a metre, which is far below GPS noise, and it keeps the projection
maths simple.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

EARTH_RADIUS_M = 6_371_008.8

Point = tuple[float, float]  # (lat, lon)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees (0-360, 0 = north) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _to_local_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """Equirectangular projection to metres relative to a reference point."""
    x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * EARTH_RADIUS_M
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


# --------------------------------------------------------------------------
# Encoded polylines (Google algorithm, precision 5)
# --------------------------------------------------------------------------

def encode_polyline(points: Sequence[Point], precision: int = 5) -> str:
    """Encode [(lat, lon), ...] into a Google encoded polyline string."""
    factor = 10 ** precision
    out: list[str] = []
    prev_lat = prev_lon = 0

    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else (delta << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon

    return "".join(out)


def decode_polyline(encoded: str, precision: int = 5) -> list[Point]:
    """Decode a Google encoded polyline string into [(lat, lon), ...]."""
    factor = 10 ** precision
    points: list[Point] = []
    index = lat = lon = 0
    length = len(encoded)

    while index < length:
        for is_lon in (False, True):
            result = shift = 0
            while True:
                if index >= length:
                    return points  # truncated input; return what we have
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        points.append((lat / factor, lon / factor))

    return points


# --------------------------------------------------------------------------
# Route snapping
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapResult:
    """Where a GPS fix sits relative to a route polyline."""

    offset_m: float       # perpendicular distance from the route
    along_m: float        # distance travelled along the route from its start
    segment_index: int    # which segment of the polyline it snapped to
    snapped: Point        # the closest point on the route itself


def cumulative_distances(points: Sequence[Point]) -> list[float]:
    """Running distance along a polyline; result[i] is the distance to points[i]."""
    out = [0.0]
    for i in range(1, len(points)):
        out.append(out[-1] + haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
    return out


def route_length_m(points: Sequence[Point]) -> float:
    if len(points) < 2:
        return 0.0
    return cumulative_distances(points)[-1]


def snap_to_route(point: Point, route: Sequence[Point]) -> SnapResult | None:
    """Find the closest position on `route` to `point`.

    Returns None for a degenerate route (fewer than two vertices), which is a
    real case while a route is still being recorded.
    """
    if len(route) < 2:
        return None

    lat, lon = point
    cumulative = cumulative_distances(route)
    best: SnapResult | None = None

    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        # Project into a local plane centred on the segment start.
        ax, ay = 0.0, 0.0
        bx, by = _to_local_xy(b[0], b[1], a[0], a[1])
        px, py = _to_local_xy(lat, lon, a[0], a[1])

        seg_dx, seg_dy = bx - ax, by - ay
        seg_len_sq = seg_dx * seg_dx + seg_dy * seg_dy

        if seg_len_sq == 0.0:
            t = 0.0
        else:
            t = ((px - ax) * seg_dx + (py - ay) * seg_dy) / seg_len_sq
            t = max(0.0, min(1.0, t))  # clamp so we stay on the segment

        # Interpolate the snapped point back in lat/lon space.
        snap_lat = a[0] + (b[0] - a[0]) * t
        snap_lon = a[1] + (b[1] - a[1]) * t
        offset = haversine(lat, lon, snap_lat, snap_lon)

        if best is None or offset < best.offset_m:
            seg_len = haversine(a[0], a[1], b[0], b[1])
            best = SnapResult(
                offset_m=offset,
                along_m=cumulative[i] + seg_len * t,
                segment_index=i,
                snapped=(snap_lat, snap_lon),
            )

    return best


def densify(points: Sequence[Point], max_gap_m: float = 25.0) -> list[Point]:
    """Insert intermediate vertices so no two are further apart than max_gap_m.

    The simulator uses this to move a bus smoothly along a route that may have
    been recorded with sparse vertices.
    """
    if len(points) < 2:
        return list(points)

    out: list[Point] = [points[0]]
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        gap = haversine(a[0], a[1], b[0], b[1])
        steps = max(1, math.ceil(gap / max_gap_m))
        for s in range(1, steps + 1):
            t = s / steps
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def point_at_distance(route: Sequence[Point], along_m: float) -> Point:
    """The lat/lon sitting `along_m` metres along the route, clamped to its ends."""
    if not route:
        raise ValueError("empty route")
    if len(route) == 1:
        return route[0]

    cumulative = cumulative_distances(route)
    total = cumulative[-1]
    along_m = max(0.0, min(along_m, total))

    for i in range(1, len(cumulative)):
        if cumulative[i] >= along_m:
            seg_len = cumulative[i] - cumulative[i - 1]
            t = 0.0 if seg_len == 0 else (along_m - cumulative[i - 1]) / seg_len
            a, b = route[i - 1], route[i]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    return route[-1]


def bbox_contains(bbox: tuple[float, float, float, float], lat: float, lon: float) -> bool:
    """bbox is (min_lat, min_lon, max_lat, max_lon)."""
    min_lat, min_lon, max_lat, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# --------------------------------------------------------------------------
# Out-and-back excursions ("spurs")
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Excursion:
    """A stretch where a path leaves a point and comes back to it.

    Routing engines produce these when a waypoint snaps to a side lane: the
    route drives up the lane to touch the waypoint and reverses out again.
    Recorded GPS traces produce them too, when a bus pulls into a stand.
    """

    start: int            # index where the path leaves
    end: int              # index where it has returned (same place as start)
    length_m: float       # distance travelled out and back
    tip: Point            # the furthest point reached from `start`


def find_excursions(
    points: Sequence[Point],
    max_length_m: float = 600.0,
    return_radius_m: float = 8.0,
) -> list[Excursion]:
    """Find out-and-back excursions no longer than max_length_m.

    An excursion is a stretch where the path goes somewhere and retraces its
    way back to within return_radius_m of where it left. Three rules keep this
    from misfiring:

      * The whole out-and-back must fit the limit. A detection is grown back
        to where the path genuinely left the road before being measured, so a
        long dead end is rejected outright rather than half-trimmed into a
        stub.
      * The path must actually go somewhere -- further than return_radius_m.
        A GPS trace sitting still at a stop repeats one coordinate; that is a
        dwell, not a detour.
      * The length limit is what protects real circular routes, which also
        return to their start but only after a long way round.
    """
    n = len(points)
    found: list[Excursion] = []

    def gap(a: int, b: int) -> float:
        return haversine(points[a][0], points[a][1], points[b][0], points[b][1])

    i = 0
    while i < n:
        travelled = 0.0
        end: int | None = None
        for j in range(i + 1, n):
            travelled += gap(j - 1, j)
            if travelled > max_length_m:
                break
            if j > i + 1 and gap(i, j) <= return_radius_m:
                end = j  # keep scanning: prefer the longest return within the limit

        if end is None:
            i += 1
            continue

        # Grow outwards while the path before `i` and after `end` retrace each
        # other: that is still the same excursion, seen from further down.
        start = i
        while start > 0 and end + 1 < n and gap(start - 1, end + 1) <= return_radius_m:
            start -= 1
            end += 1

        length = sum(gap(k - 1, k) for k in range(start + 1, end + 1))
        base = points[start]
        tip = max(points[start + 1 : end], key=lambda p: haversine(base[0], base[1], p[0], p[1]))
        reach = haversine(base[0], base[1], tip[0], tip[1])

        if length <= max_length_m and reach > return_radius_m:
            # Growing outwards can reach back over an excursion already found;
            # the wider one supersedes it.
            while found and found[-1].start >= start:
                found.pop()
            found.append(Excursion(start=start, end=end, length_m=length, tip=tip))

        # Skip past this stretch either way, so a rejected long dead end is not
        # rediscovered piecemeal from each of its inner points.
        i = end

    return found


def remove_excursions(points: Sequence[Point], excursions: Sequence[Excursion]) -> list[Point]:
    """Cut the given excursions out of a path, keeping everything else in order."""
    drop: set[int] = set()
    for ex in excursions:
        # Keep the departure vertex; drop the loop up to and including the
        # return vertex, which duplicates it.
        drop.update(range(ex.start + 1, ex.end + 1))
    return [p for idx, p in enumerate(points) if idx not in drop]
