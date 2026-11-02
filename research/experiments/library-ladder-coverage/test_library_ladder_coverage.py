"""The library ladder coverage matrix must stay mechanical and complete.

`build.py` is the only writer of `coverage.csv`. These tests pin what a lane
relies on: the committed table is what the committed evidence currently
produces, every `library/tasks` package has a row for every ladder generation
(so a new package cannot silently vanish from the gap list), a package with no
evidence still gets explicit `none` rows, verdicts follow the newest source,
digest freshness comes from evallab's own package digest, and every
`research/evidence/ladder-v*/cells.json` either reaches the matrix through the
glob or fails the build.
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
render = MODULE["render"]


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


def test_a_package_without_a_declared_task_name_is_refused(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/nameless"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nversion = "1.0.0"\n', encoding="utf-8")
    with pytest.raises(MODULE["CoverageError"]):
        discover_packages(tmp_path)


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


def test_an_unreadable_ladder_ledger_is_refused_not_skipped(tmp_path: Path) -> None:
    # A ledger whose shape this builder cannot place contributes no row; that must
    # fail loudly, because a silently skipped ledger is the stale-matrix bug.
    _ledger(tmp_path, "research/evidence/ladder-v9-mystery/cells.json", {"cells": []})
    cells = MODULE["_ledger_cells"](tmp_path, {})
    assert cells == []
    assert MODULE["unread_ledgers"](tmp_path, cells) == [
        "research/evidence/ladder-v9-mystery/cells.json"
    ]
    with pytest.raises(MODULE["CoverageError"], match="ladder-v9-mystery"):
        MODULE["require_read_ledgers"](tmp_path, cells)
