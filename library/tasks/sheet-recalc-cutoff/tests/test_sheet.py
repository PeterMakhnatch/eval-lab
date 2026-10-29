"""Hidden verifier for sheet-recalc.

The submitted engine never runs in this process. Each test sends a list of
operations to `runner.py`, which runs as an unprivileged user in a scratch
package that holds the grader's pristine formula.py plus the submitted
engine.py, and records every `formula.evaluate` call. This process compares
the results with an independent full-recompute oracle (Kosaraju SCC, values
recomputed from scratch after every edit).
"""

from __future__ import annotations

import collections
import importlib.util
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# test.sh runs pytest under `env -i`, so the override exists only for local authoring runs.
SUBMITTED = Path(os.environ.get("SHEET_RECALC_SUBMITTED", "/app/sheet/engine.py"))
SENTINEL = "@@SHEET-RECALC-RESULT@@"

_spec = importlib.util.spec_from_file_location("grader_formula", HERE / "sheet_formula.py")
F = importlib.util.module_from_spec(_spec)
sys.modules["grader_formula"] = F  # dataclasses resolve their module by name
_spec.loader.exec_module(F)


# ---------------------------------------------------------------- child runs


def run_engine(ops: list[dict], timeout: float = 20.0) -> list[dict]:
    if not SUBMITTED.is_file() or SUBMITTED.is_symlink():
        pytest.fail(f"{SUBMITTED} is missing or not a regular file")
    root = Path(tempfile.mkdtemp(prefix="sheet-sut-"))
    try:
        pkg = root / "sheet"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        shutil.copyfile(HERE / "sheet_formula.py", pkg / "formula.py")
        shutil.copyfile(SUBMITTED, pkg / "engine.py")
        shutil.copyfile(HERE / "runner.py", root / "runner.py")
        for path in [root, pkg, *root.rglob("*")]:
            mode = 0o755 if path.is_dir() else 0o644
            os.chmod(path, mode)
        nonce = secrets.token_hex(16)
        python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else "python3"
        cmd = [python, "-I", "-S", str(root / "runner.py")]
        if os.geteuid() == 0:
            cmd = ["/usr/sbin/runuser", "-u", "nobody", "--", *cmd]
        proc = subprocess.run(
            cmd,
            input=json.dumps({"root": str(root), "nonce": nonce, "ops": ops}),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"engine did not finish {len(ops)} operations within {timeout:.0f}s")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    marker = SENTINEL + nonce
    if proc.stdout.count(marker) != 1:
        pytest.fail(
            f"engine run produced no result (exit {proc.returncode}); stderr tail:\n{proc.stderr[-1500:]}"
        )
    results = json.loads(proc.stdout.split(marker, 1)[1])
    assert len(results) == len(ops)
    return results


# ------------------------------------------------------------------- oracle


