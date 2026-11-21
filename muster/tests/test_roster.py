"""roster.py: rosters, the control matrix, Harbor participants and the facts of a task."""

import os

import pytest
from seat_fixture import write_task

from muster import roster
from muster.model import InputError
from muster.roster import Roster


def test_roster_key_is_seat_order_and_safe_as_a_directory_name(tmp_path):
    made = Roster(("p1", "p2", "judge"), ("oracle", "nop", "late-1.v2"))
    assert made.roster_key() == "p1=oracle,p2=nop,judge=late-1.v2"
    assert made.items() == (("p1", "oracle"), ("p2", "nop"), ("judge", "late-1.v2"))
    (tmp_path / made.roster_key()).mkdir()
    assert os.sep not in made.roster_key() and " " not in made.roster_key()


@pytest.mark.parametrize(
    ("seats", "policies"),
    [
        ((), ()),
        (("p1", "p2"), ("oracle",)),
        (("p1", "p1"), ("oracle", "nop")),
        (("p1",), ("a/b",)),
        (("p1",), ("a=b",)),
        (("p1",), ("a,b",)),
        (("p1",), ("..",)),
        (("p 1",), ("oracle",)),
        (("p1",), ("",)),
    ],
)
def test_roster_refuses_names_that_do_not_fit(seats, policies):
    with pytest.raises(ValueError):
        Roster(seats, policies)


def test_matrix_is_the_product_in_a_stable_order():
    keys = [made.roster_key() for made in roster.matrix(["p1", "p2"], ["oracle", "nop"])]
    assert keys == ["p1=oracle,p2=oracle", "p1=oracle,p2=nop", "p1=nop,p2=oracle", "p1=nop,p2=nop"]
    assert len(roster.matrix(["a", "b", "c"], ["oracle", "nop", "x", "y"])) == 64
    assert [made.roster_key() for made in roster.matrix(["solo"], ["oracle"])] == ["solo=oracle"]


def test_matrix_refuses_more_rosters_than_the_limit():
    with pytest.raises(InputError, match=r"3 policies on 4 seats give 81 rosters, more than the limit of 64"):
        roster.matrix(["a", "b", "c", "d"], ["oracle", "nop", "x"])
    assert len(roster.matrix(["a", "b", "c", "d"], ["oracle", "nop", "x"], limit=81)) == 81
    with pytest.raises(InputError, match="limit of 3"):
        roster.matrix(["p1", "p2"], ["oracle", "nop"], limit=3)


def test_matrix_refuses_empty_and_repeated_input():
    for seats, policies in (([], ["oracle"]), (["p1"], []), (["p1"], ["nop", "nop"])):
        with pytest.raises(InputError):
            roster.matrix(seats, policies)


def test_participants_use_harbor_agents_and_script_agent(tmp_path):
    task = write_task(tmp_path, "duo", {"p1": True, "p2": False}, policies=("spoof",))
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "spoof.sh").write_text("exit 1\n")
    (extra / "idle.sh").write_text("exit 0\n")
    dirs = [task / "policies", extra]
    listed = roster.participants(Roster(("p1", "p2", "p3", "p4"), ("oracle", "nop", "spoof", "idle")), dirs)
    assert listed[:2] == [{"seat": "p1", "name": "oracle"}, {"seat": "p2", "name": "nop"}]
    assert listed[2] == {
        "seat": "p3",
        "import_path": "muster.agents.script:ScriptAgent",
        "kwargs": {"script": str(task / "policies" / "spoof.sh")},  # the task's own directory comes first
    }
    assert listed[3]["kwargs"] == {"script": str(extra / "idle.sh")}


def test_a_policy_path_is_absolute_and_keeps_its_name(tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "v2.sh").write_text("exit 0\n")
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "late.sh").symlink_to(tmp_path / "real" / "v2.sh")
    monkeypatch.chdir(tmp_path)
    found = roster.find_policy("late", ["policies"])
    assert found.is_absolute() and found.name == "late.sh"


def test_an_unknown_policy_names_the_searched_paths(tmp_path):
    with pytest.raises(ValueError) as error:
        roster.participants(Roster(("p1",), ("ghost",)), [tmp_path / "a", tmp_path / "b"])
    message = str(error.value)
    assert "unknown policy 'ghost'" in message
    assert str(tmp_path / "a" / "ghost.sh") in message and str(tmp_path / "b" / "ghost.sh") in message


def test_read_task_gives_seats_flags_and_metadata(tmp_path):
    meta = 'oracle_reward = 0.75\nexploits = ["spoof", "late"]'
    task = roster.read_task(
        write_task(tmp_path, "duo", {"p2": False, "p1": True, "judge": True}, metadata=meta)
    )
    assert task.path == tmp_path / "duo" and task.name == "muster/duo"
    assert task.seats == ("p2", "p1", "judge")  # the order of the tables in the file
    assert task.under_test == (False, True, True)
    assert (task.oracle_reward, task.exploits) == (0.75, ("spoof", "late"))
    assert (task.agent_timeout_sec, task.verifier_timeout_sec, task.build_timeout_sec) == (
        600.0,
        120.0,
        600.0,
    )


