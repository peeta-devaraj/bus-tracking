"""Tests for the learned arrival model and direction handling."""

from datetime import datetime

import pytest

from shared.eta import estimate, infer_direction
from shared.learned_eta import (
    IST,
    MAX_DWELL_EPISODE_S,
    SegmentModel,
    TrackPoint,
    arrival_time,
    time_band,
)

ROUTE_M = 4000.0


def at_ist(hour: int, minute: int = 0, day: int = 14) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST).timestamp()


def drive(
    start_ts: float,
    speed_mps,
    direction: int = 1,
    start_m: float | None = None,
    end_m: float | None = None,
    dwell: dict | None = None,
    sample_s: float = 10.0,
) -> list[TrackPoint]:
    """Drive a bus along the route at speed_mps(along, ts), sampling like a phone.

    `dwell` maps an along position to seconds to stand still on reaching it.
    """
    start_m = (0.0 if direction > 0 else ROUTE_M) if start_m is None else start_m
    end_m = (ROUTE_M if direction > 0 else 0.0) if end_m is None else end_m
    dwell = dict(dwell or {})

    ts, along = start_ts, start_m
    points = [TrackPoint(ts, along, direction)]
    next_sample = ts + sample_s
    pending_dwell = 0.0

    while (end_m - along) * direction > 0:
        if pending_dwell > 0:
            pending_dwell -= 1.0
        else:
            along += direction * speed_mps(along, ts)
            if (along - end_m) * direction > 0:
                along = end_m
            for spot in list(dwell):
                if (along - spot) * direction >= 0:
                    pending_dwell = dwell.pop(spot)
        ts += 1.0
        if ts >= next_sample:
            points.append(TrackPoint(ts, along, direction))
            next_sample += sample_s
    points.append(TrackPoint(ts, along, direction))
    return points


def constant(v):
    return lambda along, ts: v


def trained(tracks) -> SegmentModel:
    model = SegmentModel(ROUTE_M)
    for t in tracks:
        model.learn_track(t)
    return model


FALLBACK_PACE = 1 / 5.0  # 18 km/h, the naive default


class TestTimeBands:
    @pytest.mark.parametrize(
        "hour,minute,band",
        [(4, 59, "late"), (5, 0, "early"), (8, 30, "morning_peak"), (12, 0, "midday"),
         (17, 45, "evening_peak"), (21, 0, "night"), (23, 30, "late")],
    )
    def test_bands_follow_indian_time_not_utc(self, hour, minute, band):
        assert time_band(at_ist(hour, minute)) == band


def follow(positions, direction=0):
    """Feed positions through infer_direction the way ingest does."""
    anchor, out = None, []
    for along in positions:
        direction, anchor = infer_direction(anchor, direction, along)
        out.append(direction)
    return out


class TestDirection:
    def test_unknown_until_the_bus_clearly_moves(self):
        assert follow([500.0, 505.0, 498.0]) == [0, 0, 0]

    def test_a_clear_move_sets_direction(self):
        assert follow([500.0, 560.0])[-1] == 1
        assert follow([500.0, 440.0])[-1] == -1

    def test_gps_jitter_does_not_flip_a_waiting_bus(self):
        # Scatter around a bus parked at 500 m, arriving forwards.
        jitter = [500.0, 488.0, 511.0, 494.0, 507.0, 490.0, 509.0]
        assert set(follow(jitter, direction=1)) == {1}

    def test_turning_round_flips_it(self):
        assert follow([3990.0, 3900.0], direction=1)[-1] == -1

    def test_a_slow_crawl_still_registers(self):
        # 15 m between pings, never enough on its own. The previous version
        # compared consecutive pings and stayed stuck on the old direction for
        # the whole crawl; from an anchor the moves add up.
        crawl = [3990.0 - 15.0 * k for k in range(6)]
        assert follow(crawl, direction=1)[-1] == -1


class TestNaiveEstimatorDirection:
    """Before direction existed, a bus driving away from a stop got a countdown."""

    def common(self, **kw):
        base = dict(bus_id="B", label="B", stop_along_m=2000.0, age_s=5,
                    confidence="live", reported_speed_mps=6.0)
        base.update(kw)
        return estimate(**base)

    def test_a_bus_driving_away_gets_no_countdown(self):
        # At 1000 m heading back towards the start: the stop at 2000 m is behind it.
        assert self.common(bus_along_m=1000.0, direction=-1) is None

    def test_a_bus_on_the_return_leg_is_timed_correctly(self):
        # At 3000 m heading back: the stop at 2000 m is 1 km ahead of it.
        eta = self.common(bus_along_m=3000.0, direction=-1)
        assert eta is not None
        assert eta.distance_m == pytest.approx(1000.0)

    def test_unknown_direction_gets_no_countdown(self):
        assert self.common(bus_along_m=1000.0, direction=0) is None

    def test_forward_behaviour_is_unchanged_by_default(self):
        eta = self.common(bus_along_m=1000.0)
        assert eta is not None and eta.distance_m == pytest.approx(1000.0)


