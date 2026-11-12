"""Boundary tests for the auditor-SFT eval preparation (no Docker, no network, no spend)."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/auditor-sft-v1/prepare_eval.py"


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("auditor_prepare_eval", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare_eval = _load()

HOLDOUT_IDS = [
    "implement-nonogram-solver",
    "rush-hour-bfs-solver",
    "adaptive-rejection-sampler",
    "blind-maze-explorer-5x5",
    "titanic-female-survival-prediction",
]

LEAKY_IDS = [
    "format-code-task-000792",
    "format-code-task-001269",
    "format-code-task-002486",
    "format-code-task-002552",
    "format-code-task-002938",
]


def test_holdout_ids_stable_and_disjoint_from_leaky() -> None:
    assert len(set(HOLDOUT_IDS)) == 5
    assert not set(HOLDOUT_IDS) & set(LEAKY_IDS)


def test_holdout_cells_match_canonical_manifest() -> None:
    cells = prepare_eval.holdout_cells()
    assert [cell["task_id"] for cell in cells] == HOLDOUT_IDS
    manifest = prepare_eval.load_holdout()
    assert manifest["schema_version"] == "auditor-sft-holdout-manifest/v1"
    assert [task["task_id"] for task in manifest["tasks"]] == HOLDOUT_IDS
    for cell in cells:
        assert cell["task_package_digest"].startswith("sha256:")


def _manifest_doc(tasks: list[dict]) -> dict:
    return {"schema_version": "auditor-sft-holdout-manifest/v1", "tasks": tasks}


def _manifest_task(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task_package_digest": "sha256:" + "1" * 64,
        "cache": f"derived/auditor-sft-v1/holdout-tasks/{task_id}/original_task",
        "files": [{"path": "original_task/task.toml", "bytes": 3, "sha256": "sha256:" + "2" * 64}],
    }


def test_load_holdout_refuses_bad_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_eval, "ROOT", tmp_path)
    manifest_path = tmp_path / prepare_eval.HOLDOUT_MANIFEST_REL
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"schema_version": "other/v9", "tasks": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported holdout schema"):
        prepare_eval.load_holdout()
    dupes = _manifest_doc([_manifest_task("x")] * 5)
    manifest_path.write_text(json.dumps(dupes), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct|exactly five"):
        prepare_eval.load_holdout()
    nofiles = _manifest_doc([_manifest_task(f"t{i}") for i in range(4)] + [{"task_id": "t4"}])
    manifest_path.write_text(json.dumps(nofiles), encoding="utf-8")
    with pytest.raises(ValueError, match="no pinned files"):
        prepare_eval.load_holdout()


def test_ensure_holdout_bytes_keeps_matching_and_fetches_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    monkeypatch.setattr(prepare_eval, "ROOT", tmp_path)
    body = b"abc"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    manifest = _manifest_doc([_manifest_task("t0")])
    manifest["source"] = {"hf_revision": "deadbeef"}
    manifest["tasks"][0]["files"] = [{"path": "original_task/task.toml", "bytes": 3, "sha256": digest}]
    calls: list[str] = []

    def fake_fetch(revision: str, remote: str) -> bytes:
        calls.append(remote)
        assert revision == "deadbeef"
        return body

    monkeypatch.setattr(prepare_eval, "_hf_file_bytes", fake_fetch)
    ensured = prepare_eval.ensure_holdout_bytes(manifest)
    assert ensured == [{"task_id": "t0", "fetched": 1, "files": 1}]
    assert (tmp_path / manifest["tasks"][0]["cache"] / "task.toml").read_bytes() == body
    ensured = prepare_eval.ensure_holdout_bytes(manifest)
    assert ensured[0]["fetched"] == 0
    assert calls == ["tasks/t0/claude-opus-4.6/original_task/task.toml"]


def test_ensure_holdout_bytes_refuses_upstream_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prepare_eval, "ROOT", tmp_path)
    manifest = _manifest_doc([_manifest_task("t0")])
    manifest["source"] = {"hf_revision": "deadbeef"}
    monkeypatch.setattr(prepare_eval, "_hf_file_bytes", lambda revision, remote: b"xyz")
    with pytest.raises(ValueError, match="pinned bytes mismatch"):
        prepare_eval.ensure_holdout_bytes(manifest)


def test_leaky_cells_match_historical_campaign() -> None:
    cells = prepare_eval.leaky_cells()
    assert [cell["task_id"] for cell in cells] == LEAKY_IDS
    campaign = json.loads((prepare_eval.ROOT / prepare_eval.LEAKY_CAMPAIGN_REL).read_text(encoding="utf-8"))
    assert [task["task_id"] for task in campaign["tasks"]] == LEAKY_IDS


def _task_dir(tmp_path: Path, name: str, toml: str, dockerfile: str | None = None) -> Path:
    task = tmp_path / name
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(toml, encoding="utf-8")
    if dockerfile is not None:
        (task / "environment" / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return task


CLEAN_TOML = """\
[task]
name = "tw/implement-nonogram-solver"

