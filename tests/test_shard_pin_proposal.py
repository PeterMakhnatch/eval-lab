"""Contract tests for scripts/ci/propose_shard_pins.py (HAR-308)."""

from __future__ import annotations

import re
import subprocess
import sys
import zlib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/propose_shard_pins.py"


def _hash_shard(path: str, total: int = 2) -> int:
    return zlib.crc32(path.encode("utf-8")) % total + 1


def _names(shard: int, prefix: str = "tests/test_case") -> Iterator[str]:
    """Yield test file paths whose crc32 rule lands on ``shard`` of 2."""
    index = 0
    while True:
        path = f"{prefix}_{index}.py"
        if _hash_shard(path) == shard:
            yield path
        index += 1


def _classname(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def _write_report(target: Path, seconds: Mapping[str, float]) -> Path:
    cases = "".join(
        f'<testcase classname="{_classname(path)}.TestCase" name="test_one" time="{value / 2}"/>'
        f'<testcase classname="{_classname(path)}" name="test_two" time="{value / 2}"/>'
        for path, value in seconds.items()
    )
    target.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    return target


def _run(
    root: Path,
    reports: Sequence[Mapping[str, float]],
    *,
    pins: Mapping[str, int] | None = None,
    missing: Sequence[str] = (),
) -> tuple[str, dict[str, float], dict[str, str]]:
    """Lay out a repository under ``root`` and run the script over ``reports``.

    Every reported file exists except those in ``missing``. Returns stdout,
    ``GAP`` seconds per layout and ``PIN current`` verdicts per file.
    """
    for report in reports:
        for path in report:
            if path not in missing:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text("", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests/conftest.py").write_text(
        f"SHARD_OVERRIDES: dict[str, int] = {dict(pins or {})!r}\n", encoding="utf-8"
    )
    paths = [
        str(_write_report(root / f"report-{index}.xml", report))
        for index, report in enumerate(reports)
    ]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--total", "2", "--root", str(root), *paths],
        text=True,
        capture_output=True,
        check=True,
    )
    gaps = {
        match["layout"]: float(match["seconds"])
        for match in re.finditer(
            r"^GAP layout=(?P<layout>\S+) seconds=(?P<seconds>\S+)$", result.stdout, re.MULTILINE
        )
    }
    verdicts = dict(re.findall(r"^PIN current file=(\S+) .* verdict=(\S+)$", result.stdout, re.M))
    return result.stdout, gaps, verdicts


def _proposed(stdout: str) -> dict[str, int]:
    return {
        path: int(shard)
        for path, shard in re.findall(r"^PIN proposed file=(\S+) shard=(\d)", stdout, re.M)
    }


def _shard_seconds(stdout: str, layout: str) -> list[float]:
    return [
        float(value)
        for value in re.findall(rf"^SHARD layout={layout} shard=\d seconds=(\S+)", stdout, re.M)
    ]


def test_balanced_input_proposes_no_pins_and_averages_reports(tmp_path: Path) -> None:
    one = next(_names(1, "tests/line/test_nested"))
    two = next(_names(2))
    # Each file's seconds are averaged over the reports containing it (3.12 and 3.14).
    stdout, gaps, verdicts = _run(tmp_path, [{one: 10.0, two: 14.0}, {one: 30.0, two: 26.0}])

    assert _shard_seconds(stdout, "crc32") == [20.0, 20.0]
    assert gaps == {"crc32": 0.0, "current": 0.0, "proposed": 0.0}
    assert verdicts == {}
    assert _proposed(stdout) == {}
    assert "SHARD_OVERRIDES: dict[str, int] = {\n}" in stdout


def test_one_file_move_pins_the_file_that_closes_the_gap(tmp_path: Path) -> None:
    ones, twos = _names(1), _names(2)
    a = next(ones)
    b, c, d = next(twos), next(twos), next(twos)
    stdout, gaps, _ = _run(tmp_path, [{a: 100.0, b: 110.0, c: 30.0, d: 20.0}])

    assert gaps["current"] == 60.0
    assert _proposed(stdout) == {c: 1}
    assert _shard_seconds(stdout, "proposed") == [130.0, 130.0]
    assert gaps["proposed"] == 0.0
    assert f'    "{c}": 1,' in stdout


def test_harmful_existing_pin_is_dropped_and_no_op_pin_is_not_proposed(tmp_path: Path) -> None:
    ones, twos = _names(1), _names(2)
    a, x, noop = next(ones), next(ones), next(ones)
    b = next(twos)
    stdout, gaps, verdicts = _run(
        tmp_path,
        [{a: 50.0, x: 40.0, noop: 1.0, b: 91.0}],
        pins={x: 2, noop: 1},
    )

    assert verdicts == {x: "harmful", noop: "no-op"}
    assert gaps["current"] == 80.0
    assert _proposed(stdout) == {}
    assert gaps["proposed"] == 0.0


def test_unresolvable_name_is_reported_and_excluded(tmp_path: Path) -> None:
    one, two = next(_names(1)), next(_names(2))
    gone = next(_names(2, "tests/test_deleted"))
    stdout, gaps, _ = _run(tmp_path, [{one: 20.0, two: 20.0, gone: 500.0}], missing=[gone])

    assert f"UNRESOLVED file={gone} seconds=500.00" in stdout
    assert "DURATIONS files=2 unresolved=1" in stdout
    assert gaps == {"crc32": 0.0, "current": 0.0, "proposed": 0.0}
    assert gone not in _proposed(stdout)
