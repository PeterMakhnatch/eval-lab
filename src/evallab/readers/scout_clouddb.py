"""Blinded DEV DB/ATIF conversations through Inspect Scout 0.5.4, entirely local.

Run with ``uv run --with inspect-scout==0.5.4 python -m
 evallab.readers.scout_clouddb run``. This synchronizes both admitted cloud-DB
DEV traces and the independently assigned CheatBench DEV full trajectories,
scans them, and appends normalized results. No model, upload, or Docker is used.

Only four existing text scanners are supported. The six ``rule_*`` scanners
in research/explorations/trace-lab/scout/scanners.py require
``metadata['trial_dir']`` and are deliberately excluded: DB transcripts have
neither a Harbor trial directory nor identity-bearing transcript metadata.
``restored_tool_calls`` measures representation visibility, not misconduct;
its flags are always null. Publisher labels never enter scanner inputs.

The private key map is append-only and kept outside the Scout transcript DBs.
``sync`` inserts only ids absent from that map; content/reasoning are unbounded.
Each trace is projected on its own: one whose source retains no actor chronology
(``ActorHistoryUnavailable``) is quarantined and reported by run id and reason.
It is never inserted as an empty transcript, never keyed, and never counted as a
clean or scanned run; the other traces in its batch are unaffected. The sync
report keeps full admission (``eligible``) separate from ``inserted``,
``unchanged`` and ``unavailable``.

Every transcript, key-map row, result row and default cache/scan/state path is
versioned by ``ACTOR_PROJECTION_VERSION``. An unversioned transcript cache, key
map or result ledger is refused with ``StaleProjectionError`` and never extended
in place; a key map or transcript cache of another projection is refused too.
Earlier-version result rows stay as retained lineage: they are never counted as
current coverage and never rewritten.

``scan`` accepts only the four deterministic scanners; ``collect`` retains one
record per scanner label (nine records per run with the default scanner set).
Scheduled callers can pass a nonempty ``transcript_ids`` subset to ``scan``;
unselected transcript content is not loaded again.

An unchanged sweep must stay cheap. ``_insert`` opens the Parquet transcript
database only once a transcript actually exists, so a sweep whose unkeyed
remainder is entirely unavailable never imports or opens Scout at all. A caller
that already computed this DB snapshot's admission refs passes them to ``sync``,
so one sweep makes one admission decision. The scheduled watcher's
``ledger_coverage`` reuses a coverage index bound to the SHA-256 of the exact
result-ledger bytes: any content change (including a same-length rewrite)
recomputes coverage, and no index is trusted for a ledger holding unversioned
Scout rows. Admission itself is never cached: its authoritative inputs are the
whole task-identity and split-registry set, and a content digest of those inputs
costs as much as re-deciding admission.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import itertools
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, NamedTuple

from evallab.readers import ACTOR_PROJECTION_VERSION
from evallab.readers.clouddb_blind import (
    ActorHistoryUnavailable,
    TraceRef,
    atif_blind_messages,
    blind_messages,
    dev_trace_refs,
    opaque_id,
)

SCOUT_VERSION = "0.5.4"
BLIND_SCHEMA = "evallab.blind_run/v1"
SCANNERS = (
    "repeated_assistant_message",
    "harness_parse_errors",
    "native_tool_call_markup",
    "restored_tool_calls",
)
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_ROOT = Path.home() / ".local/state/evallab-prod-live"
DEFAULT_DB = STATE_ROOT / "cloud-trace-db.sqlite"
#: Shared trace-tool state. Unversioned scout files written here before the
#: native actor projection are retained as read-only evidence; nothing reads or
#: appends to them any more.
TRACE_TOOLS_STATE = STATE_ROOT / "trace-tools"
_VIEWER_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/scout")
PROJECTION_MARKER = "projection.json"
_MESSAGE_STEP = re.compile(r"^step-(-?\d+)-message-\d+$")


class StaleProjectionError(ValueError):
    """A cache or ledger belongs to another actor projection and is never reused."""


class ScoutPaths(NamedTuple):
    """Every location one actor projection reads or writes."""

    state: Path
    keymap: Path
    results: Path
    transcripts: Path
    cheatbench_transcripts: Path
    scans: Path


def scout_paths(
    trace_tools: Path, viewer: Path, projection_version: str = ACTOR_PROJECTION_VERSION,
) -> ScoutPaths:
    """Version-suffixed siblings of the legacy paths.

    Scout discovers Parquet files recursively, so a versioned transcript cache
    must never be nested inside (or contain) another projection's cache.
    """
    state = trace_tools / f"scout-{projection_version}"
    return ScoutPaths(
        state=state,
        keymap=state / "keymap-v1.jsonl",
        results=state / "results-v1.jsonl",
        transcripts=viewer / "data" / f"transcripts_clouddb_dev-{projection_version}",
        cheatbench_transcripts=(
            viewer / "data" / f"transcripts_cheatbench_dev_blind-{projection_version}"
        ),
        scans=viewer / "scans" / f"trace-tools-{projection_version}",
    )


DEFAULT_PATHS = scout_paths(TRACE_TOOLS_STATE, _VIEWER_ROOT)
DEFAULT_STATE = DEFAULT_PATHS.state
DEFAULT_TRANSCRIPTS = DEFAULT_PATHS.transcripts
DEFAULT_CHEATBENCH_TRANSCRIPTS = DEFAULT_PATHS.cheatbench_transcripts
DEFAULT_SCANS = DEFAULT_PATHS.scans
DEFAULT_KEYMAP = DEFAULT_PATHS.keymap
DEFAULT_RESULTS = DEFAULT_PATHS.results


def _require_scout() -> None:
    if version("inspect-scout") != SCOUT_VERSION:
        raise RuntimeError(f"This adapter requires inspect-scout=={SCOUT_VERSION}")


def to_transcript(opaque: str, messages: Sequence[dict[str, Any]]) -> Any:
    """Construct concrete Inspect messages with only blinded identity/metadata."""
    _require_scout()
    from inspect_ai.model import (  # ty: ignore[unresolved-import]
        ChatMessageAssistant,
        ChatMessageSystem,
        ChatMessageTool,
        ChatMessageUser,
        ContentReasoning,
        ContentText,
    )
    from inspect_ai.tool import ToolCall  # ty: ignore[unresolved-import]
    from inspect_scout import Transcript  # ty: ignore[unresolved-import]

    projected = []
    classes = {"system": ChatMessageSystem, "user": ChatMessageUser, "tool": ChatMessageTool}
    for position, message in enumerate(messages, 1):
        index = message["step_index"]
        content: Any = message["content"]
        if message["reasoning"] is not None:
            content = [
                ContentReasoning(reasoning=message["reasoning"]),
                ContentText(text=message["content"]),
            ]
        args: dict[str, Any] = {
            "id": f"step-{index}-message-{position}",
            "content": content,
            "metadata": {"step_index": index},
        }
        role = message["role"]
        if role == "assistant":
            args["tool_calls"] = [ToolCall(**call) for call in message["tool_calls"]] or None
            projected.append(ChatMessageAssistant(**args))
        else:
            if role == "tool":
                args["tool_call_id"] = message["tool_call_id"]
            projected.append(classes[role](**args))
    return Transcript(
        transcript_id=opaque,
        messages=projected,
        metadata={"blind_schema": BLIND_SCHEMA, "projection_version": ACTOR_PROJECTION_VERSION},
    )


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def read_keymap(path: str | Path) -> dict[str, dict[str, Any]]:
    """Return the private key map, refusing rows written for another projection."""
    path = Path(path)
    keymap: dict[str, dict[str, Any]] = {}
    for row in _jsonl(path):
        if row.get("projection_version") != ACTOR_PROJECTION_VERSION:
            raise StaleProjectionError(
                f"{path} holds key-map rows outside projection {ACTOR_PROJECTION_VERSION}; "
                "it is preserved unchanged, use a projection-versioned path",
            )
        keymap[row["opaque_id"]] = row
    return keymap


def versioned_rows(
    rows: Iterable[Mapping[str, Any]], path: Path,
) -> Iterator[Mapping[str, Any]]:
    """Yield ledger rows, refusing an unversioned Scout row as it is reached.

    Rows that name an earlier projection are retained lineage: they are appended
    evidence, never counted as current coverage and never rewritten. An
    unversioned Scout row predates the projection contract, so the ledger must
    not be extended in place.
    """
    for row in rows:
        if row.get("tool") == "scout" and "projection_version" not in row:
            raise StaleProjectionError(
                f"{path} holds unversioned Scout results; it is preserved unchanged, "
                "use a projection-versioned path",
            )
        yield row


def require_current_results(path: str | Path) -> None:
    """Refuse a results ledger holding Scout rows without a projection version.

    Rows that name an earlier projection are retained lineage: they are appended
    evidence, never counted as current coverage (``completed_runs`` requires an
    exact version match), and never rewritten. An unversioned Scout row predates
    the projection contract, so the ledger must not be extended in place.
    """
    path = Path(path)
    for _ in versioned_rows(_jsonl(path), path):
        pass


def _check_transcripts(directory: Path, *, claim: bool = False) -> None:
    """Refuse a transcript cache of another or no projection; optionally claim a new one."""
    marker = directory / PROJECTION_MARKER
    if marker.is_file():
        try:
            claimed = json.loads(marker.read_text(encoding="utf-8")).get("projection_version")
        except (ValueError, AttributeError):
            claimed = None
        if claimed != ACTOR_PROJECTION_VERSION:
            raise StaleProjectionError(
                f"{directory} is a transcript cache for projection {claimed!r}, "
                f"not {ACTOR_PROJECTION_VERSION}; it is preserved unchanged",
            )
        return
    if directory.exists() and next(directory.rglob("*.parquet"), None) is not None:
        raise StaleProjectionError(
            f"{directory} is an unversioned transcript cache; it is preserved unchanged, "
            "use a projection-versioned directory",
        )
    if claim:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"projection_version": ACTOR_PROJECTION_VERSION}) + "\n", encoding="utf-8",
        )
        temporary.replace(marker)


def _scanned_projection(metadata: Any) -> Any:
    """Projection version recorded in a scanned transcript's metadata, if any."""
    metadata = _clean(metadata)
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    return metadata.get("projection_version") if isinstance(metadata, dict) else None


