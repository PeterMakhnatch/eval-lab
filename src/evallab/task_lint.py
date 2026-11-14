"""Read-only static checks for Harbor task verifier trust boundaries.

These findings screen declarations and files; they do not certify verifier
correctness or replace oracle, NOP, and negative-mutant control runs.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab.control_expect import declared_expectation
from evallab.task_install_lint import _install_tokens, _shell_commands, lint_installs


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Literal["error", "warning"]
    path: str
    message: str


def _docker_operands(arguments: str) -> list[str]:
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
    # The last operand is the destination, never a source.
    operands = json.loads(payload) if payload.startswith("[") else shlex.split(payload)
    if (
        not isinstance(operands, list)
        or len(operands) < 2
        or any(not isinstance(item, str) or not item for item in operands)
    ):
        raise ValueError("COPY/ADD needs nonempty source operands and a final destination")
    if any(source.startswith("<<") for source in operands[:-1]):
        raise ValueError("COPY/ADD heredoc sources are unsupported by this static lint")
    return operands


def _docker_sources(arguments: str) -> list[str]:
    """The COPY/ADD source operands; the final destination is never a source."""
    return _docker_operands(arguments)[:-1]


_ABSOLUTE_PATH = re.compile(r"(?<![\w./:-])/(?:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")


def _posix_path(text: str) -> str:
    return text.rstrip("/") or "/"


def _contains(path: str, base: str) -> bool:
    return path == base or path.startswith(base + "/")


def _top_directory(path: str) -> str:
    parts = PurePosixPath(path).parts
    return str(PurePosixPath(*parts[:2])) if len(parts) > 2 else path


def _absolute_paths(text: str) -> set[str]:
    return {_posix_path(match.group(0)).rstrip(".") for match in _ABSOLUTE_PATH.finditer(text)}


# Commands whose operands are paths the verifier image creates. `mkdir`,
# `touch`, and `tee` create every operand; `cp`, `mv`, and `ln` create the last.
_CREATE_ALL_OPERANDS = frozenset({"mkdir", "touch", "tee"})
_CREATE_LAST_OPERAND = frozenset({"cp", "mv", "ln"})
_REDIRECTION = re.compile(r"(?<![<>&])(?:>>?|&>)\s*([^\s;&|()<>]+)")


def _command_created_paths(command: Sequence[str]) -> set[str]:
    """Absolute paths one shell command creates, as far as its tokens show."""
    if not command:
        return set()
    head = PurePosixPath(command[0]).name
    arguments = command[1:]
    operands = [argument for argument in arguments if not argument.startswith("-")]
    if head in _CREATE_ALL_OPERANDS:
        return {argument for argument in operands if argument.startswith("/")}
    if head == "install":
        directories = any(argument in {"-d", "--directory"} for argument in arguments)
        targets = operands if directories else operands[-1:]
        return {argument for argument in targets if argument.startswith("/")}
    if head in _CREATE_LAST_OPERAND:
        return {argument for argument in operands[-1:] if argument.startswith("/")}
    return set()


def _run_created_paths(line: str) -> set[str]:
    """Absolute paths one Dockerfile RUN instruction writes into the verifier image."""
    created = {match.group(1) for match in _REDIRECTION.finditer(line)}
    for command, _separator in _shell_commands(_install_tokens(line, dockerfile=True)):
        created |= _command_created_paths(command)
    return {_posix_path(path) for path in created if path.startswith("/")}


def _dockerfile_created_paths(dockerfile: Path) -> set[str]:
    """Absolute paths one verifier Dockerfile creates: COPY/ADD destinations and RUN writes."""
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    created: set[str] = set()
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += line[:-1] if continued else line
        if continued:
            pending += " "
            continue
        instruction = re.match(r"(?i)^(COPY|ADD|RUN)\b(.*)$", pending)
        if instruction is not None:
            if instruction[1].upper() == "RUN":
                created |= _run_created_paths(pending)
            else:
                try:
                    destination = _docker_operands(instruction[2])[-1]
                except ValueError:
                    destination = ""
                if destination.startswith("/"):
                    created.add(_posix_path(destination))
        pending = ""
    return created


def _verifier_owned_paths(tests: Path, artifacts: Sequence[str]) -> set[str]:
    """Paths the verifier image creates itself, so a read of them is not hidden input.

    Harbor creates the parents of declared artifacts to land them, so a created
    directory that holds an artifact proves nothing about the other files below
    it; only created paths outside those parents are verifier-owned.
    """
    if not tests.is_dir():
        return set()
    created: set[str] = set()
    for dockerfile in sorted(tests.rglob("Dockerfile*")):
        if dockerfile.is_file():
            created |= _dockerfile_created_paths(dockerfile)
    return {
        path for path in created if not any(_contains(artifact, path) for artifact in artifacts)
    }


def _artifact_contract_findings(task_dir: Path, artifacts: Sequence[str]) -> list[Finding]:
    artifacts = [
        _posix_path(path) for path in artifacts if isinstance(path, str) and path.startswith("/")
    ]
    if not artifacts:
        return []
    findings: list[Finding] = []
    declared = ", ".join(artifacts)

    def relates(path: str, artifact: str) -> bool:
        return _contains(path, artifact) or _contains(artifact, path)

    instruction = task_dir / "instruction.md"
    instructed = (
        _absolute_paths(instruction.read_text(encoding="utf-8", errors="replace"))
        if instruction.is_file()
        else set()
    )
    unmentioned = [artifact for artifact in artifacts if artifact not in instructed]
    if unmentioned:
        message = (
            f"instruction.md is missing; the agent cannot know what to produce ({declared})"
            if not instruction.is_file()
            else "the instruction never names a declared artifact path as its own path: "
            f"{', '.join(unmentioned)}; the agent cannot know what to produce"
        )
        findings.append(Finding("artifacts-instructed", "warning", str(instruction), message))

    tests = task_dir / "tests"
    paths_by_file = {
        tests_file: _absolute_paths(tests_file.read_text(encoding="utf-8", errors="replace"))
        for tests_file in (sorted(tests.rglob("*")) if tests.is_dir() else [])
        if tests_file.is_file()
    }
    verified_paths = set().union(*paths_by_file.values())
    unverified = [
        artifact
        for artifact in artifacts
        if not any(relates(path, artifact) for path in verified_paths)
    ]
    if not tests.is_dir():
        findings.append(
            Finding(
                "artifacts-verified",
                "warning",
                str(tests),
                "tests/ is missing; the separate verifier cannot observe the produced artifact "
                f"({declared})",
            )
        )
    elif unverified:
        findings.append(
            Finding(
                "artifacts-verified",
                "warning",
                str(tests),
                "no file under tests/ names the declared artifact path(s) "
                f"{', '.join(unverified)}, a path above them, or a path below them; "
                "the separate verifier cannot observe them",
            )
        )
    top_directories = {_top_directory(artifact) for artifact in artifacts}
    verifier_owned = _verifier_owned_paths(tests, artifacts)
    for tests_file, paths in paths_by_file.items():
        # Fixture data names whatever the test data holds; it is not a read.
        if tests_file.relative_to(tests).parts[0] == "fixtures":
            continue
        for path in sorted(paths):
            if any(_contains(path, owned) for owned in verifier_owned):
                continue
            if any(_contains(path, top) for top in top_directories) and not any(
                relates(path, artifact) for artifact in artifacts
            ):
                findings.append(
                    Finding(
                        "verifier-reads-undeclared",
                        "warning",
                        str(tests_file),
                        f"the verifier reads {path}, which is outside the declared artifacts; "
                        f"a separate verifier sees only ({declared})",
                    )
                )
    return findings


def _hidden_docker_source(source: str, context: Path) -> bool:
    """Screen one COPY/ADD source operand against the Docker build context.

    Harbor builds environment/Dockerfile with environment/ as the build context,
    so a source naming tests or solution enters the agent image only from that
    context. An operand that resolves to an existing context path is the task's
    own agent-visible material; one that escapes the context, follows a symlink
    out of it, or matches nothing still reaches for hidden verifier inputs.
    """
    if not {"tests", "solution"}.intersection(PurePosixPath(source.casefold()).parts):
        return False
    root = context.resolve()
    try:
        resolved = (root / source.lstrip("/")).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return True
    return not resolved.exists()


_SHA256_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-fA-F]{64}\Z")
_GIT_COMMIT = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_GIT_CLONE = re.compile(r"(?<![\w-])git\s+clone(?![\w-])")


def _from_operands(arguments: str) -> tuple[str, str | None]:
    """Return the image operand and the optional stage name of one FROM line."""
    tokens = arguments.split()
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    if not tokens:
        return "", None
    stage = tokens[2] if len(tokens) >= 3 and tokens[1].casefold() == "as" else None
    return tokens[0], stage


def _from_findings(
    dockerfile: Path, line_number: int, arguments: str, stages: set[str]
) -> list[Finding]:
    """Warn when a base image can resolve to different bytes at build time."""
    image, stage = _from_operands(arguments)
    resolved = image.casefold()
    is_stage = resolved in stages
    if stage is not None:
        stages.add(stage.casefold())
    if not image or resolved == "scratch" or is_stage:
        return []
    if "$" in image:
        return [
            Finding(
                "unpinned-image",
                "warning",
                str(dockerfile),
                f"line {line_number}: FROM {image} names a build argument, so the base "
                "image is chosen at build time and cannot be checked; pin the resolved "
                "image with an @sha256: digest",
            )
        ]
    if _SHA256_IMAGE_DIGEST.search(image):
        return []
    return [
        Finding(
            "unpinned-image",
            "warning",
            str(dockerfile),
            f"line {line_number}: FROM {image} has no sha256 digest; a tag can move to "
            "different image bytes, so pin image@sha256:<digest> before a run",
        )
    ]


def _run_findings(dockerfile: Path, line_number: int, arguments: str) -> list[Finding]:
    """Warn when a Dockerfile clone takes a branch or tag head instead of a commit."""
    if not _GIT_CLONE.search(arguments) or _GIT_COMMIT.search(arguments):
        return []
    return [
        Finding(
            "unpinned-clone",
            "warning",
            str(dockerfile),
            f"line {line_number}: RUN git clone names no 40-hex commit; a branch or tag "
            "head can move to other bytes, so check out a full commit for reproducible "
            "evidence",
        )
    ]


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
    stages: set[str] = set()
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
        instruction = re.match(r"(?i)^(FROM|COPY|ADD|RUN)\b(.*)$", pending)
        pending = ""
        if instruction is None:
            continue
        keyword = instruction[1].upper()
        arguments = instruction[2]
        if keyword == "FROM":
            findings.extend(_from_findings(dockerfile, start, arguments, stages))
            continue
        if keyword == "RUN":
            findings.extend(_run_findings(dockerfile, start, arguments))
            continue
        try:
            sources = _docker_sources(arguments)
        except ValueError as exc:
            syntax_error(start, str(exc))
            continue
        hidden = [source for source in sources if _hidden_docker_source(source, dockerfile.parent)]
        if hidden:
            findings.append(
                Finding(
                    "hidden-inputs-not-baked",
                    "error",
                    str(dockerfile),
                    f"line {start}: COPY/ADD source operands {hidden!r} name tests or "
                    "solution and do not resolve inside the environment build context; "
                    "hidden verifier inputs must not enter the agent image",
                )
            )
    if pending:
        syntax_error(start, "unfinished continuation at end of Dockerfile")
    return findings


_SOURCE_PARSERS: tuple[tuple[str, frozenset[str]], ...] = (
    ("solution", frozenset({".sh", ".py"})),
    ("controls", frozenset({".sh"})),
    ("tests", frozenset({".sh", ".py", ".json"})),
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _parse_source(path: Path, bash: str | None) -> str | None:
    """Return the parser message for one source file, or None when it parses."""
    if path.suffix == ".sh":
        if bash is None:
            return None
        result = subprocess.run(
            [bash, "-n", str(path)],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        # bash 4 and newer report an unterminated heredoc as a warning on stderr
        # while still exiting 0, so a diagnostic alone is enough to fail.
        if result.returncode != 0 or result.stderr.strip():
            detail = _one_line(result.stderr) or f"exit {result.returncode}"
            return f"bash -n: {detail}"
        return None
    label = "compile" if path.suffix == ".py" else "json.loads"
    try:
        if path.suffix == ".py":
            compile(path.read_bytes(), str(path), "exec")
        else:
            json.loads(path.read_bytes())
    except (SyntaxError, ValueError) as error:
        if isinstance(error, SyntaxError):
            line = f" (line {error.lineno})" if error.lineno is not None else ""
            return f"{label}: {_one_line(error.msg)}{line}"
        return f"{label}: {_one_line(str(error))}"
    return None


def _bash_parser() -> str | None:
    """The bash that parses shell sources, or None when bash is not on PATH."""
    return shutil.which("bash")


def _lint_sources(task_dir: Path) -> list[Finding]:
    """Parse task sources statically; nothing here executes or writes task content."""
    bash = _bash_parser()
    findings: list[Finding] = []
    for directory, suffixes in _SOURCE_PARSERS:
        for path in sorted((task_dir / directory).rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            message = _parse_source(path, bash)
            if message is not None:
                findings.append(Finding("sources-parse", "error", str(path), message))
    return findings


def lint_task(task_dir: Path) -> list[Finding]:
    """Inspect one task without executing or modifying any task content."""
    findings: list[Finding] = []
    artifact_findings: list[Finding] = []
    manifest = task_dir / "task.toml"
    diagnostics: list[Diagnostic] = []
    config = _parse_task_toml(manifest, diagnostics)
    if not manifest.is_file() or diagnostics:
        findings.append(
            Finding(
                "task-toml-parses", "error", str(manifest), "task.toml is missing or invalid TOML"
            )
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
        else:
            artifacts = config.get("artifacts")
            if isinstance(artifacts, list):
                artifact_findings = _artifact_contract_findings(task_dir, artifacts)

    solution = task_dir / "solution" / "solve.sh"
    if not solution.is_file():
        findings.append(
            Finding("solution-present", "error", str(solution), "solution/solve.sh is missing")
        )

    for context in ("environment", "tests"):
        for dockerfile in sorted((task_dir / context).rglob("Dockerfile")):
            if dockerfile.is_file():
                findings.extend(_lint_dockerfile(dockerfile))

    for control in sorted((task_dir / "controls").glob("*.sh")):
        if not control.is_file() or declared_expectation(control) is None:
            findings.append(
                Finding(
                    "controls-declare-expectation",
                    "error",
                    str(control),
                    "control script must contain a # expect: 0 or # expect: 1 header line "
                    "in its first 20 lines",
                )
            )
    findings.extend(_lint_sources(task_dir))
    findings.extend(lint_installs(task_dir))
    findings.extend(artifact_findings)
    return findings


def _task_packages(root: Path) -> Iterator[Path]:
    """Task directories below `root`, skipping hidden directories.

    A directory that holds `task.toml` is one task, and its subdirectories are
    never searched.
    """
    pending = [root]
    while pending:
        for child in pending.pop().iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / "task.toml").is_file():
                yield child
            else:
                pending.append(child)


def discover_tasks(paths: Sequence[Path]) -> list[Path]:
    """Expand task packages under each path, sorted by posix path.

    A path that is itself a task stays one task. Any other existing directory
    is searched recursively, so collection roots may nest packages. A path that
    does not exist stays one candidate, and lint reports its missing manifest.
    """
    tasks: dict[Path, None] = {}
    for path in paths:
        if path.is_dir() and not (path / "task.toml").is_file():
            for task in _task_packages(path):
                tasks[task] = None
        else:
            tasks[path] = None
    return sorted(tasks, key=lambda task: task.as_posix())


LEAKAGE_DIAGNOSTIC_CODES = frozenset(
    {"agent_image_hidden_leak", "golden_data_leak", "hidden_artifact_exposure"}
)
ISOLATION_DIAGNOSTIC_CODES = frozenset(
    {
        "agent_env_unsupported",
        "build_context_unreadable",
        "build_network_use",
        "build_proof_invalid",
        "build_proof_lockfile_mismatch",
        "build_proof_lockfile_missing",
        "build_proof_unpinned_dependency",
        "collect_hooks_invalid",
        "collect_service_invalid",
        "compose_build_path_escape",
        "compose_feature_unsupported",
        "compose_host_ports_unsupported",
        "compose_image_unpinned",
        "compose_main_service_missing",
        "compose_network_mode_unsupported",
        "compose_main_env_credential_unauthorized",
        "compose_main_env_literal_secret",
        "compose_networks_unsupported",
        "compose_privileged_unsupported",
        "compose_sidecar_env_invalid",
        "compose_sidecar_env_unauthorized",
        "compose_structure_invalid",
        "compose_syntax_error",
        "compose_topology_invalid",
        "compose_volume_escape",
        "compose_volume_invalid",
        "compose_volume_mount_invalid",
        "compose_volume_unauthorized",
        "custom_compose_unsupported",
        "mcp_server_host_invalid",
        "mcp_server_unbound",
        "mcp_servers_invalid",
        "mcp_transport_unsupported",
        "mcp_url_auth_invalid",
        "mcp_url_invalid",
        "mcp_url_path_missing",
        "mcp_url_port_missing",
        "mcp_url_scheme_invalid",
        "path_escape",
        "prebuilt_image_unsupported",
        "solution_env_unsupported",
        "symlink_unsupported",
        "verifier_collect_unsupported",
        "verifier_credential_unauthorized",
        "verifier_env_invalid",
        "verifier_env_literal_secret",
        "verifier_not_isolated",
        "verifier_network_not_isolated",
        "verifier_phase_network_not_isolated",
        "control_network_binding_mismatch",
        "control_network_isolation_missing",
        "control_stage_tampered",
        "control_task_digest_mismatch",
        "control_task_identity_mismatch",
        "control_verifier_not_isolated",
    }
)


Severity = Literal["error", "warning", "info"]
Classification = Literal["task_defect", "harness_defect", "agent_failure", "expected"]


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    classification: Classification
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "classification": self.classification,
            "path": self.path,
            "message": self.message,
        }


def _diag(
    code: str,
    path: str,
    message: str,
    *,
    severity: Severity = "error",
    classification: Classification = "task_defect",
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        code=code,
        classification=classification,
        path=path,
        message=message,
    )


def _parse_task_toml(path: Path, diagnostics: list[Diagnostic]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        diagnostics.append(_diag("task_toml_invalid", "task.toml", type(exc).__name__))
        return {}
    if not isinstance(parsed, dict):
        diagnostics.append(_diag("task_toml_invalid", "task.toml", "top level is not a table"))
        return {}
    return parsed
