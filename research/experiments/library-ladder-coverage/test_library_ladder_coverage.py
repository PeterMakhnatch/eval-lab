"""The library ladder coverage matrix and gap list stay mechanical and complete.

`build.py` is the only writer of `coverage.csv` and `gaps.csv`. These tests pin
what a lane relies on: the committed tables are what the committed evidence
currently produces, every `library/tasks` package has a row for every ladder
generation (so a new package cannot silently vanish from the gap list), a
package with no evidence still gets explicit `none` rows, verdicts follow the
newest source, digest freshness comes from evallab's own package digest, and
every `research/evidence/ladder-v*/cells.json` either reaches the matrix
through the glob or fails the build. For `gaps.csv` they pin the derived
contract: one row per `verdict=none` cell, each of the three `reason` values,
and the `superseded_by` rule. `main --check` compares committed bytes, so a
stale row or a CRLF rewrite of either table exits 1. Each `CoverageError` guard
on the builder's inputs has its own case, so a missing root, a nameless package,
a broken ledger, or an unknown version fails by a named message.
"""

from __future__ import annotations

import csv
import io
import json
import runpy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = REPO_ROOT / "research" / "experiments" / "library-ladder-coverage"
MODULE = runpy.run_path(str(EXPERIMENT / "build.py"))

LADDER_VERSIONS = MODULE["LADDER_VERSIONS"]
Package = MODULE["Package"]
Cell = MODULE["Cell"]
assemble = MODULE["assemble"]
build = MODULE["build"]
coverage_gaps = MODULE["coverage_gaps"]
discover_packages = MODULE["discover_packages"]
gap_rows = MODULE["gap_rows"]
render = MODULE["render"]
render_gaps = MODULE["render_gaps"]


