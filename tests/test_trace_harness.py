from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.production import trace_harness as th


def test_finding_validation_rejects_contract_breaks() -> None:
    record = th.finding("r", "d", detector_version="v", reader_model=None, verdict="alarm", kind="cheat")
    assert th.validate_finding(record) == []
    assert th.validate_finding({**record, "verdict": "maybe"})
    assert th.validate_finding({**record, "reader_model": "no-provider"})
    assert th.validate_finding({**record, "evidence_steps": ["5"]})
    assert th.validate_finding({**record, "summary": "w " * 41})
    assert th.validate_finding({**record, "extra": 1})
    with pytest.raises(ValueError):
        th.finding("r", "d", detector_version="v", reader_model=None, verdict="alarm", kind="weird")


def test_summary_is_trimmed_to_forty_words_and_600_characters() -> None:
    record = th.finding("r", "d", detector_version="v", reader_model=None, verdict="clear",
                        kind="cheat", summary="word " * 60)
    assert len(record["summary"].split()) == th.SUMMARY_WORDS
    long_words = th.finding("r", "d", detector_version="v", reader_model=None, verdict="clear",
                            kind="cheat", summary=("x" * 50 + " ") * 30)
    assert len(long_words["summary"]) == th.SUMMARY_CHARS
    assert th.validate_finding({**long_words, "summary": "y" * 601})


def test_store_refuses_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl"
    path.write_text(json.dumps({"run_id": "x"}) + "\n")
    with pytest.raises(ValueError):
        th.FindingStore(path).records()


