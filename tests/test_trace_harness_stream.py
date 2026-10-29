from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness_stream as stream
from evallab.production.trace_harness import HarnessOptions, HarnessRun, validate_finding

MODEL = stream.JUDGE_MODEL


def _step(number: int, command: str = "pwd", source: str = "agent") -> dict[str, Any]:
    return {
        "step_id": number, "source": source, "message": "",
        "tool_calls": [{"tool_call_id": f"call-{number}", "function_name": "bash",
                        "arguments": {"command": command}}],
        "observation": {"results": [{"content": "ok"}]},
    }


def _flagged_steps(command: str = "git log --oneline") -> list[dict[str, Any]]:
    # A ``git log`` card waits for lookahead (five agent steps) before its window is judged.
    return [_step(1, command), *(_step(n, f"echo step-{n}") for n in range(2, 9))]


def _answer(label: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    data = json.loads(messages[-1]["content"])
    action = next(a for a in data["actions"] if a["step"] == data["flagged_step"])
    return {"label": label, "mechanism": data["candidate"]["rule"],
            "evidence": [{"step": action["step"], "quote": "git log --oneline"}]}


def _positive(messages: list[dict[str, str]]) -> tuple[dict[str, Any], float]:
    return _answer("exploit-pass", messages), 0.0


def _neither(messages: list[dict[str, str]]) -> tuple[dict[str, Any], float]:
    return _answer("neither", messages), 0.0


def _run(run_id: str, steps: list[dict[str, Any]]) -> HarnessRun:
    return HarnessRun(run_id, "synthetic", "DEV", "pass", tuple(steps), True)


def _fake_call(label: str, seen: list[str]) -> stream.Call:
    def call(model: str, system: str, user: str) -> dict[str, Any]:
        seen.append(user)
        return {"ok": True, "attempts": 1, "elapsed_s": 0.25, "requested_model": model,
                "served": model, "fallback": False, "input_tokens": 100, "output_tokens": 7,
                "answer": _answer(label, [{"content": system}, {"content": user}])}

    return call


def test_first_alarm_is_reported_once_with_both_clocks() -> None:
    with stream.CascadeStream("run-a", _positive, wait_for_judge=True) as live:
        returns = [live.ingest(step) for step in _flagged_steps()]
    (alarm,) = live.alarms  # one alarm per card, not one per emitted update
    assert sum(len(raised) for raised in returns) == 1
    assert alarm["kind"] == "confirmed"
    assert alarm["flagged_step"] <= alarm["card_prefix"] <= alarm["emitted_step"]
    assert alarm["turns"] <= alarm["emitted_turns"] <= live.agent_turns
    assert alarm["emitted_step"] == alarm["card_prefix"]  # the wait made the two coincide
    assert alarm["at_eof"] is False


def test_async_judgment_emits_once_and_the_eof_flag_follows_the_returning_call() -> None:
    steps = _flagged_steps()
    with stream.CascadeStream("run-a", _positive) as live:
        from_ingest = [alarm for step in steps for alarm in live.ingest(step)]
        from_poll = live.poll()
        from_close = live.close()
    (alarm,) = live.alarms  # one alarm per card, never one per emitted update
    assert from_ingest + from_poll + from_close == [alarm]
    assert alarm["at_eof"] is (alarm in from_close)
    # The judge thread runs behind the ingest loop, so emission follows the frozen prefix.
    assert alarm["card_prefix"] <= alarm["emitted_step"] <= steps[-1]["step_id"]


def test_agent_turns_ignore_non_agent_steps() -> None:
    steps = [{"step_id": 0, "source": "user", "message": "task"}, *_flagged_steps()]
    live = stream.replay("run-a", steps, _positive)
    (alarm,) = live.alarms
    assert live.agent_turns == 8 and alarm["emitted_turns"] <= 8


def test_pass_through_alarm_needs_no_judge() -> None:
    steps = [_step(1), _step(2, "sed -i 's/x/y/' tests/test_foo.py"), _step(3)]
    (alarm,) = [a for a in stream.replay("run-a", steps, None).alarms if a["rule"] == "grader_tamper"]
    assert alarm["kind"] == "pass-through" and alarm["flagged_step"] == 2


def test_miss_without_allow_model_raises_and_the_run_abstains(tmp_path: Path) -> None:
    judge = stream.StoredVerdictJudge(stream.VerdictCache(None))
    with pytest.raises(stream.JudgeCacheMiss):
        judge([{"content": "s"}, {"content": "u"}])
    detector = stream.CascadeStreamDetector(cache=stream.VerdictCache(tmp_path / "cache.jsonl"))
    (record,) = detector.detect([_run("run-a", _flagged_steps())])
    assert validate_finding(record) == []
    assert (record["verdict"], record["score"], record["evidence_steps"]) == ("abstain", None, [])
    assert detector.runs["run-a"]["triage_unavailable"] == 1
    assert not (tmp_path / "cache.jsonl").exists()  # a miss never writes


def test_stored_verdicts_reproduce_the_alarm_and_a_different_prompt_does_not_reuse_one(
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    cache_path = tmp_path / "cache.jsonl"
    fresh = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True), cache=stream.VerdictCache(cache_path),
        call=_fake_call("exploit-pass", seen),
    )
    (alarmed,) = fresh.detect([_run("run-a", _flagged_steps())])
    assert len(seen) == 1 and fresh.runs["run-a"]["latency_ms"] == 250
    stored = stream.CascadeStreamDetector(cache=stream.VerdictCache(cache_path))
    (record,) = stored.detect([_run("run-a", _flagged_steps())])
    assert (record["verdict"], record["score"], record["evidence_steps"]) == ("alarm", 1.0, [1])
    assert (record["input_tokens"], record["output_tokens"]) == (100, 7)
    assert stored.runs["run-a"]["hits"] == 1 and stored.runs["run-a"]["misses"] == 0
    # A different window is a different prompt: no verdict is borrowed for it.
    (other,) = stored.detect([_run("run-b", _flagged_steps("git log --stat"))])
    assert stored.runs["run-b"]["misses"] == 1 and other["verdict"] == "abstain"


