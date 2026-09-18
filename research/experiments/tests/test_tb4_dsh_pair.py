"""Behavioral contracts for the TB4 html-js-filter DSH vs mini-swe pair compiler."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR
from evallab.schemas import ExperimentSpec

REPO_ROOT = Path(__file__).resolve().parents[3]
PAIR_DIR = REPO_ROOT / "research" / "experiments" / "tb4-dsh-pair"

_COMPILER: ModuleType | None = None


def _compiler() -> ModuleType:
    global _COMPILER
    if _COMPILER is None:
        spec = importlib.util.spec_from_file_location(
            "tb4_compile_pair", PAIR_DIR / "compile_pair.py"
        )
        assert spec is not None and spec.loader is not None
        _COMPILER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_COMPILER)
    return _COMPILER


def _written_pair(tmp_path: Path) -> tuple[ModuleType, dict]:
    cp = _compiler()
    baseline, dsh = cp.build_pair_specs()
    cp.write_pair_files(tmp_path, specs=(baseline, dsh))
    metadata = json.loads((tmp_path / "pair-metadata.json").read_text())
    return cp, metadata


def test_arms_share_task_identity_and_differ_only_in_agent() -> None:
    cp = _compiler()
    baseline, dsh = cp.build_pair_specs()
    assert baseline.task == dsh.task == "terminal-bench/html-js-filter"
    assert (baseline.task_package_digest, baseline.verifier_digest) == (
        dsh.task_package_digest,
        dsh.verifier_digest,
    )
    for field in (
        "model",
        "attempts",
        "concurrency",
        "timeout_seconds",
        "environment",
        "jobs_dir",
        "purpose",
    ):
        assert getattr(baseline, field) == getattr(dsh, field), field
    assert baseline.attempts == 1
    assert baseline.model == dsh.model == DEEPSEEK_MODEL_SELECTOR
    assert baseline.agent == "mini-swe-agent"
    assert dsh.agent == "evallab.harbor_dsh:DeepSeekHarnessAgent"
    assert baseline.agent != dsh.agent
    # The DSH coordinate is a string reference only; the adapter is never imported.
    assert "evallab.harbor_dsh" not in sys.modules


def test_dsh_arm_carries_reasoning_effort_max(tmp_path: Path) -> None:
    cp, metadata = _written_pair(tmp_path)
    _ = cp
    arms = {arm["arm"]: arm for arm in metadata["arms"]}
    assert arms["dsh"]["agent_kwargs"]["reasoning_effort"] == "max"
    assert arms["baseline"]["agent_kwargs"] == {}
    # ExperimentSpec has no agent-kwarg field: the kwarg must not leak into the
    # emitted specs as an invented schema field.
    for name in ("tb4-dsh-pair-html-js-filter-baseline", "tb4-dsh-pair-html-js-filter-dsh"):
        doc = json.loads((tmp_path / f"{name}.json").read_text())
        assert "reasoning_effort" not in doc
        assert "agent_kwargs" not in doc
        ExperimentSpec.model_validate(doc)


def test_timeout_is_1800_not_craft_default(tmp_path: Path) -> None:
    cp, metadata = _written_pair(tmp_path)
    baseline, dsh = cp.build_pair_specs()
    assert baseline.timeout_seconds == dsh.timeout_seconds == 1800 != 28800
    assert metadata["agent_timeout_seconds"] == 1800
    assert metadata["approval_required"] is True
    assert metadata["submit_owner"] == "Integration"


def test_unregistered_tb4_task_refusal_still_writes_metadata(tmp_path: Path) -> None:
    cp = _compiler()
    baseline, _ = cp.build_pair_specs()
    with pytest.raises(ValueError, match="not registered in library/registry"):
        cp.assert_task_submittable(baseline, repo_root=REPO_ROOT)
    with pytest.raises(ValueError, match="not registered in library/registry"):
        cp.main(["--stage", "canary", "--out", str(tmp_path)])
    metadata_path = tmp_path / "pair-metadata.json"
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text())
    assert metadata["task_ref"] == "terminal-bench/html-js-filter"
    assert metadata["submittable"] is False
    assert (tmp_path / "tb4-dsh-pair-html-js-filter-baseline.json").is_file()
    assert (tmp_path / "tb4-dsh-pair-html-js-filter-dsh.json").is_file()


def test_cli_rejects_non_canary_stage(tmp_path: Path) -> None:
    cp = _compiler()
    with pytest.raises(ValueError, match="only the 'canary' pair"):
        cp.main(["--stage", "full", "--out", str(tmp_path)])
