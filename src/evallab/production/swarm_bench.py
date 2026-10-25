"""DEV-only, zero-inference swarm selection benchmark.

Selectors consume worker predictions only. ``run_benchmark`` freezes all selections
before loading the sealed evaluator bundle. Scores describe agreement with the
retained DEV answer key, not held-out performance or certified false-positive rates.

Run ``python -m evallab.production.swarm_bench --help`` for the offline CLI.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BUNDLE_DIR = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-24/cross-model-swarm-bench"
)
FALLBACK_BUNDLE_DIR = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-24-production/raw"
)
_MODEL_ORDER = ("muse", "kimi", "sonnet", "codex")
_MODEL_PRIORITY = {alias: len(_MODEL_ORDER) - index for index, alias in enumerate(_MODEL_ORDER)}
_CONTENT_STOPWORDS = frozenset(
    (
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "not",
        "of",
        "on",
        "or",
        "otherwise",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "was",
        "were",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
    )
)
_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9]*")
_HARNESS_FILES = frozenset({"conftest.py", "test.sh", "task.toml"})

type Record = Mapping[str, Any]


@dataclass(frozen=True)
class SwarmBenchBundle:
    """Evaluator-side inputs; never pass this object to a selector."""

    manifest: Record
    dev_key: Sequence[Record]
    predictions: Sequence[Record]
    adjudication: Record
    attestation: Record
    bundle_dir: Path = DEFAULT_BUNDLE_DIR

    def __post_init__(self) -> None:
        _validate_dev_key(self.dev_key)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _record(value: Any, description: str) -> Record:
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} must be a JSON object")
    return value


def _records(value: Any, description: str) -> list[Record]:
    if not isinstance(value, list):
        raise ValueError(f"{description} must be a JSON array")
    return [_record(item, description) for item in value]


def _validate_dev_key(dev_key: Sequence[Record]) -> None:
    if any(item.get("split") != "dev" for item in dev_key):
        raise ValueError("Split-leak guard: every answer-key item must have split == 'dev'")


def _bundle_root(bundle_dir: str | Path | None) -> Path:
    if bundle_dir is not None:
        return Path(bundle_dir)
    return DEFAULT_BUNDLE_DIR if DEFAULT_BUNDLE_DIR.is_dir() else FALLBACK_BUNDLE_DIR


def _archive_index(root: Path) -> Record:
    index_path = root / "cross-model-swarm-bench-evidence-index.json"
    return _record(_read_json(index_path), "evidence index") if index_path.is_file() else {}


def _artifact_path(root: Path, name: str) -> Path:
    candidates = [root / name, root / f"cross-model-swarm-bench-{name}"]
    # The committed archive retains references for the large key/prediction pack,
    # not copies of those bytes. Resolve those references, never synthesize data.
    if not any(path.is_file() for path in candidates):
        archive = _archive_index(root)
        if name == "dev-key-subset.json":
            inventory = _record(archive.get("unfair_fail_inventory", {}), "unfair inventory")
            reference = _record(inventory.get("dev_key_subset", {}), "DEV key reference")
            if isinstance(reference.get("path"), str):
                candidates.append(Path(reference["path"]))
        manifest = _record(archive.get("manifest", {}), "archived manifest")
        for case in manifest.get("cases", []):
            prompt_path = _record(case, "manifest case").get("prompt_path")
            if isinstance(prompt_path, str):
                candidates.append(Path(prompt_path).parent.parent / name)
                break
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f"Missing retained {name}; tried: {', '.join(map(str, candidates))}")


def load_predictions(bundle_dir: str | Path | None = None) -> list[Record]:
    """Load the only input visible to candidate selectors, without opening key files."""
    return _records(
        _read_json(_artifact_path(_bundle_root(bundle_dir), "finding-pack.json")), "finding pack"
    )


def load_bundle(bundle_dir: str | Path | None = None) -> SwarmBenchBundle:
    """Load and validate evaluator inputs from a retained bundle or its archive."""
    root = _bundle_root(bundle_dir)
    manifest_path = root / "manifest.json"
    manifest = (
        _record(_read_json(manifest_path), "manifest")
        if manifest_path.is_file()
        else _record(_archive_index(root).get("manifest"), "archived manifest")
    )
    dev_key = _records(_read_json(_artifact_path(root, "dev-key-subset.json")), "DEV key")
    _validate_dev_key(dev_key)
    return SwarmBenchBundle(
        manifest=manifest,
        dev_key=dev_key,
        predictions=load_predictions(root),
        adjudication=_record(_read_json(_artifact_path(root, "adjudication.json")), "adjudication"),
        attestation=_record(_read_json(_artifact_path(root, "attestation.json")), "attestation"),
        bundle_dir=root,
    )


@dataclass(frozen=True)
class _Prediction:
    prediction_id: str
    task: str
    alias: str
    index: int
    direction: str
    path: str
    line: int
    end_line: int
    file_valid: bool
    range_valid: bool
    quote_valid: bool | None
    mechanism_tokens: frozenset[str]
    quote_tokens: frozenset[str]


def _content_tokens(value: Any) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_PATTERN.findall(str(value).lower())
        if len(token) > 2 and token not in _CONTENT_STOPWORDS
    )


def _parse_predictions(predictions: Sequence[Record]) -> list[_Prediction]:
    parsed: list[_Prediction] = []
    seen: set[str] = set()
    for raw in predictions:
        prediction_id = str(raw["prediction_id"])
        if prediction_id in seen:
            raise ValueError(f"Duplicate prediction_id in worker pool: {prediction_id}")
        seen.add(prediction_id)
        finding = _record(raw.get("finding", {}), "finding")
        location = _record(finding.get("location", {}), "finding location")
        check = _record(raw.get("location_check", {}), "location check")
        line = int(location.get("line", 0))
        end_line = int(location.get("end_line", line))
        parsed.append(
            _Prediction(
                prediction_id=prediction_id,
                task=str(raw["task"]),
                alias=str(raw["alias"]),
                index=int(raw["index"]),
                direction=str(finding.get("direction", "")),
                path=str(location.get("path", "")),
                line=line,
                end_line=end_line,
                file_valid=check.get("file_exists") is True,
                range_valid=check.get("line_range_exists") is True and 0 < line <= end_line,
                quote_valid=(
                    check["quote_found_in_cited_range"] is True
                    if "quote_found_in_cited_range" in check
                    else None
                ),
                mechanism_tokens=_content_tokens(finding.get("mechanism", "")),
                quote_tokens=_content_tokens(location.get("quote", "")),
            )
        )
    return parsed


def _budget(budget_per_task: int) -> None:
    if budget_per_task < 0:
        raise ValueError("budget_per_task must be nonnegative")


def _by_task(predictions: Sequence[_Prediction]) -> dict[str, list[_Prediction]]:
    grouped: dict[str, list[_Prediction]] = defaultdict(list)
    for prediction in predictions:
        grouped[prediction.task].append(prediction)
    return dict(sorted(grouped.items()))


def _within_window(left: _Prediction, right: _Prediction, window: int) -> bool:
    return (
        bool(left.path)
        and left.path == right.path
        and left.line <= right.end_line + window
        and right.line <= left.end_line + window
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _same_locus(left: _Prediction, right: _Prediction) -> bool:
    return left.direction == right.direction and (
        _within_window(left, right, 25)
        or _jaccard(left.mechanism_tokens, right.mechanism_tokens) >= 0.35
        or _jaccard(left.quote_tokens, right.quote_tokens) >= 0.35
    )


@dataclass(frozen=True)
class _Cluster:
    members: tuple[_Prediction, ...]

    @property
    def models(self) -> frozenset[str]:
        return frozenset(member.alias for member in self.members)

    @property
    def score(self) -> tuple[int, int, int]:
        return (
            len(self.models),
            sum(member.quote_valid is True for member in self.members),
            -min(member.index for member in self.members),
        )

    @property
    def direction(self) -> str:
        return self.members[0].direction

    @property
    def harness_boundary(self) -> bool:
        return any(Path(member.path).name in _HARNESS_FILES for member in self.members)


def _representative(cluster: _Cluster, order: tuple[str, ...] = _MODEL_ORDER) -> _Prediction:
    priority = {alias: len(order) - index for index, alias in enumerate(order)}
    return max(
        cluster.members,
        key=lambda prediction: (
            prediction.quote_valid is True,
            priority.get(prediction.alias, 0),
            prediction.index == 1,
            -prediction.index,
            prediction.prediction_id,
        ),
    )


def _rank(cluster: _Cluster) -> tuple[int, int, int, int, str]:
    representative = _representative(cluster)
    return (
        *cluster.score,
        _MODEL_PRIORITY.get(representative.alias, 0),
        representative.prediction_id,
    )


def _clusters(predictions: Sequence[_Prediction], *, require_quote: bool) -> list[_Cluster]:
    eligible = sorted(
        (
            prediction
            for prediction in predictions
            if prediction.file_valid
            and prediction.range_valid
            and (not require_quote or prediction.quote_valid is not False)
        ),
        key=lambda prediction: prediction.prediction_id,
    )
    # Connected components make pairwise locus membership independent of input order.
    parents = list(range(len(eligible)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, prediction in enumerate(eligible):
        for other_index in range(index):
            if _same_locus(prediction, eligible[other_index]):
                parents[root(index)] = root(other_index)
    components: dict[int, list[_Prediction]] = defaultdict(list)
    for index, prediction in enumerate(eligible):
        components[root(index)].append(prediction)
    return sorted(
        (_Cluster(tuple(members)) for members in components.values()), key=_rank, reverse=True
    )


def select_single_model(
    predictions: Sequence[Record], alias: str, budget_per_task: int = 3
) -> list[str]:
    """Preserve the named worker's top-ranked findings, up to the common task budget."""
    _budget(budget_per_task)
    return [
        prediction.prediction_id
        for task_predictions in _by_task(_parse_predictions(predictions)).values()
        for prediction in sorted(
            (prediction for prediction in task_predictions if prediction.alias == alias),
            key=lambda prediction: (prediction.index, prediction.prediction_id),
        )[:budget_per_task]
    ]


