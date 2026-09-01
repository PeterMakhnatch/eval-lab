"""E04: unified DuckDB attach surface (Z2 + Z3 + Z4).

One function returns a ready DuckDB connection with all available zones
registered under a single namespace. Degrades honestly per T4 and preflight
style: unavailable zones are reported with reason; usable connection is
still returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import duckdb

from evallab.contextpack import parse_doc
from evallab.runner import database_url_from_environment
from evallab.storage.paths import derived_root_from_environment
from evallab.storage.settlement import (
    ProjectionColumn,
    ProjectionTableContract,
    active_settlement_manifests,
    load_settlement_manifests,
    verify_ready_manifest,
)

TableReadinessState = Literal[
    "ready",
    "missing",
    "failed",
    "stale",
    "not_applicable",
]
ZoneReadinessState = Literal["ready", "partial", "unavailable"]


@dataclass(frozen=True)
class TableReadiness:
    """Manifest-derived readiness for one logical Z3 relation."""

    table_name: str
    state: TableReadinessState
    reason: str
    paths: tuple[Path, ...] = ()
    contract: ProjectionTableContract | None = None


@dataclass(frozen=True)
class ZoneStatus:
    """Outcome for one storage zone."""

    name: str
    attached: bool
    reason: str | None = None
    detail: str | None = None
    state: ZoneReadinessState | None = None
    tables: tuple[TableReadiness, ...] = ()

    def __post_init__(self) -> None:
        if self.state is None:
            object.__setattr__(
                self,
                "state",
                "ready" if self.attached else "unavailable",
            )


@dataclass(frozen=True)
class AttachResult:
    """Result of building the unified attach surface."""

    connection: duckdb.DuckDBPyConnection
    zones: tuple[ZoneStatus, ...]
    sql_preamble: str = field(repr=False)


TABLES = (
    "trial_facts",
    "reward_facts",
    "artifact_facts",
    "trajectories",
    "steps",
    "tool_calls",
    "tool_usage",
    "observations",
    "state_changes",
    "state_events",
    "trajectory_events",
    "agent_actions",
    "llm_calls",
    "trajectory_phases",
    "action_effects",
    "jobs",
    "traj_features",
    "behavior_labels",
    "behavior_episodes",
    "capability_opportunities",
    "process_step_facts",
    "retrieval_facts",
    "constraint_facts",
    "context_operation_facts",
    "paired_condition_facts",
    "session_dependency_facts",
    "evidence_coverage",
    "semantic_action_facts",
    "semantic_action_coverage",
    "trajectory_quality_reports",
    "trajectory_quality_findings",
    "interpretation_artifacts",
    "machine_judgments",
    "acceptance_decisions",
    "inspect_runs",
    "inspect_attempts",
    "inspect_scores",
    "inspect_events",
    "inspect_attachments",
)


def _sql_string_literal(value: str) -> str:
    """Return *value* as a safely quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _manifest_table_readiness(root: Path) -> tuple[TableReadiness, ...]:
    inventory = load_settlement_manifests(root)
    if inventory.errors:
        reason = "; ".join(f"{error.path}: {error.reason}" for error in inventory.errors)
        return tuple(TableReadiness(table, "stale", reason) for table in TABLES)

    manifests = active_settlement_manifests(inventory)
    entries: dict[str, list[TableReadiness]] = {table: [] for table in TABLES}
    for manifest in manifests:
        contracts = {contract.key: contract for contract in manifest.contract.tables}
        if manifest.state == "ready":
            try:
                if not verify_ready_manifest(root, manifest):
                    raise ValueError("ready settlement manifest failed verification")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                for contract in manifest.contract.tables:
                    if contract.table_name in entries:
                        entries[contract.table_name].append(
                            TableReadiness(
                                contract.table_name,
                                "stale",
                                reason,
                                contract=contract,
                            )
                        )
                continue
            for table in manifest.tables:
                contract = contracts[table.key]
                if table.table_name not in entries:
                    continue
                if table.state == "ready":
                    entries[table.table_name].append(
                        TableReadiness(
                            table.table_name,
                            "ready",
                            f"settlement={manifest.settlement_id}",
                            paths=(root.resolve() / table.relative_path,),
                            contract=contract,
                        )
                    )
                else:
                    entries[table.table_name].append(
                        TableReadiness(
                            table.table_name,
                            "not_applicable",
                            table.failure_reason or "explicitly not applicable",
                            contract=contract,
                        )
                    )
            continue

        reason = f"settlement={manifest.settlement_id} state={manifest.state}" + (
            f" reason={manifest.failure_reason}" if manifest.failure_reason else ""
        )
        for contract in manifest.contract.tables:
            if contract.table_name in entries:
                entries[contract.table_name].append(
                    TableReadiness(
                        contract.table_name,
                        "failed",
                        reason,
                        contract=contract,
                    )
                )

    readiness: list[TableReadiness] = []
    for table_name in TABLES:
        table_entries = entries[table_name]
        if not table_entries:
            readiness.append(
                TableReadiness(
                    table_name,
                    "missing",
                    "no active settlement contract",
                )
            )
            continue
        blocked = [entry for entry in table_entries if entry.state in {"failed", "stale"}]
        if blocked:
            state: TableReadinessState = (
                "stale" if any(entry.state == "stale" for entry in blocked) else "failed"
            )
            readiness.append(
                TableReadiness(
                    table_name,
                    state,
                    "; ".join(sorted({entry.reason for entry in blocked})),
                )
            )
            continue
        ready_entries = [entry for entry in table_entries if entry.state == "ready"]
        contracts = [entry.contract for entry in table_entries if entry.contract is not None]
        schema_digests = {contract.schema_digest for contract in contracts}
        if len(schema_digests) != 1:
            readiness.append(
                TableReadiness(
                    table_name,
                    "stale",
                    "active settlements disagree on schema digest",
                )
            )
            continue
        contract = contracts[0]
        if ready_entries:
            paths = tuple(
                sorted(
                    {path for entry in ready_entries for path in entry.paths},
                    key=str,
                )
            )
            readiness.append(
                TableReadiness(
                    table_name,
                    "ready",
                    f"{len(paths)} verified manifest-bound partition(s)",
                    paths=paths,
                    contract=contract,
                )
            )
        else:
            readiness.append(
                TableReadiness(
                    table_name,
                    "not_applicable",
                    "all active contracts explicitly mark the table not applicable",
                    contract=contract,
                )
            )
    return tuple(readiness)


