from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq
import pytest

from evallab.evidence.facts import extract_job_facts, rebuild_from_raw
from evallab.harbor_state_journal import StateJournalPlugin
from evallab.results import load_job
from evallab.state_events import StateEventValidationError, load_state_event_facts

FIXTURE = Path(__file__).parent / "fixtures/state_events/free-producer"


def _producer_module() -> Any:
    context = Path(__file__).parents[1] / "containers/state-journal"
    path = StateJournalPlugin(context_dir=context).context_dir / "producer.py"
    spec = importlib.util.spec_from_file_location("state_journal_fixture_producer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _produce_fixture(journal: Path) -> None:
    producer = _producer_module()
    root = journal / "producer-root"
    target = root / "output/answer.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"baseline\n")
    target.chmod(0o644)
    os.utime(target, ns=(1787572800000000000, 1787572800000000000))
    baseline = producer.describe(target, root=root, max_hash_bytes=8 * 1024 * 1024)
    writes = (
        (
            b"write-one\n",
            1787572800100000000,
            "2026-08-24T12:00:00.100000Z",
            ["modify", "close_write"],
        ),
        (
            b"baseline\n",
            1787572800200000000,
            "2026-08-24T12:00:00.090000Z",
            ["close_write"],
        ),
        (
            b"write-two\n",
            1787572800300000000,
            "2026-08-24T12:00:00.300000Z",
            ["close_write"],
        ),
    )
    stream_path = journal / "state-events.jsonl"
    records: list[dict[str, Any]] = []
    with stream_path.open("w", encoding="utf-8") as stream:
        for sequence, (content, mtime_ns, timestamp, operations) in enumerate(
            writes, start=1
        ):
            target.write_bytes(content)
            target.chmod(0o644)
            os.utime(target, ns=(mtime_ns, mtime_ns))
            state = producer.describe(
                target, root=root, max_hash_bytes=8 * 1024 * 1024
            )
            record = producer.build_event(
                sequence=sequence,
                timestamp=timestamp,
                path="output/answer.txt",
                operations=operations,
                cookie=None,
                is_directory=False,
                state=state,
            )
            records.append(record)
            producer.append_event(stream, record)
    final = producer.describe(target, root=root, max_hash_bytes=8 * 1024 * 1024)
    diff = producer.build_diff(
        {
            "captured_at": "2026-08-24T12:00:00.000000Z",
            "truncated": False,
            "entries": [baseline],
        },
        {
            "captured_at": "2026-08-24T12:00:00.400000Z",
            "truncated": False,
            "entries": [final],
        },
        records,
    )
    _write_json(journal / "state-diff.json", diff)
    shutil.rmtree(root)
    assert stream_path.read_bytes() == (FIXTURE / "state-events.jsonl").read_bytes()
    assert diff == json.loads((FIXTURE / "state-diff.json").read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _job(root: Path) -> Path:
    job = root / "state-event-job"
    trial = job / "sample-task__abc123"
    _write_json(job / "config.json", {"job_name": "state-event-job"})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000041",
            "started_at": "2026-08-24T12:00:00Z",
            "finished_at": "2026-08-24T12:00:02Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    _write_json(trial / "config.json", {"agent": {"name": "oracle"}})
    _write_json(trial / "lock.json", {"schema_version": 2})
    _write_json(
        trial / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000048",
            "trial_name": trial.name,
            "task_name": "local-lab/sample-task",
            "task_checksum": "abc",
            "started_at": "2026-08-24T12:00:00Z",
            "finished_at": "2026-08-24T12:00:01Z",
            "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
            "agent_result": {
                "n_input_tokens": None,
                "n_cache_tokens": None,
                "n_output_tokens": None,
                "cost_usd": None,
            },
            "verifier_result": {"rewards": {"reward": 1.0}},
            "exception_info": None,
        },
    )
    journal = trial / "state-journal"
    journal.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "status.json", journal / "status.json")
    _produce_fixture(journal)
    return job


