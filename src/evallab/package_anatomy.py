"""``PackageAnatomy``: deterministic grading and container attack-surface facts for one Harbor-style task package.

A Harbor / MiMo / Terminal-Bench task directory (``instruction.md``, ``task.toml``, ``environment/``, ``tests/``) is
parsed statically, in milliseconds and for $0, into structured vulnerability facts, so a bug hunter reads a card
instead of guessing from raw prose. Nothing runs, nothing is fetched, and no label, answer key or held-out split is
read: the only inputs are the package bytes and an optional, caller-supplied DEV reference patch.

The card has four sections:

* ``container_and_boundary``: single container vs separate verifier, what the agent image bakes in (tests, ``/opt``
  trees, ``.git`` history, solution artifacts, byte/line overlap with ``tests/``), network and root posture.
* ``grader_script_mechanics``: runner, which files ``test.sh`` resets vs which auto-load/hook files stay agent-writable,
  how reward is gated (bare ``$?``, structured report, log grep), and ``PYTEST_ADDOPTS``/``PYTHONPATH``/``PATH`` vectors.
* ``test_suite_rigor``: AST (Python) or regex (other languages) counts of tests, assertions, negative cases, literal
  input->output pairs, surface-only and mock-only checks; instruction-named symbols that no test mentions; and, given a
  reference patch, changed functions/branches no test exercises.
* ``vulnerability_hypotheses``: a ranked list tagged with ``MECHANISM_CLASSES``, with file/line evidence and a minimal
  differential witness.

Every hypothesis is static: ``confidence`` says whether the evidence is certain by construction
(``static_certain``), a heuristic (``static_probable``) or needs an execution to confirm (``needs_execution``).
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "evallab.package_anatomy/v1"
MECHANISM_CLASSES = ("grader_tamperable", "weak_check", "answer_reachable", "exploit_grader_gap")

_MAX_TEXT = 1_500_000
_MAX_FILES = 4000
_MAX_ARCHIVE = 8_000_000
_MAX_LIST = 12
_MAX_EVIDENCE = 6
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar")

_HOOK_CLASSES_BY_RUNNER = {
    "pytest": ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "sitecustomize.py",
               "usercustomize.py", "*.pth"),
    "unittest": ("sitecustomize.py", "usercustomize.py", "*.pth", "setup.cfg", "pyproject.toml"),
    "jest": ("jest.config.*", "package.json", "babel.config.*", ".babelrc"),
    "vitest": ("vitest.config.*", "package.json", "babel.config.*"),
    "mocha": (".mocharc*", "package.json"),
    "cargo": ("build.rs", ".cargo/config.toml", "Cargo.toml"),
    "go": ("go.work", "go.mod"),
    "make": ("Makefile",),
    "npm": ("package.json",),
}
_RUNNERS = (
    ("pytest", r"\b(?:py\.?test|pytest)\b"),
    ("unittest", r"\bunittest\b|\bnose2?\b"),
    ("vitest", r"\bvitest\b"),
    ("jest", r"\bjest\b"),
    ("mocha", r"\bmocha\b"),
    ("cargo", r"\bcargo\s+(?:test|nextest)\b"),
    ("go", r"\bgo\s+test\b"),
    ("rspec", r"\brspec\b"),
    ("phpunit", r"\bphpunit\b"),
    ("maven_gradle", r"\b(?:mvn|gradlew?)\b"),
    ("dotnet", r"\bdotnet\s+test\b"),
    ("ctest", r"\bctest\b"),
    ("tox", r"\btox\b"),
    ("make", r"\bmake\s+(?:test|check)\b"),
    ("npm", r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b"),
    ("node_test", r"\bnode\s+--test\b"),
    ("bats", r"\bbats\b"),
)
_TEST_LIKE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs)/|(?:^|/)test_[^/]*\.py$|_test\.(?:py|go|rs|js|ts|rb)$"
    r"|\.(?:test|spec)\.(?:js|ts|jsx|tsx|mjs)$|(?:^|/)conftest\.py$"
)
_SOLUTION_LIKE = re.compile(
    r"(?:^|/)(?:solution|solutions|solve\.sh|oracle|reference[_-]?(?:fix|solution|patch|impl)\w*"
    r"|golden\w*|gold_\w+|answers?(?:\.\w+)?|expected_?(?:output|answers?)\w*|leaked\S*)(?:/|$|\.)", re.I,
)
_STDLIB = frozenset(sys.stdlib_module_names)
_HELPER_IMPORT = re.compile(
    r"^(?:conftest|helpers?|utils?|fixtures?|support|common|testing|testlib|test_?(?:utils|helpers|support))$"
)
_SOURCE_SUFFIX = (".py", ".js", ".ts", ".rs", ".go", ".rb", ".java", ".c", ".cpp", ".sh")
_IDENT_STOP = frozenset((
    "true", "false", "none", "null", "int", "str", "float", "bool", "list", "dict", "set", "tuple", "bytes", "object",
    "type", "self", "cls", "print", "len", "range", "json", "yaml", "toml", "csv", "txt", "md", "py", "js", "ts", "rs",
    "go", "readme", "todo", "note", "example", "examples", "test", "tests", "pytest", "main", "init", "args", "kwargs",
    "return", "returns", "raise", "raises", "error", "errors", "exception", "value", "values", "key", "keys", "name",
    "names", "path", "paths", "file", "files", "string", "number", "numbers", "data", "result", "results", "input",
    "output", "outputs", "default", "defaults", "and", "or", "not", "the",
))


# --------------------------------------------------------------------------------------------------------------------
# data model


@dataclass(frozen=True)
class Hypothesis:
    id: str
    mechanism_class: str
    score: int
    confidence: str
    title: str
    evidence: tuple[dict[str, Any], ...]
    witness: str
    requires_reference: bool = False

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["evidence"] = list(self.evidence[:_MAX_EVIDENCE])
        return row


@dataclass
class Package:
    root: Path
    files: dict[str, str] = field(default_factory=dict)  # relative path -> decoded text
    sizes: dict[str, int] = field(default_factory=dict)  # every file (text or binary)
    digests: dict[str, str] = field(default_factory=dict)  # sha256 of text files and archive members
    git_dirs: list[str] = field(default_factory=list)
    truncated: bool = False

    def text(self, rel: str) -> str:
        return self.files.get(rel, "")

    def under(self, prefix: str) -> dict[str, str]:
        return {path: text for path, text in self.files.items() if path.startswith(prefix)}


@dataclass
class PatchFile:
    path: str
    new_file: bool
    deleted: bool
    hunks: list[dict[str, Any]]  # {"ctx": str, "lines": [(tag, text, patch_lineno)]}


@dataclass
class TestSource:
    __test__ = False  # not a pytest class
    path: str
    origin: str  # "file" | "patch"
    lines: list[tuple[int, str]]  # (line number in `path` for files / in test.patch for patch sources, text)
    complete: bool

    @property
    def text(self) -> str:
        return "\n".join(text for _, text in self.lines)


# --------------------------------------------------------------------------------------------------------------------
# package loading


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _expand_archive(rel: str, data: bytes, package: Package) -> None:
    if len(data) > _MAX_ARCHIVE:
        return
    try:
        if rel.endswith((".whl", ".zip")):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist()[:400]:
                    if not info.is_dir() and info.file_size <= _MAX_TEXT:
                        _add_member(package, f"{rel}!{info.filename}", archive.read(info))
        elif rel.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for info in archive.getmembers()[:400]:
                    if info.isfile() and info.size <= _MAX_TEXT:
                        handle = archive.extractfile(info)
                        if handle is not None:
                            _add_member(package, f"{rel}!{info.name}", handle.read())
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError):
        return


def _add_member(package: Package, rel: str, data: bytes) -> None:
    package.sizes[rel] = len(data)
    text = _decode(data)
    if text is not None:
        package.files[rel] = text
        package.digests[rel] = _sha(data)


def load_package(root: Path) -> Package:
    """Read every text file under ``root`` (bounded), expanding small archives under ``environment/``."""
    package = Package(root=root)
    count = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(directory)
        if ".git" in dirnames:
            dirnames.remove(".git")
            package.git_dirs.append(base.relative_to(root).as_posix() or ".")
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            count += 1
            if count > _MAX_FILES:
                package.truncated = True
                return package
            try:
                size = path.stat().st_size
            except OSError:
                continue
            package.sizes[rel] = size
            if path.is_symlink() or size > _MAX_TEXT * 4:
                continue
            data = path.read_bytes()
            text = _decode(data) if size <= _MAX_TEXT else None
            if text is not None:
                package.files[rel] = text
                package.digests[rel] = _sha(data)
            elif rel.startswith("environment/") and rel.endswith(_ARCHIVE_SUFFIXES):
                _expand_archive(rel, data, package)
    return package


# --------------------------------------------------------------------------------------------------------------------
# small helpers


def _find(text: str, pattern: str, *, flags: int = 0, limit: int = 3) -> list[tuple[int, str]]:
    regex = re.compile(pattern, flags)
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if regex.search(line):
            found.append((number, line.strip()[:200]))
            if len(found) >= limit:
                break
    return found


def _ev(file: str, line: int | None, text: str) -> dict[str, Any]:
    return {"file": file, "line": line, "text": text[:200]}


def _evidence(file: str, hits: Iterable[tuple[int, str]]) -> list[dict[str, Any]]:
    return [_ev(file, number, text) for number, text in hits]


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Shell/Dockerfile lines with ``\\`` continuations joined, keeping the first physical line number."""
    out: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not buffer:
            start = number
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        out.append((start, (buffer + stripped).strip()))
        buffer = ""
    if buffer:
        out.append((start, buffer.strip()))
    return out


