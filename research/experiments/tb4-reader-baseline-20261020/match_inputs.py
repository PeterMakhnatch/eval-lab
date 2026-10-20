"""Build per-task semantic-match packets: key findings vs the two reader arms (anonymised A/B).

Output: ``<results>/match/<aid>.json`` and ``match-batch-NN.json`` (6 tasks each), plus
``match-private.json`` mapping A/B to arms. Run after both arms' outputs exist.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import build_inputs as bi

ROOT = bi.ROOT
KEY = bi.HERE.parents[1] / "answer-keys/tb4-audit-v1"
PM_SECTIONS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-run-census/pm-sections.json"
PER_BATCH = 6
PASS = sys.argv[1] if len(sys.argv) > 1 else ""  # "" first pass, "2" independent second pass


def reader_findings(arm: str, aid: str) -> list[dict[str, Any]]:
    data = json.loads((ROOT / "outputs" / arm / f"{aid}.json").read_text())
    return [{"idx": i, "class": f.get("class"), "confidence": f.get("confidence"), "title": f.get("title"),
             "summary": f.get("summary"), "evidence_refs": f.get("evidence_refs"),
             "instruction_quote": f.get("instruction_quote"), "grader_quote": f.get("grader_quote")}
            for i, f in enumerate(data["findings"])]


def main() -> None:
    manifest = json.loads((ROOT / "private-manifest.json").read_text())
    items = [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines() if line.strip()]
    items = [i for i in items if i["scope"] == "task"]
    pm = json.loads(PM_SECTIONS.read_text())
    (ROOT / f"match{PASS}").mkdir(exist_ok=True)
    (ROOT / f"match-out{PASS}").mkdir(exist_ok=True)
    private: dict[str, Any] = {}
    aids = sorted(manifest["tasks"])
    for aid in aids:
        task = manifest["tasks"][aid]["task"]
        arms = list(bi.ARMS)
        random.Random(f"{bi.SEED}:match{PASS}:{aid}").shuffle(arms)
        private[aid] = {"A": arms[0], "B": arms[1], "task": task}
        packet = {
            "aid": aid, "task": task,
            "key_findings": [{"item_id": i["item_id"], "title": i["title"], "class": i["taxonomy_class"],
                              "category": i["expected_category"]} for i in items if i["task"] == task],
            "preference_model_text_for_task": pm.get(task),
            "list_A": reader_findings(arms[0], aid), "list_B": reader_findings(arms[1], aid),
        }
        (ROOT / f"match{PASS}" / f"{aid}.json").write_text(json.dumps(packet, indent=1) + "\n")
    order = aids[:]
    random.Random(f"{bi.SEED}:match-batches{PASS}").shuffle(order)
    batches = [order[i:i + PER_BATCH] for i in range(0, len(order), PER_BATCH)]
    for b, ids in enumerate(batches):
        (ROOT / f"match{PASS}-batch-{b:02d}.json").write_text(json.dumps(ids) + "\n")
    (ROOT / f"match{PASS}-private.json").write_text(json.dumps(private, indent=1) + "\n")
    print(len(aids), "packets;", len(batches), "batches")


if __name__ == "__main__":
    main()
