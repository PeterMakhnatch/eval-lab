"""Rank mimo-clean-v5 tasks that have no v5 cheat-ladder run and plan the next census (HAR-224).

Read-only, deterministic, standard library only: every input is a committed file under
``research/``; nothing is executed, fetched or modelled. Run from the repository root:

    python research/experiments/mimo-v5-gap-rank-20261029/rank_gap.py            # rewrite outputs
    python research/experiments/mimo-v5-gap-rank-20261029/rank_gap.py --check    # fail on drift

Outputs (this directory): ``gap-ranking.csv``, ``sample-plan.csv``, ``strata.csv``,
``summary.json``. ``README.md`` is the human report and is written by hand from them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
EXP = ROOT / "research/experiments"

SEED = 20261029
TOP_N = 100  # wave A: riskiest tasks, taken in rank order
SAMPLE_N = 120  # wave B: stratified sample of the remaining eligible gap
MAX_PER_GROUP_A = 3  # wave A takes at most this many tasks per repo group (siblings share a grader)
LOCAL_MINUTES_PER_TASK = 15.5  # v5 census README, n=50 local ladder sample (winning-cell median)
LOCAL_WORKERS = 2  # shared Docker daemon: never more than 2 concurrent trials

INPUTS = {
    "manifest": EXP / "mimo-clean-v5/manifest.csv",
    "results": EXP / "mimo-clean-v5-census/results.csv",
    "digest_refresh": EXP / "mimo-clean-v5-census/digest-refresh.csv",
    "verify_detail": EXP / "mimo-clean-v5-census/verify-detail.csv",
    "verify_overrides": EXP / "mimo-clean-v5-census/verify-overrides.csv",
    "v4_results": EXP / "mimo-clean-v4-census/results.csv",
    "v3_results": EXP / "mimo-clean-census/results.csv",
    "v6_manifest": EXP / "mimo-clean-v6/manifest.csv",
    "v6_results": EXP / "mimo-clean-v6/results.csv",
    "v6_fleet": EXP / "mimo-v6-full-fleet/mimo-v6-fleet-census.csv",
    "split": EXP / "har81-mimo-sft/split.json",
    "hack_scan": EXP / "redteam-night/hack-scan-ledger.json",
    "python_ledger": EXP / "python-task-ledger/ledger.csv",
    "download_census": EXP / "code-harden-night/census.json",
    "findings": EXP / "mimo-defect-map-v1/findings.json",
    "predictions": EXP / "mimo-defect-map-v1/predictions.json",
}

# Score = sum(WEIGHTS[c] * component[c]) with every component in [0, 1].
WEIGHTS = {"surface": 30.0, "evidence": 30.0, "leak_env": 20.0, "leverage": 10.0, "prior": -10.0}
UNIVERSAL_PREDICTED = {"grader_tamperable", "unstated_requirement"}  # fire on all 567 packages
LEAK_PY = {"pypi_fix_released": 0.6, "git_only": 0.4, "pypi_package": 0.3, "none_found": 0.0}
CENSUS_LABEL_PY = {"grader_suspect": 0.4, "broken_environment": 0.2}
TRIAGE_HIST = {  # first-missing check recorded before the digest refresh that signals a quirk
    "unverified:noexec",
    "unverified:baseline-unreliable",
    "unverified:pre-existing-failures",
    "unverified:ladder-unscored",
    "unverified:ladder-healthcheck",
    "unverified:ladder-no-reward",
    "unverified:timeout",
}
RETRY_HIST = {
    "unverified:ladder-unscored",
    "unverified:ladder-healthcheck",
    "unverified:ladder-no-reward",
    "unverified:timeout",
}
FAMILIES = ("python", "go", "js_ts", "unknown", "other_typed")
CLASSES = ("known-defect", "triage-note", "grader-surface", "leak-env", "none")
BANDS = ("singleton", "pair-to-4", "five-plus")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tie_key(seed: int, task_id: str) -> str:
    return hashlib.sha256(f"{seed}:{task_id}".encode()).hexdigest()


def family(language: str) -> str:
    if language == "python":
        return "python"
    if language == "go":
        return "go"
    if language in ("javascript", "typescript"):
        return "js_ts"
    if language == "unknown":
        return "unknown"
    return "other_typed"


def repo_band(group_size: int) -> str:
    if group_size <= 1:
        return "singleton"
    return "pair-to-4" if group_size <= 4 else "five-plus"


def historical_label(evidence: str) -> str:
    """Pre-refresh first-missing check, parsed from ``verify-detail.csv`` evidence text."""
    match = re.search(r"historical (\S+?) belongs", evidence)
    if match:
        return match.group(1)
    return evidence.split(";")[0][:60]


def ladder_coverage(
    manifest: dict[str, dict[str, str]],
    results: list[dict[str, str]],
    refresh: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Join census ladder rows to manifest tasks.

    Join keys: ``task_id`` (task level) and ``final_digest`` against the census row's
    ``final_digest`` (exact level). A v5 ladder run is a ``results.csv`` row of
    ``manifest_version == mimo-clean-v5`` with ``ladder_verdict`` in {clean, cracked}.
    """
    scored: dict[str, list[dict[str, str]]] = defaultdict(list)
    unscored: set[str] = set()
    for row in results:
        if row["manifest_version"] != "mimo-clean-v5":
            continue
        if row["ladder_verdict"] in ("clean", "cracked"):
            scored[row["task_id"]].append(row)
        elif row["ladder_verdict"] == "unscored":
            unscored.add(row["task_id"])
    on_old = on_cur = 0
    for task, rows in scored.items():
        digests = {r["final_digest"] for r in rows}
        if refresh.get(task, {}).get("old_digest") in digests:
            on_old += 1
        if manifest[task]["final_digest"] in digests:
            on_cur += 1
    return {
        "scored_tasks": set(scored),
        "unscored_tasks": unscored - set(scored),
        "cracked_tasks": {
            t for t, rs in scored.items() if any(r["ladder_verdict"] == "cracked" for r in rs)
        },
        "bound_to_prerefresh_digest": on_old,
        "bound_to_current_v5_digest": on_cur,
    }


