"""Task QA gate (OQ-6) task-level findings for DEV issue-bank task items.

No model is called. The OQ-6 corrected-gate rerun already made one audit call per
task × auditor route × arm, parsed it once, applied both gates to that parsed
response and scored the union once with the fixed scorer; each final
``cell.json`` keeps the parsed findings (``prediction.parsed_payloads``) and the
frozen ``paired.evaluate_cell`` row (``paired``: gate memberships and the
one-to-one key matching). This module only joins those stored rows to issue-bank
items, and reproduces the keyed recall with the frozen ``paired.aggregate_cells``
and ``result_summary.shared_complete_rows`` helpers (loaded from the freeze's
immutable snapshots, digest-checked).

Items: every DEV task item from ``cloud_task_issues`` and ``answer_keys``, and
every DEV ``library_findings`` item on an OQ-6 task. One record per item × route
× arm × gate (``old_0_8``, ``new_verified_0_5``), detector ``taskqa_gate``.

Verdict rules, per gate:

* keyed item -- its key is one of the case's OQ-6 scorer keys (answer-key
  ``oq6-keyed`` items through ``provenance.ref``; library findings whose
  ``Finding.locator`` is the key id): ``alarm`` iff the stored one-to-one maximum
  matching assigns one of the gate's eligible findings to that key, else
  ``clear``. This is exactly OQ-6's recall rule.
* other item on an audited task: ``alarm`` iff the gate admitted a finding on the
  same task version whose direction is compatible with the item kind
  (``wrong_pass`` -> ``grader_wrong_pass``, ``unfair_fail`` ->
  ``grader_unfair_fail``, either -> ``task_defect``), else ``clear``. Same task is
  the normalized task name (``CorpusRegistry.task_name``) plus, when the item
  pins a task version digest, that digest equal to the audited package.
* ``abstain``: the audit was sent but the cell has no complete parse and union
  judgment (OQ-6 counts the whole cell missing).
* ``unknown``: the task is not in the OQ-6 cohort, the item pins another task
  version, the input was unavailable, or the route × arm never sent the audit
  (operator-paused or quota-pending).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.production.corpus_queue import CorpusRegistry

DETECTOR = "taskqa_gate"
GATES = ("old_0_8", "new_verified_0_5")
ROUTES = ("anthropic/claude-sonnet-5-5", "kimi-code/kimi-for-coding")
ARMS = ("A", "B")
TASK_KINDS = frozenset({"grader_wrong_pass", "grader_unfair_fail", "task_defect"})
DIRECTIONS: dict[str, frozenset[str]] = {
    "grader_wrong_pass": frozenset({"wrong_pass"}),
    "grader_unfair_fail": frozenset({"unfair_fail"}),
    "task_defect": frozenset({"wrong_pass", "unfair_fail"}),
}
OQ6_NOTE = "oq6-keyed"
_RESULTS = Path.home() / "Developer/eval-lab-results"
V4_ROOT = _RESULTS / "2026-10-27/oq6-tq-gate-rerun-v4"
V5_ROOT = _RESULTS / "2026-10-28/oq6-tq-gate-rerun-v5"
DEFAULT_PLAN = V4_ROOT / "plan.json"
DEFAULT_FREEZE = V4_ROOT / "freeze.json"
DEFAULT_CALL_ROOTS = (V4_ROOT / "calls", V5_ROOT / "dev/calls", V5_ROOT / "cross/calls")
_FW2 = Path.home() / ".local/state/evallab-prod-live/data-flywheel/fw2"
DEFAULT_OUT = _FW2 / "task-findings-oq6.jsonl"
DEFAULT_SUMMARY = _FW2 / "task-findings-oq6-summary.json"
EVIDENCE_WORDS = 40


def task_name(task_id: str) -> str:
    return CorpusRegistry.task_name(task_id)


@dataclass(frozen=True)
class Case:
    """One planned OQ-6 task: its package version and scorer key ids (never key text)."""

    case_id: str
    stage: str
    task: str  # normalized task name
    package_digest: str | None
    key_ids: frozenset[str]
    key_count: int
    preparation_status: str


@dataclass(frozen=True)
class CellView:
    """The stored scientific output of one route × arm audit of one case."""

    route: str
    arm: str
    case_id: str
    path: Path
    paired: Mapping[str, Any]
    payloads: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class TaskItem:
    item_id: str
    source: str
    kind: str
    polarity: str
    task_id: str
    task: str  # normalized task name
    digest: str | None = None  # task version digest, when the item pins one
    key_id: str | None = None  # OQ-6 scorer key id, for keyed items


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_digest(item_id: str, keys: Sequence[Mapping[str, Any]]) -> str | None:
    if "@" in item_id:
        return item_id.split("@", 1)[1]
    digests = {key["package_digest"] for key in keys if key.get("package_digest")}
    if len(digests) > 1:
        raise ValueError(f"{item_id}: keys bind several package digests")
    return digests.pop() if digests else None


def load_cases(plan_path: Path) -> list[Case]:
    """Plan cases; keys files are read for key ids and package digests only."""
    cases: list[Case] = []
    seen: set[str] = set()
    for row in json.loads(plan_path.read_bytes())["cases"]:
        keys: list[Mapping[str, Any]] = []
        if row["key_count"]:
            keys_path = Path(row["keys_path"])
            if _sha(keys_path) != row["keys_sha256"]:
                raise ValueError(f"{row['case_id']}: scorer keys drifted from the plan")
            keys = json.loads(keys_path.read_bytes())
        if len(keys) != row["key_count"]:
            raise ValueError(f"{row['case_id']}: key count differs from the plan")
        name = task_name(row["item_id"])
        if name in seen:
            raise ValueError(f"task name {name} names two OQ-6 cases")
        seen.add(name)
        cases.append(
            Case(
                case_id=row["case_id"],
                stage=row["stage"],
                task=name,
                package_digest=_package_digest(row["item_id"], keys),
                key_ids=frozenset(key["key_id"] for key in keys),
                key_count=row["key_count"],
                preparation_status=row["preparation_status"],
            )
        )
    return cases


def _route_dir(route: str) -> str:
    return route.replace("/", "--")


def load_cells(
    cases: Sequence[Case], call_roots: Sequence[Path], routes: Sequence[str] = ROUTES
) -> dict[tuple[str, str, str], CellView]:
    """Final cells across the v4 and v5 roots; a case may finalize in one root only."""
    by_case = {case.case_id: case for case in cases}
    cells: dict[tuple[str, str, str], CellView] = {}
    for root in call_roots:
        for route in routes:
            for arm in ARMS:
                for case in cases:
                    path = root / _route_dir(route) / arm / case.stage / case.case_id / "cell.json"
                    if not path.is_file():
                        continue
                    record = json.loads(path.read_bytes())
                    key = (route, arm, case.case_id)
                    if key in cells:
                        raise ValueError(f"{key}: final cell in two roots")
                    if (record["route"], record["arm"], record["case_id"]) != key:
                        raise ValueError(f"{path}: cell identity differs from its directory")
                    if task_name(record["task_id"]) != by_case[case.case_id].task:
                        raise ValueError(f"{path}: cell task differs from the plan")
                    paired = record["paired"]
                    if paired["key_count"] != case.key_count:
                        raise ValueError(f"{path}: cell key denominator differs from the plan")
                    prediction = record.get("prediction") or {}
                    cells[key] = CellView(
                        route=route,
                        arm=arm,
                        case_id=case.case_id,
                        path=path,
                        paired=paired,
                        payloads=prediction.get("parsed_payloads") or [],
                    )
    return cells


def _evidence(text: str) -> str:
    words = text.split()
    return " ".join(words[:EVIDENCE_WORDS])


def _candidate(cell: CellView, candidate_id: str) -> Mapping[str, Any]:
    return cell.payloads[int(candidate_id[1:]) - 1]


def _record(
    item: TaskItem,
    route: str,
    arm: str,
    gate: str,
    verdict: str,
    evidence: str,
    source_ref: str,
    score: float | None = None,
) -> dict[str, Any]:
    return {
        "task_id": item.task_id,
        "item_ids": [item.item_id],
        "detector": DETECTOR,
        "detector_version": f"oq6.{gate}",
        "reader_model": route,
        "arm": arm,
        "verdict": verdict,
        "kind": item.kind if item.kind in TASK_KINDS else "other",
        "score": score,
        "evidence": _evidence(evidence),
        "source_ref": source_ref,
    }


def _judge(item: TaskItem, cell: CellView, gate: str) -> tuple[str, float | None, str, str]:
    """(verdict, score, evidence, candidate) for a complete cell."""
    eligible = list(cell.paired["memberships"][gate])
    if item.key_id is not None:
        matching = cell.paired["gates"][gate]["matching"]
        hits = [candidate for candidate, key in matching.items() if key == item.key_id]
        if hits:
            payload = _candidate(cell, hits[0])
            return (
                "alarm",
                float(payload["confidence"]),
                (
                    f"eligible {payload['direction']} finding {hits[0]} "
                    f"(confidence {payload['confidence']:.2f}) is the fixed scorer's "
                    "one-to-one match for this key"
                ),
                hits[0],
            )
        return (
            "clear",
            None,
            f"{len(eligible)} eligible findings; none one-to-one matched to this key",
            "",
        )
    directions = DIRECTIONS.get(item.kind, frozenset())
    hits = [c for c in eligible if _candidate(cell, c)["direction"] in directions]
    if hits:
        best = max(hits, key=lambda c: _candidate(cell, c)["confidence"])
        payload = _candidate(cell, best)
        return (
            "alarm",
            float(payload["confidence"]),
            (
                f"eligible {payload['direction']} finding {best} "
                f"(confidence {payload['confidence']:.2f}) on the same task version; "
                f"{len(hits)} direction-compatible of {len(eligible)} eligible"
            ),
            best,
        )
    return (
        "clear",
        None,
        f"{len(eligible)} eligible findings; none direction-compatible with {item.kind}",
        "",
    )


def task_findings(
    items: Sequence[TaskItem],
    cases: Sequence[Case],
    cells: Mapping[tuple[str, str, str], CellView],
    *,
    routes: Sequence[str] = ROUTES,
    plan_ref: str = "oq6-plan",
) -> list[dict[str, Any]]:
    by_task = {case.task: case for case in cases}
    records: list[dict[str, Any]] = []
    for item in items:
        case = by_task.get(item.task)
        for route in routes:
            for arm in ARMS:
                for gate in GATES:
                    records.append(_one(item, case, cells, route, arm, gate, plan_ref))
    return records


def _one(
    item: TaskItem,
    case: Case | None,
    cells: Mapping[tuple[str, str, str], CellView],
    route: str,
    arm: str,
    gate: str,
    plan_ref: str,
) -> dict[str, Any]:
    def unknown(reason: str, ref: str) -> dict[str, Any]:
        return _record(item, route, arm, gate, "unknown", reason, ref)

    if case is None:
        return unknown("task is not in the OQ-6 cohort", plan_ref)
    ref = f"{plan_ref}#{case.case_id}"
    if item.digest is not None and case.package_digest not in (None, item.digest):
        return unknown("item pins another task version than the audited package", ref)
    if item.key_id is not None and item.key_id not in case.key_ids:
        raise ValueError(f"{item.item_id}: key {item.key_id} is not a key of {case.case_id}")
    if case.preparation_status != "prepared":
        return unknown("OQ-6 task source unavailable; audit not sent", ref)
    cell = cells.get((route, arm, case.case_id))
    if cell is None:
        return unknown("audit not sent for this route and arm (paused or quota-pending)", ref)
    cell_ref = f"{cell.path}#{gate}"
    if cell.paired["audit_status"] == "unavailable_input":
        return unknown("OQ-6 task source unavailable; audit not sent", cell_ref)
    if not cell.paired["complete"]:
        reason = cell.paired.get("missing_reason") or "incomplete"
        return _record(
            item, route, arm, gate, "abstain", f"audit sent; cell incomplete ({reason})", cell_ref
        )
    verdict, score, evidence, candidate = _judge(item, cell, gate)
    if candidate:
        cell_ref = f"{cell_ref}:{candidate}"
    return _record(item, route, arm, gate, verdict, evidence, cell_ref, score)


# --- issue-bank items -------------------------------------------------------


def _from_issue(
    item: IssueItem, *, digest: str | None = None, key_id: str | None = None
) -> TaskItem:
    return TaskItem(
        item_id=item.item_id,
        source=item.source,
        kind=item.kind,
        polarity=item.polarity,
        task_id=item.task_id or item.subject_id,
        task=task_name(item.task_id or item.subject_id),
        digest=digest,
        key_id=key_id,
    )


def select_items(
    cloud_task_issues: Iterable[Any],
    answer_keys: Iterable[Any],
    library_findings: Iterable[Any],
    findings: Mapping[str, Any],
    cases: Sequence[Case],
) -> list[TaskItem]:
    """DEV task items; ``findings`` maps a library finding id to its ``Finding``."""
    tasks = {case.task for case in cases}
    keys = {key for case in cases for key in case.key_ids}
    selected: list[TaskItem] = []

    def dev_tasks(rows: Iterable[Any]) -> Iterable[IssueItem]:
        for row in rows:
            if isinstance(row, IssueItem) and row.split == "DEV" and row.subject_type == "task":
                yield row

    for item in dev_tasks(cloud_task_issues):
        selected.append(_from_issue(item))
    for item in dev_tasks(answer_keys):
        keyed = (item.provenance.note or "").startswith(OQ6_NOTE)
        if keyed and item.provenance.ref not in keys:
            raise ValueError(f"{item.item_id}: oq6-keyed ref {item.provenance.ref} is no OQ-6 key")
        selected.append(_from_issue(item, key_id=item.provenance.ref if keyed else None))
    for item in dev_tasks(library_findings):
        finding = findings[item.locator]
        if task_name(finding.task.task_id) not in tasks:
            continue
        selected.append(
            _from_issue(
                item,
                digest=finding.task.task_version_digest,
                key_id=finding.locator if finding.locator in keys else None,
            )
        )
    return selected


def bank_items(repo_root: Path, cases: Sequence[Case]) -> list[TaskItem]:
    from evallab.failure_library.bank.sources import (
        answer_keys,
        cloud_task_issues,
        library_findings,
    )
    from evallab.failure_library.store import LibraryStore

    ctx = BankContext(repo_root=repo_root)
    store = LibraryStore.for_repo(ctx.repo_root, state_root=ctx.state_root)
    findings = {
        finding.finding_id: finding
        for collection in store.collections("findings")
        for finding in store.iter_findings(collection)
    }
    return select_items(
        cloud_task_issues.build(ctx),
        answer_keys.build(ctx),
        library_findings.build(ctx),
        findings,
        cases,
    )


# --- reporting --------------------------------------------------------------


def _arm_label(route: str, arm: str, gate: str) -> str:
    return f"{route}#{arm}@{gate}"


def summarize(
    records: Sequence[Mapping[str, Any]],
    items: Sequence[TaskItem],
    cases: Sequence[Case],
    cells: Mapping[tuple[str, str, str], CellView],
) -> dict[str, Any]:
    """Per route × arm × gate: coverage, verdict mix, recall on positives, negative alarms."""
    by_id = {item.item_id: item for item in items}
    stages = {case.case_id: case.stage for case in cases}
    groups: dict[str, dict[str, Any]] = {}
    tally: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for record in records:
        item = by_id[record["item_ids"][0]]
        label = _arm_label(record["reader_model"], record["arm"], record["detector_version"][4:])
        tally[label][item.polarity][record["verdict"]] += 1
        tally[label][f"{item.source}:{item.polarity}"][record["verdict"]] += 1
        if item.key_id is not None:
            tally[label]["keyed"][record["verdict"]] += 1
    for route in ROUTES:
        for arm in ARMS:
            ran = [c for (r, a, _), c in cells.items() if r == route and a == arm]
            complete = Counter(stages[c.case_id] for c in ran if c.paired["complete"])
            for gate in GATES:
                label = _arm_label(route, arm, gate)
                counts = tally[label]
                positive, negative = counts["positive"], counts["negative"]
                judged_pos = positive["alarm"] + positive["clear"]
                judged_neg = negative["alarm"] + negative["clear"]
                groups[label] = {
                    "tasks_complete": dict(complete),
                    "tasks_sent_incomplete": sum(
                        not c.paired["complete"] and c.paired["audit_status"] != "unavailable_input"
                        for c in ran
                    ),
                    "items": {name: dict(counter) for name, counter in sorted(counts.items())},
                    "recall_on_judged_positives": (
                        round(positive["alarm"] / judged_pos, 4) if judged_pos else None
                    ),
                    "positives_alarmed": positive["alarm"],
                    "positives_judged": judged_pos,
                    "negative_alarms": negative["alarm"],
                    "negatives_judged": judged_neg,
                }
    return groups


def keyed_reproduction(
    records: Sequence[Mapping[str, Any]],
    items: Sequence[TaskItem],
    cases: Sequence[Case],
    cells: Mapping[tuple[str, str, str], CellView],
    *,
    aggregate: Callable[[list[Mapping[str, Any]]], Mapping[str, Any]],
    shared: Callable[..., tuple[list[Any], list[Any], list[str]]],
    stage: str = "dev",
) -> dict[str, Any]:
    """OQ-6's same-route A/B shared-complete keyed recall, two ways.

    ``frozen`` applies the frozen ``aggregate_cells`` to the stored rows of the
    shared complete keyed cells; ``items`` counts distinct key ids alarmed by
    this module's keyed item records on the same cells. They must agree.
    """
    keyed_cases = [c for c in cases if c.stage == stage and c.key_count]
    by_id = {item.item_id: item for item in items}
    result: dict[str, Any] = {}
    for route in ROUTES:
        rows = {
            arm: [
                dict(cells[(route, arm, c.case_id)].paired)
                for c in keyed_cases
                if (route, arm, c.case_id) in cells
            ]
            for arm in ARMS
        }
        left, right, shared_ids = shared(rows["A"], rows["B"])
        entry: dict[str, Any] = {
            "shared_complete_keyed_tasks": len(shared_ids),
            "shared_evaluable_keys": sum(row["key_count"] for row in left),
        }
        if not shared_ids:
            result[route] = {**entry, "status": "unavailable: empty shared A/B intersection"}
            continue
        shared_set = set(shared_ids)
        key_case = {key: c.case_id for c in keyed_cases for key in c.key_ids}
        for arm, arm_rows in (("A", left), ("B", right)):
            frozen = aggregate(arm_rows)
            for gate in GATES:
                alarmed = {
                    by_id[r["item_ids"][0]].key_id
                    for r in records
                    if r["reader_model"] == route
                    and r["arm"] == arm
                    and r["detector_version"] == f"oq6.{gate}"
                    and r["verdict"] == "alarm"
                    and by_id[r["item_ids"][0]].key_id is not None
                    and key_case.get(by_id[r["item_ids"][0]].key_id) in shared_set
                }
                matched = frozen["gates"][gate]["matched_keys"]
                entry[f"{arm}@{gate}"] = {
                    "frozen_matched_keys": matched,
                    "frozen_evaluable_keys": frozen["gates"][gate]["evaluable_keys"],
                    "frozen_eligible_findings": frozen["gates"][gate]["eligible_findings"],
                    "item_records_alarmed_keys": len(alarmed),
                    "agree": matched == len(alarmed),
                }
        result[route] = entry
    return result


def _frozen_module(freeze_path: Path, suffix: str, name: str) -> ModuleType:
    """Load one helper from the freeze's immutable snapshot after checking its digest."""
    freeze = json.loads(freeze_path.read_bytes())
    rows = [row for row in freeze["code_files"] if row["path"].endswith("/" + suffix)]
    if len(rows) != 1:
        raise ValueError(f"freeze pins {len(rows)} copies of {suffix}")
    snapshot = Path(rows[0]["snapshot_path"])
    if _sha(snapshot) != rows[0]["sha256"]:
        raise ValueError(f"frozen helper drift: {snapshot}")
    spec = importlib.util.spec_from_file_location(name, snapshot)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {snapshot}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    tmp.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Task QA gate (OQ-6) task-level findings for DEV issue-bank task items."
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--call-root", type=Path, action="append", default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args(argv)
    call_roots = args.call_root or list(DEFAULT_CALL_ROOTS)
    cases = load_cases(args.plan)
    cells = load_cells(cases, call_roots)
    items = bank_items(args.repo_root, cases)
    records = task_findings(items, cases, cells, plan_ref=str(args.plan))
    write_jsonl(args.out, records)
    paired = _frozen_module(args.freeze, "paired.py", "oq6_frozen_paired")
    result_summary = _frozen_module(args.freeze, "result_summary.py", "oq6_frozen_result_summary")
    reproduction = {
        stage: keyed_reproduction(
            records,
            items,
            cases,
            cells,
            aggregate=paired.aggregate_cells,
            shared=result_summary.shared_complete_rows,
            stage=stage,
        )
        for stage in ("dev", "cross")
    }
    summary = {
        "schema": "fw2.taskqa-findings-summary/v1",
        "findings": str(args.out),
        "records": len(records),
        "items": dict(Counter(item.source for item in items)),
        "keyed_items": sum(item.key_id is not None for item in items),
        "cells": len(cells),
        "call_roots": [str(root) for root in call_roots],
        "plan_sha256": _sha(args.plan),
        "arms": summarize(records, items, cases, cells),
        "keyed_reproduction": reproduction,
        "model_calls": 0,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    json.dump(
        {"records": len(records), "summary": str(args.summary), "keyed_reproduction": reproduction},
        sys.stdout,
        indent=1,
        sort_keys=True,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
