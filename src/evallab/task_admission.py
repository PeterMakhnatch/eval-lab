"""Fail-closed task admission gate: static scan plus $0 controls plus cheat ladder.

``evallab tasks admit`` runs one ordered gate over a task package directory and
writes a digest-bound admission record. The home is ``tasks`` (beside
``variant-status`` and ``stability-run``) because admission consumes those two
paths instead of duplicating them:

* the static step reuses :func:`evallab.reward_hack.scan_package`;
* the oracle/nop controls reuse :func:`evallab.task_stability.run_stability_jobs`;
* the cheat step reuses the ``evallab cheat run`` lane
  (:mod:`evallab.cheat` verdict rule: cracked iff reward >= 1.0);
* lineage variants reuse the existing ``evallab.task_variant/v1`` ledger via
  :func:`evallab.task_variants.append_status_evidence` — no second ledger.

Step order is cheapest-first (offline scan, then Docker controls, then the
12-attack ladder) with early stop on the first non-passing step. Every step
records its command, job directory, reward(s), and duration; the record always
lands, even for infrastructure failures.

Verdicts: ``admitted`` (every step passed), ``rejected`` (a step proved the
task bad: wrong control score or a cracked attack), ``not_admitted``
(infrastructure broke before proof either way: Harbor errors, missing jobs,
unreadable rewards), ``unproven`` (no oracle reference covers the task, so
solvability cannot be shown — distinct from infra). Fail-closed means infra
never silently passes.

Oracle for solution-less tasks uses the existing HAR-191 history-oracle
reference, never a new store: the committed ``oracle_sweep.csv`` projection
selects the proven row (``oracle:pass+nop:fail``), the recorded sweep receipt
validates the solution patch (sha-pinned, recorded 1.0/0.0 arms), and the
gate stages the package, plants a solve.sh applying that patch (the
mtime-validation mechanism), and grades the fixed tree with the oracle agent
for fresh Docker proof. ``--reference`` overrides with a receipt or raw patch
(``none`` skips). The step records the full provenance plus exact/task-level
digest binding. Lineage resolution is targeted (one record file by slug plus
package digest), never a corpus scan.

Static-scan policy: the scan step passes when the scan executes and records
its ledger. Non-empty findings do not veto admission, because the V1-V8 ledger
states its own limits (a finding is a claim, never an exploitation proof; a
clean scan is not a certificate) and on the current shared-container corpus
the V1/V3/V7/V8 findings are architecture-constant — the fully validated
``mtime-normalize@1`` heads carry the same ledger as their unhardened
parents. The dynamic proofs (oracle/nop/cheat) are the vetoes; findings stay
first-class evidence in the record for later policy or human review.

The step list is data-driven: :func:`default_steps` owns the order and
``STEP_RUNNERS`` maps each step kind to its runner. A later verifier-mutation
step lands as one new registry entry plus one new :class:`StepDef` — no gate
logic changes. No mutation step exists yet.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from evallab.cheat import ATTACKS
from evallab.schemas import ContractModel

if TYPE_CHECKING:
    from evallab.task_variants import VariantRecord

#: Durable admission-record schema tag.
ADMISSION_SCHEMA = "evallab.task_admission/v1"

#: Gate implementation version pinned in every record (step-list revisions bump this).
GATE_VERSION = "tasks.admit/v1"

AdmissionVerdict = Literal["admitted", "rejected", "not_admitted", "unproven"]
StepStatus = Literal["pass", "fail", "infra", "unproven"]


class AdmissionError(RuntimeError):
    """The gate broke before it could prove anything (infra class)."""


# --------------------------------------------------------------------------- #
# Step list (data-driven; append-only order)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StepDef:
    """One gate step: a name plus the kind-owned parameters it needs."""

    name: str
    kind: str
    agent: str | None = None
    expect: float | None = None
    attack: str | None = None


def default_steps() -> list[StepDef]:
    """The ordered gate: scan, oracle, nop, then one step per ladder attack."""
    return [
        StepDef(name="static-scan", kind="static-scan"),
        StepDef(name="oracle", kind="control", agent="oracle", expect=1.0),
        StepDef(name="nop", kind="control", agent="nop", expect=0.0),
        *(StepDef(name=f"cheat:{attack}", kind="cheat", attack=attack) for attack in ATTACKS),
    ]


# --------------------------------------------------------------------------- #
# Persisted record (``evallab.task_admission/v1``)
# --------------------------------------------------------------------------- #


class AdmissionFinding(ContractModel):
    """One static-ledger claim, copied verbatim from ``hack scan``."""

    flaw_class: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    title: str = Field(min_length=1)
    evidence: str = ""


class AdmissionStepRecord(ContractModel):
    """One executed step: command, job dir, reward(s), duration, outcome."""

    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    command: list[str] = Field(default_factory=list)
    job_dir: str | None = None
    rewards: list[float | None] = Field(default_factory=list)
    duration_s: float = Field(ge=0.0)
    status: StepStatus
    detail: str = ""
    findings: list[AdmissionFinding] = Field(default_factory=list)
    reference: str = ""


class AdmissionRecord(ContractModel):
    """The digest-bound admission record for one task package."""

    schema_: Literal["evallab.task_admission/v1"] = Field(
        alias="schema", default="evallab.task_admission/v1"
    )
    gate: str = Field(default=GATE_VERSION)
    task: str = Field(min_length=1)
    package_digest: str | None = None
    harbor_digest: str | None = None
    variant_record: str | None = None
    created_by: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    steps: list[AdmissionStepRecord] = Field(default_factory=list)
    skipped_steps: list[str] = Field(default_factory=list)
    verdict: AdmissionVerdict
    failing_steps: list[str] = Field(default_factory=list)
    failed_attacks: list[str] = Field(default_factory=list)
    unproven_steps: list[str] = Field(default_factory=list)
    duration_s: float = Field(ge=0.0)


def record_filename(package_digest: str | None) -> str:
    """Digest-bound filename: ``admission-<digest12>.json``."""
    short = package_digest.split(":", 1)[1][:12] if package_digest else "unknown"
    return f"admission-{short}.json"


# --------------------------------------------------------------------------- #
# Runner seams (injectable for deterministic tests)
# --------------------------------------------------------------------------- #


@dataclass
class ScanOutcome:
    """What the static step needs: findings plus package digests."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    package_digest: str | None = None
    harbor_digest: str | None = None
    command: list[str] = field(default_factory=list)


