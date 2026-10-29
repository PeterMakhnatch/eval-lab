"""Rank the MiMo clean-set rows that still lack cheat-ladder evidence (HAR-224).

Read-only over tracked receipts: nothing here builds, runs or grades a task.

The card named `mimo-clean-v5`, but the current corpus is `mimo-clean-v6`
(`docs/mimo/README.md`): the #924 digest refresh rebound every v5 grade to
`unverified:package-changed`, and v6 ships zero ladder cells of its own. This
module therefore ranks the live v6 manifest and carries the v5 label alongside
as prior-generation *context*, never as current-digest evidence.

    uv run python research/experiments/mimo-clean-v5-coverage/rank.py
    uv run python research/experiments/mimo-clean-v5-coverage/rank.py --check

`--check` compares the committed CSV against a fresh computation and exits 1 on
drift, so a manifest refresh that silently invalidates the ranking is caught.

Rows are not one queue. A row that produced a proven defect on an earlier digest,
a row that carries an answer inside its image, and a row nobody has ever looked
at have different priors, and the phase ordering encodes exactly that.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
EXPERIMENTS = REPO_ROOT / "research" / "experiments"
CORPUS = EXPERIMENTS / "mimo-clean-v6" / "manifest.csv"
OVERRIDES = EXPERIMENTS / "mimo-clean-v5-census" / "verify-overrides.csv"
CONTROLS = EXPERIMENTS / "mimo-clean-v6" / "pending-controls.json"
CENSUS_FILL = EXPERIMENTS / "mimo-clean-v5-census" / "fill_verify.py"
EXCLUDED_MARKER = "excluded-from-training"
CSV_PATH = HERE / "unverified-ranking.csv"

# Bulk sample over the reference-bearing phase: the stratum the E1 class lives in.
BULK_SAMPLE_N = 150
BULK_SAMPLE_SEED = 20261029
BULK_MIN_PER_STRATUM = 5

COLUMNS = (
    "rank",
    "phase",
    "stratum",
    "task_id",
    "language",
    "verify",
    "reference_fix",
    "prior_v5_label",
    "selection_group",
    "pending_control_cells",
    "mechanism_gap",
    "sample_draw",
    "why",
)


@dataclass(frozen=True)
class Phase:
    """One ordered work queue: what its rows already tell us, and why it comes here."""

    key: str
    gap: str
    why: str


PHASE_LEAK_PRIOR = Phase(
    "P0-proven-leak-prior",
    "a leak or grader defect was demonstrated on an earlier digest",
    "the mechanism is known and only the package changed; re-running the ladder is the cheapest "
    "way to learn whether the rebuild closed it",
)
PHASE_DEFECT_PRIOR = Phase(
    "P1-proven-defect-prior",
    "a non-leak defect was demonstrated on an earlier digest",
    "task fairness or environment failure was already proven; a pass here is not yet interpretable",
)
PHASE_SELECTION = Phase(
    "P2-frozen-selection-controls",
    "the frozen v6 selection still owes runnable control cells",
    "these tasks were already chosen as the acceptance set; finish them before opening new work",
)
PHASE_REFERENCE = Phase(
    "P3-reference-bearing",
    "an answer exists in the image and no ladder cell exists on this digest",
    "the E1 class: a fix present anywhere in the image is the corpus's dominant leak vector",
)
PHASE_REFERENCE_LESS = Phase(
    "P4-reference-less",
    "no reference fix, so no answer to copy and no oracle cell",
    "E1 is inapplicable here; only the V-checks and the ladder apply, at full per-task cost",
)

PHASE_ORDER = (
    PHASE_LEAK_PRIOR,
    PHASE_DEFECT_PRIOR,
    PHASE_SELECTION,
    PHASE_REFERENCE,
    PHASE_REFERENCE_LESS,
)
RANK_OF = {phase.key: index for index, phase in enumerate(PHASE_ORDER)}

LEAK_PRIOR_MARKERS = (
    "fail:open-leak",
    "fail:grader-hole",
    "fail:vacuous-grader",
    EXCLUDED_MARKER,
)
DEFECT_PRIOR_MARKERS = ("fail:unfair-task", "env-broken")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_manifest(path: Path = CORPUS) -> list[dict[str, str]]:
    return read_csv(path)


def prior_labels(overrides: Path = OVERRIDES) -> dict[str, str]:
    """Prior-generation defect per task, used as context only, never as current evidence.

    The v5 manifest's own `verify` column was rebound to `unverified:package-changed`
    by the #924 digest refresh, so the surviving statements about earlier digests are
    the census override triage (digest-bound) and the frozen selection's recorded
    `prior_generation_context`. Only proven defects are kept; an `unverified:*` prior
    says nothing a current-digest run does not say better.
    """
    labels: dict[str, str] = {}
    if overrides.exists():
        for row in read_csv(overrides):
            verify = row["verify"]
            if verify.startswith("fail:") or verify == "env-broken":
                labels[row["task_id"]] = verify
    if CONTROLS.exists():
        payload = json.loads(CONTROLS.read_text(encoding="utf-8"))
        for task in payload.get("tasks", []):
            context = task.get("prior_generation_context")
            if context and task["task_id"] not in labels:
                labels[task["task_id"]] = str(context)
    labels.update(_census_marks())
    return labels


def _census_marks(path: Path = CENSUS_FILL) -> dict[str, str]:
    """Defect marks the census fill step hard-codes, read from it instead of copied.

    `NODE_BUILD_LEAK` is the pair kept on `purge-build-caches@4` because `@5` fails
    closed on them; no manifest column or override row carries that state.
    """
    if not path.exists():
        return {}
    try:
        module = runpy.run_path(str(path))
    except Exception:  # a receipt script is allowed to change shape; then we lose the marks
        return {}
    return {
        task: "fail:open-leak:node-build(kept @4 after @5 setup rc=1)"
        for task in module.get("NODE_BUILD_LEAK") or ()
    }


def control_state(path: Path = CONTROLS) -> dict[str, dict[str, object]]:
    """Frozen-selection membership and outstanding control cells per task."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    state: dict[str, dict[str, object]] = {}
    for task in payload.get("tasks", []):
        state[task["task_id"]] = {
            "groups": task.get("groups") or [],
            "pending": len(task.get("pending_agents") or []),
            "runnable": bool(task.get("runnable")),
        }
    return state


