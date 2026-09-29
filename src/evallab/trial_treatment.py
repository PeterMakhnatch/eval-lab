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
``docs/trial-treatment.md``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.evidence.facts import trial_environment_type
from evallab.task_qualification import task_dir_for_trial
from evallab.task_stability import iter_trial_dirs

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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(data).hexdigest()


def source_digest(text: str) -> str:
    """Digest of Python source that ignores comments, docstrings and layout."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return "ast-sha256:" + hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest()


class _Absent:
    """A path that does not exist at a known commit."""


ABSENT = _Absent()


class CommitSources:
    """Read files at a recorded Eval Lab commit from the local object store."""

    def __init__(self, repo_root: Path) -> None:
        self._repo = repo_root
        self._cache: dict[tuple[str, str], str | _Absent | None] = {}

    def read(self, commit: str | None, path: str) -> str | _Absent | None:
        """File text, :data:`ABSENT` if the commit lacks it, ``None`` if unknown."""
        if not commit:
            return None
        key = (commit, path)
        if key not in self._cache:
            self._cache[key] = self._read(commit, path)
        return self._cache[key]

    def _read(self, commit: str, path: str) -> str | _Absent | None:
        known = subprocess.run(
            ["git", "-C", str(self._repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if known.returncode != 0:
            return None
        shown = subprocess.run(
            ["git", "-C", str(self._repo), "show", f"{commit}:{path}"],
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            return ABSENT
        return shown.stdout.decode("utf-8")


def _constant_assignments(text: str) -> dict[str, Any]:
    """Module-level ``NAME = <literal>`` assignments."""
    found: dict[str, Any] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def route_shaping(proxy_text: str) -> dict[str, Any]:
    """Sampling the proxy forces: ``x["temperature"] = 0.6``-style literals."""
    forced: dict[str, Any] = {}
    for node in ast.walk(ast.parse(proxy_text)):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _SHAPED_PARAMS
            ):
                forced[str(target.slice.value)] = node.value.value
    return forced


@dataclass
class _Field:
    value: Any
    source: str


@dataclass
class _Treatment:
    fields: dict[str, _Field] = field(default_factory=dict)

    def set(self, name: str, value: Any, source: str) -> None:
        self.fields[name] = _Field(value, source)


def _agent_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_dict(_dict(config.get("agent")).get("kwargs")))


def _agent_identity(
    config: Mapping[str, Any], result: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """(agent import path or name, Harbor agent name)."""
    agent = _dict(config.get("agent"))
    path = agent.get("import_path") or agent.get("name")
    info = _dict(result.get("agent_info")).get("name")
    return (path if isinstance(path, str) else None, info if isinstance(info, str) else None)


def _requested_thinking(kwargs: Mapping[str, Any], llm: Mapping[str, Any]) -> str:
    body = _dict(llm.get("extra_body"))
    template = _dict(llm.get("chat_template_kwargs")) or _dict(body.get("chat_template_kwargs"))
    parts = []
    effort = kwargs.get(
        "reasoning_effort", body.get("reasoning_effort", llm.get("reasoning_effort"))
    )
    if effort is not None:
        parts.append(f"reasoning_effort={effort}")
    if "enable_thinking" in template:
        parts.append(f"enable_thinking={str(template['enable_thinking']).lower()}")
    if kwargs.get("max_thinking_tokens") is not None:
        parts.append(f"max_thinking_tokens={kwargs['max_thinking_tokens']}")
    return ",".join(parts) or PROVIDER_DEFAULT


def harness_config(
    agent_name: str | None, kwargs: Mapping[str, Any], job_dir: Path
) -> dict[str, Any]:
    """Behaviour-relevant harness settings: agent kwargs minus sampling, plus prompt files."""
    config = {key: value for key, value in kwargs.items() if key not in _SAMPLING_KWARGS}
    llm = {
        key: value
        for key, value in _dict(config.pop("llm_call_kwargs", {})).items()
        if key not in _SAMPLING_LLM_KWARGS
    }
    body = {
        key: value
        for key, value in _dict(llm.pop("extra_body", {})).items()
        if key not in _THINKING_BODY_KEYS
    }
    llm.pop("chat_template_kwargs", None)
    if body:
        llm["extra_body"] = body
    if llm:
        config["llm_call_kwargs"] = llm
    if agent_name in _TERMINUS_AGENTS:
        for key, default in _TERMINUS_DEFAULTS.items():
            if key == "trajectory_config":
                config[key] = {**default, **_dict(config.get(key))}
            else:
                config.setdefault(key, default)
    tree = job_dir / "harness-tree"
    prompt_files = (
        {
            str(path.relative_to(tree)): _sha256(path.read_bytes())
            for path in sorted(tree.rglob("*"))
            if path.is_file() and path.name != "config.json"
        }
        if tree.is_dir()
        else {}
    )
    spec = _read_json(job_dir / "experiment-spec.json")
    for key in ("extra_instruction_sha256", "toolbox_sha256", "preamble_sha256"):
        if spec.get(key):
            prompt_files[key] = spec[key]
    if prompt_files:
        config["prompt_files"] = prompt_files
    return config


def collect_treatment(job_dir: Path, trial_dir: Path, sources: CommitSources) -> dict[str, Any]:
    """One ``trial_treatment.parquet`` row."""
    result = _read_json(trial_dir / "result.json")
    config = _read_json(trial_dir / "config.json")
    lock = _read_json(trial_dir / "lock.json")
    job_config = _read_json(job_dir / "config.json")
    lab = _read_json(job_dir / "lab-metadata.json")
    spec = _read_json(job_dir / "experiment-spec.json")
    t = _Treatment()

    staging = _dict(lab.get("task_staging"))
    t.set(
        "task_version_digest",
        staging.get("source_package_digest"),
        "lab-metadata.task_staging.source_package_digest",
    )
    result_config = result.get("config")
    t.set(
        "backend",
        trial_environment_type(
            config or None, result_config if isinstance(result_config, dict) else None, lock or None
        ),
        "trial config/lock environment",
    )
    agent_path, agent_name = _agent_identity(config, result)
    t.set("agent", agent_path, "trial config.json agent")
    harbor = _dict(lab.get("tools")).get("harbor")
    t.set("harbor_version", harbor, "lab-metadata.tools.harbor")

    repository = _dict(lab.get("repository"))
    commit = repository.get("commit") if repository.get("dirty") is False else None
    commit_note = (
        f"@{str(repository.get('commit'))[:12]}"
        if commit
        else "unknown: repository dirty or unrecorded at run"
    )

    model = _dict(config.get("agent")).get("model_name")
    kwargs = _agent_kwargs(config)
    llm = _dict(kwargs.get("llm_call_kwargs"))
    no_model = agent_name in NO_MODEL_AGENTS or (agent_path or "") in NO_MODEL_AGENTS or not model
    selfhosted = isinstance(model, str) and model.startswith(MIMO_SELFHOSTED_PREFIX)

    if no_model:
        for name in (
            "model",
            "model_revision",
            "serving_image",
            "serving_context_tokens",
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "thinking",
            "parser_digest",
        ):
            t.set(name, NOT_APPLICABLE, f"agent {agent_name or agent_path} calls no model")
    else:
        t.set("model", model, "trial config.json agent.model_name")
        _serving_fields(t, selfhosted, commit, commit_note, sources)
        _sampling_fields(t, kwargs, llm, selfhosted, lab, commit, commit_note, sources)
        _parser_field(t, selfhosted, commit, commit_note, sources)

    harness = harness_config(agent_name, kwargs, job_dir)
    t.set(
        "harness_config_digest",
        _sha256(_canonical(harness)),
        "agent kwargs minus sampling + harness-tree prompt files",
    )

    multiplier = config.get("agent_timeout_multiplier", job_config.get("agent_timeout_multiplier"))
    override = _dict(config.get("agent")).get("override_timeout_sec")
    task_timeout = _task_agent_timeout(trial_dir)
    multiplier = float(multiplier) if isinstance(multiplier, (int, float)) else 1.0
    t.set(
        "agent_timeout_multiplier",
        multiplier,
        "trial/job config.json agent_timeout_multiplier (Harbor default 1.0)",
    )
    t.set(
        "agent_timeout_override_seconds",
        float(override) if isinstance(override, (int, float)) else "none",
        "trial config.json agent.override_timeout_sec",
    )
    if isinstance(override, (int, float)):
        agent_timeout: float | None = float(override)
        timeout_source = "trial config.json agent.override_timeout_sec"
    elif isinstance(task_timeout, (int, float)):
        agent_timeout = round(float(task_timeout) * multiplier, 3)
        timeout_source = "task.toml agent.timeout_sec x agent_timeout_multiplier"
    else:
        agent_timeout = None
        timeout_source = "unknown: task.toml not resolvable"
    t.set("agent_timeout_seconds", agent_timeout, timeout_source)
    if no_model:
        # Control agents act instantly; the queue sizes their wall ceiling per
        # task so the verifier fits, so time limits are not a setup property.
        for name in (
            "agent_timeout_seconds",
            "agent_timeout_multiplier",
            "agent_timeout_override_seconds",
        ):
            t.set(name, NOT_APPLICABLE, f"agent {agent_name or agent_path} calls no model")
    for name in (
        "timeout_seconds",
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "cost_limit_usd",
    ):
        value = spec.get(name)
        if no_model and (name == "timeout_seconds" or value is None):
            t.set(name, NOT_APPLICABLE, f"agent {agent_name or agent_path} calls no model")
            continue
        t.set(
            name,
            float(value) if name == "cost_limit_usd" and value is not None else value,
            f"experiment-spec.json {name}",
        )

    values = {name: t.fields[name].value for name in KEY_FIELDS}
    unknown = [name for name in KEY_FIELDS if values[name] is None]
    return {
        "job_name": job_dir.name,
        "trial_name": trial_dir.name,
        "trial_dir": str(trial_dir),
        **values,
        "treatment_key": _sha256(_canonical({name: values[name] for name in KEY_FIELDS})),
        "setup_key": _sha256(_canonical({name: values[name] for name in SETUP_FIELDS})),
        "complete": not unknown,
        "unknown_fields": unknown,
        "harness_config": _canonical(harness),
        "field_sources": _canonical({name: t.fields[name].source for name in KEY_FIELDS}),
        "repository_commit": repository.get("commit"),
        "repository_dirty": repository.get("dirty"),
        "model_returned": _dict(lab.get("model_identity")).get("returned"),
        "schema": TREATMENT_SCHEMA,
    }


def _task_agent_timeout(trial_dir: Path) -> float | None:
    task_dir = task_dir_for_trial(trial_dir)
    if task_dir is None:
        return None
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = _dict(payload.get("agent")).get("timeout_sec")
    return float(value) if isinstance(value, (int, float)) else None


def _serving_fields(
    t: _Treatment, selfhosted: bool, commit: str | None, note: str, sources: CommitSources
) -> None:
    if not selfhosted:
        for name in ("model_revision", "serving_image", "serving_context_tokens"):
            t.set(name, None, "unknown: the route does not record serving pins")
        return
    text = sources.read(commit, SERVE_SOURCE)
    if not isinstance(text, str):
        reason = note if text is None else f"unknown: {SERVE_SOURCE} absent {note}"
        for name in ("model_revision", "serving_image", "serving_context_tokens"):
            t.set(name, None, reason)
        return
    constants = _constant_assignments(text)
    source = f"{SERVE_SOURCE} {note}"
    t.set("model_revision", constants.get("MODEL_REVISION"), source + " MODEL_REVISION")
    t.set("serving_image", constants.get("SGLANG_IMAGE"), source + " SGLANG_IMAGE")
    t.set("serving_context_tokens", constants.get("CONTEXT_LENGTH"), source + " CONTEXT_LENGTH")


def _sampling_fields(
    t: _Treatment,
    kwargs: Mapping[str, Any],
    llm: Mapping[str, Any],
    selfhosted: bool,
    lab: Mapping[str, Any],
    commit: str | None,
    note: str,
    sources: CommitSources,
) -> None:
    requested = {
        "temperature": kwargs.get("temperature", llm.get("temperature")),
        "top_p": llm.get("top_p"),
        "top_k": llm.get("top_k"),
    }
    max_tokens = llm.get("max_tokens")
    t.set(
        "max_tokens",
        max_tokens if max_tokens is not None else PROVIDER_DEFAULT,
        "agent kwargs llm_call_kwargs.max_tokens",
    )
    thinking = _requested_thinking(kwargs, llm)
    calls = [
        call
        for call in _dict(lab.get("provider_usage")).get("calls") or []
        if isinstance(call, dict)
    ]
    shaped = bool(calls) and all(call.get("shaping_applied") is True for call in calls)
    if not selfhosted or not shaped:
        reason = (
            "agent kwargs (no route shaping)"
            if calls or not selfhosted
            else "agent kwargs; proxy ledger missing"
        )
        for name, value in requested.items():
            t.set(name, value if value is not None else PROVIDER_DEFAULT, reason)
        t.set("thinking", thinking, reason)
        if selfhosted and not calls:
            for name in ("temperature", "top_p", "top_k", "thinking"):
                t.set(name, None, "unknown: MiMo route without a proxy ledger")
        return
    text = sources.read(commit, PROXY_SOURCE)
    if not isinstance(text, str):
        for name in ("temperature", "top_p", "top_k", "thinking"):
            t.set(name, None, f"unknown: proxy shaping code {note}")
        return
    forced = route_shaping(text)
    source = f"proxy shaping {PROXY_SOURCE} {note} (every ledger call shaping_applied)"
    for name, value in requested.items():
        if name in forced:
            t.set(name, forced[name], source)
        else:
            t.set(
                name, value if value is not None else PROVIDER_DEFAULT, "agent kwargs (not shaped)"
            )
    if "enable_thinking" in forced:
        t.set("thinking", f"enable_thinking={str(forced['enable_thinking']).lower()}", source)
    else:
        t.set("thinking", thinking, "agent kwargs (not shaped)")


def _parser_field(
    t: _Treatment, selfhosted: bool, commit: str | None, note: str, sources: CommitSources
) -> None:
    if not selfhosted:
        t.set("parser_digest", "none", "route has no tool-call normalizer")
        return
    text = sources.read(commit, PARSER_SOURCE)
    if text is None:
        t.set("parser_digest", None, f"unknown: {note}")
    elif isinstance(text, _Absent):
        t.set("parser_digest", "none", f"{PARSER_SOURCE} absent {note}")
    else:
        t.set("parser_digest", source_digest(text), f"{PARSER_SOURCE} {note}")


def _step_identity(step: Mapping[str, Any]) -> str:
    content = {key: value for key, value in step.items() if key != "metrics"}
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _trajectory_tokens(paths: Iterable[Path]) -> tuple[int, int, int, int]:
    """(prompt, completion, steps, steps without metrics) over unique steps."""
    prompt = completion = steps = unmetered = 0
    seen: set[str] = set()
    for path in paths:
        for step in _read_json(path).get("steps") or []:
            if not isinstance(step, dict):
                continue
            identity = _step_identity(step)
            if identity in seen:
                continue
            seen.add(identity)
            steps += 1
            metrics = _dict(step.get("metrics"))
            if step.get("source") == "agent" and not metrics:
                unmetered += 1
            prompt += int(metrics.get("prompt_tokens") or 0)
            completion += int(metrics.get("completion_tokens") or 0)
    return prompt, completion, steps, unmetered


def collect_capture(job_dir: Path, trial_dir: Path) -> dict[str, Any]:
    """One ``trial_capture.parquet`` row."""
    result = _read_json(trial_dir / "result.json")
    lab = _read_json(job_dir / "lab-metadata.json")
    agent_dir = trial_dir / "agent"
    files = sorted(agent_dir.glob("trajectory*.json")) if agent_dir.is_dir() else []
    head = agent_dir / "trajectory.json"
    continuations = sorted(
        (int(match.group(1)), path)
        for path in files
        if (match := _CONTINUATION_RE.match(path.name))
    )
    summaries = [path for path in files if _SUMMARIZATION_RE.match(path.name)]
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else None
    metadata = _dict((agent_result or {}).get("metadata"))
    summarization_count = metadata.get("summarization_count")
    present = [index for index, _ in continuations]
    missing = (
        sorted(set(range(1, int(summarization_count) + 1)) - set(present))
        if isinstance(summarization_count, int)
        else None
    )
    main = ([head] if head.is_file() else []) + [path for _, path in continuations]
    main_prompt, main_completion, main_steps, unmetered = _trajectory_tokens(main)
    summary_prompt, summary_completion, _, _ = _trajectory_tokens(summaries)
    has_trajectory = bool(main)
    usage = lab.get("provider_usage")
    usage = usage if isinstance(usage, dict) else None
    totals = _dict((usage or {}).get("totals"))
    calls = [call for call in (usage or {}).get("calls") or [] if isinstance(call, dict)]
    result_input = (agent_result or {}).get("n_input_tokens")
    result_output = (agent_result or {}).get("n_output_tokens")
    attributed_input = main_prompt + summary_prompt if has_trajectory else None
    attributed_output = main_completion + summary_completion if has_trajectory else None
    proxy_input = totals.get("input_tokens") if usage else None
    proxy_output = totals.get("output_tokens") if usage else None
    rollout = (agent_result or {}).get("rollout_details")
    exception = _dict(result.get("exception_info"))
    return {
        "job_name": job_dir.name,
        "trial_name": trial_dir.name,
        "result_json": bool(result),
        "exception_type": exception.get("exception_type"),
        "trajectory_head": head.is_file(),
        "continuation_indices": present,
        "continuation_count": len(present),
        "summarization_count": summarization_count
        if isinstance(summarization_count, int)
        else None,
        "continuations_missing": missing,
        "summarization_files": len(summaries),
        "n_episodes": metadata.get("n_episodes")
        if isinstance(metadata.get("n_episodes"), int)
        else None,
        "trajectory_steps": main_steps if has_trajectory else None,
        "trajectory_steps_unmetered": unmetered if has_trajectory else None,
        "recording_cast": (agent_dir / "recording.cast").is_file(),
        "verifier_stdout": (trial_dir / "verifier" / "test-stdout.txt").is_file(),
        "verifier_reward_file": any(
            (trial_dir / "verifier" / name).is_file() for name in ("reward.txt", "reward.json")
        ),
        "rollout_details": len(rollout) if isinstance(rollout, list) else None,
        "proxy_ledger": usage is not None,
        "proxy_calls": len(calls) if usage else None,
        "proxy_calls_shaped": sum(1 for call in calls if call.get("shaping_applied") is True)
        if usage
        else None,
        "proxy_unresolved_requests": usage.get("unresolved_requests") if usage else None,
        "result_input_tokens": result_input if isinstance(result_input, int) else None,
        "result_output_tokens": result_output if isinstance(result_output, int) else None,
        "trajectory_input_tokens": attributed_input,
        "trajectory_output_tokens": attributed_output,
        "proxy_input_tokens": proxy_input if isinstance(proxy_input, int) else None,
        "proxy_output_tokens": proxy_output if isinstance(proxy_output, int) else None,
        "input_tokens_unattributed": (
            proxy_input - attributed_input
            if isinstance(proxy_input, int) and attributed_input is not None
            else None
        ),
        "schema": TREATMENT_SCHEMA,
    }


def collect_jobs(
    jobs: Sequence[Path], *, repo_root: Path, produced_at: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Treatment and capture rows for every trial of the given job directories."""
    sources = CommitSources(repo_root)
    stamp = produced_at or datetime.now(UTC).isoformat()
    treatments: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    for job_dir in jobs:
        if not job_dir.is_dir():
            raise ValueError(f"job directory is missing: {job_dir}")
        for trial_dir in iter_trial_dirs(job_dir):
            treatments.append(
                {**collect_treatment(job_dir, trial_dir, sources), "produced_at": stamp}
            )
            captures.append({**collect_capture(job_dir, trial_dir), "produced_at": stamp})
    return treatments, captures


