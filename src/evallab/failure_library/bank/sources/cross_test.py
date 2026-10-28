"""Issue-bank source: the staged cross-test sets of the cross-test registry.

One item per labelled subject of every staged set the registry lists
(``ctx.cross_test_registry``), read only through the existing read-only loaders:
the trace harness' ``staged:<set>`` loader for the eight run-level sets it
registers, and each set's own corpus adapter (``specs()``) for the others. Only
ids and labels leave those loaders; no transcript, task text or gold detail is
copied, and nothing is written.

Rules that shape the output:

* Every item declares ``cross_test`` and is never upgraded; the strict cross-key
  check can only keep it as is.
* ``negative`` is emitted only for sets whose registry ``false_alarm_basis``
  names a labelled clean class (``established`` or ``mixed``). Sets whose
  negatives are merely "no flag fired" (``no_flag_fired``) or absent (``none``)
  contribute positives only; unlabelled or excluded rows are never items.
* HackBench (CC BY-NC, no rehosting) is a :class:`SourceSummary` of registry
  counts and nothing else: its bundle is never opened.
* CheatBench is not in this registry; the ``cheatbench`` bank source covers it.
* Registry entries that are staged but not itemized, and the registry's held
  sets, are reported by :func:`not_itemized` and :func:`held_sets`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    Granularity,
    IssueItem,
    Kind,
    LabelType,
    Provenance,
    SourceSummary,
    SubjectType,
    new_item,
)
from evallab.production.trace_harness import actor_inputs_available
from evallab.production.trace_harness_staged import STAGED_SETS, _rows

SOURCE = "cross-test"
DECLARED_SPLIT = "cross_test"
SUMMARY_ONLY = frozenset({"hackbench"})
#: ``false_alarm_basis.negative_basis`` values that name a labelled clean class.
NEGATIVE_BASES = frozenset({"established", "mixed"})

#: Staged sets that are not itemized, and why (reported, never silently dropped).
_NOT_ITEMIZED: Mapping[str, str] = {
    "hackbench": "CC BY-NC, no rehosting: summary counts only, never items",
    "apollo-insider-trading": (
        "completion-level misalignment/deception rows: no matching issue kind and not an agent trajectory"
    ),
    "hack-ignition": (
        "single-turn completion rows (17,154 hack incl. a prompted please_hack arm): not an agent trajectory"
        " and no completion granularity"
    ),
}

_RUNS = "research/evidence/runs"
_PQ1 = f"{_RUNS}/corpus-expansion-pq1-20261027/corpora"
_PQ2 = f"{_RUNS}/corpus-expansion-pq2-20261028/corpora"
_LB11 = f"{_RUNS}/corpus-expansion-lb11-20261028/corpora"


def _hash16(native: str) -> str:
    return hashlib.sha256(native.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class _Row:
    native_id: str
    positive: bool | None  # None: considered-only or unlabelled, never an item
    label: str
    label_type: LabelType
    actor: bool | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class _Set:
    kind: Kind
    granularity: Granularity
    subject_type: SubjectType
    rows: Callable[[str, Path, BankContext], Iterator[_Row]]


# --- loaders ---------------------------------------------------------------


def _staged_rows(
    slug: str, bundle: Path, repo_root: Path
) -> Iterator[tuple[str, Mapping[str, Any], bool | None]]:
    """The trace harness' loader: ``(run id, spec, label)`` per row of a registered staged set."""
    return _rows(slug, bundle, repo_root)


_ADAPTERS: dict[Path, Any] = {}


def _load_adapter(path: Path, slug: str) -> Any:
    path = path.resolve()
    if path not in _ADAPTERS:
        if not path.is_file():
            raise FileNotFoundError(f"adapter for {slug} not found at {path}")
        name = f"evallab_bank_adapter_{slug.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load adapter {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _ADAPTERS[path] = module
    return _ADAPTERS[path]


def _adapter_specs(
    slug: str, relative: str, bundle: Path, ctx: BankContext
) -> Iterator[Mapping[str, Any]]:
    yield from _load_adapter(ctx.repo_root / relative, slug).specs(bundle)


def _actor(spec: Mapping[str, Any]) -> bool | None:
    steps = spec.get("_steps")
    return None if steps is None else actor_inputs_available(steps)


# --- per-set row mappings --------------------------------------------------


