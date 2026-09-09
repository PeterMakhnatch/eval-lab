#!/usr/bin/env python3
"""Compile HAR-14 frozen cohort members into existing ExperimentSpec documents.

Agent and root-model identifiers stay required execution parameters. This module
does not submit jobs, approve spend, or invent Harbor agent names.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.registry import TaskRegistry
from evallab.schemas import CohortComparisonSpec, ExperimentSpec

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
COHORT_PATH = HERE / "cohort.json"
QUESTION_REF = "research/experiments/harness-first/cohort.json"
GRID_ID = "harness-first-20260909"
SUBMITTED_BY = "env-factory"

ARM_ROLES = {
    "mini-swe": "baseline",
    "authors-rlm": "candidate",
}


def load_cohort(path: Path = COHORT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def member_for(cohort: dict[str, Any], task_id: str) -> dict[str, Any]:
    for member in cohort["members"]:
        if member["task_id"] == task_id:
            return member
    raise KeyError(f"task {task_id!r} is not in the frozen cohort")


def _spec_name(stage: str, task_id: str, arm_id: str) -> str:
    return f"hf14-{stage}-{task_id}-{arm_id}"


def compile_spec(
    member: dict[str, Any],
    *,
    arm_id: str,
    agent: str,
    model: str,
) -> ExperimentSpec:
    if arm_id not in ARM_ROLES:
        raise ValueError(f"unknown arm_id {arm_id!r}")
    if not agent.strip() or not model.strip():
        raise ValueError("agent and root model are required execution parameters")
    stage = member["stage"]
    task_id = member["task_id"]
    timeout = int(member["limits"]["timeout_seconds"])
    return ExperimentSpec.model_validate(
        {
            "schema_version": 1,
            "name": _spec_name(stage, task_id, arm_id),
            "hypothesis": (
                "Same-root harness contrast on frozen "
                f"{member['registered_ref']}; this cell does not claim RLM improvement."
            ),
            "purpose": "comparison",
            "question_ref": QUESTION_REF,
            "prereg": {
                "expected": (
                    "Operability and descriptive pass@1 on identical task and verifier "
                    "identities. Not a statistically established ranking."
                ),
                "decision_rule": (
                    "The event-summary canary pair must complete without an infrastructure "
                    "exception before remaining matrix specs are eligible for Integration "
                    "approval. Partial rewards and infrastructure failures stay visible. "
                    "n<=3 and k=1 cannot establish a win."
                ),
            },
            "task": member["registered_ref"],
            "task_path": member["task_path"],
            "agent": agent,
            "model": model,
            "environment": "docker",
            "jobs_dir": "runs",
            "attempts": 1,
            "concurrency": 1,
            "timeout_seconds": timeout,
            "submitted_by": SUBMITTED_BY,
            "priority": 80 if stage == "canary" else 40,
            "est_cost_usd": 0.0,
            "task_version": member["version"],
            "verifier_digest": member["digests"]["verifier"],
            "task_package_digest": member["digests"]["package"],
            "task_family": member["task_family"],
            "task_id": task_id,
            "grid_id": GRID_ID,
            "grid_point": {
                "arm": arm_id,
                "role": ARM_ROLES[arm_id],
                "stage": stage,
                "task_id": task_id,
            },
        }
    )


def compile_pair(
    cohort: dict[str, Any],
    task_id: str,
    *,
    baseline_agent: str,
    candidate_agent: str,
    model: str,
) -> dict[str, ExperimentSpec]:
    member = member_for(cohort, task_id)
    return {
        "mini-swe": compile_spec(
            member, arm_id="mini-swe", agent=baseline_agent, model=model
        ),
        "authors-rlm": compile_spec(
            member, arm_id="authors-rlm", agent=candidate_agent, model=model
        ),
    }


def compile_stage(
    cohort: dict[str, Any],
    stage: str,
    *,
    baseline_agent: str,
    candidate_agent: str,
    model: str,
) -> dict[str, dict[str, ExperimentSpec]]:
    if stage == "canary":
        task_ids = [cohort["canary_task_id"]]
    elif stage == "matrix":
        task_ids = list(cohort["remaining_task_ids"])
    else:
        raise ValueError(f"unknown stage {stage!r}")
    return {
        task_id: compile_pair(
            cohort,
            task_id,
            baseline_agent=baseline_agent,
            candidate_agent=candidate_agent,
            model=model,
        )
        for task_id in task_ids
    }


def resolve_compiled(specs: list[ExperimentSpec], repo_root: Path = REPO_ROOT) -> None:
    registry = TaskRegistry.from_repo(repo_root)
    for spec in specs:
        registry.resolve_spec(spec, repo_root)


def write_specs(pairs: dict[str, dict[str, ExperimentSpec]], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for arms in pairs.values():
        for spec in arms.values():
            path = dest / f"{spec.name}.json"
            path.write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
            written.append(path)
    return written


def build_comparison_spec(
    *,
    comparison_id: str,
    baseline_jobs: list[str],
    candidate_jobs: list[str],
) -> CohortComparisonSpec:
    if not baseline_jobs or not candidate_jobs:
        raise ValueError("both arms need at least one completed job path")
    return CohortComparisonSpec.model_validate(
        {
            "schema_version": 1,
            "comparison_id": comparison_id,
            "experiment_id": "HAR-14",
            "declared_variable": "agent_name",
            "mode": "exploratory",
            "reward_name": "reward",
            "pass_threshold": 1.0,
            "pass_k": [1],
            "budget_exhaustion_is_failure": False,
            "pairing_key": "task_digest",
            "cohorts": [
                {"label": "mini-swe", "paths": baseline_jobs},
                {"label": "authors-rlm", "paths": candidate_jobs},
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-agent", required=True)
    parser.add_argument("--candidate-agent", required=True)
    parser.add_argument("--model", required=True, help="Shared root model/checkpoint id")
    parser.add_argument(
        "--stage",
        choices=("canary", "matrix", "all"),
        default="canary",
        help="Canary is the only first launch; matrix requires canary operability plus remaining approval.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cohort = load_cohort()
    stages = ("canary", "matrix") if args.stage == "all" else (args.stage,)
    written: list[Path] = []
    compiled: list[ExperimentSpec] = []
    for stage in stages:
        pairs = compile_stage(
            cohort,
            stage,
            baseline_agent=args.baseline_agent,
            candidate_agent=args.candidate_agent,
            model=args.model,
        )
        compiled.extend(spec for arms in pairs.values() for spec in arms.values())
        written.extend(write_specs(pairs, args.out / stage))
    resolve_compiled(compiled)
    print(json.dumps({"wrote": [str(path) for path in written], "resolved": len(compiled)}, indent=2))


if __name__ == "__main__":
    main()
