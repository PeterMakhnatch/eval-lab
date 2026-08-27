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
import shlex
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator, model_validator

from evallab.evidence.atif import TrajectoryFact, project_trial
from evallab.results import JobRecord, TrialRecord
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
    "system_assisted",
    "environment_recovery",
    "harness_retry",
]
ObservationCorrelation = Literal[
    "matched_call_id",
    "singleton_unkeyed",
    "no_observation",
    "unknown_unmatched",
]
ObservationCorrelationReason = Literal[
    "single_call_single_observation",
    "tool_call_id_not_found",
    "ambiguous_unkeyed_observations",
    "observation_absent",
]
InterventionReason = Literal[
    "post_action_user_message",
    "explicit_harness_retry",
    "explicit_environment_error",
    "post_action_system_message",
]


class SemanticReasonCode(StrEnum):
    """Closed, storage-safe reason codes for semantic outcomes."""

    NO_OBSERVATION = "no_observation"
    MATCH_FOUND = "match_found"
    PATTERN_NOT_FOUND = "pattern_not_found"
    IDENTICAL = "identical"
    DIFFERENCES_FOUND = "differences_found"
    GREP_ERROR_EXIT_CODE = "grep_error_exit_code"
    DIFF_ERROR_EXIT_CODE = "diff_error_exit_code"
    SYSTEM_ERROR_EXIT_CODE = "system_error_exit_code"
    EXIT_CODE_ERROR = "exit_code_error"
    OBSERVATION_ERROR_FLAG = "observation_error_flag"
    OBSERVATION_ERROR_STATUS = "observation_error_status"
    EXIT_CODE_MISSING = "exit_code_missing"
    SHELL_PARSE_ERROR = "shell_parse_error"
    SHELL_COMPOUND_AMBIGUOUS = "shell_compound_ambiguous"
    EMPTY_COMMAND = "empty_command"
    INCOMPLETE_PIPELINE = "incomplete_pipeline"
    MISSING_SHELL_PROGRAM = "missing_shell_program"
    STRUCTURED_SEARCH_RESULT_SHAPE_UNKNOWN = "structured_search_result_shape_unknown"
    UNMAPPED_TOOL = "unmapped_tool"
    OBSERVATION_CORRELATION_UNKNOWN = "observation_correlation_unknown"


def _canonical_bytes(value: Any) -> bytes:
    """Encode a value deterministically for hashing and size accounting."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256_digest(value: Any) -> str:
    """Compute deterministic sha256:<64-hex> digest."""
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


class UnmappedActionError(ValueError):
    """Raised in strict mode when an action or tool cannot be resolved by the profile."""


class SemanticActionFact(ContractModel):
    """Semantic action fact containing identities, digests, and reason codes only."""

    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    binding_digest: Digest
    document_id: str = Field(min_length=1)
    step_id: int | None = None
    action_id: str = Field(min_length=1)
    tool_call_id: str | None = None
    tool_name: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    arguments_sha256: Digest
    observation_sha256: Digest
    observation_correlation: ObservationCorrelation
    correlation_reason: ObservationCorrelationReason | None = None
    source_sha256: Digest
    role: ToolRole
    outcome: ActionOutcome
    reason_code: SemanticReasonCode | None = None
    detail_digest: Digest | None = None
    detail_size: int | None = Field(default=None, ge=0)
    intervention_provenance: InterventionProvenance
    intervention_sha256: Digest | None = None
    intervention_length: int | None = Field(default=None, ge=0)
    intervention_reason: InterventionReason | None = None
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_digest: Digest
    source_ref: str = Field(min_length=1)

    @field_validator(
        "binding_digest",
        "arguments_sha256",
        "observation_sha256",
        "source_sha256",
        "profile_digest",
        "intervention_sha256",
        "detail_digest",
    )
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("digest must match sha256:<64 lowercase hex digits>")
        return value

    @model_validator(mode="after")
    def _validate_detail_metadata(self) -> SemanticActionFact:
        if (self.detail_digest is None) != (self.detail_size is None):
            raise ValueError("detail_digest and detail_size must be present together")
        if self.detail_digest is not None and self.reason_code is None:
            raise ValueError("digested resolver detail requires a reason_code")
        return self


@dataclass(frozen=True)
class ResolverResult:
    """Resolver output; optional free-form detail is digested before persistence."""

    outcome: ActionOutcome
    reason_code: SemanticReasonCode | None = None
    detail: Any | None = field(default=None, repr=False, compare=False)


OutcomeResolver = Callable[
    [Mapping[str, Any], Mapping[str, Any] | None],
    ResolverResult,
]


@dataclass(frozen=True)
class ResolverRef:
    """Pinned resolver identity used by one profile rule."""

    resolver_id: str
    resolver_version: str


DEFAULT_RESOLVER_REF = ResolverRef("default", "2.0.0")
BASH_RESOLVER_REF = ResolverRef("bash-command", "2.1.0")
STRUCTURED_SEARCH_RESOLVER_REF = ResolverRef("structured-search", "2.0.0")


@dataclass(frozen=True)
class ToolMappingRule:
    """Explicit mapping rule for a specific tool or tool family."""

    tool_pattern: str
    role: ToolRole
    resolver: ResolverRef = DEFAULT_RESOLVER_REF
    command_prefix: str | None = None


def _observation_exit_code(observation: Mapping[str, Any]) -> Any:
    if "exit_code" in observation:
        return observation.get("exit_code")
    extra = observation.get("extra")
    return extra.get("exit_code") if isinstance(extra, Mapping) else None


def default_outcome_resolver(
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> ResolverResult:
    """Resolve tools that expose an exit code or an explicit error marker."""
    del action_args
    if observation is None:
        return ResolverResult("neutral", SemanticReasonCode.NO_OBSERVATION)
    code = _observation_exit_code(observation)
    if code == 0:
        return ResolverResult("success")
    if code is not None:
        return ResolverResult(
            "error",
            SemanticReasonCode.EXIT_CODE_ERROR,
            {"exit_code": code},
        )
    if observation.get("error") or observation.get("is_error"):
        return ResolverResult("error", SemanticReasonCode.OBSERVATION_ERROR_FLAG)
    if observation.get("status") in ("failed", "error"):
        return ResolverResult("error", SemanticReasonCode.OBSERVATION_ERROR_STATUS)
    return ResolverResult("success")


def _shell_program(
    command: str,
) -> tuple[str | None, SemanticReasonCode | None]:
    """Return the status-owning program or an explicit ambiguity reason."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None, SemanticReasonCode.SHELL_PARSE_ERROR
    if not tokens:
        return None, SemanticReasonCode.EMPTY_COMMAND
    for index, token in enumerate(tokens):
        redirected_ampersand = token == "&" and index > 0 and tokens[index - 1].endswith((">", "<"))
        if token in {"&&", "||", ";", ";;", ";&", ";;&", "&"} and not redirected_ampersand:
            return None, SemanticReasonCode.SHELL_COMPOUND_AMBIGUOUS
    pipe_tokens = {"|", "|&"}
    if any(token in pipe_tokens for token in tokens):
        last_pipe = max(index for index, token in enumerate(tokens) if token in pipe_tokens)
        if last_pipe + 1 >= len(tokens):
            return None, SemanticReasonCode.INCOMPLETE_PIPELINE
        tokens = tokens[last_pipe + 1 :]
    index = 0
    if tokens and tokens[0] == "env":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    while index < len(tokens):
        candidate = tokens[index]
        name, separator, _ = candidate.partition("=")
        if separator and name.replace("_", "").isalnum() and not name[0].isdigit():
            index += 1
            continue
        return Path(candidate).name, None
    return None, SemanticReasonCode.MISSING_SHELL_PROGRAM


