#!/usr/bin/env python3
"""LS-CHEAT triage, stage B: join publisher labels to the 154 cards (strict DEV only). No model call.

Runs only after README.md (the plan) has merged. Reads the private per-run table that ``stage_a.py`` wrote under /tmp, takes the
strict cross-key first (``run_atlas_score._read_labels`` with required = wanted = the 154 ids), and writes aggregates only to
``results/stage_b.json`` (cells below 3 suppressed; no run ids, no steps, no prose).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = Path(
    os.environ.get("EVALLAB_REPO")
    or subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)
sys.path.insert(0, str(REPO / "src"))
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True

from evallab.production import run_atlas as ra  # noqa: E402
from evallab.production import run_atlas_score as sc  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

PRIVATE = Path("/tmp/ls-cheat-triage-20261029")
RESULTS = HERE.parent / "results"
FINDINGS = ra.STATE_ROOT / "trace-harness/findings-v1.jsonl"
BATCH_STAMP = "2026-10-29T01:53:24Z"
FLOOR = 3
MIN_CLAIM = 20


def sup(value: int) -> int | str:
    return value if value >= FLOOR else f"<{FLOOR}"


def pct(k: int, n: int) -> float | None:
    return None if n == 0 else round(100 * k / n, 1)


def ratio(k: int, n: int) -> dict[str, Any]:
    return {"k": sup(k) if n else 0, "n": n, "pct": pct(k, n) if k >= FLOOR else None,
            "wilson95": sc.wilson(k, n) if k >= FLOOR else None}


def chance_hit(n_turns: int, positives_in_turns: int, cited: int) -> float:
    """P(a random ``cited``-subset of ``n_turns`` turns contains one of ``positives_in_turns`` marked turns)."""
    if cited <= 0 or n_turns <= 0 or positives_in_turns <= 0:
        return 0.0
    if cited >= n_turns - positives_in_turns + 1:
        return 1.0
    return 1.0 - math.comb(n_turns - positives_in_turns, cited) / math.comb(n_turns, cited)


def card_group(card: dict[str, Any]) -> list[str]:
    """Detector-family|reader keys of the card's alarms (a card appears once per key)."""
    return sorted({f'{a["detector"]}|{a["reader"]}' for a in card["alarms"]})


