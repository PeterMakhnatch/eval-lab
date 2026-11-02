"""STING-style reference-patch semantic mutation auditing.

A grader that accepts the reference patch proves little on its own: the hidden
tests may pin only the one behaviour its author happened to check. This module
takes the reference patch of a task, locates the Python lines it *adds*, and
derives small semantic mutants of that code (a dropped guard, a flipped
boundary, an omitted keyword argument, ...). Each mutant is then run through the
task's own grader with the reference patch applied first; a mutant that still
scores ``reward == 1.0`` is a container-executed proof that the grader does not
pin that behaviour ("surviving mutant", as in STING / SWE-ABS).

Deterministic by construction: no model is involved, candidate order is fixed,
and every mutant is checked to parse and to differ from the reference at the
AST level (never a syntax error, never a whitespace-only no-op). Mutants are
restricted to added lines of the reference patch so each one targets the
behaviour the task asks for, not unrelated repository code.

Execution happens through the Modal shortcut channel (``ModalBackend``), one
sandbox per cell: a probe cell applies the reference patch and dumps the
post-patch sources (also the reference control: the original grader must award
1.0), then one cell per mutant writes the mutated file over the patched tree.
Cells are consumed by their ``result.json`` marker and never rerun.
"""

from __future__ import annotations

import ast
import base64
import difflib
import functools
import hashlib
import json
import re
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: Fixed operator priority; the first mutants of a task come from distinct operators in this order.
MUTATOR_ORDER: tuple[str, ...] = (
    "boundary_flip",
    "equality_flip",
    "guard_drop",
    "handler_drop",
    "call_drop",
    "keyword_drop",
    "bool_flip",
    "int_shift",
)
DEFAULT_MUTANTS = 3
APPLIED_MARKER = "MUTANT_APPLIED"
PROBE_MARKER = "MUTANT_PROBE"
PROBE_DONE_MARKER = "MUTANT_PROBE_DONE"
#: Modal Sandbox rates (USD per CPU-second, per GiB-second), see docs/execution-tiers.md.
MODAL_CPU_USD_PER_S = 0.00003942
MODAL_GIB_USD_PER_S = 0.00000667
#: Shared estimate margin over measured sandbox seconds.
ESTIMATE_MARGIN = 1.25
#: Wall seconds reserved per cell before it starts (cap admission).
PROJECTED_CELL_SECONDS = 240

_COMPARE_FLIPS: dict[type[ast.cmpop], tuple[str, str, str]] = {
    ast.Lt: ("boundary_flip", "<", "<="),
    ast.LtE: ("boundary_flip", "<=", "<"),
    ast.Gt: ("boundary_flip", ">", ">="),
    ast.GtE: ("boundary_flip", ">=", ">"),
    ast.Eq: ("equality_flip", "==", "!="),
    ast.NotEq: ("equality_flip", "!=", "=="),
}
_COMPARE_TOKEN = re.compile(rb"<=|>=|==|!=|<|>")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- patch geometry


