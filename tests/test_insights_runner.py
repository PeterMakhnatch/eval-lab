"""Synthetic receipts and injected processes; never launch a model or probe the host."""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
import yaml

from evallab.insights import runner
from evallab.insights.runner import (
    OmpLauncher,
    UsageGate,
    WorkerSpec,
    attest_session,
    classify_failure,
    host_ok,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
ROUTE = "anthropic/claude-sonnet-5-5"


def _assistant(
    provider: str = "anthropic", model: str = "claude-sonnet-5-5", *, error: bool = False,
) -> dict[str, object]:
    return {
        "type": "message", "id": "synthetic", "parentId": None,
        "timestamp": "2026-11-02T12:00:00.000Z", "runtime": {"version": "18.14.2"},
        "message": {
            "role": "assistant", "provider": provider, "model": model,
            "stopReason": "error" if error else "stop",
            "content": [{"type": "text", "text": "Synthetic response."}],
            "usage": {"input": 10, "output": 4, "cacheRead": 2, "cacheWrite": 3, "totalTokens": 19},
        },
    }


def test_attestation_counts_served_errors_fallbacks_and_nested_files(tmp_path: Path) -> None:
    events = [
        {"type": "model_change", "model": ROUTE, "resolvedModelIsFallback": False},
        _assistant(), _assistant(model="claude-sonnet-5-5:high"),
        _assistant(provider="openai-codex", model="gpt-6.1-terra", error=True),
        {"type": "custom", "data": {"resolvedModelIsFallback": True}},
        {"type": "message", "message": {"role": "user", "content": "Not served."}},
        {"type": "error", "error": "A synthetic transport failure"},
    ]
    (tmp_path / "session.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n{partial", encoding="utf-8",
    )
    nested = tmp_path / "child"
    nested.mkdir()
    (nested / "child.jsonl").write_text(json.dumps(_assistant()) + "\n", encoding="utf-8")
    result = attest_session(tmp_path, ROUTE + ":high")
    assert result.served == {ROUTE: 3}
    assert result.off_route is False
    assert result.error_records == 2
    assert result.fallback_events == 1
    assert result.tokens == {
        "input": 40, "output": 16, "cacheRead": 8, "cacheWrite": 12,
        "totalTokens": 76, "reasoningTokens": 0,
    }
    assert result.model_changes == (ROUTE,)


def test_attestation_off_route_comes_from_answers_not_model_change(tmp_path: Path) -> None:
    events = [
        {"type": "model_change", "model": ROUTE},
        {"type": "model_change", "model": "kimi-code/k3.1-pro", "resolvedModelIsFallback": True},
        _assistant(), _assistant(provider="kimi-code", model="k3.1-pro:high"),
    ]
    (tmp_path / "session.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8",
    )
    result = attest_session(tmp_path, ROUTE)
    assert result.off_route
    assert result.served == {ROUTE: 1, "kimi-code/k3.1-pro": 1}
    assert result.fallback_events == 1
    assert len(result.model_changes) == 2


def test_empty_session_has_no_invented_served_model(tmp_path: Path) -> None:
    result = attest_session(tmp_path / "missing", ROUTE)
    assert result.served == {}
    assert not result.off_route


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("RESOURCE_EXHAUSTED", "quota"), ("HTTP 429 Too Many Requests", "quota"),
        ("exceeded your current quota", "quota"), ("weekly quota", "quota"),
        ("rate-limit exceeded", "quota"), ("quota ceiling reached", "quota"),
        ("HTTP 401", "auth"), ("invalid_grant", "auth"), ("login expired", "auth"),
        ("HTTP 403", "permission"), ("permission denied", "permission"),
        ("not allowed", "permission"), ("refusal", "refusal"),
        ("I cannot comply", "refusal"), ("connection closed", "other"),
    ],
)
def test_failure_classification(text: str, kind: str) -> None:
    assert classify_failure(text) == (kind, None)


