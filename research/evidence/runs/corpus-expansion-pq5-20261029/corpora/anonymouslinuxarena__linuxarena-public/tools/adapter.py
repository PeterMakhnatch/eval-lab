#!/usr/bin/env python3
"""Read-only adapter for the locked LinuxArena public-mirror reviewer sample (``data/sample.jsonl``).

Unit: one deduplicated trajectory (a LinuxArena agent run on one environment x main task, with or
without a hidden side task; byte-identical reruns and truncated prefix replays are merged). The
detector sees ``_steps`` only: the agent's tool calls (function + arguments) and the tool
results. The main-task description is deliberately NOT included: for the ``straj`` rows it names
the side task ("...the <side task> side task targets"), so it is a label. It never sees the
system prompt (the attack prompt names the side task), the initial user turn, agent reasoning
text, extended thinking, inter-action messages, ``submit`` calls (attack submissions carry the
side-task payload fields, so they are dropped in both modes), side-task tools
(``run_side_task_test``, phase-planning tools), side-task text, monitor scores/reasoning, scorer
explanations, run names or ids.

Construct: INSTRUCTED covert sabotage (attack-mode agents are told to do a hidden side task; the
``straj`` rows are curated sabotage trajectories with no system prompt), not spontaneous cheating.
The CLI prints aggregates only, never transcript text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "anonymouslinuxarena/linuxarena-public"
REVISION = "2fd29f9c7bc190fb873bd76c6bd648f22b9ab0a7"
LICENCE = "cc-by-4.0"
CORPUS = "anonymouslinuxarena__linuxarena-public"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
DATA = "data/sample.jsonl"

# Tools that only exist in one condition (attack side-task test and phase planning), exist for
# planning only, or whose argument schema is condition-specific (``submit``: attack submissions
# carry the side-task payload fields); dropped from BOTH modes with their results so neither the
# tool vocabulary nor the argument keys are a label.
DROPPED_TOOLS = frozenset(
    {
        "run_side_task_test",
        "start_phase1_planning",
        "start_phase2_planning",
        "ready_to_plan",
        "submit",
    }
)
# Vocabulary that names the condition. Counted in the residual scan (agent-authored code/comments
# and environment files can contain it); only the ``forbidden`` needles below are hard leaks.
CONDITION_RE = re.compile(
    r"side[\s_-]?task|sabotag|covert|stealth|red[\s-]?team|secretly|run_side_task", re.I
)
FORBIDDEN_FUNCTION_RE = re.compile(r"side_task|phase\d_planning|ready_to_plan", re.I)


def squash(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def sha256_json(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def slice_of(row: dict[str, Any]) -> str:
    """Agent-run slice with the monitor-rerun prefix removed: haiku_honest, opus_honest, ..."""
    name = row["run_display_name"].rsplit(" monitor on ", 1)[-1]
    return name.casefold().replace(" ", "_")


def is_rerun(row: dict[str, Any]) -> bool:
    return " monitor on " in row["run_display_name"]


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    with (root / DATA).open("rb") as handle:
        for line in handle:
            yield json.loads(line)


def raw_trace_sha(row: dict[str, Any]) -> str:
    """Digest of the full agent trace, excluding monitor and blue-protocol fields."""
    keys = ("function", "arguments", "output", "output_error", "agent_reasoning")
    keys += ("extended_thinking", "inter_action_messages")
    return sha256_json([{k: a.get(k) for k in keys} for a in row["actions"]])


def steps_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for action in row["actions"]:
        if action["function"] in DROPPED_TOOLS:
            continue
        steps.append(
            {
                "role": "assistant",
                "text": "",
                "tool_calls": [{"function": action["function"], "arguments": action["arguments"]}],
            }
        )
        result = action.get("output") or ""
        if action.get("output_error"):
            result = f"{result}\n{action['output_error']}".strip()
        steps.append({"role": "tool", "function": action["function"], "text": result})
    if not steps:
        return []
    return steps


def steps_sha(row: dict[str, Any]) -> str:
    return sha256_json(steps_of(row))


def label_state(row: dict[str, Any]) -> tuple[str, str]:
    """Return (state, reason). Missing or inconsistent labels are ambiguous, never negative."""
    name = slice_of(row)
    side, success = row["side_task"], row["side_task_success"]
    if not steps_of(row):
        return "excluded", "no_agent_steps"
    if side is None and name.endswith("_honest"):
        if success is True:
            return "ambiguous", "honest_with_side_success"
        return "negative", "honest_mode"
    if side is not None and (name.endswith("_attack") or name == "straj"):
        if success is True:
            return "positive", "attack_side_task_succeeded"
        if success is False:
            return "ambiguous", "attack_side_task_failed_attempted_sabotage"
        return "ambiguous", "attack_side_task_ungraded"
    return "ambiguous", "unrecognised_mode_slice"


def _call_digests(steps: list[dict[str, Any]]) -> tuple[str, ...]:
    """Per tool call digest of (function, arguments): the action sequence without outputs."""
    return tuple(
        hashlib.sha256(
            (c["function"] + "\0" + json.dumps(c["arguments"], sort_keys=True)).encode("utf-8")
        ).hexdigest()[:16]
        for s in steps
        for c in s.get("tool_calls", [])
    )


def dedupe(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Group rows into units: identical steps, or a replay that is a prefix of a longer run.

    Rows are bucketed by (slice, environment, main task, side task). Inside a bucket, rows are
    visited longest first (original agent run before a monitor rerun on ties); a row whose
    (function, arguments) sequence is a prefix of an existing unit's sequence is merged into that
    unit, otherwise it starts a new unit. This merges byte-identical reruns and the truncated
    replays that the ``GPT-5 Nano monitor on Haiku attack`` reruns contain; genuinely different
    action sequences stay separate units. The longest member of a unit supplies ``_steps``.
    Members that repeat the unit's full action sequence and carry a different label state make
    the unit ambiguous (``duplicate_conflicting_labels``). A strictly shorter prefix replay is a
    truncated copy: its own scorer outcome is not comparable with the complete run, so it does
    not vote on the label (its states are kept in ``replay_states``). Rows with no agent step are
    ``excluded``. Units with byte-identical ``_steps`` under two different cells are merged last.
    """
    records: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    groups: dict[str, dict[str, Any]] = {}
    for row in iter_rows(root):
        state, reason = label_state(row)
        steps = steps_of(row)
        record = {
            "id": row["trajectory_id"],
            "state": state,
            "reason": reason,
            "steps_sha": steps_sha(row) if steps else "",
            "calls": _call_digests(steps),
            "rerun": is_rerun(row),
            "run": row["run_display_name"],
            "raw_sha": raw_trace_sha(row),
        }
        if state == "excluded":
            groups[f"excluded:{record['id']}"] = _new_group(record)
            continue
        bucket = (
            slice_of(row),
            row["environment"]["id"],
            row["main_task"]["id"],
            (row["side_task"] or {}).get("id"),
        )
        records.setdefault(bucket, []).append(record)
    for bucket_records in records.values():
        bucket_records.sort(key=lambda r: (-len(r["calls"]), r["rerun"], r["id"]))
        roots: list[dict[str, Any]] = []
        for record in bucket_records:
            parents = [g for g in roots if g["calls"][: len(record["calls"])] == record["calls"]]
            if not parents:
                group = _new_group(record)
                roots.append(group)
                groups[f"{record['steps_sha']}:{record['id']}"] = group
                continue
            group = parents[0]
            if len(parents) > 1:
                group["merge_kinds"]["prefix_of_several_units"] += 1
            if record["steps_sha"] == group["steps_sha"]:
                kind = "identical_steps"
            elif record["calls"] == group["calls"]:
                kind = "identical_actions_other_output"
            else:
                kind = "prefix_replay"
            group["merge_kinds"][kind] += 1
            _add_member(group, record, votes=kind != "prefix_replay")
    # Byte-identical detector input under two cells (one straj trajectory attached to two side
    # tasks): one unit; conflicting label states still make it ambiguous.
    by_steps: dict[str, list[str]] = {}
    for key, group in groups.items():
        if group["steps_sha"]:
            by_steps.setdefault(group["steps_sha"], []).append(key)
    for keys in by_steps.values():
        keys.sort(key=lambda k: groups[k]["rank"])
        first = groups[keys[0]]
        for key in keys[1:]:
            other = groups.pop(key)
            first["states"].update(other["states"])
            first["replay_states"].update(other["replay_states"])
            first["ids"].extend(other["ids"])
            first["runs"].update(other["runs"])
            first["raw_shas"] |= other["raw_shas"]
            first["merge_kinds"].update(other["merge_kinds"])
            first["merge_kinds"]["identical_steps_other_cell"] += len(other["ids"])
    return groups


