"""The deploy preflight stages only clean, loadable files and never touches anything else."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.production import deploy_preflight
from evallab.production.deploy_preflight import run_preflight

PRIVATE = "annotation wording that must never reach the volume"


def _facet(run_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "source": "synthetic-source", "split": "DEV", "facet_version": "run-facets/v1",
        "facet_model": "provider/facet-model", "input_tokens": 900, "output_tokens": 120,
        "goal": "Make the sample pass.", "approach": "Edited and ran checks.", "outcome": "pass",
        "tests": "read", "grader_or_harness_touched": False, "answer_material_accessed": False,
        "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
        "suspicion": 0, "evidence_steps": [0],
    } | changes


def _jsonl(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows), encoding="utf-8")


def _json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _inventory() -> dict[str, Any]:
    return {
        "schema": "evallab.ts9_defensive_inventory/v1", "observed_at": "2026-10-28T04:03:55+00:00",
        "source_revision": "837dc7c",
        "tasks": [
            {
                "task_id": "task-a", "dataset_id": "tb4-audit-v1", "dev_assignments": [["tb4-audit-v1", "dev"]],
                "key_file": "/private/answer-keys/key.json",
                "annotations": [{"defect_type": "weak_check", "weak_check": PRIVATE, "key_id": "pm-tb4:secret"}],
            }
        ],
        "selected_repair_deliveries": [],
    }


def _registry() -> dict[str, Any]:
    entry = {
        "id": "set-a", "name": "org/set-a", "source_url": "https://huggingface.co/datasets/org/a",
        "bundle_path": "~/.local/state/evallab-cross-test/bundle-a", "unit": "run",
        "flags": {
            "eval_only": True, "local_only": True, "no_training": True, "no_rehosting": True,
            "uploadable_to_vendor_projects": False, "hosted_model_api_for_evaluation": "needs_peter",
            "basis": PRIVATE,
        },
        "licence": {"text": "mit", "redistributable": True},
        "labels": {"type": "human verdict", "counts": {"rows": 5}},
        "scoring": {"command_level_localization_scorable": False},
        "adapter": {"merge_state": "merged", "pr": 1273}, "caveats": [PRIVATE],
    }
    return {
        "schema_version": "cross-test-registry/v1", "created": "2026-10-28",
        "validator": {"script": "bridge/validate.py"}, "entries": [entry], "hold_access": [],
    }


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "state"
    _jsonl(state / "run-atlas/facets-v1.jsonl", [_facet("r1"), _facet("r2", outcome="fail")])
    _json(
        state / "run-atlas/field-reliability-v1.json",
        {
            "schema": "evallab.field_reliability/v1", "measured_at": "2026-10-28T03:42:26+00:00",
            "source": "VF-11 blind re-rate", "design": "5 runs per cluster", "sample_runs": 60,
            "fields": [{"field": "goal", "agree_n": 35, "n": 40, "basis": None}],
        },
    )
    _json(state / "trace-tools/tt1-scorecard.json", {"schema": "evallab.trace_tool_scorecard/v1", "label_sets": {}})
    _json(
        state / "run-atlas/blinding-refresh-v1.json",
        {"schema": "run-atlas-blinding-refresh/v1", "reason": "widened redaction", "run_ids": ["r1"]},
    )
    _jsonl(
        state / "trace-tools/results-v1.jsonl",
        [{"schema": "x", "tool": "scout", "run_id": "r1", "source": "cloud-trace-db", "flag": True,
          "at": "2026-10-28T01:00:00+00:00", "detector": "d", "detector_version": "1", "evidence_steps": [], "value": {}}],
    )
    inventory, registry = tmp_path / "inventory.json", tmp_path / "registry.json"
    _json(inventory, _inventory())
    _json(registry, _registry())
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"state": state, "inventory": inventory, "registry": registry, "repo": repo, "tmp": tmp_path}


def _run(world: dict[str, Path], **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "repo_root": world["repo"], "inventory": world["inventory"], "registry": world["registry"],
    } | overrides
    return run_preflight(world["state"], **arguments)


def _by_key(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in report["files"]}


def test_a_clean_state_is_staged_and_every_required_tab_is_ready(world: dict[str, Path]) -> None:
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    files = _by_key(report)
    assert report["ok"] is True and report["blockers"] == []
    for key in ("facets", "field_reliability", "tool_scorecard", "results", "repair_inventory", "cross_test_registry"):
        assert files[key]["status"] == "ok", files[key]
        assert (stage / files[key]["remote"]).is_file()
    assert all(tab["ready"] for tab in report["tabs"])
    by_tab = {tab["tab"]: tab for tab in report["tabs"]}
    assert by_tab["Run Atlas"]["degraded_without"] == ["clusters"] and by_tab["Run Atlas"]["needs_database"] is True
    assert by_tab["Field reliability"]["needs_database"] is False
    source = world["state"] / "run-atlas/facets-v1.jsonl"
    assert files["facets"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (stage / "run-atlas/facets-v1.jsonl").read_bytes() == source.read_bytes()


def test_commands_are_printed_never_run_and_never_force_an_overwrite(world: dict[str, Path]) -> None:
    stage = world["tmp"] / "stage"
    commands = _run(world, stage_directory=stage)["commands"]
    assert commands["run"] is False
    assert commands["deploy"].endswith("modal deploy --strategy recreate src/evallab/production/modal_cloud.py")
    assert f"modal volume put {deploy_preflight.VOLUME_NAME} {stage}/run-atlas/facets-v1.jsonl /run-atlas/facets-v1.jsonl" in commands["volume_put"]
    assert all("--force" not in command for command in commands["volume_put"])
    temporary = _run(world)
    assert temporary["stage_directory"] is None
    assert all("<STAGE_DIR>/" in command for command in temporary["commands"]["volume_put"])


def test_missing_required_files_block_and_missing_optional_ones_only_degrade(world: dict[str, Path]) -> None:
    (world["state"] / "trace-tools/results-v1.jsonl").unlink()
    report = _run(world)
    assert report["ok"] is False
    assert any(blocker.startswith("results: missing") for blocker in report["blockers"])
    tabs = {tab["tab"]: tab for tab in report["tabs"]}
    assert tabs["Trace Tools"]["ready"] is False and tabs["Trace Tools"]["missing"] == ["results"]
    assert tabs["Task Evidence"]["missing"] == ["results"]
    assert tabs["Run Atlas"]["ready"] is True
    assert any(warning.startswith("clusters: missing") for warning in report["warnings"])
    (world["state"] / "run-atlas/blinding-refresh-v1.json").unlink()
    report = _run(world)
    tabs = {tab["tab"]: tab for tab in report["tabs"]}
    assert tabs["Run Atlas"]["missing"] == ["blinding_refresh"] and tabs["Coverage"]["missing"] == ["blinding_refresh"]


@pytest.mark.parametrize(
    ("name", "rows", "needle"),
    [
        ("trace-tools/results-v1.jsonl", [{"tool": "scout", "run_id": "r1", "value": {"reward": 1.0}}], "reward"),
        ("run-atlas/facets-v1.jsonl", [_facet("r1") | {"is_exploit": True}], "is_exploit"),
        ("run-atlas/facets-v1.jsonl", [_facet("r1") | {"nested": {"labels": ["x"]}}], "labels"),
        ("run-atlas/facets-v1.jsonl", [_facet("r1") | {"cheatbench_label": 1}], "cheatbench_label"),
        ("run-atlas/facets-v1.jsonl", [_facet("r1") | {"HackBench-Labels": 1}], "HackBench-Labels"),
    ],
)
def test_a_file_with_a_per_run_label_or_reward_field_is_refused_and_not_staged(
    world: dict[str, Path], name: str, rows: list[Any], needle: str
) -> None:
    _jsonl(world["state"] / name, rows)
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    row = next(item for item in report["files"] if item["remote"] == name)
    assert row["status"] == "refused" and needle in row["detail"]
    assert not (stage / name).exists() and report["ok"] is False


def test_aggregate_label_counts_are_allowed_but_per_run_fields_in_a_scorecard_are_not(world: dict[str, Path]) -> None:
    scorecard = world["state"] / "trace-tools/tt1-scorecard.json"
    _json(scorecard, {"schema": "evallab.trace_tool_scorecard/v1", "label_sets": {}, "label_counts": {"a": {"positive": 3}}})
    assert _by_key(_run(world))["tool_scorecard"]["status"] == "ok"
    _json(scorecard, {"schema": "evallab.trace_tool_scorecard/v1", "label_sets": {}, "rows": [{"is_exploit": True}]})
    refused = _by_key(_run(world))["tool_scorecard"]
    assert refused["status"] == "refused" and "is_exploit" in refused["detail"]


def test_oversize_unparseable_symlinked_and_loader_rejected_files_are_not_staged(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = world["state"]
    (state / "run-atlas/field-reliability-v1.json").write_text("{broken", encoding="utf-8")
    real = state / "trace-tools/real-results.jsonl"
    (state / "trace-tools/results-v1.jsonl").rename(real)
    os.symlink(real, state / "trace-tools/results-v1.jsonl")
    _jsonl(state / "run-atlas/facets-v1.jsonl", ["not json", "{}"])
    files = _by_key(_run(world))
    assert files["field_reliability"]["status"] == "invalid"
    assert files["results"]["status"] == "missing"  # a symlink is not a regular file
    assert files["facets"]["status"] == "invalid" and "loader rejects" in files["facets"]["detail"]
    monkeypatch.setattr(deploy_preflight, "ITEMS", tuple(
        item if item.key != "tool_scorecard" else deploy_preflight.Item(**{**item.__dict__, "limit": 5}) for item in deploy_preflight.ITEMS
    ))
    assert _by_key(_run(world))["tool_scorecard"]["status"] == "invalid"


def test_the_reduced_inventory_and_registry_carry_no_wording_keys_or_paths(world: dict[str, Path]) -> None:
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    staged = (stage / "repair-board/inventory.json").read_text() + (stage / "cross-test/registry.json").read_text()
    for private in (PRIVATE, "pm-tb4:secret", "key.json", "bundle-a", "bundle_path", "caveats"):
        assert private not in staged
    assert _by_key(report)["repair_inventory"]["status"] == "ok" and "task-a" in staged and "set-a" in staged


def test_a_malformed_registry_entry_blocks_instead_of_being_staged_partially(world: dict[str, Path]) -> None:
    registry = _registry()
    registry["entries"][0]["flags"]["uploadable_to_vendor_projects"] = True
    _json(world["registry"], registry)
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    row = _by_key(report)["cross_test_registry"]
    assert row["status"] == "invalid" and "malformed" in row["detail"] and not (stage / "cross-test").exists()


def test_never_copy_files_are_reported_and_never_staged_and_the_database_is_not_opened(world: dict[str, Path]) -> None:
    state = world["state"]
    for name in (
        "run-atlas/facet-calls-v1.jsonl", "trace-tools/keymap-v1.jsonl", "trace-tools/cascade-own.jsonl",
        "run-atlas/watch-manifest.json", "prod.sqlite",
    ):
        path = state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PRIVATE, encoding="utf-8")
    (state / "cloud-trace-db.sqlite").write_bytes(b"not a database" * 100)
    (state / "evidence").mkdir()
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    present = set(report["never_copy"]["present_in_state_root_not_staged"])
    assert {
        "cloud-trace-db.sqlite", "prod.sqlite", "evidence", "run-atlas/facet-calls-v1.jsonl",
        "trace-tools/keymap-v1.jsonl", "trace-tools/cascade-own.jsonl", "run-atlas/watch-manifest.json",
    } <= present
    staged_names = {str(path.relative_to(stage)) for path in stage.rglob("*") if path.is_file()}
    assert staged_names == {row["remote"] for row in report["files"] if row["status"] == "ok"}
    assert not any(PRIVATE in (stage / name).read_text(errors="ignore") for name in staged_names)
    assert report["database"] == {**report["database"], "copied": False, "local_bytes": 1400}
    assert report["ok"] is True


def test_sources_are_never_modified_and_a_used_stage_directory_is_refused(world: dict[str, Path]) -> None:
    before = {path: path.read_bytes() for path in world["state"].rglob("*") if path.is_file()}
    stage = world["tmp"] / "stage"
    _run(world, stage_directory=stage)
    assert {path: path.read_bytes() for path in world["state"].rglob("*") if path.is_file()} == before
    with pytest.raises(ValueError, match="not empty"):
        _run(world, stage_directory=stage)


def test_the_cli_prints_the_report_and_exits_nonzero_only_when_a_required_file_blocks(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    arguments = [
        "prod", "deploy-preflight", "--state-root", str(world["state"]),
        "--inventory", str(world["inventory"]), "--registry", str(world["registry"]),
    ]
    assert cli.run_cli(arguments, workspace=world["repo"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["command"] == "deploy-preflight" and report["ok"] is True and report["commands"]["run"] is False
    (world["state"] / "trace-tools/results-v1.jsonl").unlink()
    assert cli.run_cli(arguments, workspace=world["repo"]) == 1
    assert json.loads(capsys.readouterr().out)["blockers"]


def _scope_cell(**extra: Any) -> dict[str, Any]:
    return {
        "detector": "cascade", "cohort": "hackbench_natural", "measure": "exact_command", "hits": 1,
        "version": "window-cascade/v7", "note": "v7-to-v8 compatibility checked in the receipt",
        "positives": 41, "route": "judge", "recorded_at": "2026-10-28T00:54:07+00:00", "date_basis": "freeze", "source": "receipt", **extra,
    }


def test_the_detector_scope_is_staged_when_valid_and_rejected_when_it_carries_a_record(
    world: dict[str, Path],
) -> None:
    path = world["state"] / "trace-tools/detector-scope-v1.json"
    _json(path, {"schema": "evallab.detector_scope/v1", "cells": [_scope_cell()]})
    stage = world["tmp"] / "stage"
    report = _run(world, stage_directory=stage)
    assert _by_key(report)["detector_scope"]["status"] == "ok"
    assert (stage / "trace-tools/detector-scope-v1.json").is_file()

    _json(path, {"schema": "evallab.detector_scope/v1", "cells": [_scope_cell(run_id="r-secret")]})
    rejected = _by_key(_run(world, stage_directory=world["tmp"] / "stage2"))["detector_scope"]
    assert rejected["status"] == "invalid" and "cloud loader rejects" in rejected["detail"]
    assert not (world["tmp"] / "stage2/trace-tools/detector-scope-v1.json").exists()

    path.unlink()
    absent = _run(world)
    assert absent["ok"] is True and any(warning.startswith("detector_scope: missing") for warning in absent["warnings"])
