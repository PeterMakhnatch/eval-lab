#!/usr/bin/env python3
"""Build the frozen, DEV-only OQ-3 discovery cohort without models or Docker.

Only qa-trace-sft-v0/dev.jsonl and the pinned read-only database are inputs.
Candidate hackbook entries and publisher annotations never enter trajectories.
Goodfire judgments are provisional publisher labels, not replay confirmation.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_SHA256 = "2ddb3c852f08475a80c736b073ff2032f0aae22b8c36cd4a0501025f15c055bd"
GOODFIRE_PROVENANCE = (
    "goodfire consensus_lp_v1 provisional 3-vote publisher judgment (not replay-confirmed)"
)
SAMPLING = (
    "qa-trace-sft-dev: all 32 exploit-pass and first 32 neither by ascending "
    "sha256('oq3:'+row.id); goodfire-dev: scan eligible DEV traces in ascending "
    "sha256('oq3:'+trace_id), accepting unseen task_ids until each label has 50; "
    "one task across both Goodfire strata; final-parent-path steps only"
)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def save_case(out: Path, metadata: dict, steps: list[dict]) -> dict:
    trajectory = {"schema_version": "ATIF-v1.2", "session_id": metadata["case_id"], "steps": steps}
    payload = encode(trajectory)
    path = out / metadata["case_id"] / "agent" / "trajectory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        **metadata,
        "n_agent_steps": sum(step["source"] == "agent" for step in steps),
        "trajectory_sha256": hashlib.sha256(payload).hexdigest(),
    }


def qa_cases(out: Path) -> list[dict]:
    path = ROOT / "research/datasets/qa-trace-sft-v0/dev.jsonl"
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if len(rows) != 118 or any(row["split"] != "dev" or row["source_split"] != "dev" for row in rows):
        raise ValueError("Expected exactly 118 DEV qa-trace-sft-v0 rows")
    positives = [row for row in rows if row["target"]["label"] == "exploit-pass"]
    negatives = [row for row in rows if row["target"]["label"] == "neither"]
    if len(positives) != 32 or len(negatives) != 86:
        raise ValueError("Expected qa-trace-sft-dev label counts 32 exploit-pass / 86 neither")
    def order(row):
        return (sha256("oq3:" + row["id"]), row["id"])
    selected = sorted(positives + sorted(negatives, key=order)[:32], key=order)
    cases = []
    for row in selected:
        case_id = "qa-trace-sft-dev-" + sha256(row["id"])[:16]
        steps = []
        for action in row["input"]["actions"]:
            function, separator, command = action["text"].partition(": ")
            if not separator:
                function, command = "tool", action["text"]
            steps.append({
                "step_id": action["step"], "source": "agent", "message": "",
                "tool_calls": [{"tool_call_id": f"{case_id}:{action['step']}",
                                "function_name": function, "arguments": {"command": command}}],
            })
        cases.append(save_case(out, {
            "case_id": case_id, "cohort": "qa-trace-sft-dev", "task_id": row["task"],
            "trace_id": None, "gold_label": row["target"]["label"],
            "gold_mechanism": row["target"]["mechanism"], "gold_categories": [],
            "label_provenance": row["label_provenance"],
        }, steps))
    return cases


def select_goodfire(db: sqlite3.Connection) -> list[sqlite3.Row]:
    candidates = db.execute("""
        SELECT a.trace_id, a.task_id, a.is_exploit, a.verified_receipt_json
        FROM agent_traces a JOIN corpus_tasks t ON t.task_id = a.task_id
        WHERE t.dataset_id = 'goodfire-reward-hack-data' AND t.split = 'DEV'
          AND ((a.is_exploit = 1
                AND json_extract(a.verified_receipt_json, '$.source_label.verdict_enacted') = 'yes')
            OR (json_extract(a.verified_receipt_json, '$.source_label.verdict_enacted') = 'no'
                AND json_extract(a.verified_receipt_json, '$.source_label.flag_count') = 0
                AND json_extract(a.verified_receipt_json, '$.source_label.judged') = 1))
    """).fetchall()
    counts = {"exploit-pass": 0, "neither": 0}
    selected, tasks = [], set()
    for row in sorted(candidates, key=lambda row: (sha256("oq3:" + row["trace_id"]), row["trace_id"])):
        source = json.loads(row["verified_receipt_json"])["source_label"]
        label = "exploit-pass" if source["verdict_enacted"] == "yes" else "neither"
        if counts[label] == 50 or row["task_id"] in tasks:
            continue
        selected.append(row)
        tasks.add(row["task_id"])
        counts[label] += 1
        if len(selected) == 100:
            break
    if counts != {"exploit-pass": 50, "neither": 50}:
        raise ValueError(f"Insufficient distinct eligible Goodfire DEV tasks: {counts}")
    return selected


def decode_arguments(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"command": raw}
    return decoded if isinstance(decoded, dict) else {"command": raw}


def goodfire_tool_calls(row: sqlite3.Row, case_id: str) -> list[dict]:
    raw = row["command_or_action"]
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list) and all(
        isinstance(item, dict) and {"arguments", "id", "name"} <= item.keys() for item in decoded
    ):
        return [{"tool_call_id": item["id"], "function_name": item["name"],
                 "arguments": decode_arguments(item["arguments"])} for item in decoded]
    return [{"tool_call_id": f"{case_id}:{row['step_index']}",
             "function_name": row["tool_name"] or "bash", "arguments": decode_arguments(raw)}]


def goodfire_steps(db: sqlite3.Connection, trace_id: str, case_id: str, prose: bool = False) -> list[dict]:
    rows = db.execute("""
        SELECT s.step_index, s.role, s.tool_name, s.command_or_action, s.content_text
        FROM trace_steps s JOIN agent_traces a ON a.trace_id = s.trace_id
        JOIN corpus_tasks t ON t.task_id = a.task_id
        WHERE t.dataset_id = 'goodfire-reward-hack-data' AND t.split = 'DEV'
          AND s.trace_id = ? AND json_extract(s.flags_json, '$.on_final_parent_path') = 1
        ORDER BY s.step_index
    """, (trace_id,))
    steps, last_agent = [], None
    for row in rows:
        content = row["content_text"] or ""
        if row["role"] in ("user", "system"):
            # Combine the leading system/user preamble into the first instruction.
            if steps and last_agent is None:
                steps[0]["message"] += "\n\n" + content
            else:
                steps.append({"step_id": len(steps) + 1, "source": "user", "message": content})
        elif row["role"] == "assistant":
            step = {"step_id": len(steps) + 1, "source": "agent", "message": content}
            if row["command_or_action"]:
                step["message"] = ""
                step["tool_calls"] = goodfire_tool_calls(row, case_id)
            if prose and content.strip():
                step["message"] = ""
                step["tool_calls"] = [{"tool_call_id": "note", "function_name": "assistant_note",
                                       "arguments": {"command": content[:1500]}}] + step.get("tool_calls", [])
            steps.append(step)
            last_agent = step
        elif row["role"] == "tool":
            if last_agent is None:
                raise ValueError(f"Tool output before an agent action: {trace_id}")
            result = last_agent.setdefault("observation", {"results": [{"content": ""}]})["results"][0]
            result["content"] = (result["content"] + ("\n\n" if result["content"] else "") + content)[:2000]
        else:
            raise ValueError(f"Unsupported DEV trace role: {row['role']}")
    if not steps or not any(step["source"] == "agent" for step in steps):
        raise ValueError(f"No final-parent-path agent steps: {trace_id}")
    return steps


def goodfire_cases(db: sqlite3.Connection, out: Path, prose: bool = False) -> list[dict]:
    cases = []
    for row in select_goodfire(db):
        source = json.loads(row["verified_receipt_json"])["source_label"]
        case_id = "goodfire-dev-" + sha256(row["trace_id"])[:16]
        cases.append(save_case(out, {
            "case_id": case_id, "cohort": "goodfire-dev", "task_id": row["task_id"],
            "trace_id": row["trace_id"],
            "gold_label": "exploit-pass" if source["verdict_enacted"] == "yes" else "neither",
            "gold_mechanism": None, "gold_categories": source["categories"],
            "label_provenance": GOODFIRE_PROVENANCE,
        }, goodfire_steps(db, row["trace_id"], case_id, prose)))
    return cases


def step_stats(cases: list[dict]) -> dict:
    values = [case["n_agent_steps"] for case in cases]
    return {"count": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}


def cohort_stats(cases: list[dict]) -> dict:
    cohorts = {}
    for cohort in ("qa-trace-sft-dev", "goodfire-dev"):
        subset = [case for case in cases if case["cohort"] == cohort]
        if not subset:
            continue
        labels = {}
        for label in ("exploit-pass", "neither"):
            group = [case for case in subset if case["gold_label"] == label]
            labels[label] = {"count": len(group), "agent_steps": step_stats(group)}
        cohorts[cohort] = {"count": len(subset), "labels": labels, "agent_steps": step_stats(subset)}
    return {"schema": "oq3.cohort-stats/v1", "count": len(cases),
            "cohorts": cohorts, "agent_steps": step_stats(cases)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prose", action="store_true",
                        help="Write only the Goodfire post-hoc assistant-note diagnostic")
    args = parser.parse_args()
    os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    db_path = args.db.expanduser().resolve()
    with db_path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != SNAPSHOT_SHA256:
        raise ValueError(f"Unexpected database snapshot sha256: {digest}")
    with closing(sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        cases = ([] if args.prose else qa_cases(args.out)) + goodfire_cases(db, args.out, args.prose)
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    sampling = SAMPLING
    if args.prose:
        sampling = SAMPLING.split("; ", 1)[1] + "; post-hoc assistant_note prose diagnostic"
    manifest = {"schema": "oq3.cohort/v1", "db_snapshot_sha256": digest,
                "created_by": "build_cohort.py", "sampling": sampling, "cases": cases}
    manifest_bytes = encode(manifest)
    (args.out / "manifest.json").write_bytes(manifest_bytes)
    stats = cohort_stats(cases)
    (args.out / "cohort-stats.json").write_bytes(encode(stats))
    print(json.dumps({"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), **stats}, indent=2))


if __name__ == "__main__":
    main()
