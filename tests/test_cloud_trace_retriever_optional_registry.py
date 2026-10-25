"""Optional default registry and unified filters; synthetic evidence only.

No corpus task or answer bytes are read: every registry index is a synthetic tree
under ``tmp_path`` and every database is a throwaway SQLite file, with FTS5 on and off.
The default registry is the retriever's own ``CorpusRegistry()`` call, redirected to
those trees (or made to fail) by patching ``CorpusRegistry.__init__``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from evallab.production import CloudTraceRetriever, RetrievedExample, admitted_cloud_task_ids
from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import CorpusRegistry

SECONDARY = "tb2-to-tb21-v1"
SEALED_NAME = "synthetic-sealed"
SEALED_ITEM = "synthetic-sealed-item"
SEALED_DIGEST = "synthetic-sealed-digest"
KINDS = ("task_quality", "reward_seeker", "misalignment")
ASSIGNMENTS = (
    "key_id,item_id,task,split\n"
    f"{SECONDARY},secondary-dev,,dev\n"
    f"{SECONDARY},secondary-test,,test\n"
    "secondary-key-x,shared-item,shared-task,test\n"
    "secondary-key-x,cross-test-item,cross-test-task,test\n"
    "secondary-key-y,shared-item,shared-task,dev\n"
    "secondary-key-y,y-only-dev,,dev\n"
)
FAILURES = (
    "init-file-not-found", "init-value-error", "missing-index", "corrupt-items",
    "corrupt-assignments", "corrupt-inventory", "mismatched-inventory", "incomplete-inventory",
    "missing-assignments", "empty-assignments", "header-only-assignments",
    "missing-field-assignments", "malformed-csv-assignments",
)


@pytest.fixture(params=(True, False), ids=("sqlite-fts", "sqlite-like"))
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "evidence.sqlite", sqlite_fts=request.param) as database:
        yield database


def write_index(root: Path) -> Path:
    """A minimal valid research index: one synthetic TEST row plus split assignments."""
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        (keys / dataset).mkdir(parents=True)
        (keys / dataset / "items.jsonl").write_text("")
    (keys / "tb4-audit-v1/items.jsonl").write_text(json.dumps({
        "item_id": SEALED_ITEM, "split": "test",
        "subject": {"task_id": f"namespace/{SEALED_NAME}@v1", "task_digest": SEALED_DIGEST},
    }) + "\n")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("")
    (keys / "splits").mkdir()
    (keys / "splits/assignments.csv").write_text(ASSIGNMENTS)
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }))
    return root


def install_default_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None = None,
) -> None:
    """Make the retriever's default ``CorpusRegistry()`` load a valid index or fail as named."""
    raised = {
        "init-file-not-found": FileNotFoundError("index absent"),
        "init-value-error": ValueError("index corrupt"),
    }
    if failure in raised:
        def fail(self: CorpusRegistry, *args: Any, **kwargs: Any) -> None:
            raise raised[failure]

        monkeypatch.setattr(CorpusRegistry, "__init__", fail)
        return
    root = tmp_path / "default-index"
    if failure != "missing-index":
        write_index(root)
        keys = root / "research/answer-keys"
        inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
        corruption = {
            "corrupt-items": (keys / "tb4-audit-v1/items.jsonl", "{not json\n"),
            "corrupt-assignments": (
                keys / "splits/assignments.csv", "key_id,item_id,task,split\nk,i,t,maybe\n",
            ),
            "empty-assignments": (keys / "splits/assignments.csv", ""),
            "header-only-assignments": (
                keys / "splits/assignments.csv", "key_id,item_id,task,split\n",
            ),
            "missing-field-assignments": (
                keys / "splits/assignments.csv", "key_id,item_id,task,split\nk,i,t\n",
            ),
            "malformed-csv-assignments": (
                keys / "splits/assignments.csv", 'key_id,item_id,task,split\n"k,i,t,dev\n',
            ),
            "corrupt-inventory": (inventory, "{not json"),
            "mismatched-inventory": (inventory, json.dumps({"dataset_id": "other"})),
            "incomplete-inventory": (inventory, json.dumps({"dataset_id": "mimo-defect-map-v1"})),
        }
        if failure == "missing-assignments":
            (keys / "splits/assignments.csv").unlink()
        elif failure is not None:
            path, text = corruption[failure]
            path.write_text(text)
    load = CorpusRegistry.__init__

    def redirected(self: CorpusRegistry, *args: Any, **kwargs: Any) -> None:
        load(self, root)

    monkeypatch.setattr(CorpusRegistry, "__init__", redirected)


