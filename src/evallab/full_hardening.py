"""``full-hardening@1`` (``evallab harden``): one transform for MiMo ``test.sh`` packages and general Harbor packages.

It unifies ``grader-hardening@1`` (L1 auto-load reset, L3 report gate), ``separate-verifier@10`` (grading in a fresh
verifier container) and the pristine test reset of the MiMo parent shape, and closes the gaps the C6 seal validation
measured (research/experiments/fx-c6-seal-validation-20261030): the shipped transform refused general packages,
left graded test files and grader helpers in the agent workspace trusted, and could not see test files copied into
the agent image.

Layers, recorded per package in a :class:`HardeningManifest`:

* **L1** auto-load purge: agent-created ``conftest.py``, ``sitecustomize.py``, ``usercustomize.py``, ``pytest.ini``,
  ``tox.ini``, ``*.pth`` and JavaScript runner configs are deleted before grading; files with those names present in
  the original package are restored to their recorded sha256 instead of deleted.
* **L2** pristine restoration: graded test files and grader helpers that live in the agent workspace (a test
  directory the grader runs, a code file a test executes by path, or ``--helper`` paths) are bundled under
  ``tests/_evallab/pristine/`` and written back over the workspace before grading. ``tests/`` itself is verifier-owned.
* **L3** structured report gate: the runner is asked for a report at a grade-time path directly on its command line
  (pytest ``--junitxml``, unittest through a counting runner, vitest/jest JSON, mocha JSON), so a wrapper that clears
  ``PYTEST_ADDOPTS`` no longer disables the gate. Reward 1 needs the original grader's 1 *and* a report holding at least
  the reference test count with no failure, error or extra skip.
* **L4** separation and leak scrub: grading moves to a separate verifier container (``separate-verifier@10`` for MiMo,
  ``environment_mode = "separate"`` with the workdir as the only artifact for general packages), so a ``.pth`` planted
  in the agent's site directory never runs in the grading interpreter; ``COPY``/``ADD`` of test, oracle or harness
  files into the agent image is stripped from ``environment/Dockerfile``.

A separate verifier only grades if its image can run the gate. ``--verifier-dockerfile`` carries a verifier image
into the candidate as ``tests/Dockerfile`` (byte-identical, sha256 recorded), and every manifest records
``verifier.gate_runtime``: ``native`` (the gate runs in the task image), ``declared`` (the candidate ships or was given
a verifier image), or ``unverified`` — a separate verifier with no ``tests/Dockerfile`` copies the environment image,
which need not provide ``python3``; that candidate is written with a stderr warning.

Not closed, by design: a semantic verifier requirement (C4 fabricated verification, C5 environment acquisition) is a
task-design property, and instruction text that funnels the agent to tests is flagged for the author, never rewritten.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
import shlex
import shutil
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab import full_hardening_grade as grade
from evallab.grader_hardening import AUTOLOAD_PATTERN, build_test_sh
from evallab.registry import task_directory_digest
from evallab.separate_verifier import TRANSFORM_ID_V10, build_changes_v10
from evallab.task_variants import VariantInvalid

TRANSFORM_ID = "full-hardening@1"
BUNDLE_REL = "tests/_evallab"
MANIFEST_NAME = "hardening-manifest.json"
#: Harbor builds the separate verifier from ``tests/Dockerfile`` when the package has one.
VERIFIER_DOCKERFILE = "tests/Dockerfile"

UNVERIFIED_GATE_WARNING = (
    f"{TRANSFORM_ID}: verifier gate runtime unverified. The separate verifier copies the "
    "environment image, and no tests/Dockerfile declares python3, so the L3 gate can abort "
    "before the task's own grader runs (evidence: research/experiments/query-optimize-crack). "
    "Pass --verifier-dockerfile with a verifier image that provides python3."
)

#: ``case`` pattern of the MiMo L1 block: the grader-hardening@1 set plus JavaScript runner configs.
MIMO_L1_PATTERN = (
    AUTOLOAD_PATTERN + "|vitest.config.*|*/vitest.config.*|vite.config.*|*/vite.config.*"
    "|jest.config.*|*/jest.config.*|.mocharc.*|*/.mocharc.*"
)
#: Upstream MiMo ``tests/test.sh`` lines the L3 blocks are anchored on.
MIMO_RUN_LINE = 'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1\n'
MIMO_REWARD_LINE = (
    'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'
)

CODE_SUFFIXES = (".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".mts", ".rb", ".pl", ".bash")
TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "spec", "__tests__", "t"})
LEAK_PATH_GLOBS = ("/tests", "/tests/*", "/opt/*test*", "/opt/*harness*")
LEAK_WORDS = re.compile(r"expected|answer|solution|oracle|harness|hidden", re.I)
FUNNEL = re.compile(
    r"\btests?/|\b(hidden|visible|existing|acceptance)\s+tests?\b|\bgrader\b|\bverifier\b|\btest\.sh\b",
    re.I,
)
REPORT_SUFFIX = {
    "pytest": "xml",
    "unittest": "json",
    "vitest": "json",
    "jest": "json",
    "mocha": "json",
}
#: A grader line that executes something (a path named on it is run, not merely checked for existence).
EXEC_LINE = re.compile(
    r"subprocess|sys\.executable|Popen|check_call|check_output|os\.system|\bexec\b|"
    r"\b(python[\d.]*|bash|sh|node|npx|pytest|ruby|perl)\b"
)
SHELL_TEST = re.compile(r"\[\[?\s+!?\s*-[a-zA-Z]\s|^\s*#")


class HardeningRefused(ValueError):
    """The package cannot be hardened as asked (shape unknown, report count missing, ...)."""


@dataclass
class Layer:
    layer: str
    status: str  # applied | native | not-applicable | open
    mechanism: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    kind: str
    path: str
    detail: str
    action: str


@dataclass
class VerifierGate:
    """Where the L3 gate runs: ``native`` (task image), ``declared`` (a verifier image), ``unverified``."""

    gate_runtime: str
    dockerfile: str | None = None
    dockerfile_sha256: str | None = None
    origin: str | None = None  # flag | transform | package


@dataclass
class HardeningManifest:
    transform: str
    shape: str
    source: str
    source_digest: str
    workdir: str
    runner: str | None
    separate_verifier: bool
    expected_report: dict[str, int] | None
    calibration: bool
    layers: list[Layer]
    findings: list[Finding]
    verifier: VerifierGate
    output: str | None = None
    output_digest: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CopyEntry:
    lineno: int
    line: str
    sources: tuple[str, ...]
    destination: str
    files: tuple[tuple[str, Path], ...]  # (container path, local file)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_shape(package: Path) -> str:
    tests = package / "tests"
    test_sh = tests / "test.sh"
    if not (package / "task.toml").is_file() or not test_sh.is_file():
        raise HardeningRefused("not a Harbor package: task.toml and tests/test.sh are required")
    text = test_sh.read_text(encoding="utf-8", errors="replace")
    if "full-hardening@1" in text:
        raise HardeningRefused("package is already hardened by full-hardening@1")
    if (
        "EOF_RESET_TEST_FILES" in text
        and (tests / "test_command.sh").is_file()
        and (tests / "test.patch").is_file()
    ):
        return "mimo"
    return "harbor"


def _logical_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        buffer += raw
        lines.append((start, buffer))
        buffer = ""
    if buffer:
        lines.append((start, buffer))
    return lines


def dockerfile_copies(package: Path) -> tuple[list[CopyEntry], str | None]:
    """``COPY``/``ADD`` entries of ``environment/Dockerfile`` with the files each brings in, plus the last WORKDIR."""
    dockerfile = package / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return [], None
    context = package / "environment"
    workdir: str | None = None
    entries: list[CopyEntry] = []
    for lineno, line in _logical_lines(dockerfile.read_text(encoding="utf-8", errors="replace")):
        words = line.strip().split(None, 1)
        if not words:
            continue
        op = words[0].upper()
        rest = words[1] if len(words) > 1 else ""
        if op == "WORKDIR":
            target = rest.strip().strip('"')
            workdir = target if target.startswith("/") else posixpath.join(workdir or "/", target)
            continue
        if op not in {"COPY", "ADD"}:
            continue
        try:
            args = json.loads(rest) if rest.strip().startswith("[") else shlex.split(rest)
        except ValueError:
            continue
        args = (
            [a for a in args if not a.startswith("--")]
            if all(not a.startswith("--from") for a in args)
            else []
        )
        if len(args) < 2:
            continue
        *sources, destination = args
        if not destination.startswith("/"):
            destination = posixpath.join(workdir or "/", destination)
        files: list[tuple[str, Path]] = []
        for source in sources:
            matches = (
                sorted(context.glob(source.rstrip("/")))
                if any(c in source for c in "*?[")
                else [context / source.rstrip("/")]
            )
            for match in matches:
                if match.is_dir():
                    for path in sorted(p for p in match.rglob("*") if p.is_file()):
                        rel = path.relative_to(match).as_posix()
                        files.append((posixpath.normpath(posixpath.join(destination, rel)), path))
                elif match.is_file():
                    into_dir = destination.endswith("/") or len(sources) > 1
                    target = posixpath.join(destination, match.name) if into_dir else destination
                    files.append((posixpath.normpath(target), match))
        entries.append(CopyEntry(lineno, line, tuple(sources), destination, tuple(files)))
    return entries, workdir


def _workdir(package: Path, docker_workdir: str | None) -> str:
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    workdir = config.get("environment", {}).get("workdir") or docker_workdir
    if not workdir or not str(workdir).startswith("/"):
        raise HardeningRefused("no absolute workdir in task.toml [environment] or the Dockerfile")
    return posixpath.normpath(str(workdir))


def _test_file_digests(package: Path) -> dict[str, str]:
    """sha256 of every hidden test *code* file (data fixtures shared with the agent are legitimate inputs)."""
    tests = package / "tests"
    return {
        _sha(path.read_bytes()): path.relative_to(package).as_posix()
        for path in sorted(tests.rglob("*"))
        if path.is_file()
        and path.stat().st_size > 0
        and path.name != "test.sh"
        and path.suffix in CODE_SUFFIXES
    }


def scan_leaks(package: Path, entries: list[CopyEntry]) -> list[tuple[CopyEntry, list[str], str]]:
    """``(entry, leaked container paths, reason)`` for every COPY/ADD that brings grader material into the image."""
    hidden = _test_file_digests(package)
    leaks = []
    for entry in entries:
        named = any(
            fnmatch.fnmatch(entry.destination.rstrip("/") or "/", glob) for glob in LEAK_PATH_GLOBS
        ) or any(LEAK_WORDS.search(part) for part in (*entry.sources, entry.destination))
        if named:
            leaks.append(
                (entry, [c for c, _ in entry.files], "copies test/oracle/harness material by name")
            )
            continue
        dupes = [
            (container, hidden[_sha(path.read_bytes())])
            for container, path in entry.files
            if _sha(path.read_bytes()) in hidden
        ]
        if dupes:
            leaks.append(
                (
                    entry,
                    [c for c, _ in dupes],
                    "byte-identical to " + ", ".join(sorted({h for _, h in dupes})),
                )
            )
    return leaks


def scrub_dockerfile(text: str, leaks: list[tuple[CopyEntry, list[str], str]]) -> str:
    """Remove a fully leaked COPY/ADD; follow a partly leaked one with ``RUN rm -f`` of the leaked paths."""
    lines = text.splitlines(keepends=True)
    by_line = {entry.lineno: (entry, leaked) for entry, leaked, _ in leaks}
    out: list[str] = []
    skipping = False
    pending: str | None = None
    for number, line in enumerate(lines, start=1):
        continued = line.rstrip().endswith("\\")
        if skipping:
            skipping = continued
            continue
        if number in by_line:
            entry, leaked = by_line[number]
            if len(leaked) == len(entry.files):
                out.append(
                    f"# full-hardening@1 L4: removed `{entry.line.strip()}` (grader material)\n"
                )
                skipping = continued
                continue
            pending = "RUN rm -f " + " ".join(shlex.quote(p) for p in sorted(leaked)) + "\n"
        out.append(line)
        if pending and not continued:
            out.append(pending)
            pending = None
    return "".join(out)


def funnel_findings(package: Path) -> list[Finding]:
    path = package / "instruction.md"
    if not path.is_file():
        return []
    return [
        Finding(
            "instruction-funnel", f"instruction.md:{number}", line.strip(), "flagged; not rewritten"
        )
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if FUNNEL.search(line)
    ]


def _grader_lines(package: Path) -> list[str]:
    tests = package / "tests"
    return [
        line
        for path in sorted(tests.rglob("*"))
        if path.is_file()
        and (path.suffix in CODE_SUFFIXES or path.name == "test.sh")
        and BUNDLE_REL not in path.as_posix()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
    ]


def grader_paths(
    package: Path, workdir: str, files: dict[str, Path], declared: tuple[str, ...] = ()
) -> dict[str, str]:
    """Workspace paths the grader trusts: ``{container path: why}`` (L2).

    Only paths named on a line of ``tests/`` that executes something count (an existence check such as
    ``[ -f /app/pkg/decode.py ]`` names the submission, not a helper): a code file there is a grader helper, and a
    test-named directory there is a graded test tree. ``declared`` paths are added as given. ``/tests`` is
    verifier-owned and the workdir itself is never a candidate.
    """
    found: dict[str, str] = {}
    candidates = set()
    for line in _grader_lines(package):
        if EXEC_LINE.search(line) and not SHELL_TEST.search(line):
            candidates.update(re.findall(r"[A-Za-z0-9_./-]+", line))
    for token in sorted(candidates):
        if token.startswith(("-", "/tests", "/logs")):
            continue
        if "/" not in token and "." not in token and token not in TEST_DIR_NAMES:
            continue
        path = posixpath.normpath(
            token if token.startswith("/") else posixpath.join(workdir, token)
        )
        if not path.startswith(workdir + "/"):
            continue
        if path in files and path.endswith(CODE_SUFFIXES):
            found.setdefault(path, f"code file the grader executes ({token})")
        elif posixpath.basename(path) in TEST_DIR_NAMES:
            for container in files:
                if container.startswith(path + "/"):
                    found.setdefault(container, f"graded test directory the grader runs ({token})")
    for item in declared:
        path = posixpath.normpath(item if item.startswith("/") else posixpath.join(workdir, item))
        matched = [c for c in files if c == path or c.startswith(path + "/")]
        if not matched:
            raise HardeningRefused(f"declared helper {item} is not a file the agent image receives")
        for container in matched:
            found[container] = "declared helper"
    return found


def detect_runner(text: str) -> str | None:
    for runner in grade.RUNNERS:
        if grade.inject_report(text, runner)[1]:
            return runner
    return None


def _mimo_command_text(package: Path) -> tuple[str, str | None]:
    """Hidden test command text (the ``mimo_test_command.sh`` the patch adds, else ``test_command.sh``) and its path."""
    command = (package / "tests/test_command.sh").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(/\S*mimo_test_command\.sh)", command)
    if not match:
        return command, None
    added: list[str] = []
    capture = False
    for line in (
        (package / "tests/test.patch").read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        header = re.match(r"diff --git a/(\S+) b/", line)
        if header:
            capture = header.group(1) == "mimo_test_command.sh"
        elif capture and line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return "\n".join(added) + "\n", match.group(1)


def _marker(package: Path) -> str:
    patch = (package / "tests/test.patch").read_text(encoding="utf-8", errors="replace")
    for path in re.findall(r"^diff --git a/(\S+) b/", patch, re.M):
        if path != "mimo_test_command.sh" and "test" in posixpath.basename(path):
            return posixpath.basename(path)
    raise HardeningRefused(
        "no hidden test file in tests/test.patch to use as the separation marker"
    )


def _expected(
    runner: str | None,
    expected_tests: int | None,
    expected_skipped: int,
    calibrate: bool,
    allow_degraded: bool,
) -> dict[str, int] | None:
    if runner is None or calibrate:
        return None
    if expected_tests is None:
        if not allow_degraded:
            raise HardeningRefused(
                f"{runner} runner: the reference test count is required (--expected-tests, from a reference run "
                "with --calibrate); --allow-degraded accepts 'at least one passing test'"
            )
        return {"tests": 1, "skipped": 0}
    if expected_tests < 1 or expected_skipped < 0 or expected_skipped >= expected_tests:
        raise HardeningRefused(
            f"implausible reference report: tests={expected_tests} skipped={expected_skipped}"
        )
    return {"tests": expected_tests, "skipped": expected_skipped}


def _report_line(runner: str | None) -> str:
    suffix = REPORT_SUFFIX.get(runner or "", "json")
    return (
        "export EVALLAB_REPORT=\"$V/evallab-report-$$-$(od -An -N4 -tu4 /dev/urandom | tr -d ' \\n')."
        f'{suffix}"\n'
    )


_PY = "EVALLAB_PY=$(command -v python3 || command -v python)\n"
_GRADE = '"$EVALLAB_PY" -I -S /tests/_evallab/grade.py'


def harbor_wrapper(runner: str | None, l3: bool) -> str:
    """The general-package ``tests/test.sh``: L1+L2 prepare, the original grader (report-injected), the L3 gate."""
    report = _report_line(runner) if l3 else "export EVALLAB_REPORT=/dev/null\n"
    return (
        "#!/bin/bash\n"
        "# full-hardening@1 (evallab harden): L1 auto-load purge and L2 pristine restore, then the original grader\n"
        "# (tests/_evallab/orig_test.sh, runner asked for a structured report), then the L3 report gate.\n"
        "V=/logs/verifier\n"
        'mkdir -p "$V"\n' + _PY + report + 'rm -f "$V/reward.txt"\n'
        f'if ! {_GRADE} prepare --plan /tests/_evallab/plan.json --log "$V/evallab-prepare.json"; then\n'
        '  echo "evallab harden: could not prepare the workspace (testbed problem, not scored)" >&2; exit 1\n'
        "fi\n"
        "bash /tests/_evallab/orig_test.sh\n"
        'echo "original grader exited $?"\n'
        f'{_GRADE} finish --plan /tests/_evallab/plan.json --report "$EVALLAB_REPORT" || '
        '{ echo 0 > "$V/reward.txt"; echo "evallab harden: gate could not run (python3 >= 3.7 required); reward 0" >&2; }\n'
    )


def mimo_single_container_test_sh(
    parent_text: str, runner: str | None, script: str | None, l3: bool
) -> str:
    text = build_test_sh(parent_text, with_report_gate=False, l1_pattern=MIMO_L1_PATTERN)
    text = text.replace(
        "# grader-hardening@1 L1:", "# full-hardening@1 L1 (grader-hardening@1 block):", 1
    )
    if not l3:
        return text
    if text.count(MIMO_RUN_LINE) != 1 or text.count(MIMO_REWARD_LINE) != 1:
        raise HardeningRefused(
            "MiMo tests/test.sh does not have the upstream run and reward lines exactly once"
        )
    inject = (
        f"{_GRADE} inject --plan /tests/_evallab/plan.json --file {shlex.quote(script)}\n"
        if script
        else ""
    )
    before = (
        "# full-hardening@1 L3: the runner writes a structured report to a path chosen now (injected on its command\n"
        "# line, so a wrapper that clears PYTEST_ADDOPTS keeps it); reward needs the report\n"
        + _PY
        + _report_line(runner)
        + inject
    )
    after = (
        f'{_GRADE} finish --plan /tests/_evallab/plan.json --report "$EVALLAB_REPORT" || '
        '{ echo 0 > "$V/reward.txt"; echo "evallab harden: gate could not run (python3 >= 3.7 required); reward 0" >&2; }\n'
    )
    text = text.replace(MIMO_RUN_LINE, before + MIMO_RUN_LINE, 1)
    return text.replace(MIMO_REWARD_LINE, MIMO_REWARD_LINE + after, 1)


def _toml_separate(text: str, workdir: str) -> str:
    """Set ``[verifier] environment_mode = "separate"`` and carry the workdir as the only artifact."""
    if re.search(r"^artifacts\s*=", text, re.M):
        text = re.sub(
            r"^artifacts\s*=\s*\[", f'artifacts = ["{workdir}", ', text, count=1, flags=re.M
        )
    else:
        text = re.sub(
            r"^(schema_version\s*=.*\n)",
            rf'\1artifacts = ["{workdir}"]\n',
            text,
            count=1,
            flags=re.M,
        )
        if "artifacts = [" not in text:
            text = f'artifacts = ["{workdir}"]\n' + text
    if re.search(r"^\[verifier\]\s*$", text, re.M):
        return re.sub(
            r"^\[verifier\]\s*\n",
            '[verifier]\nenvironment_mode = "separate"\n',
            text,
            count=1,
            flags=re.M,
        )
    return text + '\n[verifier]\nenvironment_mode = "separate"\n'


def plan_hardening(
    package: Path | str,
    *,
    expected_tests: int | None = None,
    expected_skipped: int = 0,
    calibrate: bool = False,
    allow_degraded: bool = False,
    single_container: bool = False,
    helpers: tuple[str, ...] = (),
    verifier_dockerfile: Path | str | None = None,
) -> tuple[HardeningManifest, dict[str, bytes | None]]:
    """The manifest and the ``{relative path: bytes or None (delete)}`` changes; reads the package, writes nothing."""
    package = Path(package)
    shape = detect_shape(package)
    supplied_dockerfile = Path(verifier_dockerfile) if verifier_dockerfile is not None else None
    if supplied_dockerfile is not None and shape != "harbor":
        raise HardeningRefused(
            f"{TRANSFORM_ID_V10} owns the MiMo verifier image; --verifier-dockerfile applies to harbor packages"
        )
    if supplied_dockerfile is not None and not supplied_dockerfile.is_file():
        raise HardeningRefused(f"verifier Dockerfile is not a file: {supplied_dockerfile}")
    entries, docker_workdir = dockerfile_copies(package)
    workdir = _workdir(package, docker_workdir)
    findings = funnel_findings(package)
    changes: dict[str, bytes | None] = {}
    layers: list[Layer] = []

    leaks = scan_leaks(package, entries)
    if leaks:
        dockerfile = (package / "environment/Dockerfile").read_text(encoding="utf-8")
        changes["environment/Dockerfile"] = scrub_dockerfile(dockerfile, leaks).encode()
        kept_sources = {
            path
            for entry in entries
            if entry not in {e for e, _, _ in leaks}
            for _, path in entry.files
        }
        for entry, leaked, reason in leaks:
            full = len(leaked) == len(entry.files)
            for container, local in entry.files:
                if container not in leaked:
                    continue
                findings.append(
                    Finding(
                        "image-leak",
                        container,
                        f"{entry.line.strip()} (Dockerfile line {entry.lineno}): {reason}",
                        "COPY removed" if full else "deleted from the image after COPY",
                    )
                )
                if full and local not in kept_sources:
                    changes[local.relative_to(package).as_posix()] = None
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    already_separate = config.get("verifier", {}).get("environment_mode") == "separate"
    separate = already_separate or not single_container

    if shape == "mimo":
        command_text, script = _mimo_command_text(package)
        runner = detect_runner(command_text)
        expected = None
        if separate:
            try:
                sv_changes, sv_inputs = build_changes_v10(package, marker=_marker(package))
            except VariantInvalid as exc:
                raise HardeningRefused(f"{TRANSFORM_ID_V10} refused the package: {exc}") from exc
            changes.update(sv_changes)
            native = f"{TRANSFORM_ID_V10} patch-only grading in a pristine verifier checkout"
            layers += [
                Layer(
                    "L1",
                    "native",
                    native + ": test-infra and auto-load files dropped from the kept change",
                ),
                Layer("L2", "native", native + ": hidden tests re-applied on a pristine tree"),
                Layer(
                    "L3",
                    "native",
                    native + f": structured certification for runner {sv_inputs['runner']}",
                ),
            ]
        else:
            expected = _expected(
                runner, expected_tests, expected_skipped, calibrate, allow_degraded
            )
            parent_text = (package / "tests/test.sh").read_text(encoding="utf-8")
            if runner is not None and script is None:  # command given inline: inject at harden time
                injected, count = grade.inject_report(command_text, runner)
                changes["tests/test_command.sh"] = injected.encode()
            changes["tests/test.sh"] = mimo_single_container_test_sh(
                parent_text, runner, script, runner is not None
            ).encode()
            layers += [
                Layer(
                    "L1",
                    "applied",
                    "grader-hardening@1 L1 block over the git tree",
                    {"pattern": MIMO_L1_PATTERN},
                ),
                Layer(
                    "L2",
                    "native",
                    "MiMo reset list restores every hidden test file and the test command wrapper",
                ),
                Layer(
                    "L3",
                    "applied" if runner else "open",
                    f"{runner} structured report injected into {script or 'tests/test_command.sh'}"
                    if runner
                    else "no supported runner in the hidden test command; exit-status grading remains",
                    {"expected": expected, "calibration": calibrate},
                ),
            ]
            plan = {
                "workdir": workdir,
                "runner": runner,
                "expected": expected,
                "l1": False,
                "l3": bool(runner),
            }
            changes[f"{BUNDLE_REL}/plan.json"] = (
                json.dumps(plan, indent=2, sort_keys=True) + "\n"
            ).encode()
            changes[f"{BUNDLE_REL}/grade.py"] = Path(grade.__file__).read_bytes()
    else:
        files = {container: local for entry in entries for container, local in entry.files}
        if leaks:
            for _, leaked, _ in leaks:
                for container in leaked:
                    files.pop(container, None)
        test_sh = (package / "tests/test.sh").read_text(encoding="utf-8")
        runner = detect_runner(test_sh)
        expected = _expected(runner, expected_tests, expected_skipped, calibrate, allow_degraded)
        baseline = {
            container: _sha(local.read_bytes())
            for container, local in files.items()
            if container.startswith(workdir + "/")
            and grade.is_autoload(posixpath.basename(container))
        }
        pristine_why = grader_paths(package, workdir, files, helpers)
        pristine = {container: _sha(files[container].read_bytes()) for container in pristine_why}
        for container in baseline:
            changes[f"{BUNDLE_REL}/baseline/{container.lstrip('/')}"] = files[
                container
            ].read_bytes()
        for container in pristine:
            changes[f"{BUNDLE_REL}/pristine/{container.lstrip('/')}"] = files[
                container
            ].read_bytes()
        injected, count = grade.inject_report(test_sh, runner) if runner else (test_sh, 0)
        plan = {
            "workdir": workdir,
            "runner": runner,
            "expected": expected,
            "l1": True,
            "l3": bool(runner),
            "baseline": baseline,
            "pristine": pristine,
        }
        changes[f"{BUNDLE_REL}/plan.json"] = (
            json.dumps(plan, indent=2, sort_keys=True) + "\n"
        ).encode()
        changes[f"{BUNDLE_REL}/grade.py"] = Path(grade.__file__).read_bytes()
        changes[f"{BUNDLE_REL}/orig_test.sh"] = injected.encode()
        changes["tests/test.sh"] = harbor_wrapper(runner, bool(runner)).encode()
        layers += [
            Layer(
                "L1",
                "applied",
                "agent-created auto-load files deleted from the workdir; baseline files restored by sha256",
                {"baseline": sorted(baseline)},
            ),
            Layer(
                "L2",
                "applied" if pristine else "not-applicable",
                "graded tests and grader helpers in the workspace restored from tests/_evallab/pristine"
                if pristine
                else "the grader trusts no workspace test or helper file (tests run from verifier-owned /tests)",
                {"pristine": pristine_why},
            ),
            Layer(
                "L3",
                "applied" if runner else "open",
                f"{runner} structured report injected into {count} invocation(s) of the original grader"
                if runner
                else "no supported runner in tests/test.sh; the original reward stands",
                {"expected": expected, "calibration": calibrate},
            ),
        ]
        if separate and not already_separate:
            text = (package / "task.toml").read_text(encoding="utf-8")
            changes["task.toml"] = _toml_separate(text, workdir).encode()

    layers.append(
        Layer(
            "L4",
            "applied"
            if separate and not already_separate
            else ("native" if already_separate else "open"),
            (
                f"{TRANSFORM_ID_V10} separate verifier container"
                if shape == "mimo"
                else 'environment_mode = "separate"; the workdir is the only artifact'
            )
            if separate
            else "single-container grading requested: site-directory start-up hooks can reach the grader",
            {"image_leaks": sum(len(leaked) for _, leaked, _ in leaks)},
        )
    )
    planned_dockerfile = changes.get(VERIFIER_DOCKERFILE)
    if supplied_dockerfile is not None:
        planned_dockerfile = supplied_dockerfile.read_bytes()
        changes[VERIFIER_DOCKERFILE] = planned_dockerfile
        verifier = VerifierGate(
            "declared", VERIFIER_DOCKERFILE, _sha(planned_dockerfile), "flag"
        )
    elif isinstance(planned_dockerfile, bytes):
        verifier = VerifierGate(
            "declared", VERIFIER_DOCKERFILE, _sha(planned_dockerfile), "transform"
        )
    elif (package / VERIFIER_DOCKERFILE).is_file():
        verifier = VerifierGate(
            "declared",
            VERIFIER_DOCKERFILE,
            _sha((package / VERIFIER_DOCKERFILE).read_bytes()),
            "package",
        )
    elif separate:
        verifier = VerifierGate("unverified")
        print(UNVERIFIED_GATE_WARNING, file=sys.stderr)
    else:
        verifier = VerifierGate("native")
    manifest = HardeningManifest(
        transform=TRANSFORM_ID,
        shape=shape,
        source=str(package),
        source_digest=task_directory_digest(package),
        workdir=workdir,
        runner=runner,
        separate_verifier=separate,
        expected_report=expected,
        calibration=calibrate,
        layers=layers,
        findings=findings,
        verifier=verifier,
    )
    return manifest, changes


def harden(
    package: Path | str,
    out: Path | str | None,
    *,
    audit_only: bool = False,
    **options: Any,
) -> HardeningManifest:
    """Plan the hardening of ``package`` and, unless ``audit_only``, write the hardened copy to ``out``."""
    package = Path(package).resolve()
    manifest, changes = plan_hardening(package, **options)
    if audit_only:
        return manifest
    if out is None:
        raise HardeningRefused("--out is required unless --audit-only")
    target = Path(out).resolve()
    if target.exists():
        raise HardeningRefused(f"output {target} already exists")
    if target == package or package in target.parents:
        raise HardeningRefused("output must be outside the source package")
    shutil.copytree(package, target, symlinks=True)
    for path in (target, *target.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
    for rel, data in sorted(changes.items()):
        path = target / rel
        if data is None:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if rel.endswith((".sh", "grade.py")):
            path.chmod(0o755)
    for directory in sorted(
        (p for p in target.joinpath("environment").rglob("*") if p.is_dir()), reverse=True
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    manifest.output = str(target)
    manifest.output_digest = task_directory_digest(target)
    (target.parent / f"{target.name}.{MANIFEST_NAME}").write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


__all__ = [
    "TRANSFORM_ID",
    "Finding",
    "HardeningManifest",
    "HardeningRefused",
    "Layer",
    "VerifierGate",
    "detect_runner",
    "detect_shape",
    "dockerfile_copies",
    "grader_paths",
    "harden",
    "plan_hardening",
    "scan_leaks",
    "scrub_dockerfile",
]
