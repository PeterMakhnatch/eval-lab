"""ScriptAgent: one shell script in a seat, its log file and its ATIF trajectory.

The unit tests need `harbor` (the fork) to be importable; they are skipped in an environment
without it. Run them with the fork's interpreter:

    PYTHONPATH=src <fork>/.venv/bin/python -m pytest tests/test_script_agent.py

The last test starts real containers through the pinned fork. It runs with MUSTER_DOCKER=1 only.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from muster import cli, load

HARBOR = os.environ.get("MUSTER_HARBOR", "")
HELLO_SEATS = Path(HARBOR).parents[2] / "examples" / "tasks" / "hello-seats" if HARBOR else Path("missing")


class FakeSeat:
    """The part of a seat environment that ScriptAgent uses."""

    def __init__(self, return_code=0, stdout="hello\n", hang=False, chmod=0):
        self.calls, self.return_code, self.stdout, self.hang, self.chmod = (
            [],
            return_code,
            stdout,
            hang,
            chmod,
        )

    async def upload_file(self, source_path, target_path):
        self.calls.append(("upload", str(source_path), target_path))

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append(("exec", command, cwd, env, user))
        if command.startswith("chmod"):
            return SimpleNamespace(
                return_code=self.chmod, stdout="chmod failed" if self.chmod else "", stderr=None
            )
        if self.hang:
            await asyncio.sleep(60)
        return SimpleNamespace(return_code=self.return_code, stdout=self.stdout, stderr=None)


@pytest.fixture
def agent(tmp_path):
    script_module = pytest.importorskip(
        "muster.agents.script", reason="harbor is not importable", exc_type=ImportError
    )
    script = tmp_path / "policies" / "spoof.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\necho hello\n")
    made = script_module.ScriptAgent(
        logs_dir=tmp_path / "trial" / "seats" / "p2" / "agent", script=str(script)
    )
    made.session_id = "hello-seats__abc__p2"
    return made


def trajectory(agent) -> dict:
    from harbor.models.trajectories import Trajectory

    text = (agent.logs_dir / "trajectory.json").read_text()
    Trajectory.model_validate_json(text)  # a valid ATIF document
    return json.loads(text)


def test_identity(agent):
    import muster

    info = agent.to_agent_info()
    assert (agent.name(), info.name, info.version, info.model_info) == (
        "script",
        "script",
        muster.__version__,
        None,
    )
    assert agent.import_path() == "muster.agents.script:ScriptAgent"
    assert agent.capabilities.atif is True


def test_setup_uploads_the_script_and_lets_the_seat_user_read_it(agent, tmp_path):
    seat = FakeSeat()
    asyncio.run(agent.setup(seat))
    assert seat.calls == [
        ("upload", str(tmp_path / "policies" / "spoof.sh"), "/logs/agent/spoof.sh"),
        ("exec", "chmod a+rx /logs/agent/spoof.sh", None, None, "root"),
    ]
    with pytest.raises(RuntimeError, match=re.escape("cannot prepare /logs/agent/spoof.sh: chmod failed")):
        asyncio.run(agent.setup(FakeSeat(chmod=1)))


def test_run_uses_the_seat_defaults_and_writes_the_log_and_the_trajectory(agent):
    seat = FakeSeat(stdout="posted\n")
    asyncio.run(agent.run("You are p2. Post one note.", seat, None))
    command = "bash /logs/agent/spoof.sh > /logs/agent/script.txt 2>&1; code=$?; cat /logs/agent/script.txt; exit $code"
    assert seat.calls == [
        ("exec", command, None, None, None)
    ]  # the seat's own working directory, environment and user
    document = trajectory(agent)
    assert (document["schema_version"], document["session_id"]) == ("ATIF-v1.7", "hello-seats__abc__p2")
    assert document["agent"]["name"] == "script" and document["agent"]["extra"]["policy"] == "spoof"
    user, step = document["steps"]
    assert (user["step_id"], user["source"], user["message"]) == (1, "user", "You are p2. Post one note.")
    assert (step["step_id"], step["source"], step["llm_call_count"]) == (2, "agent", 0)
    assert step["tool_calls"] == [
        {"tool_call_id": "script", "function_name": "bash", "arguments": {"command": command}}
    ]
    assert step["observation"]["results"] == [{"source_call_id": "script", "content": "posted\n"}]
    assert (step["extra"]["outcome"], step["extra"]["exit_code"], step["extra"]["output_chars"]) == (
        "exited",
        0,
        7,
    )


def test_a_failed_script_raises_like_an_installed_agent(agent):
    from harbor.agents.installed.base import NonZeroAgentExitCodeError

    with pytest.raises(
        NonZeroAgentExitCodeError, match=re.escape("Script spoof.sh exited with code 3\noutput: boom")
    ):
        asyncio.run(agent.run("go", FakeSeat(return_code=3, stdout="boom\n"), None))
    step = trajectory(agent)["steps"][1]
    assert step["extra"]["exit_code"] == 3 and step["observation"]["results"][0]["content"] == "boom\n"


def test_a_cancelled_run_stays_cancelled_and_leaves_a_trajectory(agent):
    async def play():
        task = asyncio.create_task(agent.run("go", FakeSeat(hang=True), None))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(play())
    step = trajectory(agent)["steps"][1]
    assert step["extra"]["outcome"] == "cancelled" and "exit_code" not in step["extra"]
    assert "script.txt" in step["observation"]["results"][0]["content"]


def test_long_output_keeps_its_end(agent):
    asyncio.run(agent.run("go", FakeSeat(stdout="x" * 30_000 + "END"), None))
    step = trajectory(agent)["steps"][1]
    content = step["observation"]["results"][0]["content"]
    assert len(content) == 20_000 and content.endswith("END") and step["extra"]["output_chars"] == 30_003


def test_the_script_must_be_an_absolute_path_of_a_file(agent, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for script in (None, "policies/spoof.sh", str(tmp_path / "missing.sh"), str(tmp_path)):
        with pytest.raises(ValueError, match="ScriptAgent needs the kwarg script"):
            type(agent)(logs_dir=tmp_path, script=script)


def test_harbor_builds_the_agent_from_a_participant(agent, tmp_path):
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import Participant

    participant = Participant(
        seat="p2",
        import_path="muster.agents.script:ScriptAgent",
        kwargs={"script": str(tmp_path / "policies" / "spoof.sh")},
        env={"ROUND": "3"},
    )
    built = AgentFactory.create_agent_from_config(participant, logs_dir=tmp_path / "logs")
    assert isinstance(built, type(agent)) and built.extra_env == {"ROUND": "3"}
    AgentFactory.run_preflight(participant)


POST = """#!/bin/bash
set -eu
other=$([ "$SEAT" = p1 ] && echo p2 || echo p1)
echo "seat=$SEAT user=$(id -un) cwd=$(pwd)"
curl -fsS http://world:8080/note -H 'Content-Type: application/json' -d "{\\"author\\": \\"$SEAT\\", \\"text\\": \\"hello from $SEAT\\"}"
for _ in $(seq 40); do
  if curl -fsS http://world:8080/notes | grep -q "\\"author\\": \\"$other\\""; then exit 0; fi
  sleep 0.5
