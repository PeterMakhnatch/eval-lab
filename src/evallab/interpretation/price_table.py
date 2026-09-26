"""Pinned provider list prices for run-report cost estimates.

``evallab.report run`` uses this table ONLY when a trial carries no
harness-reported cost (no ``agent_result.cost_usd``, no trajectory
``final_metrics.total_cost_usd``, no per-step ``cost_usd``). The resulting
figure is reported as ``source: price_table_estimate`` with full provenance
(model as seen, matched key, source URL, retrieval date, applied rates) and
is never added to, or mixed with, harness-reported spend.

Every entry below was read from the provider's PRIMARY pricing page on
2026-09-26 (see ``source_url`` per entry). Models present in retained runs
that have no public per-token list price (subscription-only seats such as
``antigravity`` tiers beyond the base ``gemini-3.7-flash`` API model, or
proxy aliases) are deliberately absent: an unknown model yields no estimate,
never a fabricated zero.

Existing repo rate conventions this table relates to (not duplicated here):
- ``containers/zai_openapi_secret_proxy.py`` ``DEFAULT_*_COST_MICROS_PER_MILLION``
  ($0.15 / $0.50 for ``zai/glm-5.3-flash``) cites the same Z.ai pricing page
  and matches the pinned values below; the cached-input rate ($0.03) is new.
- ``ZAI_INPUT/OUTPUT_COST_MICROS_PER_MILLION`` in ``execution_contracts.py``
  ($1.40 / $4.40) match the pinned ``glm-5.3`` list prices below.
- The ``EVALLAB_DEEPSEEK_*_COST_MICROS_PER_MILLION`` compose defaults
  ($0.28 / $0.42 per Mtok) match NOTHING on the official DeepSeek pricing
  page (``deepseek-flash`` is peak $0.30 in / $1.20 out, off-peak $0.15 /
  $0.60, plus a separate cache-hit rate), so no DeepSeek entry is pinned:
  a single static rate cannot represent that schedule honestly.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every entry below was verified against its ``source_url`` on this date.
PRICE_TABLE_RETRIEVED_ON = "2026-09-26"


@dataclass(frozen=True)
class ModelPrice:
    """Pinned Standard-tier list price, USD per million tokens."""

    key: str  # normalized model id this entry matches, e.g. "glm-5.3-flash"
    input_usd_per_mtok: float
    cached_input_usd_per_mtok: float
    output_usd_per_mtok: float
    source_url: str
    retrieved_on: str = PRICE_TABLE_RETRIEVED_ON


_ZAI_PRICING_URL = "https://docs.z.ai/guides/overview/pricing.md"
_OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
_GOOGLE_PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"

PRICE_TABLE: tuple[ModelPrice, ...] = (
    ModelPrice(
        key="glm-5.3-flash",
        input_usd_per_mtok=0.15,
        cached_input_usd_per_mtok=0.03,
        output_usd_per_mtok=0.50,
        source_url=_ZAI_PRICING_URL,
    ),
    ModelPrice(
        key="glm-5.3",
        input_usd_per_mtok=1.40,
        cached_input_usd_per_mtok=0.26,
        output_usd_per_mtok=4.40,
        source_url=_ZAI_PRICING_URL,
    ),
    ModelPrice(
        key="gpt-5.6-terra",
        input_usd_per_mtok=2.00,
        cached_input_usd_per_mtok=0.20,
        output_usd_per_mtok=12.00,
        source_url=_OPENAI_PRICING_URL,
    ),
    ModelPrice(
        key="gpt-5.6-luna",
        input_usd_per_mtok=0.20,
        cached_input_usd_per_mtok=0.02,
        output_usd_per_mtok=1.20,
        source_url=_OPENAI_PRICING_URL,
    ),
    ModelPrice(
        key="gpt-4o-mini",
        input_usd_per_mtok=0.15,
        cached_input_usd_per_mtok=0.075,
        output_usd_per_mtok=0.60,
        source_url=_OPENAI_PRICING_URL,
    ),
    # Google Standard paid tier in force on the retrieval date. The same page
    # schedules higher rates from 2027-01-01; re-verify before refreshing.
    ModelPrice(
        key="gemini-3.7-flash",
        input_usd_per_mtok=0.75,
        cached_input_usd_per_mtok=0.075,
        output_usd_per_mtok=3.75,
        source_url=_GOOGLE_PRICING_URL,
    ),
)

_BY_KEY = {entry.key: entry for entry in PRICE_TABLE}

#: Provider prefixes stripped before the exact match. Explicit list only:
#: anything else keeps its prefix and will not match (no fuzzy guesses).
_PROVIDER_PREFIXES = (
    "google/",
    "gemini/",
    "openai/",
    "zai-coding-plan/",
    "zai/",
    "deepseek/",
    "anthropic/",
    "openrouter/",
)


def normalize_model_id(raw: str | None) -> str | None:
    """Normalize a trial model id to a table key candidate.

    Lowercase, strip whitespace, strip one known provider prefix
    (``zai-coding-plan/glm-5.3-flash`` -> ``glm-5.3-flash``). Tier or version
    suffixes (``gemini-3.7-flash-high``) are NOT stripped: they may denote a
    different underlying model, so they stay unmatched.
    """
    if raw is None:
        return None
    candidate = raw.strip().lower()
    if not candidate:
        return None
    for prefix in _PROVIDER_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    return candidate or None


def lookup_price(raw: str | None) -> ModelPrice | None:
    """Return the pinned price for a trial model id, or None when unknown."""
    key = normalize_model_id(raw)
    return _BY_KEY.get(key) if key else None


def estimate_cost_usd(
    input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    price: ModelPrice,
) -> float | None:
    """Price token counts at pinned rates. None when counts are missing.

    Requires total input and output counts; cached input defaults to 0 when
    unreported. Cached tokens are priced at the cached-input rate, the rest
    of the input at the full input rate. Inconsistent counts (negatives, or
    cached exceeding input) yield None rather than a fabricated figure.
    """
    if input_tokens is None or output_tokens is None:
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    cached = 0 if cached_input_tokens is None else cached_input_tokens
    if cached < 0 or cached > input_tokens:
        return None
    uncached = input_tokens - cached
    return (
        uncached * price.input_usd_per_mtok
        + cached * price.cached_input_usd_per_mtok
        + output_tokens * price.output_usd_per_mtok
    ) / 1_000_000
