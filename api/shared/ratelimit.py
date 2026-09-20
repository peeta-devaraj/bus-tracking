"""Bounding how often a single bus can make the server write to storage.

The ingest endpoint records every rejected position report, and that log is
the evidence the plausibility checks actually run. But a rejection is written
*before* a signature can be trusted -- a bad signature is itself the thing
worth recording -- and bus ids are public in /live. So anyone could read an id,
fire badly signed pings at it, and make the server write a row per request:
unbounded storage cost, and a reject log so full of noise that a real spoofing
attempt would be invisible in it.

This keeps the evidence and drops the flood. Each bus may write a few
rejections per window; beyond that they are counted, and the count is attached
to the first rejection of the next window, so the log still says how much was
suppressed rather than quietly losing it.

The state is per worker and in memory on purpose: it must not cost a storage
read to decide whether to do a storage write. Several workers therefore allow
several times the limit, which is fine -- the point is to bound the flood, not
to count it exactly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    log: bool          # write this one to storage?
    suppressed: int    # how many were dropped in the window just ended


@dataclass
class _Bucket:
    window_start: float
    logged: int
    suppressed: int


class WriteLimiter:
    """Allows `max_per_window` writes per key per window, counting the rest."""

    def __init__(self, max_per_window: int = 5, window_s: float = 60.0, max_keys: int = 10_000):
        self.max_per_window = max_per_window
        self.window_s = window_s
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def record(self, key: str, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        bucket = self._buckets.get(key)

        if bucket is None or now - bucket.window_start >= self.window_s:
            carried = bucket.suppressed if bucket else 0
            self._buckets[key] = _Bucket(window_start=now, logged=1, suppressed=0)
            self._evict(now)
            return Decision(log=True, suppressed=carried)

        if bucket.logged < self.max_per_window:
            bucket.logged += 1
            return Decision(log=True, suppressed=0)

        bucket.suppressed += 1
        return Decision(log=False, suppressed=0)

    def _evict(self, now: float) -> None:
        """Forget idle keys so a flood of invented ids cannot grow this forever."""
        if len(self._buckets) <= self.max_keys:
            stale = [k for k, b in self._buckets.items() if now - b.window_start > self.window_s * 2]
            for key in stale:
                del self._buckets[key]
            return

        # Over the cap even after that: drop the oldest half outright. Losing
        # limiter state only means a few extra rows get written.
        oldest = sorted(self._buckets.items(), key=lambda kv: kv[1].window_start)
        for key, _ in oldest[: len(oldest) // 2]:
            del self._buckets[key]

    @property
    def tracked_keys(self) -> int:
        return len(self._buckets)
