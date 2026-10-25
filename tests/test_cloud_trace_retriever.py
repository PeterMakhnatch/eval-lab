"""Synthetic fixtures only: no shared database, cloud I/O, or Docker.

The retriever is exercised against a real ``CloudTraceDatabase`` on temporary
SQLite files and against a duck-typed fake store for exclusion-order and
refusal paths. Environment database configuration is cleared for every test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.production import cloud_trace_retriever
from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.cloud_trace_retriever import (
    MAX_INCLUDED_HITS,
    MAX_PROMPT_CHARACTERS,
    CloudTraceRetriever,
    TraceRetrievalResult,
)

_PROVENANCE = {
    "source_path": "synthetic/fixtures.sqlite",
    "source_digest": "ab" * 32,
}


def retriever_for(store: Any) -> CloudTraceRetriever:
    """Caller-owned stores require explicit synthetic source attestation."""
    return CloudTraceRetriever(store, **_PROVENANCE)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "EVALLAB_CLOUD_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")


def task(task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "task_id": task_id, "dataset_id": "synthetic-fixture", "split": "dev",
        "instruction_text": "Compute the checksum output and report it.", **extra,
    }


def issue(issue_id: str, task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "issue_id": issue_id, "task_id": task_id, "defect_class": "insufficient_checking",
        "direction": "wrong_pass", "status": "confirmed",
        "citation_file": "tests/check.py", "citation_line": 23, **extra,
    }


def trace(trace_id: str, task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "trace_id": trace_id, "run_id": "run-1", "task_id": task_id,
        "model_id": "synthetic-agent", "outcome": "wrong_pass",
        "verified_receipt_json": {"status": "confirmed", "verifier": "synthetic"},
        **extra,
    }


def steps(text: str = "inspect checksum") -> list[dict[str, Any]]:
    return [
        {"step_index": 0, "role": "assistant", "tool_name": "terminal",
         "command_or_action": "cat result.txt", "content_text": text},
        {"step_index": 1, "role": "tool", "tool_name": "terminal",
         "observation_preview": "checksum observed"},
    ]


def seed(db: CloudTraceDatabase, task_id: str, trace_id: str, **task_extra: Any) -> None:
    db.upsert_task(task(task_id, **task_extra))
    db.upsert_task_issue(issue(f"issue-{task_id}", task_id))
    db.upsert_trace(trace(trace_id, task_id), steps=steps())


@pytest.fixture
def db(tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "traces.sqlite") as database:
        yield database


class FakeStore:
    """Duck-typed ``search_traces`` seam; hydrations are supplied verbatim."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.queries: list[str] = []

    def search_traces(self, query: str, *, only_verified: bool = False, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise AssertionError("store must not be queried")
        assert only_verified is True
        self.queries.append(query)
        return list(self.rows)


def row(
    trace_id: str,
    task_id: str,
    *,
    split: Any = "dev",
    receipt_status: Any = "confirmed",
    issue_status: str = "confirmed",
    issue_id: str | None = None,
    defect_class: str = "insufficient_checking",
    hydrated_task_id: str | None = None,
    step_text: str = "checksum probe",
) -> dict[str, Any]:
    hydrated_task: dict[str, Any] = {
        "task_id": task_id if hydrated_task_id is None else hydrated_task_id,
        "dataset_id": "synthetic-fixture", "instruction_text": "fixture",
    }
    if split is not ...:
        hydrated_task["split"] = split
    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "verified_receipt_json": (
            {"status": receipt_status} if isinstance(receipt_status, str) else receipt_status
        ),
        "task": hydrated_task,
        "issues": [] if issue_id is None else [{
            "issue_id": issue_id, "task_id": task_id, "status": issue_status,
            "defect_class": defect_class, "direction": "wrong_pass",
            "citation_file": "tests/check.py", "citation_line": 23,
        }],
        "steps": [{"step_index": 0, "role": "assistant", "tool_name": "terminal",
                   "content_text": step_text}],
    }


