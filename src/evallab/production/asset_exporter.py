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
* ``labeled_trace`` (emitted by
  ``MisalignmentLiveWorker.verify_and_promote_trace_label``): the asset must
  link through ``source_event_id`` to a confirmed ``trace.label_verified``
  event and carry a verified ``evallab.trace_label/v1`` receipt — a
  confirmed/verified status, a non-``neither`` label, ``dataset_id``/
  ``item_id`` (and ``task_id`` when declared) bound to the asset identity, and
  a ``trace`` path or ``trace_sha256`` bound to the actual labeled trace —
  whose ``{step, quote}`` citations all resolve verbatim against that trace.
  The receipt must carry a ``verification`` block: the canonical normalized
  proof, byte-identical to the promotion event's receipt. Its underlying
  independent evidence — a deterministic verdict-rule match plus a
  measured execution receipt bound to the run/task with a declared execution
  status, or a ground-truth adjudication backed by a loaded
  ``evallab.trace_label_adjudication_record/v1`` source file — must still
  re-verify at export time, including the frozen whole-source digest, so a
  receipt changed after promotion is rejected even when the reward is
  unchanged. A ``monitor.suspicion`` semantic-triage card is raw model
  suspicion, not a verified receipt, and is never accepted on its own.

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
Every candidate must reference an ``asset.verified`` event that passes
``verify_asset_event`` or a ``task_quality.repair_qualified`` event that passes
``_check_qualification`` at export time, reloading the measured execution or
ground-truth adjudication receipts. Event-free rows and references that cannot
supply this backing fail closed. Any unconfirmed event or payload declaring
``provisional``, ``suspected``, or ``candidate`` status rejects the row even
alongside verified backing. Broken or contradictory links are also rejected.
Exports record the backing references in ``provenance.verified_by`` and produce
deterministic JSONL plus a manifest with digests and
input/accepted/deduplicated/exported counts with distinct task/mechanism counts;
deduplication keys on the exact
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
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import CorpusRegistry
from evallab.qa.env_export import RUNTIME_TOOLS
from evallab.qa.task import finite_reward

TRACE_LABELS = ("exploit-pass", "defect-fail")
#: Independent proofs a ``labeled_trace`` receipt must carry under
#: ``verification``. ``evallab.trace_label_verification/v1`` is a deterministic
#: verifier receipt (verdict-rule match plus a measured execution receipt);
#: ``evallab.trace_label_adjudication/v1`` is an explicit ground-truth
#: adjudication receipt naming a human adjudicator and referencing an
#: ``evallab.trace_label_adjudication_record/v1`` source record file.
LABEL_VERIFICATION_SCHEMA = "evallab.trace_label_verification/v1"
LABEL_ADJUDICATION_SCHEMA = "evallab.trace_label_adjudication/v1"
#: The bounded ground-truth adjudication source record an adjudication proof
#: must reference: a standalone receipt file declaring schema, human
#: adjudicator, label, and the run/task/trace binding. The proof alone is a
#: claim; this record is the evidence of the actual human judgment.
LABEL_ADJUDICATION_RECORD_SCHEMA = "evallab.trace_label_adjudication_record/v1"
_LABEL_PROOF_SCHEMAS = (LABEL_VERIFICATION_SCHEMA, LABEL_ADJUDICATION_SCHEMA)
#: The reward direction a measured execution must show to prove each label:
#: exploit-pass is an observed unearned pass, defect-fail an observed failure.
_LABEL_PROOF_REWARDS = {"exploit-pass": 1.0, "defect-fail": 0.0}
#: Semantic-suspicion markers: a judge card is never an independent proof.
_SUSPICION_KEYS = ("judge_label", "semantic_triage", "cite_or_abstain")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")



def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None



