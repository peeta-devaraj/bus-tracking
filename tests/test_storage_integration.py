"""Integration tests against a real Table Storage endpoint.

These run against Azurite locally and against the real account in Azure. They
are skipped rather than failed when no endpoint is reachable, so the unit
suite still runs on a machine with nothing set up.

Start Azurite with:  azurite --silent --location .azurite
"""

import socket
import time
import uuid

import pytest

from shared import storage
from shared.geo import encode_polyline


def _azurite_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 10002), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _azurite_running(), reason="Azurite table service not reachable on port 10002"
)


@pytest.fixture()
def bus_id() -> str:
    """A unique bus per test, cleaned up afterwards."""
    generated = f"TEST-{uuid.uuid4().hex[:8]}"
    yield generated
    storage.delete_all_live_positions(generated)
    storage.delete_bus(generated)


class TestBusRegistry:
    def test_register_then_read_back(self, bus_id):
        storage.upsert_bus(bus_id, secret="s3cret", route_id="NGL-1", label="Test bus")
        bus = storage.get_bus(bus_id)
        assert bus is not None
        assert bus["secret"] == "s3cret"
        assert bus["routeId"] == "NGL-1"
        assert bus["label"] == "Test bus"

    def test_unknown_bus_is_none(self):
        assert storage.get_bus("does-not-exist-" + uuid.uuid4().hex) is None

    def test_upsert_updates_in_place(self, bus_id):
        storage.upsert_bus(bus_id, secret="a", label="First")
        storage.upsert_bus(bus_id, secret="a", label="Second")
        assert storage.get_bus(bus_id)["label"] == "Second"

    def test_registered_bus_appears_in_the_listing(self, bus_id):
        storage.upsert_bus(bus_id, secret="a")
        assert bus_id in {b["RowKey"] for b in storage.list_buses()}

    def test_delete_removes_it(self, bus_id):
        storage.upsert_bus(bus_id, secret="a")
        storage.delete_bus(bus_id)
        assert storage.get_bus(bus_id) is None

    def test_deleting_a_missing_bus_is_harmless(self):
        storage.delete_bus("never-existed-" + uuid.uuid4().hex)  # must not raise


class TestLivePositions:
    def test_write_then_read_a_position(self, bus_id):
        now = time.time()
        storage.upsert_live_position(
            bus_id, "NGL-1", 8.1830, 77.4322, now,
            accuracy_m=10.0, speed_mps=7.5, heading=190.0, flags=["off_route"],
            along_m=420.0, offset_m=180.0, label="Test bus",
        )
        position = storage.get_live_position(bus_id, "NGL-1")
        assert position is not None
        assert position["lat"] == pytest.approx(8.1830)
        assert position["ts"] == pytest.approx(now, abs=0.01)
        assert position["alongM"] == pytest.approx(420.0)

    def test_flags_and_nonces_survive_the_round_trip(self, bus_id):
        storage.upsert_live_position(
            bus_id, "NGL-1", 8.18, 77.43, time.time(),
            flags=["off_route", "coarse_accuracy"], recent_nonces=["n1", "n2"],
        )
        found = [p for p in storage.list_live_positions("NGL-1") if p["busId"] == bus_id]
        assert len(found) == 1
        assert set(found[0]["flags"]) == {"off_route", "coarse_accuracy"}
        assert found[0]["recentNonces"] == ["n1", "n2"]

    def test_only_the_latest_position_is_kept(self, bus_id):
        storage.upsert_live_position(bus_id, "NGL-1", 8.18, 77.43, time.time())
        storage.upsert_live_position(bus_id, "NGL-1", 8.19, 77.44, time.time())
        matching = [p for p in storage.list_live_positions("NGL-1") if p["busId"] == bus_id]
        assert len(matching) == 1
        assert matching[0]["lat"] == pytest.approx(8.19)

    def test_route_filter_partitions_correctly(self, bus_id):
        storage.upsert_live_position(bus_id, "NGL-1", 8.18, 77.43, time.time())
        on_route = {p["busId"] for p in storage.list_live_positions("NGL-1")}
        other_route = {p["busId"] for p in storage.list_live_positions("NGL-999")}
        assert bus_id in on_route
        assert bus_id not in other_route

    def test_a_bus_with_no_route_still_stores(self, bus_id):
        # Recording mode: no assigned route yet.
        storage.upsert_live_position(bus_id, "", 8.18, 77.43, time.time())
        assert storage.get_live_position(bus_id, "") is not None


class TestHistory:
    def test_history_returns_newest_first(self, bus_id):
        base = time.time()
        for offset in range(3):
            storage.append_history(bus_id, base + offset * 10, 8.18 + offset * 0.001, 77.43)
        rows = storage.recent_history(bus_id)
        mine = [r for r in rows if r["busId"] == bus_id]
        assert len(mine) >= 3
        timestamps = [float(r["ts"]) for r in mine]
        assert timestamps == sorted(timestamps, reverse=True)


