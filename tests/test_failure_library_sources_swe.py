from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evallab.failure_library.sources.constructed import (
    DJINN_REVISION,
    djinn_family,
    parse_djinn,
    parse_impossible,
    parse_impossible_controls,
)
from evallab.failure_library.sources.swe_family import (
    PRO_FIXED_REVISION,
    PRO_ORIGINAL_REVISION,
    SWE_ORIGINAL_REVISION,
    SplitPolicy,
    parse_aba,
    parse_openai,
    parse_patchdiff,
    parse_pro_corrections,
    parse_utboost,
    pinned_bytes,
)
from evallab.failure_library.store import LibraryStore


@pytest.fixture
def policy(tmp_path: Path) -> SplitPolicy:
    assignments = tmp_path / "research/answer-keys/splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(
        "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
        "older,i1,astropy__astropy-12907,u1,dev,fail,\n"
        "reported,i2,swebench/astropy__astropy-12907@pin,u2,test,fail,\n"
        "cheatbench-actions-v1,i3,cross-task,u3,dev,fail,\n",
        encoding="utf-8",
    )
    return SplitPolicy(tmp_path)


def test_split_policy_blocks_cross_key_reported_tasks_and_groups_variants(
    policy: SplitPolicy,
) -> None:
    assert policy.get("astropy__astropy-12907", "swe-bench/astropy__astropy-12907") == "TEST"
    assert policy.get("cross-task", "new/cross-task") == "cross_test"
    assert policy.get("new-task/oneoff", "shared-family") == policy.get(
        "new-task/conflicting", "shared-family"
    )
    group = "shared-family"
    expected = "heldout" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5 == 0 else "DEV"
    assert policy.get("new-task", group) == expected


def test_openai_real_csv_severities_remain_human_labels(policy: SplitPolicy) -> None:
    rows = [
        {
            "instance_id": "astropy__astropy-12907",
            "underspecified": "2.0",
            "underspecified_notes": "An unstated boundary condition.",
            "false_negative": "3.0",
            "false_negative_notes": "Tests require one implementation.",
            "other_major_issues": "1.0",
            "other_notes": "Other major issue needs classification.",
            "filter_out": "True",
        },
        {
            "instance_id": "clean",
            "underspecified": "1.0",
            "false_negative": "0.0",
            "other_major_issues": "0.0",
            "filter_out": "False",
        },
    ]
    findings = parse_openai(rows, policy)
    assert len(findings) == 3
    assert {f.defect_class for f in findings} == {"TS05", "TS07", "unclassified"}
    assert all(f.tier == "V1_external_label" and f.split == "TEST" for f in findings)
    assert all(f.task.upstream_revision == SWE_ORIGINAL_REVISION for f in findings)
    assert all(f.task.upstream_repo.endswith("/SWE-bench") for f in findings)
    assert all(f.evidence[0].kind == "human_label" for f in findings)
    assert parse_openai(reversed(rows), policy) == findings


def test_source_expert_corrections_preserve_original_and_fix_versions(policy: SplitPolicy) -> None:
    readme = """- **Misleading prompts** (1 samples)
```text
instance_repo__repo-abc-vnan
```
- **Overly narrow tests** (1 samples)
```text
instance_repo__repo-def-vnan
```
- **Overly borad tests** (1 samples)
```text
instance_repo__repo-ghi-vnan
```
- **Other issues** (1 samples)
```text
instance_repo__repo-jkl-vnan
```
"""
    findings = parse_pro_corrections(readme, policy)
    assert {f.defect_class for f in findings} == {"TS06", "TS05", "TS01", "unclassified"}
    assert all(f.task.upstream_revision == PRO_ORIGINAL_REVISION for f in findings)
    assert all(f.fixed_in_revision == PRO_FIXED_REVISION for f in findings)
    assert all(f.status_now == "fixed_upstream" and f.tier == "V2_upstream_fixed" for f in findings)
    assert all("opencompass" in f.source_url for f in findings)
    with pytest.raises(ValueError, match="category count"):
        parse_pro_corrections(readme.replace("(1 samples)", "(2 samples)", 1), policy)


