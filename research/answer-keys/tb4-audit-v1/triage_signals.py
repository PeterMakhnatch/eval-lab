#!/usr/bin/env python3
"""Blind, model-free static grader-surface triage over tb4-audit-v1 agent views.

The scanner reads one exported auditor view (``dev`` or ``test``) **only**
through the view's ``SealedWorkspaceTools`` capability (``views.agent_tools``):
every directory listing and file read is a ``tools.run("read", ...)`` call, and
the capability re-verifies the sealed manifest and every file digest on each
call. It never opens task files, the answer key or split assignments itself.

For every task in ``inputs.jsonl`` it extracts structural features of the
grader (``tests/``), the instruction, ``task.toml``, the agent environment and
the reference solution, then applies general RL-environment audit rules, each
tied to one public taxonomy class:

- wrong-pass: ``TS01 weak_check`` (unread output fields, unchecked determinism,
  surface-only file checks, fixed visible inputs, timing gates, source-marker
  checks), ``TS02 grader_logic_bug`` (unguarded float parsing),
  ``TS03 answer_reachable`` (answer-like files in the agent image),
  ``TS04 grader_tamperable`` (agent code imported into or run before the
  grader, agent-controlled verifier dependencies, in-process timing, shared
  grading environment);
- wrong-fail: ``TS05 unstated_requirement`` (grader-only literals, thresholds
  and graded paths), ``TS06 instruction_grader_contradiction`` (scored
  instruction vs binary reward), ``TS07 overstrict_oracle`` (exact float/text
  equality), ``TS09 env_mismatch_or_flaky`` (Python version mismatch,
  grade-time network, wall-clock limits).

Each rule emits candidate ``qa.finding/v1`` records whose ``evidence_refs`` are
inventoried view paths (``tasks/<task>/tests/...``) and symbols read from them.
Thresholds in :data:`CALIBRATION` were chosen on the 25 ``dev`` tasks only and
are frozen before the one-shot ``test`` evaluation. Candidates are suspected
labels, never confirmed ones: static reading does not meet any class's
evidence standard.

    uv run python research/answer-keys/tb4-audit-v1/triage_signals.py \\
        --dev-view <agent-inputs/dev> --test-view <agent-inputs/test> --out triage-evaluation.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # sibling modules, as build.py imports views
    sys.path.insert(0, str(HERE))

import score_audit  # noqa: E402
import views  # noqa: E402

from evallab.qa import ask as _ask  # noqa: E402
from evallab.qa.models import Finding as FindingModel  # noqa: E402

EVALUATION_SCHEMA = "tb4.audit.static_triage_evaluation/v1"
REVIEWER = "tb4-static-triage"
STAGE = "static_triage"
MAX_TOOL_CHARS = _ask.MAX_TOOL_CHARS
CHUNK = _ask.MAX_READ_LINES
CLIP_MARK = "[line or range clipped; narrow the read]"

# taxonomy key -> (class id, finding category, error direction)
CLASSES: dict[str, tuple[str, str, str]] = {
    "weak_check": ("TS01", "weak", "wrong_pass"),
    "grader_logic_bug": ("TS02", "broken", "wrong_pass"),
    "answer_reachable": ("TS03", "reachable", "wrong_pass"),
    "grader_tamperable": ("TS04", "tamperable", "wrong_pass"),
    "unstated_requirement": ("TS05", "prompt_misaligned", "wrong_fail"),
    "instruction_grader_contradiction": ("TS06", "prompt_misaligned", "wrong_fail"),
    "overstrict_oracle": ("TS07", "broken", "wrong_fail"),
    "env_mismatch_or_flaky": ("TS09", "broken", "wrong_fail"),
}
WRONG_PASS_IDS = ("TS01", "TS02", "TS03", "TS04")
WRONG_FAIL_IDS = ("TS05", "TS06", "TS07", "TS08", "TS09", "TS10")

# Pre-calibration defaults: one prediction per detector firing, generic thresholds.
UNCALIBRATED: dict[str, Any] = {
    "unread_output_min": 1,
    "low_assertion_max": 6,
    "test_only_literal_min": 3,
    "test_only_literals_per_prediction": 8,
    "test_only_numeric_min": 3,
    "test_only_numerics_per_prediction": 6,
    "float_equality_min": 2,
    "short_timeout_max_sec": 30.0,
    "max_predictions_per_detector": 3,
    "disabled_detectors": [],
}
# Coordinate-descent grid searched on dev only (see ``calibrate``).
CALIBRATION_GRID: dict[str, list[Any]] = {
    "unread_output_min": [1, 2, 3],
    "low_assertion_max": [3, 6, 10],
    "test_only_literal_min": [2, 3, 5, 8],
    "test_only_literals_per_prediction": [4, 8, 16, 10_000],
    "test_only_numeric_min": [3, 6, 10, 10_000],
    "test_only_numerics_per_prediction": [6, 12, 10_000],
    "float_equality_min": [1, 2, 3],
    "short_timeout_max_sec": [10.0, 30.0, 60.0],
    "max_predictions_per_detector": [1, 2, 3, 4],
}
# Output of ``calibrate`` on the 25 dev tasks (dev category_1to1 F1 0.515 -> 0.585),
# frozen before the one-shot test run; ``evaluate`` refuses to scan test if it drifts.
CALIBRATION: dict[str, Any] = {
    "unread_output_min": 2,
    "low_assertion_max": 6,
    "test_only_literal_min": 2,
    "test_only_literals_per_prediction": 16,
    "test_only_numeric_min": 6,
    "test_only_numerics_per_prediction": 10_000,
    "float_equality_min": 2,
    "short_timeout_max_sec": 30.0,
    "max_predictions_per_detector": 3,
    "disabled_detectors": [
        "agent_code_runs_in_verifier",
        "agent_controls_verifier_dependencies",
        "answer_like_environment_files",
        "env_python_mismatch",
        "grade_time_network_or_install",
    ],
}

# Read caps per task surface (files, lines per file) and capability calls per task.
# Every call re-verifies the whole sealed view, so the caps bound wall time, not cost.
CAPS = {
    "tests_code": (40, 3000),
    "tests_data": (8, 100),
    "env_code": (20, 800),
    "env_data": (8, 100),
    "solution_code": (10, 1000),
    "calls_per_task": 90,
}
LONG_LINE_STOP = 3
CODE_EXT = {
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".scala",
    ".sbt",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".r",
    ".jl",
    ".lua",
    ".pl",
    ".php",
    ".sql",
    ".sage",
    ".bas",
    ".cls",
    ".frm",
}
DATA_EXT = {
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".ini",
    ".cfg",
    ".xml",
    ".html",
    ".css",
    ".properties",
}
CODE_NAMES = {"Dockerfile", "Makefile", "Containerfile"}
ANSWER_NAME = re.compile(
    r"(?i)(^|[/_.-])(expected|golden|reference|answer|answers|oracle|solution|solutions|"
    r"ground[_-]?truth|gold|solved|key)([/_.-]|$)"
)
COMMON_LITERALS = {
    "utf-8",
    "utf8",
    "ascii",
    "latin-1",
    "json",
    "true",
    "false",
    "none",
    "null",
    "error",
    "errors",
    "ok",
    "pass",
    "fail",
    "passed",
    "failed",
    "stdout",
    "stderr",
    "returncode",
    "name",
    "id",
    "type",
    "value",
    "values",
    "data",
    "result",
    "results",
    "key",
    "keys",
    "text",
    "html",
    "python",
    "python3",
    "bash",
    "main",
    "__main__",
    "index",
    "items",
    "status",
    "message",
    "content",
    "path",
    "file",
    "files",
    "output",
    "input",
    "inputs",
    "outputs",
    "size",
    "count",
    "total",
    "list",
    "dict",
    "str",
    "int",
    "float",
    "bool",
    "object",
    "string",
    "number",
    "array",
    "default",
    "test",
    "tests",
    "self",
    "args",
    "kwargs",
    "utf-16",
    "replace",
    "ignore",
    "strict",
    "rb",
    "wb",
    "w",
    "r",
    "a",
    "x",
    "lower",
    "upper",
    "info",
    "debug",
    "warning",
    "nan",
    "inf",
    "get",
    "post",
}
DIGIT = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?:[eE]-?\d+)?")


def _class_ref(taxonomy_class: str) -> tuple[str, str, str]:
    return CLASSES[taxonomy_class]


# ---------------------------------------------------------------------- reads


class SealedReader:
    """Every listing and read is one ``SealedWorkspaceTools.run("read", ...)`` call."""

    def __init__(self, tools: views.SealedWorkspaceTools) -> None:
        if not isinstance(tools, views.SealedWorkspaceTools):
            raise TypeError("triage reads only through views.SealedWorkspaceTools")
        self.tools = tools
        self.calls: Counter = Counter()
        self.paths_read: set[str] = set()
        self.listing_clips = 0
        self.long_line_clips = 0
        self.budget_stops = 0
        #: Remaining calls for the current task (None = unbounded); set by ``load_surface``.
        self.budget: int | None = None
        self._cache: dict[tuple[str, int], list[str]] = {}

    def _run(self, path: str, **bounds: int) -> str:
        self.calls["read"] += 1
        if self.budget is not None:
            self.budget -= 1
        return self.tools.run("read", {"path": path, **bounds})

    def listing(self, prefix: str) -> list[str]:
        """Inventoried files below ``prefix`` (a directory read lists retained files)."""
        try:
            out = self._run(prefix)
        except ValueError:
            return []
        names = out.split("\n")
        if len(out) >= MAX_TOOL_CHARS:
            # The capability clips listings at MAX_TOOL_CHARS; the remaining names come from
            # the same capability's sealed inventory (an in-memory set, not a filesystem read).
            self.listing_clips += 1
            names = sorted(n for n in self.tools.files if n.startswith(prefix + "/"))
        return [n for n in names if n and n.startswith(prefix + "/")]

    def lines(self, rel: str, max_lines: int) -> list[str]:
        """The first ``max_lines`` lines of ``rel``, read in capability-sized chunks.

        Rows come back numbered from ``start_line``; a clipped row is re-read as the
        first row of the next chunk. A file whose single lines exceed the per-call
        character cap (minified code, one-line data) is truncated after
        ``LONG_LINE_STOP`` such lines rather than re-read once per line.
        """
        key = (rel, max_lines)
        if key in self._cache:
            return self._cache[key]
        # Rows come back as the path once, then "<line>: <text>".
        out_lines: list[str] = []
        start, long_lines = 1, 0
        while start <= max_lines:
            if self.budget is not None and self.budget <= 0:
                self.budget_stops += 1
                break
            end = min(start + CHUNK - 1, max_lines)
            out = self._run(rel, start_line=start, end_line=end)
            self.paths_read.add(rel)
            if out.startswith(f"[no lines at {rel}:"):
                break
            rows = out.split("\n")
            clipped = rows[-1] == CLIP_MARK
            if clipped:
                rows = rows[:-1]
            texts = [row.partition(": ")[2] for row in rows[1:]]
            if clipped:
                if len(texts) <= 1:
                    self.long_line_clips += 1
                    long_lines += 1
                    out_lines.extend(texts)
                    start += 1
                    if long_lines >= LONG_LINE_STOP:
                        break
                else:
                    out_lines.extend(texts[:-1])
                    start += len(texts) - 1
                continue
            out_lines.extend(texts)
            if len(texts) < end - start + 1:
                break
            start = end + 1
        self._cache[key] = out_lines
        return out_lines

    def text(self, rel: str, max_lines: int) -> str:
        return "\n".join(self.lines(rel, max_lines))


@dataclass
class _FsAudit:
    """Counts filesystem opens/scans under watched roots; mediated = inside the capability."""

    roots: tuple[str, ...]
    mediated: int = 0
    direct: list[str] = field(default_factory=list)


_AUDIT: list[_FsAudit] = []
_CAPABILITY_FILES = {
    str(Path(views.__file__).resolve()),
    str(Path(_ask.__file__).resolve()),
}


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if not _AUDIT or event not in {"open", "os.scandir", "os.listdir"}:
        return
    audit = _AUDIT[-1]
    raw = args[0] if args else None
    if isinstance(raw, int) or raw is None:
        return
    try:
        path = os.path.abspath(os.fsdecode(os.fspath(raw)))
    except (TypeError, ValueError):
        return
    if not any(path == root or path.startswith(root + "/") for root in audit.roots):
        return
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_filename in _CAPABILITY_FILES:
            audit.mediated += 1
            return
        frame = frame.f_back
    audit.direct.append(f"{event}:{path}")


sys.addaudithook(_audit_hook)


@contextmanager
def fs_audit(*roots: Path) -> Iterator[_FsAudit]:
    audit = _FsAudit(tuple(str(Path(r).absolute()) for r in roots))
    _AUDIT.append(audit)
    try:
        yield audit
    finally:
        _AUDIT.pop()


# ------------------------------------------------------------------- features


def _kind(rel: str) -> str:
    path = PurePosixPath(rel)
    if path.name in CODE_NAMES or path.name.startswith("Dockerfile") or path.name == "conftest.py":
        return "code"
    suffix = path.suffix.lower()
    if suffix in CODE_EXT:
        return "code"
    if suffix in DATA_EXT:
        return "data"
    return "other"


def _priority(task_rel: str) -> tuple[int, int, str]:
    name = PurePosixPath(task_rel).name
    first = 0 if name in {"test.sh", "Dockerfile"} else 1 if name.startswith("test") else 2
    return (task_rel.count("/"), first, task_rel)


@dataclass
class PySignals:
    asserts: int = 0
    test_symbols: list[tuple[str, int]] = field(default_factory=list)
    graded_strings: list[tuple[str, int, str]] = field(default_factory=list)
    graded_numbers: list[tuple[float, int, str]] = field(default_factory=list)
    upper_constants: dict[str, tuple[float, int]] = field(default_factory=dict)
    float_equalities: list[tuple[int, str]] = field(default_factory=list)
    float_parses: list[tuple[int, str]] = field(default_factory=list)
    timing: list[tuple[int, str]] = field(default_factory=list)
    parsed: bool = True


_TOL_KW = {
    "abs",
    "rel",
    "rtol",
    "atol",
    "tol",
    "tolerance",
    "timeout",
    "places",
    "delta",
    "threshold",
    "max_diff",
    "eps",
    "epsilon",
}
_STRING_TOKEN = re.compile(r"^(/[\w.\-/]+|[A-Za-z_][\w\-.]{2,48})$")


@lru_cache(maxsize=4096)
def _py_signals(source: str) -> PySignals:
    sig = PySignals()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        sig.parsed = False
        sig.asserts = len(re.findall(r"^\s*assert\b", source, re.M))
        return sig
    parents: dict[ast.AST, ast.AST] = {}
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, symbol: str) -> None:
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            name = symbol
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = child.name
                if child.name.startswith(("test", "Test")):
                    sig.test_symbols.append((child.name, child.lineno))
            owner[child] = name
            visit(child, name)

    visit(tree, "<module>")
    docstrings = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    for node in ast.walk(tree):
        symbol = owner.get(node, "<module>")
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Assert):
            sig.asserts += 1
        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname in {"fail"} or fname.startswith("assert"):
                sig.asserts += 1
            if fname == "float" and node.args and not isinstance(node.args[0], ast.Constant):
                sig.float_parses.append((line, symbol))
            if fname in {
                "perf_counter",
                "perf_counter_ns",
                "monotonic",
                "time",
                "timeit",
                "process_time",
            } and isinstance(func, ast.Attribute):
                base = getattr(func.value, "id", "")
                if base in {"time", "timeit"}:
                    sig.timing.append((line, symbol))
            for kw in node.keywords:
                if kw.arg in _TOL_KW and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        sig.graded_numbers.append((float(value), line, symbol))
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            eq = any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops)
            for operand in operands:
                if (
                    eq
                    and isinstance(operand, ast.Constant)
                    and isinstance(operand.value, float)
                    and not operand.value.is_integer()
                ):
                    sig.float_equalities.append((line, symbol))
        elif isinstance(node, ast.Assign) and owner.get(node) == "<module>":
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int | float)
                    and not isinstance(node.value.value, bool)
                ):
                    sig.upper_constants[target.id] = (float(node.value.value), line)
        if isinstance(node, ast.Constant) and id(node) not in docstrings:
            parent = parents.get(node)
            value = node.value
            if isinstance(value, str):
                graded = isinstance(parent, ast.Subscript | ast.Compare) or (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Attribute)
                    and parent.func.attr in {"get", "startswith", "endswith", "count", "index"}
                )
                if isinstance(parent, ast.List | ast.Tuple | ast.Set):
                    grand = parents.get(parent)
                    graded = graded or isinstance(grand, ast.Compare | ast.Assign)
                if not graded and value.startswith("/") and isinstance(parent, ast.Call):
                    graded = True
                if not graded and value.startswith("/") and isinstance(parent, ast.Assign):
                    graded = True
                if graded and _STRING_TOKEN.match(value):
                    sig.graded_strings.append((value, line, symbol))
            elif isinstance(value, int | float) and not isinstance(value, bool):
                if isinstance(parent, ast.Compare):
                    sig.graded_numbers.append((float(value), line, symbol))
    for name, (value, line) in sig.upper_constants.items():
        sig.graded_numbers.append((value, line, name))
    return sig


_JS_STRING = re.compile(r"""(['"`])([^'"`\n]{3,60})\1""")
_JS_GRADED = re.compile(r"expect|getBy|locator|toBe|toContain|toEqual|toHave|includes|assert|===")
_JS_NUMBER = re.compile(r"toBeLessThan|toBeGreaterThan|toBeCloseTo|[<>]=?|timeout")


@lru_cache(maxsize=4096)
def _js_signals(source: str) -> PySignals:
    sig = PySignals(parsed=False)
    symbol = "<module>"
    for number, line in enumerate(source.splitlines(), 1):
        named = re.search(r"""\b(?:test|it|describe)\s*\(\s*(['"`])([^'"`]+)\1""", line)
        if named:
            symbol = named.group(2)
            sig.test_symbols.append((symbol, number))
        sig.asserts += len(re.findall(r"\bexpect\s*\(|\bassert\w*\s*\(", line))
        if _JS_GRADED.search(line):
            for _, literal in _JS_STRING.findall(line):
                if _STRING_TOKEN.match(literal):
                    sig.graded_strings.append((literal, number, symbol))
        if _JS_NUMBER.search(line):
            for raw in DIGIT.findall(line):
                value = _number(raw)
                if value is not None:
                    sig.graded_numbers.append((value, number, symbol))
        if re.search(r"performance\.now|Date\.now|process\.hrtime", line):
            sig.timing.append((number, symbol))
    return sig


def _number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _instruction_numbers(text: str) -> set[float]:
    numbers: set[float] = set()
    for match in re.finditer(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?(?:[eE]-?\d+)?)(\s*%)?", text):
        value = _number(match.group(1))
        if value is None:
            continue
        numbers.add(value)
        if match.group(2):
            numbers.add(value / 100)
        if value and abs(value) < 1:
            numbers.add(value * 100)
    for word, value in (("half", 0.5), ("twice", 2.0), ("double", 2.0), ("ten", 10.0)):
        if re.search(rf"\b{word}\b", text, re.I):
            numbers.add(value)
    return numbers


_UBUNTU_PY = {
    "24.04": "3.12",
    "22.04": "3.10",
    "20.04": "3.8",
    "noble": "3.12",
    "jammy": "3.10",
    "focal": "3.8",
}
_DEBIAN_PY = {
    "bookworm": "3.11",
    "bullseye": "3.9",
    "trixie": "3.13",
    "12": "3.11",
    "11": "3.9",
    "13": "3.13",
}


def python_version(dockerfile: str, script: str = "") -> tuple[str | None, str | None]:
    """(major.minor, evidence line) for the interpreter a Dockerfile/script runs."""
    text = "\n".join([dockerfile, script])
    patterns = (
        r"(?:uv\s+(?:python\s+install|venv|run|pip\s+install)[^\n]*?--python[ =]|"
        r"uvx\s+(?:-p|--python)[ =]|python\s+install\s+|conda[^\n]*python=)(3\.\d+)",
        r"\bpython(3\.\d+)\b",
    )
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(1), found.group(0)
    base = re.search(r"(?im)^\s*FROM\s+(\S+)", dockerfile)
    if not base:
        return None, None
    image = base.group(1).lower()
    found = re.search(r"python:(3\.\d+)", image) or re.search(r"python(3\.\d+)", image)
    if found:
        return found.group(1), base.group(0).strip()
    for tag, version in _UBUNTU_PY.items():
        if tag in image and (
            "ubuntu" in image or "playwright" in image or tag in {"noble", "jammy", "focal"}
        ):
            return version, base.group(0).strip()
    for tag, version in _DEBIAN_PY.items():
        if re.search(rf"(debian|slim)[:\-]?{tag}\b|:{tag}(-slim)?$", image):
            return version, base.group(0).strip()
    return None, base.group(0).strip()


@dataclass
class TaskSurface:
    task: str
    task_id: str
    task_digest: str | None
    files: list[str]
    instruction: str
    task_toml: str
    tests_code: dict[str, str]
    tests_data: dict[str, str]
    env_code: dict[str, str]
    env_data: dict[str, str]
    solution_code: dict[str, str]
    truncated: bool


def load_surface(reader: SealedReader, record: Mapping[str, Any]) -> TaskSurface:
    task_id = record["subject"]["task_id"]
    task = score_audit.task_name(task_id)
    base = f"tasks/{task}"
    stops_before = reader.budget_stops
    reader.budget = CAPS["calls_per_task"]
    files = sorted(
        {
            *reader.listing(f"{base}/tests"),
            *reader.listing(f"{base}/environment"),
            *reader.listing(f"{base}/solution"),
        }
    )
    instruction = reader.text(f"{base}/instruction.md", 2000)
    task_toml = reader.text(f"{base}/task.toml", 400)
    groups: dict[str, dict[str, str]] = {
        g: {} for g in ("tests_code", "tests_data", "env_code", "env_data", "solution_code")
    }
    for surface, prefix in (("tests", "tests"), ("env", "environment"), ("solution", "solution")):
        names = sorted(
            (n for n in files if n.startswith(f"{base}/{prefix}/")),
            key=lambda n: _priority(n[len(base) + 1 :]),
        )
        for kind in ("code", "data"):
            group = f"{surface}_{kind}"
            if group not in groups:
                continue
            max_files, max_lines = CAPS[group]
            for name in [n for n in names if _kind(n) == kind][:max_files]:
                lines = 3 if name.endswith((".csv", ".tsv")) else max_lines
                groups[group][name] = reader.text(name, lines)
    reader.budget = None
    truncated = reader.budget_stops > stops_before
    return TaskSurface(
        task=task,
        task_id=task_id,
        task_digest=record["subject"].get("task_digest"),
        files=files,
        instruction=instruction,
        task_toml=task_toml,
        truncated=truncated,
        **groups,
    )


@dataclass
class Signal:
    """One detector firing: the class it suggests and the view evidence it cites."""

    detector: str
    taxonomy_class: str
    summary: str
    refs: list[str]
    weight: int = 1


def _find_line(text: str, pattern: str, flags: int = 0) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line, flags):
            return number
    return None


