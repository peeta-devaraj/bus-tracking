"""Three gaps found by re-reading the code near the deadline, and their fixes.

1. The reject log could be flooded. /ping writes a rejection before a
   signature can be trusted, and bus ids are public in /live, so anyone could
   make the server write a storage row per request -- unbounded cost, and a
   reject log too noisy to show a real spoofing attempt in.
2. Position history was never deleted. It records where a named driver was,
   minute by minute, and kept it for ever.
3. The admin guard failed open with no ADMIN_KEY set, leaving the endpoints
   that hand out bus secrets unprotected if a setting were ever cleared.
"""

import time

import pytest

from shared.auth import admin_allowed, looks_like_local_emulator, new_secret
from shared.ratelimit import WriteLimiter

AZURE_CONN = (
    "DefaultEndpointsProtocol=https;AccountName=bustrackb7016d;"
    "AccountKey=Zm9vYmFy;EndpointSuffix=core.windows.net"
)
LOCAL_CONN = "UseDevelopmentStorage=true"


class TestRejectionFlooding:
    def test_the_first_few_are_logged(self):
        limiter = WriteLimiter(max_per_window=5, window_s=60)
        assert [limiter.record("BUS-1", now=1000.0).log for _ in range(5)] == [True] * 5

    def test_the_flood_after_that_is_not(self):
        limiter = WriteLimiter(max_per_window=5, window_s=60)
        for _ in range(5):
            limiter.record("BUS-1", now=1000.0)
        assert [limiter.record("BUS-1", now=1000.0 + i).log for i in range(50)] == [False] * 50

    def test_what_was_suppressed_is_reported_not_lost(self):
        limiter = WriteLimiter(max_per_window=2, window_s=60)
        for _ in range(12):
            limiter.record("BUS-1", now=1000.0)
        # 2 logged, 10 dropped; the next window's first rejection carries the count.
        decision = limiter.record("BUS-1", now=1070.0)
        assert decision.log is True
        assert decision.suppressed == 10

    def test_one_flooded_bus_does_not_silence_another(self):
        limiter = WriteLimiter(max_per_window=2, window_s=60)
        for _ in range(20):
            limiter.record("NOISY", now=1000.0)
        assert limiter.record("QUIET", now=1000.0).log is True

    def test_the_window_reopens(self):
        limiter = WriteLimiter(max_per_window=2, window_s=60)
        for _ in range(10):
            limiter.record("BUS-1", now=1000.0)
        assert limiter.record("BUS-1", now=1061.0).log is True

    def test_invented_bus_ids_cannot_grow_memory_for_ever(self):
        limiter = WriteLimiter(max_per_window=1, window_s=10, max_keys=100)
        for i in range(5000):
            limiter.record(f"MADE-UP-{i}", now=1000.0 + i)
        assert limiter.tracked_keys <= 200, "an id flood must not become a memory leak"


class TestAdminGuardFailsClosed:
    def test_the_right_key_is_accepted(self):
        key = new_secret()
        assert admin_allowed(key, key, AZURE_CONN) is True

    def test_a_wrong_or_missing_key_is_refused(self):
        key = new_secret()
        assert admin_allowed("wrong", key, AZURE_CONN) is False
        assert admin_allowed(None, key, AZURE_CONN) is False
        assert admin_allowed("", key, AZURE_CONN) is False

    def test_no_key_configured_in_azure_is_refused(self):
        # The regression that matters: this used to return True, leaving the
        # endpoint that hands out bus secrets open to anyone.
        assert admin_allowed(None, None, AZURE_CONN) is False
        assert admin_allowed("anything", "", AZURE_CONN) is False

    def test_no_key_against_the_local_emulator_is_allowed(self):
        assert admin_allowed(None, None, LOCAL_CONN) is True

    def test_unknown_storage_is_treated_as_production(self):
        assert admin_allowed(None, None, None) is False
        assert admin_allowed(None, None, "") is False

    @pytest.mark.parametrize("conn,expected", [
        ("UseDevelopmentStorage=true", True),
        ("usedevelopmentstorage=true;", True),
        ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1", True),
        (AZURE_CONN, False),
        ("DefaultEndpointsProtocol=https;AccountName=notdevstore;AccountKey=x", False),
    ])
    def test_emulator_detection(self, conn, expected):
        assert looks_like_local_emulator(conn) is expected