def test_write_revert_rewrite_survives_as_ordered_temporal_facts(tmp_path: Path) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]

    facts = load_state_event_facts(trial, job_id=job.id, experiment_id=None)

    assert [fact.predecessor_sequence for fact in facts] == [None, 1, 2]
    assert [fact.sequence for fact in facts] == [1, 2, 3]
    assert [fact.precedence for fact in facts] == [1, 2, 3]
    assert facts[0].operations == ("modify", "close_write")
    assert facts[0].before_content_sha256 == (
        "sha256:4b654bd1437066b13498661f3ca14774daf1066d072036beffaf06f0c014250e"
    )
    assert [fact.before_evidence_status for fact in facts] == [
        "known_state",
        "known_state",
        "known_state",
    ]
    assert [fact.after_content_sha256 for fact in facts] == [
        "sha256:33e157704c501c62e86a5324f7954e4d7a007587a5cc9d5cde11556ed5c9ec47",
        "sha256:4b654bd1437066b13498661f3ca14774daf1066d072036beffaf06f0c014250e",
        "sha256:b21ca99d5990b5c9c2621b537974ca060b8722f5ca3120e592065fb8600ab87a",
    ]
    assert facts[1].before_content_sha256 == facts[0].after_content_sha256
    assert facts[2].before_content_sha256 == facts[1].after_content_sha256
    assert facts[1].after_content_sha256 == facts[0].before_content_sha256
    assert {fact.temporal_semantics for fact in facts} == {
        "sequence_precedence_non_causal"
    }
    assert facts[1].event_at < facts[0].event_at  # sequence, not wall-clock order, wins
    assert len({fact.source_digest for fact in facts}) == 1
    assert facts[0].source_digest.startswith("sha256:")
    assert len({fact.source_record_digest for fact in facts}) == 3
    diff = json.loads(
        (trial.path / "state-journal/state-diff.json").read_text(encoding="utf-8")
    )
    assert diff["changes"][0]["before"]["sha256"] == facts[0].before_content_sha256
    assert diff["changes"][0]["after"]["sha256"] == facts[2].after_content_sha256
    assert facts[0].after_content_sha256 not in json.dumps(diff)


def test_reingest_and_query_are_idempotent(tmp_path: Path) -> None:
    job = load_job(_job(tmp_path))
    derived = tmp_path / "derived"
    rebuild_from_raw([job], derived)
    event_path = (
        derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}" / "state_events.parquet"
    )
    first = pq.read_table(event_path).to_pylist()

    rebuild_from_raw([job], derived)
    second = pq.read_table(event_path).to_pylist()
    queried = duckdb.sql(
        "SELECT sequence, path, after_content_sha256, "
        "list_contains(operations, 'modify') FROM read_parquet(?) "
        "ORDER BY job_id, trial_id, precedence",
        params=[str(event_path)],
    ).fetchall()

    assert second == first
    assert len(first) == 3
    assert [row[0] for row in queried] == [1, 2, 3]
    assert [row[3] for row in queried] == [True, False, False]


