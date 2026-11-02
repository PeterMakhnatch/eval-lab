"""Deterministic, bounded evidence packs and isolated worker directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

from pydantic import BaseModel

from evallab.insights.models import (
    BoardUpdate,
    ClaimExtraction,
    ClaimRecord,
    InsightBatch,
    InsightCard,
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

SNAPSHOT_NAME = "ledger.sqlite"
PASS_MANIFEST_META = "evallab.insights.pass_manifest"
PASS_DIGEST_META = "evallab.insights.pass_inputs_digest"

# The rederiver may inspect claim measurements, but never whether the original
# author's recipe reproduced. Keep this projection separate from the live view.
_REDERIVER_V_CLAIMS = """
CREATE VIEW v_claims AS
SELECT claim_id, experiment, kind, metric, subject, dataset,
       json_extract(body, '$.claim.condition') AS condition,
       json_extract(body, '$.claim.k') AS k,
       json_extract(body, '$.claim.n') AS n,
       json_extract(body, '$.claim.value') AS value,
       json_extract(body, '$.claim.unit') AS unit,
       json_extract(body, '$.claim.comparison.p_value') AS p_value,
       json_extract(body, '$.claim.split') AS split,
       json_extract(body, '$.claim.in_sample') AS in_sample,
       status, check_status,
       json_extract(body, '$.claim.statement') AS statement,
       json_extract(body, '$.claim.caveats') AS caveats,
       json_extract(body, '$.claim.models') AS models,
       fingerprint, as_of, source_id, section_id, created_at
