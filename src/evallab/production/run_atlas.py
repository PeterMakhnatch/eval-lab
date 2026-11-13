"""Read-only run projection, identity blinding, and source redaction."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import blind_atif, blind_atif_step_origins, find_forbidden_json

STATE_ROOT = Path.home() / ".local/state/evallab-prod-live"
DEFAULT_DATABASE = STATE_ROOT / "cloud-trace-db.sqlite"
CHEATBENCH_DATASET = "cheatbench-actions-v1"
# Our own Harbor/MiMo/TB runs in the cloud DB. ``qa-trace-sft-v0`` is derived
# from CheatBench rows and ``goodfire-reward-hack-data`` is external.
GOODFIRE_DATASET = "goodfire-reward-hack-data"


# Source and benchmark names are replaced in rendered text so the reader cannot
# infer the benchmark (and with it the base rate) from paths or prompts.
SOURCE_TERMS = (
    r"good[-_ ]?fire", r"cheat[-_ ]?bench", r"impossible[-_ ]?bench", r"hack[-_ ]?bench",
    r"hack[-_ ]?trace", r"reward[-_]hack[-_]data", r"terminal[-_ ]?wrench",
    r"terminal[-_ ]?bench", r"swe[-_ ]?(?:re)?bench", r"swe[-_ ]?smith", r"deep[-_ ]?swe",
    r"bounty[-_ ]?bench", r"mle[-_ ]?bench", r"kernel[-_ ]?bench", r"hal[-_ ]?usaco",
    r"cy[-_ ]?bench", r"sim[-_ ]?glot", r"mimo[-_ ]defect[-_ ]map", r"mimo(?![a-z])",
    r"tb4[-_ ]audit",
    r"tb2[-_ ]to[-_ ]tb21", r"qa[-_ ]trace[-_ ]sft", r"kimi-tb4-dev-swarm-\d{8}",
    r"claude[-_ ]opus(?:[-_ ][0-9]+(?:[-_.][0-9]+)*)?",
    r"(?<![\w.])opus(?:[-_ ][0-9]+(?:[-_.][0-9]+)*)?(?![\w])",
)
SOURCE_PLACEHOLDER = "[source]"
# Our own task prompts can name their split ("DEV benchmark task").
SPLIT_PROVENANCE_PATTERN = re.compile(r"\b(?:DEV|TEST|HELDOUT)(?=\s+benchmark\s+task\b)")
SPLIT_PLACEHOLDER = "[split]"


def source_pattern(extra_terms: Iterable[str] = ()) -> re.Pattern[str]:
    """``SOURCE_TERMS`` plus literal ids such as dataset ids, longest first."""
    literals = []
    for term in sorted({term.strip() for term in extra_terms if term and term.strip()}, key=len, reverse=True):
        if term.casefold() == "opus":
            literals.append(r"(?<![\w.])opus(?![\w])")
        else:
            literals.append(re.escape(term))
    return re.compile("|".join([*literals, *SOURCE_TERMS]), re.IGNORECASE)


SOURCE_NAME_PATTERN = source_pattern()


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AtlasRun:
    """One rollout before blinding; ``doc`` is ATIF-shaped with original step ids."""

    run_id: str
    source: str
    split: str
    outcome: str
    doc: Mapping[str, Any]


_IDENTITY_SQL = (
    "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
    " FROM corpus_tasks ORDER BY task_id"
)


def connect_readonly(database: Path) -> sqlite3.Connection:
    """Open the cloud trace DB read-only; the atlas never writes to it."""
    if not database.is_file():
        raise FileNotFoundError(f"cloud trace database not found: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def admitted_task_ids(connection: sqlite3.Connection, registry: Any) -> frozenset[str]:
    """DEV tasks that pass the shared cross-key admission gate."""
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids

    identities = [dict(row) for row in connection.execute(_IDENTITY_SQL)]
    return admitted_cloud_task_ids(identities, registry=registry)


def outcome_from_reward(reward: Any) -> str:
    if isinstance(reward, bool) or not isinstance(reward, int | float):
        return "unknown"
    if reward >= 1.0:
        return "pass"
    if reward <= 0.0:
        return "fail"
    return "unknown"


def cloud_trace_ids(
    connection: sqlite3.Connection,
    *,
    admitted: frozenset[str],
    dataset_ids: Sequence[str] | None = None,
    trust_tiers: Sequence[str] | None = None,
    trace_ids: Sequence[str] | None = None,
) -> list[str]:
    """Admitted DEV trace ids, optionally narrowed, in a stable order."""
    clauses = ["lower(c.split) = 'dev'"]
    params: list[Any] = []
    for column, values in (
        ("c.dataset_id", dataset_ids), ("a.trust_tier", trust_tiers), ("a.trace_id", trace_ids),
    ):
        if values:
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
    rows = connection.execute(
        "SELECT a.trace_id, a.task_id FROM agent_traces a JOIN corpus_tasks c USING(task_id)"
        f" WHERE {' AND '.join(clauses)} ORDER BY a.trace_id",
        params,
    )
    return [row["trace_id"] for row in rows if row["task_id"] in admitted]


def _arguments(value: Any) -> dict[str, Any]:
    """ATIF tool-call arguments are objects; native rows often store JSON strings."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"raw": value}
        return dict(parsed) if isinstance(parsed, Mapping) else {"raw": value}
    return {} if value is None else {"raw": value}