@pytest.mark.parametrize(
    "replacement, reason",
    [
        ("not-json\n", "malformed event"),
        (
            (FIXTURE / "state-events.jsonl").read_text(encoding="utf-8").splitlines()[0]
            + "\n"
            + (FIXTURE / "state-events.jsonl").read_text(encoding="utf-8").splitlines()[0]
            + "\n",
            "duplicate sequence 1",
        ),
        (
            (FIXTURE / "state-events.jsonl").read_text(encoding="utf-8").splitlines()[0]
            + "\n"
            + (FIXTURE / "state-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[1]
            .replace('"sequence": 2', '"sequence": 1')
            + "\n",
            "conflicting sequence 1",
        ),
    ],
)
def test_malformed_duplicate_and_conflicting_evidence_fail_closed(
    tmp_path: Path,
    replacement: str,
    reason: str,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    (trial.path / "state-journal/state-events.jsonl").write_text(
        replacement, encoding="utf-8"
    )

    with pytest.raises(StateEventValidationError, match=reason):
        load_state_event_facts(trial, job_id=job.id, experiment_id=None)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda line: line.replace('"sequence": 1', '"sequence": 2'),
            "does not append after 0",
        ),
        (lambda line: line + "\n", "blank records are invalid"),
        (
            lambda line: line.replace(
                "2026-08-24T12:00:00.100000Z", "2026-08-24T12:00:00.100000"
            ),
            "timestamp must include an offset",
        ),
        (
            lambda line: line.replace(
                '"path": "output/answer.txt", "sha256"',
                '"path": "output/other.txt", "sha256"',
            ),
            "state path conflicts with event path",
        ),
        (
            lambda line: line.replace('"type": "file"', '"type": "directory"'),
            "state type conflicts with event kind",
        ),
        (
            lambda line: line.replace('"modify"', '"invented_operation"'),
            "operations are invalid",
        ),
    ],
)
def test_invalid_event_contracts_fail_closed(
    tmp_path: Path,
    mutate: Any,
    reason: str,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    stream = trial.path / "state-journal/state-events.jsonl"
    first = stream.read_text(encoding="utf-8").splitlines()[0]
    stream.write_text(mutate(first) + "\n", encoding="utf-8")

    with pytest.raises(StateEventValidationError, match=reason):
        load_state_event_facts(trial, job_id=job.id, experiment_id=None)


@pytest.mark.parametrize("version", [None, 2])
def test_missing_or_unsupported_producer_version_fails_closed(
    tmp_path: Path, version: int | None
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    status_path = trial.path / "state-journal/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if version is None:
        status.pop("schema_version")
    else:
        status["schema_version"] = version
    _write_json(status_path, status)

    with pytest.raises(StateEventValidationError, match="schema_version"):
        load_state_event_facts(trial, job_id=job.id, experiment_id=None)


def test_available_missing_stream_fails_but_unavailable_is_explicit_empty(
    tmp_path: Path,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    stream = trial.path / "state-journal/state-events.jsonl"
    stream.unlink()
    with pytest.raises(StateEventValidationError, match="missing while producer status"):
        load_state_event_facts(trial, job_id=job.id, experiment_id=None)

    status_path = trial.path / "state-journal/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["status"] = "unavailable"
    _write_json(status_path, status)
    assert load_state_event_facts(trial, job_id=job.id, experiment_id=None) == ()


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing", "state-diff.json missing"),
        ("unreadable", "state-diff.json is unreadable or malformed"),
        ("malformed", "state-diff.json is unreadable or malformed"),
        ("invalid_before", "before metadata is invalid"),
        ("invalid_after", "after metadata is invalid"),
        ("invalid_path", "path is invalid"),
        ("duplicate_path", "duplicate or conflicting path"),
    ],
)
def test_available_stream_requires_valid_unambiguous_state_diff(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    if case == "missing":
        diff_path.unlink()
    elif case == "unreadable":
        diff_path.unlink()
        diff_path.mkdir()
    elif case == "malformed":
        diff_path.write_text("not-json", encoding="utf-8")
    else:
        diff = json.loads(diff_path.read_text(encoding="utf-8"))
        if case == "invalid_before":
            diff["changes"][0]["before"]["sha256"] = "invalid"
        elif case == "invalid_after":
            diff["changes"][0]["after"]["sha256"] = "invalid"
        elif case == "invalid_path":
            diff["changes"][0]["path"] = None
        elif case == "duplicate_path":
            diff["changes"].append(dict(diff["changes"][0]))
        _write_json(diff_path, diff)

    with pytest.raises(StateEventValidationError, match=reason):
        load_state_event_facts(trial, job_id=job.id, experiment_id=None)


def test_missing_diff_path_is_unknown_while_null_before_is_known_absent(
    tmp_path: Path,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    diff["changes"] = []
    diff["change_count"] = 0
    _write_json(diff_path, diff)
    facts = load_state_event_facts(trial, job_id=job.id, experiment_id=None)
    assert facts[0].before_evidence_status == "unknown_not_in_diff"

    diff = json.loads((FIXTURE / "state-diff.json").read_text(encoding="utf-8"))
    diff["changes"][0]["before"] = None
    diff["changes"][0]["change_type"] = "added"
    _write_json(diff_path, diff)
    facts = load_state_event_facts(trial, job_id=job.id, experiment_id=None)
    assert facts[0].before_evidence_status == "known_absent"


def test_invalid_state_diff_emits_sentinel_without_erasing_sibling_facts(
    tmp_path: Path,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    (trial.path / "state-journal/state-diff.json").write_text(
        "not-json", encoding="utf-8"
    )

    facts = extract_job_facts(job)

    assert len(facts.trials) == 1
    assert len(facts.rewards) == 1
    assert len(facts.state_changes) == 0
    assert len(facts.state_events) == 1
    invalid = facts.state_events[0]
    assert invalid.evidence_status == "invalid"
    assert invalid.before_evidence_status == "invalid"
    assert invalid.invalid_reason is not None
    assert "state-diff.json is unreadable or malformed" in invalid.invalid_reason


def test_invalid_stream_emits_sentinel_without_erasing_sibling_facts(
    tmp_path: Path,
) -> None:
    job = load_job(_job(tmp_path))
    trial = job.trials[0]
    stream = trial.path / "state-journal/state-events.jsonl"
    first = stream.read_text(encoding="utf-8").splitlines()[0]
    stream.write_text(
        first.replace(
            '"path": "output/answer.txt", "sha256"',
            '"path": "output/raced.txt", "sha256"',
        )
        + "\n",
        encoding="utf-8",
    )

    facts = extract_job_facts(job)

    assert len(facts.trials) == 1
    assert len(facts.rewards) == 1
    assert len(facts.state_changes) == 1
    assert len(facts.state_events) == 1
    invalid = facts.state_events[0]
    assert invalid.evidence_status == "invalid"
    assert invalid.sequence == 0
    assert invalid.invalid_reason is not None
    assert "state path conflicts with event path" in invalid.invalid_reason
    assert invalid.invalid_error_digest is not None
    assert invalid.source_digest.startswith("sha256:")


# --- Canonical State-Diff Validator & Loader Tests ----------------------------

def test_validate_state_diff_real_producer_build_diff_fixture() -> None:
    """Real producer build_diff output from fixture validates cleanly."""
    from evallab.state_events import load_state_diff, validate_state_diff_payload

    fixture_diff = FIXTURE / "state-diff.json"
    doc = load_state_diff(fixture_diff)
    assert doc.schema_version == 1
    assert doc.status == "available"
    assert doc.change_count == 1
    assert len(doc.changes) == 1
    assert doc.changes[0].path == "output/answer.txt"
    assert doc.changes[0].change_type == "modified"
    assert doc.changes[0].before is not None
    assert doc.changes[0].after is not None
    assert doc.dropped_event_count == 0

    raw_payload = json.loads(fixture_diff.read_text(encoding="utf-8"))
    doc2 = validate_state_diff_payload(raw_payload)
    assert doc2 == doc

    # Test producer diff with dropped_event_count
    raw_payload_dropped = dict(raw_payload)
    raw_payload_dropped["dropped_event_count"] = 4
    doc_dropped = validate_state_diff_payload(raw_payload_dropped)
    assert doc_dropped.dropped_event_count == 4

def test_validate_state_diff_payload_valid_v1_document() -> None:
    """Valid schema v1 state-diff document parses and validates correctly."""
    from evallab.state_events import validate_state_diff_payload

    valid_sha_1 = "sha256:" + "a" * 64
    valid_sha_2 = "sha256:" + "b" * 64

    payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 3,
        "event_count": 5,
        "changes": [
            {
                "path": "src/app.py",
                "change_type": "modified",
                "event_count": 2,
                "first_event_at": "2026-08-24T12:00:01.000000Z",
                "last_event_at": "2026-08-24T12:00:02.000000Z",
                "before": {
                    "path": "src/app.py",
                    "type": "file",
                    "size_bytes": 100,
                    "sha256": valid_sha_1,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800000000000,
                    "hash_status": "complete",
                },
                "after": {
                    "path": "src/app.py",
                    "type": "file",
                    "size_bytes": 120,
                    "sha256": valid_sha_2,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800100000000,
                    "hash_status": "complete",
                },
            },
            {
                "path": "new_file.txt",
                "change_type": "added",
                "event_count": 1,
                "first_event_at": "2026-08-24T12:00:03.000000Z",
                "last_event_at": "2026-08-24T12:00:03.000000Z",
                "before": None,
                "after": {
                    "path": "new_file.txt",
                    "type": "file",
                    "size_bytes": 50,
                    "sha256": valid_sha_1,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800200000000,
                    "hash_status": "complete",
                },
            },
            {
                "path": "old_file.txt",
                "change_type": "deleted",
                "event_count": 2,
                "first_event_at": "2026-08-24T12:00:04.000000Z",
                "last_event_at": "2026-08-24T12:00:05.000000Z",
                "before": {
                    "path": "old_file.txt",
                    "type": "file",
                    "size_bytes": 80,
                    "sha256": valid_sha_2,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800000000000,
                    "hash_status": "complete",
                },
                "after": None,
            },
        ],
    }

    doc = validate_state_diff_payload(payload)
    assert doc.schema_version == 1
    assert doc.status == "available"
    assert doc.change_count == 3
    assert doc.event_count == 5
    assert len(doc.changes) == 3
    assert doc.changes[0].path == "src/app.py"
    assert doc.changes[0].change_type == "modified"
    assert doc.changes[0].before is not None
    assert doc.changes[0].after is not None
    assert doc.changes[1].change_type == "added"
    assert doc.changes[1].before is None
    assert doc.changes[2].change_type == "deleted"
    assert doc.changes[2].after is None

    # Test serialization round-trip
    d = doc.to_dict()
    assert d["schema_version"] == 1
    assert len(d["changes"]) == 3


def test_validate_state_diff_bare_list_rejected() -> None:
    """Bare list payload is rejected: state-diff must be a top-level schema v1 dict."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "c" * 64
    changes_list = [
        {
            "path": "config/settings.json",
            "change_type": "added",
            "event_count": 0,
            "before": None,
            "after": {
                "path": "config/settings.json",
                "type": "file",
                "size_bytes": 42,
                "sha256": valid_sha,
                "mode": "-rw-r--r--",
                "mtime_ns": 1787572800000000000,
                "hash_status": "complete",
            },
        }
    ]

    with pytest.raises(StateEventValidationError, match="must be a schema_version 1 object with changes"):
        validate_state_diff_payload(changes_list)


@pytest.mark.parametrize(
    "bad_path",
    ["", "   ", "/", "///", "/etc/passwd", "../app.py", "dir/../../escape.py", "win\\path.txt", "./foo", "foo//bar", "foo/./bar", "foo/"],
)
def test_validate_state_diff_path_safety_and_normalization(bad_path: str) -> None:
    """Unsafe, absolute, backslash, or path-traversal paths are rejected."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "d" * 64
    payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [
            {
                "path": bad_path,
                "change_type": "added",
                "event_count": 0,
                "before": None,
                "after": {
                    "path": bad_path,
                    "type": "file",
                    "size_bytes": 10,
                    "sha256": valid_sha,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1,
                    "hash_status": "complete",
                },
            }
        ],
    }

    with pytest.raises(StateEventValidationError, match="path is invalid"):
        validate_state_diff_payload(payload)


@pytest.mark.parametrize(
    "bad_status",
    ["recording", "unknown", "disabled", "unavailable", "custom", "", "  ", "invalid_status"],
)
def test_validate_state_diff_status_strict_available_or_partial(bad_status: str) -> None:
    """Status must be strictly 'available' or 'partial'."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    payload = {
        "schema_version": 1,
        "status": bad_status,
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 0,
        "event_count": 0,
        "changes": [],
    }
    with pytest.raises(StateEventValidationError, match="status must be 'available' or 'partial'"):
        validate_state_diff_payload(payload)


@pytest.mark.parametrize(
    "bad_change_type",
    ["created", "unmodified", "renamed", "invented_mutation", "custom_change"],
)
def test_validate_state_diff_allowed_change_types(bad_change_type: str) -> None:
    """Unobserved or non-producer change_type values are strictly rejected."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "e" * 64
    payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [
            {
                "path": "app.py",
                "change_type": bad_change_type,
                "event_count": 0,
                "before": None,
                "after": {
                    "path": "app.py",
                    "type": "file",
                    "size_bytes": 10,
                    "sha256": valid_sha,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1,
                    "hash_status": "complete",
                },
            }
        ],
    }

    with pytest.raises(StateEventValidationError, match="not an allowed change type"):
        validate_state_diff_payload(payload)


def test_validate_state_diff_transition_consistency() -> None:
    """Before/after presence must be consistent with change_type."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "f" * 64
    meta_valid = {
        "path": "app.py",
        "type": "file",
        "size_bytes": 10,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
    }

    def make_doc(change: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "available",
            "root": "/app",
            "before_captured_at": "2026-08-24T12:00:00.000000Z",
            "after_captured_at": "2026-08-24T12:00:05.000000Z",
            "change_count": 1,
            "event_count": 0,
            "changes": [change],
        }

    # added requires before=None and after!=None
    with pytest.raises(StateEventValidationError, match="requires before=None and after!=None"):
        validate_state_diff_payload(make_doc({"path": "app.py", "change_type": "added", "event_count": 0, "before": meta_valid, "after": meta_valid}))

    # deleted requires before!=None and after=None
    with pytest.raises(StateEventValidationError, match="requires before!=None and after=None"):
        validate_state_diff_payload(make_doc({"path": "app.py", "change_type": "deleted", "event_count": 0, "before": None, "after": None}))

    # modified requires both before and after
    with pytest.raises(StateEventValidationError, match="requires both before and after"):
        validate_state_diff_payload(make_doc({"path": "app.py", "change_type": "modified", "event_count": 0, "before": None, "after": meta_valid}))


def test_validate_state_diff_metadata_enclosing_path_equality() -> None:
    """Metadata path must match the enclosing change record path."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "a" * 64
    payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [
            {
                "path": "pkg/module.py",
                "change_type": "added",
                "event_count": 0,
                "before": None,
                "after": {
                    "path": "pkg/different.py",
                    "type": "file",
                    "size_bytes": 10,
                    "sha256": valid_sha,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1,
                    "hash_status": "complete",
                },
            }
        ],
    }

    with pytest.raises(StateEventValidationError, match="conflicts with change path"):
        validate_state_diff_payload(payload)


