"""Contracts for the pinned TB4 v4.0.0 Harbor job compiler.

Tests verify:
- Complete 66-task job plan compilation with flat 8h timeout handling (28800s).
- Deterministic and resumable per-task job identity (stable ULID-compatible derivation).
- Fail-closed validation on task count, missing tasks, unexpected tasks, and upstream digest drift.
- Accidental TB3/TB4 aggregation refusal.
- Permitted Z.ai provider/agent model selection (highspeed and non-Z.ai models refused at compile time).
- Explicit TB3/TB4 non-comparability metadata and refusal of floating refs / unpinned checkouts.
- CLI compilation, JSON output, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import craft

MANIFEST = """\
schema_version = "1.0"

[task]
name = "{name}"
{version}

[metadata]
category = "Test"
{expert}

[verifier]
timeout_sec = 60.0
{mode}
"""


def _tb_task(root: Path, short: str) -> Path:
    """A discoverable Harbor task whose declared name is `terminal-bench/<short>`."""
    d = root / short
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.toml").write_text(
        MANIFEST.format(
            name=f"terminal-bench/{short}",
            version="",
            expert="",
            mode='environment_mode = "separate"',
        ),
        encoding="utf-8",
    )
    (d / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    return d


def _v4_fixture(root: Path, *, dataset: str = 'name = "terminal-bench/terminal-bench"') -> Path:
    """A pinned TB4-shaped fixture: every expected task, a pinned dataset.toml."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dataset.toml").write_text(
        f'[dataset]\n{dataset}\nversion = "4.0.0"\n', encoding="utf-8"
    )
    for ref in craft.load_migration_record()["expected_inventory"]:
        _tb_task(root, ref.split("/", 1)[1])
    return root


