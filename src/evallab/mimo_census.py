"""Fleet-wide clean-set census (mimo-clean-census-v1).

Grades every clean-set manifest row on four checks and records one row per
task in ``research/experiments/mimo-clean-census/results.csv``:

(a) nop control is 0 with tests actually executed (JUnit grade evidence);
(b) oracle control is 1 where the manifest carries a reference fix;
(c) the full 12-attack cheat ladder (``evallab.cheat_ladder.ATTACKS``,
    executed through the model-free ``cheat`` agent — payloads reused, never
    forked) grades clean, with per-attack attribution trials when a
    full-ladder trial cracks;
(d) fix-content census (``evallab.fix_content_census``) on the clean chain
    reports 0 open-leak locations for tasks with a recoverable fix.

Backends: ``docker`` (locked local Docker, $0 — parity baseline),
``modal`` (Harbor's native ModalEnvironment with the REAL clean package
semantics; currently blocked — see docs/mimo/verification.md), and
``daytona`` (bounded Daytona sandboxes; paid fallback). Fresh-sandbox
separate-verifier grading comes from Harbor's own lifecycle, not a retest.
The census never edits ``mimo_clean.py`` and never writes the clean
manifest; it reads package paths/fix identities from the manifest and writes
only its own receipt directory.

Manifest ``verify`` grades defined here (see docs/mimo/verification.md):
``verified-clean``, ``open-leak``, ``grader-hole``, ``env-broken``,
``oracle-wrong``, ``infra-flake``, ``needs-triage``, ``unverified``.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import re
import tomllib
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cheat import build_verdicts, collect_cheat_attempts
from evallab.cheat_ladder import ATTACKS
from evallab.mimo_clean import STATUS_BUILT, acceptance_pass, load_manifest, summarize_trials

#: Census version id (job-name namespace + manifest_version values).
CENSUS_VERSION = "mimo-clean-census-v1"

#: Tracked receipt directory (repo-relative). Raw run output lives outside
#: git under ~/Developer/eval-lab-results/2026-10-09/mimo-clean-census/.
RECEIPT_REL = Path("research/experiments/mimo-clean-census")
RESULTS_FILENAME = "results.csv"
SPEND_FILENAME = "spend.jsonl"
ROWS_FILENAME = "census-rows.jsonl"

#: results.csv columns (contract).
RESULTS_COLUMNS = (
    "task_id",
    "manifest_version",
    "final_digest",
    "nop",
    "oracle",
    "ladder_verdict",
    "ladder_cracking_attacks",
    "census_locations",
    "backend",
    "cost_usd",
    "run_ids",
)

#: Manifest ``verify`` grades owned by this census.
VERIFY_CLEAN = "verified-clean"
VERIFY_OPEN_LEAK = "open-leak"
VERIFY_GRADER_HOLE = "grader-hole"
VERIFY_ENV_BROKEN = "env-broken"
VERIFY_ORACLE_WRONG = "oracle-wrong"
VERIFY_INFRA_FLAKE = "infra-flake"
VERIFY_NEEDS_TRIAGE = "needs-triage"
VERIFY_UNVERIFIED = "unverified"
VERIFY_BACKEND_UNSUPPORTED = "backend-unsupported"

#: Backends the census runner supports. ``modal`` runs Harbor's native
#: ModalEnvironment; ``daytona`` the bounded Daytona sandbox env. Paid
#: backends need their SDK/credentials at run time (modal: ``uv run --with
#: modal`` + ~/.modal.toml; daytona: DAYTONA_API_KEY in the environment).
BACKENDS = ("docker", "modal", "daytona")
JOB_PREFIX = "mimo-census-v1"

#: Slice spend cap (USD) — see the assignment; enforced per batch.
SLICE_CAP_USD = 15.00

_JUNIT_RE = re.compile(
    r"rc=(?P<rc>-?\d+)\s+cases=(?P<cases>\d+)\s+bad=(?P<bad>\d+)\s+"
    r"named=(?P<named>\d+)\s+missing=\[(?P<missing>[^\]]*)\]"
)


def parse_task_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated task list (empty/None means no filter)."""
    if raw is None or not raw.strip():
        return ()
    seen: list[str] = []
    for part in raw.split(","):
        task_id = part.strip()
        if task_id and task_id not in seen:
            seen.append(task_id)
    return tuple(seen)


def parse_junit_grade(text: str) -> dict[str, Any] | None:
    """Parse one ``junit-grade.log`` line into grade facts, or None."""
    match = _JUNIT_RE.search(text.strip())
    if match is None:
        return None
    missing = [item.strip() for item in match.group("missing").split(",") if item.strip()]
    return {
        "rc": int(match.group("rc")),
        "cases": int(match.group("cases")),
        "bad": int(match.group("bad")),
        "named": int(match.group("named")),
        "missing": missing,
    }


def trial_grade_logs(trial_dir: Path) -> list[dict[str, Any]]:
    """JUnit grade facts for every verifier log under one trial dir."""
    grades: list[dict[str, Any]] = []
    for text in trial_output_logs(trial_dir, "junit-grade.log"):
        grade = parse_junit_grade(text)
        if grade is not None:
            grades.append(grade)
    return grades


_PYTEST_RESULT_LINE_RE = re.compile(r"(?m)^(?:FAILED|ERROR|PASSED)\s+\S+")
_PYTEST_SUMMARY_RE = re.compile(r"\b\d+\s+(?:failed|passed|error)\b.*in\s+[\d.]+s")


def trial_output_logs(trial_dir: Path, name: str) -> list[str]:
    """Read all verifier ``name`` logs under one trial dir (missing -> skip)."""
    texts: list[str] = []
    for log_path in sorted(trial_dir.rglob(name)):
        try:
            texts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def trial_tests_executed(trial_dir: Path) -> bool:
    """Whether a trial has evidence the verifier actually ran test cases.

    Two shapes: JUnit grade logs with ``cases > 0`` (``named`` is diagnostic
    only — some suites report cases without parsable names), or pytest
    result/summary lines in ``test_output.log`` for exit-code-graded suites
    that emit no JUnit XML.
    """
    if any(grade["cases"] > 0 for grade in trial_grade_logs(trial_dir)):
        return True
    return any(
        _PYTEST_RESULT_LINE_RE.search(text) is not None
        or _PYTEST_SUMMARY_RE.search(text) is not None
        for text in trial_output_logs(trial_dir, "test_output.log")
    )


