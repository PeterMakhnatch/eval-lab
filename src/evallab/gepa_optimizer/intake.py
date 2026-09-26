"""Replay retained Harbor specs while changing one pinned candidate artifact."""

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
    candidate_kind: str = "instructions",
    name: str | None = None,
    candidate_verifier_digest: str | None = None,
) -> ExperimentSpec:
    """Replace one candidate binding, clearing run identity and prior authorization.

    Model, limits and other behavioral settings remain unchanged. The
    ``task_package`` kind rebinds the task itself to a validated candidate
    package (see ``evallab.task_candidate``): the base spec's task must be the
    original, and the candidate package must have passed validity checks
    before replay. Stale preamble/toolbox levers are cleared because the
    candidate carries its own instruction.
    """
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise ValueError("candidate_path must be repository-relative")
    relative_candidate = candidate_path.as_posix()
    relative_jobs = jobs_dir.as_posix() if isinstance(jobs_dir, Path) else str(jobs_dir)
    if Path(relative_jobs).is_absolute() or ".." in Path(relative_jobs).parts:
        raise ValueError("jobs_dir must be repository-relative")
    if base_spec.jobs_dir != EXPLORATION_JOBS_ROOT:
        raise ValueError(
            f"Retained spec jobs_dir must be {EXPLORATION_JOBS_ROOT!r}, got {base_spec.jobs_dir!r}"
        )
    if relative_jobs != base_spec.jobs_dir:
        raise ValueError(
            f"Replay jobs_dir {relative_jobs!r} does not match retained spec {base_spec.jobs_dir!r}"
        )
    if candidate_kind == "instructions":
        path_field, digest_field = "extra_instruction_path", "extra_instruction_sha256"
        label, prefix = "GEPA prompt", "gepa"
    elif candidate_kind == "python_toolbox":
        path_field, digest_field = "toolbox_path", "toolbox_sha256"
        label, prefix = "GEPA toolbox", "gepa"
    elif candidate_kind == "terminus_harness":
        path_field, digest_field = "harness_tree_path", "harness_tree_sha256"
        label, prefix = "Terminus harness", "harness"
    elif candidate_kind == "task_package":
        path_field, digest_field = "task_path", "task_package_digest"
        label, prefix = "GEPA task package", "gepatask"
    else:
        raise ValueError(f"Unsupported candidate_kind: {candidate_kind!r}")
    if candidate_verifier_digest is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", candidate_verifier_digest
    ):
        raise ValueError(
            f"candidate_verifier_digest must be a sha256:... 64-hex string, "
            f"got {candidate_verifier_digest!r}"
        )
    update: dict[str, object] = {
        "spec_id": None,
        "submitted_at": None,
        "submitted_by": f"{prefix}-replay",
        "name": name
        or _replay_spec_name(campaign_name, candidate_path, candidate_sha256, prefix=prefix),
        "hypothesis": (
            f"{label} candidate {candidate_sha256[:16]} "
            f"replayed from retained spec {base_spec.spec_id or base_spec.name}"
        ),
        "jobs_dir": base_spec.jobs_dir,
        path_field: relative_candidate,
        digest_field: candidate_sha256,
        "policy_rule": None,
    }
    if candidate_kind != "terminus_harness":
        update["grid_point"] = None
    if candidate_kind == "task_package":
        update["task"] = relative_candidate
        update["extra_instruction_path"] = None
        update["extra_instruction_sha256"] = None
        update["toolbox_path"] = None
        update["toolbox_sha256"] = None
        if candidate_verifier_digest is not None:
            update["verifier_digest"] = candidate_verifier_digest
    for field in _CAMPAIGN_FIELDS:
        update[field] = None
    return ExperimentSpec.model_validate(base_spec.model_dump(mode="json") | update)


def validate_drift(base_spec: ExperimentSpec, current_task_digest: str) -> None:
    """Refuse replay when the retained task package no longer matches disk."""
    changed: list[str] = []
    if base_spec.task_package_digest != current_task_digest:
        changed.append("task_package_digest")
    if changed:
        raise ValueError("Retained spec drifted from current task: " + ", ".join(changed))


def _replay_spec_name(
    campaign_name: str, candidate_path: Path, candidate_sha256: str, *, prefix: str = "gepa"
) -> str:
    sanitized = re.sub(r"[^a-z0-9]+", "-", campaign_name.lower()).strip("-") or "campaign"
    path_tag = hashlib.sha256(candidate_path.as_posix().encode("utf-8")).hexdigest()[:12]
    digest_hex = candidate_sha256.split(":", 1)[-1]
    sha_tag = digest_hex[:8]
    suffix = f"{path_tag}{sha_tag}"
    budget = 80 - len(prefix) - 2 - len(suffix)
    sanitized = sanitized[: max(budget, 1)].strip("-") or "c"
    name = re.sub(r"-+", "-", f"{prefix}-{sanitized}-{suffix}").strip("-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", name):
        raise ValueError(f"replay spec name {name!r} is not a valid ExperimentSpec name")
    return name
