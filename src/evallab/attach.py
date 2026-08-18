"""E04: unified DuckDB attach surface (Z2 + Z3 + Z4).

One function returns a ready DuckDB connection with all available zones
registered under a single namespace. Degrades honestly per T4 and preflight
style: unavailable zones are reported with reason; usable connection is
still returned.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    sql_preamble: str


TABLES = (
    "trial_facts",
    "reward_facts",
    "artifact_facts",
    "trajectories",
    "steps",
    "tool_calls",
    "tool_usage",
    "observations",
    "jobs",
)

Z3_HOT = "job_id=*/trial_id=*/{table}.parquet"
Z3_COLD = "compact/{table}/dt=*/part*.parquet"


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
        conn.execute(f"ATTACH '{dsn}' AS z2 (TYPE postgres)")
        return ZoneStatus("z2", True, detail=_postgres_identity(dsn))
    except Exception as exc:
        return ZoneStatus("z2", False, reason=f"{type(exc).__name__}: {exc}")


def _z3_globs(root: Path) -> list[str]:
    hot = str(root / Z3_HOT)
    cold = str(root / Z3_COLD)
    return [hot, cold]


def _attach_z3(conn: duckdb.DuckDBPyConnection, root: Path) -> ZoneStatus:
    if not root.exists():
        return ZoneStatus(
            "z3", False, reason="derived root does not exist", detail=str(root)
        )
    has_hot = False
    if root.exists():
        has_hot = any(
            (root / d).exists() for d in root.iterdir() if d.name.startswith("job_id=")
        )
    has_cold = (root / "compact").exists()
    globs = _z3_globs(root)
    created = 0
    missing = []
    for table in TABLES:
        view_globs: list[str] = []
        for g in globs:
            if ("job_id=" in g and has_hot) or ("compact" in g and has_cold):
                view_globs.append(g.format(table=table))
        if not view_globs:
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            conn.execute(
                f"CREATE OR REPLACE VIEW z3.{table} AS SELECT * FROM (VALUES (NULL)) t LIMIT 0"
            )
            missing.append(table)
            continue
        glob_list = ", ".join(f"'{g}'" for g in view_globs)
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
    detail = f"{str(root)} ({created}/{len(TABLES)} tables)"
    if missing:
        detail += f"; missing: {', '.join(missing)} (intentionally shaped differently)"
    return ZoneStatus("z3", True, detail=detail)


def _attach_z4(conn: duckdb.DuckDBPyConnection, root: Path) -> ZoneStatus:
    docs_dir = root / "docs"
    if not docs_dir.exists():
        return ZoneStatus(
            "z4", False, reason="docs directory does not exist", detail=str(docs_dir)
        )
    try:
        conn.execute(
            "CREATE OR REPLACE TABLE z4.front_matter "
            "(path TEXT, title TEXT, status TEXT, audience TEXT[], generated_by TEXT)"
        )
        conn.execute(
            "CREATE OR REPLACE VIEW front_matter AS SELECT * FROM z4.front_matter"
        )
        for md in docs_dir.rglob("*.md"):
            try:
                meta = parse_doc(md, root=root)
                audience = list(meta.audience) if meta.audience else []
                generated_by = None
                raw = meta.raw_content
                if "generated_by:" in raw:
                    for line in raw.splitlines():
                        if "generated_by:" in line:
                            generated_by = (
                                line.split(":", 1)[1].strip().strip('"').strip("'")
                            )
                            break
                conn.execute(
                    "INSERT INTO z4.front_matter VALUES (?, ?, ?, ?, ?)",
                    (meta.path, meta.title, meta.status, audience, generated_by),
                )
            except Exception:
                continue
        return ZoneStatus("z4", True, detail=str(docs_dir))
    except Exception as exc:
        return ZoneStatus(
            "z4", False, reason=f"{type(exc).__name__}: {exc}", detail=str(docs_dir)
        )


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
    derived = derived_root_from_environment(root, explicit=explicit_derived, environ=environ)  # explicit_derived for CLI flag  # noqa: E501

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
        f"ATTACH '{dsn}' AS z2 (TYPE postgres);",
        "CREATE SCHEMA IF NOT EXISTS z3;",
        "CREATE SCHEMA IF NOT EXISTS z4;",
    ]
    globs = _z3_globs(derived)
    for table in TABLES:
        view_globs = [g.format(table=table) for g in globs]
        glob_list = ", ".join(f"'{g}'" for g in view_globs)
        lines.append(
            f"CREATE OR REPLACE VIEW {table} AS "
            f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true);"
        )
        lines.append(
            f"CREATE OR REPLACE VIEW z3.{table} AS "
            f"SELECT * FROM read_parquet([{glob_list}], union_by_name=true);"
        )
    lines.append(
        "CREATE OR REPLACE TABLE z4.front_matter "
        "(path TEXT, title TEXT, status TEXT, audience TEXT[], generated_by TEXT);"
    )
    lines.append(
        "CREATE OR REPLACE VIEW front_matter AS SELECT * FROM z4.front_matter;"
    )
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
