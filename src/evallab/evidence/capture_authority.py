"""Deterministic capture-authority and concordance contract at the ATIF boundary.

ATIF records direct tool calls issued by the agent runtime. Benchmark events
record the MCP service's observed calls. When an agent shells out to curl/HTTP
child processes (e.g. within bash), those calls appear in benchmark events but
not as direct ATIF tool calls. ATIF is not buggy in that case; it cannot observe
out-of-band child execution.

When an agent uses direct batch or range retrieval tools (such as
``get_context_chunks`` with explicit ``chunk_ids``), those direct tool calls
can be deterministically expanded into covered handles, representing valid batch
representation rather than indirect child execution.

This module establishes a deterministic capture-authority and concordance contract:
- Identifies direct vs indirect child execution.
- Distinguishes valid expanded batch/range calls from indirect capture loss.
- Compares expanded retrieval handles against benchmark retrieval handles while
  separately comparing non-retrieval call sequences/counts (e.g. list, mutate).
- Binds benchmark-event authority when benchmark events exist.
- Reason-codes incomplete trajectory capture so downstream trajectory-only ordering
  analysis can refuse while benchmark-event analysis remains admissible.
- Never synthesizes fake ATIF tool calls from verifier or benchmark evidence.
"""

from __future__ import annotations

from enum import StrEnum


class CaptureAuthority(StrEnum):
    BENCHMARK_EVENTS = "benchmark_events"
    ATIF_TRAJECTORY = "atif_trajectory"
    UNRESOLVED = "unresolved"
    NONE = "none"


_SHELL_FUNCTION_NAMES = frozenset(
    {
        "bash",
        "shell",
        "sh",
        "zsh",
        "fish",
        "cmd",
        "command",
        "powershell",
        "pwsh",
        "terminal",
        "exec",
        "execute",
        "run_terminal_cmd",
        "run_command",
        "bash_tool",
        "shell_command",
        "bash_command",
    }
)

_BATCH_TOOL_FUNCTION_NAMES = frozenset(
    {
        "get_context_chunks",
        "read_context_chunks",
        "read_chunks",
        "get_chunks",
        "batch_read",
        "batch_get_context_chunk",
        "batch_get_context_chunks",
        "range_batch",
    }
)

_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "get_context_chunk",
        "get_context_chunks",
        "read_chunk",
        "read_chunks",
        "read_context_chunk",
        "read_context_chunks",
        "batch_read",
        "batch_get_context_chunk",
        "batch_get_context_chunks",
        "range_batch",
        "get_chunks",
    }
)

_TOOL_REQUEST_EVENT_TYPES = frozenset(
    {
        "mcp_call",
        "tool_call",
        "request",
        "tool_invoked",
        "tool_call_requested",
        "tool_call_success",
        "tool_call_error",
        "tool_call_execution",
        "read_chunk",
        "execute_mutation",
        "tools/call",
        "call_tool",
    }
)


