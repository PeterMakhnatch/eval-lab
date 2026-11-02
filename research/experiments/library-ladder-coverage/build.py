"""Library cheat-ladder coverage matrix (HAR-243).

One row per ``library/tasks`` package (including ``experimental/``) and cheat
ladder generation, built read-only from committed evidence. The matrix answers
the question every lane re-derived by hand: which ladder generations ran on
which package bytes, what they found, and whether those bytes are still the
package's bytes today.

Sources (all committed, nothing is executed):

* ``library-crack-sweep-20261012`` -- the frozen v1 generation (``1.4.0``;
  ``evallab.cheat_ladder.CHEAT_AGENT_VERSION`` and the v2 sweep README both
  date it before 2026-10-13) on the shipped packages.
* ``ladder-v2-sweep-20261013`` -- ladder ``2.0.0`` on every runnable package.
* ``har230-transaction-reconciliation-20261029`` -- ladders ``1.4.0`` and
  ``2.0.0`` plus single-attack cells on the hardened shipped package.
* ``ladder-v3-sheet-recalc-20261029`` -- ladder ``3.0.0`` on the two
  hand-authored packages.
* every other ``research/evidence/ladder-v*/cells.json`` -- one directory per
  ladder card in the 2026-11-02 layout (``task_dir`` at the top level or on
  each ``cells``/``controls`` record of a multi-package ledger; ``cells``
  records with ``scope``, ``ladder_version``, ``verdict``, ``status`` and
  ``task_digest``), read by glob so a new card's evidence joins the matrix
  without editing this file. A ledger in the glob that no reader can place is
  refused rather than skipped.

The row's ``verdict`` comes from the newest source date for that package and
generation; an older, different verdict is reported in
``superseded_verdicts``. Only Harbor cells count: the crack sweep's Inspect
cells ignore ``environment_mode = "separate"`` and therefore measure the
Inspect platform, not the package. A crack-sweep or v2 cell counts only when
its own Harbor oracle/nop controls passed/failed, and a HAR-230 cell only when
that receipt's native oracle/nop controls were 1.0/0.0. Derived task-store
variants, the crack sweep's ``fixed``/``hardened`` transforms, and HAR-231's
``git_armed_variant`` cells ran on bytes that are not the shipped package, so
they are excluded.

``task_digest`` is the digest the ladder recorded (Harbor's local-package
digest; the recorded values reproduce with
``evallab.registry.harbor_task_digest``). Both digest columns describe the
cells behind ``verdict`` (the newest source date). ``digest_current`` is
``yes`` only when every digest recorded there equals that helper's value for
the package today. ``digest_recorded`` says whether any digest was recorded at
all: a row with ``digest_recorded=no`` ran on unrecorded bytes, which is not
the same claim as "ran on stale bytes" (``digest_recorded=yes,
digest_current=no``).

Reproduce (repo root, locked environment):

    uv run python research/experiments/library-ladder-coverage/build.py --check

``--check`` also compares ``gaps.csv``, the derived gap list: one row per
``verdict=none`` coverage cell, with a mechanical ``reason`` (``no-cells``,
``errored``, ``stale-digest``) and ``superseded_by`` naming the highest later
ladder version that holds a non-none verdict on the package's current bytes.
Superseded cells are not real gaps; the rows with empty ``superseded_by`` are
the ladder work that remains.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import sys
import tomllib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

TASKS_ROOT = "library/tasks"
LADDER_VERSIONS = ("1.4.0", "2.0.0", "3.0.0", "4.0.0")
COVERAGE_NAME = "coverage.csv"
GAPS_NAME = "gaps.csv"

CRACK_SWEEP = "research/evidence/library-crack-sweep-20261012/cells.json"
V2_SWEEP = "research/evidence/ladder-v2-sweep-20261013/cells.json"
HAR230_DIR = "research/evidence/har230-transaction-reconciliation-20261029"
HAR231_CELLS = "research/evidence/ladder-v3-sheet-recalc-20261029/cells.json"

#: One directory per committed ladder run; the generic ledger reader takes every
#: file here that the dedicated readers above do not already own.
LADDER_LEDGER_GLOB = "research/evidence/ladder-v*/cells.json"

#: Ledgers predating the 2026-11-02 layout, read by their own readers above.
DEDICATED_LEDGERS = (V2_SWEEP, HAR231_CELLS)

#: ``scope`` values that ran the shipped package bytes; a variant scope would be
#: a different measurement and must be admitted deliberately.
LEDGER_SCOPES = ("full", "single")

#: The frozen generation the 2026-10-12 sweep ran; v2 was introduced 2026-10-13.
CRACK_SWEEP_LADDER = "1.4.0"

COLUMNS = (
    "package",
    "task",
    "ladder_version",
    "verdict",
    "n_cells",
    "evidence_path",
    "task_digest",
    "digest_current",
    "digest_recorded",
    "superseded_verdicts",
)

GAP_COLUMNS = (
    "package",
    "ladder_version",
    "reason",
    "superseded_by",
)


class CoverageError(RuntimeError):
    """An input no longer matches the contract this builder reads."""


@dataclass(frozen=True)
class Package:
    """One ``library/tasks`` package: its repo path and Harbor task name."""

    path: str
    task: str


@dataclass(frozen=True)
class Cell:
    """One ladder measurement on a package at a ladder generation."""

    package: str
    ladder_version: str
    verdict: str | None
    task_digest: str | None
    source: str
    date: str
    supporting: tuple[str, ...] = field(default_factory=tuple)


def discover_packages(root: Path = ROOT) -> list[Package]:
    """Every directory under ``library/tasks`` that holds a ``task.toml``.

    Depth is not fixed: ``experimental/deepplanning-v1`` nests its package
    level deeper than the rest.
    """
    base = root / TASKS_ROOT
    if not base.is_dir():
        raise CoverageError(f"library tasks directory is missing: {base}")
    packages: list[Package] = []
    for toml_path in sorted(base.rglob("task.toml")):
        task_dir = toml_path.parent
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        table = data.get("task")
        name = table.get("name") if isinstance(table, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise CoverageError(
                f"{task_dir.relative_to(root).as_posix()} declares no [task] name in task.toml"
            )
        packages.append(Package(task_dir.relative_to(root).as_posix(), name.strip()))
    if not packages:
        raise CoverageError(f"no task packages found under {base}")
    names = [p.task for p in packages]
    if len(names) != len(set(names)):
        raise CoverageError("two library packages declare the same [task] name")
    return packages


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise CoverageError(f"evidence input is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _version(raw: object) -> str:
    version = str(raw or "").strip()
    if version not in LADDER_VERSIONS:
        raise CoverageError(
            f"evidence names ladder version {version!r}; extend LADDER_VERSIONS deliberately"
        )
    return version


def _flip(raw: object) -> str | None:
    """Map a matrix cheat verdict (``pass``/``fail``) to cracked/clean."""
    return {"pass": "cracked", "fail": "clean"}.get(str(raw))


def _verdict_or_none(raw: object) -> str | None:
    value = str(raw or "")
    return value if value in ("clean", "cracked") else None


def _crack_sweep_cells(root: Path, by_path: dict[str, Package]) -> list[Cell]:
    data = _read_json(root / CRACK_SWEEP)
    date = str(data.get("generated") or "")
    cells: list[Cell] = []
    for row in data.get("rows") or []:
        package = by_path.get(str(row.get("task_dir") or ""))
        setup = str(row.get("setup") or "")
        # ``fixed``/``hardened`` rows ran task transforms, not shipped bytes.
        if package is None or not setup.startswith("shipped"):
            continue
        row_cells = row.get("cells") or {}
        verdict: str | None = None
        harbor_oracle = (row_cells.get("harbor:oracle") or {}).get("verdict")
        harbor_nop = (row_cells.get("harbor:nop") or {}).get("verdict")
        if harbor_oracle == "pass" and harbor_nop == "fail":
            verdict = _flip((row_cells.get("harbor:cheat") or {}).get("verdict"))
        cells.append(
            Cell(
                package=package.path,
                ladder_version=CRACK_SWEEP_LADDER,
                verdict=verdict,
                task_digest=None,
                source=CRACK_SWEEP,
                date=date,
            )
        )
    return cells


def _v2_sweep_cells(root: Path, by_name: dict[str, Package]) -> list[Cell]:
    data = _read_json(root / V2_SWEEP)
    version = _version(data.get("ladder_version"))
    date = str(data.get("generated") or "")
    cells: list[Cell] = []
    for row in (data.get("library") or {}).get("rows") or []:
        package = by_name.get(str(row.get("task_id") or ""))
        if package is None:
            continue
        row_cells = row.get("cells") or {}
        verdict: str | None = None
        if row_cells.get("harbor:oracle") == "pass" and row_cells.get("harbor:nop") == "fail":
            verdict = _flip(row_cells.get("harbor:cheat"))
        cells.append(
            Cell(
                package=package.path,
                ladder_version=version,
                verdict=verdict,
                task_digest=None,
                source=V2_SWEEP,
                date=date,
            )
        )
    return cells


def _har230_cells(root: Path, by_path: dict[str, Package]) -> list[Cell]:
    evidence = root / HAR230_DIR
    preflight = _read_json(evidence / "native-preflight.json")
    controls = _read_json(evidence / "native-controls.json")
    package = by_path.get(str(preflight.get("task") or ""))
    if package is None:
        return []
    digest = preflight.get("harbor_task_digest")
    recorded_at = str(controls.get("recorded_at") or "")
    date = recorded_at[:10]
    control_cells = controls.get("controls") or {}
    control_ok = (control_cells.get("oracle") or {}).get("reward") == 1.0 and (
        control_cells.get("nop") or {}
    ).get("reward") == 0.0
    supporting = (
        f"{HAR230_DIR}/native-preflight.json",
        f"{HAR230_DIR}/native-controls.json",
    )
    cells: list[Cell] = []
    for verdicts_path in sorted(evidence.glob("*-verdicts.json")):
        data = _read_json(verdicts_path)
        version = _version(data.get("ladder_version"))
        source = verdicts_path.relative_to(root).as_posix()
        for trial in data.get("trials") or []:
            verdict = _verdict_or_none(trial.get("verdict")) if control_ok else None
            cells.append(
                Cell(
                    package=package.path,
                    ladder_version=version,
                    verdict=verdict,
                    task_digest=digest if isinstance(digest, str) else None,
                    source=source,
                    date=date,
                    supporting=supporting,
                )
            )
    return cells


def _har231_cells(root: Path, by_name: dict[str, Package]) -> list[Cell]:
    data = _read_json(root / HAR231_CELLS)
    date = str(data.get("generated") or "")
    cells: list[Cell] = []
    # ``git_armed_variant`` cells patch the Dockerfile into a git work tree, so
    # they measure a variant, not the shipped package.
    for record in list(data.get("cells") or []) + list(data.get("full_ladder_runs") or []):
        package = by_name.get(str(record.get("task_id") or ""))
        if package is None:
            continue
        digest = record.get("task_digest")
        cells.append(
            Cell(
                package=package.path,
                ladder_version=_version(record.get("ladder_version") or data.get("ladder_version")),
                verdict=_verdict_or_none(record.get("verdict")),
                task_digest=digest if isinstance(digest, str) else None,
                source=HAR231_CELLS,
                date=date,
            )
        )
    return cells


def _control_ok(controls: object) -> bool:
    """The ledger's own native oracle passed (1.0) and nop failed (0.0)."""
    rewards = {
        str(control.get("agent") or ""): control.get("reward")
        for control in controls or []
        if isinstance(control, dict)
    }
    return rewards.get("oracle") == 1.0 and rewards.get("nop") == 0.0


