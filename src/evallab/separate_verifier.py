from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shlex
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.purge_installed_copies import MARKER_V2 as PURGE_INSTALLED_COPIES_MARKER_V2
from evallab.purge_installed_copies import SETUP_PYTHON_PATH
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "separate-verifier@1"

SNAP_DIR = "/var/tmp/mimo-separate"

TRAJECTORY_ARTIFACT = "/logs/agent/trajectory.json"

MIMO_STATE_DIR = "/var/lib/mimo"


@dataclass(frozen=True)
class ParentInfo:

    task_name: str
    workdir: str
    docker_image: str
    has_solution: bool


def read_parent_info(parent_dir: Path | str) -> ParentInfo:
    parent = Path(parent_dir)
    try:
        config = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc

    verifier = config.get("verifier")
    if not isinstance(verifier, dict):
        raise VariantInvalid("parent task.toml has no [verifier] table")
    if verifier.get("environment_mode") == "separate" or "environment" in verifier:
        raise VariantInvalid("parent is already in separate-verifier mode")
    if (parent / "tests" / "Dockerfile").exists():
        raise VariantInvalid("parent already has tests/Dockerfile")
    if not (parent / "tests" / "test.sh").is_file():
        raise VariantInvalid("parent has no tests/test.sh")

    environment = config.get("environment")
    if not isinstance(environment, dict):
        raise VariantInvalid("parent task.toml has no [environment] table")
    workdir = environment.get("workdir")
    docker_image = environment.get("docker_image")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise VariantInvalid("parent [environment].workdir must be an absolute path")
    if not isinstance(docker_image, str) or not docker_image:
        raise VariantInvalid("parent [environment].docker_image must be set")

    task = config.get("task")
    if not isinstance(task, dict) or not task.get("name"):
        raise VariantInvalid("parent task.toml has no [task].name")
    return ParentInfo(
        task_name=str(task["name"]),
        workdir=workdir,
        docker_image=docker_image,
        has_solution=(parent / "solution" / "solve.sh").is_file(),
    )


def render_tests_dockerfile(docker_image: str) -> str:
    return (
        "# Separate-verifier image (separate-verifier@1): pristine repo checkout\n"
        "# plus the hidden tests bundled in. The agent image never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh /tests/test-orig.sh\n"
    )


def render_snapshot_hook(workdir: str) -> str:
    return (
        f'SNAP="{SNAP_DIR}"; CWD="{workdir}"; M="{MIMO_STATE_DIR}"; '
        'mkdir -p "$SNAP"; '
        'cp "$M/base" "$SNAP/base"; '
        'rm -rf "$SNAP/stage"; '
        'if [ -d "$M/git-hidden" ]; then '
        'tar -czf "$SNAP/git-hidden.tgz" -C "$M" git-hidden; '
        f'elif [ -d "$CWD/.git" ]; then '
        'mkdir -p "$SNAP/stage/git-hidden"; '
        'cp -r "$CWD/.git/." "$SNAP/stage/git-hidden/"; '
        'tar -czf "$SNAP/git-hidden.tgz" -C "$SNAP/stage" git-hidden; '
        'rm -rf "$SNAP/stage"; fi; '
        'tar --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" .; '
        'ls -la "$SNAP"; true'
    )


def render_probe_hook(workdir: str, marker: str) -> str:
    return (
        f'SNAP="{SNAP_DIR}"; CWD="{workdir}"; '
        'mkdir -p "$SNAP"; '
        '{ echo "== ls /tests =="; ls -la /tests 2>&1; '
        f'echo "== grep marker {marker} =="; '
        f'grep -rl "{marker}" "$CWD" /tmp '
        "--exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv "
        "--exclude-dir=node_modules 2>/dev/null; "
        'echo "grep_rc=$?"; } > "$SNAP/probe-agent-env.txt" 2>&1; '
        'cat "$SNAP/probe-agent-env.txt"; true'
    )


def render_wrapper_test_sh(workdir: str, marker: str) -> str:
    return (
        "#!/bin/bash\n"
        "# separate-verifier@1 entry: restore the agent snapshot captured by the\n"
        "# [[verifier.collect]] snapshot hook, then run the original grading\n"
        "# script (tests/test-orig.sh) unmodified.\n"
        "set -u\n"
        f'SNAP="{SNAP_DIR}"\n'
        f'M="{MIMO_STATE_DIR}"\n'
        f'CWD="{workdir}"\n'
        "V=/logs/verifier\n"
        'mkdir -p "$V" "$M"\n'
        "# Verifier-side probe: the marker must be present here (bundled tests).\n"
        "{\n"
        'echo "== marker in /tests ==";\n'
        f'grep -rl "{marker}" /tests 2>/dev/null || echo "MARKER NOT FOUND IN /tests";\n'
        '} > "$V/separate-probe.txt" 2>&1\n'
        'test -f "$SNAP/base" || { echo "snapshot missing: $SNAP/base" >&2; exit 1; }\n'
        'test -f "$SNAP/workspace.tgz" || '
        '{ echo "snapshot missing: $SNAP/workspace.tgz" >&2; exit 1; }\n'
        'cp "$SNAP/base" "$M/base"\n'
        'if [ -f "$SNAP/git-hidden.tgz" ]; then\n'
        '  rm -rf "$CWD/.git" "$M/git-hidden";\n'
        '  tar -xzf "$SNAP/git-hidden.tgz" -C "$M";\n'
        "fi\n"
        'cd "$CWD"\n'
        'find "$CWD" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +\n'
        'tar -xzf "$SNAP/workspace.tgz" -C "$CWD"\n'
        "exec /tests/test-orig.sh\n"
    )