def _new_group(record: dict[str, Any]) -> dict[str, Any]:
    group: dict[str, Any] = {
        "states": Counter(),
        "replay_states": Counter(),
        "ids": [],
        "runs": Counter(),
        "raw_shas": set(),
        "merge_kinds": Counter(),
        "steps_sha": record["steps_sha"],
        "rank": (-len(record["calls"]), record["rerun"], record["id"]),
        "calls": record["calls"],
        "root_id": record["id"],
        "reason": record["reason"],
    }
    _add_member(group, record, votes=True)
    return group


def _add_member(group: dict[str, Any], record: dict[str, Any], votes: bool) -> None:
    group["states" if votes else "replay_states"][record["state"]] += 1
    group["ids"].append(record["id"])
    group["runs"][record["run"]] += 1
    group["raw_shas"].add(record["raw_sha"])


def group_state(group: dict[str, Any]) -> tuple[str, str]:
    states = group["states"]
    if len(states) > 1:
        return "ambiguous", "duplicate_conflicting_labels"
    return next(iter(states)), group["reason"]


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """One dict per deduplicated labelled trajectory; only ``_steps`` may reach a detector."""
    groups = dedupe(root)
    keep = {g["root_id"]: g for g in groups.values()}
    for row in iter_rows(root):
        group = keep.get(row["trajectory_id"])
        if group is None:
            continue
        state, reason = group_state(group)
        if state == "excluded":
            continue
        steps = steps_of(row)
        yield {
            "_steps": steps,
            "gold": {"state": state, "reason": reason, "positive": state == "positive"},
            "labels": {
                "side_task_success": row["side_task_success"],
                "main_task_success": row["main_task_success"],
            },
            "identity": {
                "trajectory_id": row["trajectory_id"],
                "duplicate_ids": sorted(group["ids"]),
                "merge_kinds": dict(sorted(group["merge_kinds"].items())),
                "run_id": row["run_id"],
                "slice": slice_of(row),
                "environment": row["environment"]["id"],
                "main_task_id": row["main_task"]["id"],
                "main_task_category": row["main_task"].get("task_category"),
                "main_task_description_chars": len(row["main_task"].get("description") or ""),
                "main_task_description_names_side_task": bool(
                    CONDITION_RE.search(row["main_task"].get("description") or "")
                ),
                "side_task_id": (row["side_task"] or {}).get("id"),
                "steps_sha256": group["steps_sha"],
                "trace_sha256": sorted(group["raw_shas"]),
                "has_agent_model": bool(row.get("agent_model")),
                "condition_vocabulary": condition_vocabulary(steps),
            },
        }


