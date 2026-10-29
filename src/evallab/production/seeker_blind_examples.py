"""Blind retrieval adapter for the reward seeker's Run-Atlas evidence path.

The seeker's turn-2 prompt is built for an agent that must not learn the attacked
task's own answer, its split, or the source benchmark name. The shared example
retriever (``CloudTraceDatabase.retrieve_similar_examples``) is built for QA
workers instead: it excludes only the task ids a caller names, and its rendered
prompt carries each example's ``instruction`` ("task asks"), label-bearing
provenance (``label_categories``, ``receipt_status``, trust tiers) and the source
``dataset_id`` and ``split``.

``blind_examples`` closes that gap for seeker callers only. It always excludes the
attacked task, drops a same-task example even if a retriever returns one anyway,
and withholds the answer, label, split and source fields before the block is
rendered. The shared API keeps its documented behaviour, so worker-facing
callers are unchanged.

SB-5 measured the input to this decision on the 44-task DEV cohort (44 targets x
3 modes, 396 examples): zero same-task examples, zero ``differential`` or
``reproducer`` fields, but every facet-path example carried an ``instruction``
and label-bearing provenance keys, and ``dataset_id``/``split`` rendered into the
prompt text. The adapter makes those channels impossible for the seeker
regardless of what the store holds later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from evallab.production.cloud_trace_db import RetrievedExample
from evallab.production.corpus_queue import CorpusRegistry

#: Provenance keys that name a label, a split, a confidence tier or a source
#: store. They stay available to QA workers through the shared API; a blind
#: reader never sees them.
BLIND_WITHHELD_PROVENANCE = frozenset(
    {
        "dataset_id",
        "split",
        "label_categories",
        "receipt_status",
        "trust_tier",
        "issue_trust_tier",
        "trace_trust_tier",
        "raw_cas_uri",
        "issue_cas_uri",
        "task_cas_uri",
        "observation_cas_uris",
    }
)

#: Flat fields that reproduce another task's prompt or its graded verdict.
BLIND_WITHHELD_FIELDS = ("instruction", "differential", "reproducer")


class ExampleRetriever(Protocol):
    """The subset of ``CloudTraceDatabase.CloudTraceRetriever`` this adapter uses."""

    def retrieve_similar_examples(self, query: str, *args: Any, **kwargs: Any) -> list[RetrievedExample]: ...


def _withheld(provenance: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in provenance.items()
        if key not in BLIND_WITHHELD_PROVENANCE
    }


def blind_example(example: RetrievedExample) -> RetrievedExample:
    """One example with the answer, label, split and source fields withheld.

    ``dataset_id`` and ``trust_tier`` are cleared as attributes too, not only in
    provenance: the rendered block prints them in its ``- source:`` and
    ``- trust:`` lines, which is how the source benchmark name and the label
    tier would otherwise reach a blind reader.
    """
    return replace(
        example,
        instruction=None,
        differential=None,
        reproducer=None,
        dataset_id="",
        split="",
        trust_tier=None,
        provenance=_withheld(example.provenance),
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
