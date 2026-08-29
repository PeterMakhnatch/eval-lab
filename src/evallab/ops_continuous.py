"""Disabled-by-default continuous-loop operator validation (Ops/Runner adapters).

Not the Platform control-plane daemon. Never starts launchd, systemd, Docker,
Harbor, or billable runs. Writes only to an explicit operator state directory.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import queue
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import ValidationError, field_validator, model_validator

from evallab.evidence_store import load_blob, store_blob
from evallab.execution_contracts import CONTROL_AGENTS, PaidRunAuthorization, load_policy
from evallab.schemas import ContractModel, StandingApprovalsPolicy

MODES = frozenset({"DISABLED", "PAUSED", "RUNNING", "DRAINING", "MAINTENANCE", "KILLED"})
KILL_DISPOSITION = "FAILED_OPERATOR_KILL"
DEFAULT_MODE = "DISABLED"
REQUIRED_SCOPE = "continuous-loop"
HEARTBEAT_SKEW_SECONDS = 2.0
KILLED_ALLOWED_COMMANDS = frozenset(
    {"kill", "recover", "drain", "status", "rotate-logs", "rotate-cas"}
)
TRUST_KINDS = frozenset({"approval", "budget", "recovery"})
SHA256_HEX = 64
STATE_DIR_MODE = 0o700
STATE_FILE_MODE = 0o600

REASON_MISSING_ENABLE_TOKEN = "missing_enable_token"
REASON_MISSING_STANDING_APPROVAL = "missing_standing_approval"
REASON_MISSING_BUDGET = "missing_budget"
REASON_MISSING_SECRET = "missing_secret"
REASON_STALE_HEARTBEAT = "stale_heartbeat"
REASON_DRAIN_INCOMPLETE = "drain_incomplete"
REASON_DEFAULT_DISABLED = "default_disabled"
REASON_SAME_IDENTITY = "same_enable_and_approval_identity"
REASON_BILLABLE_REFUSED = "billable_refused"
REASON_RECOVERY_SPENT = "recovery_spent"
FILE_SECRET_PREFIX = "file:/run/secrets/"
PINNED_LINUX_SECRET_NAME = "evallab-approval-hmac"
PINNED_LINUX_SECRET_PATH = Path("/run/secrets") / PINNED_LINUX_SECRET_NAME
PINNED_LINUX_REF = FILE_SECRET_PREFIX + PINNED_LINUX_SECRET_NAME
PINNED_KEYCHAIN_SERVICE = "EvalLab"
PINNED_KEYCHAIN_ACCOUNT = "evallab-approval-hmac"
PINNED_KEYCHAIN_REF = f"keychain:{PINNED_KEYCHAIN_SERVICE}/{PINNED_KEYCHAIN_ACCOUNT}"
LINUX_TRUST_MANIFEST_PATH = Path("/etc/evallab/trusted-approval-keys.json")
MACOS_TRUST_MANIFEST_PATH = Path("/Library/Application Support/EvalLab/trusted-approval-keys.json")
PINNED_INIT_IMAGE = (
    "python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f"
)
ALLOWED_KEY_MODES = frozenset({0o400, 0o440})
TERMINAL_LEASE_STATUSES = frozenset({"settled", "terminal", "complete", "cancelled", "failed"})
TERMINAL_QUEUE_STATES = TERMINAL_LEASE_STATUSES
JOURNAL_DIRNAME = "journal"
JOURNAL_CURRENT = "current.json"
JOURNAL_PENDING = "pending.json"
MACOS_STATE_REL = Path("Library/Application Support/EvalLab")
MACOS_LOG_REL = Path("Library/Logs/EvalLab")
LAUNCHD_STATE_TOKEN = "__EVAL_LAB_STATE_DIR__"
LAUNCHD_LOG_TOKEN = "__EVAL_LAB_LOG_DIR__"
FORBIDDEN_KEY_ENVS = frozenset(
    "EVAL_LAB_" + suffix
    for suffix in (
        "HMAC_KEY",
        "TRUST_MAC_KEY",
        "APPROVAL_MAC_KEY",
        "RECOVERY_TOKEN",
        "HMAC_KEY_REF",
    )
)

CLOSED_REASONS = frozenset(
    {
        REASON_MISSING_ENABLE_TOKEN,
        REASON_MISSING_STANDING_APPROVAL,
        REASON_MISSING_BUDGET,
        REASON_MISSING_SECRET,
        REASON_STALE_HEARTBEAT,
        REASON_DRAIN_INCOMPLETE,
        REASON_DEFAULT_DISABLED,
        REASON_SAME_IDENTITY,
        REASON_BILLABLE_REFUSED,
        REASON_RECOVERY_SPENT,
    }
)

SECRET_REF_GRAMMAR = re.compile(r"^keychain:[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,64}$")
FILE_SECRET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
NONCE_GRAMMAR = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
SAFETY_PAYLOAD_KEYS = frozenset({"ok", "reason", "mode", "detail", "running", "authorized"})
AUTH_FIELDS = frozenset({"spec_id", "actor", "authorized_at", "quota_override"})
BUDGET_FIELDS = AUTH_FIELDS | frozenset({"scope", "expires_at", "ceiling_usd"})


class TrustStore(Protocol):
    """External deployment trust store. Never committed test key fingerprints."""

    def allowed_key_ids(self) -> frozenset[str]:
        """Set of active and previous 64-char hex key fingerprints."""

    def manifest_digest(self) -> str:
        """SHA-256 digest of the deployment trust manifest."""


class DeploymentTrustStore:
    def __init__(self, manifest_path: Path | None = None) -> None:
        self.manifest_path = manifest_path or (
            MACOS_TRUST_MANIFEST_PATH if sys.platform == "darwin" else LINUX_TRUST_MANIFEST_PATH
        )

    def _load_manifest(self) -> tuple[frozenset[str], str]:
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            return frozenset(), ""
        try:
            info = os.stat(self.manifest_path, follow_symlinks=False)
        except OSError:
            return frozenset(), ""
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o222):
            return frozenset(), ""
        try:
            raw_bytes = self.manifest_path.read_bytes()
            loaded = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return frozenset(), ""
        if not isinstance(loaded, dict):
            return frozenset(), ""
        active = loaded.get("active_key_id")
        previous = loaded.get("previous_key_id")
        allowed = loaded.get("allowed_key_ids")
        ids: set[str] = set()
        for cand in (active, previous):
            if isinstance(cand, str) and len(cand) == SHA256_HEX and all(c in "0123456789abcdef" for c in cand.lower()):
                ids.add(cand.lower())
        if isinstance(allowed, list):
            for cand in allowed:
                if isinstance(cand, str) and len(cand) == SHA256_HEX and all(c in "0123456789abcdef" for c in cand.lower()):
                    ids.add(cand.lower())
        if not ids:
            return frozenset(), ""
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return frozenset(ids), digest

    def allowed_key_ids(self) -> frozenset[str]:
        ids, _ = self._load_manifest()
        return ids

    def manifest_digest(self) -> str:
        _, digest = self._load_manifest()
        return digest


class WorkloadOwner(Protocol):
    """Campaign/queue owner. Operator never synthesizes worker settlement."""

    def request_cancel(self, lease_ids: list[str]) -> Mapping[str, Any]:
        """Issue cancellation through the campaign/queue owner."""

    def observe_lease(self, lease_id: str) -> Mapping[str, Any] | None:
        """Poll queue/worker/catalog. None means missing/unknown."""


class ClosedWorkloadOwner:
    def request_cancel(self, lease_ids: list[str]) -> Mapping[str, Any]:
        return {
            "requested": True,
            "executed": False,
            "owner": "campaign-queue",
            "lease_ids": list(lease_ids),
        }

    def observe_lease(self, lease_id: str) -> Mapping[str, Any] | None:
        del lease_id
        return None


class SloFreshnessPolicy(ContractModel):
    max_queue_admission_lag_seconds: float
    max_dispatch_latency_seconds: float
    max_oldest_postrun_lag_seconds: float
    max_oldest_catalog_settle_lag_seconds: float
    max_oldest_quality_lag_seconds: float
    max_oldest_projection_lag_seconds: float
    max_oldest_analysis_lag_seconds: float
    status_snapshot_max_age_seconds: float

    @field_validator(
        "max_queue_admission_lag_seconds",
        "max_dispatch_latency_seconds",
        "max_oldest_postrun_lag_seconds",
        "max_oldest_catalog_settle_lag_seconds",
        "max_oldest_quality_lag_seconds",
        "max_oldest_projection_lag_seconds",
        "max_oldest_analysis_lag_seconds",
        "status_snapshot_max_age_seconds",
    )
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("SLO fields must be positive")
        return value


class OperationalLimitsPolicy(ContractModel):
    scheduler_heartbeat_interval_seconds: float
    scheduler_stale_after_seconds: float
    worker_heartbeat_interval_seconds: float
    lease_ttl_seconds: float
    fencing_grace_seconds: float
    max_concurrent_workers: int
    postrun_hook_timeout_seconds: float
    maintenance_drain_timeout_seconds: float
    maintenance_disk_threshold_bytes: int

    @field_validator(
        "scheduler_heartbeat_interval_seconds",
        "scheduler_stale_after_seconds",
        "worker_heartbeat_interval_seconds",
        "lease_ttl_seconds",
        "fencing_grace_seconds",
        "postrun_hook_timeout_seconds",
        "maintenance_drain_timeout_seconds",
        "maintenance_disk_threshold_bytes",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("operational limit must be positive")
        return value

    @field_validator("max_concurrent_workers")
    @classmethod
    def _workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrent_workers must be >= 1")
        return value


class QualityAndQuarantinePolicy(ContractModel):
    quarantine_rolling_window_size: int
    min_window_attempts_for_calculation: int
    max_quarantine_fraction: float
    max_warn_fraction: float
    catalog_ingestion_warn_after_seconds: float
    catalog_ingestion_pause_after_seconds: float
    max_consecutive_quiet_failures: int
    auto_acceptance_enabled: bool

    @model_validator(mode="after")
    def _safe(self) -> QualityAndQuarantinePolicy:
        if self.auto_acceptance_enabled:
            raise ValueError("auto_acceptance_enabled must be false")
        if not 0 <= self.max_quarantine_fraction <= 1:
            raise ValueError("max_quarantine_fraction must be in [0, 1]")
        if not 0 <= self.max_warn_fraction <= 1:
            raise ValueError("max_warn_fraction must be in [0, 1]")
        if self.quarantine_rolling_window_size < 1:
            raise ValueError("quarantine_rolling_window_size must be >= 1")
        if self.min_window_attempts_for_calculation < 1:
            raise ValueError("min_window_attempts_for_calculation must be >= 1")
        if self.max_consecutive_quiet_failures < 1:
            raise ValueError("max_consecutive_quiet_failures must be >= 1")
        if self.catalog_ingestion_warn_after_seconds <= 0:
            raise ValueError("catalog_ingestion_warn_after_seconds must be positive")
        if self.catalog_ingestion_pause_after_seconds <= 0:
            raise ValueError("catalog_ingestion_pause_after_seconds must be positive")
        return self


class ContinuousLoopPolicy(ContractModel):
    policy_schema_version: str
    spec_id: str
    approval_signature_ref: str
    approval_digest: str
    slo_freshness: SloFreshnessPolicy
    operational_limits: OperationalLimitsPolicy
    quality_and_quarantine: QualityAndQuarantinePolicy

    @field_validator("policy_schema_version", "spec_id")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required string empty")
        return value

    @field_validator("approval_signature_ref")
    @classmethod
    def _signature_ref(cls, value: str) -> str:
        if not value.strip() or not signature_ref_allowed(value):
            raise ValueError("approval_signature_ref must name an allowlisted key source")
        return value

    @field_validator("approval_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != SHA256_HEX or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError("approval_digest must be sha256 hex")
        return value.lower()


def signature_ref_allowed(value: str) -> bool:
    return value in {PINNED_LINUX_REF, PINNED_KEYCHAIN_REF}


def canonical_binding_payload(
    *,
    policy: Mapping[str, Any],
    budget: Mapping[str, Any],
    scope: list[str] | None = None,
    spec_id: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
    nonce: str | None = None,
    key_id: str | None = None,
    kind: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bytes:
    body = {key: policy[key] for key in policy if key != "approval_digest"}
    envelope: dict[str, Any] = {
        "budget": {
            "ceiling_usd": budget.get("ceiling_usd"),
            "expires_at": budget.get("expires_at"),
            "scope": list(budget.get("scope") or []),
        },
        "policy": body,
        "scope": list(scope or budget.get("scope") or []),
        "spec_id": spec_id or policy.get("spec_id"),
    }
    if issued_at is not None:
        envelope["issued_at"] = issued_at
    if expires_at is not None:
        envelope["expires_at"] = expires_at
    if nonce is not None:
        envelope["nonce"] = nonce
    if key_id is not None:
        envelope["key_id"] = key_id
    if kind is not None:
        envelope["kind"] = kind
    if extra:
        envelope.update(dict(extra))
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def bind_policy_digest(
    *,
    policy: Mapping[str, Any],
    budget: Mapping[str, Any],
    mac_key: bytes,
) -> str:
    if not mac_key:
        raise ValueError("mac key required")
    return hmac.new(
        mac_key,
        canonical_binding_payload(policy=policy, budget=budget),
        hashlib.sha256,
    ).hexdigest()


def public_sha256_is_not_a_signature(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def key_id_for(key: bytes) -> str:
    return hashlib.sha256(b"evallab.operator.key_id.v1\0" + key).hexdigest()


def _outside_state(path: Path, state_dir: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(state_dir.resolve(strict=False))
    except ValueError:
        return True
    return False


def _escapes_or_symlinks(root: Path, path: Path) -> bool:
    try:
        root_resolved = root.resolve(strict=False)
        rel = path.resolve(strict=False).relative_to(root_resolved)
    except ValueError:
        return True
    current = root
    for part in rel.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _is_readonly_mount(path: Path) -> bool:
    try:
        vfs = os.statvfs(path)
    except OSError:
        return False
    flag = getattr(os, "ST_RDONLY", 1)
    return bool(vfs.f_flag & flag)


def load_file_key(
    path: Path,
    *,
    state_dir: Path,
    secrets_root: Path,
    allowed_key_ids: frozenset[str],
) -> bytes:
    if path.name not in {PINNED_LINUX_SECRET_NAME, f"{PINNED_LINUX_SECRET_NAME}.previous"}:
        return b""
    if path.is_symlink() or not path.is_file() or _escapes_or_symlinks(secrets_root, path):
        return b""
    if not _outside_state(path, state_dir):
        return b""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return b""
    if not stat.S_ISREG(info.st_mode):
        return b""
    mode = info.st_mode & 0o777
    if mode not in ALLOWED_KEY_MODES:
        return b""
    if info.st_mode & 0o222:
        return b""
    try:
        if os.access(path, os.W_OK):
            return b""
    except OSError:
        return b""
    try:
        data = path.read_bytes().strip()
    except OSError:
        return b""
    if len(data) < 32:
        return b""
    if not allowed_key_ids or key_id_for(data) not in allowed_key_ids:
        return b""
    return data


def load_macos_keychain_secret(ref: str, *, allowed_key_ids: frozenset[str] | None = None) -> bytes | None:
    allowed = {PINNED_KEYCHAIN_REF, PINNED_KEYCHAIN_REF + ".previous"}
    if ref not in allowed:
        return None
    if sys.platform != "darwin" or not os.access("/usr/bin/security", os.X_OK):
        return None
    account = PINNED_KEYCHAIN_ACCOUNT
    if ref.endswith(".previous"):
        account = f"{PINNED_KEYCHAIN_ACCOUNT}.previous"
    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", PINNED_KEYCHAIN_SERVICE, "-a", account, "-w"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    key = completed.stdout.strip()
    if len(key) < 32:
        return None
    if allowed_key_ids is not None and (not allowed_key_ids or key_id_for(key) not in allowed_key_ids):
        return None
    return key


def load_keychain_key(
    ref: str,
    *,
    secret_store: Callable[[str], bytes | None] | None,
    allowed_key_ids: frozenset[str],
) -> bytes:
    if ref not in {PINNED_KEYCHAIN_REF, PINNED_KEYCHAIN_REF + ".previous"}:
        return b""
    if secret_store is not None:
        try:
            data = secret_store(ref)
        except OSError:
            return b""
    else:
        data = load_macos_keychain_secret(ref, allowed_key_ids=allowed_key_ids)
    if not isinstance(data, (bytes, bytearray)):
        return b""
    key = bytes(data).strip()
    if len(key) < 32 or not allowed_key_ids or key_id_for(key) not in allowed_key_ids:
        return b""
    return key


def _linux_secret_candidates(environ: Mapping[str, str]) -> list[Path]:
    cred = environ.get("CREDENTIALS_DIRECTORY", "")
    paths: list[Path] = []
    if cred:
        root = Path(cred)
        if root.is_absolute() and root.name and not root.is_symlink():
            paths.append(root / PINNED_LINUX_SECRET_NAME)
            paths.append(root / f"{PINNED_LINUX_SECRET_NAME}.previous")
    paths.append(PINNED_LINUX_SECRET_PATH)
    paths.append(Path(str(PINNED_LINUX_SECRET_PATH) + ".previous"))
    return paths


def load_keyring(
    *,
    ref: str,
    state_dir: Path,
    secrets_root: Path | None,
    secret_store: Callable[[str], bytes | None] | None,
    environ: Mapping[str, str] | None = None,
    trust_store: TrustStore | None = None,
) -> dict[str, bytes]:
    del secrets_root
    if not signature_ref_allowed(ref):
        return {}
    allowed_ids = trust_store.allowed_key_ids() if trust_store is not None else frozenset()
    if not allowed_ids:
        return {}
    keys: list[bytes] = []
    env = environ or {}
    if ref == PINNED_LINUX_REF:
        seen: set[Path] = set()
        for path in _linux_secret_candidates(env):
            resolved = path if path in seen else path
            if resolved in seen:
                continue
            seen.add(resolved)
            root = resolved.parent
            if env.get("CREDENTIALS_DIRECTORY") and str(root) == env.get("CREDENTIALS_DIRECTORY"):
                if resolved.name != PINNED_LINUX_SECRET_NAME and resolved.name != f"{PINNED_LINUX_SECRET_NAME}.previous":
                    continue
                if resolved.is_symlink() or root.is_symlink():
                    continue
                if not _is_readonly_mount(root):
                    continue
            loaded = load_file_key(resolved, state_dir=state_dir, secrets_root=root, allowed_key_ids=allowed_ids)
            if loaded:
                keys.append(loaded)
    elif ref == PINNED_KEYCHAIN_REF:
        keys.append(load_keychain_key(PINNED_KEYCHAIN_REF, secret_store=secret_store, allowed_key_ids=allowed_ids))
        loaded = load_keychain_key(PINNED_KEYCHAIN_REF + ".previous", secret_store=secret_store, allowed_key_ids=allowed_ids)
        if loaded:
            keys.append(loaded)
    ring: dict[str, bytes] = {}
    for key in keys:
        kid = key_id_for(key)
        if kid in allowed_ids:
            ring[kid] = key
    return ring


def load_mac_key(
    environ: Mapping[str, str],
    *,
    secret_store: Callable[[str], bytes | None] | None,
    state_dir: Path,
    policy: ContinuousLoopPolicy | None,
    secrets_root: Path | None,
    trust_store: TrustStore | None = None,
) -> dict[str, bytes]:
    if any(environ.get(name) for name in FORBIDDEN_KEY_ENVS):
        return {}
    if policy is None:
        return {}
    if policy.approval_signature_ref not in {PINNED_LINUX_REF, PINNED_KEYCHAIN_REF}:
        return {}
    return load_keyring(
        ref=policy.approval_signature_ref,
        state_dir=state_dir,
        secrets_root=None,
        secret_store=secret_store,
        environ=environ,
        trust_store=trust_store,
    )


def parse_paid_authorization(
    raw: Mapping[str, Any] | None,
    *,
    now: datetime,
    allowed_fields: frozenset[str] = AUTH_FIELDS,
) -> PaidRunAuthorization | None:
    if not isinstance(raw, dict):
        return None
    if set(raw) - allowed_fields:
        return None
    try:
        spec_id = raw["spec_id"]
        actor = raw["actor"]
        authorized_at = raw["authorized_at"]
    except KeyError:
        return None
    if not isinstance(spec_id, str) or not spec_id.strip():
        return None
    if not isinstance(actor, str) or not actor.strip():
        return None
    if raw.get("quota_override"):
        return None
    try:
        if isinstance(authorized_at, datetime):
            stamped = authorized_at
        else:
            stamped = datetime.fromisoformat(str(authorized_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    stamped = stamped.replace(tzinfo=UTC) if stamped.tzinfo is None else stamped.astimezone(UTC)
    if stamped > now:
        return None
    return PaidRunAuthorization(
        spec_id=spec_id, actor=actor, authorized_at=stamped, quota_override=False
    )



def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def trust_root_for(state_dir: Path, environ: Mapping[str, str]) -> Path:
    del environ
    return state_dir / "trust"


def _index_mac(entries: Mapping[str, str], store_key: bytes) -> str:
    payload = json.dumps(dict(entries), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(store_key, payload, hashlib.sha256).hexdigest()


def load_trust_index(root: Path, store_keys: bytes | Mapping[str, bytes]) -> dict[str, str]:
    if isinstance(store_keys, (bytes, bytearray)):
        keys = [bytes(store_keys)] if store_keys else []
    else:
        keys = [bytes(key) for key in store_keys.values() if key]
    if not keys:
        return {}
    raw = _load_json_mapping(root / "index.json")
    if not raw:
        return {}
    entries = raw.get("entries")
    mac = raw.get("mac")
    if not isinstance(entries, dict) or not isinstance(mac, str):
        return {}
    cleaned = {str(key): str(value) for key, value in entries.items() if isinstance(value, str)}
    for key in keys:
        if hmac.compare_digest(mac, _index_mac(cleaned, key)):
            return cleaned
    return {}


def put_trusted_record(
    root: Path,
    store_key: bytes,
    record: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    kill_digest: str | None = None,
) -> str:
    kind = str(record.get("kind", ""))
    spec_id = str(record.get("spec_id", ""))
    if kind not in TRUST_KINDS or not spec_id:
        raise ValueError("trusted record requires kind and spec_id")
    if not store_key:
        raise ValueError("store key required")
    signed = dict(record)
    signed.setdefault("key_id", key_id_for(store_key))
    signed.setdefault("issued_at", signed.get("authorized_at"))
    if not isinstance(signed.get("nonce"), str) or not NONCE_GRAMMAR.fullmatch(str(signed.get("nonce"))):
        raise ValueError("trusted record requires nonce")
    extra = None
    if kind == "recovery":
        signed["kill_digest"] = kill_digest or signed.get("kill_digest")
        extra = {
            "kill_digest": signed.get("kill_digest"),
            "actor": signed.get("actor"),
            "fenced_ids": list(signed.get("fenced_ids") or []),
            "settlement_digests": list(signed.get("settlement_digests") or []),
        }
    bind_budget = dict(budget or {
        "ceiling_usd": signed.get("ceiling_usd"),
        "expires_at": signed.get("expires_at"),
        "scope": list(signed.get("scope") or []),
    })
    signed["binding_budget"] = {
        "ceiling_usd": bind_budget.get("ceiling_usd"),
        "expires_at": bind_budget.get("expires_at"),
        "scope": list(bind_budget.get("scope") or signed.get("scope") or []),
    }
    mac_payload = canonical_binding_payload(
        policy=policy or {"spec_id": spec_id},
        budget=signed["binding_budget"],
        scope=list(signed.get("scope") or []),
        spec_id=spec_id,
        issued_at=str(signed.get("issued_at")),
        expires_at=str(signed.get("expires_at")),
        nonce=str(signed.get("nonce")),
        key_id=str(signed["key_id"]),
        kind=kind,
        extra=extra,
    )
    signed["mac"] = hmac.new(store_key, mac_payload, hashlib.sha256).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(signed, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    uri = store_blob(root, payload)
    entries = load_trust_index(root, store_key)
    entries[f"{kind}:{spec_id}"] = uri
    index = {"entries": entries, "mac": _index_mac(entries, store_key)}
    _write_text(root / "index.json", json.dumps(index, indent=2, sort_keys=True))
    return uri


def lookup_trusted_record(
    root: Path,
    store_keys: bytes | Mapping[str, bytes],
    *,
    kind: str,
    spec_id: str,
    now: datetime,
    policy: Mapping[str, Any] | None = None,
    standing: StandingApprovalsPolicy | None = None,
    kill_digest: str | None = None,
) -> tuple[PaidRunAuthorization | None, dict[str, Any]]:
    if isinstance(store_keys, (bytes, bytearray)):
        keyring = {key_id_for(bytes(store_keys)): bytes(store_keys)} if store_keys else {}
    else:
        keyring = {str(key_id): bytes(key) for key_id, key in store_keys.items() if key}
    if kind not in TRUST_KINDS or not spec_id or not keyring:
        return None, {}
    entries = load_trust_index(root, keyring)
    uri = entries.get(f"{kind}:{spec_id}")
    if not uri:
        return None, {}
    try:
        loaded = json.loads(load_blob(root, uri).decode("utf-8"))
    except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None, {}
    if not isinstance(loaded, dict):
        return None, {}
    if loaded.get("kind") != kind or loaded.get("spec_id") != spec_id:
        return None, {}
    signer = loaded.get("signer") or loaded.get("actor")
    scope = loaded.get("scope")
    nonce = loaded.get("nonce")
    key_id = loaded.get("key_id")
    mac = loaded.get("mac")
    issued_raw = loaded.get("issued_at") or loaded.get("authorized_at")
    if not isinstance(signer, str) or not signer.strip():
        return None, {}
    if not isinstance(scope, list) or REQUIRED_SCOPE not in scope:
        return None, {}
    if not isinstance(nonce, str) or not NONCE_GRAMMAR.fullmatch(nonce):
        return None, {}
    if not isinstance(key_id, str) or key_id not in keyring:
        return None, {}
    if not isinstance(mac, str) or not isinstance(issued_raw, str):
        return None, {}
    expires_raw = loaded.get("expires_at")
    if not isinstance(expires_raw, str):
        return None, {}
    try:
        expires = _aware(datetime.fromisoformat(expires_raw.replace("Z", "+00:00")))
        issued = _aware(datetime.fromisoformat(str(issued_raw).replace("Z", "+00:00")))
    except ValueError:
        return None, {}
    skew = timedelta(seconds=HEARTBEAT_SKEW_SECONDS)
    if issued > now + skew or expires <= now:
        return None, {}
    extra = None
    if kind == "recovery":
        digest = loaded.get("kill_digest")
        if not isinstance(digest, str) or len(digest) != SHA256_HEX:
            return None, {}
        if kill_digest is not None and not hmac.compare_digest(digest, kill_digest):
            return None, {}
        fenced_ids = loaded.get("fenced_ids") or []
        settlement_digests = loaded.get("settlement_digests") or []
        if not isinstance(fenced_ids, list) or not isinstance(settlement_digests, list):
            return None, {}
        extra = {
            "kill_digest": digest,
            "actor": loaded.get("actor"),
            "fenced_ids": list(fenced_ids),
            "settlement_digests": list(settlement_digests),
        }
    bind_budget = loaded.get("binding_budget")
    if not isinstance(bind_budget, dict):
        bind_budget = {
            "ceiling_usd": loaded.get("ceiling_usd"),
            "expires_at": loaded.get("expires_at"),
            "scope": list(scope),
        }
    mac_payload = canonical_binding_payload(
        policy=policy or {"spec_id": spec_id},
        budget=bind_budget,
        scope=list(scope),
        spec_id=spec_id,
        issued_at=str(issued_raw),
        expires_at=str(expires_raw),
        nonce=nonce,
        key_id=key_id,
        kind=kind,
        extra=extra,
    )
    expected = hmac.new(keyring[key_id], mac_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None, {}
    auth = parse_paid_authorization(
        {
            "spec_id": spec_id,
            "actor": loaded.get("actor"),
            "authorized_at": loaded.get("authorized_at") or issued_raw,
            "quota_override": loaded.get("quota_override", False),
        },
        now=now + skew,
    )
    if auth is None:
        return None, {}
    out: dict[str, Any] = {
        "actor": auth.actor,
        "signer": signer,
        "scope": list(scope),
        "expires_at": expires.isoformat(),
        "issued_at": issued.isoformat(),
        "spec_id": spec_id,
        "kind": kind,
        "nonce": nonce,
        "key_id": key_id,
    }
    if kind == "budget":
        ceiling = loaded.get("ceiling_usd")
        if not isinstance(ceiling, (int, float)) or ceiling <= 0:
            return None, {}
        if standing is not None and ceiling > standing.daily_cost_ceiling_usd:
            return None, {}
        out["ceiling_usd"] = ceiling
    if kind == "recovery":
        out["jti"] = nonce
        out["kill_digest"] = loaded.get("kill_digest")
        out["fenced_ids"] = list(loaded.get("fenced_ids") or [])
        out["settlement_digests"] = list(loaded.get("settlement_digests") or [])
    return auth, out


def consume_trusted_record(
    root: Path,
    store_key: bytes | Mapping[str, bytes],
    *,
    kind: str,
    spec_id: str,
) -> bool:
    if kind not in TRUST_KINDS or not spec_id or not store_key:
        return False
    entries = load_trust_index(root, store_key)
    index_bytes: bytes = (
        bytes(store_key)
        if isinstance(store_key, (bytes, bytearray))
        else next(iter(store_key.values()), b"")
    )
    key = f"{kind}:{spec_id}"
    if key not in entries:
        return False
    del entries[key]
    if not index_bytes:
        return False
    index = {"entries": entries, "mac": _index_mac(entries, index_bytes)}
    _write_text(root / "index.json", json.dumps(index, indent=2, sort_keys=True))
    return True


def recovery_spent(state_dir: Path) -> set[str]:
    spent: set[str] = set()
    nonce_dir = state_dir / "nonces"
    if nonce_dir.is_dir():
        for child in nonce_dir.iterdir():
            if child.is_file():
                spent.add(child.name)
    path = state_dir / "recovery-spent.jsonl"
    if not path.is_file():
        return spent
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                token = row.get("nonce") or row.get("jti")
                if isinstance(token, str):
                    spent.add(token)
    except (OSError, json.JSONDecodeError):
        return spent
    return spent


def consume_nonce_atomic(state_dir: Path, nonce: str) -> bool:
    if not NONCE_GRAMMAR.fullmatch(nonce):
        return False
    directory = state_dir / "nonces"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, STATE_DIR_MODE)
    path = directory / nonce
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, STATE_FILE_MODE)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"nonce": nonce}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def digest_kill_record(state_dir: Path) -> str:
    path = state_dir / "kill.json"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def load_standing_policy(path: Path | None) -> StandingApprovalsPolicy | None:
    if path is None or not path.is_file():
        return None
    try:
        return load_policy(path)
    except ValueError:
        return None


def secret_ref_valid(value: str) -> bool:
    return bool(SECRET_REF_GRAMMAR.fullmatch(value))


@dataclass(frozen=True)
class OperatorVerdict:
    ok: bool
    reason: str | None
    mode: str
    detail: str
    payload: dict[str, Any]

    def exit_code(self) -> int:
        return 0 if self.ok else 2


@dataclass
class OperatorContext:
    state_dir: Path
    now: datetime
    enable_token: str
    enable_identity: str
    approval: PaidRunAuthorization | None
    budget: PaidRunAuthorization | None
    standing: StandingApprovalsPolicy | None
    secret_ref: str
    secret_present: bool
    policy: ContinuousLoopPolicy | None
    agent: str
    drain_timeout_seconds: float | None
    env_self_asserted_approval: bool
    env_self_asserted_budget: bool
    mac_key: bytes
    keyring: dict[str, bytes]
    budget_payload: dict[str, Any]
    recovery: PaidRunAuthorization | None
    recovery_jti: str
    recovery_nonce: str
    recovery_kill_digest: str
    recovery_fenced_ids: list[str]
    recovery_settlement_digests: list[str]
    log_dir: Path
    owner: WorkloadOwner
    trust_store: TrustStore
    trust_manifest_digest: str


def load_loop_policy(path: Path | None) -> ContinuousLoopPolicy | None:
    if path is None or not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None
    body = raw.get("continuous_loop_policy", raw)
    if not isinstance(body, dict):
        return None
    try:
        return ContinuousLoopPolicy.model_validate(body)
    except ValidationError:
        return None


def policy_complete(policy: Mapping[str, Any] | ContinuousLoopPolicy | None) -> bool:
    if isinstance(policy, ContinuousLoopPolicy):
        return True
    if not isinstance(policy, dict):
        return False
    try:
        ContinuousLoopPolicy.model_validate(policy)
    except ValidationError:
        return False
    return True


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_json_mapping(path: Path) -> dict[str, Any] | None:
    if path.name == "kill.json":
        snapshot = load_operator_snapshot(path.parent)
        if snapshot is not None and isinstance(snapshot.get("kill"), dict):
            return snapshot["kill"]
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _secure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, STATE_DIR_MODE)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.chmod(path, STATE_FILE_MODE)


def _append_event(state_dir: Path, event: Mapping[str, Any]) -> None:
    path = state_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
    os.chmod(path, STATE_FILE_MODE)


def read_mode(state_dir: Path) -> str:
    snapshot = load_operator_snapshot(state_dir)
    if snapshot is not None:
        value = snapshot.get("mode")
        if isinstance(value, str) and value in MODES:
            return value
    value = _read_text(state_dir / "mode")
    return value if value in MODES else DEFAULT_MODE


def write_mode(state_dir: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    _write_text(state_dir / "mode", mode)


def _fsync_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STATE_FILE_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, STATE_FILE_MODE)


@contextmanager
def state_lock(state_dir: Path):
    """Acquire exclusive flock on O_NOFOLLOW lockfile in state_dir."""
    _secure_state_dir(state_dir)
    lock_path = state_dir / ".lock"
    # Reject symlinked lockfile
    if lock_path.is_symlink():
        raise OSError("symlinked state lockfile rejected")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, STATE_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_operator_snapshot(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / JOURNAL_DIRNAME / JOURNAL_CURRENT
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _materialize_snapshot_views(state_dir: Path, snapshot: Mapping[str, Any]) -> None:
    mode = snapshot.get("mode")
    if isinstance(mode, str) and mode in MODES:
        write_mode(state_dir, mode)
    if "inflight" in snapshot:
        _atomic_write_json(state_dir / "inflight.json", snapshot.get("inflight") or [])
    if "leases" in snapshot:
        _atomic_write_json(state_dir / "leases.json", snapshot.get("leases") or [])
    if "observations" in snapshot:
        _atomic_write_json(state_dir / "observations.json", snapshot.get("observations") or [])
    if snapshot.get("kill") is not None:
        _atomic_write_json(state_dir / "kill.json", snapshot["kill"])
    if snapshot.get("drain") is not None:
        _atomic_write_json(state_dir / "drain.json", snapshot["drain"])


def commit_operator_snapshot(state_dir: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Commit snapshot atomically with unique temp file, fsync, and generation increment."""
    journal = state_dir / JOURNAL_DIRNAME
    journal.mkdir(parents=True, exist_ok=True)
    os.chmod(journal, STATE_DIR_MODE)
    current_snap = load_operator_snapshot(state_dir) or {}
    prev_gen = current_snap.get("generation", 0)
    snap_dict = dict(snapshot)
    if "generation" not in snap_dict or snap_dict["generation"] <= prev_gen:
        snap_dict["generation"] = prev_gen + 1
    payload = json.dumps(snap_dict, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    import time
    txn_name = f"txn_{os.getpid()}_{time.time_ns()}.tmp"
    txn_path = journal / txn_name
    current = journal / JOURNAL_CURRENT
    _fsync_write(txn_path, payload)
    os.replace(txn_path, current)
    dirfd = os.open(journal, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    _materialize_snapshot_views(state_dir, snap_dict)
    return snap_dict


def recover_journal_views(state_dir: Path) -> None:
    snapshot = load_operator_snapshot(state_dir)
    if snapshot is not None:
        _materialize_snapshot_views(state_dir, snapshot)


def _atomic_write_json(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    import time
    tmp = path.with_name(f"{path.name}.{os.getpid()}_{time.time_ns()}.tmp")
    _fsync_write(tmp, encoded)
    os.replace(tmp, path)


def apply_mode_transition(state_dir: Path, target: str, *, command: str) -> str | None:
    """Centralize legality through the serialized authoritative journal snapshot path."""
    if target not in MODES:
        return "illegal_transition"
    with state_lock(state_dir):
        current = read_mode(state_dir)
        if current == target:
            return None
        if current == "KILLED" and command != "recover":
            return "killed_latched"
        if current == "DRAINING" and command not in {"drain", "kill"}:
            return "draining_latched"
        if target == "KILLED" and command != "kill":
            return "illegal_transition"
        if target == "DRAINING" and command != "drain":
            return "illegal_transition"
        if current == "KILLED" and target != "DISABLED":
            return "killed_latched"
        if current == "KILLED" and command != "recover":
            return "killed_latched"
        snap = dict(load_operator_snapshot(state_dir) or {})
        snap["mode"] = target
        snap.setdefault("inflight", _load_inflight(state_dir)[0] or [])
        snap.setdefault("leases", _load_leases(state_dir)[0] or [])
        commit_operator_snapshot(state_dir, snap)
        return None


def _latched_kill(state_dir: Path) -> bool:
    return read_mode(state_dir) == "KILLED"


def _refuse_if_killed(ctx: OperatorContext, action: str) -> OperatorVerdict | None:
    if not _latched_kill(ctx.state_dir):
        return None
    return _verdict(
        ctx,
        ok=False,
        reason=REASON_DEFAULT_DISABLED,
        detail=f"{action} refused; emergency KILLED latch held",
    )


def _refuse_if_latched(ctx: OperatorContext, action: str) -> OperatorVerdict | None:
    mode = read_mode(ctx.state_dir)
    if mode == "KILLED":
        return _refuse_if_killed(ctx, action)
    if mode == "DRAINING":
        return _verdict(
            ctx,
            ok=False,
            reason=REASON_DRAIN_INCOMPLETE,
            detail=f"{action} refused; DRAINING latch held until drain settles inflight",
        )
    return None


def context_from_env(
    *,
    state_dir: Path,
    policy_path: Path | None,
    now: datetime,
    agent: str,
    drain_timeout_seconds: float | None,
    environ: Mapping[str, str],
    secret_store: Callable[[str], bytes | None] | None = None,
    secrets_root: Path | None = None,
    owner: WorkloadOwner | None = None,
    trust_store: TrustStore | None = None,
) -> OperatorContext:
    resolved_trust_store = trust_store if trust_store is not None else DeploymentTrustStore()
    secret_probe = state_dir / "secret_present"
    standing_path = (
        Path(environ["EVAL_LAB_STANDING_POLICY"])
        if environ.get("EVAL_LAB_STANDING_POLICY")
        else state_dir / "standing-approvals.yaml"
    )
    policy = load_loop_policy(policy_path)
    standing = load_standing_policy(standing_path)
    env_self_asserted_approval = bool(environ.get("EVAL_LAB_STANDING_APPROVAL"))
    env_self_asserted_budget = environ.get("EVAL_LAB_BUDGET_PRESENT", "") in {"1", "true", "yes"}
    keyring = load_mac_key(
        environ,
        secret_store=secret_store,
        state_dir=state_dir,
        policy=policy,
        secrets_root=secrets_root,
        trust_store=resolved_trust_store,
    )
    mac_key = next(iter(keyring.values()), b"")
    trust_root = trust_root_for(state_dir, environ)
    spec_id = policy.spec_id if policy is not None else ""
    policy_dump = policy.model_dump(mode="json") if policy is not None else None
    approval, _approval_extra = lookup_trusted_record(
        trust_root,
        keyring,
        kind="approval",
        spec_id=spec_id,
        now=now,
        policy=policy_dump,
        standing=standing,
    )
    budget, budget_payload = lookup_trusted_record(
        trust_root,
        keyring,
        kind="budget",
        spec_id=spec_id,
        now=now,
        policy=policy_dump,
        standing=standing,
    )
    recovery, recovery_extra = lookup_trusted_record(
        trust_root,
        keyring,
        kind="recovery",
        spec_id=spec_id,
        now=now,
        policy=policy_dump,
        standing=standing,
        kill_digest=digest_kill_record(state_dir),
    )
    if budget is not None and "ceiling_usd" not in budget_payload:
        budget = None
        budget_payload = {}
    secret_ref = environ.get("EVAL_LAB_SECRET_REF", "")
    secret_present = secret_probe.is_file() or environ.get("EVAL_LAB_SECRET_PRESENT", "") in {
        "1",
        "true",
        "yes",
    }
    enable_token = environ.get("EVAL_LAB_ENABLE_TOKEN", "")
    enable_identity = environ.get("EVAL_LAB_ENABLE_IDENTITY", "") or enable_token
    log_raw = environ.get("EVAL_LAB_OPERATOR_LOG_DIR", "")
    log_dir = Path(log_raw) if log_raw else state_dir / "logs"
    return OperatorContext(
        state_dir=state_dir,
        now=now,
        enable_token=enable_token,
        enable_identity=enable_identity,
        approval=approval,
        budget=budget,
        standing=standing,
        secret_ref=secret_ref,
        secret_present=secret_present,
        policy=policy,
        agent=agent,
        drain_timeout_seconds=drain_timeout_seconds,
        env_self_asserted_approval=env_self_asserted_approval,
        env_self_asserted_budget=env_self_asserted_budget,
        mac_key=mac_key,
        keyring=keyring,
        budget_payload=budget_payload,
        recovery=recovery,
        recovery_jti=str(recovery_extra.get("jti", "")),
        recovery_nonce=str(recovery_extra.get("nonce", "")),
        recovery_kill_digest=str(recovery_extra.get("kill_digest", "")),
        recovery_fenced_ids=list(recovery_extra.get("fenced_ids") or []),
        recovery_settlement_digests=list(recovery_extra.get("settlement_digests") or []),
        log_dir=log_dir,
        owner=owner if owner is not None else ClosedWorkloadOwner(),
        trust_store=resolved_trust_store,
        trust_manifest_digest=resolved_trust_store.manifest_digest(),
    )


def _verdict(
    ctx: OperatorContext,
    *,
    ok: bool,
    reason: str | None,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> OperatorVerdict:
    mode = read_mode(ctx.state_dir)
    payload = {
        "ok": ok,
        "reason": reason,
        "mode": mode,
        "detail": detail,
        "running": False,
        "authorized": False,
    }
    if extra:
        payload["details"] = {
            key: value for key, value in extra.items() if key not in SAFETY_PAYLOAD_KEYS
        }
    _append_event(
        ctx.state_dir,
        {
            "at": ctx.now.isoformat(),
            "ok": ok,
            "reason": reason,
            "mode": mode,
            "detail": detail,
        },
    )
    return OperatorVerdict(ok=ok, reason=reason, mode=mode, detail=detail, payload=payload)


def _heartbeat_stale(ctx: OperatorContext) -> bool:
    limits = ctx.policy.operational_limits if ctx.policy is not None else None
    stale_after = None if limits is None else limits.scheduler_stale_after_seconds
    path = ctx.state_dir / "heartbeat"
    if stale_after is None:
        return False
    if not path.is_file():
        return True
    raw = _read_text(path)
    try:
        stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    stamped = stamped.astimezone(UTC)
    skew = timedelta(seconds=HEARTBEAT_SKEW_SECONDS)
    if stamped > ctx.now + skew:
        return True
    if stamped > ctx.now:
        return False
    age = ctx.now - stamped
    return age > timedelta(seconds=float(stale_after))


def admission_reason(ctx: OperatorContext) -> str | None:
    has_side_inputs = bool(
        ctx.approval
        or ctx.budget
        or ctx.enable_identity
        or ctx.secret_ref
        or ctx.secret_present
        or ctx.env_self_asserted_approval
        or ctx.env_self_asserted_budget
        or ctx.policy is not None
    )
    if not ctx.enable_token:
        return REASON_MISSING_ENABLE_TOKEN if has_side_inputs else REASON_DEFAULT_DISABLED
    if ctx.env_self_asserted_approval and ctx.approval is None:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.approval is None:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.enable_identity == ctx.approval.actor:
        return REASON_SAME_IDENTITY
    if ctx.env_self_asserted_budget and ctx.budget is None:
        return REASON_MISSING_BUDGET
    if ctx.budget is None or ctx.standing is None:
        return REASON_MISSING_BUDGET
    if ctx.budget.actor in {ctx.enable_identity, ctx.approval.actor}:
        return REASON_SAME_IDENTITY
    if not secret_ref_valid(ctx.secret_ref) or not ctx.secret_present:
        return REASON_MISSING_SECRET
    if ctx.policy is None:
        return REASON_DEFAULT_DISABLED
    if ctx.policy.spec_id != ctx.approval.spec_id or ctx.policy.spec_id != ctx.budget.spec_id:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.approval.spec_id != ctx.budget.spec_id:
        return REASON_MISSING_STANDING_APPROVAL
    if not signature_ref_allowed(ctx.policy.approval_signature_ref):
        return REASON_MISSING_STANDING_APPROVAL
    if not ctx.keyring or not ctx.budget_payload:
        return REASON_MISSING_STANDING_APPROVAL
    dumped = ctx.policy.model_dump(mode="json")
    digest_ok = False
    for key in ctx.keyring.values():
        expected = bind_policy_digest(policy=dumped, budget=ctx.budget_payload, mac_key=key)
        if hmac.compare_digest(ctx.policy.approval_digest, expected):
            digest_ok = True
            break
    if not digest_ok:
        return REASON_MISSING_STANDING_APPROVAL
    if _heartbeat_stale(ctx):
        return REASON_STALE_HEARTBEAT
    return None


def _fenced_mode(state_dir: Path) -> str | None:
    mode = read_mode(state_dir)
    if mode in {"KILLED", "DRAINING"}:
        return mode
    return None


def _load_inflight(state_dir: Path) -> tuple[list[Any] | None, str | None]:
    snapshot = load_operator_snapshot(state_dir)
    if snapshot is not None and "inflight" in snapshot:
        loaded = snapshot.get("inflight")
        if not isinstance(loaded, list):
            return None, "malformed_inflight"
        if any(not isinstance(item, str) or not item.strip() for item in loaded):
            return None, "malformed_inflight"
        return loaded, None
    path = state_dir / "inflight.json"
    if not path.is_file():
        return [], None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "malformed_inflight"
    if not isinstance(loaded, list):
        return None, "malformed_inflight"
    if any(not isinstance(item, str) or not item.strip() for item in loaded):
        return None, "malformed_inflight"
    return loaded, None


def _load_leases(state_dir: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    snapshot = load_operator_snapshot(state_dir)
    if snapshot is not None and "leases" in snapshot:
        loaded = snapshot.get("leases")
        if not isinstance(loaded, list):
            return None, "malformed_leases"
        leases: list[dict[str, Any]] = []
        for item in loaded:
            if not isinstance(item, dict):
                return None, "malformed_leases"
            leases.append(item)
        return leases, None
    path = state_dir / "leases.json"
    if not path.is_file():
        return [], None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "malformed_leases"
    if not isinstance(loaded, list):
        return None, "malformed_leases"
    leases: list[dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            return None, "malformed_leases"
        leases.append(item)
    return leases, None


def _leases_unsettled(state_dir: Path) -> bool:
    return fenced_leases_unsettled(state_dir) is not None


def _lease_evidence(item: Mapping[str, Any]) -> bool:
    evidence = item.get("evidence")
    if isinstance(evidence, str):
        return bool(evidence.strip())
    if isinstance(evidence, dict):
        return bool(evidence)
    return False


def lease_settlement_digest(item: Mapping[str, Any]) -> str:
    """Local hash of operator-held JSON. Not a recovery MAC input."""
    payload = json.dumps(dict(item), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_observations(state_dir: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    snapshot = load_operator_snapshot(state_dir)
    loaded: Any = None
    if snapshot is not None and "observations" in snapshot:
        loaded = snapshot.get("observations")
    elif (state_dir / "observations.json").is_file():
        try:
            loaded = json.loads((state_dir / "observations.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, "malformed_observations"
    else:
        return [], None
    if not isinstance(loaded, list):
        return None, "malformed_observations"
    observations: list[dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            return None, "malformed_observations"
        observations.append(item)
    return observations, None


def recovery_settlement_binding(state_dir: Path) -> tuple[list[str], list[str]] | None:
    kill_record = _load_json_mapping(state_dir / "kill.json") or {}
    fenced = kill_record.get("fenced")
    if not isinstance(fenced, list):
        return None
    ids: list[str] = []
    for item in fenced:
        if not isinstance(item, str) or not item.strip():
            return None
        ids.append(item)
    ids = sorted(ids)
    if not ids:
        return [], []
    observations, error = _load_observations(state_dir)
    if error is not None or observations is None or not observations:
        return None
    by_id: dict[str, dict[str, Any]] = {}
    for item in observations:
        ident = item.get("id")
        if isinstance(ident, str) and ident.strip():
            by_id[ident] = item
    digests: list[str] = []
    for ident in ids:
        rec = by_id.get(ident)
        if rec is None or not observation_is_terminal(rec):
            return None
        digest = rec.get("settlement_digest")
        if not isinstance(digest, str) or len(digest) != SHA256_HEX:
            return None
        digests.append(digest)
    return ids, digests


def fenced_leases_unsettled(state_dir: Path) -> str | None:
    bound = recovery_settlement_binding(state_dir)
    if bound is None:
        return "fenced_lease_missing"
    return None


def observation_is_terminal(obs: Mapping[str, Any] | None) -> bool:
    if not isinstance(obs, dict):
        return False
    if obs.get("alive") is not False:
        return False
    queue_state = obs.get("queue_state") or obs.get("status")
    if not isinstance(queue_state, str) or queue_state not in TERMINAL_QUEUE_STATES:
        return False
    digest = obs.get("settlement_digest")
    if not isinstance(digest, str) or len(digest) != SHA256_HEX:
        return False
    return bool(_lease_evidence(obs) or _lease_evidence({"evidence": obs.get("evidence")}))


def _call_with_deadline(fn: Callable[[], Any], timeout_seconds: float) -> tuple[Any, bool]:
    """Execute fn in a daemon worker thread with a hard timeout.

    Never blocks on executor shutdown or thread termination if fn hangs.
    Returns (result, timed_out).
    """
    q: queue.Queue[tuple[Any, Exception | None]] = queue.Queue(maxsize=1)

    def worker():
        try:
            res = fn()
            q.put((res, None))
        except Exception as exc:
            q.put((None, exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        res, exc = q.get(timeout=max(0.001, timeout_seconds))
        if exc is not None:
            return None, False
        return res, False
    except queue.Empty:
        return None, True


def observe_fenced_leases(
    owner: WorkloadOwner,
    lease_ids: list[str],
    *,
    timeout_seconds: float | None = None,
    per_lease_timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None, list[str]]:
    observed: list[dict[str, Any]] = []
    blockers: list[str] = []
    import time
    start_time = time.monotonic()
    overall_limit = timeout_seconds if timeout_seconds is not None else 30.0
    per_lease_limit = per_lease_timeout_seconds if per_lease_timeout_seconds is not None else min(5.0, overall_limit)

    for lease_id in lease_ids:
        remaining = overall_limit - (time.monotonic() - start_time)
        if remaining <= 0:
            blockers.append(f"timeout:{lease_id}")
            break
        call_timeout = min(per_lease_limit, remaining)
        obs, timed_out = _call_with_deadline(lambda lid=lease_id: owner.observe_lease(lid), call_timeout)
        if timed_out:
            blockers.append(f"timeout:{lease_id}")
            continue
        if obs is None:
            blockers.append(f"missing:{lease_id}")
            continue
        record = dict(obs)
        record["id"] = lease_id
        if record.get("alive") is True:
            blockers.append(f"live:{lease_id}")
            continue
        if not observation_is_terminal(record):
            blockers.append(f"unknown:{lease_id}")
            continue
        observed.append(record)
    if blockers:
        return None, blockers[0], blockers
    return observed, None, []


def _recovery_settled(state_dir: Path) -> str | None:
    kill_record = _load_json_mapping(state_dir / "kill.json") or {}
    if kill_record.get("executed") is not True:
        return "kill_not_executed"
    inflight, inflight_error = _load_inflight(state_dir)
    if inflight_error is not None or inflight:
        return "inflight_remaining"
    missing = fenced_leases_unsettled(state_dir)
    if missing is not None:
        return missing
    return None


def macos_operator_paths(home: Path) -> tuple[Path, Path]:
    return (home / MACOS_STATE_REL).expanduser(), (home / MACOS_LOG_REL).expanduser()


def _reject_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise OSError("symlink ancestor rejected")
        except OSError as exc:
            if "symlink" in str(exc):
                raise
            raise OSError("symlink ancestor rejected") from exc
        parent = current.parent
        if parent == current:
            return
        current = parent


def prepare_macos_operator_dirs(home: Path) -> tuple[Path, Path]:
    _reject_symlink_ancestors(home)
    state_dir, log_dir = macos_operator_paths(home)
    for directory in (state_dir, log_dir):
        _reject_symlink_ancestors(directory.parent)
        directory.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestors(directory)
        os.chmod(directory, STATE_DIR_MODE)
    for name in ("continuous-operator.out", "continuous-operator.err"):
        path = log_dir / name
        _reject_symlink_ancestors(path if path.exists() else path.parent)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        _reject_symlink_ancestors(path)
        os.chmod(path, STATE_FILE_MODE)
    return state_dir, log_dir


def render_launchd_plist(template: Path, dest: Path, *, home: Path) -> Path:
    state_dir, log_dir = prepare_macos_operator_dirs(home)
    rendered_text = template.read_text(encoding="utf-8")
    rendered = rendered_text.replace(LAUNCHD_STATE_TOKEN, str(state_dir.resolve())).replace(
        LAUNCHD_LOG_TOKEN, str(log_dir.resolve())
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(dest.parent)
    dest.write_text(rendered, encoding="utf-8")
    _reject_symlink_ancestors(dest)
    os.chmod(dest, STATE_FILE_MODE)
    return dest


def cmd_validate(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "validate")
    if blocked:
        return blocked
    reason = admission_reason(ctx)
    if reason:
        return _verdict(ctx, ok=False, reason=reason, detail="control plane remains DISABLED")
    return _verdict(
        ctx,
        ok=True,
        reason=None,
        detail="gates passed; unit remains disabled; no service started",
        extra={"gates": "passed"},
    )


def cmd_start(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "start")
    if blocked:
        return blocked
    reason = admission_reason(ctx)
    if reason:
        return _verdict(ctx, ok=False, reason=reason, detail="start refused; remains DISABLED")
    return _verdict(
        ctx,
        ok=False,
        reason=REASON_DEFAULT_DISABLED,
        detail="start refused; units stay disabled; no service started",
    )


def cmd_dry_run(ctx: OperatorContext) -> OperatorVerdict:
    agent = ctx.agent or "oracle"
    if agent not in CONTROL_AGENTS:
        return _verdict(ctx, ok=False, reason=REASON_BILLABLE_REFUSED, detail=f"agent {agent} is billable")
    plan = {
        "would_run": agent,
        "harbor": False,
        "dispatch": False,
        "note": "oracle/nop smoke plan only",
    }
    _write_text(ctx.state_dir / "dry-run-plan.json", json.dumps(plan, indent=2, sort_keys=True))
    return _verdict(ctx, ok=True, reason=None, detail=f"recorded {agent} smoke plan", extra=plan)


def cmd_status(ctx: OperatorContext) -> OperatorVerdict:
    heartbeat = ctx.state_dir / "heartbeat"
    health_path = ctx.state_dir / "health.json"
    if _heartbeat_stale(ctx):
        return _verdict(ctx, ok=False, reason=REASON_STALE_HEARTBEAT, detail="heartbeat older than policy")
    health: dict[str, Any] = {"docker": "unknown", "catalog": "unknown", "disk": "unknown"}
    if health_path.is_file():
        loaded = json.loads(health_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            health.update(loaded)
    extra = {
        "heartbeat": _read_text(heartbeat) or "UNKNOWN",
        "health": health,
        "readiness": "UNKNOWN" if not (ctx.state_dir / "readiness.json").is_file() else "present",
    }
    return _verdict(ctx, ok=True, reason=None, detail="status snapshot", extra=extra)


def cmd_quota(ctx: OperatorContext) -> OperatorVerdict:
    reason = admission_reason(ctx)
    if reason:
        return _verdict(ctx, ok=False, reason=reason, detail="quota refused")
    return _verdict(ctx, ok=True, reason=None, detail="quota gates present; no dispatch")


def cmd_pause(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "pause")
    if blocked:
        return blocked
    illegal = apply_mode_transition(ctx.state_dir, "PAUSED", command="pause")
    if illegal:
        return _verdict(ctx, ok=False, reason=REASON_DEFAULT_DISABLED, detail="pause refused")
    return _verdict(ctx, ok=True, reason=None, detail="recorded pause; no process signalled")


def cmd_maintenance(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "maintenance")
    if blocked:
        return blocked
    illegal = apply_mode_transition(ctx.state_dir, "MAINTENANCE", command="maintenance")
    if illegal:
        return _verdict(ctx, ok=False, reason=REASON_DEFAULT_DISABLED, detail="maintenance refused")
    return _verdict(ctx, ok=True, reason=None, detail="recorded maintenance; no process signalled")


def cmd_restart(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "restart")
    if blocked:
        return blocked
    _write_text(ctx.state_dir / "restart.json", json.dumps({"intended": "restart", "executed": False}))
    illegal = apply_mode_transition(ctx.state_dir, DEFAULT_MODE, command="restart")
    if illegal:
        return _verdict(ctx, ok=False, reason=REASON_DEFAULT_DISABLED, detail="restart refused")
    return _verdict(ctx, ok=True, reason=None, detail="recorded restart intent; unit stays disabled")


def cmd_upgrade(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "upgrade")
    if blocked:
        return blocked
    _write_text(ctx.state_dir / "upgrade.json", json.dumps({"intended": "upgrade", "executed": False}))
    return _verdict(ctx, ok=True, reason=None, detail="recorded upgrade intent; no image pull")


def cmd_rollback(ctx: OperatorContext) -> OperatorVerdict:
    blocked = _refuse_if_latched(ctx, "rollback")
    if blocked:
        return blocked
    _write_text(ctx.state_dir / "rollback.json", json.dumps({"intended": "rollback", "executed": False}))
    return _verdict(ctx, ok=True, reason=None, detail="recorded rollback intent; no unit swapped")


def cmd_recover(ctx: OperatorContext) -> OperatorVerdict:
    with state_lock(ctx.state_dir):
        if not _latched_kill(ctx.state_dir):
            return _verdict(ctx, ok=False, reason=REASON_DEFAULT_DISABLED, detail="recover requires KILLED latch")
        unsettled = _recovery_settled(ctx.state_dir)
        if unsettled is not None:
            return _verdict(
                ctx,
                ok=False,
                reason=REASON_DRAIN_INCOMPLETE,
                detail="recover refused until kill executed, inflight empty, and leases settled",
                extra={"blocker": unsettled, "latched": True},
            )
        reason = admission_reason(ctx)
        if reason:
            return _verdict(ctx, ok=False, reason=reason, detail="recover refused")
        nonce = ctx.recovery_nonce or ctx.recovery_jti
        if ctx.recovery is None or not nonce:
            return _verdict(ctx, ok=False, reason=REASON_SAME_IDENTITY, detail="recovery authorization missing")
        identities = {ctx.enable_token, ctx.enable_identity, ctx.approval.actor if ctx.approval else "", ctx.budget.actor if ctx.budget else ""}
        if ctx.recovery.actor in identities or (ctx.policy is not None and ctx.recovery.spec_id != ctx.policy.spec_id):
            return _verdict(ctx, ok=False, reason=REASON_SAME_IDENTITY, detail="recovery authorization must be distinct")
        expected_digest = digest_kill_record(ctx.state_dir)
        if not expected_digest or not hmac.compare_digest(ctx.recovery_kill_digest, expected_digest):
            return _verdict(ctx, ok=False, reason=REASON_SAME_IDENTITY, detail="recovery is not bound to kill digest")
        bound = recovery_settlement_binding(ctx.state_dir)
        if bound is None:
            return _verdict(ctx, ok=False, reason=REASON_DRAIN_INCOMPLETE, detail="fenced leases missing settlement evidence")
        fenced_ids, settlement_digests = bound
        if list(ctx.recovery_fenced_ids) != fenced_ids or list(ctx.recovery_settlement_digests) != settlement_digests:
            return _verdict(ctx, ok=False, reason=REASON_SAME_IDENTITY, detail="recovery is not bound to fenced settlement")
        if nonce in recovery_spent(ctx.state_dir):
            return _verdict(ctx, ok=False, reason=REASON_RECOVERY_SPENT, detail="recovery nonce already consumed")
        audit = {
            "event": "recovery",
            "at": ctx.now.isoformat(),
            "actor": ctx.recovery.actor,
            "nonce": nonce,
            "jti": nonce,
            "spec_id": ctx.recovery.spec_id,
            "kill_digest": expected_digest,
            "trust_manifest_digest": ctx.trust_manifest_digest,
            "one_time": True,
        }
        _append_event(ctx.state_dir, audit)
        if not consume_nonce_atomic(ctx.state_dir, nonce):
            return _verdict(ctx, ok=False, reason=REASON_RECOVERY_SPENT, detail="recovery nonce already consumed")
        consumed = consume_trusted_record(
            trust_root_for(ctx.state_dir, {}),
            ctx.keyring or ctx.mac_key,
            kind="recovery",
            spec_id=ctx.recovery.spec_id,
        )
        if not consumed:
            return _verdict(ctx, ok=False, reason=REASON_SAME_IDENTITY, detail="recovery record could not be consumed")
        spent_path = ctx.state_dir / "recovery-spent.jsonl"
        with spent_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"nonce": nonce, "jti": nonce, "at": ctx.now.isoformat()}, sort_keys=True) + "\n")
        os.chmod(spent_path, STATE_FILE_MODE)
        snapshot = load_operator_snapshot(ctx.state_dir) or {}
        commit_operator_snapshot(
            ctx.state_dir,
            {
                "mode": DEFAULT_MODE,
                "inflight": snapshot.get("inflight") or [],
                "leases": snapshot.get("leases") or [],
                "observations": snapshot.get("observations") or [],
                "kill": snapshot.get("kill"),
                "drain": snapshot.get("drain"),
                "recovered": True,
            },
        )
        return _verdict(ctx, ok=True, reason=None, detail="kill latch cleared by one-time recovery")


def cmd_drain(ctx: OperatorContext) -> OperatorVerdict:
    """Optimistic Two-Phase CAS: Never hold state_lock across external observer IO."""
    # Phase 1: Snapshot generation & parameters under lock, then release lock
    with state_lock(ctx.state_dir):
        current = read_mode(ctx.state_dir)
        inflight, inflight_error = _load_inflight(ctx.state_dir)
        kill_record = _load_json_mapping(ctx.state_dir / "kill.json")
        snapshot = load_operator_snapshot(ctx.state_dir) or {}
        if kill_record is None and isinstance(snapshot.get("kill"), dict):
            kill_record = snapshot["kill"]
        fenced: list[str] = []
        if isinstance(kill_record, dict) and isinstance(kill_record.get("fenced"), list):
            fenced = [item for item in kill_record["fenced"] if isinstance(item, str) and item.strip()]
        elif inflight:
            fenced = list(inflight)
        if current != "KILLED":
            pending: dict[str, Any] = {
                "mode": "DRAINING",
                "leases": (load_operator_snapshot(ctx.state_dir) or {}).get("leases", _load_leases(ctx.state_dir)[0] or []),
                "kill": kill_record,
                "drain": {"complete": False, "observed": False},
            }
            if inflight is not None:
                pending["inflight"] = inflight
            snapshot = commit_operator_snapshot(ctx.state_dir, pending)
        base_generation = snapshot.get("generation", 0)
        base_mode = read_mode(ctx.state_dir)
        started_raw = _read_text(ctx.state_dir / "drain_started")
        if not started_raw:
            _write_text(ctx.state_dir / "drain_started", ctx.now.isoformat())
        if inflight_error is not None or inflight is None:
            drain = {"inflight": inflight if inflight is not None else [], "complete": False, "malformed": inflight_error}
            return _verdict(ctx, ok=False, reason=REASON_DRAIN_INCOMPLETE, detail="in-flight leases remain until observed settlement", extra=drain)

    # Phase 2: External IO completely OUTSIDE state lock with hard timeout
    timeout = ctx.drain_timeout_seconds if ctx.drain_timeout_seconds is not None else 30.0
    observed, blocker, blockers = observe_fenced_leases(ctx.owner, fenced, timeout_seconds=timeout)

    # Phase 3: Reacquire lock and CAS validate generation & state invariants
    with state_lock(ctx.state_dir):
        current_snap = load_operator_snapshot(ctx.state_dir) or {}
        curr_gen = current_snap.get("generation", 0)
        curr_mode = read_mode(ctx.state_dir)
        curr_kill = _load_json_mapping(ctx.state_dir / "kill.json")
        if curr_kill is None and isinstance(current_snap.get("kill"), dict):
            curr_kill = current_snap["kill"]
        curr_inflight, curr_inflight_err = _load_inflight(ctx.state_dir)

        # Recompute fenced under lock
        curr_fenced: list[str] = []
        if isinstance(curr_kill, dict) and isinstance(curr_kill.get("fenced"), list):
            curr_fenced = [item for item in curr_kill["fenced"] if isinstance(item, str) and item.strip()]
        elif curr_inflight:
            curr_fenced = list(curr_inflight)

        # If state mutated while observing (e.g. concurrent emergency kill), abort CAS without stale overwrite
        if curr_gen != base_generation or curr_mode != base_mode or curr_fenced != fenced:
            drain = {
                "inflight": curr_inflight if curr_inflight is not None else [],
                "complete": False,
                "observed": False,
                "cas_conflict": True,
                "blocker": "state_mutated_during_observation",
            }
            return _verdict(
                ctx,
                ok=False,
                reason=REASON_DRAIN_INCOMPLETE,
                detail="drain CAS aborted: state changed during external observation",
                extra=drain,
            )

        if observed is None:
            drain = {
                "inflight": curr_inflight if curr_inflight is not None else [],
                "complete": False,
                "observed": False,
                "blocker": blocker,
                "blockers": blockers,
            }
            return _verdict(
                ctx,
                ok=False,
                reason=REASON_DRAIN_INCOMPLETE,
                detail="drain polls trusted queue/worker/catalog evidence; live/unknown/missing leases refuse",
                extra=drain,
            )

        next_kill = None
        if curr_kill is not None:
            next_kill = dict(curr_kill)
            next_kill["executed"] = True
            next_kill["inflight"] = []
            next_kill["observed"] = True
        next_mode = "KILLED" if curr_mode == "KILLED" or next_kill is not None else DEFAULT_MODE
        if curr_mode == "KILLED":
            next_mode = "KILLED"
        original_leases, _lease_error = _load_leases(ctx.state_dir)
        drain = {
            "inflight": [],
            "complete": True,
            "observed": True,
            "terminated": [item.get("id") for item in observed],
        }
        commit_operator_snapshot(
            ctx.state_dir,
            {
                "mode": next_mode,
                "inflight": [],
                "leases": original_leases or [],
                "observations": observed,
                "kill": next_kill,
                "drain": drain,
            },
        )
        if next_mode == "KILLED":
            return _verdict(ctx, ok=True, reason=None, detail="observed drain settlement; KILLED latch held until recovery", extra=drain)
        return _verdict(ctx, ok=True, reason=None, detail="observed drain complete; mode DISABLED", extra=drain)


def cmd_kill(ctx: OperatorContext) -> OperatorVerdict:
    # Phase 1: Under short lock, atomically commit KILLED + executed=false FIRST
    with state_lock(ctx.state_dir):
        inflight, inflight_error = _load_inflight(ctx.state_dir)
        fenced = inflight if inflight is not None else []
        record = {
            "disposition": KILL_DISPOSITION,
            "at": ctx.now.isoformat(),
            "executed": False,
            "signalled": False,
            "cancellation_requested": True,
            "owner": "campaign-queue",
            "owner_ack": {"requested": True, "pending": True},
            "fenced": fenced,
            "malformed_inflight": inflight_error,
            "note": "emergency kill latched KILLED; cancellation requested through campaign/queue owner; executed remains false until observed drain",
        }
        leases, _lease_error = _load_leases(ctx.state_dir)
        snapshot: dict[str, Any] = {
            "mode": "KILLED",
            "leases": leases or [],
            "kill": record,
            "drain": {"complete": False},
        }
        if inflight is not None:
            snapshot["inflight"] = fenced
        snap_res = commit_operator_snapshot(ctx.state_dir, snapshot)
        kill_generation = snap_res.get("generation")

    # Phase 2: Outside lock, invoke owner.request_cancel with bounded deadline
    fenced_ids = [item for item in fenced if isinstance(item, str)]
    cancel_res, timed_out = _call_with_deadline(
        lambda: ctx.owner.request_cancel(fenced_ids),
        timeout_seconds=2.0,
    )
    if not timed_out and isinstance(cancel_res, dict):
        # Phase 3: Optional CAS update of owner_ack if same generation under lock
        with state_lock(ctx.state_dir):
            curr_snap = load_operator_snapshot(ctx.state_dir) or {}
            if curr_snap.get("generation") == kill_generation and isinstance(curr_snap.get("kill"), dict):
                curr_kill = dict(curr_snap["kill"])
                curr_kill["owner_ack"] = cancel_res
                curr_kill["owner"] = cancel_res.get("owner", "campaign-queue")
                curr_snap["kill"] = curr_kill
                commit_operator_snapshot(ctx.state_dir, curr_snap)
                record = curr_kill

    return _verdict(ctx, ok=True, reason=None, detail=KILL_DISPOSITION, extra=record)


def cmd_rotate(ctx: OperatorContext, kind: Literal["logs", "cas"]) -> OperatorVerdict:
    record: dict[str, Any] = {"kind": kind, "intended": True, "deleted": False, "root": "state-dir-only"}
    if kind == "logs":
        _secure_state_dir(ctx.log_dir)
        os.chmod(ctx.log_dir, STATE_DIR_MODE)
        stamp = ctx.now.strftime("%Y%m%dT%H%M%S")
        rotated: list[str] = []
        for name in ("continuous-operator.out", "continuous-operator.err"):
            current = ctx.log_dir / name
            if current.is_file() and current.stat().st_size:
                dest = ctx.log_dir / f"{name}.{stamp}"
                current.replace(dest)
                os.chmod(dest, STATE_FILE_MODE)
                rotated.append(dest.name)
            _write_text(current, "")
            os.chmod(current, STATE_FILE_MODE)
        record["rotated"] = rotated
        record["log_dir"] = str(ctx.log_dir)
        record["mode"] = oct(STATE_DIR_MODE)
    _write_text(ctx.state_dir / f"rotate-{kind}.json", json.dumps(record, indent=2))
    return _verdict(ctx, ok=True, reason=None, detail=f"recorded {kind} rotation; no production delete")


COMMANDS = {
    "validate": cmd_validate,
    "start": cmd_start,
    "dry-run": cmd_dry_run,
    "status": cmd_status,
    "quota": cmd_quota,
    "pause": cmd_pause,
    "drain": cmd_drain,
    "restart": cmd_restart,
    "upgrade": cmd_upgrade,
    "rollback": cmd_rollback,
    "maintenance": cmd_maintenance,
    "kill": cmd_kill,
    "recover": cmd_recover,
    "rotate-logs": lambda ctx: cmd_rotate(ctx, "logs"),
    "rotate-cas": lambda ctx: cmd_rotate(ctx, "cas"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="continuous-operator")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--agent", default="oracle")
    parser.add_argument("--drain-timeout-seconds", type=float, default=None)
    return parser


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    secret_store: Callable[[str], bytes | None] | None = None,
    secrets_root: Path | None = None,
    owner: WorkloadOwner | None = None,
    trust_store: TrustStore | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    state_dir = args.state_dir or Path(env.get("EVAL_LAB_OPERATOR_STATE", "operator-state"))
    _secure_state_dir(state_dir)
    recover_journal_views(state_dir)
    if not (state_dir / "mode").exists():
        write_mode(state_dir, DEFAULT_MODE)
    now = clock() if clock is not None else datetime.now(UTC)
    resolved_store = secret_store if secret_store is not None else load_macos_keychain_secret
    ctx = context_from_env(
        state_dir=state_dir,
        policy_path=args.policy,
        now=now,
        agent=args.agent,
        drain_timeout_seconds=args.drain_timeout_seconds,
        environ=env,
        secret_store=resolved_store,
        secrets_root=secrets_root,
        owner=owner if owner is not None else ClosedWorkloadOwner(),
        trust_store=trust_store,
    )
    mode = read_mode(state_dir)
    if mode == "KILLED" and args.command not in KILLED_ALLOWED_COMMANDS:
        blocked = _refuse_if_killed(ctx, args.command)
        verdict = blocked if blocked is not None else COMMANDS[args.command](ctx)
    elif mode == "DRAINING" and args.command in {"pause", "restart", "maintenance", "start", "validate", "upgrade", "rollback"}:
        blocked = _refuse_if_latched(ctx, args.command)
        verdict = blocked if blocked is not None else COMMANDS[args.command](ctx)
    else:
        verdict = COMMANDS[args.command](ctx)
    sys.stdout.write(json.dumps(verdict.payload, indent=2, sort_keys=True) + "\n")
    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
