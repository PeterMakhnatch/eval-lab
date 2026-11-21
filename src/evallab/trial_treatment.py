from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.evidence.parquet_io import (
    parquet_snapshot_paths,
)

TREATMENT_TABLE_FILENAME = "trial_treatment.parquet"
TREATMENT_TABLE = Path(TREATMENT_TABLE_FILENAME).stem
CAPTURE_TABLE_FILENAME = "trial_capture.parquet"
CAPTURE_TABLE = Path(CAPTURE_TABLE_FILENAME).stem

TREATMENT_SCHEMA = "evallab.trial_treatment/v1"

NOT_APPLICABLE = "n/a"
PROVIDER_DEFAULT = "default"

MIMO_SELFHOSTED_PREFIX = "selfhosted/"
OPENROUTER_PREFIX = "openrouter-metered/"
PARSER_SOURCE = "src/evallab/mimo_tool_calls.py"
PROXY_SOURCE = "containers/zai_openapi_secret_proxy.py"
SERVE_SOURCE = "tools/modal-mimo-serve/serve.py"

NO_MODEL_AGENTS = frozenset({"nop", "oracle"})

KEY_FIELDS = (
    "task_version_digest",
    "backend",
    "egress_lock",
    "agent",
    "harbor_version",
    "model",
    "model_revision",
    "serving_image",
    "serving_context_tokens",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "thinking",
    "harness_config_digest",
    "parser_digest",
    "agent_timeout_seconds",
    "agent_timeout_multiplier",
    "agent_timeout_override_seconds",
    "timeout_seconds",
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "cost_limit_usd",
)
TASK_FIELDS = ("task_version_digest", "agent_timeout_seconds")
SETUP_FIELDS = tuple(name for name in KEY_FIELDS if name not in TASK_FIELDS)

_TERMINUS_DEFAULTS: dict[str, Any] = {
    "parser_name": "json",
    "max_turns": None,
    "enable_summarize": True,
    "proactive_summarization_threshold": 8000,
    "interleaved_thinking": False,
    "trajectory_config": {"raw_content": False, "linear_history": False},
}
_TERMINUS_AGENTS = frozenset({"terminus-2", "terminus_2"})

_SAMPLING_KWARGS = frozenset({"temperature", "reasoning_effort", "max_thinking_tokens"})
_SAMPLING_LLM_KWARGS = frozenset({"temperature", "top_p", "top_k", "max_tokens"})
_THINKING_BODY_KEYS = frozenset({"reasoning_effort", "chat_template_kwargs"})
_SHAPED_PARAMS = ("temperature", "top_p", "top_k", "enable_thinking")

_CONTINUATION_RE = re.compile(r"^trajectory\.cont-(\d+)\.json$")
_SUMMARIZATION_RE = re.compile(r"^trajectory\.summarization-.+\.json$")


class _Absent:
    pass


ABSENT = _Absent()


_LOG_REACTIVE = "Context length exceeded"
_LOG_PROACTIVE = "Proactively summarizing"
_LOG_PROACTIVE_ERROR = "Error in proactively summarizing"
_LOG_FULL_FAILED = "SUMMARIZATION: Full summary failed"
_LOG_FULL_OK = "SUMMARIZATION: Full summary succeeded"
_LOG_SHORT_FAILED = "SUMMARIZATION: Short summary failed"
_LOG_SHORT_OK = "SUMMARIZATION: Short summary succeeded"
_LOG_FALLBACK_FAILED = "Even fallback chat failed"
_LOG_FALLBACK_OK = "Even fallback chat succeeded"
_LOG_SUMMARY_SAVED = "Summary subagent trajectory saved to"
_LOG_QUESTIONS_SAVED = "Questions subagent trajectory saved to"


def _matching_trials(
    treatments: Sequence[Mapping[str, Any]], captures: Sequence[Mapping[str, Any]]
) -> None:
    treatment_keys = {
        (row["job_name"], row["trial_name"], row.get("produced_at")) for row in treatments
    }
    capture_keys = {
        (row["job_name"], row["trial_name"], row.get("produced_at")) for row in captures
    }
    if treatment_keys != capture_keys:
        missing = treatment_keys ^ capture_keys
        jobs = ", ".join(sorted({str(job) for job, _, _ in missing}))
        raise ValueError(
            "treatment/capture trial identities or collection timestamps differ; "
            f"rerun tasks treatment-collect for the source jobs: {jobs}"
        )


def _table_paths(catalog: Path) -> dict[str, Path]:
    filenames = (TREATMENT_TABLE_FILENAME, CAPTURE_TABLE_FILENAME)
    snapshot = parquet_snapshot_paths(catalog / ".trial-tables")
    if snapshot is not None:
        if set(snapshot) != set(filenames):
            raise ValueError("treatment/capture snapshot must contain exactly both tables")
        return snapshot
    return {name: catalog / name for name in filenames if (catalog / name).is_file()}


def table_paths(catalog: Path) -> dict[str, Path]:
    paths = _table_paths(catalog)
    if not paths:
        return {}
    if len(paths) != 2:
        raise ValueError(
            "incomplete treatment/capture catalog; rerun tasks treatment-collect "
            "for the source jobs"
        )
    import pyarrow.parquet as pq

    keys = [
        pq.read_table(
            paths[name], columns=["job_name", "trial_name", "produced_at"]
        ).to_pylist()
        for name in (TREATMENT_TABLE_FILENAME, CAPTURE_TABLE_FILENAME)
    ]
    _matching_trials(keys[0], keys[1])
    return paths