@dataclass(frozen=True)
class MeasuredReceipt:
    """A measured scalar reward loaded from a concrete receipt file.

    ``identity`` carries the identity metadata the receipt itself declares
    (task name/digest, package path, dataset/item ids, control role, declared
    execution status); the exporter rejects declarations that contradict the
    record citing the receipt. ``sources`` is every file the measurement was
    reproduced from — a Harbor trial is its ``result.json`` plus all agreeing
    ``verifier/reward.*`` members, so the committed digest covers the whole
    receipt, not one file.
    """

    value: float
    source: Path
    kind: str
    identity: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[Path, ...] = ()

    def content_sha256(self) -> str:
        """A digest over every source file the measurement was loaded from."""
        paths = self.sources or (self.source,)
        file_hashes = sorted(_sha256_bytes(path.read_bytes()) for path in paths)
        return _sha256_text(_canon(file_hashes))

    def provenance(self) -> dict[str, Any]:
        return {
            "ref": str(self.source),
            "receipt_kind": self.kind,
            "reward": self.value,
            "receipt_sha256": self.content_sha256(),
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
    """Read Harbor TrialResult or HarborBackend's native trial summary."""
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    rewards = data.get("verifier_result")
    rewards = rewards.get("rewards") if isinstance(rewards, dict) else None
    harbor_reward = (
        finite_reward(rewards.get("reward")) if isinstance(rewards, dict) else None
    )
    if {"at", "exception", "mode", "reward"} <= data.keys():
        if (
            _nonempty_str(data["at"]) is None
            or data["mode"] not in ("oracle", "nop", "shortcut")
            or data["exception"] is not None
        ):
            return None
        native_reward = finite_reward(data["reward"])
        if "verifier_result" in data and native_reward != harbor_reward:
            return None
        return native_reward
    return harbor_reward


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
                    "task_checksum": "task_digest",
                    "trial_name": "trial_name",
                    "dataset_id": "dataset_id",
                    "item_id": "item_id",
                    "run_id": "run_id",
                },
            )
            checksum = identity.get("task_digest")
            if isinstance(checksum, str) and re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                identity["task_digest"] = f"sha256:{checksum.lower()}"
            # A real Harbor result.json carries verifier_result only when the
            # verifier actually executed; a recorded exception marks an errored
            # trial, not a clean measurement.
            if isinstance(data.get("verifier_result"), dict):
                identity["execution_status"] = (
                    "errored" if data.get("exception_info") else "measured"
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
    return MeasuredReceipt(
        reward,
        source,
        "harbor_trial",
        identity,
        tuple(path for _, path in observations),
    )


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
            {
                **_claims(data, {"task_id": "task_id", "task_digest": "task_digest"}),
                "execution_status": data["execution_status"],
            },
            (path,),
        )
    if schema == "evallab.candidate.result/v1":
        reward = _candidate_file_reward(path)
        if reward is None:
            return None
        identity = _candidate_file_identity(data)
        identity["execution_status"] = data.get("status")
        return MeasuredReceipt(reward, path, "candidate", identity, (path,))
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
        identity["execution_status"] = "regraded"
        return MeasuredReceipt(reward, path, "regrade_receipt", identity, (path,))
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
        identity["execution_status"] = result.get("status")
        return MeasuredReceipt(reward, path, "matrix_receipt", identity, (path,))
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
        inner = None
        if native.is_dir():
            inner = _measured_reward(native, _depth=_depth + 1)
            if inner is None or inner.value != reward:
                return None
        data = _read_json(candidate) or {}
        identity = _candidate_file_identity(data)
        identity["execution_status"] = data.get("status")
        inner_sources = inner.sources or (inner.source,) if inner is not None else ()
        sources = (candidate, *inner_sources)
        return MeasuredReceipt(reward, candidate, "candidate", identity, sources)
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
        sources = tuple(
            sorted({p for r in children for p in (r.sources or (r.source,))})
        )
        return MeasuredReceipt(first.value, first.source, "harbor_job", merged, sources)
    return None



def _proof_identity_problems(
    declared: Mapping[str, Any], context: Mapping[str, Any]
) -> list[str]:
    """Receipt-declared identity metadata must not contradict the binding."""
    problems: list[str] = []
    task_id = declared.get("task_id")
    expected_task = context.get("task_id")
    if (
        isinstance(task_id, str)
        and isinstance(expected_task, str)
        and CorpusRegistry.task_name(task_id) != CorpusRegistry.task_name(expected_task)
    ):
        problems.append(f"declared task_id {task_id} contradicts {expected_task}")
    for key in ("task_digest", "dataset_id", "item_id", "run_id"):
        value, expected = declared.get(key), context.get(key)
        if isinstance(value, str) and isinstance(expected, str) and value != expected:
            problems.append(f"declared {key} {value} contradicts {expected}")
    return problems


