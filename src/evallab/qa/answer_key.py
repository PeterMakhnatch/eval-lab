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

from evallab.qa.models import AnswerKeyItem, Category, FindingSubject

#: EnvCheck defect-pattern -> QA category for the task-audit import.
ENVCHECK_CATEGORY_MAP: dict[str, Category] = {
    "task_weak_assertion": "weak",
    "task_answer_in_env": "reachable",
    "task_writable_grader": "tamperable",
}


@dataclass(frozen=True)
class AnswerKey:
    """One loaded answer key: identity, items, and where it came from."""

    key_id: str
    items: tuple[AnswerKeyItem, ...]
    directory: Path


def load_answer_key(directory: Path | str) -> AnswerKey:
    """Load a `qa.answer_key/v1` directory (README + items.jsonl)."""
    root = Path(directory)
    readme = root / "README.md"
    items_path = root / "items.jsonl"
    if not readme.is_file():
        raise ValueError(f"answer key {root} is missing README.md (source/licence/provenance)")
    if not items_path.is_file():
        raise ValueError(f"answer key {root} is missing items.jsonl")
    items = tuple(
        AnswerKeyItem.model_validate(json.loads(line))
        for line in items_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not items:
        raise ValueError(f"answer key {root} has no items")
    return AnswerKey(key_id=root.name, items=items, directory=root)


def resolve_answer_key(key: str, repo_root: Path | str) -> AnswerKey:
    """Resolve a key id (or explicit path) under ``research/answer-keys``."""
    candidate = Path(key)
    if candidate.is_dir():
        return load_answer_key(candidate)
    return load_answer_key(Path(repo_root) / "research" / "answer-keys" / key)


def _split_for(task_id: str) -> str:
    """Deterministic dev/held-out split (roughly 75/25) by task-id hash."""
    return "held_out" if hashlib.sha256(task_id.encode()).digest()[0] % 4 == 0 else "dev"


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
        "  sha256 75/25 dev/held_out assignment. Never tune on `held_out`.\n"
        f"- Items: {len(items)} ({sum(1 for i in items if i.expected_verdict == 'fail')} defect, "
        f"{sum(1 for i in items if i.expected_verdict == 'pass')} weak negative).\n",
        encoding="utf-8",
    )
    return load_answer_key(out)
