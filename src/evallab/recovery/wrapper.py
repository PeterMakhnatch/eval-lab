from __future__ import annotations

from typing import Literal, assert_never

from evallab.recovery.bundle import RecoveryStateBundle


def build_recovery_initial_prompt(
    base_instruction: str,
    bundle: RecoveryStateBundle,
    message_mode: Literal["full", "summary", "none"],
) -> str:
    """Format starting context for the recovery agent based on experimental message mode."""
    if message_mode == "none":
        return base_instruction

    if message_mode == "summary":
        failed_cmds = [command for command in bundle.command_ledger if command.exit_code != 0]
        summary_text = (
            "Note: You are resuming an in-progress attempt on this task at step "
            f"{bundle.step_cutoff}.\n"
            f"The previous attempt executed {len(bundle.command_ledger)} commands, of which "
            f"{len(failed_cmds)} produced non-zero exit codes.\n"
            "The environment files and state up to this step have been restored.\n"
        )
        if failed_cmds:
            summary_text += (
                "Last failing commands:\n"
                + "\n".join(
                    f"- `{command.command}` (exit {command.exit_code})"
                    for command in failed_cmds[-3:]
                )
                + "\n"
            )
        return (
            summary_text + "\nYour objective is to inspect the current state and complete "
            "the task successfully.\n\n" + base_instruction
        )

    if message_mode == "full":
        history = "\n".join(
            f"[{command.index}] $ {command.command} (exit {command.exit_code})"
            for command in bundle.command_ledger
        )
        return (
            "Note: You are resuming an attempt on this task. "
            "Below is the prior command history:\n"
            f"{history}\n\n"
            "Inspect the restored workspace and complete the task instructions below:\n\n"
            f"{base_instruction}"
        )

    assert_never(message_mode)
