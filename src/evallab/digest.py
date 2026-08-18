from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from evallab import database
from evallab.preflight import PreflightReport, build_preflight_report, digest_section
from evallab.queue import DirectoryQueue, load_events, provider_reported_exhaustion
from evallab.runner import (
    SUPPORT_COMMAND_TIMEOUT_SECONDS,
    database_url_from_environment,
    subscription_environment,
)
from evallab.schemas import (
    CanaryDriftObservation,
    HeadlessDoctorReport,
    JudgeCalibrationRecord,
    QueueEvent,
    QueueReason,
    StandingApprovalsPolicy,
)
from evallab.storm import StormAlarm, detect_storm_alarms, digest_storm_section


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


DriftLoader = Callable[[date], list[CanaryDriftObservation]]


PreflightLoader = Callable[[], PreflightReport]


StormLoader = Callable[[date], Sequence[StormAlarm]]


@dataclass(frozen=True)
class PendingDiscovery:
    discovery_id: str
    status: str
    claim: str
    relative_link: str


DiscoveriesLoader = Callable[[], list[PendingDiscovery]]

SELF_TEST_JOB_PREFIX = "smoke-"
"""Reserved job-name prefix marking a run as one of the lab's own self-tests.

`evallab smoke` names every job it creates `smoke-<agent>-<token>` and writes it
into the reserved scratch directory `runs/_smoke/` (`src/evallab/smoke.py`). The
prefix is an attribution the runs already carry, not a guess about content: no
run the lab reports as evidence is named this way, so an `oracle` or `nop`
control run can never be classified as noise by this rule.
"""


def is_lab_self_test(trial: DigestTrial) -> bool:
    """True for a run the lab generated to test itself rather than a model."""
    return trial.job_name.startswith(SELF_TEST_JOB_PREFIX)


def event_belongs_to_report_day(event: QueueEvent, day: date) -> bool:
    """Prefer a nightly event's semantic report date over its wall-clock date."""
    if event.report_date is not None:
        return event.report_date == day.isoformat()
    return event.occurred_at.astimezone().date() == day


