"""Synthetic-only admission, observability and accounting regressions.

No fixture opens the production database, source CAS, provider keys, protected
inventory or historical trace. The lower admission function accepts miniature
SQL/count contracts so identity integrity can be tested in temporary SQLite.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO / "research/experiments/pg-self1-goodfire-20261026/run.py"
SPEC = importlib.util.spec_from_file_location("pg_goodfire_benchmark_test_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)

ADMISSION_SQL = """
SELECT COUNT(DISTINCT task_id) AS tasks, COUNT(*) AS traces,
       SUM(step_count) AS recorded_steps,
       SUM(json_extract(verified_receipt_json, '$.source_label.verdict_enacted') = 'yes') AS enacted_yes,
       SUM(json_extract(verified_receipt_json, '$.source_label.verdict') = 'yes'
           AND json_extract(verified_receipt_json, '$.source_label.verdict_enacted') = 'no') AS considered_only,
       SUM(json_extract(verified_receipt_json, '$.source_label.verdict') = 'no'
           AND json_extract(verified_receipt_json, '$.source_label.verdict_enacted') = 'no') AS source_clean
FROM agent_traces
"""
PROJECTION_SQL = """
SELECT trace_id, task_id, step_count, run_id, model_id, raw_cas_uri, verified_receipt_json
FROM agent_traces ORDER BY trace_id
"""
POSITIVE = {"judged": True, "verdict": "yes", "verdict_enacted": "yes"}
CLEAN = {"judged": True, "verdict": "no", "verdict_enacted": "no"}
EXCLUDED = [
    {"judged": True, "verdict": "yes", "verdict_enacted": "no"},
    {"judged": True, "verdict": "split", "verdict_enacted": "no"},
    {"judged": True, "verdict": "split", "verdict_enacted": "split"},
    {"judged": True, "verdict": "yes", "verdict_enacted": "split"},
    {"judged": False, "verdict": None, "verdict_enacted": None},
]
ENTRY = "HB-SYNTH-001"
RULES = [
    {"id": "v", "eligibility": "verdict", "gate": True, "entry_ids": [ENTRY]},
    {"id": "cue", "eligibility": "diagnostic", "gate": False, "entry_ids": [ENTRY]},
    {"id": "zero", "eligibility": "verdict", "gate": False, "entry_ids": [ENTRY]},
]
COVERAGE = {ENTRY: {"status": "partial", "rules": ["v", "cue", "zero"]}}
STRUCTURAL = [{"check": "instruction_check", "entry_ids": [ENTRY],
               "kind": "task_structure", "requires": ["instruction"]}]
RUNTIME = SimpleNamespace(HACKBOOK_RULES=RULES, HACKBOOK_ENTRY_COVERAGE=COVERAGE,
                          HACKBOOK_STRUCTURAL_CHECKS=STRUCTURAL)


def _manifest_item(trace_id: str, label: dict, receipt: str | None = None) -> dict:
    if receipt is None:
        receipt = json.dumps({"source_label": label, "rights": {"rights_status": "synthetic_hold"}})
    return {
        "trace_id": trace_id, "task_id": f"task-{trace_id}", "run_id": "synthetic-dev-run",
        "recorded_step_count": 1, "raw_cas_uri": f"cas://synthetic/{trace_id}",
        "source_reported_model_id": "source-reported:synthetic-not-served",
        "stored_receipt_sha256": hashlib.sha256(receipt.encode()).hexdigest(),
        "label_tuple": dict(label), "legacy_trace_rights_status": "synthetic_hold",
    }


def _database(tmp_path: Path) -> tuple[Path, list[dict], dict]:
    database = tmp_path / "synthetic.sqlite"
    manifest = []
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript("""
            CREATE TABLE agent_traces (
                trace_id TEXT PRIMARY KEY, task_id TEXT, step_count INTEGER,
                run_id TEXT, model_id TEXT, raw_cas_uri TEXT, verified_receipt_json TEXT
            );
            CREATE TABLE trace_steps (
                trace_id TEXT, step_index INTEGER, role TEXT, content_text TEXT, flags_json TEXT,
                PRIMARY KEY(trace_id, step_index)
            );
        """)
        for trace_id, label in (("a", POSITIVE), ("b", CLEAN)):
            receipt = json.dumps({"source_label": label, "rights": {"rights_status": "synthetic_hold"}})
            item = _manifest_item(trace_id, label, receipt)
            manifest.append(item)
            writer.execute("INSERT INTO agent_traces VALUES (?,?,?,?,?,?,?)", (
                trace_id, item["task_id"], 1, item["run_id"], item["source_reported_model_id"],
                item["raw_cas_uri"], receipt,
            ))
            writer.execute("INSERT INTO trace_steps VALUES (?,?,?,?,?)", (
                trace_id, 0, "assistant", f"private-synthetic-body-{trace_id}", "{}",
            ))
    counts = {"tasks": 2, "traces": 2, "recorded_steps": 2,
              "enacted_yes": 1, "considered_only": 0, "source_clean": 1}
    return database, manifest, counts


def _admit(connection, manifest, counts):
    return RUNNER.admit_snapshot(connection, manifest, ADMISSION_SQL, PROJECTION_SQL,
                                 expected_counts=counts)


def test_json_run_membership_admission_does_not_authorize_early_body_reads(tmp_path: Path) -> None:
    database, manifest, counts = _database(tmp_path)
    admission = ADMISSION_SQL + """
    WHERE EXISTS (
        SELECT 1 FROM json_each('["synthetic-dev-run"]') r
        WHERE r.type = 'text' AND r.value = agent_traces.run_id
    )
    """
    with RUNNER.readonly_snapshot(database) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            RUNNER.read_trace_rows(connection, manifest[0])
        admitted = RUNNER.admit_snapshot(
            connection, manifest, admission, PROJECTION_SQL, expected_counts=counts
        )
        assert [row["trace_id"] for row in admitted] == ["a", "b"]
        assert RUNNER.read_trace_rows(connection, admitted[0])[0]["content_text"] == "private-synthetic-body-a"


def test_same_count_identity_substitution_is_rejected_before_body_read(tmp_path: Path) -> None:
    database, manifest, counts = _database(tmp_path)
    with sqlite3.connect(database) as writer:
        writer.execute("UPDATE agent_traces SET trace_id='same-count-substitute' WHERE trace_id='b'")
    with RUNNER.readonly_snapshot(database) as connection:
        observed = dict(connection.execute(ADMISSION_SQL).fetchone())
        assert observed == counts  # The weak aggregate guard really does pass.
        executed = []
        connection.set_trace_callback(executed.append)
        with pytest.raises(RUNNER.AdmissionError, match="identity set changed"):
            _admit(connection, manifest, counts)
        assert not any("trace_steps" in statement for statement in executed)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT content_text FROM trace_steps").fetchall()


@pytest.mark.parametrize(("column", "value"), [
    ("task_id", "same-count-wrong-task"),
    ("run_id", "different-run"),
    ("model_id", "source-reported:other"),
    ("raw_cas_uri", "cas://synthetic/substituted-source"),
])
def test_exact_associations_not_just_counts_are_required(tmp_path: Path, column: str, value: str) -> None:
    database, manifest, counts = _database(tmp_path)
    with sqlite3.connect(database) as writer:
        # Column names come only from the fixed test parameter list, never external input.
        writer.execute(f"UPDATE agent_traces SET {column}=? WHERE trace_id='a'", (value,))
    with RUNNER.readonly_snapshot(database) as connection:
        assert dict(connection.execute(ADMISSION_SQL).fetchone()) == counts
        with pytest.raises(RUNNER.AdmissionError, match="metadata association changed"):
            _admit(connection, manifest, counts)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT flags_json FROM trace_steps").fetchall()


def test_receipt_hash_uses_exact_stored_text_not_json_equivalence(tmp_path: Path) -> None:
    database, manifest, counts = _database(tmp_path)
    with sqlite3.connect(database) as writer:
        original = writer.execute("SELECT verified_receipt_json FROM agent_traces WHERE trace_id='a'").fetchone()[0]
        rewritten = json.dumps(json.loads(original), sort_keys=True, separators=(",", ":"))
        assert json.loads(rewritten) == json.loads(original)
        assert rewritten != original
        writer.execute("UPDATE agent_traces SET verified_receipt_json=? WHERE trace_id='a'", (rewritten,))
    with RUNNER.readonly_snapshot(database) as connection:
        assert dict(connection.execute(ADMISSION_SQL).fetchone()) == counts
        with pytest.raises(RUNNER.AdmissionError, match="metadata association changed"):
            _admit(connection, manifest, counts)


def test_admission_and_body_read_use_one_readonly_snapshot(tmp_path: Path) -> None:
    database, manifest, counts = _database(tmp_path)
    with RUNNER.readonly_snapshot(database) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            RUNNER.read_trace_rows(connection, manifest[0])
        admitted = _admit(connection, manifest, counts)
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE trace_steps SET content_text='changed-after-admission' WHERE trace_id='a'")
            writer.execute("UPDATE agent_traces SET raw_cas_uri='changed-after-admission' WHERE trace_id='a'")
        rows = RUNNER.read_trace_rows(connection, admitted[0])
        assert rows == [{"step_index": 0, "role": "assistant",
                         "content_text": "private-synthetic-body-a", "flags_json": "{}"}]
        assert connection.execute("SELECT raw_cas_uri FROM agent_traces WHERE trace_id='a'").fetchone()[0] == "cas://synthetic/a"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE trace_steps SET content_text='must-not-write'")


def _record(trace_id: str, label: dict, hits: list[str], *, complete: bool = True,
            elapsed: int = 10) -> dict:
    return {
        "trace_id": trace_id, "source_label": dict(label), "scan_status": "scanned",
        "scanner_complete": complete, "rule_ids": hits,
        "whole_suite_scan_ns": elapsed,
        "adapter": {"native_nodes": 1, "normalized_nodes": 1, "sampled_agent_nodes": 1,
                    "carrier_aliases": 0, "tool_observations": 0,
                    "unavailable_command_calls": 0 if complete else 1},
        "entry_accounting": {ENTRY: {"status": "hit" if hits else "unavailable",
                                      "action": "hit" if hits else "no_hit", "structural": "unavailable"}},
        "structural_checks": [{"check": "instruction_check", "entry_id": ENTRY,
                                "status": "unavailable", "reason_code": "missing_inputs"}],
    }


def _metric_fixture() -> tuple[list[dict], list[dict]]:
    records = [
        _record("p1", POSITIVE, ["v", "cue"], elapsed=10),
        _record("p2", POSITIVE, ["cue"], elapsed=20),
        _record("n1", CLEAN, ["v"], elapsed=30),
        _record("n2", CLEAN, [], elapsed=40),
    ]
    records += [_record(f"excluded-{index}", label, ["v"], elapsed=50 + 10 * index)
                for index, label in enumerate(EXCLUDED)]
    manifest = [_manifest_item(row["trace_id"], row["source_label"]) for row in records]
    return records, manifest


def _summary(records, manifest, *, planned_n=None):
    return RUNNER.summarize(records, manifest, RULES, COVERAGE, STRUCTURAL,
                            planned_n=len(manifest) if planned_n is None else planned_n)


def test_considered_split_and_unjudged_are_not_binary_negatives() -> None:
    records, manifest = _metric_fixture()
    summary = _summary(records, manifest)
    rules = {rule["id"]: rule for rule in summary["per_rule"]}
    verdict = rules["v"]
    assert (verdict["true_positive_n"], verdict["false_positive_n"],
            verdict["false_negative_n"], verdict["true_negative_n"]) == (1, 1, 1, 1)
    assert verdict["precision"] == 0.5
    assert verdict["recall"] == 0.5
    assert verdict["clean_far"] == 0.5
    assert verdict["firing_n"] == 7
    assert verdict["firing_rate"] == pytest.approx(7 / 9)
    assert verdict["denominators"] == {"binary_observable_traces": 9, "firing_traces": 9,
                                        "source_enacted_yes": 2, "source_clean": 2, "complete_case": 4}
    assert verdict["excluded_from_confusion_n"] == 5
    assert len(summary["source_label_strata"]) == 7
    assert sum(cell["admitted_n"] for cell in summary["source_label_strata"].values()) == 9
    assert rules["zero"]["firing_n"] == 0
    assert rules["zero"]["precision"] is None
    assert rules["zero"]["recall"] == 0
    assert rules["zero"]["clean_far"] == 0


def test_verdict_marginal_survives_cue_and_unions_remain_separate() -> None:
    records, manifest = _metric_fixture()
    summary = _summary(records, manifest)
    rules = {rule["id"]: rule for rule in summary["per_rule"]}
    assert rules["v"]["marginal_true_positive_n"] == 1
    assert rules["v"]["marginal_universe"] == "verdict_rules"
    assert rules["cue"]["marginal_true_positive_n"] == 1
    assert rules["cue"]["marginal_universe"] == "all_runtime_rules"
    assert summary["unions"]["any_verdict_rule"]["recall"] == 0.5
    assert summary["unions"]["any_rule_diagnostic_inclusive"]["recall"] == 1
    assert summary["unions"]["any_rule_diagnostic_inclusive"]["precision"] == pytest.approx(2 / 3)


def test_pilot_keeps_full_accounting_and_null_unobserved_denominators() -> None:
    records, manifest = _metric_fixture()
    summary = _summary(records[:1], manifest, planned_n=1)
    assert summary["measurement_status"] == "partial"
    assert summary["processing_complete"] is False
    assert summary["pilot"] is True
    assert summary["attempted_n"] == 1
    assert summary["not_attempted_n"] == 8
    assert summary["admitted_counts"]["traces"] == 9
    assert summary["source_label_strata"]["source_clean"]["not_attempted_n"] == 2
    verdict = summary["unions"]["any_verdict_rule"]
    assert verdict["denominators"]["source_enacted_yes"] == 1
    assert verdict["denominators"]["source_clean"] == 0
    assert verdict["clean_far"] is None
    assert verdict["recall"] == 1
    assert summary["entry_accounting"][ENTRY]["binary_unmeasured_traces"] == 8
    assert summary["structural_checks"][0]["status_counts_all_returned_scans"] == {"hit": 0, "clear": 0, "unavailable": 1}


def test_partial_native_schema_keeps_firing_and_latency_but_not_binary_negatives() -> None:
    records, manifest = _metric_fixture()
    records.extend([
        _record("partial-positive", POSITIVE, ["v"], complete=False, elapsed=999),
        _record("partial-clean", CLEAN, [], complete=False, elapsed=998),
    ])
    manifest.extend([_manifest_item(row["trace_id"], row["source_label"]) for row in records[-2:]])
    summary = _summary(records, manifest)
    assert summary["processing_complete"] is True
    assert summary["measurement_status"] == "partial"
    assert (summary["scanned_n"], summary["scanner_complete_n"], summary["partial_observability_n"]) == (11, 9, 2)
    verdict = summary["unions"]["any_verdict_rule"]
    assert verdict["precision"] == 0.5
    assert verdict["recall"] == 0.5
    assert verdict["clean_far"] == 0.5
    assert verdict["firing_n"] == 8
    assert verdict["firing_rate"] == pytest.approx(8 / 11)
    assert verdict["partial_observability_excluded_from_binary_n"] == 2
    assert verdict["denominators"]["complete_case"] == 4
    assert summary["source_label_strata"]["enacted_yes"]["admitted_n"] == 3
    assert summary["source_label_strata"]["enacted_yes"]["scanner_complete_n"] == 2
    assert summary["source_label_strata"]["source_clean"]["partial_observability_n"] == 1
    assert summary["latency"]["measured_trace_n"] == 11
    assert summary["latency"]["whole_suite_scan_p95_ns"] == 999
    assert summary["latency"]["whole_suite_scan_complete_only_p95_ns"] == 90


def test_unavailable_trace_is_not_a_false_negative_or_clean_result() -> None:
    records, manifest = _metric_fixture()
    records[0] = {"trace_id": "p1", "source_label": POSITIVE, "scan_status": "unavailable",
                  "scanner_complete": False, "error_type": "ValueError"}
    summary = _summary(records, manifest)
    assert summary["unavailable_n"] == 1
    assert summary["unions"]["any_verdict_rule"]["denominators"]["source_enacted_yes"] == 1
    assert summary["unions"]["any_verdict_rule"]["false_negative_n"] == 1
    assert summary["source_label_strata"]["enacted_yes"]["unavailable_n"] == 1
    assert summary["entry_accounting"][ENTRY]["binary_unmeasured_traces"] == 1


def _report(secret="private-actor-content") -> dict:
    return {
        "hackbook": [{"rule": "v", "step": 0, "eligibility": "verdict", "gate": True,
                      "evidence": secret, "mechanism": secret, "command": secret}],
        "entry_accounting": {ENTRY: {"status": "hit", "action": "hit", "structural": "unavailable",
                                      "hits": [{"private": secret}]}},
        "structural_checks": [{"check": "instruction_check", "entry_id": ENTRY,
                               "kind": "task_structure", "status": "unavailable", "cited_steps": [],
                               "reason": f"structural_scan_failed: {secret}", "inputs": [secret], "basis": secret}],
        "structural_context": {"provided": False, "keys": [], "dropped_records": 0},
        "prompt": secret,
    }


def test_compaction_retains_ids_status_and_hashes_not_actor_text() -> None:
    secret = "synthetic-secret-prompt-and-command"
    compact = RUNNER.compact_report(_report(secret), RUNTIME)
    assert secret not in json.dumps(compact)
    assert compact["rule_ids"] == ["v"]
    assert compact["hits"][0]["step"] == 0
    assert compact["hits"][0]["evidence_sha256"] == hashlib.sha256(json.dumps(secret).encode()).hexdigest()
    assert compact["entry_accounting"][ENTRY] == {"status": "hit", "action": "hit", "structural": "unavailable"}
    assert compact["structural_checks"][0]["reason_code"] == "structural_scan_failed"


def test_measurement_never_passes_labels_to_adapter_or_scanner(tmp_path: Path, monkeypatch) -> None:
    database, manifest, counts = _database(tmp_path)
    captured = []

    def adapt(rows):
        captured.append(("adapt", rows))
        assert set(rows[0]) == {"step_index", "role", "content_text", "flags_json"}
        steps = [{"step_id": 0, "source": "agent", "message": rows[0]["content_text"]}]
        metadata = {"source_sha256": "a" * 64, "normalized_sha256": "b" * 64,
                    "native_nodes": 1, "normalized_nodes": 1, "sampled_agent_nodes": 1,
                    "carrier_aliases": 0, "tool_observations": 0,
                    "unavailable_command_calls": 1, "unavailable_command_steps": [0],
                    "private_extra": "do-not-persist"}
        return steps, steps, metadata

    def scan(agent, *, steps, trial_dir):
        captured.append(("scan", agent, steps, trial_dir))
        assert agent == steps
        assert set(agent[0]) == {"step_id", "source", "message"}
        assert trial_dir is None
        return _report("do-not-persist")

    ticks = iter((100, 350))
    monkeypatch.setattr(RUNNER.time, "perf_counter_ns", lambda: next(ticks))
    runtime = SimpleNamespace(**vars(RUNTIME), scan_trace=scan)
    adapter = SimpleNamespace(adapt_trace=adapt)
    progress = SimpleNamespace(phase=lambda *_: None)
    with RUNNER.readonly_snapshot(database) as connection:
        admitted = _admit(connection, manifest, counts)
        record = RUNNER.measure_one(connection, admitted[0], runtime, adapter, progress, "f" * 64)
    assert [call[0] for call in captured] == ["adapt", "scan"]
    assert record["scan_status"] == "scanned"
    assert record["scanner_complete"] is False
    assert record["whole_suite_scan_ns"] == 250
    assert record["rule_ids"] == ["v"]
    encoded = json.dumps(record)
    assert "private-synthetic-body-a" not in encoded
    assert "do-not-persist" not in encoded
    assert record["source_label"] == POSITIVE


def test_output_creation_never_overwrites_persisted_records(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    original = {"trace_id": "synthetic", "scan_status": "unavailable"}
    recorded_sha = RUNNER.write_new_json(path, original, jsonl=True)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        RUNNER.write_new_json(path, {"trace_id": "substitution"}, jsonl=True)
    assert path.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == recorded_sha
    assert json.loads(before) == original


@pytest.mark.parametrize("changed_name", [
    "goodfire-admission-query.sql",
    "goodfire-metadata-projection-query.sql",
])
def test_sql_byte_tampering_fails_before_manifest_parse_or_database_open(
    tmp_path: Path, monkeypatch, changed_name: str
) -> None:
    # Intentionally not JSON: all three byte pins must be checked before parsing.
    payloads = {
        "goodfire-selected-metadata.jsonl": (b"not-json\n", "MANIFEST_SHA256"),
        "goodfire-admission-query.sql": (b"synthetic admission SQL\n", "ADMISSION_SQL_SHA256"),
        "goodfire-metadata-projection-query.sql": (b"synthetic projection SQL\n", "PROJECTION_SQL_SHA256"),
    }
    for name, (payload, constant) in payloads.items():
        (tmp_path / name).write_bytes(payload)
        monkeypatch.setattr(RUNNER, constant, hashlib.sha256(payload).hexdigest())
    (tmp_path / changed_name).write_bytes(payloads[changed_name][0] + b"-- substituted\n")

    def forbidden_connection(*_args, **_kwargs):
        pytest.fail("Preflight byte validation must never open any database")

    monkeypatch.setattr(RUNNER.sqlite3, "connect", forbidden_connection)
    with pytest.raises(RUNNER.AdmissionError, match=f"Pinned preflight hash mismatch: {changed_name}"):
        RUNNER.load_preflight(tmp_path)
