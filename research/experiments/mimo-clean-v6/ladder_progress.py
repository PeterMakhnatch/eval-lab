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
``matched``, ``mismatched``, ``absent``, or ``unhashed``. Mismatches exit 1;
absences exit 1 only with ``--strict``; unhashed cells never change the exit
code. This mode reads the host results root and is deliberately not part of
``--check``.

Pure stdlib; no Docker. Regenerate with ``python ladder_progress.py``; verify
the committed table with ``--check`` (exit 1 on drift); print the language
summary with ``--summary``. ``check_slice_tables`` compares the README's
per-slice ladder tables with the committed rows; the test suite calls it.
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
REFUSAL_BASIS = "tamper-refusal"
GRADED_BASIS = "graded"
SECONDS_PER_HOUR = 3600.0
SLICE_MARKER = " ladder cells"
SLICE_WALL = re.compile(r"(?P<minutes>[0-9]+(?:\.[0-9]+)?)\s*min\b")
SLICE_WALL_TOLERANCE = 0.05
SLICE_COLUMNS = ("rank", "task", "verdict", "wall")
SLICE_ATTACK_COLUMNS = ("attacks executed", "attacks")
VERDICT_TOTALS = ("clean", "cracked", "unscored")
TOTALS_FIELDS = ("tasks", *VERDICT_TOTALS)
EVIDENCE_STATUSES = ("matched", "mismatched", "absent", "unhashed")


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


def _pipe_cells(line: str) -> list[str]:
    """The stripped cells of a Markdown pipe-table line."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


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


def verify_cell(cell: Mapping[str, Any], card: str, results_root: Path) -> dict[str, Any]:
    """Compare a cell's recorded hash with every candidate until one matches."""
    result: dict[str, Any] = {
        "cell": cell,
        "card": str(cell.get("card", card) or ""),
        "status": "unhashed",
        "path": None,
        "expected": cell.get("attempts_json_sha256"),
        "actual": None,
        "candidate_count": 0,
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
            result.update(status="matched", path=path, actual=actual)
            return result
        if result["status"] == "absent":
            result.update(status="mismatched", path=path, actual=actual)
    return result


def verify_evidence(cells: Sequence[dict[str, Any]], results_root: Path) -> list[dict[str, Any]]:
    """Check every coverage cell, in rank and task order, without reading the ranking."""
    ordered = sorted(cells, key=lambda cell: (int(cell["ranking"]["rank"]), str(cell["task_id"])))
    return [verify_cell(cell, str(cell.get("_card") or ""), results_root) for cell in ordered]


def render_evidence(checks: Sequence[dict[str, Any]]) -> str:
    """One stable line per cell followed by all four status counts."""
    lines = []
    for result in checks:
        cell = result["cell"]
        status = result["status"]
        prefix = (
            f"{status:<10} {cell['task_id']} {result['card']} "
            f"{cell['job_name']}/{cell['trial_name']}"
        )
        if status == "unhashed":
            detail = "cell records no attempts_json_sha256"
        elif status == "absent":
            detail = f"no attempts.json among {result['candidate_count']} candidate paths"
        else:
            detail = str(result["path"])
            if status == "mismatched":
                detail += f" expected {result['expected'][:12]} got {result['actual'][:12]}"
        lines.append(f"{prefix} {detail}")
    counts = Counter(result["status"] for result in checks)
    totals = ", ".join(f"{status}={counts[status]}" for status in EVIDENCE_STATUSES)
    lines.append(f"totals: {len(checks)} cells, {totals}")
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
        "--verify-evidence",
        action="store_true",
        help="re-hash coverage cells' host attempts.json evidence",
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
        help="with --verify-evidence, also exit 1 for absent evidence",
    )
    args = parser.parse_args(argv)
    if args.strict and not args.verify_evidence:
        parser.error("--strict requires --verify-evidence")
    if args.verify_evidence:
        cells = read_cells(HERE, ROOT, manifest=read_manifest(HERE))
        checks = verify_evidence(cells, args.results_root)
        print(render_evidence(checks), end="")
        counts = Counter(result["status"] for result in checks)
        failed = counts["mismatched"] > 0 or (args.strict and counts["absent"] > 0)
        if failed:
            print(f"{counts['mismatched']} mismatched, {counts['absent']} absent", file=sys.stderr)
        return int(failed)
    rows = build(ROOT, cells_dir=HERE)
    if args.summary:
        print(render_summary(rows), end="")
        return 0
    if args.check:
        return 0 if check(render(rows), HERE / OUTPUT_NAME) else 1
    (HERE / OUTPUT_NAME).write_text(render(rows), encoding="utf-8")
    counts = ", ".join(f"{name}={count}" for name, count in sorted(basis_counts(rows).items()))
    print(f"wrote {OUTPUT_NAME}: {len(rows)} rows ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