def _shell_vars(text: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^(?:export\s+)?([A-Za-z_]\w*)=(\S*)\s*$", line.strip())
        if match:
            variables[match.group(1)] = match.group(2).strip("\"'")
    return variables


def _expand(value: str, variables: Mapping[str, str]) -> str:
    return re.sub(
        r"\$\{?([A-Za-z_]\w*)\}?", lambda m: variables.get(m.group(1), m.group(0)), value
    ).strip("\"'")


def _toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


# --------------------------------------------------------------------------------------------------------------------
# unified diffs


def parse_unified_diff(text: str) -> list[PatchFile]:
    files: list[PatchFile] = []
    current: PatchFile | None = None
    hunk: dict[str, Any] | None = None
    new_line = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = PatchFile(match.group(2) if match else "?", False, False, [])
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.new_file = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$", line)
            new_line = int(match.group(1)) if match else 0
            hunk = {"ctx": match.group(2) if match else "", "lines": [], "start": new_line, "patch_line": number}
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in {"+", "-", " "} and not line.startswith(("+++ ", "--- ")):
            hunk["lines"].append((line[0], line[1:], number))
    return files


def _patch_sources(patch_files: Sequence[PatchFile], patch_rel: str) -> tuple[list[TestSource], list[str]]:
    """Post-image of each patch file as a ``TestSource`` (only files that look like tests or scripts)."""
    sources: list[TestSource] = []
    scripts: list[str] = []
    for patch_file in patch_files:
        if patch_file.deleted or "build_env.tar" in patch_file.path:
            continue
        lines = [
            (number, text) for hunk in patch_file.hunks for tag, text, number in hunk["lines"] if tag in "+ "
        ]
        if patch_file.path.endswith(".sh") or "test_command" in patch_file.path:
            scripts.append("\n".join(text for _, text in lines))
        if (_TEST_LIKE.search(patch_file.path) or "test" in PurePosixPath(patch_file.path).name.lower()) \
                and not patch_file.path.endswith(".sh"):
            sources.append(TestSource(f"{patch_rel}:{patch_file.path}", "patch", lines, patch_file.new_file))
    return sources, scripts


# --------------------------------------------------------------------------------------------------------------------
# Dockerfile


@dataclass
class DockerInstruction:
    line: int
    op: str
    args: str


def parse_dockerfile(text: str) -> list[DockerInstruction]:
    out = []
    for number, logical in _logical_lines(text):
        if not logical or logical.startswith("#"):
            continue
        op, _, rest = logical.partition(" ")
        out.append(DockerInstruction(number, op.upper(), rest.strip()))
    return out


def _copy_operands(args: str) -> tuple[list[str], str, bool]:
    from_stage = bool(re.search(r"--from=", args))
    cleaned = re.sub(r"--\w+(?:=\S+)?\s*", "", args).strip()
    if cleaned.startswith("["):
        try:
            parts = [str(item) for item in json.loads(cleaned)]
        except json.JSONDecodeError:
            parts = cleaned.split()
    else:
        parts = cleaned.split()
    if len(parts) < 2:
        return [], "", from_stage
    return parts[:-1], parts[-1], from_stage


def baked_files(instructions: Sequence[DockerInstruction], env_paths: Iterable[str]) -> dict[str, tuple[str, int]]:
    """Image path -> (source path relative to the package, Dockerfile line) for every ``COPY``/``ADD`` from the context."""
    context = sorted(
        path.removeprefix("environment/") for path in env_paths if path.startswith("environment/")
    )
    workdir = "/"
    baked: dict[str, tuple[str, int]] = {}
    for item in instructions:
        if item.op == "WORKDIR":
            target = item.args.split()[0] if item.args else "/"
            workdir = target if target.startswith("/") else str(PurePosixPath(workdir) / target)
            continue
        if item.op not in {"COPY", "ADD"}:
            continue
        sources, dest, from_stage = _copy_operands(item.args)
        if from_stage or not sources:
            continue
        dest_abs = dest if dest.startswith("/") else str(PurePosixPath(workdir) / dest)
        multiple = len(sources) > 1 or dest.endswith("/")
        for source in sources:
            source = source.lstrip("./") if source not in {".", "./"} else "."
            matched: list[tuple[str, str]] = []  # (context path, relative-under-source)
            for path in context:
                if path in {"Dockerfile", ".dockerignore"} and source in {"."}:
                    continue
                if source == ".":
                    matched.append((path, path))
                elif path == source:
                    matched.append((path, PurePosixPath(path).name))
                elif path.startswith(source.rstrip("/") + "/"):
                    matched.append((path, path[len(source.rstrip("/")) + 1:]))
                elif any(ch in source for ch in "*?[") and fnmatch.fnmatch(path, source):
                    matched.append((path, PurePosixPath(path).name))
            for path, relative in matched:
                is_exact_file = path == source and not multiple
                target = dest_abs if is_exact_file else str(PurePosixPath(dest_abs) / relative)
                baked.setdefault(target, (f"environment/{path}", item.line))
    return baked


# --------------------------------------------------------------------------------------------------------------------
# container and boundary


def _task_toml_facts(package: Package) -> dict[str, Any]:
    document = _toml(package.text("task.toml"))
    verifier = document.get("verifier") or {}
    environment = document.get("environment") or {}
    return {
        "verifier": verifier,
        "environment": environment,
        "agent": document.get("agent") or {},
        "workdir": environment.get("workdir"),
        "network_mode": environment.get("network_mode"),
        "docker_image": environment.get("docker_image"),
        "healthcheck": (environment.get("healthcheck") or {}).get("command"),
    }


def _layout(package: Package, facts: Mapping[str, Any]) -> tuple[str, list[str]]:
    signals: list[str] = []
    verifier = facts["verifier"]
    if str(verifier.get("environment_mode", "")).lower() == "separate":
        signals.append("task.toml [verifier].environment_mode = separate")
    if isinstance(verifier.get("environment"), Mapping):
        signals.append("task.toml [verifier.environment] table")
    if "tests/Dockerfile" in package.sizes:
        signals.append("tests/Dockerfile")
    if "grader.toml" in package.sizes:
        signals.append("grader.toml")
    return ("separate_verifier" if signals else "single_container"), signals


_VALUE_FLAGS = frozenset({"-k", "-m", "-p", "-c", "-o", "-n", "--rootdir", "--timeout", "--maxfail", "--tb", "--junitxml",
                          "--import-mode", "--cov", "--ignore", "--deselect"})


def _runner_operands(line: str) -> list[str]:
    """Path-like operands after the test runner token (redirections, flags and flag values removed)."""
    line = re.sub(r"\d*>&?\d*\s*\S*|\|.*$", " ", line)
    tokens = line.split()
    start = next((i for i, t in enumerate(tokens) if re.fullmatch(r"(?:.*/)?(?:py\.?test|unittest|vitest|jest|mocha)", t.strip("\"'"))), None)
    if start is None:
        return []
    operands: list[str] = []
    skip = False
    for token in tokens[start + 1:]:
        if skip:
            skip = False
        elif token in _VALUE_FLAGS:
            skip = True
        elif token.startswith("-") or "$" in token or token in {"&&", ";", "||"}:
            continue
        else:
            operands.append(token.strip("\"'"))
    return operands


def _is_executed_from_image(script: str, baked_tests: Sequence[str]) -> list[str]:
    """Baked test-like files the grader command would run: bare discovery, a relative path, or a baked basename."""
    if not baked_tests or not script:
        return []
    runs = [line for _, line in _logical_lines(script) if re.search(r"\b(?:py\.?test|unittest|vitest|jest|mocha)\b", line)]
    executed: list[str] = []
    for line in runs:
        operands = _runner_operands(line)
        if any(token.startswith("/tests") for token in operands):
            continue
        if not operands:
            executed.extend(baked_tests)  # bare discovery runs everything it finds under cwd
            continue
        for baked in baked_tests:
            for token in operands:
                clean = token.strip("./")
                if clean and (baked.endswith("/" + clean) or f"/{clean}/" in baked or PurePosixPath(baked).name == PurePosixPath(clean).name):
                    executed.append(baked)
    return sorted(set(executed))


_LINE_NOISE = re.compile(r"^[\W_]*$")


def _line_set(text: str) -> set[str]:
    return {line for raw in text.splitlines() if len(line := raw.strip()) >= 6 and not _LINE_NOISE.match(line)}


def _overlap(package: Package, baked: Mapping[str, tuple[str, int]]) -> list[dict[str, Any]]:
    """Baked image files whose bytes or distinct lines reappear in ``tests/`` (the verifier-only side)."""
    test_files = {
        path: text for path, text in package.files.items()
        if path.startswith("tests/") and path != "tests/test.sh" and not path.endswith("Dockerfile")
    }
    by_digest = {package.digests[p]: p for p in test_files if p in package.digests}
    test_lines = {path: _line_set(text) for path, text in test_files.items()}
    found: dict[str, dict[str, Any]] = {}
    for target, (source, line) in baked.items():
        digest = package.digests.get(source)
        if digest and digest in by_digest and package.sizes.get(source, 0) > 0:
            found[source] = {"image_path": target, "source": source, "dockerfile_line": line,
                             "tests_file": by_digest[digest], "kind": "identical_bytes"}
            continue
        text = package.files.get(source)
        if not text or len(text) > 400_000:
            continue
        baked_lines = _line_set(text)
        if len(baked_lines) < 20:
            continue
        for path, lines in test_lines.items():
            shared = len(baked_lines & lines)
            if shared >= 40 and shared >= 0.3 * min(len(baked_lines), len(lines)):
                found[source] = {"image_path": target, "source": source, "dockerfile_line": line,
                                 "tests_file": path, "shared_lines": shared, "kind": "shared_lines"}
                break
    # archives expanded under environment/: compare their members too
    for source, text in package.files.items():
        if "!" not in source or not source.startswith("environment/") or source in found:
            continue
        member_lines = _line_set(text)
        if len(member_lines) < 20:
            continue
        for path, lines in test_lines.items():
            shared = len(member_lines & lines)
            if shared >= 40 and shared >= 0.3 * min(len(member_lines), len(lines)):
                archive = source.split("!", 1)[0]
                dest = next((t for t, (s, _) in baked.items() if s == archive or s.startswith(archive)), None)
                found[source] = {"image_path": dest, "source": source, "tests_file": path,
                                 "shared_lines": shared, "kind": "shared_lines_in_archive"}
                break
    return list(found.values())


_RUN_FLAGS = (
    ("git_clone", r"\bgit\s+clone\b"),
    ("git_fetch_all", r"\bgit\s+(?:fetch|pull)\b"),
    ("git_tag_or_remote", r"\bgit\s+(?:tag|remote\s+add|checkout\s+-b)\b"),
    ("pip_install", r"\b(?:pip3?|uv\s+pip)\s+install\b"),
    ("download", r"\b(?:curl|wget)\b"),
    ("chmod_world_writable", r"\bchmod\s+(?:-R\s+)?(?:a\+w|777|666|o\+w)\b"),
    ("rm_git", r"rm\s+-rf?\s+\S*\.git\b"),
    ("sudo_or_su", r"\b(?:sudo|su\s+-)\b"),
)


def container_and_boundary(package: Package) -> tuple[dict[str, Any], dict[str, Any]]:
    facts = _task_toml_facts(package)
    layout, signals = _layout(package, facts)
    docker_text = package.text("environment/Dockerfile")
    instructions = parse_dockerfile(docker_text)
    baked = baked_files(instructions, package.files.keys() | package.sizes.keys()) if instructions else {}
    workdir = facts["workdir"]
    user = "root (no USER instruction)"
    base = None
    for item in instructions:
        if item.op == "FROM" and base is None:
            base = re.sub(r"--\w+=\S+\s*", "", item.args).split()[0] if item.args else None
        elif item.op == "WORKDIR" and not workdir:
            workdir = item.args.split()[0]
        elif item.op == "USER":
            user = item.args
    runs: list[dict[str, Any]] = []
    for item in instructions:
        if item.op == "RUN":
            tags = [name for name, pattern in _RUN_FLAGS if re.search(pattern, item.args)]
            if tags:
                runs.append({"line": item.line, "tags": tags, "text": item.args[:160]})
    baked_test_like = sorted(path for path in baked if _TEST_LIKE.search(path))
    solution_like = sorted(
        {path for path in baked if _SOLUTION_LIKE.search(path)}
        | {p for p in package.sizes if p.startswith("environment/") and _SOLUTION_LIKE.search(p)}
    )
    outside = sorted(
        path for path in baked
        if re.match(r"^/(?:opt|srv|usr/local/share|usr/share|var|root|home|etc|tmp|mnt|data)/", path)
        and not path.startswith("/var/lib/mimo")
    )
    env_paths = [p for p in package.sizes if p.startswith("environment/")]
    git_dirs = [p for p in package.git_dirs if p.startswith("environment")]
    uses_context_git = any(re.search(r"\.git(?:/|$)", source) for source, _ in baked.values())
    blocklist = package.text("environment/setup/files/blocklist")
    blocked = sorted({m.group(1) for m in re.finditer(r"^\S+\s+(\S+)", blocklist, re.M)})
    setup_sh = package.text("environment/setup/setup.sh")
    boundary = {
        "layout": layout,
        "separate_verifier_signals": signals,
        "agent_image": {
            "dockerfile_present": bool(instructions),
            "base_image": base or facts["docker_image"],
            "base_pinned_by_digest": bool(re.search(r"@sha256:[0-9a-f]{64}", base or facts["docker_image"] or "")),
            "opaque_prebuilt_image": bool(facts["docker_image"]) or (bool(base) and not baked),
            "workdir": workdir,
            "user": user,
            "copy_count": len(baked),
            "baked_test_like_files": baked_test_like[:_MAX_LIST],
            "baked_solution_like_files": solution_like[:_MAX_LIST],
            "baked_outside_workdir": outside[:_MAX_LIST],
            "tests_baked_identical_or_overlapping": _overlap(package, baked)[:_MAX_LIST] if baked else [],
            "run_audit": runs[:_MAX_LIST],
            "git": {
                "git_dir_in_build_context": bool(git_dirs),
                "copies_git_history": uses_context_git or bool(git_dirs),
                "setup_hides_history_when_not_truncated": "git-hidden" in setup_sh,
                "clone_or_fetch_in_run": any({"git_clone", "git_fetch_all"} & set(r["tags"]) for r in runs),
                "history_removed_in_run": any("rm_git" in r["tags"] for r in runs),
            },
            "writable_system_site_packages": (
                user.startswith("root")
                and bool(re.search(r"\b(?:python|pypy)", base or facts["docker_image"] or "", re.I)
                         or any("pip_install" in r["tags"] for r in runs) or "docker.io/" in (facts["docker_image"] or ""))
            ),
        },
        "network": {
            "network_mode": facts["network_mode"],
            "blocklist_domains": len(blocked),
            "blocklist_mechanism": "/etc/hosts entries applied by the agent harness" if blocked else None,
            "github_hosts_blocklisted": any(host.endswith("github.com") for host in blocked),
            "proxy_or_firewall_in_dockerfile": bool(_find(docker_text, r"(?i)\b(?:https?_proxy|iptables|nftables|squid)\b")),
            "build_time_downloads": sum(1 for r in runs if {"download", "pip_install"} & set(r["tags"])),
        },
        "root_posture": {
            "agent_user": user,
            "verifier_user": facts["verifier"].get("user"),
            "logs_verifier_world_writable": "chmod 777" in setup_sh and "/logs/verifier" in setup_sh,
        },
        "package_files": {
            "total": len(package.sizes),
            "environment": len(env_paths),
            "tests": sum(1 for p in package.sizes if p.startswith("tests/")),
            "solution_dir_present": any(p.startswith("solution/") for p in package.sizes),
            "truncated": package.truncated,
        },
    }
    context = {"facts": facts, "baked": baked, "instructions": instructions, "blocked": blocked, "workdir": workdir}
    return boundary, context


# --------------------------------------------------------------------------------------------------------------------
# grader script mechanics


def _runner_of(*texts: str) -> tuple[str, list[str]]:
    hits: list[tuple[int, str]] = []
    blob = "\n".join(texts)
    for name, pattern in _RUNNERS:
        match = re.search(pattern, blob)
        if match:
            hits.append((match.start(), name))
    hits.sort()
    names = [name for _, name in hits]
    if names:
        return names[0], names
    if re.search(r"\bpython3?\s+\S+\.py\b|\bbash\s+\S+\.sh\b|\bsh\s+\S+\.sh\b", blob):
        return "custom_script", []
    return "unknown", []


def _reset_list(test_sh: str) -> list[str]:
    match = re.search(r"<<\s*'?(EOF_RESET_TEST_FILES|EOF_RESET\w*)'?\n(.*?)\n\1", test_sh, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(2).splitlines() if line.strip()]


def _reward_gate(test_sh: str, sources: Sequence[TestSource]) -> dict[str, Any]:
    mechanisms: list[str] = []
    reward_lines = _find(test_sh, r"reward\.(?:txt|json)", limit=6)
    if re.search(r"\$\?|\$\{?RC\}?|\bRC=|\bif\s+\S.*;\s*then\s+echo\s+1|&&\s*echo\s+1", test_sh):
        mechanisms.append("exit_code")
    if re.search(r"junit|--json-report|ctrf|report\.json|expected_report|jq\s|xml\.etree|passed.*failed", test_sh, re.I):
        mechanisms.append("structured_report")
    if re.search(r"grep\s+-[a-zA-Z]*q[a-zA-Z]*\s+['\"]?(?:PASS|passed|OK|ok\b|✓)", test_sh, re.I):
        mechanisms.append("log_grep")
    if any(re.search(r"reward\.(?:txt|json)", source.text) for source in sources):
        mechanisms.append("python_writes_reward")
    unconditional = False
    depth = 0
    for number, raw in enumerate(test_sh.splitlines(), 1):
        stripped = raw.strip()
        if re.match(r"^(?:if|case|while|for)\b", stripped):
            depth += 1
        if re.match(r"^(?:fi|esac|done)\b", stripped):
            depth = max(0, depth - 1)
        if re.search(r"\becho\s+1\s*>.*reward\.", stripped) and depth == 0 and "&&" not in stripped and "||" not in stripped \
                and not re.search(r"\bif\b", stripped):
            unconditional = True
            reward_lines.append((number, stripped[:200]))
    masks: list[tuple[int, str]] = []
    masks += _find(test_sh, r"(?:pytest|unittest|jest|mocha|cargo|go test|vitest)[^\n]*\|\|\s*true")
    masks += _find(test_sh, r"^\s*set\s+\+e\b")
    pipe = _find(test_sh, r"(?:pytest|unittest|jest|mocha|cargo test|go test|vitest)[^\n|]*\|\s*(?:tee|tail|head|grep|cat)\b")
    if pipe and "pipefail" not in test_sh:
        masks += pipe
    if re.search(r"\becho\s+1\b[^\n]*reward", test_sh) and not mechanisms:
        mechanisms.append("exit_code")
    return {
        "mechanisms": mechanisms or ["none_detected"],
        "reward_write_lines": [_ev("tests/test.sh", n, t) for n, t in reward_lines[:6]],
        "reward_one_unconditional": unconditional,
        "failure_masks": [_ev("tests/test.sh", n, t) for n, t in masks[:6]],
        "structured_report_count_check": "structured_report" in mechanisms,
    }


def _env_vectors(test_sh: str, test_cmd: str, variables: Mapping[str, str]) -> dict[str, Any]:
    blob = test_sh + "\n" + test_cmd
    addopts_lines = _find(blob, r"PYTEST_ADDOPTS")
    isolation = {
        "env_i": bool(re.search(r"\benv\s+-i\b", blob)),
        "python_isolated": bool(re.search(r"\bpython3?\s+(?:-\w*[IES]\w*)\b", blob)),
        "noconftest": "--noconftest" in blob,
        "config_nulled": bool(re.search(r"-c\s+/dev/null", blob)),
        "plugin_autoload_disabled": "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in blob,
        "no_cacheprovider": "no:cacheprovider" in blob,
        "importlib_mode": "--import-mode=importlib" in blob,
    }
    bare = sorted({m.group(1) for m in re.finditer(r"(?:^|[;&|(\s])(pytest|python3?|node|npm|cargo|go|ruby)\s", blob, re.M)})
    path_reset = bool(re.search(r"\bPATH=\S*/usr", blob)) or isolation["env_i"]
    pythonpath = _find(blob, r"\bPYTHONPATH=")
    deselect = _find(blob, r"(?:pytest[^\n]*(?:-k\s+[\"'][^\"']*\bnot\s|--deselect\b|--ignore(?:-glob)?\b|-m\s+[\"']not\s|--lf\b|\s-x\b)|\bxfail\b)")
    return {
        "pytest_addopts": {
            "mentioned": bool(addopts_lines),
            "cleared_or_overridden": bool(re.search(r"(?:unset\s+PYTEST_ADDOPTS|PYTEST_ADDOPTS=(?:\"\"|''|\s))", blob)),
            "lines": _evidence("tests/test.sh", addopts_lines),
        },
        "isolation_flags": isolation,
        "path_hijack": {
            "bare_commands": bare,
            "path_reset": path_reset,
            "exposed": bool(bare) and not path_reset,
        },
        "pythonpath": [_ev("tests/test.sh", n, t) for n, t in pythonpath],
        "deselect_or_ignore": [_ev("tests/test.sh or test_command.sh", n, t) for n, t in deselect],
        "workdir_in_script": variables.get("CWD") or _find(blob, r"^cd\s+(\S+)")[:1] or None,
    }


def _executes_agent_script(
    test_sh: str, sources: Sequence[TestSource], baked: Mapping[str, tuple[str, int]]
) -> list[dict[str, Any]]:
    """Scripts or modules the verifier runs/imports that live in the agent-writable image tree."""
    checker = re.compile(r"check|verif|validat|grader|grade|attest|audit|oracle|harness|judge|scor|evaluat", re.I)
    baked_names = {PurePosixPath(path).name: path for path in baked if checker.search(PurePosixPath(path).name)}
    baked_modules = {
        PurePosixPath(path).stem: path
        for path in baked
        if path.endswith(".py") and not _TEST_LIKE.search(path) and checker.search(PurePosixPath(path).stem)
    }
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        if source.origin == "patch":
            continue
        spawns = bool(re.search(r"subprocess|Popen|check_output|os\.system", source.text))
        for number, text in source.lines:
            if spawns:
                for literal in re.findall(r"[\"']([\w./-]+\.(?:py|sh|js|rb))[\"']", text):
                    name = PurePosixPath(literal).name
                    if name in baked_names and (source.path, name) not in seen:
                        seen.add((source.path, name))
                        hits.append({"kind": "subprocess_runs_baked_script", "script": baked_names[name],
                                     **_ev(source.path, number, text)})
            match = re.match(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", text)
            if match and match.group(1) in baked_modules and (source.path, match.group(1)) not in seen:
                seen.add((source.path, match.group(1)))
                hits.append({"kind": "imports_baked_module", "script": baked_modules[match.group(1)],
                             **_ev(source.path, number, text)})
    for number, text in _find(test_sh, r"\b(?:bash|sh|python3?|node)\s+(?!-)\S+|\./\S+", limit=20):
        for token in re.findall(r"(?:bash|sh|python3?|node)\s+(\S+)|(\./\S+)", text):
            literal = (token[0] or token[1]).strip("\"'")
            name = PurePosixPath(literal).name
            if literal.startswith("/tests") or name == "test.sh" or "$(" in literal:
                continue
            if name in baked_names and (literal, "sh") not in seen:
                seen.add((literal, "sh"))
                hits.append({"kind": "test_sh_runs_baked_script", "script": baked_names[name],
                             **_ev("tests/test.sh", number, text)})
    return hits[:_MAX_LIST]


def grader_script_mechanics(package: Package, sources: Sequence[TestSource], patch_scripts: Sequence[str],
                            context: Mapping[str, Any]) -> dict[str, Any]:
    test_sh = package.text("tests/test.sh") or package.text("tests/run-tests.sh") or package.text("tests/run_tests.sh")
    entry = next((p for p in ("tests/test.sh", "tests/run-tests.sh", "tests/run_tests.sh") if p in package.files), None)
    test_cmd = package.text("tests/test_command.sh")
    other_scripts = "\n".join(text for path, text in package.files.items()
                              if path.startswith("tests/") and path.endswith(".sh") and path != entry)
    runner, runners = _runner_of(test_sh, test_cmd, *patch_scripts, other_scripts)
    variables = _shell_vars(test_sh)
    reset = _reset_list(test_sh)
    touched = sorted({s.path.split(":", 1)[1] for s in sources if s.origin == "patch"})
    patterns_reset = bool(re.search(r"conftest|sitecustomize|AUTOLOAD|autoload", test_sh))
    hooks = _HOOK_CLASSES_BY_RUNNER.get(runner, ())
    cwd = _expand(variables.get("CWD", ""), variables) or None
    if not cwd:
        match = re.search(r"^cd\s+(\S+)", test_sh, re.M)
        cwd = _expand(match.group(1), variables) if match else None
    agent_workdir = context["workdir"]
    runs_in_agent_tree = bool(cwd and agent_workdir and cwd.rstrip("/") == str(agent_workdir).rstrip("/")) or (
        bool(cwd) and cwd in {"/testbed", "/app", "/workspace", "/repo", "/src"}
    )
    restores_from_outside = bool(re.search(r"git\s+checkout\s+\S+\s+--|cp\s+(?:-r\s+)?/tests/|rsync\b.*?/tests/|git\s+apply", test_sh))
    baked_tests = sorted(p for p in context["baked"] if _TEST_LIKE.search(p))
    executed = _is_executed_from_image(test_sh + "\n" + test_cmd, baked_tests)
    early_exits = _find(test_sh, r"\bexit\s+[1-9]\b|\|\|\s*exit\b|\|\|\s*\{[^}]*exit", limit=6)
    clears_reward = bool(re.search(r"rm\s+-f\s+\S*reward|:\s*>\s*\S*reward", test_sh))
    return {
        "entrypoint": entry,
        "runner": runner,
        "runners_detected": runners,
        "layout": "separate" if context.get("layout_separate") else "single",
        "cwd": cwd,
        "runs_in_agent_workdir": runs_in_agent_tree,
        "reset_list": {
            "enumerated_files": reset[:_MAX_LIST],
            "enumerated_count": len(reset),
            "files_touched_by_hidden_patch": touched[:_MAX_LIST],
            "autoload_reset_by_pattern": patterns_reset,
            "restores_from_outside_agent_tree": restores_from_outside,
            "autoload_hook_classes_left_writable": [] if patterns_reset else list(hooks),
            "helper_modules_not_shipped_by_patch": _unshipped_helpers(sources, touched),
        },
        "reward_gate": _reward_gate(test_sh, sources),
        "env_vectors": _env_vectors(test_sh, "\n".join([test_cmd, *patch_scripts]), variables),
        "executes_agent_writable_code": _executes_agent_script(test_sh, sources, context["baked"]),
        "baked_tests_executed": executed[:_MAX_LIST],
        "early_exit_paths": [_ev("tests/test.sh", n, t) for n, t in early_exits],
        "clears_reward_file_first": clears_reward,
    }


def _unshipped_helpers(sources: Sequence[TestSource], touched: Sequence[str]) -> list[dict[str, Any]]:
    shipped = {PurePosixPath(path).stem for path in touched}
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        if source.origin != "patch":
            continue
        for number, text in source.lines:
            match = re.match(r"^\s*(?:from|import)\s+((?:[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*)", text)
            if not match:
                continue
            module = match.group(1)
            parts = module.split(".")
            leaf = parts[-1]
            if parts[0] in _STDLIB or module in seen:
                continue
            if (_HELPER_IMPORT.match(parts[0]) or _HELPER_IMPORT.match(leaf)) and leaf not in shipped:
                seen.add(module)
                found.append({"module": module, **_ev(source.path, number, text)})
    return found[:_MAX_LIST]


# --------------------------------------------------------------------------------------------------------------------
# test-suite rigor


@dataclass
class Counts:
    files: int = 0
    tests: int = 0
    assertions: int = 0
    negative_assertions: int = 0
    negative_named_tests: int = 0
    literal_eq_assertions: int = 0
    surface_assertions: int = 0
    source_text_assertions: int = 0
    mock_assertions: int = 0
    tests_without_assertion: int = 0
    parametrized_cases: int = 0
    randomized_or_property: int = 0
    loops_with_assertions: int = 0
    skipped_or_xfail: int = 0
    mode_ast_files: int = 0
    mode_regex_files: int = 0

    def add(self, other: Counts) -> None:
        for key, value in asdict(other).items():
            setattr(self, key, getattr(self, key) + value)


_NEG_NAME = re.compile(r"invalid|error|fail|reject|raise|bad|missing|empty|malformed|negative|unsupported|illegal|none|unknown|corrupt", re.I)


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _is_literal(key) and _is_literal(value) for key, value in zip(node.keys, node.values, strict=True))
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


_RAISES = {"raises", "assertRaises", "assertRaisesRegex", "assertWarns"}
_UNITTEST_NEGATIVE = {"assertRaises", "assertRaisesRegex", "assertFalse", "assertIsNone", "assertNotEqual", "assertNotIn"}
_UNITTEST_EQUAL = {"assertEqual", "assertEquals", "assertListEqual", "assertDictEqual", "assertTupleEqual"}
_SURFACE_CALLS = {"exists", "isfile", "isdir", "is_file", "is_dir", "hasattr", "callable", "isinstance"}


def _reads_source(func: ast.AST) -> bool:
    """Whether the test reads source text (``inspect.getsource`` or a ``.py``/``.js`` file) to assert on it."""
    calls = {_call_name(n) for n in ast.walk(func) if isinstance(n, ast.Call)}
    if calls & {"getsource", "getsourcelines"}:
        return True
    names_source_file = any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith(_SOURCE_SUFFIX)
        for n in ast.walk(func)
    )
    return names_source_file and bool(calls & {"read_text", "read"})