def select_naive_union(predictions: Sequence[Record]) -> list[str]:
    """Unbudgeted pool ceiling baseline; this is not an equal-budget contender."""
    return [prediction.prediction_id for prediction in _parse_predictions(predictions)]


def select_round_robin_top1(predictions: Sequence[Record], budget_per_task: int = 3) -> list[str]:
    """Take only each worker's index-1 finding, suppressing nearby same-direction loci."""
    _budget(budget_per_task)
    selected: list[str] = []
    for task_predictions in _by_task(_parse_predictions(predictions)).values():
        task_selected: list[_Prediction] = []
        for alias in _MODEL_ORDER:
            candidate = next(
                (
                    prediction
                    for prediction in task_predictions
                    if prediction.alias == alias and prediction.index == 1
                ),
                None,
            )
            if candidate is None or len(task_selected) >= budget_per_task:
                continue
            if not any(
                candidate.direction == previous.direction
                and _within_window(candidate, previous, 15)
                for previous in task_selected
            ):
                task_selected.append(candidate)
        selected.extend(prediction.prediction_id for prediction in task_selected)
    return selected


def select_locus_consensus_v1(predictions: Sequence[Record], budget_per_task: int = 3) -> list[str]:
    """Rank citation-valid locus components by model consensus, quotes, then worker rank."""
    _budget(budget_per_task)
    return [
        _representative(cluster).prediction_id
        for task_predictions in _by_task(_parse_predictions(predictions)).values()
        for cluster in _clusters(task_predictions, require_quote=False)[:budget_per_task]
    ]


