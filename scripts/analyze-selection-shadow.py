#!/usr/bin/env python3
"""Join selection-shadow and test-observation-* artifacts from quality PR runs.

Example: uv run python scripts/analyze-selection-shadow.py --runs 123 456
Failure recall measures observed failed modules, not proven diff causality or
counterfactual execution. Cost shares use complete base and native sessions only.
Repeated runs do not create independent heads or failure modules.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any

POLICIES = ("checkpoint", "checkpoint+environment")


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True, stderr=subprocess.PIPE)


def load_run(repo: str, run_id: int, directory: Path) -> dict[str, Any]:
    metadata = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
    if metadata["event"] != "pull_request" or metadata["name"] != "quality":
        raise ValueError(f"run {run_id}: expected a quality pull_request run")
    if metadata["status"] != "completed":
        raise ValueError(f"run {run_id}: run is not complete")
    gh("run", "download", str(run_id), "--repo", repo, "--dir", str(directory),
       "--pattern", "selection-shadow", "--pattern", "test-observation-*")
    manifests = list(directory.rglob("selection-manifest.json"))
    if len(manifests) != 1:
        raise ValueError(f"run {run_id}: expected 1 manifest, found {len(manifests)}")
    manifest = json.loads(manifests[0].read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError(f"run {run_id}: unsupported manifest schema")
    tested = manifest.get("tested_sha")
    if not isinstance(tested, str) or len(tested) != 40 or any(char not in "0123456789abcdef" for char in tested):
        raise ValueError(f"run {run_id}: missing or invalid tested commit")
    if manifest["status"] != "ok":
        raise ValueError(f"run {run_id}: manifest error: {manifest.get('error')}")
    if manifest["head_sha"] != metadata["head_sha"]:
        raise ValueError(f"run {run_id}: manifest head differs from the run head")
    expected = (str(run_id), str(metadata["run_attempt"]))
    if (manifest.get("run_id"), manifest.get("run_attempt")) != expected:
        raise ValueError(f"run {run_id}: stale manifest run or attempt")
    prs = metadata["pull_requests"]
    if len(prs) != 1:
        raise ValueError(f"run {run_id}: expected 1 associated PR")
    receipts = [json.loads(path.read_text()) for path in directory.rglob("*.modules.json")]
    identities = set()
    for receipt in receipts:
        if receipt.get("schema_version") != 1:
            raise ValueError(f"run {run_id}: unsupported receipt schema")
        if not receipt.get("python_version", "").startswith("3.12."):
            raise ValueError(f"run {run_id}: expected the PR Python 3.12 matrix")
        if (receipt.get("run_id"), receipt.get("run_attempt")) != expected:
            raise ValueError(f"run {run_id}: stale test receipt")
        if receipt["tested_sha"] != manifest["tested_sha"]:
            raise ValueError(f"run {run_id}: test and manifest checkout differ")
        identity = (receipt["shard"], receipt["phase"])
        if identity in identities:
            raise ValueError(f"run {run_id}: duplicate session {identity}")
        identities.add(identity)
    return {"run_id": run_id, "pr": prs[0]["number"], "created_at": metadata["created_at"],
            "manifest": manifest, "receipts": receipts}


def session_issues(row: dict[str, Any]) -> list[str]:
    """Missing evidence must not become a zero-failure observation."""
    expected = {(f"{shard}/4", phase) for shard in range(1, 5)
                for phase in ("full-suite", "harbor-native")}
    observed = {(item["shard"], item["phase"]) for item in row["receipts"]}
    issues = [f"missing session: {identity}" for identity in sorted(expected - observed)]
    issues.extend(f"unexpected session: {identity}" for identity in sorted(observed - expected))
    for receipt in row["receipts"]:
        if receipt["exitstatus"] != 0 and not (receipt["failed_modules"] or receipt["collection_errors"]):
            issues.append(f"unattributed exit {receipt['exitstatus']}: {receipt['shard']} {receipt['phase']}")
    return issues


def cost_share(row: dict[str, Any], selected: set[str]) -> float | None:
    receipts = row["receipts"]
    if session_issues(row):
        return None
    # Failures and interrupted sessions do not provide a full-suite price.
    if any(item["exitstatus"] != 0 or item["collection_errors"] for item in receipts):
        return None
    total = chosen = 0.0
    for receipt in receipts:
        for module, observation in receipt["modules"].items():
            seconds = observation["seconds"]
            if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError(f"invalid module duration: {module}")
            total += seconds
            if module in selected:
                chosen += seconds
    return chosen / total if total > 0 else None


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("no run observations")
    latest: dict[int, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: (item["created_at"], item["run_id"])):
        latest[row["pr"]] = row
    result: dict[str, Any] = {
        "runs": len(rows), "prs": len(latest), "policies": {},
        "limits": [
            "Recall uses observed test and collection failures, not a diff-causality audit.",
            "Module inclusion is not selected-execution proof. Costs price whole modules.",
            "Recall deduplicates head/base/selector/module; fallback and cost use each PR's latest run.",
            "Missing or failed sessions have no cost share. Missing evidence is not zero failure.",
            "Costs sum setup+call+teardown worker-seconds, not wall time. Lint and smoke are excluded.",
        ],
        "unobserved_runs": [
            {"run_id": row["run_id"], "pr": row["pr"], "issues": session_issues(row)}
            for row in rows if session_issues(row)
        ],
    }
    for name in POLICIES:
        failures: dict[tuple[str, str, str, str], bool] = {}
        missed_prs = set()
        missing = []
        collection_errors = []
        for row in rows:
            manifest = row["manifest"]
            policy = manifest["policies"][name]
            selected = set(policy["selected_modules"])
            modules = {module for receipt in row["receipts"] for module in receipt["failed_modules"]}
            for receipt in row["receipts"]:
                for node in receipt["collection_errors"]:
                    module = node.partition("::")[0]
                    if module in manifest["inventory"]:
                        modules.add(module)
                    else:
                        collection_errors.append({"run_id": row["run_id"], "nodeid": node})
            for module in modules:
                pair = (manifest["head_sha"], manifest["base_sha"], manifest["selector_sha256"], module)
                included = policy["fallback"] or module in selected
                if pair in failures and failures[pair] != included:
                    raise ValueError(f"selection differs across repeated head/module: {pair}")
                failures[pair] = included
                if not included:
                    missed_prs.add(row["pr"])
                    missing.append({"pr": row["pr"], "head_sha": pair[0], "module": module})
        costs = []
        incomplete = []
        incomplete_prs = {item["pr"] for item in result["unobserved_runs"]}
        fallbacks = 0
        for row in latest.values():
            policy = row["manifest"]["policies"][name]
            fallbacks += int(policy["fallback"])
            cost = cost_share(row, set(policy["selected_modules"]))
            if cost is None:
                incomplete.append(row["run_id"])
            else:
                costs.append(cost)
        costs.sort()
        result["policies"][name] = {
            "failed_head_module_pairs": len(failures),
            "selected_failed_pairs": sum(failures.values()),
            "module_recall": sum(failures.values()) / len(failures) if failures else None,
            "prs_with_omitted_failure": sorted(missed_prs),
            "escape_proxy": len(missed_prs) / len(latest) if not incomplete_prs else None,
            "escape_proxy_lower_bound": len(missed_prs) / len(latest),
            "escape_proxy_upper_bound": len(missed_prs | incomplete_prs) / len(latest),
            "escape_proxy_prs": len(latest),
            "collection_errors_without_module_attribution": collection_errors,
            "omitted_failures": sorted({(item["pr"], item["head_sha"], item["module"])
                                        for item in missing}),
            "fallback_prs": fallbacks,
            "fallback_rate": fallbacks / len(latest),
            "fallback_prs_denominator": len(latest),
            "selected_test_seconds_share": {
                "n": len(costs), "median": statistics.median(costs) if costs else None,
                "mean": statistics.mean(costs) if costs else None,
                "p90": costs[math.ceil(0.9 * len(costs)) - 1] if costs else None,
            },
            "unpriced_runs": incomplete,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PeterMakhnatch/eval-lab")
    parser.add_argument("--runs", nargs="+", type=int, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="evallab-shadow-analysis-") as temporary:
        rows = [load_run(args.repo, run_id, Path(temporary) / str(run_id))
                for run_id in dict.fromkeys(args.runs)]
        result = analyze(rows)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
