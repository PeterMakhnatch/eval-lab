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
import fcntl
import hashlib
import hmac
import json
import os
import random
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "goldset-labeling-package/v2"
TAXONOMY_VERSION = "analyst-step-taxonomy/v2"
RATING_SCHEMA_VERSION = "goldset-rating-record/v1"

MAX_TEXT_CHARS = 262144  # corpus is ~756 KB; truncating it was gratuitous
MAX_OBS_CHARS = 262144

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


# Volatile metadata is stripped ONLY at the known top level of a step. Revision 6
# dropped these keys RECURSIVELY and whitespace-collapsed EVERY string, which can
# make genuinely different payloads collide - two code blocks differing only in
# indentation are NOT the same program. Payload bytes (tool arguments, observation
# content) are now preserved verbatim.
VOLATILE_STEP_KEYS = frozenset(
    {
        "timestamp",
        "step_id",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "duration",
        "wall_time",
        "latency",
        "metrics",
        "extra",
    }
)

# Per-tool-call volatile keys, stripped at exactly one known depth.
VOLATILE_CALL_KEYS = frozenset({"tool_call_id", "id"})

# Per-observation-result volatile keys, stripped at exactly one known depth.
VOLATILE_RESULT_KEYS = frozenset({"source_call_id", "tool_call_id", "id"})


def _strip_known(mapping: Any, volatile: frozenset[str]) -> Any:
    """Drop volatile keys at THIS level only. Values pass through untouched."""
    if not isinstance(mapping, dict):
        return mapping
    return {k: v for k, v in sorted(mapping.items()) if k not in volatile}


def _canonical_step(step: dict[str, Any]) -> dict[str, Any]:
    """Logical view of a step: known metadata stripped, payload bytes preserved."""
    calls = [_strip_known(call, VOLATILE_CALL_KEYS) for call in (step.get("tool_calls") or [])]
    observation = step.get("observation") or {}
    results = [
        _strip_known(result, VOLATILE_RESULT_KEYS) for result in (observation.get("results") or [])
    ]
    return {
        "source": step.get("source"),
        # message is a payload: preserved verbatim, NOT whitespace-collapsed
        "message": step.get("message"),
        "tool_calls": calls,
        "observation": {"results": results},
    }


def logical_step_digest(step: dict[str, Any]) -> str:
    """Digest of a step's LOGICAL content: message, action, observation."""
    payload = _canonical_step(step)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def logical_trial_digest(steps: Sequence[dict[str, Any]]) -> str:
    """Digest of a whole trajectory's logical content - the cluster identity."""
    return hashlib.sha256(
        "".join(logical_step_digest(s) for s in steps).encode("utf-8")
    ).hexdigest()


class SourceRejectedError(RuntimeError):
    """Raised when a source path is unsafe or its bytes cannot be trusted."""


@dataclass(frozen=True)
class SourceBuffer:
    """One immutable read. The digest describes EXACTLY these parsed bytes.

    Revision 4 hashed with path.read_bytes() and parsed with path.read_text() -
    two separate reads. Between them the file could change, so the recorded
    digest need not describe the parsed content (TOCTOU). Both now derive from a
    single buffer.
    """

    relpath: str
    raw: bytes
    digest: str
    doc: dict[str, Any]


