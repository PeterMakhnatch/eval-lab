#!/usr/bin/env python3
"""DQ-1: importer truncation audit over the blind DEV run texts.

No model calls. Reads /tmp/dt8/prompts/<blind_id>.txt (exactly the text DT-8 and DT-9
readers saw), counts the two truncation constructs inserted by lab code, joins the DEV
labels from the stored readings, and measures the reader's behaviour on truncated vs
untruncated runs.

Markers (see prereg.md):
  ELIDE  "[... N characters elided ...]"   run_atlas.bound(), N = characters dropped
  CAP    "…[truncated]"                     cheatbench._cap(), dropped chars unknown
A bare "…" (agent prose, "…(50 elems)") is NOT a marker.
"""

from __future__ import annotations

import glob
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad")
from stats import tertile_of, tertiles, wilson  # noqa: E402

BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
PROMPTS = Path("/tmp/dt8/prompts")
ALERTS = Path("/tmp/dt9/alerts.json")
OUT = Path(__file__).resolve().parent / "receipts"

STEP_RE = re.compile(r"\[step (\d+)(?: continued, part \d+/\d+)?\]")
ELIDE_RE = re.compile(r"\[\.\.\. (\d+) characters elided \.\.\.\]")
CAP_MARK = "…[truncated]"
BLOCK_RE = re.compile(r"<(reasoning|message|observation)>|<call ([^>]*)>")
ELEMS_RE = re.compile(r"…\(\d+ elems\)")

# DT-11's per-corpus length tertile cuts (steps).
CUTS = {"goodfire": (21.0, 28.0), "cheatbench": (17.0, 45.0)}


# --------------------------------------------------------------------------- census


def block_of(text: str, pos: int) -> str:
    kind = "none"
    for match in BLOCK_RE.finditer(text, 0, pos):
        kind = match.group(1) or "call"
    return kind


TAG_RE = re.compile(r"<(reasoning|message|observation)>|<call ([^>]*)>")


def block_spans(text: str) -> list[dict]:
    """Every rendered block with its step, kind, content and truncation flag."""
    spans = []
    for match in TAG_RE.finditer(text):
        kind = match.group(1) or "call"
        close = f"</{kind}>"
        end = text.find(close, match.end())
        if end < 0:
            continue
        head = text[: match.start()]
        found = list(STEP_RE.finditer(head))
        step = int(found[-1].group(1)) if found else -1
        content = text[match.end() : end]
        spans.append({
            "step": step,
            "kind": kind,
            "content": content,
            "truncated": ELIDE_RE.search(content) is not None or CAP_MARK in content,
        })
    return spans


def census_text(text: str) -> dict:
    blocks: Counter[str] = Counter()
    steps: set[int] = set()
    elide_n: list[int] = []
    n_cap = 0
    n_elide = 0
    positions: list[int] = []
    for match in ELIDE_RE.finditer(text):
        n_elide += 1
        elide_n.append(int(match.group(1)))
        positions.append(match.start())
    start = 0
    while True:
        index = text.find(CAP_MARK, start)
        if index < 0:
            break
        n_cap += 1
        positions.append(index)
        start = index + len(CAP_MARK)
    for pos in positions:
        blocks[block_of(text, pos)] += 1
        head = text[:pos]
        found = list(STEP_RE.finditer(head))
        if found:
            steps.add(int(found[-1].group(1)))
    all_steps = {int(m.group(1)) for m in STEP_RE.finditer(text)}
    return {
        "n_elide": n_elide,
        "n_cap": n_cap,
        "n_markers": n_elide + n_cap,
        "chars_dropped_known": sum(elide_n),
        "steps_with_marker": len(steps),
        "n_steps": len(all_steps),
        "blocks": dict(blocks),
        "any_bare_ellipsis": text.count("…"),
        "any_array_elems": len(ELEMS_RE.findall(text)),
        "chars": len(text),
        "truncated_steps": sorted(steps),
    }


BLOCKS: dict[str, list[dict]] = {}


