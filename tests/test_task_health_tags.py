"""Evidence precedence and immutable metadata variants for Harbor task health."""

from __future__ import annotations

import csv
import json
import shutil
import tomllib
from pathlib import Path

import pytest

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_health_tags import (
    generate_health_tags,
    health_tag,
    metadata_bytes,
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


def test_metadata_projection_preserves_every_other_configuration_field():
    original = b"""version = "1.0"
[task]
name = "mimo-v2.6-rl/format-code-task-000001"
keywords = ["health:unrelated-keyword"]
[metadata]
category = "Python"
tags = [
  "upstream", "health:discarded", "solve:never-run",
  "task:retained",
]
[metadata.custom]
flag = true
[environment]
memory_mb = 2048
[environment.healthcheck]
command = "echo '[metadata] tags = []'"
"""
    updated = metadata_bytes(original, ["health:sound", "solve:mixed"])
    expected = tomllib.loads(original.decode())
    expected["metadata"]["tags"] = ["upstream", "task:retained", "health:sound", "solve:mixed"]
    assert tomllib.loads(updated.decode()) == expected
    assert metadata_bytes(updated, ["health:sound", "solve:mixed"]) == updated


def _csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _inputs(tmp_path: Path) -> tuple[Path, Path, dict]:
    root = tmp_path / "repo"
    parent = root / "sources/task"
    (parent / "tests").mkdir(parents=True)
    (parent / "task.toml").write_text(
        '[task]\nname = "mimo-v2.6-rl/format-code-task-000001"\n'
        '[metadata]\ncategory = "Python"\ntags = ["original"]\n',
        encoding="utf-8",
    )
    (parent / "instruction.md").write_text("Implement the requested behavior.\n")
    (parent / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    digest = task_directory_digest(parent)
    task_id = "format-code-task-000001"
    _csv(
        root / "ledger.csv",
        ["task_id", "status", "reason", "run", "run_digest", "run_variant_status"],
        [
            {
                "task_id": task_id,
                "status": "usable",
                "reason": "nop sound",
                "run": "original",
                "run_digest": digest,
                "run_variant_status": "",
            },
        ],
    )
    _csv(
        root / "locked.csv",
        ["task_id", "locked_nop", "note"],
        [
            {"task_id": task_id, "locked_nop": "sound", "note": ""},
        ],
    )
    _csv(
        root / "history.csv",
        ["task_id", "runs", "clean_pass", "copied_pass", "fail", "infra"],
        [
            {
                "task_id": task_id,
                "runs": "3",
                "clean_pass": "1",
                "copied_pass": "1",
                "fail": "1",
                "infra": "0",
            },
        ],
    )
    (root / "pool.json").write_text(
        json.dumps(
            {
                "pool": [
                    {"task_id": task_id, "task_version_digest": digest, "task": "sources/task"},
                ]
            }
        )
    )
    options = {
        "ledger_path": Path("ledger.csv"),
        "locked_nop_path": Path("locked.csv"),
        "history_path": Path("history.csv"),
        "pool_path": Path("pool.json"),
        "source_root": root,
        "variants_root": Path("derived/variants"),
        "view_root": Path("derived/view"),
    }
    return root, parent, options


def test_generator_pins_parent_and_reuses_rebuildable_metadata_variant(tmp_path: Path):
    root, parent, options = _inputs(tmp_path)
    before = task_directory_digest(parent)
    first = generate_health_tags(root, **options)
    row = first["tasks"][0]
    assert row["tags"] == ["health:sound", "solve:mixed"]
    package = Path(row["package"])
    record_path = Path(row["record"])
    record_bytes = record_path.read_bytes()
    record = json.loads(record_bytes)
    assert record["components_changed"] == ["task_toml"]
    assert record["parent"]["digest"] == before
    assert record["variant_digest"] == task_directory_digest(package)
    assert task_directory_digest(parent) == before
    assert (root / "derived/view/format-code-task-000001").resolve() == package
    shutil.rmtree(package)
    second = generate_health_tags(root, **options)
    assert second["tasks"][0]["reused"] is True
    assert record_path.read_bytes() == record_bytes
    assert task_directory_digest(package) == row["variant_digest"]
    (parent / "instruction.md").write_text("Different task.\n")
    with pytest.raises(ValueError, match="differs from ledger"):
        generate_health_tags(root, **options)


def test_exploit_verdict_is_bound_to_the_executed_package_not_task_name(tmp_path: Path):
    root, parent, options = _inputs(tmp_path)
    trial = root / "retained/job/probe__trial"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"task": {"path": "/staged/current"}}))
    (trial.parent / "lock.json").write_text(
        json.dumps(
            {
                "trials": [
                    {"task": {"path": "/staged/other", "digest": "sha256:" + "c" * 64}},
                    {"task": {"path": "/staged/current", "digest": harbor_task_digest(parent)}},
                ]
            }
        )
    )
    verdict = root / "exploit.json"
    verdict.write_text(
        json.dumps({"format-code-task-000001": {"verdict": "cracked", "trial": str(trial)}})
    )
    report = generate_health_tags(root, exploit_path=verdict, **options)
    assert report["tasks"][0]["tags"] == ["health:cracked", "solve:mixed"]
    assert report["tasks"][0]["exploit"]["digest_match"] is True
    # An explicit conflicting package binding cannot fall back to the native
    # lock or reuse the result merely because the task-id key is the same.
    verdict.write_text(
        json.dumps(
            {
                "format-code-task-000001": {
                    "verdict": "cracked",
                    "trial": str(trial),
                    "task_package_digest": "sha256:" + "d" * 64,
                }
            }
        )
    )
    report = generate_health_tags(root, exploit_path=verdict, **options)
    assert report["tasks"][0]["tags"] == ["health:sound", "solve:mixed"]
    assert report["tasks"][0]["exploit"]["digest_match"] is False
