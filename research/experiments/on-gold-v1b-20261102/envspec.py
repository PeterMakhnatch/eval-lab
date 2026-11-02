"""Derive the interpreter environment a MiMo task's grader uses, from the package's hidden-test patch.

A4 ran every synthesized contract test with the generic hook ``python3``. The graders run the task's own test
command inside a prepared environment (venv, pyenv shim, ``dev-switch``), so ``python3`` was often the wrong
interpreter (missing numpy/flask, wrong Python). The task's test command lives in the hidden test patch as
``mimo_test_command.sh`` and, for some tasks, inside a base64 tarball ``mimo_build_env.tar.gz.b64`` whose
``test_command.sh`` is the real runner. This module reads both and keeps only the environment lines.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile
from dataclasses import dataclass

_ADDED_FILE = r"diff --git a/{name} b/{name}.*?\n@@[^\n]*\n(.*?)(?=\ndiff --git|\Z)"

# Environment-only shell lines worth replaying before the contract test runs.
_ENV_LINE = re.compile(
    r"""^(
        export\s+(?!.*(?:_OPTS|PYTEST_|NODE_OPTIONS))\S+=.*
      | source\s+\S*activate\b.*
      | \.\s+\S*activate\b.*
      | eval\s+"\$\(pyenv\s+init[^)]*\)"
      | pyenv\s+global\s+\S+
      | dev-switch\s+\S+\s+\S+.*
      | cd\s+\S+
    )$""",
    re.X,
)
_EXPLICIT_PYTHON = re.compile(r"(?:^|\s)(/[\w./-]*/bin/python[\d.]*)\s+-m\s+pytest")
_WORKSPACE = re.compile(r"cd\s+/workspace/repo\b")


@dataclass(frozen=True)
class EnvSpec:
    workdir: str
    hook_command: str
    python_version: str  # "2.7" or "3"
    source: str  # which file the environment lines came from

    def summary(self) -> dict[str, str]:
        return {"workdir": self.workdir, "hook_command": self.hook_command, "python_version": self.python_version,
                "source": self.source}


def added_file_body(patch: str, name: str) -> str | None:
    """Text of a file the patch adds (``+`` lines of its hunk), or None when the patch does not add it."""
    m = re.search(_ADDED_FILE.format(name=re.escape(name)), patch, re.S)
    if not m:
        return None
    return "".join(line[1:] + "\n" for line in m.group(1).splitlines() if line.startswith("+"))


def build_env_test_command(patch: str) -> str | None:
    """``test_command.sh`` inside the patch's ``mimo_build_env.tar.gz.b64``, or None."""
    body = added_file_body(patch, "mimo_build_env.tar.gz.b64")
    if body is None:
        return None
    try:
        data = base64.b64decode("".join(body.split()))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith("test_command.sh"):
                    extracted = tf.extractfile(member)
                    return extracted.read().decode(errors="replace") if extracted else None
    except (ValueError, tarfile.TarError, OSError):
        return None
    return None


def derive_env_spec(test_patch: str) -> EnvSpec:
    """Environment of the task's grader: workdir, shell prelude + interpreter, Python major version."""
    build_env = build_env_test_command(test_patch)
    mimo = added_file_body(test_patch, "mimo_test_command.sh") or ""
    text, source = (build_env, "build_env") if build_env else (mimo, "mimo_test_command")
    workdir = "/workspace/repo" if _WORKSPACE.search(text or mimo) else "/testbed"
    prelude: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        core = re.sub(r"\s*(?:2>/dev/null|>/dev/null 2>&1)?\s*(?:\|\|\s*true)?$", "", line)
        if _ENV_LINE.match(core) and not (core.startswith("cd ") and "/" not in core):
            prelude.append(line)
    # absolute interpreter in the runner wins over whatever the prelude activates
    explicit = _EXPLICIT_PYTHON.search(text or "")
    if explicit:
        interpreter = explicit.group(1)
    else:
        interpreter = "python" if re.search(r"(?m)(?:^|[\s&;(])python\s", text or "") else "python3"
    version = "2.7" if re.search(r"2\.7\.\d+|python2", text or "") else "3"
    hook = "; ".join([f"cd {workdir}", *prelude, f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={workdir}:${{PYTHONPATH:-}} {interpreter}"])
    return EnvSpec(workdir=workdir, hook_command=hook, python_version=version, source=source)
