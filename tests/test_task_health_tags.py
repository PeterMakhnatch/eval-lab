"""Evidence precedence and immutable metadata variants for Harbor task health."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.task_health_tags import (
    _probe_binding,
    health_tag,
    solve_summary,
)


@pytest.mark.parametrize(
    ("counts", "tag", "known"),
    [
        ((0, 0, 0, 0), "solve:never-run", 0),
        ((0, 2, 0, 1), "solve:unscored", 0),
        ((0, 1, 2, 3), "solve:0-of-n", 2),
        ((2, 1, 1, 2), "solve:mixed", 3),
        ((2, 1, 0, 2), "solve:always", 2),
    ],
)
def test_solve_tags_exclude_copies_and_infra(counts, tag, known):
    row = dict(zip(("clean_pass", "copied_pass", "fail", "infra"), map(str, counts), strict=True))
    row["runs"] = str(sum(counts))
    result = solve_summary(row)
    assert (result["tag"], result["known_attempts"]) == (tag, known)
    assert solve_summary(None)["tag"] == "solve:unknown"


def test_inconsistent_history_cannot_label_a_task_always_solved():
    with pytest.raises(ValueError, match="outcome counts"):
        solve_summary(
            {"runs": "2", "clean_pass": "2", "copied_pass": "0", "fail": "1", "infra": "0"}
        )


def test_adverse_health_evidence_wins_over_sound_nop_and_clean_probe():
    row = {
        "status": "discarded",
        "reason": "image leaks the fix: /testbed/build/lib",
        "run_digest": "sha256:" + "a" * 64,
    }
    assert health_tag(row, {"locked_nop": "sound"}, exploit_verdict="clean") == "health:leak-found"
    assert health_tag(row, {"locked_nop": "sound"}, exploit_verdict="cracked") == "health:cracked"
    row.update(status="usable", reason="nop sound, leak closed")
    assert health_tag(row, {"locked_nop": "sound"}) == "health:sound"
    assert (
        health_tag(row, {"locked_nop": "sound"}, exploit_verdict="leak-found-not-cracked")
        == "health:leak-found"
    )
    assert health_tag(row, None) == "health:unchecked"
    assert health_tag(row, {"locked_nop": "infra:TimeoutError"}) == "health:unchecked"
    assert health_tag(row, {"locked_nop": "broken_environment"}) == "health:broken-environment"


def test_repair_identity_distinguishes_old_nop_from_current_failure():
    row = {
        "status": "usable",
        "reason": "validated repair",
        "run": "repair",
        "run_variant_status": "validated",
        "run_digest": "sha256:" + "b" * 64,
    }
    assert health_tag(row, {"locked_nop": "broken_environment", "note": ""}) == "health:repaired"
    assert (
        health_tag(row, {"locked_nop": "broken_environment", "note": "repaired:" + "b" * 12})
        == "health:broken-environment"
    )
    row.update(run="original", run_variant_status="")
    assert (
        health_tag(row, {"locked_nop": "sound", "note": "repaired:" + "a" * 12})
        == "health:unchecked"
    )


def test_exploit_binding_uses_the_executed_package_and_never_falls_back(tmp_path: Path):
    trial = tmp_path / "job/probe__trial"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"task": {"path": "/staged/current"}}))
    native_digest = "sha256:" + "a" * 64
    (trial.parent / "lock.json").write_text(
        json.dumps(
            {
                "trials": [
                    {"task": {"path": "/staged/other", "digest": "sha256:" + "c" * 64}},
                    {"task": {"path": "/staged/current", "digest": native_digest}},
                ]
            }
        )
    )
    row = {"verdict": "cracked", "trial": str(trial)}
    assert _probe_binding(row, input_dir=tmp_path) == {
        "package": None,
        "harbor": native_digest,
    }
    explicit_digest = "sha256:" + "d" * 64
    assert _probe_binding(
        {**row, "task_package_digest": explicit_digest}, input_dir=tmp_path
    ) == {"package": explicit_digest, "harbor": None}
