"""Isolated annotation fixtures; their licence is not a claim about the real release."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from evallab.failure_library.bank import BankContext
from evallab.failure_library.bank.sources import clean_controls
from evallab.failure_library.sources.swe_family import SWE_ORIGINAL_REVISION

_FIELDS = (
    "instance_id",
    "user_id",
    "underspecified",
    "false_negative",
    "other_major_issues",
)



def _context(
    tmp_path: Path,
    *,
    assigned_dev: Sequence[str] = (),
    sealed_tasks: Sequence[str] = (),
) -> BankContext:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    with assignments.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("key_id", "item_id", "task", "unit", "split", "expected_verdict", "exposures")
        )
        for index, task in enumerate(("unrelated-authority", *assigned_dev)):
            writer.writerow(("fixture", str(index), task, "unit", "dev", "pass", ""))
    for relative in (
        "tb4-audit-v1/items.jsonl",
        "tb4-audit-v1/tasks.jsonl",
        "cheatbench-actions-v1/items.jsonl",
        "hacktrace-act-v1/items.jsonl",
    ):
        path = keys / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": list(sealed_tasks),
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    return BankContext(
        repo_root=root,
        state_root=tmp_path / "state",
        cloud_db=tmp_path / "unused.sqlite",
        prod_live=tmp_path / "prod-live",
        cross_test_registry=tmp_path / "cross-test-registry.json",
    )


def _annotations(
    task_id: str, **scores: tuple[str, str, str]
) -> list[dict[str, str]]:
    return [
        {
            "instance_id": task_id,
            "user_id": str(index + 1),
            **{
                criterion: scores.get(criterion, ("0.0", "0.0", "0.0"))[index]
                for criterion in _FIELDS[2:]
            },
        }
        for index in range(3)
    ]


def _seed(
    ctx: BankContext,
    monkeypatch: pytest.MonkeyPatch,
    rows: Sequence[dict[str, str]],
) -> str:
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("samples_with_3_annotations_public.csv", text.getvalue())
    raw = data.getvalue()
    path = (
        ctx.state_root
        / "raw/openai-swebench-verified-annotations/2024-08-12"
        / "swe-bench-annotation-results.zip"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(clean_controls, "OPENAI_SHA", digest)
    return digest


def test_negatives_are_criterion_scoped_not_universally_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, assigned_dev=("clean", "criterion-only"))
    digest = _seed(
        ctx,
        monkeypatch,
        [
            *_annotations("clean"),
            *_annotations(
                "criterion-only",
                underspecified=("0.0", "1.0", "0.0"),
                other_major_issues=("0.0", "1.0", "0.0"),
            ),
        ],
    )
    items = list(clean_controls.build(ctx))
    by_criterion = {(item.task_id, item.locator.rsplit("/", 1)[-1]): item for item in items}
    assert set(by_criterion) == {
        ("clean", "underspecified"),
        ("clean", "false_negative"),
        ("clean", "other_major_issues"),
        ("criterion-only", "false_negative"),
    }
    assert by_criterion["clean", "underspecified"].kind == "grader_unfair_fail"
    assert by_criterion["clean", "false_negative"].kind == "grader_unfair_fail"
    assert by_criterion["clean", "other_major_issues"].kind == "task_defect"
    assert len({item.item_id for item in items}) == 4
    for (task_id, criterion), item in by_criterion.items():
        assert (item.polarity, item.label_type) == ("negative", "consensus")
        assert (item.granularity, item.subject_type) == ("task", "task")
        assert item.subject_id == f"swe-bench/{task_id}@{SWE_ORIGINAL_REVISION}"
        assert item.label == f"{criterion}=0; 3/3 human raters"
        assert item.license is None  # Never inherit the task dataset's licence.
        assert item.actor_transcript is None and item.harness_selector is None
        assert item.provenance.store == (
            "failure-library:raw/openai-swebench-verified-annotations/2024-08-12"
        )
        assert item.provenance.ref == item.locator
        assert item.provenance.digest == digest
        assert "criterion-scoped negative only" in (item.provenance.note or "")
        assert "audited task-package bytes are unpinned" in (item.provenance.note or "")


def test_incomplete_or_ambiguous_human_consensus_is_not_a_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    missing_rater = _annotations("missing-rater")
    missing_rater[2]["user_id"] = ""
    duplicate_rater = _annotations("duplicate-rater")
    duplicate_rater[2]["user_id"] = duplicate_rater[0]["user_id"]
    extra_rater = _annotations("extra-rater")
    extra_rater.append({**extra_rater[0], "user_id": "4"})
    _seed(
        ctx,
        monkeypatch,
        [
            *missing_rater,
            *duplicate_rater,
            *extra_rater,
            *_annotations("two-raters")[:2],
            *_annotations("missing-score", underspecified=("0.0", "", "0.0")),
            *_annotations(
                "severity-one",
                underspecified=("0.0", "1.0", "0.0"),
                false_negative=("1.0", "1.0", "1.0"),
                other_major_issues=("1.0", "1.0", "1.0"),
            ),
        ],
    )
    items = list(clean_controls.build(ctx))
    assert {(item.task_id, item.locator.rsplit("/", 1)[-1]) for item in items} == {
        ("missing-score", "false_negative"),
        ("missing-score", "other_major_issues"),
    }


def test_canonical_task_seals_tighten_declared_dev_negatives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, assigned_dev=("sealed", "open"), sealed_tasks=("sealed",))
    _seed(ctx, monkeypatch, [*_annotations("sealed"), *_annotations("open")])
    items = list(clean_controls.build(ctx))
    assert {(item.declared_split, item.split) for item in items if item.task_id == "sealed"} == {
        ("DEV", "TEST")
    }
    assert {(item.declared_split, item.split) for item in items if item.task_id == "open"} == {
        ("DEV", "DEV")
    }


@pytest.mark.parametrize("score", ["4.0", "-1.0", "nan"])
def test_unknown_source_severity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score: str
) -> None:
    ctx = _context(tmp_path)
    _seed(ctx, monkeypatch, _annotations("invalid", underspecified=("0.0", score, "0.0")))
    with pytest.raises(ValueError, match="Unknown OpenAI underspecified score"):
        list(clean_controls.build(ctx))


def test_wrong_pinned_archive_digest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    _seed(ctx, monkeypatch, _annotations("clean"))
    monkeypatch.setattr(clean_controls, "OPENAI_SHA", "0" * 64)
    with pytest.raises(ValueError, match="Source SHA-256 mismatch"):
        list(clean_controls.build(ctx))




def test_missing_raw_cache_never_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_download(*args: object, **kwargs: object) -> None:
        pytest.fail("clean-controls must not download source bytes")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_download)
    with pytest.raises(FileNotFoundError, match="Pinned source missing"):
        list(clean_controls.build(_context(tmp_path)))
