"""Materialize defensive Task QA hard cases from frozen, already-scored OQ-6 audits.

Only an exact scorer-key binding can establish a keyed miss. Same-task/direction
alarms are deliberately not consumed. Counts are audit-key observations; exported
misses are grouped by task package and key, not counted once per bank source.
The sole target input is OQ-6's frozen bounded source view, never a prompt with
retrieval, a key file, an auditor answer, or a canonical oracle solution.

No models, graders, containers, source builders, or production-store writes run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evallab.failure_library.bank import BankContext, IssueBank, IssueItem
from evallab.failure_library.bank import taskqa_findings as tq
from evallab.failure_library.bank.hard_cases import HardCase, write_cases
from evallab.failure_library.bank.sources.grader_repairs import ts14_summary_path
from evallab.failure_library.records import Finding
from evallab.failure_library.store import LibraryStore

DEFAULT_OUT = (
    Path.home() / ".local/state/evallab-prod-live/data-flywheel/fw3/taskqa"
)
FIXED_GATE = "new_verified_0_5"
OLD_GATE = "old_0_8"


@dataclass(frozen=True)
class PreparedTask:
    """Private key metadata and a separately hash-checked, label-free source ref."""

    case: tq.Case
    row: Mapping[str, Any]
    keys: Mapping[str, Mapping[str, Any]]
    input_ref: str | None
    input_sha256: str | None
    source_digests: Mapping[str, str]


@dataclass(frozen=True)
class KeyGroup:
    """All admissible bank observations of one exact task-package/scorer-key pair."""

    task: PreparedTask
    key_id: str
    items: tuple[IssueItem, ...]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex(digest: str) -> str:
    value = digest.removeprefix("sha256:") if isinstance(digest, str) else ""
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("source has no valid SHA-256 digest")
    return value


def _checked(path: Path, digest: str) -> str:
    actual = _sha(path)
    if actual != _hex(digest):
        raise ValueError(f"source digest drift: {path}")
    return actual


def _unavailable(rows: list[dict[str, str]], source: str | Path, reason: str) -> None:
    rows.append({"source": str(source), "reason": reason})


def _hard_case(
    subject: str,
    items: Sequence[IssueItem],
    *,
    task_id: str,
    case_kind: str,
    kind: str,
    explanation: str,
    refs: Sequence[str],
    digests: Mapping[str, str],
    prepared: PreparedTask | None = None,
    uncertain: bool = False,
    label_type: str | None = None,
) -> HardCase:
    primary = sorted(items, key=lambda item: (item.source, item.item_id))[0]
    return HardCase.model_validate({
        "system": "task-qa",
        "item_ids": sorted({item.item_id for item in items}),
        "subject_id": subject,
        "task_id": task_id,
        "split": "DEV",
        "case_kind": case_kind,
        "kind": kind,
        "polarity": primary.polarity,
        "label_type": label_type or primary.label_type,
        "explanation": explanation,
        "evidence_steps": [],
        "input_ref": prepared.input_ref if prepared else None,
        "input_sha256": prepared.input_sha256 if prepared else None,
        "source_refs": sorted(set(refs)),
        "source_digests": dict(sorted(digests.items())),
        "adjudication": "uncertain" if uncertain else "pending",
        "adjudicator_model": None,
    })


def load_prepared_tasks(
    plan_path: Path, freeze_path: Path
) -> tuple[dict[str, PreparedTask], list[dict[str, str]]]:
    """Reuse OQ-6's loaders; reject changed labels and withhold missing/drifted inputs."""
    freeze = json.loads(freeze_path.read_bytes())
    pins = {str(Path(row["path"]).resolve()): row["sha256"] for row in freeze["pre_cross_data_files"]}
    plan_pin = pins.get(str(plan_path.resolve()))
    if plan_pin is None:
        raise ValueError(f"freeze does not pin plan: {plan_path}")
    _checked(plan_path, plan_pin)
    rows = {row["case_id"]: row for row in json.loads(plan_path.read_bytes())["cases"]}
    tasks: dict[str, PreparedTask] = {}
    unavailable: list[dict[str, str]] = []
    common = {str(plan_path): _sha(plan_path), str(freeze_path): _sha(freeze_path)}
    for case in tq.load_cases(plan_path):
        if case.stage != "dev":
            continue
        row = rows[case.case_id]
        digests = dict(common)
        keys: dict[str, Mapping[str, Any]] = {}
        if case.key_count:
            key_path = Path(row["keys_path"])
            pin = pins.get(str(key_path.resolve()))
            if pin is None or _hex(pin) != _hex(row["keys_sha256"]):
                raise ValueError(f"freeze does not pin these scorer keys: {key_path}")
            digests[str(key_path)] = _checked(key_path, pin)
            keys = {key["key_id"]: key for key in json.loads(key_path.read_bytes())}
            if set(keys) != case.key_ids:
                raise ValueError(f"{case.case_id}: duplicate or inconsistent scorer key ids")
            if any(key.get("package_digest") != case.package_digest for key in keys.values()):
                raise ValueError(f"{case.case_id}: scorer key package binding differs")
        package = row.get("pinned_native_package_digest")
        if package and case.package_digest and package != case.package_digest:
            raise ValueError(f"{case.case_id}: prepared and keyed task versions differ")
        input_ref = input_sha256 = None
        source = row.get("source_path")
        if case.preparation_status != "prepared" or not source:
            _unavailable(unavailable, case.case_id, "OQ-6 bounded task input was not prepared")
        else:
            path = Path(source)
            expected = row.get("source_sha256")
            pin = pins.get(str(path.resolve()))
            if not expected or pin is None or _hex(pin) != _hex(expected):
                _unavailable(unavailable, path, "bounded input lacks a matching frozen SHA-256")
            else:
                try:
                    input_sha256 = _checked(path, pin)
                    input_ref = str(path)
                    digests[str(path)] = input_sha256
                except (OSError, ValueError) as exc:
                    _unavailable(unavailable, path, str(exc))
        if case.package_digest:
            digests[f"task-package:{case.task}"] = _hex(case.package_digest)
        tasks[case.case_id] = PreparedTask(case, row, keys, input_ref, input_sha256, digests)
    return tasks, unavailable


