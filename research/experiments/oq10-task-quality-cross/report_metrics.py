"""Aggregate metrics for one OQ10 cross-test cell (one corpus, one detector arm).

One entry point, ``summarize_cell``: counts in, plain JSON-ready dict out. No file
I/O, no task ids, prompts, gold rows or model text; callers pass counts only.

Corpus semantics come from the three exact corpus names, never from model labels:

* ``parsewave__automationbench-verified`` -- positives are publisher-verified defect
  tasks; negatives are UNCHANGED tasks, NOT certified clean. A flag on them is an
  "unverified alarm", precision is conditional on unchanged-as-clean, and the
  negative rate is never called a false-positive rate.
* ``swe_bench_multilingual`` and ``swe_gym`` (PAIChecker) -- negatives are
  human-labelled aligned tasks, so a sample false-positive rate and precision are
  defined. They measure issue/PR/test alignment, not verified weak-grader bugs.
  The two slices are separate cells; a pooled PAIChecker cell is not accepted here.

Missing, unparsed or unanswered tasks are planned-minus-completed coverage and
enter no rate. Recall is task-level (any accepted flag on a known-positive task)
with a 95% Wilson score interval that ignores clustering. ONE corpus view of a
cluster-bootstrap result from the Library scorer (``score_task_qa_gate.py``, an
entry of ``score()["views"]``) whose recall and negative-rate num/den match this
cell is retained verbatim under ``cluster_bootstrap``; it is not recomputed here
and Wilson is never relabelled as bootstrap.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from numbers import Real
from statistics import NormalDist
from typing import Any

SCHEMA = "oq10-cell-metrics/v1"
AUTOMATIONBENCH = "parsewave__automationbench-verified"
PAI_MULTILINGUAL = "swe_bench_multilingual"
PAI_SWE_GYM = "swe_gym"
CORPUS_NEGATIVES_CERTIFIED = {
    AUTOMATIONBENCH: False,
    PAI_MULTILINGUAL: True,
    PAI_SWE_GYM: True,
}
CONFUSION_KEYS = ("TP", "FN", "FP", "TN")
WILSON_Z = NormalDist().inv_cdf(0.975)

_CERTIFIED_NEGATIVE_RATE = "false_positive_rate_on_human_aligned"
_UNCERTIFIED_NEGATIVE_RATE = "unverified_alarm_rate"
_UNCERTIFIED_NOTE = (
    "AutomationBench unchanged tasks are not certified clean and carry no causal-key "
    "gold. conditional_precision holds only if every unchanged task is clean; "
    "unverified_alarm_rate is the share of completed unchanged tasks flagged, not a "
    "false-positive rate."
)
_CERTIFIED_NOTE = (
    "PAIChecker negatives are human-labelled aligned tasks: "
    "false_positive_rate_on_human_aligned and precision are sample rates at this "
    "cell's class balance, not population prevalence, and measure issue/PR/test "
    "alignment, not verified weak-grader bugs."
)
_RECALL_NOTE = (
    "Task-level: any accepted flag on a known-positive task counts. No causal-key "
    "matching and no confirmed-defect localization."
)
_WILSON_METHOD = "Wilson score 95% (z=1.959964), task-level, ignores clustering"


class MetricsError(ValueError):
    """Counts, tokens, cost or a cluster result that cannot describe one cell."""


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetricsError(f"{name} must be an int")
    if value < 0:
        raise MetricsError(f"{name} must be non-negative")
    return value


def _optional_count(name: str, value: Any) -> int | None:
    return None if value is None else _count(name, value)


def _optional_usd(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MetricsError("actual_paid_usd must be a number or None")
    usd = float(value)
    if not math.isfinite(usd) or usd < 0:
        raise MetricsError("actual_paid_usd must be finite and non-negative")
    return usd


def _round(value: float) -> float:
    return round(value, 6)


def _ratio(num: int, den: int) -> dict[str, Any]:
    return {"num": num, "den": den, "value": _round(num / den) if den else None}


def _per_task(totals: Mapping[str, int | None], tasks: int) -> dict[str, dict[str, Any]]:
    return {
        kind: {"num": total, "den": tasks, "value": None if total is None or not tasks else _round(total / tasks)}
        for kind, total in totals.items()
    }


def wilson_interval(successes: int, trials: int) -> tuple[float, float] | None:
    """95% Wilson score interval; None when there are no trials."""
    _count("successes", successes)
    _count("trials", trials)
    if successes > trials:
        raise MetricsError("successes exceed trials")
    if trials == 0:
        return None
    p = successes / trials
    z2 = WILSON_Z * WILSON_Z
    scale = 1.0 + z2 / trials
    centre = (p + z2 / (2 * trials)) / scale
    half = WILSON_Z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / scale
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == trials else min(1.0, centre + half)
    return _round(low), _round(high)


def _confusion(confusion: Mapping[str, Any]) -> tuple[int, int, int, int]:
    if not isinstance(confusion, Mapping):
        raise MetricsError("confusion must be a mapping of TP/FN/FP/TN")
    if set(confusion) != set(CONFUSION_KEYS):
        raise MetricsError("confusion keys must be exactly TP, FN, FP, TN")
    tp, fn, fp, tn = (_count(f"confusion[{key}]", confusion[key]) for key in CONFUSION_KEYS)
    return tp, fn, fp, tn


def _retained_cluster_result(
    cluster_result: Mapping[str, Any],
    *,
    certified: bool,
    tp: int,
    fn: int,
    fp: int,
    tn: int,
) -> Any:
    """Check one Library ``score_view`` dict against this cell and return a JSON copy."""
    if not isinstance(cluster_result, Mapping):
        raise MetricsError("cluster_result must be a mapping or None")
    if "views" in cluster_result:
        raise MetricsError("cluster_result must be ONE corpus view from score()['views'], not the whole score payload")
    if cluster_result.get("ids_or_text_in_this_output", False) is not False:
        raise MetricsError("cluster_result declares ids or text; aggregate results only")
    wrong_rate = _UNCERTIFIED_NEGATIVE_RATE if certified else _CERTIFIED_NEGATIVE_RATE
    if wrong_rate in cluster_result:
        raise MetricsError(f"cluster_result carries {wrong_rate}, which contradicts this corpus")
    right_rate = _CERTIFIED_NEGATIVE_RATE if certified else _UNCERTIFIED_NEGATIVE_RATE
    for key, num, den in (("recall", tp, tp + fn), (right_rate, fp, fp + tn)):
        stat = cluster_result.get(key)
        if not isinstance(stat, Mapping) or "num" not in stat or "den" not in stat:
            raise MetricsError(f"cluster_result view lacks {key} num/den")
        if (stat["num"], stat["den"]) != (num, den):
            raise MetricsError(f"cluster_result {key} num/den differ from this cell's confusion")
    try:
        text = json.dumps(cluster_result, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise MetricsError("cluster_result must be plain finite JSON") from error
    return json.loads(text)


def summarize_cell(
    *,
    corpus: str,
    planned_positive: int,
    planned_negative: int,
    confusion: Mapping[str, Any],
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    actual_paid_usd: float | None,
    cluster_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate one cell; ``confusion`` counts COMPLETED labelled tasks only.

    Raises ``MetricsError`` on an unknown corpus, malformed or negative counts,
    completed counts above the plan, negative tokens, negative/non-finite cash, or
    a cluster result that contradicts this cell. Nothing is clipped or repaired.
    """
    if corpus not in CORPUS_NEGATIVES_CERTIFIED:
        raise MetricsError(f"corpus must be one of {sorted(CORPUS_NEGATIVES_CERTIFIED)}")
    certified = CORPUS_NEGATIVES_CERTIFIED[corpus]
    planned_pos = _count("planned_positive", planned_positive)
    planned_neg = _count("planned_negative", planned_negative)
    tp, fn, fp, tn = _confusion(confusion)
    done_pos, done_neg = tp + fn, fp + tn
    if done_pos > planned_pos:
        raise MetricsError("completed positives (TP+FN) exceed planned_positive")
    if done_neg > planned_neg:
        raise MetricsError("completed negatives (FP+TN) exceed planned_negative")
    token_totals = {
        "input": _optional_count("input_tokens", input_tokens),
        "output": _optional_count("output_tokens", output_tokens),
    }
    token_totals["input_plus_output"] = (
        None
        if token_totals["input"] is None or token_totals["output"] is None
        else token_totals["input"] + token_totals["output"]
    )
    token_totals["reasoning"] = _optional_count("reasoning_tokens", reasoning_tokens)
    usd = _optional_usd(actual_paid_usd)
    retained = None
    if cluster_result is not None:
        retained = _retained_cluster_result(
            cluster_result, certified=certified, tp=tp, fn=fn, fp=fp, tn=tn
        )

    missing_pos, missing_neg = planned_pos - done_pos, planned_neg - done_neg
    done = done_pos + done_neg
    complete = missing_pos == 0 and missing_neg == 0
    interval = wilson_interval(tp, done_pos)
    recall = _ratio(tp, done_pos)
    recall.update(
        wilson95=None if interval is None else list(interval),
        interval_method=_WILSON_METHOD,
        note=_RECALL_NOTE,
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "corpus": corpus,
        "negatives_certified": certified,
        "negative_kind": "human_aligned" if certified else "unchanged_uncertified",
        "coverage": {
            "positives": {"planned": planned_pos, "completed": done_pos, "missing": missing_pos},
            "negatives": {"planned": planned_neg, "completed": done_neg, "missing": missing_neg},
            "tasks": {
                "planned": planned_pos + planned_neg,
                "completed": done,
                "missing": missing_pos + missing_neg,
            },
            "complete": complete,
            "missing_counted_as": "neither flag nor no-flag; excluded from every rate",
        },
        "rates_are": "full-plan" if complete else "completed-class only (missing withheld)",
        "confusion": (
            {"tp": tp, "fn": fn, "fp_false_alarms": fp, "tn": tn}
            if certified
            else {"tp": tp, "fn": fn, "unverified_alarms": fp, "unflagged_unverified": tn}
        ),
        "recall": recall,
    }
    if certified:
        result["precision"] = _ratio(tp, tp + fp)
        result[_CERTIFIED_NEGATIVE_RATE] = _ratio(fp, done_neg)
        result["negatives_note"] = _CERTIFIED_NOTE
    else:
        result["conditional_precision"] = _ratio(tp, tp + fp)
        result[_UNCERTIFIED_NEGATIVE_RATE] = _ratio(fp, done_neg)
        result["negatives_note"] = _UNCERTIFIED_NOTE
    result["tokens"] = {
        **token_totals,
        "per_completed_task": _per_task(token_totals, done),
        "per_flagged_known_positive_task": _per_task(token_totals, tp),
        "input_means": "caller-normalized total prompt tokens, cache reads included",
        "reasoning_means": "reported separately; never added to input_plus_output",
        "null_means": "usage unknown in a contributing call, or divisor 0; unknown, not zero",
        "basis": "totals over every consumed attempt in the cell, including missing tasks; divided by completed tasks or by known-positive tasks with a task-level flag",
    }
    result["cost"] = {
        "actual_paid_usd": usd,
        "actual_cash": "recorded" if usd is not None else "N/A",
        "usd_per_completed_task": _round(usd / done) if usd is not None and done else None,
        "usd_per_flagged_known_positive_task": _round(usd / tp) if usd is not None and tp else None,
        "null_means": "no actual cash (subscription/native or unknown); list price or client estimates are never substituted",
        "basis": "cash over every consumed attempt in the cell; per task with a task-level flag on a known positive, not per confirmed or localized defect",
    }
    result["cluster_bootstrap"] = (
        None
        if retained is None
        else {
            "source": "caller-supplied existing Library cluster-bootstrap result, retained verbatim; not recomputed",
            "result": retained,
        }
    )
    return result
