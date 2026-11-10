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
    parse_stream_json_to_atif,
    sanitize_stream_json,
)

_STREAM_FILE = "antigravity-cli.stream.jsonl"
_STDERR_FILE = "antigravity-cli.err"
_TRAJECTORY_FILE = "trajectory.json"
_HOST_TOKEN_ENV = "AGY_AUTH_JSON_PATH"
_FORCE_AUTH_ENV = "AGY_FORCE_AUTH_JSON"
_HOST_TOKEN = Path(".gemini/antigravity-cli/antigravity-oauth-token")
_UPLOADED_TOKEN = "/tmp/.agy-auth.json"
_REMOTE_TOKEN_DIR = "$HOME/.gemini/antigravity-cli"
_REMOTE_TOKEN_PATH = f"{_REMOTE_TOKEN_DIR}/antigravity-oauth-token"


def _auth_json_requested(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class AntigravityCliCapture(AntigravityCli):
    """Run ``agy`` through its documented stream-json transport.

    This class is imported by Harbor using a repo-owned import path. The installed
    Harbor adapter's install, settings and flag handling stay authoritative. The
    OAuth token staging is repo-owned: Harbor 0.21 uploaded the token from its
    ``AntigravityCli``, and 0.24 dropped that hook without a replacement.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._seeded_token = False
        super().__init__(*args, **kwargs)

    def _host_oauth_token(self) -> Path | None:
        """The host token to stage: an explicit path, else the opt-in default."""
        explicit = self._get_env(_HOST_TOKEN_ENV)
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_file():
                self.logger.warning(
                    f"{_HOST_TOKEN_ENV} names a missing file; agy runs without a "
                    "staged OAuth token",
                    extra={"path": str(path)},
                )
                return None
            return path
        if not _auth_json_requested(self._get_env(_FORCE_AUTH_ENV)):
            return None
        default = Path.home() / _HOST_TOKEN
        return default if default.is_file() else None

    async def _seed_oauth_token(self, environment: BaseEnvironment) -> None:
        """Stage the host OAuth token where ``agy`` reads it inside the trial.

        Without the token a headless ``agy`` falls back to interactive browser
        OAuth, prints a sign-in URL and dies on its own timeout, burning the run.
        The uploaded copy is removed after the token lands in the trial home.
        """
        if self._seeded_token:
            return
        token = self._host_oauth_token()
        if token is None:
            return
        await environment.upload_file(str(token), _UPLOADED_TOKEN)
        await self.exec_as_agent(
            environment,
            command=(
                f'mkdir -p "{_REMOTE_TOKEN_DIR}" && '
                f'cp {_UPLOADED_TOKEN} "{_REMOTE_TOKEN_PATH}" && '
                f'chmod 600 "{_REMOTE_TOKEN_PATH}" && '
                f"rm -f {_UPLOADED_TOKEN}"
            ),
        )
        self._seeded_token = True

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.ensure_system_dependencies(environment, ("python3",))

    @staticmethod
    def _stream_sanitizer() -> str:
        return r"""import json,re,sys
k=re.compile(r'(token|secret|password|credential|authorization|api[_-]?key)',re.I)
n=re.compile(r'tokens$',re.I)
b=re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+')
def clean(v,key=None):
 if key is not None and k.search(key) and not (type(v) is int and n.search(key)): return '<redacted>'
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

    async def _register_model_only_when_unknown(
        self, environment: BaseEnvironment, model: str, env: dict[str, str]
    ) -> bool:
        """Register custom slugs only: built-ins take agy's native request path.

        ``agy`` 400s on several built-in slugs routed through the custom-model
        path, so the registration has to follow the CLI's own model listing.
        """
        try:
            listing = await self.exec_as_agent(
                environment,
                # Remote sandbox sessions keep stdin open; agy waits for EOF.
                command="$HOME/.local/bin/agy models </dev/null",
                env=env,
                timeout_sec=self._MODEL_LIST_TIMEOUT_SEC,
            )
        except Exception:
            self.logger.debug("agy models listing failed; registering model")
            return True
        known = self._parse_known_models(listing.stdout or "")
        return not known or not self._model_is_known(model, known)

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
        await self._seed_oauth_token(environment)
        env = {"GEMINI_CLI_TRUST_WORKSPACE": "true"}
        env.update(self._forwarded_cli_env())
        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command)
        register_model = await self._register_model_only_when_unknown(
            environment, model, env
        )
        await self.exec_as_agent(
            environment,
            command=self._build_settings_command(model, register_model=register_model),
        )
        model_flag = f"--model {shlex.quote(model)} "
        extra_flags = (self.build_cli_flags() + " ") if self.build_cli_flags() else ""
        sanitizer = shlex.quote(self._stream_sanitizer())
        # `--add-dir "$PWD"` anchors bare-filename file tools to the task workdir.
        # Stderr goes to its own file and is replayed on failure with a byte cap,
        # so a stderr line cannot corrupt a JSON record and error classification
        # still sees the API or network failure that ended the run.
        command = (
            f"$HOME/.local/bin/agy --dangerously-skip-permissions "
            f"--print-timeout {self._PRINT_TIMEOUT} --add-dir \"$PWD\" "
            f"{model_flag}{extra_flags}--output-format stream-json "
            f"--prompt={escaped_instruction} </dev/null "
            f"2>/logs/agent/{_STDERR_FILE} "
            f"| python3 -c {sanitizer} > /logs/agent/{_STREAM_FILE}; "
            f"rc=$?; if [ $rc -ne 0 ]; then "
            f"tail -c {self._STDERR_REPLAY_BYTES} /logs/agent/{_STDERR_FILE} >&2; "
            f"fi; exit $rc"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            if self._seeded_token:
                with contextlib.suppress(Exception):
                    await self.exec_as_agent(
                        environment,
                        command=f'rm -f "{_REMOTE_TOKEN_PATH}"',
                    )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        path_job_id, path_trial_id = _path_identity(self.logs_dir)
        job_id = _metadata_id(metadata, "job_id") or path_job_id
        trial_id = _metadata_id(metadata, "trial_id", "context_id") or path_trial_id
        source_path = self.logs_dir / _STREAM_FILE
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
