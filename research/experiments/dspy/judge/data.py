"""Calibration corpus -> ``dspy.Example`` sets, reusing the Lab's frozen split."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import dspy

from evallab.calibrate import (
    DspyExample,
    calibration_root,
    load_dspy_examples,
    split_dspy_examples,
)


@dataclass(frozen=True)
class FamilySets:
    family: str
    all: list[dspy.Example]
    train: list[dspy.Example]
    val: list[dspy.Example]
    heldout: list[dspy.Example]


def load_key_rationales(repo_root: Path, family: str, document_id: str) -> dict[str, dict[str, str]]:
    """Return dimension -> criterion -> one-line gold rationale from the sealed key."""
    key_path = calibration_root(repo_root) / family / "answer-keys" / f"{document_id}.json"
    key = json.loads(key_path.read_text(encoding="utf-8"))
    return {
        dimension: {name: cell["rationale"] for name, cell in block.items()}
        for dimension, block in key["criteria"].items()
    }


def to_dspy_example(repo_root: Path, item: DspyExample) -> dspy.Example:
    return dspy.Example(
        document_id=item.document_id,
        family=item.family,
        rubric_json=item.rubric_json,
        document=item.document,
        expected_json=item.expected_json,
        rationales_json=json.dumps(
            load_key_rationales(repo_root, item.family, item.document_id), sort_keys=True
        ),
    ).with_inputs("family", "rubric_json", "document")


def family_sets(repo_root: Path, family: str) -> FamilySets:
    items = load_dspy_examples(repo_root, family)
    split = split_dspy_examples(items)
    convert = lambda seq: [to_dspy_example(repo_root, item) for item in seq]  # noqa: E731
    return FamilySets(
        family=family,
        all=convert(items),
        train=convert(split.train),
        val=convert(split.optimizer_validation),
        heldout=convert(split.heldout),
    )
