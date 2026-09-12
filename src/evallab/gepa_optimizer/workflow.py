"""Released GEPA search over instruction artifacts and actual Lab evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cohort import write_comparison

from .evaluator import (
    DEEPSEEK_TARGET_AGENT,
    PROVIDER_CEILING_FIELDS,
    EvaluationPending,
    LabEvaluator,
    ProviderCeilings,
)
from .proposer import JournaledReflectionLM, ProposalUnavailable
from .release import verify_release


class _EvaluationHalt(BaseException):
    """Stop engines that otherwise turn ordinary evaluator errors into bad scores."""

    def __init__(self, cause: Exception):
        self.cause = cause


def _path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Campaign paths must be repository-relative")
    path = root / relative
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Campaign paths must not traverse symlinks")
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise ValueError("Campaign path escapes repository")
    return path


def _write(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, allow_nan=False)
        stream.write("\n")


def load_campaign(path: Path, repo_root: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    allowed = {
        "name",
        "engine",
        "agent",
        "model",
        "seed_candidate_path",
        "examples",
        "validation_task_ids",
        "max_evals",
        "max_proposer_cost_usd",
        "proposer_model",
        "max_iterations",
        "max_candidates_per_iter",
        "output_dir",
        "estimated_cost_usd",
        "timeout_seconds",
        "objective",
        "max_proposer_requests",
        "provider_ceilings",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Unknown campaign fields; arbitrary engine configuration is not supported")
    for key in (
        "name",
        "engine",
        "agent",
        "seed_candidate_path",
        "output_dir",
        "examples",
        "max_evals",
    ):
        if key not in raw:
            raise ValueError(f"Missing campaign field: {key}")
    if raw["engine"] not in {"gepa", "meta_harness"}:
        raise ValueError("Only released gepa and meta_harness engines are supported")
    for key in (
        "max_evals",
        "max_iterations",
        "max_candidates_per_iter",
        "timeout_seconds",
        "max_proposer_requests",
    ):
        value = raw.get(key, 1)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("max_proposer_cost_usd", "estimated_cost_usd"):
        value = raw.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                f"{key} must be a genuine positive finite estimate/cap, not unknown-as-zero"
            )
    if not isinstance(raw["examples"], list) or not raw["examples"]:
        raise ValueError("An explicit nonempty development task list is required")
    example_fields = {"task_id", "task_path", "task_package_digest", "split"}
    if any(
        not isinstance(example, dict) or set(example) != example_fields
        for example in raw["examples"]
    ):
        raise ValueError(
            "Search examples may contain only declared task identity, path, digest and development split"
        )
    if any(example.get("split") != "development" for example in raw["examples"]):
        raise ValueError("Only development examples may enter search; no final/test split")
    ids = [example["task_id"] for example in raw["examples"]]
    val_ids = raw.get("validation_task_ids", [])
    if (
        len(set(ids)) != len(ids)
        or len(set(val_ids)) != len(val_ids)
        or not set(val_ids) <= set(ids)
    ):
        raise ValueError("Task identifiers must be unique and validation IDs explicitly declared")
    if not set(ids) - set(val_ids):
        raise ValueError("At least one development task must remain in the train pool")
    ceilings_raw = raw.get("provider_ceilings")
    if ceilings_raw is not None:
        if not isinstance(ceilings_raw, dict) or set(ceilings_raw) != set(
            PROVIDER_CEILING_FIELDS
        ):
            raise ValueError(
                "provider_ceilings must be an object with exactly the keys "
                f"{sorted(PROVIDER_CEILING_FIELDS)}"
            )
        try:
            ProviderCeilings(**ceilings_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid provider_ceilings: {exc}") from exc
    if raw["agent"] == DEEPSEEK_TARGET_AGENT and ceilings_raw is None:
        raise ValueError(
            f"DeepSeek target '{DEEPSEEK_TARGET_AGENT}' requires explicit provider_ceilings"
        )
    if raw["agent"] in {"oracle", "nop"} and ceilings_raw is not None:
        raise ValueError("Local controls do not accept provider_ceilings")
    _path(repo_root.resolve(), raw["seed_candidate_path"])
    _path(repo_root.resolve(), raw["output_dir"])
    return raw


class QualificationProposer:
    """Deterministic interface fixture, never a model or learned improvement."""

    total_cost = 0.0

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, candidate, reflective_dataset, components_to_update, **kwargs):
        self.calls += 1
        return {
            key: candidate[key]
            + "\nInspect the final task outputs against the stated requirements.\n"
            for key in components_to_update
        }


def run_campaign(
    config_path: Path,
    *,
    repo_root: Path,
    qualification: bool = False,
    proposer_approval_ref: Path | None = None,
) -> dict[str, Any]:
    """Run released search; targets remain governed by Lab approval/dispatch.

    A proposer approval reference records an operator-supplied authorization;
    it does not approve target evaluations or constitute a machine-issued grant.
    Qualification uses oracle/nop plus a deterministic proposal fixture only.
    """
    repo_root = repo_root.resolve()
    config = load_campaign(config_path, repo_root)
    pin = verify_release()
    if qualification and (config["engine"] != "gepa" or config["agent"] not in {"oracle", "nop"}):
        raise ValueError("Interface qualification requires gepa and an oracle/nop local control")
    authorization_bytes = None
    if not qualification:
        if proposer_approval_ref is None or not proposer_approval_ref.is_file():
            raise PermissionError(
                "Actual proposer execution needs an explicit authorization reference"
            )
        if config.get("max_proposer_cost_usd") is None or not config.get("proposer_model"):
            raise ValueError(
                "Actual proposer execution requires a pinned model and separate proposer cost cap"
            )
        authorization_bytes = proposer_approval_ref.read_bytes()
    output = _path(repo_root, config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    seed = _path(repo_root, config["seed_candidate_path"]).read_text(encoding="utf-8")
    if not seed.strip():
        raise ValueError("An explicit nonempty seed instruction is required")
    binding = {
        "config": config,
        "seed_sha256": "sha256:" + hashlib.sha256(seed.encode()).hexdigest(),
        "release": pin,
        "qualification": qualification,
    }
    if authorization_bytes is not None:
        authorization = json.loads(authorization_bytes)
        binding_sha256 = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        if (
            authorization.get("binding_sha256") != binding_sha256
            or not authorization.get("approved_by")
            or not authorization.get("approved_at")
        ):
            raise PermissionError(
                "Proposer authorization must identify this exact frozen campaign binding and its operator/date"
            )
    binding_path = output / "campaign.json"
    if binding_path.exists():
        if json.loads(binding_path.read_text()) != binding:
            raise ValueError("Resume configuration differs from the frozen campaign")
    else:
        _write(binding_path, binding)
    attempt = output / ("attempt-" + uuid.uuid4().hex)
    attempt.mkdir()
    ceilings = (
        ProviderCeilings(**config["provider_ceilings"])
        if config.get("provider_ceilings") is not None
        else None
    )
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output / "lab",
        examples=config["examples"],
        agent=config["agent"],
        model=config.get("model"),
        timeout_seconds=config.get("timeout_seconds", 1200),
        estimated_cost_usd=config.get("estimated_cost_usd"),
        ceilings=ceilings,
    )
    validation_ids = set(config.get("validation_task_ids", []))
    train = [row for row in config["examples"] if row["task_id"] not in validation_ids]
    validation = [row for row in config["examples"] if row["task_id"] in validation_ids]
    engine = None
    result = None
    fixture = QualificationProposer() if qualification else None
    reflection_lm = None
    status = "running"
    error_type = None
    error = None

    def evaluate(candidate, example):
        try:
            return evaluator(candidate, example)
        except Exception as exc:
            raise _EvaluationHalt(exc) from exc

    try:
        # Resolve the baseline target gate before any paid proposer can start.
        for example in config["examples"]:
            evaluate(seed, example)
        from gepa.optimize_anything import (  # ty: ignore[unresolved-import]
            OptimizeAnythingConfig,
            optimize_anything,
        )

        engine_options: dict[str, Any]
        if config["engine"] == "gepa":
            if not qualification:
                reflection_lm = JournaledReflectionLM(
                    model=config["proposer_model"],
                    directory=output / "proposer",
                    max_requests=config.get("max_proposer_requests", 1),
                )
            engine_options = {
                "reflection": {
                    "reflection_lm": reflection_lm,
                    "custom_candidate_proposer": fixture,
                    "reflection_minibatch_size": 1,
                    "skip_perfect_score": False,
                },
                "engine": {"seed": 0},
            }
        else:
            engine_options = {
                "model": config["proposer_model"],
                "max_iterations": config.get("max_iterations", 1),
                "max_candidates_per_iter": config.get("max_candidates_per_iter", 1),
            }
        upstream_config = OptimizeAnythingConfig(
            engine=config["engine"],
            name=config["name"],
            max_evals=config["max_evals"],
            max_concurrency=1,
            max_token_cost=None if qualification else config["max_proposer_cost_usd"],
            output_dir=attempt / "upstream",
            run_dir=str(output / "search-state"),
            sandbox=True,
            engine_config=engine_options,
        )
        if config["engine"] == "meta_harness":
            from .meta_engine import make_meta_harness_engine

            engine = make_meta_harness_engine(upstream_config)
            upstream_config.engine = engine
        result = optimize_anything(
            seed_candidate=seed,
            evaluator=evaluate,
            dataset=train,
            valset=validation or None,
            test_set=None,
            config=upstream_config,
            objective=config.get(
                "objective", "Improve task success with general supplementary instructions"
            ),
            background=(
                "Candidates are supplementary instruction text, not executable host code. "
                "All supplied train and validation examples are search-visible development tasks. "
                "No task-specific answers, verifier content or final evaluation tasks are permitted."
            ),
        )
        status = "completed"
    except _EvaluationHalt as halt:
        status = (
            "pending_evaluation"
            if isinstance(halt.cause, EvaluationPending)
            else "evaluation_failed"
        )
        error_type = type(halt.cause).__name__
        error = str(halt.cause)
    except ProposalUnavailable as exc:
        status = "proposer_unavailable"
        error_type = type(exc).__name__
        error = str(exc)
    except Exception as exc:
        status = "optimizer_failed"
        error_type = type(exc).__name__
        error = str(exc)
    finally:
        if engine is not None:
            engine.retain(attempt / "upstream")

    records = [asdict(record) for record in evaluator.records]
    selection = None
    comparison_paths = None
    complete = bool(records) and all(
        row["status"] == "completed" and row["score"] is not None for row in records
    )
    if result is not None and complete and status == "completed":
        best = result.best_candidate
        if not isinstance(best, str):
            raise ValueError("Released optimizer returned a non-text candidate")
        candidate_id = "sha256:" + hashlib.sha256(best.encode()).hexdigest()
        selected_records = [row for row in records if row["candidate_id"] == candidate_id]
        required = {row["task_id"] for row in (validation or train)}
        if required <= {row["task_id"] for row in selected_records} and math.isfinite(
            result.best_score
        ):
            selection = {
                "candidate_id": candidate_id,
                "text": best,
                "score": result.best_score,
                "search_visible_task_ids": sorted(required),
                "final_holdout_claim": False,
            }
        else:
            status = "incomplete_selection_coverage"
        candidates = list(dict.fromkeys(row["candidate_id"] for row in records))
        if len(candidates) >= 2:
            spec = evaluator.write_comparison_spec(candidates)
            json_path, markdown_path, _ = write_comparison(
                spec, repo_root=repo_root, output_root=attempt / "comparison"
            )
            comparison_paths = {
                "spec": str(spec),
                "json": str(json_path),
                "markdown": str(markdown_path),
            }
    elif status == "completed":
        status = "incomplete_evaluation"
    report = {
        "schema_version": 1,
        "status": status,
        "error_type": error_type,
        "error": error,
        "created_at": datetime.now(UTC).isoformat(),
        "attempt_dir": str(attempt),
        "release": pin,
        "engine": config["engine"],
        "evidence_level": "real_gepa_with_local_controls_and_deterministic_proposer"
        if qualification
        else "model_search",
        "proposer_authorization_ref": str(proposer_approval_ref) if proposer_approval_ref else None,
        "proposer_authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest()
        if authorization_bytes
        else None,
        "proposer": {
            "origin": "deterministic_interface_fixture"
            if qualification
            else config["proposer_model"],
            "calls": fixture.calls
            if fixture
            else (reflection_lm.new_requests if reflection_lm else None),
            "replayed_responses": reflection_lm.replayed if reflection_lm else 0,
            "reported_cost_usd": 0.0
            if qualification
            else (result.metadata.get("adapter_cost") if result else None),
            "actual_billing_cost_usd": None,
            "accounting_basis": "fixture_no_model"
            if qualification
            else "upstream_reported_estimate_not_complete_provider_billing",
            "cost_cap_excludes_target_evaluation": True,
        },
        "target_evaluations": records,
        "selection": selection,
        "comparison": comparison_paths,
        "upstream_eval_calls": result.total_evals if result is not None else None,
        "target_trial_jobs": sorted({row["job_path"] for row in records if row["job_path"]}),
        "sealed_final_tasks_supplied": False,
        "model_improvement_claimed": False,
    }
    _write(attempt / "result.json", report)
    return report