def _receipt_binds(identity: Mapping[str, Any], expect: Mapping[str, Any]) -> bool:
    """The receipt itself declares run/task identity matching the binding.

    Any one binding suffices — the registered ``(dataset_id, item_id)`` pair,
    a canonical task name match, or the exact registered task digest — but an
    identity-less receipt binds nothing regardless of its reward.
    """
    declared_task = _nonempty_str(identity.get("task_id"))
    expected_task = _nonempty_str(expect.get("task_id"))
    if (
        declared_task is not None
        and expected_task is not None
        and CorpusRegistry.task_name(declared_task)
        == CorpusRegistry.task_name(expected_task)
    ):
        return True
    declared_digest = _nonempty_str(identity.get("task_digest"))
    if declared_digest is not None and declared_digest == _nonempty_str(
        expect.get("task_digest")
    ):
        return True
    declared_dataset = _nonempty_str(identity.get("dataset_id"))
    declared_item = _nonempty_str(identity.get("item_id"))
    return (
        declared_dataset is not None
        and declared_item is not None
        and declared_dataset == _nonempty_str(expect.get("dataset_id"))
        and declared_item == _nonempty_str(expect.get("item_id"))
    )


def _adjudication_record_problems(
    record: Mapping[str, Any], proof: Mapping[str, Any]
) -> list[str]:
    """A ground-truth adjudication source record must stand on its own.

    ``evallab.trace_label_adjudication_record/v1`` names a validated human
    adjudicator, the confirmed label, and the run/task/trace binding — each
    declaration must equal the proof's own bound fields.
    """
    problems: list[str] = []
    if record.get("schema") != LABEL_ADJUDICATION_RECORD_SCHEMA:
        return ["adjudication record is not a recognized source receipt"]
    named = _nonempty_str(record.get("adjudicator"))
    if named is None:
        problems.append("adjudication record does not name an adjudicator")
    elif named != proof.get("adjudicator"):
        problems.append("adjudication record names a different adjudicator")
    else:
        from evallab.verdicts import validate_human_actor

        try:
            validate_human_actor(named)
        except ValueError as exc:
            problems.append(f"record adjudicator is not a validated human actor: {exc}")
    if record.get("label") != proof.get("label"):
        problems.append("adjudication record label does not match the proof")
    for key in ("run_id", "dataset_id", "item_id", "trace_prefix_sha256"):
        if not _nonempty_str(record.get(key)):
            problems.append(f"adjudication record does not bind {key}")
        elif record.get(key) != proof.get(key):
            problems.append(f"adjudication record {key} does not match the proof")
    record_task = _nonempty_str(record.get("task_id"))
    proof_task = _nonempty_str(proof.get("task_id"))
    if record_task is None or proof_task is None or (
        CorpusRegistry.task_name(record_task) != CorpusRegistry.task_name(proof_task)
    ):
        problems.append("adjudication record task_id does not match the proof")
    if type(record.get("prefix_step")) is not int or record.get("prefix_step") != proof.get(
        "prefix_step"
    ):
        problems.append("adjudication record prefix_step does not match the proof")
    return problems