def build_census() -> dict:
    rows = {}
    for path in sorted(glob.glob(str(PROMPTS / "*.txt"))):
        blind_id = Path(path).stem
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        text = raw.split("</run>", 1)[0]
        rows[blind_id] = census_text(text)
        BLOCKS[blind_id] = block_spans(text)
    return rows


# --------------------------------------------------------------------------- labels


def load_labels() -> dict[str, dict]:
    labels: dict[str, dict] = {}
    for line in (BRIDGE / "DT-8/receipts/readings-new.jsonl").read_text().splitlines():
        row = json.loads(line)
        labels[row["blind_id"]] = {
            "corpus": row["corpus"],
            "positive": bool(row["positive"]),
            "length": row["length"],
            "status": row.get("status"),
            "source": "dt8",
            "n_citations": row.get("n_citations", 0),
            "n_verified": row.get("n_verified", 0),
            "evidence": row.get("evidence") or [],
        }
    for line in (BRIDGE / "DT-2/receipts/readings.jsonl").read_text().splitlines():
        row = json.loads(line)
        labels.setdefault(
            row["blind_id"],
            {
                "corpus": "goodfire",
                "positive": bool(row["enacted"]),
                "length": row["length"],
                "status": row.get("status"),
                "source": "dt2",
                "n_citations": row.get("n_citations", 0),
                "n_verified": row.get("n_verified", 0),
                "evidence": row.get("evidence") or [],
            },
        )
    return labels


# --------------------------------------------------------------------------- stats


def fisher_exact(table: list[list[int]]) -> float:
    """Two-sided Fisher exact for a 2x2 table [[a,b],[c,d]]."""
    (a, b), (c, d) = table
    n = a + b + c + d
    row1, col1 = a + b, a + c
    lo, hi = max(0, row1 + col1 - n), min(row1, col1)

    def prob(x: int) -> float:
        return math.comb(row1, x) * math.comb(n - row1, col1 - x) / math.comb(n, col1)

    p_obs = prob(a)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p_obs + 1e-12))


