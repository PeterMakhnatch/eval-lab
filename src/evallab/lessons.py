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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from evallab.cohort import NOT_COMPARABLE, wilson_interval
from evallab.craft import CRAFT_SCHEMA, CraftRecord, TaskSource, scan
from evallab.facts import TRIAL_FACT_SCHEMA
from evallab.lineage import compute_file_digest

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
    *,
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

    # 4. Analysis sidecars
    candidate_dirs = [
        root / "derived/analysis",
        root / "research/analysis",
        root / "tests/fixtures/explorer/analyses",
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
    """Discover all valid analysis.json sidecars across the repo."""
    sidecars: list[dict[str, Any]] = []
    candidate_dirs = [
        root / "derived/analysis",
        root / "research/analysis",
        root / "tests/fixtures/explorer/analyses",
        root / "research/explorations/harbor-021/captures/analyze",
    ]

    for cdir in candidate_dirs:
        if not cdir.is_dir():
            continue
        for path in sorted(cdir.rglob("analysis.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                output = data.get("output", {})
                sidecars.append(
                    {
                        "analysis_id": str(data.get("analysis_id", "")),
                        "job_id": str(data.get("job_id", "")),
                        "source_trial_id": str(data.get("source_trial_id", "")),
                        "validity": output.get("validity", "unknown"),
                        "primary_category": output.get("primary_category", "unclassified"),
                        "summary": output.get("summary", ""),
                        "earliest_failure_step_id": output.get("earliest_failure_step_id"),
                        "confidence": output.get("confidence", "unknown"),
                        "validation_status": data.get("validation_status", "valid"),
                    }
                )
            except Exception:
                continue
    return sidecars


def load_trial_facts(
    root: Path,
    observations: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load trial facts from parquet if present, or synthesize from observation records."""
    trial_parquet_files = list(root.glob("derived/parquet/**/trial_facts.parquet"))
    if trial_parquet_files:
        try:
            with duckdb.connect(":memory:") as con:
                glob_path = str(root / "derived/parquet/**/trial_facts.parquet")
                rows = con.execute(
                    "SELECT * FROM read_parquet(?, hive_partitioning = true, "
                    "union_by_name = true)",
                    [glob_path],
                ).fetchall()
                cols = [desc[0] for desc in con.description]
                return [dict(zip(cols, r, strict=False)) for r in rows]
        except Exception:
            pass

    # Synthesize trial facts from observation records
    facts: list[dict[str, Any]] = []
    obs_list = observations if observations is not None else load_observation_records(root)
    for obs in obs_list:
        reward = obs.get("reward")
        task_name = obs.get("task", "")
        trial_id = obs.get("trial_id", "")
        facts.append(
            {
                "experiment_id": "synthesized-from-observations",
                "job_id": obs.get("job", ""),
                "trial_id": trial_id,
                "job_name": obs.get("job", ""),
                "trial_name": obs.get("trial_name", ""),
                "task_name": task_name,
                "task_digest": None,
                "verifier_digest": "synthesized",
                "environment_digest": "synthesized",
                "agent_config_digest": "synthesized",
                "agent_name": obs.get("agent", ""),
                "agent_version": None,
                "model_name": obs.get("model", ""),
                "primary_reward": reward,
                "exception_class": None if reward is not None else "HarnessException",
                "exception_phase": None,
                "duration_seconds": None,
                "environment_setup_seconds": None,
                "agent_setup_seconds": None,
                "agent_execution_seconds": None,
                "verifier_seconds": None,
                "input_tokens": None,
                "cache_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "trajectory_count": 1 if obs.get("steps_taken", 0) > 0 else 0,
                "invalid_trajectory_count": 0,
                "step_count": obs.get("steps_taken", 0),
                "llm_call_count": obs.get("steps_taken", 0),
                "tool_call_count": obs.get("steps_taken", 0),
                "command_failure_count": obs.get("tool_errors", 0),
                "repeated_failed_command_count": 1 if obs.get("loop_detected") else 0,
                "artifact_count": 0,
                "missing_artifact_count": 0,
                "artifact_set_digest": "",
            }
        )
    return facts


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
    t_craft = pa.Table.from_pylist(list(craft_records), schema=CRAFT_SCHEMA)
    t_trials = pa.Table.from_pylist(list(trial_facts), schema=TRIAL_FACT_SCHEMA)
    t_analysis = pa.Table.from_pylist(list(analysis_sidecars), schema=ANALYSIS_SIDECAR_SCHEMA)
    t_obs = pa.Table.from_pylist(list(observation_records), schema=OBSERVATION_RECORD_SCHEMA)

    con.register("craft", t_craft)
    con.register("trial_facts", t_trials)
    con.register("analysis_sidecars", t_analysis)
    con.register("observation_records", t_obs)

    resolved_sql = sql_path if sql_path is not None else SQL_LESSONS_PATH
    if resolved_sql.is_file():
        con.execute(resolved_sql.read_text(encoding="utf-8"))


def execute_lessons_views(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, list[dict[str, Any]]]:
    """Query each lesson view and return dictionary of row dictionaries."""
    views = ["v_failure_by_facet", "v_loop_rate_by_env", "v_outcome_by_verifier_type"]
    results: dict[str, list[dict[str, Any]]] = {}

    for view_name in views:
        cursor = con.execute(f"SELECT * FROM {view_name}")
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
        category = str(row.get("failure_category", "unknown"))
        dimension = f"{facet_name}={facet_value} ({category})"

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
            finding = (
                f"pass_rate={pass_rate:.1%} [95% CI: {low:.1%}-{high:.1%}, n={n}], "
                f"exceptions={exceptions}"
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
    facts = load_trial_facts(root, observations=observations)

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

    records_summary = {
        "craft_records": len(craft_records),
        "trial_facts": len(facts),
        "analysis_sidecars": len(sidecars),
        "observation_records": len(observations),
    }

    rankings_by_view = {
        view_name: rank_lesson_rows(rows)
        for view_name, rows in lessons_by_view.items()
    }

    inputs = collect_lessons_inputs(root, sql_path=sql_path)
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
                "Cross-tabulation of task verifier architecture against trial pass rates, "
                "exceptions, duration, and cost."
            ),
            "",
            (
                "| Source Repo | Verifier Type | n | Passed | Pass Rate | Wilson 95% CI | "
                "Exceptions | Exception Rate | Status | Finding |"
            ),
            "|---|---|---:|---:|---:|---|---:|---:|---|---|",
        ]
    )

    verifier_lessons = result.lessons_by_view.get("v_outcome_by_verifier_type", [])
    if verifier_lessons:
        for row in verifier_lessons:
            if row.n == 0:
                lines.append(
                    "| - | none | 0 | 0 | 0.0% | n/a | 0 | 0.0% | `insufficient n` | "
                    "insufficient n |"
                )
                continue
            det = row.details
            repo = str(det.get("source_repo", "corpus"))
            vtype = str(det.get("verifier_type", "unclassified"))
            passed = int(det.get("passed_n", 0))
            exceptions = int(det.get("exceptions_n", 0))
            exc_rate = float(det.get("exception_rate_pct", 0.0)) / 100.0
            ci_str = _format_ci(row.wilson_95)
            lines.append(
                f"| {repo} | {vtype} | {row.n} | {passed} | {row.rate:.1%} | {ci_str} | "
                f"{exceptions} | {exc_rate:.1%} | `{row.status}` | {row.finding} |"
            )
    else:
        lines.append(
            "| - | none | 0 | 0 | 0.0% | n/a | 0 | 0.0% | `insufficient n` | "
            "insufficient n |"
        )

    lines.extend(
        [
            "",
            "## 2. Loop Rate by Environment Complexity (`v_loop_rate_by_env`)",
            "",
            "Analysis of repetitive tool loops vs multi-container and environment complexity.",
            "",
            (
                "| Source Repo | Services | Container Mode | Env Files | n | Loops | Loop Rate | "
                "Wilson 95% CI | Avg Steps | Avg Tool Errors | Status | Finding |"
            ),
            "|---|---:|---|---|---:|---:|---:|---|---:|---:|---|---|",
        ]
    )

    loop_lessons = result.lessons_by_view.get("v_loop_rate_by_env", [])
    if loop_lessons:
        for row in loop_lessons:
            if row.n == 0:
                lines.append(
                    "| - | 0 | single | 0_files | 0 | 0 | 0.0% | n/a | n/a | n/a | "
                    "`insufficient n` | insufficient n |"
                )
                continue
            det = row.details
            repo = str(det.get("source_repo", "corpus"))
            services = det.get("env_services_n", 1)
            multi = "multi" if det.get("env_multi_container") else "single"
            files_b = str(det.get("env_files_bucket", "unknown"))
            loops = row.k
            ci_str = _format_ci(row.wilson_95)
            avg_s = det.get("avg_steps")
            avg_e = det.get("avg_tool_errors")
            avg_s_str = f"{avg_s:.1f}" if avg_s is not None else "n/a"
            avg_e_str = f"{avg_e:.1f}" if avg_e is not None else "n/a"
            lines.append(
                f"| {repo} | {services} | {multi} | {files_b} | {row.n} | {loops} | "
                f"{row.rate:.1%} | {ci_str} | {avg_s_str} | {avg_e_str} | "
                f"`{row.status}` | {row.finding} |"
            )
    else:
        lines.append(
            "| - | 0 | single | 0_files | 0 | 0 | 0.0% | n/a | n/a | n/a | "
            "`insufficient n` | insufficient n |"
        )

    lines.extend(
        [
            "",
            "## 3. Failure by Craft Facet (`v_failure_by_facet`)",
            "",
            (
                "Taxonomy breakdown of agent and infrastructure failures "
                "across structural task facets."
            ),
            "",
            (
                "| Source Repo | Facet Name | Facet Value | Category | Validity | n | Failures | "
                "Failure Rate | Wilson 95% CI | Status | Finding |"
            ),
            "|---|---|---|---|---|---:|---:|---:|---|---|---|",
        ]
    )

    failure_lessons = result.lessons_by_view.get("v_failure_by_facet", [])
    if failure_lessons:
        for row in failure_lessons:
            if row.n == 0:
                lines.append(
                    "| - | none | none | none | unknown | 0 | 0 | 0.0% | n/a | "
                    "`insufficient n` | insufficient n |"
                )
                continue
            det = row.details
            repo = str(det.get("source_repo", "corpus"))
            fname = str(det.get("facet_name", "facet"))
            fval = str(det.get("facet_value", "value"))
            cat = str(det.get("failure_category", "none"))
            val = str(det.get("validity", "unknown"))
            ci_str = _format_ci(row.wilson_95)
            lines.append(
                f"| {repo} | {fname} | {fval} | {cat} | {val} | {row.n} | {row.k} | "
                f"{row.rate:.1%} | {ci_str} | `{row.status}` | {row.finding} |"
            )
    else:
        lines.append(
            "| - | none | none | none | unknown | 0 | 0 | 0.0% | n/a | "
            "`insufficient n` | insufficient n |"
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
