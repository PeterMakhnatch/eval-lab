"""Paired candidate-vs-stock analysis over retained GEPA evaluation receipts.

Reads the optimizer's own receipt artifacts (``EvaluationRecord.to_dict()``
JSON under ``<campaign_dir>/evaluations/``) and reports paired outcomes per
candidate against a stock/seed candidate, plus an optional evaluation-count
comparison against a caller-supplied control campaign.

Truth rules enforced here:
- Pairing is by immutable task identity ``(task_id, task_package_digest)``
  and requires a uniform treatment binding (one ``agent`` and one ``model``
  across every row of a campaign); a comparison across different models is
  not a harness comparison and is refused.
- ``pending``/``error`` receipts are counted as missingness and never scored;
  unknown costs stay ``None`` plus an explicit count, never zero.
- The only uncertainty reported is a paired cluster bootstrap of the mean
  delta; with fewer than two clusters the interval is reported unavailable.
- No claim of improvement is made anywhere: search-visible score is not
  held-out improvement (see :data:`CAVEAT`).
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CAVEAT = (
    "Search-visible development score is not held-out improvement. Paired deltas "
    "describe optimization feedback on search-visible tasks only. The method "
    "control checks completed-evaluation counts, not matched spend, verified "
    "no-feedback provenance, or generalization."
)

CANDIDATE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
RECEIPT_STATUSES = frozenset({"completed", "pending", "error"})
REQUIRED_RECEIPT_FIELDS = (
    "candidate_id",
    "task_id",
    "task_package_digest",
    "agent",
    "model",
    "status",
    "score",
    "usage",
)
# The evaluator writes an open usage dict; these keys are totalled where
# present and counted as unknown where absent (never zero-filled).
USAGE_TOTAL_KEYS = (
    "cost_usd",
    "latency_seconds",
    "n_input_tokens",
    "n_output_tokens",
    "n_total_tokens",
)

TaskKey = tuple[str, str]


def _validate_stock_candidate(stock_candidate: str) -> str:
    """Require an exact full ``sha256:<64-hex>`` digest; prefixes are ambiguous."""
    if not isinstance(stock_candidate, str) or not CANDIDATE_ID_PATTERN.fullmatch(stock_candidate):
        raise ValueError(
            "stock_candidate must be an exact full 'sha256:<64-hex>' digest "
            f"without prefix ambiguity, got {stock_candidate!r}"
        )
    return stock_candidate


def _validate_source_groups(source_groups: Mapping[str, str] | None) -> dict[str, str] | None:
    """Copy and validate the task_id -> cluster group mapping when supplied."""
    if source_groups is None:
        return None
    if not isinstance(source_groups, Mapping):
        raise ValueError("source_groups must be a mapping of task_id to group label")
    validated: dict[str, str] = {}
    for task_id, group in source_groups.items():
        if not isinstance(task_id, str) or not isinstance(group, str):
            raise ValueError(
                f"source_groups keys and values must be strings, got {task_id!r} -> {group!r}"
            )
        validated[task_id] = group
    return dict(sorted(validated.items()))


def load_receipts(campaign_dir: Path) -> list[dict[str, Any]]:
    """Load every receipt JSON under ``<campaign_dir>/evaluations/`` in name order.

    Symlinks are rejected (a symlinked receipt is not a trustworthy artifact),
    malformed or non-object JSON is refused, and non-JSON entries are skipped.
    """
    campaign_dir = Path(campaign_dir)
    evaluations_dir = campaign_dir / "evaluations"
    if evaluations_dir.is_symlink():
        raise ValueError(f"symlinked evaluations directory rejected: {evaluations_dir}")
    if not evaluations_dir.is_dir():
        raise ValueError(f"campaign has no evaluations directory: {evaluations_dir}")
    rows: list[dict[str, Any]] = []
    for entry in sorted(evaluations_dir.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            raise ValueError(f"symlinked receipt rejected: {entry}")
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            row = json.loads(entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed receipt JSON in {entry}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"receipt must be a JSON object: {entry}")
        missing = [field for field in REQUIRED_RECEIPT_FIELDS if field not in row]
        if missing:
            raise ValueError(f"receipt {entry.name} is missing required fields: {missing}")
        if not isinstance(row["candidate_id"], str) or not CANDIDATE_ID_PATTERN.fullmatch(
            row["candidate_id"]
        ):
            raise ValueError(
                f"receipt {entry.name} carries a non-exact candidate id: {row['candidate_id']!r}"
            )
        for field in ("task_id", "task_package_digest", "agent"):
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError(f"receipt {entry.name} has an invalid {field}: {row[field]!r}")
        if not CANDIDATE_ID_PATTERN.fullmatch(row["task_package_digest"]):
            raise ValueError(f"receipt {entry.name} has a non-exact task package digest")
        if row.get("split") != "development":
            raise ValueError(f"receipt {entry.name} is not a development evaluation")
        if row["model"] is not None and not isinstance(row["model"], str):
            raise ValueError(f"receipt {entry.name} has a non-string model: {row['model']!r}")
        rows.append(row)
    if not rows:
        raise ValueError(f"no evaluation receipts found under {evaluations_dir}")
    return rows


def _classify_receipt(row: dict[str, Any]) -> tuple[str, float | None]:
    """Return ``(outcome, score)``; only finite scores on completed rows count."""
    status = row["status"]
    if not isinstance(status, str) or status not in RECEIPT_STATUSES:
        raise ValueError(
            f"receipt for task {row['task_id']!r} has unknown status {status!r}; "
            "expected one of 'completed', 'pending', 'error'"
        )
    score = row["score"]
    if status == "completed":
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            return "completed_unscored", None
        return "completed", float(score)
    return status, None


def _check_treatment_binding(
    rows: list[dict[str, Any]], campaign_dir: Path
) -> tuple[str, str | None]:
    """Require one agent and one model across every row of a campaign."""
    agents = {row["agent"] for row in rows}
    models = {row["model"] for row in rows}
    if len(agents) != 1 or len(models) != 1:
        raise ValueError(
            f"campaign {campaign_dir} mixes treatment bindings "
            f"(agents={sorted(agents)}, models={sorted(str(model) for model in models)}); "
            "a comparison across different agents or models is not a harness comparison"
        )
    return agents.pop(), models.pop()


def _quantile(values: list[float], probability: float) -> float:
    """Linear-interpolation quantile over a nonempty list."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _cluster_bootstrap_interval(
    clusters: list[list[float]], *, seed: int, resamples: int
) -> dict[str, Any]:
    """Paired cluster bootstrap of the mean delta; unavailable below two clusters."""
    if resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if len(clusters) < 2:
        return {
            "available": False,
            "low": None,
            "high": None,
            "unavailable_reason": (
                f"cluster bootstrap needs at least two clusters, got {len(clusters)}"
            ),
        }
    generator = random.Random(seed)
    count = len(clusters)
    means: list[float] = []
    for _ in range(resamples):
        resampled: list[float] = []
        for _ in range(count):
            resampled.extend(clusters[generator.randrange(count)])
        means.append(statistics.mean(resampled))
    return {
        "available": True,
        "low": _quantile(means, 0.025),
        "high": _quantile(means, 0.975),
        "unavailable_reason": None,
    }


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Sum numeric usage keys where present; absent values stay None plus a count."""
    usage_dicts = [row["usage"] if isinstance(row["usage"], dict) else {} for row in rows]
    totals: dict[str, dict[str, Any]] = {}
    for key in USAGE_TOTAL_KEYS:
        known: list[int | float] = []
        unknown = 0
        for usage in usage_dicts:
            value = usage.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                unknown += 1
            else:
                known.append(value)
        if not known:
            total: int | float | None = None
        elif all(isinstance(value, int) for value in known):
            total = sum(known)
        else:
            try:
                total = math.fsum(known)
            except OverflowError as exc:
                raise ValueError(f"usage total for {key} exceeds finite float range") from exc
        totals[key] = {"total": total, "known_rows": len(known), "unknown_rows": unknown}
    return totals


def _task_identity(task_id: str, digest: str) -> dict[str, str]:
    return {"task_id": task_id, "task_package_digest": digest}


def _candidate_vs_stock(
    candidate_id: str,
    candidate_scores: dict[TaskKey, float],
    stock_scores: dict[TaskKey, float],
    candidate_rows: dict[TaskKey, dict[str, Any]],
    stock_rows: dict[TaskKey, dict[str, Any]],
    missingness: Counter[str],
    *,
    source_groups: dict[str, str] | None,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    """Paired outcome of one candidate against the stock candidate."""
    paired_keys = sorted(set(candidate_scores) & set(stock_scores))
    only_candidate = sorted(set(candidate_scores) - set(stock_scores))
    only_stock = sorted(set(stock_scores) - set(candidate_scores))

    paired_tasks: list[dict[str, Any]] = [
        {
            **_task_identity(*key),
            "stock_score": stock_scores[key],
            "candidate_score": candidate_scores[key],
            "delta": candidate_scores[key] - stock_scores[key],
        }
        for key in paired_keys
    ]
    regressions = [row for row in paired_tasks if row["delta"] < 0.0]

    if paired_keys:
        deltas = [row["delta"] for row in paired_tasks]
        clusters_map: dict[str | TaskKey, list[float]] = defaultdict(list)
        unmapped = 0
        for key, delta in zip(paired_keys, deltas, strict=True):
            if source_groups is not None:
                group = source_groups.get(key[0])
                if group is None:
                    unmapped += 1
                    clusters_map[key].append(delta)
                else:
                    clusters_map[group].append(delta)
            else:
                clusters_map[key].append(delta)
        clusters = [clusters_map[key] for key in sorted(clusters_map, key=repr)]
        uncertainty = {
            "method": "paired cluster bootstrap of the mean delta (2.5/97.5 percentiles)",
            "seed": seed,
            "resamples": resamples,
            "clusters": len(clusters),
            "source_groups_provided": source_groups is not None,
            "unmapped_tasks": unmapped,
            **_cluster_bootstrap_interval(clusters, seed=seed, resamples=resamples),
        }
        summary = {
            "paired_task_count": len(paired_keys),
            "paired_tasks": paired_tasks,
            "stock_mean_success": statistics.mean(stock_scores[key] for key in paired_keys),
            "candidate_mean_success": statistics.mean(candidate_scores[key] for key in paired_keys),
            "paired_mean_delta": statistics.mean(deltas),
            "regressions": regressions,
            "only_candidate_tasks": [_task_identity(*key) for key in only_candidate],
            "only_stock_tasks": [_task_identity(*key) for key in only_stock],
            "missingness": dict(sorted(missingness.items())),
            "usage_totals": {
                "paired_tasks": {
                    "stock": _usage_totals([stock_rows[key] for key in paired_keys]),
                    "candidate": _usage_totals([candidate_rows[key] for key in paired_keys]),
                }
            },
            "uncertainty": uncertainty,
        }
        return summary
    return {
        "paired_task_count": 0,
        "paired_tasks": [],
        "stock_mean_success": None,
        "candidate_mean_success": None,
        "paired_mean_delta": None,
        "regressions": [],
        "only_candidate_tasks": [_task_identity(*key) for key in only_candidate],
        "only_stock_tasks": [_task_identity(*key) for key in only_stock],
        "missingness": dict(sorted(missingness.items())),
        "usage_totals": {
            "paired_tasks": {
                "stock": _usage_totals([]),
                "candidate": _usage_totals([]),
            }
        },
        "uncertainty": {
            "method": "paired cluster bootstrap of the mean delta (2.5/97.5 percentiles)",
            "seed": seed,
            "resamples": resamples,
            "clusters": 0,
            "source_groups_provided": source_groups is not None,
            "unmapped_tasks": 0,
            "available": False,
            "low": None,
            "high": None,
            "unavailable_reason": "no tasks are covered by completed evaluations on both arms",
        },
    }


def _campaign_summary(
    campaign_dir: Path,
    *,
    stock_candidate: str,
    source_groups: dict[str, str] | None,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    """Per-campaign paired analysis of every candidate against the stock candidate."""
    rows = load_receipts(campaign_dir)
    agent, model = _check_treatment_binding(rows, campaign_dir)

    scores: dict[tuple[str, TaskKey], float] = {}
    row_by_identity: dict[tuple[str, TaskKey], dict[str, Any]] = {}
    missingness: dict[str, Counter[str]] = defaultdict(Counter)
    task_identities: set[TaskKey] = set()
    for row in rows:
        candidate_id = row["candidate_id"]
        key: TaskKey = (row["task_id"], row["task_package_digest"])
        task_identities.add(key)
        outcome, score = _classify_receipt(row)
        missingness[candidate_id][outcome] += 1
        if outcome == "completed":
            combined = (candidate_id, key)
            if combined in scores:
                raise ValueError(
                    f"duplicate receipts for candidate {candidate_id} on "
                    f"task {row['task_id']}; one evaluation per candidate/task is required"
                )
            assert score is not None
            scores[combined] = score
            row_by_identity.setdefault(combined, row)

    if not any(row["candidate_id"] == stock_candidate for row in rows):
        raise ValueError(f"stock candidate {stock_candidate} has no receipts in {campaign_dir}")
    stock_scores = {
        key: score
        for (candidate_id, key), score in scores.items()
        if candidate_id == stock_candidate
    }
    if not stock_scores:
        raise ValueError(
            f"stock candidate {stock_candidate} has no completed scored evaluations "
            f"in {campaign_dir}; no paired comparison is possible"
        )
    stock_rows = {
        key: row
        for (candidate_id, key), row in row_by_identity.items()
        if candidate_id == stock_candidate
    }

    candidate_ids = sorted({row["candidate_id"] for row in rows} - {stock_candidate})
    candidates: dict[str, dict[str, Any]] = {}
    for candidate_id in candidate_ids:
        candidate_scores = {
            key: score for (owner, key), score in scores.items() if owner == candidate_id
        }
        candidate_rows = {
            key: row for (owner, key), row in row_by_identity.items() if owner == candidate_id
        }
        candidates[candidate_id] = _candidate_vs_stock(
            candidate_id,
            candidate_scores,
            stock_scores,
            candidate_rows,
            stock_rows,
            missingness[candidate_id],
            source_groups=source_groups,
            seed=seed,
            resamples=resamples,
        )

    means_by_candidate: dict[str, list[float]] = defaultdict(list)
    for (owner, _key), score in scores.items():
        means_by_candidate[owner].append(score)
    search_means = {owner: statistics.mean(values) for owner, values in means_by_candidate.items()}
    # Ties in search-visible mean resolve to the incumbent stock arm: an
    # equal-scoring candidate is not evidence that search beat the seed.
    best_id = max(
        sorted(search_means),
        key=lambda owner: (search_means[owner], owner == stock_candidate),
    )
    best_delta = 0.0 if best_id == stock_candidate else candidates[best_id]["paired_mean_delta"]
    best_paired_count = (
        len(stock_scores)
        if best_id == stock_candidate
        else candidates[best_id]["paired_task_count"]
    )

    outcome_counts = Counter()
    for counter in missingness.values():
        outcome_counts.update(counter)

    return {
        "usage_totals": _usage_totals(rows),
        "campaign_dir": str(campaign_dir),
        "agent": agent,
        "model": model,
        "stock_candidate": stock_candidate,
        "task_identities": [_task_identity(*key) for key in sorted(task_identities)],
        "receipts": {
            "files": len(rows),
            "completed": outcome_counts.get("completed", 0),
            "pending": outcome_counts.get("pending", 0),
            "error": outcome_counts.get("error", 0),
            "completed_unscored": outcome_counts.get("completed_unscored", 0),
            "candidates": len(candidate_ids) + 1,
            "task_identities": len(task_identities),
        },
        "stock": {
            "missingness": dict(sorted(missingness[stock_candidate].items())),
            "completed_tasks": len(stock_scores),
            "scored_tasks": [
                {**_task_identity(*key), "stock_score": stock_scores[key]}
                for key in sorted(stock_scores)
            ],
        },
        "candidates": candidates,
        "best_by_search_score": {
            "candidate_id": best_id,
            "search_mean_score": search_means[best_id],
            "paired_mean_delta_vs_stock": best_delta,
            "paired_task_count": best_paired_count,
        },
    }


def _budget_check(
    campaign: dict[str, Any],
    control: dict[str, Any],
) -> dict[str, Any]:
    """Budget matching of completed evaluations, task set, and treatment binding."""
    reasons: list[str] = []
    campaign_identities = {
        (row["task_id"], row["task_package_digest"]) for row in campaign["task_identities"]
    }
    control_identities = {
        (row["task_id"], row["task_package_digest"]) for row in control["task_identities"]
    }
    campaign_completed = campaign["receipts"]["completed"]
    control_completed = control["receipts"]["completed"]
    if campaign_completed != control_completed:
        reasons.append(
            f"completed evaluations differ: campaign={campaign_completed}, "
            f"control={control_completed}"
        )
    campaign_only = sorted(campaign_identities - control_identities)
    control_only = sorted(control_identities - campaign_identities)
    if campaign_only or control_only:
        reasons.append(
            f"task sets differ: {len(campaign_only)} campaign-only and "
            f"{len(control_only)} control-only task identities"
        )
    if campaign["agent"] != control["agent"]:
        reasons.append(
            f"agent differs: campaign={campaign['agent']!r}, control={control['agent']!r}"
        )
    if campaign["model"] != control["model"]:
        reasons.append(
            f"model differs: campaign={campaign['model']!r}, control={control['model']!r}"
        )
    return {
        "basis": "completed_evaluation_count_only",
        "completed_evaluations": {"campaign": campaign_completed, "control": control_completed},
        "task_identity_counts": {
            "campaign": len(campaign_identities),
            "control": len(control_identities),
        },
        "completed_evaluations_equal": campaign_completed == control_completed,
        "task_set_equal": not campaign_only and not control_only,
        "agent_equal": campaign["agent"] == control["agent"],
        "model_equal": campaign["model"] == control["model"],
        "mismatch_reasons": reasons,
        "campaign_only_task_identities": [_task_identity(*key) for key in campaign_only],
        "control_only_task_identities": [_task_identity(*key) for key in control_only],
    }


def analyze_campaign(
    campaign_dir: Path,
    *,
    stock_candidate: str,
    control_dir: Path | None = None,
    source_groups: Mapping[str, str] | None = None,
    seed: int,
    resamples: int = 2000,
) -> dict[str, Any]:
    """Analyze paired candidate-vs-stock outcomes from retained evaluation receipts.

    Args:
        campaign_dir: Campaign output directory containing ``evaluations/``.
        stock_candidate: Exact full ``sha256:<64-hex>`` digest of the stock/seed arm.
        control_dir: Optional no-feedback control campaign directory for the
            budget-matched method control.
        source_groups: Optional task_id -> cluster group mapping for the
            bootstrap; unmapped tasks form their own cluster.
        seed: Bootstrap seed; the report is deterministic under a fixed seed.
        resamples: Bootstrap resample count (positive).

    Returns:
        A JSON-serialisable report (``allow_nan=False`` safe). Raises
        ``ValueError`` on any refusal (symlinks, mixed treatment bindings,
        ambiguous stock digest, absent stock arm, malformed receipts).
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    stock = _validate_stock_candidate(stock_candidate)
    groups = _validate_source_groups(source_groups)

    campaign = _campaign_summary(
        Path(campaign_dir),
        stock_candidate=stock,
        source_groups=groups,
        seed=seed,
        resamples=resamples,
    )

    if control_dir is None:
        method_control: dict[str, Any] = {"status": "missing"}
    else:
        control_path = Path(control_dir)
        control = _campaign_summary(
            control_path,
            stock_candidate=stock,
            source_groups=groups,
            seed=seed,
            resamples=resamples,
        )
        budget = _budget_check(campaign, control)
        campaign_best = campaign["best_by_search_score"]
        control_best = control["best_by_search_score"]
        paired_support = []
        for summary in (campaign, control):
            best_id = summary["best_by_search_score"]["candidate_id"]
            selected = (
                summary["stock"]["scored_tasks"]
                if best_id == stock
                else summary["candidates"][best_id]["paired_tasks"]
            )
            paired_support.append(
                {
                    (row["task_id"], row["task_package_digest"]): row["stock_score"]
                    for row in selected
                }
            )
        support_equal = bool(paired_support[0]) and paired_support[0] == paired_support[1]
        method_control = {
            "status": "evaluation_count_matched"
            if not budget["mismatch_reasons"]
            else "budget_mismatch",
            "control_dir": str(control_path),
            "budget_check": budget,
            "campaign_best": campaign_best,
            "control_best": control_best,
            "paired_task_and_stock_scores_equal": support_equal,
            "paired_delta_difference": (
                campaign_best["paired_mean_delta_vs_stock"]
                - control_best["paired_mean_delta_vs_stock"]
                if not budget["mismatch_reasons"]
                and support_equal
                and campaign_best["paired_mean_delta_vs_stock"] is not None
                and control_best["paired_mean_delta_vs_stock"] is not None
                else None
            ),
            "caveat": CAVEAT,
            "control_usage_totals": control["usage_totals"],
        }

    report = {
        "schema_version": 1,
        "caveat": CAVEAT,
        "source_groups": groups,
        "campaign": campaign,
        "method_control": method_control,
    }
    # Enforce the serialisability contract before the caller sees the report.
    json.dumps(report, allow_nan=False)
    return report
