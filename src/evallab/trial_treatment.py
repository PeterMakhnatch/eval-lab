"""Per-trial treatment keys and capture records (HAR-93).

A **treatment key** hashes every behaviour-relevant setting of one trial, read
from the trial's own files: which task version it ran, where, with which agent
and model weights, how the model was sampled, how the harness was configured,
which tool-call parser sat between them, and which limits applied. Two trials
may be pooled into one pass rate only if their setups agree field by field.
``setup_key`` is the same hash without the task, so a cohort of tasks run
under one setup shares a single ``setup_key``.

A **capture record** says which evidence a trial left behind and how complete
it is: trajectory head and continuation files, verifier output, rollout
details, the proxy's per-call ledger, and whether the tokens the steps account
for match the result totals and the ledger.

Unknown is never zero: a value the trial's files cannot establish is ``None``
and is listed in ``unknown_fields``. Settings that do not apply (a nop agent
has no sampling) read :data:`NOT_APPLICABLE`.

Code-derived fields (route sampling, serving pins, parser digest) are read at
the Eval Lab commit recorded in ``lab-metadata.json``; a dirty or missing
commit makes them unknown. Recording-only changes do not split a key: the
repository commit itself is not a key field, source files are hashed as
docstring-free ASTs, and ``harbor_terminus.py`` (which also writes the
trajectory) is not hashed; the parser digest covers
``src/evallab/mimo_tool_calls.py`` only. Field sources are documented in
``docs/execution-tiers.md``.
"""

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

#: Value of a setting that does not apply to the trial (a nop agent's model).
NOT_APPLICABLE = "n/a"
#: A sampling parameter the harness did not send; the provider default applies.
PROVIDER_DEFAULT = "default"

MIMO_SELFHOSTED_PREFIX = "selfhosted/"
OPENROUTER_PREFIX = "openrouter-metered/"
PARSER_SOURCE = "src/evallab/mimo_tool_calls.py"
PROXY_SOURCE = "containers/zai_openapi_secret_proxy.py"
SERVE_SOURCE = "tools/modal-mimo-serve/serve.py"

#: Agents that never call a model.
NO_MODEL_AGENTS = frozenset({"nop", "oracle"})

#: Key fields, in hashing order. :data:`TASK_FIELDS` are excluded from
#: ``setup_key``.
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
#: Fields the task version determines (its digest and its own agent timeout).
TASK_FIELDS = ("task_version_digest", "agent_timeout_seconds")
SETUP_FIELDS = tuple(name for name in KEY_FIELDS if name not in TASK_FIELDS)

#: Terminus-2 constructor defaults (harbor 0.21), filled into the harness
#: config so an omitted kwarg and its explicit default hash alike.
_TERMINUS_DEFAULTS: dict[str, Any] = {
    "parser_name": "json",
    "max_turns": None,
    "enable_summarize": True,
    "proactive_summarization_threshold": 8000,
    "interleaved_thinking": False,
    "trajectory_config": {"raw_content": False, "linear_history": False},
}
_TERMINUS_AGENTS = frozenset({"terminus-2", "terminus_2"})

#: Agent kwargs that are sampling, not harness configuration.
_SAMPLING_KWARGS = frozenset({"temperature", "reasoning_effort", "max_thinking_tokens"})
_SAMPLING_LLM_KWARGS = frozenset({"temperature", "top_p", "top_k", "max_tokens"})
_THINKING_BODY_KEYS = frozenset({"reasoning_effort", "chat_template_kwargs"})
_SHAPED_PARAMS = ("temperature", "top_p", "top_k", "enable_thinking")

_CONTINUATION_RE = re.compile(r"^trajectory\.cont-(\d+)\.json$")
_SUMMARIZATION_RE = re.compile(r"^trajectory\.summarization-.+\.json$")












class _Absent:
    """A path that does not exist at a known commit."""


ABSENT = _Absent()









































#: Markers Harbor's Terminus-2 loop writes to trial.log while handling an
#: overflowing context (exact substrings, grounded in HAR-90 0758-b/c).
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
    """Resolve one immutable snapshot, or the pre-snapshot migration inputs."""
    filenames = (TREATMENT_TABLE_FILENAME, CAPTURE_TABLE_FILENAME)
    snapshot = parquet_snapshot_paths(catalog / ".trial-tables")
    if snapshot is not None:
        if set(snapshot) != set(filenames):
            raise ValueError("treatment/capture snapshot must contain exactly both tables")
        return snapshot
    return {name: catalog / name for name in filenames if (catalog / name).is_file()}


def table_paths(catalog: Path) -> dict[str, Path]:
    """Pin a complete treatment/capture pair; refuse partial legacy projections."""
    paths = _table_paths(catalog)
    if not paths:
        return {}
    if len(paths) != 2:
        raise ValueError(
            "incomplete treatment/capture catalog; rerun tasks treatment-collect "
            "for the source jobs"
        )
    # Older collectors could leave different trial sets or collection timestamps
    # after a partial write. File existence alone does not establish consistency.
    import pyarrow.parquet as pq

    keys = [
        pq.read_table(
            paths[name], columns=["job_name", "trial_name", "produced_at"]
        ).to_pylist()
        for name in (TREATMENT_TABLE_FILENAME, CAPTURE_TABLE_FILENAME)
    ]
    _matching_trials(keys[0], keys[1])
    return paths