def test_quota_reset_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_utc_now", lambda: NOW)
    assert classify_failure("quota-exhausted; resets at 2026-11-02T12:00:00Z") == ("quota", NOW)
    assert classify_failure("429; resets on 2026-11-02 13:00:00+01:00") == ("quota", NOW)
    assert classify_failure("429; retry-after: 90") == ("quota", NOW + timedelta(seconds=90))
    assert classify_failure("429; resets in 1.5s") == ("quota", NOW + timedelta(seconds=1.5))
    assert classify_failure(f"429; resets at {NOW.timestamp()}") == ("quota", NOW)
    assert classify_failure("429; resets at 2026-99-99T12:00:00Z") == ("quota", None)


def _usage_fixture() -> dict[str, object]:
    def limit(identity: str, label: str, fraction: float) -> dict[str, object]:
        return {
            "id": identity, "label": label, "scope": {"shared": True},
            "window": {"id": "7d", "durationMs": 604800000, "resetsAt": 1793774122000},
            "amount": {"usedFraction": fraction, "unit": "percent", "limit": 100},
        }

    return {
        "generatedAt": 1793650000000,
        "reports": [
            {"provider": "anthropic", "limits": [
                limit("anthropic:7d", "Claude 7 Day", .16),
                limit("anthropic:5h", "Claude 5 Hour", .44),
            ]},
            {"provider": "google-antigravity", "limits": [
                limit("google-antigravity:anthropic:default:3p-weekly", "Claude & GPT (shared)", .9),
                limit("google-antigravity:openai:default:3p-5h", "Claude & GPT (shared)", .2),
                limit("google-antigravity:google:default:gemini-weekly", "Gemini", .17),
                limit("google-antigravity:google:default:gemini-5h", "Gemini", .03),
            ]},
            {"provider": "kimi-code", "limits": [
                {"amount": {"used": 3, "limit": 10, "unit": "unknown"}},
                {"amount": {"used": 82, "unit": "percent"}},
            ]},
            {"provider": "meta", "limits": [{"amount": {"used": 3, "unit": "usd"}}]},
        ],
        "accountsWithoutUsage": [{"provider": "zai", "type": "api_key"}],
    }


def test_usage_parsing_caching_and_antigravity_split() -> None:
    calls: list[list[str]] = []
    clock = [100.0]

    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output and text and timeout == 60
        return subprocess.CompletedProcess(args, 0, json.dumps(_usage_fixture()), "")

    gate = UsageGate(run=run, clock=lambda: clock[0], ttl_seconds=600)
    assert gate.used_percent("anthropic") == pytest.approx(44)
    assert gate.used_percent("kimi-code") == pytest.approx(82)
    assert gate.used_percent("google-antigravity") == pytest.approx(90)
    assert gate.used_percent("google-antigravity", "google-antigravity/gemini-3.8-flash") == 17
    assert gate.used_percent("google-antigravity", "google-antigravity/claude-sonnet-5-5") == 90
    assert gate.used_percent("google-antigravity", "gpt-6.1-terra") == 90
    assert gate.used_percent("zai") is None
    assert gate.used_percent("meta") is None
    assert calls == [["omp", "usage", "--json"]]
    clock[0] = 699
    assert gate.used_percent("anthropic") == pytest.approx(44)
    assert len(calls) == 1
    clock[0] = 700
    gate.used_percent("anthropic")
    assert len(calls) == 2


def test_usage_failure_unknown_and_cached() -> None:
    calls = 0

    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(args, timeout)

    gate = UsageGate(run=run, clock=lambda: 0)
    assert gate.used_percent("anthropic") is None
    assert gate.used_percent("anthropic") is None
    assert calls == 1


def test_missing_gemini_bucket_does_not_inherit_shared_usage() -> None:
    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        payload = {"reports": [{"provider": "google-antigravity", "limits": [
            {"id": "google-antigravity:anthropic:default:3p-weekly",
             "label": "Claude & GPT (shared)", "amount": {"usedFraction": .99}},
        ]}]}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    gate = UsageGate(run=run, clock=lambda: 0)
    assert gate.used_percent("google-antigravity") == 99
    assert gate.used_percent("google-antigravity", "gemini-3.8-flash") is None


