"""Build the reader-baseline inputs: 66 TB4 task views, two arms, opaque ids.

Arm ``static``: the exported blind task view only (instruction, task.toml, tests, reference
solution, environment), the same view GT-1 hands to QA agents.
Arm ``leaderboard``: the same view plus ``failure-evidence.md`` built from public Harbor Hub
trials (pass rate, most common failed tests, verifier excerpts). It contains no key text.

Run from the repository root: ``.venv/bin/python research/experiments/tb4-reader-baseline-20261020/build_inputs.py``
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tb4-failure-clusters-20261020"))

import audit_packets  # noqa: E402  (sibling experiment: failure_evidence)
import run as clusters  # noqa: E402

SEED = 20261020
ROOT = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-reader-baseline"
VIEWS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/agent-inputs"
KEY_TASKS = HERE.parents[1] / "answer-keys/tb4-audit-v1/tasks.jsonl"
ARMS = ("static", "leaderboard")
PER_BATCH = 6
CATEGORY = {
    "weak_check": "weak", "unstated_requirement": "prompt_misaligned",
    "instruction_grader_contradiction": "prompt_misaligned", "grader_tamperable": "tamperable",
    "answer_reachable": "reachable", "grader_logic_bug": "broken", "overstrict_oracle": "broken",
    "wrong_answer_key": "broken", "env_mismatch_or_flaky": "broken",
    "infeasible_as_written": "broken", "exploitable_llm_judge": "weak",
}


def aid(task: str) -> str:
    return "r-" + hashlib.sha256(f"tb4-reader:{SEED}:{task}".encode()).hexdigest()[:8]


def taxonomy_markdown() -> str:
    tax = json.loads((VIEWS / "dev/taxonomy.json").read_text())
    out = ["# Task-side defect classes", ""]
    for c in tax["task_side"]:
        out += [f"## `{c['key']}`: {c['name']}", "", f"- direction: {c['direction']}",
                f"- definition: {c['definition']}", f"- not this: {c['not_this']}", ""]
    out += ["## Rules", ""] + [f"- {r}" for r in tax["label_rules"]]
    return "\n".join(out) + "\n"


def main() -> None:
    tasks = [json.loads(line) for line in KEY_TASKS.read_text().splitlines() if line.strip()]
    split = {t["task"]: t["split"] for t in tasks}
    names = sorted(split)
    inventory = json.loads((clusters.RESULTS / "inventory-all.json").read_text())
    trials = clusters.load_trials(inventory)
    if ROOT.exists():
        shutil.rmtree(ROOT / "inputs", ignore_errors=True)
    manifest: dict[str, Any] = {"seed": SEED, "tasks": {}, "batches": {}}
    for arm in ARMS:
        for name in names:
            dest = ROOT / "inputs" / arm / aid(name)
            shutil.copytree(VIEWS / split[name] / "tasks" / name, dest)
            if arm == "leaderboard":
                (dest / "failure-evidence.md").write_text(audit_packets.failure_evidence(name, trials), encoding="utf-8")
        (ROOT / "outputs" / arm).mkdir(parents=True, exist_ok=True)
        order = [aid(n) for n in names]
        random.Random(f"{SEED}:{arm}").shuffle(order)
        batches = [order[i:i + PER_BATCH] for i in range(0, len(order), PER_BATCH)]
        for b, ids in enumerate(batches):
            path = ROOT / f"batch-{arm}-{b:02d}.json"
            path.write_text(json.dumps(ids) + "\n")
        manifest["batches"][arm] = len(batches)
    manifest["tasks"] = {aid(n): {"task": n, "split": split[n]} for n in names}
    (ROOT / "private-manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (ROOT / "taxonomy.md").write_text(taxonomy_markdown(), encoding="utf-8")
    print(len(names), "tasks;", manifest["batches"], "batches per arm")


if __name__ == "__main__":
    main()