@dataclass
class ControlOutcome:
    """What a control step needs: job evidence plus observed rewards."""

    job_dir: Path | None = None
    returncode: int | None = None
    rewards: list[float | None] = field(default_factory=list)
    command: list[str] = field(default_factory=list)


@dataclass
class CheatOutcome:
    """What a cheat step needs: job evidence plus the ladder reward."""

    job_dir: Path | None = None
    returncode: int | None = None
    reward: float | None = None
    verdict: str | None = None
    command: list[str] = field(default_factory=list)


@dataclass
class StepContext:
    """Shared execution context threaded through every step runner."""

    task_dir: Path
    jobs_dir: Path
    repo_root: Path
    repo_src: Path
    job_prefix: str
    slug: str
    repeat_n: int = 3
    timeout_seconds: int = 600
    scan: Callable[[Path], ScanOutcome] | None = None
    control: Callable[..., ControlOutcome] | None = None
    cheat: Callable[..., CheatOutcome] | None = None
    reference: Callable[..., OracleReference | None] | None = None
    reference_arg: str = "auto"
    sweep_csv: Path | None = None


def default_scan(task_dir: Path) -> ScanOutcome:
    """Run the offline V1-V8 ledger in-process (no Docker, no spend)."""
    from evallab.reward_hack import scan_package

    command = ["evallab", "hack", "scan", str(task_dir), "--json"]
    scan = scan_package(task_dir)
    return ScanOutcome(
        findings=[
            {
                "flaw_class": finding.flaw_class,
                "severity": finding.severity,
                "title": finding.title,
                "evidence": finding.evidence,
            }
            for finding in scan.findings
        ],
        package_digest=scan.package_digest,
        harbor_digest=scan.harbor_digest,
        command=command,
    )


def default_control(
    *,
    task_dir: Path,
    agent: str,
    job_name: str,
    jobs_dir: Path,
    repo_src: Path,
    repeat_n: int,
) -> ControlOutcome:
    """Run one $0 oracle/nop control through the stability-job path."""
    from evallab.task_stability import run_stability_jobs

    outcomes = run_stability_jobs(
        tasks=[task_dir],
        job_prefix=job_name,
        jobs_dir=jobs_dir,
        repo_src=repo_src,
        agent=agent,
        repeat_n=repeat_n,
        n_concurrent=1,
    )
    outcome = outcomes[0]
    job_dir = jobs_dir / outcome["job_name"]
    rewards: list[float | None] = [
        reward for trial in outcome["trials"] for reward in trial["rewards"]
    ]
    return ControlOutcome(
        job_dir=job_dir if job_dir.is_dir() else None,
        returncode=outcome["returncode"],
        rewards=rewards,
        command=[str(part) for part in outcome["argv"]],
    )


