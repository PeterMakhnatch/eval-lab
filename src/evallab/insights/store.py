"""SQLite ledger for the insight engine.

The ledger is local runtime state, not Git content: claims quote experiment
reports that can name third-party runs, so it lives under
``$EVALLAB_INSIGHTS_STATE`` (default ``~/.local/state/evallab-insights``)
outside every worktree. Each table keeps a few indexed scalar columns and the
full contract as canonical JSON in ``body``; the ``v_*`` views flatten the
bodies for analysts' read-only SQL.

Writes go through :class:`InsightStore`. Agents never write the database: they
write files in their pass directory, and the engine validates and imports them.
Analysts query through :meth:`InsightStore.open_readonly`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evallab.insights.models import (
    CalibrationRecord,
    ClaimRecord,
    EntityRef,
    ExperimentRecord,
    HumanReview,
    InsightRecord,
    InsightStatus,
    PassRecord,
    PredictionRecord,
    RederivationRecord,
    SignalRecord,
    SourceRecord,
    SourceSection,
)

STATE_ENV = "EVALLAB_INSIGHTS_STATE"
DB_NAME = "insights.sqlite"
SCHEMA_USER_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  family TEXT NOT NULL,
  kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections(
  section_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  as_of TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  extracted_sha256 TEXT,
  extracted_pass TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sections_status ON sections(status, as_of);
CREATE TABLE IF NOT EXISTS experiments(
  exp_id TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims(
  claim_id TEXT PRIMARY KEY,
  experiment TEXT NOT NULL,
  kind TEXT NOT NULL,
  metric TEXT,
  subject TEXT,
  dataset TEXT,
  status TEXT NOT NULL,
  check_status TEXT NOT NULL,
  recompute_status TEXT,
  source_id TEXT NOT NULL,
  section_id TEXT NOT NULL,
  section_sha256 TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  as_of TEXT,
  stale INTEGER NOT NULL DEFAULT 0,
  pass_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_experiment ON claims(experiment);
CREATE INDEX IF NOT EXISTS claims_created ON claims(created_at);
CREATE INDEX IF NOT EXISTS claims_section ON claims(section_id);
CREATE TABLE IF NOT EXISTS entities(
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY(kind, name)
);
CREATE TABLE IF NOT EXISTS signals(
  signal_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  score REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insights(
  insight_id TEXT PRIMARY KEY,
  lens TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  check_status TEXT NOT NULL,
  rank INTEGER,
  score REAL,
  pass_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  graded_at TEXT,
  needs_recheck INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insights_status ON insights(status, created_at);
CREATE TABLE IF NOT EXISTS predictions(
  prediction_id TEXT PRIMARY KEY,
  insight_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passes(
  pass_id TEXT PRIMARY KEY,
  lens TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passes_lens ON passes(lens, created_at);
CREATE TABLE IF NOT EXISTS pass_inputs(
  pass_id TEXT PRIMARY KEY,
  lens TEXT NOT NULL,
  inputs TEXT NOT NULL,
  metadata TEXT NOT NULL,
  content_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lens_coverage(
  lens TEXT NOT NULL,
  exp_id TEXT NOT NULL,
  claims_digest TEXT NOT NULL,
  pass_id TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  PRIMARY KEY(lens, exp_id)
);
CREATE TABLE IF NOT EXISTS reviews(
  review_id TEXT PRIMARY KEY,
  insight_id TEXT NOT NULL,
  status TEXT NOT NULL,
  by TEXT NOT NULL,
  at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rederivations(
  claim_id TEXT NOT NULL,
  pass_id TEXT NOT NULL,
  agrees INTEGER,
  created_at TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(claim_id, pass_id)
);
CREATE TABLE IF NOT EXISTS referee_calibration(
  canary_id TEXT NOT NULL,
  insight_id TEXT NOT NULL,
  pass_id TEXT NOT NULL,
  arm TEXT NOT NULL,
  verdict TEXT NOT NULL,
  created_at TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(canary_id, insight_id, pass_id, arm)
);
CREATE INDEX IF NOT EXISTS referee_calibration_created ON referee_calibration(created_at, arm);
CREATE TABLE IF NOT EXISTS events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS v_claims AS
SELECT claim_id, experiment, kind, metric, subject, dataset,
       json_extract(body, '$.claim.condition') AS condition,
       json_extract(body, '$.claim.k') AS k,
       json_extract(body, '$.claim.n') AS n,
       json_extract(body, '$.claim.value') AS value,
       json_extract(body, '$.claim.unit') AS unit,
       json_extract(body, '$.claim.comparison.p_value') AS p_value,
       json_extract(body, '$.claim.split') AS split,
       json_extract(body, '$.claim.in_sample') AS in_sample,
       status, check_status, recompute_status,
       json_extract(body, '$.claim.statement') AS statement,
       json_extract(body, '$.claim.caveats') AS caveats,
       json_extract(body, '$.claim.models') AS models,
       fingerprint, as_of, source_id, section_id, created_at
FROM claims WHERE stale = 0;
CREATE VIEW IF NOT EXISTS v_insights AS
SELECT insight_id, lens, kind, status, check_status, rank, score,
       json_extract(body, '$.card.title') AS title,
       json_extract(body, '$.card.statement') AS statement,
       json_extract(body, '$.card.confidence') AS confidence,
       json_extract(body, '$.referee.verdict') AS referee_verdict,
       json_extract(body, '$.model_served') AS model_served,
       pass_id, created_at
FROM insights WHERE hidden = 0;
CREATE VIEW IF NOT EXISTS v_signals AS
SELECT signal_id, kind, score, status,
       json_extract(body, '$.title') AS title,
       json_extract(body, '$.claim_ids') AS claim_ids,
       created_at
FROM signals;
"""


