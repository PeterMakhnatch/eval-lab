"""Contracts for the BUILDER authoring pipeline (WS-C)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.authoring import (
    LEDGER_SCHEMA,
    MODEL_PROVENANCE_SCHEMA_VERSION,
    MODEL_SPEC_RESPONSE_SCHEMA,
    REGISTER_REFUSAL,
    SEED_CLASS_PASS_RATE_SQL,
    AuthoringError,
    AuthoringPipeline,
    QualificationRecord,
    RegisterRefusal,
    StructuralControlRunner,
    bump_version,
    design_novel_spec,
    find_all_craft_gaps,
    find_craft_gap,
    generate_stub_task,
    load_all_axes,
    load_axis,
    load_ledger,
    main,
    reexecute_inversion_analysis,
    sample_spec_batch,
    seed_class_pass_rates,
    spec_coordinate_key,
    upsert_ledger,
    verify_inversion_reproducibility,
    write_ledger,
)
from evallab.facts import AnalyzerCallResult
from evallab.lineage import read_artifact_inputs, resolve_lineage
from evallab.modeladapter import (
    ModelAdapterExecutionError,
    ModelAdapterResult,
    ModelAdapterTimeoutError,
)
from evallab.paths import derived_root_from_environment
from evallab.schemas import ProposalAxes, ProposalSpec

# Completeness checker from meta-task package
_checker_dir = Path(__file__).resolve().parent.parent / "library/meta/synthesize-task@1/tests"
if str(_checker_dir) not in sys.path:
    sys.path.insert(0, str(_checker_dir))

from completeness_checker import (  # noqa: E402
    check_no_answer_leakage,
    check_oracle_solution_runs,
    check_package_structure,
    check_task_completeness,
    check_task_tests_pass,
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
    (repo / "library" / "registry").mkdir(parents=True, exist_ok=True)
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
    policy_dir = repo / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "standing-approvals.yaml").write_text(
        "version: 1\n"
        "daily_cost_ceiling_usd: 20\n"
        "per_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "refuse_billable_at_used_percent: null\n"
        "auto_run:\n"
        "  - name: local-controls\n"
        "    agents: [oracle, nop]\n"
        "escalate_to_human:\n"
        "  - any_billable_agent\n"
    )
    (repo / "queue").mkdir(parents=True, exist_ok=True)
    meta_src = Path(__file__).resolve().parents[1] / "library/meta/synthesize-task@1"
    if meta_src.is_dir():
        meta_dest = repo / "library/meta/synthesize-task@1"
        meta_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(meta_src, meta_dest, dirs_exist_ok=True)
    tmpl_src = Path(__file__).resolve().parents[1] / "authoring/templates"
    if tmpl_src.is_dir():
        tmpl_dest = repo / "authoring/templates"
        tmpl_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmpl_src, tmpl_dest, dirs_exist_ok=True)
    return repo


def pipeline_for(repo: Path, *, adapter: Any = None) -> AuthoringPipeline:
    return AuthoringPipeline(
        repo,
        derived_root=repo / "derived" / "parquet",
        runner=StructuralControlRunner(),
        adapter=adapter,
        now=lambda: FIXED_NOW,
        new_id=SequencedIds(),
    )


class FakeDesignerAdapter:
    model = "fake-pinned-model-2026-08-19"
    transport = "fake"

    def __init__(
        self,
        output: str | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.output = output or json.dumps(
            {
                "schema_version": "spec/1",
                "name": "model-task",
                "category": "systems-programming",
                "scenario": "incident-emergency",
                "difficulty": "advanced",
                "summary": "A model-authored proposal.",
                "seed_class": "scenario",
                "axes": {
                    "category": "systems-programming",
                    "scenario": "incident-emergency",
                    "difficulty": "advanced",
                },
            }
        )
        self.error = error
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def __call__(self, prompt: str, schema: dict[str, Any]) -> AnalyzerCallResult:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.error is not None:
            raise self.error
        return ModelAdapterResult(
            raw_output=self.output,
            model=self.model,
            transport=self.transport,
        )




def test_model_adapter_proposal_reaches_quarantine_with_complete_provenance(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    adapter = FakeDesignerAdapter()
    proposal = pipeline_for(repo, adapter=adapter).propose_model("incident", "formal")

    manifest = json.loads((proposal.path / "proposal.json").read_text())
    provenance = manifest["provenance"]
    assert proposal.outcome == "proposed"
    assert (proposal.path / "task.toml").is_file()
    assert manifest["injected_spec"]["schema_version"] == "spec/1"
    assert provenance["schema_version"] == MODEL_PROVENANCE_SCHEMA_VERSION
    assert provenance["spec_schema_version"] == "spec/1"
    assert provenance["model"] == adapter.model
    assert provenance["transport"] == adapter.transport
    assert provenance["prompt_sha256"] == __import__("hashlib").sha256(
        adapter.prompts[0].encode("utf-8")
    ).hexdigest()
    assert provenance["raw_output_sha256"] == __import__("hashlib").sha256(
        adapter.output.encode("utf-8")
    ).hexdigest()
    assert adapter.schemas == [MODEL_SPEC_RESPONSE_SCHEMA]
    assert "chain-of-thought" in adapter.prompts[0]
    assert "raw_output" not in manifest


@pytest.mark.parametrize("failure", ["malformed", "schema", "timeout", "exit"])
def test_model_designer_failures_leave_no_partial_proposal(
    tmp_path: Path,
    failure: str,
) -> None:
    repo = make_repo(tmp_path)
    if failure == "malformed":
        adapter = FakeDesignerAdapter("{")
    elif failure == "schema":
        adapter = FakeDesignerAdapter(json.dumps({"schema_version": "wrong"}))
    elif failure == "timeout":
        adapter = FakeDesignerAdapter(
            error=ModelAdapterTimeoutError("timed out", timeout=1.0, argv=["fake"])
        )
    else:
        adapter = FakeDesignerAdapter(
            error=ModelAdapterExecutionError(
                "failed", returncode=7, argv=["fake"], stderr="failed"
            )
        )

    pipe = pipeline_for(repo, adapter=adapter)
    with pytest.raises((AuthoringError, ModelAdapterTimeoutError, ModelAdapterExecutionError)):
        pipe.propose_model("incident", "formal")
    quarantine = repo / "library" / "tasks" / "_proposed"
    assert not quarantine.exists() or not list(quarantine.iterdir())
    assert pipe.records() == []


def test_model_designer_duplicate_coordinates_are_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo, adapter=FakeDesignerAdapter())
    first = pipe.propose_model("incident", "formal")

    with pytest.raises(AuthoringError, match="duplicate"):
        pipe.propose_model("different-topic", "different-style")

    assert [record.proposal_id for record in pipe.records()] == [first.proposal_id]
    assert sorted(path.name for path in pipe.quarantine.iterdir()) == [first.proposal_id]
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
        "inversion",
        "mutation",
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


def test_completeness_checker_rejects_missing_structure(tmp_path: Path) -> None:
    task_dir = tmp_path / "broken_task"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})
    (task_dir / "solution/solve.sh").unlink()

    res = check_package_structure(task_dir)
    assert res["passed"] is False
    assert "solution/solve.sh" in res["message"]

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["package_structure"]["passed"] is False


def test_completeness_checker_rejects_broken_oracle_solution(tmp_path: Path) -> None:
    task_dir = tmp_path / "broken_oracle_task"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})
    (task_dir / "solution/solve.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (task_dir / "solution/solve.py").unlink(missing_ok=True)

    res = check_oracle_solution_runs(task_dir)
    assert res["passed"] is False

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["package_structure"]["passed"] is True
    assert overall["checks"]["oracle_solution_runs"]["passed"] is False


def test_completeness_checker_rejects_failing_task_tests(tmp_path: Path) -> None:
    task_dir = tmp_path / "failing_tests_task"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})
    (task_dir / "tests/test.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (task_dir / "tests/verify.py").unlink(missing_ok=True)

    res = check_task_tests_pass(task_dir)
    assert res["passed"] is False

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["package_structure"]["passed"] is True
    assert overall["checks"]["oracle_solution_runs"]["passed"] is True
    assert overall["checks"]["task_tests_pass"]["passed"] is False


def test_completeness_checker_rejects_planted_answer_leak_in_answer_file(tmp_path: Path) -> None:
    task_dir = tmp_path / "leaking_task_answer_file"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})

    # Plant ANSWER.txt inside environment/ containing expected output
    (task_dir / "environment/ANSWER.txt").write_text("expected_output: 42\n")

    res = check_no_answer_leakage(task_dir)
    assert res["passed"] is False
    assert any("ANSWER.txt" in leak for leak in res["leaks"])

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["no_answer_leakage"]["passed"] is False


def test_completeness_checker_rejects_planted_answer_in_innocuous_file(tmp_path: Path) -> None:
    task_dir = tmp_path / "leaking_task_innocuous_file"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})

    # Plant answer data in innocuously named files
    (task_dir / "environment/notes.md").write_text(
        '{"schema_version": 1, "total_records": 3, "status": "ok"}\n'
    )
    (task_dir / "environment/data_2.json").write_text('{"total_records": 3, "status": "ok"}\n')

    res = check_no_answer_leakage(task_dir)
    assert res["passed"] is False
    assert any("notes.md" in leak or "data_2.json" in leak for leak in res["leaks"])

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["no_answer_leakage"]["passed"] is False


def test_completeness_checker_rejects_verbatim_oracle_solution_in_visible_file(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "leaking_task_solution_span"
    generate_stub_task(task_dir, {"name": "test-task", "category": "data-processing"})

    # Copy verbatim solution span into environment/notes.md
    sol_span = (
        'summary = {\n    "schema_version": 1,\n'
        '    "total_records": len(data),\n    "status": "ok",\n}\n'
    )
    (task_dir / "environment/notes.md").write_text(sol_span)

    res = check_no_answer_leakage(task_dir)
    assert res["passed"] is False
    assert any("notes.md" in leak for leak in res["leaks"])

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is False
    assert overall["checks"]["no_answer_leakage"]["passed"] is False


def test_completeness_checker_accepts_clean_task_with_legitimate_instructions(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "clean_task"
    generate_stub_task(task_dir, {"name": "clean-task", "category": "data-processing"})

    res = check_no_answer_leakage(task_dir)
    assert res["passed"] is True
    assert res["leaks"] == []

    overall = check_task_completeness(task_dir)
    assert overall["passed"] is True
    assert overall["checks"]["no_answer_leakage"]["passed"] is True


def test_completeness_checker_rejects_structural_leakage(tmp_path: Path) -> None:
    # 1. Hidden solution directory under environment/
    task_dir_dir = tmp_path / "struct_dir_task"
    generate_stub_task(task_dir_dir, {"name": "struct-task", "category": "data-processing"})
    (task_dir_dir / "environment/solution").mkdir(parents=True)
    (task_dir_dir / "environment/solution/solve.py").write_text("print('leak')\n")

    res_dir = check_no_answer_leakage(task_dir_dir)
    assert res_dir["passed"] is False
    assert any("solution" in leak for leak in res_dir["leaks"])

    # 2. Dockerfile COPY instruction copying hidden directories
    task_dir_df = tmp_path / "struct_df_task"
    generate_stub_task(task_dir_df, {"name": "struct-df-task", "category": "data-processing"})
    (task_dir_df / "environment/Dockerfile").write_text(
        "FROM python:3.13-slim-bookworm\nCOPY solution /app/solution\n"
    )

    res_df = check_no_answer_leakage(task_dir_df)
    assert res_df["passed"] is False
    assert any("Dockerfile" in leak for leak in res_df["leaks"])


def test_propose_via_harbor_submits_craft_purpose_without_dispatch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    # Free agent: oracle
    exp_spec, queue_path, decision = pipe.propose(
        seed="craft-gap",
        via_harbor=True,
        agent="oracle",
    )
    assert exp_spec.purpose == "craft"
    assert decision.admitted is True
    assert queue_path.parent.name == "approved"
    assert queue_path.is_file()
    assert not (repo / "runs").exists()

    # Billable agent: codex (refused without recorded authorization)
    exp_spec_paid, queue_path_paid, decision_paid = pipe.propose(
        seed="craft-gap",
        via_harbor=True,
        agent="codex",
    )
    assert exp_spec_paid.purpose == "craft"
    assert decision_paid.admitted is False
    assert decision_paid.reason_code == "paid_run_unauthorized"
    assert queue_path_paid.parent.name == "waiting"
    assert queue_path_paid.is_file()
    # Still submit-only: no runs/ directory created
    assert not (repo / "runs").exists()


def test_harvest_refuses_package_when_completeness_checker_failed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    job_dir = repo / "runs" / "failed-synth-job"
    task_pkg = job_dir / "artifacts" / "output" / "task"
    generate_stub_task(task_pkg, {"name": "synth-failed"})

    # Break the task in the job artifacts
    (task_pkg / "solution/solve.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    verifier_dir = job_dir / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward.json").write_text(json.dumps({"reward": 0.0}))

    with pytest.raises(AuthoringError) as exc_info:
        pipe.harvest("failed-synth-job")
    assert "completeness checker" in str(exc_info.value).lower()
    assert not pipe.quarantine.exists() or not list(pipe.quarantine.iterdir())


def test_harvested_proposal_carries_inputs_and_lineage_resolves(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    job_dir = repo / "runs" / "synth-job-01"
    task_pkg = job_dir / "artifacts" / "output" / "task"
    generate_stub_task(task_pkg, {"name": "synth-success"})
    (job_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": "synth-job-01",
                "spec": {"name": "synth-success", "seed_class": "craft-gap"},
                "exemplar": "event-summary",
            }
        )
    )
    (job_dir / "result.json").write_text(json.dumps({"passed": True, "status": "completed"}))
    verifier_dir = job_dir / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward.json").write_text(json.dumps({"reward": 1.0}))

    proposal = pipe.harvest("synth-job-01")
    assert proposal.outcome == "proposed"
    assert proposal.job_id == "synth-job-01"
    assert proposal.inputs is not None
    assert len(proposal.inputs) >= 1

    manifest = json.loads((proposal.path / "proposal.json").read_text())
    assert "inputs" in manifest

    node = resolve_lineage(str(proposal.path / "proposal.json"), repo_root=repo)
    assert node.resolved is True
    assert len(node.inputs) >= 1
    # Job artifact is in Zone 1 (runs/)
    assert any(inp.zone == "z1" for inp in node.inputs)


def test_end_to_end_stub_loop_propose_checker_harvest_battery(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    # 1. Propose via harbor
    exp_spec, queue_path, decision = pipe.propose(
        seed="craft-gap",
        via_harbor=True,
        agent="oracle",
    )
    assert decision.admitted is True
    assert exp_spec.purpose == "craft"

    # 2. Simulate stub generator execution
    job_dir = repo / "runs" / exp_spec.name
    task_pkg = job_dir / "artifacts" / "output" / "task"
    generate_stub_task(task_pkg, {"name": "aggregated-events", "category": "data-processing"})
    (job_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": exp_spec.name,
                "spec": {"name": "aggregated-events", "seed_class": "craft-gap"},
                "exemplar": "event-summary",
            }
        )
    )
    (job_dir / "result.json").write_text(json.dumps({"passed": True}))

    # Completeness checker validates package
    comp_res = check_task_completeness(task_pkg)
    assert comp_res["passed"] is True
    verifier_dir = job_dir / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward.json").write_text(json.dumps({"reward": 1.0}))
    (verifier_dir / "checks.json").write_text(json.dumps(comp_res["checks"]))

    # 3. Harvest moves task into _proposed/<proposal_id>
    proposal = pipe.harvest(job_dir)
    assert proposal.outcome == "proposed"
    assert (proposal.path / "task.toml").is_file()
    assert (proposal.path / "instruction.md").is_file()
    assert (proposal.path / "solution/solve.sh").is_file()
    assert (proposal.path / "tests/test.sh").is_file()

    # 4. Existing battery gates and passes
    report = pipe.run_battery(proposal.proposal_id)
    assert report.all_passed is True
    assert report.outcome == "battery_passed"
    assert len(report.checks) == 4
    assert all(check.passed for check in report.checks)
    assert pipe.records()[-1].outcome == "battery_passed"


def test_cli_propose_via_harbor_and_harvest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    common = ["--root", str(repo), "--out", str(derived), "--json"]

    # Propose --via-harbor
    propose_args = [*common, "propose", "--via-harbor", "--seed", "craft-gap", "--agent", "oracle"]
    assert main(propose_args) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["purpose"] == "craft"
    assert captured["destination"] == "approved"

    # Create mock job output
    job_dir = repo / "runs" / "cli-job-01"
    task_pkg = job_dir / "artifacts" / "output" / "task"
    generate_stub_task(task_pkg, {"name": "cli-task"})
    (job_dir / "manifest.json").write_text(json.dumps({"job_id": "cli-job-01"}))
    (job_dir / "result.json").write_text(json.dumps({"passed": True}))
    verifier_dir = job_dir / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward.json").write_text(json.dumps({"reward": 1.0}))

    # Harvest
    assert main([*common, "harvest", "cli-job-01"]) == 0
    harvested = json.loads(capsys.readouterr().out)
    assert harvested["outcome"] == "proposed"
    assert harvested["job_id"] == "cli-job-01"


# --------------------------------------------------------------------------- #
# SG-2: Spec-Sampler, Axes, and Coverage-First Tests
# --------------------------------------------------------------------------- #


def test_axis_files_load_and_validate() -> None:
    """Axis YAML files under authoring/templates load and validate correctly."""
    repo_root = Path(__file__).resolve().parents[1]
    axes = load_all_axes(repo_root)
    assert "category" in axes
    assert "scenario" in axes
    assert "difficulty" in axes

    # 1. Categories: derived from CRAFT & TB3 corpus
    categories = axes["category"]
    assert len(categories) >= 10
    for cat in categories:
        assert "slug" in cat and isinstance(cat["slug"], str)
        assert "title" in cat and isinstance(cat["title"], str)
        assert "description" in cat and len(cat["description"]) > 10
        assert "corpus_exemplars" in cat and isinstance(cat["corpus_exemplars"], list)

    # 2. Scenarios: 8-10 instruction styles spanning register and length
    scenarios = axes["scenario"]
    assert 8 <= len(scenarios) <= 10
    for scen in scenarios:
        assert "slug" in scen and isinstance(scen["slug"], str)
        assert "title" in scen and isinstance(scen["title"], str)
        assert "register" in scen and isinstance(scen["register"], str)
        assert "length" in scen and isinstance(scen["length"], str)
        assert "description" in scen and len(scen["description"]) > 10

    # 3. Difficulty: levels with anti-pattern lists
    difficulties = axes["difficulty"]
    assert len(difficulties) >= 4
    for diff in difficulties:
        assert "slug" in diff and isinstance(diff["slug"], str)
        assert "description" in diff and len(diff["description"]) > 10
        assert "anti_patterns" in diff and isinstance(diff["anti_patterns"], list)
        assert len(diff["anti_patterns"]) >= 2


def test_malformed_axis_file_is_refused_with_filename(tmp_path: Path) -> None:
    """A malformed or missing axis file is refused with a message naming the file."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()

    # 1. Missing file
    with pytest.raises(AuthoringError) as exc_missing:
        load_axis("category", template_dir=template_dir)
    assert "category.yaml" in str(exc_missing.value)

    # 2. Syntax error in YAML
    bad_yaml = template_dir / "category.yaml"
    bad_yaml.write_text("slug: [unclosed list\n  broken: true")
    with pytest.raises(AuthoringError) as exc_syntax:
        load_axis("category", template_dir=template_dir)
    assert "category.yaml" in str(exc_syntax.value)

    # 3. Non-list YAML
    bad_yaml.write_text("slug: single-object\ntitle: Not a list\n")
    with pytest.raises(AuthoringError) as exc_nonlist:
        load_axis("category", template_dir=template_dir)
    assert "category.yaml" in str(exc_nonlist.value)

    # 4. Missing required field in difficulty
    diff_yaml = template_dir / "difficulty.yaml"
    diff_yaml.write_text("- slug: easy\n  description: Simple task\n")
    with pytest.raises(AuthoringError) as exc_field:
        load_axis("difficulty", template_dir=template_dir)
    assert "difficulty.yaml" in str(exc_field.value)
    assert "anti_patterns" in str(exc_field.value)