def _trial_dirs_with_results(job_dir: Path) -> list[Path]:
    """Trial subdirectories holding a result.json (sorted, job-level excluded)."""
    if not job_dir.is_dir():
        return []
    return sorted(
        candidate
        for candidate in job_dir.iterdir()
        if candidate.is_dir() and (candidate / "result.json").is_file()
    )


def _job_payload(job_dir: Path) -> dict[str, Any] | None:
    """Job-level result.json payload, or None when absent/unreadable."""
    payload_path = job_dir / "result.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def job_has_trial_errors(job_dir: Path) -> bool:
    """Whether the job-level result records errored trials or eval errors.

    Setup-failing packages (e.g. a fail-closed cache-purge block: healthcheck
    rc=1, no reward) surface here, never as scored grading failures.
    """
    payload = _job_payload(job_dir)
    if not isinstance(payload, dict):
        return False
    stats = payload.get("stats")
    if not isinstance(stats, Mapping):
        return False
    if int(stats.get("n_errored_trials", 0) or 0) > 0:
        return True
    evals = stats.get("evals")
    if not isinstance(evals, Mapping):
        return False
    for report in evals.values():
        if not isinstance(report, Mapping):
            continue
        if int(report.get("n_errors", 0) or 0) > 0:
            return True
        exceptions = report.get("exception_stats")
        if isinstance(exceptions, Mapping) and exceptions:
            return True
    return False


_DISK_EXHAUSTED_RES = (
    re.compile(r"no space left on device", re.IGNORECASE),
    re.compile(r"disk quota exceeded", re.IGNORECASE),
    re.compile(r"Daytona.*disk|disk.*Daytona.*(limit|quota|exceeded)", re.IGNORECASE),
)


def trial_disk_exhausted(trial_dir: Path) -> bool:
    """Whether a trial's logs show disk exhaustion (backend capacity, not env).

    Daytona per-sandbox disk (10 GiB) can be exhausted by images with heavy
    setup writes; such tasks are ``backend-unsupported``, never failures.
    """
    for text in trial_output_logs(trial_dir, "trial.log"):
        if any(pattern.search(text) for pattern in _DISK_EXHAUSTED_RES):
            return True
    for text in trial_output_logs(trial_dir, "setup.log"):
        if any(pattern.search(text) for pattern in _DISK_EXHAUSTED_RES):
            return True
    return False


def _unscored_grade(job_dir: Path) -> str:
    """Unscored-cell grade: ``backend-unsupported`` > ``setup-fail`` > ``unscored``."""
    trials = _trial_dirs_with_results(job_dir)
    if trials and all(trial_disk_exhausted(trial) for trial in trials):
        return "backend-unsupported"
    return "setup-fail" if job_has_trial_errors(job_dir) else "unscored"


def _rewards_string(rewards: Collection[float | None]) -> str:
    return ",".join("1" if r == 1 else "0" if r == 0 else "?" for r in rewards)


def nop_cell_grade(job_dir: Path) -> str:
    """Grade string for one nop cell dir.

    ``"0"`` = every trial rewarded 0 with executed-test evidence; anything
    else names the failure (``"missing"``, ``"unscored"``, ``"setup-fail"``
    for errored jobs such as fail-closed setup blocks, ``"fail:<rewards>"``,
    or ``"0-noexec"`` when rewards are 0 but no test-execution evidence).
    """
    trials = _trial_dirs_with_results(job_dir)
    if not trials:
        return "missing"
    rewards = summarize_trials(job_dir)
    if not rewards or any(reward is None for reward in rewards):
        return _unscored_grade(job_dir)
    if any(reward != 0 for reward in rewards):
        return f"fail:{_rewards_string(rewards)}"
    if not all(trial_tests_executed(trial) for trial in trials):
        return "0-noexec"
    return "0"


def oracle_cell_grade(job_dir: Path | None, *, has_reference_fix: bool) -> str:
    """Grade string for one oracle cell (``"n/a"`` without a reference fix)."""
    if not has_reference_fix:
        return "n/a"
    if job_dir is None:
        return "missing"
    trials = _trial_dirs_with_results(job_dir)
    if not trials:
        return "missing"
    rewards = summarize_trials(job_dir)
    if not rewards or any(reward is None for reward in rewards):
        return _unscored_grade(job_dir)
    if any(reward == 1 for reward in rewards):
        if not all(trial_tests_executed(trial) for trial in trials):
            return "1-noexec"
        return "1"
    return f"fail:{_rewards_string(rewards)}"


def ladder_summary(job_dir: Path) -> dict[str, Any]:
    """Summarize one cheat-ladder cell dir.

    Returns verdict (``clean``/``cracked``/``partial``/``unscored``/
    ``missing``), the executed/skipped attack sets, and the cracking attack
    list. ``clean`` needs every trial at reward 0 with each ladder attack
    either executed or legitimately skipped (per-attack inapplicability with
    a recorded reason); attacks that are missing or failed give ``partial``.
    Full-ladder cracks unattributable to one attack report
    ``full-ladder-unattributed``; single-attack attribution trials name
    their attack via the trial method.
    """
    trials = _trial_dirs_with_results(job_dir)
    if not trials:
        return {
            "verdict": "missing",
            "executed": [],
            "skipped": [],
            "cracking": [],
            "trials": 0,
        }
    verdicts = build_verdicts(job_dir)
    payloads = verdicts.get("trials", [])
    attempts = {entry["trial"]: entry for entry in collect_cheat_attempts(job_dir)}
    executed_all: set[str] = set()
    skipped_all: set[str] = set()
    for entry in attempts.values():
        attacks = entry.get("payload", {}).get("attacks") or []
        for attack in attacks:
            if not isinstance(attack, dict):
                continue
            name = attack.get("name")
            if not isinstance(name, str):
                continue
            if attack.get("status") == "executed":
                executed_all.add(name)
            elif attack.get("status") == "skipped":
                skipped_all.add(name)
    verdict_names: list[str] = []
    cracking: set[str] = set()
    for trial in payloads:
        verdict = trial.get("verdict", "unscored")
        verdict_names.append(verdict if isinstance(verdict, str) else "unscored")
        method = trial.get("method") or ""
        methods = [name for name in method.split(",") if name] if method else []
        if verdict == "cracked":
            if len(methods) == 1:
                cracking.add(methods[0])
            else:
                cracking.add("full-ladder-unattributed")
    if any(name == "unscored" for name in verdict_names):
        verdict = "unscored"
    elif any(name == "cracked" for name in verdict_names):
        verdict = "cracked"
    elif not verdict_names:
        verdict = "missing"
    elif set(ATTACKS) <= (executed_all | skipped_all):
        verdict = "clean"
    else:
        verdict = "partial"
    return {
        "verdict": verdict,
        "executed": sorted(executed_all),
        "skipped": sorted(skipped_all),
        "cracking": sorted(cracking),
        "trials": len(trials),
    }


