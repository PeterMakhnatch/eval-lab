"""Behavioral regressions for the training-only overnight GEPA gate."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/ovn-sft-v0/gepa_gate.py"
_spec = importlib.util.spec_from_file_location("overnight_gepa_gate", SCRIPT)
assert _spec is not None and _spec.loader is not None
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    proposal, held_out = tmp_path / "proposal.csv", tmp_path / "held_out.csv"
    _csv(proposal, ["task_id", "run_digest", "project"], [
        {"task_id": f"train-{i}", "run_digest": "sha256:" + f"{i:064x}", "project": f"org/project-{i}"}
        for i in range(12)
    ])
    _csv(held_out, ["task", "repo"], [{"task": "eval-0", "repo": "org/held-out"}])
    return proposal, held_out


@pytest.fixture
def manifest(sources: tuple[Path, Path]) -> dict[str, Any]:
    return _gate.freeze_minibatch(*sources)


def _rows(manifest: dict[str, Any], passes: int = 3, tokens: int | None = 100) -> list[dict[str, Any]]:
    return [
        {
            "task_id": task["task_id"], "task_package_digest": task["task_package_digest"],
            "trial_id": f"native-{task['task_id']}", "repeat": 1,
            "counts": {"schema": "evallab.counts/v1", "verdict": "counted_pass" if i < passes else "counted_fail", "raw_reward": float(i < passes), "scored": True, "reasons": []},
            "usage": {"input_tokens": tokens, "output_tokens": tokens},
            "receipt_paths": {"counts": f"runs/{task['task_id']}/processed/trial.json"},
        }
        for i, task in enumerate(manifest["tasks"])
    ]


def _candidate(manifest: dict[str, Any], *, passes: int = 3, tokens: int | None = 100, candidate_id: str | None = None, frozen_at: str = "2026-10-01T08:00:00Z") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id or "sha256:" + "a" * 64,
        "candidate_path": "runs/frozen-instructions.txt", "frozen_at": frozen_at,
        "rows": _rows(manifest, passes, tokens),
    }


def _exclude(row: dict[str, Any]) -> None:
    row["counts"].update(verdict="excluded", reasons=["pass_tainted"])


def test_freeze_preserves_proposal_order_and_binds_source_bytes(sources: tuple[Path, Path]) -> None:
    proposal, held_out = sources
    frozen = _gate.freeze_minibatch(proposal, held_out)
    assert [task["task_id"] for task in frozen["tasks"]] == [f"train-{i}" for i in range(10)]
    before = frozen["proposal_sha256"]
    proposal.write_bytes(proposal.read_bytes() + b"\n")
    changed = _gate.freeze_minibatch(proposal, held_out)
    assert changed["tasks"] == frozen["tasks"]
    assert changed["proposal_sha256"] != before
    assert frozen["eval_tasks_sha256"] == "sha256:" + hashlib.sha256(held_out.read_bytes()).hexdigest()


@pytest.mark.parametrize("task_id,project", [("eval-0", "org/another"), ("train-0", "other/HELD_OUT/")])
def test_freeze_refuses_held_out_task_or_normalized_repository(sources: tuple[Path, Path], task_id: str, project: str) -> None:
    proposal, held_out = sources
    rows = list(csv.DictReader(proposal.open()))
    rows[0].update(task_id=task_id, project=project)
    _csv(proposal, list(rows[0]), rows)
    with pytest.raises(_gate.GateError) as refusal:
        _gate.freeze_minibatch(proposal, held_out)
    assert refusal.value.decision == "held_out_collision"


@pytest.mark.parametrize("passes,tokens,promoted,decision", [
    (4, None, True, "counted_improvement"),
    (3, 90, True, "tokens_improvement"),
    (3, 100, False, "no_candidate_beats_seed"),
    (3, None, False, "unknown_tokens"),
    (2, 1, False, "no_candidate_beats_seed"),
])
def test_only_strict_counted_or_known_token_improvement_promotes(manifest: dict[str, Any], passes: int, tokens: int | None, promoted: bool, decision: str) -> None:
    candidate = _candidate(manifest, passes=passes, tokens=tokens)
    result = _gate.select_candidate(manifest, _rows(manifest), [candidate])
    assert result["promoted"] is promoted
    assert result["winner"] == (candidate["candidate_id"] if promoted else "seed")
    assert result["decision"] == decision


@pytest.mark.parametrize("side", ["seed", "candidate"])
def test_excluded_passes_cannot_establish_paired_improvement(manifest: dict[str, Any], side: str) -> None:
    seed, candidate = _rows(manifest), _candidate(manifest, passes=10)
    _exclude(seed[0] if side == "seed" else candidate["rows"][0])
    result = _gate.select_candidate(manifest, seed, [candidate])
    assert result["promoted"] is False
    assert result["decision"] == "incomplete_countable_coverage"
    summary = result["seed"] if side == "seed" else result["candidates"][0]
    assert summary["excluded"] == [{"task_id": "train-0", "reasons": ["pass_tainted"]}]
    assert summary["counted_fails"] == (7 if side == "seed" else 0)


@pytest.mark.parametrize("side", ["seed", "candidate"])
def test_missing_evidence_never_shrinks_the_comparison_cohort(manifest: dict[str, Any], side: str) -> None:
    seed, candidate = _rows(manifest), _candidate(manifest, passes=10)
    (seed if side == "seed" else candidate["rows"]).pop()
    result = _gate.select_candidate(manifest, seed, [candidate])
    assert result["promoted"] is False
    summary = result["seed"] if side == "seed" else result["candidates"][0]
    assert summary["missing_tasks"] == ["train-9"]
    assert summary["total_tokens"] is None


@pytest.mark.parametrize("change,decision", [
    (lambda rows: rows.append(copy.deepcopy(rows[0])), "duplicate_task_rows"),
    (lambda rows: rows[0].update(task_id="eval-0"), "extra_task_rows"),
    (lambda rows: rows[0].update(task_package_digest="sha256:" + "f" * 64), "wrong_digest"),
    (lambda rows: rows[0]["counts"].update(raw_reward=0), "malformed_counts"),
    (lambda rows: rows[0]["counts"].update(scored="true"), "malformed_counts"),
    (lambda rows: rows[0]["usage"].update(input_tokens=-1), "malformed_usage"),
])
def test_bad_candidate_evidence_is_refused(manifest: dict[str, Any], change: Any, decision: str) -> None:
    candidate = _candidate(manifest, passes=10)
    change(candidate["rows"])
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [candidate])
    assert refusal.value.decision == decision


@pytest.mark.parametrize("repeat", [None, 2, True])
def test_seed_requires_explicit_repeat_one(manifest: dict[str, Any], repeat: Any) -> None:
    rows = _rows(manifest)
    rows[0]["repeat"] = repeat
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, rows, [])
    assert refusal.value.decision == "invalid_baseline_repeat"


def test_two_candidate_cap_cannot_be_relaxed_by_manifest(manifest: dict[str, Any]) -> None:
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [_candidate(manifest)] * 3)
    assert refusal.value.decision == "too_many_candidates"
    manifest["max_candidates"] = 3
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [])
    assert refusal.value.decision == "malformed_manifest"


def test_duplicate_or_unqualified_candidate_digest_is_refused(manifest: dict[str, Any]) -> None:
    candidate = _candidate(manifest)
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [candidate, candidate])
    assert refusal.value.decision == "duplicate_candidate_ids"
    candidate["candidate_id"] = candidate["candidate_id"].removeprefix("sha256:")
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [candidate])
    assert refusal.value.decision == "malformed_candidate_id"


def test_unknown_tokens_cannot_prove_preference_between_two_improved_candidates(manifest: dict[str, Any]) -> None:
    earlier = _candidate(manifest, passes=4, tokens=None)
    later = _candidate(manifest, passes=4, tokens=1, candidate_id="sha256:" + "b" * 64, frozen_at="2026-10-01T08:10:00Z")
    result = _gate.select_candidate(manifest, _rows(manifest), [later, earlier])
    assert result["winner"] == earlier["candidate_id"]
    earlier["rows"] = _rows(manifest, passes=4, tokens=100)
    assert _gate.select_candidate(manifest, _rows(manifest), [earlier, later])["winner"] == later["candidate_id"]


def test_naive_candidate_timestamp_is_refused(manifest: dict[str, Any]) -> None:
    with pytest.raises(_gate.GateError) as refusal:
        _gate.select_candidate(manifest, _rows(manifest), [_candidate(manifest, frozen_at="2026-10-01T08:00:00")])
    assert refusal.value.decision == "malformed_timestamp"


def test_cli_freeze_is_immutable_and_selection_checks_candidate_bytes(sources: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proposal, held_out = sources
    frozen = tmp_path / "manifest.json"
    freeze_args = ["freeze", "--proposal", str(proposal), "--eval-tasks", str(held_out), "--out", str(frozen)]
    assert _gate.main(freeze_args) == 0
    first_bytes = frozen.read_bytes()
    assert _gate.main(freeze_args) == 0
    proposal.write_bytes(proposal.read_bytes() + b"\n")
    assert _gate.main(freeze_args) == 2
    assert frozen.read_bytes() == first_bytes
    manifest = json.loads(first_bytes)
    seed, instructions, receipt, result_path = (tmp_path / name for name in ("seed.json", "instructions.txt", "candidate.json", "selection.json"))
    seed.write_text(json.dumps(_rows(manifest)))
    instructions.write_text("Preserve the command's exit status.\n")
    candidate = _candidate(manifest, passes=4, candidate_id="sha256:" + hashlib.sha256(instructions.read_bytes()).hexdigest())
    candidate["candidate_path"] = str(instructions)
    receipt.write_text(json.dumps(candidate))
    select_args = ["select", "--manifest", str(frozen), "--seed", str(seed), "--candidates", str(receipt), "--out", str(result_path)]
    assert _gate.main(select_args) == 0
    assert json.loads(result_path.read_text())["winner"] == candidate["candidate_id"]
    instructions.write_text("Changed after freeze.\n")
    assert _gate.main(select_args) == 2
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["decision"] == "candidate_digest_drift"