def _strict_dev(ctx: BankContext, item: IssueItem, digest: str | None) -> bool:
    dataset = item.provenance.store.removeprefix("answer-keys:") if item.source == "answer-keys" else None
    return item.split == "DEV" and ctx.strict_split(
        item.declared_split,
        key_id=dataset,
        item_id=item.provenance.ref,
        task_id=item.task_id,
        task_digest=digest,
    ) == "DEV"


def bind_keyed_items(
    items: Sequence[IssueItem],
    tasks: Mapping[str, PreparedTask],
    findings: Mapping[str, Finding],
    ctx: BankContext,
) -> tuple[list[KeyGroup], list[tq.TaskItem], list[dict[str, str]]]:
    """Use the existing OQ-6 key join, then recheck package identity and strict DEV."""
    cases = [task.case for task in tasks.values()]
    by_task = {task.case.task: task for task in tasks.values()}
    by_id = {item.item_id: item for item in items}
    unavailable: list[dict[str, str]] = []
    library = [item for item in items if item.source == "library-findings"]
    for item in library:
        if item.locator not in findings:
            _unavailable(unavailable, item.item_id, "bank finding source is unavailable")
    selected = tq.select_items(
        [],
        [item for item in items if item.source == "answer-keys"],
        [item for item in library if item.locator in findings],
        findings,
        cases,
    )
    groups: dict[tuple[str, str], list[IssueItem]] = defaultdict(list)
    bound: list[tq.TaskItem] = []
    for selected_item in selected:
        task = by_task.get(selected_item.task)
        if task is None or selected_item.key_id is None:
            continue
        item = by_id[selected_item.item_id]
        if selected_item.key_id not in task.case.key_ids:
            raise ValueError(
                f"{item.item_id}: scorer key belongs to another OQ-6 task package"
            )
        digest = task.case.package_digest
        if not digest or selected_item.digest not in (None, digest):
            _unavailable(unavailable, item.item_id, "bank item does not bind the audited package")
            continue
        if not _strict_dev(ctx, item, digest):
            _unavailable(unavailable, item.item_id, "item or exact task version is not strict DEV")
            continue
        if item.label_type == "detector":
            _unavailable(unavailable, item.item_id, "detector-derived label is not independent truth")
            continue
        if item.polarity != "positive":
            _unavailable(unavailable, item.item_id, "refuted key is uncertainty, not a true keyed miss")
            continue
        groups[(task.case.case_id, selected_item.key_id)].append(item)
        bound.append(selected_item)
    result = [
        KeyGroup(tasks[case_id], key, tuple(sorted(rows, key=lambda item: item.item_id)))
        for (case_id, key), rows in sorted(groups.items())
    ]
    return result, bound, unavailable