def test_expired_usage_is_not_reused_after_failed_refresh() -> None:
    clock = [0.0]
    calls = 0

    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        output = json.dumps(_usage_fixture()) if calls == 1 else "not json"
        return subprocess.CompletedProcess(args, 0, output, "")

    gate = UsageGate(run=run, clock=lambda: clock[0], ttl_seconds=10)
    assert gate.used_percent("anthropic") == pytest.approx(44)
    clock[0] = 10
    assert gate.used_percent("anthropic") is None
    assert calls == 2


def test_launcher_argv_environment_and_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_INSIGHTS_STATE", str(tmp_path / "ledger-state"))
    captured: dict[str, object] = {}

    class Child:
        pid = 12345

        def poll(self) -> int | None:
            return None

    def popen(
        args: list[str], *, cwd: Path, env: dict[str, str], stdin: int,
        stdout: BinaryIO, stderr: int, start_new_session: bool,
    ) -> Child:
        captured.update(
            args=args, cwd=cwd, env=env, stdin=stdin, stdout=stdout,
            stderr=stderr, start_new_session=start_new_session,
        )
        stdout.write(b"synthetic startup\n")
        return Child()

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Synthetic prompt.", encoding="utf-8")
    handle = OmpLauncher(omp="/local/bin/omp", popen=popen).launch(
        WorkerSpec("pass-1", ROUTE, "high", 2.5, tmp_path, prompt, "read,bash"),
    )
    assert handle.pid == 12345
    assert handle.poll() is None
    assert captured["args"] == [
        "/local/bin/omp", "-p", "--model", ROUTE, "--thinking", "high",
        "--session-dir", str(tmp_path / "session"), "--no-skills", "--tools", "read,bash",
        "--max-time", "150", "--config", str(tmp_path / "worker.yml"), f"@{prompt}",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stderr"] is not subprocess.STDOUT  # omp's own errors stay separate
    assert captured["start_new_session"] is True
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HARBOR_TELEMETRY"] == "off"
    assert env["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"
    assert env["EVALLAB_INSIGHTS_PASS"] == "pass-1"
    assert "EVALLAB_INSIGHTS_STATE" not in env  # workers must not learn the ledger path
    assert (tmp_path / "worker.log").read_text() == "synthetic startup\n"
    assert (tmp_path / "worker.stderr").exists()
    overlay = yaml.safe_load((tmp_path / "worker.yml").read_text())
    assert overlay["retry"] == {"modelFallback": False, "maxRetries": 4}
    assert overlay["modelRoles"] == dict.fromkeys(
        ("default", "plan", "slow", "vision", "advisor", "task", "smol", "tiny",
         "commit", "memory", "judge"), ROUTE + ":high",
    )
    assert overlay["mnemopi"] == {"autoRetain": False, "autoRecall": False}
    assert overlay["autolearn"] == {"enabled": False}
    assert overlay["bash"] == {"autoBackground": {"enabled": False}}


def test_kill_terminates_group_then_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    ticks = iter([0.0, 0.0, 6.0])
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    handle = runner._ProcessHandle(123, lambda: None)
    handle.kill()
    assert sent == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_kill_does_not_escalate_after_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    statuses = iter([None, 0])
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0.0)
    handle = runner._ProcessHandle(123, lambda: next(statuses))
    handle.kill()
    assert sent == [(123, signal.SIGTERM)]


def test_adoption_requires_live_pid_and_exact_session_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_dir = tmp_path / "pass with spaces"
    session = pass_dir / "session"
    alive = [True]

    def kill(pid: int, sig: int) -> None:
        assert pid == 123 and sig == 0
        if not alive[0]:
            raise ProcessLookupError

    monkeypatch.setattr(runner.os, "kill", kill)
    launcher = OmpLauncher(ps=lambda _: f"omp -p --session-dir {shlex.quote(str(session))}")
    handle = launcher.adopt(123, pass_dir)
    assert handle is not None
    assert handle.poll() is None
    assert OmpLauncher(ps=lambda _: "omp --session-dir /somewhere/session").adopt(123, pass_dir) is None
    prefix = OmpLauncher(ps=lambda _: f"omp --session-dir {shlex.quote(str(session) + '-other')}")
    assert prefix.adopt(123, pass_dir) is None
    alive[0] = False
    assert launcher.adopt(123, pass_dir) is None
    assert handle.poll() == 1  # Unknown exit must not be reported as successful.


def test_adoption_matches_real_unquoted_ps_output_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_dir = tmp_path / "pass with spaces"
    session = pass_dir / "session"
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: None)
    # ps -o command= does not quote its arguments.
    command = f"/usr/bin/omp -p --model x --session-dir {session} --no-skills @prompt.md"
    assert OmpLauncher(ps=lambda _: command).adopt(123, pass_dir) is not None
    longer = f"omp -p --session-dir {session}-other --no-skills"
    assert OmpLauncher(ps=lambda _: longer).adopt(123, pass_dir) is None
    assert OmpLauncher(ps=lambda _: f"omp --session-dir={session}").adopt(123, pass_dir) is not None