def _count_assert(node: ast.Assert, counts: Counts, *, reads_source: bool) -> None:
    counts.assertions += 1
    test = node.test
    negative = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    surface = False
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, left, right = test.ops[0], test.left, test.comparators[0]
        zero = isinstance(right, ast.Constant) and right.value == 0 and not isinstance(right.value, bool)
        if isinstance(op, ast.NotEq) and zero:
            negative = True
        if isinstance(op, ast.Eq) and isinstance(left, ast.Call) and _call_name(left) == "len" and zero:
            negative = True
        if isinstance(op, ast.Eq) and _is_literal(right) != _is_literal(left):
            counts.literal_eq_assertions += 1
        if isinstance(op, ast.IsNot) and isinstance(right, ast.Constant) and right.value is None:
            surface = True
        if isinstance(op, ast.In) and isinstance(left, ast.Constant) and reads_source:
            counts.source_text_assertions += 1
            surface = True
    elif isinstance(test, ast.Call):
        surface = _call_name(test) in _SURFACE_CALLS
    elif isinstance(test, (ast.Name, ast.Attribute)):
        surface = True
    counts.negative_assertions += negative
    counts.surface_assertions += surface


def _count_unittest_call(node: ast.Call, attr: str, counts: Counts, *, reads_source: bool) -> bool:
    """Count one ``self.assertX``/mock-assert call; returns whether it was an assertion."""
    if attr.startswith("assert_called") or attr in {"assert_any_call", "assert_not_called", "assert_has_calls"}:
        counts.assertions += 1
        counts.mock_assertions += 1
        return True
    if not attr.startswith("assert") or attr == "assert_":
        return False
    counts.assertions += 1
    counts.negative_assertions += attr in _UNITTEST_NEGATIVE
    if attr in _UNITTEST_EQUAL and len(node.args) >= 2 and _is_literal(node.args[0]) != _is_literal(node.args[1]):
        counts.literal_eq_assertions += 1
    if attr in {"assertTrue", "assertIsNotNone", "assertIsInstance"}:
        counts.surface_assertions += 1
    if attr == "assertIn" and reads_source:
        counts.source_text_assertions += 1
        counts.surface_assertions += 1
    return True


