"""Temp-only refresh controls; no model, grader, exploit replay or live store."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueBank, refresh
from evallab.failure_library.bank.sources import grader_repairs, run_health, seeker
from evallab.failure_library.commands import bank_refresh


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ledger(ctx: BankContext, extra: str = "") -> Path:
    path = ctx.repo_root / "research/answer-keys/splits/assignments.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "key_id,item_id,task,split\ntoy-data,toy-item,toy-task,dev\n" + extra, encoding="utf-8"
    )
    return path


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BankContext:
    root = tmp_path / "repo"
    root.mkdir()
    context = BankContext(
        repo_root=root,
        state_root=tmp_path / "state",
        prod_live=tmp_path / "live",
        cloud_db=tmp_path / "absent-cloud.sqlite",
        cross_test_registry=tmp_path / "bridge" / "registry.json",
    )
    _ledger(context)
    for relative in (
        "tb4-audit-v1/items.jsonl",
        "tb4-audit-v1/tasks.jsonl",
        "cheatbench-actions-v1/items.jsonl",
        "hacktrace-act-v1/items.jsonl",
    ):
        path = root / "research/answer-keys" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    _json(
        root / "research/experiments/mimo-defect-map-v1/inventory.json",
        {
            "dataset_id": "mimo-defect-map-v1",
            "heldout_task_ids_excluded_from_training_all_versions": [],
            "tasks": [],
        },
    )
    monkeypatch.setenv(seeker.RESULTS_ROOT_ENV, str(tmp_path / "results"))
    monkeypatch.setenv(grader_repairs.TS14_SUMMARY_ENV, str(tmp_path / "absent-summary.json"))
    monkeypatch.setattr(grader_repairs, "_TS_REPAIRS", ())
    return context


def _finding(
    kind: str = "loop", *, verdict: str = "alarm", run_id: str = "run-one"
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "detector": f"run_health.{kind}",
        "detector_version": "run-health/4",
        "kind": kind,
        "verdict": verdict,
        "score": 0.9,
        "summary": f"{kind} observation",
        "evidence_steps": [3],
    }


def _findings(ctx: BankContext, rows: list[dict[str, Any]]) -> Path:
    path = ctx.prod_live / "run-health/findings-run-health-4.jsonl"
    _jsonl(path, rows)
    return path


def _bank(ctx: BankContext) -> IssueBank:
    return IssueBank(ctx.repo_root, state_root=ctx.state_root)


def _versions(paths: list[Path]) -> list[tuple[bytes, int, int]]:
    return [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths]


def test_startup_pending_then_unchanged_inputs_do_not_republish_bank(ctx: BankContext) -> None:
    findings = _findings(ctx, [_finding(), _finding(verdict="clear", run_id="not-a-clean-label")])
    bank = _bank(ctx)
    unrelated = bank.items_path("unrelated")
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"retained unrelated source bytes\n")
    first = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert {row["status"] for row in first["sources"].values()} == {"refreshed"}
    items = list(bank.iter_items(["run-health"]))
    assert [(item.subject_id, item.kind, item.label_type, item.polarity) for item in items] == [
        ("run-one", "loop", "detector", "positive")
    ]
    output = first["sources"]["run-health"]["output"]
    assert output["count"] == 1
    assert output["sha256"] == _sha(bank.items_path("run-health").read_bytes())
    assert output["item_digests"] == {
        items[0].item_id: _sha(bank.items_path("run-health").read_bytes().rstrip(b"\n"))
    }
    paths = [bank.manifest_path, bank.items_path("run-health"), unrelated]
    before = _versions(paths)
    # A metadata-only source change is not a byte change.
    findings.touch()
    second = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert {row["status"] for row in second["sources"].values()} == {"unchanged"}
    assert _versions(paths) == before
    assert refresh.receipt_path(bank).is_relative_to(ctx.state_root)
    assert unrelated.read_bytes() == b"retained unrelated source bytes\n"


def test_watch_ignores_unrelated_wal_writes_but_tightens_changed_cloud_admission(
    ctx: BankContext,
) -> None:
    with sqlite3.connect(ctx.cloud_db) as connection:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE corpus_tasks (
                task_id TEXT PRIMARY KEY, dataset_id TEXT, split TEXT,
                original_grader_digest TEXT, repaired_grader_digest TEXT, metadata_json TEXT
            );
            CREATE TABLE agent_traces (
                trace_id TEXT PRIMARY KEY, task_id TEXT, verified_receipt_json TEXT
            );
            CREATE TABLE trace_steps (
                trace_id TEXT, step_index INTEGER, role TEXT, content_text TEXT,
                tool_name TEXT, command_or_action TEXT
            );
            CREATE TABLE unrelated (payload TEXT);
        """)
        connection.execute(
            "INSERT INTO corpus_tasks VALUES (?, ?, ?, ?, ?, ?)",
            ("toy-data/toy-task", "toy-data", "dev", None, None, '{"item_id":"toy-item"}'),
        )
        connection.execute(
            "INSERT INTO agent_traces VALUES (?, ?, ?)",
            ("run-one", "toy-data/toy-task", "{}"),
        )
        connection.execute(
            "INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?)",
            ("run-one", 0, "assistant", "An actor observation.", None, None),
        )
    _findings(ctx, [_finding()])
    bank = _bank(ctx)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert [(item.split, item.actor_transcript) for item in bank.iter_items(["run-health"])] == [
        ("DEV", True)
    ]
    paths = [bank.manifest_path, bank.items_path("run-health")]
    before = _versions(paths)
    with sqlite3.connect(ctx.cloud_db) as writer:
        writer.execute("INSERT INTO unrelated VALUES (?)", ("new data from another lane",))
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert {row["status"] for row in receipt["sources"].values()} == {"unchanged"}
    assert _versions(paths) == before
    with sqlite3.connect(ctx.cloud_db) as writer:
        writer.execute("UPDATE corpus_tasks SET split = 'test'")
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert receipt["sources"]["run-health"]["status"] == "refreshed"
    assert [(item.split, item.actor_transcript) for item in bank.iter_items(["run-health"])] == [
        ("TEST", None)
    ]


