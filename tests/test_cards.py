"""Tests for E11: eval-card generator with purpose-bound shape and mandatory uncertainty."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evallab import cli
from evallab.cards import (
    CardRefusalError,
    build_eval_card,
    draft_eval_card,
    validate_card,
    validate_card_file,
)
from evallab.cohort import bootstrap_mean_interval
from evallab.facts import digest_json


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _create_synthetic_job(
    root: Path,
    *,
    name: str,
    agent: str = "codex",
    model: str | None = "gpt-5.6-terra",
    task_rewards: dict[str, list[float]],
    exception_trials: list[tuple[str, str]] | None = None,
    spec_data: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """Create a synthetic Harbor job and matching queue/done spec under root."""
    job_dir = root / "runs" / name
    trials: list[str] = []
    trial_index = 0

    # Ensure TEMPLATE.md exists in root
    template_src = Path(__file__).resolve().parent.parent / "research/cards/TEMPLATE.md"
    template_dest = root / "research/cards/TEMPLATE.md"
    if not template_dest.is_file():
        template_dest.parent.mkdir(parents=True, exist_ok=True)
        template_dest.write_text(template_src.read_text(encoding="utf-8"), encoding="utf-8")

    # Scored task attempts
    for task_name, rewards in task_rewards.items():
        for attempt, reward in enumerate(rewards, start=1):
            trial_index += 1
            trial_dir = job_dir / f"{task_name.replace('/', '_')}__{attempt:02d}"
            trials.append(trial_dir.name)
            trial_id = f"10000000-0000-0000-0000-{trial_index:012d}"
            _write_json(trial_dir / "config.json", {"agent": {"name": agent}})
            _write_json(
                trial_dir / "lock.json",
                {
                    "schema_version": 2,
                    "task": {"name": task_name, "digest": f"sha256:{task_name}"},
                    "agent": {
                        "name": agent,
                        "model_name": model,
                        "skills": [],
                        "mcp_servers": [],
                        "kwargs": {},
                    },
                    "skills": [],
                    "environment": {"type": "docker"},
                    "verifier": {"type": "pytest"},
                    "harbor": {"version": "0.21.0"},
                },
            )
            _write_json(
                trial_dir / "result.json",
                {
                    "id": trial_id,
                    "trial_name": trial_dir.name,
                    "task_name": task_name,
                    "agent_name": agent,
                    "agent_info": {
                        "name": agent,
                        "version": "0.147.0",
                        "model_info": {"name": model},
                    },
                    "verifier_result": {"rewards": {"reward": reward}},
                    "started_at": "2026-08-15T00:00:00Z",
                    "finished_at": "2026-08-15T00:01:00Z",
                },
            )

    # Exception trials (never measured)
    if exception_trials:
        for task_name, exc_class in exception_trials:
            trial_index += 1
            trial_dir = job_dir / f"{task_name.replace('/', '_')}__exc_{trial_index:02d}"
            trials.append(trial_dir.name)
            trial_id = f"10000000-0000-0000-0000-{trial_index:012d}"
            _write_json(trial_dir / "config.json", {"agent": {"name": agent}})
            _write_json(
                trial_dir / "lock.json",
                {
                    "schema_version": 2,
                    "task": {"name": task_name, "digest": f"sha256:{task_name}"},
                    "agent": {
                        "name": agent,
                        "model_name": model,
                        "skills": [],
                        "mcp_servers": [],
                        "kwargs": {},
                    },
                    "skills": [],
                    "environment": {"type": "docker"},
                    "verifier": {"type": "pytest"},
                    "harbor": {"version": "0.21.0"},
                },
            )
            _write_json(
                trial_dir / "result.json",
                {
                    "id": trial_id,
                    "trial_name": trial_dir.name,
                    "task_name": task_name,
                    "agent_name": agent,
                    "agent_info": {
                        "name": agent,
                        "version": "0.147.0",
                        "model_info": {"name": model},
                    },
                    "exception_info": {
                        "exception_type": exc_class,
                        "exception_phase": "unknown",
                    },
                    "verifier_result": {},
                    "started_at": "2026-08-15T00:00:00Z",
                    "finished_at": "2026-08-15T00:01:00Z",
                },
            )

    job_id = f"job-{name}"
    _write_json(job_dir / "config.json", {"name": name, "agent": agent})
    _write_json(
        job_dir / "lock.json",
        {
            "schema_version": 2,
            "agent": {"name": agent, "model_name": model},
            "harbor": {"version": "0.21.0"},
        },
    )
    _write_json(
        job_dir / "result.json",
        {
            "id": job_id,
            "job_name": name,
            "n_total_trials": len(trials),
            "stats": {"n_trials": len(trials)},
            "finished_at": "2026-08-15T00:10:00Z",
        },
    )

    # Spec file in queue/done
    first_task = next(iter(task_rewards.keys())) if task_rewards else "task-01"
    base_spec: dict[str, object] = {
        "schema_version": 1,
        "spec_id": f"spec-{name}",
        "name": name,
        "hypothesis": f"Test hypothesis for {name}",
        "purpose": "baseline",
        "task": first_task,
        "agent": agent,
        "model": model,
        "attempts": max(len(r) for r in task_rewards.values()) if task_rewards else 1,
        "concurrency": 1,
        "jobs_dir": "runs",
        "submitted_by": "test",
        "priority": 100,
        "est_cost_usd": 0.0,
    }
    if spec_data:
        base_spec.update(spec_data)

    spec_path = root / "queue/done" / f"{name}.json"
    _write_json(spec_path, base_spec)

    _write_json(
        job_dir / "lab-metadata.json",
        {"experiment": base_spec},
    )

    return spec_path, job_dir


def test_fixture_cohort_pass_at_k_and_exceptions(tmp_path: Path) -> None:
    """Fixture cohort: two tasks, k=3, one passing, one failing, plus one exception trial.

    Asserts:
    - pass@k, n, and interval match values computed in the test
    - never-measured trial is reported separately from scored zero
    """
    task_rewards = {
        "task-pass": [1.0, 1.0, 1.0],  # passes at k=3 (outcome = 1.0)
        "task-fail": [0.0, 0.0, 0.0],  # fails at k=3 (outcome = 0.0)
    }
    exception_trials = [("task-exc", "ValueError")]

    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="cohort-fixture-job",
        task_rewards=task_rewards,
        exception_trials=exception_trials,
        spec_data={"attempts": 3, "purpose": "baseline"},
    )

    rendered, card = build_eval_card(spec_path, repo_root=tmp_path)

    # Compute expected values in test
    expected_outcomes = [0.0, 1.0]  # sorted by task_digest
    expected_pass_at_k = 0.5  # 1 out of 2 tasks passed
    expected_interval = bootstrap_mean_interval(expected_outcomes, seed=card["numbers"]["k"])
    assert expected_interval is not None

    # Assert numbers
    numbers = card["numbers"]
    assert numbers["n_tasks"] == 2
    assert numbers["n_trials"] == 7  # 3 + 3 + 1
    assert numbers["k"] == 3
    assert numbers["pass_at_k"] == expected_pass_at_k
    assert numbers["exceptions"] == 1
    assert not numbers["is_underpowered"]

    # Assert rendered markdown
    assert "- Task evidence units: **2**" in rendered
    assert "- Recorded trials: **7**" in rendered
    assert "- Attempts per task (`k`): **3**" in rendered
    assert "- Observed pass@k: **0.500**" in rendered
    expected_lo = expected_interval[0]
    expected_hi = expected_interval[1]
    assert (
        f"- Task-bootstrap 95% interval: **[{expected_lo:.3f}, {expected_hi:.3f}]**"
        in rendered
    )
    assert "- Execution/harness exceptions: **1**" in rendered

    # Assert exception trial is reported separately from scored zeros
    assert (
        "1 harness/execution exception trial(s) were excluded from capability measurement."
        in card["threats"]
    )
    # The capability denominator is 2 tasks, not 3 (the exception task is never measured)
    assert numbers["n_tasks"] == 2


def test_comparison_refuses_without_prereg(tmp_path: Path) -> None:
    """purpose='comparison' without a prereg block refuses, naming the missing block."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="comparison-no-prereg",
        task_rewards={"task-01": [1.0]},
        spec_data={"purpose": "comparison"},
    )

    with pytest.raises(CardRefusalError) as exc_info:
        build_eval_card(spec_path, repo_root=tmp_path)

    message = str(exc_info.value)
    assert "comparison" in message
    assert "prereg" in message.lower()