def treatment_schema() -> Any:
    import pyarrow as pa

    text = pa.string()
    columns: list[tuple[str, Any]] = [
        ("job_name", text),
        ("trial_name", text),
        ("trial_dir", text),
    ]
    # Mixed-type key fields (a number, "default" or "n/a") are stored as JSON text.
    columns += [(name, text) for name in KEY_FIELDS]
    columns += [
        ("treatment_key", text),
        ("setup_key", text),
        ("complete", pa.bool_()),
        ("unknown_fields", pa.list_(text)),
        ("harness_config", text),
        ("field_sources", text),
        ("repository_commit", text),
        ("repository_dirty", pa.bool_()),
        ("model_returned", text),
        ("schema", text),
        ("produced_at", text),
    ]
    return pa.schema(columns)


def capture_schema() -> Any:
    import pyarrow as pa

    integer = pa.int64()
    flag = pa.bool_()
    return pa.schema(
        [
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("result_json", flag),
            ("exception_type", pa.string()),
            ("trajectory_head", flag),
            ("continuation_indices", pa.list_(integer)),
            ("continuation_count", integer),
            ("summarization_count", integer),
            ("continuations_missing", pa.list_(integer)),
            ("summarization_files", integer),
            ("n_episodes", integer),
            ("trajectory_steps", integer),
            ("trajectory_steps_unmetered", integer),
            ("recording_cast", flag),
            ("verifier_stdout", flag),
            ("verifier_reward_file", flag),
            ("rollout_details", integer),
            ("proxy_ledger", flag),
            ("proxy_calls", integer),
            ("proxy_calls_shaped", integer),
            ("proxy_unresolved_requests", integer),
            ("result_input_tokens", integer),
            ("result_output_tokens", integer),
            ("trajectory_input_tokens", integer),
            ("trajectory_output_tokens", integer),
            ("proxy_input_tokens", integer),
            ("proxy_output_tokens", integer),
            ("input_tokens_unattributed", integer),
            ("schema", pa.string()),
            ("produced_at", pa.string()),
        ]
    )