def verify_grade_for(
    *,
    nop: str,
    oracle: str,
    ladder_verdict: str,
    census_locations: int | None,
) -> str:
    """Map per-check signals to one manifest ``verify`` grade."""
    if (
        nop == "backend-unsupported"
        or oracle == "backend-unsupported"
        or ladder_verdict == "backend-unsupported"
    ):
        return VERIFY_BACKEND_UNSUPPORTED
    if nop in ("missing", "unscored") or ladder_verdict in ("missing", "unscored", "partial"):
        return VERIFY_UNVERIFIED
    if oracle in ("missing", "unscored"):
        return VERIFY_UNVERIFIED
    if census_locations is not None and census_locations > 0:
        return VERIFY_OPEN_LEAK
    if ladder_verdict == "cracked":
        return VERIFY_GRADER_HOLE
    if nop == "setup-fail" or oracle == "setup-fail":
        return VERIFY_ENV_BROKEN
    if nop != "0":
        if nop.startswith("fail:"):
            return VERIFY_ENV_BROKEN
        return VERIFY_NEEDS_TRIAGE
    if oracle not in ("1", "n/a"):
        if oracle.startswith("fail:"):
            return VERIFY_ORACLE_WRONG
        return VERIFY_NEEDS_TRIAGE
    return VERIFY_CLEAN


def census_row_pass(row: Mapping[str, Any]) -> bool:
    """Whether a results row meets census acceptance (all four checks)."""
    oracle_ok = row.get("oracle") in ("1", "n/a")
    nop_ok = row.get("nop") == "0"
    ladder_ok = row.get("ladder_verdict") == "clean" and not row.get("ladder_cracking_attacks")
    census_raw = row.get("census_locations")
    census_ok = census_raw in ("", None) or str(census_raw) == "0"
    return bool(oracle_ok and nop_ok and ladder_ok and census_ok)


def acceptance_matches(
    *,
    oracle_rewards: Collection[float | None],
    has_reference_fix: bool,
    nop_rewards: Collection[float | None],
    cracked: int,
) -> bool:
    """Shared acceptance predicate (delegates to ``mimo_clean``)."""
    return acceptance_pass(
        oracle_rewards=oracle_rewards,
        has_reference_fix=has_reference_fix,
        nop_rewards=nop_rewards,
        cracked=cracked,
    )


def fence_allows(*, spent_usd: float, projected_usd: float, cap_usd: float = SLICE_CAP_USD) -> bool:
    """Whether a batch fits the spend fence (actuals + worst case <= cap)."""
    return spent_usd + projected_usd <= cap_usd


def amortized_cost(batch_actual_usd: float, n_tasks: int) -> float:
    """Per-task cost share of one batch's provider actual (0 when empty)."""
    if n_tasks <= 0:
        return 0.0
    return batch_actual_usd / n_tasks


def blank_row(task_id: str, *, manifest_version: str, backend: str) -> dict[str, Any]:
    """Empty results row with identity columns filled."""
    return {
        "task_id": task_id,
        "manifest_version": manifest_version,
        "final_digest": "",
        "nop": "missing",
        "oracle": "missing",
        "ladder_verdict": "missing",
        "ladder_cracking_attacks": "",
        "census_locations": "",
        "backend": backend,
        "cost_usd": "",
        "run_ids": "",
    }


