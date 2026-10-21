"""Consumer-visible scientific boundaries for the metadata-only MiMo map."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[1] / "research/experiments/mimo-defect-map-v1/build_map.py"
SPEC = importlib.util.spec_from_file_location("mimo_defect_map", PATH)
assert SPEC is not None and SPEC.loader is not None
MAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAP)
PARENT = "sha256:" + "1" * 64
CHILD = "sha256:" + "2" * 64


def selection(name: str = "format-code-task-000001", package: str = PARENT) -> dict[str, Any]:
    return {
        "task_id": name,
        "package_digest": package,
        "record_id": name + "@" + package,
        "materialization": {"state": "verified_exact_package"},
    }


def prediction(
    row: dict[str, Any], classes: list[str] | None = None, status: str = "measured"
) -> dict[str, Any]:
    return {**row, "status": status, "class_keys": classes if classes is not None else []}


def finding(
    identity: str = "source:finding-1",
    package: str | None = PARENT,
    name: str | None = "format-code-task-000001",
) -> dict[str, Any]:
    return {
        "finding_id": identity,
        "task_id": name,
        "package_digest": package,
        "primary_class": {"id": "TS01", "key": "weak_check"},
        "evidence_status": "source_supported_historical",
        "category": "atlas114",
        "resolution": {"status": "historical_parent_finding"},
    }


def test_historical_labels_do_not_transfer_to_repaired_children() -> None:
    child = selection(package=CHILD)
    dispositions, metrics, candidates = MAP.comparison(
        [finding()], [child], [prediction(child, ["weak_check"])]
    )
    assert dispositions[0]["disposition"] == "historical_or_other_package_not_scanned"
    assert metrics["source_supported"]["evaluable_findings"] == 0
    assert metrics["source_supported"]["finding_recall"] is None
    assert candidates == [
        {
            "task_id": child["task_id"],
            "package_digest": CHILD,
            "class_key": "weak_check",
            "status": "unconfirmed_review_candidate",
            "not_a_new_confirmed_defect_or_false_positive": True,
        }
    ]


def test_failed_scan_is_not_a_negative_or_zero_recall() -> None:
    row = selection()
    scan = prediction(row, status="matcher_error")
    dispositions, metrics, candidates = MAP.comparison([finding()], [row], [scan])
    assert dispositions[0]["disposition"] == "matcher_error"
    assert metrics["source_supported"]["known_findings"] == 1
    assert metrics["source_supported"]["unevaluable_findings"] == 1
    assert metrics["source_supported"]["finding_recall"] is None
    assert metrics["precision"] is None and metrics["negative_denominator"] == 0
    assert candidates == []
    pool = MAP.pools([row], [], [scan], [], [])
    assert pool["bounded_screen_pool"]["count"] is None and pool["scan_state"] == "unmeasured"
    assert pool["excluded_by_first_failed_condition"] == {"not_fully_measured_by_real_matcher": 1}


def test_missing_and_duplicate_prediction_dispositions_are_refused() -> None:
    row = selection()
    with pytest.raises(ValueError, match="every roster selection"):
        MAP.comparison([], [row], [])
    with pytest.raises(ValueError, match="every roster selection"):
        MAP.comparison([], [row], [prediction(row), prediction(row)])


def test_programs_and_executions_do_not_collapse_to_task_denominators() -> None:
    row = selection()
    first, second = finding(), finding("source:finding-2")
    first["program"] = second["program"] = {"sha256": PARENT}
    first["executions"], second["executions"] = [{"execution_id": "one"}], [{"execution_id": "two"}]
    _, metrics, _ = MAP.comparison([first, second], [row], [prediction(row, ["weak_check"])])
    metric = metrics["source_supported"]
    assert (
        metric["known_findings"] == metric["evaluable_findings"] == metric["matched_findings"] == 2
    )
    assert metric["known_exact_package_class_labels"] == metric["known_task_packages"] == 1
    assert metric["finding_recall"] == metric["class_label_recall"] == 1


def test_unjoinable_claim_is_explicit_and_excluded_from_recall() -> None:
    row = selection()
    claim = finding("vals:aggregate", package=None, name=None)
    claim["evidence_status"] = "source_claim"
    dispositions, metrics, _ = MAP.comparison([claim], [row], [prediction(row)])
    assert dispositions[0]["disposition"] == "unjoinable_missing_task_identity"
    assert metrics["claims_excluded_from_recall"] == 1
    assert metrics["source_supported"]["known_findings"] == 0
    assert metrics["precision"] is None


def test_exact_controls_and_all_version_heldout_exclusion_bound_training_subset() -> None:
    heldout = [f"format-code-task-{number:06}" for number in range(1, 34)]
    held_child = selection(package=CHILD)
    train = selection("format-code-task-000034")
    no_controls = selection("format-code-task-000035")
    rows = [held_child, train, no_controls]
    controls = [
        {
            "task_id": row["task_id"],
            "package_digest": row["package_digest"],
            "case_bound_reference_nop_qualified": True,
        }
        for row in rows[:2]
    ]
    pool = MAP.pools(rows, [finding()], [prediction(row) for row in rows], controls, heldout)
    assert pool["bounded_screen_pool"]["count"] == 3
    assert pool["control_qualified_subset"]["count"] == 2
    assert pool["training_exclusion_filtered_subset"]["members"] == [train["record_id"]]
    assert pool["training_authorized_count"] == pool["certified_semantic_clean_count"] == 0
    assert pool["heldout_task_ids_excluded_from_training_all_versions"] == heldout


def test_source_custody_rejects_bytes_that_disagree_with_frozen_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    material = b'{"safe_metadata": true}\n'
    source = {
        "source_id": "one",
        "revision": "a" * 40,
        "path": "metadata.json",
        "access": "private-repository",
        "repository": "research-context",
        "sha256": MAP.digest(material),
        "bytes": len(material),
    }
    monkeypatch.setattr(MAP.subprocess, "check_output", lambda *_args, **_kwargs: material)
    assert MAP.load_sources({"sources": [source]}, {"research-context": tmp_path}) == {
        "one": {"safe_metadata": True}
    }
    source["sha256"] = PARENT
    with pytest.raises(ValueError, match="source bytes changed"):
        MAP.load_sources({"sources": [source]}, {"research-context": tmp_path})


def test_class_ids_come_from_pinned_taxonomy_and_incomplete_confirmation_is_refused() -> None:
    classes = MAP.taxonomy_classes({"task_side": [{"id": "official-id", "key": "weak_check"}]})
    row = {
        **finding(),
        "primary_class_key": "weak_check",
        "evidence": [MAP.ref("source", "/finding")],
    }
    normalized = MAP.normalize_findings([row], classes, {"source": {"finding": {}}})
    assert normalized[0]["primary_class"] == {"id": "official-id", "key": "weak_check"}
    row["differential"] = {"confirmed": True, "original_reward": 1, "fixed_reward": 0}
    with pytest.raises(ValueError, match="confirmation needs exact"):
        MAP.normalize_findings([row], classes, {"source": {"finding": {}}})


def test_zero_denominator_round_and_source_exclusions_stay_visible() -> None:
    row = selection()
    refuted = {
        **finding("round:refuted"),
        "evidence_status": "source_refuted_or_unverified",
        "category": "round5",
    }
    diagnostic = {
        **finding("v7:diagnostic"),
        "evidence_status": "source_optional_diagnostic",
        "category": "initial_v7",
    }
    _, metrics, _ = MAP.comparison(
        [refuted, diagnostic],
        [row],
        [prediction(row, ["weak_check"])],
        {"round4": "retained_complete_round_with_zero_programs"},
    )
    assert metrics["by_category"]["round4"]["denominator_state"] == "no_known_findings"
    assert metrics["by_category"]["round4"]["finding_recall"] is None
    assert metrics["source_supported"]["known_findings"] == 0
    assert metrics["excluded_from_supported_recall"]["by_evidence_status"] == {
        "source_optional_diagnostic": 1,
        "source_refuted_or_unverified": 1,
    }


def review_text(
    round5: str = "UNVERIFIED", good_data: str = "PARTIAL (3 of the 6 earlier fails not located)"
) -> str:
    rows = [
        "| 2 (attack v7.1) | a | b | c | d | PASS |",
        "| 3 (attack round-2 repairs) | a | b | c | d | PASS |",
        "| 4 (attack round-3 repair) | a | b | c | d | PASS |",
        f"| 5 (GLM seeker, all 8 graders) | in progress; no repairs at 07:29 ET | — | — | — | {round5} |",
    ]
    claim = f"| 3 | Good data 8/14 | {good_data} |"
    lines = [""] * 11 + rows + [""] * (97 - 11 - len(rows)) + [claim] + [""] * 30
    return "\n".join(lines)


def test_independent_review_leaves_unchecked_rounds_unqualified() -> None:
    review = MAP.morning_review({"morning_review": review_text()})
    verdicts = {name: row["verdict"] for name, row in review["round_verdicts"].items()}
    assert verdicts == {
        "round2": "PASS",
        "round3": "PASS",
        "round4": "PASS",
        "round5": "UNVERIFIED",
        "round6": "NOT_CHECKED",
    }
    with pytest.raises(ValueError, match="verdicts changed"):
        MAP.morning_review({"morning_review": review_text(round5="PASS")})


def test_timing_log_contributes_only_a_single_failed_node() -> None:
    log = "noise\nE   AssertionError: 0.17 not greater than 0.3\nFAILED tests/test_a.py::T::test_x - Asserti...\n"
    assert MAP.failed_reference_node(log) == {
        "failed_node_id": "tests/test_a.py::T::test_x",
        "elapsed_lower_bound_assertion": True,
    }
    with pytest.raises(ValueError, match="exactly one failed node"):
        MAP.failed_reference_node(log + "FAILED tests/test_a.py::T::test_y - Asserti...\n")


def test_recall_for_one_class_does_not_credit_another_class() -> None:
    weak_task = selection()
    flaky_task = selection("format-code-task-000002", CHILD)
    weak = finding()
    flaky = finding("source:flaky", CHILD, flaky_task["task_id"])
    flaky["primary_class"] = {"id": "TS09", "key": "env_mismatch_or_flaky"}
    _, metrics, _ = MAP.comparison(
        [weak, flaky],
        [weak_task, flaky_task],
        [prediction(weak_task, ["weak_check"]), prediction(flaky_task)],
    )
    assert metrics["source_supported"]["finding_recall"] == 0.5
    assert metrics["by_primary_class"]["weak_check"]["finding_recall"] == 1
    assert metrics["by_primary_class"]["env_mismatch_or_flaky"]["finding_recall"] == 0
    assert metrics["by_primary_class"]["env_mismatch_or_flaky"]["evaluable_findings"] == 1


WEAK_ANCESTOR = "sha256:" + "3" * 64
SIBLING = "sha256:" + "4" * 64
TASK = "format-code-task-000001"
CLASSES = {
    "weak_check": {"id": "TS01", "key": "weak_check"},
    "env_mismatch_or_flaky": {"id": "TS09", "key": "env_mismatch_or_flaky"},
}


def catalog_row(
    package: str,
    status: str = "repaired",
    source: str = "batch2",
    item: str | None = None,
    staged: str | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def observed(agent: str, rewards: list[int], package_digest: str) -> dict[str, Any]:
        return {
            "agent": agent,
            "rewards": rewards,
            "execution_id": agent,
            "source_package_digest": package_digest,
        }

    lineage: dict[str, Any] = {"record_sha256": "sha256:" + "7" * 64}
    if edges is not None:
        lineage["registered_edges_newest_first"] = edges
    return {
        "task_id": TASK,
        "package_digest": package,
        "harbor_digest": "sha256:" + "5" * 64,
        "verifier_digest": "sha256:" + "6" * 64,
        "record_id": TASK + "@" + package,
        "status": status,
        "repair_scope": "assertion-scoped controls; not blanket semantic certification",
        "heldout_excluded_from_training": False,
        "usage": {"training_authorized_by_this_release": False},
        "evidence": {"source_id": source, "json_pointer": "/tasks/0"},
        "lineage": lineage,
        "weakness_review": {"same_task_historical_positive_item": item},
        "controls": {
            "strict_reference_nop_pair": True,
            "reference": observed("oracle", [1, 1, 1], staged or package),
            "nop": observed("nop", [0, 0, 0], package),
            "observations": [],
        },
    }


def handoff() -> dict[str, Any]:
    return {
        "source": {"source_id": "batch2", "json_pointer": "/tasks/0"},
        "strict_assertion_scope_admission": True,
        "reference_nop_repeat3_pair_proved": True,
        "transformation": None,
        "changed_files": None,
        "supersedes_requested_package": None,
    }


def existing_repair(package: str = CHILD) -> dict[str, Any]:
    return {
        "repair_id": f"initial-v7:{TASK}:{package}",
        "category": "initial_v7",
        "task_id": TASK,
        "parent_package_digest": PARENT,
        "child_package_digest": package,
        "retained_detail": {"qualified_for_v7": False},
        "evidence": [{"source_id": "train_repairs", "json_pointer": "/repairs/0"}],
    }


def test_controls_only_repair_stays_controls_and_never_becomes_defect_evidence() -> None:
    catalog = [
        catalog_row(WEAK_ANCESTOR, "known-weak", "atlas"),
        catalog_row(CHILD, item="atlas:task@3333", staged=SIBLING),
    ]
    repairs, summary = MAP.reconcile_v7_repairs(catalog, [], {(TASK, CHILD): handoff()}, [])
    (row,) = repairs
    assert row["observed_wrong_program_positive"] is False
    assert row["source_qualification"]["semantic_certification"] is False
    assert row["known_weak_ancestry"]["ancestry_basis"] == "same_task_id_only_not_established"
    assert row["known_weak_ancestry"]["ancestor_package_digests_in_registered_lineage"] == []
    assert row["control_summary"]["nop"]["staged_on_selected_package"] is True
    assert row["control_summary"]["reference"]["staged_on_selected_package"] is False
    assert summary["same_task_id_only_ancestry_selected_packages"] == 1
    # A finding that points at this child cannot lend a class to a controls-only row.
    linked = {**finding("linked", WEAK_ANCESTOR, TASK), "resolution": {"child_package_digest": CHILD}}
    (classified,) = MAP.classify_repairs([row], [linked], CLASSES)
    assert classified["repair_class"]["status"] == "unmapped_underdetermined"
    assert classified["repair_class"]["class_keys"] == []


def test_registered_ancestor_is_bound_exactly_and_labels_do_not_transfer() -> None:
    edges = [
        {
            "package_digest": CHILD,
            "parent": {"package_digest": SIBLING},
            "transform": "verifier-repair@1",
            "components_changed": ["verifier"],
        },
        {
            "package_digest": SIBLING,
            "parent": {"package_digest": WEAK_ANCESTOR},
            "transform": "setup-python-path@1",
            "components_changed": ["environment"],
        },
    ]
    catalog = [catalog_row(WEAK_ANCESTOR, "known-weak", "atlas"), catalog_row(CHILD, source="train", edges=edges)]
    (row,), summary = MAP.reconcile_v7_repairs(catalog, [existing_repair()], {}, [])
    ancestry = row["known_weak_ancestry"]
    assert ancestry["ancestor_package_digests_in_registered_lineage"] == [WEAK_ANCESTOR]
    assert ancestry["direct_parent_package_digest"] == PARENT
    assert ancestry["ancestor_is_direct_parent"] is False
    assert ancestry["label_transferred_to_child"] is ancestry["label_transferred_to_direct_parent"] is False
    assert [step["transform"] for step in row["corrective_runtime_lineage"]] == ["setup-python-path@1"]
    assert summary["ancestry_established_selected_packages"] == 1


def test_same_task_repairs_for_different_packages_do_not_collapse() -> None:
    catalog = [catalog_row(CHILD, source="train"), catalog_row(SIBLING)]
    repairs, summary = MAP.reconcile_v7_repairs(
        catalog, [existing_repair()], {(TASK, SIBLING): handoff()}, []
    )
    assert [row["child_package_digest"] for row in repairs] == [CHILD, SIBLING]
    assert summary["task_ids_with_multiple_repair_packages"] == {TASK: 2}
    assert summary["selected_repaired_packages"] == 2


def test_existing_exact_repair_is_enriched_not_counted_twice() -> None:
    catalog = [catalog_row(CHILD, source="train")]
    repairs, summary = MAP.reconcile_v7_repairs(catalog, [existing_repair()], {}, [])
    (row,) = repairs
    assert row["retained_detail"] == {"qualified_for_v7": False}
    assert [evidence["source_id"] for evidence in row["evidence"]] == ["train_repairs", "clean_v7"]
    assert (summary["reconciled_to_existing_rows"], summary["added_from_catalog"]) == (1, 0)
    with pytest.raises(ValueError, match="duplicate repair row"):
        MAP.reconcile_v7_repairs(catalog, [existing_repair(), existing_repair()], {}, [])
    with pytest.raises(ValueError, match="no repair history or recognized cohort"):
        MAP.reconcile_v7_repairs(catalog, [], {}, [])


def test_claim_only_finding_does_not_classify_a_repair_and_corrective_detail_is_attached() -> None:
    catalog = [catalog_row(CHILD, source="train")]
    runtime = {(TASK, "setup-python-path@1"): {"class_review": {"primary_class": None}}}
    catalog[0]["lineage"]["registered_edges_newest_first"] = [
        {
            "package_digest": CHILD,
            "parent": {"package_digest": SIBLING},
            "transform": "setup-python-path@1",
            "components_changed": ["environment"],
        }
    ]
    repairs, _ = MAP.reconcile_v7_repairs(catalog, [existing_repair()], {}, [], runtime)
    claim = {
        **finding("claim", WEAK_ANCESTOR, TASK),
        "evidence_status": "source_claim",
        "resolution": {"child_package_digest": CHILD},
    }
    (row,) = MAP.classify_repairs(repairs, [claim], CLASSES)
    assert row["repair_class"]["status"] == "unmapped_underdetermined"
    assert row["repair_class"]["linked_unsupported_finding_ids"] == ["claim"]
    assert row["corrective_runtime_lineage"][0]["source_detail"]["class_review"] == {
        "primary_class": None
    }
