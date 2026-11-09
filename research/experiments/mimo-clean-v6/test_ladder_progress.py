"""The v6 ladder progress table stays mechanical and names each verdict's basis."""

from __future__ import annotations

import collections
import csv
import hashlib
import importlib.util
import io
import json
import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = REPO_ROOT / "research" / "experiments" / "mimo-clean-v6"
_SPEC = importlib.util.spec_from_file_location("ladder_progress", EXPERIMENT / "ladder_progress.py")
assert _SPEC is not None and _SPEC.loader is not None
MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MODULE)

assemble = MODULE.assemble
build = MODULE.build
classify_basis = MODULE.classify_basis
render = MODULE.render
ProgressError = MODULE.ProgressError
SCHEMA = MODULE.CELLS_SCHEMA
RANKING = MODULE.RANKING


def _committed() -> list[dict[str, str]]:
    text = (EXPERIMENT / "ladder-progress.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def test_ladder_progress_committed_table_reproduces_from_cell_files() -> None:
    committed = (EXPERIMENT / "ladder-progress.csv").read_text(encoding="utf-8")
    assert render(build(REPO_ROOT, cells_dir=EXPERIMENT)) == committed


def test_ladder_progress_readme_refusal_signature_counts_match_the_table() -> None:
    counted = collections.Counter(
        row["refusal_hit"].rsplit("/", 1)[-1] for row in _committed() if row["refusal_hit"]
    )
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Refusal signatures and attacks executed", 1)[1].split("\n## ", 1)[0]
    documented = {
        cells[1].strip("` "): int(cells[2])
        for cells in (
            [part.strip() for part in line.strip("|").split("|")]
            for line in section.splitlines()
            if line.startswith("| `")
        )
    }
    assert documented == dict(counted)


# Both route pages quote the v6 basis counts in one fixed sentence shape, so the
# parse stays a single regex:
#
#   v6 basis (100 ranks): <covered> covered, <clean> clean,
#   <tamper-refusal> tamper-refusal, <graded> graded, <unscored> unscored,
#   <not run> not run
#
# Each page carries that sentence exactly once, wrapped anywhere. Two numbers do
# not come straight from ``basis_counts``: covered is every rank minus ``none``,
# and clean is every ``clean`` verdict. Edit both pages together, never one alone.
BASIS_SENTENCE = re.compile(
    r"v6 basis \(100 ranks\): (\d+) covered, (\d+) clean, (\d+) tamper-refusal, "
    r"(\d+) graded, (\d+) unscored, (\d+) not run"
)
BASIS_PAGES = (REPO_ROOT / "docs" / "mimo" / "README.md", REPO_ROOT / "README.md")
BASIS_FIELDS = ("covered", "clean", "tamper-refusal", "graded", "unscored", "not run")


def _quoted_basis() -> dict[str, int]:
    """The six numbers both pages quote, from the committed ladder table."""
    rows = _committed()
    counts = MODULE.basis_counts(rows)
    not_run = counts.get("none", 0)
    return {
        "covered": len(rows) - not_run,
        "clean": sum(1 for row in rows if row["verdict"] == "clean"),
        "tamper-refusal": counts.get("tamper-refusal", 0),
        "graded": counts.get("graded", 0),
        "unscored": counts.get("unscored", 0),
        "not run": not_run,
    }


def test_ladder_progress_quoted_basis_counts_match_the_committed_table() -> None:
    expected = _quoted_basis()
    for page in BASIS_PAGES:
        text = " ".join(page.read_text(encoding="utf-8").split())
        found = BASIS_SENTENCE.findall(text)
        assert len(found) == 1, f"{page.relative_to(REPO_ROOT)} must quote the sentence once"
        quoted = dict(zip(BASIS_FIELDS, map(int, found[0]), strict=True))
        assert quoted == expected, page.relative_to(REPO_ROOT)


def test_ladder_progress_both_pages_name_the_graded_go_cells() -> None:
    go_cells = [
        row["task_id"].rsplit("-", 1)[-1]
        for row in _committed()
        if row["basis"] == "graded" and row["language"] == "go"
    ]
    assert go_cells
    for page in BASIS_PAGES:
        text = page.read_text(encoding="utf-8")
        missing = [cell for cell in go_cells if f"`{cell}`" not in text]
        assert not missing, (
            f"{page.relative_to(REPO_ROOT)} does not name graded Go cells {missing}"
        )


def test_ladder_progress_readme_basis_table_matches_the_committed_table() -> None:
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    section = readme.split("Basis counts in the committed table", 1)[1].split("\n##", 1)[0]
    header = re.search(r"\(100 ranks, (\d+) covered;", section)
    assert header is not None
    documented: dict[str, int] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [part.strip() for part in line.strip("|").split("|")]
        documented[cells[0].strip("` ")] = int(cells[1].split()[0])
    expected = _quoted_basis()
    assert int(header.group(1)) == expected["covered"]
    assert documented == {
        "tamper-refusal": expected["tamper-refusal"],
        "graded": expected["graded"],
        "unscored": expected["unscored"],
        "none": expected["not run"],
    }


def _summary_section() -> str:
    """The pipe-table lines of the README's summary section, heading-bounded."""
    heading = f"{MODULE.SUMMARY_HEADING}\n"
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    assert readme.count(heading) == 1
    body = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    return "\n".join(line for line in body.splitlines() if line.startswith("|"))


def test_ladder_progress_readme_summary_section_matches_the_rendered_table() -> None:
    assert _summary_section() == MODULE.render_summary(_committed()).rstrip("\n")


def _slice_rows() -> list[dict[str, object]]:
    return MODULE.read_slice_tables((EXPERIMENT / "README.md").read_text(encoding="utf-8"))


def test_ladder_progress_readme_slice_tables_match_the_committed_table() -> None:
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    assert MODULE.check_slice_tables(readme, _committed()) >= 60


def test_ladder_progress_no_task_appears_in_two_slice_tables() -> None:
    headings: dict[str, list[str]] = collections.defaultdict(list)
    for table in _slice_rows():
        headings[str(table["task"])].append(str(table["heading"]))
    repeated = {task: seen for task, seen in headings.items() if len(seen) > 1}
    assert not repeated, repeated


def test_ladder_progress_a_slice_table_wall_edit_is_refused(tmp_path: Path) -> None:
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    row = "| 46 | `000878` | clean | tamper refusal | 14/14 | 7.4 min CLI |"
    assert readme.count(row) == 1
    copy = tmp_path / "README.md"
    copy.write_text(readme.replace(row, row.replace("7.4 min", "9.9 min")), encoding="utf-8")
    edited = copy.read_text(encoding="utf-8")
    assert edited != readme
    with pytest.raises(ProgressError, match="000878") as refused:
        MODULE.check_slice_tables(edited, _committed())
    assert "9.9" in str(refused.value)
    assert "445.9" in str(refused.value)


def _readme_table(heading: str) -> str:
    """The pipe-table lines of one README section, heading-bounded."""
    readme = (EXPERIMENT / "README.md").read_text(encoding="utf-8")
    section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    return "\n".join(line for line in section.splitlines() if line.startswith("|")) + "\n"


@pytest.mark.parametrize(
    ("name", "heading"),
    [
        ("ladder-p2-ranks52-57.json", "## P2 ranks 52–57 ladder cells"),
        ("ladder-p2-ranks46-51.json", "## P2 ranks 46–51 ladder cells"),
    ],
)
def test_ladder_progress_render_slice_reproduces_the_readme_table(
    name: str, heading: str
) -> None:
    assert MODULE.render_slice(EXPERIMENT / name) == _readme_table(heading)


@pytest.mark.parametrize(
    "path", sorted(EXPERIMENT.glob("ladder-p2-ranks*.json")), ids=lambda path: path.name
)
def test_ladder_progress_render_slice_agrees_with_the_committed_table(path: Path) -> None:
    tasks = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    assert MODULE.check_slice_tables(
        "## rendered ladder cells\n" + MODULE.render_slice(path), _committed()
    ) == len(tasks)


def test_ladder_progress_render_slice_names_an_unscored_prior_attempt(
    tmp_path: Path,
) -> None:
    data = json.loads((EXPERIMENT / "ladder-p2-ranks52-57.json").read_text(encoding="utf-8"))
    task = next(task for task in data["tasks"] if task["ranking"]["rank"] == 55)
    path = tmp_path / "ladder-tmp.json"
    path.write_text(
        json.dumps({"schema": SCHEMA, "card": data["card"], "tasks": [task]}),
        encoding="utf-8",
    )
    rendered = MODULE.render_slice(path)
    assert "| attempt 2 published; unscored attempt 1 published and retained |" in rendered


def test_ladder_progress_render_slice_refuses_a_cell_at_another_digest(
    tmp_path: Path,
) -> None:
    data = json.loads((EXPERIMENT / "ladder-p2-ranks52-57.json").read_text(encoding="utf-8"))
    task = data["tasks"][0]
    task["final_digest"] = "sha256:stale"
    path = tmp_path / "ladder-tmp.json"
    path.write_text(
        json.dumps({"schema": SCHEMA, "card": data["card"], "tasks": [task]}),
        encoding="utf-8",
    )
    with pytest.raises(ProgressError, match="sha256:stale"):
        MODULE.render_slice(path)


def test_ladder_progress_render_slice_cli_prints_the_table(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert MODULE.main(["--render-slice", "ladder-p2-ranks52-57.json"]) == 0
    printed = capsys.readouterr().out
    assert printed == _readme_table("## P2 ranks 52–57 ladder cells")


def test_ladder_progress_summary_accounts_for_every_row_once() -> None:
    rows = _committed()
    entries = MODULE.summary(rows)
    assert {entry["language"] for entry in entries} == {row["language"] for row in rows}
    for entry in entries:
        assert sum(entry["bases"].values()) == entry["covered"]
    assert sum(entry["covered"] + entry["uncovered"] for entry in entries) == len(rows)
    assert sum(entry["uncovered"] for entry in entries) == sum(
        1 for row in rows if row["basis"] == "none"
    )


def test_ladder_progress_summary_total_row_sums_the_languages() -> None:
    entries = MODULE.summary(_committed())
    total = [
        cell.strip()
        for cell in MODULE.render_summary(_committed()).splitlines()[-1].strip("|").split("|")
    ]
    assert total[0] == "total"
    assert int(total[1]) == sum(entry["covered"] for entry in entries)
    assert int(total[2]) == sum(entry["uncovered"] for entry in entries)
    assert total[5] == f"{sum(entry['projected_hours'] for entry in entries):.2f}"


def _summary_row(task_id: str, language: str, basis: str, wall: str) -> dict[str, str]:
    return {
        "task_id": task_id,
        "language": language,
        "basis": basis,
        "verdict": "none" if basis == "none" else "clean",
        "wall_seconds": wall,
    }


def test_ladder_progress_summary_takes_the_refusal_median_when_a_refusal_exists() -> None:
    rows = [
        _summary_row("t1", "python", "tamper-refusal", "100"),
        _summary_row("t2", "python", "tamper-refusal", "300"),
        _summary_row("t3", "python", "graded", "9000"),
    ]
    entry = MODULE.summary(rows)[0]
    assert (entry["median_seconds"], entry["max_seconds"]) == (200.0, 300.0)


def test_ladder_progress_summary_falls_back_to_the_graded_median_and_counts_unscored() -> None:
    rows = [
        _summary_row("t1", "go", "graded", "100"),
        _summary_row("t2", "go", "graded", "300"),
        _summary_row("t3", "go", "unscored", "9000"),
    ]
    entry = MODULE.summary(rows)[0]
    assert (entry["median_seconds"], entry["max_seconds"]) == (200.0, 300.0)
    assert entry["bases"] == {"graded": 2, "unscored": 1}
    assert (entry["covered"], entry["uncovered"]) == (3, 0)


def test_ladder_progress_summary_projects_uncovered_rows_at_the_language_median() -> None:
    rows = [
        _summary_row("t1", "typescript", "graded", "720"),
        _summary_row("t2", "typescript", "none", ""),
        _summary_row("t3", "typescript", "none", ""),
    ]
    entry = MODULE.summary(rows)[0]
    assert entry["uncovered"] == 2
    assert entry["projected_hours"] == pytest.approx(2 * 720 / 3600)


def test_ladder_progress_summary_refuses_a_covered_row_without_a_wall_time() -> None:
    rows = [_summary_row("t1", "go", "graded", "")]
    with pytest.raises(ProgressError, match="t1"):
        MODULE.summary(rows)


def test_ladder_progress_summary_refuses_a_language_with_no_reference_basis() -> None:
    rows = [
        _summary_row("t1", "go", "unscored", "448"),
        _summary_row("t2", "go", "none", ""),
    ]
    with pytest.raises(ProgressError, match="go"):
        MODULE.summary(rows)


def test_ladder_progress_check_and_summary_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as refused:
        MODULE.main(["--check", "--summary"])
    assert refused.value.code == 2


def test_ladder_progress_has_exactly_one_row_per_ranking_rank() -> None:
    with (REPO_ROOT / RANKING).open(encoding="utf-8", newline="") as handle:
        expected = [row["task_id"] for row in csv.DictReader(handle)][:100]
    rows = _committed()
    assert [row["rank"] for row in rows] == [str(n) for n in range(1, 101)]
    assert [row["task_id"] for row in rows] == expected
    assert {row["verdict"] for row in rows if row["source_file"] == ""} == {"none"}


@pytest.mark.parametrize(
    ("decision", "basis"),
    [
        ("tamper signature in agent diff", "tamper-refusal"),
        ("rc=1 runner=RUNNER=pytest", "graded"),
        (None, "unscored"),
        ("", "unscored"),
        ("something else entirely", "unscored"),
    ],
)
def test_ladder_progress_basis_comes_from_verifier_decision(decision: object, basis: str) -> None:
    assert classify_basis(decision) == basis


def _ranking(n: int = 3) -> list[dict[str, str]]:
    return [
        {"rank": str(i), "task_id": f"t{i}", "phase": "P", "language": "python"}
        for i in range(1, n + 1)
    ]


def _cell(
    rank: int,
    decision: str | None,
    finished: str = "",
    verdict: str = "clean",
    digest: str | None = None,
    matches: bool = True,
) -> dict:
    refused = decision is not None and decision.startswith("tamper signature")
    executed = 14 if refused else 9
    return {
        "task_id": f"t{rank}",
        "ranking": {"rank": rank, "phase": "P"},
        "ladder_version": "1.4.0",
        "verdict": verdict,
        "reward": 0.0,
        "cracking_attacks": [],
        "verifier_decision": decision,
        "verifier_tamper_hit": f"tamper \\b_pytest\\b in pkg{rank}/__init__.py" if refused else None,
        "attacks_executed": executed,
        "attacks_total": executed,
        "attacks_not_executed": {},
        "wall_seconds": 10 * rank,
        "finished_utc": finished,
        "final_digest": f"sha256:cell{rank}" if digest is None else digest,
        "digest_matches_manifest": matches,
        "_source_file": f"f{rank}.json",
    }


def _manifest(n: int = 3) -> dict[str, str]:
    return {f"t{i}": f"sha256:cell{i}" for i in range(1, n + 1)}


def _cells_file(tmp_path: Path, cells: list[dict], totals: dict | None = None) -> None:
    data: dict = {
        "schema": SCHEMA,
        "tasks": [{k: v for k, v in cell.items() if k != "_source_file"} for cell in cells],
    }
    if totals is not None:
        data["totals"] = totals
    (tmp_path / "ladder-a.json").write_text(json.dumps(data), encoding="utf-8")


def _cell_file(tmp_path: Path, cell: dict, totals: dict | None = None) -> None:
    _cells_file(tmp_path, [cell], totals)


def test_ladder_progress_a_refusal_cell_names_its_file_and_a_graded_cell_does_not() -> None:
    rows = assemble(
        _ranking(3),
        [_cell(1, "tamper signature in agent diff"), _cell(2, "rc=1 runner=pytest")],
    )
    assert [(r["basis"], r["refusal_hit"], r["attacks_executed"]) for r in rows] == [
        ("tamper-refusal", "pkg1/__init__.py", "14"),
        ("graded", "", "9"),
        ("none", "", ""),
    ]


def test_ladder_progress_a_refusal_without_a_named_file_is_refused() -> None:
    cell = _cell(1, "tamper signature in agent diff")
    cell["verifier_tamper_hit"] = None
    with pytest.raises(ProgressError, match="t1"):
        assemble(_ranking(1), [cell])


def test_ladder_progress_uncovered_ranks_read_verdict_none() -> None:
    rows = assemble(_ranking(), [_cell(2, "rc=1 runner=x")])
    assert [(r["rank"], r["verdict"], r["basis"]) for r in rows] == [
        ("1", "none", "none"),
        ("2", "clean", "graded"),
        ("3", "none", "none"),
    ]
    assert (rows[1]["wall_seconds"], rows[1]["source_file"]) == ("20", "f2.json")


def test_ladder_progress_a_later_rerun_of_a_rank_supersedes_the_earlier_cell() -> None:
    old = _cell(1, "tamper signature in agent diff", finished="2026-11-01T00:00:00Z")
    new = _cell(1, "rc=0 runner=x", finished="2026-11-02T00:00:00Z")
    [row, *_] = assemble(_ranking(), [new, old])
    assert row["basis"] == "graded"


def test_ladder_progress_a_cell_for_a_different_task_is_refused() -> None:
    cell = _cell(1, None)
    cell["task_id"] = "other"
    with pytest.raises(ProgressError, match="rank 1"):
        assemble(_ranking(), [cell])


def test_ladder_progress_cell_files_with_another_schema_are_ignored(tmp_path: Path) -> None:
    cell = {k: v for k, v in _cell(1, None).items() if k != "_source_file"}
    (tmp_path / "ladder-a.json").write_text(
        json.dumps({"schema": SCHEMA, "tasks": [cell]}), encoding="utf-8"
    )
    (tmp_path / "ladder-b.json").write_text(
        json.dumps({"schema": "other/v1", "tasks": [cell]}), encoding="utf-8"
    )
    cells = MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    assert [c["_source_file"] for c in cells] == ["ladder-a.json"]


def _subset(
    task_id: str, n: int, verdict: str, reward: float | None
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "subset": n,
        "verdict": verdict,
        "reward": reward,
        "_source_file": "research/experiments/mimo-clean-v6/ladder-sub.json",
    }


def test_ladder_progress_a_task_with_a_subset_cell_shows_the_latest_subset() -> None:
    rows = assemble(
        _ranking(),
        [_cell(1, "tamper signature in agent diff")],
        [
            _subset("t1", 1, "still-refused", 0.0),
            _subset("t1", 2, "graded-clean", 0.0),
        ],
    )
    assert (rows[0]["subset_verdict"], rows[0]["subset_reward"]) == ("graded-clean", "0.0")
    assert (rows[1]["subset_verdict"], rows[1]["subset_reward"]) == ("none", "")
    assert (rows[2]["subset_verdict"], rows[2]["subset_reward"]) == ("none", "")


def test_ladder_progress_a_repeated_subset_number_takes_the_last_listed() -> None:
    rows = assemble(
        _ranking(1),
        [],
        [_subset("t1", 1, "unscored", None), _subset("t1", 1, "still-refused", 0.0)],
    )
    assert (rows[0]["subset_verdict"], rows[0]["subset_reward"]) == ("still-refused", "0.0")


def test_ladder_progress_an_unscored_subset_reward_reads_empty() -> None:
    rows = assemble(_ranking(1), [], [_subset("t1", 2, "unscored", None)])
    assert (rows[0]["subset_verdict"], rows[0]["subset_reward"]) == ("unscored", "")


def test_ladder_progress_subset_columns_read_none_without_a_subset_file(tmp_path: Path) -> None:
    subsets = MODULE.read_subset_cells(tmp_path, tmp_path, manifest=_manifest())
    rows = assemble(_ranking(), [], subsets)
    assert [(row["subset_verdict"], row["subset_reward"]) for row in rows] == [("none", "")] * 3


def test_ladder_progress_each_reader_keeps_only_its_schema(tmp_path: Path) -> None:
    subset_file = {
        "schema": MODULE.SUBSET_CELLS_SCHEMA,
        "tasks": [
            {
                "task_id": "t1",
                "final_digest": "sha256:cell1",
                "digest_matches_manifest": True,
                "subsets": [{"subset": 1, "verdict": "still-refused", "reward": 0.0}],
            }
        ],
    }
    cell_file = {
        "schema": SCHEMA,
        "tasks": [{k: v for k, v in _cell(1, None).items() if k != "_source_file"}],
    }
    (tmp_path / "ladder-sub.json").write_text(json.dumps(subset_file), encoding="utf-8")
    (tmp_path / "ladder-cov.json").write_text(json.dumps(cell_file), encoding="utf-8")
    cells = MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    assert [c["_source_file"] for c in cells] == ["ladder-cov.json"]
    subsets = MODULE.read_subset_cells(tmp_path, tmp_path, manifest=_manifest(1))
    assert [(s["task_id"], s["subset"], s["verdict"]) for s in subsets] == [
        ("t1", 1, "still-refused")
    ]
    assert subsets[0]["_source_file"] == "ladder-sub.json"


def test_ladder_progress_manifest_reads_each_task_digest(tmp_path: Path) -> None:
    (tmp_path / "manifest.csv").write_text(
        "task_id,final_digest\nt1,sha256:a\nt2,sha256:b\n", encoding="utf-8"
    )
    assert MODULE.read_manifest(tmp_path) == {"t1": "sha256:a", "t2": "sha256:b"}


def test_ladder_progress_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProgressError, match="manifest is missing"):
        MODULE.read_manifest(tmp_path)


def test_ladder_progress_a_repeated_manifest_task_is_refused(tmp_path: Path) -> None:
    (tmp_path / "manifest.csv").write_text(
        "task_id,final_digest\nt1,sha256:a\nt1,sha256:b\n", encoding="utf-8"
    )
    with pytest.raises(ProgressError, match="repeats a task_id"):
        MODULE.read_manifest(tmp_path)


def test_ladder_progress_a_cell_at_the_manifest_digest_is_accepted(tmp_path: Path) -> None:
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x"))
    cells = MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    assert [(c["task_id"], c["final_digest"]) for c in cells] == [("t1", "sha256:cell1")]


def test_ladder_progress_a_cell_at_another_digest_is_refused(tmp_path: Path) -> None:
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x", digest="sha256:stale"))
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "t1" in message
    assert "sha256:stale" in message
    assert "sha256:cell1" in message


def test_ladder_progress_a_cell_flagged_as_not_matching_the_manifest_is_refused(
    tmp_path: Path,
) -> None:
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x", matches=False))
    with pytest.raises(ProgressError, match="digest_matches_manifest") as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "t1" in message
    assert "sha256:cell1" in message


def test_ladder_progress_a_cell_absent_from_the_manifest_is_refused(tmp_path: Path) -> None:
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x"))
    with pytest.raises(ProgressError, match="t1: absent from manifest.csv"):
        MODULE.read_cells(tmp_path, tmp_path, manifest={})


@pytest.mark.parametrize(
    ("overrides", "dropped", "field", "detail"),
    [
        # The comparison case must fail on the comparison alone, so it drops the
        # key the sum rule reads; otherwise that rule would raise a message that
        # also names attacks_executed and the comparison branch could go untested.
        (
            {"attacks_executed": 10, "attacks_total": 9},
            ("attacks_not_executed",),
            "attacks_executed",
            "> attacks_total",
        ),
        (
            {"attacks_executed": 9, "attacks_total": 14, "attacks_not_executed": {}},
            (),
            "attacks_not_executed",
            "!= attacks_total",
        ),
        ({"attacks_executed": "9"}, (), "attacks_executed", "is not a count"),
        ({"attacks_total": "14"}, (), "attacks_total", "is not a count"),
        ({"attacks_not_executed": None}, (), "attacks_not_executed", "is not a mapping"),
    ],
)
def test_ladder_progress_attack_counts_that_do_not_add_up_are_refused(
    tmp_path: Path, overrides: dict, dropped: tuple[str, ...], field: str, detail: str
) -> None:
    cell = _cell(1, "rc=0 runner=x")
    cell.update(overrides)
    for key in dropped:
        del cell[key]
    _cell_file(tmp_path, cell)
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "t1" in message
    assert field in message
    assert detail in message


def test_ladder_progress_a_cell_without_attacks_not_executed_is_accepted(tmp_path: Path) -> None:
    cell = _cell(1, "rc=0 runner=x")
    cell["attacks_total"] = 14
    del cell["attacks_not_executed"]
    _cell_file(tmp_path, cell)
    [accepted] = MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    assert (accepted["attacks_executed"], accepted["attacks_total"]) == (9, 14)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"reward": 1.0}, "reward"),
        ({"cracking_attacks": ["tamper_source_skip"]}, "cracking_attacks"),
    ],
)
def test_ladder_progress_a_clean_verdict_with_a_contradiction_is_refused(
    tmp_path: Path, overrides: dict, field: str
) -> None:
    cell = _cell(1, "rc=1 runner=x", verdict="clean")
    cell.update(overrides)
    _cell_file(tmp_path, cell)
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "t1" in message
    assert "clean" in message
    assert field in message


