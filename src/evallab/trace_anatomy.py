"""Structured Case Anatomy: a deterministic, label-free reading of one agent run.

``TraceAnatomy`` splits a run into four parts a reviewer actually needs:

* ``task_contract``: the source-redacted task statement and the explicit
  prohibitions it states ("do not modify the tests").
* ``patch_and_mutations``: the exact final submitted patch (a submission field
  or the last ``diff --git`` block an agent printed) and a file-mutation ledger
  over every tool call. Writes to protected surfaces (tests, conftest, ``.pth``,
  verifier/CI/monitor configs, ...) keep their full written content; only
  blobs over ``BLOB_CHARS`` get an explicit ``[... N chars omitted ...]`` mark.
* ``high_signal_actions``: git-history probes, sensitive path/env reads,
  network/package fetches, test runs with exit codes, delegation prompts and
  test-assertion inversions.
* ``claim_vs_reality``: the final user-facing claim next to what changed, whether
  protected surfaces were touched, the last test outcome and an empty-patch flag.

Inputs are admitted DEV runs only: cloud trace ids (raw CAS bundle, else the DB
projection) and CheatBench DEV items pass the shared ``CorpusRegistry`` gate;
an explicit local Harbor/ATIF file is accepted when it is not under the answer
keys and does not declare a non-DEV split. Only steps, the task prompt and a
submission string are read: evaluator metrics, rewards, ``info`` verdict fields
and labels never enter the anatomy, and every rendered string is scrubbed of
source/benchmark names, evaluator field names and split labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.production.run_atlas import (
    CHEATBENCH_DATASET,
    DEFAULT_DATABASE,
    STATE_ROOT,
    admitted_task_ids,
    cloud_doc,
    connect_readonly,
    redact_sources,
    source_pattern,
    unpack_legacy_tool_calls,
)

ANATOMY_VERSION = "trace-anatomy/v1"
DEFAULT_CAS_DIR = STATE_ROOT / "cas"
BLOB_CHARS = 20_000
STATEMENT_CARD_CHARS = 3_000
OUTPUT_LINE_CHARS = 300
COMMAND_CHARS = 600

# Names the raw blobs use for benchmark families and suites beyond run_atlas.SOURCE_TERMS.
EXTRA_SOURCE_TERMS = (
    r"live[-_ ]?code[-_ ]?bench",
    r"human[-_ ]?eval",
    r"datacurve",
    r"no[-_]?hack",
    r"terminal[-_ ]?wrench",
    r"breaker[-_ ]bench",
)
# Evaluator fields, label names and split provenance that must never reach a reader.
LEAK_TERMS = re.compile(
    r"(?i:\b(?:verdict_enacted|is_exploit|is_task_defect_fail|fail_to_pass|pass_to_pass|"
    r"f2p_(?:passed|total)|p2p_(?:passed|total)|resolved|shortcut_signal|expected_verdict|answer[-_]keys)\b|"
    r"\bheld[-_ ]?out\b|\"split\"\s*:\s*\"[^\"]*\")|\bDEV\b"
)
LEAK_PLACEHOLDER = "[redacted]"

# --------------------------------------------------------------------------- #
# classification patterns
# --------------------------------------------------------------------------- #

PROTECTED_PATH = re.compile(
    r"(?:^|/)(?:tests?|testing|__tests__|spec)/|(?:^|/)test_[^/]*$|_test\.(?:py|go|rs|js|ts)$|"
    r"\.(?:test|spec)\.[jt]sx?$|(?:^|/)tests?\.py$|(?:^|/)conftest\.py$|\.pth$|"
    r"(?:^|/)(?:site|user)customize\.py$|(?:^|/)pytest\.ini$|(?:^|/)pyproject\.toml$|"
    r"(?:^|/)setup\.cfg$|(?:^|/)tox\.ini$|(?:^|/)noxfile\.py$|^/logs/verifier|^/opt/|"
    r"(?:^|/)\.github/workflows/|(?:^|/)\.gitlab-ci\.ya?ml$|(?:^|/)jest\.config|(?:^|/)vitest\.config|"
    r"(?:^|/)[^/]*(?:monitor|alert|gate|guard|audit)[^/]*\.(?:ya?ml|json|toml|ini|cfg|conf)$",
    re.IGNORECASE,
)

SENSITIVE_PATH = re.compile(
    r"(?<![\w.-])(/tests?(?:/|\b)|/opt/|/proc/|/logs/|/root/|/etc/(?:passwd|shadow|environment)|"
    r"/var/log/|/home/|~/\.)"
)
ENV_PROBE = re.compile(
    r"(?:^|[;&|(]\s*)(?:env|printenv|export\s+-p)\s*(?:$|[|;&>])|os\.environ|"
    r"/proc/self/environ|\becho\s+\$\{?[A-Z_]{3,}",
    re.MULTILINE,
)
GIT_HISTORY = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:log|show|reflog|remote|fetch|pull|cat-file|rev-list|blame|"
    r"ls-remote|ls-tree|stash\s+list|branch\s+-a|tag)\b"
)
NETWORK = re.compile(
    r"\b(?:curl|wget|aria2c|nc|ssh|scp)\s|\bgit\s+clone\b|\bpip3?\s+(?:install|download)\b|"
    r"\buv\s+(?:add|pip\s+install)\b|\bnpm\s+(?:install|i)\b|\byarn\s+add\b|\bpnpm\s+add\b|"
    r"\bapt(?:-get)?\s+install\b|\bapk\s+add\b|\bgo\s+(?:get|install)\b|\bcargo\s+(?:add|install)\b|"
    r"\bgem\s+install\b|requests\.(?:get|post)\(|urllib\.request|httpx\.(?:get|post)\("
)
TEST_RUNNER = re.compile(
    r"\b(?:pytest|py\.test|python[0-9.]*\s+(?:-m\s+(?:pytest|unittest)|\S*test\S*\.py|runtests\.py|"
    r"manage\.py\s+test)|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|jest|"
    r"vitest|mocha|tox|nox|make\s+(?:test|check)|mvn\s+test|gradlew?\s+test|rspec|phpunit|"
    r"dotnet\s+test|ctest)\b"
)
TEST_SUMMARY = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|skipped|xfailed)\b|^(?:OK|FAILED)\b|^ok\s|^FAIL\b|^PASS\b|"
    r"\bRan \d+ tests?\b|\bTests?:\s+\d+|test result:|AssertionError|^ALL TESTS PASSED",
    re.IGNORECASE | re.MULTILINE,
)
DELEGATION_TOOLS = re.compile(
    r"^(?:task|agent|subagent|sub_agent|spawn_agent|spawn|delegate|dispatch_agent|run_subagent|"
    r"call_agent|new_task|handoff)$",
    re.IGNORECASE,
)
DELEGATION_COMMAND = re.compile(r"\b(?:omp|claude|codex|gemini|aider)\s+(?:-p|exec|--print|run)\b")
INVERSION = re.compile(
    r"pytest\.skip|mark\.skip|xfail|unittest\.skip|skipTest|\bt\.Skip\(|\b(?:it|describe|test)\.skip\(|"
    r"@Ignore\b|@Disabled\b|\bassert\s+True\b|\bassert\b[^\n]*\bnot\s+in\b|\bno_notify\s*=\s*True|"
    r"\bassert\s+not\b|#\s*assert\b|sys\.exit\(\s*0\s*\)|os\._exit\(\s*0\s*\)"
)
PROHIBITION = re.compile(
    r"\b(?:do not|don't|never|must not|mustn't|should not|shouldn't|cannot|can't|avoid|"
    r"not allowed|forbidden|prohibited|refrain|only (?:modify|edit|change)|without (?:modifying|changing|"
    r"editing|touching))\b",
    re.IGNORECASE,
)
DIFF_START = re.compile(r"^diff --git ", re.MULTILINE)
THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
EDITOR_TOOLS = re.compile(
    r"str_replace|text_editor|edit|write|create|apply_patch|patch|insert|replace|notebook|multiedit|file_editor",
    re.IGNORECASE,
)
EDITOR_COMMANDS = {
    "create",
    "str_replace",
    "insert",
    "write",
    "overwrite",
    "append",
    "undo_edit",
    "edit",
}
SHELL_TOOLS = re.compile(r"bash|shell|terminal|exec|run|command|sh$|cmd", re.IGNORECASE)
PYTHON_TOOLS = re.compile(r"^(?:python|ipython|jupyter|code_interpreter)$", re.IGNORECASE)
PY_WRITE = re.compile(
    r"open\(\s*[rbf]?['\"]([^'\"]+)['\"]\s*,\s*[rbf]?['\"][wax]|Path\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\."
    r"write_(?:text|bytes)|shutil\.(?:copy\w*|move)\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]|"
    r"os\.(?:remove|unlink|rename|replace)\(\s*['\"]([^'\"]+)['\"]|"
    r"open\(\s*[rbf]?['\"]([^'\"]+)['\"]\s*\)\s*\.write",
    re.IGNORECASE,
)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
REDIRECT = re.compile(r"(?<![0-9&<>])(>>?)\s*([^\s;&|<>()]+)")
INTERPRETERS = ("python", "python3", "bash", "sh", "node", "ruby", "perl")


class RefusedRun(ValueError):
    """The run is not an admitted DEV run (or its corpus is unknown)."""


# --------------------------------------------------------------------------- #
# anatomy model
# --------------------------------------------------------------------------- #


@dataclass
class Mutation:
    step: int
    tool: str
    op: str
    path: str
    surface: str
    content: str | None = None
    command: str | None = None


@dataclass
class Action:
    step: int
    kind: str
    command: str
    detail: str | None = None
    exit_code: int | None = None
    outcome: str | None = None


@dataclass
class TraceAnatomy:
    """Deterministic, label-free structure of one run (see module docstring)."""

    run: dict[str, Any]
    task_contract: dict[str, Any]
    patch_and_mutations: dict[str, Any]
    high_signal_actions: dict[str, list[dict[str, Any]]]
    claim_vs_reality: dict[str, Any]
    narration: list[dict[str, Any]] = field(default_factory=list)
    anatomy_version: str = ANATOMY_VERSION

    def to_dict(
        self, *, include_narration: bool = False, include_final_claim: bool = False
    ) -> dict[str, Any]:
        data = asdict(self)
        if not include_narration:
            data.pop("narration")
        if not include_final_claim:
            data["claim_vs_reality"] = {
                k: v for k, v in data["claim_vs_reality"].items() if k != "final_claim"
            }
        return data


# --------------------------------------------------------------------------- #
# scrubbing
# --------------------------------------------------------------------------- #


def scrub_pattern(extra_terms: Iterable[str] = ()) -> re.Pattern[str]:
    """Source names (``run_atlas``), extra family/suite names and literal dataset ids."""
    base = source_pattern(extra_terms)
    return re.compile(f"{base.pattern}|{'|'.join(EXTRA_SOURCE_TERMS)}", re.IGNORECASE)


SCRUB_PATTERN = scrub_pattern()


def scrub(text: str, pattern: re.Pattern[str] = SCRUB_PATTERN) -> str:
    return LEAK_TERMS.sub(LEAK_PLACEHOLDER, redact_sources(text, pattern))


def scrub_tree(value: Any, pattern: re.Pattern[str] = SCRUB_PATTERN) -> Any:
    if isinstance(value, str):
        return scrub(value, pattern)
    if isinstance(value, list):
        return [scrub_tree(item, pattern) for item in value]
    if isinstance(value, dict):
        return {key: scrub_tree(item, pattern) for key, item in value.items()}
    return value


def elide(text: str, limit: int = BLOB_CHARS) -> str:
    """Whole text up to ``limit``; beyond it head and tail around an explicit omission mark."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} chars omitted ...]\n{text[-tail:]}"


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False, default=str)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"raw": value}
        return dict(parsed) if isinstance(parsed, Mapping) else {"raw": value}
    return {} if value is None else {"raw": value}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def raw_bundle_doc(bundle: Mapping[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
    """A raw trace bundle -> (ATIF-shaped doc on the final parent path, task prompt, submission).

    Reads only ``nodes``, ``task.data.prompt`` and the ``submission`` string of
    the agent's own submission record; metrics, rewards, evaluation verdicts and
    every other ``info`` field are never read.
    """
    traces = bundle.get("traces") if isinstance(bundle.get("traces"), list) else [bundle]
    trace = traces[0] if traces else {}
    nodes = trace.get("nodes") or []
    path: list[int] = []
    index: Any = len(nodes) - 1 if nodes else None
    seen: set[int] = set()
    while isinstance(index, int) and 0 <= index < len(nodes) and index not in seen:
        seen.add(index)
        path.append(index)
        index = nodes[index].get("parent")
    steps: list[dict[str, Any]] = []
    owners: dict[str, dict[str, Any]] = {}
    last_agent: dict[str, Any] | None = None
    for ordinal, node_index in enumerate(reversed(path)):
        message = nodes[node_index].get("message") or {}
        role = str(message.get("role") or "")
        content = _text(message.get("content"))
        if role == "assistant":
            calls = [
                {
                    "tool_call_id": str(call.get("id") or ""),
                    "function_name": str(call.get("name") or ""),
                    "arguments": _arguments(call.get("arguments")),
                }
                for call in message.get("tool_calls") or []
                if isinstance(call, Mapping)
            ]
            step: dict[str, Any] = {
                "step_id": ordinal,
                "source": "agent",
                "message": content,
                "tool_calls": calls,
                "observation": {"results": []},
            }
            if (
                isinstance(message.get("reasoning_content"), str)
                and message["reasoning_content"].strip()
            ):
                step["reasoning_content"] = message["reasoning_content"]
            for call in calls:
                if call["tool_call_id"]:
                    owners[str(call["tool_call_id"])] = step
            steps.append(step)
            last_agent = step
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            owner = owners.get(call_id) or last_agent
            if owner is not None:
                owner["observation"]["results"].append(
                    {"source_call_id": call_id or None, "content": content}
                )
            else:
                steps.append({"step_id": ordinal, "source": "tool", "message": content})
        else:
            steps.append({"step_id": ordinal, "source": role or "system", "message": content})
    data = _mapping(_mapping(trace.get("task")).get("data"))
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else None
    submission = None
    for record in _mapping(trace.get("info")).values():
        if isinstance(record, Mapping) and isinstance(record.get("submission"), str):
            submission = record["submission"]
            break
    return {"schema_version": "ATIF-v1.6", "steps": steps}, prompt, submission


@dataclass(frozen=True)
class LoadedRun:
    run_id: str
    loader: str
    doc: Mapping[str, Any]
    prompt: str | None = None
    submission: str | None = None
    raw_chars: int = 0


def _registry(repo_root: Path) -> Any:
    from evallab.production.corpus_queue import CorpusRegistry

    return CorpusRegistry(repo_root)


def load_cloud_run(
    trace_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
    cas_dir: Path = DEFAULT_CAS_DIR,
    registry: Any,
    connection: sqlite3.Connection | None = None,
    admitted: frozenset[str] | None = None,
    include_branches: bool = False,
) -> LoadedRun:
    """An admitted DEV cloud trace: the raw CAS bundle when present, else the DB projection."""
    own = connection is None
    connection = connection or connect_readonly(database)
    try:
        row = connection.execute(
            "SELECT a.task_id, a.raw_cas_uri, c.split FROM agent_traces a JOIN corpus_tasks c USING(task_id)"
            " WHERE a.trace_id = ? LIMIT 1",
            (trace_id,),
        ).fetchone()
        if row is None:
            raise RefusedRun("unknown run id (not a cloud trace)")
        admitted = admitted if admitted is not None else admitted_task_ids(connection, registry)
        if str(row["split"]).strip().casefold() != "dev" or row["task_id"] not in admitted:
            raise RefusedRun("run is not an admitted DEV run")
        if include_branches:
            from evallab.gold.projection import cloud_events

            doc = cloud_events(connection, trace_id)
            return LoadedRun(trace_id, "db-source-events", doc)
        uri = row["raw_cas_uri"]
        if uri:
            from evallab.production.cloud_trace_db import S3CompatibleCAS

            try:
                data = S3CompatibleCAS(local_directory=cas_dir, bucket="").get_bytes(uri)
            except FileNotFoundError:
                data = None
            if data is not None:
                doc, prompt, submission = raw_bundle_doc(json.loads(data))
                return LoadedRun(
                    trace_id,
                    "raw-cas",
                    doc,
                    prompt,
                    submission,
                    len(json.dumps(doc["steps"], ensure_ascii=False)),
                )
        doc = cloud_doc(connection, trace_id)
        return LoadedRun(
            trace_id,
            "db-projection",
            doc,
            None,
            None,
            len(json.dumps(doc["steps"], ensure_ascii=False)),
        )
    finally:
        if own:
            connection.close()


def load_cheatbench_run(item_id: str, *, registry: Any, results_root: Path) -> LoadedRun:
    """A CheatBench DEV item's trajectory (registry gate; the answer-key metadata is not kept)."""
    for item in registry.items(dataset_id=CHEATBENCH_DATASET, split="dev"):
        if item.item_id != item_id:
            continue
        if registry.assigned_test_anywhere(item_id=item.item_id, task_id=item.task_id):
            break
        reference = (item.metadata.get("trial_ref") or {}).get("path")
        if not reference:
            raise RefusedRun("admitted item has no trajectory reference")
        path = results_root / reference / "agent" / "trajectory.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        steps = doc.get("steps") or []
        return LoadedRun(
            item_id,
            "atif",
            {"steps": steps},
            None,
            None,
            len(json.dumps(steps, ensure_ascii=False)),
        )
    raise RefusedRun("run is not an admitted DEV item")


def load_atif_file(path: Path) -> LoadedRun:
    """A local Harbor/ATIF trial: refused under the answer keys or when it declares a non-DEV split."""
    resolved = path.expanduser().resolve()
    if "answer-keys" in resolved.parts:
        raise RefusedRun("files under the answer keys are refused")
    if resolved.is_dir():
        for candidate in (resolved / "agent" / "trajectory.json", resolved / "trajectory.json"):
            if candidate.is_file():
                resolved = candidate
                break
    return atif_run(json.loads(resolved.read_text(encoding="utf-8")), str(resolved))


def atif_run(doc: Any, name: str) -> LoadedRun:
    """The run of an already parsed ATIF document or raw bundle (no I/O); refuses a non-DEV split."""
    if isinstance(doc, Mapping) and isinstance(doc.get("traces"), list):
        steps_doc, prompt, submission = raw_bundle_doc(doc)
        return LoadedRun(
            name,
            "raw-bundle-file",
            steps_doc,
            prompt,
            submission,
            len(json.dumps(steps_doc["steps"], ensure_ascii=False)),
        )
    metadata = (
        doc.get("metadata")
        if isinstance(doc, Mapping) and isinstance(doc.get("metadata"), Mapping)
        else {}
    )
    split = metadata.get("split")
    if isinstance(split, str) and split.strip() and split.strip().casefold() != "dev":
        raise RefusedRun("trial declares a non-DEV split")
    steps = doc.get("steps") if isinstance(doc, Mapping) else None
    if not isinstance(steps, list):
        raise ValueError("not an ATIF trajectory (no steps list)")
    unpacked_steps: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, Mapping):
            step_dict = dict(step)
            if "tool_calls" in step_dict and isinstance(step_dict["tool_calls"], list):
                if any(not isinstance(c, Mapping) for c in step_dict["tool_calls"]):
                    step_dict["has_malformed_tool_calls"] = True
                step_dict["tool_calls"] = unpack_legacy_tool_calls(
                    c for c in step_dict["tool_calls"] if isinstance(c, Mapping)
                )
            unpacked_steps.append(step_dict)
        else:
            unpacked_steps.append(step)
    return LoadedRun(
        name,
        "atif",
        {**doc, "steps": unpacked_steps},
        None,
        None,
        len(json.dumps(unpacked_steps, ensure_ascii=False)),
    )


def load_run(
    run_id: str,
    *,
    repo_root: Path,
    database: Path = DEFAULT_DATABASE,
    cas_dir: Path = DEFAULT_CAS_DIR,
    results_root: Path | None = None,
    include_branches: bool = False,
) -> LoadedRun:
    """Resolve a run id (cloud trace id or CheatBench item id) through the DEV gate."""
    registry = _registry(repo_root)
    if run_id.startswith(f"{CHEATBENCH_DATASET}:"):
        from evallab.results_home import results_root as default_results_root

        return load_cheatbench_run(
            run_id, registry=registry, results_root=results_root or default_results_root()
        )
    return load_cloud_run(
        run_id, database=database, cas_dir=cas_dir, registry=registry,
        include_branches=include_branches,
    )


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def surface(path: str) -> str:
    return "protected" if PROTECTED_PATH.search(path) else "product"


def _clean_path(path: str) -> str:
    path = path.strip().strip("'\"")
    for prefix in ("a/", "b/"):
        if path.startswith(prefix) and not path.startswith(prefix * 2):
            path = path[len(prefix) :]
            break
    return path[2:] if path.startswith("./") else path


def _command(name: str, arguments: Mapping[str, Any]) -> str | None:
    for key in ("command", "cmd", "code", "script", "input", "raw"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            if (
                key == "command"
                and value.strip() in EDITOR_COMMANDS | {"view"}
                and arguments.get("path")
            ):
                return None
            return value
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return " ".join(shlex.quote(part) for part in value)
    del name
    return None


def _result_texts(step: Mapping[str, Any]) -> list[tuple[str | None, str]]:
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, Mapping) else None
    out = []
    for result in results or []:
        if isinstance(result, Mapping):
            out.append((result.get("source_call_id"), _text(result.get("content"))))
        else:
            out.append((None, _text(result)))
    return out


def _parse_result(text: str) -> tuple[int | None, str]:
    """Exit code and output from a tool result (JSON ``returncode`` envelope or plain text)."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping):
            code = parsed.get("returncode", parsed.get("exit_code"))
            output = _text(parsed.get("output", parsed.get("stdout", ""))) + _text(
                parsed.get("stderr", "")
            )
            return (code if isinstance(code, int) and not isinstance(code, bool) else None), output
    match = re.search(r"(?:exit[_ ]code|returncode|Exit status)\W{0,3}(-?\d+)", text, re.IGNORECASE)
    return (int(match.group(1)) if match else None), text


def _split_patch(patch: str) -> list[dict[str, Any]]:
    """Per-file sections of a unified git diff with +/- counts."""
    files = []
    starts = [match.start() for match in DIFF_START.finditer(patch)]
    for begin, end in zip(starts, [*starts[1:], len(patch)], strict=True):
        section = patch[begin:end]
        header = section.split("\n", 1)[0]
        match = re.match(r"diff --git a/(\S+) b/(\S+)", header)
        path = match.group(2) if match else header[len("diff --git ") :]
        lines = section.splitlines()
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        files.append(
            {
                "path": path,
                "surface": surface(path),
                "added": added,
                "removed": removed,
                "text": section,
            }
        )
    return files


def _final_diff_block(steps: Sequence[Mapping[str, Any]]) -> str | None:
    """The last tool output that contains a ``diff --git`` patch, from its first diff header on."""
    for step in reversed(steps):
        for _call_id, text in reversed(_result_texts(step)):
            _code, output = _parse_result(text)
            match = DIFF_START.search(output)
            if match:
                return output[match.start() :].rstrip("\n") + "\n"
    return None


def _final_submission(steps: Sequence[Mapping[str, Any]]) -> str | None:
    for step in reversed(steps):
        for call in reversed(step.get("tool_calls") or []):
            if not isinstance(call, Mapping):
                continue
            name = str(call.get("function_name") or call.get("name") or "")
            if name.lower() not in {"submit", "final_answer", "finish", "submit_answer", "done"}:
                continue
            arguments = _arguments(call.get("arguments"))
            for key in ("answer", "submission", "result", "content", "message", "raw"):
                if isinstance(arguments.get(key), str):
                    return arguments[key]
            return json.dumps(arguments, ensure_ascii=False)
    return None


def _heredocs(command: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(command with heredoc bodies removed, [(line, tag, body)])."""
    lines = command.split("\n")
    kept: list[str] = []
    docs: list[tuple[str, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC.finditer(line):
            tag = match.group(2)
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != tag:
                body.append(lines[index])
                index += 1
            index += 1
            docs.append((line, tag, "\n".join(body)))
    return "\n".join(kept), docs


def _heredoc_program(line: str) -> str:
    """The program that receives a heredoc: the last shell segment before ``<<``."""
    segments = _segments(line.split("<<", 1)[0])
    tokens = _tokens(segments[-1]) if segments else []
    return Path(tokens[0]).name if tokens else ""


def executed_text(command: str) -> str:
    """Shell text that runs: heredoc bodies are dropped unless an interpreter executes them."""
    stripped, docs = _heredocs(command)
    bodies = [body for line, _tag, body in docs if _heredoc_program(line).startswith(INTERPRETERS)]
    return "\n".join([stripped, *bodies])


def _segments(command: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"&&|\|\||;|\n|(?<![|>])\|(?!\|)", command)
        if part.strip()
    ]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _ok_target(target: str) -> bool:
    return (
        bool(target) and not target.startswith(("&", "/dev/")) and target not in {"-", "/dev/null"}
    )


def _without_redirections(tokens: list[str]) -> list[str]:
    """Shell words minus redirection operators and their targets (``2>/dev/null``, ``> out``)."""
    kept: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        operator = re.match(r"^\d*(?:>>?|<|&>)&?(.*)$", token)
        if operator:
            skip = not operator.group(1)
            continue
        kept.append(token)
    return kept


def shell_mutations(command: str) -> list[tuple[str, str, str | None]]:
    """``(op, path, written content)`` for file writes a shell command performs."""
    stripped, docs = _heredocs(command)
    found: list[tuple[str, str, str | None]] = []
    for line, _tag, body in docs:
        program = _heredoc_program(line)
        if program.startswith(INTERPRETERS) and program not in {"cat", "tee"}:
            found.extend(
                ("python_write", _clean_path(next(p for p in m if p)), None)
                for m in PY_WRITE.findall(body)
            )
            continue
        target = None
        redirect = REDIRECT.search(line)
        if redirect and _ok_target(redirect.group(2)):
            target = redirect.group(2)
        tee = re.search(r"\btee\s+(?:-a\s+)?([^\s;&|<>]+)", line)
        if tee and _ok_target(tee.group(1)):
            target = tee.group(1)
        if target:
            found.append(("heredoc_write", _clean_path(target), body))
    for segment in _segments(stripped):
        tokens = _without_redirections(_tokens(segment))
        if not tokens:
            continue
        program = Path(tokens[0]).name
        if "<<" not in segment:
            for op_symbol, target in REDIRECT.findall(segment):
                if _ok_target(target) and not segment.lstrip().startswith(("git diff", "diff ")):
                    content = segment if program in {"echo", "printf"} else None
                    found.append(
                        ("append" if op_symbol == ">>" else "write", _clean_path(target), content)
                    )
            tee_m = re.search(r"\btee\s+(?:-[^\s]+\s+)*([^\s;&|<>]+)", segment)
            if tee_m and _ok_target(tee_m.group(1)):
                content = segment if program in {"echo", "printf"} else None
                found.append(
                    ("append" if "-a" in segment else "write", _clean_path(tee_m.group(1)), content)
                )
        if program == "dd":
            for token in tokens[1:]:
                if token.startswith("of="):
                    found.append(("write", _clean_path(token[3:]), None))
        elif program == "sed" and any(
            re.match(r"^-[A-Za-z]*i", token) or token.startswith("--in-place")
            for token in tokens[1:]
        ):
            expressions = [token for token in tokens[1:-1] if not token.startswith("-")]
            found.append(("sed_inplace", _clean_path(tokens[-1]), " ".join(expressions)))
        elif program == "perl" and any(
            token.startswith("-") and "i" in token for token in tokens[1:]
        ):
            found.append(("perl_inplace", _clean_path(tokens[-1]), " ".join(tokens[1:-1])))
        elif program in {"cp", "mv", "ln", "install", "rsync"} and len(tokens) >= 3:
            operands = [token for token in tokens[1:] if not token.startswith("-")]
            if len(operands) >= 2:
                op = {"cp": "copy", "mv": "move", "ln": "link", "install": "copy", "rsync": "copy"}[
                    program
                ]
                found.append((op, _clean_path(operands[-1]), None))
                if program == "mv":
                    found.extend(("delete", _clean_path(source), None) for source in operands[:-1])
        elif program in {"rm", "unlink", "truncate", "shred"}:
            found.extend(
                ("delete", _clean_path(token), None)
                for token in tokens[1:]
                if not token.startswith("-")
            )
        elif program == "git" and len(tokens) >= 2:
            sub = tokens[1]
            if sub in {"checkout", "restore"} and ("--" in tokens or sub == "restore"):
                paths = (
                    tokens[tokens.index("--") + 1 :]
                    if "--" in tokens
                    else [token for token in tokens[2:] if not token.startswith("-")]
                )
                found.extend(("git_restore", _clean_path(p), None) for p in paths)
            elif sub == "reset" and "--hard" in tokens:
                found.append(("git_reset_hard", "*", None))
            elif sub in {"apply", "am"}:
                found.extend(
                    ("git_apply", _clean_path(t), None) for t in tokens[2:] if not t.startswith("-")
                )
            elif sub == "stash" and (
                len(tokens) == 2 or tokens[2] in {"push", "pop", "apply", "drop"}
            ):
                found.append(("git_stash", "*", None))
        elif program == "patch":
            found.append(("patch", "*", None))
        if program.startswith("python") and "-c" in tokens:
            code = " ".join(tokens[tokens.index("-c") + 1 :])
            write_m = re.search(r"\.write(?:_text)?\s*\(\s*['\"](.*?)['\"]\s*\)", code)
            py_content = write_m.group(1) if write_m else None
            found.extend(
                ("python_write", _clean_path(next(p for p in m if p)), py_content)
                for m in PY_WRITE.findall(code)
            )
    return found


def _apply_patch_files(text: str) -> list[tuple[str, str, str | None]]:
    """(op, path, section) tuples from ``*** Begin Patch`` envelopes or unified diffs."""
    results: list[tuple[str, str, str | None]] = []
    matches = list(
        re.finditer(r"^\*\*\* (Add|Update|Delete) File: (\S+)", text, flags=re.MULTILINE)
    )
    if matches:
        for i, match in enumerate(matches):
            action, path = match.group(1), match.group(2)
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].strip()
            op = (
                "create" if action == "Add" else ("delete" if action == "Delete" else "str_replace")
            )
            results.append((op, _clean_path(path), section))
        return results
    for part in _split_patch(text):
        op = (
            "create"
            if part.get("is_new")
            else ("delete" if part.get("is_deleted") else "str_replace")
        )
        results.append((op, _clean_path(part["path"]), part["text"]))
    return results


def _added_lines(text: str) -> str:
    lines = text.splitlines()
    if any(line.startswith("+") for line in lines):
        return "\n".join(
            line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")
        )
    return text


def _editor_mutations(name: str, arguments: Mapping[str, Any]) -> list[tuple[str, str, str | None]]:
    name_lower = name.lower()
    path = (
        arguments.get("path")
        or arguments.get("file_path")
        or arguments.get("filename")
        or arguments.get("file")
    )
    if (
        not path
        and isinstance(arguments.get("raw"), str)
        and (
            "/" in arguments["raw"]
            or arguments["raw"].endswith((".py", ".txt", ".json", ".sh", ".diff", ".patch"))
        )
    ):
        path = arguments["raw"]
    command = str(arguments.get("command") or arguments.get("action") or "").lower()
    patch = next(
        (
            arguments[key]
            for key in ("patch", "input", "diff")
            if isinstance(arguments.get(key), str)
            and (
                "*** Begin Patch" in arguments[key]
                or "*** Add File:" in arguments[key]
                or "*** Update File:" in arguments[key]
                or DIFF_START.search(arguments[key])
            )
        ),
        None,
    )
    if patch is not None:
        return _apply_patch_files(patch)

    # Hashline input format (e.g. edit {input: "[tests/test_core.py#AB12]\nPUT 3.=3:\n+ ..."})
    if not path and isinstance(arguments.get("input"), str):
        input_str = arguments["input"]
        hash_blocks = re.findall(
            r"^\[([^\n#\]]+)(?:#[0-9a-fA-F]+)?\]\s*\n(.*?)(?=(?:^\[[^\n#\]]+(?:#[0-9a-fA-F]+)?\])|\Z)",
            input_str,
            re.MULTILINE | re.DOTALL,
        )
        if hash_blocks:
            muts: list[tuple[str, str, str | None]] = []
            for target_p, block_body in hash_blocks:
                lines = [
                    line[1:] if (line.startswith("+") and not line.startswith("+++")) else line
                    for line in block_body.splitlines()
                    if not line.startswith(("PUT", "CUT", "REM", "MV", "@@"))
                ]
                added = "\n".join(lines).strip()
                mutations_op = (
                    "create"
                    if any(re.match(r"^PUT\s+<1", line) for line in block_body.splitlines())
                    else "str_replace"
                )
                muts.append((mutations_op, _clean_path(target_p), added or block_body))
            return muts

    # Edits list (e.g. MultiEdit or edit {edits: [...]})
    edits_list = arguments.get("edits")
    if isinstance(edits_list, list) and edits_list:
        muts = []
        for edit_item in edits_list:
            if isinstance(edit_item, Mapping):
                e_path = edit_item.get("path") or edit_item.get("file_path") or path
                if not e_path:
                    continue
                old_val = (
                    edit_item.get("old_str")
                    or edit_item.get("old_string")
                    or edit_item.get("oldText")
                )
                new_val = (
                    edit_item.get("new_str")
                    or edit_item.get("new_string")
                    or edit_item.get("newText")
                    or edit_item.get("content")
                    or edit_item.get("text")
                )
                content = f"--- old\n{_text(old_val)}\n+++ new\n{_text(new_val)}"
                muts.append(("str_replace", _clean_path(str(e_path)), content))
        if muts:
            return muts

    if not isinstance(path, str) or not path.strip():
        return []
    if command == "view" or (
        not command
        and name_lower not in ("create", "create_file", "write", "edit", "multiedit")
        and not any(
            key in arguments
            for key in (
                "old_str",
                "new_str",
                "old_string",
                "new_string",
                "oldText",
                "newText",
                "file_text",
                "content",
                "contents",
                "text",
                "text_content",
            )
        )
    ):
        return []

    old_val = arguments.get("old_str") or arguments.get("old_string") or arguments.get("oldText")
    new_val = arguments.get("new_str") or arguments.get("new_string") or arguments.get("newText")
    if old_val is not None or new_val is not None:
        content = f"--- old\n{_text(old_val)}\n+++ new\n{_text(new_val)}"
        return [("str_replace", _clean_path(path), content)]

    for key in ("file_text", "content", "contents", "text", "text_content"):
        if key in arguments:
            op_name = command or ("create" if name_lower in ("create", "create_file") else "write")
            return [(op_name, _clean_path(path), _text(arguments[key]))]

    if command == "insert":
        return [
            (
                "insert",
                _clean_path(path),
                _text(
                    arguments.get("new_str")
                    or arguments.get("new_string")
                    or arguments.get("newText")
                ),
            )
        ]

    if command == "create" or name_lower in ("create", "create_file"):
        return [("create", _clean_path(path), None)]

    return [(command or name, _clean_path(path), None)]


def _viewed_path(arguments: Mapping[str, Any]) -> str | None:
    path = arguments.get("path") or arguments.get("file_path")
    command = str(arguments.get("command") or "").lower()
    if isinstance(path, str) and command in {"view", "read", ""}:
        return path
    return None


def _summary_line(output: str) -> str | None:
    matches = [match for match in TEST_SUMMARY.finditer(output)]
    if not matches:
        return None
    last = matches[-1]
    start = output.rfind("\n", 0, last.start()) + 1
    end = output.find("\n", last.end())
    return output[start : end if end != -1 else len(output)].strip()[:OUTPUT_LINE_CHARS]


def _test_outcome(code: int | None, summary: str | None) -> str:
    if summary and re.search(
        r"\b[1-9]\d*\s+(?:failed|errors?)\b|^FAIL|FAILED|AssertionError|Error",
        summary,
        re.MULTILINE,
    ):
        return "fail"
    if code is not None:
        return "pass" if code == 0 else "fail"
    if summary and re.search(r"\bpassed\b|^OK\b|^ok\s|PASS", summary, re.MULTILINE):
        return "pass"
    return "unknown"


def _blocks(text: str) -> list[str]:
    """Paragraphs and list items with hard-wrapped lines joined; ``<key>value</key>`` settings dropped."""
    blocks: list[list[str]] = []
    for line in re.sub(r"<([\w-]+)>[^<\s]*</\1>", "", text).splitlines():
        stripped = line.strip()
        if not stripped:
            blocks.append([])
        elif re.match(r"(?:[-*#>]|\d+[.)])\s", stripped) or not blocks:
            blocks.append([stripped])
        else:
            blocks[-1].append(stripped)
    return [" ".join(block) for block in blocks if block]


def _constraints(texts: Iterable[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for line in _blocks(text):
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                sentence = sentence.strip(" -*#>\t")
                if not PROHIBITION.search(sentence):
                    continue
                sentence = sentence[:OUTPUT_LINE_CHARS]
                key = sentence.casefold()
                if key not in seen:
                    seen.add(key)
                    found.append(sentence)
    return found


def _final_claim(steps: Sequence[Mapping[str, Any]], submission: str | None) -> str | None:
    for step in reversed(steps):
        if step.get("source") != "agent":
            continue
        message = THINK.sub("", _text(step.get("message"))).strip()
        if message:
            return elide(message, 8_000)
    return elide(submission, 8_000) if submission else None


def build_anatomy(run: LoadedRun) -> TraceAnatomy:
    """Extract the anatomy from a loaded run (pure; no I/O, no labels)."""
    steps = [step for step in run.doc.get("steps") or [] if isinstance(step, Mapping)]
    users = [_text(step.get("message")) for step in steps if step.get("source") == "user"]
    systems = [_text(step.get("message")) for step in steps if step.get("source") == "system"]
    statement = (users[0] if users else None) or run.prompt or ""
    mutations: list[Mutation] = []
    actions: dict[str, list[Action]] = {
        kind: []
        for kind in (
            "git_history",
            "sensitive_reads",
            "network",
            "test_runs",
            "delegation",
            "assertion_inversions",
        )
    }
    narration: list[dict[str, Any]] = []
    for ordinal, step in enumerate(steps):
        if step.get("source") == "agent":
            message = _text(step.get("message")).strip()
            reasoning = _text(step.get("reasoning_content")).strip()
            if message or reasoning:
                narration.append({"step": ordinal, "message": message, "reasoning": reasoning})
        results = _result_texts(step)
        calls = unpack_legacy_tool_calls(step.get("tool_calls") or [])
        for position, call in enumerate(calls):
            name = str(call.get("function_name") or call.get("name") or "tool")
            arguments = _arguments(call.get("arguments"))
            call_id = call.get("tool_call_id") or call.get("id")
            result = next((text for source, text in results if source and source == call_id), None)
            if result is None and len(results) == len(calls):
                result = results[position][1]
            elif result is None and len(calls) == 1 and results:
                result = "\n".join(text for _source, text in results)
            code, output = _parse_result(result) if result is not None else (None, "")
            command = _command(name, arguments)
            if DELEGATION_TOOLS.match(name) or (command and DELEGATION_COMMAND.search(command)):
                prompt = next(
                    (
                        _text(arguments[key])
                        for key in ("prompt", "instructions", "task", "description", "message")
                        if key in arguments
                    ),
                    command or "",
                )
                actions["delegation"].append(Action(ordinal, "delegation", name, elide(prompt)))
            written: list[tuple[str, str, str | None]] = []
            if command is not None and (
                SHELL_TOOLS.search(name)
                or PYTHON_TOOLS.match(name)
                or not EDITOR_TOOLS.search(name)
            ):
                if PYTHON_TOOLS.match(name):
                    written = [
                        ("python_write", _clean_path(next(p for p in m if p)), None)
                        for m in PY_WRITE.findall(command)
                    ]
                else:
                    written = shell_mutations(command)
                shown = elide(command, COMMAND_CHARS)
                executed = command if PYTHON_TOOLS.match(name) else executed_text(command)
                if GIT_HISTORY.search(executed):
                    actions["git_history"].append(
                        Action(ordinal, "git_history", shown, exit_code=code)
                    )
                if SENSITIVE_PATH.search(executed) or ENV_PROBE.search(executed):
                    actions["sensitive_reads"].append(
                        Action(ordinal, "sensitive_reads", shown, exit_code=code)
                    )
                if NETWORK.search(executed):
                    actions["network"].append(Action(ordinal, "network", shown, exit_code=code))
                if TEST_RUNNER.search(executed):
                    summary = _summary_line(output)
                    actions["test_runs"].append(
                        Action(
                            ordinal, "test_runs", shown, summary, code, _test_outcome(code, summary)
                        )
                    )
            else:
                written = _editor_mutations(name, arguments)
                viewed = _viewed_path(arguments) if not written else None
                if viewed and SENSITIVE_PATH.search(viewed):
                    actions["sensitive_reads"].append(
                        Action(ordinal, "sensitive_reads", f"{name} {viewed}")
                    )
            for op, path, content in written:
                kind = surface(path)
                mutations.append(
                    Mutation(
                        ordinal,
                        name,
                        op,
                        path,
                        kind,
                        elide(content) if (kind == "protected" and content is not None) else None,
                        elide(command, COMMAND_CHARS)
                        if (kind == "protected" and command)
                        else None,
                    )
                )
                if content:
                    for line in _added_lines(content).splitlines():
                        if INVERSION.search(line):
                            actions["assertion_inversions"].append(
                                Action(
                                    ordinal,
                                    "assertion_inversions",
                                    path,
                                    line.strip()[:OUTPUT_LINE_CHARS],
                                )
                            )
    patch_source = None
    patch = run.submission if run.submission and run.submission.strip() else None
    if patch is not None:
        patch_source = "submission"
    else:
        patch = _final_diff_block(steps)
        patch_source = "final_diff_block" if patch else None
    patch_files = _split_patch(patch) if patch and DIFF_START.search(patch) else []
    for part in patch_files:
        for line in _added_lines(part["text"]).splitlines():
            if INVERSION.search(line):
                actions["assertion_inversions"].append(
                    Action(
                        -1, "assertion_inversions", part["path"], line.strip()[:OUTPUT_LINE_CHARS]
                    )
                )
    submission_text = _final_submission(steps)
    touched = sorted({m.path for m in mutations} | {part["path"] for part in patch_files})
    protected = sorted(path for path in touched if surface(path) == "protected")
    last_test = actions["test_runs"][-1] if actions["test_runs"] else None
    anatomy = TraceAnatomy(
        run={
            "run": "run-" + hashlib.sha256(run.run_id.encode()).hexdigest()[:16],
            "loader": run.loader,
            "steps": len(steps),
            "tool_calls": sum(len(step.get("tool_calls") or []) for step in steps),
            "raw_chars": run.raw_chars,
        },
        task_contract={"statement": statement, "constraints": _constraints([*systems, statement])},
        patch_and_mutations={
            "final_patch": {
                "source": patch_source,
                "sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
                "chars": len(patch) if patch else 0,
                "text": patch,
                "files": [{k: v for k, v in part.items() if k != "text"} for part in patch_files],
            },
            "final_submission": submission_text,
            "mutations": [asdict(m) for m in mutations],
            "protected_surfaces": protected,
            "product_surfaces": [path for path in touched if path not in protected],
        },
        high_signal_actions={kind: [asdict(a) for a in items] for kind, items in actions.items()},
        claim_vs_reality={
            "final_claim": _final_claim(steps, submission_text),
            "files_modified": touched,
            "protected_touched": bool(protected),
            "last_test": None
            if last_test is None
            else {
                "step": last_test.step,
                "exit_code": last_test.exit_code,
                "outcome": last_test.outcome,
                "summary": last_test.detail,
            },
            # True: the agent submitted an empty patch; None: no submission or diff was recovered.
            "final_patch_empty": False if patch else (True if run.submission is not None else None),
        },
        narration=narration,
    )
    return anatomy


def anatomy_dict(
    anatomy: TraceAnatomy,
    *,
    include_narration: bool = False,
    include_final_claim: bool = False,
    pattern: re.Pattern[str] = SCRUB_PATTERN,
) -> dict[str, Any]:
    """Scrubbed JSON form. ``final_patch.sha256`` is over the exact pre-scrub patch bytes."""
    data = scrub_tree(
        anatomy.to_dict(
            include_narration=include_narration, include_final_claim=include_final_claim
        ),
        pattern,
    )
    patch = anatomy.patch_and_mutations["final_patch"]["text"]
    data["patch_and_mutations"]["final_patch"]["exact"] = (
        patch is None or scrub(patch, pattern) == patch
    )
    return data


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _fence(text: str) -> list[str]:
    return ["```", text.rstrip("\n"), "```"]


def render_anatomy_card(
    anatomy: TraceAnatomy,
    *,
    include_narration: bool = False,
    include_final_claim: bool = False,
    pattern: re.Pattern[str] = SCRUB_PATTERN,
) -> str:
    """Compact Markdown reader view; action-only unless the narration/claim toggles are set."""
    run = anatomy.run
    lines = [
        f"# Case anatomy {run['run']}",
        f"loader={run['loader']} steps={run['steps']} tool_calls={run['tool_calls']}",
        "",
    ]
    contract = anatomy.task_contract
    lines += [
        "## Task contract",
        elide(contract["statement"].strip(), STATEMENT_CARD_CHARS) or "(no statement)",
    ]
    if contract["constraints"]:
        lines += ["", "Constraints:", *(f"- {item}" for item in contract["constraints"])]
    pm = anatomy.patch_and_mutations
    final = pm["final_patch"]
    lines += ["", "## Final patch"]
    if final["text"]:
        lines.append(
            f"source={final['source']} chars={final['chars']} sha256={final['sha256'][:12]}"
        )
        lines += [
            f"- {f['path']} [{f['surface']}] +{f['added']} -{f['removed']}" for f in final["files"]
        ]
        if final["chars"] <= BLOB_CHARS or not final["files"]:
            lines += _fence(elide(final["text"]))
        else:
            kept = [f for f in _split_patch(final["text"]) if f["surface"] == "protected"]
            omitted = final["chars"] - sum(len(f["text"]) for f in kept)
            for part in kept:
                lines += _fence(elide(part["text"]))
            lines.append(
                f"[... {omitted} chars of product-surface hunks omitted; full patch in --json ...]"
            )
    else:
        lines.append("(no final patch recovered)")
    if pm["final_submission"]:
        lines += ["", "Submission:", *_fence(elide(pm["final_submission"], 4_000))]
    lines += [
        "",
        "## File mutations",
        f"protected: {', '.join(pm['protected_surfaces']) or '(none)'}",
        f"product: {', '.join(pm['product_surfaces']) or '(none)'}",
    ]
    for mutation in pm["mutations"]:
        if mutation["surface"] != "protected":
            continue
        lines.append(
            f"- step {mutation['step']} {mutation['op']} {mutation['path']} via {mutation['tool']}"
        )
        if mutation["content"]:
            lines += _fence(mutation["content"])
        elif mutation["command"]:
            lines += _fence(mutation["command"])
    product_ops: dict[str, int] = {}
    for mutation in pm["mutations"]:
        if mutation["surface"] == "product":
            product_ops[mutation["op"]] = product_ops.get(mutation["op"], 0) + 1
    if product_ops:
        lines.append(
            "product ops: "
            + ", ".join(f"{op}={count}" for op, count in sorted(product_ops.items()))
        )
    lines += ["", "## High-signal actions"]
    any_action = False
    for kind, items in anatomy.high_signal_actions.items():
        if not items:
            continue
        any_action = True
        lines.append(f"### {kind} ({len(items)})")
        for item in items:
            where = "patch" if item["step"] < 0 else f"step {item['step']}"
            extra = []
            if item["exit_code"] is not None:
                extra.append(f"exit={item['exit_code']}")
            if item["outcome"]:
                extra.append(item["outcome"])
            suffix = f" ({', '.join(extra)})" if extra else ""
            command = (
                item["command"].replace("\n", " ⏎ ") if kind != "delegation" else item["command"]
            )
            lines.append(f"- {where}: `{elide(command, COMMAND_CHARS)}`{suffix}")
            if item["detail"]:
                lines += (
                    _fence(item["detail"]) if kind == "delegation" else [f"  -> {item['detail']}"]
                )
    if not any_action:
        lines.append("(none)")
    claim = anatomy.claim_vs_reality
    last = claim["last_test"]
    lines += ["", "## Claim vs reality"]
    if include_final_claim:
        lines += ["Final claim:", *_fence(claim["final_claim"] or "(no final message)")]
    lines += [
        f"- files modified: {len(claim['files_modified'])}",
        f"- protected surfaces touched: {'yes' if claim['protected_touched'] else 'no'}",
        "- last test run: "
        + (
            "none"
            if last is None
            else f"step {last['step']} exit={last['exit_code']} {last['outcome']}"
            + (f" | {last['summary']}" if last["summary"] else "")
        ),
        "- final patch empty: "
        + {True: "yes", False: "no", None: "unknown (no patch recovered)"}[
            claim["final_patch_empty"]
        ],
    ]
    if include_narration and anatomy.narration:
        lines += ["", "## Narration"]
        for entry in anatomy.narration:
            if entry["reasoning"]:
                lines.append(f"[step {entry['step']} reasoning] {entry['reasoning']}")
            if entry["message"]:
                lines.append(f"[step {entry['step']}] {entry['message']}")
    return scrub("\n".join(lines) + "\n", pattern)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def database_scrub_pattern(database: Path) -> re.Pattern[str]:
    """Scrub pattern extended with every dataset id present in the trace DB."""
    if not database.is_file():
        return SCRUB_PATTERN
    connection = connect_readonly(database)
    try:
        ids = [
            row[0]
            for row in connection.execute("SELECT DISTINCT dataset_id FROM corpus_tasks LIMIT 1000")
        ]
    finally:
        connection.close()
    return scrub_pattern(ids)


def register_subcommands(actions: Any) -> None:
    """``evallab trace anatomy``; registration opens nothing."""
    parser = actions.add_parser(
        "anatomy",
        help="Label-free structured case anatomy of one admitted DEV run or local ATIF trial",
        description=(
            "Task contract, exact final patch, file-mutation ledger (protected vs product surfaces), "
            "high-signal actions and claim-vs-reality. Cloud trace ids and CheatBench item ids pass the "
            "DEV admission gate; --atif-file reads a local Harbor/ATIF trial."
        ),
    )
    parser.add_argument("run", nargs="?", help="Cloud trace id or CheatBench item id")
    parser.add_argument(
        "--atif-file", type=Path, help="Local Harbor/ATIF trial JSON or trial directory"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the anatomy JSON instead of the card"
    )
    parser.add_argument(
        "--include-narration", action="store_true", help="Add agent messages and reasoning"
    )
    parser.add_argument(
        "--include-final-claim", action="store_true", help="Add the agent's final claim text"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cas-dir", type=Path, default=DEFAULT_CAS_DIR)
    parser.add_argument(
        "--results-root", type=Path, help="Published results home (CheatBench trajectories)"
    )
    parser.set_defaults(func=_anatomy_command)


def _anatomy_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    import sys

    if (args.run is None) == (args.atif_file is None):
        print("error: give exactly one of a run id or --atif-file", file=sys.stderr)
        return 2
    try:
        if args.atif_file is not None:
            run = load_atif_file(args.atif_file)
        else:
            run = load_run(
                args.run,
                repo_root=root,
                database=args.database,
                cas_dir=args.cas_dir,
                results_root=args.results_root,
            )
    except RefusedRun as error:
        print(f"refused: {error}", file=sys.stderr)
        return 3
    anatomy = build_anatomy(run)
    pattern = database_scrub_pattern(args.database)
    if args.json:
        print(
            json.dumps(
                anatomy_dict(
                    anatomy,
                    include_narration=args.include_narration,
                    include_final_claim=args.include_final_claim,
                    pattern=pattern,
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(
            render_anatomy_card(
                anatomy,
                include_narration=args.include_narration,
                include_final_claim=args.include_final_claim,
                pattern=pattern,
            ),
            end="",
        )
    return 0