def _ledger_cells(root: Path, by_path: dict[str, Package]) -> list[Cell]:
    """Read every ``ladder-v*`` ledger in the 2026-11-02 shipped-cells layout.

    The layout is a ``task_dir`` naming the package -- top-level, or per record
    when one ledger covers several packages -- plus ``cells`` records carrying
    ``scope`` (``full``/``single``), ``ladder_version``,
    ``verdict``, ``status`` and ``task_digest``. Each cell's verdict is gated by
    the oracle/nop controls of its own package. Reading the glob -- not a
    hardcoded path per ladder card -- is what keeps the next card's evidence in
    the matrix; a ledger that cannot be placed is refused by
    :func:`unread_ledgers` instead of being skipped.
    """
    evidence = root / "research" / "evidence"
    if not evidence.is_dir():
        return []
    cells: list[Cell] = []
    for path in sorted(evidence.glob("ladder-v*/cells.json")):
        source = path.relative_to(root).as_posix()
        if source in DEDICATED_LEDGERS:
            continue
        data = _read_json(path)
        records = data.get("cells")
        if not isinstance(records, list):
            continue
        default_dir = str(data.get("task_dir") or "")
        date = str(data.get("generated") or "")
        controls = [c for c in data.get("controls") or [] if isinstance(c, dict)]
        for record in records:
            if not isinstance(record, dict):
                raise CoverageError(f"{source}: ladder cell is not an object")
            package = by_path.get(str(record.get("task_dir") or default_dir))
            if package is None:
                continue
            control_ok = _control_ok(
                [c for c in controls if str(c.get("task_dir") or default_dir) == package.path]
            )
            scope = str(record.get("scope") or "")
            if scope not in LEDGER_SCOPES:
                raise CoverageError(
                    f"{source}: cell scope {scope!r} is not one of {LEDGER_SCOPES}; "
                    "a variant scope ran on other bytes and needs its own reader"
                )
            digest = record.get("task_digest")
            cells.append(
                Cell(
                    package=package.path,
                    ladder_version=_version(
                        record.get("ladder_version") or data.get("ladder_version")
                    ),
                    verdict=(
                        _verdict_or_none(record.get("verdict"))
                        if control_ok and record.get("status") == "executed"
                        else None
                    ),
                    task_digest=digest if isinstance(digest, str) else None,
                    source=source,
                    date=date,
                )
            )
    return cells


