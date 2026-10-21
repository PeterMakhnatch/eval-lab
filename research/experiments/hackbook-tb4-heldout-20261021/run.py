"""Frozen-version run of ``evallab hackbook match`` on ``tb4-audit-v1`` (pB-SELF-6).

Every earlier task-side ``match`` number was measured on DEV tasks the catalog was written from.
This script scores one *frozen* version of the matcher and catalog on the key's TEST tasks, once.

The version id is ``hackbook-match@<12 hex>``: a digest of ``research/hackbook/entries.jsonl``,
``taxonomy.json`` and ``src/evallab/hackbook/*.py``, so unrelated commits do not change it. A TEST
run is refused when ``research/answer-keys/splits/exposure.jsonl`` already records that version
(add the record after the run), and needs ``--spend-the-test-split`` to run at all. DEV runs are
free. Only aggregate cells are written; per-task predictions are never printed or saved.

    python run.py --splits dev --out dev.json
    python run.py --splits dev,test --spend-the-test-split --out result.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.hackbook import match_target  # noqa: E402

EXPOSURE = REPO / "research/answer-keys/splits/exposure.jsonl"
DEFAULT_VIEWS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/agent-inputs"


def load_scorer() -> Any:
    path = REPO / "research/answer-keys/tb4-audit-v1/score_audit.py"
    spec = importlib.util.spec_from_file_location("score_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["score_audit"] = module
    spec.loader.exec_module(module)
    return module


def version_id() -> str:
    paths = [
        REPO / "research/hackbook/entries.jsonl",
        REPO / "research/hackbook/taxonomy.json",
        *sorted((REPO / "src/evallab/hackbook").glob("*.py")),
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(REPO).as_posix().encode() + b"\0" + path.read_bytes())
    return f"hackbook-match@{digest.hexdigest()[:12]}"


def already_exposed(version: str) -> bool:
    rows = [json.loads(line) for line in EXPOSURE.read_text().splitlines() if line.strip()]
    return any(version in str(row.get("system", "")) for row in rows)


def prior_baseline(sa: Any, key: Any) -> list[dict[str, Any]]:
    """The modal DEV category on every task: what a reader with no matcher would guess."""
    modal = Counter(f.category for f in key.findings if f.split == "dev").most_common(1)[0][0]
    return [sa._pred(task, modal, id=f"prior:{task}") for task in sorted(key.tasks)]


def per_class(key: Any, report: dict[str, Any], split: str) -> dict[str, list[int]]:
    credited = {item for _, item in report["matches"]["category_1to1"]}
    cells: dict[str, list[int]] = {}
    for finding in key.findings:
        if finding.split == split:
            cell = cells.setdefault(finding.taxonomy_class, [0, 0])
            cell[0] += finding.item_id in credited
            cell[1] += 1
    return dict(sorted(cells.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--splits", default="dev", help="dev or dev,test")
    ap.add_argument("--views", type=Path, default=DEFAULT_VIEWS)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--spend-the-test-split", action="store_true")
    args = ap.parse_args()
    splits = args.splits.split(",")
    version = version_id()
    if "test" in splits:
        if not args.spend_the_test_split:
            raise SystemExit("TEST needs --spend-the-test-split: each version may run it once")
        if already_exposed(version):
            raise SystemExit(f"{version} already has a TEST exposure record; refusing a second run")
    sa = load_scorer()
    key = sa.load_key()
    predictions: list[dict[str, Any]] = []
    for split in splits:
        with tempfile.TemporaryDirectory() as tmp:
            match_target(args.views / split, kind="view", emit_findings_dir=Path(tmp))
            predictions += sa.load_predictions(Path(tmp))
    systems = {
        version: predictions,
        "prior-modal-dev-category-on-every-task": prior_baseline(sa, key),
        **{name: row["predictions"] for name, row in sa.baseline_predictions().items()},
    }
    out: dict[str, Any] = {"version": version, "splits_run": splits, "systems": {}}
    for name, preds in systems.items():
        report = sa.score(key, preds)
        cells = sa.summarize(report)
        out["systems"][name] = {
            "predictions": report["predictions"],
            "cells": {c: cells[c] for c in splits},
        }
        if name == version:
            out["per_class_category_1to1"] = {s: per_class(key, report, s) for s in splits}
            out["predictions_by_category"] = dict(
                Counter(str(p.get("category")) for p in predictions)
            )
    args.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(version, "wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