class TestLearning:
    def test_constant_speed_learns_that_speed_everywhere(self):
        model = trained(drive(at_ist(12) + i * 3600, constant(8.0)) for i in range(4))
        pred = model.predict(1, 0.0, 2000.0, at_ist(12), FALLBACK_PACE)
        assert pred.source == "learned"
        assert pred.seconds == pytest.approx(2000 / 8.0, rel=0.05)

    def test_a_slow_stretch_is_learned_where_it_is(self):
        # The first kilometre crawls at 3 m/s (a town centre), the rest runs at 12.
        speed = lambda along, ts: 3.0 if along < 1000 else 12.0
        model = trained(drive(at_ist(12) + i * 3600, speed) for i in range(4))

        through_town = model.predict(1, 0.0, 1000.0, at_ist(12), FALLBACK_PACE).seconds
        open_road = model.predict(1, 2000.0, 3000.0, at_ist(12), FALLBACK_PACE).seconds
        assert through_town == pytest.approx(1000 / 3.0, rel=0.08)
        assert open_road == pytest.approx(1000 / 12.0, rel=0.08)

    def test_rush_hour_is_learned_separately_from_midday(self):
        tracks = [drive(at_ist(8, 15, day=d), constant(3.0)) for d in (7, 8, 9, 10)]
        tracks += [drive(at_ist(12, 0, day=d), constant(10.0)) for d in (7, 8, 9, 10)]
        model = trained(tracks)
        peak = model.predict(1, 0.0, 1000.0, at_ist(8, 15), FALLBACK_PACE).seconds
        midday = model.predict(1, 0.0, 1000.0, at_ist(12, 0), FALLBACK_PACE).seconds
        assert peak > 2.5 * midday

    def test_time_spent_at_a_stop_is_learned_without_labelling_stops(self):
        with_stop = [drive(at_ist(12) + i * 3600, constant(8.0), dwell={1500.0: 90}) for i in range(4)]
        model = trained(with_stop)
        across = model.predict(1, 1000.0, 2000.0, at_ist(12), FALLBACK_PACE).seconds
        # 1 km at 8 m/s plus the 90 s at the stop.
        assert across == pytest.approx(1000 / 8.0 + 90, rel=0.1)

    def test_layover_at_a_terminus_is_not_learned(self):
        tracks = []
        for i in range(4):
            out = drive(at_ist(12) + i * 3600, constant(8.0))
            # Six minutes sitting at the far terminus before turning round.
            end = out[-1]
            parked = [TrackPoint(end.ts + 10 * k, end.along_m, 1) for k in range(1, 37)]
            tracks.append(out + parked)
        model = trained(tracks)
        last_stretch = model.predict(1, ROUTE_M - 1000.0, ROUTE_M, at_ist(12), FALLBACK_PACE).seconds
        assert last_stretch == pytest.approx(1000 / 8.0, rel=0.1)

    def test_a_long_standstill_mid_route_is_capped(self):
        # A 40-minute driver's break must not be taught as traffic.
        tracks = [drive(at_ist(12) + i * 7200, constant(8.0), dwell={2000.0: 2400}) for i in range(4)]
        model = trained(tracks)
        across = model.predict(1, 1800.0, 2200.0, at_ist(12), FALLBACK_PACE).seconds
        # 400 m at 8 m/s is 50 s, plus at most the five-minute cap. The bound is
        # written out as numbers on purpose: an earlier version computed it
        # from MAX_DWELL_EPISODE_S, so raising the cap raised the bound too and
        # the test could never fail. Uncapped, this comes out near 2,450 s.
        assert MAX_DWELL_EPISODE_S == 300.0
        assert across == pytest.approx(50 + 300, abs=30)

    def test_directions_are_learned_independently(self):
        tracks = [drive(at_ist(12) + i * 3600, constant(4.0), direction=1) for i in range(4)]
        tracks += [drive(at_ist(12) + i * 3600 + 1800, constant(12.0), direction=-1) for i in range(4)]
        model = trained(tracks)
        outbound = model.predict(1, 1000.0, 2000.0, at_ist(12), FALLBACK_PACE).seconds
        inbound = model.predict(-1, 2000.0, 1000.0, at_ist(12), FALLBACK_PACE).seconds
        assert outbound == pytest.approx(1000 / 4.0, rel=0.08)
        assert inbound == pytest.approx(1000 / 12.0, rel=0.08)

    def test_turnaround_pairs_are_ignored(self):
        model = SegmentModel(ROUTE_M)
        used = model.learn_track([
            TrackPoint(0, 3900.0, 1), TrackPoint(10, 3990.0, 1),
            TrackPoint(20, 3900.0, -1), TrackPoint(30, 3800.0, -1),
        ])
        assert used == 2  # the pair spanning the turn is skipped

    def test_gaps_in_the_signal_are_not_joined(self):
        model = SegmentModel(ROUTE_M)
        # Ten minutes of silence: the bus could have done anything.
        assert model.learn_track([TrackPoint(0, 100.0, 1), TrackPoint(600, 2100.0, 1)]) == 0

    def test_unknown_direction_is_not_learned(self):
        model = SegmentModel(ROUTE_M)
        assert model.learn_track([TrackPoint(0, 100.0, 0), TrackPoint(10, 180.0, 0)]) == 0


