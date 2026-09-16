"""Released GEPA search over instruction artifacts and actual Lab evaluations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cohort import write_comparison

from .budget import AggregateBudget, BudgetExhausted
from .evaluator import (
    DEEPSEEK_TARGET_AGENT,
    PROVIDER_CEILING_FIELDS,
    CandidateReviewRequired,
    EvaluationPending,
    EvaluationRecord,
    LabEvaluator,
    ProviderCeilings,
)
from .proposer import JournaledReflectionLM, ProposalUnavailable, ReplaySafeGepaEngine
from .release import verify_release


class CampaignStopped(BaseException):
    """Stop before the next optimizer request without cancelling an in-flight trial."""


def _check_running(output: Path) -> None:
    if (output / "STOP").exists():
        raise CampaignStopped("Campaign stopped; resume explicitly before further optimizer work")


def set_campaign_paused(config_path: Path, *, repo_root: Path, paused: bool) -> dict[str, Any]:
    root = repo_root.resolve()
    config = load_campaign(config_path, root)
    output = _path(root, config["output_dir"])
    marker = _path(root, str((output / "STOP").relative_to(root)))
    if paused:
        output.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
    return {
        "status": "stopped" if paused else "ready",
        "output_dir": str(output),
        "in_flight_trials_cancelled": False,
        "note": "Controls future optimizer requests only; queued trials retain their separate Lab lifecycle.",
    }


def approve_candidate(config_path: Path, *, repo_root: Path, candidate_id: str) -> dict[str, Any]:
    """Allow one retained candidate to reach the ordinary Lab submission gate."""
    root = repo_root.resolve()
    config = load_campaign(config_path, root)
    output = _path(root, config["output_dir"])
    digest = candidate_id.removeprefix("sha256:")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("candidate must be a complete SHA-256 digest")
    candidate = _path(root, str((output / "lab/candidates" / f"{digest}.txt").relative_to(root)))
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
        raise ValueError("Retained candidate bytes do not match the requested digest")
    approvals = _path(root, str((output / "reviewed-candidates").relative_to(root)))
    approvals.mkdir(parents=True, exist_ok=True)
    approval = _path(root, str((approvals / f"{digest}.json").relative_to(root)))
    if not approval.exists():
        _write(
            approval,
            {"candidate_id": "sha256:" + digest, "reviewed_at": datetime.now(UTC).isoformat()},
        )
    return {
        "status": "candidate_reviewed",
        "candidate_id": "sha256:" + digest,
        "candidate_path": str(candidate),
        "target_execution_authorized": False,
        "note": "Run the campaign to prepare evaluation; this does not approve spend or adopt instructions.",
    }


def campaign_status(config_path: Path, *, repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    config = load_campaign(config_path, root)
    output = _path(root, config["output_dir"])
    candidates = []
    for path in sorted((output / "lab/candidates").glob("*.txt")):
        path = _path(root, str(path.relative_to(root)))
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if path.stem != digest:
            raise ValueError("Retained candidate identity mismatch")
        candidates.append(
            {
                "candidate_id": "sha256:" + digest,
                "path": str(path),
                "text": text,
                "reviewed": (output / "reviewed-candidates" / f"{digest}.json").is_file(),
            }
        )
    reports = []
    for path in output.glob("attempt-*/result.json"):
        path = _path(root, str(path.relative_to(root)))
        reports.append(json.loads(path.read_text()))
    latest = max(reports, key=lambda row: row["created_at"]) if reports else None
    return {
        "status": "disabled"
        if not config.get("enabled", True)
        else ("stopped" if (output / "STOP").exists() else "ready"),
        "candidate_evaluation": config.get("candidate_evaluation", "review"),
        "candidates": candidates,
        "last_attempt": latest,
        "instructions_automatically_adopted": False,
    }


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


def _compute_request_efficiency_utility(
    source: Any,
    *,
    raw_score: float | None = None,
) -> tuple[float, int]:
    """Extract provider_usage from evaluator info or record and compute utility.

    score = 1 / (1 + n_physical_requests) iff native task reward is exactly 1.0 else 0.
    Incomplete, missing, or unreconciled provider accounting raises ValueError to halt.
    """
    usage: dict[str, Any]
    if isinstance(source, EvaluationRecord):
        usage = source.usage if isinstance(source.usage, dict) else {}
        extracted_score = source.score
    elif isinstance(source, dict):
        if "usage" in source and isinstance(source["usage"], dict):
            usage = source["usage"]
        else:
            usage = source
        extracted_score = source.get("score")
    else:
        raise ValueError(f"Invalid evaluation source type: {type(source).__name__}")

    effective_score = raw_score if raw_score is not None else extracted_score
    if (
        effective_score is None
        or isinstance(effective_score, bool)
        or not isinstance(effective_score, (int, float))
        or not math.isfinite(effective_score)
    ):
        raise ValueError(f"Native task reward must be a finite number: {effective_score!r}")

    if usage.get("native_request_accounting") == "missing":
        raise ValueError("Native request accounting is missing")
    if usage.get("remote_outcomes_pending"):
        raise ValueError("Remote outcomes pending in request accounting")

    identity = usage.get("identity")
    if isinstance(identity, dict):
        identity_status = identity.get("status")
        if identity_status != "matched":
            raise ValueError(f"Provider model identity status is not matched: {identity_status!r}")

    provider_usage = usage.get("provider_usage")
    if not isinstance(provider_usage, dict):
        raise ValueError("Missing or invalid provider_usage accounting")

    unresolved = provider_usage.get("unresolved_requests")
    if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved != 0:
        raise ValueError(f"Provider usage has unresolved requests: {unresolved!r}")

    calls = provider_usage.get("calls")
    if not isinstance(calls, list):
        raise ValueError("Provider usage calls must be a list")

    for idx, call in enumerate(calls, start=1):
        if not isinstance(call, dict):
            raise ValueError(f"Provider call at index {idx} is not a dictionary")
        if call.get("state") != "reconciled":
            raise ValueError(
                f"Provider call at index {idx} has unreconciled state: {call.get('state')!r}"
            )
        call_id = call.get("call_id")
        if call_id is not None:
            if isinstance(call_id, bool) or not isinstance(call_id, int) or call_id != idx:
                raise ValueError(
                    f"Provider call at index {idx} has invalid call_id: {call_id!r}"
                )

    totals = provider_usage.get("totals")
    if totals is not None:
        if not isinstance(totals, dict):
            raise ValueError("Provider usage totals must be a dictionary")
        total_requests = totals.get("requests")
        if total_requests is not None:
            if (
                isinstance(total_requests, bool)
                or not isinstance(total_requests, int)
                or total_requests < 0
            ):
                raise ValueError(f"Invalid requests count in totals: {total_requests!r}")
            if total_requests != len(calls):
                raise ValueError(
                    f"Provider totals.requests ({total_requests}) does not match calls length ({len(calls)})"
                )

    n_physical_requests = len(calls)
    if float(effective_score) == 1.0:
        utility = 1.0 / (1.0 + n_physical_requests)
    else:
        utility = 0.0

    return utility, n_physical_requests


def _compute_selection(
    *,
    result: Any,
    records: list[dict[str, Any]],
    binding: dict[str, Any],
    seed: str,
    required_task_ids: set[str],
    score_mode: str = "native_reward",
) -> tuple[dict[str, Any] | None, str | None]:
    best = result.best_candidate
    if not isinstance(best, str):
        raise ValueError("Released optimizer returned a non-text candidate")
    candidate_id = "sha256:" + hashlib.sha256(best.encode()).hexdigest()
    selected_records = [row for row in records if row["candidate_id"] == candidate_id]

    if not (
        required_task_ids <= {row["task_id"] for row in selected_records}
        and math.isfinite(result.best_score)
    ):
        return None, "incomplete_selection_coverage"

    seed_records = {
        row["task_id"]: row
        for row in records
        if row["candidate_id"] == binding["seed_sha256"] and row["task_id"] in required_task_ids
    }
    cand_records = {
        row["task_id"]: row
        for row in selected_records
        if row["task_id"] in required_task_ids
    }
    if set(seed_records) != required_task_ids or set(cand_records) != required_task_ids:
        return None, "incomplete_selection_coverage"

    seed_native_scores = {t: float(seed_records[t]["score"]) for t in required_task_ids}
    cand_native_scores = {t: float(cand_records[t]["score"]) for t in required_task_ids}
    seed_mean_native = sum(seed_native_scores.values()) / len(required_task_ids)
    cand_mean_native = sum(cand_native_scores.values()) / len(required_task_ids)

    if score_mode == "quality_gated_request_efficiency":
        seed_utilities = {}
        seed_calls_map = {}
        cand_utilities = {}
        cand_calls_map = {}
        for t in required_task_ids:
            s_util, s_calls = _compute_request_efficiency_utility(
                seed_records[t], raw_score=seed_native_scores[t]
            )
            seed_utilities[t] = s_util
            seed_calls_map[t] = s_calls
            c_util, c_calls = _compute_request_efficiency_utility(
                cand_records[t], raw_score=cand_native_scores[t]
            )
            cand_utilities[t] = c_util
            cand_calls_map[t] = c_calls

        seed_mean_utility = sum(seed_utilities.values()) / len(required_task_ids)
        cand_mean_utility = sum(cand_utilities.values()) / len(required_task_ids)
        seed_total_calls = sum(seed_calls_map.values())
        cand_total_calls = sum(cand_calls_map.values())

        has_per_task_native_regression = any(
            cand_native_scores[t] < seed_native_scores[t] for t in required_task_ids
        )
        if cand_mean_utility <= seed_mean_utility or has_per_task_native_regression:
            chosen_id = binding["seed_sha256"]
            chosen_text = seed
            final_score = seed_mean_utility
            final_utility = seed_mean_utility
            final_native = seed_mean_native
            final_calls = seed_total_calls
        else:
            chosen_id = candidate_id
            chosen_text = best
            final_score = cand_mean_utility
            final_utility = cand_mean_utility
            final_native = cand_mean_native
            final_calls = cand_total_calls

        selection = {
            "candidate_id": chosen_id,
            "text": chosen_text,
            "score": final_score,
            "score_mode": score_mode,
            "utility": final_utility,
            "native_quality": final_native,
            "calls": final_calls,
            "selection_rule": "common_pool_utility_seed_retained_on_tie_or_native_regression",
            "search_visible_task_ids": sorted(required_task_ids),
            "final_holdout_claim": False,
        }
    else:
        if cand_mean_native <= seed_mean_native:
            chosen_id, chosen_text, selected_score = (
                binding["seed_sha256"],
                seed,
                seed_mean_native,
            )
        else:
            chosen_id, chosen_text, selected_score = (
                candidate_id,
                best,
                cand_mean_native,
            )
        selection = {
            "candidate_id": chosen_id,
            "text": chosen_text,
            "score": selected_score,
            "score_mode": score_mode,
            "utility": selected_score,
            "native_quality": selected_score,
            "calls": None,
            "selection_rule": "common_pool_mean_seed_retained_on_tie_or_regression",
            "search_visible_task_ids": sorted(required_task_ids),
            "final_holdout_claim": False,
        }

    return selection, None


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
        "enabled",
        "candidate_evaluation",
        "feedback_max_chars",
        "max_target_attempts",
        "shared_budget",
        "score_mode",
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
    if raw["engine"] not in {"gepa", "meta_harness", "omni"}:
        raise ValueError(
            "Only released gepa, meta_harness and genuine omni composition are supported"
        )
    if not isinstance(raw.get("enabled", True), bool):
        raise ValueError("enabled must be a boolean")
    if raw.get("candidate_evaluation", "review") not in {"review", "automatic"}:
        raise ValueError("candidate_evaluation must be review or automatic")
    score_mode = raw.get("score_mode", "native_reward")
    if not isinstance(score_mode, str) or score_mode not in {
        "native_reward",
        "quality_gated_request_efficiency",
    }:
        raise ValueError(
            "score_mode must be 'native_reward' or 'quality_gated_request_efficiency'"
        )
    for key in (
        "max_evals",
        "max_iterations",
        "max_candidates_per_iter",
        "timeout_seconds",
        "max_proposer_requests",
        "feedback_max_chars",
        "max_target_attempts",
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
        if not isinstance(ceilings_raw, dict) or set(ceilings_raw) != set(PROVIDER_CEILING_FIELDS):
            raise ValueError(
                "provider_ceilings must be an object with exactly the keys "
                f"{sorted(PROVIDER_CEILING_FIELDS)}"
            )
        try:
            ProviderCeilings(**ceilings_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid provider_ceilings: {exc}") from exc
    if raw["agent"] in {DEEPSEEK_TARGET_AGENT, "zai-opencode"} and ceilings_raw is None:
        raise ValueError(f"Target '{raw['agent']}' requires explicit provider_ceilings")
    if ceilings_raw is not None and raw["agent"] not in {DEEPSEEK_TARGET_AGENT, "zai-opencode"}:
        raise ValueError(f"Target '{raw['agent']}' does not support provider_ceilings")
    if raw["engine"] == "omni" and (
        "max_target_attempts" not in raw
        or raw.get("max_proposer_requests", 1) < 4
        or raw["max_evals"] < 4
    ):
        raise ValueError("Omni requires a native attempt bound and budget for all four stages")
    shared = raw.get("shared_budget")
    if shared is not None:
        required = {
            "output_dir",
            "max_target_attempts",
            "max_proposer_requests",
            "max_proposer_cost_usd",
        }
        if (
            not isinstance(shared, dict)
            or set(shared) != required
            or "max_target_attempts" not in raw
        ):
            raise ValueError(
                "Shared budget requires exact group limits and a campaign native attempt cap"
            )
        _path(repo_root.resolve(), shared["output_dir"])
        for key in ("max_target_attempts", "max_proposer_requests"):
            value = shared[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Shared budget counts must be positive integers")
        value = shared["max_proposer_cost_usd"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("Shared proposer cost must be a finite positive estimate ceiling")
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
    """Explicit opt-in entrypoint; ordinary Lab runs never invoke optimization."""
    root = repo_root.resolve()
    config = load_campaign(config_path, root)
    if not config.get("enabled", True):
        return {"status": "disabled", "model_improvement_claimed": False}
    output = _path(root, config["output_dir"])
    if (output / "STOP").exists():
        return {"status": "stopped", "model_improvement_claimed": False}
    output.mkdir(parents=True, exist_ok=True)
    lock_path = _path(root, str((output / "campaign.lock").relative_to(root)))
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "model_improvement_claimed": False}
        try:
            return _run_campaign(
                config,
                repo_root=root,
                qualification=qualification,
                proposer_approval_ref=proposer_approval_ref,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_campaign(
    config: dict[str, Any],
    *,
    repo_root: Path,
    qualification: bool,
    proposer_approval_ref: Path | None,
) -> dict[str, Any]:
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
    score_mode = config.get("score_mode", "native_reward")
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
    budgets: tuple[AggregateBudget, ...] = ()
    if not qualification and "max_target_attempts" in config:
        local_budget = AggregateBudget(
            output / "budget",
            max_target_attempts=config["max_target_attempts"],
            max_proposer_requests=config.get("max_proposer_requests", 1),
            max_proposer_cost_usd=config["max_proposer_cost_usd"],
        )
        budgets = (local_budget,)
        if config.get("shared_budget") is not None:
            shared = config["shared_budget"]
            budgets += (
                AggregateBudget(
                    _path(repo_root, shared["output_dir"]),
                    max_target_attempts=shared["max_target_attempts"],
                    max_proposer_requests=shared["max_proposer_requests"],
                    max_proposer_cost_usd=shared["max_proposer_cost_usd"],
                ),
            )
    ceilings = (
        ProviderCeilings(**config["provider_ceilings"])
        if config.get("provider_ceilings") is not None
        else None
    )
    reviewed = {binding["seed_sha256"]}
    for path in (output / "reviewed-candidates").glob("*.json"):
        path = _path(repo_root, str(path.relative_to(repo_root)))
        approval = json.loads(path.read_text())
        if approval.get("candidate_id") != "sha256:" + path.stem:
            raise ValueError("Candidate review identity mismatch")
        reviewed.add(approval["candidate_id"])
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output / "lab",
        examples=config["examples"],
        agent=config["agent"],
        model=config.get("model"),
        timeout_seconds=config.get("timeout_seconds", 1200),
        estimated_cost_usd=config.get("estimated_cost_usd"),
        ceilings=ceilings,
        approved_candidate_ids=None
        if config.get("candidate_evaluation") == "automatic"
        else frozenset(reviewed),
        feedback_max_chars=config.get("feedback_max_chars", 24000),
        budgets=budgets,
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
    pending_candidate = None
    availability = None
    stage_lms: dict[str, JournaledReflectionLM] = {}

    def evaluate(candidate, example):
        try:
            _check_running(output)
            raw_score, info = evaluator(candidate, example)
            if score_mode == "quality_gated_request_efficiency":
                utility, calls = _compute_request_efficiency_utility(info, raw_score=raw_score)
                info["score_mode"] = score_mode
                info["native_quality"] = raw_score
                info["calls"] = calls
                info["utility"] = utility
                feedback_addition = (
                    f"\n\n[Evaluation metric: score_mode={score_mode} "
                    f"native_quality={raw_score} calls={calls} utility={utility}]"
                )
                if isinstance(info.get("feedback"), str):
                    info["feedback"] += feedback_addition
                else:
                    info["feedback"] = feedback_addition.strip()
                return utility, info
            return raw_score, info
        except Exception as exc:
            raise _EvaluationHalt(exc) from exc
    try:
        if config["engine"] == "omni":
            from .composition import EngineUnavailable, engine_availability, run_omni

            availability = engine_availability(config["proposer_model"])
            if not availability["all_available"]:
                raise EngineUnavailable(availability)
        # Resolve the baseline target gate before any paid proposer can start.
        for example in config["examples"]:
            evaluate(seed, example)
        from gepa.optimize_anything import (  # ty: ignore[unresolved-import]
            OptimizeAnythingConfig,
            optimize_anything,
        )

        objective = config.get(
            "objective", "Improve task success with general supplementary instructions"
        )
        background = (
            "Candidates are supplementary instruction text, not executable host code. "
            "All supplied train and validation examples are search-visible development tasks. "
            "No task-specific answers, verifier content or final evaluation tasks are permitted."
        )

        def make_config(stage_id: str, engine_name: str):
            nonlocal reflection_lm
            composing = config["engine"] == "omni"
            stages = 4 if composing else 1
            continuing = stage_id.startswith("continue-")
            requests = config.get("max_proposer_requests", 1)
            stage_requests = requests // stages + (requests % stages if continuing else 0)
            max_evals = config["max_evals"] // stages + (
                config["max_evals"] % stages if continuing else 0
            )
            stage_output = output / "stages" / stage_id if composing else output
            if engine_name == "gepa":
                if not qualification:
                    reflection_lm = JournaledReflectionLM(
                        model=config["proposer_model"],
                        directory=stage_output / "proposer",
                        max_requests=stage_requests,
                        before_request=lambda: _check_running(output),
                        budgets=budgets,
                    )
                    stage_lms[stage_id] = reflection_lm
                engine_options = {
                    "reflection": {
                        "reflection_lm": reflection_lm,
                        "custom_candidate_proposer": fixture,
                        "reflection_minibatch_size": 1,
                        "skip_perfect_score": False,
                    },
                    "engine": {"seed": 0, "max_candidate_proposals": stage_requests},
                }
            else:
                engine_options = {
                    "model": config["proposer_model"],
                    "max_iterations": config.get("max_iterations", 1),
                    "max_candidates_per_iter": config.get("max_candidates_per_iter", 1),
                }
            return OptimizeAnythingConfig(
                engine=engine_name,
                name=config["name"] + ("-" + stage_id if composing else ""),
                max_evals=max_evals,
                max_concurrency=1,
                max_token_cost=None if qualification else config["max_proposer_cost_usd"] / stages,
                output_dir=attempt / "upstream" / stage_id if composing else attempt / "upstream",
                run_dir=str(stage_output / "search-state"),
                sandbox=True,
                engine_config=engine_options,
            )

        if config["engine"] == "omni":
            result = run_omni(
                seed_candidate=seed,
                evaluator=evaluate,
                dataset=train,
                valset=validation or None,
                config_factory=make_config,
                output_dir=output / "omni",
                objective=objective,
                background=background,
                before_stage=lambda: _check_running(output),
                proposer_model=config["proposer_model"],
            )
        else:
            upstream_config = make_config(config["engine"], config["engine"])
            if config["engine"] == "meta_harness":
                from .meta_engine import make_meta_harness_engine

                engine = make_meta_harness_engine(upstream_config)
                upstream_config.engine = engine
            elif config["engine"] == "gepa":
                upstream_config.engine = ReplaySafeGepaEngine(upstream_config)
            upstream_config.engine_config = {}
            result = optimize_anything(
                seed_candidate=seed,
                evaluator=evaluate,
                dataset=train,
                valset=validation or None,
                test_set=None,
                config=upstream_config,
                objective=objective,
                background=background,
            )
        # Selection needs full common-pool evidence even after an upstream resume.
        for example in validation or train:
            evaluate(result.best_candidate, example)
        status = "completed"
    except _EvaluationHalt as halt:
        if isinstance(halt.cause, CandidateReviewRequired):
            status = "candidate_review_required"
            pending_candidate = {
                "candidate_id": halt.cause.candidate_id,
                "candidate_path": str(halt.cause.candidate_path),
            }
        else:
            status = (
                "pending_evaluation"
                if isinstance(halt.cause, EvaluationPending)
                else "evaluation_failed"
            )
        error_type = type(halt.cause).__name__
        error = str(halt.cause)
    except BudgetExhausted as exc:
        status = "budget_exhausted"
        error_type = type(exc).__name__
        error = str(exc)
    except CampaignStopped as exc:
        status = "stopped"
        error_type = type(exc).__name__
        error = str(exc)
    except ProposalUnavailable as exc:
        status = "proposer_unavailable"
        error_type = type(exc).__name__
        error = str(exc)
    except Exception as exc:
        status = (
            "engine_unavailable"
            if availability and not availability["all_available"]
            else "optimizer_failed"
        )
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
        selection, coverage_status = _compute_selection(
            result=result,
            records=records,
            binding=binding,
            seed=seed,
            required_task_ids=required,
            score_mode=score_mode,
        )
        if coverage_status:
            status = coverage_status
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
        "engine_availability": availability,
        "score_mode": score_mode,
        "budget_accounting": [
            {"directory": str(budget.directory), **budget.summary()} for budget in budgets
        ],
        "composition": result.metadata
        if result is not None and config["engine"] == "omni"
        else None,
        "evidence_level": (
            "engine_preflight_only"
            if status == "engine_unavailable"
            else "real_gepa_with_local_controls_and_deterministic_proposer"
            if qualification
            else "model_search"
        ),
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
            else (
                0
                if status == "engine_unavailable"
                else sum(lm.new_requests for lm in stage_lms.values())
                if stage_lms
                else None
            ),
            "replayed_responses": sum(lm.replayed for lm in stage_lms.values()),
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
        "pending_candidate": pending_candidate,
        "candidate_evaluation": config.get("candidate_evaluation", "review"),
        "instructions_automatically_adopted": False,
        "comparison": comparison_paths,
        "upstream_eval_calls": result.total_evals if result is not None else None,
        "target_trial_jobs": sorted({row["job_path"] for row in records if row["job_path"]}),
        "sealed_final_tasks_supplied": False,
        "model_improvement_claimed": False,
    }
    _write(attempt / "result.json", report)
    return report