class Oracle:
    """Independent model of the specified behaviour.

    The oracle evaluates every affected cell (no cutoff) into its own value
    cache, then derives the expected evaluation set from the rule: an
    affected, non-cycle cell is evaluated iff it was edited, it just left a
    cycle, or one of the cells it reads changed value. Probes compare the
    engine with a from-scratch recompute, and also self-check the cache.
    """

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.parsed: dict[str, object] = {}
        self.readers: dict[str, set[str]] = collections.defaultdict(set)
        self.cache: dict[str, object] = {}  # cell -> value, as get() reports it
        self.cyclic: set[str] = set()

    @staticmethod
    def valid(cell: str, text: str) -> bool:
        if not F.is_cell(cell) or not isinstance(text, str):
            return False
        if text.startswith("="):
            try:
                F.parse(text[1:])
            except F.FormulaError:
                return False
            return True
        return text == "" or re.fullmatch(r"-?\d+(?:\.\d+)?", text) is not None

    def get(self, cell: str) -> object:
        return self.cache.get(cell, 0.0)

    def apply(self, edits: dict[str, str]):
        """Returns (raised, expected_eval_counter)."""
        if not all(self.valid(c, t) for c, t in edits.items()):
            return True, collections.Counter()
        old = {}
        for cell, text in edits.items():
            old[cell] = self.get(cell)
            previous = self.parsed.pop(cell, None)
            if previous is not None:
                for ref in previous.refs:
                    self.readers[ref].discard(cell)
            self.texts.pop(cell, None)
            self.cache.pop(cell, None)
            self.cyclic.discard(cell)
            if text == "":
                continue
            self.texts[cell] = text
            if text.startswith("="):
                self.parsed[cell] = F.parse(text[1:])
                for ref in self.parsed[cell].refs:
                    self.readers[ref].add(cell)
            else:
                self.cache[cell] = float(text)
        affected = {c for c in edits if c in self.parsed}
        stack = list(edits)
        while stack:
            for r in self.readers.get(stack.pop(), ()):
                if r not in affected:
                    affected.add(r)
                    stack.append(r)
        for cell in affected:
            old.setdefault(cell, self.get(cell))
        cyclic = self.cycle_members({c: self.parsed[c] for c in affected})
        left_cycle = (self.cyclic & affected) - cyclic
        self.cyclic = (self.cyclic - affected) | cyclic
        for cell in cyclic:
            self.cache[cell] = F.CYCLE
        # Evaluate every other affected cell, inputs first (DFS post-order).
        done = set(cyclic)
        for root in sorted(affected - cyclic):
            todo = [root]
            while todo:
                cell = todo[-1]
                if cell in done:
                    todo.pop()
                    continue
                pending = [r for r in self.parsed[cell].refs if r in affected and r not in done]
                if pending:
                    todo.extend(pending)
                    continue
                todo.pop()
                lookup = lambda name: self.cache.get(name)  # noqa: E731
                self.cache[cell] = F.evaluate(cell, self.parsed[cell], lookup)
                done.add(cell)
        changed = {c for c in old if not same_strict(old[c], self.get(c))}
        expected = {
            c
            for c in affected - cyclic
            if c in edits or c in left_cycle or not changed.isdisjoint(self.parsed[c].refs)
        }
        return False, collections.Counter(expected)

    def formulas(self) -> dict:
        return dict(self.parsed)

    @staticmethod
    def cycle_members(formulas: dict) -> set[str]:
        # Kosaraju: order by finish time on refs graph, then sweep the transpose.
        order, seen = [], set()
        for root in formulas:
            if root in seen:
                continue
            seen.add(root)
            stack = [(root, iter(sorted(r for r in formulas[root].refs if r in formulas)))]
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    stack.pop()
                    order.append(node)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(
                        (nxt, iter(sorted(r for r in formulas[nxt].refs if r in formulas)))
                    )
        transpose = collections.defaultdict(list)
        for c, f in formulas.items():
            for r in f.refs:
                if r in formulas:
                    transpose[r].append(c)
        members, assigned = set(), set()
        for root in reversed(order):
            if root in assigned:
                continue
            comp, stack = [], [root]
            assigned.add(root)
            while stack:
                node = stack.pop()
                comp.append(node)
                for nxt in transpose.get(node, ()):
                    if nxt not in assigned:
                        assigned.add(nxt)
                        stack.append(nxt)
            if len(comp) > 1 or root in formulas[root].refs:
                members.update(comp)
        return members

    def values(self) -> dict[str, object]:
        formulas = self.formulas()
        cyclic = self.cycle_members(formulas)
        values: dict[str, object] = {}
        for c, t in self.texts.items():
            if not t.startswith("="):
                values[c] = float(t)
        for c in cyclic:
            values[c] = F.CYCLE
        done = set(cyclic)
        for root in formulas:
            if root in done:
                continue
            stack = [root]
            while stack:
                node = stack[-1]
                pending = [r for r in formulas[node].refs if r in formulas and r not in done]
                if pending:
                    stack.extend(pending)
                    continue
                stack.pop()
                if node not in done:
                    values[node] = F.evaluate(node, formulas[node], values.get)
                    done.add(node)
        return values

    def value(self, cell: str, values: dict) -> object:
        return values.get(cell, 0.0)


def same(a, b) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def same_strict(a, b) -> bool:
    """The contract's notion of 'value changed': get() results compared with ==."""
    return type(a) is type(b) and a == b


