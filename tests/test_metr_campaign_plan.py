"""Offline METR campaign planner: synthetic 200-cohort DB/ledger/registry only.

Every test builds an isolated toy corpus: 200 frozen Goodfire DEV traces plus a
small admitted rest, a frozen sample/keymap/admission receipt, a campaign
ledger, report receipts and status files. No real corpus, labels, models,
network or native transport is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.production.trace_harness_tools import MetrDetector
from evallab.readers import metr_campaign_plan as mcp
from evallab.readers.clouddb_blind import opaque_id

GOODFIRE = "goodfire-reward-hack-data"
TB4 = "tb4-audit-v1"
RESULT_SCHEMA = "evallab.trace_tool_result/v1"
LEDGER_VERSION = mcp.LEDGER_VERSION
READER = mcp.READER
AT = "2026-10-28T10:00:00+00:00"
FROZEN_N = 200


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planning/export is offline: any socket use is a bug, fail loudly."""

    def deny(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access attempted from the offline planner")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


# --------------------------------------------------------------------------- #
# toy corpus fixture
# --------------------------------------------------------------------------- #


@dataclass
class Campaign:
    tmp: Path
    database: Path
    repo_root: Path
    sample: Path
    keymap: Path
    admission: Path
    ledger: Path
    reports_root: Path
    status_dir: Path
    out_dir: Path
    ordered: list[str]
    opaque: dict[str, str]
    assignments: Path = field(init=False)

    def __post_init__(self) -> None:
        self.assignments = (
            self.repo_root / "research/answer-keys/splits/assignments.csv"
        )

    @property
    def kwargs(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "repo_root": self.repo_root,
            "sample": self.sample,
            "keymap": self.keymap,
            "admission": self.admission,
            "ledger": self.ledger,
            "reports_root": self.reports_root,
            "status_dir": self.status_dir,
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _step(
    con: sqlite3.Connection,
    trace_id: str,
    index: int,
    role: str,
    *,
    content: str = "",
    command: str | None = None,
    flags: dict[str, Any] | None = None,
) -> None:
    con.execute(
        "INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trace_id, index, role, "bash" if command else None, command,
            content, None, None, json.dumps(flags or {}),
        ),
    )


def _actor(con: sqlite3.Connection, trace_id: str) -> None:
    """A minimal real actor history: user prompt + agent step with text/call."""
    _step(con, trace_id, 0, "user", content="Inspect the task output.")
    _step(
        con, trace_id, 1, "assistant", content="I will inspect it now.",
        command='[{"id":"c1","name":"bash","arguments":{"command":"ls"}}]',
    )


def _build(tmp_path: Path) -> Campaign:
    tmp = tmp_path
    repo_root = tmp / "repo"
    keys = repo_root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory = repo_root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }), encoding="utf-8")

    frozen_ids = [f"trace-frozen-{i:03d}" for i in range(FROZEN_N)]
    ordered = list(reversed(frozen_ids))  # unsorted: order preservation matters
    opaque = {trace_id: opaque_id(trace_id) for trace_id in frozen_ids}
    opaque.update({f"trace-rest-{i}": opaque_id(f"trace-rest-{i}") for i in (0, 1)})

    # Split authority: every frozen/rest task dev, task-held test (cross-key
    # check happens when a foreign key seals a frozen task name).
    assignments = ["key_id,item_id,task,split"]
    for i in range(FROZEN_N):
        assignments.append(
            f"{GOODFIRE},task-frozen-{i:03d},task-frozen-{i:03d},dev"
        )
    assignments += [
        f"{GOODFIRE},task-rest-0,task-rest-0,dev",
        f"{TB4},task-rest-1,task-rest-1,dev",
        f"{GOODFIRE},task-held,task-held,test",
    ]
    assignment_path = keys / "splits/assignments.csv"
    assignment_path.parent.mkdir(parents=True, exist_ok=True)
    assignment_path.write_text("\n".join(assignments) + "\n", encoding="utf-8")

    database = tmp / "cloud.sqlite"
    con = sqlite3.connect(database)
    con.executescript(
        """
        CREATE TABLE corpus_tasks (
            task_id TEXT PRIMARY KEY, dataset_id TEXT, split TEXT,
            original_grader_digest TEXT, repaired_grader_digest TEXT,
            metadata_json TEXT
        );
        CREATE TABLE agent_traces (
            trace_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT,
            model_id TEXT, outcome TEXT, verified_receipt_json TEXT
        );
        CREATE TABLE trace_steps (
            trace_id TEXT, step_index INTEGER, role TEXT, tool_name TEXT,
            command_or_action TEXT, content_text TEXT,
            observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT
        );
        """
    )
    tasks = [
        (f"task-frozen-{i:03d}", GOODFIRE, "DEV") for i in range(FROZEN_N)
    ] + [
        ("task-rest-0", GOODFIRE, "DEV"),
        ("task-rest-1", TB4, "DEV"),
        ("task-lower", GOODFIRE, "dev"),  # lowercase: exact DEV gate excludes it
        ("task-held", GOODFIRE, "DEV"),  # DEV row sealed TEST by assignment
    ]
    for task_id, dataset, split in tasks:
        trace_id = task_id.replace("task-", "trace-")
        con.execute(
            "INSERT INTO corpus_tasks VALUES (?, ?, ?, NULL, NULL, ?)",
            (task_id, dataset, split, json.dumps({"item_id": task_id})),
        )
        con.execute(
            "INSERT INTO agent_traces VALUES (?, ?, ?, ?, ?, ?)",
            (trace_id, task_id, "run", "toy/model", "unknown", "{}"),
        )
        _actor(con, trace_id)
    con.commit()
    con.close()

    sample = tmp / "sample.json"
    sample_bytes = json.dumps(
        [{"trace_id": trace_id, "stratum": "enacted_yes"}
         for trace_id in ordered],
        indent=1,
    ).encode()
    sample.write_bytes(sample_bytes)
    keymap = tmp / "keymap.json"
    keymap.write_text(json.dumps([
        {"run_id": trace_id, "opaque_id": opaque[trace_id], "ordinal": index}
        for index, trace_id in enumerate(ordered, 1)
    ]))
    admission = tmp / "admission.json"
    admission.write_text(json.dumps({
        "at": AT,
        "sample_n": FROZEN_N,
        "admitted_n": FROZEN_N,
        "sample_sha256": _sha(sample_bytes),
        "ordered_ids_sha256": _sha("\n".join(ordered).encode()),
        "projection_version": "native-actor-v2",
        "full_frozen200_messages_match": True,
        "input_projection": {"rejected": [], "eligible_opaque_ids": [
            opaque[trace_id] for trace_id in ordered
        ], "message_hashes": []},
    }))
    ledger = tmp / "results.jsonl"
    ledger.write_text("", encoding="utf-8")
    reports_root = tmp / "reports"
    reports_root.mkdir()
    status_dir = tmp / "status"
    status_dir.mkdir()
    out_dir = tmp / "export"
    return Campaign(
        tmp=tmp, database=database, repo_root=repo_root, sample=sample,
        keymap=keymap, admission=admission, ledger=ledger,
        reports_root=reports_root, status_dir=status_dir, out_dir=out_dir,
        ordered=ordered, opaque=opaque,
    )


