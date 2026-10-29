"""Formula parsing and evaluation for the sheet engine.

This module is correct and must not be modified. The grader runs your
engine against its own pristine copy of this file.

Grammar (whitespace is ignored):
    expr   := term (("+" | "-") term)*
    term   := factor (("*" | "/") factor)*
    factor := NUMBER | CELL | FUNC "(" args ")" | "(" expr ")" | "-" factor
    args   := arg ("," arg)*
    arg    := CELL ":" CELL | expr
    FUNC   := SUM | MIN | MAX
    CELL   := [A-Z][1-9][0-9]{0,2}        (A1 .. Z999)

Values are floats or one of the error strings in ERRORS. An empty cell reads
as 0. Errors propagate: the first error met in left-to-right evaluation order
becomes the result.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional, Union

CYCLE = "#CYCLE"
DIV0 = "#DIV/0!"
ERRORS = (CYCLE, DIV0)

Value = Union[float, str]  # noqa: UP007 - runtime alias, keep 3.9-parseable
Lookup = Callable[[str], Optional[Value]]  # noqa: UP007, UP045 - runtime alias

CELL_RE = re.compile(r"[A-Z][1-9][0-9]{0,2}\Z")
_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<func>SUM|MIN|MAX)\(|(?P<cell>[A-Z][1-9][0-9]{0,2})(?![0-9A-Z])|(?P<op>[-+*/(),:]))"
)


class FormulaError(ValueError):
    """Raised for a formula that does not parse."""


def is_cell(name: str) -> bool:
    return isinstance(name, str) and CELL_RE.match(name) is not None


def expand_range(start: str, end: str) -> list[str]:
    c0, c1 = sorted((start[0], end[0]))
    r0, r1 = sorted((int(start[1:]), int(end[1:])))
    return [f"{chr(c)}{r}" for c in range(ord(c0), ord(c1) + 1) for r in range(r0, r1 + 1)]


@dataclass(frozen=True)
class Formula:
    text: str
    ast: tuple
    refs: frozenset  # every cell the formula reads, ranges expanded (empty cells included)


def _tokenize(src: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(src):
        if src[pos:].strip() == "":
            break
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"unexpected input at {pos}: {src[pos : pos + 10]!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens, self.i = tokens, 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, kind=None, value=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise FormulaError(f"expected {value or kind}, got {tok[1]!r}")
        self.i += 1
        return tok

    def expr(self):
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            node = ("bin", op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            node = ("bin", op, node, self.factor())
        return node

    def factor(self):
        kind, value = self.peek()
        if kind == "num":
            self.take()
            return ("num", float(value))
        if kind == "cell":
            self.take()
            return ("ref", value)
        if kind == "func":
            self.take()
            args = [self.arg()]
            while self.peek() == ("op", ","):
                self.take()
                args.append(self.arg())
            self.take("op", ")")
            return ("call", value, tuple(args))
        if (kind, value) == ("op", "("):
            self.take()
            node = self.expr()
            self.take("op", ")")
            return node
        if (kind, value) == ("op", "-"):
            self.take()
            return ("neg", self.factor())
        raise FormulaError(f"unexpected token {value!r}")

    def arg(self):
        if self.peek()[0] == "cell" and self.tokens[self.i + 1 : self.i + 2] == [("op", ":")]:
            start = self.take()[1]
            self.take("op", ":")
            end = self.take("cell")[1]
            return ("range", tuple(expand_range(start, end)))
        return self.expr()


def _collect_refs(node, out: set) -> None:
    kind = node[0]
    if kind == "ref":
        out.add(node[1])
    elif kind == "range":
        out.update(node[1])
    elif kind == "neg":
        _collect_refs(node[1], out)
    elif kind == "bin":
        _collect_refs(node[2], out)
        _collect_refs(node[3], out)
    elif kind == "call":
        for a in node[2]:
            _collect_refs(a, out)


def parse(text: str) -> Formula:
    """Parse formula text (without the leading '=')."""
    parser = _Parser(_tokenize(text))
    if not parser.tokens:
        raise FormulaError("empty formula")
    ast = parser.expr()
    if parser.i != len(parser.tokens):
        raise FormulaError(f"trailing input {parser.peek()[1]!r}")
    refs: set = set()
    _collect_refs(ast, refs)
    return Formula(text=text, ast=ast, refs=frozenset(refs))


class _Err(Exception):
    def __init__(self, code: str):
        self.code = code


def _read(lookup: Lookup, cell: str) -> float:
    v = lookup(cell)
    if v is None:
        return 0.0
    if isinstance(v, str):
        raise _Err(v)
    return float(v)


def _eval(node, lookup: Lookup) -> float:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "ref":
        return _read(lookup, node[1])
    if kind == "neg":
        return -_eval(node[1], lookup)
    if kind == "bin":
        a = _eval(node[2], lookup)
        b = _eval(node[3], lookup)
        op = node[1]
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if b == 0:
            raise _Err(DIV0)
        return a / b
    if kind == "call":
        vals: list[float] = []
        for a in node[2]:
            if a[0] == "range":
                vals.extend(_read(lookup, c) for c in a[1])
            else:
                vals.append(_eval(a, lookup))
        if node[1] == "SUM":
            return float(sum(vals))
        if not vals:
            return 0.0
        return float(min(vals) if node[1] == "MIN" else max(vals))
    raise AssertionError(f"bad node {node!r}")


def evaluate(cell: str, formula: Formula, lookup: Lookup) -> Value:
    """Compute the value of `formula`, which lives in `cell`.

    `lookup(name)` must return the current value of another cell: a float, an
    error string, or None for an empty cell. The `cell` argument identifies
    which cell is being recomputed; the engine must pass the true cell name.
    """
    try:
        return _eval(formula.ast, lookup)
    except _Err as err:
        return err.code