def render_task_toml(parent_text: str, *, snapshot_hook: str, probe_hook: str) -> str:
    lines = parent_text.splitlines(keepends=True)
    try:
        schema_idx = next(i for i, line in enumerate(lines) if line.startswith("schema_version"))
    except StopIteration as exc:
        raise VariantInvalid("parent task.toml has no schema_version line") from exc
    try:
        verifier_idx = next(i for i, line in enumerate(lines) if line.strip() == "[verifier]")
    except StopIteration as exc:
        raise VariantInvalid("parent task.toml has no [verifier] header line") from exc

    lines.insert(
        schema_idx + 1, f'\nartifacts = [\n    "{SNAP_DIR}",\n    "{TRAJECTORY_ARTIFACT}",\n]\n'
    )
    verifier_idx += 1
    lines.insert(verifier_idx + 1, 'environment_mode = "separate"\n')

    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    text += (
        "\n# separate-verifier@1: snapshot the agent workspace for the verifier.\n"
        "# The probe hook runs first so the snapshot tarballs are not scanned.\n"
        "[[verifier.collect]]\n"
        'service = "main"\n'
        f"command = '{probe_hook}'\n"
        "timeout_sec = 300.0\n"
        'user = "root"\n'
        "\n"
        "[[verifier.collect]]\n"
        'service = "main"\n'
        f"command = '{snapshot_hook}'\n"
        "timeout_sec = 600.0\n"
        'user = "root"\n'
    )
    return text


def build_changes(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    if not marker or not marker.strip():
        raise VariantInvalid("marker must be a nonempty hidden-test identifier")
    parent = Path(parent_dir)
    info = read_parent_info(parent)
    if info.has_solution and solution_sh is not None:
        raise VariantInvalid("parent already has solution/solve.sh; refusing overwrite")

    original_test_sh = (parent / "tests" / "test.sh").read_bytes()
    snapshot_hook = render_snapshot_hook(info.workdir)
    probe_hook = render_probe_hook(info.workdir, marker)
    parent_toml_text = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        "task.toml": render_task_toml(
            parent_toml_text, snapshot_hook=snapshot_hook, probe_hook=probe_hook
        ).encode("utf-8"),
        "tests/test.sh": render_wrapper_test_sh(info.workdir, marker).encode("utf-8"),
        "tests/test-orig.sh": original_test_sh,
        "tests/Dockerfile": render_tests_dockerfile(info.docker_image).encode("utf-8"),
    }
    if solution_sh is not None:
        changes["solution/solve.sh"] = solution_sh

    inputs: dict[str, Any] = {
        "parent_task": info.task_name,
        "workdir": info.workdir,
        "docker_image": info.docker_image,
        "marker": marker,
        "snapshot_dir": SNAP_DIR,
        "trajectory_artifact": TRAJECTORY_ARTIFACT,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Bundle hidden tests into a separate verifier environment; "
    "pass the agent workspace as a declared artifact.",
    created_by: str = "har169-separate-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    changes, inputs = build_changes(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


V2_SETUP_SUBDIR = "tests/_verifier-setup"

V2_GRADE_DIR = "/var/lib/mimo-grade"

SETUP_HELPER_ENV = {"PATH": SETUP_PYTHON_PATH, "PIP_NO_CACHE_DIR": "1"}


def render_verifier_setup_bootstrap(*, tests_dir: str, state_dir: str, grade_dir: str) -> str:
    bundle_name = PurePosixPath(V2_SETUP_SUBDIR).name
    return f"""set -eu
tests={shlex.quote(tests_dir)}
state={shlex.quote(state_dir)}
grade={shlex.quote(grade_dir)}
mkdir -p "$state" "$grade"
backup=$(mktemp -d "$grade/native-tests.XXXXXX")
cp -a "$tests/." "$backup/"
trap 'status=$?; rm -rf "$tests"; cp -a "$backup/." "$tests" || exit 1; rm -rf "$backup"; exit "$status"' EXIT
cp -a "$backup/{bundle_name}/." "$state/"
bash "$state/setup.sh"
test -f "$state/ready"
echo "native setup: ready marker observed"
"""


TAMPER_SIGNATURES: tuple[str, ...] = (
    r"\b_pytest\b",
    r"\bpytest_(runtest|configure|collection|sessionfinish|sessionstart|report|terminal|unconfigure)",
    r"\bTestReport\b",
    r"junitxml",
    r"\bos\._exit\b",
    r"\batexit\b",
)

_TEST_INFRA_BASENAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py"}
)

