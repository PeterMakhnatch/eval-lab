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


def _screen(ctx: QaContext, sealed: Any, text: str, counter: list[int]) -> str:
    """Text with opaque identities scrubbed; replaced outright when it names a sealed identity."""
    from evallab.production.qa_data_findings import _OPAQUE

    if ctx.retriever._sealed_text(text, sealed):
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
    header = {
        "run_id": run_id, "task_id": data["task_id"], "steps": data["step_count"],
        "actor_inputs_available": actors, "note": _NOTE,
    }
    ranked = sorted(
        sorted(data["findings"], key=lambda row: row["produced_at"], reverse=True),
        key=lambda row: (_VERDICT_RANK[row["verdict"]], row["detector"]),
    )
    detectors = []
    for row in ranked:
        item: dict[str, Any] = {
            "detector": _screen(ctx, sealed, row["detector"], withheld), "version": row["detector_version"],
            "verdict": row["verdict"], "kind": row["kind"],
        }
        if row["reader_model"]:
            item["reader"] = _screen(ctx, sealed, row["reader_model"], withheld)
        if row["score"] is not None:
            item["score"] = round(row["score"], 4)
        if row["evidence_steps"]:
            item["steps"] = row["evidence_steps"]
        if row["summary"]:
            item["summary"] = _clip(_screen(ctx, sealed, row["summary"], withheld), 280)
        detectors.append(item)
    facets = None
    if data["facets"] is not None:
        facets = {field: data["facets"][field] for field in _FACET_FIELDS}
        facets["shortcut"] = _clip(_screen(ctx, sealed, str(facets["shortcut"]), withheld), 60)
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

    cap = min(CARD_TOKENS, max(1, ctx.budget.remaining)) - 12  # room for the closing omitted/truncated keys
    card: dict[str, Any] = dict(header)
    omitted = 0

    def add(key: str, value: Any) -> bool:
        nonlocal omitted
        trial = {**card, key: value}
        if estimate_tokens(trial) > cap:
            omitted += len(value) if isinstance(value, list) else 1
            return False
        card[key] = value
        return True

    tally = {verdict: sum(row["verdict"] == verdict for row in data["findings"]) for verdict in _VERDICT_RANK}
    add("tally", {key: value for key, value in tally.items() if value})
    shown: list[dict[str, Any]] = []
    for item in detectors:
        if estimate_tokens({**card, "detectors": [*shown, item]}) > cap:
            omitted += 1
            continue
        shown.append(item)
    card["detectors"] = shown
    if facets is not None:
        add("facets", facets)
    add("flags", [f"{name}@{step}x{count}" for name, (step, count) in sorted(fired.items(), key=lambda kv: kv[1][0])])
    for item in playbook:
        shown_cards = [*card.get("playbook", []), item]
        if estimate_tokens({**card, "playbook": shown_cards}) > cap:
            omitted += 1
            continue
        card["playbook"] = shown_cards
    if ledger is not None:
        add("ledger_readers", ledger)
    if withheld[0]:
        add("withheld_strings", withheld[0])
    card["omitted"] = omitted
    card["truncated"] = omitted > 0
    ctx.budget.used += estimate_tokens(card)
    ctx.budget.omitted += omitted
    return card
