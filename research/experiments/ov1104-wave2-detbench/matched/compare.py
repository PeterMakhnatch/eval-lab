#!/usr/bin/env python3
"""Matched-observation comparison of DetBench signals against length baselines.

Reuses the loading, validity, AUROC and within-task definitions of
workers/x04/analyze.py. See PROTOCOL.md for the frozen protocol.
"""

import bisect
import collections
import hashlib
import json
import math
import pathlib
import platform
import time

import numpy as np

ROOT = pathlib.Path("/private/tmp/ov1104/shared/detbench")
OUT = pathlib.Path(__file__).resolve().parent
PREREG = pathlib.Path("/private/tmp/ov1104/wave2-detbench/PREREG.md")
PROTOCOL = OUT / "PROTOCOL.md"
B = 2000
SEED = 4204
BASELINES = ("length_chars", "length_steps")
MIN_MIXED = 5
MIN_SCORED = 50
GAP = 0.10
CACHE_CAP = 512 * 1024 * 1024
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def jsonl(p):
    with open(p) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def auc_np(y, s):
    """P(score_cheat > score_honest) + 0.5 P(tie); identical to analyze.py's auc."""
    y = np.asarray(y)
    s = np.asarray(s, dtype=float)
    pos = np.sort(s[y == 1])
    neg = np.sort(s[y == 0])
    if pos.size == 0 or neg.size == 0:
        return None
    left = np.searchsorted(neg, pos, "left")
    right = np.searchsorted(neg, pos, "right")
    return float(np.sum(left + 0.5 * (right - left)) / (pos.size * neg.size))


def load_signal(path):
    values, seen, dup = {}, set(), set()
    abst, versions, n_rows = collections.Counter(), collections.Counter(), 0
    for v in jsonl(path):
        n_rows += 1
        versions[str(v.get("version"))] += 1
        k = v.get("run_key")
        x = v.get("score")
        if (
            v.get("abstain") is not None
            or isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
        ):
            abst[str(v.get("abstain") or "invalid_or_null_score")] += 1
            continue
        if k in seen:
            dup.add(k)
        else:
            seen.add(k)
            values[k] = float(x)
    for k in dup:
        values.pop(k, None)
    return dict(
        values=values,
        duplicates=len(dup),
        n_rows=n_rows,
        abstentions=dict(abst),
        versions=dict(versions),
    )


def cluster_stats(rows, scores):
    """Per mixed cluster: (AUROC with ties half credit, positive x negative pairs)."""
    pos, neg = collections.defaultdict(list), collections.defaultdict(list)
    for item in rows:
        k, lab, cl = item[0], item[1], item[2]
        if cl is None or cl == "":
            continue
        c = str(cl)
        if lab == 1:
            pos[c].append(scores[k])
        else:
            neg[c].append(scores[k])
    out = {}
    for c in pos:
        if c not in neg:
            continue
        p, n = sorted(pos[c]), sorted(neg[c])
        w = sum(bisect.bisect_left(n, x) for x in p)
        t = sum(bisect.bisect_right(n, x) - bisect.bisect_left(n, x) for x in p)
        pairs = len(p) * len(n)
        out[c] = ((w + 0.5 * t) / pairs, pairs)
    return out


def summarize(stats):
    if not stats:
        return None, 0, 0
    return (
        float(np.mean([a for a, _ in stats.values()])),
        int(sum(k for _, k in stats.values())),
        len(stats),
    )


def boot_ci(a_sig, a_base, draws):
    d = np.asarray(a_sig, float) - np.asarray(a_base, float)
    est = draws.astype(float) @ d / draws.shape[1]
    return [float(np.quantile(est, 0.025)), float(np.quantile(est, 0.975))], est