def has_verdict(row: dict[str, str]) -> bool:
    """A terminal census grade: a pass, or a classified failure."""
    verify = row["verify"]
    return verify == "pass" or verify.startswith("fail:")


def phase_for(row: dict[str, str], prior: str, control: dict[str, object] | None) -> Phase:
    if prior:
        if any(prior.startswith(marker) or marker in prior for marker in LEAK_PRIOR_MARKERS):
            return PHASE_LEAK_PRIOR
        if any(marker in prior for marker in DEFECT_PRIOR_MARKERS):
            return PHASE_DEFECT_PRIOR
    if control and control["pending"] and control["runnable"]:
        return PHASE_SELECTION
    if row["reference_fix"] != "none":
        return PHASE_REFERENCE
    return PHASE_REFERENCE_LESS


def _draw_order(task_id: str) -> str:
    """Version-stable deterministic draw order (never `random`, which can change)."""
    return hashlib.sha256(f"{BULK_SAMPLE_SEED}:{task_id}".encode()).hexdigest()


def largest_remainder(
    sizes: dict[str, int], total: int, minimum: int = BULK_MIN_PER_STRATUM
) -> dict[str, int]:
    """Allocate `total` across strata proportionally, with a floor per stratum.

    Strata smaller than `minimum` are taken whole. The floor is applied first and
    the remaining budget splits by largest fractional remainder, so the
    allocation is exact and independent of dict order.
    """
    if total > sum(sizes.values()):
        raise ValueError("sample larger than the population")
    allocation = {name: 0 for name in sizes}
    small = {name: size for name, size in sizes.items() if size < minimum}
    for name, size in small.items():
        allocation[name] = size
    budget = total - sum(allocation.values())
    eligible = {name: size for name, size in sizes.items() if name not in small}
    if budget < 0:
        raise ValueError("floor already exceeds the sample size")
    if not eligible:
        return allocation
    quota = {name: size * budget / sum(eligible.values()) for name, size in eligible.items()}
    for name, share in quota.items():
        allocation[name] = int(share)
    remainder = budget - sum(allocation[name] for name in eligible)
    ranked = sorted(eligible, key=lambda name: (-(quota[name] - int(quota[name])), name))
    for name in ranked[:remainder]:
        allocation[name] += 1
    return allocation


