"""Spreadsheet engine with incremental recalculation."""

from __future__ import annotations

from . import formula


class Sheet:
    def __init__(self) -> None:
        self._text: dict[str, str] = {}
        self._formulas: dict[str, formula.Formula] = {}
        self._values: dict[str, formula.Value] = {}
        self._dependents: dict[str, set[str]] = {}

    # -- public API -----------------------------------------------------

    def set(self, cell: str, text: str) -> None:
        self._check(cell)
        if text.startswith("="):
            parsed = formula.parse(text[1:])
            self._text[cell] = text
            self._formulas[cell] = parsed
            for ref in parsed.refs:
                if ref in self._text:
                    self._dependents.setdefault(ref, set()).add(cell)
            self._recalc(cell, set())
        elif text == "":
            self._text.pop(cell, None)
            self._formulas.pop(cell, None)
            self._values.pop(cell, None)
        else:
            self._values[cell] = float(text)
            self._text[cell] = text
            self._formulas.pop(cell, None)
        self._propagate(cell, set())

    def set_many(self, edits: dict[str, str]) -> None:
        for cell, text in edits.items():
            self.set(cell, text)

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

    def _lookup(self, cell: str):
        return self._values.get(cell)

    def _recalc(self, cell: str, visiting: set[str]) -> None:
        if cell in visiting:
            self._values[cell] = formula.CYCLE
            return
        self._values[cell] = formula.evaluate(cell, self._formulas[cell], self._lookup)

    def _propagate(self, cell: str, visiting: set[str]) -> None:
        visiting = visiting | {cell}
        for dep in sorted(self._dependents.get(cell, ())):
            if dep not in self._formulas:
                continue
            self._recalc(dep, visiting)
            if dep not in visiting:
                self._propagate(dep, visiting)
