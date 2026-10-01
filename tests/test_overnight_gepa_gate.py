"""Focused deterministic behavioral tests for HAR-135 GEPA gate.

Tests:
1. freeze_minibatch:
   - Validates first 10 rows of committed har120_proposal.csv.
   - Binds proposal and eval CSV SHA256 hashes.
   - Rejects held-out task ID collisions and repository collisions.
   - Rejects duplicate tasks, insufficient rows (<10), and malformed digests.
   - CLI freeze idempotency: byte-identical re-run succeeds, modified content refused.
2. select_candidate & selection CLI:
   - Distinguishes countedimprovement, tokensimprovement, seedtie, unknown tokens,
     exclusions, missingdata, wrongdigest, too many candidates.
   - Catches plausible wrongwinner, coverage inflation, and contamination bugs.
   - Unknown-preserving token tiebreak.
   - Excluded trials never converted to failures; partial candidate never full coverage.
   - Deterministic candidate ordering by passes, tokens, freeze timestamp, then digest.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from research.experiments.ovn_sft_v0.gepa_gate import (
    BASELINE_REPEAT,
    COUNTS_SCHEMA,
    DuplicateCandidateError,
    DuplicateTaskRowError,
    ExtraTaskRowError,
    FREEZE_SCHEMA_VERSION,
    GateValidationError,
    HeldOutCollisionError,
    HeldOutRepositoryCollisionError,
    HeldOutTaskCollisionError,
    InvalidBaselineRepeatError,
    MalformedCountsError,
    MAX_CANDIDATES,
    MAX_GATE_COST_USD,
    MissingTaskRowError,
    SELECTION_RULE,
    SELECTION_SCHEMA_VERSION,
    TASK_COUNT,
    TaskDigestDriftError,
    TooManyCandidatesError,
    freeze_minibatch,
    main,
    repo_key,
    select_candidate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_PATH = REPO_ROOT / "research/experiments/python-task-ledger/har120_proposal.csv"
EVAL_TASKS_PATH = REPO_ROOT / "research/experiments/ovn-sft-v0/eval_tasks.csv"

EXPECTED_FIRST_10_IDS = [
    "format-code-task-001647",
    "format-code-task-000803",
    "format-code-task-001870",
    "format-code-task-000341",
    "format-code-task-001897",
    "format-code-task-002938",
    "format-code-task-001710",
    "format-code-task-001399",
    "format-code-task-002680",
    "format-code-task-001661",
]


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


def _make_counts(
    verdict: str = "counted_pass",
    reasons: list[str] | None = None,
    raw_reward: float | None = None,
) -> dict[str, Any]:
    if verdict == "counted_pass":
        reward = 1.0 if raw_reward is None else raw_reward
        scored = True
        res_reasons = []
    elif verdict == "counted_fail":
        reward = 0.0 if raw_reward is None else raw_reward
        scored = True
        res_reasons = []
    elif verdict == "excluded":
        reward = raw_reward
        scored = False
        res_reasons = reasons or ["infra"]
    else:
        reward = raw_reward
        scored = False
        res_reasons = reasons or []

    return {
        "schema": COUNTS_SCHEMA,
        "raw_reward": reward,
        "scored": scored,
        "verdict": verdict,
        "reasons": res_reasons,
        "evidence": [],
        "flags": [],
        "judgments": [],
    }


def _make_row(
    task_id: str,
    digest: str,
    *,
    verdict: str = "counted_pass",
    inp: int | None = 100,
    outp: int | None = 100,
    repeat: int | None = 1,
    reasons: list[str] | None = None,
    raw_reward: float | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_package_digest": digest,
        "trial_id": f"trial-{task_id}",
        "repeat": repeat,
        "counts": _make_counts(verdict, reasons=reasons, raw_reward=raw_reward),
        "usage": {
            "input_tokens": inp,
            "output_tokens": outp,
        },
        "receipt_paths": {
            "trial_json": f"runs/trial-{task_id}/result.json",
        },
    }


def _build_manifest() -> dict[str, Any]:
    return freeze_minibatch(PROPOSAL_PATH, EVAL_TASKS_PATH)


def _build_seed_rows(manifest: dict[str, Any], pass_count: int = 3, inp: int = 100, outp: int = 100) -> list[dict[str, Any]]:
    rows = []
    for idx, t in enumerate(manifest["tasks"]):
        verdict = "counted_pass" if idx < pass_count else "counted_fail"
        rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict=verdict, inp=inp, outp=outp, repeat=1))
    return rows


# ---------------------------------------------------------------------------
# Freeze Minibatch Tests
# ---------------------------------------------------------------------------


def test_freeze_minibatch_committed_files() -> None:
    manifest = freeze_minibatch(PROPOSAL_PATH, EVAL_TASKS_PATH)

    assert manifest["schema_version"] == FREEZE_SCHEMA_VERSION
    assert manifest["baseline_repeat"] == BASELINE_REPEAT
    assert manifest["max_candidates"] == MAX_CANDIDATES
    assert manifest["max_gate_cost_usd"] == MAX_GATE_COST_USD
    assert manifest["selection_rule"] == SELECTION_RULE
    assert manifest["task_count"] == TASK_COUNT
    assert len(manifest["tasks"]) == TASK_COUNT

    # Validate bound SHA256 hashes
    assert manifest["proposal_sha256"] == "2795720c6cfc002396fe62a08b20949507cfa8e8a79cff5f5ec044b59a2ec563"
    assert manifest["eval_tasks_sha256"] == "504b913a7c0fbd3bb523580db1c687014a22ee23bb3209990f5b6f3a47ea3967"

    # Validate exact 10 tasks in image order
    actual_ids = [t["task_id"] for t in manifest["tasks"]]
    assert actual_ids == EXPECTED_FIRST_10_IDS

    for t in manifest["tasks"]:
        assert t["task_package_digest"].startswith("sha256:")
        assert len(t["task_package_digest"]) == 71
        assert t["project"]


def test_freeze_minibatch_detects_task_id_collision(tmp_path: Path) -> None:
    # Read real eval tasks to pick a colliding task ID
    with EVAL_TASKS_PATH.open() as f:
        eval_first = next(csv.DictReader(f))
    colliding_task_id = eval_first["task"]

    bad_proposal = tmp_path / "colliding_prop.csv"
    with bad_proposal.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["task_id", "project", "run_digest"])
        writer.writeheader()
        writer.writerow({
            "task_id": colliding_task_id,
            "project": "safe_repo",
            "run_digest": "sha256:" + "0" * 64,
        })
        for i in range(1, 10):
            writer.writerow({
                "task_id": f"format-code-task-999{i:03d}",
                "project": f"repo_{i}",
                "run_digest": f"sha256:{i}" + "0" * 63,
            })

    with pytest.raises(HeldOutTaskCollisionError) as exc_info:
        freeze_minibatch(bad_proposal, EVAL_TASKS_PATH)
    assert colliding_task_id in str(exc_info.value)
    assert isinstance(exc_info.value, HeldOutCollisionError)
    assert isinstance(exc_info.value, ValueError)


def test_freeze_minibatch_detects_repository_collision(tmp_path: Path) -> None:
    # Read real eval tasks to pick a colliding repository
    with EVAL_TASKS_PATH.open() as f:
        eval_first = next(csv.DictReader(f))
    colliding_repo = eval_first["repo"]

    bad_proposal = tmp_path / "colliding_repo_prop.csv"
    with bad_proposal.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["task_id", "project", "run_digest"])
        writer.writeheader()
        writer.writerow({
            "task_id": "format-code-task-999000",
            "project": colliding_repo,
            "run_digest": "sha256:" + "0" * 64,
        })
        for i in range(1, 10):
            writer.writerow({
                "task_id": f"format-code-task-999{i:03d}",
                "project": f"safe_repo_{i}",
                "run_digest": f"sha256:{i}" + "0" * 63,
            })

    with pytest.raises(HeldOutRepositoryCollisionError) as exc_info:
        freeze_minibatch(bad_proposal, EVAL_TASKS_PATH)
    assert repo_key(colliding_repo) in str(exc_info.value)


def test_freeze_minibatch_detects_duplicate_tasks_and_insufficient_rows(tmp_path: Path) -> None:
    # Duplicate tasks
    dup_proposal = tmp_path / "dup_prop.csv"
    with dup_proposal.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["task_id", "project", "run_digest"])
        writer.writeheader()
        for i in range(10):
            writer.writerow({
                "task_id": "format-code-task-dup" if i < 2 else f"format-code-task-{i}",
                "project": f"repo_{i}",
                "run_digest": f"sha256:{i}" + "0" * 63,
            })
    with pytest.raises(DuplicateTaskRowError):
        freeze_minibatch(dup_proposal, EVAL_TASKS_PATH)

    # Insufficient rows (<10)
    short_proposal = tmp_path / "short_prop.csv"
    with short_proposal.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["task_id", "project", "run_digest"])
        writer.writeheader()
        for i in range(5):
            writer.writerow({
                "task_id": f"format-code-task-{i}",
                "project": f"repo_{i}",
                "run_digest": f"sha256:{i}" + "0" * 63,
            })
    with pytest.raises(GateValidationError) as exc:
        freeze_minibatch(short_proposal, EVAL_TASKS_PATH)
    assert "minimum 10 required" in str(exc.value)


def test_freeze_cli_idempotency_and_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = tmp_path / "frozen_manifest.json"

    # Run 1: Writes new manifest
    code1 = main(["freeze", "--proposal", str(PROPOSAL_PATH), "--eval-tasks", str(EVAL_TASKS_PATH), "--out", str(out_file)])
    assert code1 == 0
    assert out_file.is_file()
    first_bytes = out_file.read_bytes()

    # Run 2: Byte-identical re-run succeeds (idempotent)
    code2 = main(["freeze", "--proposal", str(PROPOSAL_PATH), "--eval-tasks", str(EVAL_TASKS_PATH), "--out", str(out_file)])
    assert code2 == 0
    assert out_file.read_bytes() == first_bytes

    # Run 3: Modify existing file -> refuses to overwrite
    out_file.write_text('{"tampered": true}\n', encoding="utf-8")
    code3 = main(["freeze", "--proposal", str(PROPOSAL_PATH), "--eval-tasks", str(EVAL_TASKS_PATH), "--out", str(out_file)])
    assert code3 == 1
    err = capsys.readouterr().err
    assert "Refusing to overwrite existing manifest with different content" in err


# ---------------------------------------------------------------------------
# Candidate Selection Tests: Outcomes & Scientific Rules
# ---------------------------------------------------------------------------


def test_selection_promotes_counted_improvement() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3, inp=100, outp=100)  # 3 passes, 2000 tokens

    # Candidate has 4 passes, 2500 tokens (more passes wins even with higher tokens)
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        verdict = "counted_pass" if idx < 4 else "counted_fail"
        cand_rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict=verdict, inp=125, outp=125))

    candidate = {
        "candidate_id": "sha256:" + "a" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "promoted"
    assert result["decision"] == "counted_improvement"
    assert result["winner"] == candidate["candidate_id"]
    assert result["promoted"] is True
    assert result["seed"]["counted_passes"] == 3
    assert result["candidates"][0]["counted_passes"] == 4


def test_selection_promotes_tokens_improvement_on_equal_passes() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3, inp=100, outp=100)  # 3 passes, 2000 tokens

    # Candidate has 3 passes, 1200 tokens (strictly fewer tokens on equal passes)
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        verdict = "counted_pass" if idx < 3 else "counted_fail"
        cand_rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict=verdict, inp=60, outp=60))

    candidate = {
        "candidate_id": "sha256:" + "b" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "promoted"
    assert result["decision"] == "tokens_improvement"
    assert result["winner"] == candidate["candidate_id"]
    assert result["promoted"] is True
    assert result["candidates"][0]["total_tokens"] == 1200
    assert result["seed"]["total_tokens"] == 2000


def test_selection_retains_seed_on_exact_tie() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3, inp=100, outp=100)  # 3 passes, 2000 tokens

    # Candidate has exactly 3 passes and 2000 tokens
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        verdict = "counted_pass" if idx < 3 else "counted_fail"
        cand_rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict=verdict, inp=100, outp=100))

    candidate = {
        "candidate_id": "sha256:" + "c" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "seed_retained"
    assert result["decision"] == "seed_tie"
    assert result["winner"] == "seed"
    assert result["promoted"] is False
    assert "exact tie" in result["reasons"][0]


def test_selection_retains_seed_when_tokens_unknown() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3, inp=100, outp=100)  # 3 passes, 2000 tokens

    # Candidate has 3 passes, but task 0 has None input_tokens
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        verdict = "counted_pass" if idx < 3 else "counted_fail"
        inp = None if idx == 0 else 10
        cand_rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict=verdict, inp=inp, outp=10))

    candidate = {
        "candidate_id": "sha256:" + "d" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "seed_retained"
    assert result["decision"] == "unknown_tokens"
    assert result["winner"] == "seed"
    assert result["promoted"] is False
    assert result["candidates"][0]["has_unknown_tokens"] is True


def test_selection_ineligible_on_exclusions_never_converted_to_fail() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=2, inp=100, outp=100)

    # Candidate has 9 passes, but 1 task is excluded (e.g. copied_fix)
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        if idx == 0:
            row = _make_row(t["task_id"], t["task_package_digest"], verdict="excluded", reasons=["copied_fix"])
        else:
            row = _make_row(t["task_id"], t["task_package_digest"], verdict="counted_pass", inp=10, outp=10)
        cand_rows.append(row)

    candidate = {
        "candidate_id": "sha256:" + "e" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "ineligible"
    assert result["decision"] == "exclusions"
    assert result["winner"] == "seed"
    assert result["promoted"] is False

    cand_summary = result["candidates"][0]
    assert cand_summary["eligible"] is False
    assert cand_summary["excluded"] == 1
    assert cand_summary["counted_fails"] == 0  # EXCLUDED MUST NEVER BE CONVERTED TO FAIL
    assert "copied_fix" in cand_summary["ineligible_reasons"][0]


def test_selection_ineligible_on_missing_task_rows() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=2, inp=100, outp=100)

    # Candidate only evaluated on 8 tasks (missing 2)
    cand_rows = []
    for idx in range(8):
        t = manifest["tasks"][idx]
        cand_rows.append(_make_row(t["task_id"], t["task_package_digest"], verdict="counted_pass", inp=10, outp=10))

    candidate = {
        "candidate_id": "sha256:" + "f" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate])
    assert result["status"] == "ineligible"
    assert result["decision"] == "missing_data"
    assert result["winner"] == "seed"
    assert result["promoted"] is False
    assert result["candidates"][0]["eligible"] is False
    assert result["candidates"][0]["task_count"] == 8


def test_selection_rejects_task_digest_drift() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)

    # Candidate row for task 0 has tampered digest
    cand_rows = []
    for idx, t in enumerate(manifest["tasks"]):
        digest = ("sha256:" + "f" * 64) if idx == 0 else t["task_package_digest"]
        cand_rows.append(_make_row(t["task_id"], digest, verdict="counted_pass"))

    candidate = {
        "candidate_id": "sha256:" + "1" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": cand_rows,
    }

    result = select_candidate(manifest, seed_rows, [candidate], raise_on_reject=False)
    assert result["status"] == "rejected"
    assert result["decision"] == "wrong_digest"
    assert result["promoted"] is False

    with pytest.raises(TaskDigestDriftError):
        select_candidate(manifest, seed_rows, [candidate], raise_on_reject=True)


def test_selection_rejects_too_many_candidates() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)

    candidates = [
        {
            "candidate_id": f"sha256:{i}" + "0" * 63,
            "frozen_at": "2026-10-01T12:00:00Z",
            "rows": _build_seed_rows(manifest, pass_count=4),
        }
        for i in range(3)
    ]

    result = select_candidate(manifest, seed_rows, candidates, raise_on_reject=False)
    assert result["status"] == "rejected"
    assert result["decision"] == "too_many_candidates"

    with pytest.raises(TooManyCandidatesError):
        select_candidate(manifest, seed_rows, candidates, raise_on_reject=True)


def test_selection_rejects_duplicate_candidate_ids() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)

    same_cid = "sha256:" + "9" * 64
    c1 = {
        "candidate_id": same_cid,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4),
    }
    c2 = {
        "candidate_id": same_cid,
        "frozen_at": "2026-10-01T13:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=5),
    }

    result = select_candidate(manifest, seed_rows, [c1, c2], raise_on_reject=False)
    assert result["status"] == "rejected"
    assert result["decision"] == "duplicate_candidate_ids"

    with pytest.raises(DuplicateCandidateError):
        select_candidate(manifest, seed_rows, [c1, c2], raise_on_reject=True)


def test_selection_rejects_seed_repeat_greater_than_one() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)
    seed_rows[0]["repeat"] = 2  # Not repeat 1!

    cand = {
        "candidate_id": "sha256:" + "7" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4),
    }

    result = select_candidate(manifest, seed_rows, [cand], raise_on_reject=False)
    assert result["status"] == "rejected"
    assert result["decision"] == "invalid_baseline_repeat"

    with pytest.raises(InvalidBaselineRepeatError):
        select_candidate(manifest, seed_rows, [cand], raise_on_reject=True)


def test_selection_rejects_malformed_counts() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)
    seed_rows[0]["counts"]["verdict"] = "counted_pass"
    seed_rows[0]["counts"]["raw_reward"] = 0.5  # Contradiction: pass cannot have reward 0.5!

    cand = {
        "candidate_id": "sha256:" + "8" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4),
    }

    result = select_candidate(manifest, seed_rows, [cand], raise_on_reject=False)
    assert result["status"] == "rejected"
    assert result["decision"] == "malformed_counts"

    with pytest.raises(MalformedCountsError):
        select_candidate(manifest, seed_rows, [cand], raise_on_reject=True)


def test_selection_rejects_extra_and_duplicate_task_rows() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=3)

    # Extra task row
    cand_extra = {
        "candidate_id": "sha256:" + "3" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4) + [_make_row("extra-task-1", "sha256:" + "0" * 64)],
    }
    result_extra = select_candidate(manifest, seed_rows, [cand_extra], raise_on_reject=False)
    assert result_extra["decision"] == "extra_task_rows"
    with pytest.raises(ExtraTaskRowError):
        select_candidate(manifest, seed_rows, [cand_extra], raise_on_reject=True)

    # Duplicate task row
    cand_dup = {
        "candidate_id": "sha256:" + "4" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4)[:9] + [_build_seed_rows(manifest, pass_count=4)[0]],
    }
    result_dup = select_candidate(manifest, seed_rows, [cand_dup], raise_on_reject=False)
    assert result_dup["decision"] == "duplicate_task_rows"
    with pytest.raises(DuplicateTaskRowError):
        select_candidate(manifest, seed_rows, [cand_dup], raise_on_reject=True)


def test_selection_candidate_ordering_by_timestamp_then_digest() -> None:
    manifest = _build_manifest()
    seed_rows = _build_seed_rows(manifest, pass_count=2)

    # Both candidates have 4 passes, 1000 tokens (tied on metrics)
    c1_earlier = {
        "candidate_id": "sha256:" + "b" * 64,
        "frozen_at": "2026-10-01T10:00:00Z",  # Earlier timestamp
        "rows": _build_seed_rows(manifest, pass_count=4, inp=50, outp=50),
    }
    c2_later = {
        "candidate_id": "sha256:" + "a" * 64,  # Smaller hex digest, but later timestamp
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=4, inp=50, outp=50),
    }

    # c1_earlier must win because timestamp takes precedence over candidate_id
    res1 = select_candidate(manifest, seed_rows, [c1_earlier, c2_later])
    assert res1["winner"] == c1_earlier["candidate_id"]

    # When timestamps are identical, smaller digest wins
    c2_same_time = {
        "candidate_id": "sha256:" + "a" * 64,
        "frozen_at": "2026-10-01T10:00:00Z",  # Same timestamp as c1
        "rows": _build_seed_rows(manifest, pass_count=4, inp=50, outp=50),
    }
    res2 = select_candidate(manifest, seed_rows, [c1_earlier, c2_same_time])
    assert res2["winner"] == c2_same_time["candidate_id"]


# ---------------------------------------------------------------------------
# Full CLI Execution Tests for All 8 Outcomes
# ---------------------------------------------------------------------------


def test_cli_select_distinguishes_all_outcomes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _build_manifest()
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    seed_rows = _build_seed_rows(manifest, pass_count=3, inp=100, outp=100)
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps(seed_rows, indent=2), encoding="utf-8")

    # 1. counted_improvement
    c_counted = {
        "candidate_id": "sha256:" + "1" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=5),
    }
    c_counted_file = tmp_path / "c_counted.json"
    c_counted_file.write_text(json.dumps(c_counted), encoding="utf-8")
    out_1 = tmp_path / "out_1.json"
    code_1 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_counted_file), "--out", str(out_1)])
    assert code_1 == 0
    assert json.loads(out_1.read_text())["decision"] == "counted_improvement"

    # 2. tokens_improvement
    c_tokens = {
        "candidate_id": "sha256:" + "2" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=3, inp=40, outp=40),
    }
    c_tokens_file = tmp_path / "c_tokens.json"
    c_tokens_file.write_text(json.dumps(c_tokens), encoding="utf-8")
    out_2 = tmp_path / "out_2.json"
    code_2 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_tokens_file), "--out", str(out_2)])
    assert code_2 == 0
    assert json.loads(out_2.read_text())["decision"] == "tokens_improvement"

    # 3. seed_tie
    c_tie = {
        "candidate_id": "sha256:" + "3" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=3, inp=100, outp=100),
    }
    c_tie_file = tmp_path / "c_tie.json"
    c_tie_file.write_text(json.dumps(c_tie), encoding="utf-8")
    out_3 = tmp_path / "out_3.json"
    code_3 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_tie_file), "--out", str(out_3)])
    assert code_3 == 0
    assert json.loads(out_3.read_text())["decision"] == "seed_tie"

    # 4. unknown_tokens
    c_unknown_rows = _build_seed_rows(manifest, pass_count=3, inp=50, outp=50)
    c_unknown_rows[0]["usage"]["input_tokens"] = None
    c_unknown = {
        "candidate_id": "sha256:" + "4" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": c_unknown_rows,
    }
    c_unknown_file = tmp_path / "c_unknown.json"
    c_unknown_file.write_text(json.dumps(c_unknown), encoding="utf-8")
    out_4 = tmp_path / "out_4.json"
    code_4 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_unknown_file), "--out", str(out_4)])
    assert code_4 == 0
    assert json.loads(out_4.read_text())["decision"] == "unknown_tokens"

    # 5. exclusions
    c_excl_rows = _build_seed_rows(manifest, pass_count=5)
    c_excl_rows[0] = _make_row(manifest["tasks"][0]["task_id"], manifest["tasks"][0]["task_package_digest"], verdict="excluded", reasons=["task_not_usable"])
    c_excl = {
        "candidate_id": "sha256:" + "5" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": c_excl_rows,
    }
    c_excl_file = tmp_path / "c_excl.json"
    c_excl_file.write_text(json.dumps(c_excl), encoding="utf-8")
    out_5 = tmp_path / "out_5.json"
    code_5 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_excl_file), "--out", str(out_5)])
    assert code_5 == 0
    assert json.loads(out_5.read_text())["decision"] == "exclusions"

    # 6. missing_data
    c_miss = {
        "candidate_id": "sha256:" + "6" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": _build_seed_rows(manifest, pass_count=5)[:8],  # only 8 rows
    }
    c_miss_file = tmp_path / "c_miss.json"
    c_miss_file.write_text(json.dumps(c_miss), encoding="utf-8")
    out_6 = tmp_path / "out_6.json"
    code_6 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_miss_file), "--out", str(out_6)])
    assert code_6 == 0
    assert json.loads(out_6.read_text())["decision"] == "missing_data"

    # 7. wrong_digest
    c_drift_rows = _build_seed_rows(manifest, pass_count=5)
    c_drift_rows[0]["task_package_digest"] = "sha256:" + "e" * 64
    c_drift = {
        "candidate_id": "sha256:" + "7" * 64,
        "frozen_at": "2026-10-01T12:00:00Z",
        "rows": c_drift_rows,
    }
    c_drift_file = tmp_path / "c_drift.json"
    c_drift_file.write_text(json.dumps(c_drift), encoding="utf-8")
    out_7 = tmp_path / "out_7.json"
    code_7 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", str(c_drift_file), "--out", str(out_7)])
    assert code_7 == 2
    assert json.loads(out_7.read_text())["decision"] == "wrong_digest"

    # 8. too_many_candidates
    cand_extra_files = []
    for i in range(3):
        cf = tmp_path / f"c_extra_{i}.json"
        cf.write_text(json.dumps({
            "candidate_id": f"sha256:{i}" + "0" * 63,
            "frozen_at": "2026-10-01T12:00:00Z",
            "rows": _build_seed_rows(manifest, pass_count=4),
        }), encoding="utf-8")
        cand_extra_files.append(str(cf))
    out_8 = tmp_path / "out_8.json"
    code_8 = main(["select", "--manifest", str(manifest_file), "--seed", str(seed_file), "--candidates", *cand_extra_files, "--out", str(out_8)])
    assert code_8 == 2
    assert json.loads(out_8.read_text())["decision"] == "too_many_candidates"
