"""End-to-end tests against a running API.

These drive the real HTTP surface exactly as the driver page does, including
HMAC signing. They are the executable version of the demo script: every
rejection test here is something you can show happening live on the admin
page's reject log.

Run the stack first:   .\\run-local.ps1
Point at Azure with:   BUSTRACK_API=https://<app>.azurewebsites.net/api
"""

import json
import os
import time
import uuid

import pytest
import requests

from shared.auth import BUS_ID_HEADER, SIGNATURE_HEADER, sign
from shared.geo import encode_polyline

API = os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

# The same stretch of Nagercoil road used in the geometry tests.
ROUTE_POINTS = [
    (8.18880, 77.42900),
    (8.18600, 77.43050),
    (8.18300, 77.43220),
    (8.18000, 77.43350),
    (8.17800, 77.43400),
]


def _api_up() -> bool:
    try:
        return requests.get(f"{API}/health", timeout=3).ok
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _api_up(), reason=f"API not reachable at {API}"
)


def _admin_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if ADMIN_KEY:
        headers["X-Admin-Key"] = ADMIN_KEY
    return headers


def post_ping(bus_id: str, secret: str, **fields) -> requests.Response:
    """Sign and send a position report the way the driver page does."""
    payload = {
        "lat": 8.18300,
        "lon": 77.43220,
        "ts": time.time(),
        "accuracy": 12.0,
        "speed": 8.0,
        "heading": 190.0,
        "nonce": uuid.uuid4().hex[:16],
    }
    payload.update(fields)

    # Sign the exact bytes that go on the wire.
    body = json.dumps(payload).encode("utf-8")
    return requests.post(
        f"{API}/ping",
        data=body,
        headers={
            "Content-Type": "application/json",
            BUS_ID_HEADER: bus_id,
            SIGNATURE_HEADER: sign(secret, body),
        },
        timeout=10,
    )