def test_find_all_craft_gaps_identifies_uncovered_facet_triples(tmp_path: Path) -> None:
    """find_all_craft_gaps returns all uncovered facet triples in stable order."""
    # Total combinations: 5 verifier_types x 2 multi_container x 2 pinned_deps = 20
    # Cover 6 specific triples
    covered = [
        ("pytest", False, False),
        ("pytest", False, True),
        ("diff", False, False),
        ("golden_file", True, False),
        ("judge", False, True),
        ("hybrid", True, True),
    ]
    parquet_path = write_craft_parquet(tmp_path / "craft.parquet", covered)
    gaps = find_all_craft_gaps(parquet_path)
    assert len(gaps) == 14
    assert ("pytest", False, False) not in [
        (g["verifier_type"], g["env_multi_container"], g["pinned_deps"]) for g in gaps
    ]
    assert ("pytest", True, True) in [
        (g["verifier_type"], g["env_multi_container"], g["pinned_deps"]) for g in gaps
    ]

    # Cover all 20 triples
    all_covered = []
    for v in ("pytest", "diff", "golden_file", "judge", "hybrid"):
        for m in (False, True):
            for p in (False, True):
                all_covered.append((v, m, p))
    full_parquet = write_craft_parquet(tmp_path / "full_craft.parquet", all_covered)
    assert find_all_craft_gaps(full_parquet) == []
    with pytest.raises(AuthoringError) as exc:
        find_craft_gap(full_parquet)
    assert "covers every" in str(exc.value)


