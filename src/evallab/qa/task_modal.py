"""Native Harbor execution for QA Task; imported only after cloud admission.

The original package remains the native trial's task. Script/patch staging lives
outside it, so Harbor lock files and QA findings name the same immutable bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.models.trial.config import TrialConfig  # ty: ignore[unresolved-import]
from harbor.trial.trial import Trial  # ty: ignore[unresolved-import]
from harbor.utils.env import resolve_env_vars  # ty: ignore[unresolved-import]

from evallab.dataset_audit_contracts import AuditObservation, AuditTask
from evallab.harbor_modal import (
    REGRADE_SANDBOX_TIMEOUT_SECS,
    LockedModalEnvironment,
    RegradeModalEnvironment,
)
from evallab.qa.task import (
    MODAL_RESERVATIONS_PER_ENVIRONMENT,
    HarborBackend,
    _cap_excerpt,
    _TrialAbort,
    finite_reward,
    utcnow,
    write_json,
)
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate, lab_harbor_problem, modal_resource_blocker


@dataclass
class _Admission:
    gate: CloudSpendGate
    prepaid_starts: int = 0

    def reserve_start(self) -> None:
        # Native Modal prepares/builds an image, then may try Sandbox.create twice.
        # Keep all reservations even when creation succeeds on the first attempt.
        for _ in range(MODAL_RESERVATIONS_PER_ENVIRONMENT):
            self.gate.charge()

    def start(self) -> None:
        if self.prepaid_starts:
            self.prepaid_starts -= 1
        else:
            self.reserve_start()


_ACTIVE_ADMISSION: ContextVar[_Admission | None] = ContextVar("qa_modal_admission", default=None)


class QAModalEnvironment(LockedModalEnvironment, RegradeModalEnvironment):
    """Harbor-owned lifecycle with egress evidence, PATH pin and one-hour ceiling.

    Harbor uses the same environment class for the separate verifier. Cooperative
    inheritance applies the existing lab adapters to both, without a second
    verifier/collection implementation or changes to Harbor's runtime config.
    """

    def __init__(self, *, session_id: str, trial_paths: Any, **kwargs: Any) -> None:
        # Harbor's session names use the trial basename, while QA deliberately
        # keeps evidence leaves named "trial"/"trial-0". Namespace the provider
        # identity by the FULL immutable evidence path and native role together:
        # neither two run parents nor agent/separate verifier may alias.
        identity = f"{Path(trial_paths.trial_dir).resolve()}\0{session_id}"
        provider_session = "qa-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
        super().__init__(session_id=provider_session, trial_paths=trial_paths, **kwargs)

    async def start(self, force_build: bool = False) -> None:
        admission = _ACTIVE_ADMISSION.get()
        if admission is None:
            raise ValueError("Modal QA execution requires an active cloud spend gate")
        admission.start()  # before native image preparation or any provider call
        try:
            await super().start(force_build=force_build)
        except BaseException:
            # A failed start can already own a sandbox; native trial cleanup also
            # runs, but never rely on a successful start to reclaim the allocation.
            try:
                await asyncio.shield(asyncio.wait_for(self.stop(delete=True), timeout=90))
            except Exception as cleanup_error:
                self.logger.warning("Modal QA failed-start cleanup: %s", cleanup_error)
            raise


class QAScriptAgent(BaseAgent):
    """A deterministic script channel, not a model agent or an inference client."""

    def __init__(
        self,
        *,
        qa_script_path: str | None = None,
        qa_solution_dir: str | None = None,
        qa_command: str | None = None,
        qa_workdir: str | None = None,
        qa_solution_env: dict[str, str] | None = None,
        qa_timeout_seconds: int = 1800,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.script_path = Path(qa_script_path) if qa_script_path else None
        self.solution_dir = Path(qa_solution_dir) if qa_solution_dir else None
        self.command = qa_command
        self.workdir = qa_workdir
        self.solution_env = qa_solution_env or {}
        self.timeout_seconds = qa_timeout_seconds

    @staticmethod
    def name() -> str:
        return "qa-script"

    def version(self) -> str:
        return "1"

    async def setup(self, environment: Any) -> None:
        return

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        if self.solution_dir is not None:
            await environment.upload_dir(source_dir=self.solution_dir, target_dir="/solution")
        if self.script_path is not None:
            target = (
                "/solution/solve.sh"
                if self.command == "bash /solution/solve.sh"
                else "/tmp/qa-shortcut.sh"
            )
            await environment.upload_file(source_path=self.script_path, target_path=target)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / "oracle.txt"
        log.write_text("", encoding="utf-8")
        if self.command is None:
            return
        command = "set -o pipefail; " + self.command + " 2>&1 | tee /logs/agent/oracle.txt"
        with environment.scoped_exec_env(resolve_env_vars(self.solution_env)):
            result = await environment.exec(
                command=command,
                cwd=self.workdir,
                timeout_sec=self.timeout_seconds,
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )
        log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
        if result.return_code != 0:
            (self.logs_dir / "exit-code.txt").write_text(str(result.return_code), encoding="utf-8")
            raise _TrialAbort(f"QA script exited {result.return_code}")


class ModalBackend(HarborBackend):
    """Reuse logical QA stages/replay; all execution belongs to native Harbor."""

    environment = "modal"

    def __init__(self, *, repo_root: Path, spend_gate: CloudSpendGate) -> None:
        super().__init__(repo_root=repo_root)
        if not isinstance(spend_gate, CloudSpendGate):
            raise ValueError("Modal QA requires a CloudSpendGate")
        self.spend_gate = spend_gate

    @contextmanager
    def slot(self, label: str) -> Iterator[None]:
        # Deliberately not HarborBackend.slot: no local Docker admission on Modal.
        yield

    def resource_blocker(self, package: Path) -> str | None:
        return modal_resource_blocker(package)

    def _ensure_images(self, package: Path, digest: str) -> tuple[str, str | None]:
        raise _TrialAbort("Modal QA cannot build, pull or run local Docker images")

    def run_container_trial(
        self,
        package: Path,
        *,
        digest: str,
        mode: Literal["oracle", "nop", "shortcut"],
        script: str | None = None,
        trial_dir: Path,
        timeout_seconds: int = 1800,
        solve_override: str | None = None,
        with_solution: bool = False,
        capture_logs: bool = False,
    ) -> AuditObservation:
        return self._native_trial(
            package,
            digest=digest,
            mode=mode,
            script=script,
            trial_dir=trial_dir,
            timeout_seconds=timeout_seconds,
            solve_override=solve_override,
            with_solution=with_solution,
            capture_logs=capture_logs,
        )

    async def _execute(self, config: TrialConfig) -> dict[str, Any]:
        assert self.spend_gate is not None
        admission = _Admission(self.spend_gate)
        # Refuse exhausted budgets even before native trial construction. The
        # environment consumes this prepaid start; a separate verifier reserves
        # independently immediately before its native start.
        admission.reserve_start()
        admission.prepaid_starts = 1
        token = _ACTIVE_ADMISSION.set(admission)
        try:
            trial = await Trial.create(config)
            result = await trial.run()
            return result.model_dump(mode="json")
        finally:
            _ACTIVE_ADMISSION.reset(token)

    def _native_trial(
        self,
        package: Path,
        *,
        digest: str,
        mode: Literal["oracle", "nop", "shortcut"],
        script: str | None,
        trial_dir: Path,
        timeout_seconds: int,
        solve_override: str | None,
        with_solution: bool,
        capture_logs: bool,
        verifier: bool = True,
    ) -> AuditObservation:
        package = package.resolve()
        trial_dir = trial_dir.resolve()
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        (trial_dir / "verifier").mkdir(exist_ok=True)
        facts: dict[str, Any] = {
            "reward": None,
            "job_path": str(trial_dir),
            "environment": "modal",
            "package_digest": digest,
        }
        error: str | None = None
        payload: dict[str, Any] = {}
        try:
            blocker = self.resource_blocker(package)
            if blocker:
                raise _TrialAbort(blocker)
            if task_directory_digest(package) != digest:
                raise _TrialAbort("task package digest changed before Modal execution")
            problem = lab_harbor_problem()
            if problem:
                raise _TrialAbort(problem)
            raw = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="qa-modal-stage-") as temporary:
                solution_dir = (
                    self._stage_solution(package, Path(temporary), facts)
                    if mode == "oracle" or with_solution
                    else None
                )
                command = None
                script_path = None
                if mode == "oracle":
                    command = "bash /solution/solve.sh"
                    if solve_override is not None:
                        script_path = trial_dir / "agent" / "solve-override.sh"
                        script_path.write_text(solve_override, encoding="utf-8")
                elif mode == "shortcut":
                    if script is None:
                        raise _TrialAbort("shortcut execution requires a script")
                    command = "bash /tmp/qa-shortcut.sh"
                    script_path = trial_dir / "agent" / "shortcut.sh"
                    script_path.write_text(script, encoding="utf-8")
                config = TrialConfig.model_validate(
                    {
                        "task": {"path": package},
                        "trial_name": trial_dir.name,
                        "trials_dir": trial_dir.parent,
                        "agent": {
                            "import_path": "evallab.qa.task_modal:QAScriptAgent",
                            "override_timeout_sec": timeout_seconds,
                            "override_setup_timeout_sec": min(timeout_seconds, 360),
                            "kwargs": {
                                "qa_script_path": str(script_path) if script_path else None,
                                "qa_solution_dir": str(solution_dir) if solution_dir else None,
                                "qa_command": command,
                                "qa_workdir": self.patch_workdir(package),
                                "qa_timeout_seconds": timeout_seconds,
                                "qa_solution_env": (raw.get("solution") or {}).get("env", {})
                                if mode == "oracle" or with_solution
                                else {},
                            },
                        },
                        "environment": {
                            "import_path": "evallab.qa.task_modal:QAModalEnvironment",
                            "delete": True,
                            "kwargs": {
                                "sandbox_timeout_secs": min(
                                    timeout_seconds + 600, REGRADE_SANDBOX_TIMEOUT_SECS
                                ),
                                "evallab_egress_lock": True,
                            },
                        },
                        "verifier": {
                            "disable": not verifier,
                            "max_timeout_sec": timeout_seconds,
                        },
                    }
                )
                # Native Harbor always collects /logs/artifacts (including mutation
                # captures), declared artifacts/exclusions, collect hooks and verifier
                # logs. capture_logs requests that existing native evidence, not a
                # second collection protocol.
                facts["capture_logs"] = capture_logs
                payload = asyncio.run(self._execute(config))
            exception = payload.get("exception_info")
            if exception:
                error = str(exception.get("exception_message") or exception)
            rewards = (payload.get("verifier_result") or {}).get("rewards") or {}
            reward = finite_reward(rewards.get("reward"))
            if verifier and reward is None and error is None:
                error = "native Harbor trial produced no usable reward"
            if task_directory_digest(package) != digest:
                error = "task package changed during Modal execution; result is unmeasured"
            if error is None:
                facts["reward"] = reward
            elif reward is not None:
                facts["unusable_reward"] = reward
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        for directory, filename, key in (
            ("agent", "oracle.txt", "agent_stdout"),
            ("verifier", "test-stdout.txt", "verifier_stdout"),
        ):
            path = trial_dir / directory / filename
            if not path.exists():
                path.write_text("", encoding="utf-8")
            facts[key] = _cap_excerpt(path.read_text(encoding="utf-8", errors="replace"))
        facts["exception"] = error
        assert self.spend_gate is not None
        facts["cloud_spend"] = self.spend_gate.to_json()
        write_json(trial_dir / "qa-execution.json", {"mode": mode, "at": utcnow(), **facts})
        return AuditObservation(
            stage="oracle" if mode == "oracle" else "nop",
            status="executed" if error is None else "failed",
            facts=facts,
            reason=error,
        )

    def leak(self, task: AuditTask, *, run_dir: Path, timeout_seconds: int) -> AuditObservation:
        from evallab.dataset_audit_checks import build_leak_probe_script

        if task.path is None or task.package_digest is None:
            raise ValueError(f"{task.task_id}: task package path/digest is unset")
        # Run the missing-git repository check in the SAME fresh sandbox. It costs
        # no second allocation and cannot inspect a different state from the probe.
        script = (
            "for d in /testbed /workspace/repo /workspace /app /root/repo "
            '"$HOME/repo" "$PWD"; do [ -e "$d/.git" ] && echo "qa-repo:$d"; done; true\n'
        ) + build_leak_probe_script()
        observation = self._native_trial(
            Path(task.path),
            digest=task.package_digest,
            mode="shortcut",
            script=script,
            trial_dir=run_dir / "trial",
            timeout_seconds=min(timeout_seconds, 300),
            solve_override=None,
            with_solution=False,
            capture_logs=False,
            verifier=False,
        )
        if observation.status != "executed":
            return AuditObservation(
                stage="leak", status="failed", facts=observation.facts, reason=observation.reason
            )
        text = (run_dir / "trial" / "agent" / "oracle.txt").read_text(encoding="utf-8")
        (run_dir / "oracle.txt").write_text(text, encoding="utf-8")
        result = self.leak_observation(text, run_dir=run_dir, has_repository="qa-repo:" in text)
        return result.model_copy(update={"facts": {**observation.facts, **result.facts}})
