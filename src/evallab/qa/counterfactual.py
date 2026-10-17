"""Counterfactual regrade for QA Trace T3: replay a stored pass against newer graders.

The *newer graders* of a MiMo code task (:func:`newer_graders`) are its
``mimo-clean-v6`` package (:func:`v6_grader`, the built row of the v6
manifest) and every test-repaired ``verifier-repair@*`` variant whose package
still hashes to its record (:func:`repaired_graders`). A repair derived from a
raw-grader package gets ``separate-verifier@10`` composed over it
(:func:`separate_over`) so raw repairs also grade in a separate verifier.

Retained diffs use the same model-free oracle-script matrix path as
verifier-repair validation (:func:`evallab.runner.run_matrix`): the script
applies the retained change, and the native executor owns setup, isolation,
artifact collection and grading. This is a new control, not a recovered
original workspace or trajectory. Without a diff, a retained snapshot uses
native Harbor ``trial regrade`` against the immutable source trial. Missing
artifacts and ATIF are never fabricated.

A grader is valid only when its nop scores 0 and its reference fix scores 1.
Missing reference evidence leaves the grader unvalidated, not valid. Oracle/nop
controls establish this execution gate, not semantic correctness. No model is
ever invoked. Local replays hold the host-wide Docker FIFO lock; cloud backends
are paid, so every cloud execution (controls included) is charged to a
:class:`evallab.regrade.CloudSpendGate` first and needs the lab's pinned Harbor.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import math
import re
import shlex
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evallab.regrade import CloudSpendGate

V6 = "mimo-clean-v6"
V10 = "separate-verifier@10"
REPAIR_PREFIX = "verifier-repair@"
MIMO_DATASET = "mimo-v2.6-rl"
SNAPSHOT_ARCHIVE = "workspace.tgz"
ENGINE_VERSION = "qa-trace-counterfactual/3"
#: One in-flight native replay per process; the host FIFO coordinates tabs.
DOCKER_SLOTS = threading.BoundedSemaphore(1)
#: Execution backends the replay engine can target. Cloud runs skip the local
#: Docker FIFO and the one-container process gate (both exist only to protect the
#: host). Daytona's MiMo egress lock refuses tasks with phase network policies
#: (every mimo-clean-v6 package), so Modal is the cloud backend for v6 replays.
ENVIRONMENTS = ("docker", "daytona", "modal")

#: Reserved per cloud execution (agent+verifier patch replay, or verifier-only
#: snapshot regrade). Measured $0.003-0.008 at MiMo spec (2 CPU, 8 GiB) on
#: Modal, 2026-10-16; rounded up so the gate errs toward stopping early.
CLOUD_USD_PER_EXECUTION = 0.01


def check_backend(environment: str, spend_gate: CloudSpendGate | None) -> None:
    """Refuse an unknown backend, or a paid one without a spend gate and the pinned Harbor."""
    from evallab.regrade import lab_harbor_problem

    if environment not in ENVIRONMENTS:
        raise ValueError(f"unsupported replay environment: {environment!r}")
    if environment == "docker":
        return
    if spend_gate is None:
        raise ValueError(f"{environment} replays are paid: pass a CloudSpendGate")
    problem = lab_harbor_problem()
    if problem is not None:
        raise ValueError(f"refusing {environment} replay: {problem}")


def _local_docker_gate(environment: str, label: str) -> contextlib.AbstractContextManager[Any]:
    if environment != "docker":
        return contextlib.nullcontext()
    from evallab.docker_lock import docker_slot

    stack = contextlib.ExitStack()
    stack.enter_context(DOCKER_SLOTS)
    stack.enter_context(docker_slot(label=label))
    return stack


@dataclass(frozen=True)
class GraderSelection:
    """One resolved selection route, retaining a raw repair's original selection."""

    transform: str
    variant_digest: str
    package: Path
    records: tuple[str, ...]
    manifest: str | None = None
    manifest_row: tuple[tuple[str, str], ...] = ()
    source: GraderSelection | None = None

    @property
    def label(self) -> str:
        return f"{self.transform}:{self.variant_digest.removeprefix('sha256:')[:12]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "transform": self.transform,
            "variant_digest": self.variant_digest,
            "package": str(self.package),
            "records": list(self.records),
            "manifest": self.manifest,
            "manifest_row": dict(self.manifest_row),
            "source": self.source.to_json() if self.source is not None else None,
        }


