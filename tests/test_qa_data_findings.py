"""Synthetic DEV admission, streaming projections and budgets for QA findings."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evallab.production import qa_data_findings as findings_module
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens
from evallab.production.qa_data_findings import findings


class SyntheticRegistry(CorpusRegistry):
    """Real admission policy with synthetic assignment authority and TEST locks."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = {"cross-key-task"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[QaContext]:
    with CloudTraceDatabase(tmp_path / "cloud.sqlite", sqlite_fts=False) as database:
        for task_id, split, run_names in (
            ("dev-task", "dev", ("one", "two", "three")),
            ("other-dev-task", "dev", ("one",)),
            ("test-task", "test", ("one",)),
            ("cross-key-task", "dev", ("one",)),
            ("sealed-task", "dev", ("one",)),
            ("empty-task", "dev", ()),
        ):
            database.upsert_task({
                "task_id": task_id, "dataset_id": "secret-synthetic-dataset", "split": split,
                "instruction_text": "synthetic task content",
            })
            for name in run_names:
                database.upsert_trace({
                    "trace_id": f"{task_id}/{name}", "run_id": "batch-is-not-a-trace",
                    "task_id": task_id, "model_id": "synthetic-model",
                }, steps=[])
        yield QaContext(
            database=database,
            retriever=CloudTraceRetriever(
                database, registry=SyntheticRegistry(), sealed_task_ids=["sealed-task"],
            ),
            repo_root=tmp_path, state_dir=tmp_path / "state", budget=Budget(10_000),
        )


def result(run_id: str = "dev-task/one", **changes: Any) -> dict[str, Any]:
    return {
        "schema": "evallab.trace_tool_result/v1", "run_id": run_id,
        "source": "cloud-trace-db", "tool": "scout", "detector": "repeat",
        "detector_version": "synthetic/v1", "value": 3, "flag": False,
        "evidence_steps": [1, 3], "scan": "private/opaque-keymap-id",
        "at": "2026-10-28T02:00:00Z", **changes,
    }


def ledger(ctx: QaContext, *records: Any, name: str = "results-v1.jsonl") -> Path:
    path = ctx.state_dir / "trace-tools" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        (record if isinstance(record, str) else json.dumps(record)) + "\n"
        for record in records
    ), encoding="utf-8")
    return path


