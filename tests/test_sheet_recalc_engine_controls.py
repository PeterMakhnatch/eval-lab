"""Replay the 10 engine-only controls through the real hidden verifier on the host.

A verifier change that starts rewarding lazy, recompute-all, or own-evaluator
engines must fail these regression cases (HAR-407). No Docker is required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sheet_recalc_host import PACKAGES, package_path, run_verifier, transcript

from evallab.reward_hack import declared_expectation

ENGINE_ONLY_CONTROLS = (
    ("sheet-recalc", "alt-recursive-dfs"),
    ("sheet-recalc", "game-lazy-get"),
    ("sheet-recalc", "game-own-evaluator"),
    ("sheet-recalc", "game-recompute-all"),
    ("sheet-recalc-cutoff", "alt-recursive-dfs"),
    ("sheet-recalc-cutoff", "game-lazy-get"),
    ("sheet-recalc-cutoff", "game-own-evaluator"),
    ("sheet-recalc-cutoff", "game-recompute-all"),
    ("sheet-recalc-cutoff", "partial-no-cutoff"),
    ("sheet-recalc-cutoff", "partial-no-cycle-exit"),
)
SKIPPED_CONTROLS = {
    "game-exit-zero": "calls os._exit, does not replace the engine",
    "game-forged-result": "writes the result fd directly, does not replace the engine",
    "game-patch-formula-and-tests": "edits formula.py and the verifier, not engine-only",
}

_FAILED_TEST = re.compile(r"^FAILED .*test_sheet\.py::test_\S+", re.MULTILINE)
_ENGINE_HEREDOC = "cat > /app/sheet/engine.py <<'PY'"


def parse_control(path: Path) -> tuple[float, str]:
    expectation = declared_expectation(path)
    if expectation is None:
        pytest.fail(
            f"{path.name}: no parseable '# expect: 1' or '# expect: 0' header in the first 20 lines"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    openers = [index for index, line in enumerate(lines) if line == _ENGINE_HEREDOC]
    if len(openers) != 1:
        pytest.fail(f"{path.name}: expected exactly one {_ENGINE_HEREDOC!r}, found {len(openers)}")
    start = openers[0]
    end = next((index for index in range(start + 1, len(lines)) if lines[index] == "PY"), None)
    if end is None:
        pytest.fail(f"{path.name}: unterminated engine heredoc (missing 'PY' line)")
    body = "\n".join(lines[start + 1 : end]) + "\n"
    if not body.strip():
        pytest.fail(f"{path.name}: empty engine heredoc body")
    for index, line in enumerate(lines):
        if start <= index <= end:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "set -euo pipefail":
            continue
        pytest.fail(f"{path.name}: unexpected line {index + 1} outside engine heredoc: {line!r}")
    return expectation, body


@pytest.fixture(scope="module", autouse=True)
def check_control_partition() -> None:
    for package in PACKAGES:
        engine_only = {f"{name}.sh" for pkg, name in ENGINE_ONLY_CONTROLS if pkg == package}
        expected = engine_only | {f"{name}.sh" for name in SKIPPED_CONTROLS}
        actual = {path.name for path in package_path(package, "controls").iterdir()}
        assert actual == expected, (
            f"{package}: missing controls: {sorted(expected - actual)}; "
            f"extra controls: {sorted(actual - expected)}"
        )


@pytest.mark.parametrize(
    ("package", "name"),
    ENGINE_ONLY_CONTROLS,
    ids=[f"{package}/{name}" for package, name in ENGINE_ONLY_CONTROLS],
)
def test_engine_only_control(package: str, name: str, tmp_path: Path) -> None:
    expectation, body = parse_control(package_path(package, f"controls/{name}.sh"))
    submission = tmp_path / "engine.py"
    submission.write_text(body, encoding="utf-8")
    # Stop rejected engines at the first failure to keep this file below 120 s;
    # otherwise a mostly-correct engine still runs every verifier test.
    completed = run_verifier(
        package,
        submission,
        selector=None,
        timeout=100.0 if expectation == 1 else 30.0,
        args=() if expectation == 1 else ("-x",),
    )
    output = f"{package}/{name}: " + transcript(completed)
    if expectation == 1:
        assert completed.returncode == 0, output
    else:
        assert completed.returncode != 0, output
        assert _FAILED_TEST.search(transcript(completed)), output