def unread_ledgers(root: Path, cells: Sequence[Cell]) -> list[str]:
    """``ladder-v*`` ledgers that contributed no cell, so no row reads them."""
    evidence = root / "research" / "evidence"
    if not evidence.is_dir():
        return []
    read = {cell.source for cell in cells}
    return sorted(
        path.relative_to(root).as_posix()
        for path in evidence.glob("ladder-v*/cells.json")
        if path.relative_to(root).as_posix() not in read
    )


def require_read_ledgers(root: Path, cells: Sequence[Cell]) -> None:
    """Refuse a ladder ledger in the glob that contributed no cell to a row."""
    unread = unread_ledgers(root, cells)
    if unread:
        raise CoverageError(
            f"{LADDER_LEDGER_GLOB} evidence contributes no coverage row: " + ", ".join(unread)
        )


def collect_cells(root: Path, packages: Sequence[Package]) -> list[Cell]:
    """Read every committed ladder measurement that names a library package.

    A ladder ledger in the glob that no reader consumed is an error: silently
    dropping it is how a new ladder card goes stale in the matrix.
    """
    by_name = {package.task: package for package in packages}
    by_path = {package.path: package for package in packages}
    cells = [
        *_crack_sweep_cells(root, by_path),
        *_v2_sweep_cells(root, by_name),
        *_har230_cells(root, by_path),
        *_har231_cells(root, by_name),
        *_ledger_cells(root, by_path),
    ]
    require_read_ledgers(root, cells)
    return cells


