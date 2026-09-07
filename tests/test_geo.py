import math

import pytest

from shared.geo import (
    bearing,
    cumulative_distances,
    decode_polyline,
    densify,
    encode_polyline,
    haversine,
    point_at_distance,
    route_length_m,
    snap_to_route,
)

# A short stretch of real road in Nagercoil, roughly Vadasery bus stand
# heading south-west towards the town centre.
NAGERCOIL_ROUTE = [
    (8.18880, 77.42900),
    (8.18600, 77.43050),
    (8.18300, 77.43220),
    (8.18000, 77.43350),
    (8.17800, 77.43400),
]


class TestHaversine:
    def test_zero_distance(self):
        assert haversine(8.1888, 77.4290, 8.1888, 77.4290) == pytest.approx(0.0)

    def test_one_degree_of_latitude_is_about_111km(self):
        d = haversine(8.0, 77.0, 9.0, 77.0)
        assert d == pytest.approx(111_195, rel=0.001)

    def test_known_intercity_distance(self):
        # Nagercoil to Kanyakumari is about 20 km by air.
        d = haversine(8.1780, 77.4340, 8.0883, 77.5385)
        assert 14_000 < d < 17_000

    def test_is_symmetric(self):
        a = haversine(8.1888, 77.4290, 8.1780, 77.4340)
        b = haversine(8.1780, 77.4340, 8.1888, 77.4290)
        assert a == pytest.approx(b)


class TestBearing:
    def test_due_north(self):
        assert bearing(8.0, 77.0, 9.0, 77.0) == pytest.approx(0.0, abs=0.1)

    def test_due_east(self):
        assert bearing(8.0, 77.0, 8.0, 78.0) == pytest.approx(90.0, abs=0.5)

    def test_always_in_range(self):
        for lat, lon in [(9.0, 76.0), (7.0, 78.0), (8.0, 76.5)]:
            assert 0.0 <= bearing(8.0, 77.0, lat, lon) < 360.0


class TestPolyline:
    def test_roundtrip_preserves_points_to_five_decimals(self):
        encoded = encode_polyline(NAGERCOIL_ROUTE)
        decoded = decode_polyline(encoded)
        assert len(decoded) == len(NAGERCOIL_ROUTE)
        for (olat, olon), (dlat, dlon) in zip(NAGERCOIL_ROUTE, decoded):
            assert dlat == pytest.approx(olat, abs=1e-5)
            assert dlon == pytest.approx(olon, abs=1e-5)

    def test_encoding_is_compact(self):
        # The whole point of encoding is that it beats storing JSON floats.
        encoded = encode_polyline(NAGERCOIL_ROUTE)
        naive = str([[round(a, 5), round(b, 5)] for a, b in NAGERCOIL_ROUTE])
        assert len(encoded) < len(naive) / 2

    def test_matches_reference_implementation(self):
        # The canonical example from Google's polyline algorithm documentation.
        points = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        assert encode_polyline(points) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

    def test_decodes_reference_string(self):
        decoded = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        assert decoded == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]

    def test_empty_input(self):
        assert encode_polyline([]) == ""
        assert decode_polyline("") == []

    def test_truncated_input_does_not_hang(self):
        encoded = encode_polyline(NAGERCOIL_ROUTE)
        # Chopping the string must return partial data, not loop forever.
        assert isinstance(decode_polyline(encoded[:5]), list)


class TestRouteMeasurement:
    def test_cumulative_starts_at_zero_and_increases(self):
        cum = cumulative_distances(NAGERCOIL_ROUTE)
        assert cum[0] == 0.0
        assert all(cum[i] <= cum[i + 1] for i in range(len(cum) - 1))

    def test_route_length_matches_final_cumulative(self):
        assert route_length_m(NAGERCOIL_ROUTE) == pytest.approx(
            cumulative_distances(NAGERCOIL_ROUTE)[-1]
        )

    def test_degenerate_routes_have_no_length(self):
        assert route_length_m([]) == 0.0
        assert route_length_m([(8.1, 77.4)]) == 0.0


