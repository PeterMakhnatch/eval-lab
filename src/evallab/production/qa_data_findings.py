"""Read-only, admitted-DEV findings from the local trace-tool result ledgers.

Flags are detector leads, not verdicts. Only explicitly named small tool fields
are projected. Structured identifiers/enums are restricted to 64-character
tokens; free-form mechanism/error text is capped at 200 characters. The
admission-derived sealed screen and opaque/source-identity scrub apply to both.
This is not a semantic evaluator-prose or answer-content classifier: legitimate
free-form text from an admitted DEV run may remain. Version/scan metadata and raw
value blobs are never returned.

Only decoded top-level admitted run identities contribute to output; malformed
and oversized records are silently discarded, without observable error counters.
Canonical run identities drive grouping/coverage, with distinct presentation
aliases for withheld identities. Per-call heaps retain at most 2,000 records per
flag category (fewer for small budgets); counts/coverage cover every accepted
observation. Bounded 8-byte fingerprints cover the original observation identity
and every field of the complete returned record, merging only identical mirrors;
``dedup_capped`` warns when the fingerprint allowance is exhausted.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from evallab.production.cloud_trace_db import _sealed_pattern
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError

_SCHEMA = "evallab.trace_tool_result/v1"
_FIELDS = {
    "docent": (
        "reward_hacking", "mechanism", "evidence_step", "confidence", "suspicion", "tests",
        "grader_or_harness_touched", "answer_material_accessed",
        "success_claim_without_evidence", "eval_awareness", "error",
    ),
    "scout": ("count", "score", "repetitions", "max_repetitions", "status", "suspicion"),
    "metr": ("reward_hacking", "detected", "confidence", "score", "evidence_step", "status"),
}
_OBSERVATION_FIELDS = ("flag", "status", "signal_name")
_COVERAGE_NOTE = "No finding: unscanned or no match, not a clean-run verdict."
_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_FREE_TEXT_FIELDS = frozenset({"mechanism", "error"})
_MAX_CANDIDATES = 2000
_MAX_DIGESTS = 200_000


_MAX_RECORD_BYTES = 256 * 1024
_CHUNK_BYTES = 32 * 1024
_OPAQUE = re.compile(
    r"(?i)(?:\bkeymap\s*[:=]\s*\S+|\b[0-9a-f]{32}\b|\brun-[0-9a-f]+\b"
    r"|\bsha256:[0-9a-f]+\b"
    r"|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b)"
)


@dataclass
class _Screen:
    ctx: QaContext
    sealed: re.Pattern[str]
    forbidden: tuple[str, ...]
    withheld: int = 0
    aliases: dict[str, str] = field(default_factory=dict)
    next_alias: int = 1

    def text(
        self, value: str, *, identity: bool = False, source: str = "", enumerated: bool = False,
    ) -> str:
        """Canonical admitted DB identities are joins, not opaque ledger tokens."""
        unsafe = self.ctx.retriever._sealed_text(value, self.sealed)
        if not identity:
            unsafe = unsafe or _OPAQUE.search(value) is not None
            unsafe = unsafe or any(word in value.casefold() for word in self.forbidden)
            unsafe = unsafe or bool(source and source.casefold() in value.casefold())
            unsafe = unsafe or bool(enumerated and _TOKEN.fullmatch(value) is None)
        if unsafe:
            self.withheld += 1
            return "[withheld]"
        return value if identity else value[:200]

    def run_id(self, value: str, wanted: set[str]) -> str:
        presented = self.text(value, identity=True)
        if presented != "[withheld]":
            return presented
        if value not in self.aliases:
            alias = f"run#{self.next_alias}"
            while alias in wanted:
                self.next_alias += 1
                alias = f"run#{self.next_alias}"
            self.aliases[value] = alias
            self.next_alias += 1
        return self.aliases[value]


def _small_fields(
    value: dict[str, Any], fields: tuple[str, ...], screen: _Screen, source: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in fields:
        item = value.get(key)
        if isinstance(item, str):
            result[key] = screen.text(item, source=source, enumerated=key not in _FREE_TEXT_FIELDS)
        elif isinstance(item, bool | int) or isinstance(item, float) and math.isfinite(item):
            result[key] = item
    return result


def _summary(tool: str, value: Any, screen: _Screen, source: str) -> dict[str, Any]:
    if tool in ("scout", "scout_metr") and (
        isinstance(value, bool | int) or isinstance(value, float) and math.isfinite(value)
    ):
        return {"score": value}
    if not isinstance(value, dict):
        return {}
    if tool == "scout_metr":
        return {
            key: item for key in ("score", "max")
            if isinstance(item := value.get(key), int | float) and not isinstance(item, bool)
            and (not isinstance(item, float) or math.isfinite(item))
        }
    if tool == "laminar":
        observations = value.get("observations")
        if not isinstance(observations, list):
            return {}
        compact = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            item = _small_fields(observation, _OBSERVATION_FIELDS, screen, source)
            signal_run = observation.get("signal_run")
            if "status" not in item and isinstance(signal_run, dict):
                item.update(_small_fields(signal_run, ("status",), screen, source))
            if item:
                compact.append(item)
        return {"observations": compact}
    return _small_fields(value, _FIELDS.get(tool, ()), screen, source)


@dataclass
class _Record:
    raw: bytearray | None
    wanted: bool
    oversized: bool = False


def _matches(raw: bytearray, needles: set[bytes]) -> bool:
    return b'"run_id"' in raw and (b"\\u" in raw or any(needle in raw for needle in needles))


def _top_run_id(raw: bytearray) -> str | None:
    """Decode only the first top-level run-id value, without parsing payloads.

    The bounded raw prefilter admits Unicode escapes, not just literal run-id
    spellings. The complete record's decoded identity is checked again afterward.
    """
    depth = 0
    start: int | None = None
    identity_start: int | None = None
    escaped = False
    for index, character in enumerate(raw):
        if start is not None:
            if escaped:
                escaped = False
            elif character == ord("\\"):
                escaped = True
            elif character == ord('"'):
                if start == identity_start:
                    identity = json.loads(raw[start : index + 1])
                    return identity if isinstance(identity, str) else None
                if depth == 1 and raw[start : index + 1] == b'"run_id"':
                    position = index + 1
                    while position < len(raw) and raw[position] in b" \t\r\n":
                        position += 1
                    if position < len(raw) and raw[position] == ord(":"):
                        position += 1
                        while position < len(raw) and raw[position] in b" \t\r\n":
                            position += 1
                        if position >= len(raw) or raw[position] != ord('"'):
                            return None
                        identity_start = position
                start = None
        elif character == ord('"'):
            start = index
        elif character in (ord("{"), ord("[")):
            depth += 1
        elif character in (ord("}"), ord("]")):
            depth -= 1
    return None


def _line_items(stream: BinaryIO, needles: set[bytes]) -> Iterator[_Record]:
    """Read bounded line fragments; discard overflow through the next newline."""
    item = bytearray()
    oversized = matched = False
    for chunk in iter(lambda: stream.readline(_CHUNK_BYTES), b""):
        if not oversized:
            room = _MAX_RECORD_BYTES - len(item)
            item.extend(chunk[:room])
            if len(chunk) > room:
                matched = _matches(item, needles)
                item.clear()
                oversized = True
        if chunk.endswith(b"\n"):
            yield _Record(None, matched, True) if oversized else _Record(item, _matches(item, needles))
            item = bytearray()
            oversized = matched = False
    if oversized:
        yield _Record(None, matched, True)
    elif item:
        yield _Record(item, _matches(item, needles))


def _canonical_items(stream: BinaryIO, needles: set[bytes]) -> Iterator[_Record]:
    """Frame canonical array/singleton objects, discarding overflow in-place.

    Quotes and nesting remain tracked after the cap, so an oversized object
    cannot hide the following record. At most 256 KiB of one record is retained.
    """
    container: int | None = None
    item = bytearray()
    depth = 0
    quoted = escaped = closed = oversized = matched = False
    for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
        for character in chunk:
            if closed:
                continue
            if container is None:
                if character in b" \t\r\n":
                    continue
                if character not in (ord("["), ord("{")):
                    return
                container = character
                if character == ord("["):
                    continue
            if not depth:
                if character in b" \t\r\n,":
                    continue
                if character == ord("]") and container == ord("["):
                    closed = True
                    continue
                if character != ord("{"):
                    return
            if not oversized:
                if len(item) < _MAX_RECORD_BYTES:
                    item.append(character)
                else:
                    matched = _matches(item, needles)
                    item.clear()
                    oversized = True
            if quoted:
                if escaped:
                    escaped = False
                elif character == ord("\\"):
                    escaped = True
                elif character == ord('"'):
                    quoted = False
            elif character == ord('"'):
                quoted = True
            elif character in (ord("{"), ord("[")):
                depth += 1
            elif character in (ord("}"), ord("]")):
                depth -= 1
                if not depth:
                    yield _Record(None, matched, True) if oversized else _Record(item, _matches(item, needles))
                    item = bytearray()
                    oversized = matched = False
                    closed = container == ord("{")
    if oversized:
        yield _Record(None, matched, True)
    elif item:
        yield _Record(item, _matches(item, needles))


def _ledger_records(path: Path, needles: set[bytes], *, canonical: bool = False) -> Iterator[_Record]:
    try:
        with path.open("rb") as stream:
            yield from _canonical_items(stream, needles) if canonical else _line_items(stream, needles)
    except FileNotFoundError:
        return


def _project(row: Any, wanted: set[str], screen: _Screen) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("schema") != _SCHEMA:
        return None
    if not isinstance(row.get("run_id"), str) or row["run_id"] not in wanted:
        return None
    tool, detector, at = (row.get(key) for key in ("tool", "detector", "at"))
    if not all(isinstance(item, str) and item for item in (tool, detector, at)):
        return None
    flag = row.get("flag")
    if flag is not None and not isinstance(flag, bool):
        return None
    steps = row.get("evidence_steps")
    if not isinstance(steps, list) or any(type(step) is not int or step < 0 for step in steps):
        return None
    source = row.get("source")
    source = source if isinstance(source, str) and source not in ("cloud-trace-db", "own-harbor") else ""
    summary = _summary(tool, row.get("value"), screen, source)
    if (
        tool == "scout_metr" and isinstance(maximum := row.get("max"), int | float)
        and not isinstance(maximum, bool) and (not isinstance(maximum, float) or math.isfinite(maximum))
    ):
        summary["max"] = maximum
    return {
        "tool": screen.text(tool, source=source, enumerated=True),
        "detector": screen.text(detector, source=source, enumerated=True),
        "run_id": screen.run_id(row["run_id"], wanted), "flag": flag, "evidence_steps": steps,
        "at": screen.text(at, source=source), "summary": summary,
    }


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _minimal(
    ctx: QaContext, *, requested: int, covered: int, omitted: int, dedup_capped: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "findings": {}, "truncated": True,
        "coverage": {"requested_runs": requested, "runs_with_findings": covered},
    }
    if dedup_capped:
        payload["dedup_capped"] = True
    ctx.budget.omitted += omitted
    ctx.budget.take(payload)
    return payload


def findings(
    ctx: QaContext, *, run_id: str | None = None, task_id: str | None = None, limit: int = 20,
) -> dict[str, Any]:
    """Group bounded, screened detector leads after fresh snapshot admission."""
    if (run_id is None) == (task_id is None):
        raise QaDataError("give exactly one of --run or --task")
    if limit < 1:
        raise QaDataError("--limit must be at least 1")
    if run_id is not None:
        owner = ctx.task_of_run(run_id)
    else:
        assert task_id is not None
        owner = ctx.require_task(task_id)["task_id"]
    # Fresh admission and the task-to-trace join must see the same snapshot.
    with ctx.database._transaction():
        admitted, sealed = ctx.retriever._admitted_ids()
        if owner not in admitted:
            raise QaDataError(UNAVAILABLE)
        if run_id is not None:
            row = ctx.database._execute(
                "SELECT task_id FROM agent_traces WHERE trace_id = ?", (run_id,),
            ).fetchone()
            if row is None or row[0] != owner:
                raise QaDataError(UNAVAILABLE)
            wanted = {run_id}
        else:
            rows = ctx.database._execute(
                "SELECT trace_id FROM agent_traces WHERE task_id = ? ORDER BY trace_id", (owner,),
            ).fetchall()
            wanted = {row[0] for row in rows}
        row = ctx.database._execute(
            "SELECT dataset_id FROM corpus_tasks WHERE task_id = ?", (owner,),
        ).fetchone()
        if row is None:
            raise QaDataError(UNAVAILABLE)
        dataset_id = row[0]
    forbidden = (dataset_id.casefold(),) if isinstance(dataset_id, str) and dataset_id else ()
    screen = _Screen(ctx, _sealed_pattern(frozenset(sealed)), forbidden)
    needles = {
        encoded
        for identity in wanted
        for ascii_only in (True, False)
        for raw in (json.dumps(identity, ensure_ascii=ascii_only).encode("utf-8"),)
        for encoded in (raw, raw.replace(b"/", b"\\/"))
    }
    # Scale retained candidates with the reply allowance, up to each flag cap.
    candidate_limit = min(_MAX_CANDIDATES, ctx.budget.remaining // 16)
    digest_limit = min(_MAX_DIGESTS, max(4096, ctx.budget.remaining * 16))
    flagged_heap: list[tuple[float, int, str, dict[str, Any]]] = []
    other_heap: list[tuple[float, int, str, dict[str, Any]]] = []
    seen: set[bytes] = set()
    counts: dict[str, dict[str, int]] | None = {}
    counts_size = 2
    covered: set[str] = set()
    matched = 0
    dedup_capped = False
    tools_dir = ctx.state_dir / "trace-tools"
    if wanted:
        paths = [(tools_dir / name, False) for name in ("results-v1.jsonl", "laminar-own.jsonl")]
        paths.extend((path, True) for path in sorted((tools_dir / "tt7/docent/arms").glob("*-canonical.json")))
        for path, canonical in paths:
            for item in _ledger_records(path, needles, canonical=canonical):
                if not item.wanted or item.oversized or item.raw is None:
                    continue
                try:
                    if _top_run_id(item.raw) not in wanted:
                        continue
                    row = json.loads(item.raw)
                except (ValueError, UnicodeError, RecursionError, MemoryError):
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("run_id"), str) or row["run_id"] not in wanted:
                    continue
                try:
                    steps = row.get("evidence_steps")
                    if not isinstance(steps, list) or any(type(step) is not int or step < 0 for step in steps):
                        continue
                    row["evidence_steps"] = sorted(set(steps))
                except (ValueError, TypeError, RecursionError, MemoryError):
                    continue
                canonical_id = row["run_id"]
                withheld_before, alias_before = screen.withheld, screen.next_alias
                had_alias = canonical_id in screen.aliases
                try:
                    projected = _project(row, wanted, screen)
                    if projected is None:
                        continue
                    identity = tuple(row.get(name) for name in (
                        "run_id", "tool", "detector", "detector_version", "scan", "at", "flag", "value",
                        "evidence_steps",
                    )) + (projected,)
                    encoded = json.dumps(
                        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")
                    digest = hashlib.blake2b(encoded, digest_size=8).digest()
                except (ValueError, TypeError, RecursionError, MemoryError):
                    screen.withheld, screen.next_alias = withheld_before, alias_before
                    if not had_alias:
                        screen.aliases.pop(canonical_id, None)
                    continue
                if digest in seen:
                    screen.withheld, screen.next_alias = withheld_before, alias_before
                    if not had_alias:
                        screen.aliases.pop(canonical_id, None)
                    continue
                if len(seen) < digest_limit:
                    seen.add(digest)
                else:
                    dedup_capped = True
                matched += 1
                covered.add(canonical_id)
                if counts is not None:
                    tool = projected["tool"]
                    count = counts.get(tool)
                    if count is None:
                        count = {"total": 1, "flagged": int(projected["flag"] is True)}
                        counts[tool] = count
                        counts_size += len(json.dumps({tool: count}, separators=(",", ":"))) - 2 + (len(counts) > 1)
                    else:
                        before = len(str(count["total"])) + len(str(count["flagged"]))
                        count["total"] += 1
                        count["flagged"] += projected["flag"] is True
                        counts_size += len(str(count["total"])) + len(str(count["flagged"])) - before
                    # This summary only grows. Once it alone cannot fit, the
                    # minimal fallback is certain; do not retain a huge tool map.
                    if counts_size > ctx.budget.remaining * 4:
                        counts = None
                heap = flagged_heap if projected["flag"] is True else other_heap
                if candidate_limit:
                    entry = (_timestamp(row["at"]), matched, canonical_id, projected)
                    if len(heap) < candidate_limit:
                        heapq.heappush(heap, entry)
                    elif entry[:2] > heap[0][:2]:
                        heapq.heapreplace(heap, entry)
    if counts is None:
        return _minimal(
            ctx, requested=len(wanted), covered=len(covered), omitted=matched, dedup_capped=dedup_capped,
        )

    payload: dict[str, Any] = {
        "findings": {}, "counts": counts,
        "coverage": {
            "requested_runs": len(wanted), "runs_with_findings": len(covered), "note": _COVERAGE_NOTE,
        },
        "withheld_strings": screen.withheld, "dedup_capped": dedup_capped,
        "omitted": matched, "truncated": True,
    }
    # Reserve all fixed keys, counts/coverage, and the longer boolean spelling.
    if not ctx.budget.take({**payload, "truncated": False}):
        return _minimal(
            ctx, requested=len(wanted), covered=len(covered), omitted=matched, dedup_capped=dedup_capped,
        )
    ranked = sorted(
        (*flagged_heap, *other_heap),
        key=lambda item: (
            item[2], item[3]["flag"] is not True, item[3]["tool"], item[3]["detector"], -item[0], -item[1],
        ),
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    taken = considered = 0
    for _at, _sequence, _canonical_id, record in ranked[:limit]:
        considered += 1
        group = record["run_id"]
        # Charge new group keys with their record, and subsequent item commas.
        item = {group: [record]} if group not in grouped else [record]
        if ctx.budget.take(item):
            grouped[group].append(record)
            taken += 1
    ctx.budget.omitted += matched - considered
    payload.update(
        findings=dict(grouped), omitted=matched - taken, truncated=taken < matched,
    )
    return payload
