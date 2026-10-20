"""Leaderboard failure patterns as a task-defect signal on TB4, scored against tb4-audit-v1.

Phases (``--phase``):
  coverage  per-trial failed-test extraction and per-task features; reads NO labels.
  score     joins the task features to the key labels, fits the dev threshold, evaluates
            on test, writes results.json, report.md, flagged.jsonl and the audit sample.

The design (features, labels, threshold rule, metrics) is declared in README.md before
``--phase score`` is run; its hash is written into results.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evallab.analysis_statistics import wilson_score_interval

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-failure-clusters"
KEY = REPO / "research/answer-keys/tb4-audit-v1"
SEED = 20261020
MIN_FAILS = 20  # a task needs this many failed trials with per-test evidence
MIN_TESTS = 3  # fewer tests make "the same failed set" trivially true
NEAR_MISS_MAX_FAILED = 2
STRONG_ROWS = 10  # leaderboard rows with the highest accuracy
PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s|\s*$)", re.M)


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rate(k: int, n: int) -> dict[str, Any]:
    if n == 0:
        return {"k": 0, "n": 0, "rate": None, "wilson95": None}
    ci = wilson_score_interval(k, n)
    return {"k": k, "n": n, "rate": k / n, "wilson95": [ci.lower, ci.upper]}


def fmt(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "–"
    lo, hi = r["wilson95"]
    return f"{r['k']}/{r['n']} = {r['rate']:.0%} [{lo:.0%}, {hi:.0%}]"


# ------------------------------------------------------------- evidence extraction


def failed_tests(trial_dir: Path) -> tuple[frozenset[str] | None, int | None, str]:
    """(failed test names, total tests, evidence source) from the verifier output."""
    ctrf = trial_dir / "verifier/ctrf.json"
    if ctrf.is_file():
        try:
            tests = json.loads(ctrf.read_text(encoding="utf-8"))["results"]["tests"]
        except (KeyError, ValueError):
            tests = None
        if tests:
            failed = frozenset(t["name"] for t in tests if t.get("status") not in ("passed", "skipped"))
            return failed, len(tests), "ctrf"
    stdout = trial_dir / "verifier/test-stdout.txt"
    if stdout.is_file():
        text = stdout.read_text(encoding="utf-8", errors="replace")
        failed = frozenset(PYTEST_FAILED.findall(text))
        passed = len(re.findall(r"^PASSED\s", text, re.M))
        if failed:
            return failed, len(failed) + passed if passed else None, "pytest-stdout"
    return None, None, "none"


def load_trials(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in inventory:
        trial_dir = RESULTS / "trials-lite" / item["task"] / item["trial_name"]
        failed, total, source = failed_tests(trial_dir) if trial_dir.is_dir() else (None, None, "missing")
        out.append({**item, "failed": sorted(failed) if failed is not None else None,
                    "n_tests": total, "evidence": source})
    return out


# --------------------------------------------------------------------- task features


def task_features(trials: list[dict[str, Any]], strong_rows: set[str]) -> dict[str, Any]:
    fails = [t for t in trials if t["reward"] == 0]
    passes = [t for t in trials if t["reward"] is not None and t["reward"] >= 1]
    with_evidence = [t for t in fails if t["failed"]]
    n_tests = max((t["n_tests"] or 0 for t in trials), default=0)
    out: dict[str, Any] = {
        "trials": len(trials), "passes": len(passes), "fails": len(fails),
        "fails_with_evidence": len(with_evidence), "n_tests": n_tests,
        "fail_rate": len(fails) / max(1, len(fails) + len(passes)),
        "evidence": dict(Counter(t["evidence"] for t in fails)),
    }
    informative = len(with_evidence) >= MIN_FAILS and n_tests >= MIN_TESTS
    out["informative"] = informative
    if not with_evidence:
        return out
    sets = Counter(tuple(t["failed"]) for t in with_evidence)
    modal_set, modal_count = sets.most_common(1)[0]
    test_counts = Counter(name for t in with_evidence for name in t["failed"])
    modal_test, modal_test_count = test_counts.most_common(1)[0]
    near = [t for t in with_evidence
            if len(t["failed"]) <= NEAR_MISS_MAX_FAILED and (t["n_tests"] is None or len(t["failed"]) <= 0.1 * t["n_tests"])]
    in_modal = [t for t in with_evidence if tuple(t["failed"]) == modal_set]
    out.update({
        "modal_set": list(modal_set),
        "modal_set_share": modal_count / len(with_evidence),
        "modal_test": modal_test,
        "modal_test_share": modal_test_count / len(with_evidence),
        "near_miss_share": len(near) / len(with_evidence),
        "modal_rows": len({t["row_id"] for t in in_modal}),
        "modal_strong_rows": len({t["row_id"] for t in in_modal if t["row_id"] in strong_rows}),
        "modal_set_fails": modal_count,
        "modal_set_trial_ids": [t["trial_id"] for t in in_modal],
    })
    return out


def build_features() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    inventory = json.loads((RESULTS / "inventory-all.json").read_text())
    trials = load_trials(inventory)
    accuracy: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in trials:
        if t["reward"] is not None:
            accuracy[t["row_id"]][0] += t["reward"] >= 1
            accuracy[t["row_id"]][1] += 1
    strong = set(sorted(accuracy, key=lambda r: -accuracy[r][0] / accuracy[r][1])[:STRONG_ROWS])
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trials:
        by_task[t["task"]].append(t)
    features = {task: task_features(rows, strong) for task, rows in sorted(by_task.items())}
    coverage = {
        "trials": len(trials),
        "evidence": dict(Counter(t["evidence"] for t in trials)),
        "tasks": len(features),
        "informative_tasks": sum(f["informative"] for f in features.values()),
        "uninformative": {task: {"fails_with_evidence": f["fails_with_evidence"], "n_tests": f["n_tests"]}
                          for task, f in features.items() if not f["informative"]},
    }
    return features, trials, coverage


# ------------------------------------------------------------------------ scoring


def auroc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def bootstrap_auroc(rows: list[tuple[float, bool]], rng: random.Random, n_boot: int = 2000) -> list[float] | None:
    samples = []
    for _ in range(n_boot):
        draw = [rows[rng.randrange(len(rows))] for _ in rows]
        value = auroc([s for s, y in draw if y], [s for s, y in draw if not y])
        if value is not None:
            samples.append(value)
    if len(samples) < n_boot // 2:
        return None
    samples.sort()
    return [samples[int(0.025 * (len(samples) - 1))], samples[int(0.975 * (len(samples) - 1))]]


SCORES = ("modal_set_share", "modal_test_share", "near_miss_share", "fail_rate")
PRIMARY = "modal_set_share"


def labels() -> dict[str, dict[str, Any]]:
    tasks = jsonl(KEY / "tasks.jsonl")
    items = jsonl(KEY / "items.jsonl")
    ambiguity = {i["task"] for i in items if i["scope"] == "task" and "ambiguity" in i["pm_types"]}
    epoch_fn = {t["task"] for t in tasks if any(":FN:" in f for f in t["epoch_flag_ids"])}
    out = {}
    for t in tasks:
        name = t["task"]
        out[name] = {
            "split": t["split"],
            "reviewed": t["pm_review_status"] != "Not reviewed yet.",
            "pm_ambiguity": name in ambiguity,
            "epoch_fn": name in epoch_fn,
            "any_wrong_fail": name in ambiguity or name in epoch_fn,
        }
    return out


def dev_threshold(rows: list[tuple[float, bool]]) -> dict[str, Any]:
    """Lowest score whose dev precision is at least 0.8 with at least 3 flagged tasks;
    otherwise the score maximising dev F1. Declared before the test split is scored."""
    candidates = sorted({s for s, _ in rows})
    positives = sum(y for _, y in rows)
    best, best_f1 = None, -1.0
    for c in candidates:
        flagged = [(s, y) for s, y in rows if s >= c]
        k = sum(y for _, y in flagged)
        if len(flagged) >= 3 and k / len(flagged) >= 0.8:
            return {"rule": "precision>=0.8", "threshold": c}
        precision = k / len(flagged)
        recall = k / positives if positives else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if f1 > best_f1:
            best, best_f1 = c, f1
    return {"rule": "max_dev_f1", "threshold": best}


def evaluate(features: dict[str, dict[str, Any]], labelled: dict[str, dict[str, Any]], target: str,
             rng: random.Random, scores: tuple[str, ...] = SCORES, primary: str = PRIMARY,
             require_informative: bool = True) -> dict[str, Any]:
    usable = {t: f for t, f in features.items()
              if (f["informative"] or not require_informative) and labelled[t]["reviewed"]}
    out: dict[str, Any] = {"target": target, "tasks_scored": len(usable),
                           "positives": sum(labelled[t][target] for t in usable)}
    for split in ("dev", "test"):
        part = [t for t in usable if labelled[t]["split"] == split]
        out[split] = {"tasks": len(part), "positives": sum(labelled[t][target] for t in part)}
    out["auroc"] = {}
    for score in scores:
        per_split = {}
        for split in ("dev", "test", "all"):
            part = [t for t in usable if split == "all" or labelled[t]["split"] == split]
            rows = [(usable[t][score], labelled[t][target]) for t in part]
            value = auroc([s for s, y in rows if y], [s for s, y in rows if not y])
            per_split[split] = {"value": value, "ci95": bootstrap_auroc(rows, rng) if value is not None else None}
        out["auroc"][score] = per_split
    dev_rows = [(usable[t][primary], labelled[t][target]) for t in usable if labelled[t]["split"] == "dev"]
    rule = dev_threshold(dev_rows)
    out["threshold"] = rule
    out["primary_score"] = primary
    flagged = {t for t, f in usable.items() if f[primary] >= rule["threshold"]}
    for split in ("dev", "test", "all"):
        part = {t for t in usable if split == "all" or labelled[t]["split"] == split}
        hit = {t for t in part & flagged if labelled[t][target]}
        out[f"flag_{split}"] = {
            "flagged": len(part & flagged),
            "precision": rate(len(hit), len(part & flagged)),
            "recall": rate(len(hit), sum(labelled[t][target] for t in part)),
            "false_flags_not_in_key": sorted(t for t in part & flagged if not labelled[t][target]),
        }
    return out


POSTHOC_SCORES = ("low_discrimination", "low_strong_weak_gap", "fail_rate")


def pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (sxx * syy) ** 0.5


def posthoc_features(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Item-discrimination scores on pass/fail only; defined for every task, ctrf or not.

    ``low_discrimination`` is minus the correlation, over the 35 rows, between a row's
    pass rate on the task and its accuracy on the other tasks. ``low_strong_weak_gap`` is
    minus the pass-rate gap between the 10 strongest and the 10 weakest rows (ranked on the
    other tasks). Both are high when the task does not separate strong from weak solvers.
    """
    scored = [t for t in trials if t["reward"] is not None]
    cell: dict[tuple[str, str], list[int]] = defaultdict(list)
    for t in scored:
        cell[(t["task"], t["row_id"])].append(int(t["reward"] >= 1))
    tasks = sorted({t["task"] for t in scored})
    rows = sorted({t["row_id"] for t in scored})
    wins = {r: sum(sum(cell[(k, r)]) for k in tasks) for r in rows}
    count = {r: sum(len(cell[(k, r)]) for k in tasks) for r in rows}
    out = {}
    for task in tasks:
        pass_rate = {r: sum(cell[(task, r)]) / len(cell[(task, r)]) for r in rows if cell[(task, r)]}
        ability = {r: (wins[r] - sum(cell[(task, r)])) / (count[r] - len(cell[(task, r)])) for r in pass_rate}
        order = sorted(pass_rate, key=lambda r: ability[r])
        weak, strong = order[:10], order[-10:]
        gap = sum(pass_rate[r] for r in strong) / 10 - sum(pass_rate[r] for r in weak) / 10
        corr = pearson([pass_rate[r] for r in order], [ability[r] for r in order])
        total_pass = sum(sum(cell[(task, r)]) for r in pass_rate)
        total = sum(len(cell[(task, r)]) for r in pass_rate)
        out[task] = {"informative": True, "low_discrimination": None if corr is None else -corr,
                     "low_strong_weak_gap": -gap, "fail_rate": 1 - total_pass / total,
                     "discrimination": corr, "strong_weak_gap": gap}
    return out


