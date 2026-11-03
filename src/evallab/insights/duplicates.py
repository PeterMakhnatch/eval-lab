"""Deterministic text-neighbour and duplicate checks for insight cards."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable

from evallab.insights.models import InsightCard, InsightRecord

# A conservative near-exact cutoff: ordinary edits to a sentence or title stay
# above it, while a changed conclusion or evidence scope falls well below it.
DUPLICATE_THRESHOLD = 0.92


def normalized_card_text(card: InsightCard) -> str:
    text = unicodedata.normalize("NFKD", f"{card.title} {card.statement}").casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def text_similarity(left: InsightCard, right: InsightCard) -> float:
    a, b = normalized_card_text(left), normalized_card_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _tokens(card: InsightCard) -> set[str]:
    return set(normalized_card_text(card).split())


def nearest_cards(
    card: InsightCard,
    candidates: Iterable[InsightRecord],
    *,
    withheld: set[str] | None = None,
    limit: int = 3,
) -> list[dict[str, str | float]]:
    """Return the nearest public prior cards, without author or pass identity."""
    omitted = withheld or set()
    card_claims = {ref.claim_id for ref in card.evidence}
    ranked: list[tuple[float, float, str, dict[str, str | float]]] = []
    for candidate in candidates:
        if candidate.hidden or candidate.status == "rejected" or candidate.insight_id in omitted:
            continue
        score = text_similarity(card, candidate.card)
        other_claims = {ref.claim_id for ref in candidate.card.evidence}
        union = card_claims | other_claims
        claim_overlap = len(card_claims & other_claims) / len(union) if union else 0.0
        ranked.append((score, claim_overlap, candidate.insight_id, {
            "insight_id": candidate.insight_id,
            "score": round(score, 4),
            "claim_overlap": round(claim_overlap, 4),
        }))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def duplicate_of(
    card: InsightCard, candidates: Iterable[InsightRecord]
) -> tuple[str, float] | None:
    """Return the closest prior near-exact card, using deterministic id tie-breaks."""
    nearest = nearest_cards(card, candidates, limit=1)
    if not nearest:
        return None
    match = nearest[0]
    score = float(match["score"])
    if score >= DUPLICATE_THRESHOLD:
        return str(match["insight_id"]), score
    return None
