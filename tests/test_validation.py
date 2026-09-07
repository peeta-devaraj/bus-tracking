"""Tests for the trust model.

These double as the live demonstration script: each rejection test below
corresponds to something you can show happening in real time on the admin
page's "reject log" panel.
"""

import pytest

from shared.auth import new_secret, sign, verify
from shared.validation import (
    CONF_LAST_KNOWN,
    CONF_LIVE,
    CONF_UNCERTAIN,
    FLAG_COARSE_ACCURACY,
    FLAG_NO_ROUTE,
    FLAG_OFF_ROUTE,
    REASON_BAD_COORDS,
    REASON_FUTURE_TIMESTAMP,
    REASON_REPLAYED_NONCE,
    REASON_STALE_TIMESTAMP,
    REASON_TELEPORT,
    REASON_TOO_FREQUENT,
    Fix,
    confidence_for,
    validate_fix,
)

NOW = 1_760_000_000.0

ROUTE = [
    (8.18880, 77.42900),
    (8.18600, 77.43050),
    (8.18300, 77.43220),
    (8.18000, 77.43350),
    (8.17800, 77.43400),
]


def fix(**kwargs) -> Fix:
    """A plausible on-route fix, overridable field by field."""
    defaults = dict(
        bus_id="NGL-01",
        lat=8.18300,
        lon=77.43220,
        ts=NOW,
        accuracy_m=12.0,
        speed_mps=8.0,
        heading=190.0,
        nonce="abc123",
    )
    defaults.update(kwargs)
    return Fix(**defaults)


class TestHappyPath:
    def test_a_normal_on_route_fix_is_accepted_cleanly(self):
        v = validate_fix(fix(), route=ROUTE, now=NOW)
        assert v.accepted
        assert v.reason is None
        assert v.flags == []
        assert v.quality == "good"

    def test_accepted_fix_reports_where_it_sits_on_the_route(self):
        v = validate_fix(fix(), route=ROUTE, now=NOW)
        assert v.snap is not None
        assert v.snap.offset_m < 5.0
        assert v.snap.along_m > 0.0

    def test_a_bus_moving_at_a_believable_speed_is_accepted(self):
        # About 380 m in 30 s, which is roughly 46 km/h. Brisk for a town bus
        # between stops, but entirely ordinary.
        previous = fix(ts=NOW - 30, lat=8.18600, lon=77.43050)
        v = validate_fix(fix(ts=NOW), previous=previous, route=ROUTE, now=NOW)
        assert v.accepted
        assert v.implied_speed_kmh == pytest.approx(46, abs=3)

    def test_the_speed_limit_sits_between_a_bus_and_a_spoof(self):
        # Sanity-check the threshold itself: 60 km/h passes, 120 does not.
        previous = fix(ts=NOW - 30, lat=8.18300, lon=77.43220)

        def moved(metres_per_30s: float) -> bool:
            # 0.00001 degrees of latitude is about 1.11 m.
            lat = 8.18300 + (metres_per_30s / 111_195.0)
            return validate_fix(
                fix(lat=lat, ts=NOW), previous=previous, now=NOW
            ).accepted

        assert moved(500)     # 60 km/h
        assert not moved(1000)  # 120 km/h


class TestRejections:
    """Each of these is a spoofing attempt that must never reach the map."""

    def test_teleport_is_rejected(self):
        # Claiming to be in Chennai 10 seconds after being in Nagercoil.
        previous = fix(ts=NOW - 10, lat=8.18300, lon=77.43220)
        v = validate_fix(
            fix(lat=13.0827, lon=80.2707, ts=NOW), previous=previous, now=NOW
        )
        assert not v.accepted
        assert v.reason == REASON_TELEPORT
        assert v.implied_speed_kmh is not None and v.implied_speed_kmh > 1000

    def test_replayed_old_timestamp_is_rejected(self):
        v = validate_fix(fix(ts=NOW - 600), route=ROUTE, now=NOW)
        assert not v.accepted
        assert v.reason == REASON_STALE_TIMESTAMP

    def test_timestamp_from_the_future_is_rejected(self):
        v = validate_fix(fix(ts=NOW + 600), route=ROUTE, now=NOW)
        assert not v.accepted
        assert v.reason == REASON_FUTURE_TIMESTAMP

    def test_reused_nonce_is_rejected(self):
        v = validate_fix(
            fix(nonce="seen-before"), route=ROUTE, now=NOW,
            seen_nonces={"seen-before"},
        )
        assert not v.accepted
        assert v.reason == REASON_REPLAYED_NONCE

    def test_flooding_faster_than_the_rate_limit_is_rejected(self):
        previous = fix(ts=NOW - 1)
        v = validate_fix(fix(ts=NOW), previous=previous, route=ROUTE, now=NOW)
        assert not v.accepted
        assert v.reason == REASON_TOO_FREQUENT

    def test_impossible_coordinates_are_rejected(self):
        assert validate_fix(fix(lat=91.0), now=NOW).reason == REASON_BAD_COORDS
        assert validate_fix(fix(lon=181.0), now=NOW).reason == REASON_BAD_COORDS

    def test_null_island_is_rejected(self):
        # An uninitialised GPS chip reports 0,0. It is not a bus.
        v = validate_fix(fix(lat=0.0, lon=0.0), now=NOW)
        assert not v.accepted
        assert v.reason == REASON_BAD_COORDS

    def test_rejections_carry_a_human_readable_detail(self):
        previous = fix(ts=NOW - 10)
        v = validate_fix(fix(lat=13.08, lon=80.27), previous=previous, now=NOW)
        assert v.detail  # something to show the driver and the reject log


