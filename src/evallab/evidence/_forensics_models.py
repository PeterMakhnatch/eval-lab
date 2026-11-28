from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._forensics_join import StepEventLink
from ._herding_models import BatchHerdingSummary, HerdingMetrics
from .multiagent_adapter import GradeMarkRecord, TrialFormat
from .seat_failure_modes import FailureEpisode, FailureModeKind

__all__ = [
    "BatchHerdingSummary",
    "CollisionMetrics",
    "FailureModeKind",
    "HerdingMetrics",
    "IntegrityFlags",
    "JobForensicsReport",
    "MemberForensicsReport",
    "MessageFunnelMetrics",
    "TrialForensicsReport",
    "render_forensics_summary",
]


class MessageFunnelMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages_posted: int = 0
    messages_fetched: int = 0
    messages_in_context: int = 0
    messages_acted_on: int = 0
    unread_at_action: int = 0
    never_fetched: int = 0
    message_delivery_pairs: int = 0
    unique_messages_fetched: int = 0
    unique_messages_never_fetched: int = 0
    messages_fetched_but_ignored: int = 0
    unread_messages_at_action_total: int = 0
    unread_at_action_mean: float | None = None
    fetch_calls: int = 0
    fetch_lag_median_sec: float | None = None
    context_observable: bool = False


class CollisionMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_target_actions: int = 0
    stale_read_write_races: int = 0
    shared_branch_or_file_conflicts: int = 0
    post_end_of_play_events: int = 0
    informed_duplicate_actions: int = 0
    shared_branches: dict[str, list[str]] = Field(default_factory=dict)
    shared_files: dict[str, list[str]] = Field(default_factory=dict)
    proposed_branch_collisions: dict[str, list[str]] = Field(default_factory=dict)
    overwrite_events: int = 0
    collision_details: list[dict[str, Any]] = Field(default_factory=list)


class IntegrityFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs_complete: bool
    cost_complete: bool
    seat_error_with_reward: bool
    unauthenticated_author_mismatch: bool
    event_log_present: bool
    trial_events_present: bool
    git_bundle_present: bool
    unauthenticated_author_mismatch_count: int = 0
    reward_file_mismatch: bool = False
    network_not_enforced: bool = False
    budget_unbounded_subscription: bool = False
    covert_git_signaling_when_silent: bool = False
    issues: list[str] = Field(default_factory=list)


class MemberForensicsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str
    display_name: str | None = None
    role: str | None = None
    under_test: bool | None = None
    agent_name: str | None = None
    harness: str | None = None
    model_name: str | None = None
    model_provider: str | None = None
    ran_model: bool = False
    trajectory_status: str
    n_steps: int | None = None
    n_tool_calls: int | None = None
    n_actions: int = 0
    n_reads: int = 0
    n_refused: int = 0
    n_probes: int = 0
    exception_type: str | None = None
    seat_reward: float | None = None
    cost_usd: float | None = None
    cost_status: str
    usage_mismatches: list[str] = Field(default_factory=list)
    funnel: MessageFunnelMetrics
    collisions: CollisionMetrics
    logs_complete: bool
    cost_complete: bool
    unauthenticated_author_mismatch: bool
    linked_step_events: int = 0


class TrialForensicsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-forensics/v1"
    trial_name: str
    trial_id: str | None = None
    task_name: str | None = None
    ecosystem: TrialFormat
    orchestration: str | None = None
    board_policy: str | None = None
    comms_disabled: bool = False
    grade_marks: list[GradeMarkRecord] = Field(default_factory=list)
    source_formats: list[str] = Field(default_factory=list)
    rewards: dict[str, float] | None = None
    reward_valid: bool | None = None
    trial_exception_type: str | None = None
    total_cost_usd: float | None = None
    known_cost_usd: float | None = None
    total_input_tokens: int | None = None
    total_cache_tokens: int | None = None
    total_output_tokens: int | None = None
    n_members: int
    n_world_events: int
    n_lifecycle_events: int
    n_git_commits: int
    causal_links_count: int
    causal_links_by_basis: dict[str, int] = Field(default_factory=dict)
    end_of_play_source: str | None = None
    end_of_play_ts: str | None = None
    funnel: MessageFunnelMetrics
    collisions: CollisionMetrics
    herding: HerdingMetrics = Field(default_factory=HerdingMetrics)
    integrity: IntegrityFlags
    members: dict[str, MemberForensicsReport] = Field(default_factory=dict)
    causal_links: list[StepEventLink] = Field(default_factory=list)
    failure_episodes: list[FailureEpisode] = Field(default_factory=list)
    failure_mode_counts: dict[str, int] = Field(default_factory=dict)


def render_forensics_summary(report: Any) -> str:
    f, c = report.funnel, report.collisions
    core = [
        f"Funnel: posted={f.messages_posted}, fetched={f.messages_fetched}, "
        f"in_context={f.messages_in_context}, acted_on={f.messages_acted_on}, "
        f"unread_at_action={f.unread_at_action}, never_fetched={f.never_fetched}",
        f"Collisions: duplicate_target={c.duplicate_target_actions}, "
        f"stale_rw_races={c.stale_read_write_races}, "
        f"branch_or_file={c.shared_branch_or_file_conflicts}, "
        f"post_eop={c.post_end_of_play_events}",
    ]
    if hasattr(report, "members"):
        i = report.integrity
        lines = [
            f"Trial: {report.trial_name} ({report.ecosystem}, {report.n_members} members)",
            f"Events: world={report.n_world_events}, lifecycle={report.n_lifecycle_events}, "
            f"git_commits={report.n_git_commits}, links={report.causal_links_count}",
            *core,
            f"Integrity: logs_complete={i.logs_complete}, cost_complete={i.cost_complete}, "
            f"seat_error_with_reward={i.seat_error_with_reward}, "
            f"auth_mismatch={i.unauthenticated_author_mismatch}, "
            f"event_log_present={i.event_log_present}",
        ]
        for mid, m in report.members.items():
            label = f"{mid} ({m.display_name})" if m.display_name else mid
            lines.append(
                f"  - {label}: traj={m.trajectory_status}, cost={m.cost_usd} ({m.cost_status}), "
                f"posted={m.funnel.messages_posted}, fetched={m.funnel.messages_fetched}, "
                f"in_ctx={m.funnel.messages_in_context}, acted={m.funnel.messages_acted_on}, "
                f"unread_act={m.funnel.unread_at_action}, never_fetched={m.funnel.never_fetched}"
            )
        return "\n".join(lines)

    s = report.integrity_summary
    return "\n".join(
        [
            f"Batch: {report.n_trials} trials ({report.ecosystems})",
            *core,
            f"Integrity: logs_complete={s.get('logs_complete', 0)}/{report.n_trials}, "
            f"cost_complete={s.get('cost_complete', 0)}/{report.n_trials}, "
            f"seat_error_with_reward={s.get('seat_error_with_reward', 0)}, "
            f"auth_mismatch={s.get('unauthenticated_author_mismatch', 0)}, "
            f"event_log_present={s.get('event_log_present', 0)}/{report.n_trials}",
        ]
    )


class JobForensicsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-forensics-batch/v1"
    target_path: str
    job_id: str | None = None
    n_trials: int
    ecosystems: dict[str, int] = Field(default_factory=dict)
    funnel: MessageFunnelMetrics
    collisions: CollisionMetrics
    herding_summary: BatchHerdingSummary = Field(default_factory=BatchHerdingSummary)
    integrity_summary: dict[str, int] = Field(default_factory=dict)
    failure_mode_counts: dict[str, int] = Field(default_factory=dict)
    discovery_issues: list[str] = Field(default_factory=list)
    trials: list[TrialForensicsReport] = Field(default_factory=list)
