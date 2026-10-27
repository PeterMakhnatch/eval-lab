"""Inert synthetic evidence: trust tiers order, filter and shape retrieved examples."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

TIER1, TIER2, TIER3 = "tier1_container_verified", "tier2_consensus_labeled", "tier3_provisional"


class SyntheticRegistry(CorpusRegistry):
    """Valid split authority that admits the synthetic non-canonical DEV corpus."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


@pytest.fixture(params=(True, False), ids=("sqlite-fts", "sqlite-like"))
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "evidence.sqlite", sqlite_fts=request.param) as database:
        yield database


@pytest.fixture
def retriever(db: CloudTraceDatabase) -> CloudTraceRetriever:
    return CloudTraceRetriever(db, registry=SyntheticRegistry())


def add_task(db: CloudTraceDatabase, task_id: str, instruction: str = "unrelated instructions") -> None:
    db.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": instruction,
    })


def add_trace(
    db: CloudTraceDatabase, task_id: str, tier: str, steps: list[tuple[str, str]],
    trace_id: str | None = None, **extra: Any,
) -> None:
    db.upsert_trace({
        "trace_id": trace_id or f"{task_id}/trace", "run_id": "synthetic-run",
        "task_id": task_id, "model_id": "synthetic-model", "is_exploit": True,
        "trust_tier": tier, **extra,
    }, steps=[
        {"step_index": index, "role": role, "content_text": text}
        for index, (role, text) in enumerate(steps)
    ])


def add_issue(db: CloudTraceDatabase, task_id: str, issue_id: str, tier: str, text: str) -> None:
    db.upsert_task_issue({
        "issue_id": issue_id, "task_id": task_id, "defect_class": "missing_output_check",
        "direction": "wrong_pass", "status": "confirmed", "reproducer_script": text,
        "original_reward": 1.0, "repaired_reward": 0.0, "trust_tier": tier,
    })


def task_ids(examples: list[Any]) -> list[str]:
    return [example.task_id for example in examples]


