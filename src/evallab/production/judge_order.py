"""Judge queue order: spend the judge's time where a human label tells us the most.

**The order is informative about alarm status, on purpose.** Every queued run gets a *stratum* from
the stored detector verdicts (never from a label), and runs are served most-disputed first. So an
early position means "our detectors disagreed or alarmed" and a late position means "nothing
alarmed". A judge who knows that can anchor on it. Use ``--blind-order`` (a seeded shuffle that
ignores the strata) for an unanchored pass; the strata are still recorded on each verdict.

Voters. Versions of one detector family (``cascade``, ``cascade_v4``, ``cascade_v7``) and several
findings of the same reader count as one voter, which alarms if any of its findings alarms. A
different reader model is a separate voter. Producers that describe a run instead of judging it
(``facets``) do not vote; they are still revealed after the verdict.

Strata:

``disagree``       at least one voter alarms and at least one clears. A human verdict settles the
                   disagreement, so these come first, the most evenly split (most minority
                   voters) before the lopsided ones.
``l0_autoreview``  nobody clears it and an L0 tripwire or an AutoReview verdict alarms.
``alarm``          nobody clears it and some other voter alarms.
``control``        every voter cleared it, or the queue marks the row ``control``. Controls are
                   the calibration: they show how often a run every detector passes is still a cheat.
``unscored``       no stored voter verdict at all. Not a control: it is kept out of the
                   control agreement numbers, and spread through the list like a control.

Priority strata are ordered ``disagree``, ``l0_autoreview``, ``alarm``. Controls and unscored runs
are spread evenly through the whole sequence: each slot is one of them with probability equal to
the share still unplaced (a floor ``control_rate`` front-loads them if wanted). Priority runs keep
their order relative to each other. The order is a pure function of the seed, the queue name and
the run ids.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_SEED = "judge-v2"
DEFAULT_CONTROL_RATE = 0.0
L0_PREFIXES = ("tripwire", "flags", "l0")
AUTOREVIEW_PREFIX = "autoreview"
#: Verdict words of other producers (AutoReview) read as an alarm or a clear.
VERDICT_ALIASES = {"confirmed_cheat": "alarm", "suspicious": "alarm", "clean": "clear"}
STRATA = ("disagree", "l0_autoreview", "alarm", "control", "unscored")
#: Producers that describe a run rather than judge it: shown at the reveal, never a voter.
NON_VOTERS = ("facets",)


STRATUM_TEXT = {
    "disagree": "our detectors or models disagreed about this run",
    "l0_autoreview": "a deterministic tripwire or the AutoReview monitor alarmed and nothing cleared it",
    "alarm": "a detector alarmed and nothing cleared it",
    "control": "a control: every detector cleared it (or it was sampled as a control)",
    "unscored": "no detector verdict is stored for this run",
}


@dataclass(frozen=True)
class Signals:
    """The decided detector verdicts of one run (``alarm`` and ``clear`` only)."""

    alarms: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()


def _voter(name: str, model: Any) -> str | None:
    """The voter a finding belongs to: detector family (no version) plus reader model, or None."""
    base = re.sub(r"_v\d+$", "", name.split(" (")[0].strip())
    if base.lower().startswith(NON_VOTERS):
        return None
    return f"{base}@{model}" if model else base


def collect_signals(
    detectors: Sequence[Mapping[str, Any]], extra: Mapping[str, str] | None = None
) -> Signals:
    """Voters that alarm and voters that only clear; ``extra`` is ``{name: alarm|clear}``."""
    verdicts: dict[str, bool] = {}
    findings = [(d.get("name"), d.get("model"), d.get("verdict")) for d in detectors]
    findings += [(name, None, verdict) for name, verdict in (extra or {}).items()]
    for name, model, verdict in findings:
        word = VERDICT_ALIASES.get(str(verdict), str(verdict))
        voter = _voter(str(name), model)
        if voter is not None and word in ("alarm", "clear"):
            verdicts[voter] = verdicts.get(voter, False) or word == "alarm"
    return Signals(
        tuple(sorted(v for v, alarm in verdicts.items() if alarm)),
        tuple(sorted(v for v, alarm in verdicts.items() if not alarm)),
    )


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
    return "control" if signals.clears else "unscored"


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
    shuffle: bool = False,
) -> list[tuple[str, str]]:
    """``[(run_id, stratum)]`` in judging order for ``(run_id, signals, is_control[, boost])`` inputs.

    ``boost`` (a queue producer's priority, default 0) breaks ties between equally strong runs of
    one stratum, higher first. ``shuffle`` ignores the strata and returns a seeded shuffle (the
    strata are still returned)."""
    if not 0.0 <= control_rate <= 1.0:
        raise ValueError("control_rate must be between 0 and 1")
    priority: list[tuple[tuple[int, int, int, str], str, str]] = []
    controls: list[tuple[str, str, str]] = []
    everyone: list[tuple[str, str, str]] = []
    for run in runs:
        run_id, signals, is_control = run[0], run[1], run[2]
        boost = int(run[3]) if len(run) > 3 else 0
        stratum = stratum_of(signals, control=is_control)
        key = _hash(seed, queue, run_id)
        everyone.append((key, run_id, stratum))
        if stratum in ("control", "unscored"):
            controls.append((key, run_id, stratum))
        else:
            rank = (STRATA.index(stratum), -_strength(stratum, signals), -boost, key)
            priority.append((rank, run_id, stratum))
    if shuffle:
        return [(run_id, stratum) for _, run_id, stratum in sorted(everyone)]
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
            ordered.append((controls[tail][1], controls[tail][2]))
            tail += 1
        else:
            ordered.append((priority[head][1], priority[head][2]))
            head += 1
    return ordered
