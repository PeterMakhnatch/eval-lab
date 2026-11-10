"""The fleet/ladder join and its README counts stay mechanical (HAR-372).

``research/experiments/mimo-v6-full-fleet/ladder_join.py`` is the only writer of
``fleet-ladder-join.csv``. These tests pin what the table is relied on for: the
committed CSV is what the committed census, fleet manifest, ranked cells and
clean-v6 manifest currently produce; every fleet task has exactly one row and a
task with no ranked cell keeps its cell columns empty; a cell that ran other
bytes is ``no`` and one that ran the fleet digest is ``yes``; the README count
table is those counts rather than a second hand-maintained number; and the
builder refuses inputs that disagree about a digest or a verdict.
"""

from __future__ import annotations

import csv
import io
import json
import re
import runpy
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "research/experiments/mimo-v6-full-fleet"
MODULE = runpy.run_path(str(DIR / "ladder_join.py"))
COLUMNS = MODULE["COLUMNS"]
CELL_COLUMNS = ("ladder_rank", "ladder_verdict", "ladder_basis", "ladder_digest")
README = DIR / "README.md"
COUNTS_HEADING = "## Ranked ladder cells against fleet digests"
TASK = "format-code-task-000001"
LEDGER = "research/experiments/mimo-clean-v6/ladder-fixture.json"
FLEET_DIGEST = "sha256:" + "a" * 64
CELL_DIGEST = "sha256:" + "b" * 64


def read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def _rows() -> list[dict[str, str]]:
    return read_csv(DIR / MODULE["JOIN_NAME"])


