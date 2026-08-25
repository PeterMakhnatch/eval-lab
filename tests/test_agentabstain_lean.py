from __future__ import annotations

import importlib
import json
import stat
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from library.adapters.agentabstain.adapter import load_variants, primary_verdict  # noqa: E402
from library.adapters.agentabstain.controls import evaluate  # noqa: E402
from library.adapters.agentabstain.materialize import materialize  # noqa: E402
from scripts.agentabstain.assert_reward import _job_reward  # noqa: E402

materializer_module = importlib.import_module("library.adapters.agentabstain.materialize")

SEED = json.loads((ROOT / "library/adapters/agentabstain/source/canary_state.json").read_text())


def test_selected_pair_is_act_and_abstain() -> None:
    variants = load_variants()
    assert [variant.task_type for variant in variants] == ["act", "abstain"]
    assert variants[0].pair_id == "ambiguous_action_specification/preview_002"


def test_controls_defend_oracle_nop_and_mutant_boundaries() -> None:
    observed = {name: row["verdict"] for name, row in evaluate(SEED).items()}
    assert observed == {
        "act_oracle": "pass",
        "act_nop": "fail",
        "act_mutant": "fail",
        "abstain_oracle": "pass",
        "abstain_nop": "pass",
        "abstain_mutant": "fail",
    }


def test_materializer_is_digest_keyed_and_deterministic(tmp_path: Path) -> None:
    first = materialize(tmp_path / "one")
    second = materialize(tmp_path / "two")
    def digest(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert first.name == second.name
    assert digest(first) == digest(second)

def test_materializer_emits_variant_solutions_and_executable_scripts(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    act = root / "agentabstain-ambiguous-action-preview-002-act"
    abstain = root / "agentabstain-ambiguous-action-preview-002-abstain"
    assert "spotify.write_gmail_draft" in (act / "solution/solve.sh").read_text()
    assert "deliberately make no calls" in (abstain / "solution/solve.sh").read_text()
    for package in (act, abstain):
        assert (package / "solution/solve.sh").stat().st_mode & stat.S_IXUSR
        config = tomllib.loads((package / "task.toml").read_text())
        assert config["environment"]["network_mode"] == "public"
        assert config["environment"].get("build_timeout_sec") == 120.0
        assert config["verifier"]["environment_mode"] == "separate"
        assert config["verifier"]["environment"]["network_mode"] == "no-network"
        assert (package / "tests/test.sh").stat().st_mode & stat.S_IXUSR
        verifier_docker = (package / "tests/Dockerfile").read_text()
        test_sh = (package / "tests/test.sh").read_text()
        entrypoint = (package / "environment/entrypoint.sh").read_text()
        assert "WORKDIR /tests" in verifier_docker
        assert "COPY fixtures/initial_state.json /tests/fixtures/initial_state.json" in verifier_docker
        assert "ENTRYPOINT" not in verifier_docker
        assert "reward.txt" in test_sh
        assert "set -eu" in test_sh
        assert "exec sleep infinity" in entrypoint

def test_verifier_reward_script_is_executable_and_produces_binary_reward(tmp_path: Path) -> None:
    package = materialize(tmp_path)
    act = package / "agentabstain-ambiguous-action-preview-002-act"
    test_sh = (act / "tests/test.sh").read_text()
    assert test_sh.startswith("#!/bin/sh")
    assert "/logs/verifier/reward.txt" in test_sh
    assert "printf '1" in test_sh and "printf '0" in test_sh


def test_reward_assertion_reads_persisted_harbor_result(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(json.dumps({
        "stats": {"evals": {"control": {"metrics": [{"mean": 1.0}]}}},
    }))
    assert _job_reward(job) == 1.0

def test_committed_corpus_guard_rejects_generated_task(monkeypatch) -> None:
    def fake_check_output(command, text):
        return "library/tasks/agentabstain-canary/task.toml\n" if command[-1] == "library/tasks/agentabstain-*" else ""

    monkeypatch.setattr(materializer_module.subprocess, "check_output", fake_check_output)
    try:
        materializer_module.assert_no_committed_generated(Path("/tmp"))
    except RuntimeError as error:
        assert "library/tasks/agentabstain-canary/task.toml" in str(error)
    else:
        raise AssertionError("committed generated task was not rejected")


def test_abstain_rejects_state_mutation_without_tool() -> None:
    abstain = load_variants()[1]
    changed = json.loads(json.dumps(SEED))
    changed["gmail"]["drafts"]["draft_katie_001"]["body"] = "mutated"
    verdict = primary_verdict(abstain, [], initial_state=SEED, final_state=changed)
    assert verdict["verdict"] == "fail"
    assert verdict["termination_reason"] == "state_changed_without_critical_action"