def test_spec_sampling_20_specs_zero_duplicates_and_coverage_first(tmp_path: Path) -> None:
    """Sampler emits 20 specs with zero duplicates against ledger and >=1/3 from craft gaps."""
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    derived.mkdir(parents=True, exist_ok=True)

    # Provide craft parquet with 8 known gaps (12 covered)
    # 8 / 20 = 40% >= 1/3 (33.3%)
    covered = [
        ("pytest", False, False),
        ("pytest", False, True),
        ("pytest", True, False),
        ("diff", False, False),
        ("diff", False, True),
        ("golden_file", False, False),
        ("golden_file", True, False),
        ("judge", False, False),
        ("judge", True, False),
        ("hybrid", False, False),
        ("hybrid", False, True),
        ("hybrid", True, False),
    ]
    write_craft_parquet(derived / "craft" / "craft.parquet", covered)

    # Sample 20 specs
    specs = sample_spec_batch(repo, count=20, derived_root=derived, seed=123)
    assert len(specs) == 20

    # 1. Zero duplicates among the 20 emitted specs
    emitted_coords = [spec_coordinate_key(s) for s in specs]
    assert len(emitted_coords) == len(set(emitted_coords))

    # 2. Coverage-first ordering & provenance split
    # The 8 gap-derived specs must come FIRST
    gap_specs = [s for s in specs if s.get("provenance") == "craft-gap"]
    random_specs = [s for s in specs if s.get("provenance") == "random-product"]

    assert len(gap_specs) == 8
    assert len(random_specs) == 12
    assert len(gap_specs) / len(specs) >= (1 / 3)  # 40% >= 33.3%

    # Assert ordering: first 8 are craft-gap, rest are random-product
    for i in range(8):
        assert specs[i]["provenance"] == "craft-gap"
        assert specs[i]["seed_class"] == "craft-gap"
        assert "target_facets" in specs[i]
        assert "axes" in specs[i]

    for i in range(8, 20):
        assert specs[i]["provenance"] == "random-product"
        assert specs[i]["seed_class"] == "scenario"
        assert "axes" in specs[i]