def test_validate_state_diff_producer_hash_rules() -> None:
    """Hash rules for complete, size_limit, and unreadable files are enforced."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    def make_doc(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "available",
            "root": "/app",
            "before_captured_at": "2026-08-24T12:00:00.000000Z",
            "after_captured_at": "2026-08-24T12:00:05.000000Z",
            "change_count": 1,
            "event_count": 0,
            "changes": [{"path": "data.bin", "change_type": "added", "event_count": 0, "before": None, "after": meta}],
        }

    # complete requires sha256
    bad_complete = {
        "path": "data.bin",
        "type": "file",
        "size_bytes": 100,
        "sha256": None,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
    }
    with pytest.raises(StateEventValidationError, match="hash_status='complete' requires sha256"):
        validate_state_diff_payload(make_doc(bad_complete))

    # size_limit must not have sha256
    bad_size_limit = {
        "path": "data.bin",
        "type": "file",
        "size_bytes": 100,
        "sha256": "sha256:" + "a" * 64,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "size_limit",
    }
    with pytest.raises(StateEventValidationError, match="must not have sha256"):
        validate_state_diff_payload(make_doc(bad_size_limit))

    # directory cannot have symlink target
    bad_dir = {
        "path": "data.bin",
        "type": "directory",
        "size_bytes": 4096,
        "sha256": None,
        "mode": "drwxr-xr-x",
        "mtime_ns": 1,
        "target": "some_symlink_target",
    }
    with pytest.raises(StateEventValidationError, match="directory cannot have symlink target"):
        validate_state_diff_payload(make_doc(bad_dir))


def test_validate_state_diff_timestamps_and_temporal_order() -> None:
    """Timestamps must be offset-aware and temporally ordered."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "b" * 64
    meta_valid = {
        "path": "app.py",
        "type": "file",
        "size_bytes": 10,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
    }

    # Naive timestamp without timezone offset in before_captured_at
    payload_naive = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [{"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta_valid}],
    }
    with pytest.raises(StateEventValidationError, match="must include an offset"):
        validate_state_diff_payload(payload_naive)

    # after_captured_at preceding before_captured_at
    payload_reversed_captured = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:05.000000Z",
        "after_captured_at": "2026-08-24T12:00:00.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [{"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta_valid}],
    }
    with pytest.raises(StateEventValidationError, match="after_captured_at.*precedes before_captured_at"):
        validate_state_diff_payload(payload_reversed_captured)

    # last_event_at preceding first_event_at
    payload_reversed = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 2,
        "changes": [
            {
                "path": "app.py",
                "change_type": "added",
                "event_count": 2,
                "first_event_at": "2026-08-24T12:00:05.000000Z",
                "last_event_at": "2026-08-24T12:00:01.000000Z",
                "before": None,
                "after": meta_valid,
            }
        ],
    }
    with pytest.raises(StateEventValidationError, match="last_event_at.*precedes first_event_at"):
        validate_state_diff_payload(payload_reversed)

    # event_count == 0 with timestamps
    payload_zero_events_with_time = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [
            {
                "path": "app.py",
                "change_type": "added",
                "event_count": 0,
                "first_event_at": "2026-08-24T12:00:01.000000Z",
                "last_event_at": "2026-08-24T12:00:01.000000Z",
                "before": None,
                "after": meta_valid,
            }
        ],
    }
    with pytest.raises(StateEventValidationError, match="event_count=0 requires first_event_at and last_event_at to be None"):
        validate_state_diff_payload(payload_zero_events_with_time)

    # event_count > 0 with missing first_event_at
    payload_missing_first_at = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 1,
        "changes": [
            {
                "path": "app.py",
                "change_type": "added",
                "event_count": 1,
                "first_event_at": None,
                "last_event_at": "2026-08-24T12:00:01.000000Z",
                "before": None,
                "after": meta_valid,
            }
        ],
    }
    with pytest.raises(StateEventValidationError, match="event_count > 0 requires non-null first_event_at and last_event_at"):
        validate_state_diff_payload(payload_missing_first_at)