@pytest.mark.parametrize("reward", [0.0, None, "0.5"])
def test_ladder_progress_a_cracked_verdict_without_reward_is_refused(
    tmp_path: Path, reward: object
) -> None:
    cell = _cell(1, "rc=0 runner=x", verdict="cracked")
    cell.update(reward=reward, cracking_attacks=["tamper_source_skip"])
    _cell_file(tmp_path, cell)
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "t1" in message
    assert "cracked" in message
    assert "reward" in message


@pytest.mark.parametrize("field", ["tasks", "clean", "cracked", "unscored"])
def test_ladder_progress_a_totals_block_that_disagrees_with_its_tasks_is_refused(
    tmp_path: Path, field: str
) -> None:
    totals = {"tasks": 1, "clean": 1, "cracked": 0, "unscored": 0}
    totals[field] = 99
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x"), totals=totals)
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "ladder-a.json" in message
    assert f"totals {field}" in message


@pytest.mark.parametrize("field", ["tasks", "clean", "cracked", "unscored"])
def test_ladder_progress_a_totals_block_missing_a_count_is_refused(
    tmp_path: Path, field: str
) -> None:
    totals = {"tasks": 1, "clean": 1, "cracked": 0, "unscored": 0}
    del totals[field]
    _cell_file(tmp_path, _cell(1, "rc=0 runner=x"), totals=totals)
    with pytest.raises(ProgressError) as refused:
        MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(1))
    message = str(refused.value)
    assert "ladder-a.json" in message
    assert field in message


