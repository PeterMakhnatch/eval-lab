"""Pure-synthetic tests for pai_source; no real adapter, bundle or benchmark row is touched."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/oq10-task-quality-cross/pai_source.py"
)
_SPEC = importlib.util.spec_from_file_location("oq10_pai_source", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
pai = importlib.util.module_from_spec(_SPEC)
sys.modules["oq10_pai_source"] = pai
_SPEC.loader.exec_module(pai)

SEED = "oq10-cross-v1-20261028"
MB = "swe_bench_multilingual"
GYM = "swe_gym"
FIELDS = (
    "repo",
    "base_commit",
    "problem_statement",
    "hints_text",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "patch",
)
LABEL_SENTINELS = ("SENTINEL_human_label", "SENTINEL_families", "SENTINEL_gold_positive")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_key(row: dict[str, Any]) -> tuple[str, str]:
    return (_sha(row["task_id"] + SEED), row["task_id"])


# ----------------------------------------------------------------- population fixtures


def _rows(subcorpus: str, positive: bool, repo_sizes: list[int]) -> list[dict[str, Any]]:
    rows = []
    for repo_index, size in enumerate(repo_sizes):
        for item in range(size):
            rows.append(
                {
                    "task_id": f"{subcorpus}:inst-{int(positive)}-{repo_index:02d}-{item:02d}",
                    "positive": positive,
                    "stratum": f"org/repo{repo_index:02d}",
                    "subcorpus": subcorpus,
                }
            )
    return rows


# 12 positive repos (fewer than 30) with uneven sizes; 40 negative repos (more than 30).
POSITIVE_SIZES = [20, 15, 10, 5, 4, 3, 2, 2, 2, 1, 1, 1]
NEGATIVE_SIZES = [3] * 10 + [1] * 30


def _population() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for subcorpus in (MB, GYM):
        rows += _rows(subcorpus, True, POSITIVE_SIZES)
        rows += _rows(subcorpus, False, NEGATIVE_SIZES)
    return rows


# ----------------------------------------------------------------- select_cases


def test_each_subcorpus_selected_independently_with_complete_class_counts() -> None:
    selected = pai.select_cases(_population())
    assert len(selected) == 120
    for subcorpus in (MB, GYM):
        mine = [r for r in selected if r["subcorpus"] == subcorpus]
        assert sum(r["positive"] for r in mine) == 30
        assert sum(not r["positive"] for r in mine) == 30
    # Fixed sub-corpus order, never one pooled draw.
    assert [r["subcorpus"] for r in selected] == [MB] * 60 + [GYM] * 60


def test_subcorpus_filter_selects_only_that_subcorpus() -> None:
    selected = pai.select_cases(_population(), subcorpus=GYM)
    assert len(selected) == 60
    assert {r["subcorpus"] for r in selected} == {GYM}


def test_repo_coverage_is_min_of_count_and_strata_per_class() -> None:
    selected = pai.select_cases(_population())
    for subcorpus in (MB, GYM):
        positives = [r for r in selected if r["subcorpus"] == subcorpus and r["positive"]]
        negatives = [r for r in selected if r["subcorpus"] == subcorpus and not r["positive"]]
        # 12 positive repos < 30: every repo is covered.
        assert {r["stratum"] for r in positives} == {f"org/repo{k:02d}" for k in range(12)}
        # 40 negative repos > 30: 30 distinct repos, one item each.
        assert len({r["stratum"] for r in negatives}) == 30


def test_round_robin_order_matches_hash_recipe() -> None:
    # Strata A (3 items), B (1), C (2); positives=4 -> first round one per stratum
    # in stratum-hash order, then the first stratum in that order with a second item.
    rows = [
        {"task_id": f"{MB}:{name}", "positive": True, "stratum": stratum, "subcorpus": MB}
        for stratum, names in {"A": ["a1", "a2", "a3"], "B": ["b1"], "C": ["c1", "c2"]}.items()
        for name in names
    ] + [{"task_id": f"{MB}:neg", "positive": False, "stratum": "A", "subcorpus": MB}]
    by_stratum = {s: [r for r in rows if r["positive"] and r["stratum"] == s] for s in "ABC"}
    for items in by_stratum.values():
        items.sort(key=_hash_key)
    strata = sorted(by_stratum, key=lambda s: _sha(s + SEED))
    expected = [by_stratum[s][0] for s in strata]
    second = next(s for s in strata if len(by_stratum[s]) > 1)
    expected.append(by_stratum[second][1])
    expected.sort(key=_hash_key)

    chosen = [r for r in pai.select_cases(rows, positives=4, negatives=1, subcorpus=MB) if r["positive"]]
    assert chosen == expected


def test_selection_is_order_independent_hash_ordered_and_seed_sensitive() -> None:
    population = _population()
    baseline = pai.select_cases(population)
    shuffled = list(population)
    random.Random(7).shuffle(shuffled)
    assert pai.select_cases(shuffled) == baseline
    for subcorpus in (MB, GYM):
        mine = [r for r in baseline if r["subcorpus"] == subcorpus]
        assert mine == sorted(mine, key=_hash_key)
    other = pai.select_cases(population, seed="another-seed")
    assert {r["task_id"] for r in other} != {r["task_id"] for r in baseline}


def test_selection_returns_copies_with_exact_keys() -> None:
    population = _population()
    selected = pai.select_cases(population)
    assert all(set(r) == {"task_id", "positive", "stratum", "subcorpus"} for r in selected)
    selected[0]["positive"] = not selected[0]["positive"]
    assert population == _population()


def test_shortage_is_rejected_not_narrowed() -> None:
    population = [r for r in _population() if not (r["subcorpus"] == GYM and r["positive"])]
    population += _rows(GYM, True, [29])
    with pytest.raises(ValueError, match="positives"):
        pai.select_cases(population)
    with pytest.raises(ValueError, match="negatives"):
        pai.select_cases(_population(), negatives=61, subcorpus=MB)


def test_absent_subcorpus_is_a_shortage() -> None:
    only_mb = [r for r in _population() if r["subcorpus"] == MB]
    with pytest.raises(ValueError, match="positives"):
        pai.select_cases(only_mb)
    with pytest.raises(ValueError, match="positives"):
        pai.select_cases(only_mb, subcorpus=GYM)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].update(label="SENTINEL_human_label"),
        lambda rows: rows[0].update(positive="false"),
        lambda rows: rows[0].update(positive=None),
        lambda rows: rows[0].update(subcorpus="other_corpus"),
        lambda rows: rows[0].update(stratum=""),
        lambda rows: rows[0].pop("stratum"),
    ],
)
def test_malformed_or_duplicate_population_rows_are_rejected(mutate: Any) -> None:
    rows = _population()
    mutate(rows)
    with pytest.raises(ValueError):
        pai.select_cases(rows)


@pytest.mark.parametrize("kwargs", [{"seed": ""}, {"positives": 0}, {"negatives": True}, {"subcorpus": "x"}])
def test_invalid_selection_arguments_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        pai.select_cases(_population(), **kwargs)


# ----------------------------------------------------------------- metadata_population


class FakeMetadataAdapter:
    """Fake adapter module: labels per path name, grader rows with a forbidden text column."""

    def __init__(
        self,
        labels: dict[str, dict[str, tuple[str, ...]]],
        graders: dict[str, list[dict[str, Any]]],
        expected_rows: dict[str, int] | None = None,
    ) -> None:
        self.labels = labels
        self.graders = graders
        self.requested_columns: list[tuple[str, ...]] = []
        self.SUB_CORPORA = {
            name: {
                "labels": f"labels/{name}.csv",
                "tasks": f"tasks/{name}.parquet",
                "expected_rows": (expected_rows or {}).get(name, len(labels[name])),
            }
            for name in (MB, GYM)
        }

    def load_labels(self, path: Path) -> dict[str, tuple[str, ...]]:
        return dict(self.labels[path.stem])

    def task_rows(self, path: Path, columns: tuple[str, ...]) -> Iterator[dict[str, Any]]:
        self.requested_columns.append(columns)
        assert columns == ("instance_id", "repo"), "metadata selection must not read other columns"
        for row in self.graders[path.stem]:
            yield {column: row[column] for column in columns}


def _metadata_fixture() -> FakeMetadataAdapter:
    labels = {
        MB: {"mb-1": ("SC",), "mb-2": (), "mb-3": ("DP", "FP")},
        GYM: {"gym-1": (), "gym-2": ("UL",)},
    }
    graders = {
        MB: [
            {"instance_id": "mb-1", "repo": "org/a", "problem_statement": "SHOULD_NOT_BE_READ"},
            {"instance_id": "mb-2", "repo": "org/a", "problem_statement": "SHOULD_NOT_BE_READ"},
            {"instance_id": "mb-3", "repo": "org/b", "problem_statement": "SHOULD_NOT_BE_READ"},
        ],
        GYM: [
            {"instance_id": "gym-1", "repo": "org/c", "problem_statement": "SHOULD_NOT_BE_READ"},
            {"instance_id": "gym-unlabelled", "repo": "org/c", "problem_statement": "x"},
            {"instance_id": "gym-2", "repo": "org/d", "problem_statement": "SHOULD_NOT_BE_READ"},
            {"instance_id": "gym-unlabelled-2", "repo": "", "problem_statement": "x"},
        ],
    }
    return FakeMetadataAdapter(labels, graders)


def test_metadata_population_rows_exclude_unlabelled_and_use_metadata_columns_only() -> None:
    adapter = _metadata_fixture()
    population = pai.metadata_population(adapter, Path("/synthetic/bundle"))
    assert adapter.requested_columns == [("instance_id", "repo")] * 2
    assert population == [
        {"task_id": f"{MB}:mb-1", "positive": True, "stratum": "org/a", "subcorpus": MB},
        {"task_id": f"{MB}:mb-2", "positive": False, "stratum": "org/a", "subcorpus": MB},
        {"task_id": f"{MB}:mb-3", "positive": True, "stratum": "org/b", "subcorpus": MB},
        {"task_id": f"{GYM}:gym-1", "positive": False, "stratum": "org/c", "subcorpus": GYM},
        {"task_id": f"{GYM}:gym-2", "positive": True, "stratum": "org/d", "subcorpus": GYM},
    ]
    assert "SHOULD_NOT_BE_READ" not in json.dumps(population)


def test_metadata_rows_feed_selection_without_modification() -> None:
    population = pai.metadata_population(_metadata_fixture(), Path("/synthetic/bundle"))
    selected = pai.select_cases(population, positives=1, negatives=1)
    assert len(selected) == 4
    assert {r["task_id"] for r in selected} <= {r["task_id"] for r in population}


def test_label_id_missing_from_grader_fails_join() -> None:
    adapter = _metadata_fixture()
    adapter.graders[GYM] = [r for r in adapter.graders[GYM] if r["instance_id"] != "gym-2"]
    with pytest.raises(ValueError, match="join incomplete"):
        pai.metadata_population(adapter, Path("/b"))


def test_duplicate_grader_identity_fails() -> None:
    adapter = _metadata_fixture()
    adapter.graders[MB].append(dict(adapter.graders[MB][0]))
    with pytest.raises(ValueError, match="duplicate grader identity"):
        pai.metadata_population(adapter, Path("/b"))


def test_unlabelled_grader_duplicate_still_fails() -> None:
    adapter = _metadata_fixture()
    adapter.graders[GYM].append(dict(adapter.graders[GYM][1]))
    with pytest.raises(ValueError, match="duplicate grader identity"):
        pai.metadata_population(adapter, Path("/b"))


def test_pinned_expected_rows_mismatch_fails() -> None:
    adapter = _metadata_fixture()
    adapter.SUB_CORPORA[GYM]["expected_rows"] = 3
    with pytest.raises(ValueError, match="expected_rows"):
        pai.metadata_population(adapter, Path("/b"))


@pytest.mark.parametrize("bad_repo", ["", None, 7])
def test_labelled_task_needs_a_string_repo(bad_repo: Any) -> None:
    adapter = _metadata_fixture()
    adapter.graders[MB][0]["repo"] = bad_repo
    with pytest.raises(ValueError, match="repo"):
        pai.metadata_population(adapter, Path("/b"))


def test_unexpected_subcorpus_set_fails() -> None:
    adapter = _metadata_fixture()
    adapter.SUB_CORPORA["extra"] = adapter.SUB_CORPORA[MB]
    with pytest.raises(ValueError, match="sub-corpora"):
        pai.metadata_population(adapter, Path("/b"))


# ----------------------------------------------------------------- render_auditor_input


def _auditor_input(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "repo": "org/repo",
        "base_commit": "0123abcd",
        "problem_statement": "Fix  the bug.\n\ttabs and naïve — 日本 stay exact\r\n  ",
        "hints_text": "Maintainer hint\n",
        "test_patch": "diff --git a/t.py b/t.py\n+assert fixed\n",
        "FAIL_TO_PASS": ["tests/test_a.py::test_é", "tests/test_b.py::test_b[1-2]"],
        "PASS_TO_PASS": [],
        "patch": "diff --git a/src.py b/src.py\n-old\n+new\n",
    }
    value.update(overrides)
    return value


def test_render_emits_exact_fixed_file_map() -> None:
    ai = _auditor_input()
    files = pai.render_auditor_input(ai)
    assert list(files) == [
        "instruction.md",
        "tests/00-hints.txt",
        "tests/01-test.patch",
        "tests/02-target-PR.patch",
        "tests/03-test-names.json",
        "tests/04-source-provenance.json",
    ]
    assert files["instruction.md"] == ai["problem_statement"]
    assert files["tests/00-hints.txt"] == "Maintainer hint\n"
    assert files["tests/01-test.patch"] == ai["test_patch"]
    assert files["tests/02-target-PR.patch"] == ai["patch"]
    assert files["tests/03-test-names.json"] == (
        '{\n  "FAIL_TO_PASS": [\n    "tests/test_a.py::test_é",\n'
        '    "tests/test_b.py::test_b[1-2]"\n  ],\n  "PASS_TO_PASS": []\n}\n'
    )
    assert files["tests/04-source-provenance.json"] == (
        '{\n  "base_commit": "0123abcd",\n  "repo": "org/repo"\n}\n'
    )


def test_original_patch_and_test_names_are_preserved_verbatim() -> None:
    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a \n+b\t\n\\ No newline at end of file"
    names = ["z::last", "a::first", "a::first", "m::mid[ spaced ]"]
    files = pai.render_auditor_input(_auditor_input(patch=patch, FAIL_TO_PASS=names, PASS_TO_PASS=names[::-1]))
    assert files["tests/02-target-PR.patch"] == patch
    decoded = json.loads(files["tests/03-test-names.json"])
    assert decoded == {"FAIL_TO_PASS": names, "PASS_TO_PASS": names[::-1]}


def test_native_null_hints_render_as_explicit_json_null_and_empty_hints_stay_empty() -> None:
    assert pai.render_auditor_input(_auditor_input(hints_text=None))["tests/00-hints.txt"] == "null\n"
    assert pai.render_auditor_input(_auditor_input(hints_text=""))["tests/00-hints.txt"] == ""


def test_render_does_not_mutate_input() -> None:
    ai = _auditor_input()
    before = json.dumps(ai, sort_keys=True)
    pai.render_auditor_input(ai)
    assert json.dumps(ai, sort_keys=True) == before


def test_render_emits_no_label_envelope_or_identity_text() -> None:
    ai = _auditor_input()
    files = pai.render_auditor_input(ai)
    blob = "\n".join([*files, *files.values()])
    for forbidden in ("instance_id", "sub_corpus", "gold", "human_label", "families", "identity"):
        assert forbidden not in blob


@pytest.mark.parametrize("extra", ["human_label", "families", "instance_id", "gold", "sub_corpus", "anything"])
def test_unknown_fields_are_rejected(extra: str) -> None:
    ai = _auditor_input()
    ai[extra] = "SENTINEL_human_label"
    with pytest.raises(pai.SourceShapeError) as caught:
        pai.render_auditor_input(ai)
    assert "SENTINEL_human_label" not in str(caught.value)


def test_whole_spec_envelope_is_rejected() -> None:
    spec = {
        "auditor_input": _auditor_input(),
        "gold": {"positive": True, "families": ["SC"]},
        "identity": {"instance_id": "x", "sub_corpus": MB, "repo": "org/repo"},
    }
    with pytest.raises(pai.SourceShapeError):
        pai.render_auditor_input(spec)


@pytest.mark.parametrize("field", FIELDS)
def test_missing_field_is_rejected(field: str) -> None:
    ai = _auditor_input()
    del ai[field]
    with pytest.raises(pai.SourceShapeError, match=field):
        pai.render_auditor_input(ai)


@pytest.mark.parametrize(
    "override",
    [
        {"repo": None},
        {"base_commit": 1},
        {"problem_statement": None},
        {"test_patch": None},
        {"patch": None},
        {"patch": ["diff"]},
        {"hints_text": 3},
        {"hints_text": ["h"]},
        {"FAIL_TO_PASS": ("a",)},
        {"FAIL_TO_PASS": "a"},
        {"FAIL_TO_PASS": None},
        {"PASS_TO_PASS": ["a", 2]},
        {"PASS_TO_PASS": [["a"]]},
    ],
)
def test_non_native_shapes_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(pai.SourceShapeError):
        pai.render_auditor_input(_auditor_input(**override))


def test_non_mapping_input_is_rejected() -> None:
    with pytest.raises(pai.SourceShapeError):
        pai.render_auditor_input(["repo"])  # type: ignore[arg-type]


# ----------------------------------------------------------------- iter_selected


class FakeSpecAdapter:
    AUDITOR_INPUT_FIELDS = FIELDS

    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self._specs = specs
        self.bundles: list[Path] = []
        self.exhausted = False

    def specs(self, bundle: Path) -> Iterator[dict[str, Any]]:
        self.bundles.append(bundle)
        yield from self._specs
        self.exhausted = True  # the adapter's trailing join check only runs when drained


def _spec(subcorpus: str, instance_id: str, positive: bool, **overrides: Any) -> dict[str, Any]:
    repo = f"org/repo-{len(instance_id)}-{instance_id[-1]}"
    auditor_input = _auditor_input(
        repo=repo,
        problem_statement=f"Issue text {len(instance_id)}",
        patch=f"diff --git a/file{len(instance_id)}.py b/file{len(instance_id)}.py\n+reference\n",
    )
    auditor_input.update(overrides.pop("auditor_input", {}))
    spec = {
        "auditor_input": auditor_input,
        "gold": {"positive": positive, "families": ["SENTINEL_families"] if positive else []},
        "identity": {"instance_id": instance_id, "sub_corpus": subcorpus, "repo": repo},
    }
    spec.update(overrides)
    return spec


def _selected(*pairs: tuple[str, str, bool]) -> list[dict[str, Any]]:
    return [
        {"task_id": f"{sub}:{iid}", "positive": pos, "stratum": f"org/{iid}-repo", "subcorpus": sub}
        for sub, iid, pos in pairs
    ]


def _three_specs() -> list[dict[str, Any]]:
    return [
        _spec(MB, "mb-a", True),
        _spec(MB, "mb-skip", False),
        _spec(GYM, "gym-b", False),
        _spec(GYM, "gym-skip", True),
    ]


def test_selected_records_are_prepared_in_selected_order_and_drain_the_adapter() -> None:
    adapter = FakeSpecAdapter(_three_specs())
    selected = _selected((GYM, "gym-b", False), (MB, "mb-a", True))
    records = list(pai.iter_selected(adapter, Path("/synthetic/bundle"), selected))
    assert adapter.bundles == [Path("/synthetic/bundle")]
    assert adapter.exhausted
    assert [r["task_id"] for r in records] == [f"{GYM}:gym-b", f"{MB}:mb-a"]
    for record in records:
        assert set(record) == {
            "task_id",
            "subcorpus",
            "auditor_files",
            "identity",
            "source_record_sha256",
            "preparation_status",
            "preparation_error",
        }
        assert record["preparation_status"] == "prepared"
        assert record["preparation_error"] is None


def test_records_use_adapter_projection_and_keep_scorer_identity_out_of_model_files() -> None:
    adapter = FakeSpecAdapter(_three_specs())
    (record,) = pai.iter_selected(adapter, Path("/b"), _selected((MB, "mb-a", True)))
    spec = _three_specs()[0]
    assert record["auditor_files"] == pai.render_auditor_input(spec["auditor_input"])
    assert record["identity"] == {"instance_id": "mb-a", "sub_corpus": MB, "repo": "org/repo-4-a"}
    assert record["subcorpus"] == MB
    blob = "\n".join([*record["auditor_files"], *record["auditor_files"].values()])
    for forbidden in (*LABEL_SENTINELS, "mb-a", "instance_id", "sub_corpus", "positive", record["source_record_sha256"]):
        assert forbidden not in blob
    assert "gold" not in record
    assert "mb-skip" not in blob and "gym-b" not in blob


def test_source_record_hash_is_canonical_over_auditor_input_and_identity_only() -> None:
    spec = _three_specs()[0]
    expected_text = pai.canonical_json({"auditor_input": spec["auditor_input"], "identity": spec["identity"]})
    expected = _sha(expected_text)
    (record,) = pai.iter_selected(FakeSpecAdapter(_three_specs()), Path("/b"), _selected((MB, "mb-a", True)))
    assert record["source_record_sha256"] == expected

    changed_gold = _three_specs()
    changed_gold[0]["gold"] = {"positive": True, "families": ["DP", "FP"]}
    (again,) = pai.iter_selected(FakeSpecAdapter(changed_gold), Path("/b"), _selected((MB, "mb-a", True)))
    assert again["source_record_sha256"] == expected

    changed_source = _three_specs()
    changed_source[0]["auditor_input"]["patch"] += " "
    (other,) = pai.iter_selected(FakeSpecAdapter(changed_source), Path("/b"), _selected((MB, "mb-a", True)))
    assert other["source_record_sha256"] != expected


def test_missing_selected_id_raises_before_any_record() -> None:
    adapter = FakeSpecAdapter(_three_specs())
    selected = _selected((MB, "mb-a", True), (MB, "mb-ghost", False))
    with pytest.raises(ValueError, match="absent"):
        pai.iter_selected(adapter, Path("/b"), selected)


def test_same_native_id_in_other_subcorpus_is_not_a_match() -> None:
    adapter = FakeSpecAdapter(_three_specs())
    with pytest.raises(ValueError, match="absent"):
        pai.iter_selected(adapter, Path("/b"), _selected((GYM, "mb-a", True)))


def test_duplicate_selected_ids_are_rejected() -> None:
    selected = _selected((MB, "mb-a", True), (MB, "mb-a", True))
    with pytest.raises(ValueError, match="duplicate"):
        pai.iter_selected(FakeSpecAdapter(_three_specs()), Path("/b"), selected)


def test_duplicate_adapter_identity_is_rejected() -> None:
    specs = _three_specs() + [_spec(MB, "mb-a", True)]
    with pytest.raises(ValueError, match="duplicate task identity"):
        pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), _selected((MB, "mb-a", True)))


def test_label_drift_between_selection_and_gold_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        pai.iter_selected(FakeSpecAdapter(_three_specs()), Path("/b"), _selected((MB, "mb-a", False)))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda spec: spec.update(extra="x"),
        lambda spec: spec.pop("gold"),
        lambda spec: spec["identity"].update(extra="x"),
        lambda spec: spec["identity"].update(sub_corpus="unknown"),
        lambda spec: spec["identity"].update(instance_id=""),
    ],
)
def test_adapter_envelope_drift_is_rejected_even_for_unselected_rows(tamper: Any) -> None:
    specs = _three_specs()
    tamper(specs[1])  # mb-skip is not selected
    with pytest.raises(ValueError, match="adapter"):
        pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), _selected((MB, "mb-a", True)))


def test_adapter_field_allowlist_drift_is_rejected() -> None:
    adapter = FakeSpecAdapter(_three_specs())
    adapter.AUDITOR_INPUT_FIELDS = (*FIELDS, "human_label")
    with pytest.raises(ValueError, match="allowlist"):
        pai.iter_selected(adapter, Path("/b"), _selected((MB, "mb-a", True)))


def test_malformed_selected_source_is_source_unavailable_not_repaired() -> None:
    specs = _three_specs()
    specs[0]["auditor_input"]["problem_statement"] = None
    specs[2]["auditor_input"]["human_label"] = "SENTINEL_human_label"
    selected = _selected((MB, "mb-a", True), (GYM, "gym-b", False))
    first, second = pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), selected)
    for record in (first, second):
        assert record["preparation_status"] == "source_unavailable"
        assert record["auditor_files"] == {}
        assert record["identity"]["instance_id"] in {"mb-a", "gym-b"}
        assert "SENTINEL_human_label" not in record["preparation_error"]
        assert record["source_record_sha256"] is not None
    assert "problem_statement" in first["preparation_error"]


def test_identity_repo_must_match_auditor_repo() -> None:
    specs = _three_specs()
    specs[0]["identity"]["repo"] = "org/other"
    (record,) = pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), _selected((MB, "mb-a", True)))
    assert record["preparation_status"] == "source_unavailable"
    assert record["auditor_files"] == {}


def test_unhashable_source_has_no_digest_instead_of_a_fake_one() -> None:
    specs = _three_specs()
    specs[0]["auditor_input"]["patch"] = b"bytes are not native"
    (record,) = pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), _selected((MB, "mb-a", True)))
    assert record["preparation_status"] == "source_unavailable"
    assert record["source_record_sha256"] is None


def test_null_hints_survive_hydration_explicitly() -> None:
    specs = _three_specs()
    specs[0]["auditor_input"]["hints_text"] = None
    (record,) = pai.iter_selected(FakeSpecAdapter(specs), Path("/b"), _selected((MB, "mb-a", True)))
    assert record["preparation_status"] == "prepared"
    assert record["auditor_files"]["tests/00-hints.txt"] == "null\n"
