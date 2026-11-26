from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random
from typing import Any

from evallab.package_anatomy import (
    _MAX_LIST,
    Counts,
    Package,
    TestSource,
    _py_counts,
    _regex_counts,
    analyze_package,
    test_suite_rigor,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_MAX_TESTS = 12
_PYTEST_PATH = "tests/test_generated.py"
_UNITTEST_PATH = "tests/test_generated_unittest.py"
_BROKEN_PATH = "tests/test_broken_syntax.py"
_FRAGMENT_PATH = "tests/test_patch_fragment.py"
_BLOCKS_PATH = "tests/test_surface_blocks.py"
_GUARDS_PATH = "tests/surface_guards.test.js"
_CHAIN_PATH = "tests/test_helper_chains.py"
_CHAIN_KINDS = ("assert", "fail", "plain")
_CHAIN_BODIES = {
    "assert": ("assert value",),
    "fail": ("if not value:", '    pytest.fail("value is false")'),
    "plain": ("value = bool(value)",),
}
_MAX_CHAINS = 8
_WRAPPER_PATH = "tests/test_outputs.py"
_GRADER_STEMS = ("grader", "scoring", "model_for_tests", "evaluate")
_IMPORT_FORMS = ("inner_from", "top_from", "alias_import", "relative_from")
_PLAIN_FORMS = ("single_assert", "constant_import", "uncalled_import")
_MAX_GRADER_CHECKS = 6
_INSTRUCTION = "Run the generated checks and report every failure.\n"
_HELPERS = (
    "APP_DIR = Path('/app')",
    "SCRIPT_PATH = APP_DIR / 'main.py'",
    "",
    "def check(value):",
    "    assert value",
    "",
    "def require(value):",
    "    if not value:",
    "        raise AssertionError",
    "",
)
_TEST_SH = "#!/bin/bash\ncd /tests\npytest -q test_outputs.py\n"
_COUNT_FIELDS = (
    "files",
    "tests",
    "assertions",
    "negative_assertions",
    "negative_named_tests",
    "literal_eq_assertions",
    "surface_assertions",
    "surface_only_tests",
    "source_text_assertions",
    "mock_assertions",
    "tests_without_assertion",
    "parametrized_cases",
    "randomized_or_property",
    "loops_with_assertions",
    "skipped_or_xfail",
    "mode_ast_files",
    "mode_regex_files",
)
_BROKEN_TEXT = (
    "def test_invalid_broken(:\n"
    "    assert not flag\n"
    "    with pytest.raises(ValueError):\n"
    "        raise ValueError()\n"
    "    assert value == 7\n"
    "    assert path.exists()\n"
    "    mock.assert_called_once()\n"
    "    self.assertEqual(1, make())\n"
)
_FRAGMENT_TEXT = (
    "def test_fragment():\n"
    "    assert not ready\n"
    "    with pytest.raises(KeyError):\n"
    "        raise KeyError()\n"
    "    assert len(items) == 0\n"
)
_BLOCKS_TEXT = (
    "assert setup is not None\n"
    "def test_exists(:\n"
    "    assert path.exists()\n"
    "    assert handle is not None\n"
    "def test_mixed():\n"
    "    assert path.exists()\n"
    "    assert value == 7\n"
    "def test_blank():\n"
    "    value = compute()\n"
    "def test_attr():\n"
    "    assert hasattr(module, 'run')\n"
)
_GUARDS_TEXT = (
    'test("behavior beside a guard", () => {\n'
    "  const expected = path.exists();\n"
    "  expect(actual).toBe(3);\n"
    "});\n"
    'test("surface beside a guard", () => {\n'
    "  const expected = path.exists();\n"
    "  expect(actual).toBeDefined();\n"
    "});\n"
    'test("second behavior beside a guard", () => {\n'
    "  const expected = path.exists();\n"
    "  expect(actual).toBe(4);\n"
    "});\n"
)


@dataclass(frozen=True, slots=True)
class _Shape:
    name: str
    delta: Counts
    decorators: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    hit: int = -1


@dataclass(frozen=True, slots=True)
class _TestCase:
    name: str
    shapes: tuple[_Shape, ...]
    negative_name: bool


@dataclass(frozen=True, slots=True)
class _Chain:
    index: int
    kinds: tuple[str, ...]
    cycle: bool

    @property
    def reaches(self) -> bool:
        return any(kind != "plain" for kind in self.kinds)


_PYTEST_SHAPES = (
    _Shape("plain", Counts(assertions=1), (), ("assert left == right",)),
    _Shape("negative", Counts(assertions=1, negative_assertions=1), (), ("assert code != 0",)),
    _Shape("literal", Counts(assertions=1, literal_eq_assertions=1), (), ("assert value == 7",)),
    _Shape("surface", Counts(assertions=1, surface_assertions=1), (), ("assert path.exists()",)),
    _Shape("mock", Counts(assertions=1, mock_assertions=1), (), ("mock.assert_called_once()",)),
    _Shape(
        "raises",
        Counts(assertions=1, negative_assertions=1),
        (),
        ("with pytest.raises(ValueError):", "    raise ValueError()"),
    ),
    _Shape(
        "source",
        Counts(assertions=1, surface_assertions=1, source_text_assertions=1),
        (),
        ("source = Path(__file__).read_text()", 'assert "example.py" in source'),
        1,
    ),
    _Shape(
        "source_alias",
        Counts(assertions=1, surface_assertions=1, source_text_assertions=1),
        (),
        ("with open(SCRIPT_PATH) as handle:", "    script = handle.read()", 'assert "main" in script.lower()'),
        2,
    ),
    _Shape(
        "unrelated_in",
        Counts(assertions=1),
        (),
        ("source = Path(__file__).read_text()", "log = run_cli()", 'assert "example.py" in log'),
    ),
    _Shape(
        "reused_alias",
        Counts(assertions=1),
        (),
        (
            "with open('/app/cmd.sh') as handle:",
            "    command = handle.read()",
            "with open('/app/report.json') as handle:",
            "    report = handle.read()",
            'assert "null" in report',
        ),
    ),
    _Shape(
        "loop",
        Counts(assertions=1, surface_assertions=1, loops_with_assertions=1),
        (),
        ("for item in items:", "    assert item"),
    ),
    _Shape(
        "param",
        Counts(assertions=1, parametrized_cases=3),
        ('@pytest.mark.parametrize("x", [1, 2, 3])',),
        ("assert x == x",),
    ),
    _Shape(
        "skip",
        Counts(assertions=1, skipped_or_xfail=1),
        ('@pytest.mark.skip("why")',),
        ("assert left == right",),
    ),
    _Shape("helper", Counts(assertions=1), (), ("check(value)",)),
    _Shape("raise", Counts(assertions=1), (), ("if not value:", '    raise AssertionError("value is false")')),
    _Shape("raise_helper", Counts(assertions=1), (), ("require(value)",)),
    _Shape("without", Counts(tests_without_assertion=1), (), ("value = compute()",)),
)

_UNITTEST_SHAPES = (
    _Shape("plain", Counts(assertions=1), (), ("self.assertEqual(left, right)",)),
    _Shape("negative", Counts(assertions=1, negative_assertions=1), (), ("self.assertFalse(flag)",)),
    _Shape(
        "literal",
        Counts(assertions=1, literal_eq_assertions=1),
        (),
        ("self.assertEqual(7, make())",),
    ),
    _Shape("surface", Counts(assertions=1, surface_assertions=1), (), ("self.assertTrue(flag)",)),
    _Shape("mock", Counts(assertions=1, mock_assertions=1), (), ("mock.assert_called_once()",)),
    _Shape(
        "raises",
        Counts(assertions=2, negative_assertions=2),
        (),
        ("with self.assertRaises(ValueError):", "    raise ValueError()"),
    ),
    _Shape(
        "source",
        Counts(assertions=1, surface_assertions=1, source_text_assertions=1),
        (),
        ("source = Path(__file__).read_text()", 'self.assertIn("example.py", source)'),
        1,
    ),
    _Shape(
        "source_alias",
        Counts(assertions=1, surface_assertions=1, source_text_assertions=1),
        (),
        ("with open(SCRIPT_PATH) as handle:", "    script = handle.read()", 'self.assertIn("main", script[:400])'),
        2,
    ),
    _Shape(
        "unrelated_in",
        Counts(assertions=1),
        (),
        ("source = Path(__file__).read_text()", "log = run_cli()", 'self.assertIn("example.py", log)'),
    ),
    _Shape(
        "loop",
        Counts(assertions=1, surface_assertions=1, loops_with_assertions=1),
        (),
        ("for item in items:", "    assert item"),
    ),
    _Shape(
        "skip",
        Counts(assertions=1, skipped_or_xfail=1),
        ('@unittest.skip("why")',),
        ("self.assertEqual(left, right)",),
    ),
    _Shape("helper", Counts(assertions=1), (), ("check(value)",)),
    _Shape("raise", Counts(assertions=1), (), ("if not flag:", '    raise AssertionError("flag is false")')),
    _Shape("raise_helper", Counts(assertions=1), (), ("require(flag)",)),
    _Shape("without", Counts(tests_without_assertion=1), (), ("value = compute()",)),
)


def _plan(rng: Random, shapes: tuple[_Shape, ...]) -> list[_TestCase]:
    assert 1 <= len(shapes) <= len(_PYTEST_SHAPES)
    count = rng.randint(8, _MAX_TESTS - 1)
    slots: list[list[_Shape]] = [[] for _ in range(count)]
    slots[0] = [next(shape for shape in shapes if shape.name == "without")]
    pool = [shape for shape in shapes if shape.name != "without"]
    for shape in pool:
        for index in rng.sample(range(1, count), rng.randint(1, 2)):
            slots[index].append(shape)
    for index in range(1, count):
        if not slots[index]:
            slots[index].append(rng.choice(pool))
    cases: list[_TestCase] = []
    for index, assigned in enumerate(slots):
        assert assigned
        negative_name = index == 1 or rng.random() < 0.25
        name = f"test_invalid_case_{index}" if negative_name else f"test_case_{index}"
        cases.append(_TestCase(name, tuple(assigned), negative_name))
    assert len(cases) == count
    return cases


def _surface_only(case: _TestCase) -> bool:
    asserted = sum(shape.delta.assertions for shape in case.shapes)
    surfaced = sum(shape.delta.surface_assertions for shape in case.shapes)
    assert 0 <= surfaced <= asserted
    return asserted > 0 and surfaced == asserted


def _tally(cases: list[_TestCase], randomized_import: bool) -> Counts:
    counts = Counts(files=1, tests=len(cases), mode_ast_files=1)
    if randomized_import:
        counts.randomized_or_property += 1
    for case in cases:
        counts.negative_named_tests += int(case.negative_name)
        counts.surface_only_tests += int(_surface_only(case))
        for shape in case.shapes:
            counts.add(shape.delta)
    assert counts.files == 1 and counts.mode_ast_files == 1
    assert counts.tests == len(cases) <= _MAX_TESTS
    assert counts.negative_assertions > 0 and counts.literal_eq_assertions > 0
    assert counts.surface_assertions > 0 and counts.mock_assertions > 0
    assert counts.source_text_assertions > 0 and counts.loops_with_assertions > 0
    assert counts.source_text_assertions == sum(shape.hit >= 0 for case in cases for shape in case.shapes)
    assert counts.skipped_or_xfail > 0 and counts.tests_without_assertion > 0
    assert counts.negative_named_tests > 0
    assert 0 <= counts.surface_only_tests < counts.tests
    return counts


def _emit_body(lines: list[str], case: _TestCase, indent: str, hits: list[dict[str, object]]) -> None:
    assert case.name and case.shapes
    before = len(lines)
    for shape in case.shapes:
        assert -1 <= shape.hit < len(shape.body)
        if shape.hit >= 0:
            hits.append({"line": len(lines) + 1 + shape.hit, "text": shape.body[shape.hit].strip()})
        lines.extend(f"{indent}{line}" for line in shape.body)
    assert len(lines) > before
    lines.append("")


def _render_pytest(cases: list[_TestCase], randomized_import: bool) -> tuple[str, list[dict[str, object]]]:
    lines = ["import pytest"]
    if randomized_import:
        lines.append("import random")
    lines.extend(["", *_HELPERS])
    hits: list[dict[str, object]] = []
    for case in cases:
        for shape in case.shapes:
            lines.extend(shape.decorators)
        lines.append(f"def {case.name}():")
        _emit_body(lines, case, "    ", hits)
    assert len(hits) <= _MAX_LIST
    return "\n".join(lines), hits


def _render_unittest(cases: list[_TestCase], randomized_import: bool) -> tuple[str, list[dict[str, object]]]:
    lines = ["import unittest"]
    if randomized_import:
        lines.append("import random")
    lines.extend(["", *_HELPERS, "class GeneratedTests(unittest.TestCase):"])
    hits: list[dict[str, object]] = []
    for case in cases:
        for shape in case.shapes:
            lines.extend(f"    {decorator}" for decorator in shape.decorators)
        lines.append(f"    def {case.name}(self):")
        _emit_body(lines, case, "        ", hits)
    assert len(hits) <= _MAX_LIST
    return "\n".join(lines), hits


def _source(path: str, text: str, origin: str = "file", complete: bool = True) -> TestSource:
    assert path.endswith((".py", ".js")) and text
    return TestSource(path, origin, list(enumerate(text.splitlines(), 1)), complete)


def _ratios(counts: Counts) -> dict[str, float | None]:
    asserts = counts.assertions
    return {
        "literal_eq_share": round(counts.literal_eq_assertions / asserts, 3) if asserts else None,
        "negative_share": round(counts.negative_assertions / asserts, 3) if asserts else None,
        "surface_share": round(counts.surface_assertions / asserts, 3) if asserts else None,
        "surface_only_share": round(counts.surface_only_tests / counts.tests, 3) if counts.tests else None,
        "assertions_per_test": round(asserts / counts.tests, 2) if counts.tests else None,
    }


def _expected_files(counts: Counts, path: str, mode: str, hits: list[dict[str, object]]) -> dict[str, object]:
    assert len(hits) == min(counts.source_text_assertions, _MAX_LIST)
    return {"file": path, "mode": mode, "tests": counts.tests, "assertions": counts.assertions, "source_text_hits": hits}


def _chain_plan(rng: Random) -> list[_Chain]:
    count = rng.randint(4, _MAX_CHAINS)
    chains: list[_Chain] = []
    for index in range(count):
        length = rng.randint(1, 3)
        kinds = [rng.choice(_CHAIN_KINDS) for _ in range(length)]
        if index == 0:
            kinds = ["plain"] * rng.randint(1, 2) + [rng.choice(("assert", "fail"))]
        elif index == 1:
            kinds = ["plain"] * length
        elif index == 2:
            kinds = ["plain"] * (length - 1) + ["fail"]
        assert 1 <= len(kinds) <= 3 and set(kinds) <= set(_CHAIN_KINDS)
        chains.append(_Chain(index, tuple(kinds), rng.random() < 0.25))
    assert chains[0].reaches and chains[0].kinds[0] == "plain"
    assert not chains[1].reaches and "assert" not in chains[2].kinds
    return chains


def _render_chains(rng: Random, chains: list[_Chain]) -> str:
    assert 1 <= len(chains) <= _MAX_CHAINS
    blocks: list[list[str]] = []
    for chain in chains:
        last = len(chain.kinds) - 1
        for position, kind in enumerate(chain.kinds):
            block = [f"def link_{chain.index}_{position}(value):"]
            block.extend(f"    {line}" for line in _CHAIN_BODIES[kind])
            if position < last:
                block.append(f"    link_{chain.index}_{position + 1}(value)")
            elif chain.cycle:
                block.append(f"    link_{chain.index}_0(value)")
            blocks.append(block)
        blocks.append([f"def test_chain_{chain.index}():", f"    link_{chain.index}_0(True)"])
    rng.shuffle(blocks)
    lines = ["import pytest", ""]
    for block in blocks:
        lines.extend([*block, ""])
    return "\n".join(lines)


def _chain_tally(chains: list[_Chain]) -> Counts:
    reaching = sum(chain.reaches for chain in chains)
    assert 0 < reaching < len(chains) <= _MAX_CHAINS
    return Counts(
        files=1,
        tests=len(chains),
        assertions=reaching,
        tests_without_assertion=len(chains) - reaching,
        mode_ast_files=1,
    )


def _grader_text(stem: str, checks: int) -> str:
    assert 0 <= checks <= _MAX_GRADER_CHECKS
    lines = ["THRESHOLD = 0.5", ""]
    for index in range(checks):
        lines.extend([f"def test_check_{index}(feedback):", f"    feedback.append('{stem} {index}')", "    return True", ""])
    calls = ", ".join(f"test_check_{index}(feedback)" for index in range(checks))
    lines.extend(["def grade():", "    feedback = []", f"    passed = [{calls}]"])
    lines.extend(["    return sum(passed) / max(len(passed), 1)", ""])
    return "\n".join(lines)


def _wrapper_text(form: str, stem: str) -> str:
    assert form in _IMPORT_FORMS + _PLAIN_FORMS
    top = {
        "top_from": [f"from {stem} import grade"],
        "alias_import": [f"import {stem} as scorer"],
        "relative_from": [f"from .{stem} import grade"],
        "constant_import": [f"from {stem} import THRESHOLD"],
        "uncalled_import": [f"import {stem}"],
    }.get(form, [])
    body = {
        "inner_from": [f"    from {stem} import grade", "    score = grade()"],
        "top_from": ["    score = grade()"],
        "alias_import": ["    score = scorer.grade()"],
        "relative_from": ["    score = grade()"],
        "constant_import": ["    score = THRESHOLD"],
        "uncalled_import": [f"    score = {stem}.THRESHOLD"],
        "single_assert": ["    score = 1.0"],
    }[form]
    lines = ["import os", *top, "", "def test_grader_validation():", *body]
    lines.extend(["    os.makedirs('/logs/verifier', exist_ok=True)", "    with open('/logs/verifier/reward.txt', 'w') as handle:"])
    lines.extend(["        handle.write(str(score))", "    assert True", ""])
    return "\n".join(lines)


class RigorSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        self.check_ast()
        self.check_regex_fallback()
        self.check_chains()
        self.check_delegation()
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "rigor_step_budget")

    def _require_counts(self, actual: object, expected: Counts, prefix: str) -> None:
        assert isinstance(actual, dict)
        for field in _COUNT_FIELDS:
            value = getattr(expected, field)
            self.sim.require(actual[field] == value, f"{prefix}_{field}")

    def _require_ratios(self, result: dict[str, object], counts: Counts, name: str) -> None:
        self.sim.require(result["ratios"] == _ratios(counts), name)

    def check_ast(self) -> None:
        sim = self.sim
        pytest_cases = _plan(self.rng, _PYTEST_SHAPES)
        unittest_cases = _plan(self.rng, _UNITTEST_SHAPES)
        pytest_random = self.rng.random() < 0.5
        unittest_random = not pytest_random
        pytest_text, pytest_hits = _render_pytest(pytest_cases, pytest_random)
        unittest_text, unittest_hits = _render_unittest(unittest_cases, unittest_random)
        pytest_expected = _tally(pytest_cases, pytest_random)
        unittest_expected = _tally(unittest_cases, unittest_random)
        pytest_source = _source(_PYTEST_PATH, pytest_text)
        unittest_source = _source(_UNITTEST_PATH, unittest_text)

        sim.begin("rigor-pytest", tests=pytest_expected.tests, source_hits=[hit["line"] for hit in pytest_hits])
        pytest_analysed = _py_counts(pytest_source)
        assert pytest_analysed is not None
        self._require_counts(asdict(pytest_analysed[0]), pytest_expected, "rigor_pytest")
        sim.require(pytest_analysed[1] == pytest_hits, "rigor_pytest_source_text_hits")
        sim.begin("rigor-unittest", tests=unittest_expected.tests, source_hits=[hit["line"] for hit in unittest_hits])
        unittest_analysed = _py_counts(unittest_source)
        assert unittest_analysed is not None
        self._require_counts(asdict(unittest_analysed[0]), unittest_expected, "rigor_unittest")
        sim.require(unittest_analysed[1] == unittest_hits, "rigor_unittest_source_text_hits")

        total = Counts()
        total.add(pytest_expected)
        total.add(unittest_expected)
        package = Package(
            self.root,
            files={
                "instruction.md": _INSTRUCTION,
                _PYTEST_PATH: pytest_text,
                _UNITTEST_PATH: unittest_text,
            },
        )
        sources = [pytest_source, unittest_source]
        sim.begin("rigor-totals", tests=total.tests, assertions=total.assertions)
        result = test_suite_rigor(package, sources, None)
        self._require_counts(result["counts"], total, "rigor_total")
        self._require_ratios(result, total, "rigor_ratios")
        sim.require(
            result["test_files"]
            == [
                _expected_files(pytest_expected, _PYTEST_PATH, "ast", pytest_hits),
                _expected_files(unittest_expected, _UNITTEST_PATH, "ast", unittest_hits),
            ],
            "rigor_test_files",
        )
        sim.require(result["fragment_files"] == 0, "rigor_fragment_files_absent")

    def check_regex_fallback(self) -> None:
        sim = self.sim
        broken = _source(_BROKEN_PATH, _BROKEN_TEXT)
        fragment = _source(_FRAGMENT_PATH, _FRAGMENT_TEXT, origin="patch", complete=False)
        blocks = _source(_BLOCKS_PATH, _BLOCKS_TEXT)
        guards = _source(_GUARDS_PATH, _GUARDS_TEXT)
        sim.begin("rigor-regex-fallback", files=4)
        sim.require(_py_counts(broken) is None, "rigor_syntax_error_not_parsed")
        sim.require(_py_counts(fragment) is not None, "rigor_fragment_parses_as_python")
        sim.require(_py_counts(blocks) is None, "rigor_blocks_not_parsed")
        sim.require(_py_counts(guards) is None, "rigor_guards_not_python")
        broken_counts, broken_hits = _regex_counts(broken)
        fragment_counts, fragment_hits = _regex_counts(fragment)
        blocks_counts, blocks_hits = _regex_counts(blocks)
        guards_counts, guards_hits = _regex_counts(guards)
        sim.require(broken_hits == fragment_hits == blocks_hits == guards_hits == [], "rigor_regex_no_source_text_hits")
        broken_expected = Counts(
            files=1,
            tests=1,
            assertions=4,
            negative_assertions=2,
            negative_named_tests=1,
            literal_eq_assertions=1,
            surface_assertions=1,
            mock_assertions=1,
            mode_regex_files=1,
        )
        fragment_expected = Counts(
            files=1,
            tests=1,
            assertions=2,
            negative_assertions=2,
            literal_eq_assertions=1,
            mode_regex_files=1,
        )
        blocks_expected = Counts(
            files=1,
            tests=4,
            assertions=6,
            literal_eq_assertions=1,
            surface_assertions=5,
            surface_only_tests=2,
            mode_regex_files=1,
        )
        guards_expected = Counts(
            files=1,
            tests=3,
            assertions=3,
            literal_eq_assertions=2,
            surface_assertions=4,
            surface_only_tests=1,
            mode_regex_files=1,
        )
        self._require_counts(asdict(broken_counts), broken_expected, "rigor_regex_broken_expected")
        self._require_counts(asdict(fragment_counts), fragment_expected, "rigor_regex_fragment_expected")
        self._require_counts(asdict(blocks_counts), blocks_expected, "rigor_regex_blocks_expected")
        self._require_counts(asdict(guards_counts), guards_expected, "rigor_regex_guards_expected")
        total = Counts()
        for counts in (broken_counts, fragment_counts, blocks_counts, guards_counts):
            total.add(counts)
        package = Package(
            self.root / "fallback",
            files={
                "instruction.md": _INSTRUCTION,
                _BROKEN_PATH: _BROKEN_TEXT,
                _FRAGMENT_PATH: _FRAGMENT_TEXT,
                _BLOCKS_PATH: _BLOCKS_TEXT,
                _GUARDS_PATH: _GUARDS_TEXT,
            },
        )
        result = test_suite_rigor(package, [broken, fragment, blocks, guards], None)
        self._require_counts(result["counts"], total, "rigor_regex_total")
        self._require_ratios(result, total, "rigor_regex_ratios")
        sim.require(total.mode_regex_files == 4, "rigor_regex_mode_files")
        sim.require(total.mode_ast_files == 0, "rigor_regex_no_ast_files")
        sim.require(result["fragment_files"] == 1, "rigor_regex_fragment_files")
        sim.require(
            result["test_files"]
            == [
                _expected_files(broken_counts, _BROKEN_PATH, "regex", []),
                _expected_files(fragment_counts, _FRAGMENT_PATH, "regex", []),
                _expected_files(blocks_counts, _BLOCKS_PATH, "regex", []),
                _expected_files(guards_counts, _GUARDS_PATH, "regex", []),
            ],
            "rigor_regex_test_files",
        )

    def check_chains(self) -> None:
        sim = self.sim
        rng = Random(f"rigor-helper-chains:{sim.seed}")
        chains = _chain_plan(rng)
        expected = _chain_tally(chains)
        source = _source(_CHAIN_PATH, _render_chains(rng, chains))
        sim.note(
            step=sim.step,
            subsystem=sim.subsystem,
            action="rigor-helper-chains",
            chains=["/".join(chain.kinds) + ("/cycle" if chain.cycle else "") for chain in chains],
            without=expected.tests_without_assertion,
        )
        analysed = _py_counts(source)
        sim.require(analysed is not None, "rigor_chain_parses")
        assert analysed is not None
        counts = analysed[0]
        sim.require(
            counts.tests_without_assertion == expected.tests_without_assertion,
            "rigor_chain_tests_without_assertion",
        )
        self._require_counts(asdict(counts), expected, "rigor_chain_counts")

    def _delegation_card(self, name: str, form: str, stem: str, checks: int) -> dict[str, Any]:
        root = self.root / name
        files = {
            "instruction.md": _INSTRUCTION,
            "tests/test.sh": _TEST_SH,
            _WRAPPER_PATH: _wrapper_text(form, stem),
            f"tests/{stem}.py": _grader_text(stem, checks),
        }
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        card = analyze_package(root)
        assert card["task"] == name
        return card

    def check_delegation(self) -> None:
        sim = self.sim
        rng = Random(f"rigor-delegation:{sim.seed}")
        stem = rng.choice(_GRADER_STEMS)
        form, plain_form = rng.choice(_IMPORT_FORMS), rng.choice(_PLAIN_FORMS)
        checks = rng.randint(0, _MAX_GRADER_CHECKS)
        sim.begin("rigor-delegating-wrapper", form=form, stem=stem, checks=checks)
        card = self._delegation_card("delegating", form, stem, checks)
        rigor = card["test_suite_rigor"]
        assert isinstance(rigor, dict) and isinstance(card["vulnerability_hypotheses"], list)
        sim.require(rigor.get("delegated_grader_modules") == [f"tests/{stem}.py"], "rigor_delegated_module_named")
        expected = Counts(files=1, tests=1, assertions=1, mode_ast_files=1)
        self._require_counts(rigor["counts"], expected, "rigor_delegated_counts")
        sim.require(rigor["test_files"] == [_expected_files(expected, _WRAPPER_PATH, "ast", [])], "rigor_delegated_test_files")
        ids = [hypothesis["id"] for hypothesis in card["vulnerability_hypotheses"]]
        sim.require("WC-TINY-SUITE" not in ids, "rigor_delegated_not_tiny")
        sim.require("WC-TEST-WITHOUT-ASSERTION" not in ids, "rigor_delegated_grader_checks_not_tests")
        sim.begin("rigor-plain-single-test", form=plain_form, stem=stem)
        card = self._delegation_card("plain", plain_form, stem, 0)
        rigor = card["test_suite_rigor"]
        assert isinstance(rigor, dict) and isinstance(card["vulnerability_hypotheses"], list)
        sim.require(rigor.get("delegated_grader_modules") == [], "rigor_plain_no_delegated_module")
        plain = Counts(files=2, tests=1, assertions=1, mode_ast_files=2)
        self._require_counts(rigor["counts"], plain, "rigor_plain_counts")
        tiny = [hypothesis for hypothesis in card["vulnerability_hypotheses"] if hypothesis["id"] == "WC-TINY-SUITE"]
        sim.require(len(tiny) == 1, "rigor_plain_tiny_fires")
        sim.require(tiny[0]["score"] == 40, "rigor_plain_tiny_score")
