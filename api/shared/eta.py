"""Arrival estimation.

The week-one estimator is deliberately simple and deliberately honest:

    remaining distance along the route  /  a speed estimate  =  time

with the answer reported as a *range* rather than a single number. A bus
tracker that says "arriving in 7 minutes" and is wrong by four is worse than
one that says "6-11 min" and is right, because the second one taught the rider
how much to trust it.

Refusing to answer is a valid outcome. If the bus has already passed the stop,
or its position is too stale to reason about, this module returns None rather
than inventing a number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .validation import UNCERTAIN_MAX_AGE_S


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# A town bus in Nagercoil traffic, averaged over stops and junctions. Used
# when we have no better evidence from the bus itself.
DEFAULT_SPEED_KMH = _env_float("BUSTRACK_DEFAULT_SPEED_KMH", 18.0)
# Below this the bus is standing at a stop or in a jam; its instantaneous
# speed says nothing useful about the rest of the trip.
MIN_USABLE_SPEED_KMH = _env_float("BUSTRACK_MIN_USABLE_SPEED_KMH", 5.0)
# How wide to make the reported range, as multipliers on the point estimate.
RANGE_LOW = _env_float("BUSTRACK_ETA_RANGE_LOW", 0.8)
RANGE_HIGH = _env_float("BUSTRACK_ETA_RANGE_HIGH", 1.4)
# Treat anything within this distance as "arriving now".
ARRIVAL_RADIUS_M = _env_float("BUSTRACK_ARRIVAL_RADIUS_M", 80.0)


@dataclass
class Eta:
    bus_id: str
    label: str
    distance_m: float
    low_min: int
    high_min: int
    speed_kmh_used: float
    speed_source: str          # "reported" | "average" | "default"
    confidence: str
    is_simulated: bool

    @property
    def text(self) -> str:
        if self.low_min == 0 and self.high_min <= 1:
            return "arriving"
        if self.low_min == self.high_min:
            return f"{self.low_min} min"
        return f"{self.low_min}-{self.high_min} min"

    def to_dict(self) -> dict:
        return {
            "busId": self.bus_id,
            "label": self.label,
            "distanceM": round(self.distance_m),
            "lowMin": self.low_min,
            "highMin": self.high_min,
            "text": self.text,
            "speedKmh": round(self.speed_kmh_used, 1),
            "speedSource": self.speed_source,
            "confidence": self.confidence,
            "isSimulated": self.is_simulated,
        }


def choose_speed_kmh(
    reported_mps: float | None,
    average_kmh: float | None = None,
) -> tuple[float, str]:
    """Pick the most trustworthy speed available, and say which one it was.

    Naming the source matters: it lets the UI explain *why* an estimate is
    weak instead of just showing a wide range.
    """
    if reported_mps is not None:
        reported_kmh = reported_mps * 3.6
        if reported_kmh >= MIN_USABLE_SPEED_KMH:
            return reported_kmh, "reported"

    if average_kmh is not None and average_kmh >= MIN_USABLE_SPEED_KMH:
        return average_kmh, "average"

    return DEFAULT_SPEED_KMH, "default"


def estimate(
    *,
    bus_id: str,
    label: str,
    bus_along_m: float | None,
    stop_along_m: float,
    age_s: float,
    confidence: str,
    reported_speed_mps: float | None = None,
    average_speed_kmh: float | None = None,
    is_simulated: bool = False,
    route_length_m: float | None = None,
    loops: bool = False,
) -> Eta | None:
    """Estimate when one bus reaches one stop, or None if we cannot say.

    Set `loops` for a circular route, where a bus that has passed the stop will
    come round to it again rather than never arriving.
    """
    if bus_along_m is None:
        return None

    # Too old to reason about. Showing a countdown from a four-minute-old fix
    # is exactly the lie this project exists to avoid.
    if age_s > UNCERTAIN_MAX_AGE_S:
        return None

    remaining = stop_along_m - bus_along_m

    if remaining < 0:
        if not loops or not route_length_m:
            return None  # already passed, and not coming back
        remaining += route_length_m

    if remaining <= ARRIVAL_RADIUS_M:
        return Eta(
            bus_id=bus_id,
            label=label,
            distance_m=max(0.0, remaining),
            low_min=0,
            high_min=1,
            speed_kmh_used=0.0,
            speed_source="arrived",
            confidence=confidence,
            is_simulated=is_simulated,
        )

    speed_kmh, source = choose_speed_kmh(reported_speed_mps, average_speed_kmh)
    minutes = (remaining / 1000.0) / speed_kmh * 60.0

    low = max(0, int(minutes * RANGE_LOW))
    high = max(low + 1, int(round(minutes * RANGE_HIGH)))

    return Eta(
        bus_id=bus_id,
        label=label,
        distance_m=remaining,
        low_min=low,
        high_min=high,
        speed_kmh_used=speed_kmh,
        speed_source=source,
        confidence=confidence,
        is_simulated=is_simulated,
    )


def average_speed_kmh_from_history(history: list[dict]) -> float | None:
    """Rolling average speed from a bus's recent fixes.

    History rows arrive newest-first. Returns None when there is not enough
    movement to say anything, which is the common case for a bus that has been
    sitting at a terminus.
    """
    from .geo import haversine

    usable = [h for h in history if h.get("lat") is not None][:20]
    if len(usable) < 2:
        return None

    total_m = 0.0
    total_s = 0.0
    for newer, older in zip(usable, usable[1:]):
        dt = float(newer["ts"]) - float(older["ts"])
        if dt <= 0 or dt > 300:  # ignore gaps longer than five minutes
            continue
        total_m += haversine(older["lat"], older["lon"], newer["lat"], newer["lon"])
        total_s += dt

    if total_s < 30 or total_m < 50:
        return None

    return (total_m / total_s) * 3.6