class TestDegradations:
    """Plausible but weak reports are kept, and shown with less confidence."""

    def test_coarse_gps_is_flagged_not_rejected(self):
        v = validate_fix(fix(accuracy_m=500.0), route=ROUTE, now=NOW)
        assert v.accepted
        assert FLAG_COARSE_ACCURACY in v.flags
        assert v.quality == "degraded"

    def test_off_route_is_flagged_not_rejected(self):
        # A diversion 1 km east of the route. Real buses do this.
        v = validate_fix(fix(lon=77.43220 + 0.009), route=ROUTE, now=NOW)
        assert v.accepted, "diversions must not be thrown away"
        assert FLAG_OFF_ROUTE in v.flags

    def test_a_bus_with_no_assigned_route_is_flagged(self):
        # This is recording mode: the route does not exist yet.
        v = validate_fix(fix(), route=None, now=NOW)
        assert v.accepted
        assert FLAG_NO_ROUTE in v.flags

    def test_missing_accuracy_does_not_crash_or_flag(self):
        v = validate_fix(fix(accuracy_m=None), route=ROUTE, now=NOW)
        assert v.accepted
        assert FLAG_COARSE_ACCURACY not in v.flags


class TestConfidence:
    """What the rider actually sees. Freshness dominates everything else."""

    def test_fresh_clean_fix_is_live(self):
        assert confidence_for(age_s=5, flags=[]) == CONF_LIVE

    def test_slightly_old_fix_is_uncertain(self):
        assert confidence_for(age_s=60, flags=[]) == CONF_UNCERTAIN

    def test_old_fix_is_only_a_last_known_position(self):
        assert confidence_for(age_s=600, flags=[]) == CONF_LAST_KNOWN

    def test_a_perfect_but_old_fix_is_never_shown_as_live(self):
        # The single most important rule in the whole system.
        assert confidence_for(age_s=300, flags=[]) == CONF_LAST_KNOWN

    def test_fresh_but_off_route_is_downgraded(self):
        assert confidence_for(age_s=5, flags=[FLAG_OFF_ROUTE]) == CONF_UNCERTAIN

    def test_fresh_but_coarse_is_downgraded(self):
        assert confidence_for(age_s=5, flags=[FLAG_COARSE_ACCURACY]) == CONF_UNCERTAIN

    def test_confidence_degrades_monotonically_with_age(self):
        order = {CONF_LIVE: 0, CONF_UNCERTAIN: 1, CONF_LAST_KNOWN: 2}
        ages = [0, 10, 29, 31, 90, 119, 121, 500]
        levels = [order[confidence_for(a, [])] for a in ages]
        assert levels == sorted(levels)


class TestSigning:
    def test_a_correctly_signed_body_verifies(self):
        secret = new_secret()
        body = b'{"busId":"NGL-01","lat":8.183,"lon":77.4322}'
        assert verify(secret, body, sign(secret, body))

    def test_a_tampered_body_fails(self):
        secret = new_secret()
        body = b'{"busId":"NGL-01","lat":8.183,"lon":77.4322}'
        signature = sign(secret, body)
        tampered = b'{"busId":"NGL-01","lat":9.999,"lon":77.4322}'
        assert not verify(secret, tampered, signature)

    def test_another_buses_secret_does_not_work(self):
        body = b'{"busId":"NGL-01"}'
        assert not verify(new_secret(), body, sign(new_secret(), body))

    def test_missing_or_junk_signature_fails(self):
        secret = new_secret()
        body = b'{"busId":"NGL-01"}'
        assert not verify(secret, body, "")
        assert not verify(secret, body, "deadbeef")

    def test_signature_is_case_insensitive_and_whitespace_tolerant(self):
        # Header values pick up stray whitespace and casing in transit.
        secret = new_secret()
        body = b'{"busId":"NGL-01"}'
        signature = sign(secret, body)
        assert verify(secret, body, f"  {signature.upper()}  ")

    def test_secrets_are_unique(self):
        assert len({new_secret() for _ in range(200)}) == 200