def bulk_sample(rows: list[dict[str, str]], n: int = BULK_SAMPLE_N) -> set[str]:
    """Draw `n` reference-bearing tasks, stratified by language, in a fixed order."""
    by_language: dict[str, list[str]] = {}
    for row in rows:
        by_language.setdefault(row["language"], []).append(row["task_id"])
    sizes = {name: len(ids) for name, ids in by_language.items()}
    if not sizes:
        return set()
    allocation = largest_remainder(sizes, min(n, sum(sizes.values())))
    chosen: set[str] = set()
    for language, count in allocation.items():
        ordered = sorted(by_language[language], key=_draw_order)
        chosen.update(ordered[:count])
    return chosen


def prior_for(row: dict[str, str], labels: dict[str, str]) -> str:
    """Prior-generation defect for one row.

    The manifest's own `excluded-from-training` reason is a census statement that the
    package stays answer-visible, so it outranks a generic prior when none is recorded.
    """
    label = labels.get(row["task_id"], "")
    if EXCLUDED_MARKER in row.get("reason", "") and not label.startswith(("fail:", "env-broken")):
        return f"{EXCLUDED_MARKER}(manifest reason)"
    return label


def build_rows(
    manifest: list[dict[str, str]] | None = None,
    prior: dict[str, str] | None = None,
    controls: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, str]]:
    """Every built row still lacking a complete census verdict, ranked by phase."""
    manifest = read_manifest() if manifest is None else manifest
    prior = prior_labels() if prior is None else prior
    controls = control_state() if controls is None else controls

    pending: list[dict[str, str]] = []
    for row in manifest:
        if row["status"] != "built" or has_verdict(row):
            continue
        pending.append(row)

    prior_by_task = {row["task_id"]: prior_for(row, prior) for row in pending}
    phases = {
        row["task_id"]: phase_for(row, prior_by_task[row["task_id"]], controls.get(row["task_id"]))
        for row in pending
    }
    reference_pool = [row for row in pending if phases[row["task_id"]] is PHASE_REFERENCE]
    draw = bulk_sample(reference_pool)

    language_counts: dict[str, int] = {}
    for row in pending:
        language_counts[row["language"]] = language_counts.get(row["language"], 0) + 1

    def order(row: dict[str, str]) -> tuple[int, int, str]:
        return (
            RANK_OF[phases[row["task_id"]].key],
            -language_counts[row["language"]],
            row["task_id"],
        )

    ranked: list[dict[str, str]] = []
    for rank, row in enumerate(sorted(pending, key=order), start=1):
        phase = phases[row["task_id"]]
        control = controls.get(row["task_id"]) or {}
        ranked.append(
            {
                "rank": str(rank),
                "phase": phase.key,
                "stratum": row["language"],
                "task_id": row["task_id"],
                "language": row["language"],
                "verify": row["verify"],
                "reference_fix": "yes" if row["reference_fix"] != "none" else "no",
                "prior_v5_label": prior_by_task[row["task_id"]],
                "selection_group": "+".join(control.get("groups") or []),
                "pending_control_cells": str(control.get("pending", "")),
                "mechanism_gap": phase.gap,
                "sample_draw": "yes" if row["task_id"] in draw else "",
                "why": phase.why,
            }
        )
    return ranked


def render(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed CSV matches the receipts and exit 1 on drift",
    )
    args = parser.parse_args(argv)
    rendered = render(build_rows())
    if args.check:
        committed = CSV_PATH.read_text(encoding="utf-8") if CSV_PATH.exists() else ""
        if committed != rendered:
            print(f"{CSV_PATH} is stale: regenerate it from the receipts", file=sys.stderr)
            return 1
        print(f"{CSV_PATH.name} matches the receipts")
        return 0
    CSV_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {CSV_PATH} ({rendered.count(chr(10)) - 1} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
