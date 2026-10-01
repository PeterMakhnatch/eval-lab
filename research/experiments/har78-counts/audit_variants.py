"""Structural audit of HAR-113 leak and repair variants against their records.

A finding is a changed path outside ``environment/`` and ``task.toml``, or a
nop reward of 1 after the change. Missing package trees and missing nop jobs
are reported as unchecked, not as passes.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[3]
VARIANTS = REPO / "research/experiments/har113-variants"
CENSUS = REPO / "research/experiments/har108-python-census/task_health.parquet"
WORKTREES = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees")
ALLOWED_PREFIXES = ("environment/",)
ALLOWED_FILES = {"task.toml"}
FORBIDDEN_PREFIXES = ("tests/", "verifier/", "solution/")
FORBIDDEN_FILES = {"instruction.md"}


def _allowed(path: str) -> bool:
    if path in FORBIDDEN_FILES or path.startswith(FORBIDDEN_PREFIXES):
        return False
    return path in ALLOWED_FILES or path.startswith(ALLOWED_PREFIXES)


def _load_manifest(name: str) -> dict:
    payload = json.loads((VARIANTS / name).read_text(encoding="utf-8"))
    return payload["tasks"]


def _record(entry: dict) -> dict | None:
    relative = entry.get("record")
    if not isinstance(relative, str):
        return None
    path = REPO / relative
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _nop_reward(job_name: str | None) -> tuple[float | None, str]:
    if not job_name or not WORKTREES.is_dir():
        return None, "no nop job recorded"
    matches = list(WORKTREES.glob(f"*/runs/{job_name}/*/result.json"))
    if not matches:
        return None, "nop job not on disk"
    rewards = []
    for path in matches:
        result = json.loads(path.read_text(encoding="utf-8"))
        verifier = result.get("verifier_result") or {}
        reward = (verifier.get("rewards") or {}).get("reward")
        rewards.append(reward)
    if any(reward == 1.0 or reward == 1 for reward in rewards):
        return 1.0, str(matches[0])
    if rewards and all(reward == 0.0 or reward == 0 for reward in rewards):
        return 0.0, str(matches[0])
    return None, f"nop reward unreadable: {rewards[:3]}"


def audit() -> dict:
    findings = []
    unchecked_nop = 0
    checked_nop_fail = 0
    for kind, filename in (("leak", "leak_variants.json"), ("repair", "repair_variants.json")):
        for task_id, entry in _load_manifest(filename).items():
            record = _record(entry)
            if record is None:
                findings.append({"kind": kind, "task": task_id, "finding": "record missing"})
                continue
            for item in record.get("files") or []:
                path = item.get("path")
                if isinstance(path, str) and not _allowed(path):
                    findings.append(
                        {
                            "kind": kind,
                            "task": task_id,
                            "finding": "unexpected file",
                            "path": path,
                        }
                    )
            nop_name = entry.get("nop_job")
            reward, where = _nop_reward(nop_name if isinstance(nop_name, str) else None)
            if reward == 1.0:
                findings.append({"kind": kind, "task": task_id, "finding": "nop passed", "where": where})
            elif reward == 0.0:
                checked_nop_fail += 1
            else:
                unchecked_nop += 1
    rows = pq.read_table(CENSUS).to_pylist() if CENSUS.is_file() else []
    labels = Counter(row["label"] for row in rows)
    nop_rewards = Counter("none" if row["nop_reward"] is None else str(row["nop_reward"]) for row in rows)
    return {
        "variants": {
            "leak": len(_load_manifest("leak_variants.json")),
            "repair": len(_load_manifest("repair_variants.json")),
        },
        "findings": findings,
        "nop_clean_fail": checked_nop_fail,
        "nop_unchecked": unchecked_nop,
        "census_rows": len(rows),
        "census_labels": dict(labels),
        "census_nop_rewards": dict(nop_rewards),
        "card_before_har113": {
            "rows": 1180,
            "sound": 493,
            "broken_environment": 59,
            "grader_suspect": 2,
            "unknown": 626,
        },
    }


def main() -> None:
    report = audit()
    out = Path(__file__).with_name("variant-audit.json")
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "findings"}, indent=2))
    print("findings", len(report["findings"]))
    for finding in report["findings"][:30]:
        print(finding)


if __name__ == "__main__":
    main()