class TestPrediction:
    def test_no_history_uses_the_fallback_and_says_so(self):
        model = SegmentModel(ROUTE_M)
        pred = model.predict(1, 0.0, 1000.0, at_ist(12), FALLBACK_PACE)
        assert pred.source == "fallback"
        assert pred.learned_share == 0.0
        assert pred.seconds == pytest.approx(1000 * FALLBACK_PACE)

    def test_one_trip_is_not_enough_to_trust(self):
        model = trained([drive(at_ist(12), constant(8.0))])
        assert model.predict(1, 0.0, 1000.0, at_ist(12), FALLBACK_PACE).source == "fallback"

    def test_band_falls_back_to_all_day_before_the_default(self):
        # Only midday data, but a question at night: use what the bin knows.
        model = trained(drive(at_ist(12) + i * 3600, constant(8.0)) for i in range(4))
        pred = model.predict(1, 0.0, 1000.0, at_ist(21), FALLBACK_PACE)
        assert pred.source == "learned"
        assert pred.seconds == pytest.approx(1000 / 8.0, rel=0.05)

    def test_a_stop_behind_the_bus_has_no_prediction(self):
        model = SegmentModel(ROUTE_M)
        assert model.predict(1, 2000.0, 1000.0, at_ist(12), FALLBACK_PACE) is None
        assert model.predict(-1, 1000.0, 2000.0, at_ist(12), FALLBACK_PACE) is None
        assert model.predict(0, 1000.0, 2000.0, at_ist(12), FALLBACK_PACE) is None

    def test_a_trip_running_into_the_peak_slows_down_part_way(self):
        tracks = [drive(at_ist(6, 0, day=d), constant(12.0)) for d in (7, 8, 9, 10)]
        tracks += [drive(at_ist(8, 30, day=d), constant(3.0)) for d in (7, 8, 9, 10)]
        model = trained(tracks)
        # Leaving at 07:58 the bus crosses 08:00 early on, so the rest of the
        # route should be priced at peak pace.
        crossing = model.predict(1, 0.0, 3000.0, at_ist(7, 58), FALLBACK_PACE).seconds
        all_early = 3000 / 12.0
        assert crossing > 2 * all_early

    def test_the_range_always_contains_the_point(self):
        model = trained(drive(at_ist(12) + i * 3600, constant(8.0)) for i in range(4))
        pred = model.predict(1, 0.0, 2000.0, at_ist(12), FALLBACK_PACE)
        assert pred.low_seconds <= pred.seconds <= pred.high_seconds


class TestCalibration:
    def test_refuses_with_too_little_evidence(self):
        model = SegmentModel(ROUTE_M)
        assert model.calibrate([1.0] * 10) is False
        assert (model.range_low, model.range_high, model.bias) == (0.8, 1.4, 1.0)

    def test_sets_bias_and_range_from_held_out_ratios(self):
        model = SegmentModel(ROUTE_M)
        ratios = [0.9 + 0.01 * i for i in range(41)]  # 0.90 .. 1.30
        assert model.calibrate(ratios)
        assert model.bias == pytest.approx(1.10)
        assert model.range_low == pytest.approx(0.94)
        assert model.range_high == pytest.approx(1.26)

    def test_ignores_garbage_ratios(self):
        model = SegmentModel(ROUTE_M)
        assert model.calibrate([1.0] * 29 + [float("nan"), float("inf"), -1.0, 0.0]) is False


class TestPersistence:
    def test_round_trip_predicts_identically(self):
        speed = lambda along, ts: 3.0 if along < 1000 else 12.0
        model = trained(drive(at_ist(12) + i * 3600, speed) for i in range(4))
        model.calibrate([0.9 + 0.01 * i for i in range(41)])

        meta, rows = model.to_rows()
        restored = SegmentModel.from_rows(meta, rows)
        for a, b in [(0.0, 3000.0), (500.0, 1500.0)]:
            original = model.predict(1, a, b, at_ist(12), FALLBACK_PACE)
            again = restored.predict(1, a, b, at_ist(12), FALLBACK_PACE)
            assert again.seconds == pytest.approx(original.seconds)
            assert again.high_seconds == pytest.approx(original.high_seconds)
        assert restored.calibrated


class TestArrivalTime:
    def test_interpolates_between_fixes(self):
        track = [TrackPoint(0, 0.0, 1), TrackPoint(10, 100.0, 1), TrackPoint(20, 200.0, 1)]
        assert arrival_time(track, 150.0, 1, after_ts=0) == pytest.approx(15.0)

    def test_none_if_the_bus_turns_round_first(self):
        track = [TrackPoint(0, 0.0, 1), TrackPoint(10, 100.0, 1), TrackPoint(20, 50.0, -1)]
        assert arrival_time(track, 150.0, 1, after_ts=0) is None

    def test_only_counts_arrivals_after_the_question(self):
        track = [TrackPoint(0, 0.0, 1), TrackPoint(10, 100.0, 1), TrackPoint(20, 200.0, 1)]
        assert arrival_time(track, 50.0, 1, after_ts=10) is None
