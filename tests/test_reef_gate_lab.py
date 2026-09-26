"""Gate invariants using real task freezing and queue persistence; no live inference."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import evallab_reef_gate.lab as lab_module
import pytest
from evallab_reef_gate.lab import LabConfig, LabEvaluator, LabGateError, SubprocessRunner

from evallab.cli import parser
from evallab.queue import DirectoryQueue, PolicyGate
from evallab.registry import compute_task_digests
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy
from evallab.task_prepare import prepare_task

TASKS = ("dev-alpha", "dev-beta")
CURRENT = {"terminus/AGENTS.md": "Inspect the task files before editing.\n"}
CANDIDATE = {"terminus/AGENTS.md": "Inspect files, then validate required outputs.\n"}
STAMP = "2026-09-26T00:00:00+00:00"


def git(root: Path, *args: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(
        ["git", "-c", "user.name=Gate Test", "-c", "user.email=gate@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, env=environment, capture_output=True, check=True,
    )


class LocalEvidenceRunner(SubprocessRunner):
    """Real prepare/queue contracts with injected native trial outcomes at tick."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.queue = DirectoryQueue(root / "queue")
        self.submissions: list[str] = []
        self.ticks: list[str] = []
        self.outcomes: dict[str, dict] = {}
        self.crash_after_submit = False

    def run(self, argv, *, cwd, timeout, python):
        assert cwd == self.root
        if argv[:2] == ["tasks", "prepare"]:
            args = parser().parse_args(argv)
            prepared = prepare_task(
                self.root, self.root / args.source, name=args.name, agent=args.agent,
                model=args.model, environment=args.environment, output=args.output,
                harness_tree_path=args.harness_tree, harness_tree_sha256=args.harness_tree_sha256,
                cost_limit_usd=args.cost_limit_usd, est_cost_usd=args.estimated_cost_usd,
                max_requests=args.max_requests, max_input_tokens=args.max_input_tokens,
                max_output_tokens=args.max_output_tokens, max_total_tokens=args.max_total_tokens,
                timeout_seconds=args.timeout_seconds, submitted_by=args.submitted_by,
            )
            return 0, prepared.spec.model_dump_json(), ""
        if argv[0] == "submit":
            spec = ExperimentSpec.model_validate_json((self.root / argv[1]).read_text())
            policy = StandingApprovalsPolicy(
                daily_cost_ceiling_usd=10, per_job_cost_ceiling_usd=1,
                quiet_failure_rule=3,
                auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
                escalate_to_human=[],
            )
            path, decision = self.queue.submit(spec, gate=PolicyGate(policy), spent_today_usd=0)
            assert not decision.admitted
            spec_id = self.queue.load(path).spec_id
            self.submissions.append(spec_id)
            if self.crash_after_submit:
                self.crash_after_submit = False
                raise OSError("simulated crash after durable queue submission")
            return 0, f"spec_id: {spec_id}\n", ""
        assert argv[0:2] == ["tick", "--spec-id"]
        spec_id = argv[2]
        path = self.queue.locate(spec_id)
        assert path.parent.name == "approved"
        spec = self.queue.load(path)
        self.ticks.append(spec_id)
        running = self.queue.transition(path, "running", actor="test-runner", event="started")
        trial_dir = self.root / "runs" / spec.name / "native-trial"
        trial_dir.mkdir(parents=True)
        trial = self.outcomes.get(spec_id, {
            "id": f"trial-{spec_id}", "finished_at": STAMP,
            "verifier_result": {"rewards": {"reward": 1.0}},
        })
        (trial_dir / "result.json").write_text(json.dumps(trial))
        (trial_dir.parent / "result.json").write_text(json.dumps({"finished_at": STAMP}))
        self.queue.transition(running, "done", actor="test-runner", event="completed")
        return 0, "dispatched 1 experiment(s)", ""


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(lab_module, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
    ))
    root = tmp_path / "lab"
    root.mkdir()
    (root / "library/registry").mkdir(parents=True)
    rows = []
    for name in TASKS:
        task = root / "library/tasks" / name
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(
            'version = "1.0"\n[metadata]\nname = "' + name
            + '"\n[agent]\ntimeout_sec = 60\n[environment]\ncpu_count = 1\nmemory_mb = 512\n'
        )
        (task / "instruction.md").write_text(f"Write the required result for {name}.\n")
        (task / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
        (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        package = compute_task_digests(task).package
        relative = task.relative_to(root).as_posix()
        rows.append({"task_id": name, "task_path": relative, "package_digest": package})
        record = {"task_id": name, "task_path": relative, "state": "registered",
                  "allowed_uses": ["measurement", "training"], "digests": {"package": package}}
        (root / "library/registry" / f"{name}.json").write_text(json.dumps(record))
    split = {"schema_version": 1, "dev": rows, "held_out": [{
        "task_id": "held-out", "task_path": "library/tasks/held-out",
        "package_digest": "sha256:" + "f" * 64,
    }]}
    (root / "split.json").write_text(json.dumps(split))
    git(root, "init")
    git(root, "add", "split.json")
    git(root, "commit", "-m", "Commit split before evaluation")
    config = LabConfig.from_dict({
        "lab_root": str(root), "split_path": "split.json", "record_dir": "runs/gate",
        "model": "zai/glm-5.3-flash", "cost_limit_usd": 0.40, "est_cost_usd": 0.40,
        "tick_timeout_seconds": 0.2, "tick_poll_seconds": 0.01,
    })
    runner = LocalEvidenceRunner(root)
    return LabEvaluator(config, runner=runner), runner, root


def prepare(evaluator, identity="candidate"):
    return evaluator.prepare(identity, CURRENT, CANDIDATE, TASKS)


def approve(runner, result):
    for spec_id in result["spec_ids"]:
        runner.queue.approve(spec_id, actor="fixture-human")


def test_prepare_requires_explicit_approval_and_freezes_each_pair(campaign):
    evaluator, runner, root = campaign
    result = prepare(evaluator)
    assert runner.ticks == []
    assert [row["side"] for row in result["spec_details"]] == ["candidate", "current"] * 2
    for row in result["spec_details"]:
        path = runner.queue.locate(row["spec_id"])
        assert path.parent.name == "waiting"
        spec = runner.queue.load(path)
        assert spec.task_path.startswith("runs/.prepared-tasks/")
        assert compute_task_digests(root / spec.task_path).package == spec.task_package_digest
    assert result["manifest"]["tree_digests"]["candidate"] != result["manifest"]["tree_digests"]["current"]


def test_repeated_prepare_and_post_submit_crash_do_not_duplicate_specs(campaign):
    evaluator, runner, _ = campaign
    runner.crash_after_submit = True
    with pytest.raises(OSError):
        prepare(evaluator)
    first_id = runner.submissions[0]
    recovered = prepare(evaluator)
    repeated = prepare(evaluator)
    assert recovered["spec_ids"] == repeated["spec_ids"] == runner.submissions
    assert recovered["spec_ids"][0] == first_id
    assert len(set(recovered["spec_ids"])) == 4


def test_changed_candidate_refuses_without_mutating_retained_tree(campaign):
    evaluator, runner, _ = campaign
    original = prepare(evaluator)
    tree = Path(original["manifest_path"]).parent / "candidate/terminus/AGENTS.md"
    previous = tree.read_bytes()
    with pytest.raises(LabGateError, match="conflicting content"):
        evaluator.prepare("candidate", CURRENT, {"terminus/AGENTS.md": "different"}, TASKS)
    assert tree.read_bytes() == previous
    assert runner.submissions == original["spec_ids"]


@pytest.mark.parametrize("mutation", ["uncommitted", "heldout", "alias", "registry", "drift"])
def test_exclusions_and_drift_refuse_before_any_submission(campaign, mutation):
    evaluator, runner, root = campaign
    if mutation in ("uncommitted", "heldout", "alias"):
        split = json.loads((root / "split.json").read_text())
        if mutation == "uncommitted":
            split["note"] = "not committed"
        elif mutation == "heldout":
            split["held_out"][0]["task_id"] = TASKS[0]
        else:
            split["held_out"][0]["task_path"] = str(root / split["dev"][0]["task_path"])
        (root / "split.json").write_text(json.dumps(split))
        if mutation != "uncommitted":
            git(root, "add", "split.json")
            git(root, "commit", "-m", "Malformed boundary fixture")
    elif mutation == "registry":
        path = root / "library/registry" / (TASKS[0] + ".json")
        record = json.loads(path.read_text())
        record["allowed_uses"].append("heldout")
        path.write_text(json.dumps(record))
    else:
        (root / "library/tasks" / TASKS[0] / "instruction.md").write_text("changed task")
    with pytest.raises(LabGateError):
        prepare(evaluator)
    assert runner.submissions == []
    assert runner.ticks == []


def test_model_override_in_candidate_refuses_before_any_submission(campaign):
    evaluator, runner, _ = campaign
    poisoned = {**CANDIDATE, "terminus/config.json": '{"model_name":"another-model"}'}
    with pytest.raises(ValueError, match="model/transport binding"):
        evaluator.prepare("poisoned", CURRENT, poisoned, TASKS)
    assert runner.submissions == []


def test_missing_approval_and_withdrawal_preserve_none(campaign):
    evaluator, runner, _ = campaign
    result = prepare(evaluator)
    missing = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert missing["metadata"]["incomplete_reason"] == "approval_timeout"
    assert missing["metrics"]["candidate_scores"] == (None, None)
    assert missing["metrics"]["current_scores"] == (None, None)
    assert missing["metrics"]["evaluation_sides"] == []
    assert runner.ticks == []
    runner.queue.reject(result["spec_ids"][0], actor="fixture-human", message="withdrawn")
    withdrawn = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert withdrawn["metadata"]["incomplete_reason"] == "withdrawn"
    assert runner.ticks == []


@pytest.mark.parametrize("bad_result", [
    {"finished_at": STAMP, "exception_info": {"exception_type": "ProviderError"},
     "verifier_result": {"rewards": {"reward": 0.0}}},
    {"finished_at": STAMP, "verifier_result": {"rewards": {}}},
    {"finished_at": STAMP, "verifier_result": {"rewards": {"reward": "1.0"}}},
])
def test_native_failures_are_none_and_cannot_clear_missing_side(campaign, bad_result):
    evaluator, runner, _ = campaign
    result = prepare(evaluator)
    approve(runner, result)
    runner.outcomes[result["spec_ids"][0]] = bad_result
    measured = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert measured["metrics"]["candidate_scores"] == (None, 1.0)
    assert measured["metrics"]["current_scores"] == (1.0, 1.0)
    assert measured["metrics"]["episode_failures"] == 1
    assert measured["metrics"]["evaluation_sides"] == ["current"]
    assert measured["metadata"]["complete"] is False
    assert measured["metadata"]["evidence"][0]["trial_dir"]


def test_completed_retry_reuses_results_and_preserves_pairing_order(campaign):
    evaluator, runner, _ = campaign
    result = prepare(evaluator)
    approve(runner, result)
    first = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    repeated = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert runner.ticks == result["spec_ids"]
    assert runner.submissions == result["spec_ids"]
    assert first["metrics"] == repeated["metrics"]
    assert repeated["metadata"]["complete"] is True
    assert all(row["trial_id"] for row in repeated["metadata"]["evidence"])


def test_queued_contract_tampering_refuses_before_execution(campaign):
    evaluator, runner, _ = campaign
    result = prepare(evaluator)
    path = runner.queue.locate(result["spec_ids"][0])
    spec = json.loads(path.read_text())
    spec["max_requests"] += 1
    path.write_text(json.dumps(spec))
    with pytest.raises(LabGateError, match="submitted spec differs"):
        evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert runner.ticks == []


def test_queue_stop_prevents_dispatch(campaign):
    evaluator, runner, _ = campaign
    result = prepare(evaluator)
    approve(runner, result)
    runner.queue.stop()
    outcome = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert outcome["metadata"]["incomplete_reason"] == "budget_stopped"
    assert runner.ticks == []


def test_running_prior_episode_blocks_later_dispatch(campaign):
    evaluator, runner, _ = campaign
    prepared = prepare(evaluator)
    approve(runner, prepared)
    first = runner.queue.locate(prepared["spec_ids"][0])
    runner.queue.transition(first, "running", actor="test-runner", event="started")
    result = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert result["metadata"]["incomplete_reason"] == "spec_timeout"
    assert runner.ticks == []


def test_approval_timeout_keeps_already_observed_score(campaign):
    evaluator, runner, root = campaign
    prepared = prepare(evaluator)
    first_id = prepared["spec_ids"][0]
    runner.queue.approve(first_id, actor="fixture-human")
    runner.run(["tick", "--spec-id", first_id], cwd=root, timeout=1, python=Path("unused"))
    result = evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert result["metadata"]["incomplete_reason"] == "approval_timeout"
    assert result["metrics"]["candidate_scores"] == (1.0, None)
    assert result["metrics"]["current_scores"] == (None, None)
    assert result["metrics"]["evaluation_sides"] == []
    assert runner.ticks == [first_id]


def test_retained_pair_removal_cannot_shrink_campaign(campaign):
    evaluator, runner, _ = campaign
    prepared = prepare(evaluator)
    manifest_path = Path(prepared["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    manifest["pairings"].pop()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(LabGateError, match="pairing or tree identity"):
        evaluator.evaluate("candidate", CURRENT, CANDIDATE, TASKS)
    assert runner.submissions == prepared["spec_ids"]
    assert runner.ticks == []
