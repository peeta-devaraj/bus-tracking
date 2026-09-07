"""Plausibility checks and confidence scoring for incoming position reports.

This is the part of the system that decides whether a claimed bus position is
allowed to become "truth". Two separate ideas live here and they must not be
confused:

  * REJECTION -- the report is impossible or hostile, so it never lands.
    A rejected report leaves no trace on the map.

  * DEGRADATION -- the report is plausible but weak (coarse GPS, off the
    expected route). It is stored, but it is shown to riders with reduced
    confidence rather than being silently dropped. Real buses take diversions;
    throwing those reports away would make the system lie by omission.

Every rejection carries a machine-readable reason code so the driver page can
say something useful and so the behaviour can be demonstrated on request.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .geo import SnapResult, haversine, snap_to_route

# --------------------------------------------------------------------------
# Thresholds. Overridable by app setting so they can be tuned without a
# redeploy -- useful when demoing the rejection paths live.
# --------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_SPEED_KMH = _env_float("BUSTRACK_MAX_SPEED_KMH", 90.0)
MAX_ACCURACY_M = _env_float("BUSTRACK_MAX_ACCURACY_M", 100.0)
OFF_ROUTE_M = _env_float("BUSTRACK_OFF_ROUTE_M", 150.0)
MAX_CLOCK_SKEW_S = _env_float("BUSTRACK_MAX_CLOCK_SKEW_S", 120.0)
MIN_PING_INTERVAL_S = _env_float("BUSTRACK_MIN_PING_INTERVAL_S", 5.0)

# Freshness bands used when rendering confidence to riders.
LIVE_MAX_AGE_S = _env_float("BUSTRACK_LIVE_MAX_AGE_S", 30.0)
UNCERTAIN_MAX_AGE_S = _env_float("BUSTRACK_UNCERTAIN_MAX_AGE_S", 120.0)


# Rejection reasons
REASON_BAD_COORDS = "bad_coords"
REASON_FUTURE_TIMESTAMP = "future_timestamp"
REASON_STALE_TIMESTAMP = "stale_timestamp"
REASON_TELEPORT = "teleport"
REASON_TOO_FREQUENT = "too_frequent"
REASON_REPLAYED_NONCE = "replayed_nonce"

# Degradation flags
FLAG_COARSE_ACCURACY = "coarse_accuracy"
FLAG_OFF_ROUTE = "off_route"
FLAG_NO_ROUTE = "no_route"

# Confidence levels surfaced to riders
CONF_LIVE = "live"
CONF_UNCERTAIN = "uncertain"
CONF_LAST_KNOWN = "last_known"


@dataclass
class Fix:
    """One position report, already parsed out of the request body."""

    bus_id: str
    lat: float
    lon: float
    ts: float                    # epoch seconds, as claimed by the device
    accuracy_m: float | None = None
    speed_mps: float | None = None
    heading: float | None = None
    nonce: str = ""


@dataclass
class Verdict:
    accepted: bool
    reason: str | None = None
    detail: str = ""
    flags: list[str] = field(default_factory=list)
    snap: SnapResult | None = None
    implied_speed_kmh: float | None = None

    @property
    def quality(self) -> str:
        """Ingest-time quality, before freshness is taken into account."""
        return "degraded" if self.flags else "good"


def validate_fix(
    fix: Fix,
    previous: Fix | None = None,
    route: list[tuple[float, float]] | None = None,
    now: float | None = None,
    seen_nonces: set[str] | None = None,
) -> Verdict:
    """Decide whether a position report is admissible, and how much to trust it.

    `previous` is the last accepted fix for the same bus, used for the teleport
    and rate checks. `route` is the assigned route polyline, used for the
    off-route check; None means the bus has no assigned route yet (recording
    mode), which is flagged but never rejected.
    """
    now = time.time() if now is None else now
    flags: list[str] = []

    # --- Coordinates must be real ---------------------------------------
    if not (-90.0 <= fix.lat <= 90.0) or not (-180.0 <= fix.lon <= 180.0):
        return Verdict(False, REASON_BAD_COORDS, f"lat={fix.lat} lon={fix.lon}")
    if fix.lat == 0.0 and fix.lon == 0.0:
        # Null Island: almost always an uninitialised GPS rather than a bus.
        return Verdict(False, REASON_BAD_COORDS, "null island")

    # --- Replay protection ----------------------------------------------
    age = now - fix.ts
    if age < -MAX_CLOCK_SKEW_S:
        return Verdict(
            False, REASON_FUTURE_TIMESTAMP, f"{-age:.0f}s in the future"
        )
    if age > MAX_CLOCK_SKEW_S:
        return Verdict(
            False, REASON_STALE_TIMESTAMP, f"{age:.0f}s old, limit {MAX_CLOCK_SKEW_S:.0f}s"
        )
    if seen_nonces is not None and fix.nonce and fix.nonce in seen_nonces:
        return Verdict(False, REASON_REPLAYED_NONCE, fix.nonce)

    # --- Rate limit and teleport check ----------------------------------
    implied_speed_kmh: float | None = None
    if previous is not None:
        dt = fix.ts - previous.ts

        if 0 <= dt < MIN_PING_INTERVAL_S:
            return Verdict(
                False, REASON_TOO_FREQUENT,
                f"{dt:.1f}s since last fix, minimum {MIN_PING_INTERVAL_S:.0f}s",
            )

        if dt > 0:
            distance = haversine(previous.lat, previous.lon, fix.lat, fix.lon)
            implied_speed_kmh = (distance / dt) * 3.6
            if implied_speed_kmh > MAX_SPEED_KMH:
                return Verdict(
                    False, REASON_TELEPORT,
                    f"{distance:.0f}m in {dt:.1f}s = {implied_speed_kmh:.0f} km/h",
                    implied_speed_kmh=implied_speed_kmh,
                )

    # --- Degradations: accepted, but trusted less -----------------------
    if fix.accuracy_m is not None and fix.accuracy_m > MAX_ACCURACY_M:
        flags.append(FLAG_COARSE_ACCURACY)

    snap: SnapResult | None = None
    if route:
        snap = snap_to_route((fix.lat, fix.lon), route)
        if snap is not None and snap.offset_m > OFF_ROUTE_M:
            flags.append(FLAG_OFF_ROUTE)
    else:
        flags.append(FLAG_NO_ROUTE)

    return Verdict(
        accepted=True,
        flags=flags,
        snap=snap,
        implied_speed_kmh=implied_speed_kmh,
    )


def confidence_for(age_s: float, flags: list[str] | None = None) -> str:
    """Combine freshness and ingest flags into what the rider actually sees.

    Freshness dominates: a perfect fix from four minutes ago is still only a
    last-known position, and drawing it as live is exactly the behaviour that
    destroys trust in bus tracking apps.
    """
    flags = flags or []

    if age_s > UNCERTAIN_MAX_AGE_S:
        return CONF_LAST_KNOWN
    if age_s > LIVE_MAX_AGE_S:
        return CONF_UNCERTAIN
    # Fresh, but weak for another reason.
    if FLAG_OFF_ROUTE in flags or FLAG_COARSE_ACCURACY in flags:
        return CONF_UNCERTAIN
    return CONF_LIVE
