from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ItemPlan:
    id: str
    title: str = ""
    brief: str = ""
    thinking: str = "low"
    max_minutes: float = 50.0
    max_usd: float = 2.0
    needs: list[str] = field(default_factory=list)
    gate: str | None = None
    retry: int = 0
    tools: str = "read,bash,edit,write,grep,glob,todo"
    worktree: bool = False
    continue_item: bool = False
    continue_why: str = ""
    cmd_override: list[str] | None = None
    env_override: dict[str, str] | None = None
    route: str = ""
    raw_dir: str = ""
    session_dir: str = ""


@dataclass
class SquadPlan:
    model: str = "google-antigravity/gemini-3.8-flash"
    budget_usd: float = 10.0
    max_parallel: int = 2
    route_parallel_limits: dict[str, int] = field(default_factory=dict)
    subscription: bool = True
    items: list[ItemPlan] = field(default_factory=list)
    run_dir: str = ""
    quota_file: str | None = None
    common_md: str = ""
    worker_yml: str = ""
    continue_as_new_threshold: int = 100
    dry_ledger: bool = True


@dataclass
class ItemState:
    id: str
    status: str = "pending"  # pending, waiting-gate, running, done, blocked, failed, killed-time, killed-budget, skipped
    attempt: int = 0
    minutes: float = 0.0
    cost: float = 0.0
    note: str = ""
    t0: float = 0.0


@dataclass
class SquadStatus:
    model: str
    budget_usd: float
    cumulative_spend: float
    max_parallel: int
    running: list[str] = field(default_factory=list)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)

@dataclass
class ItemResult:
    id: str
    status: str
    minutes: float = 0.0
    cost: float = 0.0
    note: str = ""
    attestation: dict[str, Any] = field(default_factory=dict)


@dataclass
class LaunchWorkerRequest:
    item_id: str
    attempt: int
    cmd: list[str]
    cwd: str
    raw_dir: str
    env_vars: dict[str, str] = field(default_factory=dict)
    heartbeat_interval_sec: float = 5.0
    idempotency_key: str = ""


@dataclass
class LaunchResult:
    item_id: str
    attempt: int
    exit_code: int
    pid: int
    log_size: int
    elapsed_sec: float
    reused_prior_run: bool = False


@dataclass
class ReportResult:
    item_id: str
    status: str  # "done", "blocked", "failed"
    note: str = ""
    report_path: str = ""


@dataclass
class SettleSpendRequest:
    item_id: str
    attempt: int
    session_dir: str
    route: str
    subscription: bool = True
    dry_ledger: bool = True
    fixed_cost: float | None = None


@dataclass
class SpendResult:
    item_id: str
    cost: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    served: dict[str, int] = field(default_factory=dict)
    off_route: dict[str, int] = field(default_factory=dict)
    fallback_events: int = 0


@dataclass
class ItemWorkflowInput:
    item: ItemPlan
    plan: SquadPlan
    attempt: int = 0
    why: str = ""


@dataclass
class SquadWorkflowInput:
    plan: SquadPlan
    states: dict[str, ItemState] = field(default_factory=dict)
    cumulative_spend: float = 0.0
    completed_item_ids: list[str] = field(default_factory=list)
