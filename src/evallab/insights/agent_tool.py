"""Read-only evidence commands available inside an insight pass."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ValidationError

from evallab.insights.check import check_card, check_extracted_claim
from evallab.insights.locate import locate_values
from evallab.insights.models import (
    BoardUpdate,
    ClaimExtraction,
    ClaimRecord,
    InsightBatch,
    Lens,
    Recipe,
    RederiveBatch,
    RefereeBatch,
    SourceRecord,
)
from evallab.insights.packs import (
    OUTPUT_FILES,
    PASS_DIGEST_META,
    PASS_MANIFEST_META,
    card_claim_ids,
    compact_claim,
    numbered,
    output_contract,
)
from evallab.insights.recompute import run_recipe
from evallab.insights.sources import read_source_text, section_text
from evallab.insights.stats import compute
from evallab.insights.store import InsightStore

# A lexer, not substring matching: quoted strings may legitimately contain SQL keywords.
_SQL_TOKEN = re.compile(
    r"'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|`[^`]*`|\[[^\]]*\]"
    r"|--[^\n]*(?:\n|$)|/\*.*?\*/|[A-Za-z_][A-Za-z_0-9]*|;|[^\s]",
    re.DOTALL,
)
_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "ATTACH", "DETACH", "PRAGMA", "CREATE",
    "DROP", "ALTER", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "RELEASE", "LOAD_EXTENSION", "WRITEFILE", "READFILE",
}
_SQL_ROW_LIMIT = 200
_SQL_LENGTH_LIMIT = 1_000_000
_SQL_DEADLINE_SECONDS = 5.0
_LOCATE_DEADLINE_SECONDS = 4.0
_MAX_OUTPUT_BYTES = 1_000_000
_MAX_VALIDATE_BYTES = 2_000_000
_TRUNCATION_NOTE = "Output truncated: row or output byte limit reached; narrow the request.\n"


class _Output:
    """Emit complete records while reserving room for a truncation notice."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = _MAX_OUTPUT_BYTES if limit is None else limit
        self.used = 0
        self.truncated = False

    def truncate(self) -> None:
        if self.truncated:
            return
        note = _TRUNCATION_NOTE.encode("utf-8")
        remaining = self.limit - self.used
        if len(note) > remaining:
            raise ValueError("output byte limit is too small for a truncation notice")
        sys.stdout.write(_TRUNCATION_NOTE)
        self.used += len(note)
        self.truncated = True

    def line(self, text: str) -> bool:
        if self.truncated:
            return False
        payload = (text + "\n").encode("utf-8")
        if len(payload) + len(_TRUNCATION_NOTE.encode("utf-8")) > self.limit - self.used:
            self.truncate()
            return False
        sys.stdout.write(text + "\n")
        self.used += len(payload)
        return True

    def value(self, value: object) -> bool:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return self.line(json.dumps(value, ensure_ascii=False, sort_keys=True))


def readonly_sql(sql: str, *, blind: bool = False, row_limit: int = _SQL_ROW_LIMIT) -> str:
    tokens = [token for token in _SQL_TOKEN.findall(sql)
              if not token.startswith(("--", "/*"))]
    if tokens and tokens[-1] == ";":
        tokens.pop()
    if not tokens or tokens[0].upper() not in {"SELECT", "WITH"}:
        raise ValueError("sql accepts one SELECT or WITH query only")
    if ";" in tokens or any(token.upper() in _FORBIDDEN for token in tokens):
        raise ValueError("sql refuses writes, directives, and multiple statements")
    if blind:
        # The raw claim body contains the original recipe; SQL is intentionally narrower
        # for independent re-derivation. Strip identifier quotes before checking.
        identifiers = {token.strip('"`[]').lower() for token in tokens
                       if not token.startswith("'")}
        if identifiers & {"body", "claims", "insights", "passes", "recipe", "recompute"}:
            raise ValueError("rederiver SQL must use public views, not raw bodies or recipes")
    # Remove comments without changing numeric literals, operators, or quoted strings.
    cleaned = _SQL_TOKEN.sub(
        lambda match: "" if match.group().startswith(("--", "/*")) else match.group(), sql,
    ).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1]
    return "SELECT * FROM (" + cleaned + f"\n) LIMIT {row_limit}"


