"""Generate shard weights from the 3 newest successful PR observation runs.

Usage: uv run python scripts/shard_weights.py DOWNLOAD_DIRECTORY
Download all four test-observation artifacts per run into separate run directories.
This maintenance command is not a CI step. Repeat it after the timing profile changes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(directory: Path) -> dict:
    runs: dict[str, dict[tuple[str, str], dict]] = {}
    for path in sorted(directory.rglob("*.modules.json")):
        payload = json.loads(path.read_text())
        if payload["phase"] not in ("full-suite", "harbor-native"):
            continue
        if payload["exitstatus"] != 0 or payload["failed_modules"] or payload["collection_errors"]:
            continue
        if not payload["python_version"].startswith("3.12."):
            continue
        run = runs.setdefault(payload["run_id"], {})
        key = (payload["phase"], payload["shard"])
        if key in run:
            raise ValueError(f"duplicate observation: {payload['run_id']} {key}")
        run[key] = payload
    expected = {(phase, f"{index}/4") for phase in ("full-suite", "harbor-native") for index in range(1, 5)}
    selected = sorted((run for run, rows in runs.items() if rows.keys() == expected), key=int, reverse=True)[:3]
    if len(selected) < 3:
        raise ValueError("need at least 3 successful runs with all 4 base and native shards")
    samples: list[dict[str, list[float]]] = []
    for run in selected:
        rows = runs[run]
        if len({row["tested_sha"] for row in rows.values()}) != 1:
            raise ValueError(f"mixed tested revisions in run {run}")
        modules: dict[str, list[float]] = {}
        for (phase, _shard), row in sorted(rows.items()):
            index = 0 if phase == "full-suite" else 1
            for path, measurement in row["modules"].items():
                if path == "tests/test_ci_coverage.py":
                    continue  # This small coverage census runs on every shard.
                values = modules.setdefault(path, [0.0, 0.0])
                if values[index]:
                    raise ValueError(f"module appears on multiple shards: {run} {phase} {path}")
                values[index] = measurement["seconds"]
        samples.append(modules)
    paths = sorted(set().union(*(sample.keys() for sample in samples)))
    medians = {
        path: [statistics.median(sample.get(path, [0, 0])[index] for sample in samples) for index in (0, 1)]
        for path in paths
    }
    weighted = {path: [round(value) for value in values] for path, values in medians.items() if max(values) >= 4}
    unweighted = {}
    for total in range(1, 9):
        loads = [[0.0, 0.0] for _ in range(total)]
        for path, values in medians.items():
            if path not in weighted:
                shard = zlib.crc32(path.encode("utf-8")) % total
                for index in (0, 1):
                    loads[shard][index] += values[index]
        unweighted[str(total)] = [[round(value) for value in load] for load in loads]
    return {"source_runs": selected, "modules": weighted, "unweighted": unweighted}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "tests/shard-weights.json")
    args = parser.parse_args()
    try:
        weights = generate(args.directory)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(weights, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(weights['modules'])} module weights from {len(weights['source_runs'])} runs to {args.output}")


if __name__ == "__main__":
    main()
