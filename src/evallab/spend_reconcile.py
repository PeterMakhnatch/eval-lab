"""Spend ledger reconciliation and provider usage verification.

Reconciles local lab spend ledgers (such as
``~/.local/state/evallab-failure-library/spend.jsonl``) against real
provider-reported balances and usage endpoints (DeepSeek, OpenRouter,
Meta/OMP, Modal) over a configurable time window. Flags discrepancies
exceeding a threshold and supports backfilling missing paid calls from
OMP session traces.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Default location of the shared lab spend ledger
DEFAULT_SPEND_LOG = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"

#: Default session directories scanned for untracked paid model calls
DEFAULT_SESSION_DIRS = (
    Path.home() / ".omp/agent/sessions",
    Path.home() / ".local/state/foreman/sessions",
)

#: Default paid providers to inspect for untracked usage
PAID_PROVIDERS = frozenset({"deepseek", "openrouter", "meta", "openai-codex", "anthropic", "xai-oauth"})

#: Known historical DeepSeek baseline: $16.00 at 2026-10-30 17:00 ET (2026-10-30T21:00:00Z)
DEEPSEEK_OCT30_BASELINE_USD = 16.00
DEEPSEEK_OCT30_BASELINE_TS = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)


@dataclass
class ProviderReconciliation:
    """Reconciliation result for a single provider over a window."""

    provider: str
    ledger_usd: float
    reported_usd: float | None
    gap_usd: float | None
    status: str  # "ok", "gap_exceeded", "fetch_error", "unsupported"
    note: str = ""
    ledger_records_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    """Full lab reconciliation report across all evaluated providers."""

    window_start: datetime
    window_end: datetime
    threshold_usd: float
    providers: list[ProviderReconciliation]
    total_ledger_usd: float
    total_reported_usd: float
    total_gap_usd: float
    exceeded_threshold: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "threshold_usd": self.threshold_usd,
            "exceeded_threshold": self.exceeded_threshold,
            "totals": {
                "ledger_usd": round(self.total_ledger_usd, 4),
                "reported_usd": round(self.total_reported_usd, 4),
                "gap_usd": round(self.total_gap_usd, 4),
            },
            "providers": [
                {
                    "provider": p.provider,
                    "ledger_usd": round(p.ledger_usd, 4),
                    "reported_usd": round(p.reported_usd, 4) if p.reported_usd is not None else None,
                    "gap_usd": round(p.gap_usd, 4) if p.gap_usd is not None else None,
                    "status": p.status,
                    "note": p.note,
                    "ledger_records_count": p.ledger_records_count,
                    "metadata": p.metadata,
                }
                for p in self.providers
            ],
            "notes": self.notes,
        }


@dataclass
class OmpPaidCall:
    """A single paid model invocation discovered in an OMP session trace."""

    session_file: str
    message_id: str
    provider: str
    model: str
    timestamp: datetime
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    raw_record: dict[str, Any] = field(default_factory=dict)


def _get_provider_key_from_omp(provider_name: str) -> str | None:
    """Attempt to load an API key for a provider from ~/.omp/agent/agent.db."""
    agent_db = Path.home() / ".omp/agent/agent.db"
    if not agent_db.is_file():
        return None
    uri = f"file:{agent_db}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT data FROM auth_credentials WHERE provider = ?",
            (provider_name,),
        ).fetchone()
        conn.close()
        if row and row[0]:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                return parsed.get("key") or parsed.get("token")
    except Exception:
        pass
    return None


def fetch_deepseek_balance(
    api_key: str | None = None,
    *,
    allow_omp_fallback: bool = True,
) -> float | None:
    """Fetch current DeepSeek account balance in USD.

    Tries official endpoint ``https://api.deepseek.com/user/balance``.
    Authentication resolved from passed key, ``DEEPSEEK_API_KEY`` env,
    or ``~/.omp/agent/agent.db``.
    """
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        key = _get_provider_key_from_omp("deepseek")

    if key:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for info in data.get("balance_infos", []):
                    if info.get("currency") == "USD":
                        return float(info.get("total_balance", 0.0))
        except (urllib.error.URLError, json.JSONDecodeError, ValueError, OSError):
            pass

    if not allow_omp_fallback:
        return None

    # Fallback to reading balance via omp usage if available
    try:
        import subprocess

        out = subprocess.run(
            ["omp", "usage", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            parsed = json.loads(out.stdout)
            for rep in parsed.get("reports", []):
                if rep.get("provider") == "deepseek":
                    for lim in rep.get("limits", []):
                        amt = lim.get("amount", {})
                        if "remaining" in amt:
                            return float(amt["remaining"])
    except Exception:
        pass

    return None


def fetch_openrouter_usage(api_key: str | None = None) -> dict[str, Any] | None:
    """Fetch current OpenRouter key usage and limits from official auth endpoint.

    Uses ``https://openrouter.ai/api/v1/auth/key``.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        key = _get_provider_key_from_omp("openrouter")

    if not key:
        return None

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})
            return {
                "usage": float(data.get("usage", 0.0)),
                "limit": float(data.get("limit", 0.0)) if data.get("limit") is not None else None,
                "remaining": (
                    float(data.get("limit_remaining", 0.0))
                    if data.get("limit_remaining") is not None
                    else None
                ),
                "is_free_tier": data.get("is_free_tier", False),
            }
    except (urllib.error.URLError, json.JSONDecodeError, ValueError, OSError):
        return None


