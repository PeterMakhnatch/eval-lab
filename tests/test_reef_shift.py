"""Reef failure shift: deterministic failure modes over imported corpora."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.evidence import reef_intake, reef_shift

FIXTURES = Path(__file__).resolve().parent / "fixtures/reef_intake"
STEPS = FIXTURES / "steps"
ANSWERS = json.loads((FIXTURES / "shift/answers.json").read_text(encoding="utf-8"))
ARTIFACT = (
    Path(__file__).resolve().parent.parent / "research/analysis/reef-shift/exp04-reference.json"
)


def _documents() -> dict[tuple[int, str], dict]:
    documents = {}
    for entry in reef_intake.iter_gate_episodes(STEPS):
        payload = reef_intake.parse_reef_gate_episode(
            entry["episode_dir"], scenario=entry["scenario"], step=entry["step"]
        )
        documents[(entry["step"], entry["episode_dir"].name)] = payload
    return documents


def test_pass_is_not_a_failure_mode() -> None:
    documents = _documents()
    assert reef_shift.episode_flags(documents[(1, "candidate-0")], ANSWERS) == {"pass"}


def test_empty_reply_and_missing_tool_use_flagged() -> None:
    documents = _documents()
    assert reef_shift.episode_flags(documents[(1, "current-0")], ANSWERS) == {
        "turn ended on an empty reply",
        "never used a tool",
    }


def test_wrong_arguments_flagged_from_recorded_error() -> None:
    documents = _documents()
    assert reef_shift.episode_flags(documents[(2, "candidate-0")], ANSWERS) == {
        "called a tool with wrong arguments"
    }


def test_failure_counts_cover_every_failed_document() -> None:
    counts = reef_shift.failure_flag_counts(list(_documents().values()), ANSWERS)
    assert counts["failed episodes"] == 3
    assert counts["turn ended on an empty reply"] == 2
    assert counts["never used a tool"] == 2
    assert counts["called a tool with wrong arguments"] == 1


def test_shift_report_marks_agreement_and_disagreement() -> None:
    documents = list(_documents().values())
    report = reef_shift.compare_failure_shift(
        documents,
        documents,
        ANSWERS,
        reference={"seed": {"failed episodes": 3}, "check": {"failed episodes": 2}},
    )
    assert report["agreement"]["seed"]["failed episodes"] is True
    assert {
        "flag": "failed episodes",
        "reference": 2,
        "observed": 3,
    } in report["disagreements"]["check"]
    assert report["all_agree"] is False


def test_shift_report_without_reference_carries_observed_only() -> None:
    documents = list(_documents().values())
    report = reef_shift.compare_failure_shift(documents, documents, ANSWERS)
    assert report["observed"]["seed"]["failed episodes"] == 3
    assert report["reference"] == {}
    assert report["agreement"] == {}
    assert report["disagreements"] == {}
    assert report["all_agree"] is None


def test_exp04_reference_artifact_is_provenance_stamped() -> None:
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert set(artifact) == {"provenance", "answers", "reference"}
    assert "04/summary.json" in artifact["provenance"]["counts_seed"]
def test_module_cli_compares_imported_corpora(tmp_path: Path, capsys) -> None:
    seed = tmp_path / "seed"
    reef_intake.import_reef_gate_run(
        STEPS, seed, run_label="fixture", results_path=FIXTURES / "results.jsonl"
    )
    assert (
        reef_shift.main(
            ["--seed", str(seed), "--check", str(seed), "--answers", str(FIXTURES / "shift/answers.json")]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["observed"]["seed"]["failed episodes"] == 3
    assert report["all_agree"] is None
    reference_file = tmp_path / "reference.json"
    reference_file.write_text(json.dumps({"seed": {"failed episodes": 3}}), encoding="utf-8")
    assert (
        reef_shift.main(
            [
                "--seed",
                str(seed),
                "--check",
                str(seed),
                "--answers",
                str(FIXTURES / "shift/answers.json"),
                "--reference",
                str(reference_file),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["all_agree"] is False