def test_container_verified_ranks_first_and_provisional_needs_an_explicit_request(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    add_task(db, "verified-weak")
    add_trace(db, "verified-weak", TIER1, [("assistant", "pytest grader")])
    add_task(db, "consensus-strong", "pytest grader pytest grader")
    add_trace(db, "consensus-strong", TIER2, [("assistant", "pytest grader")] * 4)
    add_task(db, "provisional-strongest", "pytest grader")
    add_trace(db, "provisional-strongest", TIER3, [("assistant", "pytest grader")] * 9)

    default = retriever.retrieve_similar_examples("pytest grader", None, 5)
    assert task_ids(default) == ["verified-weak", "consensus-strong"]
    assert [example.trust_tier for example in default] == [TIER1, TIER2]
    assert default[1].score > default[0].score
    assert "- trust: tier1_container_verified" in default[0].prompt_text
    assert task_ids(retriever.retrieve_similar_examples("pytest grader", None, 1)) == ["verified-weak"]

    widened = retriever.retrieve_similar_examples("pytest grader", None, 5, include_provisional=True)
    assert task_ids(widened) == ["verified-weak", "consensus-strong", "provisional-strongest"]
    assert widened[2].provenance["trust_tier"] == TIER3
    with pytest.raises(ValueError, match="include_provisional"):
        retriever.retrieve_similar_examples("pytest grader", include_provisional=1)  # type: ignore[arg-type]


def test_provisional_evidence_is_never_mixed_into_a_trusted_example(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    add_task(db, "mixed")
    add_issue(db, "mixed", "mixed/verified", TIER1, "pytest grader")
    add_issue(db, "mixed", "mixed/lead", TIER3, "pytest grader pytest grader accepts")
    add_trace(db, "mixed", TIER3, [("assistant", "pytest grader")])

    (example,) = retriever.retrieve_similar_examples("pytest grader", "task_quality", 5)
    assert example.issue_id == "mixed/verified"
    assert example.trace_id is None and example.steps == ()
    assert example.provenance["issue_trust_tier"] == TIER1
    assert example.provenance["trace_trust_tier"] is None
    # When asked to include leads, the stronger tier is still the one shown.
    (widened,) = retriever.retrieve_similar_examples(
        "pytest grader", "task_quality", 5, include_provisional=True,
    )
    assert widened.issue_id == "mixed/verified"


def add_window_trace(
    db: CloudTraceDatabase, task_id: str, texts: dict[int, str],
    flags: dict[int, Any] | None = None, **extra: Any,
) -> None:
    # Step 0 is the user prompt; then assistant/tool turns alternate, each with
    # publisher-style source metadata that is not a flag.
    db.upsert_trace({
        "trace_id": f"{task_id}/trace", "run_id": "synthetic-run", "task_id": task_id,
        "model_id": "synthetic-model", "is_exploit": True, **extra,
    }, steps=[{
        "step_index": index, "role": "user" if index == 0 else ("assistant", "tool")[(index - 1) % 2],
        "content_text": texts.get(index, f"routine step {index}"),
        "flags_json": (flags or {}).get(index, {"native_node_index": index, "source_passages": []}),
    } for index in range(15)])


def test_labeled_trace_returns_a_window_centered_on_its_best_passage(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    # The setup steps match the query best, but only the window around the
    # best-matching labeled passage (step 9, not 3) is returned.
    add_task(db, "labeled")
    add_window_trace(db, "labeled", {
        1: "pytest grader setup pytest grader", 2: "pytest grader install",
        3: "edit the readme", 9: "overwrite the pytest grader", 14: "pytest grader again",
    }, trust_tier=TIER2, flagged_passage_steps=[3, 9])
    for kind in (None, "reward_seeker", "misalignment"):
        (example,) = retriever.retrieve_similar_examples("pytest grader", kind, 5)
        assert [step["step_index"] for step in example.trace_steps] == [6, 7, 8, 9, 10, 11, 12]
        assert [step["step_index"] for step in example.trace_steps if step["flagged"]] == [9]
        assert [step.step_index for step in example.steps if step.flagged] == [9]
        assert example.provenance["passage_window"] is True
        assert example.provenance["flagged_step_indices"] == [9]
        assert "step 9 [assistant] FLAGGED overwrite" not in example.prompt_text  # no command text
        assert "- step 9 [assistant] FLAGGED observed: overwrite the pytest grader" in example.prompt_text


def test_compact_card_summarizes_a_window_and_its_pointer_expands_to_the_same_steps(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    add_task(db, "labeled")
    add_window_trace(db, "labeled", {
        1: "pytest grader setup", 3: "I could hardcode the expected value",
        9: "overwrite the pytest grader\nrm tests/check.py\nexit 0\nsleep 1",
    }, trust_tier=TIER2, flagged_passage_steps=[9], verified_receipt_json={
        "status": "verified", "source_label": {"categories": ["test_modification"]},
    })
    (example,) = retriever.retrieve_similar_examples("pytest grader", "reward_seeker", 5)
    full = retriever.format_for_prompt([example])
    card = retriever.format_for_prompt([example], format_mode="compact_cards")
    assert card.splitlines()[4:] == [
        "### 1. labeled [tier2_consensus_labeled]",
        "- mechanism: test_modification",
        "- flagged:",
        "  | overwrite the pytest grader",
        "  | rm tests/check.py",
        "  | exit 0",
        "- verify: publisher three-vote passage label; receipt verified",
        # Flags cover the whole trace (steps 1 and 3 lie outside the window).
        "- flags: reasoning_eval_awareness@1, reasoning_mentions_hardcode@3",
        "- expand: trace:labeled/trace:6-12",
    ]
    assert example.feature_flags == {
        "reasoning_eval_awareness": [1, 9], "reasoning_mentions_hardcode": [3],
    }
    assert "routine step" not in card and "routine step" in full

    expanded = retriever.expand_pointer(example.pointer)
    assert expanded["trust_tier"] == TIER2
    assert expanded["steps"] == example.trace_steps
    wider = retriever.expand_pointer("trace:labeled/trace", max_steps=4)
    assert [step["step_index"] for step in wider["steps"]] == [1, 2, 3]  # step 0 is the user turn
    with pytest.raises(ValueError, match="format_mode"):
        retriever.format_for_prompt([example], format_mode="cards")


def test_issue_card_and_pointer_expansion_fail_closed(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    add_task(db, "verified")
    add_issue(db, "verified", "verified/issue", TIER1, "pytest grader\nprintf ok > out\nexit 0\ntrue")
    add_task(db, "lead")
    add_issue(db, "lead", "lead/issue", TIER3, "pytest grader lead")
    add_window_trace(db, "lead", {1: "pytest grader lead"}, trust_tier=TIER3)
    (example,) = retriever.retrieve_similar_examples("pytest grader", "task_quality", 5)
    card = retriever.format_for_prompt([example], format_mode="compact_cards").splitlines()[4:]
    assert card == [
        "### 1. verified [tier1_container_verified]",
        "- mechanism: missing_output_check (wrong_pass)",
        "- flagged:", "  | pytest grader", "  | printf ok > out", "  | exit 0",
        "- verify: original reward 1 -> repaired reward 0",
        "- expand: issue:verified/issue",
    ]
    assert retriever.expand_pointer("issue:verified/issue")["issue"]["reproducer_script"].endswith("true")

    # Provisional rows expand only on request; unknown rows and sealed identities never.
    for pointer in ("issue:lead/issue", "trace:lead/trace:0-3", "trace:missing", "issue:missing"):
        with pytest.raises(ValueError, match="returnable"):
            retriever.expand_pointer(pointer)
    assert retriever.expand_pointer("issue:lead/issue", include_provisional=True)["trust_tier"] == TIER3
    sealed = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=("verified",))
    with pytest.raises(ValueError, match="sealed"):
        sealed.expand_pointer("issue:verified/issue")
    with pytest.raises(ValueError, match="pointer must be"):
        retriever.expand_pointer("verified/issue")


def test_trace_without_labeled_steps_uses_passage_marks_and_metadata_never_flags(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    add_task(db, "marked")
    add_window_trace(db, "marked", {1: "pytest grader setup", 5: "pytest grader overwrite"}, {
        5: {"passage_positive": True, "passage_labels": {"passage_positive": False}},
        7: {"passage_positive": False, "note": '"passage_positive": true'},
    }, trust_tier=TIER2)
    (example,) = retriever.retrieve_similar_examples("pytest grader", "reward_seeker", 5)
    assert [step["step_index"] for step in example.trace_steps] == [2, 3, 4, 5, 6, 7, 8]
    assert example.provenance["flagged_step_indices"] == [5]

    # Source metadata alone flags nothing: no window, ordinary best steps.
    add_task(db, "unmarked")
    add_window_trace(db, "unmarked", {1: "kitten mitten setup", 2: "kitten mitten install"},
                     trust_tier=TIER2)
    (plain,) = retriever.retrieve_similar_examples("kitten mitten", "reward_seeker", 5)
    assert plain.provenance["passage_window"] is False
    assert [step["step_index"] for step in plain.trace_steps] == [1, 2, 3]
    assert not any(step["flagged"] for step in plain.trace_steps)


@pytest.mark.parametrize("issue_kind", (None, "missing_output_check"))
def test_issue_led_queries_fall_back_to_ranked_partial_matches(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, issue_kind: str | None,
) -> None:
    # No document holds every term. Tier 1 outranks tier 2 even with fewer
    # matched terms; within tier 2 more terms rank higher; tier 3 never returns.
    for task_id, tier, text in (
        ("verified-one-term", TIER1, "conftest"),
        ("consensus-two-terms", TIER2, "conftest exit"),
        ("consensus-one-term", TIER2, "exit"),
        ("provisional-three-terms", TIER3, "conftest exit status"),
    ):
        add_task(db, task_id)
        add_issue(db, task_id, f"{task_id}/issue", tier, text)
    examples = retriever.retrieve_similar_examples(
        "conftest exit status hook", "task_quality", 5, issue_kind=issue_kind,
    )
    assert task_ids(examples) == ["verified-one-term", "consensus-two-terms", "consensus-one-term"]


@pytest.mark.parametrize("kind", (None, "reward_seeker", "misalignment"))
def test_kind_filtered_queries_fall_back_to_partial_matches_ranked_by_strength(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, kind: str | None,
) -> None:
    # No single document holds every query term, so only the any-term fallback
    # can find these; the trace matching more terms ranks first.
    add_task(db, "two-terms")
    add_trace(db, "two-terms", TIER2, [("assistant", "rewrite conftest"), ("tool", "exit status")],
              flagged_passage_steps=[0, 1])
    add_task(db, "one-term")
    add_trace(db, "one-term", TIER2, [("assistant", "rewrite the readme")], flagged_passage_steps=[0])
    add_task(db, "unrelated")
    add_trace(db, "unrelated", TIER2, [("assistant", "nothing relevant")], flagged_passage_steps=[0])
    examples = retriever.retrieve_similar_examples("rewrite conftest exit status hook", kind, 5)
    assert task_ids(examples) == ["two-terms", "one-term"]



def test_trust_survives_reingestion_and_assignment_is_atomic(db: CloudTraceDatabase) -> None:
    add_task(db, "stored")
    add_trace(db, "stored", TIER2, [("assistant", "a"), ("tool", "b")], flagged_passage_steps=[1])
    add_issue(db, "stored", "stored/issue", TIER1, "text")
    db.upsert_trace({
        "trace_id": "stored/trace", "run_id": "synthetic-run", "task_id": "stored",
        "model_id": "synthetic-model", "outcome": "reingested",
    })
    db.upsert_task_issue({
        "issue_id": "stored/issue", "task_id": "stored", "defect_class": "missing_output_check",
        "direction": "wrong_pass", "status": "confirmed",
    })

    def stored() -> tuple[Any, ...]:
        return (
            db._execute("SELECT trust_tier, flagged_passage_steps FROM agent_traces").fetchone(),
            db._execute("SELECT trust_tier FROM task_issues").fetchone(),
        )

    assert stored() == ((TIER2, "[1]"), (TIER1,))
    with pytest.raises(ValueError, match="stored steps"):
        db.assign_trust_tiers(
            issues={"stored/issue": TIER3},
            traces={"stored/trace": {"trust_tier": TIER1, "flagged_passage_steps": [7]}},
        )
    with pytest.raises(KeyError):
        db.assign_trust_tiers(issues={"stored/issue": TIER3, "missing": TIER3})
    with pytest.raises(ValueError, match="trust_tier"):
        db.assign_trust_tiers(issues={"stored/issue": "tier0"})
    assert stored() == ((TIER2, "[1]"), (TIER1,))
    changed = db.assign_trust_tiers(
        issues={"stored/issue": TIER1}, traces={"stored/trace": {"trust_tier": TIER3}},
    )
    assert changed == {"task_issues": 0, "agent_traces": 1}
    assert stored() == ((TIER3, "[]"), (TIER1,))
    with pytest.raises(ValueError, match="supplied steps"):
        add_trace(db, "stored", TIER2, [("assistant", "a")], trace_id="other", flagged_passage_steps=[3])


def test_stores_created_before_trust_tiers_gain_provisional_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                split TEXT NOT NULL, instruction_text TEXT NOT NULL,
                original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
                metadata_json TEXT NOT NULL);
            CREATE TABLE task_issues (issue_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                defect_class TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
                witness_digest TEXT, original_reward DOUBLE PRECISION,
                repaired_reward DOUBLE PRECISION, citation_file TEXT, citation_line INTEGER,
                reproducer_script TEXT, cas_uri TEXT);
            CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                task_id TEXT NOT NULL, model_id TEXT NOT NULL, outcome TEXT NOT NULL,
                reward DOUBLE PRECISION, is_exploit INTEGER NOT NULL,
                is_task_defect_fail INTEGER NOT NULL, step_count INTEGER NOT NULL,
                raw_cas_uri TEXT, verified_receipt_json TEXT);
            INSERT INTO corpus_tasks VALUES ('legacy', 'synthetic-corpus', 'dev', 'x', NULL, NULL, NULL, '{}');
            INSERT INTO task_issues VALUES ('legacy/issue', 'legacy', 'c', 'wrong_pass', 'confirmed',
                NULL, 1.0, 0.0, NULL, NULL, NULL, NULL);
            INSERT INTO agent_traces VALUES ('legacy/trace', 'r', 'legacy', 'm', 'o', NULL, 1, 0, 0, NULL, NULL);
        """)
    with CloudTraceDatabase(path, sqlite_fts=False) as database:
        assert database._execute("SELECT trust_tier FROM task_issues").fetchone() == (TIER3,)
        assert database._execute(
            "SELECT trust_tier, flagged_passage_steps FROM agent_traces",
        ).fetchone() == (TIER3, "[]")
        with pytest.raises(sqlite3.IntegrityError):
            database._execute("UPDATE agent_traces SET trust_tier = 'tier0'")
