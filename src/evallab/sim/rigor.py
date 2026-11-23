from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random

from evallab.package_anatomy import (
    Counts,
    Package,
    TestSource,
    _py_counts,
    _regex_counts,
    test_suite_rigor,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_MAX_TESTS = 12
_PYTEST_PATH = "tests/test_generated.py"
_UNITTEST_PATH = "tests/test_generated_unittest.py"
_BROKEN_PATH = "tests/test_broken_syntax.py"
_FRAGMENT_PATH = "tests/test_patch_fragment.py"
_INSTRUCTION = "Run the generated checks and report every failure.\n"
_COUNT_FIELDS = (
    "files",
    "tests",
    "assertions",
    "negative_assertions",
    "negative_named_tests",
    "literal_eq_assertions",
    "surface_assertions",
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


@dataclass(frozen=True, slots=True)
class _Shape:
    name: str
    delta: Counts
    decorators: tuple[str, ...] = ()
    body: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _TestCase:
    name: str
    shapes: tuple[_Shape, ...]
    negative_name: bool


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


def _tally(cases: list[_TestCase], randomized_import: bool) -> Counts:
    counts = Counts(files=1, tests=len(cases), mode_ast_files=1)
    if randomized_import:
        counts.randomized_or_property += 1
    for case in cases:
        counts.negative_named_tests += int(case.negative_name)
        for shape in case.shapes:
            counts.add(shape.delta)
    assert counts.files == 1 and counts.mode_ast_files == 1
    assert counts.tests == len(cases) <= _MAX_TESTS
    assert counts.negative_assertions > 0 and counts.literal_eq_assertions > 0
    assert counts.surface_assertions > 0 and counts.mock_assertions > 0
    assert counts.source_text_assertions > 0 and counts.loops_with_assertions > 0
    assert counts.skipped_or_xfail > 0 and counts.tests_without_assertion > 0
    assert counts.negative_named_tests > 0
    return counts


def _render_pytest(cases: list[_TestCase], randomized_import: bool) -> str:
    lines = ["import pytest"]
    if randomized_import:
        lines.append("import random")
    lines.extend(["", "def check(value):", "    assert value", ""])
    for case in cases:
        assert case.name and case.shapes
        for shape in case.shapes:
            lines.extend(shape.decorators)
        lines.append(f"def {case.name}():")
        body = [line for shape in case.shapes for line in shape.body]
        assert body
        lines.extend(f"    {line}" for line in body)
        lines.append("")
    return "\n".join(lines)


def _render_unittest(cases: list[_TestCase], randomized_import: bool) -> str:
    lines = ["import unittest"]
    if randomized_import:
        lines.append("import random")
    lines.extend(
        ["", "def check(value):", "    assert value", "", "class GeneratedTests(unittest.TestCase):"]
    )
    for case in cases:
        assert case.name and case.shapes
        for shape in case.shapes:
            lines.extend(f"    {decorator}" for decorator in shape.decorators)
        lines.append(f"    def {case.name}(self):")
        body = [line for shape in case.shapes for line in shape.body]
        assert body
        lines.extend(f"        {line}" for line in body)
        lines.append("")
    return "\n".join(lines)


def _source(path: str, text: str, origin: str = "file", complete: bool = True) -> TestSource:
    assert path.endswith(".py") and text
    return TestSource(path, origin, list(enumerate(text.splitlines(), 1)), complete)


def _ratios(counts: Counts) -> dict[str, float | None]:
    asserts = counts.assertions
    return {
        "literal_eq_share": round(counts.literal_eq_assertions / asserts, 3) if asserts else None,
        "negative_share": round(counts.negative_assertions / asserts, 3) if asserts else None,
        "surface_share": round(counts.surface_assertions / asserts, 3) if asserts else None,
        "assertions_per_test": round(asserts / counts.tests, 2) if counts.tests else None,
    }


def _expected_files(counts: Counts, path: str, mode: str) -> dict[str, object]:
    return {"file": path, "mode": mode, "tests": counts.tests, "assertions": counts.assertions}


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
        pytest_text = _render_pytest(pytest_cases, pytest_random)
        unittest_text = _render_unittest(unittest_cases, unittest_random)
        pytest_expected = _tally(pytest_cases, pytest_random)
        unittest_expected = _tally(unittest_cases, unittest_random)
        pytest_source = _source(_PYTEST_PATH, pytest_text)
        unittest_source = _source(_UNITTEST_PATH, unittest_text)

        sim.begin("rigor-pytest", tests=pytest_expected.tests)
        pytest_counts = _py_counts(pytest_source)
        assert pytest_counts is not None
        self._require_counts(asdict(pytest_counts), pytest_expected, "rigor_pytest")
        sim.begin("rigor-unittest", tests=unittest_expected.tests)
        unittest_counts = _py_counts(unittest_source)
        assert unittest_counts is not None
        self._require_counts(asdict(unittest_counts), unittest_expected, "rigor_unittest")

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
                _expected_files(pytest_expected, _PYTEST_PATH, "ast"),
                _expected_files(unittest_expected, _UNITTEST_PATH, "ast"),
            ],
            "rigor_test_files",
        )
        sim.require(result["fragment_files"] == 0, "rigor_fragment_files_absent")

    def check_regex_fallback(self) -> None:
        sim = self.sim
        broken = _source(_BROKEN_PATH, _BROKEN_TEXT)
        fragment = _source(_FRAGMENT_PATH, _FRAGMENT_TEXT, origin="patch", complete=False)
        sim.begin("rigor-regex-fallback", files=2)
        sim.require(_py_counts(broken) is None, "rigor_syntax_error_not_parsed")
        sim.require(_py_counts(fragment) is not None, "rigor_fragment_parses_as_python")
        broken_counts = _regex_counts(broken)
        fragment_counts = _regex_counts(fragment)
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
        self._require_counts(asdict(broken_counts), broken_expected, "rigor_regex_broken_expected")
        self._require_counts(asdict(fragment_counts), fragment_expected, "rigor_regex_fragment_expected")
        total = Counts()
        for counts in (broken_counts, fragment_counts):
            total.add(counts)
        package = Package(
            self.root / "fallback",
            files={
                "instruction.md": _INSTRUCTION,
                _BROKEN_PATH: _BROKEN_TEXT,
                _FRAGMENT_PATH: _FRAGMENT_TEXT,
            },
        )
        result = test_suite_rigor(package, [broken, fragment], None)
        self._require_counts(result["counts"], total, "rigor_regex_total")
        self._require_ratios(result, total, "rigor_regex_ratios")
        sim.require(total.mode_regex_files == 2, "rigor_regex_mode_files")
        sim.require(total.mode_ast_files == 0, "rigor_regex_no_ast_files")
        sim.require(result["fragment_files"] == 1, "rigor_regex_fragment_files")
        sim.require(
            result["test_files"]
            == [
                _expected_files(broken_counts, _BROKEN_PATH, "regex"),
                _expected_files(fragment_counts, _FRAGMENT_PATH, "regex"),
            ],
            "rigor_regex_test_files",
        )
