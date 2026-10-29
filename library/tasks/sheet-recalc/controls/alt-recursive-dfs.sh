#!/bin/bash
# expect: 1
# Alternative correct engine with a different algorithm (recursive DFS for the
# affected set, recursive Tarjan, DFS post-order) and a raised recursion limit.
# Guards against a verifier that only accepts the reference's structure.
set -euo pipefail
cat > /app/sheet/engine.py <<'PY'
import re
import sys

from . import formula

sys.setrecursionlimit(200_000)
_NUM = re.compile(r"-?\d+(?:\.\d+)?\Z")


class Sheet:
    def __init__(self):
        self.t, self.f, self.v, self.rd = {}, {}, {}, {}

    def set(self, cell, text):
        self.set_many({cell: text})

    def set_many(self, edits):
        plan = []
        for cell, text in edits.items():
            if not formula.is_cell(cell) or not isinstance(text, str):
                raise ValueError(cell)
            if text.startswith("="):
                plan.append((cell, text, formula.parse(text[1:])))
            elif text == "" or _NUM.match(text):
                plan.append((cell, text, None))
            else:
                raise ValueError(text)
        for cell, text, parsed in plan:
            if cell in self.f:
                for r in self.f.pop(cell).refs:
                    self.rd[r].discard(cell)
            self.t.pop(cell, None)
            self.v.pop(cell, None)
            if text:
                self.t[cell] = text
                if parsed is None:
                    self.v[cell] = float(text)
                else:
                    self.f[cell] = parsed
                    for r in parsed.refs:
                        self.rd.setdefault(r, set()).add(cell)
        aff = set()

        def mark(c):
            for r in self.rd.get(c, ()):
                if r not in aff:
                    aff.add(r)
                    mark(r)

        for cell, _, parsed in plan:
            if parsed is not None:
                aff.add(cell)
            mark(cell)

        idx, low, on, st, cyc = {}, {}, set(), [], set()

        def strong(c):
            idx[c] = low[c] = len(idx)
            st.append(c)
            on.add(c)
            for r in self.rd.get(c, ()):
                if r not in aff:
                    continue
                if r not in idx:
                    strong(r)
                    low[c] = min(low[c], low[r])
                elif r in on:
                    low[c] = min(low[c], idx[r])
            if low[c] == idx[c]:
                comp = []
                while True:
                    x = st.pop()
                    on.discard(x)
                    comp.append(x)
                    if x == c:
                        break
                if len(comp) > 1 or c in self.f[c].refs:
                    cyc.update(comp)

        for c in aff:
            if c not in idx:
                strong(c)
        for c in cyc:
            self.v[c] = formula.CYCLE
        order, seen = [], set(cyc)

        def visit(c):
            seen.add(c)
            for r in self.rd.get(c, ()):
                if r in aff and r not in seen:
                    visit(r)
            order.append(c)

        for c in aff:
            if c not in seen:
                visit(c)
        for c in reversed(order):
            self.v[c] = formula.evaluate(c, self.f[c], self.v.get)

    def get(self, cell):
        if not formula.is_cell(cell):
            raise ValueError(cell)
        return self.v.get(cell, 0.0)

    def text(self, cell):
        if not formula.is_cell(cell):
            raise ValueError(cell)
        return self.t.get(cell, "")
PY
