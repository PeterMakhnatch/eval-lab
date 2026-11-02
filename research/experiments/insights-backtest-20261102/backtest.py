"""Retrodiction backtest for the insight engine on the 2026-11-02 overnight run.

The engine sees only material written before the 03:25 ET cutoff (a frozen
snapshot of the coordinator log prefix, author reports and their receipts).
Its cards are scored against corrections and follow-ups the verifiers and the
coordinator found *after* the cutoff (``ground_truth.json``). A single strong
agent given the same snapshot is the baseline. A judge from a different model
family matches cards to ground-truth items without seeing which system wrote
them.

Everything this script reads or writes lives under the backtest directory in
local state (default ``~/.local/state/evallab-insights/backtests/2026-11-02-overnight``);
the ground truth quotes research notes, so it is never committed.

    backtest.py baseline     # one Opus pass over the same snapshot
    backtest.py judge        # blind cross-family matching, both systems
    backtest.py audit        # did any worker read outside the snapshot?
    backtest.py report       # metrics -> results.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.insights.runner import OmpLauncher, WorkerHandle, WorkerSpec, attest_session

DEFAULT_DIR = Path.home() / ".local/state/evallab-insights/backtests/2026-11-02-overnight"
JUDGE_ROUTE = "google-antigravity/gemini-3.8-flash"
BASELINE_ROUTE = "anthropic/claude-opus-5-5"
GT_PER_JUDGE_CALL = 5
MAX_PARALLEL_WORKERS = 4
# Original locations of the snapshotted material, and verifier output that only
# exists after the cutoff. A worker touching these outside the snapshot leaked.
LEAK_PATTERNS = (
    re.compile(r"research-context/bridge/2026-11-02-overnight"),
    re.compile(r"eval-lab-results/2026-11-02"),
    re.compile(r"/ver2?/raw/|/VF-[A-Z][\w-]*/|REVIEW-[\w-]+/report"),
)
_SNAPSHOT_PATH = re.compile(r"[^\s\"']*/snapshot/[^\s\"']*")


# ---------------------------------------------------------------- inputs
@dataclass(frozen=True)
class Card:
    card_id: str
    system: str
    title: str
    text: str
    experiments: tuple[str, ...]
    created_at: datetime | None
    status: str


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def _ground_truth(root: Path) -> list[dict[str, Any]]:
    return list(_load_json(root / "ground_truth.json"))


def _cutoff(root: Path) -> datetime:
    return datetime.fromisoformat(_load_json(root / "cutoff.json")["cutoff_et"])


def _ledger(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{root / 'state' / 'insights.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def engine_cards(root: Path) -> list[Card]:
    """Every analyst card that passed the engine's code checks."""
    con = _ledger(root)
    experiments = {
        row["claim_id"]: row["experiment"]
        for row in con.execute("SELECT claim_id, experiment FROM claims")
    }
    cards: list[Card] = []
    for row in con.execute("SELECT body FROM insights ORDER BY created_at"):
        record = json.loads(row["body"])
        if record["check"]["status"] == "fail":
            continue
        card = record["card"]
        parts = [
            card["statement"],
            "Observations: " + " | ".join(card["observations"]),
            "Interpretation: " + card["interpretation"],
        ]
        if card.get("challenge"):
            parts.append("Check proposed: " + card["challenge"]["check"])
        if card.get("proposed_experiment"):
            parts.append("Experiment proposed: " + card["proposed_experiment"]["hypothesis"])
        parts.extend("Prediction: " + p["statement"] for p in card.get("predictions", []))
        cited = sorted({
            experiments[e["claim_id"]] for e in card["evidence"] if e["claim_id"] in experiments
        })
        cards.append(Card(
            card_id=record["insight_id"], system="engine", title=card["title"],
            text="\n".join(parts), experiments=tuple(cited),
            created_at=datetime.fromisoformat(record["created_at"]),
            status=record["status"],
        ))
    return cards