def _anchor_rank(cluster: _Cluster) -> tuple[int, int, bool, int, int, str]:
    representative = _representative(cluster)
    return (
        len(cluster.models),
        _MODEL_PRIORITY.get(representative.alias, 0),
        representative.index == 1,
        -representative.index,
        cluster.score[1],
        representative.prediction_id,
    )


def _specialist_order(cluster: _Cluster) -> tuple[str, ...]:
    if cluster.direction == "unfair_fail":
        return ("sonnet", "kimi", "muse", "codex")
    if cluster.harness_boundary:
        return ("codex", "sonnet", "muse", "kimi")
    return _MODEL_ORDER


def _specialist_rank(
    cluster: _Cluster, anchor_direction: str
) -> tuple[int, int, int, int, int, str]:
    order = _specialist_order(cluster)
    role_priority = max(
        (len(order) - order.index(alias) for alias in cluster.models if alias in order),
        default=0,
    )
    return (
        cluster.direction != anchor_direction,
        role_priority,
        *cluster.score,
        _representative(cluster, order).prediction_id,
    )


def select_specialist_diversity_v2(
    predictions: Sequence[Record], budget_per_task: int = 3
) -> list[str]:
    """Route consensus, opposite-direction/harness, and distinct-mechanism slots blindly."""
    _budget(budget_per_task)
    selected: list[str] = []
    for task_predictions in _by_task(_parse_predictions(predictions)).values():
        clusters = _clusters(task_predictions, require_quote=True)
        if not clusters or budget_per_task == 0:
            continue
        chosen = [max(clusters, key=_anchor_rank)]
        representatives = [_representative(chosen[0])]
        if budget_per_task >= 2:
            specialists = [
                cluster
                for cluster in clusters
                if cluster is not chosen[0]
                and (cluster.direction != chosen[0].direction or cluster.harness_boundary)
            ]
            if specialists:
                specialist = max(
                    specialists,
                    key=lambda cluster: _specialist_rank(cluster, chosen[0].direction),
                )
                chosen.append(specialist)
                representatives.append(_representative(specialist, _specialist_order(specialist)))
        for cluster in clusters:
            if len(chosen) >= budget_per_task:
                break
            if cluster in chosen:
                continue
            representative = _representative(cluster)
            if any(_within_window(representative, previous, 0) for previous in representatives):
                continue
            chosen.append(cluster)
            representatives.append(representative)
        selected.extend(prediction.prediction_id for prediction in representatives)
    return selected


