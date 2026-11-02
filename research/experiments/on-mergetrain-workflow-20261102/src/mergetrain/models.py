"""Data models for the durable merge-train workflow."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass
class MergeTrainConfig:
    """Configuration parameters for the merge train."""

    repo_path: str = ""
    gh_repo: str = "PeterMakhnatch/eval-lab"
    worktrees_dir: str = ""
    gates_dir: str = ""
    required_checks: list[str] = field(
        default_factory=lambda: ["quality-required", "typecheck-required"]
    )
    generated_files: list[str] = field(
        default_factory=lambda: ["docs/repo-map.md", "docs/INDEX.md"]
    )
    generator_cmd: list[str] = field(
        default_factory=lambda: [sys.executable, "-m", "evallab.repomap", "generate"]
    )
    check_cmds: list[tuple[str, list[str]]] = field(
        default_factory=lambda: [
            ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
            ("repomap", [sys.executable, "-m", "evallab.repomap", "check"]),
            ("governance", [sys.executable, "-m", "evallab.governance", "check"]),
        ]
    )
    rebase_max_steps: int = 20
    ci_timeout_seconds: float = 1500.0  # 25 min
    ci_poll_interval_seconds: float = 1.0
    require_approval: bool = False
    dry_run: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MergeTrainInput:
    """Input parameters passed to MergeTrainWorkflow."""

    prs: list[int]
    config: MergeTrainConfig = field(default_factory=MergeTrainConfig)


@dataclass
class PrStatus:
    """Tracking status of an individual PR in the train."""

    pr_number: int
    state: str = "PENDING"  # PENDING, REBASING, WAITING_CI, WAITING_APPROVAL, MERGING, MERGED, NEEDS_HUMAN, CI_FAILED, SKIPPED, ALREADY_MERGED, ABORTED
    branch: str = ""
    head_oid: str = ""
    title: str = ""
    reason: str = ""
    merge_commit: str = ""
    updated_at: float = 0.0


@dataclass
class ViewPrParams:
    pr_number: int
    gh_repo: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ViewPrResult:
    pr_number: int
    state: str
    title: str
    head_ref_name: str
    head_ref_oid: str
    mergeable: str
    merge_state_status: str
    status_check_rollup: list[dict]
    is_draft: bool


@dataclass
class RebasePrParams:
    pr_number: int
    repo_path: str
    worktrees_dir: str
    head_ref_name: str
    head_ref_oid: str
    generated_files: list[str]
    generator_cmd: list[str]
    check_cmds: list[tuple[str, list[str]]]
    rebase_max_steps: int = 20
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class RebasePrResult:
    pr_number: int
    success: bool
    needs_human: bool = False
    reason: str = ""


@dataclass
class CheckCiParams:
    pr_number: int
    gh_repo: str
    required_checks: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckCiResult:
    pr_number: int
    is_green: bool
    is_failing: bool
    is_pending: bool
    failing_checks: list[str] = field(default_factory=list)


@dataclass
class MergePrParams:
    pr_number: int
    gh_repo: str
    branch_name: str
    gates_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MergePrResult:
    pr_number: int
    success: bool
    already_merged: bool = False
    merge_commit: str = ""
    message: str = ""
    error: str = ""
