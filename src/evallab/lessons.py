"""Statistical lesson aggregation views and findings engine (WS-D).

Executes DuckDB aggregation views joining craft facets, trial facts,
analysis sidecars, and observation records. Applies statistical gating with
Wilson 95% confidence intervals (via `evallab.cohort.wilson_interval`). Rows
below the power threshold are labeled 'insufficient n' and never reported
as generalized findings. Refuse-to-rank propagates from `evallab.cohort`.

Generates `research/lessons.md` with header `generated-by: lessons v1`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from evallab.cohort import NOT_COMPARABLE, wilson_interval
from evallab.craft import CRAFT_SCHEMA, CraftRecord, TaskSource, scan
from evallab.evidence.facts import TRIAL_FACT_SCHEMA
from evallab.interpretation.trajectory_quality import QUALITY_REPORT_TABLE
from evallab.lineage import compute_file_digest, resolve_lineage

GENERATED_HEADER = "generated-by: lessons v1"
DEFAULT_POWER_THRESHOLD = 5
SQL_LESSONS_PATH = Path("sql/lessons.sql")

ANALYSIS_SIDECAR_SCHEMA = pa.schema(
    [
        pa.field("analysis_id", pa.string()),
        pa.field("job_id", pa.string()),
        pa.field("source_trial_id", pa.string()),
        pa.field("validity", pa.string()),
        pa.field("primary_category", pa.string()),
        pa.field("summary", pa.string()),
        pa.field("earliest_failure_step_id", pa.int64()),
        pa.field("confidence", pa.string()),
        pa.field("validation_status", pa.string()),
        pa.field("source_path", pa.string()),
        pa.field("source_digest", pa.string()),
    ]
)

OBSERVATION_RECORD_SCHEMA = pa.schema(
    [
        pa.field("trial_id", pa.string()),
        pa.field("trial_name", pa.string()),
        pa.field("job", pa.string()),
        pa.field("agent", pa.string()),
        pa.field("model", pa.string()),
        pa.field("task", pa.string()),
        pa.field("reward", pa.float64()),
        pa.field("steps_taken", pa.int64()),
        pa.field("first_failure_step", pa.int64()),
        pa.field("loop_detected", pa.bool_()),
        pa.field("loop_step", pa.int64()),
        pa.field("verified_before_done", pa.bool_()),
        pa.field("tool_errors", pa.int64()),
        pa.field("summary", pa.string()),
    ]
)


TRIAL_FACTS_LEDGER_SCHEMA = pa.schema(
    [
        *TRIAL_FACT_SCHEMA,
        pa.field("quality_status", pa.string()),
    ]
)

QUALITY_STATUS_COLUMNS = (
    "quality_pass_n",
    "quality_warn_n",
    "quality_fail_n",
    "quality_quarantine_n",
)


@dataclass(frozen=True)
class LessonRow:
    """One statistically-gated lesson row derived from an aggregation view."""

    lesson_id: str
    view_name: str
    dimension: str
    metric_name: str
    n: int
    k: int
    rate: float
    wilson_95: tuple[float, float] | None
    powered: bool
    status: str
    finding: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LessonRanking:
    """Pairwise comparative ranking with refuse-to-rank propagated from cohort."""

    view_name: str
    dimension_a: str
    dimension_b: str
    rankable: bool
    ranking: str | None
    statement: str
    refusal_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LessonsResult:
    """Aggregate result from executing all lesson views."""

    generated_at: datetime
    power_threshold: int
    total_lessons: int
    powered_lessons: int
    underpowered_lessons: int
    lessons_by_view: dict[str, list[LessonRow]]
    records_summary: dict[str, int]
    inputs: tuple[dict[str, str], ...] = ()
    rankings_by_view: dict[str, list[LessonRanking]] = field(default_factory=dict)


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def collect_lessons_inputs(
    root: Path,
    sql_path: Path | None = None,
    quality_ledger: QualityLedgerRead | None = None,
    trial_parquet_partition_limit: int = 100,
) -> list[dict[str, str]]:
    """Collect all upstream input files aggregated by the lessons generator."""
    resolved_sql = sql_path if sql_path is not None else root / SQL_LESSONS_PATH
    inputs: list[dict[str, str]] = []

    # 1. SQL view file
    if resolved_sql.is_file():
        inputs.append(
            {
                "path": _relative_path(resolved_sql, root),
                "digest": compute_file_digest(resolved_sql),
            }
        )

    # 2. Craft sources
    craft_parquet = root / "derived/parquet/craft/craft.parquet"
    if craft_parquet.is_file():
        inputs.append(
            {
                "path": _relative_path(craft_parquet, root),
                "digest": compute_file_digest(craft_parquet),
            }
        )
    else:
        task_dirs: list[Path] = []
        lib_tasks = root / "library/tasks"
        if lib_tasks.is_dir():
            for p in sorted(lib_tasks.iterdir()):
                if p.is_dir() and (p / "task.toml").is_file():
                    task_dirs.append(p / "task.toml")
        demos = root / "research/explorations/harbor-021/demos/tasks"
        if demos.is_dir():
            for p in sorted(demos.iterdir()):
                if p.is_dir() and (p / "task.toml").is_file():
                    task_dirs.append(p / "task.toml")
        for tf in task_dirs:
            inputs.append(
                {
                    "path": _relative_path(tf, root),
                    "digest": compute_file_digest(tf),
                }
            )

    # 3. Observation records
    obs_dir = root / "research/observations"
    if obs_dir.is_dir():
        for md_path in sorted(obs_dir.rglob("*.md")):
            if md_path.name in {"CHECKLIST.md", "TEMPLATE.md"}:
                continue
            if md_path.is_file():
                inputs.append(
                    {
                        "path": _relative_path(md_path, root),
                        "digest": compute_file_digest(md_path),
                    }
                )

    candidate_dirs = [
        root / "derived/analysis",
        root / "research/analysis",
        root / "research/explorations/harbor-021/captures/analyze",
    ]
    for cdir in candidate_dirs:
        if not cdir.is_dir():
            continue
        for path in sorted(cdir.rglob("analysis.json")):
            if path.is_file():
                inputs.append(
                    {
                        "path": _relative_path(path, root),
                        "digest": compute_file_digest(path),
                    }
                )
    # 5. Trial facts parquet partitions (if present)
    trial_parquet_files = sorted(root.glob("derived/parquet/**/trial_facts.parquet"))
    if trial_parquet_files:
        if len(trial_parquet_files) <= trial_parquet_partition_limit:
            for pf in trial_parquet_files:
                if pf.is_file():
                    inputs.append(
                        {
                            "path": _relative_path(pf, root),
                            "digest": compute_file_digest(pf),
                        }
                    )
        else:
            # Composite digest over sorted member digests for large collections
            h = hashlib.sha256()
            for pf in trial_parquet_files:
                h.update(compute_file_digest(pf).encode("utf-8"))
            inputs.append(
                {
                    "path": "derived/parquet/**/trial_facts.parquet",
                    "digest": f"sha256:{h.hexdigest()}",
                }
            )


    # 6. Evidence Quality Ledger (shared derived store). When a bound read is
    # supplied, its digest — taken from the same bytes the rows were parsed
    # from — is recorded verbatim, closing the read/digest TOCTOU window.
    bound = quality_ledger if quality_ledger is not None else load_quality_ledger_bound(root)
    if bound.path is not None and bound.digest is not None:
        inputs.append(
            {
                "path": bound.path,
                "digest": bound.digest,
            }
        )

    inputs.sort(key=lambda x: x["path"])
    return inputs


# --------------------------------------------------------------------------- #
# Data Loaders
# --------------------------------------------------------------------------- #


def parse_observation_markdown(path: Path) -> dict[str, Any] | None:
    """Parse one observatory-1 markdown observation record."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if "Observation record" not in content and "template_version" not in content:
        return None

    data: dict[str, Any] = {}
    for line in content.splitlines():
        match = re.match(r"^\s*-\s*\*\*([a-zA-Z0-9_]+):\*\*\s*(.*?)\s*$", line)
        if match:
            key, val = match.group(1), match.group(2).strip()
            data[key] = val

    trial_id = data.get("trial_id")
    if not trial_id:
        return None

    reward_str = data.get("reward", "none")
    reward: float | None = None
    if reward_str not in {"none", "", "null"}:
        try:
            reward = float(reward_str)
        except ValueError:
            reward = None

    steps_str = data.get("steps_taken", "0")
    try:
        steps_taken = int(steps_str)
    except ValueError:
        steps_taken = 0

    first_failure_str = data.get("first_failure_step", "none")
    first_failure_step: int | None = None
    if first_failure_str not in {"none", "", "null"}:
        try:
                        first_failure_step = int(first_failure_str)
        except ValueError:
            first_failure_step = None

    loop_str = data.get("loop_detected", "no").lower()
    loop_detected = loop_str in {"yes", "true", "1"}

    loop_step_str = data.get("loop_step", "none")
    loop_step: int | None = None
    if loop_step_str not in {"none", "", "null"}:
        try:
            loop_step = int(loop_step_str)
        except ValueError:
            loop_step = None

    verified_str = data.get("verified_before_done", "no").lower()
    verified_before_done = verified_str in {"yes", "true", "1"}

    tool_errors_str = data.get("tool_errors", "0")
    try:
        tool_errors = int(tool_errors_str)
    except ValueError:
        tool_errors = 0

    return {
        "trial_id": str(trial_id),
        "trial_name": data.get("trial_name", ""),
        "job": data.get("job", ""),
        "agent": data.get("agent", ""),
        "model": data.get("model", ""),
        "task": data.get("task", ""),
        "reward": reward,
        "steps_taken": steps_taken,
        "first_failure_step": first_failure_step,
        "loop_detected": loop_detected,
        "loop_step": loop_step,
        "verified_before_done": verified_before_done,
        "tool_errors": tool_errors,
        "summary": data.get("summary", ""),
    }


