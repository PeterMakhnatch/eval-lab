"""Answer keys: every labelled item of ``research/answer-keys/*/items.jsonl``.

One item per labelled key row. Only label fields are read (verdict, category,
strength, role, refs, split); solutions, hidden tests, witnesses, evidence prose and
root-cause bodies never enter an item. ``expected_verdict`` ``fail`` is the issue
present (``positive``), ``pass`` a labelled clean subject (``negative``); ``unknown``
is not evaluable and is never an item (nor is a "not_a_pass" failed run, which is not
an instance of any bank kind). Every key needs an entry in :data:`KEYS`: a new key
directory without one fails the build instead of being dropped silently.

Mapping (verdict -> polarity; kind / label_type / granularity per key):

=============================== ============ ============================== ==================
key                             granularity  kind                           label_type
=============================== ============ ============================== ==================
breaker-bench-v1                task         category (weak, tamperable and executed (benchflow
                                             the django-7530 baseline pass  sweep) / judge
                                             -> grader_wrong_pass; reachable (terminal-wrench)
                                             -> task_defect); a hardened
                                             0.2.2 parent takes its 0.2.0
                                             child's kind
cheatbench-actions-v1           run          cheat                          human
hacktrace-act-v1                run          cheat                          judge / detector
trace-paired-v1                 run          reward_hacking -> cheat,       consensus (audit) /
                                             false_positive -> wrong_pass   executed (scripted,
                                                                            oracle) / static
trajectory-judge-400            run          cheat (brief; injected agent   construction
                                             faults, not sandbox cheats)
delphik-open-defect-tb2         task         category map; negative         upstream_fix (PR id) /
                                             -> task_defect                 static
envcheck-tb4-39                 task         category map; negative         static / human (Epoch)
                                             -> task_defect
executed-outcomes-v1            task         weak, tamperable ->            executed
                                             grader_wrong_pass
mimo-weak-tests-v1              task + run   grader_wrong_pass              executed / static
mimo-weak-tests-v2 / v2.1       task         grader_wrong_pass (positive    executed
                                             only; v2.1 is a superset)
mimo-heldout-v6-to-v7,          task         grader_wrong_pass (positive    executed
mimo-stratified-sample100,                   only)
tb21-weak-tests-v1,
weak-tests-generalization-
20261019
tb2-to-tb21-v1                  task         by change_kind                 upstream_fix
tb4-audit-v1                    task         by taxonomy class direction    human (PM report) /
                                                                            executed (witness)
mimo-fresh-window3              run          grader_wrong_pass (wrong_pass  executed
                                             +, real_pass -; not_a_pass is
                                             skipped)
mimo-v7-canonical-pass70        run          grader_wrong_pass              executed
=============================== ============ ============================== ==================

Task-category kinds (``expected_category``): ``weak``/``tamperable`` ->
``grader_wrong_pass``; ``reachable``/``broken``/``prompt_misaligned`` -> ``task_defect``.
tb4-audit-v1 refines through its taxonomy direction: ``false_fail`` classes (unstated
requirement, over-strict oracle, wrong answer key, infeasible) -> ``grader_unfair_fail``.
A negative without a category takes the key's negative kind (``NEGATIVE_KIND``).

Split: ``ctx.strict_split(row split, key_id=, item_id=, task_id=, task_digest=)``;
``declared_split`` is the row's own split string.

OQ-6: tb4-audit-v1 task findings whose strict split is DEV carry ``provenance.note``
starting ``oq6-keyed``: the 45 findings on 14 tasks that are part of the 118 keyed DEV
annotations of the Task QA gate rerun (the other 73 annotations on 30 tasks are
``mimo-defect-map-v1`` findings, not answer-key items).

CheatBench: a DEV ``cheatbench-actions-v1`` item whose full trajectory exists in the
results home has ``harness_selector="cheatbench"`` (what ``evallab trace run --runs
cheatbench`` resolves); the 21 ``cheatbench/bountybench`` items are quarantined for
missing actor provenance (``actor_transcript=False``, no selector).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    IssueItem,
    Kind,
    LabelType,
    Polarity,
    Provenance,
    SourceSummary,
    new_item,
)
from evallab.results_home import results_root

SOURCE = "answer-keys"
KEYS_RELATIVE = Path("research/answer-keys")
OQ6_NOTE = "oq6-keyed"
BOUNTYBENCH_TASK = "cheatbench/bountybench"
CHEATBENCH_SELECTOR = "cheatbench"
BENCHMARK_CARRIER_PREFIX = "__benchmark__/"
LABEL_LIMIT = 200
NOTE_LIMIT = 300

_POLARITY: dict[str, Polarity] = {"fail": "positive", "pass": "negative"}
CATEGORY_KIND: dict[str, Kind] = {
    "weak": "grader_wrong_pass",
    "tamperable": "grader_wrong_pass",
    "reachable": "task_defect",
    "broken": "task_defect",
    "prompt_misaligned": "task_defect",
}
# Breaker's only ``broken`` items are django-7530: its FAIL_TO_PASS passes with no patch,
# so the grader accepts a no-op solution.
BREAKER_KIND: dict[str, Kind] = {**CATEGORY_KIND, "broken": "grader_wrong_pass"}
# tb4-audit-v1 taxonomy class -> kind (direction false_fail -> grader_unfair_fail).
TB4_CLASS_KIND: dict[str, Kind] = {
    "weak_check": "grader_wrong_pass",
    "grader_tamperable": "grader_wrong_pass",
    "exploitable_llm_judge": "grader_wrong_pass",
    "unstated_requirement": "grader_unfair_fail",
    "overstrict_oracle": "grader_unfair_fail",
    "wrong_answer_key": "grader_unfair_fail",
    "infeasible_as_written": "grader_unfair_fail",
    "grader_logic_bug": "task_defect",
    "answer_reachable": "task_defect",
    "instruction_grader_contradiction": "task_defect",
    "env_mismatch_or_flaky": "task_defect",
}
TB2_TB21_CHANGE_KIND: dict[str, Kind] = {
    "format_only": "task_defect",
    "mixed": "task_defect",
    "environment_drift_or_oracle_fix": "task_defect",
    "verifier_relaxation_or_fix": "grader_unfair_fail",
    "instruction_clarification": "task_defect",
    "anti_hack_or_verifier_strengthening": "grader_wrong_pass",
    "resource_timeout": "task_defect",
}
# The kind of a negative whose row carries no category.
NEGATIVE_KIND: dict[str, Kind] = {
    "delphik-open-defect-tb2": "task_defect",
    "envcheck-tb4-39": "task_defect",
    "tb4-audit-v1": "task_defect",
    "executed-outcomes-v1": "grader_wrong_pass",
    "mimo-weak-tests-v1": "grader_wrong_pass",
    "mimo-fresh-window3": "grader_wrong_pass",
    "mimo-v7-canonical-pass70": "grader_wrong_pass",
    "breaker-bench-v1": "grader_wrong_pass",
}
TRACE_PAIRED = "trace-paired-v1"


@dataclass(frozen=True)
class Decision:
    """What one key row means for the bank (``None`` from a decider: not an item)."""

    kind: Kind
    polarity: Polarity
    label_type: LabelType
    label: str
    note: str | None = None


@dataclass
class KeyView:
    """All rows of one key, plus the cross-key lookups its decider needs."""

    key_id: str
    rows: list[dict[str, Any]]
    by_id: dict[str, dict[str, Any]]
    trace_paired: Mapping[str, dict[str, Any]]


Decider = Callable[[dict[str, Any], KeyView], "Decision | None"]


@dataclass(frozen=True)
class KeySpec:
    key_id: str
    license: str | None
    decide: Decider


def _label(*parts: object) -> str:
    text = ":".join(str(part) for part in parts if part not in (None, "", []))
    return (text or "unlabelled")[:LABEL_LIMIT]


def _polarity(row: Mapping[str, Any]) -> Polarity | None:
    return _POLARITY.get(str(row.get("expected_verdict")))


def _joined(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    if isinstance(value, Mapping):
        return ",".join(f"{k}={v}" for k, v in value.items())
    return "" if value is None else str(value)


def _category_kind(row: Mapping[str, Any], key_id: str) -> Kind:
    """Kind from ``expected_category``; a category-less negative takes the key's default."""
    return CATEGORY_KIND.get(str(row.get("expected_category")), NEGATIVE_KIND[key_id])


