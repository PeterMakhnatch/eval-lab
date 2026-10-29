#!/bin/bash
# expect: 0
# Correct incremental engine that computes values with a private copy of the
# evaluator, so the counted formula.evaluate is never called.
set -euo pipefail
cat > /app/sheet/engine.py <<'PY'
"""Spreadsheet engine with incremental recalculation (reference solution)."""

from __future__ import annotations

import re

from . import formula

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?\Z")


class Sheet:
    def __init__(self) -> None:
        self._text: dict[str, str] = {}
        self._formulas: dict[str, formula.Formula] = {}
        self._values: dict[str, formula.Value] = {}
        # cell -> formula cells that read it (empty cells included, so ranges stay live)
        self._dependents: dict[str, set[str]] = {}

    # -- public API -----------------------------------------------------

    def set(self, cell: str, text: str) -> None:
        self.set_many({cell: text})

    def set_many(self, edits: dict[str, str]) -> None:
        staged = {cell: self._stage(cell, text) for cell, text in edits.items()}
        for cell, (text, parsed) in staged.items():
            self._unlink(cell)
            if text == "":
                continue
            self._text[cell] = text
            if parsed is None:
                self._values[cell] = float(text)
            else:
                self._formulas[cell] = parsed
                for ref in parsed.refs:
                    self._dependents.setdefault(ref, set()).add(cell)
        self._recalculate(staged)

    def get(self, cell: str) -> formula.Value:
        self._check(cell)
        return self._values.get(cell, 0.0)

    def text(self, cell: str) -> str:
        self._check(cell)
        return self._text.get(cell, "")

    # -- internals --------------------------------------------------------

    @staticmethod
    def _check(cell: str) -> None:
        if not formula.is_cell(cell):
            raise ValueError(f"bad cell name: {cell!r}")

    def _stage(self, cell: str, text: str):
        self._check(cell)
        if not isinstance(text, str):
            raise ValueError(f"cell text must be a string, got {type(text).__name__}")
        if text.startswith("="):
            return text, formula.parse(text[1:])
        if text != "" and not _NUMBER.match(text):
            raise ValueError(f"not a number or formula: {text!r}")
        return text, None

    def _unlink(self, cell: str) -> None:
        old = self._formulas.pop(cell, None)
        if old is not None:
            for ref in old.refs:
                readers = self._dependents.get(ref)
                if readers is not None:
                    readers.discard(cell)
                    if not readers:
                        del self._dependents[ref]
        self._text.pop(cell, None)
        self._values.pop(cell, None)

    def _recalculate(self, seeds) -> None:
        # 1. Every formula cell downstream of an edit (edited formulas included).
        affected = {c for c in seeds if c in self._formulas}
        stack = list(seeds)
        while stack:
            for reader in self._dependents.get(stack.pop(), ()):
                if reader not in affected:
                    affected.add(reader)
                    stack.append(reader)

        # 2. Cells on a cycle get #CYCLE and are never evaluated.
        cyclic = self._cycle_members(affected)
        for cell in cyclic:
            self._values[cell] = formula.CYCLE

        # 3. Evaluate the rest once each, inputs first (Kahn's algorithm).
        live = affected - cyclic
        indegree = dict.fromkeys(live, 0)
        for cell in live:
            for reader in self._dependents.get(cell, ()):
                if reader in indegree:
                    indegree[reader] += 1
        ready = [c for c, n in indegree.items() if n == 0]
        while ready:
            cell = ready.pop()
            self._values[cell] = _private_evaluate(self._formulas[cell], self._values.get)
            for reader in self._dependents.get(cell, ()):
                if reader in indegree:
                    indegree[reader] -= 1
                    if indegree[reader] == 0:
                        ready.append(reader)

    def _cycle_members(self, nodes: set[str]) -> set[str]:
        """Iterative Tarjan SCC over `nodes`; returns cells on any cycle."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        scc_stack: list[str] = []
        members: set[str] = set()
        counter = 0
        for root in nodes:
            if root in index:
                continue
            work = [(root, iter(self._dependents.get(root, ())))]
            index[root] = low[root] = counter
            counter += 1
            scc_stack.append(root)
            on_stack.add(root)
            while work:
                node, children = work[-1]
                advanced = False
                for child in children:
                    if child not in nodes:
                        continue
                    if child not in index:
                        index[child] = low[child] = counter
                        counter += 1
                        scc_stack.append(child)
                        on_stack.add(child)
                        work.append((child, iter(self._dependents.get(child, ()))))
                        advanced = True
                        break
                    if child in on_stack:
                        low[node] = min(low[node], index[child])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        top = scc_stack.pop()
                        on_stack.discard(top)
                        component.append(top)
                        if top == node:
                            break
                    if len(component) > 1 or node in self._formulas[node].refs:
                        members.update(component)
        return members


def _private_evaluate(parsed, lookup):
    """Same math as formula.evaluate, but invisible to anything wrapping it."""
    try:
        return formula._eval(parsed.ast, lookup)
    except formula._Err as err:
        return err.code
PY
