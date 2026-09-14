"""Training learned arrival models from stored history, and serving them.

Kept apart from learned_eta.py so the model itself stays free of storage and
testable offline. This module is the glue: read a route's history, train,
save, and hand a cached model to the /eta endpoint.

Honesty
-------
Every model records `simulatedShare`: the fraction of its training fixes that
came from simulated buses. The API turns that into `trainedOn` on every
learned estimate -- "simulated", "real" or "mixed" -- so an estimate learned
from the simulator can never be passed off as knowledge of real traffic.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from . import storage
from .geo import decode_polyline, route_length_m, snap_to_route
from .learned_eta import SegmentModel, TrackPoint, train_with_calibration

log = logging.getLogger("bustrack.training")

HISTORY_DAYS = 14
MODEL_CACHE_TTL_S = 300.0

_cache: dict[str, tuple[float, SegmentModel | None, dict | None]] = {}


def trained_on(simulated_share: float) -> str:
    if simulated_share >= 0.999:
        return "simulated"
    if simulated_share <= 0.001:
        return "real"
    return "mixed"


def _days_back(days: int, now: float) -> list[str]:
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return [(today - timedelta(days=d)).strftime("%Y%m%d") for d in range(days - 1, -1, -1)]


def gather_tracks(route_id: str, days: int = HISTORY_DAYS, now: float | None = None):
    """A route's usable history: {day: [track per bus]}, simulated share, bus count.

    Only fixes stamped with this route and a known direction are usable. Fixes
    from before direction was recorded are skipped rather than guessed at.
    """
    now = time.time() if now is None else now
    buses = [b for b in storage.list_buses() if (b.get("routeId") or "") == route_id]

    tracks_by_day: dict[str, list[list[TrackPoint]]] = {}
    simulated = total = 0

    for day in _days_back(days, now):
        for bus in buses:
            points = []
            for row in storage.history_for_day(bus["RowKey"], day):
                if row.get("routeId") != route_id or row.get("alongM") is None:
                    continue
                direction = int(row.get("direction") or 0)
                if direction not in (1, -1):
                    continue
                points.append(TrackPoint(float(row["ts"]), float(row["alongM"]), direction))
            if len(points) < 2:
                continue
            points.sort(key=lambda p: p.ts)
            tracks_by_day.setdefault(day, []).append(points)
            total += len(points)
            if bus.get("isSimulated"):
                simulated += len(points)

    share = (simulated / total) if total else 0.0
    return tracks_by_day, share, len(buses)


def train_route(route_id: str, days: int = HISTORY_DAYS, now: float | None = None) -> dict:
    """Train and store one route's model. Returns a summary for logs and the API."""
    started = time.time()
    route = storage.find_route_anywhere(route_id)
    if not route or not route.get("polyline"):
        return {"routeId": route_id, "trained": False, "reason": "unknown route or no geometry"}

    points = decode_polyline(route["polyline"])
    length = route_length_m(points)
    city = route.get("PartitionKey")

    stops_along = []
    for stop in storage.list_stops(city):
        if route_id in (stop.get("routeIds") or []):
            snap = snap_to_route((stop["lat"], stop["lon"]), points)
            if snap is not None:
                stops_along.append(snap.along_m)

    tracks_by_day, simulated_share, bus_count = gather_tracks(route_id, days, now)
    fix_count = sum(len(t) for tracks in tracks_by_day.values() for t in tracks)
    if not tracks_by_day:
        return {"routeId": route_id, "trained": False, "reason": "no usable history",
                "buses": bus_count, "historyDays": days}

    model, info = train_with_calibration(length, tracks_by_day, sorted(stops_along))
    meta, rows = model.to_rows()
    meta.update({
        "trainedAt": time.time(),
        "historyDays": days,
        "daysWithData": info["days"],
        "fixes": fix_count,
        "buses": bus_count,
        "simulatedShare": simulated_share,
        "calibrationSamples": info["calibrationSamples"],
    })
    stored = storage.save_eta_model(route_id, meta, rows)
    _cache.pop(route_id, None)

    summary = {
        "routeId": route_id,
        "trained": True,
        "fixes": fix_count,
        "daysWithData": info["days"],
        "buses": bus_count,
        "rowsStored": stored,
        "calibrated": model.calibrated,
        "calibrationSamples": info["calibrationSamples"],
        "range": [round(model.range_low, 3), round(model.range_high, 3)],
        "bias": round(model.bias, 3),
        "trainedOn": trained_on(simulated_share),
        "seconds": round(time.time() - started, 1),
    }
    log.info("trained ETA model: %s", summary)
    return summary


def get_model(route_id: str) -> tuple[SegmentModel | None, dict | None]:
    """The stored model for a route, cached per worker for a few minutes."""
    now = time.time()
    hit = _cache.get(route_id)
    if hit and now - hit[0] < MODEL_CACHE_TTL_S:
        return hit[1], hit[2]

    model: SegmentModel | None = None
    meta: dict | None = None
    try:
        loaded = storage.load_eta_model(route_id)
        if loaded is not None:
            meta, rows = loaded
            model = SegmentModel.from_rows(meta, rows)
    except Exception:  # a broken model must never take /eta down with it
        log.exception("could not load ETA model for %s; using naive estimates", route_id)
        model, meta = None, None

    _cache[route_id] = (now, model, meta)
    return model, meta