def _cheatbench_items(repo_root: Path) -> list[dict[str, Any]]:
    keys = repo_root / "research/answer-keys"
    with (keys / "splits/assignments.csv").open(encoding="utf-8", newline="") as stream:
        assigned = {
            row["item_id"]
            for row in csv.DictReader(stream)
            if row["key_id"] == "cheatbench-actions-v1" and row["split"] == "dev"
        }
    return [
        row for row in _jsonl(keys / "cheatbench-actions-v1/items.jsonl")
        if row["item_id"] in assigned
    ]


def _cheatbench_messages(item: dict[str, Any]) -> list[dict[str, Any]]:
    ref = item["trial_ref"]
    if ref["kind"] != "published":
        raise ValueError("CheatBench full trajectories must use published trial refs")
    trajectory = Path.home() / "Developer/eval-lab-results" / ref["path"] / "agent/trajectory.json"
    doc = json.loads(trajectory.read_bytes())
    return atif_blind_messages(doc, item_id=item["item_id"])


async def _insert(transcripts_dir: Path, transcripts: Iterable[Any]) -> None:
    """Insert yielded transcripts, opening Scout only once one actually exists.

    A sweep whose unkeyed remainder is entirely unavailable yields nothing.
    Opening the Parquet transcript database (which imports Scout's Arrow stack
    and rewrites its manifest index) for an empty insert is pure cost, so the
    session starts on the first real transcript and stays memory bounded.
    """
    _check_transcripts(transcripts_dir, claim=True)
    iterator = iter(transcripts)
    first = next(iterator, None)
    if first is None:
        return
    from inspect_scout import transcripts_db  # ty: ignore[unresolved-import]

    async with transcripts_db(str(transcripts_dir)) as db:
        await db.insert(itertools.chain([first], iterator))


