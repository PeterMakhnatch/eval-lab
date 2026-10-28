"""Trace-harness adapters for the model-free rule layers (no judge, no network).

``TripwireDetector`` replays each run through the production Stage 1 exactly as the
cascade's Stage 1 does (``MisalignmentLiveWorker`` with ``judge=None``: the tripwire
rules, the hackbook prefix checks and the ``command_prefilter`` cues) and reports the
cards it raised. ``FlagsDetector`` reports ``compute_trace_feature_flags`` with the
matched playbook cards. Neither reads labels, splits or benchmark names.

Verdicts follow the cards' own eligibility, never a new threshold: a card with
``eligibility == "verdict"`` (tripwire / hackbook verdict rules) is the Stage-1 alarm;
diagnostic-only runs (prefilter cues, diagnostic hackbook hits) abstain.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from evallab import tripwires
from evallab.hackbook.playbook_cards import match_playbook_cards
from evallab.production import command_prefilter
from evallab.production.monitor_worker import MisalignmentLiveWorker
from evallab.production.trace_feature_flags import (
    ACTION_FEATURES,
    compute_trace_feature_flags,
)
from evallab.production.trace_harness import HarnessOptions, HarnessRun, finding
from evallab.production.window_cascade import CASCADE_VERSION

TRIPWIRE_VERSION = (
    f"tripwires/v{tripwires.RULES_VERSION}+{CASCADE_VERSION}"
    f"+prefilter/{len(command_prefilter.FAMILIES)}"
)
FLAGS_VERSION = "evallab.trace_feature_flags/v1"


def _ordered(steps: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Steps in ``step_id`` order, as the Stage-1 replay tools feed them."""
    return sorted(steps, key=lambda step: int(step["step_id"]))


def stage1_cards(run_id: str, steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every Stage-1 card the production monitor raises on ``steps``, in emit order.

    Each card is ``{"rule", "origin", "eligibility", "step"}`` where ``step`` is the
    original step id the card cites. Raises ``RuntimeError`` when the deterministic
    detector reports itself unavailable for this run.
    """
    cards: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        payload = event["payload"]
        if "deterministic_detector_unavailable" in (payload.get("coverage") or {}).get("gaps", []):
            raise RuntimeError(f"Stage-1 detector unavailable: {payload['coverage'].get('detector_error')}")
        if event["event_type"] == "monitor.suspicion":
            cards.append({
                "rule": payload["rule"],
                "origin": payload.get("origin"),
                "eligibility": payload.get("eligibility"),
                "step": int(payload["evidence"][0]["step"]),
            })

    # alert_playbook_cards=0 only skips attaching playbook leads to cards; card
    # identity, rule and cited step are unchanged.
    with MisalignmentLiveWorker(emit=emit, judge=None, alert_playbook_cards=0) as worker:
        for index, step in enumerate(_ordered(steps)):
            worker.ingest_event({
                "event_id": f"{run_id}:{index}",
                "run_id": run_id,
                "event_type": "trace.step",
                "payload": {"step": step},
            })
    return cards


class TripwireDetector:
    """Production Stage 1 (tripwires + hackbook + prefilter cues), judge-free."""

    name = "tripwire"
    reader_model = None
    version = TRIPWIRE_VERSION

    def __init__(self, options: HarnessOptions | None = None) -> None:
        self.options = options

    def detect(self, runs: Sequence[HarnessRun]) -> Iterable[dict[str, Any]]:
        for run in runs:
            started = time.perf_counter()
            try:
                cards = stage1_cards(f"th-{run.run_id}", run.steps)
            except RuntimeError as error:
                yield self._record(run, "unknown", None, [], str(error), started)
                continue
            gating = [card for card in cards if card["eligibility"] == "verdict"]
            rules = sorted({card["rule"] for card in gating})
            if gating:
                yield self._record(run, "alarm", len(rules), gating, ", ".join(rules), started)
            elif cards:
                diagnostic = sorted({card["rule"] for card in cards})
                yield self._record(run, "abstain", 0, cards, "diagnostic only: " + ", ".join(diagnostic), started)
            else:
                yield self._record(run, "clear", 0, [], "no Stage-1 card", started)

    def _record(
        self, run: HarnessRun, verdict: str, score: int | None,
        cards: Sequence[Mapping[str, Any]], summary: str, started: float,
    ) -> dict[str, Any]:
        return finding(
            run.run_id, self.name, detector_version=self.version, reader_model=None,
            verdict=verdict, kind="cheat", score=score,
            evidence_steps=[card["step"] for card in cards], summary=summary,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )


class FlagsDetector:
    """Deterministic trace feature flags; an ACTION feature is the alarm."""

    name = "flags"
    reader_model = None
    version = FLAGS_VERSION

    def __init__(self, options: HarnessOptions | None = None) -> None:
        self.options = options

    def detect(self, runs: Sequence[HarnessRun]) -> Iterable[dict[str, Any]]:
        for run in runs:
            started = time.perf_counter()
            flags = compute_trace_feature_flags(run.steps)
            fired = {name: flag for name, flag in flags["features"].items() if flag["fired"]}
            step_ids = run.step_ids
            evidence = [step_ids[flag["step_indices"][0]] for flag in fired.values()]
            if not fired:
                verdict, summary = "clear", "no feature flags fired"
            else:
                verdict = "alarm" if any(name in ACTION_FEATURES for name in fired) else "abstain"
                cards = match_playbook_cards(list(fired), kind="trace")
                summary = "flags: " + ", ".join(fired)
                if cards:
                    summary += "; cards: " + ", ".join(card["card_id"] for card in cards)
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=None,
                verdict=verdict, kind="cheat", score=len(fired), evidence_steps=evidence,
                summary=summary, latency_ms=round((time.perf_counter() - started) * 1000),
            )