class TestRoutesAndStops:
    def test_save_and_load_a_route(self):
        route_id = f"RT-{uuid.uuid4().hex[:6]}"
        polyline = encode_polyline([(8.1888, 77.4290), (8.1780, 77.4340)])
        storage.upsert_route(route_id, "Test route", polyline, city="testcity")
        try:
            route = storage.get_route(route_id, "testcity")
            assert route is not None
            assert route["polyline"] == polyline
            assert route["name"] == "Test route"
        finally:
            storage.delete_route(route_id, "testcity")

    def test_find_route_without_knowing_the_city(self):
        route_id = f"RT-{uuid.uuid4().hex[:6]}"
        storage.upsert_route(route_id, "Findable", "abc", city="testcity")
        try:
            found = storage.find_route_anywhere(route_id)
            assert found is not None
            assert found["name"] == "Findable"
        finally:
            storage.delete_route(route_id, "testcity")

    def test_batch_stop_insert_handles_more_than_one_page(self):
        city = f"batch-{uuid.uuid4().hex[:6]}"
        stops = [
            {"stopId": f"S{i}", "name": f"Stop {i}", "lat": 8.18 + i * 1e-4, "lon": 77.43}
            for i in range(150)  # deliberately over the 100-op batch limit
        ]
        written = storage.batch_upsert_stops(stops, city)
        assert written == 150
        assert len(storage.list_stops(city)) == 150


class TestRejectionLog:
    def test_rejections_are_recorded_and_read_back(self, bus_id):
        storage.record_rejection(bus_id, "teleport", "900km in 10s")
        reasons = [
            r for r in storage.recent_rejections(200)
            if r.get("busId") == bus_id
        ]
        assert reasons
        assert reasons[0]["reason"] == "teleport"

    def test_long_detail_is_truncated_not_rejected(self, bus_id):
        storage.record_rejection(bus_id, "teleport", "x" * 5000)
        row = next(
            r for r in storage.recent_rejections(200) if r.get("busId") == bus_id
        )
        assert len(row["detail"]) <= 512


class TestRetention:
    """Position history says where a named driver was, minute by minute.
    Nothing used to delete it, ever."""

    def _day(self, days_ago: int) -> str:
        return time.strftime("%Y%m%d", time.gmtime(time.time() - days_ago * 86400))

    def _write_fix(self, bus_id: str, days_ago: int) -> None:
        storage.batch_append_history([{
            "busId": bus_id,
            "ts": time.time() - days_ago * 86400,
            "lat": 8.183, "lon": 77.432, "alongM": 100.0, "direction": 1,
        }])

    def test_old_history_goes_and_recent_history_stays(self, bus_id):
        for days_ago in (40, 31, 29, 2):
            self._write_fix(bus_id, days_ago)

        cutoff = self._day(30)
        deleted = storage.purge_history_before([bus_id], cutoff, lookback_days=60)
        assert deleted == 2, "the 40- and 31-day-old fixes should have gone"

        assert storage.history_for_day(bus_id, self._day(40)) == []
        assert storage.history_for_day(bus_id, self._day(31)) == []
        assert len(storage.history_for_day(bus_id, self._day(29))) == 1
        assert len(storage.history_for_day(bus_id, self._day(2))) == 1

    def test_purging_twice_is_harmless(self, bus_id):
        self._write_fix(bus_id, 40)
        cutoff = self._day(30)
        assert storage.purge_history_before([bus_id], cutoff, lookback_days=60) == 1
        assert storage.purge_history_before([bus_id], cutoff, lookback_days=60) == 0

    def test_the_lookback_bounds_the_work(self, bus_id):
        # A daily run only needs to clear the day that just fell out of the
        # window; a short look-back must not reach further back than it says.
        self._write_fix(bus_id, 60)
        assert storage.purge_history_before([bus_id], self._day(30), lookback_days=5) == 0
        assert len(storage.history_for_day(bus_id, self._day(60))) == 1
        assert storage.purge_history_before([bus_id], self._day(30), lookback_days=60) == 1

    def test_old_rejections_go_too(self, bus_id):
        old_ts = time.time() - 45 * 86400
        storage.record_rejection(bus_id, "teleport", "old entry", ts=old_ts)
        assert any(r.get("busId") == bus_id for r in storage.recent_rejections(day=self._day(45)))

        storage.purge_rejections_before(self._day(30), lookback_days=60)
        assert not any(r.get("busId") == bus_id for r in storage.recent_rejections(day=self._day(45)))