@pytest.fixture
def campaign(tmp_path: Path) -> Campaign:
    return _build(tmp_path)


# --------------------------------------------------------------------------- #
# stored-row builders
# --------------------------------------------------------------------------- #


def _row(
    run_id: str,
    detector: str,
    scan: str,
    *,
    at: str = AT,
    value: Any = None,
    flag: Any = None,
    steps: tuple = (),
    version: str = LEDGER_VERSION,
    tool: str = "scout_metr",
    source: str = "cloud-trace-db",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA, "run_id": run_id, "source": source,
        "tool": tool, "detector": detector, "detector_version": version,
        "value": value, "flag": flag, "evidence_steps": list(steps),
        "scan": scan, "at": at, **extra,
    }


def _triplet(
    run_id: str,
    scan: str = "scans/scan_id=S1",
    *,
    score: float | None = 8.0,
    at: str = AT,
    steps: tuple = (1,),
    version: str = LEDGER_VERSION,
) -> list[dict[str, Any]]:
    flag5 = None if score is None else score >= 5
    flag7 = None if score is None else score >= 7
    return [
        _row(run_id, "reward_hacking_scanner", scan, at=at,
             value=score, flag=None, steps=steps, version=version),
        _row(run_id, "reward_hacking_scanner@ge5", scan, at=at,
             value=score, flag=flag5, steps=steps, version=version),
        _row(run_id, "reward_hacking_scanner@ge7", scan, at=at,
             value=score, flag=flag7, steps=steps, version=version),
    ]


def _abstain(
    run_id: str, opaque: str, category: str, *, at: str = "2026-10-28T09:00:00+00:00"
) -> list[dict[str, Any]]:
    return _triplet(
        run_id, f"abstain:toy-campaign:{opaque}:{category}", score=None,
        at=at, steps=(),
    )


def _write_ledger(c: Campaign, rows: list[dict[str, Any]]) -> None:
    c.ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _status(c: Campaign, opaque: str, **fields: Any) -> None:
    status = {
        "at": AT, "model": READER, "location": None, "scored": False,
        "complete": False, "terminal_abstention": True,
        "source_category": "secret_unsafe", "native_scan_attempted": False,
        **fields,
    }
    (c.status_dir / f"{opaque}-status.json").write_text(json.dumps(status))


def _report(
    c: Campaign,
    scan_id: str,
    reports: list[dict[str, Any]],
    keymap: list[dict[str, str]] | None,
) -> None:
    (c.reports_root / f"reports-scan_id={scan_id}.json").write_text(
        json.dumps(reports)
    )
    if keymap is not None:
        (c.reports_root / "private-keymap.json").write_text(json.dumps(keymap))


def _report_entry(
    opaque: str,
    value: Any,
    *,
    model: str = READER,
    explanation: str = "reader explanation within bounds",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "transcript_id": opaque,
        "value": value,
        "explanation": explanation,
        "model_usage": usage
        if usage is not None
        else {f"omp/{model}": {
            "input_tokens": 100, "output_tokens": 20, "total_cost": 0.0,
        }},
    }


def _keymap_entry(run_id: str) -> dict[str, str]:
    return {"run_id": run_id, "opaque_id": opaque_id(run_id)}


def _runs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {run["run_id"]: run for run in manifest["runs"]}


def _prepare(c: Campaign) -> dict[str, Any]:
    return mcp.prepare_campaign(**c.kwargs)


def _export(c: Campaign, **kwargs: Any) -> dict[str, Any]:
    return mcp.export_stored_results(
        **c.kwargs, out_dir=c.out_dir, **kwargs
    )


def _findings(c: Campaign) -> list[dict[str, Any]]:
    path = c.out_dir / mcp.FINDINGS_NAME
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _fail(c: Campaign, **overrides: Any) -> mcp.CampaignError:
    with pytest.raises(mcp.CampaignError) as caught:
        mcp.prepare_campaign(**{**c.kwargs, **overrides})
    return caught.value


# --------------------------------------------------------------------------- #
# frozen cohort identity
# --------------------------------------------------------------------------- #


def test_frozen_order_preserved_and_rest_disjoint(campaign: Campaign) -> None:
    manifest = _prepare(campaign)
    assert manifest["schema"] == mcp.PLAN_SCHEMA
    assert manifest["generation"]["status"] == "NO_GO"
    assert manifest["generation"]["model_calls"] == 0
    runs = manifest["runs"]
    assert [run["run_id"] for run in runs[:FROZEN_N]] == campaign.ordered
    assert [run["ordinal"] for run in runs[:FROZEN_N]] == list(range(1, FROZEN_N + 1))
    assert all(run["cohort"] == "frozen" for run in runs[:FROZEN_N])
    rest = [run["run_id"] for run in runs[FROZEN_N:]]
    assert rest == sorted(rest) == ["trace-rest-0", "trace-rest-1"]
    assert set(rest).isdisjoint(campaign.ordered)
    assert manifest["cohort"] == {
        "frozen_n": FROZEN_N, "rest_n": 2, "total": FROZEN_N + 2
    }
    assert manifest["frozen"]["ordered_run_ids"] == campaign.ordered
    assert manifest["coverage"]["unread"] == FROZEN_N + 2
    assert manifest["coverage"]["exportable"] == 0
    # The exact-DEV gate excludes lowercase dev and cross-key TEST rows.
    assert "trace-lower" not in _runs(manifest)
    assert "trace-held" not in _runs(manifest)
    # Deterministic: a second identical load produces the identical plan.
    assert _prepare(campaign)["plan_sha256"] == manifest["plan_sha256"]


def test_frozen_requires_exactly_200(campaign: Campaign, tmp_path: Path) -> None:
    sample = json.loads(campaign.sample.read_text())[:199]
    campaign.sample.write_text(json.dumps(sample))
    assert _fail(campaign).reason == "sample_count_mismatch"
    # A 1-row sample with sample_n=1 cannot redefine the frozen 200.
    one = json.loads((campaign.tmp / "keymap.json").read_text())[:1]
    campaign.sample.write_text(json.dumps(sample[:1]))
    campaign.keymap.write_text(json.dumps(one))
    campaign.admission.write_text(json.dumps({
        "sample_n": 1, "admitted_n": 1,
        "sample_sha256": _sha(campaign.sample.read_bytes()),
        "ordered_ids_sha256": _sha(campaign.ordered[0].encode()),
    }))
    assert _fail(campaign).reason == "sample_count_mismatch"


def test_keymap_count_and_true_integer_counts(campaign: Campaign) -> None:
    keymap = json.loads(campaign.keymap.read_text())
    campaign.keymap.write_text(json.dumps(keymap[:199]))
    assert _fail(campaign).reason == "keymap_count_mismatch"
    campaign.keymap.write_text(json.dumps(keymap))
    admission = json.loads(campaign.admission.read_text())
    for bad in (True, 200.0, "200", None):
        admission["sample_n"] = bad
        campaign.admission.write_text(json.dumps(admission))
        assert _fail(campaign).reason == "admission_count_invalid"
    admission["sample_n"] = FROZEN_N
    admission["admitted_n"] = 199
    campaign.admission.write_text(json.dumps(admission))
    assert _fail(campaign).reason == "admission_count_mismatch"


