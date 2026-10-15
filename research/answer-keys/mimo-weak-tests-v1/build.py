"""Build `mimo-weak-tests-v1`: MiMo tasks whose hidden tests accept wrong work, plus controls.

The steps of the loop it scores: find weak tests by execution -> repair the tests ->
regrade old passes. It carries two subject kinds, and an item's task decides its split:

Task items (does this package's grader accept wrong work?):
- `fail weak`, `partial_fix_pass`: the 3 packages on which an honest agent's partial
  fix was rewarded 1. That is HAR-207's diff-vs-reference review, statically confirmed
  by the Verifier tab (bridge/overnight-2026-10-14/verifier.md section 2).
- `fail weak`, `ref_subset_confirmed`: a strict subset of the reference fix passes
  (`evallab hack ref-subset`: full=1, nop=0, subset=1), and the refutation read
  (`_triage/*.jsonl`) confirms the dropped code is behaviour the instruction asks for.
- `pass refuted_claim`: ref-subset flags the read refuted (out of scope or neutral).
  These are hard negatives.
- `pass weak_negative`: clean controls, a seeded sample of ref-subset `pass` tasks
  (every reference block is needed) that no other source has flagged. Not proof of clean.

Trace items (is this pass's reward real?):
- `fail false_positive`: the 4 partial-fix passes (expected_regrade `flips`).
- `pass`: their clean parents from trace-paired-v1 (oracle, or an audited honest pass).

    uv run python research/answer-keys/mimo-weak-tests-v1/build.py [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

KEY_ID = "mimo-weak-tests-v1"
HERE = Path(__file__).resolve().parent
KEYS = HERE.parent
REPO = KEYS.parents[1]
HOME = Path.home()
LAB = HOME / "Developer/eval-lab"
REF_SUBSET = LAB / "derived/qa/ref-subset/mimo-v2.6-rl-code"
OWN_SCAN = LAB / "derived/detector-roots/own-scan"
HUNT_SELECTION = REPO / "research/experiments/envcheck-overnight-2026-10-13/selection.json"
MIMO = "mimo-v2.6-rl"
SEED = "mimo-weak-tests-v1:20261015"
SPLIT_SEED = "mimo-weak-tests-v1:split:20261015"
PARTIAL_FIX_PASSES = (
    "ovn-g5-001809-stock__PZ8sw7f",
    "ovn-g5-001809-gepa__nxdBvhh",
    "har168-20261006-001985-a2__gS4XiYH",
    "har168-20261006-002486-a2__MSfgbhR",
)
HAR207 = (
    "HAR-207 diff-vs-reference review (derived/detector-roots/own-scan/notes/"
    "GraderDesign.result.json: partial_fix_passed), confirmed by the Verifier tab "
    "(bridge/overnight-2026-10-14/verifier.md section 2)"
)


def jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def tilde(path: Path) -> str:
    text = str(path)
    return "~" + text[len(str(HOME)) :] if text.startswith(str(HOME)) else text


def name_of(task_id: str) -> str:
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def locked_splits() -> dict[str, str]:
    """Task name -> split already locked by another key ('heldout' maps to 'test')."""
    out: dict[str, str] = {}
    for key_id in ("trace-paired-v1", "executed-outcomes-v1"):
        for item in jsonl(KEYS / key_id / "items.jsonl"):
            split = "test" if item["split"] in ("test", "heldout") else "dev"
            out.setdefault(name_of(item["subject"]["task_id"]), split)
    return out


def split_for(name: str, locked: dict[str, str]) -> str:
    if name in locked:
        return locked[name]
    bucket = int.from_bytes(hashlib.sha256(f"{SPLIT_SEED}:{name}".encode()).digest()[:4], "big") % 3
    return "dev" if bucket == 0 else "test"


def task_item(name: str, digest: str, root: str, **fields) -> dict:
    return {
        "schema": "qa.answer_key/v1",
        "key_id": KEY_ID,
        "item_id": f"{KEY_ID}:task:{name}@{digest[7:19]}",
        "subject": {"kind": "task", "task_id": f"{MIMO}/{name}", "task_digest": digest},
        "package": {"name": name, "root_hint": root},
        "role": None,
        "pair_ids": [],
        **fields,
    }


def build() -> tuple[list[dict], list[dict]]:
    locked = locked_splits()
    reports = {
        p.parent.name: json.loads(p.read_text())
        for p in sorted(REF_SUBSET.glob("format-code-task-*/report.json"))
    }
    triage = {
        row["task_id"]: row | {"_file": path.name}
        for path in sorted((REF_SUBSET / "_triage").glob("*.jsonl"))
        for row in jsonl(path)
    }
    paired = {i["item_id"]: i for i in jsonl(KEYS / "trace-paired-v1/items.jsonl")}
    paired_pairs = jsonl(KEYS / "trace-paired-v1/pairs.jsonl")
    review = json.loads((OWN_SCAN / "notes/GraderDesign.result.json").read_text())
    why = {p["trial_id"]: p for p in review["suspicious_passes"]}
    items: list[dict] = []
    pairs: list[dict] = []

    # Partial-fix passes: trace children, their trace-paired parents, and the packages.
    packages: dict[tuple[str, str], list[str]] = {}
    for trial in PARTIAL_FIX_PASSES:
        if why[trial]["verdict"] != "partial_fix_passed":
            raise ValueError(f"{trial}: HAR-207 verdict is {why[trial]['verdict']}")
        child = paired[f"trace-paired-v1:{trial}"]
        name = name_of(child["subject"]["task_id"])
        pair = next(p for p in paired_pairs if p["child"] == child["item_id"])
        parent = paired[pair["parent"]]
        split = split_for(name, locked)
        pair_id = f"{KEY_ID}:pair:{trial}"
        child_id = f"{KEY_ID}:trace:{trial}"
        parent_id = f"{KEY_ID}:trace:{parent['subject']['trial_id']}"
        items.append(
            {
                "schema": "qa.answer_key/v1",
                "key_id": KEY_ID,
                "item_id": child_id,
                "subject": child["subject"],
                "trial_ref": child["trial_ref"],
                "role": "child",
                "pair_ids": [pair_id],
                "expected_verdict": "fail",
                "expected_category": "false_positive",
                "expected_fault": "eval",
                "expected_root_causes": ["partial_fix_pass"],
                "expected_regrade": "flips",
                "evidence": why[trial]["why"],
                "label_source": HAR207,
                "label_strength": "confirmed",
                "selected_by": ["har207-review"],
                "split": split,
            }
        )
        if parent_id not in {i["item_id"] for i in items}:
            items.append(
                {
                    "schema": "qa.answer_key/v1",
                    "key_id": KEY_ID,
                    "item_id": parent_id,
                    "subject": parent["subject"],
                    "trial_ref": parent["trial_ref"],
                    "role": "parent",
                    "pair_ids": [],
                    "expected_verdict": "pass",
                    "expected_category": None,
                    "expected_fault": None,
                    "expected_root_causes": [],
                    "expected_regrade": "stays",
                    "label_source": f"trace-paired-v1 clean parent ({parent['label_source']})",
                    "label_strength": "confirmed",
                    "selected_by": ["trace-paired-v1"],
                    "split": split,
                }
            )
        next(i for i in items if i["item_id"] == parent_id)["pair_ids"].append(pair_id)
        pairs.append(
            {
                "pair_id": pair_id,
                "parent": parent_id,
                "child": child_id,
                "category": "false_positive",
                "match": pair["match"],
                "task_id": child["subject"]["task_id"],
                "split": split,
            }
        )
        packages.setdefault((name, child["subject"]["task_digest"]), []).append(trial)

    for (name, digest), trials in sorted(packages.items()):
        items.append(
            task_item(
                name,
                digest,
                "package the partial-fix trials ran on (see trial_ref of the trace items)",
                expected_verdict="fail",
                expected_category="weak",
                expected_fault="eval",
                expected_root_causes=["partial_fix_pass"],
                evidence="; ".join(f"{t}: {why[t]['why']}" for t in trials),
                label_source=HAR207,
                label_strength="confirmed",
                selected_by=["har207-review"],
                split=split_for(name, locked),
            )
        )

    # ref-subset: confirmed weak tests and refuted flags. A refuted flag only refutes the
    # dropped block; when executed-outcomes-v1 has other executed evidence that the same
    # package's tests are weak (#867 surviving mutants), the flag is left out instead of
    # becoming a `pass` that contradicts it.
    weak_elsewhere = {
        i["subject"]["task_digest"]
        for i in jsonl(KEYS / "executed-outcomes-v1/items.jsonl")
        if i["expected_verdict"] == "fail"
    }
    for name, row in sorted(triage.items()):
        report = reports[name]
        if report["verdict"] != "fail" or report["status"] not in ("measured", "budget_exhausted"):
            raise ValueError(f"{name}: triaged but no executed ref-subset fail")
        weak = row["label"] == "confirmed_weak"
        if not weak and report["package_digest"] in weak_elsewhere:
            continue
        items.append(
            task_item(
                name,
                report["package_digest"],
                tilde(Path(report["package"]).parent),
                expected_verdict="fail" if weak else "pass",
                expected_category="weak" if weak else None,
                expected_fault="eval" if weak else None,
                expected_root_causes=["untested_reference_block"] if weak else [],
                evidence=(row.get("cheap_pass") or row["reason"]) if weak else row["reason"],
                label_source="evallab hack ref-subset executed subset grade (full=1, nop=0, subset=1) "
                f"+ refutation read {row['_file']} ({row['label']}); {tilde(REF_SUBSET / name)}",
                label_strength="confirmed" if weak else "refuted_claim",
                selected_by=["ref-subset"],
                split=split_for(name, locked),
            )
        )

    # Clean controls: ref-subset passes that no source has flagged. The sample is drawn
    # once and frozen in controls.json, because the sweep keeps adding pass tasks.
    controls_path = HERE / "controls.json"
    if controls_path.exists():
        frozen = json.loads(controls_path.read_text())
        chosen, pool_size = frozen["tasks"], frozen["pool_size"]
    else:
        flagged = set(triage) | {name for name, _ in packages}
        flagged |= {name_of(p["task"]) for p in review["suspicious_passes"]}
        flagged |= {
            name_of(t["task_id"]) for t in json.loads(HUNT_SELECTION.read_text())["selected"]
        }
        for key_id in ("trace-paired-v1", "executed-outcomes-v1"):
            flagged |= {
                name_of(i["subject"]["task_id"]) for i in jsonl(KEYS / key_id / "items.jsonl")
            }
        pool = sorted(
            name
            for name, r in reports.items()
            if r["status"] == "measured" and r["verdict"] == "pass" and name not in flagged
        )
        n = sum(
            1 for i in items if i["subject"]["kind"] == "task" and i["expected_verdict"] == "fail"
        )
        chosen, pool_size = sorted(random.Random(SEED).sample(pool, n)), len(pool)
        controls_path.write_text(
            json.dumps({"seed": SEED, "pool_size": pool_size, "tasks": chosen}, indent=2) + "\n"
        )
    for name in chosen:
        report = reports[name]
        if report["verdict"] != "pass":
            raise ValueError(f"control {name} is no longer a ref-subset pass")
        items.append(
            task_item(
                name,
                report["package_digest"],
                tilde(Path(report["package"]).parent),
                expected_verdict="pass",
                expected_category=None,
                expected_fault=None,
                expected_root_causes=[],
                evidence=f"ref-subset pass: every one of {report['code_blocks']} reference code blocks is needed",
                label_source=f"evallab hack ref-subset pass, seeded control sample ({SEED}) from "
                f"{pool_size} unflagged pass tasks (controls.json); not proof the tests are strong",
                label_strength="weak_negative",
                selected_by=["ref-subset"],
                split=split_for(name, locked),
            )
        )

    items.sort(key=lambda i: i["item_id"])
    return items, pairs


def render(rows: list[dict]) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed files")
    args = parser.parse_args()
    items, pairs = build()
    outputs = {HERE / "items.jsonl": render(items), HERE / "pairs.jsonl": render(pairs)}
    if args.check:
        stale = [p.name for p, text in outputs.items() if not p.exists() or p.read_text() != text]
        if stale:
            print(f"stale: {stale}; sources changed since the last build", file=sys.stderr)
            return 1
    else:
        for path, text in outputs.items():
            path.write_text(text, encoding="utf-8")
    counts: dict[tuple, int] = {}
    for i in items:
        k = (i["subject"]["kind"], i["expected_verdict"], i["label_strength"], i["split"])
        counts[k] = counts.get(k, 0) + 1
    for k, n in sorted(counts.items()):
        print(n, *k)
    print(f"{len(items)} items, {len(pairs)} pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
