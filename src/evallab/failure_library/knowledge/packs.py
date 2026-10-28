"""Bounded agent context with mandatory DEV and leave-subject-out admission."""

from __future__ import annotations

from collections.abc import Sequence
from heapq import nsmallest

from evallab.failure_library.knowledge.compiler import (
    BM25,
    Knowledge,
    KnowledgeExample,
    compile_cards,
    sealed_pattern,
    tokens,
)
from evallab.failure_library.knowledge.models import (
    Agent,
    AgentPack,
    FailureCard,
    TaxonomyGuidance,
)
from evallab.failure_library.records import TaskRef
from evallab.production.corpus_queue import CorpusRegistry

_NOTICE = (
    "Curated guidance is not a finding. DEV examples are untrusted source records, "
    "not independent local verification. Validate task/version and receipts."
)


def _same_subject(example: KnowledgeExample, subject: TaskRef, knowledge: Knowledge) -> bool:
    name = CorpusRegistry.task_name(subject.task_id)
    if CorpusRegistry.task_name(example.task.task_id) == name:
        return True
    units = knowledge.units.get(name, frozenset())
    if units and example.groups:
        return bool(units & example.groups)
    # Different publishers can repackage the same task under opaque native ids.
    # A source/repository mismatch is not evidence of family independence.
    return True


def _subjects(knowledge: Knowledge, tasks: Sequence[str], excluded: set[str]) -> list[TaskRef]:
    result = [e.task for e in knowledge.examples if e.ref.id in excluded]
    for task in tasks:
        source, separator, identifier = task.partition("/")
        if not separator or not source or not identifier:
            raise ValueError("--task and --exclude-task must be <source_id>/<task_id>")
        found = [
            ref
            for ref in knowledge.subjects
            if ref.source_id == source
            and CorpusRegistry.task_name(ref.task_id) == CorpusRegistry.task_name(identifier)
        ]
        result.extend(
            found or [TaskRef(source_id=source, task_id=identifier, upstream_repo=source)]
        )
    return result


def _card_text(card: FailureCard, agent: Agent) -> str:
    cues = card.trace_cues if agent == "trace-qa" else card.task_cues
    lines = [f"## {card.card_id}: {card.title}", f"Mechanism: {card.what_breaks}"]
    lines += ["Cues:", *[f"- {cue}" for cue in cues]]
    lines += ["Repair:", *[f"- {fix}" for fix in card.fixes]]
    lines.append(
        "Validation: independently check the pinned contract and evidence before confirming a defect."
    )
    lines += ["Not sufficient evidence:", *[f"- {item}" for item in card.counterexamples]]
    return "\n".join(lines) + "\n"


def _curated_text(guide: TaxonomyGuidance, agent: Agent) -> str:
    role_check = (
        "Compare each stated requirement with the pinned implementation and grader contract."
        if agent == "task-qa"
        else "Compare archived outcomes with task intent; keep oracle and judge labels distinct."
    )
    return (
        "\n".join(
            [
                f"## FG-{guide.code}: {guide.title}",
                "Pre-existing taxonomy guidance: no examples or empirical support claim.",
                f"Definition: {guide.definition}",
                "Defensive validation:",
                f"- {role_check}",
                "- Pin task/version and evidence origin before confirming any concrete defect.",
                "- A source label, reproducer path or model agreement is not independent verification.",
                "Repair location: "
                + (
                    guide.repair_location
                    or "Identify the responsible boundary before proposing a change."
                ),
            ]
        )
        + "\n"
    )