def admitted_refs(db_path: str | Path, *, repo_root: Path) -> list[TraceRef]:
    """Authoritative admitted DEV refs from one read-only DB snapshot.

    Admission is a function of the stored task identities and the split
    registry, both of which can tighten between sweeps, so it is recomputed on
    every sweep rather than cached. ``sync`` accepts these refs so one sweep
    projects and decides coverage against exactly one admission decision.
    """
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        return dev_trace_refs(con, repo_root=repo_root)


def sync(
    db_path: str | Path,
    transcripts_dir: str | Path,
    keymap_path: str | Path,
    *,
    repo_root: Path,
    cheatbench: bool = False,
    refs: Sequence[TraceRef] | None = None,
) -> dict[str, Any]:
    """Insert unseen opaque ids, then append their private source mappings.

    Scout also deduplicates ids on insert, so retrying an interrupted insertion
    before its key-map append does not duplicate transcripts. The iterable keeps
    the full corpus out of memory; Scout writes its own bounded Parquet batches.

    A trace whose source retains no actor history is quarantined individually
    (``unavailable``/``quarantined``) and retried on the next sync; it is neither
    inserted nor keyed. ``eligible`` is full admission and always equals
    ``unchanged + inserted + unavailable``. A stale key map or transcript cache
    raises ``StaleProjectionError`` before anything is written.

    A caller that already computed this DB snapshot's admission refs passes
    them in, so one sweep makes one admission decision for both import and scan.
    """
    _require_scout()
    keymap_path, transcripts_dir = Path(keymap_path), Path(transcripts_dir)
    keyed = set(read_keymap(keymap_path))
    _check_transcripts(transcripts_dir)
    added: list[dict[str, str]] = []
    unavailable: list[dict[str, Any]] = []
    source = "cheatbench-actions-v1" if cheatbench else "cloud-trace-db"

    def transcripts(rows: Iterable[tuple[str, list[dict[str, Any]]]]) -> Iterator[Any]:
        for run_id, messages in rows:
            opaque = opaque_id(run_id)
            transcript = to_transcript(opaque, messages)
            added.append({
                "opaque_id": opaque, "run_id": run_id, "source": source,
                "projection_version": ACTOR_PROJECTION_VERSION,
            })
            yield transcript

    if cheatbench:
        items = _cheatbench_items(repo_root)
        pending = [item for item in items if opaque_id(item["item_id"]) not in keyed]
        eligible, pending_count = len(items), len(pending)
        if pending:
            asyncio.run(_insert(transcripts_dir, transcripts(
                (item["item_id"], _cheatbench_messages(item)) for item in pending
            )))
    else:
        resolved = admitted_refs(db_path, repo_root=repo_root) if refs is None else list(refs)
        pending_refs = [ref for ref in resolved if opaque_id(ref.trace_id) not in keyed]
        eligible, pending_count = len(resolved), len(pending_refs)
        if pending_refs:
            uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as con:

                def actor_histories() -> Iterator[tuple[str, list[dict[str, Any]]]]:
                    for ref in pending_refs:
                        try:
                            messages = blind_messages(con, ref.trace_id)
                        except ActorHistoryUnavailable as error:
                            unavailable.append({
                                "run_id": ref.trace_id, "opaque_id": opaque_id(ref.trace_id),
                                "category": error.category, "step_index": error.step_index,
                                "reason": error.reason,
                            })
                            continue
                        yield ref.trace_id, messages

                asyncio.run(_insert(transcripts_dir, transcripts(actor_histories())))
    if added:
        keymap_path.parent.mkdir(parents=True, exist_ok=True)
        with keymap_path.open("a", encoding="utf-8") as stream:
            for row in added:
                stream.write(json.dumps(row) + "\n")
    return {
        "source": source, "projection_version": ACTOR_PROJECTION_VERSION,
        "eligible": eligible, "inserted": len(added), "unchanged": eligible - pending_count,
        "unavailable": len(unavailable), "quarantined": unavailable,
        "transcripts_dir": str(transcripts_dir),
    }


