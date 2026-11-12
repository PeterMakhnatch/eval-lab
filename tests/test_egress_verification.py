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
from types import SimpleNamespace

from evallab.egress_verification import (
    PROBES,
)

_TCP = next(probe for probe in PROBES if probe.kind == "tcp")


def test_cancelling_the_probe_run_propagates_instead_of_a_probe_error() -> None:
    started = 0

    async def execute(command: str, timeout_sec: int) -> SimpleNamespace:
        nonlocal started
        started += 1
        await asyncio.sleep(3600)
        raise AssertionError("a cancelled probe must not resume")


    assert started == len(PROBES)


