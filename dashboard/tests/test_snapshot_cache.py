"""RefreshingCache: stale reads serve the last snapshot and schedule one rebuild."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from dashboard.snapshot_cache import RefreshingCache


class Harness:
    """Fake clock plus a captured (not yet run) background rebuild queue."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.now = 0.0
        self.builds = 0
        self.pending: list[Callable[[], None]] = []
        self.fail_on = fail_on or set()
        self.cache = RefreshingCache(
            self._build, max_age_s=300, clock=lambda: self.now, start=self.pending.append
        )

    def _build(self, key: str) -> str:
        self.builds += 1
        if self.builds in self.fail_on:
            raise RuntimeError("projection unavailable")
        return f"{key}-v{self.builds}"


def test_first_read_is_empty_until_a_blocking_build() -> None:
    h = Harness()
    assert h.cache.get("pool") is None
    assert h.cache.build("pool") == "pool-v1"
    assert h.cache.get("pool") == "pool-v1"
    assert h.pending == []


def test_stale_reads_return_old_snapshot_and_schedule_a_single_rebuild() -> None:
    h = Harness()
    h.cache.build("pool")
    h.now = 300
    assert h.cache.get("pool") == "pool-v1"
    assert h.pending == []  # exactly max age is still fresh
    h.now = 301
    assert h.cache.get("pool") == "pool-v1"
    assert h.cache.get("pool") == "pool-v1"
    assert len(h.pending) == 1  # concurrent stale readers share one rebuild
    h.pending.pop()()
    assert h.cache.get("pool") == "pool-v2"
    assert h.pending == []  # the rebuilt snapshot is fresh again


def test_failed_rebuild_keeps_serving_and_the_next_stale_read_retries() -> None:
    h = Harness(fail_on={2})
    h.cache.build("pool")
    h.now = 301
    h.cache.get("pool")
    with pytest.raises(RuntimeError):
        h.pending.pop()()
    assert h.cache.get("pool") == "pool-v1"
    assert len(h.pending) == 1
    h.pending.pop()()
    assert h.cache.get("pool") == "pool-v3"