def _manifest(pass_dir: Path, store: InsightStore) -> dict[str, object]:
    sealed = store.get_meta(PASS_MANIFEST_META)
    expected_digest = store.get_meta(PASS_DIGEST_META)
    if sealed is None or expected_digest is None:
        raise ValueError("ledger snapshot has no sealed pass manifest")
    brief = (pass_dir / "brief.md").read_text("utf-8")
    actual_digest = hashlib.sha256((sealed + brief).encode()).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ValueError("sealed pass manifest digest does not match")
    value = json.loads(sealed)
    if not isinstance(value, dict) or value.get("lens") not in OUTPUT_FILES:
        raise ValueError("sealed pass manifest has no valid lens")
    return value


def _registered_folder(store: InsightStore, folder: str) -> Path:
    resolved = Path(folder).expanduser().resolve()
    allowed = {Path(source.folder).expanduser().resolve() for source in store.list_sources()
               if source.folder is not None}
    if resolved not in allowed:
        raise ValueError("recipe folder is not recorded on a ledger source")
    return resolved


def _claim_json(store: InsightStore, claim_id: str, *, blind: bool) -> dict[str, object]:
    record = store.get_claim(claim_id)
    if record is None:
        raise ValueError(f"unknown claim id: {claim_id}")
    if blind:
        claim = record.claim.model_dump(mode="json", exclude={"recipe"})
        return {"claim_id": record.claim_id, "source_id": record.source_id, "claim": claim}
    return record.model_dump(mode="json")


def _section(store: InsightStore, section_id: str) -> str:
    section = store.get_section(section_id)
    if section is None:
        raise ValueError(f"unknown section id: {section_id}")
    source = store.get_source(section.source_id)
    if source is None:
        raise ValueError(f"unknown source id: {section.source_id}")
    return section_text(source, section)


def _check_context(
    store: InsightStore, claims: dict[str, ClaimRecord],
) -> tuple[dict[str, SourceRecord], dict[str, str]]:
    """Source records and section text for cited claims, as `check_card` expects."""
    sources: dict[str, SourceRecord] = {}
    section_texts: dict[str, str] = {}
    for record in claims.values():
        source = store.get_source(record.source_id)
        if source is not None:
            sources[record.source_id] = source
        section_id = record.claim.section_id
        if section_id not in section_texts and store.get_section(section_id) is not None:
            section_texts[section_id] = _section(store, section_id)
    return sources, section_texts


