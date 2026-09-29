"""Proxy ledger semantics: used vs attempted usage, and per-run cost.

The metered secret proxy (``containers/zai_openapi_secret_proxy.py``) writes
one ledger per job. Each provider call passes through states::

    reserved -> reconciled | unresolved | exceeded

A reservation is a conservative upper bound taken *before* the upstream call;
reconciliation replaces it with the provider-reported usage. A call that never
settles (transport error, redirect, unreadable body, SIGTERM past the drain
deadline) stays ``reserved``/``unresolved``: its usage is unknown.

Ledger schema v2 (current) therefore reports two blocks:

- ``totals``: actual usage only — calls in ``reconciled`` or ``exceeded``
  state, summed over their settled ``input_tokens``/``output_tokens``/
  ``cost_micros``. ``totals.requests`` counts those calls.
- ``attempted``: reservations of calls still ``reserved``/``unresolved``,
  summed over ``reserved_input_tokens``/``reserved_output_tokens``/
  ``reserved_cost_micros``. Never-sent tokens never count as used.
- ``unresolved_requests`` stays a bare count of calls whose state is not
  ``reconciled`` (reserved, unresolved, *and* exceeded: an over-reservation
  call settled its accounting but still fails the trial).

Schema v1 ledgers (pre-HAR-104) report reservation-inclusive ``totals`` and
have no ``attempted`` block. They are never rewritten; readers derive used
vs attempted from the ``calls`` list via :func:`split_usage`.

Budget enforcement inside the proxy still counts in-flight reservations
against the trial ceilings (concurrent calls must not overshoot) — that is
internal proxy state, not reported totals.

Cost rule (HAR-104): per-run ``cost_usd`` is ledger usage times the ledger's
pinned pricing (``pricing.input/output_cost_micros_per_million``;
``totals.cost_micros`` is already that product per call, rounded up per call
by the proxy's ``_cost_micros``). The ledger-derived figure wins over
Harbor/litellm's own ``agent_result.cost_usd`` estimate for proxy-metered
trials. ``attempted_cost_usd`` is recorded separately so the policy gate can
be conservative: the gate spends ``used + attempted`` as a ceiling while the
catalog ``cost_usd`` is used only. A (0, 0)-priced route (self-hosted MiMo,
billed by server time) never reports $0 as spend: its ``cost_usd`` is None
with a reason. A missing ledger is None with a reason, never 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Ledger schema written by the proxy. v1 ledgers remain on disk for historic
#: runs and are derived (never rewritten) by :func:`split_usage`.
LEDGER_SCHEMA_VERSION = 2
LEGACY_LEDGER_SCHEMA_VERSION = 1

#: Calls whose settled actuals count toward ``totals``.
USED_STATES = frozenset({"reconciled", "exceeded"})
#: Calls whose reservations count toward ``attempted`` (usage unknown).
ATTEMPTED_STATES = frozenset({"reserved", "unresolved"})

MICROS_PER_USD = 1_000_000


def _integer(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 2**63 - 1
    ):
        raise ValueError(f"invalid ledger field: {label}")
    return value


def split_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute used/attempted/unresolved/sequence purely from a calls list.

    Raises ValueError on any malformed call. Shared by the v2 validator and
    the v1 derivation path so both versions agree on what the calls say.
    """
    used = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_micros": 0}
    attempted = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_micros": 0}
    unresolved = 0
    sequence = 0
    for index, raw_call in enumerate(calls, start=1):
        if not isinstance(raw_call, dict) or _integer(
            raw_call.get("call_id"), "call_id"
        ) != index:
            raise ValueError("call sequence is invalid")
        state = raw_call.get("state")
        if state in USED_STATES:
            used["requests"] += 1
            used["input_tokens"] += _integer(raw_call.get("input_tokens"), "input_tokens")
            used["output_tokens"] += _integer(
                raw_call.get("output_tokens"), "output_tokens"
            )
            used["cost_micros"] += _integer(raw_call.get("cost_micros"), "cost_micros")
            sequence += 2
            if state != "reconciled":
                unresolved += 1
        elif state in ATTEMPTED_STATES:
            attempted["requests"] += 1
            attempted["input_tokens"] += _integer(
                raw_call.get("reserved_input_tokens"), "reserved_input_tokens"
            )
            attempted["output_tokens"] += _integer(
                raw_call.get("reserved_output_tokens"), "reserved_output_tokens"
            )
            attempted["cost_micros"] += _integer(
                raw_call.get("reserved_cost_micros"), "reserved_cost_micros"
            )
            unresolved += 1
            sequence += 1 if state == "reserved" else 2
        else:
            raise ValueError(f"invalid call state: {state!r}")
    used["total_tokens"] = used["input_tokens"] + used["output_tokens"]
    return {
        "used": used,
        "attempted": attempted,
        "unresolved_requests": unresolved,
        "sequence": sequence,
    }


