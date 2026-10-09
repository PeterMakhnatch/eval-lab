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
unreadable rewards, or an oracle with no shipped reference solution).
Fail-closed means infra never silently passes.

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

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.cheat import ATTACKS
from evallab.schemas import ContractModel

#: Durable admission-record schema tag.
ADMISSION_SCHEMA = "evallab.task_admission/v1"

#: Gate implementation version pinned in every record (step-list revisions bump this).
GATE_VERSION = "tasks.admit/v1"

AdmissionVerdict = Literal["admitted", "rejected", "not_admitted"]
StepStatus = Literal["pass", "fail", "infra"]


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
        return AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=[],
            duration_s=time.monotonic() - started,
            status="infra",
            detail=(
                "oracle control needs solution/solve.sh and the package ships none; "
                "solvability is unprovable (not a task failure)"
            ),
        )
    job_name = f"{ctx.job_prefix}-{ctx.slug}-{step.agent}"
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
    job_name = f"{ctx.job_prefix}-{ctx.slug}-{step.attack}"
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
    verdict: AdmissionVerdict = (
        "admitted" if not failing and not infra else "rejected" if failing else "not_admitted"
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
        from evallab.task_variants import (
            LineageError,
            VariantError,
            append_status_evidence,
            lineage_chain,
        )

        try:
            chain = lineage_chain(task, repo_root=root, records_dir=records_dir)
        except LineageError:
            chain = []
        if chain and verdict != "not_admitted":
            status = "validated" if verdict == "admitted" else "rejected"
            current = chain[0].record.status
            if current in ("validated", "rejected") and current != status:
                variant_status_note = (
                    f"lineage record is already {current!r}; final verdicts are never reflipped"
                )
            else:
                try:
                    updated = append_status_evidence(
                        chain[0].record,
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
    "admit_task",
    "default_cheat",
    "default_control",
    "default_scan",
    "default_steps",
    "plan_admission",
    "record_filename",
]
