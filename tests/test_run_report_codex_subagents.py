"""Run report: codex native rollouts surface subagent threads Harbor drops.

Harbor 0.21's codex converter keeps only the newest
``agent/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl`` file and never emits
subagent references, so a codex run that spawns subagents reports
``delegations_only`` at best. These tests cover the native-rollout reader
(``evallab.interpretation.codex_rollouts``) and its feed into the subagent
section: child reconstruction, token attribution without double counting,
and missing/corrupt rollouts.

No retained codex trial ever invoked ``spawn_agent`` (21 ATIF trajectories
and every retained rollout across ``runs/*codex*`` use only ``exec``; the
word appears solely in system-prompt prose), so the multi-file cases use a
fixture whose record shapes mirror real retained rollouts (codex CLI
0.147.0: ``session_meta`` / ``turn_context`` / ``response_item``
``function_call`` + ``function_call_output`` paired by ``call_id`` /
``event_msg`` ``token_count`` with cumulative ``total_token_usage`` and
per-call ``last_token_usage``) plus the codex-source-verified spawn shapes:
child ``session_meta`` sources serialize as
``{"subagent": {"thread_spawn": {"parent_thread_id": ..., "depth": ...}}}``
(externally tagged ``SessionSource``; ``codex-rs/protocol/src/protocol.rs``,
wire example in ``codex-rs/cli/src/doctor/thread_inventory.rs``,
openai/codex commit 25270df) and V1 ``spawn_agent`` outputs as
``{"agent_id": "<child thread id>", "nickname": ...}``
(``codex-rs/core/src/tools/handlers/multi_agents/spawn.rs``; V2 outputs
carry only ``task_name``/``nickname`` and never link).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation.codex_rollouts import _classify_source, read_codex_rollouts
from evallab.interpretation.run_report import build_run_report, render_run_report_markdown

PARENT_SESSION = "01a0043a-4b83-7252-a594-fa289617124f"
CHILD_SESSION = "02b1155b-6c94-8363-b705-0b3907282350"
MODEL = "gpt-5.6-terra"
CHILD_ROLE = "explorer"
CHILD_NICKNAME = "eager-beaver"
CHILD_PATH = "/root/explore-dir"


def _spawn_source(parent: str = PARENT_SESSION) -> dict[str, Any]:
    return {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent,
                "depth": 1,
                "agent_path": CHILD_PATH,
                "agent_nickname": CHILD_NICKNAME,
                "agent_role": CHILD_ROLE,
            }
        }
    }


def _meta(
    session_id: str,
    ts: str = "2026-08-15T07:02:04.422Z",
    *,
    source: Any = "exec",
    thread_source: str = "user",
) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "timestamp": ts,
            "cwd": "/app",
            "originator": "codex_exec",
            "cli_version": "0.147.0",
            "source": source,
            "thread_source": thread_source,
            "model_provider": "openai",
        },
    }

def _turn_context(ts: str = "2026-08-15T07:02:05.000Z") -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "turn_context",
        "payload": {"model": MODEL, "turn_id": "turn-1", "cwd": "/app"},
    }


def _call(
    name: str, call_id: str, arguments: dict[str, Any], ts: str, *, kind: str = "custom_tool_call"
) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": kind,
            "id": f"ctc-{call_id}",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "input": json.dumps(arguments),
        },
    }


def _output(call_id: str, text: str, ts: str, *, kind: str = "custom_tool_call_output") -> dict[str, Any]:
    output: Any = text if kind == "function_call_output" else [{"type": "input_text", "text": text}]
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": kind,
            "id": f"ctco-{call_id}",
            "call_id": call_id,
            "output": output,
        },
    }

def _token_count(
    total_in: int, total_out: int, last_in: int, last_out: int, ts: str
) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total_in,
                    "cached_input_tokens": 0,
                    "output_tokens": total_out,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total_in + total_out,
                },
                "last_token_usage": {
                    "input_tokens": last_in,
                    "cached_input_tokens": 0,
                    "output_tokens": last_out,
                    "reasoning_output_tokens": 0,
                    "total_tokens": last_in + last_out,
                },
            },
        },
    }


def _write_rollout(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _parent_lines(child_id: str | None = None) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = [
        _meta(PARENT_SESSION),
        _turn_context(),
        _call("exec", "call-exec-1", {"cmd": "ls"}, "2026-08-15T07:02:06.000Z"),
        _output("call-exec-1", "app.py\n", "2026-08-15T07:02:07.000Z"),
        _token_count(13675, 116, 13675, 116, "2026-08-15T07:02:08.000Z"),
    ]
    if child_id is not None:
        lines += [
            _call(
                "spawn_agent",
                "call-spawn-1",
                {"task_name": "explore", "message": "survey the repo"},
                "2026-08-15T07:02:10.000Z",
                kind="function_call",
            ),
            # V1 spawn_agent output shape: agent_id IS the child thread id
            # (codex-rs/core/src/tools/handlers/multi_agents/spawn.rs).
            _output(
                "call-spawn-1",
                json.dumps({"agent_id": child_id, "nickname": CHILD_NICKNAME}),
                "2026-08-15T07:02:11.000Z",
                kind="function_call_output",
            ),
            _token_count(20100, 240, 6425, 124, "2026-08-15T07:02:12.000Z"),
        ]
    return lines


def _child_lines() -> list[dict[str, Any]]:
    # Cumulative totals: max(total) == 9000/900, while summing last_* would
    # give 9000+... per-call figures must not be summed into the total.
    meta = _meta(
        CHILD_SESSION, "2026-08-15T07:02:10.500Z", source=_spawn_source(), thread_source="subagent"
    )
    meta["payload"]["agent_nickname"] = CHILD_NICKNAME
    meta["payload"]["agent_role"] = CHILD_ROLE
    return [
        meta,
        _turn_context("2026-08-15T07:02:10.600Z"),
        _call("exec", "call-child-1", {"cmd": "ls src"}, "2026-08-15T07:02:13.000Z"),
        _output("call-child-1", "a.py\n", "2026-08-15T07:02:14.000Z"),
        _token_count(5000, 400, 5000, 400, "2026-08-15T07:02:15.000Z"),
        _call("exec", "call-child-2", {"cmd": "ls tests"}, "2026-08-15T07:02:16.000Z"),
        _output("call-child-2", "t.py\n", "2026-08-15T07:02:17.000Z"),
        _token_count(9000, 900, 4000, 500, "2026-08-15T07:02:18.000Z"),
    ]


def _codex_trial(
    root: Path,
    steps: list[dict[str, Any]],
    *,
    name: str = "trial",
    session_id: str = PARENT_SESSION,
    rollouts: dict[str, list[dict[str, Any]] | str] | None = None,
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    result = {
        "id": "trial-id",
        "trial_name": name,
        "task_name": "lab/task",
        "config": {"agent": {"name": "codex", "model_name": MODEL}},
        "agent_info": {"name": "codex", "version": "0.147.0"},
        "agent_result": {
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": 0.5,
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-08-15T07:00:00Z",
        "finished_at": "2026-08-15T07:10:00Z",
        "environment_setup": {
            "started_at": "2026-08-15T07:00:00Z",
            "finished_at": "2026-08-15T07:01:00Z",
        },
        "agent_setup": {
            "started_at": "2026-08-15T07:01:00Z",
            "finished_at": "2026-08-15T07:02:00Z",
        },
        "agent_execution": {
            "started_at": "2026-08-15T07:02:00Z",
            "finished_at": "2026-08-15T07:09:00Z",
        },
        "verifier": {
            "started_at": "2026-08-15T07:09:00Z",
            "finished_at": "2026-08-15T07:09:30Z",
        },
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {"name": "codex", "version": "0.147.0", "model_name": MODEL},
        "steps": [
            {
                "step_id": 0,
                "source": "user",
                "message": "Do the thing",
                "timestamp": "2026-08-15T07:02:00Z",
            },
            *steps,
        ],
    }
    (agent / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    for filename, payload in (rollouts or {}).items():
        target = agent / "sessions" / "2026" / "08" / "15" / filename
        if isinstance(payload, str):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        else:
            _write_rollout(target, payload)
    return trial


def _exec_step(step_id: int, second: int) -> dict[str, Any]:
    call_id = f"call-{step_id}"
    return {
        "step_id": step_id,
        "timestamp": f"2026-08-15T07:02:{second:02d}Z",
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": call_id, "function_name": "exec", "arguments": {"cmd": "ls"}}
        ],
        "observation": {"results": [{"source_call_id": call_id, "content": "app.py"}]},
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 100},
    }


def _spawn_step(step_id: int, second: int, child_id: str = CHILD_SESSION) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "timestamp": f"2026-08-15T07:02:{second:02d}Z",
        "source": "agent",
        "message": "",
        "tool_calls": [
            {
                "tool_call_id": "call-spawn-1",
                "function_name": "spawn_agent",
                "arguments": {"task_name": "explore", "message": "survey the repo"},
            }
        ],
        "observation": {
            "results": [
                {
                    "source_call_id": "call-spawn-1",
                    "content": f"spawned thread {child_id} for task 'explore'",
                }
            ]
        },
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 100},
    }


def test_single_rollout_with_no_children_is_none_observed(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={"rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines()},
    )
    subagents = build_run_report(trial)["subagents"]

    assert subagents["observability"] == "none_observed"
    assert subagents["count"] == 0
    assert subagents["codex_rollouts"]["status"] == "no_children"
    assert subagents["codex_rollouts"]["parent"].endswith("parent.jsonl")


def test_child_thread_reconstructed_with_own_tokens_and_spawn_link(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5), _spawn_step(2, 10)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(CHILD_SESSION),
            "rollout-2026-08-15T07-02-10-child.jsonl": _child_lines(),
        },
    )
    report = build_run_report(trial)
    subagents = report["subagents"]

    assert subagents["observability"] == "captured"
    assert subagents["count"] == 1
    (item,) = subagents["items"]
    assert item["id"] == CHILD_SESSION
    assert item["evidence"] == "codex_native_rollout"
    assert item["kind"] == "delegated"
    assert item["spawned_at_step"] == subagents["delegation_calls"][0]["step"]
    assert item["model"] == MODEL
    assert item["tool_calls"] == 2
    # Cumulative snapshot maximum, not the sum of per-call figures.
    assert item["input_tokens"] == 9000
    assert item["output_tokens"] == 900
    assert item["cost_usd"] is None
    assert item["location"].endswith("child.jsonl")
    assert [d["tool"] for d in subagents["delegation_calls"]] == ["spawn_agent"]
    assert item["depth"] == 1
    assert item["agent_role"] == CHILD_ROLE
    assert item["agent_nickname"] == CHILD_NICKNAME
    assert subagents["codex_rollouts"]["other_threads"] == []
    assert [f["source"] for f in subagents["codex_rollouts"]["files"]] == [
        "exec",
        "subagent:thread_spawn",
    ]

def test_child_tokens_never_merge_into_run_totals(tmp_path: Path) -> None:
    steps = [_exec_step(1, 5), _spawn_step(2, 10)]
    without = build_run_report(
        _codex_trial(
            tmp_path / "without",
            steps,
            rollouts={"rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(CHILD_SESSION)},
        )
    )
    with_child = build_run_report(
        _codex_trial(
            tmp_path / "with",
            steps,
            rollouts={
                "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(CHILD_SESSION),
                "rollout-2026-08-15T07-02-10-child.jsonl": _child_lines(),
            },
        )
    )

    assert with_child["subagents"]["observability"] == "captured"
    assert with_child["tokens"] == without["tokens"]
    assert with_child["cost"] == without["cost"]
    assert with_child["tokens"]["total"] == 3000 + 300


def test_missing_rollouts_are_unavailable_not_silent(tmp_path: Path) -> None:
    subagents = build_run_report(_codex_trial(tmp_path, [_exec_step(1, 5)]))["subagents"]

    assert subagents["observability"] == "unavailable"
    assert subagents["count"] == 0
    assert "no codex rollout files" in (subagents["reason"] or "")
    assert subagents["codex_rollouts"]["status"] == "absent"


def test_crashed_codex_trial_without_trajectory_is_unavailable(tmp_path: Path) -> None:
    trial = _codex_trial(tmp_path, [_exec_step(1, 5)])
    (trial / "agent" / "trajectory.json").unlink()

    report = build_run_report(trial)

    assert report["availability"]["trajectory"] == "absent"
    assert report["subagents"]["observability"] == "unavailable"
    assert "no codex rollout files" in (report["subagents"]["reason"] or "")


def test_corrupt_rollouts_are_unavailable_with_reason(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={"rollout-2026-08-15T07-02-04-bad.jsonl": "not json\n{broken\n"},
    )
    subagents = build_run_report(trial)["subagents"]

    assert subagents["observability"] == "unavailable"
    assert "bad.jsonl" in (subagents["reason"] or "")
    assert subagents["codex_rollouts"]["status"] == "unreadable"


def test_corrupt_sibling_does_not_hide_a_good_parent(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(),
            "rollout-2026-08-15T07-02-10-bad.jsonl": "not json\n",
        },
    )
    subagents = build_run_report(trial)["subagents"]

    assert subagents["observability"] == "none_observed"
    assert subagents["codex_rollouts"]["status"] == "no_children"
    assert any(not f["readable"] for f in subagents["codex_rollouts"]["files"])


def test_v2_spawn_output_without_agent_id_keeps_no_spawn_step(tmp_path: Path) -> None:
    parent = _parent_lines()
    parent.insert(
        -1,
        _call(
            "spawn_agent",
            "call-spawn-1",
            {"task_name": "explore", "message": "survey the repo"},
            "2026-08-15T07:02:10.000Z",
            kind="function_call",
        ),
    )
    parent.insert(
        -1,
        # V2 spawn_agent outputs carry only task_name/nickname: no id match
        # is possible, so the edge-linked child keeps no spawn step.
        _output(
            "call-spawn-1",
            json.dumps({"task_name": "explore", "nickname": CHILD_NICKNAME}),
            "2026-08-15T07:02:11.000Z",
            kind="function_call_output",
        ),
    )
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5), _spawn_step(2, 10)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": parent,
            "rollout-2026-08-15T07-02-10-child.jsonl": _child_lines(),
        },
    )
    subagents = build_run_report(trial)["subagents"]

    assert subagents["observability"] == "captured"
    (item,) = subagents["items"]
    assert item["id"] == CHILD_SESSION
    assert item["spawned_at_step"] is None
    assert item["input_tokens"] == 9000


def test_non_codex_trials_ignore_rollout_absence(tmp_path: Path) -> None:
    trial = _codex_trial(tmp_path, [_exec_step(1, 5)])
    doc = json.loads((trial / "agent" / "trajectory.json").read_text(encoding="utf-8"))
    doc["agent"]["name"] = "mini-swe-agent"
    (trial / "agent" / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")

    subagents = build_run_report(trial)["subagents"]

    assert subagents["observability"] == "none_observed"
    assert subagents["codex_rollouts"] is None


def test_reader_matches_parent_by_session_id_and_routes_other_thread(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(),
            # Lexically greatest but not the ATIF session, and not a
            # thread_spawn child: must land in other_threads, never children.
            "rollout-2026-08-15T09-00-00-zzz.jsonl": [
                _meta("other-thread", "2026-08-15T09:00:00.000Z"),
                _turn_context("2026-08-15T09:00:01.000Z"),
            ],
        },
    )
    read = read_codex_rollouts(trial, parent_session_id=PARENT_SESSION)

    assert read.status == "no_children"
    assert "other_threads" in (read.reason or "")
    assert read.parent is not None and read.parent.endswith("parent.jsonl")
    assert read.children == ()
    (other,) = read.other_threads
    assert other["thread_id"] == "other-thread"
    assert other["source"] == "exec"
    assert [f["source"] for f in read.files] == ["exec", "exec"]


def test_markdown_names_unavailable_state_and_native_child(tmp_path: Path) -> None:
    missing = render_run_report_markdown(
        build_run_report(_codex_trial(tmp_path / "m", [_exec_step(1, 5)]))
    )
    assert "Subagent activity unavailable:" in missing
    assert "Native codex rollouts: absent" in missing

    trial = _codex_trial(
        tmp_path / "c",
        [_exec_step(1, 5), _spawn_step(2, 10)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(CHILD_SESSION),
            "rollout-2026-08-15T07-02-10-child.jsonl": _child_lines(),
        },
    )
    rendered = render_run_report_markdown(build_run_report(trial))
    assert "Subagent activity captured in the trajectory." in rendered
    assert "codex_native_rollout" in rendered
    assert CHILD_SESSION in rendered


def test_source_classification_only_thread_spawn_with_parent_is_a_child() -> None:
    summary, edge = _classify_source({"source": "exec"})
    assert (summary, edge) == ("exec", None)

    summary, edge = _classify_source({"source": {"subagent": "review"}})
    assert (summary, edge) == ("subagent:review", None)

    summary, edge = _classify_source({"source": _spawn_source()})
    assert summary == "subagent:thread_spawn"
    assert edge is not None and edge["parent_thread_id"] == PARENT_SESSION
    assert edge["depth"] == 1

    orphan = _spawn_source()
    del orphan["subagent"]["thread_spawn"]["parent_thread_id"]
    summary, edge = _classify_source({"source": orphan})
    assert (summary, edge) == ("subagent:thread_spawn_unlinked", None)

    summary, edge = _classify_source({"source": {"subagent": {"thread_spawn": "x"}}})
    assert (summary, edge) == ("subagent:thread_spawn_malformed", None)

    assert _classify_source({}) == (None, None)
    assert _classify_source({"source": ["exec"]}) == ("unparsable", None)


def test_thread_spawn_for_another_parent_is_not_a_child(tmp_path: Path) -> None:
    lines = _child_lines()
    lines[0]["payload"]["source"] = _spawn_source(parent="some-other-parent")
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(),
            "rollout-2026-08-15T07-02-10-runaway.jsonl": lines,
        },
    )
    read = read_codex_rollouts(trial, parent_session_id=PARENT_SESSION)

    assert read.status == "no_children"
    assert read.children == ()
    (other,) = read.other_threads
    assert other["thread_id"] == CHILD_SESSION
    assert other["source"] == "subagent:thread_spawn"


def test_markdown_lists_other_native_threads(tmp_path: Path) -> None:
    trial = _codex_trial(
        tmp_path,
        [_exec_step(1, 5)],
        rollouts={
            "rollout-2026-08-15T07-02-04-parent.jsonl": _parent_lines(),
            "rollout-2026-08-15T09-00-00-zzz.jsonl": [
                _meta("other-thread", "2026-08-15T09:00:00.000Z"),
                _turn_context("2026-08-15T09:00:01.000Z"),
            ],
        },
    )
    rendered = render_run_report_markdown(build_run_report(trial))

    assert "Other native threads (not delegated spend): other-thread (exec)." in rendered
