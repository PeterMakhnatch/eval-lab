"""Build the frozen ready-for-human-labeling gold-set package.

OWNERSHIP
    Analyst owns item selection, provenance pinning, and label taxonomy.
    Tutor owns the agreement statistic, its acceptance threshold, rater
    qualification, adjudication rule, and the power argument.

HARD CONSTRAINTS
    - Emits ZERO ratings. There is no code path here that can write one.
    - LLM judge output is never read, imported, or accepted as gold.
    - readiness is NOT_READY until three unique qualified rater IDs per item
      AND every submitted label is present and in-enum. Fails closed.
    - Deterministic: content-addressed identity, canonical ordering,
      digest-derived seed. Re-runs are byte-identical.

REVISION 2 - five blockers found by independent review, all fixed at root:

  B1 Rater context was unlabelable. task_instruction was ALWAYS None (no such
     key exists in ATIF; the instruction lives in the trailing user step), and
     prior steps carried only a digest with no message/arguments/observation.
     69% of agent steps have an empty message, so 126 of 183 items showed the
     rater nothing judgeable. FIX: extract the instruction from user steps and
     render full prior-step content.

  B2 Machine truth was leaked into rater-facing context. The attention check's
     own answer and prior_error_exists were visible to raters, destroying the
     check and priming the error_response facet. FIX: machine truth moves to a
     separate withheld artifact, never shipped to raters.

  B3 Byte-identical trajectories were double-counted. 29 files carry only 26
     distinct sha256; 3 shas appear at 2 paths each. Keying on relpath inflated
     183 unique agent steps to 237 and corrupted every cluster statistic.
     FIX: identity is (source_sha256, step_index); relpaths become aliases.

  B4 Ratings were nested inside the item, so labelling mutated the "frozen"
     digest, and readiness counted rater IDs without validating labels.
     FIX: items are immutable; ratings live in separate typed RatingRecord
     sidecars; readiness validates enums and label completeness.

  B5 error_response and abstention lacked CANNOT_JUDGE while the primary label
     had it. The argument for the escape hatch applies identically to facets.
     FIX: CANNOT_JUDGE on every human-judged field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "goldset-labeling-package/v2"
TAXONOMY_VERSION = "analyst-step-taxonomy/v2"
RATING_SCHEMA_VERSION = "goldset-rating-record/v1"

MAX_TEXT_CHARS = 4000
MAX_OBS_CHARS = 4000

CANNOT_JUDGE = "CANNOT_JUDGE"
INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"

SelectionArm = Literal["prevalence_core", "rare_cell_boost"]

PRIMARY_LABEL = "step_contribution"
PRIMARY_VALUES = ("PROGRESS", "NEUTRAL", "HARMFUL", CANNOT_JUDGE, INSUFFICIENT_CONTEXT)

PRIMARY_DEFINITIONS = {
    "PROGRESS": (
        "The step moves the task closer to satisfying the stated goal: it acquires "
        "information the agent did not have, or changes state in a direction the "
        "goal requires."
    ),
    "NEUTRAL": (
        "The step neither advances nor sets back the goal. Valid exploration, "
        "re-reading already-held information, and no-op confirmations belong here."
    ),
    "HARMFUL": (
        "The step sets the task back: it destroys needed state, introduces an error "
        "the agent must later undo, or commits to a path the goal excludes."
    ),
    CANNOT_JUDGE: (
        "The rater HAS the context needed and the step is genuinely ambiguous. A "
        "valid answer, never penalised. Its rate measures TAXONOMY ambiguity."
    ),
    INSUFFICIENT_CONTEXT: (
        "The context needed to judge is ABSENT or TRUNCATED in the package itself. "
        "Distinct from CANNOT_JUDGE: this reports a PACKAGE DEFECT the builder can "
        "fix, not ambiguity in the step. Its rate measures package completeness."
    ),
}

# Every human-judged field carries CANNOT_JUDGE (B5).
FACET_LABELS = {
    "error_response": (
        "NO_PRIOR_ERROR",
        "ACKNOWLEDGED_AND_CHANGED",
        "ACKNOWLEDGED_NOT_CHANGED",
        "IGNORED_PRIOR_ERROR",
        CANNOT_JUDGE,
        INSUFFICIENT_CONTEXT,
    ),
    "abstention": (
        "ACTED",
        "DECLINED_WITH_REASON",
        "DECLINED_NO_REASON",
        CANNOT_JUDGE,
        INSUFFICIENT_CONTEXT,
    ),
    "repeats_prior_action": ("YES", "NO", CANNOT_JUDGE, INSUFFICIENT_CONTEXT),
}

FACET_DEFINITIONS = {
    "error_response": {
        "NO_PRIOR_ERROR": "No error is visible in any prior step.",
        "ACKNOWLEDGED_AND_CHANGED": (
            "A prior error is visible and this step's action differs from the "
            "failing action in tool, arguments, or approach."
        ),
        "ACKNOWLEDGED_NOT_CHANGED": (
            "The step references the prior error but repeats the failing action "
            "substantively unchanged."
        ),
        "IGNORED_PRIOR_ERROR": (
            "A prior error is visible and the step neither references nor responds to it."
        ),
        CANNOT_JUDGE: "Context is present; the step is genuinely ambiguous.",
        INSUFFICIENT_CONTEXT: "Required context is absent or truncated in the package.",
    },
    "abstention": {
        "ACTED": "The step takes an action or makes a claim.",
        "DECLINED_WITH_REASON": "The step declines to act or answer AND states a reason.",
        "DECLINED_NO_REASON": "The step declines with no reason given.",
        CANNOT_JUDGE: "Context is present; the step is genuinely ambiguous.",
        INSUFFICIENT_CONTEXT: "Required context is absent or truncated in the package.",
    },
    "repeats_prior_action": {
        "YES": "This step's tool call repeats a prior call with the same arguments.",
        "NO": "This step's action is not a verbatim repeat of any prior call.",
        CANNOT_JUDGE: "Context is present; the step is genuinely ambiguous.",
        INSUFFICIENT_CONTEXT: "Required context is absent or truncated in the package.",
    },
}

# repeats_prior_action is an ATTENTION CHECK: machine ground truth exists and is
# withheld from raters (B2). Never pooled into taxonomy agreement.
ATTENTION_CHECK_FIELD = "repeats_prior_action"

HUMAN_JUDGED_FIELDS = (PRIMARY_LABEL, *FACET_LABELS.keys())

ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    PRIMARY_LABEL: PRIMARY_VALUES,
    **{k: v for k, v in FACET_LABELS.items()},
}

EXCLUDED_LABELS = {
    "step_necessity": (
        "Requires an oracle optimal path. None exists, so any label encodes the "
        "rater's guess at optimality."
    ),
    "step_efficiency": ("Same defect, plus it presumes a cost model the instruction never states."),
    "unrecoverability": (
        "Counterfactual - quantifies over all continuations. Blocked on a "
        "preregistered predicate with a declared false-positive rate against later "
        "success. Not a human-labelable property."
    ),
}


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


@dataclass(frozen=True)
class ItemIdentity:
    """Content-addressed identity. Relpath is an alias, never an identity (B3)."""

    source_sha256: str
    step_index: int
    source_aliases: tuple[str, ...]

    @property
    def item_id(self) -> str:
        payload = f"{self.source_sha256}#{self.step_index}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LabelItem:
    """Immutable. Carries no rating field at all (B4)."""

    item_id: str
    source_sha256: str
    step_index: int
    source_aliases: tuple[str, ...]
    model_name: str | None
    agent_name: str | None
    stratum: str
    sampling_weight: float
    selection_arm: SelectionArm
    cluster_id: str
    context_completeness: dict[str, Any]
    rater_context: dict[str, Any]


@dataclass(frozen=True)
class MachineTruth:
    """WITHHELD from raters (B2). Scoring-side only."""

    item_id: str
    repeats_prior_action: bool
    tool_names: tuple[str, ...]
    # prior_error_visible REMOVED. ATIF observations carry no structured exit
    # codes, so the only available implementation was substring matching on
    # observation text. Audited at 88% false positives - it fired on
    # "Script completed / Wall time 0.1 seconds". An 88%-wrong "truth" is worse
    # than no truth, so the fact is withdrawn rather than tightened.
    prior_error_truth_available: bool = False
    prior_error_unavailable_reason: str = (
        "NO_STRUCTURED_EXIT_CODES: deterministic detection not implementable from "
        "ATIF observation text; substring matching audited at 88% false positives"
    )


def _tool_calls_rendered(step: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in step.get("tool_calls") or []:
        call = call or {}
        args_text, args_trunc = _truncate(_as_text(call.get("arguments")), MAX_TEXT_CHARS)
        out.append(
            {
                "function_name": call.get("function_name"),
                "arguments": args_text,
                "arguments_truncated": args_trunc,
            }
        )
    return out


def _observation_rendered(step: dict[str, Any]) -> list[dict[str, Any]]:
    obs = step.get("observation") or {}
    out: list[dict[str, Any]] = []
    for result in obs.get("results") or []:
        result = result or {}
        text, trunc = _truncate(_as_text(result.get("content")), MAX_OBS_CHARS)
        out.append({"content": text, "content_truncated": trunc})
    return out


def _step_view(step: dict[str, Any], index: int) -> dict[str, Any]:
    """Full content a rater needs. Used for BOTH prior steps and the item (B1)."""
    message, msg_trunc = _truncate(_as_text(step.get("message")), MAX_TEXT_CHARS)
    return {
        "index": index,
        "source": step.get("source"),
        "message": message,
        "message_truncated": msg_trunc,
        "message_is_empty": not message.strip(),
        "tool_calls": _tool_calls_rendered(step),
        "observation": _observation_rendered(step),
    }


def _extract_instruction(steps: Sequence[dict[str, Any]], upto: int) -> dict[str, Any]:
    """The task instruction is the trailing user step before the agent turn.

    No `instruction` key exists in ATIF. Earlier user steps are harness preamble
    (plugin lists, environment banners). We ship every user step preceding the
    item so the rater sees exactly what the agent saw, and mark the last one as
    the presumed task statement.
    """
    user_steps = [_step_view(s, i) for i, s in enumerate(steps[:upto]) if s.get("source") == "user"]
    return {
        "presumed_task_statement": user_steps[-1] if user_steps else None,
        "all_user_steps_before_item": user_steps,
        "extraction_rule": (
            "trailing user step before the item; earlier user steps are harness "
            "preamble and are included in full for completeness"
        ),
    }


def _completeness(steps: Sequence[dict[str, Any]], index: int) -> dict[str, Any]:
    """Builder-declared context completeness.

    A rater choosing INSUFFICIENT_CONTEXT can be cross-checked against this. If
    the builder says context is complete and raters disagree, the package has a
    defect the builder did not detect - which is exactly the signal we want.
    """
    step = steps[index]
    has_user = any(s.get("source") == "user" for s in steps[:index])
    item_view = _step_view(step, index)
    any_trunc = (
        item_view["message_truncated"]
        or any(c["arguments_truncated"] for c in item_view["tool_calls"])
        or any(o["content_truncated"] for o in item_view["observation"])
    )
    judgeable = (
        bool(item_view["tool_calls"] or item_view["observation"])
        or not item_view["message_is_empty"]
    )
    return {
        "instruction_present": has_user,
        "prior_steps_rendered": index,
        "item_has_judgeable_content": judgeable,
        "any_content_truncated": any_trunc,
        "builder_verdict": (
            "COMPLETE" if (has_user and judgeable and not any_trunc) else "DEGRADED"
        ),
    }


def _action_signature(step: dict[str, Any]) -> str | None:
    calls = step.get("tool_calls") or []
    if not calls:
        return None
    return json.dumps(
        [[(c or {}).get("function_name"), (c or {}).get("arguments")] for c in calls],
        sort_keys=True,
    )


def _stratum_of(step: dict[str, Any], index: int, n_steps: int) -> str:
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


def enumerate_universe(
    runs_root: Path,
) -> tuple[list[LabelItem], list[MachineTruth], dict[str, Any]]:
    """Enumerate deduplicated labelable agent steps with blinded rater context."""
    paths = sorted(runs_root.glob("**/agent/trajectory.json"))

    # B3: group by content digest first, so identical files collapse.
    by_sha: dict[str, list[Path]] = {}
    for path in paths:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        by_sha.setdefault(digest, []).append(path)

    items: list[LabelItem] = []
    truths: list[MachineTruth] = []
    per_cluster: list[str] = []
    duplicate_paths_dropped = 0

    for digest in sorted(by_sha):
        group = sorted(by_sha[digest])
        duplicate_paths_dropped += len(group) - 1
        canonical = group[0]
        try:
            doc = json.loads(canonical.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = doc.get("steps") or []
        if not steps:
            continue
        agent_meta = doc.get("agent") or {}
        aliases = tuple(str(p.relative_to(runs_root)) for p in group)

        for index, step in enumerate(steps):
            if step.get("source") != "agent":
                continue
            identity = ItemIdentity(digest, index, aliases)
            signature = _action_signature(step)
            repeats = signature is not None and any(
                _action_signature(s) == signature for s in steps[:index]
            )

            items.append(
                LabelItem(
                    item_id=identity.item_id,
                    source_sha256=digest,
                    step_index=index,
                    source_aliases=aliases,
                    model_name=agent_meta.get("model_name"),
                    agent_name=agent_meta.get("name"),
                    stratum=_stratum_of(step, index, len(steps)),
                    sampling_weight=1.0,
                    selection_arm="prevalence_core",
                    cluster_id=digest,  # B3: cluster is the trajectory content
                    context_completeness=_completeness(steps, index),
                    rater_context={
                        "instruction": _extract_instruction(steps, index),
                        "prior_steps": [_step_view(s, i) for i, s in enumerate(steps[:index])],
                        "item_step": _step_view(step, index),
                        "total_steps_in_trajectory": len(steps),
                    },
                )
            )
            truths.append(
                MachineTruth(
                    item_id=identity.item_id,
                    repeats_prior_action=repeats,
                    tool_names=tuple(
                        str((c or {}).get("function_name"))
                        for c in step.get("tool_calls") or []
                        if (c or {}).get("function_name")
                    ),
                )
            )
            per_cluster.append(digest)

    alias_manifest = {
        digest: {
            "canonical_relpath": str(sorted(group)[0].relative_to(runs_root)),
            "all_relpaths": [str(p.relative_to(runs_root)) for p in sorted(group)],
            "duplicate_count": len(group) - 1,
        }
        for digest, group in sorted(by_sha.items())
    }

    empty_msg = sum(1 for i in items if i.rater_context["item_step"]["message_is_empty"])
    census = {
        "trajectory_files_seen": len(paths),
        "distinct_content_digests": len(by_sha),
        "duplicate_paths_dropped": duplicate_paths_dropped,
        "agent_steps_unique": len(items),
        "clusters_with_agent_steps": len(set(per_cluster)),
        "items_with_empty_message": empty_msg,
        "items_with_empty_message_pct": (
            round(100.0 * empty_msg / len(items), 1) if items else 0.0
        ),
        "items_with_instruction_present": sum(
            1
            for i in items
            if i.rater_context["instruction"]["presumed_task_statement"] is not None
        ),
        "strata": _counts(i.stratum for i in items),
        "agent_steps_per_cluster": _counts(per_cluster),
        "models": _counts(str(i.model_name) for i in items),
        "context_completeness": _counts(i.context_completeness["builder_verdict"] for i in items),
    }
    census["alias_manifest"] = alias_manifest
    return items, truths, census


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def select_items(
    items: Sequence[LabelItem], *, core_n: int | None, boost_per_stratum: int
) -> list[LabelItem]:
    ordered = sorted(items, key=lambda i: i.item_id)
    seed_material = hashlib.sha256("".join(i.item_id for i in ordered).encode("utf-8")).hexdigest()
    rng = random.Random(int(seed_material[:16], 16))

    if core_n is None or core_n >= len(ordered):
        core, core_weight = list(ordered), 1.0
    else:
        core, core_weight = rng.sample(ordered, core_n), len(ordered) / core_n

    chosen: dict[str, LabelItem] = {}
    for item in core:
        chosen[item.item_id] = _rearm(item, core_weight, "prevalence_core")

    by_stratum: dict[str, list[LabelItem]] = {}
    for item in ordered:
        by_stratum.setdefault(item.stratum, []).append(item)
    for _stratum, pool in sorted(by_stratum.items()):
        remaining = [i for i in pool if i.item_id not in chosen]
        take = min(boost_per_stratum, len(remaining))
        for item in rng.sample(remaining, take) if take else []:
            chosen[item.item_id] = _rearm(item, 0.0, "rare_cell_boost")

    return sorted(chosen.values(), key=lambda i: i.item_id)


def _rearm(item: LabelItem, weight: float, arm: SelectionArm) -> LabelItem:
    return LabelItem(
        item_id=item.item_id,
        source_sha256=item.source_sha256,
        step_index=item.step_index,
        source_aliases=item.source_aliases,
        model_name=item.model_name,
        agent_name=item.agent_name,
        stratum=item.stratum,
        sampling_weight=weight,
        selection_arm=arm,
        cluster_id=item.cluster_id,
        context_completeness=item.context_completeness,
        rater_context=item.rater_context,
    )


# ---------------------------------------------------------------------------
# Ratings: separate typed sidecars. Items never mutate (B4).
# ---------------------------------------------------------------------------

REQUIRED_RATERS_PER_ITEM = 3

# Cluster adequacy, set by Tutor (wK:p4) power verdict 2026-08-28.
MIN_EFFECTIVE_CLUSTERS = 20.0
MAX_CLUSTER_CONCENTRATION = 0.05
TARGET_CLUSTER_FLOOR = 30  # K = max(30, 96*rho) pending an ICC pilot


def effective_clusters(cluster_sizes: Sequence[int]) -> float:
    """Kish effective cluster count: (sum n)^2 / sum n^2."""
    total = sum(cluster_sizes)
    ss = sum(n * n for n in cluster_sizes)
    if not total or not ss:
        return 0.0
    return (total * total) / ss


def evaluate_cluster_adequacy(cluster_sizes: Sequence[int]) -> dict[str, Any]:
    """Gate on design effect, not raw cluster count (Tutor power verdict)."""
    total = sum(cluster_sizes)
    k_eff = effective_clusters(cluster_sizes)
    concentration = (max(cluster_sizes) / total) if total and cluster_sizes else 0.0
    blockers: list[str] = []
    if k_eff < MIN_EFFECTIVE_CLUSTERS:
        blockers.append(
            f"EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff={k_eff:.2f} < {MIN_EFFECTIVE_CLUSTERS}"
        )
    if concentration > MAX_CLUSTER_CONCENTRATION:
        blockers.append(
            f"CLUSTER_CONCENTRATION_TOO_HIGH: {concentration:.1%} > {MAX_CLUSTER_CONCENTRATION:.0%}"
        )
    return {
        "raw_clusters": len(cluster_sizes),
        "effective_clusters_kish": round(k_eff, 2),
        "max_cluster_concentration": round(concentration, 4),
        "min_effective_clusters": MIN_EFFECTIVE_CLUSTERS,
        "max_cluster_concentration_target": MAX_CLUSTER_CONCENTRATION,
        "target_cluster_floor_pending_icc_pilot": TARGET_CLUSTER_FLOOR,
        "blockers": blockers,
    }


def load_rating_records(ratings_dir: Path | None) -> list[dict[str, Any]]:
    """Load RatingRecord sidecars. Returns [] when the directory is absent."""
    if ratings_dir is None or not ratings_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(ratings_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records.append({"_invalid_file": str(path.name)})
            continue
        for record in payload if isinstance(payload, list) else [payload]:
            record = dict(record)
            record["_source_file"] = path.name
            records.append(record)
    return records


def validate_rating(record: dict[str, Any]) -> list[str]:
    """Validate one RatingRecord. Enum and completeness checked (B4)."""
    errors: list[str] = []
    if record.get("_invalid_file"):
        return [f"UNPARSEABLE_FILE:{record['_invalid_file']}"]
    if record.get("schema_version") != RATING_SCHEMA_VERSION:
        errors.append("BAD_SCHEMA_VERSION")
    if not str(record.get("item_id") or "").strip():
        errors.append("MISSING_ITEM_ID")
    if not str(record.get("rater_id") or "").strip():
        errors.append("MISSING_RATER_ID")
    for field_name in HUMAN_JUDGED_FIELDS:
        value = record.get(field_name)
        if value is None:
            errors.append(f"MISSING_LABEL:{field_name}")
        elif value not in ALLOWED_VALUES[field_name]:
            errors.append(f"OUT_OF_ENUM:{field_name}={value!r}")
    return errors


def evaluate_readiness(
    items: Sequence[LabelItem],
    records: Sequence[dict[str, Any]],
    qualified_rater_ids: Sequence[str],
) -> dict[str, Any]:
    """Fail-closed. Validates labels, not merely the presence of rater IDs (B4).

    Also gates on cluster adequacy: a package whose design effect cannot support
    an agreement interval must not be labelled regardless of rater supply.
    """
    blockers: list[str] = []
    sizes: dict[str, int] = {}
    for item in items:
        sizes[item.cluster_id] = sizes.get(item.cluster_id, 0) + 1
    adequacy = evaluate_cluster_adequacy(list(sizes.values()))
    blockers.extend(adequacy["blockers"])
    qualified = set(qualified_rater_ids)
    item_ids = {i.item_id for i in items}

    invalid = 0
    unknown_item = 0
    by_item: dict[str, set[str]] = {}
    for record in records:
        errors = validate_rating(record)
        if errors:
            invalid += 1
            continue
        if record["item_id"] not in item_ids:
            unknown_item += 1
            continue
        by_item.setdefault(record["item_id"], set()).add(record["rater_id"])

    if len(qualified) < REQUIRED_RATERS_PER_ITEM:
        blockers.append(
            f"QUALIFIED_RATER_POOL_TOO_SMALL: have {len(qualified)}, "
            f"need >= {REQUIRED_RATERS_PER_ITEM}"
        )
    if invalid:
        blockers.append(f"INVALID_RATING_RECORDS: {invalid}")
    if unknown_item:
        blockers.append(f"RATINGS_FOR_UNKNOWN_ITEM: {unknown_item}")

    zero = sum(1 for i in items if not by_item.get(i.item_id))
    under = sum(
        1 for i in items if 0 < len(by_item.get(i.item_id, set())) < REQUIRED_RATERS_PER_ITEM
    )
    unqualified = sum(
        1 for i in items if by_item.get(i.item_id) and not by_item[i.item_id] <= qualified
    )
    if zero:
        blockers.append(f"ITEMS_WITH_ZERO_VALID_RATINGS: {zero}")
    if under:
        blockers.append(f"ITEMS_BELOW_THREE_UNIQUE_RATERS: {under}")
    if unqualified:
        blockers.append(f"ITEMS_WITH_UNQUALIFIED_RATER: {unqualified}")

    return {
        "readiness": "READY" if not blockers else "NOT_READY",
        "cluster_adequacy": adequacy,
        "required_unique_raters_per_item": REQUIRED_RATERS_PER_ITEM,
        "qualified_rater_pool_size": len(qualified),
        "valid_rating_records": sum(len(v) for v in by_item.values()),
        "blockers": blockers,
    }


def build_package(
    runs_root: Path,
    *,
    core_n: int | None,
    boost_per_stratum: int,
    ratings_dir: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    universe, truths, census = enumerate_universe(runs_root)
    selected = select_items(universe, core_n=core_n, boost_per_stratum=boost_per_stratum)
    keep = {i.item_id for i in selected}
    records = load_rating_records(ratings_dir)
    readiness = evaluate_readiness(selected, records, qualified_rater_ids=())

    n_clusters = census["clusters_with_agent_steps"]
    package = {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "rating_schema_version": RATING_SCHEMA_VERSION,
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
        "blinding": {
            "machine_truth_withheld": True,
            "withheld_fields": ["repeats_prior_action"],
            "withdrawn_fields": {
                "prior_error_visible": (
                    "Withdrawn. Only implementable as substring matching on "
                    "observation text; audited at 88% false positives. The "
                    "error_response FACET remains - humans can read the "
                    "observation - but no machine truth is claimed."
                )
            },
            "note": (
                "Machine ground truth is emitted to a separate artifact and MUST "
                "NOT be shown to raters. Leaking it destroys the attention check "
                "and primes the error_response facet."
            ),
        },
        "taxonomy": {
            "primary_label": PRIMARY_LABEL,
            "human_judged_fields": list(HUMAN_JUDGED_FIELDS),
            "allowed_values": {k: list(v) for k, v in ALLOWED_VALUES.items()},
            "primary_definitions": PRIMARY_DEFINITIONS,
            "facet_definitions": FACET_DEFINITIONS,
            "attention_check_field": ATTENTION_CHECK_FIELD,
            "missing_data_semantics": {
                CANNOT_JUDGE: (
                    "Context IS present; the step is genuinely ambiguous. Measures "
                    "taxonomy ambiguity."
                ),
                INSUFFICIENT_CONTEXT: (
                    "Context is ABSENT or TRUNCATED in the package. Measures package "
                    "completeness and reports a builder-fixable defect."
                ),
                "note": (
                    "These MUST NOT be pooled. Cross-check rater "
                    "INSUFFICIENT_CONTEXT against item.context_completeness."
                    "builder_verdict; disagreement means the builder missed a defect."
                ),
            },
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
            "independent_unit": "trajectory content digest",
            "agent_steps_unique": census["agent_steps_unique"],
            "clusters": n_clusters,
            "note": (
                "Steps nest inside trajectories and share task, model, and "
                "context. Any agreement interval computed treating steps as "
                "independent will be too narrow. Cluster on cluster_id."
            ),
        },
        "n_selected": len(selected),
        "items": [asdict(i) for i in selected],
        "readiness": readiness,
    }
    machine_truth = {
        "schema_version": "goldset-machine-truth/v1",
        "warning": "WITHHELD FROM RATERS. Scoring side only.",
        "truths": [asdict(t) for t in truths if t.item_id in keep],
    }
    return package, machine_truth


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the frozen ready-for-human-labeling gold-set package"
    )
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--machine-truth-out", type=Path, required=True)
    parser.add_argument("--ratings-dir", type=Path, default=None)
    parser.add_argument("--core-n", type=int, default=None)
    parser.add_argument("--boost-per-stratum", type=int, default=0)
    args = parser.parse_args()

    package, truth = build_package(
        args.runs_root,
        core_n=args.core_n,
        boost_per_stratum=args.boost_per_stratum,
        ratings_dir=args.ratings_dir,
    )
    for path, payload in ((args.out, package), (args.machine_truth_out, truth)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    census = package["census"]
    print(f"wrote {args.out}")
    print(f"wrote {args.machine_truth_out} (WITHHELD)")
    print(f"package_sha256 {digest}")
    print(
        f"unique_agent_steps {census['agent_steps_unique']} "
        f"clusters {census['clusters_with_agent_steps']} "
        f"duplicate_paths_dropped {census['duplicate_paths_dropped']}"
    )
    print(
        f"instruction_present {census['items_with_instruction_present']}"
        f"/{census['agent_steps_unique']}  "
        f"empty_message {census['items_with_empty_message_pct']}%"
    )
    print(f"readiness {package['readiness']['readiness']}")
    for blocker in package["readiness"]["blockers"]:
        print(f"  blocker {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