def select_from_external_file(
    predictions: Sequence[Record], selection_json_path: str | Path, budget_per_task: int = 3
) -> list[str]:
    """Validate an independently frozen LLM selection without consulting evaluator data."""
    _budget(budget_per_task)
    pool = {prediction.prediction_id: prediction for prediction in _parse_predictions(predictions)}
    document = _record(_read_json(Path(selection_json_path)), "external selection")
    selected: list[str] = []
    if "selected_prediction_ids" in document:
        values = document["selected_prediction_ids"]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("selected_prediction_ids must be a list of strings")
        selected.extend(values)
    else:
        tasks = {prediction.task for prediction in pool.values()}
        for task, values in document.items():
            if task not in tasks:
                raise ValueError(f"Unknown selection task: {task}")
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"Selection for {task} must be a list of strings")
            for prediction_id in values:
                if prediction_id not in pool or pool[prediction_id].task != task:
                    raise ValueError(f"Unknown or off-task prediction_id: {prediction_id}")
            selected.extend(values)
    if len(set(selected)) != len(selected):
        raise ValueError("External selection contains duplicate prediction IDs")
    counts: Counter[str] = Counter()
    for prediction_id in selected:
        if prediction_id not in pool:
            raise ValueError(f"Unknown selected prediction_id: {prediction_id}")
        counts[pool[prediction_id].task] += 1
    if any(count > budget_per_task for count in counts.values()):
        raise ValueError("External selection exceeds budget_per_task")
    return selected


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return _ratio(2 * precision * recall, precision + recall)


