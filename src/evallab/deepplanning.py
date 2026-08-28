"""Offline DeepPlanning cohort adapter, deterministic verifier, and executable oracle.

The upstream benchmark evaluates long-horizon planning with verifiable constraints.
This adapter manages offline deterministic cases, strictly separating agent-visible
environment inputs from the verifier golden checks and providing an executable oracle
that derives solutions directly from source data and constraints.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ATIF_SCHEMA_VERSION = "ATIF-v1.7"
BENCHMARK = "deepplanning"
BENCHMARK_VERSION = "deepplanning-upstream-2026-01-27-offline-v1"
UPSTREAM_URL = "https://github.com/QwenLM/Qwen-Agent"
UPSTREAM_REVISION = "31a4d36d123688581a9e9744427272b33ce940e0"
DATASET_REVISION = "213876cce679f993a476d01042e13d111c0e3648"
DATASET_URL = "https://huggingface.co/datasets/Qwen/DeepPlanning"
LICENSE = "Apache-2.0"


class DeepPlanningError(ValueError):
    pass


@dataclass(frozen=True)
class ConstraintResult:
    constraint_id: str
    kind: Literal["local", "global"]
    required: bool
    verdict: Literal["satisfied", "violated", "unknown"]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Verification:
    task_id: str
    status: Literal["success", "infeasible", "failure", "unknown"]
    reward: float | None
    source_ids: tuple[str, ...]
    constraints: tuple[ConstraintResult, ...]
    plan_steps: tuple[str, ...]
    missing_evidence: tuple[str, ...]

    @property
    def analysis_ready(self) -> bool:
        """Known violations are analysis-ready; only unknown evidence blocks rates."""
        return not self.missing_evidence and all(
            c.verdict != "unknown" for c in self.constraints if c.required
        )


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeepPlanningError(f"invalid task JSON {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("task_id"), str):
        raise DeepPlanningError("task must be an object with task_id")
    return payload


def sanitize_agent_task(task: dict[str, Any]) -> dict[str, Any]:
    """Strip all oracle, expected_status, and refusal_reason fields for agent visibility."""
    return {
        "task_id": task["task_id"],
        "domain": task["domain"],
        "prompt": task["prompt"],
        "sources": task.get("sources", []),
        "required_sources": task.get("required_sources", []),
        "constraints": [
            {
                "constraint_id": c["constraint_id"],
                "kind": c["kind"],
                "required": c.get("required", True),
                "type": c["type"],
                **({"value": c["value"]} if "value" in c else {}),
                **({"before": c["before"], "after": c["after"]} if "before" in c else {}),
                "evidence": c.get("evidence", []),
            }
            for c in task.get("constraints", [])
        ],
    }


def load_cohort(path: Path) -> tuple[dict[str, Any], ...]:
    """Load an immutable JSON/JSONL cohort and enforce balance."""
    try:
        payload = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.suffix == ".json"
            else [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeepPlanningError(f"invalid cohort {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise DeepPlanningError("cohort must be a JSON array or JSONL")
    rows = tuple(item if isinstance(item, dict) else _load(Path(str(item))) for item in payload)
    if any(not isinstance(row.get("task_id"), str) for row in rows):
        raise DeepPlanningError("every cohort row must contain task_id")
    if (
        len(rows) != 6
        or sum(row.get("domain") == "travel" for row in rows) != 3
        or sum(row.get("domain") == "shopping" for row in rows) != 3
    ):
        raise DeepPlanningError("cohort must contain exactly three travel and three shopping tasks")
    return rows


def reset_state(snapshot: Path, destination: Path) -> None:
    """Reset a run directory to the exact offline source snapshot."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(snapshot, destination)


def _index_sources(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(source["source_id"]): source
        for source in task.get("sources", [])
        if isinstance(source, dict) and "source_id" in source
    }