def flattened(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [record for group in payload["findings"].values() for record in group]


@pytest.mark.parametrize("argument", ["run_id", "task_id"])
@pytest.mark.parametrize("identity", ["missing-task", "test-task", "cross-key-task", "sealed-task"])
def test_non_admitted_identities_refuse_uniformly_before_opening_ledgers(
    ctx: QaContext, monkeypatch: pytest.MonkeyPatch, argument: str, identity: str,
) -> None:
    def deny_open(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an unavailable identity must not open a content file")

    monkeypatch.setattr(Path, "open", deny_open)
    arguments: dict[str, Any] = {argument: identity + ("/one" if argument == "run_id" else "")}
    with pytest.raises(QaDataError) as error:
        findings(ctx, **arguments)
    assert str(error.value) == UNAVAILABLE


@pytest.mark.parametrize("arguments", [{}, {"run_id": "dev-task/one", "task_id": "dev-task"}])
def test_requires_exactly_one_identity(ctx: QaContext, arguments: dict[str, Any]) -> None:
    with pytest.raises(QaDataError, match="exactly one"):
        findings(ctx, **arguments)


def test_invalid_limit_is_rejected(ctx: QaContext) -> None:
    with pytest.raises(QaDataError, match="limit"):
        findings(ctx, task_id="dev-task", limit=0)


def test_task_joins_exact_trace_ids_groups_and_sorts_with_pre_budget_counts(ctx: QaContext) -> None:
    ledger(
        ctx,
        result(detector="z-unflagged"),
        result(detector="z-flagged", flag=True),
        result(detector="a-flagged", flag=True),
        result("dev-task/two", tool="metr", detector="lead", flag=None),
        result("other-dev-task/one", flag=True),
        result("test-task/one", flag=True),
        result("batch-is-not-a-trace", source="own-harbor", flag=True),
        result("opaque-id", value={"run_id": "dev-task/one"}, flag=True),
    )
    payload = findings(ctx, task_id="dev-task")
    assert list(payload["findings"]) == ["dev-task/one", "dev-task/two"]
    assert [record["detector"] for record in payload["findings"]["dev-task/one"]] == [
        "a-flagged", "z-flagged", "z-unflagged",
    ]
    assert payload["counts"] == {
        "scout": {"total": 3, "flagged": 2}, "metr": {"total": 1, "flagged": 0},
    }
    assert payload["coverage"]["requested_runs"] == 3
    assert payload["coverage"]["runs_with_findings"] == 2
    assert "not a clean" in payload["coverage"]["note"]
    assert payload["omitted"] == 0
    assert payload["truncated"] is False


def test_single_run_reads_only_that_run_even_when_other_runs_are_admitted(ctx: QaContext) -> None:
    ledger(ctx, result(), result("dev-task/two"), result("other-dev-task/one"))
    payload = findings(ctx, run_id="dev-task/one")
    assert [record["run_id"] for record in flattened(payload)] == ["dev-task/one"]
    assert payload["coverage"]["requested_runs"] == 1


def test_tool_allow_lists_drop_raw_labels_evaluator_prose_and_dataset_ids(ctx: QaContext) -> None:
    evaluator = "EVALUATOR_ONLY_SECRET " * 100
    dataset = "secret-synthetic-dataset"
    opaque = "opaque-private-keymap-uuid"
    ledger(
        ctx,
        result(tool="docent", flag=True, value={
            "reward_hacking": True, "mechanism": "test manipulation", "evidence_step": 3,
            "confidence": 0.9, "evaluator_explanation": evaluator, "dataset_name": dataset,
            "label": "hidden-positive", "keymap": opaque,
            "served_model": {"dataset_id": dataset, "payload": evaluator},
        }),
        result(tool="scout", detector="small-fields", value={
            "count": 4, "status": "recorded", "explanation": evaluator, "dataset": dataset,
            "labels": ["hidden-positive"], "keymap": opaque,
        }),
        result(tool="laminar", detector="signal", value={
            "observations": [{
                "flag": True, "status": "COMPLETED", "signal_name": "repetition",
                "explanation": evaluator, "payload": dataset, "trace_id": opaque,
                "event_payload": evaluator, "signal_run": {"trace_id": opaque},
            }],
            "dataset_id": dataset, "label": "hidden-positive",
        }),
        result(tool="unrecognized", value={"explanation": evaluator, "dataset": dataset}),
    )
    payload = findings(ctx, run_id="dev-task/one")
    by_tool = {record["tool"]: record for record in flattened(payload)}
    assert by_tool["docent"]["summary"] == {
        "reward_hacking": True, "mechanism": "test manipulation", "evidence_step": 3, "confidence": 0.9,
    }
    assert by_tool["scout"]["summary"] == {"count": 4, "status": "recorded"}
    assert by_tool["laminar"]["summary"] == {
        "observations": [{"flag": True, "status": "COMPLETED", "signal_name": "repetition"}],
    }
    assert by_tool["unrecognized"]["summary"] == {}
    serialized = json.dumps(payload)
    for secret in ("EVALUATOR_ONLY_SECRET", dataset, opaque, "hidden-positive", "private/opaque"):
        assert secret not in serialized
    assert all(set(record) == {
        "tool", "detector", "run_id", "flag", "evidence_steps", "at", "summary",
    } for record in flattened(payload))


def test_summary_strings_are_capped_and_known_source_identities_redacted(ctx: QaContext) -> None:
    ledger(
        ctx,
        result(tool="docent", value={"mechanism": "x" * 400, "confidence": "high"}),
        result(tool="docent", detector="dataset-reference", value={
            "mechanism": "Copied from SECRET-SYNTHETIC-DATASET", "reward_hacking": True,
        }),
        result(tool="metr", detector="source-reference", source="hidden-benchmark-name", value={"status": "hidden-benchmark-name"}),
    )
    records = {record["detector"]: record for record in flattened(findings(ctx, run_id="dev-task/one"))}
    assert records["repeat"]["summary"]["mechanism"] == "x" * 200
    assert records["dataset-reference"]["summary"]["mechanism"] == "[withheld]"
    assert "hidden-benchmark-name" not in json.dumps(records)


def test_streams_pretty_docent_arms_and_laminar_own_with_exact_joins(ctx: QaContext) -> None:
    ledger(ctx, result())
    ledger(ctx, result(source="own-harbor", tool="laminar", value={
        "observations": [{"flag": False, "signal_run": {"status": "COMPLETED", "trace_id": "opaque"}}],
    }), result("source-only-run", source="own-harbor", tool="laminar"), name="laminar-own.jsonl")
    arms = ctx.state_dir / "trace-tools/tt7/docent/arms"
    arms.mkdir(parents=True)
    (arms / "a-canonical.json").write_text(json.dumps([
        result("dev-task/two", tool="docent", flag=True, value={"mechanism": 'nested {"quoted": true} \\ text'}),
        result("test-task/one", tool="docent", flag=True),
    ], indent=2), encoding="utf-8")
    (arms / "b-canonical.json").write_text(json.dumps(
        result(tool="docent", detector="singleton", value={"confidence": "low"}),
    ), encoding="utf-8")
    payload = findings(ctx, task_id="dev-task")
    assert payload["counts"] == {
        "docent": {"total": 2, "flagged": 1},
        "laminar": {"total": 1, "flagged": 0}, "scout": {"total": 1, "flagged": 0},
    }
    assert {record["run_id"] for record in flattened(payload)} == {"dev-task/one", "dev-task/two"}
    laminar = next(record for record in flattened(payload) if record["tool"] == "laminar")
    assert laminar["summary"] == {"observations": [{"flag": False, "status": "COMPLETED"}]}


def test_whole_record_budget_and_limit_preserve_unbudgeted_counts(ctx: QaContext) -> None:
    ledger(ctx, *(result(detector=f"detector-{index}", flag=index == 0) for index in range(12)))
    bounded = replace(ctx, budget=Budget(400))
    payload = findings(bounded, run_id="dev-task/one", limit=10)
    records = flattened(payload)
    assert 0 < len(records) < 10
    assert payload["counts"] == {"scout": {"total": 12, "flagged": 1}}
    assert payload["omitted"] == 12 - len(records)
    assert payload["truncated"] is True
    assert bounded.budget.omitted == payload["omitted"]
    assert bounded.budget.used <= bounded.budget.limit
    assert estimate_tokens(payload) <= bounded.budget.limit
    assert all(set(record) == {
        "tool", "detector", "run_id", "flag", "evidence_steps", "at", "summary",
    } for record in records)
    limited = findings(replace(ctx, budget=Budget(10_000)), run_id="dev-task/one", limit=2)
    assert len(flattened(limited)) == 2
    assert limited["omitted"] == 10
    assert limited["counts"] == payload["counts"]


def test_too_small_budget_returns_minimal_coverage_and_no_findings(ctx: QaContext) -> None:
    ledger(ctx, result())
    bounded = replace(ctx, budget=Budget(60))
    payload = findings(bounded, run_id="dev-task/one")
    assert payload == {
        "findings": {}, "truncated": True,
        "coverage": {"requested_runs": 1, "runs_with_findings": 1},
    }
    assert estimate_tokens(payload) <= bounded.budget.limit
    assert bounded.budget.used <= bounded.budget.limit


def test_torn_invalid_and_invalid_schema_lines_are_silently_ignored(ctx: QaContext) -> None:
    path = ledger(
        ctx, result(), "{not-json", '{"run_id":"dev-task/one","bad":}',
        result(schema="unsupported"), result(evidence_steps=["opaque-step-id"]),
        {**result(), "run_id": ["dev-task/one"]}, "",
    )
    with path.open("ab") as stream:
        stream.write(b'{"run_id":"dev-task/one","torn":"\xe2\x82')
    payload = findings(ctx, run_id="dev-task/one")
    assert len(flattened(payload)) == 1
    assert "skipped_lines" not in payload
    assert "oversized_records" not in payload


def test_invalid_canonical_tail_does_not_discard_valid_prefix(ctx: QaContext) -> None:
    path = ctx.state_dir / "trace-tools/tt7/docent/arms/torn-canonical.json"
    path.parent.mkdir(parents=True)
    path.write_text("[" + json.dumps(result(tool="docent")) + ', {"run_id":"dev-task/one",', encoding="utf-8")
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"docent": {"total": 1, "flagged": 0}}
    assert "skipped_lines" not in payload


def test_empty_or_absent_ledgers_do_not_imply_clean_runs(ctx: QaContext) -> None:
    payload = findings(ctx, task_id="dev-task")
    assert payload["findings"] == {}
    assert payload["counts"] == {}
    assert payload["coverage"]["requested_runs"] == 3
    assert payload["coverage"]["runs_with_findings"] == 0
    assert payload["truncated"] is False
    assert "skipped_lines" not in payload
    assert findings(replace(ctx, budget=Budget(10_000)), task_id="empty-task")["coverage"]["requested_runs"] == 0


def test_calls_do_not_memoize_ledger_contents(ctx: QaContext) -> None:
    path = ledger(ctx, result())
    assert len(flattened(findings(ctx, run_id="dev-task/one"))) == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result(detector="new-detector")) + "\n")
    payload = findings(replace(ctx, budget=Budget(10_000)), run_id="dev-task/one")
    assert len(flattened(payload)) == 2


