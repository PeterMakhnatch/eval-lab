"""Read-only static checks for Harbor task verifier trust boundaries.

These findings screen declarations and files; they do not certify verifier
correctness or replace oracle, NOP, and negative-mutant control runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import stat
import tarfile
import tomllib
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import yaml

_EXPECTATION = re.compile(r"#\s*expect:\s*[01]\s*")

# Agent-visible leak markers for converted (e.g. MiMo) tasks: grading secrets
# an agent could read before grading. Spec-necessary API names are excluded on
# purpose (the instruction must name the interface under test); anything here
# is material the hidden grader depends on.
_MIMO_LEAK_PATTERNS = (
    re.compile(r"reward\.(json|txt)"),
    re.compile(r"/tests/"),
    re.compile(r"verifier_meta"),
    re.compile(r"expected_func|EXPECTED_FUNC"),
)
_MIMO_HIDDEN_TEST_NAME = re.compile(r"func (Test\w+)\(")

# Audit-ported exploitability signals (MimoFaultAudit H2/H3/N4): the hidden
# grading command is visible text (test_command.sh), patch text, or shell
# scripts embedded in the test patch as mimo_build_env.tar.gz.b64.
_GO_TEST_LINE = re.compile(r"\bgo\s+test\s+([^;&|\n]*)")
_PYTEST_TOKEN = re.compile(r"\bpytest\b")
_NETWORK_INSTALL = re.compile(
    r"\bpip install\b|\bnpm install\b|\bgo install\b|\bcurl\s+[^\n]*?https?://"
)
_GO_FLAG_WITH_VALUE = frozenset({"-run", "-count", "-timeout", "-tags", "-cpu", "-parallel"})


def _build_env_scripts(
    patch: str,
    *,
    select: Callable[[str], bool] | None = None,
    file_cap: int | None = None,
) -> list[tuple[str, str]]:
    """Named shell sources, decoded in memory without extraction or execution.

    An optional member selector and byte cap run before reading a member body.
    """
    start = patch.find("+++ b/mimo_build_env.tar.gz.b64")
    if start < 0:
        return []
    lines: list[str] = []
    in_file = False
    for line in patch[start : start + 4_000_000].splitlines():
        if line.startswith("+++ b/mimo_build_env"):
            in_file = True
            continue
        if line.startswith("diff --git") and in_file:
            break
        if in_file and line.startswith("+"):
            lines.append(line[1:])
    if not lines:
        return []
    import base64
    import io
    import tarfile

    try:
        raw = base64.b64decode("".join(lines))
        scripts: list[tuple[str, str]] = []
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                if not (member.isfile() and member.name.endswith(".sh")):
                    continue
                if select is not None and not select(member.name):
                    continue
                if file_cap is not None and member.size > file_cap:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                scripts.append((member.name, handle.read().decode("utf-8", "replace")))
        return scripts
    except (OSError, ValueError, tarfile.TarError):
        return []


def _hidden_shell_blobs(task_dir: Path) -> list[str]:
    """Grading shell text: visible command, patch text, decoded build-env scripts."""
    blobs: list[str] = []
    for name in ("test_command.sh", "test.sh"):
        path = task_dir / "tests" / name
        if path.is_file():
            try:
                blobs.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    patch_path = task_dir / "tests" / "test.patch"
    if patch_path.is_file():
        try:
            patch = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return blobs
        blobs.append(patch)
        blobs.extend(text for _, text in _build_env_scripts(patch))
    return blobs


def _go_package_wide(invocation: str) -> bool:
    """Whether a ``go test ...`` argument string grades a whole package tree."""
    skip_next = False
    for token in invocation.split():
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if "=" not in token and token in _GO_FLAG_WITH_VALUE:
                skip_next = True
            continue
        return token in {".", "./", "./...", "..."}
    return True


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
    return operands[:-1]


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


def _as_dict(value: object) -> dict[str, Any]:
    """Narrow an untyped mapping lookup to a dict (ty cannot narrow call results)."""
    return value if isinstance(value, dict) else {}


def mimo_manifest_sha(task_dir: Path) -> str:
    """Recompute a converted task directory's sha256 exactly as the adapter manifest describes.

    h.update(relpath + NUL + file_bytes + NUL) for every file under the task
    dir, sorted by relative posix path. Ported from the MiMo audit prototype
    so catalog builds verify per-task bytes against manifest.json.
    """
    digest = hashlib.sha256()
    files = sorted(
        (path for path in task_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(task_dir).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(task_dir).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _read_toml_table(task_dir: Path) -> dict:
    """Parse task.toml leniently; unparseable or missing files yield {}."""
    manifest = task_dir / "task.toml"
    try:
        return tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _mimo_agent_visible_text(task_dir: Path) -> dict[str, str]:
    """Contents the agent sees before grading: instruction, manifest, environment/."""
    visible: dict[str, str] = {}
    for relative in ("instruction.md", "task.toml"):
        path = task_dir / relative
        if path.is_file():
            try:
                visible[relative] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    environment = task_dir / "environment"
    if environment.is_dir():
        for path in sorted(p for p in environment.rglob("*") if p.is_file()):
            try:
                visible[path.relative_to(task_dir).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except (OSError, UnicodeDecodeError):
                continue
    return visible


def lint_mimo_task(
    task_dir: Path,
    *,
    expected_manifest_sha: str | None = None,
    domain: str | None = None,
) -> list[Finding]:
    """Static MiMo-relevant checks for one converted Harbor task (read-only).

    Rules mirror the MiMo audit prototype's static signals but stay per-task:
    cross-task signals (slug collisions, split groups, near-duplicates) are
    computed by the catalog builder, which reuses this Finding type.
    """
    findings: list[Finding] = []
    config = _read_toml_table(task_dir)
    manifest = task_dir / "task.toml"
    verifier = _as_dict(config.get("verifier"))
    agent = _as_dict(config.get("agent"))
    environment_cfg = _as_dict(config.get("environment"))

    solution = task_dir / "solution"
    if not solution.is_dir() or not any(solution.rglob("*")):
        findings.append(
            Finding(
                "mimo-no-oracle",
                "warning",
                str(task_dir),
                "no solution/ oracle is recorded; solvability can only come from rollouts",
            )
        )

    verifier_user = verifier.get("user") or "root"
    if verifier_user == "root":
        findings.append(
            Finding(
                "mimo-verifier-not-isolated",
                "warning",
                str(manifest),
                "the verifier runs in the agent container as root, so agent-side "
                "runtime tampering can forge the reward",
            )
        )

    network_mode = environment_cfg.get("network_mode") or ""
    if network_mode == "public":
        agent_user = agent.get("user") or "root"
        findings.append(
            Finding(
                "mimo-network-public",
                "warning",
                str(manifest),
                "the agent runs with public network access"
                + (
                    " as root, which can rewrite /etc/hosts and bypass the answer-leak blocklist"
                    if agent_user == "root"
                    else ""
                ),
            )
        )

    verifier_env = _as_dict(verifier.get("env"))
    grade_py = task_dir / "tests" / "grade.py"
    grade_text = ""
    if grade_py.is_file():
        try:
            grade_text = grade_py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            grade_text = ""
    env_blob = json.dumps(verifier_env, sort_keys=True)
    if "JUDGE" in env_blob or "judge" in grade_text.lower():
        model_default = (
            verifier_env.get("GA_JUDGE_MODEL") or verifier_env.get("WEBDEV_JUDGE_MODEL") or ""
        )
        findings.append(
            Finding(
                "mimo-paid-judge",
                "warning",
                str(grade_py if grade_py.is_file() else manifest),
                "grading depends on a paid model judge"
                + (f" (default {model_default})" if model_default else "")
                + "; judge outages bill and score as errors, never as 0"
                + (
                    "; temperature is hardcoded to 1.0 (identical pages "
                    "re-score nondeterministically)"
                    if "temperature" in grade_text and "1.0" in grade_text
                    else ""
                )
                + (
                    "; the judge sees only the first 1500 chars (QUERY_CAP) "
                    "while briefs run past 2000"
                    if "QUERY_CAP" in grade_text or "[:1500]" in grade_text
                    else ""
                ),
            )
        )

    metadata_cfg = _as_dict(config.get("metadata"))
    visible = _mimo_agent_visible_text(task_dir)
    blob = "\n".join(visible.values())
    leaks = sorted({pattern.pattern for pattern in _MIMO_LEAK_PATTERNS if pattern.search(blob)})
    crash = metadata_cfg.get("expected_crash")
    if isinstance(crash, dict):
        disclosed = " in ".join(
            str(part) for part in (crash.get("function"), crash.get("file")) if part
        )
        if disclosed:
            leaks.append(f"expected crash disclosed in agent-visible metadata: {disclosed}")
    patch_path = task_dir / "tests" / "test.patch"
    if patch_path.is_file():
        try:
            patch = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            patch = ""
        hidden_tests = sorted(set(_MIMO_HIDDEN_TEST_NAME.findall(patch)))
        instruction = visible.get("instruction.md", "")
        leaked_tests = [name for name in hidden_tests if name in instruction]
        if leaked_tests:
            leaks.append(f"hidden tests named in instruction: {', '.join(leaked_tests)}")
    if leaks:
        findings.append(
            Finding(
                "mimo-answer-leak",
                "warning",
                str(task_dir / "instruction.md"),
                f"agent-visible files reveal grading material: {'; '.join(leaks)}",
            )
        )

    healthcheck = environment_cfg.get("healthcheck")
    healthcheck_command = (
        healthcheck.get("command", "") if isinstance(healthcheck, dict) else ""
    )
    if isinstance(healthcheck_command, str) and healthcheck_command:
        findings.append(
            Finding(
                "mimo-setup-healthcheck-only",
                "warning",
                str(manifest),
                "one-time setup runs only from [environment.healthcheck]; runners "
                "that skip the healthcheck will never run setup and grade against "
                "an unprepared testbed",
            )
        )

    if expected_manifest_sha is not None and expected_manifest_sha != mimo_manifest_sha(task_dir):
        findings.append(
            Finding(
                "mimo-manifest-digest-mismatch",
                "error",
                str(task_dir),
                "task bytes do not match the adapter manifest.json digest; the "
                "snapshot differs from the pinned conversion output",
            )
        )

    source_id = metadata_cfg.get("source_id") or ""
    if (
        isinstance(source_id, str)
        and source_id
        and source_id != task_dir.name
        and source_id.lower() == task_dir.name.lower()
    ):
        findings.append(
            Finding(
                "mimo-id-case-collision",
                "warning",
                str(manifest),
                f"metadata.source_id {source_id!r} differs from the directory name "
                f"{task_dir.name!r} only by case; slug folding can collide",
            )
        )
    # --- Audit-ported exploitability rules (MimoFaultAudit, static only) ---
    agent_user = agent.get("user") or "root"
    blobs = _hidden_shell_blobs(task_dir)
    shell = "\n".join(blobs)
    tests_dir = task_dir / "tests"
    tests_texts = list(blobs)
    if tests_dir.is_dir():
        for path in sorted(p for p in tests_dir.rglob("*") if p.is_file()):
            try:
                tests_texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    tests_text = "\n".join(tests_texts)
    setup_path = task_dir / "environment" / "setup" / "setup.sh"
    try:
        setup_text = setup_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        setup_text = ""
    instruction_text = visible.get("instruction.md", "")

    if domain == "terminal":
        findings.append(
            Finding(
                "mimo-terminal-hook-planting",
                "warning",
                str(task_dir / "tests" / "test.sh"),
                "the tests/test.sh guard only inspects files under /app; the agent "
                "runs as root, so interpreter hooks planted outside /app "
                "(sitecustomize.py, *.pth in site-packages, the pytest install "
                "itself) execute inside grading and can forge the reward",
            )
        )

    go_invocations = _GO_TEST_LINE.findall(shell)
    if domain == "code" and go_invocations:
        package_wide = any(_go_package_wide(inv) for inv in go_invocations)
        findings.append(
            Finding(
                "mimo-testmain-plantable",
                "warning",
                str(task_dir / "tests" / "test.patch"),
                "the hidden test command runs `go test` while test.sh resets only "
                "patch-touched files, so an agent-planted TestMain (or helper) in "
                "the tested package survives and exits 0"
                + (
                    "; package-wide invocation needs only a same-package helper file"
                    if package_wide
                    else ""
                ),
            )
        )
    if domain == "code" and _PYTEST_TOKEN.search(shell):
        findings.append(
            Finding(
                "mimo-conftest-plantable",
                "warning",
                str(task_dir / "tests" / "test.patch"),
                "the hidden test command invokes pytest; a planted conftest.py is "
                "untracked, survives the reset, and is auto-imported",
            )
        )
    if domain == "code" and agent_user == "root" and "git-hidden" in setup_text:
        findings.append(
            Finding(
                "mimo-git-history-readable",
                "warning",
                str(setup_path),
                "setup hides .git under /var/lib/mimo (mode 700) but the agent is "
                "root and can read it directly; fix exposure is image-dependent "
                "(safe only when history is truncated at the base commit)",
            )
        )
    if domain == "cyber" and (
        "/root/binary" in tests_text or "ROOT_BINARY" in tests_text
    ):
        findings.append(
            Finding(
                "mimo-cyber-binary-unchecksummed",
                "warning",
                str(task_dir / "tests" / "verify.py"),
                "verify.py re-runs the PoC from /root/binary without checksumming "
                "it; a root-time agent can replace the binary/run.sh with a "
                "crash-forging stub",
            )
        )

    network_dep: str | None = None
    test_sh = task_dir / "tests" / "test.sh"
    try:
        test_sh_text = test_sh.read_text(encoding="utf-8", errors="replace")
    except OSError:
        test_sh_text = ""
    if domain == "music" and "apt-get install" in test_sh_text:
        network_dep = (
            "the scorer dependency (abcmidi) is apt-installed at verify time; "
            "grading needs network and fails as not-scored without it"
        )
    elif domain == "webdev" and "cdn.jsdelivr" in instruction_text:
        network_dep = (
            "delivered pages require CDN assets at render time; a CDN hiccup at "
            "grade time scores a perfect offline page as not-scored"
        )
    elif domain == "code" and (
        "apt-get install" in test_sh_text or "apt-get install" in shell
    ):
        network_dep = (
            "apt-get runs inside the grading path; a testbed without network "
            "fails the task as not-scored"
        )
    elif domain == "code" and _NETWORK_INSTALL.search(tests_text):
        network_dep = (
            "test content fetches at grade time (pip/npm/go install or curl); "
            "hidden tests can reach the network during grading"
        )
    if network_dep is not None:
        findings.append(
            Finding(
                "mimo-verify-network-dep",
                "warning",
                str(test_sh),
                network_dep,
            )
        )
    return findings


# Native package metadata and security checks.
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
FLOATING_REFS = {"head", "latest", "main", "master", "trunk", "tip"}
FORBIDDEN_AGENT_IMAGE_PARTS = {"solution", "tests", "verifier", "workbench"}
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

# Harbor 0.21.0 package identity pattern (harbor.constants.ORG_NAME_PATTERN).
# Reproduced here so the workbench can fail-closed on invalid package names
# without importing harbor at runtime.
HARBOR_PACKAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _is_valid_harbor_package_name(name: str) -> bool:
    return bool(HARBOR_PACKAGE_NAME_PATTERN.fullmatch(name)) and ".." not in name


BUILD_NETWORK_PATTERN = re.compile(
    r"(?:https?://|ftp://|git://|ssh://|git@|\bcurl\b|\bwget\b|"
    r"\b(?:git|hg|svn)\s+(?:clone|fetch|pull|checkout)\b|"
    r"\bapt(?:-get)?\s+(?:update|install|upgrade|dist-upgrade)\b|"
    r"\b(?:apk|dnf|yum|microdnf|zypper)\s+(?:add|install|update|upgrade)\b|"
    r"\bpacman\s+(?:-[A-Za-z]*S|--sync)|"
    r"\b(?:brew|conda|mamba|micromamba)\s+(?:install|create|add|update|env)\b|"
    r"\bpip(?:3)?\s+(?:install|download|wheel)\b|"
    r"\buv\s+(?:pip\s+)?(?:install|sync|add|lock)\b|\buvx\b|"
    r"\b(?:poetry|pipenv|bundle|composer)\s+(?:install|add|update|require|lock|sync)\b|"
    r"\b(?:npm|pnpm)\s+(?:install|i|ci|add|update|up)\b|\bnpx\b|"
    r"\byarn\s+(?:install|add|ci|dlx|up|upgrade)\b|"
    r"\bgem\s+(?:install|update)\b|\bcargo\s+(?:install|add|fetch|update)\b|"
    r"\bgo\s+(?:get|install|mod\s+(?:download|tidy))\b|"
    r"\bdotnet\s+(?:restore|add|tool|build|publish|test)\b|"
    r"\b(?:mvn|mvnw|gradlew?)\b|"
    r"\bInvoke-(?:WebRequest|RestMethod)\b)",
    re.IGNORECASE,
)
NETWORK_OVERLAY_CONTENT = (
    b"services:\n  main:\n    build:\n      network: none\n    network_mode: none\n"
)


def _network_overlay_content(
    sidecar_name: Sequence[str] | str | None = None,
    volume: Mapping[str, Any] | None = None,
    network_name: str | None = None,
) -> bytes:
    if sidecar_name is None:
        if volume is not None:
            raise WorkbenchError("volume declared without sidecar service")
        if network_name is not None:
            raise WorkbenchError("network declared without sidecar service")
        return NETWORK_OVERLAY_CONTENT
    sidecar_names: list[str] = (
        [sidecar_name] if isinstance(sidecar_name, str) else [str(s) for s in sidecar_name]
    )
    if not sidecar_names:
        return NETWORK_OVERLAY_CONTENT
    for s_name in sidecar_names:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", s_name, re.IGNORECASE):
            raise WorkbenchError(f"unsafe sidecar service name in frozen candidate: {s_name!r}")
    net_name = network_name if network_name is not None else "workbench-internal"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", net_name):
        raise WorkbenchError(f"unsafe network name in frozen candidate: {net_name!r}")
    volume_name = str(volume.get("name")) if isinstance(volume, Mapping) else None
    mount_path = str(volume.get("mount_path")) if isinstance(volume, Mapping) else None
    has_volume = bool(volume_name and mount_path)
    lines = [
        "services:",
        "  main:",
        "    build:",
        "      network: none",
        "    networks:",
        f"      - {net_name}",
    ]
    if has_volume:
        lines.append("    volumes:")
        lines.append(f"      - {volume_name}:{mount_path}:ro")
    for s_name in sidecar_names:
        lines.extend(
            [
                f"  {s_name}:",
                "    build:",
                "      network: none",
                "    networks:",
                f"      - {net_name}",
            ]
        )
        if has_volume:
            lines.append("    volumes:")
            lines.append(f"      - {volume_name}:{mount_path}:rw")
    if has_volume:
        lines.extend(
            [
                "volumes:",
                f"  {volume_name}:",
            ]
        )
    lines.extend(
        [
            "networks:",
            f"  {net_name}:",
            "    internal: true",
        ]
    )
    return "\n".join(lines).encode()


def _candidate_network_overlay(candidate: Mapping[str, Any]) -> bytes:
    topology = candidate.get("compose_topology")
    if not isinstance(topology, Mapping):
        return NETWORK_OVERLAY_CONTENT
    services = topology.get("services")
    sidecars: list[str] = []
    if isinstance(services, Mapping):
        sidecars = [s for s in services if s != "main"]
    elif topology.get("sidecar_service"):
        sidecars = [str(topology["sidecar_service"])]
    volume = topology.get("volume") if isinstance(topology, Mapping) else None
    network = topology.get("network") if isinstance(topology, Mapping) else None
    network_name = network.get("name") if isinstance(network, Mapping) else None
    return _network_overlay_content(
        sidecars if sidecars else None,
        volume=volume,
        network_name=str(network_name) if sidecars and network_name is not None else None,
    )


Severity = Literal["error", "warning", "info"]
Classification = Literal["task_defect", "harness_defect", "agent_failure", "expected"]
ProvenanceZone = Literal["01-external", "02-local-evidence", "03-synthetic", "04-curated"]


class WorkbenchError(RuntimeError):
    """Safe package-check and retained-receipt refusal."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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


@dataclass(frozen=True)
class CandidateSource:
    source_uri: str
    source_ref: str
    license: str
    provenance_zone: ProvenanceZone = "03-synthetic"
    credentials: tuple[str, ...] = ()


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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


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


def _is_pinned_image_ref(value: str) -> bool:
    normalized = value.strip()
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9._/:-]+@sha256:[0-9a-f]{64}",
            normalized,
            re.IGNORECASE,
        )
        or re.fullmatch(
            r"[A-Za-z0-9._/-]+:v?\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9.-]+)?",
            normalized,
        )
    )


def _is_tb4_source(source: CandidateSource | None, config: Mapping[str, Any]) -> bool:
    if source is not None:
        src_text = f"{source.source_uri} {source.source_ref}".lower()
        if "terminal-bench" in src_text or "tb4" in src_text:
            return True
    task = config.get("task")
    if isinstance(task, Mapping):
        name = str(task.get("name", "")).lower()
        if "terminal-bench" in name or "tb4" in name:
            return True
    meta = config.get("metadata")
    if isinstance(meta, Mapping):
        fam = str(meta.get("task_family", "")).lower()
        if "terminal-bench" in fam or "tb4" in fam:
            return True
    return False


