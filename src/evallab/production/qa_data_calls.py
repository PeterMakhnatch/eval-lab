"""``qa-data`` calls over runs and history: run search, step windows, past issues and cards.

Each function returns the ``data`` payload of one call and takes whole items
from ``ctx.budget``; admission (registry cross-key DEV) always precedes content.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from evallab.hackbook.playbook_cards import list_playbook_cards, match_playbook_cards
from evallab.production.cloud_trace_db import _facet_filters, _sealed_pattern
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError, match_admitted_task

# Facet fields a QA agent needs to triage a run. ``source`` and ``split`` stay out:
# run records are routinely handed to blinded readers.
_RUN_FIELDS = (
    "run_id", "task_id", "outcome", "tests", "suspicion", "eval_awareness", "shortcut",
    "grader_or_harness_touched", "answer_material_accessed", "success_claim_without_evidence",
    "goal", "approach", "evidence_steps",
)
_STOP_WORDS = {
    "about", "above", "after", "again", "against", "because", "before", "being", "below", "between",
    "could", "doing", "during", "further", "having", "itself", "other", "should", "their", "there",
    "these", "those", "through", "under", "until", "which", "while", "would", "files", "using",
    "please", "where", "within",
}


def _instruction_query(text: str, words: int = 8) -> str:
    seen: list[str] = []
    for word in re.findall(r"[a-z][a-z_]{4,}", text.casefold()):
        if word not in _STOP_WORDS and word not in seen:
            seen.append(word)
        if len(seen) == words:
            break
    return " ".join(seen)


_SIMILARITY_FIELDS = (
    "outcome", "tests", "eval_awareness", "shortcut", "grader_or_harness_touched",
    "answer_material_accessed", "success_claim_without_evidence", "suspicion",
)
_TEXT_FIELDS = ("goal", "approach", "shortcut")
# Step fields a QA agent reads. Raw ``flags_json``, ``flagged`` (derived from passage labels)
# and CAS pointers are evaluator-side and stay out.
_STEP_FIELDS = ("step_index", "role", "tool_name", "command_or_action", "content_text", "observation_preview")


def _cluster_index(ctx: QaContext, visible: set[str]) -> list[dict[str, Any]]:
    """Stored clusters projected onto ``visible`` runs; hidden members never shape what is returned."""
    with ctx.database._transaction():
        rows = ctx.database._rows("SELECT cluster_id, level, run_ids FROM run_clusters")
    projected = []
    for row in rows:
        members = [run_id for run_id in row["run_ids"] if run_id in visible]
        if members:
            projected.append({
                "id": row["cluster_id"], "level": row["level"], "members": members,
            })
    return projected


def _deepest(index: list[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    """The run's deepest, then smallest-by-visible-members cluster; hidden-member counts never matter."""
    own = [cluster for cluster in index if run_id in cluster["members"]]
    return min(own, key=lambda c: (-c["level"], len(c["members"]), c["id"])) if own else None


def _terms(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)))


def runs(
    ctx: QaContext, *, facets: dict[str, Any], cluster_id: str | None, like_run_id: str | None,
    query: str, task_id: str | None, limit: int,
) -> dict[str, Any]:
    """Admitted runs matching facet filters, a cluster, query words and/or similarity to a run.

    Every run is listed (not one per task). Order: facet similarity to ``like_run_id``
    when given, then number of query words found in the run's goal/approach/shortcut,
    then suspicion, then run id. A query naming a sealed identity simply matches nothing.
    """
    if limit < 1:
        raise QaDataError("--limit must be at least 1")
    terms = _terms(query)
    if not (facets or cluster_id is not None or like_run_id is not None or terms or task_id is not None):
        raise QaDataError("give at least one of --facet, --cluster, --like, --query, --task")
    if task_id is not None:
        task_id = ctx.require_task(task_id)["task_id"]
    try:
        filters = _facet_filters({} if facets is None else facets)
        # One screened read of every admitted run: facet filters apply to it below, and the
        # same rows decide the --like reference and which cluster members are visible.
        with ctx.database._transaction():
            rows, sealed_ids = ctx.database._admitted_facets(ctx.retriever)
    except ValueError as exc:
        raise QaDataError(str(exc)) from exc
    everything = {row["run_id"]: row for row in rows}
    pool = [row for row in rows if all(row[field] in values for field, values in filters)]
    index = _cluster_index(ctx, set(everything))
    reference = None
    if like_run_id is not None:
        reference = everything.get(like_run_id)
        if reference is None:
            raise QaDataError(UNAVAILABLE)
    members: set[str] | None = None
    if cluster_id is not None:
        match = next((cluster for cluster in index if cluster["id"] == cluster_id), None)
        if match is None:
            raise QaDataError(UNAVAILABLE)
        members = set(match["members"])
    reference_cluster = _deepest(index, like_run_id) if like_run_id is not None else None
    sealed = _sealed_pattern(frozenset(sealed_ids))
    scored = []
    for row in pool:
        if task_id is not None and row["task_id"] != task_id:
            continue
        if members is not None and row["run_id"] not in members:
            continue
        if reference is not None and row["run_id"] == reference["run_id"]:
            continue
        hits = 0
        if terms:
            words = set(_terms(" ".join(str(row[field]) for field in _TEXT_FIELDS)))
            hits = sum(term in words for term in terms)
            if not hits:
                continue
        similarity = 0
        if reference is not None:
            similarity = sum(row[field] == reference[field] for field in _SIMILARITY_FIELDS)
            if reference_cluster is not None and row["run_id"] in reference_cluster["members"]:
                similarity += 1
        scored.append((-similarity, -hits, -row["suspicion"], row["run_id"], row, similarity))
    scored.sort(key=lambda entry: entry[:4])
    ranked = []
    for *_, row, similarity in scored[: max(limit, 0)]:
        item: dict[str, Any] = {key: row[key] for key in _RUN_FIELDS}
        cluster = _deepest(index, row["run_id"])
        if cluster is not None and not ctx.retriever._sealed_text(cluster["id"], sealed):
            # Names are model-written from a cluster's stored samples, which may include runs this
            # caller cannot see, so only the identifier and level are given.
            item["cluster"] = {"id": cluster["id"], "level": cluster["level"]}
        if reference is not None:
            item["facet_similarity"] = similarity
        ranked.append(item)
    taken = [item for item in ranked if ctx.budget.take(item)]
    return {
        "runs": taken, "matched": len(scored), "omitted": len(ranked) - len(taken),
        "truncated": len(taken) < len(scored),
    }


