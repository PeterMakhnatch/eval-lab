"""Fleet-wide clean-set census (mimo-clean-census-v1).

Grades selected clean-set manifest rows and records historical and current
task/version/digest/backend/resource-policy rows in
``research/experiments/mimo-clean-census/results.csv``:

(a) nop control is 0 with runner-appropriate evidence of actual test execution;
(b) oracle control is 1 where the manifest carries a reference fix;
(c) the full 12-attack cheat ladder (``evallab.cheat_ladder.ATTACKS``,
    executed through the model-free ``cheat`` agent — payloads reused, never
    forked) grades clean, with per-attack attribution trials when a
    full-ladder trial cracks;
(d) fix-content census (``evallab.fix_content_census``) on the clean chain
    reports 0 open-leak locations for tasks with a recoverable fix.

Backends: ``docker`` ($0 parity baseline), ``modal`` (Harbor's native
ModalEnvironment), and ``daytona`` (bounded paid sandboxes). Docker and
Modal honor package networking: the shipped v3 package disables agent
network access while setup and the separate verifier remain public.
Daytona keeps its locked policy. Fresh-sandbox separate-verifier grading
comes from Harbor's own lifecycle, not a retest.
The census never rebuilds or edits immutable packages. Reporting writes its
own receipts and, when requested, updates only the clean manifest's
``verify`` grades using evidence bound to its exact current package digest.

Manifest ``verify`` grades are ``pass``, ``fail:open-leak``,
``fail:grader-hole``, ``fail:oracle-wrong``, ``env-broken``, or ``unverified``.
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
from evallab.cheat_ladder import ATTACKS, CHEAT_AGENT_VERSION
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
    "ladder_version",
    "modal_resource_policy",
)

#: Manifest ``verify`` grades owned by this census. Vocabulary: ``pass``,
#: ``fail:<class>`` (open-leak | grader-hole | oracle-wrong), ``env-broken``,
#: ``unverified``. Anything ambiguous or not fully executed (missing cells,
#: partial ladders, probe-blind fix probes, backend refusals, ``-noexec``
#: grades) stays ``unverified``; the per-check columns carry the explicit
#: classification.
VERIFY_PASS = "pass"
VERIFY_FAIL_OPEN_LEAK = "fail:open-leak"
VERIFY_FAIL_GRADER_HOLE = "fail:grader-hole"
VERIFY_FAIL_ORACLE_WRONG = "fail:oracle-wrong"
VERIFY_ENV_BROKEN = "env-broken"
VERIFY_UNVERIFIED = "unverified"
VERIFY_FAIL_CLASSES = (
    VERIFY_FAIL_OPEN_LEAK,
    VERIFY_FAIL_GRADER_HOLE,
    VERIFY_FAIL_ORACLE_WRONG,
)

#: Selectable census checks (``--checks``). ``nop``/``oracle`` are the
#: controls cells, ``ladder`` the cheat-ladder cells, ``fix`` the fix-content
#: probe (check d).
CHECKS = ("nop", "oracle", "ladder", "fix")
GRADING_CHECKS = ("nop", "oracle", "ladder")

#: Worker bounds for ``--workers`` (process workers; the cheat ladder hands
#: the attack subset through process ``os.environ`` under a global lock, so
#: threads would race). Remote backends fan out; local Docker stays serial
#: enough to never wedge the shared daemon.
DEFAULT_WORKERS = 1
MAX_REMOTE_WORKERS = 19
MAX_DOCKER_WORKERS = 2

#: Backends the census runner supports. ``modal`` runs Harbor's native
#: ModalEnvironment; ``daytona`` the bounded Daytona sandbox env. Paid
#: backends need their SDK/credentials at run time (modal: ``uv run --with
#: modal`` + ~/.modal.toml; daytona: DAYTONA_API_KEY in the environment).
BACKENDS = ("docker", "modal", "daytona")
JOB_PREFIX = "mimo-census-v1"

#: Modal resource-policy values for ``--modal-resource-policy`` (row form).
#: ``auto`` is the Harbor default (RunRequest None); ``limit`` opts into
#: cpu/memory enforcement (Modal + census app only, validated downstream).
MODAL_POLICY_AUTO = "auto"
MODAL_POLICY_LIMIT = "limit"
MODAL_RESOURCE_POLICIES = (MODAL_POLICY_AUTO, MODAL_POLICY_LIMIT)

#: Enforcement kwargs the policy maps to (mirrors build_command forwarding).
RESOURCE_ENFORCEMENT_KEYS = ("cpu_enforcement_policy", "memory_enforcement_policy")

#: Slice spend cap (USD) — see the assignment; enforced per batch.
SLICE_CAP_USD = 13.00

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


def parse_check_list(raw: str | None) -> tuple[str, ...]:
    """Parse a ``--checks`` comma set (None/empty means all checks)."""
    if raw is None or not raw.strip():
        return CHECKS
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name or name in seen:
            continue
        if name not in CHECKS:
            raise ValueError(f"unknown check {name!r}; expected one of {list(CHECKS)}")
        seen.append(name)
    if not seen:
        raise ValueError(f"empty check set; expected one of {list(CHECKS)}")
    return tuple(name for name in CHECKS if name in seen)


def resolve_worker_count(*, backend: str, requested: int | None) -> int:
    """Validate ``--workers`` against the backend bound (default 1)."""
    count = DEFAULT_WORKERS if requested is None else requested
    limit = MAX_DOCKER_WORKERS if backend == "docker" else MAX_REMOTE_WORKERS
    if count < 1 or count > limit:
        raise ValueError(
            f"workers={count} out of bounds for backend {backend!r}: "
            f"need 1..{limit} (default {DEFAULT_WORKERS})"
        )
    return count


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


_PYTEST_RESULT_LINE_RE = re.compile(r"(?m)^(?:FAILED|PASSED)\s+\S+")
_PYTEST_ERROR_LINE_RE = re.compile(r"(?m)^ERROR\s+\S*::\S+")
_PYTEST_SUMMARY_RE = re.compile(r"(?m)^.*\b\d+\s+(?:failed|passed|error)\b.*in\s+[\d.]+s.*$")
_PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|error)")
_PYTEST_COLLECTED_NONE_RE = re.compile(r"collected 0 items?")

#: v3 structured grade lines (same ``junit-grade.log`` file): the runner
#: identity line, surefire case counts for mvn/gradle, and the marker
#: verdict for runners without countable reports.
_RUNNER_LINE_RE = re.compile(r"(?m)^runner=(?P<runner>[A-Za-z-]+)\s+rc=(?P<rc>-?\d+)")
_SUREFIRE_GRADE_RE = re.compile(
    r"rc=(?P<rc>-?\d+)\s+(?P<runner>mvn|gradle)\s+surefire=\d+\s+cases=(?P<cases>\d+)\s+bad=\d+"
)
_MARKERS_GRADE_RE = re.compile(
    r"(?m)^rc=(?P<rc>-?\d+)\s+(?P<runner>[A-Za-z-]+)\s+markers=(?P<markers>True|False)"
)

#: Runner-specific case evidence in ``test_output.log`` (mirrors the
#: verifier's own marker checks, widened so failed cases still prove
#: execution — a failing test ran; only no-tests/collection errors stay
#: noexec). Custom/node-run/make-style fallbacks have no case evidence:
#: an exit-code rc alone is never execution.
_GO_CASE_RE = re.compile(r"(?m)^--- (?:FAIL|PASS)|^=== RUN\s+\S|panic:")
_GO_PACKAGE_RE = re.compile(r"(?m)^(ok|FAIL)\s+\S+")
_GO_NOCASE_RE = re.compile(r"no test files|no tests to run|\[build failed\]")
_JEST_TESTS_RE = re.compile(r"Tests:\s+([^\n]+)")
_JEST_NO_TESTS_RE = re.compile(r"No tests found")
_VITEST_SUMMARY_RE = re.compile(r"(?:Test Files|Tests)\s+([^\n]+)")
_MOCHA_COUNT_RE = re.compile(r"(\d+)\s+(?:passing|failing|pending)")
_TAP_OK_RE = re.compile(r"(?m)^(ok|not ok)\b")
_TAP_PLAN_RE = re.compile(r"(?m)^1\.\.(\d+)")
_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) tests?")
_PHPUNIT_OK_RE = re.compile(r"(?m)^OK\s*\(|FAILURES!|ERRORS!")
_PHPUNIT_TESTS_RE = re.compile(r"Tests:\s*(\d+)")
_PHPUNIT_NO_TESTS_RE = re.compile(r"No tests executed")
_RSPEC_SUMMARY_RE = re.compile(r"(\d+) examples?, (\d+) failures?")
_CARGO_RESULT_RE = re.compile(r"test result:\s*\S+\.\s*(\d+) passed(?:;\s*(\d+) failed)?")
_KARMA_EXECUTED_RE = re.compile(r"Executed\s+([1-9]\d*)\s+of")
_JASMINE_SUMMARY_RE = re.compile(r"(\d+) specs?, (\d+) failures?")
_INT_RE = re.compile(r"\d+")


def trial_output_logs(trial_dir: Path, name: str) -> list[str]:
    """Read all verifier ``name`` logs under one trial dir (missing -> skip)."""
    texts: list[str] = []
    for log_path in sorted(trial_dir.rglob(name)):
        try:
            texts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def trial_runner(trial_dir: Path) -> str:
    """Test-runner family for one trial (``pytest`` when unrecorded).

    Reads the verifier-owned ``runner.txt`` (``RUNNER=<name>``), falling back
    to the ``runner=`` identity line in the structured grade log. Legacy
    pytest-only cells record neither and default to ``pytest``.
    """
    for log_path in sorted(trial_dir.rglob("runner.txt")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^RUNNER=(\S+)", text)
        if match:
            return match.group(1)
    for text in trial_output_logs(trial_dir, "junit-grade.log"):
        match = _RUNNER_LINE_RE.search(text)
        if match:
            return match.group("runner")
    return "pytest"


def _ints_total(text: str) -> int:
    return sum(int(item) for item in _INT_RE.findall(text))


def _runner_output_executed(runner: str, output: str) -> bool:
    """Whether runner output shows test cases ran (failures count as ran).

    No-tests and collection errors stay False; frameworks without case
    evidence (custom/node-run/make-style exit-code fallbacks) are always
    False — an rc alone never proves execution.
    """
    if runner == "go-test":
        if _GO_CASE_RE.search(output):
            return True
        return _GO_PACKAGE_RE.search(output) is not None and not _GO_NOCASE_RE.search(output)
    if runner == "jest":
        if _JEST_NO_TESTS_RE.search(output):
            return False
        return any(_ints_total(segment) > 0 for segment in _JEST_TESTS_RE.findall(output))
    if runner == "vitest":
        return any(_ints_total(segment) > 0 for segment in _VITEST_SUMMARY_RE.findall(output))
    if runner == "mocha":
        return sum(int(count) for count in _MOCHA_COUNT_RE.findall(output)) > 0
    if runner in ("tap", "ava", "node-test"):
        if _TAP_OK_RE.search(output):
            return True
        return any(int(count) > 0 for count in _TAP_PLAN_RE.findall(output))
    if runner == "unittest":
        return any(int(count) >= 1 for count in _UNITTEST_RAN_RE.findall(output))
    if runner == "phpunit":
        if _PHPUNIT_NO_TESTS_RE.search(output):
            return False
        if _PHPUNIT_OK_RE.search(output):
            return True
        return any(int(count) > 0 for count in _PHPUNIT_TESTS_RE.findall(output))
    if runner == "rspec":
        return any(int(examples) > 0 for examples, _ in _RSPEC_SUMMARY_RE.findall(output))
    if runner == "cargo-test":
        return any(
            int(passed) + int(failed or 0) > 0
            for passed, failed in _CARGO_RESULT_RE.findall(output)
        )
    if runner == "karma":
        return _KARMA_EXECUTED_RE.search(output) is not None or "FAILED" in output
    if runner == "jasmine":
        return any(int(specs) > 0 for specs, _ in _JASMINE_SUMMARY_RE.findall(output))
    if runner == "pytest":
        if _PYTEST_COLLECTED_NONE_RE.search(output):
            return False
        if _PYTEST_RESULT_LINE_RE.search(output) or _PYTEST_ERROR_LINE_RE.search(output):
            return True
        for line in _PYTEST_SUMMARY_RE.findall(output):
            counts = {kind: int(count) for count, kind in _PYTEST_COUNT_RE.findall(line)}
            if counts.get("failed", 0) > 0 or counts.get("passed", 0) > 0:
                return True
        return False
    return False


def trial_tests_executed(trial_dir: Path) -> bool:
    """Whether a trial has evidence the verifier actually ran test cases.

    Three tiers: countable reports (JUnit ``cases > 0``, surefire
    ``cases > 0``), the verifier's own ``markers=True`` verdict, then
    runner-specific case evidence in ``test_output.log`` for the trial's
    recorded runner (go-test/jest/vitest/mocha/tap/ava/node-test/karma/
    jasmine/cargo-test/rspec/phpunit/unittest/pytest). ``named`` is
    diagnostic only — some suites report cases without parsable names.
    Fallback runners (custom/node-run/make-style exit-code grading) carry
    no case evidence, so an rc alone stays noexec, as do no-tests and
    collection-error outputs.
    """
    if any(grade["cases"] > 0 for grade in trial_grade_logs(trial_dir)):
        return True
    grade_texts = trial_output_logs(trial_dir, "junit-grade.log")
    for text in grade_texts:
        surefire = _SUREFIRE_GRADE_RE.search(text)
        if surefire and int(surefire.group("cases")) > 0:
            return True
        markers = _MARKERS_GRADE_RE.search(text)
        if markers and markers.group("markers") == "True":
            return True
    runner = trial_runner(trial_dir)
    return any(
        _runner_output_executed(runner, text)
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
    census_locations: int | str | None,
    has_reference_fix: bool = True,
) -> str:
    """Map per-check signals to one manifest ``verify`` grade.

    Vocabulary: ``pass``, ``fail:<class>`` (open-leak | grader-hole |
    oracle-wrong), ``env-broken``, ``unverified``. Anything not fully
    executed (missing/unscored/partial cells, backend refusals, probe-blind
    fix probes, ``-noexec`` grades, blank unselected checks) stays
    ``unverified``; the per-check result columns carry the explicit
    classification for the receipt taxonomy. On a known-patch task a blank
    fix cell (``""`` — fix phase not yet run) stays ``unverified`` until
    the fix phase completes; tasks with no reference fix report explicit
    ``"n/a"`` and can pass without it.
    """
    if (
        nop in ("", "missing", "unscored", "backend-unsupported")
        or oracle in ("", "missing", "unscored", "backend-unsupported")
        or ladder_verdict in ("", "missing", "unscored", "partial", "backend-unsupported")
    ):
        return VERIFY_UNVERIFIED
    if census_locations == "probe-blind":
        return VERIFY_UNVERIFIED
    if has_reference_fix:
        if census_locations in ("", None):
            return VERIFY_UNVERIFIED
    elif census_locations in ("", None, "n/a"):
        census_locations = "0"
    leak_count = 0
    if isinstance(census_locations, int):
        leak_count = census_locations
    elif isinstance(census_locations, str) and census_locations not in ("",):
        try:
            leak_count = int(census_locations)
        except ValueError:
            return VERIFY_UNVERIFIED
    if leak_count > 0:
        return VERIFY_FAIL_OPEN_LEAK
    if ladder_verdict == "cracked":
        return VERIFY_FAIL_GRADER_HOLE
    if nop == "setup-fail" or oracle == "setup-fail":
        return VERIFY_ENV_BROKEN
    if nop != "0":
        if nop.startswith("fail:"):
            return VERIFY_ENV_BROKEN
        return VERIFY_UNVERIFIED
    if oracle not in ("1", "n/a"):
        if oracle.startswith("fail:"):
            return VERIFY_FAIL_ORACLE_WRONG
        return VERIFY_UNVERIFIED
    return VERIFY_PASS


def census_row_pass(row: Mapping[str, Any], *, has_reference_fix: bool = True) -> bool:
    """Whether a results row meets census acceptance (all four checks).

    On a known-patch task the fix phase must have completed with a clean
    probe (``census_locations == 0``); a blank fix cell never passes.
    Tasks with no reference fix pass with explicit ``"n/a"`` (or a blank
    cell from rows written before the explicit marker existed).
    """
    oracle_ok = row.get("oracle") in ("1", "n/a")
    nop_ok = row.get("nop") == "0"
    ladder_ok = row.get("ladder_verdict") == "clean" and not row.get("ladder_cracking_attacks")
    census_raw = row.get("census_locations")
    if has_reference_fix:
        census_ok = str(census_raw) == "0"
    else:
        census_ok = census_raw in ("", None, "n/a") or str(census_raw) == "0"
    return bool(oracle_ok and nop_ok and ladder_ok and census_ok)


#: Result columns holding per-check evidence (merged across phases, never
#: clobbered by a later partial row carrying blanks for unselected checks).
CHECK_COLUMNS = (
    "nop",
    "oracle",
    "ladder_verdict",
    "ladder_cracking_attacks",
    "census_locations",
    "ladder_version",
)

#: Check-column sentinels meaning "no evidence" when combining rows across
#: phases/backends: a blank (unselected check) or ``missing`` (cell absent)
#: never overwrites real evidence, and never counts as evidence itself.
NO_EVIDENCE = ("", None, "missing")


def merge_census_rows(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a later partial row into an earlier one (report-side).

    Per-check columns only move forward: blank/unselected incoming cells
    never overwrite prior nop/oracle/ladder/fix evidence. ``run_ids`` takes
    the ordered union; identity columns keep the newest non-blank value.
    """
    merged = dict(existing)
    for key in CHECK_COLUMNS:
        value = incoming.get(key)
        if value not in NO_EVIDENCE:
            merged[key] = value
    for key in ("manifest_version", "final_digest", "backend", "cost_usd"):
        value = incoming.get(key)
        if value not in ("", None):
            merged[key] = value
    seen: list[str] = []
    for chunk in (str(existing.get("run_ids", "")), str(incoming.get("run_ids", ""))):
        for run_id in chunk.split(","):
            if run_id and run_id not in seen:
                seen.append(run_id)
    merged["run_ids"] = ",".join(seen)
    return merged