@pytest.fixture
def retriever(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> CloudTraceRetriever:
    install_default_index(monkeypatch, tmp_path)
    return CloudTraceRetriever(db)


def add_task(db: CloudTraceDatabase, task_id: str, **extra: Any) -> None:
    db.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "pytest grader accepts empty output", "cas_uri": "cas://task", **extra,
    })


def add_issue(db: CloudTraceDatabase, task_id: str, issue_id: str, **extra: Any) -> None:
    db.upsert_task_issue({
        "issue_id": issue_id, "task_id": task_id, "defect_class": "missing_output_check",
        "direction": "wrong_pass", "status": "confirmed", "citation_file": "tests/check.py",
        "citation_line": 17, "reproducer_script": "pytest grader accepts empty output",
        "witness_digest": "synthetic-witness-digest", "cas_uri": "cas://issue", **extra,
    })


def add_trace(db: CloudTraceDatabase, task_id: str, trace_id: str, **extra: Any) -> None:
    db.upsert_trace({
        "trace_id": trace_id, "run_id": "synthetic-run", "task_id": task_id,
        "model_id": "synthetic-model", "is_exploit": True,
        "raw_cas_uri": "cas://raw-trace", "verified_receipt_json": {"status": "verified"}, **extra,
    }, steps=[
        {"step_index": 0, "role": "user", "content_text": "Solve the synthetic task."},
        {
            "step_index": 7, "role": "assistant", "tool_name": "terminal",
            "command_or_action": "pytest grader accepts empty output",
            "content_text": "Check the recorded grader boundary.",
            "observation_preview": "pytest grader accepts empty output",
            "observation_cas_uri": "cas://observation", "flags_json": ["verifier_tamper"],
        },
    ])


def seed(
    db: CloudTraceDatabase, task_id: str, *,
    issues: Mapping[str, Mapping[str, Any]] | None = None, trace: bool = True, **task: Any,
) -> None:
    """One task; ``issues`` maps issue id to overrides (default: one confirmed wrong_pass)."""
    add_task(db, task_id, **task)
    for issue_id, extra in (issues if issues is not None else {f"{task_id}/issue": {}}).items():
        add_issue(db, task_id, issue_id, **extra)
    if trace:
        add_trace(db, task_id, f"{task_id}/trace")


def ids(examples: Iterable[RetrievedExample]) -> set[str]:
    return {example.task_id for example in examples}


def rendered(examples: list[RetrievedExample]) -> str:
    """Every output surface a prompt path can carry."""
    return CloudTraceRetriever.format_for_prompt(examples) + json.dumps(
        [example.to_dict() for example in examples],
    ) + "".join(example.prompt_text for example in examples)


Entrypoint = Callable[[CloudTraceRetriever, str], list[RetrievedExample]]
ENTRYPOINTS: dict[str, Entrypoint] = {
    "kind-task-quality": lambda r, q: r.retrieve_similar_examples(q, "task_quality", 100),
    "kind-reward-seeker": lambda r, q: r.retrieve_similar_examples(q, "reward_seeker", 100),
    "kind-misalignment": lambda r, q: r.retrieve_similar_examples(q, "misalignment", 100),
    "kind-none": lambda r, q: r.retrieve_similar_examples(q, None, 100),
    "legacy-keywords": lambda r, q: r.retrieve_similar_examples(q, top_k=100),
    "issue-direction": lambda r, q: r.retrieve_similar_examples(
        q, issue_kind="wrong_pass", top_k=100,
    ),
    "issue-class": lambda r, q: r.retrieve_similar_examples(
        q, issue_kind="missing_output_check", top_k=100,
    ),
    "legacy-exclusion": lambda r, q: r.retrieve_similar_examples(
        q, exclude_task_ids={"absent-task"}, top_k=100,
    ),
    "combined": lambda r, q: r.retrieve_similar_examples(
        q, "reward_seeker", 100, issue_kind="wrong_pass", exclude_task_ids={"absent-task"},
    ),
}