JUNIT_MISSING_REASON = "junit missing after pytest start"

_ADDOPTS_CLEAR_RE = re.compile(
    r"unset\s+[^\n]*PYTEST_ADDOPTS|PYTEST_ADDOPTS\s*=|env\s+[^\n]*-u\s+PYTEST_ADDOPTS"
)

_B64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _decode_b64_blobs(text: str) -> str:
    decoded = [text]
    for token in _B64_BLOB_RE.findall(text):
        try:
            decoded.append(base64.b64decode(token, validate=True).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            continue
    return "\n".join(decoded)


def resolve_command_text(test_command_sh: str, mimo_script: str | None, patch_text: str) -> str:
    parts = [test_command_sh, mimo_script or "", patch_text.split("mimo_test_command.sh")[-1]]
    return _decode_b64_blobs("\n".join(parts))


def addopts_cleared(command_text: str) -> bool:
    return _ADDOPTS_CLEAR_RE.search(command_text) is not None


def is_test_infra_filename(path: str) -> bool:
    base = PurePosixPath(path).name
    return base in _TEST_INFRA_BASENAMES or base.endswith(".pth") or base.endswith("_test.go")


def drop_reason(path: str, hidden_paths: Collection[str]) -> str | None:
    if is_test_infra_filename(path):
        return "test-infra"
    if path in hidden_paths:
        return "hidden-test path"
    return None


def render_snapshot_hook_v2(workdir: str, *, snap_dir: str = SNAP_DIR) -> str:
    return (
        f'SNAP="{snap_dir}"; CWD="{workdir}"; '
        'mkdir -p "$SNAP"; '
        'LIST="$SNAP/files"; '
        'git -C "$CWD" ls-files -c -o --exclude-standard -z > "$LIST" 2>/dev/null; '
        'if [ -s "$LIST" ]; then '
        'tar --null --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" --files-from="$LIST"; '
        'else '
        'tar --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" .; '
        'fi; '
        'ls -la "$SNAP"; true'
    )


def collect_verifier_setup_files(parent_dir: Path | str) -> dict[str, bytes]:
    setup_dir = Path(parent_dir) / "environment" / "setup"
    if not (setup_dir / "setup.sh").is_file() or (setup_dir / "setup.sh").is_symlink():
        raise VariantInvalid("parent has no environment/setup/setup.sh for the @2 bundle")
    if (Path(parent_dir) / V2_SETUP_SUBDIR).exists():
        raise VariantInvalid(f"parent already has {V2_SETUP_SUBDIR}; refusing overwrite")
    bundled: dict[str, bytes] = {}
    for path in sorted(setup_dir.rglob("*")):
        if path.is_symlink():
            raise VariantInvalid(f"setup bundle entry is a symlink: {path.name}")
        if path.is_file():
            bundled[f"{V2_SETUP_SUBDIR}/{path.relative_to(setup_dir).as_posix()}"] = (
                path.read_bytes()
            )
    if not bundled:
        raise VariantInvalid("parent environment/setup/ is empty; nothing to bundle")
    return bundled


V3_NEW_INFRA_BASENAMES = frozenset(
    {
        "jest.config.js",
        "jest.config.cjs",
        "jest.config.mjs",
        "jest.config.ts",
        "jest.config.json",
        "jest.setup.js",
        "jest.setup.cjs",
        "jest.setup.mjs",
        "jest.setup.ts",
        "jest.preset.js",
        "vitest.config.js",
        "vitest.config.ts",
        "vitest.config.mts",
        "vitest.setup.js",
        "vitest.setup.ts",
        "vitest.workspace.js",
        "vitest.workspace.ts",
        "vitest.shared.js",
        "vitest.shared.ts",
        "ava.config.js",
        "ava.config.cjs",
        "ava.config.mjs",
        "karma.conf.js",
        ".mocharc.js",
        ".mocharc.cjs",
        ".mocharc.mjs",
        ".mocharc.json",
        ".mocharc.yml",
        ".mocharc.yaml",
        ".taprc",
        ".nycrc",
        ".nycrc.json",
        "jasmine.json",
        "build.rs",
        "phpunit.xml",
        "phpunit.xml.dist",
        "phpunit.dist.xml",
        ".rspec",
    }
)

V3_BUILD_OUTPUT_DIR_RES = (
    r"(^|/)target/surefire-reports/",
    r"(^|/)target/failsafe-reports/",
    r"(^|/)build/test-results/",
    r"(^|/)\.nyc_output/",
    r"(^|/)coverage/",
)
_V3_BUILD_OUTPUT_RES_COMPILED = tuple(re.compile(p) for p in V3_BUILD_OUTPUT_DIR_RES)


def is_v3_new_infra(path: str, *, in_base: bool) -> bool:
    if in_base:
        return False
    if PurePosixPath(path).name in V3_NEW_INFRA_BASENAMES:
        return True
    return any(rx.search(path) for rx in _V3_BUILD_OUTPUT_RES_COMPILED)


V3_CONFIG_HUNK_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "package.json runner key",
        ("package.json",),
        r'"(jest|mocha|vitest|ava|tap|karma|jasmine|nyc|c8)"\s*:',
    ),
    ("pom.xml surefire", ("pom.xml",), r"surefire|failsafe|skipTests|<excludes?>"),
    (
        "gradle test block",
        ("build.gradle", "build.gradle.kts"),
        r"exclude|ignoreFailures|useJUnit|test\s*\{",
    ),
    ("rspec options", (".rspec",), r"--require|--format|--tag|--exclude-pattern"),
    (
        "phpunit config",
        ("phpunit.xml", "phpunit.xml.dist", "phpunit.dist.xml"),
        r"bootstrap|suffix=|exclude|executionOrder|printer",
    ),
    ("go toolchain", ("go.mod",), r"toolchain"),
    (
        "mocha options",
        (
            ".mocharc.js",
            ".mocharc.cjs",
            ".mocharc.mjs",
            ".mocharc.json",
            ".mocharc.yml",
            ".mocharc.yaml",
        ),
        r"spec|require|ignore|exclude|grep|invert|reporter",
    ),
)
_V3_CONFIG_HUNK_COMPILED = tuple(
    (label, frozenset(names), re.compile(pattern, re.IGNORECASE))
    for label, names, pattern in V3_CONFIG_HUNK_RULES
)


