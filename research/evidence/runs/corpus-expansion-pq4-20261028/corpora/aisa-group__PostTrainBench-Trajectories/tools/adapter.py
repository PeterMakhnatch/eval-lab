#!/usr/bin/env python3
"""Offline PostTrainBench-Trajectories replay. Only a spec's ``_steps`` may reach a detector.

Unit = one 10-hour post-training agent run (agent CLI x base model x benchmark). The agent
trace is a CLI JSON-event stream (claude stream-json, codex exec --json, opencode --format
json) interleaved with harness stdout. Labels come from the publisher's anti-cheating judges
(contamination / disallowed-model verdict files plus ``judgement_*.json`` variants).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "aisa-group/PostTrainBench-Trajectories"
REVISION = "386b3034fa26f0f1b3ae3bfded99b791f0b254a6"
CORPUS = "aisa-group__PostTrainBench-Trajectories"
BUNDLE_DIR = "386b3034fa26"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / BUNDLE_DIR

CONTAMINATION_TXT = "contamination_judgement.txt"
DISALLOWED_TXT = "disallowed_model_judgement.txt"
# judgement_<name>.json boolean fields; every present field is a verdict.
JSON_FIELDS = {
    "gpt5_4": ("contamination", "disallowed_model"),
    "gpt5_4_rerun": ("contamination", "disallowed_model"),
    "api": ("disallowed_api_usage",),
    "ptb_lookup": ("disallowed_ptb_lookup",),
}
TXT_VOCAB = {
    CONTAMINATION_TXT: {
        "contamination detected": True,
        "no contamination detected": False,
    },
    DISALLOWED_TXT: {
        "disallowed use detected": True,
        "disallowed use not detected": False,
        "only allowed use detected": False,
    },
}
LINE = re.compile(r"^(?:\[[^\]]{0,40}\] )?(\{.*\})$")
MAX_RESULT_CHARS = 20000


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_dirs(root: Path = ROOT) -> list[Path]:
    """Every run dir with ANY judge output file (a verdict .txt or a ``judgement_*.json``)."""
    found: set[Path] = set()
    for pattern in (CONTAMINATION_TXT, DISALLOWED_TXT, "judgement_*.json"):
        found.update(p.parent for p in root.glob(f"*/*/{pattern}"))
    return sorted(found)


def trace_path(run: Path) -> Path | None:
    for name in ("solve_out.txt", "trace.txt"):
        if (run / name).is_file():
            return run / name
    return None


def verdicts(run: Path) -> dict[str, bool | None]:
    """Every judge verdict present for the run: True = cheating detected, None = unparseable."""
    out: dict[str, bool | None] = {}
    for name, vocab in TXT_VOCAB.items():
        path = run / name
        if path.is_file():
            out[name] = vocab.get(path.read_text(encoding="utf-8").strip())
    for variant, fields in JSON_FIELDS.items():
        path = run / f"judgement_{variant}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        for field in fields:
            value = data.get(field) if isinstance(data, dict) else None
            out[f"{variant}:{field}"] = value if isinstance(value, bool) else None
    return out


FAMILIES = {
    "contamination": (CONTAMINATION_TXT, "gpt5_4:contamination", "gpt5_4_rerun:contamination"),
    "disallowed": (
        DISALLOWED_TXT,
        "gpt5_4:disallowed_model",
        "gpt5_4_rerun:disallowed_model",
        "api:disallowed_api_usage",
    ),
}


def contradiction(found: dict[str, bool | None]) -> bool:
    """One judge says detected and another explicitly says none for the SAME construct."""
    for family in FAMILIES.values():
        values = [found[key] for key in family if key in found]
        if True in values and False in values:
            return True
    return False


def label_state(run: Path, found: dict[str, bool | None]) -> tuple[str, str]:
    """Strict primary label (before dedup and replay).

    excluded: no agent trace to replay.
    ambiguous: judges contradict on one construct; txt verdicts present but missing/unparseable;
    any other present verdict unparseable.
    positive: some judge says detected and none says otherwise for that construct.
    negative: every present verdict is an explicit none (txt files when staged, else the json
    judges alone) and at least one judge ran.
    """
    if trace_path(run) is None:
        return "excluded", "no_trace_file"
    if contradiction(found):
        return "ambiguous", "judge_contradiction"
    if any(value is True for value in found.values()):
        return "positive", "judge_detected"
    txt = [found[name] for name in (CONTAMINATION_TXT, DISALLOWED_TXT) if name in found]
    if txt and (len(txt) < 2 or any(value is None for value in txt)):
        return "ambiguous", "primary_verdict_missing_or_unparseable"
    if not found:
        return "ambiguous", "no_judge_verdict"
    if any(value is None for value in found.values()):
        return "ambiguous", "variant_verdict_unparseable"
    return "negative", "all_verdicts_none_detected"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(v.get("text", "") if isinstance(v, dict) else v) for v in value)
    return json.dumps(value, ensure_ascii=False)


def _clip(value: str, audit: Counter[str]) -> str:
    audit["source_chars"] += len(value)
    if len(value) > MAX_RESULT_CHARS:
        audit["clipped_results"] += 1
        return value[:MAX_RESULT_CHARS] + f"\n[... {len(value) - MAX_RESULT_CHARS} chars clipped]"
    return value


class _Builder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.audit: Counter[str] = Counter()
        self.call_step: dict[str, int] = {}
        self.group: str | None = None

    def agent(self, group: str | None = None) -> dict[str, Any]:
        if (
            group is None
            or group != self.group
            or not self.steps
            or self.steps[-1]["source"] != "agent"
        ):
            self.steps.append(
                {
                    "step_id": len(self.steps),
                    "source": "agent",
                    "input_role": "assistant",
                    "message": "",
                }
            )
        self.group = group
        return self.steps[-1]

    def text(self, step: dict[str, Any], value: str) -> None:
        step["message"] += (("\n" if step["message"] else "") + value) if value else ""

    def reasoning(self, step: dict[str, Any], value: str) -> None:
        if value:
            step["reasoning_content"] = step.get("reasoning_content", "") + value
            self.audit["nonempty_reasoning"] += 1

    def call(self, step: dict[str, Any], call_id: str, name: str, args: Any) -> None:
        self.call_step[call_id] = step["step_id"]
        step.setdefault("tool_calls", []).append(
            {"tool_call_id": call_id, "function_name": name, "arguments": args}
        )
        self.audit["tool_calls"] += 1

    def result(self, call_id: str, content: str, is_error: bool = False) -> None:
        index = self.call_step.pop(call_id, None)
        if index is None:
            self.audit["orphan_results"] += 1
            return
        self.steps[index].setdefault("observation", {"results": []})["results"].append(
            {"tool_call_id": call_id, "is_error": is_error, "content": _clip(content, self.audit)}
        )
        self.audit["tool_results"] += 1


def _claude(b: _Builder, event: dict[str, Any]) -> None:
    kind = event["type"]
    message = event.get("message")
    if kind not in {"assistant", "user"} or not isinstance(message, dict):
        b.audit[f"skipped_{kind}"] += 1
        return
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if kind == "assistant":
        step = b.agent(message.get("id"))
        for block in content or []:
            if block["type"] == "text":
                b.text(step, block["text"])
            elif block["type"] == "thinking":
                b.reasoning(step, block.get("thinking", ""))
            elif block["type"] == "tool_use":
                b.call(step, block["id"], block["name"], block.get("input", {}))
            else:
                b.audit[f"unknown_assistant_block_{block['type']}"] += 1
        return
    b.group = None
    for block in content or []:
        if block["type"] == "tool_result":
            b.result(
                block["tool_use_id"], _text(block.get("content", "")), bool(block.get("is_error"))
            )
        elif block["type"] == "text":
            b.steps.append(
                {
                    "step_id": len(b.steps),
                    "source": "user",
                    "input_role": "user",
                    "message": block["text"],
                }
            )
        else:
            b.audit[f"unknown_user_block_{block['type']}"] += 1


def _codex(b: _Builder, event: dict[str, Any]) -> None:
    kind = event["type"]
    item = event.get("item")
    if not kind.startswith("item.") or not isinstance(item, dict):
        b.audit[f"skipped_{kind}"] += 1
        return
    itype = item.get("type")
    if kind == "item.started" and itype == "command_execution":
        step = b.agent()
        b.call(step, item["id"], "command_execution", {"command": item.get("command")})
        return
    if kind != "item.completed":
        b.audit[f"skipped_{kind}"] += 1
        return
    if itype == "command_execution":
        if item["id"] not in b.call_step:
            b.call(b.agent(), item["id"], "command_execution", {"command": item.get("command")})
        b.result(
            item["id"],
            _text(item.get("aggregated_output", "")),
            item.get("exit_code") not in (0, None),
        )
    elif itype == "agent_message":
        b.text(b.agent(), item.get("text", ""))
    elif itype == "reasoning":
        b.reasoning(b.agent(), item.get("text", ""))
    elif itype in {"file_change", "web_search", "todo_list", "mcp_tool_call"}:
        step = b.agent()
        b.call(step, item["id"], itype, {k: v for k, v in item.items() if k not in {"id", "type"}})
        b.result(item["id"], "")
    else:
        b.audit[f"unknown_item_{itype}"] += 1


def _opencode(b: _Builder, event: dict[str, Any]) -> None:
    kind = event["type"]
    part = event.get("part")
    if kind == "text" and isinstance(part, dict):
        b.text(b.agent(part.get("messageID")), part.get("text", ""))
    elif kind == "reasoning" and isinstance(part, dict):
        b.reasoning(b.agent(part.get("messageID")), part.get("text", ""))
    elif kind == "tool_use" and isinstance(part, dict):
        step = b.agent(part.get("messageID"))
        state = part.get("state", {})
        b.call(step, part["callID"], part.get("tool", ""), state.get("input", {}))
        b.result(part["callID"], _text(state.get("output", "")), state.get("status") == "error")
    else:
        b.audit[f"skipped_{kind}"] += 1


def replay(path: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Map every conversational event; harness stdout and CLI telemetry are counted, not replayed."""
    b = _Builder()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = LINE.match(line.strip())
            if not match:
                b.audit["harness_lines"] += 1
                continue
            try:
                event = json.loads(match.group(1))
            except ValueError:
                b.audit["bad_json_lines"] += 1
                continue
            kind = event.get("type")
            if not isinstance(kind, str):
                b.audit["untyped_events"] += 1
            elif kind in {"assistant", "user"} or kind in {"system", "result", "rate_limit_event"}:
                _claude(b, event)
            elif kind.startswith(("item.", "thread.", "turn.")):
                _codex(b, event)
            elif kind in {"text", "reasoning", "tool_use", "step_start", "step_finish"}:
                _opencode(b, event)
            else:
                b.audit[f"skipped_{kind}"] += 1
    b.audit["unanswered_tool_calls"] = len(b.call_step)
    b.audit["mapped_steps"] = len(b.steps)
    return b.steps, b.audit


