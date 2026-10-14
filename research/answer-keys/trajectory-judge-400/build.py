"""Import trajectory-judge (400 labelled trajectories) as answer key ``trajectory-judge-400``.

Upstream: https://github.com/mohammadi-hadi/trajectory-judge (MIT, Hadi Mohammadi),
commit ``056eb6c78012028dbc499841f6d89c8a29b91eb3``; ``upstream/`` holds unmodified
copies of ``data/{trajectories.jsonl,verdicts.jsonl,run.json,PROVENANCE.md}``,
``analysis/paired.json`` and ``LICENSE``. md5s are checked against upstream
``PROVENANCE.md`` before anything is written.

Label mapping (``qa.answer_key/v1``), one item per trajectory:

- clean (``faulty: false``, 100): ``expected_verdict: pass``;
- silent fault (``faulty`` and ``outcome_correct``, 175): the outcome a grader would
  reward is right but the process is broken, so the reward is not earned:
  ``expected_category: false_positive``, ``expected_verdict: fail``;
- loud fault (``faulty`` and not ``outcome_correct``, 125): the run failed and the
  question is why: ``expected_category: failure_cause``, ``expected_verdict: fail``.

Faulty items carry ``expected_root_cause`` = upstream ``failure_type`` and
``failure_step``. Clean items carry ``expected_category: false_positive`` (the
question a clean pass answers "no" to). Pairs: each faulty trajectory with the
clean trajectory of the same ``instance_id`` (251 of 300 have one in the 400; the
other 49 parents live only in upstream's ablation file and are not imported).
Split: ``dev``/``test`` by a seeded hash of ``instance_id``.

Usage (worktree root): ``uv run python research/answer-keys/trajectory-judge-400/build.py``
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / "upstream"
KEY_ID = "trajectory-judge-400"
SCHEMA = "qa.answer_key/v1"
COMMIT = "056eb6c78012028dbc499841f6d89c8a29b91eb3"
SPLIT_SEED = "trajectory-judge-400:split:20261014"
VENDORED = ("trajectories.jsonl", "verdicts.jsonl", "run.json")


def check_md5() -> None:
    expected = dict(
        (name, md5)
        for md5, name in re.findall(
            r"^([0-9a-f]{32})\s+(\S+)$", (UPSTREAM / "PROVENANCE.md").read_text(), re.M
        )
    )
    for name in VENDORED:
        got = hashlib.md5((UPSTREAM / name).read_bytes()).hexdigest()  # noqa: S324 - upstream's own checksum
        assert got == expected[name], f"{name}: md5 {got} != upstream PROVENANCE {expected[name]}"


def split_of(instance_id: str) -> str:
    digest = hashlib.sha256(f"{SPLIT_SEED}:{instance_id}".encode()).hexdigest()
    return "test" if int(digest, 16) % 2 else "dev"


def main() -> None:
    check_md5()
    rows = [
        json.loads(line)
        for line in (UPSTREAM / "trajectories.jsonl").read_text().splitlines()
        if line
    ]
    assert len(rows) == 400
    task_digest = (
        "sha256:" + hashlib.sha256((UPSTREAM / "trajectories.jsonl").read_bytes()).hexdigest()
    )
    clean_by_instance = {
        r["instance_id"]: r["trajectory_id"] for r in rows if not r["label"]["faulty"]
    }
    items, pairs = [], []
    for row in rows:
        label = row["label"]
        faulty = label["faulty"]
        if not faulty:
            category, verdict, root = "false_positive", "pass", None
        elif label["outcome_correct"]:
            category, verdict = "false_positive", "fail"
            root = {"failure_type": label["failure_type"], "failure_step": label["failure_step"]}
        else:
            category, verdict = "failure_cause", "fail"
            root = {"failure_type": label["failure_type"], "failure_step": label["failure_step"]}
        item_id = f"{KEY_ID}:{row['trajectory_id']}"
        parent = clean_by_instance.get(row["instance_id"]) if faulty else None
        pair_ids = []
        if parent:
            pair_id = f"{KEY_ID}:pair:{row['trajectory_id']}"
            pair_ids.append(pair_id)
            pairs.append(
                {
                    "pair_id": pair_id,
                    "parent": f"{KEY_ID}:{parent}",
                    "child": item_id,
                    "category": category,
                    "task_id": f"trajectory-judge/{row['instance_id']}",
                    "split": split_of(row["instance_id"]),
                    "match": "same_instance_clean_parent",
                }
            )
        items.append(
            {
                "schema": SCHEMA,
                "key_id": KEY_ID,
                "item_id": item_id,
                "subject": {
                    "kind": "trace",
                    "task_id": f"trajectory-judge/{row['instance_id']}",
                    "task_digest": task_digest,
                    "trial_id": row["trajectory_id"],
                },
                "trace_ref": {
                    "kind": "trajectory_judge",
                    "file": "upstream/trajectories.jsonl",
                    "commit": COMMIT,
                },
                "role": "child" if faulty else "parent",
                "expected_category": category,
                "expected_verdict": verdict,
                "expected_fault": "agent" if faulty else None,
                "expected_root_cause": root,
                "silent": bool(faulty and label["outcome_correct"]),
                "label_source": f"trajectory-judge {COMMIT[:12]} injected-fault labels (synthetic customer-support domain)",
                "split": split_of(row["instance_id"]),
                "pair_ids": pair_ids,
            }
        )
    by_id = {i["item_id"]: i for i in items}
    for pair in pairs:
        by_id[pair["parent"]]["pair_ids"].append(pair["pair_id"])
    (HERE / "items.jsonl").write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in items))
    (HERE / "pairs.jsonl").write_text("".join(json.dumps(p, sort_keys=True) + "\n" for p in pairs))
    n = {s: sum(1 for i in items if i["split"] == s) for s in ("dev", "test")}
    print(f"{len(items)} items ({n}), {len(pairs)} pairs")


if __name__ == "__main__":
    main()