def default_state_root() -> Path:
    configured = os.environ.get(STATE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "evallab-insights"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _load[ModelT: BaseModel](model_type: type[ModelT], body: str) -> ModelT:
    return model_type.model_validate_json(body)


class InsightStore:
    """Read/write access to one ledger file."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        if readonly:
            if not path.exists():
                raise FileNotFoundError(f"insight ledger not found: {path}")
            uri = f"file:{path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self.migrate()
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, state_root: Path | None = None) -> InsightStore:
        root = state_root or default_state_root()
        return cls(root / DB_NAME)

    @classmethod
    def open_readonly(cls, path: Path) -> InsightStore:
        return cls(path, readonly=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> InsightStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ core
    def migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_USER_VERSION:
            raise RuntimeError(
                f"insight ledger schema {version} is newer than supported {SCHEMA_USER_VERSION}"
            )
        with self._conn:
            # Views are replaced on every open so an older runtime cannot leave
            # stale projections after the schema or a view definition changes.
            for view in ("v_claims", "v_insights", "v_signals"):
                self._conn.execute(f"DROP VIEW IF EXISTS {view}")
            self._conn.executescript(_SCHEMA)
            columns = {
                str(row[1]) for row in self._conn.execute("PRAGMA table_info(insights)").fetchall()
            }
            if "graded_at" not in columns:
                self._conn.execute("ALTER TABLE insights ADD COLUMN graded_at TEXT")
            if "needs_recheck" not in columns:
                self._conn.execute(
                    "ALTER TABLE insights ADD COLUMN needs_recheck INTEGER NOT NULL DEFAULT 0"
                )
            if "hidden" not in columns:
                self._conn.execute(
                    "ALTER TABLE insights ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
                )
            self._conn.execute(
                "UPDATE insights SET graded_at = updated_at WHERE graded_at IS NULL "
                "AND status IN ('refereed', 'needs_revision', 'rejected')"
            )
            self._conn.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise PermissionError("read-only ledger")
        with self._conn:
            yield self._conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def log_event(self, kind: str, detail: dict[str, Any], *, at: datetime | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(at, kind, detail) VALUES(?, ?, ?)",
                (_iso(at or utcnow()), kind, json.dumps(detail, sort_keys=True, default=str)),
            )

    def events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    # --------------------------------------------------------------- sources
    def upsert_source(self, record: SourceRecord) -> bool:
        """Store a source; return True when it is new or its bytes changed."""
        existing = self._conn.execute(
            "SELECT sha256 FROM sources WHERE source_id = ?", (record.source_id,)
        ).fetchone()
        changed = existing is None or existing["sha256"] != record.sha256
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sources(source_id, path, family, kind, sha256, updated_at, body) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                "path = excluded.path, family = excluded.family, kind = excluded.kind, "
                "sha256 = excluded.sha256, updated_at = excluded.updated_at, body = excluded.body",
                (
                    record.source_id,
                    record.path,
                    record.family,
                    record.kind,
                    record.sha256,
                    _iso(utcnow()),
                    _dump(record),
                ),
            )
        return changed

    def get_source(self, source_id: str) -> SourceRecord | None:
        row = self._conn.execute(
            "SELECT body FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        return None if row is None else _load(SourceRecord, row["body"])

    def list_sources(self) -> list[SourceRecord]:
        rows = self._conn.execute("SELECT body FROM sources ORDER BY path").fetchall()
        return [_load(SourceRecord, row["body"]) for row in rows]

    def replace_sections(self, source_id: str, sections: Sequence[SourceSection]) -> list[str]:
        """Store the current sections of one source.

        A section whose bytes are unchanged keeps its extraction state; a new
        or changed section becomes ``pending``; claims from a section that
        changed or disappeared are marked stale. Returns the pending ids.
        """
        current = {
            row["section_id"]: row
            for row in self._conn.execute(
                "SELECT section_id, sha256, status, extracted_sha256 FROM sections "
                "WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        }
        keep_ids = {section.section_id for section in sections}
        pending: list[str] = []
        now = _iso(utcnow())
        with self.transaction() as conn:
            for section in sections:
                previous = current.get(section.section_id)
                unchanged = previous is not None and previous["sha256"] == section.sha256
                if previous is None:
                    status = "pending"
                elif unchanged:
                    status = previous["status"]
                elif previous["status"] == "in_flight":
                    # Never hand an edited section to a second extractor. The
                    # original pass compares its launch digest before import.
                    status = "in_flight"
                else:
                    status = "pending"
                if status == "pending":
                    pending.append(section.section_id)
                conn.execute(
                    "INSERT INTO sections(section_id, source_id, ordinal, sha256, as_of, status, "
                    "updated_at, body) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(section_id) DO UPDATE SET ordinal = excluded.ordinal, "
                    "sha256 = excluded.sha256, as_of = excluded.as_of, status = excluded.status, "
                    "attempts = CASE WHEN sections.sha256 != excluded.sha256 THEN 0 "
                    "ELSE sections.attempts END, "
                    "updated_at = excluded.updated_at, body = excluded.body",
                    (
                        section.section_id,
                        source_id,
                        section.ordinal,
                        section.sha256,
                        _iso(section.as_of),
                        status,
                        now,
                        _dump(section),
                    ),
                )
                if not unchanged and previous is not None:
                    conn.execute(
                        "UPDATE claims SET stale = 1 WHERE section_id = ? AND section_sha256 != ?",
                        (section.section_id, section.sha256),
                    )
            for section_id in set(current) - keep_ids:
                conn.execute("DELETE FROM sections WHERE section_id = ?", (section_id,))
                conn.execute("UPDATE claims SET stale = 1 WHERE section_id = ?", (section_id,))
        self.flag_insights_for_recheck()
        return pending

    def get_section(self, section_id: str) -> SourceSection | None:
        row = self._conn.execute(
            "SELECT body FROM sections WHERE section_id = ?", (section_id,)
        ).fetchone()
        return None if row is None else _load(SourceSection, row["body"])

    def pending_source_summaries(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """One row per eligible source, without a recency-window prefilter."""
        return self.query(
            "SELECT source_id, COUNT(*) AS pending_count, "
            "MAX(COALESCE(as_of, updated_at)) AS newest "
            "FROM sections WHERE status = 'pending' AND attempts < ? "
            "GROUP BY source_id",
            (max_attempts,),
        )

    def pending_sections(
        self,
        *,
        source_id: str | None = None,
        limit: int | None = 100,
        max_attempts: int = 3,
    ) -> list[SourceSection]:
        clauses = ["status = 'pending'", "attempts < ?"]
        params: list[Any] = [max_attempts]
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        tail = "" if limit is None else " LIMIT ?"
        if limit is not None:
            params.append(limit)
        rows = self._conn.execute(
            "SELECT body FROM sections WHERE " + " AND ".join(clauses)
            + " ORDER BY COALESCE(as_of, updated_at) DESC, ordinal ASC" + tail,
            params,
        ).fetchall()
        return [_load(SourceSection, row["body"]) for row in rows]

    def section_status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM sections GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def mark_sections(
        self,
        section_ids: Iterable[str],
        status: str,
        *,
        pass_id: str | None = None,
        count_attempt: bool = False,
        reset_attempts: bool = False,
    ) -> None:
        if status not in {"pending", "in_flight", "extracted", "skipped", "failed"}:
            raise ValueError(f"unknown section status: {status}")
        now = _iso(utcnow())
        with self.transaction() as conn:
            for section_id in section_ids:
                if status == "in_flight":
                    conn.execute(
                        "UPDATE sections SET status = ?, extracted_pass = ?, updated_at = ? "
                        "WHERE section_id = ?",
                        (status, pass_id, now, section_id),
                    )
                elif status == "extracted":
                    conn.execute(
                        "UPDATE sections SET status = ?, extracted_sha256 = sha256, "
                        "extracted_pass = ?, updated_at = ? WHERE section_id = ?",
                        (status, pass_id, now, section_id),
                    )
                else:
                    attempts = "0" if reset_attempts else (
                        "attempts + 1" if count_attempt else "attempts"
                    )
                    conn.execute(
                        f"UPDATE sections SET status = ?, attempts = {attempts}, updated_at = ? "
                        "WHERE section_id = ?",
                        (status, now, section_id),
                    )

    def mark_section_claims_stale(self, section_ids: Iterable[str]) -> None:
        ids = list(section_ids)
        if not ids:
            return
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE claims SET stale = 1 WHERE section_id IN ({', '.join('?' for _ in ids)})",
                ids,
            )
        self.flag_insights_for_recheck()

    # ----------------------------------------------------------- experiments
    def upsert_experiment(self, record: ExperimentRecord) -> None:
        existing = self.get_experiment(record.exp_id)
        merged = record if existing is None else _merge_experiment(existing, record)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO experiments(exp_id, updated_at, body) VALUES(?, ?, ?) "
                "ON CONFLICT(exp_id) DO UPDATE SET updated_at = excluded.updated_at, "
                "body = excluded.body",
                (merged.exp_id, _iso(utcnow()), _dump(merged)),
            )

    def get_experiment(self, exp_id: str) -> ExperimentRecord | None:
        row = self._conn.execute(
            "SELECT body FROM experiments WHERE exp_id = ?", (exp_id,)
        ).fetchone()
        return None if row is None else _load(ExperimentRecord, row["body"])

    def list_experiments(self) -> list[ExperimentRecord]:
        rows = self._conn.execute("SELECT body FROM experiments ORDER BY exp_id").fetchall()
        return [_load(ExperimentRecord, row["body"]) for row in rows]

    # ---------------------------------------------------------------- claims
    def insert_claims(self, records: Iterable[ClaimRecord]) -> int:
        """Insert claims and return how many rows were written.

        A live claim with the same id is left untouched. A *stale* one (its
        section changed) is replaced: re-extracting an edited section that still
        states the same thing revives the claim with the new section digest,
        keeping its original ``created_at`` so analysts do not see it as new.
        """
        inserted = 0
        with self.transaction() as conn:
            for record in records:
                claim = record.claim
                cursor = conn.execute(
                    "INSERT INTO claims(claim_id, experiment, kind, metric, subject, "
                    "dataset, status, check_status, recompute_status, source_id, section_id, "
                    "section_sha256, fingerprint, as_of, stale, pass_id, created_at, body) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(claim_id) DO UPDATE SET status = excluded.status, "
                    "experiment = excluded.experiment, kind = excluded.kind, "
                    "metric = excluded.metric, subject = excluded.subject, "
                    "dataset = excluded.dataset, fingerprint = excluded.fingerprint, "
                    "source_id = excluded.source_id, section_id = excluded.section_id, "
                    "check_status = excluded.check_status, "
                    "recompute_status = excluded.recompute_status, "
                    "section_sha256 = excluded.section_sha256, as_of = excluded.as_of, "
                    "stale = excluded.stale, pass_id = excluded.pass_id, "
                    "body = json_set(excluded.body, '$.created_at', "
                    "json_extract(claims.body, '$.created_at')) "
                    "WHERE claims.stale = 1",
                    (
                        record.claim_id,
                        claim.experiment,
                        claim.kind,
                        claim.metric,
                        claim.subject,
                        claim.dataset,
                        claim.status,
                        record.check.status,
                        None if record.recompute is None else record.recompute.status,
                        record.source_id,
                        claim.section_id,
                        record.section_sha256,
                        record.fingerprint,
                        _iso(record.as_of),
                        int(record.stale),
                        record.pass_id,
                        _iso(record.created_at),
                        _dump(record),
                    ),
                )
                inserted += cursor.rowcount
        self.flag_insights_for_recheck()
        return inserted

    def update_claim(self, record: ClaimRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE claims SET check_status = ?, recompute_status = ?, stale = ?, body = ? "
                "WHERE claim_id = ?",
                (
                    record.check.status,
                    None if record.recompute is None else record.recompute.status,
                    int(record.stale),
                    _dump(record),
                    record.claim_id,
                ),
            )
        self.flag_insights_for_recheck()

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        row = self._conn.execute(
            "SELECT body, stale FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    def list_claims(
        self,
        *,
        experiment: str | None = None,
        since: datetime | None = None,
        include_stale: bool = False,
        include_failed: bool = False,
        limit: int | None = None,
    ) -> list[ClaimRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_stale:
            clauses.append("stale = 0")
        if not include_failed:
            clauses.append("check_status != 'fail'")
        if experiment is not None:
            clauses.append("experiment = ?")
            params.append(experiment)
        if since is not None:
            clauses.append("created_at > ?")
            params.append(_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = "" if limit is None else f" LIMIT {int(limit)}"
        rows = self._conn.execute(
            f"SELECT body, stale FROM claims {where} "
            f"ORDER BY COALESCE(as_of, created_at), claim_id{tail}",
            params,
        ).fetchall()
        return [_claim_from_row(row) for row in rows]

    def claim_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT check_status, COUNT(*) AS n FROM claims WHERE stale = 0 GROUP BY check_status"
        ).fetchall()
        return {str(row["check_status"]): int(row["n"]) for row in rows}

    def experiment_review_candidates(self) -> list[dict[str, Any]]:
        """Digest every experiment's live claim ids and their section versions."""
        rows = self._conn.execute(
            "SELECT claim_id, experiment, section_sha256, check_status, as_of, created_at "
            "FROM claims WHERE stale = 0 ORDER BY experiment, claim_id"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["experiment"]), []).append(row)
        candidates: list[dict[str, Any]] = []
        for exp_id, items in grouped.items():
            digest_payload = [
                [str(row["claim_id"]), str(row["section_sha256"])] for row in items
            ]
            candidates.append({
                "exp_id": exp_id,
                "claims_digest": hashlib.sha256(
                    json.dumps(digest_payload, separators=(",", ":")).encode()
                ).hexdigest(),
                "claim_ids": [
                    str(row["claim_id"]) for row in items if row["check_status"] != "fail"
                ],
                "claim_count": len(items),
                "recency": max(str(row["as_of"] or row["created_at"]) for row in items),
            })
        return candidates

    def coverage_digests(self, lens: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT exp_id, claims_digest FROM lens_coverage WHERE lens = ?", (lens,)
        ).fetchall()
        return {str(row["exp_id"]): str(row["claims_digest"]) for row in rows}

    def record_coverage(
        self, lens: str, pass_id: str, digests: dict[str, str], *, reviewed_at: datetime
    ) -> None:
        with self.transaction() as conn:
            for exp_id, digest in digests.items():
                conn.execute(
                    "INSERT INTO lens_coverage(lens, exp_id, claims_digest, pass_id, reviewed_at) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(lens, exp_id) DO UPDATE SET "
                    "claims_digest = excluded.claims_digest, pass_id = excluded.pass_id, "
                    "reviewed_at = excluded.reviewed_at",
                    (lens, exp_id, digest, pass_id, _iso(reviewed_at)),
                )

    def coverage_count(self, lens: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM lens_coverage WHERE lens = ?", (lens,)
        ).fetchone()
        return int(row[0])

    # -------------------------------------------------------------- entities
    def upsert_entities(self, entities: Iterable[EntityRef]) -> None:
        now = _iso(utcnow())
        with self.transaction() as conn:
            for entity in entities:
                row = conn.execute(
                    "SELECT aliases FROM entities WHERE kind = ? AND name = ?",
                    (entity.kind, entity.name),
                ).fetchone()
                aliases = set(entity.aliases)
                if row is not None:
                    aliases |= set(json.loads(row[0]))
                conn.execute(
                    "INSERT INTO entities(kind, name, aliases, first_seen) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(kind, name) DO UPDATE SET aliases = excluded.aliases",
                    (entity.kind, entity.name, json.dumps(sorted(aliases)), now),
                )

    def list_entities(self) -> list[EntityRef]:
        rows = self._conn.execute("SELECT kind, name, aliases FROM entities ORDER BY kind, name")
        return [
            EntityRef(kind=row["kind"], name=row["name"], aliases=json.loads(row["aliases"]))
            for row in rows.fetchall()
        ]

    # --------------------------------------------------------------- signals
    def upsert_signals(self, signals: Iterable[SignalRecord]) -> int:
        """Insert new signals; a known signal keeps its status. Returns inserts."""
        inserted = 0
        now = _iso(utcnow())
        with self.transaction() as conn:
            for signal in signals:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO signals(signal_id, kind, score, status, created_at, "
                    "updated_at, body) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        signal.signal_id,
                        signal.kind,
                        signal.score,
                        signal.status,
                        _iso(signal.created_at),
                        now,
                        _dump(signal),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def list_signals(self, *, status: str | None = "open", limit: int = 50) -> list[SignalRecord]:
        if status is None:
            rows = self._conn.execute(
                "SELECT body, status FROM signals ORDER BY score DESC, signal_id LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT body, status FROM signals WHERE status = ? "
                "ORDER BY score DESC, signal_id LIMIT ?",
                (status, limit),
            ).fetchall()
        out: list[SignalRecord] = []
        for row in rows:
            record = _load(SignalRecord, row["body"])
            out.append(record.model_copy(update={"status": row["status"]}))
        return out

    def mark_signals(self, signal_ids: Iterable[str], status: str) -> None:
        if status not in {"open", "consumed", "stale"}:
            raise ValueError(f"unknown signal status: {status}")
        now = _iso(utcnow())
        with self.transaction() as conn:
            for signal_id in signal_ids:
                conn.execute(
                    "UPDATE signals SET status = ?, updated_at = ? WHERE signal_id = ?",
                    (status, now, signal_id),
                )

    def signal_ids(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT signal_id FROM signals").fetchall()}

    # -------------------------------------------------------------- insights
    def insert_insights(self, records: Iterable[InsightRecord]) -> int:
        inserted = 0
        now = _iso(utcnow())
        with self.transaction() as conn:
            for record in records:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO insights(insight_id, lens, kind, status, check_status, "
                    "rank, score, pass_id, created_at, updated_at, graded_at, hidden, body) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.insight_id,
                        record.lens,
                        record.card.kind,
                        record.status,
                        record.check.status,
                        record.rank,
                        record.score,
                        record.pass_id,
                        _iso(record.created_at),
                        now,
                        now if record.status in {"refereed", "needs_revision", "rejected"} else None,
                        int(record.hidden),
                        _dump(record),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_insight(self, record: InsightRecord, *, graded_at: datetime | None = None) -> None:
        """Update a card; ``graded_at`` records when it first received a grade."""
        now = _iso(utcnow())
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT graded_at FROM insights WHERE insight_id = ?", (record.insight_id,)
            ).fetchone()
            stamp = None if previous is None else previous["graded_at"]
            if record.status in {"refereed", "needs_revision", "rejected"} and stamp is None:
                stamp = now if graded_at is None else _iso(graded_at)
            conn.execute(
                "UPDATE insights SET status = ?, check_status = ?, rank = ?, score = ?, "
                "updated_at = ?, graded_at = ?, hidden = ?, body = ? WHERE insight_id = ?",
                (
                    record.status,
                    record.check.status,
                    record.rank,
                    record.score,
                    now,
                    stamp,
                    int(record.hidden),
                    _dump(record),
                    record.insight_id,
                ),
            )

    def insights_needing_recheck(self) -> list[InsightRecord]:
        rows = self._conn.execute(
            "SELECT body FROM insights WHERE needs_recheck = 1 ORDER BY created_at, insight_id"
        ).fetchall()
        return [_load(InsightRecord, row["body"]) for row in rows]

    def clear_recheck(self, insight_ids: Iterable[str]) -> None:
        with self.transaction() as conn:
            for insight_id in insight_ids:
                conn.execute(
                    "UPDATE insights SET needs_recheck = 0 WHERE insight_id = ?", (insight_id,)
                )

    def get_insight(self, insight_id: str) -> InsightRecord | None:
        row = self._conn.execute(
            "SELECT body FROM insights WHERE insight_id = ?", (insight_id,)
        ).fetchone()
        return None if row is None else _load(InsightRecord, row["body"])

    def list_insights(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        lens: str | None = None,
        since: datetime | None = None,
        graded_since: datetime | None = None,
        limit: int | None = None,
        include_hidden: bool = False,
    ) -> list[InsightRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_hidden:
            clauses.append("hidden = 0")
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if lens is not None:
            clauses.append("lens = ?")
            params.append(lens)
        if since is not None:
            clauses.append("created_at > ?")
            params.append(_iso(since))
        if graded_since is not None:
            clauses.append("graded_at > ?")
            params.append(_iso(graded_since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = "" if limit is None else f" LIMIT {int(limit)}"
        rows = self._conn.execute(
            f"SELECT body FROM insights {where} "
            f"ORDER BY COALESCE(rank, 1000000), created_at DESC, insight_id{tail}",
            params,
        ).fetchall()
        return [_load(InsightRecord, row["body"]) for row in rows]

    def insight_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM insights WHERE hidden = 0 GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def flag_insights_for_recheck(self) -> None:
        """Flag every card that names a now-stale or failed claim.

        The stored check remains visible for audit; board/status consumers can
        exclude or explain ``needs_recheck`` without rewriting model history.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE insights SET needs_recheck = 1 WHERE needs_recheck = 0 "
                "AND status != 'check_failed' AND EXISTS ("
                "SELECT 1 FROM json_tree(insights.body) AS node "
                "JOIN claims ON claims.claim_id = node.value "
                "WHERE claims.stale = 1 OR claims.check_status = 'fail')"
            )

    def insight_recheck_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM insights WHERE needs_recheck = 1"
        ).fetchone()
        return int(row[0])

    # ----------------------------------------------------------- predictions
    def insert_predictions(self, records: Iterable[PredictionRecord]) -> int:
        inserted = 0
        with self.transaction() as conn:
            for record in records:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO predictions(prediction_id, insight_id, status, "
                    "created_at, resolved_at, body) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        record.prediction_id,
                        record.insight_id,
                        record.status,
                        _iso(record.created_at),
                        _iso(record.resolved_at),
                        _dump(record),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_prediction(self, record: PredictionRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE predictions SET status = ?, resolved_at = ?, body = ? "
                "WHERE prediction_id = ?",
                (record.status, _iso(record.resolved_at), _dump(record), record.prediction_id),
            )

    def get_prediction(self, prediction_id: str) -> PredictionRecord | None:
        row = self._conn.execute(
            "SELECT body FROM predictions WHERE prediction_id = ?", (prediction_id,)
        ).fetchone()
        return None if row is None else _load(PredictionRecord, row["body"])

    def list_predictions(self, *, status: str | None = None) -> list[PredictionRecord]:
        if status is None:
            rows = self._conn.execute(
                "SELECT body FROM predictions ORDER BY created_at, prediction_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT body FROM predictions WHERE status = ? ORDER BY created_at, prediction_id",
                (status,),
            ).fetchall()
        return [_load(PredictionRecord, row["body"]) for row in rows]

    # ---------------------------------------------------------------- passes
    def save_pass_inputs(
        self,
        pass_id: str,
        lens: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any],
        content_digest: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO pass_inputs(pass_id, lens, inputs, metadata, content_digest) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    pass_id,
                    lens,
                    json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    content_digest,
                ),
            )

    def pass_inputs(self, pass_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT lens, inputs, metadata, content_digest FROM pass_inputs WHERE pass_id = ?",
            (pass_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "lens": str(row["lens"]),
            "inputs": json.loads(row["inputs"]),
            "metadata": json.loads(row["metadata"]),
            "content_digest": str(row["content_digest"]),
        }

    def save_pass(self, record: PassRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO passes(pass_id, lens, status, created_at, updated_at, body) "
                "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(pass_id) DO UPDATE SET "
                "status = excluded.status, updated_at = excluded.updated_at, body = excluded.body",
                (
                    record.pass_id,
                    record.lens,
                    record.status,
                    _iso(record.created_at),
                    _iso(utcnow()),
                    _dump(record),
                ),
            )

    def get_pass(self, pass_id: str) -> PassRecord | None:
        row = self._conn.execute("SELECT body FROM passes WHERE pass_id = ?", (pass_id,)).fetchone()
        return None if row is None else _load(PassRecord, row["body"])

    def list_passes(
        self,
        *,
        lens: str | None = None,
        statuses: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[PassRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if lens is not None:
            clauses.append("lens = ?")
            params.append(lens)
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if since is not None:
            clauses.append("created_at > ?")
            params.append(_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = "" if limit is None else f" LIMIT {int(limit)}"
        rows = self._conn.execute(
            f"SELECT body FROM passes {where} ORDER BY created_at DESC, pass_id{tail}", params
        ).fetchall()
        return [_load(PassRecord, row["body"]) for row in rows]

    # --------------------------------------------------------------- reviews
    def add_review(self, review: HumanReview) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO reviews(review_id, insight_id, status, by, at, body) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.insight_id,
                    review.status,
                    review.by,
                    _iso(review.at),
                    _dump(review),
                ),
            )

    def latest_review(self, insight_id: str) -> HumanReview | None:
        row = self._conn.execute(
            "SELECT body FROM reviews WHERE insight_id = ? ORDER BY at DESC, review_id DESC LIMIT 1",
            (insight_id,),
        ).fetchone()
        return None if row is None else _load(HumanReview, row["body"])

    def list_reviews(self) -> list[HumanReview]:
        rows = self._conn.execute("SELECT body FROM reviews ORDER BY at, review_id").fetchall()
        return [_load(HumanReview, row["body"]) for row in rows]

    # --------------------------------------------------------- rederivations
    def save_rederivation(self, record: RederivationRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO rederivations(claim_id, pass_id, agrees, created_at, body) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(claim_id, pass_id) DO UPDATE SET "
                "agrees = excluded.agrees, body = excluded.body",
                (
                    record.claim_id,
                    record.pass_id,
                    None if record.agrees is None else int(record.agrees),
                    _iso(record.created_at),
                    _dump(record),
                ),
            )

    def list_rederivations(self, *, claim_id: str | None = None) -> list[RederivationRecord]:
        if claim_id is None:
            rows = self._conn.execute(
                "SELECT body FROM rederivations ORDER BY created_at, claim_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT body FROM rederivations WHERE claim_id = ? ORDER BY created_at",
                (claim_id,),
            ).fetchall()
        return [_load(RederivationRecord, row["body"]) for row in rows]

    def rederived_claim_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT DISTINCT claim_id FROM rederivations").fetchall()
        return {str(row[0]) for row in rows}

    # ---------------------------------------------------- referee calibration
    def pending_canaries(self) -> list[InsightRecord]:
        rows = self._conn.execute(
            "SELECT body FROM insights WHERE hidden = 1 AND status = 'checked' "
            "ORDER BY created_at, insight_id"
        ).fetchall()
        return [_load(InsightRecord, row["body"]) for row in rows]

    def canary_source_ids(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT json_extract(body, '$.canary_of') AS source_id "
            "FROM insights WHERE hidden = 1"
        ).fetchall()
        return {str(row[0]) for row in rows if row[0] is not None}

    def save_calibration(self, record: CalibrationRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO referee_calibration("
                "canary_id, insight_id, pass_id, arm, verdict, created_at, body) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    record.canary_id, record.insight_id, record.pass_id, record.arm,
                    record.verdict, _iso(record.created_at), _dump(record),
                ),
            )

    def calibration_rates(self, *, now: datetime, days: int = 30) -> dict[str, object]:
        from evallab.insights.stats import wilson

        cutoff = now - timedelta(days=days)
        rows = self._conn.execute(
            "SELECT arm, verdict FROM referee_calibration WHERE created_at >= ?",
            (_iso(cutoff),),
        ).fetchall()
        result: dict[str, object] = {"days": days}
        for arm in ("twin", "original"):
            verdicts = [str(row["verdict"]) for row in rows if row["arm"] == arm]
            success = "reject" if arm == "twin" else "accept"
            k = sum(verdict == success for verdict in verdicts)
            result[arm] = {"k": k, "n": len(verdicts), "wilson": wilson(k, len(verdicts))}
        return result

    def mark_scope_warning(self, pass_id: str) -> None:
        """Make a flagged pass's imported claims/cards visibly suspect."""
        note = f"Read-scope warning from worker pass {pass_id}; verify cited evidence independently."
        claim_rows = self._conn.execute(
            "SELECT body FROM claims WHERE pass_id = ?", (pass_id,)
        ).fetchall()
        for row in claim_rows:
            claim = _load(ClaimRecord, row["body"])
            if note not in claim.check.notes:
                check = claim.check.model_copy(update={
                    "status": "warn" if claim.check.status == "pass" else claim.check.status,
                    "notes": [*claim.check.notes, note],
                })
                self.update_claim(claim.model_copy(update={"check": check}))
        insight_rows = self._conn.execute(
            "SELECT body FROM insights WHERE pass_id = ?", (pass_id,)
        ).fetchall()
        for row in insight_rows:
            insight = _load(InsightRecord, row["body"])
            if note not in insight.check.notes:
                check = insight.check.model_copy(update={
                    "status": "warn" if insight.check.status == "pass" else insight.check.status,
                    "notes": [*insight.check.notes, note],
                })
                self.save_insight(insight.model_copy(update={"check": check}))


def _merge_experiment(old: ExperimentRecord, new: ExperimentRecord) -> ExperimentRecord:
    """Keep the union of list fields and prefer newly stated scalars."""

    def union(a: list[str], b: list[str]) -> list[str]:
        return sorted(set(a) | set(b))

    return ExperimentRecord(
        exp_id=old.exp_id,
        title=new.title or old.title,
        lane=new.lane or old.lane,
        prs=union(old.prs, new.prs),
        question=new.question or old.question,
        models=union(old.models, new.models),
        datasets=union(old.datasets, new.datasets),
        spend_usd=new.spend_usd if new.spend_usd is not None else old.spend_usd,
        folder=new.folder or old.folder,
        status_text=new.status_text or old.status_text,
    )


def _claim_from_row(row: sqlite3.Row) -> ClaimRecord:
    """The ``stale`` column is authoritative: sections mark claims stale in SQL."""
    record = _load(ClaimRecord, row["body"])
    stale = bool(row["stale"])
    return record if record.stale == stale else record.model_copy(update={"stale": stale})