def added_lines(patch: str) -> dict[str, set[int]]:
    """Post-image line numbers added per file by a unified diff (``b/`` paths, deletions skipped)."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("diff --git ") or raw.startswith("--- "):
            in_hunk = False
            if raw.startswith("diff --git "):
                current = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].split("\t")[0].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
            in_hunk = False
            continue
        if raw.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if match is None or current is None:
                in_hunk = False
                continue
            new_line = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+"):
            out.setdefault(current, set()).add(new_line)
            new_line += 1
        elif raw.startswith(" ") or raw == "":
            new_line += 1
        # '-' lines and '\ No newline at end of file' do not advance the new image
    return out


def python_targets(patch: str) -> dict[str, set[int]]:
    """Non-test ``.py`` files the patch adds lines to, with their added post-image line numbers."""
    return {
        path: lines
        for path, lines in added_lines(patch).items()
        if path.endswith(".py") and lines and not _TEST_PATH.search(path)
    }


# --------------------------------------------------------------------------- mutants


@dataclass(frozen=True)
class Mutant:
    """One semantic mutant: the full mutated file plus its provenance."""

    path: str
    operator: str
    line: int
    description: str
    source: str
    diff: str
    sha256: str

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "source"}


@dataclass(frozen=True)
class _Candidate:
    operator: str
    line: int
    description: str
    lines: tuple[bytes, ...]


def _indent(line: bytes) -> bytes:
    return line[: len(line) - len(line.lstrip(b" \t"))]


def _replace_span(
    lines: list[bytes], start: int, end: int, replacement: bytes
) -> tuple[bytes, ...]:
    """Replace 1-based inclusive line span with one replacement line."""
    newline = b"\n" if lines[start - 1].endswith(b"\n") or end < len(lines) else b""
    return tuple(lines[: start - 1] + [replacement + newline] + lines[end:])


def _replace_cols(
    lines: list[bytes], lineno: int, start: int, end: int, replacement: bytes
) -> tuple[bytes, ...]:
    line = lines[lineno - 1]
    out = list(lines)
    out[lineno - 1] = line[:start] + replacement + line[end:]
    return tuple(out)


def _compare_candidates(node: ast.Compare, lines: list[bytes]) -> list[_Candidate]:
    if len(node.ops) != 1 or node.lineno != node.end_lineno:
        return []
    flip = _COMPARE_FLIPS.get(type(node.ops[0]))
    if flip is None:
        return []
    operator, before, after = flip
    line = lines[node.lineno - 1]
    left_end = node.left.end_col_offset
    right_start = node.comparators[0].col_offset
    if left_end is None or right_start is None:
        return []
    segment = line[left_end:right_start]
    match = _COMPARE_TOKEN.search(segment)
    if match is None or match.group(0) != before.encode():
        return []
    start = left_end + match.start()
    return [
        _Candidate(
            operator,
            node.lineno,
            f"{before} -> {after}",
            _replace_cols(lines, node.lineno, start, start + len(before), after.encode()),
        )
    ]


def _exits(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Return | ast.Raise | ast.Continue | ast.Break)


def _guard_candidates(node: ast.If, lines: list[bytes]) -> list[_Candidate]:
    if node.orelse or not _exits(node.body[-1]) or node.end_lineno is None:
        return []
    indent = _indent(lines[node.lineno - 1])
    kind = type(node.body[-1]).__name__.lower()
    return [
        _Candidate(
            "guard_drop",
            node.lineno,
            f"drop `if` guard ending in {kind}",
            _replace_span(lines, node.lineno, node.end_lineno, indent + b"pass"),
        )
    ]


def _handler_candidates(node: ast.ExceptHandler, lines: list[bytes]) -> list[_Candidate]:
    first, last = node.body[0], node.body[-1]
    if last.end_lineno is None:
        return []
    indent = _indent(lines[first.lineno - 1])
    return [
        _Candidate(
            "handler_drop",
            node.lineno,
            "except handler body -> raise (handler omitted)",
            _replace_span(lines, first.lineno, last.end_lineno, indent + b"raise"),
        )
    ]


def _call_candidates(node: ast.Expr, lines: list[bytes]) -> list[_Candidate]:
    if not isinstance(node.value, ast.Call) or node.end_lineno is None:
        return []
    indent = _indent(lines[node.lineno - 1])
    name = ast.unparse(node.value.func)
    return [
        _Candidate(
            "call_drop",
            node.lineno,
            f"drop call statement {name}(...)",
            _replace_span(lines, node.lineno, node.end_lineno, indent + b"pass"),
        )
    ]


def _keyword_candidates(node: ast.Call, lines: list[bytes], added: set[int]) -> list[_Candidate]:
    out = []
    func_end = node.func.end_col_offset or 0
    for keyword in node.keywords:
        if (
            keyword.arg is None
            or keyword.lineno not in added
            or keyword.lineno != keyword.end_lineno
            or keyword.col_offset is None
            or keyword.end_col_offset is None
        ):
            continue
        line = lines[keyword.lineno - 1]
        floor = func_end if keyword.lineno == node.func.end_lineno else 0
        comma = line.rfind(b",", floor, keyword.col_offset)
        if comma < 0:
            continue
        out.append(
            _Candidate(
                "keyword_drop",
                keyword.lineno,
                f"drop keyword argument {keyword.arg}=",
                _replace_cols(lines, keyword.lineno, comma, keyword.end_col_offset, b""),
            )
        )
    return out


def _constant_candidates(node: ast.Constant, lines: list[bytes]) -> list[_Candidate]:
    if node.lineno != node.end_lineno or node.col_offset is None or node.end_col_offset is None:
        return []
    text = lines[node.lineno - 1][node.col_offset : node.end_col_offset]
    if isinstance(node.value, bool):
        flipped = b"False" if node.value else b"True"
        if text not in (b"True", b"False"):
            return []
        return [
            _Candidate(
                "bool_flip",
                node.lineno,
                f"{text.decode()} -> {flipped.decode()}",
                _replace_cols(lines, node.lineno, node.col_offset, node.end_col_offset, flipped),
            )
        ]
    if isinstance(node.value, int) and re.fullmatch(rb"\d+", text):
        shifted = str(node.value + 1).encode()
        return [
            _Candidate(
                "int_shift",
                node.lineno,
                f"{text.decode()} -> {shifted.decode()}",
                _replace_cols(lines, node.lineno, node.col_offset, node.end_col_offset, shifted),
            )
        ]
    return []


def candidates(source: str, added: set[int]) -> list[_Candidate]:
    """Every operator application whose anchor line is an added line, in source order."""
    tree = ast.parse(source)
    lines = source.encode().splitlines(keepends=True)
    found: list[_Candidate] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if isinstance(node, ast.Call):
            found.extend(_keyword_candidates(node, lines, added))
        if lineno is None or lineno not in added:
            continue
        if isinstance(node, ast.Compare):
            found.extend(_compare_candidates(node, lines))
        elif isinstance(node, ast.If):
            found.extend(_guard_candidates(node, lines))
        elif isinstance(node, ast.ExceptHandler):
            found.extend(_handler_candidates(node, lines))
        elif isinstance(node, ast.Expr):
            found.extend(_call_candidates(node, lines))
        elif isinstance(node, ast.Constant):
            found.extend(_constant_candidates(node, lines))
    found.sort(key=lambda c: (MUTATOR_ORDER.index(c.operator), c.line))
    return found


def _valid(candidate: _Candidate, reference_dump: str) -> str | None:
    """Mutated source when it parses and differs from the reference AST, else None."""
    try:
        text = b"".join(candidate.lines).decode()
        tree = ast.parse(text)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None
    return text if ast.dump(tree) != reference_dump else None


def generate_mutants(
    path: str, source: str, added: set[int], *, limit: int = DEFAULT_MUTANTS
) -> list[Mutant]:
    """Up to ``limit`` valid mutants of ``source`` anchored on ``added`` lines.

    Selection prefers distinct operators (in :data:`MUTATOR_ORDER`), then
    distinct lines; every mutant parses and differs from the reference AST.
    """
    reference_dump = ast.dump(ast.parse(source))
    valid: list[tuple[_Candidate, str]] = []
    seen: set[str] = set()
    for candidate in candidates(source, added):
        text = _valid(candidate, reference_dump)
        if text is None or text in seen:
            continue
        seen.add(text)
        valid.append((candidate, text))
    chosen: list[tuple[_Candidate, str]] = []
    for distinct_operator, distinct_line in ((True, True), (False, True), (False, False)):
        for item in valid:
            if len(chosen) >= limit:
                break
            if item in chosen:
                continue
            if distinct_operator and any(c.operator == item[0].operator for c, _ in chosen):
                continue
            if distinct_line and any(c.line == item[0].line for c, _ in chosen):
                continue
            chosen.append(item)
    return _build_mutants(path, source, chosen)


def _build_mutants(
    path: str, source: str, chosen: list[tuple[_Candidate, str]]
) -> list[Mutant]:
    original_lines = source.splitlines(keepends=True)
    return [
        Mutant(
            path=path,
            operator=c.operator,
            line=c.line,
            description=c.description,
            source=text,
            diff="".join(
                difflib.unified_diff(
                    original_lines,
                    text.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    n=2,
                )
            ),
            sha256=sha256_text(text),
        )
        for c, text in chosen
    ]


def mutants_for_patch(
    patch: str, sources: Mapping[str, str], *, limit: int = DEFAULT_MUTANTS
) -> list[Mutant]:
    """Spread ``limit`` mutants over the patch's Python targets (round-robin by file order)."""
    per_file = [
        generate_mutants(path, sources[path], lines, limit=limit)
        for path, lines in sorted(python_targets(patch).items())
        if path in sources
    ]
    chosen: list[Mutant] = []
    for index in range(limit):
        for mutants in per_file:
            if index < len(mutants):
                chosen.append(mutants[index])
    return chosen[:limit]


