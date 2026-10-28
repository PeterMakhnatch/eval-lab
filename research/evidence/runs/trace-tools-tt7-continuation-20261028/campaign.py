"""Isolate the unchanged TT7 post-merge continuation; never read labels.

The merged experiment and all old receipts are read-only provenance. Only the
125 previously unattempted positions per Claude are eligible for new calls.
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

OUT = Path(__file__).resolve().parent
BASE = OUT.with_name("trace-tools-tt7-20261027")
NAMESPACE = "tt7-continuation-20261028"
PRIVATE = common.STATE / NAMESPACE
ROUTES = ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5")
BASE_COMMIT = "25f63a3c28a3e02474eeb3014715158ba2b20493"
BASE_TREE = "c1cd22eef0da5da24486ead7c72e6f3266a74bb2"
SOURCES = ("common.py", "run_omp.py", "score.py", "finalize.py", "canonical_secret_gate.py")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"immutable campaign file already exists: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(payload)


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def filtered_results() -> bytes:
    sample = {(row["run_id"], row["source"]) for row in common.load_sample()}
    lines = []
    with common.RESULTS.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if ((row.get("run_id"), row.get("source")) in sample
                    and (str(row.get("detector", "")).startswith("tt7_form:")
                         or row.get("tool") == "scout_metr")):
                lines.append(line if line.endswith(b"\n") else line + b"\n")
    return b"".join(lines)


def opaque(run_id: str) -> str:
    return "run-" + digest(("trace-tools/v1:" + run_id).encode())[:24]


def prepare() -> None:
    started = common.now()
    clock = time.perf_counter()
    sample = common.load_sample()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    baseline_path = PRIVATE / "baseline-results.jsonl"
    if not baseline_path.exists():
        write_once(baseline_path, filtered_results())
    baseline = [json.loads(line) for line in baseline_path.read_bytes().splitlines()]
    routes = {}
    for route in ROUTES:
        done = {row["run_id"] for row in baseline if row.get("tool") == "omp"
                and row.get("detector") == f"tt7_form:{route}"}
        terminal = [row["run_id"] for row in sample if row["run_id"] in done]
        pending = [row["run_id"] for row in sample if row["run_id"] not in done]
        if len(terminal) != 75 or len(pending) != 125:
            raise ValueError("original terminal coverage differs from the authorized 75+125 partition")
        routes[route] = {"terminal_run_ids": terminal, "pending_run_ids": pending}
    terminal = {"schema": "tt7.postmerge_baseline_terminal/v1", "sample_sha256": common.SAMPLE_SHA256,
                "baseline_commit": BASE_COMMIT, "baseline_tree": BASE_TREE, "routes": routes}
    write_once(PRIVATE / "baseline-terminal.json", (json.dumps(terminal, indent=2, sort_keys=True) + "\n").encode())
    rows = []
    for item in sample:
        run = common.load_run(item)
        markers = common.run_secret_hits(run)
        rows.append({"opaque_id": opaque(item["run_id"]), "source": item["source"],
                     "text_bytes": len(run["text"].encode()), "steps": len(run["steps"]),
                     "text_sha256": digest(run["text"].encode()), "pattern_marker_count": len(markers)})
    old_audit = json.loads((common.STATE / "tt7/privacy-audit.json").read_text())
    flagged = {row["opaque_id"] for row in rows if row["pattern_marker_count"]}
    old_flagged = {row["opaque_id"] for row in old_audit["rows"] if row["pattern_marker_count"]}
    if len(rows) != 200 or len(flagged) != 4 or flagged != old_flagged:
        raise ValueError("uniform canonical privacy audit changed; no calls admitted")
    if any(opaque(run_id) in flagged for route in ROUTES for run_id in routes[route]["pending_run_ids"]):
        raise ValueError("a newly eligible position has a privacy flag; no calls admitted")
    private_audit = {"schema": "tt7.privacy_audit_private/v1", "sample_sha256": common.SAMPLE_SHA256,
                     "gate_sha256": common.SECRET_GATE_SHA256, "rows": rows,
                     "hosted_rows": old_audit["hosted_rows"], "affected_hosted_opaque_ids": [],
                     "hosted_payload_bytes": old_audit["hosted_payload_bytes"],
                     "model_calls": 0, "remote_mutations": 0}
    write_once(PRIVATE / "privacy-audit.json", (json.dumps(private_audit, indent=2, sort_keys=True) + "\n").encode())
    public_audit = {"schema": "tt7.postmerge_privacy_audit/v1", "started_at": started,
                    "finished_at": common.now(), "elapsed_s": round(time.perf_counter() - clock, 3),
                    "sample_sha256": common.SAMPLE_SHA256, "canonical_gate_sha256": common.SECRET_GATE_SHA256,
                    "input_runs": 200, "input_text_bytes": sum(row["text_bytes"] for row in rows),
                    "input_steps": sum(row["steps"] for row in rows), "policy_flagged_runs": 4,
                    "safe_eligible_runs": 196, "new_eligible_positions_flagged": 0,
                    "hosted_payload_runs_reused": len(old_audit["hosted_rows"]),
                    "hosted_payload_new_uploads": 0, "model_calls": 0, "remote_mutations": 0}
    save(OUT / "privacy-audit.json", public_audit)
    receipt = {"schema": "tt7.postmerge_campaign/v1", "at": started, "namespace": NAMESPACE,
               "sample_sha256": common.SAMPLE_SHA256, "baseline_commit": BASE_COMMIT,
               "baseline_tree": BASE_TREE, "original_source_sha256": {name: digest((BASE / name).read_bytes()) for name in SOURCES},
               "baseline_results_sha256": digest(baseline_path.read_bytes()), "requested_run_count": 200,
               "allowed_new_per_route": 125, "routes": list(ROUTES), "admission_fraction": 0.65,
               "hard_stop_fraction": 0.70, "quota_snapshot_ttl_s": 120, "concurrency": 2,
               "policy": "fixed pending-order continuation only; all earlier terminal cells immutable; no retries/fallback/paid/Luna",
               "labels_read": False, "model_calls": 0}
    save(OUT / "campaign.json", receipt)
    print(json.dumps({"prepared": True, "original_terminal_per_route": 75, "allowed_new_per_route": 125,
                      "privacy_flags": 4, "new_eligible_privacy_flags": 0,
                      "elapsed_s": round(time.perf_counter() - clock, 3), "model_calls": 0}), flush=True)


def eligible_pending() -> dict[str, set[str]]:
    receipt = json.loads((OUT / "campaign.json").read_text())
    baseline_path = PRIVATE / "baseline-results.jsonl"
    if (receipt["sample_sha256"] != common.SAMPLE_SHA256
            or digest(baseline_path.read_bytes()) != receipt["baseline_results_sha256"]):
        raise ValueError("frozen continuation sample/snapshot mismatch")
    for name, expected in receipt["original_source_sha256"].items():
        if digest((BASE / name).read_bytes()) != expected:
            raise ValueError("merged baseline source changed")
    terminal = json.loads((PRIVATE / "baseline-terminal.json").read_text())
    return {route: set(terminal["routes"][route]["pending_run_ids"]) for route in ROUTES}


def close() -> None:
    eligible_pending()
    payload = filtered_results()
    snapshot_path = PRIVATE / "results-snapshot.jsonl"
    write_once(snapshot_path, payload)
    rows = [json.loads(line) for line in payload.splitlines()]
    baseline_lines = (PRIVATE / "baseline-results.jsonl").read_bytes().splitlines()
    current_lines = Counter(payload.splitlines())
    if any(current_lines[line] < count for line, count in Counter(baseline_lines).items()):
        raise ValueError("original result evidence is missing or mutated")
    terminal = json.loads((PRIVATE / "baseline-terminal.json").read_text())
    omp = json.loads((OUT / "omp-receipts.json").read_text())
    routes = {}
    for route in ROUTES:
        new = [row for row in rows if row.get("tool") == "omp"
               and row.get("detector") == f"tt7_form:{route}"
               and row.get("scan") == str(OUT / "omp-receipts.json")]
        seen = [row["run_id"] for row in new]
        pending = terminal["routes"][route]["pending_run_ids"]
        if len(seen) != len(set(seen)) or set(seen) != set(pending[:len(seen)]):
            raise ValueError("new rows are not a unique prefix of the fixed pending order")
        state = omp["routes"][route]
        reason = state.get("reason") or "all previously unattempted positions reached terminal readings"
        routes[route] = {"original_terminal_records": 75, "new_terminal_records": len(new),
                         "unattempted_positions": 125 - len(new), "reason": reason,
                         "status": "complete" if len(new) == 125 else "partial",
                         "unattempted_reason": None if len(new) == 125 else reason,
                         "new_valid_records": sum(row.get("flag") is not None for row in new),
                         "new_abstentions": sum(row.get("flag") is None for row in new)}
    receipt = {"schema": "tt7.postmerge_continuation/v1", "sample_sha256": common.SAMPLE_SHA256,
               "requested_run_count": 200, "namespace": NAMESPACE, "status": "complete" if all(
                   row["status"] == "complete" for row in routes.values()) else "partial",
               "finished_at": common.now(), "routes": routes,
               "results_snapshot": {"path": str(snapshot_path), "sha256": digest(payload)},
               "labels_read": False, "original_evidence_preserved": True}
    save(OUT / "postmerge-continuation-receipts.json", receipt)
    print(json.dumps(receipt), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "close"))
    args = parser.parse_args()
    prepare() if args.action == "prepare" else close()


if __name__ == "__main__":
    main()