def test_retrieve_excludes_same_task_and_projects_safe_fields(db: CloudTraceDatabase) -> None:
    seed(db, "current-task", "trace-current")
    seed(db, "other-task", "trace-other")
    with retriever_for(db) as retriever:
        result = retriever.retrieve(task_id="current-task", query="checksum")
    assert isinstance(result, TraceRetrievalResult)
    assert [hit.task_id for hit in result.hits] == ["other-task"]
    hit = result.hits[0]
    assert hit.defect_class == "insufficient_checking"
    assert hit.mechanism == "wrong_pass insufficient_checking observed via terminal"
    assert hit.citation == "tests/check.py:23"
    assert hit.step_excerpt == "inspect checksum"
    receipt = result.receipt
    assert receipt["exclusion_counts"]["same_task"] == 1
    assert receipt["candidates"] == 2
    assert receipt["included_hits"] == 1
    assert receipt["trace_ids"] == ["trace-other"]
    assert receipt["issue_ids"] == ["issue-other-task"]
    assert receipt["provenance"] == "caller_attested"
    assert receipt["source_path"] == "synthetic/fixtures.sqlite"
    assert receipt["source_digest"] == "ab" * 32
    assert receipt["query"] == "checksum" and receipt["query_terms"] == ["checksum"]
    markdown = result.render_prompt_markdown()
    assert "other-task" in markdown and "current-task" not in markdown
    assert "trace-other" not in markdown  # IDs are receipt-only.


@pytest.mark.parametrize("split", ["test", "train", "heldout", ""])
def test_non_dev_splits_are_refused(split: Any) -> None:
    store = FakeStore([row("t1", "task-a", split=split, issue_id="i1")])
    result = retriever_for(store).retrieve(task_id="current-task", query="checksum")
    assert result.hits == ()
    assert result.receipt["exclusion_counts"]["non_dev_split"] == 1


def test_missing_split_and_case_normalized_dev(db: CloudTraceDatabase) -> None:
    missing = row("t1", "task-a", split=..., issue_id="i1")
    result = retriever_for(FakeStore([missing])).retrieve(task_id="current", query="checksum")
    assert result.hits == () and result.receipt["exclusion_counts"]["non_dev_split"] == 1
    seed(db, "upper-task", "trace-upper")
    db.upsert_task(task("upper-task", split="DEV"))  # case-normalized split still counts
    result = retriever_for(db).retrieve(task_id="current-task", query="checksum")
    assert [hit.task_id for hit in result.hits] == ["upper-task"]


def test_hydrated_task_mismatch_is_refused() -> None:
    store = FakeStore([row("t1", "task-a", issue_id="i1", hydrated_task_id="task-b")])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    assert result.hits == ()
    assert result.receipt["exclusion_counts"]["task_mismatch"] == 1


def test_issue_task_mismatch_is_filtered_per_issue() -> None:
    store = FakeStore([{
        "trace_id": "t1", "task_id": "task-a",
        "verified_receipt_json": {"status": "confirmed"},
        "task": {"task_id": "task-a", "split": "dev"},
        "issues": [
            {"issue_id": "i-alien", "task_id": "task-b", "status": "confirmed",
             "defect_class": "foreign_leak"},
            {"issue_id": "i-own", "task_id": "task-a", "status": "confirmed",
             "defect_class": "weak_check", "direction": "wrong_pass",
             "citation_file": "tests/x.py", "citation_line": 4},
        ],
        "steps": [],
    }])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    assert [hit.defect_class for hit in result.hits] == ["weak_check"]
    counts = result.receipt["exclusion_counts"]
    assert counts["issue_mismatch"] == 1
    assert result.receipt["issue_ids"] == ["i-own"]


def test_all_mismatched_issues_leave_no_confirmed_issue() -> None:
    store = FakeStore([{
        "trace_id": "t1", "task_id": "task-a",
        "verified_receipt_json": {"status": "confirmed"},
        "task": {"task_id": "task-a", "split": "dev"},
        "issues": [{"issue_id": "i-alien", "task_id": "task-b", "status": "confirmed"}],
        "steps": [],
    }])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    assert result.hits == ()
    counts = result.receipt["exclusion_counts"]
    assert counts["issue_mismatch"] == 1 and counts["no_confirmed_issue"] == 1


@pytest.mark.parametrize("receipt", [None, {}, {"status": "provisional"}, {"status": "refuted"}, {"note": "claimed"}])
def test_unverified_receipts_are_refused_independently_of_backend(receipt: Any) -> None:
    store = FakeStore([row("t1", "task-a", receipt_status=receipt, issue_id="i1")])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    assert result.hits == ()
    assert result.receipt["exclusion_counts"]["unverified_receipt"] == 1