def combine_task_rows(
    rows: Collection[Mapping[str, Any]],
    *,
    has_reference_fix: bool = True,
    final_digest: str | None = None,
) -> dict[str, Any] | None:
    """Aggregate one task's per-backend rows into a single task status.

    Only rows sharing one non-blank ``final_digest`` combine (controls row
    from Daytona plus ladder/fix rows from Modal, for example); a blank or
    ``missing`` check cell never stands in for evidence. When
    ``final_digest`` is given, only that exact generation combines (the
    manifest's current digest at report time) — never a most-evidence older
    winner. Without it, a single generation combines; multiple generations
    refuse (None) rather than guess. ``has_reference_fix`` (from the
    manifest, never assumed from old rows) decides whether a blank fix cell
    blocks ``pass``. Returns the combined record with a ``verify`` grade, or
    None when no row carries a combinable digest.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        digest = str(row.get("final_digest", ""))
        if digest:
            groups.setdefault(digest, []).append(row)
    if not groups:
        return None

    if final_digest is not None:
        group = groups.get(final_digest)
        if not group:
            return None
        digest = final_digest
    elif len(groups) == 1:
        digest, group = next(iter(groups.items()))
    else:
        return None
    group = sorted(group, key=lambda row: str(row.get("backend", "")))
    combined: dict[str, Any] = {
        "task_id": str(group[0].get("task_id", "")),
        "final_digest": digest,
        "backends": ",".join(
            dict.fromkeys(str(row.get("backend", "")) for row in group if row.get("backend"))
        ),
    }
    for key in CHECK_COLUMNS:
        combined[key] = next((row.get(key) for row in group if row.get(key) not in NO_EVIDENCE), "")
    seen_runs: list[str] = []
    for row in group:
        for run_id in str(row.get("run_ids", "")).split(","):
            if run_id and run_id not in seen_runs:
                seen_runs.append(run_id)
    combined["run_ids"] = ",".join(seen_runs)
    combined["verify"] = verify_grade_for(
        nop=str(combined["nop"]),
        oracle=str(combined["oracle"]),
        ladder_verdict=str(combined["ladder_verdict"]),
        census_locations=combined["census_locations"],
        has_reference_fix=has_reference_fix,
    )
    return combined


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
    """Whether accounted spend plus the reserved batch projection fits the cap."""
    return spent_usd + projected_usd <= cap_usd


def amortized_cost(batch_actual_usd: float, n_tasks: int) -> float:
    """Per-task cost share of one recorded batch total (0 when empty)."""
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
        "ladder_version": "",
        "modal_resource_policy": MODAL_POLICY_AUTO,
    }


def normalize_modal_resource_policy(raw: str | None) -> str | None:
    """Normalize a ``--modal-resource-policy`` value (None = auto default)."""
    if raw is None or str(raw).strip().lower() in ("", MODAL_POLICY_AUTO):
        return None
    if str(raw).strip().lower() == MODAL_POLICY_LIMIT:
        return MODAL_POLICY_LIMIT
    raise ValueError(
        f"unknown modal resource policy {raw!r}; expected one of {list(MODAL_RESOURCE_POLICIES)}"
    )


def policy_row_value(modal_resource_policy: str | None) -> str:
    """Row form of a normalized policy (None -> ``"auto"``)."""
    return MODAL_POLICY_LIMIT if modal_resource_policy == MODAL_POLICY_LIMIT else MODAL_POLICY_AUTO


def expected_resource_kwargs(modal_resource_policy: str | None) -> dict[str, str]:
    """Enforcement kwargs a policy implies (auto implies none recorded)."""
    if modal_resource_policy == MODAL_POLICY_LIMIT:
        return {key: MODAL_POLICY_LIMIT for key in RESOURCE_ENFORCEMENT_KEYS}
    return {}


def recorded_resource_kwargs(candidate: Path) -> dict[str, str]:
    """Enforcement kwargs recorded for a cell (config + command tokens).

    Reads ``config.json`` ``environment.kwargs`` primarily, filling gaps
    from ``lab-metadata.json`` ``--environment-kwarg key=value`` tokens.
    Absent everywhere means the Harbor default (auto).
    """
    recorded: dict[str, str] = {}
    try:
        payload = json.loads((candidate / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict):
        environment = payload.get("environment")
        if isinstance(environment, dict):
            kwargs = environment.get("kwargs")
            if isinstance(kwargs, dict):
                for key in RESOURCE_ENFORCEMENT_KEYS:
                    value = kwargs.get(key)
                    if value is not None:
                        recorded[key] = str(value)
    command = _job_lab_metadata(candidate).get("command")
    if isinstance(command, list):
        for index, token in enumerate(command):
            if token == "--environment-kwarg" and index + 1 < len(command):
                pair = str(command[index + 1])
                key, _, value = pair.partition("=")
                if key in RESOURCE_ENFORCEMENT_KEYS and key not in recorded and value:
                    recorded[key] = value
    return recorded


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


def summarize_results(
    rows: Collection[Mapping[str, Any]],
    *,
    has_reference_fix: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Pass-rate summary plus per-signal failure buckets for the receipt.

    ``has_reference_fix`` maps task ids to whether the manifest carries a
    reference fix; without it every task is graded strictly (a blank fix
    cell never passes), so partial-phase rows can never slip through.
    """
    rows = list(rows)
    buckets: dict[str, int] = {}
    failures: list[dict[str, str]] = []
    for row in rows:
        task_id = str(row.get("task_id", ""))
        fix_known = has_reference_fix.get(task_id, True) if has_reference_fix else True
        if census_row_pass(row, has_reference_fix=fix_known):
            buckets["pass"] = buckets.get("pass", 0) + 1
            continue
        if "backend-unsupported" in (
            str(row.get("nop", "")),
            str(row.get("oracle", "")),
            str(row.get("ladder_verdict", "")),
        ):
            buckets["backend-unsupported"] = buckets.get("backend-unsupported", 0) + 1
            continue
        if any(row.get(key) in ("", None) for key in ("nop", "oracle", "ladder_verdict")):
            # Phased row: some checks never ran in this backend's phase.
            # The task-level verdict comes from combine_task_rows; a
            # partial row alone is neither pass nor failure.
            buckets["partial"] = buckets.get("partial", 0) + 1
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
        if fix_known:
            if census_raw != "0":
                reasons.append(f"census={census_raw or 'pending'}")
        elif census_raw not in ("", "0", "n/a"):
            reasons.append(f"census={census_raw}")
        failures.append({"task_id": task_id, "reasons": "; ".join(reasons)})
    return {
        "total": len(rows),
        "passed": buckets.get("pass", 0),
        "failed": buckets.get("fail", 0),
        "backend_unsupported": buckets.get("backend-unsupported", 0),
        "partial": buckets.get("partial", 0),
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


#: Modal app name attributing census grading cells to this slice's exact
#: billing (approved narrow app_name; fix probes use a separate app owned by
#: the fix-content lane). Only set for ``backend="modal"``.
MODAL_CENSUS_APP_NAME = "mimo-clean-census"


def _job_lab_metadata(candidate: Path) -> dict[str, Any]:
    """Parsed ``lab-metadata.json`` for a cell dir ({} when absent)."""
    try:
        payload = json.loads((candidate / "lab-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_agent_name(candidate: Path) -> str | None:
    """Agent recorded in a cell's ``config.json`` (None when absent)."""
    try:
        payload = json.loads((candidate / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if isinstance(agents, list) and agents and isinstance(agents[0], dict):
        name = agents[0].get("name")
        return str(name) if name else None
    return None


def _job_backend_name(candidate: Path) -> str | None:
    """Backend recorded in a cell's ``lab-metadata.json`` (None when absent)."""
    command = _job_lab_metadata(candidate).get("command")
    if not isinstance(command, list):
        return None
    for index, token in enumerate(command):
        if token == "--env" and index + 1 < len(command):
            return str(command[index + 1])
    return None


def current_ladder_version() -> str:
    """Current ladder generation, independent of the optional Harbor SDK."""
    return CHEAT_AGENT_VERSION


def cheat_cell_ladder_version(cheat_dir: Path | None) -> str | None:
    """Uniform ``attempts.json`` version across a ladder cell's trials.

    None when the cell is absent, has no trials, or mixes generations —
    none of which are reusable as the current ladder.
    """
    if cheat_dir is None or not cheat_dir.is_dir():
        return None
    versions: set[str] = set()
    for trial in _trial_dirs_with_results(cheat_dir):
        for name in ("agent/cheat/attempts.json", "cheat/attempts.json"):
            path = trial / name
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            version = payload.get("version") if isinstance(payload, dict) else None
            if not version:
                return None
            versions.add(str(version))
            break
        else:
            return None
    if len(versions) != 1:
        return None
    return next(iter(versions))


_ATTEMPT_RE = re.compile(r"^(?P<base>.+)-attempt(?P<n>\d+)$")


def cell_reusable(
    candidate: Path,
    *,
    expected_digest: str,
    expected_agent: str,
    expected_backend: str,
    expected_ladder_version: str | None = None,
    expected_resource_kwargs: Mapping[str, str] | None = None,
) -> bool:
    """Whether a completed cell dir may be reused verbatim.

    A cell is bound to the package digest it staged
    (``lab-metadata.json`` ``task_staging.source_package_digest``), the
    agent in ``config.json``, the backend in ``lab-metadata.json``, and the
    recorded cpu/memory enforcement policy. Ladder cells additionally bind
    to the current cheat-agent version. Anything unverifiable or from
    another generation returns False: the caller launches a fresh
    ``-attemptN`` cell instead of reusing. AUTO and LIMIT policies never
    share cells.
    """
    if not expected_digest or not candidate.is_dir():
        return False
    staging = _job_lab_metadata(candidate).get("task_staging")
    staged_digest = staging.get("source_package_digest") if isinstance(staging, dict) else None
    if staged_digest != expected_digest:
        return False
    if _job_agent_name(candidate) != expected_agent:
        return False
    if _job_backend_name(candidate) != expected_backend:
        return False
    if (
        expected_ladder_version is not None
        and cheat_cell_ladder_version(candidate) != expected_ladder_version
    ):
        return False
    if expected_resource_kwargs is not None:
        recorded = recorded_resource_kwargs(candidate)
        wanted = {
            key: expected_resource_kwargs[key]
            for key in RESOURCE_ENFORCEMENT_KEYS
            if key in expected_resource_kwargs
        }
        actual = {
            key: recorded[key]
            for key in RESOURCE_ENFORCEMENT_KEYS
            if recorded.get(key) not in (None, "", MODAL_POLICY_AUTO)
        }
        if actual != wanted:
            return False
    return True


def scannable_cell_dirs(task_jobs: Path, base: str) -> list[Path]:
    """Existing dirs for one cell base, newest attempt first.

    Covers the base name plus every ``base-attemptN`` cell: a base-only
    lookup never reuses a valid newer attempt, so reuse scans all of them.
    """
    found: list[tuple[int, Path]] = []
    if not task_jobs.is_dir():
        return []
    for child in sorted(task_jobs.iterdir(), key=lambda entry: entry.name):
        if not child.is_dir():
            continue
        if child.name == base:
            found.append((1, child))
            continue
        match = _ATTEMPT_RE.match(child.name)
        if match and match.group("base") == base:
            found.append((int(match.group("n")), child))
    found.sort(key=lambda item: item[0], reverse=True)
    return [child for _, child in found]


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
    modal_resource_policy: str | None = None,
    root: Path,
) -> Path:
    """Run one census cell through Harbor; return the job dir.

    Completed job dirs (every trial rewarded) are reused verbatim, never
    clobbered; anything else launches under the next free ``-attemptN`` name.
    ``attacks`` selects the cheat-ladder subset (None = controls).
    ``egress_lock`` overrides the backend default (None = resolve).
    ``modal_resource_policy`` is None (auto default) or ``"limit"`` (Modal
    census-app only; anything else raises here before dispatch).
    """
    import os

    from evallab.cheat import _CHEAT_ENV_LOCK
    from evallab.execution_contracts import CHEAT_ATTACKS_ENV_VAR, RunRequest
    from evallab.queue import Executor

    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")
    if modal_resource_policy not in (None, MODAL_POLICY_LIMIT):
        raise ValueError(
            f"unknown modal resource policy {modal_resource_policy!r}; "
            "expected None (auto) or 'limit'"
        )
    if modal_resource_policy is not None and backend != "modal":
        raise ValueError("modal_resource_policy requires the Modal backend")
    request_kwargs: dict[str, Any] = {
        "task": package,
        "agent": agent,
        "name": name,
        "jobs_dir": jobs_dir,
        "environment": backend,
        "model": None,
        "concurrency": 1,
        "attempts": 1,
        "timeout_seconds": timeout_seconds,
        "allow_billable": backend != "docker",
        "egress_lock": egress_lock,
        "modal_resource_policy": modal_resource_policy,
    }
    if backend == "modal":
        # Exact-slice billing attribution (approved narrow app_name).
        request_kwargs["modal_app_name"] = MODAL_CENSUS_APP_NAME
    request = RunRequest(**request_kwargs)
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
    checks: Collection[str] = GRADING_CHECKS,
    modal_resource_policy: str | None = None,
) -> dict[str, Any]:
    """Run selected grading cells for one manifest row; return partial results.

    ``checks`` selects among ``nop``/``oracle``/``ladder`` (default all
    three); unselected checks launch nothing and report blank grades, so a
    controls-only phase never touches ladder cells and vice versa. Fix-content
    census (check ``fix``) runs separately via :func:`census_fix_content`.
    Completed cells are reused only when bound to this package digest,
    agent/backend, resource policy, and (ladder cells) the current ladder
    version; reuse scans the base plus every ``-attemptN`` cell newest-first
    so a valid newer attempt is never skipped. Anything else launches under
    the next free ``-attemptN`` name. Per-attack attribution trials launch
    when a full-ladder trial cracks.
    """
    from evallab.execution_contracts import CHEAT_AGENT

    wanted = tuple(checks)
    unknown = [name for name in wanted if name not in GRADING_CHECKS]
    if unknown:
        raise ValueError(
            f"unknown grading checks {unknown}; expected subset of {list(GRADING_CHECKS)}"
        )
    if modal_resource_policy not in (None, MODAL_POLICY_LIMIT):
        raise ValueError(
            f"unknown modal resource policy {modal_resource_policy!r}; "
            "expected None (auto) or 'limit'"
        )
    task_jobs = jobs_root / task_id
    task_jobs.mkdir(parents=True, exist_ok=True)
    package = primary / Path(str(manifest_row["package_path"]))
    has_fix = str(manifest_row.get("reference_fix", "")) not in ("", "none")
    expected_digest = str(manifest_row.get("final_digest", ""))
    expected_kwargs = expected_resource_kwargs(modal_resource_policy)

    def _reuse_scored(base: str, agent: str) -> Path | None:
        for candidate in scannable_cell_dirs(task_jobs, base):
            rewards = summarize_trials(candidate)
            if not rewards or any(reward is None for reward in rewards):
                continue
            if not cell_reusable(
                candidate,
                expected_digest=expected_digest,
                expected_agent=agent,
                expected_backend=backend,
                expected_resource_kwargs=expected_kwargs,
            ):
                continue
            return candidate
        return None

    def _launch(base: str, agent: str, attacks: str | None) -> Path:
        return run_cell(
            package=package,
            agent=agent,
            name=next_free_name(task_jobs, base),
            jobs_dir=task_jobs,
            backend=backend,
            timeout_seconds=timeout_seconds,
            attacks=attacks,
            modal_resource_policy=modal_resource_policy,
            root=root,
        )

    oracle_dir: Path | None = None
    oracle_grade = ""
    if "oracle" in wanted:
        if not has_fix:
            oracle_grade = "n/a"
        else:
            base = cell_job_name(task_id=task_id, backend=backend, cell="oracle")
            oracle_dir = _reuse_scored(base, "oracle") or _launch(base, "oracle", None)
            oracle_grade = oracle_cell_grade(oracle_dir, has_reference_fix=True)
    nop_dir: Path | None = None
    nop_grade = ""
    if "nop" in wanted:
        base = cell_job_name(task_id=task_id, backend=backend, cell="nop")
        nop_dir = _reuse_scored(base, "nop") or _launch(base, "nop", None)
        nop_grade = nop_cell_grade(nop_dir)
    cheat_dir: Path | None = None
    ladder_version = ""
    summary: dict[str, Any] = {"verdict": "", "executed": [], "cracking": []}
    run_ids: list[str] = []
    if nop_dir is not None:
        run_ids.append(nop_dir.name)
    if oracle_dir is not None:
        run_ids.append(oracle_dir.name)
    if "ladder" in wanted:
        ladder_generation = current_ladder_version()
        base = cell_job_name(task_id=task_id, backend=backend, cell="cheat")
        for candidate in scannable_cell_dirs(task_jobs, base):
            if _trial_dirs_with_results(candidate) and cell_reusable(
                candidate,
                expected_digest=expected_digest,
                expected_agent=CHEAT_AGENT,
                expected_backend=backend,
                expected_ladder_version=ladder_generation,
                expected_resource_kwargs=expected_kwargs,
            ):
                cheat_dir = candidate
                break
        if cheat_dir is None:
            # Cheat cells use the backend default lock: the cheat agent is outside
            # every egress-lock set, so locked backends refuse it at dispatch
            # (Daytona+MiMo additionally refuses explicit unlocked). A refusal is
            # recorded as backend-unsupported, never a failure; cheat coverage
            # for those tasks comes from a backend that admits it (docker now,
            # Modal once mtime-normalize@2 unblocks it) as a separate results row.
            try:
                cheat_dir = _launch(base, CHEAT_AGENT, "")
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
        if cheat_dir is not None:
            run_ids.append(cheat_dir.name)
            ladder_version = cheat_cell_ladder_version(cheat_dir) or ""
        if summary["verdict"] == "cracked" and "full-ladder-unattributed" in summary["cracking"]:
            for attack in ATTACKS:
                attack_base = cell_job_name(
                    task_id=task_id, backend=backend, cell=f"cheat-{attack}"
                )
                attack_dir: Path | None = None
                for attack_candidate in scannable_cell_dirs(task_jobs, attack_base):
                    if _trial_dirs_with_results(attack_candidate) and cell_reusable(
                        attack_candidate,
                        expected_digest=expected_digest,
                        expected_agent=CHEAT_AGENT,
                        expected_backend=backend,
                        expected_ladder_version=ladder_generation,
                        expected_resource_kwargs=expected_kwargs,
                    ):
                        attack_dir = attack_candidate
                        break
                if attack_dir is None:
                    attack_dir = _launch(attack_base, CHEAT_AGENT, attack)
                run_ids.append(attack_dir.name)
            assert cheat_dir is not None  # cracked verdict implies a cheat dir
            summary = ladder_summary(cheat_dir)
            for run_id in run_ids:
                if run_id.startswith(
                    cell_job_name(task_id=task_id, backend=backend, cell="cheat-")
                ):
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
    return {
        "nop": nop_grade,
        "oracle": oracle_grade,
        "ladder_verdict": summary["verdict"],
        "ladder_cracking_attacks": ",".join(summary["cracking"]),
        "ladder_executed": ",".join(summary["executed"]),
        "ladder_version": ladder_version,
        "run_ids": ",".join(run_ids),
    }


def fix_probe_egress_lock(clean_package: Path) -> bool:
    """Egress lock for fix probes from the shipped package network contract.

    Reads the derived clean package ``task.toml`` ``[environment]``
    ``network_mode``: public tasks probe unlocked (``False``) so the probe
    matches package semantics instead of default-locking them; every other
    mode (``none``/``no-network``/future locked modes) and any unreadable
    contract probe locked (``True``, fail closed).
    """
    try:
        document = tomllib.loads((clean_package / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return True
    if not isinstance(document, dict):
        return True
    environment = document.get("environment")
    if not isinstance(environment, dict):
        return True
    return str(environment.get("network_mode", "")) != "public"


def probe_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Completion evidence extract of a probe result row (meta provenance)."""
    return {
        key: row.get(key) for key in ("setup_rc", "ready", "probe_rc", "scan_complete", "scan_rc")
    }


def probe_completion(row: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Whether a probe result row is a completed probe run.

    Complete needs ``setup_rc == "0"``, ``ready == "yes"``,
    ``probe_rc == "0"``, and scan evidence (``scan_complete`` true, or a
    ``scan_rc`` of 0/1). Anything else returns False with an explicit
    reason — an incomplete probe never yields a clean bill (never 0).
    """
    if not isinstance(row, Mapping) or not row:
        return False, "no probe result"
    if str(row.get("setup_rc", "")) != "0":
        return False, f"setup_rc={row.get('setup_rc', '') or 'missing'}"
    if str(row.get("ready", "")) != "yes":
        return False, "ready sentinel missing"
    if str(row.get("probe_rc", "")) != "0":
        return False, f"probe_rc={row.get('probe_rc', '') or 'missing'}"
    complete = row.get("scan_complete")
    if complete is True or str(complete).lower() == "true":
        return True, ""
    try:
        if int(str(row.get("scan_rc", ""))) in (0, 1):
            return True, ""
    except (TypeError, ValueError):
        pass
    return False, f"scan incomplete (scan_rc={row.get('scan_rc', '') or 'missing'})"


def _invoke_probe(
    image: str,
    workdir: str,
    fix_sha: str,
    stage_dir: Path,
    out_dir: Path,
    *,
    backend: str = "docker",
    egress_lock: bool = True,
) -> None:
    """Run one fix-content probe on ``backend`` with the package egress lock.

    ``backend``/``egress_lock`` are owned by the fix-content lane (Docker
    default; ``modal`` replays the staged probe remotely with
    ``block_network=egress_lock``). Daytona never relaxes its standing
    policy: callers clamp ``egress_lock`` to True for it.
    """
    from evallab.fix_content_census import run_probe

    run_probe(image, workdir, fix_sha, stage_dir, out_dir, backend=backend, egress_lock=egress_lock)


def _known_patch_census(
    *,
    task_id: str,
    clean_package: Path,
    run_package: Path,
    language: str,
    scratch: Path,
    image: str,
    workdir: str,
    clean_setup_sha: str,
    patch_text: str,
    patch_sha: str,
    patterns: list[str],
    backend: str = "docker",
    egress_lock: bool = True,
) -> dict[str, Any]:
    """Probe both published and shipped setups with known-patch patterns.

    Both setups are measured even when the published positive control is
    blind or incomplete. A clean scan can establish zero locations only
    with a complete, positive published control; neither blind nor
    incomplete probes establish cleanliness. ``egress_lock`` carries the
    shipped package's setup network contract for both probes.
    """
    from evallab.fix_content_census import (
        collect_result,
        non_test_files,
        parse_diff_added_lines,
        stage_probe,
    )

    image12 = image.split("@sha256:")[-1][:12] if "@sha256:" in image else image[-12:]
    files = non_test_files(list(parse_diff_added_lines(patch_text)))
    precomputed: dict[str, Any] = {
        "patterns": patterns,
        "files": files,
        "fix_source": f"known-patch:{task_id}",
    }
    setup_source = run_package / "environment" / "setup"
    published_stage = scratch / "stage-published"
    published_out = scratch / "out-published"
    stage_probe(published_stage, setup_source, None, precomputed=precomputed)
    _invoke_probe(
        image, workdir, "", published_stage, published_out, backend=backend, egress_lock=egress_lock
    )
    published_row = collect_result(task_id, language, image12, "published", published_out)
    published_done, published_why = probe_completion(published_row)
    published_positive = bool(published_row.get("hits_total")) or (
        published_row.get("open_leak") == "yes"
    )
    if not clean_setup_sha:
        return {
            "census_locations": None,
            "reason": "clean package has no setup.sh",
            "detail": {"published": published_row},
        }
    clean_source = clean_package / "environment" / "setup"
    clean_stage = scratch / "stage-clean"
    clean_out = scratch / "out-clean"
    stage_probe(clean_stage, clean_source, None, precomputed=precomputed)
    _invoke_probe(
        image, workdir, "", clean_stage, clean_out, backend=backend, egress_lock=egress_lock
    )
    clean_row = collect_result(task_id, language, image12, "clean", clean_out)
    clean_done, clean_why = probe_completion(clean_row)
    probe_blind = published_done and not published_positive
    meta_path = scratch / "census.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "patch_sha": patch_sha,
                "extract_status": "known-patch",
                "clean_setup": "package environment/setup/setup.sh",
                "clean_setup_sha256": clean_setup_sha,
                "image": image,
                "backend": backend,
                "egress_lock": egress_lock,
                "probe_blind": probe_blind,
                "published": published_row,
                "published_complete": probe_evidence(published_row),
                "clean": clean_row,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not published_done:
        return {
            "census_locations": None,
            "reason": f"published probe incomplete: {published_why}",
            "detail": {"published": published_row, "clean": clean_row},
        }
    if not clean_done:
        return {
            "census_locations": None,
            "reason": f"clean probe incomplete: {clean_why}",
            "detail": {"published": published_row, "clean": clean_row},
        }
    if probe_blind:
        return {"census_locations": "probe-blind", "reason": "probe-blind", "detail": clean_row}
    try:
        locations = int(clean_row.get("hits_total", ""))
    except (TypeError, ValueError):
        return {
            "census_locations": None,
            "reason": "clean hits_total unparseable",
            "detail": clean_row,
        }
    return {"census_locations": locations, "reason": "", "detail": clean_row}


def census_fix_content(
    *,
    task_id: str,
    clean_package: Path,
    run_package: Path,
    language: str,
    scratch_root: Path,
    reference_fix: Path | None = None,
    backend: str = "docker",
) -> dict[str, Any]:
    """Fix-content census for one task (check d; Docker or supported remote probe).

    Two modes. Known-patch mode (a manifest ``reference_fix`` patch with
    distinctive non-test lines): patterns seed both probes directly — the
    published setup is the positive control (zero hits there reports
    ``probe-blind``), the ACTUAL clean package setup is what ships; this
    path forwards ``backend`` to the probe runner for fix-only remote
    phases. Extractor mode (no reference patch): recover the fix from the
    published image history, then probe published vs clean — Docker-only
    (remote archaeology reports explicit unavailable, never a finding).
    Returns the clean-chain ``collect_result`` row, ``probe-blind``, or
    ``{"census_locations": None}`` when no fix is recoverable.
    """
    import hashlib

    from evallab.fix_content_census import (
        collect_result,
        copy_git_from_image,
        distinctive_added_lines,
        leak_oracle_extract,
        recover_fix_lite,
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
    patch_text = ""
    patch_sha = ""
    if reference_fix is not None:
        try:
            patch_text = reference_fix.read_text(encoding="utf-8", errors="replace")
        except OSError:
            patch_text = ""
        if patch_text.strip():
            patch_sha = hashlib.sha256(patch_text.encode()).hexdigest()
    known_patterns = distinctive_added_lines(patch_text) if patch_sha else []
    meta_path = scratch / "census.meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        # Reuse completed probes verbatim (deterministic): same image, same
        # shipped setup, same fix identity (recovered SHA or patch SHA), and
        # completion evidence for the recorded outcome. Null/vacuous rows
        # never match; metadata without scan evidence is never reusable.
        identity = meta.get("patch_sha") or meta.get("fix_sha")
        want_identity = patch_sha or None
        if (
            meta.get("image") == image
            and meta.get("clean_setup_sha256") == clean_setup_sha
            and isinstance(identity, str)
            and identity
            and (identity == want_identity if want_identity else len(identity) == 40)
        ):
            if meta.get("probe_blind"):
                blind_done, _ = probe_completion(meta.get("published_complete", {}))
                clean_done, _ = probe_completion(meta.get("clean", {}))
                if blind_done and clean_done:
                    return {
                        "census_locations": "probe-blind",
                        "reason": "probe-blind",
                        "detail": meta["clean"],
                    }
            elif isinstance(meta.get("clean", {}).get("hits_total"), int):
                clean_done, _ = probe_completion(meta["clean"])
                if clean_done:
                    return {
                        "census_locations": meta["clean"]["hits_total"],
                        "reason": "",
                        "detail": meta["clean"],
                    }
    # Probe network policy comes from the shipped package contract: public
    # tasks probe unlocked, locked modes stay locked. Daytona never relaxes
    # its standing policy, so it always probes locked.
    probe_egress_lock = True if backend == "daytona" else fix_probe_egress_lock(clean_package)
    if known_patterns:
        return _known_patch_census(
            task_id=task_id,
            clean_package=clean_package,
            run_package=run_package,
            language=language,
            scratch=scratch,
            image=image,
            workdir=workdir,
            clean_setup_sha=clean_setup_sha,
            patch_text=patch_text,
            patch_sha=patch_sha,
            patterns=known_patterns,
            backend=backend,
            egress_lock=probe_egress_lock,
        )
    if backend != "docker":
        return {
            "census_locations": None,
            "reason": f"remote archaeology unavailable on {backend}; run fix census on docker",
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
    _invoke_probe(
        image,
        workdir,
        fix_sha,
        published_stage,
        published_out,
        backend=backend,
        egress_lock=probe_egress_lock,
    )
    clean_source = clean_package / "environment" / "setup"
    if not clean_setup_sha:
        return {"census_locations": None, "reason": "clean package has no setup.sh"}
    clean_stage = scratch / "stage-clean"
    clean_out = scratch / "out-clean"
    stage_probe(clean_stage, clean_source)
    _invoke_probe(
        image,
        workdir,
        fix_sha,
        clean_stage,
        clean_out,
        backend=backend,
        egress_lock=probe_egress_lock,
    )
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
                "backend": backend,
                "egress_lock": probe_egress_lock,
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
    """Append one sourced batch spend record to spend.jsonl."""
    path = receipt_dir / SPEND_FILENAME
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**dict(record), "recorded_at": utc_now_iso()}) + "\n")
    return path


def slice_spent_usd(receipt_dir: Path) -> float:
    """Sum recorded batch dollars; historical modeled totals are not invoices."""
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


#: Daytona pay-as-you-go rates (USD per hour), used for a rate-card
#: reconstruction rather than provider billing actuals. The single-allocation
#: wall-time model omits overlapping fresh-verifier allocations and can
#: undercount; callers must reserve that uncertainty separately.
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
    """Rate-card lower-bound reconstruction for Daytona cells.

    Sums trial wall-hours x one task-declared allocation over every
    ``*-daytona-*`` job dir. Overlapping agent/fresh-verifier allocations are
    not counted separately, so this is not an invoice or complete usage bill.
    Trials without parseable timestamps are excluded, never zero-filled.
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
    cost = sub.add_parser("cost", help="Record Daytona rate-card estimates, not provider invoices")
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
        "--workers",
        type=int,
        default=None,
        help=(
            "process workers for the batch (default 1, serial; "
            f"max {MAX_DOCKER_WORKERS} on docker, max {MAX_REMOTE_WORKERS} remote)"
        ),
    )
    parser.add_argument(
        "--checks",
        default=None,
        help=(
            "comma subset of nop,oracle,ladder,fix (default all; "
            "unselected checks launch nothing and report blank)"
        ),
    )
    parser.add_argument(
        "--skip-fix-census",
        action="store_true",
        help="skip check (d), grading cells only (alias for --checks without fix)",
    )
    parser.add_argument(
        "--modal-resource-policy",
        choices=list(MODAL_RESOURCE_POLICIES),
        default=MODAL_POLICY_AUTO,
        help=(
            "Modal resource policy for grading cells (default auto; "
            "limit opts into cpu/memory enforcement on the census app)"
        ),
    )


def run_task_record(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Run one task's selected checks; return exactly one results record.

    Worker-side entry point for the process pool (and the serial path):
    runs grading cells plus the fix probe per ``spec["checks"]`` and returns
    the record dict. Never appends JSONL — only the parent writes, so
    concurrent workers share no writer. Unselected checks report blank
    grades (never ``missing``), which report-side merging treats as no
    evidence rather than an outcome.
    """
    from evallab.mimo_clean import run_package_rel

    task_id = str(spec["task_id"])
    manifest_row = {str(key): str(value) for key, value in dict(spec["manifest_row"]).items()}
    manifest_version = str(spec["manifest_version"])
    primary = Path(str(spec["primary"]))
    jobs_root = Path(str(spec["jobs_root"]))
    backend = str(spec["backend"])
    root = Path(str(spec["root"]))
    selected_checks = tuple(str(name) for name in spec["checks"])
    grading = [name for name in selected_checks if name in GRADING_CHECKS]
    record: dict[str, Any] = blank_row(task_id, manifest_version=manifest_version, backend=backend)
    record["final_digest"] = manifest_row.get("final_digest", "")
    modal_resource_policy = normalize_modal_resource_policy(spec.get("modal_resource_policy"))
    record["modal_resource_policy"] = policy_row_value(modal_resource_policy)
    if grading:
        partial = census_task(
            task_id=task_id,
            manifest_row=manifest_row,
            primary=primary,
            jobs_root=jobs_root,
            backend=backend,
            timeout_seconds=int(spec["timeout_seconds"]),
            root=root,
            checks=grading,
            modal_resource_policy=modal_resource_policy,
        )
        record["nop"] = partial["nop"] if "nop" in grading else ""
        record["oracle"] = partial["oracle"] if "oracle" in grading else ""
        if "ladder" in grading:
            record["ladder_verdict"] = partial["ladder_verdict"]
            record["ladder_cracking_attacks"] = partial["ladder_cracking_attacks"]
            record["ladder_version"] = partial["ladder_version"]
        else:
            record["ladder_verdict"] = ""
            record["ladder_cracking_attacks"] = ""
            record["ladder_version"] = ""
        record["run_ids"] = partial["run_ids"]
    else:
        record["nop"] = ""
        record["oracle"] = ""
        record["ladder_verdict"] = ""
        record["ladder_cracking_attacks"] = ""
        record["ladder_version"] = ""
    if "fix" in selected_checks:
        fix_ref = manifest_row.get("reference_fix", "")
        if fix_ref in ("", "none"):
            # No reference fix exists: check (d) is explicitly not
            # applicable, so controls+ladder alone can pass.
            record["census_locations"] = "n/a"
        else:
            ledger_row = spec.get("ledger_row")
            if ledger_row is not None:
                reference_fix: Path | None = None
                if fix_ref not in ("", "none"):
                    candidate = Path(fix_ref)
                    # Absolute sweep paths stay absolute; anything else resolves
                    # against the primary checkout (index.csv patch_path later).
                    reference_fix = candidate if candidate.is_absolute() else primary / candidate
                try:
                    fix_result = census_fix_content(
                        task_id=task_id,
                        clean_package=primary / Path(str(manifest_row["package_path"])),
                        run_package=primary / run_package_rel(dict(ledger_row)),
                        language=manifest_row.get("language", "python"),
                        scratch_root=jobs_root / "fix-census-scratch",
                        reference_fix=reference_fix,
                        backend=backend,
                    )
                except Exception as exc:  # noqa: BLE001 - probe failure is a finding
                    print(f"fix-census {task_id}: {type(exc).__name__}: {exc}")
                else:
                    if fix_result.get("census_locations") is not None:
                        record["census_locations"] = str(fix_result["census_locations"])
    return record


def _run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
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
    try:
        selected_checks = parse_check_list(args.checks)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if args.skip_fix_census:
        if args.checks is not None and "fix" in selected_checks:
            print("error: --skip-fix-census conflicts with --checks including fix")
            return 2
        selected_checks = tuple(name for name in selected_checks if name != "fix")
    try:
        workers = resolve_worker_count(backend=args.backend, requested=args.workers)
        modal_resource_policy = normalize_modal_resource_policy(args.modal_resource_policy)
        if modal_resource_policy is not None and args.backend != "modal":
            raise ValueError("--modal-resource-policy limit requires --backend modal")
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    ledger: dict[str, dict[str, str]] = {}
    if "fix" in selected_checks:
        ledger_path = _resolve(root, args.ledger)
        if ledger_path.is_file():
            ledger = {row["task_id"]: row for row in load_manifest(ledger_path)}
        else:
            print(f"note: ledger {ledger_path} absent; fix-content census skipped")
    # The whole batch is fenced before any worker starts; workers never
    # re-check the cap (actuals land via record-spend/cost after the run).
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
    print(
        f"census plan: {len(selected)} tasks x checks={','.join(selected_checks)} "
        f"backend={args.backend} workers={workers} "
        f"modal_resource_policy={policy_row_value(modal_resource_policy)}"
    )
    specs: list[dict[str, Any]] = [
        {
            "task_id": task_id,
            "manifest_row": dict(row),
            "manifest_version": ctx.manifest_version,
            "primary": str(ctx.primary),
            "jobs_root": str(ctx.jobs_dir),
            "backend": args.backend,
            "timeout_seconds": args.timeout_seconds,
            "root": str(root),
            "checks": list(selected_checks),
            "ledger_row": dict(ledger[task_id]) if task_id in ledger else None,
            "modal_resource_policy": modal_resource_policy,
        }
        for task_id, row in selected
    ]
    rows_path = ctx.jobs_dir / ROWS_FILENAME
    ctx.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _emit(record: Mapping[str, Any]) -> None:
        # Only the parent appends: one record per selected task, serially,
        # so concurrent workers never share a writer.
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record)) + "\n")
        print(
            f"| {record['task_id']} | oracle={record['oracle']} nop={record['nop']} "
            f"ladder={record['ladder_verdict']}:{record['ladder_cracking_attacks'] or '-'} "
            f"census={record['census_locations'] or 'n/a'} |"
        )

    if workers == 1:
        # Native serial path: in-process, one task at a time.
        for spec in specs:
            try:
                _emit(run_task_record(spec))
            except Exception as exc:  # noqa: BLE001 - one task never kills the batch
                print(f"error {spec['task_id']}: {type(exc).__name__}: {exc}")
    else:
        # Remote fan-out: process workers (never threads — the cheat ladder
        # hands its attack subset through process os.environ under a lock).
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_task_record, spec) for spec in specs]
            for spec, future in zip(specs, futures, strict=True):
                try:
                    _emit(future.result())
                except Exception as exc:  # noqa: BLE001 - one task never kills the batch
                    print(f"error {spec['task_id']}: {type(exc).__name__}: {exc}")
    print(f"census rows appended -> {rows_path}")
    return 0