def _rows() -> list[dict[str, str]]:
    text = (EXPERIMENT / "coverage.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _package(root: Path, relative: str, name: str) -> Path:
    task_dir = root / relative
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(f'[task]\nname = "{name}"\n', encoding="utf-8")
    return task_dir


def test_committed_coverage_reproduces_from_evidence() -> None:
    # A hand edit (or an unregenerated evidence refresh) is drift, not a review
    # preference: --check and this test both fail until build.py rewrites it.
    committed = (EXPERIMENT / "coverage.csv").read_text(encoding="utf-8")
    assert render(build()) == committed


def test_every_library_package_has_one_row_per_ladder_version() -> None:
    packages = discover_packages(REPO_ROOT)
    rows = _rows()
    missing = coverage_gaps(packages, rows)
    assert not missing, f"packages without a coverage row: {missing}"
    assert len(rows) == len(packages) * len(LADDER_VERSIONS)
    keys = [(row["package"], row["ladder_version"]) for row in rows]
    assert len(keys) == len(set(keys)), "one row per package and generation"


def test_committed_gaps_reproduces_from_evidence() -> None:
    # gaps.csv is derived: a hand edit or a regenerated coverage.csv without a
    # regenerated gaps.csv is drift, and --check fails until it is rebuilt.
    committed = (EXPERIMENT / "gaps.csv").read_text(encoding="utf-8")
    assert render_gaps(gap_rows(build())) == committed


def test_gaps_csv_has_one_row_per_verdict_none_coverage_row() -> None:
    coverage = _rows()
    committed = list(
        csv.DictReader(io.StringIO((EXPERIMENT / "gaps.csv").read_text(encoding="utf-8")))
    )
    none_rows = [row for row in coverage if row["verdict"] == "none"]
    assert [(row["package"], row["ladder_version"]) for row in committed] == [
        (row["package"], row["ladder_version"]) for row in none_rows
    ]
    assert {row["reason"] for row in committed} <= {"no-cells", "errored", "stale-digest"}


def _coverage_row(
    package: str,
    version: str,
    verdict: str = "none",
    n_cells: str = "0",
    digest_current: str = "no",
    digest_recorded: str = "no",
) -> dict[str, str]:
    return {
        "package": package,
        "ladder_version": version,
        "verdict": verdict,
        "n_cells": n_cells,
        "digest_current": digest_current,
        "digest_recorded": digest_recorded,
    }


def test_gap_reason_no_cells_when_nothing_was_recorded() -> None:
    rows = [_coverage_row("p", "1.4.0", n_cells="0")]
    [gap] = gap_rows(rows)
    assert (gap["reason"], gap["superseded_by"]) == ("no-cells", "")


def test_gap_reason_errored_when_cells_recorded_no_verdict() -> None:
    # tau3-retail-1's shape: a recorded cell whose controls/verdict yield none.
    rows = [_coverage_row("p", "1.4.0", n_cells="1")]
    [gap] = gap_rows(rows)
    assert gap["reason"] == "errored"


def test_gap_reason_stale_digest_beats_errored() -> None:
    # Cells recorded a digest that no longer matches today's package bytes:
    # "stale bytes" names the fix (re-run) better than "errored".
    rows = [_coverage_row("p", "1.4.0", n_cells="2", digest_recorded="yes", digest_current="no")]
    [gap] = gap_rows(rows)
    assert gap["reason"] == "stale-digest"


def test_superseded_by_is_the_highest_later_verdict_on_the_current_digest() -> None:
    rows = [
        _coverage_row("p", "1.4.0"),
        _coverage_row("p", "2.0.0"),
        _coverage_row("p", "3.0.0", verdict="clean", n_cells="3", digest_current="no"),
        _coverage_row(
            "p",
            "4.0.0",
            verdict="clean",
            n_cells="4",
            digest_current="yes",
            digest_recorded="yes",
        ),
    ]
    gaps = {row["ladder_version"]: row for row in gap_rows(rows)}
    # The stale-digest 3.0.0 verdict does not supersede; the current-digest
    # 4.0.0 verdict does.
    assert gaps["1.4.0"]["superseded_by"] == "4.0.0"
    assert gaps["2.0.0"]["superseded_by"] == "4.0.0"


def test_superseded_by_is_empty_without_a_current_later_verdict() -> None:
    rows = [
        _coverage_row("p", "1.4.0"),
        _coverage_row("p", "3.0.0", verdict="clean", n_cells="3", digest_current="no"),
        _coverage_row("p", "4.0.0"),
    ]
    gaps = {row["ladder_version"]: row for row in gap_rows(rows)}
    # A later verdict on stale bytes does not close the earlier generation,
    # and the last generation has no later version at all.
    assert gaps["1.4.0"]["superseded_by"] == ""
    assert gaps["4.0.0"]["superseded_by"] == ""


def test_cells_without_an_evidence_date_are_refused(tmp_path: Path) -> None:
    # A cell with no date cannot be ordered against another source, so the row
    # would silently read the wrong evidence.
    package = Package("library/tasks/task", "peter/task")
    cells = [Cell(package.path, "4.0.0", "clean", None, "cells.json", "")]
    with pytest.raises(MODULE["CoverageError"], match="cells carry no evidence date"):
        assemble([package], cells, root=tmp_path)


def test_a_new_package_directory_gets_explicit_none_rows(tmp_path: Path) -> None:
    _package(tmp_path, "library/tasks/brand-new", "peter/brand-new")
    packages = discover_packages(tmp_path)
    assert [package.path for package in packages] == ["library/tasks/brand-new"]
    rows = assemble(packages, [], root=tmp_path)
    assert [(row["package"], row["ladder_version"], row["verdict"]) for row in rows] == [
        ("library/tasks/brand-new", version, "none") for version in LADDER_VERSIONS
    ]
    assert {row["n_cells"] for row in rows} == {"0"}
    assert {row["digest_recorded"] for row in rows} == {"no"}


def test_the_guard_reports_a_package_without_a_row() -> None:
    package = Package("library/tasks/lonely", "peter/lonely")
    gaps = coverage_gaps([package], [])
    assert gaps == [("library/tasks/lonely", version) for version in LADDER_VERSIONS]


def test_a_missing_library_tasks_directory_is_refused(tmp_path: Path) -> None:
    # No library/tasks at all: this root is not a checkout the builder can read.
    with pytest.raises(MODULE["CoverageError"], match="library tasks directory is missing"):
        discover_packages(tmp_path)


def test_a_library_with_no_task_packages_is_refused(tmp_path: Path) -> None:
    # The directory exists but holds no task.toml: an empty library is a bad
    # root, not a library with zero rows.
    (tmp_path / "library/tasks").mkdir(parents=True)
    with pytest.raises(MODULE["CoverageError"], match="no task packages found under"):
        discover_packages(tmp_path)


def test_a_package_without_a_declared_task_name_is_refused(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/nameless"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nversion = "1.0.0"\n', encoding="utf-8")
    with pytest.raises(MODULE["CoverageError"], match=r"declares no \[task\] name in task.toml"):
        discover_packages(tmp_path)


def test_two_packages_with_the_same_task_name_are_refused(tmp_path: Path) -> None:
    _package(tmp_path, "library/tasks/one", "peter/same")
    _package(tmp_path, "library/tasks/two", "peter/same")
    with pytest.raises(MODULE["CoverageError"], match="two library packages declare the same"):
        discover_packages(tmp_path)


def test_a_missing_evidence_input_is_refused(tmp_path: Path) -> None:
    # Every reader starts at _read_json; a deleted evidence file must name the
    # path instead of raising FileNotFoundError from further down.
    with pytest.raises(MODULE["CoverageError"], match="evidence input is missing"):
        MODULE["_read_json"](tmp_path / "research/evidence/missing/cells.json")


def test_verdict_comes_from_the_newest_source_and_keeps_the_old_one() -> None:
    package = Package("library/tasks/task", "peter/task")
    cells = [
        Cell(package.path, "1.4.0", "cracked", None, "old.json", "2026-10-12"),
        Cell(package.path, "1.4.0", "clean", None, "new.json", "2026-10-29"),
    ]
    rows = {row["ladder_version"]: row for row in assemble([package], cells)}
    row = rows["1.4.0"]
    assert row["verdict"] == "clean"
    assert row["superseded_verdicts"] == "2026-10-12:cracked"
    assert row["n_cells"] == "2"
    assert row["evidence_path"] == "new.json;old.json"


def test_digest_freshness_uses_evallab_package_digest(tmp_path: Path) -> None:
    task_dir = _package(tmp_path, "library/tasks/task", "peter/task")
    package = Package("library/tasks/task", "peter/task")
    current = MODULE["_harbor_digest"](task_dir)

    fresh = Cell(package.path, "3.0.0", "clean", current, "cells.json", "2026-10-29")
    stale = Cell(package.path, "2.0.0", "clean", "sha256:" + "0" * 64, "cells.json", "2026-10-29")
    unrecorded = Cell(package.path, "1.4.0", "clean", None, "cells.json", "2026-10-12")
    rows = {
        row["ladder_version"]: row
        for row in assemble([package], [fresh, stale, unrecorded], root=tmp_path)
    }

    assert (rows["3.0.0"]["digest_current"], rows["3.0.0"]["digest_recorded"]) == ("yes", "yes")
    assert rows["3.0.0"]["task_digest"] == current
    assert (rows["2.0.0"]["digest_current"], rows["2.0.0"]["digest_recorded"]) == ("no", "yes")
    assert (rows["1.4.0"]["digest_current"], rows["1.4.0"]["digest_recorded"]) == ("no", "no")


def _ledger(root: Path, relative: str, payload: dict[str, object]) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ledger_payload(task_dir: str, verdict: object = "clean") -> dict[str, object]:
    return {
        "generated": "2026-11-02",
        "ladder_version": "4.0.0",
        "task_dir": task_dir,
        "attacks": ["git_history"],
        "cells": [
            {
                "task_id": "peter/task",
                "scope": "full",
                "ladder_version": "4.0.0",
                "verdict": verdict,
                "status": "executed",
                "task_digest": "sha256:" + "a" * 64,
            }
        ],
        "controls": [
            {"agent": "oracle", "reward": 1.0},
            {"agent": "nop", "reward": 0.0},
        ],
    }


def test_every_committed_ladder_ledger_contributes_a_row() -> None:
    # The card that produced the ledger does not get to edit build.py: a new
    # ``research/evidence/ladder-v*`` directory must reach the matrix through the
    # glob, and one no reader consumed fails the build instead of going stale.
    packages = discover_packages(REPO_ROOT)
    cells = MODULE["collect_cells"](REPO_ROOT, packages)
    ledgers = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "research/evidence").glob("ladder-v*/cells.json")
    )
    assert ledgers, "committed ladder evidence directories disappeared"
    assert MODULE["unread_ledgers"](REPO_ROOT, cells) == []


