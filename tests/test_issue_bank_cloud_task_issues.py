from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import cloud_task_issues as source
from evallab.failure_library.records import Split
from evallab.failure_library.sources import normalize_split

SCHEMA = """
CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
    split TEXT NOT NULL, instruction_text TEXT NOT NULL DEFAULT '',
    original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE task_issues (issue_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
    defect_class TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
    witness_digest TEXT, original_reward DOUBLE PRECISION, repaired_reward DOUBLE PRECISION,
    citation_file TEXT, citation_line INTEGER, reproducer_script TEXT, cas_uri TEXT,
    trust_tier TEXT NOT NULL DEFAULT 'tier3_provisional');
"""


@dataclass
class StubContext(BankContext):
    """A ``BankContext`` whose split authority is a dict of sealed identities."""

    sealed: frozenset[str] = frozenset()

    def strict_split(
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Split:
        split = normalize_split(declared)
        if split != "DEV":
            return split
        if {item_id, task_id, task_digest} & self.sealed:
            return "TEST"
        return "DEV"


def _variants(*identities: tuple[str, str]) -> str:
    return json.dumps(
        {
            "variants": [
                {"admission": {"item_id": item_id, "source_task_digest": digest}}
                for item_id, digest in identities
            ]
        }
    )


@pytest.fixture
def make_db(tmp_path: Path) -> Callable[..., Path]:
    def make(
        tasks: list[tuple[str, str, str, str]], issues: list[dict[str, object]]
    ) -> Path:
        path = tmp_path / "cloud.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO corpus_tasks (task_id, dataset_id, split, metadata_json) VALUES (?,?,?,?)",
            tasks,
        )
        for issue in issues:
            columns = ", ".join(issue)
            marks = ", ".join("?" for _ in issue)
            connection.execute(
                f"INSERT INTO task_issues ({columns}) VALUES ({marks})", list(issue.values())
            )
        connection.commit()
        connection.close()
        return path

    return make


def _issue(issue_id: str, task_id: str = "t1", **fields: object) -> dict[str, object]:
    return {
        "issue_id": issue_id,
        "task_id": task_id,
        "defect_class": "grader_wrong_pass",
        "direction": "wrong_pass",
        "status": "confirmed",
        "trust_tier": "tier1_container_verified",
        **fields,
    }


def _build(
    db: Path, tmp_path: Path, sealed: frozenset[str] = frozenset()
) -> dict[str, IssueItem]:
    ctx = StubContext(repo_root=tmp_path, cloud_db=db, sealed=sealed)
    items = list(source.build(ctx))
    assert all(isinstance(item, IssueItem) for item in items)
    return {item.provenance.ref: item for item in items if isinstance(item, IssueItem)}


def test_tier_status_direction_mapping(make_db: Callable[..., Path], tmp_path: Path) -> None:
    db = make_db(
        [("t1", "tb4-audit-v1", "DEV", "{}")],
        [
            _issue("i-t1"),
            _issue("i-t2", trust_tier="tier2_consensus_labeled"),
            _issue("i-t3c", trust_tier="tier3_provisional"),
            _issue(
                "i-prov",
                trust_tier="tier3_provisional",
                status="provisional",
                direction="unfair_fail",
                defect_class="grader_unfair_fail",
            ),
            _issue("i-ref", trust_tier="tier3_provisional", status="refuted"),
        ],
    )
    items = _build(db, tmp_path)

    assert items["i-t1"].label_type == "executed"
    assert items["i-t1"].label == "tier1_container_verified:confirmed:grader_wrong_pass"
    assert items["i-t2"].label_type == "consensus"
    assert items["i-t3c"].label_type == "judge"
    assert items["i-prov"].label_type == "judge"
    assert "provisional" in items["i-prov"].label
    assert (items["i-prov"].kind, items["i-prov"].polarity) == ("grader_unfair_fail", "positive")
    assert (items["i-t1"].kind, items["i-t1"].polarity) == ("grader_wrong_pass", "positive")
    # A refuted claim is a real negative, not an item with the issue present.
    assert (items["i-ref"].kind, items["i-ref"].polarity) == ("grader_wrong_pass", "negative")
    assert len({item.item_id for item in items.values()}) == 5
    for item in items.values():
        assert (item.subject_type, item.subject_id, item.task_id) == ("task", "t1", "t1")
        assert item.actor_transcript is None
        assert item.harness_selector is None
        assert item.provenance.store == "cloud-trace-db:task_issues"
        assert item.locator == item.provenance.ref