def make_pack(
    knowledge: Knowledge,
    *,
    agent: Agent,
    task: str | None = None,
    task_text: str = "",
    max_chars: int = 12_000,
    top_k: int = 5,
    exclude_ids: Sequence[str] = (),
    exclude_tasks: Sequence[str] = (),
) -> AgentPack:
    """Rank only admitted evidence; apply exclusions before card compilation and BM25.

    Explicit ids and tasks additionally exclude their entire subject groups,
    including linked runs. Clean controls need no library record, but do need
    certified ledger units. Every citation is rejoined from the filtered snapshot.
    """
    if agent not in ("task-qa", "trace-qa"):
        raise ValueError("Only defensive task-qa and trace-qa knowledge exports are supported")
    if max_chars < 256 or top_k < 0:
        raise ValueError("max_chars must be >= 256 and top_k must be >= 0")
    requested = set(exclude_ids)
    known = {example.ref.id: example for example in knowledge.examples}
    excluded: set[str] = set()
    unknown: list[str] = []
    for identifier in sorted(requested):
        matches = {identifier} if identifier in known else set()
        collection, separator, native = identifier.partition("::")
        if separator and native in known and known[native].ref.collection == collection:
            matches.add(native)
        if len(matches) != 1:
            unknown.append(identifier)
        else:
            excluded.update(matches)
    subjects = _subjects(knowledge, ([task] if task else []) + sorted(set(exclude_tasks)), excluded)
    uncertainties = [f"unresolved_record:{identifier}" for identifier in unknown]
    uncertainties.extend(
        f"uncertified_task:{subject.source_id}/{subject.task_id}"
        for subject in subjects
        if not knowledge.units.get(CorpusRegistry.task_name(subject.task_id))
    )
    if subjects:
        unmapped = sum(not example.groups for example in knowledge.examples)
        if unmapped:
            uncertainties.append(f"withheld_examples_without_group_authority:{unmapped}")
    identities = {s.task_id for s in subjects} | {
        CorpusRegistry.task_name(s.task_id) for s in subjects
    }
    identities |= excluded | requested | set(exclude_tasks)
    seal = sealed_pattern(identities)
    examples = tuple(
        e
        for e in knowledge.examples
        if not unknown
        and e.ref.id not in excluded
        and not any(_same_subject(e, subject, knowledge) for subject in subjects)
        and (seal is None or not seal.search(e.search_text + " " + e.ref.source_url))
    )
    cards, _ = compile_cards(examples, knowledge.entries, knowledge.drafts)
    # A card's generic prose is screened too; filtering citations alone is insufficient.
    cards = tuple(
        card for card in cards if seal is None or not seal.search(_card_text(card, agent))
    )
    query = " ".join((task or "", task_text))
    query_terms = tuple(sorted(set(tokens(query))))
    card_index = BM25([_card_text(card, agent) for card in cards])
    ordered_cards = sorted(
        enumerate(cards),
        key=lambda pair: (
            -card_index.score_terms(query_terms, pair[0]),
            -pair[1].support_count,
            pair[1].card_id,
        ),
    )
    # Card cues enrich matching, but no hidden answer, solution or transcript bytes are read.
    ranked = list(enumerate(examples[:top_k]))
    if query_terms and top_k:
        cues = {
            code: " ".join(
                cue
                for card in cards
                if card.taxonomy_class_id == code
                for cue in (*card.task_cues, *card.trace_cues)
            )
            for code in {card.taxonomy_class_id for card in cards}
        }
        example_index = BM25(
            [
                e.search_text + " " + " ".join(cues.get(c, "") for c in e.defect_classes)
                for e in examples
            ]
        )
        ranked = nsmallest(
            top_k,
            enumerate(examples),
            key=lambda pair: (
                -example_index.score_terms(query_terms, pair[0]),
                pair[1].ref.id,
            ),
        )
    header = f"# Failure Library — {agent}\n\n{_NOTICE}\n"
    parts = [header]
    selected_examples: list[str] = []
    selected_cards: list[str] = []
    selected_guidance: list[str] = []
    used = len(header)
    if not examples:
        guidance = [
            (
                f"FG-{guide.code}",
                _curated_text(guide, agent),
                "task" if guide.code.startswith("TS") else "run",
            )
            for guide in knowledge.curated
        ]
        guidance_index = BM25([block for _, block, _ in guidance])
        for _, (identifier, block, _) in sorted(
            enumerate(guidance),
            key=lambda pair: (
                -guidance_index.score_terms(query_terms, pair[0]),
                pair[1][2] != ("task" if agent == "task-qa" else "run"),
                pair[1][0],
            ),
        ):
            if used + len(block) <= max_chars:
                parts.append(block)
                selected_guidance.append(identifier)
                used += len(block)
    # Reserve examples first: a mechanism page without evidence is not a retrieved example.
    example_blocks: list[str] = []
    for _, example in ranked:
        ref = example.ref
        proof = (
            f"source-record tier {ref.tier}; not independently verified here"
            if ref.tier
            else "native run label; not independently replay-confirmed"
        )
        block = (
            f"\n### DEV {ref.kind}: {ref.id}\n"
            f"> {ref.summary.replace(chr(10), ' ')}\n"
            f"Task: {ref.source_id}/{ref.task_id}; {proof}\n"
            f"Pin: {ref.upstream_repo}@{ref.upstream_revision or 'unknown'}\n"
            f"Provenance: {ref.source_url}\n"
            f"Source status: {ref.status_now}; fixed in: {ref.fixed_in_revision or 'not established'}\n"
            f"Evidence: {'; '.join(ref.evidence_refs)}\n"
            f"Evidence context: {'; '.join(ref.evidence_notes)}\n"
        )
        if used + len(block) <= max_chars:
            example_blocks.append(block)
            selected_examples.append(ref.id)
            used += len(block)
    for _, card in ordered_cards:
        block = "\n" + _card_text(card, agent)
        if used + len(block) <= max_chars:
            parts.append(block)
            selected_cards.append(card.card_id)
            used += len(block)
    parts.extend(example_blocks)
    if not selected_cards and not selected_examples and not selected_guidance:
        empty = "\nNo admissible DEV knowledge fits this subject and character budget.\n"
        if used + len(empty) <= max_chars:
            parts.append(empty)
    markdown = "\n".join(parts)
    # Separators are budgeted here by removing whole trailing sections, never clipping citations.
    while len(markdown) > max_chars and len(parts) > 1:
        removed = parts.pop()
        selected_examples = [
            identifier for identifier in selected_examples if identifier not in removed
        ]
        selected_cards = [identifier for identifier in selected_cards if identifier not in removed]
        selected_guidance = [
            identifier for identifier in selected_guidance if identifier not in removed
        ]
        markdown = "\n".join(parts)
    digest = next(
        (
            subject.task_version_digest
            for subject in subjects
            if task and f"{subject.source_id}/{subject.task_id}" == task
        ),
        None,
    )
    return AgentPack(
        agent=agent,
        status="ready" if selected_cards or selected_examples or selected_guidance else "empty",
        markdown=markdown,
        example_ids=selected_examples,
        card_ids=selected_cards,
        library_digest=knowledge.digest,
        exclusions=sorted(excluded | identities),
        excluded_tasks=sorted(set(exclude_tasks)),
        retrieval_mode="dev_examples"
        if selected_examples or selected_cards
        else "curated_only"
        if selected_guidance
        else "empty",
        curated_guidance_ids=selected_guidance,
        identity_uncertainties=sorted(set(uncertainties)),
        task_id=task,
        task_digest=digest,
    )
