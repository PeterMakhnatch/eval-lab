#!/usr/bin/env python3
"""Build the tb2-to-tb21-v1 version-diff answer key (qa.answer_key/v1).

TVB-style: 26 tasks modified by terminal-bench-2 PR #53 (TB2 -> TB2.1)
plus 26 unmodified (format-only) controls. A defect finding counts only
if its reproducer scores 1.0 on the TB2 package and 0.0 on the TB2.1
package; any validated reproducer on a control is a false discovery.

Source of truth: research/experiments/breaker-rediscovery-v1/tb2-diff.json
(pins harbor-framework/terminal-bench-2@69671fba and
terminal-bench-2-1@7131e437). Control selection is deterministic: the
first 26 format_only tasks sorted by task_id.

Usage: build.py [--check]
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DIFF = REPO / "research/experiments/breaker-rediscovery-v1/tb2-diff.json"
KEY_ID = "tb2-to-tb21-v1"
CONTROL_COUNT = 26


def _items() -> list[dict]:
    diff = json.loads(DIFF.read_text())
    tasks = diff["tasks"]
    modified = sorted(
        (t for t in tasks if t.get("in_pr53_26")), key=lambda t: t["task_id"]
    )
    controls = sorted(
        (t for t in tasks if t["change_kind"] == "format_only"),
        key=lambda t: t["task_id"],
    )[:CONTROL_COUNT]
    assert len(modified) == 26, len(modified)
    assert len(controls) == CONTROL_COUNT, len(controls)

    # Exposure: tasks whose TB2 version appeared in breaker-bench-v1 dev
    # tuning pairs (scaffold was tuned there; disclose, do not hide).
    dev_tb2: set[str] = set()
    pairs_path = REPO / "research/answer-keys/breaker-bench-v1/pairs.jsonl"
    for line in pairs_path.read_text().splitlines():
        if not line.strip():
            continue
        p = json.loads(line)
        if p["split"] == "dev" and p["benchmark"] == "terminal-bench-2":
            dev_tb2.add(p["task_id"].split("/", 1)[1])

    items: list[dict] = []
    for group, cohort in (("modified", modified), ("control", controls)):
        for t in cohort:
            tid = t["task_id"]
            for version, digest_key, verdict in (
                ("tb2", "old_package_digest", "fail" if group == "modified" else "pass"),
                ("tb21", "new_package_digest", "pass"),
            ):
                items.append(
                    {
                        "schema": "qa.answer_key/v1",
                        "key_id": KEY_ID,
                        "item_id": f"{KEY_ID}:{version}:{tid}",
                        "subject": {
                            "kind": "task",
                            "task_id": f"{version}/{tid}",
                            "task_digest": t[digest_key],
                        },
                        "expected_verdict": verdict,
                        "expected_category": "weak",
                        "expected_fault": "eval",
                        "expected_mechanisms": [t["change_kind"]],
                        "expected_root_cause": f"tb2-to-tb21:{t['change_kind']}",
                        "split": "test",
                        "subsplit": f"pr53_{group}" if group == "modified" else "format_only_control",
                        "source_set": "tb2_to_tb21_pr53",
                        "role": "child" if version == "tb2" else "parent",
                        "pair_ids": [f"{KEY_ID}:pair:{tid}"],
                        "paired_counterpart_id": (
                            f"{KEY_ID}:tb21:{tid}" if version == "tb2" else f"{KEY_ID}:tb2:{tid}"
                        ),
                        "change_kind": t["change_kind"],
                        "breaker_bench_dev_exposure": tid in dev_tb2,
                        "label_source": (
                            "harbor-framework/terminal-bench-2@69671fba -> "
                            "terminal-bench-2-1@7131e437 PR #53 cohort; "
                            "tb2-diff.json (breaker-rediscovery-v1)"
                        ),
                    }
                )
    items.sort(key=lambda r: r["item_id"])
    return items


def _render(items: list[dict]) -> bytes:
    return "".join(
        json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in items
    ).encode()

def _pairs(items: list[dict]) -> list[dict]:
    """Canonical pairs for the scorer: child = TB2 (faulty on modified
    tasks), parent = TB2.1 (clean). Without pairs.jsonl the scorer
    loads 0 pairs and paired_discrimination is n/a (Verifier A043)."""
    by_id = {r["item_id"]: r for r in items}
    pairs = []
    for r in items:
        if r["subject"]["task_id"].startswith("tb2/"):
            parent = by_id[r["paired_counterpart_id"]]
            pairs.append(
                {
                    "pair_id": r["pair_ids"][0],
                    "child": r["item_id"],
                    "parent": parent["item_id"],
                    "category": r["expected_category"],
                    "split": r["split"],
                    "mechanism": r["change_kind"],
                    "task_id": r["subject"]["task_id"].split("/", 1)[1],
                }
            )
    pairs.sort(key=lambda p: p["pair_id"])
    return pairs


def main() -> int:
    items = _items()
    blob = _render(items)
    pairs = _pairs(items)
    pairs_blob = _render(pairs)
    manifest = {
        "key_id": KEY_ID,
        "items": len(items),
        "pairs": len(pairs),
        "tasks": 52,
        "modified_tasks": 26,
        "control_tasks": 26,
        "items_sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
        "pairs_sha256": "sha256:" + hashlib.sha256(pairs_blob).hexdigest(),
        "source": "research/experiments/breaker-rediscovery-v1/tb2-diff.json",
        "control_selection": "first 26 format_only tasks sorted by task_id",
    }
    if "--check" in sys.argv:
        ok = (
            (HERE / "items.jsonl").read_bytes() == blob
            and (HERE / "pairs.jsonl").read_bytes() == pairs_blob
            and json.loads((HERE / "manifest.json").read_text()) == manifest
        )
        print("OK" if ok else "STALE")
        return 0 if ok else 1
    (HERE / "items.jsonl").write_bytes(blob)
    (HERE / "pairs.jsonl").write_bytes(pairs_blob)
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(items)} items, {len(pairs)} pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