def test_a_new_ladder_ledger_reaches_the_matrix_without_a_code_change(tmp_path: Path) -> None:
    _package(tmp_path, "library/tasks/task", "peter/task")
    _ledger(
        tmp_path,
        "research/evidence/ladder-v4-task-20261103/cells.json",
        _ledger_payload("library/tasks/task"),
    )
    packages = discover_packages(tmp_path)
    cells = MODULE["_ledger_cells"](tmp_path, {package.path: package for package in packages})

    assert [(cell.source, cell.ladder_version, cell.verdict) for cell in cells] == [
        ("research/evidence/ladder-v4-task-20261103/cells.json", "4.0.0", "clean")
    ]
    assert cells[0].task_digest == "sha256:" + "a" * 64
    rows = {row["ladder_version"]: row for row in assemble(packages, cells, root=tmp_path)}
    assert rows["4.0.0"]["verdict"] == "clean"
    assert rows["4.0.0"]["n_cells"] == "1"
    assert MODULE["unread_ledgers"](tmp_path, cells) == []


def test_ledger_controls_gate_the_cell_verdict(tmp_path: Path) -> None:
    # A ledger whose native nop scored 1.0 did not establish gradeability, so its
    # cells are recorded evidence with no verdict -- not a clean row.
    _package(tmp_path, "library/tasks/task", "peter/task")
    payload = _ledger_payload("library/tasks/task")
    payload["controls"] = [{"agent": "oracle", "reward": 1.0}, {"agent": "nop", "reward": 1.0}]
    _ledger(tmp_path, "research/evidence/ladder-v4-task-20261103/cells.json", payload)
    cells = MODULE["_ledger_cells"](
        tmp_path, {"library/tasks/task": Package("library/tasks/task", "peter/task")}
    )

    assert [cell.verdict for cell in cells] == [None]
    row = assemble([Package("library/tasks/task", "peter/task")], cells)[3]
    assert (row["verdict"], row["n_cells"]) == ("none", "1")