def test_unrelated_malformed_lines_never_affect_visible_counters(ctx: QaContext) -> None:
    path = ledger(ctx, result())
    baseline = findings(ctx, run_id="dev-task/one")
    with path.open("ab") as stream:
        stream.write(b'{broken unrelated line\n{"run_id":"test-task/one","bad":}\n')
        stream.write(b'{"run_id":"sealed-task/one","torn":"\xe2\x82\n')
        stream.write(b'{"note":"dev-task/one","run_id":"test-task/one","bad":}\n')
    arms = ctx.state_dir / "trace-tools/tt7/docent/arms"
    arms.mkdir(parents=True)
    (arms / "unrelated-canonical.json").write_text(
        '[{"run_id":"cross-key-task/one","bad":', encoding="utf-8",
    )
    payload = findings(replace(ctx, budget=Budget(10_000)), run_id="dev-task/one")
    assert payload == baseline
    assert "skipped_lines" not in payload


@pytest.mark.parametrize("secret", [
    "Evidence from SEALED-TASK: answer is hidden",
    "Evidence from cross-key-task: answer is hidden",
    "keymap=private-opaque-id",
    "0123456789abcdef0123456789abcdef",
    "run-deadbeef12345678",
    "run-a1b2",
    "sha256:" + "a" * 64,
    "66284ee6-f7ba-fde3-4552-e2f3d13b3a3e",
])
def test_free_form_tool_strings_withhold_sealed_and_opaque_material(
    ctx: QaContext, secret: str,
) -> None:
    ledger(ctx, result(tool="docent", value={"mechanism": secret, "confidence": "high"}))
    payload = findings(ctx, run_id="dev-task/one")
    assert flattened(payload)[0]["summary"] == {"mechanism": "[withheld]", "confidence": "high"}
    assert secret not in json.dumps(payload)
    assert payload["withheld_strings"] == 1


