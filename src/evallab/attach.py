"""E04: unified DuckDB attach surface (Z2 + Z3 + Z4).

One function returns a ready DuckDB connection with all available zones
registered under a single namespace. Degrades honestly per T4 and preflight
style: unavailable zones are reported with reason; usable connection is
still returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from evallab.contextpack import parse_doc
from evallab.paths import derived_root_from_environment
from evallab.runner import database_url_from_environment


@dataclass(frozen=True)
class ZoneStatus:
    """Outcome for one storage zone."""

    name: str
    attached: bool
    reason: str | None = None
    detail: str | None = None


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
)

Z3_HOT = "job_id=*/trial_id=*/{table}.parquet"
Z3_COLD = "compact/{table}/dt=*/part*.parquet"
Z3_STANDALONE_DIR = "{table}/*.parquet"


def _sql_string_literal(value: str) -> str:
    """Return *value* as a safely quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


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


def _z3_globs(root: Path) -> list[str]:
    hot = str(root / Z3_HOT)
    cold = str(root / Z3_COLD)
    standalone_dir = str(root / Z3_STANDALONE_DIR)
    return [hot, cold, standalone_dir]


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
    if not root.exists():
        _attach_semantic_comparison(conn, available_tables=set())
        return ZoneStatus("z3", False, reason="derived root does not exist", detail=str(root))

    hot_tables = {p.stem for p in root.glob("job_id=*/trial_id=*/*.parquet")}
    cold_tables = {p.parent.parent.name for p in root.glob("compact/*/dt=*/part*.parquet")}
    standalone_tables = {
        p.parent.name
        for p in root.glob("*/*.parquet")
        if p.parent.parent == root
        and not p.parent.name.startswith("job_id=")
        and p.parent.name != "compact"
    }
    root_tables = {p.stem for p in root.glob("*.parquet") if p.parent == root}

    created = 0
    missing = []
    for table in TABLES:
        view_globs: list[str] = []
        if table in hot_tables:
            view_globs.append(str(root / Z3_HOT.format(table=table)))
        if table in cold_tables:
            view_globs.append(str(root / Z3_COLD.format(table=table)))
        if table in standalone_tables:
            view_globs.append(str(root / Z3_STANDALONE_DIR.format(table=table)))
        if table in root_tables:
            view_globs.append(str(root / f"{table}.parquet"))
        if not view_globs:
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            conn.execute(
                f"CREATE OR REPLACE VIEW z3.{table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            missing.append(table)
            continue
        glob_list = ", ".join(_sql_string_literal(g) for g in view_globs)
        try:
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS "
                f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true)"
            )
            conn.execute(
                f"CREATE OR REPLACE VIEW z3.{table} AS "
                f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true)"
            )
            created += 1
        except Exception:
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            conn.execute(
                f"CREATE OR REPLACE VIEW z3.{table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            missing.append(table)
    _attach_semantic_comparison(
        conn,
        available_tables=hot_tables | cold_tables | standalone_tables,
    )
    detail = f"{str(root)} ({created}/{len(TABLES)} tables)"
    if missing:
        detail += f"; missing: {', '.join(missing)} (intentionally shaped differently)"
    return ZoneStatus("z3", True, detail=detail)


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
    """Return a DuckDB connection with available zones attached under one namespace.

    Z2 uses postgres_scanner against DATABASE_URL.
    Z3 registers views over hot + cold Parquet using derived_root_from_environment.
    Z4 materializes front_matter table from docs/ using parse_doc.
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
    globs = _z3_globs(derived)
    for table in TABLES:
        view_globs = [g.format(table=table) for g in globs]
        glob_list = ", ".join(_sql_string_literal(g) for g in view_globs)
        lines.append(
            f"CREATE OR REPLACE VIEW {table} AS "
            f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true);"
        )
        lines.append(
            f"CREATE OR REPLACE VIEW z3.{table} AS "
            f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true);"
        )
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
    for z in zones:
        if z.attached:
            print(f"{z.name}: attached ({z.detail or ''})")
        else:
            print(f"{z.name}: unavailable — {z.reason} (examined: {z.detail or ''})")


def attach_and_query(sql: str, **kwargs: Any) -> list[tuple[Any, ...]]:
    result = attach(**kwargs)
    rows = result.connection.execute(sql).fetchall()
    result.connection.close()
    return rows