def default_cheat(
    *,
    task_dir: Path,
    attack: str,
    job_name: str,
    jobs_dir: Path,
    repo_root: Path,
    timeout_seconds: int,
) -> CheatOutcome:
    """Run one ladder attack as a single-trial cheat job (local Docker, $0)."""
    import os
    import threading

    from evallab.cheat import build_verdicts, harbor_version, parse_attack_selection
    from evallab.execution_contracts import CHEAT_AGENT, CHEAT_ATTACKS_ENV_VAR, RunRequest
    from evallab.harbor_view import installed_harbor_version
    from evallab.queue import Executor

    version = installed_harbor_version()
    if version is None or version < (0, 24):
        raise AdmissionError(
            "evallab tasks admit needs Harbor >= 0.24 on PATH (uv sync --frozen --extra laminar)"
        )
    selected = parse_attack_selection(attack)
    command = [
        "evallab",
        "cheat",
        "run",
        "--task",
        str(task_dir),
        "--name",
        job_name,
        "--jobs-dir",
        str(jobs_dir),
        "--attacks",
        ",".join(selected),
        "--attempts",
        "1",
    ]
    request = RunRequest(
        task=task_dir,
        agent=CHEAT_AGENT,
        name=job_name,
        jobs_dir=jobs_dir,
        environment="docker",
        model=None,
        concurrency=1,
        attempts=1,
        timeout_seconds=timeout_seconds,
        allow_billable=False,
    )
    lock = threading.Lock()
    with lock:
        previous = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
        os.environ[CHEAT_ATTACKS_ENV_VAR] = ",".join(selected)
        try:
            job_dir = Executor.from_repo(repo_root).execute_direct(request)
        finally:
            if previous is None:
                os.environ.pop(CHEAT_ATTACKS_ENV_VAR, None)
            else:
                os.environ[CHEAT_ATTACKS_ENV_VAR] = previous
    payload = build_verdicts(job_dir, harbor_rev=harbor_version())
    trials = payload["trials"]
    if len(trials) != 1:
        raise AdmissionError(
            f"cheat job {job_dir} produced {len(trials)} trials, expected exactly 1"
        )
    return CheatOutcome(
        job_dir=job_dir,
        returncode=0,
        reward=trials[0]["reward"],
        verdict=trials[0]["verdict"],
        command=command,
    )


# --------------------------------------------------------------------------- #
# Oracle reference (HAR-191 history-oracle path for solution-less tasks)
# --------------------------------------------------------------------------- #

#: Sweep label that counts as proven solvability (the MiMo "proven" set).
ORACLE_PROVEN_LABEL = "oracle:pass+nop:fail"

#: Committed sweep projection, repo-rooted by the gate (never a new store).
DEFAULT_SWEEP_CSV = Path("research/experiments/python-task-ledger/oracle_sweep.csv")


class ReferenceUnproven(Exception):
    """No oracle reference covers this task: distinct from infra, never silent."""


@dataclass
class OracleReference:
    """A proven reference fix plus its digest-bound provenance."""

    task_id: str
    label: str
    fix_commit: str
    receipt_path: str
    receipt_run_digest: str
    patch_sha256: str
    patch_bytes: bytes
    recorded_oracle_reward: float | None
    recorded_nop_reward: float | None


def _task_id_of(task_dir: Path) -> str:
    """Bare task id (``format-code-task-002402``) from the package manifest."""
    import tomllib

    try:
        name = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        name = (name.get("task") or {}).get("name", "")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReferenceUnproven(f"cannot read [task].name from {task_dir}: {exc}") from exc
    bare = str(name).strip().split("/")[-1]
    if not bare:
        raise ReferenceUnproven(f"task package has no [task].name: {task_dir}")
    return bare


def _task_workdir(task_dir: Path) -> str:
    """Absolute container workdir from the package manifest."""
    import tomllib

    try:
        config = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        workdir = (config.get("environment") or {}).get("workdir", "")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReferenceUnproven(f"cannot read [environment].workdir: {exc}") from exc
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise ReferenceUnproven(f"task package has no absolute [environment].workdir: {task_dir}")
    return workdir


