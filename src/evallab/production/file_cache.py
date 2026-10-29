"""Serve a parsed retained file, re-reading it only when it changes on disk."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


def signature(path: Path) -> tuple[int, int, int] | None:
    """(mtime, size, inode) of ``path`` itself; an absent or unreadable file is ``None``."""
    try:
        seen = path.lstat()
    except OSError:
        return None
    return (seen.st_mtime_ns, seen.st_size, seen.st_ino)


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
        key = signature(self.path)
        with self._lock:
            if self._loaded and key == self._key:
                return self._value
            value = self._load(self.path)  # a thrown error leaves the last key and value in place
            self._key, self._loaded, self._value = key, True, value
            return value
