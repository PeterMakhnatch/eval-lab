"""Audit every frozen blind input and cached hosted payload, without network calls.

Only opaque identities and generic pattern counts enter the private audit index.
The public receipt contains aggregates, never matched strings or source content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import common


def opaque(run_id: str) -> str:
    return "run-" + hashlib.sha256(("trace-tools/v1:" + run_id).encode()).hexdigest()[:24]


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def record_privacy_abstentions(sample: list[dict[str, str]], flagged: set[str],
                              routes: list[str]) -> int:
    """Record new terminal policy vetoes only; never replace earlier readings."""
    import run_omp

    existing = run_omp.read_jsonl(common.RESULTS)
    added = 0
    for route in routes:
        detector = f"tt7_form:{route}"
        done = {(row["run_id"], row["source"]) for row in existing
                if row.get("tool") == "omp" and row.get("detector") == detector}
        records = []
        for item in sample:
            if opaque(item["run_id"]) not in flagged or (item["run_id"], item["source"]) in done:
                continue
            records.append({
                "schema": "evallab.trace_tool_result/v1", **item, "tool": "omp",
                "detector": detector, "detector_version": "tt7-frozen-20261027/v1",
                "value": {"abstention_reason": "privacy_scan_rejected: uniform canonical policy gate; no transmission",
                          "input_tokens": 0, "output_tokens": 0, "calls": 0, "marginal_usd": 0},
                "flag": None, "evidence_steps": [], "scan": run_omp.SCAN, "at": common.now(),
            })
        added += common.append_records(records)
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Public aggregate receipt, not the original pre-call witness")
    parser.add_argument("--record-privacy-abstentions", action="store_true",
                        help="Append only new no-inference policy vetoes; existing measurements stay unchanged")
    parser.add_argument("--routes", nargs="+", choices=["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"],
                        default=["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"])
    args = parser.parse_args()
    started, clock = common.now(), time.perf_counter()
    sample = common.load_sample()
    rows = []
    for item in sample:
        name = opaque(item["run_id"])
        try:
            run = common.load_run(item)
            markers = common.run_secret_hits(run)
        except Exception as exc:
            raise RuntimeError(f"{name}: privacy audit failed ({type(exc).__name__}); no matched strings printed") from None
        rows.append({"opaque_id": name, "source": item["source"],
                     "text_bytes": len(run["text"].encode()), "steps": len(run["steps"]),
                     "pattern_marker_count": len(markers)})
    payload_path = common.STATE / "tt7/docent/payloads.json"
    payloads = json.loads(payload_path.read_text())
    eligible_names = {opaque(item["run_id"]) for item in sample if item["source"] == "cloud-trace-db"}
    if len(payloads) != 149 or any(payload.get("name") not in eligible_names for payload in payloads):
        raise ValueError("cached hosted payloads differ from the frozen eligible Goodfire collection")
    hosted = [{"opaque_id": payload["name"], "pattern_marker_count": len(common.secret_hits(payload))}
              for payload in payloads]
    flagged = {row["opaque_id"] for row in rows if row["pattern_marker_count"]}
    if len(rows) != 200 or len(flagged) != 4:
        raise ValueError("frozen 200-input/four-policy-flag audit changed; never silently reshape the sample")
    affected = sorted({row["opaque_id"] for row in hosted if row["pattern_marker_count"]}
                      | ({row["opaque_id"] for row in hosted} & flagged))
    finished, elapsed = common.now(), round(time.perf_counter() - clock, 3)
    policy_records = record_privacy_abstentions(sample, flagged, args.routes) if args.record_privacy_abstentions else 0
    private = {"schema": "tt7.privacy_audit_private/v1", "started_at": started,
               "finished_at": finished, "elapsed_s": elapsed,
               "sample_sha256": common.SAMPLE_SHA256, "gate_sha256": common.SECRET_GATE_SHA256,
               "rows": rows, "hosted_rows": hosted, "affected_hosted_opaque_ids": affected,
               "hosted_payload_bytes": payload_path.stat().st_size, "model_calls": 0, "remote_mutations": 0}
    save(common.STATE / "tt7/privacy-audit.json", private)
    public = {"schema": "tt7.privacy_audit_reproduction/v1", "started_at": started,
              "finished_at": finished, "elapsed_s": elapsed,
              "sample_sha256": common.SAMPLE_SHA256, "canonical_gate_sha256": common.SECRET_GATE_SHA256,
              "canonical_gate_source_snapshot": "canonical_secret_gate.py",
              "input_runs": len(rows), "input_text_bytes": sum(row["text_bytes"] for row in rows),
              "input_steps": sum(row["steps"] for row in rows),
              "input_runs_by_source": dict(Counter(row["source"] for row in rows)),
              "policy_flagged_runs": len(flagged), "safe_eligible_runs": len(rows) - len(flagged),
              "policy_flagged_runs_by_source": dict(Counter(row["source"] for row in rows if row["pattern_marker_count"])),
              "policy_flagged_prefix60_runs": sum(row["opaque_id"] in flagged for row in rows[:60]),
              "hosted_payload_runs": len(hosted), "hosted_payload_bytes": payload_path.stat().st_size,
              "hosted_direct_pattern_hit_runs": sum(bool(row["pattern_marker_count"]) for row in hosted),
              "hosted_affected_runs": len(affected), "matched_strings_printed": False,
              "interpretation": "Policy pattern flags are not proof of real credentials; all200 and original measurements remain",
              "terminal_privacy_records_appended": policy_records,
              "model_calls": 0, "remote_mutations": 0, "marginal_cost_usd": 0}
    save(args.out, public)
    print(json.dumps({"input_runs": len(rows), "policy_flagged_runs": len(flagged),
                      "safe_eligible_runs": len(rows) - len(flagged), "hosted_affected_runs": len(affected),
                      "elapsed_s": elapsed, "public_receipt": str(args.out), "model_calls": 0,
                      "terminal_privacy_records_appended": policy_records}))


if __name__ == "__main__":
    main()
