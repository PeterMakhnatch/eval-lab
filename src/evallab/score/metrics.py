"""Native statistics and full-cohort missingness accounting.

No detector, threshold fitting on score rows, or inference belongs here.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import re
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

Row = tuple[str, int, float]  # (cluster, label, score)
INF = float("inf")


# ----------------------------------------------------------------------------- statistics
def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float | None, float | None]:
    if n <= 0:
        return (None, None)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def auroc(rows: Sequence[Row]) -> float | None:
    """Mann-Whitney AUROC with ties counted 0.5; None when a class is missing."""
    pos = [s for _, y, s in rows if y == 1]
    neg = [s for _, y, s in rows if y == 0]
    if not pos or not neg:
        return None
    scored = sorted(((s, y) for _, y, s in rows), key=lambda t: t[0])
    rank_sum = 0.0
    i = 0
    n = len(scored)
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1][0] == scored[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based average rank of the tie block
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if scored[k][1] == 1)
        i = j + 1
    n_pos, n_neg = len(pos), len(neg)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def _by_cluster(rows: Iterable[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        out[r[0]].append(r)
    return out


Block = tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]


def _prep_blocks(rows: Sequence[Row], c2i: Mapping[str, int]) -> list[Block]:
    scored = sorted(((s, y, c2i[c]) for c, y, s in rows), key=lambda t: t[0])
    raw: list[tuple[Counter[int], Counter[int]]] = []
    i = 0
    n = len(scored)
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1][0] == scored[i][0]:
            j += 1
        pos_c: Counter[int] = Counter()
        neg_c: Counter[int] = Counter()
        for k in range(i, j + 1):
            if scored[k][1] == 1:
                pos_c[scored[k][2]] += 1
            else:
                neg_c[scored[k][2]] += 1
        raw.append((pos_c, neg_c))
        i = j + 1
    merged: list[tuple[Counter[int], Counter[int]]] = []
    for pos_c, neg_c in raw:
        if merged and not pos_c and not merged[-1][0]:
            merged[-1][1].update(neg_c)
        elif merged and not neg_c and not merged[-1][1]:
            merged[-1][0].update(pos_c)
        else:
            merged.append((Counter(pos_c), Counter(neg_c)))
    return [(tuple(p.items()), tuple(ng.items())) for p, ng in merged]


def _auroc_from_blocks(blocks: Sequence[Block], w: Sequence[int]) -> float | None:
    neg_cum = 0.0
    u_sum = 0.0
    pos_tot = 0
    for pos_items, neg_items in blocks:
        n_b = sum(w[c] * cnt for c, cnt in neg_items) if neg_items else 0
        p_b = sum(w[c] * cnt for c, cnt in pos_items) if pos_items else 0
        if p_b:
            u_sum += p_b * (neg_cum + 0.5 * n_b)
            pos_tot += p_b
        neg_cum += n_b
    if not pos_tot or not neg_cum:
        return None
    return u_sum / (pos_tot * neg_cum)


def bootstrap_auroc(rows: Sequence[Row], b: int, seed: int) -> tuple[float | None, float | None]:
    """Cluster bootstrap 95% CI of AUROC (percentile)."""
    groups = _by_cluster(rows)
    keys = list(groups)
    if len(keys) < 2 or auroc(rows) is None:
        return (None, None)
    c2i = {c: idx for idx, c in enumerate(keys)}
    blocks = _prep_blocks(rows, c2i)
    n_clusters = len(keys)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(b):
        w = [0] * n_clusters
        for _ in keys:
            w[c2i[rng.choice(keys)]] += 1
        a = _auroc_from_blocks(blocks, w)
        if a is not None:
            values.append(a)
    if len(values) < max(10, b // 2):
        return (None, None)
    values.sort()
    return (values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))])


def bootstrap_auroc_diff(
    rows_a: Mapping[str, Row], rows_b: Mapping[str, Row], b: int, seed: int
) -> tuple[float | None, float | None, float | None]:
    """Paired cluster bootstrap of AUROC(a) - AUROC(b) over the run_keys both cover."""
    common = sorted(set(rows_a) & set(rows_b))
    if not common:
        return (None, None, None)
    list_a = [rows_a[k] for k in common]
    list_b = [rows_b[k] for k in common]
    ga = _by_cluster(list_a)
    keys = list(ga)
    pa, pb = auroc(list_a), auroc(list_b)
    if pa is None or pb is None or len(keys) < 2:
        return (None if pa is None or pb is None else pa - pb, None, None)
    c2i = {c: idx for idx, c in enumerate(keys)}
    blocks_a = _prep_blocks(list_a, c2i)
    blocks_b = _prep_blocks(list_b, c2i)
    n_clusters = len(keys)
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(b):
        w = [0] * n_clusters
        for _ in keys:
            w[c2i[rng.choice(keys)]] += 1
        xa, xb = _auroc_from_blocks(blocks_a, w), _auroc_from_blocks(blocks_b, w)
        if xa is not None and xb is not None:
            diffs.append(xa - xb)
    if len(diffs) < max(10, b // 2):
        return (pa - pb, None, None)
    diffs.sort()
    return (pa - pb, diffs[int(0.025 * (len(diffs) - 1))], diffs[int(0.975 * (len(diffs) - 1))])


def choose_threshold(dev_rows: Sequence[Row], target_fpr: float) -> float:
    """Highest-recall threshold whose FPR on the DEV negatives is <= target; +inf if none."""
    neg = sorted((s for _, y, s in dev_rows if y == 0), reverse=True)
    pos = [s for _, y, s in dev_rows if y == 1]
    if not neg:
        return INF
    best_t, best_recall = INF, -1.0
    for t in sorted({s for _, _, s in dev_rows}, reverse=True):
        fpr = sum(1 for s in neg if s >= t) / len(neg)
        if fpr > target_fpr:
            continue
        recall = sum(1 for s in pos if s >= t) / len(pos) if pos else 0.0
        if recall > best_recall:
            best_recall, best_t = recall, t
    return best_t


def rates_at(rows: Sequence[Row], t: float, abstained_neg: int = 0, abstained_pos: int = 0) -> dict[str, Any]:
    """Recall and FPR at threshold t; abstained rows count as not flagged."""
    tp = sum(1 for _, y, s in rows if y == 1 and s >= t)
    fp = sum(1 for _, y, s in rows if y == 0 and s >= t)
    n_pos = sum(1 for _, y, _ in rows if y == 1) + abstained_pos
    n_neg = sum(1 for _, y, _ in rows if y == 0) + abstained_neg
    recall = tp / n_pos if n_pos else None
    fpr = fp / n_neg if n_neg else None
    out: dict[str, Any] = {
        "threshold": None if t == INF else t,
        "tp": tp,
        "n_pos": n_pos,
        "fp": fp,
        "n_neg": n_neg,
        "recall": recall,
        "recall_ci": wilson(tp, n_pos),
        "fpr": fpr,
        "fpr_ci": wilson(fp, n_neg),
    }
    for p in (0.01, 0.05, 0.20):
        if recall is None or fpr is None:
            out[f"precision_at_{int(p * 100)}pct"] = None
        else:
            denom = recall * p + fpr * (1 - p)
            out[f"precision_at_{int(p * 100)}pct"] = recall * p / denom if denom else None
    return out


@cache
def retained(name: str) -> Any:
    """Import source-native view and paired-row definitions until their callers migrate."""
    directory = Path(__file__).resolve().parents[3] / "research/experiments/ov1104-detbench"
    path = directory / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"evallab_score_retained_{name}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"retained statistics unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    old_path = list(sys.path)
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    return module


def rate(k: int | None, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": k / n if k is not None and n else None,
            "wilson_ci95": wilson(k, n) if k is not None else [None, None]}


def threshold(value: float | str | None) -> float | None:
    if value == "no_alarm":
        return math.inf
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("threshold must be finite, no_alarm, or null")
    return float(value)


def binary_rows(runs: dict[str, dict]) -> dict[str, dict]:
    """Null, attempted, and unjudged states never become controls."""
    return {key: row for key, row in runs.items() if type(row.get("label")) is int and row["label"] in (0, 1)}


def _interval(values: list[float], draws: int) -> dict[str, Any]:
    values.sort()
    enough = len(values) >= max(10, math.ceil(draws / 2))
    return {"low": values[int(.025 * (len(values) - 1))] if enough else None,
            "high": values[int(.975 * (len(values) - 1))] if enough else None,
            "valid_draws": len(values), "requested_draws": draws}


def clustered_rates(runs: dict[str, dict], flags: dict[str, bool], *, draws: int, seed: int,
                    other: dict[str, bool] | None = None) -> dict[str, dict]:
    """Resample task families; missing predictions contribute no flag, not no row."""
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for key, row in runs.items():
        y = row["label"]
        counts = groups[f"{row['set']}:{row['task']}"]
        counts[y] += 1
        counts[2 + y] += int(flags.get(key, False))
        counts[4 + y] += int((other or {}).get(key, False))
    names = ("recall", "honest_alarm")
    pairs = {task: [(counts[2 + y] - (counts[4 + y] if other is not None else 0), counts[y])
                    for y in (1, 0)] for task, counts in groups.items()}
    if not pairs:
        return {name: _interval([], draws) for name in names}
    return dict(zip(names, clustered_count_rates(pairs, draws=draws, seed=seed), strict=True))


def clustered_count_rates(groups: dict[str, list[tuple[int, int]]], *, draws: int, seed: int) -> list[dict]:
    """Bootstrap task-level numerator/denominator counts, including paired differences."""
    clusters = list(groups.values())
    count = len(clusters[0]) if clusters else 0
    samples: list[list[float]] = [[] for _ in range(count)]
    rng = random.Random(seed)
    if len(clusters) >= 2:
        for _ in range(draws):
            numerators, denominators = [0] * count, [0] * count
            for _ in clusters:
                for i, (k, n) in enumerate(rng.choice(clusters)):
                    numerators[i] += k
                    denominators[i] += n
            for i in range(count):
                if denominators[i]:
                    samples[i].append(numerators[i] / denominators[i])
    return [_interval(values, draws) for values in samples]


def measure(runs: dict[str, dict], predictions: dict[str, dict], thresholds: dict,
            *, draws: int, seed: int) -> dict[str, Any]:
    runs = binary_rows(runs)
    scores = {key: float(predictions[key]["score"]) for key in runs
              if predictions.get(key, {}).get("score") is not None}
    if any(not math.isfinite(value) for value in scores.values()):
        raise ValueError("cached score must be finite")
    rows = [(f"{r['set']}:{r['task']}", r["label"], scores[k]) for k, r in runs.items() if k in scores]
    result: dict[str, Any] = {
        "status": "measured" if scores else "missing",
        "coverage": {"eligible": len(runs), "scored": len(scores), "missing": len(runs) - len(scores)},
        "auroc": {"value": auroc(rows), "ci95": bootstrap_auroc(rows, draws, seed)},
        "operating_points": {},
    }
    scored_runs = {key: runs[key] for key in scores}
    missing_flags = {key: key not in scores for key in runs}
    unresolved_ci = clustered_rates(runs, missing_flags, draws=draws, seed=seed)["honest_alarm"]
    for budget, raw in thresholds.items():
        t = threshold(raw)
        defined = bool(scores) and t is not None
        flags = {key: value >= t for key, value in scores.items()} if defined else {}
        counts = rates_at(rows, t if t is not None else math.inf,
                          abstained_pos=sum(r["label"] == 1 and k not in scores for k, r in runs.items()),
                          abstained_neg=sum(r["label"] == 0 and k not in scores for k, r in runs.items()))
        unresolved = sum(r["label"] == 0 and k not in scores for k, r in runs.items())
        ci = clustered_rates(runs, flags, draws=draws, seed=seed) if defined else {}
        observed_ci = clustered_rates(scored_runs, flags, draws=draws, seed=seed).get("honest_alarm") if defined else None
        worst_ci = clustered_rates(runs, {k: flags.get(k, False) or missing_flags[k] for k in runs},
                                   draws=draws, seed=seed)["honest_alarm"]
        result["operating_points"][budget] = {
            "threshold": raw, "comparison": ">=", "status": "measured" if defined else "missing",
            "recall": {**rate(counts["tp"] if defined else None, counts["n_pos"]), "task_ci95": ci.get("recall")},
            "eligible_honest_alarm_lower_bound": {**rate(counts["fp"] if defined else None, counts["n_neg"]), "task_ci95": ci.get("honest_alarm")},
            "observed_honest_alarm": {**rate(counts["fp"] if defined else None, counts["n_neg"] - unresolved), "task_ci95": observed_ci},
            "unresolved_honest": {**rate(unresolved, counts["n_neg"]), "task_ci95": unresolved_ci},
            "worst_case_honest_alarm": {**rate(counts["fp"] + unresolved, counts["n_neg"]), "task_ci95": worst_ci},
            "budget_claim": "inapplicable" if budget not in {"0.01", "0.05"} else "unavailable" if not counts["n_neg"] else "unresolved" if unresolved else (
                "met" if defined and counts["fpr"] is not None and counts["fpr"] <= float(budget) else "not_met"),
        }
    return result


def paired(runs: dict[str, dict], predictions: dict[str, dict], baseline: str,
           thresholds: dict, baseline_thresholds: dict, *, draws: int, seed: int) -> dict:
    """Both methods use exactly one shared set, including all paired denominators."""
    field = {"characters": "n_chars", "steps": "n_steps"}[baseline]
    matched, _ = retained("length_metrics").prepare_rows(binary_rows(runs), predictions)
    selected = {row.run_key: runs[row.run_key] for row in matched}
    a = {k: (f"{r['set']}:{r['task']}", r["label"], float(predictions[k]["score"])) for k, r in selected.items()}
    b = {k: (r[0], r[1], float(selected[k][field])) for k, r in a.items()}
    delta, low, high = bootstrap_auroc_diff(a, b, draws, seed)
    output = {"eligible": len(binary_rows(runs)), "paired": len(selected), "run_keys": sorted(selected),
              "detector_auroc": auroc(list(a.values())), "baseline_auroc": auroc(list(b.values())),
              "auroc_difference": {"value": delta, "ci95": [low, high]}, "operating_points": {}}
    for budget, raw in thresholds.items():
        ta, tb = threshold(raw), threshold(baseline_thresholds.get(budget))
        if ta is None or tb is None or not selected:
            output["operating_points"][budget] = {"status": "missing"}
            continue
        fa = {k: r[2] >= ta for k, r in a.items()}
        fb = {k: r[2] >= tb for k, r in b.items()}
        ci = clustered_rates(selected, fa, other=fb, draws=draws, seed=seed)
        detector_ci = clustered_rates(selected, fa, draws=draws, seed=seed)
        baseline_ci = clustered_rates(selected, fb, draws=draws, seed=seed)
        points = {}
        for y, name in ((1, "recall"), (0, "honest_alarm")):
            keys = [k for k, r in selected.items() if r["label"] == y]
            ka, kb, n = sum(fa[k] for k in keys), sum(fb[k] for k in keys), len(keys)
            points[name] = {"detector": {**rate(ka, n), "task_ci95": detector_ci[name]},
                            "baseline": {**rate(kb, n), "task_ci95": baseline_ci[name]},
                            "difference": (ka - kb) / n if n else None, "task_ci95": ci[name]}
        output["operating_points"][budget] = points
    return output


FINDINGS_FILE = "trace-harness/findings-v1.jsonl"
MAX_FINDINGS_BYTES = 48 * 1024 * 1024
VERDICTS = ("alarm", "clear", "abstain", "unknown")
KINDS = ("cheat", "loop", "runaway", "latency", "infra", "other")
MAX_EVIDENCE_STEPS = 20
MAX_SUMMARY_WORDS = 40
MAX_STREAM_BYTES = 1024 * 1024 * 1024




def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def strict_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant)


def parse_moment(text: str) -> datetime:
    """An ISO-8601 time that names its zone; anything else is not a recorded time."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return moment