def _duckdb_type(column: ProjectionColumn) -> str:
    data_type = column.arrow_type
    direct = {
        "null": "VARCHAR",
        "bool": "BOOLEAN",
        "int8": "TINYINT",
        "int16": "SMALLINT",
        "int32": "INTEGER",
        "int64": "BIGINT",
        "uint8": "UTINYINT",
        "uint16": "USMALLINT",
        "uint32": "UINTEGER",
        "uint64": "UBIGINT",
        "float": "REAL",
        "double": "DOUBLE",
        "string": "VARCHAR",
        "large_string": "VARCHAR",
        "binary": "BLOB",
        "large_binary": "BLOB",
        "date32[day]": "DATE",
        "date64[ms]": "DATE",
    }
    if data_type in direct:
        return direct[data_type]
    if data_type.startswith("timestamp["):
        return "TIMESTAMPTZ" if "tz=" in data_type else "TIMESTAMP"
    if data_type.startswith("duration["):
        return "INTERVAL"
    if data_type.startswith("decimal128(") or data_type.startswith("decimal256("):
        precision_scale = data_type[data_type.index("(") + 1 : -1]
        return f"DECIMAL({precision_scale})"
    if data_type.startswith("list<") or data_type.startswith("large_list<"):
        return "VARCHAR[]"
    raise ValueError(f"unsupported registered Arrow type for DuckDB: {data_type}")


def _typed_empty_select(contract: ProjectionTableContract) -> str:
    columns = ", ".join(
        f"CAST(NULL AS {_duckdb_type(column)}) AS {_quote_identifier(column.name)}"
        for column in contract.columns
    )
    return f"SELECT {columns} WHERE FALSE"


def _readiness_rows(
    readiness: tuple[TableReadiness, ...],
) -> list[tuple[str, str, str, int]]:
    return [(item.table_name, item.state, item.reason, len(item.paths)) for item in readiness]


def _postgres_dsn() -> str:
    return database_url_from_environment()