@pytest.fixture(scope="module")
def route_id() -> str:
    rid = f"E2E-{uuid.uuid4().hex[:6]}"
    resp = requests.post(
        f"{API}/manage/routes",
        json={
            "routeId": rid,
            "name": "E2E test route",
            "city": "testcity",
            "polyline": encode_polyline(ROUTE_POINTS),
        },
        headers=_admin_headers(),
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    return rid


@pytest.fixture()
def bus(route_id):
    """Register a throwaway bus, yield (busId, secret), then remove it."""
    bus_id = f"E2E-{uuid.uuid4().hex[:8]}"
    resp = requests.post(
        f"{API}/manage/buses",
        json={"busId": bus_id, "label": "E2E bus", "routeId": route_id},
        headers=_admin_headers(),
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    secret = resp.json()["secret"]
    assert secret

    yield bus_id, secret

    requests.delete(
        f"{API}/manage/buses/{bus_id}", headers=_admin_headers(), timeout=10
    )


class TestHealth:
    def test_health_reports_ok(self):
        body = requests.get(f"{API}/health", timeout=5).json()
        assert body["ok"] is True


class TestRegistration:
    def test_registering_returns_a_secret(self, route_id):
        bus_id = f"E2E-{uuid.uuid4().hex[:8]}"
        try:
            resp = requests.post(
                f"{API}/manage/buses",
                json={"busId": bus_id, "routeId": route_id},
                headers=_admin_headers(),
                timeout=10,
            )
            assert resp.status_code == 200
            assert resp.json()["created"] is True
            assert len(resp.json()["secret"]) > 20
        finally:
            requests.delete(f"{API}/manage/buses/{bus_id}", headers=_admin_headers(), timeout=10)

    def test_re_registering_keeps_the_same_secret(self, bus, route_id):
        bus_id, secret = bus
        resp = requests.post(
            f"{API}/manage/buses",
            json={"busId": bus_id, "label": "Renamed", "routeId": route_id},
            headers=_admin_headers(),
            timeout=10,
        )
        assert resp.json()["secret"] == secret, "a rename must not invalidate the driver's QR"
        assert resp.json()["created"] is False

    def test_busid_is_required(self):
        resp = requests.post(
            f"{API}/manage/buses", json={"label": "no id"},
            headers=_admin_headers(), timeout=10,
        )
        assert resp.status_code == 400


class TestIngestAccepts:
    def test_a_correctly_signed_fix_is_accepted(self, bus):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["flags"] == []
        assert body["offsetM"] is not None and body["offsetM"] < 10

    def test_an_accepted_fix_appears_on_the_live_map(self, bus, route_id):
        bus_id, secret = bus
        post_ping(bus_id, secret)

        live = requests.get(f"{API}/live", params={"routeId": route_id}, timeout=10).json()
        mine = [b for b in live["buses"] if b["busId"] == bus_id]
        assert len(mine) == 1
        assert mine[0]["confidence"] == "live"
        assert mine[0]["ageS"] < 30

    def test_a_diversion_is_kept_but_downgraded(self, bus, route_id):
        bus_id, secret = bus
        # About 1 km east of the route: a real diversion, not a spoof.
        resp = post_ping(bus_id, secret, lon=77.43220 + 0.009)
        assert resp.status_code == 200
        assert "off_route" in resp.json()["flags"]

        live = requests.get(f"{API}/live", params={"routeId": route_id}, timeout=10).json()
        mine = next(b for b in live["buses"] if b["busId"] == bus_id)
        assert mine["confidence"] == "uncertain", "off-route must not read as fully live"

    def test_coarse_gps_is_kept_but_downgraded(self, bus):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret, accuracy=500.0)
        assert resp.status_code == 200
        assert "coarse_accuracy" in resp.json()["flags"]


class TestIngestRejects:
    """The spoofing demo. Each of these must never reach the map."""

    def test_an_unsigned_fix_is_rejected(self, bus):
        bus_id, _ = bus
        body = json.dumps({"lat": 8.183, "lon": 77.4322, "ts": time.time()}).encode()
        resp = requests.post(
            f"{API}/ping",
            data=body,
            headers={"Content-Type": "application/json", BUS_ID_HEADER: bus_id},
            timeout=10,
        )
        assert resp.status_code == 401

    def test_a_wrong_signature_is_rejected(self, bus):
        bus_id, _ = bus
        resp = post_ping(bus_id, "not-the-real-secret")
        assert resp.status_code == 401
        assert resp.json()["reason"] == "bad_signature"

    def test_an_unregistered_bus_is_rejected(self):
        resp = post_ping("NOT-A-REAL-BUS", "whatever")
        assert resp.status_code == 401

    def test_a_tampered_body_is_rejected(self, bus):
        bus_id, secret = bus
        honest = json.dumps({"lat": 8.183, "lon": 77.4322, "ts": time.time()}).encode()
        signature = sign(secret, honest)
        tampered = json.dumps({"lat": 13.08, "lon": 80.27, "ts": time.time()}).encode()

        resp = requests.post(
            f"{API}/ping",
            data=tampered,
            headers={
                "Content-Type": "application/json",
                BUS_ID_HEADER: bus_id,
                SIGNATURE_HEADER: signature,
            },
            timeout=10,
        )
        assert resp.status_code == 401

    def test_a_teleport_to_chennai_is_rejected(self, bus):
        bus_id, secret = bus
        now = time.time()

        first = post_ping(bus_id, secret, ts=now - 20)
        assert first.status_code == 200

        # 600 km in 20 seconds.
        resp = post_ping(bus_id, secret, lat=13.0827, lon=80.2707, ts=now)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "teleport"
        assert "km/h" in resp.json()["detail"]

    def test_a_stale_timestamp_is_rejected(self, bus):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret, ts=time.time() - 3600)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "stale_timestamp"

    def test_a_future_timestamp_is_rejected(self, bus):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret, ts=time.time() + 3600)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "future_timestamp"

    def test_flooding_is_rate_limited(self, bus):
        bus_id, secret = bus
        now = time.time()
        assert post_ping(bus_id, secret, ts=now - 10).status_code == 200
        resp = post_ping(bus_id, secret, ts=now - 8)  # only 2 s later
        assert resp.status_code == 422
        assert resp.json()["reason"] == "too_frequent"

    def test_null_island_is_rejected(self, bus):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret, lat=0.0, lon=0.0)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "bad_coords"

    def test_a_rejected_fix_never_reaches_the_live_map(self, bus, route_id):
        bus_id, secret = bus
        resp = post_ping(bus_id, secret, lat=13.0827, lon=80.2707, ts=time.time() - 3600)
        assert resp.status_code == 422

        live = requests.get(f"{API}/live", params={"routeId": route_id}, timeout=10).json()
        assert not [b for b in live["buses"] if b["busId"] == bus_id]

    def test_rejections_are_written_to_the_log(self, bus):
        bus_id, secret = bus
        post_ping(bus_id, secret, ts=time.time() - 3600)

        rejections = requests.get(f"{API}/rejections", params={"limit": 200}, timeout=10).json()
        mine = [r for r in rejections["rejections"] if r["busId"] == bus_id]
        assert mine, "the reject log is the evidence the checks are real"
        assert mine[0]["reason"] == "stale_timestamp"


