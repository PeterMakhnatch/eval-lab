#!/usr/bin/env python3
"""Reweight frozen DEV decisions and bootstrap corpus-stratified task clusters."""

import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(PARENT))
import analyze as a  # noqa: E402
import exact_matrix as x  # noqa: E402

SEED = 110408
DRAWS = 5000
PRIORS = (0.01, 0.05, 0.20)
SIGNALS = {n: a.ROOT / "signals" / f"{stem}.jsonl" for n, stem in x.STEMS.items()}
INPUTS = [
    Path(__file__).resolve(),
    HERE / "PROTOCOL.md",
    PARENT / "analyze.py",
    PARENT / "exact_matrix.py",
    a.ROOT / "runs.jsonl",
    *SIGNALS.values(),
    x.MATRIX,
    x.SOURCE / "score.py",
    x.SOURCE / "detbench_lib.py",
    a.BASE,
    PARENT / "aggregates.json",
    PARENT / "exact-matrix-aggregates.json",
]


def hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in INPUTS}


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    i = (len(values) - 1) * p
    lo, hi = int(i), math.ceil(i)
    return values[lo] + (values[hi] - values[lo]) * (i - lo)


def posterior(pos, neg, prior):
    denominator = prior * pos + (1 - prior) * neg
    return prior * pos / denominator if denominator else None