@pytest.mark.parametrize("status", ["provisional", "refuted"])
def test_provisional_and_refuted_issues_are_not_useful(status: str) -> None:
    rows = [
        row("t1", "task-a", issue_status=status, issue_id="i1"),
        row("t2", "task-b", issue_id=None),  # no issues at all
    ]
    result = retriever_for(FakeStore(rows)).retrieve(task_id="current", query="checksum")
    assert result.hits == ()
    assert result.receipt["exclusion_counts"]["no_confirmed_issue"] == 2


def test_confirmed_issue_alongside_refuted_sibling_is_used() -> None:
    store = FakeStore([{
        "trace_id": "t1", "task_id": "task-a",
        "verified_receipt_json": {"status": "verified"},
        "task": {"task_id": "task-a", "split": "dev"},
        "issues": [
            {"issue_id": "i-refuted", "task_id": "task-a", "status": "refuted",
             "defect_class": "noise"},
            {"issue_id": "i-confirmed", "task_id": "task-a", "status": "confirmed",
             "defect_class": "weak_check", "direction": "unfair_fail",
             "citation_file": "tests/x.py"},
        ],
        "steps": [],
    }])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    assert [hit.defect_class for hit in result.hits] == ["weak_check"]
    hit = result.hits[0]
    assert hit.mechanism == "unfair_fail weak_check"
    assert hit.citation == "tests/x.py" and hit.step_excerpt is None
    assert result.receipt["issue_ids"] == ["i-confirmed"]


@pytest.mark.parametrize("query", ["", "   \n\t  ", "-- !! ;; %%", "…?!。", "***"])
def test_blank_and_punctuation_queries_never_touch_store(query: str) -> None:
    store = FakeStore(fail=True)
    retriever = retriever_for(store)
    with pytest.raises(ValueError):
        retriever.retrieve(task_id="current-task", query=query)
    assert store.queries == []


@pytest.mark.parametrize("task_id", ["", "   ", None, 42])
def test_blank_task_ids_never_touch_store(task_id: Any) -> None:
    store = FakeStore(fail=True)
    with pytest.raises(ValueError):
        retriever_for(store).retrieve(task_id=task_id, query="checksum")
    assert store.queries == []


def test_query_normalization_is_deterministic() -> None:
    store = FakeStore([row("t1", "task-a", issue_id="i1")])
    retriever = retriever_for(store)
    result = retriever.retrieve(task_id="current", query="  checksum\tsome\t cue!! ")
    assert store.queries == ["checksum some cue"]
    assert result.receipt["query"] == "checksum some cue"
    assert result.receipt["query_terms"] == ["checksum", "some", "cue"]


def test_overfetch_invalid_early_candidates_do_not_starve_valid_rows() -> None:
    rows = [row(f"same-{index}", "current-task", issue_id="i") for index in range(6)]
    rows += [
        row("bad-split", "task-s", split="test", issue_id="i"),
        row("bad-receipt", "task-r", receipt_status=None, issue_id="i"),
        row("bad-issue", "task-i", issue_status="refuted", issue_id="i"),
        "not-a-mapping",
    ]
    rows += [row(f"good-{index}", f"task-{index}", issue_id=f"i-{index}") for index in range(6)]
    result = retriever_for(FakeStore(rows)).retrieve(task_id="current-task", query="checksum")
    assert [hit.task_id for hit in result.hits] == [f"task-{index}" for index in range(6)]
    counts = result.receipt["exclusion_counts"]
    assert counts["same_task"] == 6 and counts["non_dev_split"] == 1
    assert counts["unverified_receipt"] == 1 and counts["no_confirmed_issue"] == 1
    assert counts["task_mismatch"] == 1
    assert result.receipt["candidates"] == len(rows)


def test_hit_cap_counts_admissible_beyond_eight() -> None:
    rows = [row(f"same-{index}", "current-task", issue_id="i") for index in range(4)]
    rows += [row(f"good-{index}", f"task-{index}", issue_id=f"i-{index}") for index in range(11)]
    result = retriever_for(FakeStore(rows)).retrieve(task_id="current-task", query="checksum")
    assert len(result.hits) == MAX_INCLUDED_HITS == 8
    assert result.receipt["exclusion_counts"]["hit_cap"] == 3
    assert result.receipt["exclusion_counts"]["same_task"] == 4
    assert len(result.receipt["trace_ids"]) == 8
    assert len(result.receipt["issue_ids"]) == 8


