from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from pydantic import ValidationError

from harbor_lab import database
from harbor_lab.queue import DirectoryQueue, load_events
from harbor_lab.runner import database_url_from_environment
from harbor_lab.schemas import (
    HeadlessDoctorReport,
    QueueEvent,
    QueueReason,
    StandingApprovalsPolicy,
)


@dataclass(frozen=True)
class DigestTrial:
    job_name: str
    task_name: str
    agent_name: str
    model_name: str | None
    reward: float | None
    exception_type: str | None
    cost_usd: float
    finished_at: str


TrialLoader = Callable[[date], list[DigestTrial]]


@dataclass(frozen=True)
class CanaryDriftObservation:
    task_name: str
    agent_name: str
    reward: float | None
    baseline_n: int
    baseline_mean: float | None
    baseline_stddev: float | None
    task_version_changed: bool
    is_harness_drift_suspect: bool
    drift_reason: str | None


DriftLoader = Callable[[date], list[CanaryDriftObservation]]


class DigestRenderer:
    def __init__(
        self,
        *,
        repo_root: Path,
        queue: DirectoryQueue,
        policy: StandingApprovalsPolicy,
        trial_loader: TrialLoader | None = None,
        drift_loader: DriftLoader | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.queue = queue
        self.policy = policy
        self._trial_loader = trial_loader or self._load_catalog_trials
        self._drift_loader = drift_loader or self._load_canary_drift

    def write(
        self,
        *,
        report_date: date,
        health_report: HeadlessDoctorReport | None = None,
        dispatched: int | None = None,
    ) -> Path:
        period_date = report_date - timedelta(days=1)
        catalog_error = False
        try:
            trials = self._trial_loader(period_date)
            early_trials = self._trial_loader(report_date)
        except Exception:
            trials = []
            early_trials = []
            catalog_error = True
        try:
            drift = self._drift_loader(period_date) + self._drift_loader(report_date)
        except Exception:
            drift = []

        events = load_events(self.queue.events_path)
        period_events = self._events_on(events, period_date)
        report_events = self._events_on(events, report_date)
        policy_by_job = self._policy_by_job(events)
        quarantine_events = [
            event
            for event in report_events
            if event.event in {"nightly_quarantined", "tick_quarantined"}
        ]
        is_quarantined = bool(quarantine_events) or (
            health_report is not None and not health_report.healthy
        )
        dispatch_count = (
            dispatched
            if dispatched is not None
            else sum(event.event == "dispatch_started" for event in report_events)
        )

        lines = [
            f"# Harbor lab digest — {report_date.isoformat()}",
            "",
            f"Reporting period: {period_date.isoformat()} (local catalog day).",
            "",
            "## Automation status",
            "",
            f"- Quarantined: {'yes' if is_quarantined else 'no'}",
            f"- Dispatches in this nightly cycle: {dispatch_count}",
        ]
        if is_quarantined:
            failed = self._failed_checks(health_report, quarantine_events)
            lines.extend(
                [
                    f"- Failed readiness checks: {', '.join(failed) if failed else 'unknown'}",
                    f"- Zero dispatch enforced: {'yes' if dispatch_count == 0 else 'NO'}",
                ]
            )
        if catalog_error:
            lines.append("- Catalog readable: no")
        else:
            lines.append("- Catalog readable: yes")

        lines.extend(
            [
                "",
                "## Completed trials",
                "",
            ]
        )
        self._append_trials(lines, trials, policy_by_job, empty="No completed trials.")

        if early_trials:
            lines.extend(
                [
                    "",
                    "## Early-morning automation",
                    "",
                    "Completed after the reporting-period cutoff:",
                    "",
                ]
            )
            self._append_trials(lines, early_trials, policy_by_job, empty="")

        lines.extend(["", "## Canary drift", ""])
        if drift:
            lines.extend(
                [
                    "| task | agent | reward | trailing 7-day mean ± σ | n | assessment |",
                    "|---|---|---:|---:|---:|---|",
                ]
            )
            for observation in drift:
                reward = "" if observation.reward is None else f"{observation.reward:g}"
                if observation.baseline_mean is None:
                    baseline = "insufficient history"
                else:
                    stddev = observation.baseline_stddev or 0.0
                    baseline = f"{observation.baseline_mean:.3f} ± {stddev:.3f}"
                assessment = (
                    f"harness-drift suspect ({observation.drift_reason}); not capability news"
                    if observation.is_harness_drift_suspect
                    else "within baseline"
                )
                lines.append(
                    f"| {_cell(observation.task_name)} | {_cell(observation.agent_name)} | "
                    f"{reward} | {baseline} | {observation.baseline_n} | "
                    f"{_cell(assessment)} |"
                )
        else:
            lines.append("No canary observations with a trailing baseline.")

        spend = sum(trial.cost_usd for trial in trials + early_trials)
        exceptions = Counter(
            "harness_failure" for trial in trials + early_trials if trial.exception_type
        )
        lines.extend(
            [
                "",
                "## Cost and failures",
                "",
                f"- Recorded spend: ${spend:.4f} / "
                f"${self.policy.daily_cost_ceiling_usd:.2f} daily ceiling",
                "- Exceptions by taxonomy: "
                + (
                    ", ".join(f"{name}={count}" for name, count in sorted(exceptions.items()))
                    if exceptions
                    else "none"
                ),
                "",
                "## Queue",
                "",
            ]
        )
        depths = {
            state: len(list(self.queue.state_dir(state).glob("*.json")))
            for state in (
                "proposed",
                "pending",
                "approved",
                "waiting",
                "running",
                "done",
                "failed",
            )
        }
        lines.append("- Depth: " + ", ".join(f"{key}={value}" for key, value in depths.items()))
        waiting = self.queue.list_specs("waiting")
        if waiting:
            lines.extend(["", "| proposal | experiment | reason |", "|---|---|---|"])
            for _, spec in waiting:
                lines.append(
                    f"| {_cell(str(spec.spec_id))} | {_cell(spec.name)} | "
                    f"{_cell(self._reason_for(str(spec.spec_id)))} |"
                )
        else:
            lines.append("- Waiting proposals: none")

        run_bytes = _directory_bytes(self.repo_root / "runs")
        prior_bytes = self._prior_run_bytes(report_date - timedelta(days=1))
        growth = (
            "baseline unavailable"
            if prior_bytes is None
            else _signed_bytes(run_bytes - prior_bytes)
        )
        lines.extend(
            [
                "",
                "## Evidence and calibration",
                "",
                f"- Run corpus: {run_bytes} bytes ({growth})",
                "- Judge calibration: not available until brief 09",
                f"- Canary observations in report: {len(drift)}",
                "",
                "## Queue events",
                "",
            ]
        )
        digest_events = period_events + report_events
        if digest_events:
            lines.extend(["| time | event | job | policy/reason |", "|---|---|---|---|"])
            for event in digest_events:
                policy_or_reason = event.policy_rule or event.reason_code or ""
                lines.append(
                    f"| {_cell(event.occurred_at.isoformat())} | {_cell(event.event)} | "
                    f"{_cell(event.job_name or '')} | {_cell(policy_or_reason)} |"
                )
        else:
            lines.append("No queue events.")

        lines.extend(["", f"<!-- run-bytes: {run_bytes} -->", ""])
        destination = self.repo_root / "digests" / f"{report_date.isoformat()}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines))
        return destination

    def _load_catalog_trials(self, day: date) -> list[DigestTrial]:
        rows = database.digest_trials(database_url_from_environment(), day)
        return [
            DigestTrial(
                job_name=str(row[0]),
                task_name=str(row[1] or ""),
                agent_name=str(row[2] or ""),
                model_name=str(row[3]) if row[3] is not None else None,
                reward=float(row[4]) if row[4] is not None else None,
                exception_type=str(row[5]) if row[5] is not None else None,
                cost_usd=float(row[6]),
                finished_at=str(row[7]),
            )
            for row in rows
        ]

    @staticmethod
    def _load_canary_drift(day: date) -> list[CanaryDriftObservation]:
        rows = database.canary_drift_observations(database_url_from_environment(), day)
        return [
            CanaryDriftObservation(
                task_name=str(row[0] or ""),
                agent_name=str(row[1] or ""),
                reward=float(row[2]) if row[2] is not None else None,
                baseline_n=int(row[3] or 0),
                baseline_mean=float(row[4]) if row[4] is not None else None,
                baseline_stddev=float(row[5]) if row[5] is not None else None,
                task_version_changed=bool(row[6]),
                is_harness_drift_suspect=bool(row[7]),
                drift_reason=str(row[8]) if row[8] is not None else None,
            )
            for row in rows
        ]

    @staticmethod
    def _events_on(events: list[QueueEvent], day: date) -> list[QueueEvent]:
        return [event for event in events if event.occurred_at.astimezone().date() == day]

    @staticmethod
    def _policy_by_job(events: list[QueueEvent]) -> dict[str, str]:
        policies: dict[str, str] = {}
        for event in events:
            if event.job_name and event.policy_rule:
                policies[event.job_name] = event.policy_rule
        return policies

    @staticmethod
    def _append_trials(
        lines: list[str],
        trials: list[DigestTrial],
        policy_by_job: dict[str, str],
        *,
        empty: str,
    ) -> None:
        if not trials:
            if empty:
                lines.append(empty)
            return
        lines.extend(
            [
                "| job | task | agent | reward | exception | policy |",
                "|---|---|---|---:|---|---|",
            ]
        )
        for trial in trials:
            reward = "" if trial.reward is None else f"{trial.reward:g}"
            lines.append(
                f"| {_cell(trial.job_name)} | {_cell(trial.task_name)} | "
                f"{_cell(trial.agent_name)} | {reward} | "
                f"{_cell(trial.exception_type or '')} | "
                f"{_cell(policy_by_job.get(trial.job_name, 'unattributed'))} |"
            )

    @staticmethod
    def _failed_checks(
        report: HeadlessDoctorReport | None,
        quarantine_events: list[QueueEvent],
    ) -> list[str]:
        if report is not None:
            return [
                name for name, succeeded in report.checks.model_dump().items() if not succeeded
            ]
        for event in reversed(quarantine_events):
            prefix = "headless_doctor_failed:"
            if event.reason_code and event.reason_code.startswith(prefix):
                return [name for name in event.reason_code.removeprefix(prefix).split(",") if name]
            if event.reason_code:
                return [event.reason_code]
        return []

    def _reason_for(self, spec_id: str) -> str:
        paths = sorted(self.queue.reasons_dir.glob(f"{spec_id}-*.json"), reverse=True)
        for path in paths:
            try:
                return QueueReason.model_validate_json(path.read_text()).code
            except (OSError, ValidationError):
                continue
        return "awaiting human review"

    def _prior_run_bytes(self, report_date: date) -> int | None:
        path = self.repo_root / "digests" / f"{report_date.isoformat()}.md"
        if not path.is_file():
            return None
        match = re.search(r"<!-- run-bytes: (\d+) -->", path.read_text())
        return int(match.group(1)) if match else None


def commit_digest(path: Path) -> bool:
    repo_root = path.resolve().parent.parent
    relative = path.resolve().relative_to(repo_root)
    subprocess.run(["git", "add", "--", str(relative)], cwd=repo_root, check=True)
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", str(relative)],
        cwd=repo_root,
        check=False,
    ).returncode
    if changed == 0:
        return False
    if changed != 1:
        raise RuntimeError("git could not inspect the staged digest")
    subprocess.run(
        [
            "git",
            "commit",
            "--only",
            "-m",
            f"Add {path.stem} lab digest",
            "--",
            str(relative),
        ],
        cwd=repo_root,
        check=True,
    )
    return True


def _directory_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _signed_bytes(value: int) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value} bytes since prior digest"