def _scanner_module() -> Any:
    path = DEFAULT_REPO_ROOT / "research/explorations/trace-lab/scout/scanners.py"
    name = "_evallab_trace_tools_scanners"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load deterministic scanners from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old_path = sys.path[:]
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    return module


def scan(
    transcripts_dir: str | Path, scans_dir: str | Path, scanners: Sequence[str] = SCANNERS,
    *, transcript_ids: Sequence[str] | None = None,
) -> Any:
    """Run deterministic text scanners, optionally only the given opaque ids."""
    _require_scout()
    from inspect_scout import scan as scout_scan  # ty: ignore[unresolved-import]
    from inspect_scout._transcript.factory import (  # ty: ignore[unresolved-import]
        transcripts_from_db,
    )

    unsupported = set(scanners) - set(SCANNERS)
    if unsupported or not scanners:
        raise ValueError(f"Choose deterministic scanners from {SCANNERS}; unsupported={unsupported}")
    if transcript_ids is not None and not transcript_ids:
        raise ValueError("transcript_ids must not be empty")
    _check_transcripts(Path(transcripts_dir))
    transcripts = transcripts_from_db(str(transcripts_dir))
    if transcript_ids is not None:
        literals = ", ".join("'" + identifier.replace("'", "''") + "'" for identifier in transcript_ids)
        transcripts = transcripts.where(f"transcript_id IN ({literals})")
    module = _scanner_module()
    return scout_scan(
        scanners={name: getattr(module, name)() for name in scanners},
        transcripts=transcripts,
        scans=str(scans_dir), model="none/none", max_processes=1, max_transcripts=4,
        display="none", log_level="warning",
    )


