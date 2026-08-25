"""Typed, versioned trajectory semantics profiles and semantic action projections.

Extracts semantic action facts from ATIF trajectories and correlated observation
streams using explicit, versioned TrajectorySemanticsProfiles. Resolves tool roles,
outcomes (including expected-negative results vs real failures), and intervention
provenance (autonomous vs user-assisted recovery).

Guarantees:
- Strict mode raises UnmappedActionError on unknown tools/actions.
- Permissive mode emits unknown_semantics with an explicit reason.
- Raw argument content and observation bodies are never persisted; only digests are retained.
- Atomic, deterministic Parquet projections sorted by primary key for byte stability.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator

from evallab.schemas import ContractModel

Digest = str
ToolRole = Literal[
    "execute",
    "read",
    "write",
    "search",
    "inspect",
    "think",
    "communicate",
    "terminate",
    "other",
]
ActionOutcome = Literal[
    "success",
    "error",
    "expected_negative",
    "neutral",
    "unknown_semantics",
]
InterventionProvenance = Literal[
    "autonomous",
    "user_assisted",
    "environment_recovery",
    "harness_retry",
    "unintervened",
]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256_digest(value: Any) -> str:
    """Compute deterministic sha256:<64-hex> digest."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class UnmappedActionError(ValueError):
    """Raised in strict mode when an action or tool cannot be resolved by the profile."""


