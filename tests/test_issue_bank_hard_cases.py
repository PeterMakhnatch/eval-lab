from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evallab.failure_library.bank.hard_cases as hard_cases
from evallab.failure_library.bank import BankContext, IssueItem, Provenance, new_item
from evallab.failure_library.bank.hard_cases import (
    AnnotationRef,
    HardCase,
    Materialization,
    export_sample,
    load_cases,
    materialize_cases,
    materialize_to_directory,
    select_adjudication_sample,
    write_cases,
)
from evallab.production.run_atlas import AtlasRun


def _item(subject: str, **overrides: Any) -> IssueItem:
    fields: dict[str, Any] = {
        "source": "fake",
        "subject_id": subject,
        "task_id": f"task/{subject}",
        "kind": "cheat",
        "polarity": "positive",
        "label_type": "human",
        "label": "source claim",
        "granularity": "run",
        "subject_type": "run",
        "declared_split": "dev",
        "split": "DEV",
        "actor_transcript": True,
        "provenance": Provenance(store="fixture", ref=subject),
    }
    fields.update(overrides)
    return new_item(**fields)


def _case(subject: str, **overrides: Any) -> HardCase:
    fields: dict[str, Any] = {
        "system": "trace-watcher",
        "subject_id": subject,
        "task_id": f"task/{subject}",
        "case_kind": "miss",
        "kind": "cheat",
        "polarity": "positive",
        "label_type": "human",
        "item_ids": [_item(subject).item_id],
        "explanation": "An unadjudicated source claim.",
        "source_refs": [f"fixture:{subject}"],
    }
    fields.update(overrides)
    return HardCase(**fields)


class _Registry:
    def __init__(self, *, authority: bool = True) -> None:
        self.authority = authority

    def has_assignment_authority(self) -> bool:
        return self.authority

    def has_split_assignments(self, key_id: str) -> bool:
        return key_id == "key-v1"

    def assigned_split(self, key_id: str, *, item_id=None, task_id=None) -> str:
        return "test" if item_id == "sealed-row" else "dev"

    def sealed_identity(self, *, task_id=None, task_digest=None) -> str | None:
        return "TEST task" if task_id == "sealed-task" else None

    def assigned_test_anywhere(self, *, item_id=None, task_id=None) -> bool:
        return item_id == "sealed-run"


