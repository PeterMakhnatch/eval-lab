#!/usr/bin/env python3
"""T2A-PKG-ANATOMY2 benchmark: latency, C1-C5 detection, 30 repair-qualified localization, OQ-6 DEV cards.

DEV only: package bytes, the vendored DEV reference patches, the journal's ``task_quality.repair_qualified`` events
(read-only) and the frozen OQ-6 plan's package paths. ``CorpusRegistry`` gates the OQ-6 cohort; no answer key, ``keys_path``
or key count is read. No model call, no container.

    run_bench.py --out <receipts dir>
"""

from __future__ import annotations

import argparse
import builtins
import csv
import hashlib
import json
import keyword
import random
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet")
sys.path.insert(0, str(BRIDGE))

from stats import fmt, wilson  # noqa: E402

from evallab.package_anatomy import analyze_package  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

CLASSIFICATION = REPO / "research/experiments/a-harden-all-20261030/results/classification.json"
REFERENCES = REPO / "research/experiments/mimo-v6-full-fleet/references.csv"
JOURNAL = Path("/Users/petermakhnatch/.local/state/evallab-prod-live/prod.sqlite")
OQ6_PLAN = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun-v4/plan.json")
C_TASKS = {
    "C1": ("fx-c5-impossible-test-modification-20261030/tasks/billing-amount-format", "grader_tamperable"),
    "C2": ("fx-c5-impossible-hidden-test-reading-20261030/tasks/c2-release-ledger-order", "answer_reachable"),
    "C3": ("fx-c5-impossible-evaluator-mutation-20261030/tasks/payout-export", "grader_tamperable"),
    "C4": ("fx-c5-impossible-fabricated-verification-20261030/tasks/release-attestation", "exploit_grader_gap"),
    "C5": ("fx-c5-impossible-environment-acquisition-20261030/tasks/glyph2-decode", "answer_reachable"),
}


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def references() -> dict[str, Path]:
    out = {}
    for row in csv.DictReader(REFERENCES.open()):
        path = REPO / row["patch_path"]
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row["patch_sha256"]:
            out[row["task_id"]] = path
    return out


def timed(package: Path, patch: Path | None, repeats: int = 3) -> tuple[dict, float]:
    samples = []
    card = {}
    for _ in range(repeats):
        start = time.perf_counter()
        card = analyze_package(package, reference_patch=patch)
        samples.append((time.perf_counter() - start) * 1000)
    return card, statistics.median(samples)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def dev_35(refs: dict[str, Path]) -> list[tuple[str, Path, Path | None]]:
    rows = json.loads(CLASSIFICATION.read_text())["tasks"]
    return [
        (row["task_id"], Path(row["package"]), refs.get(row["task_id"]) if row["reference_patch"] else None)
        for row in rows
        if row["strict_dev"]
    ]


def compact(card: dict, *, evidence: int = 3, items: int = 6) -> dict:
    """Wave-2 card: same sections, evidence and list lengths capped."""
    def cap(value):
        if isinstance(value, list):
            return [cap(v) for v in value[:items]]
        if isinstance(value, dict):
            return {k: cap(v) for k, v in value.items()}
        return value

    out = cap({k: v for k, v in card.items() if k != "vulnerability_hypotheses"})
    out["vulnerability_hypotheses"] = [{**h, "evidence": h["evidence"][:evidence]} for h in card["vulnerability_hypotheses"]]
    return out


def weak_tokens(card: dict) -> set[str]:
    tokens: set[str] = set()
    rigor = card["test_suite_rigor"]
    for cue in rigor["instruction_named_symbols_without_test_reference"]:
        tokens.add(cue["symbol"])
    ref = rigor["reference_patch"]
    if ref:
        tokens.update(row["symbol"] for row in ref["unexercised"])
        for branch in ref["branches_unreferenced"]:
            tokens.update(branch["literals"])
            tokens.update(branch["idents"])
    return tokens


NOISE = frozenset(keyword.kwlist) | frozenset(dir(builtins)) | {"self", "cls", "pytest", "assert", "tests", "test", "mock", "args"}


