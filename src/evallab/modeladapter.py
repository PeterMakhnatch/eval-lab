"""Model adapter subsystem for headless CLI model invocation.

Provides transport adapters for local subscription CLIs (cursor-agent, agy)
and injectable AnalyzerCallable wrappers for analyst and worker components.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evallab.facts import AnalyzerCallResult
from evallab.runner import subscription_environment

DISALLOWED_UNPINNED_SELECTORS: frozenset[str] = frozenset({
    "auto",
    "default",
    "none",
    "latest",
    "unpinned",
    "null",
})


class ModelAdapterError(RuntimeError):
    """Base exception for model adapter failures."""


class ModelAdapterRefusalError(ModelAdapterError):
    """Raised when an unpinned, empty, or disallowed model selector is used."""


class ModelAdapterExecutionError(ModelAdapterError):
    """Raised when a transport process exits with a non-zero status code or OS error."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        argv: list[str],
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.argv = argv
        self.stdout = stdout
        self.stderr = stderr


class ModelAdapterTimeoutError(ModelAdapterError):
    """Raised when a transport process exceeds its execution timeout."""

    def __init__(
        self,
        message: str,
        *,
        timeout: float,
        argv: list[str],
    ) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.argv = argv


def validate_pinned_model(model: str | None) -> str:
    """Validate that model is an explicit pinned model selector.

    Refuses None, empty strings, whitespace, and generic unpinned selectors
    like 'auto', 'default', or 'latest' before any subprocess starts.
    """
    if model is None:
        raise ModelAdapterRefusalError(
            "Model adapter requires an explicit pinned model selector, got None"
        )
    if not isinstance(model, str) or not model.strip():
        raise ModelAdapterRefusalError(
            f"Model adapter requires a non-empty pinned model selector, got {model!r}"
        )
    cleaned = model.strip()
    lower = cleaned.lower()
    if (
        lower in DISALLOWED_UNPINNED_SELECTORS
        or lower.startswith("auto:")
        or lower == "auto"
        or lower.startswith("default:")
    ):
        raise ModelAdapterRefusalError(
            f"Model adapter refuses unpinned model selector {model!r}; "
            "an explicit pinned model is required"
        )
    return cleaned


class SubprocessRunner(Protocol):
    """Protocol for injectable subprocess execution."""

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def default_subprocess_runner(
    argv: list[str],
    *,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a text subprocess with clean subscription environment."""
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env) if env is not None else subscription_environment(),
        cwd=cwd,
        check=False,
    )


@dataclass(frozen=True)
class ModelAdapterResult(AnalyzerCallResult):
    """Result of a model adapter invocation with full execution provenance.

    Inherits from AnalyzerCallResult so worker and fact pipelines accept it directly.
    """

    model: str = ""
    argv: list[str] = field(default_factory=list)
    transport: str = ""


TransportName = Literal["cursor-agent", "agy"]


class ModelAdapter:
    """Subprocess transport adapter for local subscription model CLIs.

    Supports 'cursor-agent' and 'agy' transports with explicit model pinning,
    configurable timeout, verbatim stdout capture, and injectable subprocess
    runner.
    """

    def __init__(
        self,
        *,
        model: str,
        transport: TransportName | str = "cursor-agent",
        timeout_seconds: float = 120.0,
        runner: SubprocessRunner | Callable[..., subprocess.CompletedProcess[str]] | None = None,
        binary_path: str | None = None,
        effort: str | None = None,
        output_format: str | None = None,
    ) -> None:
        self.model = validate_pinned_model(model)
        if transport not in {"cursor-agent", "agy"}:
            raise ValueError(
                f"Unsupported transport {transport!r}; must be 'cursor-agent' or 'agy'"
            )
        self.transport: TransportName = transport  # type: ignore[assignment]
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner or default_subprocess_runner
        self.binary_path = binary_path
        self.effort = effort
        self.output_format = output_format

    def build_argv(self, prompt: str, schema: dict[str, Any] | None = None) -> list[str]:
        """Construct the exact CLI argument vector for the selected transport."""
        if self.transport == "cursor-agent":
            binary = self.binary_path or "cursor-agent"
            argv = [binary, "-f", "--model", self.model, "-p", prompt]
            if self.output_format:
                argv.extend(["--output-format", self.output_format])
            return argv
        elif self.transport == "agy":
            binary = (
                self.binary_path
                or shutil.which("agy")
                or str(Path.home() / ".local/bin/agy")
            )
            argv = [binary, "--model", self.model, "-p", prompt]
            if self.effort:
                argv.extend(["--effort", self.effort])
            if self.output_format:
                argv.extend(["--output-format", self.output_format])
            return argv
        else:
            raise ValueError(f"Unsupported transport: {self.transport}")

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> ModelAdapterResult:
        """Execute a model completion, capturing stdout and provenance."""
        # Refuse unpinned or empty model before any process starts
        validate_pinned_model(self.model)

        argv = self.build_argv(prompt, schema)
        env = subscription_environment()

        try:
            completed = self.runner(
                argv,
                timeout=self.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelAdapterTimeoutError(
                f"Model adapter timed out after {self.timeout_seconds}s "
                f"for transport '{self.transport}' and model '{self.model}'",
                timeout=self.timeout_seconds,
                argv=argv,
            ) from exc
        except OSError as exc:
            raise ModelAdapterExecutionError(
                f"Model adapter failed to execute transport '{self.transport}': {exc}",
                returncode=-1,
                argv=argv,
                stdout="",
                stderr=str(exc),
            ) from exc

        if completed.returncode != 0:
            err_msg = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"exit code {completed.returncode}"
            )
            raise ModelAdapterExecutionError(
                f"Model adapter process failed (exit {completed.returncode}) "
                f"for transport '{self.transport}' and model '{self.model}': {err_msg}",
                returncode=completed.returncode,
                argv=argv,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        return ModelAdapterResult(
            raw_output=completed.stdout,
            model=self.model,
            argv=argv,
            transport=self.transport,
        )

    def __call__(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> ModelAdapterResult:
        """Callable interface matching AnalyzerCallable signature."""
        return self.complete(prompt, schema)


def cursor_adapter(
    *,
    model: str,
    timeout_seconds: float = 120.0,
    runner: SubprocessRunner | Callable[..., subprocess.CompletedProcess[str]] | None = None,
    binary_path: str | None = None,
    output_format: str | None = None,
) -> ModelAdapter:
    """Create a ModelAdapter instance configured for the cursor-agent transport."""
    return ModelAdapter(
        model=model,
        transport="cursor-agent",
        timeout_seconds=timeout_seconds,
        runner=runner,
        binary_path=binary_path,
        output_format=output_format,
    )


def agy_adapter(
    *,
    model: str,
    timeout_seconds: float = 120.0,
    runner: SubprocessRunner | Callable[..., subprocess.CompletedProcess[str]] | None = None,
    binary_path: str | None = None,
    effort: str | None = None,
    output_format: str | None = None,
) -> ModelAdapter:
    """Create a ModelAdapter instance configured for the agy transport."""
    return ModelAdapter(
        model=model,
        transport="agy",
        timeout_seconds=timeout_seconds,
        runner=runner,
        binary_path=binary_path,
        effort=effort,
        output_format=output_format,
    )