def test_ladder_progress_a_totals_block_that_matches_its_tasks_is_accepted(tmp_path: Path) -> None:
    clean = _cell(1, "rc=1 runner=x")
    cracked = _cell(2, "rc=0 runner=x", verdict="cracked")
    cracked.update(reward=1.0, cracking_attacks=["tamper_source_skip"])
    totals = {"tasks": 2, "clean": 1, "cracked": 1, "unscored": 0}
    _cells_file(tmp_path, [clean, cracked], totals)
    accepted = MODULE.read_cells(tmp_path, tmp_path, manifest=_manifest(2))
    assert [cell["task_id"] for cell in accepted] == ["t1", "t2"]


def test_ladder_progress_a_subset_cell_at_another_digest_is_refused(tmp_path: Path) -> None:
    subset_file = {
        "schema": MODULE.SUBSET_CELLS_SCHEMA,
        "tasks": [
            {
                "task_id": "t1",
                "final_digest": "sha256:stale",
                "digest_matches_manifest": True,
                "subsets": [{"subset": 1, "verdict": "still-refused", "reward": 0.0}],
            }
        ],
    }
    (tmp_path / "ladder-sub.json").write_text(json.dumps(subset_file), encoding="utf-8")
    with pytest.raises(ProgressError, match="sha256:stale"):
        MODULE.read_subset_cells(tmp_path, tmp_path, manifest=_manifest(1))


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the experiment dir and ranking that ``main`` treats as the repo."""
    experiment = tmp_path / "research" / "experiments" / "mimo-clean-v6"
    experiment.mkdir(parents=True)
    for path in EXPERIMENT.glob("ladder-*.json"):
        shutil.copy(path, experiment / path.name)
    shutil.copy(EXPERIMENT / "ladder-progress.csv", experiment / "ladder-progress.csv")
    shutil.copy(EXPERIMENT / "manifest.csv", experiment / "manifest.csv")
    ranking = tmp_path / RANKING
    ranking.parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / RANKING, ranking)
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "HERE", experiment)
    return experiment


@pytest.fixture
def evidence_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A minimal cell repo without a ranking, plus an isolated host results root."""
    repo = tmp_path / "repo"
    experiment = repo / "research" / "experiments" / "mimo-clean-v6"
    experiment.mkdir(parents=True)
    manifest = "task_id,final_digest\n" + "".join(
        f"{task_id},{digest}\n" for task_id, digest in _manifest().items()
    )
    (experiment / "manifest.csv").write_text(manifest, encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(MODULE, "ROOT", repo)
    monkeypatch.setattr(MODULE, "HERE", experiment)
    return experiment, results


ATTEMPTS_BYTES = b'{"attacks": []}\n'
ATTEMPTS_SHA256 = hashlib.sha256(ATTEMPTS_BYTES).hexdigest()


def _evidence_cell(rank: int = 1, **fields: object) -> dict:
    cell = _cell(rank, "rc=0 runner=x")
    cell.update(
        job_name=f"job{rank}",
        trial_name=f"trial{rank}",
        attempts_json_sha256=ATTEMPTS_SHA256,
    )
    cell.update(fields)
    return cell


def _evidence_file(experiment: Path, cells: list[dict], card: str = "HAR-328") -> None:
    (experiment / "ladder-evidence.json").write_text(
        json.dumps({"schema": SCHEMA, "card": card, "tasks": cells}), encoding="utf-8"
    )


def _attempts_file(path: Path, content: bytes = ATTEMPTS_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _result_file(trial: Path, reward: object = 0.0) -> Path:
    """A trial ``result.json``; a None reward writes a null ``verifier_result``."""
    trial.mkdir(parents=True, exist_ok=True)
    verifier = None if reward is None else {"rewards": {"reward": reward}}
    path = trial / "result.json"
    path.write_text(json.dumps({"verifier_result": verifier}), encoding="utf-8")
    return path


def _subset_entry(
    job_name: str = "har-328-subset1",
    trial_name: str | None = "har-328-subset1__abc",
    reward: object = 0.0,
) -> dict:
    return {
        "subset": 1,
        "job_name": job_name,
        "trial_name": trial_name,
        "reward": reward,
        "verdict": "graded-clean",
    }


def _subset_evidence_file(experiment: Path, entries: list[dict], task_id: str = "t1") -> None:
    (experiment / "ladder-graded-subset.json").write_text(
        json.dumps(
            {
                "schema": MODULE.SUBSET_CELLS_SCHEMA,
                "tasks": [{"task_id": task_id, "subsets": entries}],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("override", [None, "HAR-999"])
def test_ladder_progress_evidence_matches_published_convention(
    evidence_sandbox: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    override: str | None,
) -> None:
    experiment, root = evidence_sandbox
    cell = _evidence_cell()
    if override is not None:
        cell["card"] = override
    card = override or "HAR-328"
    _evidence_file(experiment, [cell])
    path = _attempts_file(root / "2026-11-08" / f"{card}-job1" / "trial1/agent/cheat/attempts.json")
    _result_file(path.parents[2])
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr()
    assert f"{'matched':<15} t1 {card} job1/trial1" in output.out
    assert str(path) in output.out
    assert (
        "totals: 1 cells, matched=1, mismatched=0, reward-mismatch=0, absent=0, unhashed=0"
        in output.out
    )
    assert output.err == ""


def test_ladder_progress_evidence_mismatch_reports_first_existing_path_and_hashes(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    published = root / "published"
    raw = root / "raw"
    _evidence_file(experiment, [_evidence_cell(published_job=str(published), raw_job=str(raw))])
    content = b"different evidence"
    path = _attempts_file(published / "trial1/agent/cheat/attempts.json", content)
    _attempts_file(raw / "trial1/agent/cheat/attempts.json", b"another mismatch")
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 1
    output = capsys.readouterr()
    assert f"{'mismatched':<15} t1" in output.out
    assert str(path) in output.out
    assert f"expected {ATTEMPTS_SHA256[:12]}" in output.out
    assert f"got {hashlib.sha256(content).hexdigest()[:12]}" in output.out
    assert "mismatched=1" in output.out
    assert "1 mismatched, 0 reward-mismatch, 0 absent, 0 subset reward-mismatch" in output.err


def test_ladder_progress_evidence_absent_only_fails_in_strict_mode(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell()])
    args = ["--verify-evidence", "--results-root", str(root)]
    assert MODULE.main(args) == 0
    output = capsys.readouterr()
    assert f"{'absent':<15} t1" in output.out
    assert "no attempts.json among 0 candidate paths" in output.out
    assert "absent=1" in output.out
    assert output.err == ""
    assert MODULE.main([*args, "--strict"]) == 1
    strict = capsys.readouterr()
    assert strict.out == output.out
    assert "0 mismatched, 0 reward-mismatch, 1 absent" in strict.err


def test_ladder_progress_evidence_matches_explicit_published_and_raw_jobs(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    published = root / "explicit-published"
    raw = root / "explicit-raw"
    _evidence_file(
        experiment,
        [_evidence_cell(1, published_job=str(published)), _evidence_cell(2, raw_job=str(raw))],
    )
    paths = [
        _attempts_file(published / "trial1/agent/cheat/attempts.json"),
        _attempts_file(raw / "trial2/agent/cheat/attempts.json"),
    ]
    for path in paths:
        _result_file(path.parents[2])
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert f"{'matched':<15} t1 HAR-328 job1/trial1" in output
    assert f"{'matched':<15} t2 HAR-328 job2/trial2" in output
    assert all(str(path) in output for path in paths)
    assert "matched=2, mismatched=0, reward-mismatch=0, absent=0, unhashed=0" in output


def test_ladder_progress_evidence_matches_raw_convention(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell()])
    path = _attempts_file(
        root / "2026-11-08/har-328-mimo-v6-ladder/t1/job1/trial1/agent/cheat/attempts.json"
    )
    _result_file(path.parents[2])
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert f"{'matched':<15} t1" in output
    assert str(path) in output
    assert "matched=1" in output


@pytest.mark.parametrize("digest", [None, "", False, 7])
def test_ladder_progress_evidence_unhashed_never_fails_or_counts_as_absent(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str], digest: object
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell(attempts_json_sha256=digest)])
    args = ["--verify-evidence", "--results-root", str(root)]
    for extra in ([], ["--strict"]):
        assert MODULE.main([*args, *extra]) == 0
        output = capsys.readouterr()
        assert f"{'unhashed':<15} t1" in output.out
        assert "cell records no attempts_json_sha256" in output.out
        assert "matched=0, mismatched=0, reward-mismatch=0, absent=0, unhashed=1" in output.out
        assert output.err == ""


def test_ladder_progress_evidence_accepts_a_later_matching_candidate(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    published = root / "explicit-published"
    raw = root / "explicit-raw"
    _evidence_file(experiment, [_evidence_cell(published_job=str(published), raw_job=str(raw))])
    _attempts_file(published / "trial1/agent/cheat/attempts.json", b"stale")
    match = _attempts_file(raw / "trial1/agent/cheat/attempts.json")
    _result_file(match.parents[2])
    assert MODULE.main(["--verify-evidence", "--results-root", str(root), "--strict"]) == 0
    output = capsys.readouterr().out
    assert str(match) in output
    assert "matched=1, mismatched=0, reward-mismatch=0" in output


def test_ladder_progress_evidence_a_matching_reward_is_matched(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell()])
    attempts = _attempts_file(root / "2026-11-08/HAR-328-job1/trial1/agent/cheat/attempts.json")
    _result_file(attempts.parents[2], reward=0.0)
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr()
    assert f"{'matched':<15} t1 HAR-328 job1/trial1" in output.out
    assert "reward-mismatch=0" in output.out
    assert output.err == ""


def test_ladder_progress_evidence_a_reward_mismatch_fails(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell()])
    attempts = _attempts_file(root / "2026-11-08/HAR-328-job1/trial1/agent/cheat/attempts.json")
    result = _result_file(attempts.parents[2], reward=1.0)
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 1
    output = capsys.readouterr()
    assert f"{'reward-mismatch':<15} t1 HAR-328 job1/trial1" in output.out
    assert f"{result} expected reward 0.0 got 1.0" in output.out
    assert "matched=0, mismatched=0, reward-mismatch=1" in output.out
    assert "0 mismatched, 1 reward-mismatch, 0 absent" in output.err


def test_ladder_progress_evidence_a_none_reward_is_matched(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [_evidence_cell(reward=None, verdict="unscored")])
    attempts = _attempts_file(root / "2026-11-08/HAR-328-job1/trial1/agent/cheat/attempts.json")
    _result_file(attempts.parents[2], reward=None)
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr()
    assert f"{'matched':<15} t1 HAR-328 job1/trial1" in output.out
    assert "reward-mismatch=0" in output.out
    assert output.err == ""


def test_ladder_progress_evidence_matches_subset_rewards_and_skips_no_trial(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [])
    _subset_evidence_file(
        experiment,
        [
            _subset_entry(),
            _subset_entry(
                job_name="har-293-001671-subset2", trial_name=None, reward=None
            ),
        ],
    )
    trial = root / "2026-11-08/HAR-328-har-328-subset1/har-328-subset1__abc"
    _result_file(trial, reward=0.0)
    assert MODULE.main(["--verify-evidence", "--results-root", str(root)]) == 0
    output = capsys.readouterr()
    assert (
        f"{'matched':<15} subset HAR-328 har-328-subset1/har-328-subset1__abc" in output.out
    )
    assert str(trial / "result.json") in output.out
    assert (
        f"{'no-trial':<15} subset HAR-293 har-293-001671-subset2/-" in output.out
    )
    assert "entry records no trial_name" in output.out
    assert (
        "subset totals: 2 entries, matched=1, reward-mismatch=0, absent=0, no-trial=1"
        in output.out
    )
    assert output.err == ""


def test_ladder_progress_evidence_an_absent_subset_job_is_reported(
    evidence_sandbox: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, root = evidence_sandbox
    _evidence_file(experiment, [])
    _subset_evidence_file(experiment, [_subset_entry()])
    args = ["--verify-evidence", "--results-root", str(root)]
    assert MODULE.main(args) == 0
    output = capsys.readouterr()
    assert f"{'absent':<15} subset HAR-328 har-328-subset1/har-328-subset1__abc" in output.out
    assert "no result.json among 0 candidate paths" in output.out
    assert "subset totals: 1 entries, matched=0, reward-mismatch=0, absent=1, no-trial=0" in output.out
    assert output.err == ""
    assert MODULE.main([*args, "--strict"]) == 1
    strict = capsys.readouterr()
    assert strict.out == output.out
    assert "1 subset absent" in strict.err


def test_ladder_progress_evidence_candidates_are_ordered_and_deduplicated(tmp_path: Path) -> None:
    early = tmp_path / "2026-11-07"
    late = tmp_path / "2026-11-08"
    late.mkdir()
    early.mkdir()
    published = early / "HAR-328-job1"
    raw = tmp_path / "explicit-raw"
    cell = _evidence_cell(published_job=str(published), raw_job=str(raw))
    suffix = Path("trial1/agent/cheat/attempts.json")
    assert MODULE.attempts_candidates(cell, "HAR-328", tmp_path) == [
        published / suffix,
        raw / suffix,
        late / "HAR-328-job1" / suffix,
        early / "har-328-mimo-v6-ladder/t1/job1" / suffix,
        late / "har-328-mimo-v6-ladder/t1/job1" / suffix,
    ]


def test_ladder_progress_evidence_sorts_by_rank_then_task_and_keeps_every_cell() -> None:
    cells = [
        _evidence_cell(2, attempts_json_sha256=None),
        _evidence_cell(1, task_id="z", attempts_json_sha256=None),
        _evidence_cell(1, task_id="a", attempts_json_sha256=None),
    ]
    checks = MODULE.verify_evidence(cells, Path("/unused"))
    assert [result["cell"]["task_id"] for result in checks] == ["a", "z", "t2"]
    assert MODULE.render_evidence(checks).endswith(
        "totals: 3 cells, matched=0, mismatched=0, reward-mismatch=0, absent=0, unhashed=3\n"
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--verify-evidence", "--check"],
        ["--verify-evidence", "--summary"],
        ["--strict"],
        ["--strict", "--check"],
        ["--strict", "--summary"],
    ],
)
def test_ladder_progress_evidence_rejects_invalid_cli_modes(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        MODULE.main(args)
    assert refused.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--check", "--summary"])
def test_ladder_progress_offline_modes_never_touch_results_root(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline mode touched host evidence")

    monkeypatch.setattr(MODULE, "verify_evidence", forbidden)
    monkeypatch.setattr(Path, "expanduser", forbidden)
    assert MODULE.main([mode, "--results-root", str(sandbox / "missing-results")]) == 0
    assert capsys.readouterr().err == ""


def test_ladder_progress_evidence_expands_the_default_results_root_at_use_time(
    evidence_sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    experiment, root = evidence_sandbox
    monkeypatch.setenv("HOME", str(root))
    _evidence_file(experiment, [_evidence_cell()])
    path = _attempts_file(
        root
        / "Developer/eval-lab-results/2026-11-08/HAR-328-job1"
        / "trial1/agent/cheat/attempts.json"
    )
    _result_file(path.parents[2])
    assert MODULE.main(["--verify-evidence"]) == 0
    assert str(path) in capsys.readouterr().out


def test_ladder_progress_check_refuses_a_cell_at_another_digest(sandbox: Path) -> None:
    path = sandbox / "ladder-p0-har244.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    task_id = data["tasks"][0]["task_id"]
    data["tasks"][0]["final_digest"] = "sha256:stale"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProgressError, match=task_id):
        MODULE.main(["--check"])


def test_ladder_progress_check_passes_on_committed_table(sandbox: Path) -> None:
    assert MODULE.main(["--check"]) == 0


def test_ladder_progress_check_fails_when_a_committed_verdict_is_edited(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    table = sandbox / "ladder-progress.csv"
    text = table.read_text(encoding="utf-8")
    assert ",clean," in text
    table.write_text(text.replace(",clean,", ",cracked,", 1), encoding="utf-8")
    assert MODULE.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert MODULE.main([]) == 0  # regenerating repairs the drift
    assert MODULE.main(["--check"]) == 0


def test_ladder_progress_summary_prints_the_table_without_writing_the_csv(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    table = sandbox / "ladder-progress.csv"
    before = table.read_bytes()
    assert MODULE.main(["--summary"]) == 0
    assert capsys.readouterr().out == MODULE.render_summary(_committed())
    assert table.read_bytes() == before