def _evaluation_inputs(
    bundle: SwarmBenchBundle,
) -> tuple[dict[str, str], set[str], dict[str, str | None]]:
    _validate_dev_key(bundle.dev_key)
    key_tasks = {str(item["item_id"]): str(item["task"]) for item in bundle.dev_key}
    if len(key_tasks) != len(bundle.dev_key):
        raise ValueError("Duplicate answer-key item_id")
    anchors = {
        str(case["anchor_id"]) for case in _records(bundle.manifest.get("cases"), "manifest cases")
    }
    if not anchors <= key_tasks.keys():
        raise ValueError("Every primary anchor must belong to the DEV key")
    predictions = {
        str(prediction["prediction_id"]): prediction for prediction in bundle.predictions
    }
    if len(predictions) != len(bundle.predictions):
        raise ValueError("Duplicate worker prediction_id")
    matches: dict[str, str | None] = {}
    for item in _records(bundle.adjudication.get("predictions"), "adjudication predictions"):
        prediction_id = str(item["prediction_id"])
        matched_key_id = item.get("matched_key_id")
        if prediction_id in matches or prediction_id not in predictions:
            raise ValueError(f"Duplicate or unknown adjudication prediction_id: {prediction_id}")
        if matched_key_id is not None:
            matched_key_id = str(matched_key_id)
            if matched_key_id not in key_tasks:
                raise ValueError(f"Adjudication references a non-DEV key: {matched_key_id}")
            if key_tasks[matched_key_id] != predictions[prediction_id]["task"]:
                raise ValueError(f"Adjudication credits a key from another task: {prediction_id}")
        matches[prediction_id] = matched_key_id
    if matches.keys() != predictions.keys():
        raise ValueError("Every worker prediction must have an explicit adjudication")
    return key_tasks, anchors, matches


def _score_counts(
    selected_prediction_ids: Sequence[str],
    *,
    key_tasks: Mapping[str, str],
    anchors: set[str],
    matches: Mapping[str, str | None],
) -> dict[str, Any]:
    if len(set(selected_prediction_ids)) != len(selected_prediction_ids):
        raise ValueError("Selection contains duplicate prediction IDs")
    unknown = set(selected_prediction_ids) - matches.keys()
    if unknown:
        raise ValueError(f"Unknown selected prediction IDs: {sorted(unknown)}")
    credited = [matches[prediction_id] for prediction_id in selected_prediction_ids]
    unique_keys = {key for key in credited if key is not None}
    pool_keys = {key for key in matches.values() if key is not None}
    credited_selected = sum(key is not None for key in credited)
    uncredited_selected = len(credited) - credited_selected
    secondary_hits = len(unique_keys)
    primary_hits = len(unique_keys & anchors)
    primary_precision = _ratio(primary_hits, len(credited))
    precision = _ratio(secondary_hits, len(credited))
    primary_recall = _ratio(primary_hits, len(anchors))
    secondary_recall = _ratio(secondary_hits, len(key_tasks))
    pool_credited = sum(key is not None for key in matches.values())
    return {
        "selected_count": len(credited),
        "primary_hits_n": primary_hits,
        "primary_total_n": len(anchors),
        "primary_recall": primary_recall,
        "primary_precision": primary_precision,
        "primary_f1": _f1(primary_precision, primary_recall),
        "secondary_hits_n": secondary_hits,
        "secondary_total_n": len(key_tasks),
        "secondary_recall": secondary_recall,
        "unique_key_precision": precision,
        "secondary_f1": _f1(precision, secondary_recall),
        "credited_key_ids": sorted(unique_keys),
        "stage_attribution": {
            "worker_pool_ceiling_n": len(pool_keys),
            "worker_miss_n": len(key_tasks) - len(pool_keys),
            "merger_kept_n": secondary_hits,
            "merger_dropped_n": len(pool_keys) - secondary_hits,
            "merger_retention_rate": _ratio(secondary_hits, len(pool_keys)),
            "duplicate_hits_filtered_n": pool_credited - credited_selected,
            "false_alarms_emitted_n": len(credited) - secondary_hits,
            "false_alarms_filtered_n": len(matches) - pool_credited - uncredited_selected,
            "credited_selected_predictions": credited_selected,
            "uncredited_selected_predictions": uncredited_selected,
            "redundant_selected_predictions": credited_selected - secondary_hits,
            "pool_credited_predictions": pool_credited,
            "pool_uncredited_predictions": len(matches) - pool_credited,
        },
    }


