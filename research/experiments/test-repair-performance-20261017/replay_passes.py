#!/usr/bin/env python3
"""Plan, then explicitly execute bounded native retained-diff replays (no models).

Free plan (all output directories must be new, versioned, and in the owned lane)::

  uv run --no-sync python research/experiments/test-repair-performance-20261017/replay_passes.py \
    --out runs/envcheck-20261017/m2/native-replay-plan-v1

Execution additionally REQUIRES --execute --max-usd --reserve-usd --approval and
--budget. The budget is the parent's SHARED, fresh $4-lane CloudSpendGate JSON,
not a new allowance for each invocation. Existing reservations are never reset.
Repeat --execution UUID with --batch for multiple original passes; controls are
shared only within this invocation, by exact parent task and full grader digest.
Use --evaluation-only for the fixed plan's frozen B a1/a2 sources, including
recorded reward0. Non-evaluation sources must remain recorded passes. No command
here generates training data, exports conversations, or invokes a model.

Interfaces under --out:
* plan.json: input hashes, untouched inventory rows/manifest pins, verified live
  aliases, native lineage/reference/resources, readiness and execution estimates.
* summary.json: {"rows": [...]} canonical QA envelopes around untouched cf.replay
  results, or explicitly unmeasured refusal rows. Model identity stays original.
* receipt.json: terminal disposition, counts, fences and reservation accounting.
* charge-journal.jsonl (execution only): fsynced reservations before dispatch;
  --budget also durably retains each charge in its shared `charges` list.
* controls/<parent>/<full-digest>/ and items/<UUID>/<full-digest>/: untouched native
  cf rows, input/dispatched matrices, actual matrix observations and references.

Native CF owns its unique runs/qa-replay-* jobs and runs/qa-trace-regrade/scripts;
run_matrix owns setup, solution staging, artifact collection, grading and normal
post-processing. Those native paths remain unchanged in the receipts. There is
no executor replacement, ingestion bypass, external-solution reuse or old-control
cache. This is a NEW oracle-script control using retained agent.diff, or the exact
agent.kept.diff whose complete pre-hidden custody evaluation_corrections proves;
it does NOT recover an original workspace, snapshot, artifact manifest, or
trajectory. The failed historical 772a78ad native regrade remains a refusal.
Reservations are conservative fences, not provider invoices.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
LANE = ROOT / "runs/envcheck-20261017"
PILOT = "772a78ad-799b-4b76-8ec0-399c08a97474"
ARCHIVE = Path.home() / "Developer/eval-lab-results/2026-10-16/envcheck-repair"
MODAL = {
    "modal_app_name": "mimo-clean-census-envcheck-20261017",
    "modal_sandbox_timeout_seconds": 300,
    "modal_resource_policy": "limit",
}
NOTICE = (
    "New model-free native retained-patch replay controls, not reconstructed "
    "original workspaces or trajectories. Nop/reference controls are execution "
    "gates, not model executions or independent semantic correctness. Partial "
    "grader scope, source annotations and every training exclusion remain intact."
)
SCHEMA = "envcheck.native-pass-replay/v1"


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json_bytes(value)
    if exclusive:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".replay-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def append_json(path: Path, value: Any) -> None:
    with path.open("ab") as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def dependencies() -> tuple[Any, Any, Any]:
    # Importing this driver itself does not import a provider or start any work.
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("envcheck_replay_partition", Path(__file__).with_name("partition.py"))
    if spec is None or spec.loader is None:
        raise ValueError("existing partition.py is unavailable")
    partition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partition)
    from evallab import runner
    from evallab.qa import counterfactual as cf

    for module in (cf, runner):
        if not Path(module.__file__).resolve().is_relative_to(ROOT / "src/evallab"):
            raise ValueError(f"native module comes from a different checkout: {module.__file__}")
    return partition, cf, runner


def evaluation_helpers() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import evaluation_corrections as evaluation

    require_equal(Path(evaluation.__file__).resolve(), Path(__file__).with_name("evaluation_corrections.py").resolve(),
                  "evaluation patch proof must come from this driver directory")
    return evaluation


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label}: {actual!r} != {expected!r}")


def unchanged_diff(cf: Any, text: str, label: str) -> dict[str, Any]:
    kept, dropped = cf.strip_binary_artifacts(text)
    if kept != text or dropped:
        raise ValueError(f"{label}: binary-artifact stripping would change the submission: {dropped}")
    return {"sha256": sha256(text.encode("utf-8")), "bytes": len(text.encode("utf-8")), "dropped_binary_artifacts": []}


def evaluation_source(
    source: dict[str, Any], pin: dict[str, Any], binding: dict[str, Any], plan: dict[str, Any],
    plan_ref: dict[str, Any], evaluation: Any,
) -> dict[str, Any]:
    matches = [(task, candidate) for task in plan["tasks"] for candidate in task["candidates"]
               if candidate["native"] and candidate["native"]["execution_id"] == source["execution_id"]]
    if len(matches) != 1:
        raise ValueError("evaluation source is not one exact native B a1/a2 UUID in the fixed plan")
    task, candidate = matches[0]
    base = candidate["native"]
    if (task["task"] != source["task"] or task.get("heldout") is not True
            or task.get("training_allowed") is not False or source.get("heldout") is not True
            or candidate["attempt"] not in (1, 2) or source.get("attempt") not in (None, candidate["attempt"])
            or candidate.get("reusable") is not True or candidate.get("reuse_issues")
            or candidate["original_reward"] not in (0, 1)
            or source["original_reward"] != candidate["original_reward"]
            or base["native_reward"] != candidate["original_reward"]
            or base["task_digest"] != task["source_task_digest"]
            or candidate["source_package_digest"] != task["source_task_digest"]):
        raise ValueError("evaluation source differs from the fixed plan's actually scored original cell")
    require_equal(evaluation.bound_partition(binding), source, "evaluation inventory file/row binding")
    evaluation.checked_variant(task, {**source, "variant": pin})
    patch, custody = evaluation.source_patch_for(source, base, binding)
    return {
        "purpose": "evaluation", "heldout": True, "training_allowed": False,
        "plan": {key: plan_ref[key] for key in ("path", "sha256")},
        "inventory_input": binding, "attempt": candidate["attempt"], "native": base,
        "source_patch": patch, "source_patch_custody": custody,
    }


def source_custody(
    source: dict[str, Any], partition: Any, cf: Any, *, evaluation: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    if source.get("flags", {}).get("alias_conflicts"):
        raise ValueError("inventory records conflicting source aliases")
    reward = partition.number(source.get("original_reward"))
    if evaluation is None:
        require_equal(reward, 1.0, "original non-evaluation source must be a recorded pass")
    elif reward not in {0.0, 1.0}:
        raise ValueError("evaluation source must have an actual recorded binary reward")
    alternative = evaluation is not None and evaluation["source_patch_custody"] is not None
    aliases = source.get("provenance", {}).get("aliases")
    if not isinstance(aliases, list) or not aliases:
        raise ValueError("inventory has no native source aliases")
    checks, verified, problems = [], [], []
    for alias in aliases:
        trial = Path(alias["trial"]).expanduser().resolve()
        check: dict[str, Any] = {"trial": str(trial), "expected": alias}
        if not trial.is_dir():
            check["state"] = "unavailable_alias_not_used"
            checks.append(check)
            continue
        try:
            current = partition.source_record(trial)
            for key in ("execution_id", "task", "model", "attempt", "original_reward", "agent", "started_at"):
                require_equal(current.get(key), source.get(key), f"current source {key}")
            require_equal(alias["trial_uuid"], source["execution_id"], "inventory alias UUID")
            for key in ("job_uuid", "trial_uuid", "source_digest", "task_checksum", "executed_task_digest", "flags"):
                require_equal(current["alias"].get(key), alias.get(key), f"current alias {key}")
            for key, reference in alias["files"].items():
                live = Path(reference["path"]).expanduser().resolve()
                if not live.is_relative_to(trial):
                    raise ValueError(f"alias file reference escapes its trial: {key}")
                require_equal(partition.file_ref(live), reference, f"retained source file {key}")
            required = ("result", "config") if alternative else ("result", "config", "patch")
            for key in required:
                if key not in alias["files"] or key not in current["alias"]["files"]:
                    raise ValueError(f"retained source {key} is missing; no substitute will be synthesized")
            patch_path = trial / ("verifier/agent.kept.diff" if alternative else "verifier/agent.diff")
            patch = patch_path.read_text(encoding="utf-8")
            if not patch.strip():
                raise ValueError("retained source patch is empty")
            require_equal(patch.encode("utf-8"), patch_path.read_bytes(), "native diff text decoding must preserve every byte")
            patch_ref = partition.file_ref(patch_path)
            if evaluation is not None:
                require_equal(patch_ref["sha256"], evaluation["source_patch"]["sha256"], "fixed-plan retained patch bytes")
            check.update(state="verified", actual=current["alias"], source_patch=patch_ref,
                         patch=unchanged_diff(cf, patch, "retained source patch"))
            verified.append(trial)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            check.update(state="refused", error=f"{type(exc).__name__}: {exc}")
            problems.append(check["error"])
        checks.append(check)
    if problems:
        raise ValueError("live alias custody failed: " + "; ".join(problems))
    if not verified:
        raise ValueError("no retained, verified native alias with its actual nonempty patch")
    preferred = Path(evaluation["native"]["source_trial"] if evaluation else source["source_trial"]).expanduser().resolve()
    if evaluation is not None and preferred not in verified:
        raise ValueError("fixed-plan native source is not a verified retained alias")
    trial = preferred if preferred in verified else verified[0]
    selected = next(check for check in checks if check["trial"] == str(trial))
    custody = {"aliases": checks, "selected_trial": str(trial), "source_digest": cf.source_digest(trial),
               "source_patch": selected["source_patch"]}
    if evaluation is not None:
        custody["evaluation"] = evaluation
    return trial, custody


def resource_metadata(package: Path) -> dict[str, Any]:
    # Use Harbor's real inheritance/image precedence, not guessed TOML defaults.
    from harbor.models.task.task import Task
    from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

    task = Task(package)
    if task.config.steps:
        raise ValueError("native CF solution overrides do not support task steps")
    definition = resolve_verifier_environment_definition(task.config, task.paths)
    if definition is None:
        raise ValueError("qualified grader must have a real separate verifier")
    output = {}
    for role, config in (("task", task.config.environment), ("verifier", definition.config)):
        cpu, memory, gpu = config.cpus, config.memory_mb, config.gpus
        if type(cpu) is not int or not 0 < cpu <= 2:
            raise ValueError(f"{role} resources must explicitly declare 0 < cpus <= 2, got {cpu!r}")
        if type(memory) is not int or not 0 < memory <= 8192:
            raise ValueError(f"{role} resources must explicitly declare 0 < memory_mb <= 8192, got {memory!r}")
        if gpu not in (None, 0) or config.gpu_types:
            raise ValueError(f"{role} GPU resources are forbidden")
        output[role] = {"cpus": cpu, "memory_mb": memory, "gpus": 0, "docker_image": config.docker_image}
    output["verifier"].update(directory=str(definition.directory), bundled_tests=definition.bundled_tests)
    return output


def grader_custody(pin: dict[str, Any], variants_path: Path, records: list[Path], partition: Any, cf: Any) -> tuple[Any, str, dict[str, Any]]:
    from evallab.mimo_clean import (
        MANIFEST_V6_REL,
        REFERENCE_INDEX_REL,
        load_reference_index,
        oracle_info_from_index,
    )
    from evallab.registry import compute_subpath_digest, compute_task_digests, harbor_task_digest
    from evallab.task_variants import verify

    if not partition.mechanical_grader_admitted(pin) or not pin["measurement_eligible"]:
        raise ValueError(f"grader lacks exact qualification custody: {pin['measurement_custody']['problems']}")
    package = Path(pin["package_path"])
    row = pin["manifest_row"]
    explicit = row.get("record")
    paths = [((variants_path.parent / explicit).resolve())] if explicit else []
    filename = pin["repaired_digest"][7:19] + ".json"
    slug = pin["task"].replace("/", "__")
    for path in records:
        if path.is_file():
            paths.append(path)
        elif path.is_dir():
            paths.extend(path.rglob(filename))
    candidates = [(path, record) for path, record in cf.repair_records(sorted(set(paths)))
                  if record.task_name == pin["task"] and record.variant_digest == pin["repaired_digest"]]
    if explicit:
        candidates = [(path, record) for path, record in candidates if path.resolve() == paths[0]]
    if not candidates:
        raise ValueError(f"exact native verifier-repair VariantRecord missing for {slug}/{filename}; pass --records")
    identities = {partition.canonical_bytes(record.model_dump(mode="json", by_alias=True, exclude={"status", "evidence"})) for _, record in candidates}
    if len(identities) != 1:
        raise ValueError("conflicting exact native lineage records")
    record_path, record = candidates[0]
    record_ref = partition.file_ref(record_path)
    if row.get("record_sha256"):
        require_equal(record_ref["sha256"], partition.digest(row["record_sha256"]), "manifest record hash")
    parent = cf.v6_grader(pin["task"], ROOT)
    if parent is None:
        raise ValueError("current exact frozen v6 parent package/manifest is unavailable")
    require_equal(record.parent.digest, parent.variant_digest, "native record frozen v6 parent")
    require_equal(record.parent.digest, partition.digest(row.get("v6_parent_digest_of_variant")), "manifest native parent digest")
    if record.parent.source.kind == "local":
        require_equal(Path(record.parent.source.path).resolve(), parent.package.resolve(), "native parent source path")
    require_equal(record.components_changed, ["verifier"], "native changed-component scope")
    for directory in (package, parent.package):
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError(f"native package contains symlinks: {directory}")
    failures = verify(record, parent_dir=parent.package, repo_root=ROOT, variants_root=package.parent.parent)
    if failures:
        raise ValueError("native VariantRecord custody: " + "; ".join(failures))
    digests, parent_digests = compute_task_digests(package), compute_task_digests(parent.package)
    require_equal(digests.package, pin["repaired_digest"], "current qualified package digest")
    require_equal(harbor_task_digest(package), record.variant_harbor_digest, "native Harbor package digest")
    require_equal(harbor_task_digest(parent.package), record.parent.harbor_digest, "native Harbor parent digest")
    for component in ("task_toml", "instruction", "environment"):
        require_equal(getattr(digests, component), getattr(parent_digests, component), f"unchanged {component}")
    require_equal(compute_subpath_digest(package / "solution"), compute_subpath_digest(parent.package / "solution"), "unchanged package solution")
    for change in record.files:
        relative = Path(change.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("native record file escapes its package")
        path = package / relative
        require_equal(sha256(path.read_bytes()) if path.is_file() else None, change.after_sha256, f"native record file {relative}")
    grader = cf.Grader(pin["task"], record.transform, pin["repaired_digest"], package, str(record_path.resolve()))
    index_path = ROOT / REFERENCE_INDEX_REL
    info = oracle_info_from_index(
        pin["task"].partition("/")[2], load_reference_index(index_path), repo_root=ROOT
    )
    reference_origin: dict[str, Any] = {"kind": "native_reference_index"}
    if info is not None:
        reference_path = Path(info.patch_path)
        reference = info.patch_bytes.decode("utf-8")
        require_equal(cf.reference_patch(pin["task"], ROOT), reference, "native reference bytes")
    else:
        controls = [
            control for control in pin["measurement_custody"]["controls"]
            if control["role"] == "reference" and control["row"].get("oracle_patch")
        ]
        if len(controls) != 1:
            raise ValueError("no indexed reference or retained M1 oracle input")
        control = controls[0]
        reference_path = Path(control["row"]["oracle_patch"])
        reference = reference_path.read_bytes().decode("utf-8")
        reference_origin = {
            "kind": "retained_m1_oracle_input",
            "accepted_control": {
                key: control[key] for key in ("path", "sha256")
            },
            "historical_input_byte_identity_attested": False,
            "validation": "Current input bytes are pinned here; a fresh native reference pass is required before any source measurement.",
        }
    if not reference.strip():
        raise ValueError("reference input is empty")
    custody = {
        "record": record_ref, "record_aliases": [partition.file_ref(path) for path, _ in candidates],
        "parent": parent.to_json(), "v6_manifest": partition.file_ref(ROOT / MANIFEST_V6_REL),
        "package_digest": digests.package, "verifier_digest": digests.verifier,
        "resources": resource_metadata(package),
        "reference_index": partition.file_ref(index_path),
        "reference_file": partition.file_ref(reference_path),
        "reference_origin": reference_origin,
        "inputs": {"nop": unchanged_diff(cf, "", "nop"), "reference": unchanged_diff(cf, reference, "reference")},
    }
    return grader, reference, custody


@dataclass
class ReplayJob:
    source: dict[str, Any]
    pin: dict[str, Any]
    trial: Path | None = None
    grader: Any = None
    reference: str | None = None
    evaluation: Any = None
    source_custody: dict[str, Any] = field(default_factory=dict)
    grader_custody: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.source["task"], self.pin["repaired_digest"]

    def plan(self) -> dict[str, Any]:
        return {
            "execution_id": self.source["execution_id"], "task": self.source["task"],
            "source": self.source, "variant": self.pin,
            "source_custody": self.source_custody, "grader_custody": self.grader_custody,
            "grader_source": self.grader.to_json() if self.grader else None,
            "status": "refused" if self.problems else "ready", "problems": self.problems,
        }


def matrix_support(job: ReplayJob, runner: Any) -> list[str]:
    from evallab.execution_contracts import new_ulid
    from evallab.schemas import ExperimentMatrix, MatrixRun

    missing = sorted(set(MODAL) - set(ExperimentMatrix.model_fields))
    if missing:
        return ["native_matrix_fields_missing:" + ",".join(missing)]
    if job.grader is None:
        return ["native_matrix_request_preflight_requires_verified_grader"]
    try:
        matrix = ExperimentMatrix(
            matrix_id=new_ulid(), name="e1017-m2-preflight", hypothesis="Free bounded request propagation check.",
            benchmark_family="qa-trace-regrade", task_id=job.source["task"].partition("/")[2],
            task=str(job.grader.package), task_package_digest=job.pin["repaired_digest"],
            verifier_digest=job.grader_custody["verifier_digest"], environment="modal", jobs_dir="runs",
            concurrency=1, timeout_seconds=3600, runs=[MatrixRun(name="e1017-m2-preflight", agent="oracle")],
            **MODAL,
        )
        request = runner.request_from_matrix(matrix, matrix.runs[0], repo_root=ROOT)
        for key, expected in MODAL.items():
            require_equal(getattr(request, key, None), expected, f"native matrix request propagation {key}")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [f"native_matrix_request_not_bounded:{type(exc).__name__}:{exc}"]
    return []


class Gate:
    """Journal the REAL CloudSpendGate's single native charge, before dispatch.

    The shared JSON remains loadable by existing qualifier gates. A separate lock
    serializes this driver's read/charge/replace; the parent must also serialize
    other lane tools which do not participate in this lock protocol.
    """

    def __init__(self, native: Any, path: Path, output: Path, *, execute: bool):
        self.native, self.path, self.output = native, path, output
        self.execute = execute
        self.journal = output / "charge-journal.jsonl"
        self.context: dict[str, Any] | None = None
        self.charged = self.dispatched = False
        self.charges_this_run = self.dispatches_this_run = 0
        self.read_state()
        if execute:
            with self.journal.open("xb") as stream:
                stream.flush()
                os.fsync(stream.fileno())

    def read_state(self) -> dict[str, Any]:
        state = json.loads(self.path.read_text()) if self.path.exists() else {**self.native.to_json(), "charges": []}
        expected = self.native.to_json()
        for key in ("approval", "cap_usd", "usd_per_execution"):
            require_equal(state.get(key), expected[key], f"shared lane gate {key}")
        count = state.get("executions")
        if type(count) is not int or count < 0:
            raise ValueError("shared lane gate has an invalid execution count")
        reserved = count * self.native.usd_per_execution
        if reserved > self.native.cap_usd + 1e-9:
            raise ValueError("shared lane gate already exceeds its cap")
        require_equal(state.get("reserved_usd"), round(reserved, 4), "shared lane reservations")
        if not isinstance(state.get("charges", []), list):
            raise ValueError("shared lane charge journal is not a list")
        self.native.executions = count
        return state

    @contextlib.contextmanager
    def execution(self, context: dict[str, Any]) -> Any:
        if self.context is not None:
            raise RuntimeError("only one native replay may be in flight")
        self.context = context
        self.charged = self.dispatched = False
        try:
            yield
        finally:
            self.context = None

    def charge(self) -> None:
        if not self.execute:
            raise RuntimeError("a free-plan gate cannot authorize any execution")
        if self.context is None or self.charged:
            raise RuntimeError("native CF must charge exactly once for an armed execution")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read_state()
            # Only cf.replay calls this method. No manual extra reservation.
            self.native.charge()
            event = {
                "event": "reserved_before_dispatch", "at": now(), "out": str(self.output),
                "ordinal": self.native.executions, "execution": self.context, "gate": self.native.to_json(),
            }
            write_json(self.path, {**state, **self.native.to_json(), "charges": [*state.get("charges", []), event]})
            self.charges_this_run += 1
            append_json(self.journal, event)
            self.charged = True

    def before_dispatch(self, matrix_id: str) -> None:
        if self.context is None or not self.charged or self.dispatched:
            raise RuntimeError("dispatch requires one durable, unused native CF charge")
        append_json(self.journal, {"event": "dispatch", "at": now(), "matrix_id": matrix_id, "execution": self.context})
        self.dispatched = True
        self.dispatches_this_run += 1

    def to_json(self) -> dict[str, Any]:
        return self.native.to_json()


def current_bindings(job: ReplayJob, inputs: list[dict[str, Any]], partition: Any, cf: Any) -> None:
    from evallab.task_variants import task_directory_digest

    origin = job.grader_custody["reference_origin"]
    references = [*inputs, job.grader_custody["record"], job.grader_custody["v6_manifest"],
                  job.grader_custody["reference_index"], job.grader_custody["reference_file"]]
    if origin["kind"] == "retained_m1_oracle_input":
        references.append(origin["accepted_control"])
    for reference in references:
        require_equal(partition.file_ref(Path(reference["path"])), reference, "pinned input changed before dispatch")
    for alias in job.source_custody["aliases"]:
        if alias["state"] == "verified":
            for reference in [*alias["actual"]["files"].values(), alias["source_patch"]]:
                require_equal(partition.file_ref(Path(reference["path"])), reference, "source alias changed before dispatch")
            require_equal(partition.digest(cf.source_digest(Path(alias["trial"]))), alias["actual"]["source_digest"], "source alias digest changed")
    require_equal(cf.source_digest(job.trial), job.source_custody["source_digest"], "original source digest changed")
    evaluation = job.source_custody.get("evaluation")
    if evaluation is not None:
        require_equal(job.evaluation.bound_partition(evaluation["inventory_input"]), job.source,
                      "evaluation source inventory row changed before dispatch")
        patch, custody = job.evaluation.source_patch_for(job.source, evaluation["native"], evaluation["inventory_input"])
        require_equal(patch, evaluation["source_patch"], "fixed-plan source patch changed before dispatch")
        require_equal(custody, evaluation["source_patch_custody"], "pre-hidden source patch proof changed before dispatch")
        require_equal(patch, job.source_custody["source_patch"], "selected patch differs from fixed-plan source")
    require_equal(task_directory_digest(job.grader.package), job.pin["repaired_digest"], "qualified grader changed")
    parent = job.grader_custody["parent"]
    require_equal(task_directory_digest(Path(parent["package"])), parent["variant_digest"], "frozen parent changed")
    require_equal(resource_metadata(job.grader.package), job.grader_custody["resources"], "actual native resources changed")
    reference = Path(job.grader_custody["reference_file"]["path"]).read_bytes().decode("utf-8")
    require_equal(reference, job.reference, "pinned reference input changed")
    if origin["kind"] == "native_reference_index":
        require_equal(cf.reference_patch(job.source["task"], ROOT), job.reference, "indexed reference patch changed")
    elif origin["kind"] != "retained_m1_oracle_input":
        raise ValueError("unrecognized native reference origin")
    unchanged_diff(cf, job.reference, "reference")
    patch_ref = job.source_custody["source_patch"]
    patch = Path(patch_ref["path"]).read_text(encoding="utf-8")
    require_equal(sha256(patch.encode("utf-8")), patch_ref["sha256"], "retained source patch text changed")
    if not patch.strip():
        raise ValueError("retained source patch disappeared before dispatch")
    unchanged_diff(cf, patch, "retained source patch")


def native_replay(job: ReplayJob, role: str, directory: Path, gate: Gate, inputs: list[dict[str, Any]], partition: Any, cf: Any, runner: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from evallab.registry import compute_task_digests
    from evallab.schemas import ExperimentMatrix

    directory.mkdir(parents=True, exist_ok=False)
    context = {"role": role, "execution_id": job.source["execution_id"], "task": job.source["task"],
               "grader_digest": job.pin["repaired_digest"], "source_digest": job.source_custody["source_digest"]}
    observation: dict[str, Any] = {"role": role, "started_at": now(), "dispatches": []}

    def bounded_matrix(matrix: Any, *, root: Path) -> Any:
        require_equal(root.resolve(), ROOT, "native matrix repo root")
        if set(MODAL) - set(ExperimentMatrix.model_fields):
            raise ValueError("native matrix bounded fields disappeared; no model_copy extras are accepted")
        require_equal(matrix.environment, "modal", "native replay backend")
        require_equal(matrix.concurrency, 1, "native replay concurrency")
        require_equal(len(matrix.runs), 1, "native replay run count")
        run = matrix.runs[0]
        if run.agent != "oracle" or run.model is not None or run.attempts != 1 or run.allow_billable or not run.solution:
            raise ValueError("only one real, model-free oracle-script submission is admitted")
        require_equal((ROOT / matrix.task).resolve(), job.grader.package.resolve(), "native replay package")
        digests = compute_task_digests(job.grader.package)
        require_equal(matrix.task_package_digest, digests.package, "matrix package digest")
        require_equal(matrix.verifier_digest, digests.verifier, "matrix verifier digest")
        current_bindings(job, inputs, partition, cf)
        bounded = matrix.model_copy(update=MODAL)
        # Re-validate declared fields; Pydantic model_copy alone accepts ignored extras.
        ExperimentMatrix.model_validate(bounded.model_dump(mode="json"))
        request = runner.request_from_matrix(bounded, run, repo_root=ROOT)
        for key, expected in MODAL.items():
            require_equal(getattr(request, key, None), expected, f"bounded request {key}")
        if os.environ.get("EVALLAB_NO_PROCESS_JOB") == "1":
            raise ValueError("normal native ingestion/post-processing must not be disabled")
        job_dir = request.jobs_dir / request.name
        matrix_receipt = request.jobs_dir / ".executor" / f"{matrix.matrix_id}.matrix.json"
        if job_dir.exists() or matrix_receipt.exists():
            raise ValueError("native job/receipt already exists; implicit reuse is forbidden")
        write_json(directory / "matrix.input.json", matrix.model_dump(mode="json"), exclusive=True)
        write_json(directory / "matrix.dispatched.json", bounded.model_dump(mode="json"), exclusive=True)
        gate.before_dispatch(matrix.matrix_id)
        execution = runner.run_matrix(bounded, root=ROOT, reuse_existing=False)
        observation["dispatches"].append({
            "matrix_id": matrix.matrix_id, "receipt": str(execution.receipt_path),
            "invocations": str(execution.invocation_path), "exit_code": execution.exit_code,
            "results": execution.results,
        })
        write_json(directory / "matrix-observation.json", observation)
        return execution

    # Reject missing/changed source bytes before CF can choose its snapshot path.
    current_bindings(job, inputs, partition, cf)
    with gate.execution(context):
        kwargs: dict[str, Any] = {}
        if role != "source":
            kwargs["diff_text"] = "" if role == "nop" else job.reference
        elif job.source_custody.get("evaluation", {}).get("source_patch_custody") is not None:
            # Only the complete, rechecked pre-hidden proof permits this override.
            kwargs["diff_text"] = Path(job.source_custody["source_patch"]["path"]).read_bytes().decode("utf-8")
        # Ordinary source replays still let native CF read its actual agent.diff.
        row = cf.replay(job.trial, job.grader, directory / "work", repo_root=ROOT,
                        environment="modal", spend_gate=gate, matrix_runner=bounded_matrix, **kwargs)
    write_json(directory / "native.json", row, exclusive=True)
    observation.update(finished_at=now(), canonical_result=partition.file_ref(directory / "native.json"))
    write_json(directory / "observation.json", observation, exclusive=True)
    return row, {"path": str(directory / "native.json"), "sha256": sha256(json_bytes(row)), "observation": observation}


def envelope(job: ReplayJob, cf: Any, *, evaluation_only: bool, controls: dict[str, Any], native: dict[str, Any] | None = None, refusals: list[str] | None = None) -> dict[str, Any]:
    base = {
        "engine": cf.ENGINE_VERSION, "execution_id": job.source["execution_id"],
        "source_digest": job.source_custody.get("source_digest"), "grader_digest": job.pin["repaired_digest"],
        "task": job.source["task"], "model": job.source.get("model"), "agent": job.source.get("agent"),
        "attempt": job.source.get("attempt"), "heldout": job.source.get("heldout"),
        "grader_source": job.grader.to_json() if job.grader else None,
        "transform": job.grader.transform if job.grader else None,
        "valid_grader": controls.get("valid") is True,
        "controls": {"nop": controls.get("nop"), "reference": controls.get("reference")},
        "measurement_scope": "new_native_retained_diff_control", "evaluation_only": evaluation_only,
        "new_model_execution": False, "training_exported": False,
        "training_allowed": False, "flags": job.source.get("flags", {}),
        "source_custody": job.source_custody,
        "original_source": job.source, "variant": job.pin,
    }
    if native is None:
        native = {"trial": str(job.trial) if job.trial else job.source.get("source_trial"),
                  "recorded": job.source.get("original_reward"), "regraded": None,
                  "outcome": "refused", "refusals": refusals or ["not_executed"]}
    return {**native, **base}


def successful_native(row: dict[str, Any], partition: Any, *, expected: float | None = None) -> bool:
    score = partition.number(row.get("regraded"))
    return (not row.get("refusals") and not row.get("execution_error") and row.get("outcome") != "refused"
            and row.get("input_kind") == "native_patch_replay" and score in {0.0, 1.0}
            and (expected is None or score == expected) and bool(row.get("job_dir")) and bool(row.get("matrix_receipt")))


def execute_jobs(jobs: list[ReplayJob], output: Path, gate: Gate, inputs: list[dict[str, Any]], partition: Any, cf: Any, runner: Any, *, evaluation_only: bool) -> tuple[list[dict[str, Any]], list[str]]:
    controls: dict[tuple[str, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    stop: list[str] = []
    for index, job in enumerate(jobs):
        control = controls.setdefault(job.key, {"valid": False, "nop": None, "reference": None, "rows": {}, "receipts": {}})
        try:
            if not control["valid"]:
                control_dir = output / "controls" / job.source["task"].replace("/", "__") / job.pin["repaired_digest"][7:]
                for role, expected in (("nop", 0.0), ("reference", 1.0)):
                    native, reference = native_replay(job, role, control_dir / role, gate, inputs, partition, cf, runner)
                    control["rows"][role], control["receipts"][role] = native, reference
                    control[role] = partition.number(native.get("regraded"))
                    write_json(control_dir / "receipt.json", {"engine": cf.ENGINE_VERSION, "grader_source": job.grader.to_json(), **control})
                    if not successful_native(native, partition, expected=expected):
                        stop = [f"{role}_control_invalid_or_infrastructure", *native.get("refusals", [])]
                        break
                control["valid"] = not stop and control["nop"] == 0.0 and control["reference"] == 1.0
                write_json(control_dir / "receipt.json", {"engine": cf.ENGINE_VERSION, "grader_source": job.grader.to_json(), **control})
            if not stop:
                directory = output / "items" / job.source["execution_id"] / job.pin["repaired_digest"][7:]
                native, reference = native_replay(job, "source", directory, gate, inputs, partition, cf, runner)
                row = envelope(job, cf, evaluation_only=evaluation_only, controls=control, native=native)
                row["native_result"] = reference
                row["control_receipt"] = str(output / "controls" / job.source["task"].replace("/", "__") / job.pin["repaired_digest"][7:] / "receipt.json")
                rows.append(row)
                write_json(directory / "receipt.json", row, exclusive=True)
                problems = partition.validate_receipt(job.source, job.pin, row)
                if not successful_native(native, partition) or problems:
                    stop = ["source_unmeasured_or_infrastructure", *native.get("refusals", []), *problems]
            else:
                rows.append(envelope(job, cf, evaluation_only=evaluation_only, controls=control, refusals=stop))
        except (Exception, KeyboardInterrupt) as exc:
            stop = [f"native_replay_stopped:{type(exc).__name__}:{exc}"]
            if len(rows) == index:
                rows.append(envelope(job, cf, evaluation_only=evaluation_only, controls=control, refusals=stop))
        write_json(output / "summary.json", {"rows": rows, "notice": NOTICE})
        if stop:
            for pending in jobs[index + 1:]:
                rows.append(envelope(pending, cf, evaluation_only=evaluation_only, controls=controls.get(pending.key, {}),
                                     refusals=["run_stopped_before_execution", *stop]))
            break
    return rows, stop


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, default=LANE / "m2/inventory-v1/inventory.jsonl")
    parser.add_argument("--variants", type=Path, default=LANE / "manifests/qualified-v1.json")
    parser.add_argument("--heldout", type=Path, help="Exact frozen all33 manifest; defaults to partition.py's canonical path")
    parser.add_argument("--records", type=Path, action="append", default=[], help="Additional native repair record file/directory (repeatable)")
    parser.add_argument("--execution", action="append", help=f"Exact original trial UUID; default {PILOT}")
    parser.add_argument("--batch", action="store_true", help="Explicitly allow more than one selected source execution")
    parser.add_argument("--evaluation-only", action="store_true", help="Use exact fixed-plan frozen B a1/a2 UUIDs, recorded reward0 or1; never exports training")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--reserve-usd", type=float)
    parser.add_argument("--approval", help="Parent's current recorded $4-lane authorization text, not a default approval")
    parser.add_argument("--budget", type=Path, help="Shared parent lane gate JSON, including reservations by other lane tools")
    args = parser.parse_args()
    try:
        args.execution = [str(UUID(value)) for value in (args.execution or [PILOT])]
        if len(set(args.execution)) != len(args.execution):
            raise ValueError("duplicate --execution selections are not additional executions")
        if len(args.execution) > 1 and not args.batch:
            raise ValueError("multiple --execution selections require explicit --batch")
        if args.max_usd is not None and (not math.isfinite(args.max_usd) or not 0 < args.max_usd <= 4.0):
            raise ValueError("--max-usd must be positive and no greater than the parent's total $4 lane")
        if args.reserve_usd is not None and (not math.isfinite(args.reserve_usd) or args.reserve_usd <= 0):
            raise ValueError("--reserve-usd must be a finite positive amount")
        if args.execute and (args.max_usd is None or args.reserve_usd is None or not (args.approval or "").strip() or args.budget is None):
            raise ValueError("paid execution requires explicit --max-usd --reserve-usd --approval --budget")
        raw_output = args.out.expanduser().absolute()
        if raw_output.is_symlink() or any(path.is_symlink() for path in raw_output.parents if path != Path("/")):
            raise ValueError("--out must not contain symlinks")
        args.out = raw_output.resolve()
        if not args.out.is_relative_to(LANE) or not re.search(r"-v[1-9][0-9]*$", args.out.name):
            raise ValueError("--out must be a new *-vN directory under this worktree's runs/envcheck-20261017/")
        if args.out.exists():
            raise ValueError("--out already exists; use a NEW version, never an implicit control/solution cache")
        if args.budget is not None:
            raw_budget = args.budget.expanduser().absolute()
            if raw_budget.is_symlink() or any(path.is_symlink() for path in raw_budget.parents if path != Path("/")):
                raise ValueError("--budget must not contain symlinks")
            args.budget = raw_budget.resolve()
            if not args.budget.is_relative_to(LANE) or args.budget.is_relative_to(args.out):
                raise ValueError("--budget must be a separate shared lane JSON under runs/envcheck-20261017/, outside this new --out")
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = arguments()
    args.out.mkdir(parents=True, exist_ok=False)
    started = now()
    rows: list[dict[str, Any]] = []
    jobs: list[ReplayJob] = []
    problems: list[str] = []
    gate: Gate | None = None
    initial_executions = 0
    partition = cf = runner = None
    plan: dict[str, Any] = {"schema": SCHEMA, "started_at": started, "root": str(ROOT), "out": str(args.out),
                            "execute": args.execute, "evaluation_only": args.evaluation_only, "notice": NOTICE,
                            "modal": MODAL, "concurrency": 1, "selected_executions": args.execution,
                            "training_exported": False, "new_model_executions": 0, "jobs": []}
    try:
        partition, cf, runner = dependencies()
        heldout, heldout_ref = partition.frozen_heldout((args.heldout or partition.DEFAULT_HELDOUT).expanduser().resolve())
        inventory_path, variants_path = args.inventory.expanduser().resolve(), args.variants.expanduser().resolve()
        inputs = [partition.file_ref(inventory_path), partition.file_ref(variants_path), heldout_ref,
                  partition.file_ref(Path(__file__)), partition.file_ref(Path(partition.__file__)),
                  partition.file_ref(Path(cf.__file__)), partition.file_ref(Path(runner.__file__))]
        evaluation = None
        fixed_plan = fixed_plan_ref = None
        if args.evaluation_only:
            evaluation = evaluation_helpers()
            fixed_plan, fixed_plan_ref, evaluation_heldout, evaluation_heldout_ref = evaluation.checked_plan()
            require_equal(evaluation_heldout, heldout, "fixed-plan frozen-heldout population")
            require_equal(evaluation_heldout_ref, heldout_ref, "fixed-plan frozen-heldout authority")
            inputs.extend(partition.file_ref(path) for path in (
                evaluation.FIXED_PLAN, Path(evaluation.__file__), Path(evaluation.c.__file__),
                Path(__file__).with_name("study_plan.py"), Path(__file__).with_name("analyze.py"),
            ))
            plan["fixed_evaluation_plan"] = {key: fixed_plan_ref[key] for key in ("path", "sha256")}
        plan["inputs"] = inputs
        inventory = partition.read_jsonl(inventory_path)
        source_map: dict[str, dict[str, Any]] = {}
        source_bindings: dict[str, dict[str, Any]] = {}
        for index, source in enumerate(inventory):
            if source["execution_id"] in source_map:
                raise ValueError("inventory repeats a native execution UUID")
            source_map[source["execution_id"]] = source
            source_bindings[source["execution_id"]] = {
                **inputs[0], "row_index": index, "row_sha256": partition.sha256(partition.canonical_bytes(source)),
            }
        pins = partition.variants(variants_path)
        from evallab.storage.paths import shared_checkout_root

        records = [ROOT / "library/task-variants", shared_checkout_root(ROOT) / "library/task-variants",
                   ARCHIVE / "port/lineage", *(path.expanduser().resolve() for path in args.records)]
        for execution_id in args.execution:
            if execution_id not in source_map:
                raise ValueError(f"selected execution is absent from the actual inventory: {execution_id}")
            source = source_map[execution_id]
            matching = [pin for pin in pins if pin["task"] == source["task"]]
            if not matching:
                raise ValueError(f"source has no exact admitted manifest grader: {source['task']}")
            for pin in matching:
                job = ReplayJob(source, pin)
                jobs.append(job)
                try:
                    require_equal(partition.parent_task(source["task"]), source["task"], "canonical original parent task")
                    require_equal(source.get("heldout"), source["task"] in heldout, "inventory frozen-heldout membership")
                    if source["task"] in heldout and not args.evaluation_only:
                        raise ValueError("frozen-heldout source requires the explicit --evaluation-only path")
                    evaluation_custody = None
                    if evaluation is not None:
                        evaluation_custody = evaluation_source(source, pin, source_bindings[execution_id],
                                                               fixed_plan, fixed_plan_ref, evaluation)
                        job.evaluation = evaluation
                    job.trial, job.source_custody = source_custody(source, partition, cf, evaluation=evaluation_custody)
                    job.grader, job.reference, job.grader_custody = grader_custody(pin, variants_path, records, partition, cf)
                except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
                    job.problems.append(f"input_custody_refused:{type(exc).__name__}:{exc}")
        ready = next((job for job in jobs if not job.problems), jobs[0])
        problems.extend(matrix_support(ready, runner))
        if os.environ.get("EVALLAB_NO_PROCESS_JOB") == "1":
            problems.append("native_post_processing_disabled_by_environment")
        if args.reserve_usd is not None and args.reserve_usd < cf.CLOUD_USD_PER_EXECUTION:
            problems.append(f"reservation_below_native_cf_floor:{cf.CLOUD_USD_PER_EXECUTION}")
        if args.budget is not None and args.max_usd is not None and args.reserve_usd is not None and (args.approval or "").strip():
            from evallab.regrade import CloudSpendGate

            gate = Gate(CloudSpendGate(args.approval, args.max_usd, args.reserve_usd), args.budget, args.out,
                        execute=args.execute)
            initial_executions = gate.native.executions
        if args.execute:
            # Version checking is local and free; every actual paid execution is
            # still gated exactly once by the native cf.replay -> Gate.charge call.
            cf.check_backend("modal", gate)
        plan.update(engine=cf.ENGINE_VERSION, jobs=[job.plan() for job in jobs], problems=problems,
                    planned_native_executions=len(jobs) + 2 * len({job.key for job in jobs}),
                    budget={"path": str(args.budget) if args.budget else None, "approval": args.approval,
                            "max_usd": args.max_usd, "reserve_usd": args.reserve_usd,
                            "initial_state": gate.to_json() if gate else None},
                    status="refused" if problems or any(job.problems for job in jobs) else "ready")
        planned = plan["planned_native_executions"]
        plan["budget"]["additional_reservation_usd"] = round(planned * args.reserve_usd, 4) if args.reserve_usd is not None else None
        plan["budget"]["fits_remaining_lane_fence"] = (
            (initial_executions + planned) * args.reserve_usd <= args.max_usd + 1e-9
            if gate else None
        )
        historical = LANE / "m2/native-pilot-v1/native-receipt.json"
        if historical.is_file():
            plan["historical_native_regrade_refusal"] = {**partition.file_ref(historical), "used_as_measurement": False}
        write_json(args.out / "plan.json", plan, exclusive=True)
        if args.execute and not problems and not any(job.problems for job in jobs):
            rows, problems = execute_jobs(jobs, args.out, gate, inputs, partition, cf, runner,
                                          evaluation_only=args.evaluation_only)
        else:
            for job in jobs:
                refusals = [*job.problems, *problems]
                if not args.execute:
                    refusals.insert(0, "execution_not_requested_free_plan")
                elif not refusals:
                    refusals.append("run_preflight_refused_before_execution")
                rows.append(envelope(job, cf, evaluation_only=args.evaluation_only, controls={}, refusals=refusals))
        measured = sum(successful_native(row, partition) and not partition.validate_receipt(job.source, job.pin, row)
                       for job, row in zip(jobs, rows, strict=True))
        success = args.execute and measured == len(jobs) and bool(jobs) and not problems
        status = "completed" if success else "planned" if not args.execute and plan["status"] == "ready" else "refused_or_stopped"
    except (Exception, KeyboardInterrupt) as exc:
        problems.append(f"driver_stopped:{type(exc).__name__}:{exc}")
        plan.update(status="refused", problems=problems, jobs=[job.plan() for job in jobs])
        if not (args.out / "plan.json").exists():
            write_json(args.out / "plan.json", plan, exclusive=True)
        # A late write/interruption must not replace earlier real observations
        # with an empty export or relabel their rewards as infrastructure zero.
        checkpoint = args.out / "summary.json"
        if not rows and checkpoint.is_file():
            try:
                rows = json.loads(checkpoint.read_text())["rows"]
            except (OSError, ValueError, KeyError, TypeError) as checkpoint_error:
                problems.append(f"summary_checkpoint_unreadable:{type(checkpoint_error).__name__}:{checkpoint_error}")
        if cf is not None:
            seen = {(row.get("execution_id"), row.get("grader_digest")) for row in rows}
            for job in jobs:
                if (job.source["execution_id"], job.pin["repaired_digest"]) not in seen:
                    rows.append(envelope(job, cf, evaluation_only=args.evaluation_only, controls={},
                                         refusals=[*job.problems, *problems]))
        measured = 0
        if partition is not None:
            by_key = {(job.source["execution_id"], job.pin["repaired_digest"]): job for job in jobs}
            for row in rows:
                job = by_key.get((row.get("execution_id"), row.get("grader_digest")))
                if job and successful_native(row, partition) and not partition.validate_receipt(job.source, job.pin, row):
                    measured += 1
        success, status = False, "refused_or_stopped"
    write_json(args.out / "summary.json", {"rows": rows, "notice": NOTICE})
    receipt = {
        "schema": SCHEMA, "started_at": started, "finished_at": now(), "status": status,
        "execute": args.execute, "notice": NOTICE, "plan": str(args.out / "plan.json"),
        "summary": str(args.out / "summary.json"), "selected_executions": args.execution,
        "requested_source_executions": len(args.execution),
        "retained_original_model_executions": len({job.source["execution_id"] for job in jobs if job.source_custody}),
        "new_model_executions": 0,
        "measured_source_rows": measured, "source_rows": len(rows), "problems": problems,
        "input_problems": [problem for job in jobs for problem in job.problems],
        "budget": {"path": str(args.budget) if args.budget else None, "gate": gate.to_json() if gate else None,
                   "initial_executions": initial_executions,
                   "native_executions_reserved_this_run": gate.charges_this_run if gate else 0,
                   "native_matrix_dispatch_attempts": gate.dispatches_this_run if gate else 0,
                   "charge_journal": str(gate.journal) if gate and gate.execute else None,
                   "notice": "Reservations are not invoices; parent reconciles total lane billing."},
        "training_exported": False, "evaluation_only": args.evaluation_only,
    }
    write_json(args.out / "receipt.json", receipt, exclusive=True)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if success or status == "planned" else 1


if __name__ == "__main__":
    raise SystemExit(main())