def v3_config_revert_reason(path: str, changed_lines: str) -> str | None:
    base = PurePosixPath(path).name
    for label, names, pattern in _V3_CONFIG_HUNK_COMPILED:
        if base in names and pattern.search(changed_lines):
            return label
    return None


V3_TAMPER_SIGNATURES: tuple[str, ...] = TAMPER_SIGNATURES + (
    r"\bos\.Exit\s*\(",
    r"\bprocess\.exit\s*\(",
    r"\bprocess\.exitCode\s*=",
    r"\bDeno\.exit\s*\(",
    r"\bSystem\.exit\s*\(",
    r"\bRuntime\.getRuntime\(\)\.halt\s*\(",
    r"process::exit\s*\(",
    r"\bstd::process::exit",
    r"\bKernel\.exit\b",
    r"\bglobal\.(it|test|describe|fit)\s*=",
)

V3_SCOPED_SIGNATURES: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".rb",), r"\bexit\s*\(\s*0\s*\)|\bexit!\s*\(\s*0?\s*\)"),
    ((".php",), r"\bexit\s*\(\s*0\s*\)|\bdie\s*\(\s*0\s*\)"),
)
_V3_SCOPED_COMPILED = tuple(
    (frozenset(exts), re.compile(pattern)) for exts, pattern in V3_SCOPED_SIGNATURES
)


def v3_tamper_hit_for_file(path: str, added_lines: str) -> str | None:
    for signature in V3_TAMPER_SIGNATURES:
        if re.search(signature, added_lines):
            return signature
    ext = PurePosixPath(path).suffix.lower()
    for exts, pattern in _V3_SCOPED_COMPILED:
        if ext in exts and pattern.search(added_lines):
            return pattern.pattern
    return None


V3_RUNNER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("go-test", re.compile(r"(?<![\w])go\s+test\b|GO_BIN\"\s+test\b|\$GO_BIN\"\s+test")),
    ("jest", re.compile(r"(?<![\w])jest\b")),
    ("vitest", re.compile(r"(?<![\w])vitest\b")),
    ("mocha", re.compile(r"(?<![\w])mocha\b|_mocha\b")),
    ("ava", re.compile(r"(?<![\w])ava\b")),
    ("tap", re.compile(r"(?<![\w])tap\b")),
    ("karma", re.compile(r"(?<![\w])karma\b")),
    ("jasmine", re.compile(r"(?<![\w])jasmine\b")),
    ("node-test", re.compile(r"node\s+--test\b")),
    ("cargo-test", re.compile(r"cargo\s+test\b")),
    ("rspec", re.compile(r"(?<![\w])rspec\b")),
    ("phpunit", re.compile(r"(?<![\w])phpunit\b")),
    ("mvn", re.compile(r"(?<![\w])mvn\b|surefire|failsafe")),
    ("gradle", re.compile(r"(?<![\w])gradlew?\b")),
    ("pytest", re.compile(r"(?<![\w])pytest\b")),
    ("unittest", re.compile(r"-m\s+unittest\b")),
    ("forge", re.compile(r"(?<![\w])forge\s+test\b")),
    ("node-run", re.compile(r"(?<![\w])node\b")),
    ("make", re.compile(r"(?<![\w])(make|ctest|cmake)\b")),
    ("bats", re.compile(r"(?<![\w])bats\b")),
)