def _aggregate(cells: Sequence[Cell]) -> str:
    """A crack wins over a clean; no measurement at all is ``none``."""
    verdicts = {cell.verdict for cell in cells if cell.verdict}
    if "cracked" in verdicts:
        return "cracked"
    if "clean" in verdicts:
        return "clean"
    return "none"


def _harbor_digest(package: Path) -> str:
    """Today's ladder package digest, from evallab's own helper."""
    try:
        from evallab.registry import harbor_task_digest
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "evallab is not importable in this interpreter; run the builder inside the "
            "locked project environment: uv run python " + Path(__file__).as_posix()
        ) from exc
    return harbor_task_digest(package)


def assemble(
    packages: Sequence[Package], cells: Sequence[Cell], root: Path = ROOT
) -> list[dict[str, str]]:
    """Cross packages with ladder generations into the coverage rows."""
    grouped: dict[tuple[str, str], list[Cell]] = defaultdict(list)
    for cell in cells:
        grouped[(cell.package, cell.ladder_version)].append(cell)

    current: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for package in packages:
        for version in LADDER_VERSIONS:
            package_cells = grouped.get((package.path, version), [])
            dates = sorted({cell.date for cell in package_cells if cell.date})
            if package_cells and not dates:
                raise CoverageError(f"{package.path}@{version}: cells carry no evidence date")
            latest = dates[-1] if dates else None
            newest = [cell for cell in package_cells if cell.date == latest]
            verdict = _aggregate(newest)
            superseded: list[str] = []
            for date in dates[:-1]:
                older = _aggregate([cell for cell in package_cells if cell.date == date])
                if older not in ("none", verdict):
                    superseded.append(f"{date}:{older}")
            digests = sorted({cell.task_digest for cell in newest if cell.task_digest})
            digest_current = "no"
            if digests:
                if package.path not in current:
                    current[package.path] = _harbor_digest(root / package.path)
                digest_current = "yes" if digests == [current[package.path]] else "no"
            rows.append(
                {
                    "package": package.path,
                    "task": package.task,
                    "ladder_version": version,
                    "verdict": verdict,
                    "n_cells": str(len(package_cells)),
                    "evidence_path": ";".join(
                        sorted(
                            {
                                source
                                for cell in package_cells
                                for source in (cell.source, *cell.supporting)
                            }
                        )
                    ),
                    "task_digest": ";".join(digests),
                    "digest_current": digest_current,
                    "digest_recorded": "yes" if digests else "no",
                    "superseded_verdicts": ";".join(superseded),
                }
            )
    return rows


