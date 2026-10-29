"""Resolve ``RunRecord.transcript_ref`` locators to native actor projections.

Locators are the collectors' own ``file://...#line=N|row=N[&field=...]`` form
(:func:`evallab.failure_library.runs.io.reference`); a bare ``#field=...``
addresses one whole JSON document. Only local retained bytes are read. Each
source file is streamed once per batch, never fetched, executed or rewritten.
"""

from __future__ import annotations

import importlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.records import RunRecord

ADAPTERS = {
    "goodfire-reward-hack-data": "goodfire",
    "hacktrace": "messages",
    "olmo-reward-hacking": "messages",
    "reward-hacking-ablation": "ablation",
    "cheatbench": "cheatbench",
    "terminal-wrench": "wrench",
    "metr-malt": "malt",
}
_PART = re.compile(r"([^.\[\]]+)((?:\[\d+\])*)")


@dataclass(frozen=True)
class Locator:
    path: Path
    kind: str  # "line" (1-based JSONL), "row" (0-based Parquet) or "document"
    index: int | None
    field: str | None


def locator(ref: str) -> Locator:
    parsed = urlparse(ref)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ActorUnavailable(
            f"not a retained local transcript: {parsed.scheme or 'relative'} ref"
        )
    fields = parse_qs(parsed.fragment, strict_parsing=bool(parsed.fragment))
    if set(fields) - {"line", "row", "field"} or any(len(v) != 1 for v in fields.values()):
        raise ActorUnavailable("unsupported transcript locator fragment")
    if "line" in fields and "row" in fields:
        raise ActorUnavailable("transcript locator names both a line and a row")
    kind = "line" if "line" in fields else "row" if "row" in fields else "document"
    index = int(fields[kind][0]) if kind != "document" else None
    if index is not None and index < (1 if kind == "line" else 0):
        raise ActorUnavailable("transcript locator index is out of range")
    field = fields["field"][0] if "field" in fields else None
    return Locator(Path(unquote(parsed.path)), kind, index, field)


def select_field(value: Any, field: str | None) -> Any:
    """Traverse ``a.b[0]`` exactly; a missing segment is unavailable, never empty."""
    if not field:
        return value
    for part in field.split("."):
        match = _PART.fullmatch(part)
        if match is None:
            raise ActorUnavailable(f"unsupported transcript field path {field!r}")
        if not isinstance(value, dict) or match[1] not in value:
            raise ActorUnavailable(f"transcript field {match[1]!r} is absent")
        value = value[match[1]]
        for index in re.findall(r"\[(\d+)\]", match[2]):
            if not isinstance(value, list) or int(index) >= len(value):
                raise ActorUnavailable(f"transcript field index {field!r} is absent")
            value = value[int(index)]
    if isinstance(value, str) and field.rsplit(".", 1)[-1] in {"messages", "steps"}:
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ActorUnavailable(f"transcript field {field!r} is not JSON") from exc
    return value


def _rows(path: Path, kind: str, wanted: set[int]) -> dict[int, Any]:
    from evallab.failure_library.runs.io import jsonl_rows, parquet_rows

    found: dict[int, Any] = {}
    if kind == "row":
        for index, row in parquet_rows(path):
            if index in wanted:
                found[index] = row
                if len(found) == len(wanted):
                    break
        return found
    for index, row in jsonl_rows(path):
        if index + 1 in wanted:
            found[index + 1] = row
            if len(found) == len(wanted):
                break
    return found


def project(raw: Any, run: RunRecord) -> ActorProjection:
    module = ADAPTERS.get(run.collection)
    if module is None:
        raise ActorUnavailable(f"no actor adapter for collection {run.collection!r}")
    adapter = importlib.import_module(f"{__package__}.{module}")
    return adapter.to_atif(raw, run)


def project_runs(
    runs: Iterable[RunRecord],
) -> Iterator[tuple[RunRecord, ActorProjection | ActorUnavailable]]:
    """Project every run, opening each retained source file once."""
    groups: dict[tuple[Path, str], list[tuple[RunRecord, Locator]]] = defaultdict(list)
    for run in runs:
        try:
            where = locator(run.transcript_ref)
        except (ActorUnavailable, ValueError) as exc:
            yield run, exc if isinstance(exc, ActorUnavailable) else ActorUnavailable(str(exc))
            continue
        groups[(where.path, where.kind)].append((run, where))
    for (path, kind), members in groups.items():
        if not path.is_file() or path.is_symlink():
            for run, _ in members:
                yield run, ActorUnavailable(f"retained transcript file is unavailable: {path.name}")
            continue
        rows: dict[int | None, Any] = {}
        try:
            if kind == "document":
                rows[None] = json.loads(path.read_text(encoding="utf-8"))
            else:
                wanted = {w.index for _, w in members if w.index is not None}
                for index, row in _rows(path, kind, wanted).items():
                    rows[index] = row
        except (OSError, ValueError) as exc:
            for run, _ in members:
                yield (
                    run,
                    ActorUnavailable(f"retained transcript is unreadable: {type(exc).__name__}"),
                )
            continue
        for run, where in members:
            try:
                if where.index not in rows:
                    raise ActorUnavailable("addressed transcript row is absent")
                yield run, project(select_field(rows[where.index], where.field), run)
            except ActorUnavailable as exc:
                yield run, exc