def test_a_multi_package_ledger_places_and_gates_each_cell_by_its_own_task_dir(
    tmp_path: Path,
) -> None:
    # One card may record several packages in one ledger; each cell lands on its
    # own package, and only that package's controls decide whether it has a verdict.
    good = Package("library/tasks/good", "peter/good")
    broken = Package("library/tasks/broken", "peter/broken")
    cell = _ledger_payload("")["cells"][0]  # type: ignore[index]
    payload = {
        "generated": "2026-11-02",
        "ladder_version": "4.0.0",
        "task_dirs": [good.path, broken.path],
        "cells": [{**cell, "task_dir": good.path}, {**cell, "task_dir": broken.path}],
        "controls": [
            {"task_dir": good.path, "agent": "oracle", "reward": 1.0},
            {"task_dir": good.path, "agent": "nop", "reward": 0.0},
            {"task_dir": broken.path, "agent": "oracle", "reward": 1.0},
            {"task_dir": broken.path, "agent": "nop", "reward": 1.0},
        ],
    }
    _ledger(tmp_path, "research/evidence/ladder-v4-pair-20261103/cells.json", payload)
    cells = MODULE["_ledger_cells"](tmp_path, {good.path: good, broken.path: broken})

    assert [(cell.package, cell.verdict) for cell in cells] == [
        (good.path, "clean"),
        (broken.path, None),
    ]
    assert MODULE["unread_ledgers"](tmp_path, cells) == []


