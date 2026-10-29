"""Blind retrieval adapter for the reward seeker's Run-Atlas evidence path.

The seeker's turn-2 prompt is built for an agent that must not learn the attacked
task's own answer, its split, or the source benchmark name. The shared example
retriever (``CloudTraceDatabase.retrieve_similar_examples``) is built for QA
workers instead: its rendered block carries each example's ``instruction`` ("task
asks"), differential, reproducer and trust tier. Since NP22 the shared renderer
itself withholds ``dataset_id``, ``split``, ``label_categories`` and
``receipt_status``, and an Atlas query must name an exclusion; this adapter
remains the seeker's wall for everything else.

``blind_examples`` always excludes the attacked task, drops a same-task example
even if a retriever returns one anyway, and withholds the answer, label, split and
source fields before the block is rendered. The shared API keeps its documented
behaviour, so worker-facing callers are unchanged.

What "blind" means is defined, and checkable, in
:mod:`evallab.production.retrieval_leak_check` (profile ``blind``): a fixed list of
forbidden fields and tokens searched in the rendered text. SB-5 measured the input
to this decision on the 44-task DEV cohort; NP23 found that the first version of
this adapter still rendered the benchmark name (inside task and trace identifiers),
the word "reward" (inside the same identifiers) and ``FLAGGED`` passage markers, so
identifiers are now opaque handles and the passage label is cleared.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from evallab.production.cloud_trace_db import RetrievedExample
from evallab.production.corpus_queue import CorpusRegistry

#: The only provenance keys a blind reader sees. Everything else (labels, split,
#: trust tiers, CAS URIs, passage markers, identifiers that embed the source
#: benchmark's name, and any key a later change adds) is dropped: an allowlist,
#: because a denylist leaks the next key someone stores.
BLIND_ALLOWED_PROVENANCE = frozenset(
    {"step_indices", "model_id", "facet_similarity", "run_facets_matched"}
)

#: Step keys the renderers read; a step's own ``trace_id``, CAS URI and feature
#: flags are not among them.
_STEP_KEYS = frozenset(
    {"step_index", "role", "tool_name", "command_or_action", "content_text", "observation_preview", "flagged"}
)

#: Flat fields that reproduce another task's prompt or its graded verdict.
BLIND_WITHHELD_FIELDS = ("instruction", "differential", "reproducer")


class ExampleRetriever(Protocol):
    """The subset of ``CloudTraceDatabase.CloudTraceRetriever`` this adapter uses."""

    def retrieve_similar_examples(self, query: str, *args: Any, **kwargs: Any) -> list[RetrievedExample]: ...


def opaque_id(kind: str, value: str | None) -> str | None:
    """A stable, one-way handle for an identifier that embeds the source benchmark's name.

    Idempotent, so blinding an already blinded example changes nothing. The handle
    is not a lookup key: a blind reader has no tool to expand it.
    """
    if not value or re.fullmatch(r"(example|issue|trace)-[0-9a-f]{10}", value):
        return value
    return f"{kind}-{hashlib.sha256(value.encode()).hexdigest()[:10]}"


def _allowed(provenance: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in provenance.items() if key in BLIND_ALLOWED_PROVENANCE}


def blind_example(example: RetrievedExample) -> RetrievedExample:
    """One example with the answer, label, split and source fields withheld.

    ``dataset_id`` and ``trust_tier`` are cleared as attributes too, not only in
    provenance: the rendered block prints them in its ``- source:`` and
    ``- trust:`` lines, which is how the source benchmark name and the label
    tier would otherwise reach a blind reader. Task, issue and trace identifiers
    embed the benchmark name (and, for a reward-hacking corpus, the word
    "reward"), so they become opaque handles, and the publisher's per-step
    ``FLAGGED`` passage label is cleared.
    """
    return replace(
        example,
        task_id=opaque_id("example", example.task_id) or "",
        issue_id=opaque_id("issue", example.issue_id),
        trace_id=opaque_id("trace", example.trace_id),
        instruction=None,
        differential=None,
        reproducer=None,
        citation=None,
        issue=None,
        dataset_id="",
        split="",
        trust_tier=None,
        steps=tuple(replace(step, flagged=False) for step in example.steps),
        trace_steps=[{**{k: v for k, v in step.items() if k in _STEP_KEYS}, "flagged": False} for step in example.trace_steps],
        provenance=_allowed(example.provenance),
        prompt_text="",
    )


def blind_examples(
    retriever: ExampleRetriever,
    for_task_id: str,
    query: str,
    *,
    exclude_task_ids: Iterable[str] = (),
    **kwargs: Any,
) -> list[RetrievedExample]:
    """Retrieve examples a blind seeker may see while it attacks ``for_task_id``.

    ``for_task_id`` and every id in ``exclude_task_ids`` are always excluded, so
    the caller cannot lose the exclusion by omitting a keyword. A retriever that
    returns the attacked task anyway (a stub, a future regression) is filtered
    here rather than trusted, and each surviving example is blinded.

    Raises ``ValueError`` for a blank target before the store is touched.
    """
    if not isinstance(for_task_id, str) or not for_task_id.strip():
        raise ValueError("for_task_id must be a nonblank task id")
    target = for_task_id.strip()
    target_name = CorpusRegistry.task_name(target)
    excluded = [target]
    for extra in exclude_task_ids:
        if not isinstance(extra, str) or not extra.strip():
            raise ValueError("exclude_task_ids must hold nonblank task ids")
        excluded.append(extra.strip())

    found = retriever.retrieve_similar_examples(
        query, exclude_task_ids=tuple(dict.fromkeys(excluded)), **kwargs
    )
    kept: list[RetrievedExample] = []
    for example in found:
        name = CorpusRegistry.task_name(example.task_id)
        if example.task_id == target or name == target_name:
            continue
        kept.append(blind_example(example))
    return kept


def render_blind(examples: Sequence[RetrievedExample]) -> str:
    """Render blinded examples exactly as the prompt builder expects.

    Kept separate from :func:`blind_examples` so a caller that already holds
    examples can blind-then-render without a second retrieval.
    """
    from evallab.production.cloud_trace_db import format_retrieval_prompt_block

    return format_retrieval_prompt_block(tuple(blind_example(e) for e in examples))