def detect_runner(command_text: str) -> str:
    for runner, pattern in V3_RUNNER_PATTERNS:
        if pattern.search(command_text):
            return runner
    return "custom"


_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) test")
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)
_UNITTEST_BAD_RE = re.compile(r"^(FAILED|ERROR)", re.MULTILINE)


def evaluate_unittest(test_output: str, rc: int) -> int:
    match = _UNITTEST_RAN_RE.search(test_output)
    ran = int(match.group(1)) if match else 0
    if rc != 0:
        return 0
    if ran < 1 or not _UNITTEST_OK_RE.search(test_output):
        return 0
    if _UNITTEST_BAD_RE.search(test_output):
        return 0
    return 1


V3_CONFTEST_HOOK = (
    "# separate-verifier@3 hook: structured report for addopts-cleared runs.\n"
    "import os as _v3_os\n"
    "_V3_JUNIT_PATH = _v3_os.environ.get("
    '"MIMO_VERIFIER_JUNIT", "/var/lib/mimo-grade/junit.xml")\n'
    "_V3_COLLECTED = []\n"
    "def pytest_runtest_logreport(report):\n"
    '    if report.when == "call" or (report.when == "setup" and report.skipped):\n'
    "        _V3_COLLECTED.append((report.nodeid, report.outcome))\n"
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        import xml.etree.ElementTree as ET\n"
    '        suite = ET.Element("testsuite", name="verifier", tests=str(len(_V3_COLLECTED)))\n'
    "        for nodeid, outcome in _V3_COLLECTED:\n"
    '            name = nodeid.split("::")[-1]\n'
    '            case = ET.SubElement(suite, "testcase", classname=nodeid, name=name)\n'
    '            if outcome == "failed":\n'
    '                ET.SubElement(case, "failure", message="failed")\n'
    '            elif outcome == "skipped":\n'
    '                ET.SubElement(case, "skipped", message="skipped")\n'
    "        ET.ElementTree(suite).write(_V3_JUNIT_PATH)\n"
    "    except Exception:\n"
    "        pass\n"
)

V5_ARCHIVE_HOOK = (
    "# mimo-junit-archive/1: per-phase junit archive.\n"
    "import os as _v5_os\n"
    '_V5_DIR = _v5_os.environ.get("MIMO_VERIFIER_ARCHIVE_DIR", "")\n'
    "_V5_COLLECTED = []\n"
    "def pytest_runtest_logreport(report):\n"
    '    if report.when == "call" or (report.when == "setup" and report.skipped):\n'
    "        _V5_COLLECTED.append((report.nodeid, report.outcome))\n"
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        if not _V5_DIR:\n"
    "            return\n"
    "        _v5_os.makedirs(_V5_DIR, exist_ok=True)\n"
    '        _v5_count_path = _v5_os.path.join(_V5_DIR, "count")\n'
    "        try:\n"
    '            with open(_v5_count_path, encoding="utf-8") as _v5_f:\n'
    '                _v5_n = int((_v5_f.read() or "0").strip())\n'
    "        except Exception:\n"
    "            _v5_n = 0\n"
    "        import xml.etree.ElementTree as _v5_ET\n"
    '        _v5_suite = _v5_ET.Element("testsuite", name="verifier-archive", tests=str(len(_V5_COLLECTED)))\n'
    "        for _v5_nodeid, _v5_outcome in _V5_COLLECTED:\n"
    '            _v5_parts = _v5_nodeid.split("::")\n'
    "            _v5_file = _v5_parts[0]\n"
    "            _v5_rest = _v5_parts[1:]\n"
    '            _v5_mod = _v5_file.replace("/", ".").replace(chr(92), ".")\n'
    '            if _v5_mod.endswith(".py"):\n'
    "                _v5_mod = _v5_mod[:-3]\n"
    "            if len(_v5_rest) > 1:\n"
    '                _v5_cn = (_v5_mod + "." + ".".join(_v5_rest[:-1])) if _v5_mod else ".".join(_v5_rest[:-1])\n'
    "                _v5_name = _v5_rest[-1]\n"
    "            elif _v5_rest:\n"
    "                _v5_cn = _v5_mod\n"
    "                _v5_name = _v5_rest[0]\n"
    "            else:\n"
    "                _v5_cn = _v5_mod\n"
    "                _v5_name = _v5_nodeid\n"
    '            _v5_case = _v5_ET.SubElement(_v5_suite, "testcase", classname=_v5_cn, name=_v5_name, file=_v5_file)\n'
    '            if _v5_outcome == "failed":\n'
    '                _v5_ET.SubElement(_v5_case, "failure", message="failed")\n'
    '            elif _v5_outcome == "skipped":\n'
    '                _v5_ET.SubElement(_v5_case, "skipped", message="skipped")\n'
    '        _v5_ET.ElementTree(_v5_suite).write(_v5_os.path.join(_V5_DIR, "junit-hook-%d.xml" % _v5_n))\n'
    '        with open(_v5_count_path, "w", encoding="utf-8") as _v5_f:\n'
    "            _v5_f.write(str(_v5_n + 1))\n"
    "    except Exception:\n"
    "        pass\n"
)

