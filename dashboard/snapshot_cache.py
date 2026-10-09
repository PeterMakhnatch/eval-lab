"""Process-wide snapshot that is rebuilt in the background once stale.

Building the Integrity snapshot reads every projection and takes about 30 s.
Under a plain TTL cache the first viewer after each expiry waited that long on
an empty page. Here only the first build blocks; a stale read returns the last
snapshot at once and starts a single background rebuild.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable


def _start_daemon_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, name="snapshot-rebuild", daemon=True).start()


class RefreshingCache[K: Hashable, V]:
    def __init__(
        self,
        build: Callable[[K], V],
        max_age_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] = _start_daemon_thread,
    ) -> None:
        self._build = build
        self._max_age_s = max_age_s
        self._clock = clock
        self._start = start
        self._lock = threading.Lock()
        self._entries: dict[K, tuple[float, V]] = {}
        self._rebuilding: set[K] = set()

    def get(self, key: K) -> V | None:
        """Last snapshot for ``key``, or None before its first build.

        A stale snapshot is still returned; it schedules one rebuild, and
        further stale reads do not schedule another until that one finishes.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            built_at, value = entry
            if self._clock() - built_at > self._max_age_s and key not in self._rebuilding:
                self._rebuilding.add(key)
                self._start(lambda: self._rebuild(key))
            return value

    def build(self, key: K) -> V:
        """Build ``key`` now, store it and return it (blocks the caller)."""
        value = self._build(key)
        with self._lock:
            self._entries[key] = (self._clock(), value)
        return value

    def _rebuild(self, key: K) -> None:
        # A failed rebuild keeps serving the last snapshot; the next stale read retries.
        try:
            self.build(key)
        finally:
            with self._lock:
                self._rebuilding.discard(key)