def score_strategy(
    selected_prediction_ids: Sequence[str],
    bundle: SwarmBenchBundle,
    *,
    budget_per_task: int = 3,
) -> dict[str, Any]:
    """Score frozen IDs, crediting each same-task DEV key at most once.

    Primary F1 uses primary-anchor precision (primary hits / emitted predictions).
    ``false_alarms`` is the requested off-key/redundancy accounting label; unmatched
    source hypotheses are not thereby scientifically certified false positives.
    The unbudgeted union receives descriptive deltas, marked non-equal-budget.
    """
    _budget(budget_per_task)
    key_tasks, anchors, matches = _evaluation_inputs(bundle)
    score = _score_counts(
        selected_prediction_ids, key_tasks=key_tasks, anchors=anchors, matches=matches
    )
    single_scores = {
        alias: _score_counts(
            select_single_model(bundle.predictions, alias, budget_per_task),
            key_tasks=key_tasks,
            anchors=anchors,
            matches=matches,
        )
        for alias in sorted({str(prediction["alias"]) for prediction in bundle.predictions})
    }
    if single_scores:
        best_alias = max(
            single_scores,
            key=lambda alias: (
                single_scores[alias]["secondary_f1"],
                single_scores[alias]["secondary_recall"],
                single_scores[alias]["primary_hits_n"],
                _MODEL_PRIORITY.get(alias, 0),
            ),
        )
        best = single_scores[best_alias]
    else:
        best_alias = None
        best = _score_counts([], key_tasks=key_tasks, anchors=anchors, matches=matches)
    pool = {str(prediction["prediction_id"]): prediction for prediction in bundle.predictions}
    task_selections: dict[str, list[str]] = defaultdict(list)
    for prediction_id in selected_prediction_ids:
        task_selections[str(pool[prediction_id]["task"])].append(prediction_id)
    score.update(
        {
            "selected_prediction_ids": list(selected_prediction_ids),
            "selection_by_task": dict(sorted(task_selections.items())),
            "budget_per_task": budget_per_task,
            "within_task_budget": all(
                len(values) <= budget_per_task for values in task_selections.values()
            ),
            "best_single_alias": best_alias,
            "equal_budget_gain_vs_best_single": {
                "delta_primary_hits": score["primary_hits_n"] - best["primary_hits_n"],
                "delta_secondary_hits": score["secondary_hits_n"] - best["secondary_hits_n"],
                "delta_precision_pp": 100
                * (score["unique_key_precision"] - best["unique_key_precision"]),
                "delta_secondary_f1_pp": 100 * (score["secondary_f1"] - best["secondary_f1"]),
            },
        }
    )
    return score


def _find_track_file(root: Path, names: Sequence[str]) -> Path | None:
    for directory in dict.fromkeys((root, FALLBACK_BUNDLE_DIR)):
        for name in names:
            path = directory / name
            if path.is_file():
                return path
    return None