def _ctx(tmp_path: Path, *, authority: bool = True) -> BankContext:
    ctx = BankContext(
        tmp_path,
        state_root=tmp_path / "state",
        cloud_db=tmp_path / "cloud.sqlite",
        prod_live=tmp_path / "live",
        cross_test_registry=tmp_path / "bridge/registry.json",
    )
    ctx.__dict__["registry"] = _Registry(authority=authority)
    return ctx


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _matrix(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    root = tmp_path / "matrix"
    _write(root / "misses.jsonl", [row for row in rows if row["cell"] != "false_alarm"])
    _write(root / "false_alarms.jsonl", [row for row in rows if row["cell"] == "false_alarm"])
    return root


def _cell(item: IssueItem, **overrides: Any) -> dict[str, Any]:
    row = {
        "item_id": item.item_id,
        "subject_id": item.subject_id,
        "kind": item.kind,
        "cell": "miss" if item.polarity == "positive" else "false_alarm",
        "detector": "facets@v1[fixture/reader]",
        "circular": False,
        "evidence_steps": [10],
        "summary": "A detector observation, not new truth.",
    }
    row.update(overrides)
    return row


def _annotation(subject: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": subject,
        "case": "miss",
        "corpus": "fixture",
        "label_agrees": True,
        "explanation": "Provisional first-pass analysis.",
        "evidence_steps": [42],
        "reader": "fixture/reader",
        "detectors": {"facets": "clear"},
    }
    row.update(overrides)
    return row


def test_contract_identity_word_limit_and_no_payload_channel() -> None:
    case = _case("one", evidence_steps=[42, 10, 42])
    assert case.evidence_steps == [10, 42]
    assert _case("one", explanation="Different evidence prose.").case_id == case.case_id
    assert _case("one", case_kind="false_alarm").case_id != case.case_id
    for changes in (
        {"case_id": "hc-" + "0" * 24},
        {"split": "TEST"},
        {"explanation": "word " * 121},
        {"input_ref": "inputs/one.txt"},
        {"evidence_steps": [True]},
        {"exploit_payload": "not a supported field"},
        {"system": "seeker-data-quality", "case_kind": "miss"},
    ):
        with pytest.raises(ValidationError):
            HardCase.model_validate({**case.model_dump(), **changes})
    assert _case("boundary", explanation="word " * 120).adjudication == "pending"


def test_case_io_keeps_identity_and_rejects_conflicting_copies(tmp_path: Path) -> None:
    one, two = _case("one"), _case("two")
    path = tmp_path / "cases.jsonl"
    write_cases(path, [two, one, one])
    assert load_cases([path]) == sorted([one, two], key=lambda case: case.case_id)
    conflict = _case("one", explanation="A conflicting interpretation.")
    with pytest.raises(ValueError, match="conflicting case"):
        write_cases(tmp_path / "conflict.jsonl", [one, conflict])
    other = tmp_path / "other.jsonl"
    write_cases(other, [conflict])
    with pytest.raises(ValueError, match="conflicting case"):
        load_cases([path, other])


def test_goodfire_aliases_merge_without_promoting_first_pass_labels(tmp_path: Path) -> None:
    subject = "goodfire-reward-hack-data/stream/native-id"
    cloud = _item(subject, source="cloud-traces", label_type="consensus", task_id="canonical-task")
    library = _item(
        "goodfire-reward-hack-data:stream:native-id",
        source="library-runs",
        label_type="judge",
        task_id="library-task",
        harness_selector=subject,
    )
    matrix = _matrix(tmp_path, [_cell(cloud), _cell(library, evidence_steps=[42])])
    annotations = _write(
        tmp_path / "first-pass.jsonl",
        [
            _annotation(subject, label_agrees="unsure", evidence_steps=[42, 99]),
        ],
    )
    result = materialize_cases(
        [library, cloud], _ctx(tmp_path), matrix_dir=matrix, annotation_paths=[annotations]
    )
    assert {case.case_kind for case in result.cases} == {"miss", "label_disagreement"}
    for case in result.cases:
        assert case.subject_id == subject and case.task_id == "canonical-task"
        assert set(case.item_ids) == {cloud.item_id, library.item_id}
        assert case.adjudication == "pending" and case.adjudicator_model is None
        assert case.input_ref is None
    miss = next(case for case in result.cases if case.case_kind == "miss")
    assert miss.evidence_steps == [10, 42]
    disagreement = next(case for case in result.cases if case.case_kind == "label_disagreement")
    assert disagreement.evidence_steps == [42, 99]
    assert disagreement.explanation == "Provisional first-pass analysis."
    assert (
        disagreement.source_digests[str(annotations)]
        == hashlib.sha256(annotations.read_bytes()).hexdigest()
    )
    assert f"fixture#{library.subject_id}" in disagreement.source_refs
    sample = select_adjudication_sample(result)
    assert [row["case_id"] for row in sample] == [disagreement.case_id]
    assert "first_pass_label_false_or_unsure" in sample[0]["reasons"]


def test_generic_cheatbench_selector_is_not_a_subject_join(tmp_path: Path) -> None:
    one = _item("cheatbench-actions-v1:one", harness_selector="cheatbench")
    two = _item("cheatbench-actions-v1:two", harness_selector="cheatbench")
    matrix = _matrix(tmp_path, [_cell(one), _cell(two)])
    annotations = _write(tmp_path / "first-pass.jsonl", [_annotation(one.subject_id)])
    result = materialize_cases(
        [one, two], _ctx(tmp_path), matrix_dir=matrix, annotation_paths=[annotations]
    )
    assert {case.subject_id for case in result.cases} == {one.subject_id, two.subject_id}
    assert {case.case_id for case in result.cases if case.subject_id == one.subject_id} == {
        select_adjudication_sample(result)[0]["case_id"]
    }
    assert all(len(case.item_ids) == 1 for case in result.cases)


def test_missing_actor_infra_circular_and_not_run_boundaries(tmp_path: Path) -> None:
    absent = _item("absent", actor_transcript=False)
    absent_negative = _item("absent-negative", actor_transcript=False, polarity="negative")
    infra = _item("infra", kind="infra")
    circular = _item(
        "weak",
        kind="loop",
        label_type="detector",
        provenance=Provenance(store="run-health:v4", ref="weak"),
    )
    not_run = _item("not-run")
    task = _item("task-row", kind="grader_wrong_pass", subject_type="task", granularity="task")
    items = [absent, absent_negative, infra, circular, not_run, task]
    matrix = _matrix(
        tmp_path,
        [
            _cell(absent),
            _cell(absent_negative),
            _cell(infra),
            _cell(circular, detector="run_health.loop@v4", circular=False),
            _cell(not_run, cell="not_run"),
            _cell(task),
        ],
    )
    result = materialize_cases(items, _ctx(tmp_path), matrix_dir=matrix, annotation_paths=[])
    cases = {case.subject_id: case for case in result.cases}
    assert set(cases) == {"absent", "absent-negative", "infra", "task-row"}
    assert {cases[name].case_kind for name in ("absent", "absent-negative")} == {
        "measurement_missing"
    }
    assert all(
        cases[name].system == "seeker-data-quality"
        for name in ("absent", "absent-negative", "infra")
    )
    assert cases["infra"].case_kind == "infra"
    assert cases["task-row"].system == "task-qa"
    assert result.counts["excluded_circular_detector_label"] == 1
    assert result.counts["excluded_not_a_judged_hard_case"] == 1
    assert select_adjudication_sample(result) == []


def test_first_pass_cannot_reintroduce_a_circular_detector_label(tmp_path: Path) -> None:
    item = _item(
        "self-scored",
        label_type="detector",
        provenance=Provenance(store="facets:v1", ref="self-scored"),
    )
    matrix = _matrix(tmp_path, [])
    first_pass = _write(tmp_path / "first-pass.jsonl", [_annotation(item.subject_id)])
    result = materialize_cases(
        [item],
        _ctx(tmp_path),
        matrix_dir=matrix,
        annotation_paths=[first_pass],
    )
    assert result.cases == [] and result.annotations == []
    assert result.counts["excluded_circular_detector_label"] == 1


@pytest.mark.parametrize("authority", [False, True])
def test_strict_split_is_rechecked_from_current_authority(tmp_path: Path, authority: bool) -> None:
    open_item = _item("open")
    sealed_task = _item("old-dev", task_id="sealed-task")
    sealed_run = _item("sealed-run")
    declared_test = _item("already-test", split="TEST", declared_split="test")
    keyed = _item(
        "key-v1:sealed-row",
        source="answer-keys",
        provenance=Provenance(store="answer-keys:key-v1", ref="sealed-row"),
    )
    items = [open_item, sealed_task, sealed_run, declared_test, keyed]
    matrix = _matrix(tmp_path, [_cell(item) for item in items])
    annotations = _write(tmp_path / "first-pass.jsonl", [_annotation(sealed_task.subject_id)])
    result = materialize_cases(
        items,
        _ctx(tmp_path, authority=authority),
        matrix_dir=matrix,
        annotation_paths=[annotations],
    )
    assert {case.subject_id for case in result.cases} == ({"open"} if authority else set())
    assert result.counts["excluded_annotation_subject_not_strict_dev_or_label_mismatch"] == 1


def test_source_digest_conflicts_fail_closed(tmp_path: Path) -> None:
    item = _item("one")
    matrix = _matrix(
        tmp_path,
        [
            _cell(item, source_refs=["source:one"], source_digests={"source:one": "a" * 64}),
            _cell(item, source_refs=["source:one"], source_digests={"source:one": "b" * 64}),
        ],
    )
    with pytest.raises(ValueError, match="conflicting source digest"):
        materialize_cases([item], _ctx(tmp_path), matrix_dir=matrix, annotation_paths=[])


def _sample_result() -> Materialization:
    cases = []
    refs = []
    for index in range(52):
        original = "false_alarm" if index < 7 else "miss"
        disagreement = index in {0, 7, 8, 9}
        case = _case(
            f"subject-{index}",
            task_id=f"task-{index if index < 30 else index - 30}",
            case_kind="label_disagreement" if disagreement else original,
            polarity="negative" if original == "false_alarm" else "positive",
        )
        cases.append(case)
        ref = AnnotationRef(
            case.case_id,
            case.subject_id,
            case.task_id,
            "alpha" if index % 2 else "beta",
            original,
            "unsure" if disagreement else True,
            f"first-pass:{index}",
        )
        refs.extend([ref, ref])  # duplicate publisher rows do not add subjects
    return Materialization(cases, refs, {}, Counter(), [])


def test_sample_priorities_stratification_and_order_independence() -> None:
    result = _sample_result()
    sample = select_adjudication_sample(result, seed="fixed-seed")
    reordered = Materialization(
        list(reversed(result.cases)), list(reversed(result.annotations)), {}, Counter(), []
    )
    assert sample == select_adjudication_sample(reordered, seed="fixed-seed")
    selected = {row["case_id"] for row in sample}
    assert len(selected) == len(sample) == 24
    assert {
        case.case_id for case in result.cases if case.case_kind == "label_disagreement"
    } <= selected
    assert sum(row["original_case"] == "false_alarm" for row in sample) == 7
    by_id = {case.case_id: case for case in result.cases}
    assert len({by_id[row["case_id"]].task_id for row in sample}) == 24
    misses = [row for row in sample if "hash_stratified_miss" in row["reasons"]]
    strata = Counter(
        next(a.corpus for a in result.annotations if a.case_id == row["case_id"]) for row in misses
    )
    assert abs(strata["alpha"] - strata["beta"]) <= 2
    assert {
        row["case_id"] for row in select_adjudication_sample(result, seed="other-seed")
    } != selected


def test_sample_caps_disagreements_and_false_alarms_before_filling_misses() -> None:
    cases = []
    refs = []
    for index in range(48):
        disagreement = index < 32
        original = "false_alarm" if 32 <= index < 39 else "miss"
        case = _case(
            f"cap-{index}",
            case_kind="label_disagreement" if disagreement else original,
            polarity="negative" if original == "false_alarm" else "positive",
        )
        cases.append(case)
        refs.append(
            AnnotationRef(
                case.case_id,
                case.subject_id,
                case.task_id,
                "fixture",
                original,
                not disagreement,
                f"first-pass:{index}",
            )
        )
    result = Materialization(cases, refs, {}, Counter(), [])
    sample = select_adjudication_sample(result)
    by_id = {case.case_id: case for case in cases}
    kinds = Counter(by_id[row["case_id"]].case_kind for row in sample)
    assert kinds == {"label_disagreement": 16, "false_alarm": 7, "miss": 1}


def _run(subject: str, *, actor: bool = True) -> AtlasRun:
    steps: list[dict[str, Any]] = [
        {"step_id": 10, "source": "user", "message": "Inspect the local implementation."}
    ]
    if actor:
        steps.append(
            {
                "step_id": 42,
                "source": "agent",
                "message": "Inspect local implementation in Goodfire.",
                "tool_calls": [],
                "observation": {"results": []},
            }
        )
    return AtlasRun(
        subject,
        "goodfire-reward-hack-data",
        "DEV",
        "publisher-outcome",
        {
            "schema_version": "ATIF-v1.6",
            "steps": steps,
            "reward": 1,
            "label": "private-publisher-label",
            "outcome": "publisher-outcome",
        },
    )


def _install_original_runs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing: bool = False,
    actor: bool = True,
) -> None:
    def load(
        subjects: list[str], ctx: BankContext, root: Path | None
    ) -> tuple[dict[str, AtlasRun], dict[str, str]]:
        return (
            {subject: _run(subject, actor=actor) for subject in subjects} if not missing else {}
        ), {subject: "original run unavailable" for subject in subjects if missing}

    monkeypatch.setattr(hard_cases, "_load_original_runs", load)


