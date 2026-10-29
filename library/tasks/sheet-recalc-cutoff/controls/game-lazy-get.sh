#!/bin/bash
# expect: 0
# Correct values and counts, but deferred: edits only queue work and the
# first get() recalculates. The contract requires eager recalculation.
set -euo pipefail
cat > /app/sheet/engine.py <<'PY'
"""Spreadsheet engine with incremental recalculation and early cutoff (reference solution)."""

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
        self._cyclic: set[str] = set()
        self._dirty: list = []

    # -- public API -----------------------------------------------------

    def set(self, cell: str, text: str) -> None:
        self.set_many({cell: text})

    def set_many(self, edits: dict[str, str]) -> None:
        staged = {cell: self._stage(cell, text) for cell, text in edits.items()}
        before = {cell: self.get(cell) for cell in staged}
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
        # Edited cells that are no longer formulas have their final value already.
        changed = {
            cell
            for cell in staged
            if cell not in self._formulas and self.get(cell) != before[cell]
        }
        self._dirty.append((staged, before, changed))

    def get(self, cell: str) -> formula.Value:
        self._check(cell)
        while self._dirty:
            self._recalculate(*self._dirty.pop(0))
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
        self._cyclic.discard(cell)
        self._text.pop(cell, None)
        self._values.pop(cell, None)

    def _recalculate(self, edited, before: dict, changed: set[str]) -> None:
        def previous(cell: str) -> formula.Value:
            return before[cell] if cell in before else self.get(cell)

        # 1. Every formula cell downstream of an edit (edited formulas included).
        affected = {c for c in edited if c in self._formulas}
        stack = list(edited)
        while stack:
            for reader in self._dependents.get(stack.pop(), ()):
                if reader not in affected:
                    affected.add(reader)
                    stack.append(reader)

        # 2. Cells on a cycle get #CYCLE and are never evaluated.
        was_cyclic = self._cyclic & affected
        cyclic = self._cycle_members(affected)
        self._cyclic = (self._cyclic - affected) | cyclic
        for cell in cyclic:
            if previous(cell) != formula.CYCLE:
                changed.add(cell)
            self._values[cell] = formula.CYCLE

        # 3. Visit the rest inputs-first (Kahn); evaluate only cells that were
        #    edited, just left a cycle, or read a cell whose value changed.
        must_evaluate = (set(edited) | was_cyclic) & affected
        live = affected - cyclic
        indegree = dict.fromkeys(live, 0)
        for cell in live:
            for reader in self._dependents.get(cell, ()):
                if reader in indegree:
                    indegree[reader] += 1
        ready = [c for c, n in indegree.items() if n == 0]
        while ready:
            cell = ready.pop()
            parsed = self._formulas[cell]
            if cell in must_evaluate or not changed.isdisjoint(parsed.refs):
                old = previous(cell)
                self._values[cell] = formula.evaluate(cell, parsed, self._values.get)
                if self._values[cell] != old:
                    changed.add(cell)
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
PY
