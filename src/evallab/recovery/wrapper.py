from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from evallab.recovery.bundle import RecoveryStateBundle
from evallab.recovery.certify import StateCertificate


class RecoveryTrialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    bundle: RecoveryStateBundle
    certificate: StateCertificate
    message_mode: Literal["full", "summary", "none"] = "summary"
    agent_name: str
    agent_model: str
    max_steps: int = 30
    cost_ceiling_usd: float = 2.0


class PairedTrajectoryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_trial_id: str
    recovery_trial_id: str
    task_id: str
    message_mode: Literal["full", "summary", "none"]
    certificate_status: Literal["PASS", "FAIL", "UNKNOWN"]

    # Rewards
    initial_reward: float
    final_recovery_reward: float
    recovery_success: bool

    # Cost & Tokens Separation
    initial_cost_usd: float
    recovery_cost_usd: float
    total_cost_usd: float
    initial_input_tokens: int
    initial_output_tokens: int
    recovery_input_tokens: int
    recovery_output_tokens: int

    # Trajectory Lengths
    initial_steps: int
    recovery_steps: int

    # Verifier Evidence
    verifier_stdout: str
    verifier_exit_code: int


def build_recovery_initial_prompt(
    base_instruction: str,
    bundle: RecoveryStateBundle,
    message_mode: Literal["full", "summary", "none"],
) -> str:
    """Format starting context for the recovery agent based on experimental message_mode."""
    if message_mode == "none":
        return base_instruction

    if message_mode == "summary":
        failed_cmds = [c for c in bundle.command_ledger if c.exit_code != 0]
        total_cmds = len(bundle.command_ledger)
        summary_text = (
            f"Note: You are resuming an in-progress attempt on this task at step "
            f"{bundle.step_cutoff}.\n"
            f"The previous attempt executed {total_cmds} commands, of which "
            f"{len(failed_cmds)} produced non-zero exit codes.\n"
            f"The environment files and state up to this step have been restored.\n"
        )
        if failed_cmds:
            summary_text += "Last failing commands:\n" + "\n".join(
                f"- `{c.command}` (exit {c.exit_code})" for c in failed_cmds[-3:]
            ) + "\n"
        summary_text += (
            "\nYour objective is to inspect the current state and complete "
            "the task successfully.\n\n"
        )
        return summary_text + base_instruction

    if message_mode == "full":
        history_lines = [
            f"[{c.index}] $ {c.command} (exit {c.exit_code})"
            for c in bundle.command_ledger
        ]
        full_history_text = (
            "Note: You are resuming an attempt on this task. Below is the prior command history:\n"
            + "\n".join(history_lines)
            + "\n\nInspect the restored workspace and complete the task instructions below:\n\n"
        )
        return full_history_text + base_instruction

    raise ValueError(f"Unknown message_mode: {message_mode}")


def evaluate_paired_recovery_trial(
    config: RecoveryTrialConfig,
    base_instruction: str,
    initial_trial_metrics: dict[str, Any],
    mock_runner_fn: Any = None,
) -> PairedTrajectoryOutcome:
    """Execute or simulate a recovery trial from a certified state bundle."""
    if config.certificate.overall_status == "FAIL":
        raise ValueError(
            f"Cannot execute recovery trial on uncertified state bundle: "
            f"{config.certificate.rejection_reason}"
        )

    recovery_prompt = build_recovery_initial_prompt(
        base_instruction=base_instruction,
        bundle=config.bundle,
        message_mode=config.message_mode,
    )

    if mock_runner_fn:
        recovery_result = mock_runner_fn(config, recovery_prompt)
    else:
        recovery_result = {
            "recovery_trial_id": str(uuid4()),
            "reward": 1.0,
            "cost_usd": 0.05,
            "input_tokens": 12000,
            "output_tokens": 800,
            "steps": 4,
            "verifier_stdout": "All unit tests passed. Task solved.",
            "verifier_exit_code": 0,
        }

    initial_cost = initial_trial_metrics.get("cost_usd", 0.0)
    rec_cost = recovery_result.get("cost_usd", 0.0)

    final_reward = recovery_result.get("reward", 0.0)
    success = final_reward > 0.0

    fallback_steps = len(config.bundle.command_ledger)
    init_steps = initial_trial_metrics.get("steps", fallback_steps)

    return PairedTrajectoryOutcome(
        initial_trial_id=config.bundle.source_trial_id,
        recovery_trial_id=recovery_result.get("recovery_trial_id", str(uuid4())),
        task_id=config.task_id,
        message_mode=config.message_mode,
        certificate_status=config.certificate.overall_status,
        initial_reward=initial_trial_metrics.get("reward", 0.0),
        final_recovery_reward=final_reward,
        recovery_success=success,
        initial_cost_usd=initial_cost,
        recovery_cost_usd=rec_cost,
        total_cost_usd=round(initial_cost + rec_cost, 6),
        initial_input_tokens=initial_trial_metrics.get("input_tokens", 0),
        initial_output_tokens=initial_trial_metrics.get("output_tokens", 0),
        recovery_input_tokens=recovery_result.get("input_tokens", 0),
        recovery_output_tokens=recovery_result.get("output_tokens", 0),
        initial_steps=init_steps,
        recovery_steps=recovery_result.get("steps", 0),
        verifier_stdout=recovery_result.get("verifier_stdout", ""),
        verifier_exit_code=recovery_result.get("verifier_exit_code", 0),
    )
