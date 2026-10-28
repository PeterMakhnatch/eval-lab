"""SB-1 admits only ledger-positive, cross-key-clean, source-plannable pairs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BUILDER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/sb1-clean-seeker-set-20261027/build_cohort.py"
)


@pytest.fixture
def builder():
    spec = importlib.util.spec_from_file_location("sb1_cohort_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "assignments.csv"
    path.write_text(
        "key_id,item_id,split,task,unit\n"
        "first,dataset/clean,dev,clean,\n"
        "first,dataset/overlap,dev,overlap,\n"
        "second,other/overlap@test-digest,test,overlap,\n"
        "first,namespace__alias,dev,,\n"
        "second,alias,heldout,,\n"
        "first,held,held,,\n"
    )
    return path


@pytest.mark.parametrize(
    ("task", "reason"),
    [
        ("unlisted", "a_no_affirmative_dev"),
        ("clean", None),
        ("dataset/clean@sha256:package", None),
        ("overlap", "a_cross_key_non_dev"),
        ("dataset/overlap@sha256:package", "a_cross_key_non_dev"),
        ("namespace__alias", "a_cross_key_non_dev"),
        ("held", "a_cross_key_non_dev"),
    ],
)
def test_dev_positive_and_cross_key_gate(builder, ledger, task, reason):
    assert builder.dev_gate(task, ledger) == reason


def package(tmp_path, name, marker):
    root = tmp_path / name
    (root / "tests").mkdir(parents=True)
    (root / "tests/test.sh").write_text(marker)
    (root / "task.toml").write_text('[environment]\ncpus=1\nmemory_mb=512\n')
    return str(root)


def plan():
    return {
        "schema": "evallab.candidate.plan/v1", "runnable": True,
        "executed": False, "reserved_usd": 0.0, "refusals": [],
        "modal_reservation": {"reserved_usd": 0.2},
        "resources": [{"role": "agent", "cpus": 1, "memory_mb": 512,
                       "build_timeout_sec": 600}],
    }


def test_static_plan_is_explicitly_source_only(builder, monkeypatch):
    calls = []

    def candidate(task, **kwargs):
        calls.append((task, kwargs))
        return plan()

    monkeypatch.setattr(builder, "run_candidate", candidate)
    builder.static_plan("original")
    builder.static_plan("original", port="port")
    for task, kwargs in calls:
        assert task == "original"
        assert kwargs["plan"] is True
        assert kwargs["backend"] == "modal"
        assert kwargs["allow_local_docker"] is False
        assert kwargs["timeout_seconds"] == 600
        assert not {"approval", "spend_ledger", "max_usd"} & kwargs.keys()
    assert calls[1][1]["grader"] == "port"
    assert calls[1][1]["port_task"] == "port"


def test_package_qualification_and_digest_only_evidence(builder, monkeypatch, tmp_path):
    original = package(tmp_path, "original", "original grader bytes\n")
    repaired = package(tmp_path, "repaired", "repaired grader bytes\n")
    calls = []

    def static(task, **kwargs):
        calls.append((task, kwargs))
        return {**plan(), "task": {"exact_tree_digest": builder.package_identity(task)[2]}}

    monkeypatch.setattr(builder, "static_plan", static)
    gate, reason, record = builder.evaluate_pair(builder.Pair(original, repaired))
    assert (gate, reason) == (5, "qualified")
    assert [task for task, _ in calls] == [original, repaired]
    assert record["original_grader_digest"] != record["repaired_grader_digest"]
    assert record["original_reservation_usd"] == record["repaired_reservation_usd"] == 0.2
    assert set(record["static_plan_digests"]) == {"original", "repaired"}
    assert "grader bytes" not in json.dumps(record)


def test_unchanged_grader_fails_before_planning(builder, monkeypatch, tmp_path):
    original = package(tmp_path, "original", "same\n")
    repaired = package(tmp_path, "repaired", "same\n")

    def forbidden(*args, **kwargs):
        pytest.fail("unchanged grader must fail before either static plan")

    monkeypatch.setattr(builder, "static_plan", forbidden)
    assert builder.evaluate_pair(builder.Pair(original, repaired))[1] == "c_grader_unchanged"


def test_missing_repair_is_accounted_not_materialized(builder, monkeypatch, tmp_path):
    original = package(tmp_path, "original", "original\n")
    repaired = tmp_path / "native-executor-only-child"
    assert builder.evaluate_pair(builder.Pair(original, str(repaired)))[1] == "c_repaired_missing"
    assert not repaired.exists()


def test_first_failure_is_plan_before_tier(builder, monkeypatch, tmp_path):
    original = package(tmp_path, "original", "original\n")
    repaired = package(tmp_path, "repaired", "repaired\n")
    (Path(original) / "task.toml").write_text('[environment]\ngpus=1\nmemory_mb=32768\n')
    monkeypatch.setattr(builder, "static_plan", lambda *args, **kwargs: {"error": "refused"})
    assert builder.evaluate_pair(builder.Pair(original, repaired))[1] == "d_original_plan_refused"


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [("gpus=1", "e_gpu_required"), ("memory_mb=24577", "e_memory_limit"),
     ('backend="docker"', "e_docker_only"), ("memory_mb=24576", None)],
)
def test_execution_tier_boundaries(builder, tmp_path, declaration, reason):
    (tmp_path / "task.toml").write_text(f"[environment]\n{declaration}\n")
    assert builder.tier_blocker(str(tmp_path), plan()) == reason


def test_census_is_idempotent_and_accounts_for_every_candidate(
    builder, monkeypatch, tmp_path, ledger
):
    original = package(tmp_path, "original", "original\n")
    repaired = package(tmp_path, "repaired", "repaired\n")
    assignments = tmp_path / "research/answer-keys/splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(ledger.read_text())
    pair = builder.Pair(original, repaired)

    class Sources:
        def __init__(self, *args):
            self.pairs = {
                ("example", task): {pair: {"fixture"}}
                for task in ("clean", "overlap", "unlisted")
            }
            self.inputs = {}
            self.coverage = {}

        def discover(self, cloud):
            pass

    calls = []

    def static(task, **kwargs):
        calls.append(task)
        return {**plan(), "task": {"exact_tree_digest": builder.package_identity(task)[2]}}

    monkeypatch.setattr(builder, "Sources", Sources)
    monkeypatch.setattr(builder, "static_plan", static)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "git-head\n")
    first = builder.build(tmp_path, tmp_path, tmp_path / "absent.sqlite")
    second = builder.build(tmp_path, tmp_path, tmp_path / "absent.sqlite")
    assert first == second
    cohort, census = first
    assert calls == [original, repaired] * 2  # Neither excluded task reaches the planner.
    assert census["candidate_count"] == 3
    assert census["qualified_count"] == 1
    assert sum(census["excluded_by_first_gate"].values()) == 2
    assert [row["task_id"] for row in cohort["tasks"]] == ["clean"]
    assert set(cohort["tasks"][0]["assignment_rows"][0]) == {
        "key_id", "item_id", "split", "task", "unit"
    }


def test_refusal_evidence_never_copies_source_expressions(builder):
    error = "GraderRefusal: setup-data recipe needs unsupported expression: PRIVATE BODY"
    evidence = builder.plan_failure({"error": error})
    assert evidence["refusal_category"] == "offline_setup_adapter_unsupported"
    assert "PRIVATE BODY" not in json.dumps(evidence)


def test_plan_identity_must_match_the_recorded_source(builder, monkeypatch, tmp_path):
    original = package(tmp_path, "original", "original\n")
    repaired = package(tmp_path, "repaired", "repaired\n")
    monkeypatch.setattr(
        builder, "static_plan",
        lambda *args, **kwargs: {**plan(), "task": {"exact_tree_digest": "changed-source"}},
    )
    gate, reason, details = builder.evaluate_pair(builder.Pair(original, repaired))
    assert (gate, reason) == (3, "d_original_plan_refused")
    assert details["refusal_category"] == "source_drift"