def test_ledger_duplicate_exclusion(tmp_path: Path) -> None:
    """A spec matching an existing ledger entry is not emitted."""
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    derived.mkdir(parents=True, exist_ok=True)

    write_craft_parquet(derived / "craft" / "craft.parquet", [("pytest", False, False)])

    # First, sample a single spec to see what would be produced
    initial = sample_spec_batch(repo, count=1, derived_root=derived, seed=42)[0]
    target_coord = spec_coordinate_key(initial)

    # Create quarantine proposal and ledger entry with this exact coordinate
    prop_dir = repo / "library" / "tasks" / "_proposed" / "prop-existing-01"
    prop_dir.mkdir(parents=True, exist_ok=True)
    prop_manifest = {
        "schema_version": "authoring/1",
        "proposal_id": "prop-existing-01",
        "seed_class": initial["seed_class"],
        "outcome": "proposed",
        "category": initial["category"],
        "scenario": initial["scenario"],
        "difficulty": initial["difficulty"],
        "target_facets": initial.get("target_facets"),
        "axes": initial.get("axes"),
    }
    (prop_dir / "proposal.json").write_text(json.dumps(prop_manifest))

    ledger_file = derived / "qualification" / "ledger.parquet"
    upsert_ledger(
        ledger_file,
        QualificationRecord(
            proposal_id="prop-existing-01",
            seed_class=initial["seed_class"],
            outcome="proposed",
            created_at="2026-08-17T00:00:00Z",
            updated_at="2026-08-17T00:00:00Z",
        ),
    )

    # Sample 10 specs now with the ledger populated
    new_specs = sample_spec_batch(repo, count=10, derived_root=derived, seed=42)
    new_coords = [spec_coordinate_key(s) for s in new_specs]

    # Target coordinate from ledger must NOT be emitted
    assert target_coord not in new_coords
    assert all(s["name"] != initial["name"] for s in new_specs)