def main():
    before = hashes()
    (HERE / "input-hashes.json").write_text(json.dumps(before, indent=2, sort_keys=True) + "\n")
    key_re = re.compile(r'"run_key"\s*:\s*"([^"\\]+)"')
    runs = {}
    with (a.ROOT / "runs.jsonl").open() as f:
        for line in f:
            if not re.search(r'"split"\s*:\s*"dev"', line):
                continue
            m = key_re.search(line)
            if not m or m.group(1).split(":", 1)[0] not in a.SETS:
                continue
            r = json.loads(line)
            assert r["split"] == "dev" and r["set"] in a.SETS and r["cluster"]
            assert r["run_key"] not in runs and r["label"] in (0, 1)
            runs[r["run_key"]] = r
    assert len(runs) == 690 and Counter(r["label"] for r in runs.values()) == {0: 358, 1: 332}
    sigs = {}
    for name, path in SIGNALS.items():
        sigs[name] = {}
        with path.open() as f:
            for line in f:
                m = key_re.search(line)
                if m and m.group(1) in runs:
                    r = json.loads(line)
                    assert r["run_key"] not in sigs[name]
                    sigs[name][r["run_key"]] = r
    matrix = {
        r["signal"]: r for r in json.loads(x.MATRIX.read_text()) if r.get("signal") in a.COMBOS
    }
    assert set(matrix) == set(a.COMBOS)
    lookup = {
        (r["corpus"], r["run_id"]): r["decision"]
        for r in json.loads(a.BASE.read_text())["decisions"]
    }
    assert len(lookup) == len(runs)
    decisions = {
        "recorded_replay_keep": {
            k: lookup[(r["set"], k[len(r["set"]) + 1 :])] for k, r in runs.items()
        }
    }
    for name, (method, members) in a.COMBOS.items():
        entry = matrix[name]
        assert entry["method"] == method and entry["members"] == [x.STEMS[n] for n in members]
        combined = x.score.combine([sigs[n] for n in members], runs, method, names=entry["members"])
        threshold = entry["thresholds"]["0.05"]
        threshold = math.inf if threshold is None else threshold
        decisions[name] = {
            k: ("missing" if v["score"] is None else "flag" if v["score"] >= threshold else "keep")
            for k, v in combined.items()
        }
        assert set(decisions[name]) == set(runs)
    clusters = {s: defaultdict(list) for s in a.SETS}
    for k, r in runs.items():
        clusters[r["set"]][r["cluster"]].append(k)
    clusters = {s: dict(sorted(groups.items())) for s, groups in clusters.items()}
    old = json.loads((PARENT / "aggregates.json").read_text())
    exact = json.loads((PARENT / "exact-matrix-aggregates.json").read_text())
    blocks = {}
    counts = {}
    for name, dec in decisions.items():
        blocks[name] = {}
        for s in a.SETS:
            blocks[name][s] = [
                Counter((runs[k]["label"], dec[k]) for k in keys) for keys in clusters[s].values()
            ]
        counts[name] = {}
        for group in ("pooled",) + a.SETS:
            ss = a.SETS if group == "pooled" else (group,)
            c = sum((v for s in ss for v in blocks[name][s]), Counter())
            pos = sum(v for (label, _), v in c.items() if label == 1)
            neg = sum(v for (label, _), v in c.items() if label == 0)
            kept_p, kept_n = c[1, "keep"], c[0, "keep"]
            previous = (
                old[name][group]
                if name == "recorded_replay_keep"
                else exact["combinations"][name]["groups"][group]
            )
            assert (kept_p, kept_p + kept_n) == (
                previous["residual"]["n"],
                previous["residual"]["denominator"],
            )
            assert (kept_n, neg) == (
                previous["honest_retention_all"]["n"],
                previous["honest_retention_all"]["denominator"],
            )
            if name != "recorded_replay_keep":
                assert (
                    c[1, "missing"] == previous["unevaluable_cheat"]
                    and c[0, "missing"] == previous["unevaluable_honest"]
                )
            counts[name][group] = {
                "cheat": pos,
                "honest": neg,
                "cheat_keep": kept_p,
                "honest_keep": kept_n,
                "cheat_flag": c[1, "flag"] if name != "recorded_replay_keep" else None,
                "honest_flag": c[0, "flag"] if name != "recorded_replay_keep" else None,
                "cheat_missing": c[1, "missing"],
                "honest_missing": c[0, "missing"],
                "cheat_drop": c[1, "drop"],
                "honest_drop": c[0, "drop"],
                "cheat_review": c[1, "review"],
                "honest_review": c[0, "review"],
            }
        if name != "recorded_replay_keep":
            c = counts[name]["pooled"]
            parity = exact["combinations"][name]["parity"]
            for key, value in [
                ("tp", c["cheat_flag"]),
                ("fp", c["honest_flag"]),
                ("n_pos", c["cheat"]),
                ("n_neg", c["honest"]),
            ]:
                assert parity[key]["match"] and parity[key]["computed"] == value, (
                    name,
                    key,
                    value,
                    parity[key],
                )

    def metrics(c, combo):
        if not c["cheat"] or not c["honest"]:
            return None
        kp, kn = c["cheat_keep"] / c["cheat"], c["honest_keep"] / c["honest"]
        fp = c["honest_flag"] / c["honest"] if combo else None
        tp = c["cheat_flag"] / c["cheat"] if combo else None
        return {
            "keep_cheat": kp,
            "keep_honest": kn,
            "sensitivity": tp,
            "false_positive_rate": fp,
            "contamination": {str(p): posterior(kp, kn, p) for p in PRIORS},
            "precision": {str(p): posterior(tp, fp, p) for p in PRIORS} if combo else None,
        }

    point = {
        n: {
            g: {"counts": c, "rates": metrics(c, n != "recorded_replay_keep")}
            for g, c in groups.items()
        }
        for n, groups in counts.items()
    }
    values = {
        n: {kind: {str(p): [] for p in PRIORS} for kind in ("contamination", "precision")}
        for n in decisions
    }
    excluded = {
        n: {
            kind: {str(p): {"no_class": 0, "no_selected_outcome": 0} for p in PRIORS}
            for kind in ("contamination", "precision")
        }
        for n in decisions
    }
    rng = random.Random(SEED)
    for _ in range(DRAWS):
        indices = {s: [rng.randrange(len(clusters[s])) for _ in clusters[s]] for s in a.SETS}
        for name in decisions:
            c = sum((blocks[name][s][i] for s in a.SETS for i in indices[s]), Counter())
            pos = sum(v for (label, _), v in c.items() if label == 1)
            neg = sum(v for (label, _), v in c.items() if label == 0)
            for kind, selected in [("contamination", "keep"), ("precision", "flag")]:
                if kind == "precision" and name == "recorded_replay_keep":
                    continue
                for p in PRIORS:
                    key = str(p)
                    if not pos or not neg:
                        excluded[name][kind][key]["no_class"] += 1
                    elif not c[1, selected] and not c[0, selected]:
                        excluded[name][kind][key]["no_selected_outcome"] += 1
                    else:
                        value = posterior(c[1, selected] / pos, c[0, selected] / neg, p)
                        assert value is not None
                        values[name][kind][key].append(value)
    intervals = {}
    for name in decisions:
        intervals[name] = {}
        for kind in ("contamination", "precision"):
            if kind == "precision" and name == "recorded_replay_keep":
                intervals[name][kind] = None
                continue
            intervals[name][kind] = {}
            for p in PRIORS:
                key = str(p)
                v = values[name][kind][key]
                e = excluded[name][kind][key]
                assert len(v) + sum(e.values()) == DRAWS
                intervals[name][kind][key] = {
                    "percentile95": [percentile(v, 0.025), percentile(v, 0.975)] if v else None,
                    "valid_draws": len(v),
                    "excluded": e,
                }
    assert before == hashes(), "Input changed during computation"
    output = {
        "seed": SEED,
        "draws": DRAWS,
        "priors": PRIORS,
        "point_counts_match_existing_outputs": True,
        "input_hashes_verified_after": True,
        "cluster_counts": {s: len(clusters[s]) for s in a.SETS},
        "point": point,
        "pooled_bootstrap": intervals,
    }
    (HERE / "results.json").write_text(
        json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )

    def fmt(v):
        return "NA" if v is None else f"{100 * v:.2f}%"

    def cell(name, kind, p):
        v = point[name]["pooled"]["rates"][kind]
        estimate = v[str(p)] if v is not None else None
        b = intervals[name][kind]
        if b is None:
            return "NA"
        b = b[str(p)]
        ci = b["percentile95"]
        return f"{fmt(estimate)} [{fmt(ci[0])}, {fmt(ci[1])}]" if ci else f"{fmt(estimate)} [NA]"

    report = [
        "# Exploratory prevalence sensitivity",
        "",
        "This analysis was registered after X06 and review results were observed. It is not blind.",
        "These are reweighted in-sample DEV operating points, not production estimates or independent validation.",
        "",
        "| Method | Cheat keep / all | Honest keep / all | Sensitivity: cheat flag / all | FPR: honest flag / all | Missing cheat / honest |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in decisions:
        c = counts[name]["pooled"]

        def frac(n, d):
            return f"{n}/{d} ({fmt(n / d)})"

        sensitivity = frac(c["cheat_flag"], c["cheat"]) if c["cheat_flag"] is not None else "NA"
        fpr = frac(c["honest_flag"], c["honest"]) if c["honest_flag"] is not None else "NA"
        report.append(
            f"| {name} | {frac(c['cheat_keep'], c['cheat'])} | {frac(c['honest_keep'], c['honest'])} | {sensitivity} | {fpr} | {c['cheat_missing']} / {c['honest_missing']} |"
        )
    report += [
        "",
        "| Method | Outcome | 1% prior | 5% prior | 20% prior | Valid / excluded draws |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name in decisions:
        for kind in ("contamination", "precision"):
            b = intervals[name][kind]
            draws = (
                "NA"
                if b is None
                else f"{b['0.01']['valid_draws']} / {sum(b['0.01']['excluded'].values())}"
            )
            report.append(
                f"| {name} | {kind} | {cell(name, kind, 0.01)} | {cell(name, kind, 0.05)} | {cell(name, kind, 0.2)} | {draws} |"
            )
    report += [
        "",
        "Cells show point estimates and 95% percentile intervals. All rates use all runs in each class as denominators, including missing scores. Missing scores are neither kept nor flagged.",
        "Recorded replay review and drop are not combined as flags; recorded sensitivity, FPR and precision are not defined here. Review is not keep.",
        f"Recorded replay exclusions: {counts['recorded_replay_keep']['pooled']['cheat_drop']} cheat and {counts['recorded_replay_keep']['pooled']['honest_drop']} honest drops; {counts['recorded_replay_keep']['pooled']['cheat_review']} cheat and {counts['recorded_replay_keep']['pooled']['honest_review']} honest reviews.",
        "The bootstrap draws 5,000 samples of task clusters with replacement within each corpus, with seed 110408. Pooled counts preserve corpus strata. Cluster counts: "
        + ", ".join(f"{s} {len(clusters[s])}" for s in a.SETS)
        + ".",
        "Each interval excludes draws without either class or without the relevant selected outcome. Exclusion reasons and per-corpus point counts are in results.json. Thresholds and scores stay fixed.",
        "The analysis assumes the class-conditional rates transfer to each hypothetical prior. Few clusters, missing scores, corpus selection, and DEV-based rule selection limit inference. The bootstrap excludes threshold and selection uncertainty.",
        "",
        "Reproduce: `PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/ov1104/wave2-detbench/workers/x06/prevalence/prevalence.py`.",
        "input-hashes.json records code, protocol and input bytes before scoring; results.json records post-computation verification and unrounded values.",
    ]
    (HERE / "REPORT.md").write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