def _ref(path: str, line: int | None = None, symbol: str | None = None) -> list[str]:
    refs = [path]
    if symbol and symbol != "<module>":
        refs.append(f"{path}::{symbol}")
    if line:
        refs.append(f"{path}:{line}")
    return refs


def _grep_refs(
    sources: Mapping[str, str], pattern: str, limit: int = 3, flags: int = 0
) -> list[str]:
    refs: list[str] = []
    for path, text in sources.items():
        line = _find_line(text, pattern, flags)
        if line:
            refs.extend(_ref(path, line))
            if len(refs) >= 2 * limit:
                break
    return refs


def extract(
    surface: TaskSurface, cfg: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], list[Signal]]:
    """Structural features plus the detector firings they support."""
    cfg = CALIBRATION if cfg is None else cfg
    base = f"tasks/{surface.task}"
    instruction_path = f"{base}/instruction.md"
    instr = surface.instruction
    instr_lower = instr.lower()
    tests_all = {**surface.tests_code, **surface.tests_data}
    tests_blob = "\n".join(tests_all.values())
    tests_code_blob = "\n".join(surface.tests_code.values())
    env_blob = "\n".join([*surface.env_code.values(), *surface.env_data.values()]).lower()
    env_names = " ".join(n[len(base) + 1 :] for n in surface.files if "/environment/" in n).lower()
    solution_blob = "\n".join(surface.solution_code.values())
    test_sh = next((t for p, t in surface.tests_code.items() if p.endswith("tests/test.sh")), "")
    test_sh_path = f"{base}/tests/test.sh"
    env_docker_path = f"{base}/environment/Dockerfile"
    tests_docker_path = f"{base}/tests/Dockerfile"
    env_docker = surface.env_code.get(env_docker_path, "")
    tests_docker = surface.tests_code.get(tests_docker_path, "")
    try:
        toml = tomllib.loads(surface.task_toml)
    except tomllib.TOMLDecodeError:
        toml = {}
    env_mode = str((toml.get("verifier") or {}).get("environment_mode") or "unspecified")

    signals_by_file: dict[str, PySignals] = {}
    for path, text in surface.tests_code.items():
        suffix = PurePosixPath(path).suffix
        if suffix == ".py":
            signals_by_file[path] = _py_signals(text)
        elif suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
            signals_by_file[path] = _js_signals(text)
    grader_files = [p for p in surface.tests_code if _kind(p) == "code"]
    asserts = sum(s.asserts for s in signals_by_file.values())
    signals: list[Signal] = []

    def primary_test_file() -> str:
        scored = sorted(signals_by_file.items(), key=lambda kv: (-kv[1].asserts, kv[0]))
        return scored[0][0] if scored else (test_sh_path if test_sh else instruction_path)

    # --- instruction surfaces
    code_tokens = sorted({t.strip() for t in re.findall(r"`([^`\n]{1,80})`", instr)})
    fences = "\n".join(re.findall(r"```[^\n]*\n(.*?)```", instr, re.S))
    field_like = {
        t for t in code_tokens if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)+", t)
    }
    field_like |= set(re.findall(r'"([A-Za-z][A-Za-z0-9_]{2,40})"\s*:', fences))
    output_paths = {
        m.group(1).rstrip(".,;:)")
        for m in re.finditer(r"(/(?:app|output|outputs|workspace|data|home|root)/[\w./\-]+)", instr)
        if re.search(
            r"(?i)(write|save|produce|output|create|generate|report|emit|store)",
            instr[max(0, m.start() - 120) : m.end() + 40],
        )
    }
    unread_fields = sorted(f for f in field_like if f not in tests_blob)
    unread_paths = sorted(
        p
        for p in output_paths
        if p not in tests_blob
        and PurePosixPath(p).name not in tests_blob
        and "." in PurePosixPath(p).name
    )
    unread = unread_fields + unread_paths

    # --- grader literals and constants absent from the instruction and agent image
    instr_numbers = _instruction_numbers(instr)
    test_only_literals: dict[str, tuple[str, int, str]] = {}
    test_only_numbers: dict[float, tuple[str, int, str]] = {}
    for path, sig in signals_by_file.items():
        for value, line, symbol in sig.graded_strings:
            low = value.lower()
            if (
                low in COMMON_LITERALS
                or low.startswith(("/tests", "/logs", "/tmp", "/proc", "/dev", "/usr", "/etc"))
                or low in instr_lower
                or PurePosixPath(low).name in instr_lower
                or low in env_blob
                or low in env_names
            ):
                continue
            test_only_literals.setdefault(value, (path, line, symbol))
        for value, line, symbol in sig.graded_numbers:
            if value in {0.0, 1.0, -1.0, 2.0, 100.0} or math.isnan(value):
                continue
            if value in instr_numbers or any(
                abs(value - n) <= 1e-9 * max(1.0, abs(n)) for n in instr_numbers
            ):
                continue
            test_only_numbers.setdefault(value, (path, line, symbol))

    # --- agent code reaching the grader
    sys_path_app = _grep_refs(
        surface.tests_code,
        r"sys\.path\.(insert|append)\([^)]*(['\"]/app|\bapp_?dir\b|\bAPP_?DIR\b|submission)|"
        r"PYTHONPATH=[^\s]*/app|spec_from_file_location\([^)]*['\"]/app|"
        r"require\(\s*['\"]/app|from\s+['\"]/app",
    )
    # The grader trusts a marker line printed by a process that also runs the submission.
    result_marker = (
        _grep_refs(
            surface.tests_code,
            r"""(find|startswith|search|match|split)\(\s*[rf]?['"][A-Z][A-Z_]{3,}[A-Z_|:= ]*['"]""",
        )
        if re.search(r"stdout|\.log\b|_log\b|LOG\b|capture_output", tests_code_blob)
        else []
    )
    agent_runs_in_verifier = _grep_refs(
        {test_sh_path: test_sh} if test_sh else {},
        r"^\s*(?:timeout\s+\S+\s+)?(?:python3?|bash|sh|node|npx|java|\./)\s*[^\n]*?/app/[\w./-]+\.(py|sh|js|ts|jar)\b",
        flags=re.M,
    )
    privilege_drop = bool(
        re.search(r"\b(runuser|setpriv|su\s+-|sudo\s+-u|nobody|setuid|chroot)\b", tests_code_blob)
    )
    agent_deps = _grep_refs(
        {test_sh_path: test_sh} if test_sh else {},
        r"(pip3?|uv\s+pip)\s+install[^\n]*(-r\s*|-e\s*)/app|xargs[^\n]*/app/[\w.-]*(apt|packages)|"
        r"npm\s+(ci|install)[^\n]*/app|source\s+/app/",
    )
    timing_files = [p for p, s in signals_by_file.items() if s.timing]
    perf_instruction = bool(
        re.search(
            r"(?i)\b(speed ?up|faster|latency|throughput|performance|runtime|benchmark|ms\b|milliseconds)",
            instr,
        )
    )

    # --- determinism and repetition
    det_required = bool(
        re.search(r"(?i)determinis|reproducib|same (?:inputs?|output|result|plan)", instr)
    )
    sol_random = bool(
        re.search(r"\brandom\b|np\.random|time\.time\(|datetime\.now|uuid4|\$RANDOM", solution_blob)
    )
    repeat_tested = bool(
        re.search(
            r"(?i)determinis|reproducib|twice|repeat|rerun|re-run|second[_ ]run|run_again|same output",
            tests_code_blob,
        )
    )
    det_unchecked = (det_required or sol_random) and not repeat_tested

    # --- surface-only document checks
    doc_format = re.search(
        r"openpyxl|load_workbook|\.xlsx|\.xlsm|python-docx|\bdocx\b|pptx|PyPDF|pdfplumber|FreeCAD|\.FCStd",
        tests_code_blob,
    )
    recompute = re.search(
        r"data_only\s*=\s*True|recalc|soffice|LibreOffice|formulas\.|\.recompute\(", tests_code_blob
    )
    existence = len(
        re.findall(
            r"\.exists\(\)|os\.path\.(?:exists|isfile|isdir)|is not None|isinstance\(|startswith\(\s*['\"]=",
            tests_code_blob,
        )
    )
    surface_only = bool(doc_format) and (not recompute or existence >= max(3, asserts // 3))

    # --- fixed visible inputs: the grader holds no input the agent has not seen
    tests_inputs = {
        PurePosixPath(n).name for n in surface.files if "/tests/" in n and _kind(n) != "code"
    }
    env_inputs = {PurePosixPath(n).name for n in surface.files if "/environment/" in n}
    hidden_inputs = tests_inputs - env_inputs
    randomized = bool(
        re.search(r"\brandom\b|hypothesis|faker|shuffle|secrets\.|uuid|np\.random", tests_code_blob)
    )
    fixed_inputs = bool(signals_by_file) and not hidden_inputs and not randomized and asserts > 0

    # --- answer-like files in the agent image
    answer_files = sorted(
        n
        for n in surface.files
        if "/environment/" in n and ANSWER_NAME.search(n[len(base) + len("/environment") + 1 :])
    )
    shared_files = sorted(
        n
        for n in surface.files
        if "/environment/" in n
        and _kind(n) == "data"
        and PurePosixPath(n).name in tests_inputs
        and ANSWER_NAME.search(PurePosixPath(n).name)
    )
    copies_grader = _grep_refs(
        {env_docker_path: env_docker} if env_docker else {},
        r"(?i)COPY\s+[^\n]*(tests|solution|oracle|reference)\b",
    )

    # --- environments
    agent_py, agent_py_line = python_version(env_docker)
    if env_mode == "separate" and tests_docker:
        verifier_py, verifier_py_line = python_version(tests_docker, test_sh)
    else:
        verifier_py, verifier_py_line = python_version("", test_sh)
        verifier_py = verifier_py or agent_py
    py_mismatch = bool(agent_py and verifier_py and agent_py != verifier_py)
    network = _grep_refs(
        {test_sh_path: test_sh} if test_sh else {},
        r"\b(curl|wget|git\s+clone|pip3?\s+install(?![^\n]*==)|npm\s+install|apt-get\s+install)\b",
    )
    timeouts = []
    for path in signals_by_file:
        text = surface.tests_code[path]
        for number, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(
                r"(?i)(?:timeout|deadline|wait_for|sleep)\w*\s*[=(:]\s*(\d+(?:\.\d+)?)", line
            ):
                value = float(match.group(1))
                if 0 < value <= cfg["short_timeout_max_sec"]:
                    timeouts.append((path, number))

    # --- grader code quality
    float_eq = [(p, n, s) for p, sig in signals_by_file.items() for n, s in sig.float_equalities]
    exact_text = _grep_refs(
        surface.tests_code,
        r"(read_text\(\)|\.read\(\)|stdout)\s*(\.strip\(\))?\s*==|==\s*\w*\.read_text\(\)",
    )
    nan_guard = bool(re.search(r"isfinite|isnan|math\.isinf|np\.isfinite|nan", tests_code_blob))
    float_parse = [(p, n, s) for p, sig in signals_by_file.items() for n, s in sig.float_parses]
    source_inspection = _grep_refs(
        surface.tests_code,
        r"['\"]/app/[\w./-]+\.(py|js|ts|tsx|jsx|scala|java|rs|go|c|cpp|bas|cls)['\"]"
        r"[^\n]*(read_text|open|read)|inspect\.getsource|\bast\.parse\(",
    )
    scored_instruction = bool(
        re.search(
            r"(?i)\b(partial credit|scored (on|by)|your score|score (is|will be)|graded on a scale|continuous)\b",
            instr,
        )
    )
    binary_reward = bool(
        re.search(r"echo\s+[01]\s*>\s*/logs/verifier/reward", test_sh)
    ) and not re.search(r"reward\.json|echo\s+\"?\$", test_sh)

    solution_loc = sum(
        1
        for text in surface.solution_code.values()
        for row in text.splitlines()
        if row.strip() and not row.strip().startswith(("#", "//"))
    )
    features = {
        "task": surface.task,
        "files_inventoried": len(surface.files),
        "files_read": len(surface.tests_code)
        + len(surface.tests_data)
        + len(surface.env_code)
        + len(surface.env_data)
        + len(surface.solution_code)
        + 2,
        "read_budget_truncated": surface.truncated,
        "test_file_count": len(grader_files),
        "test_assertion_count": asserts,
        "test_symbols": sum(len(s.test_symbols) for s in signals_by_file.values()),
        "dockerfile_has_sys_path_app": bool(sys_path_app),
        "task_toml_env_mode": env_mode,
        "instruction_code_tokens": len(code_tokens),
        "test_only_string_literals": len(test_only_literals),
        "test_only_numeric_constants": len(test_only_numbers),
        "solution_loc": solution_loc,
        "unread_output_mentions": len(unread),
        "env_python_mismatch": py_mismatch,
        "agent_python": agent_py,
        "verifier_python": verifier_py,
        "has_random_or_time_in_solution_without_repeat_test": det_unchecked,
        "has_surface_only_workbook_or_file_check": surface_only,
        "agent_code_runs_in_verifier_without_privilege_drop": bool(agent_runs_in_verifier)
        and not privilege_drop,
        "verifier_installs_agent_declared_dependencies": bool(agent_deps),
        "grader_trusts_printed_result_marker": bool(result_marker),
        "in_process_timing": bool(timing_files) and bool(sys_path_app),
        "timing_gate": bool(timing_files) and perf_instruction,
        "fixed_visible_inputs": fixed_inputs,
        "answer_like_environment_files": len(answer_files) + len(shared_files),
        "environment_copies_grader_surfaces": bool(copies_grader),
        "float_equality_checks": len(float_eq),
        "exact_text_equality_checks": len(exact_text) // 2,
        "unguarded_float_parses": 0 if nan_guard else len(float_parse),
        "agent_source_inspection": bool(source_inspection),
        "scored_instruction_binary_reward": scored_instruction and binary_reward,
        "grade_time_network_or_install": bool(network),
        "short_wall_clock_limits": len(timeouts),
    }

    main_test = primary_test_file()
    # ---------------- TS01 weak_check
    if len(unread) >= cfg["unread_output_min"]:
        signals.append(
            Signal(
                "unread_output_mentions",
                "weak_check",
                f"Instruction names outputs the grader never reads: {', '.join(unread[:8])}",
                [
                    instruction_path,
                    *_ref(instruction_path, _find_line(instr, re.escape(unread[0]))),
                    *sorted(grader_files)[:3],
                ],
                weight=max(1, len(unread) // 4),
            )
        )
    if det_unchecked and det_required:
        signals.append(
            Signal(
                "determinism_unchecked",
                "weak_check",
                "Instruction requires deterministic output but no test repeats a run",
                [
                    *_ref(instruction_path, _find_line(instr, r"(?i)determinis|reproducib|same")),
                    main_test,
                ],
            )
        )
    if surface_only:
        signals.append(
            Signal(
                "surface_only_document_check",
                "weak_check",
                "Document/model outputs are checked structurally (existence/type/formula prefix) without recomputation",
                [
                    *_grep_refs(
                        surface.tests_code, r"openpyxl|load_workbook|FreeCAD|FCStd|docx|pptx|PyPDF"
                    ),
                    main_test,
                ],
            )
        )
    if fixed_inputs:
        signals.append(
            Signal(
                "fixed_visible_inputs",
                "weak_check",
                "Grader checks one fixed input the agent can see; a hard-coded output passes",
                [
                    main_test,
                    *_ref(
                        main_test,
                        None,
                        (signals_by_file.get(main_test) or PySignals()).test_symbols[0][0]
                        if (signals_by_file.get(main_test) or PySignals()).test_symbols
                        else None,
                    ),
                ],
            )
        )
    if signals_by_file and asserts <= cfg["low_assertion_max"]:
        signals.append(
            Signal(
                "low_assertion_count",
                "weak_check",
                f"Only {asserts} assertions grade the task",
                [main_test, *sorted(grader_files)[:2]],
            )
        )
    if features["timing_gate"]:
        path = timing_files[0]
        line, symbol = signals_by_file[path].timing[0]
        signals.append(
            Signal(
                "timing_gate",
                "weak_check",
                "Speed requirement graded by a wall-clock measurement the submission can shape",
                _ref(path, line, symbol),
            )
        )
    if source_inspection:
        signals.append(
            Signal(
                "agent_source_inspection",
                "weak_check",
                "Grader inspects submitted source text for markers instead of behaviour",
                source_inspection,
            )
        )
    # ---------------- TS02 grader_logic_bug
    if float_parse and not nan_guard:
        path, line, symbol = float_parse[0]
        signals.append(
            Signal(
                "unguarded_float_parse",
                "grader_logic_bug",
                "Grader parses submitted numbers with float() and never rejects NaN/inf",
                _ref(path, line, symbol),
            )
        )
    # ---------------- TS03 answer_reachable
    if answer_files or shared_files or copies_grader:
        signals.append(
            Signal(
                "answer_like_environment_files",
                "answer_reachable",
                "Agent image ships answer-like or grader files: "
                + ", ".join(PurePosixPath(n).name for n in (answer_files + shared_files)[:6]),
                [*(answer_files + shared_files)[:4], *copies_grader],
            )
        )
    # ---------------- TS04 grader_tamperable
    if sys_path_app:
        signals.append(
            Signal(
                "agent_code_imported_into_grader",
                "grader_tamperable",
                "Grader imports agent-controlled code from /app into its own process",
                sys_path_app,
            )
        )
    if agent_runs_in_verifier and not privilege_drop:
        signals.append(
            Signal(
                "agent_code_runs_in_verifier",
                "grader_tamperable",
                "test.sh runs the submission as the grading user before/alongside the tests",
                agent_runs_in_verifier,
            )
        )
    if result_marker:
        signals.append(
            Signal(
                "self_reported_result_marker",
                "grader_tamperable",
                "Grader parses a marker line from output the submission's process can also print",
                result_marker,
            )
        )
    if agent_deps:
        signals.append(
            Signal(
                "agent_controls_verifier_dependencies",
                "grader_tamperable",
                "Verifier installs packages the submission declares",
                agent_deps,
            )
        )
    if features["in_process_timing"]:
        path = timing_files[0]
        line, symbol = signals_by_file[path].timing[0]
        signals.append(
            Signal(
                "in_process_timing",
                "grader_tamperable",
                "Timing is measured in the same process as imported agent code",
                _ref(path, line, symbol),
            )
        )
    if env_mode != "separate" and signals_by_file:
        signals.append(
            Signal(
                "shared_grading_environment",
                "grader_tamperable",
                "Grader runs inside the agent's container (no separate verifier environment)",
                [
                    *_ref(f"{base}/task.toml", _find_line(surface.task_toml, r"^\[verifier\]")),
                    main_test,
                ],
            )
        )
    # ---------------- TS05 unstated_requirement
    if len(test_only_literals) >= cfg["test_only_literal_min"]:
        items = sorted(test_only_literals.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        per = cfg["test_only_literals_per_prediction"]
        for start in range(0, len(items), per):
            chunk = items[start : start + per]
            refs: list[str] = []
            for _, (path, line, symbol) in chunk[:3]:
                refs.extend(_ref(path, line, symbol))
            signals.append(
                Signal(
                    "test_only_string_literals",
                    "unstated_requirement",
                    "Grader requires literals the instruction and agent files never state: "
                    + ", ".join(v for v, _ in chunk),
                    [instruction_path, *refs],
                )
            )
    if len(test_only_numbers) >= cfg["test_only_numeric_min"]:
        items = sorted(test_only_numbers.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        per = cfg["test_only_numerics_per_prediction"]
        for start in range(0, len(items), per):
            chunk = items[start : start + per]
            refs = []
            for _, (path, line, symbol) in chunk[:3]:
                refs.extend(_ref(path, line, symbol))
            signals.append(
                Signal(
                    "test_only_numeric_constants",
                    "unstated_requirement",
                    "Grader thresholds/tolerances absent from the instruction: "
                    + ", ".join(f"{v:g}" for v, _ in chunk),
                    [instruction_path, *refs],
                )
            )
    # ---------------- TS06 instruction_grader_contradiction
    if scored_instruction and binary_reward:
        signals.append(
            Signal(
                "scored_instruction_binary_reward",
                "instruction_grader_contradiction",
                "Instruction describes a graded score but test.sh writes a binary reward",
                [
                    *_ref(instruction_path, _find_line(instr, r"(?i)score|partial|continuous")),
                    *_ref(test_sh_path, _find_line(test_sh, r"reward")),
                ],
            )
        )
    # ---------------- TS07 overstrict_oracle
    if len(float_eq) >= cfg["float_equality_min"] or exact_text:
        refs = []
        for path, line, symbol in float_eq[:2]:
            refs.extend(_ref(path, line, symbol))
        signals.append(
            Signal(
                "exact_equality_oracle",
                "overstrict_oracle",
                "Grader compares floats or whole outputs by exact equality",
                refs + exact_text,
            )
        )
    # ---------------- TS09 env_mismatch_or_flaky
    if py_mismatch:
        signals.append(
            Signal(
                "env_python_mismatch",
                "env_mismatch_or_flaky",
                f"Agent image runs Python {agent_py} but the verifier runs Python {verifier_py}",
                [
                    *_ref(
                        env_docker_path, _find_line(env_docker, re.escape(agent_py_line or "FROM"))
                    ),
                    *(
                        (
                            _ref(
                                tests_docker_path,
                                _find_line(tests_docker, re.escape(verifier_py_line or "FROM")),
                            )
                        )
                        if tests_docker and verifier_py_line and verifier_py_line in tests_docker
                        else _ref(
                            test_sh_path,
                            _find_line(test_sh, re.escape(verifier_py_line or "python")),
                        )
                    ),
                ],
            )
        )
    if network:
        signals.append(
            Signal(
                "grade_time_network_or_install",
                "env_mismatch_or_flaky",
                "test.sh fetches or installs software at grading time",
                network,
            )
        )
    if timeouts:
        path, line = timeouts[0]
        signals.append(
            Signal(
                "short_wall_clock_limits",
                "env_mismatch_or_flaky",
                f"Grader enforces {len(timeouts)} wall-clock limit(s) of <= {cfg['short_timeout_max_sec']:g}s",
                _ref(path, line),
            )
        )
    disabled = set(cfg["disabled_detectors"])
    return features, [s for s in signals if s.detector not in disabled]


def _prediction(surface: TaskSurface, signal: Signal, index: int) -> dict[str, Any]:
    class_id, category, direction = _class_ref(signal.taxonomy_class)
    refs = list(dict.fromkeys(r for r in signal.refs if r))
    return {
        "schema": "qa.finding/v1",
        "id": f"triage:{surface.task}:{signal.detector}:{index}",
        "subject": {"kind": "task", "task_id": surface.task_id, "task_digest": surface.task_digest},
        "stage": STAGE,
        "verdict": "fail",
        "category": category,
        "severity": "medium" if direction == "wrong_pass" else "low",
        "fault": "eval",
        "summary": f"[{class_id} {signal.taxonomy_class}] {signal.summary}",
        "evidence_refs": refs,
        "reviewer": {"agent": REVIEWER, "model": None, "tokens": 0, "cost_usd": 0.0},
        "measurement": "not_run",
        "root_cause": [signal.taxonomy_class],
    }


def predictions_for(
    surface: TaskSurface, signals: list[Signal], cfg: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    cap = (CALIBRATION if cfg is None else cfg)["max_predictions_per_detector"]
    out: list[dict[str, Any]] = []
    per_detector: Counter = Counter()
    for signal in signals:
        for _ in range(min(signal.weight, cap)):
            if per_detector[signal.detector] >= cap:
                break
            out.append(_prediction(surface, signal, per_detector[signal.detector]))
            per_detector[signal.detector] += 1
    return out


def scan_view(
    view_dir: Path, *, cfg: Mapping[str, Any] | None = None, keep_surfaces: bool = False
) -> dict[str, Any]:
    """Scan one exported view through its sealed capability only.

    ``cfg`` defaults to the frozen CALIBRATION. ``keep_surfaces`` also returns the
    in-memory task surfaces (for dev calibration); they are never serialized.
    """
    view_dir = Path(view_dir).absolute()
    with fs_audit(view_dir, HERE) as audit:
        tools = views.agent_tools(view_dir)
        reader = SealedReader(tools)
        raw = reader.text("inputs.jsonl", 10_000)
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        split = records[0]["split"] if records else None
        tasks: dict[str, Any] = {}
        predictions: list[dict[str, Any]] = []
        surfaces: list[TaskSurface] = []
        for record in records:
            surface = load_surface(reader, record)
            features, signals = extract(surface, cfg)
            preds = predictions_for(surface, signals, cfg)
            tasks[surface.task] = {
                "features": features,
                "detectors": sorted({s.detector for s in signals}),
            }
            predictions.extend(preds)
            surfaces.append(surface)
        inventory = set(tools.files)
    outside = sorted(p for p in reader.paths_read if p not in inventory)
    result = {
        "split": split,
        "view": str(view_dir),
        "task_count": len(records),
        "tasks": tasks,
        "predictions": predictions,
        "read_audit": {
            "capability": f"{views.SealedWorkspaceTools.__module__}.SealedWorkspaceTools",
            "tool_calls": dict(reader.calls),
            "distinct_files_read": len(reader.paths_read),
            "files_read_outside_inventory": outside,
            "listing_clips_recovered": reader.listing_clips,
            "long_line_clips": reader.long_line_clips,
            "per_task_budget_stops": reader.budget_stops,
            "fs_events_under_view_or_key_mediated_by_capability": audit.mediated,
            "direct_fs_reads_of_view_or_key_files": len(audit.direct),
            "direct_fs_read_samples": audit.direct[:5],
            "all_reads_through_sealed_tools": not audit.direct and not outside,
        },
    }
    if keep_surfaces:
        result["surfaces"] = surfaces
    return result


def validate_predictions(predictions: Iterable[Mapping[str, Any]], tools: Any = None) -> int:
    """Check every record is a valid qa.finding/v1 whose path refs are inventoried."""
    count = 0
    for prediction in predictions:
        FindingModel.model_validate(prediction)
        if tools is not None:
            for ref in prediction["evidence_refs"]:
                path = re.split(r"::|:(?=\d+$)", ref)[0]
                if path not in tools.files:
                    raise ValueError(f"evidence ref outside the view inventory: {ref}")
        count += 1
    return count


# ----------------------------------------------------------------- evaluation

CELLS = ("dev", "test", "untuned_test", "unexposed_test")
REPORT_MODES = ("locus_and_class", "category_1to1", "task_any_1to1", "legacy_task_broadcast")


def _in_cell(cell: str, split: str, stratum: str) -> bool:
    if cell == "dev":
        return split == "dev"
    if cell == "test":
        return split == "test"
    if cell == "untuned_test":
        return split == "test" and stratum in {"unexposed", "inspected_only"}
    if cell == "unexposed_test":
        return split == "test" and stratum == "unexposed"
    raise ValueError(cell)


def _credited(
    key: Any, report: Mapping[str, Any], raw: list[Mapping[str, Any]]
) -> dict[str, tuple[set[str], set[str]]]:
    """mode -> (credited finding ids, credited prediction labels)."""
    out: dict[str, tuple[set[str], set[str]]] = {}
    for mode in score_audit.MODES:
        pairs = report["matches"][mode]
        out[mode] = ({f for _, f in pairs}, {p for p, _ in pairs})
    preds, _ = score_audit.normalize_predictions(raw)
    on_key = [p for p in preds if p.task in key.tasks]
    for mode in score_audit.LEGACY:
        items, hits = score_audit.legacy(key, on_key, mode)
        out[mode] = (items, {p.label for p in on_key if p.index in hits})
    return out


def _direction(class_id: str | None) -> str:
    if class_id in WRONG_PASS_IDS:
        return "wrong_pass_TS01_TS04"
    if class_id in WRONG_FAIL_IDS:
        return "wrong_fail_TS05_TS10"
    return "other"


def direction_breakdown(
    key: Any, credited: Mapping[str, tuple[set[str], set[str]]], raw: list[Mapping[str, Any]]
) -> dict[str, Any]:
    pred_dir = {
        str(p.get("id")): CLASSES[p["root_cause"][0]][2] for p in raw if p.get("root_cause")
    }
    pred_task = {str(p.get("id")): score_audit.task_name(p["subject"]["task_id"]) for p in raw}
    out: dict[str, Any] = {}
    for cell in CELLS:
        findings = [f for f in key.findings if _in_cell(cell, f.split, f.exposure_stratum)]
        cell_preds = [
            label
            for label, task in pred_task.items()
            if task in key.tasks
            and _in_cell(cell, key.tasks[task]["split"], key.tasks[task]["exposure_stratum"])
        ]
        row: dict[str, Any] = {}
        for mode in REPORT_MODES:
            items, labels = credited[mode]
            by_dir: dict[str, Any] = {}
            for direction in ("wrong_pass_TS01_TS04", "wrong_fail_TS05_TS10", "other"):
                group = [f for f in findings if _direction(f.taxonomy_class_id) == direction]
                if not group and direction == "other":
                    continue
                by_dir[direction] = score_audit.rate(
                    sum(f.item_id in items for f in group), len(group)
                )
            for direction, predicted in (
                ("wrong_pass", "predicted_wrong_pass"),
                ("wrong_fail", "predicted_wrong_fail"),
            ):
                mine = [lab for lab in cell_preds if pred_dir.get(lab) == direction]
                by_dir[f"precision_{predicted}"] = score_audit.rate(
                    sum(lab in labels for lab in mine), len(mine)
                )
            row[mode] = by_dir
        by_class = {}
        for class_id in sorted({f.taxonomy_class_id or "none" for f in findings}):
            group = [f for f in findings if (f.taxonomy_class_id or "none") == class_id]
            by_class[class_id] = {
                mode: score_audit.rate(
                    sum(f.item_id in credited[mode][0] for f in group), len(group)
                )
                for mode in ("category_1to1", "task_any_1to1")
            }
        row["category_1to1_recall_by_class"] = by_class
        out[cell] = row
    return out


def _cell_items(key: Any, cell: str) -> set[str]:
    return {f.item_id for f in key.findings if _in_cell(cell, f.split, f.exposure_stratum)}


def complementarity(key: Any, credited: Mapping[str, tuple[set[str], set[str]]]) -> dict[str, Any]:
    """Overlap of triage-credited findings with Epoch, Tokenless and the dev witnesses."""
    baselines = score_audit.baseline_predictions(HERE)
    others: dict[str, tuple[str, set[str]]] = {}
    for name, mode in (
        ("epoch_auditor_projection", "exact_id"),
        ("tokenless_auditor_projection", "exact_id"),
        ("confirmed_dev_witnesses", "locus_and_class"),
    ):
        report = score_audit.score(key, baselines[name]["predictions"])
        others[name] = (mode, {f for _, f in report["matches"][mode]})
    triage_items = credited["category_1to1"][0]
    out: dict[str, Any] = {
        "triage_mode": "category_1to1",
        "note": "Auditor projections credit the PM findings the key joins to each flag (exact_id); "
        "witnesses credit their port-test locus (locus_and_class). Triage credit is category_1to1, "
        "the strongest mode a blind static scanner without port-test paths can reach.",
    }
    for cell in ("dev", "test", "untuned_test", "unexposed_test"):
        universe = _cell_items(key, cell)
        mine = triage_items & universe
        row: dict[str, Any] = {"findings": len(universe), "triage": len(mine)}
        union_all = set(mine)
        for name, (mode, items) in others.items():
            theirs = items & universe
            union_all |= theirs
            row[name] = {
                "mode": mode,
                "found": len(theirs),
                "both": len(mine & theirs),
                "triage_only": len(mine - theirs),
                "other_only": len(theirs - mine),
                "union_recall": score_audit.rate(len(mine | theirs), len(universe)),
            }
        known = set().union(*(items & universe for _, items in others.values()))
        row["triage_findings_no_other_source_found"] = len(mine - known)
        row["union_all_recall"] = score_audit.rate(len(union_all), len(universe))
        out[cell] = row
    return out


def locus_reachability(key: Any, inventories: Mapping[str, set[str]]) -> dict[str, Any]:
    """How many key findings cite a port-test path that exists in the blind view."""
    out = {}
    for split, inventory in inventories.items():
        findings = [f for f in key.findings if f.split == split]
        reachable = 0
        for finding in findings:
            paths = [
                f"tasks/{finding.task}/{p.pattern.replace(chr(92), '')}" for p in finding.locus
            ]
            reachable += any(p in inventory for p in paths)
        out[split] = score_audit.rate(reachable, len(findings))
    return out


def detector_table(
    key: Any, credited: Mapping[str, tuple[set[str], set[str]]], raw: list[Mapping[str, Any]]
) -> dict[str, Any]:
    table: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for pred in raw:
        task = score_audit.task_name(pred["subject"]["task_id"])
        split = key.tasks[task]["split"] if task in key.tasks else "off_key"
        detector = str(pred["id"]).split(":")[2]
        table[detector][split]["predictions"] += 1
        table[detector][split]["credited_category_1to1"] += (
            pred["id"] in credited["category_1to1"][1]
        )
        table[detector][split]["credited_task_any_1to1"] += (
            pred["id"] in credited["task_any_1to1"][1]
        )
    return {
        det: {split: dict(c) for split, c in sorted(rows.items())}
        for det, rows in sorted(table.items())
    }


def _headline(report: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cell in ("all", *CELLS):
        out[cell] = {}
        for mode in (*score_audit.MODES, *score_audit.LEGACY):
            entry = report["modes"][mode]["headline"][cell]
            out[cell][mode] = {
                "recall": entry["recall"],
                "precision": entry["precision"],
                "f1": entry["f1"],
            }
        out[cell]["task_collision_inflation"] = report["task_collision_inflation"][cell]
    return out


def _dev_cell(surfaces: list[TaskSurface], key: Any, cfg: Mapping[str, Any]) -> dict[str, Any]:
    preds: list[dict[str, Any]] = []
    for surface in surfaces:
        _, signals = extract(surface, cfg)
        preds.extend(predictions_for(surface, signals, cfg))
    return score_audit.score(key, preds)["modes"]["category_1to1"]["headline"]["dev"]


def _objective(cell: Mapping[str, Any]) -> tuple[float, float]:
    """Dev category_1to1 F1, then recall, as the calibration objective."""
    return (cell["f1"] or 0.0, cell["recall"]["rate"] or 0.0)


def calibrate(dev_surfaces: list[TaskSurface], key: Any) -> dict[str, Any]:
    """Dev-only calibration: coordinate descent over CALIBRATION_GRID, then greedy detector removal.

    Starts from UNCALIBRATED; a change is kept only if it strictly improves the dev
    objective (ties keep the earlier value), so the result is deterministic.
    """
    if any(s.task not in key.tasks or key.tasks[s.task]["split"] != "dev" for s in dev_surfaces):
        raise ValueError("calibration accepts dev-split task surfaces only")
    cfg = dict(UNCALIBRATED)
    start = _dev_cell(dev_surfaces, key, cfg)
    best = _objective(start)
    trail: list[dict[str, Any]] = []
    for _ in range(3):
        changed = False
        for name, values in CALIBRATION_GRID.items():
            for value in values:
                if value == cfg[name]:
                    continue
                trial = cfg | {name: value}
                score = _objective(_dev_cell(dev_surfaces, key, trial))
                if score > best:
                    best, cfg, changed = score, trial, True
                    trail.append({"set": name, "value": value, "dev_f1": score[0]})
        if not changed:
            break
    detectors = sorted(
        {signal.detector for surface in dev_surfaces for signal in extract(surface, cfg)[1]}
    )
    while True:
        removal = None
        for detector in detectors:
            if detector in cfg["disabled_detectors"]:
                continue
            trial = cfg | {"disabled_detectors": sorted([*cfg["disabled_detectors"], detector])}
            score = _objective(_dev_cell(dev_surfaces, key, trial))
            if score > best:
                best, removal = score, trial
        if removal is None:
            break
        trail.append(
            {"set": "disabled_detectors", "value": removal["disabled_detectors"], "dev_f1": best[0]}
        )
        cfg = removal
    final = _dev_cell(dev_surfaces, key, cfg)
    return {
        "objective": "dev category_1to1 F1 (tie-break: recall)",
        "grid": CALIBRATION_GRID,
        "uncalibrated": UNCALIBRATED,
        "uncalibrated_dev": {k: start[k] for k in ("recall", "precision", "f1")},
        "calibrated": cfg,
        "calibrated_dev": {k: final[k] for k in ("recall", "precision", "f1")},
        "trail": trail,
        "detectors_seen_on_dev": detectors,
    }


def evaluate(dev_view: Path, test_view: Path) -> dict[str, Any]:
    """Scan dev, prove the frozen CALIBRATION is the dev optimum, then scan test once."""
    key_inputs = {
        name: "sha256:" + hashlib.sha256((score_audit.KEYS_ROOT / name).read_bytes()).hexdigest()
        for name in (
            "tb4-audit-v1/items.jsonl",
            "tb4-audit-v1/tasks.jsonl",
            "splits/exposure.jsonl",
        )
    }
    key = score_audit.load_key(HERE, score_audit.KEYS_ROOT)
    dev_scan = scan_view(dev_view, keep_surfaces=True)
    if dev_scan["split"] != "dev":
        raise ValueError("the dev view is not a dev export")
    dev_surfaces = dev_scan.pop("surfaces")
    calibration = calibrate(dev_surfaces, key)
    if calibration["calibrated"] != CALIBRATION:
        raise ValueError("CALIBRATION is not the dev calibration result; refreeze before test")
    test_scan = scan_view(test_view, keep_surfaces=True)
    test_surfaces = test_scan.pop("surfaces")
    if test_scan["split"] != "test":
        raise ValueError("the test view is not a test export")
    scans = {"dev": dev_scan, "test": test_scan}
    inventories = {
        split: set(views.agent_tools(Path(s["view"])).files) for split, s in scans.items()
    }
    valid = {}
    for split, scan in scans.items():
        valid[split] = validate_predictions(
            scan["predictions"], views.agent_tools(Path(scan["view"]))
        )
    raw = [*scans["dev"]["predictions"], *scans["test"]["predictions"]]
    report = score_audit.score(key, raw)
    credited = _credited(key, report, raw)
    key_tasks = {
        split: sorted(t for t, r in key.tasks.items() if r["split"] == split) for split in scans
    }
    # Pre-registered secondary arm: every detector at UNCALIBRATED thresholds, fixed before test.
    uncal_raw = [
        p
        for surface in (*dev_surfaces, *test_surfaces)
        for p in predictions_for(surface, extract(surface, UNCALIBRATED)[1], UNCALIBRATED)
    ]
    uncal_report = score_audit.score(key, uncal_raw)
    uncal_credited = _credited(key, uncal_report, uncal_raw)
    uncalibrated_arm = {
        "description": "All detectors at UNCALIBRATED thresholds (no dev tuning); fixed before the "
        "test scan, reported to show how much the dev calibration transfers.",
        "config": UNCALIBRATED,
        "predictions": Counter(
            key.tasks[score_audit.task_name(p["subject"]["task_id"])]["split"] for p in uncal_raw
        ),
        "headline": _headline(uncal_report),
        "error_direction": direction_breakdown(key, uncal_credited, uncal_raw),
        "detectors": detector_table(key, uncal_credited, uncal_raw),
    }
    source = Path(__file__).read_bytes()
    return {
        "schema": EVALUATION_SCHEMA,
        "key_id": score_audit.KEY_ID,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paid_cost_usd": 0.0,
        "model_calls": 0,
        "key_inputs_sha256": key_inputs,
        "scanner": {
            "path": "research/answer-keys/tb4-audit-v1/triage_signals.py",
            "sha256": "sha256:" + hashlib.sha256(source).hexdigest(),
            "calibration": CALIBRATION,
            "caps": CAPS,
            "protocol": "Thresholds chosen on dev only; the frozen scanner was run once on test.",
        },
        "inputs": {
            split: {
                "view": scan["view"],
                "bundle_sha256": "sha256:"
                + hashlib.sha256((Path(scan["view"]) / "bundle.json").read_bytes()).hexdigest(),
                "tasks_scanned": scan["task_count"],
                "tasks_in_key": len(key_tasks[split]),
                "findings_in_key": sum(1 for f in key.findings if f.split == split),
                "predictions": len(scan["predictions"]),
                "valid_qa_finding_v1": valid[split],
            }
            for split, scan in scans.items()
        },
        "read_audit": {split: scan["read_audit"] for split, scan in scans.items()}
        | {
            "all_reads_through_sealed_tools": all(
                s["read_audit"]["all_reads_through_sealed_tools"] for s in scans.values()
            ),
            "direct_fs_reads_of_view_or_key_files": sum(
                s["read_audit"]["direct_fs_reads_of_view_or_key_files"] for s in scans.values()
            ),
        },
        "headline": _headline(report),
        "error_direction": direction_breakdown(key, credited, raw),
        "complementarity": complementarity(key, credited),
        "locus_reachability": locus_reachability(key, inventories),
        "detectors": detector_table(key, credited, raw),
        "dev_calibration": calibration | {"frozen_matches_dev_calibration": True},
        "uncalibrated_arm": uncalibrated_arm,
        "features": {split: scan["tasks"] for split, scan in scans.items()},
        "predictions": raw,
        "score_predictions_tally": report["predictions"],
        "notes": [
            "locus_and_class needs a port-test path or symbol from the Karotte port; those files are "
            "not in the upstream view (see locus_reachability), so a blind scanner citing only real "
            "view files cannot earn locus credit. category_1to1 is the strongest reachable mode.",
            "Precision is a lower bound: no certified-clean tasks exist and an unmatched candidate may "
            "be a real defect PM did not list.",
            "Every candidate is a suspected static label (measurement=not_run); none meets a class "
            "evidence standard.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dev-view", type=Path, required=True)
    parser.add_argument("--test-view", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = evaluate(args.dev_view, args.test_view)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        cell: {
            mode: payload["headline"][cell][mode]["recall"]["rate"]
            for mode in (
                "locus_and_class",
                "category_1to1",
                "task_any_1to1",
                "legacy_task_broadcast",
            )
        }
        for cell in CELLS
    }
    print(
        json.dumps(
            {
                "out": str(args.out),
                "recall": summary,
                "all_reads_through_sealed_tools": payload["read_audit"][
                    "all_reads_through_sealed_tools"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
