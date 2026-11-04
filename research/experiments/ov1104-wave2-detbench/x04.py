#!/usr/bin/env python3
import collections
import hashlib
import json
import math
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path("/private/tmp/ov1104/shared/detbench")
OUT = pathlib.Path(__file__).resolve().parent
PREREG = pathlib.Path("/private/tmp/ov1104/wave2-detbench/PREREG.md")
B = 2000
SEED = 4204


def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def jsonl(p):
    with open(p) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def auc(y, s):
    pos = sorted(s[y == 1])
    neg = sorted(s[y == 0])
    n = len(pos) * len(neg)
    if not n:
        return None
    import bisect

    return (
        sum(
            bisect.bisect_left(neg, x)
            + 0.5 * (bisect.bisect_right(neg, x) - bisect.bisect_left(neg, x))
            for x in pos
        )
        / n
    )


def pooled_ci(scored):
    usable = [(r, x) for r, x in scored if r.get("cluster") not in (None, "")]
    clusters = sorted({str(r["cluster"]) for r, x in usable})
    if len(clusters) < 2:
        return None
    idx = {c: i for i, c in enumerate(clusters)}
    ordered = sorted(usable, key=lambda z: z[1])
    y = np.array([r["label"] for r, x in ordered])
    ci = np.array([idx[str(r["cluster"])] for r, x in ordered])
    scores = np.array([x for r, x in ordered])
    if not (np.any(y == 0) and np.any(y == 1)):
        return None
    ends = np.r_[np.flatnonzero(scores[1:] != scores[:-1]), len(scores) - 1]
    starts = np.r_[0, ends[:-1] + 1]
    rng = np.random.default_rng(SEED)
    results = []
    for start in range(0, B, 100):
        n = min(100, B - start)
        draws = rng.multinomial(len(clusters), np.full(len(clusters), 1 / len(clusters)), size=n)
        weights = draws[:, ci]
        neg = weights * (y == 0)
        pos = weights * (y == 1)
        cumul = np.cumsum(neg, axis=1)
        before = np.zeros((n, len(starts)), dtype=float)
        if len(starts) > 1:
            before[:, 1:] = cumul[:, starts[1:] - 1]
        tied = cumul[:, ends] - before
        pos_groups = np.add.reduceat(pos, starts, axis=1)
        num = np.sum(pos_groups * (before + 0.5 * tied), axis=1)
        den = neg.sum(axis=1) * pos.sum(axis=1)
        results.extend((num[den > 0] / den[den > 0]).tolist())
    return [float(x) for x in np.quantile(results, [0.025, 0.975])] if results else None


def calc(rows, values):
    total = len(rows)
    scored = []
    abst = collections.Counter()
    missing = 0
    versions = collections.Counter()
    for r in rows:
        v = values.get(r["run_key"])
        if v is None:
            missing += 1
            continue
        versions[str(v.get("version"))] += 1
        x = v.get("score")
        if (
            v.get("abstain") is not None
            or isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
        ):
            abst[str(v.get("abstain") or "invalid_or_null_score")] += 1
            continue
        scored.append((r, float(x)))
    y = np.array([r["label"] for r, x in scored])
    s = np.array([x for r, x in scored])
    groups = collections.defaultdict(list)
    unknown_cluster = 0
    for r, x in scored:
        if r.get("cluster") in (None, ""):
            unknown_cluster += 1
            continue
        groups[str(r["cluster"])].append((r["label"], x))
    ca = []
    wins = ties = pairs = 0
    for obs in groups.values():
        p = sorted(x for label, x in obs if label == 1)
        n = sorted(x for label, x in obs if label == 0)
        if not p or not n:
            continue
        import bisect

        w = sum(bisect.bisect_left(n, x) for x in p)
        t = sum(bisect.bisect_right(n, x) - bisect.bisect_left(n, x) for x in p)
        k = len(p) * len(n)
        wins += w
        ties += t
        pairs += k
        ca.append((w + 0.5 * t) / k)
    ci = None
    if len(ca) >= 2:
        rng = np.random.default_rng(SEED)
        a = np.array(ca)
        estimates = np.mean(a[rng.integers(0, len(a), size=(B, len(a)))], axis=1)
        ci = [float(x) for x in np.quantile(estimates, [0.025, 0.975])]
    within = float(np.mean(ca)) if ca else None
    py = np.array([r["label"] for r, x in scored if r["_reward"] == 1])
    ps = np.array([x for r, x in scored if r["_reward"] == 1])
    pooled = auc(y, s)
    pci = pooled_ci(scored) if pooled is not None else None
    return dict(
        n_total=total,
        n_scored=len(scored),
        n_missing=missing,
        n_abstain=sum(abst.values()),
        abstention_reasons=dict(abst),
        versions=dict(versions),
        positive=int(np.sum(y == 1)),
        negative=int(np.sum(y == 0)),
        pooled_auc=pooled,
        pooled_ci=pci,
        within_auc=within,
        within_ci=ci,
        cluster_total=len(
            set(str(r["cluster"]) for r in rows if r.get("cluster") not in (None, ""))
        ),
        cluster_scored=len(groups),
        cluster_mixed=len(ca),
        unknown_cluster_scored=unknown_cluster,
        pairs=pairs,
        strict_wins=wins,
        ties=ties,
        strict_paired=wins / pairs if pairs else None,
        pass_auc=auc(py, ps),
        pass_scored=len(py),
        pass_positive=int(np.sum(py == 1)),
        pass_negative=int(np.sum(py == 0)),
        reward_unknown=sum(r["_reward"] is None for r, x in scored),
        reward_nonpass=sum(r["_reward"] is not None and r["_reward"] != 1 for r, x in scored),
        gap_flag=(pooled - within > 0.1) if pooled is not None and within is not None else None,
    )


