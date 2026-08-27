"""TrajectoryIR v1: Canonical intermediate representation for agent trajectories.

Translates raw Harbor trials, ATIF documents, verifier results, and state events
into an immutable, typed, citation-preserving intermediate representation:
- Supports CAS URI and inventory record ingestion directly from evidence-cas/
- Guaranteed TemporaryDirectory cleanup via try/finally across the entire post-restore assembly
- Normalized events with action family, status-owning program, and argument skeletons
- Expected-negative exit semantics conditioned on pinned semantics profiles
- Episode segmentation and explicit candidate screening windows (non-causal)
- Composed Quality Ledger integration (linkage degradation and quarantine status)
- Exact canonical CitationHandle provenance for every event and observation
- Explicit unknowns and coverage tracking for downstream judge calibration
- Rebuildable Parquet and JSON representations with deterministic content digests
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from evallab.evidence_store import restore_evidence
from evallab.results import sha256_file
from evallab.traj import (
    outline_trajectory,
    resolve_trial_target,
)
from evallab.traj_baseline import TraceBaselineRecord, compute_trace_baseline
from evallab.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    create_citation_handle,
)

# Known POSIX programs where exit code 1 indicates an expected negative/non-match
DEFAULT_EXPECTED_NEGATIVE_PROGRAMS = frozenset(
    {"grep", "egrep", "fgrep", "diff", "cmp", "test", "[", "rg", "ag"}
)

VERIFICATION_PROGRAMS = frozenset(
    {
        "pytest",
        "unittest",
        "jest",
        "mocha",
        "vitest",
        "cargo_test",
        "cargo",
        "go_test",
        "go",
        "ctest",
        "verify",
        "test.sh",
        "verify.sh",
        "run_tests",
        "check",
    }
)

INSPECTION_PROGRAMS = frozenset(
    {
        "cat",
        "ls",
        "find",
        "head",
        "tail",
        "grep",
        "rg",
        "ag",
        "less",
        "more",
        "stat",
        "file",
        "wc",
        "read",
        "read_file",
        "view",
    }
)

MUTATION_PROGRAMS = frozenset(
    {
        "edit",
        "sed",
        "awk",
        "patch",
        "tee",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "touch",
        "write",
        "write_file",
        "append",
        "str_replace_editor",
    }
)


def _sha256_canonical_json(data: Any) -> str:
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _extract_status_owning_program(command: str | None, tool_name: str | None) -> str | None:
    """Identify the status-owning program of an action (pipeline-aware)."""
    if not command and tool_name:
        return tool_name.lower()
    if not command:
        return None

    # Handle pipeline: status is owned by the rightmost command unless pipefail
    segments = [s.strip() for s in command.split("|")]
    last_segment = segments[-1] if segments else command

    # Strip environment variable prefixes (e.g. FOO=1 bar)
    tokens = last_segment.split()
    cmd_tokens = [t for t in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]

    if not cmd_tokens:
        return tool_name.lower() if tool_name else None

    raw_prog = Path(cmd_tokens[0]).name.lower()
    # Handle sudo, env, xargs wrappers
    if raw_prog in ("sudo", "env", "xargs", "nohup", "time") and len(cmd_tokens) > 1:
        cmd_tokens = cmd_tokens[1:]
        raw_prog = Path(cmd_tokens[0]).name.lower()

    # Handle python -m <module>
    if raw_prog in ("python", "python3", "py") and len(cmd_tokens) >= 3 and cmd_tokens[1] in ("-m", "-c"):
        return f"{raw_prog}_{cmd_tokens[2]}"

    # Handle cargo / git subcommands (e.g. cargo test, git diff)
    if raw_prog in ("cargo", "git", "npm", "bun", "yarn", "docker") and len(cmd_tokens) > 1:
        subcmd = cmd_tokens[1].lower()
        if not subcmd.startswith("-"):
            return f"{raw_prog}_{subcmd}"

    return raw_prog


def _normalize_argument_skeleton(command: str | None, tool_args: Any | None) -> str | None:
    """Abstract file paths, hex hashes, and literals into a robust argument skeleton."""
    if command:
        # Strip long hexadecimal strings / digests
        skel = re.sub(r"[0-9a-fA-F]{16,}", "<DIGEST>", command)
        # Abstract file paths (relative or absolute)
        skel = re.sub(r"(?:\b|(?<=\s))[a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9_.-]+)+\b", "<PATH>", skel)
        skel = re.sub(r"(?:/[a-zA-Z0-9_.-]+)+", "<PATH>", skel)
        # Compact whitespace
        skel = re.sub(r"\s+", " ", skel).strip()
        return skel[:200]

    if isinstance(tool_args, dict):
        keys = sorted(tool_args.keys())
        return f"args({','.join(keys)})"
    elif tool_args:
        return str(tool_args)[:100]

    return None


def _classify_action_family(
    program: str | None,
    tool_name: str | None,
    is_edit: bool,
) -> str:
    """Classify the high-level semantic intent of an action."""
    prog = (program or "").lower()
    tool = (tool_name or "").lower()

    if is_edit or prog in MUTATION_PROGRAMS or tool in MUTATION_PROGRAMS:
        return "file_edit"
    if any(prog.startswith(vp) for vp in VERIFICATION_PROGRAMS) or tool in ("pytest", "verify"):
        return "verification"
    if prog in INSPECTION_PROGRAMS or tool in INSPECTION_PROGRAMS:
        return "file_read"
    if prog or tool in ("bash", "execute_command", "terminal", "run_command", "exec"):
        return "command_execution"

    return "other"


def _classify_exit_semantics(
    exit_code: int | None,
    program: str | None,
    is_error: bool,
    expected_negative_programs: frozenset[str] = DEFAULT_EXPECTED_NEGATIVE_PROGRAMS,
) -> tuple[str, bool]:
    """Classify exit code semantics, recognizing expected-negative POSIX conventions.
    
    Returns (exit_semantics, is_true_error).
    """
    if exit_code is None:
        return ("error", True) if is_error else ("unobserved", False)

    if exit_code == 0:
        return "success", False

    prog_base = (program or "").split("_")[0].lower()
    if exit_code == 1 and prog_base in expected_negative_programs:
        return "expected_negative", False

    if exit_code in (124, 137):
        return "timeout", True

    return "error", True


@dataclass(frozen=True)
class IROpportunityWindow:
    """Explicit candidate opportunity window where a tool choice, recovery, or intervention occurred."""

    opportunity_id: str
    opportunity_type: str  # "tool_selection_candidate" | "error_recovery_candidate" | "verification_candidate" | "state_mutation_candidate" | "context_compaction_candidate"
    step_index: int
    action_family: str
    status_owning_program: str | None
    has_prior_error: bool
    has_subsequent_recovery: bool
    state_before_digest: str | None
    state_after_digest: str | None
    reopening_citation: CitationHandle
    description: str
    is_screening_only: bool = True
    evidence_basis: str = "screening_heuristic"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reopening_citation"] = self.reopening_citation.to_dict()
        return d


@dataclass(frozen=True)
class IREvent:
    """Normalized atomic event in the trajectory intermediate representation."""

    event_id: str
    event_ordinal: int
    event_type: str  # "user_message" | "agent_message" | "tool_call" | "observation" | "state_change" | "verifier_check" | "context_management"
    actor: str  # "user" | "agent" | "system" | "verifier" | "environment" | "subagent"
    timestamp: str | None
    phase: str  # "setup" | "prompt" | "work" | "verifier" | "unknown"
    episode_id: int
    step_index: int
    call_index: int | None
    action_family: str  # "file_read" | "file_write" | "file_edit" | "command_execution" | "verification" | "context_control" | "model_reasoning" | "other"
    status_owning_program: str | None
    argument_skeleton: str | None
    exit_code: int | None
    exit_semantics: str  # "success" | "expected_negative" | "error" | "timeout" | "unobserved"
    is_error: bool
    payload_digest: str
    payload_bytes: int
    source_citation: CitationHandle
    summary: str
    tool_schema_digest: str | None = None
    matched_result_digest: str | None = None
    state_before_digest: str | None = None
    state_after_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_citation"] = self.source_citation.to_dict()
        return d


@dataclass(frozen=True)
class IREpisode:
    """Segmented execution episode representing a functional phase of problem-solving."""

    episode_id: int
    name: str
    episode_type: str  # "setup" | "instruction" | "inspection" | "mutation" | "verification" | "screening_recovery" | "loop" | "terminal"
    start_ordinal: int
    end_ordinal: int
    event_count: int
    tool_call_count: int
    error_count: int
    has_state_mutation: bool
    has_verification: bool
    summary: str
    key_citations: tuple[CitationHandle, ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key_citations"] = [c.to_dict() for c in self.key_citations]
        return d


GraphEdgeType = Literal[
    "chronological_sequence",
    "tool_call_response",
    "error_to_recovery",
    "mutation_to_verification",
    "context_compaction_edge",
]


def deterministic_ir_edge_id(
    trial_id: str,
    source_node_id: str,
    target_node_id: str,
    edge_type: str,
) -> str:
    """Generate a deterministic sha256 primary key for an IR graph edge."""
    parts = (trial_id, source_node_id, target_node_id, edge_type)
    return hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IRGraphNode:
    """Projected node representation of an atomic TrajectoryIR event."""

    node_id: str
    trial_id: str
    event_ordinal: int
    step_index: int
    episode_id: int
    node_type: str
    actor: str
    phase: str
    action_family: str
    status_owning_program: str | None
    exit_semantics: str
    is_error: bool
    payload_digest: str
    source_citation: CitationHandle
    summary: str
    attributes_json: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_citation"] = self.source_citation.to_dict()
        return d

    def to_projection_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "trial_id": self.trial_id,
            "event_ordinal": self.event_ordinal,
            "step_index": self.step_index,
            "episode_id": self.episode_id,
            "node_type": self.node_type,
            "actor": self.actor,
            "phase": self.phase,
            "action_family": self.action_family,
            "status_owning_program": self.status_owning_program or "",
            "exit_semantics": self.exit_semantics,
            "is_error": self.is_error,
            "payload_digest": self.payload_digest,
            "source_citation_path": self.source_citation.source_path,
            "source_citation_sha256": self.source_citation.source_sha256,
            "summary": self.summary,
            "attributes_json": self.attributes_json,
        }


@dataclass(frozen=True)
class IRGraphEdge:
    """Directed semantic edge connecting two nodes in TrajectoryIR."""

    edge_id: str
    trial_id: str
    source_node_id: str
    target_node_id: str
    edge_type: GraphEdgeType
    weight: float = 1.0
    metadata_json: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_projection_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "trial_id": self.trial_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "edge_type": self.edge_type,
            "weight": self.weight,
            "metadata_json": self.metadata_json,
        }


@dataclass(frozen=True)
class TrajectoryGraphProjection:
    """Projected Directed Acyclic Graph (DAG) over a TrajectoryIR instance."""

    ir_digest: str
    trial_id: str
    nodes: tuple[IRGraphNode, ...]
    edges: tuple[IRGraphEdge, ...]
    node_count: int
    edge_count: int
    edge_type_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_digest": self.ir_digest,
            "trial_id": self.trial_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "edge_type_counts": self.edge_type_counts,
        }


def project_ir_graph(ir: TrajectoryIR) -> TrajectoryGraphProjection:
    """Project TrajectoryIR events and citations into a typed, deterministic graph."""
    trial_id = ir.trial_id
    sorted_events = sorted(ir.events, key=lambda e: e.event_ordinal)

    # 1. Build Nodes
    nodes: list[IRGraphNode] = []
    for ev in sorted_events:
        attr = {
            "call_index": ev.call_index,
            "argument_skeleton": ev.argument_skeleton,
            "exit_code": ev.exit_code,
            "payload_bytes": ev.payload_bytes,
            "tool_schema_digest": ev.tool_schema_digest,
            "matched_result_digest": ev.matched_result_digest,
            "state_before_digest": ev.state_before_digest,
            "state_after_digest": ev.state_after_digest,
        }
        nodes.append(
            IRGraphNode(
                node_id=ev.event_id,
                trial_id=trial_id,
                event_ordinal=ev.event_ordinal,
                step_index=ev.step_index,
                episode_id=ev.episode_id,
                node_type=ev.event_type,
                actor=ev.actor,
                phase=ev.phase,
                action_family=ev.action_family,
                status_owning_program=ev.status_owning_program,
                exit_semantics=ev.exit_semantics,
                is_error=ev.is_error,
                payload_digest=ev.payload_digest,
                source_citation=ev.source_citation,
                summary=ev.summary,
                attributes_json=json.dumps(attr, sort_keys=True),
            )
        )

    # 2. Extract Edges
    edges: list[IRGraphEdge] = []
    n = len(sorted_events)

    # Rule A: chronological_sequence (consecutive event transitions)
    for i in range(n - 1):
        src, tgt = sorted_events[i], sorted_events[i + 1]
        e_type: GraphEdgeType = "chronological_sequence"
        edges.append(
            IRGraphEdge(
                edge_id=deterministic_ir_edge_id(trial_id, src.event_id, tgt.event_id, e_type),
                trial_id=trial_id,
                source_node_id=src.event_id,
                target_node_id=tgt.event_id,
                edge_type=e_type,
                weight=1.0,
                metadata_json=json.dumps({"source_ordinal": src.event_ordinal, "target_ordinal": tgt.event_ordinal}),
            )
        )

    # Rule B: tool_call_response (tool_call -> matching observation)
    tool_calls_by_id: dict[str, IREvent] = {}
    for ev in sorted_events:
        tc_id = ev.source_citation.tool_call_id
        if ev.event_type == "tool_call" and tc_id:
            tool_calls_by_id[tc_id] = ev
        elif (
            ev.event_type == "observation"
            and ev.source_citation.source_call_id
            and ev.source_citation.source_call_id in tool_calls_by_id
        ):
            call_ev = tool_calls_by_id[ev.source_citation.source_call_id]
            e_type = "tool_call_response"
            edges.append(
                IRGraphEdge(
                    edge_id=deterministic_ir_edge_id(trial_id, call_ev.event_id, ev.event_id, e_type),
                    trial_id=trial_id,
                    source_node_id=call_ev.event_id,
                    target_node_id=ev.event_id,
                    edge_type=e_type,
                    weight=1.0,
                    metadata_json=json.dumps({"tool_call_id": ev.source_citation.source_call_id, "exit_code": ev.exit_code}),
                )
            )

    # Rule C: error_to_recovery (failed action -> subsequent successful resolution)
    for i in range(n):
        ev = sorted_events[i]
        if ev.is_error or ev.exit_semantics in ("error", "timeout"):
            for j in range(i + 1, min(i + 10, n)):
                cand = sorted_events[j]
                if cand.actor in ("agent", "environment") and not cand.is_error and cand.exit_semantics == "success":
                    e_type = "error_to_recovery"
                    edges.append(
                        IRGraphEdge(
                            edge_id=deterministic_ir_edge_id(trial_id, ev.event_id, cand.event_id, e_type),
                            trial_id=trial_id,
                            source_node_id=ev.event_id,
                            target_node_id=cand.event_id,
                            edge_type=e_type,
                            weight=1.0,
                            metadata_json=json.dumps({
                                "error_program": ev.status_owning_program,
                                "recovery_program": cand.status_owning_program,
                                "gap_events": j - i,
                            }),
                        )
                    )
                    break

    # Rule D: mutation_to_verification (state edit/write -> subsequent verification)
    for i in range(n):
        ev = sorted_events[i]
        if ev.action_family in ("file_edit", "file_write"):
            for j in range(i + 1, n):
                cand = sorted_events[j]
                if cand.action_family == "verification" or cand.event_type == "verifier_check":
                    e_type = "mutation_to_verification"
                    edges.append(
                        IRGraphEdge(
                            edge_id=deterministic_ir_edge_id(trial_id, ev.event_id, cand.event_id, e_type),
                            trial_id=trial_id,
                            source_node_id=ev.event_id,
                            target_node_id=cand.event_id,
                            edge_type=e_type,
                            weight=1.0,
                            metadata_json=json.dumps({
                                "mutation_program": ev.status_owning_program,
                                "verification_program": cand.status_owning_program,
                                "gap_events": j - i,
                            }),
                        )
                    )
                    break

    # Rule E: context_compaction_edge (context management -> next agent step)
    for i in range(n):
        ev = sorted_events[i]
        if ev.event_type == "context_management" or ev.action_family == "context_control":
            for j in range(i + 1, n):
                cand = sorted_events[j]
                if cand.actor == "agent":
                    e_type = "context_compaction_edge"
                    edges.append(
                        IRGraphEdge(
                            edge_id=deterministic_ir_edge_id(trial_id, ev.event_id, cand.event_id, e_type),
                            trial_id=trial_id,
                            source_node_id=ev.event_id,
                            target_node_id=cand.event_id,
                            edge_type=e_type,
                            weight=1.0,
                            metadata_json=json.dumps({"compacted_event_ordinal": ev.event_ordinal}),
                        )
                    )
                    break

    edge_counts: dict[str, int] = {}
    for edge in edges:
        edge_counts[edge.edge_type] = edge_counts.get(edge.edge_type, 0) + 1

    return TrajectoryGraphProjection(
        ir_digest=ir.ir_digest,
        trial_id=trial_id,
        nodes=tuple(nodes),
        edges=tuple(sorted(edges, key=lambda e: e.edge_id)),
        node_count=len(nodes),
        edge_count=len(edges),
        edge_type_counts=edge_counts,
    )

@dataclass(frozen=True)
class TrajectoryIR:
    """Complete, deterministic intermediate representation of an evaluated trajectory."""

    ir_version: str
    ir_digest: str
    trial_id: str
    job_id: str
    trial_name: str
    job_name: str
    task_name: str
    task_digest: str | None
    verifier_digest: str | None
    agent_scaffold: str
    agent_version: str | None
    model_name: str
    status: str  # "featured" | "accounted_unavailable"
    unavailable_reason: str | None
    final_verdict: str  # "PASS" | "FAIL" | "PARTIAL" | "EVIDENCE_UNAVAILABLE" | "VERIFIER_ERROR" | "EXCEPTION"
    primary_reward: float | None
    exception_class: str | None
    exception_message: str | None
    duration_seconds: float | None
    total_tokens: int | None
    cost_usd: float | None
    quality_status: str  # "pass" | "warn" | "fail" | "quarantined" | "no_atif" | "unknown"
    quality_findings: tuple[str, ...]
    unpaired_tool_calls_count: int
    linkage_coverage: str  # "complete" | "degraded" | "unlinked"
    is_production_cas: bool
    events: tuple[IREvent, ...]
    episodes: tuple[IREpisode, ...]
    opportunity_windows: tuple[IROpportunityWindow, ...]
    unknowns: tuple[dict[str, str], ...]
    baseline_metrics: TraceBaselineRecord
    evidence_coverage: dict[str, Any]
    source_digests: dict[str, str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_version": self.ir_version,
            "ir_digest": self.ir_digest,
            "trial_id": self.trial_id,
            "job_id": self.job_id,
            "trial_name": self.trial_name,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "task_digest": self.task_digest,
            "verifier_digest": self.verifier_digest,
            "agent_scaffold": self.agent_scaffold,
            "agent_version": self.agent_version,
            "model_name": self.model_name,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "final_verdict": self.final_verdict,
            "primary_reward": self.primary_reward,
            "exception_class": self.exception_class,
            "exception_message": self.exception_message,
            "duration_seconds": self.duration_seconds,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "quality_status": self.quality_status,
            "quality_findings": list(self.quality_findings),
            "unpaired_tool_calls_count": self.unpaired_tool_calls_count,
            "linkage_coverage": self.linkage_coverage,
            "is_production_cas": self.is_production_cas,
            "events": [e.to_dict() for e in self.events],
            "episodes": [ep.to_dict() for ep in self.episodes],
            "opportunity_windows": [op.to_dict() for op in self.opportunity_windows],
            "unknowns": list(self.unknowns),
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "evidence_coverage": self.evidence_coverage,
            "source_digests": self.source_digests,
            "created_at": self.created_at,
        }

    def to_projection_dict(self) -> dict[str, Any]:
        """Flat projection row matching DuckDB trajectory_ir table and v_trajectory_ir_summary view."""
        return {
            "ir_digest": self.ir_digest,
            "trial_id": self.trial_id,
            "job_id": self.job_id,
            "trial_name": self.trial_name,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "task_digest": self.task_digest or "",
            "verifier_digest": self.verifier_digest or "",
            "agent_scaffold": self.agent_scaffold,
            "agent_version": self.agent_version or "",
            "model_name": self.model_name,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason or "",
            "final_verdict": self.final_verdict,
            "primary_reward": self.primary_reward,
            "exception_class": self.exception_class or "",
            "duration_seconds": self.duration_seconds,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "quality_status": self.quality_status,
            "quality_findings_json": json.dumps(list(self.quality_findings)),
            "unpaired_tool_calls_count": self.unpaired_tool_calls_count,
            "linkage_coverage": self.linkage_coverage,
            "is_production_cas": self.is_production_cas,
            "total_events": len(self.events),
            "total_episodes": len(self.episodes),
            "total_opportunities": len(self.opportunity_windows),
            "created_at": self.created_at,
        }


def _segment_episodes(events: Sequence[IREvent]) -> tuple[IREpisode, ...]:
    """Segment ordered IR events into cohesive problem-solving episodes."""
    if not events:
        return ()

    episodes: list[IREpisode] = []
    curr_events: list[IREvent] = []
    curr_type: str = "setup"
    episode_id = 1

    def _flush_episode() -> None:
        nonlocal episode_id, curr_events, curr_type
        if not curr_events:
            return
        start_ord = curr_events[0].event_ordinal
        end_ord = curr_events[-1].event_ordinal
        tc_count = sum(1 for e in curr_events if e.event_type == "tool_call")
        err_count = sum(1 for e in curr_events if e.is_error)
        has_mut = any(e.action_family in ("file_edit", "file_write") for e in curr_events)
        has_verif = any(e.action_family == "verification" for e in curr_events)
        citations = tuple(e.source_citation for e in curr_events if e.is_error or e.action_family == "verification" or e.event_type == "verifier_check")
        if not citations:
            citations = (curr_events[0].source_citation,)

        summary = f"{curr_type.capitalize()} episode: {len(curr_events)} event(s), {tc_count} tool call(s), {err_count} error(s)"
        episodes.append(
            IREpisode(
                episode_id=episode_id,
                name=f"{curr_type}_{episode_id}",
                episode_type=curr_type,
                start_ordinal=start_ord,
                end_ordinal=end_ord,
                event_count=len(curr_events),
                tool_call_count=tc_count,
                error_count=err_count,
                has_state_mutation=has_mut,
                has_verification=has_verif,
                summary=summary,
                key_citations=citations[:5],
            )
        )
        episode_id += 1
        curr_events = []

    for ev in events:
        if ev.phase == "setup" or ev.actor in ("setup", "system"):
            target_type = "setup"
        elif ev.phase == "prompt" or ev.actor == "user":
            target_type = "instruction"
        elif ev.event_type == "verifier_check" or ev.phase == "verifier":
            target_type = "terminal"
        elif ev.action_family == "verification":
            target_type = "verification"
        elif ev.action_family in ("file_edit", "file_write"):
            target_type = "mutation"
        elif ev.is_error:
            target_type = "screening_recovery"
        else:
            target_type = "inspection"

        if curr_events and curr_type != target_type and len(curr_events) >= 3:
            _flush_episode()
            curr_type = target_type
        elif not curr_events:
            curr_type = target_type

        curr_events.append(ev)

    _flush_episode()
    return tuple(episodes)


class CASTrialResolutionError(ValueError):
    """Raised when a specified CAS archive cannot resolve one exact trial root."""


def _resolve_restored_trial_dir(
    extracted_root: Path,
    inventory_record: dict[str, Any],
) -> Path:
    """Select the exact trial root inside a restored trial- or job-level CAS archive."""
    trial_name = str(inventory_record.get("trial_name") or "")
    if trial_name and (
        Path(trial_name).name != trial_name or trial_name in {".", ".."}
    ):
        raise CASTrialResolutionError(
            f"invalid CAS trial_name path component: {trial_name!r}"
        )

    def has_trajectory(path: Path) -> bool:
        return (
            (path / "agent" / "trajectory.json").is_file()
            or (path / "trajectory.json").is_file()
        )

    if has_trajectory(extracted_root):
        return extracted_root

    nested_trials = sorted(
        (
            child
            for child in extracted_root.iterdir()
            if child.is_dir() and has_trajectory(child)
        ),
        key=lambda path: path.name,
    )

    if trial_name:
        named_trial = (extracted_root / trial_name).resolve()
        if not named_trial.is_relative_to(extracted_root.resolve()):
            raise CASTrialResolutionError(
                f"CAS trial_name escapes archive root: {trial_name!r}"
            )
        if not named_trial.is_dir():
            raise CASTrialResolutionError(
                f"named trial is absent from CAS archive: {trial_name!r}"
            )
        if has_trajectory(named_trial):
            return named_trial
        if nested_trials:
            raise CASTrialResolutionError(
                f"named CAS trial has no trajectory while other trials do: {trial_name!r}"
            )
        return named_trial

    if len(nested_trials) == 1:
        return nested_trials[0]
    if len(nested_trials) > 1:
        raise CASTrialResolutionError(
            "CAS archive contains multiple trials but no trial_name"
        )
    return extracted_root


def build_trajectory_ir(
    target: str | Path | dict[str, Any],
    *,
    repo_root: Path | None = None,
    explicit_runs_root: Path | None = None,
    store_root: Path | None = None,
    policy: RedactionPolicy | None = None,
) -> TrajectoryIR:
    """Build a deterministic, typed TrajectoryIR instance from CAS evidence or trial directory."""
    root = (repo_root or Path.cwd()).resolve()
    cas_store = (store_root or root / "derived" / "evidence-cas").resolve()
    if not cas_store.exists():
        alt_cas = root / "evidence" / "cas"
        if alt_cas.exists():
            cas_store = alt_cas

    if policy is None:
        policy = RedactionPolicy()

    is_cas = False
    cas_uri: str | None = None
    inventory_record: dict[str, Any] = {}
    temp_extract_dir: tempfile.TemporaryDirectory[str] | None = None
    cas_archive_root: Path | None = None

    trial_target_path: str | Path
    if isinstance(target, dict):
        inventory_record = target
        cas_uri = target.get("cas_uri")
        trial_target_path = str(target.get("trial_name") or target.get("trial_dir") or target.get("atif_path") or target.get("trial_id") or "")
    elif isinstance(target, str) and target.startswith("cas://"):
        cas_uri = target
        trial_target_path = target
    elif isinstance(target, Path):
        trial_target_path = target
    else:
        trial_target_path = str(target)

    trial_dir: Path
    traj_path: Path | None
    result_path: Path | None

    try:
        if cas_uri:
            if not cas_store.exists():
                raise FileNotFoundError(f"CAS store does not exist: {cas_store}")
            temp_extract_dir = tempfile.TemporaryDirectory()
            extracted_path = Path(temp_extract_dir.name)
            restore_evidence(cas_store, cas_uri, extracted_path)
            is_cas = True
            cas_archive_root = extracted_path
            trial_dir = _resolve_restored_trial_dir(extracted_path, inventory_record)
            traj_cand = trial_dir / "agent" / "trajectory.json"
            if not traj_cand.is_file():
                traj_cand = trial_dir / "trajectory.json"
            traj_path = traj_cand if traj_cand.is_file() else None
            res_cand = trial_dir / "result.json"
            result_path = res_cand if res_cand.is_file() else None
            outline = outline_trajectory(
                trial_dir,
                repo_root=root,
                explicit_runs_root=extracted_path,
            )
        else:
            try:
                trial_dir, traj_path, result_path = resolve_trial_target(
                    trial_target_path, repo_root=root, explicit_runs_root=explicit_runs_root
                )
                outline = outline_trajectory(
                    trial_target_path, repo_root=root, explicit_runs_root=explicit_runs_root
                )
            except Exception:
                job_name = inventory_record.get("job_name")
                if explicit_runs_root and job_name and (explicit_runs_root / job_name).is_dir():
                    job_dir = explicit_runs_root / job_name
                    trial_dir = _resolve_restored_trial_dir(job_dir, inventory_record)
                    traj_cand = trial_dir / "agent" / "trajectory.json"
                    if not traj_cand.is_file():
                        traj_cand = trial_dir / "trajectory.json"
                    traj_path = traj_cand if traj_cand.is_file() else None
                    res_cand = trial_dir / "result.json"
                    result_path = res_cand if res_cand.is_file() else None
                    outline = outline_trajectory(
                        trial_dir,
                        repo_root=root,
                        explicit_runs_root=explicit_runs_root,
                    )
                else:
                    raise

        baseline = compute_trace_baseline(outline)

        result_data: dict[str, Any] = {}
        result_sha: str | None = None
        if result_path and result_path.is_file():
            result_sha = sha256_file(result_path)
            try:
                result_data = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                result_data = {}

        task_digest = (
            inventory_record.get("task_digest")
            or str(result_data.get("task_checksum") or result_data.get("task_digest") or "")
            or None
        )

        lock_dict: dict[str, Any] = {}
        lock_path = trial_dir / "lock.json"
        if lock_path.is_file():
            with contextlib.suppress(Exception):
                lock_dict = json.loads(lock_path.read_text(encoding="utf-8", errors="replace"))

        verifier_digest = (
            inventory_record.get("verifier_digest")
            or str(result_data.get("verifier_digest") or "")
            or str(result_data.get("verifier_checksum") or "")
            or None
        )
        if not verifier_digest:
            verifier_spec = lock_dict.get("verifier") or (result_data.get("config") or {}).get("verifier")
            if verifier_spec:
                verifier_payload = json.dumps({"task_digest": task_digest, "verifier": verifier_spec}, sort_keys=True)
                verifier_digest = f"sha256:{hashlib.sha256(verifier_payload.encode()).hexdigest()}"

        manifest_candidates = (
            trial_dir / "manifest.json",
            trial_dir / "artifacts" / "manifest.json",
        )
        manifest_path = next((p for p in manifest_candidates if p.is_file()), None)
        manifest_digest = f"sha256:{sha256_file(manifest_path)}" if manifest_path else None

        raw_agent_info = result_data.get("agent_info")
        agent_info: dict[str, Any] = raw_agent_info if isinstance(raw_agent_info, dict) else {}
        raw_config = result_data.get("config")
        config_dict: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
        raw_agent_cfg = config_dict.get("agent")
        agent_cfg: dict[str, Any] = raw_agent_cfg if isinstance(raw_agent_cfg, dict) else {}
        raw_model_info = agent_info.get("model_info")
        model_info: dict[str, Any] = raw_model_info if isinstance(raw_model_info, dict) else {}

        agent_scaffold = (
            str(inventory_record.get("agent_scaffold") or agent_info.get("name") or agent_cfg.get("name") or outline.agent_name)
        )
        model_name = (
            str(inventory_record.get("model_name") or model_info.get("name") or outline.model_name)
        )

        quality_status = str(inventory_record.get("quality_status") or "unknown")
        quality_findings: list[str] = list(inventory_record.get("quality_findings") or [])

        if quality_status == "unknown":
            q_file = trial_dir / "quality.json"
            if q_file.is_file():
                try:
                    q_data = json.loads(q_file.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(q_data, dict):
                        quality_status = str(q_data.get("quality_status") or q_data.get("status") or "unknown")
                        quality_findings = list(q_data.get("quality_findings") or q_data.get("findings") or q_data.get("reasons") or [])
                except Exception:
                    pass

        ledger_unpaired_count = sum(1 for finding in quality_findings if "UNPAIRED" in finding)
        # Raw ATIF linkage is authoritative; stale ledger findings are retained as unknowns below.
        unpaired_count = ledger_unpaired_count
        linkage_coverage = "degraded" if unpaired_count > 0 else "complete"

        primary_reward = baseline.primary_reward if baseline.primary_reward is not None else inventory_record.get("reward")
        exception_class = baseline.exception_class or inventory_record.get("exception_type")
        exception_message: str | None = None
        if isinstance(result_data.get("exception_info"), dict):
            exception_message = result_data["exception_info"].get("exception_message")
        elif isinstance(result_data.get("exception"), dict):
            exception_message = result_data["exception"].get("message")

        if outline.status == "accounted_unavailable" or quality_status == "no_atif":
            unavail = outline.unavailable_reason or quality_status or "missing_trajectory_file"
            final_verdict = f"EVIDENCE_UNAVAILABLE ({unavail})"
        elif exception_class:
            if "Reward" in str(exception_class) or "Verifier" in str(exception_class):
                final_verdict = f"VERIFIER_ERROR ({exception_class})"
            else:
                final_verdict = f"EXCEPTION ({exception_class})"
        elif primary_reward is not None:
            final_verdict = "PASS" if primary_reward >= 1.0 else ("FAIL" if primary_reward == 0.0 else f"PARTIAL ({primary_reward:.2f})")
        else:
            final_verdict = "UNKNOWN"

        events: list[IREvent] = []

        traj_payload: dict[str, Any] | None = None
        if traj_path and traj_path.is_file():
            try:
                traj_payload = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                traj_payload = None

        raw_steps_map: dict[str, dict[str, Any]] = {}
        if traj_payload and isinstance(traj_payload.get("steps"), list):
            for s in traj_payload["steps"]:
                if isinstance(s, dict) and s.get("step_id") is not None:
                    raw_steps_map[str(s["step_id"])] = s

        rel_source_path = outline.source_path
        citation_root = cas_archive_root or trial_dir
        try:
            cand_p = Path(outline.source_path)
            if cand_p.is_absolute():
                rel_source_path = (
                    cand_p.resolve().relative_to(citation_root.resolve()).as_posix()
                )
        except Exception:
            rel_source_path = "agent/trajectory.json"

        event_ord_counter = 0
        for step in outline.steps:
            raw_step = raw_steps_map.get(str(step.step_id)) or {}
            raw_tool_calls = raw_step.get("tool_calls")
            raw_observations = raw_step.get("observations")
            raw_observation = raw_step.get("observation")
            raw_results = (
                raw_observation.get("results")
                if isinstance(raw_observation, dict)
                else None
            )
            if (
                not isinstance(raw_observations, list)
                or (not raw_observations and isinstance(raw_results, list) and raw_results)
            ):
                raw_observations = raw_results
            if not isinstance(raw_observations, list):
                raw_observations = []

            is_user = step.source == "user"
            is_system = step.source in ("system", "setup")
            is_verifier = step.source == "verifier"
            is_compaction = bool(
                raw_step.get("extra", {}).get("context_management")
                or raw_step.get("context_management")
            )

            phase_name = "work"
            if is_system:
                phase_name = "setup"
            elif is_user:
                phase_name = "prompt"
            elif is_verifier:
                phase_name = "verifier"

            if isinstance(raw_tool_calls, list) and raw_tool_calls:
                # Multi-call preservation: unpack every individual tool call
                for call_idx, tc in enumerate(raw_tool_calls):
                    tc_dict = tc if isinstance(tc, dict) else {}
                    tc_name = (
                        tc_dict.get("name")
                        or tc_dict.get("function_name")
                        or (
                            tc_dict.get("function", {}).get("name")
                            if isinstance(tc_dict.get("function"), dict)
                            else None
                        )
                        or step.tool_name
                        or "tool"
                    )
                    tc_args = tc_dict.get("arguments") or (
                        tc_dict.get("function", {}).get("arguments")
                        if isinstance(tc_dict.get("function"), dict)
                        else None
                    )
                    tc_call_id = tc_dict.get("tool_call_id") or tc_dict.get("id")
                    raw_command = (
                        tc_args.get("cmd") or tc_args.get("command") or tc_args.get("input")
                        if isinstance(tc_args, dict)
                        else tc_args if isinstance(tc_args, str) else step.tool_command
                    )
                    wrapped_command = re.search(r'"cmd"\s*:\s*"((?:\\.|[^"])*)"', raw_command or "")
                    cmd_str = (
                        json.loads(f'"{wrapped_command.group(1)}"')
                        if wrapped_command is not None
                        else raw_command
                    )
                    if cmd_str and "exec_command({" in cmd_str:
                        cmd_str = None
                    prog = _extract_status_owning_program(cmd_str, tc_name)
                    skeleton = _normalize_argument_skeleton(cmd_str, tc_args)
                    action_family = _classify_action_family(prog, tc_name, is_edit=bool(tc_name == "edit"))

                    # Join only exact source_call_id identities; positional fallback is forbidden.
                    matching_obs: dict[str, Any] | None = None
                    matching_obs_index: int | None = None
                    if tc_call_id is not None:
                        for obs_index, obs in enumerate(raw_observations):
                            if isinstance(obs, dict) and (
                                obs.get("source_call_id") == tc_call_id
                                or obs.get("tool_call_id") == tc_call_id
                            ):
                                matching_obs = obs
                                matching_obs_index = obs_index
                                break

                    if matching_obs is not None:
                        obs_extra = matching_obs.get("extra") if isinstance(matching_obs.get("extra"), dict) else {}
                        exit_code = obs_extra.get("exit_code") if "exit_code" in obs_extra else matching_obs.get("exit_code")
                        obs_is_error = bool(obs_extra.get("is_error") or matching_obs.get("is_error") or (exit_code is not None and exit_code != 0))
                        exit_sem, is_true_err = _classify_exit_semantics(exit_code, prog, obs_is_error)
                    else:
                        exit_code = None
                        exit_sem = "unobserved"
                        is_true_err = False

                    ev_id = hashlib.sha256(
                        f"{outline.trial_id}:{event_ord_counter}:tool_call:{step.step_id}:{call_idx}".encode()
                    ).hexdigest()

                    citation = create_citation_handle(
                        source_path=rel_source_path,
                        source_sha256=outline.source_sha256,
                        raw_cas_uri=cas_uri,
                        step_id=step.step_id,
                        call_index=call_idx,
                        tool_call_id=tc_call_id,
                        source_call_id=tc_call_id,
                        target_type="tool_call",
                        ir_event_id=ev_id,
                        redaction_profile_digest=policy.compute_digest(),
                    )

                    tool_payload = json.dumps({"tool_call": tc_dict})
                    p_bytes = len(tool_payload.encode("utf-8"))
                    p_digest = f"sha256:{hashlib.sha256(tool_payload.encode('utf-8')).hexdigest()}"
                    cmd_snip = f": {str(cmd_str)[:60]}" if cmd_str else ""
                    summary = f"Tool {tc_name}{cmd_snip}"

                    event = IREvent(
                        event_id=ev_id,
                        event_ordinal=event_ord_counter,
                        event_type="tool_call",
                        actor=step.source,
                        timestamp=step.timestamp,
                        phase=phase_name,
                        episode_id=1,
                        step_index=step.step_id,
                        call_index=call_idx,
                        action_family=action_family,
                        status_owning_program=prog,
                        argument_skeleton=skeleton,
                        exit_code=None,
                        exit_semantics="unobserved",
                        is_error=False,
                        payload_digest=p_digest,
                        payload_bytes=p_bytes,
                        source_citation=citation,
                        summary=summary,
                    )
                    events.append(event)
                    event_ord_counter += 1
                    if matching_obs is not None and matching_obs_index is not None:
                        observation_event_id = hashlib.sha256(
                            f"{outline.trial_id}:{event_ord_counter}:observation:{step.step_id}:{matching_obs_index}".encode()
                        ).hexdigest()
                        observation_citation = create_citation_handle(
                            trial_id=outline.trial_id,
                            source_path=rel_source_path,
                            source_sha256=outline.source_sha256,
                            raw_cas_uri=cas_uri,
                            step_id=step.step_id,
                            source_call_id=tc_call_id,
                            observation_index=matching_obs_index,
                            target_type="observation",
                            ir_event_id=observation_event_id,
                            redaction_profile_digest=policy.compute_digest(),
                        )
                        observation_payload = json.dumps({"observation": matching_obs})
                        events.append(
                            IREvent(
                                event_id=observation_event_id,
                                event_ordinal=event_ord_counter,
                                event_type="observation",
                                actor="environment",
                                timestamp=step.timestamp,
                                phase=phase_name,
                                episode_id=1,
                                step_index=step.step_id,
                                call_index=None,
                                action_family="other",
                                status_owning_program=None,
                                argument_skeleton=None,
                                exit_code=exit_code,
                                exit_semantics=exit_sem,
                                is_error=is_true_err,
                                payload_digest=f"sha256:{hashlib.sha256(observation_payload.encode('utf-8')).hexdigest()}",
                                payload_bytes=len(observation_payload.encode("utf-8")),
                                source_citation=observation_citation,
                                summary=f"Observation for {tc_name}",
                            )
                        )
                        event_ord_counter += 1
            else:
                # Single non-tool or single-step event
                event_type = (
                    "context_management"
                    if is_compaction
                    else (
                        "tool_call"
                        if step.tool_name
                        else (
                            "user_message"
                            if is_user
                            else ("verifier_check" if is_verifier else ("agent_message" if step.source == "agent" else "observation"))
                        )
                    )
                )

                prog = _extract_status_owning_program(step.tool_command, step.tool_name)
                skeleton = _normalize_argument_skeleton(step.tool_command, raw_step.get("tool_calls"))
                action_family = "context_control" if is_compaction else _classify_action_family(prog, step.tool_name, is_edit=bool(step.tool_name == "edit"))
                exit_sem, is_true_err = _classify_exit_semantics(step.exit_code, prog, step.is_error)

                citation = create_citation_handle(
                    source_path=rel_source_path,
                    source_sha256=outline.source_sha256,
                    raw_cas_uri=cas_uri,
                    step_id=step.step_id,
                    target_type="step",
                    redaction_profile_digest=policy.compute_digest(),
                )

                payload_str = json.dumps(raw_step) if raw_step else (step.thought_snippet or step.tool_command or "")
                p_bytes = len(payload_str.encode("utf-8"))
                p_digest = f"sha256:{hashlib.sha256(payload_str.encode('utf-8')).hexdigest()}"

                summary_parts = []
                if step.tool_name:
                    cmd_snip = f": {step.tool_command[:60]}" if step.tool_command else ""
                    summary_parts.append(f"Tool {step.tool_name}{cmd_snip}")
                elif step.thought_snippet:
                    summary_parts.append(step.thought_snippet[:80])
                else:
                    summary_parts.append(f"Step {step.step_id} ({step.source})")

                if step.exit_code is not None:
                    summary_parts.append(f"[exit {step.exit_code}]")

                summary = " ".join(summary_parts)

                ev_id = hashlib.sha256(
                    f"{outline.trial_id}:{event_ord_counter}:{event_type}:{step.step_id}".encode()
                ).hexdigest()

                event = IREvent(
                    event_id=ev_id,
                    event_ordinal=event_ord_counter,
                    event_type=event_type,
                    actor=step.source,
                    timestamp=step.timestamp,
                    phase=phase_name,
                    episode_id=1,
                    step_index=step.step_id,
                    call_index=0 if step.tool_name else None,
                    action_family=action_family,
                    status_owning_program=prog,
                    argument_skeleton=skeleton,
                    exit_code=step.exit_code,
                    exit_semantics=exit_sem,
                    is_error=is_true_err,
                    payload_digest=p_digest,
                    payload_bytes=p_bytes,
                    source_citation=citation,
                    summary=summary,
                )
                events.append(event)
                event_ord_counter += 1

        journal_dir = trial_dir / "state-journal"
        state_events_candidates = (
            journal_dir / "state-events.jsonl",
            trial_dir / "state-events.jsonl",
        )
        state_events_path = next((p for p in state_events_candidates if p.is_file()), None)
        if state_events_path is not None:
            try:
                state_rel_path = state_events_path.relative_to(trial_dir).as_posix()
                state_sha = sha256_file(state_events_path)
                if journal_dir.is_dir() and (journal_dir / "status.json").is_file() and (journal_dir / "state-diff.json").is_file():
                    from evallab.results import TrialRecord
                    from evallab.state_events import load_state_event_facts
                    trial_rec = TrialRecord(
                        path=trial_dir,
                        result={"id": outline.trial_id, "trial_name": outline.trial_name},
                        config=config_dict,
                        lock=lock_dict,
                        rewards={},
                        artifacts=(),
                    )
                    state_facts = load_state_event_facts(
                        trial_rec,
                        job_id=outline.job_id or "job",
                        experiment_id=None,
                    )
                    for fact in state_facts:
                        if fact.evidence_status != "valid":
                            continue
                        se_event_id = hashlib.sha256(
                            f"{outline.trial_id}:{event_ord_counter}:state_change:{fact.sequence}:{fact.path}".encode()
                        ).hexdigest()
                        se_citation = create_citation_handle(
                            trial_id=outline.trial_id,
                            source_path=state_rel_path,
                            source_sha256=state_sha,
                            raw_cas_uri=cas_uri,
                            target_type="file",
                            ir_event_id=se_event_id,
                            redaction_profile_digest=policy.compute_digest(),
                        )
                        events.append(
                            IREvent(
                                event_id=se_event_id,
                                event_ordinal=event_ord_counter,
                                event_type="state_change",
                                actor="environment",
                                timestamp=fact.event_at,
                                phase="work",
                                episode_id=1,
                                step_index=fact.sequence,
                                call_index=None,
                                action_family="other",
                                status_owning_program="inotify",
                                argument_skeleton=fact.path or "file",
                                exit_code=0,
                                exit_semantics="success",
                                is_error=False,
                                payload_digest=fact.source_record_digest or f"sha256:{hashlib.sha256(str(fact.path).encode()).hexdigest()}",
                                payload_bytes=fact.after_size_bytes or 0,
                                source_citation=se_citation,
                                summary=f"State event: {','.join(fact.operations)} {fact.path}",
                                state_before_digest=fact.before_state_digest,
                                state_after_digest=fact.after_state_digest,
                            )
                        )
                        event_ord_counter += 1
                else:
                    with state_events_path.open("r", encoding="utf-8", errors="replace") as f:
                        for line_idx, line in enumerate(f):
                            line_str = line.strip()
                            if not line_str:
                                continue
                            try:
                                se_data = json.loads(line_str)
                            except Exception:
                                continue
                            if not isinstance(se_data, dict):
                                continue
                            se_path = str(se_data.get("path") or "file")
                            se_ops = se_data.get("operations") or ["modify"]
                            se_event_id = hashlib.sha256(
                                f"{outline.trial_id}:{event_ord_counter}:state_change:{line_idx}:{se_path}".encode()
                            ).hexdigest()
                            se_citation = create_citation_handle(
                                trial_id=outline.trial_id,
                                source_path=state_rel_path,
                                source_sha256=state_sha,
                                raw_cas_uri=cas_uri,
                                target_type="file",
                                ir_event_id=se_event_id,
                                redaction_profile_digest=policy.compute_digest(),
                            )
                            events.append(
                                IREvent(
                                    event_id=se_event_id,
                                    event_ordinal=event_ord_counter,
                                    event_type="state_change",
                                    actor="environment",
                                    timestamp=se_data.get("timestamp") or se_data.get("event_at"),
                                    phase="work",
                                    episode_id=1,
                                    step_index=se_data.get("sequence") or (outline.total_steps or 1),
                                    call_index=None,
                                    action_family="other",
                                    status_owning_program="inotify",
                                    argument_skeleton=se_path,
                                    exit_code=0,
                                    exit_semantics="success",
                                    is_error=False,
                                    payload_digest=f"sha256:{hashlib.sha256(line_str.encode('utf-8')).hexdigest()}",
                                    payload_bytes=len(line_str.encode("utf-8")),
                                    source_citation=se_citation,
                                    summary=f"State event: {','.join(se_ops)} {se_path}",
                                    state_before_digest=se_data.get("before_state_digest"),
                                    state_after_digest=se_data.get("after_state_digest"),
                                )
                            )
                            event_ord_counter += 1
            except Exception:
                pass
        episodes = _segment_episodes(events)

        updated_events: list[IREvent] = []
        for ev in events:
            assigned_ep_id = 1
            for ep in episodes:
                if ep.start_ordinal <= ev.event_ordinal <= ep.end_ordinal:
                    assigned_ep_id = ep.episode_id
                    break
            if assigned_ep_id != ev.episode_id:
                updated_events.append(
                    IREvent(
                        event_id=ev.event_id,
                        event_ordinal=ev.event_ordinal,
                        event_type=ev.event_type,
                        actor=ev.actor,
                        timestamp=ev.timestamp,
                        phase=ev.phase,
                        episode_id=assigned_ep_id,
                        step_index=ev.step_index,
                        call_index=ev.call_index,
                        action_family=ev.action_family,
                        status_owning_program=ev.status_owning_program,
                        argument_skeleton=ev.argument_skeleton,
                        exit_code=ev.exit_code,
                        exit_semantics=ev.exit_semantics,
                        is_error=ev.is_error,
                        payload_digest=ev.payload_digest,
                        payload_bytes=ev.payload_bytes,
                        source_citation=ev.source_citation,
                        summary=ev.summary,
                        tool_schema_digest=ev.tool_schema_digest,
                        matched_result_digest=ev.matched_result_digest,
                        state_before_digest=ev.state_before_digest,
                        state_after_digest=ev.state_after_digest,
                    )
                )
            else:
                updated_events.append(ev)
        observed_call_ids = {
            event.source_citation.source_call_id
            for event in updated_events
            if event.event_type == "observation" and event.source_citation.source_call_id
        }
        raw_unpaired_count = sum(
            1
            for event in updated_events
            if event.event_type == "tool_call"
            and (
                event.source_citation.tool_call_id is None
                or event.source_citation.tool_call_id not in observed_call_ids
            )
        )
        unpaired_count = raw_unpaired_count
        linkage_coverage = "degraded" if raw_unpaired_count > 0 else "complete"
        raw_ledger_pairing_disagreement = (
            ledger_unpaired_count != raw_unpaired_count
        )

        source_digests: dict[str, str] = {
            "source_sha256": outline.source_sha256,
            "result_sha256": result_sha or "",
            "redaction_profile_digest": policy.compute_digest(),
        }
        if cas_uri:
            source_digests["cas_uri"] = cas_uri
        if task_digest:
            source_digests["task_digest"] = task_digest
        if verifier_digest:
            source_digests["verifier_digest"] = verifier_digest
        if manifest_digest:
            source_digests["manifest_digest"] = manifest_digest

        opportunity_windows: list[IROpportunityWindow] = []
        prior_error = False
        for ev in updated_events:
            opp_type = None
            if ev.is_error:
                opp_type = "error_recovery_candidate"
                prior_error = True
            elif prior_error and ev.exit_semantics == "success":
                opp_type = "error_recovery_candidate"
                prior_error = False
            elif ev.action_family == "verification" or ev.event_type == "verifier_check":
                opp_type = "verification_candidate"
            elif ev.action_family in ("file_edit", "file_write"):
                opp_type = "state_mutation_candidate"
            elif ev.event_type == "tool_call":
                opp_type = "tool_selection_candidate"
            elif ev.event_type == "context_management":
                opp_type = "context_compaction_candidate"

            if opp_type:
                opp_id = hashlib.sha256(f"{outline.trial_id}:{ev.step_index}:{opp_type}:{ev.event_ordinal}".encode()).hexdigest()
                opportunity_windows.append(
                    IROpportunityWindow(
                        opportunity_id=opp_id,
                        opportunity_type=opp_type,
                        step_index=ev.step_index,
                        action_family=ev.action_family,
                        status_owning_program=ev.status_owning_program,
                        has_prior_error=prior_error,
                        has_subsequent_recovery=(
                            not ev.is_error and ev.exit_semantics == "success"
                        ),
                        state_before_digest=ev.state_before_digest,
                        state_after_digest=ev.state_after_digest,
                        reopening_citation=ev.source_citation,
                        description=f"{opp_type} at step {ev.step_index} ({ev.status_owning_program or ev.action_family}): {ev.summary}",
                        is_screening_only=True,
                        evidence_basis="screening_heuristic",
                    )
                )

        unknowns_list: list[dict[str, str]] = []
        if not task_digest:
            unknowns_list.append({"field": "task_digest", "reason": "not_recorded_in_trial_evidence"})
        if not verifier_digest:
            unknowns_list.append({"field": "verifier_digest", "reason": "not_recorded_in_trial_evidence"})
        unknowns_list.append({"field": "tool_schema_digest", "reason": "unset_in_raw_atif_steps"})
        unknowns_list.append({"field": "matched_result_digest", "reason": "unset_in_raw_atif_steps"})
        unknowns_list.append({"field": "state_before_after_digests", "reason": "unobserved_without_state_journal"})
        if raw_ledger_pairing_disagreement:
            unknowns_list.append(
                {
                    "field": "tool_call_pairing",
                    "reason": (
                        "raw_atif_pairing_disagrees_with_quality_ledger:"
                        f"raw_unpaired={raw_unpaired_count},"
                        f"ledger_unpaired={ledger_unpaired_count}"
                    ),
                }
            )

        from evallab.evidence_pack import compute_evidence_coverage_metrics

        coverage_metrics = compute_evidence_coverage_metrics(
            events=updated_events,
            episodes=episodes,
            status=outline.status,
            unavailable_reason=outline.unavailable_reason,
            quality_status=quality_status,
            unpaired_count=unpaired_count,
            linkage_coverage=linkage_coverage,
            is_production_cas=is_cas,
            cas_uri=cas_uri,
            result_sha=result_sha,
            primary_reward=primary_reward,
            exception_class=exception_class,
            final_verdict=final_verdict,
            recovery_count=baseline.recovery_count,
            max_cascade=baseline.max_exit_code_cascade_screening,
            trial_dir=trial_dir,
        )
        evidence_coverage = coverage_metrics.to_dict()
        ir_trial_id = str(inventory_record.get("trial_id") or outline.trial_id)
        ir_trial_name = str(inventory_record.get("trial_name") or outline.trial_name)
        ir_job_id = str(inventory_record.get("job_id") or outline.job_id)
        ir_job_name = str(inventory_record.get("job_name") or outline.job_name)
        ir_task_name = str(inventory_record.get("task_name") or outline.task_name)
        baseline = replace(
            baseline,
            trial_id=ir_trial_id,
            trial_name=ir_trial_name,
            job_id=ir_job_id,
            job_name=ir_job_name,
            task_name=ir_task_name,
            source_path=rel_source_path,
        )

        raw_ir_dict = {
            "ir_version": "1.0",
            "trial_id": ir_trial_id,
            "job_id": ir_job_id,
            "trial_name": ir_trial_name,
            "job_name": ir_job_name,
            "task_name": ir_task_name,
            "task_digest": task_digest,
            "verifier_digest": verifier_digest,
            "agent_scaffold": agent_scaffold,
            "agent_version": outline.agent_version,
            "model_name": model_name,
            "status": outline.status,
            "unavailable_reason": outline.unavailable_reason,
            "final_verdict": final_verdict,
            "primary_reward": primary_reward,
            "exception_class": exception_class,
            "exception_message": exception_message,
            "duration_seconds": outline.duration_seconds,
            "total_tokens": baseline.total_tokens,
            "cost_usd": baseline.cost_usd,
            "quality_status": quality_status,
            "quality_findings": quality_findings,
            "unpaired_tool_calls_count": unpaired_count,
            "linkage_coverage": linkage_coverage,
            "is_production_cas": is_cas,
            "events": [e.to_dict() for e in updated_events],
            "episodes": [ep.to_dict() for ep in episodes],
            "opportunity_windows": [op.to_dict() for op in opportunity_windows],
            "unknowns": unknowns_list,
            "baseline_metrics": baseline.to_dict(),
            "evidence_coverage": evidence_coverage,
            "source_digests": source_digests,
            "created_at": baseline.created_at,
        }

        ir_digest = _sha256_canonical_json(raw_ir_dict)

        return TrajectoryIR(
            ir_version="1.0",
            ir_digest=ir_digest,
            trial_id=ir_trial_id,
            job_id=ir_job_id,
            trial_name=ir_trial_name,
            job_name=ir_job_name,
            task_name=ir_task_name,
            task_digest=task_digest,
            verifier_digest=verifier_digest,
            agent_scaffold=agent_scaffold,
            agent_version=outline.agent_version,
            model_name=model_name,
            status=outline.status,
            unavailable_reason=outline.unavailable_reason,
            final_verdict=final_verdict,
            primary_reward=primary_reward,
            exception_class=exception_class,
            exception_message=exception_message,
            duration_seconds=outline.duration_seconds,
            total_tokens=baseline.total_tokens,
            cost_usd=baseline.cost_usd,
            quality_status=quality_status,
            quality_findings=tuple(quality_findings),
            unpaired_tool_calls_count=unpaired_count,
            linkage_coverage=linkage_coverage,
            is_production_cas=is_cas,
            events=tuple(updated_events),
            episodes=tuple(episodes),
            opportunity_windows=tuple(opportunity_windows),
            unknowns=tuple(unknowns_list),
            baseline_metrics=baseline,
            evidence_coverage=evidence_coverage,
            source_digests=source_digests,
            created_at=baseline.created_at,
        )
    finally:
        if temp_extract_dir is not None:
            with contextlib.suppress(Exception):
                temp_extract_dir.cleanup()