def prior_ladder(path: Path, version: str) -> set[str]:
    return {
        r["task_id"]
        for r in read_csv(path)
        if r["ladder_verdict"] == "clean" and r["manifest_version"] == version
    }


def load_features() -> dict[str, Any]:
    manifest = {r["task_id"]: r for r in read_csv(INPUTS["manifest"])}
    built = {t: r for t, r in manifest.items() if r["status"] == "built"}
    refresh = {r["task_id"]: r for r in read_csv(INPUTS["digest_refresh"])}
    results = read_csv(INPUTS["results"])
    detail = {r["task_id"]: r for r in read_csv(INPUTS["verify_detail"])}
    cover = ladder_coverage(manifest, results, refresh)

    groups = {
        r["task_id"]: r["split_group"].removeprefix("code:")
        for r in read_json(INPUTS["split"])["tasks"]
        if r["domain"] == "code"
    }
    plant = {x["task"]: x for x in read_json(INPUTS["hack_scan"])["items"]}
    pyledger = {r["task_id"]: r for r in read_csv(INPUTS["python_ledger"])}
    downloads = {x["task_id"]: x for x in read_json(INPUTS["download_census"])}
    supported: Counter[str] = Counter()
    for finding in read_json(INPUTS["findings"])["findings"]:
        if finding["evidence_status"] == "source_supported_historical":
            supported[finding["task_id"]] += 1
    predicted = {
        p["task_id"]: sorted(set(p["class_keys"]) - UNIVERSAL_PREDICTED)
        for p in read_json(INPUTS["predictions"])["predictions"]
    }
    overrides = {r["task_id"]: r for r in read_csv(INPUTS["verify_overrides"])}

    # Non-v5 (or successor) ladder evidence, only to prove there is none on current digests.
    v6_manifest = {r["task_id"]: r["final_digest"] for r in read_csv(INPUTS["v6_manifest"])}
    v6_clean = {
        r["task_id"]
        for r in read_csv(INPUTS["v6_results"])
        if r["ladder_verdict"] in ("clean", "cracked")
        and r["final_digest"] == v6_manifest.get(r["task_id"])
    }
    v6_fleet_verified = {
        r["task_id"] for r in read_csv(INPUTS["v6_fleet"]) if r["bucket"] == "v6_verified"
    }
    return {
        "manifest": manifest,
        "built": built,
        "refresh": refresh,
        "detail": detail,
        "cover": cover,
        "groups": groups,
        "plant": plant,
        "pyledger": pyledger,
        "downloads": downloads,
        "supported": supported,
        "predicted": predicted,
        "overrides": overrides,
        "v4_clean": prior_ladder(INPUTS["v4_results"], "mimo-clean-v4"),
        "v3_clean": prior_ladder(INPUTS["v3_results"], "mimo-clean-v3"),
        "v6_ladder_clean": v6_clean,
        "v6_fleet_verified": v6_fleet_verified,
    }


