"""Known grader repairs, one ``grader_repair`` item per before/after pair.

An item is the *pair*: the "before" grader is the weak one (polarity is always
``positive``: the weakness is present in the before side). What was checked
about the "after" side is the validation status, carried in ``label``:

``static_only``
    Authored and reviewed, never executed against its package (``label_type``
    ``static``).
``native_partial``
    Executed natively, but short of qualification: reference-oracle controls
    only, or a retained-execution witness without a full differential
    (``label_type`` ``executed``).
``native_validated``
    Native oracle pass, nop fail and a wrong-pass/shortcut differential all
    executed (``label_type`` ``executed``).

Sets, each keyed so the same pair never appears twice:

``ts9`` / ``ts11``
    The 16 static defensive repairs merged as PRs #1325-#1332 and #1343-#1349,
    #1351 (8 + 8). Before/after are the parent and variant *package* digests of
    ``library/task-variants/<task>/<digest12>.json``. Their native evidence is the
    TS-14 Modal reference-oracle matrix (``ts14-native-validation/summary.json``,
    PR #1403): both packages' oracle was executed, no historical incorrect
    submission was bound, so none is ``native_validated`` and each is at most
    ``native_partial``. Without a receipt that matches the variant's digests the
    status stays ``static_only``.
``cloud-variant``
    Variants of ``corpus_tasks`` in the cloud trace DB whose
    ``original_grader_digest`` and ``repaired_grader_digest`` are both set and
    differ (grader-manifest digests). These carry the older mimo30, kimi-tb4,
    tb2-to-tb21 and muse repair sets. Variants whose two digests are equal are no
    repair and are counted in :func:`survey`, not itemized.
``mimo-clean-v6``
    ``research/experiments/mimo-clean-v6-repaired``: 16 EnvCheck repairs ported
    onto v6 parents (parent digest -> ported variant digest). Only the ones with
    completed oracle x3, nop x3 and killed-mutant replay are ``native_validated``.

Experiments that cannot be itemized (no task+before+after grader identity in the
repository) are listed in :data:`SKIPPED_SETS` with the reason.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    IssueItem,
    LabelType,
    Provenance,
    SourceSummary,
    new_item,
)
from evallab.failure_library.records import Split

SOURCE = "grader-repairs"

TS14_SUMMARY_ENV = "EVALLAB_TS14_NATIVE_SUMMARY"
_DEFAULT_TS14_SUMMARY = (
    "Developer/research-context/bridge/2026-10-27-run-atlas/ts14-native-validation/summary.json"
)
_CLOUD_STORE = "cloud-trace-db:corpus_tasks"
_V6_DIR = Path("research/experiments/mimo-clean-v6-repaired")

STATIC_ONLY = "static_only"
NATIVE_PARTIAL = "native_partial"
NATIVE_VALIDATED = "native_validated"

# Most restrictive first: a repair is only as DEV as its least DEV identity.
_SPLIT_RESTRICTION: tuple[Split, ...] = ("TEST", "heldout", "cross_test", "unassigned", "DEV")
_DECLARED: dict[str, str] = {
    "dev": "DEV",
    "test": "TEST",
    "held": "heldout",
    "heldout": "heldout",
    "cross_test": "cross_test",
    "unassigned": "unassigned",
}
_FAMILY_PREFIX = "mimo-v2.6-rl/"
_NATIVE_WITNESS = "retained-native-execution-and-witness"
_QUALIFIED_SCOPE = "reference_validated_weak_test_and_repair_qualification"

SKIPPED_SETS: dict[str, str] = {
    "tb21-test-repairs-20261019": (
        "3 task patches plus a mutation-survival report per task; no before or after grader "
        "digest in the repository (the tb2-to-tb21 variants that carry digests are in cloud-variant)"
    ),
    "envcheck-repair-2026-10-15": (
        "8 repaired variants have an after digest and local-control receipts, but the before "
        "(parent) digest lives only in machine-local variant packages, not in repository files"
    ),
    "har177-repair": (
        "one strip-future-history pilot task: an environment git-history transform, "
        "not a grader repair, and a single pair"
    ),
    "test-repair-performance-20261017": (
        "analysis scripts only; no per-repair table of tasks and before/after identities "
        "is kept in the repository"
    ),
}

# (pull request, squash-merge short sha, variant record) for the 16 merged static repairs.
_TS_REPAIRS: tuple[tuple[int, str, str, str], ...] = (
    (1325, "382167485", "ts9", "terminal-bench__gsea-proteomics/b130e22853e2"),
    (1326, "7048a1372", "ts9", "terminal-bench__heat-pump-warranty/df63e7b3d43d"),
    (1327, "16561e016", "ts9", "mimo-v2.6-rl__format-code-task-002609/6d9563e42b9e"),
    (1328, "5d8168a47", "ts9", "mimo-v2.6-rl__format-code-task-001338/e6540599550b"),
    (1329, "0f1f86578", "ts9", "mimo-v2.6-rl__format-code-task-000232/fb00505ebac8"),
    (1330, "4c3d9fb8f", "ts9", "mimo-v2.6-rl__format-code-task-000847/8469c93fb1bf"),
    (1331, "3e0c1f1d4", "ts9", "mimo-v2.6-rl__format-code-task-000322/4be0f9507c0e"),
    (1332, "38de8dfbf", "ts9", "mimo-v2.6-rl__format-code-task-000079/80076e8747b2"),
    (1343, "bc5840f74", "ts11", "mimo-v2.6-rl__format-code-task-001426/81ba37e6d91b"),
    (1344, "94f8db015", "ts11", "mimo-v2.6-rl__format-code-task-000945/c79b63a54b21"),
    (1345, "a2555fb79", "ts11", "mimo-v2.6-rl__format-code-task-000682/21b5ce668f6b"),
    (1346, "b35b13727", "ts11", "mimo-v2.6-rl__format-code-task-001431/e1e5ae928f08"),
    (1347, "95c43562a", "ts11", "mimo-v2.6-rl__format-code-task-002160/189d482a8b64"),
    (1348, "62acdd41b", "ts11", "mimo-v2.6-rl__format-code-task-000872/96a48698220c"),
    (1349, "5e6e7aefb", "ts11", "mimo-v2.6-rl__format-code-task-001514/248af341c98b"),
    (1351, "3518a3807", "ts11", "mimo-v2.6-rl__format-code-task-002535/dc220af666eb"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _short(digest: str) -> str:
    return digest.removeprefix("sha256:")[:16]


def _task_id(task_name: str) -> str:
    """The cloud ``corpus_tasks`` spelling: the MiMo family prefix is dropped."""
    return task_name.removeprefix(_FAMILY_PREFIX)


def _subject_id(task_id: str, before: str, after: str) -> str:
    return f"{task_id}@{_short(before)}->{_short(after)}"


def _declared(text: str | None) -> str:
    return _DECLARED.get((text or "").lower(), "unassigned")


def _effective_split(
    ctx: BankContext,
    declared: str,
    task_id: str,
    digests: tuple[str | None, ...],
    key_ids: tuple[str, ...] = (),
) -> Split:
    """Most restrictive strict split over the task name and every package identity."""
    results: list[Split] = []
    for key_id in key_ids or (None,):
        for digest in (None, *digests):
            results.append(
                ctx.strict_split(
                    declared, key_id=key_id, item_id=task_id, task_id=task_id, task_digest=digest
                )
            )
    return min(results, key=_SPLIT_RESTRICTION.index)


def _label_type(status: str) -> LabelType:
    return "static" if status == STATIC_ONLY else "executed"


def _cloud_connection(ctx: BankContext) -> sqlite3.Connection | None:
    if not ctx.cloud_db.exists():
        return None
    connection = sqlite3.connect(f"file:{ctx.cloud_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _cloud_splits(ctx: BankContext) -> dict[str, str]:
    """``task_id -> declared split`` of every cloud corpus task (empty without the DB)."""
    connection = _cloud_connection(ctx)
    if connection is None:
        return {}
    try:
        return {
            row["task_id"]: _declared(row["split"])
            for row in connection.execute("SELECT task_id, split FROM corpus_tasks")
        }
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# TS-9 / TS-11 static defensive repairs (+ TS-14 native evidence)
# --------------------------------------------------------------------------------------


def ts14_summary_path() -> Path:
    return Path(os.environ.get(TS14_SUMMARY_ENV) or Path.home() / _DEFAULT_TS14_SUMMARY)


def _load_ts14_cases() -> tuple[dict[int, dict[str, Any]], str | None]:
    """``pr -> case`` and the sha256 of the summary file; ``({}, None)`` when absent."""
    path = ts14_summary_path()
    if not path.is_file():
        return {}, None
    summary = json.loads(path.read_text(encoding="utf-8"))
    cases = {case["pr"]: case for case in summary.get("cases", []) if "pr" in case}
    return cases, _sha256(path)


def _reward(side: Any) -> float | None:
    if not isinstance(side, dict) or side.get("status") != "measured":
        return None
    reward = side.get("reward")
    return float(reward) if isinstance(reward, int | float) else None


def _native_state(
    case: dict[str, Any] | None, parent_digest: str, variant_digest: str
) -> tuple[str, str]:
    """``(status, detail)`` from a TS-14 case that is bound to this exact digest pair."""
    if case is None:
        return STATIC_ONLY, "no native receipt"
    original, repaired = case.get("oracle_original"), case.get("oracle_repaired")
    bound = (
        isinstance(original, dict)
        and isinstance(repaired, dict)
        and original.get("task_digest") == parent_digest
        and repaired.get("task_digest") == variant_digest
    )
    if not bound:
        return STATIC_ONLY, "native receipt not bound to this digest pair"
    before, after = _reward(original), _reward(repaired)
    if before is None or after is None:
        return STATIC_ONLY, "native receipt has an unmeasured side"
    detail = f"native oracle original={before:g} repaired={after:g}"
    if after < 1.0:
        detail += " (repaired reference rejected)"
    if case.get("native_qualification") is True:
        return NATIVE_VALIDATED, detail
    return NATIVE_PARTIAL, detail + "; no historical negative bound"


def _ts_items(ctx: BankContext, cloud_splits: dict[str, str]) -> Iterator[IssueItem]:
    cases, summary_digest = _load_ts14_cases()
    for pr, merge, mission, record in _TS_REPAIRS:
        path = ctx.repo_root / "library/task-variants" / f"{record}.json"
        variant = json.loads(path.read_text(encoding="utf-8"))
        parent_digest = variant["parent"]["digest"]
        variant_digest = variant["variant_digest"]
        task_id = _task_id(variant["task_name"])
        status, detail = _native_state(cases.get(pr), parent_digest, variant_digest)
        assignments = variant.get("inputs", {}).get("dev_assignments") or []
        key_ids = tuple(key for key, split in assignments if split == "dev")
        stated = "dev" if assignments and len(key_ids) == len(assignments) else None
        declared = _declared(stated) if stated else cloud_splits.get(task_id, "unassigned")
        note = f"{mission} {variant['transform']}; merged {merge}; {detail}"
        if summary_digest and status != STATIC_ONLY:
            note += f"; ts14 summary sha256:{summary_digest[:12]}"
        yield new_item(
            source=SOURCE,
            kind="grader_repair",
            polarity="positive",
            label_type=_label_type(status),
            label=f"{mission}:{status}"
            + (" (repaired reference rejected)" if "rejected" in detail else ""),
            granularity="repair_pair",
            subject_type="repair",
            subject_id=_subject_id(task_id, parent_digest, variant_digest),
            task_id=task_id,
            locator=str(pr),
            declared_split=declared,
            split=_effective_split(
                ctx, declared, task_id, (parent_digest, variant_digest), key_ids
            ),
            actor_transcript=None,
            harness_selector=None,
            provenance=Provenance(
                store=f"git:pr/{pr}",
                ref=f"library/task-variants/{record}.json",
                digest=_sha256(path),
                note=note[:300],
            ),
        )


# --------------------------------------------------------------------------------------
# Cloud corpus_tasks variants (grader-manifest digests)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CloudVariant:
    task_id: str
    dataset_id: str
    split: str
    variant: dict[str, Any]
    row_pair: bool


def _cloud_variants(ctx: BankContext) -> Iterator[_CloudVariant]:
    connection = _cloud_connection(ctx)
    if connection is None:
        return
    try:
        query = (
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
            "metadata_json FROM corpus_tasks WHERE metadata_json LIKE '%\"variants\"%' "
            "OR (original_grader_digest <> '' AND repaired_grader_digest <> '') ORDER BY task_id"
        )
        for row in connection.execute(query):
            try:
                metadata = json.loads(row["metadata_json"])
            except ValueError:
                continue
            variants = metadata.get("variants") if isinstance(metadata, dict) else None
            row_pair = bool(row["original_grader_digest"] and row["repaired_grader_digest"])
            for variant in variants or []:
                if isinstance(variant, dict):
                    yield _CloudVariant(
                        row["task_id"], row["dataset_id"], row["split"], variant, row_pair
                    )
    finally:
        connection.close()


def _cloud_status(variant: dict[str, Any]) -> str:
    scope = variant.get("confirmation_scope")
    if (
        variant.get("scope") == _QUALIFIED_SCOPE
        and isinstance(scope, dict)
        and scope.get("repair_qualified") is True
    ):
        return NATIVE_VALIDATED
    purposes = {
        evidence.get("purpose")
        for evidence in variant.get("evidence") or []
        if isinstance(evidence, dict)
    }
    return NATIVE_PARTIAL if _NATIVE_WITNESS in purposes else STATIC_ONLY


def _cloud_pair(variant: dict[str, Any]) -> tuple[str, str] | None:
    before, after = variant.get("original_grader_digest"), variant.get("repaired_grader_digest")
    if isinstance(before, str) and isinstance(after, str) and before and after and before != after:
        return before, after
    return None


def _cloud_items(ctx: BankContext) -> Iterator[IssueItem]:
    for found in _cloud_variants(ctx):
        pair = _cloud_pair(found.variant)
        if pair is None:
            continue
        before, after = pair
        variant = found.variant
        status = _cloud_status(variant)
        source = str(variant.get("source") or "unknown")
        admission = variant.get("admission")
        digests: list[str | None] = [
            variant.get("original_package_digest"),
            variant.get("repaired_package_digest"),
        ]
        if isinstance(admission, dict):
            digests.append(admission.get("source_task_digest"))
        declared = _declared(found.split)
        variant_id = str(variant.get("variant_id") or f"{source}:{_short(after)}")
        yield new_item(
            source=SOURCE,
            kind="grader_repair",
            polarity="positive",
            label_type=_label_type(status),
            label=f"cloud-variant:{source}:{status}",
            granularity="repair_pair",
            subject_type="repair",
            subject_id=_subject_id(found.task_id, before, after),
            task_id=found.task_id,
            locator=variant_id,
            declared_split=declared,
            split=_effective_split(
                ctx,
                declared,
                found.task_id,
                tuple(d for d in digests if isinstance(d, str)),
                (found.dataset_id,),
            ),
            actor_transcript=None,
            harness_selector=None,
            provenance=Provenance(
                store=_CLOUD_STORE,
                ref=f"{found.task_id}#{variant_id}",
                note=f"dataset {found.dataset_id}; scope {variant.get('scope')}; grader-manifest digests"[
                    :300
                ],
            ),
        )


# --------------------------------------------------------------------------------------
# mimo-clean-v6-repaired (EnvCheck repairs ported onto v6 parents)
# --------------------------------------------------------------------------------------


def _v6_validated(row: dict[str, str]) -> bool:
    try:
        oracle, nop = json.loads(row["oracle_x3"]), json.loads(row["nop_x3"])
    except ValueError:
        return False
    return (
        row["oracle_status"] == "ok"
        and row["nop_status"] == "ok"
        and oracle == [1.0, 1.0, 1.0]
        and nop == [0.0, 0.0, 0.0]
        and row["kill_replay"].startswith("kill-replay:pass")
    )


def _v6_items(ctx: BankContext, cloud_splits: dict[str, str]) -> Iterator[IssueItem]:
    base = ctx.repo_root / _V6_DIR
    ports_path, results_path = base / "ports.csv", base / "results.csv"
    if not ports_path.is_file() or not results_path.is_file():
        return
    with results_path.open(newline="", encoding="utf-8") as handle:
        results = {row["task"]: row for row in csv.DictReader(handle)}
    ports_digest = _sha256(ports_path)
    with ports_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            task_id = row["task"]
            before, after = row["v6_parent_digest"], row["repaired_variant"]
            result = results.get(task_id)
            validated = result is not None and _v6_validated(result)
            status = NATIVE_VALIDATED if validated else STATIC_ONLY
            declared = cloud_splits.get(task_id, "unassigned")
            yield new_item(
                source=SOURCE,
                kind="grader_repair",
                polarity="positive",
                label_type=_label_type(status),
                label=f"mimo-clean-v6:{status}",
                granularity="repair_pair",
                subject_type="repair",
                subject_id=_subject_id(task_id, before, after),
                task_id=task_id,
                locator="mimo-clean-v6-repaired",
                declared_split=declared,
                split=_effective_split(
                    ctx, declared, task_id, (before, after, row["old_variant_digest"])
                ),
                actor_transcript=None,
                harness_selector=None,
                provenance=Provenance(
                    store="research-experiments:mimo-clean-v6-repaired",
                    ref=f"ports.csv#{task_id}",
                    digest=ports_digest,
                    note=(
                        f"source disposition {row['disposition']}; "
                        + (
                            "oracle x3 pass, nop x3 fail, killed-mutant replay pass"
                            if validated
                            else "port lineage-valid; controls not run"
                        )
                    )[:300],
                ),
            )


# --------------------------------------------------------------------------------------


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    cloud_splits = _cloud_splits(ctx)
    yield from _ts_items(ctx, cloud_splits)
    yield from _cloud_items(ctx)
    yield from _v6_items(ctx, cloud_splits)


def cloud_correspondence(ctx: BankContext) -> dict[str, Any]:
    """Whether the cloud grader-digest pairs line up with the merged TS-9/TS-11 repairs.

    Cloud rows hold *grader-manifest* digests; the TS variants hold *package*
    digests. They correspond through the cloud variant's ``original_package_digest``
    (the before side) and ``repaired_package_digest`` (the after side).
    """
    parents: dict[str, tuple[int, str]] = {}
    for pr, _merge, _mission, record in _TS_REPAIRS:
        path = ctx.repo_root / "library/task-variants" / f"{record}.json"
        variant = json.loads(path.read_text(encoding="utf-8"))
        parents[variant["parent"]["digest"]] = (pr, variant["variant_digest"])
    rows_both: set[str] = set()
    variants: Counter[str] = Counter()
    before_hit: set[int] = set()
    after_hit: set[int] = set()
    for found in _cloud_variants(ctx):
        variant = found.variant
        has_both = bool(
            variant.get("original_grader_digest") and variant.get("repaired_grader_digest")
        )
        if has_both:
            rows_both.add(found.task_id)
            variants["both_set"] += 1
            variants["differ" if _cloud_pair(variant) else "equal"] += 1
        parent = variant.get("original_package_digest")
        if has_both and parent in parents:
            pr, ts_variant = parents[parent]
            before_hit.add(pr)
            if variant.get("repaired_package_digest") == ts_variant:
                after_hit.add(pr)
    return {
        "cloud_rows_with_both_digests": len(rows_both),
        "cloud_variants": dict(variants),
        "ts_repairs": len(parents),
        "ts_before_side_matches_cloud_original": len(before_hit),
        "ts_after_side_matches_cloud_repaired": len(after_hit),
    }


def survey(ctx: BankContext) -> dict[str, Any]:
    """Live counts: set x validation status x split, plus the cloud correspondence."""
    by_set: Counter[tuple[str, str, str]] = Counter()
    for item in build(ctx):
        if isinstance(item, IssueItem):
            label_set, _, rest = item.label.partition(":")
            status = rest.rsplit(":", 1)[-1].split(" ")[0]
            if label_set == "cloud-variant":
                label_set = f"cloud-variant/{rest.split(':')[0]}"
            by_set[(label_set, status, item.split)] += 1
    return {
        "by_set_status_split": {"|".join(key): count for key, count in sorted(by_set.items())},
        "cloud": cloud_correspondence(ctx),
        "skipped_sets": SKIPPED_SETS,
    }
