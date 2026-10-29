#!/bin/bash
# expect: 0
# Correct values, wrong work: recompute every formula from scratch on every edit.
# The obvious shortcut for "values are stale"; the exactly-once contract must reject it.
set -euo pipefail
cat > /app/sheet/engine.py <<'PY'
import re

from . import formula

_NUM = re.compile(r"-?\d+(?:\.\d+)?\Z")


class Sheet:
    def __init__(self):
        self.t, self.f, self.v = {}, {}, {}

    def set(self, cell, text):
        self.set_many({cell: text})

    def set_many(self, edits):
        plan = {}
        for cell, text in edits.items():
            if not formula.is_cell(cell):
                raise ValueError(cell)
            if text.startswith("="):
                plan[cell] = (text, formula.parse(text[1:]))
            elif text == "" or _NUM.match(text):
                plan[cell] = (text, None)
            else:
                raise ValueError(text)
        for cell, (text, parsed) in plan.items():
            self.t.pop(cell, None)
            self.f.pop(cell, None)
            if text:
                self.t[cell] = text
            if parsed is not None:
                self.f[cell] = parsed
        self.v = {c: float(t) for c, t in self.t.items() if not t.startswith("=")}
        cyc = set()
        for c in self.f:
            seen, todo = set(), [r for r in self.f[c].refs if r in self.f]
            while todo:
                n = todo.pop()
                if n == c:
                    cyc.add(c)
                    break
                if n not in seen:
                    seen.add(n)
                    todo.extend(r for r in self.f[n].refs if r in self.f)
        for c in cyc:
            self.v[c] = formula.CYCLE
        done = set(cyc)
        for root in self.f:
            todo = [root]
            while todo:
                c = todo[-1]
                if c in done:
                    todo.pop()
                    continue
                need = [r for r in self.f[c].refs if r in self.f and r not in done]
                if need:
                    todo.extend(need)
                    continue
                todo.pop()
                self.v[c] = formula.evaluate(c, self.f[c], self.v.get)
                done.add(c)

    def get(self, cell):
        if not formula.is_cell(cell):
            raise ValueError(cell)
        return self.v.get(cell, 0.0)

    def text(self, cell):
        if not formula.is_cell(cell):
            raise ValueError(cell)
        return self.t.get(cell, "")
PY