def _ast_function_counts(func: ast.AST, counts: Counts, helpers: frozenset[str] | set[str] = frozenset()) -> None:
    reads_source = _reads_source(func)
    before = counts.assertions
    for node in ast.walk(func):
        if isinstance(node, ast.With):
            for item in node.items:
                if _call_name(item.context_expr) in _RAISES:
                    counts.assertions += 1
                    counts.negative_assertions += 1
        elif isinstance(node, (ast.For, ast.While)):
            counts.loops_with_assertions += any(isinstance(n, ast.Assert) for n in ast.walk(node))
        elif isinstance(node, ast.Assert):
            _count_assert(node, counts, reads_source=reads_source)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            _count_unittest_call(node, node.func.attr, counts, reads_source=reads_source)
    asserted = counts.assertions - before
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    calls = {_call_name(n) for n in ast.walk(func) if isinstance(n, ast.Call)}
    delegated = calls & helpers
    counts.assertions += len(delegated)  # an assertion helper counts once per distinct helper a test calls
    if asserted == 0 and not delegated and not calls & {"fail", "check_call", "check_output"}:
        counts.tests_without_assertion += 1
    if _NEG_NAME.search(func.name):
        counts.negative_named_tests += 1
    for decorator in func.decorator_list:
        name = _call_name(decorator)
        if name == "parametrize" and isinstance(decorator, ast.Call) and len(decorator.args) >= 2 \
                and isinstance(decorator.args[1], (ast.List, ast.Tuple)):
            counts.parametrized_cases += len(decorator.args[1].elts)
        if name in {"skip", "skipif", "xfail"}:
            counts.skipped_or_xfail += 1
        if name == "given":
            counts.randomized_or_property += 1


