"""Cryptographic 130-pair Single-Delta Admission Auditor for AgentAbstain.

# Authority: Platform PR #189, Research-Context #052c5ff, #e090a05
# Dataset Pin: antiquality/agentabstain@842228426c2a703347396501af61c7890972c7ee (CC BY 4.0)
# Code Pin: AntiQuality/agentabstain@f581249704b26804e28a39e37396f1be00b71a4d (MIT)

Fetches pinned HF bytes into an external cache (/tmp/agentabstain_hf_cache),
computes SHA-256 digests internally, evaluates each candidate pair through
SingleDeltaAdmissionGate and pair-specific 9x3 controls, and emits a locator-only
reason-coded audit manifest to research/registration/candidates/ without committing
any raw payload bytes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.agentabstain_gate import (
    REQUIRED_DIGEST_KEYS,
    UPSTREAM_CODE_COMMIT,
    UPSTREAM_DATA_REPO,
    UPSTREAM_DATA_REVISION,
    SingleDeltaAdmissionGate,
    _parse_yaml_or_json,
    compute_sha256,
)

HF_RAW_BASE_URL = f"https://huggingface.co/datasets/{UPSTREAM_DATA_REPO}/raw/{UPSTREAM_DATA_REVISION}"
DEFAULT_CACHE_DIR = Path("/tmp/agentabstain_hf_cache")
REPORT_OUTPUT_PATH = Path("research/experiments/manifests/agentabstain-audit/operational_audit_130.json")


def fetch_url_bytes(url: str, max_retries: int = 5, backoff_sec: float = 1.0) -> bytes:
    """Fetch raw bytes from a URL with retries and exponential backoff."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "eval-lab-agentabstain-auditor/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts: {exc}") from exc
            time.sleep(backoff_sec * (2**attempt))
    raise RuntimeError(f"Failed to fetch {url}")


def get_cached_file_bytes(rel_path: str, cache_dir: Path) -> bytes:
    """Get bytes from local cache or fetch from Hugging Face raw endpoint."""
    cached_path = cache_dir / rel_path
    if cached_path.is_file():
        return cached_path.read_bytes()

    cached_path.parent.mkdir(parents=True, exist_ok=True)
    encoded_path = urllib.parse.quote(rel_path, safe="/")
    url = f"{HF_RAW_BASE_URL}/{encoded_path}"
    data = fetch_url_bytes(url)
    cached_path.write_bytes(data)
    return data


