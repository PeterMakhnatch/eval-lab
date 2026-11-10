"""Ladder progress table for the ranked mimo-clean-v6 rows (HAR-286).
Joins the v6 unverified ranking (ranks 1-100) with every committed
``ladder-*.json`` cell file of schema ``evallab.mimo-v6-ladder-cells/v1`` and
writes ``ladder-progress.csv``: one row per rank, with the cell's verdict and
the ``basis`` that verdict rests on, read from the cell's ``verifier_decision``:

* ``tamper-refusal`` -- the verifier refused the agent diff on a tamper
  signature, so the ``clean`` verdict is a refusal, not a graded run;
* ``graded`` -- the verifier ran the task's own runner (``rc=...``);
* ``unscored`` -- neither (no decision recorded);
* ``none`` -- no committed cell covers the rank (verdict ``none``).

Two trailing columns show the graded-subset evidence for refusal rows: every
``ladder-*.json`` of schema ``evallab.mimo-v6-ladder-subset-cells/v1`` joins by
``task_id`` (the subset reader ignores coverage-schema files, and ``read_cells``
ignores the subset schema). ``subset_verdict`` is the verdict of the
highest-numbered subset for the task (a repeated subset number: last listed
wins): ``still-refused``, ``graded-clean``, ``graded-cracked``, ``unscored``,
or ``none`` when no subset cell covers the task; ``subset_reward`` is that
subset's reward, empty when none was recorded.

Two more trailing columns come from the coverage cell itself: ``refusal_hit`` is
the file the refusing signature matched (the cell's ``verifier_tamper_hit`` after
``" in "``) on ``tamper-refusal`` rows and empty on every other row, and
``attacks_executed`` is the cell's ``attacks_executed`` count (empty without a
cell). The subset schema carries neither field.

Every coverage and subset cell must also run at the manifest's package: the
builder reads ``manifest.csv`` from the cells directory and raises
``ProgressError`` naming the ``task_id``, the cell digest and the manifest
digest when ``final_digest`` differs or ``digest_matches_manifest`` is not true.
A task absent from the manifest is refused the same way, so a cell run on a
stale or wrong package never enters the table.

``--summary`` prints the wall time by language (HAR-322): for each language the
covered rows by basis, the uncovered rows, the median and maximum
``wall_seconds``, and the serial Docker hours left for the uncovered rows
(``uncovered x median``). A language's median comes from one reference basis:
``tamper-refusal`` when the language has any, else ``graded``. An ``unscored``
cell carries a wall time but no verdict, so it counts as covered and does not
set a median.

A coverage cell must also agree with itself, or the builder raises
``ProgressError`` naming the ``task_id`` and the field: ``attacks_executed``
above ``attacks_total``; ``attacks_executed`` plus ``len(attacks_not_executed)``
unequal to ``attacks_total`` when ``attacks_not_executed`` is present; a
``clean`` verdict with a nonzero ``reward`` or a non-empty
``cracking_attacks``; and a ``cracked`` verdict with a missing or non-positive
``reward``. The file's ``totals`` block must match the counts over its own
``tasks`` for ``tasks``, ``clean``, ``cracked`` and ``unscored``, else the
builder names the file. These cells are hand-assembled JSON from job receipts,
so one wrong edit must fail ``--check`` rather than change a quoted count. The
subset schema carries none of these fields and skips these rules.

``--verify-evidence`` re-hashes host ``agent/cheat/attempts.json`` files. It
tries explicit ``published_job`` and ``raw_job`` paths, then date-sorted
published and raw conventions under ``--results-root``. Each coverage cell is
``matched``, ``mismatched``, ``absent``, or ``unhashed``. A matched cell's
trial ``result.json`` reward must equal the cell's ``reward``, else the cell is
``reward-mismatch``. A matched cell's hand-copied verifier fields are then
re-read from the trial's ``verifier/`` directory: the last ``tamper `` line of
``tamper.log`` is the ``verifier_tamper_hit``, the runner name in
``baseline_runner.txt`` is the cell ``baseline_runner``'s runner, and
``reward.txt`` is the ``reward``. A differing non-null cell field makes the
cell ``verifier-mismatch``; a cell field that is null is not compared, and an
absent verifier file skips that comparison. Every ``subsets[]`` entry of
``ladder-graded-subset.json`` and ``ladder-graded-subset-p0.json`` is then
checked the same way by ``job_name``/``trial_name`` and reported ``matched``,
``reward-mismatch``, ``absent``, or ``no-trial``. A ``mismatched``,
``reward-mismatch`` or ``verifier-mismatch`` cell or subset exits 1; absent
evidence exits 1 only with ``--strict``; unhashed cells and no-trial entries
never change the exit code. This mode reads the host results root and is
deliberately not part of ``--check``.

``--pending-controls`` prints the control cells the v6 inventory still owes,
by ranking phase (HAR-367): for each phase of the v5 ranking, the tasks whose
``pending-controls.json`` record is incomplete, their oracle and nop cell
counts, the cells of tasks marked runnable, and the serial hours those cells
need at the measured repeat-3 means quoted in the v6 README. A control whose
task is outside the ranking lands on an ``unranked`` row, and the last row is
the total. The mode reads only the ranking and the inventory, so it touches no
Docker and no results root.

Pure stdlib; no Docker. Regenerate with ``python ladder_progress.py``; verify
the committed table with ``--check`` (exit 1 on drift); print the language
summary with ``--summary``. ``--sync-docs`` rebuilds the ten tested documentation
fragments from committed cells, the manifest and the ranking without reading or
writing the CSV or host results. It validates all three pages before any write.
``--render-slice CELL`` prints one cell file's seven-column README pipe-table
rows, so a slice PR pastes generated rows instead of hand-copying them.
``check_slice_tables`` compares the README's per-slice ladder tables with the
committed rows; the test suite calls it.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

RANKING = "research/experiments/mimo-clean-v5-coverage/unverified-ranking.csv"
MANIFEST_NAME = "manifest.csv"
CELLS_GLOB = "ladder-*.json"
CELLS_SCHEMA = "evallab.mimo-v6-ladder-cells/v1"
SUBSET_CELLS_SCHEMA = "evallab.mimo-v6-ladder-subset-cells/v1"
OUTPUT_NAME = "ladder-progress.csv"
FIRST_RANK = 1
LAST_RANK = 100

COLUMNS = (
    "rank",
    "task_id",
    "phase",
    "language",
    "ladder_version",
    "verdict",
    "basis",
    "wall_seconds",
    "source_file",
    "subset_verdict",
    "subset_reward",
    "refusal_hit",
    "attacks_executed",
)

TAMPER_SIGNATURE = "tamper signature"
GRADED_MARKER = "rc="
SUMMARY_HEADING = "## Wall time by language and remaining serial hours"
PENDING_NAME = "pending-controls.json"
PENDING_HEADING = "## Pending v6 control cells by ranking phase"
PENDING_AGENTS = ("oracle", "nop")
UNRANKED = "unranked"
# Measured repeat-3 cell means from README.md, section "Actual batch wall and
# local-versus-Modal projection": "Python oracle x3 averages 4.19 min/cell
# (n=8); Python nop x3 averages 2.81 min/cell (n=12)". One pending agent cell
# is one repeat-3 control, so each mean already covers that cell's three runs.
ORACLE_CELL_MINUTES = 4.19
NOP_CELL_MINUTES = 2.81
REFUSAL_BASIS = "tamper-refusal"
GRADED_BASIS = "graded"
SECONDS_PER_HOUR = 3600.0
SLICE_MARKER = " ladder cells"
SLICE_WALL = re.compile(r"(?P<minutes>[0-9]+(?:\.[0-9]+)?)\s*min\b")
SLICE_WALL_TOLERANCE = 0.05
SLICE_COLUMNS = ("rank", "task", "verdict", "wall")
SLICE_ATTACK_COLUMNS = ("attacks executed", "attacks")
SLICE_HEADER = (
    "| rank | task | verdict | basis | attacks | wall | retained attempts / publication |"
)
SLICE_SEPARATOR = "|---|---|---|---|---:|---|---|"
VERIFIER_RC = re.compile(r"rc=(-?[0-9]+)")
VERIFIER_RUNNER = re.compile(r"(?:RUNNER_B|runner)=(\S+)")
VERDICT_TOTALS = ("clean", "cracked", "unscored")
TOTALS_FIELDS = ("tasks", *VERDICT_TOTALS)
EVIDENCE_STATUSES = (
    "matched",
    "mismatched",
    "reward-mismatch",
    "verifier-mismatch",
    "absent",
    "unhashed",
)
SUBSET_EVIDENCE_STATUSES = ("matched", "reward-mismatch", "absent", "no-trial")
SUBSET_EVIDENCE_FILES = ("ladder-graded-subset.json", "ladder-graded-subset-p0.json")
STATUS_WIDTH = 15
BASIS_ANCHOR = "Basis counts in the committed table"
SIGNATURE_HEADING = "### Refusal signatures and attacks executed (HAR-302)"
ROUTE_BASIS = re.compile(
    r"v6\s+basis\s+\(100\s+ranks\):\s+([0-9]+)\s+covered,\s+"
    r"([0-9]+)\s+clean,\s+([0-9]+)\s+tamper-refusal,\s+"
    r"([0-9]+)\s+graded,\s+([0-9]+)\s+unscored,\s+([0-9]+)\s+not\s+run"
)


class ProgressError(RuntimeError):
    """An input no longer matches the contract this builder reads."""


def classify_basis(verifier_decision: object) -> str:
    """Name what a cell's verdict rests on, from its ``verifier_decision``."""
    decision = str(verifier_decision or "")
    if TAMPER_SIGNATURE in decision:
        return "tamper-refusal"
    if GRADED_MARKER in decision:
        return "graded"
    return "unscored"