def test_sample_export_uses_blinded_context_and_separate_original_id_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(
        "sampled", evidence_steps=[42], explanation="Do not put this source label into an input."
    )
    unselected = _case("unselected")
    _install_original_runs(monkeypatch)
    sample = [{"case_id": case.case_id}]
    cases, private = export_sample([case, unselected], sample, _ctx(tmp_path), tmp_path / "out")
    exported = next(row for row in cases if row.case_id == case.case_id)
    raw = (tmp_path / "out" / exported.input_ref).read_bytes()
    text = raw.decode()
    assert exported.input_sha256 == hashlib.sha256(raw).hexdigest()
    assert "Inspect local implementation" in text
    assert all(
        term not in text
        for term in (
            "Goodfire",
            "private-publisher-label",
            "publisher-outcome",
            case.explanation,
            case.subject_id,
        )
    )
    assert "[step 42]" not in text
    assert 42 in private[case.case_id]["original_step_ids"].values()
    assert private[case.case_id]["evidence_steps_original"] == [42]
    assert next(row for row in cases if row.case_id == unselected.case_id).input_ref is None
    assert sample[0]["runnable"] is True


@pytest.mark.parametrize("problem", ["missing_run", "no_actor", "unmapped_evidence"])
def test_unavailable_context_stays_not_runnable_without_resampling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    case = _case("one", evidence_steps=[99] if problem == "unmapped_evidence" else [42])
    _install_original_runs(
        monkeypatch, missing=problem == "missing_run", actor=problem != "no_actor"
    )
    sample = [{"case_id": case.case_id}]
    cases, private = export_sample([case], sample, _ctx(tmp_path), tmp_path / "out")
    assert cases == [case]
    assert sample[0]["case_id"] == case.case_id and sample[0]["runnable"] is False
    assert sample[0]["input_ref"] is None and sample[0]["blocked_reason"]
    assert not (tmp_path / "out" / "inputs").exists()
    if problem == "unmapped_evidence":
        assert private[case.case_id]["unmapped_evidence_steps"] == [99]