class TestReadEndpoints:
    def test_routes_lists_the_test_route(self, route_id):
        body = requests.get(f"{API}/routes", params={"city": "testcity"}, timeout=10).json()
        assert route_id in {r["routeId"] for r in body["routes"]}

    def test_route_geometry_can_be_omitted(self):
        body = requests.get(
            f"{API}/routes", params={"city": "testcity", "geometry": "0"}, timeout=10
        ).json()
        assert all("polyline" not in r for r in body["routes"])

    def test_live_accepts_a_bounding_box(self, bus, route_id):
        bus_id, secret = bus
        post_ping(bus_id, secret)

        inside = requests.get(
            f"{API}/live", params={"bbox": "8.17,77.42,8.19,77.44"}, timeout=10
        ).json()
        outside = requests.get(
            f"{API}/live", params={"bbox": "12.9,80.1,13.2,80.4"}, timeout=10
        ).json()

        assert bus_id in {b["busId"] for b in inside["buses"]}
        assert bus_id not in {b["busId"] for b in outside["buses"]}

    def test_eta_requires_a_stop_id(self):
        assert requests.get(f"{API}/eta", timeout=10).status_code == 400

    def test_eta_for_an_unknown_stop_is_404(self):
        resp = requests.get(f"{API}/eta", params={"stopId": "nope", "city": "testcity"}, timeout=10)
        assert resp.status_code == 404


class TestEtaFlow:
    def test_a_bus_behind_a_stop_gets_an_arrival_estimate(self, bus, route_id):
        bus_id, secret = bus

        stop_id = f"STOP-{uuid.uuid4().hex[:6]}"
        requests.post(
            f"{API}/manage/stops",
            json={
                "stopId": stop_id,
                "name": "E2E test stop",
                # Near the far end of the route, so the bus is approaching it.
                "lat": 8.18000, "lon": 77.43350,
                "city": "testcity",
                "routeIds": [route_id],
            },
            headers=_admin_headers(),
            timeout=10,
        )

        # Put the bus near the start of the route, moving.
        post_ping(bus_id, secret, lat=8.18880, lon=77.42900, speed=8.0)

        body = requests.get(
            f"{API}/eta", params={"stopId": stop_id, "city": "testcity"}, timeout=10
        ).json()

        mine = [a for a in body["arrivals"] if a["busId"] == bus_id]
        assert mine, "an approaching bus should produce an estimate"
        arrival = mine[0]
        assert arrival["lowMin"] <= arrival["highMin"], "must be a range, not a point"
        assert arrival["distanceM"] > 0
        assert arrival["speedSource"] in {"reported", "average", "default"}
        assert "-" in arrival["text"] or "min" in arrival["text"] or arrival["text"] == "arriving"

    def test_a_bus_that_passed_the_stop_gets_no_estimate(self, bus, route_id):
        bus_id, secret = bus

        stop_id = f"STOP-{uuid.uuid4().hex[:6]}"
        requests.post(
            f"{API}/manage/stops",
            json={
                "stopId": stop_id, "name": "Behind the bus",
                # Near the start; the bus will be reported past it.
                "lat": 8.18880, "lon": 77.42900,
                "city": "testcity", "routeIds": [route_id],
            },
            headers=_admin_headers(), timeout=10,
        )

        post_ping(bus_id, secret, lat=8.17800, lon=77.43400)

        body = requests.get(
            f"{API}/eta", params={"stopId": stop_id, "city": "testcity"}, timeout=10
        ).json()
        assert not [a for a in body["arrivals"] if a["busId"] == bus_id], (
            "refusing to answer beats inventing a number"
        )


class TestRecordingMode:
    def test_recording_builds_a_trace_that_becomes_a_route(self, bus):
        bus_id, secret = bus
        now = time.time()

        # Drive along the route in record mode. The vertices are roughly 350 m
        # apart, so pings go 20 s apart: about 63 km/h, which is a plausible
        # speed and stays inside the replay window for the oldest fix.
        for i, (lat, lon) in enumerate(ROUTE_POINTS):
            resp = post_ping(
                bus_id, secret, lat=lat, lon=lon,
                ts=now - (len(ROUTE_POINTS) - i) * 20 + 5,
                record=True,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["recording"] is True

        trace = requests.get(
            f"{API}/manage/trace/{bus_id}", headers=_admin_headers(), timeout=10
        ).json()

        assert trace["count"] >= len(ROUTE_POINTS)
        assert trace["polyline"], "the trace is the route"
        assert trace["lengthM"] > 0
