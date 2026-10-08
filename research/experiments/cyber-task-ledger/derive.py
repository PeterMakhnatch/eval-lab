#!/usr/bin/env python3
"""Derive ``cyber-instruction-submit@1`` for every ledger ``keep`` task.

Reads ``ledger.csv`` (built by ``build.py``); derives the instruction variant
with the pinned Harbor parent source. Idempotent: existing records/packages
are refused by ``derive_task`` and skipped here, so re-running derives only
what is missing. Run once, then re-run ``build.py`` to fill ``variant_record``.

```bash
uv run python research/experiments/cyber-task-ledger/derive.py
```
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from evallab.cyber_instruction import derive_cyber_instruction_submit  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402
from evallab.task_variants import VariantExistsError  # noqa: E402

REVISION = "763882ade5fc018892f1aa3c559f997138eb92cc"
REPO = "FineEnvs/MiMo-V2.6-RL-harbor-cyber"


def main() -> None:
    tasks_root = (
        shared_checkout_root(ROOT) / "derived/task-store/hf"
        f"/{REPO.replace('/', '__')}@763882ade5fc/tasks"
    )
    keeps: list[str] = []
    with open(HERE / "ledger.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["verdict"] == "keep":
                keeps.append(row["task_id"])
    derived = skipped = 0
    for task in sorted(keeps):
        try:
            derive_cyber_instruction_submit(
                tasks_root / task,
                created_by="cyber-ledger",
                repo_root=ROOT,
                parent_source={
                    "kind": "hf",
                    "repo": REPO,
                    "revision": REVISION,
                    "path": f"tasks/{task}",
                    "record": None,
                },
            )
            derived += 1
        except VariantExistsError:
            skipped += 1
    print(f"keeps={len(keeps)} derived={derived} skipped={skipped}")


if __name__ == "__main__":
    main()
