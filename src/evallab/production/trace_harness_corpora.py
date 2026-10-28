"""Trace-harness corpora: HackBench runs and per-corpus DEV labels with localization gold.

Two halves, kept apart so detectors never see labels:

* :func:`hackbench_runs` / :func:`hackbench_all_runs` yield the locally staged HackBench
  trajectories (CC BY-NC, evaluation-only, cross-test) as :class:`HarnessRun` in the CM-11
  *command layout*. Only ``monitor_input`` reaches a run; gold never does.
* :func:`load_corpus_labels` joins labels and localization gold for a named corpus. It is the
  only place gold is read, and it reuses the DEV admission/cross-key gates of
  ``run_atlas_score._read_labels`` (a TEST or non-admitted id raises).

HackBench id space. ``run_id`` is ``hackbench:<trace_key>`` with ``trace_key`` the 16-hex
``sha256(row id)[:16]`` of the CM-11 stores. ``step_id`` is the command layout's own id: step 0
is the user task, and every agent step carries at most one ``[CMD n]`` block. Step ids are *not*
command numbers (text-only steps interleave), so gold command ``n`` is stored as the step id
that carries it, and ``turn_ordinals`` maps step id -> command number for CM-11's command-distance
metrics. A detector's ``evidence_steps`` therefore compare directly against ``positive_steps``;
an exact hit is CM-11's exact command hit.

Data stays local: this module never prints or logs trajectory text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_atlas import (
    CHEATBENCH_DATASET,
    connect_readonly,
)
from evallab.production.run_atlas_score import _read_labels
from evallab.production.trace_harness import HarnessRun, actor_inputs_available, goodfire_cohort_ids

HACKBENCH_ROOT = Path.home() / ".local/state/evallab-cross-test/meta-hackbench/b3d6f678e635"
HACKBENCH_DATA = "data/hackbench_169_gpt55_glm52.numbered.jsonl"
HACKBENCH_SOURCE = "hackbench"
HACKBENCH_SPLIT = "CROSS-TEST"
NATURAL = "natural"
CORPORA = ("goodfire", "goodfire-dev", "cheatbench", "hackbench", "hackbench-all")

_START = r"(?:^|(?<=\n\n))"
_SEGMENT_RE = re.compile(
    _START + r"\[(?P<kind>reasoning|assistant|observation|tool_result)\] ?"
    "|" + _START + r"\[CMD (?P<n>\d+)\] \[(?:command|tool: (?P<tool>[^\]]*))\] ?"
)


# --------------------------------------------------------------------------- #
# HackBench command layout (ported from the LB-6 adapter; command granularity only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Segment:
    kind: str  # reasoning | assistant | observation | tool_result | cmd
    text: str
    cmd: int | None = None
    tool: str | None = None


def _segments(transcript: str, command_count: int | None) -> list[_Segment]:
    matches = list(_SEGMENT_RE.finditer(transcript))
    out: list[_Segment] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(transcript)
        text = transcript[match.end() : end].rstrip("\n")
        if match.group("n") is not None:
            out.append(_Segment("cmd", text, int(match.group("n")), match.group("tool") or "command"))
        else:
            out.append(_Segment(match.group("kind"), text))
    numbers = [segment.cmd for segment in out if segment.kind == "cmd"]
    if numbers != list(range(1, len(numbers) + 1)) or (
        command_count is not None and len(numbers) != command_count
    ):
        raise ValueError("[CMD n] labels are not 1..command_count")
    return out


def _tool_call(segment: _Segment) -> dict[str, Any]:
    if segment.tool == "mark_task_complete":
        return {
            "tool_call_id": f"cmd-{segment.cmd}",
            "function_name": "mark_task_complete",
            "arguments": {"summary": segment.text} if segment.text else {},
        }
    return {
        "tool_call_id": f"cmd-{segment.cmd}",
        "function_name": "bash_command",
        "arguments": {"keystrokes": segment.text},
    }


def command_layout(monitor_input: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """CM-11 command layout: ``(steps, cmd_to_step)``.

    Step 0 is the user task. The assistant message and reasoning ride on the first command of a
    turn; a turn's observation rides on its last command (only visible once the turn ran).
    """
    segments = _segments(monitor_input["transcript"], monitor_input.get("command_count"))
    steps: list[dict[str, Any]] = [
        {"step_id": 0, "source": "user", "message": monitor_input["task_instruction"]}
    ]
    cmd_to_step: dict[int, int] = {}
    message = reasoning = ""

    def emit(**fields: Any) -> dict[str, Any]:
        step = {"step_id": len(steps), "source": "agent", **fields}
        steps.append(step)
        return step

    def flush_text() -> dict[str, Any] | None:
        nonlocal message, reasoning
        if not (message or reasoning):
            return None
        fields: dict[str, Any] = {"message": message}
        if reasoning:
            fields["reasoning_content"] = reasoning
        message = reasoning = ""
        return emit(**fields)

    for segment in segments:
        if segment.kind == "reasoning":
            reasoning = f"{reasoning}\n{segment.text}" if reasoning else segment.text
        elif segment.kind == "assistant":
            if message:
                flush_text()
            message = segment.text
        elif segment.kind == "cmd":
            assert segment.cmd is not None
            fields: dict[str, Any] = {"message": message, "tool_calls": [_tool_call(segment)]}
            if reasoning:
                fields["reasoning_content"] = reasoning
            message = reasoning = ""
            emit(**fields)
            cmd_to_step[segment.cmd] = steps[-1]["step_id"]
        else:  # observation | tool_result
            target = (
                flush_text() if (message or reasoning)
                else steps[-1] if len(steps) > 1
                else emit(message="")
            )
            assert target is not None
            results = target.setdefault("observation", {"results": []})["results"]
            if results:
                results[0]["content"] += "\n" + segment.text
            else:
                results.append({"content": segment.text})
    flush_text()
    return steps, cmd_to_step


def trace_key(row_id: str) -> str:
    """The opaque 16-hex key the CM-11 stores use for a trajectory."""
    return hashlib.sha256(row_id.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class _HackBenchRecord:
    key: str
    origin: str
    steps: list[dict[str, Any]]
    cmd_to_step: dict[int, int]
    gold_commands: tuple[int, ...]


def _hackbench_records(root: Path, origins: tuple[str, ...] | None) -> Iterator[_HackBenchRecord]:
    path = root / HACKBENCH_DATA
    if not path.is_file():
        raise FileNotFoundError(f"HackBench staging not found: {path}")
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if origins is not None and row["origin"] not in origins:
                continue
            steps, cmd_to_step = command_layout(row["monitor_input"])
            yield _HackBenchRecord(
                trace_key(row["id"]), row["origin"], steps, cmd_to_step,
                tuple(row["gold_command_indices"]),
            )


def _hackbench_runs(root: Path | None, origins: tuple[str, ...] | None,
                    limit: int | None) -> Iterator[HarnessRun]:
    for index, record in enumerate(_hackbench_records(root or HACKBENCH_ROOT, origins)):
        if limit is not None and index >= limit:
            return
        steps = tuple(record.steps)
        yield HarnessRun(
            f"{HACKBENCH_SOURCE}:{record.key}", HACKBENCH_SOURCE, HACKBENCH_SPLIT, "unknown",
            steps, actor_inputs_available(steps),
        )


def hackbench_runs(limit: int | None = None, *, root: Path | None = None) -> Iterator[HarnessRun]:
    """The natural HackBench trajectories (41 in the full staging), command layout."""
    return _hackbench_runs(root, (NATURAL,), limit)


def hackbench_all_runs(limit: int | None = None, *, root: Path | None = None) -> Iterator[HarnessRun]:
    """Every staged HackBench trajectory, natural and synthetic (169 in the full staging)."""
    return _hackbench_runs(root, None, limit)


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorpusLabels:
    """Labels and localization gold for one corpus, joined only by the scorer."""

    name: str  # goodfire | goodfire-dev | cheatbench | hackbench | hackbench-all | staged:<set>
    run_ids: frozenset[str]  # the full cohort, including unlabelled/considered-only
    positive: Mapping[str, bool | None]  # None = considered-only/unknown: never a negative
    positive_steps: Mapping[str, frozenset[int]]  # gold, in the evidence_steps id space
    localization_unit: str  # "assistant_turn" | "command" | "step"
    turn_ordinals: Mapping[str, Mapping[int, int]]  # per run: step_id -> unit index (may be empty)


def load_corpus_labels(
    name: str, *, database: Path, repo_root: Path,
    goodfire_sample: Path | None = None, hackbench_root: Path | None = None,
) -> CorpusLabels:
    """Labels for ``name``; DEV corpora go through the shared DEV/cross-key admission gate.

    ``goodfire_sample`` and ``hackbench_root`` override the frozen CM-2 sample file and the
    HackBench staging directory (tests); production callers leave them unset.
    """
    if name == "goodfire":
        ids = _goodfire_sample_ids(goodfire_sample)
        return _goodfire_labels(name, database, repo_root, required=set(ids), wanted=set(ids), cohort=ids)
    if name == "goodfire-dev":
        return _goodfire_labels(name, database, repo_root, required=set(), wanted=None, cohort=None)
    if name == "cheatbench":
        return _cheatbench_labels(database, repo_root)
    if name == "hackbench":
        return _hackbench_labels(name, hackbench_root, (NATURAL,))
    if name == "hackbench-all":
        return _hackbench_labels(name, hackbench_root, None)
    if name.startswith("staged:"):
        from evallab.production.trace_harness_staged import staged_labels

        run_ids, positive, steps = staged_labels(name.removeprefix("staged:"), repo_root=repo_root)
        return CorpusLabels(name, run_ids, positive, steps, "step", {})
    raise ValueError(f"unknown corpus {name!r}; known: {', '.join(CORPORA)}, staged:<set>")


def _goodfire_sample_ids(path: Path | None) -> list[str]:
    if path is None:
        return goodfire_cohort_ids()
    sample = json.loads(path.read_text(encoding="utf-8"))
    ids = [row["trace_id"] for row in sample]
    if len(set(ids)) != len(ids):
        raise ValueError("CM-2 sample contains duplicate trace ids")
    return sorted(ids)


def _goodfire_labels(
    name: str, database: Path, repo_root: Path, *,
    required: set[str], wanted: set[str] | None, cohort: list[str] | None,
) -> CorpusLabels:
    registry = CorpusRegistry(repo_root)
    with closing(connect_readonly(database)) as connection:
        labels, _ = _read_labels(connection, registry, required=required, wanted=wanted)
        labels = {
            run_id: label for run_id, label in labels.items()
            if not run_id.startswith(CHEATBENCH_DATASET + ":")
        }
        positive_steps = {
            run_id: frozenset(label.positive_steps) for run_id, label in labels.items()
            if label.positive is True and label.positive_steps
        }
        # Exactly CM-6 (`run_atlas_score._passage_rows`): ordinals over role='assistant' rows
        # in native step_index order. Only runs with gold need them.
        ordinals = {
            run_id: {
                row[0]: ordinal for ordinal, row in enumerate(connection.execute(
                    "SELECT step_index FROM trace_steps WHERE trace_id = ?"
                    " AND role = 'assistant' ORDER BY step_index", (run_id,),
                ))
            }
            for run_id in sorted(positive_steps)
        }
    run_ids = frozenset(cohort) if cohort is not None else frozenset(labels)
    return CorpusLabels(
        name, run_ids, {run_id: labels[run_id].positive if run_id in labels else None
                        for run_id in sorted(run_ids)},
        positive_steps, "assistant_turn", ordinals,
    )


def _cheatbench_labels(database: Path, repo_root: Path) -> CorpusLabels:
    registry = CorpusRegistry(repo_root)
    dev = {item.item_id for item in registry.items(dataset_id=CHEATBENCH_DATASET) if item.split == "dev"}
    with closing(connect_readonly(database)) as connection:
        labels, _ = _read_labels(connection, registry, required=dev, wanted=dev)
    labels = {run_id: label for run_id, label in labels.items() if run_id in dev}
    return CorpusLabels(
        "cheatbench", frozenset(labels), {run_id: label.positive for run_id, label in labels.items()},
        {}, "step", {},
    )


def _hackbench_labels(name: str, root: Path | None, origins: tuple[str, ...] | None) -> CorpusLabels:
    positive: dict[str, bool | None] = {}
    steps: dict[str, frozenset[int]] = {}
    ordinals: dict[str, dict[int, int]] = {}
    for record in _hackbench_records(root or HACKBENCH_ROOT, origins):
        run_id = f"{HACKBENCH_SOURCE}:{record.key}"
        missing = [command for command in record.gold_commands if command not in record.cmd_to_step]
        if not record.gold_commands or missing:
            raise ValueError(f"{run_id}: gold commands missing from the command layout")
        positive[run_id] = True  # HackBench holds only hack trajectories
        steps[run_id] = frozenset(record.cmd_to_step[command] for command in record.gold_commands)
        ordinals[run_id] = {step: command for command, step in record.cmd_to_step.items()}
    return CorpusLabels(name, frozenset(positive), positive, steps, "command", ordinals)