def test_multi_phase_novel_spec_mode_with_injected_stub(tmp_path: Path) -> None:
    """Multi-phase novel-spec mode works with injected designer stub and default stub."""
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"

    # 1. Custom injected designer stub
    def custom_designer(topic: str, style: str) -> dict[str, Any]:
        return {
            "schema_version": "spec/1",
            "name": f"injected-{topic}-{style}",
            "category": f"synth-{topic}",
            "scenario": f"synth-{style}",
            "difficulty": "expert",
            "summary": f"Injected synthetic task {topic} / {style}",
            "seed_class": "scenario",
            "provenance": "novel-spec",
            "axes": {
                "category": f"synth-{topic}",
                "scenario": f"synth-{style}",
                "difficulty": "expert",
            },
        }

    specs = sample_spec_batch(
        repo,
        count=10,
        derived_root=derived,
        novel_designer=custom_designer,
        novel_count=4,
    )
    novel_specs = [s for s in specs if s.get("provenance") == "novel-spec"]
    assert len(novel_specs) == 4
    for ns in novel_specs:
        assert ns["name"].startswith("injected-")
        assert ns["category"].startswith("synth-")

    # 2. Default deterministic designer stub (invokes no provider)
    default_spec = design_novel_spec("distributed-tracing", "incident-emergency")
    assert default_spec["provenance"] == "novel-spec"
    assert default_spec["category"] == "novel-distributed-tracing"
    assert default_spec["scenario"] == "novel-incident-emergency"
    assert default_spec["difficulty"] == "intermediate"


