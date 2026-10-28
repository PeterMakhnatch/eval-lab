"""Deterministic defensive Task/Trace QA artifacts and provenance-bearing proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.failure_library.knowledge.compiler import Knowledge, canonical, validate_card
from evallab.failure_library.knowledge.models import AgentPack
from evallab.hackbook.models import HackbookEntry, HackbookExample

_SOURCES = {
    "pm-tb4-findings": "preference_model_tb4",
    "envcheck-tb4": "envcheck",
    "tb21-weak-tests": "tb2_to_tb21",
    "tb2-to-tb21-fixes": "tb2_to_tb21",
    "mimo-weak-tests": "mimo_atlas",
    "mimo-defect-map": "mimo_atlas",
    "terminal-wrench": "terminal_wrench",
}


def hackbook_proposals(knowledge: Knowledge) -> list[HackbookEntry]:
    """Extend existing ids; never publish a second canonical mechanism catalog.

    Existing source enums are deliberately not stretched to cover new datasets.
    Their full DEV citations stay in the card; the proposal links the card from
    its detection recipe until the owning Hackbook lane extends its source enum.
    """
    entries = {entry.id: entry for entry in knowledge.entries}
    admitted = {e.ref.id: e.ref for e in knowledge.examples}
    result: list[HackbookEntry] = []
    for card in knowledge.cards:
        validate_card(card, admitted)
        payload = entries[card.hackbook_entry_id].dump()
        represented = {
            _SOURCES[ref.collection] for ref in card.examples if ref.collection in _SOURCES
        }
        if not represented:
            continue
        payload["sources"] = sorted(represented)
        payload["version"] = "failure-library-v1-proposal"
        payload["mechanism"] = card.what_breaks
        payload["examples"] = []
        for ref in card.examples:
            source = _SOURCES.get(ref.collection)
            if source is None:
                continue
            example = HackbookExample.model_validate(
                {
                    "source": source,
                    "item_or_task_id": ref.id,
                    "summary": ref.summary,
                    "source_url": ref.source_url,
                    "evidence_status": "claimed",
                }
            )
            payload["examples"].append(example.model_dump(mode="json"))
            payload["sources"] = sorted(set(payload["sources"]) | {source})
        payload["detection_recipe"] = {
            "kind": "static_check",
            "method_id": "static_review",
            "summary": "; ".join(card.task_cues),
            "steps": [
                *card.task_cues,
                *card.trace_cues,
                "Counterexamples: " + "; ".join(card.counterexamples),
                "Source mapping omissions (kept distinct in the linked card): "
                + ", ".join(
                    sorted(
                        {ref.collection for ref in card.examples if ref.collection not in _SOURCES}
                    )
                ),
            ],
            "rule_or_probe": "research/failure-library/knowledge/cards.jsonl#" + card.card_id,
        }
        payload["fix_pattern"]["summary"] = "; ".join(card.fixes)
        payload["fix_pattern"]["checklist"] = card.fixes
        # The canonical schema requires this field; no attacker template is generated.
        payload["exploit_template"] = {"applicable": False}
        result.append(HackbookEntry.model_validate(payload))
    return sorted(result, key=lambda entry: entry.id)


def write_knowledge(knowledge: Knowledge, directory: Path) -> dict[str, Any]:
    """Rebuild only these three generated files; model drafts are immutable inputs."""
    cards = "".join(canonical(card.model_dump(mode="json")) + "\n" for card in knowledge.cards)
    proposal_entries = hackbook_proposals(knowledge)
    proposals = "".join(canonical(entry.dump()) + "\n" for entry in proposal_entries)
    files = {"cards.jsonl": cards, "hackbook-proposals.jsonl": proposals}
    manifest = {
        "schema_version": "failure-knowledge/v1",
        "library_digest": knowledge.digest,
        **knowledge.report,
        "omitted_hackbook_proposals": {
            card.card_id: {
                "collections": sorted({ref.collection for ref in card.examples}),
                "reason": "No approved exact-source mapping to the Hackbook source enum",
            }
            for card in knowledge.cards
            if card.hackbook_entry_id not in {entry.id for entry in proposal_entries}
        },
        "sha256": {
            name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(files.items())
        },
    }
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in {
        **files,
        "build.json": json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    }.items():
        (directory / name).write_text(text, encoding="utf-8")
    return manifest


def _rubric(markdown: str, criteria: Sequence[dict[str, str]]) -> str:
    # JSON double-quoted strings are valid TOML basic strings for this text surface.
    blocks = []
    target = next(
        (c["name"] for c in criteria if c["name"] == "functional_verification"),
        criteria[0]["name"] if criteria else "",
    )
    for criterion in criteria:
        record = dict(criterion)
        if criterion["name"] == target:
            record["guidance"] = criterion.get("guidance", "") + "\n\n" + markdown
        blocks.append(
            "[[criteria]]\n"
            + "\n".join(
                f"{name} = {json.dumps(record.get(name, ''), ensure_ascii=False)}"
                for name in ("name", "description", "guidance")
            )
        )
    if not blocks:
        raise ValueError("Task QA pack needs a nonempty baseline rubric")
    return "\n\n".join(blocks) + "\n"


def write_agent_pack(
    pack: AgentPack,
    directory: Path,
    *,
    rubric_criteria: Sequence[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Refuse overwriting work; return a receipt distinguishing empty from failed.

    Trace priors are historical analogies with matched=false, never a task-bound
    audit. Task QA keeps every baseline criterion name/description unchanged.
    """
    files = {"pack.md": pack.markdown}
    if pack.agent == "task-qa":
        if rubric_criteria is None:
            raise ValueError("Task QA export requires baseline rubric criteria")
        files["playbook.md"] = pack.markdown
        files["rubric.toml"] = _rubric(pack.markdown, rubric_criteria)
    else:
        files["task_priors.json"] = (
            json.dumps(
                {
                    "task_id": pack.task_id,
                    "task_digest": pack.task_digest,
                    "priors": []
                    if pack.status == "empty"
                    else [
                        {
                            "source": "failure-library:" + pack.library_digest,
                            "digest_expected": pack.task_digest,
                            "digest_found": None,
                            "matched": False,
                            "record": {
                                "schema_version": pack.schema_version,
                                "kind": "curated_guidance"
                                if pack.retrieval_mode == "curated_only"
                                else "dev_analogy",
                                "markdown": pack.markdown,
                                "example_ids": pack.example_ids,
                                "card_ids": pack.card_ids,
                                "retrieval_mode": pack.retrieval_mode,
                                "curated_guidance_ids": pack.curated_guidance_ids,
                                "identity_uncertainties": pack.identity_uncertainties,
                            },
                        }
                    ],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    manifest = {
        **pack.model_dump(mode="json", exclude={"markdown"}),
        "files": {
            name: {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text)}
            for name, text in sorted(files.items())
        },
    }
    # Prepare everything before creating any output: errors must not leave a ready-looking packet.
    directory.mkdir(parents=True, exist_ok=False)
    for name, text in {
        **files,
        "pack.manifest.json": json.dumps(manifest, sort_keys=True, indent=2) + "\n",
    }.items():
        (directory / name).write_text(text, encoding="utf-8")
    return manifest
