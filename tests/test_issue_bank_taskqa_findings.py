from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.failure_library.bank import Provenance, new_item
from evallab.failure_library.bank import taskqa_findings as tq
from evallab.failure_library.bank.taskqa_findings import Case, CellView, TaskItem

SONNET = "anthropic/claude-sonnet-5-5"
DIGEST = "sha256:" + "ab" * 32


def _case(case_id: str = "D0001", task: str = "task-a", keys: tuple[str, ...] = ("k1",)) -> Case:
    return Case(
        case_id=case_id,
        stage="dev",
        task=task,
        package_digest=DIGEST,
        key_ids=frozenset(keys),
        key_count=len(keys),
        preparation_status="prepared",
    )


def _payload(direction: str, confidence: float) -> dict[str, Any]:
    return {"direction": direction, "confidence": confidence, "citation_verified": True}


def _cell(
    *,
    arm: str = "A",
    case_id: str = "D0001",
    payloads: list[dict[str, Any]] | None = None,
    new: list[str] | None = None,
    old: list[str] | None = None,
    matching_new: dict[str, str] | None = None,
    complete: bool = True,
    audit_status: str = "answered",
    key_count: int = 1,
) -> CellView:
    new = new or []
    old = old or []
    paired = {
        "case_id": case_id,
        "key_count": key_count,
        "complete": complete,
        "audit_status": audit_status,
        "missing_reason": None if complete else "union_judgment_not_complete",
        "memberships": {"old_0_8": old, "new_verified_0_5": new, "union": sorted(set(old + new))},
        "gates": {
            "old_0_8": {"matching": {}, "matched_keys": 0},
            "new_verified_0_5": {
                "matching": matching_new or {},
                "matched_keys": len(matching_new or {}),
            },
        },
    }
    return CellView(
        SONNET, arm, case_id, Path(f"/r/{arm}/{case_id}/cell.json"), paired, payloads or []
    )


def _item(
    item_id: str, kind: str = "grader_wrong_pass", *, task: str = "task-a", **extra: Any
) -> TaskItem:
    fields: dict[str, Any] = {
        "item_id": item_id,
        "source": "cloud-task-issues",
        "kind": kind,
        "polarity": "positive",
        "task_id": f"ns/{task}",
        "task": task,
    }
    fields.update(extra)
    return TaskItem(**fields)