def test_string_screen_also_covers_headers_errors_and_laminar_text(ctx: QaContext) -> None:
    ledger(
        ctx,
        result(tool="docent", detector="sealed-task", at="keymap=private", value={
            "error": "test-task answer bytes",
        }),
        result(tool="laminar", value={"observations": [{
            "flag": True, "status": "cross-key-task", "signal_name": "run-deadbeef",
        }]}),
        result(tool="keymap=private-tool", detector="tool-header"),
    )
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["withheld_strings"] == 6
    serialized = json.dumps(payload)
    for secret in ("sealed-task", "test-task", "cross-key-task", "private", "run-deadbeef"):
        assert secret not in serialized
    assert any(record["tool"] == "[withheld]" for record in flattened(payload))


def test_admitted_db_run_identity_is_not_an_opaque_ledger_token(ctx: QaContext) -> None:
    identity = "dev-task/0123456789abcdef0123456789abcdef"
    ctx.database.upsert_trace({
        "trace_id": identity, "run_id": "batch", "task_id": "dev-task", "model_id": "synthetic",
    }, steps=[])
    ledger(ctx, result(identity, tool="docent", value={"mechanism": identity}))
    payload = findings(ctx, run_id=identity)
    assert list(payload["findings"]) == [identity]
    assert flattened(payload)[0]["run_id"] == identity
    assert flattened(payload)[0]["summary"] == {"mechanism": "[withheld]"}
    assert payload["withheld_strings"] == 1


def test_sealed_screen_still_applies_to_canonical_join_identities(ctx: QaContext) -> None:
    registry = ctx.retriever.registry
    assert isinstance(registry, SyntheticRegistry)
    registry._sealed_names.add("one")
    ledger(ctx, result())
    payload = findings(ctx, run_id="dev-task/one")
    assert list(payload["findings"]) == ["run#1"]
    assert flattened(payload)[0]["run_id"] == "run#1"
    assert "dev-task/one" not in json.dumps(payload)
    assert payload["withheld_strings"] == 1


def test_laminar_mirrors_are_deduplicated_before_counts_limit_and_budget(ctx: QaContext) -> None:
    mirrored = result(tool="laminar", flag=True, value={
        "observations": [{"flag": True, "status": "sealed-task"}],
    })
    ledger(ctx, mirrored)
    ledger(
        ctx, {**mirrored, "at": "2026-10-28T03:00:00Z"}, mirrored,
        {**mirrored, "detector_version": "synthetic/v2"},
        {**mirrored, "scan": "another-scan"}, name="laminar-own.jsonl",
    )
    payload = findings(ctx, run_id="dev-task/one", limit=2)
    assert payload["counts"] == {"laminar": {"total": 4, "flagged": 4}}
    assert len(flattened(payload)) == 2
    assert payload["omitted"] == 2
    assert payload["withheld_strings"] == 4