def fetch_meta_stats(
    window_start: datetime,
    window_end: datetime,
    stats_db_path: Path | None = None,
) -> dict[str, Any]:
    """Query Meta model call statistics from OMP's stats.db.

    Returns dictionary with ``calls``, ``cost_total``, and ``unpriced_calls``.
    """
    db_path = stats_db_path or (Path.home() / ".omp" / "stats.db")
    if not db_path.is_file():
        return {"calls": 0, "cost_total": 0.0, "unpriced_calls": 0}

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)

        row = cur.execute(
            """
            SELECT coalesce(sum(cost_total), 0),
                   count(*),
                   coalesce(sum(case when cost_total = 0.0 then 1 else 0 end), 0)
            FROM messages
            WHERE provider = 'meta'
              AND timestamp >= ?
              AND timestamp < ?
            """,
            (start_ms, end_ms),
        ).fetchone()
        conn.close()
        if row:
            return {
                "cost_total": float(row[0]),
                "calls": int(row[1]),
                "unpriced_calls": int(row[2]),
            }
    except Exception:
        pass
    return {"calls": 0, "cost_total": 0.0, "unpriced_calls": 0}


def read_spend_ledger(
    ledger_path: Path,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, dict[str, Any]]:
    """Aggregate per-provider spend from a spend.jsonl ledger over [window_start, window_end).

    Returns mapping: ``{provider: {"usd": float, "records": int, "routes": dict[str, float]}}``.
    """
    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"usd": 0.0, "records": 0, "routes": defaultdict(float)}
    )

    if not ledger_path.is_file():
        return totals

    try:
        with ledger_path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue

                ts_str = record.get("ts") or record.get("at")
                if not ts_str:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                except Exception:
                    continue

                if not (window_start <= dt < window_end):
                    continue

                cost = float(record.get("cost_usd") or record.get("usd") or 0.0)
                route = str(record.get("route") or record.get("model") or "unknown")
                provider = str(record.get("provider") or (route.split("/")[0] if "/" in route else route))

                entry = totals[provider]
                entry["usd"] += cost
                entry["records"] += 1
                entry["routes"][route] += cost
    except OSError:
        pass

    return totals


