"""Offline cost-inventory boundaries: real DB rows, aggregate-only output.

The fixture database is synthetic but shaped exactly like the production store:
same schema subset, same split/assignment authority and the same admission
gate. No retained trace text is copied or asserted, and every expected count is
derived from the fixture's own construction.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from evallab.readers.clouddb_blind import opaque_id
from evallab.readers.cost_inventory import (
    ACTOR_HISTORY_UNAVAILABLE,
    HYDRATION_UNAVAILABLE,
    SAFE,
    SCENARIO_SCHEMA,
    SECRET_VETO,
    RateCard,
    Scenario,
    inventory,
    load_scenario,
    load_tokenizer,
    main,
    price_quote,
)

GOODFIRE = "goodfire-reward-hack-data"
MIMO = "mimo-defect-map-v1"
QA = "qa-trace-sft-v0"
SOURCE_RUNS = ("mimo30", "muse-r1", "muse-r2", "muse-w5", "tb2-to-tb21", "kimi-tb4")
HYDRATED_TEXT = "observed-output-" * 200
NO_ENCODER = importlib.util.find_spec("tiktoken") is None


class Corpus:
    def __init__(
        self, root: Path, db_path: Path, evidence: Path, cas_root: Path,
        con: sqlite3.Connection,
    ) -> None:
        self.root = root
        self.db_path = db_path
        self.evidence = evidence
        self.cas_root = cas_root
        self.con = con

    def add_trace(
        self,
        trace_id: str,
        dataset: str,
        run_id: str,
        *,
        task_id: str | None = None,
        split: str = "DEV",
        assignment: str | None = "dev",
        model_id: str = "synthetic-model",
    ) -> None:
        task_id = task_id or "terminal-bench/" + trace_id
        self.con.execute("INSERT INTO corpus_tasks VALUES (?, ?, ?, NULL, NULL, ?)", (
            task_id, dataset, split, json.dumps({"item_id": task_id}),
        ))
        self.con.execute("INSERT INTO agent_traces VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            trace_id, task_id, run_id, model_id, "completed", "{}", 1.0, "synthetic-tier",
        ))
        if assignment is not None:
            with (self.root / "research/answer-keys/splits/assignments.csv").open(
                "a", encoding="utf-8",
            ) as stream:
                stream.write(f"{dataset},{task_id},{task_id},{assignment}\n")

    def step(
        self, trace_id: str, index: int, role: str, *, content: str | None = "",
        preview: str | None = None, uri: str | None = None, command: str | None = None,
        flags: dict[str, Any] | None = None,
    ) -> None:
        self.con.execute("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            trace_id, index, role, "bash", command, content, preview, uri,
            json.dumps(flags or {}),
        ))

    def put_bytes(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        path = self.cas_root / "sha256" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return "cas://sha256/" + digest

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        return inventory(
            self.con, repo_root=self.root, evidence_path=self.evidence,
            cas_root=self.cas_root, **kwargs,
        )


@pytest.fixture
def corpus(tmp_path: Path) -> Iterator[Corpus]:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(json.dumps({
        "dataset_id": MIMO, "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
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
    fixture.step("a-goodfire", 0, "user", content="Synthetic actor request.")
    fixture.step("a-goodfire", 1, "assistant", content="Synthetic actor answer.")
    fixture.add_trace(
        "b-own-mimo", MIMO, "mimo30", task_id="mimo-v2.6-rl-code/mimo-local",
    )
    fixture.step("b-own-mimo", 0, "user", content="Own-run actor input.")
    fixture.add_trace("c-journal", GOODFIRE, "source", model_id="retained-event-journal/no-model")
    fixture.step("c-journal", 0, "agent", content="Event journal record")
    fixture.add_trace("d-secret", GOODFIRE, "source")
    fixture.step("d-secret", 0, "user", content="key sk-abcdefghij0123456789 in prompt")
    fixture.add_trace("e-qa", QA, "muse-r1")
    fixture.step("e-qa", 0, "user", content="Unrelated dataset input.")
    fixture.add_trace("f-test", GOODFIRE, "source", split="TEST")
    fixture.step("f-test", 0, "user", content="Test split input.")
    fixture.add_trace("g-cross", GOODFIRE, "source")
    fixture.step("g-cross", 0, "user", content="Cross-key input.")
    fixture.add_trace("h-hydration", GOODFIRE, "source")
    fixture.step("h-hydration", 0, "user", content="Hydration request.")
    fixture.step(
        "h-hydration", 1, "agent", preview="partial", command="run",
        uri="cas://sha256/" + "0" * 64,
    )
    fixture.add_trace("i-hydrated", GOODFIRE, "source")
    fixture.step("i-hydrated", 0, "user", content="Full observation request.")
    fixture.step(
        "i-hydrated", 1, "agent", preview=HYDRATED_TEXT[:20], command="run",
        uri=fixture.put_bytes(HYDRATED_TEXT.encode("utf-8")),
    )
    with assignments.open("a", encoding="utf-8") as stream:
        stream.write("secondary-key,terminal-bench/g-cross,terminal-bench/g-cross,test\n")
    con.commit()
    try:
        yield fixture
    finally:
        con.close()


def _rate_card(**overrides: Any) -> RateCard:
    values: dict[str, Any] = {
        "model": "synthetic/model",
        "tier": "standard",
        "input_usd_per_mtok": 0.15,
        "cached_input_usd_per_mtok": 0.03,
        "output_usd_per_mtok": 0.60,
        "source": "https://example.invalid/pricing",
        "retrieved_on": "2026-10-28",
    }
    values.update(overrides)
    return RateCard(**values)


def _scenario(**overrides: Any) -> Scenario:
    values: dict[str, Any] = {
        "version": "unit-1",
        "currency": "USD",
        "model": "synthetic/model",
        "tier": "standard",
        "rate": _rate_card(),
        "generations": 1,
        "retries_per_generation": 0,
        "calls_per_generation": 1,
        "cached_input_fraction": 0.0,
        "output_tokens_per_call": None,
        "storage_usd_per_gb_month": None,
        "owner_plan": None,
        "sha256": "0" * 64,
    }
    values.update(overrides)
    return Scenario(**values)


def _quote(
    scenario: Scenario,
    *,
    traces: int = 10,
    input_tokens: int | None = 1_000_000,
    rendered_bytes: int = 0,
    buckets: dict[str, int] | None = None,
    admitted_not_selected: int = 0,
    outside_scope: int = 0,
) -> dict[str, Any]:
    return price_quote(
        traces=traces,
        input_tokens=input_tokens,
        rendered_bytes=rendered_bytes,
        selected=traces,
        buckets=buckets or {SAFE: traces},
        scenario=scenario,
        admitted_not_selected=admitted_not_selected,
        outside_scope=outside_scope,
    )


def _scenario_document(**overrides: Any) -> dict[str, Any]:
    model = overrides.pop("model", "synthetic/model")
    tiers = overrides.pop("tiers", None) or {
        "standard": {
            "input_usd_per_mtok": 0.15,
            "cached_input_usd_per_mtok": 0.03,
            "output_usd_per_mtok": 0.60,
            "source": "https://example.invalid/pricing",
            "retrieved_on": "2026-10-28",
        },
    }
    assumptions: dict[str, Any] = {
        "generations": 1,
        "retries_per_generation": 0,
        "calls_per_generation": 1,
        "cached_input_fraction": 0.0,
        "output_tokens_per_call": None,
        "storage_usd_per_gb_month": None,
        "owner_plan": None,
    }
    assumptions.update(overrides.pop("assumptions", {}))
    document = {
        "schema": SCENARIO_SCHEMA,
        "version": "test-2026-10-28.1",
        "currency": "USD",
        "rates": {model: tiers},
        "assumptions": assumptions,
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: Any, name: str = "scenario.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_cohorts_reconcile_and_rows_are_classified(corpus: Corpus) -> None:
    receipt = corpus.scan(include_traces=True)
    cohorts = receipt["cohorts"]
    assert cohorts["db_total"] == 9
    assert cohorts["dev_admitted"] == 7
    assert cohorts["selected"] == 6
    assert cohorts["admitted_not_selected"] == 1
    assert cohorts["outside_dev_admission"] == 2
    assert cohorts["selected"] + cohorts["admitted_not_selected"] == cohorts["dev_admitted"]
    assert cohorts["db_total"] == cohorts["dev_admitted"] + cohorts["outside_dev_admission"]
    assert receipt["buckets"] == {
        SAFE: 3,
        ACTOR_HISTORY_UNAVAILABLE: 1,
        HYDRATION_UNAVAILABLE: 1,
        SECRET_VETO: 1,
    }
    assert sum(receipt["buckets"].values()) == cohorts["selected"]
    assert receipt["quarantine"]["actor_history"]["reasons"] == {
        "unbound_event_journal_record": 1,
    }
    assert receipt["quarantine"]["hydration"]["reasons"] == {"missing_object": 1}
    assert receipt["quarantine"]["secret_veto"] == {"traces": 1, "markers": 1}
    assert receipt["labels_read"] is False
    assert receipt["model_calls"] == 0 and receipt["network_calls"] == 0

    rows = {row["opaque_id"]: row for row in receipt["traces"]}
    assert rows[opaque_id("c-journal")]["reason"] == "unbound_event_journal_record"
    assert rows[opaque_id("d-secret")]["bucket"] == SECRET_VETO
    assert rows[opaque_id("h-hydration")]["bucket"] == HYDRATION_UNAVAILABLE
    assert rows[opaque_id("i-hydrated")]["bucket"] == SAFE
    assert rows[opaque_id("i-hydrated")]["rendered_bytes"] >= len(HYDRATED_TEXT)
    assert receipt["safe_inputs"]["messages"] == sum(
        rows[key]["messages"] for key in map(
            opaque_id, ("a-goodfire", "b-own-mimo", "i-hydrated"),
        )
    )
    text = json.dumps(receipt)
    for marker in ("a-goodfire", "Synthetic actor request.", "sk-abcdefghij"):
        assert marker not in text


def test_dataset_scope_never_reprices_out_of_scope_rows(corpus: Corpus) -> None:
    receipt = corpus.scan(dataset=GOODFIRE)
    cohorts = receipt["cohorts"]
    assert cohorts["basis"] == f"dataset:{GOODFIRE}"
    assert cohorts["db_total"] == 9
    assert cohorts["dev_admitted"] == 5
    assert cohorts["dev_admitted_all_datasets"] == 7
    assert cohorts["selected"] == 5
    assert cohorts["admitted_not_selected"] == 0
    assert cohorts["outside_dev_admission"] == 2
    assert sum(receipt["buckets"].values()) == 5
    # A non-existent dataset is not silently empty: the store vetoes it.
    with pytest.raises(ValueError, match="Dataset is not allowed"):
        corpus.scan(dataset="unknown-dataset")


def test_cas_hydration_replaces_previews_and_failure_is_unpriced(corpus: Corpus) -> None:
    hydrated = corpus.scan(include_traces=True)
    rows = {row["opaque_id"]: row for row in hydrated["traces"]}
    assert rows[opaque_id("i-hydrated")]["rendered_bytes"] >= len(HYDRATED_TEXT)
    full_bytes = hydrated["safe_inputs"]["rendered_bytes"]

    digest = hashlib.sha256(HYDRATED_TEXT.encode("utf-8")).hexdigest()
    (corpus.cas_root / "sha256" / digest).unlink()
    after = corpus.scan()
    assert after["buckets"][HYDRATION_UNAVAILABLE] == 2
    assert after["buckets"][SAFE] == 2
    assert after["safe_inputs"]["rendered_bytes"] < full_bytes


def test_limit_keeps_full_cohort_counts_and_own_runs_first(corpus: Corpus) -> None:
    full = corpus.scan()
    bounded = corpus.scan(limit=2, include_traces=True)
    assert bounded["cohorts"]["selected"] == 2
    assert bounded["cohorts"]["dev_admitted"] == full["cohorts"]["dev_admitted"]
    assert bounded["cohorts"]["admitted_not_selected"] == 5
    assert sum(bounded["buckets"].values()) == 2
    assert [row["opaque_id"] for row in bounded["traces"]][0] == opaque_id("b-own-mimo")
    assert bounded["hashes"]["projection"] != full["hashes"]["projection"]
    with pytest.raises(ValueError, match="limit must be >= 1"):
        corpus.scan(limit=0)


def test_hashes_are_stable_and_track_content_and_identity(corpus: Corpus) -> None:
    first = corpus.scan()
    second = corpus.scan()
    assert first["hashes"] == second["hashes"]
    corpus.con.execute(
        "UPDATE trace_steps SET content_text = content_text || ' changed' WHERE trace_id = ?",
        ("a-goodfire",),
    )
    corpus.con.commit()
    third = corpus.scan()
    assert third["hashes"]["projection"] != first["hashes"]["projection"]
    assert third["source"]["identity_sha256"] == first["source"]["identity_sha256"]


@pytest.mark.skipif(not NO_ENCODER, reason="tiktoken is installed in this environment")
def test_bytes_only_receipt_when_no_encoder_is_installed(corpus: Corpus) -> None:
    assert load_tokenizer("auto") is None
    with pytest.raises(RuntimeError, match="tiktoken is not installed"):
        load_tokenizer("tiktoken:o200k_base")
    receipt = corpus.scan()
    assert receipt["tokenization"]["installed"] is False
    assert receipt["tokenization"]["encoding"] is None
    assert "UNKNOWN" in receipt["tokenization"]["claim"]
    assert receipt["safe_inputs"]["tokens"] is None
    assert receipt["safe_inputs"]["rendered_bytes"] > 0


def test_tokenizer_spec_validation() -> None:
    with pytest.raises(ValueError, match="unsupported tokenizer spec"):
        load_tokenizer("gpt2")
    with pytest.raises(ValueError, match="must name a tiktoken encoding"):
        load_tokenizer("tiktoken:")
    assert load_tokenizer("none") is None


class _ByteEncoder:
    """Deterministic stand-in for an installed encoder, counting UTF-8 bytes."""

    name = "test:utf8-bytes"

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


def test_quote_uses_safe_inputs_only_and_keeps_the_corpus_unknown(
    corpus: Corpus, tmp_path: Path,
) -> None:
    path = _write(tmp_path, _scenario_document(assumptions={
        "cached_input_fraction": 0.25,
        "output_tokens_per_call": 5,
        "storage_usd_per_gb_month": 0.0,
    }))
    scenario = load_scenario(path, model="synthetic/model")
    receipt = corpus.scan(tokenizer=_ByteEncoder(), scenario=scenario)
    tokens = receipt["safe_inputs"]["tokens"]
    assert tokens == receipt["safe_inputs"]["rendered_bytes"]
    quote = receipt["quote"]
    components = quote["components"]
    expected_uncached = tokens * 0.75 * 0.15 / 1_000_000
    expected_cached = tokens * 0.25 * 0.03 / 1_000_000
    expected_output = 5 * 1 * 3 * 0.60 / 1_000_000
    assert components["input_uncached_per_generation"]["usd"] == pytest.approx(
        expected_uncached,
    )
    assert components["input_cached_per_generation"]["usd"] == pytest.approx(expected_cached)
    assert components["output_per_generation"]["usd"] == pytest.approx(expected_output)
    assert components["safe_cohort_total"]["usd"] == pytest.approx(
        expected_uncached + expected_cached + expected_output,
    )
    assert components["full_corpus_total"] == {
        "usd": None,
        "missing": ["admitted_not_selected", "outside_scope", "unavailable_inputs"],
    }
    assert quote["basis"]["safe_traces"] == 3
    assert quote["basis"]["unavailable_traces"] == 3
    assert quote["basis"]["excluded"] == {
        ACTOR_HISTORY_UNAVAILABLE: 1, HYDRATION_UNAVAILABLE: 1, SECRET_VETO: 1,
    }
    assert receipt["hashes"]["quote"] == quote["quote_sha256"]
    # Changing only the scenario version changes the quote hash, not the projection.
    other = _write(tmp_path, _scenario_document(
        version="test-2026-10-28.2", assumptions={
            "cached_input_fraction": 0.25,
            "output_tokens_per_call": 5,
            "storage_usd_per_gb_month": 0.0,
        },
    ), name="other.json")
    changed = corpus.scan(
        tokenizer=_ByteEncoder(),
        scenario=load_scenario(other, model="synthetic/model"),
    )
    assert changed["hashes"]["projection"] == receipt["hashes"]["projection"]
    assert changed["hashes"]["quote"] != receipt["hashes"]["quote"]


def test_quote_arithmetic_known_components_and_scale() -> None:
    quote = _quote(
        _scenario(
            generations=2,
            retries_per_generation=1,
            calls_per_generation=2,
            cached_input_fraction=0.25,
            output_tokens_per_call=100,
            storage_usd_per_gb_month=2.0,
        ),
        traces=10,
        rendered_bytes=500_000_000,
    )
    components = quote["components"]
    assert components["input_uncached_per_generation"]["usd"] == pytest.approx(0.1125)
    assert components["input_cached_per_generation"]["usd"] == pytest.approx(0.0075)
    assert components["output_per_generation"]["usd"] == pytest.approx(0.0012)
    assert components["input_uncached_total"]["usd"] == pytest.approx(0.45)
    assert components["input_cached_total"]["usd"] == pytest.approx(0.03)
    assert components["output_total"]["usd"] == pytest.approx(0.0048)
    assert components["storage_month_1"]["usd"] == pytest.approx(1.0)
    assert components["safe_cohort_total"]["usd"] == pytest.approx(1.4848)
    assert components["full_corpus_total"] == {"usd": pytest.approx(1.4848), "missing": []}


def test_quote_missing_assumptions_are_never_zero() -> None:
    missing_output = _quote(_scenario(output_tokens_per_call=None, storage_usd_per_gb_month=0.0))
    components = missing_output["components"]
    assert components["input_uncached_per_generation"]["usd"] is not None
    assert components["output_per_generation"] == {
        "usd": None, "missing": ["output_tokens_per_call"],
    }
    assert components["safe_cohort_total"]["usd"] is None
    assert components["safe_cohort_total"]["missing"] == ["output_tokens_per_call"]

    missing_retries = _quote(_scenario(
        retries_per_generation=None, output_tokens_per_call=10,
        storage_usd_per_gb_month=0.0,
    ))
    components = missing_retries["components"]
    assert components["input_uncached_per_generation"]["usd"] is not None
    assert components["input_uncached_total"]["usd"] is None
    assert components["input_uncached_total"]["missing"] == ["retries_per_generation"]
    assert components["safe_cohort_total"]["usd"] is None

    missing_generations = _quote(_scenario(
        generations=None, output_tokens_per_call=10, storage_usd_per_gb_month=0.0,
    ))
    assert missing_generations["components"]["safe_cohort_total"]["missing"] == [
        "generations",
    ]

    missing_tokens = _quote(_scenario(), input_tokens=None)
    components = missing_tokens["components"]
    assert components["input_uncached_per_generation"] == {
        "usd": None, "missing": ["input_tokens"],
    }
    assert components["input_cached_per_generation"] == {
        "usd": None, "missing": ["input_tokens"],
    }

    missing_fraction = _quote(_scenario(cached_input_fraction=None))
    assert missing_fraction["components"]["input_uncached_per_generation"]["missing"] == [
        "cached_input_fraction",
    ]


def test_quote_cache_tier_distinctions() -> None:
    zero_fraction = _quote(_scenario(
        cached_input_fraction=0.0, rate=_rate_card(cached_input_usd_per_mtok=None),
        output_tokens_per_call=10, storage_usd_per_gb_month=0.0,
    ))
    assert zero_fraction["components"]["input_cached_per_generation"] == {
        "usd": 0.0, "missing": [],
    }
    assert zero_fraction["components"]["safe_cohort_total"]["usd"] is not None

    cached_missing_rate = _quote(_scenario(
        cached_input_fraction=0.5, rate=_rate_card(cached_input_usd_per_mtok=None),
        output_tokens_per_call=10, storage_usd_per_gb_month=0.0,
    ))
    assert cached_missing_rate["components"]["input_uncached_per_generation"]["usd"] == (
        pytest.approx(500_000 * 0.15 / 1_000_000)
    )
    assert cached_missing_rate["components"]["input_cached_per_generation"] == {
        "usd": None, "missing": ["cached_input_rate"],
    }
    assert cached_missing_rate["components"]["safe_cohort_total"]["usd"] is None

    zero_tokens = _quote(_scenario(), input_tokens=0, traces=0)
    assert zero_tokens["components"]["input_uncached_per_generation"] == {
        "usd": 0.0, "missing": [],
    }


def test_quote_never_prices_unsafe_rows_as_safe() -> None:
    known = _scenario(output_tokens_per_call=10, storage_usd_per_gb_month=0.0)
    clean = _quote(known)
    assert clean["components"]["safe_cohort_total"]["usd"] is not None
    assert clean["components"]["full_corpus_total"]["usd"] == pytest.approx(
        clean["components"]["safe_cohort_total"]["usd"],
    )
    assert clean["components"]["full_corpus_total"]["missing"] == []

    scoped = _quote(known, outside_scope=2)
    assert scoped["components"]["full_corpus_total"] == {
        "usd": None, "missing": ["outside_scope"],
    }

    unsafe = _quote(
        known,
        buckets={SAFE: 8, ACTOR_HISTORY_UNAVAILABLE: 1, SECRET_VETO: 1},
        admitted_not_selected=1,
        outside_scope=0,
    )
    assert unsafe["components"]["safe_cohort_total"]["usd"] is not None
    assert unsafe["components"]["full_corpus_total"] == {
        "usd": None,
        "missing": ["admitted_not_selected", "unavailable_inputs"],
    }
    assert unsafe["basis"]["unavailable_traces"] == 2


def test_scenario_validation_is_explicit(tmp_path: Path) -> None:
    missing_key = _scenario_document()
    del missing_key["assumptions"]["cached_input_fraction"]
    with pytest.raises(ValueError, match="missing required keys: cached_input_fraction"):
        load_scenario(_write(tmp_path, missing_key, "missing.json"), model="synthetic/model")

    unknown_key = _scenario_document()
    unknown_key["assumptions"]["assumed_cache_rate"] = 0.5
    with pytest.raises(ValueError, match="unknown keys: assumed_cache_rate"):
        load_scenario(_write(tmp_path, unknown_key, "unknown.json"), model="synthetic/model")

    with pytest.raises(ValueError, match="no rates for model"):
        load_scenario(_write(tmp_path, _scenario_document(), "model.json"), model="other/model")

    with pytest.raises(ValueError, match="no 'batch' tier"):
        load_scenario(
            _write(tmp_path, _scenario_document(), "tier.json"), model="synthetic/model",
            tier="batch",
        )

    wrong_schema = _scenario_document()
    wrong_schema["schema"] = "evallab.cost_scenario/v0"
    with pytest.raises(ValueError, match="unsupported scenario schema"):
        load_scenario(_write(tmp_path, wrong_schema, "schema.json"), model="synthetic/model")

    no_rates = _scenario_document()
    del no_rates["rates"]
    with pytest.raises(ValueError, match="has no rates and --rates pinned"):
        load_scenario(_write(tmp_path, no_rates, "rates.json"), model="synthetic/model")

    ambiguous = _scenario_document()
    with pytest.raises(ValueError, match="ambiguous"):
        load_scenario(
            _write(tmp_path, ambiguous, "ambiguous.json"), model="synthetic/model", pinned=True,
        )


def test_scenario_tier_and_pinned_rate_sources(tmp_path: Path) -> None:
    tiers = _scenario_document(tiers={
        "standard": {
            "input_usd_per_mtok": 0.15, "cached_input_usd_per_mtok": 0.03,
            "output_usd_per_mtok": 0.60, "source": "https://example.invalid/standard",
            "retrieved_on": "2026-10-28",
        },
        "batch": {
            "input_usd_per_mtok": 0.075, "cached_input_usd_per_mtok": 0.015,
            "output_usd_per_mtok": 0.30, "source": "https://example.invalid/batch",
            "retrieved_on": "2026-10-28",
        },
    })
    path = _write(tmp_path, tiers)
    batch = load_scenario(path, model="synthetic/model", tier="batch")
    assert batch.rate.input_usd_per_mtok == 0.075
    standard = load_scenario(path, model="synthetic/model", tier="standard")
    assert standard.rate.input_usd_per_mtok == 0.15
    assert batch.sha256 == standard.sha256
    assert batch.rate.source.endswith("/batch")

    from evallab.interpretation.price_table import PRICE_TABLE

    entry = PRICE_TABLE[0]
    no_rates = _scenario_document()
    del no_rates["rates"]
    pinned = load_scenario(
        _write(tmp_path, no_rates, "pinned.json"), model=entry.key, pinned=True,
    )
    assert pinned.rate.input_usd_per_mtok == entry.input_usd_per_mtok
    assert pinned.rate.cached_input_usd_per_mtok == entry.cached_input_usd_per_mtok
    assert pinned.rate.output_usd_per_mtok == entry.output_usd_per_mtok
    assert pinned.rate.retrieved_on == entry.retrieved_on
    assert pinned.rate.source == entry.source_url
    with pytest.raises(ValueError, match="no pinned list price"):
        load_scenario(
            _write(tmp_path, no_rates, "pinned-unknown.json"),
            model="synthetic/unknown", pinned=True,
        )
    with pytest.raises(ValueError, match="single standard tier"):
        load_scenario(
            _write(tmp_path, no_rates, "pinned-tier.json"), model=entry.key,
            tier="batch", pinned=True,
        )


def test_main_writes_content_free_receipt_only(corpus: Corpus, tmp_path: Path) -> None:
    out = tmp_path / "receipt.json"
    status = main([
        "--db", str(corpus.db_path),
        "--repo-root", str(corpus.root),
        "--evidence", str(corpus.evidence),
        "--cas-root", str(corpus.cas_root),
        "--tokenizer", "none",
        "--out", str(out),
    ])
    receipt_text = out.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert status == 0
    assert receipt["schema"] == "evallab.trace_cost_inventory/v1"
    assert receipt["tokenization"]["installed"] is False
    assert receipt["hashes"]["projection"]
    assert receipt["source"]["db_name"] == "cloud.sqlite"
    assert receipt["source"]["db_bytes"] == corpus.db_path.stat().st_size
    assert receipt["quote"] is None
    assert receipt["traces"] is None
    for marker in ("Synthetic actor", "sk-abcdefghij", "a-goodfire", "Hydration request."):
        assert marker not in receipt_text


def test_main_prints_aggregates_and_rejects_incomplete_scenario_args(
    corpus: Corpus, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "receipt.json"
    scenario = _write(tmp_path, _scenario_document())
    assert main([
        "--db", str(corpus.db_path),
        "--repo-root", str(corpus.root),
        "--evidence", str(corpus.evidence),
        "--cas-root", str(corpus.cas_root),
        "--tokenizer", "none",
        "--scenario", str(scenario),
        "--model", "synthetic/model",
        "--out", str(out),
    ]) == 0
    printed = capsys.readouterr().out
    summary = json.loads(printed)
    assert summary["buckets"] == {
        SAFE: 3,
        ACTOR_HISTORY_UNAVAILABLE: 1,
        HYDRATION_UNAVAILABLE: 1,
        SECRET_VETO: 1,
    }
    assert summary["quote"]["full_corpus_total"] == {
        "usd": None,
        "missing": [
            "admitted_not_selected",
            "input_tokens",
            "output_tokens_per_call",
            "outside_scope",
            "storage_usd_per_gb_month",
            "unavailable_inputs",
        ],
    }
    for marker in ("Synthetic actor", "sk-abcdefghij", "a-goodfire"):
        assert marker not in printed
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["hashes"]["projection"] == summary["hashes"]["projection"]

    with pytest.raises(SystemExit):
        main(["--db", str(corpus.db_path), "--scenario", str(scenario)])
    with pytest.raises(SystemExit):
        main(["--db", str(corpus.db_path), "--rates", "pinned"])
    with pytest.raises(SystemExit):
        main(["--db", str(corpus.db_path), "--limit", "0"])


def test_read_only_connection_is_sufficient(corpus: Corpus) -> None:
    with closing(sqlite3.connect(corpus.db_path.as_uri() + "?mode=ro", uri=True)) as con:
        receipt = inventory(
            con, repo_root=corpus.root, evidence_path=corpus.evidence,
            cas_root=corpus.cas_root, tokenizer=None,
        )
        assert receipt["cohorts"]["selected"] == 6
        assert not con.in_transaction
