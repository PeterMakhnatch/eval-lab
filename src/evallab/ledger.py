"""Work-OS ledger dataset: strict parse of `research/inbox/ledger.md`.

Driver brief S1/S3 (`research/inbox/driver-brief-work-os-2026-09-03.md`):
ledger lines become a dataset, scoreboard renders lane x model with Wilson
95% intervals via `evallab.cohort.wilson_interval`. Cells with n<5 print
`insufficient n` (same rule as `lessons.md`).

Current ledger format (header, `ledger.md#FB8A`):
`- <pane> (<model>) → <lane> #<n> <task>: <mark> — <note>`
Backfilled lines predate the pane+model rule, so model may be absent
(`model_known=False`). Mark is one of ✓/~/✗; `~` counts as not-✓.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from evallab.cohort import wilson_interval

MARKS = ("✓", "~", "✗")
MIN_N = 5

_ARROW = "→"
_EMDASH = "—"

# Pane ids look like wK:p7, wH:p1, wS:pA, wR:p1. Anything else (repo-custodian,
# analyst, engineer-data + ops) is a legacy collective identity: parseable but
# flagged model_known=False and pane wells as the raw string.
_PANE_RE = re.compile(r"^[A-Za-z]+\d*:[A-Za-z0-9]+")
_MODEL_RE = re.compile(r"\(([^)]+)\)\s*$")
_LANE_N_RE = re.compile(r"^(?P<lane>[a-z0-9-]+)(?:\s+#(?P<n>\d+)(?:\s+(?P<task>.*))?)?$")


@dataclass(frozen=True)
class LedgerMark:
    pane: str
    model: str | None
    model_known: bool
    lane: str
    task_n: int | None
    task: str
    mark: str
    note: str
    raw: str
    cost_usd: float | None = None
    turns: int | None = None

    @property
    def actor(self) -> str:
        if self.model:
            return f"{self.pane} ({self.model})"
        return self.pane

    @property
    def passed(self) -> bool:
        return self.mark == "✓"


@dataclass(frozen=True)
class ScoreCell:
    lane: str
    actor: str
    n: int
    wins: int
    rate: float
    wilson_low: float | None
    wilson_high: float | None
    median_cost_per_win: float | None
    sufficient: bool = field(compare=False)


def parse_identity(raw: str) -> tuple[str, str | None, bool]:
    raw = raw.strip()
    m = _MODEL_RE.search(raw)
    if m:
        model = m.group(1).strip()
        pane = _MODEL_RE.sub("", raw).strip()
        return pane, model or None, bool(model)
    return raw, None, False


def parse_lane_spec(raw: str) -> tuple[str, int | None, str]:
    m = _LANE_N_RE.match(raw.strip())
    if not m:
        raise ValueError(f"bad lane spec: {raw!r}")
    lane = m.group("lane")
    n = m.group("n")
    task = (m.group("task") or "").strip()
    return lane, int(n) if n is not None else None, task


def parse_line(line: str) -> LedgerMark:
    original = line.rstrip("\n")
    text = original.strip()
    if not text.startswith("- "):
        raise ValueError(f"ledger line must start with '- ': {original!r}")
    body = text[2:].strip()
    if _ARROW not in body:
        raise ValueError(f"ledger line missing '→': {original!r}")
    ident_raw, rest = body.split(_ARROW, 1)
    if ":" not in rest:
        raise ValueError(f"ledger line missing ':': {original!r}")
    lane_raw, tail = rest.split(":", 1)
    mark_pos = min(
        (tail.find(m) for m in MARKS if tail.find(m) >= 0),
        default=-1,
    )
    if mark_pos < 0:
        raise ValueError(f"ledger line missing mark ✓/~/✗: {original!r}")
    mark = tail[mark_pos]
    if _EMDASH in tail:
        note = tail.split(_EMDASH, 1)[1].strip()
    else:
        note = tail[mark_pos + 1 :].strip().lstrip("—- ").strip()
    pane, model, known = parse_identity(ident_raw)
    lane, n, task = parse_lane_spec(lane_raw)
    return LedgerMark(
        pane=pane,
        model=model,
        model_known=known,
        lane=lane,
        task_n=n,
        task=task,
        mark=mark,
        note=note,
        raw=original,
    )


def parse_ledger(text: str) -> tuple[list[LedgerMark], list[str]]:
    marks: list[LedgerMark] = []
    malformed: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("- "):
            continue
        try:
            marks.append(parse_line(line))
        except ValueError:
            malformed.append(line)
    return marks, malformed


def load_ledger(path: Path) -> tuple[list[LedgerMark], list[str]]:
    return parse_ledger(path.read_text())


def sum_session_cost(session_path: Path) -> float:
    """Sum `message.usage.cost.total` over assistant messages in an OMP JSONL session."""
    total = 0.0
    with session_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "message":
                continue
            msg = row.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage") or {}
            cost = usage.get("cost") or {}
            value = cost.get("total", 0)
            try:
                total += float(value)
            except (TypeError, ValueError):
                continue
    return total


def build_scoreboard(marks: list[LedgerMark]) -> list[ScoreCell]:
    buckets: dict[tuple[str, str], list[LedgerMark]] = {}
    for mark in marks:
        buckets.setdefault((mark.lane, mark.actor), []).append(mark)
    cells: list[ScoreCell] = []
    for (lane, actor), rows in buckets.items():
        n = len(rows)
        wins = sum(1 for r in rows if r.passed)
        rate = wins / n if n else 0.0
        bounds = wilson_interval(wins, n)
        costs = sorted(
            r.cost_usd for r in rows if r.passed and r.cost_usd is not None
        )
        median_cost: float | None = None
        if costs:
            median_cost = float(statistics.median(costs))
        cells.append(
            ScoreCell(
                lane=lane,
                actor=actor,
                n=n,
                wins=wins,
                rate=rate,
                wilson_low=bounds[0] if bounds else None,
                wilson_high=bounds[1] if bounds else None,
                median_cost_per_win=median_cost,
                sufficient=n >= MIN_N,
            )
        )
    cells.sort(key=lambda c: (c.lane, c.actor))
    return cells


def render_scoreboard(cells: list[ScoreCell]) -> str:
    lines = [
        "# Scoreboard — ledger-derived, lane x model",
        "",
        "Cells with n<5 print `insufficient n` (lessons.md rule).",
        "",
        "| lane | actor | n | ✓ | rate | wilson 95% | median cost/✓ |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cells:
        if not c.sufficient:
            lines.append(
                f"| {c.lane} | {c.actor} | {c.n} | {c.wins} "
                f"| insufficient n | insufficient n | — |"
            )
            continue
        low = f"{c.wilson_low:.3f}" if c.wilson_low is not None else "—"
        high = f"{c.wilson_high:.3f}" if c.wilson_high is not None else "—"
        cost = f"{c.median_cost_per_win:.4f}" if c.median_cost_per_win is not None else "—"
        lines.append(
            f"| {c.lane} | {c.actor} | {c.n} | {c.wins} "
            f"| {c.rate:.3f} | [{low}, {high}] | {cost} |"
        )
    lines.append("")
    return "\n".join(lines)
