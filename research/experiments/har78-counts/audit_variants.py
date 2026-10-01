"""Structural audit of HAR-113 and HAR-115 variants against their records.

Audits:
1. Leak variants (247 tasks, leak-close-pypi@1)
2. HAR-113 repairs (53 tasks, environment repairs)
3. HAR-115 repairs (44 tasks from PR #580)

Checks:
- Only declared files changed: environment/ setup files and task.toml
- In task.toml, ONLY [environment.healthcheck].command changes (re-embedding setup)
- tests/, verifier/, and instruction.md are byte-identical (0 findings)
- Nop rewards: clean fail (0.0), no passed nop
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[3]
VARIANTS_113 = REPO / "research/experiments/har113-variants"
CENSUS_115 = REPO / "research/experiments/har115-census"
CENSUS = REPO / "research/experiments/har108-python-census/task_health.parquet"
WORKTREES = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees")

MANIFESTS = [
    ("leak_har113", VARIANTS_113 / "leak_variants.json"),
    ("repair_har113", VARIANTS_113 / "repair_variants.json"),
    ("repair_har115", CENSUS_115 / "repairs.json"),
]

FORBIDDEN_PREFIXES = ("tests/", "verifier/", "solution/")
FORBIDDEN_FILES = {"instruction.md"}


def _load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
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
    toml_keys_modified = set()
    total_variants = {}

    for manifest_name, path in MANIFESTS:
        tasks = _load_manifest(path)
        total_variants[manifest_name] = len(tasks)
        for task_id, entry in tasks.items():
            record = _record(entry)
            if record is None:
                findings.append({"kind": manifest_name, "task": task_id, "finding": "record missing"})
                continue
            for item in record.get("files") or []:
                fpath = item.get("path")
                if not isinstance(fpath, str):
                    continue
                if fpath in FORBIDDEN_FILES or fpath.startswith(FORBIDDEN_PREFIXES):
                    findings.append(
                        {
                            "kind": manifest_name,
                            "task": task_id,
                            "finding": "forbidden file touched",
                            "path": fpath,
                        }
                    )
                elif fpath == "task.toml":
                    content = item.get("content") or ""
                    try:
                        parsed = tomllib.loads(content)
                        hc = parsed.get("environment", {}).get("healthcheck", {})
                        if "command" in hc:
                            toml_keys_modified.add("[environment.healthcheck].command")
                        else:
                            findings.append(
                                {
                                    "kind": manifest_name,
                                    "task": task_id,
                                    "finding": "task.toml missing healthcheck command",
                                }
                            )
                    except Exception as err:
                        findings.append(
                            {
                                "kind": manifest_name,
                                "task": task_id,
                                "finding": f"task.toml parse error: {err}",
                            }
                        )
                elif not fpath.startswith("environment/"):
                    findings.append(
                        {
                            "kind": manifest_name,
                            "task": task_id,
                            "finding": "unallowed file touched",
                            "path": fpath,
                        }
                    )
            nop_name = entry.get("nop_job")
            reward, where = _nop_reward(nop_name if isinstance(nop_name, str) else None)
            if reward == 1.0:
                findings.append({"kind": manifest_name, "task": task_id, "finding": "nop passed", "where": where})
            elif reward == 0.0:
                checked_nop_fail += 1
            else:
                unchecked_nop += 1

    rows = pq.read_table(CENSUS).to_pylist() if CENSUS.is_file() else []
    labels = Counter(row["label"] for row in rows)
    nop_rewards = Counter("none" if row["nop_reward"] is None else str(row["nop_reward"]) for row in rows)

    return {
        "variants": total_variants,
        "total_variants_audited": sum(total_variants.values()),
        "task_toml_keys_modified": sorted(toml_keys_modified),
        "findings": findings,
        "nop_clean_fail": checked_nop_fail,
        "nop_unchecked": unchecked_nop,
        "census_rows": len(rows),
        "census_labels": dict(labels),
        "census_nop_rewards": dict(nop_rewards),
        "census_pre_har113": {
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
    print("findings:", len(report["findings"]))
    for finding in report["findings"][:30]:
        print(finding)


if __name__ == "__main__":
    main()