def _reference_from_receipt(path: Path, *, task_id: str | None = None) -> OracleReference:
    """Validate a HAR-191 sweep receipt into a reference (provenance, not trust)."""
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdmissionError(f"cannot read reference receipt {path}: {exc}") from exc
    if task_id is not None and receipt.get("task_id") not in (None, task_id):
        raise AdmissionError(
            f"reference receipt is for {receipt.get('task_id')!r}, not {task_id!r}: {path}"
        )
    extraction = receipt.get("extraction") or {}
    if extraction.get("status") != "ok":
        raise ReferenceUnproven(f"receipt extraction is not ok: {path}")
    patch_file = Path(extraction.get("solution_patch_path") or "")
    try:
        patch_bytes = patch_file.read_bytes()
    except OSError as exc:
        raise ReferenceUnproven(f"reference patch is missing: {patch_file} ({exc})") from exc
    patch_hex = hashlib.sha256(patch_bytes).hexdigest()
    patch_sha = f"sha256:{patch_hex}"
    recorded_sha = str(extraction.get("solution_patch_sha256") or "").removeprefix("sha256:")
    if recorded_sha and recorded_sha != patch_hex:
        raise ReferenceUnproven(f"reference patch sha mismatch: {patch_file}")
    arms = receipt.get("arms") or {}
    oracle_reward = (arms.get("oracle") or {}).get("reward")
    nop_reward = (arms.get("nop") or {}).get("reward")
    try:
        oracle_reward = None if oracle_reward is None else float(oracle_reward)
        nop_reward = None if nop_reward is None else float(nop_reward)
    except (TypeError, ValueError):
        oracle_reward, nop_reward = None, None
    if oracle_reward != 1.0 or nop_reward != 0.0:
        raise ReferenceUnproven(
            f"receipt records oracle={oracle_reward}/nop={nop_reward}, not 1.0/0.0: {path}"
        )
    fix = extraction.get("fix") or {}
    return OracleReference(
        task_id=str(receipt.get("task_id") or task_id or ""),
        label=ORACLE_PROVEN_LABEL,
        fix_commit=str(fix.get("sha") or ""),
        receipt_path=str(path),
        receipt_run_digest=str(receipt.get("run_digest") or ""),
        patch_sha256=patch_sha,
        patch_bytes=patch_bytes,
        recorded_oracle_reward=oracle_reward,
        recorded_nop_reward=nop_reward,
    )


def resolve_oracle_reference(
    task_dir: Path,
    task_id: str,
    *,
    sweep_csv: Path | None,
    reference_arg: str = "auto",
) -> OracleReference | None:
    """Resolve the proven reference fix for one task (``auto`` | ``none`` | path).

    ``auto`` reads the committed sweep projection and validates the recorded
    receipt; a raw patch file is used verbatim as an explicit reference;
    ``none`` (or any miss) yields ``None`` — the oracle step then reports
    ``unproven`` instead of burning Docker or failing closed as infra.
    """
    if reference_arg == "none":
        return None
    if reference_arg != "auto":
        explicit = Path(reference_arg)
        if not explicit.is_file():
            raise AdmissionError(f"reference file is missing: {explicit}")
        if explicit.suffix == ".json":
            return _reference_from_receipt(explicit, task_id=task_id)
        patch_bytes = explicit.read_bytes()
        return OracleReference(
            task_id=task_id,
            label="explicit",
            fix_commit="explicit",
            receipt_path=str(explicit),
            receipt_run_digest="",
            patch_sha256=f"sha256:{hashlib.sha256(patch_bytes).hexdigest()}",
            patch_bytes=patch_bytes,
            recorded_oracle_reward=None,
            recorded_nop_reward=None,
        )
    if sweep_csv is None:
        return None
    try:
        rows = sweep_csv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    match: dict[str, str] = {}
    reader = csv.DictReader(rows)
    for row in reader:
        if (row.get("task_id") or "").strip() == task_id:
            match = row
            break
    if not match:
        return None
    if (match.get("label") or "").strip() != ORACLE_PROVEN_LABEL:
        return None
    receipt_path = Path(match.get("evidence_path") or "")
    if not receipt_path.is_file():
        return None
    return _reference_from_receipt(receipt_path, task_id=task_id)


def render_reference_solve_sh(workdir: str, patch: bytes, patch_sha: str) -> bytes:
    """Oracle solve.sh: apply the reference patch to the agent worktree, then stop."""
    try:
        diff = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdmissionError(f"reference patch is not utf-8: {exc}") from exc
    fence = f"ADMISSION_REF_{patch_sha.removeprefix('sha256:')[:12]}"
    return (
        "\n".join(
            [
                "#!/bin/bash",
                "# Admission-gate oracle: apply the proven reference patch, change nothing else.",
                "set -euo pipefail",
                f'CWD="{workdir}"',
                'cd "$CWD"',
                f"git apply --whitespace=nowarn <<'{fence}'",
                diff.rstrip("\n"),
                fence,
                'echo "admission: reference patch applied"',
            ]
        )
        + "\n"
    ).encode("utf-8")


