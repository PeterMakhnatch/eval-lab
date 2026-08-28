"""Deterministic Trajectory Error Taxonomy and Probe Classification.

Provides zero-LLM classification of trajectory errors, separating:
- Expected-negative probes (e.g. grep/which/test probe misses) from execution faults
- Harness schema rejections (tool parameter/format validation failures) from process non-zero exits
- Specific error root causes (file_not_found, permission_denied, syntax_error, timeout, runtime_exception)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    """Normalized categories for trajectory execution errors and probes."""

    HARNESS_SCHEMA_REJECTION = "harness_schema_rejection"
    COMMAND_NONZERO_EXIT = "command_nonzero_exit"
    FILE_NOT_FOUND = "file_not_found"
    PERMISSION_DENIED = "permission_denied"
    SYNTAX_ERROR = "syntax_error"
    TIMEOUT = "timeout"
    RUNTIME_EXCEPTION = "runtime_exception"
    EXPECTED_PROBE_MISS = "expected_probe_miss"
    NONE = "none"


class InterventionCategory(StrEnum):
    """Provenance category for trial execution autonomy."""

    AUTONOMOUS = "autonomous"
    HUMAN_DIRECTED = "human_directed"
    HUMAN_ASSISTED = "human_assisted"
    HUMAN_INTERVENED = "human_intervened"


# Regex patterns for deterministic probe detection
_PROBE_COMMAND_PATTERNS = re.compile(
    r"^\s*(?:which|command\s+-v|type\s+-p|grep|rg|ag|ack|find|test\s+|\[\s+|\[\[\s+|pgrep|git\s+status\s+--porcelain)\b",
    re.IGNORECASE,
)

# Regex patterns for harness schema rejections
_SCHEMA_REJECTION_PATTERNS = re.compile(
    r"(?:invalid\s+(?:parameters?|arguments?|tool\s+call|json|schema)|"
    r"missing\s+required\s+(?:argument|parameter|field)|"
    r"schema\s+validation\s+failed|"
    r"unrecognized\s+tool|"
    r"tool_not_found|"
    r"unknown\s+tool\s+name|"
    r"extra\s+fields?\s+not\s+permitted|"
    r"validation_error|"
    r"malformed\s+request|"
    r"failed\s+to\s+parse\s+tool\s+arguments|"
    r"jsondecodeerror)",
    re.IGNORECASE,
)

# Specific error patterns
_FILE_NOT_FOUND_PATTERNS = re.compile(
    r"(?:no\s+such\s+file\s+or\s+directory|filenotfounderror|cannot\s+find\s+file|path\s+does\s+not\s+exist)",
    re.IGNORECASE,
)

_PERMISSION_DENIED_PATTERNS = re.compile(
    r"(?:permission\s+denied|permissionerror|access\s+denied|eacces|operation\s+not\s+permitted)",
    re.IGNORECASE,
)

_SYNTAX_ERROR_PATTERNS = re.compile(
    r"(?:syntaxerror|parse\s+error|invalid\s+syntax|unexpected\s+token|syntax\s+error)",
    re.IGNORECASE,
)

_TIMEOUT_PATTERNS = re.compile(
    r"(?:timed?\s*out|timeouterror|command\s+timed\s+out|deadline\s+exceeded|timedoutafter)",
    re.IGNORECASE,
)

_RUNTIME_EXCEPTION_PATTERNS = re.compile(
    r"(?:traceback\s+\(most\s+recent\s+call\s+last\)|exception:|error:|fatal\s+error|panic:)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ErrorClassification:
    """Result of deterministic error classification."""

    is_error: bool
    is_expected_probe: bool
    category: ErrorCategory
    exit_code: int | None
    error_message: str | None


def is_probe_command(tool_name: str | None, tool_command: str | None) -> bool:
    """Check if a tool call or bash command is a reconnaissance / existence probe."""
    if not tool_command:
        return bool(tool_name and tool_name.lower() in {"which", "file_exists", "probe"})
    return bool(_PROBE_COMMAND_PATTERNS.search(tool_command.strip()))


def classify_step_error(
    *,
    tool_name: str | None = None,
    tool_command: str | None = None,
    exit_code: int | None = None,
    output_content: str | None = None,
    result_type: str | None = None,
    result_status: str | None = None,
) -> ErrorClassification:
    """Classify a step execution into a deterministic ErrorCategory.

    Separates:
    1. Expected negative probe misses (e.g. `grep foo bar` exiting 1 when probe is true)
    2. Harness schema rejections (e.g. invalid tool args or missing fields)
    3. Distinct runtime error categories (file_not_found, permission_denied, syntax, timeout, exception)
    4. Ordinary nonzero exit codes
    """
    output = (output_content or "").strip()
    r_type = (result_type or "").lower()
    r_status = (result_status or "").lower()

    # 1. Check for Harness Schema Rejections (tool argument parsing or validation failures)
    if _SCHEMA_REJECTION_PATTERNS.search(output) or r_type in {"schema_error", "validation_error"}:
        return ErrorClassification(
            is_error=True,
            is_expected_probe=False,
            category=ErrorCategory.HARNESS_SCHEMA_REJECTION,
            exit_code=exit_code,
            error_message=output[:200] or "Harness schema rejection",
        )

    # 2. Check if this is an expected-negative probe
    if (
        is_probe_command(tool_name, tool_command)
        and exit_code == 1
        and not _RUNTIME_EXCEPTION_PATTERNS.search(output)
        and not _SYNTAX_ERROR_PATTERNS.search(output)
    ):
        return ErrorClassification(
            is_error=False,
            is_expected_probe=True,
            category=ErrorCategory.EXPECTED_PROBE_MISS,
            exit_code=exit_code,
            error_message=f"Probe miss (exit code 1): {output[:100]}".strip(),
        )

    # 3. Check for execution errors (non-zero exit code or error status)
    has_nonzero_exit = exit_code is not None and exit_code != 0
    has_error_flag = r_type in {"error", "tool_error"} or r_status in {"error", "failed"}

    if not (has_nonzero_exit or has_error_flag):
        return ErrorClassification(
            is_error=False,
            is_expected_probe=False,
            category=ErrorCategory.NONE,
            exit_code=exit_code,
            error_message=None,
        )

    # Determine specific error category
    if _TIMEOUT_PATTERNS.search(output):
        cat = ErrorCategory.TIMEOUT
    elif _FILE_NOT_FOUND_PATTERNS.search(output):
        cat = ErrorCategory.FILE_NOT_FOUND
    elif _PERMISSION_DENIED_PATTERNS.search(output):
        cat = ErrorCategory.PERMISSION_DENIED
    elif _SYNTAX_ERROR_PATTERNS.search(output):
        cat = ErrorCategory.SYNTAX_ERROR
    elif _RUNTIME_EXCEPTION_PATTERNS.search(output):
        cat = ErrorCategory.RUNTIME_EXCEPTION
    else:
        cat = ErrorCategory.COMMAND_NONZERO_EXIT

    err_msg = output[:200].strip() if output else f"Command exited with code {exit_code}"

    return ErrorClassification(
        is_error=True,
        is_expected_probe=False,
        category=cat,
        exit_code=exit_code,
        error_message=err_msg,
    )


def classify_intervention_provenance(
    *,
    user_steps: int,
    agent_steps: int,
    system_steps: int,
    user_step_indices: list[int] | tuple[int, ...] = (),
    first_error_step: int | None = None,
) -> tuple[InterventionCategory, str]:
    """Classify trial execution provenance into autonomous vs human directed/assisted."""
    if user_steps == 0:
        return (
            InterventionCategory.AUTONOMOUS,
            "Autonomous execution (zero human turns in trajectory)",
        )

    # If all user steps occur strictly at the start (step 1 or before agent steps)
    if user_step_indices and max(user_step_indices) <= 1:
        return (
            InterventionCategory.HUMAN_DIRECTED,
            "Human directed (initial goal/task prompt only)",
        )

    # If any user step occurs after an error step, it is human-assisted recovery
    if first_error_step is not None and any(idx > first_error_step for idx in user_step_indices):
        return (
            InterventionCategory.HUMAN_ASSISTED,
            f"Human assisted (human turn intervened at or after error step {first_error_step})",
        )

    return (
        InterventionCategory.HUMAN_INTERVENED,
        f"Human intervened ({user_steps} mid-execution human turns)",
    )