def test_a_neither_verdict_is_clear(tmp_path: Path) -> None:
    detector = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True), cache=stream.VerdictCache(tmp_path / "c.jsonl"),
        call=_fake_call("neither", []),
    )
    (record,) = detector.detect([_run("run-a", _flagged_steps())])
    assert (record["verdict"], record["score"]) == ("clear", 0.0)


def test_cache_refuses_a_verdict_served_by_another_model() -> None:
    rows = [{"key": "k", "ok": True, "served": "other/model", "answer": {}}]
    with pytest.raises(ValueError, match="other/model"):
        stream.VerdictCache(None, rows=rows)
    assert len(stream.VerdictCache(None, rows=[{"key": "k", "ok": False}])) == 0


def test_quota_gate_stops_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_subprocess(*_: Any, **__: Any) -> Any:
        raise AssertionError("omp must not run past the quota gate")

    monkeypatch.setattr(stream.subprocess, "run", no_subprocess)
    row = stream.judge_call(MODEL, "s", "u", gate=stream.QuotaGate(reader=lambda _: 0.85),
                            ledger=tmp_path / "attempts.jsonl")  # fmt: skip
    assert row["ok"] is False and row["errors"][0].startswith("QuotaStop")
    assert json.loads((tmp_path / "attempts.jsonl").read_text())["error"] == "QuotaStop"


def _omp_stream(served: str, text: str, *, tokens: tuple[int, int] = (10, 3)) -> str:
    return json.dumps({
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": "u"},
            {"role": "assistant", "provider": served.split("/")[0], "model": served.split("/")[1],
             "stopReason": "stop", "content": [{"type": "text", "text": text}],
             "usage": {"input": tokens[0], "output": tokens[1]}},
        ],
    }) + "\n"


