"""HAR-177 verdict plumbing: ledger verdicts, verdict tags, probe binding."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from evallab.task_health_tags import (
    KNOWN_TAGS,
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


def test_network_verifier_is_discarded_and_other_oracle_labels_are_not():
    mod = _BUILD_MOD
    row = {"status": "usable", "reason": "nop sound"}
    label = {
        "label": "oracle:fail-network",
        "evidence": "research/experiments/python-task-ledger/oracle-pilot/000552-locked.json",
        "run_digest": "sha256:abc",
    }
    mod.apply_network_discard(row, label)
    assert row["status"] == "discarded"
    assert "000552-locked.json:oracle:fail-network" in row["reason"]
    assert "allow-list rejected" in row["reason"]
    assert mod.verdict_for(row["status"], "format-code-task-000552", _strip())[0] == "discard"
    untouched = {"status": "usable", "reason": "nop sound"}
    mod.apply_network_discard(
        untouched,
        {"label": "oracle:none", "evidence": "research/experiments/python-task-ledger/oracle-pilot/001198.json"},
    )
    assert untouched["status"] == "usable"


def test_validated_purge_replaces_the_run_and_clears_the_bound_defect():
    mod = _BUILD_MOD
    defect = "sha256:" + "a" * 64
    repaired = "sha256:" + "b" * 64
    record = {
        "transform": "purge-installed-copies@1",
        "status": "validated",
        "variant_digest": repaired,
        "created_at": "2026-10-07T00:00:00",
        "inputs": {"repairs_digest": defect},
    }
    path = mod.ROOT / "library/task-variants/fixture/purge.json"
    row = {
        "status": "usable",
        "reason": "nop sound",
        "run": "leak-closed",
        "run_digest": defect,
        "run_transform": "leak-close-pypi@1",
        "run_variant_status": "validated",
        "evidence": "census",
    }
    chosen = mod.apply_validated_purge(row, [(record, path)])
    assert chosen is not None
    assert row["run_digest"] == repaired
    assert row["run"] == "repair"
    assert row["status"] == "usable"
    assert mod.purge_repairs([(record, path)], repaired, defect)
    assert not mod.purge_repairs([(record, path)], repaired, "sha256:" + "c" * 64)
    candidate = dict(record, status="candidate", variant_digest="sha256:" + "d" * 64)
    stayed = dict(row, run_digest=defect, run="leak-closed")
    assert mod.apply_validated_purge(stayed, [(candidate, path)]) is None
    assert stayed["run_digest"] == defect


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


