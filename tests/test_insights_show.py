from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights.commands.show import _cmd
from evallab.insights.models import ClaimRecord
from evallab.insights.store import InsightStore

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _record(recompute: dict[str, object] | None) -> ClaimRecord:
    return ClaimRecord.model_validate({
        "claim_id": "C-show", "source_id": "S-1", "section_sha256": "sha", "fingerprint": "FP",
        "pass_id": "P-1", "extracted_by": "synthetic", "created_at": NOW,
        "claim": {"local_id": "c1", "experiment": "B3", "statement": "B3 flips 18 of 28 packages",
                  "kind": "measurement", "k": 18, "n": 28, "quote": "18 of 28 flipped",
                  "section_id": "sec-1"},
        "check": {"status": "pass"}, "recompute": recompute,
    })


def test_show_names_what_a_partial_recompute_validated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    partial = {"status": "partial", "validated": ["k"], "unchecked": ["n"], "detail": "Partial."}
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_record(partial)])
    args = argparse.Namespace(record_id="C-show", state_dir=tmp_path)
    assert _cmd(args, tmp_path) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["recompute_summary"] == "k matches receipt; n not checked"
    assert shown["claim"]["recompute"]["status"] == "partial"
    # A row written before `partial` existed still loads and is described, not rewritten.
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_record({"status": "reproduced", "detail": "Receipt gives 18."})
                             .model_copy(update={"claim_id": "C-old"})])
    args = argparse.Namespace(record_id="C-old", state_dir=tmp_path)
    assert _cmd(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["recompute_summary"] == "matches receipt"
