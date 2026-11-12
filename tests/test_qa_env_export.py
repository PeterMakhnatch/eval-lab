"""Teacher-data export: which runs may become rows, and why every other run is refused."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evallab.qa.env_export import ExportError, SplitGuard, executed_clean_candidates

REPO = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "d" * 64
WITNESS = "a" * 32
MODEL = "meta/muse-wonder-1.0-contributor"


def guard(**kwargs) -> SplitGuard:
    rows = [
        {"key_id": "tb4-audit-v1", "task": "terminal-bench/t-dev", "split": "dev"},
        {"key_id": "tb4-audit-v1", "task": "terminal-bench/t-test", "split": "test"},
        {"key_id": "mimo-heldout", "task": "candidate-0001-held", "split": "heldout"},
        {"key_id": "other-key", "task": "o-test", "split": "test"},
        {"key_id": "other-key", "task": "o-dev", "split": "dev"},
    ]
    return SplitGuard(assignments=rows, tb4_test_digests=["sha256:" + "e" * 64], **kwargs)


# -- split guard ------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", [
    "t-test", "terminal-bench/t-test", "terminal-bench/t-test@452bf305c6dc", "other/prefix/t-test"])
def test_guard_refuses_a_tb4_test_task_under_any_spelling(task_id):
    verdict = guard().check(task_id)
    assert not verdict.allowed and verdict.status == "refused"
    assert "tb4-audit-v1: test" in verdict.reasons[0]


def test_guard_refuses_a_tb4_test_task_by_digest_even_when_renamed():
    verdict = guard().check("renamed-task", "sha256:" + "e" * 64)
    assert not verdict.allowed and "task digest" in verdict.reasons[0]


def test_guard_refuses_heldout_ids_in_every_scope():
    for scope in ("reported", "tb4-heldout"):
        verdict = guard(scope=scope).check("candidate-0001-held")
        assert not verdict.allowed and verdict.reasons == ("mimo-heldout: heldout",)


def test_guard_scope_decides_whether_another_keys_test_split_counts():
    assert not guard(scope="reported").check("o-test").allowed
    assert guard(scope="tb4-heldout").check("o-test").allowed


def test_guard_allows_dev_and_labels_unregistered_tasks():
    assert guard().check("t-dev").status == "dev_only"
    verdict = guard().check("never-registered")
    assert verdict.allowed and verdict.status == "unregistered"


def test_guard_refuse_ids_extend_the_refusal_list():
    verdict = guard(refused_ids=["terminal-bench/t-dev@abc"]).check("t-dev")
    assert not verdict.allowed and verdict.reasons == ("refuse-ids list",)


def test_guard_rejects_an_unknown_scope():
    with pytest.raises(ExportError):
        guard(scope="everything")


def test_guard_from_repo_reads_the_real_answer_keys_and_refuses_every_tb4_test_task():
    real = SplitGuard.from_repo(REPO)
    tasks = [json.loads(line) for line in (REPO / "research/answer-keys/tb4-audit-v1/tasks.jsonl").read_text().splitlines()]
    test_tasks = [t for t in tasks if t["split"] == "test"]
    assert len(test_tasks) == 41
    for task in test_tasks:
        assert not real.check(task["subject"]["task_id"], task["subject"]["task_digest"]).allowed
    heldout_names = {
        r["task"] for r in csv.DictReader((REPO / "research/answer-keys/splits/assignments.csv").open())
        if r["split"] == "heldout"}
    assert heldout_names and all(not real.check(name).allowed for name in heldout_names)
    assert real.sources["assignments_sha256"] and real.sources["tb4_tasks_sha256"]


def test_guard_from_repo_missing_sources_fails_loudly(tmp_path):
    with pytest.raises(ExportError, match="split guard source missing"):
        SplitGuard.from_repo(tmp_path)


# -- audit session fixtures -------------------------------------------------------------


# -- seeker runs ------------------------------------------------------------------------


# -- executed-clean manifests -----------------------------------------------------------


def manifest(tmp_path: Path, members: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"dataset_id": "mimo-executed-clean-v1", "not_semantic_certification": True,
                                "members": members}))
    return path


def member(task="candidate-0002-clean", reference=(1.0, 1.0, 1.0), nop=(0.0, 0.0, 0.0), heldout=False):
    return {"task_id": task, "record_id": f"{task}@sha256:x", "cohort": "unchanged-clean-controls",
            "package_digest": "sha256:" + "1" * 64, "harbor_digest": "sha256:" + "2" * 64,
            "verifier_digest": "sha256:" + "3" * 64, "heldout_excluded_from_training": heldout,
            "controls": {"reference": list(reference), "nop": list(nop)}}


def test_strict_repeat_controls_make_a_clean_row_that_is_not_a_semantic_certificate(tmp_path):
    row, = executed_clean_candidates(manifest(tmp_path, [member()]))
    assert row.outcome == "clean" and row.target["findings"] == [] and row.target["verdict"] == "clean"
    assert row.target["not_semantic_certification"] is True
    assert row.attestation["status"] == "not_applicable"
    assert row.execution["kind"] == "executed_controls" and row.execution["confirmed"] is True


@pytest.mark.parametrize("kwargs", [{"reference": (1.0, 0.0, 1.0)}, {"nop": (0.0, 1.0, 0.0)}, {"reference": (1.0, 1.0)}])
def test_clean_requires_repeat_controls_that_all_agree(tmp_path, kwargs):
    row, = executed_clean_candidates(manifest(tmp_path, [member(**kwargs)]))
    assert row.execution["confirmed"] is False and row.execution["problems"]


def test_a_manifest_heldout_flag_or_a_guarded_task_refuses_the_clean_row(tmp_path):
    members = [member(heldout=True), member(task="candidate-0001-held"), member(task="t-test")]
    rows = list(executed_clean_candidates(manifest(tmp_path, members)))
    assert rows[0].refusals == [("split_guard", "manifest flags this package as held out")]
    assert not guard().check(rows[1].task_id, rows[1].task_digest).allowed
    assert not guard().check(rows[2].task_id, rows[2].task_digest).allowed


def test_two_clean_packages_of_one_task_are_distinct_bodies(tmp_path):
    other = member()
    other["package_digest"] = "sha256:" + "9" * 64
    first, second = executed_clean_candidates(manifest(tmp_path, [member(), other]))
    assert first.dedup_key != second.dedup_key


# -- constants that mirror private owners -----------------------------------------------


# -- failure-fact critic ----------------------------------------------------------------


UNMEASURED = {"execution_status": "unmeasured", "result": {"error": "Docker admission refused: 5347 MiB admitted; task needs 8192 MiB"}}


COMPOSE = {"execution_status": "refused", "result": {"error": "GraderRefusal: Compose tasks are unsupported on 3 hosts"}}


