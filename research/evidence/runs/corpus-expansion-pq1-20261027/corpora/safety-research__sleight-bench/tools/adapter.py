"""Local-only SleightBench conversion. CLI output is aggregates, never transcripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

CORPUS = "safety-research__sleight-bench"
REVISION = "218c58315cc01ff0dc5a100e906c27d82d259521"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]


def key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def segment_paths(root: Path = ROOT) -> list[Path]:
    return sorted(
        path.parent
        for path in (root / "attacks").glob("*/*/encrypted.bin")
        if path.parent.parent.name != "refusal_forcing"
    )


def group_id(segment: Path) -> str:
    # The three two-segment constructions use both -stageN and -partN suffixes.
    return re.sub(r"-(?:stage|part)[12]$", "", segment.name)


def record_id(metadata: dict[str, Any], arm: str) -> str:
    return metadata["uuid"] + ":" + arm


def read_blocks(path: Path) -> list[tuple[int, dict[str, Any]]]:
    blocks = []
    for index, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if set(row) == {"canary"}:
            continue
        if set(row) - {"role", "content", "cwd"}:
            raise ValueError("unexpected block fields")
        blocks.append((index, row))
    return blocks


def to_steps(blocks: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Preserve block order, text and tool inputs; link results by tool-call id."""
    steps: list[dict[str, Any]] = []
    tool_ids: set[str] = set()
    for index, block in blocks:
        role = block["role"]
        if role not in {"assistant", "user"}:
            raise ValueError("unsupported source role")
        content = block["content"]
        kind = content["type"]
        if kind == "tool_result":
            call_id = content["tool_use_id"]
            if call_id not in tool_ids:
                raise ValueError("tool result has no prior call")
            value = content["content"]
            if isinstance(value, list):
                if len(value) != 1 or set(value[0]) != {"type", "text"}:
                    raise ValueError("unsupported result block list")
                if value[0]["type"] != "text":
                    raise ValueError("non-text result")
                text, content_format = value[0]["text"], "text_block_list"
            elif isinstance(value, str):
                text, content_format = value, "string"
            else:
                raise ValueError("unsupported result content")
            result = {
                "tool_call_id": call_id,
                "content": text,
                "content_format": content_format,
                "source_block_index": index,
                "source_role": role,
            }
            if "cwd" in block:
                result["cwd"] = block["cwd"]
            if "is_error" in content:
                result["is_error"] = content["is_error"]
            steps[-1].setdefault("observation", {"results": []})["results"].append(result)
            continue
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": "agent" if role == "assistant" else "user",
            "message": "",
            "reasoning_content": "",
            "tool_calls": [],
            "block_type": kind,
            "source_block_index": index,
        }
        if "cwd" in block:
            step["cwd"] = block["cwd"]
        if kind == "text":
            step["message"] = content["text"]
        elif kind == "thinking":
            step["reasoning_content"] = content["thinking"]
        elif kind == "tool_use":
            name = content["name"]
            arguments = deepcopy(content["input"])
            if name == "Bash":
                if "keystrokes" in arguments:
                    raise ValueError("ambiguous command argument")
                arguments["keystrokes"] = arguments.pop("command")
                name = "bash_command"
            call = {
                "tool_call_id": content["id"],
                "function_name": name,
                "arguments": arguments,
            }
            step["tool_calls"].append(call)
            if content["id"] in tool_ids:
                raise ValueError("duplicate tool call id")
            tool_ids.add(content["id"])
        else:
            raise ValueError("unsupported content type")
        steps.append(step)
    return steps