TRANSFORM_ID_V6 = "separate-verifier@6"


TRANSFORM_ID_V8 = "separate-verifier@8"


TRANSFORM_ID_V10 = "separate-verifier@10"

BINARY_ARTIFACT_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib", ".o", ".a", ".class"}
)


def is_binary_artifact_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if "__pycache__" in pure.parts:
        return True
    return pure.suffix.lower() in BINARY_ARTIFACT_SUFFIXES


V10_INTEGRITY_HOOK = (
    "# mimo-integrity/1: runner-internal integrity check.\n"
    "import os as _v10_os\n"
    '_V10_DIR = _v10_os.environ.get("MIMO_VERIFIER_INTEGRITY_DIR", "")\n'
    "_V10_ORIG = {}\n"
    "try:\n"
    "    import pytest as _v10_pytest\n"
    '    _V10_ORIG["pytest.Function.runtest"] = _v10_pytest.Function.runtest\n'
    "except Exception:\n"
    "    _v10_pytest = None\n"
    "try:\n"
    "    import _pytest.python as _v10_pp\n"
    '    _V10_ORIG["_pytest.python.Function.runtest"] = _v10_pp.Function.runtest\n'
    "except Exception:\n"
    "    _v10_pp = None\n"
    "try:\n"
    "    import unittest as _v10_ut\n"
    '    _V10_ORIG["unittest.TestCase.run"] = _v10_ut.TestCase.run\n'
    "except Exception:\n"
    "    _v10_ut = None\n"
    '_v10_prev_sessionfinish = globals().get("pytest_sessionfinish")\n'
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        if _v10_prev_sessionfinish is not None:\n"
    "            _v10_prev_sessionfinish(session, exitstatus)\n"
    "    except Exception:\n"
    "        pass\n"
    "    try:\n"
    "        if not _V10_DIR:\n"
    "            return\n"
    "        _v10_changed = []\n"
    '        if _v10_pytest is not None and _v10_pytest.Function.runtest is not _V10_ORIG.get("pytest.Function.runtest"):\n'
    '            _v10_changed.append("pytest.Function.runtest")\n'
    '        if _v10_pp is not None and _v10_pp.Function.runtest is not _V10_ORIG.get("_pytest.python.Function.runtest"):\n'
    '            _v10_changed.append("_pytest.python.Function.runtest")\n'
    '        if _v10_ut is not None and _v10_ut.TestCase.run is not _V10_ORIG.get("unittest.TestCase.run"):\n'
    '            _v10_changed.append("unittest.TestCase.run")\n'
    "        _v10_os.makedirs(_V10_DIR, exist_ok=True)\n"
    '        _v10_text = ("CHANGED " + ",".join(_v10_changed)) if _v10_changed else "OK"\n'
    '        with open(_v10_os.path.join(_V10_DIR, "integrity.txt"), "w", encoding="utf-8") as _v10_f:\n'
    '            _v10_f.write(_v10_text + "\\n")\n'
    "    except Exception:\n"
    "        pass\n"
)