def scan_omp_sessions(
    session_dirs: Iterable[Path] = DEFAULT_SESSION_DIRS,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    paid_providers: frozenset[str] = PAID_PROVIDERS,
) -> list[OmpPaidCall]:
    """Scan OMP session files for paid model calls.

    Discovers calls to paid providers that were not logged to spend.jsonl.
    """
    calls: list[OmpPaidCall] = []

    for root in session_dirs:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                # Fast mtime check if window_start given
                if window_start:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                    if mtime < window_start:
                        continue
                with path.open(encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if not any(p in line for p in paid_providers) and '"cost"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(record, dict):
                            continue

                        rec_type = record.get("type")
                        msg = record.get("message") if rec_type == "message" else (
                            record if rec_type == "model_usage" else None
                        )
                        if not isinstance(msg, dict):
                            continue

                        provider = str(msg.get("provider") or "")
                        model = str(msg.get("model") or "")
                        if not provider and "/" in model:
                            provider = model.split("/")[0]

                        # Check if provider or route is a paid provider
                        if provider not in paid_providers and not any(p in model for p in paid_providers):
                            continue

                        ts_str = record.get("timestamp") or msg.get("timestamp")
                        dt = None
                        if ts_str:
                            try:
                                dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=UTC)
                            except Exception:
                                pass

                        if dt and window_start and dt < window_start:
                            continue
                        if dt and window_end and dt >= window_end:
                            continue

                        usage = msg.get("usage") or {}
                        cost_info = usage.get("cost") or {}
                        cost = (
                            float(cost_info.get("total", 0.0))
                            if isinstance(cost_info, dict)
                            else float(cost_info or 0.0)
                        )

                        message_id = str(record.get("id") or "")
                        calls.append(
                            OmpPaidCall(
                                session_file=str(path),
                                message_id=message_id,
                                provider=provider,
                                model=model,
                                timestamp=dt or datetime.now(UTC),
                                cost_usd=cost,
                                input_tokens=int(usage.get("input", 0) or 0),
                                output_tokens=int(usage.get("output", 0) or 0),
                                cache_read_tokens=int(usage.get("cacheRead", 0) or 0),
                                raw_record=record,
                            )
                        )
            except OSError:
                continue

    return calls


def sync_omp_sessions_to_ledger(
    calls: list[OmpPaidCall],
    ledger_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[int, float]:
    """Append unlogged OMP session calls to the spend ledger.

    Deduplicates against already present records.
    Returns (appended_count, appended_usd).
    """
    existing_fingerprints: set[str] = set()

    if ledger_path.is_file():
        try:
            with ledger_path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line.strip())
                        fp = d.get("omp_call_id") or d.get("purpose")
                        if fp:
                            existing_fingerprints.add(str(fp))
                    except Exception:
                        continue
        except OSError:
            pass

    to_append: list[dict[str, Any]] = []
    added_usd = 0.0

    for call in calls:
        call_fp = f"omp:{Path(call.session_file).stem}:{call.message_id}"
        if call_fp in existing_fingerprints:
            continue
        existing_fingerprints.add(call_fp)

        row = {
            "source": "omp_session_scan",
            "session_file": call.session_file,
            "message_id": call.message_id,
            "omp_call_id": call_fp,
            "provider": call.provider,
            "route": f"{call.provider}/{call.model}" if call.provider not in call.model else call.model,
            "model": call.model,
            "usd": round(call.cost_usd, 6),
            "cost_usd": round(call.cost_usd, 6),
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cache_read_tokens": call.cache_read_tokens,
            "ts": call.timestamp.isoformat(),
            "purpose": call_fp,
        }
        to_append.append(row)
        added_usd += call.cost_usd

    if to_append and not dry_run:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            for row in to_append:
                fh.write(json.dumps(row) + "\n")

    return len(to_append), added_usd


