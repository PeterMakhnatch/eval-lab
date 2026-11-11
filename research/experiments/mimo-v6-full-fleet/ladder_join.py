"""Join the ranked v6 ladder cells onto the 567-task fleet census (HAR-372).

``fleet-ladder-join.csv`` answers one question per fleet task: does a ranked
cheat-ladder cell exist, and did that cell run the fleet's own package bytes?

Inputs, all committed:

- ``mimo-v6-fleet-census.csv``: the fleet release's 567 current packages. Its
  ``v6_package_digest`` is the fleet digest; this builder refuses a census row
  whose digest disagrees with ``manifest.csv``'s ``final_digest``.
- ``../mimo-clean-v6/ladder-progress.csv``: the 100 ranked ladder cells. It
  names each cell's source ledger in ``source_file`` but records no digest, so
  the digest is read from that ledger's task entry (``final_digest``) and must
  equal ``../mimo-clean-v6/manifest.csv``'s ``final_digest`` for the task, which
  HAR-312 pins equal.

A manifest that repeats a task is refused, because a dict built from its rows
would let the later row win over the earlier one without a word.

``digest_match`` is ``yes`` when the cell ran the fleet digest, ``no`` when it
ran different bytes, and ``no-cell`` when the task has no ranked cell at all.
A cell is a measurement of whatever bytes it ran; nothing here promotes a cell
verdict into fleet acceptance, and the census buckets are emitted unchanged.

``--sync-docs`` rewrites the two pages that quote the committed join: the
count table, other-bytes sentence and final census row in this directory's
README, and the join sentence and both census sentences (including the
567-task sentence) in ``docs/mimo/README.md``. It refuses a stale join CSV,
and a page whose anchor is missing or repeated raises ``JoinError`` before any
page is written; on a synced tree it rewrites nothing.

Reproduce (repo root, locked environment):

    uv run python research/experiments/mimo-v6-full-fleet/ladder_join.py --check
    uv run python research/experiments/mimo-v6-full-fleet/ladder_join.py --sync-docs
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

FLEET_DIR = "research/experiments/mimo-v6-full-fleet"
CLEAN_DIR = "research/experiments/mimo-clean-v6"
CENSUS = f"{FLEET_DIR}/mimo-v6-fleet-census.csv"
FLEET_MANIFEST = f"{FLEET_DIR}/manifest.csv"
PROGRESS = f"{CLEAN_DIR}/ladder-progress.csv"
CLEAN_MANIFEST = f"{CLEAN_DIR}/manifest.csv"
JOIN_NAME = "fleet-ladder-join.csv"
FLEET_README = f"{FLEET_DIR}/README.md"
ROUTE_PAGE = "docs/mimo/README.md"

#: The fleet README section that owns the count table and other-bytes sentence.
COUNTS_HEADING = "## Ranked ladder cells against fleet digests"
TABLE_HEADER = (
    "| Bucket | Fleet tasks | Have a ranked cell | Cell ran the fleet digest |\n"
    "|---|---:|---:|---:|\n"
)
TABLE = re.escape(TABLE_HEADER) + r"(?:\|[^\n]*\|\n)+"
OTHER_BYTES = (
    r"Of\s+the\s+([0-9][0-9,]*)\s+fleet\s+tasks\s+with\s+a\s+ranked\s+cell,\s+"
    r"([0-9][0-9,]*)\s+ran\s+the\s+fleet\s+digest\s+and\s+([0-9][0-9,]*)\s+-\s+"
    r"([0-9][0-9,]*)\s+=\s+([0-9][0-9,]*)\s+ran\s+other\s+bytes\."
)
ROUTE_SENTENCE = (
    r"holds\s+([0-9][0-9,]*)\s+fleet\s+tasks,\s+([0-9][0-9,]*)\s+with\s+a\s+ranked\s+"
    r"cell,\s+and\s+([0-9][0-9,]*)\s+whose\s+cell\s+ran\s+the\s+fleet\s+digest;\s+"
    r"([0-9][0-9,]*)\s+have\s+no\s+ranked\s+cell\s+and\s+([0-9][0-9,]*)\s+cells\s+"
    r"ran\s+other\s+bytes"
)

#: The census buckets the release pages quote; any other bucket is a contract break.
CENSUS_BUCKETS = ("v6_verified", "v6_unverified", "broken_ref_or_env", "missing_ref")

ROUTE_CENSUS = (
    r"Its\s+fleet\s+census\s+\(`mimo-v6-fleet-census\.csv`\)\s+is\s+"
    r"([0-9][0-9,]*)\s+`v6_verified`\s+and\s+([0-9][0-9,]*)\s+`v6_unverified`"
)
ROUTE_V6_FLEET = (
    r"For\s+the\s+([0-9][0-9,]*)-task\s+v6\s+fleet\s+only\s+"
    r"([0-9][0-9,]*)\s+are\s+`v6_verified`"
)
CENSUS_ROW = (
    r"^\|\s*Final reviewed release, after exact-source controls\s*\|\s*"
    r"([0-9][0-9,]*)\s*\|\s*([0-9][0-9,]*)\s*\|\s*([0-9][0-9,]*)\s*\|\s*"
    r"([0-9][0-9,]*)\s*\|$"
)

COLUMNS = (
    "task_id",
    "language",
    "bucket",
    "fleet_digest",
    "ladder_rank",
    "ladder_verdict",
    "ladder_basis",
    "ladder_digest",
    "digest_match",
)

#: ``digest_match`` values; a task without a cell is not a stale measurement.
MATCH_YES = "yes"
MATCH_NO = "no"
MATCH_NONE = "no-cell"


class JoinError(RuntimeError):
    """An input no longer matches the contract this builder reads."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise JoinError(f"input is missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise JoinError(f"input is empty: {path}")
    return rows


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise JoinError(f"cell ledger is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(path: Path) -> dict[str, dict[str, str]]:
    """Index manifest rows by task; a repeat would silently win over an earlier row."""
    index: dict[str, dict[str, str]] = {}
    for row in _read_csv(path):
        task = row["task_id"]
        if task in index:
            raise JoinError(f"{path} repeats {task}")
        index[task] = row
    return index


def fleet_rows(root: Path = ROOT) -> list[dict[str, str]]:
    """The census rows, with each fleet digest checked against the manifest."""
    census = _read_csv(root / CENSUS)
    manifest = _manifest(root / FLEET_MANIFEST)
    if len({row["task_id"] for row in census}) != len(census):
        raise JoinError(f"{CENSUS} repeats a task_id")
    for row in census:
        task = row["task_id"]
        described = manifest.get(task)
        if described is None:
            raise JoinError(f"{task} is in the census but not in {FLEET_MANIFEST}")
        if row["v6_package_digest"] != described["final_digest"]:
            raise JoinError(
                f"{task} census digest {row['v6_package_digest']} "
                f"disagrees with manifest {described['final_digest']}"
            )
    return [
        {
            "task_id": row["task_id"],
            "language": row["language"],
            "bucket": row["bucket"],
            "fleet_digest": row["v6_package_digest"],
        }
        for row in census
    ]


def ladder_cells(root: Path = ROOT) -> dict[str, dict[str, str]]:
    """Every ranked cell, keyed by task, with the digest it actually ran."""
    progress = _read_csv(root / PROGRESS)
    manifest = _manifest(root / CLEAN_MANIFEST)
    if len({row["task_id"] for row in progress}) != len(progress):
        raise JoinError(f"{PROGRESS} ranks a task twice")
    ranks = [int(row["rank"]) for row in progress]
    if len(set(ranks)) != len(ranks):
        raise JoinError(f"{PROGRESS} repeats a rank")
    cells: dict[str, dict[str, str]] = {}
    for row in progress:
        task = row["task_id"]
        ledger = _read_json(root / row["source_file"])
        if ledger.get("manifest") != CLEAN_MANIFEST:
            raise JoinError(
                f"{row['source_file']} is bound to {ledger.get('manifest')!r}, not {CLEAN_MANIFEST}"
            )
        entries = [entry for entry in ledger.get("tasks", []) if entry.get("task_id") == task]
        if len(entries) != 1:
            raise JoinError(f"{row['source_file']} has {len(entries)} entries for {task}")
        cell = entries[0]
        digest = str(cell.get("final_digest") or "")
        if not digest:
            raise JoinError(f"{task} cell in {row['source_file']} records no digest")
        if cell.get("verdict") != row["verdict"]:
            raise JoinError(
                f"{task} ledger verdict {cell.get('verdict')!r} "
                f"disagrees with {PROGRESS} {row['verdict']!r}"
            )
        expected = manifest.get(task)
        if expected is None:
            raise JoinError(f"{task} is ranked but absent from {CLEAN_MANIFEST}")
        if digest != expected["final_digest"]:
            raise JoinError(
                f"{task} cell digest {digest} disagrees with "
                f"{CLEAN_MANIFEST} {expected['final_digest']}"
            )
        cells[task] = {
            "ladder_rank": row["rank"],
            "ladder_verdict": row["verdict"],
            "ladder_basis": row["basis"],
            "ladder_digest": digest,
        }
    return cells


def assemble(
    fleet: Sequence[dict[str, str]], cells: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """One row per fleet task, in task-id order, with the cell when one exists."""
    rows: list[dict[str, str]] = []
    for task in fleet:
        cell = cells.get(task["task_id"])
        if cell is None:
            rank = verdict = basis = digest = ""
            match = MATCH_NONE
        else:
            rank = cell["ladder_rank"]
            verdict = cell["ladder_verdict"]
            basis = cell["ladder_basis"]
            digest = cell["ladder_digest"]
            match = MATCH_YES if digest == task["fleet_digest"] else MATCH_NO
        rows.append(
            {
                "task_id": task["task_id"],
                "language": task["language"],
                "bucket": task["bucket"],
                "fleet_digest": task["fleet_digest"],
                "ladder_rank": rank,
                "ladder_verdict": verdict,
                "ladder_basis": basis,
                "ladder_digest": digest,
                "digest_match": match,
            }
        )
    return rows


def bucket_counts(rows: Sequence[dict[str, str]]) -> list[tuple[str, int, int, int]]:
    """(bucket, fleet tasks, tasks with a cell, cells at the fleet digest), in order."""
    buckets = sorted({row["bucket"] for row in rows})
    counts: list[tuple[str, int, int, int]] = []
    for bucket in [*buckets, "all"]:
        selected = rows if bucket == "all" else [row for row in rows if row["bucket"] == bucket]
        counts.append(
            (
                bucket,
                len(selected),
                sum(1 for row in selected if row["ladder_rank"]),
                sum(1 for row in selected if row["digest_match"] == MATCH_YES),
            )
        )
    return counts


def census_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    """Fleet tasks per census bucket; any other bucket is refused."""
    counts = dict.fromkeys(CENSUS_BUCKETS, 0)
    for row in rows:
        bucket = row["bucket"]
        if bucket not in counts:
            raise JoinError(f"join bucket {bucket!r} is not one of {CENSUS_BUCKETS}")
        counts[bucket] += 1
    return counts


def build(root: Path = ROOT) -> list[dict[str, str]]:
    return assemble(fleet_rows(root), ladder_cells(root))


def render(rows: Sequence[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(COLUMNS), extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _check(name: str, rebuilt: str) -> bool:
    """Compare ``rebuilt`` against the committed file's bytes; True when equal."""
    target = HERE / name
    committed = target.read_bytes() if target.is_file() else b""
    if committed == rebuilt.encode("utf-8"):
        print(f"{name}: reproduces from committed evidence")
        return True
    print(f"{name}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.decode("utf-8", "replace").splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{name} (committed)",
        tofile=f"{name} (rebuilt)",
        lineterm="",
    )
    for line in list(diff)[:40]:
        print(line, file=sys.stderr)
    return False


def _read_page(path: Path) -> str:
    if not path.is_file():
        raise JoinError(f"documentation page is missing: {path}")
    return path.read_bytes().decode("utf-8")


def _anchor(pattern: str, text: str, page: Path, anchor: str) -> re.Match[str]:
    """The single match of ``pattern``; a missing or repeated anchor is refused."""
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if len(matches) != 1:
        raise JoinError(f"{page}: expected one {anchor!r}, found {len(matches)}")
    return matches[0]


def _patch(text: str, edits: Sequence[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


Edit = tuple[int, int, str]


def _group_edits(match: re.Match[str], values: Sequence[int], offset: int = 0) -> list[Edit]:
    return [
        (offset + match.start(index), offset + match.end(index), f"{value:,}")
        for index, value in enumerate(values, start=1)
    ]


def render_table(counts: Sequence[tuple[str, int, int, int]]) -> str:
    """The fleet README count table, one row per bucket and a final all-fleet row."""
    lines = [
        f"| {'All fleet tasks' if bucket == 'all' else f'`{bucket}`'} "
        f"| {tasks} | {with_cell} | {at_digest} |\n"
        for bucket, tasks, with_cell, at_digest in counts
    ]
    return TABLE_HEADER + "".join(lines)


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` through a sibling temporary file."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def sync_docs(rows: Sequence[dict[str, str]], root: Path = ROOT) -> list[Path]:
    """Rewrite the join and census counts; check every anchor before any write.

    Returns the pages whose bytes changed. A synced tree changes nothing.
    """
    counts = bucket_counts(rows)
    _, tasks, with_cell, at_digest = next(entry for entry in counts if entry[0] == "all")
    other = with_cell - at_digest
    census = census_counts(rows)
    fleet_page = root / FLEET_README
    route_page = root / ROUTE_PAGE
    originals = {page: _read_page(page) for page in (fleet_page, route_page)}

    text = originals[fleet_page]
    heading = _anchor(f"^{re.escape(COUNTS_HEADING)}$", text, fleet_page, COUNTS_HEADING)
    following = re.search(r"(?m)^#{1,6}[ \t]", text[heading.end() :])
    start = heading.end()
    section = text[start : start + following.start()] if following else text[start:]
    # The table and the sentence are matched inside the section, but each must also be
    # unique on the whole page: TABLE alone would read a second header right below the
    # first as rows of one table, and a second sentence outside the section would keep
    # its stale counts beside the rewritten ones.
    table_anchor = f"{COUNTS_HEADING} count table"
    _anchor(re.escape(TABLE_HEADER), text, fleet_page, table_anchor)
    table = _anchor(TABLE, section, fleet_page, table_anchor)
    _anchor(OTHER_BYTES, text, fleet_page, "other-bytes sentence")
    sentence = _anchor(OTHER_BYTES, section, fleet_page, f"{COUNTS_HEADING} other-bytes sentence")
    census_row = _anchor(CENSUS_ROW, text, fleet_page, "final census row")
    fleet_text = _patch(
        text,
        [
            (start + table.start(), start + table.end(), render_table(counts)),
            *_group_edits(sentence, (with_cell, at_digest, with_cell, at_digest, other), start),
            *_group_edits(
                census_row, tuple(census[bucket] for bucket in CENSUS_BUCKETS)
            ),
        ],
    )

    text = originals[route_page]
    route = _anchor(ROUTE_SENTENCE, text, route_page, "fleet/ladder join sentence")
    census_sentence = _anchor(ROUTE_CENSUS, text, route_page, "fleet census sentence")
    v6_fleet = _anchor(ROUTE_V6_FLEET, text, route_page, "v6 fleet census sentence")
    route_text = _patch(
        text,
        [
            *_group_edits(route, (tasks, with_cell, at_digest, tasks - with_cell, other)),
            *_group_edits(
                census_sentence, (census["v6_verified"], census["v6_unverified"])
            ),
            *_group_edits(v6_fleet, (tasks, census["v6_verified"])),
        ],
    )

    changed: list[Path] = []
    for page, replacement in ((fleet_page, fleet_text), (route_page, route_text)):
        if replacement != originals[page]:
            _write_atomic(page, replacement.encode("utf-8"))
            changed.append(page)
    return changed


def _mode_label(args: argparse.Namespace) -> str:
    """The mode a refused run names in its one-line stderr report."""
    if args.check:
        return "--check"
    if args.sync_docs:
        return "--sync-docs"
    return "build"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help=f"exit 1 if the committed {JOIN_NAME} differs",
    )
    mode.add_argument(
        "--sync-docs",
        action="store_true",
        help=(
            f"rewrite the join and census counts in {FLEET_README} and {ROUTE_PAGE} from the "
            f"committed {JOIN_NAME}; exit 1 if that file is stale"
        ),
    )
    args = parser.parse_args(argv)
    try:
        rows = build()
        rebuilt = render(rows)
        if args.check:
            return 0 if _check(JOIN_NAME, rebuilt) else 1
        if args.sync_docs:
            if not _check(JOIN_NAME, rebuilt):
                return 1
            changed = sync_docs(_read_csv(HERE / JOIN_NAME), ROOT)
            names = ", ".join(str(page.relative_to(ROOT)) for page in changed) or "nothing"
            print(f"sync-docs rewrote {names}")
            return 0
        (HERE / JOIN_NAME).write_bytes(rebuilt.encode("utf-8"))
        print(f"wrote {JOIN_NAME}: {len(rows)} rows")
        return 0
    except JoinError as error:
        print(f"{_mode_label(args)}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
