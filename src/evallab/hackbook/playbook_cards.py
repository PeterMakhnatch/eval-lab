"""On-demand defensive playbooks distilled from confirmed DEV evidence.

Cards are diagnostic leads, not cheating verdicts. The packaged catalog contains
only the twelve compact cards, never source traces, private paths or audit data.
The formatter emits cards only; callers prepend their observed feature index.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from importlib.resources import files
from typing import Literal, TypedDict, cast

PlaybookKind = Literal["task", "trace"]


class PlaybookCard(TypedDict):
    card_id: str
    trigger_flags: list[str]
    mechanism_summary: str
    signature_pattern: str
    verification_recipe: str
    dev_support_count: int


@cache
def _catalog() -> tuple[PlaybookCard, ...]:
    payload = json.loads(
        files("evallab.hackbook").joinpath("playbook_cards_v1.json").read_text(encoding="utf-8")
    )
    return tuple(cast(list[PlaybookCard], payload["cards"]))


def _copy_card(card: PlaybookCard) -> PlaybookCard:
    return {**card, "trigger_flags": card["trigger_flags"].copy()}


def list_playbook_cards() -> list[PlaybookCard]:
    """Return the twelve cards in catalog order, without exposing cached mutable data."""
    return [_copy_card(card) for card in _catalog()]


def match_playbook_cards(
    trigger_flags: str | Iterable[str],
    kind: PlaybookKind | None = None,
    max_cards: int = 2,
) -> list[PlaybookCard]:
    """Return at most ``max_cards`` cards matching the supplied fired flag names.

    ``task`` selects QA/Reward-Seeker cards; ``trace`` selects Trace-Watcher cards.
    Rank by distinct matched flags, then card ID. Unknown flags have no fallback.
    Pass names of fired flags, not a boolean/state mapping (whose false keys must
    not become hits). ``issue:`` and ``label:`` names are offline evidence tags,
    not outputs of the live trace-feature detector.
    """
    if kind not in (None, "task", "trace"):
        raise ValueError("kind must be 'task', 'trace', or None")
    if max_cards < 0:
        raise ValueError("max_cards must be nonnegative")
    if isinstance(trigger_flags, Mapping):
        raise TypeError("trigger_flags must contain fired flag names, not a state mapping")
    flags = {trigger_flags} if isinstance(trigger_flags, str) else set(trigger_flags)
    if not flags or max_cards == 0:
        return []
    prefix = "QA" if kind == "task" else "TW" if kind == "trace" else ""
    ranked = [
        (len(flags.intersection(card["trigger_flags"])), card)
        for card in _catalog()
        if card["card_id"].startswith(prefix)
    ]
    ranked = [(score, card) for score, card in ranked if score]
    ranked.sort(key=lambda row: (-row[0], row[1]["card_id"]))
    return [_copy_card(card) for _, card in ranked[:max_cards]]


def format_playbook_cards(cards: Sequence[PlaybookCard]) -> str:
    """Join only the selected cards as compact JSON; return empty text for no hits.

    These v1 card payloads measure 70–90 o200k_base tokens each, including keys.
    The previously measured ~211-token median bundle also included a trace-feature
    index; this function neither invents that index nor claims a tokenizer cap.
    """
    return "\n".join(json.dumps(card, ensure_ascii=False, separators=(",", ":")) for card in cards)