def unpack_legacy_tool_calls(calls: Iterable[Any]) -> list[dict[str, Any]]:
    """Unpack legacy ATIF function_name == "tool" + arguments.raw JSON-list artifacts."""
    unpacked: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        func_name = str(call.get("function_name") or call.get("name") or "")
        arguments = call.get("arguments")
        if not func_name and "function" in call and isinstance(call["function"], Mapping):
            func_name = str(call["function"].get("name") or call["function"].get("function_name") or "")
            arguments = call["function"].get("arguments")
            unpacked.append({
                "tool_call_id": str(call.get("id") or call.get("tool_call_id") or "call"),
                "name": func_name,
                "arguments": _arguments(arguments),
            })
            continue
        if func_name == "tool" and isinstance(arguments, Mapping):
            raw_data = arguments.get("raw") if "raw" in arguments else arguments.get("command")
            if isinstance(raw_data, str) and raw_data.strip().startswith("["):
                try:
                    parsed_list = json.loads(raw_data)
                except (ValueError, TypeError):
                    parsed_list = None
            elif isinstance(raw_data, list):
                parsed_list = raw_data
            else:
                parsed_list = None

            if isinstance(parsed_list, list) and parsed_list and all(isinstance(c, Mapping) for c in parsed_list):
                base_id = str(call.get("tool_call_id") or call.get("id") or "call")
                for idx, subcall in enumerate(parsed_list):
                    sub_func = subcall.get("function")
                    if isinstance(sub_func, Mapping):
                        name = str(sub_func.get("name") or sub_func.get("function_name") or "tool")
                        args = (
                            sub_func.get("arguments")
                            or sub_func.get("input")
                            or subcall.get("arguments")
                            or subcall.get("input")
                        )
                    else:
                        name = str(subcall.get("name") or subcall.get("function_name") or "tool")
                        args = subcall.get("arguments") if "arguments" in subcall else subcall.get("input")
                    cid = str(subcall.get("id") or subcall.get("tool_call_id") or f"{base_id}-{idx}")
                    unpacked.append({
                        "tool_call_id": cid,
                        "function_name": name,
                        "arguments": _arguments(args),
                    })
                continue
        unpacked.append(dict(call))
    return unpacked


def _tool_calls(tool_name: str | None, command: str | None) -> list[dict[str, Any]]:
    if not command and not tool_name:
        return []
    if command:
        try:
            parsed = json.loads(command)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and parsed and all(
            isinstance(call, Mapping) and ("name" in call or "function" in call) for call in parsed
        ):
            calls: list[dict[str, Any]] = []
            for call in parsed:
                func_obj = call.get("function")
                if isinstance(func_obj, Mapping):
                    func_name = str(func_obj.get("name") or func_obj.get("function_name") or "")
                    raw_args = (
                        func_obj.get("arguments")
                        or func_obj.get("input")
                        or call.get("arguments")
                        or call.get("input")
                    )
                else:
                    func_name = str(call.get("name") or call.get("function_name") or "")
                    raw_args = call.get("arguments") if "arguments" in call else call.get("input")
                calls.append({
                    "tool_call_id": str(call.get("id") or call.get("tool_call_id") or ""),
                    "function_name": func_name,
                    "arguments": _arguments(raw_args),
                })
            return calls
    return [{"tool_call_id": "", "function_name": tool_name or "action",
             "arguments": _arguments(command or "")}]