class DigestRenderer:
    def __init__(
        self,
        *,
        repo_root: Path,
        queue: DirectoryQueue,
        policy: StandingApprovalsPolicy,
        trial_loader: TrialLoader | None = None,
        drift_loader: DriftLoader | None = None,
        preflight_loader: PreflightLoader | None = None,
        storm_loader: StormLoader | None = None,
        discoveries_loader: DiscoveriesLoader | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.queue = queue
        self.policy = policy
        self._trial_loader = trial_loader or self._load_catalog_trials
        self._drift_loader = drift_loader or self._load_canary_drift
        self._preflight_loader = preflight_loader or self._load_preflight
        self._storm_loader = storm_loader or self._load_storm_alarms
        self._discoveries_loader = (
            discoveries_loader
            if discoveries_loader is not None
            else self._load_pending_discoveries
        )

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
            drift = [
                (day, observation)
                for day in (period_date, report_date)
                for observation in self._drift_loader(day)
            ]
        except Exception:
            drift = []

        events = load_events(self.queue.events_path)
        period_events = self._events_on(events, period_date)
        report_events = self._events_on(events, report_date)
        policy_by_job = self._policy_by_job(events)
        quarantine_events = [
            event
            for event in period_events + report_events
            if event.event
            in {
                "nightly_quarantined",
                "tick_quarantined",
                "postgres_backup_failed",
                "digest_enrichment_failed",
            }
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
            f"# Eval lab digest — {report_date.isoformat()}",
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

        lines.append("")
        try:
            lines.extend(digest_section(self._preflight_loader()))
        except Exception as exc:
            # The preflight is a report about other reports; a failure in it must
            # never cost the nightly its trials, spend, or queue sections. Naming
            # the failure is the honest degradation — an omitted section would
            # read as "nothing to warn about".
            lines.extend(
                [
                    "## Preflight",
                    "",
                    f"- Unavailable: the preflight could not be built ({type(exc).__name__}: "
                    f"{exc}). That is not a statement that quota, queue, or power are fine.",
                ]
            )

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
                    "One row per canary per catalog day. Two rows for the same canary are "
                    "two days of that canary, not two verdicts about one day.",
                    "",
                    "| day | task | version | agent | reward | 7-day mean ± σ | n | assessment |",
                    "|---|---|---|---|---:|---:|---:|---|",
                ]
            )
            for day, observation in drift:
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
                    f"| {day.isoformat()} | {_cell(observation.task_name)} | "
                    f"{_cell(observation.task_version)} | "
                    f"{_cell(observation.agent_name)} | {reward} | {baseline} | "
                    f"{observation.baseline_n} | "
                    f"{_cell(assessment)} |"
                )
        else:
            lines.append("No canary observations with a trailing baseline.")

        spend = sum(trial.cost_usd for trial in trials + early_trials)
        exceptions = Counter(
            (
                "transient_harness"
                if trial.exception_type == "transient_harness"
                else "harness_failure"
            )
            for trial in trials + early_trials
            if trial.exception_type
        )
        exceptions["harness_failure"] += sum(
            observation.is_harness_drift_suspect for _day, observation in drift
        )
        if not exceptions["harness_failure"]:
            del exceptions["harness_failure"]
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
        # `list_specs` raises on any spec file this build cannot validate. Once
        # `ExperimentSpec.purpose` becomes required (WS-E item 1), every spec
        # queued before that landed is one such file — and a stale queue entry
        # must not be able to take down the whole nightly digest.
        try:
            waiting = self.queue.list_specs("waiting")
            unreadable = ""
        except (OSError, ValueError) as exc:
            waiting = []
            unreadable = f"{type(exc).__name__}: {exc}"
        if waiting:
            lines.extend(["", "| proposal | experiment | reason |", "|---|---|---|"])
            for _, spec in waiting:
                lines.append(
                    f"| {_cell(str(spec.spec_id))} | {_cell(spec.name)} | "
                    f"{_cell(self._reason_for(str(spec.spec_id)))} |"
                )
        elif unreadable:
            lines.append(f"- Waiting proposals: unreadable ({_cell(unreadable)})")
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
                _judge_calibration_line(self.repo_root),
                f"- Canary observations in report: {len(drift)}",
                "",
                "## Discoveries awaiting verdict",
                "",
            ]
        )
        try:
            pending_discoveries = self._discoveries_loader()
            if pending_discoveries:
                for disc in pending_discoveries:
                    lines.append(
                        f"- [**{disc.discovery_id}**]({disc.relative_link}) "
                        f"(`{disc.status}`) — {_cell(disc.claim)}"
                    )
            else:
                lines.append("No discoveries awaiting verdict.")
        except Exception as exc:
            lines.append(
                f"- Unavailable: discoveries could not be loaded ({type(exc).__name__}: {exc})."
            )
        lines.extend(
            [
                "",
                "## Queue events",
                "",
            ]
        )
        digest_events = period_events + report_events
        if digest_events:
            runs = _collapse_identical_runs(digest_events)
            if any(count > 1 for _first, _last, count in runs):
                lines.extend(
                    [
                        "A run of consecutive events identical in event, job, and "
                        "policy/reason collapses to one row carrying its repeat count and "
                        "time range. Every event that differs from the one before it is "
                        "listed on its own line, verbatim.",
                        "",
                    ]
                )
            lines.extend(["| time | event | job | policy/reason |", "|---|---|---|---|"])
            for first, last, count in runs:
                policy_or_reason = first.policy_rule or first.reason_code or ""
                when = (
                    first.occurred_at.isoformat()
                    if count == 1
                    else f"{first.occurred_at.isoformat()} – {last.occurred_at.isoformat()}"
                )
                label = first.event if count == 1 else f"{first.event} ×{count}"
                lines.append(
                    f"| {_cell(when)} | {_cell(label)} | "
                    f"{_cell(first.job_name or '')} | {_cell(policy_or_reason)} |"
                )
        else:
            lines.append("No queue events.")

        lines.append("")
        try:
            alarms = self._storm_loader(report_date)
            lines.extend(digest_storm_section(alarms))
        except Exception as exc:
            lines.extend(
                [
                    "## Storm alarms",
                    "",
                    f"- Unavailable: storm alarms could not be evaluated ({type(exc).__name__}: "
                    f"{exc}). That is not a statement that no event storm occurred.",
                ]
            )

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
        return database.canary_drift_observations(database_url_from_environment(), day)

    def _load_preflight(self) -> PreflightReport:
        """The default preflight for this checkout, read from disk only.

        The one clock read in the renderer, and the only reason it is here
        rather than in `preflight.py`: staleness needs a `now`, and every test
        injects `preflight_loader` to fix it (`agents/CHECKS.md`). Nothing in
        this path opens a socket, spawns a process, or spends anything —
        `provider_reported_exhaustion` reads the `Headroom` it is handed.
        """
        return build_preflight_report(
            self.repo_root,
            now=datetime.now(UTC),
            refusal=provider_reported_exhaustion,
            refuse_at_used_percent=getattr(
                self.policy, "refuse_billable_at_used_percent", None
            ),
        )

    def _load_pending_discoveries(self) -> list[PendingDiscovery]:
        discoveries_file = self.repo_root / "digests" / "DISCOVERIES.md"
        return parse_discoveries_awaiting_verdicts(discoveries_file)

    def _load_storm_alarms(self, day: date) -> list[StormAlarm]:
        period_date = day - timedelta(days=1)
        since_time = datetime.combine(period_date, datetime.min.time(), tzinfo=UTC)
        return detect_storm_alarms(
            self.queue.events_path,
            since=since_time,
        )

    @staticmethod
    def _events_on(events: list[QueueEvent], day: date) -> list[QueueEvent]:
        return [event for event in events if event_belongs_to_report_day(event, day)]

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
        # A self-test that raised is real signal about the harness, so it stays
        # a row. Only clean self-tests collapse into the summary below.
        reported = [
            trial for trial in trials if trial.exception_type or not is_lab_self_test(trial)
        ]
        summarised = [
            trial
            for trial in trials
            if not trial.exception_type and is_lab_self_test(trial)
        ]
        if not reported and not summarised:
            if empty:
                lines.append(empty)
            return
        if reported:
            lines.extend(
                [
                    "One row per job: a job that ran several trials shows the trial count "
                    "and every recorded reward, because 1/1/0 across three trials is not "
                    "the same fact as 1/1/1.",
                    "",
                    "| job | task | agent | trials | rewards | exceptions | policy |",
                    "|---|---|---|---:|---|---|---|",
                ]
            )
            for group in _group_trials(reported):
                head = group[0]
                lines.append(
                    f"| {_cell(head.job_name)} | {_cell(head.task_name)} | "
                    f"{_cell(head.agent_name)} | {len(group)} | "
                    f"{_cell(_reward_spread(group))} | "
                    f"{_cell(_exception_spread(group))} | "
                    f"{_cell(policy_by_job.get(head.job_name, 'unattributed'))} |"
                )
        elif empty:
            lines.append(empty)
        if summarised:
            if reported or empty:
                lines.append("")
            lines.append(
                f"Lab self-tests (job name starting `{SELF_TEST_JOB_PREFIX}`, produced by "
                "`evallab smoke`) are summarised here instead of listed. Any self-test that "
                "raised is a row in the table above, and every other run — including every "
                "`oracle` and `nop` control — is always listed. Spend and the exception "
                "taxonomy below count these trials."
            )
            lines.append("")
            lines.extend(_self_test_summary(summarised))

    @staticmethod
    def _failed_checks(
        report: HeadlessDoctorReport | None,
        quarantine_events: list[QueueEvent],
    ) -> list[str]:
        if report is not None:
            failed = [
                name for name, succeeded in report.checks.model_dump().items() if not succeeded
            ]
            if failed:
                return failed
        for event in reversed(quarantine_events):
            prefix = "headless_doctor_failed:"
            if event.reason_code and event.reason_code.startswith(prefix):
                return [name for name in event.reason_code.removeprefix(prefix).split(",") if name]
            if event.reason_code:
                return [event.reason_code]
        return []

    def _reason_for(self, spec_id: str) -> str:
        """The most recently *recorded* refusal rationale for this spec.

        Ordered by the `occurred_at` inside each record rather than by filename.
        `write_reason` names files `{spec_id}-{ULID}.json` (queue.py:808), and a
        ULID orders lexically only down to its 48-bit millisecond: two reasons
        written inside the same millisecond sort at random, because the low 80
        bits are. Since #65 made two reasons per spec ordinary — submit records
        `paid_run_unauthorized`, a later tick records the real refusal — the
        digest could print the earlier one as the current state.

        `occurred_at` is microsecond-resolution, so this narrows the tie window
        rather than closing it; an exact tie falls back to glob order. Making it
        total would mean ordering from the event log, which is a larger change
        than this defect earns.
        """
        recorded: list[QueueReason] = []
        for path in sorted(self.queue.reasons_dir.glob(f"{spec_id}-*.json")):
            try:
                recorded.append(QueueReason.model_validate_json(path.read_text()))
            except (OSError, ValidationError):
                continue
        if not recorded:
            return "awaiting human review"
        return max(recorded, key=lambda reason: reason.occurred_at).code

    def _prior_run_bytes(self, report_date: date) -> int | None:
        path = self.repo_root / "digests" / f"{report_date.isoformat()}.md"
        if not path.is_file():
            return None
        match = re.search(r"<!-- run-bytes: (\d+) -->", path.read_text())
        return int(match.group(1)) if match else None