def resolve_task_record(
    task_dir: Path, *, repo_root: Path | str, records_dir: Path | str
) -> VariantRecord | None:
    """Resolve one task directory to its lineage record without a corpus scan.

    ``lineage_chain`` parses every record under the records dir (15k+ files
    and growing); the gate only ever needs the record for the tested package,
    whose path is fully determined by its task slug plus package digest.
    Returns ``None`` for non-variant packages; a digest mismatch also yields
    ``None`` rather than a wrong record.
    """
    import tomllib

    from evallab.registry import task_directory_digest
    from evallab.task_variants import resolve_record

    try:
        full_name = str(
            tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
            .get("task", {})
            .get("name", "")
        ).strip()
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not full_name:
        return None
    slug = full_name.strip("/").replace("/", "__")
    try:
        digest = task_directory_digest(task_dir)
    except Exception:
        return None
    short = digest.split(":", 1)[1][:12]
    root = Path(repo_root).resolve()
    base = Path(records_dir)
    candidate = (base if base.is_absolute() else root / base) / slug / f"{short}.json"
    if not candidate.is_file():
        return None
    try:
        record = resolve_record(candidate, repo_root=root, records_dir=records_dir)
    except Exception:
        return None
    return record if record.variant_digest == digest else None


# --------------------------------------------------------------------------- #
# Step runners (one per kind; the registry is the extension point)
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_scan_step(step: StepDef, ctx: StepContext) -> AdmissionStepRecord:
    started = time.monotonic()
    try:
        run = ctx.scan or default_scan
        outcome = run(ctx.task_dir)
    except Exception as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"static scan broke: {exc}",
        )
    return AdmissionStepRecord(
        name=step.name,
        kind=step.kind,
        command=outcome.command,
        rewards=[],
        duration_s=time.monotonic() - started,
        status="pass",
        detail=(
            f"{len(outcome.findings)} static finding(s) recorded "
            "(advisory; the dynamic proofs veto)"
        ),
        findings=[AdmissionFinding.model_validate(row) for row in outcome.findings],
    )


def _run_control_step(step: StepDef, ctx: StepContext) -> AdmissionStepRecord:
    started = time.monotonic()
    if step.agent not in ("oracle", "nop") or step.expect is None:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"misconfigured control step: {step!r}",
        )
    if step.agent == "oracle" and not (ctx.task_dir / "solution" / "solve.sh").is_file():
        return _run_oracle_reference_step(step, ctx, started)
    job_name = _safe_job_name(ctx.job_prefix, ctx.slug, step.agent or "control")
    try:
        run = ctx.control or default_control
        outcome = run(
            task_dir=ctx.task_dir,
            agent=step.agent,
            job_name=job_name,
            jobs_dir=ctx.jobs_dir,
            repo_src=ctx.repo_src,
            repeat_n=ctx.repeat_n,
        )
    except Exception as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=["evallab", "tasks", "stability-run", "--agent", step.agent or ""],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"{step.agent} control broke: {exc}",
        )
    return _score_control_outcome(step, outcome, started)


def _score_control_outcome(
    step: StepDef,
    outcome: ControlOutcome,
    started: float,
    *,
    reference: str = "",
) -> AdmissionStepRecord:
    """Score one finished control trial set: full proof or nothing.

    No usable trial evidence is infra; a partial proof (some rewards missing
    or mismatched) is a fail — the gate demands every observed reward equal
    the expectation.
    """
    duration = time.monotonic() - started
    job = str(outcome.job_dir) if outcome.job_dir is not None else None
    observed = [reward for reward in outcome.rewards if reward is not None]
    if outcome.returncode not in (0, None) or outcome.job_dir is None or not observed:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=outcome.command,
            job_dir=job,
            rewards=list(outcome.rewards),
            duration_s=duration,
            status="infra",
            detail=f"{step.agent} control produced no usable trial evidence",
            reference=reference,
        )
    bad = [reward for reward in outcome.rewards if reward != step.expect]
    if bad:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=outcome.command,
            job_dir=job,
            rewards=list(outcome.rewards),
            duration_s=duration,
            status="fail",
            detail=(
                f"{step.agent} control must score {step.expect}; observed {list(outcome.rewards)}"
            ),
            reference=reference,
        )
    return AdmissionStepRecord(
        name=step.name,
        kind=step.kind,
        command=outcome.command,
        job_dir=job,
        rewards=list(outcome.rewards),
        duration_s=duration,
        status="pass",
        detail=f"{step.agent} control scored {step.expect} on every trial",
        reference=reference,
    )


def _safe_job_name(*parts: str) -> str:
    """Harbor job names: 3-80 lowercase letters, numbers, or hyphens."""
    import re

    name = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")
    return name[:80].rstrip("-") or "admit-job"


def _unproven_step(
    step: StepDef, started: float, detail: str, *, reference: str = ""
) -> AdmissionStepRecord:
    return AdmissionStepRecord(
        name=step.name,
        kind=step.kind,
        command=[],
        duration_s=time.monotonic() - started,
        status="unproven",
        detail=detail,
        reference=reference,
    )


