"""Out-and-back excursion detection.

Found for real, not in theory: routing the Nagercoil stops over OpenStreetMap
produced a 286 m detour up a lane into Vadasery Market and an 86 m one into a
side street at Suchindram, because those stops snapped to service roads. A bus
does neither; it stops on the main road. These tests pin that behaviour down.
"""

import pytest

from shared.geo import find_excursions, haversine, remove_excursions, route_length_m

LAT = 8.18
STEP = 0.0001  # about 11 m of longitude at this latitude


def straight(n: int, start_lon: float = 77.43) -> list[tuple[float, float]]:
    return [(LAT, start_lon + i * STEP) for i in range(n)]


def with_spur(before: int, spur: int, after: int) -> list[tuple[float, float]]:
    """A road east, a dead-end lane north of `spur` vertices and back, then on east."""
    road = straight(before)
    base = road[-1]
    out = [(base[0] + k * STEP, base[1]) for k in range(1, spur + 1)]
    back = list(reversed(out[:-1])) + [base]
    rest = [(LAT, base[1] + k * STEP) for k in range(1, after + 1)]
    return road + out + back + rest


class TestFindExcursions:
    def test_a_straight_road_has_none(self):
        assert find_excursions(straight(50)) == []

    def test_finds_a_dead_end_lane(self):
        path = with_spur(before=10, spur=8, after=10)
        found = find_excursions(path)
        assert len(found) == 1
        ex = found[0]
        # The lane is about 8 x 11 m long, travelled there and back.
        assert ex.length_m == pytest.approx(2 * 8 * 11.1, rel=0.05)

    def test_the_tip_is_the_far_end_of_the_lane(self):
        path = with_spur(before=10, spur=8, after=10)
        ex = find_excursions(path)[0]
        base = path[ex.start]
        assert haversine(base[0], base[1], ex.tip[0], ex.tip[1]) == pytest.approx(8 * 11.1, rel=0.05)

    def test_ignores_detours_longer_than_the_limit(self):
        # About 440 m out and back, with a 300 m limit.
        path = with_spur(before=5, spur=20, after=5)
        assert find_excursions(path, max_length_m=300) == []
        assert len(find_excursions(path, max_length_m=600)) == 1

    def test_a_circular_route_is_not_a_spur(self):
        # A square loop about 2.2 km round that ends where it began. Real ring
        # routes exist; the length limit is what keeps them safe.
        side = 50
        east = [(LAT, 77.43 + i * STEP) for i in range(side)]
        north = [(LAT + i * STEP, 77.43 + side * STEP) for i in range(side)]
        west = [(LAT + side * STEP, 77.43 + (side - i) * STEP) for i in range(side)]
        south = [(LAT + (side - i) * STEP, 77.43) for i in range(side + 1)]
        loop = east + north + west + south
        assert route_length_m(loop) > 2000
        assert find_excursions(loop, max_length_m=600) == []

    def test_finds_separate_spurs_without_overlap(self):
        first = with_spur(before=10, spur=5, after=10)
        second_base_lon = first[-1][1]
        second = with_spur(before=1, spur=6, after=10)
        # Shift the second road so it continues east from the first.
        offset = second_base_lon - second[0][1] + STEP
        path = first + [(lat, lon + offset) for lat, lon in second]
        found = find_excursions(path)
        assert len(found) == 2
        assert found[0].end <= found[1].start

    def test_short_or_empty_paths(self):
        assert find_excursions([]) == []
        assert find_excursions([(LAT, 77.43)]) == []
        assert find_excursions(straight(2)) == []

    def test_standing_still_is_not_an_excursion(self):
        # A GPS trace sitting at a stop repeats the same coordinate. That is a
        # dwell, not a detour, and must not be cut out of a recorded trace.
        road = straight(10)
        dwell = [road[-1]] * 6
        rest = [(LAT, road[-1][1] + k * STEP) for k in range(1, 10)]
        assert find_excursions(road + dwell + rest) == []

    def test_gps_jitter_while_parked_is_not_an_excursion(self):
        # A parked bus wanders a few metres. That stays inside the return
        # radius, so it must not register as having gone anywhere.
        road = straight(10)
        base = road[-1]
        jitter = [(base[0] + dy, base[1] + dx) for dy, dx in
                  [(0.00002, 0), (0, 0.00002), (-0.00002, 0), (0, -0.00002), (0, 0)]]
        rest = [(LAT, base[1] + k * STEP) for k in range(1, 10)]
        assert find_excursions(road + jitter + rest) == []

    def test_a_long_dead_end_is_rejected_whole_not_trimmed(self):
        # Before this was fixed, a lane longer than the limit was detected from
        # part-way up it, and removal would have left a stub dangling off the
        # route.
        path = with_spur(before=5, spur=20, after=5)
        assert find_excursions(path, max_length_m=300) == []


class TestRemoveExcursions:
    def test_removing_the_spur_restores_the_through_road(self):
        path = with_spur(before=10, spur=8, after=10)
        cleaned = remove_excursions(path, find_excursions(path))
        through = straight(20)
        assert len(cleaned) == len(through)
        for got, want in zip(cleaned, through):
            assert got == pytest.approx(want)

    def test_removal_shortens_the_route_by_the_detour(self):
        path = with_spur(before=10, spur=8, after=10)
        found = find_excursions(path)
        cleaned = remove_excursions(path, found)
        saved = route_length_m(path) - route_length_m(cleaned)
        assert saved == pytest.approx(found[0].length_m, rel=0.01)

    def test_removing_nothing_changes_nothing(self):
        path = with_spur(before=10, spur=8, after=10)
        assert remove_excursions(path, []) == path

    def test_selective_removal_keeps_the_rest(self):
        # The route builder keeps spurs into bus stations and removes only the
        # ones caused by roadside stops, so partial removal has to work.
        first = with_spur(before=10, spur=5, after=10)
        offset = first[-1][1] - 77.43 + STEP
        second = [(lat, lon + offset) for lat, lon in with_spur(before=1, spur=6, after=10)]
        path = first + second
        found = find_excursions(path)
        kept_second = remove_excursions(path, [found[0]])
        assert len(find_excursions(kept_second)) == 1
