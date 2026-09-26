"""HAR-76 harness gaps: Claude Code sidechains, OpenCode non-MCP status, Reef native text.

Real-data anchors:
- Claude Code: ``tests/fixtures/claude-sidechain/trial-sidechain`` — a trial
  dir whose ``agent/trajectory.json`` is REAL Harbor 0.21 converter output
  (installed converter run over a synthetic raw session with the real key
  layout: camelCase ``agentId``, uuid, no top-level event id; raw session
  retained under ``agent/sessions/`` exactly as Harbor retains it) and whose
  sidechain steps interleave agent-a/b/a. No real Claude Code trial exists
  in retained evidence; the producing spec is
  ``research/experiments/specs/04-claude-code-canary/event-summary.json``
  (billable, needs Peter's approval — never run here).
- OpenCode (read-only, tracked under ``research/evidence/runs``):
  ``gepa-zai-opencode-event-summary-e41a67fdd138b615317db74e/``
  ``gepa-zai-opencode-event-summary__u6EdsRJ`` step 5 (``git: command not
  found``); ``gepa-zai-opencode-event-summary-022c925c5324f95ce93ab434/``
  ``gepa-zai-opencode-event-summary__pQzeT4t`` step 5 (``xxd: command not
  found`` yet validation printed ``VALID``); ``read`` outputs with
  ``{"kind": "error"}`` event rows.
- Reef: real ATIF at ``derived/har73/control-intake-verified/trajectories``
  (har72-reef-gate worktree, read-only): 150 files, 444 steps, zero
  timestamps, no cost, 142 ``execute`` + 2 ``run_bash`` results — all
  ``exit N`` prefixed. The text-only forms below (``timed out after 60s``,
  ``refused: <reason>``) are the exact strings Reef's native seed tools
  return (``reef/harness/runners/native/seed.py``) with no exit code or flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation.run_report import (
    build_run_report,
    render_run_report_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude-sidechain"
EVIDENCE = Path(__file__).parent.parent / "research" / "evidence" / "runs"
OPENCODE_FAILED = (
    EVIDENCE
    / "gepa-zai-opencode-event-summary-e41a67fdd138b615317db74e"
    / "gepa-zai-opencode-event-summary__u6EdsRJ"
)
OPENCODE_PARTIAL = (
    EVIDENCE
    / "gepa-zai-opencode-event-summary-022c925c5324f95ce93ab434"
    / "gepa-zai-opencode-event-summary__pQzeT4t"
)


def _extra(step: dict[str, Any]) -> dict[str, Any]:
    extra = step.get("extra")
    return extra if isinstance(extra, dict) else {}


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "trial_name": "trial",
        "task_name": "lab/task",
        "config": {"agent": {"name": "opencode"}},
        "agent_info": {"name": "opencode", "version": "test"},
        "agent_result": {},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-09-01T00:00:00Z",
        "finished_at": "2026-09-01T00:10:00Z",
    }
    result.update(overrides)
    return result


def _step(
    step_id: int,
    tool: str,
    args: dict[str, Any],
    content: str,
    *,
    extra: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    call_id = f"c{step_id}-{tool}"
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": call_id, "function_name": tool, "arguments": args}
        ],
        "observation": {
            "results": [{"source_call_id": call_id, "content": content}]
        },
    }
    if timestamp is not None:
        step["timestamp"] = timestamp
    if extra is not None:
        step["extra"] = extra
    return step


def _trial(
    root: Path,
    steps: list[dict[str, Any]],
    *,
    name: str = "trial",
    result: dict[str, Any] | None = None,
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    body = result if result is not None else _result()
    body = {**body, "trial_name": name}
    (trial / "result.json").write_text(json.dumps(body), encoding="utf-8")
    doc: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "test", "version": "test"},
        "steps": steps,
    }
    (agent / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    return trial


def _reef_trial(root: Path, steps: list[dict[str, Any]], name: str = "reef") -> Path:
    """A Reef-shaped trial: no timestamps, no cost, `execute` tool, text-only results."""
    return _trial(
        root,
        steps,
        name=name,
        result=_result(
            config={"agent": {"name": "reef-harness"}},
            agent_info={"name": "reef-harness", "version": "test"},
            started_at=None,
            finished_at=None,
        ),
    )


def _sidechain(step_id: int, command: str, agent_key: str, value: str) -> dict[str, Any]:
    return _step(
        step_id,
        "bash",
        {"command": command},
        "src/a.py: TODO",
        extra={"is_sidechain": True, agent_key: value},
        timestamp=f"2026-09-01T00:02:{step_id * 5:02d}Z",
    )

def test_sidechain_session_join_attributes_real_converter_output() -> None:
    """Interleaved sidechain steps from REAL Harbor 0.21 converter output
    (tests/fixtures/claude-sidechain/, built by running the installed
    converter over a synthetic raw session with the real key layout:
    camelCase agentId, uuid, no top-level event id).

    Tool turns join by tool_call_id; the text-only agent-a turn joins by its
    unique timestamp; the two same-timestamp turns (one per agent) are
    ambiguous and share one labeled contiguous fallback run. Contiguous runs
    alone would conflate all six into one group.

    The fixture's ATIF sidechain steps carry no per-step agent id and no
    event id at all (locking the real converter shape).
    """
    trial = FIXTURES / "trial-sidechain"
    doc = json.loads((trial / "agent" / "trajectory.json").read_text(encoding="utf-8"))
    sidechain = [
        s for s in doc["steps"]
        if isinstance(s, dict) and _extra(s).get("is_sidechain") is True
    ]
    assert len(sidechain) == 6
    assert all("agent_id" not in _extra(s) for s in sidechain)
    assert all("id" not in _extra(s) for s in sidechain)

    items = build_run_report(trial)["subagents"]["items"]

    assert [(i["id"], i["steps"]) for i in items] == [
        ("agent-a", 3), ("agent-b", 1), ("sidechain-run-1", 2),
    ]
    assert all(i["evidence"] == "claude_code_sidechain" for i in items)
    fallback = items[2]
    assert fallback["tool_calls"] == 0


def test_sidechain_same_agent_reconnects_across_main_steps(tmp_path: Path) -> None:
    main = _step(
        2, "bash", {"command": "ls"}, "a",
        timestamp="2026-09-01T00:02:10Z",
    )
    steps = [
        _sidechain(1, "grep a", "agent_id", "agent-a"),
        main,
        _sidechain(3, "grep b", "agent_id", "agent-a"),
    ]
    items = build_run_report(_trial(tmp_path, steps))["subagents"]["items"]

    assert [(i["id"], i["steps"]) for i in items] == [("agent-a", 2)]


def test_sidechain_anonymous_runs_stay_split(tmp_path: Path) -> None:
    main = _step(
        2, "bash", {"command": "ls"}, "a",
        timestamp="2026-09-01T00:02:10Z",
    )
    steps = [
        _sidechain(1, "grep a", "agent_id", "agent-a"),
        main,
        _step(
            3, "bash", {"command": "grep b"}, "x",
            extra={"is_sidechain": True},
            timestamp="2026-09-01T00:02:15Z",
        ),
    ]
    # Anonymous sidechain steps (no agent key at all) group by contiguous run,
    # never merged into a named agent's group.
    groups = [
        (i["id"], i["steps"])
        for i in build_run_report(_trial(tmp_path, steps))["subagents"]["items"]
    ]

    assert groups == [("agent-a", 1), ("sidechain-run-2", 1)]


def test_opencode_bash_command_not_found_is_error_with_channel(tmp_path: Path) -> None:
    # Exact shape from u6EdsRJ step 5: validation JSON then the shell diagnostic.
    content = (
        '{\n    "schema_version": 1,\n    "total_events": 8\n}\nsummary.json\n'
        '/usr/bin/bash: line 1: git: command not found\n'
    )
    steps = [
        _step(1, "bash", {"command": "ls /app/output"}, "summary.json",
              timestamp="2026-09-01T00:02:05Z"),
        _step(2, "bash", {"command": "git status"}, content,
              timestamp="2026-09-01T00:02:10Z"),
    ]
    report = build_run_report(_trial(tmp_path, steps))

    assert report["errors"]["tool_errors"] == 1
    (example,) = report["errors"]["examples"]
    assert example["step"] == 2
    assert example["tool"] == "bash"
    assert example["evidence"] == "output_text"
    assert example["category"] == "inferred_from_output"
    assert report["errors"]["harness_signalled"] == 0
    assert report["errors"]["inferred_from_output"] == 1


def test_opencode_read_of_error_event_data_stays_unknown(tmp_path: Path) -> None:
    # events.jsonl rows with {"kind": "error"} are data the agent read, not a
    # failed read call (real shape from the opencode event-summary trials).
    content = (
        '7: {"event_id":"evt-007","kind":"error","duration_ms":900}\n'
        "\n(End of file - total 8 lines)\n</content>"
    )
    steps = [
        _step(1, "read", {"filePath": "/app/input/events.jsonl"}, content,
              timestamp="2026-09-01T00:02:05Z"),
        _step(2, "write", {"filePath": "/app/output/summary.json"}, "Wrote file successfully.",
              timestamp="2026-09-01T00:02:10Z"),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    rows = {row["tool"]: row for row in report["tools"]["by_tool"]}

    assert report["errors"]["tool_errors"] == 0
    assert (rows["read"]["ok"], rows["read"]["errors"], rows["read"]["unknown"]) == (0, 0, 1)
    assert (rows["write"]["ok"], rows["write"]["errors"], rows["write"]["unknown"]) == (0, 0, 1)


def test_opencode_embedded_mcp_success_inside_bash_stays_unknown(tmp_path: Path) -> None:
    # A bash call batching MCP JSON-RPC/SSE traffic carries no per-call status:
    # embedded `"isError":false` payloads must not read as harness signals.
    content = (
        'node_0_0:\n{"jsonrpc":"2.0","id":10,"result":{"content":[{"type":"text",'
        '"text":"{\\"status\\":\\"ok\\",\\"value\\":13}"}],"structuredContent":'
        '{"status":"ok","value":13},"isError":false}}\n'
    )
    steps = [
        _step(1, "bash", {"command": "query mcp batch"}, content,
              timestamp="2026-09-01T00:02:05Z"),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    rows = {row["tool"]: row for row in report["tools"]["by_tool"]}

    assert report["errors"]["tool_errors"] == 0
    assert (rows["bash"]["ok"], rows["bash"]["errors"], rows["bash"]["unknown"]) == (0, 0, 1)


def test_opencode_real_trials_name_their_status(tmp_path: Path) -> None:
    del tmp_path  # read-only retained trials, not fixtures under test.
    failed = build_run_report(OPENCODE_FAILED)
    partial = build_run_report(OPENCODE_PARTIAL)

    for report in (failed, partial):
        rows = {row["tool"]: row for row in report["tools"]["by_tool"]}
        assert rows["bash"]["errors"] == 1
        assert report["errors"]["tool_errors"] == 1
        assert report["errors"]["harness_signalled"] == 0
        assert report["errors"]["inferred_from_output"] == 1
        (example,) = report["errors"]["examples"]
        assert example["evidence"] == "output_text"
        assert "command not found" in (example["excerpt"] or "")
        # No per-call harness channel anywhere: everything else stays unknown.
        assert rows["read"]["unknown"] == rows["read"]["calls"]
        assert any("no per-call status" in q for q in report["data_quality"])
        # These trials record no harness cost (steps carry none): the cost is
        # either null with a reason (model not in the price table) or a
        # never-mixed `price_table_estimate` (HAR-76 price slice) — never a
        # harness-sourced figure.
        cost = report["cost"]
        assert cost["harness_cost_source"] is None
        assert cost["steps_with_cost"] == 0
        if cost["source"] == "price_table_estimate":
            table = cost["price_table"]
            assert {"model", "matched_key", "source_url", "retrieved_on",
                    "usd_per_mtok"} <= set(table)
            assert cost["total_usd"] is not None and cost["total_usd"] > 0
        else:
            assert cost["total_usd"] is None
            assert any("cost unavailable" in q for q in report["data_quality"])


def test_reef_trial_timing_and_cost_are_unavailable_not_zero(tmp_path: Path) -> None:
    steps = [
        _step(1, "execute", {"code": "print(1)"}, "1"),
        _step(2, "execute", {"code": "print(2)"}, "2"),
    ]
    report = build_run_report(_reef_trial(tmp_path, steps))
    timing, cost = report["timing"], report["cost"]

    assert timing["total_seconds"] is None
    assert all(phase["seconds"] is None for phase in timing["phases"].values())
    assert timing["steps_with_timestamps"] == 0
    assert timing["agent_span_seconds"] is None
    assert timing["step_gap_seconds"]["count"] == 0
    assert cost["total_usd"] is None
    assert cost["source"] is None
    assert cost["steps_with_cost"] == 0
    assert cost["is_lower_bound"] is False
    assert report["tokens"]["total"] is None
    assert "steps carry no timestamps: per-step timing unavailable" in report["data_quality"]
    assert any("cost unavailable" in q for q in report["data_quality"])
    assert any("no token usage recorded" in q for q in report["data_quality"])

    markdown = render_run_report_markdown(report)
    assert "n/a" in markdown
    assert "unavailable" in markdown
    assert "$0.0000" not in markdown


def test_reef_native_failure_text_is_error_with_channel(tmp_path: Path) -> None:
    # Exact strings Reef's native seed tools return with no exit code or flag
    # (reef/harness/runners/native/seed.py): "timed out after 60s" and
    # "refused: <reason>". Normal results ("exit N", "wrote N …", file text)
    # never lead with these lines.
    steps = [
        _step(1, "execute", {"code": "ok()"}, "exit 0\n1"),
        _step(2, "execute", {"code": "boom()"}, "exit 1\nboom"),
        _step(3, "execute", {"code": "slow()"}, "timed out after 60s"),
        _step(4, "execute", {"code": ""}, "refused: empty code"),
        _step(5, "run_bash", {"command": ""}, "refused: empty command"),
        _step(6, "write_file", {"path": "/x", "content": "y"},
               "refused: path escapes the workspace"),
        _step(7, "execute", {"code": "w()"}, "wrote 41 characters to out.txt"),
        _step(8, "read_file", {"path": "a.txt"}, "file contents here"),
    ]
    report = build_run_report(_reef_trial(tmp_path, steps, name="reef-fail"))
    rows = {row["tool"]: row for row in report["tools"]["by_tool"]}

    assert rows["execute"]["unknown"] == 1
    assert report["errors"]["tool_errors"] == 5
    assert report["errors"]["harness_signalled"] == 1
    assert report["errors"]["inferred_from_output"] == 4
    by_step = {(e["step"], e["tool"]): e for e in report["errors"]["examples"]}
    assert by_step[(2, "execute")]["evidence"] == "exit_prefix"
    assert by_step[(3, "execute")]["evidence"] == "output_text"
    assert by_step[(3, "execute")]["category"] == "timeout"
    assert by_step[(4, "execute")]["evidence"] == "output_text"
    assert by_step[(6, "write_file")]["evidence"] == "output_text"


def test_reef_success_mentioning_failure_words_stays_unknown(tmp_path: Path) -> None:
    steps = [
        _step(1, "execute", {"code": "suite()"}, "Errors: 0\n12 passed"),
        _step(2, "execute", {"code": "tail()"},
               "log line 1\ntimed out after 60s\nretried ok"),
        _step(3, "execute", {"code": "cat()"}, "notes on refused paper\nok"),
    ]
    report = build_run_report(_reef_trial(tmp_path, steps, name="reef-fp"))
    rows = {row["tool"]: row for row in report["tools"]["by_tool"]}

    # Failure words appear but never lead the output: no signal, stay unknown.
    assert (rows["execute"]["ok"], rows["execute"]["errors"], rows["execute"]["unknown"]) == (0, 0, 3)
    assert report["errors"]["tool_errors"] == 0


def test_reef_text_rule_does_not_leak_to_other_tools(tmp_path: Path) -> None:
    steps = [
        _step(1, "bash", {"command": "run"}, "refused: something upstream",
              timestamp="2026-09-01T00:02:05Z"),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    rows = {row["tool"]: row for row in report["tools"]["by_tool"]}

    assert (rows["bash"]["ok"], rows["bash"]["errors"], rows["bash"]["unknown"]) == (0, 0, 1)
