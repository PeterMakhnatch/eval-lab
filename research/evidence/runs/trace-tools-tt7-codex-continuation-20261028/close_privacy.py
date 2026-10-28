"""Append only previously unattempted, already-audited Luna privacy vetoes; no inference or labels."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent
STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
PRIVATE = STATE / "tt7-codex-continuation-20261028"
PHASE = "tt7-codex-continuation-20261028-privacy-closure"
ROUTE = "openai-codex/gpt-6-luna"
DETECTOR = "tt7_form:" + ROUTE
SCAN = "research/evidence/runs/trace-tools-tt7-codex-continuation-20261028/privacy-closure"
DRIVER_SHA256 = "87c0d0bb6296b28d5a38804fdfc6211e5ad1a61ff8e926f43bf4b8aa811eca28"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def opaque(run_id: str) -> str:
    return "run-" + digest(("trace-tools/v1:" + run_id).encode())[:24]


def privacy_records(sample: list[dict[str, Any]], flagged: set[str], terminal: set[str], at: str) -> list[dict[str, Any]]:
    """File order and uniform prior privacy markers only; no gold labels or model calls."""
    return [{
        "schema": "evallab.trace_tool_result/v1", "run_id": item["run_id"], "source": item["source"],
        "tool": "omp", "detector": DETECTOR, "detector_version": "tt7-frozen-20261027/v1",
        "value": {
            "abstention_reason": "privacy_scan_rejected: canonical secret-pattern gate; no actor content transmitted",
            "abstention_class": "privacy_guard", "attempt_namespace": PHASE,
            "billing_basis": "zero_no_model_transmission_privacy_guard",
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "latency_s": None, "chunked": False, "chunks": 0, "served_model": None,
            "list_price_usd": 0, "list_price_basis": "no model transmission", "marginal_usd": 0,
            "calls": 0, "successful_calls": 0, "read_elapsed_s": 0,
        },
        "flag": None, "evidence_steps": [], "scan": SCAN, "at": at,
    } for item in sample if opaque(item["run_id"]) in flagged and item["run_id"] not in terminal]


def write_once(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)


def main() -> None:
    receipt_path = OUT / "privacy-closure.json"
    if receipt_path.exists():
        raise ValueError("privacy closure is sealed; never rewrite prior evidence")
    driver_path = OUT / "continue_codex.py"
    payload = driver_path.read_bytes()
    if digest(payload) != DRIVER_SHA256:
        raise ValueError("executed driver bytes changed")
    spec = importlib.util.spec_from_file_location("tt7_privacy_closure_frozen_driver", driver_path)
    assert spec is not None and spec.loader is not None
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    bound = driver.bind()
    attempt = json.loads((OUT / "attempt.json").read_bytes())
    prepared = json.loads((PRIVATE / "prepare.json").read_bytes())
    if attempt["status"] != "partial" or attempt["labels_read"] is not False:
        raise ValueError("original generation attempt is not sealed")
    audit_bytes = (PRIVATE / "privacy-audit.json").read_bytes()
    audit = json.loads(audit_bytes)
    if (digest(audit_bytes) != prepared["privacy_audit_sha256"]
            or audit["sample_sha256"] != bound.common.SAMPLE_SHA256
            or audit["gate_sha256"] != bound.common.SECRET_GATE_SHA256
            or len(audit["rows"]) != 200 or len(set(audit["flagged"])) != 4):
        raise ValueError("uniform immutable privacy audit pins differ")
    sample = bound.common.load_sample()
    before = bound.common.RESULTS.read_bytes()
    prior = [json.loads(line) for line in before.splitlines() if line.strip()]
    sample_ids = {item["run_id"] for item in sample}
    terminal = {row["run_id"] for row in prior if row.get("tool") == "omp"
                and row.get("detector") == DETECTOR and row.get("run_id") in sample_ids}
    if len(terminal) != 15:
        raise ValueError("Luna terminal inventory changed; do not rewrite or duplicate a cell")
    rows = privacy_records(sample, set(audit["flagged"]), terminal, datetime.now(UTC).isoformat())
    if len(rows) != 4:
        raise ValueError("expected exactly four unattempted known privacy vetoes")
    record_bytes = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows)
    write_once(PRIVATE / "privacy-closure-records.jsonl", record_bytes)
    added = bound.common.append_records(rows)
    if added != 4:
        raise ValueError("all four unique privacy rows were not appended")
    after = bound.common.RESULTS.read_bytes()
    if Counter(before.splitlines()) - Counter(after.splitlines()):
        raise ValueError("pre-existing canonical rows were removed or rewritten")
    done = terminal | {row["run_id"] for row in rows}
    private_terminal = {
        "terminal_run_ids": [item["run_id"] for item in sample if item["run_id"] in done],
        "truly_unattempted_run_ids": [item["run_id"] for item in sample if item["run_id"] not in done],
    }
    terminal_bytes = (json.dumps(private_terminal, sort_keys=True, indent=2) + "\n").encode()
    write_once(PRIVATE / "privacy-closure-terminal.json", terminal_bytes)
    closure = {
        "schema": "tt7.offline_privacy_closure/v1", "at": datetime.now(UTC).isoformat(), "namespace": PHASE,
        "sample_sha256": bound.common.SAMPLE_SHA256, "canonical_gate_sha256": bound.common.SECRET_GATE_SHA256,
        "uniform_audit_sha256": digest(audit_bytes), "source_sha256": digest(Path(__file__).read_bytes()),
        "records_sha256": digest(record_bytes), "terminal_inventory_sha256": digest(terminal_bytes),
        "new_canonical_privacy_abstentions": added, "privacy_abstentions_by_source": dict(Counter(row["source"] for row in rows)),
        "fixed_sample_accounting": {"denominator": 200, "historical_luna_terminal": 2,
            "new_schema_valid_as_measured": 12, "new_protocol_abstentions": 1,
            "known_zero_call_privacy_abstentions": 4, "truly_unattempted": len(sample_ids - done), "sum": 200},
        "original_sealed_unattempted_included_privacy_vetoes": 185,
        "categories_by_source": {
            source: {"sample": sum(item["source"] == source for item in sample),
                     "privacy_abstentions": sum(row["source"] == source for row in rows),
                     "truly_unattempted": sum(item["source"] == source and item["run_id"] not in done for item in sample)}
            for source in sorted({item["source"] for item in sample})
        },
        "metrics_unchanged_by_construction": "All four cells were already non-decisions; explicit privacy abstentions remain non-decisions. No verdict, threshold, primary/secondary scoring rule or denominator changes.",
        "original_attempt_records_scores_and_measured_flagged_rows_rewritten": False,
        "original_two_and_anomalous_financial_unknowns_unchanged": True,
        "labels_read": False, "model_calls": 0, "quota_queries": 0, "input_tokens": 0, "output_tokens": 0,
        "marginal_usd": 0, "list_price_usd": 0, "future_inference_authorized": False,
    }
    bound.common.log_spend({"tool": "omp", "route": ROUTE, "purpose": "Offline categorical privacy closure; no model transmission",
        "record_type": "zero_call_privacy_closure", "batch_id": PHASE, "calls": 0, "runs": 0,
        "input_tokens": 0, "output_tokens": 0, "usd": 0, "list_price_usd": 0, "privacy_abstentions": 4,
        "basis": "Uniform pinned prior privacy audit; four previously unattempted cells only. Historical2 and anomalous1 billing remain UNKNOWN; not an all-$0 study claim.", "scan": SCAN})
    write_once(receipt_path, (json.dumps(closure, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"privacy_abstentions_added": added, "truly_unattempted": len(sample_ids - done),
                      "labels_read": False, "model_calls": 0, "quota_queries": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
