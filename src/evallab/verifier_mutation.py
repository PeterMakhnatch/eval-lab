"""Verifier mutation testing for Harbor task packages.

The most common grader defect is a verifier that accepts a wrong answer: in
Tokenless's EnvCheck release, 35 of 66 published write-ups are "insufficient
checking", and most of their inputs are one-token edits of the reference
solution's own output (``lok`` -> ``not lok``, ``min`` -> ``max``, ``+`` ->
``-``). This module searches for those inputs mechanically, the way mutation
testing grades a test suite:

1. **Capture** (two controls). The reference solution runs inside the task
   environment and the target files it leaves behind are copied out through
   Harbor's ``/logs/artifacts`` channel. The same copy without the reference
   solution captures the untouched environment and is the negative control.
2. **Mutate** (one blank control per file plus the selected mutants). Each
   mutant is the reference solution followed by one small behavioural edit to
   one captured file; a blank control truncates the file instead. The task's
   own verifier grades every run.

A file whose blank control still earns reward is not graded through that file.
A mutant that earns full reward while its file's blank control earns zero is a
*survivor*: a concrete input the verifier cannot tell apart from the reference
solution. A survivor is a hypothesis, never a finding: it can be behaviour
equivalent or change behaviour the instruction does not ask for. Confirmation
is a human reading or an objective check.

Every run is a solution-override control on the oracle channel through
:func:`evallab.runner.run_matrix`: free, local Docker, no model.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import hashlib
import json
import re
import shlex
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from evallab.execution_contracts import new_ulid
from evallab.registry import compute_task_digests
from evallab.runner import run_matrix
from evallab.schemas import ContractModel, ExperimentMatrix, MatrixRun

if TYPE_CHECKING:
    from evallab.vcheck import GradeResult, Submission

SCHEMA_REPORT = "evallab.verifier_mutation.report/v1"
SCHEMA_HYPOTHESIS = "evallab.verifier_mutation.hypothesis/v1"

#: Where captured files land inside the trial environment. Harbor 0.24 collects
#: the agent environment's ``/logs/artifacts`` into ``<trial>/artifacts/logs/artifacts``.
CAPTURE_ROOT = "/logs/artifacts/envcheck-capture"
_CAPTURE_HOST = ("artifacts", "logs", "artifacts", "envcheck-capture")

#: Captured files larger than this are not mutated.
MAX_FILE_BYTES = 262_144
#: Directory targets contribute at most this many files.
MAX_FILES_PER_TARGET = 12
#: ``run_matrix`` posture for local controls (docs/execution-tiers.md: <= 2 concurrent).
MAX_WORKERS = 2

_SKIP_PARTS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages", "dist-packages"}
)
#: Build and package manifests: their fields are rarely the task's answer, and every
#: dropped ``description`` key would otherwise surface as a survivor.
_MANIFESTS = frozenset(
    {"package.json", "package-lock.json", "tsconfig.json", "jsconfig.json", "composer.json"}
)
_C_FAMILY = frozenset(
    {
        ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala",
    }
)  # fmt: skip

Language = Literal["python", "c-family", "json"]
RunRole = Literal["capture-oracle", "capture-base", "blank", "mutant"]
Outcome = Literal["killed", "survived", "partial", "uninformative", "unscored", "control"]
Verdict = Literal[
    "survivors_found", "no_survivors", "task_broken", "unscored", "planned", "no_mutants"
]


def language_of(path: str) -> Language | None:
    if PurePosixPath(path).name in _MANIFESTS:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".json":
        return "json"
    if suffix in _C_FAMILY:
        return "c-family"
    return None


# --------------------------------------------------------------------------- #
# Mutants
# --------------------------------------------------------------------------- #


class Mutant(ContractModel):
    """One behavioural edit to one captured file, with its exact bytes."""

    id: str
    file: str
    language: Language
    operator: str
    line: int
    original: str
    replacement: str
    changed_line: bool
    patch: str
    mutated_sha256: str


@dataclass(frozen=True)
class _Edit:
    operator: str
    start: int
    end: int
    replacement: bytes


def _line_starts(source: bytes) -> list[int]:
    starts = [0]
    for index, byte in enumerate(source):
        if byte == 0x0A:
            starts.append(index + 1)
    return starts


def _between_is_operator(segment: str, operator: str) -> int | None:
    """Offset of ``operator`` in the text between two operands, if it is the only token."""
    index = segment.find(operator)
    if index < 0:
        return None
    rest = segment[:index] + segment[index + len(operator) :]
    rest = re.sub(r"#[^\n]*", "", rest)
    if rest.strip(" \t\r\n()\\"):
        return None
    return index


_COMPARE_SWAPS: dict[type[ast.cmpop], tuple[str, str, str]] = {
    ast.Lt: ("<", "<=", "boundary"),
    ast.LtE: ("<=", "<", "boundary"),
    ast.Gt: (">", ">=", "boundary"),
    ast.GtE: (">=", ">", "boundary"),
    ast.Eq: ("==", "!=", "negate-compare"),
    ast.NotEq: ("!=", "==", "negate-compare"),
}
_BINOP_SWAPS: dict[type[ast.operator], tuple[str, str]] = {
    ast.Add: ("+", "-"),
    ast.Sub: ("-", "+"),
    ast.Mult: ("*", "/"),
    ast.Div: ("/", "*"),
}
_CALL_SWAPS = {"min": "max", "max": "min", "any": "all", "all": "any"}
_FLAG_KEY = re.compile(
    r"(?:^|_)(?:ok|is|has|valid|feasible|pass|passed|enabled|allowed|flag|success|required)(?:$|_)",
    re.IGNORECASE,
)


def _boolean_names(tree: ast.AST) -> set[str]:
    """Names assigned from comparisons, boolean operators or ``not`` anywhere in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets.append(node.target)
        if value is None:
            continue
        boolean = isinstance(value, ast.Compare | ast.BoolOp) or (
            isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not)
        )
        if not boolean:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Tuple):
                names.update(element.id for element in target.elts if isinstance(element, ast.Name))
    return names