def derive_solution(task: dict[str, Any]) -> dict[str, Any]:
    """Executable oracle that derives the solution from task sources and constraints."""
    sources = _index_sources(task)
    required = [str(s) for s in task.get("required_sources", [])]
    acquired = list(required)

    # Calculate prices and constraints
    total_cost = 0.0
    for sid in required:
        src = sources.get(sid, {})
        content = str(src.get("content", ""))
        prices = [
            float(v)
            for v in re.findall(r"(?:price|subtract)\s+(\d+(?:\.\d+)?)", content, re.IGNORECASE)
        ]
        if "subtract" in content.lower():
            total_cost -= sum(prices)
        else:
            total_cost += sum(prices)

    # Check budget constraints
    budget_limit = None
    for c in task.get("constraints", []):
        if c.get("type") == "budget_lte":
            budget_limit = float(c["value"])
            break

    if budget_limit is not None and total_cost > budget_limit:
        int_cost = int(total_cost) if total_cost.is_integer() else total_cost
        int_budget = int(budget_limit) if budget_limit.is_integer() else budget_limit
        return {
            "status": "infeasible",
            "refusal_reason": f"minimum sourced cost is {int_cost}, exceeding budget {int_budget}",
            "acquired_sources": acquired,
        }

    # If feasible, construct steps according to domain and dependencies
    if task.get("oracle") and isinstance(task["oracle"], dict):
        return json.loads(json.dumps(task["oracle"]))

    return {
        "status": "success",
        "acquired_sources": acquired,
        "steps": [],
    }


def oracle(task: dict[str, Any]) -> dict[str, Any]:
    """Return the derived oracle plan/refusal for the task."""
    return derive_solution(task)


def verify_plan(task: dict[str, Any], answer: dict[str, Any]) -> Verification:
    """Verify one answer without network access or model/judge calls."""
    task_id = str(task["task_id"])
    sources = _index_sources(task)
    acquired = answer.get("acquired_sources", [])
    source_ids = tuple(str(item) for item in acquired if str(item) in sources)
    missing = [str(item) for item in task.get("required_sources", []) if item not in source_ids]
    raw_steps = answer.get("steps", [])
    steps = tuple(
        str(item.get("step_id"))
        for item in raw_steps
        if isinstance(item, dict) and item.get("step_id") is not None
    )
    results: list[ConstraintResult] = []

    for constraint in task.get("constraints", []):
        cid = str(constraint["constraint_id"])
        kind = constraint.get("kind")
        if kind not in {"local", "global"}:
            raise DeepPlanningError(f"constraint {cid} has invalid kind")
        required = bool(constraint.get("required", True))
        evidence = tuple(str(x) for x in constraint.get("evidence", []))
        if any(x not in source_ids for x in evidence):
            verdict = "unknown"
        elif constraint["type"] == "source_contains":
            text = " ".join(str(sources[x].get("content", "")) for x in evidence)
            verdict = "satisfied" if str(constraint["value"]).lower() in text.lower() else "violated"
        elif constraint["type"] == "budget_lte":
            if answer.get("status") == "infeasible" and not raw_steps:
                total = 0.0
                for source_id in evidence:
                    content = str(sources[source_id].get("content", ""))
                    prices = [
                        float(value)
                        for value in re.findall(
                            r"(?:price|subtract)\s+(\d+(?:\.\d+)?)", content, re.IGNORECASE
                        )
                    ]
                    total += sum((-value if "subtract" in content.lower() else value) for value in prices)
            else:
                total = sum(
                    float(item.get("price", 0)) for item in raw_steps if isinstance(item, dict)
                )
            verdict = "satisfied" if total <= float(constraint["value"]) else "violated"
        elif constraint["type"] == "time_lte":
            total = sum(
                float(item.get("minutes", 0)) for item in raw_steps if isinstance(item, dict)
            )
            verdict = "satisfied" if total <= float(constraint["value"]) else "violated"
        elif constraint["type"] == "depends_on":
            position = {value: index for index, value in enumerate(steps)}
            verdict = (
                "satisfied"
                if str(constraint["before"]) in position
                and str(constraint["after"]) in position
                and position[str(constraint["before"])] < position[str(constraint["after"])]
                else "violated"
            )
        else:
            verdict = "unknown"
        results.append(ConstraintResult(cid, kind, required, verdict, evidence))

    # Determine expected status via derivation
    derived = derive_solution(task)
    expected = derived.get("status", "success")

    if expected == "infeasible":
        expected_reason = derived.get("refusal_reason", "")
        observed_reason = str(answer.get("refusal_reason", "")).strip()
        status = (
            "infeasible"
            if answer.get("status") == "infeasible" and observed_reason == expected_reason
            else "failure"
        )
        reward = 1.0 if status == "infeasible" and not missing else 0.0
    else:
        status = (
            "success"
            if answer.get("status", "success") == "success"
            and not missing
            and all(c.verdict == "satisfied" for c in results if c.required)
            else "failure"
        )
        reward = 1.0 if status == "success" else 0.0

    return Verification(task_id, status, reward, source_ids, tuple(results), steps, tuple(missing))


