"""Task-evidence wrong-pass detector: flag a pass when its task is a known weak-test task.

Rules declared in research-context ``bridge/2026-10-19-deep/transfer-matrix.md``
("Task-evidence detector", 07:33 ET) before any score was computed; scored once:

* D1: task in ``mimo-weak-tests-v2.1`` supported-weak items or ``mimo-stratified-sample100``
  ``fail`` items.
* D1-v1: task is a ``fail``/weak *task* item of ``mimo-weak-tests-v1`` (2026-10-15, the only
  atlas evidence predating every scored trajectory).
* D2: D1 and the trace's edited files (write events of the frozen
  ``evallab.action_features``) touch none of the files modified by that task's v2.1 witness
  patches. Tasks without a v2.1 witness are not flagged by D2.

Estimand: ``wrong_pass`` recall and ``real_pass`` false alarms among v6 passes, on
``mimo-v7-canonical-pass70`` and ``mimo-fresh-window3`` (actions view). Also reported,
as context only: whether the task's v7 grader is a repair. No model calls, no runs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KEYS = REPO / "research/answer-keys"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(KEYS))

from external_traces import file_digest, wilson  # noqa: E402

from evallab import action_features as af  # noqa: E402
from evallab.results_home import results_root  # noqa: E402

FROZEN_ACTION_FEATURES = "88cf3b998fd3"
CONTEXT = Path.home() / "Developer/research-context"
REGRADE = CONTEXT / "bridge/2026-10-17-v7/regrade-v7.json"
CHEAT_SET_LABELS = (
    "2026-10-19/cheatbench-mimo-cheat-set-v7/evaluator-only/cheat-set-v7.labels.jsonl"
)
SCORED = ("mimo-v7-canonical-pass70", "mimo-fresh-window3")


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def task(item: dict[str, Any]) -> str:
    return item["subject"]["task_id"].split("/")[-1]


def weak_sets() -> dict[str, set[str]]:
    v21 = {task(i) for i in jsonl(KEYS / "mimo-weak-tests-v2.1/items.jsonl")}
    s100 = {
        task(i)
        for i in jsonl(KEYS / "mimo-stratified-sample100/items.jsonl")
        if i["expected_verdict"] == "fail"
    }
    v1 = {
        task(i)
        for i in jsonl(KEYS / "mimo-weak-tests-v1/items.jsonl")
        if i["subject"]["kind"] == "task" and i["expected_verdict"] == "fail"
    }
    return {"D1": v21 | s100, "v2.1": v21, "sample100": s100, "D1-v1": v1}


def witness_files(name: str) -> set[str]:
    files: set[str] = set()
    for patch in (KEYS / "mimo-weak-tests-v2.1/witnesses" / name).glob("*.patch"):
        files.update(re.findall(r"^diff --git a/(\S+) b/", patch.read_text(), re.M))
    return files


def record_scoreboard(result: dict[str, Any]) -> None:
    """D1 and D2 rows; only v6 passes are measured (``not_a_pass`` items are unmeasured)."""
    import csv
    import datetime

    path = REPO / "research/qa-scoreboard/scoreboard.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        existing = list(reader)
    if columns is None:
        raise ValueError("scoreboard header missing")
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    systems = {"D1": "weak-task@atlas-v2.1+sample100", "D2": "weak-task-untouched@atlas-v2.1"}
    rows = []
    for key, cell in result["keys"].items():
        n_items = sum(1 for _ in (KEYS / key / "items.jsonl").read_text().splitlines() if _)
        measured = cell["n_wrong_pass"] + cell["n_real_pass"]
        for det, system in systems.items():
            m = cell[det]
            row = {
                "date_utc": stamp,
                "key_id": key,
                "split": "test",
                "category": "all",
                "agent": "Task evidence",
                "system": system,
                "recall": str(m["recall"]["rate"]),
                "false_alarm_rate": str(m["false_alarm"]["rate"]),
                "root_cause_match": "n/a",
                "paired_discrimination": "n/a",
                "cost_per_item_usd": "0.0",
                "n_items": str(n_items),
                "n_measured": str(measured),
                "n_pairs": "0",
                "n_pairs_measured": "0",
                "status": "complete"
                if measured == n_items
                else f"partial: {measured} of {n_items} measured (v6 passes only)",
                "notes": "Rule declared before scoring; recall = wrong_pass, false alarms = real_pass; "
                f"flagged tasks {len(m['flagged_tasks'])}; Wilson intervals in "
                "research/experiments/task-evidence-detector-20261019/results.json; 0 model calls",
            }
            identity = ("key_id", "split", "category", "agent", "system")
            prior = [old for old in existing if all(old[k] == row[k] for k in identity)]
            if any(old[k] != row[k] for old in prior for k in ("recall", "false_alarm_rate")):
                raise ValueError(f"{key} {system}: existing metrics differ")
            if not prior:
                rows.append(row)
    with path.open("a", newline="") as stream:
        csv.DictWriter(stream, fieldnames=columns, lineterminator="\n").writerows(rows)


def touches(edited: set[str], targets: set[str]) -> bool:
    return any(e == t or e.endswith("/" + t) for e in edited for t in targets)


def repaired_tasks(results: Path) -> dict[str, bool]:
    regrade = json.loads(REGRADE.read_text())
    out = {
        name.split("/")[-1]: not grader["label"].startswith("agent-network-none")
        for name, grader in regrade["admission"]["graders"].items()
    }
    for row in jsonl(results / CHEAT_SET_LABELS):
        kind = row.get("v7_grader_kind")
        if kind:
            out[row["task_id"].split("/")[-1]] = kind != "unchanged_v6"
    return out


def main() -> None:
    if "--scoreboard" in sys.argv[1:]:
        record_scoreboard(json.loads((HERE / "results.json").read_text()))
        return
    sha = file_digest(REPO / "src/evallab/action_features.py")
    if not sha.startswith(FROZEN_ACTION_FEATURES):
        raise ValueError("action_features.py differs from the frozen version")
    results = results_root()
    sets = weak_sets()
    repaired = repaired_tasks(results)
    rows = []
    for key in SCORED:
        for item in jsonl(KEYS / key / "items.jsonl"):
            label = item["source_label"]
            if label not in ("wrong_pass", "real_pass"):
                continue
            name = task(item)
            doc = json.loads(
                (results / item["actions_only_ref"]["path"] / "agent/trajectory.json").read_text()
            )
            events, _ = af.action_events(doc["steps"])
            edited = {e.target for e in events if e.kind == "write" and e.target}
            wfiles = witness_files(name)
            rows.append(
                {
                    "key": key,
                    "item_id": item["item_id"],
                    "task": name,
                    "label": label,
                    "D1": name in sets["D1"],
                    "D1-v1": name in sets["D1-v1"],
                    "D2": name in sets["D1"] and bool(wfiles) and not touches(edited, wfiles),
                    "witness_files": sorted(wfiles),
                    "edited_files": sorted(edited),
                    "v7_grader_repaired": repaired.get(name),
                }
            )
    result: dict[str, Any] = {
        "schema": "evallab.task_evidence_detector/v1",
        "llm_calls": 0,
        "cost_usd": 0.0,
        "action_features_sha256": sha,
        "weak_set_sizes": {k: len(v) for k, v in sets.items()},
        "keys": {},
    }
    for key in SCORED:
        part = [r for r in rows if r["key"] == key]
        pos = [r for r in part if r["label"] == "wrong_pass"]
        neg = [r for r in part if r["label"] == "real_pass"]
        cell: dict[str, Any] = {
            "n_wrong_pass": len(pos),
            "n_real_pass": len(neg),
            "wrong_pass_tasks": sorted({r["task"] for r in pos}),
            "n_tasks": len({r["task"] for r in part}),
        }
        for det in ("D1", "D1-v1", "D2", "v7_grader_repaired"):
            cell[det] = {
                "recall": wilson(sum(bool(r[det]) for r in pos), len(pos)),
                "false_alarm": wilson(sum(bool(r[det]) for r in neg), len(neg)),
                "flagged_tasks": sorted({r["task"] for r in part if r[det]}),
            }
        result["keys"][key] = cell
    (HERE / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "predictions.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    )
    for key, cell in result["keys"].items():
        print(
            key, cell["n_wrong_pass"], cell["n_real_pass"], "wrong tasks", cell["wrong_pass_tasks"]
        )
        for det in ("D1", "D1-v1", "D2", "v7_grader_repaired"):
            c = cell[det]
            print(
                f"  {det}: recall {c['recall']['hits']}/{c['recall']['n']} "
                f"FA {c['false_alarm']['hits']}/{c['false_alarm']['n']} tasks {c['flagged_tasks']}"
            )


if __name__ == "__main__":
    main()