# Rows with one confirmed wrong_pass issue and one exploit trace each. DB labels say DEV for
# most of them; only the registry (or its absence) can tell which are really admissible.
ALIAS = f" {SECONDARY.upper()} "
ADMISSION_ROWS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("plain-dev", {}),
    ("dev-spaced", {"split": " Dev "}),
    ("assigned-dev", {"dataset_id": SECONDARY, "metadata_json": {"item_id": "secondary-dev"}}),
    ("alias-assigned-dev", {"dataset_id": ALIAS, "metadata_json": {"item_id": "secondary-dev"}}),
    ("y-dev-only", {"dataset_id": "secondary-key-y", "metadata_json": {"item_id": "y-only-dev"}}),
    # Fake rows for a key whose per-item assignments say TEST or say nothing.
    ("assigned-test", {"dataset_id": SECONDARY, "metadata_json": {"item_id": "secondary-test"}}),
    ("unassigned", {"dataset_id": SECONDARY, "metadata_json": {"item_id": "secondary-missing"}}),
    # Casing/padding aliases of that key must not slip past its assignments.
    ("alias-assigned-test", {"dataset_id": ALIAS, "metadata_json": {"item_id": "secondary-test"}}),
    ("alias-unassigned", {"dataset_id": ALIAS, "metadata_json": {"item_id": "secondary-missing"}}),
    # TEST under any secondary key denies the identity whatever dataset the row claims.
    ("cross-key-item-row", {
        "dataset_id": "secondary-key-y", "metadata_json": {"item_id": "shared-item"},
    }),
    ("namespace/shared-task@v3", {
        "dataset_id": "secondary-key-y", "metadata_json": {"item_id": "y-only-dev"},
    }),
    ("cross-key-external-item", {"metadata_json": {"item_id": "cross-test-item"}}),
    ("namespace/cross-test-task@v4", {}),
    ("integer-item-id", {"metadata_json": {"item_id": 123}}),
    ("list-item-id", {"metadata_json": {"item_id": ["secondary-dev"]}}),
    ("development-label", {"split": "development"}),
    ("test-label", {"split": "test"}),
    ("heldout-label", {"split": "heldout"}),
    ("unknown-label", {"split": "eval_sealed"}),
    # Registry seals: canonical name, either grader digest, item id.
    (f"namespace/{SEALED_NAME}@v2", {}),
    ("original-digest-sealed", {"original_grader_digest": SEALED_DIGEST}),
    ("repaired-digest-sealed", {"repaired_grader_digest": SEALED_DIGEST}),
    ("item-sealed", {"metadata_json": {"item_id": SEALED_ITEM}}),
)
ADMITTED = {"plain-dev", "dev-spaced", "assigned-dev", "alias-assigned-dev", "y-dev-only"}


def seed_admission_rows(db: CloudTraceDatabase) -> None:
    for task_id, extra in ADMISSION_ROWS:
        seed(db, task_id, **extra)