def _complete(cell: tq.CellView) -> bool:
    return (
        cell.paired.get("complete") is True
        and cell.paired.get("audit_status") == "answered"
        and cell.paired.get("parse_status") == "complete"
        and cell.paired.get("judge_status") == "complete"
    )


def _cell_ref(cell: tq.CellView, record: Mapping[str, Any], gate: str, locator: str) -> str:
    served = (record.get("audit") or {}).get("served_identity") or "unavailable"
    return (
        f"{cell.path}#{gate}:{quote(locator, safe='')}"
        f";route={quote(cell.route, safe='')};arm={cell.arm};served={quote(served, safe='')}"
    )


def _cell_digests(cell: tq.CellView, record: Mapping[str, Any]) -> dict[str, str]:
    digests = {str(cell.path): _sha(cell.path)}
    for name, value in (
        ("freeze", record.get("freeze_sha256")),
        ("auditor-response", (record.get("prediction") or {}).get("response_sha256")),
        ("scorer-system", (record.get("scorer") or {}).get("scorer_system_sha256")),
    ):
        if value:
            digests[f"{cell.path}#{name}"] = _hex(value)
    return digests


def _label_refs(group: KeyGroup) -> list[str]:
    key_path = group.task.row["keys_path"]
    return [
        f"{key_path}#key_id={group.key_id}",
        *(f"{item.provenance.store}#{item.provenance.ref}" for item in group.items),
    ]


def _defect_class(group: KeyGroup) -> str:
    key = group.task.keys[group.key_id]
    classes = sorted({
        str(cause["source_class"])
        for cause in key.get("root_causes", [])
        if cause.get("source_class")
    })
    primary = key.get("primary_class") or {}
    if primary.get("key"):
        classes.append(str(primary["key"]))
    if primary.get("id"):
        classes.append(str(primary["id"]))
    return "/".join(sorted(set(classes))) or str(
        key.get("category") or "unavailable defect class"
    )


def _miss_reason(cell: tq.CellView) -> str:
    eligible = cell.paired["memberships"][FIXED_GATE]
    if not cell.payloads:
        return "The complete audit parsed no findings."
    if not eligible:
        best = max(float(payload["confidence"]) for payload in cell.payloads)
        verified = sum(payload.get("citation_verified") is True for payload in cell.payloads)
        return (
            f"All {len(cell.payloads)} parsed findings were gate-excluded; maximum confidence "
            f"was {best:g}, with {verified} citation-verified. No excluded candidate was scored "
            "as this key."
        )
    stats = cell.paired["gates"][FIXED_GATE]
    return (
        f"{len(eligible)} findings survived the fixed gate, but none received this key in "
        f"the frozen one-to-one matching ({stats.get('plausible_unkeyed', 0)} unkeyed, "
        f"{stats.get('source_contradictions', 0)} source-contradicted). Same direction is not a hit."
    )