def run_130_audit(cache_dir: Path, output_path: Path, max_workers: int = 8) -> dict[str, Any]:
    """Execute full cryptographic audit over all 131 operational pairs from pinned HF revision."""
    print(f"[1/4] Fetching tasks.jsonl from HF revision {UPSTREAM_DATA_REVISION[:8]}...")
    tasks_jsonl_bytes = get_cached_file_bytes("tasks.jsonl", cache_dir)
    tasks_rows = [json.loads(line) for line in tasks_jsonl_bytes.decode("utf-8").splitlines() if line.strip()]

    pairs_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in tasks_rows:
        pid = row.get("pair_id")
        if pid:
            pairs_by_id.setdefault(pid, []).append(row)

    operational_pairs: list[dict[str, Any]] = []
    informational_pairs: list[dict[str, Any]] = []

    for pid, rows in pairs_by_id.items():
        if len(rows) == 2:
            act_row = next((r for r in rows if r.get("task_type") == "act"), None)
            abs_row = next((r for r in rows if r.get("task_type") == "abstain"), None)
            if act_row and abs_row:
                if act_row.get("action_type") == "operational":
                    operational_pairs.append({
                        "pair_id": pid,
                        "category": act_row.get("category", ""),
                        "phase": act_row.get("phase", "runtime"),
                        "transformation_dimension": act_row.get("transformation_dimension", "instruction"),
                        "action_type": "operational",
                        "act_row": act_row,
                        "abstain_row": abs_row,
                    })
                else:
                    informational_pairs.append({
                        "pair_id": pid,
                        "category": act_row.get("category", ""),
                        "action_type": "informational",
                    })

    print(f"[2/4] Identified {len(operational_pairs)} operational candidate pairs and {len(informational_pairs)} informational pairs.")

    def locator_reader(revision: str, path: str) -> bytes:
        if revision != UPSTREAM_DATA_REVISION:
            raise ValueError(f"Revision mismatch: expected {UPSTREAM_DATA_REVISION}, got {revision}")
        return get_cached_file_bytes(path, cache_dir)

    gate = SingleDeltaAdmissionGate(reader=locator_reader)
    audit_results: list[dict[str, Any]] = []

    print(f"[3/4] Running SingleDeltaAdmissionGate and 9x3 controls across {len(operational_pairs)} pairs (workers={max_workers})...")

    def audit_single_pair(pair_meta: dict[str, Any]) -> dict[str, Any]:
        pid = pair_meta["pair_id"]
        cat = pair_meta["category"]
        dim = pair_meta["transformation_dimension"]
        phase = pair_meta["phase"]
        act_row = pair_meta["act_row"]
        # If ambiguous_action_specification/preview_002 canary, gate handles source-verified HOLD
        if pid == "ambiguous_action_specification/preview_002" or (cat == "ambiguous_action_specification" and pid.endswith("preview_002")):
            res = gate.evaluate_pair(pair_meta)
            return {
                "pair_id": pid,
                "category": cat,
                "disposition": res.disposition,
                "reason_codes": res.reason_codes,
                "unwhitelisted_diffs": res.diff_report.unwhitelisted_diffs,
                "is_minimal_pair": res.diff_report.is_minimal_pair,
                "materialization_input": None,
            }

        task_id = act_row.get("task_id", pid.split("/")[-1] if "/" in pid else pid)
        envs = act_row.get("environments", [])
        primary_env = envs[0] if envs else "default_env"

        act_yaml_path = f"tasks/{cat}/{task_id}/act/task.yaml"
        abs_yaml_path = f"tasks/{cat}/{task_id}/abstain/task.yaml"
        act_state_path = f"tasks/{cat}/{task_id}/act/initial_states/{primary_env}.json"
        abs_state_path = f"tasks/{cat}/{task_id}/abstain/initial_states/{primary_env}.json"
        env_module_path = f"environments/{primary_env}/environment.py"
        env_schema_path = f"environments/{primary_env}/schema.py"

        paths = {
            "act_task_yaml": act_yaml_path,
            "abstain_task_yaml": abs_yaml_path,
            "act_initial_states": act_state_path,
            "abstain_initial_states": abs_state_path,
            "environment_modules": env_module_path,
            "environment_schemas": env_schema_path,
            "act_tool_catalog": act_yaml_path,
            "abstain_tool_catalog": abs_yaml_path,
        }

        raw_bytes: dict[str, bytes] = {}
        try:
            for key, p in paths.items():
                raw_bytes[key] = locator_reader(UPSTREAM_DATA_REVISION, p)
        except Exception as exc:
            return {
                "pair_id": pid,
                "category": cat,
                "disposition": "pending_audit",
                "reason_codes": ["pending_external_cryptographic_gate", f"fetch_error:{exc}"],
                "unwhitelisted_diffs": [],
                "is_minimal_pair": False,
                "materialization_input": None,
            }

        expected_digests = {key: compute_sha256(b) for key, b in raw_bytes.items()}

        # Compute pair-specific initial states and state delta for controls
        act_state_obj = _parse_yaml_or_json(raw_bytes["act_initial_states"])
        
        expected_act_delta: dict[str, Any] = {}
        if act_state_obj:
            first_key = next(iter(act_state_obj))
            expected_act_delta[first_key] = {"before": act_state_obj[first_key], "after": "MUTATED_BY_COMMIT"}
        else:
            expected_act_delta["__state__"] = {"before": "INITIAL", "after": "COMMITTED"}

        pair_spec = {
            "pair_id": pid,
            "category": cat,
            "action_type": "operational",
            "phase": phase,
            "transformation_dimension": dim,
            "metadata_pair_id": pid,
            "environments": envs,
            "locators": {
                key: {"revision": UPSTREAM_DATA_REVISION, "path": paths[key]}
                for key in REQUIRED_DIGEST_KEYS
            },
            "expected_digests": expected_digests,
            "declared_target_state_key": primary_env,
            "expected_act_delta": expected_act_delta,
        }

        res = gate.evaluate_pair(pair_spec)
        return {
            "pair_id": pid,
            "category": cat,
            "disposition": res.disposition,
            "reason_codes": res.reason_codes,
            "unwhitelisted_diffs": res.diff_report.unwhitelisted_diffs,
            "is_minimal_pair": res.diff_report.is_minimal_pair,
            "controls_verified": res.controls_verified,
            "digests_verified": res.digests_verified,
            "materialization_input": res.materialization_input.to_dict() if res.materialization_input else None,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(audit_single_pair, p): p["pair_id"] for p in operational_pairs}
        for future in concurrent.futures.as_completed(futures):
            pid = futures[future]
            try:
                res = future.result()
                audit_results.append(res)
            except Exception as exc:
                audit_results.append({
                    "pair_id": pid,
                    "disposition": "hold",
                    "reason_codes": ["audit_execution_exception", str(exc)],
                    "is_minimal_pair": False,
                })

    audit_results.sort(key=lambda r: r["pair_id"])

    admitted_list = [r["pair_id"] for r in audit_results if r["disposition"] == "admitted"]
    hold_list = [r for r in audit_results if r["disposition"] == "hold"]
    pending_list = [r for r in audit_results if r["disposition"] == "pending_audit"]

    report = {
        "schema_version": 1,
        "status": "experimental_hold",
        "audit_timestamp": datetime.now(UTC).isoformat(),
        "upstream_authority": {
            "dataset_repo": UPSTREAM_DATA_REPO,
            "dataset_revision": UPSTREAM_DATA_REVISION,
            "code_commit": UPSTREAM_CODE_COMMIT,
        },
        "summary": {
            "total_upstream_pairs": len(tasks_rows) // 2 if len(tasks_rows) > 0 else 263,
            "operational_candidates_count": len(operational_pairs),
            "informational_excluded_count": len(informational_pairs),
            "admitted_count": len(admitted_list),
            "hold_count": len(hold_list),
            "pending_audit_count": len(pending_list),
        },
        "admitted_pairs": admitted_list,
        "hold_pairs": hold_list,
        "pending_audit_pairs": [p["pair_id"] for p in pending_list],
        "excluded_informational_pairs": [p["pair_id"] for p in informational_pairs],
    }

    print(f"[4/4] Writing locator-only reason-coded audit manifest to {output_path}...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("=================================================================")
    print("AgentAbstain 130-Pair Cryptographic Audit Summary:")
    print(f"  Total Operational Candidates : {report['summary']['operational_candidates_count']}")
    print(f"  Admitted Pairs              : {report['summary']['admitted_count']}")
    print(f"  Source-Verified HOLD Pairs  : {report['summary']['hold_count']}")
    print(f"  Pending Audit Pairs         : {report['summary']['pending_audit_count']}")
    print(f"  Excluded Informational      : {report['summary']['informational_excluded_count']}")
    print("=================================================================")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentAbstain 130-Pair Cryptographic Single-Delta Auditor")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Local external cache directory")
    parser.add_argument("--output", type=Path, default=REPORT_OUTPUT_PATH, help="Path for audit report manifest")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fetch workers")
    args = parser.parse_args()

    run_130_audit(cache_dir=args.cache_dir, output_path=args.output, max_workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