def test_default_registry_index_admits_only_eligible_dev_evidence(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed_admission_rows(db)
    for name, call in ENTRYPOINTS.items():
        examples = call(retriever, "pytest grader")
        assert ids(examples) == ADMITTED, name
        text = rendered(examples)
        assert [t for t, _ in ADMISSION_ROWS if t not in ADMITTED and t in text] == [], name
        assert SEALED_NAME not in text.casefold(), name


@pytest.mark.parametrize("query", (
    f"inspect {SEALED_NAME}", f"inspect 'namespace/{SEALED_NAME}@v7'",
    f"inspect {SEALED_ITEM}", f"inspect {SEALED_DIGEST.upper()}",
))
def test_default_registry_seals_reject_queries_without_echo(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, query: str,
) -> None:
    seed(db, "plain-dev")
    with pytest.raises(ValueError, match="sealed") as error:
        retriever.retrieve_similar_examples(query, "task_quality")
    assert query not in str(error.value)


@pytest.mark.parametrize("failure", FAILURES)
def test_unavailable_default_registry_admits_no_database_dev_rows(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    install_default_index(monkeypatch, tmp_path, failure)
    seed_admission_rows(db)
    retriever = CloudTraceRetriever(db)
    for name, call in ENTRYPOINTS.items():
        assert call(retriever, "pytest grader") == [], name


@pytest.mark.parametrize("failure", ("init-file-not-found", "missing-index", "corrupt-items"))
@pytest.mark.parametrize(("query", "identifier"), (
    ("inspect music-harmony", "music-harmony"),
    ("inspect 'terminal-bench/music-harmony@v2'", "music-harmony"),
    ("inspect FORMAT-CODE-TASK-000142", "format-code-task-000142"),
))
def test_unavailable_default_registry_keeps_frozen_identifier_guard(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    failure: str, query: str, identifier: str,
) -> None:
    install_default_index(monkeypatch, tmp_path, failure)
    seed(db, "plain-dev")
    retriever = CloudTraceRetriever(db)
    for name, call in ENTRYPOINTS.items():
        with pytest.raises(ValueError, match="sealed|protected") as error:
            call(retriever, query)
        message = str(error.value).casefold()
        assert query.casefold() not in message and identifier not in message, name


@pytest.mark.parametrize("failure", ("init-value-error", "corrupt-inventory"))
@pytest.mark.parametrize("query", ("inspect explicit-seal", "inspect 'ns/explicit-seal@v9'"))
def test_unavailable_default_registry_keeps_explicit_seals(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    failure: str, query: str,
) -> None:
    install_default_index(monkeypatch, tmp_path, failure)
    seed(db, "plain-dev")
    retriever = CloudTraceRetriever(db, sealed_task_ids=("explicit-seal",))
    with pytest.raises(ValueError, match="sealed") as error:
        retriever.retrieve_similar_examples(query)
    assert query not in str(error.value)
    assert retriever.retrieve_similar_examples("pytest grader", top_k=100) == []


def test_explicit_registry_is_used_without_loading_the_default(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = CorpusRegistry(write_index(tmp_path / "explicit-index"))

    def forbidden(self: CorpusRegistry, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("an explicit registry must not trigger the default load")

    monkeypatch.setattr(CorpusRegistry, "__init__", forbidden)
    seed_admission_rows(db)
    retriever = CloudTraceRetriever(db, registry=explicit)
    assert ids(retriever.retrieve_similar_examples("pytest grader", top_k=100)) == ADMITTED
    with pytest.raises(ValueError, match="sealed"):
        retriever.retrieve_similar_examples(f"inspect {SEALED_NAME}")


@pytest.mark.parametrize("failure", (None, "init-file-not-found", "corrupt-inventory"))
def test_argument_validation_does_not_depend_on_the_registry(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    install_default_index(monkeypatch, tmp_path, failure)
    seed(db, "plain-dev")
    retriever = CloudTraceRetriever(db)
    for top_k in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="top_k"):
            retriever.retrieve_similar_examples("pytest grader", top_k=top_k)
        with pytest.raises(ValueError, match="top_k"):
            retriever.retrieve_similar_examples("pytest grader", "task_quality", top_k)
    for kind in ("other", "", "Task_Quality"):
        with pytest.raises(ValueError, match="kind"):
            retriever.retrieve_similar_examples("pytest grader", kind)
    for issue_kind in ("", "   "):
        with pytest.raises(ValueError, match="issue_kind"):
            retriever.retrieve_similar_examples("pytest grader", issue_kind=issue_kind)


@pytest.mark.parametrize("seal", ("ns/", "@v1", "ns/@v1"))
def test_explicit_seal_with_empty_canonical_name_does_not_block_every_query(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seal: str,
) -> None:
    install_default_index(monkeypatch, tmp_path)
    seed(db, "plain-a")
    seed(db, "plain-b")
    retriever = CloudTraceRetriever(db, sealed_task_ids=(seal,))
    assert ids(retriever.retrieve_similar_examples("pytest grader", top_k=100)) == {
        "plain-a", "plain-b",
    }


def test_sealed_identity_in_any_stored_uri_never_reaches_output(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed(db, "clean-control")
    seed(db, "dataset-sealed", dataset_id=f"corpus-{SEALED_NAME}")
    seed(db, "task-cas-sealed", cas_uri=f"cas://{SEALED_NAME.upper()}/task")
    seed(db, "issue-cas-sealed", issues={
        "issue-cas-sealed/issue": {"cas_uri": f"cas://{SEALED_NAME}/issue"},
    })
    seed(db, "trace-cas-sealed")
    add_trace(db, "trace-cas-sealed", "trace-cas-sealed/raw", raw_cas_uri=f"cas://{SEALED_NAME}/raw")
    for name, call in ENTRYPOINTS.items():
        examples = call(retriever, "pytest grader")
        assert "clean-control" in ids(examples), name
        assert not ids(examples) & {"dataset-sealed", "task-cas-sealed"}, name
        assert SEALED_NAME not in rendered(examples).casefold(), name


def test_kind_none_applies_no_worker_filter(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed(db, "wrong-pass-only", trace=False)
    seed(db, "unfair-only", trace=False, issues={
        "unfair-only/issue": {"direction": "unfair_fail", "defect_class": "overstrict_match"},
    })
    seed(db, "exploit-task")

    def found(*args: Any, **kwargs: Any) -> set[str]:
        return ids(retriever.retrieve_similar_examples("pytest grader", *args, top_k=100, **kwargs))

    per_kind = {kind: found(kind) for kind in KINDS}
    # Each worker kind drops something; kind=None drops nothing any worker would keep.
    assert "unfair-only" not in per_kind["reward_seeker"] | per_kind["misalignment"]
    assert found(None) == found() == {"wrong-pass-only", "unfair-only", "exploit-task"}
    assert found(None) >= per_kind["task_quality"] | per_kind["reward_seeker"] | per_kind["misalignment"]
    assert len(retriever.retrieve_similar_examples("pytest grader", top_k=1)) == 1
    assert len(retriever.retrieve_similar_examples("pytest grader", None, 2)) == 2


def test_issue_kind_is_exact_confirmed_only_and_selects_the_matching_issue(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed(db, "ik-wrong-pass", trace=False)
    seed(db, "ik-unfair", trace=False, issues={
        "unfair-issue": {"direction": "unfair_fail", "defect_class": "overstrict_match"},
    })
    seed(db, "ik-multi", trace=False, issues={
        "multi-a": {},
        "multi-b": {"direction": "unfair_fail", "defect_class": "overstrict_match"},
    })
    seed(db, "ik-provisional", trace=False, issues={
        "tentative": {"status": "provisional", "defect_class": "provisional_class"},
    })
    seed(db, "ik-refuted", trace=False, issues={
        "disproved": {
            "status": "refuted", "direction": "unfair_fail", "defect_class": "refuted_class",
        },
    })

    def found(issue_kind: str) -> dict[str, RetrievedExample]:
        examples = retriever.retrieve_similar_examples(
            "pytest grader", issue_kind=issue_kind, top_k=100,
        )
        assert all(example.status == "confirmed" for example in examples)
        assert all(example.issue is not None and example.issue["issue_id"] == example.issue_id
                   for example in examples)
        return {example.task_id: example for example in examples}

    wrong_pass = found("wrong_pass")
    assert set(wrong_pass) == {"ik-wrong-pass", "ik-multi"}
    assert {example.direction for example in wrong_pass.values()} == {"wrong_pass"}
    assert wrong_pass["ik-multi"].issue_id == "multi-a"
    unfair = found("unfair_fail")
    assert set(unfair) == {"ik-unfair", "ik-multi"}
    assert {example.direction for example in unfair.values()} == {"unfair_fail"}
    assert unfair["ik-multi"].issue_id == "multi-b"
    by_class = found("missing_output_check")
    assert set(by_class) == {"ik-wrong-pass", "ik-multi"}
    assert by_class["ik-multi"].issue_id == "multi-a"
    assert {example.defect_class for example in by_class.values()} == {"missing_output_check"}
    overstrict = found("overstrict_match")
    assert set(overstrict) == {"ik-unfair", "ik-multi"}
    assert overstrict["ik-multi"].issue_id == "multi-b"
    # Exact match only, and unconfirmed issues never answer a filter.
    for absent in (
        "missing_output", "wrong", "overstrict", "absent_class", "provisional_class",
        "refuted_class",
    ):
        assert found(absent) == {}, absent


def test_unconfirmed_issues_never_surface_under_issue_oriented_calls(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed(db, "unconfirmed-only", instruction_text="neutral instruction", trace=False, issues={
        "tentative": {"status": "provisional", "reproducer_script": "forbiddenmarker"},
        "disproved": {"status": "refuted", "reproducer_script": "forbiddenmarker"},
    })
    seed(db, "mixed", issues={
        "a-provisional": {"status": "provisional"}, "z-confirmed": {},
    })
    seed(db, "unconfirmed-with-trace", issues={
        "tw-issue": {"status": "provisional", "reproducer_script": "forbiddenmarker"},
    })
    markers = ("tentative", "disproved", "forbiddenmarker", "a-provisional", "tw-issue")
    for name, call in ENTRYPOINTS.items():
        assert call(retriever, "forbiddenmarker") == [], name
        examples = call(retriever, "pytest grader")
        text = rendered(examples)
        assert [marker for marker in markers if marker in text] == [], name
        assert all(example.status in (None, "confirmed") for example in examples), name
        assert {e.issue_id for e in examples if e.task_id == "mixed"} == {"z-confirmed"}, name
    for issue_kind in ("wrong_pass", "missing_output_check"):
        examples = retriever.retrieve_similar_examples(
            "pytest grader", issue_kind=issue_kind, top_k=100,
        )
        assert ids(examples) == {"mixed"}


EXCLUDED = ("namespace-a/empty-output@v1", "namespace-b/empty-output@v2", "empty-output")
KEPT = {"empty-output-extended", "keep-alpha"}


@pytest.mark.parametrize("exclusion", (
    {"namespace-a/empty-output@v1"}, {"empty-output"}, {"empty-output@v9"},
    {"other-namespace/empty-output@v3"}, {"namespace-b/empty-output@v2", "unrelated-task"},
))
@pytest.mark.parametrize("kind", (None, "task_quality"))
def test_leave_one_out_excludes_exact_and_canonical_aliases(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, exclusion: set[str], kind: str | None,
) -> None:
    for task_id in (*EXCLUDED, *KEPT):
        seed(db, task_id)
    # The query names the excluded task, yet that is not a seal: nothing raises, and
    # without an exclusion the named task and its aliases remain retrievable evidence.
    query = "pytest grader empty-output"
    assert ids(retriever.retrieve_similar_examples(query, kind, 100)) == {*EXCLUDED, *KEPT}
    examples = retriever.retrieve_similar_examples(
        query, kind, 100, exclude_task_ids=exclusion,
    )
    assert ids(examples) == KEPT
    assert not [task_id for task_id in EXCLUDED[:2] if task_id in rendered(examples)]
    assert len(retriever.retrieve_similar_examples(
        query, kind, 1, exclude_task_ids=exclusion,
    )) == 1


def test_worker_kind_issue_kind_and_exclusion_compose(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    seed(db, "combo-keep")
    seed(db, "ns-a/combo-target@v1")
    seed(db, "combo-wrong-class", issues={"wrong-class": {"defect_class": "overstrict_match"}})
    seed(db, "combo-unfair", trace=False, issues={"unfair": {"direction": "unfair_fail"}})
    seed(db, "combo-unconfirmed", issues={"unconfirmed": {"status": "provisional"}})
    seed(db, "combo-test-split", split="test")

    def found(*args: Any, **kwargs: Any) -> set[str]:
        return ids(retriever.retrieve_similar_examples("pytest grader", *args, top_k=100, **kwargs))

    worker = found("reward_seeker")
    assert {"combo-keep", "ns-a/combo-target@v1", "combo-wrong-class"} <= worker
    assert not worker & {"combo-unfair", "combo-test-split"}
    # issue_kind alone has no worker filter; unconfirmed and TEST rows never answer it.
    assert found(issue_kind="missing_output_check") == {
        "combo-keep", "ns-a/combo-target@v1", "combo-unfair",
    }
    both = found("reward_seeker", issue_kind="missing_output_check")
    assert both == {"combo-keep", "ns-a/combo-target@v1"}
    assert found(
        "reward_seeker", issue_kind="missing_output_check",
        exclude_task_ids={"ns-a/combo-target@v1"},
    ) == {"combo-keep"}
    assert found(
        "reward_seeker", issue_kind="missing_output_check", exclude_task_ids={"combo-target"},
    ) == {"combo-keep"}
    # Each filter still binds when the others admit the row.
    assert found("misalignment", issue_kind="unfair_fail") == set()
    assert found(
        "reward_seeker", issue_kind="missing_output_check",
        exclude_task_ids={"combo-keep", "ns-a/combo-target@v1"},
    ) == set()


def test_secondary_short_item_label_is_structured_only_but_task_name_is_text_sealed(
    db: CloudTraceDatabase, tmp_path: Path,
) -> None:
    root = write_index(tmp_path / "registry")
    secret = "synthetic-secondary-secret"
    with (root / "research/answer-keys/splits/assignments.csv").open("a") as stream:
        stream.write(f"secondary-short,qa,{secret},test\n")
    registry = CorpusRegistry(root)
    seed(db, "blocked-by-short-item", metadata_json={"item_id": "qa"})
    seed(db, "namespace/" + secret + "@v7")
    seed(db, "qa-prose-control", instruction_text="pytest grader qa checks")
    db.replace_trace_steps("qa-prose-control/trace", [
        {
            "step_index": 1, "role": "assistant", "command_or_action": secret + " pytest grader qa",
            "flags_json": ["synthetic"],
        },
        {
            "step_index": 7, "role": "assistant", "command_or_action": "pytest grader qa checks",
            "flags_json": ["synthetic"],
        },
    ])
    retriever = CloudTraceRetriever(db, registry=registry)
    for kind in (*KINDS, None):
        examples = retriever.retrieve_similar_examples("pytest grader qa", kind, top_k=20)
        assert ids(examples) == {"qa-prose-control"}
        assert examples[0].provenance["step_indices"] == [7]
        assert "qa" in examples[0].trace_steps[0]["command_or_action"]
        assert secret not in rendered(examples)
    with pytest.raises(ValueError, match="sealed"):
        retriever.retrieve_similar_examples("pytest " + secret)


@contextmanager
def identity_reads_only(db: CloudTraceDatabase) -> Iterator[None]:
    """Have SQLite itself reject content reads, rather than trusting a filtered result."""
    identity_columns = {
        "task_id", "dataset_id", "split", "original_grader_digest",
        "repaired_grader_digest", "metadata_json",
    }
    denied = []

    def authorize(
        action: int, table: str | None, column: str | None,
        database: str | None, trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and not (
            table == "corpus_tasks" and column in identity_columns
        ):
            denied.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db._connection.set_authorizer(authorize)
    try:
        yield
    finally:
        db._connection.set_authorizer(None)
    assert denied == []


@pytest.mark.parametrize("failure", FAILURES)
def test_missing_or_invalid_authority_refuses_before_content_reads(
    db: CloudTraceDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    install_default_index(monkeypatch, tmp_path, failure)
    seed(db, "authority-control")
    retriever = CloudTraceRetriever(db)
    with identity_reads_only(db):
        for call in ENTRYPOINTS.values():
            assert call(retriever, "pytest grader") == []


@pytest.mark.parametrize("source_padded", (False, True))
@pytest.mark.parametrize("query_padded", (False, True))
def test_normalized_test_identities_refuse_before_content_reads(
    db: CloudTraceDatabase, tmp_path: Path, source_padded: bool, query_padded: bool,
) -> None:
    def recorded(value: str) -> str:
        return f" {value.upper()} " if source_padded else value

    def queried(value: str) -> str:
        return f" {value.upper()} " if query_padded else value

    root = write_index(tmp_path / "registry")
    keys = root / "research/answer-keys"
    (keys / "tb4-audit-v1/items.jsonl").write_text(json.dumps({
        "item_id": recorded(SEALED_ITEM), "split": "test",
        "subject": {
            "task_id": recorded(f"namespace/{SEALED_NAME}@v1"),
            "task_digest": recorded(SEALED_DIGEST),
        },
    }) + "\n", encoding="utf-8")
    with (keys / "splits/assignments.csv").open("a", encoding="utf-8") as stream:
        for key, item, task_name, split in (
            ("normalized-secondary", "normalized-test-item", "normalized-test-task", "test"),
            ("normalized-conflict", "normalized-dev-item", "normalized-dev-task", "dev"),
            ("normalized-conflict", "normalized-conflict-item", "normalized-other-task", "test"),
        ):
            stream.write(",".join(recorded(value) for value in (key, item, task_name, split)) + "\n")
    (root / "research/experiments/mimo-defect-map-v1/inventory.json").write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [recorded("package-sealed")],
        "tasks": [{
            "record_id": recorded("package-record"),
            "task_id": recorded("namespace/package-sealed@v1"),
            "package_digest": recorded("synthetic-package-digest"),
        }],
    }), encoding="utf-8")
    registry = CorpusRegistry(root)
    rows = (
        ("canonical-item-row", {"metadata_json": {"item_id": queried(SEALED_ITEM)}}),
        ("secondary-item-row", {"metadata_json": {"item_id": queried("normalized-test-item")}}),
        (queried("mirror/normalized-dev-task@v8"), {
            "dataset_id": queried("normalized-conflict"),
            "metadata_json": {"item_id": queried("normalized-conflict-item")},
        }),
        (queried(f"namespace@release/{SEALED_NAME}@v9"), {}),
        (queried(SEALED_ITEM), {}),
        ("original-seal-row", {"original_grader_digest": queried(SEALED_DIGEST)}),
        ("repaired-seal-row", {"repaired_grader_digest": queried(SEALED_DIGEST)}),
        ("package-seal-row", {"original_grader_digest": queried("synthetic-package-digest")}),
        ("known-key-missing-row", {"dataset_id": queried("normalized-conflict")}),
        ("canonical-key-missing-row", {"dataset_id": queried("tb4-audit-v1")}),
    )
    for task_id, extra in rows:
        seed(db, task_id, **extra)
    retriever = CloudTraceRetriever(db, registry=registry)
    with identity_reads_only(db):
        for call in ENTRYPOINTS.values():
            assert call(retriever, "pytest grader") == []


@pytest.fixture
def identity_registry(tmp_path: Path) -> CorpusRegistry:
    root = write_index(tmp_path / "identity-registry")
    with (root / "research/answer-keys/splits/assignments.csv").open(
        "a", encoding="utf-8",
    ) as stream:
        stream.write(" IDENTITY-KEY , DEV-ITEM , Namespace/POSITIVE-DEV@v1 , DEV \n")
    return CorpusRegistry(root)


def identity_row(raw_task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "task_id": raw_task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "original_grader_digest": None, "repaired_grader_digest": None,
        "item_id": None, "item_type": None, **extra,
    }


def test_normalized_dev_keeps_real_provenance_and_both_retrieval_forms(
    db: CloudTraceDatabase, identity_registry: CorpusRegistry,
) -> None:
    task_id = " Namespace/Positive-DEV@v3 "
    dataset_id = " identity-KEY "
    seed(db, task_id, dataset_id=dataset_id, split=" Dev ", metadata_json={"item_id": " DEV-ITEM "})
    retriever = CloudTraceRetriever(db, registry=identity_registry)
    for call in ENTRYPOINTS.values():
        examples = call(retriever, "pytest grader")
        assert ids(examples) == {task_id}
        assert examples[0].dataset_id == dataset_id
        assert examples[0].issue["issue_id"] == task_id + "/issue"
        assert examples[0].provenance["task_id"] == task_id
        assert examples[0].provenance["trace_ids"] == [task_id + "/trace"]
        assert examples[0].provenance["model_id"] == "synthetic-model"
    assert db.get_task(task_id)["metadata_json"] == {"item_id": " DEV-ITEM "}


def test_public_admission_skips_mixed_invalid_rows_without_poisoning_control(
    identity_registry: CorpusRegistry,
) -> None:
    control = identity_row(" Mixed/Raw-Control@v2 ")
    invalid = [
        identity_row("invalid-metadata", item_type="invalid"),
        identity_row("invalid-task-null", task_id=None),
        identity_row("invalid-task-number", task_id=123),
        identity_row("invalid-dataset-null", dataset_id=None),
        identity_row("invalid-dataset-number", dataset_id=123),
        identity_row("invalid-split-null", split=None),
        identity_row("invalid-split-number", split=123),
        identity_row("invalid-split-unknown", split="eval_sealed"),
        identity_row("invalid-split-empty", split=""),
        identity_row("invalid-item-number", item_type="text", item_id=123),
        identity_row("invalid-item-array", item_type="array", item_id=["dev-item"]),
        identity_row("invalid-null-kind", item_type="null", item_id="dev-item"),
        identity_row("invalid-text-kind", item_type="text", item_id=None),
        identity_row("invalid-original-digest", original_grader_digest=123),
        identity_row("invalid-repaired-digest", repaired_grader_digest={}),
    ]
    for key in control:
        row = identity_row("missing-" + key)
        row.pop(key)
        invalid.append(row)
    assert admitted_cloud_task_ids(
        [*invalid, control], registry=identity_registry,
    ) == frozenset({control["task_id"]})


@pytest.mark.parametrize(("item_id", "item_type"), (
    (None, None), (None, "null"), (" DEV-ITEM ", "text"), ("dev-item", "string"),
))
def test_public_admission_accepts_explicit_null_and_backend_text_kinds(
    identity_registry: CorpusRegistry, item_id: str | None, item_type: str | None,
) -> None:
    row = identity_row(
        " Namespace/Positive-DEV@v9 ", dataset_id=" IDENTITY-KEY ",
        item_id=item_id, item_type=item_type,
    )
    assert admitted_cloud_task_ids([row], registry=identity_registry) == frozenset({row["task_id"]})
    assert admitted_cloud_task_ids(
        [row], registry=identity_registry, sealed_task_ids=(" other/POSITIVE-DEV@v2 ",),
    ) == frozenset()


@pytest.mark.parametrize("corruption", ("missing-digest", "invalid-digest", "empty-assignments"))
def test_public_admission_requires_authority_before_consuming_rows(
    identity_registry: CorpusRegistry, corruption: str,
) -> None:
    if corruption == "missing-digest":
        identity_registry.split_assignments_digest = None
    elif corruption == "invalid-digest":
        identity_registry.split_assignments_digest = "sha256:not-a-valid-digest"
    else:
        identity_registry._assignments = {}

    def unread_rows() -> Iterator[dict[str, Any]]:
        pytest.fail("missing assignment authority must not consume the row stream")
        yield identity_row("unread")

    assert admitted_cloud_task_ids(unread_rows(), registry=identity_registry) == frozenset()


def test_public_admission_matches_class_on_readonly_sqlite_metadata_rows(
    db: CloudTraceDatabase, identity_registry: CorpusRegistry, tmp_path: Path,
) -> None:
    raw_id = " Namespace/Positive-DEV@v3 "
    seed(db, raw_id, dataset_id=" IDENTITY-KEY ", metadata_json={"item_id": " DEV-ITEM "})
    seed(db, "invalid-json")
    seed(db, "sealed-metadata", metadata_json={"item_id": " SYNTHETIC-SEALED-ITEM "})
    seed(db, "known-key-unassigned", dataset_id=" identity-key ")
    db._connection.execute(
        "UPDATE corpus_tasks SET metadata_json = ? WHERE task_id = ?", ("{invalid", "invalid-json"),
    )
    source = sqlite3.connect((tmp_path / "evidence.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only = ON")
        rows = source.execute(
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
            "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.item_id') END "
            "AS item_id, CASE WHEN json_valid(metadata_json) "
            "THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type "
            "FROM corpus_tasks ORDER BY task_id",
        ).fetchall()
        admitted = admitted_cloud_task_ids(rows, registry=identity_registry)
        assert isinstance(admitted, frozenset)
        assert admitted == frozenset({raw_id})
        assert source.total_changes == 0
    finally:
        source.close()
    retriever = CloudTraceRetriever(db, registry=identity_registry)
    assert frozenset(retriever._admitted_ids()[0]) == admitted
    for call in ENTRYPOINTS.values():
        assert ids(call(retriever, "pytest grader")) == set(admitted)