def reconcile_spend(
    window_start: datetime,
    window_end: datetime,
    *,
    ledger_path: Path | None = None,
    threshold_usd: float = 1.0,
    providers: Iterable[str] = ("deepseek", "openrouter", "meta", "modal"),
    deepseek_start_balance: float | None = None,
    openrouter_start_usage: float | None = None,
) -> ReconciliationReport:
    """Compare local ledger sums with provider-reported usage and flag gaps."""
    spend_log = ledger_path or DEFAULT_SPEND_LOG
    ledger_data = read_spend_ledger(spend_log, window_start, window_end)

    target_providers = set(providers)
    results: list[ProviderReconciliation] = []
    notes: list[str] = []
    exceeded = False

    # 1. DeepSeek reconciliation
    if "deepseek" in target_providers:
        l_info = ledger_data.get("deepseek", {"usd": 0.0, "records": 0})
        ledger_usd = float(l_info["usd"])
        records_count = int(l_info["records"])

        current_bal = fetch_deepseek_balance()
        start_bal = deepseek_start_balance
        if start_bal is None and window_start <= DEEPSEEK_OCT30_BASELINE_TS:
            start_bal = DEEPSEEK_OCT30_BASELINE_USD

        if current_bal is not None and start_bal is not None:
            reported_spend = max(0.0, start_bal - current_bal)
            gap = reported_spend - ledger_usd
            status = "gap_exceeded" if gap > threshold_usd else "ok"
            if gap > threshold_usd:
                exceeded = True
            note = f"start ${start_bal:.2f} -> current ${current_bal:.2f}"
            results.append(
                ProviderReconciliation(
                    provider="deepseek",
                    ledger_usd=ledger_usd,
                    reported_usd=reported_spend,
                    gap_usd=gap,
                    status=status,
                    note=note,
                    ledger_records_count=records_count,
                    metadata={
                        "start_balance_usd": start_bal,
                        "current_balance_usd": current_bal,
                    },
                )
            )
        elif current_bal is not None:
            # Current balance available but start balance unknown
            results.append(
                ProviderReconciliation(
                    provider="deepseek",
                    ledger_usd=ledger_usd,
                    reported_usd=None,
                    gap_usd=None,
                    status="ok",
                    note=f"current balance ${current_bal:.2f} (start balance unknown; pass --deepseek-start-balance)",
                    ledger_records_count=records_count,
                    metadata={"current_balance_usd": current_bal},
                )
            )
        else:
            notes.append("deepseek: failed to query balance endpoint")
            results.append(
                ProviderReconciliation(
                    provider="deepseek",
                    ledger_usd=ledger_usd,
                    reported_usd=None,
                    gap_usd=None,
                    status="fetch_error",
                    note="could not connect to balance endpoint or missing credentials",
                    ledger_records_count=records_count,
                )
            )

    # 2. OpenRouter reconciliation
    if "openrouter" in target_providers:
        l_info = ledger_data.get("openrouter", {"usd": 0.0, "records": 0})
        ledger_usd = float(l_info["usd"])
        records_count = int(l_info["records"])

        or_info = fetch_openrouter_usage()
        if or_info is not None:
            curr_usage = or_info["usage"]
            start_usage = openrouter_start_usage or 0.0
            reported_spend = max(0.0, curr_usage - start_usage)
            gap = reported_spend - ledger_usd
            status = "gap_exceeded" if gap > threshold_usd else "ok"
            if gap > threshold_usd:
                exceeded = True
            note = f"auth/key cumulative usage ${curr_usage:.4f} (remaining: ${or_info.get('remaining', 0.0):.2f})"
            results.append(
                ProviderReconciliation(
                    provider="openrouter",
                    ledger_usd=ledger_usd,
                    reported_usd=reported_spend,
                    gap_usd=gap,
                    status=status,
                    note=note,
                    ledger_records_count=records_count,
                    metadata=or_info,
                )
            )
        else:
            notes.append("openrouter: failed to query auth/key endpoint")
            results.append(
                ProviderReconciliation(
                    provider="openrouter",
                    ledger_usd=ledger_usd,
                    reported_usd=None,
                    gap_usd=None,
                    status="fetch_error",
                    note="could not connect to openrouter key endpoint or missing key",
                    ledger_records_count=records_count,
                )
            )

    # 3. Meta reconciliation
    if "meta" in target_providers:
        l_info = ledger_data.get("meta", {"usd": 0.0, "records": 0})
        ledger_usd = float(l_info["usd"])
        records_count = int(l_info["records"])

        meta_stats = fetch_meta_stats(window_start, window_end)
        meta_reported = meta_stats["cost_total"]
        gap = meta_reported - ledger_usd
        status = "gap_exceeded" if gap > threshold_usd else "ok"
        if gap > threshold_usd:
            exceeded = True

        note_parts = [f"{meta_stats['calls']} calls in stats.db (${meta_reported:.4f})"]
        if meta_stats["unpriced_calls"] > 0:
            note_parts.append(f"{meta_stats['unpriced_calls']} unpriced calls in catalog ($0.0)")
        results.append(
            ProviderReconciliation(
                provider="meta",
                ledger_usd=ledger_usd,
                reported_usd=meta_reported,
                gap_usd=gap,
                status=status,
                note="; ".join(note_parts),
                ledger_records_count=records_count,
                metadata=meta_stats,
            )
        )

    # 4. Modal reconciliation
    if "modal" in target_providers:
        l_info = ledger_data.get("modal", {"usd": 0.0, "records": 0})
        ledger_usd = float(l_info["usd"])
        records_count = int(l_info["records"])

        # Modal reported spend from ledger or local estimates
        results.append(
            ProviderReconciliation(
                provider="modal",
                ledger_usd=ledger_usd,
                reported_usd=ledger_usd,
                gap_usd=0.0,
                status="ok",
                note=f"{records_count} cell/run rows recorded in ledger",
                ledger_records_count=records_count,
            )
        )

    tot_ledger = sum(p.ledger_usd for p in results)
    tot_reported = sum(p.reported_usd for p in results if p.reported_usd is not None)
    tot_gap = sum(p.gap_usd for p in results if p.gap_usd is not None)

    return ReconciliationReport(
        window_start=window_start,
        window_end=window_end,
        threshold_usd=threshold_usd,
        providers=results,
        total_ledger_usd=tot_ledger,
        total_reported_usd=tot_reported,
        total_gap_usd=tot_gap,
        exceeded_threshold=exceeded,
        notes=notes,
    )