FROM claims WHERE stale = 0
"""


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


def card_claim_ids(card: InsightCard) -> set[str]:
    """Return evidence and challenge targets, which are both cited evidence."""
    targets = card.challenge.target_claim_ids if card.challenge is not None else []
    return {ref.claim_id for ref in card.evidence} | set(targets)


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(text.splitlines(), start))


DATA_SUFFIXES = frozenset({".json", ".jsonl", ".csv"})
_CODE_SUFFIXES = frozenset({".py", ".sh", ".sql"})
_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_AGGREGATE_NAME = re.compile(
    r"summary|metrics?|report|results?|rollouts?|scoreboard|aggregate|scores?|stats|totals?"
    r"|manifest|labels?",
    re.IGNORECASE,
)
_CODE_FIRST = re.compile(r"prepare|label|scor|grade|build|extract|metric|analy[sz]e",
                         re.IGNORECASE)
SCAN_LIMIT = 20000
_COLLAPSE_AT = 8
_LIST_LIMITS = {"aggregate": 40, "code": 25, "collapsed": 15, "other": 15}


@dataclass(frozen=True)
class ReceiptFile:
    path: str
    size: int

    @property
    def depth(self) -> int:
        return self.path.count("/")

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()


@dataclass(frozen=True)
class ReceiptScan:
    files: tuple[ReceiptFile, ...]
    truncated: bool
    bundle_members: tuple[str, ...]


def is_aggregate_name(name: str) -> bool:
    return _AGGREGATE_NAME.search(Path(name).stem) is not None


def scan_receipts(root: Path) -> ReceiptScan:
    """Walk a receipt folder without following symlinks, stopping at ``SCAN_LIMIT`` files."""
    files: list[ReceiptFile] = []
    truncated = False
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs
                         if name not in _SKIP_DIRS and not (base / name).is_symlink())
        for name in sorted(names):
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if len(files) >= SCAN_LIMIT:
                truncated = True
                break
            files.append(ReceiptFile(path.relative_to(root).as_posix(), info.st_size))
        if truncated:
            break
    members: tuple[str, ...] = ()
    if root.name == ".bundle":
        members = tuple(sorted({file.path.split("/", 1)[0] for file in files if "/" in file.path}))
    return ReceiptScan(tuple(files), truncated, members)


def receipt_listing(folder: str | None) -> str:
    """Aggregates first, then code, collapsed per-case directories, then everything else."""
    if folder is None:
        return "Receipts: no folder recorded."
    root = Path(folder).expanduser()
    if not root.is_dir():
        return "Receipts: recorded folder unavailable."
    scan = scan_receipts(root)
    offset = 1 if scan.bundle_members else 0

    def shallow_first(file: ReceiptFile) -> tuple[int, str]:
        return (max(file.depth - offset, 0), file.path)

    data = [file for file in scan.files if file.suffix in DATA_SUFFIXES]
    # Numbered or hashed names (result_007.json, 3fa9c1d2e4.json) are per-case files.
    per_directory: dict[tuple[str, str, str], list[ReceiptFile]] = {}
    for file in scan.files:
        if file.suffix in _CODE_SUFFIXES:
            continue
        pattern = re.sub(r"[0-9a-f]{8,}|\d+", "*", Path(file.name).stem)
        key = (file.path.rpartition("/")[0], file.suffix, pattern)
        per_directory.setdefault(key, []).append(file)
    collapsed = {key: group for key, group in per_directory.items()
                 if len(group) > _COLLAPSE_AT and "*" in key[2]}
    first_pass = {file.path for group in collapsed.values() for file in group}
    remainder: dict[tuple[str, str, str], list[ReceiptFile]] = {}
    for file in scan.files:
        if (file.path in first_pass or file.suffix in _CODE_SUFFIXES
                or is_aggregate_name(file.name)):
            continue
        remainder.setdefault((file.path.rpartition("/")[0], file.suffix, "*"), []).append(file)
    collapsed.update({key: group for key, group in remainder.items()
                      if len(group) > 3 * _COLLAPSE_AT})
    collapsed_paths = {file.path for group in collapsed.values() for file in group}
    aggregates = sorted(
        (file for file in data
         if is_aggregate_name(file.name) or max(file.depth - offset, 0) == 0),
        key=shallow_first,
    )
    aggregates = [file for file in aggregates if file.path not in collapsed_paths]
    code = sorted(
        (file for file in scan.files if file.suffix in _CODE_SUFFIXES),
        key=lambda file: (_CODE_FIRST.search(file.name) is None, *shallow_first(file)),
    )
    shown = {file.path for file in aggregates} | {file.path for file in code} | collapsed_paths
    other = sorted((file for file in scan.files if file.path not in shown), key=shallow_first)

    lines: list[str] = []
    if scan.bundle_members:
        lines.append(
            "Layout: analysis bundle (.bundle). Every path below starts with its member "
            "folder and each member is one experiment folder copied unchanged. "
            f"Members: {', '.join(scan.bundle_members)}. Pass the bundle folder shown above "
            "to ./q recipe --folder, with the member folder inside the file path."
        )
    lines.append("Receipts (relative path | bytes):")

    def section(title: str, kind: str, items: list[ReceiptFile]) -> None:
        if not items:
            return
        lines.append(title)
        lines.extend(f"{file.path} | {file.size}" for file in items[:_LIST_LIMITS[kind]])
        if len(items) > _LIST_LIMITS[kind]:
            lines.append(f"{len(items) - _LIST_LIMITS[kind]} more {kind} files omitted; "
                         "use ./q locate or list the folder with bash.")

    section("Aggregate and summary data (read these first):", "aggregate", aggregates)
    section("Experiment code (labels, preparation, scoring):", "code", code)
    if collapsed:
        lines.append("Per-case directories (one line each: glob | files | bytes | examples):")
        ordered = sorted(collapsed.items(), key=lambda item: (item[0][0].count("/"), item[0]))
        for (directory, suffix, pattern), group in ordered[:_LIST_LIMITS["collapsed"]]:
            prefix = f"{directory}/" if directory else ""
            examples = ", ".join(file.name for file in group[:2])
            lines.append(f"{prefix}{pattern}{suffix} | {len(group)} files | "
                         f"{sum(file.size for file in group)} bytes | e.g. {examples}")
        if len(ordered) > _LIST_LIMITS["collapsed"]:
            lines.append(f"{len(ordered) - _LIST_LIMITS['collapsed']} more directories omitted.")
    section("Other files:", "other", other)
    if scan.truncated:
        lines.append(f"Folder scan stopped after {SCAN_LIMIT} files; later files are not listed.")
    return "\n".join(lines)


def compact_claim(record: ClaimRecord) -> str:
    claim = record.claim
    counts = f"{claim.k}/{claim.n}" if claim.k is not None else ""
    return " | ".join(
        [record.claim_id, claim.experiment, claim.statement, f"{counts}; value={claim.value}",
         claim.status, f"check={record.check.status}", "; ".join(claim.caveats)]
    )


def _layout_groups(store: InsightStore, claims: list[ClaimRecord], limit: int = 4) -> list[str]:
    """One receipt layout per distinct experiment folder behind the input claims."""
    folders: list[str] = []
    for record in claims:
        source = store.get_source(record.source_id)
        if source is not None and source.folder is not None and source.folder not in folders:
            folders.append(source.folder)
        if len(folders) == limit:
            break
    return [f"## Receipt layout\nfolder={folder}\n{receipt_listing(folder)}" for folder in folders]


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
            for claim_id in sorted(card_claim_ids(insight.card)):
                record = store.get_claim(claim_id)
                if record is None:
                    raise ValueError(f"unknown cited claim: {claim_id}")
                section = store.get_section(record.claim.section_id)
                source = store.get_source(record.source_id)
                if section is None or source is None:
                    raise ValueError(f"missing source section for cited claim: {claim_id}")
                records.append({
                    "claim": record.model_dump(mode="json"),
                    "source_section": {
                        "source_id": source.source_id,
                        "section_id": section.section_id,
                        "text": numbered(section_text(source, section), section.start_line),
                    },
                })
            # Author identity is not evidence. Include only the assigned id and card,
            # plus every support/contradiction and challenge-target claim.
            anonymous = {"insight_id": insight_id, "card": insight.card.model_dump(mode="json")}
            groups.append(
                f"## Card, cited claims, challenge targets, and source excerpts\n"
                f"{_json(anonymous)}\n{_json(records)}"
            )
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
        if inputs.lens in {"skeptic", "anomaly"}:
            groups.extend(_layout_groups(store, claims))
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


def pass_inputs_digest(pass_dir: Path) -> str:
    """Digest of the pack manifest and brief now on disk; compare with the recorded digest."""
    text = (pass_dir / "pack.json").read_text("utf-8") + (pass_dir / "brief.md").read_text("utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def _redact_for_rederiver(target: sqlite3.Connection) -> None:
    """Remove the original recipes and recompute outcomes from the snapshot copy."""
    rows = target.execute("SELECT claim_id, body FROM claims").fetchall()
    for claim_id, body in rows:
        value = json.loads(body)
        claim = value.get("claim")
        if isinstance(claim, dict):
            claim.pop("recipe", None)
        value.pop("recompute", None)
        target.execute(
            "UPDATE claims SET body = ?, recompute_status = NULL WHERE claim_id = ?",
            (_json(value), claim_id),
        )
    # Other rederivations and card-level recipe results would reveal the answer too.
    target.execute("DELETE FROM rederivations")
    target.execute("DELETE FROM pass_inputs")
    for insight_id, body in target.execute("SELECT insight_id, body FROM insights").fetchall():
        value = json.loads(body)
        card = value.get("card")
        if isinstance(card, dict) and "recipes" in card:
            card.pop("recipes")
            target.execute(
                "UPDATE insights SET body = ? WHERE insight_id = ?", (_json(value), insight_id),
            )
    target.execute("DROP VIEW v_claims")
    target.execute(_REDERIVER_V_CLAIMS)


def _snapshot_ledger(
    db_path: Path,
    destination: Path,
    *,
    lens: Lens,
    manifest: str,
    digest: str,
) -> None:
    """Back up the committed ledger, then seal the tool manifest in the copy."""
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        # A rollback-journal file needs no -wal/-shm files, so mode=ro works on 0444.
        target.execute("PRAGMA journal_mode=DELETE")
        with target:
            if lens == "rederiver":
                _redact_for_rederiver(target)
            target.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (PASS_MANIFEST_META, manifest),
            )
            target.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (PASS_DIGEST_META, digest),
            )
    except sqlite3.Error as exc:
        raise OSError(f"ledger snapshot failed: {exc}") from exc
    finally:
        target.close()
        source.close()
    destination.chmod(0o444)


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
    snapshot = path / SNAPSHOT_NAME
    _snapshot_ledger(db_path, snapshot, lens=inputs.lens, manifest=input_json, digest=digest)
    schema = output_contract(inputs.lens).model_json_schema()
    (path / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    wrapper = ("#!/bin/sh\nunset EVALLAB_INSIGHTS_STATE\nexec "
               + shlex.quote(python_executable)
               + " -m evallab.insights.agent_tool --db " + shlex.quote(str(snapshot))
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
