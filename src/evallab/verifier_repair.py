from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from evallab.schemas import ContractModel

ALLOWED_MODELS = frozenset({"gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"})


class RepairError(ValueError):
    pass


class RepairTest(ContractModel):

    id: str = Field(min_length=1, max_length=64)
    repo_path: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1)


_COMMAND_FORBIDDEN = re.compile(r"[;&|`$><\n]")


def read_task_workdir(package_dir: Path | str) -> str:
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
