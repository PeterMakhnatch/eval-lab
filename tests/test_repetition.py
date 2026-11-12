"""Focused tests for deterministic, resume-safe repeated attempt matrix (HAR-63).

Covers:
1. Idempotent deterministic attempt IDs (ULID derived from sha256(task_id + attempt_index)).
2. Complete repeated-attempt matrix preparation from compiled TB4 plan JSON.
3. Explicit gate: preparing 66x2 authorizes zero executions; authorization defaults to False.
4. Resume merge: completed, failed, timeout, and unknown cases merge without duplicating or hiding.
5. Charge honesty: failed and unknown attempts stay charged (unknown not refunded).
6. Cost aggregation correctness across pending and settled attempts.
7. CLI interface for prepare and resume operations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.campaigns import (
    TrialLimits,
)
from evallab.campaigns import (
    plan_repeated_matrix as campaign_plan_repeated_matrix,
)
from evallab.campaigns import (
    resume_repeated_matrix as campaign_resume_repeated_matrix,
)
from evallab.repetition import (
    ZAI_OPENAPI_DEFAULT_MAX_COST_USD,
    ZAI_OPENAPI_DEFAULT_MAX_INPUT_TOKENS,
    ZAI_OPENAPI_DEFAULT_MAX_OUTPUT_TOKENS,
    ZAI_OPENAPI_DEFAULT_MAX_REQUESTS,
    ZAI_OPENAPI_DEFAULT_MAX_TOTAL_TOKENS,
    RepeatedAttemptMatrix,
    deterministic_attempt_id,
    plan_repeated_matrix,
    resume_repeated_matrix,
)
from evallab.repetition import (
    main as repetition_main,
)


def _make_tb4_plan(
    num_tasks: int = 66,
    *,
    model: str = "zai/glm-5.3-flash",
    agent: str = "mini-swe-agent",
    timeout_seconds: int = 28800,
) -> dict[str, object]:
    """Create a valid compiled TB4 plan dictionary matching craft.compile_tb4 output."""
    tasks = []
    gpu_refs = {
        "terminal-bench/fp8-rmsnorm-gemm",
        "terminal-bench/jax-speedrun-gpu",
        "terminal-bench/math-eval-grader",
    }
    for i in range(num_tasks):
        ref = f"terminal-bench/task-{i:03d}" if i >= 3 else list(gpu_refs)[i]
        is_gpu = ref in gpu_refs
        tasks.append(
            {
                "task_id": f"01JTB4TASK{i:016d}",
                "task_ref": ref,
                "task_digest": f"sha256:{i:064x}",
                "timeout_seconds": timeout_seconds,
                "agent": agent,
                "model": model,
                "environment": "modal" if is_gpu else "docker",
                **({"gpu_types": ["H100"]} if is_gpu else {}),
            }
        )

    return {
        "plan_version": "tb4-job-plan/1",
        "command": "craft compile",
        "dataset_ref": "terminal-bench/terminal-bench@4.0.0",
        "source_identity": "terminal-bench/terminal-bench@4.0.0",
        "versions": {"from": "3.0.0", "to": "4.0.0"},
        "pin": {
            "tag": "v4.0.0",
            "commit": "452bf30",
            "license": "Apache-2.0",
            "schema_unchanged": True,
        },
        "resource_contract": f"flat 8-hour agent timeout on all {num_tasks} tasks",
        "timeout_seconds": timeout_seconds,
        "task_count": num_tasks,
        "selected_task_count": num_tasks,
        "provider": {
            "provider_family": "zai-openapi",
            "agent": agent,
            "selected_agent": agent,
            "selected_model": model,
            "allowed_models": [model],
            "highspeed": "refused",
        },
        "environment_routing": {
            "default": "docker",
            "remote_for_gpu": "modal",
            "gpu_task_refs": sorted(gpu_refs),
        },
        "tasks": tasks,
    }


def test_deterministic_attempt_id_is_idempotent() -> None:
    task_id = "01JTB4TASK0000000000000001"
    id_att1_run1 = deterministic_attempt_id(task_id, 1)
    id_att1_run2 = deterministic_attempt_id(task_id, 1)
    assert id_att1_run1 == id_att1_run2
    assert len(id_att1_run1) == 26

    id_att2 = deterministic_attempt_id(task_id, 2)
    assert id_att1_run1 != id_att2
    assert len(id_att2) == 26

    other_task = "01JTB4TASK0000000000000002"
    id_other = deterministic_attempt_id(other_task, 1)
    assert id_att1_run1 != id_other

    with pytest.raises(ValueError, match="attempt_index must be >= 1"):
        deterministic_attempt_id(task_id, 0)


def test_plan_repeated_matrix_defaults_and_pins(tmp_path: Path) -> None:
    plan_data = _make_tb4_plan(66)
    plan_file = tmp_path / "tb4-plan.json"
    plan_file.write_text(json.dumps(plan_data), encoding="utf-8")

    matrix = plan_repeated_matrix(plan_file, attempts_per_task=2)

    assert matrix.matrix_version == "repeated-attempt-matrix/v1"
    assert matrix.task_count == 66
    assert matrix.attempts_per_task == 2
    assert matrix.total_attempts == 132
    assert len(matrix.attempts) == 132

    # Check cost aggregation: 132 attempts * $2.50 default ceiling = $330.00
    assert matrix.total_predicted_cost_usd == 330.0
    assert matrix.consumed_cost_usd == 0.0
    assert matrix.remaining_predicted_cost_usd == 330.0

    # Check pins and backend on individual attempts
    gpu_attempts = [a for a in matrix.attempts if a.backend == "modal"]
    cpu_attempts = [a for a in matrix.attempts if a.backend == "docker"]
    assert len(gpu_attempts) == 6  # 3 GPU tasks x 2 attempts
    assert len(cpu_attempts) == 126  # 63 CPU tasks x 2 attempts

    first_attempt = matrix.attempts[0]
    assert first_attempt.attempt_index == 1
    assert first_attempt.model == "zai/glm-5.3-flash"
    assert first_attempt.agent == "mini-swe-agent"
    assert first_attempt.predicted_ceiling_usd == ZAI_OPENAPI_DEFAULT_MAX_COST_USD
    assert first_attempt.timeout_seconds == 28800
    assert first_attempt.pins["tag"] == "v4.0.0"
    assert first_attempt.pins["commit"] == "452bf30"

    # Per-attempt limits check (Z.ai OpenAPI defaults)
    limits = first_attempt.limits
    assert limits.max_requests == ZAI_OPENAPI_DEFAULT_MAX_REQUESTS
    assert limits.max_cost_usd == ZAI_OPENAPI_DEFAULT_MAX_COST_USD
    assert limits.max_input_tokens == ZAI_OPENAPI_DEFAULT_MAX_INPUT_TOKENS
    assert limits.max_output_tokens == ZAI_OPENAPI_DEFAULT_MAX_OUTPUT_TOKENS
    assert limits.max_total_tokens == ZAI_OPENAPI_DEFAULT_MAX_TOTAL_TOKENS
    assert limits.max_wall_clock_seconds == 28800

    # Idempotent re-preparation
    matrix_reprep = plan_repeated_matrix(plan_file, attempts_per_task=2)
    assert [a.attempt_id for a in matrix.attempts] == [a.attempt_id for a in matrix_reprep.attempts]


def test_authorization_default_off_and_explicit_gate(tmp_path: Path) -> None:
    plan_data = _make_tb4_plan(66)
    matrix = plan_repeated_matrix(plan_data, attempts_per_task=2)

    # Gate: preparing 66x2 authorizes zero executions
    assert matrix.authorized is False
    assert matrix.authorized_executions == 0
    assert matrix.approval_state == "prepared_not_authorized"
    assert "preparing 66x2 authorizes zero executions" in matrix.gate_statement

    # Authorize requires explicit operator
    with pytest.raises(ValueError, match="Explicit operator name required"):
        plan_repeated_matrix(plan_data, attempts_per_task=2, authorize=True)

    with pytest.raises(ValueError, match="Explicit operator name required"):
        matrix.authorize_execution(operator="")

    # Explicit authorization succeeds
    authorized_matrix = plan_repeated_matrix(
        plan_data, attempts_per_task=2, authorize=True, operator="peter"
    )
    assert authorized_matrix.authorized is True
    assert authorized_matrix.authorized_executions == 132
    assert authorized_matrix.approval_state == "authorized"
    assert authorized_matrix.authorized_by == "peter"
    assert "explicitly authorized by peter: 132 execution(s) admitted" in authorized_matrix.gate_statement
    assert all(a.approval_state == "authorized" for a in authorized_matrix.attempts)

    # authorize_execution method on existing matrix
    method_authorized = matrix.authorize_execution(operator="peter")
    assert method_authorized.authorized is True
    assert method_authorized.authorized_executions == 132
    assert method_authorized.approval_state == "authorized"


def test_resume_merge_completed_failed_timeout_unknown(tmp_path: Path) -> None:
    plan_data = _make_tb4_plan(4)  # 4 tasks x 2 attempts = 8 attempts
    matrix = plan_repeated_matrix(plan_data, attempts_per_task=2)
    assert len(matrix.attempts) == 8

    att0 = matrix.attempts[0].attempt_id
    att1 = matrix.attempts[1].attempt_id
    att2 = matrix.attempts[2].attempt_id
    att3 = matrix.attempts[3].attempt_id

    outcomes = [
        {"attempt_id": att0, "status": "completed", "usage": {"input_tokens": 1200, "cost_usd": 0.45}},
        {"attempt_id": att1, "status": "failed", "error": "ExecutionError: syntax error"},
        {"attempt_id": att2, "status": "timeout", "usage": {"wall_clock_seconds": 28800}},
        {"attempt_id": att3, "status": "unknown", "usage": {}},
    ]

    # Merge via resume_repeated_matrix
    resumed = resume_repeated_matrix(matrix, outcomes=outcomes)

    # 1. Without duplicating: total attempts remains 8
    assert len(resumed.attempts) == 8
    assert resumed.total_attempts == 8

    # 2. Without hiding: all 4 settled attempts remain visible in attempts
    att_by_id = {a.attempt_id: a for a in resumed.attempts}
    assert att_by_id[att0].outcome is not None
    assert att_by_id[att0].outcome.status == "completed"
    assert att_by_id[att0].status == "completed"

    assert att_by_id[att1].outcome is not None
    assert att_by_id[att1].outcome.status == "failed"
    assert att_by_id[att1].status == "failed"

    assert att_by_id[att2].outcome is not None
    assert att_by_id[att2].outcome.status == "timeout"
    assert att_by_id[att2].status == "timeout"

    assert att_by_id[att3].outcome is not None
    assert att_by_id[att3].outcome.status == "unknown"
    assert att_by_id[att3].status == "unknown"

    # Pending attempts remain pending
    pending_attempts = [a for a in resumed.attempts if a.outcome is None]
    assert len(pending_attempts) == 4

    assert resumed.outcomes_summary == {
        "pending": 4,
        "completed": 1,
        "failed": 1,
        "timeout": 1,
        "unknown": 1,
    }


def test_charge_honesty_unknown_and_failed_not_refunded() -> None:
    plan_data = _make_tb4_plan(4)  # 4 tasks x 2 attempts = 8 attempts
    matrix = plan_repeated_matrix(plan_data, attempts_per_task=2)

    att0 = matrix.attempts[0].attempt_id
    att1 = matrix.attempts[1].attempt_id
    att2 = matrix.attempts[2].attempt_id
    att3 = matrix.attempts[3].attempt_id

    outcomes = [
        {"attempt_id": att0, "status": "completed", "usage": {"cost_usd": 0.50}},
        {"attempt_id": att1, "status": "failed", "error": "command failed"},
        {"attempt_id": att2, "status": "timeout", "usage": {}},
        {"attempt_id": att3, "status": "unknown", "usage": {}},
    ]

    resumed = matrix.merge_outcomes(outcomes)

    # 8 attempts * $2.50 = $20.00 total predicted cost
    assert resumed.total_predicted_cost_usd == 20.0

    # Honest accounting: 4 attempts ran (1 completed, 1 failed, 1 timeout, 1 unknown).
    # All 4 are charged their predicted ceiling ($2.50 each) -> consumed is $10.00.
    # UNKNOWN IS NOT REFUNDED! FAILED IS NOT REFUNDED!
    assert resumed.consumed_cost_usd == 10.0
    assert resumed.remaining_predicted_cost_usd == 10.0
    assert resumed.consumed_cost_usd + resumed.remaining_predicted_cost_usd == resumed.total_predicted_cost_usd


def test_cost_aggregation_correctness(tmp_path: Path) -> None:
    plan_data = _make_tb4_plan(3)  # 3 tasks x 3 attempts = 9 attempts
    custom_limits = TrialLimits(
        max_requests=10,
        max_cost_usd=3.0,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        max_wall_clock_seconds=600,
    )
    matrix = plan_repeated_matrix(plan_data, attempts_per_task=3, limits=custom_limits)

    # 9 attempts * $3.00 = $27.00
    assert matrix.total_predicted_cost_usd == 27.0
    assert matrix.consumed_cost_usd == 0.0
    assert matrix.remaining_predicted_cost_usd == 27.0

    # Drop in 3 outcomes
    outcomes_file = tmp_path / "outcomes.json"
    outcomes_data = [
        {"attempt_id": matrix.attempts[0].attempt_id, "status": "completed", "usage": {}},
        {"attempt_id": matrix.attempts[1].attempt_id, "status": "failed", "error": "fail"},
        {"attempt_id": matrix.attempts[2].attempt_id, "status": "unknown", "usage": {}},
    ]
    outcomes_file.write_text(json.dumps(outcomes_data), encoding="utf-8")

    resumed = resume_repeated_matrix(matrix, outcomes=outcomes_file)
    assert resumed.consumed_cost_usd == 9.0  # 3 * 3.0
    assert resumed.remaining_predicted_cost_usd == 18.0  # 6 * 3.0
    assert resumed.consumed_cost_usd + resumed.remaining_predicted_cost_usd == 27.0


def test_resume_rejects_unknown_and_conflicting_outcomes() -> None:
    plan_data = _make_tb4_plan(2)
    matrix = plan_repeated_matrix(plan_data, attempts_per_task=2)

    # Unknown attempt_id
    with pytest.raises(ValueError, match="Unknown attempt_id 'alien-attempt'"):
        matrix.merge_outcomes([{"attempt_id": "alien-attempt", "status": "completed", "usage": {}}])

    # Conflicting outcomes in the same batch
    att0 = matrix.attempts[0].attempt_id
    with pytest.raises(ValueError, match="Conflicting retained outcome"):
        matrix.merge_outcomes(
            [
                {"attempt_id": att0, "status": "completed", "usage": {}},
                {"attempt_id": att0, "status": "failed", "usage": {}},
            ]
        )


def test_campaigns_module_re_exports() -> None:
    plan_data = _make_tb4_plan(2)
    matrix = campaign_plan_repeated_matrix(plan_data, attempts_per_task=2)
    assert isinstance(matrix, RepeatedAttemptMatrix)
    assert matrix.total_attempts == 4

    att0 = matrix.attempts[0].attempt_id
    resumed = campaign_resume_repeated_matrix(
        matrix, outcomes=[{"attempt_id": att0, "status": "completed", "usage": {}}]
    )
    assert resumed.consumed_cost_usd == ZAI_OPENAPI_DEFAULT_MAX_COST_USD


def test_repetition_cli_prepare_and_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_data = _make_tb4_plan(2)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan_data), encoding="utf-8")

    matrix_file = tmp_path / "matrix.json"

    # 1. CLI prepare
    code = repetition_main(["prepare", str(plan_file), "--attempts", "2", "--out", str(matrix_file)])
    assert code == 0
    assert matrix_file.is_file()

    out = capsys.readouterr().out
    assert "preparing 2x2 authorizes zero executions" in out
    assert "prepared_not_authorized" in out

    # 2. CLI resume with outcomes
    outcomes_file = tmp_path / "outcomes.json"
    matrix_loaded = RepeatedAttemptMatrix.model_validate_json(matrix_file.read_text(encoding="utf-8"))
    att0 = matrix_loaded.attempts[0].attempt_id
    outcomes_file.write_text(
        json.dumps([{"attempt_id": att0, "status": "completed", "usage": {}}]),
        encoding="utf-8",
    )

    resumed_file = tmp_path / "matrix_resumed.json"
    code = repetition_main(
        [
            "resume",
            str(matrix_file),
            "--outcomes",
            str(outcomes_file),
            "--out",
            str(resumed_file),
            "--authorize",
            "--operator",
            "peter",
        ]
    )
    assert code == 0
    assert resumed_file.is_file()

    resumed_data = json.loads(resumed_file.read_text(encoding="utf-8"))
    assert resumed_data["authorized"] is True
    assert resumed_data["authorized_by"] == "peter"
    assert resumed_data["approval_state"] == "authorized"
    assert resumed_data["outcomes_summary"]["completed"] == 1
    assert resumed_data["outcomes_summary"]["pending"] == 3