def split_usage(provider_usage: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{used, attempted, unresolved_requests, pricing}`` for a ledger.

    v2 ledgers are read from their ``totals``/``attempted`` blocks; v1
    ledgers are derived from the ``calls`` list. Raises ValueError when the
    ledger is not a readable v1/v2 mapping.
    """
    if not isinstance(provider_usage, Mapping):
        raise ValueError("provider usage is not a mapping")
    version = provider_usage.get("schema_version")
    calls = provider_usage.get("calls")
    if not isinstance(calls, list):
        raise ValueError("provider usage calls are invalid")
    typed_calls = [call for call in calls]
    if version == LEDGER_SCHEMA_VERSION:
        totals = provider_usage.get("totals")
        attempted_block = provider_usage.get("attempted")
        if not isinstance(totals, dict) or not isinstance(attempted_block, dict):
            raise ValueError("v2 ledger totals/attempted blocks are invalid")
        recomputed = split_calls(typed_calls)
        for name, value in recomputed["used"].items():
            if _integer(totals.get(name), f"totals.{name}") != value:
                raise ValueError("v2 ledger totals do not match provider calls")
        for name in ("requests", "input_tokens", "output_tokens", "cost_micros"):
            if _integer(attempted_block.get(name), f"attempted.{name}") != recomputed[
                "attempted"
            ][name]:
                raise ValueError("v2 ledger attempted block does not match calls")
        if _integer(
            provider_usage.get("unresolved_requests"), "unresolved_requests"
        ) != recomputed["unresolved_requests"]:
            raise ValueError("v2 ledger unresolved count does not match calls")
        used = dict(recomputed["used"])
        attempted = dict(recomputed["attempted"])
    elif version == LEGACY_LEDGER_SCHEMA_VERSION:
        recomputed = split_calls(typed_calls)
        used = dict(recomputed["used"])
        attempted = dict(recomputed["attempted"])
    else:
        raise ValueError(f"unsupported ledger schema version: {version!r}")
    pricing = provider_usage.get("pricing")
    if pricing is not None and not isinstance(pricing, dict):
        raise ValueError("ledger pricing is invalid")
    return {
        "schema_version": version,
        "used": used,
        "attempted": attempted,
        "unresolved_requests": recomputed["unresolved_requests"],
        "pricing": dict(pricing) if isinstance(pricing, dict) else None,
    }


def _is_zero_priced(pricing: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(pricing, Mapping)
        and pricing.get("input_cost_micros_per_million") == 0
        and pricing.get("output_cost_micros_per_million") == 0
    )


def build_cost_block(
    provider_usage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the per-run ``cost`` block for lab-metadata from a ledger.

    Returns ``{cost_usd, attempted_cost_usd, source, pricing, reason}``.
    ``cost_usd`` is used-tokens times pinned pricing; ``attempted_cost_usd``
    is the unresolved-reservation ceiling. Either is None (with ``reason``)
    when the ledger cannot establish it — never a silent 0, except a genuine
    measured zero on a priced route with settled calls.
    """
    if not isinstance(provider_usage, Mapping):
        return {
            "cost_usd": None,
            "attempted_cost_usd": None,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": None,
            "reason": "proxy ledger missing: no usage was recorded for this run",
        }
    try:
        split = split_usage(provider_usage)
    except ValueError as exc:
        return {
            "cost_usd": None,
            "attempted_cost_usd": None,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": None,
            "reason": f"proxy ledger unreadable: {exc}",
        }
    pricing = split["pricing"]
    attempted_cost_usd: float | None = (
        split["attempted"]["cost_micros"] / MICROS_PER_USD
    )
    if split["used"]["requests"] == 0 and split["attempted"]["requests"] == 0:
        return {
            "cost_usd": None,
            "attempted_cost_usd": attempted_cost_usd,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": pricing,
            "reason": "zero calls: no rate applies without a provider call",
        }
    if pricing is None:
        return {
            "cost_usd": None,
            "attempted_cost_usd": attempted_cost_usd,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": None,
            "reason": "ledger carries no pinned pricing: no rate may be invented",
        }
    if _is_zero_priced(pricing):
        return {
            "cost_usd": None,
            "attempted_cost_usd": attempted_cost_usd,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": pricing,
            "reason": (
                "self-hosted route bills by server time, not tokens: "
                "use execution_contracts.mimo_selfhosted_trial_cost_usd with "
                "trial wall time, concurrency, and sandbox cost; the sandbox "
                "input is unavailable at finalize so no spend is reported"
            ),
        }
    return {
        "cost_usd": split["used"]["cost_micros"] / MICROS_PER_USD,
        "attempted_cost_usd": attempted_cost_usd,
        "source": "proxy_ledger_x_pinned_price",
        "pricing": pricing,
        "reason": None
        if split["used"]["requests"]
        else "no settled calls: used cost covers zero reconciled calls",
    }