def _validate_task_metadata(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> tuple[str, str | None, list[str]]:
    schema = config.get("schema_version")
    if schema is not None and not (
        isinstance(schema, (str, int, float)) and re.fullmatch(r"[12](?:\.\d+)?", str(schema))
    ):
        diagnostics.append(
            _diag("schema_version_invalid", "task.toml", "schema_version must be a 1.x string")
        )
    task = config.get("task")
    task_table = task if isinstance(task, dict) else {}
    raw_name = task_table.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else task_dir.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        diagnostics.append(_diag("task_name_missing", "task.toml", "[task].name is required"))
    elif not _is_valid_harbor_package_name(name):
        diagnostics.append(
            _diag(
                "task_name_invalid",
                "task.toml",
                f"Package name must be in 'org/name' format with alphanumeric characters, "
                f"hyphens, underscores, and dots. Cannot start with a dot or contain '..'. "
                f"Got: {name}",
            )
        )
    raw_version = task_table.get("version")
    version: str | None = None
    if raw_version is not None:
        if isinstance(raw_version, str) and raw_version.strip():
            version = raw_version.strip()
        else:
            diagnostics.append(
                _diag(
                    "task_version_invalid", "task.toml", "[task].version must be a non-empty string"
                )
            )
    description = task_table.get("description")
    if description is not None and not isinstance(description, str):
        diagnostics.append(
            _diag("task_description_missing", "task.toml", "[task].description must be a string")
        )
    keywords = task_table.get("keywords")
    normalized_keywords: list[str] = []
    if keywords is not None:
        if isinstance(keywords, list):
            normalized_keywords = [
                item for item in keywords if isinstance(item, str) and item.strip()
            ]
        if not 3 <= len(normalized_keywords) <= 8 or len(normalized_keywords) != len(
            set(normalized_keywords)
        ):
            diagnostics.append(
                _diag(
                    "task_keywords_invalid",
                    "task.toml",
                    "[task].keywords must contain 3-8 unique non-empty strings",
                )
            )
    authors = task_table.get("authors")
    metadata = config.get("metadata")
    metadata_table = metadata if isinstance(metadata, dict) else {}
    if (not isinstance(authors, list) or not authors) and not metadata_table.get("author_name"):
        diagnostics.append(
            _diag("task_authors_missing", "task.toml", "[task].authors must name an author")
        )
    for key in ("category", "tags"):
        item = metadata_table.get(key)
        if item is None or item == "" or item == []:
            diagnostics.append(
                _diag("metadata_incomplete", "task.toml", f"[metadata].{key} is required")
            )
    if not metadata_table.get("difficulty") and not metadata_table.get(
        "expert_time_estimate_hours"
    ):
        diagnostics.append(
            _diag("metadata_incomplete", "task.toml", "[metadata].difficulty is required")
        )
    return name, version, normalized_keywords


def _validate_timeouts_and_artifacts(
    config: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: CandidateSource | None = None,
) -> list[str]:
    is_tb4 = _is_tb4_source(source, config)
    max_timeout = 28_800 if is_tb4 else 21_600
    for section in ("agent", "verifier"):
        raw = config.get(section)
        table = raw if isinstance(raw, dict) else {}
        timeout = table.get("timeout_sec")
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= max_timeout
        ):
            diagnostics.append(
                _diag(
                    "timeout_invalid",
                    "task.toml",
                    f"[{section}].timeout_sec must be between 1 and {max_timeout} seconds"
                    + (" (TB4 v4.0.0 official agent.timeout_sec=28800)" if is_tb4 else ""),
                )
            )
    raw_artifacts = config.get("artifacts")
    if not isinstance(raw_artifacts, list):
        diagnostics.append(
            _diag("artifacts_invalid", "task.toml", "artifacts must be an explicit list")
        )
        return []
    artifacts: list[str] = []
    for item in raw_artifacts:
        if not isinstance(item, str):
            diagnostics.append(
                _diag("artifact_path_invalid", "task.toml", "artifact paths must be strings")
            )
            continue
        pure = PurePosixPath(item)
        if not pure.is_absolute() or ".." in pure.parts or not item.startswith("/app/"):
            diagnostics.append(
                _diag(
                    "artifact_path_escape",
                    "task.toml",
                    f"artifact path {item!r} must be absolute under /app",
                )
            )
            continue
        artifacts.append(item)
    return artifacts


def _docker_logical_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\\\n", " ").splitlines() if line.strip()]


def _is_proven_offline_install(line: str) -> bool:
    """Accept only exact package-manager commands that cannot contact a registry."""
    normalized = line.strip()
    if re.fullmatch(r"(?:RUN\s+)?uv\s+sync(?:\s+--[A-Za-z0-9=._/-]+)+", normalized):
        tokens = normalized.split()
        return "--offline" in tokens and "--frozen" in tokens
    if not re.fullmatch(
        r"(?:RUN\s+)?(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install"
        r"(?:\s+--[A-Za-z0-9=._/-]+|\s+-r\s+\S+)+",
        normalized,
    ):
        return False
    tokens = shlex.split(normalized)
    if tokens and tokens[0] == "RUN":
        tokens = tokens[1:]
    try:
        requirement = tokens[tokens.index("-r") + 1]
    except (ValueError, IndexError):
        return False
    requirement_path = PurePosixPath(requirement)
    local_requirement = bool(
        re.fullmatch(r"/?[A-Za-z0-9._/-]+", requirement)
        and ".." not in requirement_path.parts
        and "//" not in requirement
        and not urllib.parse.urlsplit(requirement).scheme
    )
    return "--no-index" in tokens and "--require-hashes" in tokens and local_requirement


def _validate_build_network(
    text: str, relative: str, diagnostics: list[Diagnostic], has_proof: bool = False
) -> None:
    for line in _docker_logical_lines(text):
        if line.startswith("#") or re.match(r"(?i)^FROM\s+", line):
            continue
        if BUILD_NETWORK_PATTERN.search(line):
            if has_proof and _is_proven_offline_install(line):
                continue
            diagnostics.append(
                _diag(
                    "build_network_use",
                    relative,
                    "Docker builds may not fetch from a network or invoke an online package "
                    "manager; reviewed proof permits only exact offline frozen installs",
                )
            )
            return


# Both images Harbor builds from the candidate, and the human-readable name each
# refusal should use. `environment/` builds the agent image; `tests/` is what
# Harbor's `_verifier_env_build_context` hands the separate verifier image.
_BUILD_CONTEXTS: tuple[tuple[str, str], ...] = (
    ("environment", "the agent image"),
    ("tests", "the separate verifier image"),
)


# --- The build-context filenames this workbench version claims to understand --
_COMPOSE_CONFIG_NAME = re.compile(r"^(?:docker-)?compose\b.*\.(?:ya?ml|json)$", re.IGNORECASE)
_COMPOSE_ENV_CONFIG_NAME = re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE)
_BUILDER_CONFIG_NAMES = frozenset({".dockerignore"})


def _unmodelled_build_config(name: str, context: str = "environment") -> tuple[str, str] | None:
    """Classify a build-context filename read as configuration but not modelled."""
    if _COMPOSE_CONFIG_NAME.match(name):
        if context == "environment" and name in {"docker-compose.yaml", "docker-compose.yml"}:
            return None
        return (
            "custom_compose_unsupported",
            "Harbor layers a Compose file from an environment directory into both "
            "`docker compose build` and `docker compose up`, and excludes any service "
            "that declares its own network_mode or networks from the egress control "
            "that implements no-network; arbitrary task-authored Compose files are "
            "refused",
        )
    if _COMPOSE_ENV_CONFIG_NAME.match(name):
        return (
            "compose_env_file_unsupported",
            "Harbor runs `docker compose --project-directory` on this directory, so "
            "Compose reads this file and interpolates it into every Compose document "
            "including its own; v1/v2 does not model task-supplied Compose variables",
        )
    if name in _BUILDER_CONFIG_NAMES:
        return (
            "build_context_ignore_unsupported",
            "Docker's builder reads this file to drop paths from the build context, so "
            "the files the workbench scanned would not be the files in the image; v1/v2 "
            "certifies a context exactly as it stands",
        )
    if name in {"build-proof.json", "offline-build-proof.json"}:
        return None
    return None


def _is_exact_dependency_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized or normalized.lower() in FLOATING_REFS:
        return False
    return bool(
        re.fullmatch(
            r"(?:v?[0-9][A-Za-z0-9._+-]*|[0-9a-f]{40,64}|sha256:[0-9a-f]{64}|"
            r"[A-Za-z0-9._+-]+@sha256:[0-9a-f]{64})",
            normalized,
        )
    )


def _is_sha256_hex(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    raw = val.lower().removeprefix("sha256:")
    return bool(re.fullmatch(r"[0-9a-f]{64}", raw))


def _derive_all_build_contexts(
    task_dir: Path, compose_topology: Mapping[str, Any] | None, diagnostics: list[Diagnostic]
) -> tuple[tuple[str, str], ...]:
    contexts = list(_BUILD_CONTEXTS)
    seen_paths: dict[str, str] = {
        "environment": "the agent image",
        "tests": "the separate verifier image",
    }
    if compose_topology and isinstance(compose_topology.get("services"), Mapping):
        for s_name, s_info in compose_topology["services"].items():
            if not isinstance(s_info, Mapping):
                continue
            ctx_rel = s_info.get("build_context")
            if not ctx_rel or not isinstance(ctx_rel, str) or ctx_rel in ("environment", "."):
                continue
            clean_rel = ctx_rel.strip("/")
            resolved = (task_dir / clean_rel).resolve()
            if not _is_under(resolved, task_dir):
                diagnostics.append(
                    _diag(
                        "compose_build_path_escape",
                        "environment/docker-compose.yaml",
                        f"service {s_name!r} build context escapes task directory: {clean_rel}",
                    )
                )
                continue
            if resolved.is_symlink():
                diagnostics.append(
                    _diag(
                        "compose_build_path_symlink",
                        "environment/docker-compose.yaml",
                        f"service {s_name!r} build context is a symlink: {clean_rel}",
                    )
                )
                continue
            if clean_rel not in seen_paths:
                seen_paths[clean_rel] = f"the sidecar service {s_name}"
                contexts.append((clean_rel, f"the sidecar service {s_name}"))
    return tuple(contexts)


def _validate_offline_build_proofs(
    task_dir: Path, diagnostics: list[Diagnostic], compose_topology: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    proofs: dict[str, dict[str, Any]] = {}
    all_contexts = _derive_all_build_contexts(task_dir, compose_topology, diagnostics)
    for context, _image in all_contexts:
        root = task_dir / context
        if not root.is_dir() or root.is_symlink():
            continue
        for proof_name in ("build-proof.json", "offline-build-proof.json"):
            proof_path = root / proof_name
            if not proof_path.is_file() or proof_path.is_symlink():
                continue
            rel_proof = proof_path.relative_to(task_dir).as_posix()
            try:
                data = json.loads(proof_path.read_text(encoding="utf-8"))
            except Exception as exc:
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, f"build proof is not valid JSON: {exc}")
                )
                continue
            if not isinstance(data, Mapping):
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof must be a JSON object")
                )
                continue

            # Check if canonical MCP substrate proof
            if "substrate_version" in data or data.get("mode") in (
                "complete_offline_package",
                "plan_only",
            ):
                mode = data.get("mode")
                if mode == "plan_only":
                    if (root / "Dockerfile").exists():
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "plan-only proof cannot accompany an active Dockerfile build context",
                            )
                        )
                        continue
                    proofs[context] = {
                        "context": context,
                        "proof_path": rel_proof,
                        "mode": "plan_only",
                        "substrate_version": data.get("substrate_version"),
                        "proof_digest": _sha256_file(proof_path),
                    }
                    continue
                if mode != "complete_offline_package":
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"unknown substrate proof mode: {mode!r}",
                        )
                    )
                    continue

                # 0. Independent fail-closed verification: regenerate the canonical
                # server.py / Dockerfile / requirements lock from the checked-in
                # trusted manifest and the proof's declared tool definitions,
                # server params, runtime assets, and base image; byte-compare against
                # on-disk artifacts; verify every wheel EXACTLY against the manifest.
                try:
                    import evallab.mcp_substrate as _substrate

                    _independent_errors = _substrate.verify_proof_independently(root, data)
                except Exception as _exc:  # noqa: BLE001 - fail closed
                    _independent_errors = [
                        f"independent substrate proof verification failed: {_exc}"
                    ]
                if _independent_errors:
                    for _msg in _independent_errors:
                        diagnostics.append(_diag("build_proof_invalid", rel_proof, _msg))
                    continue

                # 1. Mandatory requirements.txt matching requirements_sha256
                req_path = root / "requirements.txt"
                if not req_path.is_file() or req_path.is_symlink():
                    diagnostics.append(
                        _diag(
                            "build_proof_lockfile_missing",
                            rel_proof,
                            "requirements.txt missing or symlink for substrate build proof",
                        )
                    )
                    continue
                declared_req_digest = data.get("requirements_sha256")
                if not _is_sha256_hex(declared_req_digest):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires valid sha256 'requirements_sha256'",
                        )
                    )
                    continue
                actual_req_digest = hashlib.sha256(req_path.read_bytes()).hexdigest()
                if actual_req_digest != declared_req_digest.lower().removeprefix("sha256:"):
                    diagnostics.append(
                        _diag(
                            "build_proof_lockfile_mismatch",
                            rel_proof,
                            f"requirements.txt digest {actual_req_digest} does not match proof {declared_req_digest}",
                        )
                    )
                    continue

                # 2. Mandatory Dockerfile matching dockerfile_sha256
                df_path = root / "Dockerfile"
                if not df_path.is_file() or df_path.is_symlink():
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "Dockerfile missing or symlink for substrate build proof",
                        )
                    )
                    continue
                declared_df_digest = data.get("dockerfile_sha256")
                if not _is_sha256_hex(declared_df_digest):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires valid sha256 'dockerfile_sha256'",
                        )
                    )
                    continue
                df_bytes = df_path.read_bytes()
                actual_df_digest = hashlib.sha256(df_bytes).hexdigest()
                if actual_df_digest != declared_df_digest.lower().removeprefix("sha256:"):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"Dockerfile digest {actual_df_digest} does not match proof {declared_df_digest}",
                        )
                    )
                    continue

                # 2b. Mandatory server.py matching server_sha256/server_size_bytes (exact regular bytes)
                server_path = root / "server.py"
                if not server_path.is_file() or server_path.is_symlink():
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "server.py missing or symlink for substrate build proof",
                        )
                    )
                    continue
                declared_server_sha = data.get("server_sha256")
                declared_server_size = data.get("server_size_bytes")
                if not _is_sha256_hex(declared_server_sha):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires valid sha256 'server_sha256'",
                        )
                    )
                    continue
                if not isinstance(declared_server_size, int) or declared_server_size <= 0:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires positive int 'server_size_bytes'",
                        )
                    )
                    continue
                server_bytes = server_path.read_bytes()
                if len(server_bytes) != declared_server_size:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"server.py size {len(server_bytes)} does not match proof {declared_server_size}",
                        )
                    )
                    continue
                actual_server_sha = hashlib.sha256(server_bytes).hexdigest()
                if actual_server_sha != declared_server_sha.lower().removeprefix("sha256:"):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"server.py digest {actual_server_sha} does not match proof {declared_server_sha}",
                        )
                    )
                    continue

                # 2c. Mandatory event schema version
                declared_event_schema = data.get("event_schema_version")
                if declared_event_schema != "mcp-tool-event-v1":
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires event_schema_version 'mcp-tool-event-v1'",
                        )
                    )
                    continue

                # 2d. Mandatory canonical tool_definitions_sha256
                declared_tool_defs_sha = data.get("tool_definitions_sha256")
                if not _is_sha256_hex(declared_tool_defs_sha):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate proof requires valid sha256 'tool_definitions_sha256'",
                        )
                    )
                    continue

                # 2e. Mandatory trusted manifest digest/source matching the checked-in manifest
                try:
                    import evallab.mcp_substrate as _substrate

                    _expected_manifest_digest = _substrate.trusted_wheel_manifest_digest()
                    _expected_manifest_source = _substrate.trusted_wheel_manifest_source()
                except Exception:
                    _expected_manifest_digest = None
                    _expected_manifest_source = None
                declared_manifest_digest = data.get("trusted_manifest_digest")
                declared_manifest_source = data.get("trusted_manifest_source")
                if _expected_manifest_digest is not None:
                    if declared_manifest_digest != _expected_manifest_digest:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "trusted_manifest_digest does not match checked-in trusted wheel manifest",
                            )
                        )
                        continue
                    if declared_manifest_source != _expected_manifest_source:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "trusted_manifest_source does not match checked-in trusted wheel manifest",
                            )
                        )
                        continue
                else:
                    if (
                        not _is_sha256_hex(declared_manifest_digest)
                        or not isinstance(declared_manifest_source, str)
                        or not declared_manifest_source
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "substrate proof requires valid trusted_manifest_digest/source",
                            )
                        )
                        continue

                # Parse all Dockerfile instructions; reject ADD, flags, and multi-source COPY
                df_text = df_bytes.decode("utf-8", errors="replace")
                dockerfile_copies: list[tuple[str, str]] = []
                has_df_err = False
                for line in _docker_logical_lines(df_text):
                    clean = line.strip()
                    if clean.startswith("#"):
                        continue
                    if re.match(r"(?i)^ADD\b", clean):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"ADD instructions are forbidden in canonical sidecar Dockerfile: {clean!r}",
                            )
                        )
                        has_df_err = True
                        break
                    if re.match(r"(?i)^COPY\b", clean):
                        # Reject flags (--from, --chown, etc)
                        if re.search(r"--[a-z0-9_-]+=", clean):
                            diagnostics.append(
                                _diag(
                                    "build_proof_invalid",
                                    rel_proof,
                                    f"COPY flags are forbidden in canonical sidecar Dockerfile: {clean!r}",
                                )
                            )
                            has_df_err = True
                            break
                        # Handle JSON array form COPY ["src", "dst"] vs shell form COPY src dst
                        if clean[4:].strip().startswith("["):
                            try:
                                json_arr = json.loads(clean[4:].strip())
                                if (
                                    not isinstance(json_arr, list)
                                    or len(json_arr) != 2
                                    or not all(isinstance(x, str) for x in json_arr)
                                ):
                                    diagnostics.append(
                                        _diag(
                                            "build_proof_invalid",
                                            rel_proof,
                                            f"JSON COPY must contain exactly 2 string arguments: {clean!r}",
                                        )
                                    )
                                    has_df_err = True
                                    break
                                src_token, dst_token = json_arr[0], json_arr[1]
                            except Exception:
                                diagnostics.append(
                                    _diag(
                                        "build_proof_invalid",
                                        rel_proof,
                                        f"malformed JSON COPY instruction: {clean!r}",
                                    )
                                )
                                has_df_err = True
                                break
                        else:
                            tokens = clean.split()[1:]
                            if len(tokens) != 2:
                                diagnostics.append(
                                    _diag(
                                        "build_proof_invalid",
                                        rel_proof,
                                        f"only exact 2-token COPY instructions are supported in canonical sidecar Dockerfile: {clean!r}",
                                    )
                                )
                                has_df_err = True
                                break
                            src_token, dst_token = tokens[0], tokens[1]
                        dockerfile_copies.append((src_token, dst_token))

                if has_df_err:
                    continue

                # Verify canonical base copies
                canonical_base_copies = [
                    ("wheelhouse", "/wheelhouse"),
                    ("requirements.txt", "/app/requirements.txt"),
                    ("server.py", "/app/server.py"),
                ]
                if dockerfile_copies[:3] != canonical_base_copies:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"Dockerfile must begin with canonical base COPY instructions, got {dockerfile_copies[:3]}",
                        )
                    )
                    continue

                asset_copies = dockerfile_copies[3:]
                copy_sources = [src for src, _dst in asset_copies]
                if len(copy_sources) != len(set(copy_sources)):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "Dockerfile contains duplicate asset COPY sources",
                        )
                    )
                    continue

                copy_dests = [dst for _src, dst in asset_copies]
                if len(copy_dests) != len(set(copy_dests)):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "Dockerfile contains duplicate asset COPY destinations",
                        )
                    )
                    continue

                # 3. Mandatory runtime_assets verification and 1:1 mapping with Dockerfile COPY
                has_asset_err = False
                declared_asset_paths = set()
                declared_asset_fold = set()
                if "runtime_assets" not in data:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate build proof requires mandatory 'runtime_assets' list",
                        )
                    )
                    continue
                raw_assets = data["runtime_assets"]
                if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes)):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "runtime_assets in substrate proof must be a list",
                        )
                    )
                    continue

                for a in raw_assets:
                    if (
                        not isinstance(a, Mapping)
                        or not isinstance(a.get("path"), str)
                        or not isinstance(a.get("sha256"), str)
                        or not isinstance(a.get("size_bytes"), int)
                        or not _is_sha256_hex(a["sha256"])
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "each runtime_asset entry must declare valid path (str), sha256 (hex), and size_bytes (int)",
                            )
                        )
                        has_asset_err = True
                        break
                    a_rel = a["path"]
                    # NFC normalization check
                    if unicodedata.normalize("NFC", a_rel) != a_rel:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset path {a_rel!r} must be Unicode NFC normalized",
                            )
                        )
                        has_asset_err = True
                        break
                    if (
                        not a_rel
                        or a_rel != PurePosixPath(a_rel).as_posix()
                        or "//" in a_rel
                        or "\\" in a_rel
                        or a_rel.startswith("/")
                        or a_rel.endswith("/")
                        or any(part in (".", "..") for part in PurePosixPath(a_rel).parts)
                        or any(ord(c) < 32 or ord(c) == 127 for c in a_rel)
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset path {a_rel!r} is not a normalized confined POSIX relative path",
                            )
                        )
                        has_asset_err = True
                        break
                    a_fold = a_rel.casefold()
                    a_first = a_fold.split("/", 1)[0]
                    if (
                        a_fold
                        in {
                            ".dockerignore",
                            "compose.yaml",
                            "compose.yml",
                            "docker-compose.yaml",
                            "docker-compose.yml",
                            "dockerfile",
                            "dockerfile.dockerignore",
                            "offline-build-proof.json",
                            "requirements.txt",
                            "server.py",
                            "wheelhouse",
                        }
                        or a_first
                        in {
                            ".dockerignore",
                            "compose.yaml",
                            "compose.yml",
                            "docker-compose.yaml",
                            "docker-compose.yml",
                            "dockerfile",
                            "dockerfile.dockerignore",
                            "offline-build-proof.json",
                            "requirements.txt",
                            "server.py",
                            "wheelhouse",
                        }
                        or a_fold.startswith("dockerfile.")
                        or a_first.startswith("dockerfile.")
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset path {a_rel!r} is reserved",
                            )
                        )
                        has_asset_err = True
                        break
                    if a_fold in declared_asset_fold:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"duplicate runtime asset path in proof: {a_rel!r}",
                            )
                        )
                        has_asset_err = True
                        break
                    declared_asset_fold.add(a_fold)
                    declared_asset_paths.add(a_rel)
                    a_file = root / a_rel
                    if (
                        not a_file.is_file()
                        or a_file.is_symlink()
                        or not _is_under(a_file.resolve(), root.resolve())
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset {a_rel!r} missing, symlink, or escapes root",
                            )
                        )
                        has_asset_err = True
                        break
                    a_bytes = a_file.read_bytes()
                    if len(a_bytes) != a["size_bytes"]:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset {a_rel!r} size {len(a_bytes)} does not match proof {a['size_bytes']}",
                            )
                        )
                        has_asset_err = True
                        break
                    a_hash = hashlib.sha256(a_bytes).hexdigest()
                    if a_hash != a["sha256"].lower().removeprefix("sha256:"):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"runtime asset {a_rel!r} digest {a_hash} does not match proof {a['sha256']}",
                            )
                        )
                        has_asset_err = True
                        break

                if has_asset_err:
                    continue

                # Check for prefix / ancestor collisions among runtime assets
                folded_sorted = sorted(declared_asset_fold)
                has_prefix_err = False
                for idx_f, left in enumerate(folded_sorted):
                    prefix_check = f"{left}/"
                    for right in folded_sorted[idx_f + 1 :]:
                        if right.startswith(prefix_check):
                            diagnostics.append(
                                _diag(
                                    "build_proof_invalid",
                                    rel_proof,
                                    f"runtime asset path {right!r} conflicts with ancestor prefix {left!r}",
                                )
                            )
                            has_prefix_err = True
                            break
                    if has_prefix_err:
                        break
                if has_prefix_err:
                    continue

                expected_asset_copies = [
                    (a["path"], f"/app/{a['path']}")
                    for a in sorted(raw_assets, key=lambda x: x["path"])
                ]
                if asset_copies != expected_asset_copies:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "Dockerfile asset COPY lines do not match sorted proof runtime_assets exactly",
                        )
                    )
                    continue

                # 4. Mandatory wheels verification strictly under root/wheelhouse/<basename>
                if (
                    "wheels" not in data
                    or not isinstance(data["wheels"], Sequence)
                    or not data["wheels"]
                ):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "substrate build proof requires non-empty 'wheels' list",
                        )
                    )
                    continue
                wheels = data["wheels"]

                if (
                    "wheel_count" not in data
                    or not isinstance(data["wheel_count"], int)
                    or data["wheel_count"] != len(wheels)
                ):
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"mandatory integer wheel_count missing or does not match wheels list length {len(wheels)}",
                        )
                    )
                    continue

                wheelhouse_dir = root / "wheelhouse"
                if not wheelhouse_dir.is_dir() or wheelhouse_dir.is_symlink():
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            "wheelhouse directory missing or symlink",
                        )
                    )
                    continue

                # Strict directory inspection: reject symlinks, directories, non-.whl files, and hidden files
                actual_wheel_files = []
                has_dir_err = False
                for p in wheelhouse_dir.iterdir():
                    if p.is_symlink():
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheelhouse contains symlink entry {p.name!r}",
                            )
                        )
                        has_dir_err = True
                        break
                    if not p.is_file():
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheelhouse contains non-file directory {p.name!r}",
                            )
                        )
                        has_dir_err = True
                        break
                    if not p.name.endswith(".whl") or p.name.startswith("."):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheelhouse contains non-wheel or hidden file {p.name!r}",
                            )
                        )
                        has_dir_err = True
                        break
                    actual_wheel_files.append(p.name)

                if has_dir_err:
                    continue

                if len(actual_wheel_files) != data["wheel_count"]:
                    diagnostics.append(
                        _diag(
                            "build_proof_invalid",
                            rel_proof,
                            f"actual regular wheel count {len(actual_wheel_files)} does not match proof wheel_count {data['wheel_count']}",
                        )
                    )
                    continue

                pinned_deps = []
                has_wheel_err = False
                declared_filenames = set()
                seen_fold = set()
                for w in wheels:
                    if (
                        not isinstance(w, Mapping)
                        or not isinstance(w.get("filename"), str)
                        or not isinstance(w.get("sha256"), str)
                        or not isinstance(w.get("size_bytes"), int)
                        or not _is_sha256_hex(w["sha256"])
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "each wheel entry must declare valid filename (str), sha256 (hex), and size_bytes (int)",
                            )
                        )
                        has_wheel_err = True
                        break
                    w_name = w["filename"]
                    if (
                        Path(w_name).name != w_name
                        or not w_name.endswith(".whl")
                        or "/" in w_name
                        or "\\" in w_name
                        or any(ord(c) < 32 or ord(c) == 127 for c in w_name)
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheel filename {w_name!r} must be a normalized plain .whl basename",
                            )
                        )
                        has_wheel_err = True
                        break
                    w_fold = w_name.casefold()
                    if w_fold in seen_fold:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"duplicate wheel filename in proof: {w_name!r}",
                            )
                        )
                        has_wheel_err = True
                        break
                    seen_fold.add(w_fold)
                    declared_filenames.add(w_name)
                    w_path = wheelhouse_dir / w_name
                    if (
                        not w_path.is_file()
                        or w_path.is_symlink()
                        or not _is_under(w_path.resolve(), wheelhouse_dir.resolve())
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheel {w_name!r} missing, symlink, or escapes wheelhouse",
                            )
                        )
                        has_wheel_err = True
                        break
                    w_bytes = w_path.read_bytes()
                    if len(w_bytes) != w["size_bytes"]:
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheel {w_name!r} size {len(w_bytes)} does not match proof {w['size_bytes']}",
                            )
                        )
                        has_wheel_err = True
                        break
                    w_hash = hashlib.sha256(w_bytes).hexdigest()
                    if w_hash != w["sha256"].lower().removeprefix("sha256:"):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                f"wheel {w_name!r} digest {w_hash} does not match proof {w['sha256']}",
                            )
                        )
                        has_wheel_err = True
                        break
                    pinned_deps.append(
                        {
                            "name": w.get("name") or w_name.split("-")[0],
                            "version": w.get("version") or "pinned",
                            "sha256": w["sha256"],
                            "wheel": w_name,
                        }
                    )
                if has_wheel_err:
                    continue

                # Exact inventory check
                extra_wheels = set(actual_wheel_files) - declared_filenames
                if extra_wheels:
                    diagnostics.append(
                        _diag(
                            "build_proof_unpinned_dependency",
                            rel_proof,
                            f"extra unapproved wheels in wheelhouse not in proof: {sorted(extra_wheels)}",
                        )
                    )
                    continue

                proofs[context] = {
                    "context": context,
                    "proof_path": rel_proof,
                    "lockfile": "requirements.txt",
                    "lockfile_digest": actual_req_digest,
                    "ecosystem": "pypi",
                    "reviewed_by": "eval-lab-substrate",
                    "pinned_dependencies": pinned_deps,
                    "pinned_dependencies_count": len(pinned_deps),
                    "runtime_asset_paths": tuple(sorted(declared_asset_paths)),
                    "proof_digest": _sha256_file(proof_path),
                }
                continue

            # Legacy / Tau2 offline build proof
            if data.get("kind") != "offline_build_proof":
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof kind must be 'offline_build_proof'",
                    )
                )
                continue
            ecosystem = data.get("ecosystem")
            if not isinstance(ecosystem, str) or not ecosystem.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'ecosystem'")
                )
                continue
            lockfile_rel = data.get("lockfile")
            if not isinstance(lockfile_rel, str) or not lockfile_rel.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'lockfile'")
                )
                continue
            lockfile_path = root / lockfile_rel
            if not lockfile_path.is_file() or not _is_under(lockfile_path, root):
                diagnostics.append(
                    _diag(
                        "build_proof_lockfile_missing",
                        rel_proof,
                        f"declared lockfile {lockfile_rel!r} does not exist in build context",
                    )
                )
                continue
            declared_lock_digest = data.get("lockfile_digest")
            if not isinstance(declared_lock_digest, str) or not SHA256_PATTERN.match(
                declared_lock_digest
            ):
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof requires valid sha256 'lockfile_digest'",
                    )
                )
                continue
            actual_lock_digest = _sha256_file(lockfile_path)
            if actual_lock_digest != declared_lock_digest:
                diagnostics.append(
                    _diag(
                        "build_proof_lockfile_mismatch",
                        rel_proof,
                        f"lockfile digest {actual_lock_digest} does not match declared {declared_lock_digest}",
                    )
                )
                continue
            pinned_deps = data.get("pinned_dependencies")
            if not isinstance(pinned_deps, (Mapping, Sequence)) or not pinned_deps:
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof requires non-empty 'pinned_dependencies'",
                    )
                )
                continue
            has_unpinned = False
            if isinstance(pinned_deps, Mapping):
                for dep_name, dep_ver in pinned_deps.items():
                    if (
                        not isinstance(dep_name, str)
                        or not dep_name.strip()
                        or not _is_exact_dependency_version(dep_ver)
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_unpinned_dependency",
                                rel_proof,
                                f"dependency {dep_name!r} has floating/unpinned reference {dep_ver!r}",
                            )
                        )
                        has_unpinned = True
                        break
            elif isinstance(pinned_deps, Sequence):
                for item in pinned_deps:
                    if (
                        not isinstance(item, Mapping)
                        or not item.get("name")
                        or not item.get("version")
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "each pinned dependency item must declare name and version",
                            )
                        )
                        has_unpinned = True
                        break
                    ver_str = item.get("version")
                    if not _is_exact_dependency_version(ver_str):
                        diagnostics.append(
                            _diag(
                                "build_proof_unpinned_dependency",
                                rel_proof,
                                f"dependency {item.get('name')!r} has floating/unpinned reference {ver_str!r}",
                            )
                        )
                        has_unpinned = True
                        break
            if has_unpinned:
                continue
            reviewed_by = data.get("reviewed_by")
            if not isinstance(reviewed_by, str) or not reviewed_by.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'reviewed_by'")
                )
                continue
            proofs[context] = {
                "context": context,
                "proof_path": rel_proof,
                "lockfile": lockfile_rel,
                "lockfile_digest": actual_lock_digest,
                "ecosystem": ecosystem,
                "reviewed_by": reviewed_by,
                "pinned_dependencies": pinned_deps,
                "pinned_dependencies_count": len(pinned_deps),
                "proof_digest": _sha256_file(proof_path),
            }
    return proofs


