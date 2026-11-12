"""Pinned MiMo HF snapshot intake and task-catalog Parquet builds.

This module owns the data side of Peter's MiMo RL-environment experiments:

1. ``pull_hf_snapshot`` downloads one pinned ``<org>/<repo>@<40-hex-sha>``
   Hugging Face dataset snapshot anonymously into the shared derived store at
   ``derived/task-store/hf/<org>__<repo>@<rev12>/`` under the primary
   checkout root (``default_task_store_root``, shared across worktrees),
   makes it read-only, and records a ``ProvenanceMetadata`` sidecar
   (zone ``01-external``). Idempotent: a complete snapshot whose provenance
   and material digest still match is reused; any mismatch is refused, never
   overwritten.
2. ``build_catalog`` scans pulled snapshots (plus git-tracked lineage records
   under ``library/task-variants/``) and writes the four contract Parquet
   tables under ``<derived root>/parquet/external/task_catalog/``.
3. ``show_task`` prints one task version: identity, digests, source pin,
   findings, lineage, and outcome rows.

Per-task bytes are verified against the adapter's ``manifest.json`` with the
exact hashing the adapter uses (``relpath + NUL + bytes + NUL`` per file,
sorted); mismatches become findings, never crashes. Harbor join digests come
from ``registry.harbor_task_digest``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from evallab.mimo_exploit import TABLE_FILENAME as EXPLOITS_PARQUET_FILENAME
from evallab.storage.paths import derived_root_from_environment, shared_checkout_root
from evallab.task_stability import TABLE_FILENAME as STABILITY_PARQUET_FILENAME



def _repo_relative(path: Path, repo_root: Path) -> str:
    """Portable path for git-tracked records; absolute only outside the repo."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)



MIMO_HF_PINS: dict[str, str] = {
    "code": "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
    "cyber": "763882ade5fc018892f1aa3c559f997138eb92cc",
    "general": "10b732c5079c47244a77402f5759d62763800f20",
    "terminal": "fe1c2b665aae1ba7a09a270d979724d32269ae6a",
    "webdev": "e1a6293376e8910e1fb1f28efabd93dad2f8b475",
    "music": "e1a66d4553ee20b26c571de1bc2f4193d4a32c3c",
}

#: Expected on-disk task counts per domain (registry/manifest/jsonl agree).
MIMO_EXPECTED_COUNTS: dict[str, int] = {
    "code": 2698,
    "cyber": 1000,
    "general": 925,
    "terminal": 64,
    "webdev": 2093,
    "music": 1000,
}

TASK_STORE_DIRNAME = "task-store"
VARIANTS_DIRNAME = "variants"
CATALOG_RELPATH = Path("external/task_catalog")
CATALOG_TABLES = ("task_sources", "task_versions", "task_findings", "task_lineage")
STABILITY_TABLE_FILENAME = STABILITY_PARQUET_FILENAME
STABILITY_TABLE = Path(STABILITY_TABLE_FILENAME).stem
EXPLOITS_TABLE_FILENAME = EXPLOITS_PARQUET_FILENAME
EXPLOITS_TABLE = Path(EXPLOITS_TABLE_FILENAME).stem
PROVENANCE_FILENAME = "provenance.json"
SNAPSHOT_SKIP_NAMES = frozenset({PROVENANCE_FILENAME})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_RL_SUFFIX = re.compile(r"_rl_\d+$")
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\㐀-\䶿豈-\﫿]")
_GO_TEST_TARGET = re.compile(r"go test\s+(?:-[^\s]+\s+)*(\S+)")
_HOSTED_REPO = re.compile(r"^(github\.com|gitlab\.com|bitbucket\.org)/([^/]+)/([^/]+)")
_TEST_HELPER_MODULES = frozenset({"github.com/stretchr/testify"})


class CatalogError(ValueError):
    """User-facing pull/build/show refusal."""


#: Durable curated-defect record schema. Owned here, read by the catalog build.
FINDING_RECORD_SCHEMA = "evallab.task_finding/v1"

#: Git-tracked curated finding records live under this repo-relative directory.
FINDINGS_DIRNAME = Path("library/task-findings")


















def catalog_dir(derived_root: Path) -> Path:
    """Rebuildable catalog root: ``<derived root>/parquet/external/task_catalog``."""
    return derived_root / CATALOG_RELPATH


def default_task_store_root(repo_root: Path) -> Path:
    """HF snapshot store: shared across worktrees, outside Git.

    Resolves under the *primary* checkout's ``derived/task-store`` (not the
    Parquet derived root), mirroring ``task_variants.default_variants_root``,
    so every worktree pulls into the one store the catalog build scans.
    """
    return shared_checkout_root(repo_root) / "derived" / TASK_STORE_DIRNAME


def task_store_root(repo_root: Path, *, derived_root: Path | None = None) -> Path:
    """Resolve the shared task-store root without touching the worktree."""
    if derived_root is not None:
        return derived_root / TASK_STORE_DIRNAME
    return default_task_store_root(repo_root)




"""Download collaborator: ``(repo_id, revision, dest_dir)``. Tests inject fakes."""























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













_REPO_URL = re.compile(
    r"https?://(github\.com|gitlab\.com)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
)




