def _python_edits(source: bytes) -> list[_Edit]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    starts = _line_starts(source)

    def offset(line: int, column: int) -> int:
        return starts[line - 1] + column

    def span(node: ast.expr | ast.stmt) -> tuple[int, int]:
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        end_column = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        return offset(node.lineno, node.col_offset), offset(end_line, end_column)

    def text(node: ast.expr) -> str:
        start, end = span(node)
        return source[start:end].decode("utf-8")

    def operator_between(
        left: ast.expr, right: ast.expr, token: str, replacement: str, name: str
    ) -> _Edit | None:
        lo, hi = span(left)[1], span(right)[0]
        segment = source[lo:hi].decode("utf-8", errors="replace")
        found = _between_is_operator(segment, token)
        if found is None:
            return None
        start = lo + len(segment[:found].encode("utf-8"))
        return _Edit(name, start, start + len(token.encode()), replacement.encode())

    def is_text(node: ast.AST) -> bool:
        return isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)
        )

    boolean_names = _boolean_names(tree)
    edits: list[_Edit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left = node.left
            for op, right in zip(node.ops, node.comparators, strict=True):
                swap = _COMPARE_SWAPS.get(type(op))
                if swap is not None:
                    edit = operator_between(left, right, swap[0], swap[1], swap[2])
                    if edit is not None:
                        edits.append(edit)
                left = right
        elif isinstance(node, ast.BinOp):
            swap_op = _BINOP_SWAPS.get(type(node.op))
            if swap_op is not None and not (is_text(node.left) or is_text(node.right)):
                edit = operator_between(node.left, node.right, swap_op[0], swap_op[1], "arithmetic")
                if edit is not None:
                    edits.append(edit)
        elif isinstance(node, ast.BoolOp):
            token, replacement = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
            for left, right in zip(node.values, node.values[1:], strict=False):
                edit = operator_between(left, right, token, replacement, "boolean-operator")
                if edit is not None:
                    edits.append(edit)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not | ast.USub):
            start = span(node)[0]
            operand_start = span(node.operand)[0]
            name = "drop-not" if isinstance(node.op, ast.Not) else "drop-negation"
            edits.append(_Edit(name, start, operand_start, b""))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _CALL_SWAPS
        ):
            start, end = span(node.func)
            edits.append(_Edit("min-max", start, end, _CALL_SWAPS[node.func.id].encode()))
        elif isinstance(node, ast.IfExp):
            start, end = span(node)
            for branch in (node.body, node.orelse):
                edits.append(_Edit("conditional-branch", start, end, f"({text(branch)})".encode()))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            start, end = span(node)
            edits.append(_Edit("boolean-constant", start, end, str(not node.value).encode()))
        elif (
            isinstance(node, ast.Constant)
            and type(node.value) is int
            and source[span(node)[0] : span(node)[1]].isdigit()
        ):
            start, end = span(node)
            edits.append(_Edit("integer-shift", start, end, str(node.value + 1).encode()))
        elif isinstance(node, ast.If | ast.While):
            start, end = span(node.test)
            edits.append(_Edit("negate-condition", start, end, f"not ({text(node.test)})".encode()))
        elif (isinstance(node, ast.AugAssign) and node.lineno == node.end_lineno) or (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and node.lineno == node.end_lineno
        ):
            start, end = span(node)
            edits.append(_Edit("drop-statement", start, end, b"pass"))
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in boolean_names
        ):
            start, end = span(node)
            edits.append(_Edit("negate-boolean", start, end, f"not {node.id}".encode()))
        elif isinstance(node, ast.Dict):
            # Output records name their flags; ``"landing_weight_ok": lok`` -> ``not lok``.
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and _FLAG_KEY.search(key.value)
                    and not (isinstance(value, ast.Name) and value.id in boolean_names)
                    and not (isinstance(value, ast.Constant) and isinstance(value.value, bool))
                ):
                    start, end = span(value)
                    edits.append(
                        _Edit("negate-boolean", start, end, f"not ({text(value)})".encode())
                    )
    return edits


