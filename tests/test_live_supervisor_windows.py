"""Live windows: each recorded decision leaves a bounded, blinded window the adjudicator reads unchanged.

No network, herdr, modal or model call: sources and deciders are injected fakes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_adjudicate as adj
from evallab.production import live_supervisor_cli as cli
from evallab.production import live_supervisor_windows as lw
from evallab.production.live_supervisor_triage import EVENT_CHARS, WINDOW_EVENTS

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
HIT = ls.TriggerHit("repetition", "loop", 3.0, "8 identical commands", (4,))
NAMES = ("terminal-bench", "CheatBench", "SWE-bench", "Goodfire")


class Fixed:
    """A source that returns fixed observations; every one fires one hit."""

    name = "harbor"

    def __init__(self, *observations: ls.Observation) -> None:
        self.observations = list(observations)

    def observe(self, now: datetime) -> list[ls.Observation]:
        return self.observations

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        return [HIT]


class ModelLike:
    """Overrules the proposal like a model reader: sets ``reader_model``."""

    def decide(self, observation: ls.Observation, hits: Sequence[ls.TriggerHit],
               proposal: ls.Decision) -> ls.Decision:
        return ls.Decision("continue", "other", "progressing", reader_model="anthropic/claude-sonnet-5-5",
                           confidence=0.8)


def _trial(name: str, *, events: int = 3, text: str = "ls -la", metrics: Mapping[str, Any] | None = None,
           at: datetime = NOW) -> ls.Observation:
    subject = ls.Subject("harbor_trial", f"job/{name}", "lab", killable=True)
    steps = tuple(ls.Event(i, NOW, "agent", f"{text} {i}") for i in range(events))
    return ls.Observation(subject, at, steps, dict(metrics or {"elapsed_seconds": 4000}))


def _lane(owner: str, *, label: str = "lane", metrics: Mapping[str, Any] | None = None) -> ls.Observation:
    subject = ls.Subject("omp_lane", f"w1:{label}", owner)
    events = (ls.Event(0, NOW, "fleet", "private chat text"), ls.Event(1, NOW, "fleet", "more private text"))
    return ls.Observation(subject, NOW, events, dict(metrics or {}))


def _scan(root: Path, *observations: ls.Observation, decider: ls.Decider | None = None) -> dict[str, Any]:
    return cli.run_scan(root=root, sources=[Fixed(*observations)], now=NOW, decider=decider or ls.RuleDecider())


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- join


def test_every_decision_has_exactly_one_window_with_its_key(tmp_path: Path) -> None:
    payload = _scan(tmp_path, _trial("a"), _trial("b"))

    decisions = _jsonl(tmp_path / ls.DECISIONS_FILE)
    windows = _jsonl(tmp_path / lw.WINDOWS_FILE)
    assert payload["recorded"] == 2 and payload["errors"] == {}
    assert [adj.row_key(d) for d in decisions] == [w["key"] for w in windows]
    assert len({w["key"] for w in windows}) == 2


def test_adjudicator_consumes_the_live_files_unchanged(tmp_path: Path) -> None:
    _scan(tmp_path, _trial("a", text="rm -rf build"), _trial("b"))
    rows = adj.sample(adj.read_jsonl(tmp_path / ls.DECISIONS_FILE))
    windows = {str(w["key"]): w for w in adj.read_jsonl(tmp_path / lw.WINDOWS_FILE)}

    assert len(rows) == 2
    for row in rows:
        user = adj.build_messages(row, windows[adj.row_key(row)])[1]["content"]
        assert "[event 2]" in user and "repetition (loop" in user
        assert row["action"] not in user.split("## Actions")[0]  # the decision stays hidden


def test_a_second_cycle_appends_without_touching_earlier_rows(tmp_path: Path) -> None:
    _scan(tmp_path, _trial("a"))
    first = (tmp_path / lw.WINDOWS_FILE).read_text(encoding="utf-8")
    later = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    cli.run_scan(root=tmp_path, sources=[Fixed(_trial("a", at=later))], now=later, decider=ls.RuleDecider())

    text = (tmp_path / lw.WINDOWS_FILE).read_text(encoding="utf-8")
    assert text.startswith(first) and len(text.splitlines()) == 2
    assert [w["key"] for w in _jsonl(tmp_path / lw.WINDOWS_FILE)] == [
        adj.row_key(d) for d in _jsonl(tmp_path / ls.DECISIONS_FILE)]


def test_suppressed_and_quiet_subjects_get_no_window(tmp_path: Path) -> None:
    _scan(tmp_path, _trial("a"))
    _scan(tmp_path, _trial("a"))  # same fingerprint inside the cooldown
    assert len(_jsonl(tmp_path / lw.WINDOWS_FILE)) == 1


def test_a_duplicate_key_is_refused_not_overwritten(tmp_path: Path) -> None:
    same = ls.Subject("harbor_trial", "job/a", "lab", killable=True, run_id="shared-trace")
    other = ls.Subject("harbor_trial", "job/b", "lab", killable=True, run_id="shared-trace")
    first = ls.Observation(same, NOW, (ls.Event(0, NOW, "agent", "first"),))
    second = ls.Observation(other, NOW, (ls.Event(0, NOW, "agent", "second"),))

    payload = _scan(tmp_path, first, second)

    assert payload["recorded"] == 2
    (window,) = _jsonl(tmp_path / lw.WINDOWS_FILE)
    assert window["events"][0]["text"] == "first"  # the adjudicator keeps the first decision with this key
    assert list(payload["errors"]) == ["window:harbor_trial:job/b"]


# --------------------------------------------------------------------------- blinding and authorization


def test_no_benchmark_name_survives_in_a_window(tmp_path: Path) -> None:
    text = " ".join(NAMES)
    observation = _trial(
        "a", text=f"reading /data/{text}/tasks",
        metrics={"source": "terminal-bench-4", "dataset": "SWE-bench", "label": "cheat", "reward": 1,
                 "note": f"from {text}", "steps": 7, "nested": {"a": 1}, "elapsed_seconds": 5000.5})
    hit = ls.TriggerHit("error_signature", "infra", 2.0, f"{text} image build failed", (1,))

    class Blind(Fixed):
        def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
            return [hit]

    cli.run_scan(root=tmp_path, sources=[Blind(observation)], now=NOW, decider=ls.RuleDecider())
    (window,) = _jsonl(tmp_path / lw.WINDOWS_FILE)
    stored = json.dumps({k: v for k, v in window.items() if k != "key"})

    for name in NAMES:
        assert name.lower() not in stored.lower()
    assert "[source]" in stored
    assert window["metrics"] == {"note": "from [source] [source] [source] [source]", "steps": 7,
                                 "elapsed_seconds": 5000.5}


def test_a_pinned_lane_keeps_metadata_but_no_transcript(tmp_path: Path) -> None:
    meta = {"agent_status": "working", "authorized": False, "session_read": False}
    _scan(tmp_path, _lane("peter", metrics={**meta, "authorized": True, "session_read": True}),
          _lane("fleet", label="unauthorized", metrics=meta),
          _lane("fleet", label="no-flags"))

    windows = _jsonl(tmp_path / lw.WINDOWS_FILE)
    assert len(windows) == 3
    assert all(w["events"] == [] for w in windows)
    assert windows[1]["metrics"]["agent_status"] == "working"
    assert "private" not in json.dumps(windows)


def test_an_authorized_lane_keeps_its_events(tmp_path: Path) -> None:
    _scan(tmp_path, _lane("fleet", metrics={"authorized": True, "session_read": True}))
    (window,) = _jsonl(tmp_path / lw.WINDOWS_FILE)
    assert [e["text"] for e in window["events"]] == ["private chat text", "more private text"]


def test_nothing_owned_by_peter_keeps_events_whatever_its_kind(tmp_path: Path) -> None:
    pinned = ls.Observation(ls.Subject("terminal_tab", "w9:p1", "peter"), NOW, (ls.Event(0, NOW, "tab", "private"),))
    _scan(tmp_path, pinned)
    (window,) = _jsonl(tmp_path / lw.WINDOWS_FILE)
    assert window["events"] == [] and window["subject"]["owner"] == "peter"


# --------------------------------------------------------------------------- caps


def test_the_window_is_capped_to_what_the_reader_sees(tmp_path: Path) -> None:
    long = "x" * 10_000
    observation = _trial("a", events=WINDOW_EVENTS + 30, text=long,
                         metrics={"long": long, **{f"m{i:03d}": i for i in range(200)}})
    big_hits = [ls.TriggerHit("repetition", "loop", 3.0, long, (i,)) for i in range(25)]

    class Hits(Fixed):
        def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
            return big_hits

    cli.run_scan(root=tmp_path, sources=[Hits(observation)], now=NOW, decider=ls.RuleDecider())
    (window,) = _jsonl(tmp_path / lw.WINDOWS_FILE)

    assert [e["index"] for e in window["events"]] == list(range(30, 30 + WINDOW_EVENTS))
    assert all(len(e["text"]) <= EVENT_CHARS + 60 for e in window["events"])
    assert len(window["triggers"]) == 10
    assert len(window["metrics"]) <= lw.MAX_METRICS
    assert len((tmp_path / lw.WINDOWS_FILE).read_bytes()) <= lw.MAX_WINDOW_BYTES


def test_a_row_over_the_byte_cap_loses_its_oldest_events_first(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = _trial("a", events=10, text="y" * 500)
    uncapped = lw.build_window("k", observation, [HIT])
    monkeypatch.setattr(lw, "MAX_WINDOW_BYTES", 2_500)

    window = lw.build_window("k", observation, [HIT])

    assert len(json.dumps(window, sort_keys=True).encode()) <= 2_500
    kept = [e["index"] for e in window["events"]]
    assert 0 < len(kept) < len(uncapped["events"]) and kept == sorted(kept) and kept[-1] == 9


# --------------------------------------------------------------------------- failures


def test_a_failing_window_write_keeps_the_decision_and_the_cycle(tmp_path: Path) -> None:
    (tmp_path / lw.WINDOWS_FILE).mkdir()  # every append to windows-v1.jsonl fails

    payload = _scan(tmp_path, _trial("a"), _trial("b"))

    assert payload["recorded"] == 2
    assert sorted(payload["errors"]) == ["window:harbor_trial:job/a", "window:harbor_trial:job/b"]
    assert all("IsADirectoryError" in message for message in payload["errors"].values())
    assert [d["subject"]["id"] for d in _jsonl(tmp_path / ls.DECISIONS_FILE)] == ["job/a", "job/b"]


def test_the_decision_is_durable_before_the_window_is_attempted(tmp_path: Path) -> None:
    seen: list[int] = []

    class Spy:
        def write(self, record: Mapping[str, Any], observation: ls.Observation,
                  hits: Sequence[ls.TriggerHit]) -> None:
            seen.append(len((tmp_path / ls.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()))
            raise RuntimeError("disk full")

    report = ls.scan_once([Fixed(_trial("a"), _trial("b"))], ls.RuleDecider(), ls.DecisionLog(tmp_path),
                          now=NOW, windows=Spy())

    assert seen == [1, 2]
    assert len(report.records) == 2
    assert report.errors["window:harbor_trial:job/b"] == "RuntimeError: disk full"


def test_a_torn_last_line_is_cut_before_the_next_append(tmp_path: Path) -> None:
    _scan(tmp_path, _trial("a"))
    path = tmp_path / lw.WINDOWS_FILE
    path.write_bytes(path.read_bytes() + b'{"key": "torn')

    later = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    cli.run_scan(root=tmp_path, sources=[Fixed(_trial("a", at=later))], now=later, decider=ls.RuleDecider())

    assert [w["subject"]["kind"] for w in _jsonl(path)] == ["harbor_trial", "harbor_trial"]


# --------------------------------------------------------------------------- rows for the adjudicator


def test_rows_split_the_rules_proposal_from_the_model_decision(tmp_path: Path) -> None:
    _scan(tmp_path, _trial("model"), decider=ModelLike())
    later = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    cli.run_scan(root=tmp_path, sources=[Fixed(_trial("rules", at=later))], now=later, decider=ls.RuleDecider())
    old = {**_jsonl(tmp_path / ls.DECISIONS_FILE)[0], "observed_at": "2026-10-20T00:00:00Z"}  # no window
    with (tmp_path / ls.DECISIONS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old) + "\n")

    model = lw.export_rows(tmp_path, "model", tmp_path / "model.jsonl")
    rules = lw.export_rows(tmp_path, "rules", tmp_path / "rules.jsonl")

    assert (model["rows"], model["no_arm"], model["no_window"]) == (1, 1, 1)
    assert (rules["rows"], rules["no_arm"], rules["no_window"]) == (2, 0, 1)
    (model_row,) = _jsonl(tmp_path / "model.jsonl")
    assert (model_row["arm"], model_row["action"]) == ("model", "continue")
    rules_rows = _jsonl(tmp_path / "rules.jsonl")
    assert [(r["arm"], r["action"], r["recorded_action"]) for r in rules_rows] == [
        ("rules", "kill_and_retry", "continue"), ("rules", "kill_and_retry", "kill_and_retry")]
    windows = {w["key"] for w in _jsonl(tmp_path / lw.WINDOWS_FILE)}
    assert {adj.row_key(r) for r in rules_rows} <= windows
    assert len(adj.sample(rules_rows)) == 2  # both arms are valid adjudicator input


def test_a_reader_fallback_is_not_a_model_decision(tmp_path: Path) -> None:
    class Failing:
        def decide(self, observation: ls.Observation, hits: Sequence[ls.TriggerHit],
                   proposal: ls.Decision) -> ls.Decision:
            raise RuntimeError("gateway down")

    payload = _scan(tmp_path, _trial("a"), decider=Failing())

    assert payload["records"][0]["action"] == "alert_research" and payload["records"][0]["reader_model"] is None
    assert lw.export_rows(tmp_path, "model", tmp_path / "m.jsonl")["rows"] == 0
    assert (tmp_path / "m.jsonl").read_text(encoding="utf-8") == ""


def test_export_needs_both_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="decisions-v1.jsonl, windows-v1.jsonl"):
        lw.export_rows(tmp_path, "rules", tmp_path / "out.jsonl")
    with pytest.raises(ValueError, match="arm 'both'"):
        lw.arm_row({"proposal": "continue"}, "both")


def test_rows_command_writes_the_file_and_reports_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _scan(tmp_path, _trial("a"))
    parser = argparse.ArgumentParser()
    cli.register_parser(parser.add_subparsers(dest="action", required=True))
    args = parser.parse_args(["live-supervisor", "rows", "--root", str(tmp_path), "--arm", "rules",
                              "--out", str(tmp_path / "rows.jsonl")])

    assert args.func(args, tmp_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["arm"], summary["rows"], summary["windows"]) == ("rules", 1, str(tmp_path / lw.WINDOWS_FILE))
    assert len(_jsonl(tmp_path / "rows.jsonl")) == 1

    empty = tmp_path / "empty"
    args = parser.parse_args(["live-supervisor", "rows", "--root", str(empty), "--arm", "model",
                              "--out", str(empty / "rows.jsonl")])
    assert args.func(args, empty) == 2
    assert "not found" in capsys.readouterr().err