@dataclass(frozen=True)
class Grader:
    """One physical final grader, with every selection route retained as provenance."""

    task_name: str
    transform: str
    variant_digest: str
    package: Path
    record: str
    aliases: tuple[GraderSelection, ...] = ()

    @property
    def identity(self) -> tuple[str, str]:
        return self.task_name, self.variant_digest

    @property
    def identity_json(self) -> dict[str, str]:
        return {"task_name": self.task_name, "variant_digest": self.variant_digest}

    @property
    def cache_key(self) -> str:
        task_digest = hashlib.sha256(self.task_name.encode()).hexdigest()
        return f"{task_digest}--{self.variant_digest.removeprefix('sha256:')}"

    @property
    def selections(self) -> tuple[GraderSelection, ...]:
        return self.aliases or (
            GraderSelection(self.transform, self.variant_digest, self.package, (self.record,)),
        )

    @property
    def label(self) -> str:
        return f"{self.transform}:{self.variant_digest.removeprefix('sha256:')[:12]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "identity": self.identity_json,
            "task_name": self.task_name,
            "transform": self.transform,
            "variant_digest": self.variant_digest,
            "package": str(self.package),
            "record": self.record,
            "aliases": [alias.to_json() for alias in self.selections],
        }


def _slug(task_name: str) -> str:
    return task_name.replace("/", "__")


def _digest(package: Path) -> str:
    from evallab.task_variants import task_directory_digest

    return task_directory_digest(package)


def _verify_package(package: Path, expected: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError(f"invalid full grader package digest: {expected!r}")
    if not (package / "task.toml").is_file():
        raise ValueError(f"grader package missing: {package}")
    actual = _digest(package)
    if actual != expected:
        raise ValueError(f"grader package digest mismatch: {package}: {actual} != {expected}")


def canonical_graders(candidates: Sequence[Grader]) -> list[Grader]:
    """Group only resolved full packages; verify every claimed path before execution.

    Equivalent copies are allowed, but each must reproduce its claimed digest.
    Repeated selections remain repeated provenance, never repeated measurements.
    """
    claims: dict[Path, tuple[str, str]] = {}
    grouped: dict[tuple[str, str], list[Grader]] = {}
    references: dict[str, tuple[str, str]] = {}
    for grader in candidates:
        grouped.setdefault(grader.identity, []).append(grader)
        packages = [(grader.package, grader.variant_digest)]
        for alias in grader.selections:
            if alias.variant_digest != grader.variant_digest:
                raise ValueError(f"conflicting final grader selection: {alias.label}")
        selections = list(grader.selections)
        while selections:
            alias = selections.pop()
            packages.append((alias.package, alias.variant_digest))
            if alias.source is not None:
                selections.append(alias.source)
            for reference in alias.records:
                identity = (grader.task_name, alias.variant_digest)
                if reference in references and references[reference] != identity:
                    raise ValueError(f"conflicting grader record claims: {reference}")
                references[reference] = identity
        for package, digest in packages:
            path = package.resolve()
            identity = (grader.task_name, digest)
            if path in claims and claims[path] != identity:
                raise ValueError(f"conflicting grader package claims: {path}")
            claims[path] = identity
    for package, (_, digest) in claims.items():
        _verify_package(package, digest)

    out = []
    for _, graders in sorted(grouped.items()):
        first = min(graders, key=lambda g: (g.transform, str(g.package.resolve()), g.record))
        aliases = sorted(
            (alias for grader in graders for alias in grader.selections),
            key=lambda alias: json.dumps(alias.to_json(), sort_keys=True),
        )
        out.append(dataclasses.replace(first, aliases=tuple(aliases)))
    return out


def is_separate(package: Path) -> bool:
    """Whether the package grades in its own verifier container from a snapshot."""
    toml = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    return toml.get("verifier", {}).get("environment_mode") == "separate"


def _v6_rows(manifest: Path) -> dict[str, dict[str, str]]:
    from evallab.mimo_clean import load_manifest

    rows: dict[str, dict[str, str]] = {}
    for row in load_manifest(manifest):
        task_id = row["task_id"]
        if task_id in rows and rows[task_id] != row:
            raise ValueError(f"conflicting grader manifest rows: {manifest}#{task_id}")
        rows[task_id] = row
    return rows


def v6_grader(task_name: str, repo_root: Path, *, manifest: Path | None = None) -> Grader | None:
    """The task's ``mimo-clean-v6`` package (built row of the v6 manifest).

    A selected built package must exist and reproduce ``final_digest``.
    """
    from evallab.mimo_clean import MANIFEST_V6_REL
    from evallab.storage.paths import shared_checkout_root

    dataset, _, base = task_name.partition("/")
    manifest = manifest or repo_root / MANIFEST_V6_REL
    if not manifest.is_absolute():
        manifest = repo_root / manifest
    if dataset != MIMO_DATASET or not manifest.is_file():
        return None
    row = _v6_rows(manifest).get(base)
    if row is None or row["status"] != "built":
        return None
    package = shared_checkout_root(repo_root) / row["package_path"]
    _verify_package(package, row["final_digest"])
    return Grader(
        task_name=task_name,
        transform=V6,
        variant_digest=row["final_digest"],
        package=package,
        record=f"{manifest}#{base}",
        aliases=(
            GraderSelection(
                V6,
                row["final_digest"],
                package,
                (f"{manifest}#{base}",),
                manifest=str(manifest),
                manifest_row=tuple(sorted(row.items())),
            ),
        ),
    )


def _records(task_name: str, repo_root: Path) -> list[tuple[Path, Any]]:
    from evallab.task_variants import RECORDS_DIRNAME, VariantRecord

    folder = repo_root / RECORDS_DIRNAME / _slug(task_name)
    return [
        (path, VariantRecord.model_validate_json(path.read_text(encoding="utf-8")))
        for path in sorted(folder.glob("*.json"))
    ]


def repair_records(paths: list[Path]) -> list[tuple[Path, Any]]:
    """``verifier-repair@*`` lineage records under ``paths`` (files or directories).

    Generated records are never committed (``.gitignore``), so a repair landed
    in another checkout is passed in by path.
    """
    from evallab.task_variants import VariantRecord

    files = [f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob("*.json")))]
    out = []
    for path in files:
        try:
            record = VariantRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue  # not a lineage record
        if record.transform.startswith(REPAIR_PREFIX):
            out.append((path, record))
    return out


