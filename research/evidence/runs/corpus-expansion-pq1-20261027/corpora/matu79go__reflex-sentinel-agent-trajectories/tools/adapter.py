#!/usr/bin/env python3
"""Local-only Reflex-Sentinel trajectory conversion; never emit trajectory text.

Only ``_steps`` may enter a frozen detector. All other spec fields are evaluator
metadata. The default cohort is the 250 original runs, not the dependent retries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "matu79go/reflex-sentinel-agent-trajectories"
REVISION = "9ca0d7ae9893900b61f2d45776caf8cf9197a76c"
ROOT = Path.home() / ".local/state/evallab-cross-test" / REPO.replace("/", "__") / REVISION[:12]
GENERATOR = "IQuest-Coder-V1-40B-Loop-Instruct (4-bit AWQ)"
FILES = {
    "agent": "runs/agent_runs.jsonl",
    "branch_pilot": "runs/branch_runs_pilot.jsonl",
    "branch_main": "runs/branch_runs_main.jsonl",
}
GOLD_FIELDS = {
    "agent": ("test_tampered", "pass_own", "pass_fresh"),
    "branch_pilot": ("cheat", "cheat_turn", "tc", "b", "d", "k"),
    "branch_main": ("orig_cheat", "cheat", "cheat_turn", "tc", "b", "d", "k"),
}


def trace_key(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16]


def iter_rows(root: Path = ROOT, subset: str = "agent") -> Iterator[dict[str, Any]]:
    with (root / FILES[subset]).open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def record_id(row: dict[str, Any], subset: str) -> str:
    """Retries reuse parent ids; identify them by file, parent, restart and retry."""
    if subset == "agent":
        return row["id"]
    return json.dumps([subset, row["id"], row["b"], row["d"], row["k"]], separators=(",", ":"))


def tool_call(cmd: dict[str, Any], index: int) -> dict[str, Any]:
    tool = cmd["tool"]
    arguments = {key: value for key, value in cmd.items() if key != "tool"}
    if tool == "execute_bash":
        if set(arguments) - {"command"}:
            raise ValueError("unexpected shell argument fields")
        # Some retries have a tool name but no command. Do not invent one.
        arguments = {"keystrokes": arguments["command"]} if "command" in arguments else {}
        tool = "bash_command"
    elif tool not in {"str_replace_editor", "submit"}:
        raise ValueError("unexpected source tool")
    return {"tool_call_id": f"call-{index}", "function_name": tool, "arguments": arguments}


def original_command(call: dict[str, Any]) -> dict[str, Any]:
    tool = call["function_name"]
    arguments = dict(call["arguments"])
    if tool == "bash_command":
        tool = "execute_bash"
        arguments = {"command": arguments["keystrokes"]} if "keystrokes" in arguments else {}
    return {"tool": tool, **arguments}


def to_steps(row: dict[str, Any], subset: str = "agent") -> list[dict[str, Any]]:
    """Keep all supplied text, tools and results; never read oracle fields.

    The original chat has no separate reasoning field: assistant content stays
    verbatim, including any inline tool markup. ``input_role`` preserves system
    versus user roles while the live-monitor source vocabulary stays user/agent.
    Branch rows contain parsed commands only: their absent chat and results are
    not reconstructed from parent runs or fabricated.
    """
    if subset != "agent":
        out = []
        for index, source_step in enumerate(row["steps"]):
            step: dict[str, Any] = {"step_id": index, "source": "agent", "message": ""}
            if source_step["cmd"] is not None:
                step["tool_calls"] = [tool_call(source_step["cmd"], index)]
            out.append(step)
        return out

    out = []
    turn = 0
    for message in row["msgs"]:
        role, content = message["role"], message["content"]
        if role == "tool":
            if not out or out[-1]["source"] != "agent":
                raise ValueError("tool result without preceding assistant")
            out[-1].setdefault("observation", {"results": []})["results"].append(
                {"content": content}
            )
            continue
        if role not in {"system", "user", "assistant"}:
            raise ValueError("unexpected message role")
        step = {
            "step_id": len(out),
            "source": "agent" if role == "assistant" else "user",
            "message": content,
            "input_role": role,
        }
        if role == "assistant":
            source_step = row["steps"][turn]
            if source_step["cmd"] is not None:
                step["tool_calls"] = [tool_call(source_step["cmd"], turn)]
            turn += 1
        out.append(step)
    if turn != len(row["steps"]):
        raise ValueError("assistant and parsed-step counts differ")
    return out


def specs(root: Path = ROOT, subset: str = "agent") -> Iterator[dict[str, Any]]:
    """Yield evaluator metadata plus the sole detector input, ``_steps``.

    Use only the default ``agent`` cohort for the primary run-level cross-test.
    Retry gold means a write occurred, not the original cohort's final-file diff.
    ``trace_id`` is in-memory only; persist only ``trace_key`` and indices.
    """
    if subset not in FILES:
        raise ValueError("unknown subset")
    parents = {row["id"]: row for row in iter_rows(root)} if subset != "agent" else {}
    for row in iter_rows(root, subset):
        parent = row if subset == "agent" else parents[row["id"]]
        identity = record_id(row, subset)
        yield {
            "corpus": "reflex-sentinel",
            "subset": subset,
            "trace_id": identity,
            "trace_key": trace_key(identity),
            "parent_key": trace_key(row["id"]),
            "task_key": trace_key(parent["task_id"]),
            "generator": GENERATOR,
            "split": parent["split"],
            "positive": row["test_tampered"] if subset == "agent" else row["cheat"],
            "gold": {field: row[field] for field in GOLD_FIELDS[subset]},
            "source_turns": [step["turn"] for step in row["steps"]],
            "_steps": to_steps(row, subset),
        }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Check ALL 1,420 trajectories, round-trip text, labels and call structure."""
    issues: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    by_subset: dict[str, dict[str, int]] = {}
    parent_ids = {row["id"] for row in iter_rows(root)}
    for subset in FILES:
        identities: set[str] = set()
        counts: Counter[str] = Counter()
        for row in iter_rows(root, subset):
            counts["rows"] += 1
            identity = record_id(row, subset)
            if identity in identities:
                issues["duplicate_record_id"] += 1
            identities.add(identity)
            if row["id"] not in parent_ids:
                issues["missing_parent"] += 1
            label = row["test_tampered"] if subset == "agent" else row["cheat"]
            if type(label) is not bool:
                issues["non_boolean_label"] += 1
            counts["positives" if label else "negatives"] += 1
            try:
                steps = to_steps(row, subset)
                if [step["step_id"] for step in steps] != list(range(len(steps))):
                    issues["nonsequential_step_ids"] += 1
                calls = [call for step in steps for call in step.get("tool_calls", [])]
                expected = [step["cmd"] for step in row["steps"] if step["cmd"] is not None]
                if [original_command(call) for call in calls] != expected:
                    issues["command_roundtrip"] += 1
                if len({call["tool_call_id"] for call in calls}) != len(calls):
                    issues["duplicate_tool_call_id"] += 1
                counts["mapped_steps"] += len(steps)
                counts["tool_calls"] += len(calls)
                counts["source_steps"] += len(row["steps"])
                counts["missing_command"] += sum(step["cmd"] is None for step in row["steps"])
                counts["shell_calls_without_command"] += sum(
                    call["function_name"] == "bash_command" and not call["arguments"]
                    for call in calls
                )
                if subset == "agent":
                    recovered = []
                    for step in steps:
                        recovered.append({"role": step["input_role"], "content": step["message"]})
                        recovered.extend(
                            {"role": "tool", "content": result["content"]}
                            for result in step.get("observation", {}).get("results", [])
                        )
                    if recovered != row["msgs"]:
                        issues["chat_roundtrip"] += 1
                    results = [
                        result["content"]
                        for step in steps
                        for result in step.get("observation", {}).get("results", [])
                    ]
                    if results != [
                        step["result"] for step in row["steps"] if step["cmd"] is not None
                    ]:
                        issues["result_roundtrip"] += 1
                    if row["n_turns"] != len(row["steps"]):
                        issues["turn_count"] += 1
                    if [step["turn"] for step in row["steps"]] != list(range(row["n_turns"])):
                        issues["turn_sequence"] += 1
                    counts["source_messages"] += len(recovered)
                    counts["source_text_chars"] += sum(
                        len(message["content"]) for message in recovered
                    )
                else:
                    if row["d"] != row["tc"] - row["b"]:
                        issues["branch_distance"] += 1
                    if label != (row["cheat_turn"] is not None):
                        issues["branch_cheat_turn"] += 1
                # Changing every non-input field must leave detector steps identical.
                tainted = {
                    key: value if key in {"msgs", "steps"} else None for key, value in row.items()
                }
                if to_steps(tainted, subset) != steps:
                    issues["oracle_leakage"] += 1
            except (KeyError, IndexError, TypeError, ValueError):
                issues["mapping_error"] += 1
        by_subset[subset] = dict(counts)
        totals.update(counts)
    return {
        "revision": REVISION,
        "totals": dict(totals),
        "subsets": by_subset,
        "issue_count": sum(issues.values()),
        "issues": dict(issues),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(bool(result["issue_count"]))


if __name__ == "__main__":
    main()