def write_results(rows: Collection[Mapping[str, Any]], path: Path) -> Path:
    """Write results.csv (sorted by task id, deterministic)."""
    ordered = sorted(rows, key=lambda row: row["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RESULTS_COLUMNS))
        writer.writeheader()
        for row in ordered:
            writer.writerow({key: row.get(key, "") for key in RESULTS_COLUMNS})
    return path


def load_results(path: Path) -> list[dict[str, Any]]:
    """Read results.csv back (row dicts keyed by RESULTS_COLUMNS)."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def summarize_results(rows: Collection[Mapping[str, Any]]) -> dict[str, Any]:
    """Pass-rate summary plus per-signal failure buckets for the receipt."""
    rows = list(rows)
    buckets: dict[str, int] = {}
    failures: list[dict[str, str]] = []
    for row in rows:
        task_id = str(row.get("task_id", ""))
        if census_row_pass(row):
            buckets["pass"] = buckets.get("pass", 0) + 1
            continue
        if "backend-unsupported" in (
            str(row.get("nop", "")),
            str(row.get("oracle", "")),
            str(row.get("ladder_verdict", "")),
        ):
            buckets["backend-unsupported"] = buckets.get("backend-unsupported", 0) + 1
            continue
        buckets["fail"] = buckets.get("fail", 0) + 1
        reasons: list[str] = []
        if str(row.get("nop", "")) != "0":
            reasons.append(f"nop={row.get('nop', '')}")
        if str(row.get("oracle", "")) not in ("1", "n/a"):
            reasons.append(f"oracle={row.get('oracle', '')}")
        if str(row.get("ladder_verdict", "")) != "clean" or str(
            row.get("ladder_cracking_attacks", "")
        ):
            reasons.append(
                f"ladder={row.get('ladder_verdict', '')}:{row.get('ladder_cracking_attacks', '')}"
            )
        census_raw = str(row.get("census_locations", ""))
        if census_raw not in ("", "0"):
            reasons.append(f"census={census_raw}")
        failures.append({"task_id": task_id, "reasons": "; ".join(reasons)})
    return {
        "total": len(rows),
        "passed": buckets.get("pass", 0),
        "failed": buckets.get("fail", 0),
        "backend_unsupported": buckets.get("backend-unsupported", 0),
        "failures": sorted(failures, key=lambda item: item["task_id"]),
    }


def utc_now_iso() -> str:
    """Current UTC timestamp (receipt/spend records)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value)


def _short(task_id: str) -> str:
    return task_id.removeprefix("format-code-task-")


def cell_job_name(*, task_id: str, backend: str, cell: str) -> str:
    """Job-name base for one census cell (census namespace, backend-scoped)."""
    return f"{JOB_PREFIX}-{backend}-{_short(task_id)}-{cell}"


def next_free_name(task_jobs: Path, base: str) -> str:
    """Next free job name: ``base`` unless taken, else ``base-attemptN``."""
    attempt = 1
    name = base
    while (task_jobs / name).exists():
        attempt += 1
        name = f"{base}-attempt{attempt}"
    return name


def run_cell(
    *,
    package: Path,
    agent: str,
    name: str,
    jobs_dir: Path,
    backend: str,
    timeout_seconds: int,
    attacks: str | None = None,
    egress_lock: bool | None = None,
    root: Path,
) -> Path:
    """Run one census cell through Harbor; return the job dir.

    Completed job dirs (every trial rewarded) are reused verbatim, never
    clobbered; anything else launches under the next free ``-attemptN`` name.
    ``attacks`` selects the cheat-ladder subset (None = controls).
    ``egress_lock`` overrides the backend default (None = resolve).
    """
    import os

    from evallab.cheat import _CHEAT_ENV_LOCK
    from evallab.execution_contracts import CHEAT_ATTACKS_ENV_VAR, RunRequest
    from evallab.queue import Executor

    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")
    request = RunRequest(
        task=package,
        agent=agent,
        name=name,
        jobs_dir=jobs_dir,
        environment=backend,
        model=None,
        concurrency=1,
        attempts=1,
        timeout_seconds=timeout_seconds,
        allow_billable=backend != "docker",
        egress_lock=egress_lock,
    )
    if attacks is None:
        return Executor.from_repo(root).execute_direct(request)
    with _CHEAT_ENV_LOCK:
        previous = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
        os.environ[CHEAT_ATTACKS_ENV_VAR] = attacks
        try:
            return Executor.from_repo(root).execute_direct(request)
        finally:
            if previous is None:
                os.environ.pop(CHEAT_ATTACKS_ENV_VAR, None)
            else:
                os.environ[CHEAT_ATTACKS_ENV_VAR] = previous


def census_task(
    *,
    task_id: str,
    manifest_row: Mapping[str, str],
    primary: Path,
    jobs_root: Path,
    backend: str,
    timeout_seconds: int,
    root: Path,
) -> dict[str, Any]:
    """Run all grading cells for one manifest row; return partial results.

    Fix-content census (check d) is backend-independent content analysis and
    runs separately via :func:`census_fix_content`; this function covers the
    nop/oracle/ladder grading cells, including per-attack attribution trials
    when a full-ladder trial cracks.
    """
    from evallab.execution_contracts import CHEAT_AGENT

    task_jobs = jobs_root / task_id
    task_jobs.mkdir(parents=True, exist_ok=True)
    package = primary / Path(str(manifest_row["package_path"]))
    has_fix = str(manifest_row.get("reference_fix", "")) not in ("", "none")

    oracle_dir: Path | None = None
    if has_fix:
        base = cell_job_name(task_id=task_id, backend=backend, cell="oracle")
        candidate = task_jobs / base
        if summarize_trials(candidate) and all(
            reward is not None for reward in summarize_trials(candidate)
        ):
            oracle_dir = candidate
        else:
            oracle_dir = run_cell(
                package=package,
                agent="oracle",
                name=next_free_name(task_jobs, base),
                jobs_dir=task_jobs,
                backend=backend,
                timeout_seconds=timeout_seconds,
                root=root,
            )
    base = cell_job_name(task_id=task_id, backend=backend, cell="nop")
    candidate = task_jobs / base
    if summarize_trials(candidate) and all(
        reward is not None for reward in summarize_trials(candidate)
    ):
        nop_dir = candidate
    else:
        nop_dir = run_cell(
            package=package,
            agent="nop",
            name=next_free_name(task_jobs, base),
            jobs_dir=task_jobs,
            backend=backend,
            timeout_seconds=timeout_seconds,
            root=root,
        )
    base = cell_job_name(task_id=task_id, backend=backend, cell="cheat")
    candidate = task_jobs / base
    if _trial_dirs_with_results(candidate):
        cheat_dir: Path | None = candidate
    else:
        # Cheat cells use the backend default lock: the cheat agent is outside
        # every egress-lock set, so locked backends refuse it at dispatch
        # (Daytona+MiMo additionally refuses explicit unlocked). A refusal is
        # recorded as backend-unsupported, never a failure; cheat coverage
        # for those tasks comes from a backend that admits it (docker now,
        # Modal once mtime-normalize@2 unblocks it) as a separate results row.
        try:
            cheat_dir = run_cell(
                package=package,
                agent=CHEAT_AGENT,
                name=next_free_name(task_jobs, base),
                jobs_dir=task_jobs,
                backend=backend,
                timeout_seconds=timeout_seconds,
                attacks="",
                root=root,
            )
        except ValueError as exc:
            if "egress_lock" not in str(exc):
                raise
            print(f"note {task_id}: cheat refused on {backend} ({exc})")
            cheat_dir = None
    summary = (
        ladder_summary(cheat_dir)
        if cheat_dir is not None
        else {"verdict": "backend-unsupported", "executed": [], "cracking": []}
    )
    run_ids = [nop_dir.name]
    if cheat_dir is not None:
        run_ids.append(cheat_dir.name)
    if summary["verdict"] == "cracked" and "full-ladder-unattributed" in summary["cracking"]:
        for attack in ATTACKS:
            attack_base = cell_job_name(task_id=task_id, backend=backend, cell=f"cheat-{attack}")
            attack_candidate = task_jobs / attack_base
            if _trial_dirs_with_results(attack_candidate):
                attack_dir = attack_candidate
            else:
                attack_dir = run_cell(
                    package=package,
                    agent=CHEAT_AGENT,
                    name=next_free_name(task_jobs, attack_base),
                    jobs_dir=task_jobs,
                    backend=backend,
                    timeout_seconds=timeout_seconds,
                    attacks=attack,
                    root=root,
                )
            run_ids.append(attack_dir.name)
        assert cheat_dir is not None  # cracked verdict implies a cheat dir
        summary = ladder_summary(cheat_dir)
        for run_id in run_ids:
            if run_id.startswith(cell_job_name(task_id=task_id, backend=backend, cell="cheat-")):
                single = ladder_summary(task_jobs / run_id)
                if single["verdict"] == "cracked":
                    attack_name = run_id.removeprefix(
                        cell_job_name(task_id=task_id, backend=backend, cell="") + "-"
                    )
                    if attack_name not in summary["cracking"]:
                        summary["cracking"].append(attack_name)
        summary["cracking"] = sorted(
            name for name in summary["cracking"] if name != "full-ladder-unattributed"
        ) or ["full-ladder-unattributed"]
    nop_grade = nop_cell_grade(nop_dir)
    oracle_grade = oracle_cell_grade(oracle_dir, has_reference_fix=has_fix)
    return {
        "nop": nop_grade,
        "oracle": oracle_grade,
        "ladder_verdict": summary["verdict"],
        "ladder_cracking_attacks": ",".join(summary["cracking"]),
        "ladder_executed": ",".join(summary["executed"]),
        "run_ids": ",".join(run_ids),
    }


def census_fix_content(
    *,
    task_id: str,
    clean_package: Path,
    run_package: Path,
    language: str,
    scratch_root: Path,
) -> dict[str, Any]:
    """Fix-content census for one task (check d, local Docker, $0).

    Recovers the reference fix from the published image (pre-cleanup run
    package), probes the published setup, then probes the ACTUAL clean
    package setup (what ships — never a recomposed approximation, so new
    transform versions are measured exactly). Returns the clean-chain
    ``collect_result`` row, or ``{"census_locations": None}`` when no fix is
    recoverable.
    """
    import hashlib

    from evallab.fix_content_census import (
        collect_result,
        copy_git_from_image,
        leak_oracle_extract,
        recover_fix_lite,
        run_probe,
        stage_probe,
    )

    scratch = scratch_root / task_id
    scratch.mkdir(parents=True, exist_ok=True)
    task_doc = tomllib.loads((run_package / "task.toml").read_text())
    environment = task_doc.get("environment", {})
    image = str(environment.get("docker_image", ""))
    workdir = str(environment.get("workdir", "/testbed"))
    if not image:
        return {"census_locations": None, "reason": "run package has no docker_image pin"}
    clean_setup_sha = ""
    clean_sh_path = clean_package / "environment" / "setup" / "setup.sh"
    if clean_sh_path.is_file():
        clean_setup_sha = hashlib.sha256(clean_sh_path.read_bytes()).hexdigest()
    meta_path = scratch / "census.meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        # Reuse completed probes verbatim (deterministic): same image (fix
        # source) and same shipped setup means the same locations count.
        if (
            meta.get("image") == image
            and meta.get("clean_setup_sha256") == clean_setup_sha
            and isinstance(meta.get("clean", {}).get("hits_total"), int)
        ):
            return {
                "census_locations": meta["clean"]["hits_total"],
                "reason": "",
                "detail": meta["clean"],
            }
    git_dir = scratch / "git-copy"
    if not copy_git_from_image(image, workdir, git_dir):
        return {"census_locations": None, "reason": "could not copy .git from image"}
    git_bare = git_dir / ".git"
    if not git_bare.is_dir():
        return {"census_locations": None, "reason": "image has no worktree .git to recover from"}
    extract_out = scratch / "extract"
    extract_out.mkdir(parents=True, exist_ok=True)
    try:
        evidence = leak_oracle_extract(run_package, git_bare, extract_out)
    except Exception as exc:  # noqa: BLE001 - extractor refusal is a finding, not a crash
        return {"census_locations": None, "reason": f"extractor failed: {exc}"}
    extract_status = str(evidence.get("status", ""))
    raw_fix = evidence.get("fix")
    fix_sha = raw_fix if isinstance(raw_fix, str) and raw_fix else ""
    if not fix_sha:
        try:
            lite = recover_fix_lite(git_bare, "", [])
            lite_sha = lite.get("sha", "")
            fix_sha = lite_sha if isinstance(lite_sha, str) and lite_sha else ""
        except Exception as exc:  # noqa: BLE001 - fallback refusal is a finding
            return {"census_locations": None, "reason": f"no recoverable fix: {exc}"}
    if not fix_sha:
        rationale = str(evidence.get("rationale", extract_status))
        return {"census_locations": None, "reason": f"no recoverable fix ({rationale[:120]})"}
    image12 = image.split("@sha256:")[-1][:12] if "@sha256:" in image else image[-12:]
    setup_source = run_package / "environment" / "setup"
    published_stage = scratch / "stage-published"
    published_out = scratch / "out-published"
    stage_probe(published_stage, setup_source)
    run_probe(image, workdir, fix_sha, published_stage, published_out)
    clean_source = clean_package / "environment" / "setup"
    if not clean_setup_sha:
        return {"census_locations": None, "reason": "clean package has no setup.sh"}
    clean_stage = scratch / "stage-clean"
    clean_out = scratch / "out-clean"
    stage_probe(clean_stage, clean_source)
    run_probe(image, workdir, fix_sha, clean_stage, clean_out)
    published_row = collect_result(task_id, language, image12, "published", published_out)
    clean_row = collect_result(task_id, language, image12, "clean", clean_out)
    meta_path = scratch / "census.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "fix_sha": fix_sha,
                "extract_status": extract_status,
                "clean_setup": "package environment/setup/setup.sh",
                "clean_setup_sha256": clean_setup_sha,
                "image": image,
                "published": {k: published_row.get(k) for k in ("hits_total", "open_leak")},
                "clean": clean_row,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        locations = int(clean_row.get("hits_total", 0))
    except (TypeError, ValueError):
        locations = 0
    return {"census_locations": locations, "reason": "", "detail": clean_row}


def append_spend_record(receipt_dir: Path, record: Mapping[str, Any]) -> Path:
    """Append one batch spend record (provider actuals) to spend.jsonl."""
    path = receipt_dir / SPEND_FILENAME
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**dict(record), "recorded_at": utc_now_iso()}) + "\n")
    return path


