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
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
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


# Deterministic precedence for the ONE canonical primary rejection reason per
# record. Reasons are non-exclusive (a replayed contract digest also invalidates
# the signature), so a flat reason tally cannot reconcile with records_rejected.
# The primary tally does reconcile, exactly; all_reasons is published alongside and
# explicitly labelled non-exclusive.
REJECTION_PRECEDENCE = (
    "MALFORMED_ENTRY",
    "UNPARSEABLE_FILE",
    "BAD_SCHEMA_VERSION",
    "MISSING_ITEM_ID",
    "MISSING_RATER_KEY_ID",
    "RATER_KEY_NOT_QUALIFIED",
    "SIGNATURE_UNVERIFIABLE_NO_KEYRING",
    "SIGNATURE_INVALID_OR_UNREGISTERED_KEY",
    "RATING_CONTRACT_DIGEST_NOT_ENFORCED",
    "RATING_CONTRACT_DIGEST_MISMATCH",
    "ITEM_CONTEXT_DIGEST_NOT_ENFORCED",
    "ITEM_CONTEXT_DIGEST_MISMATCH",
    "UNKNOWN_ITEM",
    "UNKNOWN_ITEM_ID",
    "DUPLICATE_SUBMISSION",
    "CONFLICTING_SUBMISSION",
)


def primary_rejection_reason(reasons: Sequence[str]) -> str:
    """Single canonical reason, by declared precedence. Never ambiguous."""
    categories = {r.split(":", 1)[0] for r in reasons}
    for candidate in REJECTION_PRECEDENCE:
        if candidate in categories:
            return candidate
    return "OTHER"


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
    if not isinstance(doc, dict):
        return {}, [f"KEYSTORE_NOT_AN_OBJECT: got {type(doc).__name__}"]
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
    if not isinstance(registry, dict):
        return [], {}, [f"REGISTRY_NOT_AN_OBJECT: got {type(registry).__name__}"]
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
    raters = registry.get("raters")
    if not isinstance(raters, list):
        return [], {}, [*problems, f"REGISTRY_RATERS_NOT_A_LIST: got {type(raters).__name__}"]
    for position, entry in enumerate(raters):
        if not isinstance(entry, dict):
            problems.append(
                f"REGISTRY_ENTRY_NOT_AN_OBJECT: index {position} is {type(entry).__name__}"
            )
            continue
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


