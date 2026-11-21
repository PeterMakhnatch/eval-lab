from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
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


def match_playbook_cards(
    trigger_flags: str | Iterable[str],
    kind: PlaybookKind | None = None,
    max_cards: int = 2,
) -> list[PlaybookCard]:
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