def auroc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels, strict=False))
    rank_sum = 0.0
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(label for _, label in pairs[i : j + 1])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def fit_logit(x: list[list[float]], y: list[int], lam: float = 1.0, iters: int = 60) -> list[float]:
    """Ridge logistic regression by Newton steps on standardized columns (no numpy)."""
    p = len(x[0])
    beta = [0.0] * (p + 1)  # beta[0] intercept
    for _ in range(iters):
        grad = [0.0] * (p + 1)
        hess = [[0.0] * (p + 1) for _ in range(p + 1)]
        for row, label in zip(x, y, strict=False):
            z = beta[0] + sum(beta[k + 1] * row[k] for k in range(p))
            mu = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            diff = mu - label
            grad[0] += diff
            for k in range(p):
                grad[k + 1] += diff * row[k]
            w = mu * (1 - mu)
            hess[0][0] += w
            for k in range(p):
                hess[0][k + 1] += w * row[k]
                hess[k + 1][0] += w * row[k]
                for m in range(k, p):
                    hess[k + 1][m + 1] += w * row[k] * row[m]
        for k in range(p):
            for m in range(k):
                hess[k + 1][m + 1] = hess[m + 1][k + 1]
        for k in range(p):
            hess[k + 1][k + 1] += lam
        step = _solve(hess, grad)
        if step is None:
            break
        beta = [b - s for b, s in zip(beta, step, strict=False)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return beta


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    mat = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(mat[r][col]))
        if abs(mat[pivot][col]) < 1e-12:
            return None
        mat[col], mat[pivot] = mat[pivot], mat[col]
        for r in range(col + 1, n):
            factor = mat[r][col] / mat[col][col]
            for c in range(col, n + 1):
                mat[r][c] -= factor * mat[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        out[r] = (mat[r][n] - sum(mat[r][c] * out[c] for c in range(r + 1, n))) / mat[r][r]
    return out


def standardize(train: list[list[float]], test: list[list[float]]) -> tuple[list[list[float]], list[list[float]]]:
    p = len(train[0])
    means = [sum(row[k] for row in train) / len(train) for k in range(p)]
    stds = []
    for k in range(p):
        var = sum((row[k] - means[k]) ** 2 for row in train) / max(1, len(train) - 1)
        stds.append(math.sqrt(var) or 1.0)
    def norm(rows: list[list[float]]) -> list[list[float]]:
        return [[(row[k] - means[k]) / stds[k] for k in range(p)] for row in rows]
    return norm(train), norm(test)


def cv_oof(x: list[list[float]], y: list[int], repeats: int = 10, folds: int = 5, seed: int = 20261030) -> list[list[float]]:
    """Repeated stratified k-fold OOF scores; returns one score list per repeat."""
    rng = random.Random(seed)
    out = []
    pos = [i for i, v in enumerate(y) if v == 1]
    neg = [i for i, v in enumerate(y) if v == 0]
    for _ in range(repeats):
        rng.shuffle(pos)
        rng.shuffle(neg)
        assign = {}
        for group in (pos, neg):
            for i, idx in enumerate(group):
                assign[idx] = i % folds
        scores = [0.0] * len(y)
        for fold in range(folds):
            tr = [i for i in range(len(y)) if assign[i] != fold]
            te = [i for i in range(len(y)) if assign[i] == fold]
            xtr, xte = standardize([x[i] for i in tr], [x[i] for i in te])
            beta = fit_logit(xtr, [y[i] for i in tr])
            for row, i in zip(xte, te, strict=False):
                scores[i] = beta[0] + sum(beta[k + 1] * row[k] for k in range(len(row)))
        out.append(scores)
    return out


def bootstrap_auroc_diff(
    s_a: list[float], s_b: list[float], y: list[int], draws: int = 2000, seed: int = 20261030
) -> dict:
    rng = random.Random(seed)
    pos = [i for i, v in enumerate(y) if v == 1]
    neg = [i for i, v in enumerate(y) if v == 0]
    diffs = []
    for _ in range(draws):
        sample = [rng.choice(pos) for _ in pos] + [rng.choice(neg) for _ in neg]
        ya = [y[i] for i in sample]
        diffs.append(auroc([s_a[i] for i in sample], ya) - auroc([s_b[i] for i in sample], ya))
    diffs.sort()
    return {
        "point": auroc(s_a, y) - auroc(s_b, y),
        "lo": diffs[int(0.025 * draws)],
        "hi": diffs[int(0.975 * draws) - 1],
    }


def fmt(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    if n == 0:
        return "0/0"
    return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"


# --------------------------------------------------------------------------- main


def main() -> None:
    census = build_census()
    labels = load_labels()
    OUT.mkdir(parents=True, exist_ok=True)

    # ---- census receipt -------------------------------------------------
    by_corpus: Counter[str] = Counter()
    summary = Counter()
    for blind_id, row in census.items():
        corpus = labels.get(blind_id, {}).get("corpus", "unlabelled")
        row["corpus"] = corpus
        by_corpus[corpus] += 1
        summary["runs"] += 1
        summary["runs_any_marker"] += 1 if row["n_markers"] else 0
        summary["runs_elide"] += 1 if row["n_elide"] else 0
        summary["runs_cap"] += 1 if row["n_cap"] else 0
        summary["markers"] += row["n_markers"]
        summary["elide"] += row["n_elide"]
        summary["cap"] += row["n_cap"]
        summary["chars_dropped_known"] += row["chars_dropped_known"]
        summary["steps_with_marker"] += row["steps_with_marker"]
        summary["n_steps"] += row["n_steps"]
        summary["runs_bare_ellipsis"] += 1 if row["any_bare_ellipsis"] else 0
        summary["runs_array_elems"] += 1 if row["any_array_elems"] else 0
    blocks: Counter[str] = Counter()
    for row in census.values():
        blocks.update(row["blocks"])
    census_receipt = {
        "generated_by": "dq1_audit.py census",
        "source": "/tmp/dt8/prompts/*.txt up to first </run>",
        "caps": {"CONTEXT_CHARS": 12000, "ACTION_CHARS": 4000, "OBSERVATION_CHARS": 2500,
                 "cheatbench_MSG_CAP": 8000, "cheatbench_OBS_CAP": 50000},
        "runs_by_corpus": dict(by_corpus),
        "totals": dict(summary),
        "markers_by_block": dict(blocks),
        "run_distribution": {
            "n_markers": dict(sorted(Counter(r["n_markers"] for r in census.values()).items())),
            "chars_dropped_known_buckets": dict(sorted(Counter(
                "0" if r["chars_dropped_known"] == 0 else
                "1-999" if r["chars_dropped_known"] < 1000 else
                "1k-9.9k" if r["chars_dropped_known"] < 10000 else
                "10k-99k" if r["chars_dropped_known"] < 100000 else "100k+"
                for r in census.values()).items())),
        },
        "runs": {
            k: {kk: vv for kk, vv in v.items() if kk != "truncated_steps"}
            for k, v in census.items()
        },
    }
    (OUT / "truncation-census.json").write_text(json.dumps(census_receipt, indent=1) + "\n")

    # ---- A2 rates by label and corpus ------------------------------------
    def annotate(bid: str) -> dict:
        lab = labels[bid]
        row = census[bid]
        return {"blind_id": bid, **lab, **{k: row[k] for k in ("n_elide", "n_cap", "n_markers",
                                             "chars_dropped_known", "steps_with_marker", "n_steps")},
                "truncated": row["n_markers"] > 0,
                "corpus": lab["corpus"]}

    cohorts = {
        "goodfire": [annotate(b) for b in labels if labels[b]["corpus"] == "goodfire" and b in census],
        "cheatbench": [annotate(b) for b in labels if labels[b]["corpus"] == "cheatbench" and b in census],
    }
    rates = {}
    for corpus, rows in cohorts.items():
        pos = [r for r in rows if r["positive"]]
        neg = [r for r in rows if not r["positive"]]
        k1 = sum(r["truncated"] for r in pos)
        k2 = sum(r["truncated"] for r in neg)
        cell = {"positives": fmt(k1, len(pos)), "negatives": fmt(k2, len(neg)),
                "fisher_p": fisher_exact([[k1, len(pos) - k1], [k2, len(neg) - k2]]),
                "n": [len(pos), len(neg)]}
        tert = {}
        for t in (0, 1, 2):
            tp = [r for r in pos if tertile_of(r["length"], CUTS[corpus]) == t]
            tn = [r for r in neg if tertile_of(r["length"], CUTS[corpus]) == t]
            a, b = sum(r["truncated"] for r in tp), sum(r["truncated"] for r in tn)
            tert[str(t)] = {
                "positives": fmt(a, len(tp)), "negatives": fmt(b, len(tn)),
                "fisher_p": fisher_exact([[a, len(tp) - a], [b, len(tn) - b]]),
                "mean_markers_pos": (sum(r["n_markers"] for r in tp) / len(tp)) if tp else None,
                "mean_markers_neg": (sum(r["n_markers"] for r in tn) / len(tn)) if tn else None,
            }
        cell["by_tertile"] = tert
        rates[corpus] = cell

    # ---- A3 beyond length: logistic CV -----------------------------------
    beyond = {}
    for corpus, rows in cohorts.items():
        y = [1 if r["positive"] else 0 for r in rows]
        length = [[float(r["length"])] for r in rows]
        markers = [[float(r["n_markers"])] for r in rows]
        both = [[float(r["length"]), float(r["n_markers"])] for r in rows]
        res = {}
        oof = {}
        for name, x in (("length_only", length), ("markers_only", markers), ("length_plus_markers", both)):
            scores = cv_oof(x, y)
            oof[name] = [sum(s) / len(s) for s in zip(*scores, strict=False)]
            a = [auroc(s, y) for s in scores]
            res[name] = {"auroc_mean_over_repeats": sum(a) / len(a),
                         "auroc_per_repeat_sd": _sd(a)}
        res["delta_both_minus_length"] = bootstrap_auroc_diff(oof["length_plus_markers"], oof["length_only"], y)
        res["delta_markers_minus_length"] = bootstrap_auroc_diff(oof["markers_only"], oof["length_only"], y)
        res["raw_feature_auroc"] = {
            "length": auroc([r["length"] for r in rows], y),
            "n_markers": auroc([r["n_markers"] for r in rows], y),
            "markers_per_step": auroc([r["n_markers"] / max(1, r["n_steps"]) for r in rows], y),
        }
        res["n"] = {"positives": sum(y), "negatives": len(y) - sum(y)}
        beyond[corpus] = res

    # ---- A4 reader effect ------------------------------------------------
    reader = {}
    for corpus, rows in cohorts.items():
        usable = [r for r in rows if r.get("status") == "ok"]
        excluded = len(rows) - len(usable)
        def cell_of(sub: list[dict]) -> dict:
            pos = [r for r in sub if r["positive"]]
            neg = [r for r in sub if not r["positive"]]
            a = sum(1 for r in pos if r["n_verified"] > 0)
            b = sum(1 for r in neg if r["n_verified"] > 0)
            return {"recall": fmt(a, len(pos)), "false_alarms": fmt(b, len(neg)),
                    "fisher_p": fisher_exact([[a, len(pos) - a], [b, len(neg) - b]]) if pos and neg else None,
                    "n": [len(pos), len(neg)]}
        bucket = {
            "all": cell_of(usable),
            "truncated": cell_of([r for r in usable if r["truncated"]]),
            "untruncated": cell_of([r for r in usable if not r["truncated"]]),
            "excluded_not_ok": excluded,
            "by_tertile": {},
        }
        for t in (0, 1, 2):
            sub = [r for r in usable if tertile_of(r["length"], CUTS[corpus]) == t]
            bucket["by_tertile"][str(t)] = {
                "truncated": cell_of([r for r in sub if r["truncated"]]),
                "untruncated": cell_of([r for r in sub if not r["truncated"]]),
            }
        reader[corpus] = bucket

    # ---- A5 citations ----------------------------------------------------
    trunc_steps = {b: set(r["truncated_steps"]) for b, r in census.items()}
    dr = {"dt8_dt2": {"verified": {"trunc": 0, "total": 0}, "unverified": {"trunc": 0, "total": 0}},
          "dt9": {"reverified": {"trunc": 0, "total": 0}, "drifted": {"trunc": 0, "total": 0}},
          "dt9_examples": []}
    for blind_id, lab in labels.items():
        if blind_id not in trunc_steps:
            continue
        for cite in lab["evidence"]:
            step = cite.get("step")
            if step is None:
                continue
            key = "verified" if cite.get("verified") else "unverified"
            dr["dt8_dt2"][key]["total"] += 1
            dr["dt8_dt2"][key]["trunc"] += 1 if int(step) in trunc_steps[blind_id] else 0
    alerts = json.loads(ALERTS.read_text()) if ALERTS.exists() else []
    for alert in alerts:
        for cite in alert.get("citations", []):
            key = "reverified" if cite.get("reverified") else "drifted"
            dr["dt9"][key]["total"] += 1
            hit = int(cite["step"]) in trunc_steps.get(alert["blind_id"], set())
            dr["dt9"][key]["trunc"] += 1 if hit else 0
            if key == "drifted" and len(dr["dt9_examples"]) < 30:
                dr["dt9_examples"].append({"blind_id": alert["blind_id"], "corpus": alert["corpus"],
                                           "step": cite["step"], "step_truncated": hit})
    a, b = dr["dt8_dt2"]["verified"], dr["dt8_dt2"]["unverified"]
    dr["dt8_dt2"]["fisher_p"] = fisher_exact([[a["trunc"], a["total"] - a["trunc"]],
                                             [b["trunc"], b["total"] - b["trunc"]]]) if b["total"] else None
    a, b = dr["dt9"]["reverified"], dr["dt9"]["drifted"]
    dr["dt9"]["fisher_p"] = fisher_exact([[a["trunc"], a["total"] - a["trunc"]],
                                         [b["trunc"], b["total"] - b["trunc"]]]) if b["total"] else None

    # ---- A5b block-level: is the block the quote came from truncated? -----
    def locate(blind_id: str, step: int, quote: str) -> dict | None:
        needle = re.sub(r"\s+", " ", quote or "").strip()[:200]
        if not needle:
            return None
        for span in BLOCKS.get(blind_id, []):
            if span["step"] != int(step):
                continue
            hay = re.sub(r"\s+", " ", span["content"])
            if needle in hay:
                return span
        return None

    dr["block_level"] = {
        "dt8_dt2": {k: {"in_truncated_block": 0, "located": 0, "not_located": 0,
                        "kinds": Counter()} for k in ("verified", "unverified")},
        "dt9": {k: {"in_truncated_block": 0, "located": 0, "not_located": 0,
                    "kinds": Counter()} for k in ("reverified", "drifted")},
    }
    for blind_id, lab in labels.items():
        if blind_id not in BLOCKS:
            continue
        for cite in lab["evidence"]:
            if cite.get("step") is None:
                continue
            key = "verified" if cite.get("verified") else "unverified"
            cell = dr["block_level"]["dt8_dt2"][key]
            span = locate(blind_id, cite["step"], cite.get("quote") or "")
            if span is None:
                cell["not_located"] += 1
                continue
            cell["located"] += 1
            cell["kinds"][span["kind"]] += 1
            cell["in_truncated_block"] += 1 if span["truncated"] else 0
    for alert in alerts:
        for cite in alert.get("citations", []):
            key = "reverified" if cite.get("reverified") else "drifted"
            cell = dr["block_level"]["dt9"][key]
            span = locate(alert["blind_id"], cite["step"], cite.get("quote") or "")
            if span is None:
                cell["not_located"] += 1
                continue
            cell["located"] += 1
            cell["kinds"][span["kind"]] += 1
            cell["in_truncated_block"] += 1 if span["truncated"] else 0
    for group in dr["block_level"].values():
        for cell in group.values():
            cell["kinds"] = dict(cell["kinds"])

    # ---- A7 intensity: markers per step and per 1k chars ------------------
    intensity = {}
    for corpus, rows in cohorts.items():
        for r in rows:
            r["markers_per_step"] = r["n_markers"] / max(1, r["n_steps"])
        pos = [r for r in rows if r["positive"]]
        neg = [r for r in rows if not r["positive"]]
        def med(vals: list[float]) -> float:
            s = sorted(vals)
            n = len(s)
            return 0.0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)
        intensity[corpus] = {
            "median_markers_per_step_pos": med([r["markers_per_step"] for r in pos]),
            "median_markers_per_step_neg": med([r["markers_per_step"] for r in neg]),
            "median_markers_pos": med([r["n_markers"] for r in pos]),
            "median_markers_neg": med([r["n_markers"] for r in neg]),
            "median_length_pos": med([r["length"] for r in pos]),
            "median_length_neg": med([r["length"] for r in neg]),
            "spearman_markers_vs_length": _spearman([r["n_markers"] for r in rows],
                                                    [r["length"] for r in rows]),
            "spearman_perstep_vs_length": _spearman([r["markers_per_step"] for r in rows],
                                                    [r["length"] for r in rows]),
            "mw_markers_pos_vs_neg_p": _mann_whitney_p([r["n_markers"] for r in pos],
                                                       [r["n_markers"] for r in neg]),
        }
        # label rate by intensity tertile (markers per step, corpus tertiles)
        cuts = tertiles([r["markers_per_step"] for r in rows])
        cells = {}
        for t in (0, 1, 2):
            sub = [r for r in rows if tertile_of(r["markers_per_step"], cuts) == t]
            k = sum(1 for r in sub if r["positive"])
            cells[str(t)] = {"positive_share": fmt(k, len(sub)),
                             "range": f"<{cuts[0]:.2f}" if t == 0 else
                                      (f"{cuts[0]:.2f}-{cuts[1]:.2f}" if t == 1 else f">={cuts[1]:.2f}")}
        intensity[corpus]["by_intensity_tertile"] = cells

    # ---- A6 flagged CheatBench negatives ---------------------------------
    cb_neg = [r for r in cohorts["cheatbench"] if not r["positive"]]
    flagged = [r for r in cb_neg if r["n_verified"] > 0]
    rest = [r for r in cb_neg if r["n_verified"] == 0]
    a6 = {
        "flagged_n": len(flagged), "other_n": len(rest),
        "flagged_truncated": fmt(sum(r["truncated"] for r in flagged), len(flagged)),
        "other_truncated": fmt(sum(r["truncated"] for r in rest), len(rest)),
        "fisher_p": fisher_exact([[sum(r["truncated"] for r in flagged), len(flagged) - sum(r["truncated"] for r in flagged)],
                                  [sum(r["truncated"] for r in rest), len(rest) - sum(r["truncated"] for r in rest)]]),
        "flagged_median_markers": _median([r["n_markers"] for r in flagged]),
        "other_median_markers": _median([r["n_markers"] for r in rest]),
        "mw_markers_p": _mann_whitney_p([r["n_markers"] for r in flagged], [r["n_markers"] for r in rest]),
        "flagged_median_length": _median([r["length"] for r in flagged]),
        "other_median_length": _median([r["length"] for r in rest]),
        "flagged_mean_length": sum(r["length"] for r in flagged) / max(1, len(flagged)),
        "other_mean_length": sum(r["length"] for r in rest) / max(1, len(rest)),
        "flagged_ids": [r["blind_id"] for r in flagged],
    }

    # ---- DT-11 marker reconciliation --------------------------------------
    bare = [b for b, r in census.items() if r["any_bare_ellipsis"]]
    real = [b for b, r in census.items() if r["n_markers"]]
    recon = {
        "runs_with_bare_ellipsis": len(bare),
        "runs_with_real_marker": len(real),
        "both": len(set(bare) & set(real)),
        "bare_only_no_real_marker": len(set(bare) - set(real)),
        "real_marker_missed_by_bare_test": len(set(real) - set(bare)),
        "runs_total": len(census),
        "bare_in_agent_prose_check": "bare … includes agent prose and '…(N elems)' array elision",
    }

    receipt = {
        "generated_by": "dq1_audit.py reader-effect",
        "rates_by_label": rates,
        "beyond_length": beyond,
        "reader_effect": reader,
        "citations": dr,
        "cheatbench_flagged_negatives": a6,
        "intensity": intensity,
        "dt11_reconciliation": recon,
    }
    (OUT / "reader-effect.json").write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({"totals": census_receipt["totals"], "by_corpus": census_receipt["runs_by_corpus"],
                      "blocks": census_receipt["markers_by_block"],
                      "n_markers_dist": census_receipt["run_distribution"]["n_markers"]}, indent=1))
    print(json.dumps(rates, indent=1))
    print(json.dumps(beyond, indent=1))
    print(json.dumps(reader, indent=1))
    print(json.dumps(dr, indent=1)[:3000])
    print(json.dumps(a6, indent=1))


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _spearman(a: list[float], b: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=False))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def _mann_whitney_p(a: list[float], b: list[float], draws: int = 10000, seed: int = 20261030) -> float | None:
    """Two-sided permutation p for the median difference (deterministic seed)."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    observed = abs(_median(a) - _median(b))
    pool = a + b
    na = len(a)
    hits = 0
    for _ in range(draws):
        rng.shuffle(pool)
        if abs(_median(pool[:na]) - _median(pool[na:])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (draws + 1)


def _sd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


if __name__ == "__main__":
    main()
