"""Stored task-catalog schemas and read-only outcome views."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from evallab.mimo_exploit import TABLE_FILENAME as EXPLOITS_PARQUET_FILENAME
from evallab.task_stability import TABLE_FILENAME as STABILITY_PARQUET_FILENAME

CATALOG_RELPATH = Path("external/task_catalog")
STABILITY_TABLE_FILENAME = STABILITY_PARQUET_FILENAME
STABILITY_TABLE = Path(STABILITY_TABLE_FILENAME).stem
EXPLOITS_TABLE_FILENAME = EXPLOITS_PARQUET_FILENAME
EXPLOITS_TABLE = Path(EXPLOITS_TABLE_FILENAME).stem




TASK_SOURCES_SCHEMA = pa.schema(
    [
        pa.field("source_repo", pa.string()),
        pa.field("source_revision", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("snapshot_relpath", pa.string()),
        pa.field("n_manifest", pa.int64()),
        pa.field("n_disk", pa.int64()),
        pa.field("n_registry", pa.int64()),
        pa.field("license", pa.string()),
        pa.field("material_digest", pa.string()),
    ]
)

TASK_VERSIONS_SCHEMA = pa.schema(
    [
        pa.field("task_version_digest", pa.string()),
        pa.field("harbor_digest", pa.string()),
        pa.field("task_name", pa.string()),
        pa.field("task_id", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("source_repo", pa.string()),
        pa.field("source_revision", pa.string()),
        pa.field("source_path", pa.string()),
        pa.field("upstream_manifest_sha256", pa.string()),
        pa.field("parent_digest", pa.string()),
        pa.field("transform", pa.string()),
        pa.field("digest_task_toml", pa.string()),
        pa.field("digest_instruction", pa.string()),
        pa.field("digest_environment", pa.string()),
        pa.field("digest_verifier", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("category", pa.string()),
        pa.field("docker_image", pa.string()),
        pa.field("grader_kind", pa.string()),
        pa.field("grader_cost", pa.string()),
        pa.field("has_solution", pa.bool_()),
        pa.field("network_mode", pa.string()),
        pa.field("agent_user", pa.string()),
        pa.field("step_limit", pa.int64()),
        pa.field("agent_timeout_sec", pa.float64()),
        pa.field("verifier_timeout_sec", pa.float64()),
        pa.field("cpus", pa.int64()),
        pa.field("memory_mb", pa.int64()),
        pa.field("instruction_chars", pa.int64()),
        pa.field("instruction_lang", pa.string()),
        pa.field("split_group", pa.string()),
    ]
)

TASK_FINDINGS_SCHEMA = pa.schema(
    [
        pa.field("task_version_digest", pa.string()),
        pa.field("task_id", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("rule", pa.string()),
        pa.field("severity", pa.string()),
        pa.field("message", pa.string()),
    ]
)

TASK_LINEAGE_SCHEMA = pa.schema(
    [
        pa.field("child_digest", pa.string()),
        pa.field("child_harbor_digest", pa.string()),
        pa.field("parent_digest", pa.string()),
        pa.field("parent_harbor_digest", pa.string()),
        pa.field("transform", pa.string()),
        pa.field("record_path", pa.string()),
        pa.field("origin", pa.string()),
    ]
)




def task_outcomes_sql(trials: str = "trial_facts", versions: str = "task_versions") -> str:
    """Per task-version x backend x agent/model outcome rollup (Harbor digest join).

    The legacy ``n_infra`` column means unscored (missing, nonfinite, or -1
    reward), not that an agent exception necessarily caused infrastructure
    failure. A scored exception remains in the pass-rate denominator.
    Wilson 95% interval is computed on scored attempts only.
    ``backend`` is ``COALESCE(environment_type, 'unknown')``:
    local and cloud trials never pool into one row. Old trial_facts
    partitions without ``environment_type`` read as ``'unknown'`` through
    ``union_by_name``.
    """
    inner = f"""
        SELECT
            v.task_version_digest AS task_version_digest,
            v.harbor_digest AS harbor_digest,
            v.task_id AS task_id,
            v.domain AS domain,
            COALESCE(t.environment_type, 'unknown') AS backend,
            COALESCE(t.agent_name, '') AS agent_name,
            COALESCE(t.model_name, '') AS model_name,
            COUNT(t.trial_id) AS n_attempts,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL AND (
                t.primary_reward IS NULL OR NOT isfinite(t.primary_reward)
                OR t.primary_reward = -1) THEN 1 ELSE 0 END), 0) AS n_infra,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND isfinite(t.primary_reward)
                AND t.primary_reward <> -1 THEN 1 ELSE 0 END), 0) AS n_scored,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND isfinite(t.primary_reward)
                AND t.primary_reward <> -1 AND t.primary_reward >= 1.0
                THEN 1 ELSE 0 END), 0) AS n_pass,
            AVG(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND isfinite(t.primary_reward)
                AND t.primary_reward <> -1 THEN t.primary_reward END) AS mean_reward
        FROM {versions} AS v
        LEFT JOIN {trials} AS t ON t.task_digest = v.harbor_digest
        GROUP BY v.task_version_digest, v.harbor_digest, v.task_id, v.domain,
            COALESCE(t.environment_type, 'unknown'),
            COALESCE(t.agent_name, ''), COALESCE(t.model_name, '')
    """
    return f"""
    SELECT
        task_version_digest, harbor_digest, task_id, domain, backend,
        agent_name, model_name, n_attempts, n_scored, n_infra, n_pass, mean_reward,
        CASE WHEN n_scored > 0 THEN n_pass * 1.0 / n_scored END AS pass_rate,
        CASE WHEN n_scored > 0 THEN GREATEST(0.0,
            ((n_pass * 1.0 / n_scored + 3.8416 / (2 * n_scored)) / (1 + 3.8416 / n_scored))
            - (1.96 * SQRT((n_pass * 1.0 / n_scored) * (1 - n_pass * 1.0 / n_scored)
                / n_scored + 3.8416 / (4 * n_scored * n_scored))
                / (1 + 3.8416 / n_scored))) END AS pass_rate_lo,
        CASE WHEN n_scored > 0 THEN LEAST(1.0,
            ((n_pass * 1.0 / n_scored + 3.8416 / (2 * n_scored)) / (1 + 3.8416 / n_scored))
            + (1.96 * SQRT((n_pass * 1.0 / n_scored) * (1 - n_pass * 1.0 / n_scored)
                / n_scored + 3.8416 / (4 * n_scored * n_scored))
                / (1 + 3.8416 / n_scored))) END AS pass_rate_hi,
        CASE
            WHEN n_attempts = 0 THEN 'untested'
            WHEN n_scored = 0 THEN 'infra_only'
            WHEN n_pass * 1.0 / n_scored >= 1.0 THEN 'always_pass'
            WHEN n_pass * 1.0 / n_scored <= 0.0 THEN 'always_fail'
            ELSE 'learnable'
        END AS verdict
    FROM ({inner}) AS rollup
    """


def task_audit_sql(
    outcomes: str = "v_task_outcomes",
    versions: str = "task_versions",
    stability: str | None = None,
    exploits: str | None = None,
    qualification: str | None = None,
    findings: str | None = None,
    *,
    has_stability: bool = True,
    has_exploits: bool = True,
    has_qualification: bool = False,
    has_findings: bool = True,
) -> str:
    """One row per task version x backend x agent/model with verdict and eligibility.

    Missing stability/exploit tables yield null columns, never guessed
    values; eligibility is then false with a reason. Split assignment
    (train|heldout) is applied at export time, so the view stays
    split-agnostic and ``train_eligible`` here means learnable, stable, and
    free of confirmed exploits.

    With ``has_qualification``, the latest qualification trial per
    (task version, backend) joins on both keys: a version whose latest
    trial on that backend is ``broken`` is train-ineligible with reason
    ``broken on <backend>``. ``inconclusive`` (backend-quota) trials never
    mark a task broken. The join is one-to-one per audit row, never fanout.

    With ``has_findings``, an ``error``-severity row in the catalog
    ``task_findings`` table (curated defects under
    ``library/task-findings/``) makes the version train-ineligible with
    reason ``finding: <rule>``, whatever the backend. This is a hard
    defect, so it takes precedence over every other ineligibility reason.
    """
    from evallab.task_qualification import TABLE as QUALIFICATION_TABLE_DEFAULT

    stability_table = stability or STABILITY_TABLE
    exploits_table = exploits or EXPLOITS_TABLE
    qualification_table = qualification or QUALIFICATION_TABLE_DEFAULT
    findings_table = findings or "task_findings"
    stability_source = (
        f"(SELECT task_version_digest, verdict, n_runs, evidence_path FROM {stability_table})"
        if has_stability
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS verdict, CAST(NULL AS INTEGER) AS n_runs, "
        "CAST(NULL AS VARCHAR) AS evidence_path WHERE FALSE)"
    )
    exploits_source = (
        f"(SELECT task_version_digest, exploit_status, evidence_path FROM {exploits_table})"
        if has_exploits
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS exploit_status, "
        "CAST(NULL AS VARCHAR) AS evidence_path WHERE FALSE)"
    )
    qualification_source = (
        f"(SELECT task_version_digest, backend, status, finished_at,"
        f" job_name, trial_name FROM {qualification_table})"
        if has_qualification
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS backend, CAST(NULL AS VARCHAR) AS status, "
        "CAST(NULL AS VARCHAR) AS finished_at, CAST(NULL AS VARCHAR) AS job_name, "
        "CAST(NULL AS VARCHAR) AS trial_name WHERE FALSE)"
    )
    findings_source = (
        f"(SELECT task_version_digest, rule FROM {findings_table} WHERE severity = 'error')"
        if has_findings
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS rule WHERE FALSE)"
    )
    return f"""
    WITH stab AS (
        SELECT s.task_version_digest AS digest,
            SUM(s.n_runs) AS stability_runs,
            CASE
                WHEN SUM(CASE WHEN s.verdict = 'flipped' THEN 1 ELSE 0 END) > 0 THEN 'flipped'
                WHEN SUM(CASE WHEN s.verdict = 'errored' THEN 1 ELSE 0 END) > 0 THEN 'errored'
                WHEN COUNT(*) > 0 THEN 'stable'
            END AS stability,
            LIST(s.evidence_path) AS stability_evidence_paths
        FROM {stability_source} AS s
        GROUP BY s.task_version_digest
    ),
    expl AS (
        SELECT e.task_version_digest AS digest,
            CASE
                WHEN SUM(CASE WHEN e.exploit_status = 'confirmed' THEN 1 ELSE 0 END) > 0
                    THEN 'confirmed'
                WHEN SUM(CASE WHEN e.exploit_status = 'suspected' THEN 1 ELSE 0 END) > 0
                    THEN 'suspected'
                WHEN SUM(CASE WHEN e.exploit_status = 'none' THEN 1 ELSE 0 END) > 0 THEN 'none'
                WHEN COUNT(*) > 0 THEN 'not_probed'
            END AS exploit_status,
            LIST(e.evidence_path) AS exploit_evidence_paths
        FROM {exploits_source} AS e
        GROUP BY e.task_version_digest
    ),
    qual_ranked AS (
        SELECT q.task_version_digest AS digest, q.backend AS backend,
            q.status AS status,
            ROW_NUMBER() OVER (
                PARTITION BY q.task_version_digest, q.backend
                ORDER BY q.finished_at DESC NULLS LAST, q.job_name DESC, q.trial_name DESC
            ) AS rn
        FROM {qualification_source} AS q
    ),
    qual AS (
        SELECT digest, backend,
            MAX(CASE WHEN rn = 1 AND status = 'broken' THEN 1 ELSE 0 END) AS is_broken
        FROM qual_ranked
        GROUP BY digest, backend
    ),
    ferr AS (
        SELECT f.task_version_digest AS digest,
            MIN(f.rule) AS rule
        FROM {findings_source} AS f
        GROUP BY f.task_version_digest
    )
    SELECT
        v.task_version_digest AS task_version_digest,
        v.harbor_digest AS harbor_digest,
        v.task_id AS task_id,
        v.task_name AS task_name,
        v.domain AS domain,
        v.split_group AS split_group,
        v.grader_kind AS grader_kind,
        v.grader_cost AS grader_cost,
        o.backend AS backend,
        o.agent_name AS agent_name,
        o.model_name AS model_name,
        o.n_attempts AS n_attempts,
        o.n_scored AS n_scored,
        o.n_infra AS n_infra,
        o.pass_rate AS pass_rate,
        o.pass_rate_lo AS pass_rate_lo,
        o.pass_rate_hi AS pass_rate_hi,
        o.verdict AS verdict,
        stab.stability AS stability,
        stab.stability_runs AS stability_runs,
        stab.stability_evidence_paths AS stability_evidence_paths,
        expl.exploit_status AS exploit_status,
        expl.exploit_evidence_paths AS exploit_evidence_paths,
        qual.is_broken AS qual_broken,
        ferr.rule AS finding_rule,
        COALESCE(o.verdict = 'learnable' AND stab.stability = 'stable'
            AND (expl.exploit_status IS NULL
                OR expl.exploit_status IN ('none', 'not_probed'))
            AND (qual.is_broken IS NULL OR qual.is_broken = 0)
            AND ferr.rule IS NULL, false) AS train_eligible,
        CASE
            WHEN ferr.rule IS NOT NULL THEN 'finding: ' || ferr.rule
            WHEN o.verdict IS NULL OR o.verdict != 'learnable'
                THEN 'not learnable: ' || COALESCE(o.verdict, 'missing')
            WHEN stab.stability IS NULL THEN 'no stability evidence'
            WHEN stab.stability != 'stable' THEN 'unstable verifier: ' || stab.stability
            WHEN expl.exploit_status = 'confirmed' THEN 'confirmed exploit'
            WHEN expl.exploit_status = 'suspected' THEN 'suspected exploit awaiting review'
            WHEN qual.is_broken = 1 THEN 'broken on ' || COALESCE(o.backend, 'unknown')
        END AS train_ineligible_reason
    FROM {versions} AS v
    LEFT JOIN {outcomes} AS o ON o.task_version_digest = v.task_version_digest
    LEFT JOIN stab ON stab.digest = v.task_version_digest
    LEFT JOIN expl ON expl.digest = v.task_version_digest
    LEFT JOIN qual ON qual.digest = v.task_version_digest AND qual.backend = o.backend
    LEFT JOIN ferr ON ferr.digest = v.task_version_digest
    """


