"""Export the checker's pool labels as one JSONL file for Data (HAR-108).

Each line has:
- task_id, label, label_without_repo, sample_labels, repo_used;
- items: the not_inferable and guessable items that decided the label, each with its test reference and quote;
- dropped_existing: items the repo check removed, with the file:line where the name exists.

Usage: python3 export_pool.py POOL_OUT_DIR OUT.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def _item(i: dict) -> dict:
    return {k: i.get(k) for k in ("severity", "kind", "what", "names", "test_ref", "quote")}


def main(pool_dir: Path, out: Path) -> None:
    lines = []
    for path in sorted(pool_dir.glob("*.json")):
        r = json.loads(path.read_text())
        lines.append({
            "task_id": r["task_id"],
            "label": r["label"],
            "label_without_repo": r["label_without_repo"],
            "sample_labels": r["sample_labels"],
            "repo_used": r["repo_used"],
            "items": [_item(i) for i in r["unstated"]],
            "dropped_existing": [{"names": i.get("names"), "repo_evidence": i.get("repo_evidence")}
                                 for i in r["dropped_existing"]],
        })
    out.write_text("".join(json.dumps(x) + "\n" for x in lines))
    counts = Counter(x["label"] for x in lines)
    flipped = sum(x["label_without_repo"] == "broken" and x["label"] != "broken" for x in lines)
    print(f"{len(lines)} tasks: {dict(counts)}; repo check turned {flipped} broken calls into suspect or sound")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