def separate_over(parent: Path, task_name: str, repo_root: Path, *, via: str) -> Grader | None:
    """Find or derive ``separate-verifier@10`` over a raw-grader ``parent``.

    A test repair derived from a raw MiMo package grades in the agent's own
    container, which a stored pass no longer has; composing ``@10`` keeps the
    repaired tests and grades the snapshot instead. The derivation is
    deterministic, so a throwaway derive names the digest and an existing
    shared package that hashes to it is reused (its record may live in another
    checkout). ``via`` names the chain in the grader's transform.
    """
    from evallab.mimo_clean import derive_marker
    from evallab.separate_verifier import derive_separate_verifier_v10
    from evallab.task_variants import RECORDS_DIRNAME, VariantExistsError, default_variants_root

    marker = derive_marker(
        (parent / "tests" / "test.patch").read_text(errors="replace"),
        task_name.partition("/")[2],
    )
    with tempfile.TemporaryDirectory(prefix="qa-trace-v10-") as scratch:
        planned = derive_separate_verifier_v10(
            parent, marker=marker, repo_root=Path(scratch), variants_root=Path(scratch) / "store"
        )
    digest12 = planned.variant_digest.removeprefix("sha256:")[:12]
    package = default_variants_root(repo_root) / _slug(task_name) / digest12
    if not (package / "task.toml").is_file():
        # Another worker may derive the same package first; the digest check decides.
        with contextlib.suppress(VariantExistsError):
            derive_separate_verifier_v10(parent, marker=marker, repo_root=repo_root)
    _verify_package(package, planned.variant_digest)
    return Grader(
        task_name=task_name,
        transform=f"{via}>{V10}",
        variant_digest=planned.variant_digest,
        package=package,
        record=str(RECORDS_DIRNAME / _slug(task_name) / f"{digest12}.json"),
    )


def repaired_graders(
    task_name: str, repo_root: Path, extra: Sequence[tuple[Path, Any]] = ()
) -> list[Grader]:
    """Test-repaired graders for the task: committed records plus ``extra``.

    A repair whose package is a separate verifier (e.g. derived from a v6
    package) grades as is; a raw-grader repair gets ``@10`` composed over it.
    """
    from evallab.task_variants import default_variants_root

    found = [
        (path, rec)
        for path, rec in _records(task_name, repo_root)
        if rec.transform.startswith(REPAIR_PREFIX)
    ]
    found += [(path, rec) for path, rec in extra if rec.task_name == task_name]
    out: list[Grader] = []
    for path, record in found:
        digest12 = record.variant_digest.removeprefix("sha256:")[:12]
        package = default_variants_root(repo_root) / _slug(task_name) / digest12
        _verify_package(package, record.variant_digest)
        if is_separate(package):
            grader = Grader(task_name, record.transform, record.variant_digest, package, str(path))
        else:
            grader = separate_over(package, task_name, repo_root, via=record.transform)
            if grader is not None:
                grader = dataclasses.replace(
                    grader,
                    record=f"{path} > {grader.record}",
                    aliases=(
                        GraderSelection(
                            grader.transform,
                            grader.variant_digest,
                            grader.package,
                            (grader.record,),
                            source=GraderSelection(
                                record.transform,
                                record.variant_digest,
                                package,
                                (str(path),),
                            ),
                        ),
                    ),
                )
        if grader is not None:
            out.append(grader)
    return out


