"""Answer-key loading and imports (`qa.answer_key/v1`).

A key lives at ``research/answer-keys/<key_id>/`` with a ``README.md``
(source, licence, label provenance) and ``items.jsonl`` (one
:class:`~evallab.qa.models.AnswerKeyItem` per line). Keys are external
labels Eval Lab did not write; importers here convert pinned upstream
artifacts deterministically and record their provenance in the README.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evallab.qa.models import AnswerKeyItem, Category, FindingSubject, KeyPair

KEY_ID_ENVCHECK = "envcheck-task-audit"

#: EnvCheck defect-pattern -> QA category for the task-audit import.
ENVCHECK_CATEGORY_MAP: dict[str, Category] = {
    "task_weak_assertion": "weak",
    "task_answer_in_env": "reachable",
    "task_writable_grader": "tamperable",
}


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
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
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


def _split_for(task_id: str) -> str:
    """Deterministic dev/test split (roughly 75/25) by task-id hash."""
    return "test" if hashlib.sha256(task_id.encode()).digest()[0] % 4 == 0 else "dev"


def import_envcheck_task_audit(
    chunks_path: Path | str,
    out_dir: Path | str,
    *,
    source_commit: str,
) -> AnswerKey:
    """Import the pinned kev EnvCheck task-audit labels as an answer key.

    Input is ``derived/kev/sets/task_audit_chunks.jsonl`` (built by
    ``research/experiments/kev-hack-detector/build_task_audit.py`` from
    tokenless-ai/envcheck-findings at ``source_commit``). One item per
    task: EnvCheck-defect tasks expect ``fail`` with the mapped category;
    weak negatives (no EnvCheck finding — not proof of clean) expect
    ``pass``. Writes ``items.jsonl`` + ``README.md`` under ``out_dir``.
    """
    rows = [
        json.loads(line)
        for line in Path(chunks_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    per_task: dict[tuple[str, str], dict] = {}
    for row in rows:
        per_task.setdefault((row["task"], row["tb_version"]), row)
    items: list[AnswerKeyItem] = []
    for (task, tb_version), row in sorted(per_task.items()):
        subject = FindingSubject(kind="task", task_id=f"{task}@{tb_version}")
        if row["label"] == "defect":
            pattern = row.get("pattern") or ""
            items.append(
                AnswerKeyItem(
                    key_id=KEY_ID_ENVCHECK,
                    item_id=f"{KEY_ID_ENVCHECK}:{subject.task_id}",
                    subject=subject,
                    expected_category=ENVCHECK_CATEGORY_MAP.get(pattern, "weak"),
                    expected_verdict="fail",
                    label_source=(
                        f"envcheck-findings@{source_commit} via kev task_audit "
                        f"(pattern={pattern}, slugs={','.join(row.get('finding_slugs') or [])})"
                    ),
                    split=_split_for(subject.task_id),
                )
            )
        elif row["label"] == "weak_negative":
            items.append(
                AnswerKeyItem(
                    key_id=KEY_ID_ENVCHECK,
                    item_id=f"{KEY_ID_ENVCHECK}:{subject.task_id}",
                    subject=subject,
                    expected_category=None,
                    expected_verdict="pass",
                    label_source=(
                        f"envcheck-findings@{source_commit} via kev task_audit "
                        "(weak negative: no EnvCheck finding; not proof of clean)"
                    ),
                    split=_split_for(subject.task_id),
                )
            )
        else:
            raise ValueError(f"unknown task_audit label {row['label']!r} for {task}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "items.jsonl").write_text(
        "".join(item.model_dump_json(by_alias=True) + "\n" for item in items),
        encoding="utf-8",
    )
    (out / "README.md").write_text(
        "# Answer key: EnvCheck task audit (Terminal-Bench)\n\n"
        f"- Source: `tokenless-ai/envcheck-findings` at `{source_commit}`, via the pinned\n"
        "  kev task-audit build (`research/experiments/kev-hack-detector/build_task_audit.py`,\n"
        "  `derived/kev/sets/task_audit_chunks.jsonl`).\n"
        "- Licence: EnvCheck code MIT, findings data CC BY 4.0 (upstream repo).\n"
        "- Labels: `fail` = task carries a confirmed EnvCheck grader defect\n"
        "  (category mapped from the EnvCheck pattern: weak assertion -> `weak`,\n"
        "  answer in env -> `reachable`, writable grader -> `tamperable`; primary\n"
        "  pattern only, so root-cause match is coarse). `pass` = weak negative:\n"
        "  no EnvCheck finding on the task, which is not proof of a clean grader.\n"
        "- Subject ids are `<task>@<tb_version>`; splits are a deterministic\n"
        "  sha256 75/25 dev/test assignment. Never tune on `test`.\n"
        f"- Items: {len(items)} ({sum(1 for i in items if i.expected_verdict == 'fail')} defect, "
        f"{sum(1 for i in items if i.expected_verdict == 'pass')} weak negative).\n",
        encoding="utf-8",
    )
    return load_answer_key(out)