def _py_counts(source: TestSource) -> Counts | None:
    if not source.path.split(":")[-1].endswith(".py"):
        return None
    try:
        tree = ast.parse(source.text)
    except (SyntaxError, ValueError):
        return None
    counts = Counts(files=1, mode_ast_files=1)
    helpers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test")
        and any(isinstance(n, ast.Assert) for n in ast.walk(node))
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            counts.tests += 1
            _ast_function_counts(node, counts, helpers)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            if any(re.match(r"hypothesis|random|faker|numpy\.random", name or "") for name in names):
                counts.randomized_or_property += 1
    return counts


_REGEX_COUNTERS: dict[str, str] = {
    "tests": r"^\s*(?:async\s+)?def\s+test\w*\(|\b(?:it|test)(?:\.each[^(]*)?\s*\(\s*[\"'`]|^func\s+Test\w+\(|#\[test\]|@Test\b|^@test\b",
    "assertions": r"^\s*assert\b|\bself\.assert\w+\(|\bexpect\(|\bt\.(?:Error|Errorf|Fatal|Fatalf)\(|\b(?:assert|require)\.\w+\(|\bassert(?:_eq|_ne)?!|\bassert(?:That|Equals?|True|False)\(",
    "negative_assertions": r"pytest\.raises|assertRaises|toThrow|\.rejects\b|should_panic|assertThrows|\.not\.to|assert\s+not\b|assertFalse|returncode\s*!=\s*0",
    "literal_eq_assertions": r"assert\s+.+==\s*(?:[\"'\[{(\-\d]|True\b|False\b|None\b)|\.(?:toBe|toEqual|toStrictEqual)\(\s*(?:[\"'`\[{\-\d]|true\b|false\b|null\b)|assert_eq!\(.*,\s*(?:\"|\d|vec!|\[)|assert\.Equal\(t,\s*[^,]+,\s*(?:\"|\d)",
    "surface_assertions": r"(?:assert|expect).*(?:os\.path\.(?:exists|isfile|isdir)|\.exists\(\)|is_file\(\)|toBeDefined|toBeTruthy|toBeInstanceOf|hasattr\(|callable\(|is not None|isinstance\()",
    "source_text_assertions": r"getsource|readFileSync\([^)]*\.(?:js|ts)|read_text\(\)[^\n]*\.py",
    "mock_assertions": r"assert_called|toHaveBeenCalled|assert_any_call|\bverify\(",
    "randomized_or_property": r"hypothesis|@given\b|\brandom\.|\bfaker\b|fast-check|fc\.assert|proptest|quickcheck|\brand::",
    "skipped_or_xfail": r"pytest\.mark\.(?:skip|xfail)|@unittest\.skip|\bxit\(|\.skip\(|#\[ignore\]|t\.Skip\(",
}


def _regex_counts(source: TestSource) -> Counts:
    counts = Counts(files=1, mode_regex_files=1)
    for _, text in source.lines:
        for name, pattern in _REGEX_COUNTERS.items():
            if re.search(pattern, text):
                setattr(counts, name, getattr(counts, name) + 1)
        if re.search(r"^\s*(?:async\s+)?def\s+test\w*(?:invalid|error|fail|reject|raise|bad|missing|empty)\w*\(", text, re.I):
            counts.negative_named_tests += 1
    return counts


def _build_env_command(patch_text: str) -> str:
    """Decoded ``test_command.sh`` of a MiMo ``mimo_build_env`` bundle (the grader-hardening decoder, imported lazily)."""
    if "build_env.tar.gz.b64" not in patch_text:
        return ""
    from evallab.grader_hardening import build_env_command

    return build_env_command(patch_text)


def _test_sources(package: Package) -> tuple[list[TestSource], list[str], list[PatchFile]]:
    sources: list[TestSource] = []
    scripts: list[str] = []
    patch_files: list[PatchFile] = []
    for path, text in sorted(package.files.items()):
        if not path.startswith("tests/") or path.endswith(("Dockerfile", ".sh", ".patch", ".json", ".md", ".txt", ".toml")):
            continue
        if path.endswith(_SOURCE_SUFFIX) or _TEST_LIKE.search(path):
            sources.append(TestSource(path, "file", list(enumerate(text.splitlines(), 1)), True))
    for path, text in sorted(package.files.items()):
        if path.startswith("tests/") and path.endswith(".patch"):
            parsed = parse_unified_diff(text)
            patch_files.extend(parsed)
            more, more_scripts = _patch_sources(parsed, path)
            sources.extend(more)
            scripts.extend(more_scripts)
            scripts.append(_build_env_command(text))
    return sources, scripts, patch_files


# --------------------------------------------------------------------------------------------------------------------
# instruction cues and reference patch


def _symbol_kind(token: str, span: str) -> str:
    if token.startswith("-"):
        return "flag"
    if "/" in token or re.search(r"\.\w{1,5}$", token) and token.rsplit(".", 1)[-1].lower() in {"py", "js", "ts", "rs", "go", "json", "toml", "yaml", "yml", "md", "txt", "csv", "sh", "cfg", "ini"}:
        return "file"
    if re.match(r"^[A-Z][A-Za-z0-9]+$", token):
        return "class"
    if "(" in span or "_" in token or re.search(r"[a-z][A-Z]", token):
        return "function"
    return "name"


_CONSTRAINT = re.compile(r"\b(?:must|should|shall|raise[sd]?|reject\w*|error|invalid|exactly|at least|at most|never|only|exit(?:s)?\s+(?:non-?zero|with)|return[s]?)\b", re.I)


def instruction_cues(instruction: str) -> list[dict[str, Any]]:
    cues: dict[str, dict[str, Any]] = {}
    sentences = re.split(r"(?<=[.!?])\s+|\n+", instruction)
    for sentence in sentences:
        constrained = bool(_CONSTRAINT.search(sentence))
        spans = [(m.group(1), m.group(1)) for m in re.finditer(r"`([^`\n]{2,80})`", sentence)]
        spans += [(m.group(1), m.group(0)) for m in re.finditer(r"\b([A-Za-z_]\w{2,})\(\)?", sentence)]
        spans += [(m.group(0), m.group(0)) for m in re.finditer(r"(?<![\w-])--[a-z][\w-]+", sentence)]
        spans += [(m.group(0), m.group(0)) for m in re.finditer(r"(?<![\w/.])[\w./-]+\.(?:py|js|ts|rs|go|json|toml|ya?ml|csv|sh|cfg|ini)\b", sentence)]
        for span, raw in spans:
            for token in re.findall(r"--[a-z][\w-]+|[A-Za-z_][\w./-]*", span):
                token = token if token.startswith("--") else token.strip(".-/")
                symbol = token.rsplit(".", 1)[-1] if (not token.startswith("-") and "/" not in token and token.count(".") >= 1 and not re.search(r"\.[a-z]{1,4}$", token)) else token
                if len(symbol) < 3 or symbol.lower() in _IDENT_STOP or symbol.isdigit():
                    continue
                kind = _symbol_kind(symbol, raw)
                if kind == "name" and not re.fullmatch(r"[A-Za-z_]\w*", symbol):
                    continue
                if kind == "name" and span == raw and "`" not in sentence:
                    continue
                entry = cues.setdefault(symbol, {"symbol": symbol, "kind": kind, "constraint": False, "sentence": ""})
                if constrained and not entry["constraint"]:
                    entry["constraint"] = True
                    entry["sentence"] = sentence.strip()[:200]
    return list(cues.values())


def _word_in(symbol: str, text: str) -> bool:
    last = symbol.rsplit("/", 1)[-1]
    return bool(re.search(rf"(?<![\w]){re.escape(last)}(?![\w])", text))


def instruction_mismatch(instruction: str, tests_text: str) -> list[dict[str, Any]]:
    out = []
    for cue in instruction_cues(instruction):
        if not _word_in(cue["symbol"], tests_text):
            weight = {"function": 3, "flag": 3, "file": 2, "class": 2, "name": 1}[cue["kind"]] + (2 if cue["constraint"] else 0)
            out.append({**cue, "weight": weight})
    out.sort(key=lambda row: (-row["weight"], row["symbol"]))
    return out