def test_identical_projections_are_deduped() -> None:
    rows = [
        row("t1", "task-a", issue_id="i-first"),
        row("t1-dup", "task-a", issue_id="i-second"),  # same projected fields
        row("t2", "task-b", issue_id="i-b", defect_class="other_class"),
    ]
    result = retriever_for(FakeStore(rows)).retrieve(task_id="current", query="checksum")
    assert len(result.hits) == 2
    assert result.receipt["issue_ids"] == ["i-first", "i-b"]
    assert result.receipt["exclusion_counts"]["duplicate"] == 1


def test_candidates_issues_and_steps_are_sorted_deterministically() -> None:
    rows = [
        row("t2", "task-b", issue_id="i-b", defect_class="other_class"),
        {
            "trace_id": "t1", "task_id": "task-a",
            "verified_receipt_json": {"status": "confirmed"},
            "task": {"task_id": "task-a", "split": "dev"},
            "issues": [  # reverse issue_id order
                {"issue_id": "i-2", "task_id": "task-a", "status": "confirmed",
                 "defect_class": "second_class", "direction": "wrong_pass"},
                {"issue_id": "i-1", "task_id": "task-a", "status": "confirmed",
                 "defect_class": "first_class", "direction": "wrong_pass"},
            ],
            "steps": [  # reverse step_index order
                {"step_index": 3, "role": "tool", "content_text": "late step text"},
                {"step_index": 0, "role": "tool", "content_text": "checksum early"},
            ],
        },
    ]
    forward = retriever_for(FakeStore(rows)).retrieve(task_id="current", query="checksum")
    assert [hit.task_id for hit in forward.hits] == ["task-a", "task-a", "task-b"]
    assert [hit.defect_class for hit in forward.hits[:2]] == ["first_class", "second_class"]
    assert forward.hits[0].step_excerpt == "checksum early"
    assert forward.receipt["trace_ids"] == ["t1", "t2"]
    assert forward.receipt["issue_ids"] == ["i-1", "i-2", "i-b"]
    backward = retriever_for(FakeStore(list(reversed(rows)))).retrieve(
        task_id="current", query="checksum",
    )
    assert backward == forward


def test_deterministic_result_and_rendered_cap() -> None:
    rows = [
        row(
            f"good-{index}", f"task-{index}", issue_id=f"i-{index}",
            defect_class="weak_check " + "x" * 2000,
            step_text="checksum " + "y" * 2000,
        )
        for index in range(9)
    ]
    retriever = retriever_for(FakeStore(rows))
    first = retriever.retrieve(task_id="current", query="checksum")
    second = retriever.retrieve(task_id="current", query="checksum")
    assert first == second
    assert len(first.hits) == 8
    for hit in first.hits:
        assert len(hit.defect_class) <= 120
        assert len(hit.mechanism) <= 200
        assert hit.step_excerpt is not None and len(hit.step_excerpt) <= 240
    markdown = first.render_prompt_markdown()
    assert markdown.startswith("## Related verified DEV evidence")
    assert len(markdown) <= MAX_PROMPT_CHARACTERS == 4500
    for hit in first.hits:  # every included hit keeps its bullet
        assert f"`{hit.task_id}`" in markdown
    serialized = json.dumps(first.to_dict(), sort_keys=True)
    deserialized = json.dumps(second.to_dict(), sort_keys=True)
    assert serialized == deserialized


def test_rendered_char_cap_excludes_unfittable_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row(f"t{index}", f"task-{index}", issue_id=f"i-{index}") for index in range(6)]
    monkeypatch.setattr(cloud_trace_retriever, "MAX_PROMPT_CHARACTERS", 400)
    result = retriever_for(FakeStore(rows)).retrieve(task_id="current", query="checksum")
    counts = result.receipt["exclusion_counts"]
    assert 0 < len(result.hits) < 6
    assert len(result.hits) + counts["rendered_char_cap"] == 6
    markdown = result.render_prompt_markdown()
    assert len(markdown) <= 400
    assert len(result.receipt["trace_ids"]) == len(result.hits)
    for hit in result.hits:
        assert f"`{hit.task_id}`" in markdown  # receipt IDs match rendered bullets
    assert "task-5" not in markdown


