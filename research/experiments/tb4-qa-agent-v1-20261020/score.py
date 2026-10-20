"""Score QA agent v1 and the three comparison rows with the unchanged tb4-audit-v1 scorer.

Rows: QA agent v0 (frozen QA-2 predictions), static triage scanner (#1118, rerun here; it
is deterministic), plain Opus read (#1119 static arm predictions) and QA agent v1 (this
experiment's recorded findings). Every row goes through
``research/answer-keys/tb4-audit-v1/score_audit.py``; the table reports TEST recall and
TEST precision for ``category_1to1``, ``locus_and_class`` (strict) and ``task_any_1to1``
(task-level), with and without ``biped-contact-dynamics`` (exposed during v0 development).

Run from the repository root:
``.venv/bin/python research/experiments/tb4-qa-agent-v1-20261020/score.py --campaign test``
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KEY = REPO / "research/answer-keys/tb4-audit-v1"
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-20"
OUT = RESULTS / "tb4-qa-agent-v1"
VIEWS = RESULTS / "tb4-audit-v1/agent-inputs"
V0 = Path.home() / "Developer/eval-lab-data/open-qa-20261020/qa-task/qa2-frozen-test-20261020-01/predictions.jsonl"
READER = RESULTS / "tb4-reader-baseline/predictions-static-all.jsonl"
MODES = ("category_1to1", "locus_and_class", "task_any_1to1")
EXPOSED = "biped-contact-dynamics"


def task_of(row: dict[str, Any]) -> str:
    return row["subject"]["task_id"].removeprefix("terminal-bench/")


def v1_rows(campaign: str) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((OUT / campaign / "runs").glob("*/findings/*.json")):
        row = json.loads(path.read_text())
        row["id"] = f"v1:{path.parent.parent.name}:{path.stem[:12]}"
        rows.append(row)
    return rows


def v0_rows() -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in V0.read_text().splitlines() if line.strip()]
    # The frozen QA-2 rows may carry null ids (the schema allows it); give each a unique
    # task-scoped id, as QA-2's own scoring adapter did. No other field changes.
    for n, row in enumerate(rows):
        row["id"] = f"v0:{task_of(row)}:{row.get('id') or n}"
    return rows


def static_rows(work: Path) -> list[dict[str, Any]]:
    out = work / "static-triage"
    subprocess.run([sys.executable, str(KEY / "triage_signals.py"), "--dev-view", str(VIEWS / "dev"),
                    "--test-view", str(VIEWS / "test"), "--out", str(out)], check=True,
                   capture_output=True, cwd=REPO)
    files = sorted(out.rglob("*.jsonl")) if out.is_dir() else [out]
    rows = []
    for path in files:
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("schema") == "qa.finding/v1"]


def reader_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in READER.read_text().splitlines() if line.strip()]


def score(rows: list[dict[str, Any]], name: str, work: Path) -> dict[str, Any]:
    pred = work / f"predictions-{name}.jsonl"
    pred.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = work / f"score-{name}.json"
    subprocess.run([sys.executable, str(KEY / "score_audit.py"), "--predictions", str(pred),
                    "--out", str(out)], check=True, capture_output=True, cwd=REPO)
    return json.loads(out.read_text())


def row_cells(rows: list[dict[str, Any]], name: str, work: Path, items: dict[str, dict[str, Any]],
              split_tasks: set[str], split: str) -> dict[str, Any]:
    result = score(rows, name, work)
    split_items = {i for i, item in items.items() if item["split"] == split}
    nobiped_items = {i for i in split_items if items[i]["task"] != EXPOSED}
    claims = [r for r in rows if task_of(r) in split_tasks]
    nobiped_claims = [r for r in claims if task_of(r) != EXPOSED]
    cells: dict[str, Any] = {
        "split": split, "claims": len(claims), "claims_nobiped": len(nobiped_claims),
        "tasks_with_claim": len({task_of(r) for r in claims}),
        "claims_by_class": dict(Counter((r.get("root_cause") or [r.get("taxonomy_class")])[0]
                                        for r in claims)),
    }
    for mode in MODES:
        matched = {key_id for _, key_id in result["matches"][mode]}
        head = result["modes"][mode]["headline"][split]
        hits = len(matched & split_items)
        assert hits == head["recall"]["k"], (name, mode, hits, head["recall"])
        nobiped_hits = matched & nobiped_items
        cells[mode] = {
            "all": {"hits": hits, "findings": len(split_items), "claims": head["precision"]["n"],
                    "precision_hits": head["precision"]["k"]},
            "nobiped": {"hits": len(nobiped_hits), "findings": len(nobiped_items),
                        "claims": len(nobiped_claims)},
            "matched_item_ids": sorted(matched & split_items),
        }
    return cells


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", default="test")
    args = parser.parse_args()
    work = OUT / f"scoring-{args.campaign}"
    work.mkdir(parents=True, exist_ok=True)
    items = {}
    for line in (KEY / "items.jsonl").read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            if item["scope"] == "task":
                items[item["item_id"]] = item
    tasks = [json.loads(line) for line in (KEY / "tasks.jsonl").read_text().splitlines() if line.strip()]
    split = "test" if args.campaign == "test" else "dev"
    split_tasks = {t["task"] for t in tasks if t["split"] == split}
    table: dict[str, Any] = {"schema": "tb4-qa-agent-v1/score/v1", "campaign": args.campaign,
                             "split": split, "rows": {}}
    sources = {"v1": lambda: v1_rows(args.campaign)}
    if args.campaign == "test":
        sources = {"v0": v0_rows, "static_scanner": lambda: static_rows(work),
                   "plain_read": reader_rows, **sources}
    for name, load in sources.items():
        table["rows"][name] = row_cells(load(), name, work, items, split_tasks, split)
    if split == "dev":
        covered = sorted({task_of(r) for r in v1_rows(args.campaign)})
        table["note"] = ("DEV smoke: denominators are every DEV finding, but v1 ran only on the "
                         f"smoke tasks; read the per-task cells. Tasks with a claim: {covered}")
    target = HERE / f"results-{args.campaign}.json"
    target.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    for name, cells in table["rows"].items():
        line = [name, f"claims {cells['claims']}"]
        for mode in MODES:
            t, nb = cells[mode]["all"], cells[mode]["nobiped"]
            line.append(f"{mode} {t['hits']}/{t['findings']} (no biped {nb['hits']}/{nb['findings']}) "
                        f"prec {t['precision_hits']}/{t['claims']}")
        print(" | ".join(line))


if __name__ == "__main__":
    main()
