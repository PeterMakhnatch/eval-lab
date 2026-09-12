"""Read-only static checks for Harbor task verifier trust boundaries.

These findings screen declarations and files; they do not certify verifier
correctness or replace oracle, NOP, and negative-mutant control runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evallab.task_workbench import Diagnostic, _parse_task_toml

_HIDDEN_INPUT = re.compile(r"""^\s*(?:COPY|ADD)\s+.*(?<=[\s/"'])(?:tests|solution)/""", re.IGNORECASE)
_EXPECTATION = re.compile(r"#\s*expect:\s*[01]\s*")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Literal["error", "warning"]
    path: str
    message: str


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
        # Docker removes full-line comments before joining continued instructions.
        contents = "\n".join(
            line
            for line in dockerfile.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        contents = re.sub(r"\\\n", " ", contents)
        for line in contents.splitlines():
            if _HIDDEN_INPUT.search(line):
                findings.append(
                    Finding(
                        "hidden-inputs-not-baked",
                        "error",
                        str(dockerfile),
                        "COPY/ADD references tests/ or solution/; hidden verifier inputs "
                        "must not enter the agent image",
                    )
                )

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