#: Operators outside :data:`MUTATOR_ORDER`: used only to widen the pool of *fresh* mutants
#: (certifier probe/eval sets), never by :func:`generate_mutants`, so existing selections do not move.
EXTRA_MUTATOR_ORDER: tuple[str, ...] = (
    "arith_flip",
    "bool_op_flip",
    "negate_condition",
    "return_none",
)
_ARITH_FLIPS: dict[type[ast.operator], tuple[bytes, bytes]] = {
    ast.Add: (b"+", b"-"),
    ast.Sub: (b"-", b"+"),
}
_BOOL_FLIPS: dict[type[ast.boolop], tuple[bytes, bytes]] = {
    ast.And: (b"and", b"or"),
    ast.Or: (b"or", b"and"),
}


def _is_text_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)
    )


def _single_line(node: ast.AST) -> bool:
    return (
        getattr(node, "lineno", None) is not None
        and node.lineno == getattr(node, "end_lineno", None)
        and getattr(node, "col_offset", None) is not None
        and getattr(node, "end_col_offset", None) is not None
    )


def _binop_candidates(node: ast.BinOp, lines: list[bytes]) -> list[_Candidate]:
    flip = _ARITH_FLIPS.get(type(node.op))
    if flip is None or not _single_line(node) or _is_text_constant(node.left):
        return []
    if _is_text_constant(node.right):
        return []
    before, after = flip
    left_end, right_start = node.left.end_col_offset, node.right.col_offset
    if left_end is None or right_start is None:
        return []
    segment = lines[node.lineno - 1][left_end:right_start]
    at = segment.find(before)
    if at < 0:
        return []
    start = left_end + at
    return [
        _Candidate(
            "arith_flip",
            node.lineno,
            f"{before.decode()} -> {after.decode()}",
            _replace_cols(lines, node.lineno, start, start + len(before), after),
        )
    ]