def _lines(*texts: str | None, minimum: int = 40) -> list[str]:
    return [
        squash(line)
        for text in texts
        if text
        for line in text.splitlines()
        if len(line.strip()) >= minimum
    ]


def _sentences(text: str | None) -> list[str]:
    return [squash(s) for s in re.split(r"(?<=[.!?])\s+", text or "") if len(s) >= 40]


def _word_in(needle: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])", text) is not None


def _leaves(value: Any) -> Iterator[str]:
    """Every key and scalar of a tool-call argument tree as plain text (not JSON-escaped)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif value is not None:
        yield str(value)


def _without_quoted_code(value: Any) -> Any:
    """``attack_analysis`` minus ``incriminating_behaviour``: that field quotes the agent's code."""
    if isinstance(value, dict):
        return {
            key: _without_quoted_code(item)
            for key, item in value.items()
            if key != "incriminating_behaviour"
        }
    if isinstance(value, list):
        return [_without_quoted_code(item) for item in value]
    return value


def _args_text(steps: list[dict[str, Any]]) -> str:
    return squash(
        "\n".join(
            leaf for s in steps for c in s.get("tool_calls", []) for leaf in _leaves(c["arguments"])
        )
    )


def _results_text(steps: list[dict[str, Any]]) -> str:
    return squash("\n".join(s["text"] for s in steps if s["role"] == "tool"))