def slice_spent_usd(receipt_dir: Path) -> float:
    """Total slice spend so far from recorded batch actuals."""
    path = receipt_dir / SPEND_FILENAME
    if not path.is_file():
        return 0.0
    total = 0.0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    for line in lines:
        if not line.strip():
            continue
        try:
            total += float(json.loads(line).get("actual_usd", 0.0))
        except (ValueError, TypeError):
            continue
    return total


#: Daytona pay-as-you-go rates (USD per hour) for usage-based actuals. Basis:
#: measured HAR-88 census (code stratum); posted wallet charges are not
#: API-accessible, so usage x rate-card is the provider actuals source the
#: census records (the assignment names Daytona usage explicitly).
DAYTONA_RATE_CPU_USD_H = 0.0504
DAYTONA_RATE_MEM_GIB_USD_H = 0.0162
DAYTONA_RATE_DISK_GIB_USD_H = 0.000108
DAYTONA_DISK_FREE_GIB = 5.0
DAYTONA_DEFAULT_DISK_GIB = 10.0


def daytona_hourly_usd(*, cpus: float, mem_gib: float, disk_gib: float) -> float:
    """Hourly cost for a Daytona sandbox allocation (usage x rate-card)."""
    billable_disk = max(0.0, disk_gib - DAYTONA_DISK_FREE_GIB)
    return (
        cpus * DAYTONA_RATE_CPU_USD_H
        + mem_gib * DAYTONA_RATE_MEM_GIB_USD_H
        + billable_disk * DAYTONA_RATE_DISK_GIB_USD_H
    )