def _validate_sidecar_environment(
    service_name: str,
    service_env: Any,
    rel_path: str,
    credentials: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> None:
    """Validate sidecar environment values are simple, declared credential placeholders."""
    entries: list[tuple[str, str]]
    if isinstance(service_env, Mapping):
        entries = [(str(k), str(v)) for k, v in service_env.items()]
    elif isinstance(service_env, Sequence) and not isinstance(service_env, (str, bytes)):
        entries = []
        for item in service_env:
            if not isinstance(item, str):
                diagnostics.append(
                    _diag(
                        "compose_sidecar_env_invalid",
                        rel_path,
                        f"sidecar {service_name!r} environment entry {item!r} is not a string",
                    )
                )
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                entries.append((key, value))
            else:
                entries.append((item, ""))
    else:
        diagnostics.append(
            _diag(
                "compose_sidecar_env_invalid",
                rel_path,
                f"sidecar {service_name!r} environment must be a mapping or list",
            )
        )
        return

    for key, value in entries:
        if not value:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_invalid",
                    rel_path,
                    f"sidecar {service_name!r} environment entry {key!r} has no value",
                )
            )
            continue
        match = re.fullmatch(r"^\$\{([A-Z_][A-Z0-9_]*)\}$|^\$([A-Z_][A-Z0-9_]*)$", value)
        if not match:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_invalid",
                    rel_path,
                    f"sidecar {service_name!r} environment value {key}={value!r} is not a simple ${{VAR}} or $VAR placeholder",
                )
            )
            continue
        var = match.group(1) or match.group(2)
        if var not in credentials:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_unauthorized",
                    rel_path,
                    f"sidecar {service_name!r} environment references undeclared credential {var!r}",
                )
            )


def _validate_service_volume_mounts(
    service_name: str,
    service_mounts: Any,
    volume_name: str | None,
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> list[dict[str, Any]]:
    """Validate a service volume list uses only the task-local named volume."""
    valid_mounts: list[dict[str, Any]] = []
    if not isinstance(service_mounts, Sequence) or isinstance(service_mounts, (str, bytes)):
        diagnostics.append(
            _diag(
                "compose_volume_mount_invalid",
                rel_path,
                f"service {service_name!r} volumes must be a list",
            )
        )
        return valid_mounts
    if volume_name is None and service_mounts:
        diagnostics.append(
            _diag(
                "compose_volume_mount_invalid",
                rel_path,
                f"service {service_name!r} mounts a volume but no top-level 'volumes' is declared",
            )
        )
        return valid_mounts
    for mount in service_mounts:
        if "docker.sock" in str(mount):
            diagnostics.append(
                _diag(
                    "compose_volume_escape",
                    rel_path,
                    f"service {service_name!r} volume mount {mount!r} attempts docker socket access",
                )
            )
            continue
        source, target, mode = _parse_compose_volume_mount(mount)
        if source is None:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} volume mount {mount!r} is malformed",
                )
            )
            continue
        if target is None:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} named volume mount {mount!r} is missing a target path",
                )
            )
            continue
        if (
            source.startswith("/")
            or re.match(r"^[A-Za-z]:/", source)
            or ":" in source
            or "\\" in source
        ):
            diagnostics.append(
                _diag(
                    "compose_volume_escape",
                    rel_path,
                    f"service {service_name!r} volume mount {mount!r} is a host bind or external path",
                )
            )
            continue
        if source != volume_name:
            diagnostics.append(
                _diag(
                    "compose_volume_unauthorized",
                    rel_path,
                    f"service {service_name!r} may only mount task-local volume {volume_name!r}, got {source!r}",
                )
            )
            continue
        if not _is_under_path_only(Path(target), Path("/")):
            diagnostics.append(
                _diag(
                    "compose_volume_escape",
                    rel_path,
                    f"service {service_name!r} volume target {target!r} escapes the container root",
                )
            )
            continue
        expected_mode = "ro" if service_name == "main" else "rw"
        if mode != expected_mode:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} must mount {volume_name!r} as {expected_mode}, got {mode!r}",
                )
            )
            continue
        valid_mounts.append({"source": source, "target": target, "mode": mode})
    return valid_mounts


def _parse_compose_volume_mount(mount: Any) -> tuple[str | None, str | None, str]:
    """Parse a Compose volume mount string or mapping.

    Returns (source, target, mode).  Mode defaults to 'rw'.  For malformed
    entries, returns (None, None, '') and the caller emits a diagnostic.
    """
    if isinstance(mount, str):
        parts = mount.split(":")
        if len(parts) == 1:
            # short form: target path creates anonymous volume (rejected later)
            return parts[0], parts[0], "rw"
        source = parts[0]
        target = parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        return source, target, mode
    if isinstance(mount, Mapping):
        source = mount.get("source")
        target = mount.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            return None, None, ""
        read_only = mount.get("read_only", False)
        if not isinstance(read_only, bool):
            read_only = str(read_only).lower() in {"true", "1", "yes", "ro"}
        return source, target, "ro" if read_only else "rw"
    return None, None, ""


def _is_under_path_only(path: Path, root: Path) -> bool:
    """Check that a path is absolute and stays under root (used for volume targets)."""
    try:
        return path.is_absolute() and _is_under(path, root)
    except (ValueError, OSError):
        return False


def _extract_sidecar_env(service_env: Any) -> dict[str, str]:
    """Return the sidecar environment as a {key: value} mapping."""
    result: dict[str, str] = {}
    if isinstance(service_env, Mapping):
        for k, v in service_env.items():
            result[str(k)] = str(v)
    elif isinstance(service_env, Sequence) and not isinstance(service_env, (str, bytes)):
        for item in service_env:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                result[key] = value
    return result