def wilson_interval(n_pass: int, n_scored: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson score interval for a pass rate (95% by default; null when unscored)."""
    if n_scored <= 0:
        return None, None
    rate = n_pass / n_scored
    denom = 1.0 + z * z / n_scored
    center = (rate + z * z / (2.0 * n_scored)) / denom
    margin = (
        z * math.sqrt(rate * (1.0 - rate) / n_scored + z * z / (4.0 * n_scored * n_scored)) / denom
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def outcome_verdict(n_attempts: int, n_scored: int, n_pass: int) -> str:
    """HAR-83 verdict: infra attempts never count as pass or fail.

    nop/control trials stay distinguishable because the view groups by agent
    name and model; they never merge into model learnability rows.
    """
    if n_attempts == 0:
        return "untested"
    if n_scored == 0:
        return "infra_only"
    rate = n_pass / n_scored
    if rate >= 1.0:
        return "always_pass"
    if rate <= 0.0:
        return "always_fail"
    return "learnable"


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

@dataclass(frozen=True)
class ExportResult:
    """Outcome of one train-eligible export."""

    path: Path
    sha256: str
    n_eligible: int
    provisional: bool
    table_digests: dict[str, str | None]


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _read_split_map(split_path: Path | None) -> dict[str, str]:
    if split_path is None:
        return {}
    try:
        payload = json.loads(split_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"unreadable split file {split_path}: {exc}") from exc
    mapping = payload.get("splits") if isinstance(payload, dict) else None
    if mapping is None:
        mapping = payload
    if not isinstance(mapping, dict):
        raise CatalogError(f"split file {split_path} must map digests to train|heldout")
    splits: dict[str, str] = {}
    for digest, split in mapping.items():
        if split not in ("train", "heldout"):
            raise CatalogError(
                f"split file {split_path}: {digest!r} maps to {split!r} (want train|heldout)"
            )
        splits[str(digest)] = str(split)
    return splits


def export_train_eligible(
    out: Path,
    *,
    repo_root: Path,
    derived_root: Path | None = None,
    split_path: Path | None = None,
) -> ExportResult:
    """Write the git-tracked train-eligible JSON with table digests and a self hash.

    Eligibility = view ``train_eligible`` and split != heldout. Without a
    split file the export is marked provisional and splits stay unassigned.
    """
    from evallab.storage.attach import attach
    from evallab.task_qualification import TABLE as QUALIFICATION_TABLE
    from evallab.task_qualification import TABLE_FILENAME as QUALIFICATION_PARQUET_FILENAME

    derived = derived_root or derived_root_from_environment(repo_root)
    catalog = catalog_dir(derived)
    if not (catalog / "task_versions.parquet").is_file():
        raise CatalogError(f"no catalog tables under {catalog}; run catalog build first")
    table_digests: dict[str, str | None] = {}
    for name, filename in (
        *((name, f"{name}.parquet") for name in CATALOG_TABLES),
        (STABILITY_TABLE, STABILITY_TABLE_FILENAME),
        (EXPLOITS_TABLE, EXPLOITS_TABLE_FILENAME),
        (QUALIFICATION_TABLE, QUALIFICATION_PARQUET_FILENAME),
    ):
        path = catalog / filename
        table_digests[name] = _sha256_file(path) if path.is_file() else None

    splits = _read_split_map(split_path)
    result = attach(repo_root=repo_root, explicit_derived=derived)
    try:
        rows = result.connection.execute(
            "SELECT task_version_digest, harbor_digest, task_name, split_group, backend, "
            "agent_name, model_name, verdict, pass_rate, stability, exploit_status, "
            "train_eligible, train_ineligible_reason FROM v_task_audit"
        ).fetchall()
        columns = [desc[0] for desc in result.connection.description]
    finally:
        result.connection.close()
    provisional = split_path is None
    items: list[dict[str, Any]] = []
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        if not record.get("train_eligible"):
            continue
        digest = str(record["task_version_digest"])
        split = splits.get(digest, "unassigned")
        if split == "heldout":
            continue
        items.append(
            {
                "task_version_digest": digest,
                "harbor_digest": record.get("harbor_digest"),
                "task_name": record.get("task_name"),
                "split_group": record.get("split_group"),
                "split": split,
                "backend": record.get("backend"),
                "agent_name": record.get("agent_name"),
                "model_name": record.get("model_name"),
                "verdict": record.get("verdict"),
                "pass_rate": record.get("pass_rate"),
                "stability": record.get("stability"),
                "exploit_status": record.get("exploit_status"),
            }
        )
    items.sort(
        key=lambda item: (
            str(item["task_version_digest"]),
            str(item["backend"]),
            str(item["agent_name"]),
            str(item["model_name"]),
        )
    )
    # The digest names the training set itself: same items, same digest, no
    # matter when or from which table snapshot it was exported (see meta).
    identity = {"schema": "evallab.train_eligible/v1", "items": items}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    sha = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    meta = {
        "created_at": datetime.now(UTC).isoformat(),
        "provisional": provisional,
        "split_path": _repo_relative(split_path, repo_root) if split_path else None,
        "table_digests": table_digests,
        "n_eligible": len(items),
        "n_considered": len(rows),
    }
    payload = {**identity, "meta": meta}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({**payload, "sha256": sha}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ExportResult(
        path=out, sha256=sha, n_eligible=len(items),
        provisional=provisional, table_digests=table_digests,
    )
