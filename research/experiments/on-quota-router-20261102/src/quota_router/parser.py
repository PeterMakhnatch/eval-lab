"""Parser for `omp usage` text output."""

from __future__ import annotations

import re
import subprocess

from .models import MetricReading, UsageSnapshot

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Duration parser: e.g. "6d18h", "3h25m", "5m", "30s"
DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)

# Canonical known metric definitions: (metric_key, display_name, regex_pattern)
KNOWN_METRIC_PATTERNS = [
    (
        "claude_7d",
        "Claude 7 Day",
        r"Claude 7 Day(?!\s*\(Fable\))\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "fable_7d",
        "Claude 7 Day (Fable)",
        r"Claude 7 Day \(Fable\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "devin_daily",
        "Daily Quota (Pro)",
        r"Daily Quota \(Pro\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "devin_wk",
        "Weekly Quota (Pro)",
        r"Weekly Quota \(Pro\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "gemini_5h",
        "Gemini (5 Hour)",
        r"Gemini \(5 Hour\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "gemini_wk",
        "Gemini (Weekly)",
        r"Gemini \(Weekly\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "ag_shared_wk",
        "Claude & GPT (shared) (Weekly)",
        r"Claude & GPT \(shared\) \(Weekly\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "ag_shared_5h",
        "Claude & GPT (shared) (5 Hour)",
        r"Claude & GPT \(shared\) \(5 Hour\)\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "codex_7d",
        "OpenAI 7 days",
        r"7 days\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "kimi_5h",
        "Kimi 5h limit",
        r"5h limit\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "kimi_monthly",
        "Kimi Monthly total",
        r"Monthly total\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
    (
        "grok_wk",
        "SuperGrok Weekly Credits",
        r"SuperGrok Weekly Credits\s+(?:[^\s\d%]+\s+)?([\d.]+)% used(?:\s*·\s*resets in (\S+))?",
    ),
]


def strip_ansi(text: str) -> str:
    """Remove ANSI color escape sequences from text."""
    return ANSI_ESCAPE_RE.sub("", text)


def parse_duration_to_seconds(dur_str: str | None) -> float | None:
    """Parse duration string like '6d18h', '3h25m', '5m' into seconds.

    Returns None if input is empty or invalid.
    """
    if not dur_str:
        return None
    cleaned = dur_str.strip()
    m = DURATION_RE.match(cleaned)
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    total = days * 86400.0 + hours * 3600.0 + minutes * 60.0 + seconds * 1.0
    return total if total > 0 or (days == 0 and hours == 0 and minutes == 0 and seconds == 0) else None


def parse_usage(raw_text: str | None) -> UsageSnapshot:
    """Parse `omp usage` text output into a structured UsageSnapshot.

    Handles ANSI escapes, percentages, reset durations, and dollar balances.
    Resilient against corrupted, empty, or unexpected formats.
    """
    if not raw_text or not raw_text.strip():
        return UsageSnapshot(raw_text=raw_text or "")

    clean_text = strip_ansi(raw_text)
    metrics: dict[str, MetricReading] = {}
    balances_usd: dict[str, float] = {}

    # 1. Match known metric patterns
    for key, name, pattern in KNOWN_METRIC_PATTERNS:
        match = re.search(pattern, clean_text)
        if match:
            pct_val = float(match.group(1))
            reset_str = match.group(2) if len(match.groups()) >= 2 else None
            reset_sec = parse_duration_to_seconds(reset_str)
            metrics[key] = MetricReading(
                metric_key=key,
                name=name,
                percent_used=pct_val,
                resets_in_str=reset_str,
                resets_in_seconds=reset_sec,
            )

    # 2. Extract dollar balances: e.g. "Balance (USD) ········ $0.85 left"
    balance_matches = re.finditer(r"Balance\s*\(USD\)\s+\S+\s+\$([\d.]+)\s+left", clean_text)
    for i, bm in enumerate(balance_matches):
        bal_key = f"balance_usd_{i}" if i > 0 else "deepseek_balance_usd"
        balances_usd[bal_key] = float(bm.group(1))

    # 3. Extract overage balances: e.g. "Overage balance: $-2.43"
    overage_match = re.search(r"Overage balance:\s*\$(-?[\d.]+)", clean_text)
    if overage_match:
        balances_usd["overage_usd"] = float(overage_match.group(1))

    return UsageSnapshot(
        metrics=metrics,
        balances_usd=balances_usd,
        raw_text=clean_text,
    )


def scrub_usage_text(raw_text: str) -> str:
    """Scrub sensitive identifiers (emails, real names, tokens) from usage text.

    Used to produce sanitized test fixtures safe for commit.
    """
    txt = strip_ansi(raw_text)

    # Replace emails
    txt = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "user@example.com", txt)

    # Replace specific known personal names
    txt = re.sub(r"Peter\s+Makhnatch", "User", txt)

    # Replace Devin account IDs
    txt = re.sub(r"devin-team\$account-[0-9a-f]+", "devin-team$account-scrubbed", txt)

    # Replace Kimi alphanumeric account hashes
    txt = re.sub(r"\b[a-z0-9]{20}\b", "scrubbed-token", txt)

    return txt


def fetch_live_usage(timeout: int = 30) -> str:
    """Execute `omp usage` CLI and return raw output.

    Falls back to empty string on error or timeout.
    """
    try:
        res = subprocess.run(
            ["omp", "usage"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.stdout
    except Exception:
        return ""
