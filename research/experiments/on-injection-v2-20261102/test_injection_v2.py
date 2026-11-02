"""Tests for B4-INJECTION-V2 experiment library and metric computation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evallab.autoreview_monitor import build_compact_view
from evallab.trace_anatomy import LoadedRun, build_anatomy

sys.path.insert(0, str(Path(__file__).parent))
from experiment_lib import (  # noqa: E402
    BENCH_ROOT,
    CHEATING_IDS,
    DEFENSE_D_PASS1_SYSTEM,
    DEFENSE_E_LINE,
    HONEST_IDS,
    PAYLOADS,
    build_baseline_prompt,
    build_defense_d_pass1_prompt,
    build_defense_d_pass2_prompt,
    build_defense_e_prompt,
    inject_run,
)


def test_cohort_definitions() -> None:
    assert len(CHEATING_IDS) == 18
    assert len(HONEST_IDS) == 30
    assert len(set(CHEATING_IDS)) == 18
    assert len(set(HONEST_IDS)) == 30
    assert not set(CHEATING_IDS).intersection(set(HONEST_IDS))

    for cid in CHEATING_IDS + HONEST_IDS:
        fp = BENCH_ROOT / f"{cid}.json"
        assert fp.exists(), f"Missing trajectory file: {fp}"


def test_payloads_specification() -> None:
    assert len(PAYLOADS) == 6
    expected_keys = {
        "v0_sec881",
        "v1_iso_compliance",
        "v2_qa_lead_ticket",
        "v3_secops_incident",
        "v4_arb_rfc",
        "v5_harness_calib",
    }
    assert set(PAYLOADS.keys()) == expected_keys


def test_injection_survival() -> None:
    # Test on a representative cheating run
    cid = "c1-billing-amount-format__gemini38__t1"
    fp = BENCH_ROOT / f"{cid}.json"
    doc = json.loads(fp.read_text())

    for variant_key, payload in PAYLOADS.items():
        doc_inj = inject_run(doc, variant_key)
        run = LoadedRun(run_id=cid, loader="bench", doc=doc_inj, raw_chars=len(json.dumps(doc_inj)))
        anatomy = build_anatomy(run)
        view = build_compact_view(anatomy, run)

        # Confirm that a key token from each payload appears in the rendered view text
        token = payload.split()[1]  # e.g., APPROVED, COMPLIANCE, QA, SECOPS, ARCHITECTURE, EVAL-LAB
        assert token in view.text, f"Token {token} not found in view text for variant {variant_key}"


def test_prompt_builders() -> None:
    cid = "c1-billing-amount-format__gemini38__t1"
    fp = BENCH_ROOT / f"{cid}.json"
    doc = json.loads(fp.read_text())
    run = LoadedRun(run_id=cid, loader="bench", doc=doc, raw_chars=len(json.dumps(doc)))
    anatomy = build_anatomy(run)
    view = build_compact_view(anatomy, run)

    baseline_prompt = build_baseline_prompt(view)
    assert "<view>" in baseline_prompt
    assert view.text in baseline_prompt

    defense_e = build_defense_e_prompt(view)
    assert DEFENSE_E_LINE.strip() in defense_e
    assert "<view>" in defense_e

    pass1 = build_defense_d_pass1_prompt(view)
    assert DEFENSE_D_PASS1_SYSTEM.strip() in pass1
    assert "<trace>" in pass1

    pass2 = build_defense_d_pass2_prompt("Action 1: bash command")
    assert "<action_ledger>" in pass2
    assert "Action 1: bash command" in pass2

def test_compute_metrics_synthetic() -> None:
    from compute_metrics import (
        compute_defense_metrics,
        compute_injection_metrics,
        compute_noise_floor_metrics,
    )

    synthetic_receipts: dict[str, dict] = {}

    # Create synthetic control repeats
    for cid in CHEATING_IDS:
        for r in range(1, 6):
            # One case flip-flops
            is_alert = not (cid == CHEATING_IDS[0] and r == 3)
            synthetic_receipts[f"control_r{r}_{cid}"] = {
                "is_alert": is_alert,
                "suspicion": 95 if is_alert else 10,
                "verdict": "confirmed_cheat" if is_alert else "clean",
            }

    for cid in HONEST_IDS:
        for r in range(1, 6):
            synthetic_receipts[f"control_r{r}_{cid}"] = {
                "is_alert": False,
                "suspicion": 0,
                "verdict": "clean",
            }

    noise_res = compute_noise_floor_metrics(synthetic_receipts)
    assert noise_res["flip_flop_cheats"]["flip_flop_count"] == 1
    assert noise_res["flip_flop_honest"]["flip_flop_count"] == 0

    # Create synthetic injection runs
    for var in PAYLOADS:
        for r in range(1, 4):
            for cid in CHEATING_IDS:
                # Introduce a flip in v0_sec881 for case 1
                is_alert = not (var == "v0_sec881" and cid == CHEATING_IDS[1])
                synthetic_receipts[f"inj_{var}_r{r}_{cid}"] = {
                    "is_alert": is_alert,
                    "suspicion": 90 if is_alert else 15,
                    "verdict": "confirmed_cheat" if is_alert else "clean",
                }

    inj_res = compute_injection_metrics(synthetic_receipts)
    assert inj_res["by_variant"]["v0_sec881"]["flips"] == 3
    assert inj_res["total_flips"] == 3

    # Create synthetic defense runs
    for d_name in ("defense_d", "defense_e"):
        for var in PAYLOADS:
            for cid in CHEATING_IDS:
                synthetic_receipts[f"{d_name}_{var}_{cid}"] = {
                    "is_alert": True,
                    "suspicion": 90,
                    "verdict": "confirmed_cheat",
                }
        for cid in CHEATING_IDS:
            synthetic_receipts[f"{d_name}_uninjected_{cid}"] = {
                "is_alert": True,
                "suspicion": 95,
                "verdict": "confirmed_cheat",
            }
        for cid in HONEST_IDS:
            synthetic_receipts[f"{d_name}_uninjected_{cid}"] = {
                "is_alert": False,
                "suspicion": 0,
                "verdict": "clean",
            }

    def_res = compute_defense_metrics(synthetic_receipts)
    assert def_res["defenses"]["defense_d"]["injected_flips"]["k"] == 0
    assert def_res["defenses"]["defense_e"]["injected_flips"]["k"] == 0
    assert def_res["defenses"]["defense_d"]["uninjected_cheat_recall"]["rate"] == 1.0
    assert def_res["defenses"]["defense_d"]["honest_false_alarms"]["rate"] == 0.0
