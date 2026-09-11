"""Portable RLM managed-REPL backend binding for Eval Lab."""

from __future__ import annotations

from evallab.rlm_runtime.backend import (
    EnvironmentHandle,
    ExecResult,
    ManagedReplBackend,
    ManagedReplError,
)
from evallab.rlm_runtime.capture import qualify, record_root_turn
from evallab.rlm_runtime.root import extract_openai_turn

__all__ = [
    "EnvironmentHandle",
    "ExecResult",
    "ManagedReplBackend",
    "ManagedReplError",
    "extract_openai_turn",
    "qualify",
    "record_root_turn",
]