def test_empty_result_renders_usable_markdown() -> None:
    result = retriever_for(FakeStore([])).retrieve(task_id="current", query="checksum")
    assert result.hits == ()
    markdown = result.render_prompt_markdown()
    assert markdown.startswith("## Related verified DEV evidence")
    assert "No verified DEV trace evidence" in markdown
    assert len(markdown) <= MAX_PROMPT_CHARACTERS


def test_markdown_escapes_injection_and_strips_control() -> None:
    store = FakeStore([{
        "trace_id": "t1", "task_id": "task-a",
        "verified_receipt_json": {"status": "confirmed"},
        "task": {"task_id": "task-a", "split": "dev"},
        "issues": [{
            "issue_id": "i1", "task_id": "task-a", "status": "confirmed",
            "defect_class": "weak *check*\n[evil](https://x) <img>\x00\x07",
            "direction": "wrong_pass", "citation_file": "tests/x.py",
        }],
        "steps": [{"step_index": 0, "role": "tool", "content_text": "line1\nline2\x00end"}],
    }])
    result = retriever_for(store).retrieve(task_id="current", query="checksum")
    markdown = result.render_prompt_markdown()
    assert "[evil](https://x)" not in markdown
    assert "\\*check\\*" in markdown and "\\[evil\\]" in markdown
    assert "\\<img\\>" in markdown
    assert "\x00" not in markdown and "\x07" not in markdown
    assert "line1\nline2" not in markdown
    assert "line1 line2 end" in markdown  # controls blank, never join words
    serialized = json.dumps(result.to_dict())
    assert "\x00" not in serialized and "line1\\nline2" not in serialized


def test_no_full_trace_or_cas_leakage_in_result_or_markdown(db: CloudTraceDatabase) -> None:
    sentinels = {
        "instruction": "ZZINSTRSENTINEL9 " * 300,
        "script": "ZZSCRIPTSENTINEL9 " * 300,
        "receipt": "ZZRECEIPTSENTINEL9",
        "step": "ZZSTEPSENTINEL9 " * 300,
        "metadata": "ZZMETASENTINEL9",
        "raw_cas": "cas://sha256/" + "9" * 64,
        "obs_cas": "cas://sha256/" + "8" * 64,
        "task_cas": "cas://sha256/" + "7" * 64,
    }
    db.upsert_task(task("current-task", instruction_text="checksum current instruction"))
    db.upsert_task_issue(issue("i-current", "current-task"))
    db.upsert_trace(trace("trace-current", "current-task"), steps=steps())
    db.upsert_task(task(
        "source-task",
        instruction_text="checksum " + sentinels["instruction"],
        metadata_json={"note": sentinels["metadata"]},
        cas_uri=sentinels["task_cas"],
    ))
    db.upsert_task_issue(issue(
        "issue-source", "source-task",
        reproducer_script=sentinels["script"],
        witness_digest="sha256:" + "a" * 64,
        cas_uri="cas://sha256/" + "b" * 64,
    ))
    db.upsert_trace(trace(
        "trace-source", "source-task",
        raw_cas_uri=sentinels["raw_cas"],
        verified_receipt_json={"status": "confirmed", "note": sentinels["receipt"]},
    ), steps=[
        {"step_index": 0, "role": "assistant", "tool_name": "terminal",
         "content_text": "checksum short benign"},
        {"step_index": 1, "role": "tool", "content_text": sentinels["step"],
         "observation_cas_uri": sentinels["obs_cas"]},
        {"step_index": 2, "role": "tool", "content_text": "more " + sentinels["step"]},
    ])
    result = retriever_for(db).retrieve(task_id="current-task", query="checksum")
    assert [hit.task_id for hit in result.hits] == ["source-task"]
    serialized = json.dumps(result.to_dict(), ensure_ascii=True)
    markdown = result.render_prompt_markdown()
    for blob in (serialized, markdown):
        for sentinel in sentinels.values():
            assert sentinel not in blob
        for forbidden_key in (
            "instruction_text", "reproducer_script", "raw_cas_uri", "observation_cas_uri",
            "verified_receipt_json", "metadata_json", "witness_digest", "flags_json",
            "step_index", "command_or_action", "observation_preview",
        ):
            assert forbidden_key not in blob
        assert "cas://sha256/" not in blob
    assert result.hits[0].step_excerpt == "checksum short benign"
    assert result.receipt["exclusion_counts"]["same_task"] == 1