class SemanticActionFact(ContractModel):
    """Semantic action observation fact projected without raw secrets/payloads."""

    trial_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    step_id: int | None = None
    action_id: str = Field(min_length=1)
    tool_call_id: str | None = None
    tool_name: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    arguments_sha256: Digest
    observation_sha256: Digest
    source_sha256: Digest
    role: ToolRole
    outcome: ActionOutcome
    outcome_detail: str | None = None
    intervention_provenance: InterventionProvenance
    intervention_detail: str | None = None
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_digest: Digest
    source_ref: str = Field(min_length=1)

    @field_validator("arguments_sha256", "observation_sha256", "source_sha256", "profile_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("digest must match sha256:<64 lowercase hex digits>")
        return value


@dataclass(frozen=True)
class ToolMappingRule:
    """Explicit mapping rule for a specific tool or tool family."""

    tool_pattern: str
    role: ToolRole
    outcome_resolver: Callable[[Mapping[str, Any], Mapping[str, Any] | None], tuple[ActionOutcome, str | None]] | None = None
    command_prefix: str | None = None


def default_outcome_resolver(
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> tuple[ActionOutcome, str | None]:
    """Default outcome resolution based on exit code or observation error keys."""
    if observation is None:
        return "neutral", "no_observation"
    if "exit_code" in observation:
        code = observation.get("exit_code")
        if code == 0:
            return "success", None
        if code is not None:
            return "error", f"exit_code_{code}"
    if observation.get("error") or observation.get("is_error"):
        return "error", str(observation.get("error") or "error_flag_set")
    if observation.get("status") in ("failed", "error"):
        return "error", str(observation.get("status"))
    return "success", None


def bash_command_outcome_resolver(
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> tuple[ActionOutcome, str | None]:
    """Outcome resolution distinguishing expected-negative grep/diff from real errors."""
    if observation is None:
        return "neutral", "no_observation"

    cmd = str(action_args.get("command") or action_args.get("cmd") or "").strip()
    code = observation.get("exit_code")
    content = str(observation.get("content") or observation.get("output") or observation.get("stdout") or "")

    # Grep semantic contract: exit code 1 is 'not found' (expected negative search result), exit code >= 2 is syntax/file error
    if cmd.startswith("grep ") or cmd.startswith("rg ") or " grep " in cmd:
        if code == 0:
            return "success", "match_found"
        if code == 1:
            return "expected_negative", "pattern_not_found"
        if code is not None and code >= 2:
            return "error", f"grep_error_exit_code_{code}"

    # Diff semantic contract: exit code 0 is identical, exit code 1 is differences found (expected negative identity)
    if cmd.startswith("diff ") or cmd.startswith("cmp "):
        if code == 0:
            return "success", "identical"
        if code == 1:
            return "expected_negative", "differences_found"
        if code is not None and code >= 2:
            return "error", f"diff_error_exit_code_{code}"

    # Generic command exit code
    if code == 0:
        return "success", None
    if code is not None:
        # Check command not found or permission denied
        if "command not found" in content or "Permission denied" in content:
            return "error", f"system_error_exit_code_{code}"
        return "error", f"exit_code_{code}"

    if observation.get("error") or observation.get("is_error"):
        return "error", str(observation.get("error") or "error_flag_set")
    return "success", None


@dataclass(frozen=True)
class TrajectorySemanticsProfile:
    """Versioned specification of tool semantic roles, outcome rules, and interventions."""

    profile_id: str
    version: str
    description: str
    tool_rules: tuple[ToolMappingRule, ...] = ()
    custom_resolvers: Mapping[str, Callable[..., tuple[ActionOutcome, str | None]]] = field(
        default_factory=dict
    )

    @property
    def digest(self) -> str:
        """Deterministic sha256 digest of the profile configuration."""
        raw = {
            "profile_id": self.profile_id,
            "version": self.version,
            "description": self.description,
            "rules": [
                {
                    "tool_pattern": r.tool_pattern,
                    "role": r.role,
                    "command_prefix": r.command_prefix,
                }
                for r in self.tool_rules
            ],
            "custom_keys": sorted(self.custom_resolvers.keys()),
        }
        return _sha256_digest(raw)

    def resolve_action(
        self,
        tool_name: str,
        action_args: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
        *,
        strict: bool = True,
    ) -> tuple[ToolRole, ActionOutcome, str | None]:
        """Resolve the semantic role and outcome for an action."""
        # 1. Match explicit tool mapping rules
        cmd = str(action_args.get("command") or action_args.get("cmd") or "").strip()
        for rule in self.tool_rules:
            if rule.tool_pattern == tool_name or rule.tool_pattern == "*":
                if rule.command_prefix and not cmd.startswith(rule.command_prefix):
                    continue
                resolver = rule.outcome_resolver or default_outcome_resolver
                outcome, detail = resolver(action_args, observation)
                return rule.role, outcome, detail

        # 2. Match custom resolvers by tool name
        if tool_name in self.custom_resolvers:
            resolver = self.custom_resolvers[tool_name]
            outcome, detail = resolver(action_args, observation)
            return "other", outcome, detail

        # 3. Unmapped fallback
        if strict:
            raise UnmappedActionError(
                f"Profile '{self.profile_id}@{self.version}' has no mapping rule for tool '{tool_name}' (command='{cmd[:40]}')"
            )
        return "other", "unknown_semantics", f"unmapped_tool:{tool_name}"


# Standard default profiles
GENERIC_POSIX_PROFILE = TrajectorySemanticsProfile(
    profile_id="posix-generic",
    version="1.0.0",
    description="Standard POSIX tool semantics covering bash commands and common structured tools",
    tool_rules=(
        ToolMappingRule("bash", "search", bash_command_outcome_resolver, command_prefix="grep"),
        ToolMappingRule("bash", "search", bash_command_outcome_resolver, command_prefix="rg"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="diff"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="cat"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="ls"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="head"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="tail"),
        ToolMappingRule("bash", "write", bash_command_outcome_resolver, command_prefix="sed"),
        ToolMappingRule("bash", "write", bash_command_outcome_resolver, command_prefix="touch"),
        ToolMappingRule("bash", "write", bash_command_outcome_resolver, command_prefix="echo"),
        ToolMappingRule("bash", "execute", bash_command_outcome_resolver),
        ToolMappingRule("read", "read", default_outcome_resolver),
        ToolMappingRule("write", "write", default_outcome_resolver),
        ToolMappingRule("edit", "write", default_outcome_resolver),
        ToolMappingRule("grep", "search", default_outcome_resolver),
        ToolMappingRule("glob", "search", default_outcome_resolver),
        ToolMappingRule("think", "think", default_outcome_resolver),
        ToolMappingRule("finish", "terminate", default_outcome_resolver),
        ToolMappingRule("submit", "terminate", default_outcome_resolver),
        ToolMappingRule("terminate", "terminate", default_outcome_resolver),
    ),
)
LOCA_PROFILE = TrajectorySemanticsProfile(
    profile_id="loca-bench-v1",
    version="1.0.0",
    description="Explicit profile for LOCA long-context benchmark operations and runtime tools",
    tool_rules=(
        ToolMappingRule("read_file", "read", default_outcome_resolver),
        ToolMappingRule("view_file", "read", default_outcome_resolver),
        ToolMappingRule("search_files", "search", default_outcome_resolver),
        ToolMappingRule("grep_files", "search", default_outcome_resolver),
        ToolMappingRule("retrieve_context", "read", default_outcome_resolver),
        ToolMappingRule("compact_memory", "execute", default_outcome_resolver),
        ToolMappingRule("evict_memory", "execute", default_outcome_resolver),
        ToolMappingRule("execute_query", "execute", default_outcome_resolver),
        ToolMappingRule("db_query", "execute", default_outcome_resolver),
        ToolMappingRule("bash", "search", bash_command_outcome_resolver, command_prefix="grep"),
        ToolMappingRule("bash", "search", bash_command_outcome_resolver, command_prefix="find"),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="cat"),
        ToolMappingRule("bash", "execute", bash_command_outcome_resolver),
        ToolMappingRule("submit", "terminate", default_outcome_resolver),
        ToolMappingRule("answer", "terminate", default_outcome_resolver),
        ToolMappingRule("finish", "terminate", default_outcome_resolver),
    ),
)

AGENTABSTAIN_PROFILE = TrajectorySemanticsProfile(
    profile_id="agentabstain-v1",
    version="1.0.0",
    description="Explicit profile for AgentAbstain ambiguous action and safe tool execution",
    tool_rules=(
        ToolMappingRule("spotify.get_user_playlists", "read", default_outcome_resolver),
        ToolMappingRule("spotify.get_playlist_tracks", "read", default_outcome_resolver),
        ToolMappingRule("spotify.search_tracks", "search", default_outcome_resolver),
        ToolMappingRule("spotify.write_gmail_draft", "write", default_outcome_resolver),
        ToolMappingRule("gmail_and_email_records.manage_gmail_draft", "write", default_outcome_resolver),
        ToolMappingRule("gmail.list_drafts", "read", default_outcome_resolver),
        ToolMappingRule("gmail.send_message", "communicate", default_outcome_resolver),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="cat"),
        ToolMappingRule("bash", "search", bash_command_outcome_resolver, command_prefix="grep"),
        ToolMappingRule("bash", "execute", bash_command_outcome_resolver),
        ToolMappingRule("read", "read", default_outcome_resolver),
        ToolMappingRule("write", "write", default_outcome_resolver),
        ToolMappingRule("abstain", "terminate", default_outcome_resolver),
        ToolMappingRule("finish", "terminate", default_outcome_resolver),
        ToolMappingRule("submit", "terminate", default_outcome_resolver),
    ),
)

DEEPPLANNING_PROFILE = TrajectorySemanticsProfile(
    profile_id="deepplanning-v1",
    version="1.0.0",
    description="Explicit profile for DeepPlanning multi-step shopping and travel workflows",
    tool_rules=(
        ToolMappingRule("search_items", "search", default_outcome_resolver),
        ToolMappingRule("search_flights", "search", default_outcome_resolver),
        ToolMappingRule("search_hotels", "search", default_outcome_resolver),
        ToolMappingRule("filter_items", "inspect", default_outcome_resolver),
        ToolMappingRule("view_item_details", "inspect", default_outcome_resolver),
        ToolMappingRule("view_details", "inspect", default_outcome_resolver),
        ToolMappingRule("calculate_budget", "inspect", default_outcome_resolver),
        ToolMappingRule("check_constraints", "inspect", default_outcome_resolver),
        ToolMappingRule("add_to_cart", "execute", default_outcome_resolver),
        ToolMappingRule("book_travel", "execute", default_outcome_resolver),
        ToolMappingRule("reserve_hotel", "execute", default_outcome_resolver),
        ToolMappingRule("bash", "inspect", bash_command_outcome_resolver, command_prefix="cat"),
        ToolMappingRule("bash", "execute", bash_command_outcome_resolver),
        ToolMappingRule("submit_plan", "terminate", default_outcome_resolver),
        ToolMappingRule("submit", "terminate", default_outcome_resolver),
        ToolMappingRule("finish", "terminate", default_outcome_resolver),
    ),
)

BENCHMARK_PROFILES: dict[str, TrajectorySemanticsProfile] = {
    "posix": GENERIC_POSIX_PROFILE,
    "posix-generic": GENERIC_POSIX_PROFILE,
    "loca": LOCA_PROFILE,
    "loca-bench-v1": LOCA_PROFILE,
    "agentabstain": AGENTABSTAIN_PROFILE,
    "agentabstain-v1": AGENTABSTAIN_PROFILE,
    "deepplanning": DEEPPLANNING_PROFILE,
    "deepplanning-v1": DEEPPLANNING_PROFILE,
}


def get_profile(name_or_id: str) -> TrajectorySemanticsProfile:
    """Resolve a semantics profile by alias or ID."""
    key = name_or_id.lower().strip()
    if key in BENCHMARK_PROFILES:
        return BENCHMARK_PROFILES[key]
    raise KeyError(f"Unknown semantics profile: {name_or_id}. Known profiles: {sorted(BENCHMARK_PROFILES.keys())}")


SEMANTIC_ACTION_FACT_SCHEMA = pa.schema([
    pa.field("trial_id", pa.string(), nullable=False),
    pa.field("document_id", pa.string(), nullable=False),
    pa.field("step_id", pa.int64(), nullable=True),
    pa.field("action_id", pa.string(), nullable=False),
    pa.field("tool_call_id", pa.string(), nullable=True),
    pa.field("tool_name", pa.string(), nullable=False),
    pa.field("sequence", pa.int64(), nullable=False),
    pa.field("arguments_sha256", pa.string(), nullable=False),
    pa.field("observation_sha256", pa.string(), nullable=False),
    pa.field("source_sha256", pa.string(), nullable=False),
    pa.field("role", pa.string(), nullable=False),
    pa.field("outcome", pa.string(), nullable=False),
    pa.field("outcome_detail", pa.string(), nullable=True),
    pa.field("intervention_provenance", pa.string(), nullable=False),
    pa.field("intervention_detail", pa.string(), nullable=True),
    pa.field("profile_id", pa.string(), nullable=False),
    pa.field("profile_version", pa.string(), nullable=False),
    pa.field("profile_digest", pa.string(), nullable=False),
    pa.field("source_ref", pa.string(), nullable=False),
])

TRAJECTORY_SEMANTIC_SCHEMAS: dict[str, pa.Schema] = {
    "semantic_action_facts": SEMANTIC_ACTION_FACT_SCHEMA,
}


def extract_semantic_actions(
    atif_data: Mapping[str, Any],
    profile: TrajectorySemanticsProfile,
    *,
    strict: bool = True,
) -> list[SemanticActionFact]:
    """Extract semantic action facts from an in-memory ATIF trajectory structure."""
    trial_id = str(
        atif_data.get("trial_id")
        or atif_data.get("session_id")
        or atif_data.get("id")
        or "unknown_trial"
    )
    doc_id = str(atif_data.get("document_id") or atif_data.get("source_digest") or _sha256_digest(atif_data))
    source_sha = _sha256_digest(atif_data)
    profile_digest = profile.digest

    steps = atif_data.get("steps") or []
    facts: list[SemanticActionFact] = []
    current_sequence = 0
    had_user_intervention_since_last_action = False
    last_user_message_snippet: str | None = None
    had_environment_error_since_last_action = False

    for s_idx, step in enumerate(steps):
        step_source = str(step.get("source") or step.get("role") or "").lower()
        step_id = step.get("step_id")
        if step_id is not None:
            with suppress(ValueError, TypeError):
                step_id = int(step_id)

        # Track intervening user / environment messages
        if step_source in ("user", "human"):
            had_user_intervention_since_last_action = True
            msg = str(step.get("message") or step.get("content") or "")
            last_user_message_snippet = msg[:100] if msg else "user_message"
            continue
        if step_source in ("system", "environment", "harness"):
            err = str(step.get("error") or step.get("message") or "")
            if "error" in err.lower() or "exception" in err.lower() or "retry" in err.lower():
                had_environment_error_since_last_action = True
            continue

        # Extract agent actions and tool calls
        tool_calls = step.get("tool_calls") or []
        # If no explicit tool_calls list, check if the step itself is an action
        if not tool_calls and (step.get("tool_name") or step.get("function_name") or step.get("tool")):
            tool_calls = [step]

        # Correlated observations
        # Correlated observations (support step.observations, step.observation.results, or single step.observation)
        observations_raw = step.get("observations")
        if observations_raw is None:
            obs_obj = step.get("observation")
            if isinstance(obs_obj, dict):
                results = obs_obj.get("results")
                observations_raw = results if isinstance(results, list) else [obs_obj]
            elif isinstance(obs_obj, list):
                observations_raw = obs_obj
            else:
                observations_raw = []

        observations = observations_raw if isinstance(observations_raw, list) else []
        obs_map: dict[str, dict[str, Any]] = {}
        for o_idx, obs in enumerate(observations):
            if isinstance(obs, dict):
                call_id = obs.get("source_call_id") or obs.get("tool_call_id") or obs.get("call_id") or str(o_idx)
                obs_map[str(call_id)] = obs
        for c_idx, tc in enumerate(tool_calls):
            tool_name = str(tc.get("tool_name") or tc.get("function_name") or tc.get("tool") or tc.get("name") or "unknown")
            action_id = str(tc.get("action_id") or tc.get("event_id") or tc.get("tool_call_id") or f"act_{s_idx}_{c_idx}")
            tool_call_id = tc.get("tool_call_id") or tc.get("call_id")

            # Extract arguments mapping
            raw_args = tc.get("arguments") or tc.get("args") or tc.get("parameters") or {}
            if isinstance(raw_args, str):
                try:
                    args_map = json.loads(raw_args)
                except Exception:
                    args_map = {"raw_text": raw_args}
            elif isinstance(raw_args, dict):
                args_map = raw_args
            else:
                args_map = {"value": raw_args}

            args_digest = _sha256_digest(args_map)

            # Correlate observation
            matched_obs: dict[str, Any] | None = None
            if tool_call_id and str(tool_call_id) in obs_map:
                matched_obs = obs_map[str(tool_call_id)]
            elif str(c_idx) in obs_map:
                matched_obs = obs_map[str(c_idx)]
            elif observations:
                matched_obs = observations[min(c_idx, len(observations) - 1)]

            obs_digest = _sha256_digest(matched_obs) if matched_obs is not None else "sha256:" + "0" * 64

            # Resolve semantic role and outcome
            role, outcome, outcome_detail = profile.resolve_action(
                tool_name,
                args_map,
                matched_obs,
                strict=strict,
            )

            # Resolve intervention provenance
            if had_user_intervention_since_last_action:
                intervention_prov: InterventionProvenance = "user_assisted"
                intervention_detail = last_user_message_snippet
            elif had_environment_error_since_last_action:
                intervention_prov = "environment_recovery"
                intervention_detail = "post_environment_error"
            else:
                intervention_prov = "autonomous"
                intervention_detail = None



            fact = SemanticActionFact(
                trial_id=trial_id,
                document_id=doc_id,
                step_id=step_id,
                action_id=action_id,
                tool_call_id=str(tool_call_id) if tool_call_id is not None else None,
                tool_name=tool_name,
                sequence=current_sequence,
                arguments_sha256=args_digest,
                observation_sha256=obs_digest,
                source_sha256=source_sha,
                role=role,
                outcome=outcome,
                outcome_detail=outcome_detail,
                intervention_provenance=intervention_prov,
                intervention_detail=intervention_detail,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                profile_digest=profile_digest,
                source_ref=f"{trial_id}#step={step_id or s_idx}#action={action_id}",
            )
            facts.append(fact)
            current_sequence += 1
        # Reset intervention tracking after consuming it across this step's actions
        if tool_calls:
            had_user_intervention_since_last_action = False
            last_user_message_snippet = None
            had_environment_error_since_last_action = False

    return facts


@contextmanager
def _table_lock(target_file: Path, exclusive: bool = True):
    """File lock ensuring atomic concurrency control during Parquet writes."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = target_file.with_suffix(f"{target_file.suffix}.lock")
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, flags)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_parquet_atomic(target_path: Path, table: pa.Table) -> None:
    """Atomic replacement of Parquet file using fsync and temporary write-rename."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target_path.parent,
        prefix=f".{target_path.stem}_tmp_",
        suffix=".parquet",
        delete=False,
    ) as tmp:
        tmp_name = tmp.name

    try:
        pq.write_table(table, tmp_name, compression="zstd")
        with open(tmp_name, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_name, target_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def project_semantic_actions_parquet(
    actions: Sequence[SemanticActionFact],
    output_path: Path | str,
) -> Path:
    """Deterministically project semantic action facts to an atomic Parquet table.

    Rows are sorted by (trial_id, sequence, action_id) to guarantee bit-for-bit
    reproducibility across runs regardless of input collection ordering.
    """
    target = Path(output_path)
    sorted_actions = sorted(
        actions,
        key=lambda a: (a.trial_id, a.sequence, a.action_id),
    )

    rows = [
        {
            "trial_id": a.trial_id,
            "document_id": a.document_id,
            "step_id": a.step_id,
            "action_id": a.action_id,
            "tool_call_id": a.tool_call_id,
            "tool_name": a.tool_name,
            "sequence": a.sequence,
            "arguments_sha256": a.arguments_sha256,
            "observation_sha256": a.observation_sha256,
            "source_sha256": a.source_sha256,
            "role": a.role,
            "outcome": a.outcome,
            "outcome_detail": a.outcome_detail,
            "intervention_provenance": a.intervention_provenance,
            "intervention_detail": a.intervention_detail,
            "profile_id": a.profile_id,
            "profile_version": a.profile_version,
            "profile_digest": a.profile_digest,
            "source_ref": a.source_ref,
        }
        for a in sorted_actions
    ]

    table = pa.Table.from_pylist(rows, schema=SEMANTIC_ACTION_FACT_SCHEMA)
    with _table_lock(target, exclusive=True):
        _write_parquet_atomic(target, table)

    return target