def test_comparison_renders_with_valid_prereg(tmp_path: Path) -> None:
    """purpose='comparison' with a valid prereg block generates card quoting prereg verbatim."""
    prereg = {
        "expected": "pass@3 delta >= +0.25 on tool-use tasks",
        "decision_rule": "adopt if lower bound of task-bootstrap 95% interval > 0",
    }
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="comparison-with-prereg",
        task_rewards={"task-01": [1.0, 1.0, 1.0], "task-02": [1.0, 0.0, 1.0]},
        spec_data={
            "purpose": "comparison",
            "attempts": 3,
            "prereg": prereg,
        },
    )

    rendered, card = build_eval_card(spec_path, repo_root=tmp_path)

    assert card["purpose"] == "comparison"
    assert "### Preregistration" in rendered
    assert f"- Expected result: {prereg['expected']}" in rendered
    assert f"- Decision rule: {prereg['decision_rule']}" in rendered


def test_practice_purpose_refuses(tmp_path: Path) -> None:
    """purpose='practice' is excluded from cards and refuses."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="practice-spec",
        task_rewards={"task-01": [1.0]},
        spec_data={"purpose": "practice"},
    )

    with pytest.raises(CardRefusalError) as exc_info:
        build_eval_card(spec_path, repo_root=tmp_path)

    message = str(exc_info.value)
    assert "practice" in message.lower()
    assert "excluded" in message.lower()


def test_underpowered_cohort_renders_not_distinguishable(tmp_path: Path) -> None:
    """Underpowered cohort (n_tasks=1) renders 'not distinguishable' rather than a bare rate."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="underpowered-cohort",
        task_rewards={"task-only": [1.0, 1.0, 1.0]},
        spec_data={"attempts": 3, "purpose": "baseline"},
    )

    rendered, card = build_eval_card(spec_path, repo_root=tmp_path)

    assert card["numbers"]["is_underpowered"]
    assert card["numbers"]["pass_at_k"] is None
    assert card["numbers"]["pass_at_k_text"] == "not distinguishable"

    # In markdown: must NOT render bare rate "1.000"
    assert "- Observed pass@k: **not distinguishable**" in rendered
    assert "- Task-bootstrap 95% interval: **unavailable**" in rendered
    assert "Underpowered cohort (n_tasks < 2); pass@k is not distinguishable." in card["threats"]