def _repair_track(path: Path) -> dict[str, Any]:
    document = _record(_read_json(path), "repair swarm")
    round1 = _records(document.get("round1_results"), "repair round 1")
    round2 = _records(document.get("round2_results"), "repair round 2")
    r1_hits = sum(row.get("qualified") is True for row in round1)
    r2_units: dict[tuple[str, str], bool] = {}
    for row in round2:
        unit = (str(row["proposer"]), str(row["task"]))
        r2_units[unit] = r2_units.get(unit, False) or row.get("qualified") is True
    faults = [row for row in round2 if row.get("edit_applied_ok") is False]
    isolated = sum(row.get("qualified") is True for row in faults)
    r2_hits = sum(r2_units.values())
    return {
        "track": 2,
        "name": "Task Quality Repair + Challenger Swarm",
        "status": "available",
        "source_path": str(path),
        "round_1": {
            "qualified_n": r1_hits,
            "total_n": len(round1),
            "rate": _ratio(r1_hits, len(round1)),
        },
        "round_2": {
            "qualified_n": r2_hits,
            "total_n": len(r2_units),
            "rate": _ratio(r2_hits, len(r2_units)),
        },
        "challenger_lift_pp": 100 * (_ratio(r2_hits, len(r2_units)) - _ratio(r1_hits, len(round1))),
        "round_2_challenger_calls_n": len(round2),
        "round_2_raw": {
            "total_n": len(round2),
            "qualified_n": sum(row.get("qualified") is True for row in round2),
            "edit_applied_ok_true_n": sum(row.get("edit_applied_ok") is True for row in round2),
            "edit_applied_ok_false_n": len(faults),
        },
        "challenger_faults": {
            "unparsed_or_unapplied_response_n": len(faults),
            "fault_rate": _ratio(len(faults), len(round2)),
            "retained_qualified_n": isolated,
            "fault_isolation_rate": _ratio(isolated, len(faults)),
            "http_429_n": None,
            "note": "Unparsed or unapplied responses are observed; HTTP status is not in this summary. Isolation rate is retained qualification among those responses.",
        },
        "round_2_pooling": "Best qualified challenger result per (proposer, task), including reroutes; not 40 independent repairs.",
    }


def _seeker_track(path: Path) -> dict[str, Any]:
    document = _record(_read_json(path), "seeker swarm")
    summary = _record(document.get("summary"), "seeker summary")
    outcomes = _records(summary.get("outcomes"), "seeker outcomes")
    total = int(summary.get("tasks_attempted", len(outcomes)))
    first = int(summary.get("attempt_1_confirmed", 0))
    second = sum(
        row.get("exploit_confirmed") is True and int(row.get("attempts_run", 0)) <= 2
        for row in outcomes
    )
    controls = _records(document.get("unfair_fail_controls", []), "unfair-fail controls")
    false_positives = sum(row.get("exploit_confirmed") is True for row in controls)
    controls_in_file = len(controls)
    control_path = path.with_name("muse-seeker-swarm.json")
    if not controls and control_path.is_file():
        baseline = _record(_read_json(control_path), "seeker baseline")
        baseline_summary = _record(baseline.get("summary"), "seeker baseline summary")
        controls = [
            row
            for row in _records(baseline_summary.get("outcomes"), "baseline seeker outcomes")
            if str(row.get("task_id", "")).rsplit("/", 1)[-1]
            in {"photonic-waveguide-routing", "cad-model"}
        ]
        false_positives = sum(row.get("exploit_confirmed") is True for row in controls)
    return {
        "track": 3,
        "name": "Reward Seeker Failure-Memory Handoff Swarm",
        "status": "available",
        "source_path": str(path),
        "attempt_1": {"confirmed_n": first, "total_n": total, "rate": _ratio(first, total)},
        "attempt_2": {"confirmed_n": second, "total_n": total, "rate": _ratio(second, total)},
        "handoff_lift_pp": 100 * (_ratio(second, total) - _ratio(first, total)),
        "unfair_fail_controls_in_file": controls_in_file,
        "unfair_fail_controls": {
            "total_n": len(controls),
            "source_path": str(path if controls_in_file else control_path) if controls else None,
            "false_positives_n": false_positives if controls else None,
            "false_positive_rate": _ratio(false_positives, len(controls)) if controls else None,
            "status": "measured" if controls else "not_present_in_round2_artifact",
            "cohort": "Round-1 unfair-fail controls, separate from the 10 Round-2 wrong-pass tasks"
            if controls and not controls_in_file
            else "Round-2 artifact",
        },
        "paid_api_spend_usd": document.get("paid_api_spend_usd"),
    }