def _run_oracle_reference_step(
    step: StepDef, ctx: StepContext, started: float
) -> AdmissionStepRecord:
    """Oracle via the proven reference patch (HAR-191 path for solution-less tasks).

    Stages the package, plants a solve.sh that applies the reference patch in
    the agent worktree (the mtime-validation mechanism), and grades the fixed
    tree with the oracle agent. No reference, no Docker: ``unproven``.
    """
    try:
        task_id = _task_id_of(ctx.task_dir)
    except ReferenceUnproven as exc:
        return _unproven_step(step, started, str(exc))
    try:
        resolve = ctx.reference or resolve_oracle_reference
        ref = resolve(
            ctx.task_dir,
            task_id,
            sweep_csv=ctx.sweep_csv,
            reference_arg=ctx.reference_arg,
        )
    except AdmissionError as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"oracle reference broke: {exc}",
        )
    except ReferenceUnproven as exc:
        return _unproven_step(step, started, str(exc))
    if ref is None:
        return _unproven_step(
            step,
            started,
            f"no proven oracle reference for {task_id} "
            f"(need {ORACLE_PROVEN_LABEL} in the sweep or --reference PATH)",
        )
    try:
        workdir = _task_workdir(ctx.task_dir)
    except ReferenceUnproven as exc:
        return _unproven_step(step, started, str(exc), reference=_ref_label(ref))
    try:
        from evallab.task_stability import stage_task

        ref_src = ctx.jobs_dir / f"{ctx.job_prefix}-{ctx.slug}-oracle-refsrc"
        if ref_src.exists():
            import shutil

            def _writable_rmtree(target: Path) -> None:
                def _onerror(func: Callable[..., object], path: str, _exc: object) -> None:
                    os.chmod(path, 0o700)
                    func(path)

                shutil.rmtree(target, onerror=_onerror)

            _writable_rmtree(ref_src)
        staged = stage_task(ctx.task_dir, ref_src)
        staged.chmod(staged.stat().st_mode | 0o200)
        for dirpath, dirnames, filenames in os.walk(staged):
            for name in dirnames + filenames:
                target = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    target.chmod(target.stat().st_mode | 0o200)
        solve = staged / "solution" / "solve.sh"
        solve.parent.mkdir(parents=True, exist_ok=True)
        solve.write_bytes(render_reference_solve_sh(workdir, ref.patch_bytes, ref.patch_sha256))
        os.chmod(solve, 0o755)
    except OSError as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"oracle reference staging broke: {exc}",
            reference=_ref_label(ref),
        )
    job_name = _safe_job_name(ctx.job_prefix, ctx.slug, step.agent or "oracle")
    try:
        run = ctx.control or default_control
        outcome = run(
            task_dir=staged,
            agent=step.agent or "oracle",
            job_name=job_name,
            jobs_dir=ctx.jobs_dir,
            repo_src=ctx.repo_src,
            repeat_n=ctx.repeat_n,
        )
    except Exception as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=["evallab", "tasks", "stability-run", "--agent", step.agent or ""],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"{step.agent} control broke: {exc}",
            reference=_ref_label(ref),
        )
    return _score_control_outcome(step, outcome, started, reference=_ref_label(ref, ctx.task_dir))


def _ref_label(ref: OracleReference, task_dir: Path | None = None) -> str:
    """One-line digest-bound reference provenance for the record."""
    binding = "unbound"
    if task_dir is not None and ref.receipt_run_digest:
        try:
            from evallab.registry import task_directory_digest

            binding = (
                "exact"
                if task_directory_digest(task_dir) == ref.receipt_run_digest
                else "task-level"
            )
        except Exception:
            binding = "unverified"
    return (
        f"sweep:{ref.receipt_path} fix={ref.fix_commit} patch={ref.patch_sha256} "
        f"recorded-oracle={ref.recorded_oracle_reward}/nop={ref.recorded_nop_reward} "
        f"binding={binding}"
    )