def run_level_check(features: dict[str, dict[str, Any]], trials: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Per census task: how the modal failed set overlaps the RC-1 defect-fail labels.

    RC-1 labels come from Preference Model's own statements, so this is agreement with a
    second, partly independent labelling, not ground truth.
    """
    path = RESULTS.parent / "tb4-run-census/labels-fail.jsonl"
    if not path.is_file():
        return None
    rc1 = {row["trial_id"]: row["label"] for row in jsonl(path)}
    task_of = {t["trial_id"]: t["task"] for t in trials}
    per_task: dict[str, dict[str, int]] = {}
    for task in sorted({task_of[tid] for tid in rc1}):
        modal = set(features[task].get("modal_set_trial_ids", []))
        defect = {tid for tid, label in rc1.items() if task_of[tid] == task and label == "task_defect"}
        fails = {tid for tid in rc1 if task_of[tid] == task}
        per_task[task] = {"rc1_fails": len(fails), "rc1_defect_fails": len(defect),
                          "modal_set_fails": len(modal), "both": len(modal & defect)}
    return {"per_task": per_task}


def render(result: dict[str, Any]) -> str:
    out = ["# Failure patterns as a task-defect signal on TB4: tables", "",
           f"Population: {result['coverage']['tasks']} tasks, {result['coverage']['trials']:,} trials; "
           f"{result['coverage']['informative_tasks']} tasks have at least {MIN_FAILS} failed trials with "
           f"per-test evidence and at least {MIN_TESTS} tests.", ""]
    for target in ("pm_ambiguity", "any_wrong_fail", "epoch_fn"):
        ev = result["evaluation"][target]
        out += [f"## Target: {target} ({ev['positives']}/{ev['tasks_scored']} scored tasks positive)", "",
                "| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |", "|---|---|---|---|"]
        for score in SCORES:
            a = ev["auroc"][score]

            def cell(x: dict[str, Any]) -> str:
                return "–" if x["value"] is None else f"{x['value']:.2f}"
            ci = a["all"]["ci95"]
            out.append(f"| {score} | {cell(a['dev'])} | {cell(a['test'])} | {cell(a['all'])}"
                       + (f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "") + " |")
        out += ["", f"Threshold from dev ({ev['threshold']['rule']}): {PRIMARY} ≥ {ev['threshold']['threshold']:.3f}", "",
                "| split | flagged | precision | recall |", "|---|---|---|---|"]
        for split in ("dev", "test", "all"):
            f = ev[f"flag_{split}"]
            out.append(f"| {split} | {f['flagged']} | {fmt(f['precision'])} | {fmt(f['recall'])} |")
        out.append("")
    return "\n".join(out) + "\n"


def render_posthoc(result: dict[str, Any]) -> str:
    out = ["# Post hoc: item-discrimination scores (defined for all tasks)", "",
           f"{result['tasks']} tasks scored. Added after the primary results; "
           "3 scores × 3 targets were tried, so read intervals as exploratory.", ""]
    for target, ev in result["evaluation"].items():
        out += [f"## Target: {target} ({ev['positives']}/{ev['tasks_scored']} positive)", "",
                "| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |", "|---|---|---|---|"]
        for score in POSTHOC_SCORES:
            a = ev["auroc"][score]

            def cell(x: dict[str, Any]) -> str:
                return "–" if x["value"] is None else f"{x['value']:.2f}"
            ci = a["all"]["ci95"]
            out.append(f"| {score} | {cell(a['dev'])} | {cell(a['test'])} | {cell(a['all'])}"
                       + (f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "") + " |")
        out += ["", f"Threshold from dev ({ev['threshold']['rule']}): {ev['primary_score']} ≥ {ev['threshold']['threshold']:.3f}", "",
                "| split | flagged | precision | recall |", "|---|---|---|---|"]
        for split in ("dev", "test", "all"):
            f = ev[f"flag_{split}"]
            out.append(f"| {split} | {f['flagged']} | {fmt(f['precision'])} | {fmt(f['recall'])} |")
        out.append("")
    return "\n".join(out) + "\n"


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("coverage", "score", "posthoc"), required=True)
    args = parser.parse_args()
    features, trials, coverage = build_features()
    if args.phase == "coverage":
        print(json.dumps(coverage, indent=1, sort_keys=True))
        (RESULTS / "coverage.json").write_text(json.dumps(coverage, indent=1, sort_keys=True) + "\n")
        return
    if args.phase == "posthoc":
        rng = random.Random(SEED)
        labelled = labels()
        ph = posthoc_features(trials)
        ph = {t: f for t, f in ph.items() if f["low_discrimination"] is not None}
        evaluation = {target: evaluate(ph, labelled, target, rng, POSTHOC_SCORES, "low_discrimination", False)
                      for target in ("pm_ambiguity", "any_wrong_fail", "epoch_fn")}
        result = {"schema": "tb4-failure-clusters/posthoc/v1", "seed": SEED, "declared": "post hoc, after primary results",
                  "tasks": len(ph), "evaluation": evaluation, "features": ph}
        (HERE / "posthoc.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        (HERE / "posthoc.md").write_text(render_posthoc(result))
        print(render_posthoc(result))
        return
    rng = random.Random(SEED)
    labelled = labels()
    evaluation = {target: evaluate(features, labelled, target, rng)
                  for target in ("pm_ambiguity", "any_wrong_fail", "epoch_fn")}
    result: dict[str, Any] = {
        "schema": "tb4-failure-clusters/v1", "seed": SEED,
        "declaration_sha256": sha(HERE / "README.md"),
        "constants": {"min_fails": MIN_FAILS, "min_tests": MIN_TESTS, "near_miss_max_failed": NEAR_MISS_MAX_FAILED,
                      "strong_rows": STRONG_ROWS, "primary_score": PRIMARY},
        "coverage": coverage, "evaluation": evaluation,
        "rc1_overlap": run_level_check(features, trials),
        "features": {t: {k: v for k, v in f.items() if k != "modal_set_trial_ids"} for t, f in features.items()},
    }
    (HERE / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "report.md").write_text(render(result))
    (RESULTS / "features-full.json").write_text(json.dumps(features, sort_keys=True) + "\n")
    print(render(result))


if __name__ == "__main__":
    main()