def _postgres_identity(dsn: str) -> str:
    try:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(dsn)
        host = str(info.get("host") or "localhost")
        port = str(info.get("port") or "5432")
        dbname = str(info.get("dbname") or "")
        return f"{host}:{port}/{dbname}" if dbname else f"{host}:{port}"
    except Exception:
        return "unparsable"


def _attach_z2(conn: duckdb.DuckDBPyConnection, dsn: str) -> ZoneStatus:
    try:
        conn.execute("INSTALL postgres_scanner")
        conn.execute("LOAD postgres_scanner")
        conn.execute(f"ATTACH {_sql_string_literal(dsn)} AS z2 (TYPE postgres)")
        return ZoneStatus("z2", True, detail=_postgres_identity(dsn))
    except Exception as exc:
        detail = str(exc)
        for candidate in (dsn, dsn.replace("'", "''")):
            detail = detail.replace(candidate, "<REDACTED DSN>")
        return ZoneStatus("z2", False, reason=f"{type(exc).__name__}: {detail}")


SEMANTIC_COMPARISON_COLUMNS = (
    "job_id",
    "trial_id",
    "document_id",
    "tool_call_id",
    "action_id",
    "function_name",
    "mechanical_outcome",
    "exit_code",
    "mechanical_arguments_sha256",
    "task_id",
    "binding_digest",
    "profile_id",
    "profile_version",
    "profile_digest",
    "semantic_role",
    "semantic_outcome",
    "reason_code",
    "detail_digest",
    "detail_size",
    "observation_correlation",
    "correlation_reason",
    "intervention_provenance",
    "intervention_sha256",
    "intervention_length",
    "intervention_reason",
)


def _empty_semantic_comparison_sql() -> str:
    columns = []
    for name in SEMANTIC_COMPARISON_COLUMNS:
        sql_type = (
            "BIGINT" if name in {"exit_code", "detail_size", "intervention_length"} else "VARCHAR"
        )
        columns.append(f"CAST(NULL AS {sql_type}) AS {name}")
    return "SELECT " + ", ".join(columns) + " WHERE FALSE"


def _semantic_comparison_sql(
    mechanical_relation: str,
    semantic_relation: str,
) -> str:
    return f"""
        SELECT
            mechanical.job_id,
            mechanical.trial_id,
            mechanical.document_id,
            mechanical.tool_call_id,
            mechanical.action_id,
            mechanical.function_name,
            mechanical.outcome AS mechanical_outcome,
            mechanical.exit_code,
            mechanical.arguments_sha256 AS mechanical_arguments_sha256,
            semantic.task_id,
            semantic.binding_digest,
            semantic.profile_id,
            semantic.profile_version,
            semantic.profile_digest,
            semantic.role AS semantic_role,
            semantic.outcome AS semantic_outcome,
            semantic.reason_code,
            semantic.detail_digest,
            semantic.detail_size,
            semantic.observation_correlation,
            semantic.correlation_reason,
            semantic.intervention_provenance,
            semantic.intervention_sha256,
            semantic.intervention_length,
            semantic.intervention_reason
        FROM {mechanical_relation} AS mechanical
        LEFT JOIN {semantic_relation} AS semantic
          ON mechanical.job_id = semantic.job_id
         AND mechanical.trial_id = semantic.trial_id
         AND mechanical.document_id = semantic.document_id
         AND (
              mechanical.tool_call_id IS NOT NULL
              AND semantic.tool_call_id IS NOT NULL
              AND mechanical.tool_call_id = semantic.tool_call_id
              OR (
                  mechanical.action_id = semantic.action_id
                  AND (
                      mechanical.tool_call_id IS NULL
                      OR semantic.tool_call_id IS NULL
                  )
              )
         )
    """


def _attach_semantic_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    available_tables: set[str],
) -> None:
    if {"agent_actions", "semantic_action_facts"} <= available_tables:
        comparison = _semantic_comparison_sql("agent_actions", "semantic_action_facts")
        z3_comparison = _semantic_comparison_sql("z3.agent_actions", "z3.semantic_action_facts")
    else:
        comparison = _empty_semantic_comparison_sql()
        z3_comparison = comparison
    try:
        conn.execute(f"CREATE OR REPLACE VIEW v_semantic_vs_mechanical AS {comparison}")
        conn.execute(f"CREATE OR REPLACE VIEW z3.v_semantic_vs_mechanical AS {z3_comparison}")
    except Exception:
        # A discovered file can still be malformed or use an older schema. Do not
        # expose a partial or guessed comparison in that case.
        empty = _empty_semantic_comparison_sql()
        conn.execute(f"CREATE OR REPLACE VIEW v_semantic_vs_mechanical AS {empty}")
        conn.execute(f"CREATE OR REPLACE VIEW z3.v_semantic_vs_mechanical AS {empty}")