def load_observation_records(root: Path) -> list[dict[str, Any]]:
    """Discover and parse all observation records under research/observations."""
    obs_dir = root / "research/observations"
    if not obs_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for md_path in sorted(obs_dir.rglob("*.md")):
        if md_path.name in {"CHECKLIST.md", "TEMPLATE.md"}:
            continue
        parsed = parse_observation_markdown(md_path)
        if parsed is not None:
            records.append(parsed)
    return records


def load_craft_records(root: Path) -> list[dict[str, Any]]:
    """Load craft records from parquet or scan repository task corpora."""
    parquet_path = root / "derived/parquet/craft/craft.parquet"
    if parquet_path.is_file():
        try:
            with duckdb.connect(":memory:") as con:
                rows = con.execute("SELECT * FROM read_parquet(?)", [str(parquet_path)]).fetchall()
                cols = [desc[0] for desc in con.description]
                return [dict(zip(cols, r, strict=False)) for r in rows]
        except Exception:
            pass

    # Fallback to deterministic scan
    sources: list[TaskSource] = []
    if (root / "library/tasks").is_dir():
        sources.append(
            TaskSource(
                root=root / "library/tasks",
                source_repo="local-lab/library",
                label="library",
            )
        )
    if (root / "research/explorations/harbor-021/demos/tasks").is_dir():
        sources.append(
            TaskSource(
                root=root / "research/explorations/harbor-021/demos/tasks",
                source_repo="local-lab/harbor-021-demos",
                label="harbor-021",
            )
        )

    if not sources:
        return []

    scan_result = scan(sources)
    return [_craft_record_to_dict(r) for r in scan_result.records]


def _craft_record_to_dict(r: CraftRecord) -> dict[str, Any]:
    return {
        "task_ref": r.task_ref,
        "source_repo": r.source_repo,
        "version": r.version,
        "task_digest": r.task_digest,
        "instruction_chars": r.instruction_chars,
        "instruction_style": str(r.instruction_style) if r.instruction_style else None,
        "env_n_files": r.env_n_files,
        "env_languages": list(r.env_languages),
        "env_services_n": r.env_services_n,
        "env_multi_container": r.env_multi_container,
        "verifier_type": str(r.verifier_type) if r.verifier_type else None,
        "anti_cheat": [str(x) for x in r.anti_cheat],
        "answer_hiding": r.answer_hiding,
        "difficulty_mechanism": str(r.difficulty_mechanism) if r.difficulty_mechanism else None,
        "human_minutes": r.human_minutes,
        "pinned_deps": r.pinned_deps,
        "facets_schema_version": r.facets_schema_version,
        "verifier_signals": list(r.verifier_signals),
        "unresolved_facets": list(r.unresolved_facets),
        "base_image_pin": str(r.base_image_pin) if r.base_image_pin else None,
    }