def _boolop_candidates(node: ast.BoolOp, lines: list[bytes]) -> list[_Candidate]:
    flip = _BOOL_FLIPS.get(type(node.op))
    if flip is None or not _single_line(node):
        return []
    before, after = flip
    first_end, second_start = node.values[0].end_col_offset, node.values[1].col_offset
    if first_end is None or second_start is None:
        return []
    segment = lines[node.lineno - 1][first_end:second_start]
    match = re.search(rb"\b" + before + rb"\b", segment)
    if match is None:
        return []
    start = first_end + match.start()
    return [
        _Candidate(
            "bool_op_flip",
            node.lineno,
            f"{before.decode()} -> {after.decode()}",
            _replace_cols(lines, node.lineno, start, start + len(before), after),
        )
    ]


def _negate_candidates(node: ast.If, lines: list[bytes]) -> list[_Candidate]:
    test = node.test
    if not _single_line(test) or test.lineno != node.lineno:
        return []
    text = lines[test.lineno - 1][test.col_offset : test.end_col_offset]
    return [
        _Candidate(
            "negate_condition",
            node.lineno,
            "negate `if` condition",
            _replace_cols(
                lines, test.lineno, test.col_offset, test.end_col_offset, b"not (" + text + b")"
            ),
        )
    ]


def _return_candidates(node: ast.Return, lines: list[bytes]) -> list[_Candidate]:
    value = node.value
    if value is None or not _single_line(node) or not _single_line(value):
        return []
    if isinstance(value, ast.Constant) and value.value is None:
        return []
    return [
        _Candidate(
            "return_none",
            node.lineno,
            "return value -> None",
            _replace_cols(lines, node.lineno, value.col_offset, value.end_col_offset, b"None"),
        )
    ]


def extra_candidates(source: str, added: set[int]) -> list[_Candidate]:
    """Operator applications of :data:`EXTRA_MUTATOR_ORDER` anchored on added lines, in operator then line order."""
    tree = ast.parse(source)
    lines = source.encode().splitlines(keepends=True)
    found: list[_Candidate] = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) not in added:
            continue
        if isinstance(node, ast.BinOp):
            found.extend(_binop_candidates(node, lines))
        elif isinstance(node, ast.BoolOp):
            found.extend(_boolop_candidates(node, lines))
        elif isinstance(node, ast.If):
            found.extend(_negate_candidates(node, lines))
        elif isinstance(node, ast.Return):
            found.extend(_return_candidates(node, lines))
    found.sort(key=lambda c: (EXTRA_MUTATOR_ORDER.index(c.operator), c.line))
    return found


