"""Mechanical trajectory analysis for RLM runs (bench and Harbor).

Reads retained trajectories and answers, per policy, the questions the
2026-09-16 study had to answer by hand: how often the model drifted off dspy's
field markers and in which way, how many REPL errors occurred, which tools the
code actually called (sub-LM, environment bridge, sandbox ``open()``), and
where the iterations and cost went. Everything is counted from files; nothing
is estimated.

Usage::

    python -m evallab.rlm.traj_report --bench runs/x/bench --harbor runs \
        --markdown out.md --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PARSE_ERROR_PREFIX = "[Error] Your previous response could not be parsed"
HEAD_RE = re.compile(r"Unparsed response head: (.*)$", re.S)
TOOL_PATTERNS = {
    "llm_query": re.compile(r"\bllm_query\("),
    "llm_query_batched": re.compile(r"\bllm_query_batched\("),
    "SUBMIT": re.compile(r"\bSUBMIT\("),
    "exec_command": re.compile(r"\bexec_command\("),
    "read_file": re.compile(r"\bread_file\("),
    "write_file": re.compile(r"\bwrite_file\("),
    "run_python": re.compile(r"\brun_python\("),
    "sandbox_open": re.compile(r"(?<![\w.])open\("),
    "regex": re.compile(r"\bre\.(search|findall|finditer|match|sub|compile)\("),
}


def classify_drift(head: str) -> str:
    """Bucket an unparsed response head into the observed drift categories.

    The head is the prefix the harness recorded (600 chars before 2026-09-16
    07:30 UTC, 1500 after); a fence that starts beyond it is invisible, so
    ``prose-without-code`` is an upper bound on genuinely code-free turns.
    """
    text = head.strip().strip("'\"")
    has_reasoning_marker = "[[ ## reasoning ## ]]" in text
    has_code_marker = "[[ ## code ## ]]" in text
    has_fence = "```" in text
    has_code_label = re.search(r"(?m)^\s*Code:\s*$", text) is not None or "\\nCode:" in text
    starts_reasoning_label = re.match(r"\s*Reasoning:", text) is not None
    if starts_reasoning_label and not has_reasoning_marker:
        return "mirrored-history-format"
    if has_reasoning_marker and has_code_label and not has_code_marker:
        return "reasoning-marker-plus-code-label"
    if has_code_marker and not has_reasoning_marker:
        return "code-marker-without-reasoning-marker"
    if has_reasoning_marker and has_code_marker:
        return "markers-present-but-malformed"
    if has_fence:
        return "preamble-plus-fenced-code"
    return "prose-without-code"


def _steps_summary(steps: list[dict[str, Any]]) -> dict[str, Any]:
    drift = Counter()
    repl_errors = 0
    tools: Counter[str] = Counter()
    for step in steps:
        output = str(step.get("output", ""))
        code = str(step.get("code", ""))
        if output.startswith(PARSE_ERROR_PREFIX):
            match = HEAD_RE.search(output)
            drift[classify_drift(match.group(1) if match else "")] += 1
            continue
        if output.startswith("[Error]"):
            repl_errors += 1
        for name, pattern in TOOL_PATTERNS.items():
            if pattern.search(code):
                tools[name] += 1
    return {"drift": dict(drift), "repl_errors": repl_errors, "tools_steps": dict(tools)}


def load_bench(directory: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for row_file in sorted(directory.glob("*.jsonl")):
        for line in row_file.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            steps: list[dict[str, Any]] = []
            path = row.get("trajectory_path")
            if path and Path(path).is_file():
                steps = json.loads(Path(path).read_text()).get("trajectory", [])
            runs.append(
                {
                    "source": "bench",
                    "run": row["task_id"],
                    "policy": row["policy"],
                    "family": row["family"],
                    "score": row["score"],
                    "iterations": row["iterations"],
                    "cost_usd": row["cost_usd"],
                    "parse_failures": row.get("parse_failures", 0),
                    "salvaged_actions": row.get("salvaged_actions", 0),
                    "error": row.get("error"),
                    **_steps_summary(steps),
                }
            )
    return runs


def load_harbor(root: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for trajectory in sorted(root.glob("*/*/agent/rlm/trajectory.json")):
        trial = trajectory.parents[2]
        result_path = trial / "result.json"
        policy_path = trajectory.with_name("policy.json")
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        policy = json.loads(policy_path.read_text()) if policy_path.is_file() else {}
        meta = (result.get("agent_result") or {}).get("metadata") or {}
        steps = json.loads(trajectory.read_text())
        runs.append(
            {
                "source": "harbor",
                "run": trial.parent.name,
                "policy": (policy.get("policy") or {}).get("policy_id"),
                "family": result.get("task_name") or trial.parent.name,
                "score": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
                "iterations": len(steps),
                "cost_usd": (result.get("agent_result") or {}).get("cost_usd"),
                "parse_failures": meta.get("rlm_parse_failures"),
                "salvaged_actions": meta.get("rlm_salvaged_actions"),
                "error": (result.get("exception_info") or {}).get("exception_type"),
                **_steps_summary(steps),
            }
        )
    return runs


def aggregate(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[f"{run['source']}:{run['policy']}"].append(run)
    out: dict[str, dict[str, Any]] = {}
    for key, items in sorted(grouped.items()):
        drift: Counter[str] = Counter()
        tools: Counter[str] = Counter()
        for run in items:
            drift.update(run["drift"])
            tools.update(run["tools_steps"])
        scored = [r for r in items if isinstance(r["score"], (int, float))]
        costs = [float(r["cost_usd"]) for r in items if isinstance(r["cost_usd"], (int, float))]
        out[key] = {
            "runs": len(items),
            "mean_score": (sum(r["score"] for r in scored) / len(scored)) if scored else None,
            "mean_iterations": sum(r["iterations"] for r in items) / len(items),
            "mean_cost_usd": (sum(costs) / len(costs)) if costs else None,
            "runs_with_drift": sum(1 for r in items if sum(r["drift"].values()) > 0),
            "drift_total": sum(drift.values()),
            "drift_by_kind": dict(drift),
            "salvaged_total": sum(int(r["salvaged_actions"] or 0) for r in items),
            "repl_errors": sum(r["repl_errors"] for r in items),
            "tool_steps": dict(tools),
            "errors": sum(1 for r in items if r["error"]),
            "most_expensive": sorted(
                (
                    {"run": r["run"], "cost_usd": r["cost_usd"], "iterations": r["iterations"]}
                    for r in items
                    if isinstance(r["cost_usd"], (int, float))
                ),
                key=lambda r: -float(r["cost_usd"]),
            )[:3],
        }
    return out


def render(summary: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| source:policy | runs | mean score | iters | $/run | runs w/ drift | drift turns | salvaged | REPL errs | llm_query steps | write_file | sandbox open() | errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, block in summary.items():
        tools = block["tool_steps"]
        score = "n/a" if block["mean_score"] is None else f"{block['mean_score']:.2f}"
        cost = "n/a" if block["mean_cost_usd"] is None else f"{block['mean_cost_usd']:.3f}"
        lines.append(
            f"| {key} | {block['runs']} | {score} | {block['mean_iterations']:.1f} | {cost} | {block['runs_with_drift']} | "
            f"{block['drift_total']} | {block['salvaged_total']} | {block['repl_errors']} | "
            f"{tools.get('llm_query', 0) + tools.get('llm_query_batched', 0)} | {tools.get('write_file', 0)} | {tools.get('sandbox_open', 0)} | {block['errors']} |"
        )
    lines.append("")
    lines.append("Drift kinds:")
    for key, block in summary.items():
        if block["drift_by_kind"]:
            lines.append(
                f"- {key}: "
                + ", ".join(
                    f"{kind} {count}"
                    for kind, count in sorted(block["drift_by_kind"].items(), key=lambda kv: -kv[1])
                )
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--bench", type=Path, action="append", default=[], help="bench output dir (repeatable)"
    )
    parser.add_argument("--harbor", type=Path, help="runs root holding rlm-* job directories")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    runs: list[dict[str, Any]] = []
    for directory in args.bench:
        runs.extend(load_bench(directory))
    if args.harbor:
        runs.extend(load_harbor(args.harbor))
    if not runs:
        raise SystemExit("no trajectories found")
    summary = aggregate(runs)
    text = render(summary)
    print(text)
    if args.markdown:
        args.markdown.write_text(text + "\n")
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "runs": runs}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