def test_mirrored_findings_with_null_scan_metadata_are_still_deduplicated(ctx: QaContext) -> None:
    mirrored = result(tool="laminar", detector_version=None, scan=None, value={"observations": []})
    ledger(ctx, mirrored)
    ledger(ctx, mirrored, name="laminar-own.jsonl")
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"laminar": {"total": 1, "flagged": 0}}
    assert len(flattened(payload)) == 1
    assert "skipped_lines" not in payload


def test_scout_metr_null_flag_measurements_keep_scores_and_maximum(ctx: QaContext) -> None:
    ledger(
        ctx,
        result(tool="scout_metr", detector="scalar", value=3.5, flag=None),
        result(tool="scout_metr", detector="mapping", value={"score": 2, "max": 5}, flag=None),
        result(tool="scout_metr", detector="top-level-max", value=4, max=6, flag=None),
    )
    payload = findings(ctx, run_id="dev-task/one")
    records = {record["detector"]: record for record in flattened(payload)}
    assert records["scalar"]["summary"] == {"score": 3.5}
    assert records["mapping"]["summary"] == {"score": 2, "max": 5}
    assert records["top-level-max"]["summary"] == {"score": 4, "max": 6}
    assert all(record["flag"] is None for record in records.values())
    assert payload["counts"] == {"scout_metr": {"total": 3, "flagged": 0}}


def test_scout_metr_top_level_maximum_is_part_of_the_complete_record_fingerprint(ctx: QaContext) -> None:
    smaller = result(tool="scout_metr", detector="maximum", value=4, max=6, flag=None)
    larger = {**smaller, "max": 10}
    ledger(ctx, smaller, larger)
    ledger(ctx, larger, name="laminar-own.jsonl")
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"scout_metr": {"total": 2, "flagged": 0}}
    assert {
        (record["summary"]["score"], record["summary"]["max"]) for record in flattened(payload)
    } == {(4, 6), (4, 10)}


def test_fingerprint_also_covers_indirectly_projected_public_fields(ctx: QaContext) -> None:
    ordinary = result(tool="docent", value={"mechanism": "source-marker"})
    screened = {**ordinary, "source": "source-marker"}
    ledger(ctx, ordinary, screened)
    ledger(ctx, screened, name="laminar-own.jsonl")
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"docent": {"total": 2, "flagged": 0}}
    assert {record["summary"]["mechanism"] for record in flattened(payload)} == {
        "source-marker", "[withheld]",
    }
    assert payload["withheld_strings"] == 1


def test_oversized_jsonl_records_are_silently_discarded_then_reading_resumes(ctx: QaContext) -> None:
    body = "x" * (2 * 1024 * 1024)
    ledger(
        ctx, result(value={"mechanism": body}), result("test-task/one", value={"mechanism": body}),
        {"payload": body, **result(detector="wanted-id-after-the-cap")}, result(detector="after"),
    )
    payload = findings(ctx, run_id="dev-task/one")
    assert "oversized_records" not in payload
    assert "skipped_lines" not in payload
    assert [record["detector"] for record in flattened(payload)] == ["after"]


def test_matching_2mib_record_keeps_memory_bounded_under_budget_one(ctx: QaContext) -> None:
    ledger(ctx, result(value={"mechanism": "x" * (2 * 1024 * 1024)}))
    tracemalloc.start()
    try:
        payload = findings(replace(ctx, budget=Budget(1)), run_id="dev-task/one")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert payload["findings"] == {}
    assert payload["truncated"] is True
    assert peak < 1024 * 1024


def test_oversized_canonical_objects_resume_at_the_next_object(ctx: QaContext) -> None:
    path = ctx.state_dir / "trace-tools/tt7/docent/arms/oversized-canonical.json"
    path.parent.mkdir(parents=True)
    body = 'nested {"quoted": [1]} \\\\ ' * 16_000
    path.write_text(json.dumps([
        result(tool="docent", value={"mechanism": body}),
        result("test-task/one", tool="docent", value={"mechanism": body}),
        result(tool="docent", detector="after", value={"confidence": "high"}),
    ], indent=2), encoding="utf-8")
    payload = findings(ctx, run_id="dev-task/one")
    assert "oversized_records" not in payload
    assert [record["detector"] for record in flattened(payload)] == ["after"]
    assert "skipped_lines" not in payload