def test_read_task_defaults(tmp_path):
    path = write_task(tmp_path, "plain", {"p1": True}, agent_timeout=None)
    text = (path / "task.toml").read_text().replace('[task]\nname = "muster/plain"\n', "")
    (path / "task.toml").write_text(text)
    task = roster.read_task(path)
    assert (task.name, task.oracle_reward, task.exploits, task.agent_timeout_sec) == ("plain", None, (), None)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "cannot read task.toml"),
        ("[task\n", "is not valid TOML"),
        ('[task]\nname = "x"\n', r"no \[agents.<seat>\] table"),
        ("[agents.p1]\nunder_test = 1\n", "under_test is true or false"),
        ('[metadata]\noracle_reward = "high"\n[agents.p1]\n', "oracle_reward is a number"),
        ("[metadata]\noracle_reward = inf\n[agents.p1]\n", "oracle_reward is a number"),
        ('[metadata]\nexploits = "spoof"\n[agents.p1]\n', "exploits is a list of policy names"),
    ],
)
def test_read_task_refuses_unusable_tasks(tmp_path, text, message):
    if text is not None:
        (tmp_path / "task.toml").write_text(text)
    with pytest.raises(InputError, match=message):
        roster.read_task(tmp_path)


def test_task_dirs_is_one_task_or_the_tasks_inside(tmp_path):
    for name in ("b-task", "a-task"):
        write_task(tmp_path / "set", name, {"p1": True})
    (tmp_path / "set" / "notes").mkdir()
    assert roster.task_dirs(tmp_path / "set" / "a-task") == [tmp_path / "set" / "a-task"]
    assert roster.task_dirs(tmp_path / "set") == [tmp_path / "set" / "a-task", tmp_path / "set" / "b-task"]
    for unusable in (tmp_path / "set" / "notes", tmp_path / "missing"):
        with pytest.raises(InputError):
            roster.task_dirs(unusable)


def test_job_config_is_a_harbor_job_on_local_docker(tmp_path):
    listed = [{"seat": "p1", "name": "oracle"}]
    shared = {"tasks": [tmp_path / "t1", tmp_path / "t2"], "participants": listed}
    config = roster.job_config(
        name="j", jobs_dir=tmp_path / "jobs", attempts=3, concurrency=2, timeout_multiplier=None, **shared
    )
    assert config == {
        "job_name": "j",
        "jobs_dir": str(tmp_path / "jobs"),
        "n_attempts": 3,
        "n_concurrent_trials": 2,
        "quiet": True,
        "environment": {"type": "docker", "delete": True},
        "participants": listed,
        "tasks": [{"path": str(tmp_path / "t1")}, {"path": str(tmp_path / "t2")}],
    }
    assert "agents" not in config  # a seat trial refuses a config that sets `agent`
    scaled = roster.job_config(
        name="j", jobs_dir=tmp_path, attempts=1, concurrency=1, timeout_multiplier=0.05, **shared
    )
    assert scaled["agent_timeout_multiplier"] == 0.05 and "timeout_multiplier" not in scaled


def test_job_timeout_follows_the_task_timeouts(tmp_path):
    task = roster.read_task(write_task(tmp_path, "duo", {"p1": True}))
    assert task.trial_budget_sec(None) == 600 + 360 + 600 + 120 + 300
    assert task.trial_budget_sec(0.05) == 600 + 360 + 30 + 120 + 300
    assert roster.job_timeout_sec([task], 2, 1, 0.05) == 2 * 1410 + 1410 + 120
    assert roster.job_timeout_sec([task, task], 2, 2, None) == 2 * 1980 + 1980 + 120
    unlimited = roster.read_task(write_task(tmp_path, "open", {"p1": True}, agent_timeout=None))
    assert unlimited.trial_budget_sec(None) == 600 + 360 + 3600 + 120 + 300


def test_policy_env_goes_to_oracle_and_scripted_seats_and_refuses_secrets(tmp_path):
    (tmp_path / "idle.sh").write_text("#!/bin/bash\n")
    env = roster.policy_env(["WAIT_SEC=5", "MODE=fast"])
    assert env == {"WAIT_SEC": "5", "MODE": "fast"}
    listed = roster.participants(Roster(("p1", "p2", "p3"), ("oracle", "idle", "nop")), [tmp_path], env)
    assert [entry.get("env") for entry in listed] == [env, env, None]
    assert roster.participants(Roster(("p1",), ("oracle",)), [tmp_path]) == [{"seat": "p1", "name": "oracle"}]
    for bad in ("WAIT_SEC", "1X=2", "OPENAI_API_KEY=x", "github_token=y"):
        with pytest.raises(InputError):
            roster.policy_env([bad])
