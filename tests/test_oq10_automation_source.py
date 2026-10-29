"""Pure-synthetic tests for the OQ10 AutomationBench source module.

No real adapter, bundle, task or label data is touched: the adapter is a fake
namespace and every CSV/spec is built inline.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/oq10-task-quality-cross/automation_source.py"
)
_SPEC = importlib.util.spec_from_file_location("oq10_automation_source_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
src = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = src
_SPEC.loader.exec_module(src)

SEED = "oq10-cross-v1-20261028"
CORPUS = "fake__automation"
DOMAINS = ("sales", "marketing", "operations", "support", "finance", "hr", "simple")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def fake_adapter(specs: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        CORPUS=CORPUS,
        DOMAINS=DOMAINS,
        specs=specs if specs is not None else (lambda bundle: iter(())),
    )


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header), *(",".join(row) for row in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- population


def build_bundle(
    tmp_path: Path,
    tasks: list[tuple[str, str, str]],
    fixes: list[tuple[str, str]],
    fix_header: tuple[str, ...] = ("task_name", "domain"),
) -> Path:
    write_csv(
        tmp_path / "tasks.csv",
        ["task_name", "example_id", "domain"],
        [[name, example, domain] for name, example, domain in tasks],
    )
    rows = [list(row) for row in fixes]
    write_csv(tmp_path / "fixes.csv", list(fix_header), rows)
    return tmp_path


def test_population_joins_by_task_name_not_example_id(tmp_path: Path) -> None:
    bundle = build_bundle(
        tmp_path,
        tasks=[("t-b", "7", "hr"), ("t-a", "7", "sales"), ("t-c", "8", "simple")],
        fixes=[("t-a", "sales")],
    )
    population = src.metadata_population(fake_adapter(), bundle)
    assert population == [
        {"task_id": "t-a", "positive": True, "stratum": "sales", "subcorpus": CORPUS},
        {"task_id": "t-b", "positive": False, "stratum": "hr", "subcorpus": CORPUS},
        {"task_id": "t-c", "positive": False, "stratum": "simple", "subcorpus": CORPUS},
    ]


def test_population_omits_unlabelled_rows_instead_of_calling_them_negative(tmp_path: Path) -> None:
    bundle = build_bundle(
        tmp_path,
        tasks=[("t-a", "1", "sales"), ("t-nodomain", "2", ""), ("", "3", "hr")],
        fixes=[],
    )
    population = src.metadata_population(fake_adapter(), bundle)
    assert [row["task_id"] for row in population] == ["t-a"]


@pytest.mark.parametrize(
    ("tasks", "fixes"),
    [
        ([("t-a", "1", "sales"), ("t-a", "2", "sales")], []),
        ([("t-a", "1", "sales")], [("t-a", "sales"), ("t-a", "sales")]),
        ([("t-a", "1", "sales")], [("t-missing", "sales")]),
        ([("t-a", "1", "sales")], [("", "sales")]),
        ([("t-a", "1", "sales")], [("t-a", "hr")]),
        ([("t-a", "1", "sales")], [("t-nodomain", "")]),
        ([("t-a", "1", "not-a-domain")], []),
        ([("t-nodomain", "1", "")], [("t-nodomain", "")]),
    ],
)
def test_population_rejects_bad_joins(
    tmp_path: Path, tasks: list[tuple[str, str, str]], fixes: list[tuple[str, str]]
) -> None:
    bundle = build_bundle(tmp_path, tasks, fixes)
    with pytest.raises(src.AutomationSourceError):
        src.metadata_population(fake_adapter(), bundle)


def test_population_rejects_missing_columns(tmp_path: Path) -> None:
    write_csv(tmp_path / "tasks.csv", ["task_name"], [["t-a"]])
    write_csv(tmp_path / "fixes.csv", ["task_name"], [])
    with pytest.raises(src.AutomationSourceError):
        src.metadata_population(fake_adapter(), tmp_path)


def test_population_fixes_without_domain_column_still_joins(tmp_path: Path) -> None:
    bundle = build_bundle(
        tmp_path, tasks=[("t-a", "1", "finance")], fixes=[("t-a",)], fix_header=("task_name",)
    )
    assert src.metadata_population(fake_adapter(), bundle)[0]["positive"] is True


# -------------------------------------------------------------------- sampler


def make_population(
    positives: dict[str, int], negatives: dict[str, int], subcorpus: str = CORPUS
) -> list[dict[str, Any]]:
    rows = []
    for domain, count in positives.items():
        rows += [
            {"task_id": f"{domain}-pos-{i}", "positive": True, "stratum": domain, "subcorpus": subcorpus}
            for i in range(count)
        ]
    for domain, count in negatives.items():
        rows += [
            {"task_id": f"{domain}-neg-{i}", "positive": False, "stratum": domain, "subcorpus": subcorpus}
            for i in range(count)
        ]
    return rows


UNEVEN_POSITIVES = {
    "sales": 12, "marketing": 9, "operations": 7, "support": 6, "finance": 5, "hr": 1, "simple": 2,
}
PLENTY_NEGATIVES = {domain: 40 for domain in DOMAINS}


def test_selection_covers_every_positive_domain_and_matches_negatives_exactly() -> None:
    population = make_population(UNEVEN_POSITIVES, PLENTY_NEGATIVES)
    chosen = src.select_cases(population)
    positives = [row for row in chosen if row["positive"]]
    negatives = [row for row in chosen if not row["positive"]]
    assert (len(positives), len(negatives)) == (30, 30)
    per_domain_pos = {d: sum(r["stratum"] == d for r in positives) for d in DOMAINS}
    per_domain_neg = {d: sum(r["stratum"] == d for r in negatives) for d in DOMAINS}
    assert all(count >= 1 for count in per_domain_pos.values())
    assert per_domain_pos == per_domain_neg
    assert len({row["task_id"] for row in chosen}) == 60


def test_round_robin_follows_hash_ordered_strata_and_items() -> None:
    population = make_population({domain: 5 for domain in DOMAINS}, PLENTY_NEGATIVES)
    chosen = src.select_cases(population, positives=10, negatives=10)
    strata = sorted(DOMAINS, key=lambda s: (sha(s + SEED), s))
    # 10 positives over 7 strata: first three strata in hash order get two, rest one.
    expected_counts = {s: (2 if i < 3 else 1) for i, s in enumerate(strata)}
    for stratum in DOMAINS:
        pool = sorted(
            (r for r in population if r["positive"] and r["stratum"] == stratum),
            key=lambda r: (sha(r["task_id"] + SEED), r["task_id"]),
        )
        got = {r["task_id"] for r in chosen if r["positive"] and r["stratum"] == stratum}
        assert got == {r["task_id"] for r in pool[: expected_counts[stratum]]}
        neg_pool = sorted(
            (r for r in population if not r["positive"] and r["stratum"] == stratum),
            key=lambda r: (sha(r["task_id"] + SEED), r["task_id"]),
        )
        got_neg = {r["task_id"] for r in chosen if not r["positive"] and r["stratum"] == stratum}
        assert got_neg == {r["task_id"] for r in neg_pool[: expected_counts[stratum]]}


def test_exhausted_stratum_is_skipped_and_remaining_rounds_continue() -> None:
    population = make_population({"sales": 1, "hr": 5}, {"sales": 5, "hr": 5})
    chosen = src.select_cases(population, positives=4, negatives=4)
    assert sum(r["positive"] and r["stratum"] == "sales" for r in chosen) == 1
    assert sum(r["positive"] and r["stratum"] == "hr" for r in chosen) == 3
    assert sum((not r["positive"]) and r["stratum"] == "sales" for r in chosen) == 1
    assert sum((not r["positive"]) and r["stratum"] == "hr" for r in chosen) == 3


def test_output_is_ordered_by_task_hash_and_independent_of_row_order() -> None:
    population = make_population(UNEVEN_POSITIVES, PLENTY_NEGATIVES)
    baseline = src.select_cases(population)
    assert baseline == sorted(baseline, key=lambda r: (sha(r["task_id"] + SEED), r["task_id"]))
    for trial in range(5):
        shuffled = copy.deepcopy(population)
        random.Random(trial).shuffle(shuffled)
        assert src.select_cases(shuffled) == baseline


def test_selection_ignores_extra_population_keys_and_does_not_mutate_input() -> None:
    population = make_population({"sales": 3}, {"sales": 3})
    for row in population:
        row["labels"] = {"issue_group": "SENTINEL"}
    snapshot = copy.deepcopy(population)
    chosen = src.select_cases(population, positives=3, negatives=3)
    assert population == snapshot
    assert all(set(row) == {"task_id", "positive", "stratum", "subcorpus"} for row in chosen)


def test_shortages_and_invalid_inputs_are_rejected_not_narrowed() -> None:
    with pytest.raises(src.AutomationSourceError):  # not enough positives
        src.select_cases(make_population({"sales": 2, "hr": 2}, PLENTY_NEGATIVES))
    with pytest.raises(src.AutomationSourceError):  # negative shortage in one domain
        src.select_cases(
            make_population({"sales": 3, "hr": 3}, {"sales": 3, "hr": 2}), positives=6, negatives=6
        )
    with pytest.raises(src.AutomationSourceError):  # matched negatives need equal counts
        src.select_cases(make_population({"sales": 40}, {"sales": 40}), positives=30, negatives=20)
    with pytest.raises(src.AutomationSourceError):  # duplicate id
        rows = make_population({"sales": 2}, {"sales": 2})
        src.select_cases([*rows, rows[0]], positives=2, negatives=2)
    with pytest.raises(src.AutomationSourceError):  # unlabelled/unknown class type
        rows = make_population({"sales": 2}, {"sales": 2})
        rows[0]["positive"] = None
        src.select_cases(rows, positives=2, negatives=2)
    with pytest.raises(src.AutomationSourceError):
        src.select_cases(make_population({"sales": 2}, {"sales": 2}), seed="", positives=2, negatives=2)
    with pytest.raises(src.AutomationSourceError):
        src.select_cases(make_population({"sales": 2}, {"sales": 2}), positives=0, negatives=0)


def test_subcorpus_must_be_explicit_when_population_is_mixed() -> None:
    mixed = make_population({"a": 2}, {"a": 2}, subcorpus="one") + [
        {**row, "task_id": "x-" + row["task_id"], "subcorpus": "two"}
        for row in make_population({"a": 2}, {"a": 2})
    ]
    with pytest.raises(src.AutomationSourceError):
        src.select_cases(mixed, positives=2, negatives=2)
    chosen = src.select_cases(mixed, positives=2, negatives=2, subcorpus="two")
    assert {row["subcorpus"] for row in chosen} == {"two"}
    with pytest.raises(src.AutomationSourceError):
        src.select_cases(mixed, positives=2, negatives=2, subcorpus="absent")


# ------------------------------------------------------------------ rendering


def auditor_input(marker: str = "a") -> dict[str, Any]:
    return {
        "repository": "https://example.invalid/bench",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "prompt": [{"role": "user", "content": f"Do task {marker} — naïve ✓"}],
        "answer": {"value": 3},
        "info": {
            "initial_state": {"records": [{"id": "rec_1", "status": "open", "owner_id": 4}]},
            "assertions": [
                {"kind": "x", "scored": True, "status": "pending", "task_id": "inner-1"},
                {"kind": "y", "scored": False, "status": "done"},
            ],
            "zapier_tools": ["tool_b", "tool_a"],
            "meta": {"example_id": 11, "fixed": "business-value", "gold": "kept"},
        },
        "grader_material": {
            "pkg/rubric/__init__.py": "import x\n",
            "pkg/tools/b.py": "B\n",
            "pkg/tools/a.py": "A\r\n  trailing  \n",
            "pkg/a-b.py": "dash",
        },
    }


def test_render_follows_fixed_map_and_preserves_every_original_value() -> None:
    payload = auditor_input()
    files = src.render_auditor_input(payload)
    assert files["instruction.md"] == canon(payload["prompt"])
    assert files["tests/00-task-info.json"] == canon(payload["info"])
    assert files["tests/01-task-answer.json"] == canon(payload["answer"])
    assert files["tests/02-source-provenance.json"] == canon(
        {"repository": payload["repository"], "revision": payload["revision"]}
    )
    for path, text in payload["grader_material"].items():
        assert files["tests/original-grader/" + path] == text
    assert set(files) == {
        "instruction.md",
        "tests/00-task-info.json",
        "tests/01-task-answer.json",
        "tests/02-source-provenance.json",
        *("tests/original-grader/" + path for path in payload["grader_material"]),
    }
    info = json.loads(files["tests/00-task-info.json"])
    assert info == payload["info"]  # nested scored/status/*_id/fixed/gold untouched
    assert info["assertions"][0]["scored"] is True
    assert info["assertions"][0]["status"] == "pending"
    assert info["assertions"][0]["task_id"] == "inner-1"
    assert info["initial_state"]["records"][0]["id"] == "rec_1"
    assert info["meta"] == {"example_id": 11, "fixed": "business-value", "gold": "kept"}
    assert "naïve ✓" in files["instruction.md"]  # not ASCII-escaped


def test_render_is_deterministic_sorted_and_input_order_independent() -> None:
    payload = auditor_input()
    reordered = copy.deepcopy(payload)
    reordered["grader_material"] = dict(reversed(list(payload["grader_material"].items())))
    reordered["info"] = dict(reversed(list(payload["info"].items())))
    first, second, third = (
        src.render_auditor_input(payload),
        src.render_auditor_input(copy.deepcopy(payload)),
        src.render_auditor_input(reordered),
    )
    assert first == second == third
    assert list(first) == list(third)
    graders = [path for path in first if path.startswith("tests/original-grader/")]
    # component-wise order, matching the adapter's sorted(Path) order ('a-b.py' < 'rubric/' < ...)
    assert graders == [
        "tests/original-grader/pkg/a-b.py",
        "tests/original-grader/pkg/rubric/__init__.py",
        "tests/original-grader/pkg/tools/a.py",
        "tests/original-grader/pkg/tools/b.py",
    ]
    assert list(first)[:4] == [
        "instruction.md",
        "tests/00-task-info.json",
        "tests/01-task-answer.json",
        "tests/02-source-provenance.json",
    ]


def test_canonical_json_format() -> None:
    text = src.canonical_json({"b": [1, {"z": None, "é": "ü"}], "a": 1.5})
    assert text == '{\n  "a": 1.5,\n  "b": [\n    1,\n    {\n      "z": null,\n      "é": "ü"\n    }\n  ]\n}\n'
    assert text.endswith("}\n") and not text.endswith("\n\n")
    for bad in (float("nan"), float("inf"), {1: "x"}, ("tuple",), {"s": "\ud800"}):
        with pytest.raises(src.AutomationSourceError):
            src.canonical_json(bad)


def test_render_does_not_truncate_large_sources() -> None:
    payload = auditor_input()
    big = "x" * 300_000
    payload["grader_material"]["pkg/big.py"] = big
    payload["prompt"][0]["content"] = big
    files = src.render_auditor_input(payload)
    assert files["tests/original-grader/pkg/big.py"] == big
    assert json.loads(files["instruction.md"])[0]["content"] == big


def test_render_rejects_envelope_missing_and_mistyped_fields() -> None:
    for extra in ("gold", "labels", "identity", "reconstruction", "auditor_input_status"):
        payload = auditor_input()
        payload[extra] = {"label_state": "SENTINEL"}
        with pytest.raises(src.AutomationSourceError):
            src.render_auditor_input(payload)
    for missing in src.AUDITOR_FIELDS:
        payload = auditor_input()
        del payload[missing]
        with pytest.raises(src.AutomationSourceError):
            src.render_auditor_input(payload)
    whole_spec = {"auditor_input": auditor_input(), "gold": {}, "labels": {}, "identity": {}}
    with pytest.raises(src.AutomationSourceError):
        src.render_auditor_input(whole_spec)
    with pytest.raises(src.AutomationSourceError):
        src.render_auditor_input([])  # type: ignore[arg-type]

    mutations = [
        ("prompt", "text instead of list"),
        ("info", []),
        ("repository", ""),
        ("revision", 3),
        ("grader_material", {}),
        ("grader_material", ["a.py"]),
        ("grader_material", {"a.py": b"bytes"}),
        ("answer", float("nan")),
        ("info", {"assertions": [], "task_name": "leaked-identity"}),
        ("info", {"assertions": [], "fixed": "extra-root"}),
    ]
    for field, value in mutations:
        payload = auditor_input()
        payload[field] = value
        with pytest.raises(src.AutomationSourceError):
            src.render_auditor_input(payload)


def test_render_accepts_any_json_answer_and_partial_info_roots() -> None:
    for answer in (None, "", 0, False, [], "a string"):
        payload = auditor_input()
        payload["answer"] = answer
        payload["info"] = {"assertions": []}
        files = src.render_auditor_input(payload)
        assert json.loads(files["tests/01-task-answer.json"]) == answer


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd", "../x.py", "a/../b.py", "a/./b.py", "a//b.py", "a/b/", "", "./a.py",
        "a\\b.py", "C:/x.py", "c:x.py", "a/\x00b.py", "a/\nb.py", "..",
    ],
)
def test_render_rejects_unsafe_grader_paths(path: str) -> None:
    payload = auditor_input()
    payload["grader_material"][path] = "text"
    with pytest.raises(src.AutomationSourceError):
        src.render_auditor_input(payload)


def test_render_rejects_non_string_grader_path() -> None:
    payload = auditor_input()
    payload["grader_material"][7] = "text"
    with pytest.raises(src.AutomationSourceError):
        src.render_auditor_input(payload)


@pytest.mark.parametrize(
    "paths",
    [
        ("pkg/A.py", "pkg/a.py"),
        ("pkg/caf\u00e9.py", "pkg/cafe\u0301.py"),
        ("pkg/mod", "pkg/mod/inner.py"),
        ("pkg", "PKG/inner.py"),
    ],
)
def test_render_rejects_grader_path_collisions(paths: tuple[str, str]) -> None:
    payload = auditor_input()
    payload["grader_material"] = {paths[0]: "one", paths[1]: "two"}
    with pytest.raises(src.AutomationSourceError):
        src.render_auditor_input(payload)


def test_source_record_hash_covers_whole_auditor_input_canonically() -> None:
    payload = auditor_input()
    reordered = {key: payload[key] for key in reversed(list(payload))}
    assert src.source_record_sha256(payload) == sha(canon(payload))
    assert src.source_record_sha256(reordered) == src.source_record_sha256(payload)
    changed = copy.deepcopy(payload)
    changed["grader_material"]["pkg/b.py"] = "different"
    assert src.source_record_sha256(changed) != src.source_record_sha256(payload)


def test_key_name_audit_records_paths_without_touching_source() -> None:
    payload = auditor_input()
    snapshot = copy.deepcopy(payload)
    paths = src.key_name_audit(payload, {"scored", "status"})
    assert payload == snapshot
    assert paths == sorted(paths)
    assert "/info/assertions/0/scored" in paths
    assert "/info/assertions/0/status" in paths
    assert "/info/assertions/0/task_id" in paths
    assert "/info/initial_state/records/0/id" in paths
    assert "/info/initial_state/records/0/owner_id" in paths
    assert "/info/meta/example_id" in paths
    assert "/info/meta/gold" not in paths  # key-name set only; values never inspected
    assert not any(path.startswith("/grader_material") for path in paths)


# -------------------------------------------------------------- iter_selected

SENTINELS = ("SENTINEL_GOLD", "SENTINEL_LABEL", "SENTINEL_RECON", "SENTINEL_CLUSTER_EXTRA")


def make_spec(name: str, status: str = "ready", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "auditor_input": payload if payload is not None else auditor_input(name),
        "auditor_input_status": status,
        "gold": {"label_state": "SENTINEL_GOLD"},
        "labels": {"label_state": "SENTINEL_LABEL", "issue_group": "SENTINEL_LABEL", "scored": True},
        "identity": {
            "task_name": name,
            "example_id": "5",
            "domain": "sales",
            "cluster_key": "sales",
            "extra": "SENTINEL_CLUSTER_EXTRA",
        },
        "reconstruction": {"original_task_available": True, "note": "SENTINEL_RECON"},
    }


def selection(*names: str) -> list[dict[str, Any]]:
    return [
        {"task_id": name, "positive": True, "stratum": "sales", "subcorpus": CORPUS} for name in names
    ]


def adapter_for(specs: list[dict[str, Any]]) -> SimpleNamespace:
    return fake_adapter(lambda bundle: iter(specs))


def test_iter_selected_emits_only_auditor_input_in_selected_order(tmp_path: Path) -> None:
    specs = [make_spec("t1"), make_spec("t2"), make_spec("t3")]
    adapter = adapter_for(specs)
    records = list(src.iter_selected(adapter, tmp_path, selection("t3", "t1")))
    assert [r["task_id"] for r in records] == ["t3", "t1"]
    first = records[1]
    assert set(first) == {
        "task_id", "subcorpus", "auditor_files", "identity", "source_record_sha256",
        "preparation_status", "preparation_error",
    }
    assert first["subcorpus"] == CORPUS
    assert first["preparation_status"] == "prepared" and first["preparation_error"] is None
    assert first["auditor_files"] == src.render_auditor_input(specs[0]["auditor_input"])
    assert first["identity"] == {
        "task_name": "t1", "example_id": "5", "domain": "sales", "cluster_key": "sales",
    }
    assert first["source_record_sha256"] == sha(canon(specs[0]["auditor_input"]))
    # no envelope content anywhere in the emitted records
    dump = json.dumps(records, ensure_ascii=False)
    for sentinel in SENTINELS:
        assert sentinel not in dump
    for record in records:
        assert not any(key in record for key in ("gold", "labels", "reconstruction"))
        for path in record["auditor_files"]:
            assert path == "instruction.md" or path.startswith("tests/")
            assert "gold" not in path and "label" not in path


def test_source_hash_and_files_do_not_depend_on_envelope_labels(tmp_path: Path) -> None:
    plain = make_spec("t1")
    other = make_spec("t1")
    other["gold"] = {"label_state": "different"}
    other["labels"] = {"label_state": "different"}
    other["reconstruction"] = {"original_task_available": False}
    a = list(src.iter_selected(adapter_for([plain]), tmp_path, selection("t1")))[0]
    b = list(src.iter_selected(adapter_for([other]), tmp_path, selection("t1")))[0]
    assert a == b


def test_iter_selected_is_deterministic_and_receives_bundle_path(tmp_path: Path) -> None:
    calls: list[Path] = []

    def specs(bundle: Path) -> Any:
        calls.append(bundle)
        return iter([make_spec("t1"), make_spec("t2")])

    adapter = fake_adapter(specs)
    one = list(src.iter_selected(adapter, tmp_path, selection("t1", "t2")))
    two = list(src.iter_selected(adapter, tmp_path, selection("t1", "t2")))
    assert one == two
    assert calls == [tmp_path, tmp_path]
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_non_ready_status_is_source_unavailable_never_substituted(tmp_path: Path) -> None:
    partial = {"repository": "https://example.invalid/bench", "revision": "r" * 40}
    specs = [make_spec("t1", status="original_grader_unavailable", payload=partial), make_spec("t2")]
    records = list(src.iter_selected(adapter_for(specs), tmp_path, selection("t1", "t2")))
    assert records[0]["preparation_status"] == "source_unavailable"
    assert records[0]["preparation_error"] == "adapter_status:original_grader_unavailable"
    assert records[0]["auditor_files"] == {}
    assert records[0]["source_record_sha256"] == sha(canon(partial))
    assert records[1]["preparation_status"] == "prepared"


def test_ready_spec_with_invalid_auditor_input_is_source_unavailable(tmp_path: Path) -> None:
    bad = auditor_input("t1")
    bad["grader_material"]["../escape.py"] = "x"
    leaking = auditor_input("t2")
    leaking["gold"] = {"label_state": "SENTINEL_GOLD"}
    specs = [make_spec("t1", payload=bad), make_spec("t2", payload=leaking)]
    records = list(src.iter_selected(adapter_for(specs), tmp_path, selection("t1", "t2")))
    for record in records:
        assert record["preparation_status"] == "source_unavailable"
        assert record["preparation_error"].startswith("invalid_auditor_input:")
        assert record["auditor_files"] == {}
    assert "SENTINEL_GOLD" not in json.dumps(records)


def test_unselected_specs_are_not_projected(tmp_path: Path) -> None:
    unselected = {"identity": make_spec("other")["identity"]}  # no auditor_input at all
    records = list(
        src.iter_selected(adapter_for([unselected, make_spec("t1")]), tmp_path, selection("t1"))
    )
    assert [r["task_id"] for r in records] == ["t1"]


def test_missing_and_duplicate_ids_raise_before_any_record(tmp_path: Path) -> None:
    specs = [make_spec("t1"), make_spec("t2")]
    with pytest.raises(src.AutomationSourceError):  # selected id absent from the adapter
        list(src.iter_selected(adapter_for(specs), tmp_path, selection("t1", "ghost")))
    with pytest.raises(src.AutomationSourceError):  # duplicate selected id
        src.iter_selected(adapter_for(specs), tmp_path, selection("t1", "t1"))
    with pytest.raises(src.AutomationSourceError):  # duplicate id yielded by the adapter
        list(src.iter_selected(adapter_for([*specs, make_spec("t2")]), tmp_path, selection("t1")))
    with pytest.raises(src.AutomationSourceError):  # empty selection
        src.iter_selected(adapter_for(specs), tmp_path, [])
    with pytest.raises(src.AutomationSourceError):  # subcorpus must match the adapter corpus
        src.iter_selected(
            adapter_for(specs), tmp_path,
            [{"task_id": "t1", "positive": True, "stratum": "sales", "subcorpus": "other"}],
        )


def test_malformed_identity_raises(tmp_path: Path) -> None:
    spec = make_spec("t1")
    del spec["identity"]["example_id"]
    with pytest.raises(src.AutomationSourceError):
        list(src.iter_selected(adapter_for([spec]), tmp_path, selection("t1")))
    with pytest.raises(src.AutomationSourceError):
        list(src.iter_selected(adapter_for([{"auditor_input": {}}]), tmp_path, selection("t1")))


def test_selected_rows_are_not_mutated(tmp_path: Path) -> None:
    chosen = selection("t1")
    snapshot = copy.deepcopy(chosen)
    list(src.iter_selected(adapter_for([make_spec("t1")]), tmp_path, chosen))
    assert chosen == snapshot