def is_held(row: dict[str, str], hist: str) -> str:
    """Admission holds: never rankable for a clean-set census."""
    if "excluded-from-training" in row["reason"]:
        return "excluded-from-training"
    if hist.startswith("fail:"):
        return "fail-label"
    if hist == "env-broken":
        return "env-broken"
    return ""


def components(task: str, feat: dict[str, Any], gap_group_size: int) -> dict[str, float]:
    row = feat["built"][task]
    fam = family(row["language"])
    hist = historical_label(feat["detail"][task]["evidence"])
    # pytest family: conftest/skip planting is closed by purge + separate-verifier
    surface = 1 / 5 if fam == "python" else feat["plant"][task]["plant_weight"] / 5
    n_sup = feat["supported"].get(task, 0)
    n_pred = len(feat["predicted"].get(task, []))
    evidence = 0.7 * min(n_sup, 3) / 3 + 0.3 * min(n_pred, 3) / 3
    leak = 0.0
    if fam == "python":
        led = feat["pyledger"][task]
        leak += LEAK_PY[led["leak_channel"]] + CENSUS_LABEL_PY.get(led["census_label"], 0.0)
    else:
        dl = feat["downloads"][task]
        leak += 0.4 if dl["unconditional_tools"] else (0.2 if dl["downloads"] else 0.0)
    ov = feat["overrides"].get(task)
    quirk_override = ov is not None and not ov["verify"].startswith(("pass", "fail:", "env-broken"))
    if hist in TRIAGE_HIST or quirk_override:
        leak += 0.4
    leak = min(leak, 1.0)
    leverage = min(math.log2(max(gap_group_size, 1)), 3) / 3
    if task in feat["v4_clean"]:
        prior = 1.0
    elif task in feat["v3_clean"]:
        prior = 0.5
    else:
        prior = 0.0
    return {
        "surface": surface,
        "evidence": evidence,
        "leak_env": leak,
        "leverage": leverage,
        "prior": prior,
    }


def score(comp: dict[str, float], weights: dict[str, float] = WEIGHTS) -> float:
    return sum(weights[k] * comp[k] for k in weights)


def finding_class(task: str, comp: dict[str, float], feat: dict[str, Any]) -> str:
    """Primary static-finding class, first match wins (documented precedence)."""
    hist = historical_label(feat["detail"][task]["evidence"])
    if feat["supported"].get(task, 0) > 0:
        return "known-defect"
    if hist in TRIAGE_HIST or task in feat["overrides"]:
        return "triage-note"
    fam = family(feat["built"][task]["language"])
    if fam != "python" and feat["plant"][task]["plant_weight"] >= 3:
        return "grader-surface"
    if comp["leak_env"] >= 0.3:
        return "leak-env"
    return "none"


def rank_gap(feat: dict[str, Any], seed: int = SEED) -> list[dict[str, Any]]:
    cover = feat["cover"]
    gap = [t for t in sorted(feat["built"]) if t not in cover["scored_tasks"]]
    held = {
        t: is_held(feat["built"][t], historical_label(feat["detail"][t]["evidence"])) for t in gap
    }
    eligible = [t for t in gap if not held[t]]
    group_count = Counter(feat["groups"][t] for t in eligible)
    all_count = Counter(feat["groups"][t] for t in feat["built"])
    rows: list[dict[str, Any]] = []
    for task in gap:
        comp = components(task, feat, group_count.get(feat["groups"][task], 1))
        row = feat["built"][task]
        hist = historical_label(feat["detail"][task]["evidence"])
        rows.append(
            {
                "task_id": task,
                "language": row["language"],
                "domain_family": family(row["language"]),
                "repo_group": feat["groups"][task],
                "repo_group_gap_tasks": group_count.get(feat["groups"][task], 0),
                "repo_group_all_tasks": all_count[feat["groups"][task]],
                "repo_band": repo_band(group_count.get(feat["groups"][task], 1)),
                "has_reference_fix": "yes" if row["reference_fix"] not in ("", "none") else "no",
                "historical_label": hist,
                "hold": held[task],
                "retry_flag": "yes"
                if hist in RETRY_HIST or task in cover["unscored_tasks"]
                else "no",
                "prior_ladder": "v4-clean"
                if task in feat["v4_clean"]
                else ("v3-clean" if task in feat["v3_clean"] else "none"),
                "supported_findings": feat["supported"].get(task, 0),
                "predicted_classes": "|".join(feat["predicted"].get(task, [])),
                "plant_vector": feat["plant"][task]["plant_vector"]
                if task in feat["plant"]
                else "",
                **{f"c_{k}": round(v, 4) for k, v in comp.items()},
                "score": round(score(comp), 3),
                "finding_class": finding_class(task, comp, feat),
                "tie_key": tie_key(seed, task),
            }
        )
    eligible_rows = sorted(
        (r for r in rows if not r["hold"]), key=lambda r: (-r["score"], r["tie_key"])
    )
    held_rows = sorted((r for r in rows if r["hold"]), key=lambda r: r["task_id"])
    for i, r in enumerate(eligible_rows, 1):
        r["rank"] = i
    for r in held_rows:
        r["rank"] = ""
    return eligible_rows + held_rows


