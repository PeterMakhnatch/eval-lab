"""Cloud trace DB runs: labelled cheat positives and real clean negatives.

Reads ``agent_traces`` joined to ``corpus_tasks`` from ``ctx.cloud_db``
(read-only). One run is at most one item per kind:

* ``is_exploit = 1`` -> ``cheat`` positive. Goodfire rows with a non-empty
  ``flagged_passage_steps`` are ``passage`` granularity (``positive_units`` =
  flagged steps, ``locator`` empty); everything else is ``run``.
* ``is_task_defect_fail = 1`` -> ``task_defect`` positive.
* Negatives are only runs the source itself labels clean: Goodfire judged
  ``verdict=no`` and ``verdict_enacted=no`` (the CM-6 "clean" stratum), and
  ``source_label:neither`` runs (CheatBench human-benign, HackTrace ACT, the
  PR #1137 census). Goodfire "considered-only" (``yes``/``no``), ``split``
  verdicts and unjudged rows, and every other non-exploit run (MiMo/TB audit
  controls, candidate attempts), carry no clean label and are not items.
* A run without an actor transcript is "unknown, never clean": it is never a
  negative.

Only DEV runs that pass the shared admission gate (``admitted_cloud_task_ids``
over the registry's ``assignments.csv`` authority) and whose strict split is
still DEV are DEV, can be reached by ``evallab trace run --runs <trace_id>``,
and have their steps read (only to decide ``actor_transcript``). Every other
row keeps its declared split (``held`` -> ``heldout``); a DEV declaration that
is not admitted is never DEV.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
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

SOURCE = "cloud-traces"
STORE = "cloud-trace-db:agent_traces"
GOODFIRE_DATASET = "goodfire-reward-hack-data"
GOODFIRE_LICENSE = "none-declared; internal research only"
CHEATBENCH_KEY = "cheatbench-actions-v1"
CHEATBENCH_LICENSE = "MIT"
NEITHER = "source_label:neither"
_TIER_LABEL: dict[str, LabelType] = {
    "tier1_container_verified": "executed",
    "tier2_consensus_labeled": "consensus",
    "tier3_provisional": "judge",
}
_NOTE_LIMIT = 300
_RECEIPT = "a.verified_receipt_json"


def _receipt(path: str, alias: str) -> str:
    return f"CASE WHEN json_valid({_RECEIPT}) THEN json_extract({_RECEIPT}, '{path}') END AS {alias}"


_SQL = (
    "SELECT a.trace_id, a.task_id, a.outcome, a.is_exploit, a.is_task_defect_fail,"
    " a.trust_tier, a.flagged_passage_steps, c.dataset_id, c.split,"
    " c.original_grader_digest, c.repaired_grader_digest,"
    " CASE WHEN json_valid(c.metadata_json) AND json_type(c.metadata_json, '$.item_id') = 'text'"
    " THEN json_extract(c.metadata_json, '$.item_id') END AS item_id, "
    + ", ".join((
        _receipt("$.admission.effective_split", "receipt_split"),
        _receipt("$.source_label.judged", "judged"),
        _receipt("$.source_label.verdict", "verdict"),
        _receipt("$.source_label.verdict_enacted", "enacted"),
        _receipt("$.source_label.source_protocol", "protocol"),
        _receipt("$.label_provenance", "provenance"),
        _receipt("$.measured", "measured"),
        _receipt("$.canonical_admission.dataset_id", "canonical_dataset"),
        _receipt("$.canonical_admission.item_id", "canonical_item"),
    ))
    + " FROM agent_traces a JOIN corpus_tasks c USING(task_id) ORDER BY a.trace_id"
)
_IDENTITY_SQL = (
    "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
    " FROM corpus_tasks ORDER BY task_id"
)


def _admitted(connection: sqlite3.Connection, ctx: BankContext) -> frozenset[str]:
    """Tasks the shared cross-key gate admits as DEV (the Run Atlas loader's gate)."""
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids

    identities = [dict(row) for row in connection.execute(_IDENTITY_SQL)]
    return admitted_cloud_task_ids(identities, registry=ctx.registry)


def _has_actor_transcript(connection: sqlite3.Connection, trace_id: str) -> bool:
    """The exports' ``actor_inputs_available`` rule over the run's stored steps.

    Same per-step projection as ``trace_findings.has_actor_inputs``: ATIF, then
    ``blind_atif``, then "some agent step has text or a tool call". Stops at the
    first step that counts.
    """
    from evallab.cheatbench_fidelity import blind_atif
    from evallab.production.cloud_trace_export import _actor_inputs_available, _atif

    cursor = connection.execute(
        "SELECT step_index, role, content_text, tool_name, command_or_action FROM trace_steps"
        " WHERE trace_id = ? ORDER BY step_index",
        (trace_id,),
    )
    for step_index, role, content, tool_name, command in cursor:
        step = {
            "step_index": step_index, "role": role or "", "content_text": content,
            "tool_name": tool_name, "command_or_action": command, "observation_preview": None,
        }
        if _actor_inputs_available(blind_atif(_atif([step], "probe"), item_id="probe")["steps"]):
            return True
    return False


def _flagged_steps(value: Any) -> int:
    """Distinct flagged step indices in the stored ``flagged_passage_steps`` list."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return 0
    if not isinstance(parsed, list):
        return 0
    return len({step for step in parsed if isinstance(step, int) and not isinstance(step, bool)})


def _declared(row: Mapping[str, Any]) -> str:
    """The split as stored (``held`` is the DB's spelling of ``heldout``)."""
    text = str(row["split"]).strip().lower()
    return "heldout" if text in {"held", "held_out", "held-out"} else text


def _tighter(row: Mapping[str, Any], declared: str) -> str:
    """A receipt that says the run is not DEV tightens a DEV declaration."""
    effective = row["receipt_split"]
    if declared == "dev" and isinstance(effective, str) and effective.strip().lower() != "dev":
        return "heldout" if effective.strip().lower() in {"held", "heldout"} else effective.strip().lower()
    return declared


def _non_goodfire_type(row: Mapping[str, Any], polarity: str) -> LabelType:
    provenance = str(row["provenance"] or "")
    if "human-verified" in provenance:
        return "human"
    if str(row["outcome"]).startswith("source_label:"):
        if "method=deterministic" in provenance:
            return "detector"
        if "method=judge" in provenance:
            return "judge"
        if "deterministic negative rule" in provenance:
            # HackTrace ACT: judge-labelled positives, rule-labelled negatives.
            return "judge" if polarity == "positive" else "detector"
    if row["measured"] in (1, True):
        return "executed"  # a retained native execution measured the exploit
    return _TIER_LABEL[row["trust_tier"]]


def _goodfire_state(row: Mapping[str, Any]) -> str | None:
    """``positive`` | ``negative`` | None (considered-only, split, unjudged, unlabelled)."""
    if row["is_exploit"]:
        return "positive"
    if row["judged"] not in (1, True):
        return None
    verdict, enacted = row["verdict"], row["enacted"]
    if (verdict, enacted) == ("yes", "yes"):
        return "positive"
    if (verdict, enacted) == ("no", "no"):
        return "negative"
    return None


def _cheat_verdict(row: Mapping[str, Any]) -> tuple[str, LabelType, str] | None:
    if row["dataset_id"] == GOODFIRE_DATASET:
        state = _goodfire_state(row)
        if state is None:
            return None
        label = f"{row['protocol']}: verdict={row['verdict']} enacted={row['enacted']}"
        return state, "consensus", label
    if row["is_exploit"]:
        label = str(row["outcome"]) if str(row["outcome"]).startswith("source_label:") else (
            f"is_exploit=1 ({row['trust_tier']})"
        )
        return "positive", _non_goodfire_type(row, "positive"), label
    if row["outcome"] == NEITHER:
        return "negative", _non_goodfire_type(row, "negative"), NEITHER
    return None


def _license(row: Mapping[str, Any]) -> str | None:
    if row["dataset_id"] == GOODFIRE_DATASET:
        return GOODFIRE_LICENSE
    if row["canonical_dataset"] == CHEATBENCH_KEY:
        return CHEATBENCH_LICENSE
    return None


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    from evallab.production.run_atlas import connect_readonly

    connection = connect_readonly(ctx.cloud_db)
    try:
        admitted = _admitted(connection, ctx)
        splits: dict[tuple[Any, ...], Split] = {}

        def strict(row: Mapping[str, Any], declared: str) -> Split:
            keys = [(row["dataset_id"], row["item_id"])]
            if isinstance(row["canonical_dataset"], str) and isinstance(row["canonical_item"], str):
                keys.append((row["canonical_dataset"], row["canonical_item"]))
            result: Split = "DEV"
            for key_id, item_id in keys:
                cache = (declared, key_id, item_id, row["task_id"], row["original_grader_digest"])
                if cache not in splits:
                    splits[cache] = ctx.strict_split(
                        declared, key_id=key_id, item_id=item_id, task_id=row["task_id"],
                        task_digest=row["original_grader_digest"],
                    )
                if splits[cache] != "DEV":
                    return splits[cache]
                result = splits[cache]
            return result

        for record in connection.execute(_SQL):
            row = dict(record)
            flagged = _flagged_steps(row["flagged_passage_steps"])
            cheat = _cheat_verdict(row)
            defect = bool(row["is_task_defect_fail"])
            if cheat is None and not defect:
                continue
            declared = _tighter(row, _declared(row))
            split = strict(row, declared)
            dev = split == "DEV" and row["task_id"] in admitted
            if split == "DEV" and not dev:
                split = "unassigned"  # DEV declared, but the admission gate does not admit it
            actor: bool | None = _has_actor_transcript(connection, row["trace_id"]) if dev else None
            common: dict[str, Any] = {
                "source": SOURCE, "subject_type": "run", "subject_id": row["trace_id"],
                "task_id": row["task_id"], "declared_split": str(row["split"]),
                "split": split, "license": _license(row), "actor_transcript": actor,
                "harness_selector": row["trace_id"] if dev else None,
                "provenance": Provenance(
                    store=STORE, ref=row["trace_id"],
                    note=str(row["provenance"])[:_NOTE_LIMIT] if row["provenance"] else None,
                ),
            }
            if cheat is not None:
                polarity, label_type, label = cheat
                # No actor transcript is unknown, never clean.
                if not (polarity == "negative" and actor is False):
                    passage = polarity == "positive" and flagged > 0
                    yield new_item(
                        **common, kind="cheat", polarity=polarity, label_type=label_type,
                        label=label[:200], granularity="passage" if passage else "run",
                        positive_units=flagged if passage else None,
                    )
            if defect:
                yield new_item(
                    **common, kind="task_defect", polarity="positive",
                    label_type=_TIER_LABEL[row["trust_tier"]],
                    label=f"is_task_defect_fail=1 ({row['trust_tier']})", granularity="run",
                )
    finally:
        connection.close()
