"""Pinned LOCA inputs with cache-first, SHA-256-verified acquisition.

The lean benchmark deliberately does not vendor the upstream repository.  A
materialization may use only the immutable pin record, while ``preflight``
can require a local cache (or fetch the same files) before generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
PINS_PATH = ROOT / "sources.json"
_SHA_PREFIX = "sha256:"


class PinError(RuntimeError):
    """Raised when a pinned source, license, or cache entry is invalid."""


@dataclass(frozen=True)
class Pin:
    name: str
    url: str
    sha256: str
    license: str | None = None

    @property
    def digest(self) -> str:
        return self.sha256.removeprefix(_SHA_PREFIX)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(_SHA_PREFIX):
        raise PinError(f"{field} must be a sha256:<hex> value")
    digest = value[len(_SHA_PREFIX) :]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise PinError(f"{field} is not a lowercase SHA-256 digest")
    return value


def load_pins(path: Path = PINS_PATH) -> tuple[dict, tuple[Pin, ...]]:
    """Load and validate the compact pin record before any I/O is attempted."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PinError(f"cannot read pin record {path}: {exc}") from exc
    license_record = record.get("license")
    if not isinstance(license_record, dict) or license_record.get("spdx") != "MIT":
        raise PinError("LOCA license pin must be an MIT license object")
    _require_digest(license_record.get("sha256"), "license.sha256")
    upstream = record.get("upstream", {})
    for key in ("repository", "commit", "sandbox_commit"):
        if not isinstance(upstream.get(key), str) or not upstream[key]:
            raise PinError(f"upstream.{key} is required")
    raw_pins = record.get("files")
    if not isinstance(raw_pins, list) or not raw_pins:
        raise PinError("at least one source file pin is required")
    pins: list[Pin] = []
    names: set[str] = set()
    for item in raw_pins:
        if not isinstance(item, dict):
            raise PinError("source file pins must be objects")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names or "/" in name or "\\" in name or name in {".", ".."}:
            raise PinError(f"invalid or duplicate source pin name: {name!r}")
        names.add(name)
        url = item.get("url")
        if not isinstance(url, str) or not (url.startswith("https://") or url.startswith("file://")):
            raise PinError(f"source pin {name} must use https:// or file://")
        digest = _require_digest(item.get("sha256"), f"files[{name}].sha256")
        pins.append(Pin(name, url, digest, item.get("license")))
    return record, tuple(pins)


def source_digest(record: dict, pins: Iterable[Pin]) -> str:
    """Return the stable digest used in ``derived/harbor-tasks/loca/<digest>``."""
    payload = {
        "upstream": record["upstream"],
        "license": record["license"],
        "files": [pin.__dict__ for pin in sorted(pins, key=lambda p: p.name)],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _digest(encoded)


def _checked(path: Path, expected: str) -> Path:
    if not path.is_file():
        raise PinError(f"missing cached pinned input: {path}")
    actual = _digest(path.read_bytes())
    if actual != expected.removeprefix(_SHA_PREFIX):
        raise PinError(f"SHA-256 mismatch for {path}: expected {expected}, got sha256:{actual}")
    return path


def fetch_pinned(pin: Pin, cache_dir: Path, *, offline: bool = False) -> Path:
    """Return a verified cache entry, atomically fetching it when permitted."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{pin.name}.{pin.digest}"
    if target.exists():
        return _checked(target, pin.sha256)
    if offline:
        raise PinError(f"offline mode has no cache entry for {pin.name}")
    with urllib.request.urlopen(pin.url, timeout=30) as response:
        payload = response.read()
    if _digest(payload) != pin.digest:
        raise PinError(f"downloaded SHA-256 mismatch for {pin.name}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{pin.name}.", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return _checked(target, pin.sha256)


def verify_cache(cache_dir: Path, *, offline: bool = True) -> dict[str, str]:
    """Verify all source and license cache entries; fail closed on mismatch."""
    record, pins = load_pins()
    license_pin = Pin(
        "LICENSE",
        record["license"]["url"],
        record["license"]["sha256"],
        record["license"]["spdx"],
    )
    entries = (license_pin, *pins)
    result: dict[str, str] = {}
    for pin in entries:
        result[pin.name] = str(fetch_pinned(pin, cache_dir, offline=offline))
    return result
