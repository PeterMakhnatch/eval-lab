"""The fleet/ladder join and its README counts stay mechanical (HAR-372, HAR-389).

``research/experiments/mimo-v6-full-fleet/ladder_join.py`` is the only writer of
``fleet-ladder-join.csv`` and of the join counts quoted on the fleet README and
the MiMo route page. These tests pin what the table is relied on for: the
committed CSV is what the committed census, fleet manifest, ranked cells and
clean-v6 manifest currently produce; every fleet task has exactly one row and a
task with no ranked cell keeps its cell columns empty; a cell that ran other
bytes is ``no`` and one that ran the fleet digest is ``yes``; the README count
table and both count sentences are those counts rather than a second
hand-maintained number, and ``--sync-docs`` rewrites both pages together or
neither; and the builder refuses inputs that disagree about a digest or a
verdict, repeat a task in a manifest, or otherwise break the contract it reads.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
import runpy
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DIR = REPO / "research/experiments/mimo-v6-full-fleet"
MODULE = runpy.run_path(str(DIR / "ladder_join.py"))
COLUMNS = MODULE["COLUMNS"]
CELL_COLUMNS = ("ladder_rank", "ladder_verdict", "ladder_basis", "ladder_digest")
README = DIR / "README.md"
COUNTS_HEADING = MODULE["COUNTS_HEADING"]
TASK = "format-code-task-000001"
LEDGER = "research/experiments/mimo-clean-v6/ladder-fixture.json"
FLEET_DIGEST = "sha256:" + "a" * 64
CELL_DIGEST = "sha256:" + "b" * 64
ROUTE_PAGE = REPO / MODULE["ROUTE_PAGE"]
SYNCED_PAGES = (MODULE["FLEET_README"], MODULE["ROUTE_PAGE"])
OPEN_ITEMS = "## Open items"
ROUTE_COUNTS = re.compile(
    r"holds (?P<tasks>\d[\d,]*) fleet tasks, "
    r"(?P<with_cell>\d[\d,]*) with a ranked cell, and "
    r"(?P<at_digest>\d[\d,]*) whose cell ran the fleet digest; "
    r"(?P<no_cell>\d[\d,]*) have no ranked cell and "
    r"(?P<other>\d[\d,]*) cells ran other bytes"
)


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


def counts_section(path: Path = README) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(COUNTS_HEADING)
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)
    return "\n".join(section)


def readme_counts(path: Path = README) -> list[tuple[str, int, int, int]]:
    """The README count table as (bucket, tasks, with a cell, at the fleet digest)."""
    table: list[list[str]] = []
    for line in counts_section(path).splitlines():
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


def other_bytes(path: Path = README) -> tuple[int, ...]:
    """The README other-bytes sentence as (with a cell, at digest, with, at, other)."""
    match = re.search(MODULE["OTHER_BYTES"], counts_section(path))
    assert match is not None, "the count section must state the other-bytes count"
    return tuple(int(value.replace(",", "")) for value in match.groups())


def open_items(path: Path = ROUTE_PAGE) -> str:
    sections = path.read_text(encoding="utf-8").split(OPEN_ITEMS)
    assert len(sections) == 2, f"{path} must contain exactly one {OPEN_ITEMS!r}"
    section = re.split(r"^## ", sections[1], maxsplit=1, flags=re.MULTILINE)[0]
    return " ".join(section.split())


def route_counts(path: Path = ROUTE_PAGE) -> tuple[int, ...]:
    """The route sentence as (tasks, with a cell, at digest, no cell, other bytes)."""
    match = ROUTE_COUNTS.search(open_items(path))
    assert match is not None, "Open items must quote the fleet/ladder join counts"
    return tuple(
        int(match[name].replace(",", ""))
        for name in ("tasks", "with_cell", "at_digest", "no_cell", "other")
    )


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


def assert_pages_quote(rows: Sequence[dict[str, str]], root: Path = REPO) -> None:
    """Both pages quote ``rows``: the table, the other-bytes and the route sentence."""
    counts = MODULE["bucket_counts"](rows)
    _, tasks, with_cell, at_digest = next(entry for entry in counts if entry[0] == "all")
    other = with_cell - at_digest
    no_cell = sum(row["digest_match"] == MODULE["MATCH_NONE"] for row in rows)
    assert readme_counts(root / MODULE["FLEET_README"]) == counts
    assert other_bytes(root / MODULE["FLEET_README"]) == (
        with_cell,
        at_digest,
        with_cell,
        at_digest,
        other,
    )
    assert route_counts(root / MODULE["ROUTE_PAGE"]) == (
        tasks,
        with_cell,
        at_digest,
        no_cell,
        other,
    )


def test_readme_and_route_page_quote_the_join_csv() -> None:
    assert_pages_quote(_rows())


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    """A root holding byte copies of the two committed pages that quote the join."""
    for relative in SYNCED_PAGES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    return tmp_path


def flipped_rows() -> list[dict[str, str]]:
    """The committed join with one cell at other bytes relabelled as the fleet digest."""
    rows = _rows()
    row = next(row for row in rows if row["digest_match"] == MODULE["MATCH_NO"])
    row["digest_match"] = MODULE["MATCH_YES"]
    return rows


def test_flipping_one_join_row_rewrites_both_pages_together(docs_root: Path) -> None:
    rows = flipped_rows()
    before = {relative: (docs_root / relative).read_bytes() for relative in SYNCED_PAGES}
    assert MODULE["sync_docs"](rows, docs_root) == [docs_root / page for page in SYNCED_PAGES]
    for relative in SYNCED_PAGES:
        assert (docs_root / relative).read_bytes() != before[relative]
    assert_pages_quote(rows, docs_root)
    tasks, with_cell, at_digest, no_cell, other = route_counts()
    assert route_counts(docs_root / MODULE["ROUTE_PAGE"]) == (
        tasks,
        with_cell,
        at_digest + 1,
        no_cell,
        other - 1,
    )
    # The flipped rows are now quoted, so syncing them again writes nothing.
    assert MODULE["sync_docs"](rows, docs_root) == []
    assert not list(docs_root.rglob(".*.tmp"))


def test_sync_docs_on_the_committed_tree_is_byte_identical_twice(
    docs_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI reads the committed join through HERE and writes the pages under ROOT;
    # pointing ROOT at byte copies proves the committed tree is already synced.
    namespace = MODULE["main"].__globals__
    assert namespace["ROOT"] == REPO
    monkeypatch.setitem(namespace, "ROOT", docs_root)
    for _ in range(2):
        assert MODULE["main"](["--sync-docs"]) == 0
        assert "sync-docs rewrote nothing" in capsys.readouterr().out
        for relative in SYNCED_PAGES:
            assert (docs_root / relative).read_bytes() == (REPO / relative).read_bytes()


def test_sync_docs_refuses_a_stale_join_and_excludes_check(
    docs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = MODULE["main"].__globals__
    stale = docs_root / "stale"
    stale.mkdir()
    (stale / MODULE["JOIN_NAME"]).write_text("stale\n", encoding="utf-8")
    monkeypatch.setitem(namespace, "HERE", stale)
    monkeypatch.setitem(namespace, "ROOT", docs_root)
    assert MODULE["main"](["--sync-docs"]) == 1
    for relative in SYNCED_PAGES:
        assert (docs_root / relative).read_bytes() == (REPO / relative).read_bytes()
    with pytest.raises(SystemExit) as raised:
        MODULE["main"](["--check", "--sync-docs"])
    assert raised.value.code == 2


def test_route_page_links_both_subset_receipts() -> None:
    paragraph = next(
        bullet for bullet in open_items().split(" - ") if "ladder-graded-subset.json" in bullet
    )
    assert "ladder-graded-subset-p0.json" in paragraph
    targets = {Path(target).name: target for target in re.findall(r"\]\((\.\./[^)]+)\)", paragraph)}
    for receipt in ("ladder-graded-subset.json", "ladder-graded-subset-p0.json"):
        assert receipt in targets, f"Open items must link {receipt}"
        assert (ROUTE_PAGE.parent / targets[receipt]).resolve().is_file()


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


def change(relative: str, old: str, new: str) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        rewrite(root / relative, old, new)

    return apply


def append_row(relative: str, row: Sequence[str]) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        with (root / relative).open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerow(row)

    return apply


def remove(relative: str) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        (root / relative).unlink()

    return apply


def truncate(relative: str) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        (root / relative).write_text("", encoding="utf-8")

    return apply


#: One case per refusal the builder can report: the mutation, and a fragment of
#: the message that must name the broken input. A manifest row that repeats a
#: task is refused rather than letting the later row overwrite the earlier one,
#: so the fleet and the clean manifest each get their own case.
REFUSALS: list[tuple[str, Callable[[Path], None], str]] = [
    ("missing-input", remove(MODULE["CENSUS"]), "input is missing: "),
    ("empty-input", truncate(MODULE["CENSUS"]), "input is empty: "),
    ("missing-ledger", remove(LEDGER), "cell ledger is missing: "),
    (
        "census-repeats-a-task",
        append_row(MODULE["CENSUS"], (TASK, "python", "v6_verified", FLEET_DIGEST)),
        "repeats a task_id",
    ),
    (
        "census-task-absent-from-fleet-manifest",
        change(MODULE["CENSUS"], TASK, "format-code-task-999999"),
        "is in the census but not in",
    ),
    (
        "census-vs-fleet-manifest",
        change(MODULE["FLEET_MANIFEST"], FLEET_DIGEST, "sha256:" + "c" * 64),
        "census digest",
    ),
    (
        "fleet-manifest-repeats-a-task",
        append_row(MODULE["FLEET_MANIFEST"], (TASK, FLEET_DIGEST)),
        f"{MODULE['FLEET_MANIFEST']} repeats {TASK}",
    ),
    (
        "progress-ranks-a-task-twice",
        append_row(MODULE["PROGRESS"], (2, TASK, "clean", "graded", LEDGER)),
        "ranks a task twice",
    ),
    (
        "progress-repeats-a-rank",
        append_row(MODULE["PROGRESS"], (1, "format-code-task-000002", "clean", "graded", LEDGER)),
        "repeats a rank",
    ),
    (
        "ledger-manifest",
        change(LEDGER, MODULE["CLEAN_MANIFEST"], "research/experiments/other/manifest.csv"),
        "is bound to",
    ),
    ("missing-entry", change(LEDGER, TASK, "format-code-task-999999"), "entries for"),
    ("ledger-records-no-digest", change(LEDGER, CELL_DIGEST, ""), "records no digest"),
    (
        "ledger-verdict",
        change(LEDGER, '"verdict": "clean"', '"verdict": "unscored"'),
        "ledger verdict",
    ),
    (
        "clean-manifest-repeats-a-task",
        append_row(MODULE["CLEAN_MANIFEST"], (TASK, CELL_DIGEST)),
        f"{MODULE['CLEAN_MANIFEST']} repeats {TASK}",
    ),
    (
        "ranked-but-absent-from-clean-manifest",
        change(MODULE["CLEAN_MANIFEST"], TASK, "format-code-task-999999"),
        "is ranked but absent from",
    ),
    (
        "cell-vs-clean-manifest",
        change(MODULE["CLEAN_MANIFEST"], CELL_DIGEST, "sha256:" + "d" * 64),
        "cell digest",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_a_disagreeing_input_is_refused(
    join_root: Path, mutate: Callable[[Path], None], message: str
) -> None:
    mutate(join_root)
    with pytest.raises(MODULE["JoinError"], match=re.escape(message)):
        MODULE["build"](join_root)


def append_text(relative: str, text: str) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        with (root / relative).open("a", encoding="utf-8") as handle:
            handle.write(text)

    return apply


def repeat_count_table(root: Path) -> None:
    """Copy the count table directly below itself, with no blank line between them."""
    page = root / MODULE["FLEET_README"]
    text = page.read_text(encoding="utf-8")
    table = re.search(MODULE["TABLE"], text)
    assert table is not None
    page.write_text(text[: table.end()] + table.group() + text[table.end() :], encoding="utf-8")


def move_other_bytes_sentence(root: Path) -> None:
    """Move the other-bytes sentence out of the counts section, to the end of the page."""
    page = root / MODULE["FLEET_README"]
    text = page.read_text(encoding="utf-8")
    sentence = re.search(MODULE["OTHER_BYTES"], text)
    assert sentence is not None
    remainder = text[: sentence.start()] + text[sentence.end() :]
    page.write_text(f"{remainder}\n{sentence.group()}\n", encoding="utf-8")


FLEET_README = MODULE["FLEET_README"]
ROUTE = MODULE["ROUTE_PAGE"]

#: One case per way a page can lose its unique anchor, and the message naming it.
#: Each case runs against rows that would otherwise rewrite both pages.
DOC_REFUSALS: list[tuple[str, Callable[[Path], None], str]] = [
    ("missing-route-page", remove(ROUTE), "documentation page is missing: "),
    (
        "missing-counts-heading",
        change(FLEET_README, COUNTS_HEADING, "## Ladder cells"),
        f"expected one {COUNTS_HEADING!r}, found 0",
    ),
    (
        "repeated-counts-heading",
        append_text(FLEET_README, f"\n{COUNTS_HEADING}\n"),
        f"expected one {COUNTS_HEADING!r}, found 2",
    ),
    (
        "missing-count-table",
        change(FLEET_README, "| Bucket |", "| Group |"),
        "count table', found 0",
    ),
    (
        "repeated-count-table",
        repeat_count_table,
        "count table', found 2",
    ),
    (
        "missing-other-bytes-sentence",
        change(FLEET_README, "ran other bytes.", "ran elsewhere."),
        "one 'other-bytes sentence', found 0",
    ),
    (
        # Outside the counts section, where a section-only match would leave it stale.
        "repeated-other-bytes-sentence",
        append_text(
            FLEET_README,
            "\nOf the 98 fleet tasks with a ranked cell, 15 ran the fleet digest and "
            "98 - 15 = 83 ran other bytes.\n",
        ),
        "other-bytes sentence', found 2",
    ),
    (
        "other-bytes-sentence-outside-counts-section",
        move_other_bytes_sentence,
        f"{COUNTS_HEADING} other-bytes sentence', found 0",
    ),
    (
        "missing-route-sentence",
        change(ROUTE, "cells ran other bytes", "cells ran elsewhere"),
        "join sentence', found 0",
    ),
    (
        "repeated-route-sentence",
        append_text(
            ROUTE,
            "\nIt holds 1 fleet tasks, 1 with a ranked cell, and 1 whose cell ran the "
            "fleet digest; 0 have no ranked cell and 0 cells ran other bytes.\n",
        ),
        "join sentence', found 2",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [case[1:] for case in DOC_REFUSALS],
    ids=[case[0] for case in DOC_REFUSALS],
)
def test_a_missing_or_repeated_anchor_writes_no_page(
    docs_root: Path, mutate: Callable[[Path], None], message: str
) -> None:
    mutate(docs_root)
    before = {
        relative: (docs_root / relative).read_bytes()
        for relative in SYNCED_PAGES
        if (docs_root / relative).is_file()
    }
    with pytest.raises(MODULE["JoinError"], match=re.escape(message)):
        MODULE["sync_docs"](flipped_rows(), docs_root)
    assert {relative: (docs_root / relative).read_bytes() for relative in before} == before


def test_every_join_error_needs_a_refusal_case() -> None:
    """A raise without a case, or a case whose raise is gone, fails here."""
    tree = ast.parse((DIR / "ladder_join.py").read_text(encoding="utf-8"))
    sites = {
        function.name: sum(
            1
            for node in ast.walk(function)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "JoinError"
        )
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
    }
    documentation = {"_read_page", "_anchor"}
    # 14 contract checks at f8cc7ce60, plus the manifest repeat check: one site,
    # which the fleet and the clean manifest case each exercise.
    assert sum(count for name, count in sites.items() if name not in documentation) == 15
    assert len(REFUSALS) == 16
    assert len({message for _, _, message in REFUSALS}) == len(REFUSALS)
    # A missing page, and one anchor check that every missing or repeated anchor hits.
    assert sum(sites[name] for name in documentation) == 2
    assert len(DOC_REFUSALS) == 10
    assert len({message for _, _, message in DOC_REFUSALS}) == len(DOC_REFUSALS)
