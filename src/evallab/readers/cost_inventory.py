"""Offline cost-input inventory for admitted trace cohorts.

Answers one bounded engineering question: which stored actor inputs may be
priced at all, and what do the safe ones cost at one explicitly chosen rate
card? The reader performs no model calls, vendor lookups, scanner runs, label
reads or database writes.

Every stage reuses the production authority instead of a local variant:

* selection: :func:`evallab.trace_store.core.select_traces` (allowed datasets,
  attested own runs) over :func:`evallab.readers.clouddb_blind.dev_trace_refs`
  (exact ``DEV`` plus the shared cross-key admission gate);
* actor projection and hydration: :func:`evallab.trace_store.core.load_trace`,
  so stored observation previews are replaced by their exact CAS bytes and a
  preview is never counted as a truncated input;
* quarantine: :func:`evallab.trace_store.secrets.secret_hits` over the projected
  payload, which yields only the generic ``secret-pattern`` marker;
* rates: a caller-supplied versioned scenario document, or the repository's
  already pinned :mod:`evallab.interpretation.price_table` when the caller asks
  for it explicitly. This module re-declares no rate, tariff, retry count,
  output length or cache fraction.

The receipt exposes aggregates only: salted opaque run ids, bucket counts,
byte/token totals and content-free digests. Actor text is streamed one trace at
a time, hashed and dropped; no payload is printed or persisted. Source, split,
task and model identity never enter per-trace output; only dataset provenance
does, matching the store's own metadata boundary.

Pricing is component-wise. Anything the caller did not supply stays ``None``
with a stable ``missing`` reason, so a partial scenario can never produce a
full-corpus figure. ``full_corpus_total.usd`` is filled only when every selected
input is safe *and* every component is known; otherwise it is UNKNOWN with the
reasons attached.

Usage::

    python -m evallab.readers.cost_inventory --limit 50 \
        --scenario /tmp/cost-scenario.json --rates pinned \
        --model z-ai/glm-5.3-flash --tier standard \
        --out /tmp/cost-inventory.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from evallab.readers.clouddb_blind import (
    ActorHistoryUnavailable,
    TraceRef,
    blind_messages,
    dev_trace_refs,
    opaque_id,
)
from evallab.trace_store.core import HydrationError, TracePayload, load_trace, select_traces
from evallab.trace_store.secrets import secret_hits

SCHEMA = "evallab.trace_cost_inventory/v1"
QUOTE_SCHEMA = "evallab.trace_cost_quote/v1"
SCENARIO_SCHEMA = "evallab.cost_scenario/v1"

SAFE = "safe"
ACTOR_HISTORY_UNAVAILABLE = "actor_history_unavailable"
HYDRATION_UNAVAILABLE = "hydration_unavailable"
SECRET_VETO = "secret_veto"
BUCKETS = (SAFE, ACTOR_HISTORY_UNAVAILABLE, HYDRATION_UNAVAILABLE, SECRET_VETO)

DEFAULT_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENCODING = "o200k_base"
_ASSUMPTION_KEYS = (
    "generations",
    "retries_per_generation",
    "calls_per_generation",
    "cached_input_fraction",
    "output_tokens_per_call",
    "storage_usd_per_gb_month",
    "owner_plan",
)
_TOKENIZER_CLAIM = (
    "installed encoder counts the canonical payload text; this is a proxy for model input "
    "tokens, not exact provider wire, cached-prefix or billable tokens"
)
_BYTES_ONLY_CLAIM = (
    "no offline encoder is installed; exact UTF-8 payload bytes are reported and token "
    "counts/costs stay UNKNOWN (no bytes-to-token conversion is applied)"
)
_LIMITS = (
    "unavailable inputs are counted by reason and never priced",
    "hydration failures and unsupported actor record types stay UNKNOWN, not empty",
    "output length, generations, retries, storage tariffs and owner-plan coverage are "
    "caller-supplied or UNKNOWN",
    "byte and token totals cover the stored actor projection plus its metadata envelope",
)


class TokenCounter(Protocol):
    """An installed offline encoder: a name for the receipt and a count method."""

    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class _TiktokenCounter:
    name: str
    encode: Callable[[str], list[int]] = field(repr=False, compare=False)

    def count(self, text: str) -> int:
        return len(self.encode(text))


def load_tokenizer(spec: str = "auto") -> TokenCounter | None:
    """Resolve an already-installed encoder; never installs or downloads one.

    ``auto`` uses tiktoken's ``o200k_base`` when importable and silently falls
    back to bytes-only otherwise. ``tiktoken:<encoding>`` is an explicit opt-in
    that fails loudly when tiktoken or the encoding is absent. ``none`` forces
    bytes-only. A cold tiktoken cache is not populated here: if the encoder
    asset is missing the lookup fails and stays bytes-only.
    """
    if spec == "none":
        return None
    if spec == "auto":
        encoding_name = DEFAULT_ENCODING
        required = False
    elif spec.startswith("tiktoken:"):
        encoding_name = spec.partition(":")[2]
        if not encoding_name:
            raise ValueError("tokenizer spec must name a tiktoken encoding")
        required = True
    else:
        raise ValueError(f"unsupported tokenizer spec: {spec!r}")
    if importlib.util.find_spec("tiktoken") is None:
        if required:
            raise RuntimeError("tiktoken is not installed in this environment")
        return None
    import tiktoken  # ty: ignore[unresolved-import]

    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except (KeyError, OSError, ValueError) as exc:
        if required:
            raise ValueError(f"unknown or unavailable tiktoken encoding: {encoding_name}") from exc
        return None
    return _TiktokenCounter(f"tiktoken:{encoding_name}", encoding.encode)


@dataclass(frozen=True)
class RateCard:
    """One versioned list-price card; every rate is USD per million tokens."""

    model: str
    tier: str
    input_usd_per_mtok: float
    cached_input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    source: str
    retrieved_on: str


@dataclass(frozen=True)
class Scenario:
    """A caller-supplied versioned pricing scenario resolved to one rate card."""

    version: str
    currency: str
    model: str
    tier: str
    rate: RateCard
    generations: int | None
    retries_per_generation: int | None
    calls_per_generation: int | None
    cached_input_fraction: float | None
    output_tokens_per_call: int | None
    storage_usd_per_gb_month: float | None
    owner_plan: str | None
    sha256: str


def _check_keys(
    document: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str],
    label: str,
) -> None:
    keys = set(document)
    missing = sorted(set(required) - keys)
    unknown = sorted(keys - set(required) - set(optional))
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _integer(value: Any, label: str, *, minimum: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer or null")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _number(
    value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return float(value)


def _rate_card(document: Mapping[str, Any], *, model: str, tier: str) -> RateCard:
    label = f"rate card {model}/{tier}"
    _check_keys(
        document,
        ("input_usd_per_mtok", "cached_input_usd_per_mtok", "output_usd_per_mtok", "source",
         "retrieved_on"),
        (),
        label,
    )
    input_rate = _number(document["input_usd_per_mtok"], f"{label} input_usd_per_mtok",
                         minimum=0.0)
    if input_rate is None:
        raise ValueError(f"{label} input_usd_per_mtok is required")
    return RateCard(
        model=model,
        tier=tier,
        input_usd_per_mtok=input_rate,
        cached_input_usd_per_mtok=_number(
            document["cached_input_usd_per_mtok"], f"{label} cached_input_usd_per_mtok",
            minimum=0.0,
        ),
        output_usd_per_mtok=_number(
            document["output_usd_per_mtok"], f"{label} output_usd_per_mtok", minimum=0.0,
        ),
        source=_text(document["source"], f"{label} source"),
        retrieved_on=_text(document["retrieved_on"], f"{label} retrieved_on"),
    )


def pinned_rate(model: str, tier: str = "standard") -> RateCard:
    """Adapt the pinned repository list price; the table publishes one tier.

    Values come from :mod:`evallab.interpretation.price_table` verbatim, so the
    receipt's ``retrieved_on``/``source`` identify the same authority. An
    unlisted model or a non-standard tier is an error, never a fallback.
    """
    if tier != "standard":
        raise ValueError(
            "pinned price table publishes a single standard tier; "
            f"tier {tier!r} needs explicit rates",
        )
    from evallab.interpretation.price_table import lookup_price

    price = lookup_price(model)
    if price is None:
        raise ValueError(f"no pinned list price for model {model!r}; supply explicit rates")
    return RateCard(
        model=model,
        tier="standard",
        input_usd_per_mtok=price.input_usd_per_mtok,
        cached_input_usd_per_mtok=price.cached_input_usd_per_mtok,
        output_usd_per_mtok=price.output_usd_per_mtok,
        source=price.source_url,
        retrieved_on=price.retrieved_on,
    )


def load_scenario(
    path: Path, *, model: str, tier: str = "standard", pinned: bool = False,
) -> Scenario:
    """Validate a versioned scenario document and resolve one rate card.

    The document must declare ``schema``, ``version``, ``currency`` and every
    assumption key explicitly (``null`` means UNKNOWN, never a default). With a
    ``rates`` object the selected model/tier must exist; with ``pinned=True``
    and no ``rates`` object the pinned table supplies the card. Ambiguous input
    (both sources) and missing keys fail visibly.
    """
    raw = Path(path).read_bytes()
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid scenario JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("scenario must be a JSON object")
    _check_keys(
        document,
        ("schema", "version", "currency", "assumptions"),
        ("rates",),
        "scenario",
    )
    if document["schema"] != SCENARIO_SCHEMA:
        raise ValueError(f"unsupported scenario schema: {document['schema']!r}")
    version = _text(document["version"], "scenario version")
    currency = _text(document["currency"], "scenario currency").upper()
    assumptions = document["assumptions"]
    if not isinstance(assumptions, dict):
        raise ValueError("scenario assumptions must be an object")
    _check_keys(assumptions, _ASSUMPTION_KEYS, (), "scenario assumptions")
    generations = _integer(assumptions["generations"], "generations", minimum=1)
    retries = _integer(
        assumptions["retries_per_generation"], "retries_per_generation", minimum=0,
    )
    calls = _integer(assumptions["calls_per_generation"], "calls_per_generation", minimum=1)
    fraction = _number(
        assumptions["cached_input_fraction"], "cached_input_fraction", minimum=0.0, maximum=1.0,
    )
    output_tokens = _integer(
        assumptions["output_tokens_per_call"], "output_tokens_per_call", minimum=0,
    )
    storage = _number(
        assumptions["storage_usd_per_gb_month"], "storage_usd_per_gb_month", minimum=0.0,
    )
    owner_plan = assumptions["owner_plan"]
    if owner_plan is not None:
        owner_plan = _text(owner_plan, "owner_plan")

    rates = document.get("rates")
    if rates is not None:
        if pinned:
            raise ValueError("scenario defines explicit rates; --rates pinned is ambiguous")
        if not isinstance(rates, dict):
            raise ValueError("scenario rates must be an object")
        model_rates = rates.get(model)
        if not isinstance(model_rates, dict):
            raise ValueError(f"scenario has no rates for model {model!r}")
        if tier not in model_rates:
            raise ValueError(f"scenario has no {tier!r} tier for model {model!r}")
        card_document = model_rates[tier]
        if not isinstance(card_document, dict):
            raise ValueError(f"rate card {model}/{tier} must be an object")
        card = _rate_card(card_document, model=model, tier=tier)
    elif pinned:
        card = pinned_rate(model, tier)
    else:
        raise ValueError("scenario has no rates and --rates pinned was not requested")

    return Scenario(
        version=version,
        currency=currency,
        model=model,
        tier=tier,
        rate=card,
        generations=generations,
        retries_per_generation=retries,
        calls_per_generation=calls,
        cached_input_fraction=fraction,
        output_tokens_per_call=output_tokens,
        storage_usd_per_gb_month=storage,
        owner_plan=owner_plan,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@dataclass(frozen=True)
class TraceInput:
    """One scanned trace: counts and digest only, never actor text."""

    opaque_id: str
    dataset: str
    bucket: str
    reason: str | None
    messages: int
    rendered_bytes: int
    tokens: int | None
    sha256: str | None
    secret_markers: int = 0


def render_input(payload: TracePayload) -> str:
    """Canonical actor input text: metadata envelope plus projected messages.

    The encoding matches the store's own deterministic JSON convention
    (``ensure_ascii=False``, sorted keys, compact separators), so identical
    stored bytes always render identically.
    """
    return json.dumps(
        {"metadata": payload.metadata, "messages": payload.messages},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _actor_reason(con: sqlite3.Connection, trace_id: str) -> str:
    """Recover the precise actor-history reason that ``load_trace`` flattens."""
    try:
        blind_messages(con, trace_id)
    except ActorHistoryUnavailable as exc:
        return exc.reason
    return "actor_history_not_retained"


def trace_input(
    con: sqlite3.Connection,
    ref: TraceRef,
    *,
    cas_root: Path | None = None,
    tokenizer: TokenCounter | None = None,
) -> TraceInput:
    """Classify one selected trace into a bucket, keeping only safe aggregates."""
    try:
        payload = load_trace(con, ref, cas_root=cas_root)
    except HydrationError as exc:
        if exc.category == ACTOR_HISTORY_UNAVAILABLE:
            return TraceInput(
                opaque_id(ref.trace_id), ref.dataset_id, ACTOR_HISTORY_UNAVAILABLE,
                _actor_reason(con, ref.trace_id), 0, 0, None, None,
            )
        return TraceInput(
            opaque_id(ref.trace_id), ref.dataset_id, HYDRATION_UNAVAILABLE, exc.category,
            0, 0, None, None,
        )
    markers = secret_hits({"metadata": payload.metadata, "messages": payload.messages})
    if markers:
        return TraceInput(
            opaque_id(ref.trace_id), ref.dataset_id, SECRET_VETO, "secret_pattern",
            0, 0, None, None, len(markers),
        )
    text = render_input(payload)
    raw = text.encode("utf-8")
    return TraceInput(
        opaque_id=opaque_id(ref.trace_id),
        dataset=ref.dataset_id,
        bucket=SAFE,
        reason=None,
        messages=len(payload.messages),
        rendered_bytes=len(raw),
        tokens=None if tokenizer is None else tokenizer.count(text),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def scan_inputs(
    con: sqlite3.Connection,
    refs: Iterable[TraceRef],
    *,
    cas_root: Path | None = None,
    tokenizer: TokenCounter | None = None,
) -> Iterator[TraceInput]:
    """Yield one :class:`TraceInput` per ref; memory stays bounded per trace."""
    for ref in refs:
        yield trace_input(con, ref, cas_root=cas_root, tokenizer=tokenizer)


def _canonical(document: Any) -> str:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _priced(
    tokens: float | None, rate: float | None, *, token_reasons: Sequence[str], rate_reason: str,
) -> dict[str, Any]:
    """Exact arithmetic for one component, or explicit unknowns.

    Zero tokens is an exact zero regardless of the rate; a missing token total or
    rate is never treated as zero.
    """
    if tokens is None:
        return {
            "usd": None,
            "missing": list(token_reasons) + ([] if rate is not None else [rate_reason]),
        }
    if tokens == 0:
        return {"usd": 0.0, "missing": []}
    if rate is None:
        return {"usd": None, "missing": [rate_reason]}
    return {"usd": tokens * rate / 1_000_000, "missing": []}


def _scaled(
    component: Mapping[str, Any], scale: float | None, scale_missing: list[str],
) -> dict[str, Any]:
    if component["usd"] is None or scale is None:
        missing = sorted(set(component["missing"]) | set(scale_missing))
        return {"usd": None, "missing": missing}
    return {"usd": component["usd"] * scale, "missing": []}


def _combined(components: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    missing = sorted({
        reason for component in components.values() for reason in component["missing"]
    })
    if missing:
        return {"usd": None, "missing": missing}
    return {"usd": sum(float(component["usd"]) for component in components.values()),
            "missing": []}


def price_quote(
    *,
    traces: int,
    input_tokens: int | None,
    rendered_bytes: int,
    selected: int,
    buckets: Mapping[str, int],
    scenario: Scenario,
    admitted_not_selected: int = 0,
    outside_scope: int = 0,
) -> dict[str, Any]:
    """Price the safe cohort component-wise, keeping every gap explicit.

    ``*_per_generation`` components cover one pass over the safe inputs; totals
    apply ``generations * (1 + retries_per_generation)``. Output pricing needs an
    explicit calls/output-length assumption; storage needs an explicit tariff.
    ``full_corpus_total`` stays UNKNOWN whenever any selected input is unsafe,
    any admitted input was not selected, any database row is outside the exact
    DEV admission scope, or any component is missing; an excluded trace can
    never be priced as safe.
    """
    excluded = {
        bucket: count for bucket, count in buckets.items() if bucket != SAFE and count
    }
    unavailable = sum(excluded.values())
    fraction = scenario.cached_input_fraction

    if input_tokens is None:
        uncached = {"usd": None, "missing": ["input_tokens"]}
        cached = {"usd": None, "missing": ["input_tokens"]}
    elif fraction is None:
        uncached = {"usd": None, "missing": ["cached_input_fraction"]}
        cached = {"usd": None, "missing": ["cached_input_fraction"]}
    else:
        cached_tokens = input_tokens * fraction
        uncached = _priced(
            input_tokens - cached_tokens, scenario.rate.input_usd_per_mtok,
            token_reasons=("input_tokens",), rate_reason="input_rate",
        )
        cached = _priced(
            cached_tokens, scenario.rate.cached_input_usd_per_mtok,
            token_reasons=("input_tokens",), rate_reason="cached_input_rate",
        )

    output_missing = [
        reason for reason, value in (
            ("calls_per_generation", scenario.calls_per_generation),
            ("output_tokens_per_call", scenario.output_tokens_per_call),
        ) if value is None
    ]
    calls, per_call = scenario.calls_per_generation, scenario.output_tokens_per_call
    output_tokens = None if calls is None or per_call is None else calls * per_call * traces
    output = _priced(
        output_tokens, scenario.rate.output_usd_per_mtok,
        token_reasons=output_missing or ("output_tokens_per_call",),
        rate_reason="output_rate",
    )

    scale_missing = [
        reason for reason, value in (
            ("generations", scenario.generations),
            ("retries_per_generation", scenario.retries_per_generation),
        ) if value is None
    ]
    generations, retries = scenario.generations, scenario.retries_per_generation
    scale = None if generations is None or retries is None else generations * (1 + retries)

    storage = {
        "usd": None if scenario.storage_usd_per_gb_month is None else (
            rendered_bytes / 1_000_000_000 * scenario.storage_usd_per_gb_month
        ),
        "missing": [] if scenario.storage_usd_per_gb_month is not None else [
            "storage_usd_per_gb_month",
        ],
    }

    per_generation = {"input_uncached": uncached, "input_cached": cached, "output": output}
    totals = {
        name: _scaled(component, scale, scale_missing)
        for name, component in per_generation.items()
    }
    safe_total = _combined({**totals, "storage_month_1": storage})

    corpus_missing = list(safe_total["missing"])
    if unavailable:
        corpus_missing.append("unavailable_inputs")
    if admitted_not_selected:
        corpus_missing.append("admitted_not_selected")
    if outside_scope:
        corpus_missing.append("outside_scope")
    full_corpus = {
        "usd": safe_total["usd"] if not corpus_missing else None,
        "missing": sorted(set(corpus_missing)),
    }

    quote = {
        "schema": QUOTE_SCHEMA,
        "scenario": {
            "version": scenario.version,
            "currency": scenario.currency,
            "model": scenario.model,
            "tier": scenario.tier,
            "rates_source": scenario.rate.source,
            "rates_retrieved_on": scenario.rate.retrieved_on,
            "scenario_sha256": scenario.sha256,
            "assumptions": {
                "generations": scenario.generations,
                "retries_per_generation": scenario.retries_per_generation,
                "calls_per_generation": scenario.calls_per_generation,
                "cached_input_fraction": scenario.cached_input_fraction,
                "output_tokens_per_call": scenario.output_tokens_per_call,
                "storage_usd_per_gb_month": scenario.storage_usd_per_gb_month,
                "owner_plan": scenario.owner_plan,
            },
        },
        "basis": {
            "selected_traces": selected,
            "safe_traces": traces,
            "input_tokens": input_tokens,
            "rendered_bytes": rendered_bytes,
            "unavailable_traces": unavailable,
            "excluded": excluded,
            "admitted_not_selected": admitted_not_selected,
            "outside_scope": outside_scope,
        },
        "components": {
            "input_uncached_per_generation": uncached,
            "input_cached_per_generation": cached,
            "output_per_generation": output,
            **{f"{name}_total": component for name, component in totals.items()},
            "storage_month_1": storage,
            "safe_cohort_total": safe_total,
            "full_corpus_total": full_corpus,
        },
        "applicability": {
            "owner_plan": scenario.owner_plan,
            "owner_plan_status": (
                "declared" if scenario.owner_plan is not None else "unknown"
            ),
            "claim": (
                "whether an owner plan, subscription seat or BYOK credit covers these inputs "
                "is not derivable from the trace database"
                if scenario.owner_plan is None else
                "caller-declared owner plan; coverage is not verified by this reader"
            ),
        },
    }
    return {**quote, "quote_sha256": hashlib.sha256(_canonical(quote).encode()).hexdigest()}


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _source_identity_sha256(con: sqlite3.Connection) -> str:
    """Fingerprint of the stored identities, in stable order, without content."""
    digest = hashlib.sha256()
    for row in con.execute(
        "SELECT a.trace_id, a.task_id, t.dataset_id, t.split, a.model_id "
        "FROM agent_traces a LEFT JOIN corpus_tasks t ON t.task_id = a.task_id "
        "ORDER BY a.trace_id",
    ):
        digest.update(
            "\t".join("" if value is None else str(value) for value in row).encode("utf-8")
            + b"\n",
        )
    return digest.hexdigest()


def _file_sha256(path: Path, *, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


def inventory(
    con: sqlite3.Connection,
    *,
    repo_root: Path,
    dataset: str | None = None,
    limit: int | None = None,
    evidence_path: Path | None = None,
    cas_root: Path | None = None,
    tokenizer: TokenCounter | None = None,
    scenario: Scenario | None = None,
    include_traces: bool = False,
    db_path: Path | None = None,
    hash_db: bool = False,
) -> dict[str, Any]:
    """Scan the allowed cohort once and return a content-free receipt.

    ``limit`` bounds the per-trace scan to the first N deterministic selections
    (own runs first, then sorted trace id, as the store selects). Selection and
    admission still read the whole identity surface, and cohort counts always
    describe the full selection so a bounded run reconciles.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    started = time.monotonic()
    db_total = _count(con, "agent_traces")
    step_total = _count(con, "trace_steps")
    task_total = _count(con, "corpus_tasks")

    admitted = dev_trace_refs(con, repo_root=Path(repo_root))
    scoped_admitted = [ref for ref in admitted if dataset is None or ref.dataset_id == dataset]
    selected = select_traces(
        con, repo_root=Path(repo_root), dataset=dataset, evidence_path=evidence_path,
    )
    if limit is not None:
        selected = selected[:limit]
    selected_ids = {ref.trace_id for ref in selected}
    admitted_not_selected = [
        ref for ref in scoped_admitted if ref.trace_id not in selected_ids
    ]

    projection = hashlib.sha256()
    buckets: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    markers = 0
    safe_messages = 0
    safe_bytes = 0
    safe_tokens: int | None = 0 if tokenizer is not None else None
    traces: list[dict[str, Any]] = []
    for item in scan_inputs(con, selected, cas_root=cas_root, tokenizer=tokenizer):
        buckets[item.bucket] += 1
        if item.reason is not None:
            reasons[f"{item.bucket}:{item.reason}"] += 1
        markers += item.secret_markers
        projection.update(_canonical([
            item.opaque_id, item.bucket, item.reason or "", item.messages,
            item.rendered_bytes, item.tokens, item.sha256 or "",
        ]).encode("utf-8") + b"\n")
        if item.bucket == SAFE:
            safe_messages += item.messages
            safe_bytes += item.rendered_bytes
            if safe_tokens is not None and item.tokens is not None:
                safe_tokens += item.tokens
            elif item.tokens is None:
                safe_tokens = None
        if include_traces:
            traces.append({
                "opaque_id": item.opaque_id,
                "dataset": item.dataset,
                "bucket": item.bucket,
                "reason": item.reason,
                "messages": item.messages,
                "rendered_bytes": item.rendered_bytes,
                "tokens": item.tokens,
                "sha256": item.sha256,
            })

    quote = None
    if scenario is not None:
        quote = price_quote(
            traces=buckets[SAFE],
            input_tokens=safe_tokens,
            rendered_bytes=safe_bytes,
            selected=len(selected),
            buckets=buckets,
            scenario=scenario,
            admitted_not_selected=len(admitted_not_selected),
            outside_scope=db_total - len(scoped_admitted),
        )

    identity_sha256 = _source_identity_sha256(con)
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - started, 3),
        "cohorts": {
            "basis": f"dataset:{dataset}" if dataset else "all_allowed",
            "db_total": db_total,
            "dev_admitted": len(scoped_admitted),
            "dev_admitted_all_datasets": len(admitted),
            "outside_dev_admission": db_total - len(admitted),
            "selected": len(selected),
            "admitted_not_selected": len(admitted_not_selected),
            "selected_by_dataset": dict(sorted(Counter(
                ref.dataset_id for ref in selected
            ).items())),
        },
        "buckets": {bucket: buckets[bucket] for bucket in BUCKETS},
        "quarantine": {
            "actor_history": {
                "traces": buckets[ACTOR_HISTORY_UNAVAILABLE],
                "reasons": dict(sorted(
                    (key.partition(":")[2], value) for key, value in reasons.items()
                    if key.startswith(ACTOR_HISTORY_UNAVAILABLE + ":")
                )),
            },
            "hydration": {
                "traces": buckets[HYDRATION_UNAVAILABLE],
                "reasons": dict(sorted(
                    (key.partition(":")[2], value) for key, value in reasons.items()
                    if key.startswith(HYDRATION_UNAVAILABLE + ":")
                )),
            },
            "secret_veto": {
                "traces": buckets[SECRET_VETO],
                "markers": markers,
            },
        },
        "safe_inputs": {
            "traces": buckets[SAFE],
            "messages": safe_messages,
            "rendered_bytes": safe_bytes,
            "tokens": safe_tokens,
        },
        "tokenization": {
            "encoding": tokenizer.name if tokenizer is not None else None,
            "installed": tokenizer is not None,
            "claim": _TOKENIZER_CLAIM if tokenizer is not None else _BYTES_ONLY_CLAIM,
        },
        "source": {
            "db_name": Path(db_path).name if db_path is not None else None,
            "db_bytes": Path(db_path).stat().st_size if db_path is not None else None,
            "db_sha256": _file_sha256(Path(db_path)) if hash_db and db_path is not None else None,
            "identity_sha256": identity_sha256,
            "agent_traces": db_total,
            "corpus_tasks": task_total,
            "trace_steps": step_total,
        },
        "hashes": {
            "source": identity_sha256,
            "projection": projection.hexdigest(),
            "quote": None if quote is None else quote["quote_sha256"],
        },
        "quote": quote,
        "traces": traces if include_traces else None,
        "labels_read": False,
        "model_calls": 0,
        "network_calls": 0,
        "limits": list(_LIMITS),
    }
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--cas-root", type=Path)
    parser.add_argument("--dataset")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--tokenizer", default="auto",
        help="auto (installed o200k_base or bytes-only), none, or tiktoken:<encoding>",
    )
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--tier", default="standard")
    parser.add_argument("--rates", choices=("scenario", "pinned"), default="scenario")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--per-trace", action="store_true")
    parser.add_argument("--hash-db", action="store_true")
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.scenario is None:
        if args.model is not None or args.rates == "pinned" or args.tier != "standard":
            parser.error("--model/--tier/--rates require --scenario")
        scenario = None
    else:
        if args.model is None:
            parser.error("--model is required with --scenario")
        scenario = load_scenario(
            args.scenario, model=args.model, tier=args.tier, pinned=args.rates == "pinned",
        )
    tokenizer = load_tokenizer(args.tokenizer)

    db_uri = args.db.expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(db_uri, uri=True)) as con:
        receipt = inventory(
            con,
            repo_root=args.repo_root,
            dataset=args.dataset,
            limit=args.limit,
            evidence_path=args.evidence,
            cas_root=args.cas_root,
            tokenizer=tokenizer,
            scenario=scenario,
            include_traces=args.per_trace,
            db_path=args.db,
            hash_db=args.hash_db,
        )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    summary: dict[str, Any] = {
        key: receipt[key] for key in (
            "cohorts", "buckets", "quarantine", "safe_inputs", "tokenization", "hashes",
        )
    }
    if receipt["quote"] is not None:
        summary["quote"] = receipt["quote"]["components"]
    print(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