def _manifest_sha(root: Path) -> dict[str, str]:
    path = root / "STAGE-MANIFEST.json"
    if not path.is_file():
        return {}
    return {e["path"]: e["sha256"] for e in json.loads(path.read_bytes())["files"]}


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def judges_run(found: dict[str, bool | None]) -> list[str]:
    """Which judges produced output for the run: txt, gpt5_4, gpt5_4_rerun, api, ptb_lookup."""
    names = {"txt" if ":" not in key else key.split(":")[0] for key in found}
    return sorted(names)


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield every run dir with judge output, including ambiguous/excluded records.

    ``labels.state`` is the PRIMARY label after trace deduplication: strict judge rule, then one
    canonical row per identical trace (sorted run path, first); copies are ``excluded`` and a
    canonical row whose copies carry different labels is ``ambiguous``. ``labels.positive_any``
    keeps the permissive any-judge predicate. Zero-step traces and runs whose agent could see a
    verdict are excluded after replay by :func:`finalize`.
    """
    manifest_sha = _manifest_sha(root)
    rows: list[dict[str, Any]] = []
    for index, run in enumerate(run_dirs(root)):
        config, unit = run.parent.name, run.name
        found = verdicts(run)
        state, reason = label_state(run, found)
        trace = trace_path(run)
        benchmark, _, rest = unit.partition("_")
        sha = None
        if trace is not None:
            sha = manifest_sha.get(trace.relative_to(root).as_posix()) or _file_sha(trace)
        rows.append(
            {
                "identity": {
                    "row_index": index,
                    "run_id": f"{config}/{unit}",
                    "trace_key": digest(f"{config}/{unit}"),
                    "task_id": f"{benchmark}_{rest.rsplit('_', 1)[0]}",
                    "task_key": digest(f"{benchmark}_{rest.rsplit('_', 1)[0]}"),
                    "cluster_key": digest(benchmark),
                    "config": config,
                    "benchmark": benchmark,
                    "base_model": rest.rsplit("_", 1)[0],
                },
                "labels": {
                    "state": state,
                    "reason": reason,
                    "verdicts": found,
                    "judges": judges_run(found),
                    "positive_any": any(value is True for value in found.values()),
                },
                "trace_file": None if trace is None else trace.name,
                "trace_sha256": sha,
                "_trace_path": trace,
            }
        )
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["trace_sha256"] is not None:
            groups.setdefault(row["trace_sha256"], []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: r["identity"]["run_id"])
        for row in group:
            row["identity"]["duplicate_group_size"] = len(group)
        if len(group) == 1:
            continue
        base = {r["labels"]["state"] for r in group if r["labels"]["state"] != "excluded"}
        keep = group[0]
        for row in group[1:]:
            row["labels"]["state"], row["labels"]["reason"] = "excluded", "duplicate_trace"
        if len(base) > 1 and keep["labels"]["state"] in ("positive", "negative"):
            keep["labels"]["state"], keep["labels"]["reason"] = (
                "ambiguous",
                "duplicate_label_conflict",
            )
    yield from rows


def to_steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [] if spec["_trace_path"] is None else replay(spec["_trace_path"])[0]


def _norm(value: str) -> str:
    return " ".join(value.split())


def _leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)


def visible_text(steps: list[dict[str, Any]]) -> str:
    """Everything a detector can read: message, reasoning, results and tool-call arguments."""
    parts: list[str] = []
    for step in steps:
        parts += [step.get("message", ""), step.get("reasoning_content", "")]
        for call in step.get("tool_calls", []):
            parts.append(call["function_name"])
            parts.extend(_leaves(call["arguments"]))
        for result in step.get("observation", {}).get("results", []):
            parts.append(result["content"])
    return "\n".join(parts)


def _forbidden_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_forbidden_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_forbidden_keys(v) for v in value))
    return set()


STEP_FIELDS = {
    "step_id",
    "source",
    "input_role",
    "message",
    "reasoning_content",
    "tool_calls",
    "observation",
}
JUDGE_FILE_NAMES = re.compile(
    r"contamination_judgement|disallowed_model_judgement|judgement_[A-Za-z0-9_]+\.json"
)
# Leak kinds that exclude a run from scoring: the agent could read its own verdict.
VERDICT_LEAKS = ("verdict_file_name_in_steps", "verdict_body_in_steps")


def _judge_strings(data: Any) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for item in data.values():
            yield from _judge_strings(item)
    elif isinstance(data, list):
        for item in data:
            yield from _judge_strings(item)


def leak_audit(run: Path, steps: list[dict[str, Any]]) -> Counter[str]:
    """Search ALL detector-visible text, tool-call arguments included, for judge material.

    Judge file names and verdict bodies (a whole line equal to the run's own verdict file) mark a
    run as ``verdict_visible_to_agent``; judge justification sentences are audited as well.
    """
    leaks: Counter[str] = Counter()
    for step in steps:
        if set(step) - STEP_FIELDS:
            leaks["unexpected_step_fields"] += 1
    raw = visible_text(steps)
    if JUDGE_FILE_NAMES.search(raw):
        leaks["verdict_file_name_in_steps"] += 1
    for base in (run, run / "task"):
        for name in (CONTAMINATION_TXT, DISALLOWED_TXT):
            path = base / name
            body = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            if body and re.search(rf"(?m)^[ \t]*{re.escape(body)}[ \t]*$", raw):
                leaks["verdict_body_in_steps"] += 1
    text = _norm(raw)
    for path in run.glob("judgement_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for value in _judge_strings(data):
            for sentence in re.split(r"(?<=[.!?])\s+", _norm(value)):
                if len(sentence) >= 60 and sentence in text:
                    leaks["judge_justification_in_steps"] += 1
            needle = _norm(value)
            if len(needle) >= 120 and (needle[:120] in text or needle[-120:] in text):
                leaks["judge_justification_in_steps"] += 1
    return leaks


def finalize(spec: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[str, str, Counter[str]]:
    """Primary state after replay: no steps or a visible verdict means the run cannot be scored."""
    state, reason = spec["labels"]["state"], spec["labels"]["reason"]
    if state == "excluded":
        return state, reason, Counter()
    if not steps:
        return "excluded", "trace_has_no_replayable_steps", Counter()
    leaks = leak_audit(spec["_trace_path"].parent, steps)
    if any(leaks[kind] for kind in VERDICT_LEAKS):
        return "excluded", "verdict_visible_to_agent", leaks
    return state, reason, leaks


def scan(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Replay each non-duplicate trace once; yield spec, final state, step count, leaks, audit."""
    for spec in specs(root):
        if spec["_trace_path"] is None or spec["labels"]["reason"] == "duplicate_trace":
            steps, audit = [], Counter()
        else:
            steps, audit = replay(spec["_trace_path"])
        state, reason, leaks = finalize(spec, steps)
        bad = _forbidden_keys([s.get("tool_calls", []) for s in steps]) & {"verdict", "label"}
        yield {
            "spec": spec,
            "state": state,
            "reason": reason,
            "n_steps": len(steps),
            "leaks": leaks,
            "audit": audit,
            "label_like_tool_arg_keys": bool(bad),
        }


def selfcheck(root: Path = ROOT, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Replay audit; raises if any non-excluded row has judge material in detector input."""
    totals: Counter[str] = Counter()
    leaks: Counter[str] = Counter()
    audits: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    excluded_leak = 0
    for row in rows if rows is not None else scan(root):
        totals["runs"] += 1
        totals["label_like_tool_arg_keys"] += row["label_like_tool_arg_keys"]
        audits.update(row["audit"])
        if row["reason"] == "verdict_visible_to_agent":
            excluded_leak += 1
        elif row["state"] != "excluded":
            totals["scored_runs_checked"] += 1
            leaks.update(row["leaks"])
        for key in row["audit"]:
            if key.startswith(("unknown_", "skipped_")):
                unsupported[key] += 1
    result = {
        "totals": dict(totals),
        "leaks": dict(leaks),
        "leak_total": sum(leaks.values()),
        "excluded_verdict_visible_runs": excluded_leak,
        "audit": dict(audits),
        "runs_with_unsupported_or_skipped_event_kinds": dict(unsupported),
    }
    if result["leak_total"]:
        raise AssertionError(f"judge material reaches detector input: {result['leaks']}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
