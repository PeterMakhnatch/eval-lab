"""Data models and message specifications for CertifyGold durable workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MutantSummary:
    """Summary of a generated AST mutant."""

    operator: str
    line: int
    diff: str
    sha256: str
    is_train: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "line": self.line,
            "diff": self.diff,
            "sha256": self.sha256,
            "is_train": self.is_train,
        }


@dataclass
class PackageSpec:
    """Specification of a task package to certify."""

    package_id: str
    repo_name: str = ""
    reference_patch: str = ""
    python_version: str = "3"
    mutant_count: int = 4  # e.g., 2 train mutants, 2 eval mutants
    inject_failures: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "repo_name": self.repo_name,
            "reference_patch": self.reference_patch,
            "python_version": self.python_version,
            "mutant_count": self.mutant_count,
            "inject_failures": self.inject_failures,
        }


@dataclass
class CellSpec:
    """Specification for an evaluation cell."""

    cell_key: str
    package_id: str
    cell_type: str  # "reference", "nop", "witness", "exit0_pth", "train_mutant", "eval_mutant", "eval_base_mutant"
    operator: str = ""
    mutant_line: int = 0
    command: str = ""
    script: str = ""
    expected_reward: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "package_id": self.package_id,
            "cell_type": self.cell_type,
            "operator": self.operator,
            "mutant_line": self.mutant_line,
            "command": self.command,
            "script": self.script,
            "expected_reward": self.expected_reward,
        }


@dataclass
class CellResult:
    """Result of an evaluated cell."""

    cell_key: str
    package_id: str
    cell_type: str
    exit_code: int = 0
    reward: float = 0.0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    cache_hit: bool = False
    error: str | None = None
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "package_id": self.package_id,
            "cell_type": self.cell_type,
            "exit_code": self.exit_code,
            "reward": self.reward,
            "cost_usd": self.cost_usd,
            "wall_seconds": self.wall_seconds,
            "cache_hit": self.cache_hit,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class FakeCellConfig:
    """Configuration for fake activity runners (injectable costs and failures)."""

    results_dir: str = ""
    base_cost_usd: float = 0.005
    latency_seconds: float = 0.0
    inject_infra_errors: dict[str, int] = field(default_factory=dict)  # cell_key -> remaining error attempts
    inject_submission_failures: list[str] = field(default_factory=list)  # cell_keys that fail submission
    killed_eval_mutants: list[str] = field(default_factory=list)  # eval mutant cell_keys killed by synthesizer
    ledger_file: str = ""


@dataclass
class PackageCertificationResult:
    """Verdict and metrics for a single package certification."""

    package_id: str
    status: str  # "gold_structural", "gold_heldout", "not_gold", "cap_exhausted", "failed", "cancelled"
    failure_reason: str = ""
    structural_pass: bool = False
    heldout_kill_rate: float = 0.0
    killed_mutants: int = 0
    total_eval_mutants: int = 0
    cost_usd: float = 0.0
    cell_results: list[CellResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "structural_pass": self.structural_pass,
            "heldout_kill_rate": self.heldout_kill_rate,
            "killed_mutants": self.killed_mutants,
            "total_eval_mutants": self.total_eval_mutants,
            "cost_usd": self.cost_usd,
            "cell_results": [c.to_dict() for c in self.cell_results],
        }


@dataclass
class SettleSpendRequest:
    """Request payload for settling spend on the durable ledger."""

    package_id: str
    reserved_usd: float
    actual_usd: float
    ledger_file: str = ""


@dataclass
class SpendSummary:
    """Spend settlement summary."""

    package_id: str
    reserved_usd: float
    actual_usd: float
    cumulative_spend_usd: float


@dataclass
class CorpusPlan:
    """Top-level specification for a corpus certification campaign."""

    packages: list[PackageSpec] = field(default_factory=list)
    cap_usd: float = 5.0
    approver: str = "operator"
    cap_timeout_seconds: float = 60.0
    modal_concurrency: int = 4
    synth_concurrency: int = 2
    continue_as_new_threshold: int = 50  # cells before workflow.continue_as_new
    results_dir: str = ""
    manifest_output_dir: str = ""
    fake_config: FakeCellConfig = field(default_factory=FakeCellConfig)


@dataclass
class CorpusBoard:
    """Queryable status board for a running or completed corpus workflow."""

    total_packages: int = 0
    completed_packages: int = 0
    failed_packages: int = 0
    running_packages: list[str] = field(default_factory=list)
    paused: bool = False
    cumulative_spend: float = 0.0
    cap_usd: float = 0.0
    coverage_k: int = 0
    coverage_n: int = 0
    manifest_written: bool = False
    is_partial: bool = False
    package_statuses: dict[str, str] = field(default_factory=dict)


@dataclass
class CorpusManifest:
    """Aggregated certification manifest emitted at campaign end."""

    timestamp: str = ""
    total_packages: int = 0
    certified_packages: int = 0
    partial: bool = False
    coverage_k: int = 0
    coverage_n: int = 0
    cumulative_spend_usd: float = 0.0
    cap_usd: float = 0.0
    packages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_packages": self.total_packages,
            "certified_packages": self.certified_packages,
            "partial": self.partial,
            "coverage_k": self.coverage_k,
            "coverage_n": self.coverage_n,
            "cumulative_spend_usd": self.cumulative_spend_usd,
            "cap_usd": self.cap_usd,
            "packages": self.packages,
        }


@dataclass
class CorpusWorkflowInput:
    """Input payload for CertifyCorpusWorkflow across continue-as-new generations."""

    plan: CorpusPlan
    generation: int = 1
    prior_completed: list[PackageCertificationResult] = field(default_factory=list)
    prior_spend: float = 0.0
    pending_indices: list[int] = field(default_factory=list)
    current_cap: float | None = None
