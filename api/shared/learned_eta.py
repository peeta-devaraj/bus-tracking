"""Arrival estimates learned from how buses have actually moved.

The naive estimator in eta.py divides remaining distance by one speed. That
cannot know that the stretch through Kottar crawls at 8 a.m. while the highway
to Kanyakumari does not, or that a bus loses two minutes at Anna Bus Stand.
This module learns exactly those things from stored position history.

The model
---------
Each route is cut into fixed-length bins (200 m). For every bin, direction of
travel and time-of-day band, it accumulates two numbers from real fixes:

    seconds spent in the bin     and     metres travelled through it

Their ratio is a *pace* in seconds per metre. Crucially, time spent standing
still -- at a stop, a junction, a jam -- lands in the seconds but not the
metres, so dwell is learned automatically without anyone labelling stops.

Predicting an arrival walks from the bus to the stop bin by bin, adding
pace x distance, and advancing a clock as it goes so a trip that runs into the
evening peak slows down part-way.

Where data is thin it degrades rather than guesses: band-specific pace, then
all-day pace for that bin, then a caller-supplied fallback. The share of the
distance covered by learned data is reported with every prediction.

Honesty rule
------------
The model does not know whether its history came from real buses or the
simulator. The caller records that (`simulated_share`) and the API must pass it
through. A learned estimate trained on simulated traffic is a demonstration of
the method, not a claim about Nagercoil's roads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

IST = timezone(timedelta(hours=5, minutes=30))

BIN_M = 200.0

# Consecutive fixes further apart than this are not joined: the bus may have
# done anything in between (lost signal, parked, gone off shift).
MAX_PAIR_GAP_S = 120.0

# Movement this small between fixes is GPS jitter on a bus standing still.
DWELL_MOVE_M = 8.0

# A fix may appear to step backwards by up to this much from GPS noise alone.
# Anything larger means the direction label is wrong for this pair.
BACKTRACK_TOLERANCE_M = 25.0

# Standing still this close to either end of the route is layover: the bus is
# waiting for its departure time, which depends on the timetable, not the road.
# Learning it would make every arrival at a terminus look minutes slower.
TERMINUS_ZONE_M = 150.0

# A single stationary spell longer than this mid-route is a parked bus or a
# driver's break, not traffic. Only the first part of it is learned.
MAX_DWELL_EPISODE_S = 300.0

# Effective traversals of a bin needed before its pace is trusted.
MIN_TRIPS = 3.0

# Calibration needs enough held-out predictions for quantiles to mean anything.
MIN_CALIBRATION_SAMPLES = 30

# Hours in IST. Nagercoil's day, not UTC's.
BANDS: tuple[tuple[str, int, int], ...] = (
    ("late", 0, 5),
    ("early", 5, 8),
    ("morning_peak", 8, 10),
    ("midday", 10, 16),
    ("evening_peak", 16, 20),
    ("night", 20, 23),
    ("late", 23, 24),
)
ALL_DAY = "all"


def time_band(ts: float) -> str:
    """Time-of-day band for an epoch timestamp, in Indian Standard Time."""
    hour = datetime.fromtimestamp(ts, IST).hour
    for name, start, end in BANDS:
        if start <= hour < end:
            return name
    return "late"


@dataclass(frozen=True)
class TrackPoint:
    """One fix reduced to what the model needs."""

    ts: float
    along_m: float
    direction: int  # +1 towards the end of the route, -1 back, 0 unknown


@dataclass
class BinStats:
    seconds: float = 0.0
    metres: float = 0.0

    @property
    def trips(self) -> float:
        """Evidence in units of 200 m travelled.

        For a standard 200 m bin that is how many times a bus passed through
        it. For a larger bin it is simply more evidence, which is fine: the
        threshold is about having seen enough road, not about bin size.
        """
        return self.metres / BIN_M

    @property
    def pace(self) -> float | None:
        return self.seconds / self.metres if self.metres > 0 else None


@dataclass
class Prediction:
    seconds: float
    low_seconds: float
    high_seconds: float
    learned_share: float  # fraction of the distance priced from history
    source: str           # "learned" | "mixed" | "fallback"


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of already-sorted values."""
    if not sorted_values:
        raise ValueError("no values")
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