def test_proposal_records_axis_coordinates_lineage(tmp_path: Path) -> None:
    """Proposal records axis coordinates and provenance for lineage tracking."""
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    # Propose craft-gap
    proposal = pipe.propose(seed="craft-gap")
    assert proposal.category is not None
    assert proposal.scenario is not None
    assert proposal.difficulty is not None
    assert proposal.provenance == "craft-gap"
    assert proposal.axes is not None
    assert "target_facets" in proposal.axes

    # Check proposal.json
    raw = json.loads((proposal.path / "proposal.json").read_text())
    assert raw["category"] == proposal.category
    assert raw["scenario"] == proposal.scenario
    assert raw["difficulty"] == proposal.difficulty
    assert raw["provenance"] == "craft-gap"
    assert raw["axes"] == proposal.axes

    # Check ProposalSpec schema instantiation
    spec_model = ProposalSpec(
        name=f"spec-{proposal.proposal_id}",
        category=proposal.category,
        scenario=proposal.scenario,
        difficulty=proposal.difficulty,
        seed_class=proposal.seed_class,
        provenance=proposal.provenance,
        axes=ProposalAxes(
            category=proposal.category,
            scenario=proposal.scenario,
            difficulty=proposal.difficulty,
            target_facets=proposal.target_facets,
        ),
    )
    assert spec_model.axes.category == proposal.category