def _validate_compose_networks(
    data: Mapping[str, Any],
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Validate the top-level networks section, if any.

    Returns `(network_record, network_name, is_valid)`.
    A valid record contains exactly one network whose definition is only
    ``internal: true``, using a safe task-local name.
    """
    top_networks = data.get("networks")
    if top_networks is None:
        return None, None, True
    if not isinstance(top_networks, Mapping):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                "top-level 'networks' must be a mapping",
            )
        )
        return None, None, False
    if not top_networks:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                "top-level 'networks' must declare exactly one network",
            )
        )
        return None, None, False
    if len(top_networks) > 1:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"top-level 'networks' may contain at most 1 network, got {len(top_networks)}: {list(top_networks)}",
            )
        )
        return None, None, False
    net_name, net_def = next(iter(top_networks.items()))
    if not isinstance(net_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", net_name):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network name {net_name!r} is not a safe task-local name",
            )
        )
        return None, None, False
    if not isinstance(net_def, Mapping):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network {net_name!r} definition must be a mapping",
            )
        )
        return None, None, False
    for key in sorted(net_def, key=str):
        if not isinstance(key, str) or key != "internal":
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"network {net_name!r} declares unsupported key {key!r}",
                )
            )
            return None, None, False
    if net_def.get("internal") is not True:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network {net_name!r} must set internal: true",
            )
        )
        return None, None, False
    return {"name": net_name, "internal": True}, net_name, True


def _validate_service_networks(
    service_name: str,
    networks: Any,
    network_name: str,
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> None:
    """Validate a service attaches only to the one declared internal network."""
    if isinstance(networks, list):
        if len(networks) != 1 or not isinstance(networks[0], str) or networks[0] != network_name:
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"service {service_name!r} must declare exactly one network attachment to {network_name!r}",
                )
            )
        return
    diagnostics.append(
        _diag(
            "compose_networks_unsupported",
            rel_path,
            f"service {service_name!r} 'networks' must be a single-item list containing {network_name!r}",
        )
    )


def _validate_compose_topology(
    task_dir: Path,
    diagnostics: list[Diagnostic],
    credentials: tuple[str, ...] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    compose_path = task_dir / "environment/docker-compose.yaml"
    if not compose_path.is_file():
        compose_path = task_dir / "environment/docker-compose.yml"
        if not compose_path.is_file():
            return None, None
    rel_path = compose_path.relative_to(task_dir).as_posix()
    try:
        raw_text = compose_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except Exception as exc:
        diagnostics.append(
            _diag("compose_syntax_error", rel_path, f"docker-compose.yaml syntax error: {exc}")
        )
        return None, None
    if not isinstance(data, Mapping):
        diagnostics.append(
            _diag("compose_structure_invalid", rel_path, "docker-compose.yaml must be a mapping")
        )
        return None, None
    volume_definition: dict[str, Any] | None = None
    for top_key in data:
        if top_key not in {"services", "version", "volumes", "networks"}:
            diagnostics.append(
                _diag(
                    "custom_compose_unsupported",
                    rel_path,
                    f"top-level Compose key {top_key!r} is unsupported; only 'services', 'volumes', and 'networks' are modelled",
                )
            )
    services = data.get("services")
    if not isinstance(services, Mapping) or not services:
        diagnostics.append(
            _diag(
                "compose_structure_invalid", rel_path, "docker-compose.yaml must declare 'services'"
            )
        )
        return None, None
    if "main" not in services:
        diagnostics.append(
            _diag(
                "compose_main_service_missing",
                rel_path,
                "Compose topology must declare a 'main' service",
            )
        )
        return None, None
    top_volumes = data.get("volumes")
    volume_name: str | None = None
    if top_volumes is not None:
        if not isinstance(top_volumes, Mapping):
            diagnostics.append(
                _diag("compose_volume_invalid", rel_path, "top-level 'volumes' must be a mapping")
            )
            top_volumes = None
        elif top_volumes:
            if len(top_volumes) > 1:
                diagnostics.append(
                    _diag(
                        "compose_volume_invalid",
                        rel_path,
                        f"top-level 'volumes' may contain at most 1 volume, got {len(top_volumes)}: {list(top_volumes)}",
                    )
                )
            volume_name, volume_def = next(iter(top_volumes.items()))
            if not isinstance(volume_name, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]*", volume_name
            ):
                diagnostics.append(
                    _diag(
                        "compose_volume_invalid",
                        rel_path,
                        f"volume name {volume_name!r} is not a safe task-local name",
                    )
                )
                volume_name = None
            else:
                if volume_def is not None and volume_def != {}:
                    diagnostics.append(
                        _diag(
                            "compose_volume_unauthorized",
                            rel_path,
                            f"volume {volume_name!r} may only be declared with an empty or null definition",
                        )
                    )
                if volume_name:
                    volume_definition = {volume_name: None}
    network_record, network_name, networks_valid = _validate_compose_networks(
        data, rel_path, diagnostics
    )
    service_names = list(services.keys())
    sidecar_names: list[str] = []
    for name in service_names:
        if not isinstance(name, str):
            diagnostics.append(
                _diag(
                    "compose_topology_invalid",
                    rel_path,
                    f"service name {name!r} must be a string",
                )
            )
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name, re.IGNORECASE):
            diagnostics.append(
                _diag(
                    "compose_topology_invalid",
                    rel_path,
                    f"service name {name!r} is invalid",
                )
            )
        elif name != "main":
            sidecar_names.append(name)
    sidecar_name: str | None = sidecar_names[0] if sidecar_names else None
    service_summaries: dict[str, Any] = {}
    for name, s_config in services.items():
        if not isinstance(name, str):
            diagnostics.append(
                _diag(
                    "compose_structure_invalid",
                    rel_path,
                    f"service name {name!r} must be a string",
                )
            )
            continue
        if not isinstance(s_config, Mapping):
            diagnostics.append(
                _diag(
                    "compose_structure_invalid",
                    rel_path,
                    f"service {name!r} configuration must be a mapping",
                )
            )
            continue
        allowed_service_keys = {"build", "image"}
        dedicated_service_keys = {
            "cpus",
            "depends_on",
            "environment",
            "expose",
            "healthcheck",
            "ipc",
            "network_mode",
            "networks",
            "pid",
            "ports",
            "privileged",
            "pull_policy",
            "shm_size",
            "volumes",
        }
        unsupported_service_keys = sorted(
            set(s_config) - allowed_service_keys - dedicated_service_keys,
            key=str,
        )
        if unsupported_service_keys:
            diagnostics.append(
                _diag(
                    "compose_service_key_unsupported",
                    rel_path,
                    f"service {name!r} uses unsupported keys {unsupported_service_keys}; "
                    "v2 models only local build or pinned image",
                )
            )
        if "network_mode" in s_config:
            net_mode = s_config["network_mode"]
            if net_mode == "host":
                diagnostics.append(
                    _diag(
                        "custom_compose_unsupported",
                        rel_path,
                        f"service {name!r} declares forbidden host network mode {net_mode!r}",
                    )
                )
                continue
            elif not (isinstance(net_mode, str) and net_mode.startswith("service:")):
                diagnostics.append(
                    _diag(
                        "custom_compose_unsupported",
                        rel_path,
                        f"service {name!r} declares custom network_mode {net_mode!r}",
                    )
                )
                continue
        if "networks" in s_config:
            if not networks_valid:
                pass
            elif network_name is None:
                diagnostics.append(
                    _diag(
                        "compose_networks_unsupported",
                        rel_path,
                        f"service {name!r} declares networks but no top-level network is defined",
                    )
                )
            else:
                _validate_service_networks(
                    name, s_config["networks"], network_name, rel_path, diagnostics
                )
        elif networks_valid and network_name is not None:
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"service {name!r} must attach to declared network {network_name!r}",
                )
            )
        if "depends_on" in s_config:
            dep = s_config["depends_on"]
            if not isinstance(dep, (list, Mapping)):
                diagnostics.append(
                    _diag(
                        "compose_topology_invalid",
                        rel_path,
                        f"service {name!r} depends_on must be a list or mapping",
                    )
                )
        if "expose" in s_config:
            exp = s_config["expose"]
            if not isinstance(exp, list):
                diagnostics.append(
                    _diag(
                        "compose_host_ports_unsupported",
                        rel_path,
                        f"service {name!r} expose must be a list",
                    )
                )
        if "ports" in s_config and s_config["ports"]:
            diagnostics.append(
                _diag(
                    "compose_host_ports_unsupported",
                    rel_path,
                    f"service {name!r} may not publish host ports",
                )
            )
        if s_config.get("privileged") is True:
            diagnostics.append(
                _diag(
                    "compose_privileged_unsupported",
                    rel_path,
                    f"service {name!r} may not request privileged mode",
                )
            )
        if s_config.get("pid") == "host" or s_config.get("ipc") == "host":
            diagnostics.append(
                _diag(
                    "compose_privileged_unsupported",
                    rel_path,
                    f"service {name!r} may not share host PID/IPC namespace",
                )
            )

        service_env = s_config.get("environment")
        sidecar_env: dict[str, str] | None = None
        if service_env and name != "main":
            _validate_sidecar_environment(name, service_env, rel_path, credentials, diagnostics)
            sidecar_env = _extract_sidecar_env(service_env)

        service_mounts = s_config.get("volumes")
        valid_mounts: list[dict[str, Any]] = []
        if service_mounts is not None:
            valid_mounts = _validate_service_volume_mounts(
                name,
                service_mounts,
                volume_name,
                rel_path,
                diagnostics,
            )

        build_cfg = s_config.get("build")
        image_cfg = s_config.get("image")
        build_context_rel: str | None = None
        if build_cfg is not None:
            if isinstance(build_cfg, str):
                ctx_str = build_cfg
            elif isinstance(build_cfg, Mapping):
                unsupported_build_keys = sorted(
                    set(build_cfg) - {"context", "dockerfile", "args"}, key=str
                )
                if unsupported_build_keys:
                    diagnostics.append(
                        _diag(
                            "compose_build_key_unsupported",
                            rel_path,
                            f"service {name!r} build uses unsupported keys {unsupported_build_keys}",
                        )
                    )
                ctx_str = str(build_cfg.get("context", "."))
            else:
                ctx_str = "."
            resolved_ctx = (task_dir / "environment" / ctx_str).resolve()
            if not _is_under(resolved_ctx, task_dir / "environment"):
                diagnostics.append(
                    _diag(
                        "compose_build_path_escape",
                        rel_path,
                        f"service {name!r} build context {ctx_str!r} escapes environment directory",
                    )
                )
            else:
                build_context_rel = resolved_ctx.relative_to(task_dir.resolve()).as_posix()
                dockerfile_name = (
                    str(build_cfg.get("dockerfile", "Dockerfile"))
                    if isinstance(build_cfg, Mapping)
                    else "Dockerfile"
                )
                nested_dockerfile = resolved_ctx / dockerfile_name
                if not nested_dockerfile.is_file():
                    diagnostics.append(
                        _diag(
                            "compose_build_dockerfile_missing",
                            rel_path,
                            f"service {name!r} local build context has no Dockerfile",
                        )
                    )
                else:
                    nested_relative = nested_dockerfile.relative_to(task_dir).as_posix()
                    nested_text = _read_text(nested_dockerfile)
                    sidecar_proof = any(
                        (resolved_ctx / pn).is_file()
                        for pn in ("build-proof.json", "offline-build-proof.json")
                    )
                    _validate_build_network(
                        nested_text, nested_relative, diagnostics, has_proof=sidecar_proof
                    )
                    from_lines = [
                        line
                        for line in _docker_logical_lines(nested_text)
                        if re.match(r"(?i)^FROM\s+", line)
                    ]
                    if not from_lines:
                        diagnostics.append(
                            _diag(
                                "compose_build_from_missing",
                                nested_relative,
                                f"service {name!r} Dockerfile has no FROM instruction",
                            )
                        )
                    for from_line in from_lines:
                        ref = from_line.split()[1] if len(from_line.split()) >= 2 else ""
                        if not _is_pinned_image_ref(ref):
                            diagnostics.append(
                                _diag(
                                    "compose_image_unpinned",
                                    nested_relative,
                                    f"service {name!r} base image {ref!r} is not immutable",
                                )
                            )
        elif image_cfg is not None:
            if not isinstance(image_cfg, str) or not _is_pinned_image_ref(image_cfg):
                diagnostics.append(
                    _diag(
                        "compose_image_unpinned",
                        rel_path,
                        f"service {name!r} image {image_cfg!r} must be pinned by @sha256 digest or immutable version",
                    )
                )
        elif name != "main":
            diagnostics.append(
                _diag(
                    "compose_structure_invalid",
                    rel_path,
                    f"service {name!r} must declare either 'build' context or pinned 'image'",
                )
            )
        service_summaries[name] = {
            "name": name,
            "build_context": build_context_rel,
            "image": image_cfg if isinstance(image_cfg, str) else None,
            "environment": sidecar_env if name != "main" else None,
            "volumes": valid_mounts,
        }
    volume_record: dict[str, Any] | None = None
    if volume_name:
        main_targets = [m["target"] for m in service_summaries.get("main", {}).get("volumes", [])]
        sidecar_targets = [
            m["target"]
            for m in service_summaries.get(sidecar_name or "", {}).get("volumes", [])
            if sidecar_name
        ]
        mount_path: str | None = None
        if main_targets:
            mount_path = main_targets[0]
        elif sidecar_targets:
            mount_path = sidecar_targets[0]
        if main_targets and sidecar_targets and main_targets[0] != sidecar_targets[0]:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"main and sidecar must mount {volume_name!r} to the same target, "
                    f"got {main_targets[0]!r} and {sidecar_targets[0]!r}",
                )
            )
        volume_record = {
            "name": volume_name,
            "mount_path": mount_path,
            "definition": volume_definition,
        }
    if networks_valid and network_name is not None and sidecar_name is None:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"internal network {network_name!r} requires a sidecar service",
            )
        )
    topology_record = {
        "compose_file": rel_path,
        "services": service_summaries,
        "sidecar_service": sidecar_name,
        "network": network_record,
        "volume": volume_record,
        "digest": _sha256_bytes(_canonical_bytes(data)),
    }
    return topology_record, sidecar_name


def _validate_build_context_contents(
    task_dir: Path,
    diagnostics: list[Diagnostic],
    build_proofs: Mapping[str, Any] | None = None,
    compose_topology: Mapping[str, Any] | None = None,
) -> None:
    """Refuse unmodelled configuration files, then content-scan the payload."""
    all_contexts = _derive_all_build_contexts(task_dir, compose_topology, diagnostics)
    nested_roots = [
        (task_dir / ctx).resolve()
        for ctx, _img in all_contexts
        if ctx not in ("environment", "tests")
    ]
    for context, image in all_contexts:
        root = task_dir / context
        if not root.is_dir() or root.is_symlink():
            continue
        dockerfile = f"{context}/Dockerfile"
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved_path = path.resolve()
            if context == "environment" and any(
                _is_under(resolved_path, n_root) for n_root in nested_roots
            ):
                continue
            relative = path.relative_to(task_dir).as_posix()
            if relative == dockerfile:
                continue
            if context == "environment" and path.name in {
                "docker-compose.yaml",
                "docker-compose.yml",
            }:
                continue
            if path.name in {"build-proof.json", "offline-build-proof.json"}:
                continue
            proof_asset_paths = (
                build_proofs.get(context, {}).get("runtime_asset_paths", frozenset())
                if build_proofs
                else frozenset()
            )
            context_relative = path.relative_to(root).as_posix()
            if context_relative.startswith("wheelhouse/") and path.suffix == ".whl":
                if build_proofs and context in build_proofs:
                    _verify_wheel_in_build_proof(path, root, build_proofs[context], diagnostics)
                    continue
                diagnostics.append(
                    _diag(
                        "build_context_unreadable",
                        relative,
                        f"binary wheel {path.name!r} is only permitted with a reviewed build proof",
                    )
                )
                continue
            unmodelled = _unmodelled_build_config(path.name, context)
            if unmodelled is not None:
                code, detail = unmodelled
                diagnostics.append(
                    _diag(code, relative, f"{detail}; refused in the build context for {image}")
                )
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                if context_relative in proof_asset_paths:
                    # Exact proof has already bound this binary payload's path, bytes, size, and COPY mapping.
                    # Text proof-bound assets still reach the normal content/network scan above.
                    continue
                diagnostics.append(
                    _diag(
                        "build_context_unreadable",
                        relative,
                        f"every file in the build context for {image} must be readable UTF-8 "
                        "text so it can be scanned for build-time network use; ship "
                        "reviewable inputs instead",
                    )
                )
                continue
            if BUILD_NETWORK_PATTERN.search(text):
                unapproved = any(
                    BUILD_NETWORK_PATTERN.search(line)
                    and not (
                        build_proofs
                        and context in build_proofs
                        and _is_proven_offline_install(line)
                    )
                    for line in _docker_logical_lines(text)
                )
                if unapproved:
                    diagnostics.append(
                        _diag(
                            "build_network_use",
                            relative,
                            f"files in the build context for {image} may not fetch from a network "
                            "or invoke an online package manager; proof permits only exact "
                            "offline frozen installs",
                        )
                    )


def _verify_wheel_in_build_proof(
    path: Path,
    root: Path,
    proof: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> None:
    """Verify a .whl file is covered by the build proof and has a matching hash."""
    rel = path.relative_to(root.parent).as_posix()
    pinned = proof.get("pinned_dependencies")
    wheel_hash = _sha256_file(path)
    if not isinstance(pinned, (Mapping, Sequence)):
        diagnostics.append(
            _diag(
                "build_proof_invalid",
                rel,
                "build proof has no pinned dependencies for wheel verification",
            )
        )
        return
    matched: dict[str, Any] | None = None
    if isinstance(pinned, Mapping):
        for name, value in pinned.items():
            if isinstance(value, Mapping):
                item = value
            else:
                item = {"name": name, "version": value, "hash": None, "wheel": None}
            if _wheel_matches_entry(path.name, item):
                matched = item
                break
    elif isinstance(pinned, Sequence):
        for item in pinned:
            if _wheel_matches_entry(path.name, item):
                matched = item
                break
    if matched is None:
        diagnostics.append(
            _diag(
                "build_proof_unpinned_dependency",
                rel,
                f"wheel {path.name!r} is not listed in the build proof",
            )
        )
        return
    declared_hash = matched.get("hash") or matched.get("sha256")
    if declared_hash and str(declared_hash).lower().removeprefix(
        "sha256:"
    ) != wheel_hash.removeprefix("sha256:"):
        diagnostics.append(
            _diag(
                "build_proof_invalid",
                rel,
                f"wheel {path.name!r} digest {wheel_hash} does not match proof {declared_hash}",
            )
        )


def _wheel_matches_entry(wheel_name: str, item: Mapping[str, Any]) -> bool:
    """Check whether a wheel filename matches a pinned dependency entry."""
    if not isinstance(item, Mapping):
        return False
    declared_wheel = item.get("wheel")
    if isinstance(declared_wheel, str):
        return wheel_name == declared_wheel or wheel_name == declared_wheel.rsplit("/", 1)[-1]
    name = str(item.get("name", "")).replace("-", "_")
    version = str(item.get("version", ""))
    if not name or not version:
        return False
    normalized = name.replace("-", "_").lower()
    wheel_stem = wheel_name.lower().rsplit(".whl", 1)[0]
    # wheel naming: name-version[-build?]-python-abi-platform.whl
    return wheel_stem.startswith(f"{normalized}-{version}")


# Retained receipt readers have no execution or registration authority.
SCHEMA_VERSION = 1
WORKBENCH_VERSION = "m049-v1"


# Read-only historical receipt and native review comparisons.
def _file_sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# Owned external quality-audit format: metadata.json + observed-summary.json
# (arm-name mapping) with no summary.json. Recorded manifests use rich
# {sha256, size, copied_mode} entries; hashes may be bare hex or sha256:-prefixed.
_OWNED_VERIFIER_CANDIDATES = ("offline-tests", "repaired-tests", "derivative/tests")
_OWNED_SOURCE_PACKAGE_DIR = "task"
_OWNED_CONTROLS_DIR = "controls"
_OWNED_RUNTIME_ENV_DIR = "runtime-environment"


def _owned_bare_sha(value: Any) -> str | None:
    """Normalize a recorded manifest digest to bare lowercase hex, or None."""
    if not isinstance(value, str):
        return None
    bare = value.lower().removeprefix("sha256:")
    if len(bare) != 64 or any(char not in "0123456789abcdef" for char in bare):
        return None
    return bare


def _owned_manifest_check(manifest: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Verify every recorded manifest entry against retained bytes under root.

    Names alone never bind: each entry must exist as a regular file with
    matching sha256 and size. A missing candidate root leaves coverage unbound;
    present-but-differing bytes are mismatched.
    """
    detail: dict[str, Any] = {
        "status": "unbound",
        "checked_files": 0,
        "matched_files": 0,
        "missing_files": [],
        "mismatched_files": [],
        "executed_manifest": {},
    }
    entries = dict(manifest) if isinstance(manifest, Mapping) else {}
    if not entries:
        return detail
    if root.is_symlink() or not root.is_dir():
        return detail
    try:
        resolved_root = root.resolve()
    except OSError:
        return detail
    missing: list[str] = []
    mismatched: list[str] = []
    executed: dict[str, dict[str, Any]] = {}
    checked = 0
    matched = 0
    for rel in sorted(entries):
        expected = entries[rel]
        recorded = _owned_bare_sha(
            expected.get("sha256") if isinstance(expected, Mapping) else None
        )
        expected_size = expected.get("size") if isinstance(expected, Mapping) else None
        relative = PurePosixPath(rel)
        target = resolved_root / rel
        if (
            recorded is None
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != rel
        ):
            mismatched.append(rel)
            continue
        if target.is_symlink() or not target.is_file():
            missing.append(rel)
            continue
        try:
            target.resolve().relative_to(resolved_root)
        except ValueError:
            missing.append(rel)
            continue
        checked += 1
        try:
            actual_size = target.stat().st_size
        except OSError:
            mismatched.append(rel)
            continue
        # Host stat modes/owners are never read: only content bytes bind.
        actual_sha = _file_sha256_hex(target)
        if actual_sha == recorded and actual_size == expected_size:
            matched += 1
            executed[rel] = {"sha256": actual_sha, "size": actual_size}
        else:
            mismatched.append(rel)
    retained_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    mismatched.extend(sorted(retained_files - entries.keys()))
    detail["checked_files"] = checked
    detail["matched_files"] = matched
    detail["missing_files"] = missing
    detail["mismatched_files"] = mismatched
    detail["executed_manifest"] = executed
    if missing or mismatched:
        detail["status"] = "mismatched"
    elif matched == len(entries) and matched > 0:
        detail["status"] = "verified"
    else:
        detail["status"] = "mismatched"
    return detail


def _owned_runner_check(recorded: Any, path: Path) -> dict[str, Any]:
    """Bind one recorded runner hash against explicit companion source bytes."""
    bare = _owned_bare_sha(recorded)
    detail: dict[str, Any] = {
        "status": "unbound",
        "recorded_sha256": bare,
        "executed_sha256": None,
        "executed_path": None,
    }
    if bare is None:
        return detail
    if path.is_symlink() or not path.is_file():
        return detail
    executed = _file_sha256_hex(path)
    detail["executed_sha256"] = executed
    detail["executed_path"] = str(path)
    detail["status"] = "verified" if executed == bare else "mismatched"
    return detail


def _owned_float(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (ValueError, TypeError):
        return None
    return result if result is not None and math.isfinite(result) else None


def _owned_arm_record(
    *,
    arm_name: str,
    summary_entry: Mapping[str, Any],
    arm_dir: Path | None,
    declaration_entry: Mapping[str, Any],
    runtime_input_paths: list[str],
) -> dict[str, Any]:
    """Build one owned arm record from actual compact result fields.

    Reward annotations (accepted/rejected) and declaration labels stay separate
    from ordinary result execution status. No task scripts are executed.
    """
    record: dict[str, Any] = dict(summary_entry) if isinstance(summary_entry, Mapping) else {}
    result_file = arm_dir / "result.json" if arm_dir is not None else None
    execution_record: dict[str, Any] = {}
    if arm_dir is not None and arm_dir.is_dir():
        record.update(_audit_json_object(arm_dir / "observed.json"))
        execution_record = _audit_json_object(arm_dir / "result.json")
        record.update(execution_record)
    # Cached summaries/annotations cannot replace the original execution receipt.
    has_record = result_file is not None and result_file.is_file()
    digests: dict[str, Any] = {}
    task_manifest: dict[str, Any] | None = None
    runtime_manifest_issue: str | None = None
    if arm_dir is not None and arm_dir.is_dir():
        digests = _audit_json_object(arm_dir / "digests.json")
        try:
            raw_manifest = _audit_json_object(arm_dir / "task-file-manifest.json")
            task_manifest = dict(raw_manifest) if raw_manifest else None
        except (WorkbenchError, OSError, ValueError):
            runtime_manifest_issue = "runtime_manifest_unreadable"
    observed_reward = _owned_float(execution_record.get("reward"))
    ctrf_summary = execution_record.get("ctrf_summary")
    if not isinstance(ctrf_summary, Mapping):
        ctrf_summary = {}
    action_exit = execution_record.get("action_exit")
    verifier_exit = execution_record.get("verifier_exit")
    if not has_record:
        execution_status = "missing"
    elif action_exit == 0 and verifier_exit == 0 and observed_reward is not None:
        execution_status = "completed"
    elif action_exit is None and verifier_exit is None and observed_reward is None:
        execution_status = "unassessed"
    else:
        execution_status = "failed"
    declared_expected = _owned_float(declaration_entry.get("expected_reward"))
    inputs = digests.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = {}
    outputs = record.get("output_digests")
    if not isinstance(outputs, Mapping):
        outputs = digests.get("outputs") if isinstance(digests.get("outputs"), Mapping) else {}
    return {
        "arm": arm_name,
        "execution_status": execution_status,
        "observed_reward": observed_reward,
        "declared_label": declaration_entry.get("declared_validity"),
        "declared_expected_reward": declared_expected,
        "declared_rationale": declaration_entry.get("rationale"),
        "observed_outcome": record.get("outcome"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "action_exit": action_exit,
        "verifier_exit": verifier_exit,
        "tests_passed": ctrf_summary.get("passed"),
        "tests_total": ctrf_summary.get("tests"),
        "evidence_path": str(arm_dir) if arm_dir is not None and arm_dir.is_dir() else None,
        "input_digests": dict(inputs),
        "output_digests": dict(outputs) if isinstance(outputs, Mapping) else {},
        "task_file_manifest": task_manifest,
        "runtime_manifest_issue": runtime_manifest_issue,
        "runtime_metadata_required": True,
        "runtime_input_paths": list(runtime_input_paths),
    }


def _load_owned_quality_audit_evidence(
    audit_path: Path, *, source_root: Path | None = None
) -> dict[str, Any]:
    """Consume an owned-format evidence directory (metadata.json + observed-summary.json)."""
    try:
        metadata = json.loads((audit_path / "metadata.json").read_text(encoding="utf-8"))
        observed = json.loads((audit_path / "observed-summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(
            f"owned audit directory has unreadable metadata: {audit_path}: {exc}"
        ) from exc
    if not isinstance(metadata, dict) or not isinstance(observed, dict):
        raise WorkbenchError(f"owned audit directory has malformed metadata: {audit_path}")
    declaration = metadata.get("declaration")
    if not isinstance(declaration, Mapping):
        raise WorkbenchError(f"owned audit directory missing declaration: {audit_path}")
    declared_arms = declaration.get("arms")
    if not isinstance(declared_arms, Mapping):
        raise WorkbenchError(f"owned audit directory missing declared arms: {audit_path}")
    declared_task_id = declaration.get("task_id")
    declared_source_path = declaration.get("source_path")
    phase = metadata.get("phase")
    task_id = metadata.get("task_id")
    condition_id = (
        f"{phase}/{task_id}" if isinstance(phase, str) and isinstance(task_id, str) else None
    )

    def _recorded_manifest(key: str) -> dict[str, Any]:
        raw = metadata.get(key)
        if not isinstance(raw, Mapping):
            return {}
        normalized: dict[str, Any] = {}
        for rel, entry in raw.items():
            if not isinstance(entry, Mapping):
                continue
            bare = _owned_bare_sha(entry.get("sha256"))
            if bare is None:
                continue
            normalized[str(rel)] = {"sha256": bare, "size": entry.get("size")}
        return normalized

    recorded_verifier = _recorded_manifest("verifier_manifest")
    recorded_source = _recorded_manifest("source_task_manifest")
    recorded_controls = _recorded_manifest("controls_manifest")
    recorded_runtime_env = _recorded_manifest("runtime_environment_manifest")
    # Exact initial runtime paths, with the task_file/ mount prefix stripped.
    # These describe the declared initial environment, not pre-action execution proof.
    runtime_input_paths = sorted(
        str(rel).removeprefix("task_file/")
        for rel in recorded_runtime_env
        if str(rel).startswith("task_file/")
    )
    source_base: Path | None = None
    if source_root is not None and isinstance(declared_task_id, str) and declared_task_id:
        candidate_base = source_root.resolve() / "tasks" / declared_task_id
        if (
            PurePosixPath(declared_task_id).name == declared_task_id
            and candidate_base.resolve().is_relative_to(source_root.resolve())
            and not candidate_base.is_symlink()
            and candidate_base.is_dir()
        ):
            source_base = candidate_base
    package_binding: dict[str, Any] = {"status": "unbound"}
    if source_base is not None:
        package_binding = _owned_manifest_check(
            metadata.get("source_task_manifest")
            if isinstance(metadata.get("source_task_manifest"), Mapping)
            else {},
            source_base / _OWNED_SOURCE_PACKAGE_DIR,
        )
    verifier_candidates: list[dict[str, Any]] = []
    verifier_status = "unbound"
    verifier_details: dict[str, Any] = {}
    if source_base is not None:
        examined = 0
        for candidate in _OWNED_VERIFIER_CANDIDATES:
            candidate_root = source_base / candidate
            if candidate_root.is_symlink() or not candidate_root.is_dir():
                continue
            examined += 1
            check = _owned_manifest_check(
                metadata.get("verifier_manifest")
                if isinstance(metadata.get("verifier_manifest"), Mapping)
                else {},
                candidate_root,
            )
            if check["status"] == "verified":
                verifier_candidates.append(
                    {
                        "candidate": candidate,
                        "path": str(candidate_root.resolve()),
                        "executed_manifest": check["executed_manifest"],
                    }
                )
            else:
                verifier_details[f"candidate_{candidate}"] = {
                    "status": check["status"],
                    "missing_files": check["missing_files"],
                    "mismatched_files": check["mismatched_files"],
                }
        if verifier_candidates:
            verifier_status = "verified"
        elif examined > 0:
            verifier_status = "mismatched"
    executed_manifest: dict[str, Any] = (
        verifier_candidates[0]["executed_manifest"] if verifier_candidates else {}
    )
    recorded_test_state = recorded_verifier.get("test_state.py", {}).get("sha256")
    executed_test_state = executed_manifest.get("test_state.py", {}).get("sha256")
    runner_binding: dict[str, Any] = {"status": "unbound"}
    ownership_runner_binding: dict[str, Any] = {"status": "unbound"}
    controls_binding: dict[str, Any] = {"status": "unbound"}
    runtime_env_binding: dict[str, Any] = {"status": "unbound"}
    if source_root is not None:
        lane_root = source_root.resolve()
        runner_binding = _owned_runner_check(metadata.get("runner_sha256"), lane_root / "runner.py")
        ownership_runner_binding = _owned_runner_check(
            metadata.get("ownership_runner_sha256"), lane_root / "ownership_runner.py"
        )
        if source_base is not None:
            controls_binding = _owned_manifest_check(
                metadata.get("controls_manifest")
                if isinstance(metadata.get("controls_manifest"), Mapping)
                else {},
                source_base / _OWNED_CONTROLS_DIR,
            )
            runtime_env_binding = _owned_manifest_check(
                metadata.get("runtime_environment_manifest")
                if isinstance(metadata.get("runtime_environment_manifest"), Mapping)
                else {},
                source_base / _OWNED_RUNTIME_ENV_DIR,
            )
    package_status = package_binding.get("status", "unbound")
    if package_status == "mismatched" or verifier_status == "mismatched":
        provenance_status = "mismatched"
    elif package_status == "verified" and verifier_status == "verified":
        provenance_status = "verified"
    elif package_status in ("verified", "partial") or verifier_status in ("verified", "partial"):
        provenance_status = "partial"
    else:
        provenance_status = "unbound"
    disk_arm_dirs = {
        path.name: path
        for path in audit_path.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and (
            path.name in declared_arms or path.name in observed or (path / "result.json").is_file()
        )
    }
    all_arm_names = sorted(
        set(declared_arms.keys()) | set(observed.keys()) | set(disk_arm_dirs.keys())
    )
    arms: dict[str, Any] = {}
    for arm_name in all_arm_names:
        summary_entry = observed.get(arm_name)
        declaration_entry = declared_arms.get(arm_name)
        arms[arm_name] = _owned_arm_record(
            arm_name=arm_name,
            summary_entry=summary_entry if isinstance(summary_entry, Mapping) else {},
            arm_dir=disk_arm_dirs.get(arm_name),
            declaration_entry=declaration_entry if isinstance(declaration_entry, Mapping) else {},
            runtime_input_paths=runtime_input_paths,
        )
    lead_review = declaration.get("lead_review")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "quality_audit_evidence",
        "evidence_format": "quality_owned",
        "origin": "external_quality_audit",
        "workbench_version": WORKBENCH_VERSION,
        "audit_path": str(audit_path),
        "run_id": None,
        "run_uuid": None,
        "condition_id": condition_id,
        "declaration": dict(declaration),
        "task_dir": str(source_base / _OWNED_SOURCE_PACKAGE_DIR) if source_base else None,
        "declared_task_id": declared_task_id,
        "declared_source_path": declared_source_path,
        "phase": phase,
        "verification_mode": metadata.get("verification_mode"),
        "container_image": metadata.get("image_id"),
        "provenance_binding": {
            "status": provenance_status,
            "package_snapshot_status": package_status,
            "expected_files": len(recorded_source),
            "checked_files": package_binding.get("checked_files", 0),
            "matched_files": package_binding.get("matched_files", 0),
            "missing_files": package_binding.get("missing_files", []),
            "mismatched_files": package_binding.get("mismatched_files", []),
            "executed_verifier_status": verifier_status,
            "recorded_verifier_manifest": recorded_verifier,
            "recorded_verifier_sha256": recorded_test_state,
            "executed_verifier_sha256": executed_test_state,
            "executed_verifier_manifest": executed_manifest,
            "executed_verifier_path": (
                verifier_candidates[0]["path"] if verifier_candidates else None
            ),
            "executed_verifier_candidates": [item["candidate"] for item in verifier_candidates],
            **verifier_details,
            "recorded_source_task_manifest": recorded_source,
            "recorded_controls_manifest": recorded_controls,
            "recorded_runtime_environment_manifest": recorded_runtime_env,
            "recorded_runner_sha256": _owned_bare_sha(metadata.get("runner_sha256")),
            "recorded_ownership_runner_sha256": _owned_bare_sha(
                metadata.get("ownership_runner_sha256")
            ),
            "runner_binding": runner_binding,
            "ownership_runner_binding": ownership_runner_binding,
            "controls_binding": controls_binding,
            "runtime_environment_binding": runtime_env_binding,
        },
        "execution_summary": {
            "arms_discovered": len(arms),
            "completed_arms": sum(
                1 for item in arms.values() if item["execution_status"] == "completed"
            ),
            "failed_arms": sum(1 for item in arms.values() if item["execution_status"] == "failed"),
        },
        "arms": arms,
        "reviewer_findings": {"lead_review": lead_review} if lead_review else {},
        "admission_authority": "candidate_evidence_only",
        "notices": [
            "Direct-Docker candidate evidence only; not Harbor certification.",
            "Arm names and reviewer labels do not determine validity or admission.",
            "No tasks admitted, registered, or published.",
        ],
    }


def load_quality_audit_evidence(
    audit_dir: Path,
    *,
    repo_root: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Consume an external quality-audit evidence directory read-only.

    Returns a normalized record of per-arm execution outcomes, verifierCTRFFacts,
    and task/verifier provenance bindings without executing tasks or mutating admission state.

    `source_root` optionally points at explicit companion source bytes (the cohort
    lane root); it is never guessed from host paths baked into the evidence.
    """
    audit_path = audit_dir.resolve()
    if not audit_path.is_dir():
        raise WorkbenchError(f"audit directory does not exist: {audit_path}")

    summary_file = audit_path / "summary.json"
    if not summary_file.is_file():
        owned_metadata = audit_path / "metadata.json"
        owned_observed = audit_path / "observed-summary.json"
        if owned_metadata.is_file() and owned_observed.is_file():
            return _load_owned_quality_audit_evidence(audit_path, source_root=source_root)
        raise WorkbenchError(f"audit directory missing summary.json: {audit_path}")

    summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
    run_meta_file = audit_path / "run_meta.json"
    run_meta_data = (
        json.loads(run_meta_file.read_text(encoding="utf-8")) if run_meta_file.is_file() else {}
    )
    audit_rollup_file = audit_path / "audit-rollup.json"
    audit_rollup_data = (
        json.loads(audit_rollup_file.read_text(encoding="utf-8"))
        if audit_rollup_file.is_file()
        else {}
    )
    task_manifest_file = audit_path / "task-manifest.json"
    if not task_manifest_file.is_file() and (audit_path.parent / "task-manifest.json").is_file():
        task_manifest_file = audit_path.parent / "task-manifest.json"
    task_manifest_data = (
        json.loads(task_manifest_file.read_text(encoding="utf-8"))
        if task_manifest_file.is_file()
        else {}
    )

    run_id = summary_data.get("run_id") or run_meta_data.get("run_id") or audit_path.name
    run_uuid = summary_data.get("run_uuid") or run_meta_data.get("run_uuid")
    meta_block = run_meta_data.get("meta", {})
    task_dir_raw = meta_block.get("task_dir")
    task_dir = Path(task_dir_raw).resolve() if task_dir_raw else None
    container_image = meta_block.get("image")
    input_digests = meta_block.get("input_digests", {})

    # Provenance binding check:
    # Look ONLY at explicit declarations/manifests belonging to this evidence directory.
    # NEVER borrow hashes from sibling directories or guess based on task name patterns.
    recorded_verifier_sha = (
        meta_block.get("verifier_sha256")
        or summary_data.get("verifier_sha256")
        or summary_data.get("findings", {}).get("verifier_sha256")
    )

    provenance_status = "unbound"
    binding_details: dict[str, Any] = {}

    retained_hashes: dict[str, str] = {}
    if audit_rollup_data and "hashes" in audit_rollup_data:
        hashes_block = audit_rollup_data["hashes"]
        if (
            task_dir
            and task_dir.name in hashes_block
            and isinstance(hashes_block[task_dir.name], dict)
        ):
            retained_hashes = hashes_block[task_dir.name]
    elif task_manifest_data:
        retained_hashes = task_manifest_data

    # 1. Package snapshot check (does task_dir match retained manifest)
    package_snapshot_status = "unbound"
    if task_dir:
        if not task_dir.is_dir():
            package_snapshot_status = "missing_task_dir"
        elif retained_hashes:
            checked_files = 0
            matched_files = 0
            mismatched_files: list[str] = []
            missing_files: list[str] = []
            verified_package_files: dict[str, str] = {}
            for rel_path, expected_hash in retained_hashes.items():
                if rel_path.startswith("image_") or not isinstance(expected_hash, str):
                    continue
                cand_file = task_dir / rel_path
                if not cand_file.is_file():
                    missing_files.append(rel_path)
                    continue
                checked_files += 1
                actual_hash = _file_sha256_hex(cand_file)
                if actual_hash == expected_hash.removeprefix("sha256:"):
                    matched_files += 1
                    verified_package_files[rel_path] = actual_hash
                else:
                    mismatched_files.append(rel_path)

            binding_details["expected_files"] = len(retained_hashes)
            binding_details["checked_files"] = checked_files
            binding_details["matched_files"] = matched_files
            binding_details["verified_package_files"] = verified_package_files
            if missing_files:
                binding_details["missing_files"] = missing_files
            if mismatched_files:
                binding_details["mismatched_files"] = mismatched_files

            # Check input_digests from run_meta if present
            input_mismatches: list[str] = []
            input_missing: list[str] = []
            if input_digests:
                for input_rel, expected_hash in input_digests.items():
                    cand_input = task_dir / "environment/task_file/input" / input_rel
                    if not cand_input.is_file():
                        cand_input = task_dir / "input" / input_rel
                    if not cand_input.is_file():
                        input_missing.append(input_rel)
                        continue
                    actual_hash = _file_sha256_hex(cand_input)
                    if actual_hash != expected_hash.removeprefix("sha256:"):
                        input_mismatches.append(input_rel)

                if input_missing:
                    binding_details["input_missing"] = input_missing
                if input_mismatches:
                    binding_details["input_mismatches"] = input_mismatches

            if missing_files or mismatched_files or input_mismatches or input_missing:
                package_snapshot_status = "mismatched"
            elif checked_files == len(retained_hashes) and checked_files > 0:
                package_snapshot_status = "verified"
            else:
                package_snapshot_status = "partial"

    binding_details["package_snapshot_status"] = package_snapshot_status

    # 2. Executed-verifier binding check
    # Locate actual verifier bytes executed by this run
    executed_verifier_status = "unbound"
    retained_verifier_candidates = [
        audit_path / "tests/test_state.py",
        audit_path / "original-tests/test_state.py",
        audit_path.parent / "original-tests/test_state.py",
    ]
    if task_dir and task_dir.is_dir():
        retained_verifier_candidates.append(task_dir / "tests/test_state.py")

    for cand_v in retained_verifier_candidates:
        if cand_v.is_file():
            v_hash = _file_sha256_hex(cand_v)
            if recorded_verifier_sha and v_hash == recorded_verifier_sha.removeprefix("sha256:"):
                executed_verifier_status = "verified"
                binding_details["executed_verifier_sha256"] = v_hash
                binding_details["executed_verifier_path"] = str(cand_v)
                break

    if recorded_verifier_sha:
        binding_details["recorded_verifier_sha256"] = recorded_verifier_sha.removeprefix("sha256:")
        if executed_verifier_status != "verified":
            # If snapshot has a verifier but its hash doesn't match recorded_verifier_sha
            if task_dir and (task_dir / "tests/test_state.py").is_file():
                snap_v_hash = _file_sha256_hex(task_dir / "tests/test_state.py")
                binding_details["snapshot_verifier_sha256"] = snap_v_hash
            executed_verifier_status = "mismatched"

    binding_details["executed_verifier_status"] = executed_verifier_status

    # Composite provenance status composition rule:
    # - "verified" ONLY when BOTH package_snapshot_status and executed_verifier_status are "verified".
    # - "mismatched" if either status is "mismatched".
    # - "partial" if one is verified and the other is unbound/partial/missing.
    # - "unbound" if neither has positive verification.
    if package_snapshot_status == "mismatched" or executed_verifier_status == "mismatched":
        provenance_status = "mismatched"
    elif package_snapshot_status == "verified" and executed_verifier_status == "verified":
        provenance_status = "verified"
    elif package_snapshot_status in ("verified", "partial") or executed_verifier_status in (
        "verified",
        "partial",
    ):
        provenance_status = "partial"
    else:
        provenance_status = "unbound"
    # Arms extraction
    arms: dict[str, Any] = {}
    raw_arms_summary = summary_data.get("arms", {})
    findings_per_arm = summary_data.get("findings", {}).get("per_arm", {})

    disk_arm_dirs = {
        p.name: p
        for p in audit_path.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and (
            p.name in raw_arms_summary
            or p.name in findings_per_arm
            or (p / "result.json").is_file()
        )
    }
    all_arm_names = sorted(
        set(raw_arms_summary.keys()) | set(findings_per_arm.keys()) | set(disk_arm_dirs.keys())
    )

    for arm_name in all_arm_names:
        arm_dir = disk_arm_dirs.get(arm_name)
        result_file = arm_dir / "result.json" if arm_dir else None
        result_data = (
            json.loads(result_file.read_text(encoding="utf-8"))
            if result_file and result_file.is_file()
            else {}
        )
        digests_file = arm_dir / "digests.json" if arm_dir else None
        digests_data = (
            json.loads(digests_file.read_text(encoding="utf-8"))
            if digests_file and digests_file.is_file()
            else {}
        )
        task_manifest_file = arm_dir / "task-file-manifest.json" if arm_dir else None
        task_manifest = None
        runtime_manifest_issue = None
        if task_manifest_file is not None:
            try:
                task_manifest = _audit_json_object(task_manifest_file) or None
            except (WorkbenchError, OSError, ValueError):
                runtime_manifest_issue = "runtime_manifest_unreadable"

        arm_summary = raw_arms_summary.get(arm_name, {})
        finding = findings_per_arm.get(arm_name, {})

        observed_reward_raw = result_data.get("reward", arm_summary.get("reward"))
        # float conversion if numeric string
        try:
            observed_reward = (
                float(observed_reward_raw) if observed_reward_raw is not None else None
            )
        except (ValueError, TypeError):
            observed_reward = None
        if observed_reward is not None and not math.isfinite(observed_reward):
            observed_reward = None

        ctrf_summary = result_data.get("ctrf_summary") or arm_summary.get("ctrf_summary") or {}
        tests_passed = ctrf_summary.get("passed", 0)
        tests_total = ctrf_summary.get("tests", 0)

        # Arm execution status: completed vs missing/failed vs unassessed
        action_exit = result_data.get("action_exit")
        verifier_exit = result_data.get("verifier_exit")
        if not result_file or not result_file.is_file():
            execution_status = "missing"
        elif action_exit == 0 and verifier_exit == 0 and observed_reward is not None:
            execution_status = "completed"
        elif action_exit is None and verifier_exit is None and observed_reward is None:
            execution_status = "unassessed"
        else:
            execution_status = "failed"
        arms[arm_name] = {
            "arm": arm_name,
            "execution_status": execution_status,
            "observed_reward": observed_reward,
            "declared_label": finding.get("label"),
            "declared_expected_reward": (
                float(finding["expected_reward"])
                if finding.get("expected_reward") is not None
                else None
            ),
            "action_exit": action_exit,
            "verifier_exit": verifier_exit,
            "tests_passed": tests_passed,
            "tests_total": tests_total,
            "evidence_path": str(arm_dir) if arm_dir and arm_dir.is_dir() else None,
            "input_digests": digests_data.get("inputs", {}),
            "output_digests": result_data.get("output_digests", digests_data.get("outputs", {})),
            "task_file_manifest": task_manifest if task_manifest else None,
            "runtime_manifest_issue": runtime_manifest_issue,
        }

    # Retain reviewer findings verbatim without conflating with execution
    reviewer_findings = summary_data.get("findings", {})

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "quality_audit_evidence",
        "workbench_version": WORKBENCH_VERSION,
        "audit_path": str(audit_path),
        "run_id": run_id,
        "run_uuid": run_uuid,
        "task_dir": str(task_dir) if task_dir else None,
        "container_image": container_image,
        "provenance_binding": {
            "status": provenance_status,
            **binding_details,
        },
        "execution_summary": {
            "arms_discovered": len(arms),
            "completed_arms": sum(1 for a in arms.values() if a["execution_status"] == "completed"),
            "failed_arms": sum(1 for a in arms.values() if a["execution_status"] == "failed"),
        },
        "arms": arms,
        "reviewer_findings": reviewer_findings,
        "admission_authority": "candidate_evidence_only",
        "notices": [
            "Direct-Docker candidate evidence only; not Harbor certification.",
            "Arm names and reviewer labels do not determine validity or admission.",
            "No tasks admitted, registered, or published.",
        ],
    }


def _audit_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkbenchError(f"expected a JSON object: {path}")
    return value


def _archive_entry_matches(entry: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """Compare one archive manifest entry against the streamed tar observation."""
    sha256 = entry.get("sha256")
    size = entry.get("size")
    uid = entry.get("uid")
    gid = entry.get("gid")
    mode = entry.get("mode")
    return (
        isinstance(sha256, str)
        and _is_sha256_hex(sha256)
        and sha256.lower().removeprefix("sha256:") == observed["sha256"]
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size == observed["size"]
        and isinstance(uid, int)
        and not isinstance(uid, bool)
        and uid == observed["uid"]
        and isinstance(gid, int)
        and not isinstance(gid, bool)
        and gid == observed["gid"]
        and isinstance(mode, str)
        and mode == observed["mode"]
    )


def _audit_archive_identity(arm_path: Path, kind: str) -> dict[str, Any]:
    """Stream-check one retained task_file export archive without extracting it.

    The runner exports ``{kind}-task-file.tar`` from a live container with
    ``docker cp <cid>:/task_file/. -`` and retains the command digest/exit in
    ``{kind}-task-file.receipt.json`` plus per-file
    ``{sha256,size,uid,gid,mode}`` entries in ``{kind}-task-file.manifest.json``.
    The action archive is exported once the action phase has finished; the
    verifier archive is exported right after the task_file copy-in and before
    the verifier starts, so neither archive ever describes pre-action state.

    Members are streamed read-only: regular files are hashed in chunks, and
    symlinks, hardlinks, device/fifo members, path escapes, duplicate members,
    and manifest drift become explicit mismatched issues instead of being
    followed or extracted. Normal directory members are structural and need no
    manifest entry. Names normalize by dropping exactly one leading ``./``,
    matching the runner's ``removeprefix('./')``. Archives that a legacy run
    never retained stay unbound.
    """
    result: dict[str, Any] = {"status": "unbound", "files": {}, "issues": []}
    archive_path = arm_path / f"{kind}-task-file.tar"
    receipt_path = arm_path / f"{kind}-task-file.receipt.json"
    manifest_path = arm_path / f"{kind}-task-file.manifest.json"
    present = [
        path
        for path in (archive_path, receipt_path, manifest_path)
        if path.exists() or path.is_symlink()
    ]
    if not present:
        return result
    if any(path.is_symlink() for path in present):
        return {**result, "status": "mismatched", "issues": [f"{kind}_archive_symlinked"]}
    if not archive_path.is_file():
        return {**result, "status": "mismatched", "issues": [f"{kind}_archive_missing"]}
    issues: list[str] = []
    if not receipt_path.is_file():
        issues.append(f"{kind}_archive_receipt_missing")
    else:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = None
        if not isinstance(receipt, dict):
            issues.append(f"{kind}_archive_receipt_invalid")
        else:
            if receipt.get("exit") != 0:
                issues.append(f"{kind}_archive_receipt_exit_nonzero")
            recorded = receipt.get("archive_sha256")
            if not _is_sha256_hex(recorded):
                issues.append(f"{kind}_archive_receipt_digest_unbound")
            elif _file_sha256_hex(archive_path) != recorded.lower().removeprefix("sha256:"):
                issues.append(f"{kind}_archive_digest_mismatch")
    manifest: dict[str, Any] | None = None
    if not manifest_path.is_file():
        issues.append(f"{kind}_archive_manifest_missing")
    else:
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            manifest = parsed
        else:
            issues.append(f"{kind}_archive_manifest_invalid")
    files: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                parts = PurePosixPath(name).parts
                if member.name.startswith("/") or name.startswith("/") or ".." in parts:
                    issues.append(f"{kind}_archive_member_escape:{name}")
                    continue
                if name in seen:
                    issues.append(f"{kind}_archive_member_duplicate:{name}")
                    continue
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isfile():
                    issues.append(f"{kind}_archive_member_unsupported:{name}")
                    continue
                entry = manifest.get(name) if manifest is not None else None
                if not isinstance(entry, Mapping):
                    issues.append(f"{kind}_archive_file_unmanifested:{name}")
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    issues.append(f"{kind}_archive_member_unreadable:{name}")
                    continue
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    size += len(chunk)
                observed = {
                    "sha256": digest.hexdigest(),
                    "size": size,
                    "uid": member.uid,
                    "gid": member.gid,
                    "mode": oct(member.mode),
                }
                files[name] = observed
                if not _archive_entry_matches(entry, observed):
                    issues.append(f"{kind}_archive_file_mismatch:{name}")
    except (tarfile.TarError, OSError, EOFError):
        issues.append(f"{kind}_archive_unreadable")
    if manifest is not None:
        issues.extend(
            f"{kind}_archive_manifest_uncovered:{name}"
            for name in sorted(manifest)
            if name not in files
        )
    status = "mismatched" if issues else "verified"
    return {
        "status": status,
        "files": files if status == "verified" else {},
        "issues": sorted(set(issues)),
    }


def _audit_ownership_identity(action: Mapping[str, Any], runtime_path: Path) -> dict[str, Any]:
    """Bind the checked action archive to the retained host copy by bytes only.

    Container uid/gid survive only inside the archives; ``docker cp`` to the
    host rewrites ownership, so host uid/gid are deliberately never runtime
    metadata. Agreement is exact file bytes and size, and the returned file map
    carries the archive's own ``{sha256,size,uid,gid,mode}`` observations.
    """
    if action["status"] == "unbound":
        return {"status": "unbound", "files": {}, "issues": []}
    issues = list(action["issues"])
    # The retention comparison is meaningful only against a fully checked map;
    # a withheld (mismatched) one must not fabricate per-file verdicts.
    if action["status"] != "verified":
        return {"status": "mismatched", "files": {}, "issues": sorted(set(issues))}
    if not runtime_path.is_dir():
        issues.append("ownership_retention_absent")
    else:
        disk_files = {
            path.relative_to(runtime_path).as_posix()
            for path in runtime_path.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        for rel, entry in sorted(action["files"].items()):
            target = runtime_path / rel
            if target.is_symlink() or not target.is_file():
                issues.append(f"ownership_retained_missing:{rel}")
            elif (
                _file_sha256_hex(target) != entry["sha256"]
                or target.stat().st_size != entry["size"]
            ):
                issues.append(f"ownership_retained_mismatch:{rel}")
        issues.extend(
            f"ownership_retained_extra:{rel}" for rel in sorted(disk_files - action["files"].keys())
        )
    status = "mismatched" if issues else "verified"
    return {
        "status": status,
        "files": dict(action["files"]) if status == "verified" else {},
        "issues": sorted(set(issues)),
    }


def _audit_transport_identity(
    action: Mapping[str, Any], verifier: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare copy-in fidelity between the two checked archives.

    The runner streams the action archive into the verifier container and
    immediately re-exports it, so equality of file bytes, ownership (uid/gid),
    and modes across the two checked maps is the retained evidence that the
    transfer changed nothing. It covers the post-action bytes only and never
    pre-action state.
    """
    if action["status"] == "unbound" and verifier["status"] == "unbound":
        return {"status": "unbound", "issues": []}
    issues: list[str] = []
    for side, identity in (("action", action), ("verifier", verifier)):
        if identity["status"] != "verified":
            issues.extend(identity["issues"])
            issues.append(f"transport_{side}_archive_{identity['status']}")
    # Per-file drift is named only between two fully checked maps; a withheld
    # (mismatched) side must not fabricate per-file change verdicts.
    if action["status"] == "verified" and verifier["status"] == "verified":
        issues.extend(
            f"transport_changed:{rel}"
            for rel in sorted(action["files"].keys() | verifier["files"].keys())
            if action["files"].get(rel) != verifier["files"].get(rel)
        )
    return {
        "status": "mismatched" if issues else "verified",
        "issues": sorted(set(issues)),
    }


def _audit_runtime_identity(arm: Mapping[str, Any] | None) -> dict[str, Any]:
    """Check complete retained runtime bytes, never the shared package snapshot.

    ``status``/``inputs``/``outputs``/``issues`` cover the retained
    ``task_file`` copy against ``task-file-manifest.json``, whose values are
    either the legacy plain sha256 mapping or the runner's rich
    ``{sha256,size,copied_mode}`` entries; host uid/gid are never runtime
    metadata, and ``copied_mode`` is checked against the retained host file
    only where the manifest records it. Inputs are the ``input/`` and
    ``inputs/`` prefixes plus the exact ``runtime_input_paths`` declared for
    the arm; every other manifest entry is an output.

    ``ownership`` reports container file identity
    ``{sha256,size,uid,gid,mode}`` from the checked post-action archive bound
    to the retained copy by bytes, and ``transport`` reports whether the
    checked verifier-side re-export (taken after copy-in, before the verifier
    started) equals it. Archives a legacy run never retained leave both
    unbound.
    """
    result: dict[str, Any] = {
        "status": "unbound",
        "inputs": {},
        "outputs": {},
        "issues": [],
        "ownership": {"status": "unbound", "files": {}, "issues": []},
        "transport": {"status": "unbound", "issues": []},
    }
    if not arm or not arm.get("evidence_path"):
        return result
    arm_path = Path(arm["evidence_path"])
    action_identity = _audit_archive_identity(arm_path, "action")
    verifier_identity = _audit_archive_identity(arm_path, "verifier")
    transport = _audit_transport_identity(action_identity, verifier_identity)
    manifest_path = arm_path / "task-file-manifest.json"
    runtime_path = arm_path / "task_file"
    if arm_path.is_symlink() or runtime_path.is_symlink() or manifest_path.is_symlink():
        return {
            **result,
            "status": "mismatched",
            "issues": ["symlinked_runtime_retention"],
            "ownership": {
                "status": "mismatched",
                "files": {},
                "issues": ["symlinked_runtime_retention"],
            },
            "transport": transport,
        }
    if not manifest_path.is_file() or not runtime_path.is_dir():
        return {
            **result,
            "ownership": _audit_ownership_identity(action_identity, runtime_path),
            "transport": transport,
        }
    try:
        manifest = _audit_json_object(manifest_path)
    except (WorkbenchError, OSError, ValueError):
        return {
            **result,
            "status": "mismatched",
            "issues": ["runtime_manifest_unreadable"],
            "ownership": _audit_ownership_identity(action_identity, runtime_path),
            "transport": transport,
            "manifest_path": str(manifest_path),
        }
    issues: list[str] = []
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}
    runtime_inputs = {
        value for value in (arm.get("runtime_input_paths") or ()) if isinstance(value, str)
    }
    for rel, expected in sorted(manifest.items()):
        relative = PurePosixPath(rel) if isinstance(rel, str) else None
        if relative is None:
            issues.append(str(rel))
            continue
        target = runtime_path / rel
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != rel
            or not target.resolve().is_relative_to(runtime_path.resolve())
        ):
            issues.append(rel)
            continue
        if isinstance(expected, str):
            if not _is_sha256_hex(expected):
                issues.append(rel)
                continue
            digest = expected.lower().removeprefix("sha256:")
            expected_size = None
            expected_mode = None
        elif isinstance(expected, Mapping):
            recorded_sha = expected.get("sha256")
            expected_size = expected.get("size")
            expected_mode = expected.get("copied_mode")
            if (
                not _is_sha256_hex(recorded_sha)
                or isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or (expected_mode is not None and not isinstance(expected_mode, str))
            ):
                issues.append(rel)
                continue
            digest = recorded_sha.lower().removeprefix("sha256:")
        else:
            issues.append(rel)
            continue
        is_input = rel.startswith("input/") or rel.startswith("inputs/") or rel in runtime_inputs
        (inputs if is_input else outputs)[rel] = digest
        retained_stat = target.stat() if not target.is_symlink() and target.is_file() else None
        if (
            retained_stat is None
            or _file_sha256_hex(target) != digest
            or (expected_size is not None and retained_stat.st_size != expected_size)
            or (
                expected_mode is not None
                and oct(stat.S_IMODE(retained_stat.st_mode)) != expected_mode
            )
        ):
            issues.append(rel)
    # Omitted files are missing coverage, not permission to compare a partial manifest.
    disk_files = {
        path.relative_to(runtime_path).as_posix()
        for path in runtime_path.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    issues.extend(sorted(disk_files - manifest.keys()))
    return {
        "status": "mismatched" if issues else "verified",
        "inputs": inputs,
        "outputs": outputs,
        "issues": sorted(set(issues)),
        "ownership": _audit_ownership_identity(action_identity, runtime_path),
        "transport": transport,
        "manifest_path": str(manifest_path),
    }


def _audit_pair_identity(
    before: Mapping[str, Any], after: Mapping[str, Any], field: str
) -> dict[str, Any]:
    left, right = before[field], after[field]
    changed = (
        sorted(key for key in left.keys() | right.keys() if left.get(key) != right.get(key))
        if before["status"] == after["status"] == "verified"
        else []
    )
    statuses = (before["status"], after["status"])
    status = (
        "mismatched"
        if "mismatched" in statuses
        else "unbound"
        if statuses != ("verified", "verified")
        else "changed"
        if changed
        else "same"
    )
    return {
        "status": status,
        "before_status": before["status"],
        "after_status": after["status"],
        "changed_paths": changed,
        "before_issues": before["issues"],
        "after_issues": after["issues"],
    }


def _audit_snapshot_identity(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Compare retained package bytes outside the explicitly varied verifier file."""
    task_dir = evidence.get("task_dir")
    status = evidence["provenance_binding"]["package_snapshot_status"]
    if not task_dir or status != "verified":
        return {"status": status, "files": {}, "issues": []}
    if evidence.get("evidence_format") == "quality_owned":
        # This exact manifest was checked by the reader. Do not re-read the
        # shared source directory as a second, potentially changed snapshot.
        recorded = evidence["provenance_binding"]["recorded_source_task_manifest"]
        return {
            "status": "verified",
            "files": {path: entry["sha256"] for path, entry in recorded.items()},
            "issues": [],
        }
    # Listed-file verification cannot promote uncovered current bytes into a
    # retained condition identity. Reuse only hashes checked against receipts.
    verified = evidence["provenance_binding"].get("verified_package_files", {})
    paths = {
        path.relative_to(task_dir).as_posix()
        for path in Path(task_dir).rglob("*")
        if (path.is_file() or path.is_symlink())
        and path.relative_to(task_dir).as_posix() != "tests/test_state.py"
    }
    uncovered = sorted(paths - verified.keys())
    return {
        "status": "partial" if uncovered else "verified",
        "files": {path: digest for path, digest in verified.items() if path in paths},
        "issues": [f"package_file_unbound:{path}" for path in uncovered],
    }


def _native_review_record(path: Path, issues: list[str]) -> dict[str, Any]:
    """Keep missing/malformed retained records explicit, never reconstruct them."""
    if not path.is_file():
        issues.append(f"record_missing:{path}")
        return {}
    try:
        return _audit_json_object(path)
    except (OSError, ValueError, WorkbenchError) as exc:
        issues.append(f"record_unreadable:{path}:{type(exc).__name__}")
        return {}


def _native_review_path(value: Any, base: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkbenchError("native review evidence paths must be nonempty strings")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _load_native_trial_evidence(trial_path: Path | None) -> dict[str, Any]:
    issues: list[str] = []
    result = _native_review_record(trial_path / "result.json", issues) if trial_path else {}
    config = result.get("config")
    config = config if isinstance(config, dict) else {}
    verifier = result.get("verifier_result")
    verifier = verifier if isinstance(verifier, dict) else {}
    rewards = verifier.get("rewards")
    reward = _owned_float(rewards.get("reward")) if isinstance(rewards, dict) else None
    exception = result.get("exception_info")
    details = (
        _native_review_record(trial_path / "verifier/reward-details.json", issues)
        if trial_path
        else {}
    )
    resolved = details.get("resolved") if result else None
    resolved = resolved if isinstance(resolved, bool) else None
    status = (
        "missing"
        if not result
        else "failed"
        if exception
        else "completed"
        if result.get("finished_at") and reward is not None
        else "uncompleted"
    )
    captured: dict[str, Any] = {}
    for phase in ("pre", "post"):
        record = (
            _native_review_record(trial_path / f"agent/quality-{phase}-action.json", issues)
            if trial_path
            else {}
        )
        files = record.get("files")
        captured[phase] = files if isinstance(files, dict) and files else None
    return {
        "trial_path": str(trial_path) if trial_path else None,
        "trial_id": result.get("id"),
        "trial_name": result.get("trial_name"),
        "job_id": config.get("job_id"),
        "execution_status": status,
        "observed_reward": reward,
        "observed_resolved": resolved,
        "diagnostics": details,
        "exception_info": exception,
        "pre_action_files": captured["pre"],
        "post_action_files": captured["post"],
        "result_sha256": _file_sha256_hex(trial_path / "result.json")
        if trial_path and (trial_path / "result.json").is_file()
        else None,
        "issues": issues,
    }


def _compare_review_result_and_repair_contract(
    review_result_path: Path,
    repair_contract_path: Path,
) -> dict[str, Any]:
    issues: list[str] = []
    review_result_path, repair_contract_path = (
        review_result_path.resolve(),
        repair_contract_path.resolve(),
    )
    review = _native_review_record(review_result_path, issues)
    contract = _native_review_record(repair_contract_path, issues)
    if issues:
        raise WorkbenchError("; ".join(issues))
    source_findings = _native_review_path(
        contract.get("source_findings"), repair_contract_path.parent
    )
    if source_findings != review_result_path:
        raise WorkbenchError("repair contract must bind this review via source_findings")
    rows = review.get("comparison")
    if not isinstance(rows, list) or not rows:
        raise WorkbenchError("native review requires a nonempty comparison array")
    expectations = contract.get("expected_rewards") or {}
    if not isinstance(expectations, dict):
        raise WorkbenchError("repair expected_rewards must be an object")
    findings = review.get("findings") or []
    if not isinstance(findings, list):
        raise WorkbenchError("review findings must be an array")
    findings_by_control = {
        item["control"]: item
        for item in findings
        if isinstance(item, dict) and isinstance(item.get("control"), str)
    }
    arms: dict[str, Any] = {}
    control_trials: list[dict[str, Any]] = []
    seen_trials: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("arm"), str) or not row["arm"]:
            raise WorkbenchError("native comparison rows require a named arm")
        name = row["arm"]
        if name in arms:
            raise WorkbenchError(f"duplicate native comparison arm: {name}")
        runtime = row.get("runtime_identities") or {}
        if not isinstance(runtime, dict):
            raise WorkbenchError(f"invalid runtime identities for {name}")
        sides: dict[str, Any] = {}
        reasons: list[str] = []
        for side in ("original", "repaired"):
            path = _native_review_path(row.get(f"{side}_trial"), review_result_path.parent)
            evidence = _load_native_trial_evidence(path)
            identity = runtime.get(side) or {}
            if not isinstance(identity, dict):
                raise WorkbenchError(f"invalid {side} runtime identity for {name}")
            evidence["declared_runtime_identity"] = identity
            if path is not None:
                key = str(path)
                if key in seen_trials:
                    raise WorkbenchError(f"trial reused across native comparison slots: {path}")
                seen_trials.add(key)
            if evidence["execution_status"] != "completed":
                reasons.append(f"{side}_execution_{evidence['execution_status']}")
            if evidence["issues"]:
                reasons.append(f"{side}_retained_records_incomplete")
            sides[side] = evidence
            control_trials.append({"arm": name, "side": side, **evidence})
        original, repaired = sides["original"], sides["repaired"]
        pre_equal = (
            original["pre_action_files"] == repaired["pre_action_files"]
            if original["pre_action_files"] and repaired["pre_action_files"]
            else None
        )
        post_equal = (
            original["post_action_files"] == repaired["post_action_files"]
            if original["post_action_files"] and repaired["post_action_files"]
            else None
        )
        for phase, equal in (("pre", pre_equal), ("post", post_equal)):
            if equal is not True:
                reasons.append(f"{phase}_action_state_{'unbound' if equal is None else 'changed'}")
        original_image = original["declared_runtime_identity"].get("image_id")
        repaired_image = repaired["declared_runtime_identity"].get("image_id")
        if not original_image or not repaired_image:
            reasons.append("image_identity_unbound")
        elif original_image != repaired_image:
            reasons.append("image_identity_changed")
        before_reward, after_reward = original["observed_reward"], repaired["observed_reward"]
        complete = all(side["execution_status"] == "completed" for side in sides.values())
        delta = after_reward - before_reward if complete else None
        arms[name] = {
            **sides,
            "observed_reward_delta": delta,
            "partial_credit": after_reward is not None
            and 0 < after_reward < 1
            and repaired["observed_resolved"] is False,
            "captured_state": {
                "declared_scope": review.get("comparison_state_scope"),
                "declared_same_state": row.get("same_captured_pre_action_and_post_action_state"),
                "pre_action_files_match": pre_equal,
                "post_action_files_match": post_equal,
                "qualification": "Equality of retained named-file records only; not full filesystem or image equality.",
            },
            "declared_interpretation": {
                "semantic_validity": row.get("semantic_validity"),
                "expected_reward": expectations.get(name),
                "finding": findings_by_control.get(name),
            },
            "pairing": {"status": "unqualified" if reasons else "matched", "reasons": reasons},
        }
    infrastructure: list[dict[str, Any]] = []
    evidence_root = _native_review_path(review.get("evidence_root"), review_result_path.parent)
    failure_manifest: dict[str, Any] = {}
    if evidence_root:
        failure_manifest = _native_review_record(
            evidence_root / "instrumentation-failures.json", issues
        )
    entries = failure_manifest.get("failures") or []
    if not isinstance(entries, list):
        raise WorkbenchError("instrumentation failures must be an array")
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkbenchError("instrumentation failure entries must be objects")
        job = _native_review_path(entry.get("job"), evidence_root or review_result_path.parent)
        trials = (
            sorted(
                child
                for child in job.iterdir()
                if child.is_dir() and (child / "result.json").is_file()
            )
            if job and job.is_dir()
            else []
        )
        for trial in trials or [None]:
            observed = _load_native_trial_evidence(trial)
            infrastructure.append(
                {
                    "job_path": str(job) if job else None,
                    "declared_failure": entry,
                    "declared_classification": failure_manifest.get("classification"),
                    "observed": observed,
                }
            )
    source = review.get("source")
    source = source if isinstance(source, dict) else {}
    return {
        "kind": "quality_native_review_comparison",
        "schema_version": SCHEMA_VERSION,
        "comparison_format": "native_review_contract",
        "review_result_path": str(review_result_path),
        "review_result_sha256": _file_sha256_hex(review_result_path),
        "repair_contract_path": str(repair_contract_path),
        "repair_contract_sha256": _file_sha256_hex(repair_contract_path),
        "experiment_axis": contract,
        "declared_disposition": review.get("disposition"),
        "source_lineage": review.get("source_lineage"),
        "populations": {
            "paired_arms_count": len(arms),
            "declared_control_slots": len(control_trials),
            "control_trials_count": sum(bool(item["trial_id"]) for item in control_trials),
            "infrastructure_failures_count": sum(
                bool(item["observed"]["exception_info"]) for item in infrastructure
            ),
            "declared_infrastructure_jobs_count": len(entries),
        },
        "reported_population_counts": {
            "native_ingested_jobs": review.get("native_ingested_jobs"),
            "native_reward_facts": review.get("native_reward_facts"),
            "preserved_instrumentation_failures": review.get("preserved_instrumentation_failures"),
        },
        "arms": arms,
        "control_trials": control_trials,
        "infrastructure_failures": infrastructure,
        "scope_limits": review.get("scope_limits") or [],
        "source_limits": source.get("limits") or [],
        "admission_authority": "candidate_evidence_only",
        "issues": issues,
        "notices": [
            "Rewards come only from original native result.json; diagnostics and declarations never fill missing rewards.",
            "Neutral deltas and unresolved partial credit are observations, not automatic improvement or semantic validity.",
            "Named state equality does not establish cross-image, whole-filesystem, causal or training equivalence.",
            "Infrastructure records are separate from paired controls; unknown rewards remain unknown.",
            "No execution, ingestion, admission or merge authority.",
        ],
    }


def compare_quality_audits(
    before_dir: Path | None = None,
    after_dir: Path | None = None,
    *,
    declaration_path: Path | None = None,
    repo_root: Path | None = None,
    source_root: Path | None = None,
    review_result_path: Path | None = None,
    repair_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Compare retained audit conditions without inferring semantic improvement."""
    if review_result_path is not None or repair_contract_path is not None:
        if review_result_path is None or repair_contract_path is None:
            raise WorkbenchError(
                "both --review-result and --repair-contract are required when using review-result mode"
            )
        if (
            before_dir is not None
            or after_dir is not None
            or declaration_path is not None
            or source_root is not None
        ):
            raise WorkbenchError(
                "invalid mixed modes: cannot combine --review-result/--repair-contract with audit directories, --declaration or --source-root"
            )
        return _compare_review_result_and_repair_contract(
            review_result_path,
            repair_contract_path,
        )

    if before_dir is None or after_dir is None:
        raise WorkbenchError(
            "must provide either before_dir and after_dir, or --review-result and --repair-contract"
        )
    before = load_quality_audit_evidence(before_dir, repo_root=repo_root, source_root=source_root)
    after = load_quality_audit_evidence(after_dir, repo_root=repo_root, source_root=source_root)
    if declaration_path is not None and not declaration_path.is_file():
        raise WorkbenchError(f"comparison declaration does not exist: {declaration_path}")
    declaration = _audit_json_object(declaration_path) if declaration_path else {}
    owned = any(run.get("evidence_format") == "quality_owned" for run in (before, after))
    declared_arms = declaration.get("arms", {})
    if not isinstance(declared_arms, dict):
        raise WorkbenchError("comparison declaration arms must be an object")
    declaration_rows = declaration.get("rows", [])
    if not isinstance(declaration_rows, list):
        raise WorkbenchError("comparison declaration rows must be an array")
    for row in declaration_rows:
        if not isinstance(row, dict) or not isinstance(row.get("arm"), str):
            raise WorkbenchError("comparison declaration row requires an arm identity")
        if row["arm"] in declared_arms:
            raise WorkbenchError(f"duplicate comparison declaration arm: {row['arm']}")
        declared_arms[row["arm"]] = {
            "expected_reward": row.get("declared_expectation_for_repaired_scope")
        }
    bindings = [run["provenance_binding"] for run in (before, after)]
    verifier_ids = [binding.get("executed_verifier_sha256") for binding in bindings]
    declared_ids = [
        declaration.get("upstream_verifier_sha256", declaration.get("verifier_before_sha256")),
        declaration.get("repaired_verifier_sha256", declaration.get("verifier_after_sha256")),
    ]
    declared_ids = [
        value.lower().removeprefix("sha256:")
        if isinstance(value, str) and _is_sha256_hex(value)
        else None
        for value in declared_ids
    ]
    verifiers_bound = all(binding["executed_verifier_status"] == "verified" for binding in bindings)
    verifier_manifests = [binding.get("executed_verifier_manifest", {}) for binding in bindings]
    changed_bundle_paths = {
        path
        for path in verifier_manifests[0].keys() | verifier_manifests[1].keys()
        if verifier_manifests[0].get(path) != verifier_manifests[1].get(path)
    }
    bundle_change_declared = True
    if owned and changed_bundle_paths - {"test_state.py"}:
        # Legacy file hashes still cover a test_state-only repair. A launcher
        # or sidecar change needs an explicit declaration of both full bundles.
        declared_manifests: list[dict[str, Any] | None] = []
        for key in ("upstream_verifier_manifest", "repaired_verifier_manifest"):
            manifest = declaration.get(key)
            declared_manifests.append(
                {
                    path: {
                        "sha256": _owned_bare_sha(entry.get("sha256")),
                        "size": entry.get("size"),
                    }
                    for path, entry in manifest.items()
                    if isinstance(entry, Mapping)
                }
                if isinstance(manifest, Mapping)
                and all(
                    isinstance(entry, Mapping) and type(entry.get("size")) is int
                    for entry in manifest.values()
                )
                else None
            )
        bundle_change_declared = declared_manifests == verifier_manifests
    axis_status = (
        "unbound"
        if not verifiers_bound
        else "unchanged"
        if verifier_ids[0] == verifier_ids[1] and not (owned and changed_bundle_paths)
        else "declared_verifier_change"
        if verifier_ids == declared_ids and bundle_change_declared
        else "undeclared_verifier_change"
    )
    reasons: list[str] = []
    for row in declaration_rows:
        for side, run_dir in (("before", before_dir), ("after", after_dir)):
            claimed = row.get(f"{side}_path")
            if (
                not isinstance(claimed, str)
                or Path(claimed).resolve() != (run_dir / row["arm"]).resolve()
            ):
                reasons.append(f"{side}_declared_evidence_path_mismatch")
    if owned and not all(run.get("evidence_format") == "quality_owned" for run in (before, after)):
        reasons.append("evidence_format_changed")
    for side, binding in zip(("before", "after"), bindings, strict=True):
        for identity in ("package_snapshot", "executed_verifier"):
            if binding[f"{identity}_status"] != "verified":
                reasons.append(f"{side}_{identity}_{binding[f'{identity}_status']}")
    if axis_status not in ("unchanged", "declared_verifier_change"):
        reasons.append("verifier_axis_unbound" if axis_status == "unbound" else axis_status)
    if declaration_path and not all(declared_ids):
        reasons.append("declared_verifier_identity_unbound")
    elif any(declared_ids) and verifier_ids != declared_ids:
        reasons.append("declared_verifier_identity_mismatch")
    package_identity = _audit_pair_identity(
        _audit_snapshot_identity(before), _audit_snapshot_identity(after), "files"
    )
    if package_identity["status"] != "same":
        reasons.append(f"package_{package_identity['status']}")
    images = [_owned_bare_sha(run["container_image"]) for run in (before, after)]
    if not all(_is_sha256_hex(image) for image in images):
        reasons.append("image_identity_unbound")
    elif images[0] != images[1]:
        reasons.append("image_identity_changed")
    if declaration.get("image_id") and any(
        image != _owned_bare_sha(declaration["image_id"]) for image in images
    ):
        reasons.append("declared_image_mismatch")
    # A declaration is an annotation, not proof of execution. Verify its retained
    # runner/instruction bytes when supplied, and keep missing coverage explicit.
    runner_status = "unbound"
    if owned:
        runner_status = "verified"
        for component in (
            "runner_binding",
            "ownership_runner_binding",
            "controls_binding",
            "runtime_environment_binding",
        ):
            records = [binding.get(component, {"status": "unbound"}) for binding in bindings]
            for side, record in zip(("before", "after"), records, strict=True):
                if record["status"] != "verified":
                    reasons.append(f"{side}_{component}_{record['status']}")
                    if component in ("runner_binding", "ownership_runner_binding"):
                        runner_status = "unbound" if record["status"] == "unbound" else "mismatched"
            value_key = (
                "executed_sha256"
                if component in ("runner_binding", "ownership_runner_binding")
                else "executed_manifest"
            )
            if all(record["status"] == "verified" for record in records) and records[0].get(
                value_key
            ) != records[1].get(value_key):
                reasons.append(f"{component}_changed")
                if component in ("runner_binding", "ownership_runner_binding"):
                    runner_status = "changed"
    elif declaration_path and _is_sha256_hex(declaration.get("runner_sha256")):
        runner_path = declaration_path.parent / "runner.py"
        runner_status = (
            "verified"
            if runner_path.is_file()
            and _file_sha256_hex(runner_path)
            == declaration["runner_sha256"].lower().removeprefix("sha256:")
            else "mismatched"
        )
    if runner_status != "verified":
        reasons.append(f"runner_identity_{runner_status}")
    instruction_sha = declaration.get("source_instruction_sha256")
    if instruction_sha:
        for side, run in (("before", before), ("after", after)):
            instruction = Path(run["task_dir"]) / "instruction.md" if run["task_dir"] else None
            if (
                not _is_sha256_hex(instruction_sha)
                or not instruction
                or not instruction.is_file()
                or _file_sha256_hex(instruction) != instruction_sha.lower().removeprefix("sha256:")
            ):
                reasons.append(f"{side}_declared_instruction_mismatch")
    arms: dict[str, Any] = {}
    names = before["arms"].keys() | after["arms"].keys() | declared_arms.keys()
    for name in sorted(names):
        left, right = before["arms"].get(name), after["arms"].get(name)
        runtime_before, runtime_after = (
            _audit_runtime_identity(left),
            _audit_runtime_identity(right),
        )
        input_identity = _audit_pair_identity(runtime_before, runtime_after, "inputs")
        output_identity = _audit_pair_identity(runtime_before, runtime_after, "outputs")
        arm_reasons = list(reasons)
        for side, arm in (("before", left), ("after", right)):
            if arm is None or arm["execution_status"] != "completed":
                arm_reasons.append(
                    f"{side}_execution_missing"
                    if arm is None
                    else f"{side}_execution_{arm['execution_status']}"
                )
        for field, identity in (("input", input_identity), ("output", output_identity)):
            if identity["status"] != "same":
                arm_reasons.append(f"runtime_{field}_{identity['status']}")
        # Docker inspect is retained evidence of the command/image used, unlike
        # names such as 'oracle'. No Docker access is performed here.
        actions = []
        for (side, arm), run in zip(
            (("before", left), ("after", right)), (before, after), strict=True
        ):
            inspect_path = (
                Path(arm["evidence_path"]) / "action-inspect.json"
                if arm and arm["evidence_path"]
                else None
            )
            inspect_rows = (
                json.loads(inspect_path.read_text())
                if inspect_path and inspect_path.is_file()
                else []
            )
            action = (
                inspect_rows[0] if isinstance(inspect_rows, list) and len(inspect_rows) == 1 else {}
            )
            if not isinstance(action, dict):
                raise WorkbenchError(f"invalid action inspection: {inspect_path}")
            args = action.get("Args")
            if args is None:
                args = []
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise WorkbenchError(f"invalid action argument vector: {inspect_path}")
            command = (
                [action["Path"], *args]
                if isinstance(action.get("Path"), str) and action["Path"]
                else None
            )
            actions.append(command)
            if command is None:
                arm_reasons.append(f"{side}_action_identity_unbound")
            action_image = action.get("Image")
            run_image = run["container_image"]
            if (
                not isinstance(action_image, str)
                or not _is_sha256_hex(action_image)
                or not _is_sha256_hex(run_image)
            ):
                arm_reasons.append(f"{side}_action_image_unbound")
            elif action_image.lower().removeprefix("sha256:") != run_image.lower().removeprefix(
                "sha256:"
            ):
                arm_reasons.append(f"{side}_action_image_mismatched")
            result = (
                _audit_json_object(Path(arm["evidence_path"]) / "result.json")
                if arm and arm["evidence_path"]
                else {}
            )
            if result.get("arm") != name:
                arm_reasons.append(f"{side}_arm_identity_unbound")
        if all(action is not None for action in actions) and actions[0] != actions[1]:
            arm_reasons.append("action_identity_changed")
        arm_declaration = declared_arms.get(name, {})
        if not isinstance(arm_declaration, dict):
            raise WorkbenchError(f"comparison declaration arm must be an object: {name}")
        expected_action = arm_declaration.get("action_command")
        if expected_action is not None and any(
            action != expected_action for action in actions if action is not None
        ):
            arm_reasons.append("declared_action_mismatch")
        rewards = [arm.get("observed_reward") if arm else None for arm in (left, right)]
        numeric_rewards = all(
            isinstance(reward, (int, float)) and math.isfinite(reward) for reward in rewards
        )
        completed = all(arm and arm["execution_status"] == "completed" for arm in (left, right))
        if not numeric_rewards:
            arm_reasons.append("reward_unavailable")
        delta = (
            cast(float, rewards[1]) - cast(float, rewards[0])
            if completed and numeric_rewards
            else None
        )
        empty_ownership = {"status": "unbound", "files": {}, "issues": []}
        filesystem_identity = _audit_pair_identity(
            runtime_before.get("ownership", empty_ownership),
            runtime_after.get("ownership", empty_ownership),
            "files",
        )
        transports = {
            "before": runtime_before.get("transport", {"status": "unbound", "issues": []}),
            "after": runtime_after.get("transport", {"status": "unbound", "issues": []}),
        }
        archive_evidence_present = any(
            runtime.get(field, empty_ownership)["status"] != "unbound"
            for runtime in (runtime_before, runtime_after)
            for field in ("ownership", "transport")
        )
        if owned or archive_evidence_present:
            if filesystem_identity["status"] != "same":
                arm_reasons.append(f"runtime_filesystem_{filesystem_identity['status']}")
            for side, transfer in transports.items():
                if transfer["status"] != "verified":
                    arm_reasons.append(f"{side}_transport_{transfer['status']}")
        side_declarations = {
            side: run.get("declaration", {}).get("arms", {}).get(name, {})
            for side, run in (("before", before), ("after", after))
        }
        for (side, arm_decl), action in zip(side_declarations.items(), actions, strict=True):
            if (
                action is not None
                and arm_decl.get("action_command") is not None
                and action != arm_decl["action_command"]
            ):
                arm_reasons.append(f"{side}_declared_action_mismatch")
        arms[name] = {
            "before": left,
            "after": right,
            "observed_reward_delta": delta,
            "input_identity": input_identity,
            "output_identity": output_identity,
            "filesystem_identity": filesystem_identity,
            "transport_identity": transports,
            "condition_declarations": side_declarations,
            "action_commands": {"before": actions[0], "after": actions[1]},
            "pairing": {
                "status": "unqualified" if arm_reasons else "matched",
                "reasons": arm_reasons,
            },
            "declaration": arm_declaration,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "quality_audit_comparison",
        "before": before,
        "after": after,
        "experiment_axis": {
            "status": axis_status,
            "before_verifier_sha256": verifier_ids[0],
            "after_verifier_sha256": verifier_ids[1],
            "before_verifier_manifest": bindings[0].get("executed_verifier_manifest"),
            "after_verifier_manifest": bindings[1].get("executed_verifier_manifest"),
        },
        "package_identity": package_identity,
        "runner_identity": runner_status,
        "condition_reasons": reasons,
        "declaration": declaration,
        "declaration_path": str(declaration_path.resolve()) if declaration_path else None,
        "arms": arms,
        "admission_authority": "candidate_evidence_only",
        "notices": [
            "Read-only retained-evidence comparison; no admission, certification, or publication.",
            "Matched means retained conditions match outside the declared verifier axis, not a causal or semantic guarantee.",
            "Runtime manifests describe extracted post-action bytes, not proof of unchanged pre-action inputs.",
            "Archive ownership/modes describe post-action and pre-verifier transport, never pre-action state.",
            "A reward decrease is an observation, not automatic improvement; declarations and reviewer judgments are separate.",
        ],
    }


# Supported task.toml surface.
# Native checks reproduce Harbor's network resolution. Unknown keys are refused
# until the corresponding checks model their effect. Some fields accept only
# the inert values in _MODELLED_CONSTRUCT_VALUES. None marks a free-form table.
_SUPPORTED_ENVIRONMENT_KEYS = frozenset(
    {
        "network_mode",
        "docker_image",
        "build_timeout_sec",
        "cpus",
        "memory_mb",
        "storage_mb",
        # Admitted for one value each; see `_MODELLED_CONSTRUCT_VALUES`.
        "os",
        "gpus",
        "mcp_servers",
        "env",
    }
)
SUPPORTED_TASK_CONFIG: dict[str, frozenset[str] | None] = {
    "": frozenset(
        {
            "schema_version",
            "artifacts",
            "task",
            "metadata",
            "agent",
            "verifier",
            "environment",
            "solution",
        }
    ),
    "task": frozenset({"name", "version", "description", "keywords", "authors"}),
    "task.authors": frozenset({"name", "email"}),
    "metadata": None,
    "agent": frozenset({"timeout_sec"}),
    "verifier": frozenset(
        {"timeout_sec", "environment_mode", "network_mode", "environment", "collect", "env"}
    ),
    # docker_image is deliberately absent here: a prebuilt verifier image would
    # bypass the tests/ build-context scan, which is the only boundary the
    # verifier image has.
    "verifier.environment": _SUPPORTED_ENVIRONMENT_KEYS - {"docker_image"},
    "environment": _SUPPORTED_ENVIRONMENT_KEYS,
    "environment.mcp_servers": frozenset({"name", "transport", "url"}),
    "verifier.collect": frozenset({"command", "service"}),
    "verifier.env": None,
    # SolutionConfig carries exactly one field, `env` (Harbor 0.21.0
    # models/task/config.py:335-336), so naming it closes the table.
    "solution": frozenset({"env"}),
}

# Notes for the constructs most likely to be reached, so the refusal explains
# itself instead of only naming a path.
_UNSUPPORTED_CONFIG_NOTES: dict[str, str] = {
    "steps": (
        "Harbor resolves a multi-step task's verifier step-first — "
        "resolve_effective_verifier_env_config returns steps[i].verifier.environment "
        "before [verifier.environment], and the phase override falls back the same way — "
        "so compliant task-level tables do not constrain a step's verifier. v1 models a "
        "single verify pass and refuses [[steps]] rather than certify one it cannot resolve"
    ),
    "multi_step_reward_strategy": "v1 models a single verify pass",
    "environment.allow_internet": (
        "the deprecated allow_internet alias is folded into network_mode by a Harbor "
        "model validator, but only when network_mode is absent from model_fields_set and "
        "allowed_hosts is None (models/task/config.py:885-892); mirroring that three-way "
        "interaction would put a second, weaker network resolver beside "
        "_effective_verifier_network. Declare network_mode explicitly instead"
    ),
    "verifier.environment.docker_image": (
        "a prebuilt verifier image is never built from tests/, so the build-context scan "
        "that is the verifier image's only network boundary would never see it"
    ),
}


@dataclass(frozen=True)
class _ModelledValue:
    """The single value shape v1 reproduces for an admitted key, and why.

    The module's rule is that a key arrives with the check that models it. For
    the F-06 keys the modelled shape is the inert one: Harbor reads each of them
    behind an emptiness or default test, so the accepted value is provably
    equivalent to omitting the key, while any other value reaches a Harbor code
    path v1 does not reproduce. `accepts` is that equivalence, and `note` records
    the Harbor behaviour that makes anything else a refusal.
    """

    accepts: Callable[[Any], bool]
    note: str


def _is_empty_array(value: Any) -> bool:
    return isinstance(value, list) and not value


def _is_empty_table(value: Any) -> bool:
    return isinstance(value, Mapping) and not value


def _is_default_task_os(value: Any) -> bool:
    # Harbor lowercases but does not strip (EnvironmentConfig.normalize_os), so
    # the accepted spelling is matched the same way.
    return isinstance(value, str) and value.lower() == "linux"


def _is_zero_gpus(value: Any) -> bool:
    # `True` is an int in Python and `gpus = true` is not a resource request.
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


_EMPTY_ENV_NOTE = (
    "v1 models only an empty env table. Harbor resolves every entry against the *host* "
    "environment at runtime — resolve_env_vars substitutes ${VAR} from os.environ and "
    "raises when it is unset (utils/env.py:94-130) — so a populated table makes the "
    "container's environment a function of the workstation, which defeats determinism, "
    "and is the documented channel for API keys (verifier/verifier.py:166-171 warns about "
    "exactly that, and trial/trial.py:778-813 scrubs their resolved values out of the job "
    "directory afterwards). An empty table is provably inert: every consumer reads it "
    "behind a truthiness test"
)
_MODELLED_ENVIRONMENT_VALUES: dict[str, _ModelledValue] = {
    "os": _ModelledValue(
        accepts=_is_default_task_os,
        note=(
            "v1/v2 models only os = 'linux', which is the TaskOS default and therefore "
            "identical to omitting the key. os = 'windows' is refused because Harbor "
            "cannot enforce this workbench's isolation claim there: DockerEnvironment "
            "raises 'Docker network allowlist and dynamic network policy are only "
            "supported for Linux containers' whenever egress control is required "
            "(environments/docker/docker.py:218-222), and egress control is required by "
            "every network_mode other than public "
            "(environments/docker/docker.py:265-275). A Windows task also switches the "
            "file-transfer and exec platform and the artifact convention source, none of "
            "which v1/v2 reproduces"
        ),
    ),
    "gpus": _ModelledValue(
        accepts=_is_zero_gpus,
        note=(
            "v1/v2 models only gpus = 0, which Harbor folds to the same 0 as omitting the "
            "key (environments/base.py:367-369). A nonzero request cannot run under the "
            "local controls the workbench executes at all — DockerEnvironment leaves "
            "EnvironmentCapabilities.gpus at False, so Harbor raises 'Task requires N "
            "GPU(s) but docker environment does not support GPU allocation' "
            "(environments/base.py:745-750) — and the GPU-capable environments it steers "
            "to are cloud providers whose isolation v1/v2 does not model"
        ),
    ),
    "env": _ModelledValue(accepts=_is_empty_table, note=_EMPTY_ENV_NOTE),
}
# Keyed by the dotted spec `_scan_supported_table` computes, so one entry covers
# a key wherever it is reachable. `[verifier.environment]` reuses
# `EnvironmentConfig` verbatim, so it inherits the same value models.
_MODELLED_CONSTRUCT_VALUES: dict[str, _ModelledValue] = {
    **{f"environment.{key}": model for key, model in _MODELLED_ENVIRONMENT_VALUES.items()},
    **{f"verifier.environment.{key}": model for key, model in _MODELLED_ENVIRONMENT_VALUES.items()},
    "verifier.environment.mcp_servers": _ModelledValue(
        accepts=_is_empty_array,
        note=(
            "verifier environment may not declare MCP servers; verifier executes in isolation "
            "(harbor/trial/trial.py:648-650)"
        ),
    ),
    "solution.env": _ModelledValue(accepts=_is_empty_table, note=_EMPTY_ENV_NOTE),
}

_COLLECT_GUARD_CP_PATTERN = re.compile(
    r"^if\s+\[\s*!\s*-f\s+(?P<dst_guard>/[^\s\]]+)\s*\]\s*&&\s*\[\s*-f\s+(?P<src_guard>/[^\s\]]+)\s*\]\s*;\s*then\s+cp\s+(?:-f\s+)?(?P<src>/[^\s;]+)\s+(?P<dst>/[^\s;]+)\s*;\s*fi$",
    re.IGNORECASE,
)
_COLLECT_SRC_GUARD_CP_PATTERN = re.compile(
    r"^if\s+\[\s*-f\s+(?P<src_guard>/[^\s\]]+)\s*\]\s*;\s*then\s+cp\s+(?:-f\s+)?(?P<src>/[^\s;]+)\s+(?P<dst>/[^\s;]+)\s*;\s*fi$",
    re.IGNORECASE,
)
_COLLECT_TEST_CP_PATTERN = re.compile(
    r"^test\s+-f\s+(?P<src_guard>/[^\s&]+)\s*&&\s*cp\s+(?:-f\s+)?(?P<src>/[^\s]+)\s+(?P<dst>/[^\s]+)$",
    re.IGNORECASE,
)
_COLLECT_PLAIN_CP_PATTERN = re.compile(
    r"^cp\s+(?:-f\s+)?(?P<src>/[^\s]+)\s+(?P<dst>/[^\s]+)$",
    re.IGNORECASE,
)

NETWORK_SCRIPT_PATTERN = re.compile(
    r"(?:https?://|\bcurl\b|\bwget\b|\bapt(?:-get)?\b|\bpip(?:3)?\s+install\b|"
    r"\buvx\b|\bnpm\s+(?:install|ci)\b|\byarn\s+install\b|\bgit\s+clone\b|"
    r"\b(?:ssh|scp|nc|ncat|netcat|telnet)\b|\bsocket\.(?:socket|create_connection)\b|"
    r"\burllib\.|\brequests\.|\bhttpx\.|\baiohttp\.)",
    re.IGNORECASE,
)
NONDETERMINISM_PATTERN = re.compile(
    r"(?:\brandom\.|\bsecrets\.|\buuid\.uuid4\b|\btime\.time\b|"
    r"\bdatetime\.now\b|/dev/(?:u?random)|\bdate\s+\+)",
    re.IGNORECASE,
)
_MISSING = object()


def _unsupported_configuration(location: str, note: str | None = None) -> Diagnostic:
    note = note or _UNSUPPORTED_CONFIG_NOTES.get(location)
    detail = f"; {note}" if note else ""
    return _diag(
        "unsupported_task_configuration",
        "task.toml",
        f"{location} is outside the task.toml surface workbench {WORKBENCH_VERSION} models, "
        f"so isolation cannot be proven for this task{detail}. This is a limitation of the "
        "workbench, not necessarily a defect in the task: a construct v1 does not reproduce "
        "is refused rather than silently certified",
        classification="harness_defect",
    )


def _validate_supported_configuration(
    config: Mapping[str, Any], diagnostics: list[Diagnostic]
) -> None:
    """Refuse every task.toml construct this workbench version does not model.

    This is the fail-closed counterpart to `_effective_verifier_network`. That
    function reproduces Harbor's resolution for the constructs in
    `SUPPORTED_TASK_CONFIG`; anything else reaches Harbor unexamined, and an
    unexamined construct that changes the effective network policy turns a
    packet into a false green. Refusing an honest task the workbench cannot
    reason about is the cheaper error.
    """
    _scan_supported_table(config, "", "", diagnostics)


def _scan_supported_table(
    table: Mapping[str, Any], spec: str, location: str, diagnostics: list[Diagnostic]
) -> None:
    allowed = SUPPORTED_TASK_CONFIG[spec]
    if allowed is None:
        return
    for key in sorted(table):
        child_spec = f"{spec}.{key}" if spec else key
        child_location = f"{location}.{key}" if location else key
        if key not in allowed:
            diagnostics.append(_unsupported_configuration(child_location))
            continue
        if key in {"mcp_servers", "collect"} and isinstance(table[key], Mapping):
            diagnostics.append(_unsupported_configuration(child_location))
            continue
        model = _MODELLED_CONSTRUCT_VALUES.get(child_spec)
        if model is not None:
            # A key admitted for one value is decided by that value and never
            # descended into: the accepted shape is empty or scalar by
            # construction, so there is nothing below it, and descending would
            # let an unmodelled child pass under an allowlisted parent.
            if not model.accepts(table[key]):
                diagnostics.append(_unsupported_configuration(child_location, model.note))
            continue
        _scan_supported_value(table[key], child_spec, child_location, diagnostics)


def _scan_supported_value(
    value: Any, spec: str, location: str, diagnostics: list[Diagnostic]
) -> None:
    """Descend into tables and arrays of tables, naming the exact offending path."""
    if isinstance(value, Mapping):
        if SUPPORTED_TASK_CONFIG.get(spec, _MISSING) is _MISSING:
            # An allowlisted scalar key that arrived as a table is a shape the
            # workbench never modelled either.
            diagnostics.append(_unsupported_configuration(location))
            return
        _scan_supported_table(value, spec, location, diagnostics)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, Mapping | list):
                _scan_supported_value(item, spec, f"{location}[{index}]", diagnostics)


def _validate_layout(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    required = (
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    )
    for relative in required:
        if not (task_dir / relative).is_file():
            diagnostics.append(_diag("required_file_missing", relative, "required file is missing"))

    for path in sorted(task_dir.rglob("*")):
        if not path.is_symlink():
            continue
        relative = path.relative_to(task_dir).as_posix()
        diagnostics.append(
            _diag(
                "symlink_unsupported",
                relative,
                "candidate packages may not contain symlinks; Harbor build context bytes "
                "must be regular files",
            )
        )
        if not _is_under(path, task_dir):
            diagnostics.append(
                _diag("path_escape", relative, "symlink resolves outside the candidate package")
            )

    for relative in ("solution/solve.sh", "tests/test.sh"):
        path = task_dir / relative
        if path.is_file() and not os.access(path, os.X_OK):
            diagnostics.append(
                _diag("script_not_executable", relative, "script must be executable")
            )
    if (task_dir / ".gitignore").exists():
        diagnostics.append(
            _diag(
                "custom_package_ignore_unsupported",
                ".gitignore",
                "v1 cannot bind Harbor task digests with custom package ignore rules",
            )
        )


def _docker_copy_sources(arguments: str) -> tuple[list[str], bool]:
    """Parse COPY/ADD sources and fail closed on unsupported dynamic syntax."""
    payload = arguments.strip()
    while payload.startswith("--"):
        match = re.match(r"--[^\s]+\s+", payload)
        if match is None:
            return [], True
        payload = payload[match.end() :].lstrip()
    if payload.startswith("["):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return [], True
        if (
            not isinstance(value, list)
            or len(value) < 2
            or any(not isinstance(item, str) for item in value)
        ):
            return [], True
        return list(value[:-1]), False
    try:
        tokens = shlex.split(payload)
    except ValueError:
        return [], True
    if len(tokens) < 2:
        return [], True
    return tokens[:-1], False


def _validate_mcp_servers(
    config: Mapping[str, Any],
    sidecar_name: Sequence[str] | str | None,
    diagnostics: list[Diagnostic],
) -> list[dict[str, Any]]:
    environment = config.get("environment")
    if not isinstance(environment, Mapping):
        return []
    raw_mcp = environment.get("mcp_servers")
    if raw_mcp is None or raw_mcp == []:
        return []
    if not isinstance(raw_mcp, Sequence) or isinstance(raw_mcp, (str, bytes)):
        diagnostics.append(
            _diag(
                "mcp_servers_invalid",
                "task.toml",
                "[environment].mcp_servers must be a list of server tables",
            )
        )
        return []
    servers: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_mcp):
        location = f"environment.mcp_servers[{idx}]"
        if not isinstance(item, Mapping):
            diagnostics.append(
                _diag("mcp_servers_invalid", "task.toml", f"{location} must be a table")
            )
            continue
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]*", name, re.IGNORECASE
        ):
            diagnostics.append(
                _diag(
                    "mcp_servers_invalid", "task.toml", f"{location}.name must be a safe identifier"
                )
            )
        transport = item.get("transport")
        if transport != "streamable-http":
            diagnostics.append(
                _diag(
                    "mcp_transport_unsupported",
                    "task.toml",
                    f"{location}.transport is {transport!r}; only 'streamable-http' is supported in this v2 slice (stdio/SSE rejected)",
                )
            )
        url_str = item.get("url")
        if not isinstance(url_str, str) or not url_str.strip():
            diagnostics.append(
                _diag(
                    "mcp_url_invalid", "task.toml", f"{location}.url must be a non-empty URL string"
                )
            )
            continue
        try:
            parsed = urllib.parse.urlparse(url_str)
        except Exception:
            diagnostics.append(
                _diag(
                    "mcp_url_invalid", "task.toml", f"{location}.url {url_str!r} cannot be parsed"
                )
            )
            continue
        if parsed.scheme.lower() != "http":
            diagnostics.append(
                _diag(
                    "mcp_url_scheme_invalid",
                    "task.toml",
                    f"{location}.url scheme is {parsed.scheme!r}; only 'http' is supported for local MCP sidecars",
                )
            )
        if parsed.username or parsed.password:
            diagnostics.append(
                _diag(
                    "mcp_url_auth_invalid",
                    "task.toml",
                    f"{location}.url may not contain embedded credentials",
                )
            )
        host = parsed.hostname
        if (
            not host
            or host.lower() in {"localhost", "127.0.0.1", "::1"}
            or re.match(r"^\d+\.\d+\.\d+\.\d+$", host)
        ):
            diagnostics.append(
                _diag(
                    "mcp_server_host_invalid",
                    "task.toml",
                    f"{location}.url host {host!r} is invalid; must name a declared local Compose service, not localhost or IP",
                )
            )
        elif sidecar_name is None or (
            host != sidecar_name if isinstance(sidecar_name, str) else host not in set(sidecar_name)
        ):
            target_desc = (
                repr(sidecar_name)
                if isinstance(sidecar_name, str)
                else str(list(sidecar_name or []))
            )
            diagnostics.append(
                _diag(
                    "mcp_server_unbound",
                    "task.toml",
                    f"{location}.url host {host!r} does not match declared task Compose sidecar service {target_desc}",
                )
            )
        if parsed.port is None or not (1 <= parsed.port <= 65535):
            diagnostics.append(
                _diag(
                    "mcp_url_port_missing",
                    "task.toml",
                    f"{location}.url must specify an explicit port (1-65535)",
                )
            )
        if not parsed.path or parsed.path in {"", "/"}:
            diagnostics.append(
                _diag(
                    "mcp_url_path_missing",
                    "task.toml",
                    f"{location}.url must specify an explicit non-empty endpoint path",
                )
            )
        if parsed.query or parsed.fragment:
            diagnostics.append(
                _diag(
                    "mcp_url_invalid",
                    "task.toml",
                    f"{location}.url may not contain query parameters or fragments",
                )
            )
        servers.append(
            {
                "name": str(name),
                "transport": str(transport),
                "url": str(url_str),
                "host": host,
                "port": parsed.port,
                "path": parsed.path,
            }
        )
    return servers


def _validate_verifier_collect(
    config: Mapping[str, Any],
    artifacts: list[str],
    diagnostics: list[Diagnostic],
) -> list[dict[str, str]]:
    verifier = config.get("verifier")
    if not isinstance(verifier, Mapping):
        return []
    raw_collect = verifier.get("collect")
    if raw_collect is None or raw_collect == []:
        return []
    if not isinstance(raw_collect, Sequence) or isinstance(raw_collect, (str, bytes)):
        diagnostics.append(
            _diag(
                "collect_hooks_invalid",
                "task.toml",
                "[verifier.collect] must be a list of hook tables",
            )
        )
        return []
    hooks: list[dict[str, str]] = []
    artifacts_set = set(artifacts)
    for idx, item in enumerate(raw_collect):
        location = f"verifier.collect[{idx}]"
        if not isinstance(item, Mapping):
            diagnostics.append(
                _diag("collect_hooks_invalid", "task.toml", f"{location} must be a table")
            )
            continue
        service = item.get("service")
        if service != "main":
            diagnostics.append(
                _diag(
                    "collect_service_invalid",
                    "task.toml",
                    f"{location}.service is {service!r}; only fixed service 'main' is permitted",
                )
            )
        cmd_raw = item.get("command")
        if not isinstance(cmd_raw, str) or not cmd_raw.strip():
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command must be a non-empty string",
                )
            )
            continue
        command = cmd_raw.strip()
        m = (
            _COLLECT_GUARD_CP_PATTERN.match(command)
            or _COLLECT_SRC_GUARD_CP_PATTERN.match(command)
            or _COLLECT_TEST_CP_PATTERN.match(command)
            or _COLLECT_PLAIN_CP_PATTERN.match(command)
        )
        if not m:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command {command!r} does not match deterministic copy-hook grammar",
                )
            )
            continue
        groups = m.groupdict()
        src = groups.get("src", "")
        dst = groups.get("dst", "")
        dst_guard = groups.get("dst_guard")
        src_guard = groups.get("src_guard")
        if dst_guard is not None and dst_guard != dst:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination guard {dst_guard!r} does not match destination {dst!r}",
                )
            )
            continue
        if src_guard is not None and src_guard != src:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source guard {src_guard!r} does not match source {src!r}",
                )
            )
            continue
        pure_src = PurePosixPath(src)
        pure_dst = PurePosixPath(dst)
        safe_path = re.compile(r"^/[A-Za-z0-9._/-]+$")
        if (
            not safe_path.fullmatch(src)
            or not pure_src.is_absolute()
            or ".." in pure_src.parts
            or "//" in src
        ):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source path {src!r} must be an absolute normalized literal path",
                )
            )
            continue
        if (
            not safe_path.fullmatch(dst)
            or not pure_dst.is_absolute()
            or ".." in pure_dst.parts
            or "//" in dst
        ):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination path {dst!r} must be an absolute normalized literal path",
                )
            )
            continue
        if any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure_src.parts):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source {src!r} exposes hidden task component",
                )
            )
            continue
        if dst not in artifacts_set:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination {dst!r} is not declared in artifacts {sorted(artifacts_set)}",
                )
            )
            continue
        hooks.append({"service": "main", "command": command, "src": src, "dst": dst})
    return hooks


def _validate_verifier_env(
    config: Mapping[str, Any],
    source: CandidateSource,
    diagnostics: list[Diagnostic],
) -> list[str]:
    verifier = config.get("verifier")
    if not isinstance(verifier, Mapping):
        return []
    v_env = verifier.get("env")
    if v_env is None or v_env == {}:
        return []
    if not isinstance(v_env, Mapping):
        diagnostics.append(
            _diag(
                "verifier_env_invalid",
                "task.toml",
                "[verifier.env] must be a table of environment variables",
            )
        )
        return []
    allowed_credentials = set(source.credentials)
    validated_vars: list[str] = []
    for key, value in sorted(v_env.items()):
        location = f"verifier.env.{key}"
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            diagnostics.append(
                _diag("verifier_env_invalid", "task.toml", f"{location} variable name is invalid")
            )
            continue
        if not isinstance(value, str):
            diagnostics.append(
                _diag(
                    "verifier_env_literal_secret",
                    "task.toml",
                    f"{location} value must be a placeholder string",
                )
            )
            continue
        match = re.fullmatch(r"^\$\{([A-Za-z0-9_]+)\}$|^\$([A-Za-z0-9_]+)$", value.strip())
        if not match:
            diagnostics.append(
                _diag(
                    "verifier_env_literal_secret",
                    "task.toml",
                    f"{location} contains a literal value or complex interpolation {value!r}; only simple placeholders '${{VAR}}' are allowed",
                )
            )
            continue
        var_ref = match.group(1) or match.group(2)
        if var_ref not in allowed_credentials:
            diagnostics.append(
                _diag(
                    "verifier_credential_unauthorized",
                    "task.toml",
                    f"{location} references credential {var_ref!r} which is not declared in source metadata credentials {sorted(allowed_credentials)}",
                )
            )
            continue
        if key != var_ref:
            diagnostics.append(
                _diag(
                    "verifier_credential_alias_unsupported",
                    "task.toml",
                    f"{location} must use the same declared credential name on both sides of the placeholder",
                )
            )
            continue
        validated_vars.append(key)
    env_cfg = config.get("environment")
    if isinstance(env_cfg, Mapping):
        agent_env = env_cfg.get("env")
        if isinstance(agent_env, Mapping) and agent_env:
            diagnostics.append(
                _diag(
                    "agent_env_unsupported",
                    "task.toml",
                    "[environment].env must remain empty; credentials may not propagate to agent",
                )
            )
    sol_cfg = config.get("solution")
    if isinstance(sol_cfg, Mapping):
        sol_env = sol_cfg.get("env")
        if isinstance(sol_env, Mapping) and sol_env:
            diagnostics.append(
                _diag(
                    "solution_env_unsupported",
                    "task.toml",
                    "[solution].env must remain empty; credentials may not propagate to solution",
                )
            )
    return validated_vars


def _is_remote_docker_source(source: str) -> bool:
    normalized = source.strip().lower()
    return bool(
        re.match(r"^(?:https?|ftp|git|ssh)://", normalized) or normalized.startswith("git@")
    )


def _validate_dockerfile(
    task_dir: Path, diagnostics: list[Diagnostic], has_proof: bool = False
) -> str | None:
    path = task_dir / "environment/Dockerfile"
    if not path.is_file():
        return None
    text = _read_text(path)
    _validate_build_network(text, "environment/Dockerfile", diagnostics, has_proof=has_proof)
    base_ref: str | None = None
    for line in _docker_logical_lines(text):
        if line.startswith("#"):
            continue
        if re.match(r"(?i)^FROM\s+", line):
            parts = shlex.split(line)
            references = [
                part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
            ]
            if references:
                base_ref = references[0]
                if base_ref != "scratch" and not re.search(r"@sha256:[0-9a-f]{64}$", base_ref):
                    diagnostics.append(
                        _diag(
                            "base_image_unpinned",
                            "environment/Dockerfile",
                            "every FROM image must be pinned by @sha256 digest",
                        )
                    )
        copy_match = re.match(r"(?i)^(COPY|ADD)\s+(.+)$", line)
        if copy_match:
            instruction = copy_match.group(1).upper()
            sources, unsupported = _docker_copy_sources(copy_match.group(2))
            if unsupported:
                diagnostics.append(
                    _diag(
                        "agent_image_copy_unsupported",
                        "environment/Dockerfile",
                        "COPY/ADD syntax must be statically resolvable",
                    )
                )
            for source in sources:
                if instruction == "ADD" and _is_remote_docker_source(source):
                    diagnostics.append(
                        _diag(
                            "remote_docker_add",
                            "environment/Dockerfile",
                            "remote Docker ADD sources are forbidden even when checksummed",
                        )
                    )
                pure = PurePosixPath(source)
                if (
                    source in {".", "./"}
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or bool(re.search(r"[$*?\[\]{}]", source))
                    or any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure.parts)
                ):
                    diagnostics.append(
                        _diag(
                            "agent_image_hidden_leak",
                            "environment/Dockerfile",
                            "COPY/ADD may not include hidden components, '.', or escaping paths",
                        )
                    )

    normalized = text.replace("\\\n", " ")
    if not has_proof:
        for match in re.finditer(r"\bpip(?:3)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE):
            try:
                dependencies = [
                    item for item in shlex.split(match.group(1)) if not item.startswith("-")
                ]
            except ValueError:
                dependencies = []
            for dependency in dependencies:
                if "==" not in dependency and "@" not in dependency:
                    diagnostics.append(
                        _diag(
                            "dependency_unpinned",
                            "environment/Dockerfile",
                            "pip dependencies must use exact == or immutable @ pins",
                        )
                    )
                    break
        for match in re.finditer(
            r"\bapt(?:-get)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE
        ):
            try:
                dependencies = [
                    item for item in shlex.split(match.group(1)) if not item.startswith("-")
                ]
            except ValueError:
                dependencies = []
            for dependency in dependencies:
                if dependency in {"install"}:
                    continue
                if "=" not in dependency:
                    diagnostics.append(
                        _diag(
                            "dependency_unpinned",
                            "environment/Dockerfile",
                            "apt dependencies must use exact package=version pins",
                        )
                    )
                    break
    return base_ref


def _validate_verifier_image(
    task_dir: Path, diagnostics: list[Diagnostic], has_proof: bool = False
) -> None:
    path = task_dir / "tests/Dockerfile"
    if not path.is_file():
        return
    text = _read_text(path)
    _validate_build_network(text, "tests/Dockerfile", diagnostics, has_proof=has_proof)
    found_from = False
    for line in _docker_logical_lines(text):
        if line.startswith("#") or not re.match(r"(?i)^FROM\s+", line):
            continue
        found_from = True
        parts = shlex.split(line)
        references = [
            part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
        ]
        if (
            references
            and references[0] != "scratch"
            and not re.search(r"@sha256:[0-9a-f]{64}$", references[0])
        ):
            diagnostics.append(
                _diag(
                    "verifier_image_unpinned",
                    "tests/Dockerfile",
                    "the separate verifier FROM image must be pinned by @sha256 digest",
                )
            )
    if not found_from:
        diagnostics.append(
            _diag(
                "verifier_image_invalid",
                "tests/Dockerfile",
                "separate verifier Dockerfile must declare a FROM image",
            )
        )


def _effective_verifier_network(config: Mapping[str, Any]) -> tuple[str, str, str]:
    """Resolve the verifier's effective network modes the way Harbor 0.21.0 does.

    Harbor discards `extra_docker_compose` when it builds the separate verifier
    runtime config, so the workbench's no-network overlay never reaches the
    verifier container. Its exposure is therefore whatever `task.toml` declares:

    * baseline (applies from container start) — `[verifier.environment]` when that
      table exists, otherwise a deep copy of `[environment]`. `EnvironmentConfig`
      defaults `network_mode` to `public`, so a table that omits the key does not
      inherit the other table's value.
    * phase (applies during `verify()`) — the explicit `[verifier].network_mode`
      override when set, otherwise the baseline.

    This is a mirror, not a call: Harbor ships as a standalone CLI tool rather
    than a library this package imports, so the resolver cannot be reused in
    process. The mirrored symbols are
    `harbor.models.task.verifier_mode.resolve_effective_verifier_env_config`,
    `harbor.models.task.config.BaselineNetworkPolicyConfig.resolve_baseline`, and
    `harbor.trial.network_policy.resolve_verifier_phase_policy` (Harbor 0.21.0).
    `test_verifier_network_resolution_matches_harbor` executes those three
    against the installed Harbor and fails if this mirror drifts from them.

    Both this function and Harbor's resolver take a step argument that this one
    omits, because `_validate_supported_configuration` refuses `[[steps]]`
    outright: Harbor resolves step-first, and v1 does not model a multi-step
    trial. The mirror is only valid for the step-less case it is given.

    Returns `(baseline, phase, baseline_origin)`.
    """
    environment = config.get("environment")
    environment_table = environment if isinstance(environment, Mapping) else {}
    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, Mapping) else {}
    verifier_environment = verifier_table.get("environment")
    if isinstance(verifier_environment, Mapping):
        declared = verifier_environment.get("network_mode")
        origin = "[verifier.environment]"
    else:
        declared = environment_table.get("network_mode")
        origin = "[environment] (inherited)"
    baseline = declared if isinstance(declared, str) else "public"
    override = verifier_table.get("network_mode")
    phase = override if isinstance(override, str) else baseline
    return baseline, phase, origin


def _validate_network_and_isolation(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> None:
    environment = config.get("environment")
    environment_table = environment if isinstance(environment, dict) else {}
    network_mode = environment_table.get("network_mode")
    if network_mode not in {"no-network", "public", "allowlist"}:
        diagnostics.append(
            _diag(
                "network_policy_invalid",
                "task.toml",
                "[environment].network_mode must be explicit and supported",
            )
        )
    # A declared [environment].docker_image makes Harbor skip the build entirely
    # (should_use_prebuilt_docker_image returns True whenever it is set), so the
    # reviewed environment/Dockerfile is never built and the overlay's
    # build.network=none never applies. Refuse rather than certify an image the
    # workbench did not see built.
    if environment_table.get("docker_image") is not None:
        diagnostics.append(
            _diag(
                "prebuilt_image_unsupported",
                "task.toml",
                "[environment].docker_image bypasses the reviewed environment/Dockerfile "
                "build and the workbench's build-time network denial",
            )
        )
    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, dict) else {}
    if verifier_table.get("environment_mode") != "separate":
        diagnostics.append(
            _diag(
                "verifier_not_isolated",
                "task.toml",
                "[verifier].environment_mode must be 'separate'",
            )
        )
    declared_modes = [
        value
        for value in (
            verifier_table.get("network_mode"),
            verifier_table.get("environment", {}).get("network_mode")
            if isinstance(verifier_table.get("environment"), Mapping)
            else None,
        )
        if value is not None
    ]
    if any(value not in {"no-network", "public", "allowlist"} for value in declared_modes):
        diagnostics.append(
            _diag(
                "verifier_network_invalid",
                "task.toml",
                "verifier network_mode is invalid",
            )
        )
    baseline, _phase, origin = _effective_verifier_network(config)
    if baseline != "no-network":
        diagnostics.append(
            _diag(
                "verifier_network_not_isolated",
                "task.toml",
                "the separate verifier environment must declare network_mode='no-network'; "
                f"the effective baseline is '{baseline}' from {origin}. Harbor drops the "
                "workbench no-network overlay for the verifier container, so a networked "
                "verifier can exfiltrate hidden inputs and can make verifier_deterministic "
                "an artifact of a stable remote response",
            )
        )
    phase_override = verifier_table.get("network_mode")
    if isinstance(phase_override, str) and phase_override != "no-network":
        diagnostics.append(
            _diag(
                "verifier_phase_network_not_isolated",
                "task.toml",
                "[verifier].network_mode reopens the network for the verification phase; "
                f"the effective phase policy is '{phase_override}' and must be 'no-network'",
            )
        )
    # Task-authored Compose files are refused by `_validate_build_context_contents`
    # for every build context, not by exact path here: the same file under
    # `tests/` reaches the separate verifier's build and `up`, and refusing only
    # `environment/docker-compose.yaml` left that open.

    for root_name in ("tests", "verifier", "solution"):
        root = task_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            text = _read_text(path)
            relative = path.relative_to(task_dir).as_posix()
            if any(
                NETWORK_SCRIPT_PATTERN.search(line) and not _is_proven_offline_install(line)
                for line in _docker_logical_lines(text)
            ):
                diagnostics.append(
                    _diag(
                        "runtime_network_use",
                        relative,
                        "control/verifier scripts may not fetch or install over "
                        "the network at runtime",
                    )
                )
            if root_name in {"tests", "verifier"} and NONDETERMINISM_PATTERN.search(text):
                diagnostics.append(
                    _diag(
                        "verifier_nondeterminism_static",
                        relative,
                        "verifier references a nondeterministic clock/random source",
                    )
                )