def _run_cheat_step(step: StepDef, ctx: StepContext) -> AdmissionStepRecord:
    started = time.monotonic()
    if not step.attack:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"misconfigured cheat step: {step!r}",
        )
    job_name = _safe_job_name(ctx.job_prefix, ctx.slug, step.attack)
    try:
        run = ctx.cheat or default_cheat
        outcome = run(
            task_dir=ctx.task_dir,
            attack=step.attack,
            job_name=job_name,
            jobs_dir=ctx.jobs_dir,
            repo_root=ctx.repo_root,
            timeout_seconds=ctx.timeout_seconds,
        )
    except Exception as exc:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=["evallab", "cheat", "run", "--attacks", step.attack],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=f"cheat {step.attack} broke: {exc}",
        )
    duration = time.monotonic() - started
    job = str(outcome.job_dir) if outcome.job_dir is not None else None
    rewards = [] if outcome.reward is None else [outcome.reward]
    if outcome.job_dir is None or outcome.reward is None:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=outcome.command,
            job_dir=job,
            rewards=rewards,
            duration_s=duration,
            status="infra",
            detail=f"cheat {step.attack} produced no usable reward",
        )
    if outcome.reward >= 1.0:
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=outcome.command,
            job_dir=job,
            rewards=rewards,
            duration_s=duration,
            status="fail",
            detail=f"cheat {step.attack} cracked the task (reward {outcome.reward})",
        )
    return AdmissionStepRecord(
        name=step.name,
        kind=step.kind,
        command=outcome.command,
        job_dir=job,
        rewards=rewards,
        duration_s=duration,
        status="pass",
        detail=f"cheat {step.attack} resisted (reward {outcome.reward})",
    )


#: Step-kind registry. A future verifier-mutation step registers one entry
#: here and appends one :class:`StepDef` in :func:`default_steps`.
STEP_RUNNERS: dict[str, Callable[[StepDef, StepContext], AdmissionStepRecord]] = {
    "static-scan": _run_scan_step,
    "control": _run_control_step,
    "cheat": _run_cheat_step,
}


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


@dataclass
class AdmissionResult:
    """In-memory gate outcome: the record, where it landed, lineage effect."""

    record: AdmissionRecord
    record_path: Path
    variant_status_updated: bool
    variant_record: str | None = None
    variant_status_note: str | None = None


def plan_admission(
    steps: Sequence[StepDef] | None = None,
) -> list[dict[str, Any]]:
    """Dry-run plan: ordered step names with their representative commands."""
    planned: list[dict[str, Any]] = []
    for step in steps or default_steps():
        if step.kind == "static-scan":
            command = ["evallab", "hack", "scan", "<task>", "--json"]
        elif step.kind == "control":
            command = [
                "harbor",
                "run",
                "--agent",
                step.agent or "",
                "<task>",
                "--verifier",
                "evallab.harbor_repeat_verifier:RepeatVerifier",
            ]
        elif step.kind == "cheat":
            command = [
                "evallab",
                "cheat",
                "run",
                "--task",
                "<task>",
                "--attacks",
                step.attack or "",
                "--attempts",
                "1",
            ]
        else:
            command = [f"<{step.kind}>", "<task>"]
        planned.append({"name": step.name, "kind": step.kind, "command": command})
    return planned


def _task_slug(task_dir: Path) -> str:
    return task_dir.name.strip() or "task"


