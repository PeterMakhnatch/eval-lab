"""Replay a retained Harbor ExperimentSpec as GEPA candidate configuration."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from evallab.queue import read_spec
from evallab.schemas import EXPLORATION_JOBS_ROOT, ExperimentSpec

_CAMPAIGN_FIELDS: tuple[str, ...] = (
    "campaign_ledger",
    "campaign_cell_id",
    "campaign_attempt_id",
    "campaign_attempt_index",
    "campaign_manifest_digest",
    "campaign_spec_digest",
    "campaign_evidence_store",
)


def load_retained_spec(path: Path) -> ExperimentSpec:
    """Load a queued or archived spec through ordinary queue JSON validation."""
    return read_spec(Path(path))


def replay_spec_for_candidate(
    base_spec: ExperimentSpec,
    *,
    campaign_name: str,
    candidate_path: Path,
    candidate_sha256: str,
    jobs_dir: str | Path,
) -> ExperimentSpec:
    """Copy a retained run's configuration onto a new GEPA candidate spec.

    Identity, submission metadata, instruction artifact, grid_point and
    campaign provenance are replaced; every other field is copied verbatim.
    """
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise ValueError("candidate_path must be repository-relative")
    relative_candidate = candidate_path.as_posix()
    relative_jobs = jobs_dir.as_posix() if isinstance(jobs_dir, Path) else str(jobs_dir)
    if Path(relative_jobs).is_absolute() or ".." in Path(relative_jobs).parts:
        raise ValueError("jobs_dir must be repository-relative")
    if base_spec.jobs_dir != EXPLORATION_JOBS_ROOT:
        raise ValueError(
            f"Retained spec jobs_dir must be {EXPLORATION_JOBS_ROOT!r}, "
            f"got {base_spec.jobs_dir!r}"
        )
    if relative_jobs != base_spec.jobs_dir:
        raise ValueError(
            f"Replay jobs_dir {relative_jobs!r} does not match retained spec "
            f"{base_spec.jobs_dir!r}"
        )
    update: dict[str, object] = {
        "spec_id": None,
        "submitted_at": None,
        "submitted_by": "gepa-replay",
        "name": _replay_spec_name(campaign_name, candidate_path, candidate_sha256),
        "hypothesis": (
            f"GEPA prompt candidate {candidate_sha256[:16]} "
            f"replayed from retained spec {base_spec.spec_id or base_spec.name}"
        ),
        "jobs_dir": base_spec.jobs_dir,
        "extra_instruction_path": relative_candidate,
        "extra_instruction_sha256": candidate_sha256,
        "grid_point": None,
    }
    for field in _CAMPAIGN_FIELDS:
        update[field] = None
    if not (base_spec.toolbox_path and base_spec.toolbox_sha256):
        update["toolbox_path"] = None
        update["toolbox_sha256"] = None
    return base_spec.model_copy(update=update)


def validate_drift(base_spec: ExperimentSpec, current_task_digest: str) -> None:
    """Refuse replay when the retained task package no longer matches disk."""
    changed: list[str] = []
    if base_spec.task_package_digest != current_task_digest:
        changed.append("task_package_digest")
    if changed:
        raise ValueError("Retained spec drifted from current task: " + ", ".join(changed))


def _replay_spec_name(campaign_name: str, candidate_path: Path, candidate_sha256: str) -> str:
    sanitized = re.sub(r"[^a-z0-9]+", "-", campaign_name.lower()).strip("-") or "campaign"
    path_tag = hashlib.sha256(candidate_path.as_posix().encode("utf-8")).hexdigest()[:12]
    digest_hex = candidate_sha256.split(":", 1)[-1]
    sha_tag = digest_hex[:8]
    suffix = f"{path_tag}{sha_tag}"
    budget = 80 - len("gepa-") - 1 - len(suffix)
    sanitized = sanitized[: max(budget, 1)].strip("-") or "c"
    name = re.sub(r"-+", "-", f"gepa-{sanitized}-{suffix}").strip("-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", name):
        raise ValueError(f"replay spec name {name!r} is not a valid ExperimentSpec name")
    return name