def _attach_z3(conn: duckdb.DuckDBPyConnection, root: Path) -> ZoneStatus:
    readiness = list(_manifest_table_readiness(root))
    available_tables: set[str] = set()

    for index, table in enumerate(readiness):
        identifier = _quote_identifier(table.table_name)
        if table.state == "ready":
            sources = ", ".join(_sql_string_literal(str(path)) for path in table.paths)
            select_sql = f"SELECT * FROM read_parquet([{sources}])"
        elif table.state == "not_applicable" and table.contract is not None:
            select_sql = _typed_empty_select(table.contract)
        else:
            continue
        try:
            conn.execute(f"CREATE OR REPLACE VIEW {identifier} AS {select_sql}")
            conn.execute(f"CREATE OR REPLACE VIEW z3.{identifier} AS {select_sql}")
            if table.state == "ready":
                available_tables.add(table.table_name)
        except Exception as exc:
            readiness[index] = TableReadiness(
                table.table_name,
                "stale",
                f"attach failed: {type(exc).__name__}: {exc}",
                paths=table.paths,
                contract=table.contract,
            )

    readiness_tuple = tuple(readiness)
    conn.execute(
        "CREATE OR REPLACE TABLE z3.table_readiness "
        "(table_name VARCHAR, state VARCHAR, reason VARCHAR, path_count BIGINT)"
    )
    conn.executemany(
        "INSERT INTO z3.table_readiness VALUES (?, ?, ?, ?)",
        _readiness_rows(readiness_tuple),
    )
    conn.execute("CREATE OR REPLACE VIEW table_readiness AS SELECT * FROM z3.table_readiness")
    _attach_semantic_comparison(conn, available_tables=available_tables)

    admitted = sum(table.state in {"ready", "not_applicable"} for table in readiness_tuple)
    ready_count = sum(table.state == "ready" for table in readiness_tuple)
    if admitted == len(TABLES):
        state: ZoneReadinessState = "ready"
        reason = None
    elif admitted:
        state = "partial"
        reason = "manifest coverage is incomplete"
    else:
        state = "unavailable"
        reason = (
            "derived root does not exist" if not root.exists() else "no manifest-admitted tables"
        )
    detail = (
        f"{root} ({ready_count} ready, "
        f"{admitted - ready_count} not applicable, "
        f"{len(TABLES) - admitted} blocked)"
    )
    return ZoneStatus(
        "z3",
        state == "ready",
        reason=reason,
        detail=detail,
        state=state,
        tables=readiness_tuple,
    )


def _attach_z4(conn: duckdb.DuckDBPyConnection, root: Path) -> ZoneStatus:
    docs_dir = root / "docs"
    if not docs_dir.exists():
        return ZoneStatus("z4", False, reason="docs directory does not exist", detail=str(docs_dir))
    try:
        conn.execute(
            "CREATE OR REPLACE TABLE z4.front_matter "
            "(path TEXT, title TEXT, status TEXT, audience TEXT[], generated_by TEXT)"
        )
        conn.execute("CREATE OR REPLACE VIEW front_matter AS SELECT * FROM z4.front_matter")
        for md in docs_dir.rglob("*.md"):
            try:
                meta = parse_doc(md, root=root)
                audience = list(meta.audience) if meta.audience else []
                generated_by = None
                raw = meta.raw_content
                if "generated_by:" in raw:
                    for line in raw.splitlines():
                        if "generated_by:" in line:
                            generated_by = line.split(":", 1)[1].strip().strip('"').strip("'")
                            break
                conn.execute(
                    "INSERT INTO z4.front_matter VALUES (?, ?, ?, ?, ?)",
                    (meta.path, meta.title, meta.status, audience, generated_by),
                )
            except Exception:
                continue
        return ZoneStatus("z4", True, detail=str(docs_dir))
    except Exception as exc:
        return ZoneStatus("z4", False, reason=f"{type(exc).__name__}: {exc}", detail=str(docs_dir))