def trial_wall_hours(trial_dir: Path) -> float | None:
    """Wall-clock hours from a trial result.json (None when unparseable)."""
    try:
        payload = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        start = datetime.fromisoformat(str(payload["started_at"]))
        end = datetime.fromisoformat(str(payload["finished_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    seconds = (end - start).total_seconds()
    return seconds / 3600.0 if seconds >= 0 else None


def task_sandbox_allocation(package: Path) -> tuple[float, float, float]:
    """(cpus, mem_gib, disk_gib) for cost accounting from a task package."""
    import tomllib

    try:
        environment = tomllib.loads((package / "task.toml").read_text()).get("environment", {})
    except (OSError, tomllib.TOMLDecodeError):
        environment = {}
    if not isinstance(environment, dict):
        environment = {}
    try:
        cpus = float(environment.get("cpus", 2))
    except (TypeError, ValueError):
        cpus = 2.0
    try:
        mem_gib = float(environment.get("memory_mb", 8192)) / 1024.0
    except (TypeError, ValueError):
        mem_gib = 8.0
    return cpus, mem_gib, DAYTONA_DEFAULT_DISK_GIB


def daytona_batch_cost_usd(
    *,
    jobs_root: Path,
    task_ids: Collection[str],
    primary: Path,
    manifest: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Usage-based actuals for Daytona cells: per-task and batch totals.

    Sums trial wall-hours x task-declared allocation rate over every
    ``*-daytona-*`` job dir under each task's jobs dir. Trials without
    parseable timestamps are listed unscored (excluded from the total, never
    zero-filled).
    """
    per_task: dict[str, float] = {}
    unscored: list[str] = []
    trials = 0
    for task_id in task_ids:
        total = 0.0
        task_dir = jobs_root / task_id
        package_rel = str(manifest.get(task_id, {}).get("package_path", ""))
        allocation = (
            task_sandbox_allocation(primary / package_rel)
            if package_rel
            else (2.0, 8.0, DAYTONA_DEFAULT_DISK_GIB)
        )
        rate = daytona_hourly_usd(cpus=allocation[0], mem_gib=allocation[1], disk_gib=allocation[2])
        found = False
        if task_dir.is_dir():
            for job_dir in sorted(task_dir.iterdir()):
                if not job_dir.is_dir() or "-daytona-" not in job_dir.name:
                    continue
                for trial in _trial_dirs_with_results(job_dir):
                    hours = trial_wall_hours(trial)
                    if hours is None:
                        unscored.append(f"{task_id}/{job_dir.name}/{trial.name}")
                        continue
                    found = True
                    trials += 1
                    total += hours * rate
        if found:
            per_task[task_id] = total
    return {
        "per_task_usd": per_task,
        "batch_usd": sum(per_task.values()),
        "trials": trials,
        "unscored": unscored,
        "basis": (
            "daytona usage (trial wall-hours from result.json) x rate-card "
            f"cpu={DAYTONA_RATE_CPU_USD_H}/h mem={DAYTONA_RATE_MEM_GIB_USD_H}/GiB-h "
            f"disk={DAYTONA_RATE_DISK_GIB_USD_H}/GiB-h beyond {DAYTONA_DISK_FREE_GIB}GiB"
        ),
    }


# --------------------------------------------------------------------------- #
# CLI: ``evallab mimo-census run|report|record-spend``
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class CensusContext:
    """Resolved paths shared by every census verb."""

    root: Path
    receipt_dir: Path
    manifest_path: Path
    manifest_version: str
    jobs_dir: Path
    primary: Path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--manifest-version", default=None)
    parser.add_argument("--receipt-dir", type=Path, default=None)
    parser.add_argument("--jobs-dir", type=Path, default=None)


def resolve_context(args: argparse.Namespace, root: Path) -> CensusContext:
    """Resolve manifest/receipt/jobs paths for one CLI invocation."""
    from evallab.storage.paths import shared_checkout_root

    receipt_dir = _resolve(root, args.receipt_dir) if args.receipt_dir else root / RECEIPT_REL
    manifest_path = (
        _resolve(root, args.manifest)
        if args.manifest
        else root / "research/experiments/mimo-clean-v1/manifest.csv"
    )
    manifest_version = args.manifest_version or manifest_path.parent.name
    jobs_dir = (
        _resolve(root, args.jobs_dir)
        if args.jobs_dir
        else Path.home() / "Developer/eval-lab-results/2026-10-09/mimo-clean-census"
    )
    return CensusContext(
        root=root,
        receipt_dir=receipt_dir,
        manifest_path=manifest_path,
        manifest_version=manifest_version,
        jobs_dir=jobs_dir,
        primary=shared_checkout_root(root),
    )


def build_mimo_census_parser(commands: Any) -> None:
    """Register the top-level ``evallab mimo-census`` command."""
    parser = commands.add_parser(
        "mimo-census",
        help="Fleet-wide clean-set census: nop/oracle/ladder/fix-content + results.csv",
        description=__doc__.split("\n\n")[0] if __doc__ else "mimo-clean-census-v1",
    )
    sub = parser.add_subparsers(dest="mimo_census_cmd", required=True)
    _register_census_commands(sub)


def _register_census_commands(sub: Any) -> None:
    """Register the census verbs (``run``/``report``/``record-spend``/``cost``)."""
    run = sub.add_parser("run", help="Run census cells for manifest tasks")
    _add_run_args(run)
    run.set_defaults(func=_run_command)
    report = sub.add_parser("report", help="Assemble results.csv + summary from census rows")
    _add_common(report)
    report.add_argument("--cost-default-usd", type=float, default=0.0)
    report.set_defaults(func=_report_command)
    record = sub.add_parser(
        "record-spend", help="Record one batch's provider actuals in spend.jsonl"
    )
    _add_common(record)
    record.add_argument("--batch-id", required=True)
    record.add_argument("--tasks", required=True, help="comma-separated task ids in the batch")
    record.add_argument("--backend", choices=BACKENDS, required=True)
    record.add_argument("--actual-usd", type=float, required=True)
    record.add_argument("--evidence", default="", help="provider receipt path or query")
    record.set_defaults(func=_record_spend_command)
    cost = sub.add_parser("cost", help="Compute Daytona usage-based actuals and record them")
    _add_common(cost)
    cost.add_argument("--batch-id", required=True)
    cost.add_argument("--tasks", required=True, help="comma-separated task ids in the batch")
    cost.set_defaults(func=_cost_command)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("research/experiments/python-task-ledger/ledger.csv"),
        help="ledger CSV resolving pre-cleanup run packages for fix-content census",
    )
    parser.add_argument(
        "--tasks", default=None, help="comma-separated task ids (default: all built)"
    )
    parser.add_argument("--backend", choices=BACKENDS, default="docker")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--worst-case-usd-per-task", type=float, default=0.25)
    parser.add_argument(
        "--skip-fix-census", action="store_true", help="skip check (d), grading cells only"
    )


def _run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.mimo_clean import run_package_rel

    ctx = resolve_context(args, root)
    manifest = {row["task_id"]: row for row in load_manifest(ctx.manifest_path)}
    wanted = parse_task_list(args.tasks)
    selected = [
        (task_id, row)
        for task_id, row in sorted(manifest.items())
        if row["status"] == STATUS_BUILT
        and row.get("package_path")
        and (not wanted or task_id in wanted)
    ]
    if wanted:
        for task_id in wanted:
            if task_id not in manifest:
                print(f"skip {task_id}: not in manifest {ctx.manifest_path}")
    ledger: dict[str, dict[str, str]] = {}
    if not args.skip_fix_census:
        ledger_path = _resolve(root, args.ledger)
        if ledger_path.is_file():
            ledger = {row["task_id"]: row for row in load_manifest(ledger_path)}
        else:
            print(f"note: ledger {ledger_path} absent; fix-content census skipped")
    if args.backend != "docker":
        projected = len(selected) * args.worst_case_usd_per_task
        spent = slice_spent_usd(ctx.receipt_dir)
        if not fence_allows(spent_usd=spent, projected_usd=projected):
            print(
                f"refuse: slice spent ${spent:.2f} + projected ${projected:.2f} "
                f"> cap ${SLICE_CAP_USD:.2f}; narrow --tasks and retry"
            )
            return 2
        print(
            f"spend fence ok: spent ${spent:.2f} + projected ${projected:.2f} "
            f"<= ${SLICE_CAP_USD:.2f}"
        )
    rows_path = ctx.jobs_dir / ROWS_FILENAME
    ctx.jobs_dir.mkdir(parents=True, exist_ok=True)
    for task_id, row in selected:
        try:
            partial = census_task(
                task_id=task_id,
                manifest_row=row,
                primary=ctx.primary,
                jobs_root=ctx.jobs_dir,
                backend=args.backend,
                timeout_seconds=args.timeout_seconds,
                root=root,
            )
        except Exception as exc:  # noqa: BLE001 - one task never kills the batch
            print(f"error {task_id}: {type(exc).__name__}: {exc}")
            continue
        record: dict[str, Any] = blank_row(
            task_id, manifest_version=ctx.manifest_version, backend=args.backend
        )
        record["final_digest"] = row.get("final_digest", "")
        record.update(
            {
                "nop": partial["nop"],
                "oracle": partial["oracle"],
                "ladder_verdict": partial["ladder_verdict"],
                "ladder_cracking_attacks": partial["ladder_cracking_attacks"],
                "run_ids": partial["run_ids"],
            }
        )
        ledger_row = ledger.get(task_id)
        if not args.skip_fix_census and ledger_row is not None:
            try:
                fix_result = census_fix_content(
                    task_id=task_id,
                    clean_package=ctx.primary / Path(str(row["package_path"])),
                    run_package=ctx.primary / run_package_rel(ledger_row),
                    language=row.get("language", "python"),
                    scratch_root=ctx.jobs_dir / "fix-census-scratch",
                )
            except Exception as exc:  # noqa: BLE001 - probe failure is a finding
                print(f"fix-census {task_id}: {type(exc).__name__}: {exc}")
            else:
                if fix_result.get("census_locations") is not None:
                    record["census_locations"] = str(fix_result["census_locations"])
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(
            f"| {task_id} | oracle={record['oracle']} nop={record['nop']} "
            f"ladder={record['ladder_verdict']}:{record['ladder_cracking_attacks'] or '-'} "
            f"census={record['census_locations'] or 'n/a'} |"
        )
    print(f"census rows appended -> {rows_path}")
    return 0


def _report_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    ctx = resolve_context(args, root)
    rows_path = ctx.jobs_dir / ROWS_FILENAME
    if not rows_path.is_file():
        print(f"error: no census rows at {rows_path}; run `mimo-census run` first")
        return 2
    # Rows are keyed (task_id, backend): one task may carry a daytona row
    # (controls; ladder backend-unsupported) plus a docker row (full ladder).
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    with rows_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            key = (str(record["task_id"]), str(record.get("backend", "")))
            latest[key] = {str(key): value for key, value in record.items()}
    spend_path = ctx.receipt_dir / SPEND_FILENAME
    batch_cost: dict[str, float] = {}
    if spend_path.is_file():
        for line in spend_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            tasks = parse_task_list(str(entry.get("tasks", "")))
            if tasks:
                share = amortized_cost(float(entry.get("actual_usd", 0.0)), len(tasks))
                for task_id in tasks:
                    batch_cost[task_id] = share
    rows: list[dict[str, Any]] = []
    for (task_id, _backend), record in sorted(latest.items()):
        record["manifest_version"] = ctx.manifest_version
        if record.get("cost_usd", "") in ("", None):
            record["cost_usd"] = (
                f"{batch_cost[task_id]:.4f}"
                if task_id in batch_cost
                else f"{args.cost_default_usd:.2f}"
            )
        rows.append(record)
    out = write_results(rows, ctx.receipt_dir / RESULTS_FILENAME)
    summary = summarize_results(rows)
    print(f"results.csv: {summary['passed']}/{summary['total']} pass -> {out}")
    for failure in summary["failures"]:
        print(f"FAIL {failure['task_id']}: {failure['reasons']}")
    return 0 if summary["failed"] == 0 else 1


def _record_spend_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    ctx = resolve_context(args, root)
    tasks = parse_task_list(args.tasks)
    if not tasks:
        print("error: --tasks needs at least one task id")
        return 2
    path = append_spend_record(
        ctx.receipt_dir,
        {
            "batch_id": args.batch_id,
            "backend": args.backend,
            "tasks": ",".join(tasks),
            "n_tasks": len(tasks),
            "actual_usd": args.actual_usd,
            "evidence": args.evidence,
        },
    )
    total = slice_spent_usd(ctx.receipt_dir)
    print(f"recorded batch {args.batch_id}: ${args.actual_usd:.4f} -> {path}")
    print(f"slice total so far: ${total:.4f} / ${SLICE_CAP_USD:.2f}")
    return 0


def _cost_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    ctx = resolve_context(args, root)
    tasks = parse_task_list(args.tasks)
    if not tasks:
        print("error: --tasks needs at least one task id")
        return 2
    manifest = {row["task_id"]: row for row in load_manifest(ctx.manifest_path)}
    result = daytona_batch_cost_usd(
        jobs_root=ctx.jobs_dir, task_ids=tasks, primary=ctx.primary, manifest=manifest
    )
    path = append_spend_record(
        ctx.receipt_dir,
        {
            "batch_id": args.batch_id,
            "backend": "daytona",
            "tasks": ",".join(tasks),
            "n_tasks": len(tasks),
            "actual_usd": round(result["batch_usd"], 4),
            "trials": result["trials"],
            "unscored": result["unscored"],
            "evidence": result["basis"],
        },
    )
    for task_id in sorted(result["per_task_usd"]):
        print(f"daytona {task_id}: ${result['per_task_usd'][task_id]:.4f}")
    for unscored in result["unscored"]:
        print(f"unscored (excluded): {unscored}")
    total = slice_spent_usd(ctx.receipt_dir)
    print(f"recorded batch {args.batch_id}: ${result['batch_usd']:.4f} -> {path}")
    print(f"slice total so far: ${total:.4f} / ${SLICE_CAP_USD:.2f}")
    return 0


__all__ = [
    "ATTACKS",
    "BACKENDS",
    "CENSUS_VERSION",
    "JOB_PREFIX",
    "RESULTS_COLUMNS",
    "RESULTS_FILENAME",
    "ROWS_FILENAME",
    "SLICE_CAP_USD",
    "SPEND_FILENAME",
    "VERIFY_BACKEND_UNSUPPORTED",
    "VERIFY_CLEAN",
    "VERIFY_ENV_BROKEN",
    "VERIFY_GRADER_HOLE",
    "VERIFY_INFRA_FLAKE",
    "VERIFY_NEEDS_TRIAGE",
    "VERIFY_OPEN_LEAK",
    "VERIFY_ORACLE_WRONG",
    "VERIFY_UNVERIFIED",
    "acceptance_matches",
    "amortized_cost",
    "append_spend_record",
    "blank_row",
    "build_mimo_census_parser",
    "census_fix_content",
    "census_row_pass",
    "census_task",
    "daytona_batch_cost_usd",
    "daytona_hourly_usd",
    "cell_job_name",
    "fence_allows",
    "job_has_trial_errors",
    "ladder_summary",
    "load_results",
    "next_free_name",
    "nop_cell_grade",
    "oracle_cell_grade",
    "parse_junit_grade",
    "parse_task_list",
    "run_cell",
    "slice_spent_usd",
    "summarize_results",
    "summarize_trials",
    "task_sandbox_allocation",
    "trial_grade_logs",
    "trial_disk_exhausted",
    "trial_output_logs",
    "trial_tests_executed",
    "trial_wall_hours",
    "utc_now_iso",
    "verify_grade_for",
    "write_results",
]
