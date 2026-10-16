#!/usr/bin/env python3
"""Build training arms A/B/C from recorded ordinary MiMo passes (FOCUS overnight plan 2026-10-16).

A (original): every recorded passing model trajectory, one per execution UUID.
B (repaired): A rows whose every qualified repaired-v6 regrade still passes and that carry
   no defect flag.
C (control): the first |C| rows of a seeded permutation of C's sampling frame. The frame is
   A minus any ``--c-exclude-task`` tasks and minus rows the trainer's eligibility rule refuses
   (``--c-eligibility``); |C| is the number of B rows that rule accepts. The manifest publishes
   the full order, so a consumer that must replace a C row takes the next one in order and
   never leaves the frame.

Unit = execution UUID: archive aliases of one execution are one example, never several.
Rows outside B carry the concrete reasons, so nothing is silently dropped.

    uv run python research/experiments/qa-trace/training-arms/build_arms.py \
        --passes <campaign>/inputs/ordinary-passes.jsonl \
        --regrade <campaign>/ordinary-76*/summary.json \
        --audit <campaign>/full-prompt-comparator-audit-v3.json \
        --out <dir> [--heldout-manifest <json|txt>] [--seed 20261016] \
        [--c-exclude-task <task-id> ...] [--c-eligibility <to_export summary.json>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

RESULTS = Path.home() / "Developer/eval-lab-results"
FAULT_STRATA = {"native_faulty", "previously_reviewed_faulty", "static_fault_suspected", "unclear"}
FETCH_CMD = re.compile(
    r"\b(pip3? (download|install)|uv pip install|git clone|curl |wget |npm (install|i) |go get )"
)
FETCH_SEEN = re.compile(
    r"Successfully downloaded|Downloading https?://|Cloning into|HTTP request sent|"
    r"Saved /|Downloading [\w.-]+-[\d.]+.*\.(whl|tar\.gz)"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def trajectory_for(rows: list[dict[str, Any]]) -> Path | None:
    for row in rows:
        for key in ("observation_trial_path", "trial_path"):
            path = RESULTS / row[key] / "agent/trajectory.json"
            if path.is_file():
                return path
    return None


def _observation_text(step: dict[str, Any]) -> str:
    layers = (step.get("extra") or {}).get("step_layers") or {}
    observed = (layers.get("observed") or {}).get("output")
    if observed is not None:
        return observed
    results = (step.get("observation") or {}).get("results") or []
    return "".join(str(r.get("content", "")) for r in results)


def to_messages(traj: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any] | None, str]:
    """Chat rows the model actually saw/produced. Returns (messages, tools, fidelity)."""
    agent = traj.get("agent") or {}
    native = agent.get("name") == "mimoagent"
    messages: list[dict[str, Any]] = []
    fidelity = "raw_completion"
    for step in traj.get("steps", []):
        source = step.get("source")
        if source in ("system", "user"):
            messages.append({"role": source, "content": step.get("message") or ""})
            continue
        if source != "agent":
            continue
        layers = (step.get("extra") or {}).get("step_layers") or {}
        proposed = layers.get("proposed")
        if native:
            calls = []
            for call in step.get("tool_calls") or []:
                native_args = (call.get("extra") or {}).get("native_arguments")
                arguments = (
                    native_args
                    if isinstance(native_args, str)
                    else json.dumps(call.get("arguments"))
                )
                calls.append(
                    {
                        "id": call.get("tool_call_id"),
                        "type": "function",
                        "function": {"name": call.get("function_name"), "arguments": arguments},
                    }
                )
            message: dict[str, Any] = {"role": "assistant", "content": step.get("message") or ""}
            if step.get("reasoning_content"):
                message["reasoning_content"] = step["reasoning_content"]
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            for result in (step.get("observation") or {}).get("results") or []:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.get("source_call_id"),
                        "content": str(result.get("content", "")),
                    }
                )
        elif proposed:
            message = {"role": "assistant", "content": proposed.get("message") or ""}
            if proposed.get("reasoning"):
                message["reasoning_content"] = proposed["reasoning"]
            messages.append(message)
            observed = _observation_text(step)
            if observed:
                messages.append({"role": "user", "content": observed})
        else:
            # No raw completion recorded: keep the parsed actions, labelled as such.
            fidelity = "parsed_actions_only"
            message = {
                "role": "assistant",
                "content": step.get("message") or "",
                "tool_calls": [
                    {
                        "id": c.get("tool_call_id"),
                        "type": "function",
                        "function": {
                            "name": c.get("function_name"),
                            "arguments": json.dumps(c.get("arguments")),
                        },
                    }
                    for c in step.get("tool_calls") or []
                ],
            }
            if step.get("reasoning_content"):
                message["reasoning_content"] = step["reasoning_content"]
            messages.append(message)
            observed = _observation_text(step)
            if observed:
                messages.append({"role": "user", "content": observed})
    tools = agent.get("tool_definitions") if native else None
    return messages, tools, fidelity


def network_fetch(traj: dict[str, Any]) -> list[int]:
    """Agent steps that issued a fetch command whose output shows a completed download."""
    hits = []
    for step in traj.get("steps", []):
        if step.get("source") != "agent":
            continue
        commands = " ".join(json.dumps(c.get("arguments")) for c in step.get("tool_calls") or [])
        if FETCH_CMD.search(commands) and FETCH_SEEN.search(_observation_text(step)):
            hits.append(step.get("step_id"))
    return hits


NAMESPACE = "mimo-v2.6-rl"


def canonical_task(task_id: str) -> str:
    """One form for every task id: ``mimo-v2.6-rl/<id>``.

    Bare ids get the namespace; a foreign namespace is an error, never a silent miss.
    """
    value = task_id.strip()
    namespace, slash, name = value.rpartition("/")
    if not slash:
        namespace, name = NAMESPACE, value
    if namespace != NAMESPACE or not name or "/" in name:
        raise ValueError(f"held-out id {task_id!r} is not a {NAMESPACE} task id")
    return f"{NAMESPACE}/{name}"


def heldout_tasks(path: Path | None) -> set[str] | None:
    """Canonical held-out ids from plaintext (one per line, ``#`` comments) or JSON.

    JSON may be the canonical ``evallab.sft_split/2`` manifest, a list, or
    ``{"heldout": [...]}`` of ids or of objects carrying ``task``/``task_id``/
    ``task_name``. Any other shape raises.
    """
    if path is None:
        return None
    text = path.read_text()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        lines = (line.strip() for line in text.splitlines())
        return {canonical_task(line) for line in lines if line and not line.startswith("#")}
    if isinstance(doc, dict) and doc.get("contract") == SFT_SPLIT_CONTRACT:
        return _sft_split_heldout(doc)
    items = doc.get("heldout") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError("held-out JSON must be a list or an object with a 'heldout' list")
    ids = set()
    for item in items:
        if isinstance(item, dict):
            item = next((item[k] for k in ("task", "task_id", "task_name") if k in item), None)
        if not isinstance(item, str):
            raise ValueError(f"held-out entry has no task id: {item!r}")
        ids.add(canonical_task(item))
    return ids


SFT_SPLIT_CONTRACT = "evallab.sft_split/2"


def _sft_split_heldout(doc: dict[str, Any]) -> set[str]:
    """MiMo held-out tasks of a multi-domain split manifest.

    Other domains' held-out tasks cannot overlap MiMo training rows, so only the
    MiMo namespace is kept; the per-task rows must agree with ``heldout_task_ids``.
    """
    rows = [row for row in doc["tasks"] if row["split"] == "heldout"]
    if sorted(row["task_id"] for row in rows) != sorted(doc["heldout_task_ids"]):
        raise ValueError("sft_split manifest tasks disagree with heldout_task_ids")
    names = [row.get("task_name") or row["task_id"] for row in rows]
    return {
        canonical_task(name)
        for name in names
        if "/" not in name or name.startswith(f"{NAMESPACE}/")
    }


def is_heldout(task: str, heldout: set[str] | None) -> bool:
    return heldout is not None and canonical_task(task) in heldout


def draw_control(
    arm_a: list[dict[str, Any]],
    size: int,
    seed: int,
    excluded_tasks: set[str],
    ineligible: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (C, draw order): C is the first ``size`` rows of a seeded permutation of the frame.

    The frame is A without ``excluded_tasks`` (canonical ids) and ``ineligible`` execution
    ids. For a fixed frame, ``random.sample`` prefixes agree across sizes, so C equals
    ``Random(seed).sample(frame, size)``, and a consumer replacing a row keeps the same draw.
    """
    frame = [
        e
        for e in arm_a
        if canonical_task(e["task"]) not in excluded_tasks and e["execution_id"] not in ineligible
    ]
    if len(frame) < size:
        raise SystemExit(f"C sampling frame has {len(frame)} rows; C needs {size}")
    order = random.Random(seed).sample(frame, len(frame))
    return order[:size], order


def arm_body(arm: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(e, sort_keys=True) + "\n" for e in sorted(arm, key=lambda e: e["execution_id"])
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=Path, required=True)
    parser.add_argument("--regrade", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--heldout-manifest", type=Path)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument(
        "--c-exclude-task",
        action="append",
        default=[],
        help="task id whose executions never enter C's sampling frame (repeatable)",
    )
    parser.add_argument(
        "--c-eligibility",
        type=Path,
        help="trainer to_export summary.json computed on this exact A; its dropped rows "
        "leave C's frame and B's trainable count",
    )
    args = parser.parse_args()
    c_excluded = {canonical_task(t) for t in args.c_exclude_task}

    observations = jsonl(args.passes)
    regrade = json.loads(args.regrade.read_text())["rows"]
    audit = {
        r["item_id"]: r["evidence_stratum"]
        for r in json.loads(args.audit.read_text())["observations"]
    }
    heldout = heldout_tasks(args.heldout_manifest)

    by_execution: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        by_execution.setdefault(row["execution_id"], []).append(row)
    regrade_by_item: dict[str, list[dict[str, Any]]] = {}
    for row in regrade:
        regrade_by_item.setdefault(row["item_id"], []).append(row)

    examples = []
    for execution_id, rows in sorted(by_execution.items()):
        task = rows[0]["task"]
        assert all(r["task"] == task for r in rows), execution_id
        path = trajectory_for(rows)
        if path is None:
            raise SystemExit(f"no trajectory for execution {execution_id}")
        raw = path.read_bytes()
        traj = json.loads(raw)
        messages, tools, fidelity = to_messages(traj)
        item_ids = [r["item_id"] for r in rows]
        regrades = [g for i in item_ids for g in regrade_by_item.get(i, [])]
        strata = {audit.get(i, "unaudited") for i in item_ids}
        fetches = network_fetch(traj)

        reasons = []
        if is_heldout(task, heldout):
            reasons.append("heldout_task")
        qualified = [
            g for g in regrades if g.get("valid_grader") and g["outcome"] in ("stays", "flips")
        ]
        if not qualified:
            seen = sorted(
                {g["outcome"] if g.get("valid_grader") else "unvalidated_grader" for g in regrades}
            )
            reasons.append("no_qualified_regrade:" + ("+".join(seen) if seen else "no_grader"))
        if any(g["outcome"] == "flips" for g in qualified):
            reasons.append("regrade_fails")
        if any(g["outcome"] == "refused" for g in regrades):
            reasons.append("replay_refused")
        if strata & FAULT_STRATA:
            reasons.append("defect_flag:" + "+".join(sorted(strata & FAULT_STRATA)))
        if fetches:
            reasons.append("network_fetch_observed")

        examples.append(
            {
                "execution_id": execution_id,
                "task": task,
                "item_ids": item_ids,
                "agent": (traj.get("agent") or {}).get("name"),
                "model": (traj.get("agent") or {}).get("model_name"),
                "trajectory_path": str(path),
                "trajectory_sha256": sha256(raw),
                "fidelity": fidelity,
                "recorded_reward": 1.0,
                "regrades": [
                    {
                        k: g.get(k)
                        for k in (
                            "grader",
                            "outcome",
                            "regraded",
                            "valid_grader",
                            "controls",
                            "matrix_receipt",
                        )
                    }
                    for g in regrades
                ],
                "evidence_strata": sorted(strata),
                "network_fetch_steps": fetches,
                "excluded_from_b": reasons,
                "messages": messages,
                **({"tools": tools} if tools else {}),
            }
        )

    usable = [e for e in examples if "heldout_task" not in e["excluded_from_b"]]
    arm_a = usable
    arm_b = [e for e in usable if not e["excluded_from_b"]]
    unknown = c_excluded - {canonical_task(e["task"]) for e in examples}
    if unknown:
        raise SystemExit(f"--c-exclude-task matches no execution: {sorted(unknown)}")
    ineligible: dict[str, str] = {}
    eligibility_source = None
    if args.c_eligibility:
        summary = json.loads(args.c_eligibility.read_text())
        a_sha = "sha256:" + sha256(arm_body(arm_a).encode())
        if summary["source_files"]["A"] != a_sha:
            raise SystemExit(
                f"--c-eligibility was computed on A {summary['source_files']['A']}, not {a_sha}"
            )
        ineligible = summary["dropped_before_arms"]
        eligibility_source = {
            "path": str(args.c_eligibility),
            "sha256": sha256(args.c_eligibility.read_bytes()),
            "rules": summary["rules"],
        }
    b_trainable = [e for e in arm_b if e["execution_id"] not in ineligible]
    arm_c, c_order = draw_control(arm_a, len(b_trainable), args.seed, c_excluded, set(ineligible))

    args.out.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, arm in (("A", arm_a), ("B", arm_b), ("C", arm_c)):
        body = arm_body(arm)
        (args.out / f"{name}.jsonl").write_text(body)
        hashes[name] = sha256(body.encode())

    reason_counts = Counter(r.split(":")[0] for e in usable for r in e["excluded_from_b"])
    manifest = {
        "schema": "qa-trace.training-arms/v1",
        "question": "Does training on repaired MiMo passes beat training on the originals (FOCUS 2026-10-16)?",
        "unit": "execution UUID (archive aliases collapsed)",
        "seed": args.seed,
        "inputs_sha256": {
            "passes": sha256(args.passes.read_bytes()),
            "regrade": sha256(args.regrade.read_bytes()),
            "audit": sha256(args.audit.read_bytes()),
            "heldout_manifest": sha256(args.heldout_manifest.read_bytes())
            if args.heldout_manifest
            else None,
        },
        "heldout_applied": heldout is not None,
        "heldout_ids": len(heldout) if heldout is not None else None,
        "heldout_overlapping_training_tasks": sorted(
            {e["task"] for e in examples if is_heldout(e["task"], heldout)}
        ),
        "observations": len(observations),
        "tasks": sorted({e["task"] for e in examples}),
        "tasks_per_arm": {
            name: len({e["task"] for e in arm})
            for name, arm in (("A", arm_a), ("B", arm_b), ("C", arm_c))
        },
        "executions": len(examples),
        "heldout_excluded": len(examples) - len(usable),
        "counts": {"A": len(arm_a), "B": len(arm_b), "C": len(arm_c)},
        "c_overlap_with_b": len(
            {e["execution_id"] for e in arm_c} & {e["execution_id"] for e in arm_b}
        ),
        "c_frame": {
            "excluded_tasks": sorted(c_excluded),
            "excluded_executions": sum(canonical_task(e["task"]) in c_excluded for e in arm_a),
            "eligibility_source": eligibility_source,
            "ineligible_executions": dict(sorted(ineligible.items())),
            "c_size_rule": "|B| minus B rows the eligibility rule refuses",
            "b_trainable": [e["execution_id"] for e in b_trainable],
            "size": len(c_order),
            "draw_order": [e["execution_id"] for e in c_order],
        },
        "excluded_from_b_reason_counts": dict(reason_counts),
        "fidelity_counts": dict(Counter(e["fidelity"] for e in arm_a)),
        "agents": dict(Counter(e["agent"] for e in arm_a)),
        "models": dict(Counter(e["model"] for e in arm_a)),
        "arm_sha256": hashes,
        "row_schema": "messages (OpenAI chat; assistant may carry reasoning_content/tool_calls; mimoagent rows add tools) plus provenance fields",
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "executions",
                    "heldout_applied",
                    "counts",
                    "c_overlap_with_b",
                    "excluded_from_b_reason_counts",
                    "fidelity_counts",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