def main():
    if not PROTOCOL.exists():
        raise SystemExit("PROTOCOL.md must exist before hashing")

    files = sorted((ROOT / "signals").glob("*.jsonl"))
    manifest = ROOT / "runs.jsonl"
    # Hash protocol and every input BEFORE reading labels or scores.
    snap = {
        "protocol_sha256": digest(PROTOCOL),
        "prereg_sha256": digest(PREREG),
        "runs_sha256": digest(manifest),
        "signals": {p.name: digest(p) for p in files},
        "signal_count": len(files),
        "compare_py_sha256": digest(pathlib.Path(__file__).resolve()),
        "before_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED,
        "bootstrap": B,
        "command": "uv run --with numpy python compare.py",
        "python_version": platform.python_version(),
        "inputs": {
            "protocol": str(PROTOCOL),
            "prereg": str(PREREG),
            "runs": str(manifest),
            "signals_dir": str(ROOT / "signals"),
        },
    }
    (OUT / "input-hashes.json").write_text(
        json.dumps({**snap, "verified_unchanged": None, "changed": []}, indent=2, sort_keys=True)
        + "\n"
    )
    log(f"hashed {len(files)} signal files, protocol, prereg, manifest")

    rows = list(jsonl(manifest))
    keys = [r["run_key"] for r in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError("duplicate manifest run keys")
    sets = collections.defaultdict(list)
    for r in rows:
        if r.get("split") not in ("dev", "heldout") or r.get("label") not in (0, 1):
            continue
        sets[r["set"]].append((r["run_key"], int(r["label"]), r.get("cluster"), r.get("split")))
    n_manifest = len(rows)
    n_used = sum(len(v) for v in sets.values())

    sig = {p.stem: load_signal(p) for p in files}
    signal_meta = {
        name: {
            "n_rows": rec["n_rows"],
            "duplicate_keys_excluded": rec["duplicates"],
            "abstentions": rec["abstentions"],
            "versions": rec["versions"],
        }
        for name, rec in sig.items()
    }
    log(f"loaded {len(sig)} signals")
    base_scores = {b: sig[b]["values"] for b in BASELINES}

    # Baseline provenance: length scores must equal manifest n_steps / n_chars.
    manifest_by_key = {r["run_key"]: r for r in rows}
    integrity = {}
    for b, field in (("length_chars", "n_chars"), ("length_steps", "n_steps")):
        bad, checked = [], 0
        for k, v in base_scores[b].items():
            r = manifest_by_key.get(k)
            if r is None or r.get(field) is None:
                continue
            checked += 1
            if float(r[field]) != v:
                bad.append({"run_key": k, "score": v, "manifest": r[field]})
        integrity[b] = {
            "field": field,
            "checked": checked,
            "mismatches": len(bad),
            "examples": bad[:5],
        }
    log(
        f"baseline integrity: {integrity['length_chars']['mismatches']} char, "
        f"{integrity['length_steps']['mismatches']} step mismatches"
    )

    set_base_counts = {
        (name, b): sum(1 for t in rr if t[0] in base_scores[b])
        for name, rr in sets.items()
        for b in BASELINES
    }

    # Cache cluster AUROCs for baseline on full-set matched rows.
    full_base_stats = {}
    for name, rr in sets.items():
        for b in BASELINES:
            full_base_stats[(name, b)] = cluster_stats(rr, base_scores[b])
    log("precomputed full-set baseline cluster statistics")

    draw_cache, cache_bytes = {}, [0]

    def get_draws(set_name, clusters):
        key = (set_name, tuple(clusters))
        m = draw_cache.get(key)
        if m is not None:
            return m, True
        k = len(clusters)
        rng = np.random.default_rng(SEED)
        m = rng.multinomial(k, np.full(k, 1.0 / k), size=B).astype(np.int16)
        if cache_bytes[0] + m.nbytes <= CACHE_CAP:
            draw_cache[key] = m
            cache_bytes[0] += m.nbytes
        return m, False

    out_rows = []
    for si, (sname, srec) in enumerate(sorted(sig.items()), 1):
        sv = srec["values"]
        is_baseline = sname in BASELINES
        for set_name, rr in sorted(sets.items()):
            n_set = len(rr)
            sig_in_set = [t for t in rr if t[0] in sv]
            n_sig = len(sig_in_set)
            if not sig_in_set:
                continue
            sig_stats = cluster_stats(sig_in_set, sv)
            w_sig, pairs, n_mixed = summarize(sig_stats)
            p_sig = auc_np([t[1] for t in sig_in_set], [sv[t[0]] for t in sig_in_set])
            for b in BASELINES:
                bv = base_scores[b]
                matched = [t for t in sig_in_set if t[0] in bv]
                n_match = len(matched)
                if n_match == 0:
                    continue
                full = n_match == n_set
                b_stats = full_base_stats[(set_name, b)] if full else cluster_stats(matched, bv)
                w_base, b_pairs, b_mixed = summarize(b_stats)
                p_base = auc_np([t[1] for t in matched], [bv[t[0]] for t in matched])
                rec = {
                    "signal": sname,
                    "set": set_name,
                    "split": rr[0][3],
                    "baseline": b,
                    "signal_is_baseline": is_baseline,
                    "n_set": n_set,
                    "n_signal_scored": n_sig,
                    "n_baseline_scored": set_base_counts[(set_name, b)],
                    "n_matched": n_match,
                    "n_matched_unknown_cluster": sum(1 for t in matched if t[2] in (None, "")),
                    "matched_positive": sum(t[1] for t in matched),
                    "matched_negative": sum(1 for t in matched if t[1] == 0),
                    "mixed_clusters": n_mixed,
                    "pairs": pairs,
                    "within_signal": w_sig,
                    "within_baseline": w_base,
                    "within_diff": (None if w_sig is None or w_base is None else w_sig - w_base),
                    "pooled_signal": p_sig,
                    "pooled_baseline": p_base,
                    "pooled_diff": (None if p_sig is None or p_base is None else p_sig - p_base),
                    "within_diff_ci": None,
                    "boot_draws": 0,
                    "draws_cached": False,
                    "eligible": False,
                    "ineligible_reasons": [],
                }
                reasons = []
                if n_mixed < MIN_MIXED:
                    reasons.append(f"mixed_clusters<{MIN_MIXED}")
                if n_sig < MIN_SCORED:
                    reasons.append(f"signal_scored<{MIN_SCORED}")
                if rec["n_baseline_scored"] < MIN_SCORED:
                    reasons.append(f"baseline_scored<{MIN_SCORED}")
                if w_sig is None or w_base is None:
                    reasons.append("no_within_task_estimate")
                if not reasons:
                    clusters = sorted(sig_stats)
                    a_sig = [sig_stats[c][0] for c in clusters]
                    a_base = [b_stats[c][0] for c in clusters]
                    draws, cached = get_draws(set_name, clusters)
                    ci, _ = boot_ci(a_sig, a_base, draws)
                    rec.update(
                        within_diff_ci=ci,
                        boot_draws=int(draws.shape[0]),
                        draws_cached=bool(cached),
                        eligible=True,
                    )
                else:
                    rec["ineligible_reasons"] = reasons
                out_rows.append(rec)
        if si % 50 == 0:
            log(f"{si}/{len(sig)} signals")

    log(f"computed {len(out_rows)} comparison rows")

    # Verify every input is unchanged after computing, before writing results.
    after = {
        "protocol_sha256": digest(PROTOCOL),
        "prereg_sha256": digest(PREREG),
        "runs_sha256": digest(manifest),
        "signals": {p.name: digest(p) for p in files},
    }
    changed = [
        k for k in ("protocol_sha256", "prereg_sha256", "runs_sha256") if after[k] != snap[k]
    ]
    changed += [
        f"signals/{n}" for n in snap["signals"] if after["signals"].get(n) != snap["signals"][n]
    ]
    final_hash = {
        **snap,
        "after_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "after": after,
        "changed": changed,
        "verified_unchanged": not changed,
    }
    (OUT / "input-hashes.json").write_text(json.dumps(final_hash, indent=2, sort_keys=True) + "\n")
    if changed:
        raise SystemExit(f"inputs changed during computation: {changed}")

    # Primary row per (signal, set): the better length baseline.
    primary = {}
    for rec in out_rows:
        if not rec["eligible"]:
            continue
        key = (rec["signal"], rec["set"])
        cur = primary.get(key)
        if cur is None or rec["within_baseline"] > cur["within_baseline"] + 1e-15:
            primary[key] = rec
    prim = [primary[k] for k in sorted(primary)]
    testable = [r for r in prim if not r["signal_is_baseline"]]

    def count_block(pred):
        rr = [r for r in testable if pred(r)]
        return {
            "rows": len(rr),
            "signals": len({r["signal"] for r in rr}),
            "names": sorted({r["signal"] for r in rr}),
        }

    def ci(r):
        return r["within_diff_ci"]

    def beats(r):
        return r["within_diff"] > 0 and ci(r) is not None and ci(r)[0] > 0

    def loses(r):
        return r["within_diff"] < 0 and ci(r) is not None and ci(r)[1] < 0

    def degenerate(r):
        return ci(r) is not None and ci(r)[0] == ci(r)[1]

    summary = {
        "comparison_rows_total": len(out_rows),
        "eligible_rows_total": len([r for r in out_rows if r["eligible"]]),
        "eligible_signal_sets": len(prim),
        "testable_signal_sets": len(testable),
        "distinct_testable_signals": len({r["signal"] for r in testable}),
        "beats_better_baseline_ci_lower_gt_0": count_block(beats),
        "loses_to_better_baseline_ci_upper_lt_0": count_block(loses),
        "positive_point_estimate": count_block(lambda r: r["within_diff"] > 0),
        "negative_point_estimate": count_block(lambda r: r["within_diff"] < 0),
        "tie_point_estimate": count_block(lambda r: r["within_diff"] == 0),
        "better_baseline_rows": {
            b: len([r for r in testable if r["baseline"] == b]) for b in BASELINES
        },
    }
    zw_lose = [r for r in testable if loses(r) and degenerate(r)]
    summary["zero_width_ci"] = {
        "rows": len([r for r in testable if degenerate(r)]),
        "counted_as_loses": len(zw_lose),
        "counted_as_beats": len([r for r in testable if beats(r) and degenerate(r)]),
        "tied_rows": len([r for r in testable if degenerate(r) and r["within_diff"] == 0]),
        "sets": sorted({r["set"] for r in testable if degenerate(r)}),
        "loses_excluding_zero_width": len([r for r in testable if loses(r) and not degenerate(r)]),
    }
    patterns = collections.defaultdict(lambda: [0, 0, 0])
    per_set = collections.defaultdict(
        lambda: {"eligible_rows": 0, "beats": 0, "loses": 0, "zero_width": 0}
    )
    for r in testable:
        p = patterns[r["signal"]]
        p[2] += 1
        if beats(r):
            p[0] += 1
        if loses(r):
            p[1] += 1
        d = per_set[r["set"]]
        d["eligible_rows"] += 1
        d["beats"] += int(beats(r))
        d["loses"] += int(loses(r))
        d["zero_width"] += int(degenerate(r))
    summary["signal_patterns"] = {
        "signals_with_eligible_rows": len(patterns),
        "beat_only": sum(1 for v in patterns.values() if v[0] > 0 and v[1] == 0),
        "lose_only": sum(1 for v in patterns.values() if v[1] > 0 and v[0] == 0),
        "both": sum(1 for v in patterns.values() if v[0] > 0 and v[1] > 0),
        "neither": sum(1 for v in patterns.values() if v[0] == 0 and v[1] == 0),
    }
    summary["per_set"] = {k: v for k, v in sorted(per_set.items())}
    gap_rows = [
        r for r in testable if r["pooled_signal"] is not None and r["within_signal"] is not None
    ]
    gap = [r for r in gap_rows if r["pooled_signal"] - r["within_signal"] > GAP]
    summary["pooled_gap_gt_0_10"] = {
        "total_rows": len(gap),
        "beats_length_within_task": len([r for r in gap if r["within_diff"] > 0]),
        "does_not_beat_length_within_task": len([r for r in gap if not r["within_diff"] > 0]),
        "ci_confirmed_beats": len([r for r in gap if beats(r)]),
        "ci_confirmed_loses": len([r for r in gap if loses(r)]),
        "evaluated_rows": len(gap_rows),
    }
    summary["baseline_integrity"] = integrity
    summary["manifest_rows"] = n_manifest
    summary["manifest_rows_used"] = n_used
    summary["draw_cache_entries"] = len(draw_cache)
    summary["draw_cache_bytes"] = cache_bytes[0]

    (OUT / "matched.json").write_text(
        json.dumps(
            {
                "meta": {
                    "protocol": "PROTOCOL.md",
                    "protocol_sha256": snap["protocol_sha256"],
                    "seed": SEED,
                    "bootstrap": B,
                    "min_mixed_clusters": MIN_MIXED,
                    "min_scored": MIN_SCORED,
                    "gap_threshold": GAP,
                    "better_baseline_rule": "higher within_task AUROC on matched rows; tie -> length_chars",
                    "command": snap["command"],
                },
                "summary": summary,
                "signal_meta": signal_meta,
                "rows": out_rows,
            },
            indent=None,
            sort_keys=True,
        )
        + "\n"
    )
    log("wrote matched.json")

    # ---------------- digest ----------------
    def fmt(x):
        return "—" if x is None else f"{x:.3f}"

    def ci_s(r):
        c = r["within_diff_ci"]
        return f"[{fmt(c[0])}, {fmt(c[1])}]" if c else "unavailable"

    ranked = sorted(testable, key=lambda r: (r["within_diff"], r["signal"], r["set"]))
    top = list(reversed(ranked[-15:]))
    bottom = ranked[:15]

    lines = [
        "# X04 matched-observation comparison against length baselines",
        "",
        f"Question: on the SAME scored observations, does each signal beat the length baselines within task? "
        f"Protocol and inputs were hashed before computing and re-verified after "
        f"(`verified_unchanged = {str(final_hash['verified_unchanged']).lower()}`; see `input-hashes.json`). "
        f"Seed {SEED}, {B} cluster-bootstrap draws, mixed-label clusters only, ties half credit, "
        f"matched rows only. Within-task intervals resample mixed clusters; the difference interval is computed "
        f"from paired per-cluster AUROC differences on the same matched rows.",
        "",
        "## Headline",
        "",
        f"- Manifest rows used: {n_used} of {n_manifest} (`dev`/`heldout`, label 0 or 1). "
        f"{len(sets)} sets, {len(sig)} signal files including the 2 length baselines.",
        f"- Eligible (signal, set) rows: {len(prim)}; after excluding the two baseline files: {len(testable)} "
        f"primary rows covering {summary['distinct_testable_signals']} signals, plus "
        f"{summary['eligible_rows_total']} eligible signal-set-baseline comparison rows in `matched.json`. "
        f"Eligibility: at least {MIN_MIXED} mixed clusters and at least {MIN_SCORED} scored runs for signal and baseline.",
        f"- Beat the better length baseline within task with the bootstrap CI lower bound above 0: "
        f"**{summary['beats_better_baseline_ci_lower_gt_0']['rows']} rows / "
        f"{summary['beats_better_baseline_ci_lower_gt_0']['signals']} signals** (a signal counts with at least "
        f"one qualifying set).",
        f"- Lose to the better length baseline with the CI upper bound below 0: "
        f"**{summary['loses_to_better_baseline_ci_upper_lt_0']['rows']} rows / "
        f"{summary['loses_to_better_baseline_ci_upper_lt_0']['signals']} signals** (same counting).",
        f"- Point-estimate sign only (no CI exclusion): {summary['positive_point_estimate']['rows']} positive, "
        f"{summary['negative_point_estimate']['rows']} negative, {summary['tie_point_estimate']['rows']} tied.",
        f"- Better baseline chosen per row: {summary['better_baseline_rows']}.",
    ]
    sp = summary["signal_patterns"]
    zw = summary["zero_width_ci"]
    lines += [
        f"- Of {sp['signals_with_eligible_rows']} signals with at least one eligible set: {sp['beat_only']} "
        f"beat with a CI above 0 in at least one set and never lose with a CI below 0, {sp['lose_only']} only lose, "
        f"{sp['both']} do both across sets, {sp['neither']} do neither. A signal counted in several sets is one "
        f"detector, not independent evidence.",
        f"- Degenerate intervals: {zw['rows']} primary rows have a zero-width interval because every mixed cluster "
        f"has the same AUROC difference. {zw['counted_as_loses']} of them are counted as losses, almost all on "
        f"`trusted-monitoring-logs` where the char baseline is perfect and the signal is constant; "
        f"{zw['tied_rows']} are exact ties from duplicate-of-baseline scores. Excluding the zero-width losses, "
        f"CI-confirmed losses are {zw['loses_excluding_zero_width']}.",
        "",
        "This is a descriptive secondary analysis with prior exposure to the X04 aggregate and the DetBench "
        "signal matrix. No family-wise correction is applied, so individual intervals near 0 should not be read "
        "as confirmations.",
        "",
        "## Per-set outcomes (primary rows)",
        "",
        "| set | eligible rows | CI beats | CI loses | zero-width intervals |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, d in summary["per_set"].items():
        lines.append(
            f"| {name} | {d['eligible_rows']} | {d['beats']} | {d['loses']} | {d['zero_width']} |"
        )
    lines += [
        "",
        "## Baseline integrity",
        "",
        "| baseline | manifest field | checked | mismatches |",
        "|---|---|---:|---:|",
    ]
    for b in BASELINES:
        it = integrity[b]
        lines.append(f"| {b} | {it['field']} | {it['checked']} | {it['mismatches']} |")
    lines += [
        "",
        "Full-set baseline within-task AUROC (mixed clusters / matched runs):",
        "",
        "| set | split | mixed clusters in set | " + " | ".join(BASELINES) + " |",
        "|---|---|---:|" + "---:|" * len(BASELINES),
    ]
    for name in sorted(sets):
        rr = sets[name]
        cells = []
        mixed_total = len(full_base_stats[(name, BASELINES[0])])
        for b in BASELINES:
            w, _, _ = summarize(full_base_stats[(name, b)])
            cells.append(fmt(w))
        lines.append(f"| {name} | {rr[0][3]} | {mixed_total} | " + " | ".join(cells) + " |")

    def table(rs, title):
        out = [
            f"## {title}",
            "",
            "| # | signal | set | within signal | better baseline | within baseline | diff | 95% CI | mixed clusters | matched runs | pairs |",
            "|---:|---|---|---:|---|---:|---:|---|---:|---:|---:|",
        ]
        for i, r in enumerate(rs, 1):
            out.append(
                f"| {i} | {r['signal']} | {r['set']} | {fmt(r['within_signal'])} | {r['baseline']} | "
                f"{fmt(r['within_baseline'])} | {r['within_diff']:+.3f} | {ci_s(r)} | "
                f"{r['mixed_clusters']} | {r['n_matched']} | {r['pairs']} |"
            )
        return out

    lines += [""] + table(
        top, "Top 15 by within-task difference against the better baseline (eligible rows)"
    )
    lines += [
        "",
        "Repeated values are derived signals with identical predictions; equal rows are not independent evidence.",
    ]
    lines += [""] + table(
        bottom, "Bottom 15 by within-task difference against the better baseline (eligible rows)"
    )

    lines += [
        "",
        "## Best signal per set",
        "",
        "Best eligible signal by within-task AUROC, with its difference from the better length baseline.",
        "",
        "| set | split | best signal | within signal | better baseline | within baseline | diff | 95% CI | mixed clusters |",
        "|---|---|---|---:|---|---:|---:|---|---:|",
    ]
    by_set = collections.defaultdict(list)
    for r in testable:
        by_set[r["set"]].append(r)
    for name in sorted(by_set):
        rs = by_set[name]
        best = max(rs, key=lambda r: (r["within_signal"], r["signal"]))
        lines.append(
            f"| {name} | {best['split']} | {best['signal']} | {fmt(best['within_signal'])} | "
            f"{best['baseline']} | {fmt(best['within_baseline'])} | {best['within_diff']:+.3f} | "
            f"{ci_s(best)} | {best['mixed_clusters']} |"
        )
    no_set = sorted(set(sets) - set(by_set))
    if no_set:
        lines += ["", "Sets with no eligible signal row: " + ", ".join(no_set) + "."]

    pg = summary["pooled_gap_gt_0_10"]
    lines += [
        "",
        "## Pooled-versus-within gap",
        "",
        f"Rows where pooled AUROC exceeds within-task AUROC by more than {GAP:.2f} "
        f"(on the better-baseline matched rows): **{pg['total_rows']}** of {pg['evaluated_rows']} evaluated rows.",
        "",
        "| category | rows |",
        "|---|---:|",
        f"| gap > {GAP:.2f}, beats length within task (point estimate) | {pg['beats_length_within_task']} |",
        f"| gap > {GAP:.2f}, does not beat length within task (point estimate) | {pg['does_not_beat_length_within_task']} |",
        f"| gap > {GAP:.2f}, CI-confirmed beat | {pg['ci_confirmed_beats']} |",
        f"| gap > {GAP:.2f}, CI-confirmed loss | {pg['ci_confirmed_loses']} |",
        f"| gap > {GAP:.2f}, all | {pg['total_rows']} |",
    ]
    if gap:
        gap_sorted = sorted(gap, key=lambda r: -(r["pooled_signal"] - r["within_signal"]))[:10]
        lines += [
            "",
            "Largest gaps:",
            "",
            "| signal | set | pooled | within | gap | within diff vs better baseline |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for r in gap_sorted:
            lines.append(
                f"| {r['signal']} | {r['set']} | {fmt(r['pooled_signal'])} | {fmt(r['within_signal'])} | "
                f"{r['pooled_signal'] - r['within_signal']:+.3f} | {r['within_diff']:+.3f} |"
            )

    lines += [
        "",
        "## Limitations",
        "",
        f"- Several sets have exactly {MIN_MIXED} mixed clusters, so the percentile interval is coarse and unstable.",
        f"- {len(testable)} eligible signal-set tests are examined with no multiplicity correction; some "
        f"CI bounds near 0 are expected by chance. These counts are descriptive, not family-wise tests.",
        "- Prior exposure: the signals, baselines and sets were seen in the X04 aggregate and the DetBench "
        "signal matrix; several signals are detectors previously selected on DEV. No selection correction.",
        "- The better length baseline is selected on the same matched rows used for the test, which makes the "
        "reported difference conservative; the interval does not account for that selection.",
        "- Matched subsets differ from full-set composition, and matching removes runs where a signal abstains; "
        "results do not describe deployment coverage.",
        "- Rows inside a cluster are correlated and are resampled as a cluster; sets with few clusters cannot "
        "support within-task inference.",
        "- Length is a proxy for task difficulty and trajectory style, not a causal control. Beating it is "
        "necessary evidence of added discrimination, not proof of cheating detection.",
        "- Only `dev` and `heldout` manifest rows were read. No TEST data, no model calls.",
        "",
        f"Command: `{snap['command']}`. Protocol: `PROTOCOL.md` (sha256 `{snap['protocol_sha256']}`). "
        f"Input hashes: `input-hashes.json`. Rows: `matched.json`.",
    ]
    (OUT / "DIGEST.md").write_text("\n".join(lines) + "\n")
    log("wrote DIGEST.md")


if __name__ == "__main__":
    main()
