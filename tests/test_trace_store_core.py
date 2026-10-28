"""Synthetic trace-store boundaries; all DB, split, evidence and CAS state is injected."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from evallab.readers.clouddb_blind import TraceRef
from evallab.trace_store.core import (
    ALLOWED_DATASETS,
    METADATA_FIELDS,
    HydrationError,
    TracePayload,
    load_trace,
    select_traces,
    trace_uuid,
)
from evallab.trace_store.secrets import SECRET_RE, secret_hits

GOODFIRE = "goodfire-reward-hack-data"
MIMO = "mimo-defect-map-v1"
TB2 = "tb2-to-tb21-v1"
TB4 = "tb4-audit-v1"
SOURCE_RUNS = ("mimo30", "muse-r1", "muse-r2", "muse-w5", "tb2-to-tb21", "kimi-tb4")
ANNOTATION = "synthetic-evaluator-annotation-not-actor-content"


@dataclass
class Corpus:
    root: Path
    db_path: Path
    evidence_path: Path
    cas_root: Path
    con: sqlite3.Connection

    def add_trace(
        self,
        trace_id: str,
        dataset: str,
        run_id: str,
        *,
        task_id: str | None = None,
        split: str = "DEV",
        assignment: str | None = "dev",
    ) -> TraceRef:
        task_id = task_id or "terminal-bench/" + trace_id
        self.con.execute("INSERT INTO corpus_tasks VALUES (?, ?, ?, NULL, NULL, ?)", (
            task_id, dataset, split, json.dumps({
                "item_id": task_id, "source_label": ANNOTATION, "reward": 1,
            }),
        ))
        self.con.execute("INSERT INTO agent_traces VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            trace_id, task_id, run_id, "synthetic-model", "completed",
            json.dumps({"source_label": ANNOTATION}), 1.0, "synthetic-trust-tier",
        ))
        self.step(trace_id, 0, "user", content="Synthetic actor request.")
        if assignment is not None:
            with (self.root / "research/answer-keys/splits/assignments.csv").open(
                "a", encoding="utf-8",
            ) as stream:
                stream.write(f"{dataset},{task_id},{task_id},{assignment}\n")
        return TraceRef(trace_id, task_id, dataset)

    def step(
        self, trace_id: str, index: int, role: str, *, content: str | None = "",
        preview: str | None = None, uri: str | None = None,
        command: str | None = None, flags: dict[str, Any] | None = None,
    ) -> None:
        self.con.execute("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            trace_id, index, role, "bash", command, content, preview, uri,
            json.dumps({
                "source_label": ANNOTATION, "source_passages": [ANNOTATION],
                "is_exploit": True, "trust_tier": ANNOTATION,
                "flagged_passage_steps": [index], **(flags or {}),
            }),
        ))

    def put_bytes(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        path = self.cas_root / "sha256" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return "cas://sha256/" + digest

    def put_bundle(self, bundle: Any) -> str:
        # Same deterministic UTF-8 JSON representation as S3CompatibleCAS.put_bundle.
        return self.put_bytes(json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))

    def select(self, dataset: str | None = None) -> list[TraceRef]:
        return select_traces(
            self.con, repo_root=self.root, dataset=dataset, evidence_path=self.evidence_path,
        )

    def ref(self, trace_id: str = "z-own-mimo") -> TraceRef:
        return next(ref for ref in self.select() if ref.trace_id == trace_id)

    def load(self, trace_id: str = "z-own-mimo") -> TracePayload:
        return load_trace(self.con, self.ref(trace_id), cas_root=self.cas_root)


@pytest.fixture
def corpus(tmp_path: Path) -> Iterator[Corpus]:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in (TB4, "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    (keys / TB4 / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": MIMO, "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": ["inventory-held"],
    }), encoding="utf-8")
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir()
    assignments.write_text("key_id,item_id,task,split\n", encoding="utf-8")
    evidence = tmp_path / "evidence/ingestion-sources.json"
    evidence.parent.mkdir()
    evidence.write_text(json.dumps({
        "artifact_sources": {run: {"traces": 1} for run in SOURCE_RUNS},
    }), encoding="utf-8")
    con = sqlite3.connect(tmp_path / "cloud.sqlite")
    con.executescript("""
        CREATE TABLE corpus_tasks (
            task_id TEXT PRIMARY KEY, dataset_id TEXT, split TEXT,
            original_grader_digest TEXT, repaired_grader_digest TEXT, metadata_json TEXT
        );
        CREATE TABLE agent_traces (
            trace_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, model_id TEXT,
            outcome TEXT, verified_receipt_json TEXT, reward REAL, trust_tier TEXT
        );
        CREATE TABLE trace_steps (
            trace_id TEXT, step_index INTEGER, role TEXT, tool_name TEXT,
            command_or_action TEXT, content_text TEXT, observation_preview TEXT,
            observation_cas_uri TEXT, flags_json TEXT,
            PRIMARY KEY (trace_id, step_index)
        );
    """)
    fixture = Corpus(root, tmp_path / "cloud.sqlite", evidence, tmp_path / "cas", con)
    fixture.add_trace("a-goodfire", GOODFIRE, "source-card:goodfire")
    fixture.add_trace("z-own-mimo", MIMO, "mimo30", task_id="mimo-v2.6-rl-code/mimo-local")
    fixture.add_trace("b-own-tb2", TB2, "tb2-to-tb21")
    fixture.add_trace("c-own-tb4", TB4, "kimi-tb4")
    fixture.add_trace("excluded-tb4-hub", TB4, "qa-trace-sft-v0:tb4-audit-v1")
    fixture.add_trace("excluded-qa", "qa-trace-sft-v0", "muse-r1")
    fixture.add_trace("excluded-hackbench", "hacktrace-act-v1", "hackbench-source")
    fixture.add_trace("excluded-cheatbench", "cheatbench-actions-v1", "cheatbench-source")
    fixture.add_trace("excluded-hackbench-alias", "HackBench", "mimo30")
    fixture.add_trace("excluded-cheatbench-alias", "CheatBench", "mimo30")
    fixture.add_trace("excluded-lower-dev", GOODFIRE, "source", split="dev")
    fixture.add_trace("excluded-held", GOODFIRE, "source", split="held")
    fixture.add_trace("excluded-test", GOODFIRE, "source", split="TEST")
    fixture.add_trace("excluded-unadmitted", TB4, "kimi-tb4", assignment=None)
    fixture.add_trace("excluded-inventory-held", GOODFIRE, "source",
                      task_id="external/inventory-held")
    fixture.add_trace("excluded-cross-key", GOODFIRE, "source")
    with assignments.open("a", encoding="utf-8") as stream:
        stream.write("secondary-key,held-item,terminal-bench/excluded-cross-key,test\n")
    con.commit()
    try:
        yield fixture
    finally:
        con.close()


def test_exact_dev_cross_key_and_source_gate_prioritize_own(corpus: Corpus) -> None:
    assert frozenset({GOODFIRE, MIMO, TB2, TB4}) == ALLOWED_DATASETS
    assert [ref.trace_id for ref in corpus.select()] == [
        "b-own-tb2", "c-own-tb4", "z-own-mimo", "a-goodfire",
    ]
    assert [ref.trace_id for ref in corpus.select(GOODFIRE)] == ["a-goodfire"]
    assert [ref.trace_id for ref in corpus.select(TB4)] == ["c-own-tb4"]
    corpus.con.row_factory = sqlite3.Row
    assert [ref.trace_id for ref in corpus.select(MIMO)] == ["z-own-mimo"]


@pytest.mark.parametrize("dataset", ["qa-trace-sft-v0", "HackBench", "CheatBench", "unknown"])
def test_disallowed_dataset_filter_fails(corpus: Corpus, dataset: str) -> None:
    with pytest.raises(ValueError, match="Dataset is not allowed"):
        corpus.select(dataset)


@pytest.mark.parametrize(("dataset", "run"), [
    (MIMO, "mimo30"), (MIMO, "muse-r1"), (MIMO, "muse-r2"), (MIMO, "muse-w5"),
    (TB2, "tb2-to-tb21"), (TB4, "kimi-tb4"), (TB4, "muse-r1"),
])
def test_every_exact_own_run_is_allowed(corpus: Corpus, dataset: str, run: str) -> None:
    ref = corpus.add_trace("allowed-extra", dataset, run)
    assert ref in corpus.select(dataset)


@pytest.mark.parametrize(("dataset", "run"), [
    (MIMO, "kimi-tb4"), (MIMO, "mimo30-suffix"), (MIMO, "MIMO30"),
    (TB2, "muse-r1"), (TB4, "muse-r2"), (TB4, "tb4-hub"),
])
def test_evidence_cannot_expand_dataset_run_allowlists(
    corpus: Corpus, dataset: str, run: str,
) -> None:
    evidence = json.loads(corpus.evidence_path.read_text(encoding="utf-8"))
    evidence["artifact_sources"][run] = {"traces": 1}
    corpus.evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    ref = corpus.add_trace("disallowed-extra", dataset, run)
    assert ref not in corpus.select(dataset)


def test_assignment_authority_is_required(corpus: Corpus) -> None:
    (corpus.root / "research/answer-keys/splits/assignments.csv").unlink()
    assert corpus.select() == []


def test_own_selection_requires_retained_source_attestation(corpus: Corpus) -> None:
    corpus.evidence_path.write_text(json.dumps({
        "artifact_sources": {"mimo30": {}},
    }), encoding="utf-8")
    assert [ref.trace_id for ref in corpus.select(MIMO)] == ["z-own-mimo"]
    with pytest.raises(ValueError, match="artifact_sources: kimi-tb4, tb2-to-tb21"):
        corpus.select()
    # Goodfire does not depend on an unrelated own-run evidence file.
    corpus.evidence_path.unlink()
    assert len(corpus.select(GOODFIRE)) == 1
    with pytest.raises(FileNotFoundError):
        corpus.select(MIMO)


@pytest.mark.parametrize("evidence", ["{invalid", "[]", '{"artifact_sources": []}',
                                      '{"artifact_sources": {"mimo30": null}}'])
def test_malformed_source_evidence_fails(corpus: Corpus, evidence: str) -> None:
    corpus.evidence_path.write_text(evidence, encoding="utf-8")
    with pytest.raises(ValueError):
        corpus.select(MIMO)


def test_default_evidence_path_is_injectable(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    evidence = home / ".local/state/evallab-prod-live/cloud-trace-db-evidence/ingestion-sources.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(corpus.evidence_path.read_bytes())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    refs = select_traces(corpus.con, repo_root=corpus.root, dataset=MIMO)
    assert [ref.trace_id for ref in refs] == ["z-own-mimo"]


def test_selection_and_loading_work_with_read_only_database(corpus: Corpus) -> None:
    with closing(sqlite3.connect(corpus.db_path.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        refs = select_traces(con, repo_root=corpus.root, evidence_path=corpus.evidence_path)
        payload = load_trace(con, refs[0], cas_root=corpus.cas_root)
        assert payload.trace_id == "b-own-tb2"
        assert not con.in_transaction


def test_exact_metadata_has_no_evaluator_columns(corpus: Corpus) -> None:
    payload = corpus.load()
    assert payload.metadata == {
        "run_id": "z-own-mimo", "source": MIMO, "benchmark": "mimo-v2.6-rl-code",
        "model": "synthetic-model", "task_id": "mimo-v2.6-rl-code/mimo-local",
        "outcome": "completed", "split": "DEV",
    }
    assert set(payload.metadata) == METADATA_FIELDS
    assert payload.trace_id == payload.metadata["run_id"]
    assert payload.dataset_id == payload.metadata["source"]
    assert payload.hydrated_bytes == 0
    assert ANNOTATION not in json.dumps(payload.metadata)
    assert ANNOTATION not in json.dumps(payload.messages)
    with pytest.raises(FrozenInstanceError):
        payload.trace_id = "changed"


def test_uuid_contract_is_shared_stable_and_namespaced() -> None:
    expected = str(uuid5(NAMESPACE_URL, "evallab-trace-store/v1:synthetic-trace"))
    assert trace_uuid("synthetic-trace") == expected
    assert UUID(expected).version == 5
    assert trace_uuid("other-trace") != expected


@pytest.mark.parametrize("trace_id", ["excluded-lower-dev", "excluded-held", "excluded-tb4-hub",
                                     "excluded-cheatbench"])
def test_loading_rejects_unallowed_ref_sources(corpus: Corpus, trace_id: str) -> None:
    task_id, dataset_id = corpus.con.execute(
        "SELECT a.task_id, t.dataset_id FROM agent_traces a JOIN corpus_tasks t USING(task_id) "
        "WHERE a.trace_id = ?", (trace_id,),
    ).fetchone()
    with pytest.raises(ValueError, match="source is not allowed"):
        load_trace(corpus.con, TraceRef(trace_id, task_id, dataset_id), cas_root=corpus.cas_root)


def test_loading_rejects_missing_and_mismatched_identity(corpus: Corpus) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_trace(corpus.con, TraceRef("absent", "absent", MIMO), cas_root=corpus.cas_root)
    with pytest.raises(ValueError, match="does not match"):
        load_trace(corpus.con, TraceRef("z-own-mimo", "different-task", MIMO),
                   cas_root=corpus.cas_root)


def test_raw_observation_hydrates_all_utf8_bytes_and_preserves_pairing(corpus: Corpus) -> None:
    full = "café 完整 observation\n" * 20_000
    preview = full[:131]
    reasoning = "完整 reasoning\n" * 20_000
    corpus.step("z-own-mimo", 1, "assistant", content="Inspecting.", command=json.dumps([
        {"id": "native-call", "name": "bash", "arguments": {"command": "cat output"}},
    ]), flags={"reasoning_content": reasoning})
    corpus.step("z-own-mimo", 2, "tool", content=preview,
                uri=corpus.put_bytes(full.encode("utf-8")), flags={"tool_call_id": "native-call"})
    payload = corpus.load()
    assert payload.messages[1]["reasoning"] == reasoning
    assert payload.messages[2]["content"] == full
    assert payload.messages[2]["tool_call_id"] == "native-call"
    assert payload.hydrated_bytes == len(full.encode("utf-8")) - len(preview.encode("utf-8"))
    assert ANNOTATION not in json.dumps(payload.messages, ensure_ascii=False)


def test_atif_cas_projects_all_results_but_never_bundle_annotations(corpus: Corpus) -> None:
    first, second = "first 完整\n" * 10_000, "second café\n" * 10_000
    uri = corpus.put_bundle({
        "observation": {"results": [
            {"source_call_id": "call-one", "content": first, "source_label": ANNOTATION},
            {"source_call_id": "call-two", "content": second, "receipt": ANNOTATION},
        ], "source_label": ANNOTATION},
        "is_exploit": True, "trust_tier": ANNOTATION, "flagged_passage_steps": [7],
        "verified_receipt_json": {"reward": 1}, "answer_key": ANNOTATION,
    })
    corpus.step("c-own-tb4", 7, "agent", content="Inspecting both files.",
                preview="stored preview", uri=uri, command=json.dumps([
                    {"id": "call-one", "name": "bash", "arguments": {"command": "cat one"}},
                    {"id": "call-two", "name": "bash", "arguments": {"command": "cat two"}},
                ]), flags={"reasoning_content": "Keep this reasoning."})
    payload = corpus.load("c-own-tb4")
    assistant, one, two = payload.messages[1:]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning"] == "Keep this reasoning."
    assert one["content"] == first and one["tool_call_id"] == "call-one"
    assert two["content"] == second and two["tool_call_id"] == "call-two"
    assert one["step_index"] == two["step_index"] == assistant["step_index"] == 7
    assert payload.hydrated_bytes == (
        len(first.encode("utf-8")) + len(second.encode("utf-8")) - len(b"stored preview")
    )
    serialized = json.dumps(payload.messages)
    for forbidden in (ANNOTATION, "is_exploit", "trust_tier", "verified_receipt_json", "answer_key"):
        assert forbidden not in serialized


@pytest.mark.parametrize("bundle", [
    {"results": [{"content": "full result", "source_call_id": "paired"}], "labels": ANNOTATION},
    {"messages": [{"role": "assistant", "content": ANNOTATION},
                  {"role": "tool", "content": "full result", "tool_call_id": "paired"}],
     "source_label": ANNOTATION},
    {"content": "full result", "source_call_id": "paired", "source_label": ANNOTATION},
    {"message": {"role": "tool", "content": "full result", "tool_call_id": "paired"},
     "source_label": ANNOTATION},
    {"message": "full result", "source_label": ANNOTATION},
    {"observation": "full result", "source_label": ANNOTATION},
])
def test_known_actor_envelope_schemas_are_whitelisted(corpus: Corpus, bundle: dict) -> None:
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri=corpus.put_bundle(bundle),
                flags={"tool_call_id": "paired"})
    payload = corpus.load()
    assert payload.messages[-1]["content"] == "full result"
    assert payload.messages[-1]["tool_call_id"] == "paired"
    assert ANNOTATION not in json.dumps(payload.messages)


def test_observation_without_inline_preview_is_still_hydrated(corpus: Corpus) -> None:
    uri = corpus.put_bundle({"results": [{"content": "full result", "source_call_id": "call-5"}]})
    corpus.step("c-own-tb4", 5, "agent", content="Reading.", command="cat file", uri=uri)
    payload = corpus.load("c-own-tb4")
    assert [message["role"] for message in payload.messages] == ["user", "assistant", "tool"]
    assert payload.messages[-1]["content"] == "full result"
    assert payload.messages[-1]["tool_call_id"] == "call-5"
    assert payload.hydrated_bytes == len(b"full result")


def test_all_pointers_including_raw_system_artifacts_are_hydrated(corpus: Corpus) -> None:
    full = "Synthetic actor-visible system context.\n" * 500
    corpus.step("z-own-mimo", 4, "system", content=full[:30],
                uri=corpus.put_bytes(full.encode("utf-8")))
    payload = corpus.load()
    assert payload.messages[-1]["role"] == "system"
    assert payload.messages[-1]["content"] == full
    assert payload.hydrated_bytes == len(full.encode("utf-8")) - 30


def test_preview_bound_raw_json_artifacts_remain_exact(corpus: Corpus) -> None:
    raw = b'{\n  "tool_output": ["one", "two"],\n  "count": 2\n}\n'
    preview = raw[:18].decode()
    corpus.step("z-own-mimo", 1, "tool", content=preview, uri=corpus.put_bytes(raw))
    payload = corpus.load()
    assert payload.messages[-1]["content"] == raw.decode()
    assert payload.hydrated_bytes == len(raw) - len(preview.encode())


def test_raw_json_message_field_does_not_discard_other_actor_fields(corpus: Corpus) -> None:
    raw = b'{"message":"actor-visible error","details":["one","two"],"status":500}\n'
    corpus.step("z-own-mimo", 1, "tool", content=raw[:25].decode(), uri=corpus.put_bytes(raw))
    assert corpus.load().messages[-1]["content"] == raw.decode()


@pytest.mark.parametrize("field", [
    "source_label", "is_exploit", "trust_tier", "flagged_passage_steps", "receipt",
    "verified_receipt_json", "answer-key", "labels", "reward", "flags_json",
    "generation_receipt", "source_receipt",
])
def test_raw_evaluator_json_is_rejected_even_with_exact_preview_binding(
    corpus: Corpus, field: str,
) -> None:
    raw = json.dumps({"nested": {field: ANNOTATION}}).encode()
    corpus.step("z-own-mimo", 2, "tool", content=raw.decode(), uri=corpus.put_bytes(raw))
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "evaluator_fields"
    assert ANNOTATION not in str(caught.value)


@pytest.mark.parametrize(("role", "slot"), [
    ("system", "content"), ("user", "content"), ("assistant", "content"),
    ("tool", "content"), ("agent", "content"), ("agent", "preview"),
])
@pytest.mark.parametrize("field", [
    "source_label", "is_exploit", "trust_tier", "answer-key", "flags_json", "generation_receipt",
])
def test_inline_evaluator_envelopes_fail_without_cas(
    corpus: Corpus, role: str, slot: str, field: str,
) -> None:
    envelope = json.dumps({"nested": {field: ANNOTATION}})
    corpus.step(
        "z-own-mimo", 3, role,
        content=envelope if slot == "content" else "Inspecting.",
        preview=envelope if slot == "preview" else None,
    )
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "evaluator_fields"
    assert caught.value.trace_id == "z-own-mimo"
    assert caught.value.step_index == 3
    assert ANNOTATION not in str(caught.value)
    assert not corpus.cas_root.exists()


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_quoted_evaluator_json_is_rejected_recursively(corpus: Corpus, layers: int) -> None:
    content = json.dumps({"is_exploit": ANNOTATION})
    for _ in range(layers):
        content = json.dumps(content)
    corpus.step("z-own-mimo", 2, "tool", content=content)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "evaluator_fields"
    assert ANNOTATION not in str(caught.value)


def test_goodfire_inline_evaluator_envelope_is_also_rejected(corpus: Corpus) -> None:
    corpus.step("a-goodfire", 2, "tool", content='{"source_label":"not actor content"}')
    with pytest.raises(HydrationError) as caught:
        corpus.load("a-goodfire")
    assert caught.value.category == "evaluator_fields"


@pytest.mark.parametrize("content", [
    'An example JSON object is {"source_label":"a source-code field"}.',
    json.dumps('An example JSON object is {"source_label":"a source-code field"}.'),
    '"An example": {"source_label":"a source-code field"}',
    json.dumps({"stdout": '"An example": {"source_label":"a source-code field"}'}),
    '```json\n{"source_label":"a source-code field"}\n```',
    'payload = {"source_label":"a source-code field"}',
    "{'source_label': 'a Python dict expression, not evaluator JSON'}",
    '{"message":"ordinary actor tool output","count":2}',
])
def test_inline_actor_prose_and_code_are_preserved_verbatim(
    corpus: Corpus, content: str,
) -> None:
    corpus.step("z-own-mimo", 2, "tool", content=content)
    payload = corpus.load()
    assert payload.messages[-1]["content"] == content
    assert payload.hydrated_bytes == 0


@pytest.mark.parametrize("preview", [
    '{"source_label":"clipped',
    r'{"sour\u0063e_label":"clipped',
])
def test_clipped_evaluator_json_preview_cannot_be_silently_scrubbed(
    corpus: Corpus, preview: str,
) -> None:
    uri = corpus.put_bundle({"content": "otherwise safe actor output", "source_label": ANNOTATION})
    corpus.step("z-own-mimo", 2, "tool", content=preview, uri=uri)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "evaluator_fields"


@pytest.mark.parametrize("content", [
    {"source_label": ANNOTATION}, '{"source_label":"synthetic-annotation"}',
])
def test_annotations_nested_in_projected_content_fail(corpus: Corpus, content: Any) -> None:
    uri = corpus.put_bundle({"results": [{"content": content}]})
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri=uri)
    with pytest.raises(HydrationError):
        corpus.load()


@pytest.mark.parametrize(("raw", "category"), [
    (b'{"results": invalid}', "malformed_json"),
    (b'{"results":[{"not_content":"annotation"}]}', "malformed_observation"),
    (b'{"results":[]}', "malformed_observation"),
    (b'{"unknown_envelope":"not actor content"}', "unsupported_schema"),
    (b"unbound raw artifact", "unsupported_schema"),
    (b"\xff\x00", "invalid_utf8"),
])
def test_bad_cas_never_falls_back_to_preview(corpus: Corpus, raw: bytes, category: str) -> None:
    corpus.step("z-own-mimo", 2, "tool", content="stored preview", uri=corpus.put_bytes(raw))
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == category
    assert caught.value.trace_id == "z-own-mimo"
    assert caught.value.step_index == 2
    assert "stored preview" not in str(caught.value)


@pytest.mark.parametrize("uri", ["", "cas://private", "cas://sha256/../outside",
                                 "cas://sha256/" + "A" * 64])
def test_malformed_uri_is_explicit(corpus: Corpus, uri: str) -> None:
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri=uri)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "invalid_uri"


def test_missing_and_digest_mismatched_cas_fail(corpus: Corpus) -> None:
    uri = "cas://sha256/" + "0" * 64
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri=uri)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "missing_object"
    path = corpus.cas_root / "sha256" / ("0" * 64)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not the declared digest")
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "digest_mismatch"


def test_every_pointer_is_checked_even_after_successful_hydration(corpus: Corpus) -> None:
    corpus.step("z-own-mimo", 1, "tool", content="preview",
                uri=corpus.put_bytes(b"preview full actor output"))
    corpus.step("z-own-mimo", 5, "tool", content="second preview",
                uri="cas://sha256/" + "0" * 64)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "missing_object"
    assert caught.value.step_index == 5


def test_unreadable_cas_object_fails_explicitly(corpus: Corpus) -> None:
    digest = "0" * 64
    (corpus.cas_root / "sha256" / digest).mkdir(parents=True)
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri="cas://sha256/" + digest)
    with pytest.raises(HydrationError) as caught:
        corpus.load()
    assert caught.value.category == "unreadable_object"


def test_ambiguous_parallel_observation_pairing_fails(corpus: Corpus) -> None:
    uri = corpus.put_bundle({"results": [{"content": "unpaired actor output"}]})
    corpus.step("c-own-tb4", 8, "agent", content="Reading.", preview="preview", uri=uri,
                command=json.dumps([
                    {"id": "one", "name": "bash", "arguments": {}},
                    {"id": "two", "name": "bash", "arguments": {}},
                ]))
    with pytest.raises(HydrationError) as caught:
        corpus.load("c-own-tb4")
    assert caught.value.category == "ambiguous_observation"


def test_goodfire_inline_content_requires_no_cas(corpus: Corpus) -> None:
    full = "Goodfire inline actor observation.\n" * 20_000
    corpus.step("a-goodfire", 2, "tool", content=full)
    payload = corpus.load("a-goodfire")
    assert payload.messages[-1]["content"] == full
    assert payload.hydrated_bytes == 0
    assert not corpus.cas_root.exists()


def test_default_cas_path_is_injectable(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = home / ".local/state/evallab-prod-live/cas"
    full = b"preview plus full actor output"
    uri = corpus.put_bytes(full)
    digest = uri.rsplit("/", 1)[1]
    destination = root / "sha256" / digest
    destination.parent.mkdir(parents=True)
    destination.write_bytes(full)
    corpus.step("z-own-mimo", 2, "tool", content="preview", uri=uri)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    payload = load_trace(corpus.con, corpus.ref())
    assert payload.messages[-1]["content"] == full.decode()


def test_real_shaped_provider_key_is_detected_in_free_text() -> None:
    key = "sk-" + "proj-" + "A7b" * 16
    assert secret_hits("example credential: " + key + "\nend") == ["secret-pattern"]


def test_canonical_task_hash_is_not_a_provider_credential() -> None:
    task_id = "goodfire-reward-hack-data/swebench_verified/task-" + "a" * 30
    assert secret_hits({"metadata": {"task_id": task_id}}) == []
    key = "sk-" + "proj-" + "A7b" * 16
    assert secret_hits({"metadata": {"task_id": task_id}, "content": "token: " + key}) == [
        "secret-pattern"
    ]


@pytest.mark.parametrize("secret", [
    "sk-" + "a" * 16, "dk_" + "a" * 10, "hf_" + "a" * 20,
    "AKIA" + "A" * 16, "ghp_" + "a" * 20, "gho_" + "a" * 20,
    "ghu_" + "a" * 20, "ghs_" + "a" * 20, "xoxb-" + "a" * 10,
    "xoxp-" + "a" * 10, "-----BEGIN " + "RSA PRIVATE KEY-----",
    "EXAMPLE_API_KEY=abcdefgh", "EXAMPLE_SECRET=abcdefgh", "PASSWORD=abcdefgh",
    "TOKEN=abcdefgh", "EXAMPLE_TOKEN=abcdefgh", "EXAMPLE_TOKENS_BACKUP=abcdefgh",
])
def test_secret_scan_recurses_and_never_returns_secret_prefixes(secret: str) -> None:
    payload = {"metadata": {"nested": [None, {"content": secret + " " + secret}]}}
    before = json.dumps(payload)
    assert secret_hits(payload) == ["secret-pattern", "secret-pattern"]
    assert json.dumps(payload) == before
    assert secret[:12] not in json.dumps(secret_hits(payload))


def test_secret_traversal_matches_original_dict_values_and_lists_only() -> None:
    secret = "sk-" + "a" * 20
    assert secret_hits({secret: "safe", "number": 1, "tuple": (secret,)}) == []
    assert secret_hits([{"tool_calls": [{"arguments": {"command": secret}}]}]) == ["secret-pattern"]
    assert secret_hits("TOKENIZER_INFINITY=abcdefgh\nMAX_TOKENS_PER_PAGE=128") == []
    assert secret_hits("hf_short sk-short TOKEN=$VARIABLE PASSWORD='literal'") == []


@pytest.mark.parametrize("wrappers", [1, 2, 3])
def test_secret_scan_decodes_nested_json_content_without_losing_raw_hits(wrappers: int) -> None:
    key = "sk-" + "proj-" + "A7b" * 16
    content = json.dumps({"stdout": "heading\n" + key})
    for _ in range(wrappers - 1):
        content = json.dumps(content)
    # The encoded newline's `n` masks the token boundary in a raw-only scan.
    assert SECRET_RE.search(content) is None
    payload = {"transcripts": [{"messages": [{"content": content}]}]}
    original = json.dumps(payload)
    assert secret_hits(payload) == ["secret-pattern"]
    assert json.dumps(payload) == original
    assert key not in json.dumps(secret_hits(payload))


def test_json_wrapping_never_relaxes_raw_hits_or_matches_task_hashes() -> None:
    key = "sk-" + "a" * 20
    # Retain the original raw-string scan even when the key is a JSON property.
    assert secret_hits(json.dumps({key: "safe"})) == ["secret-pattern"]
    task_id = "task-" + "a" * 30
    assert secret_hits({"content": json.dumps({"stdout": "heading\n" + task_id})}) == []