def check(oracle: Oracle, ops: list[dict], results: list[dict], label: str = "") -> None:
    for i, (op, res) in enumerate(zip(ops, results, strict=True)):
        where = f"{label} op#{i} {json.dumps({k: v for k, v in op.items() if k not in ('probe', 'probe_text')})[:200]}"
        if op["op"] == "probe":
            raised, expected = False, collections.Counter()
        else:
            edits = {op["cell"]: op["text"]} if op["op"] == "set" else op["edits"]
            raised, expected = oracle.apply(edits)
        if raised:
            assert res["error"] == "ValueError", f"{where}: expected ValueError, got {res['error']}"
        else:
            assert res["error"] is None, f"{where}: unexpected exception {res['error']}"
        got = collections.Counter(res["evals"])
        if got != expected:
            extra, missing = got - expected, expected - got
            pytest.fail(
                f"{where}: wrong evaluations: {sum(got.values())} calls, expected {sum(expected.values())}; "
                f"extra={dict(list(extra.items())[:8])} missing={sorted(missing)[:8]}"
            )
        assert res["get_evals"] == 0, (
            f"{where}: get() evaluated formulas ({res['get_evals']} calls)"
        )
        if res["values"]:
            values = oracle.values()
            for cell, v in res["values"].items():
                want = oracle.value(cell, values)
                # Grader self-check: the oracle's incremental cache must equal a full recompute.
                assert same(oracle.get(cell), want), f"ORACLE BUG at {where}: {cell}"
                assert same(v, want), f"{where}: {cell} = {v!r}, expected {want!r}"
        for cell, t in res["texts"].items():
            assert t == oracle.texts.get(cell, ""), (
                f"{where}: text({cell}) = {t!r}, expected {oracle.texts.get(cell, '')!r}"
            )


def scenario(steps: list, probe: list[str], label: str, timeout: float = 20.0) -> None:
    ops = []
    for step in steps:
        if isinstance(step, dict):
            ops.append({"op": "set_many", "edits": step, "probe": probe, "probe_text": probe})
        else:
            cell, text = step
            ops.append(
                {"op": "set", "cell": cell, "text": text, "probe": probe, "probe_text": probe}
            )
    check(Oracle(), ops, run_engine(ops, timeout), label)


GRID = [f"{c}{r}" for c in "ABCDEF" for r in range(1, 9)]


# -------------------------------------------------------------------- tests


def test_unchanged_value_stops_propagation():
    scenario(
        [
            ("A1", "1"),
            ("B1", "=A1*0"),
            ("C1", "=B1+1"),
            ("D1", "=C1+A1"),
            ("A1", "5"),
            ("A1", "5"),
            ("B2", "=MAX(A1, 100)"),
            ("C2", "=B2/4"),
            ("A1", "7"),
            ("A1", "200"),
        ],
        ["A1", "B1", "C1", "D1", "B2", "C2"],
        "cutoff",
    )


def test_errors_that_stay_errors_do_not_propagate():
    scenario(
        [("A1", "0"), ("B1", "=1/A1"), ("C1", "=B1+1"), ("A2", "3"), ("B1", "=A2/A1"), ("A1", "1")],
        ["A1", "A2", "B1", "C1"],
        "error-cutoff",
    )


def test_clearing_a_zero_is_not_a_change():
    scenario(
        [("A1", "0"), ("B1", "=A1+1"), ("C1", "=B1"), ("A1", ""), ("A1", "0.0"), ("A1", "-0")],
        ["A1", "B1", "C1"],
        "zero",
    )


def test_cell_leaving_a_cycle_is_reevaluated_even_if_inputs_did_not_change():
    # X1 sits on two cycles' worth of #CYCLE inputs. Dropping Y1's read of X1
    # takes X1 off every cycle while Y1 stays #CYCLE (it is still on Y1<->W1),
    # so none of X1's inputs change value, yet X1 must now evaluate to #DIV/0!.
    scenario(
        [
            ("W1", "=Y1"),
            ("X1", "=1/0+Y1"),
            ("Y1", "=W1+X1"),
            ("Z1", "=X1"),
            ("Y1", "=W1"),
            ("Y1", "=W1+X1"),
        ],
        ["W1", "X1", "Y1", "Z1"],
        "cycle-exit",
    )