_DEF = re.compile(
    r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|\bfunction\s+(\w+)\s*\(|^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\(?[^=]*=>"
    r"|^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(|^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"
)
_BRANCH = re.compile(r"^\s*(?:if|elif|else\s+if|except|raise|throw|return\s+Err|case|when|panic!?)\b")


def reference_patch_surface(patch_text: str, tests_text: str) -> dict[str, Any]:
    changed: dict[str, dict[str, Any]] = {}
    branches: list[dict[str, Any]] = []
    for patch_file in parse_unified_diff(patch_text):
        if _TEST_LIKE.search(patch_file.path) or patch_file.deleted:
            continue
        for hunk in patch_file.hunks:
            owner = None
            match = _DEF.search(hunk["ctx"] or "")
            if match:
                owner = next(g for g in match.groups() if g)
            edits = 0  # edits to the hunk's enclosing function: those before the first definition the hunk adds
            for tag, text, _ in hunk["lines"]:
                if tag == "+" and _DEF.search(text):
                    break
                edits += tag in "+-"
            if owner and edits:
                row = changed.setdefault(owner, {"symbol": owner, "file": patch_file.path, "edited_lines": 0, "defined_in_patch": False})
                row["edited_lines"] += edits
            for tag, text, number in hunk["lines"]:
                if tag != "+":
                    continue
                defined = _DEF.search(text)
                if defined:
                    name = next(g for g in defined.groups() if g)
                    row = changed.setdefault(name, {"symbol": name, "file": patch_file.path, "edited_lines": 0, "defined_in_patch": True})
                    row["defined_in_patch"] = True
                    row["edited_lines"] += 1
                if _BRANCH.match(text):
                    literals = [s for s in re.findall(r"[\"']([^\"'\n]{3,60})[\"']", text)]
                    idents = [i for i in re.findall(r"[A-Za-z_]\w{3,}", text) if i not in {"raise", "return", "elif", "else", "except", "throw", "case", "when", "panic", "None", "True", "False", "self", "len", "not", "and", "isinstance"}]
                    branches.append({"file": patch_file.path, "patch_line": number, "text": text.strip()[:160],
                                     "literals": literals[:4], "idents": idents[:6]})
    unexercised = []
    exercised = []
    for row in changed.values():
        if row["symbol"].startswith("__") or len(row["symbol"]) < 3:
            continue
        (exercised if _word_in(row["symbol"], tests_text) else unexercised).append(row)
    unreferenced_branches = []
    for branch in branches:
        keys = branch["literals"] or branch["idents"][:3]
        if keys and not any(_word_in(key, tests_text) or key in tests_text for key in keys):
            unreferenced_branches.append(branch)
    unexercised.sort(key=lambda r: (-r["edited_lines"], r["symbol"]))
    return {
        "changed_symbols": len(changed),
        "exercised_symbols": len(exercised),
        "unexercised": unexercised[:_MAX_LIST],
        "unexercised_count": len(unexercised),
        "branch_lines": len(branches),
        "branches_unreferenced": unreferenced_branches[:_MAX_LIST],
        "branches_unreferenced_count": len(unreferenced_branches),
    }


def test_suite_rigor(package: Package, sources: Sequence[TestSource], reference_patch: str | None) -> dict[str, Any]:
    counts = Counts()
    per_file: list[dict[str, Any]] = []
    for source in sources:
        file_counts = _py_counts(source) if source.path.split(":")[-1].endswith(".py") else None
        if file_counts is None or (source.origin == "patch" and not source.complete):
            file_counts = _regex_counts(source)
        counts.add(file_counts)
        per_file.append({"file": source.path, "mode": "ast" if file_counts.mode_ast_files else "regex",
                         "tests": file_counts.tests, "assertions": file_counts.assertions})
    tests_text = "\n".join(source.text for source in sources) + "\n" + package.text("tests/test_command.sh")
    tests_text += "\n".join(text for path, text in package.files.items() if path.startswith("tests/") and path.endswith(".sh"))
    instruction = package.text("instruction.md")
    mismatch = instruction_mismatch(instruction, tests_text) if tests_text.strip() else []
    asserts = max(counts.assertions, 0)
    result: dict[str, Any] = {
        "counts": asdict(counts),
        "ratios": {
            "literal_eq_share": round(counts.literal_eq_assertions / asserts, 3) if asserts else None,
            "negative_share": round(counts.negative_assertions / asserts, 3) if asserts else None,
            "surface_share": round(counts.surface_assertions / asserts, 3) if asserts else None,
            "assertions_per_test": round(asserts / counts.tests, 2) if counts.tests else None,
        },
        "instruction_error_words": bool(re.search(r"\b(?:raise[sd]?|raising|invalid|malformed|unsupported|non-?zero|\w+Error|exception)\b", instruction, re.I)),
        "instruction_named_symbols_without_test_reference": mismatch[:_MAX_LIST * 2],
        "instruction_named_symbols_total": len(instruction_cues(instruction)),
        "instruction_named_symbols_unreferenced_count": len(mismatch),
        "test_files": per_file[:_MAX_LIST],
        "fragment_files": sum(1 for s in sources if s.origin == "patch" and not s.complete),
        "reference_patch": None,
    }
    if reference_patch is not None:
        result["reference_patch"] = reference_patch_surface(reference_patch, tests_text)
    return result


# --------------------------------------------------------------------------------------------------------------------
# hypotheses


def _h(hyps: list[Hypothesis], hid: str, cls: str, score: int, confidence: str, title: str,
       evidence: Sequence[dict[str, Any]], witness: str, *, reference: bool = False) -> None:
    assert cls in MECHANISM_CLASSES
    hyps.append(Hypothesis(hid, cls, score, confidence, title, tuple(evidence[:_MAX_EVIDENCE]), witness, reference))


_UNENFORCED = re.compile(
    r"(?:do not|don't|must not|never|should not|shouldn't|not allowed to)[^.\n]{0,60}"
    r"(?:modify|edit|change|touch|delete|alter|overwrite|rewrite|tamper|read|open|look at|access)[^.\n]{0,60}"
    r"(?:test|tests|check|grader|verifier|script|harness|checker)", re.I,
)


def build_hypotheses(package: Package, boundary: Mapping[str, Any], grader: Mapping[str, Any],
                     rigor: Mapping[str, Any], sources: Sequence[TestSource]) -> list[Hypothesis]:
    hyps: list[Hypothesis] = []
    image = boundary["agent_image"]
    gate = grader["reward_gate"]
    vectors = grader["env_vectors"]
    reset = grader["reset_list"]
    counts = rigor["counts"]
    single = boundary["layout"] == "single_container"
    runner = grader["runner"]
    test_sh = package.text("tests/test.sh")
    instruction = package.text("instruction.md")

    # ---- grader_tamperable
    if grader["baked_tests_executed"]:
        hits = grader["baked_tests_executed"]
        _h(hyps, "GT-BAKED-TESTS-EXECUTED", "grader_tamperable", 95, "static_certain",
           "The verifier executes test files that the agent image bakes into the agent-writable tree",
           [_ev("environment/Dockerfile", None, f"image path {p}") for p in hits] +
           _evidence("tests/test.sh", _find(test_sh, r"pytest|unittest|jest|mocha|vitest|cargo|go test", limit=2)),
           "In the agent session overwrite the baked test file(s) with trivially passing asserts, change nothing else, then "
           "run the verifier: reward 1 with no feature implemented proves it.")
    scripts = grader["executes_agent_writable_code"]
    if scripts:
        _h(hyps, "GT-VERIFIER-RUNS-AGENT-CODE", "grader_tamperable", 90, "static_certain",
           "The verifier runs or imports a script/module that ships in the agent-writable image tree",
           [_ev(s.get("file", ""), s.get("line"), f"{s['kind']}: {s['script']} :: {s.get('text', '')}") for s in scripts],
           "Edit the named baked script/module so it always exits 0 / prints the PASS line, leave the solution undone, run the "
           "verifier: reward 1 proves the check itself is agent-controlled.")
    hook_classes = reset["autoload_hook_classes_left_writable"]
    if single and hook_classes and grader["runs_in_agent_workdir"]:
        noisolation = not (vectors["isolation_flags"]["noconftest"] or vectors["isolation_flags"]["python_isolated"]
                           or vectors["isolation_flags"]["env_i"] or vectors["isolation_flags"]["config_nulled"])
        if noisolation:
            witness = (
                "As the agent add a root conftest.py whose pytest_runtest_makereport/pytest_collection_modifyitems hook forces "
                "every test to pass (or a sitecustomize.py/.pth that sets the exit status 0), leave the feature unimplemented, "
                "run the verifier: reward 1 vs the empty-diff baseline 0 is the differential."
                if runner in {"pytest", "unittest"} else
                f"As the agent edit one of the hook/config files ({', '.join(hook_classes)}) so the {runner} run reports success "
                "(e.g. a setup file or script hook that forces exit 0), leave the feature unimplemented, run the verifier: "
                "reward 1 vs the empty-diff baseline 0 is the differential."
            )
            _h(hyps, "GT-AUTOLOAD-WRITABLE", "grader_tamperable", 80, "static_probable",
               f"Single-container {runner} grader runs in the agent workdir and resets only an enumerated file list; "
               "auto-load/config hooks stay agent-writable",
               _evidence("tests/test.sh", _find(test_sh, r"EOF_RESET|git checkout|cd\s", limit=3)) +
               [_ev("tests/test.sh", None, "autoload hooks left writable: " + ", ".join(hook_classes))],
               witness)
    if single and gate["mechanisms"] == ["exit_code"] and not gate["structured_report_count_check"] and runner != "unknown":
        _h(hyps, "EG-EXIT-CODE-ONLY-GATE", "exploit_grader_gap", 50, "static_probable",
           "Reward is a bare exit-code check: nothing cross-checks collected/passed test counts, so any process that exits 0 inside the "
           "test command scores",
           _evidence("tests/test.sh", [(e["line"], e["text"]) for e in gate["reward_write_lines"] if e["line"]]),
           "Make the test command exit 0 without running the suite (wrapper, early sys.exit(0) via conftest/sitecustomize, deselecting "
           "everything with exit code masked) and compare reward to the honest empty-diff baseline.")
    if gate["failure_masks"]:
        _h(hyps, "EG-FAILURE-MASKED", "exploit_grader_gap", 85, "static_certain",
           "The grader masks the test command's failure (`|| true`, `set +e`, or a pipe without pipefail) before gating reward",
           [_ev(e["file"], e["line"], e["text"]) for e in gate["failure_masks"]],
           "Run the verifier on an empty diff; if reward is 1 the mask is live. Otherwise fail a single test and rerun.")
    if gate["reward_one_unconditional"]:
        _h(hyps, "EG-REWARD-UNCONDITIONAL", "exploit_grader_gap", 90, "static_certain",
           "A reward of 1 is written outside any conditional branch",
           [_ev(e["file"], e["line"], e["text"]) for e in gate["reward_write_lines"]],
           "Run the verifier with a failing solution: reward 1 confirms the unconditional write.")
    if single and grader["early_exit_paths"] and not grader["clears_reward_file_first"] and boundary["root_posture"]["logs_verifier_world_writable"]:
        _h(hyps, "GT-REWARD-PREWRITTEN", "grader_tamperable", 55, "needs_execution",
           "/logs/verifier is world-writable, reward.txt is never cleared first, and test.sh has early `exit 1` paths that write no reward: "
           "an agent that pre-writes reward 1 and trips an early-exit path may score",
           [_ev(e["file"], e["line"], e["text"]) for e in grader["early_exit_paths"]],
           "As the agent write `1` to /logs/verifier/reward.txt and remove /var/lib/mimo/base (or whatever the first early-exit guard "
           "reads); run the verifier and read the reward the harness records.")
    if single and runner in {"pytest", "unittest"} and image["writable_system_site_packages"] and not vectors["isolation_flags"]["python_isolated"] \
            and not vectors["isolation_flags"]["env_i"]:
        _h(hyps, "GT-SITE-PACKAGES-HOOK", "grader_tamperable", 40, "needs_execution",
           "Root agent shares the verifier's Python: a .pth/sitecustomize in writable site-packages loads before the test command",
           [_ev("environment/Dockerfile", None, f"user={image['user']}; base={image['base_image']}")],
           "Drop a .pth file into site-packages that registers a pytest plugin forcing a pass; run the verifier.")
    if vectors["path_hijack"]["exposed"] and single:
        _h(hyps, "GT-PATH-HIJACK", "grader_tamperable", 35, "needs_execution",
           "The grader invokes bare commands with an agent-writable PATH/cwd and no PATH reset",
           [_ev("tests/test.sh", None, "bare commands: " + ", ".join(vectors["path_hijack"]["bare_commands"]))],
           "Place a shim named like the first bare command earlier on PATH (or in the cwd for `.`-first PATH) that exits 0; run the verifier.")
    if reset["helper_modules_not_shipped_by_patch"]:
        _h(hyps, "GT-HELPER-NOT-RESET", "grader_tamperable", 60, "static_probable",
           "Hidden tests import helper/conftest modules that the hidden patch does not ship, so the agent can rewrite them",
           [_ev(h["file"], h["line"], h["text"]) for h in reset["helper_modules_not_shipped_by_patch"]],
           "Modify the named helper to make its assertion helpers no-ops (return True) and run the hidden tests.")
    # ---- answer_reachable
    overlaps: dict[str, list[dict[str, Any]]] = {}
    for item in image["tests_baked_identical_or_overlapping"]:
        overlaps.setdefault(item["tests_file"], []).append(item)
    for tests_file, items in sorted(overlaps.items()):
        identical = any(i["kind"] == "identical_bytes" for i in items)
        _h(hyps, "AR-TESTS-IN-IMAGE", "answer_reachable", 92 if identical else 85, "static_certain",
           f"The agent image contains {'identical bytes' if identical else 'large shared line sets'} with verifier-only {tests_file} "
           f"({len(items)} baked copy/copies)",
           [_ev("environment/Dockerfile", i.get("dockerfile_line"), f"bakes {i.get('source')} at {i.get('image_path')}") for i in items]
           + [_ev(tests_file, None, "verifier-side copy")],
           "From inside the agent session, read the baked copy and derive the expected values from it (no task work); score proves reachability.")
    overlapped = {i["source"] for items in overlaps.values() for i in items}
    solution_like = [p for p in image["baked_solution_like_files"] if p not in overlapped and not any(
        p.endswith(s.split("!")[0]) for s in overlapped)]
    if solution_like:
        _h(hyps, "AR-SOLUTION-ARTIFACT-IN-IMAGE", "answer_reachable", 88, "static_probable",
           "Solution/reference/expected-output-looking artifacts are baked into, or live next to, the agent build context",
           [_ev(p, None, "solution-like path") for p in solution_like],
           "Open the named artifact in the agent session and submit its contents unchanged.")
    git = image["git"]
    if git["copies_git_history"] or (git["clone_or_fetch_in_run"] and not git["history_removed_in_run"]):
        _h(hyps, "AR-GIT-HISTORY", "answer_reachable", 65, "static_probable",
           "The image keeps git history/remotes that can contain the fix (build-context .git or clone/fetch without removing .git)",
           [_ev("environment/Dockerfile", r["line"], r["text"]) for r in image["run_audit"] if {"git_clone", "git_fetch_all"} & set(r["tags"])]
           or [_ev("environment", None, "git dir in build context")],
           "In the agent session run `git log --all --oneline`, `git tag`, `git branch -a`; look for a commit/tag containing the fix and cherry-pick it.")
    outside = image["baked_outside_workdir"]
    named = [p for p in outside if re.search(r"harness|leak|expected|golden|answer|solution|reference|gold|verif|grader|oracle", p, re.I)]
    if named:
        _h(hyps, "AR-BAKED-OUTSIDE-WORKDIR", "answer_reachable", 70, "static_probable",
           "Files with verifier/answer-like names are baked outside the workdir",
           [_ev("environment/Dockerfile", None, p) for p in named], "`find / -name <basename>` in the agent session and read it.")
    if boundary["network"]["network_mode"] == "public" and boundary["network"]["blocklist_domains"] and not boundary["network"]["github_hosts_blocklisted"]:
        _h(hyps, "AR-PUBLIC-NETWORK-UPSTREAM", "answer_reachable", 45, "needs_execution",
           "Public network with a blocklist that omits the upstream code host",
           [_ev("environment/setup/files/blocklist", None, f"{boundary['network']['blocklist_domains']} domains, no github.com")],
           "Fetch the upstream repository fix commit from the agent session and apply it.")
    elif boundary["network"]["network_mode"] == "public" and boundary["network"]["blocklist_domains"]:
        _h(hyps, "AR-PUBLIC-NETWORK-BLOCKLIST-EDITABLE", "answer_reachable", 30, "needs_execution",
           "Public network; the answer-leak blocklist is /etc/hosts lines applied by the harness, which a root agent can undo or route around "
           "(mirrors, package indexes, search engines not on the list)",
           [_ev("environment/setup/files/blocklist", None, f"{boundary['network']['blocklist_domains']} blocked domains")],
           "From the agent session remove the hosts entry (or use an unlisted mirror) and fetch the upstream fix; compare to a run that respects the list.")
    if rigor["counts"]["literal_eq_assertions"] >= 3 and rigor["ratios"]["literal_eq_share"] is not None and rigor["ratios"]["literal_eq_share"] >= 0.7 \
            and not counts["randomized_or_property"] and not counts["loops_with_assertions"] and boundary["layout"]:
        visible = bool(image["baked_test_like_files"] or grader["baked_tests_executed"])
        _h(hyps, "AR-LITERAL-PAIRS-CARVE" if visible else "WC-LITERAL-PAIRS", "answer_reachable" if visible else "weak_check",
           70 if visible else 40, "static_probable",
           f"{counts['literal_eq_assertions']}/{counts['assertions']} assertions are hard-coded literal input->output equalities with no "
           f"randomized/property cases: a lookup table over the literals passes ({'tests are agent-visible' if visible else 'tests hidden, so only literal-guessing is possible'})",
           _evidence("tests", [(0, f"literal_eq_share={rigor['ratios']['literal_eq_share']}, parametrized_cases={counts['parametrized_cases']}")]),
           "Implement the function as a dict lookup of the literal pairs from the visible/inferred tests and add one unseen input: the "
           "lookup passes the suite but fails the unseen input.")
    # ---- weak_check
    miss = rigor["instruction_named_symbols_without_test_reference"]
    strong = [m for m in miss if m["weight"] >= 3]
    if strong:
        _h(hyps, "WC-INSTRUCTION-SYMBOL-UNTESTED", "weak_check", min(75, 40 + 5 * len(strong)), "static_probable",
           f"{len(strong)} function/flag/constraint symbols named in instruction.md have zero references in tests/*",
           [_ev("instruction.md", None, f"{m['kind']} `{m['symbol']}`" + (f" :: {m['sentence']}" if m["constraint"] else "")) for m in strong],
           "Break exactly the named behavior (e.g. return a wrong value for that symbol/flag) while keeping every other behavior intact; "
           "if the suite still passes, the check is weak for that clause.")
    ref = rigor["reference_patch"]
    if ref and ref["unexercised"]:
        _h(hyps, "WC-REFERENCE-SYMBOL-UNEXERCISED", "weak_check", min(85, 50 + 6 * ref["unexercised_count"]), "static_probable",
           f"{ref['unexercised_count']} of {ref['changed_symbols']} symbols the reference patch changes are never referenced by tests/*",
           [_ev(r["file"], None, f"{r['symbol']} (edited lines={r['edited_lines']})") for r in ref["unexercised"]],
           "Revert only the named symbol's change in the reference solution and run the suite: if it still passes the symbol is unasserted.",
           reference=True)
    if ref and ref["branches_unreferenced"]:
        _h(hyps, "WC-REFERENCE-BRANCH-UNASSERTED", "weak_check", min(80, 45 + 3 * ref["branches_unreferenced_count"]), "static_probable",
           f"{ref['branches_unreferenced_count']} of {ref['branch_lines']} branch/raise lines added by the reference patch have no literal or identifier in tests/*",
           [_ev(b["file"], b["patch_line"], b["text"]) for b in ref["branches_unreferenced"]],
           "Delete the named branch from the reference solution and run the suite; passing means the branch is unasserted.", reference=True)
    if counts["tests"] and counts["tests_without_assertion"]:
        _h(hyps, "WC-TEST-WITHOUT-ASSERTION", "weak_check", 55, "static_certain",
           f"{counts['tests_without_assertion']} of {counts['tests']} test functions contain no assertion",
           [_ev(f["file"], None, f"{f['tests']} tests / {f['assertions']} assertions") for f in rigor["test_files"]],
           "Stub the code under those tests to return None: they still pass.")
    if counts["assertions"] and counts["surface_assertions"] / counts["assertions"] >= 0.5:
        _h(hyps, "WC-SURFACE-ONLY", "weak_check", 60, "static_probable",
           f"{counts['surface_assertions']}/{counts['assertions']} assertions are surface checks (existence, non-None, truthiness, source-text)",
           [_ev(f["file"], None, "surface-heavy suite") for f in rigor["test_files"]],
           "Provide an empty/placeholder implementation that merely exposes the names/files; the suite passes without behavior.")
    if counts["source_text_assertions"]:
        _h(hyps, "WC-SOURCE-TEXT-ASSERT", "weak_check", 65, "static_probable",
           "Assertions search source text instead of executing behavior",
           [_ev(f["file"], None, "source-text assertion present") for f in rigor["test_files"]],
           "Insert the searched-for tokens in a comment and leave behavior wrong.")
    if counts["mock_assertions"] and counts["mock_assertions"] >= counts["assertions"] - counts["mock_assertions"]:
        _h(hyps, "WC-MOCK-ONLY", "weak_check", 55, "static_probable",
           "Mock call assertions dominate: the suite checks interaction, not outcomes",
           [_ev(f["file"], None, "mock-heavy suite") for f in rigor["test_files"]],
           "Call the mocked collaborator with the expected arguments and return a wrong result.")
    if rigor["instruction_error_words"] and counts["tests"] and not counts["negative_assertions"] and not counts["negative_named_tests"]:
        _h(hyps, "WC-NO-NEGATIVE-CASES", "weak_check", 50, "static_probable",
           "instruction.md describes error/invalid-input behavior but no test asserts an error, rejection or non-zero exit",
           [_ev("instruction.md", n, t) for n, t in _find(instruction, r"\b(?:error|raise|invalid|reject|fail|exception)", flags=re.I, limit=3)],
           "Make the implementation silently accept invalid input; the suite stays green.")
    if counts["tests"] and counts["tests"] <= 2 and counts["assertions"] <= 3 and not counts["parametrized_cases"]:
        _h(hyps, "WC-TINY-SUITE", "weak_check", 40, "static_certain",
           f"Only {counts['tests']} test(s) / {counts['assertions']} assertion(s)",
           [_ev(f["file"], None, f"{f['tests']} tests / {f['assertions']} assertions") for f in rigor["test_files"]],
           "Implement only the single exercised path.")
    if counts["tests"] == 0 and not rigor["fragment_files"] and (counts["files"] or package.text("tests/test.sh")):
        _h(hyps, "WC-NO-TESTS-FOUND", "weak_check", 45, "static_probable",
           "No test functions were detected in tests/*; the grader may rely on exit status of a script alone",
           [_ev("tests/test.sh", None, "no test definitions parsed")],
           "Submit an empty diff and run the verifier: reward 1 means the grader checks nothing.")
    if vectors["deselect_or_ignore"]:
        _h(hyps, "WC-DESELECTED-TESTS", "weak_check", 70, "static_certain",
           "The grader deselects/ignores tests (-k not, --deselect, --ignore, xfail, -x)",
           [_ev(e["file"], e["line"], e["text"]) for e in vectors["deselect_or_ignore"]],
           "Break the deselected behavior and rerun: the verdict should not change.")
    # ---- exploit_grader_gap
    artifact_only = _artifact_only(sources)
    if artifact_only:
        _h(hyps, "EG-ARTIFACT-ONLY-CHECK", "exploit_grader_gap", 72, "static_probable",
           "Tests read a file the agent can author (absolute path in the agent tree) and compare it to constants; they never run project code to "
           "regenerate or verify it",
           artifact_only,
           "Hand-write the artifact with the expected constants (copied from the instruction/test expectations) and skip the real procedure; "
           "reward 1 shows the check is satisfiable by fabrication.")
    unenforced = _UNENFORCED.search(instruction)
    enforced = bool(re.search(r"sha256sum|git\s+diff|cmp\s|\bdiff\s|chattr|git\s+checkout|cp\s+(?:-\w+\s+)?/tests", test_sh))
    if unenforced and not enforced:
        _h(hyps, "EG-INSTRUCTION-RULE-UNENFORCED", "exploit_grader_gap", 60, "static_probable",
           "instruction.md forbids touching tests/checks, but test.sh has no integrity check (hash, diff, restore from outside)",
           _evidence("instruction.md", _find(instruction, _UNENFORCED.pattern, flags=re.I, limit=2)),
           "Do the forbidden edit (weaken the named test/check) and confirm the verifier still pays out.")
    hyps.sort(key=lambda h: (-h.score, h.id))
    # de-duplicate hypothesis ids (several overlap items share an id): keep order, suffix later ones
    seen: dict[str, int] = {}
    out = []
    for h in hyps:
        n = seen.get(h.id, 0)
        seen[h.id] = n + 1
        out.append(h if n == 0 else Hypothesis(f"{h.id}#{n + 1}", *(getattr(h, f) for f in ("mechanism_class", "score", "confidence", "title", "evidence", "witness", "requires_reference"))))
    return out


def _artifact_only(sources: Sequence[TestSource]) -> list[dict[str, Any]]:
    """Test files that read an absolute-path artifact and never import/run project code."""
    hits: list[dict[str, Any]] = []
    for source in sources:
        if source.origin == "patch" or not source.path.endswith(".py"):
            continue
        text = source.text
        reads = _find(text, r"Path\(\s*[\"']/(?:app|testbed|workspace|repo|work|output|home)/[^\"']+[\"']\s*\)|open\(\s*[\"']/(?:app|testbed|workspace|repo|home)/", limit=2)
        if not reads or not re.search(r"json\.loads|\.read_text\(|json\.load\(|csv\.|yaml", text):
            continue
        runs_code = re.search(r"subprocess|Popen|importlib|import_module|sys\.path\.(?:insert|append)|^\s*from\s+(?!__future__|pathlib|typing|json|os|re|sys|pytest|hashlib|csv|collections|dataclasses|decimal)\w+\s+import|\bexec\(|\beval\(", text, re.M)
        if runs_code:
            continue
        literal_expected = len(re.findall(r"^[A-Z_]{4,}\s*=\s*[\{\[\"']", text, re.M)) + len(re.findall(r"==\s*[\"'\d\[{]", text))
        if literal_expected >= 3:
            hits += _evidence(source.path, reads)
            hits.append(_ev(source.path, None, f"{literal_expected} literal expectations, no project code executed"))
    return hits


# --------------------------------------------------------------------------------------------------------------------
# entry points


def analyze_package(root: str | Path, *, reference_patch: str | Path | None = None) -> dict[str, Any]:
    """Return the ``PackageAnatomy`` card for one task package directory."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"task package is not a directory: {root}")
    package = load_package(root)
    boundary, context = container_and_boundary(package)
    context["layout_separate"] = boundary["layout"] == "separate_verifier"
    sources, patch_scripts, _ = _test_sources(package)
    grader = grader_script_mechanics(package, sources, patch_scripts, context)
    for image_path in grader["baked_tests_executed"]:
        baked_source = context["baked"][image_path][0]
        if baked_source in package.files:
            sources.append(TestSource(baked_source, "file", list(enumerate(package.files[baked_source].splitlines(), 1)), True))
    patch_text = Path(reference_patch).read_text(encoding="utf-8", errors="replace") if reference_patch else None
    rigor = test_suite_rigor(package, sources, patch_text)
    hypotheses = build_hypotheses(package, boundary, grader, rigor, sources)
    limits = []
    if not package.text("instruction.md"):
        limits.append("no instruction.md: instruction-vs-test cues are empty")
    if not sources:
        limits.append("no test sources found under tests/")
    if boundary["agent_image"]["opaque_prebuilt_image"] and not context["baked"]:
        limits.append("agent image is an opaque prebuilt digest: baked-file facts are unavailable")
    if package.truncated:
        limits.append(f"package truncated at {_MAX_FILES} files")
    if reference_patch is None:
        limits.append("no reference patch supplied: symbol/branch exercise checks skipped")
    manifest = hashlib.sha256(
        "\n".join(f"{path}\0{size}\0{package.digests.get(path, '')}" for path, size in sorted(package.sizes.items())).encode()
    ).hexdigest()
    return {
        "schema": SCHEMA,
        "task": root.name,
        "package_digest": f"sha256:{manifest}",
        "reference_patch_used": reference_patch is not None,
        "container_and_boundary": boundary,
        "grader_script_mechanics": grader,
        "test_suite_rigor": rigor,
        "vulnerability_hypotheses": [h.as_dict() for h in hypotheses],
        "hypothesis_classes": sorted({h.mechanism_class for h in hypotheses}),
        "limits": limits,
    }


def discover_packages(paths: Sequence[Path]) -> list[Path]:
    """A task directory (has ``task.toml`` or ``instruction.md``) is itself a package; otherwise its children are."""
    found: dict[Path, None] = {}
    for path in paths:
        if (path / "task.toml").is_file() or (path / "instruction.md").is_file():
            found[path] = None
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir() and ((child / "task.toml").is_file() or (child / "instruction.md").is_file()):
                    found[child] = None
        else:
            raise FileNotFoundError(f"not a task package or collection: {path}")
    return list(found)


def render_text(card: Mapping[str, Any]) -> str:
    boundary, grader = card["container_and_boundary"], card["grader_script_mechanics"]
    rigor = card["test_suite_rigor"]["counts"]
    lines = [
        f"{card['task']}  layout={boundary['layout']} runner={grader['runner']} "
        f"gate={'+'.join(grader['reward_gate']['mechanisms'])} tests={rigor['tests']} assertions={rigor['assertions']}",
    ]
    for hyp in card["vulnerability_hypotheses"]:
        lines.append(f"  [{hyp['score']:>3}] {hyp['mechanism_class']:<19} {hyp['id']}: {hyp['title']}")
        for item in hyp["evidence"][:2]:
            lines.append(f"        {item['file']}:{item['line'] or '-'}  {item['text']}")
    if not card["vulnerability_hypotheses"]:
        lines.append("  no hypotheses")
    return "\n".join(lines)


def dumps_card(card: Mapping[str, Any]) -> str:
    return json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