def _by_task() -> dict[str, dict[str, str]]:
    return {row["task_id"]: row for row in _rows()}


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fieldnames)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def readme_counts() -> list[tuple[str, int, int, int]]:
    """The README count table as (bucket, tasks, with a cell, at the fleet digest)."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index(COUNTS_HEADING)
    table: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            table.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    header, *body = table
    assert header == ["Bucket", "Fleet tasks", "Have a ranked cell", "Cell ran the fleet digest"]
    counts: list[tuple[str, int, int, int]] = []
    for label, total, with_cell, at_digest in body:
        if set(label) <= {"-", ":"}:  # the markdown separator row
            continue
        key = "all" if label == "All fleet tasks" else label.strip("`")
        counts.append((key, int(total), int(with_cell), int(at_digest)))
    return counts


@pytest.fixture
def join_root(tmp_path: Path) -> Path:
    """A minimal root that joins one ranked cell onto one fleet task."""
    write_csv(
        tmp_path / MODULE["FLEET_MANIFEST"], ("task_id", "final_digest"), [(TASK, FLEET_DIGEST)]
    )
    write_csv(
        tmp_path / MODULE["CENSUS"],
        ("task_id", "language", "bucket", "v6_package_digest"),
        [(TASK, "python", "v6_verified", FLEET_DIGEST)],
    )
    write_csv(
        tmp_path / MODULE["CLEAN_MANIFEST"], ("task_id", "final_digest"), [(TASK, CELL_DIGEST)]
    )
    write_csv(
        tmp_path / MODULE["PROGRESS"],
        ("rank", "task_id", "verdict", "basis", "source_file"),
        [(1, TASK, "clean", "graded", LEDGER)],
    )
    ledger = tmp_path / LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "manifest": MODULE["CLEAN_MANIFEST"],
                "tasks": [{"task_id": TASK, "final_digest": CELL_DIGEST, "verdict": "clean"}],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_committed_join_reproduces_from_committed_inputs() -> None:
    # A hand edit, or a refreshed census or ladder cell that was not regenerated,
    # is drift: --check and this test both fail until ladder_join.py rewrites it.
    committed = (DIR / MODULE["JOIN_NAME"]).read_bytes()
    assert MODULE["render"](MODULE["build"]()).encode("utf-8") == committed


def test_check_exits_one_on_drift_and_zero_when_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # runpy hands back a namespace copy, so the writer's own globals are the ones
    # that decide where the committed file is read from.
    namespace = MODULE["main"].__globals__
    assert namespace["HERE"] == DIR
    monkeypatch.setitem(namespace, "HERE", tmp_path)
    committed = tmp_path / MODULE["JOIN_NAME"]
    rebuilt = MODULE["render"](MODULE["build"]())
    committed.write_text("stale\n", encoding="utf-8")
    assert MODULE["main"](["--check"]) == 1
    # Same lines, different bytes: --check is byte equality, not text equality.
    committed.write_bytes(rebuilt.replace("\n", "\r\n").encode("utf-8"))
    assert MODULE["main"](["--check"]) == 1
    committed.write_bytes(rebuilt.encode("utf-8"))
    assert MODULE["main"](["--check"]) == 0


def test_every_fleet_task_has_exactly_one_row_and_keeps_census_facts() -> None:
    census = read_csv(DIR / "mimo-v6-fleet-census.csv")
    rows = _rows()
    assert len(rows) == len(census) == 567
    assert [row["task_id"] for row in rows] == sorted(row["task_id"] for row in census)
    assert tuple(rows[0]) == COLUMNS
    described = {row["task_id"]: row for row in census}
    for row in rows:
        source = described[row["task_id"]]
        assert row["language"] == source["language"]
        assert row["bucket"] == source["bucket"]
        assert row["fleet_digest"] == source["v6_package_digest"]


def test_a_fleet_task_without_a_ranked_cell_keeps_empty_cell_columns() -> None:
    rows = _by_task()
    ranked = MODULE["ladder_cells"]()
    unranked = [task for task in rows if task not in ranked]
    assert unranked, "the fixture fleet must contain a task with no ranked cell"
    for task in unranked:
        assert rows[task]["digest_match"] == MODULE["MATCH_NONE"]
    for row in rows.values():
        cell_values = [row[column] for column in CELL_COLUMNS]
        if row["digest_match"] == MODULE["MATCH_NONE"]:
            assert not any(cell_values)
        else:
            assert all(cell_values), f"{row['task_id']} has a match label but no cell"


def test_a_cell_at_another_digest_is_marked_no() -> None:
    row = _by_task()["format-code-task-000081"]  # ranked 17, v6_verified fleet task
    assert row["digest_match"] == MODULE["MATCH_NO"]
    assert row["ladder_rank"] == "17"
    assert row["ladder_verdict"] == "clean"
    assert row["ladder_digest"] != row["fleet_digest"]
    for other in _rows():
        if other["digest_match"] == MODULE["MATCH_NO"]:
            assert other["ladder_digest"] != other["fleet_digest"]
        elif other["digest_match"] == MODULE["MATCH_YES"]:
            assert other["ladder_digest"] == other["fleet_digest"]


def test_a_cell_that_ran_the_fleet_digest_is_marked_yes() -> None:
    ranked = MODULE["ladder_cells"]()
    matched = {
        task: row for task, row in _by_task().items() if row["digest_match"] == MODULE["MATCH_YES"]
    }
    assert matched
    for task, row in matched.items():
        assert row["ladder_digest"] == row["fleet_digest"] == ranked[task]["ladder_digest"]


def test_an_unscored_cell_keeps_its_recorded_verdict() -> None:
    # An unscored cell is a measurement of the fleet bytes without a reward; the
    # join reports it rather than dropping it into "no cell".
    row = _by_task()["format-code-task-000928"]
    assert row["ladder_verdict"] == "unscored"
    assert row["ladder_basis"] == "unscored"
    assert row["digest_match"] == MODULE["MATCH_YES"]


def test_ranked_tasks_outside_the_fleet_have_no_row() -> None:
    rows = _by_task()
    assert "format-code-task-000495" not in rows
    assert "format-code-task-001694" not in rows


def test_join_rows_carry_no_host_paths() -> None:
    offenders = [
        row["task_id"]
        for row in _rows()
        if any("/" in value or "~" in value for value in row.values())
    ]
    assert not offenders


def test_readme_counts_equal_the_join_csv() -> None:
    assert readme_counts() == MODULE["bucket_counts"](_rows())


def test_builder_joins_a_cell_at_another_digest_in_isolation(join_root: Path) -> None:
    assert MODULE["build"](join_root) == [
        {
            "task_id": TASK,
            "language": "python",
            "bucket": "v6_verified",
            "fleet_digest": FLEET_DIGEST,
            "ladder_rank": "1",
            "ladder_verdict": "clean",
            "ladder_basis": "graded",
            "ladder_digest": CELL_DIGEST,
            "digest_match": MODULE["MATCH_NO"],
        }
    ]


def test_builder_marks_a_cell_at_the_fleet_digest_yes(join_root: Path) -> None:
    rewrite(join_root / MODULE["CLEAN_MANIFEST"], CELL_DIGEST, FLEET_DIGEST)
    rewrite(join_root / LEDGER, CELL_DIGEST, FLEET_DIGEST)
    rows = MODULE["build"](join_root)
    assert rows[0]["digest_match"] == MODULE["MATCH_YES"]
    assert rows[0]["ladder_digest"] == rows[0]["fleet_digest"] == FLEET_DIGEST


@pytest.mark.parametrize(
    ("relative", "old", "new", "message"),
    [
        (MODULE["FLEET_MANIFEST"], FLEET_DIGEST, "sha256:" + "c" * 64, "disagrees with manifest"),
        (
            MODULE["CLEAN_MANIFEST"],
            CELL_DIGEST,
            "sha256:" + "d" * 64,
            "disagrees with research/experiments/mimo-clean-v6/manifest.csv",
        ),
        (
            LEDGER,
            MODULE["CLEAN_MANIFEST"],
            "research/experiments/other/manifest.csv",
            "is bound to",
        ),
        (LEDGER, TASK, "format-code-task-999999", "entries for"),
    ],
    ids=["census-vs-fleet-manifest", "cell-vs-clean-manifest", "ledger-manifest", "missing-entry"],
)
def test_a_disagreeing_input_is_refused(
    join_root: Path, relative: str, old: str, new: str, message: str
) -> None:
    rewrite(join_root / relative, old, new)
    with pytest.raises(MODULE["JoinError"], match=re.escape(message)):
        MODULE["build"](join_root)