def mine_keyed_cases(
    groups: Sequence[KeyGroup],
    cells: Mapping[tuple[str, str, str], tq.CellView],
    records: Mapping[Path, Mapping[str, Any]],
    *,
    source_digests: Mapping[str, str] | None = None,
    routes: Sequence[str] = tq.ROUTES,
) -> tuple[list[HardCase], dict[str, Any]]:
    """Group exact fixed-gate misses and key-matched gate drops, without new judging."""
    hard: list[HardCase] = []
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for group in groups:
        observations: list[tuple[tq.CellView, str]] = []
        misses: list[tq.CellView] = []
        drops: list[tuple[tq.CellView, str, str]] = []
        for route, arm in ((route, arm) for route in routes for arm in tq.ARMS):
            tally = counts[f"{route}#{arm}"]
            cell = cells.get((route, arm, group.task.case.case_id))
            if group.task.case.preparation_status != "prepared" or (
                cell is not None and cell.paired.get("audit_status") == "unavailable_input"
            ):
                tally["unavailable_input_keys"] += 1
                continue
            if cell is None:
                tally["not_run_keys"] += 1
                continue
            if not _complete(cell):
                tally["incomplete_keys"] += 1
                continue
            tally["complete_keys"] += 1
            matching = {gate: cell.paired["gates"][gate]["matching"] for gate in tq.GATES}
            if group.key_id not in matching[FIXED_GATE].values():
                tally["fixed_gate_missed_keys"] += 1
                misses.append(cell)
                observations.append((cell, FIXED_GATE))
            else:
                tally["fixed_gate_matched_keys"] += 1
            for gate, other in ((OLD_GATE, FIXED_GATE), (FIXED_GATE, OLD_GATE)):
                candidates = [candidate for candidate, key in matching[other].items() if key == group.key_id]
                for candidate in candidates:
                    if candidate not in cell.paired["memberships"][gate]:
                        tally[f"{gate}_dropped_keyed_findings"] += 1
                        drops.append((cell, gate, candidate))
                        observations.append((cell, gate))
        if not observations:
            continue
        primary = sorted(group.items, key=lambda item: (item.source, item.item_id))[0]
        refs = _label_refs(group)
        digests = {**(source_digests or {}), **group.task.source_digests}
        for item in group.items:
            if item.provenance.digest:
                digests[f"{item.provenance.store}#{item.provenance.ref}"] = _hex(item.provenance.digest)
        for cell, gate in observations:
            refs.append(_cell_ref(cell, records[cell.path], gate, group.key_id))
            digests.update(_cell_digests(cell, records[cell.path]))
        explanation = (
            f"Source key class: {_defect_class(group)}. The fixed gate left this exact "
            f"package/key unmatched in {len(misses)} complete route/arm audits. "
        )
        if drops:
            cell, gate, candidate = drops[0]
            payload = cell.payloads[int(candidate[1:]) - 1]
            explanation += (
                f"{cell.route} arm {cell.arm}: {candidate} was key-matched under the other gate "
                f"but excluded by {gate} (confidence {payload['confidence']:g}, "
                f"citation_verified={payload['citation_verified']}). "
            )
        elif misses:
            cell = misses[0]
            explanation += f"{cell.route} arm {cell.arm}: {_miss_reason(cell)} "
        explanation += "This is a retained-source miss, not fresh defect confirmation."
        subject = f"{group.task.case.task}@{group.task.case.package_digest}#key={group.key_id}"
        hard.append(_hard_case(
            subject, group.items, task_id=group.task.case.task, case_kind="miss",
            kind=primary.kind, explanation=explanation, refs=refs, digests=digests,
            prepared=group.task,
        ))
    return hard, {name: dict(sorted(tally.items())) for name, tally in sorted(counts.items())}