def report_row_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """Generation-bound report key: never merge across generations.

    ``(task_id, backend, manifest_version, final_digest,
    modal_resource_policy)`` — a v3 ladder-only row never merges into a v2
    controls row, and AUTO/LIMIT rows never merge. Missing policies predate
    the flag and ran as auto; unrecognized values stay distinct (truthful,
    never merged).
    """
    raw_policy = str(record.get("modal_resource_policy", "") or "")
    policy = raw_policy.strip().lower()
    if policy in ("", MODAL_POLICY_AUTO):
        policy = MODAL_POLICY_AUTO
    elif policy != MODAL_POLICY_LIMIT:
        policy = raw_policy
    return (
        str(record.get("task_id", "")),
        str(record.get("backend", "")),
        str(record.get("manifest_version", "")),
        str(record.get("final_digest", "")),
        policy,
    )


def _report_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.mimo_clean import write_manifest

    ctx = resolve_context(args, root)
    rows_path = ctx.jobs_dir / ROWS_FILENAME
    if not rows_path.is_file():
        print(f"error: no census rows at {rows_path}; run `mimo-census run` first")
        return 2
    # Rows merge only within one generation key: one task may carry a daytona
    # controls row plus a docker ladder row, and v2 rows never merge into v3.
    # A later ladder-only row carries blanks for unselected checks, which
    # never overwrite prior nop/oracle evidence within its generation.
    latest: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    with rows_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = {str(key): value for key, value in json.loads(line).items()}
            key = report_row_key(record)
            record["modal_resource_policy"] = key[4]
            latest[key] = merge_census_rows(latest[key], record) if key in latest else record
    # Recorded amounts accumulate per (task, backend) across batches. Costs
    # are amortizations, not per-task invoices; legacy Daytona is modeled.
    batch_cost: dict[tuple[str, str], float] = {}
    spend_path = ctx.receipt_dir / SPEND_FILENAME
    if spend_path.is_file():
        for line in spend_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            tasks = parse_task_list(str(entry.get("tasks", "")))
            if tasks:
                share = amortized_cost(float(entry.get("actual_usd", 0.0)), len(tasks))
                entry_backend = str(entry.get("backend", ""))
                for task_id in tasks:
                    key = (task_id, entry_backend)
                    batch_cost[key] = batch_cost.get(key, 0.0) + share
    # Whether each task has a reference fix, and its current digest, comes
    # from the current manifest, never from old rows: a known-patch task
    # with a blank fix cell stays unverified until the fix phase completes,
    # while a task with no reference reports explicit ``n/a``.
    manifest_rows: list[dict[str, str]] = []
    if ctx.manifest_path.is_file():
        manifest_rows = load_manifest(ctx.manifest_path)
    has_reference_fix: dict[str, bool] | None = (
        {
            str(row["task_id"]): str(row.get("reference_fix", "")) not in ("", "none")
            for row in manifest_rows
        }
        if manifest_rows
        else None
    )
    current_digest = {
        str(row["task_id"]): str(row.get("final_digest", "")) for row in manifest_rows
    }
    unpriced_count: dict[tuple[str, str], int] = {}
    for key, record in latest.items():
        if record.get("cost_usd", "") in ("", None):
            cost_key = (key[0], key[1])
            unpriced_count[cost_key] = unpriced_count.get(cost_key, 0) + 1
    rows: list[dict[str, Any]] = []
    for key, record in sorted(latest.items()):
        task_id = key[0]
        backend = key[1]
        # Original row versions stay truthful: never relabel manifest_version.
        if record.get("cost_usd", "") in ("", None):
            share = batch_cost.get((task_id, backend))
            if share is not None:
                share /= unpriced_count[(task_id, backend)]
            record["cost_usd"] = (
                f"{share:.4f}" if share is not None else f"{args.cost_default_usd:.2f}"
            )
        if (
            has_reference_fix is not None
            and not has_reference_fix.get(task_id, True)
            and record.get("census_locations", "") in ("", None)
        ):
            record["census_locations"] = "n/a"
        rows.append(record)
    out = write_results(rows, ctx.receipt_dir / RESULTS_FILENAME)
    current_rows = (
        [
            record
            for record in rows
            if record.get("final_digest") == current_digest.get(str(record["task_id"]))
        ]
        if manifest_rows
        else rows
    )
    summary = summarize_results(current_rows, has_reference_fix=has_reference_fix)
    print(
        f"results.csv: {summary['passed']}/{summary['total']} current-digest rows pass "
        f"({len(rows)} historical/current rows retained) -> {out}"
    )
    for failure in summary["failures"]:
        print(f"FAIL {failure['task_id']}: {failure['reasons']}")
    # Task-level aggregate: combine each task's per-backend rows on the exact
    # current manifest digest (controls Daytona + ladder/fix Modal) into the
    # verify grade that lands in manifest.verify. Per-check columns above
    # carry the explicit classification behind every unverified grade.
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in rows:
        by_task.setdefault(str(record["task_id"]), []).append(record)
    aggregates: dict[str, dict[str, Any]] = {}
    for task_id in sorted(by_task):
        fix_known = has_reference_fix.get(task_id, True) if has_reference_fix else True
        exact = current_digest.get(task_id) or None
        aggregate = combine_task_rows(
            by_task[task_id], has_reference_fix=fix_known, final_digest=exact
        )
        if aggregate is None:
            print(f"AGG {task_id}: verify={VERIFY_UNVERIFIED} (no exact-digest rows)")
            continue
        aggregates[task_id] = aggregate
        print(
            f"AGG {task_id}: verify={aggregate['verify']} "
            f"nop={aggregate['nop'] or '-'} oracle={aggregate['oracle'] or '-'} "
            f"ladder={aggregate['ladder_verdict'] or '-'}:"
            f"{aggregate['ladder_cracking_attacks'] or '-'} "
            f"census={aggregate['census_locations'] or 'n/a'} "
            f"backends={aggregate['backends']}"
        )
    # Manifest verify writes ONLY aggregates on the exact current digest for
    # that task; every other manifest row (and column) is left untouched.
    if manifest_rows:
        updated = 0
        for row in manifest_rows:
            task_id = str(row["task_id"])
            aggregate = aggregates.get(task_id)
            if aggregate is not None and aggregate["final_digest"] == str(
                row.get("final_digest", "")
            ):
                row["verify"] = str(aggregate["verify"])
                updated += 1
        write_manifest(manifest_rows, ctx.manifest_path)
        print(f"manifest verify updated for {updated}/{len(manifest_rows)} tasks")
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
            "measurement_kind": "rate-card-lower-bound",
            "provider_actual_usd": None,
            "evidence": result["basis"],
        },
    )
    for task_id in sorted(result["per_task_usd"]):
        print(f"daytona estimate {task_id}: ${result['per_task_usd'][task_id]:.4f}")
    for unscored in result["unscored"]:
        print(f"unscored (excluded): {unscored}")
    total = slice_spent_usd(ctx.receipt_dir)
    print(f"recorded lower-bound estimate {args.batch_id}: ${result['batch_usd']:.4f} -> {path}")
    print(f"slice total so far: ${total:.4f} / ${SLICE_CAP_USD:.2f}")
    return 0


