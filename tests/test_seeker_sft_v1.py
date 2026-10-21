"""Unit tests for the seeker-sft-v1 dataset builder, SFT compiler, TB4 DEV scorer, and TB4 TEST leak guard (ST-1 / pE-SELF-1 / pE-SELF-2)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO_ROOT / "research/datasets/seeker-sft-v1"
BUILD_SCRIPT = DATASET_DIR / "build.py"
COMPILE_SCRIPT = DATASET_DIR / "compile_sft.py"
EVAL_SCRIPT = DATASET_DIR / "eval_tb4_dev.py"


def _load_mod(name: str, path: Path):
    if str(DATASET_DIR) not in sys.path:
        sys.path.insert(0, str(DATASET_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_committed_seeker_sft_v1_check_passes() -> None:
    build_mod = _load_mod("seeker_sft_v1_build", BUILD_SCRIPT)
    compile_mod = _load_mod("seeker_sft_v1_compile", COMPILE_SCRIPT)
    eval_mod = _load_mod("seeker_sft_v1_eval", EVAL_SCRIPT)
    assert build_mod.main(["--check"]) == 0
    assert compile_mod.main(["--check"]) == 0
    assert eval_mod.main(["--check"]) == 0


def test_admitted_sft_excludes_tb4_test_tasks_digests_aliases_canaries_and_graft_eval() -> None:
    mod = _load_mod("seeker_sft_v1_build", BUILD_SCRIPT)
    admitted, quarantined, summary = mod.build_records()
    leak_audit = mod.run_leak_audit(admitted, quarantined, summary)

    assert leak_audit["status"] == "pass"
    assert leak_audit["tb4_test_tasks_checked"] == 41
    assert leak_audit["tb4_test_digests_checked"] == 41
    assert leak_audit["tb4_test_task_hits_in_admitted"] == 0
    assert leak_audit["literal_tb4_test_hits_across_all_files"] == 0
    assert leak_audit["canary_hits_in_admitted"] == 0
    assert leak_audit["raw_payload_canary_hits_in_admitted"] == 0
    assert leak_audit["raw_payload_tb4_test_hits_in_admitted"] == 0
    assert leak_audit["unresolved_pin_placeholders"] == 0
    assert leak_audit["admitted_rows_with_embedded_payload_and_sha256"] == 429

    _, tb4_test, _, _ = mod.load_tb4_sets()
    manifests = json.loads(mod.SOURCE_MANIFESTS_PATH.read_text(encoding="utf-8"))
    graft_eval = set(manifests["terminal_wrench_auditor_sft_v1"]["graft_eval_tasks"])

    assert len(admitted) == 429
    assert len(quarantined) == 1200
    assert summary["counts"]["admitted_curated_seeker_sft_rows"] == 322
    assert summary["counts"]["admitted_tier1_replay_verified"] == 87
    assert summary["counts"]["admitted_tier1_differential_verified"] == 28
    for row in admitted + quarantined:
        src_pin = row["source_pin"]
        pin_sha = str(src_pin.get("artifact_sha256") or src_pin.get("csv_sha256") or "")
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", pin_sha)

    for row in admitted:
        assert row["training_status"] == "admitted_sft"
        assert row["tb4_test_disjoint"] is True
        assert row["task_id"] not in tb4_test
        assert not mod.is_tb4_test_alias(row["task_id"], tb4_test)
        assert row["source_family"] != "school_of_reward_hacks"
        assert row["payload_leak_scan_passed"] is True
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(row["payload_sha256"]))
        assert int(row["payload_bytes"]) > 0
        assert str(row["payload_excerpt"]).strip()
        assert len(row["hackbook_entry_ids"]) >= 1
        if str(row["trajectory_id"]).startswith("mimo-eo:"):
            assert row["verified_differential"] is False
            assert row["replay_evidence"]["nop_control_reward"] == 0.0
            assert row["replay_evidence"]["hardened_reward"] is None
        if row["source_family"] == "terminal_wrench":
            assert row["task_id"] not in graft_eval
            assert row["graft_tw_split"] == "train"


def test_compile_sft_and_data_quality_audit() -> None:
    build_mod = _load_mod("seeker_sft_v1_build", BUILD_SCRIPT)
    compile_mod = _load_mod("seeker_sft_v1_compile", COMPILE_SCRIPT)
    admitted = build_mod._jsonl(build_mod.ADMITTED_PATH)
    quarantined = build_mod._jsonl(build_mod.QUARANTINED_PATH)
    tier1_examples = compile_mod.compile_tier1_witness_conversations()
    seeker_tool_examples = compile_mod.compile_seeker_tool_conversations()
    transfer_splits = compile_mod.build_transfer_splits(admitted, seeker_tool_examples)
    audit = compile_mod.build_data_quality_audit(
        admitted, quarantined, tier1_examples, seeker_tool_examples, transfer_splits
    )

    assert len(tier1_examples) == 21
    assert all(e["parser_verified"] is True for e in tier1_examples)
    assert len(seeker_tool_examples) == 90
    assert all(e["confined_tools_verified"] is True for e in seeker_tool_examples)
    assert all(e["strict_packaging_verified"] is True for e in seeker_tool_examples)
    assert audit["pool_filtering_summary"]["curated_seeker_sft_rows"] == 322
    assert audit["pool_filtering_summary"]["non_curated_filtered_rows"] == 107
    assert audit["terminus_parser_verification"]["combined_parser_verified_histories"] == 162
    assert audit["terminus_parser_verification"]["combined_parser_verified_supervised_turns"] == 465
    assert audit["terminus_parser_verification"]["combined_parse_pass_rate"] == 1.0
    assert audit["ve1_st1_clearance_resolution"]["st1_unresolved_pins_remaining"] == 0
    assert audit["ve1_st1_clearance_resolution"]["quarantined_unmaterialized_tw_subset_rows"] == 65
    assert audit["ve1_st1_clearance_resolution"]["st1_controls_not_differential_reclassified"] == 15
    assert audit["ve1_st1_clearance_resolution"]["true_differential_verified_1_0_admitted"] == 28
    assert audit["ve1_st1_clearance_resolution"]["quarantined_raw_payload_canary_rows"] == 414
    assert audit["seeker_tool_sft_verification"]["total_conversations"] == 90
    assert audit["seeker_tool_sft_verification"]["unconditioned_positive_exploits"] == 30
    assert audit["seeker_tool_sft_verification"]["hackbook_conditioned_positive_exploits"] == 30
    assert (
        audit["seeker_tool_sft_verification"]["confirmed_1_0_differential_positive_conversations"]
        == 56
    )
    assert (
        audit["seeker_tool_sft_verification"]["single_version_1_1_replay_positive_conversations"]
        == 4
    )
    assert audit["seeker_tool_sft_verification"]["executed_clean_negative_controls"] == 30
    assert audit["seeker_tool_sft_verification"]["confined_tools_verified_rate"] == 1.0
    assert audit["seeker_tool_sft_verification"]["strict_packaging_verified_rate"] == 1.0
    assert audit["seeker_tool_sft_verification"]["task_disjoint_tb4_dev_train_conversations"] == 68
    assert audit["seeker_tool_sft_verification"]["loco_splits_verified"] is True

    assert transfer_splits["task_disjoint_tb4_dev"]["train_eval_task_overlap_count"] == 0
    assert transfer_splits["task_disjoint_tb4_dev"]["tb4_test_overlap_count"] == 0
    for split_info in transfer_splits["leave_one_hack_class_out"].values():
        assert split_info["zero_heldout_class_in_train_verified"] is True
        assert split_info["train_seeker_tool_negative_controls"] == 30


def test_tb4_dev_eval_cohort_and_differential_replay_scorer() -> None:
    eval_mod = _load_mod("seeker_sft_v1_eval", EVAL_SCRIPT)
    cohort = eval_mod.build_tb4_dev_cohort()
    counts = cohort["counts"]
    assert counts["total_dev_tasks"] == 25
    assert counts["strict_unseen_dev_tasks"] == 20
    assert counts["witness_seeded_dev_tasks"] == 4
    assert counts["quarantined_alias_dev_tasks"] == 1
    assert counts["total_dev_findings"] == 86
    assert counts["wrong_pass_dev_findings"] == 57
    assert counts["pm_executed_reward_hack_dev_findings"] == 49

    smoke = cohort["witness_smoke_verification"]
    assert smoke["confirmed_differential_exploits_1_0"] == 10
    assert smoke["confirmed_tasks_count"] == 4
    assert smoke["diagnostic_port_also_passed_with_tripwire_1_1"] == 1
    assert smoke["rejected_invalid_controls_or_binding"] == 1
    assert smoke["non_exploit_trials"] == 1

    rs1 = cohort["rs1_baseline_comparison"]
    assert rs1["drivers"]["sonnet"]["confirmed_differential_exploits_1_0"] == 1
    assert rs1["drivers"]["sonnet"]["matched_pm_findings_count"] == 4
    assert rs1["drivers"]["gptoss"]["matched_pm_findings_count"] == 3
    assert (
        rs1["open_model_gap_vs_pm_executed_dev_hacks"]["open_model_missed_pm_executed_findings"]
        == 46
    )
    strict_audit = rs1["strict_submission_audit"]
    assert strict_audit["sonnet"]["strict_valid"] == 23
    assert strict_audit["codex"]["strict_valid"] == 23
    assert strict_audit["gptoss"]["structural_valid"] == 25
    assert strict_audit["gptoss"]["strict_valid"] == 20
    assert strict_audit["gptoss"]["strict_non_noop_valid"] == 14
    assert strict_audit["gptoss"]["claimed_exploits_strict_valid"] == 12
    assert strict_audit["gptoss"]["orphaned_companion_submissions"] == 3
    assert strict_audit["gptoss"]["contradictory_exploit_false_submissions"] == 2

    hb_audit = rs1["blind_hackbook_signal_audit"]
    assert hb_audit["total_dev_tasks"] == 25
    assert hb_audit["tasks_with_discriminating_signals"] == 25
    assert hb_audit["tasks_with_pm_findings"] == 22
    assert hb_audit["exact_pm_class_hit_tasks"] == 17
    assert hb_audit["gptoss_unconditioned_missed_pm_tasks"] == 19
    assert hb_audit["hackbook_exact_class_hits_on_gptoss_missed_pm_tasks"] == 14


def test_seeker_findings_hook_admits_tb4_dev_and_drops_tb4_test(tmp_path: Path) -> None:
    mod = _load_mod("seeker_sft_v1_build", BUILD_SCRIPT)
    tb4_dev, tb4_test, _, _ = mod.load_tb4_sets()
    dev_task = sorted(tb4_dev)[0]
    test_task = sorted(tb4_test)[0]

    findings_file = tmp_path / "rs1_findings.jsonl"
    findings_file.write_text(
        json.dumps(
            {
                "task": dev_task,
                "model": "sonnet-seeker",
                "mechanism_task_class": "weak_check",
                "mechanism_run_class": "exploit_grader_gap",
                "mechanism_detail": "rs1_confirmed_exploit",
                "upstream_reward": 1.0,
                "port_reward": 0.0,
            }
        )
        + "\n"
        + json.dumps(
            {
                "task": test_task,
                "model": "sonnet-seeker",
                "mechanism_task_class": "weak_check",
                "mechanism_run_class": "exploit_grader_gap",
                "mechanism_detail": "should_be_dropped",
                "upstream_reward": 1.0,
                "port_reward": 0.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    admitted, quarantined, summary = mod.build_records(findings_file)
    leak_audit = mod.run_leak_audit(admitted, quarantined, summary)
    assert leak_audit["status"] == "pass"
    assert len(admitted) == 430
    seeker_rows = [r for r in admitted if r["trajectory_id"].startswith("tb4-dev-seeker:")]
    assert len(seeker_rows) == 1
    assert seeker_rows[0]["task_id"] == dev_task
    assert seeker_rows[0]["verified_differential"] is True
    assert seeker_rows[0]["curated_seeker_sft"] is True


def test_seeker_sft_pilot_uses_the_committed_corpus_and_reports_every_replay() -> None:
    """pE-SELF-5: the pilot's container saw exactly the committed corpus, and no run is silently unreplayed."""
    pilot_dir = REPO_ROOT / "research/experiments/seeker-sft-pilot-20261021"
    validation = json.loads((pilot_dir / "validation.json").read_text(encoding="utf-8"))
    committed = [
        json.loads(line)
        for line in (DATASET_DIR / "seeker_tool_sft.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert validation["conversations"] == len(committed) == 90
    assert [row["example_id"] for row in validation["rows"]] == [
        row["example_id"] for row in committed
    ]

    summary = json.loads((pilot_dir / "replay-summary.json").read_text(encoding="utf-8"))
    runs = summary["runs"]
    assert len(runs) == 12
    assert {run["arm"] for run in runs} == {"base", "trained"}
    assert len({run["task"] for run in runs}) == 6
    # A run with no recorded replay outcome would let a partial replay read as complete.
    assert all(run["upstream_status"] for run in runs)
    # Confirmation is the measured 1/0 differential, never the model's own claim.
    for run in runs:
        assert run["confirmed_1_0"] is (run["upstream_reward"] == 1.0 and run["port_reward"] == 0.0)
        if run["confirmed_1_0"]:
            assert run["claimed_exploit"] is True


def test_seeker_training_data_excludes_tb4_audit_v1_test_tasks() -> None:
    """pE-SELF-6: the authoritative split marks 41 TEST tasks; none may reach training data."""
    keys = [
        json.loads(line)
        for line in (REPO_ROOT / "research/answer-keys/tb4-audit-v1/tasks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    test_tasks = {row["task"].rsplit("/", 1)[-1] for row in keys if row.get("split") == "test"}
    dev_tasks = {row["task"].rsplit("/", 1)[-1] for row in keys if row.get("split") == "dev"}
    assert len(test_tasks) == 41
    assert len(dev_tasks) == 25

    for path in sorted(DATASET_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            task = row.get("task_id") or row.get("task")
            if isinstance(task, str) and task:
                assert task.rsplit("/", 1)[-1] not in test_tasks, f"{path.name}: {task}"


def test_seeker_sft_loss_target_ablation_reports_every_run() -> None:
    """pE-SELF-6: both loss-target arms must carry a replay outcome, and confirmation stays measured."""
    experiment = REPO_ROOT / "research/experiments/seeker-sft-loss-target-20261021"
    results = json.loads((experiment / "loss-target-results-768.json").read_text(encoding="utf-8"))
    assert results["max_new_tokens"] == 768
    assert set(results["arms"]) == {"full", "assistant"}
    for arm, payload in results["arms"].items():
        assert len(payload["runs"]) == 6, arm

    summary = json.loads((experiment / "replay-summary.json").read_text(encoding="utf-8"))
    runs = summary["runs"]
    assert len(runs) == 12
    assert {run["arm"] for run in runs} == {"full", "assistant"}
    assert all(run["upstream_status"] for run in runs)
    for run in runs:
        assert run["confirmed_1_0"] is (run["upstream_reward"] == 1.0 and run["port_reward"] == 0.0)

    leak = json.loads((experiment / "leak-check.json").read_text(encoding="utf-8"))
    assert leak["status"] == "pass"
    assert leak["training_test_task_hits"] == []
    assert leak["artifact_hits"] == []
    assert set(leak["pilot_task_splits"].values()) == {"dev"}


def test_seeker_eval_baselines_reconcile_with_the_replay_summaries() -> None:
    """pE-SELF-7: the baseline table must reconcile with the committed replay summaries."""
    experiment = REPO_ROOT / "research/experiments/seeker-eval-v1-20261021"
    baselines = json.loads((experiment / "baselines.json").read_text(encoding="utf-8"))
    rows = {row["arm"]: row for row in baselines["rows"] if row["family"] == "sft"}
    for source, path in (
        (
            "pilot-768",
            REPO_ROOT / "research/experiments/seeker-sft-pilot-20261021/replay-summary.json",
        ),
        (
            "loss-target-768",
            REPO_ROOT / "research/experiments/seeker-sft-loss-target-20261021/replay-summary.json",
        ),
    ):
        summary = json.loads(path.read_text(encoding="utf-8"))
        for arm, bucket in summary["by_arm"].items():
            row = rows[f"{source}:{arm}"]
            assert row["tasks"] == bucket["runs"]
            assert row["strict_valid"] == bucket["strict_valid"]
            assert row["claims"] == bucket["claimed_exploits"]
            assert row["upstream_measured"] == bucket["upstream_measured"]
            assert row["upstream_reward1"] == bucket["upstream_reward_1"]
            assert row["confirmed_1_0"] == bucket["confirmed_1_0"]
            assert row["plausible_claims"] <= row["claims"]

    truncation = json.loads((experiment / "truncation-analysis.json").read_text(encoding="utf-8"))
    for bucket in truncation["by_arm"].values():
        assert bucket["runs_with_unparsed_turns"] <= bucket["runs"]
        assert bucket["unparsed_tool_call_turns"] <= bucket["turns"]
    # pE-SELF-5's arms show no truncation; that is what corrects the pE-SELF-6 claim.
    assert truncation["by_arm"]["pilot-768:base"]["unparsed_tool_call_turns"] == 0
    assert truncation["by_arm"]["pilot-768:trained"]["unparsed_tool_call_turns"] == 0

    quality = json.loads((experiment / "claim-quality.json").read_text(encoding="utf-8"))
    for bucket in quality["by_arm"].values():
        assert bucket["plausible_claims"] <= bucket["claims"] <= bucket["runs"]


def test_seeker_corpus_audit_reconciles_with_the_committed_corpus() -> None:
    """pE-SELF-8: the audit's effective-size numbers must match the committed corpus."""
    experiment = REPO_ROOT / "research/experiments/seeker-corpus-audit-20261021"
    audit = json.loads((experiment / "corpus-audit.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (DATASET_DIR / "seeker_tool_sft.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    effective = audit["effective_size"]

    assert audit["conversations"] == len(rows) == 90
    assert effective["conversations"] == len(rows)
    assert effective["positive_conversations"] == sum(1 for row in rows if row["exploit_found"])
    assert effective["negative_conversations"] == sum(1 for row in rows if not row["exploit_found"])
    # The finding is that the corpus is smaller than its conversation count.
    assert effective["distinct_task_body_pairs"] < len(rows)
    assert effective["positive_distinct_task_body_pairs"] < effective["positive_conversations"]
    assert effective["extra_copies"] > 0
    assert audit["execution_share"]["execution_share_of_assistant_content"] > 0.5