def fresh_mutants(
    path: str,
    source: str,
    added: set[int],
    *,
    exclude_sha256: Iterable[str] = (),
    extra_operators: bool = True,
) -> list[Mutant]:
    """Every valid mutant of ``source`` on ``added`` lines whose source hash is not in ``exclude_sha256``.

    Unlike :func:`generate_mutants` nothing is selected or capped: the caller orders and slices the pool
    (the gold certifier draws probe and extra held-out mutants from it). Order is generation order
    (standard operators, then :data:`EXTRA_MUTATOR_ORDER` ones when ``extra_operators``).
    """
    excluded = set(exclude_sha256)
    reference_dump = ast.dump(ast.parse(source))
    found = candidates(source, added)
    if extra_operators:
        found = found + extra_candidates(source, added)
    seen: set[str] = set()
    chosen: list[tuple[_Candidate, str]] = []
    for candidate in found:
        text = _valid(candidate, reference_dump)
        if text is None or text in seen:
            continue
        seen.add(text)
        if sha256_text(text) in excluded:
            continue
        chosen.append((candidate, text))
    return _build_mutants(path, source, chosen)


# --------------------------------------------------------------------------- container scripts


def _write(path: str, body: str) -> str:
    encoded = base64.b64encode(body.encode()).decode()
    return f"echo {encoded} | base64 -d > {path}\n"


def _apply_reference(reference_patch: str) -> str:
    return _write("/tmp/mutant-reference.patch", reference_patch) + (
        "git apply --whitespace=nowarn /tmp/mutant-reference.patch"
        " || patch -p1 < /tmp/mutant-reference.patch || exit 1\n"
    )


def probe_script(*, workdir: str, reference_patch: str, targets: Iterable[str]) -> str:
    """Apply the reference patch, then dump each target's post-patch bytes as one marker line."""
    body = "".join(
        f"test -f {t} || exit 1\necho {PROBE_MARKER} {t} $(base64 < {t} | tr -d '\\n')\n"
        for t in targets
    )
    return (
        f"set -u\ncd {workdir} || exit 1\n"
        + _apply_reference(reference_patch)
        + body
        + (f"echo {PROBE_DONE_MARKER}\n")
    )


def mutant_script(*, workdir: str, reference_patch: str, mutant: Mutant) -> str:
    """Apply the reference patch, overwrite one file with the mutant, prove the bytes landed."""
    return (
        f"set -u\ncd {workdir} || exit 1\n"
        + _apply_reference(reference_patch)
        + _write(mutant.path, mutant.source)
        + f'test "$(sha256sum {mutant.path} | cut -d" " -f1)" = {mutant.sha256} || exit 1\n'
        + f"echo {APPLIED_MARKER} {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}\n"
    )


