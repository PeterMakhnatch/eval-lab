"""Visible smoke tests. Run with: cd /app && python -m pytest -q tests"""

import pytest
from sheet import formula
from sheet.engine import Sheet


@pytest.fixture
def evals(monkeypatch):
    """Records the cell name of every formula.evaluate call."""
    calls = []
    original = formula.evaluate

    def counted(cell, parsed, lookup):
        calls.append(cell)
        return original(cell, parsed, lookup)

    monkeypatch.setattr(formula, "evaluate", counted)
    return calls


def test_simple_formula():
    s = Sheet()
    s.set("A1", "2")
    s.set("A2", "=A1*3")
    assert s.get("A2") == 6.0
    assert s.text("A2") == "=A1*3"
    assert s.get("B7") == 0.0


def test_dependent_updates():
    s = Sheet()
    s.set("A1", "1")
    s.set("B1", "=A1+1")
    s.set("A1", "5")
    assert s.get("B1") == 6.0


def test_division_by_zero():
    s = Sheet()
    s.set("A1", "=1/B1")
    assert s.get("A1") == formula.DIV0


def test_each_dependent_evaluated_once(evals):
    s = Sheet()
    s.set("A1", "1")
    s.set("B1", "=A1+1")
    s.set("B2", "=A1+2")
    s.set("C1", "=B1+B2")
    evals.clear()
    s.set("A1", "10")
    assert sorted(evals) == ["B1", "B2", "C1"]
    assert s.get("C1") == 23.0


def test_unchanged_value_is_not_propagated(evals):
    s = Sheet()
    s.set("A1", "1")
    s.set("B1", "=A1*0")
    s.set("C1", "=B1+1")
    evals.clear()
    s.set("A1", "9")
    assert evals == ["B1"]
    assert s.get("C1") == 1.0