def test_augmented_test_patch_is_not_an_executed_wrong_pass(policy: SplitPolicy) -> None:
    rows = {"astropy__astropy-12907": {"aug_test": "fixture augmented patch", "augFail2Pass": []}}
    (finding,) = parse_utboost(rows, policy)
    assert finding.defect_class == "TS01"
    assert finding.direction == "wrong_pass"
    assert finding.tier == "V1_external_label"
    assert finding.evidence[0].kind == "human_label"
    assert finding.evidence[0].reproducer_ref is None
    assert finding.split == "TEST"


def test_patchdiff_keeps_task_patch_pairs_and_excludes_style_only_results(
    policy: SplitPolicy,
) -> None:
    discrepancy = {
        "oracle_pass_model_fail": ["tests/test_optimizer.py::test_alter"],
        "difference": "functionality",
    }
    by_tool = {
        "CodeStory": {
            "astropy__astropy-12907": discrepancy,
            "style-only": {
                "oracle_pass_model_fail": ["lint.py::style"],
                "difference": "coding_conventions",
            },
            "empty": {"oracle_pass_model_fail": [], "difference": "functionality"},
        },
        "OpenHands": {"astropy__astropy-12907": discrepancy},
    }
    findings = parse_patchdiff(by_tool, policy)
    assert len(findings) == 2
    assert len({f.finding_id for f in findings}) == 2
    assert all(f.tier == "V3_executed" and f.direction == "wrong_pass" for f in findings)
    assert all("zenodo.org" in (f.evidence[0].receipt_ref or "") for f in findings)
    assert all(f.split == "TEST" for f in findings)


def test_aba_major_llm_audit_never_becomes_execution_evidence(policy: SplitPolicy) -> None:
    positive = {
        "source": "aba_traj",
        "task_id": "astropy__astropy-12907",
        "positive": True,
        "direction": "wrong_fail",
        "classes": ["TS06", "TS05"],
        "evidence_bar": "llm_audit",
        "raw_label": "Major (severity 2): 2; conflict",
        "ref": "https://data.autobenchaudit.com/example.json",
    }
    findings = parse_aba("swev", [positive, {**positive, "positive": False}], policy)
    assert len(findings) == 2
    assert all(f.tier == "V0_unverified" and f.status_now == "unknown" for f in findings)
    assert all(f.evidence[0].kind == "judge_label" for f in findings)
    assert all(f.summary.startswith("UNVERIFIED") for f in findings)
    assert all(f.direction == "unfair_fail" for f in findings)
    assert all(f.split == "TEST" for f in findings)
    assert (
        parse_aba("swev", [{**positive, "classes": list(reversed(positive["classes"]))}], policy)
        == findings
    )


@pytest.mark.parametrize(
    "dataset,identity",
    [
        ("fjzzq2002/impossible_swebench", "instance_id"),
        ("fjzzq2002/impossible_livecodebench", "task_id"),
    ],
)
def test_impossiblebench_does_not_call_original_controls_broken(
    dataset: str, identity: str, policy: SplitPolicy
) -> None:
    rows = [
        {identity: "astropy__astropy-12907", "impossible_type": variant}
        for variant in ("original", "oneoff", "conflicting")
    ]
    findings = parse_impossible(dataset, rows, policy)
    assert len(findings) == 2
    assert {f.defect_class for f in findings} == {"TS10", "TS06"}
    assert all(f.tier == "V1_external_label" and f.split == "TEST" for f in findings)
    assert len({f.task.task_id for f in findings}) == 2
    assert all(f.evidence[0].kind == "construction" for f in findings)
    with pytest.raises(ValueError, match="Unknown ImpossibleBench"):
        parse_impossible(dataset, [{identity: "a", "impossible_type": "unrecognized"}], policy)