def summarize_swarm_tracks(
    strategy_scores: Mapping[str, Record],
    *,
    bundle_dir: str | Path | None = None,
    repair_summary_path: str | Path | None = None,
    seeker_summary_path: str | Path | None = None,
) -> dict[str, Any]:
    """Combine discovery scores and evidence-backed repair/seeker swarm health."""
    root = _bundle_root(bundle_dir)
    repair = (
        Path(repair_summary_path)
        if repair_summary_path is not None
        else _find_track_file(
            root, ("repair-swarm/summary.json", "cross-model-repair-challenger-swarm.json")
        )
    )
    seeker = (
        Path(seeker_summary_path)
        if seeker_summary_path is not None
        else _find_track_file(root, ("muse-seeker-swarm-round2.json",))
    )
    return {
        "schema": "evallab.swarm_tracks/v1",
        "split": "dev",
        "limitations": "Retained, purpose-selected DEV diagnostic; no held-out/generalization claim. Off-key predictions are unverified, not certified false positives.",
        "tracks": [
            {
                "track": 1,
                "name": "Task Quality Discovery Hill-Climb",
                "status": "available",
                "strategies": dict(strategy_scores),
            },
            _repair_track(repair)
            if repair is not None
            else {
                "track": 2,
                "name": "Task Quality Repair + Challenger Swarm",
                "status": "missing_artifact",
            },
            _seeker_track(seeker)
            if seeker is not None
            else {
                "track": 3,
                "name": "Reward Seeker Failure-Memory Handoff Swarm",
                "status": "missing_artifact",
            },
        ],
    }


def run_benchmark(
    bundle_dir: str | Path | None = None,
    *,
    external_selection: str | Path | None = None,
    budget_per_task: int = 3,
) -> dict[str, Any]:
    """Freeze all blind selections, then open the evaluator and compute the report."""
    predictions = load_predictions(bundle_dir)
    selections = {
        f"single_{alias}": select_single_model(predictions, alias, budget_per_task)
        for alias in _MODEL_ORDER
    }
    selections.update(
        {
            "naive_union": select_naive_union(predictions),
            "round_robin_top1": select_round_robin_top1(predictions, budget_per_task),
            "locus_consensus_v1": select_locus_consensus_v1(predictions, budget_per_task),
            "specialist_diversity_v2": select_specialist_diversity_v2(predictions, budget_per_task),
        }
    )
    if external_selection is not None:
        selections["llm_adjudicator"] = select_from_external_file(
            predictions, external_selection, budget_per_task
        )
    bundle = load_bundle(bundle_dir)
    if list(bundle.predictions) != predictions:
        raise ValueError("Finding pack changed after selections were frozen")
    scores = {
        name: score_strategy(selected, bundle, budget_per_task=budget_per_task)
        for name, selected in selections.items()
    }
    report = summarize_swarm_tracks(scores, bundle_dir=bundle.bundle_dir)
    report.update(
        {
            "schema": "evallab.swarm_bench/v1",
            "source_commit": bundle.manifest.get("source_commit"),
            "bundle_dir": str(bundle.bundle_dir),
            "budget_per_task": budget_per_task,
            "paid_api_spend_usd": 0.0,
            "model_calls": 0,
            "strategies": scores,
            "worker_telemetry": bundle.attestation.get("models", {}),
        }
    )
    return report


def _markdown_table(scores: Mapping[str, Record]) -> str:
    lines = [
        "| Strategy | Selected | Primary | Secondary | Key precision | Secondary F1 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, score in scores.items():
        label = f"{name} (unbudgeted)" if not score["within_task_budget"] else name
        lines.append(
            f"| {label} | {score['selected_count']} | "
            f"{score['primary_hits_n']}/{score['primary_total_n']} | "
            f"{score['secondary_hits_n']}/{score['secondary_total_n']} | "
            f"{100 * score['unique_key_precision']:.2f}% | {100 * score['secondary_f1']:.2f}% |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print a concise table and the reproducible JSON report; no model calls occur."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument("--external-selection", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_benchmark(args.bundle_dir, external_selection=args.external_selection)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(encoded, encoding="utf-8")
    print(_markdown_table(report["strategies"]))
    print("\nDEV-only key agreement; naive union is not equal-budget; paid API spend: $0.\n")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
