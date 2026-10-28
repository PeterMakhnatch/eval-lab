"""Task QA gate-rerun progress for the dashboard: counts from the OQ-6 coverage summaries.

``<data_root>/task-qa/gate-progress-v1.json`` (``evallab.gate_progress/v1``, path configurable
with ``EVALLAB_GATE_PROGRESS``) says how far the corrected Task QA gate rerun has got on the 399
DEV tasks (44 keyed, 355 unkeyed): per auditor model and arm, tasks answered, consumed, quota
pending and failed, and the keyed recall and precision at the fixed 0.5 floor next to the old 0.8
gate. It holds integers and route names only: no task id, case id, finding, key or source text.

The exporter (``evallab prod export-gate-progress``) reads two retained, aggregate-only files:
the OQ-6 coverage summary (``coverage.py --aggregate-only``, schema
``oq6.stage-continuation-coverage-summary/v1``) and the v4 checkpoint summary
(``checkpoint-v4-summary.json``, which fixes the cohort and the answered counts at its checkpoint).
It copies named integer fields and nothing else; anything else in those files is never read into
the output. Every recall and precision is a **keyed subset** result: unkeyed tasks are not
certified negatives, so none of these numbers is a population false-alarm rate.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evallab.production.atlas_view import read_regular, strict_loads

SCHEMA = "evallab.gate_progress/v1"
FILE = "gate-progress-v1.json"
DEFAULT_FILE = f"task-qa/{FILE}"
MAX_BYTES = 256 * 1024
COVERAGE_SCHEMA = "oq6.stage-continuation-coverage-summary/v1"
CHECKPOINT_SCHEMA = "oq6.v4-checkpoint-aggregate/v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
SCOPE = "keyed subset, not population false-alarm rate"

_Count = Annotated[int, Field(ge=0)]
_Route = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,40}/[A-Za-z0-9][A-Za-z0-9._:-]{0,60}$")]
_Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Gate(_Strict):
    """Matched keys and eligible findings of one gate on the tasks read so far."""

    matched_keys: _Count
    eligible_findings: _Count

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.matched_keys > self.eligible_findings:
            raise ValueError("a matched key is one eligible finding, so matches cannot exceed findings")
        return self


class Cohort(_Strict):
    dev_tasks: Annotated[int, Field(gt=0)]
    keyed_tasks: _Count
    unkeyed_tasks: _Count
    planned_keys: _Count

    @model_validator(mode="after")
    def validate_cohort(self) -> Self:
        if self.keyed_tasks + self.unkeyed_tasks != self.dev_tasks:
            raise ValueError("keyed plus unkeyed tasks must be the DEV tasks")
        return self


class Source(_Strict):
    coverage_sha256: _Sha
    checkpoint_sha256: _Sha


class Row(_Strict):
    route: _Route
    arm: Literal["A", "B"]
    answered: _Count
    consumed: _Count
    quota_pending: _Count
    failed: _Count
    scorer_quota_pending: _Count
    fully_scored: _Count
    baseline_answered: _Count
    keys_read: _Count
    new_0_5: Gate | None
    old_0_8: Gate | None


class Progress(_Strict):
    schema_: Literal["evallab.gate_progress/v1"] = Field(alias="schema")
    exported_at: Annotated[str, Field(min_length=1, max_length=64)]
    source: Source
    cohort: Cohort
    rows: Annotated[list[Row], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if datetime.fromisoformat(self.exported_at).tzinfo is None:
            raise ValueError("an export time must carry its timezone")
        keys = [(row.route, row.arm) for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("a model and arm appear twice")
        total = self.cohort.dev_tasks
        for row in self.rows:
            if not row.baseline_answered <= row.answered <= row.consumed <= total:
                raise ValueError("answered tasks must lie between the checkpoint count and the consumed tasks")
            if row.consumed + row.quota_pending > total or row.failed > row.consumed:
                raise ValueError("consumed, pending and failed tasks do not fit the cohort")
            if row.scorer_quota_pending > row.answered or row.fully_scored > row.answered:
                raise ValueError("scoring counts exceed the answered tasks")
            if row.keys_read > self.cohort.planned_keys:
                raise ValueError("keys read exceed the planned keys")
            for gate in (row.new_0_5, row.old_0_8):
                if gate is not None and gate.matched_keys > row.keys_read:
                    raise ValueError("matched keys exceed the keys read")
            if (row.new_0_5 is None) != (row.old_0_8 is None):
                raise ValueError("both gates are read together or neither is")
        return self


def _served(document: Progress) -> dict[str, Any]:
    cohort = document.cohort
    rows = []
    for row in document.rows:
        rows.append(
            {
                "route": row.route,
                "arm": row.arm,
                "answered": row.answered,
                "consumed": row.consumed,
                "quota_pending": row.quota_pending,
                "failed": row.failed,
                "scorer_quota_pending": row.scorer_quota_pending,
                "fully_scored": row.fully_scored,
                "baseline_answered": row.baseline_answered,
                "keys_read": row.keys_read,
                "new_0_5": None if row.new_0_5 is None else row.new_0_5.model_dump(),
                "old_0_8": None if row.old_0_8 is None else row.old_0_8.model_dump(),
            }
        )
    rows.sort(key=lambda item: (item["route"], item["arm"]))
    return {
        "exported_at": document.exported_at,
        "cohort": cohort.model_dump(),
        "source": document.source.model_dump(),
        "rows": rows,
        "all_answered": all(row["answered"] == cohort.dev_tasks for row in rows),
        "scope": SCOPE,
    }


def load_progress(path: Path) -> dict[str, Any]:
    """The served progress, or why there is none; error text never repeats file contents."""
    base: dict[str, Any] = {"schema": SCHEMA, "present": False, "progress": None, "error": None}
    try:
        raw = read_regular(path.parent, path.name, MAX_BYTES)
        if raw is None:
            return base | {"error": "not retained"}
        document = Progress.model_validate(strict_loads(raw.decode("utf-8")))
    except ValidationError:
        return base | {"error": "invalid counts-only gate progress"}
    except (OSError, ValueError, UnicodeDecodeError):
        return base | {"error": "unreadable counts-only gate progress"}
    return base | {
        "present": True,
        "progress": _served(document),
        "notice": (
            "Counts from the retained OQ-6 coverage summaries. Recall and precision are measured on "
            "the keyed subset only; unkeyed tasks are not certified negatives, so none of "
            "these is a population false-alarm rate. Eligible findings also count findings on "
            "answered unkeyed tasks, which have no key to match, so precision is not comparable "
            "with the keyed-only denominators at the v4 checkpoint. Quota-pending tasks are "
            "unsent, not failures and not zeros."
        ),
    }


# --- building the file from the retained summaries ------------------------------------------


def _read(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} must be a regular file")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_SOURCE_BYTES} bytes")
    raw = path.read_bytes()
    value = strict_loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _gate(stats: Mapping[str, Any]) -> Gate | None:
    matched, eligible = stats["matched_keys"], stats["eligible_findings"]
    if matched is None and eligible is None:
        return None  # nothing read yet: unavailable, not zero
    return Gate(matched_keys=_int(matched, "matched_keys"), eligible_findings=_int(eligible, "eligible_findings"))


def from_summaries(
    coverage: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    *,
    coverage_sha256: str,
    checkpoint_sha256: str,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build the document from the two summaries, copying named integer fields only."""
    if coverage.get("schema") != COVERAGE_SCHEMA:
        raise ValueError(f"expected a {COVERAGE_SCHEMA} summary")
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise ValueError(f"expected a {CHECKPOINT_SCHEMA} summary")
    if (
        coverage.get("true_population_false_positive_rate") is not None
        or coverage.get("unkeyed_tasks_are_not_certified_negatives") is not True
    ):
        raise ValueError("the coverage summary must treat unkeyed tasks as not certified negatives")
    cohort = checkpoint["cohort"]
    dev_tasks, planned = _int(cohort["DEV_tasks"], "DEV_tasks"), _int(cohort["DEV_evaluable_annotations"], "DEV_evaluable_annotations")
    baseline = {
        (group["auditor_route"], group["arm"]): _int(group["auditor_answered"], "auditor_answered")
        for group in checkpoint["groups"]
        if group["stage"] == "dev"
    }
    rows = []
    for group in coverage["unique_union_groups"]:
        if group["stage"] != "dev" or group["phase"] != "unique_union" or group["active"] is not True:
            continue
        quality = group["quality"]
        if (
            _int(group["represented_tasks"], "represented_tasks") != dev_tasks
            or _int(quality["planned_tasks"], "planned_tasks") != dev_tasks
            or _int(quality["planned_keys"], "planned_keys") != planned
        ):
            raise ValueError("the coverage summary is for a different DEV cohort than the checkpoint")
        gates = quality["gates"]
        route, arm = group["auditor_route"], group["arm"]
        rows.append(
            {
                "route": route,
                "arm": arm,
                "answered": _int(group["auditor_answered"], "auditor_answered"),
                "consumed": _int(group["auditor_dispatched"], "auditor_dispatched"),
                "quota_pending": _int(group["auditor_unsent_backlog"], "auditor_unsent_backlog"),
                "failed": _int(group["terminal_consumed_failures"], "terminal_consumed_failures"),
                "scorer_quota_pending": _int(group["scorer_only_unsent_backlog"], "scorer_only_unsent_backlog"),
                "fully_scored": _int(group["fully_union_scored"], "fully_union_scored"),
                "baseline_answered": baseline.get((route, arm), 0),
                "keys_read": _int(quality["evaluable_keys"], "evaluable_keys"),
                "new_0_5": None if (new := _gate(gates["new_verified_0_5"])) is None else new.model_dump(),
                "old_0_8": None if (old := _gate(gates["old_0_8"])) is None else old.model_dump(),
            }
        )
    document = {
        "schema": SCHEMA,
        "exported_at": exported_at or datetime.now(UTC).isoformat(),
        "source": {"coverage_sha256": coverage_sha256, "checkpoint_sha256": checkpoint_sha256},
        "cohort": {
            "dev_tasks": dev_tasks,
            "keyed_tasks": _int(cohort["DEV_keyed_tasks"], "DEV_keyed_tasks"),
            "unkeyed_tasks": _int(cohort["DEV_unkeyed_tasks_not_certified_negatives"], "DEV_unkeyed_tasks"),
            "planned_keys": planned,
        },
        "rows": rows,
    }
    Progress.model_validate(document)
    return document


def export_progress(coverage_path: Path, checkpoint_path: Path) -> dict[str, Any]:
    coverage, coverage_sha = _read(coverage_path)
    checkpoint, checkpoint_sha = _read(checkpoint_path)
    return from_summaries(coverage, checkpoint, coverage_sha256=coverage_sha, checkpoint_sha256=checkpoint_sha)


def write_progress(
    directory: Path, document: Mapping[str, Any], *, replace: bool = False
) -> dict[str, Any]:
    """Validate and atomically write the file; an existing different file needs ``replace``."""
    Progress.model_validate(document)
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"{FILE} must be a regular file")
        if target.read_bytes() != body and not replace:
            raise FileExistsError(f"{target} already differs; pass --replace to overwrite it")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".gate-progress-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "rows": len(document["rows"]),
    }