def baseline_cards(root: Path) -> list[Card]:
    out = root / "baseline" / "out" / "cards.json"
    if not out.exists():
        return []
    created = datetime.fromtimestamp(out.stat().st_mtime, UTC)
    cards = []
    for index, item in enumerate(_load_json(out)["cards"], start=1):
        text = item["statement"] + "\nCheck proposed: " + item.get("check", "")
        cards.append(Card(
            card_id=f"B-{index:03d}", system="baseline", title=item["title"], text=text,
            experiments=tuple(item.get("experiments", [])), created_at=created,
            status="baseline",
        ))
    return cards


# ---------------------------------------------------------------- workers
def _run_workers(specs: list[WorkerSpec], *, timeout_minutes: float) -> None:
    launcher = OmpLauncher()
    pending = list(specs)
    running: dict[str, tuple[WorkerHandle, float]] = {}
    while pending or running:
        while pending and len(running) < MAX_PARALLEL_WORKERS:
            spec = pending.pop(0)
            running[spec.pass_id] = (launcher.launch(spec), time.monotonic())
        time.sleep(5)
        for pass_id, (handle, started) in list(running.items()):
            if handle.poll() is not None:
                del running[pass_id]
            elif time.monotonic() - started > timeout_minutes * 60:
                handle.kill()
                del running[pass_id]


def _spec(
    pass_dir: Path, route: str, prompt: str, *, tools: str, minutes: float,
    thinking: str = "medium",
) -> WorkerSpec:
    pass_dir.mkdir(parents=True, exist_ok=True)
    (pass_dir / "out").mkdir(exist_ok=True)
    prompt_file = pass_dir / "prompt.md"
    prompt_file.write_text(prompt, "utf-8")
    return WorkerSpec(
        pass_id=f"{pass_dir.parent.name}-{pass_dir.name}", route=route, thinking=thinking,
        max_minutes=minutes, pass_dir=pass_dir, prompt_file=prompt_file, tools=tools,
    )


def _attested(pass_dir: Path, route: str) -> dict[str, Any]:
    attestation = attest_session(pass_dir / "session", route)
    return {
        "served": attestation.served,
        "off_route": attestation.off_route,
        "tokens": attestation.tokens,
    }


# ---------------------------------------------------------------- baseline
BASELINE_PROMPT = """You are a senior research analyst reviewing an overnight batch of
experiments on AI-evaluation infrastructure (cheat detection, graders, workflow engines).
It is 03:25 ET. Everything written so far is under `{snapshot}`:

- `{findings}`: the coordinator's running log of results.
- the author reports listed in `reports.txt` (one path per line), each next to the
  receipt files its author wrote.

Read what you need. Then write `out/cards.json`:

{{"cards": [{{"title": "...", "statement": "...", "check": "...", "experiments": ["..."]}}]}}

Each card is one finding a research lead would want before the next decision:
a claim that is likely wrong or overstated (and why), a connection between experiments,
an anomaly worth explaining, or the next experiment to run. `statement` says what you
observed and what you infer from it; `check` is the cheapest concrete step that would
settle it; `experiments` are the experiment ids the card is about. Write as many cards
as are worth a lead's attention, most important first, and at most 40. Recompute numbers
from the receipts where you can. Read only files under `{snapshot}`. Do not run models,
trials or paid services. Write only `out/cards.json`.
"""


def cmd_baseline(root: Path) -> int:
    snapshot = root / "snapshot"
    base = root / "baseline"
    reports = sorted(str(path) for path in snapshot.rglob("*.md") if path.name != "FINDINGS.md")
    base.mkdir(parents=True, exist_ok=True)
    (base / "reports.txt").write_text("\n".join(reports) + "\n", "utf-8")
    findings = next(snapshot.rglob("FINDINGS.md"))
    prompt = BASELINE_PROMPT.format(snapshot=snapshot, findings=findings)
    spec = _spec(base, BASELINE_ROUTE, prompt, tools="read,bash,grep,glob,write",
                 minutes=45, thinking="high")
    _run_workers([spec], timeout_minutes=46)
    receipt = _attested(base, BASELINE_ROUTE)
    out = base / "out" / "cards.json"
    receipt["cards"] = len(_load_json(out)["cards"]) if out.exists() else None
    (base / "receipt.json").write_text(json.dumps(receipt, indent=2), "utf-8")
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["cards"] is not None else 1