def test_two_issues_on_one_task_stay_distinct(
    make_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = make_db(
        [("t1", "tb4-audit-v1", "DEV", "{}")],
        [_issue("a", defect_class="x"), _issue("b", defect_class="y")],
    )
    assert set(_build(db, tmp_path)) == {"a", "b"}


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        (None, "task"),
        ("/repo/tasks/cad-model/instruction.md", "task"),
        ("/repo/packages/tb2/x/tests/test_outputs.py", "test"),
        ("/repo/tasks/x/tests/grader/param_check.py", "test"),
        ("/repo/tasks/x/tests/test.sh", "test"),
        ("/repo/tasks/x/tests/legacy_app/README.md", "task"),
        ("/repo/tasks/x/environment/layout_spec.json", "task"),
        ("/repo/research/answer-keys/hacktrace-act-v1/items.jsonl", "task"),
    ],
)
def test_granularity_follows_citation(citation: str | None, expected: str) -> None:
    assert source.citation_granularity(citation) == expected


def test_note_carries_presence_not_script_body(
    make_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = make_db(
        [("t1", "tb4-audit-v1", "DEV", "{}")],
        [
            _issue("with", reproducer_script="#!/bin/bash\nSECRET_BODY=1", witness_digest="sha256:ab"),
            _issue("without"),
        ],
    )
    items = _build(db, tmp_path)

    assert "reproducer present" in (items["with"].provenance.note or "")
    assert "witness_digest present" in (items["with"].provenance.note or "")
    assert "no reproducer" in (items["without"].provenance.note or "")
    assert "no witness_digest" in (items["without"].provenance.note or "")
    assert all("SECRET_BODY" not in item.model_dump_json() for item in items.values())


def test_sealed_variant_identity_is_never_emitted_as_dev(
    make_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = make_db(
        [
            ("clean", "mimo-defect-map-v1", "DEV", _variants(("clean@sha256:aa", "sha256:aa"))),
            # Declared DEV, but one admitted variant's package digest is TEST-sealed.
            (
                "tight",
                "mimo-defect-map-v1",
                "DEV",
                _variants(("tight@sha256:bb", "sha256:bb"), ("tight@sha256:cc", "sha256:cc")),
            ),
            ("named", "tb4-audit-v1", "DEV", "{}"),
        ],
        [_issue("i-clean", "clean"), _issue("i-tight", "tight"), _issue("i-named", "named")],
    )
    items = _build(db, tmp_path, sealed=frozenset({"sha256:cc", "named"}))

    assert items["i-clean"].split == "DEV"
    assert items["i-tight"].split == "TEST"
    assert items["i-tight"].declared_split == "DEV"
    assert items["i-named"].split == "TEST"


def test_held_declaration_is_kept_as_heldout(make_db: Callable[..., Path], tmp_path: Path) -> None:
    db = make_db([("h", "SimGlot", "held", "{}")], [_issue("i-h", "h")])
    items = _build(db, tmp_path)

    assert items["i-h"].declared_split == "heldout"
    assert items["i-h"].split == "heldout"


def test_task_missing_from_corpus_is_never_dev(
    make_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = make_db([], [_issue("orphan", "ghost")])
    items = _build(db, tmp_path)

    assert items["orphan"].declared_split == "unassigned"
    assert items["orphan"].split == "unassigned"


def test_cloud_db_is_opened_read_only(make_db: Callable[..., Path], tmp_path: Path) -> None:
    db = make_db([("t1", "tb4-audit-v1", "DEV", "{}")], [_issue("a")])
    before = db.read_bytes()
    _build(db, tmp_path)

    assert db.read_bytes() == before