def test_private_directory_outputs_and_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    item = _item("one")
    _write(ctx.state_root / "bank/fake.jsonl", [item.model_dump(mode="json")])
    matrix = _matrix(tmp_path, [_cell(item, evidence_steps=[42])])
    annotations = _write(
        tmp_path / "first-pass.jsonl", [_annotation(item.subject_id, label_agrees=False)]
    )
    _install_original_runs(monkeypatch)
    out = tmp_path / "materialized"
    summary = materialize_to_directory(ctx, out, matrix_dir=matrix, annotation_paths=[annotations])
    cases = load_cases([out / "trace-watcher.jsonl", out / "seeker-data-quality.jsonl"])
    assert {case.case_kind for case in cases} == {"miss", "label_disagreement"}
    assert summary["adjudication"] == {"pending": 2}
    assert summary["sample_cases"] == summary["runnable_sample_cases"] == 1
    assert summary["sample_shortfall"] == 23
    plan_path = out / "adjudication-plan.json"
    before = plan_path.read_bytes()
    plan = json.loads(before)
    assert plan["model_invoked"] is False and plan["adjudicator_family"] == "Opus"
    assert (
        plan["case_files"]["trace-watcher.jsonl"]
        == hashlib.sha256((out / "trace-watcher.jsonl").read_bytes()).hexdigest()
    )
    assert json.loads((out / "input-map.json").read_text())[plan["sample"][0]["case_id"]][
        "evidence_steps_original"
    ] == [42]
    with pytest.raises(ValueError, match="fresh or empty directory"):
        materialize_to_directory(
            ctx, out, matrix_dir=matrix, annotation_paths=[annotations], seed="different"
        )
    assert plan_path.read_bytes() == before


