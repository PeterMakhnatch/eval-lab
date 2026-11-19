"""Deterministic reward-integrity verification for finished Harbor job dirs.

Harbor writes the same reward in up to three places (trial ``result.json``
``verifier_result.rewards``, ``verifier/reward.json``, and the job-level
``stats.evals`` aggregate) with no checksum binding them, and Eval Lab readers
have historically disagreed on edge values: a boolean or ``-1`` reward is a
scored pass on one path and unscored on another (HAR-446/HAR-455). A reward
quoted from such a job is only trustworthy if every copy agrees and every
value is a finite, scored number.

This module is a pure, stdlib-only file reader in the mold of
:mod:`evallab.results`. It runs no task, calls no model, and writes nothing.
Findings carry a severity:

- ``block``: a reward from this job must not be quoted until fixed.
- ``warn``: quotable with the caveat attached.
- ``info``: an explanation, e.g. why Harbor left ``pass_at_k`` empty.

A job verdict is ``error`` when the directory is not a readable Harbor job,
``block`` when any block finding fires, else ``pass``. The CLI maps these to
exit codes 2/1/0 so the check works as a CI gate.

Task identity is reported, never joined: a trial ``task_checksum`` and a job
lock ``task.digest`` are different digest namespaces (they differ on the same
trial), so both are listed as opaque strings.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JOBS_VERIFY_SCHEMA = "evallab.jobs.verify/v1"

BLOCK = "block"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    detail: str
    trial: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "trial": self.trial,
            "detail": self.detail,
        }


@dataclass
class JobReport:
    path: Path
    verdict: str = "pass"
    trials_indexed: int = 0
    trials_expected: int | None = None
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, detail: str, trial: str | None = None) -> None:
        self.findings.append(Finding(code=code, severity=severity, detail=detail, trial=trial))

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "verdict": self.verdict,
            "trials_indexed": self.trials_indexed,
            "trials_expected": self.trials_expected,
            "findings": [finding.to_json() for finding in self.findings],
        }


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    """Return (object, error). Error is a short stable code suffix."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, UnicodeError):
        return {}, "unreadable"
    except json.JSONDecodeError:
        return {}, "invalid_json"
    if not isinstance(value, dict):
        return {}, "not_an_object"
    return value, None


def _reward_value_problem(value: Any) -> str | None:
    """Classify one raw reward value; None means a scored finite number."""
    if isinstance(value, bool):
        return "reward_bool"
    if not isinstance(value, (int, float)):
        return "reward_non_numeric"
    if not math.isfinite(value):
        return "reward_non_finite"
    if float(value) == -1:
        return "reward_unscored_sentinel"
    return None


def _is_trial_result(result: dict[str, Any]) -> bool:
    return "task_name" in result and "trial_name" in result


def _trial_shaped(directory: Path) -> bool:
    return any(
        (directory / name).exists() for name in ("lock.json", "trial.log", "verifier", "agent")
    )


