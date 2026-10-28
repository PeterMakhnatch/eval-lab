"""Synthetic-only report proofs; no model, corpus, credential, or cloud access."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SUMMARY_PATH = Path(__file__).resolve().parents[1] / "research/experiments/oq6-task-quality-gate-rerun/result_summary.py"
spec = importlib.util.spec_from_file_location("oq6_task_quality_summary", SUMMARY_PATH)
assert spec and spec.loader
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def row(case_id, keys=1, *, complete=True):
    return {"case_id": case_id, "key_count": keys, "complete": complete}


def test_ab_uses_only_same_complete_cells_and_exact_shared_key_denominator():
    a = [row("D1", 3), row("D2", 2), row("D3", 5, complete=False)]
    b = [row("D1", 3, complete=False), row("D2", 2), row("D3", 5)]
    aggregate = Mock(side_effect=lambda rows: {"keys": sum(item["key_count"] for item in rows)})
    result = summary.paired_ab_report(a, b, aggregate)
    assert result["shared_case_ids"] == ["D2"]
    assert result["shared_complete_tasks"] == 1
    assert result["shared_evaluable_keys"] == 2
    assert result["A"] == result["B"] == {"keys": 2}
    assert result["not_full_ALL_DEV_coverage"] is True
    assert [[item["case_id"] for item in call.args[0]] for call in aggregate.call_args_list] == [["D2"], ["D2"]]


def test_empty_ab_intersection_is_unavailable_not_zero_recall():
    aggregate = Mock()
    result = summary.paired_ab_report([row("D1")], [row("D2")], aggregate)
    assert result["status"] == "unavailable"
    assert result["shared_evaluable_keys"] == 0
    assert result["A"] is None and result["B"] is None
    aggregate.assert_not_called()


def test_ab_rejects_changed_key_denominator_or_duplicate_identity():
    with pytest.raises(ValueError, match="denominators"):
        summary.shared_complete_rows([row("D1", 2)], [row("D1", 3)])
    with pytest.raises(ValueError, match="duplicate"):
        summary.shared_complete_rows([row("D1"), row("D1")], [row("D1")])


def attempt_directory(root, *, role="audit"):
    path = root / "calls" / "synthetic--model" / "A" / "dev" / "D1" / role
    path.mkdir(parents=True)
    return path


def test_receipt_before_final_cell_is_counted_once_with_actual_usage(tmp_path):
    path = attempt_directory(tmp_path)
    marker = {"requested_route": "synthetic/model", "attempt_limit": 1}
    receipt = {"attempt_consumed": True, "status": "answered", "tokens_in": 17, "tokens_out": 5}
    (path / "call-consumed.json").write_text(json.dumps(marker))
    (path / "receipt.json").write_text(json.dumps(receipt))
    inspect = Mock()
    result = summary.reconcile_native_attempts(tmp_path, ["synthetic/model"], [{"case_id": "D1", "stage": "dev"}], inspect)
    assert len(result) == 1
    assert result[0]["receipt"]["tokens_in"] == 17
    assert result[0]["receipt"]["tokens_out"] == 5
    assert result[0]["complete_native_receipt"] is True
    assert not (path.parent / "cell.json").exists()
    inspect.assert_not_called()


def test_consumed_without_receipt_recovers_snapshot_and_preserves_unknown_usage(tmp_path):
    path = attempt_directory(tmp_path)
    marker = {"requested_route": "synthetic/model", "attempt_limit": 1}
    (path / "call-consumed.json").write_text(json.dumps(marker))
    inspect = Mock(return_value={"attempt_consumed": True, "status": "unanswered", "tokens_in": None, "tokens_out": None})
    result = summary.reconcile_native_attempts(tmp_path, ["synthetic/model"], [{"case_id": "D1", "stage": "dev"}], inspect)
    assert len(result) == 1
    assert result[0]["complete_native_receipt"] is False
    assert result[0]["receipt"]["tokens_in"] is None
    assert result[0]["receipt"]["tokens_out"] is None
    assert result[0]["consumption_sha256"] is not None
    inspect.assert_called_once_with(path, marker, "report_consumed_without_complete_receipt")
    assert not (path / "receipt.json").exists()


def test_incomplete_receipt_never_hides_its_consumption_marker(tmp_path):
    path = attempt_directory(tmp_path, role="scorer")
    (path / "call-consumed.json").write_text(json.dumps({"requested_route": "synthetic/scorer"}))
    (path / "receipt.json").write_text('{"attempt_consumed":')
    inspect = Mock(return_value={"attempt_consumed": True, "status": "unanswered", "tokens_in": None, "tokens_out": None})
    result = summary.reconcile_native_attempts(tmp_path, ["synthetic/model"], [{"case_id": "D1", "stage": "dev"}], inspect)
    assert len(result) == 1 and result[0]["role"] == "scorer"
    assert result[0]["receipt_error"] is not None
    assert result[0]["complete_native_receipt"] is False
    inspect.assert_called_once()


def test_cli_does_not_pair_different_provider_routes(tmp_path, monkeypatch):
    keys = b'[{"key_id":"synthetic-key"}]'
    key_path = tmp_path / "keys.json"
    key_path.write_bytes(keys)
    case = {"case_id": "D1", "stage": "dev", "key_count": 1, "keys_path": str(key_path), "keys_sha256": hashlib.sha256(keys).hexdigest(), "preparation_status": "prepared"}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"cases": [case]}))
    models = ["provider-a/model", "provider-b/model"]
    protocol = {"study_version": "synthetic", "models": models, "execution": {"raw_root": str(tmp_path), "plan_path": str(plan_path), "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(), "cutoff_at": "2000-01-01T00:00:00Z", "disabled_routes": {}}}
    protocol_path = tmp_path / "freeze.json"
    protocol_path.write_text(json.dumps(protocol))
    digest = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    protocol_path.with_suffix(".sha256").write_text(digest)
    def aggregate(rows):
        return {"full_cohort_complete": all(item["complete"] for item in rows), "rows": len(rows)}
    fake_paired = SimpleNamespace(aggregate_cells=aggregate, evaluate_cell=lambda item, *args, **kwargs: row(item["case_id"], complete=False))
    monkeypatch.setattr(summary, "_science", lambda protocol: (fake_paired, lambda receipts: {"attempts": len(receipts)}, SimpleNamespace(_abandoned=Mock())))
    for model, arm in ((models[0], "A"), (models[1], "B")):
        path = tmp_path / "calls" / model.replace("/", "--") / arm / "dev" / "D1" / "cell.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"freeze_sha256": digest, "route": model, "arm": arm, "case_id": "D1", "paired": row("D1")}))
    # An answered audit can survive a crash before its final scientific cell.
    # It must contribute one actual attempt/usage record, not a zero-cost miss.
    partial = tmp_path / "calls" / models[0].replace("/", "--") / "B" / "dev" / "D1" / "audit"
    partial.mkdir(parents=True)
    (partial / "call-consumed.json").write_text(json.dumps({"requested_route": models[0]}))
    (partial / "receipt.json").write_text(json.dumps({"attempt_consumed": True, "status": "answered", "tokens_in": 13, "tokens_out": 7}))
    terminal = tmp_path / "outcomes" / (models[1].replace("/", "--") + "__A") / "D1.json"
    terminal.parent.mkdir(parents=True)
    terminal.write_text(json.dumps({"freeze_sha256": digest, "plan_sha256": protocol["execution"]["plan_sha256"], "route": models[1], "arm": "A", "case_id": "D1", "status": "missing", "reason": "cutoff_unsent_or_pending", "cell_path": None, "paired": row("D1", complete=False)}))
    summary.main(["--protocol", str(protocol_path)])
    results = list(tmp_path.glob("results-*.json"))
    assert len(results) == 1
    result = json.loads(results[0].read_text())
    dev = [item for item in result["same_route_AB_shared_complete"] if item["stage"] == "dev"]
    assert len(dev) == 2
    assert all(item["status"] == "unavailable" and item["shared_complete_tasks"] == 0 for item in dev)
    assert result["all_DEV_complete"] is False
    unfinished = next(item for item in result["groups"] if item["auditor_route"] == models[0] and item["arm"] == "B" and item["stage"] == "dev")
    assert unfinished["attempted_audits"] == unfinished["answered_audits"] == 1
    assert unfinished["answered_without_final_quality_tasks"] == 1
    assert unfinished["quality"]["full_cohort_complete"] is False
    assert result["combined_accounting"]["attempts"] == 1
    cutoff = next(item for item in result["groups"] if item["auditor_route"] == models[1] and item["arm"] == "A" and item["stage"] == "dev")
    assert cutoff["scheduler_missing_outcomes"] == cutoff["pending_at_cutoff_tasks"] == 1
    assert cutoff["final_cell_records"] == cutoff["unfinalized_planned_tasks"] == 0
