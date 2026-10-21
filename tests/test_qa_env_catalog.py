"""The reviewer's hackbook is a pinned, generic taxonomy lookup that cannot leak case material."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from evallab.qa.env_catalog import DefectCatalog
from evallab.qa.env_playbooks import defect_classes

LEAK = "zzz-hidden-case-label"
PUBLIC = {"id", "key", "name", "definition", "direction", "fix_location", "subtypes",
          "evidence_standard", "detection_methods"}


def _row(index: int, key: str) -> dict[str, Any]:
    return {
        "id": f"TS{index:02d}", "key": key, "name": key.replace("_", " "),
        "definition": f"Generic family number {index}.",
        "direction": "evaluation too lenient", "fix_location": "grader",
        "subtypes": ["subtype-a", "subtype-b"],
        "evidence_standard": "A concrete counterexample.",
        "detection_methods": ["read the grader source"],
        # Source-file material a reviewer must never be handed:
        "examples": [{"case": LEAK, "payload": "rm -rf /"}],
        "case_label": LEAK, "urls": ["https://example.test/" + LEAK],
        "exploit_program": "import os; os.system('id')", "probe": "assert False",
    }


def _document() -> dict[str, Any]:
    return {"version": 3, "task_side": [_row(i, key) for i, key in enumerate(defect_classes(), 1)]}


def _write(path: Path, document: Any, suffix: str = ".json") -> Path:
    target = path.with_suffix(suffix)
    text = json.dumps(document) if suffix == ".json" else yaml.safe_dump(document)
    target.write_text(text)
    return target


def _row_of(document: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in document["task_side"] if row["key"] == key)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_lookup_hands_out_only_generic_taxonomy_fields(root: Path, suffix: str) -> None:
    path = _write(root / "hackbook", _document(), suffix)
    result = DefectCatalog(path).lookup({})
    assert {m["key"] for m in result["matches"]} == set(defect_classes())
    assert all(set(m) <= PUBLIC and {"key", "definition"} <= set(m) for m in result["matches"])
    text = json.dumps(result)
    for secret in (LEAK, "rm -rf", "example.test", "os.system", "assert False"):
        assert secret not in text
    assert result["catalog_digest"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["catalog_version"] == 3
    assert result["evidence_basis"] == "generic_taxonomy"
    assert result["case_examples_included"] is False and result["executable_payloads_included"] is False


def test_query_selects_by_all_words_over_public_text_only(root: Path) -> None:
    document = _document()
    _row_of(document, "weak_check")["detection_methods"] = ["compare-against-instruction"]
    catalog = DefectCatalog(_write(root / "hackbook", document))

    only = catalog.lookup({"query": "compare-against-instruction"})
    assert [m["key"] for m in only["matches"]] == ["weak_check"] and only["total_matches"] == 1

    others = catalog.lookup({"query": "GRADER Source"})  # case-insensitive, every word must match
    assert others["total_matches"] == 10
    assert "weak_check" not in {m["key"] for m in others["matches"]}

    assert catalog.lookup({"query": "compare-against-instruction read"})["matches"] == []
    assert catalog.lookup({})["total_matches"] == 11

    limited = catalog.lookup({"query": "grader source", "limit": 3})
    assert len(limited["matches"]) == 3 and limited["total_matches"] == 10

    # Words that exist only in withheld source fields cannot be used as a probing oracle.
    for hidden in (LEAK, "rm -rf", "os.system"):
        assert catalog.lookup({"query": hidden})["total_matches"] == 0


def test_class_filter_restricts_the_search_to_that_class(root: Path) -> None:
    catalog = DefectCatalog(_write(root / "hackbook", _document()))
    one = catalog.lookup({"defect_class": "answer_reachable"})
    assert [m["key"] for m in one["matches"]] == ["answer_reachable"]
    assert catalog.lookup({"defect_class": "answer_reachable", "query": "weak"})["matches"] == []


@pytest.mark.parametrize("arguments", [
    {"extra": 1}, {"defect_class": "invented_class"}, {"defect_class": 5},
    {"limit": 0}, {"limit": 12}, {"limit": True}, {"query": 5}, {"query": "x" * 1001},
])
def test_lookup_refuses_arguments_outside_the_documented_surface(root: Path, arguments: dict[str, Any]) -> None:
    catalog = DefectCatalog(_write(root / "hackbook", _document()))
    with pytest.raises(ValueError):
        catalog.lookup(arguments)


def test_lookup_boundaries_are_inclusive(root: Path) -> None:
    catalog = DefectCatalog(_write(root / "hackbook", _document()))
    assert len(catalog.lookup({"limit": 11})["matches"]) == 11
    assert len(catalog.lookup({"limit": 1})["matches"]) == 1
    assert catalog.lookup({"query": "x" * 1000})["matches"] == []


# -- shape of the source ---------------------------------------------------------------------


def _mutated(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    document = copy.deepcopy(_document())
    change(document)
    return document


def _first(document: dict[str, Any]) -> dict[str, Any]:
    return document["task_side"][0]


MALFORMED: dict[str, Callable[[dict[str, Any]], None]] = {
    "nested definition": lambda d: _first(d).__setitem__("definition", {"case": LEAK}),
    "nested name": lambda d: _first(d).__setitem__("name", {"case": LEAK}),
    "nested subtype": lambda d: _first(d).__setitem__("subtypes", [{"case": LEAK}]),
    "methods as one string": lambda d: _first(d).__setitem__("detection_methods", "read the source"),
    "nested method list": lambda d: _first(d).__setitem__("detection_methods", [["read"]]),
    "empty definition": lambda d: _first(d).__setitem__("definition", "  "),
    "missing definition": lambda d: _first(d).pop("definition"),
    "repeated class": lambda d: d["task_side"][1].__setitem__("key", _first(d)["key"]),
    "uncovered class": lambda d: d["task_side"].pop(),
    "invented class": lambda d: _first(d).__setitem__("key", "invented_class"),
    "list version": lambda d: d.__setitem__("version", [1]),
    "boolean version": lambda d: d.__setitem__("version", True),
    "missing version": lambda d: d.pop("version"),
    "task_side is not a list": lambda d: d.__setitem__("task_side", {"weak_check": {}}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_catalog_that_could_carry_hidden_case_data_or_miss_a_class_is_refused(root: Path, name: str) -> None:
    path = _write(root / "hackbook", _mutated(MALFORMED[name]))
    with pytest.raises(ValueError):
        DefectCatalog(path)


@pytest.mark.parametrize("raw", ['["not", "a", "mapping"]', '{"task_side": '])
def test_a_source_that_is_not_the_shared_schema_is_refused(root: Path, raw: str) -> None:
    path = root / "hackbook.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        DefectCatalog(path)


def test_malformed_yaml_is_a_setup_error_not_a_parser_crash(root: Path) -> None:
    path = root / "hackbook.yaml"
    path.write_text("task_side: [unclosed\nversion: 1\n")
    with pytest.raises(ValueError):
        DefectCatalog(path)


def test_source_size_limit_is_two_megabytes_inclusive(root: Path) -> None:
    document = _document()
    document["padding"] = ""
    overhead = len(json.dumps(document).encode())
    path = root / "hackbook.json"

    document["padding"] = "x" * (2_000_000 - overhead)
    path.write_text(json.dumps(document))
    assert path.stat().st_size == 2_000_000
    assert DefectCatalog(path).lookup({"limit": 1})["total_matches"] == 11

    document["padding"] += "x"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        DefectCatalog(path)


# -- identity is pinned and rechecked ----------------------------------------------------------


def test_changed_bytes_are_refused_on_lookup_and_the_original_is_accepted_again(root: Path) -> None:
    path = _write(root / "hackbook", _document())
    original = path.read_bytes()
    catalog = DefectCatalog(path)
    path.write_bytes(original.replace(b"Generic family number 1.", b"Generic family number 9."))
    with pytest.raises(ValueError):
        catalog.lookup({})
    path.write_bytes(original)
    assert catalog.lookup({})["total_matches"] == 11


def test_replacing_the_pinned_file_with_a_symlink_is_refused_even_with_identical_bytes(root: Path) -> None:
    path = _write(root / "hackbook", _document())
    catalog = DefectCatalog(path)
    twin = root / "twin.json"
    twin.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(twin)
    with pytest.raises(ValueError):
        catalog.lookup({})


def test_swapping_a_parent_directory_for_a_symlink_is_refused(root: Path) -> None:
    folder = root / "catalog"
    folder.mkdir()
    catalog = DefectCatalog(_write(folder / "hackbook", _document()))
    folder.rename(root / "catalog-real")
    folder.symlink_to(root / "catalog-real")
    with pytest.raises(ValueError):
        catalog.lookup({})


@pytest.mark.parametrize("where", ["file", "directory"])
def test_a_symlinked_source_path_is_refused_at_pinning(root: Path, where: str) -> None:
    real = root / "real"
    real.mkdir()
    target = _write(real / "hackbook", _document())
    if where == "file":
        link = root / "link.json"
        link.symlink_to(target)
        path = link
    else:
        (root / "linked").symlink_to(real)
        path = root / "linked" / target.name
    with pytest.raises(ValueError):
        DefectCatalog(path)
