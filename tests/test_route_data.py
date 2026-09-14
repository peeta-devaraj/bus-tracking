"""Integrity checks on the committed Nagercoil route data.

tools/data/nagercoil_routes.json is generated once from OpenStreetMap and then
committed, so these tests run offline. They exist because nobody can review an
encoded polyline by eye: a bad rebuild or a hand edit would otherwise only show
up as buses drifting off-route in a demo.
"""

import json
from pathlib import Path

import pytest

from shared.geo import decode_polyline, find_excursions, haversine, route_length_m, snap_to_route
from shared.validation import OFF_ROUTE_M

DATA_PATH = Path(__file__).resolve().parents[1] / "tools" / "data" / "nagercoil_routes.json"

# Nagercoil town down to Kanyakumari, generously padded.
BBOX = (8.00, 77.30, 8.30, 77.65)


@pytest.fixture(scope="module")
def data():
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def stops(data):
    return {s["stopId"]: s for s in data["stops"]}


def routes(data):
    return data["routes"]


def test_the_build_recorded_no_problems(data):
    assert data["problems"] == []


def test_it_is_labelled_honestly(data):
    # It is road geometry between real stops, not a surveyed route, and the
    # data must never claim otherwise.
    assert data["source"] == "osm-routed"
    assert "not a surveyed bus route" in data["caveat"]
    assert "OpenStreetMap" in data["attribution"]


def test_every_stop_is_traceable_to_an_osm_feature(data):
    for stop in data["stops"]:
        kind, _, osm_id = stop["osm"].partition("/")
        assert kind in {"node", "way", "relation"}, stop
        assert osm_id.isdigit(), stop


def test_every_stop_is_in_nagercoil_district(data):
    lo_lat, lo_lon, hi_lat, hi_lon = BBOX
    for stop in data["stops"]:
        pos = stop["position"]
        assert lo_lat <= pos["lat"] <= hi_lat and lo_lon <= pos["lon"] <= hi_lon, stop["stopId"]


def test_routes_only_reference_known_stops(data, stops):
    for route in routes(data):
        for stop_id in route["stopIds"]:
            assert stop_id in stops, f"{route['routeId']} names unknown stop {stop_id}"


def test_polylines_decode_to_real_routes(data):
    for route in routes(data):
        points = decode_polyline(route["polyline"])
        assert len(points) == route["vertices"]
        assert route_length_m(points) == pytest.approx(route["distanceM"], rel=0.01)


def test_routes_start_and_end_at_their_first_and_last_stop(data, stops):
    for route in routes(data):
        points = decode_polyline(route["polyline"])
        first = stops[route["stopIds"][0]]["position"]
        last = stops[route["stopIds"][-1]]["position"]
        assert haversine(points[0][0], points[0][1], first["lat"], first["lon"]) < OFF_ROUTE_M
        assert haversine(points[-1][0], points[-1][1], last["lat"], last["lon"]) < OFF_ROUTE_M


def test_every_stop_sits_on_its_route(data, stops):
    """A bus waiting at a stop must never be flagged off-route by ingest."""
    for route in routes(data):
        points = decode_polyline(route["polyline"])
        for stop_id in route["stopIds"]:
            pos = stops[stop_id]["position"]
            snap = snap_to_route((pos["lat"], pos["lon"]), points)
            assert snap.offset_m < OFF_ROUTE_M, f"{stop_id} is {snap.offset_m:.0f} m off {route['routeId']}"


def test_stops_come_in_travel_order(data):
    # The original hand-made seed visited Kottar before the town bus stand,
    # which is the wrong way round. Order along the line must match the list.
    for route in routes(data):
        alongs = [s["alongM"] for s in route["stopsAlong"]]
        assert [s["stopId"] for s in route["stopsAlong"]] == route["stopIds"]
        assert alongs == sorted(alongs), route["routeId"]
        assert len(set(alongs)) == len(alongs), route["routeId"]


def test_legs_add_up_to_the_route(data):
    for route in routes(data):
        legs_m = sum(leg["distanceM"] for leg in route["legs"])
        first, last = route["stopsAlong"][0]["alongM"], route["stopsAlong"][-1]["alongM"]
        assert legs_m == pytest.approx(last - first, abs=1.0)
        assert all(leg["distanceM"] > 0 and leg["freeFlowS"] > 0 for leg in route["legs"])


def test_free_flow_speeds_are_physically_plausible(data):
    # Implied car speeds between stops. Below walking pace or above the speed
    # of a car on these roads would mean the leg data is corrupt.
    for route in routes(data):
        for leg in route["legs"]:
            kmh = leg["distanceM"] / leg["freeFlowS"] * 3.6
            assert 8 <= kmh <= 90, f"{route['routeId']} {leg['from']}->{leg['to']}: {kmh:.0f} km/h"


def test_no_side_lane_detours_remain_at_roadside_stops(data, stops):
    """The market and town-centre spurs were cut out; none may creep back."""
    for route in routes(data):
        points = decode_polyline(route["polyline"])
        for ex in find_excursions(points, max_length_m=600):
            near = [
                stop_id for stop_id in route["stopIds"]
                if stops[stop_id]["kind"] == "roadside"
                and haversine(ex.tip[0], ex.tip[1],
                              stops[stop_id]["position"]["lat"], stops[stop_id]["position"]["lon"]) < 120
            ]
            assert not near, f"{route['routeId']}: {ex.length_m:.0f} m detour into roadside stop {near}"


def test_route_ids_are_stable(data):
    # Buses, stored history and the simulator refer to these by id. Renaming
    # one silently orphans all of that.
    assert {r["routeId"] for r in routes(data)} == {"NGL-VAD-KKD", "NGL-SUC", "NGL-KK"}