def test_caller_owned_database_is_never_closed(db: CloudTraceDatabase) -> None:
    seed(db, "source-task", "trace-source")
    retriever = retriever_for(db)
    assert retriever.retrieve(task_id="current-task", query="checksum").hits
    retriever.close()
    with retriever:  # context exit also leaves the caller's database open
        pass
    assert db.get_task("source-task") is not None
    assert db.search_traces("checksum")


def test_path_snapshot_never_mutates_source_and_reports_receipt(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as setup:
        seed(setup, "source-task", "trace-source")
        seed(setup, "current-task", "trace-current")
    before = source.read_bytes()
    with CloudTraceRetriever(source) as retriever:
        result = retriever.retrieve(task_id="current-task", query="checksum")
        assert retriever.source_digest == result.receipt["source_digest"]
        assert result.receipt["provenance"] == "snapshot"
        assert result.receipt["source_path"] == str(source.resolve())
        assert retriever.source_path == str(source.resolve())
        assert [hit.task_id for hit in result.hits] == ["source-task"]
    assert source.read_bytes() == before  # never opened for write
    with CloudTraceDatabase(source) as reopened:  # original stays fully usable
        assert reopened.get_task("source-task") is not None
        reopened.upsert_task(task("post-check"))
        assert reopened.get_task("post-check") is not None


def test_snapshot_digest_is_deterministic_and_content_bound(tmp_path: Path) -> None:
    digests: list[str] = []
    paths = [tmp_path / f"source-{index}.sqlite" for index in range(2)]
    for path, marker in zip(paths, ("alpha", "beta"), strict=True):
        with CloudTraceDatabase(path) as setup:
            seed(setup, "source-task", "trace-source")
            setup.upsert_task(task("marker-task", instruction_text=f"checksum {marker}"))
        for _ in range(2):
            with CloudTraceRetriever(path) as retriever:
                digest = retriever.source_digest
                assert digest is not None and re.fullmatch(r"[0-9a-f]{64}", digest)
                digests.append(digest)
    assert digests[0] == digests[1] and digests[2] == digests[3]
    assert digests[0] != digests[2]
    for path in paths:
        assert path.exists()  # snapshots are private temporaries elsewhere


def test_constructor_requires_explicit_source_and_refuses_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'env.sqlite'}")
    monkeypatch.setenv("EVALLAB_CLOUD_DATABASE_URL", f"sqlite:///{tmp_path / 'env.sqlite'}")
    with pytest.raises(ValueError, match="environment"):
        CloudTraceRetriever()
    for url in ("postgresql://user:secret@host/db", "sqlite:////tmp/x.sqlite", "file:///tmp/x.sqlite"):
        with pytest.raises(ValueError):
            CloudTraceRetriever(url)
    with pytest.raises(FileNotFoundError):
        CloudTraceRetriever(tmp_path / "missing.sqlite")
    with pytest.raises(ValueError, match="not a file"):
        CloudTraceRetriever(tmp_path)


def test_caller_owned_requires_explicit_valid_provenance(db: CloudTraceDatabase) -> None:
    with pytest.raises(ValueError, match="provenance"):
        CloudTraceRetriever(db)
    with pytest.raises(ValueError, match="provenance"):
        CloudTraceRetriever(db, source_path="snapshot.sqlite")
    with pytest.raises(ValueError, match="provenance"):
        CloudTraceRetriever(db, source_digest="ab" * 32)
    for bad_path in ("", "   ", "postgresql://u:p@host/db", "file:///x", "sqlite:///x"):
        with pytest.raises(ValueError, match="source_path"):
            CloudTraceRetriever(db, source_path=bad_path, source_digest="ab" * 32)
    for bad_digest in ("", "ab" * 31, "AB" * 32, "z" * 64, 12):
        with pytest.raises(ValueError, match="source_digest"):
            CloudTraceRetriever(db, source_path="snapshot.sqlite", source_digest=bad_digest)
    retriever = CloudTraceRetriever(db, source_path="snapshot.sqlite", source_digest="ab" * 32)
    assert retriever.source_path == "snapshot.sqlite"
    assert retriever.source_digest == "ab" * 32
    db.get_task("missing")  # still usable


def test_path_mode_rejects_manual_provenance(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source):
        pass
    with pytest.raises(ValueError, match="derived provenance"):
        CloudTraceRetriever(source, source_path="other.sqlite", source_digest="ab" * 32)
