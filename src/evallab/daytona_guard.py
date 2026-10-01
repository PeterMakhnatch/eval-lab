"""Read-only Daytona admission with a cross-lane host-local reservation lock.

Provider shapes come from https://www.daytona.io/docs/openapi.json:
``/api-keys/current`` identifies the key's organization (its record can contain
key material and is never persisted); ``/organizations/{id}/usage`` carries
``regionUsage`` quotas; ``/sandbox`` uses ``items``/``nextCursor`` pagination;
``/snapshots/{name}`` uses cpu/mem/disk/gpu/sandboxClass.

Resource units follow the provider's GiB fields without conversion. Effective
limits are the minimum of committed ceilings and fresh live quota. Nullable
live per-sandbox caps fall back to the dated dashboard caps, not an invented
limit. Monetary dashboard observations are not live credit or spend approval.

Live/transitional and unknown states reserve every dimension. Stopped container
sandboxes retain disk; stopped VMs and paused/archived/destroyed sandboxes free
compute/RAM, and paused VMs free disk. All states remain in the audit inventory.
Only the requested region/class pool is totaled. Actual usage is the maximum
of inventory and provider telemetry, independently per dimension.

The lock covers the provider read, pending-state read, decision and atomic
write. This matters: reading inventory before the lock can race another runner
reconciling a now-visible reservation against a newer inventory. Pending names
are counted until matching inventory visibility or their conservative expiry.
Creation exceptions never release them. An instance owns only reservations it
made; another instance cannot reuse, replace or release that name.
Owned resizing records the latest actual request but retains the per-dimension
high-water hold until inventory visibility: an earlier creation could still be
ambiguous. The hold counts once, never old plus new; refused resizes leave it
unchanged, and retries never shorten its expiry.

Injected ``fetch(path)`` returns parsed JSON, or ``(HTTP_status, parsed_JSON)``;
exceptions are sanitized to their type. Every real request is an authenticated
GET using DAYTONA_API_KEY; no API key or identity response is stored or logged.
"""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

_DIMENSIONS = ("cpu", "memory_gib", "disk_gib", "gpu")
_API_FIELDS = {"cpu": "cpu", "memory_gib": "memory", "disk_gib": "disk", "gpu": "gpu"}
_USAGE_FIELDS = {
    "cpu": ("totalCpuQuota", "currentCpuUsage", "maxCpuPerSandbox"),
    "memory_gib": ("totalMemoryQuota", "currentMemoryUsage", "maxMemoryPerSandbox"),
    "disk_gib": ("totalDiskQuota", "currentDiskUsage", "maxDiskPerSandbox"),
    "gpu": ("totalGpuQuota", "currentGpuUsage", None),
}
_DEFAULT_API_URL = "https://app.daytona.io/api"
_INVENTORY_PATH = "/sandbox?limit=200&includeErroredDeleted=true&includeWarm=true"
_FREE_STATES = frozenset({"archived", "deleted", "destroyed"})
_VM_CLASSES = frozenset({"linux-vm", "android", "windows"})
_MISSING = object()


class GuardUnavailable(RuntimeError):
    """Required provider/config/state evidence is unavailable or invalid."""