def test_a_ledger_cell_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    # A record this builder cannot read must fail loudly rather than vanish.
    _ledger(
        tmp_path,
        "research/evidence/ladder-v4-task-20261111/cells.json",
        {"cells": ["not an object"]},
    )
    with pytest.raises(MODULE["CoverageError"], match="ladder cell is not an object"):
        MODULE["_ledger_cells"](tmp_path, {})


def test_a_ledger_cell_scope_that_ran_other_bytes_is_refused(tmp_path: Path) -> None:
    # ``variant`` scopes patched the package; their cells do not measure the
    # shipped bytes and need a reader of their own.
    package = Package("library/tasks/task", "peter/task")
    _ledger(
        tmp_path,
        "research/evidence/ladder-v4-task-20261111/cells.json",
        {
            "generated": "2026-11-11",
            "ladder_version": "4.0.0",
            "task_dir": package.path,
            "cells": [{"scope": "variant", "status": "executed"}],
            "controls": [],
        },
    )
    with pytest.raises(MODULE["CoverageError"], match="a variant scope ran on other bytes"):
        MODULE["_ledger_cells"](tmp_path, {package.path: package})


def test_a_ledger_naming_an_unknown_ladder_version_is_refused(tmp_path: Path) -> None:
    package = Package("library/tasks/task", "peter/task")
    _ledger(
        tmp_path,
        "research/evidence/ladder-v4-task-20261111/cells.json",
        {
            "generated": "2026-11-11",
            "ladder_version": "9.9.9",
            "task_dir": package.path,
            "cells": [{"scope": "full", "status": "executed"}],
            "controls": [],
        },
    )
    with pytest.raises(MODULE["CoverageError"], match="extend LADDER_VERSIONS deliberately"):
        MODULE["_ledger_cells"](tmp_path, {package.path: package})


def test_an_unreadable_ladder_ledger_is_refused_not_skipped(tmp_path: Path) -> None:
    # A ledger whose shape this builder cannot place contributes no row; that must
    # fail loudly, because a silently skipped ledger is the stale-matrix bug.
    _ledger(tmp_path, "research/evidence/ladder-v9-mystery/cells.json", {"cells": []})
    cells = MODULE["_ledger_cells"](tmp_path, {})
    assert cells == []
    assert MODULE["unread_ledgers"](tmp_path, cells) == [
        "research/evidence/ladder-v9-mystery/cells.json"
    ]
    with pytest.raises(
        MODULE["CoverageError"],
        match=r"contributes no coverage row: research/evidence/ladder-v9-mystery/cells.json",
    ):
        MODULE["require_read_ledgers"](tmp_path, cells)