@pytest.mark.parametrize("malformed", ['{"run_id":', "[]\n", '{"run_id":"run-one"}\n'])
def test_malformed_and_missing_source_retain_last_good_collection_across_retries(
    ctx: BankContext, malformed: str
) -> None:
    findings = _findings(ctx, [_finding()])
    bank = _bank(ctx)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    paths = [bank.manifest_path, bank.items_path("run-health")]
    before = _versions(paths)
    findings.write_text(malformed, encoding="utf-8")
    for _ in range(2):
        receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
        row = receipt["sources"]["run-health"]
        assert row["status"] == "source-not-ready"
        assert row["output"]["count"] == 1
        assert _versions(paths) == before
    findings.unlink()
    for _ in range(2):
        receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
        assert receipt["sources"]["run-health"]["status"] == "source-not-ready"
        assert "missing" in receipt["sources"]["run-health"]["error"]
        assert _versions(paths) == before
    replacement = findings.with_name("published.tmp")
    _jsonl(replacement, [_finding("runaway")])
    replacement.replace(findings)
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert receipt["sources"]["run-health"]["status"] == "refreshed"
    assert "error" not in receipt["sources"]["run-health"]
    assert [item.kind for item in bank.iter_items(["run-health"])] == ["runaway"]


def test_source_changing_during_build_never_publishes_mixed_generation(
    ctx: BankContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _findings(ctx, [_finding()])
    bank = _bank(ctx)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    before = _versions([bank.manifest_path, bank.items_path("run-health")])
    _jsonl(findings, [_finding("runaway")])
    original = run_health.build

    def changing(context: BankContext):
        yield from original(context)
        _jsonl(findings, [_finding("latency")])

    monkeypatch.setattr(run_health, "build", changing)
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert receipt["sources"]["run-health"]["status"] == "source-not-ready"
    assert "changed during refresh" in receipt["sources"]["run-health"]["error"]
    assert _versions([bank.manifest_path, bank.items_path("run-health")]) == before
    monkeypatch.setattr(run_health, "build", original)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert [item.kind for item in bank.iter_items(["run-health"])] == ["latency"]


def test_watch_refreshes_immediately_and_observes_second_cycle_update(
    ctx: BankContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _findings(ctx, [_finding()])
    cycles = refresh.watch(ctx, interval=0.01, seeker_cells_roots=[])
    first = next(cycles)
    assert first["sources"]["run-health"]["status"] == "refreshed"
    monkeypatch.setattr(refresh.time, "sleep", lambda _: _jsonl(findings, [_finding("runaway")]))
    second = next(cycles)
    cycles.close()
    assert second["sources"]["run-health"]["status"] == "refreshed"
    assert [item.kind for item in _bank(ctx).iter_items(["run-health"])] == ["runaway"]


def test_repair_receipt_retains_full_pairs_and_native_qualification_without_upgrading_partial(
    ctx: BankContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repairs = tuple((pr, "merged", "ts9", f"repair-{pr}") for pr in (1, 2, 3))
    monkeypatch.setattr(grader_repairs, "_TS_REPAIRS", repairs)
    digests: dict[int, tuple[str, str]] = {}
    for pr, _, _, record in repairs:
        before, after = "sha256:" + str(pr) * 64, "sha256:" + str(pr + 3) * 64
        digests[pr] = before, after
        _json(
            ctx.repo_root / "library/task-variants" / f"{record}.json",
            {
                "parent": {"digest": before},
                "variant_digest": after,
                "task_name": f"toy-task-{pr}",
                "transform": "defensive-repair",
                "inputs": {"dev_assignments": [["toy-data", "dev"]]},
            },
        )
    summary = tmp_path / "summary.json"
    cases = []
    for pr, reward, qualified in ((2, 0.0, False), (3, 1.0, True)):
        before, after = digests[pr]
        cases.append(
            {
                "pr": pr,
                "native_qualification": qualified,
                "oracle_original": {"status": "measured", "task_digest": before, "reward": 1.0},
                "oracle_repaired": {"status": "measured", "task_digest": after, "reward": reward},
            }
        )
    _json(summary, {"cases": cases})
    monkeypatch.setenv(grader_repairs.TS14_SUMMARY_ENV, str(summary))
    bank = _bank(ctx)
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    pairs = receipt["sources"]["grader-repairs"]["repair_pairs"]
    items = {int(item.locator): item for item in bank.iter_items(["grader-repairs"])}
    assert len(pairs) == 3
    for pr, item in items.items():
        assert (
            pairs[item.item_id]["before_digest"],
            pairs[item.item_id]["after_digest"],
        ) == digests[pr]
        assert pairs[item.item_id]["digest_kind"] == "package"
    assert pairs[items[1].item_id]["qualification"] == "static_only"
    assert pairs[items[2].item_id]["qualification"] == "native_partial"
    assert pairs[items[2].item_id]["native_qualified"] is False
    assert pairs[items[2].item_id]["repaired_reference_rejected"] is True
    assert pairs[items[3].item_id]["qualification"] == "native_validated"
    assert pairs[items[3].item_id]["native_qualified"] is True
    before = _versions([bank.manifest_path, bank.items_path("grader-repairs")])
    summary.write_text('{"cases":[', encoding="utf-8")
    failed = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert failed["sources"]["grader-repairs"]["status"] == "source-not-ready"
    assert failed["sources"]["grader-repairs"]["repair_pairs"] == pairs
    assert _versions([bank.manifest_path, bank.items_path("grader-repairs")]) == before


def _pilot(ctx: BankContext) -> Path:
    path = seeker._results_root() / "2026-10-28/sb1-pilot-fixture/cells.jsonl"
    _jsonl(
        path,
        [
            {
                "dataset_id": "toy-data",
                "task": "toy-task",
                "model": "toy/model",
                "turn_1": {"outcome": "served_route_mismatch"},
                "turn_2": {"outcome": "upstream_failed", "original_reward": 0.0},
            }
        ],
    )
    return path


def test_seeker_failed_grade_is_not_infra_and_tightened_ledger_is_rechecked(
    ctx: BankContext,
) -> None:
    _pilot(ctx)
    bank = _bank(ctx)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    items = list(bank.iter_items(["seeker"]))
    assert [(item.kind, item.locator, item.label_type, item.split) for item in items] == [
        ("infra", "turn_1", "executed", "DEV")
    ]
    assert items[0].actor_transcript is False
    _ledger(ctx, "secondary,sealed,toy-task,test\n")
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert receipt["sources"]["seeker"]["status"] == "refreshed"
    assert [item.split for item in bank.iter_items(["seeker"])] == ["TEST"]
    assert list(bank.iter_items(["seeker"], split="DEV")) == []


def test_not_ready_source_cannot_leave_old_dev_items_admitted_after_new_seal(
    ctx: BankContext,
) -> None:
    pilot = _pilot(ctx)
    bank = _bank(ctx)
    refresh.refresh_once(ctx, seeker_cells_roots=[])
    previous = list(bank.iter_items(["seeker"]))[0]
    pilot.write_text('{"partial":', encoding="utf-8")
    _ledger(ctx, "secondary,sealed,toy-task,test\n")
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    row = receipt["sources"]["seeker"]
    assert row["status"] == "source-not-ready"
    assert row["retained_dev_quarantined"] == 1
    retained = list(bank.iter_items(["seeker"]))[0]
    assert retained.item_id == previous.item_id
    assert (retained.label, retained.label_type, retained.provenance) == (
        previous.label,
        previous.label_type,
        previous.provenance,
    )
    assert retained.split == "TEST"
    assert list(bank.iter_items(["seeker"], split="DEV")) == []


def _retained_cell(ctx: BankContext) -> tuple[Path, Path]:
    root = ctx.repo_root.parent / "cells"
    cell = root / "toy-data/toy-task/toy_model/turn-1"
    prompt, response, submission = (
        b"Inert retained prompt.\n",
        b'{"text":"inert response"}\n',
        b"#!/bin/sh\nexit 0\n",
    )
    cell.mkdir(parents=True)
    (cell / "prompt.md").write_bytes(prompt)
    (cell / "response.jsonl").write_bytes(response)
    (cell / "submission").mkdir()
    (cell / "submission/solve.sh").write_bytes(submission)
    for side, reward in (("original", 1.0), ("repaired", 0.0)):
        log = cell / side / "verifier/test-stdout.txt"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"inert retained native log\n")
        _json(
            cell / side / "candidate.json",
            {
                "schema": "evallab.candidate.result/v1",
                "status": "measured",
                "executed": True,
                "error": None,
                "reward": reward,
                "task": {"id": "toy-task"},
                "logs": {
                    "verifier": [{"path": str(log), "digest": "sha256:" + _sha(log.read_bytes())}]
                },
            },
        )
    _json(
        cell / "result.json",
        {
            "dataset_id": "toy-data",
            "task_id": "toy-task",
            "turn": 1,
            "requested_model_route": "toy/model",
            "served_provider": "toy",
            "served_model": "model",
            "attestation_ok": True,
            "hide_tests": True,
            "llm_stdout_sha256": _sha(response),
            "llm_solve_sha256": _sha(submission),
            "executed_solve_sha256": _sha(submission),
            "original_status": "measured",
            "repaired_status": "measured",
            "original_reward": 1.0,
            "repaired_reward": 0.0,
            "verified_differential": True,
        },
    )
    return root, cell


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"attestation_ok": False}, "unattested"),
        ({"served_model": "fallback"}, "served_route_mismatch"),
        ({"repaired_reward": 1.0}, "not_differential"),
    ],
)
def test_real_exporter_gates_reject_outcome_flag_without_known_hack(
    ctx: BankContext, change: dict[str, Any], gate: str
) -> None:
    root, cell = _retained_cell(ctx)
    result = cell / "result.json"
    _json(result, {**json.loads(result.read_text()), **change})
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[root])
    row = receipt["sources"]["seeker"]
    assert row["status"] == "refreshed"
    assert row["verified_witness"]["confirmed"] == 0
    assert row["verified_witness"]["rejected_by_gate"] == {gate: 1}
    assert list(_bank(ctx).iter_items(["seeker"])) == []