def commit_digest(path: Path) -> bool:
    repo_root = path.resolve().parent.parent
    relative = path.resolve().relative_to(repo_root)
    environment = {
        **subscription_environment(),
        "GIT_EDITOR": ":",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def run_git(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=repo_root,
                check=check,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("bounded digest Git command failed") from exc

    run_git(["git", "add", "--", str(relative)], check=True)
    changed = run_git(
        ["git", "diff", "--cached", "--quiet", "--", str(relative)],
        check=False,
    ).returncode
    if changed == 0:
        return False
    if changed != 1:
        raise RuntimeError("git could not inspect the staged digest")
    run_git(
        [
            "git",
            "commit",
            "--only",
            "-m",
            f"Add {path.stem} lab digest",
            "--",
            str(relative),
        ],
        check=True,
    )
    return True


def _directory_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _self_test_summary(trials: list[DigestTrial]) -> list[str]:
    """One bullet per (task, agent) cohort, so a self-test is counted, not lost."""
    cohorts: dict[tuple[str, str], list[DigestTrial]] = {}
    for trial in trials:
        cohorts.setdefault((trial.task_name, trial.agent_name), []).append(trial)
    summary = []
    for (task_name, agent_name), cohort in sorted(cohorts.items()):
        rewards = [trial.reward for trial in cohort if trial.reward is not None]
        if not rewards:
            observed = "no recorded reward"
        elif min(rewards) == max(rewards):
            observed = f"reward {min(rewards):g}"
        else:
            observed = f"reward {min(rewards):g}–{max(rewards):g}"
        missing = len(cohort) - len(rewards)
        unscored = f", {missing} without a reward" if missing else ""
        plural = "" if len(cohort) == 1 else "s"
        # A summary that names none of its members cannot be followed back to a
        # run directory, so the cohort's most recent job stays quotable.
        latest = max(cohort, key=lambda trial: (trial.finished_at, trial.job_name))
        summary.append(
            f"- {len(cohort)} self-test trial{plural} — {task_name} / {agent_name}, "
            f"{observed}{unscored}, 0 exceptions (latest: {latest.job_name})"
        )
    return summary


def _group_trials(trials: list[DigestTrial]) -> list[list[DigestTrial]]:
    """One group per (job, task, agent), in first-seen order.

    A canary job runs the attempt count `policy/canary-suite.yaml` declares, but
    the catalog records no attempt ordinal — only an opaque Harbor trial name
    (`event-summary__5E3btLv`). Numbering the rows `attempt 1..3` would invent a
    sequence nothing recorded, so the honest rendering is one row per job with
    the trial count and the full reward spread.
    """
    groups: dict[tuple[str, str, str], list[DigestTrial]] = {}
    for trial in trials:
        groups.setdefault((trial.job_name, trial.task_name, trial.agent_name), []).append(trial)
    return list(groups.values())


def _reward_spread(trials: list[DigestTrial]) -> str:
    """Every recorded reward, so 1/1/0 can never render the same as 1/1/1."""
    rewards = sorted(trial.reward for trial in trials if trial.reward is not None)
    unscored = len(trials) - len(rewards)
    if not rewards:
        return "unscored" if len(trials) == 1 else f"{len(trials)} unscored"
    if not unscored and rewards[0] == rewards[-1]:
        return f"{rewards[0]:g}" if len(rewards) == 1 else f"{rewards[0]:g} ×{len(rewards)}"
    # Sorted, never chronological: the catalog orders trials by an opaque name,
    # so presenting them in order would imply a sequence it does not record.
    spread = ", ".join(f"{value:g}" for value in rewards)
    return f"{spread}, +{unscored} unscored" if unscored else spread


def _exception_spread(trials: list[DigestTrial]) -> str:
    counts = Counter(trial.exception_type for trial in trials if trial.exception_type)
    total = len(trials)
    return ", ".join(
        name if count == total else f"{name} ({count} of {total})"
        for name, count in sorted(counts.items())
    )


def _event_identity(event: QueueEvent) -> tuple[str, str, str]:
    return (
        event.event,
        event.job_name or "",
        event.policy_rule or event.reason_code or "",
    )


def _collapse_identical_runs(
    events: list[QueueEvent],
) -> list[tuple[QueueEvent, QueueEvent, int]]:
    """Fold each run of consecutive identical events into (first, last, count).

    Identity deliberately excludes the timestamp, so a half-hourly heartbeat
    collapses. It deliberately includes the reason code, so `tick_deferred |
    executor_busy` never disappears into a run of `tick_deferred |
    no_approved_specs`. A run of one is returned unchanged, which is what keeps
    every distinct event on its own verbatim line.
    """
    runs: list[tuple[QueueEvent, QueueEvent, int]] = []
    for event in events:
        if runs and _event_identity(runs[-1][0]) == _event_identity(event):
            first, _last, count = runs[-1]
            runs[-1] = (first, event, count + 1)
        else:
            runs.append((event, event, 1))
    return runs


def _measured_calibration_records(repo_root: Path) -> tuple[list[JudgeCalibrationRecord], int]:
    """Every committed judge-calibration record, split measured vs unreportable."""
    root = repo_root / "research/calibration/records"
    measured: list[JudgeCalibrationRecord] = []
    unreportable = 0
    for path in sorted(root.rglob("*.json")):
        try:
            record = JudgeCalibrationRecord.model_validate_json(path.read_text())
        except (OSError, ValidationError):
            # Not a calibration record: the directory also holds queue specs and
            # DSPy run notes. Silence here is correct; inventing a judge is not.
            continue
        if record.status == "measured":
            measured.append(record)
        else:
            unreportable += 1
    return measured, unreportable


def _judge_calibration_line(repo_root: Path) -> str:
    """Report the measured calibration state, never a brief number.

    `research/calibration/records/` is the committed record of every judge the
    lab has measured. Whether any judge is usable is exactly "does a measured
    record reach its own agreement floor", so that is what this reports.
    """
    measured, unreportable = _measured_calibration_records(repo_root)
    stub = f" {unreportable} non-measured record(s) are not reportable." if unreportable else ""
    if not measured:
        return (
            "- Judge calibration: no judge is calibrated — no measured record under "
            f"`research/calibration/records/`.{stub}"
        )
    passing = [record for record in measured if record.meets_floor]
    best = max(measured, key=lambda record: record.mean_agreement)
    detail = (
        f"{best.family} / {best.judge_backend} {best.judge_model}, mean agreement "
        f"{best.mean_agreement:.3f} against a {best.agreement_floor:.2f} floor over "
        f"{best.document_count} documents ({best.evaluated_on.isoformat()})"
    )
    if not passing:
        return (
            f"- Judge calibration: no judge is calibrated — 0 of {len(measured)} measured "
            f"record(s) reach their agreement floor, closest {detail}. No judged dimension "
            f"is reportable and the analysis worker's `calibrated_judges_only` admission "
            f"gate stays closed until one clears its floor.{stub}"
        )
    return (
        f"- Judge calibration: {len(passing)} of {len(measured)} measured record(s) reach "
        f"their agreement floor; best {detail}.{stub}"
    )


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _signed_bytes(value: int) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value} bytes since prior digest"


def parse_discoveries_awaiting_verdicts(
    discoveries_path: Path,
    *,
    database_url: str | None = None,
    max_entries: int = 5,
) -> list[PendingDiscovery]:
    if not discoveries_path.is_file():
        return []

    decided_statuses: dict[str, str] = {}
    try:
        from evallab.verdicts import list_current_verdicts_from_catalog

        verdicts = list_current_verdicts_from_catalog(database_url)
        for v in verdicts:
            decided_statuses[v.discovery_id] = v.status
    except (Exception, SystemExit):
        pass

    try:
        content = discoveries_path.read_text(encoding="utf-8")
    except Exception:
        return []

    entries: list[tuple[str, str, str]] = []
    current_id: str | None = None
    current_status: str = "draft"
    current_claim: str | None = None

    for line in content.splitlines():
        if line.startswith("## D-"):
            if current_id is not None:
                entries.append((current_id, current_status, current_claim or "draft finding"))
            header_part = line.removeprefix("## ").strip()
            parts = header_part.split(" — ", 1)
            current_id = parts[0].strip()
            current_status = parts[1].strip() if len(parts) > 1 else "draft"
            current_claim = None
        elif current_id is not None and line.startswith("- Claim:"):
            current_claim = line.removeprefix("- Claim:").strip()

    if current_id is not None:
        entries.append((current_id, current_status, current_claim or "draft finding"))

    pending: list[PendingDiscovery] = []
    for disc_id, status_str, claim in reversed(entries):
        effective_status = decided_statuses.get(disc_id, status_str)
        if effective_status in ("draft", "pending", "needs_evidence"):
            anchor = disc_id.lower()
            pending.append(
                PendingDiscovery(
                    discovery_id=disc_id,
                    status=effective_status,
                    claim=claim,
                    relative_link=f"DISCOVERIES.md#{anchor}",
                )
            )
        if len(pending) >= max_entries:
            break

    return pending
