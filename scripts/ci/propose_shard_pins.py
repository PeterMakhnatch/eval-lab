#!/usr/bin/env python3
"""Propose ``SHARD_OVERRIDES`` pins that balance CI test shards from JUnit durations.

Reads one or more pytest JUnit reports (for example the four ``full-suite.xml``
files of one CI run: two Pythons times two shards), averages each test file's
summed seconds over the reports that contain it, and prints:

* per-shard serial seconds under the plain crc32 rule and under the current
  pins in ``tests/conftest.py``;
* a verdict for every current pin (``keep``, ``no-op``, ``harmful``,
  ``unmeasured``);
* the proposed whole-file pin set that minimizes the gap between the heaviest
  and the lightest shard, with the resulting per-shard sums and a ready-to-paste
  ``SHARD_OVERRIDES`` block.

The shard rule mirrors ``pytest_collection_modifyitems`` in ``tests/conftest.py``:
``crc32(rel_path) % total + 1`` unless a pin overrides it, and
``tests/test_ci_coverage.py`` runs on every shard. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EVERY_SHARD_FILE = "tests/test_ci_coverage.py"
# 3.12 and 3.14 per-file sums of one run differ by tens of seconds, so a new pin
# must shrink the averaged gap by more than this to earn its maintenance.
DEFAULT_TOLERANCE_SECONDS = 10.0
_MAX_ROUNDS = 100


def test_file(classname: str) -> str:
    """Return the test module path a JUnit ``classname`` belongs to.

    Same mapping as ``scripts/ci/report_slowest_files.py`` (HAR-305):
    ``tests.test_x`` and ``tests.test_x.TestY`` both map to ``tests/test_x.py``;
    the module is the first ``test_*`` segment.
    """
    parts = classname.split(".")
    for index, part in enumerate(parts):
        if part.startswith("test_"):
            return "/".join(parts[: index + 1]) + ".py"
    while len(parts) > 1 and parts[-1][:1].isupper():
        parts.pop()
    return "/".join(parts) + ".py" if classname else "<no classname>"


def hash_shard(path: str, total: int) -> int:
    return zlib.crc32(path.encode("utf-8")) % total + 1


@dataclass(frozen=True)
class Durations:
    seconds: dict[str, float]
    """Average summed seconds per resolvable test file."""
    unresolved: dict[str, float]
    """Average seconds per mapped path that does not exist under the root."""


def load_durations(reports: Sequence[Path], root: Path) -> Durations:
    """Average each file's summed testcase seconds over the reports containing it."""
    totals: dict[str, float] = {}
    appearances: dict[str, int] = {}
    for report in reports:
        per_file: dict[str, float] = {}
        for testcase in ET.parse(report).getroot().iter("testcase"):
            path = test_file(testcase.get("classname", ""))
            per_file[path] = per_file.get(path, 0.0) + float(testcase.get("time") or 0.0)
        for path, seconds in per_file.items():
            totals[path] = totals.get(path, 0.0) + seconds
            appearances[path] = appearances.get(path, 0) + 1
    seconds: dict[str, float] = {}
    unresolved: dict[str, float] = {}
    for path, total in totals.items():
        average = total / appearances[path]
        (seconds if (root / path).is_file() else unresolved)[path] = average
    return Durations(seconds=seconds, unresolved=unresolved)


def read_current_pins(conftest: Path) -> dict[str, int]:
    """Return the literal ``SHARD_OVERRIDES`` mapping assigned in ``conftest``."""
    for node in ast.parse(conftest.read_text(encoding="utf-8")).body:
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "SHARD_OVERRIDES":
            assert node.value is not None  # type: ignore[union-attr]
            pins = ast.literal_eval(node.value)  # type: ignore[union-attr]
            return {str(path): int(shard) for path, shard in pins.items()}
    raise SystemExit(f"{conftest}: no SHARD_OVERRIDES assignment found")


def shard_of(path: str, pins: Mapping[str, int], total: int) -> int:
    """The shard a file runs on; same precedence as the conftest hook."""
    pin = pins.get(path)
    if pin is not None and pin <= total:
        return pin
    return hash_shard(path, total)


def shard_sums(seconds: Mapping[str, float], pins: Mapping[str, int], total: int) -> list[float]:
    sums = [0.0] * total
    for path, value in seconds.items():
        if path == EVERY_SHARD_FILE:
            sums = [current + value for current in sums]
        else:
            sums[shard_of(path, pins, total) - 1] += value
    return sums


def gap(sums: Sequence[float]) -> float:
    return max(sums) - min(sums)


def effective_pins(assignment: Mapping[str, int], total: int) -> dict[str, int]:
    """Only pins that flip the crc32 shard; a pin equal to the hash shard is a no-op."""
    return {
        path: shard
        for path, shard in sorted(assignment.items())
        if path != EVERY_SHARD_FILE and shard != hash_shard(path, total)
    }


def pin_verdicts(
    seconds: Mapping[str, float], pins: Mapping[str, int], total: int
) -> dict[str, str]:
    """Judge each current pin against the current layout without it."""
    verdicts: dict[str, str] = {}
    current_gap = gap(shard_sums(seconds, pins, total))
    for path, shard in sorted(pins.items()):
        if shard > total or shard == hash_shard(path, total):
            verdicts[path] = "no-op"
        elif path not in seconds:
            verdicts[path] = "unmeasured"
        else:
            without = {other: pin for other, pin in pins.items() if other != path}
            harmful = gap(shard_sums(seconds, without, total)) < current_gap
            verdicts[path] = "harmful" if harmful else "keep"
    return verdicts