class SegmentModel:
    """Learned pace per (direction, time band, bin) for one route."""

    def __init__(self, route_length_m: float, bin_m: float = BIN_M, use_bands: bool = True):
        """`bin_m` equal to the route length with `use_bands=False` gives one
        average pace per direction: the "average pace" baseline that separates
        the value of accounting for stops at all from the value of knowing
        where and when the road is slow.
        """
        if route_length_m <= 0:
            raise ValueError("route must have length")
        self.route_length_m = float(route_length_m)
        self.bin_m = float(bin_m)
        self.use_bands = use_bands
        self.n_bins = max(1, math.ceil(self.route_length_m / self.bin_m))
        self.stats: dict[tuple[int, str, int], BinStats] = {}

        # Range multipliers and a point-estimate bias correction. Uncalibrated
        # defaults match the naive estimator so an untrained model is no more
        # confident than what it replaces.
        self.range_low = 0.8
        self.range_high = 1.4
        self.bias = 1.0
        self.calibrated = False

        self.tracks_learned = 0
        self.pairs_learned = 0

    # ---------------------------------------------------------------- learning

    def _bin(self, along_m: float) -> int:
        return min(self.n_bins - 1, max(0, int(along_m // self.bin_m)))

    def _add(self, direction: int, band: str, b: int, seconds: float, metres: float) -> None:
        keys = ((direction, band, b), (direction, ALL_DAY, b)) if self.use_bands else ((direction, ALL_DAY, b),)
        for key in keys:
            st = self.stats.get(key)
            if st is None:
                st = self.stats[key] = BinStats()
            st.seconds += seconds
            st.metres += metres

    def learn_track(self, points: Iterable[TrackPoint]) -> int:
        """Absorb one bus's fixes. Returns how many fix pairs were usable."""
        pts = sorted(points, key=lambda p: p.ts)
        used = 0
        dwell_so_far = 0.0

        for p, q in zip(pts, pts[1:]):
            dt = q.ts - p.ts
            if dt <= 0 or dt > MAX_PAIR_GAP_S:
                dwell_so_far = 0.0
                continue

            d = q.direction
            # A pair straddling a turnaround belongs to neither direction.
            if d == 0 or p.direction != d:
                dwell_so_far = 0.0
                continue

            moved = (q.along_m - p.along_m) * d
            if moved < -BACKTRACK_TOLERANCE_M:
                continue

            band = time_band((p.ts + q.ts) / 2.0)

            if moved <= DWELL_MOVE_M:
                in_terminus = (
                    p.along_m <= TERMINUS_ZONE_M
                    or p.along_m >= self.route_length_m - TERMINUS_ZONE_M
                )
                if in_terminus:
                    continue
                # Standing still mid-route: the time belongs to where it stood,
                # up to the cap for a single spell.
                allowed = max(0.0, min(dt, MAX_DWELL_EPISODE_S - dwell_so_far))
                dwell_so_far += dt
                if allowed > 0:
                    self._add(d, band, self._bin(p.along_m), allowed, 0.0)
            else:
                dwell_so_far = 0.0
                lo = max(0.0, min(p.along_m, q.along_m))
                hi = min(self.route_length_m, max(p.along_m, q.along_m))
                span = hi - lo
                if span <= 0:
                    continue
                # Spread the elapsed time across every bin crossed, in
                # proportion to the distance covered inside each.
                b = self._bin(lo)
                while b < self.n_bins and b * self.bin_m < hi:
                    start = max(lo, b * self.bin_m)
                    end = min(hi, (b + 1) * self.bin_m)
                    if end > start:
                        self._add(d, band, b, dt * (end - start) / span, end - start)
                    b += 1
            used += 1

        if used:
            self.tracks_learned += 1
            self.pairs_learned += used
        return used

    # -------------------------------------------------------------- prediction

    def pace_for(self, direction: int, band: str, b: int) -> tuple[float | None, str]:
        st = self.stats.get((direction, band, b)) if self.use_bands else None
        if st is not None and st.trips >= MIN_TRIPS and st.pace:
            return st.pace, "band"
        st = self.stats.get((direction, ALL_DAY, b))
        if st is not None and st.trips >= MIN_TRIPS and st.pace:
            return st.pace, "all_day"
        return None, "none"

    def raw_seconds(
        self,
        direction: int,
        from_along_m: float,
        to_along_m: float,
        start_ts: float,
        fallback_pace: float,
    ) -> tuple[float, float] | None:
        """Uncorrected travel time and learned share, or None if not ahead."""
        if direction not in (1, -1):
            return None
        remaining = (to_along_m - from_along_m) * direction
        if remaining < 0:
            return None
        if remaining == 0:
            return 0.0, 1.0

        total = remaining
        pos = from_along_m
        clock = start_ts
        seconds = 0.0
        learned_m = 0.0

        # Hard stop on iterations: a route is at most a few hundred bins.
        for _ in range(self.n_bins * 4 + 10):
            if remaining <= 1e-6:
                break
            if direction > 0:
                b = self._bin(pos)
                step = min(remaining, (b + 1) * self.bin_m - pos)
            else:
                # Moving backwards from exactly a bin edge means the bin below.
                b = self._bin(pos - 1e-6)
                step = min(remaining, pos - b * self.bin_m)
            if step <= 1e-9:
                step = min(remaining, self.bin_m)

            pace, source = self.pace_for(direction, time_band(clock), b)
            if pace is None:
                pace = fallback_pace
            else:
                learned_m += step

            seconds += pace * step
            clock += pace * step
            pos += direction * step
            remaining -= step

        return seconds, learned_m / total

    def predict(
        self,
        direction: int,
        from_along_m: float,
        to_along_m: float,
        start_ts: float,
        fallback_pace: float,
    ) -> Prediction | None:
        raw = self.raw_seconds(direction, from_along_m, to_along_m, start_ts, fallback_pace)
        if raw is None:
            return None
        seconds, share = raw

        if share >= 0.999:
            source = "learned"
        elif share <= 0.001:
            source = "fallback"
        else:
            source = "mixed"

        # Calibration only describes learned predictions. Where the distance
        # is priced mostly by the fallback, keep the fallback's own range.
        if source == "fallback":
            point, low, high = seconds, seconds * 0.8, seconds * 1.4
        else:
            point = seconds * self.bias
            low, high = seconds * self.range_low, seconds * self.range_high

        return Prediction(
            seconds=point,
            low_seconds=min(low, point),
            high_seconds=max(high, point),
            learned_share=share,
            source=source,
        )

    # ------------------------------------------------------------- calibration

    def calibrate(self, ratios: Sequence[float], low_q: float = 0.1, high_q: float = 0.9) -> bool:
        """Set the range and bias from held-out actual/predicted time ratios.

        With the defaults, about 80% of arrivals should land inside the
        reported range. Returns False, leaving defaults, if there is too
        little evidence.
        """
        clean = sorted(r for r in ratios if math.isfinite(r) and r > 0)
        if len(clean) < MIN_CALIBRATION_SAMPLES:
            return False
        self.bias = _quantile(clean, 0.5)
        self.range_low = _quantile(clean, low_q)
        self.range_high = _quantile(clean, high_q)
        self.calibrated = True
        return True

    # ------------------------------------------------------------ persistence

    def to_rows(self) -> tuple[dict, list[dict]]:
        meta = {
            "routeLengthM": self.route_length_m,
            "binM": self.bin_m,
            "useBands": self.use_bands,
            "rangeLow": self.range_low,
            "rangeHigh": self.range_high,
            "bias": self.bias,
            "calibrated": self.calibrated,
            "tracksLearned": self.tracks_learned,
            "pairsLearned": self.pairs_learned,
        }
        rows = [
            {"direction": d, "band": band, "bin": b, "seconds": st.seconds, "metres": st.metres}
            for (d, band, b), st in self.stats.items()
        ]
        return meta, rows

    @classmethod
    def from_rows(cls, meta: dict, rows: Iterable[dict]) -> "SegmentModel":
        model = cls(
            float(meta["routeLengthM"]),
            float(meta.get("binM", BIN_M)),
            bool(meta.get("useBands", True)),
        )
        model.range_low = float(meta.get("rangeLow", 0.8))
        model.range_high = float(meta.get("rangeHigh", 1.4))
        model.bias = float(meta.get("bias", 1.0))
        model.calibrated = bool(meta.get("calibrated", False))
        model.tracks_learned = int(meta.get("tracksLearned", 0))
        model.pairs_learned = int(meta.get("pairsLearned", 0))
        for row in rows:
            key = (int(row["direction"]), str(row["band"]), int(row["bin"]))
            model.stats[key] = BinStats(float(row["seconds"]), float(row["metres"]))
        return model


def arrival_time(
    points: Sequence[TrackPoint],
    target_along_m: float,
    direction: int,
    after_ts: float,
) -> float | None:
    """When a recorded track actually reached target_along_m, travelling in
    `direction`, at or after after_ts. None if it turned round first.

    This is ground truth for evaluation, so it only interpolates between real
    fixes and never extrapolates.
    """
    pts = [p for p in sorted(points, key=lambda p: p.ts) if p.ts >= after_ts]
    for p, q in zip(pts, pts[1:]):
        if q.direction != direction:
            return None  # turned round before getting there
        a = (p.along_m - target_along_m) * direction
        b = (q.along_m - target_along_m) * direction
        if a <= 0 <= b:
            if b == a:
                return p.ts
            return p.ts + (q.ts - p.ts) * (-a / (b - a))
    return None
