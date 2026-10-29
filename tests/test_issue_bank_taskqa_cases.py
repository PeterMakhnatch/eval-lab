from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem, Provenance, new_item
from evallab.failure_library.bank import taskqa_cases as tc
from evallab.failure_library.bank import taskqa_findings as tq

ROUTE = tq.ROUTES[0]
DIGEST = "sha256:" + "ab" * 32
REPAIRED = "sha256:" + "cd" * 32
REFERENCE = "sha256:" + "ef" * 32


def _write(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _item(key: str, *, source: str = "answer-keys", **overrides: Any) -> IssueItem:
    fields: dict[str, Any] = {
        "source": source,
        "kind": "grader_wrong_pass",
        "polarity": "positive",
        "label_type": "human",
        "label": "retained source claim",
        "granularity": "task",
        "subject_type": "task",
        "subject_id": f"{source}:task-a:{key}",
        "task_id": "ns/task-a",
        "locator": key if source == "library-findings" else "",
        "declared_split": "dev",
        "split": "DEV",
        "provenance": Provenance(
            store=f"{source}:labels", ref=key,
            note="oq6-keyed" if source == "answer-keys" else None,
        ),
    }
    fields.update(overrides)
    return new_item(**fields)


def _prepared(tmp_path: Path, keys: tuple[str, ...] = ("k1",)) -> tc.PreparedTask:
    source = tmp_path / "source.txt"
    source.write_text("Bounded instruction and native test source, without answer labels.")
    keys_path = tmp_path / "private-keys.json"
    key_rows = [{
        "key_id": key, "package_digest": DIGEST,
        "summary": "SECRET KEY DESCRIPTION MUST NOT BECOME TARGET INPUT",
        "root_causes": [{"source_class": "weak_check"}],
    } for key in keys]
    _write(keys_path, key_rows)
    case = tq.Case("D0001", "dev", "task-a", DIGEST, frozenset(keys), len(keys), "prepared")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return tc.PreparedTask(
        case,
        {"keys_path": str(keys_path), "source_path": str(source), "source_sha256": digest},
        {row["key_id"]: row for row in key_rows}, str(source), digest,
        {str(source): digest, str(keys_path): hashlib.sha256(keys_path.read_bytes()).hexdigest()},
    )


def _cell(
    tmp_path: Path,
    task: tc.PreparedTask,
    *,
    arm: str = "A",
    payloads: list[dict[str, Any]] | None = None,
    new: dict[str, str] | None = None,
    old: dict[str, str] | None = None,
    eligible_new: list[str] | None = None,
    eligible_old: list[str] | None = None,
    complete: bool = True,
    judgments: list[dict[str, Any]] | None = None,
) -> tuple[tq.CellView, dict[str, Any]]:
    payloads = payloads or []
    new, old = new or {}, old or {}
    eligible_new = eligible_new if eligible_new is not None else list(new)
    eligible_old = eligible_old if eligible_old is not None else list(old)
    paired = {
        "case_id": task.case.case_id, "key_count": task.case.key_count,
        "complete": complete, "audit_status": "answered",
        "parse_status": "complete", "judge_status": "complete" if complete else "missing",
        "memberships": {
            tc.FIXED_GATE: eligible_new, tc.OLD_GATE: eligible_old,
            "union": sorted(set(eligible_new + eligible_old)),
        },
        "gates": {
            gate: {"matching": matching, "matched_keys": len(matching)}
            for gate, matching in ((tc.FIXED_GATE, new), (tc.OLD_GATE, old))
        },
    }
    record = {
        "paired": paired,
        "audit": {"served_identity": "actual/provider-model"},
        "scorer": {"judgments": judgments or []},
    }
    path = tmp_path / arm / "cell.json"
    _write(path, record)
    return tq.CellView(ROUTE, arm, task.case.case_id, path, paired, payloads), record


def _payload(confidence: float = 0.6, *, verified: bool = True) -> dict[str, Any]:
    return {"direction": "wrong_pass", "confidence": confidence, "citation_verified": verified}


def _mine(groups, *stored):
    cells = {(cell.route, cell.arm, cell.case_id): cell for cell, _ in stored}
    records = {cell.path: row for cell, row in stored}
    return tc.mine_keyed_cases(groups, cells, records, routes=[ROUTE])


def test_exact_matching_not_same_direction_and_counts_by_arm(tmp_path: Path) -> None:
    task = _prepared(tmp_path, ("k1", "k2"))
    duplicate = _item("finding-1", source="library-findings")
    groups = [
        tc.KeyGroup(task, "k1", (_item("k1"), duplicate)),
        tc.KeyGroup(task, "k2", (_item("k2"),)),
    ]
    a = _cell(tmp_path, task, payloads=[_payload()], new={"C1": "k2"})
    b = _cell(tmp_path, task, arm="B", payloads=[_payload()], new={"C1": "k1"})
    hard, counts = _mine(groups, a, b)
    by_key = {case.subject_id.rsplit("=", 1)[1]: case for case in hard}
    assert set(by_key) == {"k1", "k2"}
    assert set(by_key["k1"].item_ids) == {_item("k1").item_id, duplicate.item_id}
    for arm in ("A", "B"):
        assert counts[f"{ROUTE}#{arm}"]["complete_keys"] == 2
        assert counts[f"{ROUTE}#{arm}"]["fixed_gate_missed_keys"] == 1
        assert counts[f"{ROUTE}#{arm}"]["fixed_gate_matched_keys"] == 1
    assert all(case.adjudication == "pending" for case in hard)
    assert all(case.input_ref == task.input_ref for case in hard)
    assert all("SECRET KEY DESCRIPTION" not in case.explanation for case in hard)
    assert all(any("served=actual%2Fprovider-model" in ref for ref in case.source_refs) for case in hard)


def test_incomplete_or_unsent_audit_is_not_a_true_miss(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    group = tc.KeyGroup(task, "k1", (_item("k1"),))
    incomplete = _cell(tmp_path, task, complete=False)
    hard, counts = _mine([group], incomplete)
    assert hard == []
    assert counts[f"{ROUTE}#A"] == {"incomplete_keys": 1}
    assert counts[f"{ROUTE}#B"] == {"not_run_keys": 1}


def test_complete_empty_audit_is_a_source_keyed_miss(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    hard, counts = _mine([tc.KeyGroup(task, "k1", (_item("k1"),))], _cell(tmp_path, task))
    assert counts[f"{ROUTE}#A"]["fixed_gate_missed_keys"] == 1
    assert hard[0].case_kind == "miss"
    assert "weak_check" in hard[0].explanation
    assert len(hard[0].explanation.split()) <= 120


def test_mimo_primary_class_is_preserved_in_the_hard_reason(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    task = replace(task, keys={"k1": {
        "key_id": "k1", "package_digest": DIGEST,
        "primary_class": {"id": "TS01", "key": "weak_check"},
    }})
    hard, _ = _mine([tc.KeyGroup(task, "k1", (_item("k1"),))], _cell(tmp_path, task))
    assert "TS01" in hard[0].explanation and "weak_check" in hard[0].explanation


def test_confidence_gate_drop_is_retained_even_when_fixed_gate_hits(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    cell = _cell(tmp_path, task, payloads=[_payload(0.6)], new={"C1": "k1"})
    hard, counts = _mine([tc.KeyGroup(task, "k1", (_item("k1"),))], cell)
    assert counts[f"{ROUTE}#A"]["fixed_gate_matched_keys"] == 1
    assert counts[f"{ROUTE}#A"][f"{tc.OLD_GATE}_dropped_keyed_findings"] == 1
    assert "0.6" in hard[0].explanation
    assert tc.OLD_GATE in hard[0].explanation
    assert any(f"#{tc.OLD_GATE}:k1" in ref for ref in hard[0].source_refs)


def test_eligible_unmatched_finding_remains_a_keyed_miss(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    cell = _cell(tmp_path, task, payloads=[_payload(0.9)], eligible_new=["C1"], eligible_old=["C1"])
    hard, counts = _mine([tc.KeyGroup(task, "k1", (_item("k1"),))], cell)
    assert counts[f"{ROUTE}#A"]["fixed_gate_missed_keys"] == 1
    assert hard[0].case_kind == "miss"
    assert any(f"#{tc.FIXED_GATE}:k1" in ref for ref in hard[0].source_refs)


def test_unkeyed_or_refuted_scorer_candidate_is_not_a_clean_task_negative(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    group = tc.KeyGroup(task, "k1", (_item("k1"),))
    cell, record = _cell(
        tmp_path, task, payloads=[_payload(0.9), _payload(0.7)],
        eligible_new=["C1", "C2"], eligible_old=["C1"],
        judgments=[
            {"id": "C1", "verdict": "false_alarm", "matched_key_ids": [], "reason": "private text"},
            {"id": "C2", "verdict": "plausible_unkeyed", "matched_key_ids": [], "reason": "private text"},
        ],
    )
    hard = tc.uncertainty_cases([group], {(ROUTE, "A", task.case.case_id): cell}, {cell.path: record})
    assert {case.subject_id.rsplit(":", 1)[1] for case in hard} == {"C1", "C2"}
    assert all(case.case_kind == "label_disagreement" for case in hard)
    assert all(case.adjudication == "uncertain" and case.polarity == "positive" for case in hard)
    assert all(case.label_type == "judge" for case in hard)
    assert all("private text" not in case.explanation for case in hard)


class _Registry:
    def has_assignment_authority(self):
        return True

    def has_split_assignments(self, key_id):
        return False

    def assigned_test_anywhere(self, *, item_id=None, task_id=None):
        return task_id == "sealed-task"

    def sealed_identity(self, *, task_id=None, task_digest=None):
        return "sealed exact version" if task_digest == REPAIRED else None


def _ctx(tmp_path: Path) -> BankContext:
    ctx = BankContext(repo_root=tmp_path, state_root=tmp_path / "bank-state")
    ctx.__dict__["registry"] = _Registry()
    return ctx


def test_refuted_source_claim_is_uncertain_and_needs_exact_input_binding(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    unbound = _item(
        "refuted-unbound", polarity="negative",
        provenance=Provenance(store="answer-keys:labels", ref="refuted-claim"),
    )
    exact = _item("refuted-exact", source="library-findings", polarity="negative")
    finding = SimpleNamespace(task=SimpleNamespace(task_version_digest=DIGEST))
    hard, unavailable = tc.source_uncertainty_cases(
        [unbound, exact], {task.case.case_id: task}, {exact.locator: finding}, _ctx(tmp_path),
    )
    by_item = {case.item_ids[0]: case for case in hard}
    assert by_item[unbound.item_id].input_ref is None
    assert by_item[exact.item_id].input_ref == task.input_ref
    assert all(case.case_kind == "label_disagreement" for case in hard)
    assert all(case.adjudication == "uncertain" for case in hard)
    assert all(case.polarity == "negative" for case in hard)  # Source claim, not task truth.
    assert {row["source"] for row in unavailable} == {unbound.item_id}


def test_key_binding_excludes_unkeyed_negative_detector_and_sealed_items(tmp_path: Path) -> None:
    task = _prepared(tmp_path, ("k1", "k2", "k3", "k4"))
    valid = _item("k1")
    unkeyed = _item("lead", provenance=Provenance(store="answer-keys:labels", ref="row"))
    negative = _item("k2", polarity="negative")
    circular = _item("k3", label_type="detector")
    sealed = _item("k4", task_id="sealed-task")
    groups, selected, unavailable = tc.bind_keyed_items(
        [valid, unkeyed, negative, circular, sealed], {task.case.case_id: task}, {}, _ctx(tmp_path)
    )
    assert [group.key_id for group in groups] == ["k1"]
    assert [item.item_id for item in selected] == [valid.item_id]
    assert {row["source"] for row in unavailable} == {negative.item_id, circular.item_id}
    # A stale bank DEV label cannot override the exact task-package seal.
    sealed_task = replace(task, case=replace(task.case, package_digest=REPAIRED))
    groups, _, unavailable = tc.bind_keyed_items([valid], {task.case.case_id: sealed_task}, {}, _ctx(tmp_path))
    assert groups == []
    assert {row["source"] for row in unavailable} == {valid.item_id}


def test_library_key_binding_requires_exact_package_digest(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    item = _item("finding-1", source="library-findings")
    finding = SimpleNamespace(locator="k1", task=SimpleNamespace(task_id="task-a", task_version_digest=REPAIRED))
    groups, _, unavailable = tc.bind_keyed_items(
        [item], {task.case.case_id: task}, {"finding-1": finding}, _ctx(tmp_path)
    )
    assert groups == []
    assert {row["source"] for row in unavailable} == {item.item_id}


def test_known_scorer_key_cannot_be_rebound_to_another_task(tmp_path: Path) -> None:
    task = _prepared(tmp_path)
    other = replace(
        task,
        case=replace(task.case, case_id="D0002", task="task-b", key_ids=frozenset({"k2"})),
        keys={"k2": {"key_id": "k2", "package_digest": DIGEST}},
    )
    foreign_key_item = _item("k2", task_id="ns/task-a")
    with pytest.raises(ValueError):
        tc.bind_keyed_items(
            [foreign_key_item], {"D0001": task, "D0002": other}, {}, _ctx(tmp_path),
        )


def _frozen_plan(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    task = _prepared(tmp_path)
    keys_path = Path(task.row["keys_path"])
    row = {
        **task.row, "case_id": "D0001", "stage": "dev", "item_id": "ns/task-a",
        "key_count": 1, "keys_sha256": hashlib.sha256(keys_path.read_bytes()).hexdigest(),
        "preparation_status": "prepared", "pinned_native_package_digest": DIGEST,
    }
    plan = tmp_path / "plan.json"
    _write(plan, {"cases": [row]})
    source = Path(task.input_ref)
    freeze = tmp_path / "freeze.json"
    _write(freeze, {"pre_cross_data_files": [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (plan, source, keys_path)
    ]})
    return plan, freeze, source, keys_path


def test_prepared_input_is_separate_from_labels_and_withheld_on_drift(tmp_path: Path) -> None:
    plan, freeze, source, keys = _frozen_plan(tmp_path)
    tasks, unavailable = tc.load_prepared_tasks(plan, freeze)
    assert unavailable == []
    assert tasks["D0001"].input_ref == str(source)
    assert tasks["D0001"].input_ref != str(keys)
    source.write_text("changed input")
    tasks, unavailable = tc.load_prepared_tasks(plan, freeze)
    assert tasks["D0001"].input_ref is None
    assert tasks["D0001"].input_sha256 is None
    assert {row["source"] for row in unavailable} == {str(source)}


def test_frozen_plan_and_key_drift_fail_closed(tmp_path: Path) -> None:
    plan, freeze, _, keys = _frozen_plan(tmp_path)
    keys.write_text("[]")
    with pytest.raises(ValueError):
        tc.load_prepared_tasks(plan, freeze)
    plan.write_text("{}")
    with pytest.raises(ValueError):
        tc.load_prepared_tasks(plan, freeze)


def _repair_fixture(tmp_path: Path) -> tuple[IssueItem, Path]:
    variant_path = tmp_path / "library/task-variants/task-a/variant.json"
    variant_sha = _write(variant_path, {
        "task_name": "ns/task-a", "parent": {"digest": DIGEST}, "variant_digest": REPAIRED,
    })
    item = _item(
        "repair", source="grader-repairs", kind="grader_repair", granularity="repair_pair",
        subject_type="repair", locator="1328", task_id="task-a", label_type="executed",
        provenance=Provenance(store="git:pr/1328", ref=str(variant_path.relative_to(tmp_path)), digest=variant_sha),
    )
    sides = {}
    for phase, digest, reward in (("original", DIGEST, 1.0), ("repaired", REPAIRED, 0.0)):
        path = tmp_path / phase / "receipt.json"
        receipt_sha = _write(path, {
            "status": "measured", "executed": True, "error": None, "evidence_errors": [],
            "reward": reward, "task": {"digest": digest, "id": "ns/task-a", "tests_digest": digest},
            "input": {"digest": REFERENCE},
        })
        sides[f"oracle_{phase}"] = {
            "status": "measured", "task_digest": digest, "reward": reward,
            "tests_digest": digest,
            "input_digest": REFERENCE, "native_receipt": str(path), "native_receipt_sha256": receipt_sha,
        }
    summary = tmp_path / "ts14-summary.json"
    _write(summary, {"cases": [{"pr": 1328, "task_name": "ns/task-a", **sides}]})
    return item, summary


def test_repair_reference_rejection_needs_both_bound_native_receipts(tmp_path: Path) -> None:
    item, summary = _repair_fixture(tmp_path)
    ctx = _ctx(tmp_path)
    # This fixture's repair is DEV; the separate seal test above exercises rejection.
    ctx.__dict__["registry"] = SimpleNamespace(
        has_assignment_authority=lambda: True,
        has_split_assignments=lambda key: False,
        sealed_identity=lambda **kwargs: None,
        assigned_test_anywhere=lambda **kwargs: False,
    )
    hard, unavailable = tc.repair_regression_cases([item], tmp_path, summary, ctx)
    assert len(hard) == 1
    case = hard[0]
    assert case.kind == "grader_unfair_fail" and case.label_type == "executed"
    assert case.adjudication == "uncertain"
    assert case.input_ref is None and case.input_sha256 is None
    assert case.source_digests["task-package:ns/task-a#original"] == DIGEST.removeprefix("sha256:")
    assert case.source_digests["task-package:ns/task-a#repaired"] == REPAIRED.removeprefix("sha256:")
    assert {row["source"] for row in unavailable} == {case.subject_id}
    receipt = tmp_path / "repaired/receipt.json"
    receipt.write_text("{}")
    hard, unavailable = tc.repair_regression_cases([item], tmp_path, summary, ctx)
    assert hard == []
    assert {row["source"] for row in unavailable} == {f"{summary}#pr=1328"}


def test_repair_summary_alone_is_not_native_execution_proof(tmp_path: Path) -> None:
    item, summary = _repair_fixture(tmp_path)
    row = json.loads(summary.read_bytes())
    row["cases"][0]["oracle_original"]["task_digest"] = "sha256:" + "00" * 32
    _write(summary, row)
    hard, unavailable = tc.repair_regression_cases([item], tmp_path, summary, _ctx(tmp_path))
    assert hard == []
    assert {row["source"] for row in unavailable} == {f"{summary}#pr=1328"}


def test_cli_missing_source_cannot_leave_a_stale_successful_collection(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "task-qa.jsonl").write_text("stale prior collection\n")
    result = tc.main([
        "--repo-root", str(tmp_path), "--plan", str(tmp_path / "missing-plan.json"),
        "--freeze", str(tmp_path / "missing-freeze.json"), "--out", str(output),
    ])
    assert result == 1
    assert (output / "task-qa.jsonl").read_text() == ""
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["status"] == "unavailable"
    assert summary["cases"] == 0
    assert summary["model_calls"] == 0
    assert "missing-freeze.json" in summary["unavailable"][0]["reason"]
