"""The learning pipeline end to end: history in storage -> trained model -> /eta.

Runs against Azurite. The storage half needs only Azurite; the /eta half also
needs the Functions host on :7071 and is skipped without it.

History is produced by the traffic simulator and written with isSimulated set,
exactly as tools/backfill_history.py does, so the model must come back
labelled as trained on simulated data.
"""

import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

from shared import storage, training
from shared.auth import BUS_ID_HEADER, SIGNATURE_HEADER, new_secret, sign
from shared.geo import point_at_distance

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from traffic import STRUCTURED, Traffic, load_profile  # noqa: E402

API = os.environ.get("BUSTRACK_API", "http://127.0.0.1:7071/api")
SOURCE_ROUTE = "NGL-VAD-KKD"


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(not _listening(10002), reason="Azurite not reachable on 10002"),
    # Writes three simulated days of history to storage: about a minute.
    pytest.mark.slow,
]


@pytest.fixture(scope="module")
def trained_route():
    """A throwaway copy of a real route with three days of simulated history, trained."""
    profile = load_profile(SOURCE_ROUTE)
    source = next(r for r in json.load(open(
        os.path.join(os.path.dirname(__file__), "..", "tools", "data", "nagercoil_routes.json"),
        encoding="utf-8"))["routes"] if r["routeId"] == SOURCE_ROUTE)

    route_id = f"LEARN-{uuid.uuid4().hex[:6]}"
    bus_id = f"SIMHIST-{uuid.uuid4().hex[:6]}"
    secret = new_secret()

    storage.upsert_route(route_id, "Learning test route", source["polyline"], city="testcity")
    storage.upsert_bus(bus_id, secret, route_id=route_id, label="history bus", is_simulated=True)

    stop_ids = []
    for stop in profile.stops:
        lat, lon = point_at_distance(profile.points, stop.along_m)
        sid = f"{route_id}-{stop.stop_id}"
        storage.upsert_stop(sid, stop.stop_id, lat, lon, city="testcity", route_ids=[route_id])
        stop_ids.append((sid, stop.along_m))

    # Days ending yesterday, inside the default 14-day training window.
    traffic = Traffic(profile, mode=STRUCTURED, seed=5)
    today = datetime.now(timezone.utc).date()
    rows = []
    for back in (3, 2, 1):
        day = today - timedelta(days=back)
        for fix in traffic.run_day(day, 0, sample_s=20.0):
            rows.append({
                "busId": bus_id, "ts": fix.ts, "lat": fix.lat, "lon": fix.lon,
                "alongM": fix.along_m, "direction": fix.direction, "routeId": route_id,
                "speedMps": fix.speed_mps, "isSimulated": True,
            })
    written = storage.batch_append_history(rows)

    summary = training.train_route(route_id)

    yield {
        "route_id": route_id, "bus_id": bus_id, "secret": secret, "profile": profile,
        "stops": stop_ids, "summary": summary, "written": written,
    }

    storage.delete_live_position(bus_id, route_id)
    storage.delete_bus(bus_id)
    storage.delete_route(route_id, city="testcity")
    storage.delete_eta_model(route_id)


class TestTrainingFromStorage:
    def test_history_was_written(self, trained_route):
        assert trained_route["written"] > 1000

    def test_backfilling_twice_does_not_double_the_history(self, trained_route):
        # Fixed row keys make a re-run overwrite rather than duplicate.
        day = time.strftime("%Y%m%d", time.gmtime(time.time() - 86400))
        before = len(storage.history_for_day(trained_route["bus_id"], day))
        rows = [
            {k: r.get(k) for k in ("busId", "ts", "lat", "lon", "alongM", "direction", "routeId")}
            for r in storage.history_for_day(trained_route["bus_id"], day)
        ]
        storage.batch_append_history(rows)
        assert len(storage.history_for_day(trained_route["bus_id"], day)) == before

    def test_the_route_trained(self, trained_route):
        summary = trained_route["summary"]
        assert summary["trained"] is True, summary
        assert summary["daysWithData"] == 3
        assert summary["fixes"] > 1000

    def test_it_is_labelled_as_trained_on_simulated_data(self, trained_route):
        assert trained_route["summary"]["trainedOn"] == "simulated"

    def test_it_calibrated_on_held_out_days(self, trained_route):
        summary = trained_route["summary"]
        assert summary["calibrationSamples"] >= 30
        assert summary["calibrated"] is True
        low, high = summary["range"]
        assert 0 < low < 1.0 < high

    def test_the_stored_model_loads_and_predicts_from_history(self, trained_route):
        training._cache.clear()
        model, meta = training.get_model(trained_route["route_id"])
        assert model is not None
        assert meta["simulatedShare"] == pytest.approx(1.0)

        length = trained_route["profile"].length_m
        # Mid-afternoon IST, heading out from the first stop to the last.
        ts = datetime.now(timezone.utc).replace(hour=9, minute=30).timestamp()
        pred = model.predict(1, 200.0, length - 200.0, ts, fallback_pace=1 / 5.0)
        assert pred is not None
        assert pred.learned_share > 0.9
        assert pred.low_seconds <= pred.seconds <= pred.high_seconds

    def test_an_unknown_route_is_refused_cleanly(self):
        summary = training.train_route(f"NOPE-{uuid.uuid4().hex[:6]}")
        assert summary["trained"] is False


@pytest.mark.skipif(not _listening(7071), reason="Functions host not running on 7071")
class TestLearnedEstimatesThroughTheApi:
    def _ping(self, bus_id, secret, along_m, ts, profile):
        lat, lon = point_at_distance(profile.points, along_m)
        body = json.dumps({
            "lat": round(lat, 6), "lon": round(lon, 6), "ts": ts, "accuracy": 10.0,
            "speed": 6.0, "heading": 0.0, "nonce": uuid.uuid4().hex[:16],
        }).encode("utf-8")
        resp = requests.post(f"{API}/ping", data=body, timeout=10, headers={
            "Content-Type": "application/json", BUS_ID_HEADER: bus_id, SIGNATURE_HEADER: sign(secret, body),
        })
        assert resp.status_code == 200, resp.text

    def test_eta_uses_the_learned_model_and_says_what_it_learned_from(self, trained_route):
        route = trained_route
        now = time.time()
        # Heading outbound, past the first two stops.
        self._ping(route["bus_id"], route["secret"], 1500.0, now - 6, route["profile"])
        self._ping(route["bus_id"], route["secret"], 1560.0, now, route["profile"])

        far_stop, _ = route["stops"][-1]
        body = requests.get(f"{API}/eta", params={"stopId": far_stop, "city": "testcity"}, timeout=30).json()
        mine = [a for a in body["arrivals"] if a["busId"] == route["bus_id"]]
        assert mine, body
        arrival = mine[0]
        assert arrival["method"] == "learned", arrival
        assert arrival["trainedOn"] == "simulated"
        assert arrival["learnedShare"] >= 0.5
        assert arrival["lowMin"] <= arrival["highMin"]
