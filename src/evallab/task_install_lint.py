from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from evallab.shell_lint import command_head, logical_lines, shell_commands, split_shell

if TYPE_CHECKING:
    from evallab.task_lint import Finding


_FROZEN_UNPINNED: dict[tuple[str, str], frozenset[str]] = {
    ("tau3-retail-1", "environment/runtime-server/Dockerfile"): frozenset(
        {"fastmcp>=3.0"}
    ),
}

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

_XARGS_VALUE_OPTIONS = frozenset(
    {
        "-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "--replace",
        "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs",
        "-s", "--max-chars", "--process-slot-var",
    }
)
_ARTIFACT_SUBSTITUTION = re.compile(
    r"""\$\(\s*(?:cat\s+|<\s*)(?P<dollar>"[^"\n]+"|'[^'\n]+'|[^\s()]+)\s*\)"""
    r"""|`\s*cat\s+(?P<backtick>"[^"\n]+"|'[^'\n]+'|[^\s`]+)\s*`"""
)


def _option_end(tokens: list[str], index: int) -> int:
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        option, separator, _ = token.partition("=")
        index += 1
        if not separator and option in _INSTALL_VALUE_OPTIONS and index < len(tokens):
            index += 1
    return index


def _install_command(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens:
        return None
    head = PurePosixPath(tokens[0]).name
    if head in {"pip", "pip3"}:
        index = _option_end(tokens, 1)
        if tokens[index:index + 1] == ["install"]:
            return f"{head} install", tokens[index + 1:]
        return None
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", head):
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            if tokens[index] == "-m":
                break
            option = tokens[index]
            index += 1
            if option in {"-X", "-W"} and index < len(tokens):
                index += 1
        if tokens[index:index + 1] != ["-m"]:
            return None
        index = _option_end(tokens, index + 1)
        if tokens[index:index + 1] != ["pip"]:
            return None
        index = _option_end(tokens, index + 1)
        if tokens[index:index + 1] == ["install"]:
            return f"{head} -m pip install", tokens[index + 1:]
        return None
    if head == "uv":
        index = _option_end(tokens, 1)
        if tokens[index:index + 1] == ["pip"]:
            inner = _option_end(tokens, index + 1)
            if tokens[inner:inner + 1] == ["install"]:
                return "uv pip install", tokens[inner + 1:]
            return None
        if tokens[index:index + 1] == ["tool"]:
            inner = _option_end(tokens, index + 1)
            if tokens[inner:inner + 1] == ["install"]:
                return "uv tool install", tokens[inner + 1:]
            return None
        return None
    if head == "uvx":
        return "uvx --with", tokens[1:]
    return None


def _install_specs(arguments: list[str], *, uvx: bool) -> list[str]:
    specs: list[str] = []
    index = 0
    options = True
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
                if uvx and option in {"--with", "-w", "--from"} and value:
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
        parts = line.split(None, 1)
        while parts and parts[0].startswith("--"):
            line = parts[1] if len(parts) > 1 else ""
            parts = line.split(None, 1)
        if line.startswith("["):
            try:
                payload = json.loads(line)
            except ValueError:
                return []
            return payload if isinstance(payload, list) and all(
                isinstance(token, str) for token in payload
            ) else []
    return split_shell(line)


def _is_dockerfile(path: Path) -> bool:
    return path.name == "Dockerfile" or path.name.startswith("Dockerfile.")


def _download_command(command: list[str]) -> bool:
    head = PurePosixPath(command[0]).name
    if head in {"curl", "wget"}:
        return any(not token.startswith("-") for token in command[1:])
    return head == "git" and "clone" in command[1:]


def _checksum_command(command: list[str]) -> bool:
    head = PurePosixPath(command[0]).name
    arguments = command[1:]
    if not any(token in {"-c", "--check"} for token in arguments):
        return False
    if head == "sha256sum":
        return True
    if head != "shasum":
        return False
    for index, token in enumerate(arguments):
        if token in {"-a256", "--algorithm=256"}:
            return True
        if token in {"-a", "--algorithm"} and arguments[index + 1:index + 2] == ["256"]:
            return True
    return False


def _task_sources(task_dir: Path) -> list[Path]:
    sources = [
        path
        for path in task_dir.rglob("*")
        if path.is_file() and (_is_dockerfile(path) or path.suffix == ".sh")
    ]
    return sorted(sources)


def _python_install_inputs(arguments: list[str]) -> Iterator[str]:
    index = 0
    options = True
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if options and argument == "--":
            options = False
        elif options and argument.startswith("-"):
            option, separator, value = argument.partition("=")
            if option in _INSTALL_VALUE_OPTIONS:
                if not separator and index < len(arguments):
                    value = arguments[index]
                    index += 1
                if option in {"-r", "--requirement"} and value:
                    yield value
        else:
            yield argument


def _xargs_inputs(command: list[str]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    index = 1
    while index < len(command):
        argument = command[index]
        index += 1
        if argument == "--":
            break
        if not argument.startswith("-"):
            index -= 1
            break
        option, separator, value = argument.partition("=")
        if option in _XARGS_VALUE_OPTIONS:
            if not separator and index < len(command):
                value = command[index]
                index += 1
            if option in {"-a", "--arg-file"} and value:
                files.append(value)
    return files, command_head(command[index:])


def _installs_packages(command: list[str]) -> bool:
    return _install_command(command) is not None or bool(
        command
        and PurePosixPath(command[0]).name in {"apt-get", "apt"}
        and "install" in command[1:]
    )


def _agent_dependency_artifacts(
    line: str, commands: list[tuple[list[str], str]], artifacts: set[str]
) -> list[str]:
    references: list[str] = []
    installing = False
    for command, _ in commands:
        install = _install_command(command)
        if install is not None:
            references.extend(_python_install_inputs(install[1]))
        installing |= _installs_packages(command)
        if command and PurePosixPath(command[0]).name == "xargs":
            files, operand = _xargs_inputs(command)
            if _installs_packages(operand):
                references.extend(files)
                installing = True
    if installing:
        for match in _ARTIFACT_SUBSTITUTION.finditer(line):
            value = match.group("dollar") or match.group("backtick")
            references.append(value[1:-1] if value.startswith(("'", '"')) else value)
    return sorted(
        dict.fromkeys(reference for reference in references if reference in artifacts),
        key=line.find,
    )


def lint_installs(task_dir: Path, artifacts: Sequence[str] = ()) -> list[Finding]:
    from evallab.task_lint import Finding

    findings: list[Finding] = []
    declared = {
        artifact.rstrip("/") or "/"
        for artifact in artifacts
        if PurePosixPath(artifact).is_absolute()
    }
    for path in _task_sources(task_dir):
        dockerfile = _is_dockerfile(path)
        under_tests = path.is_relative_to(task_dir / "tests")
        verifier = not dockerfile and under_tests
        accepted = _FROZEN_UNPINNED.get(
            (task_dir.name, path.relative_to(task_dir).as_posix()), frozenset()
        )
        runtime_reported = False
        network_download_line: int | None = None
        script_checksum = False
        for number, line in logical_lines(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            commands = list(shell_commands(_install_tokens(line, dockerfile=dockerfile)))
            unpinned: list[str] = []
            installers: list[str] = []
            runtime_install = False
            download = False
            checksum = False
            for index, (command, separator) in enumerate(commands):
                if not command:
                    continue
                head = PurePosixPath(command[0]).name
                if under_tests:
                    download |= _download_command(command)
                    checksum |= _checksum_command(command)
                install = _install_command(command)
                if install is not None:
                    label, arguments = install
                    specs = [
                        spec
                        for spec in _install_specs(arguments, uvx=head == "uvx")
                        if _unpinned_spec(spec) and spec not in accepted
                    ]
                    if specs:
                        installers.append(label)
                        unpinned.extend(specs)
                runtime_install |= head in {"apt-get", "uvx"} or install is not None
                if head in {"curl", "wget"} and separator in {"|", "|&"}:
                    following = commands[index + 1][0] if index + 1 < len(commands) else []
                    runtime_install |= bool(following) and PurePosixPath(
                        following[0]
                    ).name in {"sh", "bash", "zsh", "dash", "ksh"}
            if download and not (dockerfile and checksum):
                network_download_line = network_download_line or number
            if not dockerfile:
                script_checksum |= checksum
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
            if under_tests and declared:
                for artifact in _agent_dependency_artifacts(line, commands, declared):
                    findings.append(
                        Finding(
                            "verifier-installs-agent-dependencies",
                            "warning",
                            str(path),
                            f"line {number}: the verifier installs packages from the "
                            f"agent-written artifact {artifact}; the agent writes that file "
                            "before grading, so one entry can load a pytest plugin or startup "
                            "hook and forge the pass; install from a verifier-owned list instead",
                        )
                    )
        if network_download_line is not None and not script_checksum:
            findings.append(
                Finding(
                    "verifier-network-download",
                    "warning",
                    str(path),
                    f"line {network_download_line}: the verifier downloads from the network "
                    "without a checksum; check the bytes with sha256sum -c in the same "
                    f"{'RUN instruction' if dockerfile else 'script'} so a moved or vanished "
                    "remote cannot turn a correct answer into 0",
                )
            )
    return findings
