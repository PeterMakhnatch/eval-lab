import json
from datetime import UTC, datetime

from tidegauge.atif import LFS_HEAD, Call, read_trajectory


def write(trial_dir, content):
    """Write ``agent/trajectory.json`` under a trial directory. Text and bytes go in as they are."""
    path = trial_dir / "agent" / "trajectory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return trial_dir


def atif(steps, **root):
    """An ATIF file as a dictionary. The steps get their ``step_id`` here."""
    return {
        "schema_version": "ATIF-v1.6",
        "session_id": "s",
        "agent": {"name": "terminus-2", "version": "2.0.0", "model_name": "anthropic/claude-opus-4-6"},
        "steps": [{"step_id": i, **step} for i, step in enumerate(steps, start=1)],
        **root,
    }


def agent(metrics=None, **fields):
    step = {"source": "agent", "message": "ok", **fields}
    if metrics is not None:
        step["metrics"] = metrics
    return step


def test_calls_totals_and_agent(tmp_path):
    first = {"prompt_tokens": 1400, "completion_tokens": 304, "cost_usd": 0.0146}
    second = {"prompt_tokens": 8934.0, "completion_tokens": 965, "cached_tokens": 1390, "cost_usd": 0.08}
    totals = {
        "total_prompt_tokens": 10334,
        "total_completion_tokens": 1269,
        "total_cached_tokens": 1390,
        "total_cost_usd": 0.0946,
    }
    steps = [
        {"source": "user", "message": "do the task", "timestamp": "2026-02-06T00:14:34+00:00"},
        agent(first, timestamp="2026-02-06T00:15:42.5+00:00"),
        agent(second, timestamp="2026-02-06T00:17:38Z"),
    ]
    t = read_trajectory(write(tmp_path, atif(steps, final_metrics=totals)))
    assert t.problem is None and t.path == tmp_path / "agent" / "trajectory.json"
    assert (t.schema_version, t.agent, t.agent_version) == ("ATIF-v1.6", "terminus-2", "2.0.0")
    assert t.model == "anthropic/claude-opus-4-6"
    assert t.n_steps == 3
    assert t.calls == (
        Call(2, datetime(2026, 2, 6, 0, 15, 42, 500000, tzinfo=UTC), 1400, 304, None, 0.0146),
        Call(3, datetime(2026, 2, 6, 0, 17, 38, tzinfo=UTC), 8934, 965, 1390, 0.08),
    )
    assert isinstance(t.calls[1].prompt_tokens, int)
    assert (t.total_prompt_tokens, t.total_completion_tokens, t.total_cached_tokens) == (10334, 1269, 1390)
    assert t.total_cost_usd == 0.0946
    assert (t.continued, t.subagents) == (False, 0)


def test_no_file_is_none(tmp_path):
    assert read_trajectory(tmp_path) is None
    (tmp_path / "agent").mkdir()
    assert read_trajectory(tmp_path) is None
    (tmp_path / "other").write_text("this trial directory is a file")
    assert read_trajectory(tmp_path / "other") is None


def test_files_that_cannot_be_used_have_a_problem(tmp_path):
    pointer = f"{LFS_HEAD}.github.com/spec/v1\noid sha256:4d7a2146\nsize 89374\n"
    cases = {
        "lfs": (pointer, "Git LFS pointer"),
        "cut": ('{"schema_version": "ATIF-v1.6", "steps": [', "unreadable: JSONDecodeError"),
        "empty": ("", "unreadable: JSONDecodeError"),
        "bytes": (b"\xff\xfe{}", "unreadable: UnicodeDecodeError"),
        "list": ([1, 2], "not ATIF: no schema_version"),
        "other-format": ({"agent": "codebrain", "atif_trajectory": {}}, "not ATIF: no schema_version"),
        "number": ({"schema_version": 1, "steps": []}, "not ATIF: no schema_version"),
    }
    for name, (content, problem) in cases.items():
        t = read_trajectory(write(tmp_path / name, content))
        assert t.problem == problem, name
        assert (t.calls, t.n_steps, t.schema_version) == ((), 0, None)
    (tmp_path / "dir" / "agent" / "trajectory.json").mkdir(parents=True)
    assert read_trajectory(tmp_path / "dir").problem.startswith("unreadable: ")


def test_a_number_that_python_cannot_convert_is_a_problem(tmp_path):
    data = atif([agent({"prompt_tokens": 10**400})])
    assert read_trajectory(write(tmp_path, data)).problem.startswith("unexpected shape: ")


def test_steps_that_are_not_calls(tmp_path):
    user = {"source": "user", "message": "task", "metrics": {"prompt_tokens": 50}}
    steps = [
        {"source": "system", "message": "rules"},
        user,  # only an agent step is a call
        agent(),  # no metrics
        agent({"completion_tokens": 7}),  # no prompt tokens
        agent({"prompt_tokens": None}),
        agent({"prompt_tokens": "1200"}),
        agent({"prompt_tokens": True}),
        agent({"prompt_tokens": -5}),
        agent({"prompt_tokens": float("nan")}),
        agent({"prompt_tokens": 0, "completion_tokens": 0}),  # 0: the adapter did not report the usage
        agent({"prompt_tokens": 900}, is_copied_context=True),  # a call of an earlier session
        agent("metrics are not a mapping"),
        "not a step",
    ]
    t = read_trajectory(write(tmp_path, {"schema_version": "ATIF-v1.6", "steps": steps}))
    assert t.problem is None
    assert (t.n_steps, t.calls) == (12, ())  # 12 of the 13 items are steps


def test_optional_fields(tmp_path):
    zeros = {"prompt_tokens": 10, "cached_tokens": 0, "cost_usd": 0}
    steps = [
        agent(zeros, llm_call_count=3, timestamp="2026-02-06T10:00:00"),
        agent({"prompt_tokens": 20, "cost_usd": -1, "cached_tokens": 2.5}, timestamp="yesterday"),
        agent({"prompt_tokens": 30}, timestamp=1770336000, is_copied_context=False),
    ]
    data = atif(
        steps,
        schema_version="ATIF-v9.9",
        agent="judy",
        final_metrics={"total_prompt_tokens": "many", "total_cost_usd": float("inf")},
        continued_trajectory_ref="trajectory.cont-1.json",
        subagent_trajectories=[atif([agent({"prompt_tokens": 5})]), atif([])],
    )
    t = read_trajectory(write(tmp_path, data))
    assert t.schema_version == "ATIF-v9.9"  # kept as read
    assert (t.agent, t.agent_version, t.model) == (None, None, None)
    first, second, third = t.calls
    # A time without a zone is read as UTC. A cached count of 0 and a cost of 0 are kept as read.
    assert first == Call(1, datetime(2026, 2, 6, 10, tzinfo=UTC), 10, None, 0, 0.0, 3)
    assert second == Call(2, None, 20, None, 2, None)
    assert third == Call(3, None, 30, None, None, None)
    assert (t.total_prompt_tokens, t.total_cost_usd) == (None, None)
    assert (t.continued, t.subagents) == (True, 2)  # recorded; the calls of the subagents are not read


def test_a_file_without_steps_has_no_calls(tmp_path):
    for name, steps in (("none", None), ("text", "steps"), ("empty", [])):
        t = read_trajectory(write(tmp_path / name, {"schema_version": "ATIF-v1.2", "steps": steps}))
        assert (t.problem, t.n_steps, t.calls) == (None, 0, ())
    assert read_trajectory(write(tmp_path / "bare", {"schema_version": "ATIF-v1.0"})).calls == ()
