"""Contracts for the BUILDER authoring pipeline (WS-C)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.authoring import (
    LEDGER_SCHEMA,
    REGISTER_REFUSAL,
    SEED_CLASS_PASS_RATE_SQL,
    AuthoringError,
    AuthoringPipeline,
    QualificationRecord,
    RegisterRefusal,
    StructuralControlRunner,
    bump_version,
    find_craft_gap,
    load_ledger,
    main,
    seed_class_pass_rates,
    upsert_ledger,
    write_ledger,
)

FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class SequencedIds:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"prop{self.n:02d}"


def write_task(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    instruction: str = "Summarize the input.\n",
    separate: bool = True,
) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True)
    mode = 'environment_mode = "separate"' if separate else ""
    (task_dir / "task.toml").write_text(
        f"""schema_version = "1.0"

[task]
name = "{name}"
version = "{version}"

[verifier]
timeout_sec = 60.0
{mode}
"""
    )
    (task_dir / "instruction.md").write_text(instruction)
    environment = task_dir / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (environment / "input.txt").write_text("payload\n")
    tests = task_dir / "tests"
    tests.mkdir()
    (tests / "test_outputs.py").write_text("def test_placeholder() -> None:\n    assert True\n")
    solution = task_dir / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/bash\necho ok\n")
    return task_dir


def write_scenario(repo: Path, stem: str = "gap-notes", body: str | None = None) -> Path:
    path = repo / "research" / "scenarios" / f"{stem}.md"
    path.parent.mkdir(parents=True)
    path.write_text(body or f"# {stem}\n\nA research scenario about hidden verifiers.\n")
    return path


def write_craft_parquet(path: Path, covered: list[tuple[str, bool, bool]] | None = None) -> Path:
    rows = covered or [("pytest", False, False)]
    table = pa.table(
        {
            "verifier_type": [row[0] for row in rows],
            "env_multi_container": [row[1] for row in rows],
            "pinned_deps": [row[2] for row in rows],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    write_task(repo / "library" / "tasks", "event-summary")
    write_scenario(repo)
    (repo / "library" / "registry").mkdir(parents=True)
    (repo / "library" / "registry" / "event-summary.json").write_text(
        json.dumps(
            {
                "task_id": "event-summary",
                "task_path": "library/tasks/event-summary",
                "state": "registered",
            }
        )
    )
    write_craft_parquet(repo / "derived" / "parquet" / "craft" / "craft.parquet")
    return repo


def pipeline_for(repo: Path) -> AuthoringPipeline:
    return AuthoringPipeline(
        repo,
        derived_root=repo / "derived" / "parquet",
        runner=StructuralControlRunner(),
        now=lambda: FIXED_NOW,
        new_id=SequencedIds(),
    )


def test_bump_version_never_returns_source() -> None:
    assert bump_version("1.0.0") == "1.0.1"
    assert bump_version("2.3") == "2.4.0"
    assert bump_version(None) == "0.1.0"
    assert bump_version("nightly") == "nightly-proposed"


def test_propose_mutation_is_a_new_version_and_leaves_source(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    source = repo / "library" / "tasks" / "event-summary"
    before = (source / "task.toml").read_text()
    pipe = pipeline_for(repo)

    proposal = pipe.propose("mutation", ref="event-summary")

    assert proposal.seed_class == "mutation"
    assert proposal.ref_task == "event-summary"
    assert proposal.version == "1.0.1"
    assert proposal.outcome == "proposed"
    assert (proposal.path / "task.toml").is_file()
    assert 'version = "1.0.1"' in (proposal.path / "task.toml").read_text()
    assert (source / "task.toml").read_text() == before
    assert proposal.path == repo / "library" / "tasks" / "_proposed" / "prop01"
    record = pipe.records()[0]
    assert record.outcome == "proposed"
    assert record.seed_class == "mutation"
    assert record.ref_task == "event-summary"


def test_propose_scenario_seeds_from_research(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    proposal = pipe.propose("scenario", ref="gap-notes")

    assert proposal.seed_class == "scenario"
    assert proposal.scenario_path == "research/scenarios/gap-notes.md"
    instruction = (proposal.path / "instruction.md").read_text()
    assert "research/scenarios/gap-notes.md" in instruction
    assert "hidden verifiers" in instruction
    assert (proposal.path / "tests" / "test_proposed.py").is_file()


def test_propose_craft_gap_targets_zero_coverage_facet(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    proposal = pipe.propose("craft-gap")

    assert proposal.seed_class == "craft-gap"
    assert proposal.target_facets == {
        "verifier_type": "pytest",
        "env_multi_container": False,
        "pinned_deps": True,
    }
    instruction = (proposal.path / "instruction.md").read_text()
    assert "pinned_deps: `True`" in instruction


def test_find_craft_gap_is_stable(tmp_path: Path) -> None:
    path = write_craft_parquet(
        tmp_path / "craft.parquet",
        [("pytest", False, False), ("pytest", False, True)],
    )
    assert find_craft_gap(path) == {
        "verifier_type": "pytest",
        "env_multi_container": True,
        "pinned_deps": False,
    }


def test_battery_records_four_bools_and_evidence(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("mutation", ref="event-summary")

    report = pipe.run_battery(proposal.proposal_id)

    assert report.all_passed is True
    assert report.outcome == "battery_passed"
    assert [item.check for item in report.checks] == [
        "oracle",
        "nop",
        "fair_oracle",
        "adversarial",
    ]
    assert [item.passed for item in report.checks] == [True, True, True, True]
    assert report.checks[0].reward == 1.0
    assert report.checks[1].reward == 0.0
    assert report.checks[1].attempts == 2
    assert report.checks[2].agent == "fair-oracle"
    assert report.checks[3].reward == 0.0
    record = pipe.records()[0]
    assert record.battery_oracle is True
    assert record.battery_nop is True
    assert record.battery_fair_oracle is True
    assert record.battery_adversarial is True
    assert len(record.evidence_paths) == 4
    assert all(Path(path).is_file() for path in record.evidence_paths)
    assert record.outcome == "battery_passed"


def test_battery_fails_when_solution_is_missing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("mutation", ref="event-summary")
    for child in (proposal.path / "solution").iterdir():
        child.unlink()
    (proposal.path / "solution").rmdir()

    report = pipe.run_battery(proposal.proposal_id)

    assert report.all_passed is False
    assert report.checks[0].passed is False
    assert pipe.records()[0].battery_oracle is False
    assert pipe.records()[0].outcome == "proposed"


def test_review_writes_score_and_advances_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("scenario", ref="gap-notes")
    pipe.run_battery(proposal.proposal_id)

    report = pipe.review(proposal.proposal_id)

    assert report.outcome == "craft_reviewed"
    assert report.score == 1.0
    assert report.reasons
    assert Path(report.evidence_path).is_file()
    record = pipe.records()[0]
    assert record.review_score == 1.0
    assert record.outcome == "craft_reviewed"
    assert load_proposal_outcome(proposal.path) == "craft_reviewed"


def load_proposal_outcome(path: Path) -> str:
    return json.loads((path / "proposal.json").read_text())["outcome"]


def test_review_refuses_before_battery(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("mutation", ref="event-summary")
    with pytest.raises(AuthoringError, match="battery_passed"):
        pipe.review(proposal.proposal_id)


def test_ledger_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "qualification" / "ledger.parquet"
    first = QualificationRecord(
        proposal_id="alpha",
        seed_class="mutation",
        ref_task="event-summary",
        battery_oracle=True,
        battery_nop=True,
        battery_fair_oracle=False,
        battery_adversarial=True,
        evidence_paths=["a.json", "b.json"],
        review_score=0.5,
        outcome="proposed",
        created_at="2026-08-16T12:00:00Z",
        updated_at="2026-08-16T12:00:00Z",
    )
    second = QualificationRecord(
        proposal_id="beta",
        seed_class="scenario",
        outcome="craft_reviewed",
        created_at="2026-08-16T12:00:00Z",
        updated_at="2026-08-16T13:00:00Z",
    )
    write_ledger(path, [second, first])
    loaded = load_ledger(path)
    assert [record.proposal_id for record in loaded] == ["alpha", "beta"]
    assert loaded[0] == first
    assert loaded[1].battery_oracle is None
    assert loaded[1].evidence_paths == []
    table = pq.read_table(path)
    assert table.schema.equals(LEDGER_SCHEMA)


def test_seed_class_pass_rate_is_one_duckdb_query(tmp_path: Path) -> None:
    path = tmp_path / "ledger.parquet"
    rows = [
        QualificationRecord(
            proposal_id="m1",
            seed_class="mutation",
            battery_oracle=True,
            battery_nop=True,
            battery_fair_oracle=True,
            battery_adversarial=True,
            outcome="craft_reviewed",
            created_at="2026-08-16T12:00:00Z",
            updated_at="2026-08-16T12:00:00Z",
        ),
        QualificationRecord(
            proposal_id="m2",
            seed_class="mutation",
            battery_oracle=True,
            battery_nop=False,
            battery_fair_oracle=True,
            battery_adversarial=True,
            outcome="proposed",
            created_at="2026-08-16T12:00:00Z",
            updated_at="2026-08-16T12:00:00Z",
        ),
        QualificationRecord(
            proposal_id="s1",
            seed_class="scenario",
            battery_oracle=True,
            battery_nop=True,
            battery_fair_oracle=True,
            battery_adversarial=True,
            outcome="battery_passed",
            created_at="2026-08-16T12:00:00Z",
            updated_at="2026-08-16T12:00:00Z",
        ),
    ]
    write_ledger(path, rows)
    rates = {seed: (rate, n) for seed, rate, n in seed_class_pass_rates(path)}
    assert rates["mutation"] == (0.5, 2)
    assert rates["scenario"] == (1.0, 1)
    assert "FROM read_parquet($ledger)" in SEED_CLASS_PASS_RATE_SQL
    assert "GROUP BY 1" in SEED_CLASS_PASS_RATE_SQL


def test_automation_cannot_register(tmp_path: Path) -> None:
    """Fail-closed: authoring refuses to write the registered outcome."""
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("mutation", ref="event-summary")
    pipe.run_battery(proposal.proposal_id)
    pipe.review(proposal.proposal_id)

    with pytest.raises(RegisterRefusal, match="human-only") as raised:
        pipe.register(proposal.proposal_id)
    assert REGISTER_REFUSAL in str(raised.value)
    assert raised.value.proposal_id == proposal.proposal_id
    assert raised.value.outcome == "craft_reviewed"

    record = pipe.records()[0]
    poisoned = record.model_copy(update={"outcome": "registered"})
    with pytest.raises(RegisterRefusal, match="human-only"):
        upsert_ledger(pipe.ledger, poisoned)

    reloaded = load_ledger(pipe.ledger)
    assert reloaded[0].outcome == "craft_reviewed"
    registry_after = {path.name for path in (repo / "library" / "registry").glob("*.json")}
    assert registry_after == {"event-summary.json"}
    assert load_proposal_outcome(proposal.path) == "craft_reviewed"


def test_five_proposal_batch_halts_at_human_gate(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    items = pipe.run_batch(5)

    assert len(items) == 5
    assert [item.seed_class for item in items] == [
        "mutation",
        "scenario",
        "craft-gap",
        "mutation",
        "scenario",
    ]
    assert {item.outcome for item in items} == {"craft_reviewed"}
    records = pipe.records()
    assert len(records) == 5
    assert all(record.outcome == "craft_reviewed" for record in records)
    assert all(record.review_score is not None for record in records)
    with pytest.raises(RegisterRefusal):
        pipe.register(items[0].proposal_id)
    assert all(record.outcome != "registered" for record in load_ledger(pipe.ledger))


def test_cli_propose_battery_review_and_register_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    common = ["--root", str(repo), "--out", str(derived), "--json"]

    assert main([*common, "propose", "--seed", "mutation", "--ref", "event-summary"]) == 0
    proposed = json.loads(capsys.readouterr().out)
    proposal_id = proposed["proposal_id"]
    assert proposed["outcome"] == "proposed"

    assert main([*common, "battery", proposal_id]) == 0
    battery = json.loads(capsys.readouterr().out)
    assert battery["all_passed"] is True
    assert battery["outcome"] == "battery_passed"

    assert main([*common, "review", proposal_id]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["outcome"] == "craft_reviewed"

    assert main([*common, "register", proposal_id]) == 2
    captured = capsys.readouterr()
    assert REGISTER_REFUSAL in captured.err
    assert "human" in captured.err


def test_cli_batch_five(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    assert (
        main(
            [
                "--root",
                str(repo),
                "--out",
                str(derived),
                "--json",
                "batch",
                "--count",
                "5",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["count"] == 5
    assert payload["outcome"] == "craft_reviewed"
    assert payload["halt"] == REGISTER_REFUSAL
    assert REGISTER_REFUSAL in captured.err
    assert all(item["outcome"] == "craft_reviewed" for item in payload["proposals"])
