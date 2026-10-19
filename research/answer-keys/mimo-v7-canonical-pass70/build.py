"""Register the 70 canonical MiMo v6 passes as a ``qa.answer_key/v1`` trace key.

Labels come only from the accepted v7 regrade (``heldout-v7.pass-labels/v1``,
pinned by SHA-256 in ``internal-views/source.json``): 6 ``wrong_pass`` (positive,
``expected_verdict: fail``) and 64 ``real_pass``. Actor inputs are the opaque
published full/actions views from ``internal_traces.py`` (PR #1087), bound by the
hashes in ``internal-views/views.jsonl``. Nothing is re-adjudicated or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = HERE.parent
REPO = KEYS.parents[1]
sys.path.insert(0, str(KEYS))
sys.path.insert(0, str(REPO / "src"))

from external_traces import canonical, file_digest  # noqa: E402
from internal_traces import canonical_item, jsonl  # noqa: E402

from evallab.qa.answer_key import load_answer_key  # noqa: E402
from evallab.results_home import results_root  # noqa: E402

KEY_ID = "mimo-v7-canonical-pass70"


def build(research_context: Path, results: Path) -> dict[str, int]:
    sources = json.loads((KEYS / "internal-views/source.json").read_text())["sources"]
    source = next(s for s in sources if s["dataset"] == KEY_ID)
    labels_path = research_context / source["labels"]["path"]
    if file_digest(labels_path) != source["labels_sha256"]:
        raise ValueError(f"{labels_path} does not match the pinned regrade hash")
    regrade = json.loads(labels_path.read_text())
    views = {
        v["source_item_id"]: v
        for v in jsonl(KEYS / "internal-views/views.jsonl")
        if v["dataset"] == KEY_ID
    }
    items = []
    for row in regrade["labels"]:
        item = canonical_item(row)
        view = views[item["item_id"]]
        for name, ref in (("full", view["full_ref"]), ("actions", view["actions_only_ref"])):
            trajectory = results / ref["path"] / "agent/trajectory.json"
            digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
            if digest != view["trajectory_sha256"][name]:
                raise ValueError(f"{item['item_id']}: {name} view hash mismatch")
        item["trial_ref"] = view["full_ref"]
        item["actions_only_ref"] = view["actions_only_ref"]
        item["trajectory_sha256"] = view["trajectory_sha256"]
        item["full_ref"] = view["full_ref"]
        items.append(item)
    items.sort(key=lambda i: i["item_id"])
    counts = {
        "fail": sum(i["expected_verdict"] == "fail" for i in items),
        "pass": sum(i["expected_verdict"] == "pass" for i in items),
    }
    if counts != source["verdict_counts"] or len(items) != source["n_items"]:
        raise ValueError(f"label counts {counts} differ from the pinned source")
    (HERE / "items.jsonl").write_bytes(b"".join(canonical(i) for i in items))
    if len(load_answer_key(HERE).items) != len(items):
        raise ValueError("answer-key loader lost items")
    return {"items": len(items), **counts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-context-root", type=Path, default=Path.home() / "Developer/research-context"
    )
    args = parser.parse_args()
    print(json.dumps(build(args.research_context_root, results_root()), sort_keys=True))


if __name__ == "__main__":
    main()