def parse_probe(agent_log: str) -> dict[str, str]:
    """Post-patch sources dumped by :func:`probe_script`; empty when the probe never finished."""
    if PROBE_DONE_MARKER not in agent_log:
        return {}
    sources: dict[str, str] = {}
    for line in agent_log.splitlines():
        if not line.startswith(PROBE_MARKER + " "):
            continue
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        try:
            sources[parts[1]] = base64.b64decode(parts[2], validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            continue
    return sources


def mutant_applied(agent_log: str, mutant: Mutant) -> bool:
    return f"{APPLIED_MARKER} {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}" in (
        agent_log
    )


# --------------------------------------------------------------------------- execution


class SandboxMeter:
    """Cost estimate from measured sandbox seconds with the shared margin; the cap is enforced on it."""

    def __init__(self, cap_usd: float):
        self.cap, self.spent, self.cells, self.seconds = cap_usd, 0.0, 0, 0.0
        self._lock = threading.Lock()

    @staticmethod
    def estimate(package: Path, wall_seconds: float) -> float:
        env = tomllib.loads((package / "task.toml").read_text()).get("environment", {})
        rate = (
            int(env.get("cpus", 2)) * MODAL_CPU_USD_PER_S
            + (int(env.get("memory_mb", 8192)) / 1024) * MODAL_GIB_USD_PER_S
        )
        return rate * wall_seconds * ESTIMATE_MARGIN

    def admit(self, projected_usd: float) -> None:
        with self._lock:
            if self.spent + projected_usd > self.cap:
                raise RuntimeError(
                    f"Modal cap ${self.cap:.2f} would be passed (spent ${self.spent:.3f})"
                )

    def add(self, usd: float, wall_seconds: float) -> None:
        with self._lock:
            self.spent += usd
            self.seconds += wall_seconds
            self.cells += 1


CellRunner = Callable[[Path, Path, str], dict[str, Any]]
"""``(cell_dir, package, script) -> result`` with at least ``reward`` and ``agent_log``."""


def modal_cell_runner(
    *, repo_root: Path, spend_gate: Any, meter: SandboxMeter, timeout_seconds: int = 1800
) -> CellRunner:
    """A :data:`CellRunner` over ``ModalBackend``; ``result.json`` is the consumption marker."""

    def run(cell_dir: Path, package: Path, script: str) -> dict[str, Any]:
        result_path = cell_dir / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text())
        from evallab.qa.task_modal import ModalBackend
        from evallab.registry import task_directory_digest

        meter.admit(meter.estimate(package, PROJECTED_CELL_SECONDS))
        cell_dir.mkdir(parents=True, exist_ok=True)
        backend = ModalBackend(repo_root=repo_root, spend_gate=spend_gate)
        started = time.monotonic()
        observation = backend.run_container_trial(
            package,
            digest=task_directory_digest(package),
            mode="shortcut",
            script=script,
            trial_dir=cell_dir / "trial",
            timeout_seconds=timeout_seconds,
        )
        wall = time.monotonic() - started
        trial = cell_dir / "trial"
        agent_log = _read_text(trial / "agent/oracle.txt")
        test_output = _read_text(trial / "verifier/test_output.log")
        cost = meter.estimate(package, wall)
        meter.add(cost, wall)
        result = {
            "status": observation.status,
            "reason": observation.reason,
            "reward": observation.facts.get("reward"),
            "wall_s": round(wall, 1),
            "est_usd": round(cost, 5),
            "agent_log": agent_log,
            "test_output_tail": test_output[-600:],
            "evidence_sha256": sha256_text(agent_log + test_output),
        }
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        return result

    return run


def _read_text(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _workdir(package: Path) -> str:
    return tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]


