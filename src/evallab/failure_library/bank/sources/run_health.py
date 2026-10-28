"""Run Health: detector alarms and the blind DEV loop audit.

Inputs, both read-only:

* ``<prod_live>/run-health/findings-run-health-<N>.jsonl``: ``trace-findings/v1`` records
  from the deterministic ``run_health`` detectors; only the highest ``N`` is read (a newer
  version supersedes the older one). Each ``alarm`` is a positive of its kind (loop,
  runaway, latency, infra) with ``label_type = "detector"``: a rule fired, which is a
  weak label. ``clear``, ``unknown`` and ``abstain`` are not items; a detector clear is
  not a ground-truth negative.
* The p11 blind audit (PR #1404; ``raw/rh-p11`` beside the Run Atlas bridge): 136
  16-step windows, 68 ``run-health/3`` loop alarms plus 68 all-clear controls, each
  rated by two blind model raters. Where the two raters agree, the run gets one ``loop``
  item at ``segment`` granularity with ``label_type = "judge"``: ``loop`` is a
  positive, ``not_loop`` a negative. A split or ``unclear`` verdict is not an item.
  The locator ``blind-audit:p11`` keeps these apart from the detector alarms.

Split and actor transcript follow the ``cloud-traces`` source (cloud task identity
through ``ctx.strict_split``, the exports' ``actor_inputs_available`` rule).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    IssueItem,
    Provenance,
    SourceSummary,
    new_item,
)
from evallab.failure_library.bank.sources.cloud_traces import (
    _admitted,
    _declared,
    _has_actor_transcript,
    _tighter,
)
from evallab.failure_library.records import Split

SOURCE = "run-health"
FINDINGS_PATTERN = re.compile(r"^findings-run-health-(\d+)\.jsonl$")
KINDS = frozenset({"loop", "runaway", "latency", "infra"})
AUDIT_RELATIVE = Path("raw") / "rh-p11"
AUDIT_LOCATOR = "blind-audit:p11"
AUDIT_VERSION = "run-health/3"  # the detector version whose alarms the audit windowed
AUDIT_VERDICTS = {"loop": "positive", "not_loop": "negative"}
_NOTE_LIMIT = 300
_SQL = (
    "SELECT a.task_id, c.dataset_id, c.split, c.original_grader_digest,"
    " CASE WHEN json_valid(c.metadata_json) AND json_type(c.metadata_json, '$.item_id') = 'text'"
    " THEN json_extract(c.metadata_json, '$.item_id') END AS item_id,"
    " CASE WHEN json_valid(a.verified_receipt_json)"
    " THEN json_extract(a.verified_receipt_json, '$.admission.effective_split') END"
    " AS receipt_split"
    " FROM agent_traces a JOIN corpus_tasks c USING(task_id) WHERE a.trace_id = ?"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield number, row


def latest_findings(run_health_dir: Path) -> Path | None:
    """The findings file with the highest detector version number, if any."""
    versions = {
        int(match.group(1)): path
        for path in run_health_dir.glob("findings-run-health-*.jsonl")
        if (match := FINDINGS_PATTERN.match(path.name))
    }
    return versions[max(versions)] if versions else None


@dataclass(frozen=True)
class _Where:
    task_id: str | None
    declared: str
    split: Split
    actor: bool | None
    selector: str | None


class _Locator:
    """Cloud task identity, strict split and actor rule for a run id (cached)."""

    def __init__(self, ctx: BankContext) -> None:
        self._ctx = ctx
        self._cache: dict[str, _Where] = {}
        self._connection: sqlite3.Connection | None = None
        self._admitted: frozenset[str] = frozenset()
        if ctx.cloud_db.is_file():
            self._connection = sqlite3.connect(f"file:{ctx.cloud_db}?mode=ro", uri=True)
            self._connection.row_factory = sqlite3.Row
            self._admitted = _admitted(self._connection, ctx)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()

    def where(self, run_id: str) -> _Where:
        if run_id not in self._cache:
            self._cache[run_id] = self._lookup(run_id)
        return self._cache[run_id]

    def _lookup(self, run_id: str) -> _Where:
        row = None
        if self._connection is not None:
            row = self._connection.execute(_SQL, (run_id,)).fetchone()
        if row is None:  # no cloud identity: nothing to tighten against, so never DEV
            return _Where(None, "unassigned", "unassigned", None, None)
        record = dict(row)
        declared = _tighter(record, _declared(record))
        split = self._ctx.strict_split(
            declared,
            key_id=record["dataset_id"],
            item_id=record["item_id"],
            task_id=record["task_id"],
            task_digest=record["original_grader_digest"],
        )
        if split == "DEV" and record["task_id"] not in self._admitted:
            split = "unassigned"  # declared DEV, but the admission gate does not admit it
        assert self._connection is not None
        dev = split == "DEV"
        return _Where(
            record["task_id"],
            str(record["split"]),
            split,
            _has_actor_transcript(self._connection, run_id) if dev else None,
            run_id if dev else None,
        )


def _detector_items(findings: Path, locator: _Locator) -> Iterator[IssueItem]:
    digest = _sha256(findings)
    for number, row in _jsonl(findings):
        kind, run_id = row.get("kind"), row.get("run_id")
        if row.get("verdict") != "alarm" or kind not in KINDS or not run_id:
            continue  # clear / unknown / abstain are not ground truth
        where = locator.where(str(run_id))
        steps = row.get("evidence_steps") or []
        version = str(row.get("detector_version") or findings.stem)
        yield new_item(
            source=SOURCE,
            kind=kind,
            polarity="positive",
            label_type="detector",
            label=f"{row.get('detector')}: {row.get('summary') or 'alarm'}"[:200],
            granularity="run",
            subject_type="run",
            subject_id=str(run_id),
            task_id=where.task_id,
            positive_units=len(steps) or None,
            declared_split=where.declared,
            split=where.split,
            license=None,
            actor_transcript=where.actor,
            harness_selector=where.selector,
            provenance=Provenance(
                store=f"run-health:{version}",
                ref=str(run_id),
                digest=digest,
                note=f"{findings.name}#{number} score={row.get('score')}"[:_NOTE_LIMIT],
            ),
        )


def _audit_votes(audit: Path) -> dict[str, dict[str, str]]:
    votes: dict[str, dict[str, str]] = {}
    for path in sorted(audit.glob("out-*.jsonl")):
        rater = path.stem.rsplit("-", 1)[-1][:1]
        for _, row in _jsonl(path):
            if isinstance(row.get("packet"), str):
                votes.setdefault(row["packet"], {})[rater] = str(row.get("verdict"))
    return votes


def _audit_items(ctx: BankContext, locator: _Locator) -> Iterator[IssueItem]:
    audit = ctx.cross_test_registry.parent / AUDIT_RELATIVE
    key_path = audit / "key.jsonl"
    if not key_path.is_file():
        return
    digest = _sha256(key_path)
    votes = _audit_votes(audit)
    seen: set[str] = set()
    for number, key in _jsonl(key_path):
        run_id, packet = key.get("run_id"), key.get("packet")
        raters = votes.get(str(packet), {})
        verdicts = set(raters.values())
        if not run_id or len(raters) < 2 or len(verdicts) != 1:
            continue  # both raters or no label
        polarity = AUDIT_VERDICTS.get(next(iter(verdicts)))
        if polarity is None or str(run_id) in seen:
            continue
        where = locator.where(str(run_id))
        if polarity == "negative" and where.actor is not True:
            continue  # no actor transcript is unknown, never clean
        seen.add(str(run_id))
        yield new_item(
            source=SOURCE,
            kind="loop",
            polarity=polarity,
            label_type="judge",
            label=f"blind audit p11: {next(iter(verdicts))} (2 raters agree)",
            granularity="segment",
            subject_type="run",
            subject_id=str(run_id),
            task_id=where.task_id,
            locator=AUDIT_LOCATOR,
            declared_split=where.declared,
            split=where.split,
            license=None,
            actor_transcript=where.actor,
            harness_selector=where.selector,
            provenance=Provenance(
                store=f"run-health:{AUDIT_VERSION}",
                ref=f"{run_id}#{AUDIT_LOCATOR}",
                digest=digest,
                note=f"PR #1404 audit; key.jsonl#{number}; 16-step window, two blind raters"[
                    :_NOTE_LIMIT
                ],
            ),
        )


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    locator = _Locator(ctx)
    try:
        findings = latest_findings(ctx.prod_live / "run-health")
        if findings is not None:
            yield from _detector_items(findings, locator)
        yield from _audit_items(ctx, locator)
    finally:
        locator.close()