[agent]
timeout_sec = 900.0

[environment]
cpus = 1
memory_mb = 2048
gpus = 0
"""


def test_lock_eligibility_clean_task(tmp_path: Path) -> None:
    task = _task_dir(tmp_path, "clean", CLEAN_TOML, "FROM docker.io/ubuntu:22.04\nRUN apt-get install -y tmux\n")
    result = prepare_eval.lock_eligibility(task)
    assert result["eligible"] is True
    assert result["tmux"] == "dockerfile-names-tmux"


def test_lock_eligibility_refuses_compose_gpu_phase(tmp_path: Path) -> None:
    task = _task_dir(tmp_path, "multi", CLEAN_TOML)
    (task / "environment" / "docker-compose.yaml").write_text("services:\n  main: {}\n", encoding="utf-8")
    assert prepare_eval.lock_eligibility(task)["eligible"] is False
    gpu = _task_dir(tmp_path, "gpu", CLEAN_TOML.replace("gpus = 0", "gpus = 1"))
    assert any("GPU" in reason for reason in prepare_eval.lock_eligibility(gpu)["reasons"])
    phase = _task_dir(tmp_path, "phase", CLEAN_TOML + '\n[verifier]\nnetwork_mode = "no-network"\n')
    assert prepare_eval.lock_eligibility(phase)["eligible"] is False


def test_lock_eligibility_flags_opaque_base(tmp_path: Path) -> None:
    image = "FROM registry.example.com/org/img@sha256:abc\n"
    task = _task_dir(tmp_path, "opaque", CLEAN_TOML, image)
    result = prepare_eval.lock_eligibility(task)
    assert result["eligible"] is True
    assert result["tmux"] == "opaque-base-unverified"
    bare = _task_dir(tmp_path, "bare", CLEAN_TOML, "RUN echo hi\n")
    assert prepare_eval.lock_eligibility(bare)["tmux"] == "dockerfile-silent"


def test_lora_sampler_binding_parses_strictly() -> None:
    selector = prepare_eval.lora_selector("tinker://abc123_XYZ:train:0/sampler_weights/50")
    assert selector == prepare_eval.BASE_MODEL + "@tinker://abc123_XYZ:train:0/sampler_weights/50"
    with pytest.raises(ValueError):
        prepare_eval.lora_selector("not-a-sampler-path")
    with pytest.raises(ValueError):
        prepare_eval.lora_selector("")


def test_locked_docker_admits_terminus_not_codex() -> None:
    from evallab.execution_contracts import EGRESS_LOCK_AGENTS, TERMINUS_AGENT

    assert TERMINUS_AGENT in EGRESS_LOCK_AGENTS["docker"]
    assert "codex" not in EGRESS_LOCK_AGENTS["docker"]


def test_base_spec_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_eval, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_eval, "HARNESS", tmp_path / "harness")

    class _Harness:
        sha256 = "sha256:" + "0" * 64

    cell = {
        "task_id": "implement-nonogram-solver",
        "short": "implement-nonogram-solver",
        "task_package_digest": "sha256:" + "1" * 64,
        "kind": "holdout",
    }
    payload = prepare_eval.build_spec(
        cell, "runs/.prepared-tasks/x", _Harness(), prepare_eval.TW_PROMPT_REL,
        "sha256:" + "2" * 64,
        attempt=1, arm="base", model=prepare_eval.BASE_MODEL,
    )
    assert payload["name"] == "aud1-base-h-implement-nonogram-solver-k1"
    assert payload["model"] == prepare_eval.BASE_MODEL
    assert payload["extra_instruction_path"] == prepare_eval.TW_PROMPT_REL
    assert payload["egress_lock"] is True
    assert payload["environment"] == "docker"
    assert payload["max_requests"] == prepare_eval.MAX_REQUESTS
    assert payload["max_input_tokens"] == prepare_eval.MAX_INPUT_TOKENS
    lora = prepare_eval.build_spec(
        cell, "runs/.prepared-tasks/x", _Harness(), prepare_eval.TW_PROMPT_REL,
        "sha256:" + "2" * 64,
        attempt=1, arm="lora", model=prepare_eval.BASE_MODEL + "@tinker://r:train:0/sampler_weights/1",
    )
    assert lora["name"] == "aud1-lora-h-implement-nonogram-solver-k1"
    probe = prepare_eval.build_spec(
        {**cell, "kind": "leaky", "short": "000792"}, "runs/.prepared-tasks/y",
        _Harness(), "research/experiments/preambles/exploit-probe-v2.md", "sha256:" + "3" * 64,
        attempt=1, arm="base", model=prepare_eval.BASE_MODEL,
    )
    assert probe["name"] == "aud1-base-l-000792-k1"
    assert probe["extra_instruction_path"] == "research/experiments/preambles/exploit-probe-v2.md"