def uncertainty_cases(
    groups: Sequence[KeyGroup],
    cells: Mapping[tuple[str, str, str], tq.CellView],
    records: Mapping[Path, Mapping[str, Any]],
    *,
    source_digests: Mapping[str, str] | None = None,
) -> list[HardCase]:
    """Scorer unkeyed/refuted candidates remain questions, never clean-task negatives."""
    by_case: dict[str, list[KeyGroup]] = defaultdict(list)
    for group in groups:
        by_case[group.task.case.case_id].append(group)
    hard: list[HardCase] = []
    for (_, _, case_id), cell in sorted(cells.items()):
        if case_id not in by_case or not _complete(cell):
            continue
        task_groups = by_case[case_id]
        task = task_groups[0].task
        items = [item for group in task_groups for item in group.items]
        record = records[cell.path]
        for judgment in (record.get("scorer") or {}).get("judgments", []):
            verdict = judgment["verdict"]
            if verdict not in {"plausible_unkeyed", "false_alarm", "unclear"}:
                continue
            candidate = judgment["id"]
            payload = cell.payloads[int(candidate[1:]) - 1]
            kind = {"wrong_pass": "grader_wrong_pass", "unfair_fail": "grader_unfair_fail"}[payload["direction"]]
            subject = f"{task.case.task}@{task.case.package_digest}#{cell.route}:{cell.arm}:{candidate}"
            explanation = (
                f"{cell.route} arm {cell.arm} produced a {payload['direction']} candidate "
                f"at confidence {payload['confidence']:g}; citation_verified="
                f"{payload['citation_verified']}. The stored union scorer called it {verdict}, "
                "not an exact matched key. This is candidate-level uncertainty for the Verifier, "
                "not a negative label for the task: another real keyed defect may coexist. "
                "No new adjudication or execution was performed."
            )
            refs = [
                _cell_ref(cell, record, "union", candidate),
                *(ref for group in task_groups for ref in _label_refs(group)),
            ]
            digests = {
                **(source_digests or {}),
                **task.source_digests,
                **_cell_digests(cell, record),
            }
            hard.append(_hard_case(
                subject, items, task_id=task.case.task, case_kind="label_disagreement",
                kind=kind, explanation=explanation, refs=refs, digests=digests,
                prepared=task, uncertain=True, label_type="judge",
            ))
    return hard


def source_uncertainty_cases(
    items: Sequence[IssueItem],
    tasks: Mapping[str, PreparedTask],
    findings: Mapping[str, Finding],
    ctx: BankContext,
    *,
    source_digests: Mapping[str, str] | None = None,
) -> tuple[list[HardCase], list[dict[str, str]]]:
    """Preserve refuted source claims without certifying their whole tasks clean."""
    by_task = {task.case.task: task for task in tasks.values()}
    hard: list[HardCase] = []
    unavailable: list[dict[str, str]] = []
    for item in items:
        task = by_task.get(tq.task_name(item.task_id or item.subject_id))
        if (
            task is None or item.subject_type != "task" or item.polarity != "negative"
            or item.source not in {"answer-keys", "library-findings"}
            or item.label_type == "detector"
        ):
            continue
        finding = findings.get(item.locator) if item.source == "library-findings" else None
        digest = finding.task.task_version_digest if finding else None
        if item.source == "library-findings" and finding is None:
            _unavailable(unavailable, item.item_id, "refuted finding source is unavailable")
            continue
        if not _strict_dev(ctx, item, digest):
            continue
        exact_input = task if digest and digest == task.case.package_digest else None
        if exact_input is None:
            _unavailable(
                unavailable, item.item_id,
                "source-ref-only: refuted claim lacks this exact OQ-6 package binding",
            )
        ref = f"{item.provenance.store}#{item.provenance.ref}"
        digests = dict(source_digests or {})
        if item.provenance.digest:
            digests[ref] = _hex(item.provenance.digest)
        if digest:
            digests[f"task-package:{task.case.task}"] = _hex(digest)
        if exact_input:
            digests.update(exact_input.source_digests)
        hard.append(_hard_case(
            f"{item.subject_id}#refuted-source-claim={item.provenance.ref}",
            [item], task_id=item.task_id or task.case.task, case_kind="label_disagreement",
            kind=item.kind, uncertain=True, prepared=exact_input, refs=[ref], digests=digests,
            explanation=(
                "The source refuted one defect claim. Its source-negative polarity is retained "
                "only as an uncertain claim-level observation, not an automatic clean-task label "
                "or a proven detector false alarm. Another real defect may coexist on this task. "
                "No same-task/direction match establishes claim identity, and no new adjudication "
                "or execution was performed."
            ),
        ))
    return hard, unavailable