__all__ = [
    "ATTACKS",
    "BACKENDS",
    "CENSUS_VERSION",
    "CHECK_COLUMNS",
    "CHECKS",
    "DEFAULT_WORKERS",
    "GRADING_CHECKS",
    "MAX_REMOTE_WORKERS",
    "MODAL_CENSUS_APP_NAME",
    "MODAL_POLICY_AUTO",
    "MODAL_POLICY_LIMIT",
    "MODAL_RESOURCE_POLICIES",
    "NO_EVIDENCE",
    "RESOURCE_ENFORCEMENT_KEYS",
    "RESULTS_FILENAME",
    "ROWS_FILENAME",
    "SLICE_CAP_USD",
    "SPEND_FILENAME",
    "VERIFY_ENV_BROKEN",
    "VERIFY_FAIL_CLASSES",
    "VERIFY_FAIL_GRADER_HOLE",
    "VERIFY_FAIL_OPEN_LEAK",
    "VERIFY_FAIL_ORACLE_WRONG",
    "VERIFY_PASS",
    "VERIFY_UNVERIFIED",
    "acceptance_matches",
    "amortized_cost",
    "append_spend_record",
    "blank_row",
    "build_mimo_census_parser",
    "census_fix_content",
    "census_row_pass",
    "census_task",
    "cell_reusable",
    "cheat_cell_ladder_version",
    "combine_task_rows",
    "current_ladder_version",
    "daytona_batch_cost_usd",
    "daytona_hourly_usd",
    "cell_job_name",
    "expected_resource_kwargs",
    "fence_allows",
    "fix_probe_egress_lock",
    "job_has_trial_errors",
    "ladder_summary",
    "load_results",
    "merge_census_rows",
    "next_free_name",
    "nop_cell_grade",
    "oracle_cell_grade",
    "parse_check_list",
    "parse_junit_grade",
    "parse_task_list",
    "policy_row_value",
    "probe_completion",
    "probe_evidence",
    "resolve_worker_count",
    "report_row_key",
    "run_cell",
    "run_task_record",
    "scannable_cell_dirs",
    "slice_spent_usd",
    "summarize_results",
    "summarize_trials",
    "task_sandbox_allocation",
    "trial_grade_logs",
    "trial_disk_exhausted",
    "trial_output_logs",
    "trial_runner",
    "trial_tests_executed",
    "trial_wall_hours",
    "utc_now_iso",
    "verify_grade_for",
    "write_results",
]