def _minimal_evidence(root: Path, task: str) -> None:
    """Every committed evidence input, with one cell for each globbed ledger.

    The v2 and v3 ledgers also match ``ladder-v*/cells.json``, so they must place
    a cell on ``task`` or ``require_read_ledgers`` refuses them before ``build``
    reaches its own guard.
    """
    files: dict[str, dict[str, object]] = {
        "research/evidence/library-crack-sweep-20261012/cells.json": {
            "generated": "2026-10-12",
            "rows": [],
        },
        "research/evidence/ladder-v2-sweep-20261013/cells.json": {
            "generated": "2026-10-13",
            "ladder_version": "2.0.0",
            "library": {"rows": [{"task_id": task, "cells": {}}]},
        },
        "research/evidence/har230-transaction-reconciliation-20261029/native-preflight.json": {},
        "research/evidence/har230-transaction-reconciliation-20261029/native-controls.json": {},
        "research/evidence/ladder-v3-sheet-recalc-20261029/cells.json": {
            "generated": "2026-10-29",
            "cells": [{"task_id": task, "ladder_version": "3.0.0"}],
        },
    }
    for relative, payload in files.items():
        _ledger(root, relative, payload)


def test_build_refuses_packages_without_a_coverage_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is unreachable while ``assemble`` emits every pair by construction."""
    _package(tmp_path, "library/tasks/task", "peter/task")
    _minimal_evidence(tmp_path, "peter/task")
    monkeypatch.setitem(
        MODULE["build"].__globals__,
        "coverage_gaps",
        lambda packages, rows: [("library/tasks/task", "1.4.0")],
    )
    with pytest.raises(MODULE["CoverageError"], match="packages without a coverage row"):
        MODULE["build"](tmp_path)


def _copy_tables(out_dir: Path) -> None:
    for name in ("coverage.csv", "gaps.csv"):
        (out_dir / name).write_bytes((EXPERIMENT / name).read_bytes())


@pytest.mark.parametrize(
    ("argv", "label"), [([], "build"), (["--check"], "--check")], ids=["build", "check"]
)
def test_main_reports_a_coverage_error_as_one_stderr_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    label: str,
) -> None:
    # runpy hands back a namespace copy, so patch the writer's own globals: the
    # first input build() reads is the library package tree.
    detail = f"library tasks directory is missing: {tmp_path}/library/tasks"

    def refuse(_root: Path) -> list[object]:
        raise MODULE["CoverageError"](detail)

    monkeypatch.setitem(MODULE["main"].__globals__, "discover_packages", refuse)
    assert MODULE["main"](argv, out_dir=tmp_path) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [f"{label}: {detail}"]
    assert "Traceback" not in captured.err


def test_check_exits_zero_on_the_committed_tree(capsys: pytest.CaptureFixture[str]) -> None:
    # --check only reads, so the committed directory itself is safe to point at.
    assert MODULE["main"](["--check"]) == 0
    out = capsys.readouterr().out
    assert "coverage.csv: reproduces" in out
    assert "gaps.csv: reproduces" in out


def test_check_exits_zero_on_an_exact_copy(tmp_path: Path) -> None:
    _copy_tables(tmp_path)
    assert MODULE["main"](["--check"], out_dir=tmp_path) == 0


@pytest.mark.parametrize("name", ["coverage.csv", "gaps.csv"])
def test_check_exits_one_on_a_stale_table(
    tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_tables(tmp_path)
    lines = (tmp_path / name).read_bytes().splitlines(keepends=True)
    assert len(lines) > 1, f"{name} needs a data row to drop"
    (tmp_path / name).write_bytes(b"".join(lines[:-1]))
    assert MODULE["main"](["--check"], out_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert f"{name}: stale" in err
    assert lines[-1].decode("utf-8").rstrip("\n") in err  # the diff names the dropped row


@pytest.mark.parametrize("name", ["coverage.csv", "gaps.csv"])
@pytest.mark.parametrize(
    "count",
    [pytest.param(-1, id="crlf"), pytest.param(1, id="one-stray-cr")],
)
def test_check_exits_one_on_carriage_return_drift(tmp_path: Path, name: str, count: int) -> None:
    # Text-mode reads fold CRLF to LF; the check must see the bytes on disk.
    _copy_tables(tmp_path)
    target = tmp_path / name
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n", count))
    assert MODULE["main"](["--check"], out_dir=tmp_path) == 1
