"""Agreement of zero-shot Kev-4B with frozen two-rater labels, per rater field.

Pre-registered before any Kev output existed (see README "Plan"): the cards in
``questions_trace.json`` / ``questions_window.json`` and every metric below are
fixed; no threshold is tuned on these labels.

Reference = rater consensus (both raters gave the same value); rows where the
raters disagree are excluded and counted. Inter-rater agreement over all rows
is the ceiling, the majority class the floor.

- Choice fields on the digest (``stop_reason``, ``blame``, ``loop_kind``):
  argmax accuracy, Cohen's kappa, and accuracy/coverage where Kev's top
  probability is >= 0.8. ``stop_reason`` and ``blame`` also get a rule baseline
  from the same run facts.
- ``loop_kind`` from windows (threshold-free): AUROC of loop-vs-none with
  max(repetition, completion-claim) over windows, and of repetition-vs-claim
  among looping runs with max(repetition) - max(claim).
- ``pass_copied`` / ``sft_clean`` (passes only): AUROC and accuracy at 0.5 of
  the digest noul; for ``pass_copied`` also AUROC of max-window ``upstream_fetch``.
- ``first_failure`` location: the argmax ``first_failure_here`` window, and the
  earliest window with P >= 0.5, hit when the window contains either rater's
  ref. Baselines: first window, last window, uniform-random window; ceiling:
  share of runs whose two rater refs fall in one common window.

Usage (worktree root):
  uv run python research/experiments/kev-rater-fields/analyze.py \
      --digest-scores derived/kev-rater-fields/digest.scores.jsonl \
      --window-scores derived/kev-rater-fields/windows.scores.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
DERIVED = WORKTREE / "derived/kev-rater-fields"
CONF = 0.8
CHOICE_FIELDS = ("stop_reason", "blame", "loop_kind")
INFRA_SAFE = {None, "TrialBudgetExhaustedError", "LoopBreakStop", "AgentTimeoutError"}


def read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


def auroc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def frac(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None}


def rule_stop(f: dict) -> str:
    exc = f.get("exception_type")
    if exc is None:
        return "model_finished"
    if "LoopBreak" in exc:
        return "loop_break"
    if exc == "TrialBudgetExhaustedError":
        return "request_ceiling" if (f.get("model_calls") or 0) >= 115 else "token_ceiling"
    if exc == "AgentTimeoutError":
        return "agent_timeout"
    return "infra_error"


def rule_blame(f: dict) -> str:
    if f.get("exception_type") not in INFRA_SAFE:
        return "infra"
    return "none" if f.get("reward") == 1.0 else "model"


def choice_field(cohort, dig, field):
    rows = [c for c in cohort if c["consensus"][field] is not None]
    pairs, conf_hits, conf_n = [], 0, 0
    for c in rows:
        probs = dig[c["id"]][field]["probabilities"]
        pred = max(probs, key=probs.get)
        pairs.append((pred, c["consensus"][field]))
        if probs[pred] >= CONF:
            conf_n += 1
            conf_hits += pred == c["consensus"][field]
    gold = Counter(g for _, g in pairs)
    inter = [(c["raters"][field]["rater_a"], c["raters"][field]["rater_b"]) for c in cohort]
    out = {
        "n_consensus": len(rows),
        "n_rater_disagree": len(cohort) - len(rows),
        "gold": dict(gold.most_common()),
        "pred": dict(Counter(p for p, _ in pairs).most_common()),
        "kev_accuracy": frac(sum(p == g for p, g in pairs), len(pairs)),
        "kev_kappa": kappa(pairs),
        f"kev_at_p{CONF}": {**frac(conf_hits, conf_n), "coverage": round(conf_n / len(rows), 3)},
        "majority_baseline": frac(gold.most_common(1)[0][1], len(pairs)),
        "inter_rater": {**frac(sum(a == b for a, b in inter), len(inter)), "kappa": kappa(inter)},
        "confusion": dict(Counter(f"{g} -> {p}" for p, g in pairs if p != g).most_common()),
    }
    rule = {"stop_reason": rule_stop, "blame": rule_blame}.get(field)
    if rule:
        rp = [(rule(c["facts"]), c["consensus"][field]) for c in rows]
        out["rule_baseline"] = {**frac(sum(p == g for p, g in rp), len(rp)), "kappa": kappa(rp)}
    return out


def noul_field(cohort, dig, win_max, field, window_q=None):
    rows = [c for c in cohort if c["consensus"][field] is not None]
    pos = [c for c in rows if c["consensus"][field]]
    neg = [c for c in rows if not c["consensus"][field]]
    p = {c["id"]: dig[c["id"]][field]["noul"] for c in rows}
    out = {
        "n_consensus": len(rows),
        "n_rater_disagree": sum(
            1 for c in cohort if c["raters"][field]["rater_a"] != c["raters"][field]["rater_b"]
        ),
        "positives": len(pos),
        "negatives": len(neg),
        "digest_auroc": auroc([p[c["id"]] for c in pos], [p[c["id"]] for c in neg]),
        "digest_accuracy_at_0.5": frac(
            sum((p[c["id"]] >= 0.5) == bool(c["consensus"][field]) for c in rows), len(rows)
        ),
        "majority_baseline": frac(max(len(pos), len(neg)), len(rows)),
    }
    if window_q:
        have = [c for c in rows if c["id"] in win_max]
        out[f"window_max_{window_q}_auroc"] = auroc(
            [win_max[c["id"]][window_q] for c in have if c["consensus"][field]],
            [win_max[c["id"]][window_q] for c in have if not c["consensus"][field]],
        )
    return out


def loop_from_windows(cohort, win_max):
    rows = [c for c in cohort if c["consensus"]["loop_kind"] is not None and c["id"] in win_max]

    def loop(c):
        return max(win_max[c["id"]]["repetition_here"], win_max[c["id"]]["completion_claim_here"])

    looped = [c for c in rows if c["consensus"]["loop_kind"] != "none"]
    calm = [c for c in rows if c["consensus"]["loop_kind"] == "none"]

    def margin(c):
        return win_max[c["id"]]["repetition_here"] - win_max[c["id"]]["completion_claim_here"]

    return {
        "n": len(rows),
        "loop_vs_none_auroc": auroc([loop(c) for c in looped], [loop(c) for c in calm]),
        # a max over windows rises with run length, and loops make runs long
        "loop_vs_none_auroc_run_length_baseline": auroc(
            [c["n_steps"] for c in looped], [c["n_steps"] for c in calm]
        ),
        "repetition_vs_claim_auroc": auroc(
            [margin(c) for c in looped if c["consensus"]["loop_kind"] == "repetition"],
            [margin(c) for c in looped if c["consensus"]["loop_kind"] == "completion-claim"],
        ),
    }


def first_failure(cohort, windows):
    by_run = defaultdict(list)
    for w in windows:
        by_run[w["run_id"]].append(w)
    hits = Counter()
    n = 0
    random_expect = 0.0
    ceiling = 0
    two_refs = 0
    for c in cohort:
        refs = {r for r in c["first_failure_refs"]}
        ws = sorted(by_run.get(c["id"], []), key=lambda w: w["window_index"])
        if not refs or not ws:
            continue
        n += 1
        good = [any(r in w["refs"] for r in refs) for w in ws]
        p = [w["p"] for w in ws]
        best = max(range(len(ws)), key=lambda i: (p[i], -i))
        early = next((i for i in range(len(ws)) if p[i] >= 0.5), best)
        hits["kev_argmax"] += good[best]
        hits["kev_earliest_p0.5"] += good[early]
        hits["first_window"] += good[0]
        hits["last_window"] += good[-1]
        random_expect += sum(good) / len(ws)
        ra, rb = (c["raters"]["first_failure_ref"][k] for k in ("rater_a", "rater_b"))
        if ra and rb:
            two_refs += 1
            ceiling += any(ra in w["refs"] and rb in w["refs"] for w in ws)
    out = {k: frac(v, n) for k, v in sorted(hits.items())}
    out["uniform_random_window"] = {"expected": round(random_expect / n, 3) if n else None, "n": n}
    out["raters_share_a_window"] = frac(ceiling, two_refs)
    return out


def loop_kind_posthoc(cohort, dig):
    """Checks added after seeing the scores (not part of the plan)."""
    pred = {
        c["id"]: max(
            dig[c["id"]]["loop_kind"]["probabilities"],
            key=dig[c["id"]]["loop_kind"]["probabilities"].get,
        )
        for c in cohort
    }
    top = {c["id"]: max(dig[c["id"]]["loop_kind"]["probabilities"].values()) for c in cohort}
    rows = [c for c in cohort if c["consensus"]["loop_kind"] is not None]
    def ok(c):
        return pred[c["id"]] == c["consensus"]["loop_kind"]

    # a loop-break exception message names "repetition"; is Kev just reading it?
    no_lb = [c for c in rows if "LoopBreak" not in (c["facts"]["exception_type"] or "")]
    ranked = sorted(rows, key=lambda c: top[c["id"]])
    thirds = [ranked[i * len(ranked) // 3 : (i + 1) * len(ranked) // 3] for i in range(3)]
    return {
        "note": "post hoc: chosen after the pre-registered results were known",
        "accuracy_without_loop_break_exception": frac(sum(map(ok, no_lb)), len(no_lb)),
        "kev_vs_each_rater_all_runs": {
            r: frac(sum(pred[c["id"]] == c["raters"]["loop_kind"][r] for c in cohort), len(cohort))
            for r in ("rater_a", "rater_b")
        },
        "accuracy_by_top_probability_third": [
            {"min_top_p": round(top[t[0]["id"]], 3), **frac(sum(map(ok, t)), len(t))}
            for t in thirds
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest-scores", type=Path, default=DERIVED / "digest.scores.jsonl")
    ap.add_argument("--window-scores", type=Path, default=DERIVED / "windows.scores.jsonl")
    ap.add_argument("--out", type=Path, default=HERE / "results/agreement.json")
    a = ap.parse_args()
    cohort = read(DERIVED / "cohort.jsonl")
    dig_rows = read(a.digest_scores)
    errors = [r for r in dig_rows if "answers" not in r]
    dig = {r["chunk_id"]: r["answers"] for r in dig_rows if "answers" in r}
    win_rows = read(a.window_scores)
    win_err = [r for r in win_rows if "answers" not in r]
    wscore = {r["chunk_id"]: r["answers"] for r in win_rows if "answers" in r}
    windows = read(DERIVED / "windows.jsonl")
    missing = [c["id"] for c in cohort if c["id"] not in dig]
    if missing or errors or win_err or any(w["chunk_id"] not in wscore for w in windows):
        raise SystemExit(
            f"incomplete scores: {len(missing)} digests missing, {len(errors)} digest errors, "
            f"{len(win_err)} window errors"
        )
    win_max: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for w in windows:
        ans = wscore[w["chunk_id"]]
        w["p"] = ans["first_failure_here"]["noul"]
        for q in ("repetition_here", "completion_claim_here", "upstream_fetch"):
            win_max[w["run_id"]][q] = max(win_max[w["run_id"]][q], ans[q]["noul"])
    res = {
        "schema": "kev-rater-fields.agreement/v1",
        "model": "jaredpalmer/kev-4b@v1.0",
        "n_runs": len(cohort),
        "n_windows": len(windows),
        "confidence_cut": CONF,
        "choice_fields": {f: choice_field(cohort, dig, f) for f in CHOICE_FIELDS},
        "loop_kind_from_windows": loop_from_windows(cohort, win_max),
        "pass_copied": noul_field(cohort, dig, win_max, "pass_copied", "upstream_fetch"),
        "sft_clean": noul_field(cohort, dig, win_max, "sft_clean"),
        "first_failure_location": first_failure(cohort, windows),
        "loop_kind_posthoc": loop_kind_posthoc(cohort, dig),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