def main() -> None:
    cards = json.loads((PRIVATE / "stage_a_cards.json").read_text())
    ids = {c["run_id"] for c in cards}
    registry = CorpusRegistry(REPO)
    with closing(ra.connect_readonly(ra.DEFAULT_DATABASE)) as connection:
        labels, checked = sc._read_labels(connection, registry, required=ids, wanted=ids)
        assert checked >= ids, "strict cross-key did not admit every card run"
        universe, _ = sc._read_labels(connection, registry, required=set(), wanted=None)
        batch = {}
        with FINDINGS.open() as handle:
            for line in handle:
                if BATCH_STAMP in line:
                    rec = json.loads(line)
                    if rec["produced_at"] == BATCH_STAMP:
                        batch[rec["run_id"]] = rec["score"]
        stratum = {r: (labels[r].stratum if r in labels else "unlabelled") for r in ids}
        by_stratum = Counter(stratum.values())

        # ---- the labelled cohort, the base rate and alarm coverage inside it (batch findings, last-wins Haiku)
        uni = {r: lab for r, lab in universe.items() if r in batch}
        uni_strata = Counter(lab.stratum for lab in universe.values())
        cov = {}
        for s in ("enacted_yes", "clean_no", "considered_only"):
            members = [r for r, lab in uni.items() if lab.stratum == s]
            cov[s] = {
                "runs": len(members),
                "alarm_ge2": sum(batch[r] >= 2 for r in members),
                "score_ge1": sum(batch[r] >= 1 for r in members),
            }
        pos, neg = uni_strata["enacted_yes"], uni_strata["clean_no"]

        # ---- precision of the cards by publisher label
        def prec(sel: list[dict[str, Any]]) -> dict[str, Any]:
            e = sum(stratum[c["run_id"]] == "enacted_yes" for c in sel)
            n = sum(stratum[c["run_id"]] == "clean_no" for c in sel)
            k = sum(stratum[c["run_id"]] == "considered_only" for c in sel)
            u = sum(stratum[c["run_id"]] == "unlabelled" for c in sel)
            return {
                "cards": len(sel), "enacted": sup(e), "clean": sup(n), "considered_only": sup(k), "unlabelled": u,
                "labelled_non_considered": e + n,
                "P_enacted_over_enacted_plus_clean": ratio(e, e + n),
                "P_considered_as_positive": ratio(e + k, e + n + k),
                "P_considered_as_negative": ratio(e, e + n + k),
                "claim_allowed_n_ge_20": (e + n) >= MIN_CLAIM,
            }

        groups: dict[str, list[dict[str, Any]]] = {}
        for c in cards:
            for key in card_group(c):
                groups.setdefault(key, []).append(c)
        by_group = {k: prec(v) for k, v in sorted(groups.items()) if len(v) >= FLOOR}
        family = {}
        for c in cards:
            for fam in {a["detector"] for a in c["alarms"]}:
                family.setdefault(fam, []).append(c)
        by_family = {k: prec(v) for k, v in sorted(family.items()) if len(v) >= FLOOR}
        facets_only = [c for c in cards if {a["detector"] for a in c["alarms"]} == {"facets"}]

        # ---- cited steps against publisher positive steps (enacted cards that have them)
        def steps_for(sel: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {c["run_id"]: {"evidence_steps": sorted({s for a in c["alarms"] for s in a["steps"]})} for c in sel}

        enacted_cards = [c for c in cards if stratum[c["run_id"]] == "enacted_yes"]
        rows = sc._passage_rows(connection, labels, steps_for(enacted_cards))
        chance_exact, chance_w5 = [], []
        for r in rows:
            turns = [x[0] for x in connection.execute(
                "SELECT step_index FROM trace_steps WHERE trace_id = ? AND role = 'assistant' ORDER BY step_index", (r,))]
            ord_of = {s: i for i, s in enumerate(turns)}
            pos_turns = sorted(ord_of[s] for s in set(labels[r].positive_steps) if s in ord_of)
            cited_n = len({s for a in next(c for c in cards if c["run_id"] == r)["alarms"] for s in a["steps"]})
            window = {t for p in pos_turns for t in range(p - 5, p + 6) if 0 <= t < len(turns)}
            chance_exact.append(chance_hit(len(turns), len(pos_turns), cited_n))
            chance_w5.append(chance_hit(len(turns), len(window), cited_n))
        steps = {
            "enacted_cards": len(enacted_cards), "with_positive_steps_and_mappable": len(rows),
            "exact_hit": sup(sum(bool(v["exact"]) for v in rows.values())),
            "within5_hit": sup(sum(bool(v["within5"]) for v in rows.values())),
            "chance_expected_exact": round(sum(chance_exact), 2), "chance_expected_within5": round(sum(chance_w5), 2),
        }

    doc = {
        "stage": "B (labels joined, strict DEV only)",
        "cards": len(cards), "admitted_by_strict_cross_key": len(checked & ids),
        "label_status_of_cards": {k: sup(v) for k, v in sorted(by_stratum.items())},
        "labelled_universe_strict_dev": dict(sorted(uni_strata.items())),
        "labelled_universe_base_rate_enacted_over_enacted_plus_clean": ratio(pos, pos + neg),
        "alarm_coverage_in_labelled_cohort_batch_findings": cov,
        "precision_all_cards": prec(cards),
        "precision_facets_only_cards": prec(facets_only),
        "precision_by_detector_family": by_family,
        "precision_by_detector_reader": by_group,
        "cited_steps_vs_publisher_positive_steps": steps,
        "rule": {"claim_needs_labelled_non_considered_cards": MIN_CLAIM,
                 "worth_time_needs_wilson_lower_over_base_rate_pct": pct(pos, pos + neg)},
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "stage_b.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
