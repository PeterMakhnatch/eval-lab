"""Approval interruption and sealed-input boundaries, not benchmark evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import evallab.gepa_optimizer.workflow as workflow
from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR
from evallab.gepa_optimizer.evaluator import DEEPSEEK_TARGET_AGENT
from evallab.gepa_optimizer.proposer import JournaledReflectionLM, ProposalUnavailable
from evallab.gepa_optimizer.workflow import QualificationProposer, _EvaluationHalt, load_campaign
from evallab.registry import task_directory_digest
from evallab.schemas import ExperimentSpec


def test_completed_proposal_is_replayed_without_another_model_call(tmp_path, monkeypatch):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    import gepa.lm

    calls = []

    class ResponseModel:
        total_cost = 0.0

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            calls.append(prompt)
            return "retained proposal"

    monkeypatch.setattr(gepa.lm, "LM", ResponseModel)
    first = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=1)
    assert first("development feedback") == "retained proposal"
    resumed = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=1)
    assert resumed("development feedback") == "retained proposal"
    with pytest.raises(ProposalUnavailable):
        resumed("different feedback")
    assert calls == ["development feedback"]


def test_ambiguous_model_failure_is_not_automatically_retried(tmp_path, monkeypatch):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    import gepa.lm

    calls = []

    class FailedModel:
        total_cost = 0.0

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            calls.append(prompt)
            raise TimeoutError("response outcome unknown")

    monkeypatch.setattr(gepa.lm, "LM", FailedModel)
    first = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=2)
    with pytest.raises(TimeoutError):
        first("development feedback")
    resumed = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=2)
    with pytest.raises(ProposalUnavailable):
        resumed("development feedback")
    assert len(calls) == 1


def test_released_gepa_does_not_turn_lab_halt_into_zero_reward(tmp_path):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    def pending(candidate, example):
        raise _EvaluationHalt(RuntimeError("Lab evaluation unavailable"))

    with pytest.raises(_EvaluationHalt):
        optimize_anything(
            seed_candidate="seed",
            evaluator=pending,
            dataset=[{"id": "dev"}],
            config=OptimizeAnythingConfig(
                max_evals=4,
                max_concurrency=1,
                output_dir=tmp_path / "out",
                run_dir=str(tmp_path / "state"),
                engine_config={
                    "reflection": {
                        "reflection_lm": None,
                        "custom_candidate_proposer": QualificationProposer(),
                    }
                },
            ),
        )


def test_campaign_rejects_final_tasks_before_evaluator_construction(tmp_path: Path):
    config = {
        "name": "sealed",
        "engine": "gepa",
        "agent": "oracle",
        "max_evals": 4,
        "seed_candidate_path": "seed.txt",
        "output_dir": "out",
        "examples": [
            {
                "task_id": "sealed",
                "task_path": "tasks/sealed",
                "task_package_digest": "sha256:" + "a" * 64,
                "split": "final",
            }
        ],
    }
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_campaign(path, tmp_path)


# --- DeepSeek-first campaign target (HAR-23) ---

CEILINGS: dict[str, Any] = {
    "max_requests": 4,
    "max_input_tokens": 8000,
    "max_output_tokens": 2000,
    "max_total_tokens": 10000,
    "cost_limit_usd": 0.5,
}


def _write_task(repo_root: Path, task_rel_path: str = "tasks/task_1") -> dict[str, Any]:
    """Minimal valid task package with its example declaration."""
    task_dir = repo_root / task_rel_path
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text('name = "task-1"\nversion = "1.0"\n', encoding="utf-8")
    (task_dir / "instruction.md").write_text("Complete the task.\n", encoding="utf-8")
    (task_dir / "environment").mkdir(exist_ok=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")
    return {
        "task_id": Path(task_rel_path).name,
        "task_path": task_rel_path,
        "task_package_digest": task_directory_digest(task_dir),
        "split": "development",
    }


def _write_campaign(
    repo_root: Path,
    task: dict[str, Any],
    *,
    agent: str = DEEPSEEK_TARGET_AGENT,
    model: Any = DEEPSEEK_MODEL_SELECTOR,
    ceilings: Any = None,
    filename: str = "campaign.json",
) -> Path:
    """Campaign JSON with ceilings included unless explicitly overridden."""
    raw: dict[str, Any] = {
        "name": "deepseek-target-path",
        "engine": "gepa",
        "agent": agent,
        "model": model,
        "seed_candidate_path": "seed.txt",
        "examples": [task],
        "validation_task_ids": [],
        "max_evals": 2,
        "timeout_seconds": 600,
        "output_dir": "out/campaign",
        "estimated_cost_usd": 1.5,
        "max_proposer_cost_usd": 0.5,
        "proposer_model": "test/proposer",
        "objective": (
            "Qualify the DeepSeek target path; search-visible scores only, "
            "no held-out improvement claim"
        ),
    }
    if ceilings is None:
        raw["provider_ceilings"] = dict(CEILINGS)
    elif ceilings != "omit":
        raw["provider_ceilings"] = ceilings
    path = repo_root / filename
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_campaign_loader_requires_ceilings_for_deepseek(tmp_path: Path) -> None:
    """A DeepSeek campaign without provider_ceilings is refused at load time."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = _write_task(repo_root)
    path = _write_campaign(repo_root, task, ceilings="omit")

    with pytest.raises(ValueError, match="requires explicit provider_ceilings"):
        load_campaign(path, repo_root)


