"""Read-only task dossiers assembled from the lab's existing evidence (HAR-186)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.dataset_audit_contracts import AuditRecord
from evallab.dataset_audit_plugins import normalize_task_id, task_evidence, task_page_name
from evallab.storage.task_audit import read_audit_records
from evallab.task_dossier_trials import PAGE_BASE, task_trials

DEFAULT_STATIC_AUDIT = Path("/tmp/static-task-audit.csv")

_FACET_KEYS = (
    "health", "verdict", "tags", "leak", "repair", "static_flags",
    "failing_tests", "exploit_probes", "sources", "errors",
)


def _null_facets() -> dict[str, Any]:
    return {
        "health": None, "verdict": None, "tags": None, "leak": None,
        "repair": None, "static_flags": None, "failing_tests": None,
        "exploit_probes": None, "sources": {}, "errors": [],
    }


def _facets_from_mapping(facets: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only the stored HAR-186 facet keys; absence stays None, not keep."""
    merged = _null_facets()
    if isinstance(facets, Mapping):
        for key in _FACET_KEYS:
            if key in facets:
                merged[key] = facets[key]
    return merged


def _audit_block(record: AuditRecord, *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "dataset_id": record.task.dataset_id,
        "task_name": record.task.task_name,
        "task_id": record.task.task_id,
        "aliases": list(record.task.aliases),
        "package_digest": record.task.package_digest,
        "harbor_digest": record.task.harbor_digest,
        "source_uri": record.task.source_uri,
        "revision": record.task.revision,
        "verdict": record.verdict,
        "verdict_reason": record.verdict_reason,
        "verdict_sources": [source.model_dump(mode="json") for source in record.verdict_sources],
        "tags": list(record.tags),
        "stages": {
            stage: observation.model_dump(mode="json")
            for stage, observation in record.stages.items()
        },
        "facets_present": sorted(
            key for key in _FACET_KEYS
            if isinstance(record.facets, dict) and record.facets.get(key) is not None
        ),
        "record": record.model_dump(mode="json"),
    }


def _package_relation(trial: Mapping[str, Any], record: AuditRecord) -> str:
    """Whether an already collected trial executed the audited package."""
    comparable = False
    for field, digest in (
        ("task_digest", record.task.harbor_digest),
        ("source_package_digest", record.task.package_digest),
    ):
        observed = trial.get(field)
        if isinstance(observed, str) and isinstance(digest, str):
            if observed != digest:
                return "mismatch"
            comparable = True
    if comparable:
        return "match"
    if record.task.harbor_digest is None and record.task.package_digest is None:
        return "unknown_unbound_audit"
    return "unknown"