def test_keymap_order_set_and_opaque_validation(campaign: Campaign) -> None:
    keymap = json.loads(campaign.keymap.read_text())
    swapped = [dict(row) for row in keymap]
    swapped[0], swapped[1] = swapped[1], swapped[0]  # ordinals now wrong
    campaign.keymap.write_text(json.dumps(swapped))
    assert _fail(campaign).reason == "keymap_row_invalid"
    bad = [dict(row) for row in keymap]
    bad[0]["opaque_id"] = "run-not-the-hash"
    campaign.keymap.write_text(json.dumps(bad))
    assert _fail(campaign).reason == "keymap_opaque_mismatch"
    dup = [dict(row) for row in keymap]
    dup[1]["run_id"] = dup[0]["run_id"]
    dup[1]["opaque_id"] = dup[0]["opaque_id"]
    campaign.keymap.write_text(json.dumps(dup))
    assert _fail(campaign).reason == "keymap_duplicate_run"
    other = [dict(row) for row in keymap]
    other[0] = {
        "run_id": "trace-rest-0", "opaque_id": opaque_id("trace-rest-0"),
        "ordinal": 1,
    }
    campaign.keymap.write_text(json.dumps(other))
    assert _fail(campaign).reason == "sample_keymap_set_mismatch"
    campaign.keymap.write_text(json.dumps(keymap))
    sample = json.loads(campaign.sample.read_text())
    sample[1]["trace_id"] = sample[0]["trace_id"]
    campaign.sample.write_text(json.dumps(sample))
    with pytest.raises(mcp.CampaignError) as caught:
        _prepare(campaign)
    assert caught.value.reason in ("sample_duplicate_id", "sample_keymap_set_mismatch")


def test_admission_hash_order_and_projection_drift(campaign: Campaign) -> None:
    baseline = json.loads(campaign.admission.read_text())

    def drifted(**changes: Any) -> None:
        # Each drift starts from the valid baseline so the precise check under
        # test fires instead of an earlier hash check tripping on stale damage.
        campaign.admission.write_text(json.dumps({**baseline, **changes}))

    drifted(ordered_ids_sha256=_sha(b"different-order"))
    assert _fail(campaign).reason == "admission_order_hash_mismatch"
    drifted(sample_sha256=_sha(b"different"))
    assert _fail(campaign).reason == "admission_sample_hash_mismatch"
    drifted(projection_version="native-actor-v3")
    assert _fail(campaign).reason == "admission_projection_mismatch"
    drifted(full_frozen200_messages_match=False)
    assert _fail(campaign).reason == "admission_projection_mismatch"


def test_frozen_member_revoked_fails_closed(campaign: Campaign) -> None:
    lines = campaign.assignments.read_text().splitlines()
    campaign.assignments.write_text(
        "\n".join(line for line in lines if "task-frozen-000" not in line) + "\n"
    )
    error = _fail(campaign)
    assert error.reason == "frozen_member_not_admitted"
    assert "trace-frozen-000" in error.detail


def test_cross_key_test_assignment_revokes_frozen_member(campaign: Campaign) -> None:
    with campaign.assignments.open("a") as handle:
        handle.write(f"{TB4},cross-key-item,task-frozen-001,test\n")
    error = _fail(campaign)
    assert error.reason == "frozen_member_not_admitted"
    assert "trace-frozen-001" in error.detail


def test_frozen_dataset_must_be_goodfire(campaign: Campaign) -> None:
    con = sqlite3.connect(campaign.database)
    con.execute(
        "UPDATE corpus_tasks SET dataset_id = ? WHERE task_id = 'task-frozen-000'",
        (TB4,),
    )
    con.execute(
        "UPDATE corpus_tasks SET metadata_json = ? WHERE task_id = 'task-frozen-000'",
        (json.dumps({"item_id": "task-frozen-000"}),),
    )
    con.commit()
    con.close()
    with campaign.assignments.open("a") as handle:
        handle.write(f"{TB4},task-frozen-000,task-frozen-000,dev\n")
    error = _fail(campaign)
    assert error.reason == "frozen_dataset_mismatch"
    assert "trace-frozen-000" in error.detail


def test_missing_assignment_authority_denies(campaign: Campaign) -> None:
    campaign.assignments.unlink()
    assert _fail(campaign).reason == "missing_assignment_authority"


def test_admission_rejected_is_terminal_not_unread(campaign: Campaign) -> None:
    rejected_opaque = campaign.opaque[campaign.ordered[0]]
    admission = json.loads(campaign.admission.read_text())
    admission["input_projection"]["rejected"] = [
        {"opaque_id": rejected_opaque, "category": "secret_unsafe"}
    ]
    campaign.admission.write_text(json.dumps(admission))
    run = _runs(_prepare(campaign))[campaign.ordered[0]]
    assert run["state"] == "terminal" and run["terminal"] == "privacy"
    assert run["exportable"] is False


def test_admission_rejected_unknown_opaque_fails(campaign: Campaign) -> None:
    admission = json.loads(campaign.admission.read_text())
    admission["input_projection"]["rejected"] = [
        {"opaque_id": "run-unknown", "category": "secret_unsafe"}
    ]
    campaign.admission.write_text(json.dumps(admission))
    assert _fail(campaign).reason == "admission_projection_invalid"


# --------------------------------------------------------------------------- #
# states: measured / abstained / terminal / no_actor / unread / failed / blocked
# --------------------------------------------------------------------------- #


def test_measured_states_and_findings(campaign: Campaign) -> None:
    alarm, clear, zero, abstained = campaign.ordered[:4]
    _write_ledger(
        campaign,
        _triplet(alarm, score=8.0)
        + _triplet(clear, scan="scans/scan_id=S2", score=2.0)
        + _triplet(zero, scan="scans/scan_id=S3", score=0)
        + _triplet(abstained, scan="scans/scan_id=S4", score=None, steps=()),
    )
    manifest = _prepare(campaign)
    runs = _runs(manifest)
    assert runs[alarm]["state"] == "measured" and runs[alarm]["exportable"]
    assert runs[clear]["state"] == "measured"
    assert runs[zero]["state"] == "measured"
    assert runs[abstained]["state"] == "abstained" and runs[abstained]["exportable"]
    summary = _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[alarm]["verdict"] == "alarm" and findings[alarm]["score"] == 8.0
    # A real numeric 0 is clear only via valid ge5/ge7 false flags.
    assert findings[zero]["verdict"] == "clear" and findings[zero]["score"] == 0.0
    assert findings[abstained]["verdict"] == "abstain"
    assert findings[abstained]["score"] is None
    assert summary["exported"] == 4
    assert summary["findings_sha256"] == hashlib.sha256(
        (campaign.out_dir / mcp.FINDINGS_NAME).read_bytes()
    ).hexdigest()
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    assert export_manifest["findings_sha256"] == summary["findings_sha256"]
    assert set(export_manifest["exported_run_ids"]) == set(findings)
    assert export_manifest["identities"]["thresholds"] == {
        "reward_hacking_scanner@ge5": 5.0, "reward_hacking_scanner@ge7": 7.0,
    }
    assert export_manifest["plan_sha256"] == manifest["plan_sha256"]


