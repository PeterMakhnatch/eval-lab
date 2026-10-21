"""Unit tests for the seeker-sft-v1 dataset builder, SFT compiler, TB4 DEV scorer, and TB4 TEST leak guard (ST-1 / pE-SELF-1)."""

from __future__ import annotations

import importlib.util
import json
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

    _, tb4_test, _, _ = mod.load_tb4_sets()
    manifests = json.loads(mod.SOURCE_MANIFESTS_PATH.read_text(encoding="utf-8"))
    graft_eval = set(manifests["terminal_wrench_auditor_sft_v1"]["graft_eval_tasks"])

    assert len(admitted) == 911
    assert summary["counts"]["admitted_curated_seeker_sft_rows"] == 400
    assert summary["counts"]["admitted_tier1_replay_verified"] == 86
    assert summary["counts"]["admitted_tier1_differential_verified"] == 42

    for row in admitted:
        assert row["training_status"] == "admitted_sft"
        assert row["tb4_test_disjoint"] is True
        assert row["task_id"] not in tb4_test
        assert not mod.is_tb4_test_alias(row["task_id"], tb4_test)
        assert row["source_family"] != "school_of_reward_hacks"
        if row["source_family"] == "terminal_wrench":
            assert row["task_id"] not in graft_eval
            assert row["graft_tw_split"] == "train"


def test_compile_sft_and_data_quality_audit() -> None:
    build_mod = _load_mod("seeker_sft_v1_build", BUILD_SCRIPT)
    compile_mod = _load_mod("seeker_sft_v1_compile", COMPILE_SCRIPT)
    admitted = build_mod._jsonl(build_mod.ADMITTED_PATH)
    quarantined = build_mod._jsonl(build_mod.QUARANTINED_PATH)
    tier1_examples = compile_mod.compile_tier1_witness_conversations()
    audit = compile_mod.build_data_quality_audit(admitted, quarantined, tier1_examples)

    assert len(tier1_examples) == 20
    assert all(e["parser_verified"] is True for e in tier1_examples)
    assert audit["pool_filtering_summary"]["curated_seeker_sft_rows"] == 400
    assert audit["pool_filtering_summary"]["non_curated_filtered_rows"] == 511
    assert audit["terminus_parser_verification"]["combined_parser_verified_histories"] == 171
    assert audit["terminus_parser_verification"]["combined_parser_verified_supervised_turns"] == 496
    assert audit["terminus_parser_verification"]["combined_parse_pass_rate"] == 1.0
    assert audit["cheatbench_dev_action_audit"]["harness_leak_only_rows"] == 467
    assert audit["cheatbench_dev_action_audit"]["task_level_exploit_rows"] == 49


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
    assert len(admitted) == 912
    seeker_rows = [r for r in admitted if r["trajectory_id"].startswith("tb4-dev-seeker:")]
    assert len(seeker_rows) == 1
    assert seeker_rows[0]["task_id"] == dev_task
    assert seeker_rows[0]["verified_differential"] is True
    assert seeker_rows[0]["curated_seeker_sft"] is True