def _assemble_dossier(
    *,
    task_id: str,
    task_name: str | None,
    facets: dict[str, Any],
    audit: dict[str, Any],
    trial_facet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the complete JSON dossier; a missing history stays explicit."""
    if trial_facet is None:
        trials: list[dict[str, Any]] | None = None
        coverage = {
            "task_id": task_id, "status": "not_collected", "collected": False,
            "note": "trial history was not collected; not proof of zero runs",
            "read_only": True,
        }
    else:
        trials = [dict(trial) for trial in trial_facet.get("trials", [])]
        coverage = dict(trial_facet.get("coverage", {}))
    return {
        "schema": "evallab.task_dossier/v1",
        "task_id": task_id,
        "task_name": task_name,
        "page_url": PAGE_BASE + task_page_name(task_id),
        **facets,
        "trials": trials,
        "coverage": coverage,
        "audit": audit,
    }


def dossier_for_audit(
    record: AuditRecord, trial_facet: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Combine a collected audit record and history facet with no reads.

    ``trial_facet`` is a ``task_trials_for_tasks`` value for the canonical
    task id. ``None`` means history was not collected, not that no runs
    exist. A mismatched historical digest is reported per trial, never given
    the audited package's verdict.
    """
    trials = None if trial_facet is None else [dict(trial) for trial in trial_facet.get("trials", [])]
    if trials is not None:
        for trial in trials:
            trial["audited_package_relation"] = _package_relation(trial, record)
    facet = None if trial_facet is None else {
        "trials": trials,
        "coverage": dict(trial_facet.get("coverage", {})),
    }
    return _assemble_dossier(
        task_id=record.task.task_id,
        task_name=record.task.task_name,
        facets=_facets_from_mapping(record.facets),
        audit=_audit_block(record, status="recorded"),
        trial_facet=facet,
    )


def _identity_summary(record: AuditRecord) -> dict[str, Any]:
    return {
        "dataset_id": record.task.dataset_id,
        "task_id": record.task.task_id,
        "task_name": record.task.task_name,
        "source_uri": record.task.source_uri,
        "revision": record.task.revision,
        "package_digest": record.task.package_digest,
        "harbor_digest": record.task.harbor_digest,
        "verdict": record.verdict,
    }


def _select_records(
    task_id: str, records: Sequence[AuditRecord], *, dataset_id: str | None,
) -> list[AuditRecord]:
    """Records for one canonical task, exact alias or native-name match only."""
    matched = []
    for record in records:
        task = record.task
        if dataset_id is not None and task.dataset_id != dataset_id:
            continue
        if task_id in (task.task_id, task.task_name, *task.aliases):
            matched.append(record)
    return matched


def task_dossier(
    task_id: str,
    *,
    repo_root: Path | None = None,
    roots: Sequence[Path] | None = None,
    derived_root: Path | None = None,
    reader_store: Path | None = None,
    static_audit: Path | None = DEFAULT_STATIC_AUDIT,
    audit_path: Path | None = None,
    dataset_id: str | None = None,
) -> dict[str, Any]:
    """Return task evidence without publishing, backfilling, or calling a provider.

    Missing task evidence remains null; an empty trial list is scoped to the
    selected local roots, not a claim that the task has never run elsewhere.
    Candidate repairs and independent copy judgments retain their own status.
    The existing audit projection joins automatically; ``audit_path`` overrides
    its location. Multiple package versions stay ambiguous, never guessed.
    """
    from evallab.cli import repo_root as code_root
    from evallab.storage.paths import task_audit_path

    canonical_id = normalize_task_id(task_id)
    root = (repo_root if repo_root is not None else code_root()).resolve()
    facets = _facets_from_mapping(task_evidence(canonical_id, repo_root=root, static_audit=static_audit))
    history = task_trials(
        canonical_id, repo_root=root, roots=roots, derived_root=derived_root,
        reader_store=reader_store,
    )
    path = Path(audit_path) if audit_path is not None else task_audit_path(root, derived_root=derived_root)
    if audit_path is None and not path.is_file():
        audit: dict[str, Any] = {"status": "not_recorded", "records": [], "dataset_id": dataset_id}
        return _assemble_dossier(
            task_id=canonical_id, task_name=None, facets=facets, audit=audit,
            trial_facet=history,
        )
    if not path.exists():
        raise ValueError(f"audit path is missing: {path}")
    matched = _select_records(canonical_id, read_audit_records(path), dataset_id=dataset_id)
    if not matched:
        return _assemble_dossier(
            task_id=canonical_id, task_name=None, facets=facets,
            audit={"status": "not_recorded", "records": [], "dataset_id": dataset_id},
            trial_facet=history,
        )
    if len(matched) > 1:
        disagreements = [
            key for key in _FACET_KEYS
            if key != "errors"
            and any(
                isinstance(record.facets, dict) and record.facets.get(key) != facets.get(key)
                for record in matched if isinstance(record.facets, dict)
            )
        ]
        return _assemble_dossier(
            task_id=canonical_id, task_name=None, facets=facets,
            audit={
                "status": "ambiguous",
                "records": [_identity_summary(record) for record in matched],
                "dataset_id": dataset_id,
                "evidence_disagreement": disagreements,
                "note": (
                    "multiple audited package versions match; no record guessed. "
                    "Select a dataset or inspect the exported per-package dossiers."
                ),
            },
            trial_facet=history,
        )
    record = matched[0]
    stored = _facets_from_mapping(record.facets if isinstance(record.facets, dict) else None)
    disagreements = [
        key for key in _FACET_KEYS
        if key not in ("sources", "errors") and stored.get(key) != facets.get(key)
    ]
    dossier = dossier_for_audit(record, history)
    if isinstance(record.facets, dict) and not record.facets:
        dossier.update(facets)
    if disagreements:
        dossier["plugin_evidence"] = dict(facets)
        dossier["evidence_disagreement"] = disagreements
    return dossier


def _display(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def render_dossier(dossier: dict[str, Any]) -> str:
    health = dossier["health"] or {}
    verdict = dossier["verdict"] or {}
    leak = dossier["leak"] or {}
    repair = dossier["repair"] or {}
    found = leak.get("found")
    leak_label = "yes" if found is True else "no recorded finding" if found is False else "unknown"
    lines = [
        f"Task: {dossier['task_id']}",
        f"Page: {dossier['page_url']} (address only; availability not checked)",
        f"Health: {_display(health.get('tag'))}",
        f"Verdict: {_display(verdict.get('verdict'))}",
        f"Evidence tags: {_display(dossier['tags'])}",
        f"Leak: {leak_label}; channel={_display(leak.get('channel'))}",
        f"Repair: {_display(repair.get('status'))}; variant={_display(repair.get('variant_digest'))}",
    ]
    if repair.get("status") == "candidate":
        lines.append("  Candidate repair exists; it is not a validated repair.")
    static = dossier["static_flags"]
    lines.append(f"Static flags: {_display(static)}")
    failing = dossier["failing_tests"]
    lines.append("Failing-test evidence:")
    lines.extend("  " + line for line in (
        json.dumps(failing, indent=2, ensure_ascii=False, sort_keys=True).splitlines()
        if failing is not None else ["not recorded"]
    ))
    lines.append(f"Exploit probes: {_display(dossier['exploit_probes'])}")
    audit = dossier.get("audit") or {}
    if audit.get("status") == "recorded":
        lines.append(
            f"Audit: {audit.get('verdict')} ({audit.get('dataset_id')}); "
            "routing only, not certification or admission"
        )
    elif audit.get("status") == "ambiguous":
        lines.append(
            f"Audit: ambiguous across {len(audit.get('records', []))} package versions; no verdict selected"
        )
    trials = dossier["trials"]
    if trials is None:
        lines.extend(["", "Trials: not collected (not evidence of zero runs)"])
    else:
        lines.extend(["", f"Trials: {len(trials)} (all local attempts, including controls/probes)"])
        for trial in trials:
            lines.extend([
                f"- {_display(trial.get('campaign'))} / {trial['job']} / {trial['trial']}",
                f"  legit={_display(trial.get('legit'))} reward={_display(trial.get('reward'))} "
                f"gated={_display(trial.get('reward_gated'))} stop={_display(trial.get('stop_reason'))}",
                f"  Copy verdicts: {_display(trial.get('copy_verdicts'))}",
                f"  Laminar: {_display(trial.get('laminar_url'))}",
            ])
            if trial.get("audited_package_relation"):
                lines.append(f"  Audited package: {trial['audited_package_relation']}")
            if trial.get("projection_error"):
                lines.append(f"  Evidence gap: {trial['projection_error']}")
            if trial.get("binding_conflicts"):
                lines.append(f"  Reader identity conflicts: {_display(trial['binding_conflicts'])}")
    if dossier.get("errors"):
        lines.append(f"Evidence errors: {_display(dossier['errors'])}")
    return "\n".join(lines)


def _command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    from evallab.cli import _resolve

    try:
        dossier = task_dossier(
            args.task_id,
            repo_root=root,
            roots=[_resolve(root, path) for path in args.runs_dir] if args.runs_dir else None,
            derived_root=_resolve(root, args.derived_root) if args.derived_root else None,
            reader_store=_resolve(root, args.reader_store) if args.reader_store else None,
            static_audit=_resolve(root, args.static_audit),
            audit_path=_resolve(root, args.audit_path) if args.audit_path else None,
            dataset_id=args.dataset,
        )
        print(
            json.dumps(dossier, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if args.json else render_dossier(dossier)
        )
    except (OSError, ValueError) as exc:
        print(f"task: {exc}", file=sys.stderr)
        return 1
    return 0


def build_task_dossier_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "task", help="Read-only task dossier from existing health, trials, and reader evidence",
        description="Read-only, $0 task dossier; never runs a model or writes an evidence store.",
        epilog=(
            "Agent usage: uv run evallab task format-code-task-000792 --json. "
            "Python: from evallab.task_dossier import task_dossier; task_dossier(task_id). "
            "Missing evidence is unknown, not clean; candidate repairs are not validated."
        ),
    )
    parser.add_argument("task_id", help="Canonical task id, e.g. format-code-task-000792 or harbor/hello-world")
    parser.add_argument("--json", action="store_true", help="Emit the complete source-backed dossier as JSON")
    parser.add_argument("--runs-dir", type=Path, action="append", default=[], help="Restrict local trial roots (repeatable)")
    parser.add_argument("--derived-root", type=Path, help="Read existing projections from this root; never backfill")
    parser.add_argument("--reader-store", type=Path, help="Read existing Laminar/Harbor reader verdicts from this store")
    parser.add_argument("--static-audit", type=Path, default=DEFAULT_STATIC_AUDIT, help="Optional static-audit CSV; absent is unknown")
    parser.add_argument("--audit-path", type=Path, default=None, help="Override the audit.parquet found in the existing derived root")
    parser.add_argument("--dataset", type=str, default=None, help="Only select audit records from this dataset id")
    parser.set_defaults(func=_command)