def _verifier_file_rewards(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Rewards from verifier/reward.json, else verifier/reward.txt."""
    reward_json = trial_dir / "verifier" / "reward.json"
    if reward_json.is_file():
        value, error = _read_json_object(reward_json)
        if error is not None:
            return None, f"verifier/reward.json: {error}"
        return value, None
    reward_txt = trial_dir / "verifier" / "reward.txt"
    if reward_txt.is_file():
        try:
            return {"reward": float(reward_txt.read_text(encoding="utf-8").strip())}, None
        except (OSError, UnicodeError, ValueError):
            return None, "verifier/reward.txt: unparsable"
    return None, None


def _check_trial(report: JobReport, trial_dir: Path, result: dict[str, Any]) -> dict[str, float]:
    """Check one indexed trial; return its scored rewards for the job roll-up."""
    trial_name = str(result.get("trial_name") or trial_dir.name)
    verifier_result = result.get("verifier_result")
    raw_rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    if not isinstance(raw_rewards, dict):
        raw_rewards = {}

    scored: dict[str, float] = {}
    for name, value in sorted(raw_rewards.items()):
        problem = _reward_value_problem(value)
        if problem is None:
            scored[str(name)] = float(value)
        else:
            report.add(
                problem,
                BLOCK,
                f"reward {name!r}={value!r} is unscored and must not count as pass or fail",
                trial=trial_name,
            )

    file_rewards, file_error = _verifier_file_rewards(trial_dir)
    if file_error is not None:
        report.add("reward_file_unreadable", BLOCK, file_error, trial=trial_name)
    elif file_rewards is not None and raw_rewards:
        mismatched = sorted(
            name
            for name in set(file_rewards) | set(raw_rewards)
            if file_rewards.get(name) != raw_rewards.get(name)
        )
        if mismatched:
            report.add(
                "reward_mismatch_result_vs_file",
                BLOCK,
                "verifier reward file disagrees with result.json on: " + ", ".join(mismatched),
                trial=trial_name,
            )
    elif file_rewards is not None and not raw_rewards:
        report.add(
            "reward_file_orphan",
            WARN,
            "verifier reward file exists but result.json captured no rewards; "
            "the file is the only reward authority for this trial",
            trial=trial_name,
        )

    _check_seats(report, trial_name, result, scored)

    if not raw_rewards and file_rewards is None:
        exception = result.get("exception_info")
        if isinstance(exception, dict) and exception.get("exception_type"):
            report.add(
                "reward_missing_exceptioned",
                WARN,
                f"no reward; trial exceptioned ({exception.get('exception_type')})",
                trial=trial_name,
            )
        else:
            report.add(
                "reward_missing",
                BLOCK,
                "no reward in result.json or verifier reward file",
                trial=trial_name,
            )
    return scored


def _check_seats(
    report: JobReport,
    trial_name: str,
    result: dict[str, Any],
    scored: dict[str, float],
) -> None:
    """Seat-trial integrity: roster agreement and per-seat evidence state.

    A multi-agent (seat) trial records one ``seat_results`` entry per seat
    and its roster in ``config.participants``. The trial reward alone hides
    two quotability problems: a seat that errored beside a passing reward,
    and a seat whose logs never reached the trial dir. Both stay visible
    here without rewriting the reward.
    """
    config = result.get("config")
    participants = config.get("participants") if isinstance(config, dict) else None
    participant_seats = {
        str(entry["seat"])
        for entry in participants
        if isinstance(entry, dict) and entry.get("seat")
    } if isinstance(participants, list) else set()
    seat_results = result.get("seat_results")
    seats = seat_results if isinstance(seat_results, list) else []
    if not participant_seats and not seats:
        return
    if participant_seats and not seats:
        report.add(
            "seat_results_missing",
            BLOCK,
            "config lists participants but result.json has no seat_results; "
            "the seat runs are not recorded",
            trial=trial_name,
        )
        return
    seat_names = [str(seat.get("seat")) for seat in seats if isinstance(seat, dict)]
    if len(seat_names) != len(set(seat_names)):
        report.add(
            "seat_duplicate",
            BLOCK,
            f"duplicate seat names in seat_results: {sorted(seat_names)}",
            trial=trial_name,
        )
    if participant_seats and set(seat_names) != participant_seats:
        report.add(
            "seat_participant_mismatch",
            BLOCK,
            f"seat_results seats {sorted(set(seat_names))} do not match "
            f"config participants {sorted(participant_seats)}",
            trial=trial_name,
        )
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        name = str(seat.get("seat"))
        exception = seat.get("exception_info")
        if scored and isinstance(exception, dict) and exception.get("exception_type"):
            report.add(
                "seat_exception_beside_reward",
                WARN,
                f"seat {name!r} errored ({exception.get('exception_type')}) "
                "beside a scored trial reward; quote the reward with this seat error",
                trial=trial_name,
            )
        if seat.get("logs_downloaded") is False:
            report.add(
                "seat_logs_missing",
                WARN,
                f"seat {name!r} logs never reached the trial dir "
                "(logs_downloaded is false); its trajectory is unavailable",
                trial=trial_name,
            )


def _check_job_stats(
    report: JobReport,
    stats: dict[str, Any],
    trial_rewards: dict[str, dict[str, float]],
) -> None:
    evals = stats.get("evals")
    if not isinstance(evals, dict) or not evals:
        report.add("job_stats_missing_evals", BLOCK, "job result.json stats has no evals")
        return

    stored_n_trials = 0
    stored_reward_stats: dict[str, dict[str, list[str]]] = {}
    pass_at_k_empty = True
    for _eval_key, eval_stats in sorted(evals.items()):
        if not isinstance(eval_stats, dict):
            continue
        n_trials = eval_stats.get("n_trials")
        if isinstance(n_trials, int) and not isinstance(n_trials, bool):
            stored_n_trials += n_trials
        if eval_stats.get("pass_at_k"):
            pass_at_k_empty = False
        reward_stats = eval_stats.get("reward_stats")
        if not isinstance(reward_stats, dict):
            continue
        for dimension, by_value in reward_stats.items():
            if not isinstance(by_value, dict):
                continue
            merged = stored_reward_stats.setdefault(str(dimension), {})
            for value, names in by_value.items():
                if isinstance(names, list):
                    merged.setdefault(str(value), []).extend(str(name) for name in names)

    # Harbor counts only scored trials in stats.evals: an exceptioned trial
    # with no reward appears in n_total_trials/n_errored_trials but not in any
    # eval bucket (observed: canary-transaction-reconciliation-codex-20260816,
    # 3 trials on disk, eval n_trials=2, 1 NonZeroAgentExitCodeError).
    scored_trials = sum(1 for rewards in trial_rewards.values() if rewards)
    if stored_n_trials and stored_n_trials != scored_trials:
        report.add(
            "job_stats_trial_count_desync",
            BLOCK,
            f"stats.evals n_trials sums to {stored_n_trials}, "
            f"recomputed {scored_trials} scored trials",
        )

    recomputed: dict[str, dict[str, list[str]]] = {}
    for trial_name, rewards in trial_rewards.items():
        for dimension, value in rewards.items():
            recomputed.setdefault(dimension, {}).setdefault(str(value), []).append(trial_name)
    for by_value in list(stored_reward_stats.values()) + list(recomputed.values()):
        for names in by_value.values():
            names.sort()
    if stored_reward_stats != recomputed:
        report.add(
            "job_stats_reward_desync",
            BLOCK,
            "stats.evals reward_stats does not match rewards recomputed from trial result.json files",
        )

    if pass_at_k_empty and trial_rewards:
        multi_dim = any(len(rewards) != 1 for rewards in trial_rewards.values())
        non_binary = any(
            value not in (0.0, 1.0)
            for rewards in trial_rewards.values()
            for value in rewards.values()
        )
        if multi_dim or non_binary:
            reason = "multi-dimension rewards" if multi_dim else "non-{0,1} reward values"
            report.add(
                "pass_at_k_empty",
                INFO,
                f"Harbor pass_at_k is empty because of {reason}; "
                "Harbor only fills it for single-dimension binary rewards",
            )


def verify_job(job_dir: Path) -> JobReport:
    report = JobReport(path=job_dir)
    result_path = job_dir / "result.json"
    result, error = _read_json_object(result_path)
    if error is not None:
        report.verdict = "error"
        report.add("job_result_unreadable", BLOCK, f"result.json: {error}")
        return report
    if "n_total_trials" not in result or "stats" not in result:
        report.verdict = "error"
        report.add(
            "job_result_not_a_job",
            BLOCK,
            "result.json lacks n_total_trials/stats; not a Harbor job directory",
        )
        return report
    if not result.get("finished_at"):
        report.add("job_unfinished", BLOCK, "job result.json has no finished_at")

    expected = result.get("n_total_trials")
    report.trials_expected = (
        expected if isinstance(expected, int) and not isinstance(expected, bool) else None
    )
    if report.trials_expected is None:
        report.add(
            "job_trial_count_invalid", BLOCK, f"n_total_trials is not an integer: {expected!r}"
        )

    trial_rewards: dict[str, dict[str, float]] = {}
    seen_ids: dict[str, str] = {}
    for candidate in sorted(job_dir.iterdir()):
        if not candidate.is_dir():
            continue
        trial_result, trial_error = _read_json_object(candidate / "result.json")
        if trial_error is not None or not _is_trial_result(trial_result):
            if _trial_shaped(candidate):
                report.add(
                    "trial_result_missing",
                    BLOCK,
                    "trial-shaped directory has no readable trial result.json; "
                    "its reward, if any, is excluded from every count",
                    trial=candidate.name,
                )
            continue
        trial_name = str(trial_result["trial_name"])
        trial_id = trial_result.get("id")
        if isinstance(trial_id, str):
            if trial_id in seen_ids:
                report.add(
                    "trial_duplicate_id",
                    BLOCK,
                    f"trial id also used by {seen_ids[trial_id]}",
                    trial=trial_name,
                )
            seen_ids[trial_id] = trial_name
        if trial_name in trial_rewards:
            report.add(
                "trial_duplicate_name", BLOCK, "duplicate trial_name in job", trial=trial_name
            )
        trial_rewards[trial_name] = _check_trial(report, candidate, trial_result)

    report.trials_indexed = len(trial_rewards)
    if report.trials_expected is not None and report.trials_expected != report.trials_indexed:
        report.add(
            "trial_count_mismatch",
            BLOCK,
            f"job declares {report.trials_expected} trials, indexed {report.trials_indexed}",
        )

    lock, lock_error = _read_json_object(job_dir / "lock.json")
    if lock_error is None:
        _report_digest_namespaces(report, lock)

    stats = result.get("stats")
    _check_job_stats(report, stats if isinstance(stats, dict) else {}, trial_rewards)

    if report.verdict != "error" and any(f.severity == BLOCK for f in report.findings):
        report.verdict = "block"
    return report


def _report_digest_namespaces(report: JobReport, lock: dict[str, Any]) -> None:
    trials = lock.get("trials")
    if not isinstance(trials, list):
        return
    digests = []
    for entry in trials:
        if not isinstance(entry, dict):
            continue
        task = entry.get("task")
        if isinstance(task, dict) and task.get("digest"):
            digests.append(f"{task.get('name')}={task['digest']}")
    if digests:
        report.add(
            "task_digest_namespace",
            INFO,
            "lock task digests (distinct namespace from trial task_checksum, never join them): "
            + "; ".join(digests[:8]),
        )


def verify_jobs(job_dirs: list[Path]) -> dict[str, Any]:
    reports = [verify_job(path) for path in job_dirs]
    verdicts = [report.verdict for report in reports]
    return {
        "schema": JOBS_VERIFY_SCHEMA,
        "jobs": [report.to_json() for report in reports],
        "summary": {
            "jobs": len(reports),
            "pass": verdicts.count("pass"),
            "block": verdicts.count("block"),
            "error": verdicts.count("error"),
            "block_findings": sum(
                1 for report in reports for f in report.findings if f.severity == BLOCK
            ),
            "warn_findings": sum(
                1 for report in reports for f in report.findings if f.severity == WARN
            ),
        },
    }


def exit_code(report: dict[str, Any]) -> int:
    summary = report["summary"]
    if summary["jobs"] == 0 or summary["error"]:
        return 2
    if summary["block"]:
        return 1
    return 0


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for job in report["jobs"]:
        lines.append(
            f"{job['verdict'].upper():5} {job['path']} "
            f"(trials {job['trials_indexed']}/{job['trials_expected']})"
        )
        for finding in job["findings"]:
            where = f" [{finding['trial']}]" if finding["trial"] else ""
            lines.append(f"  {finding['severity']:5} {finding['code']}{where}: {finding['detail']}")
    summary = report["summary"]
    lines.append(
        f"jobs: {summary['jobs']} (pass {summary['pass']}, block {summary['block']}, "
        f"error {summary['error']}); block findings {summary['block_findings']}, "
        f"warn findings {summary['warn_findings']}"
    )
    return "\n".join(lines)
