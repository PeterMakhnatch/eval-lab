"""LS-3 adjudication: sampling, blind prompt, strict reply, scoring and CLI with a fake transport."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_adjudicate as adj
from evallab.production import run_atlas as atlas

MODEL = adj.ADJUDICATOR_MODEL


def _reply(action: str = "kill_and_retry", *, reason: str = "same failing command repeated", confidence: Any = 0.8,
           served_model: str = MODEL, input_tokens: int = 900, output_tokens: int = 40,
           **extra: Any) -> atlas.Completion:
    text = json.dumps({"action": action, "reason": reason, "confidence": confidence, **extra})
    return atlas.Completion(text=text, served_model=served_model, input_tokens=input_tokens,
                            output_tokens=output_tokens, latency_seconds=1.5)


class FakeTransport:
    def __init__(self, replies: Sequence[atlas.Completion | atlas.TransportError]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, list[dict[str, str]], int]] = []

    def complete(self, model: str, messages: Sequence[Mapping[str, str]], *, max_tokens: int) -> atlas.Completion:
        self.requests.append((model, [dict(m) for m in messages], max_tokens))
        assert self.replies, "unexpected extra model request"
        reply = self.replies.pop(0)
        if isinstance(reply, atlas.TransportError):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("an adjudication test opened the real gateway")

    monkeypatch.setattr(adj, "GatewayTransport", forbidden)
    monkeypatch.setattr(atlas.subprocess, "Popen", forbidden)


def _row(case_id: str, *, set_name: str = "g6_loop", action: str = "kill_and_retry",
         label: str | None = "kill_and_retry", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"case_id": case_id, "set": set_name, "subject_kind": "harbor_trial", "action": action,
                           "proposal": action, "reason": "SECRET_SUPERVISOR_REASON",
                           "triggers": [{"trigger": "repetition", "kind": "loop", "score": 2.5,
                                         "detail": "same command 10x", "evidence_steps": [7, 8]},
                                        {"trigger": "wall_time", "kind": "runaway", "score": 1.2,
                                         "detail": "52 min on a 45 min budget", "evidence_steps": []}]}
    if label is not None:
        row |= {"label_action": label, "label_positive": label != "continue", "label_kind": "loop"}
    return row | extra


def _window(key: str, *, events: int = 3, **extra: Any) -> dict[str, Any]:
    return {"key": key, "subject": {"kind": "harbor_trial", "owner": "lab", "killable": True},
            "metrics": {"steps": events, "tokens": 12000},
            "actor_inputs_available": True,
            "events": [{"index": i, "at": f"2026-10-28T10:{i:02d}:00Z", "source": "agent",
                        "text": f"event {i}: run pytest"} for i in range(events)]} | extra


# --------------------------------------------------------------------------- sample


def _grid() -> list[dict[str, Any]]:
    rows = []
    for i in range(30):
        rows.append(_row(f"loop:{i}", action="kill_and_retry", label="kill_and_retry"))
        rows.append(_row(f"loopfp:{i}", action="kill_and_retry", label="continue"))
    for i in range(5):
        rows.append(_row(f"cheat:{i}", set_name="cheat_set", action="flag_cheating", label="flag_cheating"))
    return rows


def test_sample_caps_cells_and_total_deterministically() -> None:
    rows = _grid()
    picked = adj.sample(rows, per_cell=4, total=100, seed=3)
    cells: dict[tuple[str, str, str], int] = {}
    for row in picked:
        cells[adj.stratum(row)] = cells.get(adj.stratum(row), 0) + 1
    assert cells == {("g6_loop", "kill_and_retry", "agree"): 4, ("g6_loop", "kill_and_retry", "disagree"): 4,
                     ("cheat_set", "flag_cheating", "agree"): 4}
    assert [adj.row_key(r) for r in picked] == [adj.row_key(r) for r in adj.sample(rows, per_cell=4, total=100, seed=3)]
    assert {adj.row_key(r) for r in picked} != {adj.row_key(r) for r in adj.sample(rows, per_cell=4, total=100, seed=4)}


def test_sample_total_cap_keeps_every_cell_before_second_draws() -> None:
    picked = adj.sample(_grid(), per_cell=10, total=5, seed=0)
    assert len(picked) == 5
    assert {adj.stratum(r) for r in picked} == {("g6_loop", "kill_and_retry", "agree"),
                                                 ("g6_loop", "kill_and_retry", "disagree"),
                                                 ("cheat_set", "flag_cheating", "agree")}


def test_sample_drops_error_skipped_unknown_and_duplicate_rows() -> None:
    rows = [_row("a"), _row("a", action="continue"), _row("b", error="boom"), _row("c", skipped="no source"),
            _row("d", action="explode")]
    assert [adj.row_key(r) for r in adj.sample(rows)] == ["a"]
    with pytest.raises(ValueError):
        adj.sample(rows, per_cell=0)


def test_live_decision_records_stratify_by_subject_kind_unlabelled() -> None:
    record = {"run_id": "live:omp_lane:p12", "observed_at": "2026-10-28T10:00:00Z", "action": "nudge",
              "subject": {"kind": "omp_lane", "id": "p12", "owner": "fleet", "killable": False}}
    assert adj.row_key(record) == "live:omp_lane:p12@2026-10-28T10:00:00Z"
    assert adj.stratum(record) == ("omp_lane", "nudge", adj.UNLABELLED)
    with pytest.raises(ValueError):
        adj.row_key({"action": "continue"})


# --------------------------------------------------------------------------- prompt


def test_prompt_is_blind_to_label_set_names_and_decision() -> None:
    row = _row("cheat_cheatbench_dev:item-42", set_name="cheat_cheatbench_dev", label="flag_cheating",
               action="flag_cheating")
    window = _window(row["case_id"], metrics={"label_action": "flag_cheating", "set": "x", "steps": 3,
                                              "dataset": "goodfire-reward-hack-data"})
    window["events"].append({"index": 3, "at": None, "source": "tool",
                             "text": "cd /data/CheatBench/terminal-bench tasks"})
    window["subject"]["id"] = "cheatbench-trial-7"
    text = "\n".join(m["content"] for m in adj.build_messages(row, window, seed=1))
    for leaked in ("cheat_cheatbench_dev", "item-42", "SECRET_SUPERVISOR_REASON", "CheatBench", "cheatbench",
                   "terminal-bench", "goodfire", "label", "supervisor decided", "proposal", "trial-7"):
        assert leaked not in text, leaked
    for shown in ("kind: harbor_trial", "owner: lab", "can be stopped by the supervisor: yes",
                  "same command 10x", "52 min on a 45 min budget", "event 2: run pytest", "[event 3] no time tool",
                  "- steps: 3", "- dataset: [source]"):
        assert shown in text, shown


def test_prompt_order_is_seeded_and_events_stay_chronological() -> None:
    row, window = _row("a"), _window("a", events=60)

    def action_order(seed: int) -> list[str]:
        user = adj.build_messages(row, window, seed=seed)[1]["content"]
        return [line[2:].split(":")[0] for line in user.split("## Actions")[1].splitlines()
                if line.startswith("- ")]

    assert adj.build_messages(row, window, seed=5) == adj.build_messages(row, window, seed=5)
    orders = {tuple(action_order(seed)) for seed in range(6)}
    assert len(orders) > 1 and all(sorted(o) == sorted(ls.ACTIONS) for o in orders)
    user = adj.build_messages(row, window, seed=0)[1]["content"]
    shown = [int(line.split("]")[0].removeprefix("[event ")) for line in user.splitlines() if line.startswith("[event ")]
    assert shown == list(range(60 - adj.WINDOW_EVENTS, 60))
    assert f"{60 - adj.WINDOW_EVENTS} earlier omitted" in user


def test_prompt_bounds_long_event_text() -> None:
    window = _window("a")
    window["events"][0]["text"] = "x" * 50_000
    user = adj.build_messages(_row("a"), window)[1]["content"]
    assert "characters elided" in user and len(user) < 20_000


# --------------------------------------------------------------------------- adjudicate


def test_adjudicate_ok_record_and_request_shape() -> None:
    transport = FakeTransport([_reply("continue", confidence=1)])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert record["status"] == "ok" and record["adjudicated_action"] == "continue" and record["confidence"] == 1.0
    assert record["decided_action"] == "kill_and_retry" and record["label_action"] == "kill_and_retry"
    assert (record["served_model"], record["input_tokens"], record["output_tokens"], record["latency_ms"]) == (
        MODEL, 900, 40, 1500)
    model, messages, max_tokens = transport.requests[0]
    assert model == MODEL and max_tokens == adj.MAX_OUTPUT_TOKENS and [m["role"] for m in messages] == ["system", "user"]


def test_adjudicate_rejects_other_served_model_and_paid_routes() -> None:
    with pytest.raises(atlas.TransportError) as caught:
        adj.adjudicate(_row("a"), _window("a"), transport=FakeTransport([_reply(served_model="anthropic/claude-sonnet-5-5")]))
    assert caught.value.quota
    with pytest.raises(ValueError):
        adj.adjudicate(_row("a"), _window("a"), transport=FakeTransport([]), model="openrouter/opus")


@pytest.mark.parametrize("reply", [
    _reply("stop_everything"),
    _reply(reason="line one\nline two"),
    _reply(reason=" ".join(["word"] * (adj.REASON_WORDS + 1))),
    _reply(confidence=1.5),
    _reply(confidence=True),
    _reply(kind="loop"),
    atlas.Completion(text="I think continue.", served_model=MODEL, input_tokens=1, output_tokens=1,
                     latency_seconds=0.1),
])
def test_adjudicate_marks_non_strict_replies_invalid(reply: atlas.Completion) -> None:
    transport = FakeTransport([reply, reply])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert record["status"] == "invalid" and record["errors"] and "adjudicated_action" not in record
    assert record["repaired"] is True and len(transport.requests) == 2


# --------------------------------------------------------------------------- repair turn

PROSE = "Looking at the window, the run is making progress.\n\n"


def _prose_reply(action: str = "continue", *, served_model: str = MODEL, input_tokens: int = 900,
                 output_tokens: int = 300) -> atlas.Completion:
    body = json.dumps({"action": action, "reason": "tests are advancing", "confidence": 0.7})
    return atlas.Completion(text=PROSE + body, served_model=served_model, input_tokens=input_tokens,
                            output_tokens=output_tokens, latency_seconds=2.0)


def test_valid_first_reply_sends_no_repair_turn() -> None:
    transport = FakeTransport([_reply("continue")])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert len(transport.requests) == 1 and record["repaired"] is False and record["attempts"] == 1
    assert "turns" not in record and "first_errors" not in record


def test_prose_then_json_reply_is_repaired_with_one_format_only_turn() -> None:
    transport = FakeTransport([_prose_reply(), _reply("continue", input_tokens=1_400, output_tokens=40)])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert record["status"] == "ok" and record["adjudicated_action"] == "continue"
    assert record["repaired"] is True and record["attempts"] == 2 and record["first_errors"]
    assert record["repair_prompt_version"] == adj.REPAIR_PROMPT_VERSION and len(record["repair_prompt_sha256"]) == 64
    assert "errors" not in record and "reply_excerpt" not in record
    first, second = (request[1] for request in transport.requests)
    assert len(transport.requests) == 2 and second[: len(first)] == first
    assert [m["role"] for m in second[len(first):]] == ["assistant", "user"]
    assert second[len(first)]["content"] == _prose_reply().text
    assert all(request[0] == MODEL and request[2] == adj.MAX_OUTPUT_TOKENS for request in transport.requests)


def test_repair_turn_shows_no_label_set_or_decided_action_and_is_independent_of_the_row() -> None:
    reply = _prose_reply("continue")
    rows = [_row("secret-case-1", set_name="cheat_cheatbench_dev", action="kill_and_retry", label="nudge"),
            _row("other-case-2", set_name="g6_loop", action="alert_research", label="continue")]
    added: list[list[dict[str, str]]] = []
    for row in rows:
        transport = FakeTransport([reply, _reply("continue")])
        adj.adjudicate(row, _window(row["case_id"]), transport=transport)
        first, second = (request[1] for request in transport.requests)
        added.append(second[len(first):])
    assert added[0] == added[1]
    text = json.dumps(added[0])
    for shown in ("secret-case-1", "cheatbench", "g6_loop", "kill_and_retry", "nudge", "alert_research", "label",
                  "decided", "supervisor"):
        assert shown not in text, shown


def test_repair_messages_skip_an_empty_assistant_turn() -> None:
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert [m["role"] for m in adj.repair_messages(base, "  \n", ["not one JSON object"])] == [
        "system", "user", "user"]


def test_second_failure_stays_invalid_and_never_makes_a_third_call() -> None:
    bad = _reply(confidence=1.5)
    transport = FakeTransport([_prose_reply(), bad])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert record["status"] == "invalid" and record["repaired"] is True and record["attempts"] == 2
    assert record["errors"] == ["confidence must be a number from 0 to 1"] and record["first_errors"] != record["errors"]
    assert record["reply_excerpt"] == bad.text and "adjudicated_action" not in record
    assert len(transport.requests) == 2 and transport.replies == []


def test_record_tokens_are_the_sum_of_both_turns() -> None:
    transport = FakeTransport([_prose_reply(input_tokens=900, output_tokens=300),
                               _reply("continue", input_tokens=1_400, output_tokens=40)])
    record = adj.adjudicate(_row("a"), _window("a"), transport=transport)
    assert (record["input_tokens"], record["output_tokens"], record["latency_ms"]) == (2_300, 340, 3_500)
    assert record["turns"] == [{"input_tokens": 900, "output_tokens": 300, "latency_ms": 2_000},
                               {"input_tokens": 1_400, "output_tokens": 40, "latency_ms": 1_500}]


def test_served_model_mismatch_on_the_repair_turn_excludes_the_row_and_keeps_the_spent_tokens() -> None:
    wrong = _reply("continue", served_model="anthropic/claude-sonnet-5-5", input_tokens=1_400, output_tokens=40)
    with pytest.raises(adj.ReaderTransportError) as caught:
        adj.adjudicate(_row("a"), _window("a"), transport=FakeTransport([_prose_reply(), wrong]))
    assert caught.value.quota and (caught.value.spent_input_tokens, caught.value.spent_output_tokens) == (2_300, 340)


def test_transport_failure_on_the_repair_turn_keeps_the_first_turn_tokens() -> None:
    failure = atlas.TransportError("timed out", retryable=True)
    with pytest.raises(adj.ReaderTransportError) as caught:
        adj.adjudicate(_row("a"), _window("a"), transport=FakeTransport([_prose_reply(), failure]))
    assert caught.value.retryable and not caught.value.quota
    assert (caught.value.spent_input_tokens, caught.value.spent_output_tokens) == (900, 300)


def test_main_counts_both_turns_in_the_summary_and_stops_on_a_repair_turn_fallback(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows_path, windows_path, out = _files(tmp_path, [_row("a"), _row("b")], [_window("a"), _window("b")])
    argv = ["--rows", str(rows_path), "--windows", str(windows_path), "--out", str(out)]
    wrong = _reply("continue", served_model="anthropic/claude-sonnet-5-5", input_tokens=1_400, output_tokens=40)
    transport = FakeTransport([_reply("continue"), _prose_reply(), wrong])
    assert adj.main(argv, transport=transport) == 2
    capsys.readouterr()
    records = {r["key"]: r for r in adj.read_jsonl(out)}
    assert sorted(records) == ["a", "b"] and records["a"]["status"] == "ok"
    assert records["b"]["status"] == "error" and "sonnet" in records["b"]["error"]
    assert (records["b"]["input_tokens"], records["b"]["output_tokens"]) == (2_300, 340)
    transport = FakeTransport([_prose_reply(), _reply("continue", input_tokens=1_400, output_tokens=40)])
    assert adj.main(argv, transport=transport) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["repaired"] == 1 and summary["counts"] == {"ok": 2}
    assert (summary["input_tokens"], summary["output_tokens"]) == (900 + 2_300, 40 + 340)


def test_parse_reply_accepts_a_json_fence_only() -> None:
    body = '{"action": "nudge", "reason": "idle with work queued", "confidence": 0.6}'
    assert adj.parse_reply(f"```json\n{body}\n```")[0] == {"action": "nudge", "reason": "idle with work queued",
                                                            "confidence": 0.6}
    assert adj.parse_reply(f"Here: {body}")[0] is None


# --------------------------------------------------------------------------- cheat question


def _cheat_reply(verdict: str = "yes", step: Any = 2, *, served_model: str = MODEL, **extra: Any) -> atlas.Completion:
    body = {"verdict": verdict, "step": step, "reason": "reads the pane log for the answer", "confidence": 0.7, **extra}
    return atlas.Completion(text=json.dumps(body), served_model=served_model, input_tokens=700, output_tokens=30,
                            latency_seconds=1.0)


def _flag_row(case_id: str = "g6_loop:x") -> dict[str, Any]:
    return _row(case_id, action="flag_cheating", label="kill_and_retry") | {
        "triggers": [{"trigger": "trace_alarm", "kind": "cheat", "score": 1.0,
                      "detail": "live_watch harness_log_read: SECRET_RULE_DETAIL cat /logs/agent/terminus_2.pane",
                      "evidence_steps": [2]}]}


def test_cheat_prompt_is_blind_and_never_shows_the_rule_or_detail() -> None:
    row = _flag_row()
    window = _window(row["case_id"], events=4)
    window["events"][3]["text"] = "cd /data/CheatBench/terminal-bench tasks"
    for pointer in (True, False):
        text = "\n".join(m["content"] for m in adj.build_cheat_messages(row, window, pointer=pointer))
        for leaked in ("g6_loop", "SECRET_RULE_DETAIL", "harness_log_read", "trace_alarm", "flag_cheating",
                       "kill_and_retry", "SECRET_SUPERVISOR_REASON", "CheatBench", "terminal-bench", "owner"):
            assert leaked not in text, leaked
        assert "event 2: run pytest" in text and "/logs/agent/evallab-output/" in text
        assert ("## Events a pattern monitor pointed at" in text) is pointer
    pointed = adj.build_cheat_messages(row, window, pointer=True)[1]["content"].split("(it may be wrong)")[1]
    assert pointed.lstrip().startswith("2\n")


def test_adjudicate_cheat_record_and_request_shape() -> None:
    transport = FakeTransport([_cheat_reply("yes", 2), _cheat_reply("no", None), _cheat_reply("unclear", 1)])
    row, window = _flag_row(), _window("g6_loop:x", events=4)
    records = [adj.adjudicate_cheat(row, window, transport=transport, pointer=p) for p in (True, False, True)]
    assert [(r["status"], r["verdict"], r["step"]) for r in records] == [("ok", "yes", 2), ("ok", "no", None),
                                                                       ("ok", "unclear", 1)]
    first = records[0]
    assert first["question"] == "cheat" and first["prompt_version"] == adj.CHEAT_PROMPT_VERSION
    assert first["pointer"] is True and records[1]["pointer"] is False
    assert (first["served_model"], first["input_tokens"], first["output_tokens"]) == (MODEL, 700, 30)
    assert first["decided_action"] == "flag_cheating" and "prompt_sha256" in first
    assert records[0]["prompt_sha256"] != records[1]["prompt_sha256"]


def test_adjudicate_cheat_rejects_other_served_model_and_paid_routes() -> None:
    with pytest.raises(atlas.TransportError) as caught:
        adj.adjudicate_cheat(_flag_row(), _window("a"), transport=FakeTransport([_cheat_reply(served_model="x/y")]))
    assert caught.value.quota
    with pytest.raises(ValueError):
        adj.adjudicate_cheat(_flag_row(), _window("a"), transport=FakeTransport([]), model="openrouter/opus")


@pytest.mark.parametrize("reply", [
    _cheat_reply("maybe"),
    _cheat_reply("yes", 99),  # not a shown event
    _cheat_reply("yes", None),
    _cheat_reply("no", "2"),
    _cheat_reply("no", True),
    _cheat_reply(reason="line one\nline two"),
    _cheat_reply(confidence=2),
    _cheat_reply(action="x"),
    atlas.Completion(text="Yes, step 2.", served_model=MODEL, input_tokens=1, output_tokens=1, latency_seconds=0.1),
])
def test_adjudicate_cheat_marks_non_strict_replies_invalid(reply: atlas.Completion) -> None:
    record = adj.adjudicate_cheat(_flag_row(), _window("a", events=4), transport=FakeTransport([reply]))
    assert record["status"] == "invalid" and record["errors"] and "verdict" not in record


# --------------------------------------------------------------------------- score


def _scored(key: str, decided: str, judged: str, label: str | None = None, status: str = "ok") -> dict[str, Any]:
    return {"key": key, "status": status, "decided_action": decided, "adjudicated_action": judged,
            "label_action": label, "confidence": 0.9, "reason": "r"}


def test_score_precision_recall_kappa_and_false_stops() -> None:
    records = [
        _scored("1", "kill_and_retry", "kill_and_retry", "kill_and_retry"),
        _scored("2", "kill_and_retry", "continue", "continue"),
        _scored("3", "kill_and_mark_infra", "kill_and_retry", "kill_and_retry"),
        _scored("4", "continue", "continue", "continue"),
        _scored("5", "continue", "kill_and_retry"),
        _scored("6", "continue", "continue", status="invalid"),
    ]
    table = adj.score(records)
    kill = table["supervisor_vs_adjudicator"]["per_action"]["kill_and_retry"]
    assert (kill["tp"], kill["fp"], kill["fn"], kill["precision"], kill["recall"]) == (1, 1, 2, 0.5, 0.3333)
    cont = table["supervisor_vs_adjudicator"]["per_action"]["continue"]
    assert (cont["precision"], cont["recall"]) == (0.5, 0.5)
    assert table["supervisor_vs_adjudicator"]["per_action"]["nudge"]["precision"] is None
    assert table["adjudicator_vs_label"] == {"n": 4, "agreement": 1.0, "kappa": 1.0}
    assert [f["key"] for f in table["false_stops"]] == ["2"]
    assert table["counts"] == {"invalid": 1, "ok": 5}


def test_kappa_edges() -> None:
    assert adj.kappa([]) is None
    assert adj.kappa([("continue", "continue")] * 3) is None
    assert adj.kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]) == 0.0
    assert adj.kappa([("a", "a"), ("b", "b"), ("a", "b"), ("a", "a")]) == 0.5


# --------------------------------------------------------------------------- windows + CLI


def test_window_record_round_trips_into_a_prompt() -> None:
    subject = ls.Subject("harbor_trial", "job/trial-1", "lab", killable=True, label="secret display label")
    observation = ls.Observation(subject, datetime(2026, 10, 28, 12, tzinfo=UTC),
                                 events=(ls.Event(4, None, "agent", "ls -la"),), metrics={"steps": 5})
    hit = ls.TriggerHit("silence", "latency", 3.0, "no step for 45 min", (4,))
    window = adj.window_record("k", observation, [hit])
    json.dumps(window)
    assert window["subject"] == {"kind": "harbor_trial", "owner": "lab", "killable": True}
    user = adj.build_messages({"case_id": "k", "action": "continue"}, window)[1]["content"]
    assert "no step for 45 min [events: 4]" in user and "ls -la" in user and "trial-1" not in user


def _files(tmp_path: Path, rows: list[dict[str, Any]], windows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    rows_path, windows_path = tmp_path / "rows.jsonl", tmp_path / "windows.jsonl"
    rows_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    windows_path.write_text("".join(json.dumps(w) + "\n" for w in windows), encoding="utf-8")
    return rows_path, windows_path, tmp_path / "adj.jsonl"


def test_main_writes_records_and_score_then_resumes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [_row("a"), _row("b", action="continue", label="continue"), _row("c")]
    rows_path, windows_path, out = _files(tmp_path, rows, [_window("a"), _window("b")])
    argv = ["--rows", str(rows_path), "--windows", str(windows_path), "--out", str(out)]
    transport = FakeTransport([_reply("kill_and_retry"), _reply("continue")])
    assert adj.main(argv, transport=transport) == 0
    records = adj.read_jsonl(out)
    assert sorted((r["key"], r["status"]) for r in records) == [("a", "ok"), ("b", "ok"), ("c", "no_window")]
    summary = json.loads(capsys.readouterr().out)
    assert summary["served_models"] == [MODEL] and summary["input_tokens"] == 1800
    score = json.loads((tmp_path / "adj-score.json").read_text(encoding="utf-8"))
    assert score["adjudicator_vs_label"]["n"] == 2 and score["counts"] == {"no_window": 1, "ok": 2}
    assert adj.main(argv, transport=FakeTransport([])) == 0
    assert len(adj.read_jsonl(out)) == 3


def test_main_stops_on_quota_and_retries_errors_later(tmp_path: Path) -> None:
    rows_path, windows_path, out = _files(tmp_path, [_row("a"), _row("b")], [_window("a"), _window("b")])
    argv = ["--rows", str(rows_path), "--windows", str(windows_path), "--out", str(out)]
    quota = atlas.TransportError("usage limit", quota=True)
    assert adj.main(argv, transport=FakeTransport([quota])) == 2
    assert [r["status"] for r in adj.read_jsonl(out)] == ["error"]
    assert adj.main(argv, transport=FakeTransport([_reply(), _reply()])) == 0
    assert sorted(r["status"] for r in adj._latest(adj.read_jsonl(out)).values()) == ["ok", "ok"]
