"""Fail-closed admission, AutoReview cache schema and model/spend enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec


def _batch_dir(spec_path: Path) -> Path:
    spec = read_json(spec_path)
    return Path(spec["out_root"]) / spec["batch_id"]


def _set_manifest(spec_path: Path, rows: list[dict[str, Any]]) -> None:
    write_rows(Path(read_json(spec_path)["sources"][0]["rollouts"]), rows)


def _assert_refused(spec_path: Path, match: str) -> None:
    with pytest.raises(Exception, match=match):
        core.run_line(spec_path, spec_path.parent)
    assert not _batch_dir(spec_path).exists()
    assert not Path(read_json(spec_path)["judge"]["queue"]).exists()


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


@pytest.mark.parametrize(
    "document",
    [
        {"split": "test", "metadata": {"split": "dev"}, "steps": []},
        {"metadata": {"split": "test"}, "steps": []},
    ],
)
def test_direct_json_trajectory_declaring_test_is_refused(
    fixture_spec: Path, document: dict[str, Any]
) -> None:
    trajectory = fixture_spec.parent / "trajectory.json"
    write_json(trajectory, document)
    spec = read_json(fixture_spec)
    spec["sources"][0]["rollouts"] = str(trajectory)
    write_json(fixture_spec, spec)
    _assert_refused(fixture_spec, "non-DEV")


def test_nested_answer_key_file_inside_a_bundle_is_refused(fixture_spec: Path) -> None:
    bundle = fixture_spec.parent / "bundle"
    (bundle / "research" / "answer-keys").mkdir(parents=True)
    (bundle / "solution.py").write_text("def f():\n    return 1\n")
    (bundle / "research" / "answer-keys" / "gold.json").write_text('{"x": 1}')
    _set_manifest(fixture_spec, [{"id": "bundled", "task_id": "plain-task", "path": str(bundle)}])
    _assert_refused(fixture_spec, "answer-keys")


@pytest.mark.parametrize(
    "status",
    [
        {"split": "test", "tasks": [{"task_id": "plain-task", "status": "weak", "reason": "x"}]},
        {
            "tasks": [
                {"task_id": "plain-task", "status": "weak", "reason": "x", "split": "test"},
            ]
        },
        {"plain-task": {"status": "weak", "reason": "x", "metadata": {"split": "heldout"}}},
    ],
)
def test_task_status_declaring_a_non_dev_split_is_refused(
    fixture_spec: Path, status: dict[str, Any]
) -> None:
    write_json(Path(read_json(fixture_spec)["task_status"]), status)
    _assert_refused(fixture_spec, "non-DEV")


def _cache_spec(spec_path: Path, rows: list[dict[str, Any]]) -> None:
    spec = read_json(spec_path)
    cache = spec_path.parent / "cache"
    write_rows(cache / "review.jsonl", rows)
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    spec["models"]["allowed"] = ["synthetic/model"]
    write_json(spec_path, spec)


def _cached(run_id: str, **stage: Any) -> dict[str, Any]:
    base = {
        "decision": "review",
        "reasons": ["AUTOREVIEW_SUSPICION_80"],
        "details": {
            "suspicion": 80,
            "verdict": "suspicious",
            "model": "synthetic/model",
            "attested": True,
            "cost_usd": 0.0,
        },
    }
    return {"id": run_id, "stage_decisions": {"autoreview": {**base, **stage}}}


def _both(**stage: Any) -> list[dict[str, Any]]:
    return [_cached("clean-run", **stage), _cached("weak-run", **stage)]


@pytest.mark.parametrize(
    "rows,match",
    [
        # evaluator fields hidden in details, in the row, or in another stage
        (
            _both(details={"suspicion": 80, "verdict": "suspicious", "label": True}),
            "label",
        ),
        (
            [{**row, "is_cheat": True} for row in _both()],
            "evaluator/label field",
        ),
        (
            [
                {
                    **row,
                    "stage_decisions": {
                        **row["stage_decisions"],
                        "l0": {"details": {"expected_verdict": "cheat"}},
                    },
                }
                for row in _both()
            ],
            "evaluator/label field",
        ),
        # a free-form reason is how a label could steer the decision
        (_both(reasons=["HUMAN_LABEL_TRUE"]), "outside the stage schema"),
        (_both(extra="x"), "unexpected fields"),
        # a verdict that contradicts its own evidence
        (
            _both(decision="drop", reasons=["AUTOREVIEW_CLEAN"], details={"suspicion": 5}),
            "disagrees with its own evidence",
        ),
        (
            _both(
                details={
                    **_cached("x")["stage_decisions"]["autoreview"]["details"],
                    "attested": False,
                }
            ),
            "disagrees",
        ),
        # DEV declaration on a cached row
        ([{**row, "split": "test"} for row in _both()], "non-DEV"),
    ],
)
def test_untrusted_autoreview_cache_is_refused_before_any_output(
    fixture_spec: Path, rows: list[dict[str, Any]], match: str
) -> None:
    _cache_spec(fixture_spec, rows)
    _assert_refused(fixture_spec, match)


def test_trace_never_carries_cache_free_text_and_flipped_labels_change_nothing(
    fixture_spec: Path,
) -> None:
    _cache_spec(fixture_spec, _both())
    first = core.run_line(fixture_spec, fixture_spec.parent)
    spec = read_json(fixture_spec)
    spec["batch_id"] = "flipped"
    write_json(fixture_spec, spec)
    labels = Path(spec["sources"][0]["labels"])
    write_rows(
        labels, [{"id": "clean-run", "is_cheat": True}, {"id": "weak-run", "is_cheat": False}]
    )
    second = core.run_line(fixture_spec, fixture_spec.parent)
    for name in ("decisions.jsonl", "trace.jsonl"):
        assert digest(first / name) == digest(second / name)
    for row in read_rows(first / "trace.jsonl"):
        assert set(row["autoreview"]["details"]) <= set(core._AR_DETAILS)


def test_label_isolation_inspects_trace_and_receipts(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert _check(batch, fixture_spec.parent, "label_isolation")["status"] == "PASS"
    trace = read_rows(batch / "trace.jsonl")
    trace[0]["autoreview"]["details"]["label"] = True
    write_rows(batch / "trace.jsonl", trace)
    receipt = read_json(batch / "receipts" / "trace.json")
    receipt["outputs"][0]["sha256"] = digest(batch / "trace.jsonl")  # provenance stays consistent
    write_json(batch / "receipts" / "trace.json", receipt)
    result = _check(batch, fixture_spec.parent, "label_isolation")
    assert result["status"] == "FAIL" and "label" in result["details"]


def test_label_isolation_inspects_receipts(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    receipt = read_json(batch / "receipts" / "decisions.json")
    receipt["counts"]["is_cheat"] = 3
    write_json(batch / "receipts" / "decisions.json", receipt)
    assert _check(batch, fixture_spec.parent, "label_isolation")["status"] == "FAIL"


def test_trace_tampering_is_caught_by_recomputation(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    trace = read_rows(batch / "trace.jsonl")
    trace[0]["l0"]["reasons"] = ["FORGED"]
    write_rows(batch / "trace.jsonl", trace)
    receipt = read_json(batch / "receipts" / "trace.json")
    receipt["outputs"][0]["sha256"] = digest(batch / "trace.jsonl")
    write_json(batch / "receipts" / "trace.json", receipt)
    assert _check(batch, fixture_spec.parent, "determinism")["status"] == "FAIL"


# ---- live AutoReview: served route, fallbacks, preventive cap --------------------------------

ROUTE = "synthetic/model"


@pytest.fixture
def live_spec(fixture_spec: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    steps = [
        {"step_id": 0, "source": "user", "message": "Fix the bug"},
        {
            "step_id": 1,
            "source": "assistant",
            "tool_calls": [{"name": "write", "arguments": {"path": "a.py", "content": "x = 1\n"}}],
        },
    ]
    rows = []
    for index in (1, 2):
        trajectory = fixture_spec.parent / f"t{index}.json"
        write_json(trajectory, {"steps": steps})
        rows.append({"id": f"run-{index}", "task_id": "plain-task", "path": str(trajectory)})
    _set_manifest(fixture_spec, rows)
    write_rows(Path(read_json(fixture_spec)["sources"][0]["labels"]), [])
    spec = read_json(fixture_spec)
    spec["autoreview"] = {
        "mode": "live",
        "route": ROUTE,
        "approval": "synthetic test",
        "max_usd": 1.0,
        "max_call_usd": 0.6,
    }
    spec["models"]["allowed"] = [ROUTE]
    write_json(fixture_spec, spec)
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _item: soft)
    return fixture_spec


def _stub(monkeypatch: pytest.MonkeyPatch, **result: Any) -> list[str]:
    calls: list[str] = []

    def fake(prompt: str, *, model: str, **_: Any) -> dict[str, Any]:
        calls.append(model)
        return {
            "stdout": json.dumps({"suspicion": 10, "verdict": "clean"}),
            "served_models": [model],
            "n_fallbacks": 0,
            "usage": {"usd": 0.1},
            **result,
        }

    monkeypatch.setattr(hd, "run_omp_session", fake)
    return calls


def test_live_run_records_and_verifies_the_served_route(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch)
    batch = core.run_line(live_spec, live_spec.parent)
    assert calls == [ROUTE, ROUTE]
    assert core.check_batch(batch, live_spec.parent)["passed"]
    trace = read_rows(batch / "trace.jsonl")
    assert [r["autoreview"]["call"]["served_models"] for r in trace] == [[ROUTE], [ROUTE]]
    assert read_json(batch / "receipts" / "trace.json")["models"] == [
        {"role": "autoreview", "route": ROUTE}
    ]
    assert read_json(batch / "receipts" / "trace.json")["spend_usd"] == pytest.approx(0.2)


def test_a_redirected_route_fails_even_though_the_requested_route_is_allowed(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, served_models=["synthetic/disallowed"], n_fallbacks=1)
    batch = core.run_line(live_spec, live_spec.parent)
    assert {m["route"] for m in read_json(batch / "receipts" / "trace.json")["models"]} == {
        "synthetic/disallowed"
    }
    result = _check(batch, live_spec.parent, "spend_and_models")
    assert result["status"] == "FAIL"
    # The reviewer's attestation rule already made the verdict a review, never a keep.
    assert {r["decision"] for r in read_rows(batch / "decisions.jsonl")} == {"review"}


def test_a_fallback_with_an_allowed_route_still_fails_the_gate(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, n_fallbacks=1)
    batch = core.run_line(live_spec, live_spec.parent)
    result = _check(batch, live_spec.parent, "spend_and_models")
    assert result["status"] == "FAIL" and "fallback" in result["details"]


def test_live_cap_is_reserved_before_each_call(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, usage={"usd": 0.5})
    batch = core.run_line(live_spec, live_spec.parent)
    # 0.5 spent + 0.6 reserved > 1.0: the second call is refused, not made and then audited.
    assert len(calls) == 1
    reasons = [r["autoreview"]["reasons"] for r in read_rows(batch / "trace.jsonl")]
    assert reasons[1] == ["AUTOREVIEW_ERROR:BudgetReservationRefused"]
    assert core.check_batch(batch, live_spec.parent)["passed"]


def test_a_call_over_its_bound_halts_further_calls_and_fails_the_gate(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, usage={"usd": 0.9})
    batch = core.run_line(live_spec, live_spec.parent)
    assert len(calls) == 1
    result = _check(batch, live_spec.parent, "spend_and_models")
    assert result["status"] == "FAIL" and "bound" in result["details"]


def test_live_spec_needs_a_per_call_bound(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    spec["models"]["allowed"] = [ROUTE]
    live = {"mode": "live", "route": ROUTE, "approval": "x", "max_usd": 1}
    for extra in ({}, {"max_call_usd": 0}, {"max_call_usd": 2}, {"max_call_usd": True}):
        spec["autoreview"] = {**live, **extra}
        write_json(fixture_spec, spec)
        with pytest.raises(ValueError, match="max_call_usd"):
            load_spec(fixture_spec)
    spec["autoreview"] = {**live, "max_call_usd": 0.5}
    write_json(fixture_spec, spec)
    assert load_spec(fixture_spec)["autoreview"]["max_call_usd"] == 0.5