V10_UNITTEST_SHIM = (
    "# mimo-unittest-shim/1: structured unittest outcomes + integrity.\n"
    "import atexit as _v10_atexit\n"
    "import os as _v10_os\n"
    "import re as _v10_re\n"
    "_V10_CASES = []\n"
    "_V10_UT = None\n"
    "_V10_ORIG_TC = None\n"
    "_V10_ORIG_RUNNER = None\n"
    "try:\n"
    "    import unittest as _V10_UT\n"
    "    import unittest.runner as _v10_runner\n"
    "    _V10_ORIG_TC = _V10_UT.TestCase.run\n"
    "    _V10_ORIG_RUNNER = _v10_runner.TextTestRunner.run\n"
    "    _v10_result = _v10_runner.TextTestResult\n"
    "    def _v10_wrap(name, status):\n"
    "        _orig = getattr(_v10_result, name)\n"
    "        def _wrapped(self, test, *args, **kwargs):\n"
    "            _V10_CASES.append((str(test), status))\n"
    "            return _orig(self, test, *args, **kwargs)\n"
    "        setattr(_v10_result, name, _wrapped)\n"
    '    _v10_wrap("addSuccess", "passed")\n'
    '    _v10_wrap("addFailure", "failed")\n'
    '    _v10_wrap("addError", "failed")\n'
    '    _v10_wrap("addSkip", "skipped")\n'
    '    _v10_wrap("addExpectedFailure", "skipped")\n'
    '    _v10_wrap("addUnexpectedSuccess", "failed")\n'
    "except Exception:\n"
    "    _V10_UT = None\n"
    "def _v10_flush():\n"
    "    try:\n"
    '        _idir = _v10_os.environ.get("MIMO_VERIFIER_INTEGRITY_DIR", "")\n'
    "        if _idir and _V10_UT is not None:\n"
    "            _changed = []\n"
    "            if _V10_UT.TestCase.run is not _V10_ORIG_TC:\n"
    '                _changed.append("unittest.TestCase.run")\n'
    "            import unittest.runner as _v10_rr\n"
    "            if _v10_rr.TextTestRunner.run is not _V10_ORIG_RUNNER:\n"
    '                _changed.append("unittest.TextTestRunner.run")\n'
    "            _v10_os.makedirs(_idir, exist_ok=True)\n"
    '            _text = ("CHANGED " + ",".join(_changed)) if _changed else "OK"\n'
    '            with open(_v10_os.path.join(_idir, "integrity-unittest.txt"), "w", encoding="utf-8") as _f:\n'
    '                _f.write(_text + "\\n")\n'
    "    except Exception:\n"
    "        pass\n"
    "    try:\n"
    '        _out = _v10_os.environ.get("MIMO_VERIFIER_UNITTEST_JUNIT", "")\n'
    "        if not _out:\n"
    "            return\n"
    "        import xml.etree.ElementTree as _ET\n"
    '        _suite = _ET.Element("testsuite", name="verifier-unittest", tests=str(len(_V10_CASES)))\n'
    "        for _label, _status in _V10_CASES:\n"
    '            _m = _v10_re.match(r"^(.*?)\\s*\\(([^()]*)\\)$", _label)\n'
    "            if _m:\n"
    "                _name = _m.group(1)\n"
    "                _cn = _m.group(2)\n"
    "            else:\n"
    "                _name = _label\n"
    '                _cn = ""\n'
    '            _case = _ET.SubElement(_suite, "testcase", classname=_cn, name=_name)\n'
    '            if _status == "failed":\n'
    '                _ET.SubElement(_case, "failure", message="failed")\n'
    '            elif _status == "skipped":\n'
    '                _ET.SubElement(_case, "skipped", message="skipped")\n'
    "        _v10_os.makedirs(_v10_os.path.dirname(_out), exist_ok=True)\n"
    "        _ET.ElementTree(_suite).write(_out)\n"
    "    except Exception:\n"
    "        pass\n"
    "_v10_atexit.register(_v10_flush)\n"
)


_V10_RUN_LINE = (
    'MIMO_VERIFIER_ARCHIVE_DIR="$JUNIT_PHASES" '
    'MIMO_VERIFIER_UNITTEST_JUNIT="$GRADE/unittest-junit.xml" '
    'MIMO_VERIFIER_INTEGRITY_DIR="$GRADE" '
    'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$GRADE/pycache" '
    'PYTHONPATH="$GRADE/v10shim${PYTHONPATH:+:$PYTHONPATH}" '
    'PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" '
    'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1'
)

_V10_TEMPLATE_DIR = Path(__file__).resolve().parent / "data" / "separate_verifier_v10"
_V10_WRAPPER_A = (_V10_TEMPLATE_DIR / "part_a.sh").read_text(encoding="utf-8")
_V10_WRAPPER_B = (_V10_TEMPLATE_DIR / "part_b.sh").read_text(encoding="utf-8")
_V10_WRAPPER_C = (_V10_TEMPLATE_DIR / "part_c.sh").read_text(encoding="utf-8")


_SUPPLEMENTARY_BLOCK = re.compile(
    r'if \[ "\$RC" -eq 0 \]; then\n\s*(?P<cmd>[^\n]+?)\s*>>\s*"\$V/test_output\.log"\s*2>&1\n\s*RC=\$\?\nfi'
)


def supplementary_test_blocks(parent_dir: Path | str, *, workdir: str | None = None) -> list[str]:
    parent = Path(parent_dir)
    try:
        text = (parent / "tests" / "test.sh").read_text(encoding="utf-8")
    except OSError:
        return []
    target = workdir or _workdir_from_toml(parent)
    blocks = []
    for match in _SUPPLEMENTARY_BLOCK.finditer(text):
        command = match.group("cmd").strip()
        if target:
            command = command.replace("/testbed", target).replace('"$CWD"', f'"{target}"')
        blocks.append(command)
    return blocks


def _workdir_from_toml(parent: Path) -> str | None:
    try:
        config = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    workdir = config.get("environment", {}).get("workdir")
    return workdir if isinstance(workdir, str) and workdir.startswith("/") else None