def _clean(value: Any) -> Any:
    """Normalize scalar Pandas/NumPy missing values for strict JSON output."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    # Pandas nullable scalars have no truth value and are not JSON values.
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    return value


def _evidence_steps(references: Any) -> list[int]:
    references = _clean(references)
    if isinstance(references, str):
        references = json.loads(references)
    return sorted({
        int(match[1])
        for ref in references or []
        if (match := _MESSAGE_STEP.fullmatch(str(ref.get("id") or "")))
    })


def collect(
    scan_location: str | Path, keymap_path: str | Path, results_path: str | Path,
) -> int:
    """Append native results, mapping reference ids to original step indices.

    Every row must name a transcript in the current key map whose scanned
    metadata carries the current projection; otherwise nothing is written.
    """
    _require_scout()
    from inspect_scout import scan_results_df  # ty: ignore[unresolved-import]

    keymap = read_keymap(keymap_path)
    results_path = Path(results_path)
    require_current_results(results_path)
    results = scan_results_df(str(scan_location), rows="results")
    at = datetime.now(UTC).isoformat()
    lines = []
    for scanner, frame in results.scanners.items():
        for _, row in frame.iterrows():
            identity = keymap.get(row["transcript_id"])
            if (
                identity is None
                or _scanned_projection(row.get("transcript_metadata")) != ACTOR_PROJECTION_VERSION
            ):
                raise StaleProjectionError(
                    f"{scan_location} scanned a transcript that is not a keyed "
                    f"{ACTOR_PROJECTION_VERSION} actor history; nothing was collected",
                )
            label, value = _clean(row.get("label")), _clean(row.get("value"))
            flag = None
            if (
                scanner != "restored_tool_calls" and not _clean(row.get("scan_error"))
                and isinstance(value, int | float) and not isinstance(value, bool)
            ):
                flag = value >= 2 if scanner == "repeated_assistant_message" else value > 0
            record = {
                "schema": "evallab.trace_tool_result/v1",
                "run_id": identity["run_id"], "source": identity["source"],
                "projection_version": ACTOR_PROJECTION_VERSION,
                "tool": "scout", "detector": f"{scanner}:{label}" if label else scanner,
                "detector_version": f"inspect-scout/{SCOUT_VERSION}:"
                f"{_clean(row.get('scanner_version')) or '1'}",
                "value": value, "flag": flag,
                "evidence_steps": _evidence_steps(row.get("message_references")),
                "scan": str(scan_location), "at": at,
            }
            lines.append(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a", encoding="utf-8") as stream:
        stream.writelines(lines)
    return len(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI for incremental import, local scans, and append-only collection."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "scan", "collect", "run"):
        command = commands.add_parser(name)
        command.add_argument("--keymap-path", type=Path, default=DEFAULT_KEYMAP)
        if name in {"sync", "run"}:
            command.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
            command.add_argument("--db-path", type=Path, default=DEFAULT_DB)
        if name in {"sync", "scan", "run"}:
            command.add_argument("--transcripts-dir", type=Path)
            command.add_argument("--cheatbench", action="store_true")
        if name in {"scan", "run"}:
            command.add_argument("--scans-dir", type=Path, default=DEFAULT_SCANS)
            command.add_argument("--scanners", nargs="+", choices=SCANNERS, default=SCANNERS)
        if name in {"collect", "run"}:
            command.add_argument("--results-path", type=Path, default=DEFAULT_RESULTS)
        if name == "collect":
            command.add_argument("scan_location", type=Path)
        if name == "run":
            command.add_argument(
                "--cheatbench-transcripts-dir", type=Path, default=DEFAULT_CHEATBENCH_TRANSCRIPTS,
            )
    args = parser.parse_args(argv)
    started = time.monotonic()
    report: dict[str, Any] = {}
    errors = []
    if args.command == "collect":
        report["result_lines"] = collect(args.scan_location, args.keymap_path, args.results_path)
    else:
        sources = [False, True] if args.command == "run" else [args.cheatbench]
        reports = []
        for cheatbench in sources:
            transcripts_dir = (
                args.cheatbench_transcripts_dir if cheatbench and args.command == "run"
                else args.transcripts_dir or (
                    DEFAULT_CHEATBENCH_TRANSCRIPTS if cheatbench else DEFAULT_TRANSCRIPTS
                )
            )
            source_report: dict[str, Any] = {"transcripts_dir": str(transcripts_dir)}
            if args.command in {"sync", "run"}:
                source_report.update(sync(
                    args.db_path, transcripts_dir, args.keymap_path,
                    repo_root=args.repo_root, cheatbench=cheatbench,
                ))
            if args.command in {"scan", "run"}:
                status = scan(transcripts_dir, args.scans_dir, args.scanners)
                source_report.update({
                    "scan_location": status.location, "complete": status.complete,
                    "errors": [error.model_dump() for error in status.errors],
                })
                errors.extend(source_report["errors"])
                if not status.complete:
                    errors.append({"scan": status.location, "error": "incomplete scan"})
                if args.command == "run":
                    source_report["result_lines"] = collect(
                        status.location, args.keymap_path, args.results_path,
                    )
            reports.append(source_report)
        report["sources"] = reports
    report.update({"errors": errors, "wall_seconds": round(time.monotonic() - started, 3)})
    print(json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
