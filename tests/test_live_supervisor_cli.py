"""Behaviour of ``evallab live-supervisor``: registration, lazy sources, lock, cycles, forwarding.

Every external seam is injected: sources, decider, clock, sleep, gateway transport and the
module loader. No herdr, launchctl, modal, omp, network or wall-clock dependency; nothing is
killed, stopped or messaged.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_cli as cli

ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.live-supervisor.plist"
NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evallab")
    cli.register_parser(parser.add_subparsers(dest="action", required=True))
    return parser


def _parse(*argv: str) -> argparse.Namespace:
    """Parse ``evallab live-supervisor <argv>`` through the real registration."""
    return _parser().parse_args(["live-supervisor", *argv])


class FakeSource:
    """A source that observes fixed subjects and triggers once per subject."""

    def __init__(self, name: str = "harbor", *, hits: int = 1, probe: Any = None) -> None:
        self.name = name
        self.hits = hits
        self.probe = probe
        self.observations: list[ls.Observation] = [self._observation()]

    @staticmethod
    def _observation() -> ls.Observation:
        subject = ls.Subject("harbor_trial", "job/trial-1", "lab", killable=True)
        events = tuple(ls.Event(i, NOW, "agent", f"step {i}") for i in range(3))
        return ls.Observation(subject, NOW, events, {"elapsed_seconds": 4000})

    def observe(self, now: datetime) -> list[ls.Observation]:
        if self.probe is not None:
            self.probe()
        return self.observations

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        del observation
        return [ls.TriggerHit("silence", "latency", 3.5, "no new step", (2,))] * self.hits


# --------------------------------------------------------------------------- registration


def test_registration_is_inert_and_defaults_are_one_cycle() -> None:
    args = _parse("scan")

    assert args.func is cli._scan_command
    assert args.source is None
    assert args.reader == "rules"
    assert args.root == ls.DEFAULT_ROOT
    assert args.max_tokens_per_day is None
    assert args.json is False


def test_registration_imports_no_source_module() -> None:
    """Registering the command imports this module only: no source, replay or triage module."""
    script = (
        "import argparse, sys\n"
        "import evallab.production.live_supervisor_cli as cli\n"
        "parser = argparse.ArgumentParser()\n"
        "cli.register_parser(parser.add_subparsers(dest='action', required=True))\n"
        "leaked = sorted(name for name in sys.modules "
        "if name.startswith('evallab.production.live_supervisor_')\n"
        "          and name != 'evallab.production.live_supervisor_cli')\n"
        "print(','.join(leaked))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True,
                               text=True, check=True)
    assert completed.stdout.strip() == "", completed.stdout


def test_sources_repeat_and_unknown_names_are_rejected() -> None:
    args = _parse("scan", "--source", "harbor", "--source", "fleet")
    assert args.source == ["harbor", "fleet"]
    with pytest.raises(SystemExit):
        _parse("scan", "--source", "docker")
    with pytest.raises(SystemExit):
        _parse("scan", "--reader", "opus")


def test_replay_forwards_its_documented_flags() -> None:
    args = _parse(
        "replay", "--manifest", "/m.jsonl", "--out", "/rows.jsonl", "--set", "loop", "--set", "infra",
        "--workers", "4", "--store-copy", "--score-out", "/score.json", "--", "--limit-per-set", "9",
    )

    assert args.func is cli._replay_command
    assert cli.replay_argv(args) == [
        "--manifest", "/m.jsonl", "--out", "/rows.jsonl", "--set", "loop", "--set", "infra",
        "--workers", "4", "--score-out", "/score.json", "--store-copy", "--limit-per-set", "9",
    ]
    assert "--" not in cli.replay_argv(args)


def test_replay_requires_an_output_and_omits_unset_flags() -> None:
    with pytest.raises(SystemExit):
        _parse("replay", "--manifest", "/m.jsonl")
    args = _parse("replay", "--out", "/rows.jsonl")
    assert cli.replay_argv(args) == ["--out", "/rows.jsonl"]


def test_adjudicate_requires_rows_windows_and_out() -> None:
    for missing in ("--rows", "--windows", "--out"):
        argv = {"--rows": "/r.jsonl", "--windows": "/w.jsonl", "--out": "/o.jsonl"}
        argv.pop(missing)
        with pytest.raises(SystemExit):
            _parse("adjudicate", *[part for pair in argv.items() for part in pair])
    args = _parse(
        "adjudicate", "--rows", "/r.jsonl", "--windows", "/w.jsonl", "--out", "/o.jsonl",
        "--model", "anthropic/claude-opus-5-5", "--limit", "12", "--per-cell", "3", "--seed", "7",
    )
    assert args.func is cli._adjudicate_command
    assert cli.adjudicate_argv(args) == [
        "--rows", "/r.jsonl", "--windows", "/w.jsonl", "--out", "/o.jsonl",
        "--model", "anthropic/claude-opus-5-5", "--limit", "12", "--per-cell", "3", "--seed", "7",
    ]


def test_delegation_passes_argv_and_returns_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def main(argv: list[str]) -> int:
        seen.append(argv)
        return 2

    monkeypatch.setattr(cli, "_import", lambda module, what: SimpleNamespace(main=main))
    assert cli.delegate("live_supervisor_replay", ["--out", "/x"], what="the replay harness") == 2
    assert seen == [["--out", "/x"]]


def test_delegation_errors_name_the_missing_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="live_supervisor_nope is missing"):
        cli.delegate("live_supervisor_nope", [], what="the replay harness")
    monkeypatch.setattr(cli, "_import", lambda module, what: SimpleNamespace())
    with pytest.raises(ValueError, match="has no main"):
        cli.delegate("live_supervisor_replay", [], what="the replay harness")


# --------------------------------------------------------------------------- lazy sources


def test_source_specs_reject_unknown_and_repeated_names() -> None:
    with pytest.raises(ValueError, match="not in harbor"):
        cli.specs_for(["docker"])
    with pytest.raises(ValueError, match="repeated"):
        cli.specs_for(["harbor", "harbor"])
    with pytest.raises(ValueError, match="at least one source"):
        cli.specs_for([])
    assert [spec.name for spec in cli.specs_for(["fleet", "harbor"])] == ["fleet", "harbor"]


def test_build_sources_imports_lazily_and_scopes_state_to_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []
    constructed: list[tuple[str, dict[str, Any]]] = []

    def fake_import(module: str, what: str) -> Any:
        imported.append(module)
        del what
        return SimpleNamespace(**{
            "HarborSource": lambda: constructed.append(("harbor", {})) or "harbor",
            "FleetSource": lambda **kwargs: constructed.append(("fleet", kwargs)) or "fleet",
            "LaunchdSource": lambda: constructed.append(("services", {})) or "services",
            "ModalSource": lambda: constructed.append(("modal", {})) or "modal",
            "ProcessSource": lambda: constructed.append(("processes", {})) or "processes",
        })

    monkeypatch.setattr(cli, "_import", fake_import)
    assert imported == []
    built = cli.build_sources(["harbor", "fleet", "modal"], root=tmp_path)

    assert built == ["harbor", "fleet", "modal"]
    assert constructed == [
        ("harbor", {}), ("fleet", {"state_path": tmp_path / "fleet-state.json"}), ("modal", {}),
    ]
    assert imported == ["live_supervisor_harbor", "live_supervisor_fleet", "live_supervisor_services"]


# --------------------------------------------------------------------------- lock


def test_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "nested" / cli.LOCK_FILE
    with cli.hold_lock(path):
        with pytest.raises(cli.SupervisorAlreadyRunning, match="another live-supervisor"), cli.hold_lock(path):
            pass
        assert "pid" in path.read_text()
    with cli.hold_lock(path):
        pass


# --------------------------------------------------------------------------- one cycle


def test_run_scan_records_a_shadow_decision_under_the_lock(tmp_path: Path) -> None:
    def probe() -> None:
        with pytest.raises(cli.SupervisorAlreadyRunning), cli.hold_lock(tmp_path / cli.LOCK_FILE):
            pass

    source = FakeSource(probe=probe)
    payload = cli.run_scan(root=tmp_path, sources=[source], now=NOW, decider=ls.RuleDecider())

    assert payload["mode"] == "shadow"
    assert payload["sources"] == ["harbor"]
    assert (payload["observed"], payload["recorded"], payload["quiet"]) == (1, 1, 0)
    assert payload["observed_at"] == "2026-10-28T20:00:00Z"
    (record,) = payload["records"]
    assert (record["action"], record["proposal"], record["acted"]) == ("kill_and_retry", "kill_and_retry", False)
    written = (tmp_path / ls.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in written] == ["live:harbor_trial:job/trial-1"]


def test_run_scan_skips_quiet_and_terminal_subjects(tmp_path: Path) -> None:
    source = FakeSource(hits=0)
    quiet = cli.run_scan(root=tmp_path, sources=[source], now=NOW, decider=ls.RuleDecider())
    assert (quiet["quiet"], quiet["recorded"]) == (1, 0)
    assert not (tmp_path / ls.DECISIONS_FILE).exists()

    terminal = FakeSource()
    terminal.observations = [ls.Observation(_terminal_subject(), NOW, terminal=True)]
    stopped = cli.run_scan(root=tmp_path, sources=[terminal], now=NOW, decider=ls.RuleDecider())
    assert (stopped["terminal"], stopped["recorded"]) == (1, 0)


def _terminal_subject() -> ls.Subject:
    return ls.Subject("harbor_trial", "job/trial-2", "lab", killable=True)


def test_run_scan_reports_a_broken_source_without_stopping(tmp_path: Path) -> None:
    class Broken:
        name = "modal"

        def observe(self, now: datetime) -> list[ls.Observation]:
            raise RuntimeError("modal cli not logged in")

        def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
            raise AssertionError("a broken source is never asked for triggers")

    payload = cli.run_scan(root=tmp_path, sources=[Broken(), FakeSource()], now=NOW,
                           decider=ls.RuleDecider())
    assert payload["errors"] == {"modal": "RuntimeError: modal cli not logged in"}
    assert payload["recorded"] == 1


# --------------------------------------------------------------------------- readers


def test_rules_reader_is_free_and_model_reader_needs_a_cap(tmp_path: Path) -> None:
    with cli.decider_for("rules", root=tmp_path) as decider:
        assert isinstance(decider, ls.RuleDecider)
    with pytest.raises(ValueError, match="--max-tokens-per-day"), cli.decider_for("model", root=tmp_path):
        pass
    with pytest.raises(ValueError, match="not in rules, model"), cli.decider_for("opus", root=tmp_path):
        pass
    with pytest.raises(ValueError, match="not be negative"), cli.decider_for(
        "model", root=tmp_path, daily_token_budget=-1,
    ):
        pass


def test_model_reader_builds_and_closes_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evallab.production import run_atlas

    state = SimpleNamespace(transports=0, closed=0, seen=[])

    class FakeTransport:
        def __init__(self) -> None:
            state.transports += 1

        def close(self) -> None:
            state.closed += 1

    class FakeDecider:
        def __init__(self, transport: Any, root: Path, *, daily_token_budget: int) -> None:
            state.seen.append((transport, root, daily_token_budget))

        def decide(self, observation: ls.Observation, hits: Any, proposal: ls.Decision) -> ls.Decision:
            return proposal

    monkeypatch.setattr(run_atlas, "GatewayTransport", FakeTransport)
    monkeypatch.setattr(cli, "_import", lambda module, what: SimpleNamespace(ModelDecider=FakeDecider))

    with cli.decider_for("model", root=tmp_path, daily_token_budget=500_000) as decider:
        assert isinstance(decider, FakeDecider)
        assert state.closed == 0
    assert state.transports == 1
    assert state.closed == 1
    transport, root, cap = state.seen[0]
    assert isinstance(transport, FakeTransport)
    assert (root, cap) == (tmp_path, 500_000)


def test_model_reader_wires_the_merged_triage_decider(tmp_path: Path) -> None:
    """The merged ModelDecider is built with the gateway and the cap; no child starts here."""
    from evallab.production import live_supervisor_triage

    with cli.decider_for("model", root=tmp_path, daily_token_budget=1_000) as decider:
        assert isinstance(decider, cli.HeadroomGatedDecider)
        inner = decider.decider
        assert isinstance(inner, live_supervisor_triage.ModelDecider)
        assert inner.transport is not None
        assert inner.budget.limit == 1_000
        assert decider.models == (inner.triage_model, inner.decide_model, inner.deep_model)


# --------------------------------------------------------------------------- reader headroom


class CountingDecider:
    def __init__(self) -> None:
        self.calls = 0
        self.answer = ls.Decision("kill_and_retry", "loop", "rule proposal stands")

    def decide(self, observation: ls.Observation, hits: Any, proposal: ls.Decision) -> ls.Decision:
        del observation, hits, proposal
        self.calls += 1
        return self.answer


def _observation() -> ls.Observation:
    subject = ls.Subject("harbor_trial", "job/trial-1", "lab", killable=True)
    return ls.Observation(subject, NOW, (ls.Event(1, NOW, "agent", "step 1"),), {})


PROPOSAL = ls.Decision("kill_and_retry", "loop", "loop: 8 identical commands")
HITS = (ls.TriggerHit("repetition", "loop", 2.0, "8 identical commands", (1,)),)


def test_headroom_gate_refuses_to_send_when_a_reader_route_is_used_up() -> None:
    inner = CountingDecider()
    checks: list[Sequence[str]] = []

    def check(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        checks.append(routes)
        return [r for r in routes if not r.startswith("anthropic/")], ["anthropic"], "omp-usage-json"

    gate = cli.HeadroomGatedDecider(inner, models=["anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"],
                                   check=check, clock=lambda: NOW)
    decision = gate.decide(_observation(), HITS, PROPOSAL)

    assert decision.action == "alert_research"
    assert decision.kind == "other"
    assert "reader unavailable" in decision.reason
    assert "90% window" in decision.reason and "anthropic" in decision.reason
    assert inner.calls == 0  # no model request was sent
    assert len(checks) == 1


def test_headroom_gate_passes_the_reader_through_when_all_routes_have_headroom() -> None:
    inner = CountingDecider()
    gate = cli.HeadroomGatedDecider(
        inner, models=["anthropic/claude-sonnet-5-5"],
        check=lambda routes: (list(routes), [], "omp-usage-json"), clock=lambda: NOW)

    assert gate.decide(_observation(), HITS, PROPOSAL) is inner.answer
    assert inner.calls == 1
    assert gate.blocked() is None


def test_headroom_gate_checks_at_most_once_per_ttl() -> None:
    moment = [NOW]
    checks: list[int] = []

    def check(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        checks.append(len(routes))
        return list(routes), [], "omp-usage-json"

    gate = cli.HeadroomGatedDecider(CountingDecider(), models=["anthropic/x"], check=check,
                                   clock=lambda: moment[0], ttl=timedelta(minutes=5))
    gate.decide(_observation(), HITS, PROPOSAL)
    moment[0] = NOW + timedelta(minutes=4)
    gate.decide(_observation(), HITS, PROPOSAL)
    assert len(checks) == 1  # one check covers the whole cycle window
    moment[0] = NOW + timedelta(minutes=5)
    gate.decide(_observation(), HITS, PROPOSAL)
    assert len(checks) == 2


def test_headroom_gate_defaults_to_the_omp_usage_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from evallab.production import run_atlas_watch

    seen: list[Sequence[str]] = []

    def routes_with_headroom(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        seen.append(routes)
        return [], ["anthropic"], "omp-usage-json"

    monkeypatch.setattr(run_atlas_watch, "routes_with_headroom", routes_with_headroom)
    gate = cli.HeadroomGatedDecider(CountingDecider(), models=["anthropic/y"], clock=lambda: NOW)

    assert gate.decide(_observation(), HITS, PROPOSAL).action == "alert_research"
    assert seen == [("anthropic/y",)]


def test_decider_for_gates_the_model_reader_with_an_injected_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evallab.production import run_atlas

    closed: list[bool] = []

    class FakeTransport:
        def close(self) -> None:
            closed.append(True)

    class FakeDecider:
        triage_model = "anthropic/claude-haiku-5-5"
        decide_model = "anthropic/claude-sonnet-5-5"
        deep_model = "anthropic/claude-opus-5-5"

        def __init__(self, transport: Any, root: Path, *, daily_token_budget: int) -> None:
            del transport, root, daily_token_budget

        def decide(self, observation: ls.Observation, hits: Any, proposal: ls.Decision) -> ls.Decision:
            del observation, hits
            return proposal

    monkeypatch.setattr(run_atlas, "GatewayTransport", FakeTransport)
    monkeypatch.setattr(cli, "_import", lambda module, what: SimpleNamespace(ModelDecider=FakeDecider))

    with cli.decider_for(
        "model", root=tmp_path, daily_token_budget=10,
        check=lambda routes: ([], ["anthropic"], "omp-usage-json"), clock=lambda: NOW,
    ) as decider:
        assert isinstance(decider, cli.HeadroomGatedDecider)
        assert decider.decide(_observation(), HITS, PROPOSAL).action == "alert_research"
    assert closed == [True]  # the gateway child is closed with the context


# --------------------------------------------------------------------------- scan command


def test_scan_command_passes_defaults_and_prints_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_scan(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "observed": 2, "quiet": 1, "terminal": 0, "suppressed": 0, "recorded": 1,
            "actions": {"alert_research": 1}, "errors": {"fleet": "RuntimeError: herdr absent"},
            "mode": "shadow", "sources": ["harbor", "fleet"], "observed_at": "2026-10-28T20:00:00Z",
            "records": [{"action": "alert_research", "kind": "infra",
                         "run_id": "live:queue_keeper:keeper", "summary": "error_signature: build failed"}],
        }

    monkeypatch.setattr(cli, "run_scan", fake_scan)
    args = _parse("scan", "--source", "harbor", "--source", "fleet",
                                 "--max-tokens-per-day", "9")
    assert args.func(args, tmp_path) == 0

    assert seen == {"names": ["harbor", "fleet"], "root": ls.DEFAULT_ROOT, "reader": "rules",
                    "daily_token_budget": 9}
    out, err = capsys.readouterr()
    assert "live-supervisor scan (shadow): observed=2 quiet=1 terminal=0 suppressed=0 recorded=1" in out
    assert 'actions={"alert_research": 1} sources=harbor,fleet' in out
    assert "  alert_research infra live:queue_keeper:keeper: error_signature: build failed" in out
    assert "source fleet failed: RuntimeError: herdr absent" in err


def test_scan_command_json_is_one_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "run_scan", lambda **_kwargs: {"mode": "shadow", "records": [],
                                                            "errors": {}, "sources": []})
    args = _parse("scan", "--json")
    assert args.func(args, tmp_path) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"errors": {}, "mode": "shadow", "records": [], "sources": []}
    assert err == ""


def test_scan_command_uses_every_source_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    empty = {"observed": 0, "quiet": 0, "terminal": 0, "suppressed": 0, "recorded": 0,
             "actions": {}, "errors": {}, "mode": "shadow", "sources": [], "records": []}
    monkeypatch.setattr(cli, "run_scan", lambda **kwargs: seen.update(kwargs) or empty)
    args = _parse("scan", "--root", "/tmp/elsewhere")
    assert args.func(args, tmp_path) == 0
    assert seen["names"] == cli.SOURCE_NAMES
    assert seen["root"] == Path("/tmp/elsewhere")


# --------------------------------------------------------------------------- serve


def test_serve_loops_with_the_interval_between_cycles(tmp_path: Path) -> None:
    emitted: list[dict[str, Any]] = []
    slept: list[float] = []
    moments = [NOW + timedelta(minutes=2 * n) for n in range(3)]
    source = FakeSource(hits=0)

    assert cli.serve(root=tmp_path, interval=120.0, cycles=3, clock=lambda: moments.pop(0),
                     sleep=slept.append, emit=emitted.append, sources=[source],
                     decider=ls.RuleDecider()) == 0

    assert [payload["observed_at"] for payload in emitted] == [
        "2026-10-28T20:00:00Z", "2026-10-28T20:02:00Z", "2026-10-28T20:04:00Z",
    ]
    assert slept == [120.0, 120.0]
    assert all(payload["mode"] == "shadow" for payload in emitted)


def test_serve_holds_the_lock_for_its_lifetime(tmp_path: Path) -> None:
    def probe() -> None:
        with pytest.raises(cli.SupervisorAlreadyRunning, match="another live-supervisor"), cli.hold_lock(
            tmp_path / cli.LOCK_FILE,
        ):
            pass

    assert cli.serve(root=tmp_path, cycles=1, clock=lambda: NOW, sleep=lambda _s: None,
                     emit=lambda _payload: None, sources=[FakeSource(probe=probe)],
                     decider=ls.RuleDecider()) == 0


def test_serve_refuses_bad_intervals_and_cycle_counts(tmp_path: Path) -> None:
    for interval in (0.0, -120.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="--interval must be finite and positive"):
            cli.serve(root=tmp_path, interval=interval, cycles=1, sources=[])
    with pytest.raises(ValueError, match="--cycles must be positive"):
        cli.serve(root=tmp_path, cycles=0, sources=[])


def test_serve_command_returns_zero_on_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(**_kwargs: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "serve", interrupted)
    args = _parse("serve", "--interval", "60")
    assert args.func(args, tmp_path) == 0
    assert args.interval == 60.0


def test_serve_command_passes_the_interval_and_reader(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "serve", lambda **kwargs: seen.update(kwargs) or 0)
    args = _parse("serve", "--reader", "model", "--max-tokens-per-day", "1000")
    assert args.func(args, tmp_path) == 0
    assert seen["interval"] == 120.0
    assert seen["reader"] == "model"
    assert seen["daily_token_budget"] == 1000
    assert seen["names"] == cli.SOURCE_NAMES


# --------------------------------------------------------------------------- routing alarms


def _alarm_record(run_id: str = "live:harbor_trial:job/trial-1", **extra: Any) -> dict[str, Any]:
    return {"run_id": run_id, "detector": "trace_harness", "detector_version": "trace-harness/1",
            "reader_model": "anthropic/claude-haiku-5-5", "verdict": "alarm", "kind": "cheat",
            "score": 2.5, "evidence_steps": [1, 3], "summary": "hidden test read: tests/secret",
            **extra}


class FakeRouter:
    """Records the flagged pairs and the paths it pretends to card."""

    def __init__(self, *, fail: bool = False) -> None:
        self.flagged: list[tuple[Any, Any]] = []
        self.fail = fail

    def route(self, flagged: Any) -> list[Path]:
        self.flagged = list(flagged)
        if self.fail:
            raise RuntimeError("queue unwritable")
        return [Path("/cards/one.md") for _ in self.flagged]


def _subscriber(records: list[dict[str, Any]], seen: list[Any] | None = None) -> Any:
    def subscribe(paths: Any, *, since_offsets_state: Path) -> list[dict[str, Any]]:
        if seen is not None:
            seen.append((list(paths), since_offsets_state))
        since_offsets_state.write_text(json.dumps({"files": {"x": {"offset": len(records)}}}),
                                       encoding="utf-8")
        return records

    return subscribe


def test_route_alarms_first_start_begins_at_eof(tmp_path: Path) -> None:
    router = FakeRouter()
    payload = cli.route_alarms(root=tmp_path, findings=[tmp_path / "f.jsonl"], queue=tmp_path / "q.jsonl",
                               trial_roots=[], now=NOW, subscriber=_subscriber([_alarm_record()]),
                               router=router)

    assert payload["started_at_eof"] is True
    assert (payload["alarms"], payload["cheat"], payload["cards"]) == (1, 0, [])
    assert router.flagged == []
    assert (tmp_path / cli.ALARM_CURSOR).is_file()
    assert not (tmp_path / cli.ALARM_CANDIDATE).exists()


def test_route_alarms_routes_only_cheat_alarms_and_advances_the_cursor(tmp_path: Path) -> None:
    cursor = tmp_path / cli.ALARM_CURSOR
    cursor.write_text('{"files": {"old": {"offset": 0}}}', encoding="utf-8")
    router = FakeRouter()
    records = [_alarm_record(), _alarm_record(run_id="live:harbor_trial:job/trial-2", kind="loop")]
    seen: list[Any] = []

    payload = cli.route_alarms(root=tmp_path, findings=[tmp_path / "f.jsonl"], queue=tmp_path / "q.jsonl",
                               trial_roots=[], now=NOW, subscriber=_subscriber(records, seen),
                               router=router)

    assert payload["started_at_eof"] is False
    assert (payload["alarms"], payload["cheat"], payload["cards"]) == (2, 1, ["/cards/one.md"])
    assert len(router.flagged) == 1
    assert router.flagged[0][1].action == "flag_cheating"
    assert seen[0][1] == tmp_path / cli.ALARM_CANDIDATE  # read into the candidate, not the durable file
    assert json.loads(cursor.read_text())["files"]["x"]["offset"] == 2


def test_route_alarms_keeps_the_cursor_when_routing_fails(tmp_path: Path) -> None:
    cursor = tmp_path / cli.ALARM_CURSOR
    cursor.write_text('{"files": {"old": {"offset": 0}}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="queue unwritable"):
        cli.route_alarms(root=tmp_path, findings=[tmp_path / "f.jsonl"], queue=tmp_path / "q.jsonl",
                         trial_roots=[], now=NOW, subscriber=_subscriber([_alarm_record()]),
                         router=FakeRouter(fail=True))

    assert cursor.read_text() == '{"files": {"old": {"offset": 0}}}'
    assert not (tmp_path / cli.ALARM_CANDIDATE).exists()


def test_route_alarms_never_overlaps_a_running_pass(tmp_path: Path) -> None:
    with cli.hold_lock(tmp_path / cli.ROUTE_LOCK), pytest.raises(cli.SupervisorAlreadyRunning):
        cli.route_alarms(root=tmp_path, findings=[tmp_path / "f.jsonl"], queue=tmp_path / "q.jsonl",
                         trial_roots=[], now=NOW, subscriber=_subscriber([_alarm_record()]),
                         router=FakeRouter())


def test_route_alarms_command_skips_when_a_pass_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def busy(**_kwargs: Any) -> dict[str, Any]:
        raise cli.SupervisorAlreadyRunning("another live-supervisor process holds route-alarms.lock")

    monkeypatch.setattr(cli, "route_alarms", busy)
    args = _parse("route-alarms", "--once")
    assert args.func(args, tmp_path) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "previous pass still running; skipped" in err


def test_route_alarms_replay_existing_routes_history_on_the_first_pass(tmp_path: Path) -> None:
    router = FakeRouter()
    payload = cli.route_alarms(root=tmp_path, findings=[tmp_path / "f.jsonl"], queue=tmp_path / "q.jsonl",
                               trial_roots=[], replay_existing=True, now=NOW,
                               subscriber=_subscriber([_alarm_record()]), router=router)

    assert payload["started_at_eof"] is False
    assert payload["cards"] == ["/cards/one.md"]
    assert (tmp_path / cli.ALARM_CURSOR).is_file()


def test_route_alarms_uses_the_production_findings_helpers_on_a_state_root(tmp_path: Path) -> None:
    """Real subscriber + real default path helper on a temp tree; only the router is faked."""
    from evallab.production import live_supervisor_cheat as cheat

    state = tmp_path / "prod-state"
    (state / "trace-harness").mkdir(parents=True)
    (state / "run-health").mkdir()
    rows = [_alarm_record(run_id="retained-trace:cheat-1"),
            _alarm_record(run_id="retained-trace:loop-1", kind="loop"),
            {"run_id": "retained-trace:clear", "detector": "run_health", "verdict": "clear", "kind": "loop"}]
    (state / "trace-harness" / "findings-v1.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    router = FakeRouter()

    payload = cli.route_alarms(root=tmp_path / "live-supervisor", state_root=state,
                               queue=tmp_path / "q.jsonl", trial_roots=[], replay_existing=True,
                               now=NOW, subscriber=cheat.subscribe_alarms, router=router)

    assert payload["findings"] == [str(state / "trace-harness" / "findings-v1.jsonl")]
    assert (payload["alarms"], payload["cheat"]) == (2, 1)
    assert [pair[1].action for pair in router.flagged] == ["flag_cheating"]
    cursor = json.loads((tmp_path / "live-supervisor" / cli.ALARM_CURSOR).read_text())
    assert cursor["files"][str(state / "trace-harness" / "findings-v1.jsonl")]["offset"] > 0


def test_route_alarms_cards_a_real_cheat_alarm_and_writes_one_queue_line(tmp_path: Path) -> None:
    """The merged CheatRouter really writes a card and the Trace Watcher queue line, all in temp."""
    from evallab.production import live_supervisor_cheat as cheat

    state = tmp_path / "prod-state"
    (state / "trace-harness").mkdir(parents=True)
    findings = state / "trace-harness" / "findings-v1.jsonl"
    findings.write_text(json.dumps(_alarm_record(run_id="retained-trace:cheat-1")) + "\n",
                        encoding="utf-8")
    queue = tmp_path / "queue-append.jsonl"
    root = tmp_path / "live-supervisor"

    payload = cli.route_alarms(root=root, state_root=state, queue=queue, trial_roots=[],
                               replay_existing=True, now=NOW, subscriber=cheat.subscribe_alarms)

    (card,) = payload["cards"]
    assert Path(card) == root / cheat.CARDS_DIR / f"{cheat.safe_name('retained-trace:cheat-1')}.md"
    text = Path(card).read_text(encoding="utf-8")
    assert "Live Supervisor cheat card" in text
    assert "trace_harness" in text and "hidden test read" in text
    (line,) = [json.loads(row) for row in queue.read_text(encoding="utf-8").splitlines()]
    assert line["pane"] == cheat.TRACE_WATCHER_PANE
    assert str(card) in line["text"]
    # a second pass over the same findings cards nothing new
    again = cli.route_alarms(root=root, state_root=state, queue=queue, trial_roots=[],
                             now=NOW, subscriber=cheat.subscribe_alarms)
    assert (again["alarms"], again["cards"]) == (0, [])


def test_flagged_reads_real_steps_from_a_live_trial(tmp_path: Path) -> None:
    trial = tmp_path / "runs" / "job" / "trial-1"
    (trial / "agent").mkdir(parents=True)
    steps = [{"step_id": 1, "source": "agent", "message": "reading hidden tests",
              "tool_calls": [{"function_name": "bash_command", "arguments": {"keystrokes": "cat tests/secret\n"}}],
              "observation": {"results": [{"content": "ok"}]}},
             {"step_id": 2, "source": "user", "message": "continue"}]
    (trial / "agent" / "trajectory.json").write_text(json.dumps(
        {"schema_version": "ATIF-v1.5", "session_id": "s", "agent": {"name": "x", "version": "1"},
         "steps": steps}), encoding="utf-8")
    (trial / "result.json").write_text("{}", encoding="utf-8")

    observation, decision = cli.flagged(_alarm_record(), now=NOW, trial_roots=[tmp_path / "runs"])

    assert observation.subject.kind == "harbor_trial"
    assert observation.subject.finding_run_id == "live:harbor_trial:job/trial-1"
    assert observation.subject.killable is False
    assert [event.index for event in observation.events] == [1, 2]
    assert "tests/secret" in observation.events[0].text
    assert observation.metrics["trace_alarms"][0]["detector"] == "trace_harness"
    assert decision.action == "flag_cheating"
    assert decision.evidence_steps == (1, 3)
    assert decision.reader_model == "anthropic/claude-haiku-5-5"


def test_flagged_without_a_trial_invents_no_steps(tmp_path: Path) -> None:
    observation, decision = cli.flagged(_alarm_record(run_id="retained-trace:abc"),
                                        now=NOW, trial_roots=[tmp_path / "runs"])

    assert observation.subject.kind == "stored_run"
    assert observation.events == ()
    assert observation.actor_inputs_available is False
    assert observation.metrics["trace_alarms"] == [{"detector": "trace_harness", "score": 2.5,
                                                    "summary": "hidden test read: tests/secret",
                                                    "evidence_steps": [1, 3]}]
    assert decision.action == "flag_cheating" and decision.kind == "cheat"
    assert decision.evidence_steps == (1, 3)


def test_flagged_never_trusts_a_path_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "agent"
    outside.mkdir(parents=True)
    record = _alarm_record(run_id="live:harbor_trial:../outside")
    observation, _ = cli.flagged(record, now=NOW, trial_roots=[tmp_path / "runs"])
    assert observation.events == ()


def test_route_alarms_command_parses_and_prints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def fake_route(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"observed_at": "2026-10-28T20:00:00Z", "findings": [], "queue": "/q.jsonl",
                "started_at_eof": False, "alarms": 2, "cheat": 1, "cards": ["/cards/a.md"]}

    monkeypatch.setattr(cli, "route_alarms", fake_route)
    args = _parse("route-alarms", "--once", "--findings", "/f.jsonl", "--queue", "/q.jsonl",
                  "--state-root", "/prod-state", "--trial-root", "/runs", "--replay-existing")
    assert args.func(args, tmp_path) == 0

    assert seen == {"root": ls.DEFAULT_ROOT, "findings": [Path("/f.jsonl")], "queue": Path("/q.jsonl"),
                    "state_root": Path("/prod-state"), "trial_roots": [Path("/runs")],
                    "replay_existing": True}
    out, err = capsys.readouterr()
    assert "live-supervisor route-alarms: alarms=2 cheat=1 cards=1 queue=/q.jsonl" in out
    assert "  /cards/a.md" in out
    assert err == ""


def test_route_alarms_command_json_and_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    passes: list[bool] = []
    sleeps: list[float] = []

    def fake_route(**kwargs: Any) -> dict[str, Any]:
        passes.append(kwargs["replay_existing"])
        return {"observed_at": "2026-10-28T20:00:00Z", "findings": [], "queue": "/q.jsonl",
                "started_at_eof": False, "alarms": 0, "cheat": 0, "cards": []}

    monkeypatch.setattr(cli, "route_alarms", fake_route)
    args = _parse("route-alarms", "--interval", "30", "--replay-existing", "--json")

    def stop(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:  # two passes seen: Ctrl-C at the second wait
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    assert args.func(args, tmp_path) == 0

    assert passes == [True, False]  # history only on the first pass
    assert sleeps == [30.0, 30.0]
    assert json.loads(capsys.readouterr().out.splitlines()[0])["cheat"] == 0


def test_route_alarms_command_rejects_bad_interval_and_once_plus_interval(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _parse("route-alarms", "--once", "--interval", "30")
    args = _parse("route-alarms", "--interval", "0")
    with pytest.raises(ValueError, match="--interval must be finite and positive"):
        args.func(args, tmp_path)


# --------------------------------------------------------------------------- launchd template


def test_launchd_template_is_a_capped_shadow_scan_every_two_minutes() -> None:
    template = PLIST.read_text(encoding="utf-8")
    loaded = plistlib.loads(PLIST.read_bytes())

    assert loaded["Label"] == "com.petermakhnatch.evallab.live-supervisor"
    assert loaded["StartInterval"] == 120
    assert loaded["RunAtLoad"] is True
    assert loaded["WorkingDirectory"] == "__WORKDIR__"
    argv = loaded["ProgramArguments"]
    assert argv[:3] == ["__EVALLAB__", "live-supervisor", "scan"]
    assert argv[argv.index("--reader") + 1] == "model"
    assert int(argv[argv.index("--max-tokens-per-day") + 1]) > 0
    assert "act" not in argv and "--mode" not in template
    environment = loaded["EnvironmentVariables"]
    assert environment["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"
    assert environment["PATH"].startswith("__HOME__/.local/bin:")
    assert loaded["StandardOutPath"] == "__LOG_DIR__/live-supervisor.out"
    assert loaded["StandardErrorPath"] == "__LOG_DIR__/live-supervisor.err"
    assert "__EVALLAB__" in template and "__LOG_DIR__" in template


def test_launchd_template_routes_alarms_every_two_minutes() -> None:
    alarms = PLIST.parent / "com.petermakhnatch.evallab.live-supervisor-alarms.plist"
    loaded = plistlib.loads(alarms.read_bytes())

    assert loaded["Label"] == "com.petermakhnatch.evallab.live-supervisor-alarms"
    assert loaded["StartInterval"] == 120
    assert loaded["ProgramArguments"] == ["__EVALLAB__", "live-supervisor", "route-alarms", "--once"]
    assert loaded["EnvironmentVariables"]["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"
    assert loaded["StandardOutPath"] == "__LOG_DIR__/live-supervisor-alarms.out"
    assert "--replay-existing" not in loaded["ProgramArguments"]
