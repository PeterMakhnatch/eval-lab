"""Read-only static checks for Harbor task verifier trust boundaries.

These findings screen declarations and files; they do not certify verifier
correctness or replace oracle, NOP, and negative-mutant control runs.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from evallab.task_workbench import Diagnostic, _parse_task_toml

_EXPECTATION = re.compile(r"#\s*expect:\s*[01]\s*")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Literal["error", "warning"]
    path: str
    message: str


def _docker_sources(arguments: str) -> list[str]:
    """Use the workbench's flag/JSON/shlex pattern, retaining operand validation."""
    payload = arguments.strip()
    while payload.startswith("--"):
        parts = payload.split(maxsplit=1)
        flag = shlex.split(parts[0])[0]
        payload = parts[1].lstrip() if len(parts) == 2 else ""
        name, separator, value = flag[2:].partition("=")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or (separator and not value):
            raise ValueError("malformed COPY/ADD flag; use --name=value")
        if not separator and name not in {"link", "parents", "keep-git-dir"}:
            raise ValueError(f"flag --{name} needs an explicit --{name}=value")
    # Like _docker_copy_sources in task_workbench, the last operand is never a source.
    operands = json.loads(payload) if payload.startswith("[") else shlex.split(payload)
    if (
        not isinstance(operands, list)
        or len(operands) < 2
        or any(not isinstance(item, str) or not item for item in operands)
    ):
        raise ValueError("COPY/ADD needs nonempty source operands and a final destination")
    if any(source.startswith("<<") for source in operands[:-1]):
        raise ValueError("COPY/ADD heredoc sources are unsupported by this static lint")
    return operands[:-1]


def _lint_dockerfile(dockerfile: Path) -> list[Finding]:
    findings: list[Finding] = []

    def syntax_error(line_number: int, message: str) -> None:
        findings.append(
            Finding(
                "dockerfile-copy-parses",
                "error",
                str(dockerfile),
                f"line {line_number}: {message}; fix or simplify the Dockerfile "
                "before relying on hidden-input screening",
            )
        )

    try:
        text = dockerfile.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        syntax_error(1, "Dockerfile must be UTF-8 text")
        return findings

    pending = ""
    start = 1
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("#"):
            if re.match(r"#\s*escape\s*=", line, re.IGNORECASE) and not line.endswith("\\"):
                syntax_error(number, "only backslash continuation escapes are supported")
            continue
        if not line:
            continue
        if not pending:
            start = number
        continued = line.endswith("\\")
        pending += line[:-1] if continued else line
        if continued:
            pending += " "
            continue
        instruction = re.match(r"(?i)^(COPY|ADD)\b(.*)$", pending)
        pending = ""
        if instruction is None:
            continue
        try:
            sources = _docker_sources(instruction[2])
        except ValueError as exc:
            syntax_error(start, str(exc))
            continue
        hidden = [
            source
            for source in sources
            if {"tests", "solution"}.intersection(PurePosixPath(source.casefold()).parts)
        ]
        if hidden:
            findings.append(
                Finding(
                    "hidden-inputs-not-baked",
                    "error",
                    str(dockerfile),
                    f"line {start}: COPY/ADD source operands {hidden!r} name tests or "
                    "solution; hidden verifier inputs must not enter the agent image",
                )
            )
    if pending:
        syntax_error(start, "unfinished continuation at end of Dockerfile")
    return findings


def lint_task(task_dir: Path) -> list[Finding]:
    """Inspect one task without executing or modifying any task content."""
    findings: list[Finding] = []
    manifest = task_dir / "task.toml"
    diagnostics: list[Diagnostic] = []
    config = _parse_task_toml(manifest, diagnostics)
    if not manifest.is_file() or diagnostics:
        findings.append(
            Finding("task-toml-parses", "error", str(manifest), "task.toml is missing or invalid TOML")
        )
    else:
        verifier = config.get("verifier")
        mode = verifier.get("environment_mode") if isinstance(verifier, dict) else None
        if mode != "separate":
            findings.append(
                Finding(
                    "verifier-isolation",
                    "warning",
                    str(manifest),
                    "the verifier runs inside the agent container and agent-side runtime "
                    "tampering (replacing pytest/git) can forge reward",
                )
            )
        elif not config.get("artifacts"):
            findings.append(
                Finding(
                    "artifacts-declared",
                    "error",
                    str(manifest),
                    'environment_mode = "separate" requires nonempty top-level artifacts; '
                    "nothing from the agent container reaches the verifier",
                )
            )

    solution = task_dir / "solution" / "solve.sh"
    if not solution.is_file():
        findings.append(
            Finding("solution-present", "error", str(solution), "solution/solve.sh is missing")
        )

    dockerfile = task_dir / "environment" / "Dockerfile"
    if dockerfile.is_file():
        findings.extend(_lint_dockerfile(dockerfile))

    for control in sorted((task_dir / "controls").glob("*.sh")):
        if not control.is_file() or not any(
            _EXPECTATION.fullmatch(line)
            for line in control.read_text(encoding="utf-8").splitlines()
        ):
            findings.append(
                Finding(
                    "controls-declare-expectation",
                    "error",
                    str(control),
                    "control script must contain a # expect: 0 or # expect: 1 header line",
                )
            )
    return findings


def discover_tasks(paths: Sequence[Path]) -> list[Path]:
    """Expand immediate task children; nonexistent inputs remain lint errors."""
    tasks: dict[Path, None] = {}
    for path in paths:
        candidates = (
            sorted(child for child in path.iterdir() if (child / "task.toml").is_file())
            if path.is_dir() and not (path / "task.toml").is_file()
            else [path]
        )
        for task in candidates:
            tasks[task] = None
    return list(tasks)