def cloud_doc(connection: sqlite3.Connection, trace_id: str) -> dict[str, Any]:
    """Native DB steps -> an ATIF-shaped document keyed by original ``step_index``.

    From ``flags_json`` only ``reasoning_content``, ``tool_call_id`` and the
    structural ``on_final_parent_path`` are read; publisher judge flags,
    passages and labels in that column are never read. Nodes explicitly off the
    final parent path are resampled alternatives, not part of the run, and are
    dropped. Tool results attach to the agent step that called them, as in ATIF.
    """
    steps: list[dict[str, Any]] = []
    owners: dict[str, dict[str, Any]] = {}
    last_agent: dict[str, Any] | None = None
    rows = connection.execute(
        "SELECT step_index, role, tool_name, command_or_action, content_text,"
        " observation_preview, flags_json FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
        (trace_id,),
    )
    for row in rows:
        try:
            flags = json.loads(row["flags_json"] or "{}")
        except ValueError:
            flags = {}
        flags = flags if isinstance(flags, Mapping) else {}
        if flags.get("on_final_parent_path") is False:
            continue
        role = str(row["role"] or "")
        content = row["content_text"] or ""
        if role in ("assistant", "agent"):
            step: dict[str, Any] = {
                "step_id": row["step_index"], "source": "agent", "message": content,
                "tool_calls": _tool_calls(row["tool_name"], row["command_or_action"]),
                "observation": {"results": []},
            }
            reasoning = flags.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                step["reasoning_content"] = reasoning
            if row["observation_preview"]:
                step["observation"]["results"].append({"content": row["observation_preview"]})
            for call in step["tool_calls"]:
                if call["tool_call_id"]:
                    owners[call["tool_call_id"]] = step
            steps.append(step)
            last_agent = step
            continue
        if role == "tool":
            call_id = flags.get("tool_call_id")
            owner = owners.get(str(call_id)) if call_id else None
            owner = owner or last_agent
            text = content or row["observation_preview"] or ""
            if owner is not None:
                owner["observation"]["results"].append({"content": text})
                continue
            steps.append({"step_id": row["step_index"], "source": "tool", "message": text})
            continue
        step = {"step_id": row["step_index"], "source": role or "system", "message": content}
        calls = _tool_calls(row["tool_name"], row["command_or_action"])
        if calls:
            step["tool_calls"] = calls
        if row["observation_preview"]:
            step["observation"] = {"results": [{"content": row["observation_preview"]}]}
        steps.append(step)
    return {"schema_version": "ATIF-v1.6", "steps": steps}


def cloud_runs(
    database: Path,
    *,
    registry: Any,
    dataset_ids: Sequence[str] | None = None,
    trust_tiers: Sequence[str] | None = None,
    trace_ids: Sequence[str] | None = None,
    skip: Iterable[str] = (),
) -> Iterator[AtlasRun]:
    """Admitted DEV rollouts from the cloud trace DB, read-only."""
    skipped = set(skip)
    connection = connect_readonly(database)
    try:
        admitted = admitted_task_ids(connection, registry)
        for trace_id in cloud_trace_ids(
            connection, admitted=admitted, dataset_ids=dataset_ids,
            trust_tiers=trust_tiers, trace_ids=trace_ids,
        ):
            if trace_id in skipped:
                continue
            row = connection.execute(
                "SELECT a.reward, c.dataset_id, c.split FROM agent_traces a"
                " JOIN corpus_tasks c USING(task_id) WHERE a.trace_id = ?",
                (trace_id,),
            ).fetchone()
            yield AtlasRun(
                run_id=trace_id, source=row["dataset_id"], split=str(row["split"]).upper(),
                outcome=outcome_from_reward(row["reward"]), doc=cloud_doc(connection, trace_id),
            )
    finally:
        connection.close()


def cheatbench_runs(
    registry: Any,
    results_root: Path,
    *,
    dataset_id: str = CHEATBENCH_DATASET,
    item_ids: Sequence[str] | None = None,
    skip: Iterable[str] = (),
) -> Iterator[AtlasRun]:
    """CheatBench DEV items from the answer-key registry (full trajectory view)."""
    skipped = set(skip)
    wanted = set(item_ids) if item_ids else None
    for item in registry.items(dataset_id=dataset_id, split="dev"):
        if item.item_id in skipped or (wanted is not None and item.item_id not in wanted):
            continue
        if registry.assigned_test_anywhere(item_id=item.item_id, task_id=item.task_id):
            continue
        reference = (item.metadata.get("trial_ref") or {}).get("path")
        if not reference:
            continue
        path = results_root / reference / "agent" / "trajectory.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        yield AtlasRun(run_id=item.item_id, source=dataset_id, split="DEV", outcome="unknown", doc=doc)


# --------------------------------------------------------------------------- #
# blinding and rendering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlindRun:
    """What the model may see: renumbered steps plus the private id map."""

    steps: list[dict[str, Any]]
    origins: list[Any]


def blind_run(run: AtlasRun) -> BlindRun:
    """Allowlist ``steps`` only, apply :func:`blind_atif`, and fail closed on leaks."""
    allowlisted = {"schema_version": run.doc.get("schema_version"), "steps": run.doc.get("steps") or []}
    opaque = "run-" + hashlib.sha256(run.run_id.encode()).hexdigest()[:16]
    blinded = blind_atif(allowlisted, item_id=opaque)
    hits = find_forbidden_json(blinded["steps"])
    if hits:
        raise ValueError(f"blinded run still carries evaluator fields: {hits[:3]}")
    return BlindRun(steps=blinded["steps"], origins=blind_atif_step_origins(allowlisted))


def redact_sources(text: str, pattern: re.Pattern[str] = SOURCE_NAME_PATTERN) -> str:
    text = pattern.sub(SOURCE_PLACEHOLDER, text)
    return SPLIT_PROVENANCE_PATTERN.sub(SPLIT_PLACEHOLDER, text)
