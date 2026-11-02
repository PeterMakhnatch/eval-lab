"""Pure helpers for reference-filtered test synthesis (no Modal, no model calls).

The synthesizer writes one unittest file with many independent test methods. The reference solution is run once
with per-test results; methods that fail on the reference are removed from the file, the rest are kept.
"""

from __future__ import annotations

import ast
import re

MIN_KEPT = 3
ASKED = 12

# a unittest -v result header: "test_x (__main__.Class.test_x) ... ok"  (py3.11)  or  "... (__main__.Class)" (py2.7)
_HEADER = re.compile(r"(?m)^(test\w*) \(__main__\.([\w.]+?)\)(?:[^\n]*)$")
_VERDICT = re.compile(r"(?m)(?:\.\.\. |^)(ok|FAIL|ERROR)\s*$")
_MAIN_CALL = re.compile(r"unittest\.main\([^)]*\)")


def prepare_code(code: str) -> str:
    """Force verbose unittest output so the grader log carries one verdict line per test."""
    if _MAIN_CALL.search(code):
        return _MAIN_CALL.sub("unittest.main(verbosity=2)", code)
    return code.rstrip("\n") + "\n\n\nif __name__ == '__main__':\n    unittest.main(verbosity=2)\n"


def list_test_methods(code: str) -> list[tuple[str, int, int]]:
    """``(name, first_line, last_line)`` of every ``test*`` method of a TestCase class, decorators included.

    Raises SyntaxError when the file does not parse under Python 3.
    """
    tree = ast.parse(code)
    out: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test"):
                    first = min([item.lineno, *[d.lineno for d in item.decorator_list]])
                    out.append((item.name, first, item.end_lineno or item.lineno))
    return out


def parse_verdicts(log: str) -> dict[str, str]:
    """Per-test verdict (``ok``/``FAIL``/``ERROR``) from the contract test's ``unittest -v`` output.

    Only ``__main__`` tests are read, so a hidden unittest suite run earlier in the same log is ignored. A test
    whose verdict line cannot be read is omitted (the caller treats it as not kept).
    """
    headers = list(_HEADER.finditer(log))
    verdicts: dict[str, str] = {}
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(log)
        segment = log[h.start() : end]
        # a verdict appears on the header line or on a later line (test printed output in between)
        found = _VERDICT.search(segment)
        if found:
            verdicts[h.group(1)] = found.group(1)
    return verdicts


def drop_tests(code: str, drop: set[str]) -> str:
    """Remove the named test methods (by line span, so the rest of the file stays byte-identical)."""
    spans = sorted((first, last) for name, first, last in list_test_methods(code) if name in drop)
    lines = code.splitlines(keepends=True)
    keep = [True] * len(lines)
    for first, last in spans:
        for n in range(first - 1, last):
            keep[n] = False
    return "".join(line for line, k in zip(lines, keep, strict=True) if k)


def failure_blocks(log: str, names: set[str], limit_chars: int = 1200) -> str:
    """The ``ERROR:``/``FAIL:`` blocks of the named tests, each cut to ``limit_chars`` (for the repair prompt)."""
    blocks = re.split(r"\n={60,}\n", log)
    shown: list[str] = []
    for block in blocks:
        m = re.match(r"(?:ERROR|FAIL): (\w+) ", block)
        if m and m.group(1) in names:
            shown.append(block.strip()[-limit_chars:])
    return "\n\n".join(shown)


def build_filtered_prompt(
    *,
    instruction: str,
    hidden_tests: str,
    reference_patch: str,
    train_diffs: list[str],
    survivor_diffs: list[str] | None,
    interpreter_command: str,
    python_version: str,
    test_filename: str,
    previous_test: str | None = None,
    asked: int = ASKED,
) -> str:
    """Prompt asking for ``asked`` independent test methods that each assert only specified behavior."""
    parts = [
        "You are writing a supplementary unittest suite for a software task grader. The suite is run only after "
        "the grader's own hidden tests pass, against a candidate solution, to catch candidates that pass the "
        "hidden tests but are wrong.\n\n## Task statement (what the candidate was asked to do)\n",
        instruction.strip(),
        "\n\n## The grader's existing hidden tests\n",
        hidden_tests.strip(),
        "\n\n## Reference solution (visible diff of one correct implementation)\n",
        reference_patch.strip(),
    ]
    if previous_test and previous_test.strip():
        parts += ["\n\n## Your previous test file (an earlier round); keep what it checks, add what it missed\n",
                  previous_test.strip()]
    if survivor_diffs:
        parts += [
            "\n\n## Mutants your previous file still ACCEPTED\nThey are variants of the reference with a behavior "
            "change. Add tests whose assertions follow from the task statement or the reference diff and that "
            "fail on these variants:\n\n",
            "\n\n".join(survivor_diffs),
        ]
    if train_diffs:
        parts += [
            "\n\n## Sample behavior-changing variants of the reference\nA good suite fails on variants like these, "
            "but only by asserting behavior that the task statement or the reference diff specifies:\n\n",
            "\n\n".join(train_diffs),
        ]
    py = "Python 2.7 and Python 3 (no f-strings, no print statement, no nonlocal)" if python_version == "2.7" else "Python 3"
    parts += [
        "\n\n## Output requirements\n",
        f"Write ONE Python `unittest` file, saved as `/tests/{test_filename}` and run as "
        f"`cd /testbed && {interpreter_command} /tests/{test_filename}`; the repository is checked out in the "
        f"working directory of that command. Compatible with {py}. Start with `import sys; sys.path.insert(0, "
        "'/testbed')` and end with `if __name__ == '__main__': unittest.main(verbosity=2)`.\n\n"
        f"Write EXACTLY {asked} independent test methods (`test_*`) in one or more `unittest.TestCase` classes. "
        "Each method must stand alone: no shared mutable state, no dependence on another test, no required order.\n\n"
        "Each method must assert ONLY behavior that the task statement or the visible reference diff specifies. Rules:\n"
        "1. Inputs inside the documented contract; never call code with arguments the statement does not allow.\n"
        "2. Public API only: no private helpers (leading underscore), no source inspection (no `inspect`, `ast`, "
        "reading `.py` files), minimal mocking.\n"
        "3. No exact error text, timing, repr/formatting, ordering or default values unless the statement specifies "
        "them. Prefer asserting return values, state changes, and exception TYPES that the statement names.\n"
        "4. Deterministic, no network, whole file under 60 seconds.\n"
        "5. Each method SHOULD target a different behavior or branch the reference diff introduces, so that "
        "together they cover the change.\n"
        "Methods that fail on the reference solution are discarded automatically, so do not guess: when unsure "
        "what the reference does, assert less.\n"
        "Output exactly ONE fenced ```python``` block with the complete file and nothing else.\n",
    ]
    return "".join(parts)


def repair_section(previous_code: str, failures: str, *, asked: int = ASKED) -> str:
    """Appended to the round prompt for the single repair call: only the failing tests' messages are shown."""
    return (
        "\n\n## Repair: some of your methods failed on the reference solution and were discarded\n"
        "Your previous file:\n\n```python\n" + previous_code.strip() + "\n```\n\n"
        "Failure messages of the discarded methods only:\n\n" + failures.strip() +
        f"\n\nOutput the COMPLETE file again with {asked} independent methods: keep the passing ones unchanged "
        "and replace each failed one with a different test that asserts only behavior the statement or "
        "the reference diff specifies. One fenced ```python``` block, nothing else.\n"
    )
