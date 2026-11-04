"""The v6 ladder progress table stays mechanical and names each verdict's basis."""

from __future__ import annotations

import collections
import csv
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
    return {
        "task_id": f"t{rank}",
        "ranking": {"rank": rank, "phase": "P"},
        "ladder_version": "1.4.0",
        "verdict": verdict,
        "verifier_decision": decision,
        "verifier_tamper_hit": f"tamper \\b_pytest\\b in pkg{rank}/__init__.py" if refused else None,
        "attacks_executed": 14 if refused else 9,
        "wall_seconds": 10 * rank,
        "finished_utc": finished,
        "final_digest": f"sha256:cell{rank}" if digest is None else digest,
        "digest_matches_manifest": matches,
        "_source_file": f"f{rank}.json",
    }


def _manifest(n: int = 3) -> dict[str, str]:
    return {f"t{i}": f"sha256:cell{i}" for i in range(1, n + 1)}


def _cell_file(tmp_path: Path, cell: dict) -> None:
    body = {k: v for k, v in cell.items() if k != "_source_file"}
    (tmp_path / "ladder-a.json").write_text(
        json.dumps({"schema": SCHEMA, "tasks": [body]}), encoding="utf-8"
    )


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