# ---------------------------------------------------------------- judge
JUDGE_PROMPT = """You are grading whether analyst notes written before 03:25 ET anticipated
problems and follow-ups that were found only later that night.

Below are LATER FINDINGS (each with the author's earlier claim it concerns) and the
analysts' CARDS. For every later finding, decide which cards, if any, anticipate it.

- "full": a card targets the same experiment or artifact AND names the same specific
  problem, mechanism or follow-up, so acting on the card would very likely have
  surfaced the finding.
- "partial": a card targets the same experiment with a closely related concern but
  misses the specific mechanism, or names the right mechanism on a neighbouring target.
- "none": no card does either. Generic advice ("check denominators everywhere") is none.

If a finding carries a NOTE about what was already known before 03:25, apply it: a card
that only repeats an already-known caveat earns "none".

Write exactly one file, `out/judgment.json`:
{{"judgments": [{{"gt_id": "GT-..", "match": "full|partial|none", "card_ids": ["..."],
  "why": "at most 40 words"}}]}}
Cover every finding below exactly once. `card_ids` lists only cards supporting the match
(empty for none). Do not read or write any other file.

# LATER FINDINGS
{findings}

# CARDS
{cards}
"""

QUALITY_PROMPT = """You are rating analyst cards written for a research lead. For each card,
decide whether it is SPECIFIC (names a concrete experiment or artifact, a concrete
defect, connection or anomaly, and a check someone could run) or GENERIC (advice that
would apply to any experiment, or no concrete check). Do not judge whether it is true.

Write exactly one file, `out/quality.json`:
{{"ratings": [{{"card_id": "...", "rating": "specific|generic", "why": "at most 20 words"}}]}}
Rate every card below exactly once. Do not read or write any other file.

# CARDS
{cards}
"""


def _finding_text(item: dict[str, Any]) -> str:
    earlier = " / ".join(ref["quote"][:600] for ref in item.get("target_pre_cutoff_refs", []))
    lines = [
        f"## {item['gt_id']} (experiments: {', '.join(item['target_items'])})",
        f"Later finding: {item['summary']}",
        f"Verifier wording: {item['post_quote'][:500]}",
    ]
    if earlier:
        lines.append(f"Earlier author claim: {earlier}")
    if item.get("pre_cutoff_caveat") or item["anticipatable"] == "partial":
        lines.append(f"NOTE: {item['anticipatable_note']}")
    return "\n".join(lines)


def _card_text(card: Card, alias: str) -> str:
    return (
        f"## {alias} (experiments: {', '.join(card.experiments) or 'unstated'})\n"
        f"{card.title}\n{card.text[:2400]}"
    )


def _aliases(cards: list[Card], seed: int) -> dict[str, Card]:
    shuffled = list(cards)
    random.Random(seed).shuffle(shuffled)
    return {f"C{index:03d}": card for index, card in enumerate(shuffled, start=1)}