def _stored(row: Mapping[str, Any]) -> dict[str, Any]:
    """Key-field values as JSON text so numbers and sentinels share a column."""
    return {
        **row,
        **{name: None if row[name] is None else _canonical(row[name]) for name in KEY_FIELDS},
    }


def _loaded(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **row,
        **{name: None if row[name] is None else json.loads(row[name]) for name in KEY_FIELDS},
    }


def _write(rows: Sequence[Mapping[str, Any]], schema: Any, path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({f.name: [row.get(f.name) for row in rows] for f in schema}, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _read(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist() if path.is_file() else []


def upsert_tables(
    treatments: Sequence[Mapping[str, Any]],
    captures: Sequence[Mapping[str, Any]],
    catalog: Path,
) -> tuple[Path, Path, int, int]:
    """Merge rows into both tables, replacing trials collected before.

    Trials are identified by ``(job_name, trial_name)``. Returns the two paths
    and the resulting row counts.
    """
    out: list[Any] = []
    for rows, filename, schema, encode in (
        (treatments, TREATMENT_TABLE_FILENAME, treatment_schema(), _stored),
        (captures, CAPTURE_TABLE_FILENAME, capture_schema(), dict),
    ):
        path = catalog / filename
        merged = {(row["job_name"], row["trial_name"]): row for row in _read(path)}
        merged.update({(row["job_name"], row["trial_name"]): encode(row) for row in rows})
        ordered = [merged[key] for key in sorted(merged)]
        _write(ordered, schema, path)
        out.append((path, len(ordered)))
    return out[0][0], out[1][0], out[0][1], out[1][1]


def read_treatments(catalog: Path) -> list[dict[str, Any]]:
    return [_loaded(row) for row in _read(catalog / TREATMENT_TABLE_FILENAME)]


@dataclass(frozen=True)
class PoolCheck:
    ok: bool
    trials: int
    tasks: int
    setup_keys: list[str]
    differing: dict[str, dict[str, list[str]]]
    unknown: dict[str, list[str]]

    def render(self) -> str:
        verdict = "POOLABLE" if self.ok else "REFUSED"
        lines = [
            f"{verdict}: {self.trials} trials, {self.tasks} task versions, "
            f"{len(self.setup_keys)} setup key(s)"
        ]
        for key in self.setup_keys:
            lines.append(f"  setup_key {key}")
        for name, values in self.differing.items():
            lines.append(f"  differs: {name}")
            for value, trials in values.items():
                lines.append(f"    {value}: {', '.join(trials)}")
        for name, trials in self.unknown.items():
            lines.append(f"  unknown: {name} in {len(trials)} trial(s)")
        return "\n".join(lines)


def pool_check(
    rows: Sequence[Mapping[str, Any]], *, same_task: bool = False, accept_unknown: bool = False
) -> PoolCheck:
    """Refuse to pool trials whose setups differ, or that leave a field unknown.

    Only :data:`SETUP_FIELDS` are compared unless ``same_task``, which also
    requires one task version (:data:`TASK_FIELDS`). ``accept_unknown`` treats unknown values as
    equal to each other (still reported); a known value never equals unknown.
    """
    if not rows:
        raise ValueError("no trials to check")
    fields = KEY_FIELDS if same_task else SETUP_FIELDS
    label = [f"{row['job_name']}/{row['trial_name']}" for row in rows]
    differing: dict[str, dict[str, list[str]]] = {}
    unknown: dict[str, list[str]] = {}
    for name in fields:
        by_value: dict[str, list[str]] = {}
        for row, trial in zip(rows, label, strict=True):
            value = row[name]
            by_value.setdefault("unknown" if value is None else _canonical(value), []).append(trial)
            if value is None:
                unknown.setdefault(name, []).append(trial)
        if len(by_value) > 1:
            differing[name] = by_value
    ok = not differing and (accept_unknown or not unknown)
    return PoolCheck(
        ok=ok,
        trials=len(rows),
        tasks=len({row["task_version_digest"] for row in rows}),
        setup_keys=sorted({str(row["setup_key"]) for row in rows}),
        differing=differing,
        unknown=unknown,
    )