_C_TOKEN = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])')
  | (?P<op>===|!==|==|!=|<=|>=|&&|\|\||[ ]\+[ ]|[ ]-[ ]|[ ]<[ ]|[ ]>[ ])
  | (?P<word>\b(?:true|false)\b|\bMath\.(?:min|max)\b|(?<![\w.])(?:min|max)(?=\s*\())
  | (?P<dotcall>\.(?:min|max)(?=\s*\())
    """,
    re.VERBOSE | re.DOTALL,
)
_C_SWAPS = {
    "===": ("!==", "negate-compare"), "!==": ("===", "negate-compare"),
    "==": ("!=", "negate-compare"), "!=": ("==", "negate-compare"),
    "<=": ("<", "boundary"), ">=": (">", "boundary"),
    " < ": (" <= ", "boundary"), " > ": (" >= ", "boundary"),
    "&&": ("||", "boolean-operator"), "||": ("&&", "boolean-operator"),
    " + ": (" - ", "arithmetic"), " - ": (" + ", "arithmetic"),
    "true": ("false", "boolean-constant"), "false": ("true", "boolean-constant"),
    "Math.min": ("Math.max", "min-max"), "Math.max": ("Math.min", "min-max"),
    "min": ("max", "min-max"), "max": ("min", "min-max"),
    ".min": (".max", "min-max"), ".max": (".min", "min-max"),
}  # fmt: skip


def _c_family_edits(source: bytes) -> list[_Edit]:
    """Lexical operator swaps outside strings and comments; no parse, so no compile check."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return []
    edits: list[_Edit] = []
    for match in _C_TOKEN.finditer(text):
        kind = match.lastgroup
        if kind in {"comment", "string"}:
            continue
        token = match.group(0)
        swap = _C_SWAPS.get(token)
        if swap is None:
            continue
        start = len(text[: match.start()].encode("utf-8"))
        edits.append(_Edit(swap[1], start, start + len(token.encode()), swap[0].encode()))
    return edits


def _json_indent(text: str) -> int | None:
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    second = lines[1]
    return len(second) - len(second.lstrip(" ")) or None


def _json_mutations(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[str, tuple, Any]]:
    """(operator, path, replacement) for each leaf flip and each structural drop."""
    if isinstance(value, bool):
        yield "boolean-constant", path, not value
    elif isinstance(value, int):
        yield "integer-shift", path, value + 1
    elif isinstance(value, float):
        yield "number-scale", path, value * 1.5 if value else 1.0
    elif isinstance(value, str):
        if value:
            yield "empty-string", path, ""
    elif isinstance(value, list):
        if value:
            yield "drop-last-item", path, value[:-1]
        for index, item in enumerate(value):
            yield from _json_mutations(item, (*path, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield "drop-key", (*path, key), _DROP
            yield from _json_mutations(item, (*path, key))


_DROP = object()


def _json_replace(document: Any, path: tuple, replacement: Any) -> Any:
    if not path:
        return replacement
    head, *rest = path
    if isinstance(document, dict):
        copy = dict(document)
        if not rest and replacement is _DROP:
            del copy[head]
        else:
            copy[head] = _json_replace(document[head], tuple(rest), replacement)
        return copy
    copy_list = list(document)
    copy_list[head] = _json_replace(document[head], tuple(rest), replacement)
    return copy_list


def _json_variants(source: bytes) -> list[tuple[str, bytes, str, str]]:
    """(operator, mutated bytes, original summary, replacement summary)."""
    try:
        text = source.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    indent = _json_indent(text)
    trailing = "\n" if text.endswith("\n") else ""
    variants: list[tuple[str, bytes, str, str]] = []
    for operator, path, replacement in _json_mutations(document):
        mutated = _json_replace(document, path, replacement)
        rendered = json.dumps(mutated, indent=indent, ensure_ascii=False) + trailing
        location = "$" + "".join(f"[{json.dumps(part)}]" for part in path)
        old: Any = document
        for part in path:
            old = old[part]
        before = f"{location} = {json.dumps(old, ensure_ascii=False)[:80]}"
        after = "removed" if replacement is _DROP else json.dumps(replacement)[:80]
        variants.append((operator, rendered.encode("utf-8"), before, after))
    return variants


def _changed_lines(base: bytes | None, oracle: bytes) -> set[int]:
    """1-based oracle lines the reference solution added or rewrote (all when created)."""
    oracle_lines = oracle.decode("utf-8", errors="replace").splitlines()
    if base is None:
        return set(range(1, len(oracle_lines) + 1))
    base_lines = base.decode("utf-8", errors="replace").splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(a=base_lines, b=oracle_lines, autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            changed.update(range(j1 + 1, j2 + 1))
    return changed


def _unified_patch(path: str, before: bytes, after: bytes) -> str:
    relative = path.lstrip("/")
    return "".join(
        difflib.unified_diff(
            before.decode("utf-8", errors="replace").splitlines(keepends=True),
            after.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            n=3,
        )
    )


def _mutant_id(path: str, operator: str, mutated: bytes) -> str:
    digest = hashlib.sha256(f"{path}\0{operator}\0".encode() + mutated).hexdigest()
    return digest[:10]


def _compiles(language: Language, mutated: bytes) -> bool:
    if language != "python":
        return True
    try:
        compile(mutated, "<mutant>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def _python_comment_or_docstring_lines(source: bytes) -> set[int]:
    """Lines inside docstrings; their constants are not behaviour."""
    lines: set[int] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return lines
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return lines


def generate_mutants(
    path: str, oracle: bytes, base: bytes | None = None
) -> list[tuple[Mutant, bytes]]:
    """Every valid mutant of one captured file with its bytes, in source order, deduplicated."""
    language = language_of(path)
    if language is None or len(oracle) > MAX_FILE_BYTES:
        return []
    changed = _changed_lines(base, oracle)
    seen: set[bytes] = {oracle}
    mutants: list[tuple[Mutant, bytes]] = []
    if language == "json":
        for operator, mutated, before, after in _json_variants(oracle):
            if mutated in seen:
                continue
            seen.add(mutated)
            patch = _unified_patch(path, oracle, mutated)
            mutants.append(
                (
                    Mutant(
                        id=_mutant_id(path, operator, mutated),
                        file=path,
                        language=language,
                        operator=operator,
                        line=_first_changed_line(oracle, mutated),
                        original=before,
                        replacement=after,
                        changed_line=True,
                        patch=patch,
                        mutated_sha256=hashlib.sha256(mutated).hexdigest(),
                    ),
                    mutated,
                )
            )
        return mutants
    edits = _python_edits(oracle) if language == "python" else _c_family_edits(oracle)
    starts = _line_starts(oracle)
    skip_lines = _python_comment_or_docstring_lines(oracle) if language == "python" else set()
    for edit in sorted(edits, key=lambda item: (item.start, item.end, item.operator)):
        line = _line_of(starts, edit.start)
        if line in skip_lines:
            continue
        mutated = oracle[: edit.start] + edit.replacement + oracle[edit.end :]
        if mutated in seen or not _compiles(language, mutated):
            continue
        seen.add(mutated)
        mutants.append(
            (
                Mutant(
                    id=_mutant_id(path, edit.operator, mutated),
                    file=path,
                    language=language,
                    operator=edit.operator,
                    line=line,
                    original=oracle[edit.start : edit.end].decode("utf-8", errors="replace"),
                    replacement=edit.replacement.decode("utf-8", errors="replace"),
                    changed_line=line in changed,
                    patch=_unified_patch(path, oracle, mutated),
                    mutated_sha256=hashlib.sha256(mutated).hexdigest(),
                ),
                mutated,
            )
        )
    return mutants


def _line_of(starts: Sequence[int], offset: int) -> int:
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low + 1


def _first_changed_line(before: bytes, after: bytes) -> int:
    old, new = before.splitlines(), after.splitlines()
    for index, (left, right) in enumerate(zip(old, new, strict=False), start=1):
        if left != right:
            return index
    return min(len(old), len(new)) + 1


def select_mutants(mutants: Sequence[Mutant], *, limit: int, seed: str = "") -> list[Mutant]:
    """A deterministic sample balanced across files, then operators, preferring oracle lines.

    Files take turns. Inside a file, pass one takes at most one mutant per line by
    cycling over operator queues; pass two continues with what is left, same
    order. Inside each operator queue, mutants on lines the reference solution
    wrote come first, then a seeded hash order spreads picks across the file.
    """
    if limit <= 0:
        return []
    by_file: dict[str, list[Mutant]] = {}
    for mutant in mutants:
        by_file.setdefault(mutant.file, []).append(mutant)
    orders = [_file_order(by_file[path], seed) for path in sorted(by_file)]
    chosen: list[Mutant] = []
    for rank in range(max(map(len, orders), default=0)):
        for order in orders:
            if rank < len(order):
                chosen.append(order[rank])
                if len(chosen) == limit:
                    return chosen
    return chosen


def _file_order(mutants: Sequence[Mutant], seed: str) -> list[Mutant]:
    def rank(mutant: Mutant) -> tuple[int, str]:
        digest = hashlib.sha256(f"{seed}\0{mutant.id}".encode()).hexdigest()
        return (0 if mutant.changed_line else 1, digest)

    queues: dict[str, list[Mutant]] = {}
    for mutant in sorted(mutants, key=rank):
        queues.setdefault(mutant.operator, []).append(mutant)
    operators = sorted(queues, key=lambda name: (-len(queues[name]), name))
    order: list[Mutant] = []
    taken: set[str] = set()
    for distinct_lines in (True, False):
        used_lines = {mutant.line for mutant in order}
        cursors = dict.fromkeys(operators, 0)
        progressed = True
        while progressed:
            progressed = False
            for name in operators:
                queue = queues[name]
                while cursors[name] < len(queue):
                    candidate = queue[cursors[name]]
                    cursors[name] += 1
                    if candidate.id in taken or (distinct_lines and candidate.line in used_lines):
                        continue
                    order.append(candidate)
                    taken.add(candidate.id)
                    used_lines.add(candidate.line)
                    progressed = True
                    break
    return order


# --------------------------------------------------------------------------- #
# Control scripts
# --------------------------------------------------------------------------- #


def _wrap_reference(solve: str) -> str:
    """Run the reference solution in a subshell so its ``exit``/``set -e`` stay scoped."""
    body = solve if solve.endswith("\n") else solve + "\n"
    return (
        f'(\n{body})\nenvcheck_rc=$?\nif [ "$envcheck_rc" -ne 0 ]; then exit "$envcheck_rc"; fi\n'
    )


def _header(title: str, expect: int) -> str:
    return f"#!/usr/bin/env bash\n# envcheck: {title}\n# expect: {expect}\n"


def capture_script(solve: str | None, targets: Sequence[str]) -> str:
    """Optionally run the reference solution, then copy each target out via /logs/artifacts."""
    lines = [_header("capture-oracle" if solve is not None else "capture-base", 1 if solve else 0)]
    if solve is not None:
        lines.append(_wrap_reference(solve))
    for target in targets:
        destination = f"{CAPTURE_ROOT}{target.rstrip('/')}"
        quoted, parent = (
            shlex.quote(target.rstrip("/")),
            shlex.quote(str(PurePosixPath(destination).parent)),
        )
        lines.append(
            f"if [ -e {quoted} ]; then mkdir -p {parent} && "
            f"cp -a {quoted} {shlex.quote(destination)}; fi\n"
        )
    return "".join(lines)


def _heredoc(path: str, content: bytes) -> str:
    text = content.decode("utf-8")
    tag = "ENVCHECK_" + hashlib.sha256(content).hexdigest()[:12].upper()
    if tag in text:
        raise ValueError(f"heredoc delimiter collides with the content of {path}")
    body = text if text.endswith("\n") else text + "\n"
    quoted = shlex.quote(path)
    parent = shlex.quote(str(PurePosixPath(path).parent))
    return f"mkdir -p {parent}\ncat > {quoted} <<'{tag}'\n{body}{tag}\n"


def mutant_script(solve: str, path: str, mutated: bytes, title: str) -> str:
    return _header(title, 0) + _wrap_reference(solve) + _heredoc(path, mutated)


def blank_script(solve: str, path: str) -> str:
    return _header(f"blank {path}", 0) + _wrap_reference(solve) + f": > {shlex.quote(path)}\n"


# --------------------------------------------------------------------------- #
# VerifierCheck single-submission grading (additive vcheck helper)
# --------------------------------------------------------------------------- #


def grade_submission(
    package: str | Path,
    submission: Submission,
    *,
    repo_root: str | Path,
    run_dir: str | Path,
    timeout_seconds: int = 1_800,
) -> GradeResult:
    """Grade one vcheck submission with the task's own verifier.

    Reuses the solution-override machinery: the reference ``solution/solve.sh``
    runs in a subshell, then the submission is applied -- a unified-diff
    ``patch`` via ``patch -p1``, or exact bytes written for ``file``/``json``
    (base64 when the bytes are not UTF-8). Empty content is a no-op, so the
    oracle and blank control submissions grade the reference solution and the
    untouched environment respectively. ``base="environment"`` skips the
    reference entirely.

    One ``oracle``-channel trial through ``run_matrix``: free local Docker, no
    model. ``status`` mirrors the trial outcome (``ok``/``mismatch`` scored,
    ``infra`` otherwise). Raises ``ValueError`` for non-task packages, missing
    reference solutions, relative target paths, non-UTF-8 patches, and invalid
    JSON payloads.
    """
    import base64

    from evallab.vcheck import GradeResult  # lazy: vcheck re-exports this helper

    def _write_bytes(path: str, data: bytes) -> str:
        try:
            return _heredoc(path, data)
        except (UnicodeDecodeError, ValueError):
            encoded = base64.encodebytes(data).decode("ascii")
            tag = "VCHECK_" + hashlib.sha256(data).hexdigest()[:12].upper()
            quoted = shlex.quote(path)
            parent = shlex.quote(str(PurePosixPath(path).parent))
            return f"mkdir -p {parent}\nbase64 -d <<'{tag}' > {quoted}\n{encoded}{tag}\n"

    root = Path(package).expanduser().resolve()
    config = _task_config(root)
    if config.get("steps"):
        raise ValueError("solution-override controls do not support declared task steps")
    solve_path = root / "solution" / "solve.sh"
    if not solve_path.is_file():
        raise ValueError(f"{root}: no reference solution (solution/solve.sh); nothing to grade")
    kind, target, content, base = (
        submission.kind,
        submission.path,
        submission.content,
        submission.base,
    )
    if kind not in ("patch", "file", "json"):
        raise ValueError(f"unknown submission kind: {kind!r}")
    if base not in ("oracle", "environment"):
        raise ValueError(f"unknown submission base: {base!r}")
    if target and not target.startswith("/"):
        raise ValueError("submission paths must be absolute container paths")
    if kind == "json" and content:
        try:
            json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"submission to {target or '<unknown>'} is not valid JSON") from exc
    script = _header(f"vcheck {kind} {target or 'no-op'}", 0)
    if base == "oracle":
        script += _wrap_reference(solve_path.read_text(encoding="utf-8"))
    if content:
        if kind == "patch":
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("patch submissions must be UTF-8 text") from exc
            script += _heredoc("/tmp/vcheck-submission.patch", text.encode("utf-8"))
            script += "patch -p1 --no-backup-if-mismatch -i /tmp/vcheck-submission.patch\n"
        else:
            script += _write_bytes(target, content)
    repo = Path(repo_root).resolve()
    work = Path(run_dir).expanduser().resolve()
    if not work.is_relative_to(repo):
        raise ValueError(f"run directory must stay inside the repository ({repo}); got {work}")
    scripts = work / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    label = hashlib.sha256(
        b"\0".join([kind.encode(), target.encode(), content, base.encode()])
    ).hexdigest()[:12]
    task_name = str((config.get("task") or {}).get("name") or root.name)
    slug = _slug(task_name.rsplit("/", 1)[-1], limit=28)
    script_path = scripts / f"grade-{label}.sh"
    script_path.write_text(script, encoding="utf-8")
    run_name = f"vc-{label}-{slug}"[:80].rstrip("-")
    digests = compute_task_digests(root)
    matrix = ExperimentMatrix(
        matrix_id=new_ulid(),
        name=f"vcheck-{slug}-grade",
        hypothesis=f"Grade one VerifierCheck submission for {slug} with the task's own verifier.",
        benchmark_family=slug,
        task_id=slug if len(slug) >= 3 else f"{slug}-task",
        task=str(root),
        task_package_digest=digests.package,
        verifier_digest=digests.verifier,
        environment="docker",
        concurrency=1,
        timeout_seconds=timeout_seconds,
        runs=[
            MatrixRun(
                name=run_name,
                agent="oracle",
                expect_reward=0.0,
                solution=script_path.relative_to(repo).as_posix(),
            )
        ],
    )
    (work / "matrices").mkdir(parents=True, exist_ok=True)
    (work / "matrices" / f"{matrix.name}.json").write_text(
        json.dumps(matrix.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    results = _default_runner(matrix, repo)
    result = next(
        (entry for entry in results if entry.get("name") == run_name),
        {"status": "infra", "error": "no matrix result"},
    )
    status = result.get("status")
    canonical = status if status in ("ok", "mismatch") else "infra"
    rewards = result.get("rewards") or []
    reward = float(rewards[0]) if canonical != "infra" and rewards else None
    if reward is None:
        canonical = "infra"
    job_dir = repo / "runs" / run_name
    return GradeResult(
        reward=reward,
        status=canonical,
        verifier_outputs={"rewards": rewards, "error": result.get("error")},
        job_dir=job_dir.relative_to(repo).as_posix() if job_dir.is_dir() else None,
        script=script_path.relative_to(repo).as_posix(),
    )


# --------------------------------------------------------------------------- #
# Plan and report contracts
# --------------------------------------------------------------------------- #


class MutationRun(ContractModel):
    name: str
    role: RunRole
    file: str | None = None
    mutant: str | None = None
    script: str
    script_sha256: str
    status: Literal["planned", "ok", "mismatch", "infra"] = "planned"
    reward: float | None = None
    outcome: Outcome | None = None
    job_dir: str | None = None
    error: str | None = None


class FileSummary(ContractModel):
    path: str
    language: Language | None
    captured: bool
    oracle_changed: bool
    candidates: int = 0
    selected: int = 0
    blank_reward: float | None = None
    graded: bool | None = None
    killed: int = 0
    survived: int = 0
    partial: int = 0
    unscored: int = 0
    note: str | None = None


class MutationReport(ContractModel):
    schema_: Literal["evallab.verifier_mutation.report/v1"] = Field(
        alias="schema", default=SCHEMA_REPORT
    )
    package: str
    package_digest: str
    task: str
    verifier_mode: str
    targets: list[str]
    run_dir: str
    executed: bool = False
    verdict: Verdict = "planned"
    verdict_reason: str = ""
    mutation_score: float | None = None
    files: list[FileSummary] = []
    mutants: list[Mutant] = []
    runs: list[MutationRun] = []
    notes: list[str] = []
    #: Mutant runs by outcome: total, killed, survived, partial, uninformative, unscored.
    counts: dict[str, int] = {}
    #: Ids of mutants that kept reward (survived or partial), in run order.
    survivors: list[str] = []
    limits: str


_LIMITS = (
    "a survivor is a reproducible input the task's own verifier scores like the reference "
    "solution; it is a hypothesis until a human or an objective check rules out a behaviour-"
    "equivalent mutant and confirms the instruction requires the changed behaviour. A killed "
    "mutant proves only that one edit is detected; no survivor is not a certificate. Mutants "
    "edit captured files after the reference solution ran, so outputs it already wrote from the "
    "unmutated code are not regenerated unless the verifier does that itself"
)


def _task_config(package: Path) -> dict[str, Any]:
    path = package / "task.toml"
    if not path.is_file():
        raise ValueError(f"{package}: not a Harbor task package (task.toml is missing)")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc


def declared_targets(config: dict[str, Any]) -> list[str]:
    """Absolute artifact paths the task declares; these are what a separate verifier reads."""
    targets: list[str] = []
    for entry in config.get("artifacts") or []:
        source = entry if isinstance(entry, str) else (entry or {}).get("source")
        if isinstance(source, str) and source.startswith("/"):
            targets.append(source)
    return targets


def _slug(text: str, *, limit: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit] or "task"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

MatrixRunner = Callable[[ExperimentMatrix, Path], list[dict[str, Any]]]


def _default_runner(matrix: ExperimentMatrix, repo_root: Path) -> list[dict[str, Any]]:
    return run_matrix(matrix, root=repo_root).results


@dataclass
class _Audit:
    package: Path
    repo_root: Path
    run_dir: Path
    slug: str
    nonce: str
    solve: str
    digests: Any
    timeout_seconds: int
    workers: int
    runner: MatrixRunner

    def stage(self, role: RunRole, label: str, script: str, **fields: Any) -> MutationRun:
        scripts = self.run_dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        path = scripts / f"{label}.sh"
        path.write_text(script, encoding="utf-8")
        name = f"ec-{self.nonce}-{_slug(label, limit=24)}-{self.slug}"[:80].rstrip("-")
        return MutationRun(
            name=name,
            role=role,
            script=path.relative_to(self.repo_root).as_posix(),
            script_sha256=_sha(script),
            **fields,
        )

    def matrix(self, runs: Sequence[MutationRun], part: str) -> ExperimentMatrix:
        return ExperimentMatrix(
            matrix_id=new_ulid(),
            name=f"envcheck-{self.slug}-{part}",
            hypothesis=(
                f"The verifier of {self.slug} rejects behavioural edits of the reference "
                "solution's output that the instruction does not allow."
            ),
            benchmark_family=self.slug,
            task_id=self.slug if len(self.slug) >= 3 else f"{self.slug}-task",
            task=str(self.package),
            task_package_digest=self.digests.package,
            verifier_digest=self.digests.verifier,
            environment="docker",
            concurrency=1,
            timeout_seconds=self.timeout_seconds,
            runs=[
                MatrixRun(
                    name=run.name,
                    agent="oracle",
                    expect_reward=1.0 if run.role == "capture-oracle" else 0.0,
                    solution=run.script,
                )
                for run in runs
            ],
        )

    def execute(self, runs: list[MutationRun], phase: str) -> None:
        """Run ``runs`` as up to ``workers`` matrices and record status, reward, job dir."""
        if not runs:
            return
        workers = max(1, min(self.workers, MAX_WORKERS, len(runs)))
        chunks = [runs[index::workers] for index in range(workers)]
        matrices = [self.matrix(chunk, f"{phase}-{index}") for index, chunk in enumerate(chunks)]
        matrices_dir = self.run_dir / "matrices"
        matrices_dir.mkdir(parents=True, exist_ok=True)
        for matrix in matrices:
            (matrices_dir / f"{matrix.name}.json").write_text(
                json.dumps(matrix.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
            )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(lambda matrix: self.runner(matrix, self.repo_root), matrices))
        by_name = {result["name"]: result for output in outputs for result in output}
        for run in runs:
            result = by_name.get(run.name, {"status": "infra", "error": "no matrix result"})
            status = result.get("status")
            run.status = status if status in {"ok", "mismatch"} else "infra"
            rewards = result.get("rewards") or []
            run.reward = float(rewards[0]) if run.status != "infra" and rewards else None
            if run.status != "infra" and run.reward is None:
                run.status = "infra"
            run.error = result.get("error")
            job_dir = self.repo_root / "runs" / run.name
            run.job_dir = (
                job_dir.relative_to(self.repo_root).as_posix() if job_dir.is_dir() else None
            )


def _captured_root(repo_root: Path, run: MutationRun) -> Path | None:
    if run.job_dir is None:
        return None
    for trial in sorted((repo_root / run.job_dir).iterdir()):
        candidate = trial.joinpath(*_CAPTURE_HOST)
        if trial.is_dir() and candidate.is_dir():
            return candidate
    return None


def _captured_files(root: Path | None, target: str) -> dict[str, bytes]:
    """Container path -> bytes for one target (a file, or a bounded walk of a directory)."""
    if root is None:
        return {}
    host = root / target.strip("/")
    if host.is_file():
        return {target.rstrip("/"): host.read_bytes()}
    if not host.is_dir():
        return {}
    files: dict[str, bytes] = {}
    for path in sorted(host.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or _SKIP_PARTS & set(relative.parts):
            continue
        if language_of(path.name) is None or path.stat().st_size > MAX_FILE_BYTES:
            continue
        files["/" + relative.as_posix()] = path.read_bytes()
        if len(files) >= MAX_FILES_PER_TARGET:
            break
    return files


def _classify_mutation_runs(report: MutationReport) -> None:
    summaries = {summary.path: summary for summary in report.files}
    for run in report.runs:
        if run.role != "blank" or run.file is None:
            continue
        summary = summaries[run.file]
        summary.blank_reward = run.reward
        if run.status == "infra" or run.reward is None:
            summary.graded = None
            run.outcome = "unscored"
        else:
            summary.graded = run.reward == 0
            run.outcome = "control"
    for run in report.runs:
        if run.role != "mutant" or run.file is None:
            continue
        summary = summaries[run.file]
        if run.status == "infra" or run.reward is None:
            run.outcome = "unscored"
            summary.unscored += 1
        elif run.reward <= 0:
            run.outcome = "killed"
            summary.killed += 1
        elif summary.graded is not True:
            run.outcome = "uninformative"
        elif run.reward >= 1:
            run.outcome = "survived"
            summary.survived += 1
        else:
            run.outcome = "partial"
            summary.partial += 1
    killed = sum(summary.killed for summary in report.files)
    escaped = sum(summary.survived + summary.partial for summary in report.files)
    report.mutation_score = killed / (killed + escaped) if killed + escaped else None
    mutant_runs = [run for run in report.runs if run.role == "mutant"]
    report.counts = {"total": len(mutant_runs)} | {
        outcome: sum(1 for run in mutant_runs if run.outcome == outcome)
        for outcome in ("killed", "survived", "partial", "uninformative", "unscored")
    }
    ungraded = [summary.path for summary in report.files if summary.graded is False]
    survivors = [run.mutant for run in mutant_runs if run.outcome in {"survived", "partial"}]
    report.survivors = [mutant for mutant in survivors if mutant is not None]
    if survivors:
        report.verdict = "survivors_found"
        report.verdict_reason = (
            f"{len(survivors)} mutant(s) earned reward from the task's own verifier while the "
            "same file's blank control earned 0"
        )
    elif killed:
        report.verdict = "no_survivors"
        report.verdict_reason = f"every scored mutant ({killed}) was rejected"
    elif not any(run.role == "mutant" for run in report.runs):
        report.verdict = "no_mutants"
        report.verdict_reason = "no graded file produced a mutant"
    else:
        report.verdict = "unscored"
        report.verdict_reason = "no mutant produced an informative scored trial"
    if ungraded:
        report.verdict_reason += "; blank control still rewarded for " + ", ".join(ungraded)


def run_mutation_audit(
    package: str | Path,
    *,
    repo_root: Path,
    targets: Sequence[str] = (),
    max_mutants: int = 24,
    seed: str = "",
    mutant_ids: Sequence[str] = (),
    execute: bool = False,
    workers: int = 1,
    timeout_seconds: int = 1_800,
    output_dir: Path | None = None,
    runner: MatrixRunner | None = None,
) -> tuple[MutationReport, Path]:
    """Capture the reference output, mutate it, and grade every mutant with the task's verifier."""
    root = Path(package).expanduser().resolve()
    config = _task_config(root)
    if config.get("steps"):
        raise ValueError("solution-override controls do not support declared task steps")
    solve_path = root / "solution" / "solve.sh"
    if not solve_path.is_file():
        raise ValueError(f"{root}: no reference solution (solution/solve.sh); nothing to mutate")
    chosen = list(targets) or declared_targets(config)
    if not chosen:
        raise ValueError(
            f"{root}: the task declares no artifacts; pass --target with the absolute path of "
            "each file or directory the verifier reads"
        )
    if any(not target.startswith("/") for target in chosen):
        raise ValueError("targets must be absolute container paths")
    repo = Path(repo_root).resolve()
    task_name = str((config.get("task") or {}).get("name") or root.name)
    slug = _slug(task_name.rsplit("/", 1)[-1], limit=28)
    nonce = new_ulid().lower()[-6:]
    run_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else repo / "runs" / ".envcheck" / f"{slug}-{nonce}"
    )
    if not run_dir.is_relative_to(repo):
        raise ValueError(f"run directory must stay inside the repository ({repo}); got {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    verifier_mode = str((config.get("verifier") or {}).get("environment_mode") or "shared")
    audit = _Audit(
        package=root,
        repo_root=repo,
        run_dir=run_dir,
        slug=slug,
        nonce=nonce,
        solve=solve_path.read_text(encoding="utf-8"),
        digests=compute_task_digests(root),
        timeout_seconds=timeout_seconds,
        workers=workers,
        runner=runner or _default_runner,
    )
    report = MutationReport(
        package=str(root),
        package_digest=audit.digests.package,
        task=task_name,
        verifier_mode=verifier_mode,
        targets=chosen,
        run_dir=str(run_dir),
        limits=_LIMITS,
    )
    oracle_capture = audit.stage(
        "capture-oracle", "capture-oracle", capture_script(audit.solve, chosen)
    )
    base_capture = audit.stage("capture-base", "capture-base", capture_script(None, chosen))
    report.runs = [oracle_capture, base_capture]
    if not execute:
        _write_report(run_dir, report)
        return report, run_dir

    report.executed = True
    audit.execute(report.runs, "capture")
    for run in report.runs:
        run.outcome = "unscored" if run.status == "infra" else "control"
    if oracle_capture.status == "infra" or base_capture.status == "infra":
        report.verdict = "unscored"
        report.verdict_reason = "a capture control did not produce a scored trial"
        _write_report(run_dir, report)
        return report, run_dir
    if (oracle_capture.reward or 0) < 1 or (base_capture.reward or 0) > 0:
        report.verdict = "task_broken"
        report.verdict_reason = (
            f"capture controls scored oracle={oracle_capture.reward:g} "
            f"base={base_capture.reward:g}; the verifier must give 1 and 0 before any "
            "mutant is informative"
        )
        _write_report(run_dir, report)
        return report, run_dir

    oracle_root = _captured_root(repo, oracle_capture)
    base_root = _captured_root(repo, base_capture)
    capture_dir = run_dir / "capture"
    generated: dict[str, list[tuple[Mutant, bytes]]] = {}
    for target in chosen:
        oracle_files = _captured_files(oracle_root, target)
        base_files = _captured_files(base_root, target)
        if not oracle_files:
            report.files.append(
                FileSummary(
                    path=target,
                    language=language_of(target),
                    captured=False,
                    oracle_changed=False,
                    note="absent after the reference solution, or no mutable files inside",
                )
            )
            continue
        for path, content in oracle_files.items():
            saved = capture_dir / path.lstrip("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(content)
            base = base_files.get(path)
            candidates = generate_mutants(path, content, base)
            summary = FileSummary(
                path=path,
                language=language_of(path),
                captured=True,
                oracle_changed=base != content,
                candidates=len(candidates),
            )
            if summary.language is None:
                summary.note = "not mutated: unsupported file type or package manifest"
            elif not candidates:
                summary.note = "no mutation operator applies"
            report.files.append(summary)
            if candidates:
                generated[path] = candidates
    if not generated:
        report.verdict = "no_mutants"
        report.verdict_reason = "no captured target produced a mutant"
        _write_report(run_dir, report)
        return report, run_dir

    # Blank controls first: a file the verifier does not grade makes its mutants uninformative.
    blanks = [
        audit.stage("blank", f"blank-{index:02d}", blank_script(audit.solve, path), file=path)
        for index, path in enumerate(sorted(generated))
    ]
    report.runs.extend(blanks)
    audit.execute(blanks, "blank")
    _classify_mutation_runs(report)
    graded = {summary.path for summary in report.files if summary.graded}
    pool = [mutant for path in sorted(graded) for mutant, _ in generated[path]]
    content_of = {mutant.id: content for path in graded for mutant, content in generated[path]}
    if mutant_ids:
        wanted = set(mutant_ids)
        selected = [mutant for mutant in pool if mutant.id in wanted][:max_mutants]
        missing = wanted - {mutant.id for mutant in selected}
        if missing:
            report.notes.append(
                "requested mutants not run (not generated, or their file's blank control did "
                "not score 0): " + ", ".join(sorted(missing))
            )
    else:
        selected = select_mutants(pool, limit=max_mutants, seed=seed)
    report.mutants = selected
    for summary in report.files:
        summary.selected = sum(1 for mutant in selected if mutant.file == summary.path)
    if not selected:
        _write_report(run_dir, report)
        return report, run_dir
    mutant_runs: list[MutationRun] = []
    inputs = run_dir / "inputs"
    for mutant in selected:
        title = f"mutant {mutant.id} {mutant.operator} {mutant.file}:{mutant.line}"
        mutant_runs.append(
            audit.stage(
                "mutant",
                f"m-{mutant.id}",
                mutant_script(audit.solve, mutant.file, content_of[mutant.id], title),
                file=mutant.file,
                mutant=mutant.id,
            )
        )
        folder = inputs / mutant.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{mutant.id}.patch").write_text(mutant.patch, encoding="utf-8")
    report.runs.extend(mutant_runs)
    audit.execute(mutant_runs, "mutate")
    _classify_mutation_runs(report)
    _write_report(run_dir, report)
    return report, run_dir


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #


def hypotheses(report: MutationReport) -> list[dict[str, Any]]:
    """One candidate record per escaped mutant, shaped like an EnvCheck finding case."""
    by_id = {mutant.id: mutant for mutant in report.mutants}
    controls = {run.role: run.reward for run in report.runs if run.role.startswith("capture")}
    blanks = {run.file: run.reward for run in report.runs if run.role == "blank"}
    records: list[dict[str, Any]] = []
    for run in report.runs:
        if run.outcome not in {"survived", "partial"} or run.mutant is None:
            continue
        mutant = by_id[run.mutant]
        records.append(
            {
                "format": SCHEMA_HYPOTHESIS,
                "id": f"{_slug(report.task.rsplit('/', 1)[-1])}-{mutant.id}",
                "status": "candidate",
                "task": report.task,
                "package": report.package,
                "package_digest": report.package_digest,
                "defect": {"family": "insufficient-checking", "class": None},
                "mutation": {
                    "operator": mutant.operator,
                    "file": mutant.file,
                    "line": mutant.line,
                    "original": mutant.original,
                    "replacement": mutant.replacement,
                    "oracle_changed_line": mutant.changed_line,
                },
                "claim": (
                    f"After the reference solution, changing {mutant.file}:{mutant.line} "
                    f"`{mutant.original}` -> `{mutant.replacement}` ({mutant.operator}) earns "
                    f"reward {run.reward:g} from the task's own verifier; the reference "
                    f"solution earns {controls.get('capture-oracle')}, the untouched "
                    f"environment {controls.get('capture-base')} and an emptied "
                    f"{mutant.file} {blanks.get(mutant.file)}."
                ),
                "case": {
                    "input": mutant.id,
                    "path": f"inputs/{mutant.id}/{mutant.id}.patch",
                    "base": "oracle",
                    "sha256": hashlib.sha256(mutant.patch.encode("utf-8")).hexdigest(),
                    "grader_verdict": {"status": "graded", "reward": run.reward},
                    "job": run.job_dir,
                    "script": run.script,
                },
                "intended_verdict": {
                    "verdict": "fail",
                    "basis": None,
                    "pending": (
                        "confirm the mutant is not behaviour-equivalent and that instruction.md "
                        "requires the changed behaviour"
                    ),
                },
                "adjudication": None,
            }
        )
    return records


def _write_report(run_dir: Path, report: MutationReport) -> None:
    (run_dir / "mutation-report.json").write_text(
        json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "mutation-report.md").write_text(render_report(report), encoding="utf-8")
    records = hypotheses(report)
    path = run_dir / "hypotheses.jsonl"
    if records:
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
    elif path.exists():
        path.unlink()


def _reward(value: float | None) -> str:
    return "—" if value is None else format(value, "g")


def render_report(report: MutationReport) -> str:
    score = "—" if report.mutation_score is None else f"{report.mutation_score:.0%}"
    lines = [
        f"# Verifier mutation audit: {report.task}",
        "",
        f"- package: `{report.package}`",
        f"- package digest: `{report.package_digest}`",
        f"- verifier mode: {report.verifier_mode}",
        f"- verdict: **{report.verdict}**",
        f"- reason: {report.verdict_reason or '—'}",
        f"- mutation score (killed / scored informative mutants): {score}",
        *(f"- note: {note}" for note in report.notes),
        "",
        "## Controls",
        "",
        "| run | role | file | reward | outcome |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {run.name} | {run.role} | {run.file or '—'} | {_reward(run.reward)} | "
        f"{run.outcome or run.status} |"
        for run in report.runs
        if run.role != "mutant"
    ]
    lines += [
        "",
        "## Files",
        "",
        "| file | graded | candidates | run | killed | survived | partial | unscored | note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{item.path}` | {'—' if item.graded is None else item.graded} | {item.candidates} | "
        f"{item.selected} | {item.killed} | {item.survived} | {item.partial} | {item.unscored} | "
        f"{item.note or ''} |"
        for item in report.files
    ]
    by_id = {mutant.id: mutant for mutant in report.mutants}
    escaped = [run for run in report.runs if run.outcome in {"survived", "partial"}]
    lines += ["", "## Survivors (candidates, not findings)", ""]
    if not escaped:
        lines.append("none")
    for run in escaped:
        mutant = by_id[run.mutant or ""]
        lines += [
            f"### `{mutant.id}` {mutant.operator} at `{mutant.file}:{mutant.line}` "
            f"(reward {_reward(run.reward)})",
            "",
            f"Job: `{run.job_dir}`. Input: `inputs/{mutant.id}/{mutant.id}.patch`.",
            "",
            "```diff",
            mutant.patch.rstrip("\n"),
            "```",
            "",
        ]
    killed = [run for run in report.runs if run.outcome == "killed"]
    if killed:
        lines += ["", "## Killed", ""]
        lines += [
            f"- `{run.mutant}` {by_id[run.mutant or ''].operator} at "
            f"`{run.file}:{by_id[run.mutant or ''].line}`"
            for run in killed
        ]
    lines += ["", f"Limits: {report.limits}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _mutate_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    output_dir = args.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = root / output_dir
    # With --json, stdout carries exactly one JSON document; matrix progress goes to stderr.
    progress = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
    try:
        with progress:
            report, run_dir = run_mutation_audit(
                args.package,
                repo_root=root,
                targets=args.target,
                max_mutants=args.max_mutants,
                seed=args.seed,
                mutant_ids=args.mutant,
                execute=args.execute,
                workers=args.workers,
                timeout_seconds=args.timeout_seconds,
                output_dir=output_dir,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"hack mutate: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True))
    else:
        print(f"Task: {report.task} ({report.verifier_mode} verifier)")
        print(f"Verdict: {report.verdict} ({report.verdict_reason or 'capture plan written'})")
        if report.mutation_score is not None:
            print(f"Mutation score: {report.mutation_score:.0%}")
        for item in report.files:
            print(
                f"  {item.path}: graded={item.graded} run={item.selected} killed={item.killed} "
                f"survived={item.survived} partial={item.partial}"
                + (f" ({item.note})" if item.note else "")
            )
    print(f"run directory: {run_dir}", file=sys.stderr if args.json else sys.stdout)
    return 0


def add_mutate_parser(hack_commands: Any) -> None:
    mutate = hack_commands.add_parser(
        "mutate",
        help=(
            "Verifier mutation testing: grade small behavioural edits of the reference "
            "solution's output with the task's own verifier (local, free)"
        ),
    )
    mutate.add_argument("package", type=Path)
    mutate.add_argument(
        "--target",
        action="append",
        default=[],
        help="Absolute container path to capture and mutate; repeatable "
        "(default: the task's declared artifacts)",
    )
    mutate.add_argument("--max-mutants", type=int, default=24)
    mutate.add_argument("--seed", default="", help="Changes which mutants are sampled")
    mutate.add_argument(
        "--mutant",
        action="append",
        default=[],
        help="Run exactly this mutant id from a previous report (repeatable); ids are stable "
        "for the same reference output",
    )
    mutate.add_argument("--workers", type=int, default=1, choices=range(1, MAX_WORKERS + 1))
    mutate.add_argument("--execute", action="store_true", help="Run the local control matrices")
    mutate.add_argument("--timeout-seconds", type=int, default=1_800)
    mutate.add_argument("--output-dir", type=Path)
    mutate.add_argument("--json", action="store_true")
    mutate.set_defaults(func=_mutate_command)