class AdmissionRefused(RuntimeError):
    """Capacity was not admitted; ``snapshot`` contains the safe evidence."""

    def __init__(self, message: str, snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # Never forward the Authorization header to an unexpected host.
        return None


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardUnavailable("InvalidNumber")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise GuardUnavailable("InvalidNumber") from None
    if not math.isfinite(number) or number < 0:
        raise GuardUnavailable("InvalidNumber")
    return number


def _resources(raw: Any, *, candidate: bool = False) -> dict[str, float]:
    if not isinstance(raw, dict) or set(raw) != set(_DIMENSIONS):
        raise GuardUnavailable("InvalidResources")
    resources = {dim: _number(raw[dim]) for dim in _DIMENSIONS}
    if candidate and any(resources[dim] <= 0 for dim in _DIMENSIONS if dim != "gpu"):
        raise GuardUnavailable("InvalidResources")
    return resources


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuardUnavailable("InvalidString")
    return value


def _vector(resources: dict[str, float] | None = None, *, count: int = 0) -> dict[str, float]:
    return {
        **{dim: resources[dim] if resources else 0.0 for dim in _DIMENSIONS},
        "concurrent_sandboxes": float(count),
    }


def _iso(epoch: float | None = None) -> str:
    return (datetime.now(UTC) if epoch is None else datetime.fromtimestamp(epoch, UTC)).isoformat()


def _counts(item: dict[str, Any]) -> dict[str, float]:
    """Count active quota, not nominal allocation or desiredState."""
    state = item["state"].lower()
    klass = item["sandbox_class"]
    allocated = {dim: item[dim] for dim in _DIMENSIONS}
    if state in _FREE_STATES:
        return _vector()
    if state == "stopped" and klass in _VM_CLASSES:
        return _vector()
    if state in {"stopped", "paused"} and klass == "container":
        return _vector({"cpu": 0.0, "memory_gib": 0.0, "disk_gib": item["disk_gib"], "gpu": 0.0})
    if state == "paused" and klass in _VM_CLASSES:
        return _vector()
    if state == "paused":
        return _vector({"cpu": 0.0, "memory_gib": 0.0, "disk_gib": item["disk_gib"], "gpu": 0.0})
    return _vector(allocated, count=1)  # includes creating/start/stop and unknown states


class DaytonaGuard:
    def __init__(
        self,
        *,
        config_path: Path | None = None,
        state_root: Path | None = None,
        api_url: str | None = None,
        fetch: Callable[[str], Any] | None = None,
    ) -> None:
        path = config_path if config_path is not None else (
            Path(__file__).resolve().parents[2] / "policy" / "daytona-limits.yaml"
        )
        self._config = self._load_config(Path(path))
        state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        if state_root is None and not Path(state_home).is_absolute():
            raise GuardUnavailable("InvalidStateRoot")
        self._state_root = Path(state_root) if state_root is not None else (
            Path(state_home) / "evallab" / "daytona-admission"
        )
        self._api_url = (api_url or os.environ.get("DAYTONA_API_URL") or _DEFAULT_API_URL).rstrip("/")
        url = urllib.parse.urlsplit(self._api_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise GuardUnavailable("InvalidAPIURL")
        self._fetch = fetch if fetch is not None else self._http_get
        self._owner = uuid.uuid4().hex
        self._owned: set[str] = set()

    @staticmethod
    def _load_config(path: Path) -> dict[str, Any]:
        try:
            config = yaml.safe_load(path.read_text())
        except Exception as error:
            raise GuardUnavailable(type(error).__name__) from None
        if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise GuardUnavailable("InvalidConfig")
        _text(config.get("organization_id"))
        if config.get("region") != "us" or config.get("sandbox_class") != "container":
            raise GuardUnavailable("UnsupportedConfigTarget")
        if _number(config.get("safety_fraction")) != 0.8:
            raise GuardUnavailable("InvalidSafetyFraction")
        _resources(config.get("quota"), candidate=True)
        caps = config.get("per_sandbox")
        if not isinstance(caps, dict) or set(caps) != set(_DIMENSIONS):
            raise GuardUnavailable("InvalidPerSandboxLimits")
        for dim in _DIMENSIONS:
            if caps[dim] is None:
                if dim != "gpu":
                    raise GuardUnavailable("MissingPerSandboxLimit")
            elif _number(caps[dim]) == 0 and dim != "gpu":
                raise GuardUnavailable("InvalidPerSandboxLimit")
        if config.get("concurrent_sandboxes", "missing") is not None:
            raise GuardUnavailable("UnverifiedConcurrentLimit")
        sources = config.get("sources")
        if not isinstance(sources, dict):
            raise GuardUnavailable("MissingLimitSources")
        for key in ("usage", "usage_observed_at", "dashboard_limits", "dashboard_observed_at",
                    "wallet", "wallet_observed_at"):
            _text(sources.get(key))
        if config.get("monetary_api") != "unavailable":
            raise GuardUnavailable("UnverifiedMonetaryLimit")
        wallet = config.get("wallet_observed")
        if not isinstance(wallet, dict):
            raise GuardUnavailable("MissingMonetarySource")
        for key in ("free_usd", "paid_usd", "month_spend_usd"):
            _number(wallet.get(key))
        return config

    def _http_get(self, path: str) -> tuple[int, Any]:
        key = os.environ.get("DAYTONA_API_KEY")
        if not key:
            raise GuardUnavailable("CredentialsUnavailable")
        request = urllib.request.Request(
            self._api_url + path,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                if response.status != 200:
                    return int(response.status), None
                return 200, json.loads(response.read())
        except urllib.error.HTTPError as error:
            # Do not read or quote the HTTP body (even a non-JSON 404 is a 404).
            status = error.code
            error.close()
            return int(status), None
        except Exception as error:
            raise GuardUnavailable(type(error).__name__) from None

    def _get(self, path: str, *, allow_missing: bool = False) -> Any:
        try:
            result = self._fetch(path)
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_missing:
                return _MISSING
            raise GuardUnavailable(f"HTTP {int(error.code)}") from None
        except Exception as error:
            # Never copy a callback's exception message (it may contain secrets).
            if isinstance(error, GuardUnavailable) and self._fetch == self._http_get:
                raise
            raise GuardUnavailable(type(error).__name__) from None
        if isinstance(result, tuple):
            if len(result) != 2 or isinstance(result[0], bool) or not isinstance(result[0], int):
                raise GuardUnavailable("InvalidHTTPResponse")
            status, body = result
            if status == 404 and allow_missing:
                return _MISSING
            if status != 200:
                raise GuardUnavailable(f"HTTP {status}")
            return body
        return result

    def _target(self, region: str | None, sandbox_class: str) -> tuple[str, str]:
        target = region if region is not None else (
            os.environ.get("DAYTONA_TARGET") or self._config["region"]
        )
        if target != self._config["region"] or sandbox_class != self._config["sandbox_class"]:
            raise GuardUnavailable("UnsupportedTarget")
        return target, sandbox_class

    def _identity(self) -> None:
        # Select only this field; the remaining response may contain the API key.
        record = self._get("/api-keys/current")
        if not isinstance(record, dict) or record.get("organizationId") != self._config["organization_id"]:
            raise GuardUnavailable("OrganizationMismatch")

    def _inventory(self) -> list[dict[str, Any]]:
        inventory: list[dict[str, Any]] = []
        cursors: set[str] = set()
        ids: set[str] = set()
        path = _INVENTORY_PATH
        for _ in range(1000):
            page = self._get(path)
            if not isinstance(page, dict) or "nextCursor" not in page or not isinstance(page.get("items"), list):
                raise GuardUnavailable("IncompleteInventory")
            for raw in page["items"]:
                if not isinstance(raw, dict):
                    raise GuardUnavailable("InvalidSandbox")
                sandbox_id = _text(raw.get("id"))
                if sandbox_id in ids:
                    raise GuardUnavailable("InconsistentInventory")
                ids.add(sandbox_id)
                org_id = raw.get("organizationId")
                if org_id is not None and org_id != self._config["organization_id"]:
                    raise GuardUnavailable("OrganizationMismatch")
                # Unknown target/class cannot be assigned a known pool safely.
                item = {
                    "id": sandbox_id,
                    "name": _text(raw.get("name")),
                    "region": _text(raw.get("target")),
                    "sandbox_class": _text(raw.get("sandboxClass")),
                    "state": _text(raw.get("state")),
                    **{dim: _number(raw.get(field)) for dim, field in _API_FIELDS.items()},
                }
                if item["sandbox_class"] not in {"container", *_VM_CLASSES}:
                    raise GuardUnavailable("UnknownSandboxClass")
                inventory.append(item)
            cursor = page["nextCursor"]
            if cursor is None:
                return inventory
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise GuardUnavailable("InvalidInventoryCursor")
            cursors.add(cursor)
            path = _INVENTORY_PATH + "&cursor=" + urllib.parse.quote(cursor, safe="")
        raise GuardUnavailable("IncompleteInventory")

    def _live(self, region: str, sandbox_class: str) -> dict[str, Any]:
        self._identity()
        org = urllib.parse.quote(self._config["organization_id"], safe="")
        usage = self._get(f"/organizations/{org}/usage")
        if not isinstance(usage, dict) or not isinstance(usage.get("regionUsage"), list):
            raise GuardUnavailable("IncompleteUsage")
        matches = []
        for entry in usage["regionUsage"]:
            if not isinstance(entry, dict):
                raise GuardUnavailable("InvalidUsage")
            if entry.get("regionId") == region and entry.get("sandboxClass") == sandbox_class:
                matches.append(entry)
        if len(matches) != 1:
            raise GuardUnavailable("AmbiguousRegionUsage")
        entry = matches[0]
        limits: dict[str, float] = {}
        telemetry = _vector()
        per_sandbox: dict[str, float | None] = {}
        cap_sources: dict[str, str] = {}
        for dim, (quota_field, usage_field, sandbox_field) in _USAGE_FIELDS.items():
            limits[dim] = min(_number(self._config["quota"][dim]), _number(entry.get(quota_field)))
            telemetry[dim] = _number(entry.get(usage_field))
            committed_cap = self._config["per_sandbox"][dim]
            live_cap = entry.get(sandbox_field) if sandbox_field is not None else None
            if sandbox_field is not None and sandbox_field not in entry:
                raise GuardUnavailable("IncompletePerSandboxLimits")
            if live_cap is None:
                per_sandbox[dim] = committed_cap
                cap_sources[dim] = "dated_dashboard" if committed_cap is not None else "unavailable"
            else:
                numeric_cap = _number(live_cap)
                per_sandbox[dim] = min(_number(committed_cap), numeric_cap) if committed_cap is not None else numeric_cap
                cap_sources[dim] = "min_live_api_and_dated_dashboard"
        inventory = self._inventory()
        inventory_usage = _vector()
        for item in inventory:
            if item["region"] == region and item["sandbox_class"] == sandbox_class:
                accounted = _counts(item)
                for dim in inventory_usage:
                    inventory_usage[dim] = _number(inventory_usage[dim] + accounted[dim])
        telemetry["concurrent_sandboxes"] = inventory_usage["concurrent_sandboxes"]
        used = {dim: max(telemetry[dim], inventory_usage[dim]) for dim in telemetry}
        pressure = [
            dim for dim in _DIMENSIONS
            if (limits[dim] > 0 and used[dim] >= limits[dim] * 0.8)
            or (limits[dim] == 0 and used[dim] > 0)
        ]
        return {
            "schema_version": 1,
            "observed_at": _iso(),
            "organization_id": self._config["organization_id"],
            "api_url": self._api_url,
            "region": region,
            "sandbox_class": sandbox_class,
            "source": "live-api:organizations-usage+inventory-max",
            "limit_sources": copy.deepcopy(self._config["sources"]),
            "monetary": {
                "api_status": "unavailable",
                "credit_ceiling_usd": None,
                "live_credit_headroom_usd": None,
                "dashboard_observed_at": self._config["sources"]["wallet_observed_at"],
                "dashboard_source": self._config["sources"]["wallet"],
                "dated_dashboard_observation": copy.deepcopy(self._config["wallet_observed"]),
                "spending_authorization": False,
            },
            "limits": {**limits, "concurrent_sandboxes": None},
            "per_sandbox_limits": per_sandbox,
            "per_sandbox_limit_sources": cap_sources,
            "safety_fraction": 0.8,
            "used": used,
            "telemetry_used": telemetry,
            "inventory_used": inventory_usage,
            "usage_source": {
                dim: "tie" if telemetry[dim] == inventory_usage[dim] else (
                    "telemetry" if telemetry[dim] > inventory_usage[dim] else "inventory"
                ) for dim in _DIMENSIONS
            },
            "pending": _vector(),
            "inventory": inventory,
            "at_or_near_limit": pressure,
            "reasons": [],
        }

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._state_root / "reservations.lock", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise GuardUnavailable(type(error).__name__) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as error:
            raise GuardUnavailable(type(error).__name__) from None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _state(self) -> dict[str, dict[str, Any]]:
        path = self._state_root / "reservations.json"
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as error:
            raise GuardUnavailable(type(error).__name__) from None
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data["schema_version"] != 1 or not isinstance(data.get("reservations"), dict)):
            raise GuardUnavailable("InvalidReservationState")
        entries = data["reservations"]
        for name, entry in entries.items():
            _text(name)
            if not isinstance(entry, dict):
                raise GuardUnavailable("InvalidReservationState")
            _resources(entry.get("resources"), candidate=True)
            if "requested_resources" in entry:
                requested = _resources(entry["requested_resources"], candidate=True)
                if any(requested[dim] > entry["resources"][dim] for dim in _DIMENSIONS):
                    raise GuardUnavailable("InvalidReservationState")
            for field in ("owner", "region", "sandbox_class", "organization_id", "api_url", "created_at", "expires_at"):
                _text(entry.get(field))
            _number(entry.get("expires_at_epoch"))
        return entries

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        fd, name = tempfile.mkstemp(prefix="reservations-", suffix=".tmp", dir=self._state_root)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"schema_version": 1, "reservations": entries}, file, allow_nan=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self._state_root / "reservations.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _same_pool(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        return all(entry[field] == snapshot[field] for field in
                   ("organization_id", "api_url", "region", "sandbox_class"))

    def _reconcile(
        self, entries: dict[str, dict[str, Any]], snapshot: dict[str, Any], now: float,
    ) -> dict[str, dict[str, Any]]:
        visible = {(item["name"], item["region"], item["sandbox_class"]) for item in snapshot["inventory"]}
        return {
            name: dict(entry) for name, entry in entries.items()
            if entry["expires_at_epoch"] > now
            and not (entry["organization_id"] == snapshot["organization_id"]
                     and entry["api_url"] == snapshot["api_url"]
                     and (name, entry["region"], entry["sandbox_class"]) in visible)
        }

    def _pending(
        self, entries: dict[str, dict[str, Any]], snapshot: dict[str, Any], *, omit: str | None = None,
    ) -> dict[str, float]:
        result = _vector()
        for name, entry in entries.items():
            if name == omit or not self._same_pool(entry, snapshot):
                continue
            for dim in _DIMENSIONS:
                result[dim] = _number(result[dim] + entry["resources"][dim])
            result["concurrent_sandboxes"] += 1
        return result

    @staticmethod
    def _request(resources: dict[str, float], count: int) -> tuple[dict[str, float] | None, list[str]]:
        try:
            candidate = _resources(resources, candidate=True)
        except GuardUnavailable:
            return None, ["invalid-resources"]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return None, ["invalid-count"]
        try:
            scaled = {dim: value * count for dim, value in candidate.items()}
            requested = _vector(scaled, count=count)
        except OverflowError:
            return None, ["invalid-count"]
        if any(not math.isfinite(value) for value in requested.values()):
            return None, ["invalid-count"]
        return requested, []

    @staticmethod
    def _decide(
        snapshot: dict[str, Any], resources: dict[str, float], count: int,
        pending: dict[str, float], *, already_visible: bool = False,
        reserved_resources: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        requested, reasons = DaytonaGuard._request(resources, count)
        valid_request = requested is not None
        requested = requested or _vector()
        for dim, cap in snapshot["per_sandbox_limits"].items():
            if valid_request and cap is not None and resources[dim] > cap:
                reasons.append(f"per-sandbox-{dim}-exceeded")
        # Nominal request and conservative hold differ after an owned resize.
        # An earlier creation might be ambiguous, so shrinking cannot release
        # previously held capacity until the provider inventory is authoritative.
        increment = requested if reserved_resources is None else _vector(reserved_resources, count=1)
        if already_visible:
            increment = _vector()
        projected = {
            dim: _number(snapshot["used"][dim] + pending[dim] + increment[dim])
            for dim in pending
        }
        for dim in _DIMENSIONS:
            if projected[dim] > snapshot["limits"][dim] * 0.8:
                reasons.append(f"projected-{dim}-over-cap")
        snapshot.update(pending=pending, requested=requested, projected=projected,
                        admitted=not reasons, reasons=reasons)
        if reserved_resources is not None:
            snapshot["reservation_resources"] = _vector(reserved_resources, count=1)
        return snapshot

    @staticmethod
    def _require_admitted(snapshot: dict[str, Any]) -> dict[str, Any]:
        if not snapshot["admitted"]:
            raise AdmissionRefused("Daytona admission refused: " + ", ".join(snapshot["reasons"]), snapshot)
        return snapshot

    def observe(self, *, region: str | None = None, sandbox_class: str = "container") -> dict[str, Any]:
        target, klass = self._target(region, sandbox_class)
        with self._locked():
            snapshot = self._live(target, klass)
            entries = self._reconcile(self._state(), snapshot, time.time())
            snapshot["pending"] = self._pending(entries, snapshot)
            snapshot["projected"] = {
                dim: snapshot["used"][dim] + snapshot["pending"][dim]
                for dim in snapshot["used"]
            }
            return snapshot

    def check(
        self, resources: dict[str, float], *, count: int = 1,
        region: str | None = None, sandbox_class: str = "container",
    ) -> dict[str, Any]:
        target, klass = self._target(region, sandbox_class)
        with self._locked():
            snapshot = self._live(target, klass)
            entries = self._reconcile(self._state(), snapshot, time.time())
            return self._require_admitted(self._decide(
                snapshot, resources, count, self._pending(entries, snapshot)))

    def reserve(
        self, name: str, resources: dict[str, float], *, ttl_seconds: int,
        region: str | None = None, sandbox_class: str = "container",
    ) -> dict[str, Any]:
        _text(name)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise GuardUnavailable("InvalidReservationLifetime")
        _number(ttl_seconds)
        target, klass = self._target(region, sandbox_class)
        with self._locked():
            snapshot = self._live(target, klass)
            original = self._state()
            now = time.time()
            entries = self._reconcile(original, snapshot, now)
            existing = entries.get(name)
            visible = [item for item in snapshot["inventory"] if item["name"] == name]
            own = (existing is not None and existing["owner"] == self._owner
                   and self._same_pool(existing, snapshot))
            pending = self._pending(entries, snapshot, omit=name if own else None)
            request, _ = self._request(resources, 1)
            held = None
            if request is not None and own and existing is not None:
                held = {
                    dim: max(existing["resources"][dim], request[dim])
                    for dim in _DIMENSIONS
                }
            snapshot = self._decide(
                snapshot, resources, 1, pending, already_visible=bool(visible),
                reserved_resources=held,
            )
            if existing is not None and not own:
                snapshot["reasons"].append("reservation-name-owned-by-another-runner")
            if visible:
                if len(visible) != 1 or not self._same_pool({
                    **visible[0], "organization_id": snapshot["organization_id"],
                    "api_url": snapshot["api_url"],
                }, snapshot):
                    snapshot["reasons"].append("sandbox-name-pool-mismatch")
                elif request is not None and any(visible[0][dim] != request[dim] for dim in _DIMENSIONS):
                    snapshot["reasons"].append("sandbox-name-resources-mismatch")
                elif visible[0]["state"].lower() in {"stopped", "paused", *_FREE_STATES}:
                    # This interface admits creation, not reactivation. A name
                    # retaining only disk (or no quota) cannot justify ignoring
                    # a new request's compute allocation on a create retry.
                    snapshot["reasons"].append("sandbox-name-not-active")
            snapshot["admitted"] = not snapshot["reasons"]
            self._require_admitted(snapshot)
            if visible:
                snapshot["reservation_status"] = "already-visible"
            elif existing is not None and own:
                actual = _resources(resources, candidate=True)
                snapshot["reservation_status"] = (
                    "reused" if existing.get("requested_resources", existing["resources"]) == actual
                    else "resized"
                )
                existing["requested_resources"] = actual
                existing["resources"] = held
                # Never shorten a lifetime on retry; a later provider creation
                # might run for the newly supplied TTL from this retry.
                existing["expires_at_epoch"] = max(existing["expires_at_epoch"], now + ttl_seconds)
                existing["expires_at"] = _iso(existing["expires_at_epoch"])
            else:
                entries[name] = {
                    "owner": self._owner,
                    "organization_id": snapshot["organization_id"],
                    "api_url": snapshot["api_url"],
                    "region": target,
                    "sandbox_class": klass,
                    "resources": _resources(resources, candidate=True),
                    "requested_resources": _resources(resources, candidate=True),
                    "created_at": _iso(now),
                    "expires_at": _iso(now + ttl_seconds),
                    "expires_at_epoch": now + ttl_seconds,
                }
                snapshot["reservation_status"] = "new"
            if entries != original:
                self._save(entries)
            if not visible:
                self._owned.add(name)
            return snapshot

    def release(self, name: str) -> None:
        """Caller asserts known teardown/noncreation/visibility; clear only own name."""
        _text(name)
        if name not in self._owned:
            return
        with self._locked():
            entries = self._state()
            entry = entries.get(name)
            if entry is not None and entry["owner"] == self._owner:
                del entries[name]
                self._save(entries)
            self._owned.discard(name)

    def sandbox_missing(self, sandbox_id: str) -> bool:
        record = self._get("/sandbox/" + urllib.parse.quote(_text(sandbox_id), safe=""), allow_missing=True)
        return record is _MISSING

    def snapshot_resources(self, name: str) -> tuple[dict[str, float], str]:
        record = self._get("/snapshots/" + urllib.parse.quote(_text(name), safe=""))
        if not isinstance(record, dict):
            raise GuardUnavailable("InvalidSnapshot")
        # SnapshotDto calls RAM 'mem'; no guessed defaults, including GPU.
        fields = {**_API_FIELDS, "memory_gib": "mem"}
        resources = _resources({dim: record.get(field) for dim, field in fields.items()}, candidate=True)
        return resources, _text(record.get("sandboxClass"))

    def default_snapshot(self) -> str:
        record = self._get("/config")
        if not isinstance(record, dict):
            raise GuardUnavailable("InvalidConfigResponse")
        return _text(record.get("defaultSnapshot"))