def fmt(x):
    return "—" if x is None else f"{x:.3f}"


def main():
    files = sorted((ROOT / "signals").glob("*.jsonl"))
    manifest = ROOT / "runs.jsonl"
    # Freeze and publish input hashes before loading labels or scores.
    snap = {
        "command": sys.executable + " " + str(pathlib.Path(__file__).resolve()),
        "seed": SEED,
        "bootstrap": B,
        "prereg_sha256": digest(PREREG),
        "runs_sha256": digest(manifest),
        "signals": {str(p): digest(p) for p in files},
    }
    (OUT / "input-hashes.json").write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    rows = list(jsonl(manifest))
    keys = [r["run_key"] for r in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError("duplicate manifest run keys")
    # Hash the exact reward-bearing inputs before reading reward values.
    views = sorted({r["view"] for r in rows if r.get("view")})
    vh = {}
    for p in views:
        try:
            vh[p] = digest(p)
        except (OSError, TypeError):
            vh[p] = None
    snap["views"] = vh
    (OUT / "input-hashes.json").write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    reward = {}
    for p in views:
        try:
            with open(p) as f:
                v = json.load(f)
            reward[p] = (v.get("final") or {}).get("reward")
        except (OSError, ValueError, AttributeError):
            reward[p] = None
    byset = collections.defaultdict(list)
    for r in rows:
        if r.get("split") not in ("dev", "heldout") or r.get("label") not in (0, 1):
            continue
        r["_reward"] = reward.get(r.get("view"))
        byset[(r["set"], r["split"])].append(r)
    output = []
    problems = []
    for file in files:
        if digest(file) != snap["signals"][str(file)]:
            problems.append(f"{file.name}: changed after snapshot; excluded")
            continue
        values = {}
        duplicates = set()
        for v in jsonl(file):
            k = v.get("run_key")
            if k in values:
                duplicates.add(k)
            else:
                values[k] = v
        for k in duplicates:
            values.pop(k, None)
        for (name, split), rr in sorted(byset.items()):
            for strata in ("all", "natural", "prompted"):
                part = (
                    rr
                    if strata == "all"
                    else [r for r in rr if r.get("natural") is (strata == "natural")]
                )
                if not part:
                    continue
                rec = {
                    "signal": file.stem,
                    "set": name,
                    "split": split,
                    "stratum": strata,
                    "duplicate_keys_excluded": len(duplicates),
                    **calc(part, values),
                }
                rec["read_by"] = sorted({v for r in part for v in r.get("read_by", [])})
                output.append(rec)
        print(file.name, flush=True)
    (OUT / "aggregate.json").write_text(
        json.dumps(
            {"snapshot": "input-hashes.json", "records": output, "problems": problems},
            indent=None,
            sort_keys=True,
        )
        + "\n"
    )
    lines = [
        "# X04: within-task detector discrimination",
        "",
        "Each row is a signal and set. AUROC gives ties half credit. Strict pairs give ties zero. Within-task confidence intervals resample mixed-label task clusters (2,000 draws, seed 4204). Missing scores and abstentions are excluded, not negative predictions. `pass` uses recorded reward exactly 1. Null values mean insufficient eligible data. The JSON contains strata, score versions, abstention reasons, cluster coverage, reward counts and exposure history.",
        "",
        "The single L0 rule identified by the existing DEV matrix is `l0v_full_l0_hard_gate_rule_hidden_info_read`. It and both length signals appear as individual rows; versions and coverage differ, so compare only matched observations. No TEST data or answer keys were consulted.",
        "",
        "| signal | set | split | pooled | within [95% CI] | pass | strict | pairs | mixed/scored clusters | scored/total | missing | abstain | gap >0.10 |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in output:
        if r["stratum"] != "all":
            continue
        ci = r["within_ci"]
        w = fmt(r["within_auc"]) + (f" [{fmt(ci[0])}, {fmt(ci[1])}]" if ci else " [unavailable]")
        lines.append(
            f"| {r['signal']} | {r['set']} | {r['split']} | {fmt(r['pooled_auc'])} | {w} | {fmt(r['pass_auc'])} | {fmt(r['strict_paired'])} | {r['pairs']} | {r['cluster_mixed']}/{r['cluster_scored']} | {r['n_scored']}/{r['n_total']} | {r['n_missing']} | {r['n_abstain']} | {r['gap_flag']} |"
        )
    lines += [
        "",
        "## Limitations",
        "",
        "These are descriptive, not blind confirmatory results. Prior detector exposure is in the aggregate. A single mixed cluster has no bootstrap interval. Missing cluster IDs do not form artificial pairs. Passing-only results exclude missing rewards. Natural and prompted rows require both labels in the same stratum and appear in the aggregate. Multiple versions are reported, not silently treated as one frozen detector. Baseline selection comes from the existing DEV matrix, not held-out optimization. Signal files changing after snapshot are excluded.",
        "",
        f"Command: `{snap['command']}`. Input hashes: `input-hashes.json`. Issues: {problems or 'none'}.",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