def test_exporter_reward_differential_without_exact_native_bindings_fails_closed(
    ctx: BankContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.production import cloud_trace_db

    root, _ = _retained_cell(ctx)

    def forbidden_database(*_: Any, **__: Any) -> None:
        raise AssertionError("refresh constructed a production database writer")

    monkeypatch.setattr(cloud_trace_db, "CloudTraceDatabase", forbidden_database)
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[root])
    row = receipt["sources"]["seeker"]
    assert row["status"] == "source-not-ready"
    witness = row["verified_witness"]
    assert witness["exporter_confirmed"] == 1
    assert witness["confirmed"] == 0
    assert "distinct full original/repaired package digests" in witness["missing_witness_bindings"]
    assert (
        "original native receipt candidate_digest bound to the executed candidate"
        in witness["missing_witness_bindings"]
    )
    assert list(_bank(ctx).iter_items(["seeker"])) == []
    assert not (ctx.prod_live / "seeker-known-hacks").exists()


def test_unknown_non_actor_seeker_receipt_is_never_automatically_clean(ctx: BankContext) -> None:
    labels = ctx.prod_live / seeker.LABELS_RELATIVE
    _jsonl(
        labels,
        [
            {
                "schema": seeker.LABEL_SCHEMA,
                "run_id": "non-actor",
                "task_id": "toy-task",
                "source_label": {"judged": True, "verdict": "no"},
                "admission": {"effective_split": "dev"},
            }
        ],
    )
    before = labels.read_bytes()
    receipt = refresh.refresh_once(ctx, seeker_cells_roots=[])
    assert receipt["sources"]["seeker"]["status"] == "refreshed"
    assert list(_bank(ctx).iter_items(["seeker"])) == []
    assert labels.read_bytes() == before


