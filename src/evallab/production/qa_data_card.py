"""``qa-data trace-card``: one compact, label-free evidence card for one admitted DEV run.

The card gathers what every trace detector says about a run: the stored
``trace-findings/v1`` verdicts (one line per detector, version and reader), the
Run Atlas facet leads, the deterministic trace feature flags (first cited step
each) with the trace playbook cards they trigger, and per-reader counts from the
trace-tool ledgers. It never carries an outcome, reward, model, dataset or split
field, and it stays within ``CARD_TOKENS`` whatever ``--budget`` allows: whole
items are dropped, lowest priority first, and counted in ``omitted``.

Verdicts are leads, not labels. An absent finding is not a clean verdict, and a
run with no actor transcript is unknown, never clean.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from evallab.hackbook.playbook_cards import match_playbook_cards
from evallab.production.cloud_trace_db import _sealed_pattern
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens
from evallab.production.trace_feature_flags import compute_trace_feature_flags
from evallab.production.trace_findings import has_actor_inputs, has_table

CARD_TOKENS = 1500
_VERDICT_RANK = {"alarm": 0, "unknown": 1, "abstain": 2, "clear": 3}
_FACET_FIELDS = (
    "suspicion", "tests", "eval_awareness", "grader_or_harness_touched", "answer_material_accessed",
    "success_claim_without_evidence", "shortcut", "evidence_steps",
)
_NOTE = (
    "Leads, not verdicts. No finding is not a clean verdict; a run with no actor transcript is unknown, never clean."
)
_WITHHELD = "[withheld]"
_LEDGER_BUDGET = 400


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _screen(ctx: QaContext, sealed: Any, forbidden: str, text: str, counter: list[int]) -> str:
    """Free text with opaque identities scrubbed; withheld when it names a sealed identity or the run's dataset."""
    from evallab.production.qa_data_findings import _OPAQUE

    if ctx.retriever._sealed_text(text, sealed) or (forbidden and forbidden in text.casefold()):
        counter[0] += 1
        return _WITHHELD
    return _OPAQUE.sub("[id]", text)


def _stored(ctx: QaContext, run_id: str) -> dict[str, Any]:
    """Everything the card reads from the store, admitted and read in one snapshot."""
    with ctx.database._transaction():
        admitted, sealed = ctx.retriever._admitted_ids()
        row = ctx.database._execute("SELECT task_id FROM agent_traces WHERE trace_id = ?", (run_id,)).fetchone()
        if row is None or row[0] not in admitted:
            raise QaDataError(UNAVAILABLE)
        task_id = row[0]
        dataset = ctx.database._execute(
            "SELECT dataset_id FROM corpus_tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        steps = ctx.database._rows(
            "SELECT step_index, role, content_text, tool_name, command_or_action FROM trace_steps "
            "WHERE trace_id = ? AND role IN ('agent', 'assistant') ORDER BY step_index", (run_id,),
        )
        step_count = ctx.database._execute(
            "SELECT COUNT(*) FROM trace_steps WHERE trace_id = ?", (run_id,),
        ).fetchone()[0]
        facets = ctx.database._rows(
            "SELECT * FROM run_facets WHERE run_id = ? AND task_id = ?", (run_id, task_id),
        )
        findings = ctx.database._rows(
            "SELECT * FROM trace_findings WHERE run_id = ? AND task_id = ?", (run_id, task_id),
        ) if has_table(ctx.database) else []
        actors = has_actor_inputs(ctx.database, run_id)
    return {
        "task_id": task_id, "sealed": sealed, "steps": steps, "step_count": step_count,
        "dataset": dataset[0].casefold() if dataset and isinstance(dataset[0], str) else "",
        "facets": facets[0] if facets else None, "findings": findings, "actor_inputs_available": actors,
    }


def _ledger_counts(ctx: QaContext, run_id: str) -> dict[str, Any] | None:
    """Per-reader row and flag counts from the trace-tool ledgers (their own screened ``counts``)."""
    from evallab.production import qa_data_findings

    try:
        sub = dataclasses.replace(ctx, budget=Budget(_LEDGER_BUDGET))
        reply = qa_data_findings.findings(sub, run_id=run_id, limit=1)
    except QaDataError:
        return None
    counts = reply.get("counts")
    return None if counts is None else {tool: [value["total"], value["flagged"]] for tool, value in sorted(counts.items())}