def test_folded_tool_feedback_preserves_exact_owner_and_rejects_off_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(tmp_path)
    ctx.cloud_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ctx.cloud_db) as connection:
        connection.execute(
            "CREATE TABLE trace_steps(trace_id TEXT, step_index INTEGER, role TEXT, flags_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO trace_steps VALUES (?, ?, ?, ?)",
            [
                ("sampled", 10, "user", "{}"),
                ("sampled", 42, "assistant", "{}"),
                ("sampled", 50, "assistant", "{}"),
                ("sampled", 51, "tool", '{"tool_call_id":"first"}'),
                ("sampled", 99, "assistant", '{"on_final_parent_path":false}'),
                ("sampled", 100, "tool", '{"on_final_parent_path":false}'),
            ],
        )
    run = _run("sampled")
    run.doc["steps"][1]["tool_calls"] = [{"tool_call_id": "first", "function_name": "inspect"}]
    run.doc["steps"][1]["observation"] = {"results": [{"content": "Retained first-call feedback."}]}
    run.doc["steps"].append(
        {
            "step_id": 50,
            "source": "agent",
            "message": "A later action.",
            "tool_calls": [{"tool_call_id": "second", "function_name": "inspect"}],
        }
    )
    monkeypatch.setattr(hard_cases, "_load_original_runs", lambda *args: ({"sampled": run}, {}))
    case = _case("sampled", evidence_steps=[51])
    sample = [{"case_id": case.case_id}]
    cases, private = export_sample([case], sample, ctx, tmp_path / "retained")
    assert cases[0].input_ref is not None
    assert private[case.case_id]["feedback_step_aliases"] == {"51": 1}
    assert private[case.case_id]["evidence_steps_model"] == [1]  # Not the later action.
    assert (
        "Retained first-call feedback." in (tmp_path / "retained" / cases[0].input_ref).read_text()
    )
    off_path = _case("sampled", evidence_steps=[100])
    sample = [{"case_id": off_path.case_id}]
    cases, private = export_sample([off_path], sample, ctx, tmp_path / "off-path")
    assert cases[0].input_ref is None
    assert private[off_path.case_id]["unmapped_evidence_steps"] == [100]