def test_two_generations_byte_identical(tmp_path: Path) -> None:
    """Two generations of the same card are byte-identical (determinism)."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="deterministic-card",
        task_rewards={"task-a": [1.0, 0.0], "task-b": [0.0, 1.0]},
        exception_trials=[("task-c", "NonZeroAgentExitCodeError")],
        spec_data={"attempts": 2, "purpose": "baseline"},
    )

    rendered1, card1 = build_eval_card(spec_path, repo_root=tmp_path)
    rendered2, card2 = build_eval_card(spec_path, repo_root=tmp_path)

    assert rendered1 == rendered2
    assert rendered1.encode("utf-8") == rendered2.encode("utf-8")
    assert json.dumps(card1, sort_keys=True) == json.dumps(card2, sort_keys=True)


def test_inputs_list_carries_spec_and_job_digests(tmp_path: Path) -> None:
    """Emitted inputs list carries the spec and job lock digests for E14 lineage."""
    spec_path, job_dir = _create_synthetic_job(
        tmp_path,
        name="lineage-card",
        task_rewards={"task-x": [1.0, 1.0], "task-y": [0.0, 0.0]},
        spec_data={"attempts": 2, "purpose": "baseline"},
    )

    _, card = build_eval_card(spec_path, repo_root=tmp_path)

    inputs = card["inputs"]
    assert isinstance(inputs, list)
    assert len(inputs) >= 2

    # Check spec input
    spec_raw = json.loads(spec_path.read_text())
    expected_spec_digest = digest_json(spec_raw)
    spec_entry = next(
        (item for item in inputs if item.get("path") == f"queue/done/{spec_path.name}"),
        None,
    )
    assert spec_entry is not None
    assert spec_entry["digest"] == expected_spec_digest

    # Check job input
    job_lock = json.loads((job_dir / "lock.json").read_text())
    expected_job_lock_digest = digest_json(job_lock)
    job_entry = next(
        (item for item in inputs if "runs/lineage-card" in item.get("path", "")),
        None,
    )
    assert job_entry is not None
    assert job_entry["digest"] == expected_job_lock_digest


def test_draft_eval_card_writes_file_atomically(tmp_path: Path) -> None:
    """draft_eval_card writes output card atomically to specified destination."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="draft-write-test",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [0.0, 1.0]},
        spec_data={"attempts": 2},
    )

    out_file = tmp_path / "research/cards/custom-output.md"
    dest, card = draft_eval_card(spec_path, repo_root=tmp_path, output_path=out_file)

    assert dest == out_file
    assert out_file.is_file()
    content = out_file.read_text(encoding="utf-8")
    assert "# Eval card: draft-write-test" in content
    assert card["title"] == "draft-write-test"