done
exit 1
"""


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("MUSTER_DOCKER") != "1", reason="starts real containers: set MUSTER_DOCKER=1"
)
@pytest.mark.skipif(not HELLO_SEATS.is_dir(), reason="MUSTER_HARBOR is not the harbor of a fork checkout")
def test_a_script_policy_in_real_seats(tmp_path, capsys):
    """Each seat posts a note with a script and waits for the other: SEAT, user and working directory apply."""
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "post.sh").write_text(POST)
    out = tmp_path / "out"
    argv = ["matrix", str(HELLO_SEATS), "--policies", "post", "--policy-dir", str(tmp_path / "policies")]
    status = cli.main([*argv, "--timeout-multiplier", "0.1", "--out", str(out), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert (
        status == 0 and report["rosters"][0]["rewards"]["reward"]["mean"] == 1.0
    )  # p1's note and both records
    (trial,) = load.walk(out)
    assert trial.roster_key() == "p1=post,p2=post" and trial.exception is None
    for run in trial.seats:
        assert (run.agent, run.policy, run.exception) == ("script", "post", None)
        log = (run.logs / "script.txt").read_text()
        assert log.startswith(f"seat={run.seat} user=player cwd=/home/player\n")
        assert (run.logs / "post.sh").read_text() == POST
        step = json.loads(run.trajectory.read_text())["steps"][1]
        if run.under_test:  # play ends with this seat: its script ran to its end
            assert step["extra"]["exit_code"] == 0 and step["observation"]["results"][0]["content"] == log
        else:  # Harbor cancels a seat that is not under test when play ends; its script goes on in the seat
            assert step["extra"]["outcome"] in ("exited", "cancelled")
    notes = (trial.artifacts / "logs" / "world" / "notes.jsonl").read_text()
    assert '"author": "p1"' in notes and '"author": "p2"' in notes
