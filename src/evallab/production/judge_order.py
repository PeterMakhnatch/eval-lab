"""Judge queue order: spend the judge's time where a human label tells us the most.

Every queued run gets a *stratum* from the stored detector verdicts (never from a label):

``disagree``       at least one detector or model alarms and at least one clears it. A human
                   verdict settles the disagreement, so these come first, the most evenly split
                   (most minority voters) before the lopsided ones.
``l0_autoreview``  nobody clears it and an L0 tripwire or an AutoReview verdict alarms.
``alarm``          nobody clears it and some other detector alarms.
``control``        nothing alarms (all cleared, or nothing stored), or the queue marks the row
                   ``control``. Controls are the calibration: they show how often a run every
                   detector passes is still a cheat.

Priority strata are ordered ``disagree``, ``l0_autoreview``, ``alarm``. Controls are spread
evenly through the whole sequence: each slot is a control with probability equal to the share of
controls still unplaced (a floor ``control_rate`` front-loads them if wanted). Priority runs keep
their order relative to each other, so position says nothing about the stratum of a run. The order
is a pure function of the seed, the queue name and the run ids.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_SEED = "judge-v2"
DEFAULT_CONTROL_RATE = 0.0
L0_PREFIXES = ("tripwire", "flags", "l0")
AUTOREVIEW_PREFIX = "autoreview"
#: Verdict words of other producers (AutoReview) read as an alarm or a clear.
VERDICT_ALIASES = {"confirmed_cheat": "alarm", "suspicious": "alarm", "clean": "clear"}
STRATA = ("disagree", "l0_autoreview", "alarm", "control")


STRATUM_TEXT = {
    "disagree": "our detectors or models disagreed about this run",
    "l0_autoreview": "a deterministic tripwire or the AutoReview monitor alarmed and nothing cleared it",
    "alarm": "a detector alarmed and nothing cleared it",
    "control": "a control: nothing alarmed (or it was sampled as a control)",
}


@dataclass(frozen=True)
class Signals:
    """The decided detector verdicts of one run (``alarm`` and ``clear`` only)."""

    alarms: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()


def collect_signals(
    detectors: Sequence[Mapping[str, Any]], extra: Mapping[str, str] | None = None
) -> Signals:
    """Distinct alarming and clearing detector names; ``extra`` is ``{name: alarm|clear}``."""
    alarms: set[str] = set()
    clears: set[str] = set()
    pairs = [
        (
            f"{d.get('name')}@{d['model']}" if d.get("model") else str(d.get("name")),
            VERDICT_ALIASES.get(str(d.get("verdict")), str(d.get("verdict"))),
        )
        for d in detectors
    ]
    pairs += [(str(name), VERDICT_ALIASES.get(str(v), str(v))) for name, v in (extra or {}).items()]
    for name, verdict in pairs:
        if verdict == "alarm":
            alarms.add(name)
        elif verdict == "clear":
            clears.add(name)
    return Signals(tuple(sorted(alarms)), tuple(sorted(clears)))


def _family(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith(AUTOREVIEW_PREFIX):
        return "autoreview"
    return "l0" if lowered.startswith(L0_PREFIXES) else "other"


def stratum_of(signals: Signals, *, control: bool = False) -> str:
    if control:
        return "control"
    if signals.alarms and signals.clears:
        return "disagree"
    if signals.alarms:
        families = {_family(name) for name in signals.alarms}
        return "l0_autoreview" if families & {"l0", "autoreview"} else "alarm"
    return "control"


def _strength(stratum: str, signals: Signals) -> int:
    if stratum == "disagree":
        return min(len(signals.alarms), len(signals.clears))
    families = {_family(name) for name in signals.alarms}
    return len(signals.alarms) + (1 if {"l0", "autoreview"} <= families else 0)


def _hash(seed: str, queue: str, run_id: str) -> str:
    return hashlib.sha256(f"{seed}:{queue}:{run_id}".encode()).hexdigest()


def order_runs(
    queue: str,
    runs: Sequence[tuple[Any, ...]],
    *,
    seed: str = DEFAULT_SEED,
    control_rate: float = DEFAULT_CONTROL_RATE,
) -> list[tuple[str, str]]:
    """``[(run_id, stratum)]`` in judging order for ``(run_id, signals, is_control[, boost])`` inputs.

    ``boost`` (a queue producer's priority, default 0) breaks ties between equally strong runs of
    one stratum, higher first."""
    if not 0.0 <= control_rate <= 1.0:
        raise ValueError("control_rate must be between 0 and 1")
    priority: list[tuple[tuple[int, int, int, str], str, str]] = []
    controls: list[tuple[str, str]] = []
    for run in runs:
        run_id, signals, is_control = run[0], run[1], run[2]
        boost = int(run[3]) if len(run) > 3 else 0
        stratum = stratum_of(signals, control=is_control)
        key = _hash(seed, queue, run_id)
        if stratum == "control":
            controls.append((key, run_id))
        else:
            rank = (STRATA.index(stratum), -_strength(stratum, signals), -boost, key)
            priority.append((rank, run_id, stratum))
    priority.sort()
    controls.sort()
    rng = random.Random(_hash(seed, queue, "interleave"))
    ordered: list[tuple[str, str]] = []
    head, tail = 0, 0
    while head < len(priority) or tail < len(controls):
        if head == len(priority):
            take_control = True
        elif tail == len(controls):
            take_control = False
        else:
            remaining_controls = len(controls) - tail
            share = remaining_controls / (remaining_controls + len(priority) - head)
            take_control = rng.random() < max(control_rate, share)
        if take_control:
            ordered.append((controls[tail][1], "control"))
            tail += 1
        else:
            ordered.append((priority[head][1], priority[head][2]))
            head += 1
    return ordered