def newer_graders(
    task_name: str,
    repo_root: Path,
    repairs: Sequence[tuple[Path, Any]] = (),
    *,
    manifest: Path | None = None,
) -> list[Grader]:
    """The task's final graders, grouped after every selection and composition."""
    v6 = v6_grader(task_name, repo_root, manifest=manifest)
    return canonical_graders(([v6] if v6 else []) + repaired_graders(task_name, repo_root, repairs))


def strip_binary_artifacts(diff: str) -> tuple[str, list[str]]:
    """Drop whole per-file stanzas for paths ``separate-verifier@10`` drops from a
    kept change (``__pycache__``, compiled objects); returns (kept diff, dropped paths).
    """
    from evallab.separate_verifier import is_binary_artifact_path

    kept: list[str] = []
    dropped: list[str] = []
    for stanza in re.split(r"(?m)^(?=diff --git )", diff):
        header = re.match(r"diff --git a/(\S+) b/(\S+)", stanza)
        if header and any(is_binary_artifact_path(p) for p in header.groups()):
            dropped.append(header.group(2))
        else:
            kept.append(stanza)
    return "".join(kept), dropped


def _score(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _outcome(recorded: float | None, regraded: float | None) -> str:
    if recorded is None or regraded is None:
        return "refused"
    if recorded < 1.0 and regraded <= recorded:
        return "not_a_pass"
    if recorded == regraded:
        return "stays"
    return "flips" if regraded < recorded else "loosened"


def _patch_script(diff: str, workdir: str) -> str:
    script = f"#!/bin/bash\nset -euo pipefail\ncd {shlex.quote(workdir)}\n"
    if diff:
        encoded = base64.b64encode(diff.encode("utf-8")).decode("ascii")
        script += (
            f"printf %s {shlex.quote(encoded)} | base64 -d | git apply --whitespace=nowarn -\n"
        )
    return script


def _replay_patch(
    diff: str,
    grader: Grader,
    *,
    repo_root: Path,
    matrix_runner: Any = None,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
) -> dict[str, Any]:
    """Run the recorded change as a model-free native oracle-script control.

    This is a new patch replay, not a recovered original workspace or ATIF.
    The canonical executor owns setup, isolation, collection and native jobs.
    A cloud replay is charged to ``spend_gate`` before it runs.
    """
    from evallab.execution_contracts import new_ulid
    from evallab.registry import compute_task_digests
    from evallab.regrade import SpendCapReached
    from evallab.runner import run_matrix
    from evallab.schemas import ExperimentMatrix, MatrixRun

    if environment != "docker" and spend_gate is not None:
        try:
            spend_gate.charge()
        except SpendCapReached:
            return {
                "regraded": None,
                "input_kind": "native_patch_replay",
                "refusals": ["spend_cap_reached"],
                "environment": environment,
            }

    config = tomllib.loads((grader.package / "task.toml").read_text(encoding="utf-8"))
    script = _patch_script(diff, config["environment"]["workdir"])
    script_digest = hashlib.sha256(script.encode()).hexdigest()
    root = repo_root / "runs" / "qa-trace-regrade"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script_path = scripts / f"{script_digest}.sh"
    # Different graders can replay the same content-addressed script at once.
    # Never truncate the path a sibling native execution is already reading.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=scripts, prefix=f".{script_digest}.", delete=False
        ) as staged:
            temporary = Path(staged.name)
            staged.write(script)
        temporary.replace(script_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    ident = new_ulid()
    name = f"qa-replay-{ident.lower()}"
    digests = compute_task_digests(grader.package)
    matrix = ExperimentMatrix(
        matrix_id=ident,
        name=name,
        hypothesis="Replay a retained change against a pinned counterfactual grader.",
        benchmark_family="qa-trace-regrade",
        task_id=grader.task_name.rsplit("/", 1)[-1],
        task=str(grader.package),
        task_package_digest=digests.package,
        verifier_digest=digests.verifier,
        jobs_dir="runs",
        environment=environment,
        concurrency=1,
        timeout_seconds=3600,
        runs=[
            MatrixRun(
                name=name,
                agent="oracle",
                solution=script_path.relative_to(repo_root).as_posix(),
            )
        ],
    )
    execution = (matrix_runner or run_matrix)(matrix, root=repo_root)
    result = execution.results[0]
    rewards = result.get("rewards", [])
    score = (
        _score(rewards[0]) if len(rewards) == 1 and result["status"] in {"ok", "mismatch"} else None
    )
    return {
        "regraded": score,
        "input_kind": "native_patch_replay",
        "matrix_receipt": str(execution.receipt_path),
        "job_dir": str(repo_root / "runs" / name),
        "refusals": [] if score is not None else ["patch_replay_failed"],
        "environment": environment,
        "execution_error": result.get("error"),
        "script_sha256": script_digest,
    }


def source_digest(trial_dir: Path) -> str:
    """Bind cached replays to retained source bytes, not a trial directory name."""
    from evallab.separate_verifier import SNAP_DIR, TRAJECTORY_ARTIFACT

    digest = hashlib.sha256()
    for relative in (
        "result.json",
        "config.json",
        "agent/trajectory.json",
        "verifier/agent.diff",
        "artifacts/manifest.json",
        f"artifacts{SNAP_DIR}/{SNAPSHOT_ARCHIVE}",
        f"artifacts{TRAJECTORY_ARTIFACT}",
    ):
        digest.update(relative.encode())
        path = trial_dir / relative
        if path.is_file():
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        else:
            digest.update(b"absent")
    return digest.hexdigest()


def replay(
    trial_dir: Path,
    grader: Grader,
    work: Path,
    *,
    diff_text: str | None = None,
    repo_root: Path,
    matrix_runner: Any = None,
    runner: Any = subprocess.run,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
) -> dict[str, Any]:
    """Regrade a recorded snapshot or natively replay its recorded diff.

    Missing source artifacts are never invented. Diff-only inputs become new,
    explicitly labelled native oracle-script controls, not patched trial records.
    A cloud ``environment`` needs ``spend_gate``; each execution is charged to it.
    """
    from evallab.execution_contracts import new_ulid
    from evallab.heldout_verifier import scan_agent_diff
    from evallab.regrade import (
        CLOUD_REGRADE_ENVIRONMENTS,
        LOCAL_REGRADE_ENVIRONMENTS,
        read_reward_observation,
        regrade_trial,
    )
    from evallab.separate_verifier import SNAP_DIR

    canonical_graders([grader])
    check_backend(environment, spend_gate)
    observation = read_reward_observation(trial_dir)
    recorded = _score((observation.rewards or {}).get("reward")) if observation else None
    row: dict[str, Any] = {
        "grader": grader.label,
        "grader_identity": grader.identity_json,
        "trial": str(trial_dir),
        "recorded": recorded,
    }
    if diff_text is None and recorded is None:
        return {**row, "outcome": "refused", "refusals": ["source_reward_unavailable"]}
    snap = trial_dir / "artifacts" / SNAP_DIR.lstrip("/") / SNAPSHOT_ARCHIVE
    diff_path = trial_dir / "verifier" / "agent.diff"
    snapshot_backends = {*LOCAL_REGRADE_ENVIRONMENTS, *CLOUD_REGRADE_ENVIRONMENTS}
    with _local_docker_gate(environment, f"qa-trace-regrade {grader.label}"):
        if diff_text is None and not diff_path.is_file() and snap.is_file():
            if environment not in snapshot_backends:
                return {
                    **row,
                    "outcome": "refused",
                    "refusals": ["snapshot_regrade_backend_unsupported"],
                }
            receipt = regrade_trial(
                trial_dir=trial_dir,
                task_dir=grader.package,
                trials_dir=work / "regrades" / new_ulid().lower(),
                environment=environment,
                runner=runner,
                spend_gate=spend_gate,
            )
            score = (
                _score((receipt.regraded.rewards or {}).get("reward"))
                if receipt.regraded and not receipt.refused
                else None
            )
            row.update(
                environment=environment,
                regraded=score,
                input_kind="recorded_snapshot",
                regrade_dir=receipt.regrade_trial_dir,
                refusals=[str(code.value) for code in receipt.refusals],
                harbor_output_tail=(
                    (receipt.execution.stdout_tail + receipt.execution.stderr_tail)[-2000:]
                    if receipt.execution and receipt.refusals
                    else None
                ),
            )
        else:
            if diff_text is None:
                if not diff_path.is_file():
                    return {
                        **row,
                        "outcome": "refused",
                        "refusals": ["source_patch_unavailable"],
                    }
                diff_text = diff_path.read_text(encoding="utf-8")
            kept, dropped = strip_binary_artifacts(diff_text)
            work.mkdir(parents=True, exist_ok=True)
            checked = work / "agent.replay.diff"
            checked.write_text(kept, encoding="utf-8")
            problems, _ = scan_agent_diff(str(checked))
            if problems:
                return {**row, "outcome": "refused", "refusals": ["diff_unreplayable"]}
            row.update(
                _replay_patch(
                    kept,
                    grader,
                    repo_root=repo_root,
                    matrix_runner=matrix_runner,
                    environment=environment,
                    spend_gate=spend_gate,
                )
            )
            row["dropped_binary_artifacts"] = dropped
    row["outcome"] = _outcome(recorded, row.get("regraded"))
    return row


def reference_patch(task_name: str, repo_root: Path) -> str | None:
    """The validated reference fix (oracle pass + nop fail) for a MiMo task, if any."""
    from evallab.mimo_clean import REFERENCE_INDEX_REL, load_reference_index, oracle_info_from_index

    index_path = repo_root / REFERENCE_INDEX_REL
    if not index_path.is_file() or not task_name.startswith(f"{MIMO_DATASET}/"):
        return None
    info = oracle_info_from_index(
        task_name.partition("/")[2], load_reference_index(index_path), repo_root=repo_root
    )
    return info.patch_bytes.decode("utf-8", "replace") if info else None


def cached_controls(grader: Grader, work: Path, *, repo_root: Path) -> dict[str, Any] | None:
    """The grader's stored controls, if they still match its grader, engine and reference."""
    cache = work / "controls.json"
    if not cache.is_file():
        return None
    patch = reference_patch(grader.task_name, repo_root)
    cached = json.loads(cache.read_text())
    identity = _controls_identity(grader, patch)
    if (
        all(cached.get(k) == value for k, value in identity.items())
        and cached.get("nop") is not None
        and (patch is None or cached.get("reference") is not None)
    ):
        return cached
    return None


def control_executions(grader: Grader, work: Path, *, repo_root: Path) -> int:
    """Executions :func:`grader_controls` would start: nop plus reference, unless cached."""
    if cached_controls(grader, work, repo_root=repo_root) is not None:
        return 0
    return 1 + (reference_patch(grader.task_name, repo_root) is not None)


def _controls_identity(grader: Grader, patch: str | None) -> dict[str, Any]:
    return {
        "grader_identity": grader.identity_json,
        "engine": ENGINE_VERSION,
        "reference_sha256": hashlib.sha256(patch.encode()).hexdigest()
        if patch is not None
        else None,
    }


def grader_controls(
    grader: Grader,
    shell_trial: Path,
    work: Path,
    *,
    repo_root: Path,
    runner: Any = subprocess.run,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
) -> dict[str, Any]:
    """Nop/reference execution gate, bound to grader, engine and reference bytes.

    Missing references or failed executions remain unvalidated. Results are
    never treated as evidence of semantic correctness.
    """
    canonical_graders([grader])
    cached = cached_controls(grader, work, repo_root=repo_root)
    if cached is not None:
        return cached
    cache = work / "controls.json"
    patch = reference_patch(grader.task_name, repo_root)
    identity = _controls_identity(grader, patch)
    nop = replay(
        shell_trial,
        grader,
        work / "nop",
        diff_text="",
        repo_root=repo_root,
        runner=runner,
        environment=environment,
        spend_gate=spend_gate,
    )
    reference = (
        replay(
            shell_trial,
            grader,
            work / "reference",
            diff_text=patch,
            repo_root=repo_root,
            runner=runner,
            environment=environment,
            spend_gate=spend_gate,
        )
        if patch is not None
        else None
    )
    nop_score = _score(nop.get("regraded"))
    reference_score = _score(reference.get("regraded")) if reference else None
    controls = {
        **identity,
        "nop": nop_score,
        "reference": reference_score,
        "reference_available": patch is not None,
        "valid": nop_score == 0.0 and reference_score == 1.0,
        "rows": {"nop": nop, "reference": reference},
    }
    work.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(controls, indent=1, default=str))
    return controls