def read_source_once(path: Path, runs_root: Path) -> SourceBuffer:
    """Read a trajectory exactly once; hash and parse the same buffer."""
    resolved_root = runs_root.resolve()
    if path.is_symlink():
        raise SourceRejectedError(f"SYMLINK_REJECTED: {path}")
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SourceRejectedError(f"ROOT_ESCAPE_REJECTED: {path}") from exc
    for parent in path.parents:
        if parent in (runs_root, resolved_root):
            break
        if parent.is_symlink():
            raise SourceRejectedError(f"SYMLINK_PARENT_REJECTED: {parent}")
    if not resolved.is_file():
        raise SourceRejectedError(f"NOT_A_REGULAR_FILE: {path}")

    with open(resolved, "rb") as handle:
        raw = handle.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRejectedError(f"UNPARSEABLE: {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise SourceRejectedError(f"NOT_AN_OBJECT: {path}")
    return SourceBuffer(str(relative), raw, digest, doc)


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
    logical_lineage: tuple[str, ...]
    model_name: str | None
    agent_name: str | None
    stratum: str
    sampling_weight: float
    selection_arm: SelectionArm
    cluster_id: str
    logical_step_digest: str
    item_context_digest: str
    context_completeness: dict[str, Any]
    rater_context: dict[str, Any]


def label_item_from_dict(payload: Mapping[str, Any]) -> LabelItem:
    """Typed reconstruction from a serialized item. Lists become tuples."""
    return LabelItem(
        item_id=str(payload["item_id"]),
        source_sha256=str(payload["source_sha256"]),
        step_index=int(payload["step_index"]),
        source_aliases=tuple(payload["source_aliases"]),
        logical_lineage=tuple(payload["logical_lineage"]),
        model_name=payload.get("model_name"),
        agent_name=payload.get("agent_name"),
        stratum=str(payload["stratum"]),
        sampling_weight=float(payload["sampling_weight"]),
        selection_arm=(
            "rare_cell_boost"
            if payload["selection_arm"] == "rare_cell_boost"
            else "prevalence_core"
        ),
        cluster_id=str(payload["cluster_id"]),
        logical_step_digest=str(payload["logical_step_digest"]),
        item_context_digest=str(payload["item_context_digest"]),
        context_completeness=dict(payload["context_completeness"]),
        rater_context=dict(payload["rater_context"]),
    )


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
    """Builder-declared context completeness, evaluated over PRIOR steps too.

    Revision 3 examined only the item's own truncation and whether any user step
    existed. It therefore reported COMPLETE for 124 items whose PRIOR steps were
    truncated - the verdict was simply false. COMPLETE now requires that nothing
    anywhere in the rendered context is truncated and every prior step carries
    renderable content.
    """
    item_view = _step_view(steps[index], index)
    prior_views = [_step_view(s, i) for i, s in enumerate(steps[:index])]
    has_user = any(s.get("source") == "user" for s in steps[:index])

    def _trunc(view: dict[str, Any]) -> bool:
        return bool(
            view["message_truncated"]
            or any(c["arguments_truncated"] for c in view["tool_calls"])
            or any(o["content_truncated"] for o in view["observation"])
        )

    def _contentful(view: dict[str, Any]) -> bool:
        return bool(view["message"].strip() or view["tool_calls"] or view["observation"])

    item_truncated = _trunc(item_view)
    prior_truncated = sum(1 for v in prior_views if _trunc(v))
    prior_contentless = sum(1 for v in prior_views if not _contentful(v))
    item_judgeable = _contentful(item_view)

    reasons: list[str] = []
    if not has_user:
        reasons.append("NO_TASK_STATEMENT")
    if not item_judgeable:
        reasons.append("ITEM_HAS_NO_CONTENT")
    if item_truncated:
        reasons.append("ITEM_TRUNCATED")
    if prior_truncated:
        reasons.append(f"PRIOR_TRUNCATED:{prior_truncated}")
    if prior_contentless:
        reasons.append(f"PRIOR_CONTENTLESS:{prior_contentless}")

    return {
        "instruction_present": has_user,
        "prior_steps_rendered": len(prior_views),
        "item_has_judgeable_content": item_judgeable,
        "item_truncated": item_truncated,
        "prior_steps_truncated": prior_truncated,
        "prior_steps_contentless": prior_contentless,
        "degraded_reasons": reasons,
        "builder_verdict": "COMPLETE" if not reasons else "INCOMPLETE",
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
    # Each file is read EXACTLY ONCE; digest and parse share one buffer.
    by_sha: dict[str, list[SourceBuffer]] = {}
    rejected: list[str] = []
    for path in paths:
        try:
            buffer = read_source_once(path, runs_root)
        except SourceRejectedError as exc:
            rejected.append(str(exc))
            continue
        except OSError as exc:
            rejected.append(f"IO_ERROR: {path}: {exc}")
            continue
        by_sha.setdefault(buffer.digest, []).append(buffer)

    items: list[LabelItem] = []
    truths: list[MachineTruth] = []
    per_cluster: list[str] = []
    duplicate_paths_dropped = 0

    for digest in sorted(by_sha):
        group = sorted(by_sha[digest], key=lambda b: b.relpath)
        duplicate_paths_dropped += len(group) - 1
        doc = group[0].doc  # already parsed from the hashed buffer
        steps = doc.get("steps") or []
        if not steps:
            continue
        agent_meta = doc.get("agent") or {}
        aliases = tuple(b.relpath for b in group)
        logical_trial = logical_trial_digest(steps)

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
                    logical_lineage=(),
                    model_name=agent_meta.get("model_name"),
                    agent_name=agent_meta.get("name"),
                    stratum=_stratum_of(step, index, len(steps)),
                    sampling_weight=1.0,
                    selection_arm="prevalence_core",
                    cluster_id=logical_trial,  # logical, not raw-byte (sec fix 2)
                    logical_step_digest=logical_step_digest(step),
                    item_context_digest="",  # stamped below once context is built
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
            per_cluster.append(logical_trial)

    alias_manifest = {
        digest: {
            "canonical_relpath": sorted(b.relpath for b in group)[0],
            "all_relpaths": sorted(b.relpath for b in group),
            "duplicate_count": len(group) - 1,
        }
        for digest, group in sorted(by_sha.items())
    }

    # Stamp the context digest now that rater_context exists.
    items = [
        replace(
            i,
            item_context_digest=compute_item_context_digest(
                i.rater_context,
                cluster_id=i.cluster_id,
                step_index=i.step_index,
            ),
        )
        for i in items
    ]

    # Semantic clones were previously distinct ITEMS: 183 items carried only 167
    # distinct logical step digests. Deduplicate on the logical digest and keep
    # every raw lineage alias on the survivor.
    deduped: dict[str, LabelItem] = {}
    lineage: dict[str, list[str]] = {}
    clone_items_dropped = 0
    for item in items:
        # Identity is the FULL context digest, not the step's own content.
        key = item.item_context_digest
        entry = json.dumps(
            {
                "source_sha256": item.source_sha256,
                "step_index": item.step_index,
                "source_aliases": list(item.source_aliases),
            },
            sort_keys=True,
        )
        if key in deduped:
            clone_items_dropped += 1
            lineage[key].append(entry)
            continue
        deduped[key] = item
        lineage[key] = [entry]

    items = [
        replace(i, logical_lineage=tuple(lineage[i.item_context_digest])) for i in deduped.values()
    ]
    keep_ids = {i.item_id for i in items}
    truths = [tr for tr in truths if tr.item_id in keep_ids]
    per_cluster = [i.cluster_id for i in items]

    empty_msg = sum(1 for i in items if i.rater_context["item_step"]["message_is_empty"])
    census = {
        "trajectory_files_seen": len(paths),
        "sources_rejected": len(rejected),
        "rejection_reasons": sorted(rejected),
        "distinct_content_digests": len(by_sha),
        "duplicate_paths_dropped": duplicate_paths_dropped,
        "clone_items_dropped": clone_items_dropped,
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
        logical_lineage=item.logical_lineage,
        model_name=item.model_name,
        agent_name=item.agent_name,
        stratum=item.stratum,
        sampling_weight=weight,
        selection_arm=arm,
        cluster_id=item.cluster_id,
        logical_step_digest=item.logical_step_digest,
        item_context_digest=item.item_context_digest,
        context_completeness=item.context_completeness,
        rater_context=item.rater_context,
    )


# ---------------------------------------------------------------------------
# Ratings: separate typed sidecars. Items never mutate (B4).
# ---------------------------------------------------------------------------

REQUIRED_RATERS_PER_ITEM = 3

# Cluster adequacy, set by Tutor (wK:p4) power verdict 2026-08-28.
MIN_EFFECTIVE_CLUSTERS = 20.0
# At most this fraction of items may be context-INCOMPLETE before the package is
# unfit to label: a rater forced into INSUFFICIENT_CONTEXT on most items measures
# the builder, not the agent.
MAX_INCOMPLETE_CONTEXT_FRACTION = 0.20
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


REGISTRY_SCHEMA_VERSION = "goldset-rater-registry/v1"


class RegistryRejectedError(RuntimeError):
    """Raised when the qualified-rater registry cannot be trusted."""


KEYSTORE_SCHEMA_VERSION = "goldset-rater-keystore/v1"


def load_rater_keystore(keystore_path: Path | None) -> tuple[dict[str, str], list[str]]:
    """Load rater secrets from a SEPARATE, NEVER-EXPORTED keystore.

    Revision 6 put shared_secret inside the signed roster, so anyone who could
    read the roster could forge ratings - which defeats the signature entirely.
    The roster now carries key_id and qualification ONLY; secrets live here.
    """
    if keystore_path is None:
        return {}, ["KEYSTORE_ABSENT"]
    try:
        doc = json.loads(keystore_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"KEYSTORE_UNREADABLE: {exc}"]
    if doc.get("schema_version") != KEYSTORE_SCHEMA_VERSION:
        return {}, ["KEYSTORE_BAD_SCHEMA_VERSION"]
    keys = doc.get("keys") or {}
    if not isinstance(keys, dict):
        return {}, ["KEYSTORE_MALFORMED"]
    return {str(k): str(v) for k, v in keys.items() if k and v}, []


def registry_signing_payload(registry: Mapping[str, Any]) -> str:
    """Canonical bytes the registry authority signs."""
    return json.dumps(
        {
            "schema_version": registry.get("schema_version"),
            "authority_key_id": registry.get("authority_key_id"),
            "raters": registry.get("raters"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def sign_registry(registry: Mapping[str, Any], secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        registry_signing_payload(registry).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def load_rater_registry(
    registry_path: Path | None,
    authority_secret: str | None,
    keystore_path: Path | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Load an AUTHENTICATED qualified-rater roster plus a SEPARATE keystore.

    Returns (qualified_key_ids, keyring, problems). The roster is signed and
    carries key_id + qualification ONLY - never secrets. Secrets come from the
    keystore, which is never exported and never written into any artifact.
    A roster containing a secret is REJECTED outright.
    """
    if registry_path is None:
        return [], {}, ["REGISTRY_ABSENT"]
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], {}, [f"REGISTRY_UNREADABLE: {exc}"]
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        return [], {}, ["REGISTRY_BAD_SCHEMA_VERSION"]
    if authority_secret is None:
        return [], {}, ["REGISTRY_AUTHORITY_SECRET_NOT_SUPPLIED"]
    expected = sign_registry(registry, authority_secret)
    if not hmac.compare_digest(expected, str(registry.get("signature") or "")):
        return [], {}, ["REGISTRY_SIGNATURE_INVALID"]

    problems: list[str] = []
    secrets, keystore_problems = load_rater_keystore(keystore_path)
    problems.extend(keystore_problems)

    qualified: list[str] = []
    keyring: dict[str, str] = {}
    seen: set[str] = set()
    for entry in registry.get("raters") or []:
        entry = entry or {}
        if any(k in entry for k in ("shared_secret", "secret", "key", "private_key")):
            # A roster that carries secrets is not merely weak, it is wrong.
            return [], {}, [*problems, "REGISTRY_CONTAINS_SECRET_MATERIAL"]
        key_id = str(entry.get("key_id") or "").strip()
        if not key_id:
            problems.append("REGISTRY_ENTRY_INCOMPLETE")
            continue
        if not entry.get("qualified"):
            problems.append(f"REGISTRY_ENTRY_NOT_QUALIFIED: {key_id}")
            continue
        if key_id in seen:
            problems.append(f"REGISTRY_DUPLICATE_KEY_ID: {key_id}")
            continue
        seen.add(key_id)
        secret = secrets.get(key_id)
        if not secret:
            problems.append(f"KEYSTORE_MISSING_KEY: {key_id}")
            continue
        qualified.append(key_id)
        keyring[key_id] = secret
    return qualified, keyring, problems


def compute_item_context_digest(
    rater_context: Mapping[str, Any],
    *,
    cluster_id: str = "",
    step_index: int = -1,
) -> str:
    """Digest over trial identity, step ordinal, and EVERY rater-visible field.

    Two reasons identity is inside the digest:

    1. Binding only the current step's logical content left the task instruction
       and all prior observations unbound - alterable while every signature and
       readiness check still passed, so labels would silently answer a different
       question.
    2. Using step content alone as an IDENTITY wrongly merged distinct contexts.
       Measured: 5 digests collapsed 16 steps, worst case 6 steps across 6 DIFFERENT
       trials sharing one terminal message, plus consecutive indices 17/18 inside a
       single trial. Distinct trial or step ordinal must never merge; only genuine
       byte-identical copies of the SAME logical item may alias.
    """
    payload = {
        "cluster_id": cluster_id,
        "step_index": step_index,
        "rater_context": rater_context,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


RATING_CONTRACT_SCHEMA = "goldset-rating-contract/v1"


def compute_rating_contract_digest(
    items: Sequence[LabelItem],
    *,
    codebook_version: str,
    rating_schema_version: str,
    package_schema_version: str,
) -> str:
    """IMMUTABLE contract a rater signs. Computed BEFORE any intake.

    Why this exists: ratings previously had to bind `package_digest`, but that
    digest covers the whole serialized package INCLUDING readiness and rating
    summaries - so it changes as ratings arrive. Requiring a rating to sign it was
    circular: the value only exists after the ratings are counted.

    This digest covers exactly what a rater is shown and judged against, in order,
    plus the codebook and schema versions. Nothing downstream of intake can alter
    it, so it is stable for the whole labelling campaign. Artifact digests
    (`package_digest`, `build_id`) stay SEPARATE and are free to include readiness
    and rating summaries.
    """
    payload = {
        "contract_schema": RATING_CONTRACT_SCHEMA,
        "codebook_version": codebook_version,
        "rating_schema_version": rating_schema_version,
        "package_schema_version": package_schema_version,
        # Order matters: the ordered sequence of what raters see is part of the
        # contract, not merely the set.
        "ordered_items": [
            {"item_id": i.item_id, "item_context_digest": i.item_context_digest}
            for i in sorted(items, key=lambda x: x.item_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def compute_item_set_digest(items: Sequence[LabelItem], codebook_version: str) -> str:
    """Stable anchor ratings bind to. Changes on ANY recut that alters items.

    package_digest cannot serve: it is stamped at write time, after readiness, so
    binding to it is circular. The item-set digest is computable before readiness
    and is exactly what a replayed rating must not match across recuts.
    """
    return hashlib.sha256(
        json.dumps(
            {
                "codebook_version": codebook_version,
                "items": sorted((i.item_id, i.item_context_digest) for i in items),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


RATING_SIGNED_FIELDS = (
    "schema_version",
    "rating_contract_digest",
    "item_id",
    "item_context_digest",
    "rater_key_id",
    *HUMAN_JUDGED_FIELDS,
)


def rating_signing_payload(record: Mapping[str, Any]) -> str:
    """Canonical bytes a rater signs. Excludes the signature itself."""
    return json.dumps(
        {k: record.get(k) for k in RATING_SIGNED_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
    )


def sign_rating(record: Mapping[str, Any], secret: str) -> str:
    """HMAC-SHA256 over the canonical payload. Used by the ingest tool and tests."""
    return hmac.new(
        secret.encode("utf-8"),
        rating_signing_payload(record).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_rating_signature(record: Mapping[str, Any], keyring: Mapping[str, str]) -> bool:
    """Verify against a registered key. Absent key ID or bad MAC -> False."""
    key_id = record.get("rater_key_id")
    secret = keyring.get(str(key_id)) if key_id else None
    if not secret:
        return False
    supplied = str(record.get("signature") or "")
    return hmac.compare_digest(sign_rating(record, secret), supplied)


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
        entries = payload if isinstance(payload, list) else [payload]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                # A scalar or list entry previously crashed dict(record) with a
                # TypeError. Fail closed with a diagnostic instead.
                records.append(
                    {
                        "_invalid_entry": (
                            f"{path.name}[{index}]: expected object, got {type(entry).__name__}"
                        )
                    }
                )
                continue
            record = dict(entry)
            record["_source_file"] = path.name
            records.append(record)
    return records


def validate_rating(
    record: dict[str, Any],
    *,
    rating_contract_digest: str | None = None,
    context_digests: Mapping[str, str] | None = None,
    keyring: Mapping[str, str] | None = None,
) -> list[str]:
    """Validate one RatingRecord.

    A rater_id is NOT self-asserting evidence (security fix 3). A record must
    bind the package digest and the item's logical digest, and carry a signature
    verifiable against a registered qualified-rater key.
    """
    errors: list[str] = []
    if record.get("_invalid_file"):
        return [f"UNPARSEABLE_FILE:{record['_invalid_file']}"]
    if record.get("_invalid_entry"):
        return [f"MALFORMED_ENTRY:{record['_invalid_entry']}"]
    if record.get("schema_version") != RATING_SCHEMA_VERSION:
        errors.append("BAD_SCHEMA_VERSION")
    item_id = str(record.get("item_id") or "").strip()
    if not item_id:
        errors.append("MISSING_ITEM_ID")
    if not str(record.get("rater_key_id") or "").strip():
        errors.append("MISSING_RATER_KEY_ID")

    # Replay defence: a rating signed against a different item set is rejected
    # even when the individual item_id and logical digest still exist (P1).
    # Replay defence, non-circular: the contract digest is fixed before intake.
    if rating_contract_digest is None:
        errors.append("RATING_CONTRACT_DIGEST_NOT_ENFORCED")
    elif record.get("rating_contract_digest") != rating_contract_digest:
        errors.append("RATING_CONTRACT_DIGEST_MISMATCH")

    # Tamper defence: the rater signs the CONTEXT THEY SAW, so altering the task
    # instruction or any prior observation invalidates the record.
    if context_digests is None:
        errors.append("ITEM_CONTEXT_DIGEST_NOT_ENFORCED")
    elif item_id:
        expected = context_digests.get(item_id)
        if expected is None:
            errors.append("UNKNOWN_ITEM")
        elif record.get("item_context_digest") != expected:
            errors.append("ITEM_CONTEXT_DIGEST_MISMATCH")

    for field_name in HUMAN_JUDGED_FIELDS:
        value = record.get(field_name)
        if value is None:
            errors.append(f"MISSING_LABEL:{field_name}")
        elif value not in ALLOWED_VALUES[field_name]:
            errors.append(f"OUT_OF_ENUM:{field_name}={value!r}")

    # Fail closed: an absent keyring means the signature CANNOT be verified. That
    # is a rejection, never a skip.
    if not keyring:
        errors.append("SIGNATURE_UNVERIFIABLE_NO_KEYRING")
    elif not verify_rating_signature(record, keyring):
        errors.append("SIGNATURE_INVALID_OR_UNREGISTERED_KEY")
    return errors


def evaluate_readiness(
    items: Sequence[LabelItem],
    records: Sequence[dict[str, Any]],
    qualified_rater_ids: Sequence[str],
    *,
    rating_contract_digest: str | None = None,
    keyring: Mapping[str, str] | None = None,
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

    incomplete = sum(
        1 for item in items if item.context_completeness.get("builder_verdict") != "COMPLETE"
    )
    incomplete_fraction = (incomplete / len(items)) if items else 0.0
    if incomplete_fraction > MAX_INCOMPLETE_CONTEXT_FRACTION:
        blockers.append(
            f"CONTEXT_INCOMPLETE_TOO_HIGH: {incomplete}/{len(items)} "
            f"({incomplete_fraction:.1%}) > {MAX_INCOMPLETE_CONTEXT_FRACTION:.0%}"
        )
    qualified = set(qualified_rater_ids)
    item_ids = {i.item_id for i in items}

    invalid = 0
    unknown_item = 0
    duplicate_submissions = 0
    conflicting_submissions = 0
    by_item: dict[str, set[str]] = {}
    seen: dict[tuple[str, str], str] = {}
    context_digests = {i.item_id: i.item_context_digest for i in items}
    for record in records:
        errors = validate_rating(
            record,
            rating_contract_digest=rating_contract_digest,
            context_digests=context_digests,
            keyring=keyring,
        )
        if errors:
            invalid += 1
            continue
        if record["item_id"] not in item_ids:
            unknown_item += 1
            continue
        key = (record["item_id"], record["rater_key_id"])
        fingerprint = rating_signing_payload(record)
        if key in seen:
            # Append-only: one submission per (item, rater). A byte-identical
            # resubmission is a duplicate; a differing one is a conflict.
            if seen[key] == fingerprint:
                duplicate_submissions += 1
            else:
                conflicting_submissions += 1
            continue
        seen[key] = fingerprint
        by_item.setdefault(record["item_id"], set()).add(record["rater_key_id"])

    if len(qualified) < REQUIRED_RATERS_PER_ITEM:
        blockers.append(
            f"QUALIFIED_RATER_POOL_TOO_SMALL: have {len(qualified)}, "
            f"need >= {REQUIRED_RATERS_PER_ITEM}"
        )
    if invalid:
        blockers.append(f"INVALID_RATING_RECORDS: {invalid}")
    if unknown_item:
        blockers.append(f"RATINGS_FOR_UNKNOWN_ITEM: {unknown_item}")
    if duplicate_submissions:
        blockers.append(f"DUPLICATE_SUBMISSIONS: {duplicate_submissions}")
    if conflicting_submissions:
        blockers.append(f"CONFLICTING_SUBMISSIONS: {conflicting_submissions}")

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
        "context_adequacy": {
            "items_incomplete": incomplete,
            "items_total": len(items),
            "incomplete_fraction": round(incomplete_fraction, 4),
            "max_incomplete_fraction": MAX_INCOMPLETE_CONTEXT_FRACTION,
        },
        "required_unique_raters_per_item": REQUIRED_RATERS_PER_ITEM,
        "qualified_rater_pool_size": len(qualified),
        "valid_rating_records": sum(len(v) for v in by_item.values()),
        "authentication": {
            "rater_id_is_self_asserting": False,
            "requires": [
                "rating_contract_digest",
                "item_context_digest",
                "rater_key_id",
                "signature",
            ],
            "append_only_unique_key": "(item_id, rater_key_id)",
            "rating_contract_digest": rating_contract_digest,
            "rating_contract_schema": RATING_CONTRACT_SCHEMA,
            "replay_defence": (
                "Ratings bind rating_contract_digest, computed BEFORE intake over "
                "the ordered rater-visible contexts plus codebook and schema "
                "versions. It is immutable for the campaign, so the binding is not "
                "circular. package_digest and build_id are SEPARATE artifact "
                "digests and may include readiness and rating summaries."
            ),
        },
        "blockers": blockers,
    }


def build_package(
    runs_root: Path,
    *,
    core_n: int | None,
    boost_per_stratum: int,
    ratings_dir: Path | None,
    registry_path: Path | None = None,
    authority_secret: str | None = None,
    keystore_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    universe, truths, census = enumerate_universe(runs_root)
    selected = select_items(universe, core_n=core_n, boost_per_stratum=boost_per_stratum)
    keep = {i.item_id for i in selected}
    records = load_rating_records(ratings_dir)
    qualified, keyring, registry_problems = load_rater_registry(
        registry_path, authority_secret, keystore_path
    )
    # Never ship an item whose context we KNOW is incomplete and ask the rater to
    # flag it. Incomplete items are excluded from delivery and from readiness; they
    # remain recorded in the package for transparency.
    deliverable = [
        i for i in selected if i.context_completeness.get("builder_verdict") == "COMPLETE"
    ]
    excluded_incomplete = len(selected) - len(deliverable)
    rating_contract_digest = compute_rating_contract_digest(
        deliverable,
        codebook_version=TAXONOMY_VERSION,
        rating_schema_version=RATING_SCHEMA_VERSION,
        package_schema_version=SCHEMA_VERSION,
    )
    readiness = evaluate_readiness(
        deliverable,
        records,
        qualified_rater_ids=qualified,
        rating_contract_digest=rating_contract_digest,
        keyring=keyring or None,
    )
    readiness["context_diagnostic_2x2"] = context_diagnostic_2x2(deliverable, records)
    readiness["registry"] = {
        "source": str(registry_path) if registry_path else None,
        "qualified_key_ids": len(qualified),
        "problems": registry_problems,
    }
    if registry_problems:
        readiness["blockers"] = [
            *readiness["blockers"],
            *(f"REGISTRY: {p}" for p in registry_problems),
        ]
        readiness["readiness"] = "NOT_READY"

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
        "statistical_parameters_owned_by_tutor": {
            "decided_2026_08_28": {
                "primary_statistic": "gwet_ac1_multirater_nominal",
                "declared_universe_q": 12,
                "interval_method": "percentile_cluster_bootstrap",
                "bootstrap_resamples": 4000,
                "target_ci_half_width_95": 0.05,
                "complementary_statistics": [
                    "krippendorff_alpha_nominal",
                    "fleiss_kappa",
                    "pairwise_cohen_kappa",
                ],
                "cluster_unit": "cluster_id (canonical logical trajectory digest)",
                "prevalence_valid_core_required": True,
                "sampling_weights_required": True,
            },
            "still_null": {
                "acceptance_threshold": None,
                "adjudication_rule": None,
                "rater_qualification_criteria": None,
            },
            "note": (
                "acceptance_threshold is EXPLICITLY null by Tutor's decision, not "
                "an oversight. An interval will be reported without a pass/fail "
                "verdict until a threshold is justified."
            ),
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
        "n_deliverable": len(deliverable),
        "excluded_incomplete": excluded_incomplete,
        "deliverable_item_ids": sorted(i.item_id for i in deliverable),
        "items": [asdict(i) for i in selected],
        "readiness": readiness,
    }
    machine_truth = {
        "schema_version": "goldset-machine-truth/v1",
        "warning": "WITHHELD FROM RATERS. Scoring side only.",
        "truths": [asdict(t) for t in truths if t.item_id in keep],
    }
    return package, machine_truth


# ---------------------------------------------------------------------------
# Rater export bundle (security fix 1)
#
# "Withheld" was previously asserted by FILENAME while the truth file sat in the
# same directory and tests loaded both. Anyone handed the directory got the
# answers. The bundle below is a physically separate artifact that contains no
# truth and does not even name which field is the attention check.
# ---------------------------------------------------------------------------

FORBIDDEN_BUNDLE_PATTERNS = (
    "*machine_truth*",
    "*WITHHELD*",
    "*truth*",
    "*attention*",
    "*labeling_package*",
    "*registry*",
    "*keystore*",
    "*secret*",
    "*.key",
    "*.pem",
)


class BundleContaminationError(RuntimeError):
    """Raised when a rater bundle directory contains a forbidden artifact."""


def _assert_bundle_clean(bundle_dir: Path) -> None:
    """Recursive scan. A forbidden file one level down is just as readable."""
    offenders: set[str] = set()
    for pattern in FORBIDDEN_BUNDLE_PATTERNS:
        for path in bundle_dir.rglob(pattern):
            offenders.add(str(path.relative_to(bundle_dir)))
    # A symlink can point at a truth file outside the bundle entirely.
    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            offenders.add(f"SYMLINK:{path.relative_to(bundle_dir)}")
    if offenders:
        raise BundleContaminationError(
            f"forbidden artifacts present in rater bundle {bundle_dir}: {sorted(offenders)}"
        )


def context_diagnostic_2x2(
    items: Sequence[LabelItem], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Post-hoc builder verdict x rater sufficiency cross-tabulation.

    Only computable because the two signals are INDEPENDENT: the builder verdict
    is withheld from the bundle, so a rater choosing INSUFFICIENT_CONTEXT did so
    without being told what the builder concluded.

    The off-diagonal cells are the informative ones:
      COMPLETE   x INSUFFICIENT_CONTEXT -> the builder MISSED a defect
      INCOMPLETE x sufficient           -> the builder was over-strict
    """
    verdict_of = {i.item_id: i.context_completeness.get("builder_verdict") for i in items}
    cells = {
        ("COMPLETE", "sufficient"): 0,
        ("COMPLETE", "INSUFFICIENT_CONTEXT"): 0,
        ("INCOMPLETE", "sufficient"): 0,
        ("INCOMPLETE", "INSUFFICIENT_CONTEXT"): 0,
    }
    for record in records:
        builder = verdict_of.get(str(record.get("item_id")))
        if builder not in ("COMPLETE", "INCOMPLETE"):
            continue
        rater = (
            INSUFFICIENT_CONTEXT
            if any(record.get(field) == INSUFFICIENT_CONTEXT for field in HUMAN_JUDGED_FIELDS)
            else "sufficient"
        )
        cells[(builder, rater)] += 1
    return {
        "counts": {f"{b}|{r}": n for (b, r), n in sorted(cells.items())},
        "builder_missed_a_defect": cells[("COMPLETE", INSUFFICIENT_CONTEXT)],
        "builder_over_strict": cells[("INCOMPLETE", "sufficient")],
        "interpretation": (
            "COMPLETE x INSUFFICIENT_CONTEXT means the builder missed a defect it "
            "believed it had detected. INCOMPLETE x sufficient means the builder "
            "was over-strict. Valid ONLY because builder_verdict is withheld from "
            "the rater bundle."
        ),
    }


def build_rater_bundle(package: Mapping[str, Any]) -> dict[str, Any]:
    """Strip everything a rater must not see.

    Removes the attention-check field identity: telling raters which field is the
    check is itself a leak, even without its per-item answer.
    """
    # Strip the attention-check identity AND any prose revealing the
    # machine-truth mechanism. Telling raters a withheld truth exists, or which
    # field is cross-checked against it, is itself a leak.
    semantics = {
        k: v
        for k, v in package["taxonomy"].get("missing_data_semantics", {}).items()
        if k != "note"
    }
    taxonomy = {
        k: v
        for k, v in package["taxonomy"].items()
        if k not in ("attention_check_field", "missing_data_semantics")
    }
    if semantics:
        taxonomy["missing_data_semantics"] = semantics
    return {
        "schema_version": "goldset-rater-bundle/v1",
        # INFORMATIONAL ONLY - not signed. The signed binding is
        # rating_contract_digest, which is immutable for the campaign.
        "package_digest_informational_only": package["package_digest"],
        "rating_contract_digest": package["readiness"]["authentication"]["rating_contract_digest"],
        "taxonomy_version": package["taxonomy_version"],
        "codebook_version": package["taxonomy_version"],
        "rating_schema_version": package["rating_schema_version"],
        "taxonomy": taxonomy,
        "instructions_to_rater": {
            "required_fields": list(HUMAN_JUDGED_FIELDS),
            "every_field_offers": [CANNOT_JUDGE, INSUFFICIENT_CONTEXT],
            "cannot_judge_vs_insufficient_context": (
                f"{CANNOT_JUDGE}: you HAVE the context and the step is genuinely "
                f"ambiguous. {INSUFFICIENT_CONTEXT}: the context you need is absent "
                "or truncated in this bundle. Neither is penalised."
            ),
            "submission_binding": (
                "Each submission MUST carry, copied from this bundle: "
                "rating_contract_digest and the item's item_context_digest; "
                "plus your rater_key_id and an HMAC "
                "signature over the canonical payload. A rater_id alone is not "
                "accepted, and altering any part of the context you were shown "
                "invalidates the record."
            ),
        },
        "items": [
            {
                "item_id": item["item_id"],
                "item_context_digest": item["item_context_digest"],
                # builder_verdict and degraded_reasons are WITHHELD. Telling a
                # rater the builder already judged this context would prime
                # INSUFFICIENT_CONTEXT and destroy the 2x2 diagnostic, whose
                # whole value rests on the two signals being independent.
                "rater_context": item["rater_context"],
            }
            for item in package["items"]
            if item["item_id"] in set(package["deliverable_item_ids"])
        ],
    }


def export_rater_bundle(package: Mapping[str, Any], bundle_dir: Path) -> Path:
    """Write the rater bundle, refusing if the directory is contaminated."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    _assert_bundle_clean(bundle_dir)
    bundle = build_rater_bundle(package)
    out = bundle_dir / "rater_bundle.json"
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _assert_bundle_clean(bundle_dir)  # re-assert after write
    return out


# ---------------------------------------------------------------------------
# Paired atomic output (security fix 5)
#
# Revision 4 wrote the package and the truth file in a plain loop with no lock,
# no atomicity and no shared generation ID, so a concurrent reader could pair a
# NEW package with an OLD truth file and never detect it. Both artifacts now
# carry the same build_id and are renamed into place under one lock.
# ---------------------------------------------------------------------------

LOCK_NAME = ".goldset-build.lock"


class OutputPathError(RuntimeError):
    """Raised when output paths collide or a lock cannot be taken."""


class PairMismatchError(RuntimeError):
    """Raised when a package and truth file do not share a build_id."""


def _serialize(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


BUILD_ID_EXCLUDED_PACKAGE_KEYS = ("build_id", "package_digest")
BUILD_ID_EXCLUDED_TRUTH_KEYS = ("build_id",)


def compute_build_id(package: Mapping[str, Any], truth: Mapping[str, Any]) -> str:
    """Closed generation ID, recomputable from the WRITTEN pair.

    Formula, exactly:
        build_id = sha256(
            serialize(package minus {build_id, package_digest})
            + serialize(truth   minus {build_id})
        )
    package_digest is excluded because it is stamped AFTER build_id; including it
    made the public value unrecomputable from the artifacts on disk.
    """
    stripped_pkg = {k: v for k, v in package.items() if k not in BUILD_ID_EXCLUDED_PACKAGE_KEYS}
    stripped_truth = {k: v for k, v in truth.items() if k not in BUILD_ID_EXCLUDED_TRUTH_KEYS}
    return hashlib.sha256(
        (_serialize(stripped_pkg) + _serialize(stripped_truth)).encode("utf-8")
    ).hexdigest()


@contextmanager
def _build_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_NAME
    with open(lock_path, "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OutputPathError(f"CONCURRENT_BUILD_IN_PROGRESS: {lock_path} is held") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    """Write to a distinct temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_paired_outputs(
    package: dict[str, Any],
    truth: dict[str, Any],
    package_path: Path,
    truth_path: Path,
) -> str:
    """Stamp a shared build_id and rename both artifacts into place atomically."""
    resolved = [package_path.resolve(strict=False), truth_path.resolve(strict=False)]
    if resolved[0] == resolved[1]:
        raise OutputPathError(f"IDENTICAL_OUTPUT_PATHS: {package_path}")

    package_dir = resolved[0].parent
    truth_dir = resolved[1].parent

    build_id = compute_build_id(package, truth)
    package["build_id"] = build_id
    truth["build_id"] = build_id
    package["package_digest"] = hashlib.sha256(
        _serialize({k: v for k, v in package.items() if k != "package_digest"}).encode("utf-8")
    ).hexdigest()

    # A single lock on the package directory does not serialise a peer writing the
    # truth file in a DIFFERENT directory, so sequential replaces could tear.
    # Take locks in a deterministic order over every distinct directory.
    lock_dirs = sorted({package_dir, truth_dir}, key=str)
    with ExitStack() as stack:
        for directory in lock_dirs:
            stack.enter_context(_build_lock(directory))
        _atomic_write(package_path, _serialize(package))
        _atomic_write(truth_path, _serialize(truth))

    # Post-write verification: prove the pair on disk is coherent before returning.
    written_pkg, written_truth = load_paired_artifacts(package_path, truth_path)
    if written_pkg["build_id"] != build_id or written_truth["build_id"] != build_id:
        raise PairMismatchError(f"POST_WRITE_VERIFICATION_FAILED: expected build_id {build_id}")
    return build_id


def load_paired_artifacts(package_path: Path, truth_path: Path) -> tuple[dict, dict]:
    """Consumer-side loader. REFUSES a mismatched pair (security fix 5)."""
    package = json.loads(package_path.read_text(encoding="utf-8"))
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    # Recompute from actual canonical content: a stored ID proves nothing.
    recomputed_pkg_digest = hashlib.sha256(
        _serialize({k: v for k, v in package.items() if k != "package_digest"}).encode("utf-8")
    ).hexdigest()
    if package.get("package_digest") != recomputed_pkg_digest:
        raise PairMismatchError(
            f"PACKAGE_DIGEST_RECOMPUTE_MISMATCH: stored "
            f"{package.get('package_digest')!r} != {recomputed_pkg_digest!r}"
        )
    recomputed_build_id = compute_build_id(package, truth)
    if package.get("build_id") != recomputed_build_id:
        raise PairMismatchError(
            f"BUILD_ID_RECOMPUTE_MISMATCH: stored {package.get('build_id')!r} "
            f"!= {recomputed_build_id!r}"
        )
    for item in package.get("items", []):
        expected = compute_item_context_digest(
            item["rater_context"],
            cluster_id=item["cluster_id"],
            step_index=item["step_index"],
        )
        if item.get("item_context_digest") != expected:
            raise PairMismatchError(f"ITEM_CONTEXT_DIGEST_RECOMPUTE_MISMATCH: {item['item_id']}")
    if package.get("build_id") != truth.get("build_id"):
        raise PairMismatchError(
            f"BUILD_ID_MISMATCH: package={package.get('build_id')!r} "
            f"truth={truth.get('build_id')!r}"
        )
    if not package.get("build_id"):
        raise PairMismatchError("BUILD_ID_ABSENT")
    return package, truth


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the frozen ready-for-human-labeling gold-set package"
    )
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--machine-truth-out", type=Path, required=True)
    parser.add_argument("--ratings-dir", type=Path, default=None)
    parser.add_argument(
        "--rater-registry",
        type=Path,
        default=None,
        help="signed qualified-rater registry (goldset-rater-registry/v1)",
    )
    parser.add_argument(
        "--rater-keystore",
        type=Path,
        default=None,
        help="rater secret keystore (goldset-rater-keystore/v1); NEVER exported",
    )
    parser.add_argument(
        "--registry-authority-secret-env",
        default="GOLDSET_REGISTRY_AUTHORITY_SECRET",
        help="env var holding the registry authority secret",
    )
    parser.add_argument("--core-n", type=int, default=None)
    parser.add_argument("--boost-per-stratum", type=int, default=0)
    parser.add_argument(
        "--export-rater-bundle",
        type=Path,
        default=None,
        help="directory to write the rater-safe bundle into (no truth artifacts)",
    )
    args = parser.parse_args()

    package, truth = build_package(
        args.runs_root,
        core_n=args.core_n,
        boost_per_stratum=args.boost_per_stratum,
        ratings_dir=args.ratings_dir,
        registry_path=args.rater_registry,
        authority_secret=os.environ.get(args.registry_authority_secret_env),
        keystore_path=args.rater_keystore,
    )
    try:
        build_id = write_paired_outputs(package, truth, args.out, args.machine_truth_out)
    except OutputPathError as exc:
        print(f"REFUSED {exc}")
        return 2

    if args.export_rater_bundle is not None:
        try:
            bundle_path = export_rater_bundle(package, args.export_rater_bundle)
        except BundleContaminationError as exc:
            print(f"REFUSED {exc}")
            return 2
        print(f"wrote {bundle_path} (RATER-SAFE, no truth artifacts)")

    file_sha256 = hashlib.sha256(args.out.read_bytes()).hexdigest()
    census = package["census"]
    print(f"wrote {args.out}")
    print(f"wrote {args.machine_truth_out} (WITHHELD)")
    print(f"build_id {build_id}")
    print(f"labeling_package_file_sha256 {file_sha256}")
    print(f"package_digest (in-band)     {package['package_digest']}")
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