def reconstructed_blocks(steps: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Round-trip check, excluding only canary headers and thinking signatures."""
    restored = []
    for step in steps:
        kind = step["block_type"]
        content: dict[str, Any] = {"type": kind}
        if kind == "text":
            content["text"] = step["message"]
        elif kind == "thinking":
            content["thinking"] = step["reasoning_content"]
        else:
            call = step["tool_calls"][0]
            name, arguments = call["function_name"], deepcopy(call["arguments"])
            if name == "bash_command":
                name = "Bash"
                arguments["command"] = arguments.pop("keystrokes")
            content.update(id=call["tool_call_id"], name=name, input=arguments)
        row = {
            "role": "assistant" if step["source"] == "agent" else "user",
            "content": content,
        }
        if "cwd" in step:
            row["cwd"] = step["cwd"]
        restored.append((step["source_block_index"], row))
        for result in step.get("observation", {}).get("results", []):
            value = result["content"]
            if result["content_format"] == "text_block_list":
                value = [{"type": "text", "text": value}]
            result_content = {
                "type": "tool_result",
                "tool_use_id": result["tool_call_id"],
                "content": value,
            }
            if "is_error" in result:
                result_content["is_error"] = result["is_error"]
            row = {"role": result["source_role"], "content": result_content}
            if "cwd" in result:
                row["cwd"] = result["cwd"]
            restored.append((result["source_block_index"], row))
    return sorted(restored, key=lambda item: item[0])


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Only `_steps` may enter the detector; all other fields are scoring metadata."""
    for segment in segment_paths(root):
        metadata = json.loads((segment / "metadata.json").read_text())
        for arm, filename in (("attack", "transcript.jsonl"), ("benign", "benign.jsonl")):
            trace_key = key(record_id(metadata, arm))
            yield {
                "corpus": CORPUS,
                "trace_id": trace_key,
                "trace_key": trace_key,
                "positive": arm == "attack",
                "pair_key": key(group_id(segment)),
                "segment_pair_key": key(metadata["uuid"]),
                "labels": {
                    "category_key": key(segment.parent.name),
                    "scenario_key": key(metadata["scenario"]),
                    "outcome_key": key(metadata["outcome"]),
                    "tag_keys": [key(tag) for tag in metadata["tags"]],
                },
                "_steps": to_steps(read_blocks(segment / filename)),
            }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    issues: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    seen_records: set[str] = set()
    pair_arms: dict[str, Counter[bool]] = {}
    canaries: set[str] = set()
    for segment in segment_paths(root):
        metadata = json.loads((segment / "metadata.json").read_text())
        for arm, filename in (("attack", "transcript.jsonl"), ("benign", "benign.jsonl")):
            counts["rows"] += 1
            counts["positives" if arm == "attack" else "negatives"] += 1
            trace_key = key(record_id(metadata, arm))
            issues["duplicate_trace_key"] += trace_key in seen_records
            seen_records.add(trace_key)
            pair_arms.setdefault(key(group_id(segment)), Counter())[arm == "attack"] += 1
            path = segment / filename
            for line in path.read_text().splitlines():
                if not line.strip():
                    counts["blank_lines_ignored"] += 1
                    continue
                row = json.loads(line)
                if set(row) == {"canary"}:
                    counts["canary_headers_removed"] += 1
                    canaries.add(row["canary"])
            blocks = read_blocks(path)
            try:
                steps = to_steps(blocks)
                expected = deepcopy(blocks)
                for _, block in expected:
                    content = block["content"]
                    if "signature" in content:
                        counts["thinking_signatures_removed"] += 1
                        content.pop("signature")
                    if content["type"] == "tool_result" and isinstance(content["content"], list):
                        counts["single_text_result_wrappers_normalized"] += 1
                issues["round_trip_mismatch"] += reconstructed_blocks(steps) != expected
                issues["nonsequential_step_ids"] += [s["step_id"] for s in steps] != list(
                    range(1, len(steps) + 1)
                )
                for step, following in zip(steps, steps[1:], strict=False):
                    issues["observation_order_mismatch"] += any(
                        result["source_block_index"] >= following["source_block_index"]
                        for result in step.get("observation", {}).get("results", [])
                    )
                counts["source_blocks"] += len(blocks)
                counts["steps"] += len(steps)
                counts["tool_calls"] += sum(len(s["tool_calls"]) for s in steps)
                counts["tool_results"] += sum(
                    len(s.get("observation", {}).get("results", [])) for s in steps
                )
                call_ids = {call["tool_call_id"] for step in steps for call in step["tool_calls"]}
                result_ids = {
                    result["tool_call_id"]
                    for step in steps
                    for result in step.get("observation", {}).get("results", [])
                }
                counts["tool_calls_without_result"] += len(call_ids - result_ids)
                issues["canary_in_detector_steps"] += any(
                    canary in json.dumps(steps, ensure_ascii=False) for canary in canaries
                )
            except (KeyError, ValueError, TypeError, IndexError):
                issues["conversion_error"] += 1
    for arms in pair_arms.values():
        issues["pair_arm_mismatch"] += arms[True] != arms[False]
    counts["pairs"] = len(pair_arms)
    counts["segments"] = len(segment_paths(root))
    issues["publisher_count_mismatch"] += (
        counts["rows"],
        counts["positives"],
        counts["negatives"],
        counts["pairs"],
        counts["segments"],
    ) != (86, 43, 43, 40, 43)
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "issues": sum(issues.values()),
        "issue_counts": {name: count for name, count in sorted(issues.items()) if count},
        **dict(counts),
    }


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
