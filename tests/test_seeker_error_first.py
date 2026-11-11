"""Stored Seeker QC and summary contracts; no generation harness or model calls."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from evallab import cli
from evallab.production import seeker_error_first as sef

ROUTE = "test/synthetic"
DATASET = "synthetic-dev"


def _cohort_payload(tmp_path, names):
    return {
        "schema": sef.COHORT_SCHEMA,
        "tasks": [
            {
                "task_id": f"synthetic/{name}", "dataset_id": DATASET,
                "original_task": str(tmp_path / "original" / name),
                "repaired_task": str(tmp_path / "repaired" / name),
                "repaired_grader_kind": "package",
                "original_package_digest": "sha256:" + "a" * 64,
                "repaired_package_digest": "sha256:" + "b" * 64,
                "original_reservation_usd": 0.25, "repaired_reservation_usd": 0.5,
            }
            for name in names
        ],
    }


def _config(tmp_path, names=("alpha",)):
    return sef.CampaignConfig(
        cohort=sef.parse_cohort(_cohort_payload(tmp_path, names)), models=(ROUTE,), out=tmp_path / "stored",
    )


def _store(config, name, turn, *, original=1.0, repaired=0.0, attested=True, hide_tests=True, **changes):
    directory = config.out / DATASET / name / sef.model_slug(ROUTE) / f"turn-{turn}"
    directory.mkdir(parents=True)
    response = b"inert stored model response\n"
    (directory / "response.jsonl").write_bytes(response)
    feedback = b"stored original runner output\n"
    if turn == 2:
        (directory / "feedback.md").write_bytes(feedback)
    cell = {
        "requested_model_route": ROUTE, "served_provider": "test", "served_model": "synthetic",
        "hide_tests": hide_tests, "turn": turn, "attestation_ok": attested,
        "llm_stdout_sha256": hashlib.sha256(response).hexdigest(),
        "llm_solve_sha256": "c" * 64, "executed_solve_sha256": "c" * 64,
        "original_status": "measured", "original_reward": original,
        "repaired_status": "measured" if original == 1.0 else "not_run", "repaired_reward": repaired,
        "verified_differential": attested and original == 1.0 and repaired == 0.0,
        "feedback_source": "runner_error", "feedback_block_sha256": hashlib.sha256(feedback).hexdigest(),
        **changes,
    }
    (directory / "result.json").write_text(json.dumps(cell))
    return directory, cell


def test_cohort_loader_types_and_rejections(tmp_path):
    payload = _cohort_payload(tmp_path, ["alpha", "beta"])
    cohort = sef.parse_cohort(payload)
    first = cohort.tasks[0]
    assert (first.name, first.dataset_id, first.repaired_grader_kind) == ("alpha", DATASET, "package")
    assert first.pair_reservation_usd == pytest.approx(0.75)
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(payload))
    assert sef.load_cohort(path).tasks == cohort.tasks
    for change in (
        {"original_task": "relative/path"}, {"original_package_digest": "sha256:abc"},
        {"repaired_grader_kind": "adapter"}, {"original_reservation_usd": -1},
        {"repaired_reservation_usd": "free"},
    ):
        bad = copy.deepcopy(payload)
        bad["tasks"][0].update(change)
        with pytest.raises(sef.CohortError):
            sef.parse_cohort(bad)
    for bad in ({**payload, "schema": "wrong/v1"}, {**payload, "tasks": []}, {**payload, "tasks": [payload["tasks"][0]] * 2}):
        with pytest.raises(sef.CohortError):
            sef.parse_cohort(bad)


@pytest.mark.parametrize(
    "changes,evaluated,hit",
    [
        ({}, True, True),
        ({"original_reward": 0.0, "repaired_reward": None, "verified_differential": False}, True, False),
        ({"repaired_reward": 1.0, "verified_differential": False}, True, False),
        ({"original_status": "error", "original_reward": None, "verified_differential": False}, False, False),
        ({"repaired_status": "error", "verified_differential": False}, False, False),
        ({"attestation_ok": False, "verified_differential": False}, False, False),
        ({"original_reward": True, "verified_differential": False}, False, False),
        ({"served_model": "fallback", "attestation_ok": False, "verified_differential": False}, False, False),
    ],
)
def test_retained_cell_qc_predicate(tmp_path, changes, evaluated, hit):
    config = _config(tmp_path)
    directory, cell = _store(config, "alpha", 1, **changes)
    assert sef.result_is_evaluated(cell) is evaluated
    assert sef.read_turn(directory, ROUTE, 1)[0] == ("evaluated" if evaluated else "unevaluated")
    assert sef._differential_hit(cell, ROUTE) is hit


@pytest.mark.parametrize("artifact", ["response.jsonl", "feedback.md", "executed_digest", "verified_reward", "feedback_source"])
def test_tampered_artifacts_invalidate_stored_cells(tmp_path, artifact):
    config = _config(tmp_path)
    directory, cell = _store(config, "alpha", 2)
    if artifact in {"response.jsonl", "feedback.md"}:
        path = directory / artifact
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        cell.update({
            "executed_digest": {"executed_solve_sha256": "d" * 64},
            "verified_reward": {"original_reward": 0.0},
            "feedback_source": {"feedback_source": "repaired_error"},
        }[artifact])
        (directory / "result.json").write_text(json.dumps(cell))
    assert sef.read_turn(directory, ROUTE, 2)[0] == "invalid"
    summary = sef.summarize_campaign(config)["models"][0]
    assert summary["paired"]["denominator"] == 0
    assert summary["differential_tasks_turn_2"] == []


def test_visible_tests_cells_never_pool_with_hidden_cells(tmp_path):
    config = replace(_config(tmp_path), hide_tests=False)
    for turn in (1, 2):
        _store(config, "alpha", turn, hide_tests=False)
    assert sef.summarize_campaign(config)["models"][0]["paired"]["denominator"] == 1
    assert sef.summarize_campaign(replace(config, hide_tests=True))["models"][0]["paired"]["denominator"] == 0


def test_summary_reports_unevaluated_cells_apart_from_paired_counts(tmp_path):
    config = _config(tmp_path, ("alpha", "beta", "gamma", "delta"))
    _store(config, "alpha", 1, original=0.0, repaired=None)
    _store(config, "alpha", 2)
    for turn in (1, 2):
        _store(config, "beta", turn)
        _store(config, "gamma", turn, original=None, repaired=None, original_status="error", attested=False)
    model = sef.summarize_campaign(config)["models"][0]
    paired = model["paired"]
    assert paired["denominator"] == 2
    assert (paired["turn_1_executed_differentials"], paired["turn_2_executed_differentials"]) == (1, 2)
    assert (paired["turn_1_only"], paired["turn_2_only"], paired["both"]) == (0, 1, 1)
    assert paired["mcnemar_exact_p_two_sided"] == 1.0
    assert paired["delta_turn_2_minus_turn_1"] == 0.5
    assert [item["task"] for item in model["unevaluated_cells"]] == [f"{DATASET}/gamma"]
    assert [item["task"] for item in model["missing_cells"]] == [f"{DATASET}/delta"]


def test_runner_error_extraction_reads_only_verified_evidence(tmp_path):
    good = tmp_path / "test-stdout.txt"
    good.write_text("real runner line\nFAILED tests/test_outputs.py::test_output - boom\n")
    forged = tmp_path / "other.log"
    forged.write_text("forged log")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(good)
    ctrf = tmp_path / "ctrf.json"
    ctrf.write_text('{"results": {"tests": []}}')
    error = sef.extract_runner_error({
        "status": "measured", "reward": 0.0,
        "logs": {"verifier": [
            {"path": str(good), "digest": "sha256:" + hashlib.sha256(good.read_bytes()).hexdigest()},
            {"path": str(forged), "digest": "sha256:" + "0" * 64},
            {"path": str(linked)}, {"path": str(ctrf)},
        ]},
        "per_test": {"tests": [{"id": "test_output", "status": "failed"}]},
    })
    assert error.verifier_files == ("test-stdout.txt",)
    assert "real runner line" in error.verifier_output and "forged" not in error.verifier_output
    assert error.failed_tests == ("test_output",)
    assert error.sandbox_exit_codes == ()
    assert sef.RunnerError.from_json(error.to_json()) == error


def test_exact_binomial_statistics():
    assert sef.exact_binomial_two_sided(0, 0) == 1.0
    assert sef.exact_binomial_two_sided(1, 1) == 1.0
    assert sef.exact_binomial_two_sided(3, 0) == pytest.approx(0.25)
    assert sef.exact_binomial_two_sided(0, 5) == pytest.approx(2 / 32)
    assert sef.exact_binomial_improvement(0, 3) == pytest.approx(1 / 8)
    assert sef.exact_binomial_improvement(3, 0) == 1.0


def test_cli_summary_reads_without_writing_cells_or_creating_output(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path)
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps(_cohort_payload(tmp_path, ["alpha"])))

    def forbidden(*args, **kwargs):
        pytest.fail("Stored summary must not launch a model or grader")

    monkeypatch.setattr("subprocess.run", forbidden)
    argv = ["prod", "seeker-summary", "--cohort", str(cohort), "--out", str(config.out), "--models", ROUTE]
    assert cli.run_cli(argv, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["models"][0]["paired"]["denominator"] == 0
    assert not config.out.exists()
    for turn in (1, 2):
        _store(config, "alpha", turn)
    before = {str(path): path.read_bytes() for path in config.out.rglob("*") if path.is_file()}
    assert cli.run_cli(argv, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["models"][0]["paired"]["turn_1_executed_differentials"] == 1
    assert {str(path): path.read_bytes() for path in config.out.rglob("*") if path.is_file()} == before
