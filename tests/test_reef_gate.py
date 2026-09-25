"""Reef harness-gate intake: episode conversion, pairing, and import."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.evidence import reef_gate
from evallab.evidence.atif import SUPPORTED_SCHEMA_VERSIONS, _validate_fallback

FIXTURES = Path(__file__).resolve().parent / "fixtures/reef_gate"
STEPS = FIXTURES / "steps"


def _fixture_episodes() -> list[dict]:
    return reef_gate.iter_gate_episodes(STEPS)


def test_all_fixture_episodes_convert_and_validate() -> None:
    episodes = _fixture_episodes()
    assert len(episodes) == 4
    for entry in episodes:
        payload = reef_gate.parse_reef_gate_episode(
            entry["episode_dir"],
            scenario=entry["scenario"],
            step=entry["step"],
            run_label="fixture",
        )
        assert _validate_fallback(payload) is None
        assert payload["schema_version"] in SUPPORTED_SCHEMA_VERSIONS
        assert payload["extra"]["origin"] == "reef"
        reef_meta = payload["extra"]["reef"]
        assert reef_meta["side"] in ("candidate", "current")
        assert reef_meta["step"] == entry["step"]
        assert reef_meta["scenario"] == "aa-gate"
        assert payload["agent"]["name"] == reef_gate.REEF_AGENT_NAME


def test_tool_calls_pair_with_observations_in_one_step() -> None:
    episode_dir = STEPS / "aa-gate/1/episodes/candidate-0"
    payload = reef_gate.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=1)
    tool_step = next(step for step in payload["steps"] if step.get("tool_calls"))
    results = tool_step["observation"]["results"]
    call_ids = {call["tool_call_id"] for call in tool_step["tool_calls"]}
    assert {result["source_call_id"] for result in results} <= call_ids
    assert results[0]["content"] == "7"


def test_empty_reply_is_kept_as_evidence() -> None:
    episode_dir = STEPS / "aa-gate/1/episodes/current-0"
    payload = reef_gate.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=1)
    assert _validate_fallback(payload) is None
    agent_steps = [step for step in payload["steps"] if step["source"] == "agent"]
    assert agent_steps and all(isinstance(step["message"], str) for step in agent_steps)


def test_missing_source_fields_stay_missing(tmp_path: Path) -> None:
    episode_dir = tmp_path / "candidate-0"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps({"task": "[toy] Reply with 7."}), encoding="utf-8"
    )
    (episode_dir / "session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "seq": 0, "data": {"agent": "root"}}),
                json.dumps({"type": "turn/start", "seq": 1, "data": {"turn": 1}}),
                json.dumps({"type": "step/start", "seq": 2, "data": {"turn": 1, "step": 1}}),
                json.dumps(
                    {
                        "type": "assistant/message",
                        "seq": 3,
                        "data": {"step": 1, "content": "", "tool_calls": [], "finish": "stop"},
                    }
                ),
                json.dumps({"type": "step/end", "seq": 4, "data": {"turn": 1, "step": 1}}),
                json.dumps(
                    {"type": "turn/end", "seq": 5, "data": {"turn": 1, "reason": {"kind": "done"}}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload = reef_gate.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=9)
    assert _validate_fallback(payload) is None
    reef_meta = payload["extra"]["reef"]
    for absent in (
        "episode_score",
        "failure",
        "path",
        "exit_code",
        "stdout",
        "stderr",
        "residue",
        "proposal_id",
        "release_id",
    ):
        assert absent not in reef_meta
    assert "model_name" not in payload["agent"]
    assert all("metrics" not in step for step in payload["steps"])
    assert all("model_name" not in step for step in payload["steps"])


def test_failed_episode_with_score_stays_unscored(tmp_path: Path) -> None:
    episode_dir = tmp_path / "current-0"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "task": "[toy] Reply with 7.",
                "score": 1.0,
                "failure": {"kind": "timeout"},
                "path": {"stages": ["think"], "reason": "error"},
            }
        ),
        encoding="utf-8",
    )
    (episode_dir / "session.jsonl").write_text(
        json.dumps({"type": "session", "seq": 0, "data": {"agent": "root", "model": "m"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant/message",
                "seq": 1,
                "data": {"step": 1, "content": "x", "tool_calls": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = reef_gate.parse_reef_gate_episode(episode_dir)
    assert _validate_fallback(payload) is None
    assert "episode_score" not in payload["extra"]["reef"]
    assert payload["extra"]["reef"]["failure"] == {"kind": "timeout"}
    assert reef_gate.episode_passed(1.0) is True
    assert reef_gate.episode_passed(None) is None


def test_unknown_episode_names_are_not_episodes() -> None:
    assert reef_gate.parse_episode_name("candidate-0") == {
        "side": "candidate",
        "task_index": 0,
        "repeat": 0,
    }
    assert reef_gate.parse_episode_name("current-2-3") == {
        "side": "current",
        "task_index": 2,
        "repeat": 3,
    }
    assert reef_gate.parse_episode_name("notes") is None
    assert reef_gate.parse_episode_name("candidate-x") is None


def test_import_writes_historical_layout_not_a_harbor_job(tmp_path: Path) -> None:
    out = tmp_path / "reef-fixture"
    summary = reef_gate.import_reef_gate_run(
        STEPS, out, run_label="fixture", results_path=FIXTURES / "results.jsonl"
    )
    assert summary["episodes_total"] == 4
    assert summary["episode_pass"] == 1
    assert summary["trials"] == 2
    assert summary["published"] == 1
    assert summary["wins_total"] == 1
    assert summary["losses_total"] == 0

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["evidence_kind"] == "historical"
    assert manifest["origin"] == "reef"
    assert len(manifest["trajectories"]) == 4
    for relative in manifest["trajectories"]:
        payload = json.loads((out / relative).read_text(encoding="utf-8"))
        assert _validate_fallback(payload) is None
        assert payload["extra"]["origin"] == "reef"

    pairs = json.loads((out / "pairs.json").read_text(encoding="utf-8"))
    assert [(pair["step"], pair["outcome"], pair["published"]) for pair in pairs] == [
        (1, "W", True),
        (2, "T", False),
    ]

    # Not a native Harbor job: no Harbor trial layout anywhere in the output.
    assert not list(out.rglob("result.json"))
    assert not list(out.rglob("trajectory.json"))
    assert (out / "intake.json").read_text(encoding="utf-8").find('"native_harbor_job": false') != -1


def test_module_cli_imports_fixture_corpus(tmp_path: Path, capsys) -> None:
    out = tmp_path / "cli-out"
    assert (
        reef_gate.main(
            [
                "--steps-root",
                str(STEPS),
                "--out",
                str(out),
                "--run-label",
                "fixture",
                "--results",
                str(FIXTURES / "results.jsonl"),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["episode_pass"] == 1
    assert (out / "summary.json").is_file()