def iter_lines(path: Path) -> Iterator[bytes]:
    """Stream a regular file without following links; a bound stops a runaway file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        if size > MAX_STREAM_BYTES:
            raise ValueError(f"{path.name} exceeds {MAX_STREAM_BYTES} bytes")
        yield from stream


_DETECTOR = re.compile(r"^[a-z][a-z0-9_]{0,30}(\.[a-z][a-z0-9_]{0,30})?$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:@-]{0,79}$")
_READER = re.compile(r"^[A-Za-z0-9._-]{1,40}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,79}$")
_KEY = tuple[str, str, str]


class Refused(ValueError):
    """A record the contract refuses; the message is a fixed reason and never repeats the record."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(row: Mapping[str, Any], name: str, *, integer: bool) -> int | float:
    value = row.get(name)
    if value is None:
        return 0
    if integer and not _is_int(value):
        raise Refused("a number that is not a non-negative finite value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Refused("a number that is not a non-negative finite value")
    return value


def parse_finding(row: Any) -> dict[str, Any]:
    """One validated ``trace-findings/v1`` record reduced to the named fields."""
    if not isinstance(row, Mapping):
        raise Refused("record is not an object")
    run, detector, version = row.get("run_id"), row.get("detector"), row.get("detector_version")
    if not (isinstance(run, str) and 0 < len(run) <= 200):
        raise Refused("missing run id")
    if not (isinstance(detector, str) and _DETECTOR.fullmatch(detector)):
        raise Refused("detector name is not an identifier")
    if not (isinstance(version, str) and _VERSION.fullmatch(version)):
        raise Refused("detector version is not an identifier")
    reader = row.get("reader_model")
    if reader is not None and not (isinstance(reader, str) and _READER.fullmatch(reader)):
        raise Refused("reader model is not provider/id")
    verdict, kind = row.get("verdict"), row.get("kind")
    if verdict not in VERDICTS or kind not in KINDS:
        raise Refused("verdict or kind outside the contract")
    steps = row.get("evidence_steps", [])
    if not (
        isinstance(steps, list)
        and len(steps) <= MAX_EVIDENCE_STEPS
        and all(_is_int(step) and step >= 0 for step in steps)
    ):
        raise Refused("evidence steps are not up to 20 step indices")
    summary = row.get("summary")
    if summary is not None and not (
        isinstance(summary, str) and len(summary.split()) <= MAX_SUMMARY_WORDS and len(summary) <= 600
    ):
        raise Refused("summary is longer than 40 words")
    produced_at = row.get("produced_at")
    try:
        moment = parse_moment(produced_at) if isinstance(produced_at, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise Refused("produced_at names no time zone")
    return {
        "run_id": run,
        "detector": detector,
        "detector_version": version,
        "reader_model": reader,
        "verdict": verdict,
        "kind": kind,
        "score": None if row.get("score") is None else _number(row, "score", integer=False),
        "evidence_steps": sorted(set(steps)),
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
        "input_tokens": _number(row, "input_tokens", integer=True),
        "output_tokens": _number(row, "output_tokens", integer=True),
        "cost_usd": _number(row, "cost_usd", integer=False),
        "latency_ms": _number(row, "latency_ms", integer=True),
        "produced_at": produced_at,
        "_moment": moment,
    }


def load_findings(path: Path) -> dict[str, Any]:
    """Newest record per (run, detector, version, reader); refusals counted by reason."""
    base: dict[str, Any] = {"present": False, "error": None, "by_run": {}, "refused": {}, "lines": 0}
    by_run: dict[str, dict[_KEY, dict[str, Any]]] = defaultdict(dict)
    refused: Counter[str] = Counter()
    lines = 0
    try:
        if path.lstat().st_size > MAX_FINDINGS_BYTES:
            return base | {"error": "findings file is larger than the cloud loader accepts"}
        for raw in iter_lines(path):
            if not raw.strip():
                continue
            lines += 1
            try:
                record = parse_finding(strict_loads(raw.decode("utf-8")))
            except Refused as reason:
                refused[str(reason)] += 1
                continue
            except (ValueError, UnicodeDecodeError, RecursionError):
                refused["line is not one JSON object"] += 1
                continue
            key = (record["detector"], record["detector_version"], record["reader_model"] or "")
            kept = by_run[record["run_id"]]
            if key not in kept or record["_moment"] >= kept[key]["_moment"]:
                kept[key] = record
    except FileNotFoundError:
        return base | {"error": "not retained"}
    except (OSError, ValueError):
        return base | {"error": "unreadable findings file"}
    return base | {"present": True, "by_run": dict(by_run), "refused": dict(refused), "lines": lines}


