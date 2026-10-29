"""Scan and serve route the model reader through the configured subscription models."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_cli as cli
from evallab.production import live_supervisor_triage as triage
from evallab.production import run_atlas

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
TRIAGE = "openai-codex/gpt-6.1-terra"
DECIDE = "openai-codex/gpt-6.1-sol"


def test_default_claude_routes_are_unchanged() -> None:
    assert (triage.TRIAGE_MODEL, triage.DECIDE_MODEL, triage.DEEP_MODEL) == (
        "anthropic/claude-haiku-5-5",
        "anthropic/claude-sonnet-5-5",
        "anthropic/claude-opus-5-5",
    )


class Source:
    name = "harbor"

    def __init__(self, elapsed: int = 600) -> None:
        self.elapsed = elapsed

    def observe(self, now: datetime) -> list[ls.Observation]:
        subject = ls.Subject("harbor_trial", "job/trial-1", "lab", killable=True)
        return [ls.Observation(subject, now, (ls.Event(3, now, "agent", "step"),),
                               {"elapsed_seconds": self.elapsed})]

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        return [ls.TriggerHit("repetition", "loop", 2.0, "same command", (3,))]


class Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    def complete(self, model: str, messages: object, *, max_tokens: int) -> run_atlas.Completion:
        self.calls.append(model)
        answer = json.dumps({"action": "kill_and_retry", "kind": "loop", "confidence": 0.8,
                             "reason": "repeated command", "evidence_steps": [3]})
        return run_atlas.Completion(answer, model, 100, 20, 0.5)

    def close(self) -> None:
        self.closed = True


def parse(command: str, root: Path, *flags: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.register_parser(parser.add_subparsers(dest="action", required=True))
    return parser.parse_args(["live-supervisor", command, "--root", str(root), "--reader", "model",
                              "--max-tokens-per-day", "10000", *flags])


@pytest.mark.parametrize("command", ["scan", "serve"])
@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), (triage.TRIAGE_MODEL, triage.DECIDE_MODEL)),
        (("--triage-model", TRIAGE), (TRIAGE, triage.DECIDE_MODEL)),
        (("--decide-model", DECIDE), (triage.TRIAGE_MODEL, DECIDE)),
        (("--triage-model", TRIAGE, "--decide-model", DECIDE), (TRIAGE, DECIDE)),
    ],
)
def test_command_routes_reader_and_checks_all_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
    flags: tuple[str, ...], expected: tuple[str, str],
) -> None:
    transport = Transport()
    checked: list[tuple[str, ...]] = []
    monkeypatch.setattr(run_atlas, "GatewayTransport", lambda: transport)
    monkeypatch.setattr(cli, "build_sources", lambda *args, **kwargs: [Source()])

    def headroom(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        checked.append(tuple(routes))
        return list(routes), [], "test"

    monkeypatch.setattr(cli, "_headroom_check", lambda: headroom)
    if command == "serve":
        serve = cli.serve
        monkeypatch.setattr(cli, "serve", lambda **kwargs: serve(**kwargs, cycles=1))
    args = parse(command, tmp_path, *flags)
    assert args.func(args, tmp_path) == 0
    assert checked == [(*expected, triage.DEEP_MODEL)]
    assert transport.calls == list(expected)
    assert transport.closed


@pytest.mark.parametrize("command", ["scan", "serve"])
def test_long_running_kill_uses_default_opus_with_custom_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    transport = Transport()
    checked: list[tuple[str, ...]] = []
    monkeypatch.setattr(run_atlas, "GatewayTransport", lambda: transport)
    monkeypatch.setattr(cli, "build_sources", lambda *args, **kwargs: [Source(elapsed=1801)])

    def headroom(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        checked.append(tuple(routes))
        return list(routes), [], "test"

    monkeypatch.setattr(cli, "_headroom_check", lambda: headroom)
    if command == "serve":
        serve = cli.serve
        monkeypatch.setattr(cli, "serve", lambda **kwargs: serve(**kwargs, cycles=1))
    args = parse(command, tmp_path, "--triage-model", TRIAGE, "--decide-model", DECIDE)
    assert args.func(args, tmp_path) == 0
    assert checked == [(TRIAGE, DECIDE, triage.DEEP_MODEL)]
    assert transport.calls == [TRIAGE, triage.DEEP_MODEL]
    assert transport.closed


@pytest.mark.parametrize("blocked", [TRIAGE, DECIDE, triage.DEEP_MODEL])
def test_quota_gate_blocks_every_configured_route_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str,
) -> None:
    transport = Transport()
    checked: list[tuple[str, ...]] = []
    monkeypatch.setattr(run_atlas, "GatewayTransport", lambda: transport)

    def headroom(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
        checked.append(tuple(routes))
        return [route for route in routes if route != blocked], ["exhausted"], "test"

    with cli.decider_for("model", root=tmp_path, daily_token_budget=10000,
                         triage_model=TRIAGE, decide_model=DECIDE, check=headroom) as reader:
        obs = Source().observe(NOW)[0]
        hits = Source().triggers(obs)
        decision = reader.decide(obs, hits, ls.propose(obs.subject, hits))
    assert checked == [(TRIAGE, DECIDE, triage.DEEP_MODEL)]
    assert decision.action == "alert_research"
    assert blocked in decision.reason
    assert transport.calls == []
    assert transport.closed


@pytest.mark.parametrize("flag", ["--triage-model", "--decide-model"])
def test_non_subscription_route_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    transport = Transport()
    monkeypatch.setattr(run_atlas, "GatewayTransport", lambda: transport)
    monkeypatch.setattr(cli, "build_sources", lambda *args, **kwargs: [])
    args = parse("scan", tmp_path, flag, "openai/gpt-4o")
    with pytest.raises(ValueError, match="not a subscription route"):
        args.func(args, tmp_path)
    assert transport.calls == []
