#!/usr/bin/env python3
"""HAR-108 Python code pool and its Daytona nop specs.

Pool: every FineEnvs code task (pinned snapshot) whose ``metadata.category``
is ``Python`` or whose hidden test patch touches a ``.py`` file. Both splits
are included: a nop runs no model, so held-out tasks are not exposed.

Nop order is ``sha256("har108:" + task_id)``, a seeded shuffle, so any prefix
cut by the budget is a representative sample of the pool rather than its
lightest corner. Tasks that already have a Daytona nop row for the same
``task_version_digest`` are not re-run.

Writes ``pool.json`` and ``specs/har108-nop-<id>.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import PRIMARY, ROOT, SNAPSHOTS, nop_spec  # noqa: E402

SEED = "har108:"
PATCHED = re.compile(r"^diff --git a/(\S+)", re.M)
HARNESS_FILES = {"mimo_test_command.sh", "test_commands.json"}
SIZES = ROOT / "research/experiments/mimo-daytona-nop/image-sizes.json"


def rank(task_id: str) -> str:
    return hashlib.sha256((SEED + task_id).encode()).hexdigest()


def main() -> None:
    split = json.loads((ROOT / "research/experiments/har81-mimo-sft/split.json").read_text())
    catalog = PRIMARY / "derived/parquet/external/task_catalog"
    nopped = {
        row["task_version_digest"]: row["job_name"]
        for row in pq.read_table(catalog / "task_qualification.parquet").to_pylist()
        if row["backend"] == "daytona" and row["agent_name"] == "nop"
    }
    mib = json.loads(SIZES.read_text())["mib"]
    pool = []
    for task in split["tasks"]:
        if task["domain"] != "code":
            continue
        rel = f"derived/task-store/hf/{SNAPSHOTS['code']}/tasks/{task['task_id']}"
        path = PRIMARY / rel
        config = tomllib.loads((path / "task.toml").read_text())
        files = [
            f
            for f in PATCHED.findall((path / "tests/test.patch").read_text(errors="replace"))
            if f not in HARNESS_FILES
        ]
        category = config["metadata"].get("category")
        if category != "Python" and not any(f.endswith(".py") for f in files):
            continue
        image = config["environment"]["docker_image"]
        pool.append(
            {
                "task_id": task["task_id"],
                "task_version_digest": task["task_version_digest"],
                "split": task["split"],
                "split_group": task["split_group"],
                "category": category,
                "task": rel,
                "image_mib": mib.get(image.split("sha256:")[1][:12]),
                "rank": rank(task["task_id"])[:12],
                "existing_nop": nopped.get(task["task_version_digest"]),
            }
        )
    pool.sort(key=lambda e: e["rank"])
    specs = HERE / "specs"
    specs.mkdir(exist_ok=True)
    for entry in pool:
        if entry["existing_nop"]:
            continue
        name = f"har108-nop-{entry['task_id'].removeprefix('format-code-task-')}"
        spec_path = specs / f"{name}.json"
        if not spec_path.exists():
            spec = nop_spec(
                entry["task"],
                name,
                "Census nop: the Python code task starts, applies its hidden tests and "
                "grades on Daytona with reward 0 and no setup error.",
            )
            spec["submitted_by"] = "har108-census"
            spec_path.write_text(json.dumps(spec, indent=2) + "\n")
        entry["spec"] = f"specs/{name}.json"
    (HERE / "pool.json").write_text(
        json.dumps({"card": "HAR-108", "seed": SEED, "n": len(pool), "pool": pool}, indent=1) + "\n"
    )
    print(len(pool), "tasks;", sum(1 for e in pool if e["existing_nop"]), "already nopped")


if __name__ == "__main__":
    main()
