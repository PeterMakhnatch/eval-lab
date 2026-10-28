"""Freeze aggregate arm coverage before the separate, local-only label join.

This program never opens labels, passage annotations, or model evidence. It
distinguishes the completed prefix batch from the blocked full-200 continuation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from common import SAMPLE_SHA256, STATE, load_sample, now

OUT = Path(__file__).resolve().parent
TERMINAL = {"complete", "partial", "deferred"}


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def roles_for(model: str) -> list[str]:
    lowered = model.lower()
    if any(part in lowered for part in ("mini", "flash", "glm")):
        return ["cheap"]
    return ["cheap", "frontier"] if "sonnet" in lowered else ["frontier"]


def main() -> None:
    load_sample()  # Exact frozen bytes/identities, no labels.
    docent = load(OUT / "docent-receipts.json")
    omp = load(OUT / "omp-receipts.json")
    prefix = load(OUT / "omp-prefix60-receipts.json")
    continuation = load(OUT / "continuation-200-receipts.json")
    luna_financial = load(OUT / "luna-financial-basis.json")
    if (continuation.get("schema") != "tt7.subscription_continuation_admission/v1"
            or continuation.get("sample_sha256") != SAMPLE_SHA256
            or continuation.get("requested_run_count") != 200
            or continuation.get("continuation_start_zero_based") != 60
            or continuation.get("continuation_end_exclusive") != 200
            or continuation.get("status") not in TERMINAL
            or "finished_at" not in continuation
            or datetime.fromisoformat(continuation["finished_at"]) < datetime.fromisoformat(omp["finished_at"])):
        raise ValueError("full-200 continuation has not reached a terminal receipt")
    schedule = load(STATE / "tt7/omp/schedule.json")
    if schedule.get("target_runs") != 60 or schedule.get("sample_sha256") != SAMPLE_SHA256:
        raise ValueError("initial scheduled batch is not the authorized fixed prefix-60")
    if "resumed_at" not in schedule or datetime.fromisoformat(prefix["finished_at"]) < datetime.fromisoformat(schedule["resumed_at"]):
        raise ValueError("initial prefix batch has not reached its terminal receipt")
    arms = []
    for arm in docent["arms"]:
        model = str(arm.get("model") or "glm-unavailable")
        detector = arm.get("detector") or f"tt7_form:docent/{model}-deferred"
        if arm["status"] not in TERMINAL:
            raise ValueError(f"Docent arm not terminal: {model}")
        arms.append({"tool": "docent", "detector": detector, "model": model,
                     "status": "partial" if arm["status"] == "complete" else arm["status"],
                     "reason": (arm.get("reason") or "Goodfire150 reading complete") + "; CheatBench50 storage prohibited",
                     "scan": arm.get("scan"), "roles": roles_for(model), "subscription": False,
                     "marginal_cost_usd_per_run": 0.0, "scope": "full-200",
                     "requested_scope": "goodfire-150", "target_run_count": 200, "expected_runs": 200,
                     "expected_by_source": {"cloud-trace-db": 150, "cheatbench-actions-v1": 50},
                     "excluded_source_reason": "CheatBench storage is prohibited; its 50 full-cohort cells explicitly abstain",
                     "list_price_usd_per_million_input": None, "list_price_usd_per_million_output": None})
    for model, arm in omp["routes"].items():
        if arm["status"] not in TERMINAL:
            raise ValueError(f"subscription arm not terminal: {model}")
        claude = model.startswith("anthropic/")
        if claude:
            batch = prefix["routes"][model]
            phase = continuation["routes"][model]
            if (batch.get("scope") != "prefix-60" or batch.get("target_runs") != 60
                    or batch.get("recorded_runs") != 60):
                raise ValueError("Claude's preserved initial batch does not cover the fixed prefix-60")
            if (phase["status"] not in TERMINAL
                    or 60 + phase["new_terminal_records"] + phase["unattempted_positions"] != 200):
                raise ValueError("Claude's continuation does not account for all fixed 200 positions")
            reason = f"{phase['reason']}; preserved prefix-60 plus {phase['new_terminal_records']} continuation terminal cells"
        else:
            reason = arm.get("reason")
        rates = omp["rates_per_million_input_output"][model]
        arms.append({"tool": "omp", "detector": f"tt7_form:{model}", "model": model,
                     "status": arm["status"], "reason": reason, "scan": str(OUT / "omp-receipts.json"),
                     "roles": roles_for(model), "subscription": True,
                     "marginal_cost_verified": claude,
                     "auth_class": None if claude else luna_financial["auth_class"],
                     "transport_api": None if claude else luna_financial["attested_transport_api"],
                     "scope": "full-200", "target_run_count": 200, "expected_runs": 200, "total_sample": 200,
                     "observed_batch_scope": "prefix-60+fixed-continuation" if claude else "initial-stopped-coverage",
                     "unattempted_reason": continuation["routes"][model]["unattempted_reason"] if claude else "missing_record",
                     "threshold": 0.70 if claude else 0.90,
                     "effective_admission_threshold": continuation["admission_fraction"] if claude else 0.90,
                     "expected_by_source": {"cloud-trace-db": 150, "cheatbench-actions-v1": 50},
                     "list_price_usd_per_million_input": rates[0], "list_price_usd_per_million_output": rates[1]})
    arms.extend([
        {"tool": "omp", "detector": "tt7_form:xai-oauth/grok-4.3", "model": "xai-oauth/grok-4.3",
         "status": "deferred", "reason": "weekly quota exhausted until 2026-10-30 14:10 ET; no calls",
         "scan": None, "roles": ["frontier"], "subscription": True, "expected_runs": 200},
        {"tool": "laminar", "detector": "tt7_form:laminar/flow-1", "model": "laminar/flow-1",
         "status": "deferred", "reason": "paid Signals approved but not funded; additionally CheatBench storage is prohibited",
         "scan": None, "roles": ["cheap"], "subscription": False, "expected_runs": 200},
    ])
    manifest = {"schema": "tt7.readings_finalized/v1", "finalized": True, "at": now(),
                "sample_sha256": SAMPLE_SHA256, "arms": arms,
                "main_comparison": "all 150 Goodfire DEV hosted readings; CheatBench cross-test never used to choose",
                "supplemental_comparison": "identical sample[:60], 35 Goodfire and 25 CheatBench, every arm/baseline restricted identically",
                "labels_read": False, "subscription_scope_authority": continuation["authority"],
                "subscription_admission_fraction": continuation["admission_fraction"],
                "subscription_completed_batch_receipt": "omp-prefix60-receipts.json",
                "subscription_continuation_receipt": "continuation-200-receipts.json",
                "privacy_audit_receipt": "privacy-audit.json",
                "privacy_gate_source_snapshot": "canonical_secret_gate.py",
                "privacy_secondary_view": "196 safe-eligible IDs, descriptive only; all original 200 remain",
                "financial_basis": {
                    "receipt": "luna-financial-basis.json",
                    "unverified_route": luna_financial["route"],
                    "auth_class": luna_financial["auth_class"],
                    "attested_transport_api": luna_financial["attested_transport_api"],
                    "included_free_entitlement_verified": False,
                    "provider_billed_usd": None, "marginal_usd": None,
                    "client_reported_usd": 0, "list_price_equivalent_usd": 0.0136769,
                },
                "privacy_receipt": "docent-receipts.json", "pricing_receipt": "pricing.json"}
    path = OUT / "readings-finalized.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"finalized": True, "arms": len(arms), "manifest": str(path), "labels_read": False}))


if __name__ == "__main__":
    main()