def cmd_judge(root: Path) -> int:
    gt = _ground_truth(root)
    systems = {"engine": engine_cards(root), "baseline": baseline_cards(root)}
    specs: list[WorkerSpec] = []
    for name, cards in systems.items():
        if not cards:
            print(f"{name}: no cards; skipped", file=sys.stderr)
            continue
        aliases = _aliases(cards, seed=20261102)
        judge_root = root / "judge" / name
        judge_root.mkdir(parents=True, exist_ok=True)
        (judge_root / "aliases.json").write_text(
            json.dumps({alias: card.card_id for alias, card in aliases.items()}, indent=2),
            "utf-8",
        )
        cards_text = "\n\n".join(_card_text(card, alias) for alias, card in aliases.items())
        for start in range(0, len(gt), GT_PER_JUDGE_CALL):
            batch = gt[start:start + GT_PER_JUDGE_CALL]
            findings = "\n\n".join(_finding_text(item) for item in batch)
            prompt = JUDGE_PROMPT.format(findings=findings, cards=cards_text)
            specs.append(_spec(judge_root / f"match-{start // GT_PER_JUDGE_CALL:02d}",
                               JUDGE_ROUTE, prompt, tools="write", minutes=15))
        specs.append(_spec(judge_root / "quality", JUDGE_ROUTE,
                           QUALITY_PROMPT.format(cards=cards_text), tools="write", minutes=15))
    _run_workers(specs, timeout_minutes=16)
    failures = 0
    for spec in specs:
        receipt = _attested(spec.pass_dir, JUDGE_ROUTE)
        receipt["outputs"] = sorted(p.name for p in (spec.pass_dir / "out").iterdir())
        (spec.pass_dir / "receipt.json").write_text(json.dumps(receipt, indent=2), "utf-8")
        if receipt["off_route"] or not receipt["outputs"]:
            failures += 1
            print(f"judge call failed or off-route: {spec.pass_dir}", file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------- audit
def _tool_calls(session_dir: Path) -> Iterable[str]:
    for path in session_dir.rglob("*.jsonl"):
        for line in path.read_text("utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            if message.get("role") != "assistant":
                continue
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    yield json.dumps(part.get("arguments", {}))


def cmd_audit(root: Path) -> dict[str, Any]:
    session_dirs = sorted((root / "state" / "passes").glob("*/session"))
    session_dirs.append(root / "baseline" / "session")
    flagged: list[dict[str, str]] = []
    calls = 0
    for session in session_dirs:
        if not session.exists():
            continue
        for arguments in _tool_calls(session):
            calls += 1
            outside = _SNAPSHOT_PATH.sub("", arguments)
            hits = [pattern.pattern for pattern in LEAK_PATTERNS if pattern.search(outside)]
            if hits:
                flagged.append({
                    "pass": session.parent.name, "patterns": ",".join(hits),
                    "call": arguments[:300],
                })
    result = {"sessions": len(session_dirs), "tool_calls": calls, "flagged": flagged}
    (root / "audit.json").write_text(json.dumps(result, indent=2), "utf-8")
    return result


# ---------------------------------------------------------------- report
def _judgments(root: Path, system: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    judge_root = root / "judge" / system
    aliases = _load_json(judge_root / "aliases.json")
    matched: dict[str, dict[str, Any]] = {}
    for path in sorted(judge_root.glob("match-*/out/judgment.json")):
        for item in _load_json(path)["judgments"]:
            item["card_ids"] = [aliases.get(alias, alias) for alias in item.get("card_ids", [])]
            matched[item["gt_id"]] = item
    ratings: dict[str, str] = {}
    quality = judge_root / "quality" / "out" / "quality.json"
    if quality.exists():
        for item in _load_json(quality)["ratings"]:
            ratings[aliases.get(item["card_id"], item["card_id"])] = item["rating"]
    return matched, ratings


def _system_metrics(
    root: Path, system: str, cards: list[Card], gt: list[dict[str, Any]],
    start: datetime | None, cutoff: datetime,
) -> dict[str, Any]:
    matched, ratings = _judgments(root, system)
    by_id = {card.card_id: card for card in cards}
    full = [g for g in gt if matched.get(g["gt_id"], {}).get("match") == "full"]
    partial = [g for g in gt if matched.get(g["gt_id"], {}).get("match") == "partial"]
    hit_cards = {
        cid for item in matched.values() if item["match"] != "none" for cid in item["card_ids"]
    }
    leads = []
    for item in full + partial:
        first = datetime.fromisoformat(item["first_seen_et"])
        times = [
            by_id[cid].created_at for cid in matched[item["gt_id"]]["card_ids"]
            if cid in by_id and by_id[cid].created_at is not None
        ]
        if times and start is not None:
            # A card's delay after its system started, set against the night's clock.
            card_delay = (min(t for t in times if t is not None) - start).total_seconds() / 60
            leads.append(round((first - cutoff).total_seconds() / 60 - card_delay))
    return {
        "cards": len(cards),
        "judged_items": len(matched),
        "full": len(full),
        "partial": len(partial),
        "recall_full": round(len(full) / len(gt), 3),
        "recall_full_or_partial": round((len(full) + len(partial)) / len(gt), 3),
        "eligible_yes": sum(1 for g in gt if g["anticipatable"] == "yes"),
        "eligible_full": sum(1 for g in full if g["anticipatable"] == "yes"),
        "eligible_full_or_partial": sum(
            1 for g in full + partial if g["anticipatable"] == "yes"
        ),
        "cards_matching_any_item": len(hit_cards & set(by_id)),
        "specific_cards": sum(1 for cid in by_id if ratings.get(cid) == "specific"),
        "rated_cards": sum(1 for cid in by_id if cid in ratings),
        "lead_minutes": sorted(leads),
        "per_item": {
            g["gt_id"]: matched.get(g["gt_id"], {}).get("match", "unjudged") for g in gt
        },
    }


def _pass_summary(passes: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, dict[str, Any]] = {}
    for record in passes:
        lens = summary.setdefault(record["lens"], {
            "passes": 0, "succeeded": 0, "output_tokens": 0, "cache_read": 0, "served": {},
        })
        lens["passes"] += 1
        lens["succeeded"] += record["status"] == "succeeded"
        tokens = record.get("tokens") or {}
        lens["output_tokens"] += tokens.get("output", 0)
        lens["cache_read"] += tokens.get("cacheRead", 0)
        for model, count in (record.get("served") or {}).items():
            lens["served"][model] = lens["served"].get(model, 0) + count
    return summary


def _claim_summary(con: sqlite3.Connection) -> dict[str, Any]:
    def count(sql: str) -> int:
        return int(con.execute(sql).fetchone()[0])

    return {
        "claims": count("SELECT COUNT(*) FROM claims"),
        "check_fail": count("SELECT COUNT(*) FROM claims WHERE check_status = 'fail'"),
        "check_warn": count("SELECT COUNT(*) FROM claims WHERE check_status = 'warn'"),
        "recompute": {
            row[0]: row[1] for row in con.execute(
                "SELECT recompute_status, COUNT(*) FROM claims "
                "WHERE recompute_status IS NOT NULL GROUP BY recompute_status"
            )
        },
        "experiments": count("SELECT COUNT(*) FROM experiments"),
        "signals": count("SELECT COUNT(*) FROM signals"),
    }


def cmd_report(root: Path) -> int:
    gt = _ground_truth(root)
    cutoff = _cutoff(root)
    con = _ledger(root)
    passes = [json.loads(row["body"]) for row in con.execute("SELECT body FROM passes")]
    start = min((datetime.fromisoformat(p["created_at"]) for p in passes), default=None)
    engine = engine_cards(root)
    result: dict[str, Any] = {
        "ground_truth_items": len(gt),
        "engine": _system_metrics(root, "engine", engine, gt, start, cutoff),
        "engine_passes": _pass_summary(passes),
        "claims": _claim_summary(con),
        "audit": cmd_audit(root),
    }
    baseline = baseline_cards(root)
    if baseline:
        base_start = datetime.fromtimestamp(
            (root / "baseline" / "prompt.md").stat().st_mtime, UTC
        )
        result["baseline"] = _system_metrics(
            root, "baseline", baseline, gt, base_start, cutoff
        )
        result["baseline_receipt"] = _load_json(root / "baseline" / "receipt.json")
    (root / "results.json").write_text(json.dumps(result, indent=2, default=str), "utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "audit"}, indent=2, default=str))
    print(f"audit: {len(result['audit']['flagged'])} flagged of "
          f"{result['audit']['tool_calls']} tool calls")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=["baseline", "judge", "audit", "report"])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    root: Path = args.dir.expanduser().resolve()
    if args.command == "baseline":
        return cmd_baseline(root)
    if args.command == "judge":
        return cmd_judge(root)
    if args.command == "audit":
        print(json.dumps(cmd_audit(root), indent=2))
        return 0
    return cmd_report(root)


if __name__ == "__main__":
    raise SystemExit(main())