@pytest.mark.parametrize("limit", [60, 120, 200, 400, 1000])
def test_every_returned_key_fits_the_data_budget(ctx: QaContext, limit: int) -> None:
    ledger(ctx, *(result(detector=f"detector-{index}") for index in range(12)))
    bounded = replace(ctx, budget=Budget(limit))
    payload = findings(bounded, task_id="dev-task")
    assert estimate_tokens(payload) <= limit
    assert bounded.budget.used <= limit
    assert bounded.budget.used >= estimate_tokens(payload)
    if payload["findings"]:
        assert "withheld_strings" in payload
        assert "dedup_capped" in payload


def test_task_join_rechecks_admission_in_its_snapshot(
    ctx: QaContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ctx.require_task
    registry = ctx.retriever.registry
    assert isinstance(registry, SyntheticRegistry)

    def task_then_lock(identity: str) -> dict[str, Any]:
        task = original(identity)
        registry._assigned_test_names.add(identity)
        return task

    monkeypatch.setattr(ctx, "require_task", task_then_lock)
    with pytest.raises(QaDataError) as error:
        findings(ctx, task_id="dev-task")
    assert str(error.value) == UNAVAILABLE


@pytest.mark.parametrize("detector", [
    "Evaluator verdict: the answer was copied, and this is a long explanation",
    "a" * 65,
    "forbidden/detector",
])
def test_structured_detector_tokens_cannot_carry_evaluator_prose(
    ctx: QaContext, detector: str,
) -> None:
    prose = "Legitimate free-form evaluator hypothesis for this admitted run."
    ledger(ctx, result(
        tool="docent", detector=detector, detector_version=prose, scan=prose,
        value={"mechanism": prose},
    ))
    payload = findings(ctx, run_id="dev-task/one")
    record = flattened(payload)[0]
    assert record["detector"] == "[withheld]"
    assert record["summary"] == {"mechanism": prose}
    assert record["tool"] == "docent"
    assert "detector_version" not in record
    assert "scan" not in record
    assert payload["withheld_strings"] == 1


@pytest.mark.parametrize("field", ["confidence", "tests", "eval_awareness"])
def test_enumerated_summary_fields_must_be_short_structured_tokens(
    ctx: QaContext, field: str,
) -> None:
    ledger(ctx, result(tool="docent", value={field: "evaluator explanation, not an enum"}))
    payload = findings(ctx, run_id="dev-task/one")
    assert flattened(payload)[0]["summary"] == {field: "[withheld]"}
    assert payload["withheld_strings"] == 1


def test_newer_and_conflicting_laminar_observations_survive_mirror_dedup(ctx: QaContext) -> None:
    old = result(tool="laminar", flag=None, at="2026-10-28T01:00:00Z", value={
        "observations": [{"flag": None, "status": "PENDING"}],
    })
    new = result(tool="laminar", flag=True, at="2026-10-28T02:00:00Z", value={
        "observations": [{"flag": True, "status": "COMPLETED"}], "conflicting": False,
    })
    newest = {**new, "at": "2026-10-28T03:00:00Z"}
    mirror = {**newest, "value": {
        "conflicting": False, "observations": [{"status": "COMPLETED", "flag": True}],
    }}
    ledger(ctx, old)
    ledger(ctx, old, new, newest, mirror, name="laminar-own.jsonl")
    payload = findings(ctx, run_id="dev-task/one")
    records = flattened(payload)
    assert payload["counts"] == {"laminar": {"total": 3, "flagged": 2}}
    assert [record["at"] for record in records] == [
        "2026-10-28T03:00:00Z", "2026-10-28T02:00:00Z", "2026-10-28T01:00:00Z",
    ]
    assert [record["flag"] for record in records] == [True, True, None]


def test_prefilter_accepts_unicode_and_slash_escaped_run_id_encodings(ctx: QaContext) -> None:
    identity = "dev-task/café/one"
    ctx.database.upsert_trace({
        "trace_id": identity, "run_id": "batch", "task_id": "dev-task", "model_id": "synthetic",
    }, steps=[])
    raw = []
    for ascii_only in (True, False):
        for slash_escaped in (True, False):
            row = result(identity, detector=f"encoding-{int(ascii_only)}-{int(slash_escaped)}")
            text = json.dumps(row, ensure_ascii=ascii_only)
            raw.append(text.replace("/", "\\/") if slash_escaped else text)
    raw.append(json.dumps(result("test-task/one", value={"mentioned_run": identity})))
    ledger(ctx, *raw)
    payload = findings(ctx, run_id=identity)
    assert payload["counts"] == {"scout": {"total": 4, "flagged": 0}}
    assert {record["run_id"] for record in flattened(payload)} == {identity}


def test_withheld_runs_have_distinct_aliases_and_canonical_coverage(ctx: QaContext) -> None:
    registry = ctx.retriever.registry
    assert isinstance(registry, SyntheticRegistry)
    registry._sealed_names.update({"one", "two"})
    ledger(ctx, result("dev-task/two"), result("dev-task/one"), result("dev-task/one", detector="second"))
    payload = findings(ctx, task_id="dev-task")
    assert set(payload["findings"]) == {"run#1", "run#2"}
    assert sorted(map(len, payload["findings"].values())) == [1, 2]
    assert payload["coverage"]["runs_with_findings"] == 2
    assert payload["counts"] == {"scout": {"total": 3, "flagged": 0}}
    assert "dev-task/one" not in json.dumps(payload)
    assert "dev-task/two" not in json.dumps(payload)
    assert all(
        record["run_id"] == alias
        for alias, records in payload["findings"].items() for record in records
    )


def test_non_admitted_oversized_prefix_mentions_cannot_change_any_output(ctx: QaContext) -> None:
    path = ledger(ctx, result())
    baseline = findings(ctx, run_id="dev-task/one")
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"mentioned_run":"dev-task/one","run_id":"test-task/one","payload":"')
        stream.write("x" * (2 * 1024 * 1024))
        stream.write('"}\n')
        stream.write('{"mentioned_run":"dev-task/one","run_id":"test-task/one","bad":}\n')
    payload = findings(replace(ctx, budget=Budget(10_000)), run_id="dev-task/one")
    assert payload == baseline
    assert "skipped_lines" not in payload
    assert "oversized_records" not in payload


def test_candidate_heaps_cap_both_flag_categories_but_counts_cover_all_rows(ctx: QaContext) -> None:
    path = ledger(ctx)
    with path.open("a", encoding="utf-8") as stream:
        for index in range(10_000):
            stream.write(json.dumps(result(value=index, flag=index % 2 == 0)) + "\n")
    bounded = replace(ctx, budget=Budget(1_000_000))
    payload = findings(bounded, run_id="dev-task/one", limit=10_000)
    records = flattened(payload)
    flagged = [record for record in records if record["flag"]]
    unflagged = [record for record in records if not record["flag"]]
    assert len(flagged) == len(unflagged) == 2000
    assert min(record["summary"]["score"] for record in flagged) == 6000
    assert min(record["summary"]["score"] for record in unflagged) == 6001
    assert flagged[0]["summary"]["score"] == 9998
    assert unflagged[0]["summary"]["score"] == 9999
    assert payload["counts"] == {"scout": {"total": 10_000, "flagged": 5000}}
    assert payload["omitted"] == 6000
    assert payload["dedup_capped"] is False


@pytest.mark.parametrize("many_tools", [False, True])
def test_many_matching_records_keep_tiny_budget_peak_below_two_mib(
    ctx: QaContext, many_tools: bool,
) -> None:
    path = ledger(ctx)
    with path.open("a", encoding="utf-8") as stream:
        for index in range(12_000):
            tool = f"tool-{index}" if many_tools else "scout"
            stream.write(json.dumps(result(tool=tool, value=index, flag=index % 2 == 0)) + "\n")
    tracemalloc.start()
    try:
        payload = findings(replace(ctx, budget=Budget(100)), run_id="dev-task/one")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert payload["coverage"]["runs_with_findings"] == 1
    assert payload["dedup_capped"] is True
    assert estimate_tokens(payload) <= 100
    if not many_tools:
        assert payload["counts"] == {"scout": {"total": 12_000, "flagged": 6000}}


@pytest.mark.parametrize("canonical", [False, True])
def test_ten_thousand_level_array_record_is_discarded_without_failing_the_call(
    ctx: QaContext, canonical: bool,
) -> None:
    deeply_nested = json.dumps(result(value="__DEEP_ARRAY__")).replace(
        '"__DEEP_ARRAY__"', "[" * 10_000 + "0" + "]" * 10_000,
    )
    after = result(detector="after-deep")
    if canonical:
        path = ctx.state_dir / "trace-tools/tt7/docent/arms/deep-canonical.json"
        path.parent.mkdir(parents=True)
        path.write_text("[" + deeply_nested + "," + json.dumps(after) + "]", encoding="utf-8")
    else:
        ledger(ctx, deeply_nested, after)
    payload = findings(ctx, run_id="dev-task/one")
    # Python 3.12 raises RecursionError on the deep record (it is discarded); 3.14 decodes it. Either
    # way the call succeeds, later records survive, and the deep value never reaches the output.
    detectors = [record["detector"] for record in flattened(payload)]
    assert "after-deep" in detectors and set(detectors) <= {"after-deep", "repeat"}
    assert payload["counts"]["scout"]["total"] == len(detectors)
    assert "[[[[" not in json.dumps(payload)


def test_normalized_evidence_steps_participate_in_exact_mirror_dedup(ctx: QaContext) -> None:
    ledger(ctx, result(evidence_steps=[3, 1, 3]), result(evidence_steps=[1, 4]))
    ledger(ctx, result(evidence_steps=[1, 3]), name="laminar-own.jsonl")
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"scout": {"total": 2, "flagged": 0}}
    assert {tuple(record["evidence_steps"]) for record in flattened(payload)} == {(1, 3), (1, 4)}


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("slash_escaped", [False, True])
def test_ascii_unicode_escapes_in_run_ids_are_decoded_before_ownership_checks(
    ctx: QaContext, canonical: bool, slash_escaped: bool,
) -> None:
    spelling = "dev-\\u0074ask" + ("\\/" if slash_escaped else "/") + "one"
    raw = json.dumps(result()).replace("dev-task/one", spelling)
    other = json.dumps(result("test-task/one", value={"mentioned_run": "dev-task/one"})).replace(
        "dev-task/one", spelling,
    )
    if canonical:
        path = ctx.state_dir / "trace-tools/tt7/docent/arms/escaped-canonical.json"
        path.parent.mkdir(parents=True)
        path.write_text("[" + other + "," + raw + "]", encoding="utf-8")
    else:
        ledger(ctx, other, raw)
    payload = findings(ctx, run_id="dev-task/one")
    assert [record["run_id"] for record in flattened(payload)] == ["dev-task/one"]
    assert payload["counts"] == {"scout": {"total": 1, "flagged": 0}}


