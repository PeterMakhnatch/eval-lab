"""Native verifier patch and command builders."""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from evallab.schemas import ContractModel

#: Models the repair proposer may address. Passed through verbatim; there is
#: no fallback and no silent substitution.
ALLOWED_MODELS = frozenset({"gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"})


class RepairError(ValueError):
    """Raised when a repair step violates its contract (fail closed)."""


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


class RepairTest(ContractModel):
    """One additional semantic test file, appended as a new-file diff to ``tests/test.patch``."""

    id: str = Field(min_length=1, max_length=64)
    #: New path inside the testbed repository, not a Harbor-package member.
    repo_path: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1)


_COMMAND_FORBIDDEN = re.compile(r"[;&|`$><\n]")


# --------------------------------------------------------------------------- #
# Patch-appendix and command-wrapper builders (v5 harness shape)
# --------------------------------------------------------------------------- #
# The v5 grader (``tests/test.sh``) applies ``/tests/test.patch`` with
# ``git apply`` in every graded phase and rewards exit 0 of
# ``sh -c "$(cat /tests/test_command.sh)"``. A repair therefore appends
# new-file diffs to ``tests/test.patch`` and wraps ``tests/test_command.sh``
# so the original command runs first, verbatim, with its exit status
# preserved, and the new tests run after it with the task interpreter.
# Both baseline and candidate phases see the same assertions because both
# apply the same patch and run the same command.


def read_task_workdir(package_dir: Path | str) -> str:
    """Absolute testbed workdir from the parent ``task.toml``."""
    path = Path(package_dir) / "task.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RepairError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RepairError(f"{path}: invalid TOML: {exc}") from exc
    workdir = (config.get("environment") or {}).get("workdir")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise RepairError(f"{path}: environment.workdir must be an absolute path")
    return workdir


def build_patch_appendix(tests: Sequence[RepairTest]) -> str:
    """New-file ``git apply`` diffs for the proposed tests, in order."""
    chunks: list[str] = []
    for test in tests:
        if not test.code.endswith("\n"):
            raise RepairError(f"{test.repo_path}: code must end with a newline")
        lines = test.code.splitlines()
        if not lines:
            raise RepairError(f"{test.repo_path}: code must not be empty")
        body = "".join(f"+{line}\n" for line in lines)
        chunks.append(
            f"diff --git a/{test.repo_path} b/{test.repo_path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{test.repo_path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
            f"{body}"
        )
    return "".join(chunks)


def build_command_wrapper(
    original: str,
    new_command: str,
    *,
    workdir: str,
    interpreter: str | None = "/opt/test_venv/bin/python3",
) -> str:
    """Original command verbatim, then the new tests; original status kept.

    Runs under ``sh -c`` (POSIX only): the original line runs first and its
    status is saved; the new tests run next from the task workdir; a failing
    new-test run exits 1, otherwise the original status is returned exactly.
    Bare ``python`` / ``python3`` in the new command use the task venv when
    that interpreter exists. The check runs in the grader, not on the host.
    ``None`` reproduces historical wrappers without interpreter preference;
    it does not change original-command or failure propagation.
    """
    base = original if original.endswith("\n") else original + "\n"
    python_preference = ""
    if interpreter is not None:
        quoted = shlex.quote(interpreter)
        bin_dir = shlex.quote(str(Path(interpreter).parent))
        python_preference = (
            f"if [ -x {quoted} ]; then\n"
            f"  PATH={bin_dir}:$PATH\n"
            "  export PATH\n"
            f'  python() {{ {quoted} "$@"; }}\n'
            f'  python3() {{ {quoted} "$@"; }}\n'
            "fi\n"
        )
    return (
        f"(\n{base})\n"
        "REPAIR_RC=$?\n"
        f"cd {shlex.quote(workdir)} || exit 1\n"
        f"{python_preference}"
        f"{new_command}\n"
        "REPAIR_NEW_RC=$?\n"
        'if [ "$REPAIR_NEW_RC" -ne 0 ]; then exit 1; fi\n'
        'exit "$REPAIR_RC"\n'
    )


_EDIT_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@([^\n]*)\n")