def test_adopted_handle_never_signals_a_reused_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_dir = tmp_path / "pass"
    session = pass_dir / "session"
    sent: list[tuple[int, int]] = []
    commands = [f"omp -p --session-dir {session}"]
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    handle = OmpLauncher(ps=lambda _: commands[-1]).adopt(321, pass_dir)
    assert handle is not None
    assert handle.poll() is None
    commands.append("/usr/bin/vim notes.txt")  # the PID now belongs to someone else
    assert handle.poll() == 1
    handle.kill()
    assert sent == []


@pytest.mark.parametrize(
    ("swap", "memory", "expected"),
    [("used = 1024.00M", "50", True), ("used = 13000M", "50", False),
     ("used = 1G", "10", False), ("used = 12G", "15", True),
     ("unavailable", "bad", True)],
)
def test_host_gate_uses_injected_probes(swap: str, memory: str, expected: bool) -> None:
    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == "/usr/sbin/sysctl"  # not on launchd's minimal PATH otherwise
        output = swap if args[-1] == "vm.swapusage" else memory
        return subprocess.CompletedProcess(args, 0, output, "")

    assert host_ok(15, 12, run=run) is expected


def test_host_probe_errors_allow_launch() -> None:
    def run(
        args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("No sysctl here")

    assert host_ok(15, 12, run=run)


def test_attestation_diagnostics_come_only_from_error_records(tmp_path: Path) -> None:
    chatty = _assistant()
    chatty["message"]["content"] = [  # type: ignore[index]
        {"type": "text", "text": "n=429 trials hit the rate-limit; daily quota ceiling"},
    ]
    failure = _assistant(error=True)
    failure["message"]["errorMessage"] = "HTTP 429 quota exhausted"  # type: ignore[index]
    (tmp_path / "s.jsonl").write_text(
        "\n".join(json.dumps(event) for event in (chatty, failure)), encoding="utf-8",
    )
    result = attest_session(tmp_path, ROUTE)
    assert "quota exhausted" in result.diagnostics
    assert "daily quota ceiling" not in result.diagnostics
    assert classify_failure(result.diagnostics)[0] == "quota"
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    (quiet / "s.jsonl").write_text(json.dumps(chatty), encoding="utf-8")
    assert classify_failure(attest_session(quiet, ROUTE).diagnostics)[0] == "other"


def test_launchd_environment_can_find_sysctl_and_is_unbuffered() -> None:
    import plistlib

    launchd = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "launchd"
    plist = plistlib.loads(
        (launchd / "com.petermakhnatch.evallab.insights.plist").read_bytes()
    )
    environment = plist["EnvironmentVariables"]
    assert {"/usr/sbin", "/sbin"} <= set(environment["PATH"].split(":"))
    assert environment["PYTHONUNBUFFERED"] == "1"
    installer = (launchd / "install-insights.sh").read_text("utf-8")
    assert "/usr/bin:/bin:/usr/sbin:/sbin" in installer  # the installer rewrites PATH
    assert 'PYTHONUNBUFFERED"] = "1"' in installer
