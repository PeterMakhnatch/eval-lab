"""Behavioural tests for the agent-network-none@1 transform."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from evallab.agent_network_policy import (
    AGENT_NETWORK_MODE,
    TASK_TOML_REL,
    TRANSFORM_ID,
    build_changes,
    build_task_toml,
    derive_agent_network_none,
)
from evallab.task_variants import VariantInvalid

#: Minimal clean-package task.toml shape: public environment baseline,
#: separate verifier without its own environment table (what
#: separate-verifier@3 declares), plain [agent] table.
PARENT_TASK_TOML = """\
schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/format-code-task-002552"

[agent]
timeout_sec = 3600.0

[verifier]
environment_mode = "separate"
timeout_sec = 2100.0

[environment]
docker_image = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:deadbeef"
workdir = "/testbed"
network_mode = "public"
"""


def test_transform_id_shape() -> None:
    assert TRANSFORM_ID == "agent-network-none@1"
    assert AGENT_NETWORK_MODE == "no-network"


def test_override_lands_in_agent_table_only() -> None:
    new_text = build_task_toml(PARENT_TASK_TOML)
    config = tomllib.loads(new_text)
    assert config["agent"]["network_mode"] == "no-network"
    assert config["agent"]["timeout_sec"] == 3600.0
    # Verifier and environment declarations are byte-identical in effect:
    # no phase override, no verifier environment table, public baseline.
    assert "network_mode" not in config["verifier"]
    assert "environment" not in config["verifier"]
    assert config["environment"]["network_mode"] == "public"


def test_second_application_refused() -> None:
    once = build_task_toml(PARENT_TASK_TOML)
    with pytest.raises(VariantInvalid, match="already declares"):
        build_task_toml(once)


def test_refuses_invalid_toml() -> None:
    with pytest.raises(VariantInvalid, match="not valid TOML"):
        build_task_toml("schema_version = [\n")


def test_refuses_agent_allowlist_without_mode(tmp_path: Path) -> None:
    text = PARENT_TASK_TOML.replace(
        'timeout_sec = 3600.0', 'timeout_sec = 3600.0\nallowed_hosts = ["example.com"]'
    )
    with pytest.raises(VariantInvalid, match="allowed_hosts"):
        build_task_toml(text)


def test_refuses_windows_target() -> None:
    text = PARENT_TASK_TOML.replace('workdir = "/testbed"', 'workdir = "/testbed"\nos = "windows"')
    with pytest.raises(VariantInvalid, match="Windows"):
        build_task_toml(text)


def test_missing_agent_table_is_appended() -> None:
    text = PARENT_TASK_TOML.replace("[agent]\ntimeout_sec = 3600.0\n\n", "")
    new_text = build_task_toml(text)
    config = tomllib.loads(new_text)
    assert config["agent"] == {"network_mode": "no-network"}


def test_only_task_toml_changes(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / TASK_TOML_REL).write_text(PARENT_TASK_TOML, encoding="utf-8")
    (parent / "instruction.md").write_text("Fix it.\n", encoding="utf-8")
    changes, inputs = build_changes(parent)
    assert set(changes) == {TASK_TOML_REL}
    assert inputs["agent_network_mode"] == "no-network"
    assert inputs["default_chain"] is False


def test_refuses_missing_task_toml(tmp_path: Path) -> None:
    with pytest.raises(VariantInvalid, match="no readable task.toml"):
        build_changes(tmp_path)


def test_derive_records_lineage(tmp_path: Path) -> None:
    from evallab.task_variants import materialize

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / TASK_TOML_REL).write_text(PARENT_TASK_TOML, encoding="utf-8")
    record = derive_agent_network_none(
        parent, repo_root=tmp_path, variants_root=tmp_path / "variants"
    )
    assert record.transform == TRANSFORM_ID
    package_dir = materialize(
        record, parent, repo_root=tmp_path, variants_root=tmp_path / "variants"
    )
    derived_text = (package_dir / TASK_TOML_REL).read_text(encoding="utf-8")
    assert tomllib.loads(derived_text)["agent"]["network_mode"] == "no-network"

def test_harbor_plan_locks_agent_and_keeps_verifier_public() -> None:
    """End-to-end policy resolution through Harbor 0.24's own resolver.

    The derived task.toml must resolve to a no-network agent phase and a
    public verifier phase under separate-verifier mode — i.e. V6 closed,
    network graders untouched.
    """
    harbor_models = pytest.importorskip("harbor.models.task.config")
    network_policy = pytest.importorskip("harbor.trial.network_policy")
    trial_config = pytest.importorskip("harbor.models.trial.config")
    task_cfg = harbor_models.TaskConfig.model_validate(
        tomllib.loads(build_task_toml(PARENT_TASK_TOML))
    )
    plan = network_policy.resolve_trial_network_plan(
        task_cfg,
        trial_config.AgentConfig(),
        trial_config.EnvironmentConfig(),
        None,
        verifier_mode=harbor_models.VerifierEnvironmentMode.SEPARATE,
    )
    assert plan.agent_phase.network_mode == harbor_models.NetworkMode.NO_NETWORK
    assert plan.verifier_phase.network_mode == harbor_models.NetworkMode.PUBLIC