def attach(
    *,
    repo_root: Path | None = None,
    explicit_derived: Path | None = None,
    environ: dict[str, str] | None = None,
) -> AttachResult:
    """Return a DuckDB connection with honestly reported storage zones.

    Z2 uses ``postgres_scanner`` against ``DATABASE_URL``. Z3 admits only exact
    files from verified ready settlement manifests. Z4 materializes repository
    document front matter.
    """
    root = repo_root or Path.cwd()
    dsn = _postgres_dsn()
    derived = derived_root_from_environment(
        root, explicit=explicit_derived, environ=environ
    )  # explicit_derived for CLI flag  # noqa: E501

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS z3")
    conn.execute("CREATE SCHEMA IF NOT EXISTS z4")
    z2 = _attach_z2(conn, dsn)
    z3 = _attach_z3(conn, derived)
    z4 = _attach_z4(conn, root)

    zones = (z2, z3, z4)
    sql = build_sql_preamble(dsn, derived, root)
    return AttachResult(conn, zones, sql)


def build_sql_preamble(dsn: str, derived: Path, root: Path) -> str:
    lines = [
        "INSTALL postgres_scanner;",
        "LOAD postgres_scanner;",
        f"ATTACH {_sql_string_literal(dsn)} AS z2 (TYPE postgres);",
        "CREATE SCHEMA IF NOT EXISTS z3;",
        "CREATE SCHEMA IF NOT EXISTS z4;",
    ]
    readiness = _manifest_table_readiness(derived)
    available_tables: set[str] = set()
    for table in readiness:
        identifier = _quote_identifier(table.table_name)
        if table.state == "ready":
            sources = ", ".join(_sql_string_literal(str(path)) for path in table.paths)
            select_sql = f"SELECT * FROM read_parquet([{sources}])"
            available_tables.add(table.table_name)
        elif table.state == "not_applicable" and table.contract is not None:
            select_sql = _typed_empty_select(table.contract)
        else:
            continue
        lines.append(f"CREATE OR REPLACE VIEW {identifier} AS {select_sql};")
        lines.append(f"CREATE OR REPLACE VIEW z3.{identifier} AS {select_sql};")
    lines.append(
        "CREATE OR REPLACE TABLE z3.table_readiness "
        "(table_name VARCHAR, state VARCHAR, reason VARCHAR, path_count BIGINT);"
    )
    for table_name, state, reason, path_count in _readiness_rows(readiness):
        lines.append(
            "INSERT INTO z3.table_readiness VALUES "
            f"({_sql_string_literal(table_name)}, {_sql_string_literal(state)}, "
            f"{_sql_string_literal(reason)}, {path_count});"
        )
    lines.append("CREATE OR REPLACE VIEW table_readiness AS SELECT * FROM z3.table_readiness;")
    if {"agent_actions", "semantic_action_facts"} <= available_tables:
        lines.append(
            "CREATE OR REPLACE VIEW v_semantic_vs_mechanical AS "
            + _semantic_comparison_sql("agent_actions", "semantic_action_facts")
            + ";"
        )
        lines.append(
            "CREATE OR REPLACE VIEW z3.v_semantic_vs_mechanical AS "
            + _semantic_comparison_sql("z3.agent_actions", "z3.semantic_action_facts")
            + ";"
        )
    lines.append(
        "CREATE OR REPLACE TABLE z4.front_matter "
        "(path TEXT, title TEXT, status TEXT, audience TEXT[], generated_by TEXT);"
    )
    lines.append("CREATE OR REPLACE VIEW front_matter AS SELECT * FROM z4.front_matter;")
    return "\n".join(lines)


def print_zones(zones: tuple[ZoneStatus, ...]) -> None:
    for zone in zones:
        if zone.state == "ready":
            print(f"{zone.name}: ready ({zone.detail or ''})")
        else:
            print(f"{zone.name}: {zone.state} — {zone.reason} (examined: {zone.detail or ''})")


def attach_and_query(sql: str, **kwargs: Any) -> list[tuple[Any, ...]]:
    result = attach(**kwargs)
    rows = result.connection.execute(sql).fetchall()
    result.connection.close()
    return rows