def load_analysis_sidecars(root: Path) -> list[dict[str, Any]]:
    """Discover validated production analysis sidecars with file provenance."""
    sidecars: list[dict[str, Any]] = []
    candidate_dirs = [
        root / "derived/analysis",
        root / "research/analysis",
        root / "research/explorations/harbor-021/captures/analyze",
    ]

    for cdir in candidate_dirs:
        if not cdir.is_dir():
            continue
        for path in sorted(cdir.rglob("analysis.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("validation_status") != "valid":
                    continue
                output = data.get("output")
                if not isinstance(output, dict):
                    continue
                sidecars.append(
                    {
                        "analysis_id": str(data.get("analysis_id", "")),
                        "job_id": str(data.get("job_id", "")),
                        "source_trial_id": str(data.get("source_trial_id", "")),
                        "validity": output.get("validity"),
                        "primary_category": output.get("primary_category"),
                        "summary": output.get("summary"),
                        "earliest_failure_step_id": output.get("earliest_failure_step_id"),
                        "confidence": output.get("confidence"),
                        "validation_status": "valid",
                        "source_path": _relative_path(path, root),
                        "source_digest": compute_file_digest(path),
                    }
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
    return sidecars


def load_trial_facts(root: Path) -> list[dict[str, Any]]:
    """Load deterministic trial facts from parquet, refusing annotation substitutes."""
    trial_parquet_files = list(root.glob("derived/parquet/**/trial_facts.parquet"))
    if trial_parquet_files:
        try:
            with duckdb.connect(":memory:") as con:
                glob_path = str(root / "derived/parquet/**/trial_facts.parquet")
                # Hive partitioning is deliberately off: the derived store mixes
                # `job_id=/trial_id=` and `compact/dt=` layouts whose partition
                # keys cannot be unified, and `job_id`/`trial_id` are physical
                # columns in every file anyway.
                rows = con.execute(
                    "SELECT * FROM read_parquet(?, union_by_name = true)",
                    [glob_path],
                ).fetchall()
                cols = [desc[0] for desc in con.description]
                return [dict(zip(cols, r, strict=False)) for r in rows]
        except Exception:
            return []
    return []


@dataclass(frozen=True)
class QualityLedgerRead:
    """Ledger rows bound to the exact bytes they were parsed from.

    ``digest`` is computed from the same single ``read_bytes()`` payload the
    rows were deserialized from, so a bytes swap between read and digest
    recording cannot make the recorded identity describe different content.
    """

    rows: tuple[dict[str, Any], ...]
    digest: str | None
    path: str | None


def load_quality_ledger_bound(
    root: Path,
    *,
    derived_root: Path | None = None,
) -> QualityLedgerRead:
    """Read the Evidence Quality Ledger once, binding digest to parsed bytes.

    The ledger is read from the tracked repository snapshot
    (``derived/parquet/trajectory_quality_reports.parquet``), not the live
    derived store: the snapshot is what the committed lessons.md was rendered
    from, so every checkout — clean or not — reproduces the artifact
    byte-for-byte. Updating the projection means refreshing the snapshot and
    regenerating in the same change.
    """
    resolved = (
        derived_root if derived_root is not None else root / "derived/parquet"
    )
    reports_path = resolved / f"{QUALITY_REPORT_TABLE}.parquet"
    if not reports_path.is_file():
        return QualityLedgerRead(rows=(), digest=None, path=None)
    payload = reports_path.read_bytes()
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    relative = _relative_path(reports_path, root)
    try:
        rows = tuple(pq.read_table(pa.BufferReader(payload)).to_pylist())
    except Exception:
        return QualityLedgerRead(rows=(), digest=digest, path=relative)
    return QualityLedgerRead(rows=rows, digest=digest, path=relative)


def load_quality_ledger(
    root: Path,
    *,
    derived_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Load Evidence Quality Ledger report rows from the shared derived store."""
    return list(load_quality_ledger_bound(root, derived_root=derived_root).rows)


def join_quality_statuses(
    trial_facts: Sequence[dict[str, Any]],
    quality_reports: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the Evidence Quality Ledger status to each trial fact row.

    Trials bind by exact ``(job_id, trial_id)`` pair only. A ``trial_id``-only
    fallback binds solely when the ledger holds exactly one job identity for
    that trial and that identity is empty — the ledger's own dedup key carries
    no job authority of its own. Conflicting statuses for one identity, or a
    non-empty ledger job identity that differs from the fact row, stay
    unbound (``quality_status=None``). Binding depends only on the set of
    ledger reports, never on row order. Unbound rows keep their current,
    ungated treatment unchanged.
    """
    pair_statuses: dict[tuple[str, str], set[str]] = {}
    trial_job_ids: dict[str, set[str]] = {}
    for report in quality_reports:
        status = str(report.get("status") or "")
        trial_id = str(report.get("trial_id") or "")
        if not status or not trial_id:
            continue
        pair = (str(report.get("job_id") or ""), trial_id)
        pair_statuses.setdefault(pair, set()).add(status)
        trial_job_ids.setdefault(trial_id, set()).add(pair[0])

    # Conflicting statuses for one identity are ambiguous authority: unbound,
    # deterministically, regardless of ledger row order.
    bound: dict[tuple[str, str], str | None] = {
        pair: (next(iter(statuses)) if len(statuses) == 1 else None)
        for pair, statuses in pair_statuses.items()
    }

    joined: list[dict[str, Any]] = []
    for row in trial_facts:
        trial_id = str(row.get("trial_id") or "")
        job_id = str(row.get("job_id") or "")
        status = bound.get((job_id, trial_id))
        if status is None and trial_job_ids.get(trial_id) == {""}:
            # The ledger knows this trial under no job identity at all; bind
            # the unique empty-identity status. Any cross-job or conflicting
            # ledger identity never binds.
            status = bound.get(("", trial_id))
        joined.append({**row, "quality_status": status})
    return joined


# --------------------------------------------------------------------------- #
# DuckDB Engine & Statistical Gating
# --------------------------------------------------------------------------- #


def populate_duckdb(
    con: duckdb.DuckDBPyConnection,
    *,
    craft_records: Sequence[dict[str, Any]],
    trial_facts: Sequence[dict[str, Any]],
    analysis_sidecars: Sequence[dict[str, Any]],
    observation_records: Sequence[dict[str, Any]],
    sql_path: Path | None = None,
) -> None:
    """Populate DuckDB with in-memory tables and execute view definitions."""
    # Math input: ledger-quarantined trials are excluded from the frozen
    # sql/lessons.sql eligibility views BEFORE the math runs, not filtered at
    # presentation time. The full joined set stays reachable as
    # `trial_facts_all`, which backs the companion quality-count views, so
    # quarantined trials remain visible in quality_*_n without entering any
    # eligibility denominator. With no ledger (all statuses None) the two
    # tables are identical and the math is unchanged.
    math_facts = [
        row for row in trial_facts if row.get("quality_status") != "quarantine"
    ]
    t_craft = pa.Table.from_pylist(list(craft_records), schema=CRAFT_SCHEMA)
    t_trials = pa.Table.from_pylist(list(math_facts), schema=TRIAL_FACTS_LEDGER_SCHEMA)
    t_trials_all = pa.Table.from_pylist(
        list(trial_facts), schema=TRIAL_FACTS_LEDGER_SCHEMA
    )
    t_analysis = pa.Table.from_pylist(list(analysis_sidecars), schema=ANALYSIS_SIDECAR_SCHEMA)
    t_obs = pa.Table.from_pylist(list(observation_records), schema=OBSERVATION_RECORD_SCHEMA)

    con.register("craft", t_craft)
    con.register("trial_facts", t_trials)
    con.register("trial_facts_all", t_trials_all)
    con.register("analysis_sidecars", t_analysis)
    con.register("observation_records", t_obs)

    resolved_sql = sql_path if sql_path is not None else SQL_LESSONS_PATH
    if resolved_sql.is_file():
        con.execute(resolved_sql.read_text(encoding="utf-8"))
    con.execute(_QUALITY_GRAIN_SQL)


# Trial-level companions to the frozen `sql/lessons.sql` views. They reproduce
# each view's grouping keys verbatim (facet view: sidecar dedup and mechanical
# CASEs included) only to count ledger quality states per row; the eligibility
# math (n, failures_n, rates, intervals) stays exclusively in sql/lessons.sql.
# Update the grains together with sql/lessons.sql.
_QUALITY_GRAIN_SQL = """
CREATE OR REPLACE VIEW trial_quality_by_verifier_type AS
SELECT
    c.source_repo,
    coalesce(c.verifier_type, 'unclassified') AS verifier_type,
    t.trial_id,
    t.quality_status
FROM craft c
JOIN trial_facts_all t
    ON c.task_digest IS NOT NULL
   AND t.task_digest IS NOT NULL
   AND c.task_digest = t.task_digest;

CREATE OR REPLACE VIEW trial_quality_by_env AS
SELECT
    c.source_repo,
    coalesce(c.env_services_n, 1) AS env_services_n,
    coalesce(c.env_multi_container, false) AS env_multi_container,
    CASE
        WHEN c.env_n_files IS NULL THEN 'unknown'
        WHEN c.env_n_files = 0 THEN '0_files'
        WHEN c.env_n_files <= 5 THEN '1_to_5_files'
        WHEN c.env_n_files <= 20 THEN '6_to_20_files'
        ELSE 'over_20_files'
    END AS env_files_bucket,
    t.trial_id,
    t.quality_status
FROM craft c
JOIN trial_facts_all t
    ON c.task_digest IS NOT NULL
   AND t.task_digest IS NOT NULL
   AND c.task_digest = t.task_digest;

CREATE OR REPLACE VIEW trial_quality_by_facet AS
WITH validated_sidecars AS (
    SELECT * EXCLUDE (sidecar_rank)
    FROM (
        SELECT
            a.*,
            row_number() OVER (
                PARTITION BY a.source_trial_id
                ORDER BY
                    coalesce(a.source_path, ''),
                    coalesce(a.analysis_id, ''),
                    coalesce(a.job_id, ''),
                    coalesce(a.source_digest, ''),
                    coalesce(a.primary_category, ''),
                    coalesce(a.validity, ''),
                    coalesce(a.summary, ''),
                    coalesce(cast(a.earliest_failure_step_id AS VARCHAR), ''),
                    coalesce(a.confidence, '')
            ) AS sidecar_rank
        FROM analysis_sidecars a
        WHERE a.validation_status = 'valid'
          AND nullif(a.source_trial_id, '') IS NOT NULL
    )
    WHERE sidecar_rank = 1
),
trial_joined AS (
    SELECT
        c.source_repo,
        coalesce(c.verifier_type, 'unclassified') AS verifier_type,
        coalesce(c.instruction_style, 'unclassified') AS instruction_style,
        coalesce(c.difficulty_mechanism, 'unclassified') AS difficulty_mechanism,
        CASE
            WHEN c.env_multi_container IS TRUE THEN 'multi_container'
            WHEN c.env_multi_container IS FALSE THEN 'single_container'
            ELSE 'unspecified'
        END AS env_container_mode,
        coalesce(c.base_image_pin, 'unpinned') AS base_image_pin,
        CASE
            WHEN c.pinned_deps IS TRUE THEN 'pinned'
            WHEN c.pinned_deps IS FALSE THEN 'unpinned'
            ELSE 'unstated'
        END AS dependency_pinning,
        t.trial_id,
        a.primary_category AS model_failure_category,
        a.validity AS model_validity,
        CASE WHEN a.source_trial_id IS NOT NULL THEN 'validated_analysis_sidecar' END
            AS model_diagnosis_source,
        CASE
            WHEN t.exception_class IS NOT NULL THEN 'exception'
            WHEN t.primary_reward IS NULL THEN 'unmeasured'
            WHEN t.primary_reward < 1.0 THEN 'unscored_failure'
            ELSE 'none'
        END AS mechanical_failure_category,
        CASE
            WHEN t.exception_class IS NOT NULL THEN 'exception_trial'
            WHEN t.primary_reward IS NULL THEN 'not_measured'
            WHEN t.primary_reward >= 1.0 THEN 'passed'
            ELSE 'measured_agent_attempt'
        END AS mechanical_validity,
        'trial_facts' AS mechanical_diagnosis_source,
        t.quality_status
    FROM craft c
    JOIN trial_facts_all t
        ON c.task_digest IS NOT NULL
       AND t.task_digest IS NOT NULL
       AND c.task_digest = t.task_digest
    LEFT JOIN validated_sidecars a
        ON a.source_trial_id = t.trial_id
)
SELECT source_repo, 'verifier_type' AS facet_name, verifier_type AS facet_value, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
UNION ALL
SELECT source_repo, 'instruction_style', instruction_style, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
UNION ALL
SELECT source_repo, 'difficulty_mechanism', difficulty_mechanism, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
UNION ALL
SELECT source_repo, 'env_container_mode', env_container_mode, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
UNION ALL
SELECT source_repo, 'base_image_pin', base_image_pin, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
UNION ALL
SELECT source_repo, 'dependency_pinning', dependency_pinning, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined;

CREATE OR REPLACE VIEW v_outcome_by_verifier_type_quality AS
SELECT
    source_repo,
    verifier_type,
    count(trial_id) FILTER (WHERE quality_status = 'pass') AS quality_pass_n,
    count(trial_id) FILTER (WHERE quality_status = 'warn') AS quality_warn_n,
    count(trial_id) FILTER (WHERE quality_status = 'fail') AS quality_fail_n,
    count(trial_id) FILTER (WHERE quality_status = 'quarantine') AS quality_quarantine_n
FROM trial_quality_by_verifier_type
GROUP BY source_repo, verifier_type;

CREATE OR REPLACE VIEW v_loop_rate_by_env_quality AS
SELECT
    source_repo,
    env_services_n,
    env_multi_container,
    env_files_bucket,
    count(trial_id) FILTER (WHERE quality_status = 'pass') AS quality_pass_n,
    count(trial_id) FILTER (WHERE quality_status = 'warn') AS quality_warn_n,
    count(trial_id) FILTER (WHERE quality_status = 'fail') AS quality_fail_n,
    count(trial_id) FILTER (WHERE quality_status = 'quarantine') AS quality_quarantine_n
FROM trial_quality_by_env
GROUP BY source_repo, env_services_n, env_multi_container, env_files_bucket;

CREATE OR REPLACE VIEW v_failure_by_facet_quality AS
SELECT
    source_repo,
    facet_name,
    facet_value,
    model_failure_category,
    model_validity,
    model_diagnosis_source,
    mechanical_failure_category,
    mechanical_validity,
    mechanical_diagnosis_source,
    count(trial_id) FILTER (WHERE quality_status = 'pass') AS quality_pass_n,
    count(trial_id) FILTER (WHERE quality_status = 'warn') AS quality_warn_n,
    count(trial_id) FILTER (WHERE quality_status = 'fail') AS quality_fail_n,
    count(trial_id) FILTER (WHERE quality_status = 'quarantine') AS quality_quarantine_n
FROM trial_quality_by_facet
GROUP BY ALL;
"""

_VIEW_QUALITY_JOIN_KEYS: dict[str, tuple[str, ...]] = {
    "v_outcome_by_verifier_type": ("source_repo", "verifier_type"),
    "v_loop_rate_by_env": (
        "source_repo",
        "env_services_n",
        "env_multi_container",
        "env_files_bucket",
    ),
    "v_failure_by_facet": (
        "source_repo",
        "facet_name",
        "facet_value",
        "model_failure_category",
        "model_validity",
        "model_diagnosis_source",
        "mechanical_failure_category",
        "mechanical_validity",
        "mechanical_diagnosis_source",
    ),
}

# The frozen views end in ORDER BY, but the enrichment join would otherwise
# leave ties between equal sort keys to the query plan. Restating each frozen
# ordering and extending it with the remaining group keys keeps row order —
# and therefore lesson ids — total and deterministic.
_VIEW_ORDER_BY: dict[str, str] = {
    "v_outcome_by_verifier_type": "v.source_repo, v.n DESC, v.verifier_type",
    "v_loop_rate_by_env": (
        "v.source_repo, v.n DESC, v.loop_rate_pct DESC, "
        "v.env_services_n, v.env_multi_container, v.env_files_bucket"
    ),
    "v_failure_by_facet": (
        "v.source_repo, v.facet_name, v.n DESC, v.failures_n DESC, v.facet_value, "
        "v.model_failure_category, v.model_validity, v.model_diagnosis_source, "
        "v.mechanical_failure_category, v.mechanical_validity, v.mechanical_diagnosis_source"
    ),
}


def _enriched_view_sql(view_name: str) -> str:
    """LEFT-join precomputed ledger counts onto a frozen view's rows."""
    predicate = "\n       AND ".join(
        f"v.{key} IS NOT DISTINCT FROM q.{key}"
        for key in _VIEW_QUALITY_JOIN_KEYS[view_name]
    )
    columns = ", ".join(
        f"coalesce(q.{column}, 0) AS {column}" for column in QUALITY_STATUS_COLUMNS
    )
    return (
        f"SELECT v.*, {columns}\n"
        f"FROM {view_name} v\n"
        f"LEFT JOIN {view_name}_quality q\n"
        f"  ON {predicate}\n"
        f"ORDER BY {_VIEW_ORDER_BY[view_name]}"
    )


def execute_lessons_views(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, list[dict[str, Any]]]:
    """Query each lesson view and return dictionary of row dictionaries."""
    views = ["v_failure_by_facet", "v_loop_rate_by_env", "v_outcome_by_verifier_type"]
    results: dict[str, list[dict[str, Any]]] = {}

    for view_name in views:
        cursor = con.execute(_enriched_view_sql(view_name))
        cols = [desc[0] for desc in con.description]
        rows = cursor.fetchall()
        results[view_name] = [dict(zip(cols, r, strict=False)) for r in rows]

    return results


def apply_statistical_gating(
    view_rows: dict[str, list[dict[str, Any]]],
    *,
    power_threshold: int = DEFAULT_POWER_THRESHOLD,
) -> dict[str, list[LessonRow]]:
    """Transform raw view rows into statistically-gated LessonRow entries."""
    lessons: dict[str, list[LessonRow]] = {
        "v_failure_by_facet": [],
        "v_loop_rate_by_env": [],
        "v_outcome_by_verifier_type": [],
    }

    # 1. v_failure_by_facet
    for idx, row in enumerate(view_rows.get("v_failure_by_facet", []), start=1):
        n = int(row.get("n", 0))
        failures = int(row.get("failures_n", 0))
        rate = failures / n if n > 0 else 0.0
        interval = wilson_interval(failures, n) if n > 0 else None
        powered = n >= power_threshold
        status = "sufficient" if powered else "insufficient n"

        facet_name = str(row.get("facet_name", "unknown"))
        facet_value = str(row.get("facet_value", "unknown"))
        model_category = row.get("model_failure_category")
        mechanical_category = str(row.get("mechanical_failure_category", "unknown"))
        model_label = "none" if model_category is None else str(model_category)
        dimension = (
            f"{facet_name}={facet_value} "
            f"(model={model_label}; mechanical={mechanical_category})"
        )

        if powered and interval is not None:
            low, high = interval
            finding = f"failure_rate={rate:.1%} [95% CI: {low:.1%}-{high:.1%}, n={n}]"
        else:
            finding = "insufficient n"

        lessons["v_failure_by_facet"].append(
            LessonRow(
                lesson_id=f"failure_facet_{idx:03d}",
                view_name="v_failure_by_facet",
                dimension=dimension,
                metric_name="failure_rate",
                n=n,
                k=failures,
                rate=rate,
                wilson_95=interval,
                powered=powered,
                status=status,
                finding=finding,
                details=row,
            )
        )

    if not lessons["v_failure_by_facet"]:
        lessons["v_failure_by_facet"].append(
            LessonRow(
                lesson_id="failure_facet_001",
                view_name="v_failure_by_facet",
                dimension="none",
                metric_name="failure_rate",
                n=0,
                k=0,
                rate=0.0,
                wilson_95=None,
                powered=False,
                status="insufficient n",
                finding="insufficient n",
                details={},
            )
        )

    # 2. v_loop_rate_by_env
    for idx, row in enumerate(view_rows.get("v_loop_rate_by_env", []), start=1):
        n = int(row.get("n", 0))
        loops = int(row.get("loops_n", 0))
        rate = loops / n if n > 0 else 0.0
        interval = wilson_interval(loops, n) if n > 0 else None
        powered = n >= power_threshold
        status = "sufficient" if powered else "insufficient n"

        services = row.get("env_services_n", 1)
        multi = "multi_container" if row.get("env_multi_container") else "single_container"
        files_bucket = row.get("env_files_bucket", "unknown")
        dimension = f"services={services}, {multi}, files={files_bucket}"

        if powered and interval is not None:
            low, high = interval
            finding = f"loop_rate={rate:.1%} [95% CI: {low:.1%}-{high:.1%}, n={n}]"
        else:
            finding = "insufficient n"

        lessons["v_loop_rate_by_env"].append(
            LessonRow(
                lesson_id=f"loop_env_{idx:03d}",
                view_name="v_loop_rate_by_env",
                dimension=dimension,
                metric_name="loop_rate",
                n=n,
                k=loops,
                rate=rate,
                wilson_95=interval,
                powered=powered,
                status=status,
                finding=finding,
                details=row,
            )
        )

    if not lessons["v_loop_rate_by_env"]:
        lessons["v_loop_rate_by_env"].append(
            LessonRow(
                lesson_id="loop_env_001",
                view_name="v_loop_rate_by_env",
                dimension="none",
                metric_name="loop_rate",
                n=0,
                k=0,
                rate=0.0,
                wilson_95=None,
                powered=False,
                status="insufficient n",
                finding="insufficient n",
                details={},
            )
        )

    # 3. v_outcome_by_verifier_type
    for idx, row in enumerate(view_rows.get("v_outcome_by_verifier_type", []), start=1):
        n = int(row.get("n", 0))
        passed = int(row.get("passed_n", 0))
        exceptions = int(row.get("exceptions_n", 0))
        pass_rate = passed / n if n > 0 else 0.0
        pass_interval = wilson_interval(passed, n) if n > 0 else None
        powered = n >= power_threshold
        status = "sufficient" if powered else "insufficient n"

        verifier_type = str(row.get("verifier_type", "unclassified"))
        dimension = f"verifier_type={verifier_type}"

        if powered and pass_interval is not None:
            low, high = pass_interval
            never_measured = int(row.get("never_measured_n", 0))
            finding = (
                f"pass_rate={pass_rate:.1%} [95% CI: {low:.1%}-{high:.1%}, n={n}], "
                f"excluded_exceptions={exceptions}, excluded_never_measured={never_measured}"
            )
        else:
            finding = "insufficient n"

        lessons["v_outcome_by_verifier_type"].append(
            LessonRow(
                lesson_id=f"verifier_outcome_{idx:03d}",
                view_name="v_outcome_by_verifier_type",
                dimension=dimension,
                metric_name="pass_rate",
                n=n,
                k=passed,
                rate=pass_rate,
                wilson_95=pass_interval,
                powered=powered,
                status=status,
                finding=finding,
                details=row,
            )
        )

    if not lessons["v_outcome_by_verifier_type"]:
        lessons["v_outcome_by_verifier_type"].append(
            LessonRow(
                lesson_id="verifier_outcome_001",
                view_name="v_outcome_by_verifier_type",
                dimension="none",
                metric_name="pass_rate",
                n=0,
                k=0,
                rate=0.0,
                wilson_95=None,
                powered=False,
                status="insufficient n",
                finding="insufficient n",
                details={},
            )
        )

    return lessons


def compare_lesson_rows(
    row_a: LessonRow,
    row_b: LessonRow,
) -> LessonRanking:
    """Compare two lesson rows with refusal-to-rank propagated from cohort."""
    reasons: list[str] = []

    if not row_a.powered or not row_b.powered:
        reasons.append(
            f"insufficient n: {row_a.dimension} (n={row_a.n}), {row_b.dimension} (n={row_b.n})"
        )

    if row_a.k == 0 and row_b.k == 0 and row_a.n > 0 and row_b.n > 0:
        reasons.append("uninformative metric: zero observed events across both cohorts")

    if row_a.wilson_95 is None or row_b.wilson_95 is None:
        reasons.append("confidence interval unavailable")
    elif row_a.powered and row_b.powered:
        low_a, high_a = row_a.wilson_95
        low_b, high_b = row_b.wilson_95
        if max(low_a, low_b) <= min(high_a, high_b):
            reasons.append(
                f"intervals overlap: [{low_a:.1%}, {high_a:.1%}] vs [{low_b:.1%}, {high_b:.1%}]"
            )

    reasons = list(dict.fromkeys(reasons))
    if reasons:
        statement = f"{NOT_COMPARABLE}: {'; '.join(reasons)}"
        ranking = None
        rankable = False
    else:
        rankable = True
        if row_a.rate > row_b.rate:
            ranking = f"{row_a.dimension} > {row_b.dimension}"
        elif row_b.rate > row_a.rate:
            ranking = f"{row_b.dimension} > {row_a.dimension}"
        else:
            statement = f"{NOT_COMPARABLE}: identical observed rate {row_a.rate:.1%}"
            return LessonRanking(
                view_name=row_a.view_name,
                dimension_a=row_a.dimension,
                dimension_b=row_b.dimension,
                rankable=False,
                ranking=None,
                statement=statement,
                refusal_reasons=(f"identical observed rate {row_a.rate:.1%}",),
            )
        statement = f"Ranking: {ranking} (rates: {row_a.rate:.1%} vs {row_b.rate:.1%})"

    return LessonRanking(
        view_name=row_a.view_name,
        dimension_a=row_a.dimension,
        dimension_b=row_b.dimension,
        rankable=rankable,
        ranking=ranking,
        statement=statement,
        refusal_reasons=tuple(reasons),
    )


def rank_lesson_rows(
    rows: Sequence[LessonRow],
) -> list[LessonRanking]:
    """Pairwise rank a collection of lesson rows within a view."""
    rankings: list[LessonRanking] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            rankings.append(compare_lesson_rows(rows[i], rows[j]))
    return rankings


def build_lessons(
    root: Path,
    *,
    power_threshold: int = DEFAULT_POWER_THRESHOLD,
    sql_path: Path | None = None,
    generated_at: datetime | None = None,
) -> LessonsResult:
    """Build and evaluate statistical lesson aggregation views across repo data."""
    craft_records = load_craft_records(root)
    observations = load_observation_records(root)
    sidecars = load_analysis_sidecars(root)
    ledger = load_quality_ledger_bound(root)
    quality_reports = list(ledger.rows)
    facts = join_quality_statuses(load_trial_facts(root), quality_reports)
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft_records,
            trial_facts=facts,
            analysis_sidecars=sidecars,
            observation_records=observations,
            sql_path=sql_path,
        )
        raw_views = execute_lessons_views(con)

    lessons_by_view = apply_statistical_gating(raw_views, power_threshold=power_threshold)

    all_lessons = [item for sublist in lessons_by_view.values() for item in sublist]
    powered = sum(1 for item in all_lessons if item.powered)
    underpowered = sum(1 for item in all_lessons if not item.powered)

    quality_status_counts = Counter(
        str(report.get("status") or "") for report in quality_reports
    )
    records_summary = {
        "craft_records": len(craft_records),
        "trial_facts": len(facts),
        "analysis_sidecars": len(sidecars),
        "observation_records": len(observations),
        "quality_ledger_evaluated": len(quality_reports),
        "quality_ledger_pass": quality_status_counts.get("pass", 0),
        "quality_ledger_warn": quality_status_counts.get("warn", 0),
        "quality_ledger_fail": quality_status_counts.get("fail", 0),
        "quality_ledger_quarantine": quality_status_counts.get("quarantine", 0),
    }

    rankings_by_view = {
        view_name: rank_lesson_rows(rows)
        for view_name, rows in lessons_by_view.items()
    }

    inputs = collect_lessons_inputs(root, sql_path=sql_path, quality_ledger=ledger)
    return LessonsResult(
        generated_at=generated_at if generated_at is not None else datetime.now(UTC),
        power_threshold=power_threshold,
        total_lessons=len(all_lessons),
        powered_lessons=powered,
        underpowered_lessons=underpowered,
        lessons_by_view=lessons_by_view,
        records_summary=records_summary,
        inputs=tuple(inputs),
        rankings_by_view=rankings_by_view,
    )


# --------------------------------------------------------------------------- #
# Markdown Report Generation
# --------------------------------------------------------------------------- #


def _format_ci(interval: tuple[float, float] | None) -> str:
    if interval is None:
        return "n/a"
    low, high = interval
    return f"[{low:.1%}, {high:.1%}]"


def _quality_counts(details: dict[str, Any]) -> tuple[int, int, int, int]:
    """Ledger pass/warn/fail/quarantine counts recorded on a view row."""
    return (
        int(details.get("quality_pass_n", 0) or 0),
        int(details.get("quality_warn_n", 0) or 0),
        int(details.get("quality_fail_n", 0) or 0),
        int(details.get("quality_quarantine_n", 0) or 0),
    )


def _quality_cells(counts: tuple[int, int, int, int]) -> str:
    """Render the four ledger count cells of a lesson table row."""
    return f"{counts[0]} | {counts[1]} | {counts[2]} | {counts[3]}"


def render_lessons_markdown(result: LessonsResult) -> str:
    """Render structured markdown report matching research/lessons.md specification."""
    rec_sum = result.records_summary
    lines: list[str] = [
        "---",
        "status: living",
        "audience:",
        "  - builder",
        "  - analyst",
    ]
    if result.inputs:
        lines.append("inputs:")
        for inp in result.inputs:
            lines.append(f"  - path: {inp['path']}")
            lines.append(f"    digest: {inp['digest']}")
    else:
        lines.append("inputs: []")
    lines.extend(
        [
            "---",
            "",
            f"<!-- {GENERATED_HEADER} -->",
            "# Statistical Lessons & Aggregation Views",
            "",
            f"- **Generated at:** {result.generated_at.strftime('%Y-%m-%d %H:%M:%SZ')}",
            (
                f"- **Statistical Gating:** Power threshold $n \\ge {result.power_threshold}$, "
                "Wilson 95% confidence interval"
            ),
            (
                f"- **Corpus Summary:** {rec_sum.get('craft_records', 0)} craft tasks, "
                f"{rec_sum.get('trial_facts', 0)} trials, "
                f"{rec_sum.get('observation_records', 0)} observation records, "
                f"{rec_sum.get('analysis_sidecars', 0)} analysis sidecars"
            ),
            *(
                [
                    (
                        "- **Evidence Quality Ledger:** "
                        f"{rec_sum.get('quality_ledger_evaluated', 0)} evaluated trials "
                        f"(pass {rec_sum.get('quality_ledger_pass', 0)}, "
                        f"warn {rec_sum.get('quality_ledger_warn', 0)}, "
                        f"fail {rec_sum.get('quality_ledger_fail', 0)}, "
                        f"quarantine {rec_sum.get('quality_ledger_quarantine', 0)})"
                    )
                ]
                if "quality_ledger_evaluated" in rec_sum
                else []
            ),
            (
                f"- **Findings Gate:** {result.powered_lessons} statistically powered finding(s), "
                f"{result.underpowered_lessons} observation row(s) gated with `insufficient n`"
            ),
            "",
            "---",
            "",
            "## 1. Outcome by Verifier Type (`v_outcome_by_verifier_type`)",
            "",
            (
                "Cross-tabulation of task verifier architecture against measured trial pass "
                "rates. Exception and never-measured trials are reported but excluded from "
                "the capability denominator."
            ),
            "",
            (
                "| Source Repo | Verifier Type | Total Trials | Eligible n | Passed | Pass Rate | "
                "Wilson 95% CI | Excluded Exceptions | Excluded Never Measured | "
                "Ledger Pass | Ledger Warn | Ledger Fail | Ledger Quarantine | Status | Finding |"
            ),
            "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )

    verifier_lessons = result.lessons_by_view.get("v_outcome_by_verifier_type", [])
    if verifier_lessons:
        for row in verifier_lessons:
            if row.n == 0:
                det = row.details
                lines.append(
                    f"| {det.get('source_repo', '-')} | "
                    f"{det.get('verifier_type', 'none')} | "
                    f"{int(det.get('total_trials_n', 0))} | 0 | 0 | 0.0% | n/a | "
                    f"{int(det.get('exceptions_n', 0))} | "
                    f"{int(det.get('never_measured_n', 0))} | "
                    f"{_quality_cells(_quality_counts(det))} | `insufficient n` | "
                    "insufficient n |"
                )
                continue
            det = row.details
            repo = str(det.get("source_repo", "corpus"))
            vtype = str(det.get("verifier_type", "unclassified"))
            total = int(det.get("total_trials_n", row.n))
            passed = int(det.get("passed_n", 0))
            exceptions = int(det.get("exceptions_n", 0))
            never_measured = int(det.get("never_measured_n", 0))
            ci_str = _format_ci(row.wilson_95)
            lines.append(
                f"| {repo} | {vtype} | {total} | {row.n} | {passed} | {row.rate:.1%} | "
                f"{ci_str} | {exceptions} | {never_measured} | "
                f"{_quality_cells(_quality_counts(det))} | `{row.status}` | "
                f"{row.finding} |"
            )
    else:
        lines.append(
            "| - | none | 0 | 0 | 0 | 0.0% | n/a | 0 | 0 | 0 | 0 | 0 | 0 | "
            "`insufficient n` | insufficient n |"
        )

    lines.extend(
        [
            "",
            "## 2. Loop Rate by Environment Complexity (`v_loop_rate_by_env`)",
            "",
            (
                "Observation-annotation loop rates by environment complexity. Markdown "
                "annotations remain identified and are not substituted for deterministic facts."
            ),
            (
                "| Source Repo | Annotation Source | Services | Container Mode | Env Files | "
                "Total Trials | Annotated | Unannotated | Eligible n | Loops | Loop Rate | "
                "Wilson 95% CI | Avg Annotated Steps | Avg Annotated Tool Errors | "
                "Ledger Pass | Ledger Warn | Ledger Fail | Ledger Quarantine | Status | Finding |"
            ),
            (
                "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|"
                "---:|---:|---:|---:|---|---|"
            ),
        ]
    )

    loop_lessons = result.lessons_by_view.get("v_loop_rate_by_env", [])
    if loop_lessons:
        for row in loop_lessons:
            det = row.details
            repo = str(det.get("source_repo", "-"))
            annotation_source = str(
                det.get("observation_source", "observation_markdown")
            )
            services = det.get("env_services_n", 1)
            multi = "multi" if det.get("env_multi_container") else "single"
            files_b = str(det.get("env_files_bucket", "unknown"))
            total = int(det.get("total_trials_n", row.n))
            annotated = int(det.get("annotated_n", 0))
            unannotated = int(det.get("unannotated_n", total - annotated))
            loops = row.k
            ci_str = _format_ci(row.wilson_95)
            avg_s = det.get("avg_observation_steps")
            avg_e = det.get("avg_observation_tool_errors")
            avg_s_str = f"{avg_s:.1f}" if avg_s is not None else "n/a"
            avg_e_str = f"{avg_e:.1f}" if avg_e is not None else "n/a"
            lines.append(
                f"| {repo} | {annotation_source} | {services} | {multi} | {files_b} | "
                f"{total} | {annotated} | {unannotated} | {row.n} | {loops} | "
                f"{row.rate:.1%} | {ci_str} | {avg_s_str} | {avg_e_str} | "
                f"{_quality_cells(_quality_counts(det))} | `{row.status}` | {row.finding} |"
            )
    else:
        lines.append(
            "| - | observation_markdown | 0 | single | 0_files | 0 | 0 | 0 | 0 | 0 | "
            "0.0% | n/a | n/a | n/a | 0 | 0 | 0 | 0 | `insufficient n` | insufficient n |"
        )

    lines.extend(
        [
            "",
            "## 3. Failure by Craft Facet (`v_failure_by_facet`)",
            "",
            (
                "Source-discriminated model diagnoses and mechanical trial-fact classifications "
                "across structural task facets. The two vocabularies are never merged."
            ),
            "",
            (
                "| Source Repo | Facet Name | Facet Value | Model Category | Model Validity | "
                "Model Source | Mechanical Category | Mechanical Validity | Mechanical Source | "
                "Total Trials | Eligible n | Exceptions | Never Measured | Excluded | Failures | "
                "Failure Rate | Wilson 95% CI | "
                "Ledger Pass | Ledger Warn | Ledger Fail | Ledger Quarantine | Status | Finding |"
            ),
            (
                "|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|"
                "---:|---|---:|---:|---:|---:|---|---|"
            ),
        ]
    )

    failure_lessons = result.lessons_by_view.get("v_failure_by_facet", [])
    if failure_lessons:
        for row in failure_lessons:
            det = row.details
            repo = str(det.get("source_repo", "-"))
            fname = str(det.get("facet_name", "none"))
            fval = str(det.get("facet_value", "none"))
            model_cat = det.get("model_failure_category") or "none"
            model_val = det.get("model_validity") or "none"
            model_source = det.get("model_diagnosis_source") or "none"
            mechanical_cat = str(det.get("mechanical_failure_category", "none"))
            mechanical_val = str(det.get("mechanical_validity", "none"))
            mechanical_source = str(
                det.get("mechanical_diagnosis_source", "trial_facts")
            )
            total = int(det.get("total_trials_n", row.n))
            exceptions = int(det.get("exceptions_n", 0))
            never_measured = int(det.get("never_measured_n", 0))
            excluded = int(det.get("excluded_n", exceptions + never_measured))
            ci_str = _format_ci(row.wilson_95)
            lines.append(
                f"| {repo} | {fname} | {fval} | {model_cat} | {model_val} | {model_source} | "
                f"{mechanical_cat} | {mechanical_val} | {mechanical_source} | {total} | "
                f"{row.n} | {exceptions} | {never_measured} | {excluded} | {row.k} | "
                f"{row.rate:.1%} | {ci_str} | "
                f"{_quality_cells(_quality_counts(det))} | `{row.status}` | {row.finding} |"
            )
    else:
        lines.append(
            "| - | none | none | none | none | none | none | none | trial_facts | 0 | 0 | "
            "0 | 0 | 0 | 0 | 0.0% | n/a | 0 | 0 | 0 | 0 | `insufficient n` | "
            "insufficient n |"
        )

    lines.extend(
        [
            "",
            "## Statistical Gating Rules",
            "",
            (
                "1. **Sample Size Floor ($n \\ge 5$):** Rows with sample count $n < 5$ carry "
                "status `insufficient n` and render findings as `insufficient n`. They are "
                "preserved for evidence tracking but never reported as generalized findings."
            ),
            (
                "2. **Confidence Intervals:** Every proportion is bounded by a two-sided 95% "
                "Wilson score interval with continuity correction from `evallab.cohort`."
            ),
            (
                "3. **Refuse-to-Rank Propagation:** Comparative rankings propagate refusal "
                "(`not distinguishable / not comparable`) from `evallab.cohort` whenever "
                "sample sizes are underpowered, confidence intervals overlap, or metrics "
                "reflect uninformative instrumentation gaps."
            ),
            (
                "4. **Deterministic Regeneration:** This file is generated by `evallab.lessons`; "
                "hand-edits are prohibited."
            ),
            "",
        ]
    )

    return "\n".join(lines)


def generate_lessons_file(
    root: Path,
    output_path: Path | None = None,
    *,
    power_threshold: int = DEFAULT_POWER_THRESHOLD,
    sql_path: Path | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """Generate research/lessons.md from repository evidence."""
    target = output_path if output_path is not None else root / "research/lessons.md"
    result = build_lessons(
        root,
        power_threshold=power_threshold,
        sql_path=sql_path,
        generated_at=generated_at,
    )
    markdown = render_lessons_markdown(result)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target


def check_lessons_freshness(root: Path, target: Path | None = None) -> bool:
    """Return whether committed lessons exactly match current source inputs."""
    lessons_path = target if target is not None else root / "research/lessons.md"
    if not lessons_path.is_file():
        return False
    committed = lessons_path.read_text(encoding="utf-8")
    timestamp_match = re.search(
        r"^- \*\*Generated at:\*\* ([0-9]{4}-[0-9]{2}-[0-9]{2} "
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z)$",
        committed,
        flags=re.MULTILINE,
    )
    if timestamp_match is None:
        return False
    generated_at = datetime.strptime(
        timestamp_match.group(1), "%Y-%m-%d %H:%M:%SZ"
    ).replace(tzinfo=UTC)
    expected = render_lessons_markdown(
        build_lessons(root, generated_at=generated_at)
    )
    return committed == expected


def main() -> int:
    """Check the committed lessons projection from a clean checkout."""
    root = Path.cwd()
    if not check_lessons_freshness(root):
        print("research/lessons.md is stale; regenerate with evallab.lessons")
        return 1
    lineage = resolve_lineage("research/lessons.md", repo_root=root)
    if lineage.status != "resolved":
        print(f"research/lessons.md lineage is {lineage.status}")
        return 1
    print("research/lessons.md is fresh with resolved lineage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
