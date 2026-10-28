"""Serve a parsed retained file, re-reading it only when it changes on disk."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class ChangeCache:
    """``load(path)`` runs once per (mtime, size, inode) of ``path``; an absent file is one state."""

    def __init__(self, path: Path, load: Callable[[Path], dict[str, Any]]) -> None:
        self.path = path
        self._load = load
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._loaded = False
        self._value: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        try:
            seen = self.path.lstat()
            key: tuple[int, int, int] | None = (seen.st_mtime_ns, seen.st_size, seen.st_ino)
        except OSError:
            key = None
        with self._lock:
            if not self._loaded or key != self._key:
                self._key, self._loaded = key, True
                self._value = self._load(self.path)
            return self._value