@pytest.mark.parametrize(
    "agent,model", [("oracle", None), ("baseline", "anthropic/claude-opus-4-6")]
)
def test_campaign_refuses_ceilings_without_a_supporting_runtime(
    tmp_path: Path, agent: str, model: str | None
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = _write_task(repo_root)
    path = _write_campaign(repo_root, task, agent=agent, model=model)
    with pytest.raises(ValueError):
        load_campaign(path, repo_root)
    raw = json.loads(path.read_text())
    del raw["provider_ceilings"]
    path.write_text(json.dumps(raw))
    assert load_campaign(path, repo_root)["agent"] == agent


def test_campaign_loader_rejects_malformed_ceilings(tmp_path: Path) -> None:
    """Ceilings with wrong keys or non-positive values are refused at load time."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = _write_task(repo_root)

    extra = dict(CEILINGS)
    extra["max_attempts"] = 1
    with pytest.raises(ValueError, match="exactly the keys"):
        load_campaign(_write_campaign(repo_root, task, ceilings=extra), repo_root)

    zeroed = dict(CEILINGS)
    zeroed["max_requests"] = 0
    with pytest.raises(ValueError, match="Invalid provider_ceilings"):
        load_campaign(_write_campaign(repo_root, task, ceilings=zeroed), repo_root)

    unbounded = dict(CEILINGS)
    unbounded["max_total_tokens"] = (
        unbounded["max_input_tokens"] + unbounded["max_output_tokens"] + 1
    )
    with pytest.raises(ValueError, match="Invalid provider_ceilings"):
        load_campaign(_write_campaign(repo_root, task, ceilings=unbounded), repo_root)


def test_deepseek_fixture_campaign_loads() -> None:
    """The committed DeepSeek fixture is accepted by the campaign loader."""
    repo_root = Path(__file__).resolve().parents[1]
    config = load_campaign(
        repo_root / "research/experiments/harness-gepa/deepseek-mini-swe.json",
        repo_root,
    )

    assert config["agent"] == DEEPSEEK_TARGET_AGENT
    assert config["model"] == DEEPSEEK_MODEL_SELECTOR
    assert set(config["provider_ceilings"]) == {
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "cost_limit_usd",
    }


class _StubExecutor:
    """Queue stub that records the submitted spec and stays pending (never admits)."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.submitted_specs: list[ExperimentSpec] = []

    def submit(self, spec: ExperimentSpec) -> tuple[Path, Any]:
        self.submitted_specs.append(spec)

        @dataclass
        class PendingDecision:
            admitted: bool = False
            policy_rule: str | None = None
            reason: str = "awaiting operator policy approval"

        spec_path = self.repo_root / "out" / f"{spec.name}.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        return spec_path, PendingDecision()


def test_run_campaign_deepseek_stops_pending_without_approving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_campaign on a DeepSeek campaign submits one ceilings-bound spec, then
    halts at EvaluationPending: the pending decision admits nothing."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = _write_task(repo_root)
    (repo_root / "seed.txt").write_text("Study the requirements before acting.\n")
    config_path = _write_campaign(repo_root, task)

    pin = {"commit": "test", "version": "test", "python_source_tree_sha256": "test"}
    monkeypatch.setattr(workflow, "verify_release", lambda: pin)

    stub = _StubExecutor(repo_root)
    real_evaluator = workflow.LabEvaluator

    def _factory(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("executor", stub)
        return real_evaluator(*args, **kwargs)

    monkeypatch.setattr(workflow, "LabEvaluator", _factory)

    config = load_campaign(config_path, repo_root)
    seed_sha256 = "sha256:" + hashlib.sha256((repo_root / "seed.txt").read_bytes()).hexdigest()
    binding = {
        "config": config,
        "seed_sha256": seed_sha256,
        "release": pin,
        "qualification": False,
    }
    binding_sha256 = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    approval_ref = repo_root / "approval.json"
    approval_ref.write_text(
        json.dumps(
            {
                "binding_sha256": binding_sha256,
                "approved_by": "test-operator",
                "approved_at": "2026-09-11",
            }
        ),
        encoding="utf-8",
    )

    report = workflow.run_campaign(
        config_path, repo_root=repo_root, proposer_approval_ref=approval_ref
    )

    assert report["status"] == "pending_evaluation"
    assert report["error_type"] == "EvaluationPending"
    assert len(stub.submitted_specs) == 1
    spec = stub.submitted_specs[0]
    assert spec.agent == DEEPSEEK_TARGET_AGENT
    assert spec.model == DEEPSEEK_MODEL_SELECTOR
    assert spec.max_requests == CEILINGS["max_requests"]
    assert spec.max_input_tokens == CEILINGS["max_input_tokens"]
    assert spec.max_output_tokens == CEILINGS["max_output_tokens"]
    assert spec.max_total_tokens == CEILINGS["max_total_tokens"]
    assert spec.cost_limit_usd == CEILINGS["cost_limit_usd"]