def _v3_fixture(root: Path) -> Path:
    """A TB3-shaped fixture with all 74 tasks (the 66 kept plus the 8 removed)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\n', encoding="utf-8"
    )
    record = craft.load_migration_record()
    refs = set(record["expected_inventory"]) | set(record["removed_tasks"])
    for ref in sorted(refs):
        _tb_task(root, ref.split("/", 1)[1])
    return root


def test_compile_tb4_produces_complete_66_task_job_plan(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    out_file = tmp_path / "out" / "tb4-plan.json"
    plan = craft.compile_tb4(v4, out=out_file)

    assert plan["plan_version"] == "tb4-job-plan/1"
    assert plan["command"] == "craft compile"
    assert plan["dataset_ref"] == "terminal-bench/terminal-bench@4.0.0"
    assert plan["source_identity"] == "terminal-bench/terminal-bench@4.0.0"
    assert plan["versions"] == {"from": "3.0.0", "to": "4.0.0"}
    assert plan["pin"]["tag"] == "v4.0.0"
    assert plan["pin"]["commit"] == "452bf30"
    assert plan["pin"]["license"] == "Apache-2.0"
    assert plan["pin"]["schema_unchanged"] is True
    assert plan["timeout_seconds"] == 28_800
    assert plan["task_count"] == 66
    assert plan["non_comparable"] is True
    assert plan["floating_refs_forbidden"] is True
    assert plan["provider"]["agent"] == "zai-opencode"
    assert plan["provider"]["selected_model"] == "zai-coding-plan/glm-5.3"
    assert plan["provider"]["highspeed"] == "refused"
    assert plan["refuses"] == {
        "tb3_mixing": True,
        "floating_refs": True,
        "digest_drift": True,
    }
    assert plan["manifest_digest"].startswith("sha256:")

    tasks = plan["tasks"]
    assert len(tasks) == 66
    expected_inventory = craft.load_migration_record()["expected_inventory"]

    for task_entry, expected_ref in zip(tasks, expected_inventory, strict=True):
        assert task_entry["task_ref"] == expected_ref
        assert len(task_entry["task_id"]) == 26  # valid 26-char Crockford ULID
        assert task_entry["task_digest"].startswith("sha256:")
        assert task_entry["timeout_seconds"] == 28_800  # flat 8h timeout on all 66
        assert task_entry["agent"] == "zai-opencode"
        assert task_entry["model"] == "zai-coding-plan/glm-5.3"

    assert out_file.is_file()
    written_data = json.loads(out_file.read_text(encoding="utf-8"))
    assert written_data["plan_version"] == "tb4-job-plan/1"
    assert len(written_data["tasks"]) == 66


def test_compile_tb4_job_identity_is_deterministic_and_resumable(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    plan1 = craft.compile_tb4(v4)
    plan2 = craft.compile_tb4(v4)

    # Identical task IDs across runs for resumability
    ids1 = [t["task_id"] for t in plan1["tasks"]]
    ids2 = [t["task_id"] for t in plan2["tasks"]]
    assert ids1 == ids2

    # Distinct IDs for all 66 tasks
    assert len(set(ids1)) == 66

    # Function-level stability
    single_id1 = craft.deterministic_tb4_task_id(
        "terminal-bench/terminal-bench@4.0.0", "terminal-bench/atrx-vep-crispr"
    )
    single_id2 = craft.deterministic_tb4_task_id(
        "terminal-bench/terminal-bench@4.0.0", "terminal-bench/atrx-vep-crispr"
    )
    assert single_id1 == single_id2
    assert len(single_id1) == 26


def test_compile_tb4_fails_closed_on_task_count_drift(tmp_path: Path) -> None:
    # 65 tasks (one missing)
    v4_missing = tmp_path / "v4_missing"
    v4_missing.mkdir(parents=True)
    (v4_missing / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\nversion = "4.0.0"\n',
        encoding="utf-8",
    )
    for ref in craft.load_migration_record()["expected_inventory"][:-1]:
        _tb_task(v4_missing, ref.split("/", 1)[1])

    with pytest.raises(ValueError, match="task count drift|missing expected task"):
        craft.compile_tb4(v4_missing)

    # 67 tasks (one extra)
    v4_extra = tmp_path / "v4_extra"
    v4_extra.mkdir(parents=True)
    (v4_extra / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\nversion = "4.0.0"\n',
        encoding="utf-8",
    )
    for ref in craft.load_migration_record()["expected_inventory"]:
        _tb_task(v4_extra, ref.split("/", 1)[1])
    _tb_task(v4_extra, "extra-unregistered-task")

    with pytest.raises(ValueError, match="task count drift|unexpected task"):
        craft.compile_tb4(v4_extra)


def test_compile_tb4_fails_closed_on_inventory_mismatch(tmp_path: Path) -> None:
    # 66 tasks, but one expected task is replaced with an unexpected task
    v4_swapped = tmp_path / "v4_swapped"
    v4_swapped.mkdir(parents=True)
    (v4_swapped / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\nversion = "4.0.0"\n',
        encoding="utf-8",
    )
    inventory = list(craft.load_migration_record()["expected_inventory"])
    for ref in inventory[:-1]:
        _tb_task(v4_swapped, ref.split("/", 1)[1])
    _tb_task(v4_swapped, "foreign-task-substitute")

    with pytest.raises(ValueError, match="inventory drift: missing expected task"):
        craft.compile_tb4(v4_swapped)


def test_compile_tb4_fails_closed_on_upstream_digest_drift(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    out_file = tmp_path / "plan.json"

    # Initial compile creates baseline snapshot
    craft.compile_tb4(v4, out=out_file)
    assert out_file.is_file()

    # Recompiling identical fixture succeeds
    craft.compile_tb4(v4, out=out_file)

    # Mutate one task's instruction.md -> changes task_digest
    first_task_short = craft.load_migration_record()["expected_inventory"][0].split("/", 1)[1]
    (v4 / first_task_short / "instruction.md").write_text("Mutated task instruction content.\n")

    # Recompiling against prior plan must fail closed on digest drift
    with pytest.raises(ValueError, match="upstream digest drift detected"):
        craft.compile_tb4(v4, out=out_file)


def test_compile_tb4_refuses_accidental_tb3_aggregation(tmp_path: Path) -> None:
    v3 = _v3_fixture(tmp_path / "v3")
    v4 = _v4_fixture(tmp_path / "v4")

    with pytest.raises(ValueError, match="accidental TB3/TB4 aggregation refused"):
        craft.compile_tb4(v4, tb3_path=v3)


def test_compile_tb4_provider_and_model_selection(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")

    # Permitted flash model
    plan_flash = craft.compile_tb4(v4, model="zai-coding-plan/glm-5.3-flash")
    assert plan_flash["provider"]["selected_model"] == "zai-coding-plan/glm-5.3-flash"
    assert all(t["model"] == "zai-coding-plan/glm-5.3-flash" for t in plan_flash["tasks"])

    # Refuse highspeed model
    with pytest.raises(ValueError, match="invalid model selector|highspeed"):
        craft.compile_tb4(v4, model="zai-coding-plan/glm-5.3-highspeed")

    with pytest.raises(ValueError, match="invalid model selector|highspeed"):
        craft.compile_tb4(v4, model="glm-5.3-highspeed")

    # Refuse non-Z.ai models
    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model="openai/gpt-4o")

    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model="deepseek/deepseek-v4-flash")


def test_compile_tb4_refuses_floating_refs_and_unpinned_checkouts(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")

    # Floating ref refused
    for floating in ("terminal-bench/terminal-bench@latest", "latest", "@head"):
        with pytest.raises(ValueError, match="floating"):
            craft.compile_tb4(v4, ref=floating)

    # Wrong version refused
    with pytest.raises(ValueError, match="wrong version"):
        craft.compile_tb4(v4, ref="terminal-bench/terminal-bench@3.0.0")

    # Wrong dataset name refused
    v4_wrong_ds = _v4_fixture(tmp_path / "v4_wrong_ds", dataset='name = "wrong/dataset"')
    with pytest.raises(ValueError, match="wrong dataset"):
        craft.compile_tb4(v4_wrong_ds)


def test_compile_tb4_cli_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    out_file = tmp_path / "cli-plan.json"

    # Successful compile via CLI with --json
    code = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--out",
            str(out_file),
            "--model",
            "zai-coding-plan/glm-5.3",
            "--json",
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    data = json.loads(stdout)
    assert data["plan_version"] == "tb4-job-plan/1"
    assert data["task_count"] == 66
    assert out_file.is_file()

    # Successful compile via CLI with plain text summary
    code_text = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--out",
            str(out_file),
        ]
    )
    assert code_text == 0
    text_out = capsys.readouterr().out
    assert "craft compile" in text_out
    assert "66 tasks, flat timeout 28800s (8h)" in text_out

    # CLI refuses TB3 aggregation flag
    v3 = _v3_fixture(tmp_path / "v3")
    code_agg = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--tb3-root",
            str(v3),
        ]
    )
    assert code_agg == 2
    err_agg = capsys.readouterr().err
    assert "accidental TB3/TB4 aggregation refused" in err_agg

    # CLI refuses highspeed model selector
    code_highspeed = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--model",
            "zai-coding-plan/glm-5.3-highspeed",
        ]
    )
    assert code_highspeed == 2
    err_highspeed = capsys.readouterr().err
    assert "invalid model selector" in err_highspeed
