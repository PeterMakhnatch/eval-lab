"""The `session.id` bridge between Phoenix traces and the research graph.

Three properties are defended here:

1. **Redaction survives conversion.** What promotion withheld must not reach a
   span payload. The proof is mutation-controlled: the same document with the
   markers replaced by plaintext *does* leak, so the passing assertion is not
   vacuous.
2. **Trace identity is derived, not guessed.** The ids `evallab` computes for a
   trial equal the ids the converter actually emits.
3. **The reverse join refuses ambiguity.** `trajectory_documents.session_id` is
   not unique and cannot be made unique, so resolution returns a set and says
   so when a session reaches more than one trial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from evallab.atif import project_trial
from evallab.results import load_job
from evallab.tracing import (
    TraceError,
    convert_atif,
    iter_spans,
    leaked_values,
    redaction_markers,
    resolve_session,
    root_session_id,
    session_lookup_sql,
    span_attribute,
    trace_identity,
    trace_identity_for_trial,
    truncated_redaction_markers,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "research/evidence/runs"
# A promoted, redacted Codex trial. Its `session_id` is one of the values the
# shared catalog holds, so this file is the committed half of a live join.
EVIDENCE_TRIAL = EVIDENCE / "canary-event-summary-codex-20260815/event-summary__5E3btLv"
EVIDENCE_SESSION_ID = "01a0043a-4b83-7252-a594-fa289617124f"
EVIDENCE_TRIAL_ID = "aa94250c-4f6c-4b66-bf20-1c36cd371133"


def _committed_codex_trajectories() -> list[Path]:
    return sorted(EVIDENCE.glob("*/*/agent/trajectory.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _evidence_trajectory() -> dict[str, Any]:
    return _load(EVIDENCE_TRIAL / "agent/trajectory.json")


def _unredact(node: Any, replacements: dict[str, str]) -> Any:
    """Rebuild a document with every redaction marker replaced by plaintext."""
    if isinstance(node, str):
        return replacements.get(node, node)
    if isinstance(node, dict):
        return {key: _unredact(value, replacements) for key, value in node.items()}
    if isinstance(node, list):
        return [_unredact(value, replacements) for value in node]
    return node


# --------------------------------------------------------------------------
# 1. Redaction must not be undone by conversion.
# --------------------------------------------------------------------------


def test_committed_evidence_is_actually_redacted() -> None:
    """Guard the premise: the tests below are worthless if nothing is redacted."""
    trajectory = _evidence_trajectory()
    markers = redaction_markers(trajectory)

    assert len(markers) >= 2, "promoted evidence must carry redaction markers"
    assert truncated_redaction_markers(trajectory) == ()
    assert trajectory["evallab_redaction"]


def test_root_span_input_is_the_redaction_marker_verbatim() -> None:
    trajectory = _evidence_trajectory()
    _resource_spans, payload = convert_atif(trajectory)
    roots = [span for span in iter_spans(payload) if not span.get("parentSpanId")]
    assert len(roots) == 1

    value = span_attribute(roots[0], "input.value")
    assert value in redaction_markers(trajectory)


def test_no_committed_evidence_trajectory_leaks_prompt_text_into_spans() -> None:
    """The withheld bytes never appear; only the marker does.

    The plaintext is genuinely gone from the repository, so it cannot be
    searched for directly. What is checkable is the converse: every marker that
    reaches a span is a whole, unaltered marker from the source document. That
    rules out both a reconstructed prompt and a marker cut short by
    `atif2otel`'s attribute truncation, which would drop the digest while still
    looking redacted.
    """
    trajectories = _committed_codex_trajectories()
    assert trajectories, "committed Codex evidence is required"

    for path in trajectories:
        trajectory = _load(path)
        source_markers = set(redaction_markers(trajectory))
        assert source_markers, path

        _resource_spans, payload = convert_atif(trajectory)
        span_markers = set(redaction_markers(payload))
        assert span_markers, f"{path}: redaction did not reach the spans at all"
        assert span_markers <= source_markers, f"{path}: span carries a foreign marker"
        assert truncated_redaction_markers(payload) == (), f"{path}: marker truncated"


def test_spans_would_leak_if_promotion_stopped_redacting() -> None:
    """Mutation control for the two tests above.

    Conversion copies whatever the ATIF holds. Feed it the same document with
    the markers replaced by plaintext and the plaintext lands on the span — so
    the assertions above fail the moment redaction is bypassed, which is the
    only reason they are worth having.
    """
    trajectory = _evidence_trajectory()
    markers = redaction_markers(trajectory)
    secrets = {
        marker: f"LEAKED-PROMPT-{index}-do-not-ship" for index, marker in enumerate(markers)
    }
    bypassed = _unredact(trajectory, secrets)

    _resource_spans, payload = convert_atif(bypassed)
    leaks = leaked_values(payload, secrets.values())
    assert leaks, "bypassing redaction must be visible in the span payload"

    roots = [span for span in iter_spans(payload) if not span.get("parentSpanId")]
    assert span_attribute(roots[0], "input.value") in secrets.values()

    # The guard above asserts spans still carry whole markers. Bypassed, they
    # carry none, so that assertion fails exactly when redaction is defeated.
    assert redaction_markers(payload) == ()

    # ...and the real document, run through the identical check, is clean.
    _real_spans, real_payload = convert_atif(trajectory)
    assert leaked_values(real_payload, secrets.values()) == ()
    assert redaction_markers(real_payload)


def test_a_marker_cut_short_by_attribute_truncation_is_detected() -> None:
    """Control for the truncation half of the guard.

    `atif2otel` truncates long attribute values. A marker sliced before its
    digest still reads as redacted, so the guard must not accept it.
    """
    trajectory = _evidence_trajectory()
    _real_spans, real_payload = convert_atif(trajectory)
    marker = redaction_markers(real_payload)[0]
    mangled = _unredact(trajectory, {marker: marker[: len(marker) // 2]})

    _resource_spans, payload = convert_atif(mangled)
    assert truncated_redaction_markers(real_payload) == ()
    assert truncated_redaction_markers(payload) != ()


# --------------------------------------------------------------------------
# 2. Trial -> trace.
# --------------------------------------------------------------------------


def test_trace_identity_matches_the_ids_the_converter_emits() -> None:
    trajectory = _evidence_trajectory()
    identity = trace_identity(trajectory)
    _resource_spans, payload = convert_atif(trajectory)

    spans = iter_spans(payload)
    assert {span["traceId"] for span in spans} == {identity.trace_id}
    roots = [span for span in spans if not span.get("parentSpanId")]
    assert roots[0]["spanId"] == identity.root_span_id


def test_trace_identity_session_is_the_span_session_attribute() -> None:
    trajectory = _evidence_trajectory()
    identity = trace_identity(trajectory)
    _resource_spans, payload = convert_atif(trajectory)

    assert identity.joinable
    assert identity.session_id == EVIDENCE_SESSION_ID
    assert root_session_id(payload) == identity.session_id


def test_trace_identity_for_trial_reads_the_committed_trial_directory() -> None:
    identity = trace_identity_for_trial(EVIDENCE_TRIAL)
    assert identity.session_id == EVIDENCE_SESSION_ID
    assert len(identity.trace_id) == 32
    assert len(identity.root_span_id) == 16


def test_continued_sessions_share_one_trace_but_not_one_session_id() -> None:
    """Why `session_id` is the span key and `base_session_id` is the trace key.

    Harbor writes a resumed session as `<base>-cont-N` and the converter strips
    the suffix when seeding the trace, so two documents with different
    `session_id` values land in the same Phoenix trace.
    """
    trajectory = _evidence_trajectory()
    continued = dict(trajectory, session_id=f"{EVIDENCE_SESSION_ID}-cont-1")

    first = trace_identity(trajectory)
    second = trace_identity(continued)

    assert first.session_id != second.session_id
    assert first.base_session_id == second.base_session_id == EVIDENCE_SESSION_ID
    assert first.trace_id == second.trace_id


def test_a_trajectory_without_a_session_id_is_not_joinable() -> None:
    trajectory = dict(_evidence_trajectory())
    trajectory.pop("session_id")
    identity = trace_identity(trajectory)

    assert identity.session_id is None
    assert not identity.joinable
    assert identity.trace_id  # still traceable, just not resolvable back


# --------------------------------------------------------------------------
# 3. Why `session_id` cannot carry a UNIQUE constraint.
# --------------------------------------------------------------------------


def _write_trial(root: Path, trajectory: dict[str, Any]) -> Path:
    """A minimal completed Harbor job with one trial holding `trajectory`."""
    job = root / "job"
    trial = job / "task__aaaaaaa"
    (trial / "agent").mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-0000000000aa",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
                "finished_at": "2026-08-16T00:00:00Z",
            }
        )
    )
    for name in ("config.json", "lock.json", "lab-metadata.json"):
        (job / name).write_text("{}")
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-0000000000bb",
                "task_name": "task",
                "trial_name": "task__aaaaaaa",
                "agent_name": "codex",
            }
        )
    )
    for name in ("config.json", "lock.json"):
        (trial / name).write_text("{}")
    (trial / "agent/trajectory.json").write_text(json.dumps(trajectory))
    return job


def test_embedded_subagents_produce_several_documents_with_one_session_id(
    tmp_path: Path,
) -> None:
    """The reason `sql/schema.sql` indexes `session_id` instead of constraining it.

    ATIF v1.7 embedded subagents share their parent's `session_id` and are
    disambiguated by `trajectory_id`. `project_trial` writes one
    `trajectory_documents` row per embedded payload, so `UNIQUE (session_id)`
    would not deduplicate a multi-agent trial — it would abort its ingest.
    """
    parent = _evidence_trajectory()
    child = dict(parent, trajectory_id="subagent-1")
    parent = dict(parent, trajectory_id="root", subagent_trajectories=[child])

    job = load_job(_write_trial(tmp_path, parent))
    documents = project_trial(job, job.trials[0]).trajectories

    assert len(documents) == 2
    assert {doc.session_id for doc in documents} == {EVIDENCE_SESSION_ID}
    assert len({doc.document_id for doc in documents}) == 2
    assert sorted(doc.embedded_path or "" for doc in documents) == [
        "",
        "subagent:subagent-1",
    ]


def test_schema_indexes_session_id_without_asserting_uniqueness() -> None:
    """A guard, not a style check: a future agent will be tempted by UNIQUE."""
    lines = (ROOT / "sql/schema.sql").read_text().splitlines()
    statements = " ".join(
        line for line in lines if not line.lstrip().startswith("--")
    ).upper()
    session_ddl = [
        part for part in statements.split(";") if "TRAJECTORY_DOCUMENTS" in part
    ]

    assert any(
        "CREATE INDEX IF NOT EXISTS TRAJECTORY_DOCUMENTS_SESSION_IDX" in part
        for part in session_ddl
    )
    assert not any("UNIQUE" in part for part in session_ddl if "SESSION_ID" in part)


# --------------------------------------------------------------------------
# 4. Span -> research graph, over the real join text.
# --------------------------------------------------------------------------

CATALOG_DDL = """
CREATE TABLE jobs (
    id text PRIMARY KEY,
    job_name text NOT NULL,
    evidence_path text NOT NULL,
    experiment_id text
);
CREATE TABLE trials (
    id text PRIMARY KEY,
    job_id text NOT NULL,
    trial_name text NOT NULL,
    agent_name text
);
CREATE TABLE trajectory_documents (
    id text PRIMARY KEY,
    trial_id text NOT NULL,
    session_id text,
    trajectory_id text,
    embedded_path text
);
"""


@pytest.fixture
def catalog() -> Any:
    """A DuckDB stand-in shaped like the catalog columns the join reads."""
    connection = duckdb.connect(":memory:")
    connection.execute(CATALOG_DDL)
    connection.execute(
        "INSERT INTO jobs VALUES "
        "('job-1', 'canary-event-summary-codex-20260815', "
        "'runs/canary-event-summary-codex-20260815', '01M021T5SMYY9E4EBCCMNF43A6')"
    )
    connection.execute(
        f"INSERT INTO trials VALUES ('{EVIDENCE_TRIAL_ID}', 'job-1', "
        "'event-summary__5E3btLv', 'codex')"
    )
    connection.execute(
        f"INSERT INTO trajectory_documents VALUES ('doc-root', '{EVIDENCE_TRIAL_ID}', "
        f"'{EVIDENCE_SESSION_ID}', NULL, NULL)"
    )
    try:
        yield connection
    finally:
        connection.close()


def _fetch(connection: Any):
    def run(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        return connection.execute(sql, list(params)).fetchall()

    return run


def test_a_span_session_id_resolves_to_experiment_job_and_trial(catalog: Any) -> None:
    trajectory = _evidence_trajectory()
    _resource_spans, payload = convert_atif(trajectory)
    session = root_session_id(payload)

    resolution = resolve_session(session, fetch=_fetch(catalog), placeholder="?")
    match = resolution.trial

    assert match.experiment_id == "01M021T5SMYY9E4EBCCMNF43A6"
    assert match.job_name == "canary-event-summary-codex-20260815"
    assert match.trial_id == EVIDENCE_TRIAL_ID
    # The committed evidence directory and the resolved trial are the same trial.
    assert match.trial_name == EVIDENCE_TRIAL.name
    assert match.agent_name == "codex"
    assert match.is_root_document


def test_subagent_fan_out_still_resolves_to_one_trial(catalog: Any) -> None:
    catalog.execute(
        f"INSERT INTO trajectory_documents VALUES ('doc-sub', '{EVIDENCE_TRIAL_ID}', "
        f"'{EVIDENCE_SESSION_ID}', 'subagent-1', 'subagent:subagent-1')"
    )
    resolution = resolve_session(
        EVIDENCE_SESSION_ID, fetch=_fetch(catalog), placeholder="?"
    )

    assert len(resolution.matches) == 2
    assert resolution.trial_ids == (EVIDENCE_TRIAL_ID,)
    assert resolution.trial.document_id == "doc-root"


def test_a_session_reaching_two_trials_is_refused_not_guessed(catalog: Any) -> None:
    """The failure a UNIQUE constraint was supposed to prevent, prevented here."""
    catalog.execute(
        "INSERT INTO trials VALUES ('other-trial', 'job-1', 'event-summary__zzzzzzz', 'codex')"
    )
    catalog.execute(
        f"INSERT INTO trajectory_documents VALUES ('doc-other', 'other-trial', "
        f"'{EVIDENCE_SESSION_ID}', NULL, NULL)"
    )
    resolution = resolve_session(
        EVIDENCE_SESSION_ID, fetch=_fetch(catalog), placeholder="?"
    )

    assert len(resolution.trial_ids) == 2
    with pytest.raises(TraceError, match="fans out to 2 trials"):
        _ = resolution.trial


def test_an_unknown_session_is_a_clear_error(catalog: Any) -> None:
    resolution = resolve_session("not-a-session", fetch=_fetch(catalog), placeholder="?")

    assert resolution.matches == ()
    with pytest.raises(TraceError, match="no catalog trajectory document"):
        _ = resolution.trial


def test_resolving_without_a_session_id_is_refused(catalog: Any) -> None:
    with pytest.raises(TraceError, match="session.id is required"):
        resolve_session(None, fetch=_fetch(catalog), placeholder="?")


def test_session_lookup_sql_reads_only_derived_catalog_tables() -> None:
    sql = session_lookup_sql()
    assert "%s" in sql
    assert session_lookup_sql("?").count("?") == 1
    # Read-only, and never a write path into the evidence zone.
    upper = sql.upper()
    assert upper.startswith("SELECT")
    assert not any(word in upper for word in ("INSERT", "UPDATE", "DELETE", "CREATE"))
