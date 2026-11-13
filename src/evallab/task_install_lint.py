"""Static checks for task dependency pins and verifier-time installs.

`task_lint` owns the package rules. This module owns the two dependency-drift
rules: an install that will drift after authoring, and an install that runs
while grading. Both screen source text; neither executes a task file.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.task_lint import Finding


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Join backslash continuations and retain the first physical line number."""
    pending = ""
    start = 1
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            start = number
        continued = line.endswith("\\")
        pending += line[:-1] if continued else line
        if continued:
            pending += " "
            continue
        yield start, pending
        pending = ""
    if pending:
        yield start, pending


_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&", ";;"})
_INSTALL_VALUE_OPTIONS = frozenset(
    {
        "-r", "--requirement", "-c", "--constraint", "--build-constraint",
        "-e", "--editable", "-i", "--index-url", "--extra-index-url",
        "-p", "--python", "-f", "--find-links", "-t", "--target",
        "--prefix", "--root", "--cache-dir", "--index", "--default-index",
        "--trusted-host", "--proxy", "--cert", "--client-cert", "--timeout",
        "--retries", "--log", "--config-settings", "-C", "--platform",
        "--python-version", "--implementation", "--abi", "--only-binary",
        "--no-binary", "--keyring-provider", "--resolution", "--prerelease",
        "--exclude-newer", "--link-mode", "--python-platform", "--directory",
        "--project", "--with", "-w", "--from", "--src", "--extra", "--group",
        "--only-group", "--upgrade-strategy", "--progress-bar", "--report",
        "--global-option", "--install-option", "--build-option",
    }
)


def _command_head(tokens: list[str]) -> list[str]:
    """Remove assignments and execution wrappers before inspecting a command."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        wrapper = PurePosixPath(token).name
        if wrapper not in {"sudo", "env", "command", "exec"}:
            break
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            index += 1
            if option == "--":
                break
            if option in {"-u", "--user", "-g", "--group", "-h", "--host", "--unset", "-C"}:
                index += 1
    return tokens[index:]


def _shell_commands(tokens: list[str]) -> Iterator[tuple[list[str], str]]:
    start = 0
    for index, token in enumerate(tokens):
        if token in _COMMAND_SEPARATORS:
            if index > start:
                yield _command_head(tokens[start:index]), token
            start = index + 1
    if start < len(tokens):
        yield _command_head(tokens[start:]), ""


def _install_command(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens:
        return None
    head = PurePosixPath(tokens[0]).name
    if head in {"pip", "pip3"} and tokens[1:2] == ["install"]:
        return f"{head} install", tokens[2:]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", head) and tokens[1:4] == [
        "-m", "pip", "install"
    ]:
        return f"{head} -m pip install", tokens[4:]
    if head == "uv" and tokens[1:3] in (["pip", "install"], ["tool", "install"]):
        return f"uv {tokens[1]} install", tokens[3:]
    if head == "uvx":
        return "uvx --with", tokens[1:]
    return None


def _install_specs(arguments: list[str], *, uvx: bool) -> list[str]:
    specs: list[str] = []
    index = 0
    options = True
    # `uv tool install --from SPEC command` installs SPEC; the operand is a name.
    skip_command = False
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if options and argument == "--":
            options = False
            continue
        if options and argument.startswith("-"):
            option, separator, value = argument.partition("=")
            if option in _INSTALL_VALUE_OPTIONS:
                if not separator and index < len(arguments):
                    value = arguments[index]
                    index += 1
                if uvx and option in {"--with", "-w"} and value:
                    specs.append(value)
                if not uvx and option == "--from" and value:
                    specs.append(value)
                    skip_command = True
            continue
        if skip_command:
            skip_command = False
            continue
        if not uvx:
            if arguments[index:index + 1] == ["@"] and index + 1 < len(arguments):
                argument += f" @ {arguments[index + 1]}"
                index += 2
            specs.append(argument)
    return specs


def _unpinned_spec(spec: str) -> bool:
    if "==" in spec or "$" in spec or spec.startswith((".", "/", "~", "file:")):
        return False
    if "://" in spec or spec.startswith("git+"):
        return not (
            re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])", spec)
            or re.search(r"[#&]sha256=[0-9a-fA-F]{64}(?![0-9a-fA-F])", spec)
        )
    return "/" not in spec


def _install_tokens(line: str, *, dockerfile: bool) -> list[str]:
    if dockerfile:
        instruction = re.match(r"(?i)^RUN\s+(.*)$", line)
        if instruction is None:
            return []
        line = instruction[1].strip()
        if line.startswith("["):
            try:
                payload = json.loads(line)
            except ValueError:
                return []
            return payload if isinstance(payload, list) and all(
                isinstance(token, str) for token in payload
            ) else []
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def lint_installs(task_dir: Path) -> list[Finding]:
    """Screen build dependencies and verifier installs without executing them."""
    # Imported here: task_lint imports this module while it loads.
    from evallab.task_lint import Finding

    dockerfiles = [task_dir / directory / "Dockerfile" for directory in ("environment", "tests")]
    scripts = [
        path
        for path in sorted(task_dir.rglob("*.sh"))
        if not any(part.startswith(".") for part in path.relative_to(task_dir).parts)
    ]
    findings: list[Finding] = []
    for path in [*dockerfiles, *scripts]:
        if not path.is_file():
            continue
        dockerfile = path in dockerfiles
        verifier = not dockerfile and path.is_relative_to(task_dir / "tests")
        runtime_reported = False
        for number, line in _logical_lines(path.read_text(encoding="utf-8", errors="replace")):
            commands = list(_shell_commands(_install_tokens(line, dockerfile=dockerfile)))
            unpinned: list[str] = []
            installers: list[str] = []
            runtime_install = False
            for index, (command, separator) in enumerate(commands):
                if not command:
                    continue
                head = PurePosixPath(command[0]).name
                install = _install_command(command)
                if install is not None:
                    label, arguments = install
                    specs = [
                        spec for spec in _install_specs(arguments, uvx=head == "uvx")
                        if _unpinned_spec(spec)
                    ]
                    if specs:
                        installers.append(label)
                        unpinned.extend(specs)
                runtime_install |= head in {"apt-get", "uvx"} or install is not None
                if head in {"curl", "wget"} and separator in {"|", "|&"}:
                    following = commands[index + 1][0] if index + 1 < len(commands) else []
                    runtime_install |= bool(following) and PurePosixPath(following[0]).name in {
                        "sh", "bash", "zsh", "dash", "ksh"
                    }
            if unpinned:
                findings.append(
                    Finding(
                        "unpinned-python-dependency",
                        "warning",
                        str(path),
                        f"line {number}: {', '.join(installers)} installs unpinned python "
                        f"dependencies: {', '.join(unpinned)}; pin each with == so the image "
                        "cannot drift",
                    )
                )
            if verifier and runtime_install and not runtime_reported:
                findings.append(
                    Finding(
                        "verifier-runtime-install",
                        "warning",
                        str(path),
                        f"line {number}: the verifier installs or fetches tools while grading; "
                        "bake them into tests/Dockerfile so a mirror failure cannot turn a "
                        "correct answer into 0",
                    )
                )
                runtime_reported = True
    return findings
