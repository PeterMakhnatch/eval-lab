#!/usr/bin/env python3
"""Local-only KernelBench Hard replay; never emit trajectory text or gold to a detector.

Only ``spec['_steps']`` is detector input. All remaining spec fields are evaluator
metadata, including native source-line mappings and final-solution gold spans.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

REPO = "Infatoshi/kernelbench-hard-runs"
REVISION = "a17d332b967d03868f10e12df97d7bbaa06bb1b8"
CORPUS = "infatoshi__kernelbench-hard-runs"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
VERDICTS = frozenset({"rubric_leak", "clean", "reward_hack", "interesting", "bug"})


def trace_key(record_id: str) -> str:
    return hashlib.sha256(record_id.encode()).hexdigest()[:16]


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    with (root / "runs.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def annotation(row: dict[str, Any], root: Path = ROOT) -> dict[str, Any] | None:
    """The index stores annotation basenames, not root-relative paths."""
    if not row["annotation_file"]:
        return None
    name = row["annotation_file"]
    if Path(name).name != name:
        raise ValueError("annotation path is not a basename")
    result = yaml.safe_load((root / "annotations" / name).read_text(encoding="utf-8"))
    if result["run_id"] != row["run_id"] or result["verdict"] not in VERDICTS:
        raise ValueError("annotation join or verdict is invalid")
    return result


def native_events(row: dict[str, Any], root: Path = ROOT) -> Iterator[tuple[int, Any]]:
    """Keep malformed Kimi footer lines verbatim; Codex files are plain text."""
    text = (root / row["transcript_path"]).read_text(encoding="utf-8")
    if row["harness"] == "codex":
        yield 1, text
        return
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield line_number, json.loads(line)
        except json.JSONDecodeError:
            yield line_number, line


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _call(name: str, arguments: Any, call_id: str) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"raw_arguments": arguments}
    arguments = {"raw_arguments": arguments} if not isinstance(arguments, dict) else dict(arguments)
    # Preserve every argument, including flags, rather than collapsing all tools
    # into shell commands. Only the three native shell names are normalized.
    if name in {"bash", "Bash", "Shell"}:
        name = "bash_command"
        if "command" in arguments:
            arguments["keystrokes"] = arguments.pop("command")
    return {"tool_call_id": call_id, "function_name": name, "arguments": arguments}


def event_step(event: Any, harness: str) -> dict[str, Any] | None:
    """Normalize semantic payloads; omit only transport/accounting envelopes."""
    if isinstance(event, str):
        # No role or command boundaries can honestly be inferred from these logs.
        return {"source": "agent", "message": event}
    if harness == "opencode":
        kind = event["type"]
        if kind in {"step_start", "step_finish"}:
            return None
        if kind == "error":
            return {
                "source": "agent",
                "message": "",
                "observation": {"results": [{"content": _text(event["error"])}]},
            }
        part = event["part"]
        if kind == "text":
            return {"source": "agent", "message": part["text"]}
        if kind != "tool_use":
            raise ValueError("unrecognized OpenCode event type")
        state = part["state"]
        content = state["output"] if "output" in state else state["error"]
        # One native invalid-tool event has an empty callID. Its existing part id
        # provides a stable link without inventing an executed tool or command.
        call_id = part["callID"] or part["id"]
        return {
            "source": "agent",
            "message": "",
            "tool_calls": [_call(part["tool"], state["input"], call_id)],
            "observation": {"results": [{"tool_call_id": call_id, "content": content}]},
        }
    if harness == "claude":
        if event["type"] == "rate_limit_event":
            return None
        if event["type"] == "system":
            if event["subtype"] == "init":
                return None
            # Task notifications contain execution evidence, not gold summaries.
            payload = {
                key: value
                for key, value in event.items()
                if key not in {"type", "subtype", "session_id", "uuid"}
            }
            return {
                "source": "agent",
                "message": "",
                "observation": {"results": [{"content": _text(payload)}]},
            }
        message = event["message"]
    elif harness == "kimi":
        message = event
    else:
        raise ValueError("unrecognized transcript harness")
    role = message["role"]
    if role == "tool":
        return {
            "source": "agent",
            "message": "",
            "observation": {
                "results": [
                    {"tool_call_id": message["tool_call_id"], "content": _text(message["content"])}
                ]
            },
        }
    if role not in {"user", "assistant"}:
        raise ValueError("unrecognized native message role")
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    content = message.get("content", "")
    if isinstance(content, str):
        texts.append(content)
    else:
        for block in content:
            kind = block["type"]
            if kind == "text":
                texts.append(block["text"])
            elif kind in {"thinking", "think"}:
                thoughts.append(block.get("thinking", block.get("think", "")))
            elif kind == "tool_use":
                calls.append(_call(block["name"], block["input"], block["id"]))
            elif kind == "tool_result":
                results.append(
                    {"tool_call_id": block["tool_use_id"], "content": _text(block["content"])}
                )
            else:
                # Unknown textual/reference blocks remain visible as structured JSON.
                texts.append(_text(block))
    for call in message.get("tool_calls", []):
        calls.append(_call(call["function"]["name"], call["function"]["arguments"], call["id"]))
    step: dict[str, Any] = {
        "source": "agent" if role == "assistant" or (results and not texts) else "user",
        "message": "\n".join(texts),
    }
    if thoughts:
        step["reasoning_content"] = "\n".join(thoughts)
    if calls:
        step["tool_calls"] = calls
    if results:
        step["observation"] = {"results": results}
    return step


def to_steps(
    row: dict[str, Any], root: Path = ROOT
) -> tuple[list[dict[str, Any]], dict[int, list[int]]]:
    steps: list[dict[str, Any]] = []
    native_lines: dict[int, list[int]] = {}
    for line_number, event in native_events(row, root):
        step = event_step(event, row["harness"])
        if step is None:
            continue
        step["step_id"] = len(steps)
        native_lines[len(steps)] = [line_number, line_number]
        if row["harness"] == "codex":
            native_lines[len(steps)][1] = len(event.splitlines())
        steps.append(step)
    return steps, native_lines


def gold_evidence(
    row: dict[str, Any], gold: dict[str, Any] | None, root: Path = ROOT
) -> list[dict[str, Any]]:
    """Quotes refer to inclusive, one-based *artifact* lines, never replay steps."""
    spans = []
    for index, quote in enumerate((gold or {}).get("quotes", [])):
        relative = Path(quote["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe gold artifact path")
        path = root / "runs" / row["run_id"] / relative
        bounds = quote["lines"]
        line_count = len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0
        valid = (
            len(bounds) == 2
            and all(type(n) is int for n in bounds)
            and 1 <= bounds[0] <= bounds[1] <= line_count
        )
        spans.append(
            {
                "quote_index": index,
                "file_key": trace_key(quote["file"]),
                "lines": bounds,
                "valid": valid,
            }
        )
    return spans


def specs(root: Path = ROOT, annotated_only: bool = True) -> Iterator[dict[str, Any]]:
    """Default: exactly the 30 scoreable rows. Never pass the full spec to a detector."""
    for row in iter_rows(root):
        gold = annotation(row, root)
        if annotated_only and gold is None:
            continue
        verdict = gold["verdict"] if gold else None
        if verdict is not None and verdict not in {"rubric_leak", "clean"}:
            raise ValueError("pinned scoring recipe does not cover this verdict")
        steps, native_lines = to_steps(row, root)
        yield {
            "corpus": CORPUS,
            "trace_id": row["run_id"],
            "trace_key": trace_key(row["run_id"]),
            "positive": verdict == "rubric_leak" if verdict is not None else None,
            "verdict": verdict,
            "model": row["model_label"],
            "task": row["problem"],
            "harness": row["harness"],
            "gold_evidence": gold_evidence(row, gold, root),
            "native_lines_by_step": native_lines,
            "_steps": steps,
        }


def _payload_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _payload_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_strings(item)


def expected_payloads(event: Any, harness: str) -> Iterator[str]:
    """Independent coverage audit of source semantic strings, not transport fields."""
    if isinstance(event, str):
        yield event
    elif harness == "opencode":
        if event["type"] == "text":
            yield event["part"]["text"]
        elif event["type"] == "tool_use":
            state = event["part"]["state"]
            yield from _payload_strings(state["input"])
            yield state.get("output", state.get("error", ""))
        elif event["type"] == "error":
            yield from _payload_strings(event["error"])
    elif harness == "claude" and event["type"] in {"system", "rate_limit_event"}:
        if event["type"] == "system" and event["subtype"] != "init":
            for key, value in event.items():
                if key not in {"type", "subtype", "session_id", "uuid"}:
                    yield from _payload_strings(value)
    else:
        message = event["message"] if harness == "claude" else event
        content = message.get("content", "")
        if isinstance(content, str):
            yield content
        else:
            for block in content:
                if block["type"] in {"thinking", "think"}:
                    yield block.get("thinking", block.get("think", ""))
                elif block["type"] == "tool_use":
                    yield from _payload_strings(block["input"])
                elif block["type"] == "tool_result":
                    yield from _payload_strings(block["content"])
                else:
                    yield from _payload_strings({k: v for k, v in block.items() if k != "type"})
        for call in message.get("tool_calls", []):
            args = call["function"]["arguments"]
            if isinstance(args, str):
                with suppress(json.JSONDecodeError):
                    args = json.loads(args)
            yield from _payload_strings(args)


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Audit ALL rows. Known source limitations are counted separately, not hidden."""
    issues: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    keys: set[str] = set()
    for row in iter_rows(root):
        counts["rows"] += 1
        key = trace_key(row["run_id"])
        if key in keys:
            issues["duplicate_trace_key"] += 1
        keys.add(key)
        gold = annotation(row, root)
        if gold:
            counts["annotated"] += 1
            counts[gold["verdict"]] += 1
        else:
            counts["unlabeled"] += 1
        steps, line_map = to_steps(row, root)
        if not steps or [s["step_id"] for s in steps] != list(range(len(steps))):
            issues["step_ids"] += 1
        if set(line_map) != set(range(len(steps))):
            issues["native_line_mapping"] += 1
        counts["steps"] += len(steps)
        if row["harness"] == "codex":
            counts["opaque_plaintext_runs"] += 1
        for _, event in native_events(row, root):
            step = event_step(event, row["harness"])
            if step is None:
                counts["omitted_transport_events"] += 1
                continue
            if isinstance(event, str) and row["harness"] != "codex":
                counts["retained_nonjson_footer_lines"] += 1
            if (
                row["harness"] == "opencode"
                and isinstance(event, dict)
                and event["type"] == "tool_use"
                and not event["part"]["callID"]
            ):
                counts["native_empty_call_ids_using_part_id"] += 1
            # Include decoded JSON observation strings in this coverage check.
            visible = list(_payload_strings(step))
            for text in list(visible):
                with suppress(json.JSONDecodeError, TypeError):
                    visible.extend(_payload_strings(json.loads(text)))
            if any(
                text and not any(text in field for field in visible)
                for text in expected_payloads(event, row["harness"])
            ):
                issues["lost_semantic_text"] += 1
            if step["source"] not in {"agent", "user"}:
                issues["source"] += 1
            if set(step) - {
                "step_id",
                "source",
                "message",
                "reasoning_content",
                "tool_calls",
                "observation",
            }:
                issues["unexpected_detector_field"] += 1
            for call in step.get("tool_calls", []):
                counts["tool_calls"] += 1
                if (
                    not call["tool_call_id"]
                    or not call["function_name"]
                    or not isinstance(call["arguments"], dict)
                ):
                    issues["tool_call_shape"] += 1
            for result in step.get("observation", {}).get("results", []):
                if not isinstance(result["content"], str):
                    issues["observation_shape"] += 1
        for span in gold_evidence(row, gold, root):
            counts["gold_quotes"] += 1
            counts["valid_gold_ranges" if span["valid"] else "invalid_gold_ranges"] += 1
            if not span["valid"] and span["lines"] != [0]:
                issues["unexpected_invalid_gold_range"] += 1
    if {k: counts[k] for k in ("rows", "annotated", "rubric_leak", "clean", "unlabeled")} != {
        "rows": 84,
        "annotated": 30,
        "rubric_leak": 11,
        "clean": 19,
        "unlabeled": 54,
    }:
        issues["pinned_counts"] += 1
    if counts["invalid_gold_ranges"] != 1:
        issues["pinned_gold_limitation"] += 1
    return {"issues": sum(issues.values()), "issue_counts": dict(issues), "counts": dict(counts)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(bool(result["issues"]))


if __name__ == "__main__":
    main()