def test_set_many_change_detection_uses_values_before_the_batch():
    scenario(
        [
            ("A1", "4"),
            ("B1", "=A1*2"),
            ("C1", "=B1+A2"),
            {"A1": "=A2", "A2": "4"},
            {"A1": "4", "A2": ""},
            {"B1": "=A1+4", "A2": "0"},
        ],
        ["A1", "A2", "B1", "C1"],
        "batch-cutoff",
    )


def test_literals_arithmetic_and_empty_cells():
    scenario(
        [
            ("A1", "2"),
            ("A2", "-3.5"),
            ("B1", "=A1*(A2+10)/4"),
            ("B2", "=MAX(A1:A3)-MIN(A1, A3, 7)"),
            ("B3", "=SUM(A1:B2)"),
        ],
        ["A1", "A2", "A3", "B1", "B2", "B3"],
        "literals",
    )


def test_invalid_input_raises_and_changes_nothing():
    scenario(
        [
            ("A1", "1"),
            ("B1", "=A1+1"),
            ("A1", "=1+"),
            ("a1", "5"),
            ("A1000", "5"),
            ("A1", "abc"),
            ("B1", "=SUM(A1:)"),
            {"A1": "9", "B1": "=(("},
            ("A1", "4"),
        ],
        ["A1", "B1"],
        "invalid",
    )


def test_transitive_chain_updates_every_dependent_once():
    scenario(
        [("A1", "1"), ("A2", "=A1+1"), ("A3", "=A2*2"), ("A4", "=A3-A1"), ("A1", "10")],
        ["A1", "A2", "A3", "A4"],
        "chain",
    )


def test_diamond_is_evaluated_once_per_cell_with_fresh_inputs():
    scenario(
        [
            ("A1", "1"),
            ("B1", "=A1+1"),
            ("B2", "=A1*10"),
            ("C1", "=B1+B2"),
            ("D1", "=C1+B1+A1"),
            ("A1", "3"),
        ],
        ["A1", "B1", "B2", "C1", "D1"],
        "diamond",
    )


def test_range_tracks_cells_that_are_still_empty():
    scenario(
        [("D1", "=SUM(A1:A5)"), ("A1", "1"), ("A3", "4"), ("A5", "=A3*2"), ("A3", "")],
        ["A1", "A3", "A5", "D1"],
        "range",
    )


def test_replaced_formula_drops_old_dependencies():
    scenario(
        [
            ("A1", "1"),
            ("C1", "7"),
            ("B1", "=A1"),
            ("B1", "=C1"),
            ("A1", "5"),
            ("A2", "=B1"),
            ("B1", "2"),
            ("A1", "6"),
            ("C1", "8"),
        ],
        ["A1", "B1", "C1", "A2"],
        "stale-edges",
    )


def test_cycle_members_are_errors_and_downstream_propagates():
    scenario(
        [
            ("A1", "=B1"),
            ("B1", "=C1+1"),
            ("D1", "=A1*2"),
            ("C1", "=A1"),
            ("E1", "=D1+1"),
            ("F1", "=F1+1"),
        ],
        ["A1", "B1", "C1", "D1", "E1", "F1"],
        "cycle",
    )


def test_breaking_a_cycle_recovers_every_member():
    scenario(
        [
            ("A1", "=B1"),
            ("B1", "=C1+1"),
            ("C1", "=A1"),
            ("D1", "=C1"),
            ("C1", "5"),
            ("C1", "=B1"),
            ("B1", "1"),
            ("F1", "=F1"),
            ("F1", "2"),
        ],
        ["A1", "B1", "C1", "D1", "F1"],
        "cycle-recovery",
    )


def test_old_edges_do_not_create_false_cycles():
    scenario([("A1", "=B1"), ("A1", "1"), ("B1", "=A1"), ("A1", "4")], ["A1", "B1"], "false-cycle")


def test_errors_propagate_and_recover():
    scenario(
        [
            ("A1", "0"),
            ("B1", "=1/A1"),
            ("C1", "=B1+1"),
            ("D1", "=SUM(B1:C1)"),
            ("A1", "2"),
            ("A1", ""),
            ("E1", "=A1/A1"),
        ],
        ["A1", "B1", "C1", "D1", "E1"],
        "errors",
    )


