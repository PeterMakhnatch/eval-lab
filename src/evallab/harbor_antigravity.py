"""Repo-owned Harbor adapter for Antigravity's structured headless stream."""

from __future__ import annotations

import contextlib
import json
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.antigravity_cli import AntigravityCli  # ty: ignore[unresolved-import]
from harbor.agents.installed.base import with_prompt_template  # ty: ignore[unresolved-import]
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.antigravity import (
    create_fallback_atif_for_print_mode,
    parse_stream_json_to_atif,
    sanitize_stream_json,
)

_STREAM_FILE = "antigravity-cli.stream.jsonl"
_TEXT_FILE = "antigravity-cli.txt"
_TRAJECTORY_FILE = "trajectory.json"


class AntigravityCliCapture(AntigravityCli):
    """Run ``agy`` through its documented stream-json transport.

    This class is imported by Harbor using a repo-owned import path. The installed
    Harbor adapter remains untouched, including its OAuth token staging and scrub.
    """

    def __init__(self, *args, capture_stream: bool = True, **kwargs: Any) -> None:
        self._capture_stream = capture_stream
        super().__init__(*args, **kwargs)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.ensure_system_dependencies(environment, ("python3",))

    @staticmethod
    def _stream_sanitizer() -> str:
        return r"""import json,re,sys
k=re.compile(r'(token|secret|password|credential|authorization|api[_-]?key)',re.I)
b=re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+')
def clean(v,key=None):
 if key is not None and k.search(key): return '<redacted>'
 if isinstance(v,dict): return {str(a):clean(c,str(a)) for a,c in v.items()}
 if isinstance(v,list): return [clean(a) for a in v]
 if isinstance(v,str): return b.sub('Bearer <redacted>',v)
 return v
for line in sys.stdin:
 try:
  obj=json.loads(line)
 except (TypeError,json.JSONDecodeError):
  continue
 if isinstance(obj,dict): print(json.dumps(clean(obj),ensure_ascii=False,sort_keys=True),flush=True)
"""

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        escaped_instruction = shlex.quote(instruction)
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        model = self.model_name.rsplit("/", 1)[-1]
        env = {"GEMINI_CLI_TRUST_WORKSPACE": "true"}
        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command, env=env)
        settings_command, _ = self._build_settings_command(model)
        if settings_command:
            await self.exec_as_agent(environment, command=settings_command, env=env)
        model_flag = f"--model {shlex.quote(model)} "
        extra_flags = (self.build_cli_flags() + " ") if self.build_cli_flags() else ""
        if self._capture_stream:
            sanitizer = shlex.quote(self._stream_sanitizer())
            command = (
                "set -o pipefail; "
                f"$HOME/.local/bin/agy --dangerously-skip-permissions {model_flag}"
                f"{extra_flags}--prompt={escaped_instruction} --output-format stream-json "
                f"2>/dev/null | python3 -c {sanitizer} > /logs/agent/{_STREAM_FILE}"
            )
        else:
            command = (
                f"$HOME/.local/bin/agy --dangerously-skip-permissions {model_flag}"
                f"{extra_flags}--prompt={escaped_instruction} "
                f"2>/dev/null | tee /logs/agent/{_TEXT_FILE}"
            )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            if self._seeded_token:
                with contextlib.suppress(Exception):
                    await self.exec_as_agent(
                        environment,
                        command=f'rm -f "{self._REMOTE_TOKEN_PATH}"',
                    )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        path_job_id, path_trial_id = _path_identity(self.logs_dir)
        job_id = _metadata_id(metadata, "job_id") or path_job_id
        trial_id = _metadata_id(metadata, "trial_id", "context_id") or path_trial_id
        source_path = self.logs_dir / (_STREAM_FILE if self._capture_stream else _TEXT_FILE)
        if self._capture_stream:
            if not source_path.is_file():
                return
            raw = source_path.read_text(errors="replace")
            sanitized = sanitize_stream_json(raw)
            source_path.write_text(sanitized)
            payload = parse_stream_json_to_atif(
                sanitized,
                agent_version=self.version() or "unknown",
                model_name=self.model_name,
                raw_source=_STREAM_FILE,
                job_id=job_id,
                trial_id=trial_id,
            )
        else:
            if not source_path.is_file():
                return
            final_response = source_path.read_text(errors="replace")
            payload = create_fallback_atif_for_print_mode(
                final_response,
                agent_version=self.version() or "unknown",
                model_name=self.model_name,
                raw_source=_TEXT_FILE,
                job_id=job_id,
                trial_id=trial_id,
            )
        if payload is None:
            return
        (self.logs_dir / _TRAJECTORY_FILE).write_text(json.dumps(payload, indent=2) + "\n")
        final_metrics = payload.get("final_metrics")
        if isinstance(final_metrics, dict):
            context.n_input_tokens = final_metrics.get("total_prompt_tokens")
            context.n_output_tokens = final_metrics.get("total_completion_tokens")
            context.n_cache_tokens = final_metrics.get("total_cached_tokens")


def _metadata_id(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value


def _path_identity(logs_dir: Path) -> tuple[str | None, str | None]:
    for directory in (logs_dir, *logs_dir.parents):
        if (directory / "lock.json").is_file():
            return directory.parent.name, directory.name
    return None, None