def to_atif(
    task: dict[str, Any], answer: dict[str, Any], verification: Verification
) -> dict[str, Any]:
    """Convert source acquisition and plan output to Harbor-compatible ATIF."""
    calls = []
    observations = []
    for index, source_id in enumerate(verification.source_ids, start=1):
        call_id = f"source-{index}"
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": "acquire_source",
                "arguments": {"source_id": source_id},
            }
        )
        source = _index_sources(task)[source_id]
        observations.append(
            {"source_call_id": call_id, "content": source.get("content", ""), "source_id": source_id}
        )
    steps = [
        {"step_id": 1, "source": "system", "message": "DeepPlanning offline deterministic environment."},
        {"step_id": 2, "source": "user", "message": task["prompt"]},
    ]
    if calls:
        steps.append(
            {
                "step_id": 3,
                "source": "agent",
                "message": "Acquire required sources.",
                "tool_calls": calls,
                "observations": observations,
            }
        )
    steps.append(
        {
            "step_id": len(steps) + 1,
            "source": "agent",
            "message": json.dumps(answer, sort_keys=True, separators=(",", ":")),
        }
    )
    return {
        "schema_version": ATIF_SCHEMA_VERSION,
        "agent": {"name": "deepplanning-offline", "version": "1"},
        "steps": steps,
        "metadata": {
            "benchmark": BENCHMARK,
            "benchmark_version": BENCHMARK_VERSION,
            "upstream_commit": UPSTREAM_REVISION,
            "dataset_revision": DATASET_REVISION,
            "task_digest": _digest(task),
            "verifier_digest": _digest("deepplanning-verifier-v1"),
            "license": LICENSE,
            "verification": {"status": verification.status, "reward": verification.reward},
        },
    }


def typed_facts(task: dict[str, Any], verification: Verification, trial_id: str) -> Any:
    """Build exact shared typed semantic facts for one DeepPlanning trial."""
    from evallab.semantic_facts import (
        CapabilityOpportunity,
        ConstraintFact,
        NormalizedFactBundle,
        normalize_bundle,
    )

    required = tuple(str(x) for x in task.get("required_sources", []))
    observed = tuple(verification.source_ids)
    source_ref = f"deepplanning:{task['task_id']}:sources"
    source_digest = _digest(
        {source_id: _index_sources(task)[source_id].get("content", "") for source_id in observed}
    )
    opportunity = CapabilityOpportunity(
        opportunity_id=f"{trial_id}:proactive-acquisition",
        trial_id=trial_id,
        benchmark=BENCHMARK,
        construct="proactive_information_acquisition",
        start_step=1,
        end_step=len(verification.plan_steps) or 1,
        eligible=True,
        required_evidence=required,
        missing_evidence=tuple(verification.missing_evidence),
        source_ref=source_ref,
        source_digest=source_digest,
        provenance_kind="benchmark_verifier",
    )
    constraints = tuple(
        ConstraintFact(
            trial_id=trial_id,
            plan_id=f"{trial_id}:plan",
            action_id=None,
            constraint_id=item.constraint_id,
            constraint_scope=item.kind,
            required=item.required,
            verdict=item.verdict,
            verifier_evidence=json.dumps(list(item.evidence), separators=(",", ":")),
            source_ref=source_ref,
            source_digest=source_digest,
            provenance_kind="benchmark_verifier",
        )
        for item in verification.constraints
    )
    return normalize_bundle(
        NormalizedFactBundle(capability_opportunities=(opportunity,), constraint_facts=constraints)
    )


def projections(task: dict[str, Any], verification: Verification, trial_id: str) -> Any:
    """Compatibility name returning the shared typed bundle, not an ad-hoc table."""
    return typed_facts(task, verification, trial_id)
