"""ScriptAgent: a Harbor agent that runs one shell script in its seat.

Harbor loads it through `import_path: "muster.agents.script:ScriptAgent"` with the kwarg `script`,
the absolute host path of a bash script. This is the only muster module that imports harbor.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import override

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Agent, Observation, ObservationResult, Step, ToolCall, Trajectory

from muster import __version__

ATIF_VERSION = "ATIF-v1.7"  # the first version with llm_call_count, which marks a step without a model
OUTPUT_LIMIT = 20_000  # characters of script output kept in the trajectory


class ScriptAgent(BaseAgent):
    """Runs one bash script in the seat, as the seat's Compose user, in its working directory,
    with its Compose environment (SEAT, SEAT_TOKEN, WORLD) plus the participant's `env`."""

    capabilities = AgentCapabilities(atif=True)
    LOG_FILE = "script.txt"

    def __init__(self, logs_dir: Path, script: str | Path | None = None, **kwargs):
        super().__init__(logs_dir=logs_dir, **kwargs)
        if script is None or not Path(script).is_absolute() or not Path(script).is_file():
            raise ValueError(
                f"ScriptAgent needs the kwarg script, the absolute path of a file; got {script!r}"
            )
        self._script = Path(script)
        self._target = (self.environment_logs_dir / self._script.name).as_posix()

    @staticmethod
    @override
    def name() -> str:
        return "script"

    @override
    def version(self) -> str:
        return __version__

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload the script into the seat's log directory and let the seat's user read it."""
        await environment.upload_file(self._script, self._target)
        result = await environment.exec(f"chmod a+rx {shlex.quote(self._target)}", user="root")
        if result.return_code != 0:
            raise RuntimeError(f"ScriptAgent cannot prepare {self._target}: {result.stdout or result.stderr}")

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        """Run the script, keep its output in /logs/agent/script.txt and write trajectory.json.

        The script writes to the log file, not to the exec stream, so it keeps running when Harbor
        cancels the seat at the end of play. A non-zero exit raises NonZeroAgentExitCodeError.
        """
        log = shlex.quote((self.environment_logs_dir / self.LOG_FILE).as_posix())
        command = f"bash {shlex.quote(self._target)} > {log} 2>&1; code=$?; cat {log}; exit $code"
        started = _now()
        exit_code: int | None = None
        output: str | None = None
        try:
            result = await environment.exec(command=command)
            exit_code, output = result.return_code, result.stdout or ""
        finally:
            try:
                self._write_trajectory(instruction, command, started, exit_code, output)
            except Exception as error:  # a cancelled run must stay cancelled
                self.logger.error(f"ScriptAgent could not write trajectory.json: {error}")
        if exit_code != 0:
            raise NonZeroAgentExitCodeError(
                f"Script {self._script.name} exited with code {exit_code}\noutput: {output[-2000:]}"
            )

    def _write_trajectory(
        self, instruction: str, command: str, started: str, exit_code: int | None, output: str | None
    ) -> None:
        """One user step with the instruction, one agent step with the command and its output."""
        extra: dict[str, object] = {"outcome": "cancelled", "finished_at": _now()}
        content = f"The run was cancelled before the script ended. Its output is in {self.LOG_FILE}."
        if exit_code is not None and output is not None:
            extra.update(outcome="exited", exit_code=exit_code, output_chars=len(output))
            content = output[-OUTPUT_LIMIT:]
        trajectory = Trajectory(
            schema_version=ATIF_VERSION,
            session_id=self.session_id,
            agent=Agent(
                name=self.name(),
                version=self.version(),
                extra={"policy": self._script.stem, "script": str(self._script)},
            ),
            steps=[
                Step(step_id=1, timestamp=started, source="user", message=instruction),
                Step(
                    step_id=2,
                    timestamp=started,
                    source="agent",
                    message=f"Run the policy script {self._script.name}.",
                    tool_calls=[
                        ToolCall(tool_call_id="script", function_name="bash", arguments={"command": command})
                    ],
                    observation=Observation(
                        results=[ObservationResult(source_call_id="script", content=content)]
                    ),
                    llm_call_count=0,
                    extra=extra,
                ),
            ],
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(trajectory.to_json_dict(), indent=2) + "\n"
        (self.logs_dir / "trajectory.json").write_text(text, encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
