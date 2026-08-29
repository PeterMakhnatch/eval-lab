"""Build the frozen ready-for-human-labeling gold-set package.

OWNERSHIP
    Analyst owns item selection, provenance pinning, and label taxonomy.
    Tutor owns the agreement statistic, its acceptance threshold, rater
    qualification, adjudication rule, and the power argument.

HARD CONSTRAINTS
    - Emits ZERO ratings. Every rating slot is null and stays null until a
      qualified human rater fills it. This script cannot write a rating.
    - LLM judge output is never read, never imported, never accepted as gold.
    - readiness is NOT_READY until three qualified independent rater IDs exist
      for every item. The readiness function is the only gate and it fails
      closed on missing, duplicate, or unqualified rater IDs.
    - Deterministic: canonical ordering, sha256 provenance per source file,
      digest-derived sampling seed. Re-running byte-identically reproduces the
      package.

PROVENANCE KEY
    Items are keyed by (source_relpath, step_index, source_sha256).
    Trial basename is NOT unique - 29 trajectory files collapse to 26 distinct
    basenames - so basename must never be used as an identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "goldset-labeling-package/v1"
TAXONOMY_VERSION = "analyst-step-taxonomy/v1"

# ---------------------------------------------------------------------------
# Label taxonomy - grounded in what an ATIF agent step actually contains.
#
# A rater sees: the task instruction, all prior steps, this step's message,
# its tool_calls, and its observation. Every label below is answerable from
# exactly that. Labels requiring an oracle optimal path are EXCLUDED by
# design and listed in EXCLUDED_LABELS with the reason.
# ---------------------------------------------------------------------------

PRIMARY_LABEL = "step_contribution"
PRIMARY_VALUES = ("PROGRESS", "NEUTRAL", "HARMFUL", "CANNOT_JUDGE")

PRIMARY_DEFINITIONS = {
    "PROGRESS": (
        "The step moves the task closer to satisfying the stated goal: it "
        "acquires information the agent did not have, or changes state in a "
        "direction the goal requires."
    ),
    "NEUTRAL": (
        "The step neither advances nor sets back the goal. Valid exploration, "
        "re-reading already-held information, and no-op confirmations belong "
        "here."
    ),
    "HARMFUL": (
        "The step sets the task back: it destroys needed state, introduces an "
        "error the agent must later undo, or commits to a path the goal "
        "excludes."
    ),
    "CANNOT_JUDGE": (
        "The step cannot be classified from the instruction, prior steps, and "
        "this step's own content. Choosing this is a valid answer and is NOT "
        "penalised - it is a measurement of protocol coverage, not rater "
        "failure."
    ),
}

# Orthogonal facets. Kept separate rather than collapsed into the primary
# ordinal, because collapsing loses exactly the distinctions the analysis needs.
FACET_LABELS = {
    "error_response": (
        "NO_PRIOR_ERROR",
        "ACKNOWLEDGED_AND_CHANGED",
        "ACKNOWLEDGED_NOT_CHANGED",
        "IGNORED_PRIOR_ERROR",
    ),
    "abstention": ("ACTED", "DECLINED_WITH_REASON", "DECLINED_NO_REASON"),
}

FACET_DEFINITIONS = {
    "error_response": {
        "NO_PRIOR_ERROR": "No error was present in any prior step.",
        "ACKNOWLEDGED_AND_CHANGED": (
            "A prior error exists and this step's action differs from the "
            "failing action in tool, arguments, or approach."
        ),
        "ACKNOWLEDGED_NOT_CHANGED": (
            "The step references the prior error but repeats the failing "
            "action substantively unchanged."
        ),
        "IGNORED_PRIOR_ERROR": (
            "A prior error exists and the step neither references nor responds "
            "to it."
        ),
    },
    "abstention": {
        "ACTED": "The step takes an action or makes a claim.",
        "DECLINED_WITH_REASON": (
            "The step declines to act or answer AND states a reason."
        ),
        "DECLINED_NO_REASON": "The step declines to act or answer with no reason given.",
    },
}

# Mechanically decidable, included ONLY as a rater attention check. Machine
# ground truth exists, so rater-vs-machine disagreement measures rater
# attention rather than item ambiguity. It is NOT a gold label.
ATTENTION_CHECK = "repeats_prior_action_verbatim"

EXCLUDED_LABELS = {
    "step_necessity": (
        "Requires knowing the optimal path. No oracle exists for these tasks, "
        "so any label would encode the rater's guess at optimality."
    ),
    "step_efficiency": (
        "Same defect as step_necessity, plus it presumes a cost model the "
        "instruction does not state."
    ),
    "unrecoverability": (
        "Counterfactual - it quantifies over all possible continuations. "
        "Blocked on a preregistered predicate with a declared false-positive "
        "rate against later success. Not a human-labelable property."
    ),
}


@dataclass(frozen=True)
class ItemProvenance:
    source_relpath: str
    step_index: int
    source_sha256: str

    @property
    def item_id(self) -> str:
        payload = f"{self.source_relpath}#{self.step_index}#{self.source_sha256}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RatingSlot:
    """An empty rating slot. This class has no way to hold a rating value.

    Ratings are attached only by the separate human-ingest path, which requires
    a qualified rater ID. Nothing in this builder can populate one.
    """

    rater_id: None = None
    step_contribution: None = None
    error_response: None = None
    abstention: None = None
    repeats_prior_action_verbatim: None = None
    submitted_at: None = None


@dataclass(frozen=True)
class LabelItem:
    item_id: str
    provenance: dict[str, Any]
    stratum: str
    sampling_weight: float
    selection_arm: Literal["prevalence_core", "rare_cell_boost"]
    context: dict[str, Any]
    ratings: list[dict[str, Any]] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stratum_of(step: dict[str, Any], index: int, n_steps: int) -> str:
    """Label-independent stratum. Must never use anything resembling a label."""
    has_tool = "tool" if step.get("tool_calls") else "notool"
    if n_steps <= 1:
        position = "only"
    elif index == 0:
        position = "first"
    elif index >= n_steps - 1:
        position = "terminal"
    elif index < n_steps / 2:
        position = "early"
    else:
        position = "late"
    return f"{has_tool}:{position}"


def _render_context(
    steps: Sequence[dict[str, Any]], index: int, instruction: str | None
) -> dict[str, Any]:
    """Everything a rater needs, with no repository access required."""
    step = steps[index]
    prior_error = any(
        bool(s.get("observation", {}) or {}) and _looks_error(s) for s in steps[:index]
    )
    return {
        "task_instruction": instruction,
        "step_index": index,
        "total_steps": len(steps),
        "prior_steps_digest": [
            {
                "index": i,
                "source": s.get("source"),
                "tool_names": _tool_names(s),
                "had_error_signal": _looks_error(s),
            }
            for i, s in enumerate(steps[:index])
        ],
        "this_step": {
            "source": step.get("source"),
            "message": step.get("message"),
            "tool_calls": step.get("tool_calls"),
            "observation": step.get("observation"),
        },
        "machine_facts": {
            "prior_error_exists": prior_error,
            "tool_names": _tool_names(step),
            ATTENTION_CHECK: _repeats_prior_verbatim(steps, index),
        },
    }


def _tool_names(step: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for call in step.get("tool_calls") or []:
        fn = (call or {}).get("function_name")
        if fn:
            out.append(str(fn))
    return out


def _looks_error(step: dict[str, Any]) -> bool:
    obs = step.get("observation") or {}
    results = obs.get("results") or []
    for r in results:
        content = json.dumps((r or {}).get("content", ""))
        if any(tok in content.lower() for tok in ("traceback", "error", "exit code 1")):
            return True
    return False


def _repeats_prior_action_signature(step: dict[str, Any]) -> str | None:
    calls = step.get("tool_calls") or []
    if not calls:
        return None
    return json.dumps(
        [
            [(c or {}).get("function_name"), (c or {}).get("arguments")]
            for c in calls
        ],
        sort_keys=True,
    )


def _repeats_prior_verbatim(steps: Sequence[dict[str, Any]], index: int) -> bool:
    sig = _repeats_prior_action_signature(steps[index])
    if sig is None:
        return False
    return any(_repeats_prior_action_signature(s) == sig for s in steps[:index])


def enumerate_universe(runs_root: Path) -> tuple[list[LabelItem], dict[str, Any]]:
    """Enumerate every labelable agent step with pinned provenance."""
    paths = sorted(runs_root.glob("**/agent/trajectory.json"))
    candidates: list[tuple[ItemProvenance, dict[str, Any], str]] = []
    trial_of_item: list[str] = []
    trials_seen: set[str] = set()
    basenames: set[str] = set()

    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = doc.get("steps") or []
        if not steps:
            continue
        sha = _sha256_file(path)
        relpath = str(path.relative_to(runs_root))
        trial_key = relpath  # full relpath: basename is NOT unique
        trials_seen.add(trial_key)
        basenames.add(path.parent.parent.name)
        instruction = doc.get("instruction") or doc.get("task_instruction")

        for index, step in enumerate(steps):
            if step.get("source") != "agent":
                continue  # only agent steps carry an agent decision to judge
            prov = ItemProvenance(relpath, index, sha)
            context = _render_context(steps, index, instruction)
            candidates.append((prov, context, _stratum_of(step, index, len(steps))))
            trial_of_item.append(trial_key)

    census = {
        "trajectory_files": len(paths),
        "distinct_trial_relpaths": len(trials_seen),
        "distinct_trial_basenames": len(basenames),
        "basename_collisions": len(trials_seen) - len(basenames),
        "agent_steps_total": len(candidates),
        "trials_with_agent_steps": len(set(trial_of_item)),
        "strata": _counts(stratum for _, _, stratum in candidates),
        "agent_steps_per_trial": _counts(trial_of_item),
    }
    items = [
        LabelItem(
            item_id=prov.item_id,
            provenance=asdict(prov),
            stratum=stratum,
            sampling_weight=1.0,
            selection_arm="prevalence_core",
            context=context,
            ratings=[],
        )
        for prov, context, stratum in candidates
    ]
    return items, census


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def select_items(
    items: Sequence[LabelItem],
    *,
    core_n: int | None,
    boost_per_stratum: int,
) -> list[LabelItem]:
    """Prevalence-valid random core plus rare-cell boost, weights recorded.

    Seed is derived from the digest of the candidate item ids, so selection is
    reproducible and cannot be tuned by re-running.
    """
    ordered = sorted(items, key=lambda i: i.item_id)
    seed_material = hashlib.sha256(
        "".join(i.item_id for i in ordered).encode("utf-8")
    ).hexdigest()
    rng = random.Random(int(seed_material[:16], 16))

    if core_n is None or core_n >= len(ordered):
        core = list(ordered)
        core_weight = 1.0
    else:
        core = rng.sample(ordered, core_n)
        core_weight = len(ordered) / core_n

    chosen: dict[str, LabelItem] = {}
    for item in core:
        chosen[item.item_id] = LabelItem(
            item_id=item.item_id,
            provenance=item.provenance,
            stratum=item.stratum,
            sampling_weight=core_weight,
            selection_arm="prevalence_core",
            context=item.context,
            ratings=[],
        )

    by_stratum: dict[str, list[LabelItem]] = {}
    for item in ordered:
        by_stratum.setdefault(item.stratum, []).append(item)

    for stratum, pool in sorted(by_stratum.items()):
        remaining = [i for i in pool if i.item_id not in chosen]
        take = min(boost_per_stratum, len(remaining))
        for item in rng.sample(remaining, take) if take else []:
            chosen[item.item_id] = LabelItem(
                item_id=item.item_id,
                provenance=item.provenance,
                stratum=stratum,
                sampling_weight=0.0,  # boost arm: excluded from prevalence math
                selection_arm="rare_cell_boost",
                context=item.context,
                ratings=[],
            )

    return sorted(chosen.values(), key=lambda i: i.item_id)


REQUIRED_RATERS_PER_ITEM = 3


def evaluate_readiness(
    items: Sequence[LabelItem], qualified_rater_ids: Sequence[str]
) -> dict[str, Any]:
    """Fail-closed readiness gate. NOT_READY unless every condition holds."""
    blockers: list[str] = []
    qualified = set(qualified_rater_ids)

    if len(qualified) < REQUIRED_RATERS_PER_ITEM:
        blockers.append(
            f"QUALIFIED_RATER_POOL_TOO_SMALL: have {len(qualified)}, "
            f"need >= {REQUIRED_RATERS_PER_ITEM}"
        )

    unrated = 0
    under_rated = 0
    duplicate_rater = 0
    unqualified = 0
    for item in items:
        rater_ids = [r.get("rater_id") for r in item.ratings]
        present = [r for r in rater_ids if r]
        if not present:
            unrated += 1
            continue
        if len(present) < REQUIRED_RATERS_PER_ITEM:
            under_rated += 1
        if len(set(present)) != len(present):
            duplicate_rater += 1
        if any(r not in qualified for r in present):
            unqualified += 1

    if unrated:
        blockers.append(f"ITEMS_WITH_ZERO_RATINGS: {unrated}")
    if under_rated:
        blockers.append(f"ITEMS_BELOW_THREE_RATERS: {under_rated}")
    if duplicate_rater:
        blockers.append(f"ITEMS_WITH_DUPLICATE_RATER_ID: {duplicate_rater}")
    if unqualified:
        blockers.append(f"ITEMS_WITH_UNQUALIFIED_RATER: {unqualified}")

    return {
        "readiness": "READY" if not blockers else "NOT_READY",
        "required_raters_per_item": REQUIRED_RATERS_PER_ITEM,
        "qualified_rater_pool_size": len(qualified),
        "blockers": blockers,
    }


def build_package(
    runs_root: Path, *, core_n: int | None, boost_per_stratum: int
) -> dict[str, Any]:
    universe, census = enumerate_universe(runs_root)
    selected = select_items(
        universe, core_n=core_n, boost_per_stratum=boost_per_stratum
    )
    readiness = evaluate_readiness(selected, qualified_rater_ids=())

    payload = {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "ownership": {
            "analyst": ["item_selection", "provenance", "label_taxonomy"],
            "tutor": [
                "agreement_statistic",
                "acceptance_threshold",
                "rater_qualification",
                "adjudication_rule",
                "power_argument",
            ],
        },
        "taxonomy": {
            "primary_label": PRIMARY_LABEL,
            "primary_values": list(PRIMARY_VALUES),
            "primary_definitions": PRIMARY_DEFINITIONS,
            "facet_labels": {k: list(v) for k, v in FACET_LABELS.items()},
            "facet_definitions": FACET_DEFINITIONS,
            "attention_check": ATTENTION_CHECK,
            "excluded_labels": EXCLUDED_LABELS,
        },
        "unset_parameters_owned_by_tutor": {
            "agreement_statistic": None,
            "acceptance_threshold": None,
            "required_interval_width": None,
            "adjudication_rule": None,
            "rater_qualification_criteria": None,
        },
        "census": census,
        "clustering_warning": {
            "independent_unit": "trial",
            "agent_steps": census["agent_steps_total"],
            "trials_with_agent_steps": census["trials_with_agent_steps"],
            "note": (
                "Steps nest inside trials and share task, model, and context. "
                "Any agreement interval computed treating steps as independent "
                "will be too narrow. Cluster on the trial relpath."
            ),
        },
        "n_selected": len(selected),
        "items": [asdict(i) for i in selected],
        "readiness": readiness,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the frozen ready-for-human-labeling gold-set package"
    )
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--core-n",
        type=int,
        default=None,
        help="prevalence-core size; omit to take the full universe",
    )
    parser.add_argument("--boost-per-stratum", type=int, default=0)
    args = parser.parse_args()

    payload = build_package(
        args.runs_root,
        core_n=args.core_n,
        boost_per_stratum=args.boost_per_stratum,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    args.out.write_text(serialized, encoding="utf-8")

    payload_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    print(f"wrote {args.out}")
    print(f"package_sha256 {payload_digest}")
    print(f"readiness {payload['readiness']['readiness']}")
    for blocker in payload["readiness"]["blockers"]:
        print(f"  blocker {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