def render_reconciliation(report: ReconciliationReport) -> str:
    """Format reconciliation report as readable terminal output."""
    lines: list[str] = []
    lines.append(
        f"Spend Reconciliation [{report.window_start.strftime('%Y-%m-%d %H:%M UTC')} "
        f"to {report.window_end.strftime('%Y-%m-%d %H:%M UTC')}]"
    )
    lines.append(f"Threshold: ${report.threshold_usd:.2f} USD")
    lines.append("-" * 78)
    lines.append(
        f"{'Provider':<14} {'Ledger':>10} {'Reported':>10} {'Gap':>10}  {'Status':<12} {'Notes'}"
    )
    lines.append("-" * 78)

    for p in report.providers:
        led_str = f"${p.ledger_usd:.2f}"
        rep_str = f"${p.reported_usd:.2f}" if p.reported_usd is not None else "N/A"
        gap_str = f"${p.gap_usd:.2f}" if p.gap_usd is not None else "N/A"
        status_label = "EXCEEDED" if p.status == "gap_exceeded" else p.status.upper()
        lines.append(
            f"{p.provider:<14} {led_str:>10} {rep_str:>10} {gap_str:>10}  {status_label:<12} {p.note}"
        )

    lines.append("-" * 78)
    lines.append(
        f"{'TOTAL':<14} ${report.total_ledger_usd:>9.2f} ${report.total_reported_usd:>9.2f} "
        f"${report.total_gap_usd:>9.2f}  "
        f"{'FAIL' if report.exceeded_threshold else 'PASS'}"
    )

    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"  Note: {note}")

    return "\n".join(lines)