RATING_SIGNED_FIELDS = (
    "schema_version",
    "rating_contract_digest",
    "item_id",
    "item_context_digest",
    "rater_key_id",
    # Correction intent MUST be signed, or a proxy can inject or alter it. The
    # field is included whether it is null or set, so its absence is also signed.
    "supersedes",
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


def load_intake(
    ratings_dir: Path | None,
    *,
    rating_contract_digest: str,
    context_digests: Mapping[str, str],
    keyring: Mapping[str, str],
    qualified_rater_ids: Collection[str],
    anchor_root: Path | None = None,
    anchor_secret: str | None = None,
    repair: bool = False,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Production intake. LEDGER ONLY - there is no loose-JSON path.

    Returns (records, intake_mode, problems). Problems become readiness blockers
    rather than exceptions, so a refused intake blocks the package instead of
    crashing the build.

    THE LEGACY GLOB PATH IS GONE, and its removal is the control. While it
    existed, intake chose between paths by looking for `head.json` and
    `ledger.jsonl`, so deleting those two markers downgraded a ledger to the glob
    path and BYPASSED the mandatory anchor entirely: a flat directory of record
    files was ingested with no anchor and no problem reported. Any
    marker-sniffing heuristic is bypassable by removing the markers, so the
    choice itself had to go rather than the heuristic be improved.

    Supplying `ratings_dir` therefore means "here is a complete, anchored
    ledger". Anything less is refused. No `ratings_dir` means zero ratings.
    """
    problems: list[str] = []
    if ratings_dir is None:
        return [], "no_ratings_dir", problems
    if not ratings_dir.is_dir():
        return [], "ledger", [f"LEDGER_DIR_ABSENT: {ratings_dir}"]

    # A complete ledger, demanded explicitly rather than sniffed. A missing part
    # is a refusal, never a downgrade.
    for required in ("head.json", "ledger.jsonl"):
        if not (ratings_dir / required).is_file():
            problems.append(
                f"LEDGER_INCOMPLETE: {required} is absent; a ratings directory must "
                f"be a complete append-only ledger. There is no loose-JSON intake."
            )
    if problems:
        return [], "ledger", problems

    try:
        records = load_ledger(ratings_dir, repair=repair)
    except LedgerRecoverableError as exc:
        # Actionable, and never repaired implicitly: an interrupted append is a
        # state an operator should acknowledge, not something a build silently
        # rewrites underneath them.
        return [], "ledger", [f"LEDGER_NEEDS_REPAIR: {exc}"]
    except INTAKE_FAILURE_TYPES as exc:
        return [], "ledger", [f"LEDGER_INVALID: {type(exc).__name__}: {exc}"]

    if records:
        root = DEFAULT_ANCHOR_ROOT if anchor_root is None else anchor_root
        if anchor_secret is None:
            problems.append(
                "LEDGER_ANCHOR_MISSING: a nonempty ledger intake requires a "
                "coordinator-signed external head anchor"
            )
        else:
            try:
                # LOOKED UP, never supplied: keyed by the contract digest under a
                # fixed coordinator root. A caller-selectable path let anyone who
                # could influence the invocation present a stale anchor.
                anchor = resolve_anchor(
                    root,
                    rating_contract_digest=rating_contract_digest,
                    ratings_dir=ratings_dir,
                )
                verify_against_anchor(
                    ratings_dir,
                    anchor,
                    anchor_secret=anchor_secret,
                    rating_contract_digest=rating_contract_digest,
                )
            except LedgerError as exc:
                problems.append(f"LEDGER_ANCHOR_REJECTED: {exc}")
    if problems:
        # Fail closed: an unanchored or forked ledger yields NO ratings.
        return [], "ledger", problems
    try:
        resolved = effective_ratings(
            records,
            rating_contract_digest=rating_contract_digest,
            context_digests=context_digests,
            keyring=keyring,
            qualified_rater_ids=qualified_rater_ids,
        )
    except INTAKE_FAILURE_TYPES as exc:
        # Intake is a trust boundary and must NEVER raise: a hostile or corrupt
        # ledger has to produce a NOT_READY package, not a crashed build, or a
        # single malformed record becomes a denial of service. Record content is
        # arbitrary once it is not ours - a list where a rater id belongs makes a
        # keyring lookup raise TypeError, for instance - so the exception type is
        # named in the diagnostic rather than guessed at in advance.
        return (
            [],
            "ledger",
            [f"LEDGER_RESOLUTION_FAILED: {type(exc).__name__}: {exc}"],
        )
    return resolved, "ledger", problems


def validate_rating(
    record: dict[str, Any],
    *,
    rating_contract_digest: str | None = None,
    context_digests: Mapping[str, str] | None = None,
    keyring: Mapping[str, str] | None = None,
    qualified_rater_ids: Collection[str] | None = None,
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
    key_id = str(record.get("rater_key_id") or "").strip()
    if not key_id:
        errors.append("MISSING_RATER_KEY_ID")
    # Qualification is an ACCEPTANCE criterion, not merely a readiness blocker: a
    # signature-valid record from an unqualified key previously reached the
    # accepted set and therefore the diagnostic.
    if qualified_rater_ids is None:
        errors.append("QUALIFICATION_NOT_ENFORCED")
    elif key_id and key_id not in qualified_rater_ids:
        errors.append(f"RATER_KEY_NOT_QUALIFIED: {key_id}")

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
    intake_mode: str = "unspecified",
    intake_problems: Sequence[str] = (),
) -> dict[str, Any]:
    """Fail-closed. Validates labels, not merely the presence of rater IDs (B4).

    Also gates on cluster adequacy: a package whose design effect cannot support
    an agreement interval must not be labelled regardless of rater supply.
    """
    blockers: list[str] = []
    # An unanchored, forked or invalid ledger BLOCKS readiness. It cannot be a
    # warning: the whole point of the anchor is that a rolled-back ledger looks
    # locally valid, so a build that proceeds would silently drop ratings.
    blockers.extend(intake_problems)
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
    accepted: list[dict[str, Any]] = []
    primary_reasons: dict[str, int] = {}
    all_reasons: dict[str, int] = {}

    def _reject(reasons: Sequence[str]) -> None:
        primary = primary_rejection_reason(reasons)
        primary_reasons[primary] = primary_reasons.get(primary, 0) + 1
        for reason in {r.split(":", 1)[0] for r in reasons}:
            all_reasons[reason] = all_reasons.get(reason, 0) + 1

    context_digests = {i.item_id: i.item_context_digest for i in items}
    for record in records:
        errors = validate_rating(
            record,
            rating_contract_digest=rating_contract_digest,
            context_digests=context_digests,
            keyring=keyring,
            qualified_rater_ids=qualified,
        )
        if errors:
            invalid += 1
            _reject(errors)
            continue
        if record["item_id"] not in item_ids:
            unknown_item += 1
            _reject(["UNKNOWN_ITEM_ID"])
            continue
        key = (record["item_id"], record["rater_key_id"])
        fingerprint = rating_signing_payload(record)
        if key in seen:
            # Append-only: one submission per (item, rater). A byte-identical
            # resubmission is a duplicate; a differing one is a conflict.
            label = "DUPLICATE_SUBMISSION" if seen[key] == fingerprint else "CONFLICTING_SUBMISSION"
            if seen[key] == fingerprint:
                duplicate_submissions += 1
            else:
                conflicting_submissions += 1
            _reject([label])
            continue
        seen[key] = fingerprint
        accepted.append(dict(record))
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
        "rating_intake": {
            "intake_mode": intake_mode,
            "records_seen": len(records),
            "records_accepted": len(accepted),
            "records_rejected": len(records) - len(accepted),
            "primary_rejection_reasons": dict(sorted(primary_reasons.items())),
            "all_rejection_reasons_non_exclusive": dict(sorted(all_reasons.items())),
            "records_with_reason": sum(primary_reasons.values()),
            "reconciliation": (
                "records_seen == records_accepted + records_rejected, and "
                "sum(primary_rejection_reasons) == records_rejected == "
                "records_with_reason. all_rejection_reasons_non_exclusive may sum "
                "HIGHER because one record can fail several checks - a replayed "
                "contract digest also invalidates the signature."
            ),
            "note": (
                "Only ACCEPTED records reach any diagnostic - validated, "
                "signature-verified against a qualified key, and bound to the "
                "rating contract. Rejected records are counted here and nowhere "
                "else, so a forged or unsigned submission cannot enter a "
                "diagnostic without first passing validation."
            ),
        },
        "context_diagnostic_2x2": context_diagnostic_2x2(items, accepted),
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


# ---------------------------------------------------------------------------
# Append-only rating ledger
#
# Loose JSON files in a directory are not an append-only intake: they can be
# overwritten, deleted, truncated or replayed with no trace. This is a real log -
# immutable content-addressed records, a fsync'd hash chain, and a head manifest,
# with load-time verification of both the chain and every record's inclusion.
# Corrections APPEND a superseding record; nothing is ever replaced.
# ---------------------------------------------------------------------------

LEDGER_SCHEMA = "goldset-rating-ledger/v1"
LEDGER_ANCHOR_SCHEMA = "goldset-ledger-anchor/v2"

# FIXED coordinator-controlled anchor root. Production NEVER takes an anchor path
# from the caller: an attacker who can influence the build invocation could
# otherwise point it at any stale anchor and replay an old head. Tests inject an
# authority root explicitly; the production default is fixed and fail-closed.
DEFAULT_ANCHOR_ROOT = Path.home() / ".goldset" / "anchors"

# SCOPE OF THE GUARANTEE, stated honestly.
#
# The ledger is LOCALLY TAMPER-EVIDENT: any edit, deletion or reordering that
# leaves the chain or the head manifest inconsistent is detected at load.
#
# It is NOT ROLLBACK-PROOF on its own. An attacker who truncates ledger.jsonl AND
# rewrites head.json consistently produces a shorter but internally valid ledger.
# Detecting that requires an EXTERNAL ANCHOR - a head hash published somewhere the
# attacker does not control. `verify_against_anchor` accepts such an anchor; until
# one is published, truncation resistance MUST NOT be claimed.
LEDGER_ANCHOR_NOTE = (
    "locally tamper-evident; rollback/truncation detectable only against an external head anchor"
)
GENESIS_HASH = "0" * 64


def _assert_coordinator_owned(path: Path, what: str) -> None:
    """The anchor authority must be owned by us and not writable by anyone else.

    A symlink, a foreign owner, or a group/world-writable mode all mean the
    "external" anchor is not actually outside the writer's control, which is the
    entire property the anchor exists to provide.
    """
    if path.is_symlink():
        raise LedgerError(f"{what}_IS_SYMLINK: {path}")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise LedgerError(f"{what}_ABSENT: {path}") from exc
    except OSError as exc:
        raise LedgerError(f"{what}_UNREADABLE: {path}: {exc.strerror}") from exc
    if info.st_uid != os.getuid():
        raise LedgerError(f"{what}_FOREIGN_OWNER: {path} is owned by uid {info.st_uid}")
    if info.st_mode & 0o022:
        raise LedgerError(
            f"{what}_GROUP_OR_WORLD_WRITABLE: {path} has mode {oct(info.st_mode & 0o777)}"
        )


def publish_anchor(
    anchor_root: Path,
    ledger_dir: Path,
    *,
    rating_contract_digest: str,
    secret: str,
) -> dict[str, Any]:
    """Coordinator action: publish the CURRENT head and record it monotonically.

    Called after each accepted batch. The append-only log is what makes a stale
    anchor detectable: rolling back the anchor alone is no longer enough, because
    the highest count ever published is recorded beside it.
    """
    anchor_root.mkdir(parents=True, exist_ok=True)
    head_hash, count = ledger_head(ledger_dir)
    anchor = sign_ledger_anchor(
        head_hash,
        count,
        secret,
        ledger=ledger_id(ledger_dir),
        rating_contract_digest=rating_contract_digest,
    )
    log = anchor_root / f"{rating_contract_digest}.anchor.log"
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"entry_count": count, "head_hash": head_hash}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _atomic_write(
        anchor_root / f"{rating_contract_digest}.anchor.json",
        json.dumps(anchor, indent=2, sort_keys=True) + "\n",
    )
    return anchor


def highest_published_count(anchor_root: Path, rating_contract_digest: str) -> int:
    """Highest entry_count ever published for this campaign, from the log."""
    log = anchor_root / f"{rating_contract_digest}.anchor.log"
    if not log.exists():
        return 0
    _assert_coordinator_owned(log, "ANCHOR_LOG")
    highest = 0
    for line in _read_text_or_raise(log, "ANCHOR_LOG").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ANCHOR_LOG_MALFORMED_JSON: {log}") from exc
        count = entry.get("entry_count") if isinstance(entry, Mapping) else None
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise LedgerError(f"ANCHOR_LOG_COUNT_INVALID: {count!r}")
        highest = max(highest, count)
    return highest


def resolve_anchor(
    anchor_root: Path,
    *,
    rating_contract_digest: str,
    ratings_dir: Path,
) -> Mapping[str, Any]:
    """Load the current anchor from the coordinator root, keyed by the contract.

    The anchor is looked up rather than supplied, and the root must live OUTSIDE
    the rater-writable ratings tree - an anchor a rater can rewrite anchors
    nothing.
    """
    root = anchor_root.resolve()
    ratings = ratings_dir.resolve()
    if root == ratings or root.is_relative_to(ratings) or ratings.is_relative_to(root):
        raise LedgerError(
            f"ANCHOR_ROOT_INSIDE_RATINGS_TREE: {root} overlaps {ratings}; the "
            f"anchor authority must be outside the writable ratings tree"
        )
    _assert_coordinator_owned(anchor_root, "ANCHOR_ROOT")
    if not anchor_root.is_dir():
        raise LedgerError(f"ANCHOR_ROOT_NOT_A_DIRECTORY: {anchor_root}")
    anchor_file = anchor_root / f"{rating_contract_digest}.anchor.json"
    _assert_coordinator_owned(anchor_file, "ANCHOR_FILE")
    anchor = _read_ledger_json(anchor_file, "ANCHOR")
    # MONOTONICITY. Exact head equality alone cannot distinguish a genuine
    # 3-entry ledger from a 6-entry ledger truncated to 3 and presented with a
    # re-signed 3-entry anchor. The append-only publication log can: an anchor
    # below the highest count ever published is a rollback.
    highest = highest_published_count(anchor_root, rating_contract_digest)
    claimed = anchor.get("entry_count") if isinstance(anchor, Mapping) else None
    if isinstance(claimed, int) and not isinstance(claimed, bool) and claimed < highest:
        raise LedgerError(
            f"ANCHOR_ROLLED_BACK: anchor claims {claimed} entries but "
            f"{highest} were published for this campaign"
        )
    return anchor


def _read_text_or_raise(path: Path, what: str) -> str:
    """Read UTF-8 text, funnelling every failure into LedgerError."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LedgerError(f"{what}_FILE_ABSENT: {path}") from exc
    except UnicodeDecodeError as exc:
        raise LedgerError(f"{what}_NOT_UTF8: {path}") from exc
    except OSError as exc:
        raise LedgerError(f"{what}_UNREADABLE: {path}: {exc.strerror}") from exc


def _read_ledger_json(path: Path, what: str) -> Any:
    """Read and parse JSON on the ledger path, FAIL-CLOSED as a LedgerError.

    Every read failure mode is funnelled into one exception type so intake can
    convert it into a readiness diagnostic. Previously a malformed head manifest
    escaped as a raw JSONDecodeError and crashed the build instead of refusing
    it, which turns a corrupt or deleted file into a denial of service rather
    than a NOT_READY package.
    """
    text = _read_text_or_raise(path, what)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what}_MALFORMED_JSON: {path}: {exc.msg}") from exc


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


class LedgerError(RuntimeError):
    """Raised when the rating ledger is not append-only-consistent."""


class LedgerRecoverableError(LedgerError):
    """Raised for an interrupted append that is deterministically repairable.

    The append sequence is: write the record file, append the log entry, update
    the head manifest. A crash between those steps used to brick the ledger
    permanently - an orphan record raised LEDGER_RECORDS_NOT_INCLUDED and a log
    tail past the head raised LEDGER_HEAD_MISMATCH, with no way forward. Both
    states are now classified, and repaired only on explicit request.
    """


# Everything intake converts into a readiness diagnostic instead of raising.
# Listed explicitly rather than caught as bare Exception, so a genuine defect in
# our own code still surfaces as a crash.
# Every RatingRecord field that MUST be a string for the record to be structurally
# well-formed. `supersedes` is checked separately: it is str-or-None.
_MUST_BE_STRING = (
    "schema_version",
    "rating_contract_digest",
    "item_id",
    "item_context_digest",
    "rater_key_id",
    "signature",
    *HUMAN_JUDGED_FIELDS,
)

INTAKE_FAILURE_TYPES = (
    LedgerError,
    OSError,
    UnicodeDecodeError,
    json.JSONDecodeError,
    TypeError,
    AttributeError,
    ValueError,
)


def _record_id(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in ("record_id",)}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _entry_hash(previous: str, record_id: str, created_at: str) -> str:
    return hashlib.sha256(f"{previous}|{record_id}|{created_at}".encode()).hexdigest()


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ledger_markers_present(ledger_dir: Path) -> bool:
    """True when anything indicates a ledger lives here."""
    if (ledger_dir / "ledger.jsonl").exists() or (ledger_dir / "head.json").exists():
        return True
    records = ledger_dir / "records"
    return records.is_dir() and any(records.glob("*.json"))


def ledger_head(ledger_dir: Path) -> tuple[str, int]:
    """Current chain head and record count from the manifest.

    FAILS CLOSED when ledger markers exist but the head manifest does not: a
    missing head with records on disk is a removed manifest, not an empty ledger,
    and returning genesis would silently accept it.
    """
    manifest = ledger_dir / "head.json"
    if not manifest.is_file():
        if ledger_markers_present(ledger_dir):
            raise LedgerError(
                "HEAD_MANIFEST_MISSING: ledger markers are present but head.json is "
                "absent; an empty ledger has no records and no log"
            )
        return GENESIS_HASH, 0
    doc = _read_ledger_json(manifest, "HEAD")
    if not isinstance(doc, dict):
        raise LedgerError(f"HEAD_MANIFEST_NOT_AN_OBJECT: got {type(doc).__name__}")
    if doc.get("schema") != LEDGER_SCHEMA:
        raise LedgerError(f"HEAD_SCHEMA_UNSUPPORTED: {doc.get('schema')!r}")
    head = doc.get("head_hash")
    count = doc.get("count")
    if not _is_sha256(head):
        raise LedgerError(f"HEAD_HASH_MALFORMED: {head!r}")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise LedgerError(f"HEAD_COUNT_NOT_A_NON_NEGATIVE_INT: {count!r}")
    return head, count


def _supersedes_chain_ok(start: str, by_id: Mapping[str, Mapping[str, Any]]) -> bool:
    """Walk the supersedes chain from `start`; False when it revisits a node."""
    seen: set[str] = set()
    cursor: str | None = start
    while cursor:
        if cursor in seen:
            return False
        seen.add(cursor)
        node = by_id.get(cursor)
        nxt = node.get("supersedes") if node else None
        cursor = str(nxt) if nxt else None
    return True


def append_rating_record(
    ledger_dir: Path,
    record: Mapping[str, Any],
    *,
    created_at: str,
    rating_contract_digest: str,
    context_digests: Mapping[str, str],
    keyring: Mapping[str, str],
    qualified_rater_ids: Collection[str],
) -> dict[str, Any]:
    """Append one AUTHENTICATED immutable record. O_EXCL, under lock, fsync'd.

    Correction intent is read SOLELY from the signed record. There is deliberately
    no `supersedes` parameter: while one existed, every authorization check was
    gated on it, so a caller who prepared a record carrying a signed
    `supersedes` and appended with the default `None` skipped the cross-item,
    cross-rater and already-superseded checks entirely, and the effective view -
    which reads the stored field - dropped the victim. Two sources of authority
    for one decision is the defect; there is now one.

    The record is also VALIDATED here, against trusted verifier context, before
    acceptance. Without that, an unauthenticated spoof could suppress a valid
    rating: append performed no signature check, so a record signed with any
    secret at all was written, resolution dropped the victim, and the spoof was
    then rejected downstream - destroying the honest rating and leaving nothing.
    """
    ledger_dir.mkdir(parents=True, exist_ok=True)
    records_dir = ledger_dir / "records"
    records_dir.mkdir(exist_ok=True)

    problems = validate_rating(
        dict(record),
        rating_contract_digest=rating_contract_digest,
        context_digests=context_digests,
        keyring=keyring,
        qualified_rater_ids=qualified_rater_ids,
    )
    if problems:
        raise LedgerError(f"RECORD_REJECTED_AT_APPEND: {sorted(problems)}")

    with _build_lock(ledger_dir):
        previous, count = ledger_head(ledger_dir)

        # Content addressing alone cannot stop replay: previous_entry_hash is part
        # of the record, so the same rating appended at a new chain position gets a
        # DIFFERENT record_id and O_EXCL never collides. Rating identity therefore
        # needs its own append-only-unique constraint, and a second submission for
        # one (item, rater) is admissible ONLY as an explicit correction.
        # Walk directly: the exclusive build lock is already held, and taking the
        # shared read lock on the same lock file from a second descriptor would
        # self-deadlock.
        existing, _ = _walk_ledger(ledger_dir)
        identity = (str(record.get("item_id")), str(record.get("rater_key_id")))
        prior = [
            r for r in existing if (str(r.get("item_id")), str(r.get("rater_key_id"))) == identity
        ]
        superseded_ids = {str(r["supersedes"]) for r in existing if r.get("supersedes")}
        live_prior = [r for r in prior if str(r.get("record_id")) not in superseded_ids]

        raw_supersedes = record.get("supersedes")
        supersedes = str(raw_supersedes) if raw_supersedes else None

        if live_prior and not supersedes:
            raise LedgerError(
                f"RATING_ALREADY_PRESENT: {identity[0]}/{identity[1]} already has a "
                f"live record; a correction must set supersedes"
            )
        # UNCONDITIONAL: reached whenever the signed record claims a correction.
        if supersedes:
            by_id = {str(r.get("record_id")): r for r in existing}
            target = by_id.get(supersedes)
            if target is None:
                raise LedgerError(f"SUPERSEDES_UNKNOWN_RECORD: {supersedes}")
            if supersedes in superseded_ids:
                raise LedgerError(f"SUPERSEDES_ALREADY_SUPERSEDED: {supersedes}")
            # A correction may only replace the SAME rater's rating of the SAME
            # item under the SAME contract. Without this, supersedes is a deletion
            # primitive: pointing it at another rater's record removed that record
            # from the effective view.
            if str(target.get("item_id")) != identity[0]:
                raise LedgerError(
                    f"SUPERSEDES_CROSS_ITEM: target item {target.get('item_id')!r} "
                    f"!= {identity[0]!r}"
                )
            if str(target.get("rater_key_id")) != identity[1]:
                raise LedgerError(
                    f"SUPERSEDES_CROSS_RATER: target rater "
                    f"{target.get('rater_key_id')!r} != {identity[1]!r}"
                )
            if str(target.get("rating_contract_digest")) != str(
                record.get("rating_contract_digest")
            ):
                raise LedgerError("SUPERSEDES_CROSS_CONTRACT: target signed another contract")
            if not _supersedes_chain_ok(supersedes, by_id):
                raise LedgerError(f"SUPERSEDES_CYCLE: chain from {supersedes} revisits a record")

        stored = dict(record)
        stored["created_at"] = created_at
        stored["previous_entry_hash"] = previous
        rid = _record_id(stored)
        stored["record_id"] = rid

        target_path = records_dir / f"{rid}.json"
        payload = json.dumps(stored, indent=2, sort_keys=True) + "\n"
        try:
            fd = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
        except FileExistsError as exc:
            raise LedgerError(f"RECORD_ALREADY_EXISTS: {rid}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        entry_hash = _entry_hash(previous, rid, created_at)
        entry = {
            "schema": LEDGER_SCHEMA,
            "seq": count + 1,
            "record_id": rid,
            "created_at": created_at,
            "previous_entry_hash": previous,
            "entry_hash": entry_hash,
            "supersedes": supersedes,
        }
        with open(ledger_dir / "ledger.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        manifest = {
            "schema": LEDGER_SCHEMA,
            "head_hash": entry_hash,
            "count": count + 1,
        }
        _atomic_write(
            ledger_dir / "head.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        _fsync_path(ledger_dir)
        return stored


def _walk_ledger(
    ledger_dir: Path, *, repair: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify the chain and return (records, per-position entry hashes).

    The entry hashes are what an external anchor is compared against, so the walk
    exposes them rather than discarding them.
    """
    log = ledger_dir / "ledger.jsonl"
    if not log.is_file():
        if ledger_markers_present(ledger_dir):
            raise LedgerError(
                "LEDGER_LOG_MISSING: ledger markers are present but ledger.jsonl is absent"
            )
        return [], []
    entries: list[dict[str, Any]] = []
    try:
        log_text = log.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerError(f"LEDGER_LOG_NOT_UTF8: {log}") from exc
    except OSError as exc:
        raise LedgerError(f"LEDGER_LOG_UNREADABLE: {log}: {exc.strerror}") from exc
    for lineno, line in enumerate(log_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"LEDGER_LINE_UNPARSEABLE: line {lineno}") from exc
        if not isinstance(entry, dict):
            raise LedgerError(f"LEDGER_ENTRY_NOT_AN_OBJECT: line {lineno}")
        if entry.get("schema") != LEDGER_SCHEMA:
            raise LedgerError(f"LEDGER_ENTRY_SCHEMA_UNSUPPORTED: line {lineno}")
        entries.append(entry)

    previous = GENESIS_HASH
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    hashes: list[str] = []
    for position, entry in enumerate(entries, start=1):
        seq = entry.get("seq")
        # A typed check, not a coercion: int("1") == 1 would accept a string seq.
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != position:
            raise LedgerError(f"LEDGER_SEQ_BROKEN at position {position}: {seq!r}")
        if entry.get("previous_entry_hash") != previous:
            raise LedgerError(f"LEDGER_CHAIN_BROKEN at seq {position}")
        rid = str(entry.get("record_id") or "")
        if not _is_sha256(rid):
            raise LedgerError(f"LEDGER_RECORD_ID_MALFORMED at seq {position}")
        if rid in seen:
            raise LedgerError(f"LEDGER_REPLAYED_RECORD: {rid}")
        seen.add(rid)
        expected = _entry_hash(previous, rid, str(entry.get("created_at") or ""))
        if entry.get("entry_hash") != expected:
            raise LedgerError(f"LEDGER_ENTRY_HASH_INVALID at seq {position}")

        path = ledger_dir / "records" / f"{rid}.json"
        if not path.is_file():
            raise LedgerError(f"LEDGER_RECORD_MISSING: {rid}")
        stored = _read_ledger_json(path, "LEDGER_RECORD")
        if not isinstance(stored, dict):
            raise LedgerError(f"LEDGER_RECORD_NOT_AN_OBJECT: {rid} is {type(stored).__name__}")
        if _record_id(stored) != rid:
            raise LedgerError(f"LEDGER_RECORD_TAMPERED: {rid}")
        records.append(stored)
        hashes.append(expected)
        previous = expected

    head_hash, count = ledger_head(ledger_dir)
    if count != len(entries) or head_hash != previous:
        # CRASH WINDOW 2: the log entry landed, the head update did not. The log
        # is the commit point, so a head that names an EARLIER position whose
        # hash matches exactly is a recoverable interrupted append, and rolling
        # the head forward is deterministic. Rolling forward cannot hide a fork,
        # because the anchor compares the entry at the anchored position.
        recoverable = 0 < count < len(entries) and hashes[count - 1] == head_hash
        if recoverable and repair:
            _atomic_write(
                ledger_dir / "head.json",
                json.dumps(
                    {"schema": LEDGER_SCHEMA, "head_hash": previous, "count": len(entries)},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
        elif recoverable:
            raise LedgerRecoverableError(
                f"LEDGER_HEAD_BEHIND_LOG: manifest names entry {count} of "
                f"{len(entries)} and its hash matches, so an append was "
                f"interrupted after the log write. Re-run with repair=True "
                f"(CLI: --repair-ledger) to roll the head forward."
            )
        else:
            raise LedgerError(
                f"LEDGER_HEAD_MISMATCH: manifest {head_hash[:12]}…/{count} vs "
                f"chain {previous[:12]}…/{len(entries)}"
            )

    on_disk = (
        {p.stem for p in (ledger_dir / "records").glob("*.json")}
        if (ledger_dir / "records").is_dir()
        else set()
    )
    orphans = sorted(on_disk - seen)
    if orphans:
        # CRASH WINDOW 1: the record file landed, the log entry did not. Such a
        # record was never committed. It is deterministically discardable ONLY if
        # it is fully verifiable - its content must hash to its own filename -
        # because anything else is corruption rather than an interrupted append.
        verifiable = []
        for stem in orphans:
            path = ledger_dir / "records" / f"{stem}.json"
            try:
                body = _read_ledger_json(path, "LEDGER_RECORD")
            except LedgerError:
                break
            if not isinstance(body, dict) or _record_id(body) != stem:
                break
            verifiable.append(stem)
        if len(verifiable) == len(orphans) and repair:
            for stem in verifiable:
                path = ledger_dir / "records" / f"{stem}.json"
                path.chmod(0o644)
                quarantine = ledger_dir / "uncommitted"
                quarantine.mkdir(exist_ok=True)
                path.rename(quarantine / path.name)
        elif len(verifiable) == len(orphans):
            raise LedgerRecoverableError(
                f"LEDGER_UNCOMMITTED_RECORDS: {orphans} are fully verifiable but "
                f"absent from the log, so an append was interrupted before the "
                f"log write. They were never committed. Re-run with repair=True "
                f"(CLI: --repair-ledger) to move them to uncommitted/."
            )
        else:
            raise LedgerError(f"LEDGER_RECORDS_NOT_INCLUDED: {orphans}")
    return records, hashes


def load_ledger(ledger_dir: Path, *, repair: bool = False) -> list[dict[str, Any]]:
    """Verify the chain and every record's inclusion, then return the records.

    Raises on: broken chain, head/manifest mismatch, missing record file, a record
    whose content does not hash to its own record_id, a record file absent from
    the ledger, and any replayed record_id.
    """
    with _read_lock(ledger_dir):
        return _walk_ledger(ledger_dir, repair=repair)[0]


def ledger_id(ledger_dir: Path) -> str | None:
    """Stable identity of a ledger: the hash of its FIRST entry.

    Immutable across appends, so an anchor can be bound to the ledger it was
    published for and cannot be replayed against a different one.
    """
    _records, hashes = _walk_ledger(ledger_dir)
    return hashes[0] if hashes else None


def sign_ledger_anchor(
    head_hash: str,
    entry_count: int,
    secret: str,
    *,
    ledger: str | None = None,
    rating_contract_digest: str | None = None,
) -> dict[str, Any]:
    """Coordinator-signed external head anchor.

    Binds the LEDGER IDENTITY and the CONTRACT DIGEST as well as the head, so an
    anchor published for one ledger or one campaign cannot be presented for
    another.
    """
    body = {
        "schema": LEDGER_ANCHOR_SCHEMA,
        "ledger_id": ledger,
        "rating_contract_digest": rating_contract_digest,
        "head_hash": head_hash,
        "entry_count": entry_count,
    }
    body["signature"] = hmac.new(
        secret.encode("utf-8"),
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return body


def verify_ledger_anchor(anchor: Mapping[str, Any], secret: str) -> dict[str, Any]:
    """Validate an anchor's schema, types and signature. Returns its fields."""
    if not isinstance(anchor, Mapping):
        raise LedgerError("ANCHOR_NOT_AN_OBJECT")
    if anchor.get("schema") != LEDGER_ANCHOR_SCHEMA:
        raise LedgerError(f"ANCHOR_SCHEMA_UNSUPPORTED: {anchor.get('schema')!r}")
    head = anchor.get("head_hash")
    count = anchor.get("entry_count")
    if not _is_sha256(head):
        raise LedgerError(f"ANCHOR_HEAD_MALFORMED: {head!r}")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise LedgerError(f"ANCHOR_COUNT_NOT_A_NON_NEGATIVE_INT: {count!r}")
    for field in ("ledger_id", "rating_contract_digest"):
        value = anchor.get(field)
        if value is not None and not isinstance(value, str):
            raise LedgerError(f"ANCHOR_{field.upper()}_NOT_A_STRING: {type(value).__name__}")
    signature = anchor.get("signature")
    if not isinstance(signature, str):
        raise LedgerError("ANCHOR_SIGNATURE_MISSING")
    expected = sign_ledger_anchor(
        str(head),
        count,
        secret,
        ledger=anchor.get("ledger_id"),
        rating_contract_digest=anchor.get("rating_contract_digest"),
    )["signature"]
    if not hmac.compare_digest(signature, expected):
        raise LedgerError("ANCHOR_SIGNATURE_INVALID: anchor is not from the trusted coordinator")
    return {
        "head_hash": str(head),
        "entry_count": count,
        "ledger_id": anchor.get("ledger_id"),
        "rating_contract_digest": anchor.get("rating_contract_digest"),
    }


def verify_against_anchor(
    ledger_dir: Path,
    anchor: Mapping[str, Any],
    *,
    anchor_secret: str,
    rating_contract_digest: str | None = None,
) -> None:
    """Require the anchor to describe the ledger EXACTLY. No unanchored suffix.

    A prefix match is not enough, and that was a real hole. While the check only
    required `local_count >= anchor_count` with a matching hash at the anchored
    position, a STALE anchor verified against a longer ledger - leaving every
    later entry unanchored - and it also verified against a ledger truncated back
    to that same old head, silently discarding every rating appended since.

    So the anchor must name the CURRENT head and the CURRENT count, and be bound
    to this ledger's identity and this campaign's contract. The coordinator
    publishes a fresh anchor after each accepted batch; until it does, the newer
    entries are not admissible, which is the correct default.
    """
    fields = verify_ledger_anchor(anchor, anchor_secret)
    _records, hashes = _walk_ledger(ledger_dir)
    local_count = len(hashes)
    local_head = hashes[-1] if hashes else GENESIS_HASH

    # IDENTITY FIRST. "Is this even the right ledger, for the right campaign?"
    # precedes "does it have the right length and head". Checked last, this was
    # unreachable: if count and head both match then the ledgers are
    # hash-identical, so their first entries match too and the mismatch could
    # never fire. Ordered first, it is both reachable and the clearer error.
    anchored_ledger = fields["ledger_id"]
    if anchored_ledger is not None and anchored_ledger != (hashes[0] if hashes else None):
        raise LedgerError("ANCHOR_LEDGER_MISMATCH: anchor was published for a different ledger")
    anchored_contract = fields["rating_contract_digest"]
    if (
        anchored_contract is not None
        and rating_contract_digest is not None
        and anchored_contract != rating_contract_digest
    ):
        raise LedgerError("ANCHOR_CONTRACT_MISMATCH: anchor was published for a different campaign")

    if fields["entry_count"] != local_count:
        if fields["entry_count"] < local_count:
            raise LedgerError(
                f"LEDGER_UNANCHORED_SUFFIX: anchor covers {fields['entry_count']} "
                f"of {local_count} entries; entries "
                f"{fields['entry_count'] + 1}-{local_count} are unanchored. The "
                f"coordinator must publish a new anchor after each accepted batch."
            )
        raise LedgerError(
            f"LEDGER_ROLLBACK_DETECTED: local length {local_count} < anchored "
            f"{fields['entry_count']}"
        )
    if fields["head_hash"] != local_head:
        raise LedgerError(
            f"LEDGER_FORK_DETECTED: local head {local_head[:12]}… != anchored "
            f"{fields['head_hash'][:12]}… at count {local_count}"
        )


def _authorized_edges(
    valid: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    """Accept only supersede edges that satisfy every graph invariant.

    APPEND-TIME CHECKS DO NOT PROTECT THIS PATH. A ledger can arrive handcrafted
    or migrated, having never passed through `append_rating_record`, and an
    anchored one at that. Resolution therefore re-derives authorization from the
    records themselves instead of trusting that some earlier writer checked.

    Returns (target -> corrector, rejected corrector ids).
    """
    by_id = {str(r.get("record_id")): r for r in valid}
    edges: dict[str, str] = {}
    rejected: list[str] = []
    claims: dict[str, list[str]] = {}
    for record in valid:
        raw = record.get("supersedes")
        if not raw:
            continue
        claims.setdefault(str(raw), []).append(str(record.get("record_id")))

    for target_id, correctors in claims.items():
        target = by_id.get(target_id)
        # ONE live correction per target. Two records claiming the same victim is
        # a conflict, not a chain, so neither edge is honoured.
        if target is None or len(correctors) != 1:
            rejected.extend(correctors)
            continue
        corrector_id = correctors[0]
        corrector = by_id[corrector_id]
        same = (
            str(corrector.get("item_id")) == str(target.get("item_id"))
            and str(corrector.get("rater_key_id")) == str(target.get("rater_key_id"))
            and str(corrector.get("rating_contract_digest"))
            == str(target.get("rating_contract_digest"))
        )
        if not same:
            rejected.append(corrector_id)
            continue
        edges[target_id] = corrector_id

    # Acyclic: walk the superseded-by relation and drop every edge on a cycle.
    #
    # The walk must follow target -> corrector -> (corrector's own corrector).
    # Following the corrector's `supersedes` field instead returns immediately to
    # the target, so EVERY legitimate one-step correction looks like a cycle -
    # which is exactly what the first version of this did, and what the
    # legitimate-correction case caught.
    for start in list(edges):
        seen: set[str] = set()
        cursor = start
        while cursor in edges:
            if cursor in seen:
                for node in seen:
                    corrector = edges.pop(node, None)
                    if corrector is not None:
                        rejected.append(corrector)
                break
            seen.add(cursor)
            cursor = edges[cursor]
    return edges, sorted(set(rejected))


def effective_ratings(
    records: Sequence[Mapping[str, Any]],
    *,
    rating_contract_digest: str,
    context_digests: Mapping[str, str],
    keyring: Mapping[str, str],
    qualified_rater_ids: Collection[str],
) -> list[dict[str, Any]]:
    """Latest non-superseded VALID record per (item_id, rater_key_id).

    TWO independent gates, in this order:

    1. ORDER IS LOAD-BEARING: every record is validated and qualified FIRST, and
       only then is supersession resolved among the survivors. Resolving first let
       an invalid or unqualified correction delete a valid rating - the correction
       was rejected downstream, but the victim was already gone, so a spoof
       suppressed honest work before readiness ever saw it.

    2. EVERY SUPERSEDE EDGE IS RE-AUTHORIZED HERE. Append-time authorization does
       not cover a ledger that was handcrafted or migrated rather than appended,
       so same item/rater/contract, one-live-correction-per-target and acyclicity
       are re-checked from the records. A record whose claimed edge is
       unauthorized is DISCARDED, and its victim survives: fail closed in both
       directions rather than trusting an upstream writer.

    The verifier context is required rather than optional for the same reason the
    supersedes parameter was removed: an optional guard is a guard someone omits.
    """
    # STRUCTURAL type check across every field, before any value check.
    #
    # The line drawn here matters. A WRONG VALUE - a well-formed string that is
    # not in the codebook - is merely an invalid rating, and dropping it while
    # keeping honest ratings is correct. A WRONG TYPE - a list or dict where a
    # string belongs - is something authenticated append can never produce, so
    # its presence means filesystem-level control of the ledger. That is a
    # compromised ledger, and the whole intake refuses.
    #
    # This was previously enforced for `rater_key_id` alone, on exactly that
    # reasoning, while nineteen other fields carrying a container were silently
    # dropped and the remaining ratings shipped. One field failing closed and the
    # rest degrading quietly is not a policy; it is an oversight.
    for record in records:
        for field in _MUST_BE_STRING:
            value = record.get(field)
            if not isinstance(value, str):
                raise LedgerError(
                    f"LEDGER_RECORD_FIELD_NOT_A_STRING: {field} is "
                    f"{type(value).__name__}; a ledger containing a structurally "
                    f"corrupt record is not partially usable"
                )
        supersedes = record.get("supersedes")
        if supersedes is not None and not isinstance(supersedes, str):
            raise LedgerError(
                f"LEDGER_RECORD_FIELD_NOT_A_STRING: supersedes is {type(supersedes).__name__}"
            )

    valid = [
        dict(record)
        for record in records
        if not validate_rating(
            dict(record),
            rating_contract_digest=rating_contract_digest,
            context_digests=context_digests,
            keyring=keyring,
            qualified_rater_ids=qualified_rater_ids,
        )
    ]
    edges, rejected = _authorized_edges(valid)
    superseded = set(edges)
    dropped = set(rejected)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for record in valid:
        rid = str(record.get("record_id"))
        if rid in superseded or rid in dropped:
            continue
        key = (str(record.get("item_id")), str(record.get("rater_key_id")))
        out[key] = record
    return [out[k] for k in sorted(out)]


def taxonomy_contract_block() -> dict[str, Any]:
    """The exact taxonomy/codebook a rater is shown, and which the contract covers."""
    return {
        "primary_label": PRIMARY_LABEL,
        "human_judged_fields": list(HUMAN_JUDGED_FIELDS),
        "allowed_values": {k: list(v) for k, v in ALLOWED_VALUES.items()},
        "primary_definitions": PRIMARY_DEFINITIONS,
        "facet_definitions": FACET_DEFINITIONS,
        "attention_check_field": ATTENTION_CHECK_FIELD,
        "missing_data_semantics": {
            CANNOT_JUDGE: (
                "Context IS present; the step is genuinely ambiguous. Measures taxonomy ambiguity."
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
    anchor_root: Path | None = None,
    anchor_secret: str | None = None,
    repair_ledger: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    taxonomy_block = taxonomy_contract_block()
    universe, truths, census = enumerate_universe(runs_root)
    selected = select_items(universe, core_n=core_n, boost_per_stratum=boost_per_stratum)
    keep = {i.item_id for i in selected}
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
    # ONE canonical contract digest, computed from the SAME canonical bundle the
    # rater receives. Two functions previously disagreed - the server expected an
    # item-only digest while the client signed a whole-bundle digest - so every
    # genuine rating was rejected with RATING_CONTRACT_DIGEST_MISMATCH. The
    # distributed path never worked, and testing each half in isolation could not
    # reveal it.
    contract_bundle = build_rater_bundle(
        {
            "taxonomy_version": TAXONOMY_VERSION,
            "rating_schema_version": RATING_SCHEMA_VERSION,
            "taxonomy": taxonomy_block,
            "items": [asdict(i) for i in deliverable],
            "deliverable_item_ids": sorted(i.item_id for i in deliverable),
        }
    )
    rating_contract_digest = compute_bundle_contract_digest(contract_bundle)
    # Intake runs AFTER the contract digest and the registry, because it needs both
    # to validate a record before letting it alter the effective view.
    records, intake_mode, intake_problems = load_intake(
        ratings_dir,
        rating_contract_digest=rating_contract_digest,
        context_digests={i.item_id: i.item_context_digest for i in deliverable},
        keyring=keyring,
        qualified_rater_ids=qualified,
        anchor_root=anchor_root,
        anchor_secret=anchor_secret,
        repair=repair_ledger,
    )
    readiness = evaluate_readiness(
        deliverable,
        records,
        qualified_rater_ids=qualified,
        rating_contract_digest=rating_contract_digest,
        keyring=keyring or None,
        intake_mode=intake_mode,
        intake_problems=intake_problems,
    )
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
        "taxonomy": taxonomy_block,
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

# The bundle is defined by an EXACT ALLOWLIST of owned files, not a pathname
# denylist. A denylist can never be complete: renaming
# machine_truth_WITHHELD.json to deep/answers.json defeated it entirely while the
# full withheld truth sat in the destination. An allowlist makes the filename and
# the content irrelevant - anything not owned by the bundle is rejected.
BUNDLE_ALLOWLIST = frozenset({"rater_bundle.json"})


class BundleContaminationError(RuntimeError):
    """Raised when a rater bundle directory holds anything the bundle does not own."""


def _assert_bundle_exact(bundle_dir: Path) -> None:
    """Recursive EXACT allowlist. Every extra path and every symlink is rejected."""
    offenders: list[str] = []
    for path in sorted(bundle_dir.rglob("*")):
        rel = str(path.relative_to(bundle_dir))
        if path.is_symlink():
            offenders.append(f"SYMLINK:{rel}")
            continue
        if path.is_dir():
            offenders.append(f"UNOWNED_DIRECTORY:{rel}")
            continue
        if rel not in BUNDLE_ALLOWLIST:
            offenders.append(f"UNOWNED_FILE:{rel}")
    if offenders:
        raise BundleContaminationError(
            f"rater bundle {bundle_dir} contains paths the bundle does not own "
            f"(allowlist={sorted(BUNDLE_ALLOWLIST)}): {offenders}"
        )


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
        "schema_version": BUNDLE_SCHEMA,
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
                # Identity inputs to item_context_digest MUST be signed bundle
                # fields, otherwise a rater cannot recompute the digest and is
                # forced to copy a supplied value blindly.
                "cluster_id": item["cluster_id"],
                "step_index": item["step_index"],
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


BUNDLE_CONTRACT_EXCLUDED = ("rating_contract_digest", "coordinator_signature")
BUNDLE_SCHEMA = "goldset-rater-bundle/v2"


def compute_bundle_contract_digest(bundle: Mapping[str, Any]) -> str:
    """Contract digest over the FULL canonical bundle, minus digest and signature.

    Previously the digest covered only an ordered item list, so a rater could not
    confirm that the taxonomy, codebook or instructions they were shown were the
    ones under contract. Digesting the whole bundle makes the contract
    independently verifiable from the artifact alone.
    """
    payload = {k: v for k, v in bundle.items() if k not in BUNDLE_CONTRACT_EXCLUDED}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def sign_bundle(bundle: Mapping[str, Any], distribution_secret: str) -> str:
    """Coordinator signature over the canonical contract digest."""
    return hmac.new(
        distribution_secret.encode("utf-8"),
        compute_bundle_contract_digest(bundle).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class BundleVerificationError(RuntimeError):
    """Raised when a rater bundle fails independent verification."""


def verify_bundle(bundle: Mapping[str, Any], distribution_secret: str | None) -> str:
    """Recompute everything; trust nothing supplied. Used by CLIENT and SERVER.

    Returns the recomputed contract digest. Raises on any mismatch:
      - coordinator signature absent or invalid
      - supplied rating_contract_digest disagrees with the recomputation
      - any item_context_digest disagrees with recomputation from its own
        signed inputs (rater_context + cluster_id + step_index)
    """
    if not isinstance(bundle, Mapping):
        raise BundleVerificationError(f"BUNDLE_NOT_AN_OBJECT: got {type(bundle).__name__}")
    if bundle.get("schema_version") != BUNDLE_SCHEMA:
        raise BundleVerificationError(
            f"BUNDLE_SCHEMA_UNSUPPORTED: {bundle.get('schema_version')!r} != {BUNDLE_SCHEMA!r}"
        )
    for required in (
        "taxonomy",
        "instructions_to_rater",
        "codebook_version",
        "rating_schema_version",
        "items",
        "rating_contract_digest",
    ):
        if required not in bundle:
            raise BundleVerificationError(f"BUNDLE_MISSING_FIELD: {required}")
    items = bundle.get("items")
    if not isinstance(items, list):
        raise BundleVerificationError(f"BUNDLE_ITEMS_NOT_A_LIST: got {type(items).__name__}")
    if not items:
        raise BundleVerificationError("BUNDLE_EMPTY: no items to rate")
    for position, entry in enumerate(items):
        if not isinstance(entry, Mapping):
            raise BundleVerificationError(
                f"BUNDLE_ITEM_NOT_AN_OBJECT: index {position} is {type(entry).__name__}"
            )
        for field in (
            "item_id",
            "item_context_digest",
            "cluster_id",
            "step_index",
            "rater_context",
        ):
            if field not in entry:
                raise BundleVerificationError(
                    f"BUNDLE_ITEM_MISSING_FIELD: index {position} lacks {field}"
                )
    if len({str(i["item_id"]) for i in items}) != len(items):
        raise BundleVerificationError("BUNDLE_DUPLICATE_ITEM_IDS")

    if not distribution_secret:
        raise BundleVerificationError("NO_DISTRIBUTION_KEY: cannot verify coordinator")
    expected_signature = sign_bundle(bundle, distribution_secret)
    supplied_signature = str(bundle.get("coordinator_signature") or "")
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise BundleVerificationError("COORDINATOR_SIGNATURE_INVALID")

    recomputed = compute_bundle_contract_digest(bundle)
    if bundle.get("rating_contract_digest") != recomputed:
        raise BundleVerificationError(
            f"CONTRACT_DIGEST_MISMATCH: supplied "
            f"{bundle.get('rating_contract_digest')!r} != {recomputed!r}"
        )

    for item in bundle.get("items") or []:
        expected_ctx = compute_item_context_digest(
            item["rater_context"],
            cluster_id=item["cluster_id"],
            step_index=item["step_index"],
        )
        if item.get("item_context_digest") != expected_ctx:
            raise BundleVerificationError(f"ITEM_CONTEXT_DIGEST_MISMATCH: {item.get('item_id')}")
    return recomputed


def prepare_rating(
    bundle: Mapping[str, Any],
    *,
    item_id: str,
    labels: Mapping[str, str],
    rater_key_id: str,
    rater_secret: str,
    distribution_secret: str,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Rater-client path. Verifies the bundle and RECOMPUTES before signing.

    A client that copies supplied digests learns nothing about whether the
    context it displayed is the context under contract. This helper refuses to
    sign until it has independently reproduced both digests.
    """
    contract = verify_bundle(bundle, distribution_secret)
    item = next((i for i in bundle["items"] if i["item_id"] == item_id), None)
    if item is None:
        raise BundleVerificationError(f"UNKNOWN_ITEM_IN_BUNDLE: {item_id}")
    context_digest = compute_item_context_digest(
        item["rater_context"],
        cluster_id=item["cluster_id"],
        step_index=item["step_index"],
    )
    record = {
        "schema_version": RATING_SCHEMA_VERSION,
        "rating_contract_digest": contract,
        "item_id": item_id,
        "item_context_digest": context_digest,
        "rater_key_id": rater_key_id,
        "supersedes": supersedes,
        **{field: labels[field] for field in HUMAN_JUDGED_FIELDS},
    }
    record["signature"] = sign_rating(record, rater_secret)
    return record


def export_rater_bundle(
    package: Mapping[str, Any],
    bundle_dir: Path,
    distribution_secret: str | None = None,
) -> Path:
    """Build into a FRESH temp dir, verify the exact allowlist, then publish.

    Generating in place and scanning afterwards cannot work: whatever was already
    in the destination is not the bundle's, and a pathname denylist stops
    recognising it the moment it is renamed. Measured bypass: the withheld truth
    copied to `deep/answers.json` exported cleanly. So the bundle is built
    somewhere empty, verified to contain EXACTLY its owned files, and only then
    published - and the destination must be empty or absent, never merged into.
    """
    bundle = build_rater_bundle(package)
    bundle["rating_contract_digest"] = compute_bundle_contract_digest(bundle)
    declared = package.get("readiness", {}).get("authentication", {}).get("rating_contract_digest")
    if declared and declared != bundle["rating_contract_digest"]:
        raise BundleVerificationError(
            f"CONTRACT_DIGEST_DIVERGENCE: package declares {declared[:12]}… but the "
            f"exported bundle computes {bundle['rating_contract_digest'][:12]}…; the "
            f"server would reject every rating produced from this bundle"
        )
    if distribution_secret:
        bundle["coordinator_signature"] = sign_bundle(bundle, distribution_secret)
    staging = Path(tempfile.mkdtemp(prefix="goldset-bundle-"))
    try:
        _assert_bundle_exact(staging)  # empty staging passes trivially
        staged = staging / "rater_bundle.json"
        staged.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _assert_bundle_exact(staging)

        if bundle_dir.is_symlink():
            raise BundleContaminationError(f"destination {bundle_dir} is a symlink")
        if bundle_dir.exists():
            existing = sorted(str(q.relative_to(bundle_dir)) for q in bundle_dir.rglob("*"))
            if existing:
                raise BundleContaminationError(
                    f"destination {bundle_dir} is not empty; refusing to merge a "
                    f"rater bundle into pre-existing paths: {existing}"
                )
        bundle_dir.mkdir(parents=True, exist_ok=True)
        published = bundle_dir / "rater_bundle.json"
        os.replace(staged, published)
        _assert_bundle_exact(bundle_dir)  # verify AFTER publish
        return published
    finally:
        for leftover in sorted(staging.rglob("*"), reverse=True):
            if leftover.is_file() or leftover.is_symlink():
                leftover.unlink(missing_ok=True)
            else:
                leftover.rmdir()
        staging.rmdir()


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
def _read_lock(directory: Path) -> Iterator[None]:
    """SHARED reader lock. Concurrent readers coexist; an appender is excluded.

    Readers previously took no lock at all, so a load could interleave with an
    append and observe a record file that the log did not yet mention - a torn
    read reported as corruption.
    """
    if not directory.is_dir():
        yield
        return
    lock_path = directory / LOCK_NAME
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        "--distribution-secret-env",
        default="GOLDSET_DISTRIBUTION_SECRET",
        help=(
            "env var holding the coordinator distribution secret; REQUIRED to "
            "export a rater bundle, since an unsigned bundle cannot be verified"
        ),
    )
    parser.add_argument(
        "--anchor-secret-env",
        default="GOLDSET_ANCHOR_SECRET",
        help="env var holding the anchor trust secret",
    )
    parser.add_argument(
        "--repair-ledger",
        action="store_true",
        help=(
            "repair a deterministically recoverable interrupted append: roll a "
            "head that is behind the log forward, and quarantine fully verifiable "
            "uncommitted records. Never implicit."
        ),
    )
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
        anchor_secret=os.environ.get(args.anchor_secret_env),
        repair_ledger=args.repair_ledger,
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
        distribution_secret = os.environ.get(args.distribution_secret_env)
        if not distribution_secret:
            print(
                f"REFUSED UNSIGNED_EXPORT: set {args.distribution_secret_env}; an "
                f"unsigned bundle carries no coordinator signature and a rater "
                f"cannot verify it"
            )
            return 2
        try:
            bundle_path = export_rater_bundle(
                package, args.export_rater_bundle, distribution_secret
            )
        except (BundleContaminationError, BundleVerificationError) as exc:
            print(f"REFUSED {exc}")
            return 2
        print(f"wrote {bundle_path} (RATER-SAFE, coordinator-signed)")

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