# ---------------------------------------------------------------------------
# deciders, one per key
# ---------------------------------------------------------------------------


def _breaker(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None  # the two unmeasured 0.2.2 timeout cells
    source = row.get("source_set")
    reference = row
    if polarity == "negative":
        counterpart = view.by_id.get(str(row.get("paired_counterpart_id")))
        if counterpart is not None and counterpart.get("expected_verdict") == "fail":
            reference = counterpart  # a hardened parent answers its hackable child's kind
    kind = BREAKER_KIND.get(str(reference.get("expected_category")), NEGATIVE_KIND[view.key_id])
    label_type: LabelType = "executed" if source == "benchflow_0.2.0_vs_0.2.2" else "judge"
    return Decision(
        kind,
        polarity,
        label_type,
        _label(source, row.get("role"), reference.get("expected_category"),
               row.get("expected_root_cause")),
    )


def _cheatbench(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    labels = row.get("source_labels") or {}
    return Decision(
        "cheat", polarity, "human",
        _label(labels.get("label"), labels.get("cheating_mechanism")),
    )


def _hacktrace(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    labels = row.get("source_labels") or {}
    label_type: LabelType = "judge" if labels.get("label_source") == "judge" else "detector"
    return Decision(
        "cheat", polarity, label_type,
        _label("ACT" if labels.get("ACT") else "not-act", labels.get("judge_category")),
    )


def _trace_paired_label_type(row: Mapping[str, Any]) -> LabelType:
    source = str(row.get("label_source") or "")
    if source.startswith("contamination-audit"):
        return "consensus"  # GPT judge + blind Claude judge, disagreements resolved
    if source.startswith("HAR-207"):
        return "static"  # diff-against-reference review
    return "executed"  # scripted attack the grader rewarded / oracle rewarded 1


def _trace_paired(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    kind: Kind = "cheat" if row.get("expected_category") == "reward_hacking" else "grader_wrong_pass"
    return Decision(
        kind, polarity, _trace_paired_label_type(row),
        _label(row.get("trace_kind"), row.get("role"), _joined(row.get("expected_root_cause"))),
    )


def _trajectory_judge(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    cause = row.get("expected_root_cause") or {}
    detail = "clean" if polarity == "negative" else _label(
        cause.get("failure_type"), "silent" if row.get("silent") else "loud"
    )
    return Decision(
        "cheat", polarity, "construction", detail,
        "injected agent-process fault in a synthetic customer-support domain, not a sandbox cheat",
    )


def _delphik(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    causes = row.get("root_cause") or []
    first = causes[0] if causes else {}
    label_type: LabelType = "upstream_fix" if "__pr-" in str(first.get("id")) else "static"
    return Decision(
        _category_kind(row, view.key_id), polarity, label_type,
        _label(row.get("label_strength"), row.get("expected_category"), first.get("source_class")),
    )


def _envcheck(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    strength = row.get("label_strength")
    causes = row.get("root_cause") or []
    label_type: LabelType = "human" if strength == "epoch_flagged" else "static"
    return Decision(
        _category_kind(row, view.key_id), polarity, label_type,
        _label(strength, row.get("expected_category"),
               causes[0].get("source_class") if causes else None),
    )


def _executed_task(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    selected = _joined(row.get("selected_by"))
    return Decision(
        _category_kind(row, view.key_id), polarity, "executed",
        _label(row.get("label_strength"), row.get("expected_category"),
               _joined(row.get("expected_root_causes"))),
        f"selected_by={selected}" if selected else None,
    )


def _mimo_weak_tests_v1(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    causes = _joined(row.get("expected_root_causes"))
    if (row.get("subject") or {}).get("kind") == "trace":
        paired = view.trace_paired.get(str(row["subject"].get("trial_id")))
        label_type: LabelType = _trace_paired_label_type(paired) if paired else "static"
        return Decision(
            "grader_wrong_pass", polarity, label_type,
            _label("trace", row.get("role"), row.get("expected_category")),
        )
    label_type = "static" if causes == "partial_fix_pass" else "executed"
    selected = _joined(row.get("selected_by"))
    return Decision(
        _category_kind(row, view.key_id), polarity, label_type,
        _label(row.get("label_strength"), row.get("expected_category"), causes),
        f"selected_by={selected}" if selected else None,
    )


def _positive_only(row: dict[str, Any], view: KeyView) -> Decision | None:
    """Keys whose only labelled verdict is ``fail``; ``unknown`` is not clean."""
    polarity = _polarity(row)
    if polarity is None:
        return None
    selected = _joined(row.get("selected_by"))
    notes = [f"selected_by={selected}"] if selected else []
    if view.key_id == "mimo-weak-tests-v2.1":
        notes.append("superset of mimo-weak-tests-v2 positives")
    return Decision(
        CATEGORY_KIND.get(str(row.get("expected_category")), "grader_wrong_pass"), polarity, "executed",
        _label(row.get("label_strength"), row.get("expected_category"),
               _joined(row.get("expected_root_causes"))),
        "; ".join(notes) or None,
    )


def _tb2_to_tb21(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    change = str(row.get("change_kind"))
    return Decision(
        TB2_TB21_CHANGE_KIND[change], polarity, "upstream_fix",
        _label(change, row.get("role"), row.get("expected_verdict")),
        "breaker-bench-v1 dev exposure" if row.get("breaker_bench_dev_exposure") else None,
    )


def _tb4_audit(row: dict[str, Any], view: KeyView) -> Decision | None:
    polarity = _polarity(row)
    if polarity is None:
        return None
    strength = row.get("label_strength")
    label_type: LabelType = "executed" if strength == "confirmed" else "human"
    note = "benchmark-wide context finding, not a task" if row.get("scope") == "benchmark" else None
    return Decision(
        TB4_CLASS_KIND[str(row["taxonomy_class"])], polarity, label_type,
        _label(row.get("taxonomy_class"), strength, _joined(row.get("pm_types"))),
        note,
    )


def _run_outcome(row: dict[str, Any], view: KeyView) -> Decision | None:
    """MiMo run keys: a wrong pass is a positive, a real pass the negative."""
    polarity = _polarity(row)
    if polarity is None:
        return None
    if row.get("source_label") == "not_a_pass":
        return None  # a failed run: the grader did not pass anything wrongly
    return Decision(
        "grader_wrong_pass", polarity, "executed",
        _label(row.get("source_label"), row.get("prompt_condition") or row.get("arm")),
    )


KEYS: dict[str, KeySpec] = {
    spec.key_id: spec
    for spec in (
        KeySpec("breaker-bench-v1", "Apache-2.0", _breaker),
        KeySpec("cheatbench-actions-v1", "MIT", _cheatbench),
        KeySpec("delphik-open-defect-tb2", None, _delphik),
        KeySpec("envcheck-tb4-39", "CC-BY-4.0", _envcheck),
        KeySpec("executed-outcomes-v1", "internal", _executed_task),
        KeySpec("hacktrace-act-v1", "CC-BY-4.0", _hacktrace),
        KeySpec("mimo-fresh-window3", "internal", _run_outcome),
        KeySpec("mimo-heldout-v6-to-v7", "internal", _positive_only),
        KeySpec("mimo-stratified-sample100", "internal", _positive_only),
        KeySpec("mimo-v7-canonical-pass70", "internal", _run_outcome),
        KeySpec("mimo-weak-tests-v1", "internal", _mimo_weak_tests_v1),
        KeySpec("mimo-weak-tests-v2", "Apache-2.0", _positive_only),
        KeySpec("mimo-weak-tests-v2.1", "Apache-2.0", _positive_only),
        KeySpec("tb2-to-tb21-v1", None, _tb2_to_tb21),
        KeySpec("tb21-weak-tests-v1", None, _positive_only),
        KeySpec("tb4-audit-v1", None, _tb4_audit),
        KeySpec(TRACE_PAIRED, "internal", _trace_paired),
        KeySpec("trajectory-judge-400", "MIT", _trajectory_judge),
        KeySpec("weak-tests-generalization-20261019", None, _positive_only),
    )
}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _key_dirs(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/items.jsonl"))


def _check_keys(root: Path) -> None:
    unmapped = sorted(set(_key_dirs(root)) - set(KEYS))
    if unmapped:
        raise ValueError(f"answer keys without a bank mapping (add to KEYS): {unmapped}")


# ---------------------------------------------------------------------------
# per-item facts
# ---------------------------------------------------------------------------


def _reference_path(row: Mapping[str, Any]) -> str | None:
    for field in ("trial_ref", "full_ref"):
        ref = row.get(field)
        if isinstance(ref, Mapping) and ref.get("path"):
            return str(ref["path"])
    return None


def _trajectory_exists(path: str | None, results: Path) -> bool:
    return path is not None and (results / path / "agent" / "trajectory.json").is_file()


def _is_run(row: Mapping[str, Any]) -> bool:
    """Trace subjects are runs; task subjects are tasks (mimo-weak-tests-v1 has both)."""
    return (row.get("subject") or {}).get("kind") == "trace"


def _actor_transcript(
    key_id: str, row: Mapping[str, Any], view: KeyView, key_dir: Path, results: Path
) -> bool | None:
    """Whether the run's own steps exist locally; ``None`` for a task subject."""
    if not _is_run(row):
        return None
    if key_id == "trajectory-judge-400":
        return (key_dir / "upstream" / "trajectories.jsonl").is_file()
    if (row.get("subject") or {}).get("task_id") == BOUNTYBENCH_TASK:
        return False  # quarantined: no supported actor role
    source: Mapping[str, Any] = row
    if _reference_path(row) is None:  # a run keyed elsewhere (trace-paired-v1)
        source = view.trace_paired.get(str((row.get("subject") or {}).get("trial_id")), row)
    if source.get("has_atif") is False:
        return False  # oracle / scripted-attack trial: no actor steps
    return _trajectory_exists(_reference_path(source), results)


def _harness_selector(
    key_id: str, row: Mapping[str, Any], split: str, results: Path
) -> str | None:
    if key_id != "cheatbench-actions-v1" or split != "DEV":
        return None
    if (row.get("subject") or {}).get("task_id") == BOUNTYBENCH_TASK:
        return None
    return CHEATBENCH_SELECTOR if _trajectory_exists(_reference_path(row), results) else None


def _task_id(row: Mapping[str, Any]) -> str | None:
    task = (row.get("subject") or {}).get("task_id")
    if not task or str(task).startswith(BENCHMARK_CARRIER_PREFIX):
        return None
    return str(task)


def _qualified(key_id: str, item_id: str) -> str:
    return item_id if item_id.startswith(f"{key_id}:") else f"{key_id}:{item_id}"


def _note(*parts: str | None) -> str | None:
    text = "; ".join(part for part in parts if part)
    return text[:NOTE_LIMIT] or None


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    root = ctx.repo_root / KEYS_RELATIVE
    _check_keys(root)
    results = results_root()
    trace_paired = {
        str((row.get("subject") or {}).get("trial_id")): row
        for row in _read_rows(root / TRACE_PAIRED / "items.jsonl")
    }
    for key_id, spec in KEYS.items():
        items_path = root / key_id / "items.jsonl"
        rows = _read_rows(items_path)
        view = KeyView(key_id, rows, {str(r.get("item_id")): r for r in rows}, trace_paired)
        digest = _sha256(items_path)
        for row in rows:
            decision = spec.decide(row, view)
            if decision is None:
                continue
            item_id = str(row["item_id"])
            subject = row.get("subject") or {}
            task_id = _task_id(row)
            declared = str(row["split"])
            split = ctx.strict_split(
                declared, key_id=key_id, item_id=item_id, task_id=task_id,
                task_digest=subject.get("task_digest"),
            )
            oq6 = (
                key_id == "tb4-audit-v1" and row.get("scope") == "task" and split == "DEV"
            )
            yield new_item(
                source=SOURCE,
                kind=decision.kind,
                polarity=decision.polarity,
                label_type=decision.label_type,
                label=decision.label,
                granularity="run" if _is_run(row) else "task",
                subject_type="run" if _is_run(row) else "task",
                subject_id=_qualified(key_id, item_id),
                task_id=task_id,
                declared_split=declared,
                split=split,
                license=spec.license,
                actor_transcript=_actor_transcript(key_id, row, view, root / key_id, results),
                harness_selector=_harness_selector(key_id, row, split, results),
                provenance=Provenance(
                    store=f"answer-keys:{key_id}",
                    ref=item_id,
                    digest=digest,
                    note=_note(OQ6_NOTE if oq6 else None, decision.note),
                ),
            )


def oq6_keyed(items: list[IssueItem]) -> tuple[int, int]:
    """(annotations, distinct tasks) among items carrying the ``oq6-keyed`` mark."""
    marked = [i for i in items if (i.provenance.note or "").startswith(OQ6_NOTE)]
    return len(marked), len({i.task_id for i in marked})
