"""Staged labels cannot silently become released QA Trace accuracy results."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "score_v7", REPO / "research/experiments/qa-trace/score_v7.py"
)
assert _spec is not None and _spec.loader is not None
score_v7 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_v7)


def _inputs(tmp_path: Path, *, released: bool) -> tuple[Path, Path, Path, Path]:
    out = tmp_path / "predictions"
    out.mkdir()
    subjects, labels = [], []
    for index, (label, real, fp) in enumerate(
        [("wrong_pass", False, True), ("real_pass", True, False), ("wrong_pass", None, None)]
    ):
        subject = {
            "item_id": f"pass-{index}",
            "arm": "base",
            "attempt_index": 1,
            "task": f"task-{index}",
            "source_uid": ["job", str(index)],
        }
        subjects.append(subject)
        labels.append(
            {
                **subject,
                "task_id": subject["task"],
                "population": "official",
                "label": label,
                "excluded_from_primary": False,
            }
        )
        verdict = out / "runs" / score_v7.blind_id(subject["item_id"]) / "verdict.json"
        verdict.parent.mkdir(parents=True)
        verdict.write_text(json.dumps({"reward_real": real, "false_positive": fp}))
    passes = out / "passes.jsonl"
    passes.write_text("".join(json.dumps(s) + "\n" for s in subjects))
    seal = tmp_path / "seal.sha256"
    seal.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(out)}\n"
            for path in sorted(out.rglob("*.json*"))
        )
    )
    key = tmp_path / "labels.json"
    key.write_text(
        json.dumps(
            {
                "schema": "heldout-v7.pass-labels/v1",
                "scoring_allowed": released,
                "labels": labels,
            }
        )
    )
    return key, passes, out, seal


def test_unreleased_key_requires_explicit_staged_opt_in(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, released=False)
    with pytest.raises(SystemExit, match="requires --staged"):
        score_v7.score(*inputs)
    report = score_v7.score(*inputs, staged=True)
    assert report["measurement_status"] == "staged_pre_release"
    view = report["views"]["all official original v6 passes"]["false_positive"]
    assert view["wrong_pass_caught"]["hit"] == 1
    assert view["wrong_pass_caught"]["n"] == 1
    assert view["real_pass_flagged"]["hit"] == 0
    assert view["real_pass_flagged"]["n"] == 1
    assert view["abstained"] == 1


def test_staged_invocation_stays_staged_even_with_released_key(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, released=True)
    assert score_v7.score(*inputs)["measurement_status"] == "released"
    assert score_v7.score(*inputs, staged=True)["measurement_status"] == "staged_pre_release"


def test_staged_scoring_checks_predictions_before_opening_labels(tmp_path: Path) -> None:
    key, passes, out, seal = _inputs(tmp_path, released=False)
    passes.write_text("changed sealed predictions\n")
    key.write_text("not JSON: the seal must be checked first")
    with pytest.raises(SystemExit, match="seal broken, refusing to read labels"):
        score_v7.score(key, passes, out, seal, staged=True)
