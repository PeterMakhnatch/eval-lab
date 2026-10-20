#!/usr/bin/env python3
"""Finding-level, exposure-stratified scorer for QA predictions on tb4-audit-v1.

A task often carries several PM findings, so crediting every finding on a task
the auditor flagged (``legacy_any``) or every same-category finding on it
(``legacy_task_broadcast``; generalized to every finding a ``category_1to1`` edge
reaches) inflates recall. This scorer instead runs a maximum
1-to-1 bipartite matching (Kuhn's augmenting paths) per task: each prediction
is credited to at most one ground-truth finding and each finding at most once.

Matching modes form a ladder; each accepts every edge of the stricter modes, so
``exact_id <= locus_and_class <= category_1to1 <= task_any_1to1`` always holds:

- ``exact_id``: the prediction names the finding's ``item_id`` in ``id`` or
  ``root_cause``.
- ``locus_and_class``: or the prediction's ``evidence_refs``/``summary`` cite a
  file path or symbol from the finding's ``port.tests`` AND it names the
  finding's ``taxonomy_class`` (``taxonomy_class``/``root_cause``) or category.
- ``category_1to1``: or ``prediction.category == finding.expected_category``.
- ``task_any_1to1``: any prediction on the same task.

Only task-scoped findings are scored (the 3 benchmark-wide context entries are
excluded from task denominators, as in the key README). Predictions are
``qa.finding/v1``-shaped dicts; ``pass``/``unknown`` verdicts are not defect
claims and are ignored. No certified-clean task set exists, so precision is a
lower bound: an unmatched prediction may be a real defect PM did not list.

    uv run python research/answer-keys/tb4-audit-v1/score_audit.py --predictions findings.jsonl
    uv run python research/answer-keys/tb4-audit-v1/score_audit.py --benchmark-baselines \\
        --out /path/to/scorer-evaluation.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.analysis_statistics import compute_wilson_interval

HERE = Path(__file__).resolve().parent
KEYS_ROOT = HERE.parent
KEY_ID = "tb4-audit-v1"
SCORE_SCHEMA = "tb4.audit.score/v1"
EVALUATION_SCHEMA = "tb4.audit.scorer_evaluation/v1"
MODES = ("exact_id", "locus_and_class", "category_1to1", "task_any_1to1")
LEGACY = ("legacy_task_broadcast", "legacy_any")
SPLITS = ("all", "dev", "test")
EXPOSURE_STRATA = ("unexposed", "inspected_only", "tuned")
EXPOSURE_CELLS = (*EXPOSURE_STRATA, "untuned")
LABEL_STRENGTHS = ("confirmed", "pm_reported")
AUDITOR_SUPPORT = ("0_github_only", "1", "2", "3")
HEADLINE = ("all", "dev", "test", "untuned_test", "unexposed_test")
NON_CLAIM_VERDICTS = {"pass", "unknown"}
_IDENT = r"[A-Za-z0-9_]"


def task_name(task_id: str) -> str:
    """Bare task name: drop ``@pin`` and any benchmark prefix (as splits/guard.py does)."""
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- key


def exposure_stratum(kinds: Iterable[str]) -> str:
    kinds = set(kinds)
    if "tuned" in kinds:
        return "tuned"
    if "inspected" in kinds:
        return "inspected_only"
    return "unexposed"


def fallback_exposures(
    task_names: Iterable[str], keys_root: Path = KEYS_ROOT
) -> dict[str, dict[str, Any]]:
    """Historical exposures per task from ``splits/exposure.jsonl`` (build.py's rule).

    A record touches a task if it lists it under ``tasks`` or if
    ``splits/guard.py:exposed_items`` (``items``/``items_file``/``where``, over
    every other key) returns an item on it. ``tb4-audit-v1`` itself is excluded.
    """
    spec = importlib.util.spec_from_file_location("_tb4_split_guard", keys_root / "splits/guard.py")
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    keys = {k: v for k, v in guard.load_keys().items() if k != KEY_ID}
    wanted = set(task_names)
    touched: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for record in _jsonl(keys_root / "splits/exposure.jsonl"):
        names = set(record.get("tasks") or [])
        for key_id, ids in guard.exposed_items(record, keys).items():
            names |= {
                task_name(item["subject"]["task_id"])
                for item in keys[key_id]["items"]
                if item["item_id"] in ids
            }
        for name in names & wanted:
            touched[name].append((record["exposure_id"], record["kind"]))
    return {
        name: {
            "historical_exposures": sorted(e for e, _ in touched.get(name, [])),
            "exposure_stratum": exposure_stratum(k for _, k in touched.get(name, [])),
        }
        for name in sorted(wanted)
    }


def _locus_patterns(item: Mapping[str, Any]) -> tuple[re.Pattern[str], ...]:
    """File paths (relative to the task) and symbols of the finding's port tests."""
    task = item.get("task") or ""
    tokens: set[tuple[str, bool]] = set()
    for test in (item.get("port") or {}).get("tests") or []:
        path = test.get("path") or ""
        if task and path.startswith(task + "/"):
            path = path[len(task) + 1 :]
        if path:
            tokens.add((path, False))
        symbol = test.get("symbol")
        if symbol and len(symbol) >= 4:
            tokens.add((symbol, True))
    patterns = []
    for token, is_symbol in sorted(tokens):
        body = re.escape(token)
        if is_symbol:
            body = rf"(?<!{_IDENT}){body}(?!{_IDENT})"
        patterns.append(re.compile(body))
    return tuple(patterns)


@dataclass(frozen=True)
class Finding:
    item_id: str
    task: str
    split: str
    exposure_stratum: str
    label_strength: str
    auditor_support: str
    taxonomy_class: str
    taxonomy_class_id: str | None
    category: str
    locus: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Key:
    findings: tuple[Finding, ...]
    tasks: Mapping[str, Mapping[str, Any]]
    exposure_source: str
    excluded_benchmark_items: int

    def by_task(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = defaultdict(list)
        for finding in self.findings:
            out[finding.task].append(finding)
        return out


def build_key(
    items: list[Mapping[str, Any]],
    tasks: list[Mapping[str, Any]],
    exposures: Mapping[str, Mapping[str, Any]] | None = None,
) -> Key:
    """Index key rows; ``exposures`` fills in tasks whose row lacks ``exposure_stratum``."""
    task_rows: dict[str, dict[str, Any]] = {}
    from_key = all("exposure_stratum" in row for row in tasks)
    for row in tasks:
        merged = dict(row)
        if not from_key:
            if exposures is None or row["task"] not in exposures:
                raise ValueError(f"no exposure stratum for task {row['task']}")
            merged.update(exposures[row["task"]])
        task_rows[row["task"]] = merged
    findings = []
    excluded = 0
    for item in items:
        if item.get("scope", "task") != "task":
            excluded += 1
            continue
        task = item["task"]
        if task not in task_rows:
            raise ValueError(f"{item['item_id']}: task {task} not in tasks.jsonl")
        support = int((item.get("auditor_evidence") or {}).get("counted_source_count") or 0)
        findings.append(
            Finding(
                item_id=item["item_id"],
                task=task,
                split=item["split"],
                exposure_stratum=item.get("exposure_stratum")
                or task_rows[task]["exposure_stratum"],
                label_strength=item["label_strength"],
                auditor_support="0_github_only" if support == 0 else str(min(support, 3)),
                taxonomy_class=item["taxonomy_class"],
                taxonomy_class_id=item.get("taxonomy_class_id"),
                category=item["expected_category"],
                locus=_locus_patterns(item),
            )
        )
    return Key(
        findings=tuple(sorted(findings, key=lambda f: f.item_id)),
        tasks=task_rows,
        exposure_source="key" if from_key else "fallback:splits/exposure.jsonl",
        excluded_benchmark_items=excluded,
    )


def load_key(key_dir: Path = HERE, keys_root: Path = KEYS_ROOT) -> Key:
    items = _jsonl(key_dir / "items.jsonl")
    tasks = _jsonl(key_dir / "tasks.jsonl")
    exposures = None
    if not all("exposure_stratum" in row for row in tasks):
        exposures = fallback_exposures((row["task"] for row in tasks), keys_root)
    return build_key(items, tasks, exposures)


# ------------------------------------------------------------------ predictions


@dataclass(frozen=True)
class Prediction:
    index: int
    label: str
    task: str
    category: str | None
    names: frozenset[str]
    text: str


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v is not None]


def normalize_predictions(raw: Iterable[Mapping[str, Any]]) -> tuple[list[Prediction], Counter]:
    """Return scored defect claims and a tally of everything ignored."""
    preds: list[Prediction] = []
    tally: Counter = Counter()
    for index, row in enumerate(raw):
        tally["total"] += 1
        if row.get("verdict") in NON_CLAIM_VERDICTS:
            tally["ignored_non_fail_verdict"] += 1
            continue
        task_id = row.get("task") or (row.get("subject") or {}).get("task_id")
        if not task_id:
            tally["ignored_no_task"] += 1
            continue
        names = {*_strings(row.get("id")), *_strings(row.get("root_cause"))}
        names |= set(_strings(row.get("taxonomy_class")))
        text = "\n".join([*_strings(row.get("evidence_refs")), *_strings(row.get("summary"))])
        preds.append(
            Prediction(
                index=index,
                label=str(row.get("id") or f"#{index}"),
                task=task_name(str(task_id)),
                category=row.get("category"),
                names=frozenset(names),
                text=text,
            )
        )
    return preds, tally


def load_predictions(path: Path) -> list[dict[str, Any]]:
    """A JSON list, a JSONL file, or a directory of ``*.json``/``*.jsonl`` findings."""
    if path.is_dir():
        rows: list[dict[str, Any]] = []
        for child in sorted(path.rglob("*")):
            if child.suffix in {".json", ".jsonl"} and child.is_file():
                rows.extend(load_predictions(child))
        return rows
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        return data.get("findings") or data.get("predictions") or [data]
    return list(data)


# --------------------------------------------------------------------- matching


def _exact(pred: Prediction, finding: Finding) -> bool:
    return finding.item_id in pred.names


def _class(pred: Prediction, finding: Finding) -> bool:
    return (
        finding.taxonomy_class in pred.names
        or (finding.taxonomy_class_id is not None and finding.taxonomy_class_id in pred.names)
        or pred.category == finding.category
    )


def _locus(pred: Prediction, finding: Finding) -> bool:
    return any(p.search(pred.text) for p in finding.locus)


EDGE: dict[str, Callable[[Prediction, Finding], bool]] = {
    "exact_id": _exact,
    "locus_and_class": lambda p, f: _exact(p, f) or (_locus(p, f) and _class(p, f)),
    "category_1to1": lambda p, f: (
        _exact(p, f) or (_locus(p, f) and _class(p, f)) or p.category == f.category
    ),
    "task_any_1to1": lambda p, f: True,
}


def max_bipartite_matching(
    adjacency: list[list[int]], n_right: int, initial: list[int | None] | None = None
) -> list[int | None]:
    """Kuhn's augmenting-path algorithm; returns ``left -> right`` (or None).

    ``initial`` is a valid matching on a subgraph to extend. Augmenting paths never
    unmatch a vertex, so every vertex matched in ``initial`` stays matched.
    """
    match_right: list[int | None] = [None] * n_right
    for left, right in enumerate(initial or []):
        if right is not None:
            match_right[right] = left

    def augment(left: int, seen: list[bool]) -> bool:
        for right in adjacency[left]:
            if seen[right]:
                continue
            seen[right] = True
            owner = match_right[right]
            if owner is None or augment(owner, seen):
                match_right[right] = left
                return True
        return False

    already = {left for left in match_right if left is not None}
    for left in range(len(adjacency)):
        if left not in already:
            augment(left, [False] * n_right)
    match_left: list[int | None] = [None] * len(adjacency)
    for right, left in enumerate(match_right):
        if left is not None:
            match_left[left] = right
    return match_left


def _by_task(preds: list[Prediction]) -> dict[str, list[Prediction]]:
    out: dict[str, list[Prediction]] = defaultdict(list)
    for pred in preds:
        out[pred.task].append(pred)
    return out


def match_ladder(key: Key, preds: list[Prediction]) -> dict[str, list[tuple[Prediction, Finding]]]:
    """Maximum 1-to-1 matching per task for every mode, stricter modes first.

    Each mode extends the previous mode's matching (its edges are a superset), so the
    findings and predictions credited by a stricter mode stay credited by every looser
    one, in every stratum, not only in total.
    """
    findings_by_task = key.by_task()
    pairs: dict[str, list[tuple[Prediction, Finding]]] = {mode: [] for mode in MODES}
    for task, task_preds in sorted(_by_task(preds).items()):
        findings = findings_by_task.get(task, [])
        if not findings:
            continue
        current: list[int | None] | None = None
        for mode in MODES:
            edge = EDGE[mode]
            adjacency = [[j for j, f in enumerate(findings) if edge(p, f)] for p in task_preds]
            current = max_bipartite_matching(adjacency, len(findings), current)
            pairs[mode].extend(
                (task_preds[i], findings[j]) for i, j in enumerate(current) if j is not None
            )
    return pairs


LEGACY_EDGES = {"legacy_task_broadcast": "category_1to1", "legacy_any": "task_any_1to1"}


def legacy(key: Key, preds: list[Prediction], mode: str) -> tuple[set[str], set[int]]:
    """Task-broadcast credit: (credited item_ids, credited prediction indexes).

    Every finding reachable by any prediction's edge is credited, with no capacity
    limit: ``legacy_any`` credits every finding on a flagged task and
    ``legacy_task_broadcast`` every finding a ``category_1to1`` edge reaches (for
    predictions without ids or loci, every same-category finding on the task).
    """
    edge = EDGE[LEGACY_EDGES[mode]]
    findings_by_task = key.by_task()
    items: set[str] = set()
    hits: set[int] = set()
    for pred in preds:
        reached = [f for f in findings_by_task.get(pred.task, []) if edge(pred, f)]
        if reached:
            hits.add(pred.index)
            items.update(f.item_id for f in reached)
    return items, hits


# ---------------------------------------------------------------------- metrics


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def rate(k: int, n: int) -> dict[str, Any]:
    interval = compute_wilson_interval(k, n)
    return {
        "k": k,
        "n": n,
        "rate": _round(k / n) if n else None,
        "wilson_95": None if interval is None else [_round(interval[0]), _round(interval[1])],
    }


def _f1(precision: dict[str, Any] | None, recall: dict[str, Any]) -> float | None:
    if precision is None or precision["rate"] is None or recall["rate"] is None:
        return None
    p, r = precision["rate"], recall["rate"]
    return _round(2 * p * r / (p + r)) if p + r else 0.0


def _cell(
    findings: list[Finding],
    preds: list[Prediction] | None,
    credited_items: set[str],
    credited_preds: set[int],
) -> dict[str, Any]:
    recall = rate(sum(f.item_id in credited_items for f in findings), len(findings))
    precision = (
        None if preds is None else rate(sum(p.index in credited_preds for p in preds), len(preds))
    )
    return {"recall": recall, "precision": precision, "f1": _f1(precision, recall)}


def _task_cells(key: Key) -> dict[str, Callable[[str, str], bool]]:
    """Task-attribute cells (usable for findings and predictions): name -> f(split, stratum)."""
    cells: dict[str, Callable[[str, str], bool]] = {}
    for split in SPLITS:
        for exposure in ("any", *EXPOSURE_CELLS):
            name = split if exposure == "any" else f"{exposure}/{split}"
            cells[name] = lambda s, e, split=split, exposure=exposure: (
                (split == "all" or s == split)
                and (
                    exposure == "any"
                    or e == exposure
                    or (exposure == "untuned" and e in {"unexposed", "inspected_only"})
                )
            )
    return cells


def _stratify(
    key: Key, preds: list[Prediction], credited_items: set[str], credited_preds: set[int]
) -> dict[str, Any]:
    known = [p for p in preds if p.task in key.tasks]

    def task_cell(split: str, exposure: str) -> dict[str, Any]:
        test = _task_cells(key)[split if exposure == "any" else f"{exposure}/{split}"]
        findings = [f for f in key.findings if test(f.split, f.exposure_stratum)]
        cell_preds = [
            p
            for p in known
            if test(key.tasks[p.task]["split"], key.tasks[p.task]["exposure_stratum"])
        ]
        return _cell(findings, cell_preds, credited_items, credited_preds)

    def item_cell(attr: str, value: str) -> dict[str, Any]:
        findings = [f for f in key.findings if getattr(f, attr) == value]
        return _cell(findings, None, credited_items, credited_preds)

    classes = sorted({f.taxonomy_class for f in key.findings})
    return {
        "headline": {
            "all": task_cell("all", "any"),
            "dev": task_cell("dev", "any"),
            "test": task_cell("test", "any"),
            "untuned_test": task_cell("test", "untuned"),
            "unexposed_test": task_cell("test", "unexposed"),
        },
        "split": {split: task_cell(split, "any") for split in SPLITS},
        "exposure_stratum": {
            exposure: {split: task_cell(split, exposure) for split in SPLITS}
            for exposure in EXPOSURE_CELLS
        },
        "label_strength": {v: item_cell("label_strength", v) for v in LABEL_STRENGTHS},
        "auditor_support": {v: item_cell("auditor_support", v) for v in AUDITOR_SUPPORT},
        "taxonomy_class": {v: item_cell("taxonomy_class", v) for v in classes},
    }


def _inflation(legacy_cell: dict[str, Any], strict_cell: dict[str, Any]) -> float | None:
    """Legacy minus 1-to-1 recall on one cell, from exact counts (same denominator)."""
    legacy_recall, strict_recall = legacy_cell["recall"], strict_cell["recall"]
    if not legacy_recall["n"]:
        return None
    return _round((legacy_recall["k"] - strict_recall["k"]) / legacy_recall["n"])


def score(key: Key, raw_predictions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score predictions under every 1-to-1 mode and both legacy broadcasts."""
    preds, tally = normalize_predictions(raw_predictions)
    on_key = [p for p in preds if p.task in key.tasks]
    tally["benchmark_scope"] = sum(p.task == "terminal-bench-4" for p in preds)
    tally["off_key_task"] = len(preds) - len(on_key) - tally["benchmark_scope"]
    tally["scored"] = len(on_key)
    modes: dict[str, Any] = {}
    matches: dict[str, list[list[str]]] = {}
    for mode, pairs in match_ladder(key, on_key).items():
        matches[mode] = [[p.label, f.item_id] for p, f in pairs]
        modes[mode] = _stratify(
            key, on_key, {f.item_id for _, f in pairs}, {p.index for p, _ in pairs}
        )
    for mode in LEGACY:
        credited_items, credited_preds = legacy(key, on_key, mode)
        modes[mode] = _stratify(key, on_key, credited_items, credited_preds)
    inflation = {
        cell: {
            "category": _inflation(
                modes["legacy_task_broadcast"]["headline"][cell],
                modes["category_1to1"]["headline"][cell],
            ),
            "any": _inflation(
                modes["legacy_any"]["headline"][cell], modes["task_any_1to1"]["headline"][cell]
            ),
        }
        for cell in HEADLINE
    }
    return {
        "schema": SCORE_SCHEMA,
        "key_id": KEY_ID,
        "exposure_source": key.exposure_source,
        "denominators": {
            "task_findings": len(key.findings),
            "excluded_benchmark_scope_findings": key.excluded_benchmark_items,
        },
        "predictions": dict(sorted(tally.items())),
        "modes": modes,
        "task_collision_inflation": inflation,
        "matches": matches,
        "notes": [
            "Modes are a ladder: each accepts every edge of the stricter modes and extends the "
            "stricter matching, so credit is monotone in every stratum.",
            "legacy_any credits every finding on a flagged task; legacy_task_broadcast every "
            "finding a category_1to1 edge reaches (every same-category finding on the task for "
            "predictions without ids or loci). task_collision_inflation = legacy recall minus "
            "the matching 1-to-1 recall (category: legacy_task_broadcast - category_1to1; any: "
            "legacy_any - task_any_1to1), i.e. the credit the 1-to-1 capacity removes.",
            "Precision is a lower bound (no certified-clean tasks). label_strength, "
            "auditor_support and taxonomy_class are finding attributes, so those cells report "
            "recall only.",
        ],
    }


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Compact headline recall/precision table for one score report."""
    return {
        cell: {
            mode: {
                "recall": report["modes"][mode]["headline"][cell]["recall"]["rate"],
                "recall_wilson_95": report["modes"][mode]["headline"][cell]["recall"]["wilson_95"],
                "precision": report["modes"][mode]["headline"][cell]["precision"]["rate"],
                "matched": report["modes"][mode]["headline"][cell]["recall"]["k"],
                "of": report["modes"][mode]["headline"][cell]["recall"]["n"],
            }
            for mode in (*MODES, *LEGACY)
        }
        | {"task_collision_inflation": report["task_collision_inflation"][cell]}
        for cell in HEADLINE
    }


# -------------------------------------------------------------------- baselines


def _raw_items(key_dir: Path) -> list[dict[str, Any]]:
    return [row for row in _jsonl(key_dir / "items.jsonl") if row.get("scope") == "task"]


def _pred(task: str, category: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "schema": "qa.finding/v1",
        "subject": {"kind": "task", "task_id": f"terminal-bench/{task}"},
        "verdict": "fail",
        "category": category,
        **extra,
    }


def _majority(values: list[str]) -> str | None:
    if not values:
        return None
    counts = Counter(values)
    return sorted(counts, key=lambda v: (-counts[v], v))[0]


def baseline_predictions(key_dir: Path = HERE) -> dict[str, dict[str, Any]]:
    items = _raw_items(key_dir)
    tasks = _jsonl(key_dir / "tasks.jsonl")
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_task[item["task"]].append(item)

    generic = [_pred(task, "weak", id=f"generic-weak:{task}") for task in sorted(by_task)]
    oracle = [
        _pred(task, category, id=f"category-oracle:{task}:{category}")
        for task in sorted(by_task)
        for category in sorted({i["expected_category"] for i in by_task[task]})
    ]

    def projection(auditor: str, join: str) -> list[dict[str, Any]]:
        """One prediction per auditor flag/slug, naming the findings the key joins to it."""
        unit_field = _AUDITOR_UNITS[auditor][0]
        units: dict[tuple[str, str], list[dict[str, Any]]] = {
            (task["task"], unit): [] for task in tasks for unit in task.get(unit_field) or []
        }
        for item in items:
            if _auditor_hit(item, auditor, join):
                for unit in _auditor_units(item, auditor):
                    units.setdefault((item["task"], unit), []).append(item)
        return [
            _pred(
                task,
                _majority([i["expected_category"] for i in found]),
                id=f"{auditor}:{unit}",
                root_cause=sorted(i["item_id"] for i in found),
            )
            for (task, unit), found in sorted(units.items())
        ]

    witnesses = [
        _pred(
            item["task"],
            item["expected_category"],
            id=f"witness:{item['witness']['witness_id']}",
            summary=f"executable witness {item['witness']['witness_id']}",
            evidence_refs=[item["witness"]["port_test"], item["witness"]["solution_dir"]],
        )
        for item in items
        if item["label_strength"] == "confirmed" and item.get("witness")
    ]
    return {
        "one_generic_weak_per_flagged_task": {
            "description": "One generic `weak` prediction per task with >=1 task-scoped PM "
            "finding; no ids, loci or classes. Shows task-broadcast recall inflation.",
            "predictions": generic,
        },
        "one_per_task_category_oracle": {
            "description": "One prediction per (task, distinct expected_category) pair, with "
            "oracle knowledge of the categories present; no ids or loci.",
            "predictions": oracle,
        },
        "epoch_auditor_projection": {
            "description": "Each Epoch flag on the cohort as one prediction; root_cause names "
            "the PM findings the key counts as Epoch-matched (26 findings), category is their "
            "majority category (null for unmatched flags).",
            "predictions": projection("epoch", "counted"),
        },
        "tokenless_auditor_projection": {
            "description": "Each Tokenless slug on the cohort as one prediction; root_cause names "
            "the PM findings the key counts as Tokenless-matched (24 counted findings).",
            "predictions": projection("tokenless", "counted"),
        },
        "tokenless_auditor_projection_direct": {
            "description": "As tokenless_auditor_projection, but root_cause names only the 9 "
            "direct (source-identity) Tokenless findings.",
            "predictions": projection("tokenless", "direct"),
        },
        "confirmed_dev_witnesses": {
            "description": "The 10 confirmed dev-split executable witnesses, each citing its "
            "port test locus and category but not the finding id.",
            "predictions": witnesses,
        },
    }


def _chapman(n1: int, n2: int, m: int) -> dict[str, Any]:
    n_hat = (n1 + 1) * (n2 + 1) / (m + 1) - 1
    observed = n1 + n2 - m
    return {
        "n_first": n1,
        "n_second": n2,
        "overlap": m,
        "observed": observed,
        "lincoln_petersen": _round(n1 * n2 / m) if m else None,
        "chapman": _round(n_hat),
        "undetected_chapman": _round(n_hat - observed),
    }


def _chao(detections: list[int]) -> dict[str, Any]:
    freq = Counter(detections)
    s_obs, f1, f2 = len(detections), freq[1], freq[2]
    extra = f1 * f1 / (2 * f2) if f2 else f1 * (f1 - 1) / 2
    return {
        "observed_units": s_obs,
        "f1": f1,
        "f2": f2,
        "f3": freq[3],
        "chao1_lower_bound": _round(s_obs + extra),
        "undetected_lower_bound": _round(extra),
    }


_STRATA: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "all_reviewed": lambda t: True,
    "dev_reviewed": lambda t: t["split"] == "dev",
    "test_reviewed": lambda t: t["split"] == "test",
    "untuned_test_reviewed": lambda t: (
        t["split"] == "test" and t["exposure_stratum"] in {"unexposed", "inspected_only"}
    ),
    "unexposed_test_reviewed": lambda t: (
        t["split"] == "test" and t["exposure_stratum"] == "unexposed"
    ),
}
# Auditor -> (task-row unit field, finding join fields in priority order).
_AUDITOR_UNITS = {
    "epoch": ("epoch_flag_ids", ("shared_issue_flag_ids", "task_flag_ids")),
    "tokenless": ("tokenless_slugs", ("matched_slugs", "task_slugs")),
}


def _corroborated(finding: Mapping[str, Any]) -> bool:
    return finding["auditor_evidence"]["counted_source_count"] >= 2


def _executed(finding: Mapping[str, Any]) -> bool:
    return finding["pm_execution"]["execution_claim"] == "true"


def _confirmed(finding: Mapping[str, Any]) -> bool:
    return finding["label_strength"] == "confirmed"


def _auditor_hit(finding: Mapping[str, Any], auditor: str, join: str) -> bool:
    return bool(finding["auditor_evidence"]["auditors"][auditor][join])


def _auditor_units(finding: Mapping[str, Any], auditor: str) -> list[str]:
    """Flags/slugs a finding is joined to (its matched ones, else every one on its task)."""
    audit = finding["auditor_evidence"]["auditors"][auditor]
    for field in _AUDITOR_UNITS[auditor][1]:
        if audit.get(field):
            return list(audit[field])
    return []


def _capture_recapture(
    tasks: Mapping[str, Mapping[str, Any]], findings: list[dict[str, Any]], join: str
) -> dict[str, Any]:
    """Two-source Chapman and three-source Chao estimates over one stratum.

    ``join`` is ``direct`` (source-identity links) or ``counted`` (also PM attribution).
    """
    matched: dict[str, int] = {}
    unmatched: dict[str, int] = {}
    for auditor, (unit_field, _) in _AUDITOR_UNITS.items():
        joined: dict[str, set[str]] = defaultdict(set)
        for finding in findings:
            if _auditor_hit(finding, auditor, join):
                joined[finding["task"]].update(_auditor_units(finding, auditor))
        matched[auditor] = sum(_auditor_hit(f, auditor, join) for f in findings)
        unmatched[auditor] = sum(
            len(set(row.get(unit_field) or []) - joined[name]) for name, row in tasks.items()
        )
    detections = [
        1 + sum(_auditor_hit(f, auditor, join) for auditor in _AUDITOR_UNITS) for f in findings
    ]
    detections += [1] * sum(unmatched.values())
    n_pm = len(findings)
    return {
        "pm_x_epoch": _chapman(n_pm, matched["epoch"] + unmatched["epoch"], matched["epoch"]),
        "pm_x_tokenless": _chapman(
            n_pm, matched["tokenless"] + unmatched["tokenless"], matched["tokenless"]
        ),
        "chao_pm_epoch_tokenless": _chao(detections),
        "epoch_unmatched_flags": unmatched["epoch"],
        "tokenless_unmatched_slugs": unmatched["tokenless"],
    }


def _prevalence(
    tasks: Mapping[str, Mapping[str, Any]], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        by_task[finding["task"]].append(finding)

    def tasks_with(test: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
        return rate(sum(any(map(test, by_task[name])) for name in tasks), len(tasks))

    def share(test: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
        return rate(sum(map(test, findings)), len(findings))

    return {
        "reviewed_tasks": len(tasks),
        "task_level": {
            "any_pm_finding": rate(sum(bool(by_task[name]) for name in tasks), len(tasks)),
            "any_reported_defect_any_source": rate(
                sum(row["task_label_status"] == "reported_defect" for row in tasks.values()),
                len(tasks),
            ),
            "corroborated_by_ge2_auditors": tasks_with(_corroborated),
            "pm_execution_claimed": tasks_with(_executed),
            "confirmed_witness": tasks_with(_confirmed),
        },
        "finding_level": {
            "findings": len(findings),
            "findings_per_reviewed_task": _round(len(findings) / len(tasks)) if tasks else None,
            "share_corroborated_by_ge2_auditors": share(_corroborated),
            "share_pm_execution_claimed": share(_executed),
            "share_confirmed_witness": share(_confirmed),
        },
        "capture_recapture": {
            join: _capture_recapture(tasks, findings, join) for join in ("direct", "counted")
        },
    }


def prevalence_by_stratum(key: Key, key_dir: Path = HERE) -> dict[str, Any]:
    items = {i["item_id"]: i for i in _raw_items(key_dir)}
    reviewed = {
        name: row
        for name, row in key.tasks.items()
        if (row.get("pm_review_status") or row.get("pm_none_text")) != "Not reviewed yet."
    }
    strata = {}
    for name, keep in _STRATA.items():
        tasks = {t: row for t, row in reviewed.items() if keep(row)}
        findings = [items[f.item_id] for f in key.findings if f.task in tasks]
        strata[name] = _prevalence(tasks, findings)
    return {
        "strata": strata,
        "notes": [
            "Reviewed = PM did not mark the task 'Not reviewed yet.' (65 of 66 tasks: 24 dev, "
            "41 test). 'No issues found' is not a certified-clean label, so prevalence is of "
            "reported defects among reviewed tasks.",
            "Capture-recapture units: PM task-scoped findings plus Epoch flags / Tokenless slugs "
            "the key joins to no PM finding (direct = source-identity links only; counted = also "
            "undisputed PM attribution). Sources are not independent (PM attributes Epoch and "
            "Tokenless; Tokenless cites Epoch), so these are rough lower bounds on undetected "
            "findings, not unbiased estimates.",
            "PM detects every key unit by construction, so the three-source Chao (M_h) bound is "
            "dominated by PM-only singletons and is not a credible size estimate; it is reported "
            "for completeness. The two-source Chapman figures (conditional on PM's catalogue) "
            "are the usable undetected-finding indications.",
        ],
    }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def benchmark_baselines(key_dir: Path = HERE, keys_root: Path = KEYS_ROOT) -> dict[str, Any]:
    key = load_key(key_dir, keys_root)
    baselines: dict[str, Any] = {}
    for name, spec in baseline_predictions(key_dir).items():
        report = score(key, spec["predictions"])
        by_split = Counter(
            key.tasks[task_name(p["subject"]["task_id"])]["split"] for p in spec["predictions"]
        )
        baselines[name] = {
            "description": spec["description"],
            "n_predictions": len(spec["predictions"]),
            "predictions_by_split": dict(sorted(by_split.items())),
            "summary": summarize(report),
            "score": report,
        }
    stratum_counts = Counter((row["split"], row["exposure_stratum"]) for row in key.tasks.values())
    finding_counts = Counter((f.split, f.exposure_stratum) for f in key.findings)
    exposure_check: dict[str, Any] = {"source": key.exposure_source}
    if key.exposure_source == "key":
        fallback = fallback_exposures(key.tasks, keys_root)
        exposure_check["fallback_disagreements"] = sorted(
            name
            for name, row in key.tasks.items()
            if row["exposure_stratum"] != fallback[name]["exposure_stratum"]
        )
    return {
        "schema": EVALUATION_SCHEMA,
        "key_id": KEY_ID,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paid_cost_usd": 0.0,
        "inputs": {name: _sha256(key_dir / name) for name in ("items.jsonl", "tasks.jsonl")}
        | {"splits/exposure.jsonl": _sha256(keys_root / "splits/exposure.jsonl")},
        "exposure": exposure_check
        | {
            "tasks": {
                f"{split}/{stratum}": stratum_counts[(split, stratum)]
                for split in ("dev", "test")
                for stratum in EXPOSURE_STRATA
            },
            "task_findings": {
                f"{split}/{stratum}": finding_counts[(split, stratum)]
                for split in ("dev", "test")
                for stratum in EXPOSURE_STRATA
            },
        },
        "modes": list(MODES),
        "legacy": list(LEGACY),
        "baselines": baselines,
        "prevalence_by_stratum": prevalence_by_stratum(key, key_dir),
    }


# -------------------------------------------------------------------------- CLI


def _write(payload: dict[str, Any], out: Path | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if out is None:
        sys.stdout.write(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--predictions", type=Path, help="JSON, JSONL or directory of findings")
    group.add_argument(
        "--benchmark-baselines", action="store_true", help="score the registered baselines"
    )
    parser.add_argument("--key-dir", type=Path, default=HERE)
    parser.add_argument("--keys-root", type=Path, default=KEYS_ROOT)
    parser.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    if args.benchmark_baselines:
        payload = benchmark_baselines(args.key_dir, args.keys_root)
        _write(payload, args.out)
        if args.out is not None:
            generic = payload["baselines"]["one_generic_weak_per_flagged_task"]["summary"]
            print(
                json.dumps(
                    {
                        "out": str(args.out),
                        "baselines": sorted(payload["baselines"]),
                        "generic_weak_recall_all": {
                            mode: generic["all"][mode]["recall"] for mode in (*MODES, *LEGACY)
                        },
                    },
                    sort_keys=True,
                )
            )
        return 0
    key = load_key(args.key_dir, args.keys_root)
    _write(score(key, load_predictions(args.predictions)), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