def _cell_row(label: str, mutant: Mutant | None, result: Mapping[str, Any]) -> dict[str, Any]:
    applied = (
        PROBE_DONE_MARKER in result.get("agent_log", "")
        if mutant is None
        else mutant_applied(result.get("agent_log", ""), mutant)
    )
    return {
        "cell": label,
        "mutant": None if mutant is None else mutant.summary(),
        "applied": applied,
        "reward": result.get("reward"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "wall_s": result.get("wall_s"),
        "est_usd": result.get("est_usd"),
        "evidence_sha256": result.get("evidence_sha256"),
    }


def audit_task(
    *,
    task_id: str,
    package: Path,
    reference_patch: str,
    out_dir: Path,
    run_cell: CellRunner,
    repaired_package: Path | None = None,
    limit: int = DEFAULT_MUTANTS,
    submit: Callable[[Callable[[], dict[str, Any]]], Future[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Probe, derive mutants, run them against the original (and optionally repaired) grader.

    Returns the audit record also written to ``out_dir/audit.json``. A survivor
    counts only when the mutant's applied marker is present and the reference
    control scored 1.0 on that grader (the repaired control is the qualification
    receipt's 3/3 reference passes, not re-run here). ``submit`` may run cells
    concurrently; it must return an object with ``result()``.
    """
    targets = sorted(python_targets(reference_patch))
    record: dict[str, Any] = {
        "task_id": task_id,
        "package": str(package),
        "repaired_package": None if repaired_package is None else str(repaired_package),
        "patch_sha256": sha256_text(reference_patch),
        "python_targets": targets,
        "probe": None,
        "mutants": [],
        "original": [],
        "repaired": [],
    }
    if not targets:
        record["outcome"] = "no python target in reference patch"
        return _finish(out_dir, record)
    workdir = _workdir(package)
    probe = run_cell(
        out_dir / "probe",
        package,
        probe_script(workdir=workdir, reference_patch=reference_patch, targets=targets),
    )
    record["probe"] = _cell_row("probe", None, probe)
    sources = parse_probe(probe.get("agent_log", ""))
    mutants = mutants_for_patch(reference_patch, sources, limit=limit)
    record["mutants"] = [m.summary() for m in mutants]
    for mutant in mutants:
        (out_dir / f"{mutant.operator}-L{mutant.line}.diff").parent.mkdir(
            parents=True, exist_ok=True
        )
        (out_dir / f"{mutant.operator}-L{mutant.line}.diff").write_text(mutant.diff)
    if not mutants:
        record["outcome"] = (
            "probe failed" if not sources else "no valid mutant on the added python lines"
        )
        return _finish(out_dir, record)
    graders = [("original", package)]
    if repaired_package is not None:
        graders.append(("repaired", repaired_package))
    pending: list[tuple[str, str, Mutant, Future[dict[str, Any]]]] = []
    for grader, grader_package in graders:
        for index, mutant in enumerate(mutants):
            label = f"{grader}-m{index}-{mutant.operator}-L{mutant.line}"
            script = mutant_script(
                workdir=_workdir(grader_package), reference_patch=reference_patch, mutant=mutant
            )
            job = functools.partial(run_cell, out_dir / label, grader_package, script)
            future: Future[dict[str, Any]]
            if submit is None:
                future = Future()
                future.set_result(job())
            else:
                future = submit(job)
            pending.append((grader, label, mutant, future))
    for grader, label, mutant, future in pending:
        record[grader].append(_cell_row(label, mutant, future.result()))
    control_ok = record["probe"]["applied"] and record["probe"]["reward"] == 1.0
    record["control_original_ok"] = control_ok
    record["survivors_original"] = [
        r["cell"] for r in record["original"] if r["applied"] and r["reward"] == 1.0
    ]
    record["survivors_repaired"] = [
        r["cell"] for r in record["repaired"] if r["applied"] and r["reward"] == 1.0
    ]
    record["weak_original"] = control_ok and bool(record["survivors_original"])
    record["weak_repaired"] = (
        None if repaired_package is None else bool(record["survivors_repaired"])
    )
    record["outcome"] = "audited"
    return _finish(out_dir, record)


def _finish(out_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "audit.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


# --------------------------------------------------------------------------- CLI


def build_audit_mutants_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "audit-mutants",
        help="STING-style reference-patch semantic mutants through a task's grader (Modal, capped)",
    )
    parser.add_argument("--package", type=Path, required=True, help="Original task package")
    parser.add_argument("--reference-patch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Audit output directory")
    parser.add_argument("--repaired-package", type=Path, help="Repaired grader snapshot")
    parser.add_argument("--limit", type=int, default=DEFAULT_MUTANTS)
    parser.add_argument(
        "--plan", action="store_true", help="Offline: list the Python mutation targets and exit"
    )
    parser.add_argument("--approval", help="Recorded approval text for the Modal spend gate")
    parser.add_argument("--max-usd", type=float, default=1.75)
    parser.add_argument("--per-execution-usd", type=float, default=0.005)
    parser.add_argument("--ledger", type=Path, help="Persisted spend ledger (outside evidence)")
    parser.add_argument("--app-name", default="mutant-auditor")
    parser.set_defaults(func=_audit_mutants_command)


def _audit_mutants_command(args: Any, root: Path, *, harbor: Any = None) -> int:
    patch = args.reference_patch.read_text()
    targets = python_targets(patch)
    if args.plan:
        print(
            json.dumps(
                {
                    "package": str(args.package),
                    "python_targets": {p: sorted(n) for p, n in sorted(targets.items())},
                },
                indent=2,
            )
        )
        return 0
    if not args.approval:
        raise ValueError("Modal execution requires --approval (recorded approval text)")
    from evallab.regrade import CloudSpendGate

    gate = CloudSpendGate(
        approval=args.approval,
        cap_usd=args.max_usd,
        usd_per_execution=args.per_execution_usd,
        spend_ledger=args.ledger,
        modal_app_name=args.app_name,
        billing_tag=args.app_name,
    )
    meter = SandboxMeter(args.max_usd)
    record = audit_task(
        task_id=args.package.name,
        package=args.package,
        reference_patch=patch,
        out_dir=args.out,
        run_cell=modal_cell_runner(repo_root=root, spend_gate=gate, meter=meter),
        repaired_package=args.repaired_package,
        limit=args.limit,
    )
    print(
        json.dumps(
            {
                "task_id": record["task_id"],
                "outcome": record["outcome"],
                "weak_original": record.get("weak_original"),
                "weak_repaired": record.get("weak_repaired"),
                "estimated_usd": round(meter.spent, 4),
                "executions": meter.cells,
            },
            indent=2,
        )
    )
    return 0