def _validation_problems(
    store: InsightStore,
    manifest: dict[str, object],
    file: Path,
    sink: _Output,
) -> list[str]:
    lens = cast(Lens, manifest["lens"])
    try:
        if file.stat().st_size > _MAX_VALIDATE_BYTES:
            return [f"output file exceeds {_MAX_VALIDATE_BYTES} bytes"]
        output = output_contract(lens).model_validate_json(file.read_text("utf-8"))
    except (ValidationError, ValueError, OSError) as exc:
        return [str(exc)]
    problems: list[str] = []
    if isinstance(output, InsightBatch):
        if output.lens != lens:
            problems.append("batch lens does not match the pass lens")
        known = {insight.insight_id for insight in store.list_insights()}
        claim_ids: set[str] = set().union(*(card_claim_ids(card) for card in output.cards))
        claims = {claim_id: record for claim_id in claim_ids
                  if (record := store.get_claim(claim_id)) is not None}
        sources, section_texts = _check_context(store, claims)
        seen: set[str] = set()
        for card in output.cards:
            if card.local_id in seen:
                problems.append(f"duplicate card local_id: {card.local_id}")
            seen.add(card.local_id)
            missing = sorted(card_claim_ids(card) - claims.keys())
            problems.extend(f"{card.local_id}: unknown claim id: {item}" for item in missing)
            result = check_card(
                card, claims=claims, known_insights=known,
                sources=sources, section_texts=section_texts,
            )
            if result.status == "fail":
                problems.extend(f"{card.local_id}: {note}" for note in result.notes)
            elif result.status == "warn":
                for note in result.notes:
                    sink.line(f"Warning: {card.local_id}: {note}")
    elif isinstance(output, ClaimExtraction):
        expected = set(cast(list[str], manifest.get("section_ids", [])))
        source = store.get_source(output.source_id)
        if source is None:
            problems.append(f"unknown source id: {output.source_id}")
        for section_id in expected:
            section = store.get_section(section_id)
            if section is None or section.source_id != output.source_id:
                problems.append(f"{section_id}: section does not belong to output source")
        if set(output.section_ids) != expected:
            problems.append("section_ids must match the pass input sections")
        if not set(output.skipped_section_ids) <= expected:
            problems.append("skipped_section_ids contains a section outside this pass")
        covered = {claim.section_id for claim in output.claims} | set(output.skipped_section_ids)
        if expected - covered:
            problems.append("every input section needs claims or an explicit skip")
        seen = set()
        for claim in output.claims:
            if claim.local_id in seen:
                problems.append(f"duplicate claim local_id: {claim.local_id}")
            seen.add(claim.local_id)
            section = store.get_section(claim.section_id)
            if section is None or claim.section_id not in expected:
                problems.append(f"{claim.local_id}: unknown or unassigned section")
                continue
            if section.source_id != output.source_id:
                problems.append(f"{claim.local_id}: source_id does not match section")
            result = check_extracted_claim(claim, _section(store, claim.section_id))
            if result.status == "fail":
                problems.extend(f"{claim.local_id}: {note}" for note in result.notes)
            elif result.status == "warn":
                for note in result.notes:
                    sink.line(f"Warning: {claim.local_id}: {note}")
            if claim.recipe is not None:
                source = store.get_source(section.source_id)
                if source is None or source.folder is None:
                    problems.append(f"{claim.local_id}: recipe has no registered folder")
                else:
                    try:
                        run_recipe(claim.recipe, _registered_folder(store, source.folder))
                    except (ValueError, OSError) as exc:
                        problems.append(f"{claim.local_id}: recipe failed: {exc}")
    elif isinstance(output, RefereeBatch):
        expected = set(cast(list[str], manifest.get("insight_ids", [])))
        ids = [verdict.insight_id for verdict in output.verdicts]
        if set(ids) != expected or len(ids) != len(set(ids)):
            problems.append("provide exactly one verdict for each input insight")
        for verdict in output.verdicts:
            if store.get_insight(verdict.insight_id) is None:
                problems.append(f"unknown insight id: {verdict.insight_id}")
            if verdict.nearest_existing and store.get_insight(verdict.nearest_existing) is None:
                problems.append(f"unknown nearest_existing: {verdict.nearest_existing}")
    elif isinstance(output, BoardUpdate):
        if len(output.summary.split()) > 300:
            problems.append("summary exceeds 300 words")
        if len(output.promote) > 5:
            problems.append("promote contains more than five proposals")
        ids = [entry.insight_id for entry in output.ranked] + output.promote
        ids += [item for merge in output.merges for item in [merge.keep, *merge.duplicates]]
        ids += [item for change in output.supersedes for item in [change.new, change.old]]
        for insight_id in ids:
            if store.get_insight(insight_id) is None:
                problems.append(f"unknown insight id: {insight_id}")
        for resolution in output.resolutions:
            if store.get_prediction(resolution.prediction_id) is None:
                problems.append(f"unknown prediction id: {resolution.prediction_id}")
            if resolution.status in {"hit", "miss"} and not resolution.claim_ids:
                problems.append("hit/miss prediction resolutions require claim ids")
            for claim_id in resolution.claim_ids:
                if store.get_claim(claim_id) is None:
                    problems.append(f"unknown resolution claim id: {claim_id}")
    elif isinstance(output, RederiveBatch):
        expected = set(cast(list[str], manifest.get("claim_ids", [])))
        ids = [item.claim_id for item in output.claims]
        if set(ids) != expected or len(ids) != len(set(ids)):
            problems.append("provide exactly one rederivation for each input claim")
        for item in output.claims:
            record = store.get_claim(item.claim_id)
            if record is None:
                problems.append(f"unknown claim id: {item.claim_id}")
                continue
            if item.recipe is None:
                if not item.not_recomputable_reason:
                    problems.append(f"{item.claim_id}: missing reason for no recipe")
                continue
            source = store.get_source(record.source_id)
            if source is None or source.folder is None:
                problems.append(f"{item.claim_id}: no registered receipt folder")
                continue
            try:
                value = run_recipe(item.recipe, _registered_folder(store, source.folder))
                if item.value is None or abs(value - item.value) > 1e-9 * max(1, abs(value)):
                    problems.append(f"{item.claim_id}: value does not match tested recipe")
            except (ValueError, OSError) as exc:
                problems.append(f"{item.claim_id}: recipe failed: {exc}")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pass-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sql").add_argument("query")
    for name in ("claim", "insight", "experiment", "section", "checks"):
        commands.add_parser(name).add_argument("id")
    source = commands.add_parser("source")
    source.add_argument("id")
    source.add_argument("--lines")
    stat = commands.add_parser("stat")
    stat.add_argument("op")
    stat.add_argument("args")
    recipe = commands.add_parser("recipe")
    recipe.add_argument("recipe")
    recipe.add_argument("--folder", required=True)
    commands.add_parser("schema")
    locate = commands.add_parser("locate")
    locate.add_argument("--k", type=int)
    locate.add_argument("--n", type=int)
    locate.add_argument("--value", help="printed value, for example 60.0%% or 0.667")
    locate.add_argument("--unit")
    locate.add_argument("--folder", help="registered receipt folder (default: all registered)")
    commands.add_parser("validate").add_argument("file", nargs="?", type=Path)
    commands.add_parser("kb").add_argument("query", nargs="+")
    return parser