def test_original_control_metadata_is_pinned_and_not_an_executed_clean_claim(
    policy: SplitPolicy,
) -> None:
    dataset = "fjzzq2002/impossible_livecodebench"
    row = {
        "task_id": "astropy__astropy-12907",
        "prompt": "Fixture specification.",
        "test": "opaque original test source",
        "original_test": "opaque original test source",
        "entry_point": "fixture_function",
        "impossible_type": "original",
    }
    (control,) = parse_impossible_controls(dataset, [row], policy)
    assert control["task"].task_id == "astropy__astropy-12907/original"
    assert control["task"].task_version_digest is None
    assert control["task"].upstream_revision
    assert control["split"] == "TEST"
    assert control["label_kind"] == "construction"
    assert control["raw_row_digest"].startswith("sha256:")
    assert len(control["artifact_sha256"]) == 64
    assert "original-00000-of-00001.parquet" in control["label_source"]
    assert "test" not in control
    (changed,) = parse_impossible_controls(dataset, [{**row, "test": "changed source"}], policy)
    assert changed["raw_row_digest"] != control["raw_row_digest"]
    assert changed["split"] == control["split"]
    with pytest.raises(ValueError, match="impossible_type=original"):
        parse_impossible_controls(dataset, [{**row, "impossible_type": "oneoff"}], policy)


def test_djinn_secure_companion_is_not_a_repair_of_the_insecure_verifier(
    policy: SplitPolicy,
) -> None:
    rows = [
        {
            "id": "array_ops_001_14",
            "exploit_type": "verifier_logic_override",
            "fix_hardened": True,
            "description": "Return the array sum.",
            "function_name": "array_sum",
        },
        {
            "id": "unrelated_alias_itc_2",
            "exploit_type": "hardcoding_or_memorization",
            "fix_hardened": False,
            "description": "Return the array  sum.",
            "function_name": "array_sum",
        },
    ]
    findings = parse_djinn("train", rows, policy)
    assert {f.defect_class for f in findings} == {"TS01", "TS04"}
    assert len({f.split for f in findings}) == 1
    assert all(f.tier == "V1_external_label" and f.status_now == "unknown" for f in findings)
    assert all(f.task.upstream_revision == DJINN_REVISION for f in findings)
    assert djinn_family(rows[0]) == djinn_family(rows[1])
    assert djinn_family(rows[0]) != djinn_family({**rows[0], "description": "Different problem."})
    coverage = parse_djinn("coverage", rows[1:], policy)
    assert coverage[0].split == findings[0].split


def test_raw_snapshot_refuses_modified_source_and_survives_missing_origin(tmp_path: Path) -> None:
    origin = tmp_path / "origin.json"
    raw = b'{"source":"fixture"}'
    origin.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    state = tmp_path / "state"
    assert (
        pinned_bytes("fixture", "revision", "data.json", digest, state_root=state, local=origin)
        == raw
    )
    cached = state / "raw/fixture/revision/data.json"
    assert cached.stat().st_mode & 0o222 == 0
    origin.unlink()
    assert pinned_bytes("fixture", "revision", "data.json", digest, state_root=state) == raw
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        pinned_bytes("fixture", "revision", "data.json", "0" * 64, state_root=state)


def test_rebuilt_collection_bytes_are_deterministic(tmp_path: Path, policy: SplitPolicy) -> None:
    rows = [
        {
            "id": f"array_ops_{n:03}_14",
            "exploit_type": "test_skipping",
            "description": "Return the array sum.",
            "function_name": "array_sum",
        }
        for n in range(4)
    ]
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    path = store.write_findings("djinn", parse_djinn("train", rows, policy))
    expected = path.read_bytes()
    store.write_findings("djinn", parse_djinn("train", reversed(rows), policy))
    assert path.read_bytes() == expected
    assert len(list(store.iter_findings("djinn"))) == 4