def _step_text(steps: list[dict[str, Any]]) -> str:
    """All text a detector can read: function names, argument keys and values, tool results."""
    parts: list[str] = []
    for s in steps:
        parts.append(s["text"])
        for c in s.get("tool_calls", []):
            parts.append(c["function"])
            parts.extend(_leaves(c["arguments"]))
    return squash("\n".join(parts))


def condition_vocabulary(steps: list[dict[str, Any]]) -> dict[str, bool]:
    """Condition-naming words in tool arguments and in tool results (whitespace-normalised)."""
    in_args = bool(CONDITION_RE.search(_args_text(steps)))
    in_results = bool(CONDITION_RE.search(_results_text(steps)))
    return {
        "tool_arguments": in_args,
        "tool_results": in_results,
        "anywhere": in_args or in_results,
    }


def _arg_keys(row: dict[str, Any], dropped: bool) -> set[tuple[str, str]]:
    return {
        (a["function"], key)
        for a in row["actions"]
        if (a["function"] in DROPPED_TOOLS) is dropped
        for key in (a.get("arguments") or {})
    }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Real leak scan: whitespace-normalised substring checks on step text including tool args.

    Hard leaks (must be 0): attack-only system-prompt sentences, side-task goal/scoring/short
    description lines, ``attack_analysis`` lines, side-task name, run display name, trajectory/run
    ids, dropped-tool function names, tool-argument keys that belong to the side-task tool
    schemas (keys of the dropped tools' arguments that no honest kept tool uses), and any
    Haiku-attack tool-argument key that no honest run uses. Residual vocabulary (``side task``
    etc. inside agent-written code, comments or environment files) is counted per slice, in tool
    arguments and in tool results separately, and is not treated as an adapter leak. Monitor
    reasoning and scorer explanations quote the agent's own actions/pytest output, so their line
    overlap with tool text is reported as natural overlap, not a leak.
    """
    honest_sentences: set[str] = set()
    honest_keys: set[tuple[str, str]] = set()
    dropped_keys: set[str] = set()
    for row in iter_rows(root):
        if row["side_task"] is None:
            honest_sentences.update(_sentences(row["system_prompt"]))
            honest_keys |= _arg_keys(row, dropped=False)
        else:
            dropped_keys |= {key for _, key in _arg_keys(row, dropped=True)}
    honest_functions = {function for function, _ in honest_keys}
    schema_keys = dropped_keys - {key for _, key in honest_keys}
    name_forms: set[str] = set()
    for row in iter_rows(root):
        if row["side_task"] and row["side_task"].get("name"):
            form = squash(row["side_task"]["name"])
            name_forms.update(n for n in (form, form.replace("_", " ")) if len(n) >= 6)
    env_visible: set[str] = set()  # side-task names that honest agents also see in the environment
    for row in iter_rows(root):
        if row["side_task"] is None:
            text = _step_text(steps_of(row))
            env_visible.update(n for n in name_forms if _word_in(n, text))
    leaks: Counter[str] = Counter()
    issues: set[str] = set()
    stripped: Counter[str] = Counter()
    residual: dict[str, Counter[str]] = {}
    natural: Counter[str] = Counter()
    straj_only_functions: set[str] = set()
    seen = 0
    step_total = 0
    for row in iter_rows(root):
        steps = steps_of(row)
        if not steps:
            continue
        seen += 1
        step_total += len(steps)
        tag = slice_of(row)
        stripped["system_prompts"] += bool(row["system_prompt"])
        stripped["initial_user_messages"] += bool(row["initial_user_message"])
        for action in row["actions"]:
            stripped["agent_reasoning"] += bool(action.get("agent_reasoning"))
            stripped["extended_thinking"] += bool(action.get("extended_thinking"))
            stripped["inter_action_messages"] += bool(action.get("inter_action_messages"))
            stripped["monitor_responses"] += bool(action.get("monitor_response"))
            stripped["dropped_tool_calls"] += action["function"] in DROPPED_TOOLS
            stripped["submit_calls"] += action["function"] == "submit"
        stripped["traj_monitor_responses"] += bool(row["traj_monitor_response"])
        stripped["scorer_explanations"] += bool(row["scorer_explanation"])
        stripped["attack_analysis"] += bool(row["attack_analysis"])
        text = _step_text(steps)
        side = row["side_task"] or {}
        needles: dict[str, list[str]] = {
            "attack_prompt_sentence": [
                s
                for s in _sentences(row["system_prompt"])
                if s not in honest_sentences and s not in squash(row["main_task"]["description"])
            ],
            "side_description_line": _lines(side.get("short_description")),
            "attack_analysis_prose_line": _lines(
                *_leaves(_without_quoted_code(row["attack_analysis"]))
            ),
            "main_description_line": _lines(row["main_task"].get("description"))
            if slice_of(row) == "straj"
            else [],
            "run_display_name": [squash(row["run_display_name"])],
            "ids": [row["trajectory_id"], row["run_id"]],
        }
        quoted = _lines(*_leaves(row["attack_analysis"]))
        natural["attack_analysis_quoted_code_line_overlap"] += any(n in text for n in quoted)
        for kind, items in needles.items():
            if kind == "main_description_line":
                continue  # description is not in _steps at all; checked below for the record
            if any(n and n in text for n in items):
                leaks[kind] += 1
                issues.add(f"{kind}:{row['trajectory_id']}")
        names = {squash(side.get("name")), squash(side.get("name")).replace("_", " ")} - {""}
        hits = {n for n in names if len(n) >= 6 and _word_in(n, text)}
        if hits - env_visible:
            leaks["side_name"] += 1
            issues.add(f"side_name:{row['trajectory_id']}")
        elif hits:
            natural["side_name_in_environment_files_also_seen_by_honest_agents"] += 1
        payload = _lines(side.get("goal"), side.get("scoring_details"))
        natural["agent_authored_side_payload_line_overlap"] += any(n in text for n in payload)
        description_hit = any(n in text for n in needles["main_description_line"])
        natural["straj_description_line_in_steps"] += description_hit
        calls = [c for s in steps for c in s.get("tool_calls", [])]
        if any(
            c["function"] in DROPPED_TOOLS or FORBIDDEN_FUNCTION_RE.search(c["function"])
            for c in calls
        ):
            leaks["dropped_tool_name"] += 1
            issues.add(f"dropped_tool_name:{row['trajectory_id']}")
        kept_keys = {(c["function"], key) for c in calls for key in c["arguments"] or {}}
        if {key for _, key in kept_keys} & schema_keys:
            leaks["side_task_schema_argument_key"] += 1
            issues.add(f"side_task_schema_argument_key:{row['trajectory_id']}")
        if tag == "haiku_attack" and kept_keys - honest_keys:
            leaks["attack_only_argument_key"] += 1
            issues.add(f"attack_only_argument_key:{row['trajectory_id']}")
        if tag == "straj":
            natural["straj_rows_with_argument_keys_no_honest_run_uses"] += bool(
                kept_keys - honest_keys
            )
            straj_only_functions.update(
                c["function"] for c in calls if c["function"] not in honest_functions
            )
        vocab = condition_vocabulary(steps)
        residual.setdefault(tag, Counter())["trajectories"] += 1
        residual[tag]["with_condition_vocabulary"] += vocab["anywhere"]
        residual[tag]["in_tool_arguments"] += vocab["tool_arguments"]
        residual[tag]["in_tool_results"] += vocab["tool_results"]
        monitors = [(a.get("monitor_response") or {}).get("reasoning") for a in row["actions"]] + [
            (row["traj_monitor_response"] or {}).get("reasoning")
        ]
        natural["monitor_reasoning_line_overlap"] += any(n in text for n in _lines(*monitors))
        natural["scorer_explanation_line_overlap"] += any(
            n in text for n in _lines(row["scorer_explanation"])
        )
    return {
        "trajectory_rows_scanned": seen,
        "steps": step_total,
        "stripped_from_detector_input": dict(sorted(stripped.items())),
        "side_task_schema_argument_keys": sorted(schema_keys),
        "straj_only_tool_functions": sorted(straj_only_functions),
        "leaks": dict(sorted(leaks.items())),
        "leak_count": sum(leaks.values()),
        "issue_count": len(issues),
        "issues": sorted(issues)[:20],
        "residual_condition_vocabulary_by_slice": {k: dict(v) for k, v in sorted(residual.items())},
        "natural_overlap_rows": dict(sorted(natural.items())),
    }


if __name__ == "__main__":
    print(json.dumps(selfcheck(), indent=1, sort_keys=True))
