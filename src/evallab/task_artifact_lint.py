"""Own one static rule screening declared artifacts against separate verifier paths.

The paths are those a separate verifier grades from. This module never executes
task content.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.task_lint import Finding


_PROTECTED_ROOTS = (
    "/tests",
    "/logs/verifier",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
    "/var/lib",
    "/proc",
    "/sys",
    "/dev",
)


def lint_artifact_overwrites(task_dir: Path, artifacts: Sequence[str]) -> list[Finding]:
    """Screen artifacts after the caller confirms separate verifier mode."""
    # Imported here: task_lint imports this module while it loads.
    from evallab.task_lint import Finding

    findings: list[Finding] = []
    for artifact in artifacts:
        if not isinstance(artifact, str) or not artifact.startswith("/"):
            continue
        artifact = "/" + posixpath.normpath(artifact).lstrip("/")
        for root in _PROTECTED_ROOTS:
            if (
                artifact == root
                or artifact.startswith(root + "/")
                or root.startswith(artifact.rstrip("/") + "/")
            ):
                findings.append(
                    Finding(
                        "artifact-overwrites-verifier",
                        "error",
                        str(task_dir / "task.toml"),
                        f"declared artifact {artifact} covers {root}, which the verifier grades "
                        "from; Harbor uploads declared artifacts into the verifier container "
                        "before grading, so the agent can replace the grader, its reward file, "
                        "or the verifier interpreter and score 1; declare a path that holds "
                        "only task output",
                    )
                )
                break
    return findings