def build_card(ctx: QaContext, run_id: str, *, ledgers: bool = True) -> dict[str, Any]:
    """The card of one admitted run; refuses any other id with the uniform refusal."""
    if not isinstance(run_id, str):
        raise QaDataError(UNAVAILABLE)
    data = _stored(ctx, run_id)
    sealed = _sealed_pattern(frozenset(data["sealed"]))
    withheld = [0]
    actors = data["actor_inputs_available"]

    def screen(text: str) -> str:
        return _screen(ctx, sealed, data["dataset"], text, withheld)

    header = {
        "run_id": run_id, "task_id": data["task_id"], "steps": data["step_count"],
        "actor_inputs_available": actors, "note": _NOTE,
    }
    entries = []
    for row in data["findings"]:
        # A `clear` stored before the run's steps were replaced by ones with no actor transcript is unknown now.
        stale = row["verdict"] == "clear" and not actors
        item: dict[str, Any] = {
            "detector": screen(row["detector"]), "version": screen(row["detector_version"]),
            "verdict": "unknown" if stale else row["verdict"], "kind": row["kind"],
        }
        if stale:
            item["recorded"] = "clear"
        if row["reader_model"]:
            item["reader"] = screen(row["reader_model"])
        if row["score"] is not None:
            item["score"] = round(row["score"], 4)
        if row["evidence_steps"]:
            item["steps"] = row["evidence_steps"]
        if row["summary"]:
            item["summary"] = _clip(screen(row["summary"]), 280)
        entries.append((row["produced_at"], item))
    detectors = [
        item for _, item in sorted(
            sorted(entries, key=lambda entry: entry[0], reverse=True),
            key=lambda entry: (_VERDICT_RANK[entry[1]["verdict"]], entry[1]["detector"]),
        )
    ]
    facets = None
    if data["facets"] is not None:
        facets = {field: data["facets"][field] for field in _FACET_FIELDS}
        facets["shortcut"] = _clip(screen(str(facets["shortcut"])), 60)
    flags = compute_trace_feature_flags(data["steps"])
    originals = [row["step_index"] for row in data["steps"]]
    fired = {
        name: (originals[flag["step_indices"][0]], flag["count"])
        for name, flag in flags["features"].items() if flag["fired"]
    }
    playbook = [
        {
            "card": card["card_id"], "mechanism": _clip(card["mechanism_summary"], 140),
            "verify": _clip(card["verification_recipe"], 140),
        }
        for card in match_playbook_cards(sorted(fired), "trace", 2)
    ]
    ledger = _ledger_counts(ctx, run_id) if ledgers else None

    cap = min(CARD_TOKENS, max(1, ctx.budget.remaining)) - 24  # room for the closing omitted/shortened/truncated keys
    card: dict[str, Any] = dict(header)
    omitted = shortened = 0

    def add(key: str, value: Any) -> None:
        nonlocal omitted
        if estimate_tokens({**card, key: value}) > cap:
            omitted += 1
            return
        card[key] = value

    def fit(key: str, items: list[Any], shorter: Any) -> None:
        """Whole items in priority order, each shortened before it is dropped; the first drop ends the list."""
        nonlocal omitted, shortened
        kept: list[Any] = []
        card[key] = kept
        for index, item in enumerate(items):
            for attempt, candidate in enumerate(shorter(item)):
                if estimate_tokens({**card, key: [*kept, candidate]}) <= cap:
                    kept.append(candidate)
                    shortened += attempt > 0
                    break
            else:
                omitted += len(items) - index
                return

    verdicts = [item["verdict"] for item in detectors]
    add("tally", {verdict: verdicts.count(verdict) for verdict in _VERDICT_RANK if verdict in verdicts})
    fit("detectors", detectors, lambda item: (
        item, {key: value for key, value in item.items() if key != "summary"},
        {key: value for key, value in item.items() if key not in {"summary", "steps"}},
    ))
    if facets is not None:
        add("facets", facets)
    fit(
        "flags", [f"{name}@{step}x{count}" for name, (step, count) in sorted(fired.items(), key=lambda kv: kv[1][0])],
        lambda item: (item,),
    )
    if playbook:
        fit("playbook", playbook, lambda item: (item, {key: value for key, value in item.items() if key != "verify"}))
    if ledger is not None:
        add("ledger_readers", ledger)
    if withheld[0]:
        add("withheld_strings", withheld[0])
    card["omitted"] = omitted
    if shortened:
        card["shortened"] = shortened
    card["truncated"] = omitted > 0 or shortened > 0
    ctx.budget.used += estimate_tokens(card)
    ctx.budget.omitted += omitted
    return card
