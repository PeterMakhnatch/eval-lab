"""Tune the combined cheat detector: per-source OR-of-components thresholds.

Components per row: each ``rules`` sub-rule (parsed from the verdict
explanation), each ``content_rules`` rule, each cached ``kev_*`` score, and
Sol (numeric P(cheat), only where judged; unjudged rows count as not flagged
by Sol since Sol runs as a cascade on rule-negative rows only).

Per source the script greedily builds an interpretable OR of at most four
literals (binary flags or ``score > cut``) maximizing recall subject to
FPR <= target on the SELECTION rows' negatives. Sources with fewer than 50
negatives borrow their GROUP-pooled rows for selection; reporting stays on
the source's own rows. Selection runs inside a seeded
2-fold split (``sha256("combined-v1:fold:<seed>:<id>")``); published numbers
are OUT-OF-FOLD with Wilson 95% CIs, plus in-sample. The deployable rule fit
on all rows goes to ``thresholds.json``; with ``--write-cache`` the OOF
decisions are stored as ``cache/combined_v1`` verdicts so
``evallab detectors score --detector combined_v1`` reports held-out numbers.

Sources: ours, cb, tw, aria, olmo, ht, contamination-audit tuned separately;
tw-raw is reported with the tw thresholds but never used for tuning. ``g6``
has no ``reward_hacking`` labels. cb ``attempt`` rows (null label) are excluded.
Usage (from the worktree root)::

    uv run python research/experiments/combined-detector/tune.py
    uv run python research/experiments/combined-detector/tune.py --write-cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

FOLD_SEED = "20261013"
MAX_LITERALS = 4
FPR_TARGETS = (0.05, 0.02)
TUNED_SOURCES = ("ours", "cb", "tw", "aria", "olmo", "ht", "contamination-audit")
KEV_DETECTORS = (
    "kev_any_cheat",
    "kev_router",
    "kev_stacked",
    "kev_ft1_any_cheat",
    "kev_grader_tamper",
    "kev_hollow_solution",
    "kev_max_pattern",
)
#: Tie-break rank when two literals give the same recall (cheaper first).
_LITERAL_RANK = {"rules": 0, "content": 1, "kev": 2, "sol": 3}

_GROUP_OF = {
    "ours": "natural-agentic",
    "cb": "natural-agentic",
    "contamination-audit": "natural-agentic",
    "tw": "prompted-agentic",
    "aria": "real-RL-single-turn",
    "olmo": "real-RL-single-turn",
    "ht": "prompted-single-turn",
}


def wilson(hit: int, n: int, z: float = 1.96) -> dict[str, float | None]:
    """Wilson 95% CI for a binomial rate."""
    if not n:
        return {"rate": None, "lo": None, "hi": None}
    p = hit / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"rate": p, "lo": max(0.0, center - half), "hi": min(1.0, center + half)}


def _parse_fired(explanation: str) -> list[str]:
    if not explanation.startswith("fired:"):
        return []
    body = explanation[len("fired:") :].strip()
    return [] if body == "none" else [part.strip() for part in body.split(",") if part.strip()]


def fold_of(row_id: str) -> int:
    """Seeded 2-fold assignment, stable across interim and final tuning."""
    digest = hashlib.sha256(f"combined-v1:fold:{FOLD_SEED}:{row_id}".encode()).hexdigest()
    return int(digest, 16) % 2


def load_features(
    root: Path,
) -> tuple[list[dict], dict[str, dict[str, float | bool | None]]]:
    """Manifest rows (labelled ``reward_hacking``) and per-row components."""
    from evallab.detectors import Corpus

    corpus = Corpus.load(root / "research" / "calibration" / "detectors-v1")
    cached_rules = corpus.cached("rules")
    cached_content = corpus.cached("content_rules")
    cached_kev = {det: corpus.cached(det) for det in KEV_DETECTORS}
    cached_sol = corpus.cached("sol_codex")
    rows: list[dict] = []
    features: dict[str, dict[str, float | bool | None]] = {}
    for row in corpus.rows:
        label = row["labels"].get("reward_hacking")
        if label is None or row["source"] not in (*TUNED_SOURCES, "tw-raw"):
            continue
        rows.append(row)
        feat: dict[str, float | bool | None] = {}
        for sub in _parse_fired((cached_rules.get(row["id"]) or {}).get("explanation") or ""):
            feat[f"rules:{sub}"] = True
        for sub in _parse_fired((cached_content.get(row["id"]) or {}).get("explanation") or ""):
            feat[f"content:{sub}"] = True
        for det in KEV_DETECTORS:
            score = (cached_kev[det].get(row["id"]) or {}).get("score")
            if isinstance(score, int | float):
                feat[f"kev:{det}"] = float(score)
        sol = (cached_sol.get(row["id"]) or {}).get("score")
        if isinstance(sol, int | float):
            feat["sol:sol_codex"] = float(sol)
        features[row["id"]] = feat
    return rows, features


def _fires(literal: tuple, feat: dict) -> bool:
    if literal[0] == "flag":
        return feat.get(literal[1]) is True
    value = feat.get(literal[1])
    return isinstance(value, float) and value > literal[2]


def _rank(literal: tuple) -> int:
    kind = literal[1].split(":")[0]
    return _LITERAL_RANK.get(kind, 9)


def select(
    train_ids: list[str],
    labels: dict[str, bool],
    features: dict,
    target_fpr: float,
) -> list[tuple]:
    """Greedy OR: max train recall with train FPR <= target (<=4 literals)."""
    pos = {i for i in train_ids if labels[i]}
    neg = {i for i in train_ids if not labels[i]}
    if not pos or not neg:
        return []
    binary = sorted({name for i in train_ids for name, val in features[i].items() if val is True})
    numeric = sorted(
        {name for i in train_ids for name, val in features[i].items() if isinstance(val, float)}
    )
    chosen: list[tuple] = []
    covered: set[str] = set()
    covered_neg: set[str] = set()
    current_recall = 0.0
    for _ in range(MAX_LITERALS):
        best: tuple | None = None
        best_key: tuple | None = None
        for name in binary:
            literal = ("flag", name)
            if literal in chosen:
                continue
            hit_p = sum(1 for i in pos if i not in covered and features[i].get(name) is True)
            hit_n = sum(1 for i in neg if i not in covered_neg and features[i].get(name) is True)
            best, best_key = _consider(
                best, best_key, literal, hit_p, hit_n, covered, covered_neg, pos, neg, target_fpr
            )
        for name in numeric:
            scored = sorted(
                (
                    (features[i][name], i)
                    for i in train_ids
                    if isinstance(features[i].get(name), float)
                ),
                reverse=True,
            )
            gain_p = gain_n = 0
            cursor = 0
            while cursor < len(scored):
                cut = scored[cursor][0]
                best, best_key = _consider(
                    best,
                    best_key,
                    ("score", name, cut),
                    gain_p,
                    gain_n,
                    covered,
                    covered_neg,
                    pos,
                    neg,
                    target_fpr,
                )
                while cursor < len(scored) and scored[cursor][0] == cut:
                    _, row_id = scored[cursor]
                    if row_id in pos and row_id not in covered:
                        gain_p += 1
                    elif row_id in neg and row_id not in covered_neg:
                        gain_n += 1
                    cursor += 1
        if best is None or (best_key is not None and best_key[0] <= current_recall + 1e-12):
            break
        chosen.append(best)
        assert best is not None
        covered |= {i for i in pos if _fires(best, features[i])}
        covered_neg |= {i for i in neg if _fires(best, features[i])}
        current_recall = len(covered) / len(pos)
    return chosen


def _consider(
    best: tuple | None,
    best_key: tuple | None,
    literal: tuple,
    hit_p: int,
    hit_n: int,
    covered: set[str],
    covered_neg: set[str],
    pos: set[str],
    neg: set[str],
    target_fpr: float,
) -> tuple[tuple | None, tuple | None]:
    fpr = (len(covered_neg) + hit_n) / len(neg)
    if fpr > target_fpr + 1e-12:
        return best, best_key
    recall = (len(covered) + hit_p) / len(pos)
    key = (recall, -hit_n, -_rank(literal), str(literal))
    if best_key is None or key > best_key:
        return literal, key
    return best, best_key


def decide(chosen: list[tuple], feat: dict) -> tuple[bool, list[str]]:
    """Flag + the literals that fired (for ``fired_components``)."""
    fired = [
        f"{lit[1]}>{lit[2]:g}" if lit[0] == "score" else lit[1]
        for lit in chosen
        if _fires(lit, feat)
    ]
    return bool(fired), fired


def _rate_block(ids: list[str], flags: dict[str, bool], labels: dict[str, bool]) -> dict:
    pos = [i for i in ids if labels[i]]
    neg = [i for i in ids if not labels[i]]
    rec = wilson(sum(1 for i in pos if flags.get(i)), len(pos))
    fpr = wilson(sum(1 for i in neg if flags.get(i)), len(neg))
    return {
        "positives": len(pos),
        "negatives": len(neg),
        "recall": rec,
        "fpr": fpr,
    }


def cheat_type(row: dict) -> str:
    """Per-cheat-type key: tw families; cb mechanism; rl/ours families."""
    if row["source"] == "cb":
        return str((row.get("cheatbench") or {}).get("mechanism") or "none")
    families = row.get("families") or []
    return "+".join(sorted(families)) if families else "none"


MIN_NEGATIVES_PER_SOURCE = 50


def tune_source(
    source: str,
    rows: list[dict],
    labels: dict[str, bool],
    features: dict,
    train_rows: list[dict] | None = None,
) -> dict:
    """OOF + in-sample operating points at 5% and 2%, plus per-type recall.

    Selection runs on ``train_rows`` (defaults to ``rows``). Sources with
    fewer than ``MIN_NEGATIVES_PER_SOURCE`` negatives borrow their group's
    pooled rows for selection; reporting stays on the source's own rows.
    """
    ids = [r["id"] for r in rows]
    train_ids = [r["id"] for r in train_rows] if train_rows is not None else ids
    borrowed = train_rows is not None
    by_fold = {
        0: [i for i in train_ids if fold_of(i) == 0],
        1: [i for i in train_ids if fold_of(i) == 1],
    }
    out: dict = {
        "source": source,
        "group": _GROUP_OF[source],
        "positives": sum(1 for i in ids if labels[i]),
        "negatives": sum(1 for i in ids if not labels[i]),
        "borrowed_selection": borrowed,
        "operating_points": {},
        "per_type": {},
    }
    oof_flags: dict[str, dict[str, bool]] = {}
    for target in FPR_TARGETS:
        key = f"fpr<={target:.0%}"
        fold_rules = {f: select(by_fold[1 - f], labels, features, target) for f in (0, 1)}
        flags = {i: decide(fold_rules[fold_of(i)], features[i])[0] for i in ids}
        fired_map = {i: decide(fold_rules[fold_of(i)], features[i])[1] for i in ids}
        oof_flags[key] = flags
        full_rule = select(train_ids, labels, features, target)
        in_flags = {i: decide(full_rule, features[i])[0] for i in ids}
        out["operating_points"][key] = {
            "target": target,
            "components": [
                (f"{lit[1]}>{lit[2]:g}" if lit[0] == "score" else lit[1]) for lit in full_rule
            ],
            "oof": _rate_block(ids, flags, labels),
            "insample": _rate_block(ids, in_flags, labels),
            "deployable": [list(lit) for lit in full_rule],
        }
        out[f"oof_fired_{key}"] = fired_map
    for ctype in sorted({cheat_type(r) for r in rows if labels[r["id"]]}):
        members = [r["id"] for r in rows if labels[r["id"]] and cheat_type(r) == ctype]
        out["per_type"][ctype] = {
            "n": len(members),
            "oof_recall_5": wilson(
                sum(1 for i in members if oof_flags["fpr<=5%"].get(i)), len(members)
            ),
        }
    sol_judged = [i for i in ids if isinstance(features[i].get("sol:sol_codex"), float)]
    out["sol_coverage"] = {
        "judged": len(sol_judged),
        "judged_positives": sum(1 for i in sol_judged if labels[i]),
        "judged_negatives": sum(1 for i in sol_judged if not labels[i]),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-cache", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    rows, features = load_features(root)
    labels = {r["id"]: bool(r["labels"]["reward_hacking"]) for r in rows}
    by_source = {s: [r for r in rows if r["source"] == s] for s in (*TUNED_SOURCES, "tw-raw")}

    results: dict = {
        "fold_seed": FOLD_SEED,
        "max_literals": MAX_LITERALS,
        "sources": {},
        "groups": {},
        "transfer": {},
        "excluded": {
            "cb_attempt_null_label": sum(
                1
                for r in _all_rows(root)
                if r["source"] == "cb" and r["labels"].get("reward_hacking") is None
            ),
            "tw_raw_never_tuned": True,
        },
    }
    for source in TUNED_SOURCES:
        mine = by_source[source]
        n_neg = sum(1 for r in mine if not labels[r["id"]])
        pool: list[dict] | None = None
        if n_neg < MIN_NEGATIVES_PER_SOURCE:
            pool = [r for r in rows if _GROUP_OF.get(r["source"]) == _GROUP_OF[source]]
        results["sources"][source] = tune_source(source, mine, labels, features, train_rows=pool)

    # tw-raw diagnostic: tw thresholds applied, never tuned.
    tw_rule = [
        tuple(lit) for lit in results["sources"]["tw"]["operating_points"]["fpr<=5%"]["deployable"]
    ]
    raw_rows = by_source["tw-raw"]
    raw_ids = [r["id"] for r in raw_rows]
    raw_flags = {i: decide(tw_rule, features[i])[0] for i in raw_ids}
    results["sources"]["tw-raw"] = {
        "source": "tw-raw",
        "note": "label-leakage diagnostic: tw 5%-thresholds applied, never tuned here",
        "positives": sum(1 for i in raw_ids if labels[i]),
        "negatives": sum(1 for i in raw_ids if not labels[i]),
        "oof": _rate_block(raw_ids, raw_flags, labels),
    }

    # Group pools (OOF flags at 5%): prompted vs real-RL apart.
    oof5 = {
        i: results["sources"][r["source"]]["oof_fired_fpr<=5%"][i]
        for r in rows
        if r["source"] in TUNED_SOURCES
        for i in [r["id"]]
    }
    for group in sorted(set(_GROUP_OF.values())):
        members = [r["id"] for r in rows if _GROUP_OF.get(r["source"]) == group]
        results["groups"][group] = _rate_block(members, oof5, labels)

    # Transfer diagnostic (in-sample fits): ht <-> pooled aria+olmo.
    ht_ids = [r["id"] for r in by_source["ht"]]
    rl_ids = [r["id"] for r in by_source["aria"] + by_source["olmo"]]
    ht_rule = [
        tuple(lit) for lit in results["sources"]["ht"]["operating_points"]["fpr<=5%"]["deployable"]
    ]
    rl_pool_rule = select(rl_ids, labels, features, 0.05)
    results["transfer"] = {
        "ht_rule_on_aria_olmo": _rate_block(
            rl_ids, {i: decide(ht_rule, features[i])[0] for i in rl_ids}, labels
        ),
        "aria_olmo_rule_on_ht": _rate_block(
            ht_ids,
            {i: decide([tuple(lit) for lit in rl_pool_rule], features[i])[0] for i in ht_ids},
            labels,
        ),
        "aria_olmo_pooled_components": [
            (f"{lit[1]}>{lit[2]:g}" if lit[0] == "score" else lit[1]) for lit in rl_pool_rule
        ],
    }

    out_dir = root / "research" / "experiments" / "combined-detector"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")

    combined_rows = []
    for source in TUNED_SOURCES:
        fired_map = results["sources"][source]["oof_fired_fpr<=5%"]
        for r in by_source[source]:
            combined_rows.append(
                {
                    "id": r["id"],
                    "source": source,
                    "label": labels[r["id"]],
                    "cheat_type": cheat_type(r),
                    "flagged": oof5[r["id"]],
                    "fired_components": fired_map[r["id"]],
                    "fold": fold_of(r["id"]),
                }
            )
    with (out_dir / "combined_rows.jsonl").open("w") as handle:
        for entry in combined_rows:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    thresholds = {
        "fold_seed": FOLD_SEED,
        "selection_rule": (
            "per-source 2-fold greedy OR (<=4 literals) maximizing recall at "
            f"train FPR<=target; sources with fewer than {MIN_NEGATIVES_PER_SOURCE} "
            "negatives borrow their GROUP-pooled rows for selection "
            "(components, cuts, deployable) while reporting stays on the "
            "source's own rows"
        ),
        "borrowed": {
            source: results["sources"][source]["group"]
            for source in TUNED_SOURCES
            if results["sources"][source].get("borrowed_selection")
        },
        "group_default": {
            "agentic": "cb",
            "single_turn_rl": "pooled_aria_olmo",
        },
        "per_source": {
            source: {
                key: results["sources"][source]["operating_points"][key]["deployable"]
                for key in ("fpr<=5%", "fpr<=2%")
            }
            for source in TUNED_SOURCES
        },
        "pooled_aria_olmo": [list(lit) for lit in rl_pool_rule],
    }
    (out_dir / "thresholds.json").write_text(
        json.dumps(thresholds, indent=1, sort_keys=True) + "\n"
    )

    # Mirror for worker B.
    mirror = Path.home() / "Developer/eval-lab/derived/detector-roots/combined_results.json"
    mirror.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
    mirror_rows = Path.home() / "Developer/eval-lab/derived/detector-roots/combined_rows.jsonl"
    with mirror_rows.open("w") as handle:
        for entry in combined_rows:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    if args.write_cache:
        from evallab.detectors import Corpus

        corpus = Corpus.load(root / "research" / "calibration" / "detectors-v1")
        for entry in combined_rows:
            corpus.store(
                "combined_v1",
                {
                    "id": entry["id"],
                    "detector": "combined_v1",
                    "flagged": entry["flagged"],
                    "score": None,
                    "explanation": "oof-5%: " + (", ".join(entry["fired_components"]) or "none"),
                    "cost_usd": 0.0,
                    "fold": entry["fold"],
                },
            )
        corpus.seal()

    for source in (*TUNED_SOURCES, "tw-raw"):
        block = results["sources"][source]
        point = block.get("operating_points", {}).get("fpr<=5%", block.get("oof", {}))
        oof = point.get("oof", point) if isinstance(point, dict) else {}
        print(
            f"{source}: {block.get('positives', '?')}pos/{block.get('negatives', '?')}neg "
            f"oof-recall={_pct(oof.get('recall'))} oof-fpr={_pct(oof.get('fpr'))} "
            f"comps={point.get('components') if isinstance(point, dict) else '?'}"
        )
    return 0


def _pct(block: dict | None) -> str:
    if not block or block.get("rate") is None:
        return "-"
    return f"{block['rate']:.0%}[{block['lo']:.0%}-{block['hi']:.0%}]"


def _all_rows(root: Path) -> list[dict]:
    from evallab.detectors import Corpus

    return Corpus.load(root / "research" / "calibration" / "detectors-v1").rows


if __name__ == "__main__":
    raise SystemExit(main())