def test_set_many_is_atomic_and_evaluates_once():
    scenario(
        [
            ("A1", "=B1+1"),
            ("B1", "5"),
            ("C1", "=A1+B1"),
            {"A1": "7", "B1": "=A1+1"},
            {"A1": "=B1", "B1": "=A1"},
            {"B1": "3", "C1": "=A1*B1", "D1": "=C1"},
            {"A1": "1", "A2": "2", "A3": "=A1+A2", "A4": "=A3+C1"},
            {"A1": "100", "B1": "=A1+"},
            {"A1": "", "C1": ""},
        ],
        ["A1", "A2", "A3", "A4", "B1", "C1", "D1"],
        "batch",
    )


def chain_cells(n: int) -> list[str]:
    return [f"{chr(65 + i // 999)}{i % 999 + 1}" for i in range(n)]


def test_deep_chain_of_ten_thousand_cells():
    cells = chain_cells(10_000)
    ends = [cells[0], cells[1], cells[5000], cells[-1]]
    ops = [{"op": "set", "cell": cells[0], "text": "1"}]
    ops += [
        {"op": "set", "cell": c, "text": f"={p}+1"} for p, c in zip(cells, cells[1:], strict=False)
    ]
    ops += [
        {"op": "set", "cell": cells[0], "text": "5", "probe": ends},
        {"op": "set", "cell": cells[0], "text": f"={cells[-1]}", "probe": ends},
        {"op": "set", "cell": cells[0], "text": "2", "probe": ends},
    ]
    check(Oracle(), ops, run_engine(ops, timeout=60), "deep-chain")


def test_wide_fan_out_and_large_ranges():
    readers = [f"{c}{r}" for c in "BC" for r in range(1, 1000)]
    ops = [{"op": "set", "cell": "A1", "text": "1"}]
    ops += [{"op": "set_many", "edits": {cell: f"=A1*{i % 7}" for i, cell in enumerate(readers)}}]
    ops += [
        {"op": "set", "cell": "D1", "text": "=SUM(B1:C999)+SUM(A1:A999)"},
        {"op": "set", "cell": "A1", "text": "3", "probe": ["A1", "B5", "C999", "D1"]},
        {"op": "set", "cell": "A500", "text": "1", "probe": ["D1"]},
    ]
    check(Oracle(), ops, run_engine(ops, timeout=60), "wide")


def random_expr(rng: random.Random, depth: int = 0) -> str:
    roll = rng.random()
    if depth >= 2 or roll < 0.35:
        return rng.choice(GRID) if rng.random() < 0.75 else str(rng.randint(0, 9))
    if roll < 0.55:
        a, b = sorted(rng.sample(GRID, 2))
        args = [f"{a}:{b}"] + [random_expr(rng, depth + 1) for _ in range(rng.randint(0, 1))]
        return f"{rng.choice(['SUM', 'MIN', 'MAX'])}({', '.join(args)})"
    op = rng.choice("+-*/" if rng.random() < 0.8 else "/")
    return f"({random_expr(rng, depth + 1)}{op}{random_expr(rng, depth + 1)})"


def random_text(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.3:
        return str(rng.randint(-5, 9))
    if roll < 0.42:
        return ""
    if roll < 0.46:
        return rng.choice(["=", "=A1+", "=SUM(A1)(", "x1", "1..2", "=G0"])
    if roll < 0.56:
        # Formulas whose value often survives an input change exercise the cutoff.
        a, b = rng.sample(GRID, 2)
        return rng.choice([f"={a}*0", f"=MIN({a}, 0)", f"=MAX({a}:{b}, 5)", f"=({a}-{a})+{b}"])
    return "=" + random_expr(rng)


@pytest.mark.parametrize("seed", [11, 23, 37, 41, 59, 73])
def test_randomized_edit_sequences_match_full_recompute(seed: int):
    rng = random.Random(seed)
    ops = []
    for _ in range(300):
        if rng.random() < 0.15:
            cells = rng.sample(GRID, rng.randint(2, 4))
            ops.append(
                {"op": "set_many", "edits": {c: random_text(rng) for c in cells}, "probe": GRID}
            )
        else:
            ops.append(
                {"op": "set", "cell": rng.choice(GRID), "text": random_text(rng), "probe": GRID}
            )
    check(Oracle(), ops, run_engine(ops, timeout=30), f"seed={seed}")