def _best_move(
    seconds: Mapping[str, float], assignment: dict[str, int], sums: list[float], tolerance: float
) -> tuple[float, list[tuple[str, int]]]:
    """Best single move, or a swap beating it by more than ``tolerance``, heaviest to lightest."""
    heavy = max(range(len(sums)), key=lambda i: (sums[i], -i)) + 1
    light = min(range(len(sums)), key=lambda i: (sums[i], i)) + 1
    others = [value for index, value in enumerate(sums, 1) if index not in (heavy, light)]

    def gap_after(delta: float) -> float:
        return gap([sums[heavy - 1] - delta, sums[light - 1] + delta, *others])

    on_heavy = sorted(path for path, shard in assignment.items() if shard == heavy)
    on_light = sorted(path for path, shard in assignment.items() if shard == light)
    best_gap = gap(sums)
    best: list[tuple[str, int]] = []
    for path in on_heavy:
        candidate = gap_after(seconds[path])
        if candidate < best_gap:
            best_gap, best = candidate, [(path, light)]
    single_gap = best_gap
    for path in on_heavy:
        for other in on_light:
            delta = seconds[path] - seconds[other]
            if delta <= 0:
                continue
            candidate = gap_after(delta)
            # A swap costs up to two pins: take it only when it clearly beats every single move.
            if candidate < best_gap and candidate < single_gap - tolerance:
                best_gap, best = candidate, [(path, light), (other, heavy)]
    return best_gap, best


def propose(
    seconds: Mapping[str, float],
    pins: Mapping[str, int],
    total: int,
    *,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
) -> dict[str, int]:
    """Return the effective pin set after dropping no-op and harmful pins and rebalancing.

    Starting from crc32 plus the kept current pins, repeatedly apply the single
    file move (or a two-file swap that beats it by more than ``tolerance``)
    between the heaviest and the lightest shard while the move shrinks the gap
    by more than ``tolerance``.
    """
    verdicts = pin_verdicts(seconds, pins, total)
    kept = {path: shard for path, shard in pins.items() if verdicts[path] in ("keep", "unmeasured")}
    balanced = {path: value for path, value in seconds.items() if path != EVERY_SHARD_FILE}
    assignment = {path: shard_of(path, kept, total) for path in balanced}
    for _ in range(_MAX_ROUNDS):
        sums = shard_sums(balanced, assignment, total)
        candidate_gap, moves = _best_move(balanced, assignment, sums, tolerance)
        if not moves or gap(sums) - candidate_gap <= tolerance:
            break
        for path, shard in moves:
            assignment[path] = shard
    proposal = effective_pins(assignment, total)
    proposal.update((path, shard) for path, shard in kept.items() if verdicts[path] == "unmeasured")
    return dict(sorted(proposal.items()))


def _shard_lines(layout: str, sums: Sequence[float], files: Mapping[int, int]) -> Iterable[str]:
    for index, value in enumerate(sums, 1):
        yield f"SHARD layout={layout} shard={index} seconds={value:.2f} files={files[index]}"
    yield f"GAP layout={layout} seconds={gap(sums):.2f}"


def _file_counts(
    seconds: Mapping[str, float], pins: Mapping[str, int], total: int
) -> dict[int, int]:
    counts = dict.fromkeys(range(1, total + 1), 0)
    for path in seconds:
        if path == EVERY_SHARD_FILE:
            for index in counts:
                counts[index] += 1
        else:
            counts[shard_of(path, pins, total)] += 1
    return counts


def render(
    durations: Durations,
    pins: Mapping[str, int],
    total: int,
    *,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
) -> list[str]:
    seconds = durations.seconds
    lines = [f"DURATIONS files={len(seconds)} unresolved={len(durations.unresolved)}"]
    lines += [
        f"UNRESOLVED file={path} seconds={value:.2f} (excluded: no such file under the root)"
        for path, value in sorted(durations.unresolved.items())
    ]
    layouts = {"crc32": {}, "current": dict(pins)}
    for layout, layout_pins in layouts.items():
        sums = shard_sums(seconds, layout_pins, total)
        lines += _shard_lines(layout, sums, _file_counts(seconds, layout_pins, total))
    for path, verdict in pin_verdicts(seconds, pins, total).items():
        lines.append(
            f"PIN current file={path} shard={pins[path]} hash={hash_shard(path, total)} "
            f"seconds={seconds.get(path, 0.0):.2f} verdict={verdict}"
        )
    proposal = propose(seconds, pins, total, tolerance=tolerance)
    for path, shard in proposal.items():
        lines.append(
            f"PIN proposed file={path} shard={shard} hash={hash_shard(path, total)} "
            f"seconds={seconds.get(path, 0.0):.2f}"
        )
    sums = shard_sums(seconds, proposal, total)
    lines += _shard_lines("proposed", sums, _file_counts(seconds, proposal, total))
    lines.append("SHARD_OVERRIDES: dict[str, int] = {")
    lines += [f'    "{path}": {shard},' for path, shard in proposal.items()]
    lines.append("}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="pytest JUnit XML reports")
    parser.add_argument("--total", type=int, required=True, help="number of CI test shards")
    parser.add_argument(
        "--root", type=Path, default=Path(), help="repository root that test paths resolve under"
    )
    parser.add_argument(
        "--conftest",
        type=Path,
        default=None,
        help="conftest holding SHARD_OVERRIDES (default: ROOT/tests/conftest.py)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_SECONDS,
        help="minimum gap reduction in seconds that justifies another pin",
    )
    args = parser.parse_args(argv)
    if args.total < 1:
        parser.error("--total must be at least 1")
    conftest = args.conftest or args.root / "tests" / "conftest.py"
    durations = load_durations(args.reports, args.root)
    pins = read_current_pins(conftest)
    print("\n".join(render(durations, pins, args.total, tolerance=args.tolerance)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
