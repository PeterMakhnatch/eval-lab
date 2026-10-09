"""Model-free scripted harness for Prime ``verifiers`` rollouts ($0, no inference).

The harness stages a fixed file set into the rollout runtime and runs one
shell command there (oracle/cheat), or nothing at all (nop). It never contacts
the interception endpoint: the ``model`` on the agent config is an inert label
and zero model calls are made, so this is not local LLM inference — it is the
same scripted-agent shape as verifiers' own ``agent-user-v1`` test fixture
(``ScriptedAgentUserHarness``), which runs ``runtime.run_program`` with no
model traffic.

The module doubles as the harness plugin. verifiers resolves a harness id by
importing it as a single-level module name (a dotted id makes its
``find_spec`` probe raise), so the runner registers this module under the
alias ``evallab_vf_scripted_harness`` in ``sys.modules`` before constructing
the env (see ``interop_verifiers._ensure_harness_alias``) and uses that alias
as the harness id. ``__all__`` exports the config alongside the one
``Harness`` subclass the loader requires. This module is only ever imported
with the ``xplat-verifiers`` dependency group installed.
"""

from __future__ import annotations

import base64
import shlex

from pydantic import Field
from verifiers.v1.clients import ModelContext  # ty: ignore[unresolved-import]
from verifiers.v1.configs.harness import HarnessConfig  # ty: ignore[unresolved-import]
from verifiers.v1.harness import Harness  # ty: ignore[unresolved-import]
from verifiers.v1.runtimes import ProgramResult, Runtime  # ty: ignore[unresolved-import]
from verifiers.v1.task import TaskData  # ty: ignore[unresolved-import]
from verifiers.v1.trace import Trace  # ty: ignore[unresolved-import]

__all__ = ["ScriptedHarness", "ScriptedHarnessConfig"]

#: Single-level plugin id: verifiers imports the harness id as a module, so a
#: dotted path cannot be used (see module docstring).
HARNESS_PLUGIN_ID = "evallab_vf_scripted_harness"


class ScriptedHarnessConfig(HarnessConfig):
    """Plan carrier: absolute container paths (base64 bytes) + one command."""

    id: str = HARNESS_PLUGIN_ID
    command: str | None = None
    """Shell command run once in the task workdir; None = nop (stage, run nothing)."""
    workdir: str = "/"
    """Container cwd for the command (shared task_workdir rule, Harbor default /)."""
    files_b64: dict[str, str] = Field(default_factory=dict)
    """Absolute container path -> base64-encoded file bytes to stage first."""


class ScriptedHarness(Harness[ScriptedHarnessConfig]):
    """Stage ``files_b64``, run ``command``; never call a model."""

    SUPPORTS_MCP = True
    EXECUTES_CODE = True
    NEEDS_CONTAINER = True

    #: Trace-record tail bound: program output stays reviewable evidence (e.g.
    #: the cheat ladder's CHEAT_LADDER_RESULT line for per-attack status).
    MAX_OUTPUT_CHARS = 6000

    async def launch(
        self,
        ctx: ModelContext,
        trace: Trace,
        runtime: Runtime,
        endpoint: str,
        secret: str,
        mcp_urls: dict[str, str],
        data: TaskData,
        tool_interception_url: str | None = None,
    ) -> ProgramResult:
        for path, payload in self.config.files_b64.items():
            await runtime.write(path, base64.b64decode(payload))
        if not self.config.command:
            return ProgramResult(0, "", "scripted nop: staged files, ran no command")
        command = f"cd {shlex.quote(self.config.workdir)} && {self.config.command}"
        result = await runtime.run_program(["sh", "-c", command], {})
        trace.info["scripted_command"] = self.config.command
        trace.info["scripted_workdir"] = self.config.workdir
        trace.info["scripted_exit"] = result.exit_code
        trace.info["scripted_stdout_tail"] = result.stdout[-self.MAX_OUTPUT_CHARS :]
        trace.info["scripted_stderr_tail"] = result.stderr[-self.MAX_OUTPUT_CHARS :]
        for line in result.stdout.splitlines():
            if line.startswith("CHEAT_LADDER_RESULT="):
                trace.info["cheat_ladder_result"] = line.split("=", 1)[1][:65536]
        return result