def test_writer_lock_is_reentrant_and_shared_by_manifest_across_state_roots(
    ctx: BankContext,
) -> None:
    first = _bank(ctx)
    second = IssueBank(ctx.repo_root, state_root=ctx.state_root.with_name("other-state"))
    attempting, entered = threading.Event(), threading.Event()

    def contender() -> None:
        attempting.set()
        with refresh.bank_writer_lock(second):
            entered.set()

    thread = threading.Thread(target=contender, daemon=True)
    with refresh.bank_writer_lock(first), refresh.bank_writer_lock(first):
        thread.start()
        assert attempting.wait(2)
        assert not entered.wait(0.05)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert entered.is_set()


def test_once_cli_reports_source_not_ready_with_nonzero_exit(
    ctx: BankContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _findings(ctx, [_finding()]).write_text('{"partial":', encoding="utf-8")
    monkeypatch.setenv("EVALLAB_PROD_LIVE_ROOT", str(ctx.prod_live))
    monkeypatch.setenv("EVALLAB_CLOUD_TRACE_DB", str(ctx.cloud_db))
    monkeypatch.setenv("EVALLAB_CROSS_TEST_REGISTRY", str(ctx.cross_test_registry))
    parser = argparse.ArgumentParser()
    bank_refresh.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["bank-refresh", "--once", "--state-root", str(ctx.state_root)])
    assert args.func(args, ctx.repo_root) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["sources"]["run-health"]["status"] == "source-not-ready"


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf"])
def test_watch_rejects_invalid_interval(interval: str) -> None:
    parser = argparse.ArgumentParser()
    bank_refresh.register(parser.add_subparsers(required=True))
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["bank-refresh", "--watch", "--interval", interval])
    assert error.value.code == 2
