"""Synthetic bus traffic with realistic structure, for evaluating ETAs offline.

READ THIS BEFORE QUOTING ANY NUMBER PRODUCED WITH IT
-----------------------------------------------------
Nobody has recorded real buses in Nagercoil for this project. Every trip here
is invented. The *shape* of the traffic is chosen to be plausible -- town
streets choke at rush hour, the highway mostly does not, buses wait longer at
bus stands than at roadside stops -- but the magnitudes are assumptions, not
measurements. An estimator that does well on this data has shown that its
*method* can exploit that kind of structure. It has not shown anything about
Nagercoil's actual roads.

That is also why there is a `uniform` mode. It has the same average speed and
the same randomness, but no structure by place or time of day. A learned model
should gain nothing there. If it appears to, the evaluation is broken.

Where the physical numbers come from
------------------------------------
Each route's legs carry OSRM free-flow car speeds (tools/data). A leg whose
free-flow speed is under URBAN_FREE_FLOW_KMH is treated as town streets, the
rest as open road. That is the only place-specific input; everything else is
the assumption tables below.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared.eta import infer_direction  # noqa: E402
from shared.geo import cumulative_distances, decode_polyline  # noqa: E402
from shared.learned_eta import IST, time_band  # noqa: E402

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "nagercoil_routes.json")

STRUCTURED = "structured"
UNIFORM = "uniform"

# ---------------------------------------------------------------- assumptions

URBAN_FREE_FLOW_KMH = 35.0

# Buses are slower than the free-flow car speed OSRM reports, and capped.
BUS_FACTOR = 0.8
BUS_MAX_KMH = 55.0

# Speed multiplier by road type and time-of-day band.
CONGESTION = {
    "urban": {"late": 1.0, "early": 0.9, "morning_peak": 0.45, "midday": 0.75,
              "evening_peak": 0.4, "night": 0.85},
    "open": {"late": 1.0, "early": 1.0, "morning_peak": 0.75, "midday": 0.95,
             "evening_peak": 0.7, "night": 1.0},
}

# Seconds standing at a stop, by kind, before the time-of-day factor.
DWELL_S = {"station": (60.0, 150.0), "roadside": (15.0, 45.0)}
PEAK_DWELL_FACTOR = 1.5
PEAK_BANDS = {"morning_peak", "evening_peak"}

# Unscheduled halts (signals, a cow, a lorry unloading), per 100 m.
HALT_CHANCE = {"urban": 0.08, "open": 0.01}
HALT_S = (10.0, 40.0)

# Randomness shared by both modes, so the control differs only in structure.
DAY_SIGMA = 0.10        # some days are simply worse
TRIP_SIGMA = 0.07       # some drivers are simply quicker
PIECE_SIGMA = 0.15      # speed wanders along the road, per 100 m
PIECE_M = 100.0

LAYOVER_S = (240.0, 480.0)
TERMINUS_STOP_ZONE_M = 150.0
SERVICE_START = (5, 30)
SERVICE_END = (22, 30)
HEADWAY_S = 20 * 60


@dataclass
class Stop:
    stop_id: str
    along_m: float
    kind: str


@dataclass
class RouteProfile:
    route_id: str
    length_m: float
    points: list[tuple[float, float]]
    stops: list[Stop]
    # (start_m, end_m, free-flow km/h), covering the route between first and last stop
    legs: list[tuple[float, float, float]]
    cumulative: list[float]

    def position(self, along_m: float) -> tuple[float, float]:
        """Lat/lon at a distance along the route.

        shared.geo.point_at_distance recomputes the route's running distances
        on every call. Fine for the API, far too slow for simulating weeks of
        GPS samples, so this looks positions up in precomputed distances.
        """
        along_m = min(self.length_m, max(0.0, along_m))
        i = max(1, bisect.bisect_left(self.cumulative, along_m))
        i = min(i, len(self.points) - 1)
        seg = self.cumulative[i] - self.cumulative[i - 1]
        t = 0.0 if seg == 0 else (along_m - self.cumulative[i - 1]) / seg
        a, b = self.points[i - 1], self.points[i]
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    def road_type(self, along_m: float) -> str:
        for start, end, kmh in self.legs:
            if start <= along_m <= end:
                return "urban" if kmh < URBAN_FREE_FLOW_KMH else "open"
        return "urban"

    def free_flow_kmh(self, along_m: float) -> float:
        for start, end, kmh in self.legs:
            if start <= along_m <= end:
                return kmh
        return self.legs[0][2] if along_m < self.legs[0][0] else self.legs[-1][2]


def load_profile(route_id: str, path: str = DATA_PATH) -> RouteProfile:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    route = next(r for r in data["routes"] if r["routeId"] == route_id)
    kinds = {s["stopId"]: s["kind"] for s in data["stops"]}
    points = decode_polyline(route["polyline"])

    along = {s["stopId"]: s["alongM"] for s in route["stopsAlong"]}
    stops = [Stop(s["stopId"], s["alongM"], kinds[s["stopId"]]) for s in route["stopsAlong"]]
    legs = [
        (along[leg["from"]], along[leg["to"]], leg["distanceM"] / leg["freeFlowS"] * 3.6)
        for leg in route["legs"]
    ]
    cumulative = cumulative_distances(points)
    return RouteProfile(route_id, cumulative[-1], points, stops, legs, cumulative)


@dataclass
class Fix:
    """What a phone would send, plus the truth the evaluation grades against."""

    ts: float
    along_m: float          # observed: true position plus GPS noise, clamped
    lat: float
    lon: float
    speed_mps: float        # observed instantaneous speed
    direction: int          # inferred the same way ingest infers it
    true_along_m: float
    true_direction: int


class Traffic:
    def __init__(self, profile: RouteProfile, mode: str = STRUCTURED, seed: int = 1):
        if mode not in (STRUCTURED, UNIFORM):
            raise ValueError(mode)
        self.profile = profile
        self.mode = mode
        self.rng = random.Random(seed)
        self._uniform_kmh = self._average_structured_kmh() if mode == UNIFORM else None
        self._uniform_halt_chance = self._spread_halt_chance() if mode == UNIFORM else None

    def _average_structured_kmh(self) -> float:
        """Length- and daytime-weighted mean bus speed of the structured model.

        The control must be as fast *on average* as the structured traffic, or
        any difference between estimators could just be a speed mismatch.
        """
        daytime = [b for b in ("early", "morning_peak", "midday", "evening_peak", "night")]
        weights = {"early": 2.5, "morning_peak": 2, "midday": 6, "evening_peak": 4, "night": 2.5}
        total_w = total = 0.0
        step = 50.0
        along = 0.0
        while along < self.profile.length_m:
            road = self.profile.road_type(along)
            base = min(BUS_MAX_KMH, self.profile.free_flow_kmh(along) * BUS_FACTOR)
            for band in daytime:
                w = weights[band] * step
                # Averaging pace, not speed, so the mean trip time matches.
                total += w / (base * CONGESTION[road][band])
                total_w += w
            along += step
        return total_w / total

    def _spread_halt_chance(self) -> float:
        """Halt probability per 100 m for the control.

        The control removes waiting *at stops*, because stops are at fixed
        places a model could learn. But dropping that time entirely would make
        control trips faster than structured ones. So the same expected waiting
        -- unscheduled halts plus stop dwell -- is re-spread as halts equally
        likely anywhere on the route.
        """
        L = self.profile.length_m or 1.0
        urban_m = sum(e - s for s, e, kmh in self.profile.legs if kmh < URBAN_FREE_FLOW_KMH)
        halts_per_trip = (HALT_CHANCE["urban"] * urban_m + HALT_CHANCE["open"] * (L - urban_m)) / PIECE_M
        mean_halt = sum(HALT_S) / 2.0
        # Every stop except the one a trip starts from gets a dwell.
        dwell_per_trip = sum(sum(DWELL_S[s.kind]) / 2.0 for s in self.profile.stops[1:])
        pieces = L / PIECE_M
        return (halts_per_trip * mean_halt + dwell_per_trip) / mean_halt / pieces

    # ---- speeds and stops -------------------------------------------------

    def speed_kmh(self, along_m: float, ts: float) -> float:
        if self.mode == UNIFORM:
            return self._uniform_kmh
        road = self.profile.road_type(along_m)
        base = min(BUS_MAX_KMH, self.profile.free_flow_kmh(along_m) * BUS_FACTOR)
        return base * CONGESTION[road][time_band(ts)]

    def dwell_s(self, stop: Stop, ts: float) -> float:
        if self.mode == UNIFORM:
            return 0.0  # no place-specific waiting in the control
        lo, hi = DWELL_S[stop.kind]
        seconds = self.rng.uniform(lo, hi)
        if time_band(ts) in PEAK_BANDS:
            seconds *= PEAK_DWELL_FACTOR
        return seconds

    def halt_chance(self, along_m: float) -> float:
        if self.mode == UNIFORM:
            return self._uniform_halt_chance
        return HALT_CHANCE[self.profile.road_type(along_m)]

    # ---- one bus, one day ---------------------------------------------------

    def run_day(self, day: date, bus_index: int, sample_s: float = 10.0, gps_noise_m: float = 5.0) -> list[Fix]:
        rng = self.rng
        profile = self.profile
        L = profile.length_m

        start = datetime(day.year, day.month, day.day, *SERVICE_START, tzinfo=IST).timestamp()
        end = datetime(day.year, day.month, day.day, *SERVICE_END, tzinfo=IST).timestamp()
        ts = start + bus_index * HEADWAY_S

        day_factor = math.exp(rng.gauss(0, DAY_SIGMA))
        direction = 1 if bus_index % 2 == 0 else -1
        along = 0.0 if direction > 0 else L

        fixes: list[Fix] = []
        next_sample = ts
        anchor: float | None = None
        prev_dir = 0

        def emit(true_speed_mps: float) -> None:
            nonlocal anchor, prev_dir, next_sample
            obs = min(L, max(0.0, along + rng.gauss(0, gps_noise_m)))
            lat, lon = profile.position(obs)
            inferred, anchor = infer_direction(anchor, prev_dir, obs)
            fixes.append(Fix(
                ts=ts, along_m=obs, lat=lat, lon=lon,
                speed_mps=max(0.0, true_speed_mps + rng.gauss(0, 0.4)) if true_speed_mps > 0 else 0.0,
                direction=inferred, true_along_m=along, true_direction=direction,
            ))
            prev_dir = inferred
            next_sample += sample_s

        while ts < end:
            trip_factor = math.exp(rng.gauss(0, TRIP_SIGMA))
            stops = sorted(profile.stops, key=lambda s: s.along_m * direction)
            # A stop within the departure terminus zone was already served by
            # the layover. Without this, a bus pulled away from Nagercoil
            # Junction, reached the stop 8.5 m further on, and waited at its
            # own terminus a second time.
            pending = [s for s in stops if (s.along_m - along) * direction > TERMINUS_STOP_ZONE_M]
            target = L if direction > 0 else 0.0
            piece_factor = math.exp(rng.gauss(0, PIECE_SIGMA))
            piece_left = PIECE_M
            wait = 0.0

            while (target - along) * direction > 0 and ts < end:
                if wait > 0:
                    step_speed = 0.0
                    wait -= 1.0
                else:
                    kmh = self.speed_kmh(along, ts) * day_factor * trip_factor * piece_factor
                    step_speed = max(1.0, kmh / 3.6)
                    move = min(step_speed, (target - along) * direction)
                    along += direction * move
                    piece_left -= move
                    if piece_left <= 0:
                        piece_left += PIECE_M
                        piece_factor = math.exp(rng.gauss(0, PIECE_SIGMA))
                        if rng.random() < self.halt_chance(along):
                            wait = rng.uniform(*HALT_S)
                    while pending and (along - pending[0].along_m) * direction >= 0:
                        wait += self.dwell_s(pending.pop(0), ts)

                ts += 1.0
                if ts >= next_sample:
                    emit(step_speed)

            # Layover at the terminus, then back the other way.
            layover = rng.uniform(*LAYOVER_S)
            layover_end = ts + layover
            while ts < layover_end and ts < end:
                ts += 1.0
                if ts >= next_sample:
                    emit(0.0)
            direction = -direction

        return fixes


def synthesize(
    profile: RouteProfile,
    first_day: date,
    days: int,
    buses: int = 3,
    mode: str = STRUCTURED,
    seed: int = 1,
) -> dict[date, list[list[Fix]]]:
    """Every bus's fixes for each day, keyed by day."""
    traffic = Traffic(profile, mode=mode, seed=seed)
    out: dict[date, list[list[Fix]]] = {}
    for d in range(days):
        day = first_day + timedelta(days=d)
        out[day] = [traffic.run_day(day, b) for b in range(buses)]
    return out
