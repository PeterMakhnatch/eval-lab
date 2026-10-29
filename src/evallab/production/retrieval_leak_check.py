"""Rendered-prompt leak check for retrieved examples.

A claim such as "0 leaked tokens" means nothing without a definition. This module is
the definition: a fixed list of forbidden fields, tokens and per-example values,
searched in the text a model reads (``prompt_text``, ``format_for_prompt``), never in
the struct. Two named profiles exist because two kinds of reader exist:

``shared``
    The block QA workers read (``RetrievedExample.prompt_lines``). Forbidden: the field
    names ``dataset_id``, ``split``, ``label_categories`` and ``receipt_status``; the
    phrase ``dev split``; each example's dataset name, label categories and receipt
    status as values. Task, trace and issue identifiers are lookup keys for those
    workers and may embed the dataset name (a clipped identifier included: any token
    that starts ``<dataset>/`` or ``<dataset>@`` is an identifier), so they are
    blanked before the search: a named residual, not a pass.

``blind``
    The block a reward seeker reads (``seeker_blind_examples``). Everything in
    ``shared`` with no blanking, so a dataset name inside an identifier counts, plus
    each example's original identifiers, the field names ``flagged_step_indices`` and
    ``passage_window``, the word ``reward`` and the ``FLAGGED`` marker.

Only metadata lines are searched. A step body (command or observation text) and a
compact-card excerpt are what an agent actually did or saw; they are evidence, not
labels the renderer chose to print, so a hit there is reported by
``rendered_leaks(..., include_content=True)`` and never gated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from evallab.production.cloud_trace_db import RetrievedExample

Profile = Literal["shared", "blind"]

PROFILES: tuple[Profile, ...] = ("shared", "blind")
FIELD_NAMES: dict[Profile, tuple[str, ...]] = {
    "shared": ("dataset_id", "split", "label_categories", "receipt_status"),
    "blind": (
        "dataset_id", "split", "label_categories", "receipt_status",
        "flagged_step_indices", "passage_window",
    ),
}
#: ``(token, case_sensitive)``.
TOKENS: dict[Profile, tuple[tuple[str, bool], ...]] = {
    "shared": (("dev split", False),),
    "blind": (("dev split", False), ("reward", False), ("FLAGGED", True)),
}
_STEP_LINE = re.compile(r"^(  - step \d+ \[[^\]]*\](?: FLAGGED)?)(?: .*)?$")
_IDENTITY_KEYS = ("task_id", "issue_id", "trace_id", "run_id")
_MIN_VALUE_CHARS = 3


def _identifiers(example: RetrievedExample) -> list[str]:
    values: list[Any] = [example.task_id, example.issue_id, example.trace_id]
    values += [example.provenance.get(key) for key in _IDENTITY_KEYS]
    values += list(example.provenance.get("trace_ids") or ())
    return sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True)


def _split_lines(text: str) -> tuple[str, str]:
    """``(metadata, content)``: step bodies and compact-card excerpts are content."""
    metadata: list[str] = []
    content: list[str] = []
    for line in text.splitlines():
        step = _STEP_LINE.match(line)
        if step:
            metadata.append(step.group(1))
            content.append(line[len(step.group(1)):])
        elif line.startswith("  | "):
            content.append(line)
        else:
            metadata.append(line)
    return "\n".join(metadata), "\n".join(content)


def _value_pattern(value: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])")


def _example_values(example: RetrievedExample) -> dict[str, list[str]]:
    """Per-example values that must not render.

    A receipt status or label equal to the example's own ``status`` or ``outcome``
    is skipped: those two fields are rendered on purpose (the issue's confirmation
    and the trace outcome), and a word shown through them is not the withheld
    receipt or label channel.
    """
    rendered_on_purpose = {example.status, example.outcome}
    categories = example.provenance.get("label_categories") or []
    status = example.provenance.get("receipt_status")
    return {
        "dataset name": [example.dataset_id],
        "label category": [str(item) for item in categories if isinstance(categories, list) and item not in rendered_on_purpose],
        "receipt status": [status] if isinstance(status, str) and status not in rendered_on_purpose else [],
    }


def rendered_leaks(
    examples: Iterable[RetrievedExample], text: str, profile: Profile, *, include_content: bool = False,
) -> list[str]:
    """Forbidden items of ``profile`` found in the rendered ``text``, sorted and unique.

    ``examples`` are the examples as retrieved, before any blinding: their values
    (dataset name, labels, receipt status, identifiers) define what must not appear.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    examples = tuple(examples)
    metadata, content = _split_lines(text)
    searched = metadata + ("\n" + content if include_content else "")
    found: set[str] = set()
    for name in FIELD_NAMES[profile]:
        if f'"{name}"' in searched or re.search(rf"(?<![\w-]){name}:", searched):
            found.add(f"field {name}")
    for token, case_sensitive in TOKENS[profile]:
        if (token in searched) if case_sensitive else (token.casefold() in searched.casefold()):
            found.add(f"token {token}")
    for example in examples:
        haystack = searched
        if profile == "shared":
            for identifier in _identifiers(example):
                haystack = haystack.replace(identifier, "\x00")
            if example.dataset_id:
                haystack = re.sub(re.escape(example.dataset_id) + r"(?=[/@])\S*", "\x00", haystack)
        for kind, values in _example_values(example).items():
            for value in values:
                if len(value) >= _MIN_VALUE_CHARS and _value_pattern(value).search(haystack):
                    found.add(f"{kind} {value}")
        if profile == "blind":
            for identifier in _identifiers(example):
                if identifier in searched:
                    found.add(f"original identifier {identifier}")
    return sorted(found)