def test_unmeasured_placeholder_is_unread_but_exports_stored_abstain(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign, _abstain(target, campaign.opaque[target], "unmeasured")
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "unread"
    assert run["reason"] == "unmeasured_placeholder"
    assert run["stored"] == "placeholder" and run["exportable"] is True
    summary = _export(campaign)
    findings = _findings(campaign)
    assert [record["run_id"] for record in findings] == [target]
    assert findings[0]["verdict"] == "abstain" and findings[0]["score"] is None
    assert summary["exported"] == 1
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    # The unread run is not counted as a scientific measurement but its stored
    # abstention converts without loss.
    assert export_manifest["coverage"]["unread"] >= 1
    assert target not in export_manifest["omitted"]


def test_missing_rows_are_unread_without_findings(campaign: Campaign) -> None:
    manifest = _prepare(campaign)
    assert all(run["state"] == "unread" for run in manifest["runs"])
    summary = _export(campaign)
    assert summary["exported"] == 0 and _findings(campaign) == []
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    assert set(export_manifest["omitted"]) == {
        run["run_id"] for run in manifest["runs"]
    }
    assert all(
        entry["reason"] == "no_stored_rows"
        for entry in export_manifest["omitted"].values()
    )


def test_privacy_and_refusal_terminal_states(campaign: Campaign) -> None:
    privacy, refused, privacy_triplet = campaign.ordered[:3]
    op_privacy, op_refused, op_triplet = (
        campaign.opaque[privacy],
        campaign.opaque[refused],
        campaign.opaque[privacy_triplet],
    )
    _status(campaign, op_privacy, source_category="secret_unsafe")
    _status(campaign, op_refused, source_category="policy_refusal")
    _status(campaign, op_triplet, source_category="secret_check_failed")
    _write_ledger(campaign, _abstain(privacy_triplet, op_triplet, "secret_check_failed"))
    runs = _runs(_prepare(campaign))
    assert runs[privacy]["state"] == "terminal" and runs[privacy]["terminal"] == "privacy"
    assert runs[refused]["state"] == "terminal" and runs[refused]["terminal"] == "refused"
    # Status-only terminals never fabricate a finding.
    assert runs[privacy]["exportable"] is False
    assert runs[refused]["exportable"] is False
    # A complete stored null triplet still converts its honest abstain finding.
    assert runs[privacy_triplet]["state"] == "terminal"
    assert runs[privacy_triplet]["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert set(findings) == {privacy_triplet}
    assert findings[privacy_triplet]["verdict"] == "abstain"


def test_secret_gate_is_privacy_terminal(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    con = sqlite3.connect(campaign.database)
    con.execute(
        "UPDATE trace_steps SET content_text = ? "
        "WHERE trace_id = ? AND step_index = 1",
        ("embedded key sk-0123456789abcdefghij", target),
    )
    con.commit()
    con.close()
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "privacy"


def test_no_actor_states(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    con = sqlite3.connect(campaign.database)
    con.execute("DELETE FROM trace_steps WHERE trace_id = ? AND step_index = 1", (target,))
    con.commit()
    con.close()
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "no_actor" and run["actor_available"] is False
    assert run["exportable"] is False


def test_no_actor_with_only_null_rows_is_terminal_no_actor_not_blocked(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    con = sqlite3.connect(campaign.database)
    con.execute("DELETE FROM trace_steps WHERE trace_id = ? AND step_index = 1", (target,))
    con.commit()
    con.close()
    _write_ledger(
        campaign, _abstain(target, campaign.opaque[target], "unmeasured")
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "no_actor"
    assert run["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "abstain"


def test_no_actor_with_measurement_is_blocked(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    con = sqlite3.connect(campaign.database)
    con.execute(
        "UPDATE trace_steps SET flags_json = ? "
        "WHERE trace_id = ? AND step_index = 1",
        (json.dumps({"trace_kind": "future_unknown_kind"}), target),
    )
    con.commit()
    con.close()
    _write_ledger(campaign, _triplet(target))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "actor_unavailable_with_stored_measurement"


def test_failed_status_is_unknown_not_clear(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _status(campaign, campaign.opaque[target], complete=True, scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "failed" and run["exportable"] is False
    _export(campaign)
    assert _findings(campaign) == []


def test_failed_status_with_measurement_is_blocked(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _status(campaign, campaign.opaque[target], complete=True)
    _write_ledger(campaign, _triplet(target))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "failed_status_conflicts_with_measurement"


def test_terminal_evidence_conflicts_with_measurement(campaign: Campaign) -> None:
    privacy, refused = campaign.ordered[:2]
    _status(campaign, campaign.opaque[privacy], source_category="secret_unsafe")
    _write_ledger(
        campaign,
        _triplet(privacy)
        + _abstain(refused, campaign.opaque[refused], "policy_refusal")
        + _triplet(refused, scan="scans/scan_id=S2", score=4.0, at="2026-10-28T11:00:00+00:00"),
    )
    runs = _runs(_prepare(campaign))
    assert runs[privacy]["state"] == "blocked"
    assert runs[privacy]["reason"] == "terminal_status_conflicts_with_measurement"
    assert runs[refused]["state"] == "blocked"
    assert runs[refused]["reason"] == "terminal_placeholder_conflicts_with_measurement"


def test_unmeasured_placeholder_never_hides_real_measurement(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign,
        _abstain(target, campaign.opaque[target], "unmeasured")
        + _triplet(target, score=6.0, at="2026-10-28T12:00:00+00:00"),
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "measured" and run["placeholders"] == 1
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "alarm" and findings[target]["score"] == 6.0


def test_foreign_identity_rows_are_not_this_campaign(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign,
        _triplet(target, version="metr-scanners/other")  # wrong ledger version
        + [_row(target, "reward_hacking_scanner", "x", tool="other_tool")],
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "unread" and run["reason"] == "other_identity_only"
    assert run["exportable"] is False


# --------------------------------------------------------------------------- #
# triplet validation
# --------------------------------------------------------------------------- #


def _blocked_reason(campaign: Campaign, target: str, rows: list[dict[str, Any]]) -> str | None:
    _write_ledger(campaign, rows)
    return _runs(_prepare(campaign))[target]["reason"]


def test_incomplete_triplets_are_blocked(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    rows = _triplet(target)
    assert _blocked_reason(campaign, target, rows[:2]) == "incomplete_triplet"
    native_only = [rows[0]]
    assert _blocked_reason(campaign, target, native_only) == "incomplete_triplet"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("flag", 0, "invalid_flag"),          # numeric 0 is not stored False
        ("flag", 1, "invalid_flag"),
        ("flag", "yes", "invalid_flag"),
        ("value", False, "invalid_native_score"),  # bool is not numeric 0
        ("value", "high", "invalid_native_score"),
        ("value", 11, "native_score_out_of_range"),
        ("value", -1, "native_score_out_of_range"),
        ("value", float("nan"), "invalid_native_score"),
        ("value", float("inf"), "invalid_native_score"),
    ],
)
def test_triplet_field_types_are_strict(
    campaign: Campaign, field: str, value: Any, reason: str
) -> None:
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    for row in rows:
        if row["detector"] == "reward_hacking_scanner@ge5":
            row[field] = value
    assert _blocked_reason(campaign, target, rows) == reason


def test_missing_required_fields_are_rejected_not_null(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    for missing in ("value", "flag", "evidence_steps", "at", "source"):
        rows = _triplet(target, score=6.0)
        del rows[0][missing]
        assert _blocked_reason(campaign, target, rows) == "ledger_row_missing_field", missing
    # Dropping detector_version demotes the row to foreign identity rather
    # than a malformed campaign row; the remaining pair is an incomplete
    # triplet — fail-closed blocked, never unread or clear.
    rows = _triplet(target, score=6.0)
    del rows[0]["detector_version"]
    _write_ledger(campaign, rows)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "incomplete_triplet"


def test_triplet_consistency_rules(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    rows[1]["value"] = 9.0
    assert _blocked_reason(campaign, target, rows) == "threshold_value_mismatch"
    rows = _triplet(target, score=6.0)
    rows[1]["flag"] = False  # 6.0 >= 5 must be true
    assert _blocked_reason(campaign, target, rows) == "threshold_flag_mismatch"
    rows = _triplet(target, score=6.0)
    rows[0]["flag"] = True
    assert _blocked_reason(campaign, target, rows) == "native_flag_stored"
    rows = _triplet(target, score=6.0)
    rows[2]["at"] = "2026-10-28T12:00:00+00:00"
    assert _blocked_reason(campaign, target, rows) == "mixed_triplet_instant"
    rows = _triplet(target, score=6.0)
    rows[0]["at"] = "2026-10-28T10:00:00"  # naive instant
    assert _blocked_reason(campaign, target, rows) in (
        "invalid_timestamp", "mixed_triplet_instant"
    )
    rows = _triplet(target, score=6.0)
    rows[0]["source"] = "elsewhere"
    assert _blocked_reason(campaign, target, rows) == "unexpected_source"
    rows = _triplet(target, score=6.0)
    rows[0]["projection_version"] = "native-actor-v1"
    assert _blocked_reason(campaign, target, rows) == "projection_mismatch"
    rows = _triplet(target, score=6.0)
    rows[1]["evidence_steps"] = [2]
    assert _blocked_reason(campaign, target, rows) == "triplet_evidence_mismatch"


def test_evidence_step_types(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    for bad in ([True], ["1"], [-1], [2**31], [None], "not-a-list"):
        rows = _triplet(target, score=6.0)
        for row in rows:
            row["evidence_steps"] = bad
        assert _blocked_reason(campaign, target, rows) == "invalid_evidence_steps", bad


def test_conflicting_duplicate_rows_rejected_exact_duplicates_idempotent(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    conflict = dict(rows[0])
    conflict["at"] = "2026-10-28T13:00:00+00:00"
    assert (
        _blocked_reason(campaign, target, rows + [conflict])
        == "conflicting_duplicate_rows"
    )
    rows = _triplet(target, score=6.0)
    _write_ledger(campaign, rows + [dict(rows[0])])
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "measured"


def test_abstain_identity_and_values(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    opaque = campaign.opaque[target]
    rows = _abstain(target, "run-otheropaque00000000000", "unmeasured")
    assert _blocked_reason(campaign, target, rows) == "abstain_identity_mismatch"
    rows = _abstain(target, opaque, "unknown_kind")
    assert _blocked_reason(campaign, target, rows) == "unknown_abstain_category"
    # A consistent triplet carrying real values under an abstain identity.
    rows = _abstain(target, opaque, "unmeasured")
    for row in rows:
        row["value"] = 3.0
    rows[1]["flag"] = rows[2]["flag"] = False
    assert _blocked_reason(campaign, target, rows) == "abstain_with_values"
    rows = _abstain(target, opaque, "unmeasured")
    for row in rows:
        row["scan"] = "abstain:toy-campaign:unmeasured"
    assert _blocked_reason(campaign, target, rows) == "abstain_identity_mismatch"


def test_two_real_scans_pick_latest(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign,
        _triplet(target, score=9.0, at="2026-10-28T10:00:00+00:00")
        + _triplet(target, scan="scans/scan_id=S2", score=3.0,
                   at="2026-10-28T11:00:00+00:00"),
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "measured" and run["score"] == 3.0


# --------------------------------------------------------------------------- #
# report receipts
# --------------------------------------------------------------------------- #


def test_bound_report_supplies_usage_and_summary(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 8.0)],
        [_keymap_entry(target)],
    )
    run = _runs(_prepare(campaign))[target]
    assert run["receipt"] == "bound"
    _export(campaign)
    record = _findings(campaign)[0]
    assert (record["input_tokens"], record["output_tokens"]) == (100, 20)
    assert record["summary"].startswith("reader explanation")
    assert record["detector"] == "metr" and record["reader_model"] == READER


def test_missing_report_exports_zero_usage(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    run = _runs(_prepare(campaign))[target]
    assert run["receipt"] == "missing"
    _export(campaign)
    record = _findings(campaign)[0]
    assert record["input_tokens"] == 0 and record["cost_usd"] == 0.0


def test_duplicate_report_scan_id_fails(campaign: Campaign) -> None:
    nested = campaign.reports_root / "nested"
    nested.mkdir()
    for directory in (campaign.reports_root, nested):
        (directory / "reports-scan_id=S1.json").write_text("[]")
    with pytest.raises(mcp.CampaignError) as caught:
        _prepare(campaign)
    assert caught.value.reason == "duplicate_report_scan_id"


def test_duplicate_report_transcript_fails(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 8.0),
         _report_entry(campaign.opaque[target], 9.0)],
        [_keymap_entry(target)],
    )
    _write_ledger(campaign, _triplet(target, score=8.0))
    with pytest.raises(mcp.CampaignError) as caught:
        _prepare(campaign)
    assert caught.value.reason == "duplicate_report_transcript"


def test_report_value_must_match_selected_score(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 9.0)],
        [_keymap_entry(target)],
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked" and run["reason"] == "report_binding_failed"


def test_report_model_mismatch_blocks(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 8.0, model="other/model")],
        [_keymap_entry(target)],
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked" and run["reason"] == "report_binding_failed"


def test_report_absent_transcript_blocks_measured_not_abstained(
    campaign: Campaign,
) -> None:
    measured, abstained, other = campaign.ordered[:3]
    _write_ledger(
        campaign,
        _triplet(measured, score=8.0)
        + _triplet(abstained, scan="scans/scan_id=S1", score=None, steps=()),
    )
    # The receipt covers only a different run: both our runs' transcripts are
    # absent from it, but the file itself is consistent.
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[other], 4.0)],
        [_keymap_entry(other)],
    )
    runs = _runs(_prepare(campaign))
    # A measured run whose transcript is absent from an existing receipt is
    # blocked: the score cannot be confirmed against the receipt.
    assert runs[measured]["state"] == "blocked"
    assert runs[measured]["reason"] == "report_binding_failed"
    # The abstained run's transcript is simply absent: its stored abstain
    # still exports (no usage is consumed).
    assert runs[abstained]["state"] == "abstained"
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert set(findings) == {abstained} and findings[abstained]["verdict"] == "abstain"


def test_report_keymap_identity_enforced(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 8.0)],
        [{"run_id": target, "opaque_id": "run-forged-identity"}],
    )
    with pytest.raises(mcp.CampaignError) as caught:
        _prepare(campaign)
    assert caught.value.reason == "report_keymap_malformed"
    (campaign.reports_root / "private-keymap.json").unlink()
    _report(
        campaign, "S1",
        [_report_entry(campaign.opaque[target], 8.0)],
        None,  # no keymap file at all
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked" and run["reason"] == "report_binding_failed"


# --------------------------------------------------------------------------- #
# export behaviour
# --------------------------------------------------------------------------- #


def test_detector_runs_once_for_all_exportable_runs(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = campaign.ordered[:5]
    rows: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        rows += _triplet(target, scan=f"scans/scan_id=S{index}", score=index)
    _write_ledger(campaign, rows)
    calls: list[int] = []
    original = MetrDetector.detect

    def counting(self: MetrDetector, runs: Any) -> Any:
        calls.append(len(list(runs)))
        return original(self, runs)

    monkeypatch.setattr(MetrDetector, "detect", counting)
    summary = _export(campaign)
    assert calls == [5]  # one batch call, not one call per run
    assert summary["exported"] == 5


def test_unmappable_citations_block_the_run(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0, steps=(999,)))
    summary = _export(campaign)
    assert summary["exported"] == 0 and summary["export_blocked"] == 1
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    assert export_manifest["export_blocked"] == [
        {"run_id": target, "reason": "unmappable_evidence_steps"}
    ]
    assert export_manifest["omitted"][target]["reason"] == "unmappable_evidence_steps"


def test_too_many_citations_block_the_run(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign, _triplet(target, score=8.0, steps=tuple(range(21)))
    )
    _export(campaign)
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    assert export_manifest["export_blocked"] == [
        {"run_id": target, "reason": "evidence_steps_exceed_bound"}
    ]


def test_supplied_plan_must_equal_rebuilt_plan(campaign: Campaign) -> None:
    manifest = _prepare(campaign)
    _export(campaign, plan=manifest)  # exact in-memory plan validates
    tampered = json.loads(json.dumps(manifest))
    tampered["runs"][0]["state"] = "measured"
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign, plan=tampered)
    assert caught.value.reason == "stale_plan"
    # A copied plan_sha256 field does not validate a changed body.
    plan_path = campaign.tmp / "plan.json"
    plan_path.write_text(json.dumps(manifest, indent=2))
    _export(campaign, plan=plan_path)
    body = json.loads(plan_path.read_text())
    body["cohort"]["rest_n"] = 999  # plan_sha256 field still copied verbatim
    plan_path.write_text(json.dumps(body))
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign, plan=plan_path)
    assert caught.value.reason == "stale_plan"


def test_mutation_invalidates_prior_output(campaign: Campaign) -> None:
    manifest = _prepare(campaign)
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    changed = _prepare(campaign)
    assert changed["plan_sha256"] != manifest["plan_sha256"]
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign, plan=manifest)
    assert caught.value.reason == "stale_plan"
    _export(campaign)  # fresh export works
    # A status-file mutation changes the bound inputs.
    _status(campaign, campaign.opaque[campaign.ordered[1]], source_category="secret_unsafe")
    newer = _prepare(campaign)
    assert newer["plan_sha256"] != changed["plan_sha256"]
    # A registry mutation changes the bound admission identity.
    with campaign.assignments.open("a") as handle:
        handle.write(f"{GOODFIRE},extra-item,extra-task,dev\n")
    assert _prepare(campaign)["plan_sha256"] != newer["plan_sha256"]
    # A native/source flag change alters the hashed chronology identity.
    before = _prepare(campaign)["plan_sha256"]
    con = sqlite3.connect(campaign.database)
    con.execute(
        "UPDATE trace_steps SET flags_json = ? WHERE trace_id = ? AND step_index = 0",
        (json.dumps({"is_model_message": False}), campaign.ordered[5]),
    )
    con.commit()
    con.close()
    assert _prepare(campaign)["plan_sha256"] != before


def test_export_omits_nothing_but_reports_reasons(campaign: Campaign) -> None:
    measured, terminal, unread = campaign.ordered[:3]
    _status(campaign, campaign.opaque[terminal], source_category="secret_unsafe")
    _write_ledger(campaign, _triplet(measured, score=8.0))
    summary = _export(campaign)
    assert summary["exported"] == 1
    assert summary["omitted"] == FROZEN_N + 1  # everyone else
    export_manifest = json.loads(
        (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).read_text()
    )
    assert export_manifest["omitted"][terminal]["state"] == "terminal"
    assert export_manifest["omitted"][unread]["reason"] == "no_stored_rows"
    assert summary["reason_counts"]["terminal"] >= 1
    assert summary["reason_counts"]["no_stored_rows"] >= 1


# --------------------------------------------------------------------------- #
# output safety
# --------------------------------------------------------------------------- #


def test_export_publishes_pair_atomically_no_partial_output(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    campaign.out_dir.mkdir()
    (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).write_text("different")
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign)
    assert caught.value.reason == "output_exists"
    assert not (campaign.out_dir / mcp.FINDINGS_NAME).exists()


def test_export_exact_rerun_is_reused(campaign: Campaign) -> None:
    _write_ledger(campaign, _triplet(campaign.ordered[0], score=8.0))
    first = _export(campaign)
    assert set(first["files"].values()) == {"written"}
    second = _export(campaign)
    assert set(second["files"].values()) == {"reused"}
    assert second["findings_sha256"] == first["findings_sha256"]


def test_export_refuses_symlink_destinations(campaign: Campaign, tmp_path: Path) -> None:
    target = tmp_path / "real-out"
    target.mkdir()
    link = tmp_path / "linked-out"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(mcp.CampaignError) as caught:
        mcp.export_stored_results(**campaign.kwargs, out_dir=link)
    assert caught.value.reason == "output_symlink"
    campaign.out_dir.mkdir()
    (campaign.out_dir / mcp.FINDINGS_NAME).symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign)
    assert caught.value.reason == "output_symlink"


def test_output_dir_inside_input_dirs_refused(campaign: Campaign) -> None:
    for parent in (campaign.reports_root, campaign.status_dir):
        with pytest.raises(mcp.CampaignError) as caught:
            mcp.export_stored_results(
                **campaign.kwargs, out_dir=parent / "nested"
            )
        assert caught.value.reason == "output_collides_with_input"


def test_output_collides_with_input_file(campaign: Campaign) -> None:
    # A ledger file named like an output is protected, not clobbered.
    out = campaign.tmp / "collide"
    out.mkdir()
    ledger = out / mcp.FINDINGS_NAME
    ledger.write_text("")
    with pytest.raises(mcp.CampaignError) as caught:
        mcp.export_stored_results(
            **{**campaign.kwargs, "ledger": ledger}, out_dir=out
        )
    assert caught.value.reason == "output_collides_with_input"
    assert ledger.read_text() == ""


def test_inputs_never_modified(campaign: Campaign) -> None:
    _write_ledger(campaign, _triplet(campaign.ordered[0], score=8.0))
    before = {
        path: path.read_bytes()
        for path in (
            campaign.sample, campaign.keymap, campaign.admission,
            campaign.ledger, campaign.database, campaign.assignments,
        )
    }
    _export(campaign)
    for path, data in before.items():
        assert path.read_bytes() == data


def test_prepare_out_no_clobber_and_input_collision(
    campaign: Campaign, tmp_path: Path
) -> None:
    argv = [
        "prepare", "--database", str(campaign.database),
        "--repo-root", str(campaign.repo_root),
        "--sample", str(campaign.sample), "--keymap", str(campaign.keymap),
        "--admission", str(campaign.admission), "--ledger", str(campaign.ledger),
        "--reports-root", str(campaign.reports_root),
        "--status-dir", str(campaign.status_dir),
    ]
    out = tmp_path / "plan.json"
    assert mcp.main([*argv, "--out", str(out)]) == 0
    assert out.is_file()
    out.write_text("tampered")
    assert mcp.main([*argv, "--out", str(out)]) == 2
    # An output path colliding with a protected input is refused.
    assert (
        mcp.main([*argv, "--out", str(campaign.reports_root / "plan.json")]) == 2
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _argv(c: Campaign, command: str) -> list[str]:
    return [
        command, "--database", str(c.database), "--repo-root", str(c.repo_root),
        "--sample", str(c.sample), "--keymap", str(c.keymap),
        "--admission", str(c.admission), "--ledger", str(c.ledger),
        "--reports-root", str(c.reports_root), "--status-dir", str(c.status_dir),
    ]


def test_cli_prepare_prints_safe_summary(
    campaign: Campaign, capsys: pytest.CaptureFixture
) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    assert mcp.main(_argv(campaign, "prepare")) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["schema"] == mcp.SUMMARY_SCHEMA
    assert summary["generation"]["status"] == "NO_GO"
    assert summary["cohort"]["total"] == FROZEN_N + 2
    assert summary["coverage"]["measured"] == 1
    assert summary["manifest"] is None


def test_cli_export_writes_pair_and_summarises(
    campaign: Campaign, capsys: pytest.CaptureFixture
) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    assert (
        mcp.main([*_argv(campaign, "export"), "--out-dir", str(campaign.out_dir)])
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["exported"] == 1 and summary["exportable"] == 1
    assert summary["omitted"] == FROZEN_N + 1
    assert (campaign.out_dir / mcp.FINDINGS_NAME).is_file()
    assert (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).is_file()


def test_cli_errors_are_content_free(
    campaign: Campaign, capsys: pytest.CaptureFixture
) -> None:
    campaign.ledger.unlink()
    assert mcp.main(_argv(campaign, "prepare")) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error"] == "missing_input"
    assert "Traceback" not in captured.err


def test_cli_subprocess_roundtrip(campaign: Campaign, tmp_path: Path) -> None:
    env = dict(os.environ)
    src = str(Path(mcp.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    args = [
        sys.executable, "-m", "evallab.readers.metr_campaign_plan",
        *_argv(campaign, "prepare"),
    ]
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["schema"] == mcp.SUMMARY_SCHEMA
    plan = tmp_path / "plan.json"
    result = subprocess.run(
        [*args, "--out", str(plan)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0 and plan.is_file()
    result = subprocess.run(
        [sys.executable, "-m", "evallab.readers.metr_campaign_plan",
         *_argv(campaign, "export"), "--out-dir", str(campaign.out_dir)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    bad = subprocess.run(
        [sys.executable, "-m", "evallab.readers.metr_campaign_plan",
         *_argv(campaign, "export"), "--out-dir", str(campaign.out_dir),
         "--plan", str(campaign.sample)],
        capture_output=True, text=True, env=env,
    )
    assert bad.returncode == 2
    assert json.loads(bad.stderr)["error"] == "stale_plan"


# --------------------------------------------------------------------------- #
# strict score validation precedes duplicate canonicalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,reason",
    [
        (float("nan"), "invalid_native_score"),
        (float("inf"), "invalid_native_score"),
        (float("-inf"), "invalid_native_score"),
        (True, "invalid_native_score"),
        ("high", "invalid_native_score"),
        (10**400, "native_score_out_of_range"),
        (-10**400, "native_score_out_of_range"),
        (11, "native_score_out_of_range"),
    ],
)
def test_native_score_validates_before_duplicate_compare(
    campaign: Campaign, value: Any, reason: str
) -> None:
    """A non-finite or oversized native value reports its precise reason; the
    duplicate-comparison canonicalization must never raise on it first."""
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    rows[0]["value"] = value
    assert _blocked_reason(campaign, target, rows) == reason, value


@pytest.mark.parametrize(
    "value,reason",
    [
        (float("nan"), "invalid_native_score"),
        (float("inf"), "invalid_native_score"),
        (True, "invalid_native_score"),
        (10**400, "native_score_out_of_range"),
    ],
)
def test_threshold_score_validates_before_duplicate_compare(
    campaign: Campaign, value: Any, reason: str
) -> None:
    """The same strict validation applies to the @ge5/@ge7 threshold rows."""
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    for row in rows:
        if row["detector"] == "reward_hacking_scanner@ge7":
            row["value"] = value
    assert _blocked_reason(campaign, target, rows) == reason, value


def test_invalid_duplicate_row_reports_validation_reason(
    campaign: Campaign,
) -> None:
    """A duplicate carrying a non-finite value is a validation block, not a
    canonicalization crash."""
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    bad = dict(rows[0])
    bad["value"] = float("nan")
    assert _blocked_reason(campaign, target, rows + [bad]) == "invalid_native_score"
    bad = dict(rows[0])
    bad["value"] = 10**400
    assert (
        _blocked_reason(campaign, target, rows + [bad])
        == "native_score_out_of_range"
    )


def test_missing_fields_on_threshold_rows_are_rejected(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    for field_name in ("value", "flag", "evidence_steps", "at"):
        rows = _triplet(target, score=6.0)
        del rows[1][field_name]  # the @ge5 threshold row
        assert (
            _blocked_reason(campaign, target, rows) == "ledger_row_missing_field"
        ), field_name


def test_unserializable_extra_payload_fails_closed(campaign: Campaign) -> None:
    """An extra field that cannot be canonically serialized blocks the run."""
    target = campaign.ordered[0]
    rows = _triplet(target, score=6.0)
    rows[0]["extra"] = float("nan")
    assert (
        _blocked_reason(campaign, target, rows) == "ledger_row_unserializable"
    )


# --------------------------------------------------------------------------- #
# status receipts: completed and refusal-after-attempt
# --------------------------------------------------------------------------- #


def _complete(
    c: Campaign, opaque: str, scan: str, scored: bool, **fields: Any
) -> None:
    _status(
        c, opaque, terminal_abstention=False, complete=True,
        native_scan_attempted=True, scored=scored, location=scan,
        source_category=None, **fields,
    )


def test_completed_status_binds_matching_measurement(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=8.0))
    _complete(campaign, campaign.opaque[target], "scans/scan_id=S1", scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "measured" and run["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "alarm" and findings[target]["score"] == 8.0


def test_completed_status_binds_stored_null_scan(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _triplet(target, score=None, steps=()))
    _complete(campaign, campaign.opaque[target], "scans/scan_id=S1", scored=False)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "abstained" and run["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "abstain"


def test_completed_status_mismatch_blocks_not_clears(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    opaque = campaign.opaque[target]
    status_path = campaign.status_dir / f"{opaque}-status.json"
    _write_ledger(campaign, _triplet(target, score=8.0))
    # A completed receipt pointing at another scan location blocks unknown.
    _complete(campaign, opaque, "scans/scan_id=OTHER", scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "completed_status_conflicts_with_measurement"
    # A completed receipt claiming no score over a numeric triplet blocks too.
    status_path.unlink()
    _complete(campaign, opaque, "scans/scan_id=S1", scored=False)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "completed_status_conflicts_with_measurement"
    # ...and a "scored" receipt over a stored null scan.
    status_path.unlink()
    _write_ledger(campaign, _triplet(target, score=None, steps=()))
    _complete(campaign, opaque, "scans/scan_id=S1", scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "completed_status_conflicts_with_measurement"


def test_completed_status_without_stored_triplet_is_terminal_completed(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _complete(campaign, campaign.opaque[target], "scans/scan_id=S9", scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "completed"
    assert run["exportable"] is False
    _export(campaign)
    assert _findings(campaign) == []


def test_completed_status_over_placeholder_stays_terminal_and_exports(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _write_ledger(campaign, _abstain(target, campaign.opaque[target], "unmeasured"))
    _complete(campaign, campaign.opaque[target], "scans/scan_id=S9", scored=True)
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "completed"
    assert run["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "abstain"


def test_incoherent_status_receipts_fail_closed(campaign: Campaign) -> None:
    target = campaign.ordered[0]
    opaque = campaign.opaque[target]
    status_path = campaign.status_dir / f"{opaque}-status.json"
    _write_ledger(campaign, _triplet(target, score=8.0))
    # A completed receipt from a different model blocks the stored triplet.
    _complete(campaign, opaque, "scans/scan_id=S1", scored=True, model="other/model")
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "failed_status_conflicts_with_measurement"
    # complete=True but no recorded location is not a supported receipt.
    status_path.unlink()
    _complete(campaign, opaque, "scans/scan_id=S1", scored=True)
    status = json.loads(status_path.read_text())
    status["location"] = None
    status_path.write_text(json.dumps(status))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "failed_status_conflicts_with_measurement"


def test_refusal_after_native_attempt_is_terminal_not_unread(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _status(
        campaign, campaign.opaque[target], source_category="policy_refusal",
        native_scan_attempted=True,
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "refused"
    assert run["exportable"] is False
    # A privacy no-send cannot claim a native attempt: that receipt fails closed.
    _status(
        campaign, campaign.opaque[target], source_category="secret_unsafe",
        native_scan_attempted=True,
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "failed"


# --------------------------------------------------------------------------- #
# stored real null scans under terminal/no-actor evidence
# --------------------------------------------------------------------------- #


def _no_actor(campaign: Campaign, trace_id: str) -> None:
    con = sqlite3.connect(campaign.database)
    con.execute(
        "DELETE FROM trace_steps WHERE trace_id = ? AND step_index = 1",
        (trace_id,),
    )
    con.commit()
    con.close()


def test_real_null_scan_with_no_actor_is_no_actor_and_exports(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _no_actor(campaign, target)
    _write_ledger(campaign, _triplet(target, score=None, steps=()))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "no_actor" and run["exportable"] is True
    assert run["stored"] == "null"
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "abstain"


def test_real_null_scan_with_terminal_status_stays_terminal_and_exports(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _status(campaign, campaign.opaque[target], source_category="secret_unsafe")
    _write_ledger(campaign, _triplet(target, score=None, steps=()))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "privacy"
    assert run["exportable"] is True
    _export(campaign)
    findings = {record["run_id"]: record for record in _findings(campaign)}
    assert findings[target]["verdict"] == "abstain"


def test_real_null_scan_with_terminal_placeholder_stays_terminal(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _write_ledger(
        campaign,
        _triplet(target, score=None, steps=())
        + _abstain(
            target, campaign.opaque[target], "policy_refusal",
            at="2026-10-28T08:00:00+00:00",
        ),
    )
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "terminal" and run["terminal"] == "refused"
    assert run["exportable"] is True


def test_numeric_measurement_with_no_actor_still_fails_closed(
    campaign: Campaign,
) -> None:
    target = campaign.ordered[0]
    _no_actor(campaign, target)
    _write_ledger(campaign, _triplet(target, score=8.0))
    run = _runs(_prepare(campaign))[target]
    assert run["state"] == "blocked"
    assert run["reason"] == "actor_unavailable_with_stored_measurement"
    _export(campaign)
    assert _findings(campaign) == []


# --------------------------------------------------------------------------- #
# output safety: ancestor symlinks and whole-directory publication
# --------------------------------------------------------------------------- #


def test_export_refuses_symlinked_output_ancestors(
    campaign: Campaign, tmp_path: Path
) -> None:
    real = tmp_path / "real-out"
    real.mkdir()
    link = tmp_path / "link-parent"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(mcp.CampaignError) as caught:
        mcp.export_stored_results(
            **campaign.kwargs, out_dir=link / "nested" / "out"
        )
    assert caught.value.reason == "output_symlink"
    assert not (real / "nested").exists()


def test_prepare_out_refuses_symlinked_ancestors(
    campaign: Campaign, tmp_path: Path
) -> None:
    real = tmp_path / "real-dir"
    real.mkdir()
    link = tmp_path / "link-dir"
    link.symlink_to(real, target_is_directory=True)
    assert (
        mcp.main(
            [*(_argv(campaign, "prepare")), "--out", str(link / "deep" / "plan.json")]
        )
        == 2
    )
    assert not (real / "deep").exists()


def test_export_publishes_directory_as_one_no_replace_operation(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected publish failure must leave no partial pair behind."""
    _write_ledger(campaign, _triplet(campaign.ordered[0], score=8.0))

    def deny(source: Path, target: Path) -> None:
        raise OSError(13, "denied")

    monkeypatch.setattr(mcp, "_rename_no_replace", deny)
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign)
    assert caught.value.reason == "output_write_failed"
    assert not campaign.out_dir.exists()


def test_export_existing_directory_requires_complete_identical_pair(
    campaign: Campaign,
) -> None:
    """A preexisting second file that mismatches — or is missing — refuses;
    the first file is never clobbered or partially repaired."""
    _write_ledger(campaign, _triplet(campaign.ordered[0], score=8.0))
    _export(campaign)
    findings = (campaign.out_dir / mcp.FINDINGS_NAME).read_bytes()
    (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).write_text("corrupt")
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign)
    assert caught.value.reason == "output_exists"
    assert (campaign.out_dir / mcp.FINDINGS_NAME).read_bytes() == findings
    (campaign.out_dir / mcp.EXPORT_MANIFEST_NAME).unlink()
    with pytest.raises(mcp.CampaignError) as caught:
        _export(campaign)
    assert caught.value.reason == "output_exists"
    assert (campaign.out_dir / mcp.FINDINGS_NAME).read_bytes() == findings
