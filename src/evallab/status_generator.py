"""STATUS.md Generator: deterministic projection of live catalog and queue state.

Generates and updates docs/STATUS.md to answer
"what happened yesterday and what is running now" deterministically
without requiring interactive terminal navigation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from evallab import database
from evallab.gc import parse_finished_at
from evallab.queue import QUEUE_STATES
from evallab.results import load_job
from evallab.runner import database_url_from_environment
from evallab.schemas import ExperimentSpec
from evallab.storm import (
    DEFAULT_STORM_THRESHOLD,
    StormAlarm,
    detect_storm_alarms,
    render_storm_banner,
)

DEFAULT_STATUS_PATH = Path("docs/STATUS.md")
PROGRAM_PATH = Path("research/experiments/PROGRAM.json")


@dataclass(frozen=True)
class TrialSummary:
    job_name: str
    task_name: str
    agent_name: str
    model_name: str | None
    reward: float | None
    exception_type: str | None
    cost_usd: float
    finished_at: str


@dataclass(frozen=True)
class QueueSpecItem:
    state: str
    spec_id: str
    name: str
    task: str
    agent: str
    model: str | None
    purpose: str | None
    reason: str | None = None


@dataclass(frozen=True)
class ProgramExperimentItem:
    exp_id: str
    research_question: str
    status: str
    blocker: str
    next_action: str
    notes: str


@dataclass
class StatusReportData:
    target_date: date
    reporting_date: date
    recent_trials: list[TrialSummary] = field(default_factory=list)
    recent_jobs: list[dict[str, Any]] = field(default_factory=list)
    catalog_accessible: bool = True
    trials_source: str = "catalog"
    catalog_error: str | None = None
    unreadable_jobs_count: int = 0
    running_specs: list[QueueSpecItem] = field(default_factory=list)
    approved_specs: list[QueueSpecItem] = field(default_factory=list)
    waiting_specs: list[QueueSpecItem] = field(default_factory=list)
    pending_specs: list[QueueSpecItem] = field(default_factory=list)
    proposed_specs: list[QueueSpecItem] = field(default_factory=list)
    program_experiments: list[ProgramExperimentItem] = field(default_factory=list)
    storm_alarms: list[StormAlarm] = field(default_factory=list)
    storm_error: str | None = None
    operational_smoke_count: int = 0
    raw_notes: list[str] = field(default_factory=list)

def _safe_load_spec(path: Path) -> tuple[ExperimentSpec | None, str | None]:
    try:
        return ExperimentSpec.model_validate_json(path.read_text()), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _load_reason_for_spec(queue_root: Path, spec_id: str) -> str | None:
    reasons_dir = queue_root / "reasons"
    if not reasons_dir.is_dir():
        return None
    for path in reasons_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text())
            if isinstance(payload, dict) and payload.get("spec_id") == spec_id:
                code = payload.get("code") or payload.get("reason_code")
                msg = payload.get("message")
                if code and msg:
                    return f"{code}: {msg}"
                return str(code or msg or "unknown")
        except Exception:
            continue
    return None


def _load_program_experiments(program_path: Path) -> list[ProgramExperimentItem]:
    if not program_path.is_file():
        return []
    try:
        data = json.loads(program_path.read_text())
        experiments = data.get("experiments", [])
        items: list[ProgramExperimentItem] = []
        for exp in experiments:
            if not isinstance(exp, dict):
                continue
            items.append(
                ProgramExperimentItem(
                    exp_id=str(exp.get("id", "")),
                    research_question=str(exp.get("research_question", "")),
                    status=str(exp.get("status", "")),
                    blocker=str(exp.get("blocker", "")),
                    next_action=str(exp.get("next_action", "")),
                    notes=str(exp.get("notes", "")),
                )
            )
        return items
    except Exception:
        return []


def collect_status_data(
    repo_root: Path,
    *,
    target_date: date | None = None,
    database_url: str | None = None,
    storm_threshold: int = DEFAULT_STORM_THRESHOLD,
    events_window: timedelta = timedelta(hours=24),
    storm_loader: Callable[[date], Sequence[StormAlarm]] | None = None,
    trial_loader: Callable[[date], Sequence[TrialSummary]] | None = None,
) -> StatusReportData:
    """Collect all live state needed to project STATUS.md."""
    resolved_root = repo_root.resolve()
    current_date = target_date or datetime.now(UTC).date()
    yesterday = current_date - timedelta(days=1)

    # 1. Load catalog / database trials
    catalog_accessible = True
    trials_source = "catalog"
    catalog_error: str | None = None
    unreadable_jobs_count = 0
    recent_trials: list[TrialSummary] = []

    if trial_loader is not None:
        try:
            recent_trials = list(trial_loader(yesterday))
            catalog_accessible = True
            trials_source = "catalog"
        except Exception as exc:
            catalog_accessible = False
            catalog_error = f"{type(exc).__name__}: {exc}"
    else:
        target_db_url = (
            database_url
            if database_url is not None
            else database_url_from_environment()
        )
        if target_db_url:
            try:
                rows = database.digest_trials(target_db_url, yesterday)
                for row in rows:
                    recent_trials.append(
                        TrialSummary(
                            job_name=str(row[0]),
                            task_name=str(row[1] or ""),
                            agent_name=str(row[2] or ""),
                            model_name=str(row[3]) if row[3] is not None else None,
                            reward=float(row[4]) if row[4] is not None else None,
                            exception_type=str(row[5]) if row[5] is not None else None,
                            cost_usd=float(row[6]) if row[6] is not None else 0.0,
                            finished_at=str(row[7]) if row[7] is not None else "",
                        )
                    )
                catalog_accessible = True
                trials_source = "catalog"
            except Exception as exc:
                catalog_accessible = False
                catalog_error = f"{type(exc).__name__}: {exc}"
        else:
            catalog_accessible = False
            catalog_error = "DATABASE_URL not configured"
    # Strict fallback: ONLY when catalog is inaccessible do we inspect the filesystem.
    # When catalog is accessible and returns no trials for yesterday, that is an authentic
    # zero-trial day ("nothing ran") and must NEVER be replaced by historical filesystem data.
    if not catalog_accessible:
        job_roots = [
            resolved_root / "research" / "evidence" / "runs",
            resolved_root / "runs",
        ]
        seen_jobs: set[Path] = set()
        trials_source = "filesystem"
        for root in job_roots:
            if not root.is_dir():
                continue
            for result_path in root.rglob("result.json"):
                candidate = result_path.parent
                if candidate in seen_jobs or any(
                    part.startswith(".") for part in candidate.relative_to(root).parts
                ):
                    continue
                seen_jobs.add(candidate)

                try:
                    payload = json.loads(result_path.read_text())
                except Exception:
                    unreadable_jobs_count += 1
                    continue

                if not isinstance(payload, dict):
                    unreadable_jobs_count += 1
                    continue

                # Only top-level completed jobs are loaded via load_job
                if not (
                    "n_total_trials" in payload
                    and "stats" in payload
                    and payload.get("finished_at")
                ):
                    # Check if this might be an errored/malformed job vs trial result
                    if "task_name" not in payload and "trial_name" not in payload:
                        unreadable_jobs_count += 1
                    continue

                try:
                    job = load_job(candidate)
                except Exception:
                    unreadable_jobs_count += 1
                    continue

                finished_str = str(job.result.get("finished_at") or "")
                finished_dt = parse_finished_at(finished_str)
                if finished_dt is None:
                    started_str = str(job.result.get("started_at") or "")
                    finished_dt = parse_finished_at(started_str)

                if finished_dt is None:
                    unreadable_jobs_count += 1
                    continue

                # STRICT DATE FILTER: only include jobs finished on the reporting date (yesterday)
                if finished_dt.date() != yesterday:
                    continue

                for trial in job.trials:
                    res = trial.result if isinstance(trial.result, dict) else {}
                    exc_info = res.get("exception_info")
                    exc_type = (
                        str(exc_info["exception_type"])
                        if isinstance(exc_info, dict) and "exception_type" in exc_info
                        else None
                    )
                    raw_agent_info = res.get("agent_info")
                    agent_dict = raw_agent_info if isinstance(raw_agent_info, dict) else {}
                    raw_model_info = agent_dict.get("model_info") or res.get("model_info")
                    model_dict = raw_model_info if isinstance(raw_model_info, dict) else {}

                    cfg_agent = job.config.get("agent") if isinstance(job.config, dict) else None
                    cfg_agent_dict = cfg_agent if isinstance(cfg_agent, dict) else {}
                    agent_name = str(
                        agent_dict.get("name")
                        or res.get("agent_name")
                        or cfg_agent_dict.get("name")
                        or "unknown"
                    )

                    model_val = model_dict.get("name") or model_dict.get("model_name")
                    model_name = str(model_val) if model_val is not None else None

                    cfg_task = job.config.get("task") if isinstance(job.config, dict) else None
                    cfg_task_dict = cfg_task if isinstance(cfg_task, dict) else {}
                    task_name = str(
                        res.get("task_name")
                        or cfg_task_dict.get("name")
                        or job.name
                    )
                    recent_trials.append(
                        TrialSummary(
                            job_name=job.name,
                            task_name=task_name,
                            agent_name=agent_name,
                            model_name=model_name,
                            reward=trial.primary_reward,
                            exception_type=exc_type,
                            cost_usd=float(res.get("cost_usd") or 0.0),
                            finished_at=finished_str,
                        )
                    )
    # Sort trials deterministically
    recent_trials.sort(key=lambda t: (t.task_name, t.job_name, t.agent_name))
    # 2. Queue state
    queue_root = resolved_root / "queue"
    specs_by_state: dict[str, list[QueueSpecItem]] = {s: [] for s in QUEUE_STATES}
    smoke_count = 0

    if queue_root.is_dir():
        for state in QUEUE_STATES:
            state_dir = queue_root / state
            if not state_dir.is_dir():
                continue
            for spec_file in sorted(state_dir.glob("*.json")):
                spec, _err = _safe_load_spec(spec_file)
                if spec is None:
                    continue
                if spec.name.startswith("oracle-") or spec.name.startswith("smoke-"):
                    smoke_count += 1
                reason = (
                    _load_reason_for_spec(queue_root, str(spec.spec_id))
                    if state in ("waiting", "rejected", "failed")
                    else None
                )
                specs_by_state[state].append(
                    QueueSpecItem(
                        state=state,
                        spec_id=str(spec.spec_id),
                        name=spec.name,
                        task=spec.task,
                        agent=spec.agent,
                        model=spec.model,
                        purpose=spec.purpose,
                        reason=reason,
                    )
                )

    # 3. PROGRAM.json items
    program_file = resolved_root / PROGRAM_PATH
    program_exps = _load_program_experiments(program_file)

    # 4. Storm alarms
    storm_alarms: list[StormAlarm] = []
    storm_error: str | None = None
    try:
        if storm_loader is not None:
            storm_alarms = list(storm_loader(current_date))
        else:
            since_time = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
            storm_alarms = list(
                detect_storm_alarms(
                    repo_root=resolved_root,
                    threshold=storm_threshold,
                    since=since_time,
                )
            )
    except Exception as exc:
        storm_error = f"{type(exc).__name__}: {exc}"

    return StatusReportData(
        target_date=current_date,
        reporting_date=yesterday,
        recent_trials=recent_trials,
        catalog_accessible=catalog_accessible,
        trials_source=trials_source,
        catalog_error=catalog_error,
        unreadable_jobs_count=unreadable_jobs_count,
        running_specs=specs_by_state.get("running", []),
        approved_specs=specs_by_state.get("approved", []),
        waiting_specs=specs_by_state.get("waiting", []),
        pending_specs=specs_by_state.get("pending", []),
        proposed_specs=specs_by_state.get("proposed", []),
        program_experiments=program_exps,
        storm_alarms=storm_alarms,
        storm_error=storm_error,
        operational_smoke_count=smoke_count,
    )


def render_status_markdown(data: StatusReportData) -> str:
    """Render deterministic Markdown representing research status."""
    lines: list[str] = [
        "---",
        "status: living",
        "audience:",
        "  - operator",
        "  - builder",
        "  - runner",
        "---",
        "",
        f"# Research status — {data.target_date.isoformat()}",
        "",
        "Projection of live catalog, queue state, and `PROGRAM.json`.",
        "Answers what happened yesterday and what is running now deterministically.",
        "",
    ]

    # Storm Alarm banner if active
    if data.storm_alarms:
        lines.append(render_storm_banner(data.storm_alarms))

    # Section 1: RECENT
    lines.extend(
        [
            f"## RECENT (Yesterday: {data.reporting_date.isoformat()})",
            "",
        ]
    )
    if data.recent_trials:
        if data.trials_source == "filesystem":
            lines.append("*(Source: filesystem fallback — catalog unavailable)*")
            lines.append("")

        # Group trials by task
        by_task: dict[str, list[TrialSummary]] = defaultdict(list)
        for t in data.recent_trials:
            by_task[t.task_name].append(t)

        for task_name in sorted(by_task.keys()):
            task_trials = by_task[task_name]
            total_trials = len(task_trials)
            success_count = sum(
                1 for t in task_trials if t.reward is not None and t.reward >= 1.0
            )
            exceptions = Counter(
                t.exception_type for t in task_trials if t.exception_type
            )
            agent_names = sorted({t.agent_name for t in task_trials if t.agent_name})
            model_names = sorted({t.model_name for t in task_trials if t.model_name})

            agent_info = ", ".join(agent_names) if agent_names else "unknown"
            model_info = f" ({', '.join(model_names)})" if model_names else ""

            exc_summary = ""
            if exceptions:
                exc_str = ", ".join(f"{k}={v}" for k, v in sorted(exceptions.items()))
                exc_summary = f" [exceptions: {exc_str}]"

            lines.append(
                f"- **{task_name}** — {success_count}/{total_trials} `reward==1.0` "
                f"via {agent_info}{model_info}{exc_summary}"
            )
        if data.unreadable_jobs_count > 0:
            plural = "y" if data.unreadable_jobs_count == 1 else "ies"
            msg = f"- *Warning:* {data.unreadable_jobs_count} job director{plural} unreadable."
            lines.append(msg)
        lines.append("")
    elif data.catalog_accessible:
        lines.extend(
            [
                "No completed trials observed in the reporting window.",
                "",
            ]
        )
        if data.unreadable_jobs_count > 0:
            plural = "y" if data.unreadable_jobs_count == 1 else "ies"
            msg = f"- *Warning:* {data.unreadable_jobs_count} job director{plural} unreadable."
            lines.append(msg)
            lines.append("")
    else:
        error_msg = f" ({data.catalog_error})" if data.catalog_error else ""
        lines.extend(
            [
                f"Source unavailable: catalog inaccessible{error_msg}.",
                "",
            ]
        )
        if data.unreadable_jobs_count > 0:
            plural = "y" if data.unreadable_jobs_count == 1 else "ies"
            msg = f"- *Warning:* {data.unreadable_jobs_count} job director{plural} unreadable."
            lines.append(msg)
            lines.append("")
    # Section 2: RUNNING NOW
    lines.extend(
        [
            "## RUNNING NOW",
            "",
        ]
    )

    active_specs = data.running_specs + data.approved_specs
    if not active_specs:
        lines.extend(
            [
                "Nothing in `queue/running/` or `queue/approved/`.",
                "",
            ]
        )
    else:
        for item in sorted(active_specs, key=lambda s: (s.state, s.name)):
            purpose_str = f" [purpose: {item.purpose}]" if item.purpose else ""
            lines.append(
                f"- `[{item.state.upper()}]` **{item.name}** (`{item.spec_id}`) — "
                f"task=`{item.task}`, agent=`{item.agent}`{purpose_str}"
            )
        lines.append("")

    # Section 3: NEXT
    lines.extend(
        [
            "## NEXT",
            "",
        ]
    )

    next_specs = data.waiting_specs + data.pending_specs + data.proposed_specs
    if not next_specs:
        lines.append(
            "No queued work waiting in `queue/waiting/`, `queue/pending/`, or `queue/proposed/`."
        )
    else:
        for item in sorted(next_specs, key=lambda s: (s.state, s.name)):
            reason_str = f" — *Reason/Blocker:* {item.reason}" if item.reason else ""
            purpose_str = f" [purpose: {item.purpose}]" if item.purpose else ""
            lines.append(
                f"- `[{item.state}]` **{item.name}** (`{item.spec_id}`): "
                f"task=`{item.task}`, agent=`{item.agent}`{purpose_str}{reason_str}"
            )
    lines.append("")

    # Section 4: PROGRAM EXPERIMENTS & OPEN DECISIONS
    if data.program_experiments:
        active_exps = [
            e
            for e in data.program_experiments
            if e.status in ("proposed", "waiting", "designed", "idea")
        ]
        if active_exps:
            lines.extend(
                [
                    "### Program Ledger Next Actions",
                    "",
                ]
            )
            for exp in active_exps:
                lines.append(
                    f"1. **{exp.exp_id}** (`status: {exp.status}`): {exp.research_question}"
                )
                if exp.blocker and exp.blocker.lower() != "none":
                    lines.append(f"   - *Blocker:* {exp.blocker}")
                if exp.next_action:
                    lines.append(f"   - *Next Action:* {exp.next_action}")
            lines.append("")

    # Section 5: TASK DECISIONS
    lines.extend(
        [
            "## TASK DECISIONS",
            "",
            "Human-owned, unresolved decisions from active proposals and policy review.",
            "",
        ]
    )

    # Extract blockers and unresolved notes from program
    decision_count = 0
    for exp in data.program_experiments:
        if exp.blocker and exp.blocker.lower() not in ("none", ""):
            decision_count += 1
            lines.append(f"- **{exp.exp_id}**: {exp.blocker}")
        elif "decision" in exp.notes.lower() or "unsubmitted" in exp.notes.lower():
            decision_count += 1
            lines.append(f"- **{exp.exp_id}**: {exp.notes}")

    if decision_count == 0:
        lines.append("- None currently blocking dispatch.")
    lines.append("")

    # Section 6: OPERATIONAL SMOKE & SYSTEM HEALTH
    if data.storm_error is not None:
        storm_str = f"unavailable ({data.storm_error})"
    elif not data.storm_alarms:
        storm_str = "0 (quiet: no alarms in window)"
    else:
        storm_str = f"{len(data.storm_alarms)} (active)"

    lines.extend(
        [
            "## SYSTEM HEALTH & OPERATIONAL SMOKE",
            "",
            f"- Catalog accessible: {'yes' if data.catalog_accessible else 'no'}",
            f"- Operational smoke/control specs count: {data.operational_smoke_count}",
            f"- Active storm alarms: {storm_str}",
        ]
    )
    if data.unreadable_jobs_count > 0:
        lines.append(f"- Unreadable job directories: {data.unreadable_jobs_count}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_status_markdown(
    repo_root: Path,
    *,
    target_date: date | None = None,
    database_url: str | None = None,
    storm_threshold: int = DEFAULT_STORM_THRESHOLD,
    storm_loader: Callable[[date], Sequence[StormAlarm]] | None = None,
    trial_loader: Callable[[date], Sequence[TrialSummary]] | None = None,
) -> str:
    """Generate the full STATUS.md content for a repository."""
    data = collect_status_data(
        repo_root,
        target_date=target_date,
        database_url=database_url,
        storm_threshold=storm_threshold,
        storm_loader=storm_loader,
        trial_loader=trial_loader,
    )
    return render_status_markdown(data)


def update_status_file(
    repo_root: Path,
    *,
    target_date: date | None = None,
    destination: Path | None = None,
    database_url: str | None = None,
    storm_threshold: int = DEFAULT_STORM_THRESHOLD,
    storm_loader: Callable[[date], Sequence[StormAlarm]] | None = None,
    trial_loader: Callable[[date], Sequence[TrialSummary]] | None = None,
) -> Path:
    """Generate and write STATUS.md to disk idempotently."""
    dest = destination or (repo_root / DEFAULT_STATUS_PATH)
    content = generate_status_markdown(
        repo_root,
        target_date=target_date,
        database_url=database_url,
        storm_threshold=storm_threshold,
        storm_loader=storm_loader,
        trial_loader=trial_loader,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return dest