def _verdicts(records: list[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    return {
        (r["item_ids"][0], r["arm"], r["detector_version"]): r["verdict"]
        for r in records
        if r["reader_model"] == SONNET
    }


def test_keyed_item_uses_one_to_one_matching_per_gate() -> None:
    cell = _cell(
        payloads=[_payload("wrong_pass", 0.6), _payload("wrong_pass", 0.9)],
        new=["C1", "C2"],
        old=["C2"],
        matching_new={"C1": "k1"},
    )
    keyed = _item("ib-keyed", key_id="k1")
    records = tq.task_findings([keyed], [_case()], {(SONNET, "A", "D0001"): cell}, routes=[SONNET])
    verdicts = _verdicts(records)
    assert verdicts["ib-keyed", "A", "oq6.new_verified_0_5"] == "alarm"
    # old gate admitted C2 (same direction) but the scorer matched no key to it
    assert verdicts["ib-keyed", "A", "oq6.old_0_8"] == "clear"
    alarm = next(r for r in records if r["verdict"] == "alarm")
    assert alarm["score"] == 0.6
    assert alarm["source_ref"].endswith("#new_verified_0_5:C1")


def test_unkeyed_items_match_on_direction() -> None:
    cell = _cell(payloads=[_payload("wrong_pass", 0.85)], new=["C1"], old=["C1"])
    items = [
        _item("ib-wp"),
        _item("ib-uf", "grader_unfair_fail"),
        _item("ib-td", "task_defect"),
        _item("ib-cheat", "cheat"),
    ]
    records = tq.task_findings(items, [_case()], {(SONNET, "A", "D0001"): cell}, routes=[SONNET])
    verdicts = _verdicts(records)
    assert verdicts["ib-wp", "A", "oq6.old_0_8"] == "alarm"
    assert verdicts["ib-uf", "A", "oq6.old_0_8"] == "clear"
    assert verdicts["ib-td", "A", "oq6.old_0_8"] == "alarm"
    assert verdicts["ib-cheat", "A", "oq6.old_0_8"] == "clear"
    kinds = {r["item_ids"][0]: r["kind"] for r in records}
    assert kinds["ib-cheat"] == "other"
    assert kinds["ib-td"] == "task_defect"


def test_missing_and_incomplete_cells() -> None:
    incomplete = _cell(arm="A", complete=False, payloads=[_payload("wrong_pass", 0.9)], new=["C1"])
    items = [
        _item("ib-run"),
        _item("ib-elsewhere", task="task-z"),
        _item("ib-version", digest="sha256:" + "cd" * 32),
    ]
    records = tq.task_findings(
        items, [_case()], {(SONNET, "A", "D0001"): incomplete}, routes=[SONNET]
    )
    verdicts = _verdicts(records)
    assert verdicts["ib-run", "A", "oq6.new_verified_0_5"] == "abstain"
    assert verdicts["ib-run", "B", "oq6.new_verified_0_5"] == "unknown"  # never sent
    assert {verdicts["ib-elsewhere", arm, "oq6.old_0_8"] for arm in "AB"} == {"unknown"}
    assert verdicts["ib-version", "A", "oq6.old_0_8"] == "unknown"


def test_unavailable_input_is_unknown() -> None:
    cell = _cell(complete=False, audit_status="unavailable_input")
    records = tq.task_findings(
        [_item("ib-x")], [_case()], {(SONNET, "A", "D0001"): cell}, routes=[SONNET]
    )
    assert _verdicts(records)["ib-x", "A", "oq6.old_0_8"] == "unknown"


def test_record_shape() -> None:
    cell = _cell(payloads=[_payload("unfair_fail", 0.7)], new=["C1"])
    records = tq.task_findings(
        [_item("ib-uf", "grader_unfair_fail")],
        [_case()],
        {(SONNET, "A", "D0001"): cell},
        routes=[SONNET],
    )
    assert len(records) == 4  # 2 arms x 2 gates
    for record in records:
        assert set(record) == {
            "task_id",
            "item_ids",
            "detector",
            "detector_version",
            "reader_model",
            "arm",
            "verdict",
            "kind",
            "score",
            "evidence",
            "source_ref",
        }
        assert record["detector"] == "taskqa_gate"
        assert record["task_id"] == "ns/task-a"
        assert len(record["evidence"].split()) <= tq.EVIDENCE_WORDS


def test_keyed_item_with_foreign_key_is_rejected() -> None:
    cell = _cell()
    with pytest.raises(ValueError, match="not a key"):
        tq.task_findings(
            [_item("ib-k", key_id="other")],
            [_case()],
            {(SONNET, "A", "D0001"): cell},
            routes=[SONNET],
        )


def _write_json(path: Path, value: Any) -> str:
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(tmp_path: Path) -> Path:
    keys = tmp_path / "D0001.json"
    keys_sha = _write_json(keys, [{"key_id": "k1", "package_digest": DIGEST, "summary": "secret"}])
    plan = tmp_path / "plan.json"
    _write_json(
        plan,
        {
            "cases": [
                {
                    "case_id": "D0001",
                    "stage": "dev",
                    "item_id": "terminal-bench/task-a",
                    "key_count": 1,
                    "keys_path": str(keys),
                    "keys_sha256": keys_sha,
                    "preparation_status": "prepared",
                },
                {
                    "case_id": "D0002",
                    "stage": "dev",
                    "item_id": "task-b@" + DIGEST,
                    "key_count": 0,
                    "preparation_status": "prepared",
                },
            ]
        },
    )
    return plan


def test_load_cases_and_cells(tmp_path: Path) -> None:
    cases = tq.load_cases(_plan(tmp_path))
    assert [(c.task, c.package_digest, c.key_ids) for c in cases] == [
        ("task-a", DIGEST, frozenset({"k1"})),
        ("task-b", DIGEST, frozenset()),
    ]
    root = tmp_path / "calls"
    directory = root / "anthropic--claude-sonnet-5-5" / "B" / "dev" / "D0001"
    directory.mkdir(parents=True)
    cell = {
        "route": SONNET,
        "arm": "B",
        "case_id": "D0001",
        "task_id": "task-a",
        "paired": {"key_count": 1, "complete": True},
        "prediction": {"parsed_payloads": [_payload("wrong_pass", 0.9)]},
    }
    _write_json(directory / "cell.json", cell)
    cells = tq.load_cells(cases, [root], routes=[SONNET])
    assert list(cells) == [(SONNET, "B", "D0001")]
    cell["task_id"] = "task-b"
    _write_json(directory / "cell.json", cell)
    with pytest.raises(ValueError, match="task differs"):
        tq.load_cells(cases, [root], routes=[SONNET])


def test_load_cases_rejects_key_drift(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    (tmp_path / "D0001.json").write_text("[]")
    with pytest.raises(ValueError, match="drifted"):
        tq.load_cases(plan)


def _issue(
    source: str,
    task_id: str,
    *,
    ref: str,
    note: str | None = None,
    locator: str = "",
    split: str = "DEV",
):
    return new_item(
        source=source,
        kind="grader_wrong_pass",
        polarity="positive",
        label_type="human",
        label="x",
        granularity="task",
        subject_type="task",
        subject_id=f"{source}:{task_id}:{ref}",
        task_id=task_id,
        locator=locator,
        declared_split=split,
        split=split,
        provenance=Provenance(store=f"{source}:store", ref=ref, note=note),
    )


def test_select_items_marks_keyed_items() -> None:
    cases = [_case(keys=("k1", "k2"))]
    keyed_ak = _issue("answer-keys", "terminal-bench/task-a", ref="k1", note="oq6-keyed")
    plain_ak = _issue("answer-keys", "terminal-bench/task-a", ref="row-9")
    heldout = _issue("answer-keys", "terminal-bench/task-a", ref="row-8", split="TEST")
    cti = _issue("cloud-task-issues", "elsewhere", ref="issue-1")
    lf_keyed = _issue("library-findings", "task-a", ref="fl-1", locator="fl-1")
    lf_other = _issue("library-findings", "task-zz", ref="fl-2", locator="fl-2")
    findings = {
        "fl-1": SimpleNamespace(
            locator="k2", task=SimpleNamespace(task_id="task-a", task_version_digest=DIGEST)
        ),
        "fl-2": SimpleNamespace(
            locator="loc", task=SimpleNamespace(task_id="task-zz", task_version_digest=DIGEST)
        ),
    }
    items = tq.select_items(
        [cti], [keyed_ak, plain_ak, heldout], [lf_keyed, lf_other], findings, cases
    )
    by_id = {item.item_id: item for item in items}
    assert set(by_id) == {keyed_ak.item_id, plain_ak.item_id, cti.item_id, lf_keyed.item_id}
    assert by_id[keyed_ak.item_id].key_id == "k1"
    assert by_id[plain_ak.item_id].key_id is None
    assert by_id[lf_keyed.item_id].key_id == "k2"
    assert by_id[lf_keyed.item_id].digest == DIGEST
    assert by_id[cti.item_id].task == "elsewhere"


def test_select_items_rejects_unknown_oq6_ref() -> None:
    bad = _issue("answer-keys", "terminal-bench/task-a", ref="nope", note="oq6-keyed")
    with pytest.raises(ValueError, match="no OQ-6 key"):
        tq.select_items([], [bad], [], {}, [_case()])


def _aggregate(rows: list[Any]) -> dict[str, Any]:
    evaluable = sum(row["key_count"] for row in rows)
    return {
        "gates": {
            gate: {
                "matched_keys": sum(row["gates"][gate]["matched_keys"] for row in rows),
                "evaluable_keys": evaluable,
                "eligible_findings": sum(len(row["memberships"][gate]) for row in rows),
            }
            for gate in tq.GATES
        }
    }


def _shared(left: list[Any], right: list[Any]) -> tuple[list[Any], list[Any], list[str]]:
    a = {row["case_id"]: row for row in left}
    b = {row["case_id"]: row for row in right}
    ids = sorted(k for k in a.keys() & b.keys() if a[k]["complete"] and b[k]["complete"])
    return [a[k] for k in ids], [b[k] for k in ids], ids


def test_keyed_reproduction_agrees_with_records() -> None:
    cases = [_case("D0001", "task-a", ("k1",)), _case("D0002", "task-b", ("k2",))]
    payloads = [_payload("wrong_pass", 0.6)]
    cells = {
        (SONNET, "A", "D0001"): _cell(
            arm="A", payloads=payloads, new=["C1"], matching_new={"C1": "k1"}
        ),
        (SONNET, "B", "D0001"): _cell(arm="B"),
        (SONNET, "A", "D0002"): _cell(
            arm="A", case_id="D0002", payloads=payloads, new=["C1"], matching_new={"C1": "k2"}
        ),
        # D0002 never ran in arm B: outside the shared A/B intersection
    }
    items = [_item("ib-1", key_id="k1"), _item("ib-2", task="task-b", key_id="k2")]
    records = tq.task_findings(items, cases, cells, routes=[SONNET])
    result = tq.keyed_reproduction(
        records, items, cases, cells, aggregate=_aggregate, shared=_shared
    )
    sonnet = result[SONNET]
    assert sonnet["shared_complete_keyed_tasks"] == 1
    assert sonnet["A@new_verified_0_5"] == {
        "frozen_matched_keys": 1,
        "frozen_evaluable_keys": 1,
        "frozen_eligible_findings": 1,
        "item_records_alarmed_keys": 1,
        "agree": True,
    }
    assert sonnet["B@new_verified_0_5"]["frozen_matched_keys"] == 0
    assert result["kimi-code/kimi-for-coding"]["status"].startswith("unavailable")


def test_summarize_counts_per_arm() -> None:
    cell = _cell(payloads=[_payload("wrong_pass", 0.9)], new=["C1"], old=["C1"])
    negative = TaskItem(
        "ib-neg", "cloud-task-issues", "grader_wrong_pass", "negative", "ns/task-a", "task-a"
    )
    items = [_item("ib-pos"), negative]
    cells = {(SONNET, "A", "D0001"): cell}
    records = tq.task_findings(items, [_case()], cells, routes=[SONNET])
    groups = tq.summarize(records, items, [_case()], cells)
    arm = groups[f"{SONNET}#A@new_verified_0_5"]
    assert arm["tasks_complete"] == {"dev": 1}
    assert arm["positives_alarmed"] == 1 and arm["positives_judged"] == 1
    assert arm["negative_alarms"] == 1
    assert arm["items"]["cloud-task-issues:negative"] == {"alarm": 1}
    assert groups[f"{SONNET}#B@old_0_8"]["items"]["positive"] == {"unknown": 1}