class TestSnapToRoute:
    def test_point_on_the_route_snaps_with_near_zero_offset(self):
        snap = snap_to_route(NAGERCOIL_ROUTE[2], NAGERCOIL_ROUTE)
        assert snap is not None
        assert snap.offset_m < 1.0

    def test_along_distance_grows_as_the_bus_progresses(self):
        first = snap_to_route(NAGERCOIL_ROUTE[1], NAGERCOIL_ROUTE)
        later = snap_to_route(NAGERCOIL_ROUTE[3], NAGERCOIL_ROUTE)
        assert first is not None and later is not None
        assert later.along_m > first.along_m

    def test_start_and_end_anchor_the_measurement(self):
        start = snap_to_route(NAGERCOIL_ROUTE[0], NAGERCOIL_ROUTE)
        end = snap_to_route(NAGERCOIL_ROUTE[-1], NAGERCOIL_ROUTE)
        assert start is not None and end is not None
        assert start.along_m == pytest.approx(0.0, abs=1.0)
        assert end.along_m == pytest.approx(route_length_m(NAGERCOIL_ROUTE), abs=1.0)

    def test_offset_reflects_real_distance_from_the_road(self):
        # This route runs roughly north to south, so displacing east of a
        # middle vertex is a genuine perpendicular offset. 0.009 degrees of
        # longitude at this latitude is a little under 1 km.
        mid_lat, mid_lon = NAGERCOIL_ROUTE[2]
        off_route = (mid_lat, mid_lon + 0.009)
        snap = snap_to_route(off_route, NAGERCOIL_ROUTE)
        assert snap is not None
        assert 800 < snap.offset_m < 1050

    def test_point_before_the_start_clamps_to_the_start(self):
        # North of the first vertex is off the far end of the route. It must
        # clamp to the start rather than projecting onto an imaginary
        # extension of the first segment.
        before = (NAGERCOIL_ROUTE[0][0] + 0.009, NAGERCOIL_ROUTE[0][1])
        snap = snap_to_route(before, NAGERCOIL_ROUTE)
        assert snap is not None
        assert snap.along_m == pytest.approx(0.0, abs=1.0)
        assert snap.snapped == pytest.approx(NAGERCOIL_ROUTE[0])

    def test_snapping_clamps_beyond_the_route_ends(self):
        # A point well past the last vertex must snap to the end, not overshoot.
        beyond = (NAGERCOIL_ROUTE[-1][0] - 0.02, NAGERCOIL_ROUTE[-1][1])
        snap = snap_to_route(beyond, NAGERCOIL_ROUTE)
        assert snap is not None
        assert snap.along_m <= route_length_m(NAGERCOIL_ROUTE) + 1.0

    def test_degenerate_route_returns_none(self):
        assert snap_to_route((8.18, 77.43), []) is None
        assert snap_to_route((8.18, 77.43), [(8.18, 77.43)]) is None

    def test_repeated_vertices_do_not_divide_by_zero(self):
        route = [(8.18, 77.43), (8.18, 77.43), (8.17, 77.44)]
        snap = snap_to_route((8.175, 77.435), route)
        assert snap is not None
        assert math.isfinite(snap.offset_m)


class TestDensifyAndInterpolate:
    def test_densify_closes_large_gaps(self):
        dense = densify(NAGERCOIL_ROUTE, max_gap_m=25.0)
        gaps = [
            haversine(dense[i - 1][0], dense[i - 1][1], dense[i][0], dense[i][1])
            for i in range(1, len(dense))
        ]
        assert max(gaps) <= 25.5

    def test_densify_preserves_endpoints_and_length(self):
        dense = densify(NAGERCOIL_ROUTE, max_gap_m=25.0)
        assert dense[0] == NAGERCOIL_ROUTE[0]
        assert dense[-1] == pytest.approx(NAGERCOIL_ROUTE[-1])
        assert route_length_m(dense) == pytest.approx(
            route_length_m(NAGERCOIL_ROUTE), rel=0.01
        )

    def test_densify_leaves_short_routes_alone(self):
        assert densify([], 25.0) == []
        assert densify([(8.18, 77.43)], 25.0) == [(8.18, 77.43)]

    def test_point_at_distance_walks_the_route(self):
        total = route_length_m(NAGERCOIL_ROUTE)
        midpoint = point_at_distance(NAGERCOIL_ROUTE, total / 2)
        snap = snap_to_route(midpoint, NAGERCOIL_ROUTE)
        assert snap is not None
        assert snap.along_m == pytest.approx(total / 2, abs=2.0)

    def test_point_at_distance_clamps_at_both_ends(self):
        assert point_at_distance(NAGERCOIL_ROUTE, -500) == pytest.approx(
            NAGERCOIL_ROUTE[0]
        )
        assert point_at_distance(NAGERCOIL_ROUTE, 10**6) == pytest.approx(
            NAGERCOIL_ROUTE[-1]
        )

    def test_point_at_distance_rejects_empty_route(self):
        with pytest.raises(ValueError):
            point_at_distance([], 100)