def largest_remainder(
    total: int, weights: dict[Any, float], caps: dict[Any, int], floor: int = 0
) -> dict[Any, int]:
    """Integer allocation proportional to ``weights``; each key within [min(floor, cap), cap]."""
    keys = [k for k in weights if caps[k] > 0]
    alloc = {k: min(floor, caps[k]) for k in keys}
    remaining = total - sum(alloc.values())
    while remaining > 0:
        open_keys = [k for k in keys if alloc[k] < caps[k]]
        if not open_keys:
            break
        wsum = sum(weights[k] for k in open_keys)
        quota = {k: remaining * weights[k] / wsum for k in open_keys}
        base = {k: min(int(quota[k]), caps[k] - alloc[k]) for k in open_keys}
        for k in open_keys:
            alloc[k] += base[k]
        remaining -= sum(base.values())
        if remaining <= 0:
            break
        frac = sorted(open_keys, key=lambda k: (-(quota[k] - int(quota[k])), str(k)))
        for k in frac:
            if remaining <= 0:
                break
            if alloc[k] < caps[k]:
                alloc[k] += 1
                remaining -= 1
    return alloc


def plan_sample(
    ranked: list[dict[str, Any]], seed: int = SEED, top_n: int = TOP_N, sample_n: int = SAMPLE_N
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wave A = top ``top_n`` by score, at most MAX_PER_GROUP_A per repo group.

    Wave B = nested stratified sample of eligible tasks whose repo group is not in wave A:
    domain_family -> finding_class (sqrt-proportional, floor 1) -> repo band (proportional),
    one task per repo group.
    """
    eligible = [r for r in ranked if not r["hold"]]
    plan: list[dict[str, Any]] = []
    used_groups: set[str] = set()
    taken_a: Counter[str] = Counter()
    for r in eligible:
        if len(plan) >= top_n:
            break
        if taken_a[r["repo_group"]] >= MAX_PER_GROUP_A:
            continue
        taken_a[r["repo_group"]] += 1
        plan.append({**r, "wave": "A", "stratum": "top-risk"})
        used_groups.add(r["repo_group"])
    pool = [r for r in eligible if r["repo_group"] not in used_groups]
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in pool:
        cells[(r["domain_family"], r["finding_class"])].append(r)
    cell_alloc = largest_remainder(
        sample_n,
        {k: math.sqrt(len(v)) for k, v in cells.items()},
        {k: len(v) for k, v in cells.items()},
        floor=1,
    )
    strata: list[dict[str, Any]] = []
    for cell in sorted(cells):
        members = cells[cell]
        by_band: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in members:
            by_band[r["repo_band"]].append(r)
        band_alloc = largest_remainder(
            cell_alloc.get(cell, 0),
            {b: float(len(v)) for b, v in by_band.items()},
            {b: len(v) for b, v in by_band.items()},
        )
        for band in BANDS:
            candidates = sorted(by_band.get(band, []), key=lambda r: r["tie_key"])
            want = band_alloc.get(band, 0)
            chosen: list[dict[str, Any]] = []
            for r in candidates:
                if len(chosen) >= want:
                    break
                if r["repo_group"] in used_groups:
                    continue
                chosen.append(r)
                used_groups.add(r["repo_group"])
            for r in chosen:
                plan.append({**r, "wave": "B", "stratum": "|".join((*cell, band))})
            strata.append(
                {
                    "domain_family": cell[0],
                    "finding_class": cell[1],
                    "repo_band": band,
                    "population": len(by_band.get(band, [])),
                    "cell_population": len(members),
                    "allocated": want,
                    "selected": len(chosen),
                }
            )
    return plan, strata


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = average_ranks(a), average_ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va and vb else float("nan")


def sensitivity(ranked: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    eligible = [r for r in ranked if not r["hold"]]
    comps = [{k: r[f"c_{k}"] for k in WEIGHTS} for r in eligible]
    base = [score(c) for c in comps]
    base_top = {r["task_id"] for r in eligible[:top_n]}
    variants: dict[str, dict[str, float]] = {f"drop {k}": {**WEIGHTS, k: 0.0} for k in WEIGHTS}
    variants["equal weights (25/25/25/25, no prior)"] = {
        "surface": 25.0,
        "evidence": 25.0,
        "leak_env": 25.0,
        "leverage": 25.0,
        "prior": 0.0,
    }
    variants["evidence x2"] = {**WEIGHTS, "evidence": 60.0}
    variants["surface x2"] = {**WEIGHTS, "surface": 60.0}
    out = []
    in_top: Counter[str] = Counter()
    for name, w in variants.items():
        alt = [score(c, w) for c in comps]
        top = {
            eligible[i]["task_id"]
            for i in sorted(range(len(alt)), key=lambda i: (-alt[i], eligible[i]["tie_key"]))[
                :top_n
            ]
        }
        in_top.update(top)
        out.append(
            {
                "variant": name,
                "spearman_vs_baseline": round(spearman(base, alt), 4),
                "top_overlap": len(base_top & top),
                "top_n": top_n,
            }
        )
    for r in ranked:
        r["top_in_variants"] = "" if r["hold"] else in_top.get(r["task_id"], 0)
    return out


def reason(row: dict[str, Any]) -> str:
    """One-line, human-readable justification of a row's score from its components."""
    parts: list[str] = []
    if row["c_surface"] >= 0.6 and row["plant_vector"]:
        parts.append(f"grader surface: {row['plant_vector']}")
    if row["supported_findings"]:
        parts.append(f"{row['supported_findings']} source-supported finding(s)")
    if row["predicted_classes"]:
        parts.append("predicted: " + row["predicted_classes"].replace("|", ","))
    if row["c_leak_env"] >= 0.3:
        parts.append(f"leak/env quirk {row['c_leak_env']}")
    if row["repo_group_gap_tasks"] >= 5:
        parts.append(f"repo group of {row['repo_group_gap_tasks']} gap tasks")
    if row["prior_ladder"] != "none":
        parts.append(f"credit: prior {row['prior_ladder']}")
    return "; ".join(parts) or "no static signal above baseline"


def build(seed: int = SEED) -> dict[str, Any]:
    feat = load_features()
    ranked = rank_gap(feat, seed)
    sens = sensitivity(ranked)
    for r in ranked:
        r["reason"] = reason(r)
    plan, strata = plan_sample(ranked, seed)
    cover = feat["cover"]
    built = feat["built"]
    gap_all = [r for r in ranked]
    eligible = [r for r in ranked if not r["hold"]]
    hist_gap = Counter(r["historical_label"] for r in gap_all)
    summary = {
        "seed": seed,
        "weights": WEIGHTS,
        "inputs_sha256": {k: sha256_file(p) for k, p in sorted(INPUTS.items())},
        "population": {
            "manifest_rows": len(feat["manifest"]),
            "built": len(built),
            "v5_ladder_scored_tasks": len(cover["scored_tasks"]),
            "v5_ladder_cracked_tasks": len(cover["cracked_tasks"]),
            "v5_ladder_unscored_only_tasks": len(cover["unscored_tasks"]),
            "scored_bound_to_prerefresh_v5_digest": cover["bound_to_prerefresh_digest"],
            "scored_bound_to_current_v5_digest": cover["bound_to_current_v5_digest"],
            "v6_digests_with_ladder_run": len(feat["v6_ladder_clean"]),
            "v6_fleet_verified_tasks": len(feat["v6_fleet_verified"]),
            "gap_tasks": len(gap_all),
            "gap_held": len(gap_all) - len(eligible),
            "gap_held_by_reason": dict(Counter(r["hold"] for r in gap_all if r["hold"])),
            "gap_eligible": len(eligible),
            "gap_historical_label": dict(sorted(hist_gap.items(), key=lambda kv: (-kv[1], kv[0]))),
            "gap_with_prior_v4_clean": sum(1 for r in eligible if r["prior_ladder"] == "v4-clean"),
            "gap_with_prior_v3_clean": sum(1 for r in eligible if r["prior_ladder"] == "v3-clean"),
            "eligible_by_family": dict(Counter(r["domain_family"] for r in eligible)),
            "eligible_by_finding_class": dict(Counter(r["finding_class"] for r in eligible)),
            "eligible_by_band": dict(Counter(r["repo_band"] for r in eligible)),
            "eligible_with_supported_finding": sum(1 for r in eligible if r["supported_findings"]),
            "eligible_retry_flag": sum(1 for r in eligible if r["retry_flag"] == "yes"),
        },
        "plan": {
            "top_n": TOP_N,
            "sample_n": SAMPLE_N,
            "wave_a": sum(1 for p in plan if p["wave"] == "A"),
            "wave_b": sum(1 for p in plan if p["wave"] == "B"),
            "distinct_repo_groups": len({p["repo_group"] for p in plan}),
            "wave_a_by_family": dict(Counter(p["domain_family"] for p in plan if p["wave"] == "A")),
            "wave_a_by_finding_class": dict(
                Counter(p["finding_class"] for p in plan if p["wave"] == "A")
            ),
            "wave_b_by_family": dict(Counter(p["domain_family"] for p in plan if p["wave"] == "B")),
            "wave_b_by_finding_class": dict(
                Counter(p["finding_class"] for p in plan if p["wave"] == "B")
            ),
            "local_minutes_per_task": LOCAL_MINUTES_PER_TASK,
            "local_workers": LOCAL_WORKERS,
            "wall_hours_wave_a": round(TOP_N * LOCAL_MINUTES_PER_TASK / 60 / LOCAL_WORKERS, 1),
            "wall_hours_wave_b": round(SAMPLE_N * LOCAL_MINUTES_PER_TASK / 60 / LOCAL_WORKERS, 1),
            "zero_crack_upper_bound_95_wave_b": round(1 - 0.05 ** (1 / SAMPLE_N), 4),
        },
        "sensitivity": sens,
    }
    return {"ranked": ranked, "plan": plan, "strata": strata, "summary": summary}


RANK_COLUMNS = [
    "rank",
    "task_id",
    "score",
    "language",
    "domain_family",
    "finding_class",
    "repo_group",
    "repo_group_gap_tasks",
    "repo_group_all_tasks",
    "repo_band",
    "has_reference_fix",
    "historical_label",
    "prior_ladder",
    "retry_flag",
    "supported_findings",
    "predicted_classes",
    "plant_vector",
    "c_surface",
    "c_evidence",
    "c_leak_env",
    "c_leverage",
    "c_prior",
    "top_in_variants",
    "hold",
    "reason",
]
PLAN_COLUMNS = [
    "wave",
    "stratum",
    "rank",
    "task_id",
    "score",
    "domain_family",
    "finding_class",
    "repo_group",
    "repo_band",
    "language",
    "has_reference_fix",
    "historical_label",
    "retry_flag",
    "reason",
]
STRATA_COLUMNS = [
    "domain_family",
    "finding_class",
    "repo_band",
    "population",
    "cell_population",
    "allocated",
    "selected",
]


def render_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render_all(result: dict[str, Any]) -> dict[str, str]:
    plan = sorted(result["plan"], key=lambda r: (r["wave"], r["rank"]))
    return {
        "gap-ranking.csv": render_csv(result["ranked"], RANK_COLUMNS),
        "sample-plan.csv": render_csv(plan, PLAN_COLUMNS),
        "strata.csv": render_csv(result["strata"], STRATA_COLUMNS),
        "summary.json": json.dumps(result["summary"], indent=2, sort_keys=True) + "\n",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if committed outputs differ")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    outputs = render_all(build(args.seed))
    drift = [
        n
        for n, text in outputs.items()
        if not (HERE / n).exists() or (HERE / n).read_text() != text
    ]
    if args.check:
        print("drift: " + ", ".join(drift) if drift else "outputs reproduce")
        return 1 if drift else 0
    for name, text in outputs.items():
        (HERE / name).write_text(text, encoding="utf-8")
    print("wrote " + ", ".join(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