def window(
    ctx: QaContext, run_id: str, *, first: int | None, last: int | None, around: int | None, radius: int,
) -> dict[str, Any]:
    """Recorded non-user steps of one admitted run, in a range or a radius around one step."""
    ctx.task_of_run(run_id)
    if around is not None:
        if first is not None or last is not None:
            raise QaDataError("--around cannot combine with --first/--last")
        if radius < 0:
            raise QaDataError("--radius must be nonnegative")
        first, last = max(0, around - radius), around + radius
    if first is None:
        first = 0
    if last is None:
        last = first + 19
    if first < 0 or last < first:
        raise QaDataError("step range must satisfy 0 <= first <= last")
    try:
        result = ctx.retriever.expand_pointer(
            f"trace:{run_id}:{first}-{last}", include_provisional=True, max_steps=max(1, last - first + 1),
        )
    except ValueError as exc:
        raise QaDataError(UNAVAILABLE) from exc
    steps = [{field: step[field] for field in _STEP_FIELDS} for step in result.get("steps", [])]
    taken = [step for step in steps if ctx.budget.take(step)]
    return {
        "run_id": run_id, "first": first, "last": last, "steps": taken, "omitted": len(steps) - len(taken),
        "truncated": len(taken) < len(steps),
        "next_first": taken[-1]["step_index"] + 1 if taken and len(taken) < len(steps) else None,
    }


def history(
    ctx: QaContext, task_id: str, *, flags: Sequence[str], limit: int, with_outcomes: bool = False,
) -> dict[str, Any]:
    """The task's own issues, similar past issues elsewhere, and matching playbook cards.

    Trust tiers and original/repaired reward differentials are outcome labels: they are returned
    only with ``with_outcomes`` (the CLI's ``--with-outcomes``), never by default.
    """
    if limit < 1:
        raise QaDataError("--limit must be at least 1")
    if not isinstance(task_id, str):
        raise QaDataError(UNAVAILABLE)
    with ctx.database._transaction():  # admission, task row and issue rows share one snapshot
        admitted, _sealed = ctx.retriever._admitted_ids()
        canonical = match_admitted_task(admitted, task_id)
        tasks = ctx.database._rows(
            "SELECT instruction_text FROM corpus_tasks WHERE task_id = ?", (canonical,),
        ) if canonical is not None else []
        issue_rows = ctx.database._rows(
            "SELECT issue_id, defect_class, direction, status, trust_tier, citation_file, citation_line, "
            "original_reward, repaired_reward FROM task_issues WHERE task_id = ? ORDER BY issue_id",
            (canonical,),
        ) if tasks else []
    if not tasks or canonical is None:
        raise QaDataError(UNAVAILABLE)
    task_id = canonical
    live = [row for row in issue_rows if row["status"] != "refuted"]
    own = []
    for row in live:
        fields = ("issue_id", "defect_class", "direction", "status") + (("trust_tier",) if with_outcomes else ())
        item = {key: row[key] for key in fields if row[key] is not None}
        if row["citation_file"]:
            item["citation"] = f"{row['citation_file']}:{row['citation_line']}"
        if with_outcomes and row["original_reward"] is not None and row["repaired_reward"] is not None:
            item["differential"] = [row["original_reward"], row["repaired_reward"]]
        if row["status"] == "confirmed":
            item["expand"] = f"issue:{row['issue_id']}"
        if ctx.budget.take(item):
            own.append(item)
    similar: list[dict[str, Any]] = []
    query = _instruction_query(tasks[0]["instruction_text"] or "")
    examples = ctx.retriever.retrieve_similar_examples(
        query, "task_quality", limit, exclude_task_ids=[task_id],
    ) if query else []
    for example in examples:
        item = {
            "task_id": example.task_id, "issue_id": example.issue_id, "defect_class": example.defect_class,
            "direction": example.direction, "expand": f"issue:{example.issue_id}",
        }
        if with_outcomes:
            item["trust_tier"] = example.trust_tier
        if example.citation:
            item["citation"] = example.citation
        if with_outcomes and example.differential:
            item["differential"] = example.differential
        if ctx.budget.take(item):
            similar.append(item)
    triggers = sorted({
        flag[:80] for flag in flags[:20]
    } | {f"issue:{row['defect_class']}" for row in live if row["defect_class"]})
    cards = [card for card in match_playbook_cards(triggers, "task", 3) if ctx.budget.take(card)] if triggers else []
    index = [
        {"card_id": card["card_id"], "trigger_flags": card["trigger_flags"]}
        for card in list_playbook_cards() if card["card_id"].startswith("QA")
    ]
    index_taken = index if ctx.budget.take(index) else []
    return {
        "task_id": task_id, "issues": own, "issues_total": len(live),
        "refuted_issues": len(issue_rows) - len(live),
        "similar_issues": similar, "playbook_cards": cards,
        "card_index": index_taken,
        "truncated": len(own) < len(live) or ctx.budget.omitted > 0,
    }
