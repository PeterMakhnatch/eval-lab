#!/usr/bin/env python3
import hashlib
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(PARENT))
import analyze as a  # noqa: E402
import exact_matrix as x  # noqa: E402

SEED, REPLICATES = 110406, 5000
SIGNALS = {n: a.ROOT / "signals" / f"{stem}.jsonl" for n, stem in x.STEMS.items()}
INPUTS = [
    HERE / "METHOD.md",
    Path(__file__).resolve(),
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
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo = int(index)
    hi = math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def main():
    before = hashes()
    (HERE / "input-hashes.json").write_text(json.dumps(before, indent=2, sort_keys=True) + "\n")
    runs = {}
    key_re = re.compile(r'"run_key"\s*:\s*"([^"\\]+)"')
    with (a.ROOT / "runs.jsonl").open() as f:
        for line in f:
            if not re.search(r'"split"\s*:\s*"dev"', line):
                continue
            m = key_re.search(line)
            if not m or m.group(1).split(":", 1)[0] not in a.SETS:
                continue
            r = json.loads(line)
            assert r["split"] == "dev" and r["set"] in a.SETS and r["cluster"]
            assert r["run_key"] not in runs
            runs[r["run_key"]] = r
    assert len(runs) == 690
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
    baseline = json.loads(a.BASE.read_text())["decisions"]
    lookup = {(r["corpus"], r["run_id"]): r["decision"] for r in baseline}
    assert len(lookup) == len(runs)
    decisions = {
        "recorded_replay_keep": {
            k: lookup[(r["set"], k[len(r["set"]) + 1 :])] == "keep" for k, r in runs.items()
        }
    }
    for name, (method, members) in a.COMBOS.items():
        entry = matrix[name]
        assert entry["method"] == method and entry["members"] == [x.STEMS[n] for n in members]
        combined = x.score.combine([sigs[n] for n in members], runs, method, names=entry["members"])
        threshold = entry["thresholds"]["0.05"]
        threshold = math.inf if threshold is None else threshold
        decisions[name] = {
            k: v["score"] is not None and v["score"] < threshold for k, v in combined.items()
        }
    clusters = {s: defaultdict(list) for s in a.SETS}
    for k, r in runs.items():
        clusters[r["set"]][r["cluster"]].append(k)
    for s in a.SETS:
        clusters[s] = dict(sorted(clusters[s].items()))
    groups = ("pooled",) + a.SETS
    original = json.loads((PARENT / "aggregates.json").read_text())
    exact = json.loads((PARENT / "exact-matrix-aggregates.json").read_text())
    block = {}
    result = {}
    for name, dec in decisions.items():
        block[name] = {}
        result[name] = {}
        for s in a.SETS:
            block[name][s] = []
            for keys in clusters[s].values():
                kept = [k for k in keys if dec.get(k, False)]
                block[name][s].append((sum(runs[k]["label"] == 1 for k in kept), len(kept)))
        for group in groups:
            ss = a.SETS if group == "pooled" else (group,)
            count = sum(v[0] for s in ss for v in block[name][s])
            denom = sum(v[1] for s in ss for v in block[name][s])
            previous = (
                original[name][group]["residual"]
                if name == "recorded_replay_keep"
                else exact["combinations"][name]["groups"][group]["residual"]
            )
            assert (count, denom) == (previous["n"], previous["denominator"]), (
                name,
                group,
                count,
                denom,
                previous,
            )
            result[name][group] = {
                "cheats": count,
                "kept": denom,
                "fraction": count / denom if denom else None,
                "wilson95": a.wilson(count, denom),
                "task_clusters": sum(len(clusters[s]) for s in ss),
                "valid_draws": 0,
                "excluded_zero_kept": 0,
                "bootstrap_percentile95": None,
            }
    draws = {name: {group: [] for group in groups} for name in decisions}
    rng = random.Random(SEED)
    for _ in range(REPLICATES):
        indices = {s: [rng.randrange(len(clusters[s])) for _ in clusters[s]] for s in a.SETS}
        for name in decisions:
            pooled_c = pooled_n = 0
            for s in a.SETS:
                c = sum(block[name][s][i][0] for i in indices[s])
                n = sum(block[name][s][i][1] for i in indices[s])
                pooled_c += c
                pooled_n += n
                if n:
                    draws[name][s].append(c / n)
            if pooled_n:
                draws[name]["pooled"].append(pooled_c / pooled_n)
    for name in decisions:
        for group in groups:
            values = draws[name][group]
            r = result[name][group]
            r["valid_draws"] = len(values)
            r["excluded_zero_kept"] = REPLICATES - len(values)
            if values:
                r["bootstrap_percentile95"] = [percentile(values, 0.025), percentile(values, 0.975)]
    assert before == hashes(), "Input changed during computation"
    output = {
        "seed": SEED,
        "replicates": REPLICATES,
        "input_hashes_verified_after": True,
        "point_counts_match_existing_outputs": True,
        "results": result,
    }
    (HERE / "results.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")

    def interval(v):
        return "NA" if v is None else f"{100 * v[0]:.1f}–{100 * v[1]:.1f}%"

    report = [
        "# Exploratory task-cluster bootstrap",
        "",
        "This is a DEV follow-on, not a blind result. Counts match the existing baseline and exact-matrix outputs.",
        "",
        "| Method | Corpus | Cheats / kept | Wilson 95% | Cluster bootstrap 95% | Valid draws | Task clusters |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for name in decisions:
        for group in groups:
            r = result[name][group]
            rate = "NA" if r["fraction"] is None else f"{100 * r['fraction']:.1f}%"
            report.append(
                f"| {name} | {group} | {r['cheats']}/{r['kept']} ({rate}) | {interval(r['wilson95'])} | {interval(r['bootstrap_percentile95'])} | {r['valid_draws']}/5000 | {r['task_clusters']} |"
            )
    report += [
        "",
        "Zero-kept draws are excluded; NA means no valid draw. Wilson intervals treat runs as independent. Bootstrap intervals resample task clusters within each corpus; pooled draws retain corpus strata.",
        "",
        "Few clusters can yield unstable or degenerate percentile intervals, including zero-width intervals. Excluding zero-kept draws conditions the interval on a nonzero denominator. The fixed DEV-selected rules and thresholds are not refitted in bootstrap draws, so these intervals omit selection and fitting uncertainty. Corpus selection and missing-component semantics limit generalization. The recorded replay and counterfactual filters are not the same operating rule.",
        "",
        "Exact command:",
        "",
        "```sh",
        "PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/ov1104/wave2-detbench/workers/x06/cluster/bootstrap.py",
        "```",
        "",
        "See METHOD.md, input-hashes.json and results.json for method, receipts and unrounded values.",
    ]
    (HERE / "REPORT.md").write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
