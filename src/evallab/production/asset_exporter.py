"""Verified-asset export with receipt re-verification, plus the Model Lab training gate.

``VerifiedAssetExporter`` reads ``asset.verified`` events from
``ContinuousWorkQueue.read_events`` and re-verifies the underlying
execution/verifier receipts before exporting a row. Event names, inline
``measured``/``verified_differential`` flags and inline reward claims are never
trusted on their own: every claimed measurement must be reproduced from the
actual receipt files the producer referenced.

Producer-compatible contracts (no parallel invented formats):

* ``repaired_task`` (emitted by ``TaskQualityLiveWorker._qualify_repair``):
  the asset must link through ``qualification_event_id`` to a confirmed
  ``task_quality.repair_qualified`` event whose ``job_id`` resolves to the
  repair job. Qualification requires ``ref_passes=3``, ``nop=0``,
  ``exploit=0`` with three trials per control; the ``control_evidence`` records
  must hold nine distinct evidence references, each loading a measured
  receipt whose reward equals the claimed one (reference ``1.0``, nop/exploit
  ``0.0``). The repaired package must still hash to ``task_package_digest``.
* ``exploit_witness`` (emitted by ``RewardSeekerLiveWorker``): the asset must
  link through ``source_event_id`` to a confirmed ``seeker.exploit_confirmed``
  event. The bound ``exploit_run`` and ``repaired_run`` receipts must
  independently load measured ``1.0`` and ``0.0`` rewards, and the candidate
  digest must recompute from the immutable submission bytes.
* ``labeled_trace``: no production worker currently emits this kind. The
  explicit contract is a verified ``evallab.trace_label/v1`` receipt — a
  confirmed/verified status, a non-``neither`` label, ``dataset_id``/
  ``item_id`` (and ``task_id`` when declared) bound to the asset identity, and
  a ``trace`` path or ``trace_sha256`` bound to the actual labeled trace —
  whose ``{step, quote}`` citations all resolve verbatim against that trace.
  A ``monitor.suspicion`` semantic-triage card is raw model suspicion, not a
  verified receipt, and is never accepted on its own.

Measured receipts are only recognized producer forms: Harbor trial dirs whose
``result.json`` and ``verifier/reward.{json,txt}`` agree, ``run_candidate``
``candidate.json`` bound to its native trial, ``qa.env.observation/v1``
runtime-tool receipts, ``regrade-receipt/v1`` re-scorings, and experiment
``matrix`` receipts. A bare ``{"reward": ...}`` JSON object is never measured
evidence outside the ``verifier/reward.json`` member of a trial directory.
Where a receipt or control record declares identity metadata — task id or
digest, package path, dataset/item ids, or a control role — the declaration
must agree with the qualification record that cites it.

``export_training_bundle`` is the Model Lab Training Gate. It re-resolves every
candidate's ``(dataset_id, item_id)`` through the queue's ``CorpusRegistry`` —
which applies the scoped canonical TEST lock (TB4 TEST names/digests, sealed
item ids, and the MiMo permanent heldouts seal matching aliases across the
canonical and ``register_external`` corpora; secondary key ids seal only their
own rows) — and rejects sealed, unregistered, or conflicting identities
regardless of any caller-supplied split label. No frozen-evaluation token
exists on this path. Recognized identity/provenance fields inside candidate
rows and referenced event payloads are inspected recursively, so a dev
envelope cannot launder heldout content hidden in a nested ``dataset_id``,
``item_id``, ``task_id``, or digest field; arbitrary prose is never scanned.
Candidates that reference journal events (asset, qualification, or receipt
identities) have those references resolved and re-verified; broken or
contradictory links fail closed. Exports are deterministic JSONL plus a
manifest with digests, provenance, and input/accepted/deduplicated/exported
counts with distinct task/mechanism counts; deduplication keys on the exact
``(dataset_id, canonical task name, mechanism_signature)`` tuple where the
canonical task name drops ``@version`` suffixes and namespace prefixes.
Output writes refuse symlink-ancestor escapes and any same-named file whose
existing bytes differ; the rows/manifest pair validates before writing and
leaves no mismatched half-bundle on failure. A durable consumer cursor
acknowledges only after the complete bundle is on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from evallab.production.corpus_queue import WORKER_SYSTEMS, CorpusItem, CorpusRegistry
from evallab.production.seeker_worker import compute_candidate_digest
from evallab.qa import trace_cheap
from evallab.qa.env_export import RUNTIME_TOOLS
from evallab.qa.task import finite_reward
from evallab.registry import task_directory_digest

ASSET_ROW_SCHEMA = "evallab.verified_asset_row/v1"
ASSET_MANIFEST_SCHEMA = "evallab.asset_export/v1"
TRAINING_ROW_SCHEMA = "evallab.training_row/v1"
TRAINING_MANIFEST_SCHEMA = "evallab.training_bundle/v1"
ASSET_KINDS = ("repaired_task", "exploit_witness", "labeled_trace")
TRACE_LABEL_SCHEMA = "evallab.trace_label/v1"
TRACE_LABELS = ("exploit-pass", "defect-fail")
VERIFIED_LABEL_STATUSES = ("confirmed", "verified")
_ASSET_ROWS_NAME = "assets.jsonl"
_ASSET_MANIFEST_NAME = "asset_manifest.json"
_TRAINING_ROWS_NAME = "training_rows.jsonl"
_TRAINING_MANIFEST_NAME = "training_manifest.json"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
#: Candidate keys whose values name journal events the row relies on.
_EVENT_REFERENCE_KEYS = (
    "asset_event_id",
    "event_id",
    "qualification_event_id",
    "source_event_id",
    "receipt_event_id",
    "label_event_id",
)
#: Recognized nested identity/provenance keys. Only these structured fields are
#: inspected during the candidate row scan; free text is never pattern-matched.
_ITEM_CLAIM_KEYS = ("item_id",)
_ITEM_SEAL_KEYS = ("record_id",)
_TASK_CLAIM_KEYS = ("task_id",)
_TASK_SEAL_KEYS = (
    "task_name",
    "task",
    "task_package",
    "repaired_task",
    "original_task",
    "task_dir",
    "package",
)
_DIGEST_CLAIM_KEYS = ("task_digest",)
_DIGEST_SEAL_KEYS = (
    "task_package_digest",
    "repaired_task_digest",
    "package_digest",
    "digest",
)
#: Control roles a receipt or execution record may declare, and the control
#: groups each may stand in for. Exploit replays are explicitly labelled
#: oracle-script controls, so "oracle" receipts can serve the exploit group.
_ROLE_GROUPS = {
    "oracle": {"reference", "exploit"},
    "reference": {"reference"},
    "nop": {"nop"},
    "blank": {"nop"},
    "shortcut": {"exploit"},
    "exploit": {"exploit"},
    "candidate": {"exploit"},
    "solution-directory": {"exploit"},
    "replay": {"exploit"},
    "regrade": {"exploit"},
}
_NESTED_SCAN_DEPTH = 12


class AssetExportError(ValueError):
    """The export could not be set up or safely written."""


class ExporterQueue(Protocol):
    """The synchronous ContinuousWorkQueue surface the exporter consumes."""

    @property
    def registry(self) -> CorpusRegistry: ...

    def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        run_id: str | None = None,
        after_event_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def read_consumer(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def ack_consumer(self, consumer_id: str, seq: int) -> None: ...

    def get_event(self, event_id: str) -> dict[str, Any] | None: ...


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class MeasuredReceipt:
    """A measured scalar reward loaded from a concrete receipt file.

    ``identity`` carries the identity metadata the receipt itself declares
    (task name/digest, package path, control role); the exporter rejects
    declarations that contradict the record citing the receipt.
    """

    value: float
    source: Path
    kind: str
    identity: Mapping[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        return {
            "ref": str(self.source),
            "receipt_kind": self.kind,
            "reward": self.value,
            "receipt_sha256": _sha256_bytes(self.source.read_bytes()),
            **({"identity": dict(self.identity)} if self.identity else {}),
        }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def _claims(data: Mapping[str, Any], keys: Mapping[str, str]) -> dict[str, Any]:
    """Declared identity metadata a receipt carries under known field names."""
    claims: dict[str, Any] = {}
    for source_key, claim in keys.items():
        value = data.get(source_key)
        if isinstance(value, str) and value.strip():
            claims[claim] = value
    return claims


def _result_json_reward(path: Path) -> float | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    rewards = data.get("verifier_result")
    rewards = rewards.get("rewards") if isinstance(rewards, dict) else None
    if not isinstance(rewards, dict):
        return None
    return finite_reward(rewards.get("reward"))


def _verifier_file_reward(path: Path) -> float | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text:
        return None
    if path.name == "reward.json":
        data = _read_json(path)
        if not isinstance(data, dict) or not data:
            return None
        return finite_reward(data.get("reward"))
    try:
        return finite_reward(float(text))
    except ValueError:
        return None


def _trial_dir_reward(directory: Path) -> MeasuredReceipt | None:
    """The verifier reward a Harbor trial dir recorded; all sources must agree."""
    observations: list[tuple[float | None, Path]] = []
    identity: dict[str, Any] = {}
    result_path = directory / "result.json"
    if result_path.is_file() and not result_path.is_symlink():
        data = _read_json(result_path)
        if isinstance(data, dict):
            identity = _claims(
                data,
                {
                    "mode": "role",
                    "agent": "role",
                    "role": "role",
                    "task_name": "task_id",
                    "task_id": "task_id",
                    "dataset_id": "dataset_id",
                    "item_id": "item_id",
                },
            )
        observations.append((_result_json_reward(result_path), result_path))
    verifier = directory / "verifier"
    for name in ("reward.json", "reward.txt"):
        reward_path = verifier / name
        if reward_path.is_file() and not reward_path.is_symlink():
            observations.append((_verifier_file_reward(reward_path), reward_path))
    if not observations:
        return None
    values = {reward for reward, _ in observations}
    if None in values or len(values) != 1:
        return None
    source = observations[0][1]
    reward = observations[0][0]
    assert reward is not None
    return MeasuredReceipt(reward, source, "harbor_trial", identity)


def _candidate_file_reward(path: Path) -> float | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    if (
        data.get("schema") != "evallab.candidate.result/v1"
        or data.get("status") != "measured"
        or data.get("executed") is not True
        or data.get("error")
    ):
        return None
    return finite_reward(data.get("reward"))


def _candidate_file_identity(data: Mapping[str, Any]) -> dict[str, Any]:
    task = data.get("task")
    if not isinstance(task, Mapping):
        return {}
    return _claims(
        task, {"id": "task_id", "digest": "task_digest", "path": "task_package"}
    )


def _receipt_file_reward(path: Path) -> MeasuredReceipt | None:
    """Measured reward from a single JSON receipt file of a known form.

    Only recognized producer receipts qualify. An arbitrary JSON object that
    merely carries a ``reward`` scalar proves nothing about a measured
    execution, so there is deliberately no generic reward fallback: the
    ``reward.json`` inside a Harbor trial dir loads through
    :func:`_verifier_file_reward` instead.
    """
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if schema == "qa.env.observation/v1":
        # Mirror the qa.env witness binding: measured runtime-tool receipts only.
        if data.get("execution_status") != "measured" or data.get("tool") not in RUNTIME_TOOLS:
            return None
        result = data.get("result")
        reward = finite_reward(result.get("reward")) if isinstance(result, dict) else None
        if reward is None:
            return None
        return MeasuredReceipt(
            reward,
            path,
            "observation",
            _claims(data, {"task_id": "task_id", "task_digest": "task_digest"}),
        )
    if schema == "evallab.candidate.result/v1":
        reward = _candidate_file_reward(path)
        if reward is None:
            return None
        return MeasuredReceipt(reward, path, "candidate", _candidate_file_identity(data))
    if data.get("schema_version") == "regrade-receipt/v1":
        if data.get("refusals") or not isinstance(data.get("regraded"), dict):
            return None
        rewards = data["regraded"].get("rewards")
        reward = finite_reward(rewards.get("reward")) if isinstance(rewards, dict) else None
        if reward is None:
            return None
        identity = _claims(data.get("source") or {}, {"task_name": "task_id"})
        if "task_id" not in identity:
            identity.update(
                _claims(data.get("verifier") or {}, {"task_name": "task_id"})
            )
        identity.update(
            _claims(data.get("verifier") or {}, {"task_dir": "task_package"})
        )
        return MeasuredReceipt(reward, path, "regrade_receipt", identity)
    if isinstance(data.get("matrix"), dict) and isinstance(data.get("results"), list):
        results = data["results"]
        if len(results) != 1 or not isinstance(results[0], dict):
            return None
        result = results[0]
        rewards = result.get("rewards")
        if (
            result.get("status") not in {"ok", "mismatch"}
            or not isinstance(rewards, list)
            or len(rewards) != 1
        ):
            return None
        reward = finite_reward(rewards[0])
        if reward is None:
            return None
        identity = _claims(
            data["matrix"],
            {
                "task_id": "task_id",
                "task": "task_package",
                "task_package_digest": "task_digest",
            },
        )
        identity.update(_claims(result, {"agent": "role"}))
        return MeasuredReceipt(reward, path, "matrix_receipt", identity)
    return None


def _measured_reward(path: Path, *, _depth: int = 0) -> MeasuredReceipt | None:
    """Load a measured scalar reward from a referenced receipt path.

    Handles Harbor trial dirs (``result.json`` / ``verifier/reward.{json,txt}``),
    ``run_candidate`` output dirs (``candidate.json`` bound to its native
    trial), native output dirs (``trial/`` children), Harbor job dirs (one
    level of trial children, all agreeing), and single JSON receipt files.
    """
    if _depth > 4 or path.is_symlink():
        return None
    if path.is_file():
        return _receipt_file_reward(path)
    if not path.is_dir():
        return None
    trial = _trial_dir_reward(path)
    if trial is not None:
        return trial
    candidate = path / "candidate.json"
    if candidate.is_file() and not candidate.is_symlink():
        reward = _candidate_file_reward(candidate)
        if reward is None:
            return None
        native = path / "native"
        if native.is_dir():
            inner = _measured_reward(native, _depth=_depth + 1)
            if inner is None or inner.value != reward:
                return None
        identity = _candidate_file_identity(_read_json(candidate) or {})
        return MeasuredReceipt(reward, candidate, "candidate", identity)
    for name in ("trial", "native"):
        child = path / name
        if child.is_dir() and not child.is_symlink():
            found = _measured_reward(child, _depth=_depth + 1)
            if found is not None:
                return found
    children: list[MeasuredReceipt] = []
    for child in sorted(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            found = _trial_dir_reward(child)
            if found is not None:
                children.append(found)
    if children and len({receipt.value for receipt in children}) == 1:
        first = children[0]
        merged = {
            key: value
            for key, value in first.identity.items()
            if all(receipt.identity.get(key) == value for receipt in children)
        }
        return MeasuredReceipt(first.value, first.source, "harbor_job", merged)
    return None


def _files_mapping(directory: Path) -> dict[str, str | bytes] | None:
    """Read a solution bundle directory into the candidate-digest file mapping.

    Files read as text reproduce the producer's ``str`` submission mapping;
    undecodable members fall back to bytes exactly like the producer contract.
    """
    if not directory.is_dir() or directory.is_symlink():
        return None
    files: dict[str, str | bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            return None
        try:
            files[path.relative_to(directory).as_posix()] = path.read_text(
                encoding="utf-8"
            )
        except UnicodeError:
            try:
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
            except OSError:
                return None
        except OSError:
            return None
    return files or None


class VerifiedAssetExporter:
    """Re-verify ``asset.verified`` journal events and export checked rows.

    ``queue`` is the shared ``ContinuousWorkQueue``; its registry resolves
    corpus identities and applies the scoped canonical TEST lock. Relative
    receipt paths resolve against ``repo_root``, never the process cwd — pass
    the repository root the producing workers used.
    """

    def __init__(
        self, queue: ExporterQueue, *, repo_root: str | Path | None = None
    ) -> None:
        self.queue = queue
        self.repo_root = (
            Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
        )

    # -- shared helpers ------------------------------------------------------

    def _path(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.repo_root / path
        return path.resolve()

    def _registry(self) -> CorpusRegistry | None:
        return getattr(self.queue, "registry", None)

    def _identity(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """Resolve the asset's corpus identity; TEST and unknown fail closed."""
        problems: list[str] = []
        dataset_id = _nonempty_str(payload.get("dataset_id"))
        item_id = _nonempty_str(payload.get("item_id"))
        if dataset_id is None or item_id is None:
            return None, ["asset payload is missing dataset_id/item_id"]
        registry = self._registry()
        if registry is None:
            return None, ["queue has no corpus registry to resolve identities"]
        try:
            item = registry.get(dataset_id, item_id)
        except (KeyError, ValueError) as exc:
            return None, [f"unregistered corpus identity {dataset_id}/{item_id}: {exc}"]
        if item.split != "dev":
            problems.append(f"corpus identity {dataset_id}/{item_id} is sealed as {item.split}")
        # Only task_id is a task identity here; the seeker payload also carries
        # a "task" package path, which is evidence, not an identity claim.
        claimed_task = payload.get("task_id")
        if claimed_task is not None:
            if not isinstance(claimed_task, str) or item.task_id is None:
                problems.append("claimed task_id cannot be bound to the registry item")
            elif claimed_task != item.task_id:
                problems.append(
                    f"claimed task_id {claimed_task} conflicts with registered {item.task_id}"
                )
        # The corpus digest binds the registered task identity. A repaired
        # package legitimately digests differently, so only task_digest claims
        # are compared against the registry item.
        claimed_digest = payload.get("task_digest")
        if claimed_digest is not None and claimed_digest != item.task_digest:
            problems.append("claimed task digest conflicts with the registry item")
        identity = {
            "dataset_id": dataset_id,
            "item_id": item_id,
            "task_id": item.task_id,
            "task_digest": item.task_digest,
            "split": item.split,
        }
        return (identity if not problems else None), problems

    def _linked_event(
        self, event_id: Any, *, expected_type: str | None = None, run_id: str | None = None
    ) -> tuple[dict[str, Any] | None, list[str]]:
        event_id = _nonempty_str(event_id)
        if event_id is None:
            return None, ["linked event id is missing or blank"]
        linked = self.queue.get_event(event_id)
        if linked is None:
            return None, [f"linked event does not exist in the journal: {event_id}"]
        problems = []
        if expected_type is not None and linked.get("event_type") != expected_type:
            problems.append(
                f"linked event {event_id} is {linked.get('event_type')}, not {expected_type}"
            )
        if run_id is not None and linked.get("run_id") != run_id:
            problems.append(f"linked event {event_id} belongs to another run_id")
        return (linked if not problems else None), problems

    @staticmethod
    def _identity_consistent(
        left: Mapping[str, Any], right: Mapping[str, Any], *, context: str
    ) -> list[str]:
        problems = []
        for key in ("dataset_id", "item_id"):
            if left.get(key) is not None and right.get(key) is not None and left[key] != right[key]:
                problems.append(f"{context}: conflicting {key} between linked payloads")
        return problems

    # -- reward receipts -------------------------------------------------------

    def _receipt(self, value: Any) -> MeasuredReceipt | None:
        path = self._path(value)
        if path is None:
            return None
        return _measured_reward(path)

    def _bind_declared(
        self, declared: Mapping[str, Any], context: Mapping[str, Any], *, tag: str
    ) -> list[str]:
        """Declared receipt/execution metadata must not contradict the record.

        A field only binds when both sides provide it; absent metadata stays an
        absence rather than an invented producer claim.
        """
        problems: list[str] = []
        task_id = declared.get("task_id")
        expected_task = context.get("task_id")
        if (
            isinstance(task_id, str)
            and isinstance(expected_task, str)
            and CorpusRegistry.task_name(task_id)
            != CorpusRegistry.task_name(expected_task)
        ):
            problems.append(f"{tag}: declared task_id {task_id} contradicts {expected_task}")
        for key in ("task_digest", "dataset_id", "item_id"):
            value, expected = declared.get(key), context.get(key)
            if isinstance(value, str) and isinstance(expected, str) and value != expected:
                problems.append(f"{tag}: declared {key} {value} contradicts {expected}")
        package = declared.get("task_package")
        expected_package = context.get("task_package")
        if (
            isinstance(package, str)
            and isinstance(expected_package, str)
            and self._path(package) != self._path(expected_package)
        ):
            problems.append(f"{tag}: declared package {package} is not the bound package")
        role = declared.get("role")
        control = context.get("control")
        if isinstance(role, str) and isinstance(control, str):
            allowed = _ROLE_GROUPS.get(_normalized(role))
            if allowed is not None and control not in allowed:
                problems.append(
                    f"{tag}: declared role {role} cannot stand in for {control}"
                )
        return problems

    @staticmethod
    def _execution_claims(record: Mapping[str, Any]) -> dict[str, Any]:
        """Identity fields a control record's execution payload declares."""
        execution = record.get("execution")
        if not isinstance(execution, Mapping):
            return {}
        declared = _claims(
            execution,
            {
                "task_id": "task_id",
                "task_digest": "task_digest",
                "task_package_digest": "task_digest",
                "dataset_id": "dataset_id",
                "item_id": "item_id",
                "control": "role",
                "mode": "role",
                "role": "role",
                "agent": "role",
                "stage": "role",
            },
        )
        task = execution.get("task")
        if isinstance(task, Mapping):
            for key, claim in (
                ("id", "task_id"),
                ("digest", "task_digest"),
                ("path", "task_package"),
            ):
                if isinstance(task.get(key), str):
                    declared.setdefault(claim, task[key])
        return declared

    def _check_control_records(
        self,
        records: Any,
        expected: float,
        *,
        label: str,
        context: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Three measured control records, each reproduced from its receipt."""
        problems: list[str] = []
        receipts: list[dict[str, Any]] = []
        context = {**context, "control": label}
        if not isinstance(records, list) or len(records) != 3:
            return [], [f"{label}: expected exactly 3 control records"]
        for index, record in enumerate(records):
            tag = f"{label}[{index}]"
            if not isinstance(record, Mapping):
                problems.append(f"{tag}: control record is not an object")
                continue
            if record.get("measured") is not True:
                problems.append(f"{tag}: record does not claim a fresh measurement")
            claimed = finite_reward(record.get("reward"))
            if claimed is None or claimed != expected:
                problems.append(
                    f"{tag}: claimed reward {record.get('reward')!r} != {expected}"
                )
            problems.extend(
                self._bind_declared(self._execution_claims(record), context, tag=tag)
            )
            receipt = self._receipt(record.get("evidence_ref"))
            if receipt is None:
                problems.append(f"{tag}: evidence_ref did not load a measured receipt")
                continue
            problems.extend(self._bind_declared(receipt.identity, context, tag=tag))
            if receipt.value != expected:
                problems.append(
                    f"{tag}: receipt reward {receipt.value} != expected {expected}"
                )
            if claimed is not None and receipt.value != claimed:
                problems.append(
                    f"{tag}: receipt reward {receipt.value} contradicts claimed {claimed}"
                )
            receipts.append({"evidence_ref": record["evidence_ref"], **receipt.provenance()})
        return receipts, problems

    def _verify_control_evidence(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """The 3x reference / 3x nop / 3x exploit control matrix, fully checked."""
        problems: list[str] = []
        for key, expected in (("ref_passes", 3), ("nop", 0), ("exploit", 0)):
            if not _exact_int(payload.get(key), expected):
                problems.append(f"{key} must be exactly {expected}")
        for key in ("reference_trials", "nop_trials", "exploit_trials"):
            if not _exact_int(payload.get(key), 3):
                problems.append(f"{key} must be exactly 3")
        rewards = payload.get("control_rewards")
        if not isinstance(rewards, Mapping) or set(rewards) != {
            "reference",
            "nop",
            "exploit",
        }:
            problems.append("control_rewards must cover reference/nop/exploit")
        else:
            for name, expected in (("reference", 1.0), ("nop", 0.0), ("exploit", 0.0)):
                group = rewards[name]
                if not isinstance(group, list) or len(group) != 3 or any(
                    finite_reward(value) != expected for value in group
                ):
                    problems.append(f"control_rewards.{name} must be three {expected} rewards")
        evidence = payload.get("control_evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "reference",
            "nop",
            "exploit",
        }:
            problems.append("control_evidence must cover reference/nop/exploit")
            return None, problems
        context = {
            "task_id": payload.get("task_id"),
            "task_digest": payload.get("task_package_digest")
            or payload.get("repaired_task_digest"),
            "task_package": payload.get("task_package") or payload.get("repaired_task"),
            "dataset_id": payload.get("dataset_id"),
            "item_id": payload.get("item_id"),
        }
        receipts: dict[str, Any] = {}
        resolved: set[Path] = set()
        for name, expected in (("reference", 1.0), ("nop", 0.0), ("exploit", 0.0)):
            group_receipts, group_problems = self._check_control_records(
                evidence[name], expected, label=name, context=context
            )
            problems.extend(group_problems)
            for entry in group_receipts:
                resolved.add(Path(entry["ref"]))
            receipts[name] = group_receipts
        measured_refs = sum(len(group) for group in receipts.values())
        if measured_refs == 9 and len(resolved) != 9:
            problems.append("control evidence references are not nine distinct receipts")
        return (
            ({"receipts": receipts} if not problems else None),
            problems,
        )

    def _check_qualification(
        self, event: Mapping[str, Any], *, run_id: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Re-verify one ``task_quality.repair_qualified`` event end to end."""
        problems: list[str] = []
        if event.get("event_type") != "task_quality.repair_qualified":
            problems.append("qualification event is not task_quality.repair_qualified")
        if event.get("status") != "confirmed":
            problems.append("qualification event is not confirmed")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None, [*problems, "qualification payload is not an object"]
        if payload.get("qualified") is not True or payload.get("measured") is not True:
            problems.append("qualification does not claim a measured qualification")
        job, job_problems = self._linked_event(
            payload.get("job_id"), expected_type="task_quality.repair_task", run_id=run_id
        )
        problems.extend(job_problems)
        if job is not None:
            problems.extend(
                self._identity_consistent(
                    payload, job.get("payload") or {}, context="repair job"
                )
            )
        digest = payload.get("task_package_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            problems.append("task_package_digest is not a full sha256 digest")
        else:
            package = self._path(payload.get("task_package"))
            if package is None or not package.is_dir():
                problems.append("repaired task package is missing")
            else:
                try:
                    actual = task_directory_digest(package)
                except (OSError, ValueError) as exc:
                    problems.append(f"repaired task package is undigestable: {exc}")
                else:
                    if actual != digest:
                        problems.append(
                            "repaired task package no longer matches task_package_digest"
                        )
        if payload.get("repaired_task_digest") not in (None, digest):
            problems.append("repaired_task_digest conflicts with task_package_digest")
        evidence, evidence_problems = self._verify_control_evidence(payload)
        problems.extend(evidence_problems)
        qualification = {
            "qualification_event_id": event.get("event_id"),
            "task_package": payload.get("task_package"),
            "task_package_digest": digest,
            "job_id": payload.get("job_id"),
            "job_dataset_id": (job or {}).get("payload", {}).get("dataset_id"),
            "control_rewards": payload.get("control_rewards"),
            **(evidence or {}),
        }
        return (qualification if not problems else None), problems

    # -- per-kind verification ---------------------------------------------------

    def _verify_repaired_task(
        self, event: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        problems: list[str] = []
        digest = payload.get("task_package_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            problems.append("task_package_digest is not a full sha256 digest")
            digest = None
        asset_id = payload.get("asset_id")
        if digest is not None and asset_id != f"repaired-task:{digest}":
            problems.append("asset_id does not match the repaired-task digest identity")
        package = self._path(payload.get("task_package"))
        qualification_event, link_problems = self._linked_event(
            payload.get("qualification_event_id"), run_id=str(event.get("run_id"))
        )
        problems.extend(link_problems)
        qualification = None
        if qualification_event is not None:
            qualification, qual_problems = self._check_qualification(
                qualification_event, run_id=str(event.get("run_id"))
            )
            problems.extend(qual_problems)
            q_payload = qualification_event["payload"]
            problems.extend(
                self._identity_consistent(payload, q_payload, context="qualification")
            )
            if digest is not None and q_payload.get("task_package_digest") != digest:
                problems.append("qualification names a different task_package_digest")
            if (
                _nonempty_str(q_payload.get("task_package")) is not None
                and q_payload.get("task_package") != payload.get("task_package")
            ):
                problems.append("qualification names a different repaired task package")
            for key in ("control_rewards", "control_evidence"):
                if payload.get(key) is None:
                    problems.append(f"asset payload is missing {key}")
                elif payload[key] != q_payload.get(key):
                    problems.append(f"asset {key} contradicts the qualification event")
        if package is None or not package.is_dir():
            problems.append("repaired task package is missing")
        elif digest is not None:
            try:
                if task_directory_digest(package) != digest:
                    problems.append("task_package no longer matches task_package_digest")
            except (OSError, ValueError) as exc:
                problems.append(f"task_package is undigestable: {exc}")
        row = {
            "task_package": str(package) if package is not None else payload.get("task_package"),
            "task_package_digest": digest,
            "qualification": qualification,
        }
        return (row if not problems else None), problems

    def _verify_exploit_witness(
        self, event: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        problems: list[str] = []
        if payload.get("source_event_type") != "seeker.exploit_confirmed":
            problems.append("source_event_type is not seeker.exploit_confirmed")
        source, link_problems = self._linked_event(
            payload.get("source_event_id"),
            expected_type="seeker.exploit_confirmed",
            run_id=str(event.get("run_id")),
        )
        problems.extend(link_problems)
        source_payload: Mapping[str, Any] = {}
        if source is not None:
            if source.get("status") != "confirmed":
                problems.append("source exploit event is not confirmed")
            source_payload = source.get("payload") or {}
            for key, expected in (
                ("original_reward", 1.0),
                ("upstream_reward", 1.0),
                ("repaired_reward", 0.0),
            ):
                if finite_reward(source_payload.get(key)) != expected:
                    problems.append(f"source {key} is not a measured {expected}")
            problems.extend(
                self._identity_consistent(payload, source_payload, context="source")
            )
            for key in (
                "task_id",
                "candidate_digest",
                "task_package",
                "repaired_task",
                "exploit_run",
                "repaired_run",
            ):
                if (
                    payload.get(key) is not None
                    and source_payload.get(key) is not None
                    and payload[key] != source_payload[key]
                ):
                    problems.append(f"asset {key} contradicts the confirmed exploit event")
        candidate_digest = payload.get("candidate_digest")
        if not isinstance(candidate_digest, str) or not _DIGEST_RE.fullmatch(candidate_digest):
            problems.append("candidate_digest is not a full sha256 digest")
            candidate_digest = None
        if candidate_digest is not None and isinstance(payload.get("task_id"), str):
            expected_id = f"witness:{payload['task_id']}:{candidate_digest.removeprefix('sha256:')[:12]}"
            if payload.get("asset_id") != expected_id:
                problems.append("asset_id does not match the witness task/digest identity")
        if _nonempty_str(payload.get("task_id")) is None:
            problems.append("exploit_witness requires a task_id to bind its identity")
        if candidate_digest is not None:
            submission = payload.get("exploit_files")
            if not isinstance(submission, Mapping) or not submission:
                problems.append("exploit_files are missing; the digest cannot recompute")
            else:
                try:
                    recomputed = compute_candidate_digest(submission)
                except (TypeError, ValueError) as exc:
                    problems.append(f"exploit_files cannot recompute a digest: {exc}")
                else:
                    if recomputed != candidate_digest:
                        problems.append("exploit_files do not reproduce candidate_digest")
            bundle = payload.get("solution_bundle")
            if bundle is not None:
                bundle_path = self._path(bundle)
                files = _files_mapping(bundle_path) if bundle_path is not None else None
                if files is None or compute_candidate_digest(files) != candidate_digest:
                    problems.append("solution_bundle does not reproduce candidate_digest")
        for key, expected in (("exploit_run", 1.0), ("repaired_run", 0.0)):
            receipt = self._receipt(payload.get(key))
            if receipt is None:
                problems.append(f"{key} did not load a measured receipt")
                continue
            problems.extend(
                self._bind_declared(
                    receipt.identity,
                    {
                        "task_id": payload.get("task_id"),
                        "task_package": payload.get(
                            "task_package" if key == "exploit_run" else "repaired_task"
                        ),
                        "dataset_id": payload.get("dataset_id"),
                        "item_id": payload.get("item_id"),
                    },
                    tag=key,
                )
            )
            if receipt.value != expected:
                problems.append(f"{key} receipt reward {receipt.value} != {expected}")
        exploit_ref = self._path(payload.get("exploit_run"))
        repaired_ref = self._path(payload.get("repaired_run"))
        if (
            exploit_ref is not None
            and repaired_ref is not None
            and exploit_ref == repaired_ref
        ):
            problems.append("exploit and repaired runs must be distinct receipts")
        row = {
            "source_event_id": payload.get("source_event_id"),
            "candidate_digest": candidate_digest,
            "shortcut_signature": _nonempty_str(payload.get("shortcut_signature")),
            "mechanism_signature": _nonempty_str(payload.get("shortcut_signature"))
            or _nonempty_str(payload.get("mechanism")),
            "original_reward": 1.0,
            "repaired_reward": 0.0,
            "exploit_run": payload.get("exploit_run"),
            "repaired_run": payload.get("repaired_run"),
        }
        return (row if not problems else None), problems

    def _load_label_receipt(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """Load the verified ``evallab.trace_label/v1`` receipt.

        A ``monitor.suspicion`` semantic-triage card is raw model suspicion —
        useful input to a verifier, never a verified label — so it fails here
        even with completed triage and surviving cite-or-abstain. No current
        production worker emits the verified receipt; the explicit contract is
        checked regardless.
        """
        receipt = payload.get("label_receipt", payload.get("receipt"))
        problems: list[str] = []
        if isinstance(receipt, str):
            path = self._path(receipt)
            data = _read_json(path) if path is not None and path.is_file() else None
            if not isinstance(data, dict):
                return None, ["label_receipt path did not load a JSON object"]
            receipt = data
        if not isinstance(receipt, Mapping):
            return None, ["labeled_trace needs a label_receipt object or JSON path"]
        if any(
            key in receipt for key in ("judge_label", "semantic_triage", "cite_or_abstain")
        ):
            problems.append("a monitor suspicion card is not a verified trace-label receipt")
        if receipt.get("schema") != TRACE_LABEL_SCHEMA:
            problems.append("label receipt is not an evallab.trace_label/v1 receipt")
        if receipt.get("status") not in VERIFIED_LABEL_STATUSES:
            problems.append("label receipt is not verified")
        label = receipt.get("label")
        if label not in TRACE_LABELS:
            problems.append(f"label receipt label {label!r} is not a verified trace label")
        for key in ("dataset_id", "item_id"):
            claimed = receipt.get(key)
            if _nonempty_str(claimed) is None:
                problems.append(f"label receipt does not bind the asset {key}")
            elif claimed != payload.get(key):
                problems.append(f"label receipt {key} contradicts the asset identity")
        claimed_task = receipt.get("task_id")
        if claimed_task is not None and (
            not isinstance(claimed_task, str)
            or _nonempty_str(payload.get("task_id")) is None
            or CorpusRegistry.task_name(claimed_task)
            != CorpusRegistry.task_name(str(payload["task_id"]))
        ):
            problems.append("label receipt task_id contradicts the asset identity")
        trace = _nonempty_str(receipt.get("trace"))
        trace_sha256 = _nonempty_str(receipt.get("trace_sha256"))
        if trace is None and trace_sha256 is None:
            problems.append("label receipt does not bind the labeled trace")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            problems.append("label receipt carries no step citations")
            return None, problems
        citations: list[dict[str, Any]] = []
        for index, cite in enumerate(evidence):
            if (
                not isinstance(cite, Mapping)
                or type(cite.get("step")) is not int
                or not _nonempty_str(cite.get("quote"))
            ):
                problems.append(f"citation[{index}] needs an integer step and nonempty quote")
                continue
            citations.append({"step": cite["step"], "quote": cite["quote"]})
        bound = {
            "label": label,
            "citations": citations,
            "trace": trace,
            "trace_sha256": trace_sha256,
        }
        return (bound if not problems else None), problems

    def _load_trace_steps(
        self, payload: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]] | None, Path | None, list[str]]:
        for key in ("trace", "trajectory", "trace_path"):
            value = payload.get(key)
            if value is None:
                continue
            path = self._path(value)
            if path is None:
                return None, None, [f"{key} is not a usable path"]
            try:
                steps, _ = trace_cheap.load_steps(path)
            except (OSError, ValueError) as exc:
                return None, None, [f"trace did not load trajectory steps: {exc}"]
            return steps, path, []
        steps = payload.get("trace_steps", payload.get("steps"))
        if isinstance(steps, list) and steps and all(isinstance(step, dict) for step in steps):
            return list(steps), None, []
        return None, None, ["labeled_trace needs a trace path or embedded trace steps"]

    def _trace_bound(
        self,
        receipt: Mapping[str, Any],
        trace_path: Path | None,
        steps: list[dict[str, Any]] | None,
    ) -> bool:
        """The receipt's declared trace must resolve to the labeled trace."""
        declared = receipt.get("trace")
        if declared is not None:
            if trace_path is None:
                return False
            bound = self._path(declared)
            return bound is not None and bound == trace_path
        digest = receipt.get("trace_sha256")
        return (
            isinstance(digest, str)
            and steps is not None
            and digest == _sha256_text(_canon(steps))
        )

    def _verify_labeled_trace(
        self, event: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        problems: list[str] = []
        steps, trace_path, trace_problems = self._load_trace_steps(payload)
        problems.extend(trace_problems)
        receipt, receipt_problems = self._load_label_receipt(payload)
        problems.extend(receipt_problems)
        if receipt is not None and not self._trace_bound(receipt, trace_path, steps):
            problems.append("label receipt does not bind the labeled trace")
        if payload.get("source_event_id") is not None:
            source, link_problems = self._linked_event(
                payload.get("source_event_id"), run_id=str(event.get("run_id"))
            )
            problems.extend(link_problems)
            if source is not None and source.get("status") != "confirmed":
                problems.append("linked trace-label source event is not confirmed")
            if source is not None:
                problems.extend(
                    self._identity_consistent(
                        payload, source.get("payload") or {}, context="source"
                    )
                )
        if receipt is not None and steps is not None:
            action_text = {
                action["step"]: action["text"] for action in trace_cheap.actions_only(steps)
            }
            for cite in receipt["citations"]:
                text = action_text.get(cite["step"])
                if text is None:
                    problems.append(f"cited step {cite['step']} is not a trace action step")
                elif _normalized(cite["quote"]) not in _normalized(text):
                    problems.append(
                        f"cite quote on step {cite['step']} is not verbatim in the trace"
                    )
        row = {
            "label": receipt["label"] if receipt is not None else None,
            "citations": receipt["citations"] if receipt is not None else None,
            "mechanism_signature": _nonempty_str(payload.get("mechanism")),
            "source_event_id": payload.get("source_event_id"),
        }
        return (row if not problems else None), problems

    # -- event entry point -------------------------------------------------------

    def verify_asset_event(
        self, event: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Verify one ``asset.verified`` envelope; ``(row, problems)``."""
        problems: list[str] = []
        if event.get("event_type") != "asset.verified":
            problems.append("event is not asset.verified")
        if event.get("status") != "confirmed":
            problems.append("asset event is not confirmed")
        if event.get("system") not in WORKER_SYSTEMS:
            problems.append("asset event does not come from a worker system")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None, [*problems, "asset payload is not an object"]
        asset_id = _nonempty_str(payload.get("asset_id"))
        if asset_id is None:
            problems.append("asset_id is missing")
        kind = payload.get("kind")
        if kind not in ASSET_KINDS:
            problems.append(f"unknown asset kind: {kind!r}")
            return None, problems
        if payload.get("status") not in (None, "confirmed"):
            problems.append("asset payload status is not confirmed")
        identity, identity_problems = self._identity(payload)
        problems.extend(identity_problems)
        verifier = {
            "repaired_task": self._verify_repaired_task,
            "exploit_witness": self._verify_exploit_witness,
            "labeled_trace": self._verify_labeled_trace,
        }[kind]
        evidence, kind_problems = verifier(event, payload)
        problems.extend(kind_problems)
        if problems:
            return None, problems
        assert identity is not None and evidence is not None and asset_id is not None
        body = {
            "asset_id": asset_id,
            "kind": kind,
            "event_id": event.get("event_id"),
            "seq": event.get("seq"),
            "run_id": event.get("run_id"),
            "system": event.get("system"),
            **{key: identity[key] for key in ("dataset_id", "item_id", "task_id", "split")},
            "evidence": evidence,
        }
        row = {
            "schema": ASSET_ROW_SCHEMA,
            "row_id": _sha256_text(_canon(body))[:32],
            **body,
            "asset_sha256": _sha256_text(_canon(body)),
        }
        return row, []

    # -- journal scanning and asset export ---------------------------------------

    def _scan(
        self,
        *,
        after_seq: int,
        after_event_id: str | None,
        run_id: str | None,
        consumer_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if consumer_id is not None and (
            after_seq != 0 or after_event_id is not None or run_id is not None
        ):
            # The durable consumer cursor is itself the replay position;
            # combining it with an explicit cursor or a run filter would
            # silently skip cursor data, so the ambiguity is refused.
            raise AssetExportError(
                "consumer_id cannot be combined with run_id, after_seq, or after_event_id"
            )
        events: list[dict[str, Any]] = []
        if consumer_id is not None:
            # The durable cursor is acknowledged only after the bundle is
            # durably written; page through read_events so an interrupted
            # export replays the whole window instead of losing it mid-scan.
            batch = self.queue.read_consumer(consumer_id, limit=limit)
            events.extend(batch)
            while batch:
                batch = self.queue.read_events(
                    after_seq=int(batch[-1]["seq"]), limit=limit
                )
                events.extend(batch)
            return events
        cursor = after_seq
        first = True
        while True:
            try:
                batch = self.queue.read_events(
                    after_seq=cursor,
                    after_event_id=after_event_id if first else None,
                    run_id=run_id,
                    limit=limit,
                )
            except KeyError as exc:
                raise AssetExportError(
                    f"unknown replay cursor event_id: {after_event_id}"
                ) from exc
            first = False
            if not batch:
                break
            events.extend(batch)
            cursor = max(cursor, int(batch[-1]["seq"]))
        return events

    def export_verified_assets(
        self,
        out_dir: str | Path,
        *,
        after_seq: int = 0,
        after_event_id: str | None = None,
        run_id: str | None = None,
        consumer_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Verify every ``asset.verified`` in the scanned journal and export rows.

        Re-scanning produces a deterministic, deduplicated projection: the first
        verified event per ``asset_id`` wins and forged or incomplete events are
        reported as rejections, never exported. With ``consumer_id`` the durable
        consumer cursor acknowledges only after the complete bundle is written;
        a failed export replays the same window next time.
        """
        if type(limit) is not int or limit < 1:
            raise AssetExportError("limit must be a positive integer")
        events = self._scan(
            after_seq=after_seq,
            after_event_id=after_event_id,
            run_id=run_id,
            consumer_id=consumer_id,
            limit=limit,
        )
        rows: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        claimed: dict[str, dict[str, Any]] = {}
        duplicates = 0
        last_seq = after_seq
        for event in events:
            last_seq = max(last_seq, int(event.get("seq") or 0))
            if event.get("event_type") != "asset.verified":
                continue
            payload = event.get("payload") or {}
            asset_id = str(payload.get("asset_id") or "") if isinstance(payload, Mapping) else ""
            row, problems = self.verify_asset_event(event)
            if row is None:
                rejections.append(
                    {
                        "event_id": event.get("event_id"),
                        "seq": event.get("seq"),
                        "asset_id": asset_id or None,
                        "kind": payload.get("kind") if isinstance(payload, Mapping) else None,
                        "reasons": problems,
                    }
                )
                continue
            if asset_id in claimed:
                duplicates += 1
                continue
            claimed[asset_id] = row
            rows.append(row)
        rows.sort(key=lambda row: row["asset_id"])
        by_kind: dict[str, int] = {}
        for row in rows:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        directory = self._prepare_output(
            out_dir, names=(_ASSET_ROWS_NAME, _ASSET_MANIFEST_NAME)
        )
        rows_text = "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        )
        manifest = {
            "schema": ASSET_MANIFEST_SCHEMA,
            "consumer_id": consumer_id,
            "run_id": run_id,
            "after_seq": after_seq,
            "last_seq": last_seq,
            "events_scanned": len(events),
            "asset_events": len(rows) + duplicates + len(rejections),
            "exported": len(rows),
            "deduplicated": duplicates,
            "rejected": len(rejections),
            "by_kind": dict(sorted(by_kind.items())),
            "rejections": rejections,
            "rows_jsonl_sha256": _sha256_text(rows_text),
        }
        self._write_bundle(
            directory,
            {
                _ASSET_ROWS_NAME: rows_text,
                _ASSET_MANIFEST_NAME: json.dumps(
                    manifest, indent=2, sort_keys=True, allow_nan=False
                )
                + "\n",
            },
        )
        if consumer_id is not None and events:
            self.queue.ack_consumer(consumer_id, last_seq)
        return manifest

    # -- Model Lab training gate --------------------------------------------------

    def _asset_index(self) -> dict[str, dict[str, Any]]:
        """All ``asset.verified`` events keyed by payload asset_id (journal order)."""
        if getattr(self, "_cached_asset_index", None) is None:
            index: dict[str, dict[str, Any]] = {}
            cursor = 0
            while True:
                batch = self.queue.read_events(after_seq=cursor, limit=1000)
                if not batch:
                    break
                for event in batch:
                    cursor = max(cursor, int(event["seq"]))
                    if event.get("event_type") != "asset.verified":
                        continue
                    payload = event.get("payload")
                    if isinstance(payload, Mapping) and _nonempty_str(
                        payload.get("asset_id")
                    ):
                        index.setdefault(str(payload["asset_id"]), event)
                if len(batch) < 1000:
                    break
            self._cached_asset_index = index
        return self._cached_asset_index

    def _nested_identity_problems(
        self, value: Any, *, dataset_id: str, item: CorpusItem, depth: int = 0
    ) -> list[str]:
        """Conflicting or sealed identity claims nested below the top level.

        Only the recognized identity/provenance keys are inspected — a
        ``(dataset_id, item_id)`` pair resolves through the registry, bare item
        ids and task names hit the registry seals, and task digests compare
        against the sealed digest set. Arbitrary prose is never scanned, so
        free text that merely mentions a name cannot false-positive.

        Nesting deeper than ``_NESTED_SCAN_DEPTH`` is itself a rejection:
        claims past the scan horizon cannot be resolved against the registry,
        so the scan fails closed rather than passing unverified structure.
        """
        problems: list[str] = []
        registry = self._registry()
        if registry is None:
            return problems
        if depth > _NESTED_SCAN_DEPTH:
            return [f"nested structure exceeds maximum scan depth {_NESTED_SCAN_DEPTH}"]
        if isinstance(value, Mapping):
            nested_dataset = value.get("dataset_id")
            nested_item = value.get("item_id")
            if (
                isinstance(nested_dataset, str)
                and nested_dataset.strip()
                and nested_dataset != dataset_id
            ):
                problems.append(
                    f"nested dataset_id {nested_dataset} conflicts with {dataset_id}"
                )
            if isinstance(nested_item, str) and nested_item.strip():
                sealed = registry.sealed_identity(item_id=nested_item)
                if sealed is not None:
                    problems.append(f"nested item_id {nested_item} is a sealed {sealed}")
                elif nested_item != item.item_id:
                    problems.append(
                        f"nested item_id {nested_item} conflicts with {item.item_id}"
                    )
                if isinstance(nested_dataset, str) and nested_dataset.strip():
                    try:
                        resolved = registry.get(nested_dataset, nested_item)
                    except (KeyError, ValueError):
                        problems.append(
                            f"nested identity {nested_dataset}/{nested_item} "
                            "is unregistered"
                        )
                    else:
                        if resolved.split != "dev":
                            problems.append(
                                f"nested identity {nested_dataset}/{nested_item} "
                                f"is sealed as {resolved.split}"
                            )
            for key in _ITEM_SEAL_KEYS:
                claim = value.get(key)
                if (
                    isinstance(claim, str)
                    and claim.strip()
                    and registry.sealed_identity(item_id=claim) is not None
                ):
                    problems.append(f"nested {key} {claim} is a sealed item_id")
            canonical = item.task_id or item.item_id
            for key in _TASK_CLAIM_KEYS:
                claim = value.get(key)
                if not isinstance(claim, str) or not claim.strip():
                    continue
                if registry.sealed_identity(task_id=claim) is not None:
                    problems.append(f"nested {key} {claim} is a sealed task name")
                elif registry.task_name(claim) != registry.task_name(canonical):
                    problems.append(
                        f"nested {key} {claim} conflicts with task {canonical}"
                    )
            for key in _TASK_SEAL_KEYS:
                claim = value.get(key)
                if (
                    isinstance(claim, str)
                    and claim.strip()
                    and registry.sealed_identity(task_id=claim) is not None
                ):
                    problems.append(f"nested {key} {claim} is a sealed task name")
            for key in _DIGEST_CLAIM_KEYS:
                claim = value.get(key)
                if not isinstance(claim, str) or not claim.strip():
                    continue
                if registry.sealed_identity(task_digest=claim) is not None:
                    problems.append(f"nested {key} {claim} is a sealed task digest")
                elif item.task_digest is not None and claim != item.task_digest:
                    problems.append(
                        f"nested {key} {claim} conflicts with the registry item"
                    )
            for key in _DIGEST_SEAL_KEYS:
                claim = value.get(key)
                if (
                    isinstance(claim, str)
                    and claim.strip()
                    and registry.sealed_identity(task_digest=claim) is not None
                ):
                    problems.append(f"nested {key} {claim} is a sealed task digest")
            children: Iterable[Any] = value.values()
        elif isinstance(value, (list, tuple)):
            children = value
        else:
            return problems
        for child in children:
            problems.extend(
                self._nested_identity_problems(
                    child, dataset_id=dataset_id, item=item, depth=depth + 1
                )
            )
        return problems

    def _candidate_events(
        self, candidate: Mapping[str, Any], *, dataset_id: str, item: CorpusItem
    ) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
        """Resolve and re-verify every journal reference a candidate makes."""
        problems: list[str] = []
        resolved: dict[str, dict[str, Any]] = {}
        references: dict[str, dict[str, Any]] = {}
        asset_id = _nonempty_str(candidate.get("asset_id"))
        if asset_id is not None:
            event = self._asset_index().get(asset_id)
            if event is None:
                problems.append("asset_id does not resolve to an asset.verified event")
            else:
                references["asset_id"] = event
        for key in _EVENT_REFERENCE_KEYS:
            event_id = _nonempty_str(candidate.get(key))
            if event_id is None:
                continue
            event = self.queue.get_event(event_id)
            if event is None:
                problems.append(f"{key} does not resolve to a journal event")
                continue
            references[key] = event
        for key, event in references.items():
            resolved[key] = event
            linked_payload = event.get("payload")
            if isinstance(linked_payload, Mapping):
                for field in ("dataset_id", "item_id"):
                    claimed = candidate.get(field)
                    if (
                        claimed is not None
                        and linked_payload.get(field) is not None
                        and linked_payload[field] != claimed
                    ):
                        problems.append(
                            f"{key} names a different {field} than the candidate claims"
                        )
                problems.extend(
                    f"{key}: {problem}"
                    for problem in self._nested_identity_problems(
                        linked_payload, dataset_id=dataset_id, item=item
                    )
                )
            if event.get("event_type") == "asset.verified":
                _, asset_problems = self.verify_asset_event(event)
                problems.extend(f"{key}: {problem}" for problem in asset_problems)
            elif event.get("event_type") == "task_quality.repair_qualified":
                _, qual_problems = self._check_qualification(
                    event, run_id=str(event.get("run_id"))
                )
                problems.extend(f"{key}: {problem}" for problem in qual_problems)
        return (resolved if not problems else None), problems

    def _check_candidate(
        self,
        index: int,
        candidate: Any,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        problems: list[str] = []
        if not isinstance(candidate, Mapping):
            return None, [f"candidate[{index}] is not an object"]
        registry = self._registry()
        if registry is None:
            return None, ["queue has no corpus registry to resolve identities"]
        dataset_id = _nonempty_str(candidate.get("dataset_id"))
        item_id = _nonempty_str(candidate.get("item_id"))
        item = None
        if dataset_id is None or item_id is None:
            problems.append("candidate is missing dataset_id/item_id")
        else:
            try:
                item = registry.get(dataset_id, item_id)
            except (KeyError, ValueError) as exc:
                problems.append(
                    f"unregistered corpus identity {dataset_id}/{item_id}: {exc}"
                )
            else:
                if item.split != "dev":
                    problems.append(
                        f"{dataset_id}/{item_id} resolves to sealed split {item.split}"
                    )
        if item is not None:
            # An identity claim the registry cannot confirm is conflicting
            # lineage; claims are exact, never fuzzy alias matches.
            claimed_task = candidate.get("task_id")
            if claimed_task is not None and claimed_task != item.task_id:
                problems.append("claimed task_id conflicts with the registry item")
            claimed_digest = candidate.get("task_digest")
            if claimed_digest is not None and claimed_digest != item.task_digest:
                problems.append("claimed task_digest conflicts with the registry item")
        if item is not None and dataset_id is not None:
            problems.extend(
                self._nested_identity_problems(
                    candidate, dataset_id=dataset_id, item=item
                )
            )
        mechanism = _nonempty_str(candidate.get("mechanism_signature"))
        if mechanism is None:
            problems.append("mechanism_signature must be a nonempty string")
        row = candidate.get("row")
        if not isinstance(row, Mapping):
            problems.append("candidate row must be an object")
        else:
            try:
                _canon(row)
            except (TypeError, ValueError):
                problems.append("candidate row is not JSON-serializable")
        if problems:
            return None, problems
        assert item is not None and dataset_id is not None
        events, event_problems = self._candidate_events(
            candidate, dataset_id=dataset_id, item=item
        )
        problems.extend(event_problems)
        if problems:
            return None, problems
        assert item is not None and mechanism is not None
        assert isinstance(row, Mapping)
        canonical_task = item.task_id or item.item_id
        body = {
            "dataset_id": dataset_id,
            "item_id": item_id,
            "task_id": canonical_task,
            "mechanism_signature": mechanism,
            "row": dict(row),
            "provenance": {
                "candidate_index": index,
                "split": item.split,
                "registry_task_digest": item.task_digest,
                "events": {
                    key: {
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "seq": event["seq"],
                    }
                    for key, event in sorted((events or {}).items())
                },
            },
        }
        entry = {
            "schema": TRAINING_ROW_SCHEMA,
            "row_id": _sha256_text(_canon(body))[:32],
            **body,
        }
        return entry, []

    def export_training_bundle(
        self, candidates: Iterable[Mapping[str, Any]], out_dir: str | Path
    ) -> dict[str, Any]:
        """Re-verify candidates and export the deduplicated training bundle.

        Every candidate is re-checked against the registry and the journal at
        export time; a passed dict is never trusted. The bundle refuses TEST
        identities, unknown identities, missing mechanism signatures, and
        contradictory provenance — including recognized identity fields nested
        inside rows or linked event payloads. Dedup keys on the exact
        ``(dataset_id, canonical task name, mechanism_signature)`` tuple.
        """
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Iterable):
            raise AssetExportError("candidates must be an iterable of objects")
        candidates = list(candidates)
        entries: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            entry, problems = self._check_candidate(index, candidate)
            if entry is None:
                rejections.append(
                    {
                        "index": index,
                        "dataset_id": candidate.get("dataset_id")
                        if isinstance(candidate, Mapping)
                        else None,
                        "item_id": candidate.get("item_id")
                        if isinstance(candidate, Mapping)
                        else None,
                        "reasons": problems,
                    }
                )
                continue
            entries.append(entry)
        registry = self._registry()
        canonical = registry.task_name if registry is not None else CorpusRegistry.task_name
        winners: dict[tuple[str, str, str], tuple[str, int, dict[str, Any]]] = {}
        for entry in entries:
            key = (
                entry["dataset_id"],
                canonical(entry["task_id"]),
                entry["mechanism_signature"],
            )
            encoded = _canon(entry)
            rank = (encoded, entry["provenance"]["candidate_index"])
            if key not in winners or rank < (winners[key][0], winners[key][1]):
                winners[key] = (encoded, entry["provenance"]["candidate_index"], entry)
        rows = [winners[key][2] for key in sorted(winners)]
        directory = self._prepare_output(
            out_dir, names=(_TRAINING_ROWS_NAME, _TRAINING_MANIFEST_NAME)
        )
        rows_text = "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        )
        manifest = {
            "schema": TRAINING_MANIFEST_SCHEMA,
            "input": len(candidates),
            "accepted": len(entries),
            "deduplicated": len(entries) - len(rows),
            "exported": len(rows),
            "rejected": len(rejections),
            "distinct_tasks": len(
                {(row["dataset_id"], canonical(row["task_id"])) for row in rows}
            ),
            "distinct_mechanisms": len({row["mechanism_signature"] for row in rows}),
            "rejections": rejections,
            "rows_jsonl_sha256": _sha256_text(rows_text),
        }
        self._write_bundle(
            directory,
            {
                _TRAINING_ROWS_NAME: rows_text,
                _TRAINING_MANIFEST_NAME: json.dumps(
                    manifest, indent=2, sort_keys=True, allow_nan=False
                )
                + "\n",
            },
        )
        return manifest

    # -- output safety --------------------------------------------------------------

    @staticmethod
    def _prepare_output(out_dir: str | Path, *, names: Sequence[str]) -> Path:
        directory = Path(out_dir).expanduser()
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        for ancestor in (directory, *directory.parents):
            if ancestor.is_symlink():
                raise AssetExportError(
                    f"output path escapes through a symlink ancestor: {ancestor}"
                )
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise AssetExportError(f"output path is not a plain directory: {directory}")
            allowed = set(names)
            stray = sorted(
                child.name for child in directory.iterdir() if child.name not in allowed
            )
            if stray:
                raise AssetExportError(
                    f"refusing to overwrite unrelated files in {directory}: {stray}"
                )
        else:
            directory.mkdir(parents=True)
        return directory

    def _write_bundle(self, directory: Path, files: Mapping[str, str]) -> None:
        """Write a multi-file bundle consistently.

        Every member validates before the first byte moves: a pre-existing
        member may only be replaced by byte-identical content (an idempotent
        re-export), and anything else is refused. If a write still fails, the
        members this call created are removed so a mismatched half-bundle —
        fresh rows beside a stale manifest — never remains on disk.
        """
        targets: list[tuple[Path, bytes]] = []
        for name, text in files.items():
            path = directory / name
            data = text.encode("utf-8")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise AssetExportError(f"refusing to overwrite non-file output: {path}")
            if path.is_file() and path.read_bytes() != data:
                raise AssetExportError(
                    f"refusing to overwrite different existing file: {path}"
                )
            targets.append((path, data))
        created: list[Path] = []
        try:
            for path, data in targets:
                if path.is_file() and path.read_bytes() == data:
                    continue
                existed = path.exists()
                self._atomic_write(path, data)
                if not existed:
                    created.append(path)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def export_training_bundle(
    queue: ExporterQueue,
    candidates: Iterable[Mapping[str, Any]],
    out_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Module-level Model Lab Training Gate over a production queue."""
    return VerifiedAssetExporter(queue, repo_root=repo_root).export_training_bundle(
        candidates, out_dir
    )


__all__ = [
    "ASSET_KINDS",
    "ASSET_MANIFEST_SCHEMA",
    "ASSET_ROW_SCHEMA",
    "AssetExportError",
    "ExporterQueue",
    "MeasuredReceipt",
    "TRACE_LABEL_SCHEMA",
    "TRAINING_MANIFEST_SCHEMA",
    "TRAINING_ROW_SCHEMA",
    "VerifiedAssetExporter",
    "export_training_bundle",
]
