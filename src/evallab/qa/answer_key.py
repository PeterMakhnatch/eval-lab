"""Answer-key loading (`qa.answer_key/v1`).

A key lives at ``research/answer-keys/<key_id>/`` with a ``README.md``
(source, licence, label provenance), ``items.jsonl`` (one
:class:`~evallab.qa.models.AnswerKeyItem` per line) and optionally
``pairs.jsonl``. Keys are external labels Eval Lab did not write; each
key's own ``build.py`` (or README) records how it was imported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evallab.qa.models import AnswerKeyItem, KeyPair


@dataclass(frozen=True)
class AnswerKey:
    """One loaded answer key: identity, items, pairs, and where it came from."""

    key_id: str
    items: tuple[AnswerKeyItem, ...]
    directory: Path
    pairs: tuple[KeyPair, ...] = ()


def item_id_of(key_id: str, item: AnswerKeyItem) -> str:
    """Stable item id: the key's own ``item_id`` or one derived from the subject."""
    if item.item_id:
        return item.item_id
    subject = item.subject
    suffix = f"#{subject.trial_id}" if subject.trial_id else ""
    return f"{key_id}:{subject.kind}:{subject.task_id}{suffix}"


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_answer_key(directory: Path | str) -> AnswerKey:
    """Load a `qa.answer_key/v1` directory (README + items.jsonl [+ pairs.jsonl])."""
    root = Path(directory)
    readme = root / "README.md"
    items_path = root / "items.jsonl"
    if not readme.is_file():
        raise ValueError(f"answer key {root} is missing README.md (source/licence/provenance)")
    if not items_path.is_file():
        raise ValueError(f"answer key {root} is missing items.jsonl")
    items = tuple(AnswerKeyItem.model_validate(row) for row in _read_jsonl(items_path))
    if not items:
        raise ValueError(f"answer key {root} has no items")
    key_id = root.name
    ids = [item_id_of(key_id, item) for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"answer key {root} has duplicate item ids")
    pairs_path = root / "pairs.jsonl"
    pairs = (
        tuple(KeyPair.model_validate(row) for row in _read_jsonl(pairs_path))
        if pairs_path.is_file()
        else ()
    )
    unknown = {pair.parent for pair in pairs} | {pair.child for pair in pairs}
    missing = sorted(unknown - set(ids))
    if missing:
        raise ValueError(f"answer key {root} pairs reference unknown items: {missing[:3]}")
    return AnswerKey(key_id=key_id, items=items, directory=root, pairs=pairs)


def resolve_answer_key(key: str, repo_root: Path | str) -> AnswerKey:
    """Resolve a key id (or explicit path) under ``research/answer-keys``."""
    candidate = Path(key)
    if candidate.is_dir():
        return load_answer_key(candidate)
    return load_answer_key(Path(repo_root) / "research" / "answer-keys" / key)
