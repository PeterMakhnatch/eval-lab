"""Golden-file tests for digest and preflight rendering (WS-F).

Pins the full rendered text of `DigestRenderer` and `evallab preflight` so an
unintended formatting or content change fails CI instead of drifting. Clocks,
loaders, and queue contents are frozen; the only host value that still reaches
the surfaces is the tmp workspace path, which is replaced with `<TMP>` before
the byte comparison.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evallab.digest import DigestRenderer, DigestTrial
from evallab.preflight import build_preflight_report, render_preflight
from evallab.queue import DirectoryQueue, provider_reported_exhaustion
from evallab.schemas import (
    AutoRunRule,
    CanaryDriftObservation,
    QueueEvent,
    StandingApprovalsPolicy,
)

#: Fixed instant. Every clock the renderers accept is this value.
NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
REPORT_DATE = NOW.date()
PERIOD_DATE = date(2026, 8, 15)

#: 2026-08-20T18:32:49Z, the reset the committed evidence actually reports.
RESETS_AT_EPOCH = 1_787_250_769

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
TMP_PLACEHOLDER = "<TMP>"
WAITING_SPEC_ID = "01GOLDENWAIT00000000000000"


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_paid_trial(
    root: Path,
    *,
    agent: str = "codex",
    job_name: str = "canary-event-summary-codex-20260816",
    trial_name: str = "event-summary__1",
) -> Path:
    """One completed paid trial, the only thing that can carry a quota reading."""
    job = root / "runs" / job_name
    write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-0000000000ff",
            "started_at": "2026-08-16T05:00:00Z",
            "finished_at": "2026-08-16T06:00:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1},
        },
    )
    write_json(job / "lab-metadata.json", {"command": ["harbor", "run", "--agent", agent]})
    trial = job / trial_name
    write_json(
        trial / "result.json",
        {
            "id": f"trial-{trial_name}",
            "trial_name": trial_name,
            "task_name": "canary/event-summary",
            "started_at": "2026-08-16T05:30:00Z",
            "finished_at": "2026-08-16T05:40:00Z",
            "agent_info": {"name": agent, "model_info": {"name": "gpt-5.6-terra"}},
            "agent_result": {
                "n_input_tokens": 1_000,
                "n_cache_tokens": 800,
                "n_output_tokens": 20,
            },
        },
    )
    return trial


def add_quota_snapshot(
    trial: Path,
    *,
    observed_at: datetime,
    used_percent: float | None = 92.0,
    has_credits: bool = False,
    unlimited: bool = False,
    balance: str = "0",
    resets_at: int | None = RESETS_AT_EPOCH,
    rate_limit_reached_type: str | None = None,
) -> None:
    """The `rate_limits` block the Codex CLI attaches to a `token_count` event."""
    event = {
        "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1_000, "output_tokens": 20}},
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10_080,
                    "resets_at": resets_at,
                },
                "secondary": None,
                "credits": {
                    "has_credits": has_credits,
                    "unlimited": unlimited,
                    "balance": balance,
                },
                "plan_type": "prolite",
                "rate_limit_reached_type": rate_limit_reached_type,
            },
        },
    }
    rollout = trial / "agent/sessions/2026/08/16/rollout-2026-08-16T05-30-00-abc.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps(event) + "\n")


def queue_spec(
    queue_root: Path,
    *,
    state: str = "approved",
    name: str,
    task: str = "canary/event-summary",
    agent: str = "codex",
    attempts: int = 1,
    expected_reward: float | None = None,
    purpose: str | None = "drift",
    spec_id: str | None = None,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "spec_id": spec_id or name.upper().replace("-", ""),
        "name": name,
        "hypothesis": "a hypothesis",
        "task": task,
        "agent": agent,
        "attempts": attempts,
        "submitted_by": "tester",
    }
    if expected_reward is not None:
        payload["expected_reward"] = expected_reward
    if purpose is not None:
        payload["purpose"] = purpose
    path = queue_root / state / f"{agent}-{name}.json"
    write_json(path, payload)
    return path


def period_trials() -> list[DigestTrial]:
    return [
        DigestTrial(
            job_name="event-summary-oracle",
            task_name="library/tasks/event-summary",
            agent_name="oracle",
            model_name=None,
            reward=1.0,
            exception_type=None,
            cost_usd=0.0,
            finished_at="2026-08-15T18:00:00Z",
        ),
        DigestTrial(
            job_name="canary-event-summary-codex",
            task_name="canary/event-summary",
            agent_name="codex",
            model_name="gpt-5.6-terra",
            reward=0.0,
            exception_type=None,
            cost_usd=0.42,
            finished_at="2026-08-15T19:00:00Z",
        ),
        DigestTrial(
            job_name="broken-container",
            task_name="canary/event-summary",
            agent_name="codex",
            model_name="gpt-5.6-terra",
            reward=None,
            exception_type="EnvironmentError",
            cost_usd=0.0,
            finished_at="2026-08-15T19:10:00Z",
        ),
        DigestTrial(
            job_name="smoke-oracle-fixture",
            task_name="library/tasks/event-summary",
            agent_name="oracle",
            model_name=None,
            reward=1.0,
            exception_type=None,
            cost_usd=0.0,
            finished_at="2026-08-15T19:20:00Z",
        ),
    ]


def early_trials() -> list[DigestTrial]:
    return [
        DigestTrial(
            job_name="early-oracle",
            task_name="library/tasks/event-summary",
            agent_name="oracle",
            model_name=None,
            reward=1.0,
            exception_type=None,
            cost_usd=0.0,
            finished_at="2026-08-16T06:00:00Z",
        ),
    ]


def trial_loader(day: date) -> list[DigestTrial]:
    if day == PERIOD_DATE:
        return period_trials()
    if day == REPORT_DATE:
        return early_trials()
    return []


def drift_on(day: date) -> list[CanaryDriftObservation]:
    if day == PERIOD_DATE:
        return [
            CanaryDriftObservation(
                task_name="canary/event-summary",
                task_version="1.0.0",
                agent_name="codex",
                reward=1.0,
                attempt_count=3,
                exception_count=0,
                baseline_n=6,
                baseline_mean=1.0,
                baseline_stddev=0.0,
                previous_task_version="1.0.0",
                task_version_changed=False,
                is_harness_drift_suspect=False,
            ),
        ]
    if day == REPORT_DATE:
        return [
            CanaryDriftObservation(
                task_name="canary/transaction-reconciliation",
                task_version="1.1.0",
                agent_name="codex",
                reward=0.0,
                attempt_count=3,
                exception_count=0,
                baseline_n=6,
                baseline_mean=1.0,
                baseline_stddev=0.0,
                previous_task_version="1.0.0",
                task_version_changed=True,
                is_harness_drift_suspect=True,
                drift_reason="task_version_changed",
            ),
        ]
    return []


def populate_queue(queue: DirectoryQueue) -> None:
    """Frozen specs, reasons, and events. `report_date` is set so event-day
    membership does not depend on the host timezone (`astimezone()`).
    """
    root = queue.root
    queue_spec(root, name="base-a", purpose="baseline")
    queue_spec(
        root,
        name="cmp-alpha",
        purpose="comparison",
        task="t/alpha",
        attempts=4,
        expected_reward=0.5,
    )
    queue_spec(
        root,
        name="cmp-bravo",
        purpose="comparison",
        task="t/bravo",
        attempts=4,
        expected_reward=0.5,
    )
    queue_spec(
        root,
        name="cmp-charlie",
        purpose="comparison",
        task="t/charlie",
        attempts=4,
        expected_reward=0.5,
    )
    queue_spec(
        root,
        state="waiting",
        name="canary-run",
        purpose="drift",
        spec_id=WAITING_SPEC_ID,
    )
    queue_spec(
        root,
        state="proposed",
        name="oracle-control",
        agent="oracle",
        purpose="practice",
        task="library/tasks/event-summary",
    )
    write_json(
        queue.reasons_dir / f"{WAITING_SPEC_ID}-01GOLDENREASON000000000000.json",
        {
            "spec_id": WAITING_SPEC_ID,
            "occurred_at": "2026-08-16T11:00:00Z",
            "code": "daily_cost_ceiling",
            "message": "recorded by the gate",
        },
    )
    period_stamp = datetime(2026, 8, 15, 18, 0, 0, tzinfo=UTC)
    for index in range(3):
        queue.append_event(
            QueueEvent(
                event_id=f"event-tick-{index:02d}",
                spec_id=WAITING_SPEC_ID,
                occurred_at=period_stamp + timedelta(seconds=index),
                event="tick_idle",
                actor="executor",
                report_date=PERIOD_DATE.isoformat(),
            )
        )
    queue.append_event(
        QueueEvent(
            event_id="event-dispatch",
            spec_id="CMPALPHA",
            occurred_at=datetime(2026, 8, 15, 19, 0, 0, tzinfo=UTC),
            event="dispatch_started",
            actor="executor",
            job_name="canary-event-summary-codex",
            policy_rule="canary",
            report_date=PERIOD_DATE.isoformat(),
        )
    )
    queue.append_event(
        QueueEvent(
            event_id="event-quarantine",
            spec_id=WAITING_SPEC_ID,
            occurred_at=datetime(2026, 8, 16, 11, 30, 0, tzinfo=UTC),
            event="nightly_quarantined",
            actor="nightly",
            reason_code="headless_doctor_failed:docker_reachable",
            report_date=REPORT_DATE.isoformat(),
        )
    )


def build_workspace(root: Path) -> DirectoryQueue:
    trial = make_paid_trial(root)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(hours=2))
    queue = DirectoryQueue(root / "queue")
    populate_queue(queue)
    return queue


def frozen_preflight(root: Path):
    return build_preflight_report(
        root,
        now=NOW,
        paid_agents=("claude-code", "codex"),
        quota_roots=None,
        queue_root=root / "queue",
        refusal=provider_reported_exhaustion,
        refuse_at_used_percent=90.0,
        useful_effect=None,
    )


def frozen_renderer(root: Path, queue: DirectoryQueue) -> DigestRenderer:
    report = frozen_preflight(root)
    return DigestRenderer(
        repo_root=root,
        queue=queue,
        policy=policy(),
        trial_loader=trial_loader,
        drift_loader=drift_on,
        preflight_loader=lambda: report,
    )


def normalize_rendered(text: str, tmp_root: Path) -> str:
    """Replace the tmp workspace path with a stable placeholder.

    Absolute paths are the only residual host value that reaches these
    surfaces once clocks and loaders are frozen.
    """
    resolved = str(tmp_root.resolve())
    raw = str(tmp_root)
    for candidate in sorted({resolved, raw}, key=len, reverse=True):
        text = text.replace(candidate, TMP_PLACEHOLDER)
    return text


def render_digest_text(root: Path, queue: DirectoryQueue) -> str:
    path = frozen_renderer(root, queue).write(report_date=REPORT_DATE, health_report=None)
    return normalize_rendered(path.read_text(), root)


def render_preflight_text(root: Path) -> str:
    return normalize_rendered(render_preflight(frozen_preflight(root)), root)


def assert_matches_golden(actual: str, filename: str) -> None:
    path = GOLDEN_DIR / filename
    if os.environ.get("UPDATE_GOLDENS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
    expected = path.read_text()
    assert actual == expected


def test_digest_rendering_matches_golden(tmp_path: Path) -> None:
    queue = build_workspace(tmp_path)
    assert_matches_golden(render_digest_text(tmp_path, queue), "digest.md")


def test_preflight_rendering_matches_golden(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    assert_matches_golden(render_preflight_text(tmp_path), "preflight.txt")


def test_digest_rendering_is_stable_across_two_regenerations(tmp_path: Path) -> None:
    queue = build_workspace(tmp_path)
    first = render_digest_text(tmp_path, queue)
    second = render_digest_text(tmp_path, queue)
    assert first == second


def test_preflight_rendering_is_stable_across_two_regenerations(tmp_path: Path) -> None:
    build_workspace(tmp_path)
    first = render_preflight_text(tmp_path)
    second = render_preflight_text(tmp_path)
    assert first == second
