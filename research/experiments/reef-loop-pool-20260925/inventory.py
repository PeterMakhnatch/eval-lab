#!/usr/bin/env python3
"""Regenerate inventory.json for the reef-loop-pool proposal.

Reads only committed repository state (library/registry/*.json, task.toml,
Dockerfiles) plus the historical-outcome export produced read-only from the
shared catalog (see README). Deterministic; no execution, no host state.

Usage:
  uv run python research/experiments/reef-loop-pool-20260925/inventory.py \
      --history /tmp/loopool-history.json
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

REGISTRY_TASKS = [
    ("event-summary", "library/tasks/event-summary"),
    ("query-optimize", "library/tasks/query-optimize"),
    ("syn-funcdag-easy", "library/tasks/experimental/syn-funcdag-easy"),
    ("tau3-retail-1", "library/tasks/tau3-retail-1"),
    ("terminal-bench-html-js-filter", "library/tasks/terminal-bench-html-js-filter"),
    ("transaction-reconciliation", "library/tasks/transaction-reconciliation"),
    ("travel-lisbon-002", "library/tasks/experimental/deepplanning-v1/travel-lisbon-002"),
]

CATALOG_TASK_NAMES = {
    "event-summary": "local-lab/event-summary",
    "query-optimize": "terminal-bench/query-optimize",
    "syn-funcdag-easy": "evallab/syn-funcdag-easy",
    "tau3-retail-1": "sierra-research/tau3-bench__tau3-retail-1",
    "terminal-bench-html-js-filter": "terminal-bench/html-js-filter",
    "transaction-reconciliation": "petermakhnatch/transaction-reconciliation",
    "travel-lisbon-002": "deepplanning-v1/travel-lisbon-002",
}

EXP05_ROOT = Path.home() / "Developer/research-context/reef/experiments/exp05/tasks"
EXP05_TASKS = [
    ("exp05-search-fix-median", EXP05_ROOT / "search/fix-median"),
    ("exp05-search-sales-total", EXP05_ROOT / "search/sales-total"),
    ("exp05-search-error-count", EXP05_ROOT / "search/error-count"),
    ("exp05-search-top-words", EXP05_ROOT / "search/top-words"),
    ("exp05-holdout-fix-slugify", EXP05_ROOT / "holdout/fix-slugify"),
    ("exp05-holdout-inventory-value", EXP05_ROOT / "holdout/inventory-value"),
    ("exp05-holdout-status-codes", EXP05_ROOT / "holdout/status-codes"),
    ("exp05-holdout-top-tags", EXP05_ROOT / "holdout/top-tags"),
]

BENCHMARK_PACKAGES = [
    "action-memory-v1",
    "mcp-funcdag-v1",
    "mcp-recovery-v1",
]


def task_facts(path: Path) -> dict[str, object]:
    toml = tomllib.loads((path / "task.toml").read_text())
    environment = toml.get("environment", {})
    agent = toml.get("agent", {})
    verifier = toml.get("verifier", {})
    metadata = toml.get("metadata", {})
    dockerfile = path / "environment/Dockerfile"
    from_lines = (
        [line for line in dockerfile.read_text().splitlines() if line.startswith("FROM")]
        if dockerfile.exists()
        else []
    )
    emulated = any("--platform=linux/amd64" in line for line in from_lines)
    mcp = environment.get("mcp_servers") or []
    return {
        "task_name": toml.get("task", {}).get("name"),
        "declared_resources": {
            "agent_timeout_s": agent.get("timeout_sec"),
            "verifier_timeout_s": verifier.get("timeout_sec"),
            "memory_mb": environment.get("memory_mb"),
            "cpus": environment.get("cpus"),
            "gpus": environment.get("gpus", 0),
        },
        "image_from": from_lines,
        "image_platform": "local-emulated" if emulated else "arm64-native-or-multiarch",
        "mcp_servers": [server.get("name") for server in mcp if isinstance(server, dict)],
        "terminus2_fit": (
            "needs-mcp-tool-services"
            if mcp
            else "plain-terminal"
        ),
        "category": metadata.get("category"),
        "tags": metadata.get("tags"),
        "difficulty": metadata.get("difficulty"),
    }


def registry_entry(task_id: str) -> dict[str, object]:
    path = REPO / "library/registry" / f"{task_id}.json"
    record = json.loads(path.read_text())
    digests = record.get("digests", {})
    controls = record.get("control_evidence") or {}
    current_digest = digests.get("package")
    evidence = {}
    for agent, entry in controls.items():
        evidence[agent] = {
            "reward": entry.get("reward"),
            "observed_at": entry.get("observed_at"),
            "at_current_package_digest": (
                (entry.get("task_digests") or {}).get("package") == current_digest
            ),
        }
    return {
        "registry_state": record.get("state"),
        "allowed_uses": record.get("allowed_uses"),
        "package_digest": current_digest,
        "registry_limits": record.get("limits"),
        "control_evidence": evidence,
        "source_uri": record.get("source_uri"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    args = parser.parse_args()
    history: dict[str, list[dict[str, object]]] = json.loads(args.history.read_text())

    tasks = []
    for task_id, rel in REGISTRY_TASKS:
        path = REPO / rel
        entry: dict[str, object] = {"task_id": task_id, "path": rel}
        entry.update(registry_entry(task_id))
        entry.update(task_facts(path))
        entry["historical_outcomes"] = history.get(CATALOG_TASK_NAMES[task_id], [])
        tasks.append(entry)

    for task_id, path in EXP05_TASKS:
        entry = {
            "task_id": task_id,
            "path": str(path).replace(str(Path.home()), "~"),
            "registry_state": "unregistered-external",
            "allowed_uses": None,
            "package_digest": None,
            "package_digest_note": (
                "external Reef experiment tasks; Eval Lab pins the package digest per "
                "prepared spec (docs/execution-tiers.md Terminus 2 section)"
            ),
            "registry_limits": None,
            "control_evidence": {},
            "historical_outcomes": [],
        }
        entry.update(task_facts(path))
        entry["historical_outcomes_note"] = (
            "exp05 round 1 (qwen3-coder:30b, Terminus 2 via Reef proxy): search outcomes "
            "fix-median 1/1, sales-total 1/1, error-count 0/1, top-words 0/1; see "
            "research-context/reef/experiments/work/05/summary.json"
        )
        tasks.append(entry)

    packages = []
    for name in BENCHMARK_PACKAGES:
        readme = (REPO / "library/benchmarks" / name / "README.md").read_text()
        packages.append(
            {
                "package": f"library/benchmarks/{name}",
                "materialized_on_demand": "derived/harbor-tasks/" in readme or "materialize" in readme,
                "terminus2_fit": "needs-mcp-tool-services",
                "note": "MCP sidecar benchmark family (FastMCP streamable-HTTP); excluded from the first Terminus 2 pool",
            }
        )

    report = {
        "schema_version": 1,
        "generated": "2026-09-25",
        "history_source": (
            "shared Eval Lab catalog (read-only SELECT on localhost:54329, "
            "trials table, task names mapped per CATALOG_TASK_NAMES); exported "
            "2026-09-25 by the LoopPool worker"
        ),
        "registry_tasks": tasks,
        "benchmark_packages": packages,
        "missing_data": [
            "exp05 tasks have no registry record or package digest until a prepared spec pins one.",
            "Per-task pass rates for any Terminus 2 model on the exp05 pool exist only as exp05 round 1 single episodes (qwen3-coder:30b) and HAR-71's 0/4 (qwen2.5:7b on two lab tasks).",
            "Benchmark package task counts are materialization-time facts, not committed counts.",
        ],
    }
    target = Path(__file__).resolve().parent / "inventory.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