def test_validate_state_diff_count_consistency_and_duplicates() -> None:
    """change_count mismatch, negative counts, and duplicate paths fail closed."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "1" * 64
    meta = {
        "path": "app.py",
        "type": "file",
        "size_bytes": 10,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
    }

    # change_count claim mismatch
    payload_count_mismatch = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 5,
        "event_count": 0,
        "changes": [{"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta}],
    }
    with pytest.raises(StateEventValidationError, match="does not match changes length"):
        validate_state_diff_payload(payload_count_mismatch)

    # negative change_count
    payload_neg_count = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": -1,
        "event_count": 0,
        "changes": [],
    }
    with pytest.raises(StateEventValidationError, match="change_count must be a non-negative int64"):
        validate_state_diff_payload(payload_neg_count)

    # duplicate path
    payload_duplicate = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 2,
        "event_count": 0,
        "changes": [
            {"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta},
            {"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta},
        ],
    }
    with pytest.raises(StateEventValidationError, match="duplicate or conflicting path"):
        validate_state_diff_payload(payload_duplicate)


def test_validate_state_event_metadata_strict_no_coercion() -> None:
    """Pydantic strict=True rejects type coercion on metadata fields."""
    from evallab.state_events import StateEventValidationError, validate_state_event_metadata

    valid_sha = "sha256:" + "a" * 64
    base_meta = {
        "path": "app.py",
        "type": "file",
        "size_bytes": 100,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1787572800000000000,
        "hash_status": "complete",
    }

    # string for size_bytes must be rejected without int coercion
    bad_size_str = dict(base_meta, size_bytes="100")
    with pytest.raises(StateEventValidationError, match="metadata is invalid"):
        validate_state_event_metadata(bad_size_str, expected_path="app.py", side="before")

    # bool for size_bytes must be rejected without int coercion
    bad_size_bool = dict(base_meta, size_bytes=True)
    with pytest.raises(StateEventValidationError, match="metadata is invalid"):
        validate_state_event_metadata(bad_size_bool, expected_path="app.py", side="before")

    # string for mtime_ns must be rejected
    bad_mtime_str = dict(base_meta, mtime_ns="1787572800000000000")
    with pytest.raises(StateEventValidationError, match="metadata is invalid"):
        validate_state_event_metadata(bad_mtime_str, expected_path="app.py", side="before")

    # int for mode must be rejected without string coercion
    bad_mode_int = dict(base_meta, mode=644)
    with pytest.raises(StateEventValidationError, match="metadata is invalid"):
        validate_state_event_metadata(bad_mode_int, expected_path="app.py", side="before")

    # int for path must be rejected without string coercion
    bad_path_int = dict(base_meta, path=123)
    with pytest.raises(StateEventValidationError, match="metadata is invalid"):
        validate_state_event_metadata(bad_path_int, expected_path="app.py", side="before")


def test_validate_state_diff_signed_int64_bounds() -> None:
    """Signed int64 bounds and non-boolean requirements on all count/size fields."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "b" * 64
    valid_meta = {
        "path": "app.py",
        "type": "file",
        "size_bytes": 10,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
    }

    def make_doc(*, change_count: Any = 1, event_count: Any = 0, dropped_event_count: Any = 0, meta: dict[str, Any] = valid_meta) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "available",
            "root": "/app",
            "before_captured_at": "2026-08-24T12:00:00.000000Z",
            "after_captured_at": "2026-08-24T12:00:05.000000Z",
            "change_count": change_count,
            "event_count": event_count,
            "dropped_event_count": dropped_event_count,
            "changes": [{"path": "app.py", "change_type": "added", "event_count": 0, "before": None, "after": meta}],
        }

    # size_bytes overflow (> 2**63 - 1)
    bad_size_overflow = dict(valid_meta, size_bytes=2**63)
    with pytest.raises(StateEventValidationError, match="size_bytes must be a non-negative int64"):
        validate_state_diff_payload(make_doc(meta=bad_size_overflow))

    # mtime_ns overflow (> 2**63 - 1)
    bad_mtime_overflow = dict(valid_meta, mtime_ns=2**63)
    with pytest.raises(StateEventValidationError, match="mtime_ns must be a signed int64"):
        validate_state_diff_payload(make_doc(meta=bad_mtime_overflow))

    # bool in change_count
    with pytest.raises(StateEventValidationError, match="change_count must be a non-negative int64"):
        validate_state_diff_payload(make_doc(change_count=True))

    # bool in event_count
    with pytest.raises(StateEventValidationError, match="event_count must be a non-negative int64"):
        validate_state_diff_payload(make_doc(event_count=True))

    # bool in dropped_event_count
    with pytest.raises(StateEventValidationError, match="dropped_event_count must be a non-negative int64"):
        validate_state_diff_payload(make_doc(dropped_event_count=True))