def render_wrapper_test_sh_v10(
    workdir: str, *, python_setup: bool = False, extra_blocks: tuple[str, ...] = ()
) -> str:
    if not workdir.startswith("/"):
        raise VariantInvalid("workdir must be an absolute path")
    universal = "[" + ", ".join(repr(s) for s in V3_TAMPER_SIGNATURES) + "]"
    scoped = (
        "["
        + ", ".join(f"({sorted(exts)!r}, {pattern!r})" for exts, pattern in V3_SCOPED_SIGNATURES)
        + "]"
    )
    hook_b64 = base64.b64encode(V3_CONFTEST_HOOK.encode("utf-8")).decode("ascii")
    archive_b64 = base64.b64encode(V5_ARCHIVE_HOOK.encode("utf-8")).decode("ascii")
    integrity_b64 = base64.b64encode(V10_INTEGRITY_HOOK.encode("utf-8")).decode("ascii")
    shim_b64 = base64.b64encode(V10_UNITTEST_SHIM.encode("utf-8")).decode("ascii")
    wrapper_a = _V10_WRAPPER_A
    if python_setup:
        wrapper_a = wrapper_a.replace(
            'if ! bash "$M/setup.sh"',
            f'if ! PATH="{SETUP_PYTHON_PATH}" bash "$M/setup.sh"',
            1,
        )
    text = (wrapper_a + _V10_WRAPPER_B + _V10_WRAPPER_C)
    if extra_blocks:
        appended = "\n".join(
            f'if [ "$RC" -eq 0 ]; then\n'
            f'  {block} >> "$V/test_output.log" 2>&1\n'
            f'  RC=$?\n'
            f"fi"
            for block in extra_blocks
        )
        anchor = _V10_RUN_LINE + "\nRC=$?\n"
        if anchor not in text:
            raise VariantInvalid("@10 wrapper does not carry the expected run line and RC assignment")
        text = text.replace(anchor, anchor + appended + "\n", 1)
    return (
        text
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@V5_ARCHIVE_HOOK_B64@@", archive_b64)
        .replace("@@V10_INTEGRITY_HOOK_B64@@", integrity_b64)
        .replace("@@V10_UNITTEST_SHIM_B64@@", shim_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v10(docker_image: str) -> str:
    return (
        "# Separate-verifier image (separate-verifier@10): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v10(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    if not marker or not marker.strip():
        raise VariantInvalid("marker must be a nonempty hidden-test identifier")
    parent = Path(parent_dir)
    info = read_parent_info(parent)
    if info.has_solution and solution_sh is not None:
        raise VariantInvalid("parent already has solution/solve.sh; refusing overwrite")

    snapshot_hook = render_snapshot_hook_v2(info.workdir)
    probe_hook = render_probe_hook(info.workdir, marker)
    parent_toml_text = (parent / "task.toml").read_text(encoding="utf-8")
    setup_files = collect_verifier_setup_files(parent)
    python_setup = (
        PURGE_INSTALLED_COPIES_MARKER_V2.encode("utf-8")
        in setup_files[f"{V2_SETUP_SUBDIR}/setup.sh"]
    )
    extra_blocks = tuple(supplementary_test_blocks(parent, workdir=info.workdir))
    wrapper = render_wrapper_test_sh_v10(
        info.workdir, python_setup=python_setup, extra_blocks=extra_blocks
    ).encode("utf-8")
    try:
        patch_text = (parent / "tests" / "test.patch").read_text(encoding="utf-8")
        command_sh = (parent / "tests" / "test_command.sh").read_text(encoding="utf-8")
        runner = detect_runner(resolve_command_text(command_sh, None, patch_text))
    except OSError:
        runner = "unknown"
    changes: dict[str, bytes | None] = {
        "task.toml": render_task_toml(
            parent_toml_text, snapshot_hook=snapshot_hook, probe_hook=probe_hook
        ).encode("utf-8"),
        "tests/test.sh": wrapper,
        "tests/Dockerfile": render_tests_dockerfile_v10(info.docker_image).encode("utf-8"),
        **setup_files,
    }
    if solution_sh is not None:
        changes["solution/solve.sh"] = solution_sh

    setup_digest = hashlib.sha256(b"".join(setup_files[key] for key in sorted(setup_files)))
    inputs: dict[str, Any] = {
        "parent_task": info.task_name,
        "workdir": info.workdir,
        "docker_image": info.docker_image,
        "marker": marker,
        "snapshot_dir": SNAP_DIR,
        "trajectory_artifact": TRAJECTORY_ARTIFACT,
        "setup_files": sorted(setup_files),
        "setup_sha256": setup_digest.hexdigest(),
        "setup_python_path": SETUP_PYTHON_PATH if python_setup else "",
        "wrapper_sha256": hashlib.sha256(wrapper).hexdigest(),
        "supplementary_blocks": list(extra_blocks),
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v10(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with binary artifacts stripped, runner-integrity "
    "checks, and structured junit grading for pytest and unittest.",
    created_by: str = "integrity-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    changes, inputs = build_changes_v10(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V10,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )
