"""Read-only static checks for Harbor task verifier trust boundaries.

These findings screen declarations and files; they do not certify verifier
correctness or replace oracle, NOP, and negative-mutant control runs.
"""

import hashlib
import json
import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab.task_workbench import Diagnostic, _parse_task_toml

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


def _build_env_scripts(patch: str) -> list[str]:
    """Shell scripts embedded in the test patch as ``mimo_build_env.tar.gz.b64``."""
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
        scripts: list[str] = []
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                if not (member.isfile() and member.name.endswith(".sh")):
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                scripts.append(handle.read().decode("utf-8", "replace"))
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
        blobs.extend(_build_env_scripts(patch))
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