def _sql_rows(db: Path, sql: str, sink: _Output) -> None:
    """Run bounded read-only SQL: deadline, length limit, row and byte caps."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _SQL_LENGTH_LIMIT)
        deadline = time.monotonic() + _SQL_DEADLINE_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cursor = conn.execute(sql)
            emitted = 0
            while (row := cursor.fetchone()) is not None:
                if emitted == _SQL_ROW_LIMIT:
                    sink.truncate()
                    return
                if not sink.value(dict(row)):
                    return
                emitted += 1
        except sqlite3.Error as exc:
            if "interrupted" in str(exc):
                raise ValueError(
                    f"sql exceeded the {_SQL_DEADLINE_SECONDS:g} second deadline; "
                    "add WHERE/LIMIT or avoid unbounded recursion"
                ) from exc
            if "too big" in str(exc):
                raise ValueError(
                    f"sql value exceeds the {_SQL_LENGTH_LIMIT} byte limit; select less text"
                ) from exc
            raise
    finally:
        conn.close()


def _run(
    args: argparse.Namespace, store: InsightStore, manifest: dict[str, object], sink: _Output,
) -> int:
    lens = cast(Lens, manifest["lens"])
    blind = lens == "rederiver"
    if args.command == "sql":
        statement = readonly_sql(args.query, blind=blind, row_limit=_SQL_ROW_LIMIT + 1)
        _sql_rows(args.db, statement, sink)
    elif args.command == "claim":
        sink.value(_claim_json(store, args.id, blind=blind))
    elif args.command in {"insight", "checks"}:
        if blind:
            raise ValueError("rederiver cannot inspect existing cards or checks")
        insight = store.get_insight(args.id)
        if insight is None:
            raise ValueError(f"unknown insight id: {args.id}")
        if args.command == "insight":
            sink.value(insight)
        else:
            ids = card_claim_ids(insight.card)
            claims = {claim_id: record for claim_id in ids
                      if (record := store.get_claim(claim_id)) is not None}
            sources, section_texts = _check_context(store, claims)
            sink.value(check_card(
                insight.card, claims=claims,
                known_insights={item.insight_id for item in store.list_insights()},
                sources=sources, section_texts=section_texts,
            ))
    elif args.command == "experiment":
        experiment = store.get_experiment(args.id)
        if experiment is None:
            raise ValueError(f"unknown experiment id: {args.id}")
        sink.value(experiment)
        for record in store.list_claims(experiment=args.id):
            if not sink.line(compact_claim(record)):
                break
    elif args.command == "section":
        section = store.get_section(args.id)
        if section is None:
            raise ValueError(f"unknown section id: {args.id}")
        for line in numbered(_section(store, args.id), section.start_line).splitlines():
            if not sink.line(line):
                break
    elif args.command == "source":
        source = store.get_source(args.id)
        if source is None:
            raise ValueError(f"unknown source id: {args.id}")
        lines = read_source_text(source).splitlines()
        start, end = 1, len(lines)
        if args.lines:
            match = re.fullmatch(r"([1-9]\d*)-([1-9]\d*)", args.lines)
            if match is None:
                raise ValueError("--lines expects A-B with positive line numbers")
            start, end = map(int, match.groups())
            if end < start:
                raise ValueError("line range ends before it starts")
        for line in numbered("\n".join(lines[start - 1:end]), start).splitlines():
            if not sink.line(line):
                break
    elif args.command == "stat":
        values = json.loads(args.args)
        if not isinstance(values, dict):
            raise ValueError("stat arguments must be a JSON object")
        sink.value(compute(args.op, values))
    elif args.command == "recipe":
        folder = _registered_folder(store, args.folder)
        recipe = Recipe.model_validate_json(args.recipe)
        sink.value({"value": run_recipe(recipe, folder)})
    elif args.command == "locate":
        if args.folder is not None:
            folders = [_registered_folder(store, args.folder)]
        else:
            folders = sorted({
                Path(source.folder).expanduser().resolve() for source in store.list_sources()
                if source.folder is not None and Path(source.folder).expanduser().is_dir()
            })
            if not folders:
                raise ValueError("no registered receipt folder is available")
        result = locate_values(
            folders, k=args.k, n=args.n, value=args.value, unit=args.unit,
            deadline_seconds=_LOCATE_DEADLINE_SECONDS,
        )
        sink.value({key: item for key, item in result.items() if key != "hits"})
        for hit in result["hits"]:
            if not sink.value(hit):
                break
    elif args.command == "schema":
        sink.value(output_contract(lens).model_json_schema())
    elif args.command == "validate":
        file = args.file or args.pass_dir / "out" / OUTPUT_FILES[lens]
        if not file.is_absolute() and args.file is not None:
            file = args.pass_dir / file
        problems = _validation_problems(store, manifest, file, sink)
        if problems:
            for index, problem in enumerate(problems, 1):
                if not sink.line(f"{index}. {problem}"):
                    break
            return 2
        sys.stdout.write("OK\n")
    elif args.command == "kb":
        if lens != "ideator":
            raise ValueError("kb is available only to the ideator")
        result = subprocess.run(
            ["kb", "-n", "8", " ".join(args.query)], check=False, capture_output=True,
            text=True, timeout=60,
        )
        for line in result.stdout.splitlines():
            if not sink.line(line):
                break
        if result.stderr:
            print(result.stderr[:_MAX_OUTPUT_BYTES], end="", file=sys.stderr)
        return 0 if result.returncode == 0 else 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with InsightStore.open_readonly(args.db) as store:
            store.query("PRAGMA query_only=ON")
            manifest = _manifest(args.pass_dir, store)
            return _run(args, store, manifest, _Output())
    except (
        OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