def _harness(
    positive: tuple[str, LabelType], negative: tuple[str, LabelType]
) -> Callable[[str, Path, BankContext], Iterator[_Row]]:
    """Run-level sets the trace harness registers: ids, labels and actor availability from its loader."""

    def rows(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
        prefix = f"staged:{slug}:"
        for run_id, spec, label in _staged_rows(slug, bundle, ctx.repo_root):
            if label is None:
                continue
            text, label_type = positive if label else negative
            yield _Row(run_id.removeprefix(prefix), label, text, label_type, _actor(spec))

    return rows


def _sleight(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    for spec in _adapter_specs(slug, f"{_PQ1}/safety-research__sleight-bench/tools/adapter.py", bundle, ctx):
        attack = bool(spec["positive"])
        yield _Row(
            spec["trace_key"], attack,
            "publisher attack segment" if attack else "publisher benign counterpart segment",
            "construction", _actor(spec),
        )


def _paichecker(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    for spec in _adapter_specs(slug, f"{_LB11}/manyi-w__paichecker/tools/adapter.py", bundle, ctx):
        identity, gold = spec["identity"], spec["gold"]
        families = "+".join(gold["families"])
        yield _Row(
            f"{identity['sub_corpus']}:{identity['instance_id']}", bool(gold["positive"]),
            f"misalignment: {families}" if gold["positive"] else "No Misalignment",
            "human", None, identity["instance_id"],
        )


def _automationbench(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    adapter = f"{_LB11}/parsewave__automationbench-verified/tools/adapter.py"
    for spec in _adapter_specs(slug, adapter, bundle, ctx):
        labels = spec["labels"]
        verified = labels["label_state"] == "defective_verified"
        yield _Row(
            spec["identity"]["task_name"], True if verified else None,
            f"defective_verified: {labels['issue_group']}", "human", None, spec["identity"]["task_name"],
        )


def _mistrace(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    for spec in _adapter_specs(slug, f"{_PQ2}/ameek__mistrace/tools/adapter.py", bundle, ctx):
        state = spec["gold"]["label_state"]
        if not state.startswith("positive_"):
            continue  # negatives are "both channels false": not certified clean, so not items
        success = state == "positive_success"
        yield _Row(
            _hash16(spec["identity"]["run_key"]), True,
            "reward hack: execution success and judge attempt" if success else "reward hack: judge attempt only",
            "consensus" if success else "judge", _actor(spec),
        )


def _rhb(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    adapter = f"{_PQ2}/joesbobclaw__reward-hacking-benchmark/tools/adapter.py"
    for spec in _adapter_specs(slug, adapter, bundle, ctx):
        if spec["labels"]["state"] != "positive":
            continue  # negatives are "exploit not attempted": not certified clean, so not items
        succeeded = spec["gold"]["exploit_succeeded"]
        yield _Row(
            spec["trace_key"], True,
            "exploit attempted and succeeded" if succeeded else "exploit attempted, failed",
            "executed", _actor(spec),
        )


def _assistant_axis(slug: str, bundle: Path, ctx: BankContext) -> Iterator[_Row]:
    adapter = f"{_PQ1}/arkonyx__assistant-axis-reward-hacking/tools/adapter.py"
    for spec in _adapter_specs(slug, adapter, bundle, ctx):
        if not spec["positive"]:
            continue  # "unflagged" turns are weak negatives, not judge-confirmed clean
        yield _Row(
            spec["trace_key"], True, f"judge HACK verdict ({str(spec['gold']['strength']).lower()})",
            "judge", _actor(spec),
        )


SETS: Mapping[str, _Set] = {
    "kernelbench-hard-runs": _Set("cheat", "run", "run", _harness(
        ("rubric_leak (maintainer verdict)", "human"), ("clean (maintainer verdict)", "human"))),
    "reflex-sentinel": _Set("cheat", "run", "run", _harness(
        ("test file tampered (execution oracle)", "executed"),
        ("no final test-file difference (oracle)", "executed"))),
    "cuaverifierbench": _Set("grader_wrong_pass", "run", "run", _harness(
        ("Incorrect run (blind strict-majority human outcome)", "consensus"),
        ("Correct run (blind strict-majority human outcome)", "consensus"))),
    "agent-cheat-index": _Set("cheat", "run", "run", _harness(
        ("quality-gated tamper detector fired", "detector"),
        ("no gated tamper detection (not certified honest)", "detector"))),
    "evilgenie-escalation": _Set("cheat", "run", "run", _harness(
        ("reward hacked or tampered (multi-channel, no conflict)", "consensus"),
        ("legitimate, no tamper evidence in any channel", "consensus"))),
    "sdf-shortcut-taking": _Set("cheat", "run", "run", _harness(
        ("shortcut: checker workaround detected", "executed"),
        ("no workaround (fixed or no_commit)", "executed"))),
    "researcharena-benchmark-gaming": _Set("cheat", "run", "run", _harness(
        ("benchmark-gaming side task succeeded (prompted)", "executed"),
        ("no-side-task baseline", "construction"))),
    "beam-tripwire": _Set("cheat", "run", "run", _harness(
        ("cheated: test tamper or special-casing", "detector"),
        ("no test tamper or special-casing detected", "detector"))),
    "sleight-bench": _Set("cheat", "segment", "run", _sleight),
    "paichecker": _Set("task_defect", "task", "task", _paichecker),
    "automationbench-verified": _Set("task_defect", "task", "task", _automationbench),
    "mistrace-reward-hacking": _Set("cheat", "run", "run", _mistrace),
    "rhb-delivery-queue": _Set("cheat", "run", "run", _rhb),
    "assistant-axis": _Set("cheat", "step", "run", _assistant_axis),
}


# --- registry --------------------------------------------------------------


def _registry(ctx: BankContext) -> dict[str, Any]:
    raw = json.loads(ctx.cross_test_registry.read_bytes())
    if raw.get("schema_version") != "cross-test-registry/v1":
        raise ValueError(f"unexpected cross-test registry schema {raw.get('schema_version')!r}")
    return raw  # type: ignore[no-any-return]


def held_sets(ctx: BankContext) -> dict[str, str]:
    """The registry's gated or unstaged sets (never itemized), id -> why."""
    raw = _registry(ctx)
    held = {entry["id"]: str(entry.get("why_held") or entry.get("status", "held")) for entry in raw.get("hold_access", [])}
    held.update(
        {entry["id"]: str(entry.get("why") or entry.get("status", "not staged"))
         for entry in raw.get("hold_licence_not_staged", [])}
    )
    return held


def not_itemized(ctx: BankContext) -> dict[str, str]:
    """Staged registry entries this source does not itemize, id -> why."""
    ids = {entry["id"] for entry in _registry(ctx)["entries"]}
    return {slug: why for slug, why in _NOT_ITEMIZED.items() if slug in ids}


def _provenance(entry: Mapping[str, Any], ref: str, note: str | None = None) -> Provenance:
    return Provenance(
        store=f"cross-test:{entry['id']}", ref=ref, digest=entry["manifest"]["sha256"], note=note
    )


def _summary(entry: Mapping[str, Any]) -> SourceSummary:
    counts = entry["labels"]["counts"]
    positive, negative = counts["hack_positive_records"], counts["clean_negative_records"]
    return SourceSummary(
        source=SOURCE,
        kind="cheat",
        granularity="run",
        split="cross_test",
        license=entry["licence"]["text"],
        actor_transcript=True,
        counts={"positive": positive, "negative": negative},
        reason=(
            f"{entry['id']} (CC BY-NC, no rehosting): {positive} hack trajectories "
            f"({counts['natural_records']} natural), {negative} clean; counts only, no rows stored"
        ),
        provenance=_provenance(entry, f"{entry['id']}@{entry['revision'][:12]}"),
    )


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    for entry in _registry(ctx)["entries"]:
        slug = entry["id"]
        if slug in SUMMARY_ONLY:
            yield _summary(entry)
            continue
        if slug in _NOT_ITEMIZED:
            continue
        mapping = SETS.get(slug)
        if mapping is None:
            raise ValueError(f"cross-test registry entry {slug!r} has no issue-bank mapping")
        bundle = Path(entry["bundle_path"]).expanduser()
        if not bundle.is_dir():
            raise FileNotFoundError(f"staged bundle for {slug} is not on this machine: {bundle}")
        basis = entry["labels"]["false_alarm_basis"]["negative_basis"]
        emit_negatives = basis in NEGATIVE_BASES
        selector = f"staged:{slug}" if slug in STAGED_SETS else None
        seen: set[str] = set()
        for row in mapping.rows(slug, bundle, ctx):
            if row.positive is None or (not row.positive and not emit_negatives):
                continue
            if row.native_id in seen:
                raise ValueError(f"{slug}: duplicate row id {row.native_id}")
            seen.add(row.native_id)
            yield new_item(
                source=SOURCE,
                kind=mapping.kind,
                polarity="positive" if row.positive else "negative",
                label_type=row.label_type,
                label=row.label,
                granularity=mapping.granularity,
                subject_type=mapping.subject_type,
                subject_id=f"{slug}:{row.native_id}",
                task_id=row.task_id,
                declared_split=DECLARED_SPLIT,
                split=ctx.strict_split(DECLARED_SPLIT, item_id=row.native_id, task_id=row.task_id),
                license=entry["licence"]["text"],
                actor_transcript=row.actor,
                harness_selector=selector,
                provenance=_provenance(entry, row.native_id, f"negative_basis={basis}"),
            )