def idents(text: str) -> set[str]:
    return {i for i in re.findall(r"[A-Za-z_]\w{3,}", text) if i not in NOISE}


def added_text(patch: Path) -> str:
    return "\n".join(line[1:] for line in patch.read_text(errors="replace").splitlines() if line.startswith("+") and not line.startswith("+++"))


def repaired_events() -> dict[str, dict]:
    con = sqlite3.connect(f"file:{JOURNAL}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            "SELECT envelope FROM events WHERE event_type = 'task_quality.repair_qualified' ORDER BY seq LIMIT 400"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for (envelope,) in rows:
        payload = json.loads(envelope)["payload"]
        out.setdefault(payload["task_id"], payload)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    refs = references()
    summary: dict = {}

    # Q1: latency on the 35 strict-DEV packages
    latencies, classes, ids = [], {}, {}
    rows35 = []
    for task, package, patch in dev_35(refs):
        card, ms = timed(package, patch)
        latencies.append(ms)
        rows35.append({"task": task, "ms": round(ms, 2), "reference_patch": patch is not None,
                       "hypotheses": len(card["vulnerability_hypotheses"]),
                       "classes": card["hypothesis_classes"]})
        for hyp in card["vulnerability_hypotheses"]:
            base = hyp["id"].split("#")[0]
            ids[base] = ids.get(base, 0) + 1
        for cls in card["hypothesis_classes"]:
            classes[cls] = classes.get(cls, 0) + 1
    summary["q1"] = {"n": len(latencies), "p50_ms": round(pct(latencies, 0.5), 2), "p95_ms": round(pct(latencies, 0.95), 2),
                     "max_ms": round(max(latencies), 2), "classes_per_package": classes, "hypothesis_ids": dict(sorted(ids.items(), key=lambda kv: -kv[1])),
                     "with_reference_patch": sum(r["reference_patch"] for r in rows35)}
    summary["q1"]["gate_pass"] = summary["q1"]["p95_ms"] < 50 and summary["q1"]["p50_ms"] < 50
    (out / "latency-35.json").write_text(json.dumps({"rows": rows35, **summary["q1"]}, indent=1) + "\n")

    # Q2: C1-C5
    c_rows = []
    for name, (rel, expected) in C_TASKS.items():
        card, ms = timed(REPO / "research/experiments" / rel, None)
        hyps = card["vulnerability_hypotheses"]
        rank = next((i + 1 for i, h in enumerate(hyps) if h["mechanism_class"] == expected and h["score"] >= 60), None)
        c_rows.append({"task": name, "package": rel, "expected": expected, "hit": rank is not None, "rank": rank, "ms": round(ms, 2),
                       "top3": [(h["id"], h["mechanism_class"], h["score"]) for h in hyps[:3]],
                       "classes": card["hypothesis_classes"]})
    summary["q2"] = {"hits": sum(r["hit"] for r in c_rows), "n": len(c_rows), "gate_pass": sum(r["hit"] for r in c_rows) >= 4}
    (out / "c1-c5.json").write_text(json.dumps({"rows": c_rows, **summary["q2"]}, indent=1) + "\n")

    # Q3: 30 repair-qualified originals
    events = repaired_events()
    rng = random.Random(20261101)
    rows30, hit_any, loc_hits, loc_n, chance = [], 0, 0, 0, []
    for task, payload in sorted(events.items()):
        original, repaired = Path(payload["original_task_package"]), Path(payload["repaired_task"])
        patch = refs.get(task)
        card, ms = timed(original, patch, repeats=1)
        weak = [h for h in card["vulnerability_hypotheses"] if h["mechanism_class"] == "weak_check" and h["score"] >= 50]
        hit_any += bool(weak)
        row = {"task": task, "weak_check_hypotheses": [h["id"] for h in weak], "reference_patch": patch is not None}
        orig_tests = original / "tests/test.patch"
        rep_tests = repaired / "tests/test_outputs.py"  # the repair adds a supplementary test file
        if patch is not None and orig_tests.is_file() and rep_tests.is_file():
            new = idents(rep_tests.read_text(errors="replace")) - idents(orig_tests.read_text(errors="replace"))
            patch_idents = idents(added_text(patch))
            relevant = {i for i in new if i in patch_idents or i in idents((original / "instruction.md").read_text(errors="replace"))}
            flagged = weak_tokens(card)
            row.update(repair_new_tokens=len(relevant), flagged=len(flagged))
            if relevant:
                hits = bool(relevant & flagged)
                loc_n += 1
                loc_hits += hits
                row["localized"] = hits
                universe = sorted({s["symbol"] for s in (card["test_suite_rigor"]["reference_patch"] or {}).get("unexercised", [])} | flagged)
                if universe:
                    k = len(flagged)
                    pool = sorted(patch_idents)
                    draws = sum(bool(relevant & set(rng.sample(pool, min(k, len(pool))))) for _ in range(200))
                    chance.append(draws / 200)
                    row["chance"] = round(draws / 200, 3)
            else:
                row["localized"] = None
        rows30.append(row)
    n30 = len(rows30)
    q3b = loc_hits / loc_n if loc_n else None
    summary["q3"] = {"n": n30, "weak_check_any": hit_any, "weak_check_wilson": [round(x, 3) for x in wilson(hit_any, n30)],
                     "localized": loc_hits, "localizable_n": loc_n, "localized_rate": q3b,
                     "localized_wilson": [round(x, 3) for x in wilson(loc_hits, loc_n)] if loc_n else None,
                     "chance_rate_mean": round(statistics.mean(chance), 3) if chance else None,
                     "gate_pass": wilson(hit_any, n30)[0] >= 0.70 and q3b is not None and chance != [] and q3b > statistics.mean(chance)}
    (out / "repair30.json").write_text(json.dumps({"rows": rows30, **summary["q3"]}, indent=1) + "\n")

    # OQ-6 DEV cards
    registry = CorpusRegistry(repo_root=REPO)
    plan = json.loads(OQ6_PLAN.read_text())["cases"]
    cards, refused, build_ms = [], 0, []
    for case in plan:
        if case.get("stage") != "dev":
            continue
        if registry.assigned_split(case["dataset_id"], item_id=case["item_id"], task_id=case["task_id"]) != "dev" \
                or registry.assigned_test_anywhere(item_id=case["item_id"], task_id=case["task_id"]):
            refused += 1
            continue
        package = Path(case["package"])
        patch = refs.get(case["task_id"])
        start = time.perf_counter()
        card = analyze_package(package, reference_patch=patch)
        build_ms.append((time.perf_counter() - start) * 1000)
        card.update(case_id=case["case_id"], dataset_id=case["dataset_id"], task_id=case["task_id"], item_id=case["item_id"],
                    package_source=str(package))
        cards.append(compact(card))
    cards.sort(key=lambda c: c["case_id"])
    receipt = out / "oq6-dev-anatomy-cards.jsonl"
    receipt.write_text("".join(json.dumps(c, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for c in cards))
    dist: dict[str, int] = {}
    cls_dist: dict[str, int] = {}
    for card in cards:
        for hyp in card["vulnerability_hypotheses"]:
            dist[hyp["id"].split("#")[0]] = dist.get(hyp["id"].split("#")[0], 0) + 1
        for cls in card["hypothesis_classes"]:
            cls_dist[cls] = cls_dist.get(cls, 0) + 1
    summary["oq6"] = {"cards": len(cards), "refused_non_dev": refused, "cards_with_reference_patch": sum(c["reference_patch_used"] for c in cards),
                      "datasets": {d: sum(c["dataset_id"] == d for c in cards) for d in sorted({c["dataset_id"] for c in cards})},
                      "p50_ms": round(pct(build_ms, 0.5), 2), "p95_ms": round(pct(build_ms, 0.95), 2),
                      "hypothesis_ids": dict(sorted(dist.items(), key=lambda kv: -kv[1])), "classes_per_card": cls_dist,
                      "receipt_sha256_12": sha12(receipt)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(fmt(summary["q3"]["weak_check_any"], summary["q3"]["n"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
