"""HAR-177 verdict plumbing: ledger verdicts, verdict tags, probe binding."""

from __future__ import annotations

import csv
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from evallab.registry import task_directory_digest
from evallab.task_health_tags import (
    KNOWN_TAGS,
    generate_health_tags,
    metadata_bytes,
    verdict_tag,
)

_BUILD = Path(__file__).resolve().parents[1] / "research/experiments/python-task-ledger/build.py"


def _build():
    spec = importlib.util.spec_from_file_location("har177_ledger_build", _BUILD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BUILD_MOD = _build()


def _strip(status="candidate"):
    record = {"transform": "strip-future-history@1", "status": status}
    return (record, _BUILD_MOD.ROOT / f"library/task-variants/fixture/{status}.json")


def test_verdict_mapping_discard_wins_and_nop_failure_is_fix(monkeypatch):
    mod = _BUILD_MOD
    assert mod.verdict_for("discarded", "format-code-task-000124", _strip()) == (
        "discard",
        "ledger:status=discarded",
    )
    # The nop-failure mechanism still forces fix when populated.
    monkeypatch.setattr(mod, "STRIP_NOP_FAILED", {"format-code-task-000003": ("runs/fixture-nop",)})
    verdict, evidence = mod.verdict_for("usable", "format-code-task-000003", _strip())
    assert (verdict, evidence) == ("fix", "runs/fixture-nop:strip-nop-failed")
    # A discarded status still wins over a nop failure.
    assert mod.verdict_for("discarded", "format-code-task-000003", None)[0] == "discard"


def test_verdict_mapping_fixed_transform_task_is_keep_with_note():
    mod = _BUILD_MOD
    verdict, evidence = mod.verdict_for("usable", "format-code-task-000076", _strip("validated"))
    assert verdict == "keep"
    assert "runs/har177-r2nop-000076-80e446f0f122:strip-nop-pass-fixed-transform" in evidence


def test_verdict_mapping_strip_present_is_keep_and_probe_evidence_retained():
    mod = _BUILD_MOD
    assert mod.verdict_for("usable", "format-code-task-000003", _strip()) == (
        "keep",
        "library/task-variants/fixture/candidate.json:strip-future-history@1=candidate",
    )
    keep = mod.verdict_for("usable", "format-code-task-000003", _strip("validated"))[0]
    assert keep == "keep"
    verdict, evidence = mod.verdict_for("review", "format-code-task-002402", _strip())
    assert verdict == "keep"
    assert "build.py:PROBE_CRACKED#format-code-task-002402" in evidence
    verdict, evidence = mod.verdict_for("review", "format-code-task-002552", _strip())
    assert verdict == "keep"
    assert "build.py:PROBE_CRACKED#format-code-task-002552" in evidence
    # No strip variant and no nop failure is still fix.
    assert mod.verdict_for("usable", "format-code-task-000003", None) == (
        "fix",
        "library/task-variants:strip-future-history@1=absent",
    )


def test_oracle_not_keep_overrides_keep_and_discard_wins():
    mod = _BUILD_MOD
    keep_evidence = "library/task-variants/fixture/candidate.json:strip-future-history@1=candidate"
    network = {
        "label": "oracle:fail-network",
        "evidence": "research/experiments/python-task-ledger/oracle-pilot/000552-locked.json",
        "run_digest": "sha256:abc",
    }
    verdict, evidence = mod.apply_oracle("keep", keep_evidence, network)
    assert verdict == "fix"
    assert evidence.endswith(":oracle:fail-network")
    none = {
        "label": "oracle:none",
        "evidence": "research/experiments/python-task-ledger/oracle-pilot/001198.json",
        "run_digest": "sha256:def",
    }
    assert mod.apply_oracle("keep", keep_evidence, none)[0] == "fix"
    # A discarded status still wins over an oracle label.
    assert mod.apply_oracle("discard", "ledger:status=discarded", network) == (
        "discard",
        "ledger:status=discarded",
    )
    # Extractor misses do not override a strip keep.
    neutral = {"label": "oracle:fail", "evidence": "x", "run_digest": ""}
    assert mod.apply_oracle("keep", keep_evidence, neutral) == ("keep", keep_evidence)


def test_oracle_loader_rejects_an_unmapped_label(tmp_path, monkeypatch):
    mod = _BUILD_MOD
    bad = tmp_path / "oracle_sweep.csv"
    bad.write_text("task_id,label,evidence\nformat-code-task-000001,oracle:maybe,x\n")
    monkeypatch.setattr(mod, "ORACLE_SWEEP", bad)
    monkeypatch.setattr(mod, "ORACLE_PILOT", tmp_path / "missing.csv")
    with pytest.raises(SystemExit, match="unmapped oracle label"):
        mod.load_oracle()


def test_strip_pick_takes_the_latest_strip_record():
    mod = _BUILD_MOD
    old = (
        {
            "transform": "strip-future-history@1",
            "status": "candidate",
            "created_at": "2026-10-01T00:00:00",
        },
        Path("a.json"),
    )
    new = (
        {
            "transform": "strip-future-history@1",
            "status": "validated",
            "created_at": "2026-10-06T00:00:00",
        },
        Path("b.json"),
    )
    other = (
        {
            "transform": "leak-close-pypi@1",
            "status": "validated",
            "created_at": "2026-10-07T00:00:00",
        },
        Path("c.json"),
    )
    assert mod.strip_pick([old, new, other]) == new
    assert mod.strip_pick([other]) is None
    assert mod.strip_pick([]) is None


def test_verdict_tags_are_known_and_validated():
    assert {"verdict:keep", "verdict:fix", "verdict:discard"} <= KNOWN_TAGS
    assert verdict_tag({"verdict": "keep"}) == "verdict:keep"
    assert verdict_tag({"verdict": "fix"}) == "verdict:fix"
    assert verdict_tag({"verdict": "discard"}) == "verdict:discard"
    with pytest.raises(ValueError, match="unsupported verdict"):
        verdict_tag({"verdict": "maybe"})
    with pytest.raises(ValueError, match="unsupported verdict"):
        verdict_tag({})


def test_metadata_bytes_manages_verdict_namespace_and_description():
    original = (
        b'[task]\nname = "mimo-v2.6-rl/format-code-task-000001"\n'
        b'[metadata]\ncategory = "Python"\n'
        b'tags = ["upstream", "verdict:keep", "health:sound", "solve:mixed"]\n'
    )
    updated = metadata_bytes(
        original,
        ["health:discarded", "solve:never-run", "verdict:discard"],
        description="image leaks the fix: /testbed/build/lib",
    )
    parsed = tomllib.loads(updated.decode())
    assert parsed["metadata"]["tags"] == [
        "upstream",
        "health:discarded",
        "solve:never-run",
        "verdict:discard",
    ]
    assert parsed["metadata"]["description"] == "image leaks the fix: /testbed/build/lib"
    assert parsed["metadata"]["category"] == "Python"
    # Re-running with the same tags is idempotent; None preserves description.
    assert (
        metadata_bytes(
            updated,
            ["health:discarded", "solve:never-run", "verdict:discard"],
            description="image leaks the fix: /testbed/build/lib",
        )
        == updated
    )
    assert metadata_bytes(original, ["health:sound", "solve:mixed", "verdict:keep"]) != updated
    with pytest.raises(ValueError, match="non-empty"):
        metadata_bytes(original, ["health:sound"], description="  ")


def _csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _task_parent(root: Path, task_id: str) -> tuple[Path, str]:
    parent = root / "sources" / task_id
    (parent / "tests").mkdir(parents=True)
    (parent / "task.toml").write_text(
        f'[task]\nname = "mimo-v2.6-rl/{task_id}"\n'
        '[metadata]\ncategory = "Python"\ntags = ["original"]\n',
        encoding="utf-8",
    )
    (parent / "instruction.md").write_text("Implement the requested behavior.\n")
    (parent / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    return parent, task_directory_digest(parent)


def test_probe_feed_binds_probed_task_and_leaves_others_not_probed(tmp_path: Path):
    root = tmp_path / "repo"
    probed = "format-code-task-000001"
    plain = "format-code-task-000002"
    parents = {}
    for task_id in (probed, plain):
        parents[task_id] = _task_parent(root, task_id)
    _csv(
        root / "ledger.csv",
        [
            "task_id",
            "status",
            "reason",
            "run",
            "run_digest",
            "run_variant_status",
            "verdict",
            "verdict_evidence",
        ],
        [
            {
                "task_id": probed,
                "status": "review",
                "reason": "HAR-161 exploit probe cracked it (fixture)",
                "run": "original",
                "run_digest": parents[probed][1],
                "run_variant_status": "",
                "verdict": "fix",
                "verdict_evidence": "build.py:PROBE_CRACKED#fixture",
            },
            {
                "task_id": plain,
                "status": "usable",
                "reason": "nop sound",
                "run": "original",
                "run_digest": parents[plain][1],
                "run_variant_status": "",
                "verdict": "keep",
                "verdict_evidence": "ledger:status=usable",
            },
        ],
    )
    _csv(
        root / "locked.csv",
        ["task_id", "locked_nop", "note"],
        [
            {"task_id": probed, "locked_nop": "sound", "note": ""},
            {"task_id": plain, "locked_nop": "sound", "note": ""},
        ],
    )
    _csv(
        root / "history.csv",
        ["task_id", "runs", "clean_pass", "copied_pass", "fail", "infra"],
        [
            {
                "task_id": probed,
                "runs": "0",
                "clean_pass": "0",
                "copied_pass": "0",
                "fail": "0",
                "infra": "0",
            },
            {
                "task_id": plain,
                "runs": "0",
                "clean_pass": "0",
                "copied_pass": "0",
                "fail": "0",
                "infra": "0",
            },
        ],
    )
    (root / "pool.json").write_text(
        json.dumps(
            {
                "pool": [
                    {
                        "task_id": task_id,
                        "task_version_digest": digest,
                        "task": f"sources/{task_id}",
                    }
                    for task_id, (_, digest) in ((probed, parents[probed]), (plain, parents[plain]))
                ]
            }
        )
    )
    feed = root / "probe.json"
    feed.write_text(
        json.dumps(
            {
                probed: {
                    "verdict": "cracked",
                    "trial": None,
                    "task_package_digest": parents[probed][1],
                }
            }
        )
    )
    report = generate_health_tags(
        root,
        ledger_path=Path("ledger.csv"),
        locked_nop_path=Path("locked.csv"),
        history_path=Path("history.csv"),
        pool_path=Path("pool.json"),
        exploit_path=Path("probe.json"),
        source_root=root,
        variants_root=Path("derived/variants"),
        view_root=Path("derived/view"),
    )
    by_task = {row["task_id"]: row for row in report["tasks"]}
    cracked = by_task[probed]
    assert cracked["tags"] == ["health:cracked", "solve:never-run", "verdict:fix"]
    assert cracked["exploit"]["verdict"] == "cracked"
    assert cracked["exploit"]["digest_match"] is True
    metadata = tomllib.loads((Path(cracked["package"]) / "task.toml").read_bytes().decode())
    assert metadata["metadata"]["description"] == "HAR-161 exploit probe cracked it (fixture)"
    untouched = by_task[plain]
    assert untouched["tags"] == ["health:sound", "solve:never-run", "verdict:keep"]
    assert untouched["exploit"]["verdict"] == "not-probed"
    assert untouched["exploit"]["digest_match"] is None
    metadata = tomllib.loads((Path(untouched["package"]) / "task.toml").read_bytes().decode())
    assert "description" not in metadata["metadata"]
