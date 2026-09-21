"""Contracts for the pinned TB4 v4.0.0 Harbor job compiler.

Tests verify:
- Complete 66-task job plan compilation with flat 8h timeout handling (28800s).
- Deterministic and resumable per-task job identity (stable ULID-compatible derivation).
- Fail-closed validation on task count, missing tasks, unexpected tasks, and upstream digest drift.
- Accidental TB3/TB4 aggregation refusal.
- Permitted Z.ai and DeepSeek model/agent pairs, including the official selector cutover.
- Explicit TB3/TB4 non-comparability metadata and refusal of floating refs / unpinned checkouts.
- CLI compilation, JSON output, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import craft
from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR

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
    assert plan["selected_task_count"] == 66
    assert plan["non_comparable"] is True
    assert plan["floating_refs_forbidden"] is True
    assert plan["provider"]["agent"] == "zai-opencode"
    assert plan["provider"]["selected_model"] == "zai-coding-plan/glm-5.3"
    assert plan["provider"]["highspeed"] == "refused"
    assert plan["refuses"] == {
        "tb3_mixing": True,
        "floating_refs": True,
        "digest_drift": True,
        "gpu_on_local_docker": True,
    }
    routing = plan["environment_routing"]
    assert routing["default"] == "docker"
    assert routing["remote_for_gpu"] == "modal"
    assert routing["gpu_task_refs"] == [
        "terminal-bench/fp8-rmsnorm-gemm",
        "terminal-bench/jax-speedrun-gpu",
        "terminal-bench/math-eval-grader",
    ]
    assert routing["required_credentials"]["modal"] == ["MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"]
    assert "zai/glm-5.3-flash" in plan["provider"]["allowed_models"]
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


@pytest.mark.parametrize(
    "baseline",
    ["full", "full-to-subset", "subset", "narrowed", "legacy-full"],
)
def test_compile_tb4_fails_closed_on_upstream_digest_drift(
    tmp_path: Path, baseline: str
) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    out_file = tmp_path / "plan.json"
    inventory = craft.load_migration_record()["expected_inventory"]
    selected = [inventory[0]]

    # Exercise full and subset baselines, including a full plan overwritten
    # with a subset before a later drift check.
    initial_selection = selected if baseline == "subset" else None
    craft.compile_tb4(v4, out=out_file, include_tasks=initial_selection)
    if baseline == "narrowed":
        craft.compile_tb4(v4, out=out_file, include_tasks=selected)
    elif baseline == "legacy-full":
        prior = json.loads(out_file.read_text(encoding="utf-8"))
        del prior["task_digests"]
        out_file.write_text(json.dumps(prior), encoding="utf-8")

    receipt = out_file.read_bytes()
    drifted_ref = inventory[1]
    drifted_short = drifted_ref.split("/", 1)[1]
    (v4 / drifted_short / "instruction.md").write_text(
        "Mutated unselected task instruction content.\n", encoding="utf-8"
    )

    # Filtering emitted jobs must never filter the pinned digest comparison.
    with pytest.raises(ValueError, match="upstream digest drift detected") as error:
        craft.compile_tb4(
            v4, out=out_file, include_tasks=None if baseline == "full" else selected
        )
    assert drifted_ref in str(error.value)
    assert out_file.read_bytes() == receipt


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
    assert plan_flash["provider"]["provider_family"] == "zai"
    assert all(t["model"] == "zai-coding-plan/glm-5.3-flash" for t in plan_flash["tasks"])

    # Refuse highspeed model
    with pytest.raises(ValueError, match="invalid model selector|highspeed"):
        craft.compile_tb4(v4, model="zai-coding-plan/glm-5.3-highspeed")

    with pytest.raises(ValueError, match="invalid model selector|highspeed"):
        craft.compile_tb4(v4, model="glm-5.3-highspeed")

    # Refuse non-Z.ai models
    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model="openai/gpt-4o")

    # DeepSeek model with default zai-opencode agent is refused
    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model=DEEPSEEK_MODEL_SELECTOR)

    # DeepSeek model with mini-swe-agent is permitted
    plan_ds_mini = craft.compile_tb4(
        v4, model=DEEPSEEK_MODEL_SELECTOR, agent="mini-swe-agent"
    )
    assert plan_ds_mini["provider"]["provider_family"] == "deepseek"
    assert plan_ds_mini["provider"]["selected_agent"] == "mini-swe-agent"
    assert plan_ds_mini["provider"]["selected_model"] == DEEPSEEK_MODEL_SELECTOR
    assert all(t["model"] == DEEPSEEK_MODEL_SELECTOR for t in plan_ds_mini["tasks"])
    assert all(t["agent"] == "mini-swe-agent" for t in plan_ds_mini["tasks"])

    # DeepSeek model with DSH agent is permitted
    plan_ds_dsh = craft.compile_tb4(
        v4,
        model=DEEPSEEK_MODEL_SELECTOR,
        agent="evallab.harbor_dsh:DeepSeekHarnessAgent",
    )
    assert plan_ds_dsh["provider"]["provider_family"] == "deepseek"
    assert plan_ds_dsh["provider"]["selected_agent"] == "evallab.harbor_dsh:DeepSeekHarnessAgent"

    # DeepSeek model with unknown agent is refused
    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model=DEEPSEEK_MODEL_SELECTOR, agent="codex")


def test_compile_tb4_official_selector_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate PR409's imported constant without changing any runtime or study.
    official_selector = "deepseek/deepseek-flash"
    monkeypatch.setattr(craft, "DEEPSEEK_MODEL_SELECTOR", official_selector)
    v4 = _v4_fixture(tmp_path / "v4")
    selected = [craft.load_migration_record()["expected_inventory"][0]]
    for agent in ("mini-swe-agent", "evallab.harbor_dsh:DeepSeekHarnessAgent"):
        plan = craft.compile_tb4(
            v4, model=official_selector, agent=agent, include_tasks=selected
        )
        assert plan["provider"]["provider_family"] == "deepseek"
        assert plan["tasks"][0]["model"] == official_selector
        assert plan["tasks"][0]["agent"] == agent
        assert [official_selector, agent] in plan["provider"]["allowed_pairs"]
        with pytest.raises(ValueError, match="invalid model selector"):
            craft.compile_tb4(
                v4, model="deepseek/deepseek-v4-flash", agent=agent, include_tasks=selected
            )


def test_compile_tb4_include_tasks_subset(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    inventory = craft.load_migration_record()["expected_inventory"]
    # Pick two tasks: first as short name, second as full ref
    first_short = inventory[0].split("/", 1)[1]
    second_full = inventory[1]

    # Full compile for manifest digest baseline
    full_plan = craft.compile_tb4(v4)

    # Subset compile mixing short name and full ref
    subset_plan = craft.compile_tb4(
        v4,
        include_tasks=[first_short, second_full],
    )

    assert subset_plan["task_count"] == 66
    assert subset_plan["selected_task_count"] == 2
    assert len(subset_plan["tasks"]) == 2
    assert subset_plan["manifest_digest"] == full_plan["manifest_digest"]

    # Verify task entries order and fields
    assert subset_plan["tasks"][0]["task_ref"] == inventory[0]
    assert subset_plan["tasks"][1]["task_ref"] == inventory[1]
    for task_entry in subset_plan["tasks"]:
        assert len(task_entry["task_id"]) == 26
        assert task_entry["task_digest"].startswith("sha256:")
        assert task_entry["timeout_seconds"] == 28_800
        assert task_entry["agent"] == "zai-opencode"
        assert task_entry["model"] == "zai-coding-plan/glm-5.3"


    # Changing output selection preserves the full baseline for later resumes.
    out_file = tmp_path / "subset-plan.json"
    craft.compile_tb4(v4, out=out_file, include_tasks=[first_short])
    expanded = craft.compile_tb4(v4, out=out_file, include_tasks=[second_full])
    assert [task["task_ref"] for task in expanded["tasks"]] == [second_full]
    restored = craft.compile_tb4(v4, out=out_file)
    assert [task["task_ref"] for task in restored["tasks"]] == inventory

    # Historical subset-only receipts cannot prove the unselected task bytes.
    craft.compile_tb4(v4, out=out_file, include_tasks=[first_short])
    prior = json.loads(out_file.read_text(encoding="utf-8"))
    del prior["task_digests"]
    out_file.write_text(json.dumps(prior), encoding="utf-8")
    receipt = out_file.read_bytes()
    with pytest.raises(ValueError, match="upstream task set drift"):
        craft.compile_tb4(v4, out=out_file, include_tasks=[first_short])
    assert out_file.read_bytes() == receipt
    # Unknown task name raises ValueError naming the unknown entry
    with pytest.raises(ValueError, match="no-such-task"):
        craft.compile_tb4(v4, include_tasks=["no-such-task"])

    # 66-task inventory guard still fires under a subset request
    v4_missing = tmp_path / "v4_missing"
    v4_missing.mkdir(parents=True)
    (v4_missing / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\nversion = "4.0.0"\n',
        encoding="utf-8",
    )
    for ref in inventory[:-1]:
        _tb_task(v4_missing, ref.split("/", 1)[1])

    with pytest.raises(ValueError, match="task count drift|missing expected task"):
        craft.compile_tb4(v4_missing, include_tasks=[first_short])


@pytest.mark.parametrize("subset", [False, True])
def test_compile_tb4_refuses_floating_refs_and_unpinned_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subset: bool
) -> None:
    v4 = _v4_fixture(tmp_path / "v4")
    selected = [craft.load_migration_record()["expected_inventory"][0]] if subset else None

    # Floating ref refused
    for floating in ("terminal-bench/terminal-bench@latest", "latest", "@head"):
        with pytest.raises(ValueError, match="floating"):
            craft.compile_tb4(v4, ref=floating, include_tasks=selected)

    # Wrong version refused
    with pytest.raises(ValueError, match="wrong version"):
        craft.compile_tb4(
            v4, ref="terminal-bench/terminal-bench@3.0.0", include_tasks=selected
        )

    # Wrong dataset name refused
    v4_wrong_ds = _v4_fixture(tmp_path / "v4_wrong_ds", dataset='name = "wrong/dataset"')
    with pytest.raises(ValueError, match="wrong dataset"):
        craft.compile_tb4(v4_wrong_ds, include_tasks=selected)

    # A checkout without a declared version or matching Git pin stays refused.
    (v4 / "dataset.toml").write_text(
        '[dataset]\nname = "terminal-bench/terminal-bench"\n', encoding="utf-8"
    )
    monkeypatch.setattr(craft, "_git_pin_ref", lambda root: None)
    with pytest.raises(ValueError, match="unpinned Terminal-Bench checkout"):
        craft.compile_tb4(v4, include_tasks=selected)


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
    assert data["selected_task_count"] == 66
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

    # CLI compile with --include-task
    inventory = craft.load_migration_record()["expected_inventory"]
    short_task = inventory[0].split("/", 1)[1]
    full_task = inventory[1]
    subset_out = tmp_path / "cli-subset-plan.json"
    code_subset = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--out",
            str(subset_out),
            "--include-task",
            short_task,
            "--include-task",
            full_task,
            "--json",
        ]
    )
    assert code_subset == 0
    stdout_subset = capsys.readouterr().out
    data_subset = json.loads(stdout_subset)
    assert data_subset["task_count"] == 66
    assert data_subset["selected_task_count"] == 2
    assert len(data_subset["tasks"]) == 2

    # CLI rejects unknown --include-task with exit 2 and error text naming task
    code_unknown = craft.main(
        [
            "compile",
            "--tb4-root",
            str(v4),
            "--include-task",
            "non-existent-task-ref",
        ]
    )
    assert code_unknown == 2
    err_unknown = capsys.readouterr().err
    assert "non-existent-task-ref" in err_unknown

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


def test_compile_tb4_zai_openapi_mini_swe_pair_and_routing(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")

    # zai-openapi provider family: mini-SWE-agent + GLM-5.3-Flash standard API
    plan = craft.compile_tb4(
        v4,
        model="zai/glm-5.3-flash",
        agent="mini-swe-agent",
    )
    assert plan["provider"]["provider_family"] == "zai-openapi"
    assert plan["provider"]["model_prefix"] == "zai/"
    assert ["zai/glm-5.3-flash", "mini-swe-agent"] in plan["provider"]["allowed_pairs"]

    # GPU tasks carry the remote environment + gpu_types; CPU tasks stay local
    gpu_entries = [t for t in plan["tasks"] if t["task_ref"] in craft.TB4_GPU_TASK_REFS]
    cpu_entries = [t for t in plan["tasks"] if t["task_ref"] not in craft.TB4_GPU_TASK_REFS]
    assert len(gpu_entries) == 3
    assert all(t["environment"] == "modal" and t["gpu_types"] == ["H100"] for t in gpu_entries)
    assert all(t["environment"] == "docker" and "gpu_types" not in t for t in cpu_entries)

    # The pair is refused with the wrong agent
    with pytest.raises(ValueError, match="invalid model selector"):
        craft.compile_tb4(v4, model="zai/glm-5.3-flash", agent="zai-opencode")

    # Unknown remote backend refused
    with pytest.raises(ValueError, match="invalid remote_environment"):
        craft.compile_tb4(v4, remote_environment="skynet")


def test_compile_tb4_docker_refuses_gpu_tasks_instead_of_degrading(tmp_path: Path) -> None:
    v4 = _v4_fixture(tmp_path / "v4")

    # Full cohort on local docker: GPU class must fail closed, never silently run
    with pytest.raises(ValueError, match="no CUDA on this host"):
        craft.compile_tb4(v4, remote_environment="docker")

    # CPU-only selection on local docker is fine
    inventory = craft.load_migration_record()["expected_inventory"]
    cpu_only = [r.split("/", 1)[1] for r in inventory if r not in craft.TB4_GPU_TASK_REFS]
    plan_cpu = craft.compile_tb4(
        v4, remote_environment="docker", include_tasks=cpu_only[:3]
    )
    assert plan_cpu["selected_task_count"] == 3
    assert all(t["environment"] == "docker" for t in plan_cpu["tasks"])

    # Beam alternate backend routing is represented
    plan_beam = craft.compile_tb4(v4, remote_environment="beam")
    assert plan_beam["environment_routing"]["remote_for_gpu"] == "beam"
    gpu_beam = [t for t in plan_beam["tasks"] if t["task_ref"] in craft.TB4_GPU_TASK_REFS]
    assert all(t["environment"] == "beam" for t in gpu_beam)


def test_compile_tb4_real_checkout_shape_no_dataset_toml_git_pinned(tmp_path: Path) -> None:
    """Upstream checkouts ship no dataset.toml and carry archive/ tasks.

    The exact v4.0.0 git pin establishes dataset identity on its own, and
    discovery scopes to tasks/ so archived tasks stay out of the 66.
    """
    import subprocess

    root = tmp_path / "tb4-real"
    tasks = root / "tasks"
    for ref in craft.load_migration_record()["expected_inventory"]:
        _tb_task(tasks, ref.split("/", 1)[1])
    # An archived task that must NOT be counted.
    _tb_task(root / "archive", "gpt2-codegolf-old")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "tag", craft.TB4_PIN_TAG], check=True)

    plan = craft.compile_tb4(root)
    assert plan["task_count"] == 66
    assert plan["selected_task_count"] == 66
    assert plan["dataset_ref"] == craft.TB4_DATASET_REF
    assert all("gpt2-codegolf-old" not in t["task_ref"] for t in plan["tasks"])

    # Unpinned + undeclared still refuses (fail-closed preserved).
    unpinned = tmp_path / "tb4-unpinned"
    unpinned.mkdir(parents=True)
    for ref in craft.load_migration_record()["expected_inventory"]:
        _tb_task(unpinned, ref.split("/", 1)[1])
    with pytest.raises(ValueError, match="not a Terminal-Bench checkout"):
        craft.compile_tb4(unpinned)
