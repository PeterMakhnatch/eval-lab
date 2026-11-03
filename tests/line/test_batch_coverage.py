"""Every batch file is covered: a hash in provenance or a recomputation catches each edit.

Each tamper below refreshes whatever hash or row count the edit would otherwise break, so the
named gate (and message) is the only thing that can notice it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import (
    canonical_hash,
    digest,
    read_json,
    read_rows,
    strict_digest,
    write_json,
    write_rows,
)
from evallab.line.replay import _input_hashes
from evallab.line.spec import load_spec
from evallab.production.judge_station import STATE_DIR


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def _failed(batch: Path, root: Path) -> dict[str, str]:
    return {
        c["name"]: c["details"]
        for c in core.check_batch(batch, root)["checks"]
        if c["status"] != "PASS"
    }


def _rehash(batch: Path) -> None:
    """Re-hash and re-count every receipt reference that points inside the batch."""
    root = batch.resolve()
    for path in sorted((batch / "receipts").glob("*.json")):
        receipt = read_json(path)
        for side in ("inputs", "outputs"):
            for ref in receipt[side]:
                target = core.resolve_ref(batch, ref["path"])
                if root not in target.resolve().parents:
                    continue
                ref["sha256"] = digest(target)
                if "rows" in ref:
                    ref["rows"] = len(read_rows(target))
        write_json(path, receipt)


def _edit_receipt(batch: Path, stage: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = batch / "receipts" / f"{stage}.json"
    receipt = read_json(path)
    edit(receipt)
    write_json(path, receipt)


# ---- 1. verdicts.jsonl -------------------------------------------------------------------------


def _verdict(row_id: str, run_id: str, verdict: str, judge: str = "j") -> dict[str, Any]:
    return {
        "schema": "judge-verdict/v1",
        "id": row_id,
        "judge": judge,
        "run_id": run_id,
        "verdict": verdict,
    }


def _station(spec_path: Path, rows: list[dict[str, Any]]) -> None:
    write_rows(Path(read_json(spec_path)["judge"]["verdicts"]), rows)


def _verdict_batch(fixture_spec: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    _station(
        fixture_spec,
        rows
        if rows is not None
        else [_verdict("v1", "clean-run", "cheat"), _verdict("v2", "weak-run", "legitimate")],
    )
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    return batch


def _ghost(rows: list[dict[str, Any]]) -> None:
    rows.append(_verdict("v9", "ghost-run", "cheat"))


def _label_field(rows: list[dict[str, Any]]) -> None:
    rows[0]["is_cheat"] = True


VERDICT_TAMPERS: dict[str, Callable[[list[dict[str, Any]]], None]] = {
    "flipped value": lambda rows: rows[0].update(verdict="legitimate"),
    "duplicated row": lambda rows: rows.append(dict(rows[0])),
    "row for a run that was not admitted": _ghost,
    "is_cheat field added": _label_field,
    "row removed": lambda rows: rows.pop(),
}


def test_untouched_verdicts_are_the_pinned_stations_effective_rows(fixture_spec: Path) -> None:
    batch = _verdict_batch(fixture_spec)
    assert read_rows(batch / "verdicts.jsonl") == [
        _verdict("v1", "clean-run", "cheat"),
        _verdict("v2", "weak-run", "legitimate"),
    ]
    assert not _failed(batch, fixture_spec.parent)


@pytest.mark.parametrize("tamper", list(VERDICT_TAMPERS))
def test_an_edited_verdicts_file_with_refreshed_hashes_fails_judge_routing(
    fixture_spec: Path, tamper: str
) -> None:
    batch = _verdict_batch(fixture_spec)
    rows = read_rows(batch / "verdicts.jsonl")
    VERDICT_TAMPERS[tamper](rows)
    write_rows(batch / "verdicts.jsonl", rows)
    _rehash(batch)
    failed = _failed(batch, fixture_spec.parent)
    assert set(failed) == {"judge_routing"}, failed
    assert "verdicts.jsonl differs from its Station snapshot" in failed["judge_routing"]


def test_the_edit_without_a_refreshed_hash_is_caught_by_provenance(fixture_spec: Path) -> None:
    batch = _verdict_batch(fixture_spec)
    rows = read_rows(batch / "verdicts.jsonl")
    rows[0]["verdict"] = "legitimate"
    write_rows(batch / "verdicts.jsonl", rows)
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "hash mismatch" in result["details"]


def test_station_rows_for_runs_that_were_not_admitted_are_excluded(fixture_spec: Path) -> None:
    batch = _verdict_batch(
        fixture_spec,
        [
            _verdict("v1", "clean-run", "cheat"),
            _verdict("v2", "some-other-run", "cheat"),
            _verdict("v3", "ghost-run", "legitimate"),
        ],
    )
    assert [r["run_id"] for r in read_rows(batch / "verdicts.jsonl")] == ["clean-run"]
    # The snapshot still holds every Station byte, so the exclusion is a recomputation, not a copy.
    snapshot = next((batch / "evidence" / "station-inputs").glob("*.jsonl"))
    assert len(read_rows(snapshot)) == 3


def test_only_the_latest_verdict_of_one_judge_for_one_run_is_kept(fixture_spec: Path) -> None:
    batch = _verdict_batch(
        fixture_spec,
        [
            _verdict("v1", "clean-run", "cheat", judge="j"),
            _verdict("v2", "clean-run", "legitimate", judge="j"),
            _verdict("v3", "clean-run", "cheat", judge="k"),
        ],
    )
    kept = read_rows(batch / "verdicts.jsonl")
    assert [(r["id"], r["judge"], r["verdict"]) for r in kept] == [
        ("v2", "j", "legitimate"),
        ("v3", "k", "cheat"),
    ]
    # Putting the superseded row back (hashes refreshed) is a mismatch with the recomputation.
    write_rows(batch / "verdicts.jsonl", [_verdict("v1", "clean-run", "cheat"), *kept])
    _rehash(batch)
    failed = _failed(batch, fixture_spec.parent)
    assert set(failed) == {"judge_routing"}, failed
    assert "verdicts.jsonl differs from its Station snapshot" in failed["judge_routing"]


# ---- 2. tasks.jsonl ----------------------------------------------------------------------------


def _dup_task(rows: list[dict[str, Any]]) -> None:
    rows.append(dict(rows[0]))


def _extra_task_field(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["note"] = "injected"


def _reorder_tasks(rows: list[dict[str, Any]]) -> None:
    rows.reverse()


TASK_TAMPERS: dict[str, Callable[[list[dict[str, Any]]], None]] = {
    "duplicated row": _dup_task,
    "extra field": _extra_task_field,
    "reordered rows": _reorder_tasks,
}


@pytest.mark.parametrize("tamper", list(TASK_TAMPERS))
def test_an_edited_tasks_file_with_refreshed_hashes_fails_dev_only(
    fixture_spec: Path, tamper: str
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    rows = read_rows(batch / "tasks.jsonl")
    assert len(rows) == 2
    TASK_TAMPERS[tamper](rows)
    write_rows(batch / "tasks.jsonl", rows)
    _rehash(batch)
    _edit_receipt(batch, "task", lambda r: r["counts"].update(rows_out=len(rows)))
    result = _check(batch, fixture_spec.parent, "dev_only")
    assert result["status"] == "FAIL" and "task source changed" in result["details"]
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "PASS"


# ---- 3. receipts -------------------------------------------------------------------------------


def _set(**fields: Any) -> Callable[[dict[str, Any]], None]:
    return lambda receipt: receipt.update(fields)


def _drop_rows(receipt: dict[str, Any]) -> None:
    del receipt["outputs"][0]["rows"]


def _drop_field(receipt: dict[str, Any]) -> None:
    del receipt["seconds"]


RECEIPT_TAMPERS: list[tuple[str, str, Callable[[dict[str, Any]], None], str]] = [
    (
        "task",
        "rows_in",
        lambda r: r["counts"].update(rows_in=9999),
        "task receipt totals differ from the batch files",
    ),
    (
        "export",
        "rows_out",
        lambda r: r["counts"].update(rows_out=9999),
        "export receipt totals differ from the batch files",
    ),
    ("intake", "rows deleted", _drop_rows, "intake receipt outputs are not exactly"),
    (
        "decisions",
        "one code_rev",
        _set(code_rev="b" * 40),
        "receipts come from different code revisions",
    ),
    ("export", "batch_id", _set(batch_id="another"), "export receipt names another batch"),
    (
        "judge",
        "extra field",
        _set(unvalidated="x"),
        "judge receipt has unexpected or missing fields",
    ),
    ("task", "missing field", _drop_field, "task receipt has unexpected or missing fields"),
    (
        "trace",
        "forged errors",
        _set(errors=[{"class": "X", "message": "AUTOREVIEW_ERROR:X", "run_id": "clean-run"}]),
        "trace receipt errors differ from the trace",
    ),
    (
        "intake",
        "errors on a non-trace stage",
        _set(errors=[{"class": "X", "message": "m", "run_id": "clean-run"}]),
        "intake receipt errors differ from the trace",
    ),
    (
        "judge",
        "models",
        _set(models=[{"role": "autoreview", "route": "synthetic/model"}]),
        "judge receipt records model use or spend",
    ),
    ("export", "spend_usd", _set(spend_usd=0.5), "export receipt records model use or spend"),
]


def test_an_untouched_batch_passes_every_gate(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    result = core.check_batch(batch, fixture_spec.parent)
    assert result["passed"] and len(result["checks"]) == 8


@pytest.mark.parametrize(
    "stage,edit,tamper,message",
    RECEIPT_TAMPERS,
    ids=[f"{stage}-{edit}" for stage, edit, *_ in RECEIPT_TAMPERS],
)
def test_a_forged_receipt_field_fails_provenance(
    fixture_spec: Path,
    stage: str,
    edit: str,
    tamper: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    _edit_receipt(batch, stage, tamper)
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and message in result["details"], result


# ---- 4. live mode ------------------------------------------------------------------------------

ROUTE = "synthetic/model"
AR_KEYS = ("stage", "decision", "reasons", "evidence_steps", "details")


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
    spec = read_json(fixture_spec)
    write_rows(Path(spec["sources"][0]["rollouts"]), rows)
    write_rows(Path(spec["sources"][0]["labels"]), [])
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


def _retrace(batch: Path, root: Path, mutate: Callable[[list[dict[str, Any]]], None]) -> None:
    """Edit trace.jsonl, then make every other file agree with the edited trace.

    Decisions are recomputed from the edited readings (as the check does), the decision hashes in
    check.json and every receipt hash are refreshed, so only the live-reading validation can fail.
    """
    rows = read_rows(batch / "trace.jsonl")
    mutate(rows)
    write_rows(batch / "trace.jsonl", rows)
    spec = read_json(batch / "spec.json")
    items, intake = core._intake(spec, root)
    stored = {
        r["run_id"]: hd.StageVerdict(**{k: v for k, v in r["autoreview"].items() if k in AR_KEYS})
        for r in rows
    }
    decisions, _, _ = core._evaluate(spec, items, intake, core._tasks(spec, items), {}, stored)
    write_rows(batch / "decisions.jsonl", decisions)
    for decision, name in (("keep", "kept"), ("drop", "dropped"), ("review", "review")):
        write_rows(
            batch / "export" / f"{name}.jsonl", [d for d in decisions if d["decision"] == decision]
        )
    queue = core._queue(decisions)
    write_rows(batch / "judge-queue.jsonl", queue)
    check = read_json(batch / "check.json")
    check["decision_hash"] = check["second_decision_hash"] = canonical_hash(decisions)
    write_json(batch / "check.json", check)
    _rehash(batch)
    _edit_receipt(batch, "judge", lambda r: r["counts"].update(rows_out=len(queue)))
    spend = sum(r["autoreview"]["call"]["cost_usd"] for r in rows if "call" in r["autoreview"])
    routes = [{"role": "autoreview", "route": x} for x in sorted(core.used_routes(rows))]
    _edit_receipt(batch, "trace", lambda r: r.update(spend_usd=spend, models=routes))


def _live_failure(batch: Path, root: Path, message: str, run_id: str) -> None:
    determinism = _check(batch, root, "determinism")
    assert determinism["status"] == "FAIL", determinism
    assert message in determinism["details"] and run_id in determinism["details"], determinism
    # Not a hash or recomputation failure: the evidence is otherwise self-consistent.
    assert "decision hashes differ" not in determinism["details"]
    assert "differs from recomputation" not in determinism["details"]
    assert _check(batch, root, "provenance")["status"] == "PASS"
    failed = _failed(batch, root)
    # Everything else agrees with the edited trace; only the scoreboard view may follow it.
    assert set(failed) - {"scoreboard_recompute"} == {"determinism"}, failed


def _live_batch(live_spec: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _stub(monkeypatch)
    batch = core.run_line(live_spec, live_spec.parent)
    assert core.check_batch(batch, live_spec.parent)["passed"]
    return batch


def _row(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    return next(r for r in rows if r["run_id"] == run_id)["autoreview"]


def test_a_normal_live_batch_passes_every_gate_and_the_trace_is_backed(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    for row in read_rows(batch / "trace.jsonl"):
        ar = row["autoreview"]
        assert ar["reasons"] == ["AUTOREVIEW_CLEAN"] and ar["details"]["model"] == ROUTE
        assert ar["call"]["cost_usd"] == pytest.approx(ar["details"]["cost_usd"])


def test_live_reading_whose_decision_contradicts_its_reasons_fails_determinism(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    _retrace(batch, live_spec.parent, lambda rows: _row(rows, "run-1").update(decision="drop"))
    _live_failure(batch, live_spec.parent, "disagrees with its own evidence", "run-1")


def test_live_reading_without_its_call_record_fails_determinism(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    _retrace(batch, live_spec.parent, lambda rows: _row(rows, "run-2").pop("call"))
    _live_failure(batch, live_spec.parent, "not backed by its call record", "run-2")


def test_live_reading_whose_cost_disagrees_with_its_call_fails_determinism(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)
    _retrace(
        batch,
        live_spec.parent,
        lambda rows: _row(rows, "run-1")["details"].update(cost_usd=0.05),
    )
    _live_failure(batch, live_spec.parent, "not backed by its call record", "run-1")


def test_not_eligible_reading_on_an_eligible_run_fails_determinism(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _live_batch(live_spec, monkeypatch)

    def hide_the_reading(rows: list[dict[str, Any]]) -> None:
        ar = _row(rows, "run-1")
        ar.pop("call")
        ar.update(
            decision="pass",
            reasons=["AUTOREVIEW_NOT_ELIGIBLE"],
            details={"eligible": False},
            score=None,
            cited_steps=ar["evidence_steps"],
        )

    _retrace(batch, live_spec.parent, hide_the_reading)
    _live_failure(batch, live_spec.parent, "eligible run without a live reading", "run-1")


def test_a_reading_planted_on_an_ineligible_run_fails_determinism(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    quiet = hd.StageVerdict("l0", "pass", [], [], {})
    monkeypatch.setattr(
        hd, "evaluate_l0_stage", lambda item: quiet if item.item_id == "run-2" else soft
    )
    batch = _live_batch(live_spec, monkeypatch)
    rows = read_rows(batch / "trace.jsonl")
    assert _row(rows, "run-2")["reasons"] == ["AUTOREVIEW_NOT_ELIGIBLE"]
    assert "call" not in _row(rows, "run-2")

    def plant(rows: list[dict[str, Any]]) -> None:
        _row(rows, "run-2").update(
            {
                k: v
                for k, v in _row(rows, "run-1").items()
                if k in {*AR_KEYS, "call", "score", "cited_steps"}
            }
        )

    _retrace(batch, live_spec.parent, plant)
    _live_failure(batch, live_spec.parent, "live reading on an ineligible run", "run-2")


# ---- 5 & 6. declared inputs that are one file --------------------------------------------------


def test_the_default_station_verdicts_file_counts_as_a_declared_input(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    del spec["judge"]["verdicts"]
    write_json(fixture_spec, spec)
    assert load_spec(fixture_spec)["judge"].get("verdicts") is None  # distinct files are fine
    station_file = STATE_DIR / "verdicts.jsonl"
    for kind in ("queue", "labels"):
        broken = read_json(fixture_spec)
        if kind == "queue":
            broken["judge"]["queue"] = str(station_file)
        else:
            broken["sources"][0]["labels"] = str(station_file)
        write_json(fixture_spec, broken)
        with pytest.raises(ValueError, match="same file"):
            load_spec(fixture_spec)


KINDS = ("rollouts", "labels", "task_status", "cache", "queue", "verdicts")


def _kinds_spec(tmp_path: Path, spec_path: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    files = {
        "rollouts": Path(read_json(spec_path)["sources"][0]["rollouts"]),
        "labels": tmp_path / "labels.jsonl",
        "task_status": tmp_path / "status.json",
        "cache": tmp_path / "cache.jsonl",
        "queue": tmp_path / "queue.jsonl",
        "verdicts": tmp_path / "no-verdicts.jsonl",
    }
    for name in ("cache", "queue", "verdicts"):
        files[name].write_text("")
    spec = read_json(spec_path)
    spec["autoreview"]["cache"] = str(files["cache"])
    return files, spec


def _point(spec: dict[str, Any], kind: str, path: Path) -> None:
    if kind in {"rollouts", "labels"}:
        spec["sources"][0][kind] = str(path)
    elif kind == "task_status":
        spec["task_status"] = str(path)
    elif kind == "cache":
        spec["autoreview"]["cache"] = str(path)
    else:
        spec["judge"][kind] = str(path)


def test_declared_inputs_of_distinct_files_load(fixture_spec: Path, tmp_path: Path) -> None:
    _, spec = _kinds_spec(tmp_path, fixture_spec)
    write_json(fixture_spec, spec)
    loaded = load_spec(fixture_spec)
    assert loaded["autoreview"]["cache"] == str((tmp_path / "cache.jsonl").resolve())


@pytest.mark.parametrize(
    "kind,other",
    [(kind, other) for kind in KINDS for other in KINDS if kind != other],
)
def test_a_hard_link_of_another_kinds_file_is_refused(
    fixture_spec: Path, tmp_path: Path, kind: str, other: str
) -> None:
    files, spec = _kinds_spec(tmp_path, fixture_spec)
    link = tmp_path / f"link-{kind}-of-{other}"
    os.link(files[other], link)
    _point(spec, kind, link)
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError, match="same file"):
        load_spec(fixture_spec)


def test_one_labels_file_may_serve_two_sources(fixture_spec: Path, tmp_path: Path) -> None:
    spec = read_json(fixture_spec)
    second = tmp_path / "rollouts-b.jsonl"
    write_rows(second, [{"id": "other-run", "task_id": "plain-task", "path": str(tmp_path)}])
    spec["sources"].append(
        {"corpus": "second", "rollouts": str(second), "labels": spec["sources"][0]["labels"]}
    )
    write_json(fixture_spec, spec)
    loaded = load_spec(fixture_spec)
    assert loaded["sources"][0]["labels"] == loaded["sources"][1]["labels"]


# ---- 7. replay input hashes --------------------------------------------------------------------


def _bundle(root: Path, *, link: str | None) -> Path:
    bundle = root / "bundle"
    bundle.mkdir()
    (bundle / "trajectory.json").write_text('{"steps": []}')
    outside = root / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not part of the bundle")
    if link == "file":
        (bundle / "result.json").symlink_to(outside / "secret.txt")
    elif link == "directory":
        (bundle / "verifier").symlink_to(outside, target_is_directory=True)
    return bundle


def _hash_spec(rollouts: Path, cache: Path | None = None) -> dict[str, Any]:
    return {
        "sources": [{"rollouts": str(rollouts), "labels": None}],
        "task_status": None,
        "autoreview": {"cache": str(cache) if cache else None},
    }


def test_replay_hashes_a_plain_bundle_exactly_as_the_receipts_do(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, link=None)
    assert _input_hashes(_hash_spec(bundle)) == {str(bundle): strict_digest(bundle)}
    assert strict_digest(bundle) == digest(bundle)


@pytest.mark.parametrize("link", ["file", "directory"])
def test_replay_refuses_a_symlink_inside_a_rollout_bundle(tmp_path: Path, link: str) -> None:
    bundle = _bundle(tmp_path, link=link)
    with pytest.raises(ValueError, match="symlink"):
        _input_hashes(_hash_spec(bundle))


def test_replay_refuses_a_symlink_inside_a_cache_directory(tmp_path: Path) -> None:
    clean = _bundle(tmp_path, link=None)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "review.jsonl").write_text("")
    (cache / "extra.jsonl").symlink_to(tmp_path / "outside" / "secret.txt")
    with pytest.raises(ValueError, match="symlink"):
        _input_hashes(_hash_spec(clean, cache))