def repair_regression_cases(
    items: Sequence[IssueItem],
    repo_root: Path,
    summary_path: Path,
    ctx: BankContext,
) -> tuple[list[HardCase], list[dict[str, str]]]:
    """Admit TS-14 reference rejections only with both exact native receipt bindings."""
    unavailable: list[dict[str, str]] = []
    if not summary_path.is_file():
        _unavailable(unavailable, summary_path, "TS-14 native oracle summary is unavailable")
        return [], unavailable
    summary = json.loads(summary_path.read_bytes())
    bank = {item.locator: item for item in items if item.source == "grader-repairs" and item.split == "DEV"}
    hard: list[HardCase] = []
    for row in summary.get("cases", []):
        before, after = row.get("oracle_original") or {}, row.get("oracle_repaired") or {}
        if before.get("reward") != 1 or not isinstance(after.get("reward"), int | float) or after["reward"] >= 1:
            continue
        pr = str(row["pr"])
        item = bank.get(pr)
        try:
            if item is None:
                raise ValueError("no strict-DEV bank repair item binds this PR")
            variant_path = repo_root / item.provenance.ref
            if not item.provenance.digest:
                raise ValueError("bank repair item has no variant source digest")
            variant_sha = _checked(variant_path, item.provenance.digest)
            variant = json.loads(variant_path.read_bytes())
            parent, repaired = variant["parent"]["digest"], variant["variant_digest"]
            if tq.task_name(row["task_name"]) != tq.task_name(item.task_id or ""):
                raise ValueError("native pair task identity differs from bank repair")
            if tq.task_name(variant["task_name"]) != tq.task_name(row["task_name"]):
                raise ValueError("native pair task identity differs from pinned variant")
            if (before.get("task_digest"), after.get("task_digest")) != (parent, repaired):
                raise ValueError("native pair is not bound to the exact original/repaired digests")
            if not before.get("input_digest") or before["input_digest"] != after.get("input_digest"):
                raise ValueError("native pair does not bind the same canonical reference input")
            if not all(_strict_dev(ctx, item, digest) for digest in (parent, repaired)):
                raise ValueError("original or repaired package is no longer strict DEV")
            assignment_keys = [
                key for key, split in variant.get("inputs", {}).get("dev_assignments", [])
                if split == "dev"
            ]
            if any(
                ctx.strict_split(
                    item.declared_split, key_id=key, task_id=item.task_id,
                    task_digest=digest,
                ) != "DEV"
                for key in assignment_keys for digest in (parent, repaired)
            ):
                raise ValueError("repair's source-key assignment is no longer strict DEV")
            digests = {str(summary_path): _sha(summary_path), str(variant_path): variant_sha}
            refs = [str(variant_path), f"{summary_path}#pr={pr}"]
            for side in (before, after):
                receipt_path = Path(side["native_receipt"])
                digests[str(receipt_path)] = _checked(receipt_path, side["native_receipt_sha256"])
                receipt = json.loads(receipt_path.read_bytes())
                if not (
                    side.get("status") == receipt.get("status") == "measured"
                    and receipt.get("executed") is True
                    and receipt.get("error") is None
                    and not receipt.get("evidence_errors")
                    and receipt.get("reward") == side["reward"]
                    and receipt.get("task", {}).get("digest") == side["task_digest"]
                    and bool(side.get("tests_digest"))
                    and receipt.get("task", {}).get("tests_digest") == side["tests_digest"]
                    and receipt.get("input", {}).get("digest") == side["input_digest"]
                    and tq.task_name(receipt.get("task", {}).get("id", "")) == tq.task_name(row["task_name"])
                ):
                    raise ValueError(f"native oracle receipt is not bound: {receipt_path}")
                refs.append(str(receipt_path))
                digests[f"{receipt_path}#tests"] = _hex(side["tests_digest"])
            digests[f"task-package:{row['task_name']}#original"] = _hex(parent)
            digests[f"task-package:{row['task_name']}#repaired"] = _hex(repaired)
            digests[f"canonical-reference:pr/{pr}"] = _hex(before["input_digest"])
            subject = f"{row['task_name']}@{parent}->{repaired}#repair-regression"
            hard.append(_hard_case(
                subject, [item], task_id=row["task_name"], case_kind="label_disagreement",
                kind="grader_unfair_fail", label_type="executed", uncertain=True,
                explanation=(
                    "Repair-regression/unfair-fail candidate: native canonical-reference oracle "
                    f"reward changed from 1 to {after['reward']:g} on the same input. Both executed "
                    "receipts bind the exact original/repaired package digests. Reference rejection "
                    "is observed, but task correctness still needs review. These oracle controls "
                    "alone establish neither a wrong-pass differential nor a qualified repair. "
                    "No frozen bounded input for the repaired version is substituted with the original."
                ),
                refs=refs, digests=digests,
            ))
            _unavailable(unavailable, subject, "source-ref-only: no frozen OQ-6 input for repaired package")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            _unavailable(unavailable, f"{summary_path}#pr={pr}", str(exc))
    return hard, unavailable


