"""Offline consumer contracts: split locks, subject groups, citations and native packets."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.failure_library.knowledge.compiler import compile_library, validate_card
from evallab.failure_library.knowledge.export import (
    hackbook_proposals,
    write_agent_pack,
    write_knowledge,
)
from evallab.failure_library.knowledge.models import Agent, CardDraft
from evallab.failure_library.knowledge.packs import make_pack
from evallab.failure_library.records import (
    Evidence,
    Finding,
    RunLabel,
    RunRecord,
    TaskRef,
    make_finding_id,
)
from evallab.failure_library.store import LibraryStore
from evallab.hackbook.models import HackbookEntry, load_hackbook
from evallab.qa.task import load_rubric

REPO = Path(__file__).resolve().parents[1]
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _finding(task: str, *, split: str = "DEV", summary: str | None = None) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": make_finding_id("fixture-source", task, REVISION, "TS01", "item-" + task),
            "collection": "fixture-key",
            "locator": "item-" + task,
            "task": TaskRef(
                source_id="fixture-source",
                task_id=task,
                upstream_repo="https://example.org/task-repo",
                upstream_revision=REVISION,
            ),
            "defect_class": "TS01",
            "direction": "wrong_pass",
            "summary": summary
            or "Only one fixed input is tested; literal answers satisfy the check.",
            "evidence": [
                Evidence(
                    kind="human_label",
                    label_source="offline published audit fixture",
                    note="Published report; not locally executed.",
                )
            ],
            "tier": "V1_external_label",
            "finder": "fixture auditor",
            "source_url": "https://example.org/audit/" + task,
            "observed_at": date(2026, 10, 27),
            "split": split,
        }
    )


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, LibraryStore, list[Finding]]:
    keys = tmp_path / "research/answer-keys"
    (keys / "splits").mkdir(parents=True)
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        (keys / dataset).mkdir()
        (keys / dataset / "items.jsonl").write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    tasks = [
        "subject",
        "sibling",
        "algebra",
        "geometry",
        "sorting",
        "formatting",
        "test-row",
        "heldout-row",
        "cross-row",
        "laundered",
        "permanent-seal",
    ]
    with (keys / "splits/assignments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["key_id", "item_id", "task", "unit", "split"])
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "key_id": "fixture-key",
                    "item_id": "item-" + task,
                    "task": task,
                    "unit": "family-and-template-a" if task in {"subject", "sibling"} else task,
                    "split": "test" if task == "laundered" else "dev",
                }
            )
    inventory = tmp_path / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "tasks": [],
                "heldout_task_ids_excluded_from_training_all_versions": ["permanent-seal"],
            }
        )
    )
    knowledge_dir = tmp_path / "research/failure-library/knowledge"
    knowledge_dir.mkdir(parents=True)
    knowledge_dir.joinpath("mechanisms.json").write_bytes(
        (REPO / "research/failure-library/knowledge/mechanisms.json").read_bytes()
    )
    hackbook = tmp_path / "research/hackbook"
    hackbook.mkdir()
    hackbook.joinpath("entries.jsonl").write_bytes(
        (REPO / "research/hackbook/entries.jsonl").read_bytes()
    )
    records = [
        _finding(
            task,
            split={"test-row": "TEST", "heldout-row": "heldout", "cross-row": "cross_test"}.get(
                task, "DEV"
            ),
        )
        for task in tasks
    ]
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    store.write_findings("fixture-key", records)
    return tmp_path, store, records


def test_non_dev_and_cross_key_laundering_never_reach_any_pack(library) -> None:
    root, store, records = library
    knowledge = compile_library(root, store)
    rejected = {
        record.finding_id
        for record in records
        if record.task.task_id
        in {
            "test-row",
            "heldout-row",
            "cross-row",
            "laundered",
            "permanent-seal",
        }
    }
    assert knowledge.report["by_split"] == {"DEV": 6}
    for agent in ("task-qa", "trace-qa", "seeker"):
        pack = make_pack(knowledge, agent=agent, top_k=100)
        assert not set(pack.example_ids) & rejected
        assert all(identifier not in pack.markdown for identifier in rejected)
        assert pack.status == "ready"
    assert all(ref.id not in rejected for card in knowledge.cards for ref in card.examples)


def test_leave_subject_out_excludes_task_family_and_template(library) -> None:
    root, store, records = library
    knowledge = compile_library(root, store)
    pack = make_pack(knowledge, agent="task-qa", task="fixture-source/subject", top_k=100)
    excluded = {
        record.finding_id for record in records if record.task.task_id in {"subject", "sibling"}
    }
    assert pack.status == "ready"
    assert len(pack.example_ids) == 4
    assert not excluded & set(pack.example_ids)
    assert "fixture-source/subject" not in pack.markdown
    assert "fixture-source/sibling" not in pack.markdown


def test_explicit_frozen_canary_ids_exclude_linked_runs_and_group(library) -> None:
    root, store, records = library
    subject = records[0]
    run = RunRecord(
        run_id="frozen-run",
        collection="fixture-runs",
        task=subject.task,
        outcome="pass",
        labels=[RunLabel(label="reported shortcut", kind="human", source="fixture")],
        transcript_ref="https://example.org/run",
        split="DEV",
        finding_ids=[subject.finding_id],
    )
    store.write_runs("fixture-runs", [run], source_revision=REVISION)
    knowledge = compile_library(root, store)
    assert "frozen-run" in {example.ref.id for example in knowledge.examples}
    pack = make_pack(knowledge, agent="trace-qa", exclude_ids=[subject.finding_id], top_k=100)
    assert "frozen-run" not in pack.example_ids
    assert subject.finding_id not in pack.example_ids
    assert records[1].finding_id not in pack.example_ids


def test_missing_group_authority_fails_closed_for_same_source(library) -> None:
    root, store, _ = library
    knowledge = compile_library(root, store)
    pack = make_pack(knowledge, agent="seeker", task="fixture-source/unknown-subject")
    assert pack.status == "empty"
    assert not pack.example_ids and not pack.card_ids


def test_protected_name_embedded_in_dev_summary_is_not_returned(library) -> None:
    root, store, _ = library
    extra = _finding("innocent", summary="A summary quotes permanent-seal and its answer.")
    extra = extra.model_copy(update={"collection": "external-fixture"})
    clean = _finding("innocent-safe").model_copy(update={"collection": "external-fixture"})
    store.write_findings("external-fixture", [extra, clean])
    knowledge = compile_library(root, store)
    admitted = {example.ref.id for example in knowledge.examples}
    assert extra.finding_id not in admitted
    assert clean.finding_id in admitted


def test_cards_are_real_dev_citations_and_model_citation_fabrication_is_refused(library) -> None:
    root, store, _ = library
    knowledge = compile_library(root, store)
    assert knowledge.cards
    admitted = {example.ref.id: example.ref for example in knowledge.examples}
    for card in knowledge.cards:
        assert card.schema_version == "failure-card/v1"
        assert 3 <= len(card.examples) <= 10
        validate_card(card, admitted)
        assert card.examples[0].evidence_notes == ["Published report; not locally executed."]
    card = knowledge.cards[0]
    changed = card.model_copy(
        update={
            "examples": [
                card.examples[0].model_copy(update={"id": "invented-id"}),
                *card.examples[1:],
            ]
        }
    )
    with pytest.raises(ValueError, match="admitted DEV"):
        validate_card(changed, admitted)


def test_rebuild_and_bm25_are_deterministic_and_relevant(library, tmp_path: Path) -> None:
    root, store, records = library
    special = _finding(
        "algebra", summary="Tensor determinant comparison checks only a fixed input fixture."
    )
    store.write_findings(
        "fixture-key", [special if r.task.task_id == "algebra" else r for r in reversed(records)]
    )
    first = compile_library(root, store)
    second = compile_library(root, store)
    assert first.digest == second.digest
    assert [c.model_dump() for c in first.cards] == [c.model_dump() for c in second.cards]
    pack = make_pack(first, agent="task-qa", task_text="tensor determinant", top_k=1)
    assert pack.example_ids == [special.finding_id]
    write_knowledge(first, tmp_path / "built-a")
    write_knowledge(second, tmp_path / "built-b")
    for name in ("cards.jsonl", "hackbook-proposals.jsonl", "build.json"):
        assert (tmp_path / "built-a" / name).read_bytes() == (
            tmp_path / "built-b" / name
        ).read_bytes()
    for entry in hackbook_proposals(first):
        HackbookEntry.model_validate(entry.dump())
        assert entry.id in {e.id for e in load_hackbook(root)}


@pytest.mark.parametrize("agent", ["task-qa", "trace-qa", "seeker"])
def test_native_agent_packets_match_existing_extension_shapes(
    library, tmp_path: Path, agent: Agent
) -> None:
    root, store, _ = library
    knowledge = compile_library(root, store)
    pack = make_pack(knowledge, agent=agent, task="fixture-source/subject", max_chars=4_000)
    assert len(pack.markdown) <= 4_000
    directory = tmp_path / agent
    criteria = [
        {
            "name": "functional_verification",
            "description": "Actual behavior",
            "guidance": "Check behavior",
        },
        {"name": "verifiable", "description": "Reliable grader", "guidance": "Check reliability"},
    ]
    manifest = write_agent_pack(pack, directory, rubric_criteria=criteria)
    assert manifest["status"] == "ready"
    assert (directory / "pack.md").read_text() == pack.markdown
    if agent == "task-qa":
        loaded = load_rubric(directory / "rubric.toml")
        assert [c["name"] for c in loaded] == [c["name"] for c in criteria]
        assert any(pack.markdown in c["guidance"] for c in loaded)
    elif agent == "trace-qa":
        prior = json.loads((directory / "task_priors.json").read_text())
        assert prior["task_id"] == "fixture-source/subject"
        assert prior["priors"][0]["matched"] is False
        assert prior["priors"][0]["record"]["kind"] == "dev_analogy"
    else:
        assert (directory / "hackbook.md").read_text() == pack.markdown
    with pytest.raises(FileExistsError):
        write_agent_pack(pack, directory, rubric_criteria=criteria)


def test_char_cap_never_clips_citations_and_invalid_budgets_fail(library) -> None:
    root, store, _ = library
    knowledge = compile_library(root, store)
    for limit in (256, 500, 1_000):
        pack = make_pack(knowledge, agent="seeker", max_chars=limit)
        assert len(pack.markdown) <= limit
        for identifier in pack.example_ids:
            assert identifier in pack.markdown
    with pytest.raises(ValueError, match="max_chars"):
        make_pack(knowledge, agent="task-qa", max_chars=1)


def test_pack_cli_emits_markdown_and_known_native_files(library, tmp_path: Path, capsys) -> None:
    root, _, _ = library
    output = tmp_path / "packet"
    code = run_cli(
        [
            "failures",
            "pack",
            "--for",
            "trace-qa",
            "--task",
            "fixture-source/subject",
            "--state-root",
            str(tmp_path / "state"),
            "--output-dir",
            str(output),
            "--max-chars",
            "2000",
        ],
        workspace=root,
    )
    assert code == 0
    assert capsys.readouterr().out.startswith("# Failure Library")
    assert json.loads((output / "pack.manifest.json").read_text())["status"] == "ready"


def test_missing_split_authority_or_unknown_draft_is_refused(library) -> None:
    root, store, _ = library
    draft = CardDraft(
        hackbook_id="HB-TS01-999",
        what_breaks="Mismatch",
        task_cues=["cue"],
        trace_cues=["cue"],
        seeker_hypothesis="Mismatch",
        fixes=["fix"],
        counterexamples=["control"],
    )
    with pytest.raises(ValueError, match="existing Hackbook"):
        compile_library(root, store, drafts=[draft])
    (root / "research/answer-keys/splits/assignments.csv").unlink()
    with pytest.raises(ValueError, match="split assignment authority"):
        compile_library(root, store)


def test_mislabeled_cross_test_sources_and_non_dev_runs_are_refused(library) -> None:
    root, store, records = library
    runs = [
        RunRecord(
            run_id=f"forbidden-{split}",
            collection="fixture-runs",
            task=records[0].task,
            outcome="pass",
            labels=[],
            transcript_ref="https://example.org/run",
            split=split,
        )
        for split in ("TEST", "heldout", "cross_test")
    ]
    forged = RunRecord(
        run_id="forged-cheatbench-dev",
        collection="cheatbench-ablation",
        task=records[0].task.model_copy(update={"source_id": "cheatbench"}),
        outcome="pass",
        transcript_ref="https://example.org/cross-test",
        split="DEV",
    )
    store.write_runs("fixture-runs", runs, source_revision=REVISION)
    store.write_runs("cheatbench-ablation", [forged], source_revision=REVISION)
    knowledge = compile_library(root, store)
    assert not {r.run_id for r in [*runs, forged]} & {e.ref.id for e in knowledge.examples}


def test_run_cannot_join_non_dev_finding_knowledge(library) -> None:
    root, store, records = library
    non_dev = next(record for record in records if record.split == "TEST")
    run = RunRecord(
        run_id="bad-join",
        collection="fixture-runs",
        task=records[0].task,
        outcome="fail",
        transcript_ref="https://example.org/run",
        split="DEV",
        finding_ids=[non_dev.finding_id],
    )
    store.write_runs("fixture-runs", [run], source_revision=REVISION)
    knowledge = compile_library(root, store)
    assert run.run_id not in {e.ref.id for e in knowledge.examples}


def test_run_labels_build_behavior_cards_without_inventing_task_defects_or_replays(library) -> None:
    root, store, records = library
    runs = [
        RunRecord(
            run_id=f"behavior-{index}",
            collection="fixture-runs",
            task=record.task,
            outcome="pass",
            labels=[
                RunLabel(label="verifier_pass", kind="executed", source="fixture grader receipt"),
                RunLabel(
                    label="test infrastructure modification",
                    rs_code="RS02",
                    kind="judge",
                    source="fixture behavior judge",
                ),
            ],
            transcript_ref=f"https://example.org/unread-transcript/{index}",
            split="DEV",
        )
        for index, record in enumerate(records[:3])
    ]
    store.write_runs("fixture-runs", runs, source_revision=REVISION)
    knowledge = compile_library(root, store)
    behavior_cards = [card for card in knowledge.cards if card.taxonomy_class_id == "RS02"]
    assert behavior_cards
    for card in behavior_cards:
        assert card.defect_class == "unclassified" and card.run_code == "RS02"
        assert all(ref.kind == "run" and ref.tier is None for ref in card.examples)
        assert all("[judge, RS02]" in ref.summary for ref in card.examples)
    run_examples = [example for example in knowledge.examples if example.ref.kind == "run"]
    assert all(example.defect_classes == ("RS02",) for example in run_examples)
    pack = make_pack(knowledge, agent="trace-qa", task_text="test infrastructure modification")
    assert set(pack.example_ids) & {run.run_id for run in runs}
    assert "not replay-confirmed" in pack.markdown


def test_unmapped_cross_publisher_identity_cannot_bypass_subject_exclusion(library) -> None:
    root, store, _ = library
    runs = [
        RunRecord(
            run_id=f"opaque-{index}",
            collection="opaque-runs",
            task=TaskRef(
                source_id="opaque-source",
                task_id=str(100 + index),
                upstream_repo="https://example.org/repackaged",
                upstream_revision=REVISION,
            ),
            outcome="pass",
            labels=[
                RunLabel(
                    label="test infrastructure modification",
                    rs_code="RS02",
                    kind="judge",
                    source="publisher",
                )
            ],
            transcript_ref=f"https://example.org/opaque/{index}",
            split="DEV",
        )
        for index in range(3)
    ]
    store.write_runs("opaque-runs", runs, source_revision=REVISION)
    knowledge = compile_library(root, store)
    global_pack = make_pack(knowledge, agent="trace-qa", top_k=100)
    assert set(global_pack.example_ids) & {run.run_id for run in runs}
    named = make_pack(knowledge, agent="trace-qa", task="fixture-source/subject", top_k=100)
    assert not set(named.example_ids) & {run.run_id for run in runs}
    opaque_subject = make_pack(knowledge, agent="trace-qa", task="opaque-source/100")
    assert opaque_subject.status == "empty"
    with pytest.raises(ValueError, match="admitted DEV subject identities"):
        make_pack(knowledge, agent="trace-qa", exclude_ids=["missing-frozen-id"])