def test_validate_state_diff_canonical_root_dot() -> None:
    """Literal '.' is accepted as canonical root path in state diff records."""
    from evallab.state_events import validate_state_diff_payload

    payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "before_captured_at": "2026-08-24T12:00:00.000000Z",
        "after_captured_at": "2026-08-24T12:00:05.000000Z",
        "change_count": 1,
        "event_count": 0,
        "changes": [
            {
                "path": ".",
                "change_type": "modified",
                "event_count": 0,
                "before": {
                    "path": ".",
                    "type": "directory",
                    "size_bytes": 4096,
                    "sha256": None,
                    "mode": "drwxr-xr-x",
                    "mtime_ns": 1,
                },
                "after": {
                    "path": ".",
                    "type": "directory",
                    "size_bytes": 4096,
                    "sha256": None,
                    "mode": "drwxr-xr-x",
                    "mtime_ns": 2,
                },
            }
        ],
    }

    doc = validate_state_diff_payload(payload)
    assert doc.changes[0].path == "."
    assert doc.changes[0].before.path == "."
    assert doc.changes[0].after.path == "."


def test_validate_state_diff_producer_field_exclusivity_detailed() -> None:
    """Non-files must not have sha256 or hash_status, files cannot have target, symlinks can have target."""
    from evallab.state_events import StateEventValidationError, validate_state_diff_payload

    valid_sha = "sha256:" + "d" * 64

    def make_doc(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "available",
            "root": "/app",
            "before_captured_at": "2026-08-24T12:00:00.000000Z",
            "after_captured_at": "2026-08-24T12:00:05.000000Z",
            "change_count": 1,
            "event_count": 0,
            "changes": [{"path": "item", "change_type": "added", "event_count": 0, "before": None, "after": meta}],
        }

    # File with target
    file_with_target = {
        "path": "item",
        "type": "file",
        "size_bytes": 10,
        "sha256": valid_sha,
        "mode": "-rw-r--r--",
        "mtime_ns": 1,
        "hash_status": "complete",
        "target": "some_symlink_target",
    }
    with pytest.raises(StateEventValidationError, match="regular file cannot have symlink target"):
        validate_state_diff_payload(make_doc(file_with_target))

    # Directory with sha256
    dir_with_sha = {
        "path": "item",
        "type": "directory",
        "size_bytes": 4096,
        "sha256": valid_sha,
        "mode": "drwxr-xr-x",
        "mtime_ns": 1,
    }
    with pytest.raises(StateEventValidationError, match="non-file 'directory' must not have sha256"):
        validate_state_diff_payload(make_doc(dir_with_sha))

    # Directory with hash_status
    dir_with_hash_status = {
        "path": "item",
        "type": "directory",
        "size_bytes": 4096,
        "sha256": None,
        "mode": "drwxr-xr-x",
        "mtime_ns": 1,
        "hash_status": "complete",
    }
    with pytest.raises(StateEventValidationError, match="non-file 'directory' must not have hash_status"):
        validate_state_diff_payload(make_doc(dir_with_hash_status))

    # Symlink with valid target passes cleanly
    symlink_valid = {
        "path": "item",
        "type": "symlink",
        "size_bytes": 12,
        "sha256": None,
        "mode": "lrwxrwxrwx",
        "mtime_ns": 1,
        "target": "/app/original.txt",
    }
    doc = validate_state_diff_payload(make_doc(symlink_valid))
    assert doc.changes[0].after.type == "symlink"
    assert doc.changes[0].after.target == "/app/original.txt"
