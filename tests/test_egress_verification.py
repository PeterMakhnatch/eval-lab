"""Fail-closed arms of the in-sandbox egress probe classifier and verdict (HAR-382).

Stdlib only: every probe runs through a fake ``execute`` callable, never
Docker, a network socket or a Harbor sandbox. A trial counts as
internet-blocked only through ``verdict``; these tests pin that a network
failure message is the only exit-1 block, an unknown probe kind never counts
as blocked or reachable, a missing probe never verifies, and cancellation
propagates instead of becoming a recorded probe error.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.egress_verification import (
    BLOCKED,
    ERROR,
    PROBES,
    Probe,
    classify,
    run_probes,
    verdict,
)

_TCP = next(probe for probe in PROBES if probe.kind == "tcp")


def _result(return_code: Any, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(return_code=return_code, stdout=stdout, stderr=stderr)


def _blocked_answer(probe: Probe) -> SimpleNamespace:
    """What a working, fully locked sandbox answers for ``probe``."""
    if probe.kind == "control_tcp":
        return _result(1, "", "bash: connect: Connection refused")
    if probe.kind == "control_dns":
        return _result(0, "127.0.0.1 localhost")
    if probe.kind == "dns":
        return _result(2)
    return _result(124)


async def _blocked_network(command: str, timeout_sec: int) -> SimpleNamespace:
    probe = next(probe for probe in PROBES if probe.command == command)
    return _blocked_answer(probe)


def _blocked_records() -> list[dict[str, Any]]:
    return asyncio.run(run_probes(_blocked_network))


def test_blocked_baseline_verifies() -> None:
    records = _blocked_records()
    assert verdict(records) == (True, "controls passed and every DNS and TCP probe was blocked")


@pytest.mark.parametrize(
    "stderr",
    [
        "bash: connect: Connection refused",
        "bash: connect: Network is unreachable",
        "bash: connect: No route to host",
        "bash: connect: Connection timed out",
        "bash: connect: Host is unreachable",
        "bash: connect: Operation not permitted",
        "bash: connect: Permission denied",
    ],
)
def test_tcp_exit_one_with_a_network_failure_message_is_blocked(stderr: str) -> None:
    outcome, detail = classify(_TCP, 1, "", f"{stderr}\nbash: line 1: /dev/tcp/1.1.1.1/443: x")
    assert outcome == BLOCKED
    assert stderr.split(": ")[-1] in detail


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "bash: /dev/tcp/1.1.1.1/443: No such file or directory",
        "bash: line 1: 3: Bad file descriptor",
    ],
)
def test_tcp_exit_one_without_a_network_failure_message_is_an_error(stderr: str) -> None:
    outcome, _ = classify(_TCP, 1, "", stderr)
    assert outcome == ERROR


@pytest.mark.parametrize("return_code", [2, 127])
def test_network_failure_message_needs_exit_one(return_code: int) -> None:
    outcome, _ = classify(_TCP, return_code, "", "bash: connect: Connection refused")
    assert outcome == ERROR


@pytest.mark.parametrize(
    ("return_code", "stdout", "stderr"),
    [
        (0, "93.184.216.34 STREAM example.com", ""),  # a "success" answer
        (124, "", ""),  # a timeout that would be a block for tcp or dns
        (2, "", ""),  # a "no answer" that would be a dns block
        (1, "", "bash: connect: Connection refused"),  # a tcp network failure
    ],
)
def test_unknown_probe_kind_is_an_error_never_blocked_or_reachable(
    return_code: int, stdout: str, stderr: str
) -> None:
    probe = dataclasses.replace(_TCP, name="udp4_cloudflare", kind="udp")
    outcome, detail = classify(probe, return_code, stdout, stderr)
    assert outcome == ERROR
    assert detail == "unknown probe kind 'udp'"


def test_unknown_probe_kind_in_the_plan_leaves_the_lock_unverified() -> None:
    odd = dataclasses.replace(_TCP, name="udp4_cloudflare", kind="udp", command="odd-probe")
    plan = (*PROBES, odd)

    async def execute(command: str, timeout_sec: int) -> SimpleNamespace:
        if command == odd.command:
            return _result(124)
        return await _blocked_network(command, timeout_sec)

    records = asyncio.run(run_probes(execute, plan))
    assert {r["name"]: r["outcome"] for r in records}["udp4_cloudflare"] == ERROR
    verified, reason = verdict(records, plan)
    assert verified is False
    assert reason == "block could not be established (probe errors): udp4_cloudflare"


@pytest.mark.parametrize("absent", [probe.name for probe in PROBES])
def test_verdict_with_a_probe_absent_is_unverified(absent: str) -> None:
    records = [record for record in _blocked_records() if record["name"] != absent]
    assert verdict(records) == (False, f"probes did not run: {absent}")


def test_verdict_with_no_results_names_every_probe() -> None:
    verified, reason = verdict([])
    assert verified is False
    assert reason == "probes did not run: " + ", ".join(probe.name for probe in PROBES)


def test_extra_records_do_not_stand_in_for_a_missing_probe() -> None:
    records = [r for r in _blocked_records() if r["name"] != "tcp6_google"]
    records.append({"name": "tcp6_google_retry", "outcome": BLOCKED})
    records.append({"outcome": BLOCKED})  # a record with no name at all
    assert verdict(records) == (False, "probes did not run: tcp6_google")


def test_cancellation_raised_by_execute_propagates_and_records_nothing() -> None:
    async def execute(command: str, timeout_sec: int) -> SimpleNamespace:
        if command == _TCP.command:
            raise asyncio.CancelledError
        return await _blocked_network(command, timeout_sec)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_probes(execute))


def test_cancelling_the_probe_run_propagates_instead_of_a_probe_error() -> None:
    started = 0

    async def execute(command: str, timeout_sec: int) -> SimpleNamespace:
        nonlocal started
        started += 1
        await asyncio.sleep(3600)
        raise AssertionError("a cancelled probe must not resume")

    async def cancel_mid_run() -> list[dict[str, Any]]:
        task = asyncio.ensure_future(run_probes(execute))
        while started < len(PROBES):
            await asyncio.sleep(0)
        task.cancel()
        return await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_mid_run())
    assert started == len(PROBES)


def test_ordinary_exec_exception_is_still_a_recorded_error() -> None:
    # Contrast with cancellation: a failing exec channel is one probe's error.
    async def execute(command: str, timeout_sec: int) -> SimpleNamespace:
        if command == _TCP.command:
            raise RuntimeError("exec channel closed")
        return await _blocked_network(command, timeout_sec)

    records = asyncio.run(run_probes(execute))
    record = next(r for r in records if r["name"] == _TCP.name)
    assert record["outcome"] == ERROR
    assert record["detail"] == "exec failed: RuntimeError: exec channel closed"
    assert verdict(records)[0] is False