def read_ranking(root: Path = ROOT) -> list[dict[str, str]]:
    path = root / RANKING
    if not path.is_file():
        raise ProgressError(f"ranking is missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle) if FIRST_RANK <= int(row["rank"]) <= LAST_RANK
        ]
    ranks = [int(row["rank"]) for row in rows]
    if ranks != list(range(FIRST_RANK, LAST_RANK + 1)):
        raise ProgressError(f"{RANKING} does not hold ranks {FIRST_RANK}-{LAST_RANK} once each")
    return rows


def read_manifest(cells_dir: Path) -> dict[str, str]:
    """``task_id`` to ``final_digest`` for every row of ``manifest.csv``."""
    path = cells_dir / MANIFEST_NAME
    if not path.is_file():
        raise ProgressError(f"manifest is missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    manifest = {row["task_id"]: row["final_digest"] for row in rows}
    if len(manifest) != len(rows):
        raise ProgressError(f"{MANIFEST_NAME} repeats a task_id")
    return manifest


def _require_manifest_digest(task: Mapping[str, Any], manifest: Mapping[str, str]) -> None:
    """Refuse a cell that did not run the manifest package for its task."""
    task_id = str(task["task_id"])
    expected = manifest.get(task_id)
    digest = str(task.get("final_digest") or "")
    if expected is None:
        raise ProgressError(f"{task_id}: absent from {MANIFEST_NAME}; cannot check its digest")
    if digest != expected:
        raise ProgressError(
            f"{task_id}: cell final_digest {digest} != manifest final_digest {expected}"
        )
    if task.get("digest_matches_manifest") is not True:
        flag = task.get("digest_matches_manifest")
        raise ProgressError(
            f"{task_id}: digest_matches_manifest is {flag!r}, not true "
            f"(cell final_digest {digest}, manifest final_digest {expected})"
        )


def _require_attack_counts(task: Mapping[str, Any]) -> None:
    """Refuse a cell whose executed and unexecuted attack counts do not add up."""
    task_id = str(task["task_id"])
    total = task.get("attacks_total")
    if total is None:
        return
    if not isinstance(total, int):
        raise ProgressError(f"{task_id}: attacks_total {total!r} is not a count")
    executed = task.get("attacks_executed")
    if not isinstance(executed, int):
        raise ProgressError(f"{task_id}: attacks_executed {executed!r} is not a count")
    if executed > total:
        raise ProgressError(f"{task_id}: attacks_executed {executed} > attacks_total {total}")
    if "attacks_not_executed" in task:
        unexecuted = task["attacks_not_executed"]
        if not isinstance(unexecuted, dict):
            raise ProgressError(
                f"{task_id}: attacks_not_executed {unexecuted!r} is not a mapping"
            )
        if executed + len(unexecuted) != total:
            raise ProgressError(
                f"{task_id}: attacks_executed {executed} + attacks_not_executed "
                f"{len(unexecuted)} != attacks_total {total}"
            )


def _require_consistent_verdict(task: Mapping[str, Any]) -> None:
    """Refuse a clean or cracked verdict whose reward or attacks contradict it."""
    task_id = str(task["task_id"])
    verdict = task.get("verdict")
    reward = task.get("reward")
    if verdict == "clean":
        if reward != 0:
            raise ProgressError(f"{task_id}: verdict clean with reward {reward!r}, not 0")
        cracking = task.get("cracking_attacks") or []
        if cracking:
            raise ProgressError(
                f"{task_id}: verdict clean with {len(cracking)} cracking_attacks, not none"
            )
    elif verdict == "cracked" and (not isinstance(reward, (int, float)) or reward <= 0):
        raise ProgressError(f"{task_id}: verdict cracked with reward {reward!r}, not > 0")


def _require_file_totals(data: Mapping[str, Any], source: str) -> None:
    """Refuse a cell file whose ``totals`` block disagrees with its own tasks."""
    totals = data.get("totals")
    if totals is None:
        return
    tasks = data.get("tasks", [])
    counts = {"tasks": len(tasks)}
    for verdict in VERDICT_TOTALS:
        counts[verdict] = sum(1 for task in tasks if task.get("verdict") == verdict)
    for field in TOTALS_FIELDS:
        if field not in totals:
            raise ProgressError(f"{source}: totals is missing {field}")
        stated = totals[field]
        if stated != counts[field]:
            raise ProgressError(
                f"{source}: totals {field} {stated} != {counts[field]} over its tasks"
            )


def read_cells(
    cells_dir: Path, root: Path = ROOT, *, manifest: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Every cell of every ``ladder-*.json`` with the v6 cell schema, with its file."""
    cells: list[dict[str, Any]] = []
    for path in sorted(cells_dir.glob(CELLS_GLOB)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != CELLS_SCHEMA:
            continue
        source = path.relative_to(root).as_posix()
        for task in data.get("tasks", []):
            _require_manifest_digest(task, manifest)
            _require_attack_counts(task)
            _require_consistent_verdict(task)
            cells.append(
                {**task, "_source_file": source, "_card": task.get("card", data.get("card", ""))}
            )
        _require_file_totals(data, source)
    return cells


def _newest_cell(cells: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """A re-run supersedes an earlier cell on the same rank: latest finish wins."""
    return max(cells, key=lambda cell: (str(cell.get("finished_utc") or ""), cell["_source_file"]))


def read_subset_cells(
    cells_dir: Path, root: Path = ROOT, *, manifest: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Flatten ``tasks[].subsets[]`` of every subset-schema ``ladder-*.json``, by task."""
    subsets: list[dict[str, Any]] = []
    for path in sorted(cells_dir.glob(CELLS_GLOB)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SUBSET_CELLS_SCHEMA:
            continue
        source = path.relative_to(root).as_posix()
        for task in data.get("tasks", []):
            _require_manifest_digest(task, manifest)
            for subset in task.get("subsets", []):
                subsets.append({**subset, "task_id": task["task_id"], "_source_file": source})
    return subsets


def _newest_subset(subsets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The highest-numbered subset supersedes earlier ones; a repeated number: last listed wins."""
    newest = subsets[0]
    for subset in subsets[1:]:
        if int(subset["subset"]) >= int(newest["subset"]):
            newest = subset
    return newest


def _refusal_hit(cell: dict[str, Any]) -> str:
    """The file a refusal cell's ``verifier_tamper_hit`` names, after ``" in "``."""
    hit = cell.get("verifier_tamper_hit")
    _, separator, path = str(hit or "").partition(" in ")
    if not separator or not path:
        raise ProgressError(
            f"{cell['task_id']}: tamper refusal without a ' in <file>' verifier_tamper_hit: {hit!r}"
        )
    return path


def assemble(
    ranking: Sequence[dict[str, str]],
    cells: Sequence[dict[str, Any]],
    subsets: Sequence[dict[str, Any]] = (),
) -> list[dict[str, str]]:
    """One row per ranked task; rows without a cell read ``verdict=none``."""
    by_rank: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        by_rank.setdefault(int(cell["ranking"]["rank"]), []).append(cell)
    by_task: dict[str, list[dict[str, Any]]] = {}
    for subset in subsets:
        by_task.setdefault(str(subset["task_id"]), []).append(subset)
    known = {int(row["rank"]) for row in ranking}
    stray = sorted(rank for rank in by_rank if rank not in known and rank <= LAST_RANK)
    if stray:
        raise ProgressError(f"cells name ranks missing from the ranking: {stray}")
    rows: list[dict[str, str]] = []
    for ranked in ranking:
        rank = int(ranked["rank"])
        row = {
            "rank": str(rank),
            "task_id": ranked["task_id"],
            "phase": ranked["phase"],
            "language": ranked["language"],
            "ladder_version": "",
            "verdict": "none",
            "basis": "none",
            "wall_seconds": "",
            "source_file": "",
            "subset_verdict": "none",
            "subset_reward": "",
            "refusal_hit": "",
            "attacks_executed": "",
        }
        if rank in by_rank:
            cell = _newest_cell(by_rank[rank])
            if cell["task_id"] != ranked["task_id"]:
                raise ProgressError(
                    f"rank {rank}: cell is for {cell['task_id']}, ranking says {ranked['task_id']}"
                )
            basis = classify_basis(cell.get("verifier_decision"))
            row.update(
                ladder_version=str(cell["ladder_version"]),
                verdict=str(cell["verdict"]),
                basis=basis,
                wall_seconds=str(cell["wall_seconds"]),
                source_file=cell["_source_file"],
                refusal_hit=_refusal_hit(cell) if basis == "tamper-refusal" else "",
                attacks_executed=str(cell["attacks_executed"]),
            )
        if ranked["task_id"] in by_task:
            subset = _newest_subset(by_task[ranked["task_id"]])
            reward = subset.get("reward")
            row.update(
                subset_verdict=str(subset["verdict"]),
                subset_reward="" if reward is None else str(reward),
            )
        rows.append(row)
    return rows


def build(root: Path = ROOT, *, cells_dir: Path) -> list[dict[str, str]]:
    manifest = read_manifest(cells_dir)
    return assemble(
        read_ranking(root),
        read_cells(cells_dir, root, manifest=manifest),
        read_subset_cells(cells_dir, root, manifest=manifest),
    )


def render(rows: Sequence[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def basis_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["basis"]] = counts.get(row["basis"], 0) + 1
    return counts


def reference_basis(language: str, bases: Sequence[str]) -> str:
    """The one basis whose wall times set a language's median."""
    if REFUSAL_BASIS in bases:
        return REFUSAL_BASIS
    if GRADED_BASIS in bases:
        return GRADED_BASIS
    raise ProgressError(
        f"{language}: no {REFUSAL_BASIS} or {GRADED_BASIS} row to set a median; "
        f"bases are {sorted(bases)}"
    )


def wall_seconds(row: Mapping[str, Any]) -> float:
    """A row's ``wall_seconds`` as a number; a covered row must carry one."""
    try:
        return float(row["wall_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise ProgressError(
            f"{row.get('task_id')}: wall_seconds {row.get('wall_seconds')!r} is not a number"
        ) from error


def summary(rows: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    """One entry per language: covered rows by basis, uncovered rows, wall median/max.

    ``projected_hours`` is ``uncovered x median_seconds``: the serial Docker hours
    left to cover the language's remaining ranks at its reference-basis wall time.
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["language"], []).append(row)
    entries: list[dict[str, Any]] = []
    for language in sorted(grouped):
        language_rows = grouped[language]
        covered = [row for row in language_rows if row["basis"] != "none"]
        bases = Counter(row["basis"] for row in covered)
        reference = reference_basis(language, sorted(bases))
        walls = [wall_seconds(row) for row in covered if row["basis"] == reference]
        if not walls:
            raise ProgressError(f"{language}: {reference} rows carry no wall time")
        median = statistics.median(walls)
        uncovered = len(language_rows) - len(covered)
        entries.append(
            {
                "language": language,
                "covered": len(covered),
                "bases": dict(bases),
                "uncovered": uncovered,
                "median_seconds": median,
                "max_seconds": max(walls),
                "projected_hours": uncovered * median / SECONDS_PER_HOUR,
            }
        )
    return entries


def render_summary(rows: Sequence[dict[str, str]]) -> str:
    """The ``--summary`` table, in the same Markdown pipe-table shape as the README."""
    buffer = io.StringIO()
    buffer.write(
        "| language | covered | uncovered | median wall (s) | max wall (s) "
        "| projected serial hours |\n"
    )
    buffer.write("|---|---:|---:|---:|---:|---:|\n")
    entries = summary(rows)
    for entry in entries:
        bases = ", ".join(f"`{name}` {count}" for name, count in sorted(entry["bases"].items()))
        buffer.write(
            f"| {entry['language']} | {entry['covered']} ({bases}) | {entry['uncovered']} "
            f"| {entry['median_seconds']:.1f} | {entry['max_seconds']:.1f} "
            f"| {entry['projected_hours']:.2f} |\n"
        )
    buffer.write(
        f"| total | {sum(entry['covered'] for entry in entries)} "
        f"| {sum(entry['uncovered'] for entry in entries)} | | "
        f"| {sum(entry['projected_hours'] for entry in entries):.2f} |\n"
    )
    return buffer.getvalue()


def read_pending_controls(path: Path) -> list[dict[str, Any]]:
    """The ``remaining_original_controls`` entries of ``pending-controls.json``.

    Refuse a repeated task, an agent other than ``oracle`` or ``nop``, and a
    ``runnable`` value that is not a boolean, so the pending table counts only
    cells the inventory can actually run.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProgressError(f"pending controls are unreadable: {path}: {error}") from error
    if not isinstance(data, dict):
        raise ProgressError(f"{path}: top level is not an object")
    entries = data.get("remaining_original_controls")
    if not isinstance(entries, list):
        raise ProgressError(f"{path}: no remaining_original_controls list")
    seen: set[str] = set()
    controls: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProgressError(f"{path}: remaining_original_controls holds a non-object entry")
        task_id = str(entry.get("task_id") or "")
        if not task_id:
            raise ProgressError(f"{path}: a control entry has no task_id")
        if task_id in seen:
            raise ProgressError(f"{path}: repeats task {task_id}")
        seen.add(task_id)
        agents = entry.get("agents")
        if not isinstance(agents, list) or any(agent not in PENDING_AGENTS for agent in agents):
            raise ProgressError(f"{task_id}: agents {agents!r} are not cells of {PENDING_AGENTS}")
        runnable = entry.get("runnable")
        if not isinstance(runnable, bool):
            raise ProgressError(f"{task_id}: runnable {runnable!r} is not a boolean")
        controls.append({"task_id": task_id, "agents": list(agents), "runnable": runnable})
    return controls


def projected_control_hours(oracle_cells: int, nop_cells: int) -> float:
    """Serial hours for pending repeat-3 controls at the measured cell means.

    Each pending cell is one repeat-3 control, and the README means are per
    such cell, so a cell costs its agent's mean once; the three runs are
    already inside it.
    """
    minutes = oracle_cells * ORACLE_CELL_MINUTES + nop_cells * NOP_CELL_MINUTES
    return minutes / 60.0


def _pending_entry(
    name: str, tasks: int, oracle_cells: int, nop_cells: int, runnable_cells: int
) -> dict[str, Any]:
    """One pending-controls row from a phase's cell counts."""
    return {
        "phase": name,
        "tasks": tasks,
        "oracle_cells": oracle_cells,
        "nop_cells": nop_cells,
        "runnable_cells": runnable_cells,
        "projected_hours": projected_control_hours(oracle_cells, nop_cells),
    }


def pending_controls(
    controls: Sequence[Mapping[str, Any]], ranking: Sequence[Mapping[str, str]]
) -> list[dict[str, Any]]:
    """One row per ranking phase, then ``unranked``, then ``total``.

    A control whose task is absent from the ranking lands on the ``unranked``
    row rather than being dropped. ``runnable_cells`` counts the oracle and nop
    cells of a task the inventory marks runnable; ``projected_hours`` counts
    every pending cell, so it measures the phase's whole debt.
    """
    phase_of = {row["task_id"]: row["phase"] for row in ranking}
    grouped: dict[str, dict[str, int]] = {}
    for control in controls:
        phase = phase_of.get(str(control["task_id"]), UNRANKED)
        counts = grouped.setdefault(phase, {"tasks": 0, "oracle": 0, "nop": 0, "runnable": 0})
        agents = [str(agent) for agent in control["agents"]]
        counts["tasks"] += 1
        counts["oracle"] += agents.count("oracle")
        counts["nop"] += agents.count("nop")
        if control["runnable"]:
            counts["runnable"] += len(agents)
    empty = {"tasks": 0, "oracle": 0, "nop": 0, "runnable": 0}
    entries = [
        _pending_entry(
            phase, counts["tasks"], counts["oracle"], counts["nop"], counts["runnable"]
        )
        for phase, counts in sorted(grouped.items())
        if phase != UNRANKED
    ]
    unranked = grouped.get(UNRANKED, empty)
    entries.append(
        _pending_entry(
            UNRANKED, unranked["tasks"], unranked["oracle"], unranked["nop"], unranked["runnable"]
        )
    )
    fields = ("tasks", "oracle_cells", "nop_cells", "runnable_cells")
    total = _pending_entry(
        "total", *(sum(entry[field] for entry in entries) for field in fields)
    )
    return [*entries, total]


def render_pending_controls(
    controls: Sequence[Mapping[str, Any]], ranking: Sequence[Mapping[str, str]]
) -> str:
    """The ``--pending-controls`` table, in the README's Markdown pipe-table shape."""
    buffer = io.StringIO()
    buffer.write(
        "| phase | tasks | oracle cells | nop cells | runnable cells "
        "| projected serial hours |\n"
    )
    buffer.write("|---|---:|---:|---:|---:|---:|\n")
    for entry in pending_controls(controls, ranking):
        buffer.write(
            f"| {entry['phase']} | {entry['tasks']} | {entry['oracle_cells']} "
            f"| {entry['nop_cells']} | {entry['runnable_cells']} "
            f"| {entry['projected_hours']:.2f} |\n"
        )
    return buffer.getvalue()


def _pipe_cells(line: str) -> list[str]:
    """The stripped cells of a Markdown pipe-table line."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _doc_match(pattern: str, text: str, page: Path, anchor: str) -> re.Match[str]:
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if len(matches) != 1:
        raise ProgressError(f"{page}: expected one {anchor!r}, found {len(matches)}")
    return matches[0]


def _doc_table(text: str, page: Path, anchor: str, headers: Sequence[str]) -> re.Match[str]:
    start = _doc_match(re.escape(anchor), text, page, anchor).end()
    heading = re.search(r"(?m)^#{1,6}[ \t]+", text[start:])
    end = start + heading.start() if heading else len(text)
    pattern = re.compile(r"(?m)^\|[^\r\n]*(?:\r?\n\|[^\r\n]*)*")
    tables = list(pattern.finditer(text, start, end))
    if len(tables) != 1:
        raise ProgressError(f"{page}: {anchor!r}: expected one table, found {len(tables)}")
    table = tables[0]
    lines = table.group().splitlines()
    if len(lines) < 3 or _pipe_cells(lines[0]) != list(headers):
        raise ProgressError(f"{page}: {anchor!r}: missing or invalid table header")
    if any(line.strip().count("|") != len(headers) + 1 or not line.rstrip().endswith("|")
           for line in lines):
        raise ProgressError(f"{page}: {anchor!r}: invalid table row")
    separators = _pipe_cells(lines[1])
    if len(separators) != len(headers) or any(
        re.fullmatch(r":?-+:?", cell) is None for cell in separators
    ):
        raise ProgressError(f"{page}: {anchor!r}: invalid table separator")
    return table


def _doc_count_table(
    table: str, page: Path, anchor: str, counts: Mapping[str, int], *, signature: bool = False
) -> str:
    edits: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    offset = 0
    for index, line in enumerate(table.splitlines(keepends=True)):
        if index >= 2:
            cells = _pipe_cells(line)
            columns = 3 if signature else 2
            if len(cells) != columns:
                raise ProgressError(f"{page}: {anchor!r}: invalid count row {line.strip()!r}")
            key = cells[1 if signature else 0].strip("` ")
            if not key or key in seen or key not in counts:
                raise ProgressError(f"{page}: {anchor!r}: missing, repeated or unknown row {key!r}")
            if signature and not cells[0]:
                raise ProgressError(f"{page}: {anchor!r}: missing signature label for {key!r}")
            seen.add(key)
            prefix = r"\|[^|\r\n]*\|" + (r"[^|\r\n]*\|" if signature else "")
            match = re.fullmatch(
                prefix + r"[ \t]*(?P<count>[0-9]+)(?:[ \t]+[^|\r\n]*)?\|[ \t]*(?:\r?\n)?",
                line,
            )
            if match is None:
                raise ProgressError(f"{page}: {anchor!r}: invalid count for row {key!r}")
            edits.append(
                (offset + match.start("count"), offset + match.end("count"), str(counts.get(key, 0)))
            )
        offset += len(line)
    missing = counts.keys() - seen
    if missing:
        raise ProgressError(f"{page}: {anchor!r}: missing count rows for {sorted(missing)}")
    return _doc_patch(table, edits)


def _doc_patch(text: str, edits: Sequence[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _doc_summary(table: str, rows: Sequence[dict[str, str]], page: Path) -> str:
    seen: set[str] = set()
    lines = table.splitlines()[2:]
    for line in lines:
        cells = _pipe_cells(line)
        if len(cells) != 6 or cells[0] in seen:
            raise ProgressError(f"{page}: {SUMMARY_HEADING!r}: invalid or repeated summary row")
        language, covered, uncovered, median, maximum, projected = cells
        seen.add(language)
        if (
            re.fullmatch(r"[0-9]+(?: \([^)\r\n]+\))?", covered) is None
            or re.fullmatch(r"[0-9]+", uncovered) is None
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", projected) is None
            or (
                language != "total"
                and any(
                    re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None
                    for value in (median, maximum)
                )
            )
            or (language == "total" and (median or maximum))
        ):
            raise ProgressError(f"{page}: {SUMMARY_HEADING!r}: invalid counts for {language!r}")
    expected = {row["language"] for row in rows} | {"total"}
    if seen != expected or _pipe_cells(lines[-1])[0] != "total":
        raise ProgressError(
            f"{page}: {SUMMARY_HEADING!r}: expected summary rows {sorted(expected)}, "
            f"found {sorted(seen)} with total last"
        )
    newline = "\r\n" if "\r\n" in table else "\n"
    return render_summary(rows).rstrip("\n").replace("\n", newline)


def sync_docs(rows: Sequence[dict[str, str]], *, root: Path, cells_dir: Path) -> None:
    """Refresh ten tested fragments, validating every page before writing any."""
    pages = (root / "README.md", root / "docs" / "mimo" / "README.md", cells_dir / "README.md")
    originals: dict[Path, str] = {}
    for page in pages:
        try:
            originals[page] = page.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ProgressError(f"{page}: cannot read documentation: {error}") from error
    counts = basis_counts(rows)
    covered = len(rows) - counts.get("none", 0)
    route_counts = (
        covered,
        sum(row["verdict"] == "clean" for row in rows),
        counts.get(REFUSAL_BASIS, 0),
        counts.get(GRADED_BASIS, 0),
        counts.get("unscored", 0),
        counts.get("none", 0),
    )
    replacements: dict[Path, str] = {}
    for page in pages[:2]:
        text = originals[page]
        _doc_match(r"v6\s+basis\s+\(100\s+ranks\):", text, page, "v6 basis (100 ranks):")
        match = _doc_match(ROUTE_BASIS.pattern, text, page, "complete v6 basis sentence")
        replacements[page] = _doc_patch(
            text,
            [
                (match.start(index), match.end(index), str(count))
                for index, count in enumerate(route_counts, start=1)
            ],
        )
    page = pages[2]
    text = originals[page]
    basis_table = _doc_table(text, page, BASIS_ANCHOR, ("basis", "ranks"))
    _doc_match(
        r"\(100\s+ranks,\s+[0-9]+\s+covered;\s+Docker-free\):",
        text,
        page,
        BASIS_ANCHOR + " covered subanchor",
    )
    covered_match = _doc_match(
        re.escape(BASIS_ANCHOR) + r"\s+\(100\s+ranks,\s+([0-9]+)\s+covered;\s+Docker-free\):",
        text,
        page,
        BASIS_ANCHOR + " covered count",
    )
    basis_text = _doc_count_table(
        basis_table.group(),
        page,
        BASIS_ANCHOR,
        {basis: counts.get(basis, 0) for basis in (REFUSAL_BASIS, GRADED_BASIS, "unscored", "none")},
    )
    signatures = Counter(
        row["refusal_hit"].rsplit("/", 1)[-1] for row in rows if row["refusal_hit"]
    )
    signature_table = _doc_table(
        text, page, SIGNATURE_HEADING, ("signature", "file suffix", "count")
    )
    signature_text = _doc_count_table(
        signature_table.group(), page, SIGNATURE_HEADING, signatures, signature=True
    )
    summary_table = _doc_table(
        text,
        page,
        SUMMARY_HEADING,
        (
            "language", "covered", "uncovered", "median wall (s)", "max wall (s)",
            "projected serial hours",
        ),
    )
    summary_text = _doc_summary(summary_table.group(), rows, page)
    refusals = counts.get(REFUSAL_BASIS, 0)
    clean = route_counts[1]
    refusal_rate = 100 * refusals / clean if clean else 0.0
    python_entry = next(entry for entry in summary(rows) if entry["language"] == "python")
    refusal_rows = [row for row in rows if row["basis"] == REFUSAL_BASIS]
    all_attacks = sum(row["attacks_executed"] == "14" for row in refusal_rows)
    short_tasks = [
        f"`{_slice_task(row['task_id'])}`"
        for row in sorted(refusal_rows, key=lambda row: int(row["rank"]))
        if row["attacks_executed"] == "13"
    ]
    if len(short_tasks) > 2:
        short_list = ", ".join(short_tasks[:-1]) + ", and " + short_tasks[-1]
    else:
        short_list = " and ".join(short_tasks) or "none"
    fragments = (
        (
            "rate among clean ladder verdicts is",
            r"([0-9]+)\s+of\s+the\s+([0-9]+)\s+`clean`\s+verdicts\s+are\s+tamper\s+"
            r"refusals,\s+so\s+the\s+refusal\s+rate\s+among\s+clean\s+ladder\s+verdicts\s+"
            r"is\s+([0-9]+)\s*/\s*([0-9]+)\s+\(([0-9]+(?:\.[0-9]+)?)\s*%\);\s+"
            r"([0-9]+)\s+rest\s+on\s+a\s+graded\s+run\.",
            (refusals, clean, refusals, clean, f"{refusal_rate:.1f}", counts.get(GRADED_BASIS, 0)),
        ),
        (
            "refusal verdicts",
            r"so\s+the\s+([0-9]+)\s+refusal\s+verdicts",
            (refusals,),
        ),
        (
            "therefore uses its",
            r"therefore\s+uses\s+its\s+([0-9]+)\s+tamper\s+refusals,\s+not\s+its\s+"
            r"([0-9]+)\s+graded\s+rows",
            (
                python_entry["bases"].get(REFUSAL_BASIS, 0),
                python_entry["bases"].get(GRADED_BASIS, 0),
            ),
        ),
        (
            "refused rows,",
            r"Of\s+the\s+([0-9]+)\s+refused\s+rows,\s+([0-9]+)\s+ran\s+all\s+14\s+"
            r"attacks;\s+(`[0-9]{6}`(?:\s*(?:,\s*(?:and\s+)?|and\s+)"
            r"`[0-9]{6}`)*|none)\s+ran\s+13\s+of\s+14\.",
            (refusals, all_attacks, short_list),
        ),
        (
            "uncovered rows one at a time",
            r"covering\s+the\s+([0-9]+)\s+uncovered\s+rows\s+one\s+at\s+a\s+time\s+"
            r"on\s+local\s+Docker\s+costs\s+about\s+\*\*([0-9]+(?:\.[0-9]+)?)\s+h\*\*",
            (python_entry["uncovered"], f"{python_entry['projected_hours']:.2f}"),
        ),
    )
    fragment_edits: list[tuple[int, int, str]] = []
    for anchor, pattern, values in fragments:
        _doc_match(r"\s+".join(map(re.escape, anchor.split())), text, page, anchor)
        match = _doc_match(pattern, text, page, f"complete {anchor} fragment")
        fragment_edits.extend(
            (match.start(index), match.end(index), str(value))
            for index, value in enumerate(values, start=1)
        )
    replacements[page] = _doc_patch(
        text,
        [
            (covered_match.start(1), covered_match.end(1), str(covered)),
            (basis_table.start(), basis_table.end(), basis_text),
            (signature_table.start(), signature_table.end(), signature_text),
            (summary_table.start(), summary_table.end(), summary_text),
            *fragment_edits,
        ],
    )
    for page, replacement in replacements.items():
        if replacement != originals[page]:
            page.write_bytes(replacement.encode("utf-8"))


def _slice_attacks(value: str, where: str) -> int:
    """The executed count of an ``N/M`` attacks cell."""
    executed, separator, _ = value.partition("/")
    if not separator or not executed.strip().isdigit():
        raise ProgressError(f"{where}: attacks cell {value!r} is not N/M")
    return int(executed)


def read_slice_tables(text: str) -> list[dict[str, Any]]:
    """Every README row of every pipe table under a ``## ... ladder cells`` heading.

    A section without a table (HAR-244 states its three rows in prose) yields
    no rows. Each row carries ``heading``, ``rank``, ``task`` (the backticked
    rank suffix), ``verdict``, ``wall_minutes``, and ``attacks`` (``None``
    without an ``attacks`` or ``attacks executed`` column).
    """
    rows: list[dict[str, Any]] = []
    for section in re.split(r"(?m)^## ", text)[1:]:
        heading, _, body = section.partition("\n")
        if SLICE_MARKER not in heading:
            continue
        table = [line for line in body.splitlines() if line.startswith("|")]
        if not table:
            continue
        header = [cell.lower() for cell in _pipe_cells(table[0])]
        missing = [name for name in SLICE_COLUMNS if name not in header]
        if missing:
            raise ProgressError(
                f"slice table {heading!r} lacks column(s): {', '.join(missing)}"
            )
        attacks = next(
            (index for index, name in enumerate(header) if name in SLICE_ATTACK_COLUMNS),
            None,
        )
        for line in table[1:]:
            cells = _pipe_cells(line)
            if len(cells) != len(header):
                raise ProgressError(
                    f"slice table {heading!r} row {line!r} has {len(cells)} "
                    f"cell(s), not {len(header)}"
                )
            if all(set(cell) <= set("-: ") for cell in cells):
                continue
            record = dict(zip(header, cells, strict=True))
            where = f"slice table {heading!r} rank {record['rank']}"
            match = SLICE_WALL.search(record["wall"])
            if match is None:
                raise ProgressError(f"{where}: wall cell {record['wall']!r} has no minutes")
            rows.append(
                {
                    "heading": heading,
                    "rank": int(record["rank"]),
                    "task": record["task"].strip("`"),
                    "verdict": record["verdict"],
                    "wall_minutes": float(match.group("minutes")),
                    "attacks": _slice_attacks(cells[attacks], where) if attacks is not None else None,
                }
            )
    return rows


def check_slice_tables(text: str, rows: Sequence[Mapping[str, str]]) -> int:
    """Refuse any README slice row that disagrees with ``rows``; return the row count.

    Compares rank and task suffix, the verdict's leading word, wall minutes
    within ``SLICE_WALL_TOLERANCE``, and, where the table has an attacks
    column, the executed attack count.
    """
    by_rank = {int(row["rank"]): row for row in rows}
    tables = read_slice_tables(text)
    for table in tables:
        rank = int(table["rank"])
        task = str(table["task"])
        row = by_rank.get(rank)
        if row is None:
            raise ProgressError(f"rank {rank} (`{task}`): absent from ladder-progress.csv")
        if not str(row["task_id"]).endswith(task):
            raise ProgressError(
                f"rank {rank} (`{task}`): ladder-progress.csv names `{row['task_id']}`"
            )
        leading = str(table["verdict"]).split()
        verdict = leading[0].rstrip(":") if leading else ""
        if verdict != row["verdict"]:
            raise ProgressError(
                f"rank {rank} (`{task}`): verdict {verdict!r} disagrees "
                f"with {row['verdict']!r}"
            )
        seconds = wall_seconds(row)
        if abs(float(table["wall_minutes"]) - seconds / 60) > SLICE_WALL_TOLERANCE:
            raise ProgressError(
                f"rank {rank} (`{task}`): wall {table['wall_minutes']} min "
                f"disagrees with {seconds:g} s"
            )
        if table["attacks"] is not None and int(table["attacks"]) != int(row["attacks_executed"]):
            raise ProgressError(
                f"rank {rank} (`{task}`): attacks executed {table['attacks']} "
                f"disagree with {row['attacks_executed']}"
            )
    return len(tables)


def _slice_rank(task: Mapping[str, Any]) -> int:
    """The cell task's ``ranking.rank`` as an integer."""
    try:
        return int(task["ranking"]["rank"])
    except (KeyError, TypeError, ValueError) as error:
        raise ProgressError(f"{task.get('task_id')}: no integer ranking.rank") from error


def _slice_task(task_id: object) -> str:
    """The backticked task column: the six-digit suffix of the task id."""
    return str(task_id).rsplit("-", 1)[-1]


def _slice_basis(verifier_decision: object) -> str:
    """The README basis cell: a refusal, the verifier's rc, or unscored."""
    decision = str(verifier_decision or "")
    if TAMPER_SIGNATURE in decision:
        return "tamper refusal"
    match = VERIFIER_RC.search(decision)
    if match is not None:
        return f"graded (`rc={match.group(1)}`)"
    return "unscored"


def _slice_attack_cell(task: Mapping[str, Any]) -> str:
    """The executed/total attacks cell; both counts must be present."""
    executed = task.get("attacks_executed")
    total = task.get("attacks_total")
    if (
        isinstance(executed, bool)
        or not isinstance(executed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
    ):
        raise ProgressError(
            f"{task.get('task_id')}: attacks {executed!r}/{total!r} is not a count pair"
        )
    return f"{executed}/{total}"


def _slice_wall(task: Mapping[str, Any]) -> str:
    """The wall cell: ``wall_seconds`` in minutes, one decimal, plus ``min CLI``."""
    seconds = task.get("wall_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ProgressError(
            f"{task.get('task_id')}: wall_seconds {seconds!r} is not a number"
        )
    return f"{seconds / 60:.1f} min CLI"


def _slice_attempts(task: Mapping[str, Any]) -> str:
    """The retained-attempts cell: the published attempt and each prior attempt."""
    prior = task.get("prior_attempts") or []
    publication = "published" if task.get("published_job") else "not published"
    parts = [f"attempt {len(prior) + 1} {publication}"]
    for position, entry in enumerate(prior, start=1):
        number = entry.get("attempt", position)
        if entry.get("published_job"):
            label = str(entry.get("verdict") or "unscored")
            parts.append(f"{label} attempt {number} published and retained")
        elif any(entry.get(key) for key in ("job_name", "raw_job", "trial_name")):
            parts.append(f"attempt {number} retained")
        else:
            parts.append(f"no-job attempt {number} retained")
    return "; ".join(parts)


def read_slice_cells(
    path: Path, *, cells_dir: Path = HERE
) -> list[dict[str, Any]]:
    """One cell file's tasks after the manifest and self-consistency checks."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CELLS_SCHEMA:
        found = data.get("schema") if isinstance(data, dict) else None
        raise ProgressError(f"{path.name}: schema {found!r} is not {CELLS_SCHEMA}")
    manifest = read_manifest(cells_dir)
    tasks = data.get("tasks", [])
    for task in tasks:
        _require_manifest_digest(task, manifest)
        _require_attack_counts(task)
        _require_consistent_verdict(task)
    _require_file_totals(data, path.name)
    return tasks


def render_slice(path: Path, *, cells_dir: Path = HERE) -> str:
    """The README pipe table for one cell file, one row per rank.

    The columns are those of the ranks 46-57 Python slice sections; the rendered
    rows must be byte-identical to the table the README quotes for that file.
    """
    lines = [SLICE_HEADER, SLICE_SEPARATOR]
    for task in sorted(read_slice_cells(path, cells_dir=cells_dir), key=_slice_rank):
        task_id = task["task_id"]
        lines.append(
            f"| {_slice_rank(task)} | `{_slice_task(task_id)}` "
            f"| {str(task.get('verdict') or '')} "
            f"| {_slice_basis(task.get('verifier_decision'))} "
            f"| {_slice_attack_cell(task)} "
            f"| {_slice_wall(task)} "
            f"| {_slice_attempts(task)} |"
        )
    return "\n".join(lines) + "\n"


def attempts_candidates(cell: Mapping[str, Any], card: str, results_root: Path) -> list[Path]:
    """Explicit jobs, published conventions, then raw conventions, without repeats."""
    card = str(cell.get("card", card) or "")
    trial = Path(str(cell["trial_name"])) / "agent" / "cheat" / "attempts.json"
    candidates = [
        Path(str(cell[key])).expanduser() / trial
        for key in ("published_job", "raw_job")
        if cell.get(key)
    ]
    dates = sorted(path for path in results_root.expanduser().glob("*") if path.is_dir())
    candidates.extend(date / f"{card}-{cell['job_name']}" / trial for date in dates)
    candidates.extend(
        date
        / f"{card.lower()}-mimo-v6-ladder"
        / str(cell["task_id"])
        / str(cell["job_name"])
        / trial
        for date in dates
    )
    return list(dict.fromkeys(candidates))


def _read_reward(path: Path) -> Any:
    """A ``result.json`` verifier reward; None when the file or block is absent."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    verifier = data.get("verifier_result")
    if not isinstance(verifier, Mapping):
        return None
    rewards = verifier.get("rewards")
    if not isinstance(rewards, Mapping):
        return None
    return rewards.get("reward")


def _runner_name(text: str) -> str | None:
    """The runner name of a ``RUNNER_B=<name>`` or ``runner=<name>`` status."""
    match = VERIFIER_RUNNER.search(text)
    return match.group(1) if match is not None else None


def _verifier_diffs(cell: Mapping[str, Any], verifier_dir: Path) -> list[str]:
    """Every cell verifier field that the trial's ``verifier/`` files disprove.

    ``verifier_tamper_hit``, ``baseline_runner`` and ``reward`` were hand-copied
    from ``tamper.log`` (the last ``tamper `` line), ``baseline_runner.txt``
    and ``reward.txt``. Only non-null cell fields are compared; an absent
    verifier file skips its comparison, so a graded cell whose ``tamper.log``
    holds no ``tamper `` line still matches. ``baseline_runner`` compares only
    the runner name: cell formats vary by slice (``RUNNER_B=pytest``,
    ``runner=pytest rc=1``, ``baseline_sec=N runner=pytest rc=1``), while the
    file always records ``RUNNER_B=<name>``.
    """
    evidence: list[tuple[str, bool, Any, Any]] = []
    tamper_log = verifier_dir / "tamper.log"
    tamper_hit = None
    if tamper_log.is_file():
        try:
            lines = tamper_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        tamper_hit = next(
            (line for line in reversed(lines) if line.startswith("tamper ")), None
        )
    evidence.append(
        (
            "verifier_tamper_hit",
            tamper_log.is_file(),
            cell.get("verifier_tamper_hit"),
            tamper_hit,
        )
    )
    runner_file = verifier_dir / "baseline_runner.txt"
    runner = None
    if runner_file.is_file():
        try:
            runner = _runner_name(runner_file.read_text(encoding="utf-8"))
        except OSError:
            runner = None
    expected_runner = None
    if cell.get("baseline_runner") is not None:
        expected_runner = _runner_name(str(cell["baseline_runner"])) or cell["baseline_runner"]
    evidence.append(("baseline_runner", runner_file.is_file(), expected_runner, runner))
    reward_file = verifier_dir / "reward.txt"
    reward = None
    if reward_file.is_file():
        try:
            raw = reward_file.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        try:
            reward = float(raw)
        except ValueError:
            reward = raw or None
    evidence.append(("reward", reward_file.is_file(), cell.get("reward"), reward))
    return [
        f"{name}: cell {expected!r} != verifier {actual!r}"
        for name, present, expected, actual in evidence
        if present and expected is not None and expected != actual
    ]


def verify_cell(cell: Mapping[str, Any], card: str, results_root: Path) -> dict[str, Any]:
    """Compare a cell's recorded hash with every candidate until one matches.

    A matched cell's trial ``result.json`` reward must equal the cell's
    ``reward``; a differing reward makes the cell ``reward-mismatch``. The
    trial's ``verifier/`` files must also agree with the cell's non-null
    ``verifier_tamper_hit``, ``baseline_runner`` and ``reward``; a differing
    value makes the cell ``verifier-mismatch``.
    """
    result: dict[str, Any] = {
        "cell": cell,
        "card": str(cell.get("card", card) or ""),
        "status": "unhashed",
        "path": None,
        "expected": cell.get("attempts_json_sha256"),
        "actual": None,
        "candidate_count": 0,
        "reward_path": None,
        "expected_reward": cell.get("reward"),
        "actual_reward": None,
        "verifier_path": None,
        "verifier_diffs": [],
    }
    expected = result["expected"]
    if not isinstance(expected, str) or not expected:
        return result
    candidates = attempts_candidates(cell, card, results_root)
    result.update(status="absent", candidate_count=len(candidates))
    for path in candidates:
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual == expected:
            trial = path.parents[2]
            reward_path = trial / "result.json"
            actual_reward = _read_reward(reward_path)
            verifier_diffs = _verifier_diffs(cell, trial / "verifier")
            if verifier_diffs:
                status = "verifier-mismatch"
            elif actual_reward != result["expected_reward"]:
                status = "reward-mismatch"
            else:
                status = "matched"
            result.update(
                status=status,
                path=path,
                actual=actual,
                reward_path=reward_path,
                actual_reward=actual_reward,
                verifier_path=trial / "verifier",
                verifier_diffs=verifier_diffs,
            )
            return result
        if result["status"] == "absent":
            result.update(status="mismatched", path=path, actual=actual)
    return result


def read_subset_evidence(cells_dir: Path) -> list[dict[str, Any]]:
    """Every ``tasks[].subsets[]`` entry of the two graded-subset cell files."""
    entries: list[dict[str, Any]] = []
    for name in SUBSET_EVIDENCE_FILES:
        path = cells_dir / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SUBSET_CELLS_SCHEMA:
            raise ProgressError(f"{name}: unexpected schema {data.get('schema')!r}")
        for task in data.get("tasks", []):
            for subset in task.get("subsets", []):
                entries.append({**subset, "task_id": task["task_id"], "_source_file": name})
    return entries


def _subset_card(job_name: str) -> str:
    """The upper-case card prefix of a subset job name (``har-284-...`` -> ``HAR-284``)."""
    return "-".join(job_name.split("-")[:2]).upper()


def subset_candidates(entry: Mapping[str, Any], results_root: Path) -> list[Path]:
    """Published then raw ``result.json`` paths for one subset entry, without repeats."""
    job_name = str(entry["job_name"])
    trial = str(entry["trial_name"])
    card = _subset_card(job_name)
    suffix = Path(trial) / "result.json"
    dates = sorted(path for path in results_root.expanduser().glob("*") if path.is_dir())
    candidates = [date / f"{card}-{job_name}" / suffix for date in dates]
    candidates.extend(
        date / f"{card.lower()}-graded-subset" / job_name / suffix for date in dates
    )
    return list(dict.fromkeys(candidates))


def verify_subset(entry: Mapping[str, Any], results_root: Path) -> dict[str, Any]:
    """Compare one subset entry's recorded reward with its job's ``result.json``."""
    job_name = str(entry.get("job_name") or "")
    trial = entry.get("trial_name")
    result: dict[str, Any] = {
        "entry": entry,
        "card": _subset_card(job_name) if job_name else "",
        "status": "no-trial",
        "path": None,
        "expected": entry.get("reward"),
        "actual": None,
        "candidate_count": 0,
    }
    if not trial:
        return result
    candidates = subset_candidates(entry, results_root)
    result.update(status="absent", candidate_count=len(candidates))
    for path in candidates:
        if not path.is_file():
            continue
        actual = _read_reward(path)
        if actual == result["expected"]:
            result.update(status="matched", path=path, actual=actual)
            return result
        if result["status"] == "absent":
            result.update(status="reward-mismatch", path=path, actual=actual)
    return result


def verify_subsets(
    entries: Sequence[Mapping[str, Any]], results_root: Path
) -> list[dict[str, Any]]:
    """Check every subset entry in file, task, and subset order."""
    return [verify_subset(entry, results_root) for entry in entries]


def verify_evidence(cells: Sequence[dict[str, Any]], results_root: Path) -> list[dict[str, Any]]:
    """Check every coverage cell, in rank and task order, without reading the ranking."""
    ordered = sorted(cells, key=lambda cell: (int(cell["ranking"]["rank"]), str(cell["task_id"])))
    return [verify_cell(cell, str(cell.get("_card") or ""), results_root) for cell in ordered]


def render_evidence(
    checks: Sequence[dict[str, Any]], subset_checks: Sequence[dict[str, Any]] = ()
) -> str:
    """One stable line per coverage cell and subset entry, then each totals line."""
    lines = []
    for result in checks:
        cell = result["cell"]
        status = result["status"]
        prefix = (
            f"{status:<{STATUS_WIDTH}} {cell['task_id']} {result['card']} "
            f"{cell['job_name']}/{cell['trial_name']}"
        )
        if status == "unhashed":
            detail = "cell records no attempts_json_sha256"
        elif status == "absent":
            detail = f"no attempts.json among {result['candidate_count']} candidate paths"
        elif status == "reward-mismatch":
            detail = (
                f"{result['reward_path']} expected reward {result['expected_reward']!r} "
                f"got {result['actual_reward']!r}"
            )
        elif status == "verifier-mismatch":
            detail = f"{result['verifier_path']}: " + "; ".join(result["verifier_diffs"])
        else:
            detail = str(result["path"])
            if status == "mismatched":
                detail += f" expected {result['expected'][:12]} got {result['actual'][:12]}"
        lines.append(f"{prefix} {detail}")
    counts = Counter(result["status"] for result in checks)
    totals = ", ".join(f"{status}={counts[status]}" for status in EVIDENCE_STATUSES)
    lines.append(f"totals: {len(checks)} cells, {totals}")
    if subset_checks:
        for result in subset_checks:
            entry = result["entry"]
            status = result["status"]
            trial = entry.get("trial_name") or "-"
            prefix = (
                f"{status:<{STATUS_WIDTH}} subset {result['card']} "
                f"{entry['job_name']}/{trial}"
            )
            if status == "no-trial":
                detail = "entry records no trial_name"
            elif status == "absent":
                detail = f"no result.json among {result['candidate_count']} candidate paths"
            elif status == "reward-mismatch":
                detail = (
                    f"{result['path']} expected reward {result['expected']!r} "
                    f"got {result['actual']!r}"
                )
            else:
                detail = str(result["path"])
            lines.append(f"{prefix} {detail}")
        subset_counts = Counter(result["status"] for result in subset_checks)
        totals = ", ".join(
            f"{status}={subset_counts[status]}" for status in SUBSET_EVIDENCE_STATUSES
        )
        lines.append(f"subset totals: {len(subset_checks)} entries, {totals}")
    return "\n".join(lines) + "\n"


def check(rebuilt: str, target: Path) -> bool:
    """Compare ``rebuilt`` against the committed file; True when it matches."""
    committed = target.read_text(encoding="utf-8") if target.is_file() else ""
    if committed == rebuilt:
        print(f"{OUTPUT_NAME}: reproduces from committed cells")
        return True
    print(f"{OUTPUT_NAME}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{OUTPUT_NAME} (committed)",
        tofile=f"{OUTPUT_NAME} (rebuilt)",
        lineterm="",
    )
    for line in list(diff)[:40]:
        print(line, file=sys.stderr)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help=f"exit 1 if the committed {OUTPUT_NAME} differs from a rebuild",
    )
    mode.add_argument(
        "--summary",
        action="store_true",
        help="print wall time by language and the serial hours left for uncovered rows",
    )
    mode.add_argument(
        "--pending-controls",
        action="store_true",
        help="print pending control cells by ranking phase and their serial hours",
    )
    mode.add_argument(
        "--verify-evidence",
        action="store_true",
        help="re-hash coverage cells' host attempts.json evidence",
    )
    mode.add_argument(
        "--render-slice",
        metavar="CELL",
        type=Path,
        help="print the README pipe-table rows for one ladder cell file",
    )
    mode.add_argument(
        "--sync-docs",
        action="store_true",
        help="refresh the ten tested documentation fragments from committed cells (offline)",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("~/Developer/eval-lab-results"),
        help="host results directory (only read by --verify-evidence)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="with --verify-evidence, also exit 1 for absent cell or subset evidence",
    )
    args = parser.parse_args(argv)
    if args.strict and not args.verify_evidence:
        parser.error("--strict requires --verify-evidence")
    if args.render_slice is not None:
        path = args.render_slice
        if not path.is_absolute() and not path.is_file():
            path = HERE / path
        print(render_slice(path), end="")
        return 0
    if args.pending_controls:
        controls = read_pending_controls(HERE / PENDING_NAME)
        print(render_pending_controls(controls, read_ranking(ROOT)), end="")
        return 0
    if args.verify_evidence:
        cells = read_cells(HERE, ROOT, manifest=read_manifest(HERE))
        checks = verify_evidence(cells, args.results_root)
        subset_checks = verify_subsets(read_subset_evidence(HERE), args.results_root)
        print(render_evidence(checks, subset_checks), end="")
        counts = Counter(result["status"] for result in checks)
        subset_counts = Counter(result["status"] for result in subset_checks)
        failed = (
            counts["mismatched"] > 0
            or counts["reward-mismatch"] > 0
            or counts["verifier-mismatch"] > 0
            or subset_counts["reward-mismatch"] > 0
            or (args.strict and (counts["absent"] > 0 or subset_counts["absent"] > 0))
        )
        if failed:
            print(
                f"{counts['mismatched']} mismatched, "
                f"{counts['reward-mismatch']} reward-mismatch, "
                f"{counts['verifier-mismatch']} verifier-mismatch, "
                f"{counts['absent']} absent, "
                f"{subset_counts['reward-mismatch']} subset reward-mismatch, "
                f"{subset_counts['absent']} subset absent",
                file=sys.stderr,
            )
        return int(failed)
    rows = build(ROOT, cells_dir=HERE)
    if args.summary:
        print(render_summary(rows), end="")
        return 0
    if args.sync_docs:
        sync_docs(rows, root=ROOT, cells_dir=HERE)
        return 0
    if args.check:
        return 0 if check(render(rows), HERE / OUTPUT_NAME) else 1
    (HERE / OUTPUT_NAME).write_text(render(rows), encoding="utf-8")
    counts = ", ".join(f"{name}={count}" for name, count in sorted(basis_counts(rows).items()))
    print(f"wrote {OUTPUT_NAME}: {len(rows)} rows ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