def coverage_gaps(
    packages: Sequence[Package], rows: Sequence[dict[str, str]]
) -> list[tuple[str, str]]:
    """(package path, ladder version) pairs that have no row."""
    covered = {(row["package"], row["ladder_version"]) for row in rows}
    return [
        (package.path, version)
        for package in packages
        for version in LADDER_VERSIONS
        if (package.path, version) not in covered
    ]


def gap_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """One gap row per ``verdict=none`` coverage row, with a mechanical reason.

    ``reason`` is ``no-cells`` when nothing was ever recorded at that
    generation, ``stale-digest`` when cells recorded a package digest that no
    longer matches today's bytes, and ``errored`` otherwise (cells were
    recorded but produced no verdict). ``superseded_by`` names the highest
    later ladder version whose row holds a non-none verdict on the current
    package digest -- the generation no longer needs to run because a newer
    one already measured these bytes. An empty ``superseded_by`` is a real
    gap.
    """
    by_package: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        by_package[row["package"]][row["ladder_version"]] = row
    gaps: list[dict[str, str]] = []
    for row in rows:
        if row["verdict"] != "none":
            continue
        if int(row["n_cells"]) == 0:
            reason = "no-cells"
        elif row["digest_recorded"] == "yes" and row["digest_current"] == "no":
            reason = "stale-digest"
        else:
            reason = "errored"
        later = LADDER_VERSIONS[LADDER_VERSIONS.index(row["ladder_version"]) + 1 :]
        superseded_by = ""
        for version in later:
            candidate = by_package[row["package"]].get(version)
            if (
                candidate is not None
                and candidate["verdict"] != "none"
                and candidate["digest_current"] == "yes"
            ):
                superseded_by = version
        gaps.append(
            {
                "package": row["package"],
                "ladder_version": row["ladder_version"],
                "reason": reason,
                "superseded_by": superseded_by,
            }
        )
    return gaps


def build(root: Path = ROOT) -> list[dict[str, str]]:
    packages = discover_packages(root)
    cells = collect_cells(root, packages)
    rows = assemble(packages, cells, root)
    gaps = coverage_gaps(packages, rows)
    if gaps:  # pragma: no cover - assemble emits every pair by construction
        raise CoverageError(f"packages without a coverage row: {gaps}")
    return rows


def _render(rows: Sequence[dict[str, str]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render(rows: Sequence[dict[str, str]]) -> str:
    return _render(rows, COLUMNS)


def render_gaps(rows: Sequence[dict[str, str]]) -> str:
    return _render(rows, GAP_COLUMNS)


def _check(name: str, rebuilt: str) -> bool:
    """Compare ``rebuilt`` against the committed file; True when it matches."""
    target = HERE / name
    committed = target.read_text(encoding="utf-8") if target.is_file() else ""
    if committed == rebuilt:
        print(f"{name}: reproduces from committed evidence")
        return True
    print(f"{name}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{name} (committed)",
        tofile=f"{name} (rebuilt)",
        lineterm="",
    )
    for line in list(diff)[:40]:
        print(line, file=sys.stderr)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if committed coverage.csv or gaps.csv differs",
    )
    args = parser.parse_args(argv)
    rows = build()
    gaps = gap_rows(rows)
    if args.check:
        ok = _check(COVERAGE_NAME, render(rows))
        ok = _check(GAPS_NAME, render_gaps(gaps)) and ok
        return 0 if ok else 1
    (HERE / COVERAGE_NAME).write_text(render(rows), encoding="utf-8")
    (HERE / GAPS_NAME).write_text(render_gaps(gaps), encoding="utf-8")
    print(f"wrote {COVERAGE_NAME}: {len(rows)} rows; {GAPS_NAME}: {len(gaps)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