def bash_command_outcome_resolver(
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> ResolverResult:
    """Resolve POSIX command outcomes using the program that owns the exit code."""
    if observation is None:
        return ResolverResult("neutral", SemanticReasonCode.NO_OBSERVATION)

    command = str(action_args.get("command") or action_args.get("cmd") or "").strip()
    program, ambiguity = _shell_program(command)
    code = _observation_exit_code(observation)
    content = str(
        observation.get("content") or observation.get("output") or observation.get("stdout") or ""
    )

    if ambiguity is not None:
        return ResolverResult("unknown_semantics", ambiguity)
    if program in {"grep", "rg"}:
        if code == 0:
            return ResolverResult("success", SemanticReasonCode.MATCH_FOUND)
        if code == 1:
            return ResolverResult(
                "expected_negative",
                SemanticReasonCode.PATTERN_NOT_FOUND,
            )
        if isinstance(code, int) and code >= 2:
            return ResolverResult(
                "error",
                SemanticReasonCode.GREP_ERROR_EXIT_CODE,
                {"exit_code": code},
            )
    if program in {"diff", "cmp"}:
        if code == 0:
            return ResolverResult("success", SemanticReasonCode.IDENTICAL)
        if code == 1:
            return ResolverResult(
                "expected_negative",
                SemanticReasonCode.DIFFERENCES_FOUND,
            )
        if isinstance(code, int) and code >= 2:
            return ResolverResult(
                "error",
                SemanticReasonCode.DIFF_ERROR_EXIT_CODE,
                {"exit_code": code},
            )
    if code == 0:
        return ResolverResult("success")
    if code is not None:
        reason_code = (
            SemanticReasonCode.SYSTEM_ERROR_EXIT_CODE
            if "command not found" in content or "Permission denied" in content
            else SemanticReasonCode.EXIT_CODE_ERROR
        )
        return ResolverResult("error", reason_code, {"exit_code": code})
    if observation.get("error") or observation.get("is_error"):
        return ResolverResult("error", SemanticReasonCode.OBSERVATION_ERROR_FLAG)
    return ResolverResult("unknown_semantics", SemanticReasonCode.EXIT_CODE_MISSING)


def structured_search_outcome_resolver(
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> ResolverResult:
    """Resolve explicitly declared structured searches by their result cardinality."""
    del action_args
    if observation is None:
        return ResolverResult("neutral", SemanticReasonCode.NO_OBSERVATION)
    if observation.get("error") or observation.get("is_error"):
        return ResolverResult("error", SemanticReasonCode.OBSERVATION_ERROR_FLAG)
    if observation.get("status") in ("failed", "error"):
        return ResolverResult("error", SemanticReasonCode.OBSERVATION_ERROR_STATUS)
    for key in ("match_count", "result_count", "total_count", "count"):
        value = observation.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return (
                ResolverResult("success", SemanticReasonCode.MATCH_FOUND)
                if value > 0
                else ResolverResult(
                    "expected_negative",
                    SemanticReasonCode.PATTERN_NOT_FOUND,
                )
            )
    for key in ("matches", "results", "files", "items"):
        value = observation.get(key)
        if isinstance(value, list):
            return (
                ResolverResult("success", SemanticReasonCode.MATCH_FOUND)
                if value
                else ResolverResult(
                    "expected_negative",
                    SemanticReasonCode.PATTERN_NOT_FOUND,
                )
            )
    return ResolverResult(
        "unknown_semantics",
        SemanticReasonCode.STRUCTURED_SEARCH_RESULT_SHAPE_UNKNOWN,
    )


_ACTION_OUTCOMES = frozenset(
    {"success", "error", "expected_negative", "neutral", "unknown_semantics"}
)


@dataclass(frozen=True)
class NormalizedResolverResult:
    """Storage-safe resolver output with any free-form detail irreversibly digested."""

    outcome: ActionOutcome
    reason_code: SemanticReasonCode | None
    detail_digest: Digest | None
    detail_size: int | None

    def canonical_record(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason_code": self.reason_code.value if self.reason_code is not None else None,
            "detail_digest": self.detail_digest,
            "detail_size": self.detail_size,
        }


def _normalize_resolver_result(result: ResolverResult) -> NormalizedResolverResult:
    if not isinstance(result, ResolverResult):
        raise TypeError("resolver must return ResolverResult")
    if result.outcome not in _ACTION_OUTCOMES:
        raise ValueError(f"resolver returned invalid outcome: {result.outcome!r}")
    if result.reason_code is not None and not isinstance(result.reason_code, SemanticReasonCode):
        raise ValueError("resolver reason_code must be a SemanticReasonCode")
    if result.detail is not None and result.reason_code is None:
        raise ValueError("resolver detail requires a SemanticReasonCode")
    if result.detail is None:
        detail_digest = None
        detail_size = None
    else:
        payload = _canonical_bytes(result.detail)
        detail_digest = _sha256_digest(payload)
        detail_size = len(payload)
    return NormalizedResolverResult(
        outcome=result.outcome,
        reason_code=result.reason_code,
        detail_digest=detail_digest,
        detail_size=detail_size,
    )


def _canonical_json(value: Any) -> str:
    """Canonical JSON used to freeze resolver conformance inputs."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class ResolverConformanceVector:
    """Immutable normalized input that exercises one resolver behavior path."""

    vector_id: str
    action_args_json: str
    observation_json: str

    @classmethod
    def from_inputs(
        cls,
        vector_id: str,
        action_args: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
    ) -> ResolverConformanceVector:
        if not vector_id:
            raise ValueError("resolver conformance vector_id must be non-empty")
        return cls(
            vector_id=vector_id,
            action_args_json=_canonical_json(dict(action_args)),
            observation_json=_canonical_json(
                dict(observation) if observation is not None else None
            ),
        )

    def materialize(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        action_args = json.loads(self.action_args_json)
        observation = json.loads(self.observation_json)
        if not isinstance(action_args, dict):
            raise ValueError("resolver action_args vector must decode to an object")
        if observation is not None and not isinstance(observation, dict):
            raise ValueError("resolver observation vector must decode to an object or null")
        return action_args, observation

    def canonical_input_record(self) -> dict[str, Any]:
        action_args, observation = self.materialize()
        return {
            "vector_id": self.vector_id,
            "action_args": action_args,
            "observation": observation,
        }


@dataclass(frozen=True)
class ResolverSpec:
    """Versioned resolver plus behavior-defining canonical conformance vectors."""

    resolver_id: str
    resolver_version: str
    resolve: OutcomeResolver = field(repr=False, compare=False)
    conformance_vectors: tuple[ResolverConformanceVector, ...]
    behavior_digest: Digest = field(init=False)

    def __post_init__(self) -> None:
        if not self.resolver_id or not self.resolver_version:
            raise ValueError("resolver_id and resolver_version must be non-empty")
        if not self.conformance_vectors:
            raise ValueError("resolver must define at least one conformance vector")
        vector_ids = [vector.vector_id for vector in self.conformance_vectors]
        if len(vector_ids) != len(set(vector_ids)):
            raise ValueError("resolver conformance vector IDs must be unique")
        records: list[dict[str, Any]] = []
        for vector in sorted(self.conformance_vectors, key=lambda item: item.vector_id):
            action_args, observation = vector.materialize()
            normalized = _normalize_resolver_result(self.resolve(action_args, observation))
            records.append(
                {
                    **vector.canonical_input_record(),
                    "output": normalized.canonical_record(),
                }
            )
        object.__setattr__(self, "behavior_digest", _sha256_digest(records))

    def resolve_normalized(
        self,
        action_args: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
    ) -> NormalizedResolverResult:
        return _normalize_resolver_result(self.resolve(action_args, observation))


class ResolverRegistry:
    """Immutable registry of versioned, behavior-digested outcome resolvers."""

    def __init__(self, specs: Sequence[ResolverSpec]) -> None:
        entries: dict[tuple[str, str], ResolverSpec] = {}
        for spec in specs:
            key = (spec.resolver_id, spec.resolver_version)
            if key in entries:
                raise ValueError(f"duplicate resolver registration: {key[0]}@{key[1]}")
            entries[key] = spec
        self._entries = entries

    def get(self, reference: ResolverRef) -> ResolverSpec:
        try:
            return self._entries[(reference.resolver_id, reference.resolver_version)]
        except KeyError as exc:
            raise KeyError(
                f"unknown resolver: {reference.resolver_id}@{reference.resolver_version}"
            ) from exc


def _vector(
    vector_id: str,
    action_args: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
) -> ResolverConformanceVector:
    return ResolverConformanceVector.from_inputs(
        vector_id,
        action_args,
        observation,
    )


DEFAULT_RESOLVER_SPEC = ResolverSpec(
    resolver_id="default",
    resolver_version="2.0.0",
    resolve=default_outcome_resolver,
    conformance_vectors=(
        _vector("no_observation", {}, None),
        _vector("top_level_exit_zero", {}, {"exit_code": 0}),
        _vector("nested_exit_nonzero", {}, {"extra": {"exit_code": 9}}),
        _vector("error_flag", {}, {"error": "opaque"}),
        _vector("error_status", {}, {"status": "failed"}),
        _vector("opaque_success", {}, {"content": "opaque"}),
    ),
)
BASH_RESOLVER_SPEC = ResolverSpec(
    resolver_id="bash-command",
    resolver_version="2.1.0",
    resolve=bash_command_outcome_resolver,
    conformance_vectors=(
        _vector("no_observation", {"command": "true"}, None),
        _vector(
            "environment_assignment_no_match",
            {"command": "LC_ALL=C grep absent file.txt"},
            {"exit_code": 1},
        ),
        _vector(
            "pipeline_status_owner",
            {"command": "cat file.txt | diff expected.txt -"},
            {"exit_code": 1},
        ),
        _vector(
            "nested_exit_code",
            {"command": "grep pattern file.txt"},
            {"extra": {"exit_code": 2}},
        ),
        _vector(
            "compound_command_ambiguous",
            {"command": "cd work && grep pattern file.txt"},
            {"exit_code": 1},
        ),
        _vector("shell_parse_error", {"command": "'"}, {"exit_code": 2}),
        _vector("empty_command", {"command": ""}, {"exit_code": 0}),
        _vector("incomplete_pipeline", {"command": "echo ok |"}, {"exit_code": 2}),
        _vector("missing_shell_program", {"command": "env -i"}, {"exit_code": 0}),
        _vector(
            "system_error",
            {"command": "missing-command"},
            {"exit_code": 127, "output": "command not found"},
        ),
        _vector("exit_code_missing", {"command": "python task.py"}, {"output": "done"}),
    ),
)
STRUCTURED_SEARCH_RESOLVER_SPEC = ResolverSpec(
    resolver_id="structured-search",
    resolver_version="2.0.0",
    resolve=structured_search_outcome_resolver,
    conformance_vectors=(
        _vector("no_observation", {}, None),
        _vector("zero_count", {}, {"match_count": 0}),
        _vector("positive_count", {}, {"result_count": 2}),
        _vector("empty_results", {}, {"results": []}),
        _vector("nonempty_results", {}, {"results": [{"id": "r1"}]}),
        _vector("undeclared_empty_content", {}, {"content": ""}),
        _vector("error_flag", {}, {"error": "opaque"}),
        _vector("error_status", {}, {"status": "error"}),
        _vector("unknown_shape", {}, {"content": "opaque"}),
    ),
)
DEFAULT_RESOLVER_REGISTRY = ResolverRegistry(
    (
        DEFAULT_RESOLVER_SPEC,
        BASH_RESOLVER_SPEC,
        STRUCTURED_SEARCH_RESOLVER_SPEC,
    )
)


@dataclass(frozen=True)
class ActionResolution:
    """Role plus storage-safe normalized outcome for one action."""

    role: ToolRole
    outcome: ActionOutcome
    reason_code: SemanticReasonCode | None
    detail_digest: Digest | None
    detail_size: int | None


@dataclass(frozen=True)
class TrajectorySemanticsProfile:
    """Versioned specification of tool semantic roles and outcome rules."""

    profile_id: str
    version: str
    description: str
    tool_rules: tuple[ToolMappingRule, ...] = ()
    resolver_registry: ResolverRegistry = field(
        default=DEFAULT_RESOLVER_REGISTRY,
        repr=False,
        compare=False,
    )

    @property
    def digest(self) -> str:
        """Digest profile rules and the conformance-observed resolver behavior."""
        rules = []
        for rule in self.tool_rules:
            spec = self.resolver_registry.get(rule.resolver)
            rules.append(
                {
                    "tool_pattern": rule.tool_pattern,
                    "role": rule.role,
                    "command_prefix": rule.command_prefix,
                    "resolver_id": spec.resolver_id,
                    "resolver_version": spec.resolver_version,
                    "resolver_behavior_digest": spec.behavior_digest,
                }
            )
        return _sha256_digest(
            {
                "profile_id": self.profile_id,
                "version": self.version,
                "description": self.description,
                "rules": rules,
            }
        )

    def resolve_action(
        self,
        tool_name: str,
        action_args: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
        *,
        strict: bool = True,
    ) -> ActionResolution:
        """Resolve an action without exposing free-form resolver detail."""
        command = str(action_args.get("command") or action_args.get("cmd") or "").strip()
        for rule in self.tool_rules:
            if rule.tool_pattern not in (tool_name, "*"):
                continue
            if rule.command_prefix:
                program, _ = _shell_program(command)
                if program != rule.command_prefix:
                    continue
            spec = self.resolver_registry.get(rule.resolver)
            normalized = spec.resolve_normalized(action_args, observation)
            return ActionResolution(
                role=rule.role,
                outcome=normalized.outcome,
                reason_code=normalized.reason_code,
                detail_digest=normalized.detail_digest,
                detail_size=normalized.detail_size,
            )
        if strict:
            raise UnmappedActionError(
                f"Profile '{self.profile_id}@{self.version}' has no mapping rule "
                f"for tool '{tool_name}'"
            )
        return ActionResolution(
            role="other",
            outcome="unknown_semantics",
            reason_code=SemanticReasonCode.UNMAPPED_TOOL,
            detail_digest=None,
            detail_size=None,
        )


# Standard default profiles
GENERIC_POSIX_PROFILE = TrajectorySemanticsProfile(
    profile_id="posix-generic",
    version="1.0.0",
    description="Standard POSIX tool semantics covering bash commands and common structured tools",
    tool_rules=(
        ToolMappingRule("bash", "search", BASH_RESOLVER_REF, command_prefix="grep"),
        ToolMappingRule("bash", "search", BASH_RESOLVER_REF, command_prefix="rg"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="diff"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="cat"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="ls"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="head"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="tail"),
        ToolMappingRule("bash", "write", BASH_RESOLVER_REF, command_prefix="sed"),
        ToolMappingRule("bash", "write", BASH_RESOLVER_REF, command_prefix="touch"),
        ToolMappingRule("bash", "write", BASH_RESOLVER_REF, command_prefix="echo"),
        ToolMappingRule("bash", "execute", BASH_RESOLVER_REF),
        ToolMappingRule("read", "read"),
        ToolMappingRule("write", "write"),
        ToolMappingRule(
            "run_bash",
            "search",
            BASH_RESOLVER_REF,
            command_prefix="grep",
        ),
        ToolMappingRule(
            "run_bash",
            "search",
            BASH_RESOLVER_REF,
            command_prefix="rg",
        ),
        ToolMappingRule(
            "run_bash",
            "inspect",
            BASH_RESOLVER_REF,
            command_prefix="diff",
        ),
        ToolMappingRule("run_bash", "execute", BASH_RESOLVER_REF),
        ToolMappingRule("edit", "write"),
        ToolMappingRule("grep", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("glob", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("think", "think"),
        ToolMappingRule("finish", "terminate"),
        ToolMappingRule("submit", "terminate"),
        ToolMappingRule("terminate", "terminate"),
    ),
)
LOCA_PROFILE = TrajectorySemanticsProfile(
    profile_id="loca-bench-v1",
    version="1.0.0",
    description="Explicit profile for LOCA long-context benchmark operations and runtime tools",
    tool_rules=(
        ToolMappingRule("read_file", "read"),
        ToolMappingRule("view_file", "read"),
        ToolMappingRule("search_files", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("grep_files", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("retrieve_context", "read"),
        ToolMappingRule("compact_memory", "execute"),
        ToolMappingRule("evict_memory", "execute"),
        ToolMappingRule("execute_query", "execute"),
        ToolMappingRule("db_query", "execute"),
        ToolMappingRule("bash", "search", BASH_RESOLVER_REF, command_prefix="grep"),
        ToolMappingRule("bash", "search", BASH_RESOLVER_REF, command_prefix="find"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="cat"),
        ToolMappingRule("bash", "execute", BASH_RESOLVER_REF),
        ToolMappingRule("submit", "terminate"),
        ToolMappingRule("answer", "terminate"),
        ToolMappingRule("finish", "terminate"),
    ),
)

AGENTABSTAIN_PROFILE = TrajectorySemanticsProfile(
    profile_id="agentabstain-v1",
    version="1.0.0",
    description="Explicit profile for AgentAbstain ambiguous action and safe tool execution",
    tool_rules=(
        ToolMappingRule("spotify.get_user_playlists", "read"),
        ToolMappingRule("spotify.get_playlist_tracks", "read"),
        ToolMappingRule(
            "spotify.search_tracks",
            "search",
            STRUCTURED_SEARCH_RESOLVER_REF,
        ),
        ToolMappingRule("spotify.write_gmail_draft", "write"),
        ToolMappingRule("gmail_and_email_records.manage_gmail_draft", "write"),
        ToolMappingRule("gmail.list_drafts", "read"),
        ToolMappingRule("gmail.send_message", "communicate"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="cat"),
        ToolMappingRule("bash", "search", BASH_RESOLVER_REF, command_prefix="grep"),
        ToolMappingRule("bash", "execute", BASH_RESOLVER_REF),
        ToolMappingRule("read", "read"),
        ToolMappingRule("write", "write"),
        ToolMappingRule("abstain", "terminate"),
        ToolMappingRule("finish", "terminate"),
        ToolMappingRule("submit", "terminate"),
    ),
)

DEEPPLANNING_PROFILE = TrajectorySemanticsProfile(
    profile_id="deepplanning-v1",
    version="1.0.0",
    description="Explicit profile for DeepPlanning multi-step shopping and travel workflows",
    tool_rules=(
        ToolMappingRule("search_items", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("search_flights", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("search_hotels", "search", STRUCTURED_SEARCH_RESOLVER_REF),
        ToolMappingRule("filter_items", "inspect"),
        ToolMappingRule("view_item_details", "inspect"),
        ToolMappingRule("view_details", "inspect"),
        ToolMappingRule("calculate_budget", "inspect"),
        ToolMappingRule("check_constraints", "inspect"),
        ToolMappingRule("add_to_cart", "execute"),
        ToolMappingRule("book_travel", "execute"),
        ToolMappingRule("reserve_hotel", "execute"),
        ToolMappingRule("bash", "inspect", BASH_RESOLVER_REF, command_prefix="cat"),
        ToolMappingRule("bash", "execute", BASH_RESOLVER_REF),
        ToolMappingRule("submit_plan", "terminate"),
        ToolMappingRule("submit", "terminate"),
        ToolMappingRule("finish", "terminate"),
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
    raise KeyError(
        f"Unknown semantics profile: {name_or_id}. Known profiles: {sorted(BENCHMARK_PROFILES.keys())}"
    )


class TaskProfileBinding(ContractModel):
    """Explicit, digest-pinned task-to-profile selection."""

    task_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_digest: Digest

    @classmethod
    def from_profile(
        cls,
        task_id: str,
        profile: TrajectorySemanticsProfile,
    ) -> TaskProfileBinding:
        return cls(
            task_id=task_id,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            profile_digest=profile.digest,
        )

    @model_validator(mode="after")
    def _validate_profile_pin(self) -> TaskProfileBinding:
        profile = get_profile(self.profile_id)
        if profile.version != self.profile_version:
            raise ValueError("binding profile_version does not match registered profile")
        if profile.digest != self.profile_digest:
            raise ValueError("binding profile_digest does not match registered profile")
        return self

    @property
    def digest(self) -> str:
        return _sha256_digest(self.model_dump(mode="json"))


CoverageStatus = Literal["analysis_ready", "screening_only", "stale_profile"]


class SemanticCoverage(ContractModel):
    """Explicit coverage decision for one trial and one pinned profile."""

    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    binding_digest: Digest
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_digest: Digest
    total_actions: int = Field(ge=0)
    resolved_actions: int = Field(ge=0)
    unknown_actions: int = Field(ge=0)
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    query_threshold: float = Field(ge=0.0, le=1.0)
    status: CoverageStatus


@dataclass(frozen=True)
class SemanticProjectionResult:
    files: tuple[Path, ...]
    coverage: tuple[SemanticCoverage, ...]


SEMANTIC_ACTION_FACT_SCHEMA = pa.schema(
    [
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("task_id", pa.string(), nullable=False),
        pa.field("binding_digest", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("step_id", pa.int64(), nullable=True),
        pa.field("action_id", pa.string(), nullable=False),
        pa.field("tool_call_id", pa.string(), nullable=True),
        pa.field("tool_name", pa.string(), nullable=False),
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("arguments_sha256", pa.string(), nullable=False),
        pa.field("observation_sha256", pa.string(), nullable=False),
        pa.field("observation_correlation", pa.string(), nullable=False),
        pa.field("correlation_reason", pa.string(), nullable=True),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("reason_code", pa.string(), nullable=True),
        pa.field("detail_digest", pa.string(), nullable=True),
        pa.field("detail_size", pa.int64(), nullable=True),
        pa.field("intervention_provenance", pa.string(), nullable=False),
        pa.field("intervention_sha256", pa.string(), nullable=True),
        pa.field("intervention_length", pa.int64(), nullable=True),
        pa.field("intervention_reason", pa.string(), nullable=True),
        pa.field("profile_id", pa.string(), nullable=False),
        pa.field("profile_version", pa.string(), nullable=False),
        pa.field("profile_digest", pa.string(), nullable=False),
        pa.field("source_ref", pa.string(), nullable=False),
    ]
)

SEMANTIC_ACTION_COVERAGE_SCHEMA = pa.schema(
    [
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("task_id", pa.string(), nullable=False),
        pa.field("binding_digest", pa.string(), nullable=False),
        pa.field("profile_id", pa.string(), nullable=False),
        pa.field("profile_version", pa.string(), nullable=False),
        pa.field("profile_digest", pa.string(), nullable=False),
        pa.field("total_actions", pa.int64(), nullable=False),
        pa.field("resolved_actions", pa.int64(), nullable=False),
        pa.field("unknown_actions", pa.int64(), nullable=False),
        pa.field("coverage_fraction", pa.float64(), nullable=False),
        pa.field("query_threshold", pa.float64(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
    ]
)
TRAJECTORY_SEMANTIC_SCHEMAS: dict[str, pa.Schema] = {
    "semantic_action_facts": SEMANTIC_ACTION_FACT_SCHEMA,
    "semantic_action_coverage": SEMANTIC_ACTION_COVERAGE_SCHEMA,
}


def extract_semantic_actions(
    atif_data: Mapping[str, Any],
    profile: TrajectorySemanticsProfile,
    *,
    job_id: str,
    trial_id: str,
    task_id: str,
    binding_digest: str,
    document_id: str,
    source_sha256: str,
    source_ref: str,
    strict: bool = True,
) -> list[SemanticActionFact]:
    """Extract semantic facts while retaining no raw arguments or message content."""
    steps = atif_data.get("steps")
    if not isinstance(steps, list):
        raise ValueError("ATIF document must contain a steps list")

    facts: list[SemanticActionFact] = []
    current_sequence = 0
    emitted_action = False
    pending_provenance: InterventionProvenance | None = None
    pending_digest: str | None = None
    pending_length: int | None = None
    pending_reason: InterventionReason | None = None

    for step_index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        step_source = str(step.get("source") or step.get("role") or "").lower()
        raw_step_id = step.get("step_id")
        step_id: int | None = None
        if raw_step_id is not None:
            with suppress(ValueError, TypeError):
                step_id = int(raw_step_id)

        if step_source in ("user", "human", "system", "environment", "harness"):
            if not emitted_action:
                continue
            message = str(step.get("message") or step.get("content") or step.get("error") or "")
            pending_digest = _sha256_digest(message)
            pending_length = len(message.encode("utf-8"))
            explicit_error = bool(
                step.get("error")
                or step.get("is_error")
                or step.get("status") in ("failed", "error")
            )
            if step_source in ("user", "human"):
                pending_provenance = "user_assisted"
                pending_reason = "post_action_user_message"
            elif step_source == "harness" and step.get("retry"):
                pending_provenance = "harness_retry"
                pending_reason = "explicit_harness_retry"
            elif step_source in ("environment", "harness") and explicit_error:
                pending_provenance = "environment_recovery"
                pending_reason = "explicit_environment_error"
            else:
                pending_provenance = "system_assisted"
                pending_reason = "post_action_system_message"
            continue

        raw_tool_calls = step.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        if not tool_calls and (
            step.get("tool_name") or step.get("function_name") or step.get("tool")
        ):
            tool_calls = [step]

        observations_raw = step.get("observations")
        if observations_raw is None:
            observation = step.get("observation")
            if isinstance(observation, Mapping):
                results = observation.get("results")
                observations_raw = results if isinstance(results, list) else [observation]
            elif isinstance(observation, list):
                observations_raw = observation
            else:
                observations_raw = []
        observations = (
            [item for item in observations_raw if isinstance(item, Mapping)]
            if isinstance(observations_raw, list)
            else []
        )
        keyed_observations: dict[str, Mapping[str, Any]] = {}
        unkeyed_observations: list[Mapping[str, Any]] = []
        for observation in observations:
            call_id = (
                observation.get("source_call_id")
                or observation.get("tool_call_id")
                or observation.get("call_id")
            )
            if call_id is None:
                unkeyed_observations.append(observation)
            else:
                keyed_observations[str(call_id)] = observation

        for call_index, raw_call in enumerate(tool_calls):
            if not isinstance(raw_call, Mapping):
                continue
            tool_name = str(
                raw_call.get("tool_name")
                or raw_call.get("function_name")
                or raw_call.get("tool")
                or raw_call.get("name")
                or "unknown"
            )
            tool_call_id_value = raw_call.get("tool_call_id") or raw_call.get("call_id")
            tool_call_id = str(tool_call_id_value) if tool_call_id_value is not None else None
            action_id = str(
                raw_call.get("action_id")
                or raw_call.get("event_id")
                or tool_call_id
                or f"act_{step_index}_{call_index}"
            )
            raw_args = raw_call.get("arguments")
            if raw_args is None:
                raw_args = raw_call.get("args")
            if raw_args is None:
                raw_args = raw_call.get("parameters")
            if raw_args is None:
                command = raw_call.get("command") or raw_call.get("cmd")
                raw_args = {"command": command} if command is not None else {}
            if isinstance(raw_args, str):
                try:
                    decoded_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    decoded_args = {"raw_text": raw_args}
                args_map = (
                    decoded_args if isinstance(decoded_args, Mapping) else {"value": decoded_args}
                )
            elif isinstance(raw_args, Mapping):
                args_map = raw_args
            else:
                args_map = {"value": raw_args}

            matched_observation: Mapping[str, Any] | None = None
            correlation: ObservationCorrelation
            correlation_reason: ObservationCorrelationReason | None
            if tool_call_id is not None and tool_call_id in keyed_observations:
                matched_observation = keyed_observations[tool_call_id]
                correlation = "matched_call_id"
                correlation_reason = None
            elif (
                tool_call_id is None
                and len(tool_calls) == 1
                and len(unkeyed_observations) == 1
                and not keyed_observations
            ):
                matched_observation = unkeyed_observations[0]
                correlation = "singleton_unkeyed"
                correlation_reason = "single_call_single_observation"
            elif observations:
                correlation = "unknown_unmatched"
                correlation_reason = (
                    "tool_call_id_not_found"
                    if tool_call_id is not None
                    else "ambiguous_unkeyed_observations"
                )
            else:
                correlation = "no_observation"
                correlation_reason = "observation_absent"

            resolution = profile.resolve_action(
                tool_name,
                args_map,
                matched_observation,
                strict=strict,
            )
            if correlation == "unknown_unmatched":
                resolution = ActionResolution(
                    role=resolution.role,
                    outcome="unknown_semantics",
                    reason_code=SemanticReasonCode.OBSERVATION_CORRELATION_UNKNOWN,
                    detail_digest=None,
                    detail_size=None,
                )

            provenance = pending_provenance or "autonomous"
            facts.append(
                SemanticActionFact(
                    job_id=job_id,
                    trial_id=trial_id,
                    task_id=task_id,
                    binding_digest=binding_digest,
                    document_id=document_id,
                    step_id=step_id,
                    action_id=action_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    sequence=current_sequence,
                    arguments_sha256=_sha256_digest(args_map),
                    observation_sha256=(
                        _sha256_digest(matched_observation)
                        if matched_observation is not None
                        else _sha256_digest(b"")
                    ),
                    observation_correlation=correlation,
                    correlation_reason=correlation_reason,
                    source_sha256=source_sha256,
                    role=resolution.role,
                    outcome=resolution.outcome,
                    reason_code=resolution.reason_code,
                    detail_digest=resolution.detail_digest,
                    detail_size=resolution.detail_size,
                    intervention_provenance=provenance,
                    intervention_sha256=pending_digest,
                    intervention_length=pending_length,
                    intervention_reason=pending_reason,
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                    profile_digest=profile.digest,
                    source_ref=(
                        f"{source_ref}#step={step_id if step_id is not None else step_index}"
                        f"#action={action_id}"
                    ),
                )
            )
            emitted_action = True
            current_sequence += 1
        if tool_calls:
            pending_provenance = None
            pending_digest = None
            pending_length = None
            pending_reason = None

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
        pq.write_table(
            table,
            tmp_name,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
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

    Rows are sorted by immutable trial/document/action identity to guarantee
    bit-for-bit reproducibility across input collection orderings.
    """
    target = Path(output_path)
    sorted_actions = sorted(
        actions,
        key=lambda action: (
            action.job_id,
            action.trial_id,
            action.document_id,
            action.sequence,
            action.action_id,
        ),
    )

    rows = [
        {
            "job_id": a.job_id,
            "trial_id": a.trial_id,
            "task_id": a.task_id,
            "binding_digest": a.binding_digest,
            "document_id": a.document_id,
            "step_id": a.step_id,
            "action_id": a.action_id,
            "tool_call_id": a.tool_call_id,
            "tool_name": a.tool_name,
            "sequence": a.sequence,
            "arguments_sha256": a.arguments_sha256,
            "observation_sha256": a.observation_sha256,
            "observation_correlation": a.observation_correlation,
            "correlation_reason": a.correlation_reason,
            "source_sha256": a.source_sha256,
            "role": a.role,
            "outcome": a.outcome,
            "reason_code": (a.reason_code.value if a.reason_code is not None else None),
            "detail_digest": a.detail_digest,
            "detail_size": a.detail_size,
            "intervention_provenance": a.intervention_provenance,
            "intervention_sha256": a.intervention_sha256,
            "intervention_length": a.intervention_length,
            "intervention_reason": a.intervention_reason,
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


def semantic_coverage(
    actions: Sequence[SemanticActionFact],
    *,
    job_id: str,
    trial_id: str,
    binding: TaskProfileBinding,
    query_threshold: float,
) -> SemanticCoverage:
    """Compute one explicit thresholded coverage decision."""
    if not 0.0 <= query_threshold <= 1.0:
        raise ValueError("query_threshold must be between 0.0 and 1.0")
    unknown_actions = sum(
        action.outcome == "unknown_semantics"
        or action.observation_correlation == "unknown_unmatched"
        for action in actions
    )
    total_actions = len(actions)
    resolved_actions = total_actions - unknown_actions
    fraction = resolved_actions / total_actions if total_actions else 0.0
    return SemanticCoverage(
        job_id=job_id,
        trial_id=trial_id,
        task_id=binding.task_id,
        binding_digest=binding.digest,
        profile_id=binding.profile_id,
        profile_version=binding.profile_version,
        profile_digest=binding.profile_digest,
        total_actions=total_actions,
        resolved_actions=resolved_actions,
        unknown_actions=unknown_actions,
        coverage_fraction=fraction,
        query_threshold=query_threshold,
        status=(
            "analysis_ready"
            if total_actions > 0 and fraction >= query_threshold
            else "screening_only"
        ),
    )


def _embedded_payload(payload: Mapping[str, Any], embedded_path: str | None) -> Mapping[str, Any]:
    current = payload
    if embedded_path is None:
        return current
    for component in embedded_path.split("/"):
        identifier = component.removeprefix("subagent:")
        children = current.get("subagent_trajectories")
        if not isinstance(children, list):
            raise ValueError(f"embedded ATIF path is absent: {embedded_path}")
        selected: Mapping[str, Any] | None = None
        for index, child in enumerate(children):
            if not isinstance(child, Mapping):
                continue
            child_identifier = str(child.get("trajectory_id") or index)
            if child_identifier == identifier:
                selected = child
                break
        if selected is None:
            raise ValueError(f"embedded ATIF path is absent: {embedded_path}")
        current = selected
    return current


def _load_normalized_source_document(
    trial: TrialRecord,
    trajectory: TrajectoryFact,
) -> Mapping[str, Any]:
    source = trial.path / trajectory.source_path
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"ATIF source is not an object: {source}")
    return _embedded_payload(payload, trajectory.embedded_path)


def _validate_mechanical_identity(
    actions: Sequence[SemanticActionFact],
    projection: Any,
    *,
    document_id: str,
) -> None:
    expected = {
        (tool.step_id, tool.tool_call_id): tool
        for tool in projection.tool_calls
        if tool.document_id == document_id
    }
    observed = {
        (action.step_id, action.tool_call_id): action
        for action in actions
        if action.document_id == document_id and action.tool_call_id is not None
    }
    if set(expected) != set(observed):
        raise ValueError(f"semantic/mechanical tool identity mismatch for document {document_id}")
    for key, tool in expected.items():
        action = observed[key]
        if action.arguments_sha256 != tool.arguments_sha256:
            raise ValueError(f"semantic/mechanical argument digest mismatch for call {key[1]}")
        if action.tool_name != tool.function_name:
            raise ValueError(f"semantic/mechanical tool name mismatch for call {key[1]}")


def _write_coverage_parquet(
    coverage: SemanticCoverage,
    output_path: Path,
) -> Path:
    table = pa.Table.from_pylist(
        [coverage.model_dump(mode="json")],
        schema=SEMANTIC_ACTION_COVERAGE_SCHEMA,
    )
    with _table_lock(output_path, exclusive=True):
        _write_parquet_atomic(output_path, table)
    return output_path


def project_job_semantics(
    jobs: Sequence[JobRecord],
    *,
    bindings: Sequence[TaskProfileBinding],
    output_root: Path | str,
    query_threshold: float,
    strict: bool = True,
) -> SemanticProjectionResult:
    """Project validated ATIF documents with an explicit binding for every task."""
    by_task: dict[str, TaskProfileBinding] = {}
    for binding in bindings:
        if binding.task_id in by_task:
            raise ValueError(f"duplicate task profile binding: {binding.task_id}")
        by_task[binding.task_id] = binding

    root = Path(output_root).resolve()
    files: list[Path] = []
    coverage_rows: list[SemanticCoverage] = []
    for job in sorted(jobs, key=lambda item: item.id):
        for trial in sorted(job.trials, key=lambda item: item.id):
            task_value = trial.result.get("task_name")
            if not isinstance(task_value, str) or not task_value:
                raise ValueError(f"trial {trial.id} has no explicit task_name")
            if task_value not in by_task:
                raise ValueError(f"task has no semantics profile binding: {task_value}")
            binding = by_task[task_value]
            profile = get_profile(binding.profile_id)
            projection = project_trial(job, trial)
            unusable = [
                trajectory
                for trajectory in projection.trajectories
                if trajectory.validation_status != "valid"
            ]
            if unusable:
                reasons = ", ".join(
                    trajectory.validation_error or trajectory.validation_status
                    for trajectory in unusable
                )
                raise ValueError(f"trial {trial.id} has unusable ATIF: {reasons}")

            actions: list[SemanticActionFact] = []
            for trajectory in projection.trajectories:
                payload = _load_normalized_source_document(trial, trajectory)
                document_actions = extract_semantic_actions(
                    payload,
                    profile,
                    job_id=job.id,
                    trial_id=trial.id,
                    task_id=task_value,
                    binding_digest=binding.digest,
                    document_id=trajectory.document_id,
                    source_sha256=trajectory.source_sha256,
                    source_ref=(
                        trajectory.source_path
                        if trajectory.embedded_path is None
                        else f"{trajectory.source_path}#{trajectory.embedded_path}"
                    ),
                    strict=strict,
                )
                _validate_mechanical_identity(
                    document_actions,
                    projection,
                    document_id=trajectory.document_id,
                )
                actions.extend(document_actions)

            partition = root / f"job_id={job.id}" / f"trial_id={trial.id}"
            action_path = project_semantic_actions_parquet(
                actions,
                partition / "semantic_action_facts.parquet",
            )
            coverage = semantic_coverage(
                actions,
                job_id=job.id,
                trial_id=trial.id,
                binding=binding,
                query_threshold=query_threshold,
            )
            coverage_path = _write_coverage_parquet(
                coverage,
                partition / "semantic_action_coverage.parquet",
            )
            files.extend((action_path, coverage_path))
            coverage_rows.append(coverage)

    return SemanticProjectionResult(
        files=tuple(files),
        coverage=tuple(sorted(coverage_rows, key=lambda row: (row.job_id, row.trial_id))),
    )


def query_semantic_coverage(
    derived_root: Path | str,
    *,
    query_threshold: float,
) -> tuple[SemanticCoverage, ...]:
    """Recompute coverage from hot semantic facts using an explicit threshold."""
    if not 0.0 <= query_threshold <= 1.0:
        raise ValueError("query_threshold must be between 0.0 and 1.0")
    root = Path(derived_root)
    coverage_files = sorted(root.glob("job_id=*/trial_id=*/semantic_action_coverage.parquet"))
    rows: list[SemanticCoverage] = []
    for coverage_file in coverage_files:
        stored_rows = pq.read_table(coverage_file).to_pylist()
        if len(stored_rows) != 1:
            raise ValueError(f"expected one coverage row: {coverage_file}")
        stored = SemanticCoverage.model_validate(stored_rows[0])
        action_file = coverage_file.with_name("semantic_action_facts.parquet")
        actions = (
            [
                SemanticActionFact.model_validate(row)
                for row in pq.read_table(action_file).to_pylist()
            ]
            if action_file.is_file()
            else []
        )
        try:
            current_profile = get_profile(stored.profile_id)
        except KeyError:
            profile_is_current = False
        else:
            profile_is_current = (
                current_profile.version == stored.profile_version
                and current_profile.digest == stored.profile_digest
            )
        binding = TaskProfileBinding.model_construct(
            task_id=stored.task_id,
            profile_id=stored.profile_id,
            profile_version=stored.profile_version,
            profile_digest=stored.profile_digest,
        )
        computed = semantic_coverage(
            actions,
            job_id=stored.job_id,
            trial_id=stored.trial_id,
            binding=binding,
            query_threshold=query_threshold,
        )
        rows.append(
            computed
            if profile_is_current
            else computed.model_copy(update={"status": "stale_profile"})
        )
    return tuple(sorted(rows, key=lambda row: (row.job_id, row.trial_id)))
