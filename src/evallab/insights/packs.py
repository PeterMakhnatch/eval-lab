"""Deterministic, bounded evidence packs and isolated worker directories."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

from pydantic import BaseModel

from evallab.insights.models import (
    BoardUpdate,
    ClaimExtraction,
    ClaimRecord,
    InsightBatch,
    Lens,
    RederiveBatch,
    RefereeBatch,
    safe_identifier,
)
from evallab.insights.sources import section_text
from evallab.insights.store import InsightStore

OUTPUT_FILES: dict[Lens, str] = {
    "extractor": "claims.json",
    "skeptic": "cards.json",
    "connector": "cards.json",
    "anomaly": "cards.json",
    "theorist": "cards.json",
    "ideator": "cards.json",
    "referee": "verdicts.json",
    "synthesizer": "board.json",
    "rederiver": "rederive.json",
}


@dataclass(frozen=True)
class PassInputs:
    lens: Lens
    section_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    insight_ids: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    prediction_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PassDirectory:
    path: Path
    prompt_file: Path
    output_file: Path
    inputs_digest: str


def output_contract(lens: Lens) -> type[BaseModel]:
    if lens == "extractor":
        return ClaimExtraction
    if lens == "referee":
        return RefereeBatch
    if lens == "synthesizer":
        return BoardUpdate
    if lens == "rederiver":
        return RederiveBatch
    return InsightBatch


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(text.splitlines(), start))


def receipt_listing(folder: str | None) -> str:
    """List a bounded number of local receipt names; do not follow symlinks."""
    if folder is None:
        return "Receipts: no folder recorded."
    root = Path(folder).expanduser()
    if not root.is_dir():
        return "Receipts: recorded folder unavailable."
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    lines = ["Receipts (relative path | bytes):"]
    for path in files[:60]:
        lines.append(f"{path.relative_to(root).as_posix()} | {path.stat().st_size}")
    if len(files) > 60:
        lines.append(f"{len(files) - 60} more receipt names omitted.")
    return "\n".join(lines)


def compact_claim(record: ClaimRecord) -> str:
    claim = record.claim
    counts = f"{claim.k}/{claim.n}" if claim.k is not None else ""
    return " | ".join(
        [record.claim_id, claim.experiment, claim.statement, f"{counts}; value={claim.value}",
         claim.status, f"check={record.check.status}", "; ".join(claim.caveats)]
    )


def _groups(store: InsightStore, inputs: PassInputs) -> list[str]:
    """Return highest-priority groups first; a group is never cut mid-record."""
    groups: list[str] = []
    claims = []
    for claim_id in inputs.claim_ids:
        record = store.get_claim(claim_id)
        if record is None:
            raise ValueError(f"unknown input claim: {claim_id}")
        claims.append(record)
    if inputs.lens == "extractor":
        sections = []
        for section_id in inputs.section_ids:
            section = store.get_section(section_id)
            if section is None:
                raise ValueError(f"unknown input section: {section_id}")
            source = store.get_source(section.source_id)
            if source is None:
                raise ValueError(f"unknown source: {section.source_id}")
            sections.append((section, source))
        if len({source.source_id for _, source in sections}) > 1:
            raise ValueError("an extractor pass must contain sections of one source")
        groups.extend(
            f"## Section {section.section_id}\nsource={source.source_id} path={source.path}\n"
            f"family={source.family} kind={source.kind} folder={source.folder}\n"
            f"{receipt_listing(source.folder)}\n"
            f"{numbered(section_text(source, section), section.start_line)}"
            for section, source in sections
        )
        groups.extend(f"## Entity registry\n{_json(entity)}" for entity in store.list_entities())
        groups.extend(f"## Known experiment\n{_json(exp)}" for exp in store.list_experiments())
        return groups
    if inputs.lens == "rederiver":
        for record in claims:
            source = store.get_source(record.source_id)
            claim = record.claim
            # Explicit allowlist: never serialize the original claim or recompute object.
            body = {
                "claim_id": record.claim_id, "experiment": claim.experiment,
                "statement": claim.statement, "quote": claim.quote, "k": claim.k,
                "n": claim.n, "value": claim.value, "unit": claim.unit,
                "metric": claim.metric, "condition": claim.condition,
                "section_id": claim.section_id, "folder": source.folder if source else None,
            }
            groups.append(f"## Claim to independently derive\n{_json(body)}\n"
                          f"{receipt_listing(source.folder if source else None)}")
        return groups
    insights = store.list_insights()
    by_id = {record.insight_id: record for record in insights}
    if inputs.lens == "referee":
        for insight_id in inputs.insight_ids:
            insight = by_id.get(insight_id)
            if insight is None:
                raise ValueError(f"unknown input insight: {insight_id}")
            records = []
            for ref in insight.card.evidence:
                record = store.get_claim(ref.claim_id)
                if record is None:
                    raise ValueError(f"unknown cited claim: {ref.claim_id}")
                records.append(record.model_dump(mode="json"))
            groups.append(f"## Card and all cited claims\n{_json(insight)}\n{_json(records)}")
        return groups
    if inputs.lens == "synthesizer":
        groups.extend(f"## Refereed insight\n{_json(record)}" for record in insights
                      if record.status == "refereed")
        groups.extend(f"## Current rank\n{record.rank} | {record.insight_id}"
                      for record in sorted(insights, key=lambda item: (item.rank or 10**9,
                                                                     item.insight_id))
                      if record.rank is not None)
        groups.extend(f"## Open prediction\n{_json(prediction)}"
                      for prediction in store.list_predictions(status="open"))
        groups.extend(f"## New claim\n{compact_claim(record)}" for record in claims)
        return groups
    groups.append("## Ledger overview\n" + _json({
        "claim_checks": store.claim_counts(), "insight_statuses": store.insight_counts(),
        "experiments": len(store.list_experiments()),
    }))
    claim_groups = [f"## Input claim\n{compact_claim(record)}" for record in claims]
    signals = store.list_signals(limit=10000)
    selected = set(inputs.signal_ids)
    signals.sort(key=lambda signal: (signal.signal_id not in selected, -signal.score,
                                     signal.signal_id))
    signal_groups = [f"## Open signal\n{_json(signal)}" for signal in signals]
    ranked = sorted((item for item in insights if item.rank is not None),
                    key=lambda item: (item.rank or 10**9, item.insight_id))
    board_groups = [f"## Board insight\n{_json(record)}" for record in ranked[:12]]
    ranked_ids = {record.insight_id for record in ranked[:12]}
    recent = sorted(
        (record for record in insights
         if record.status in {"checked", "refereed", "needs_revision"}
         and record.lens != inputs.lens and record.insight_id not in ranked_ids),
        key=lambda record: (-record.created_at.timestamp(), record.insight_id),
    )
    recent_groups = [f"## Other lens recent card\n{_json(record)}" for record in recent]
    prediction_groups = [f"## Open prediction\n{_json(prediction)}"
                         for prediction in store.list_predictions(status="open")]
    if inputs.lens in {"theorist", "ideator"}:
        refereed_groups = [
            f"## Refereed insight\n{_json(record)}" for record in insights
            if record.status == "refereed" and record.referee is not None
            and record.insight_id not in ranked_ids
        ]
        groups.extend(board_groups)
        groups.extend(refereed_groups)
        groups.extend(prediction_groups)
        groups.extend(signal_groups)
        groups.extend(claim_groups)
        groups.extend(recent_groups)
    else:
        if inputs.lens == "anomaly":
            groups.extend(group for signal, group in zip(signals, signal_groups, strict=True)
                          if signal.signal_id in selected)
        groups.extend(claim_groups)
        groups.extend(group for signal, group in zip(signals, signal_groups, strict=True)
                      if inputs.lens != "anomaly" or signal.signal_id not in selected)
        groups.extend(board_groups)
        groups.extend(recent_groups)
        groups.extend(prediction_groups)
    return groups


def _bounded_pack(groups: list[str], lens: Lens, budget: int) -> str:
    header = f"# {lens} evidence pack\n\n"
    kept: list[str] = []
    for index, group in enumerate(groups):
        remaining = len(groups) - index - 1
        note = (f"\n\n{remaining} more omitted; query with ./q sql "
                "'SELECT * FROM v_claims LIMIT 200' or ./q section <id>.\n")
        candidate = header + "\n\n".join([*kept, group]) + (note if remaining else "\n")
        if len(candidate) > budget:
            break
        kept.append(group)
    omitted = len(groups) - len(kept)
    note = (f"\n\n{omitted} more omitted; query with ./q sql "
            "'SELECT * FROM v_claims LIMIT 200' or ./q section <id>.\n") if omitted else "\n"
    result = header + "\n\n".join(kept) + note
    if len(result) > budget:
        raise ValueError("max_pack_chars is too small for the pack header and omission note")
    return result


def build_pass_dir(
    store: InsightStore,
    inputs: PassInputs,
    *,
    pass_id: str,
    state_root: Path,
    db_path: Path,
    max_pack_chars: int,
    python_executable: str,
) -> PassDirectory:
    safe_identifier(pass_id)
    brief = resources.files("evallab.insights").joinpath("lenses", f"{inputs.lens}.md").read_text(
        encoding="utf-8"
    )
    pack = _bounded_pack(_groups(store, inputs), inputs.lens, max_pack_chars)
    input_json = _json(asdict(inputs)) + "\n"
    digest = hashlib.sha256((input_json + brief).encode()).hexdigest()
    path = (state_root / "passes" / pass_id).resolve()
    path.mkdir(parents=True, exist_ok=False)
    (path / "out").mkdir()
    (path / "brief.md").write_text(brief, encoding="utf-8")
    (path / "pack.md").write_text(pack, encoding="utf-8")
    (path / "pack.json").write_text(input_json, encoding="utf-8")
    schema = output_contract(inputs.lens).model_json_schema()
    (path / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    wrapper = ("#!/bin/sh\nexec " + shlex.quote(python_executable)
               + " -m evallab.insights.agent_tool --db " + shlex.quote(str(db_path.resolve()))
               + " --pass-dir " + shlex.quote(str(path)) + ' "$@"\n')
    q = path / "q"
    q.write_text(wrapper, encoding="utf-8")
    q.chmod(0o700)
    output_file = path / "out" / OUTPUT_FILES[inputs.lens]
    prompt = (brief + "\n\nRead pack.md first, then schema.json. Use ./q for evidence.\n"
              f"Write exactly one output file: out/{output_file.name}.\n"
              "Do not change source files, the ledger, or this pass's inputs.\n"
              "Run ./q validate until it prints OK, then stop.\n")
    prompt_file = path / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    return PassDirectory(path, prompt_file, output_file, digest)