def build_taskqa_cases(
    repo_root: Path,
    *,
    plan_path: Path = tq.DEFAULT_PLAN,
    freeze_path: Path = tq.DEFAULT_FREEZE,
    call_roots: Sequence[Path] = tq.DEFAULT_CALL_ROOTS,
    state_root: Path | None = None,
    ts14_summary: Path | None = None,
) -> tuple[list[HardCase], dict[str, Any]]:
    """Read retained local sources only and return cases plus exact arm denominators."""
    repo_root = repo_root.resolve()
    plan_path, freeze_path = plan_path.resolve(), freeze_path.resolve()
    call_roots = tuple(root.resolve() for root in call_roots)
    state_root = state_root.resolve() if state_root is not None else None
    tasks, unavailable = load_prepared_tasks(plan_path, freeze_path)
    ctx = BankContext(repo_root=repo_root, **({"state_root": state_root} if state_root else {}))
    bank = IssueBank(repo_root, state_root=state_root)
    sources = ("answer-keys", "library-findings", "grader-repairs")
    items = list(bank.iter_items(sources, split="DEV"))
    source_digests: dict[str, str] = {}
    for source in sources:
        path = bank.items_path(source)
        if path.is_file():
            source_digests[str(path)] = _sha(path)
        else:
            _unavailable(unavailable, path, "issue-bank source file is unavailable")
    assignments = repo_root / "research/answer-keys/splits/assignments.csv"
    if assignments.is_file():
        source_digests[str(assignments)] = _sha(assignments)
    else:
        _unavailable(unavailable, assignments, "strict DEV assignment authority is unavailable")
    store = LibraryStore.for_repo(repo_root, state_root=state_root)
    findings: dict[str, Finding] = {}
    for collection in sorted({
        item.provenance.store.removeprefix("failure-library:findings/")
        for item in items if item.source == "library-findings"
    }):
        path = store.findings_path(collection)
        if not path.is_file():
            _unavailable(unavailable, path, "bank finding collection is unavailable")
            continue
        digest = _sha(path)
        relevant = [item for item in items if item.provenance.store == f"failure-library:findings/{collection}"]
        if any(item.provenance.digest and _hex(item.provenance.digest) != digest for item in relevant):
            _unavailable(unavailable, path, "finding collection drifted from issue-bank provenance")
            continue
        source_digests[str(path)] = digest
        findings.update((finding.finding_id, finding) for finding in store.iter_findings(collection))
    groups, keyed_items, missing = bind_keyed_items(items, tasks, findings, ctx)
    unavailable.extend(missing)
    cases = [task.case for task in tasks.values()]
    cells = tq.load_cells(cases, call_roots)
    records: dict[Path, Mapping[str, Any]] = {}
    admitted: dict[tuple[str, str, str], tq.CellView] = {}
    for identity, cell in cells.items():
        record = json.loads(cell.path.read_bytes())
        row = tasks[cell.case_id].row
        if record.get("source_sha256") != row.get("source_sha256") or record.get("keys_sha256") != row.get("keys_sha256"):
            _unavailable(unavailable, cell.path, "audit source/key digest differs from frozen plan")
            continue
        records[cell.path] = record
        admitted[identity] = cell
    hard, arm_counts = mine_keyed_cases(groups, admitted, records, source_digests=source_digests)
    uncertain = uncertainty_cases(
        groups, admitted, records, source_digests=source_digests,
    )
    refuted, missing = source_uncertainty_cases(
        items, tasks, findings, ctx, source_digests=source_digests,
    )
    unavailable.extend(missing)
    repairs, missing = repair_regression_cases(
        items, repo_root, ts14_summary or ts14_summary_path(), ctx,
    )
    unavailable.extend(missing)
    hard.extend(uncertain)
    hard.extend(refuted)
    hard.extend(repairs)
    paired = tq._frozen_module(freeze_path, "paired.py", "fw3_oq6_frozen_paired")
    result_summary = tq._frozen_module(freeze_path, "result_summary.py", "fw3_oq6_frozen_result_summary")
    reproduction = tq.keyed_reproduction(
        tq.task_findings(keyed_items, cases, admitted), keyed_items, cases, admitted,
        aggregate=paired.aggregate_cells, shared=result_summary.shared_complete_rows,
    )
    for entry in reproduction.values():
        for arm in tq.ARMS:
            for gate in tq.GATES:
                tally = entry.get(f"{arm}@{gate}")
                if tally:
                    tally["frozen_missed_keys"] = tally["frozen_evaluable_keys"] - tally["frozen_matched_keys"]
                    if not tally["agree"]:
                        _unavailable(unavailable, "keyed-reproduction", "strict-DEV bank bindings do not cover frozen scorer hits")
    bound = {(group.task.case.case_id, group.key_id) for group in groups}
    unbound = sum((case.case_id, key) not in bound for case in cases for key in case.key_ids)
    if unbound:
        _unavailable(unavailable, plan_path, f"{unbound} frozen DEV keys lack admissible exact bank bindings")
    hard.sort(key=lambda case: case.case_id)
    summary = {
        "schema": "fw3.taskqa-hard-cases-summary/v1",
        "cases": len(hard),
        "keyed_package_key_cases": len(hard) - len(uncertain) - len(refuted) - len(repairs),
        "uncertain_scorer_candidates": len(uncertain),
        "uncertain_refuted_source_claims": len(refuted),
        "repair_regression_cases": len(repairs),
        "strict_dev_package_key_groups": len(groups),
        "unbound_frozen_dev_keys": unbound,
        "audit_key_observations_by_route_arm": arm_counts,
        "shared_complete_keyed_reproduction": reproduction,
        "case_kinds": dict(sorted(Counter(case.case_kind for case in hard).items())),
        "input_available_cases": sum(case.input_ref is not None for case in hard),
        "source_digests": {**source_digests, str(plan_path): _sha(plan_path), str(freeze_path): _sha(freeze_path)},
        "unavailable": unavailable,
        "model_calls": 0,
        "limits": [
            "Keyed misses are retained source-label observations, not new defect adjudication.",
            "Unkeyed and source-contradicted candidates are uncertainty, never clean-task negatives.",
            "Counts separate route/arm audit-key observations from deduplicated package/key cases.",
            "Repair reference rejections do not establish a wrong-pass differential or qualified repair.",
        ],
    }
    return hard, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--plan", type=Path, default=tq.DEFAULT_PLAN)
    parser.add_argument("--freeze", type=Path, default=tq.DEFAULT_FREEZE)
    parser.add_argument("--call-root", type=Path, action="append")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--ts14-summary", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        cases, summary = build_taskqa_cases(
            args.repo_root, plan_path=args.plan, freeze_path=args.freeze,
            call_roots=args.call_root or tq.DEFAULT_CALL_ROOTS, state_root=args.state_root,
            ts14_summary=args.ts14_summary,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        summary = {
            "schema": "fw3.taskqa-hard-cases-summary/v1", "cases": 0, "model_calls": 0,
            "status": "unavailable", "unavailable": [{"source": str(args.plan), "reason": str(exc)}],
        }
        args.out.mkdir(parents=True, exist_ok=True)
        write_cases(args.out / "task-qa.jsonl", [])
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        print()
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    write_cases(args.out / "task-qa.jsonl", cases)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    json.dump({"cases": len(cases), "summary": str(args.out / "summary.json"), "model_calls": 0}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
