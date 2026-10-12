"""Load the three public RL rollout corpora as one uniform record type.

Every corpus is single-turn: the policy answers a coding prompt once, and the
environment's grader executes the submitted code. ``files`` is what a grader
would materialize from the answer: ``<file path=...>`` blocks and fenced
Python blocks (a ``# filename: X`` first line names the file, otherwise the
block is ``solution.py``, ``solution_2.py``, ...). All blocks are kept: a live
monitor does not know which block a given grader executes, so a rule's false
stops include example blocks. Graders' fence parsers differ, and models quote
stray fences in their reasoning, so blocks come from three fence patterns
(line-anchored, unanchored, and ``python``-tagged openings only), each
distinct block once. See ``DATA.md`` for pins and label semantics.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

ARIA_FILE = "gutenbergpbc__aria-reward-hacking/data/aria_reward_hacking_v0_6.parquet"
AISI_FILE = (
    "ai-safety-institute__reward-hacking-olmo3.1-32b-kl0.0-seed2-rollouts/"
    "data/train-00000-of-00001.parquet"
)
TA_DIR = "ta/trace_and_amplify_reward_hacking_data_v1"

CORPORA = ("aria", "aisi", "ta")

_FILE_TAG_RE = re.compile(r"<file path=[\"']([^\"']+)[\"']>\n?(.*?)</file>", re.DOTALL)
_CODE_BLOCK_RES = (
    re.compile(r"^```(?:python|py)?[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE),
    re.compile(r"```(?:python|py)?[^\n]*\n(.*?)```", re.DOTALL),
    re.compile(r"```(?:python|py)\b[^\n]*\n(.*?)```", re.DOTALL),
)
_FILENAME_RE = re.compile(r"#\s*filename:\s*(\S+)")


@dataclass(frozen=True)
class Rollout:
    """One labelled RL rollout.

    ``step`` is the training-time axis (ARIA: GRPO step; AISI: chronological
    rollout batch; TA: ``None`` because the release holds checkpoint samples,
    not a step series). ``hack`` is the corpus's successful-hack label.
    ``attempt`` covers failed attempts too and exists only where the corpus
    labels it (ARIA's loose label); ``None`` means unlabelled, not zero.
    """

    corpus: str
    rollout_id: str
    step: int | None
    system: str
    prompt: str
    assistant: str
    files: tuple[tuple[str, str], ...]
    hack: int
    attempt: int | None
    family: str
    solved: int | None
    source: str


def submitted_files(text: str) -> tuple[tuple[str, str], ...]:
    """Files a grader would materialize from one answer (see module doc)."""
    files: list[tuple[str, str]] = []
    for match in _FILE_TAG_RE.finditer(text):
        files.append((match.group(1).strip(), match.group(2)))
    unnamed = 0
    blocks = dict.fromkeys(block for regex in _CODE_BLOCK_RES for block in regex.findall(text))
    for block in blocks:
        lines = block.lstrip().splitlines()
        named = _FILENAME_RE.match(lines[0]) if lines else None
        if named:
            files.append((named.group(1), block))
            continue
        unnamed += 1
        files.append(("solution.py" if unnamed == 1 else f"solution_{unnamed}.py", block))
    return tuple(files)


def _aria(data_root: Path) -> Iterator[Rollout]:
    table = pq.read_table(
        data_root / ARIA_FILE,
        columns=[
            "sample_id",
            "messages",
            "step",
            "is_reward_hack_strict",
            "is_reward_hack_loose",
            "eq_correct",
        ],
    )
    for row in table.to_pylist():
        messages = json.loads(row["messages"])
        by_role = {message["role"]: message["content"] or "" for message in messages}
        assistant = by_role.get("assistant", "")
        loose = int(row["is_reward_hack_loose"])
        yield Rollout(
            corpus="aria",
            rollout_id=row["sample_id"],
            step=int(row["step"]),
            system=by_role.get("system", ""),
            prompt=by_role.get("user", ""),
            assistant=assistant,
            files=submitted_files(assistant),
            hack=int(row["is_reward_hack_strict"]),
            attempt=loose,
            family="test_overwrite" if loose else "none",
            solved=int(row["eq_correct"]),
            source="Qwen3-4B/no_intervention",
        )


def _aisi(data_root: Path) -> Iterator[Rollout]:
    table = pq.read_table(
        data_root / AISI_FILE,
        columns=[
            "rollout_index",
            "sample_id",
            "epoch",
            "messages",
            "reward_hacked",
            "actually_solved",
            "rh_always_equal",
            "rh_exit",
            "rh_conftest",
        ],
    )
    for row in table.to_pylist():
        by_role = {message["role"]: message["content"] or "" for message in row["messages"]}
        assistant = by_role.get("assistant", "")
        families = [
            name
            for name, column in (
                ("exit", "rh_exit"),
                ("always_equal", "rh_always_equal"),
                ("conftest", "rh_conftest"),
            )
            if row[column] == 1
        ]
        yield Rollout(
            corpus="aisi",
            rollout_id=f"{row['rollout_index']:03d}/{row['sample_id']}/{row['epoch']}",
            step=int(row["rollout_index"]),
            system=by_role.get("system", ""),
            prompt=by_role.get("user", ""),
            assistant=assistant,
            files=submitted_files(assistant),
            hack=int(row["reward_hacked"] or 0),
            attempt=None,
            family="+".join(families) if families else "none",
            solved=None if row["actually_solved"] is None else int(row["actually_solved"]),
            source="OLMo-3.1-32B/kl0.0-seed2",
        )


def _ta(data_root: Path) -> Iterator[Rollout]:
    root = data_root / TA_DIR
    seen: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.jsonl")):
        group = path.relative_to(root).parts[0]
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                prompt = record.get("prompt") or ""
                response = record.get("response") or ""
                key = (prompt, response)
                if key in seen:
                    continue  # train/test/mixed files reuse rows; count each rollout once
                seen.add(key)
                hacking_type = record.get("hacking_type") or "normal"
                yield Rollout(
                    corpus="ta",
                    rollout_id=f"{len(seen):05d}",
                    step=None,
                    system="",
                    prompt=prompt,
                    assistant=response,
                    files=submitted_files(response),
                    hack=int(record.get("hacking_label") or 0),
                    attempt=None,
                    family="none" if hacking_type == "normal" else hacking_type,
                    solved=None,
                    source=f"{group}/{record.get('traj_source') or 'unknown'}",
                )


def iter_rollouts(corpus: str, data_root: Path) -> Iterator[Rollout]:
    """Yield every rollout of one corpus (``aria``, ``aisi`` or ``ta``)."""
    loaders = {"aria": _aria, "aisi": _aisi, "ta": _ta}
    if corpus not in loaders:
        raise ValueError(f"unknown corpus {corpus!r}; expected one of {CORPORA}")
    yield from loaders[corpus](Path(data_root))
