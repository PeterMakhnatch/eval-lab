"""The wave-A agent import failure must refuse the spec before dispatch.

HAR-116 wave A burned its dispatch on an agent-side import
(``ModuleNotFoundError: No module named 'duckdb'``) that failed inside
Harbor's tool venv. When a spec's harness tree enables lab agent knobs, the
launch path proves the import in Harbor's python first; a broken closure
refuses the spec before any sandbox or Modal spend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import evallab.runner as runner_module
from evallab.execution_contracts import RunRequest
from evallab.runner import preflight_harness_agent_imports, run_experiment
from evallab.terminus_harness import load_harness_tree


def _tree(tmp_path: Path, config: dict[str, Any], name: str = "tree") -> tuple[Path, str]:
    root = tmp_path / name
    (root / "terminus").mkdir(parents=True)
    (root / "terminus" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return root, load_harness_tree(root).sha256


def _request(tmp_path: Path, tree: Path, digest: str, agent: str = "terminus-2") -> RunRequest:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "preflight-task"\n\n[agent]\n',
        encoding="utf-8",
    )
    return RunRequest(
        task=task_dir,
        agent=agent,
        model="zai/glm-5.3-flash" if agent == "terminus-2" else None,
        name="preflight-trial",
        jobs_dir=tmp_path / "runs",
        timeout_seconds=30,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
        harness_tree_path=tree,
        harness_tree_sha256=digest,
    )


def _fake_python(tmp_path: Path, body: str, name: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_tree_without_lab_knobs_never_probes(tmp_path: Path) -> None:
    tree, digest = _tree(tmp_path, {"temperature": 0.6})
    request = _request(tmp_path, tree, digest)
    # A python that does not exist would explode if spawned: None proves
    # the probe never runs when no lab knob is on.
    assert (
        preflight_harness_agent_imports(
            request, repo_root=tmp_path, python=tmp_path / "no-such-python"
        )
        is None
    )


def test_non_terminus_spec_never_probes(tmp_path: Path) -> None:
    tree, digest = _tree(tmp_path, {"loop_break": True})
    request = _request(tmp_path, tree, digest, agent="mini-swe-agent")
    assert (
        preflight_harness_agent_imports(
            request, repo_root=tmp_path, python=tmp_path / "no-such-python"
        )
        is None
    )


def test_broken_agent_import_refuses_with_module_reason(tmp_path: Path) -> None:
    tree, digest = _tree(tmp_path, {"loop_break": True, "output_cap_chars": 2000})
    request = _request(tmp_path, tree, digest)
    failing = _fake_python(
        tmp_path,
        "echo \"ModuleNotFoundError: No module named 'duckdb'\" >&2\nexit 1",
        "failing-python",
    )
    reason = preflight_harness_agent_imports(request, repo_root=tmp_path, python=failing)
    assert reason is not None
    assert "ModuleNotFoundError" in reason
    assert "duckdb" in reason
    assert "no sandbox or Modal spend" in reason


def test_missing_harbor_python_refuses(tmp_path: Path) -> None:
    tree, digest = _tree(tmp_path, {"completion_fix": True})
    request = _request(tmp_path, tree, digest)
    reason = preflight_harness_agent_imports(
        request, repo_root=tmp_path, python=tmp_path / "no-such-python"
    )
    assert reason is not None
    assert "Harbor's python is unavailable" in reason


def test_run_experiment_refuses_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, digest = _tree(tmp_path, {"loop_break": True})
    request = _request(tmp_path, tree, digest)
    monkeypatch.setattr(
        runner_module,
        "preflight_request",
        lambda _req: type("Decision", (), {"proceed": True, "reason": None})(),
    )

    def _must_not_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dispatch ran despite the broken agent import")

    monkeypatch.setattr(runner_module, "run_harbor_process", _must_not_dispatch)
    failing = _fake_python(
        tmp_path,
        "echo \"ModuleNotFoundError: No module named 'duckdb'\" >&2\nexit 1",
        "failing-python",
    )
    monkeypatch.setenv("EVALLAB_HARBOR_PYTHON", str(failing))
    with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
        run_experiment(request, repo_root=tmp_path)
    assert not (tmp_path / "runs" / request.name).exists()
    assert os.environ["EVALLAB_HARBOR_PYTHON"] == str(failing)