def test_every_attempt_keeps_its_usage_served_model_and_ledger_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        _omp_stream("other/model", '{"label": "exploit-pass", "evidence": []}', tokens=(11, 2)),
        _omp_stream(MODEL, '{"label": "neither", "mechanism": null, "evidence": []}', tokens=(13, 4)),
    ]
    monkeypatch.setattr(stream.subprocess, "run", lambda *_a, **_k: type(
        "P", (), {"returncode": 0, "stdout": replies.pop(0)})())
    monkeypatch.setattr(stream.time, "sleep", lambda _seconds: None)
    ledger = tmp_path / "attempts.jsonl"
    row = stream.judge_call(MODEL, "s", "u", ledger=ledger)
    assert row["ok"] is True and row["answer"]["label"] == "neither"
    assert (row["input_tokens"], row["output_tokens"]) == (24, 6)  # both attempts, not the last one
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [(r["attempt"], r["ok"], r["served"]) for r in rows] == [
        (1, False, "other/model"), (2, True, MODEL),
    ]


HAIKU = "anthropic/claude-haiku-5-5"


def test_reader_override_labels_the_finding_and_uses_its_own_cache(tmp_path: Path) -> None:
    seen: list[str] = []
    cache_path = tmp_path / "haiku.jsonl"
    detector = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True, reader=HAIKU),
        cache=stream.VerdictCache(cache_path, model=HAIKU), call=_fake_call("exploit-pass", seen),
    )
    (record,) = detector.detect([_run("run-a", _flagged_steps())])
    assert (record["reader_model"], record["verdict"]) == (HAIKU, "alarm")
    assert detector.reader_model == HAIKU and detector.stats.served == {HAIKU: 1}
    assert json.loads(cache_path.read_text())["served"] == HAIKU
    assert stream.reader_cache_path(HAIKU) != stream.reader_cache_path(MODEL) == stream.DEFAULT_CACHE
    assert stream.provider_of(HAIKU) == "anthropic"
    with pytest.raises(ValueError, match="reader is"):
        stream.CascadeStreamDetector(HarnessOptions(reader=HAIKU), cache=stream.VerdictCache(None))


def test_concurrent_replay_keeps_input_order_and_per_run_accounting(tmp_path: Path) -> None:
    seen: list[str] = []
    detector = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True), cache=stream.VerdictCache(tmp_path / "c.jsonl"),
        call=_fake_call("exploit-pass", seen), workers=3,
    )
    runs = [_run(f"run-{n}", _flagged_steps(f"git log --oneline -{n + 1}")) for n in range(6)]
    records = list(detector.detect(runs))
    assert [r["run_id"] for r in records] == [r.run_id for r in runs]
    assert all(detector.runs[r.run_id]["fresh"] == 1 for r in runs)
    assert detector.stats.fresh == 6 and detector.stats.input_tokens == 600


def test_a_quota_stopped_run_yields_no_finding_so_it_is_not_cached_as_abstain(tmp_path: Path) -> None:
    def stopped(model: str, system: str, user: str) -> dict[str, Any]:
        return {"ok": False, "attempts": 1, "errors": ["QuotaStop: anthropic window at 85% (stop at 85%)"],
                "requested_model": model, "input_tokens": 0, "output_tokens": 0, "elapsed_s": 0.0}

    detector = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True, reader=HAIKU),
        cache=stream.VerdictCache(tmp_path / "c.jsonl", model=HAIKU), call=stopped,
    )
    assert list(detector.detect([_run("run-a", _flagged_steps())])) == []
    assert detector.stats.quota_stopped and "run-a" not in detector.runs

    def refused(model: str, system: str, user: str) -> dict[str, Any]:
        return {"ok": False, "attempts": 3, "errors": ["OmpRouteError: refused"], "requested_model": model,
                "input_tokens": 5, "output_tokens": 0, "elapsed_s": 0.0}

    other = stream.CascadeStreamDetector(
        HarnessOptions(allow_model=True, reader=HAIKU),
        cache=stream.VerdictCache(tmp_path / "d.jsonl", model=HAIKU), call=refused,
    )
    (record,) = other.detect([_run("run-a", _flagged_steps())])
    assert record["verdict"] == "abstain"  # a refusal is unknown, kept as abstain; only quota is retried
