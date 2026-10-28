"""Stage one ``trace-harness/findings-v1.jsonl`` for the cloud volume from the retained detector files.

The Harness tab and the Run Atlas alarm filter read one findings file, but the detector lanes write
their own (the trace harness lane's ``findings-v1.jsonl``, Run Health's
``findings-run-health-<n>.jsonl``). ``evallab prod export-trace-findings`` merges them read-only:

* every non-blank line is validated with the cloud loader's own ``harness_view.parse_finding``;
  a line that is not one JSON object, breaks the contract, or holds a per-run label or reward field
  (deploy-preflight's ``FINDINGS_FORBIDDEN`` policy) is refused and counted by reason, never copied;
* the newest record per (run, detector, detector version, reader model) wins, exactly as
  ``harness_view.load_findings`` keeps it (a later line wins a tie, so a later ``--findings`` file
  wins over an earlier one);
* only the contract fields are written, one record per line, sorted, to ``<out>/findings-v1.jsonl``
  through a temporary file in the same directory; an existing different file needs ``replace``.

It reads no database, calls no model and writes nothing outside the output directory. A linked
source, a linked output directory or a linked output file is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.production.atlas_view import strict_loads
from evallab.production.deploy_preflight import count_forbidden
from evallab.production.harness_view import MAX_FINDINGS_BYTES, Refused, parse_finding
from evallab.production.tool_scorecard_view import iter_lines

FILE = "findings-v1.jsonl"
NOT_JSON = "line is not one JSON object"
LABELLED = "holds a per-run label or reward field"
_Key = tuple[str, str, str, str]


def _regular_source(path: Path) -> None:
    try:
        seen = os.lstat(path)
    except FileNotFoundError:
        raise ValueError(f"{path} does not exist") from None
    if not stat.S_ISREG(seen.st_mode):
        raise ValueError(f"{path} must be a regular file, not a link or directory")


def _record(raw: bytes) -> dict[str, Any]:
    """One contract record, or ``Refused`` with a fixed reason that never repeats the line."""
    try:
        value = strict_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Refused(NOT_JSON) from None
    if count_forbidden(value, "findings"):
        raise Refused(LABELLED)
    return parse_finding(value)


def merge_findings(sources: Sequence[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The kept records sorted by key, and the per-source, kept, superseded and refused counts."""
    if not sources:
        raise ValueError("name at least one --findings file")
    kept: dict[_Key, dict[str, Any]] = {}
    refused: Counter[str] = Counter()
    read: list[dict[str, Any]] = []
    for source in sources:
        _regular_source(source)
        digest = hashlib.sha256()
        lines = 0
        for raw in iter_lines(source):
            digest.update(raw)
            if not raw.strip():
                continue
            lines += 1
            try:
                record = _record(raw)
            except Refused as reason:
                refused[str(reason)] += 1
                continue
            key = (record["run_id"], record["detector"], record["detector_version"], record["reader_model"] or "")
            if key not in kept or record["_moment"] >= kept[key]["_moment"]:
                kept[key] = record
        read.append({"path": str(source), "lines": lines, "sha256": digest.hexdigest()})
    total = sum(entry["lines"] for entry in read)
    return [kept[key] for key in sorted(kept)], {
        "sources": read,
        "lines_read": total,
        "kept": len(kept),
        "superseded": total - len(kept) - sum(refused.values()),
        "refused": dict(sorted(refused.items())),
    }


def _body(records: Sequence[dict[str, Any]]) -> bytes:
    lines = [
        json.dumps({name: value for name, value in record.items() if name != "_moment"}, ensure_ascii=False)
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _detectors(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for record in records:
        counts[(record["detector"], record["detector_version"], record["reader_model"] or "")][record["verdict"]] += 1
    return [
        {
            "detector": detector,
            "detector_version": version,
            "reader_model": reader or None,
            "records": sum(verdicts.values()),
            **{verdict: verdicts.get(verdict, 0) for verdict in ("alarm", "clear", "abstain", "unknown")},
        }
        for (detector, version, reader), verdicts in sorted(counts.items())
    ]


def export_findings(sources: Sequence[Path], directory: Path, *, replace: bool = False) -> dict[str, Any]:
    """Merge, validate and atomically write ``<directory>/findings-v1.jsonl``; return the receipt."""
    records, counts = merge_findings(sources)
    if not records:
        raise ValueError("no finding validated; nothing written")
    body = _body(records)
    if len(body) > MAX_FINDINGS_BYTES:
        raise ValueError(f"the merged file is larger than the {MAX_FINDINGS_BYTES} bytes the cloud loader accepts")
    if directory.is_symlink():
        raise ValueError(f"{directory} must be a directory, not a link")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"{FILE} must be a regular file")
        if target.read_bytes() != body and not replace:
            raise FileExistsError(f"{target} already differs; pass --replace to overwrite it")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".findings-export-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        **counts,
        "runs": len({record["run_id"] for record in records}),
        "detectors": _detectors(records),
    }