def verify_label_proof(
    proof: Any,
    *,
    expect: Mapping[str, Any],
    load_measured: Callable[[Any], MeasuredReceipt | None],
    load_record: Callable[[Any], Mapping[str, Any] | None] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Verify an independent trace-label proof receipt (bounded contract).

    Two recognized producer forms, never a bare ``verified: true`` claim or a
    ``monitor.suspicion`` semantic card:

    * ``evallab.trace_label_verification/v1`` — deterministic verification.
      ``verdict_rule`` must equal the expected deterministic rule (when the
      caller supplies one), ``measurement.reward`` must be the exact reward
      direction the label asserts (``exploit-pass`` -> 1.0 observed unearned
      pass, ``defect-fail`` -> 0.0 observed undeserved failure), and
      ``measurement.evidence_ref`` must load a real measured receipt via
      ``load_measured`` whose value equals the claim. The receipt must stand
      on its own: it must declare a measured ``execution_status`` and bind
      the run/task — matching dataset/item ids, task name, or task digest —
      so an unrelated reward-only trial can never be relabeled as proof. Its
      whole source-file digest (``receipt_sha256``) is frozen into the
      normalized block and re-checked at export.
    * ``evallab.trace_label_adjudication/v1`` — explicit ground-truth
      adjudication naming a validated human ``adjudicator`` and a ``record``
      reference that must load a standalone
      ``evallab.trace_label_adjudication_record/v1`` source receipt via
      ``load_record``: schema, human actor, label, and the run/task/trace
      binding are all checked, and the record's content digest is frozen as
      ``record_sha256`` and re-verified at export. A self-declared
      ``{adjudicator, record}`` pair alone is never verification. Trust
      boundary: the record proves a human judgment was recorded — custody
      and authenticity of the record file itself (who wrote it, where it
      lives) belong to the caller, not to this check.

    ``expect`` binds the proof to identity and trace: every field the caller
    supplies (``run_id``, ``dataset_id``, ``item_id``, ``task_id``,
    ``task_digest``, ``rule``, ``prefix_step``, ``trace_prefix_sha256``,
    ``label``) must equal the proof's declaration; ``run_id``, ``dataset_id``,
    ``item_id``, ``task_id``, ``prefix_step`` and ``trace_prefix_sha256`` are
    mandatory declarations in the proof itself.

    Returns the canonical normalized verification block to embed in the
    ``evallab.trace_label/v1`` receipt, or ``(None, problems)``.
    """
    problems: list[str] = []
    if not isinstance(proof, Mapping) or not proof:
        return None, ["label proof is not a verification receipt object"]
    schema = proof.get("schema")
    if schema not in _LABEL_PROOF_SCHEMAS:
        return None, ["label proof is not a recognized independent verification receipt"]
    if any(key in proof for key in _SUSPICION_KEYS):
        problems.append("a semantic suspicion card is not an independent label proof")
    label = proof.get("label")
    if label not in TRACE_LABELS:
        problems.append(f"label proof label {label!r} is not a verified trace label")
    wanted_label = _nonempty_str(expect.get("label"))
    if wanted_label is not None and label != wanted_label:
        problems.append("label proof label conflicts with the expected label")
    declared_split = proof.get("split")
    if declared_split is not None and (
        not isinstance(declared_split, str) or declared_split.strip().lower() != "dev"
    ):
        problems.append("label proof declares a non-dev split")
    for key in ("run_id", "dataset_id", "item_id"):
        declared, wanted = proof.get(key), expect.get(key)
        if _nonempty_str(declared) is None:
            problems.append(f"label proof does not bind {key}")
        elif isinstance(wanted, str) and wanted and declared != wanted:
            problems.append(f"label proof {key} conflicts with the bound identity")
    declared_task = _nonempty_str(proof.get("task_id"))
    wanted_task = _nonempty_str(expect.get("task_id"))
    if declared_task is None:
        problems.append("label proof does not bind task_id")
    elif wanted_task is not None and (
        CorpusRegistry.task_name(declared_task) != CorpusRegistry.task_name(wanted_task)
    ):
        problems.append("label proof task_id conflicts with the bound task")
    declared_digest = _nonempty_str(proof.get("task_digest"))
    wanted_digest = _nonempty_str(expect.get("task_digest"))
    if declared_digest is not None and wanted_digest is not None and (
        declared_digest != wanted_digest
    ):
        problems.append("label proof task_digest conflicts with the bound task")
    declared_prefix = proof.get("prefix_step")
    if type(declared_prefix) is not int or declared_prefix < 0:
        problems.append("label proof does not bind a nonnegative integer prefix_step")
        declared_prefix = None
    wanted_prefix = expect.get("prefix_step")
    if (
        declared_prefix is not None
        and type(wanted_prefix) is int
        and declared_prefix != wanted_prefix
    ):
        problems.append("label proof prefix_step conflicts with the bound prefix")
    declared_trace = _nonempty_str(proof.get("trace_prefix_sha256"))
    wanted_trace = _nonempty_str(expect.get("trace_prefix_sha256"))
    if declared_trace is None:
        problems.append("label proof does not bind the observed trace prefix digest")
    elif wanted_trace is None:
        problems.append("the bound observed trace prefix digest cannot be verified")
    elif declared_trace != wanted_trace:
        problems.append("label proof trace_prefix_sha256 conflicts with the observed prefix")
    mechanism = _nonempty_str(proof.get("mechanism"))
    citations: list[dict[str, Any]] = []
    evidence = proof.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            problems.append("label proof evidence is not a list")
        else:
            for index, cite in enumerate(evidence):
                if (
                    not isinstance(cite, Mapping)
                    or type(cite.get("step")) is not int
                    or not _nonempty_str(cite.get("quote"))
                ):
                    problems.append(
                        f"proof citation[{index}] needs an integer step and nonempty quote"
                    )
                    continue
                citations.append({"step": cite["step"], "quote": cite["quote"]})
    rule = _nonempty_str(proof.get("verdict_rule"))
    wanted_rule = _nonempty_str(expect.get("rule"))
    if rule is not None and wanted_rule is not None and rule != wanted_rule:
        problems.append("label proof verdict_rule conflicts with the bound deterministic rule")
    measurement_block: dict[str, Any] | None = None
    adjudicator: str | None = None
    record: str | None = None
    record_sha256: str | None = None
    if schema == LABEL_VERIFICATION_SCHEMA:
        if rule is None:
            problems.append("deterministic label proof does not declare a verdict_rule")
        measurement = proof.get("measurement")
        if not isinstance(measurement, Mapping):
            problems.append("deterministic label proof carries no measurement object")
        else:
            claimed = finite_reward(measurement.get("reward"))
            expected = _LABEL_PROOF_REWARDS.get(str(label))
            if claimed is None:
                problems.append("proof measurement reward is not a finite [0,1] number")
            elif claimed != expected:
                problems.append(
                    f"claimed reward {claimed} cannot prove {label} (expected {expected})"
                )
            receipt = load_measured(measurement.get("evidence_ref"))
            if receipt is None:
                problems.append("proof measurement did not load a measured receipt")
            else:
                if claimed is not None and receipt.value != claimed:
                    problems.append("measured receipt reward contradicts the claimed reward")
                problems.extend(_proof_identity_problems(receipt.identity, expect))
                execution_status = _nonempty_str(
                    receipt.identity.get("execution_status")
                )
                if execution_status not in ("measured", "regraded", "ok"):
                    problems.append(
                        "measured receipt declares no executed measurement status"
                    )
                if not _receipt_binds(receipt.identity, expect):
                    problems.append(
                        "measured receipt does not bind the run/task: it declares "
                        "no matching dataset/item or task name/digest"
                    )
                try:
                    receipt_digest = receipt.content_sha256()
                except OSError:
                    receipt_digest = None
                    problems.append("measured receipt sources are unreadable")
                declared_receipt_sha = _nonempty_str(measurement.get("receipt_sha256"))
                if (
                    declared_receipt_sha is not None
                    and declared_receipt_sha != receipt_digest
                ):
                    problems.append(
                        "measured receipt content changed since the committed proof"
                    )
                measurement_block = {
                    "evidence_ref": measurement.get("evidence_ref"),
                    "reward": receipt.value,
                    "receipt_kind": receipt.kind,
                    "receipt_sha256": receipt_digest,
                    "execution_status": execution_status,
                }
    else:
        named = _nonempty_str(proof.get("adjudicator"))
        if named is None:
            problems.append("adjudication proof does not name an adjudicator")
        else:
            from evallab.verdicts import validate_human_actor

            try:
                adjudicator = validate_human_actor(named)
            except ValueError as exc:
                problems.append(f"adjudicator is not a validated human actor: {exc}")
        record = _nonempty_str(proof.get("record"))
        if record is None:
            problems.append("adjudication proof carries no source provenance record")
        elif load_record is None:
            problems.append("adjudication proof cannot resolve its source record")
        else:
            record_obj = load_record(record)
            if not isinstance(record_obj, Mapping):
                problems.append(
                    "adjudication record did not load a recognized source receipt"
                )
            else:
                record_sha256 = _sha256_text(_canon(dict(record_obj)))
                declared_record_sha = _nonempty_str(proof.get("record_sha256"))
                if declared_record_sha is not None and declared_record_sha != record_sha256:
                    problems.append(
                        "adjudication record content changed since the committed proof"
                    )
                problems.extend(_adjudication_record_problems(record_obj, proof))
    if problems:
        return None, problems
    assert declared_prefix is not None
    block: dict[str, Any] = {
        "schema": schema,
        "mode": "deterministic"
        if schema == LABEL_VERIFICATION_SCHEMA
        else "ground_truth_adjudication",
        "label": label,
        "run_id": proof["run_id"],
        "dataset_id": proof["dataset_id"],
        "item_id": proof["item_id"],
        "task_id": declared_task,
        "prefix_step": declared_prefix,
        "trace_prefix_sha256": declared_trace,
        "mechanism": mechanism,
        "evidence": citations,
    }
    if declared_digest is not None:
        block["task_digest"] = declared_digest
    if schema == LABEL_VERIFICATION_SCHEMA:
        block["verdict_rule"] = rule
        block["measurement"] = measurement_block
    else:
        block["adjudicator"] = adjudicator
        block["record"] = record
        block["record_sha256"] = record_sha256
    return block, []