def _sensitive_lines(task_dir: Path) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    roots = (task_dir / "solution", task_dir / "tests", task_dir / "verifier")
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in {
                "Dockerfile",
                "test.sh",
                "evaluate.py",
                "build-proof.json",
                "offline-build-proof.json",
                "requirements.txt",
            }:
                # Verifier-image plumbing and build proofs are not golden task content.
                continue
            if path.suffix in {".whl", ".tar.gz", ".zip"}:
                continue
            relative = path.relative_to(task_dir).as_posix()
            text = _read_text(path)
            if "fixtures" not in path.relative_to(root).parts:
                for line in text.splitlines():
                    normalized = " ".join(line.strip().split())
                    if (
                        len(normalized) >= 32
                        and not normalized.startswith(("#", "//", "/*", "*"))
                        and re.search(r"[A-Za-z]", normalized)
                    ):
                        candidates.append((relative, normalized))
            if any(token in path.name.lower() for token in ("golden", "expected", "answer")):
                compact = " ".join(text.split())
                if len(compact) >= 12:
                    candidates.append((relative, compact))
    return candidates


def _validate_golden_leak(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    visible_paths: list[Path] = []
    for relative in ("instruction.md", "instructions.md"):
        path = task_dir / relative
        if path.is_file():
            visible_paths.append(path)
    environment = task_dir / "environment"
    if environment.exists():
        visible_paths.extend(
            path
            for path in sorted(environment.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    visible = "\n".join(" ".join(_read_text(path).split()) for path in visible_paths)
    for source_path, span in _sensitive_lines(task_dir):
        if span and span in visible:
            diagnostics.append(
                _diag(
                    "golden_data_leak",
                    source_path,
                    "a hidden solution/verifier span is present in agent-visible task bytes",
                )
            )
            break


def _validate_test_contract(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    test_script = task_dir / "tests/test.sh"
    if test_script.is_file():
        text = _read_text(test_script)
        verifier_text = "\n".join(
            _read_text(path)
            for path in sorted((task_dir / "tests").rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        writes_reward = "/logs/verifier/reward" in verifier_text or (
            "/logs/verifier" in verifier_text
            and ("reward.json" in verifier_text or "reward.txt" in verifier_text)
        )
        if not writes_reward:
            diagnostics.append(
                _diag(
                    "verifier_reward_missing",
                    "tests/test.sh",
                    "verifier must write an absolute /logs/verifier/reward output",
                )
            )
        if "/tests/" not in text:
            diagnostics.append(
                _diag(
                    "verifier_path_relative",
                    "tests/test.sh",
                    "verifier entrypoint must use an absolute /tests path",
                )
            )
    instruction = task_dir / "instruction.md"
    if instruction.is_file() and not instruction.read_text(encoding="utf-8").strip():
        diagnostics.append(_diag("instruction_empty", "instruction.md", "instruction is empty"))


def check_package(
    task_dir: Path, *, source: CandidateSource | None = None
) -> tuple[Diagnostic, ...]:
    """Screen native package metadata and security without running controls.

    A clean result is not a Gold certificate or permission to publish.
    Source credentials are names only; no credential values are read.
    """
    task_dir = task_dir.resolve()
    if not task_dir.is_dir():
        raise WorkbenchError(f"task directory is missing: {task_dir}")
    diagnostics: list[Diagnostic] = []
    _validate_layout(task_dir, diagnostics)
    if any(item.code in {"symlink_unsupported", "path_escape"} for item in diagnostics):
        return tuple(diagnostics)
    config = _parse_task_toml(task_dir / "task.toml", diagnostics)
    _validate_supported_configuration(config, diagnostics)
    _validate_task_metadata(config, task_dir, diagnostics)
    artifacts = _validate_timeouts_and_artifacts(config, diagnostics, source=source)
    credentials = source.credentials if source else ()
    topology, sidecar = _validate_compose_topology(task_dir, diagnostics, credentials)
    proofs = _validate_offline_build_proofs(task_dir, diagnostics, topology)
    sidecars = [name for name in topology["services"] if name != "main"] if topology else sidecar
    _validate_mcp_servers(config, sidecars, diagnostics)
    _validate_verifier_collect(config, artifacts, diagnostics)
    _validate_verifier_env(config, source or CandidateSource("", "", ""), diagnostics)
    _validate_dockerfile(task_dir, diagnostics, has_proof="environment" in proofs)
    _validate_build_context_contents(task_dir, diagnostics, proofs, topology)
    _validate_verifier_image(task_dir, diagnostics, has_proof="tests" in proofs)
    _validate_network_and_isolation(config, task_dir, diagnostics)
    _validate_golden_leak(task_dir, diagnostics)
    _validate_test_contract(task_dir, diagnostics)
    return tuple(sorted(diagnostics, key=lambda item: (item.severity, item.code, item.path)))


def main() -> None:
    """Run native package checks, without Workbench orchestration."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--credential", action="append", default=[], help="Allowed credential name")
    args = parser.parse_args()
    source = CandidateSource("", "", "", credentials=tuple(args.credential))
    findings = check_package(args.task, source=source)
    print(json.dumps([finding.to_dict() for finding in findings], indent=2))
    raise SystemExit(1 if any(finding.severity == "error" for finding in findings) else 0)


if __name__ == "__main__":
    main()