def test_escape_free_irrelevant_lines_never_reach_json_decode(
    ctx: QaContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger(ctx, result("other-dev-task/one"), result())
    original = findings_module.json.loads

    def refuse_irrelevant_decode(value: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(value, bytes | bytearray) and b"other-dev-task/one" in value:
            pytest.fail("an escape-free irrelevant ledger line reached JSON decoding")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(findings_module.json, "loads", refuse_irrelevant_decode)
    payload = findings(ctx, run_id="dev-task/one")
    assert payload["counts"] == {"scout": {"total": 1, "flagged": 0}}


@pytest.mark.parametrize("failure", [RecursionError, MemoryError])
@pytest.mark.parametrize("phase", ["decode", "fingerprint", "projection"])
def test_resource_errors_discard_one_record_without_losing_later_findings(
    ctx: QaContext, monkeypatch: pytest.MonkeyPatch, failure: type[Exception], phase: str,
) -> None:
    registry = ctx.retriever.registry
    assert isinstance(registry, SyntheticRegistry)
    registry._sealed_names.add("two")
    ledger(
        ctx,
        result("dev-task/two", tool="docent", detector="bad", value={"mechanism": "sealed-task"}),
        result(tool="docent", detector="good", value={"confidence": "high"}),
        result("dev-task/two", tool="docent", detector="after", value={"confidence": "high"}),
    )
    if phase == "decode":
        original_loads = findings_module.json.loads

        def fail_decode(value: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(value, bytes | bytearray) and b'"detector": "bad"' in value:
                raise failure("synthetic decode resource limit")
            return original_loads(value, *args, **kwargs)

        monkeypatch.setattr(findings_module.json, "loads", fail_decode)
    elif phase == "fingerprint":
        original_dumps = findings_module.json.dumps

        def fail_fingerprint(value: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(value, tuple) and len(value) > 2 and value[2] == "bad":
                raise failure("synthetic fingerprint resource limit")
            return original_dumps(value, *args, **kwargs)

        monkeypatch.setattr(findings_module.json, "dumps", fail_fingerprint)
    else:
        original_project = findings_module._project

        def fail_projection(row: Any, wanted: set[str], screen: Any) -> Any:
            projected = original_project(row, wanted, screen)
            if row.get("detector") == "bad":
                raise failure("synthetic projection resource limit")
            return projected

        monkeypatch.setattr(findings_module, "_project", fail_projection)
    payload = findings(ctx, task_id="dev-task")
    assert {record["detector"] for record in flattened(payload)} == {"good", "after"}
    assert payload["counts"] == {"docent": {"total": 2, "flagged": 0}}
    assert payload["coverage"]["runs_with_findings"] == 2
    assert payload["withheld_strings"] == 1
    assert set(payload["findings"]) == {"dev-task/one", "run#1"}