def test_cli_card_generate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI evallab card generate command outputs card to stdout, with --json, and to file."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="cli-test-card",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [1.0, 0.0]},
        spec_data={"attempts": 2},
    )

    # 1. Test stdout rendering
    code = cli.run_cli(["card", "generate", str(spec_path)], workspace=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    assert "# Eval card: cli-test-card" in captured.out
    assert "- Observed pass@k: **1.000**" in captured.out

    # 2. Test --json summary
    code = cli.run_cli(["card", "generate", str(spec_path), "--json"], workspace=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["title"] == "cli-test-card"
    assert payload["numbers"]["n_tasks"] == 2

    # 3. Test -o output file writing
    out_path = tmp_path / "research/cards/cli-written.md"
    code = cli.run_cli(
        ["card", "generate", str(spec_path), "-o", str(out_path)],
        workspace=tmp_path,
    )
    assert code == 0
    captured = capsys.readouterr()
    assert f"eval card: {out_path}" in captured.out
    assert out_path.is_file()
    assert "# Eval card: cli-test-card" in out_path.read_text(encoding="utf-8")


def test_validate_card_valid(tmp_path: Path) -> None:
    """Generated card passes validate_card and validate_card_file."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="valid-card-job",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [1.0, 0.0]},
        spec_data={"attempts": 2},
    )
    rendered, _ = build_eval_card(spec_path, repo_root=tmp_path)
    result = validate_card(rendered)
    assert result.valid is True
    assert len(result.errors) == 0

    card_file = tmp_path / "research/cards/valid-card.md"
    card_file.parent.mkdir(parents=True, exist_ok=True)
    card_file.write_text(rendered, encoding="utf-8")
    file_result = validate_card_file(card_file)
    assert file_result.valid is True
    assert len(file_result.errors) == 0


def test_validate_card_unresolved_marker() -> None:
    """Card with unresolved template markers fails validation."""
    bad_content = "# Eval card: Bad\n\n## Question\n\n{{UNRESOLVED}}\n"
    result = validate_card(bad_content)
    assert result.valid is False
    assert any("unresolved template marker" in err for err in result.errors)


def test_validate_card_missing_contamination_caveat(tmp_path: Path) -> None:
    """Card without contamination caveat fails validation."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="no-contam-job",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [1.0, 0.0]},
        spec_data={"attempts": 2},
    )
    rendered, _ = build_eval_card(spec_path, repo_root=tmp_path)
    stripped = re.sub(r"- Contamination caveat:[^\n]*\n", "", rendered)
    stripped = re.sub(
        r"## Contamination note[\s\S]*?(?=##)",
        "## Contamination note\n\nNone\n\n",
        stripped,
    )
    result = validate_card(stripped)
    assert result.valid is False
    assert any("contamination caveat" in err.lower() for err in result.errors)


def test_validate_card_missing_elicitation_caveat(tmp_path: Path) -> None:
    """Card without elicitation caveat fails validation."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="no-elicit-job",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [1.0, 0.0]},
        spec_data={"attempts": 2},
    )
    rendered, _ = build_eval_card(spec_path, repo_root=tmp_path)
    stripped = re.sub(r"- Elicitation caveat:[^\n]*\n", "", rendered)
    stripped = re.sub(
        r"## Elicitation tuple and caveats[\s\S]*?(?=##)",
        "## Elicitation\n\nNone\n\n",
        stripped,
    )
    result = validate_card(stripped)
    assert result.valid is False
    assert any("elicitation caveat" in err.lower() for err in result.errors)


def test_cli_card_validate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI command  works."""
    spec_path, _ = _create_synthetic_job(
        tmp_path,
        name="cli-validate-job",
        task_rewards={"task-1": [1.0, 1.0], "task-2": [1.0, 0.0]},
        spec_data={"attempts": 2},
    )
    card_file = tmp_path / "research/cards/cli-validate.md"
    draft_eval_card(spec_path, repo_root=tmp_path, output_path=card_file)

    # 1. card validate
    code = cli.run_cli(["card", "validate", str(card_file)], workspace=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    assert "VALID" in captured.out

    # 2. card validate --json
    code = cli.run_cli(["card", "validate", str(card_file), "--json"], workspace=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["valid"] is True

    # 3. invalid card returns exit code 1
    bad_file = tmp_path / "research/cards/bad.md"
    bad_file.write_text("# Bad Card\n\nNothing here.\n", encoding="utf-8")
    code = cli.run_cli(["card", "validate", str(bad_file)], workspace=tmp_path)
    assert code == 1
    captured = capsys.readouterr()
    assert "INVALID" in captured.err

def test_all_committed_cards_pass_validation() -> None:
    """All markdown cards committed under research/cards/ pass validate_card_file."""
    cards_dir = Path(__file__).resolve().parent.parent / "research/cards"
    assert cards_dir.is_dir()
    card_files = [f for f in cards_dir.glob("*.md") if f.name not in {"README.md", "TEMPLATE.md"}]
    assert len(card_files) >= 1
    for card_file in card_files:
        result = validate_card_file(card_file)
        assert result.valid is True, f"Card {card_file.name} failed validation: {result.errors}"


def test_oracle_vs_codex_card_verdict_framing() -> None:
    """Oracle-vs-Codex card embeds instrument finding framing, avoiding false capability claims."""
    card_path = Path(__file__).resolve().parent.parent / "research/cards/oracle-vs-codex-cohort.md"
    assert card_path.is_file()
    content = card_path.read_text(encoding="utf-8")
    assert "instrument finding" in content.lower()
    assert "does not establish that codex lacks task capability" in content.lower()
    assert validate_card_file(card_path).valid is True


def test_sg1_metaloop_card_checks() -> None:
    """SG-1 meta-loop card validates and covers all 4 completeness checks."""
    card_path = Path(__file__).resolve().parent.parent / "research/cards/sg1-metaloop.md"
    assert card_path.is_file()
    content = card_path.read_text(encoding="utf-8")
    assert "package_structure" in content
    assert "no_answer_leakage" in content
    assert "oracle_solution_runs" in content
    assert "task_tests_pass" in content
    assert validate_card_file(card_path).valid is True


def test_real_corpus_evidence_skip_if_missing() -> None:
    """Real corpus test: skips if runs/ or promoted evidence is absent (CI conformance)."""
    corpus_dir = Path("runs")
    if not corpus_dir.is_dir():
        pytest.skip("Machine-local corpus data (runs/) is absent in CI")