def admit_task(
    task_dir: Path | str,
    *,
    jobs_dir: Path | str,
    repo_root: Path | str,
    repo_src: Path | str | None = None,
    job_prefix: str = "admit",
    records_dir: Path | str = Path("library/task-variants"),
    by: str = "operator",
    repeat_n: int = 3,
    timeout_seconds: int = 600,
    steps: Sequence[StepDef] | None = None,
    step_runners: Mapping[str, Callable[[StepDef, StepContext], AdmissionStepRecord]] | None = None,
    scan: Callable[[Path], ScanOutcome] | None = None,
    control: Callable[..., ControlOutcome] | None = None,
    cheat: Callable[..., CheatOutcome] | None = None,
    reference: Callable[..., OracleReference | None] | None = None,
    reference_arg: str = "auto",
    sweep_csv: Path | str | None = None,
    output: Path | str | None = None,
    update_variant_status: bool = True,
) -> AdmissionResult:
    """Run the ordered gate over one task package; always write the record.

    Stops after the first non-passing step. Raises :class:`AdmissionError`
    only when the record itself cannot be written; step-level infrastructure
    failures become ``not_admitted`` records instead.
    """
    from evallab.registry import harbor_task_digest, task_directory_digest

    task = Path(task_dir).resolve()
    jobs = Path(jobs_dir).resolve()
    root = Path(repo_root).resolve()
    src = (
        Path(repo_src).resolve() if repo_src is not None else Path(__file__).resolve().parent.parent
    )
    if not task.is_dir() or not (task / "task.toml").is_file():
        raise AdmissionError(f"task package is missing task.toml: {task}")

    runners = dict(STEP_RUNNERS) if step_runners is None else dict(step_runners)
    ordered = list(steps) if steps is not None else default_steps()
    ctx = StepContext(
        task_dir=task,
        jobs_dir=jobs,
        repo_root=root,
        repo_src=src,
        job_prefix=job_prefix,
        slug=_task_slug(task),
        repeat_n=repeat_n,
        timeout_seconds=timeout_seconds,
        scan=scan,
        control=control,
        cheat=cheat,
        reference=reference,
        reference_arg=reference_arg,
        sweep_csv=(Path(sweep_csv) if sweep_csv is not None else root / DEFAULT_SWEEP_CSV),
    )

    gate_started = time.monotonic()
    try:
        package_digest: str | None = task_directory_digest(task)
    except Exception:
        package_digest = None
    try:
        harbor_digest: str | None = harbor_task_digest(task)
    except Exception:
        harbor_digest = None

    executed: list[AdmissionStepRecord] = []
    skipped = [step.name for step in ordered]
    for step in ordered:
        runner = runners.get(step.kind)
        if runner is None:
            executed.append(
                AdmissionStepRecord(
                    name=step.name,
                    kind=step.kind,
                    command=[],
                    duration_s=0.0,
                    status="infra",
                    detail=f"no runner registered for kind {step.kind!r}",
                )
            )
        else:
            executed.append(runner(step, ctx))
        skipped.pop(0)
        if executed[-1].status != "pass":
            break

    failing = [row.name for row in executed if row.status == "fail"]
    infra = [row.name for row in executed if row.status == "infra"]
    unproven = [row.name for row in executed if row.status == "unproven"]
    verdict: AdmissionVerdict = (
        "admitted"
        if not failing and not infra and not unproven
        else "rejected"
        if failing
        else "unproven"
        if unproven
        else "not_admitted"
    )
    failed_attacks = [
        row.name.split(":", 1)[1]
        for row in executed
        if row.status == "fail" and row.kind == "cheat" and ":" in row.name
    ]
    record = AdmissionRecord(
        task=str(task),
        package_digest=package_digest,
        harbor_digest=harbor_digest,
        created_by=by,
        created_at=_utc_now_iso(),
        steps=executed,
        skipped_steps=skipped,
        verdict=verdict,
        failing_steps=[*failing, *infra],
        failed_attacks=failed_attacks,
        unproven_steps=unproven,
        duration_s=time.monotonic() - gate_started,
    )

    jobs.mkdir(parents=True, exist_ok=True)
    gate_dir = jobs / f"{job_prefix}-{ctx.slug}"
    gate_dir.mkdir(parents=True, exist_ok=True)
    record_path = (
        Path(output).resolve() if output is not None else gate_dir / record_filename(package_digest)
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        record_path.write_text(
            json.dumps(record.model_dump(mode="json", by_alias=True), indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise AdmissionError(f"cannot write admission record {record_path}: {exc}") from exc

    variant_status_updated = False
    variant_record: str | None = None
    variant_status_note: str | None = None
    if update_variant_status:
        from evallab.task_variants import VariantError, append_status_evidence

        head = resolve_task_record(task, repo_root=root, records_dir=records_dir)
        if head is not None and verdict in ("admitted", "rejected"):
            status = "validated" if verdict == "admitted" else "rejected"
            current = head.status
            if current in ("validated", "rejected") and current != status:
                variant_status_note = (
                    f"lineage record is already {current!r}; final verdicts are never reflipped"
                )
            else:
                try:
                    updated = append_status_evidence(
                        head,
                        status,  # type: ignore[arg-type]
                        evidence=(f"admission {verdict}: {record_path} (package {package_digest})"),
                        by=by,
                        repo_root=root,
                        records_dir=records_dir,
                    )
                except VariantError as exc:
                    variant_status_note = f"variant-status update refused: {exc}"
                else:
                    variant_record = updated.record_relpath().as_posix()
                    variant_status_updated = True

    if variant_record is not None:
        record = record.model_copy(update={"variant_record": variant_record})
        try:
            record_path.write_text(
                json.dumps(record.model_dump(mode="json", by_alias=True), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise AdmissionError(f"cannot rewrite admission record {record_path}: {exc}") from exc

    return AdmissionResult(
        record=record,
        record_path=record_path,
        variant_status_updated=variant_status_updated,
        variant_record=variant_record,
        variant_status_note=variant_status_note,
    )


__all__ = [
    "ADMISSION_SCHEMA",
    "GATE_VERSION",
    "AdmissionError",
    "AdmissionFinding",
    "AdmissionRecord",
    "AdmissionResult",
    "AdmissionStepRecord",
    "CheatOutcome",
    "ControlOutcome",
    "ScanOutcome",
    "StepContext",
    "StepDef",
    "STEP_RUNNERS",
    "DEFAULT_SWEEP_CSV",
    "ORACLE_PROVEN_LABEL",
    "OracleReference",
    "ReferenceUnproven",
    "admit_task",
    "default_cheat",
    "default_control",
    "default_scan",
    "default_steps",
    "plan_admission",
    "record_filename",
    "render_reference_solve_sh",
    "resolve_oracle_reference",
    "resolve_task_record",
]
