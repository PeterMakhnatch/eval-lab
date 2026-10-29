"""Synthetic-only tests for the OQ-10 scoring bridge.

The frozen Library scorer's real bytes are copied beside a synthetic registry (the
"copied together" layout), and its own gold builders run on synthetic staged
bundles and a synthetic PAIChecker adapter. No real registry, bundle, benchmark
record, model output or network is touched; every identity starts with ``syn-``.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "research/experiments/oq10-task-quality-cross/scoring.py"
)
_SPEC = importlib.util.spec_from_file_location("oq10_scoring_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
scoring = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = scoring
_SPEC.loader.exec_module(scoring)

LIBRARY_SCORER = Path(
    os.environ.get(
        "OQ10_TASK_QA_SCORER",
        str(Path.home() / "Developer/research-context/bridge/2026-10-27-run-atlas/score_task_qa_gate.py"),
    )
)
pytestmark = pytest.mark.skipif(
    not LIBRARY_SCORER.is_file(), reason="frozen Library score_task_qa_gate.py is not on this host"
)

AB = scoring.AUTOMATIONBENCH
PAI = scoring.PAICHECKER
AB_VIEW = "parsewave__automationbench-verified"
ML, GYM = scoring.PAI_SUBCORPORA
OLD, NEW = scoring.GATES
ROLES = {
    "sonnet": "anthropic/claude-sonnet-5-5",
    "kimi": "kimi-code/k3.1-pro",
    "haiku": "anthropic/claude-haiku-5-5",
}
BODY_MARKER = "SYNTHETIC-MODEL-OUTPUT-BODY"
AB_ADAPTER = "adapters/automationbench/adapter.py"
PAI_ADAPTER = "adapters/paichecker/adapter.py"
PAI_ADAPTER_SOURCE = '''import json
from pathlib import Path

SUB_CORPORA = {
    "swe_bench_multilingual": {"labels": "ml-labels.json", "tasks": "ml-tasks.json"},
    "swe_gym": {"labels": "gym-labels.json", "tasks": "gym-tasks.json"},
}


def load_labels(path):
    return {key: tuple(value) for key, value in json.loads(Path(path).read_text()).items()}


def task_rows(path, columns):
    for row in json.loads(Path(path).read_text()):
        yield {name: row[name] for name in columns}
'''

# Native synthetic population: (task_name, domain, scored, issue_group or None).
AB_TASKS = [
    ("syn-ab-alpha", "dom-one", "True", "group-x"),
    ("syn-ab-bravo", "dom-one", "True", None),
    ("syn-ab-charlie", "dom-one", "False", None),
    ("syn-ab-delta", "dom-two", "True", "group-y"),
    ("syn-ab-echo", "dom-two", "False", "group-x"),
    ("syn-ab-foxtrot", "dom-two", "True", None),
]
# (instance_id, repo, families or None = unlabelled). The shared id is positive in
# one sub-corpus and negative in the other.
PAI_TASKS = {
    ML: [("syn-shared__1", "repo-one", ["SC"]), ("syn-ml__2", "repo-two", []),
         ("syn-ml__3", "repo-one", []), ("syn-ml__4", "repo-two", None)],
    GYM: [("syn-shared__1", "repo-three", []), ("syn-gym__2", "repo-four", ["DP", "IS"]),
          ("syn-gym__3", "repo-three", [])],
}
SELECTED = [
    ("C0001", AB, AB_VIEW, {"task_name": "syn-ab-alpha", "example_id": "ex-0", "domain": "dom-one", "cluster_key": "dom-one"}),
    ("C0002", AB, AB_VIEW, {"task_name": "syn-ab-bravo", "example_id": "ex-1", "domain": "dom-one", "cluster_key": "dom-one"}),
    ("C0003", AB, AB_VIEW, {"task_name": "syn-ab-delta", "example_id": "ex-3", "domain": "dom-two", "cluster_key": "dom-two"}),
    ("C0004", AB, AB_VIEW, {"task_name": "syn-ab-foxtrot", "example_id": "ex-5", "domain": "dom-two", "cluster_key": "dom-two"}),
    ("C0005", PAI, ML, {"instance_id": "syn-shared__1", "sub_corpus": ML, "repo": "repo-one"}),
    ("C0006", PAI, ML, {"instance_id": "syn-ml__2", "sub_corpus": ML, "repo": "repo-two"}),
    ("C0007", PAI, GYM, {"instance_id": "syn-shared__1", "sub_corpus": GYM, "repo": "repo-three"}),
    ("C0008", PAI, GYM, {"instance_id": "syn-gym__2", "sub_corpus": GYM, "repo": "repo-four"}),
]
# Sonnet old-gate flags; the new gate differs only on C0003 (an AutomationBench positive).
SONNET_OLD = {
    "C0001": True, "C0002": False, "C0003": False, "C0004": True,
    "C0005": True, "C0006": False, "C0007": True, "C0008": True,
}
_SAME = object()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hash(case_id: str) -> str:
    return sha(f"synthetic-source-{case_id}".encode())


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def pin(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha(path.read_bytes())}


def sealed_rows() -> list[dict[str, Any]]:
    rows = []
    for case_id, corpus, subcorpus, identity in SELECTED:
        task_id = identity["task_name"] if corpus == AB else f"{subcorpus}:{identity['instance_id']}"
        rows.append({
            "case_id": case_id, "corpus": corpus, "subcorpus": subcorpus, "task_id": task_id,
            "identity": dict(identity), "source_record_sha256": source_hash(case_id),
        })
        rows[-1]["record_binding_sha256"] = source_hash(case_id + "b")
    return rows


def public_rows() -> list[dict[str, Any]]:
    return [
        {"case_id": case_id, "corpus": corpus, "preparation_status": "prepared",
         "source_record_sha256": source_hash(case_id),
         "record_binding_sha256": source_hash(case_id + "b"),
         "source": {"path": f"/synthetic/{case_id}/source.txt", "sha256": source_hash(case_id + "v")},
         "prompt": {"path": f"/synthetic/{case_id}/prompt.txt", "sha256": source_hash(case_id + "p")}}
        for case_id, corpus, _, _ in SELECTED
    ]


def binding_fields(case_id: str) -> dict[str, str]:
    return {
        "record_binding_sha256": source_hash(case_id + "b"),
        "source_record_sha256": source_hash(case_id),
        "source_sha256": source_hash(case_id + "v"),
        "prompt_sha256": source_hash(case_id + "p"),
    }


def cell(
    case_id: str, role: str, old: Any, new: Any = _SAME, *, status: str | None = None,
    prediction_status: str | None = None, terminal: bool = True,
) -> dict[str, Any]:
    new = old if new is _SAME else new
    flags = {OLD: old, NEW: new}
    decided = old is not None and new is not None
    return {
        "case_id": case_id, "role": role, "route": ROLES[role], "terminal": terminal,
        "status": status or ("ok" if decided else "missing"), "native_disposition": "answered",
        "flags": flags,
        **binding_fields(case_id),
        "prediction": {
            "status": prediction_status or ("ok" if decided else "missing"), "flags": dict(flags),
            **binding_fields(case_id),
            "parsed_payloads": [{"note": BODY_MARKER}] if decided else None,
        },
    }


def sonnet_cells() -> list[dict[str, Any]]:
    return [cell(c, "sonnet", v, True if c == "C0003" else v) for c, v in SONNET_OLD.items()]


def file_pins(directory: Path, names: list[str]) -> dict[str, str]:
    return {name: sha((directory / name).read_bytes()) for name in names}


@pytest.fixture
def library(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path.resolve()
    scorer = root / "library" / "score_task_qa_gate.py"
    scorer.parent.mkdir(parents=True)
    scorer.write_bytes(LIBRARY_SCORER.read_bytes())
    registry = write(root / "library" / "cross-test-registry.json", json.dumps({"entries": [
        {"id": AB, "bundle_path": "~/no-such-synthetic-bundle", "adapter": {"module_path": AB_ADAPTER},
         "labels": {"counts": {"rows": 6, "defective_verified": 3, "domains": {"dom-one": 3, "dom-two": 3}}}},
        {"id": PAI, "bundle_path": "~/no-such-synthetic-bundle", "adapter": {"module_path": PAI_ADAPTER},
         "labels": {"counts": {ML: {"rows": 3}, GYM: {"rows": 3}}}},
    ]}))
    eval_lab = root / "mini-eval-lab"
    ab_adapter = write(eval_lab / AB_ADAPTER, "# synthetic AutomationBench adapter placeholder\n")
    pai_adapter = write(eval_lab / PAI_ADAPTER, PAI_ADAPTER_SOURCE)

    ab_bundle = root / "ab-bundle"
    write(ab_bundle / "STAGE-MANIFEST.json", "{}\n")
    write_csv(ab_bundle / "tasks.csv", ["task_name", "domain", "scored", "example_id"],
              [[name, domain, scored, f"ex-{i}"] for i, (name, domain, scored, _) in enumerate(AB_TASKS)])
    write_csv(ab_bundle / "fixes.csv", ["task_name", "issue_group"],
              [[name, group] for name, _, _, group in AB_TASKS if group])
    pai_bundle = root / "pai-bundle"
    write(pai_bundle / "STAGE-MANIFEST.json", "{}\n")
    for sub, prefix in ((ML, "ml"), (GYM, "gym")):
        rows = PAI_TASKS[sub]
        write(pai_bundle / f"{prefix}-tasks.json", json.dumps([{"instance_id": i, "repo": r} for i, r, _ in rows]))
        write(pai_bundle / f"{prefix}-labels.json", json.dumps({i: f for i, _, f in rows if f is not None}))

    frozen = scoring.load_scorer(pin(scorer), pin(registry))
    adapters = {AB: sha(ab_adapter.read_bytes()), PAI: sha(pai_adapter.read_bytes())}
    bundle_files = {
        AB: file_pins(ab_bundle, ["STAGE-MANIFEST.json", "tasks.csv", "fixes.csv"]),
        PAI: file_pins(pai_bundle, ["STAGE-MANIFEST.json", "ml-labels.json", "ml-tasks.json",
                                    "gym-labels.json", "gym-tasks.json"]),
    }
    native = scoring.load_native_gold(
        frozen, eval_lab_root=eval_lab, adapters=adapters, bundle_files=bundle_files,
        automationbench_bundle=ab_bundle, paichecker_bundle=pai_bundle,
    )
    return SimpleNamespace(
        root=root, scorer=scorer, registry=registry, eval_lab=eval_lab, adapters=adapters,
        bundle_files=bundle_files, ab_bundle=ab_bundle, pai_bundle=pai_bundle, frozen=frozen, native=native,
    )


def run(lib: SimpleNamespace, records: list[dict[str, Any]], *, public=None, sealed=None) -> dict[str, Any]:
    return scoring.score_cross_test(
        lib.frozen, lib.native,
        public_cases=public_rows() if public is None else public,
        sealed_cases=sealed_rows() if sealed is None else sealed,
        records=records, roles=ROLES,
    )


def scored(result: dict[str, Any], view: str, role: str, gate: str = OLD) -> tuple[dict, dict]:
    entry = result["views"][view]["roles"][role]["gates"][gate]
    return entry, entry["score"]["views"][view]


# --------------------------------------------------------------------------- scoring


def test_native_gold_scored_per_view_with_pinned_bootstrap_and_no_identity_leak(library):
    result = run(library, sonnet_cells())

    assert sorted(result["views"]) == sorted([AB_VIEW, ML, GYM])  # no pooled PAIChecker view
    ab_cell, ab = scored(result, AB_VIEW, "sonnet")
    assert (ab["tasks"], ab["positives"], ab["negatives"]) == (4, 2, 2)  # sample only, not the 6 native tasks
    assert ab["confusion"] == {"tp": 1, "fn": 1, "unverified_alarms": 1, "unflagged_unverified": 1}
    assert {g: v["positives_in_group"] for g, v in ab["recall_by_group"]["groups"].items()} == {
        "group-x": 1, "group-y": 1,
    }
    assert ab["clusters"] == {"kind": "domain", "count": 2} and ab["recall"]["ci95"] is not None
    assert ab["rates_are"] == scoring.SAMPLE_RATES and ab["scorer_rates_are"] == "full-population"
    assert scored(result, AB_VIEW, "sonnet", NEW)[1]["confusion"]["tp"] == 2
    assert ab_cell["derived"]["conditional_precision"]["value"] == 0.5

    # The bare id shared by both PAIChecker sub-corpora joins to two different native tasks.
    _, ml = scored(result, ML, "sonnet")
    gym_cell, gym = scored(result, GYM, "sonnet")
    assert ml["confusion"] == {"tp": 1, "fn": 0, "fp_false_alarms": 0, "tn": 1}
    assert gym["confusion"] == {"tp": 1, "fn": 0, "fp_false_alarms": 1, "tn": 0}
    assert set(ml["recall_by_group"]["groups"]) == {"SC"}
    assert set(gym["recall_by_group"]["groups"]) == {"DP", "IS"}
    assert gym_cell["derived"]["precision_on_human_aligned"]["value"] == 0.5
    assert ml["clusters"] == {"kind": "repo", "count": 2}

    boot = ab_cell["score"]["bootstrap"]
    assert (boot["resamples"], boot["seed"]) == (10_000, 0)
    assert ab_cell["score"]["script_sha256"] == library.frozen.scorer_sha256
    assert result["dependencies"]["native_gold"]["adapters"][PAI]["sha256"] == library.adapters[PAI]
    assert run(library, sonnet_cells()) == result

    # Dependencies carry local tmp paths and pins only; everything else must be aggregate.
    text = json.dumps({key: value for key, value in result.items() if key != "dependencies"})
    assert "syn-" not in text and BODY_MARKER not in text and re.search(r"C\d{4}", text) is None


def test_null_and_absent_flags_stay_missing_never_negative(library):
    haiku = [
        cell("C0001", "haiku", True), cell("C0002", "haiku", None), cell("C0003", "haiku", False),
    ]
    result = run(library, sonnet_cells() + haiku)

    haiku_cell, ab = scored(result, AB_VIEW, "haiku")
    assert ab["confusion"] == {"tp": 1, "fn": 1, "unverified_alarms": 0, "unflagged_unverified": 0}
    assert ab["missing"] == {"positives": 0, "negatives": 2} and ab["complete"] is False
    assert ab["unverified_alarm_rate"]["value"] is None and ab["unverified_alarm_rate"]["den"] == 0
    assert ab["rates_are"] != scoring.SAMPLE_RATES
    assert haiku_cell["coverage"] == {
        "planned": 4, "records": 3, "decided": 2, "missing_no_record": 1, "missing_null_flag": 1,
        "null_flag_by_status": {"missing": 1},
    }
    assert scored(result, AB_VIEW, "sonnet")[1]["confusion"]["unflagged_unverified"] == 1

    # A role with no cells at all is all-missing, not all-negative.
    for view in (ML, GYM):
        kimi_cell, kimi = scored(result, view, "kimi")
        assert kimi["covered"] == {"positives": 0, "negatives": 0}
        assert kimi["recall"]["value"] is None and kimi["recall"]["ci95"] is None
        assert kimi_cell["coverage"]["missing_no_record"] == 2 and kimi_cell["coverage"]["decided"] == 0
        assert kimi_cell["derived"]["precision_on_human_aligned"]["value"] is None
        assert kimi_cell["derived"]["recall_wilson95"]["interval"] is None


def test_source_unavailable_case_is_missing_and_cannot_carry_a_decision(library):
    public = public_rows()
    public[5]["preparation_status"] = "source_unavailable"
    unavailable = {
        "case_id": "C0006", "role": "sonnet", "route": ROLES["sonnet"], "terminal": True,
        "status": "source_unavailable", "flags": {OLD: None, NEW: None},
        **binding_fields("C0006"),
    }
    records = [r for r in sonnet_cells() if r["case_id"] != "C0006"] + [unavailable]
    cell_ml, ml = scored(run(library, records, public=public), ML, "sonnet")
    assert ml["missing"] == {"positives": 0, "negatives": 1}
    assert cell_ml["coverage"]["null_flag_by_status"] == {"source_unavailable": 1}

    with pytest.raises(scoring.ScoringBridgeError, match="decision on an unprepared case"):
        run(library, sonnet_cells(), public=public)


def test_load_cells_projects_terminal_cells_and_checks_their_names(library):
    cells = library.root / "cells"
    for record in sonnet_cells():
        write(cells / "sonnet" / f"{record['case_id']}.json", json.dumps(record))
    loaded = scoring.load_cells(cells, ROLES)
    assert all("parsed_payloads" not in r["prediction"] for r in loaded)
    assert run(library, loaded) == run(library, sonnet_cells())

    write(cells / "sonnet" / "C0009.json", json.dumps(cell("C0001", "sonnet", True)))
    with pytest.raises(scoring.ScoringBridgeError, match="names another case or role"):
        scoring.load_cells(cells, ROLES)


def test_summarize_cell_inputs_maps_scorer_confusion(library):
    result = run(library, sonnet_cells())
    ab = scoring.summarize_cell_inputs(result, view=AB_VIEW, role="sonnet", gate=OLD)
    assert {k: v for k, v in ab.items() if k != "cluster_result"} == {
        "corpus": AB_VIEW, "planned_positive": 2, "planned_negative": 2,
        "confusion": {"TP": 1, "FN": 1, "FP": 1, "TN": 1},
    }
    assert ab["cluster_result"] is scored(result, AB_VIEW, "sonnet")[1]
    gym = scoring.summarize_cell_inputs(result, view=GYM, role="sonnet", gate=OLD)
    assert gym["confusion"] == {"TP": 1, "FN": 0, "FP": 1, "TN": 0}
    with pytest.raises(scoring.ScoringBridgeError, match="no scored cell"):
        scoring.summarize_cell_inputs(result, view="pooled_secondary", role="sonnet", gate=OLD)


# --------------------------------------------------------------------------- binding


def test_bind_inputs_is_label_free_and_matches_scoring_digests(library):
    bound = scoring.bind_inputs(public_rows(), sealed_rows())
    assert bound["views"] == {
        AB_VIEW: {"corpus": AB, "cases": 4}, ML: {"corpus": PAI, "cases": 2}, GYM: {"corpus": PAI, "cases": 2},
    }
    inputs = run(library, [])["inputs"]
    assert bound["public_cases_sha256"] == inputs["public_cases_sha256"]
    assert bound["sealed_cases_sha256"] == inputs["sealed_cases_sha256"]
    assert "syn-" not in json.dumps(bound) and "positive" not in json.dumps(bound)


def _set(rows: list[dict], index: int, path: tuple[str, ...], value: Any) -> list[dict]:
    target = rows[index]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return rows


MANIFEST_ERRORS = {
    "public case missing": (lambda p, s: (p[:-1], s), "case ids differ"),
    "extra public case": (lambda p, s: (p + [dict(p[0], case_id="C0009")], s), "case ids differ"),
    "corpus mismatch": (lambda p, s: (_set(p, 4, ("corpus",), AB), s), "corpus differ"),
    "label in public": (lambda p, s: (_set(p, 0, ("positive",), True), s), "label/identity fields"),
    "source hash mismatch": (
        lambda p, s: (_set(p, 0, ("source_record_sha256",), source_hash("other")), s),
        "source_record_sha256 differ",
    ),
    "foreign PAI prefix": (lambda p, s: (p, _set(s, 4, ("task_id",), f"{GYM}:syn-shared__1")), "task_id is not"),
    "PAI sub-corpus binding": (lambda p, s: (p, _set(s, 4, ("identity", "sub_corpus"), GYM)), "binding differs"),
    "sealed extra key": (lambda p, s: (p, _set(s, 0, ("positive",), True)), "keys must be exactly"),
    "same task twice": (
        lambda p, s: (p, _set(_set(s, 1, ("identity", "task_name"), "syn-ab-alpha"), 1, ("task_id",), "syn-ab-alpha")),
        "repeats an already selected task",
    ),
    "two AB sub-corpora": (lambda p, s: (p, _set(s, 3, ("subcorpus",), "other-ab")), "several sub-corpora"),
}


@pytest.mark.parametrize("name", sorted(MANIFEST_ERRORS))
def test_manifest_binding_errors_stop_before_scoring(library, name):
    mutate, message = MANIFEST_ERRORS[name]
    public, sealed = mutate(public_rows(), sealed_rows())
    with pytest.raises(scoring.ScoringBridgeError, match=message):
        scoring.bind_inputs(public, sealed)
    with pytest.raises(scoring.ScoringBridgeError, match=message):
        run(library, [], public=public, sealed=sealed)


@pytest.mark.parametrize(
    ("index", "path", "value", "message"),
    [
        (0, ("identity", "task_name"), "syn-ab-unknown", "absent from the native gold"),
        (0, ("identity", "example_id"), "ex-3", "example_id differs from the native aliases"),
        (0, ("identity", "domain"), "dom-two", "domain differs from its cluster_key"),
        (0, ("identity", "cluster_key"), "dom-two", "domain differs from its cluster_key"),
        (5, ("identity", "repo"), "repo-one", "repo differs from the native repo"),
    ],
)
def test_sealed_identity_must_join_exactly_one_native_task(library, index, path, value, message):
    sealed = _set(sealed_rows(), index, path, value)
    if path[-1] == "task_name":
        sealed[index]["task_id"] = value
    with pytest.raises(scoring.ScoringBridgeError, match=message):
        run(library, [], sealed=sealed)


def _flag(records: list[dict], index: int, gate: str, value: Any) -> list[dict]:
    records[index]["flags"][gate] = value
    records[index]["prediction"]["flags"][gate] = value
    return records


RECORD_ERRORS = {
    "unknown case": (lambda r: r + [dict(copy.deepcopy(r[0]), case_id="C0099")], "outside the manifests"),
    "unregistered role": (lambda r: r + [dict(copy.deepcopy(r[0]), role="opus")], "unregistered role"),
    "route mismatch": (lambda r: [dict(r[0], route=ROLES["haiku"])] + r[1:], "route differs"),
    "identical duplicate": (lambda r: r + [copy.deepcopy(r[0])], "never deduplicated"),
    "conflicting duplicate": (lambda r: r + [cell("C0001", "sonnet", False)], "never deduplicated"),
    "integer flag": (lambda r: _flag(r, 0, OLD, 1), "true, false or null"),
    "string flag": (lambda r: _flag(r, 0, OLD, "false"), "true, false or null"),
    "missing gate": (lambda r: [dict(r[0], flags={OLD: True})] + r[1:], "exactly the gates"),
    "bool without root ok": (lambda r: [cell("C0001", "sonnet", True, status="missing")] + r[1:], "need status ok"),
    "bool without prediction ok": (
        lambda r: [cell("C0001", "sonnet", True, prediction_status="missing")] + r[1:], "need status ok",
    ),
    "mixed gates": (lambda r: [cell("C0001", "sonnet", True, None)] + r[1:], "all boolean or all null"),
    "quota pending": (lambda r: [cell("C0001", "sonnet", True, terminal=False)] + r[1:], "not terminal"),
    "prediction drift": (
        lambda r: [dict(r[0], prediction={"status": "ok", "flags": {OLD: False, NEW: True}})] + r[1:],
        "prediction flags differ",
    ),
    "null flags marked ok": (lambda r: [cell("C0001", "sonnet", None, status="ok")] + r[1:], "status ok with null"),
}


@pytest.mark.parametrize("name", sorted(RECORD_ERRORS))
def test_record_errors_are_never_coerced_or_deduplicated(library, name):
    mutate, message = RECORD_ERRORS[name]
    with pytest.raises(scoring.ScoringBridgeError, match=message):
        run(library, mutate(sonnet_cells()))


# --------------------------------------------------------------------------- dependencies


def test_scorer_pin_and_location_are_enforced(library):
    with pytest.raises(scoring.ScoringBridgeError, match="differ from the pinned SHA-256"):
        scoring.load_scorer({"path": str(library.scorer), "sha256": "0" * 64}, pin(library.registry))

    elsewhere = write(library.root / "elsewhere" / "cross-test-registry.json", library.registry.read_text())
    with pytest.raises(scoring.ScoringBridgeError, match="beside it"):
        scoring.load_scorer(pin(library.scorer), pin(elsewhere))

    relocated = library.root / "relocated" / "score_task_qa_gate.py"
    relocated.parent.mkdir()
    relocated.write_bytes(library.scorer.read_bytes())
    with pytest.raises(scoring.ScoringBridgeError, match="beside it"):
        scoring.load_scorer(pin(relocated), pin(library.registry))

    with library.scorer.open("a", encoding="utf-8") as stream:
        stream.write("\n# drift\n")
    with pytest.raises(scoring.ScoringBridgeError, match="drifted after load"):
        run(library, sonnet_cells())


def test_native_gold_inputs_must_match_prospective_pins(library):
    def load(adapters=None, pai_files=None):
        return scoring.load_native_gold(
            library.frozen, eval_lab_root=library.eval_lab, adapters=adapters or library.adapters,
            bundle_files={PAI: library.bundle_files[PAI] if pai_files is None else pai_files},
            paichecker_bundle=library.pai_bundle,
        )

    with pytest.raises(scoring.ScoringBridgeError, match="adapter file .*differs from its prospective pin"):
        load(adapters={**library.adapters, PAI: "0" * 64})
    unpinned = {k: v for k, v in library.bundle_files[PAI].items() if k != "gym-labels.json"}
    with pytest.raises(scoring.ScoringBridgeError, match="unpinned bundle files: gym-labels.json"):
        load(pai_files=unpinned)
    without_fixes = {k: v for k, v in library.bundle_files[AB].items() if k != "fixes.csv"}
    with pytest.raises(scoring.ScoringBridgeError, match="must pin fixes.csv"):
        scoring.load_native_gold(
            library.frozen, eval_lab_root=library.eval_lab, adapters=library.adapters,
            bundle_files={AB: without_fixes}, automationbench_bundle=library.ab_bundle,
        )

    write(library.pai_bundle / "gym-labels.json", json.dumps({}))
    with pytest.raises(scoring.ScoringBridgeError, match="gym-labels.json differs from its prospective pin"):
        load()

    (library.eval_lab / PAI_ADAPTER).unlink()
    with pytest.raises(scoring.ScoringBridgeError, match="origin/main fallback"):
        load()


@pytest.mark.parametrize("location", ["root", "prediction"])
@pytest.mark.parametrize(
    "field", ["record_binding_sha256", "source_record_sha256", "source_sha256", "prompt_sha256"],
)
def test_predictions_must_bind_every_frozen_source_artifact(library, location, field):
    records = sonnet_cells()
    target = records[0] if location == "root" else records[0]["prediction"]
    target[field] = source_hash("foreign")
    with pytest.raises(scoring.ScoringBridgeError, match=field + " differs from frozen case"):
        run(library, records)


def test_null_native_source_hash_does_not_allow_an_unbound_missing_record(library):
    public, sealed = public_rows(), sealed_rows()
    public[0]["source_record_sha256"] = sealed[0]["source_record_sha256"] = None
    public[0]["record_binding_sha256"] = sealed[0]["record_binding_sha256"] = None
    with pytest.raises(scoring.ScoringBridgeError, match="record_binding_sha256 must"):
        run(library, [], public=public, sealed=sealed)