def test_cli_sample_specs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI sample command emits specs in human summary and JSON formats."""
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    derived.mkdir(parents=True, exist_ok=True)
    write_craft_parquet(derived / "craft" / "craft.parquet", [("pytest", False, False)])
    common = ["--root", str(repo), "--out", str(derived)]

    # 1. Human formatted output
    assert main([*common, "sample", "--count", "5"]) == 0
    captured = capsys.readouterr().out
    assert "Sampled 5 specs" in captured
    assert "Craft-gap queries:" in captured

    # 2. JSON formatted output
    assert main([*common, "--json", "sample", "--count", "5"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 5
    assert "specs" in data
    assert len(data["specs"]) == 5
    assert data["craft_gap_count"] >= 1


def test_real_corpus_sample_specs_coverage_and_split() -> None:
    """Real corpus test: sampler emits 20 specs with zero duplicates against ledger.

    Asserter: >=1/3 originate from craft gaps.
    """
    repo_root = Path(__file__).resolve().parents[1]
    derived = derived_root_from_environment(repo_root)
    craft_pq = derived / "craft" / "craft.parquet"
    if not craft_pq.is_file():
        pytest.skip("Machine-local craft.parquet absent; skipped in CI")

    specs = sample_spec_batch(repo_root, count=20, derived_root=derived)
    assert len(specs) == 20

    # Zero duplicates
    coords = [spec_coordinate_key(s) for s in specs]
    assert len(coords) == len(set(coords))

    # Provenance split
    gap_count = sum(1 for s in specs if s.get("provenance") == "craft-gap")
    assert gap_count / len(specs) >= (1 / 3), f"Expected >= 1/3 gap specs, got {gap_count}/20"
    assert gap_count == 13  # 13 gaps present in real scanned corpus


def test_propose_inversion_executes_reference_analysis_and_records_provenance(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    # Add structured events.jsonl to library/tasks/event-summary/environment
    events_data = [
        {"kind": "click", "duration_ms": 120, "user": "alice"},
        {"kind": "scroll", "duration_ms": 45, "user": "bob"},
        {"kind": "click", "duration_ms": 200, "user": "alice"},
        {"kind": "submit", "duration_ms": 310, "user": "carol"},
    ]
    env_dir = repo / "library" / "tasks" / "event-summary" / "environment"
    events_file = env_dir / "events.jsonl"
    events_file.write_text("\n".join(json.dumps(r) for r in events_data) + "\n", encoding="utf-8")

    pipe = pipeline_for(repo)
    proposal = pipe.propose("inversion", ref="event-summary")

    assert proposal.seed_class == "inversion"
    assert proposal.outcome == "proposed"
    assert proposal.source_path == "library/tasks/event-summary/environment/events.jsonl"
    assert proposal.source_digest is not None
    assert proposal.source_digest.startswith("sha256:")

    # Check inversion analysis metadata
    assert proposal.inversion_analysis is not None
    inv = proposal.inversion_analysis
    assert inv["schema_version"] == "inversion/1"
    assert inv["data_asset_path"] == "library/tasks/event-summary/environment/events.jsonl"
    assert inv["data_asset_digest"] == proposal.source_digest
    assert "analysis_code" in inv
    assert inv["analysis_digest"].startswith("sha256:")

    # Verify computed_value came from real execution against data
    computed = inv["computed_value"]
    assert computed["schema_version"] == 1
    assert computed["total_records"] == 4
    assert computed["status"] == "ok"
    assert computed["counts"] == {"click": 2, "scroll": 1, "submit": 1}
    assert computed["total_duration_ms"] == 675

    # Check proposal package files
    assert (proposal.path / "task.toml").is_file()
    assert (proposal.path / "instruction.md").is_file()
    assert (proposal.path / "environment/events.jsonl").is_file()
    assert (proposal.path / "solution/solve.sh").is_file()
    assert (proposal.path / "solution/solve.py").is_file()
    assert (proposal.path / "tests/test.sh").is_file()
    assert (proposal.path / "tests/verify.py").is_file()
    assert (proposal.path / "inversion.json").is_file()

    # Check ledger record
    record = pipe.records()[0]
    assert record.seed_class == "inversion"
    assert record.outcome == "proposed"


def test_failed_reference_analysis_yields_refusal_not_guessed_key(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)

    # 1. Broken Python syntax or runtime exception in reference analysis
    broken_code = "import json\nraise RuntimeError('Fatal failure in reference analysis')\n"
    with pytest.raises(AuthoringError, match="reference analysis failed") as exc_info:
        pipe.propose("inversion", ref="event-summary", analysis_code=broken_code)
    assert "Fatal failure" in str(exc_info.value)

    # Ensure no proposal was created in quarantine
    proposals = list(pipe.quarantine.glob("*")) if pipe.quarantine.exists() else []
    assert len(proposals) == 0

    # 2. Analysis produces no output file
    empty_code = "print('did nothing')\n"
    with pytest.raises(AuthoringError, match="produced no output file"):
        pipe.propose("inversion", ref="event-summary", analysis_code=empty_code)

    # 3. Non-existent data asset
    with pytest.raises(AuthoringError, match="no data asset found"):
        pipe.propose("inversion", ref="non_existent_asset_xyz")


def test_inversion_reproducibility_check_catches_model_authored_key(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("inversion", ref="event-summary")

    # Ground truth execution reproduces exact answer
    assert verify_inversion_reproducibility(proposal) is True
    assert verify_inversion_reproducibility(proposal.path) is True

    # Re-execute explicitly
    recomputed = reexecute_inversion_analysis(proposal.path)
    assert recomputed == proposal.inversion_analysis["computed_value"]

    # Simulate a model-authored / fabricated key that diverges from execution
    inv_path = proposal.path / "inversion.json"
    inv_data = json.loads(inv_path.read_text(encoding="utf-8"))
    inv_data["computed_value"]["total_lines"] = 99999  # fabricated value
    inv_path.write_text(json.dumps(inv_data), encoding="utf-8")

    prop_path = proposal.path / "proposal.json"
    prop_data = json.loads(prop_path.read_text(encoding="utf-8"))
    prop_data["inversion_analysis"]["computed_value"]["total_lines"] = 99999
    prop_path.write_text(json.dumps(prop_data), encoding="utf-8")

    # Verification detects mismatch
    assert verify_inversion_reproducibility(proposal.path) is False


def test_three_inversion_proposals_reach_human_gate_and_reproduce_exact_answer(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)

    # Asset 1: JSONL event stream
    events_path = repo / "library" / "tasks" / "event-summary" / "environment" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps({"kind": "view", "duration_ms": 100})
        + "\n"
        + json.dumps({"kind": "click", "duration_ms": 250})
        + "\n"
        + json.dumps({"kind": "view", "duration_ms": 150})
        + "\n",
        encoding="utf-8",
    )

    # Asset 2: JSON data array
    data_path = (
        repo
        / "library"
        / "meta"
        / "synthesize-task@1"
        / "environment"
        / "skeleton"
        / "environment"
        / "data.json"
    )
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps(
            [
                {"id": 1, "type": "alpha", "val": 10},
                {"id": 2, "type": "beta", "val": 20},
                {"id": 3, "type": "alpha", "val": 30},
                {"id": 4, "type": "gamma", "val": 40},
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Asset 3: SQL query script / data asset
    sql_path = repo / "library" / "tasks" / "query-optimize" / "environment" / "my-query.sql"
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_text(
        "SELECT id, name FROM users WHERE active = 1;\nSELECT count(*) FROM orders;\n",
        encoding="utf-8",
    )

    pipe = pipeline_for(repo)

    proposals = [
        pipe.propose("inversion", ref="library/tasks/event-summary/environment/events.jsonl"),
        pipe.propose(
            "inversion",
            ref="library/meta/synthesize-task@1/environment/skeleton/environment/data.json",
        ),
        pipe.propose("inversion", ref="library/tasks/query-optimize/environment/my-query.sql"),
    ]

    for prop in proposals:
        assert prop.seed_class == "inversion"
        assert prop.outcome == "proposed"

        # 1. Run local battery controls
        battery = pipe.run_battery(prop.proposal_id)
        assert battery.all_passed is True
        assert battery.outcome == "battery_passed"
        assert len(battery.checks) == 4
        assert all(c.passed for c in battery.checks)

        # 2. Review rubric
        review = pipe.review(prop.proposal_id)
        assert review.outcome == "craft_reviewed"
        assert review.score == 1.0
        assert any(
            "inversion answer key verified by execution" in reason for reason in review.reasons
        )

        # 3. Fail-closed human gate: automation cannot register
        with pytest.raises(RegisterRefusal):
            pipe.register(prop.proposal_id)

        # 4. LOAD-BEARING: Re-executing reference analysis reproduces recorded answer exactly
        assert verify_inversion_reproducibility(prop) is True
        recomputed = reexecute_inversion_analysis(prop.path)
        assert recomputed == prop.inversion_analysis["computed_value"]

    # All 3 proposals reached craft_reviewed on ledger
    records = pipe.records()
    assert len(records) == 3
    assert all(r.seed_class == "inversion" for r in records)
    assert all(r.outcome == "craft_reviewed" for r in records)
    assert all(r.review_score == 1.0 for r in records)


def test_inversion_lineage_resolves_through_evallab_lineage(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("inversion", ref="event-summary")

    manifest = json.loads((proposal.path / "proposal.json").read_text(encoding="utf-8"))
    assert "inputs" in manifest
    assert len(manifest["inputs"]) == 1
    assert manifest["inputs"][0]["path"] == "library/tasks/event-summary/environment/input.txt"
    assert manifest["inputs"][0]["digest"] == proposal.source_digest

    # 1. read_artifact_inputs contract parser
    status, inputs, reason = read_artifact_inputs(proposal.path / "proposal.json", "z3", repo)
    assert status == "ok"
    assert len(inputs) == 1
    assert inputs[0]["path"] == "library/tasks/event-summary/environment/input.txt"
    assert inputs[0]["digest"] == proposal.source_digest

    # 2. Lineage walker traces from proposal to data asset and verifies digest
    node = resolve_lineage(str(proposal.path / "proposal.json"), repo_root=repo)
    assert len(node.inputs) == 1
    input_node = node.inputs[0]
    assert input_node.path == "library/tasks/event-summary/environment/input.txt"
    assert input_node.actual_digest == proposal.source_digest
    assert input_node.expected_digest == proposal.source_digest


def test_inversion_task_passes_completeness_checker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    data_file = repo / "library" / "tasks" / "event-summary" / "environment" / "data.json"
    data_file.write_text(json.dumps([{"id": 1, "type": "a", "val": 10}]), encoding="utf-8")

    pipe = pipeline_for(repo)
    proposal = pipe.propose("inversion", ref=str(data_file.relative_to(repo)))

    # Run completeness checker on generated inversion task package
    comp_report = check_task_completeness(proposal.path)
    assert comp_report["passed"] is True
    assert comp_report["checks"]["package_structure"]["passed"] is True
    assert comp_report["checks"]["oracle_solution_runs"]["passed"] is True
    assert comp_report["checks"]["task_tests_pass"]["passed"] is True
    assert comp_report["checks"]["no_answer_leakage"]["passed"] is True


def test_battery_and_review_gate_inversion_without_shortcut(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pipe = pipeline_for(repo)
    proposal = pipe.propose("inversion", ref="event-summary")

    # Cannot review before battery
    with pytest.raises(AuthoringError, match="battery_passed"):
        pipe.review(proposal.proposal_id)

    # Delete solution: battery oracle check must fail
    for child in (proposal.path / "solution").iterdir():
        child.unlink()
    (proposal.path / "solution").rmdir()

    battery_report = pipe.run_battery(proposal.proposal_id)
    assert battery_report.all_passed is False
    assert battery_report.checks[0].passed is False  # oracle check failed
    assert pipe.records()[0].battery_oracle is False
    assert pipe.records()[0].outcome == "proposed"


def test_cli_propose_inversion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    derived = repo / "derived" / "parquet"
    common = ["--root", str(repo), "--out", str(derived), "--json"]

    assert main([*common, "propose", "--seed", "inversion", "--ref", "event-summary"]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["seed_class"] == "inversion"
    assert captured["outcome"] == "proposed"
    proposal_id = captured["proposal_id"]

    # Battery
    assert main([*common, "battery", proposal_id]) == 0
    battery = json.loads(capsys.readouterr().out)
    assert battery["all_passed"] is True
    assert battery["outcome"] == "battery_passed"

    # Review
    assert main([*common, "review", proposal_id]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["outcome"] == "craft_reviewed"
    assert review["score"] == 1.0

    # Register halts
    assert main([*common, "register", proposal_id]) == 2
    err_output = capsys.readouterr().err
    assert REGISTER_REFUSAL in err_output
