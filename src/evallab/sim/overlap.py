from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

from evallab.package_anatomy import (
    Package,
    TestSource,
    _executes_agent_script,
    _overlap,
    _reset_list,
    _test_sources,
    _unshipped_helpers,
    baked_files,
    grader_script_mechanics,
    load_package,
    parse_dockerfile,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_Baked = dict[str, tuple[str, int]]

_STEP_BUDGET = 20
_MAX_LIST = 12
_PACKAGES = 2
_MAX_FILES = 48
_HEX = "0123456789abcdef"
_CHECKER_WORDS = (
    "check",
    "verify",
    "validate",
    "grader",
    "grade",
    "attest",
    "audit",
    "oracle",
    "harness",
    "judge",
    "score",
    "evaluate",
    "Checker",
    "VERIFY",
)
_PLAIN_WORDS = ("runner", "build", "setup", "convert", "render", "launch", "migrate", "prepare")
_HIT_MODES = ("spawn", "import", "shell")
_MISS_MODES = ("silent", "patch", "test_like", "skipped")
_SHELL_FORMS = ("bash", "sh", "python", "python3", "node", "dot")
_SHELL_EXT = {"bash": "sh", "sh": "sh", "python": "py", "python3": "py", "node": "js", "dot": "sh"}
_IMPORT_FORMS = ("from", "import", "alias", "indented")
_LITERAL_FORMS = ("image", "relative", "bare")
_TEST_LIKE_DIRS = ("/opt/tests", "/srv/spec", "/app/__tests__")
_MODULE_DIRS = ("/opt/lib", "/usr/local/lib/grading")
_HELPER_NAMES = tuple(
    name
    for name in (
        "conftest",
        "helper",
        "helpers",
        "util",
        "utils",
        "fixture",
        "fixtures",
        "support",
        "common",
        "testing",
        "testlib",
        "test_utils",
        "testutils",
        "test_helpers",
        "test_support",
    )
    if name not in sys.stdlib_module_names
)
_HELPER_FORMS = ("import", "from", "package", "dotted")
_STDLIB_IMPORTS = (
    "from importlib.util import find_spec",
    "import email.utils",
    "from unittest.util import safe_repr",
    "import wsgiref.util",
)
_RESET_TAG = "EOF_RESET_TEST_FILES"
_RESET_QUOTES = ("", "'", '"')
_OVERLAP_KINDS = ("identical", "shared", "few", "ratio", "unbaked")
_SHARED_RANGES = {"shared": (40, 70), "few": (20, 39), "ratio": (40, 48)}
_EXECUTE_KINDS = (
    "subprocess_runs_baked_script",
    "imports_baked_module",
    "test_sh_runs_baked_script",
)
_OVERLAP_HITS = {"identical": "identical_bytes", "shared": "shared_lines"}
assert len(_HELPER_NAMES) >= 8
assert all(module.split(".")[0] in sys.stdlib_module_names for module in ("importlib", "email"))


@dataclass(frozen=True, slots=True)
class _Call:
    mode: str
    checker: bool
    name: str
    image: str
    form: str
    quote: str

    @property
    def hit(self) -> bool:
        return self.checker and self.mode in _HIT_MODES


@dataclass(slots=True)
class _Build:
    token: str
    files: dict[str, str] = field(default_factory=dict)
    copies: list[tuple[str, str]] = field(default_factory=list)
    file_hits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    shell_hits: list[dict[str, Any]] = field(default_factory=list)
    helpers: list[dict[str, Any]] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    reset: list[str] = field(default_factory=list)
    copied: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    baked: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def executes(self) -> list[dict[str, Any]]:
        found = [hit for path in sorted(self.file_hits) for hit in self.file_hits[path]]
        return found + self.shell_hits


def _flag(rng: Random) -> bool:
    return rng.random() < 0.5


def _token(rng: Random) -> str:
    return "".join(rng.choice(_HEX) for _ in range(8))


def _hit(kind: str, script: str, file: str, line: int, text: str) -> dict[str, Any]:
    assert kind in _EXECUTE_KINDS and line >= 1
    return {"kind": kind, "script": script, "file": file, "line": line, "text": text}


def _name(rng: Random, checker: bool, ext: str, index: int) -> str:
    assert ext in ("py", "sh", "js")
    word = rng.choice(_CHECKER_WORDS if checker else _PLAIN_WORDS)
    return f"{word}_{_token(rng)}{index:02d}.{ext}"


def _call(rng: Random, mode: str, checker: bool, index: int) -> _Call:
    assert mode in _HIT_MODES or mode in _MISS_MODES
    quote = rng.choice(("'", '"'))
    if mode in ("import", "test_like"):
        name = _name(rng, checker, "py", index)
        directory = rng.choice(_TEST_LIKE_DIRS if mode == "test_like" else _MODULE_DIRS)
        form = rng.choice(_IMPORT_FORMS)
        return _Call(mode, checker, name, f"{directory}/{name}", form, quote)
    if mode in ("shell", "skipped"):
        form = rng.choice(_SHELL_FORMS if mode == "shell" else _SHELL_FORMS[:-1])
        name = _name(rng, checker, _SHELL_EXT[form], index)
        quote = rng.choice(("", "'", '"'))
        return _Call(mode, checker, name, f"/opt/tools/{name}", form, quote)
    name = _name(rng, checker, rng.choice(("py", "sh")), index)
    form = rng.choice(_LITERAL_FORMS)
    return _Call(mode, checker, name, f"/opt/tools/{name}", form, quote)


def _calls(rng: Random) -> list[_Call]:
    modes = [(mode, checker) for mode in _HIT_MODES for checker in (True, False)]
    modes += [(mode, True) for mode in _MISS_MODES]
    modes += [(rng.choice(_HIT_MODES), _flag(rng)) for _ in range(rng.randint(0, 2))]
    calls = [_call(rng, mode, checker, index) for index, (mode, checker) in enumerate(modes)]
    rng.shuffle(calls)
    assert len({call.name for call in calls}) == len(calls)
    assert sum(call.hit for call in calls) <= _MAX_LIST
    return calls


def _script_text(name: str) -> str:
    if name.endswith(".py"):
        return f'print("{name}")\n'
    if name.endswith(".js"):
        return f'console.log("{name}")\n'
    return f"#!/bin/sh\necho {name}\n"


def _literal(call: _Call) -> str:
    literals = {"image": call.image, "relative": f"tools/{call.name}", "bare": call.name}
    assert len(literals) == len(_LITERAL_FORMS)
    return literals[call.form]


def _spawn_line(call: _Call) -> str:
    runner = "python3" if call.name.endswith(".py") else "bash"
    quote = call.quote
    return f"    subprocess.run([{quote}{runner}{quote}, {quote}{_literal(call)}{quote}], check=True)"


def _render_spawn(build: _Build, calls: Sequence[_Call]) -> None:
    path = f"tests/test_{build.token}_run.py"
    lines = ["import subprocess", "", "", "def test_tools():"]
    hits: list[dict[str, Any]] = []
    for call in calls:
        assert call.mode == "spawn"
        lines.append(_spawn_line(call))
        if call.hit:
            hits.append(_hit("subprocess_runs_baked_script", call.image, path, len(lines), lines[-1]))
    build.files[path] = "\n".join(lines) + "\n"
    build.file_hits[path] = hits


def _import_lines(call: _Call, index: int) -> list[str]:
    stem = call.name.removesuffix(".py")
    if call.form == "from":
        return [f"from {stem} import main"]
    if call.form == "import":
        return [f"import {stem}"]
    if call.form == "alias":
        return [f"import {stem} as tool_{index}"]
    assert call.form == "indented"
    return ["", f"def test_load_{index}():", f"    from {stem} import main"]


def _render_imports(build: _Build, calls: Sequence[_Call]) -> None:
    path = f"tests/test_{build.token}_load.py"
    lines = ["import pytest", ""]
    hits: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        assert call.mode in ("import", "test_like")
        lines += _import_lines(call, index)
        if call.hit:
            hits.append(_hit("imports_baked_module", call.image, path, len(lines), lines[-1]))
    build.files[path] = "\n".join(lines) + "\n"
    build.file_hits[path] = hits


def _render_silent(build: _Build, calls: Sequence[_Call]) -> None:
    path = f"tests/test_{build.token}_paths.py"
    lines = [f"SCRIPT_{index} = {call.quote}{_literal(call)}{call.quote}" for index, call in enumerate(calls)]
    assert all(call.mode == "silent" and not call.hit for call in calls)
    build.files[path] = "\n".join(lines) + "\n"
    build.file_hits[path] = []


def _shell_line(call: _Call) -> str:
    assert call.mode in ("shell", "skipped")
    if call.form == "dot":
        assert call.mode == "shell"
        return f"cd /opt/tools && ./{call.name}"
    path = call.image if call.mode == "shell" else f"/tests/{call.name}"
    quoted = f"{call.quote}{path}{call.quote}"
    if call.form in ("python", "python3"):
        return f"{call.form} {quoted} --strict"
    return f"{call.form} {quoted}"


def _reset_lines(rng: Random, build: _Build, present: bool) -> list[str]:
    if not present:
        build.reset = []
        return ["git status --short"]
    entries = [f"tests/test_{build.token}_{index}.py" for index in range(rng.randint(1, 6))]
    quote = rng.choice(_RESET_QUOTES)
    space = rng.choice(("", " "))
    lines = [f"cat > /tmp/reset_files <<{space}{quote}{_RESET_TAG}{quote}"]
    for entry in entries:
        if _flag(rng):
            lines.append("")
        lines.append(rng.choice(("", "  ", "\t")) + entry)
    lines.append(_RESET_TAG)
    build.reset = entries
    assert len(entries) <= _MAX_LIST
    return lines


def _render_test_sh(rng: Random, build: _Build, calls: Sequence[_Call], present: bool) -> None:
    lines = ["#!/bin/bash", "set -uo pipefail", *_reset_lines(rng, build, present)]
    for call in calls:
        lines.append(_shell_line(call))
        if call.hit:
            hit = _hit("test_sh_runs_baked_script", call.image, "tests/test.sh", len(lines), lines[-1])
            build.shell_hits.append(hit)
    lines += ["pytest /tests -rA", "echo 1 > /logs/verifier/reward.txt"]
    build.files["tests/test.sh"] = "\n".join(lines) + "\n"


def _helper_import(rng: Random, leaf: str, token: str) -> tuple[str, str]:
    form = rng.choice(_HELPER_FORMS)
    if form == "import":
        return f"import {leaf}", leaf
    if form == "from":
        return f"from {leaf} import thing", leaf
    if form == "package":
        return f"from tests.{leaf} import thing", f"tests.{leaf}"
    assert form == "dotted"
    return f"import pkg_{token}.{leaf}", f"pkg_{token}.{leaf}"


_Body = list[tuple[str, str | None]]


def _patch_body(rng: Random, build: _Build, calls: Sequence[_Call]) -> tuple[_Body, list[str]]:
    leaves = rng.sample(_HELPER_NAMES, rng.randint(2, 4))
    split = rng.randint(1, len(leaves) - 1)
    body: _Body = []
    for leaf in leaves[:split]:
        body.append(_helper_import(rng, leaf, build.token))
    for leaf in leaves[split:]:
        body.append((_helper_import(rng, leaf, build.token)[0], None))
    body += [(text, None) for text in rng.sample(_STDLIB_IMPORTS, rng.randint(1, 2))]
    if _flag(rng):
        body.append((f"from lib_{build.token} import thing", None))
    for call in calls:
        assert call.mode == "patch" and not call.hit
        body.append((_spawn_line(call).strip(), None))
    rng.shuffle(body)
    shipped = [f"tests/{rng.choice(('', 'support/'))}{leaf}.py" for leaf in leaves[split:]]
    return [("import subprocess", None), *body], shipped


def _new_file(path: str, texts: Sequence[str]) -> list[str]:
    assert texts
    header = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null", f"+++ b/{path}"]
    return [*header, f"@@ -0,0 +1,{len(texts)} @@", *(f"+{text}" for text in texts)]


def _render_patch(rng: Random, build: _Build, calls: Sequence[_Call]) -> None:
    patch = f"tests/{build.token}.patch"
    hidden = f"tests/test_{build.token}_hidden.py"
    body, shipped = _patch_body(rng, build, calls)
    rows = _new_file(hidden, [text for text, _ in body])
    first = len(rows) - len(body) + 1
    for offset, (text, module) in enumerate(body):
        if module is not None:
            build.helpers.append({"module": module, "file": f"{patch}:{hidden}", "line": first + offset, "text": text})
    for index, path in enumerate(shipped):
        rows += _new_file(path, [f"VALUE = {index}"])
    build.files[patch] = "\n".join(rows) + "\n"
    build.touched = sorted([hidden, *shipped])
    assert len(set(build.touched)) == len(build.touched)
    assert 1 <= len(build.helpers) <= _MAX_LIST


def _numbered(prefix: str, count: int) -> list[str]:
    assert 1 <= count <= 400
    return [f"assert {prefix}_{index:03d} == {index}" for index in range(count)]


def _overlap_texts(rng: Random, kind: str, token: str) -> tuple[str, str, int]:
    if kind in ("identical", "unbaked"):
        text = "\n".join(_numbered(f"same_{token}", rng.randint(3, 30))) + "\n"
        return text, text, 0
    low, high = _SHARED_RANGES[kind]
    shared = rng.randint(low, high)
    floor = shared * 10 // 3 + 1 - shared if kind == "ratio" else 0
    common = _numbered(f"both_{token}", shared)
    env = common + _numbered(f"env_{token}", floor + rng.randint(1, 5))
    extra = floor + rng.randint(0, 5)
    tests = common + (_numbered(f"test_{token}", extra) if extra else [])
    rng.shuffle(env)
    rng.shuffle(tests)
    assert len(env) >= 20 and set(env) != set(tests)
    return "\n".join(tests) + "\n", "\n".join(env) + "\n", shared


def _render_overlap(rng: Random, build: _Build) -> None:
    for kind in _OVERLAP_KINDS:
        token = f"{kind}{_token(rng)}"
        tests_text, env_text, shared = _overlap_texts(rng, kind, token)
        tests_path = f"tests/fixtures/test_copy_{token}.py"
        source = f"data/copy_{token}.py"
        build.files[tests_path] = tests_text
        build.files[f"environment/{source}"] = env_text
        if kind != "unbaked":
            build.copies.append((source, f"/app/data/copy_{token}.py"))
            build.copied[f"environment/{source}"] = (tests_path, kind, shared)


def _render_dockerfile(rng: Random, build: _Build) -> None:
    copies = list(build.copies)
    rng.shuffle(copies)
    lines = ["FROM python:3.12-slim", "WORKDIR /app"]
    for source, image in copies:
        if _flag(rng):
            lines.append("RUN true")
        lines.append(f"COPY {source} {image}")
        build.baked[image] = (f"environment/{source}", len(lines))
    build.files["environment/Dockerfile"] = "\n".join(lines) + "\n"
    assert len(build.baked) == len(copies)


def _build(rng: Random, reset_present: bool) -> _Build:
    build = _Build(token=_token(rng))
    calls = _calls(rng)
    for call in calls:
        directory = "lib" if call.mode in ("import", "test_like") else "tools"
        build.files[f"environment/{directory}/{call.name}"] = _script_text(call.name)
        build.copies.append((f"{directory}/{call.name}", call.image))
    _render_spawn(build, [call for call in calls if call.mode == "spawn"])
    _render_imports(build, [call for call in calls if call.mode in ("import", "test_like")])
    _render_silent(build, [call for call in calls if call.mode == "silent"])
    _render_patch(rng, build, [call for call in calls if call.mode == "patch"])
    shell = [call for call in calls if call.mode in ("shell", "skipped")]
    _render_test_sh(rng, build, shell, reset_present)
    _render_overlap(rng, build)
    _render_dockerfile(rng, build)
    assert len(build.files) <= _MAX_FILES
    assert len(build.executes) <= _MAX_LIST
    return build


def _expected_overlap(build: _Build) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for image, (source, line) in build.baked.items():
        copied = build.copied.get(source)
        if copied is None or copied[1] not in _OVERLAP_HITS:
            continue
        tests_file, kind, shared = copied
        row = {"image_path": image, "source": source, "dockerfile_line": line, "tests_file": tests_file}
        if kind == "shared":
            row["shared_lines"] = shared
        found.append({**row, "kind": _OVERLAP_HITS[kind]})
    assert len(found) == len(_OVERLAP_HITS)
    return found


def _write(root: Path, files: dict[str, str]) -> None:
    assert 1 <= len(files) <= _MAX_FILES
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert (root / "tests" / "test.sh").is_file()


def _kind(rows: Sequence[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("kind") == kind]


class OverlapSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        reset_index = self.rng.randrange(_PACKAGES)
        for index in range(_PACKAGES):
            build = _build(self.rng, index == reset_index)
            self._check(build, self.root / f"package-{index}", index)
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "overlap_step_budget")

    def _check(self, build: _Build, root: Path, index: int) -> None:
        sim = self.sim
        _write(root, build.files)
        sim.begin("overlap-package", package=index, files=len(build.files), reset=bool(build.reset))
        package = load_package(root)
        sources, scripts, _patches = _test_sources(package)
        dockerfile = parse_dockerfile(package.text("environment/Dockerfile"))
        baked = baked_files(dockerfile, list(package.sizes))
        sim.require(baked == build.baked, "overlap_baked_map")
        sim.require(list(baked) == list(build.baked), "overlap_baked_order")
        touched = sorted({source.path.split(":", 1)[1] for source in sources if source.origin == "patch"})
        sim.require(touched == build.touched, "overlap_patch_touched")
        test_sh = package.text("tests/test.sh")
        self._check_executes(build, test_sh, sources, baked)
        sim.begin("overlap-helpers", helpers=len(build.helpers))
        helpers = _unshipped_helpers(sources, touched)
        sim.require(helpers == build.helpers, "overlap_unshipped_helpers")
        sim.require(len(helpers) <= _MAX_LIST, "overlap_unshipped_helpers_bound")
        sim.begin("overlap-reset", entries=len(build.reset))
        sim.require(_reset_list(test_sh) == build.reset, "overlap_reset_list")
        self._check_tests(build, package, baked)
        context = {"workdir": "/app", "baked": baked, "layout_separate": False}
        sim.begin("overlap-mechanics")
        result = grader_script_mechanics(package, sources, scripts, context)
        reset = result["reset_list"]
        sim.require(reset["enumerated_files"] == build.reset, "overlap_mechanics_reset")
        sim.require(reset["helper_modules_not_shipped_by_patch"] == build.helpers, "overlap_mechanics_helpers")
        sim.require(result["executes_agent_writable_code"] == build.executes, "overlap_mechanics_executes")

    def _check_executes(
        self, build: _Build, test_sh: str, sources: list[TestSource], baked: _Baked
    ) -> None:
        sim = self.sim
        expected = build.executes
        sim.begin("overlap-executes", hits=len(expected))
        result = _executes_agent_script(test_sh, sources, baked)
        for kind in _EXECUTE_KINDS:
            sim.require(_kind(result, kind) == _kind(expected, kind), f"overlap_{kind}")
        sim.require(result == expected, "overlap_executes_order")
        sim.require(len(result) <= _MAX_LIST, "overlap_executes_bound")

    def _check_tests(self, build: _Build, package: Package, baked: _Baked) -> None:
        sim = self.sim
        expected = _expected_overlap(build)
        sim.begin("overlap-tests", baked=len(baked))
        result = _overlap(package, baked)
        for kind in _OVERLAP_HITS.values():
            sim.require(_kind(result, kind) == _kind(expected, kind), f"overlap_{kind}")
        sim.require(result == expected, "overlap_tests_order")
        sim.require(len(result) <= _MAX_LIST, "overlap_tests_bound")
