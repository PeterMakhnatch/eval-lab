"""Matched-row length controls for DetBench; no model calls or optional dependencies.

Higher scores indicate label 1. Call evaluate separately per set and for pooled
populations. Freeze the returned strata_edges for subsequent bootstrap/comparison.
The DEV logistic fit uses a fixed 1e-4 L2 penalty on its standardized slope.
"""
from __future__ import annotations

import bisect
import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any


def _number(value: Any, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return float(value)


def _label(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError("label must be integer 0 or 1")
    return value


@dataclass(frozen=True)
class MetricRow:
    run_key: str
    task: str
    label: int
    score: float
    n_chars: float
    n_steps: float

    def __post_init__(self) -> None:
        if not isinstance(self.run_key, str) or not self.run_key:
            raise ValueError("run_key must be a nonempty string")
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("task must be a nonempty globally namespaced task ID")
        _label(self.label)
        _number(self.score, "score")
        _number(self.n_chars, "n_chars", nonnegative=True)
        _number(self.n_steps, "n_steps", nonnegative=True)


def prepare_rows(
    runs: Mapping[str, Mapping[str, Any]], scores: Mapping[str, Any]
) -> tuple[list[MetricRow], dict[str, Any]]:
    """Join once for all controls. Missing values exclude rows; invalid values raise.

    Exclusion counts are first-failure counts, in label/chars/steps/score order.
    Scores without a runs row are reported, never silently added to the cohort.
    Task identity prefers task, then task_id, then cluster (first non-None).
    The set namespace prevents cross-set collisions. Cluster may be run-specific;
    callers must supply the actual task when the source cluster is not a task.
    """
    rows = []
    excluded: Counter[str] = Counter()
    for key in sorted(runs):
        run = runs[key]
        if run.get("run_key", key) != key:
            raise ValueError(f"run_key mismatch: {key}")
        missing = next((name for name in ("label", "n_chars", "n_steps") if run.get(name) is None), None)
        if missing:
            excluded[f"missing_{missing}"] += 1
            continue
        y = _label(run["label"])
        chars = _number(run["n_chars"], "n_chars", nonnegative=True)
        steps = _number(run["n_steps"], "n_steps", nonnegative=True)
        value = scores.get(key)
        if isinstance(value, Mapping):
            if value.get("run_key", key) != key:
                raise ValueError(f"signal run_key mismatch: {key}")
            value = value.get("score")
        if value is None:
            excluded["missing_score"] += 1
            continue
        if not isinstance(run.get("set"), str) or not run["set"]:
            raise ValueError(f"{key}: missing set")
        task_id = next((run[field] for field in ("task", "task_id", "cluster")
                        if run.get(field) is not None), None)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{key}: missing or invalid task identity")
        # Length-prefixing prevents collisions if set/task strings contain ':'.
        task = f"{len(run['set'])}:{run['set']}:{task_id}"
        rows.append(MetricRow(key, task, y, _number(value, "score"), chars, steps))
    return rows, {
        "input_rows": len(runs), "matched_rows": len(rows), "excluded": dict(excluded),
        "scores_without_runs": len(set(scores) - set(runs)),
    }


def auroc(labels: Sequence[int], scores: Sequence[float]) -> float | None:
    """Mann-Whitney AUROC, half credit for ties; undefined for one class."""
    if len(labels) != len(scores):
        raise ValueError("labels and scores must have equal lengths")
    ranked = sorted((_number(s, "score"), _label(y)) for y, s in zip(labels, scores, strict=True))
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    wins = neg_before = 0.0
    i = 0
    while i < len(ranked):
        j = i + 1
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        pos = sum(y for _, y in ranked[i:j])
        neg = j - i - pos
        wins += pos * (neg_before + neg / 2)
        neg_before += neg
        i = j
    return wins / (positives * negatives)


def choose_threshold(labels: Sequence[int], scores: Sequence[float], target_fpr: float = 0.05) -> float | None:
    """Lowest observed threshold within budget, including whole score tie blocks.

    This maximizes recall and then permitted detections. With no negatives the
    threshold is undefined, not an unsupported zero-FPR claim.
    """
    target_fpr = _number(target_fpr, "target_fpr")
    if not 0 <= target_fpr <= 1:
        raise ValueError("target_fpr must be between 0 and 1")
    if len(labels) != len(scores):
        raise ValueError("labels and scores must have equal lengths")
    blocks: dict[float, int] = defaultdict(int)
    for y, s in zip(labels, scores, strict=True):
        blocks[_number(s, "score")] += 1 - _label(y)
    negatives = len(labels) - sum(labels)
    if not negatives:
        return None
    threshold = math.inf
    fp = 0
    for value in sorted(blocks, reverse=True):
        fp += blocks[value]
        if fp / negatives > target_fpr:
            break
        threshold = value
    return threshold


def fit_length_strata(rows: Sequence[MetricRow], bins: int = 10) -> tuple[float, ...]:
    """Nearest-rank pooled length quantiles, upper-inclusive; never split ties."""
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 1:
        raise ValueError("bins must be a positive integer")
    lengths = sorted(r.n_chars for r in rows)
    if not lengths:
        return ()
    return tuple(sorted({lengths[math.ceil(len(lengths) * k / bins) - 1]
                         for k in range(1, bins)} - {lengths[-1]}))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


@dataclass(frozen=True)
class LengthModel:
    intercept: float | None
    slope: float | None
    mean_log_length: float
    scale_log_length: float
    n_dev: int
    reason: str | None = None

    def predict(self, n_chars: float) -> float | None:
        x = math.log1p(_number(n_chars, "n_chars", nonnegative=True))
        if self.intercept is None or self.slope is None:
            return None
        return _sigmoid(self.intercept + self.slope * (x - self.mean_log_length) / self.scale_log_length)


def fit_length_model(dev_runs: Sequence[Mapping[str, Any]]) -> LengthModel:
    """Fit DEV-only ridge logistic label ~ log1p(chars); reject incomplete DEV.

    No detector-score selection affects this fit. Empty/one-class DEV returns
    an undefined model. Missing labels/lengths raise rather than silently train
    on a different population. Newton iterations use an unpenalized intercept.
    """
    xs, ys = [], []
    seen = set()
    for run in dev_runs:
        if run.get("split") != "dev":
            raise ValueError("length model accepts only split='dev'")
        key = run.get("run_key")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("DEV rows need unique nonempty run_key")
        seen.add(key)
        ys.append(_label(run.get("label")))
        xs.append(math.log1p(_number(run.get("n_chars"), "n_chars", nonnegative=True)))
    n = len(xs)
    if not n or len(set(ys)) < 2:
        return LengthModel(None, None, 0.0, 1.0, n, "DEV requires both classes")
    mean = sum(xs) / n
    scale = math.sqrt(sum((x - mean) ** 2 for x in xs) / n) or 1.0
    zs = [(x - mean) / scale for x in xs]
    a = math.log(sum(ys) / (n - sum(ys)))
    b = 0.0
    ridge = 1e-4

    def loss(aa: float, bb: float) -> float:
        return sum(max(t, 0) - y * t + math.log1p(math.exp(-abs(t)))
                   for z, y in zip(zs, ys, strict=True) for t in [aa + bb * z]) / n + ridge * bb * bb / 2

    for _ in range(100):
        ga, gb, haa, hab, hbb = 0.0, ridge * b, 0.0, 0.0, ridge
        for z, y in zip(zs, ys, strict=True):
            p = _sigmoid(a + b * z)
            v = p * (1 - p) / n
            ga += (p - y) / n
            gb += (p - y) * z / n
            haa += v
            hab += v * z
            hbb += v * z * z
        if max(abs(ga), abs(gb)) < 1e-10:
            return LengthModel(a, b, mean, scale, n)
        determinant = haa * hbb - hab * hab
        if determinant <= 0:
            raise ValueError("length-model Newton system is singular")
        da, db = (hbb * ga - hab * gb) / determinant, (haa * gb - hab * ga) / determinant
        old = loss(a, b)
        step = 1.0
        while step > 2 ** -30 and loss(a - step * da, b - step * db) > old:
            step /= 2
        if step <= 2 ** -30:
            raise ValueError("length-model fit failed to descend")
        a, b = a - step * da, b - step * db
    raise ValueError("length-model fit did not converge")


def _group_auc(rows: Sequence[MetricRow], scores: Sequence[float], groups: Sequence[Any]) -> dict[str, Any]:
    buckets: dict[Any, list[int]] = defaultdict(list)
    for i, group in enumerate(groups):
        buckets[group].append(i)
    detail = []
    weighted = 0.0
    eligible = 0
    for group, indices in sorted(buckets.items()):
        labels = [rows[i].label for i in indices]
        value = auroc(labels, [scores[i] for i in indices])
        detail.append({"group": group, "rows": len(indices), "positive": sum(labels), "auroc": value})
        if value is not None:
            eligible += len(indices)
            weighted += value * len(indices)
    return {"auroc": weighted / eligible if eligible else None, "eligible_rows": eligible,
            "total_rows": len(rows), "coverage": eligible / len(rows) if rows else None,
            "eligible_groups": sum(d["auroc"] is not None for d in detail),
            "total_groups": len(detail), "groups": detail}


def _rates(labels: Sequence[int], scores: Sequence[float], threshold: float | None) -> dict[str, Any]:
    pos = sum(labels)
    neg = len(labels) - pos
    tp = sum(y == 1 and s >= threshold for y, s in zip(labels, scores, strict=True)) if threshold is not None else None
    fp = sum(y == 0 and s >= threshold for y, s in zip(labels, scores, strict=True)) if threshold is not None else None
    return {"threshold": threshold if threshold is not None and math.isfinite(threshold) else None,
            "reject_all": threshold == math.inf, "defined": threshold is not None,
            "recall": tp / pos if tp is not None and pos else None,
            "fpr": fp / neg if fp is not None and neg else None,
            "true_positive": tp, "false_positive": fp, "positive": pos, "negative": neg}


def _point(rows: Sequence[MetricRow], vectors: Mapping[str, Sequence[float]], edges: Sequence[float],
           thresholds: Mapping[str, float | None], target_fpr: float) -> dict[str, Any]:
    labels = [r.label for r in rows]
    out: dict[str, Any] = {}
    for name, values in vectors.items():
        result = {"raw_auroc": auroc(labels, values),
                  "length_stratified": _group_auc(rows, values, [bisect.bisect_left(edges, r.n_chars) for r in rows]),
                  "within_task": _group_auc(rows, values, [r.task for r in rows]),
                  "empirical_operating_point": _rates(labels, values, choose_threshold(labels, values, target_fpr))}
        if name in thresholds:
            result["dev_fixed_operating_point"] = _rates(labels, values, thresholds[name])
        out[name] = result
    a = out["signal"]["raw_auroc"]
    b = out.get("length_model", {}).get("raw_auroc")
    return {"scores": out, "paired_gain": a - b if a is not None and b is not None else None}


def _scalars(point: Mapping[str, Any]) -> dict[str, float | None]:
    out = {"paired_gain": point["paired_gain"]}
    for name, result in point["scores"].items():
        out[f"{name}.raw_auroc"] = result["raw_auroc"]
        for metric in ("length_stratified", "within_task"):
            out[f"{name}.{metric}.auroc"] = result[metric]["auroc"]
        for metric in ("empirical_operating_point", "dev_fixed_operating_point"):
            if metric in result:
                for rate in ("recall", "fpr"):
                    out[f"{name}.{metric}.{rate}"] = result[metric][rate]
    return out


def _resample(rows: Sequence[MetricRow], indices_by_task: Sequence[Sequence[int]], rng: random.Random
              ) -> tuple[list[MetricRow], list[int]]:
    sampled, indices = [], []
    for copy_id in range(len(indices_by_task)):
        for i in rng.choice(indices_by_task):
            # A copied task is a separate bootstrap cluster, not a merged group.
            sampled.append(replace(rows[i], task=str(copy_id)))
            indices.append(i)
    return sampled, indices


class _TieBlocks:
    """Sorted once; each score tie stores sparse task/label sufficient counts."""

    def __init__(self, indices: Sequence[int], rows: Sequence[MetricRow],
                 scores: Sequence[float], task_ids: Sequence[int]) -> None:
        counts: dict[float, Counter[tuple[int, int]]] = defaultdict(Counter)
        for i in indices:
            counts[scores[i]][task_ids[i], rows[i].label] += 1
        self.blocks = [
            (score, tuple((task, label, n) for (task, label), n in counts[score].items()))
            for score in sorted(counts)
        ]

    def weighted(self, multiplicity: Sequence[int]) -> list[tuple[float, int, int]]:
        blocks = []
        for score, entries in self.blocks:
            pos = neg = 0
            for task, label, n in entries:
                count = multiplicity[task] * n
                if label:
                    pos += count
                else:
                    neg += count
            if pos or neg:
                blocks.append((score, pos, neg))
        return blocks


def _block_auc(blocks: Sequence[tuple[float, int, int]]) -> tuple[float | None, int, int]:
    wins = neg_before = positives = 0
    for _, pos, neg in blocks:
        # Integer doubled wins avoid rounding until the final division.
        wins += pos * (2 * neg_before + neg)
        neg_before += neg
        positives += pos
    value = wins / (2 * positives * neg_before) if positives and neg_before else None
    return value, positives, neg_before


class _BootstrapPlan:
    """Compute bootstrap scalars without copying rows, sorting, or group details."""

    def __init__(self, rows: Sequence[MetricRow], vectors: Mapping[str, Sequence[float]],
                 edges: Sequence[float], thresholds: Mapping[str, float | None],
                 target_fpr: float) -> None:
        tasks = sorted({r.task for r in rows})
        self.n_tasks = len(tasks)
        lookup = {task: i for i, task in enumerate(tasks)}
        task_ids = [lookup[r.task] for r in rows]
        by_task: list[list[int]] = [[] for _ in tasks]
        by_stratum: dict[int, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            by_task[task_ids[i]].append(i)
            by_stratum[bisect.bisect_left(edges, r.n_chars)].append(i)
        self.thresholds = thresholds
        self.target_fpr = target_fpr
        self.scores = {}
        for name, values in vectors.items():
            task_stats = []
            for task, indices in enumerate(by_task):
                auc = auroc([rows[i].label for i in indices], [values[i] for i in indices])
                if auc is not None:
                    task_stats.append((task, len(indices), auc * len(indices)))
            self.scores[name] = (
                _TieBlocks(range(len(rows)), rows, values, task_ids),
                [_TieBlocks(indices, rows, values, task_ids) for indices in by_stratum.values()],
                task_stats,
            )

    def scalars(self, multiplicity: Sequence[int]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for name, (raw, strata, task_stats) in self.scores.items():
            blocks = raw.weighted(multiplicity)
            auc, positives, negatives = _block_auc(blocks)
            out[f"{name}.raw_auroc"] = auc
            weighted = eligible = 0
            for stratum in strata:
                value, pos, neg = _block_auc(stratum.weighted(multiplicity))
                if value is not None:
                    weighted += value * (pos + neg)
                    eligible += pos + neg
            out[f"{name}.length_stratified.auroc"] = weighted / eligible if eligible else None
            eligible = sum(multiplicity[task] * n for task, n, _ in task_stats)
            weighted = sum(multiplicity[task] * weight for task, _, weight in task_stats)
            out[f"{name}.within_task.auroc"] = weighted / eligible if eligible else None

            # Reselect the empirical threshold using only present whole ties.
            tp = fp = 0
            if negatives:
                for _, pos, neg in reversed(blocks):
                    if (fp + neg) / negatives > self.target_fpr:
                        break
                    tp += pos
                    fp += neg
            prefix = f"{name}.empirical_operating_point"
            out[f"{prefix}.recall"] = tp / positives if negatives and positives else None
            out[f"{prefix}.fpr"] = fp / negatives if negatives else None
            if name in self.thresholds:
                threshold = self.thresholds[name]
                tp = fp = 0
                if threshold is not None:
                    for score, pos, neg in blocks:
                        if score >= threshold:
                            tp += pos
                            fp += neg
                prefix = f"{name}.dev_fixed_operating_point"
                out[f"{prefix}.recall"] = tp / positives if threshold is not None and positives else None
                out[f"{prefix}.fpr"] = fp / negatives if threshold is not None and negatives else None
        a, b = out["signal.raw_auroc"], out.get("length_model.raw_auroc")
        out["paired_gain"] = a - b if a is not None and b is not None else None
        return out


def evaluate(
    rows: Sequence[MetricRow], *, length_model: LengthModel | None = None,
    strata_edges: Sequence[float] | None = None, dev_thresholds: Mapping[str, float | None] | None = None,
    bootstrap: int = 1000, seed: int = 1106, target_fpr: float = 0.05,
) -> dict[str, Any]:
    """Point metrics and paired task-cluster percentile 95% intervals.

    Thresholds/model are caller-frozen DEV artifacts. Empirical operating points
    are descriptive evaluation ROC points, never deployment thresholds. Bootstrap
    reselects only these empirical ROC points, not DEV thresholds or length edges.
    Undefined draws are counted. CI requires >=2 tasks and >=max(10,B/2) valid
    draws; one-class point estimates never receive an interval.
    """
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 0:
        raise ValueError("bootstrap must be a nonnegative integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if len({r.run_key for r in rows}) != len(rows):
        raise ValueError("evaluation run_key values must be unique")
    target_fpr = _number(target_fpr, "target_fpr")
    if not 0 <= target_fpr <= 1:
        raise ValueError("target_fpr must be between 0 and 1")
    edges = tuple(fit_length_strata(rows) if strata_edges is None else strata_edges)
    for edge in edges:
        _number(edge, "strata edge", nonnegative=True)
    if tuple(sorted(set(edges))) != edges:
        raise ValueError("strata edges must be strictly increasing")
    vectors = {"signal": [r.score for r in rows], "characters": [r.n_chars for r in rows],
               "steps": [r.n_steps for r in rows]}
    if length_model is not None and length_model.reason is None:
        predictions = [length_model.predict(r.n_chars) for r in rows]
        if any(p is None for p in predictions):
            raise ValueError("length model has no coefficients")
        vectors["length_model"] = predictions
    thresholds = dict(dev_thresholds or {})
    if set(thresholds) - {"signal", "characters", "steps", "length_model"}:
        raise ValueError("unknown DEV threshold score name")
    for threshold in thresholds.values():
        if threshold is not None and threshold != math.inf:
            _number(threshold, "DEV threshold")
    point = _point(rows, vectors, edges, thresholds, target_fpr)
    groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row.task].append(i)
    original = _scalars(point)
    draws: dict[str, list[float]] = {key: [] for key in original}
    rng = random.Random(seed)
    if len(groups) >= 2 and bootstrap:
        plan = _BootstrapPlan(rows, vectors, edges, thresholds, target_fpr)
        for _ in range(bootstrap):
            multiplicity = [0] * plan.n_tasks
            for _ in range(plan.n_tasks):
                # Same Random.choice sequence as _resample on sorted tasks.
                multiplicity[rng.randrange(plan.n_tasks)] += 1
            values = plan.scalars(multiplicity)
            for key, value in values.items():
                if value is not None:
                    draws[key].append(value)
    intervals = {}
    for key, values in draws.items():
        values.sort()
        enough = original[key] is not None and len(values) >= max(10, math.ceil(bootstrap / 2))
        intervals[key] = {"low": values[int(0.025 * (len(values) - 1))] if enough else None,
                          "high": values[int(0.975 * (len(values) - 1))] if enough else None,
                          "valid_draws": len(values), "requested_draws": bootstrap}
    return {**point, "n_rows": len(rows), "n_tasks": len(groups), "strata_edges": list(edges),
            "target_fpr": target_fpr, "seed": seed, "intervals": intervals,
            "length_model_reason": length_model.reason if length_model else "not supplied"}
