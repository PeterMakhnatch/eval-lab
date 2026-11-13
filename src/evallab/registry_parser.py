"""Task registry and development checkpoint command registration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _registry_promote_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.registry import RegistryError, promote_task

    registry_dir = Path(args.registry_dir) if args.registry_dir else None
    jobs_roots = [Path(args.jobs_dir)] if args.jobs_dir else None
    allowed_uses = (
        [u.strip() for u in args.allowed_uses.split(",")]  # type: ignore[arg-type]
        if args.allowed_uses
        else None
    )

    state = (
        "registered"
        if getattr(args, "register", False) or args.state == "registered"
        else "candidate"
    )
    if state == "registered" and not args.actor:
        print("error: registering a task record requires --actor", file=sys.stderr)
        return 1

    try:
        record = promote_task(
            task_path=args.task_path,
            repo_root=root,
            registry_dir=registry_dir,
            task_id=args.task_id,
            task_family=getattr(args, "task_family", None),
            version=args.version,
            source_uri=args.source_uri,
            source_ref=args.source_ref,
            license_str=args.license,
            provenance_zone=args.provenance_zone,
            is_synthetic=args.synthetic,
            timeout_seconds=args.timeout_seconds,
            max_memory_mb=args.max_memory_mb,
            max_cpus=args.max_cpus,
            allowed_uses=allowed_uses,
            human_minutes=args.human_minutes,
            state=state,
            actor=args.actor,
            jobs_roots=jobs_roots,
            certification_path=getattr(args, "certification_packet", None),
        )
    except (RegistryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
    else:
        print(f"promoted: {record.task_id}@{record.version} (state: {record.state})")
        print(f"  path:     {record.task_path}")
        print(f"  package:  {record.digests.package}")
        evidence = record.control_evidence
        if evidence is not None:
            print(f"  oracle:   {evidence.oracle.job_name} ({evidence.oracle.reward})")
            print(f"  nop:      {evidence.nop.job_name} ({evidence.nop.reward})")
        if record.approved_by:
            print(f"  approved: {record.approved_by} at {record.approved_at}")
    return 0


def _registry_register_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.registry import RegistryError, register_task

    if not args.actor or not args.actor.strip():
        print("error: registering a task requires --actor", file=sys.stderr)
        return 1

    registry_dir = Path(args.registry_dir) if args.registry_dir else None

    try:
        record = register_task(
            task_id=args.task_id,
            actor=args.actor,
            repo_root=root,
            registry_dir=registry_dir,
            certification_path=getattr(args, "certification_packet", None),
        )
    except (RegistryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
    else:
        print(f"registered: {record.task_id}@{record.version} (state: {record.state})")
        print(f"  approved by: {record.approved_by}")
        print(f"  approved at: {record.approved_at}")
    return 0


def _registry_audit_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.registry import audit_registry

    report = audit_registry(root)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.passed else 1

    print(f"Task Registry Audit (Total records: {report.total_records})")
    print(f"  Registered: {report.registered_count}")
    print(f"  Candidate:  {report.candidate_count}")
    print(f"  Retired:    {report.retired_count}")
    print()

    if not report.findings:
        print("PASS: zero audit findings. Registry and queue claims are valid.")
        return 0

    error_count = sum(1 for f in report.findings if f.severity == "error")
    warning_count = sum(1 for f in report.findings if f.severity == "warning")
    info_count = sum(1 for f in report.findings if f.severity == "info")

    print(f"Findings: {error_count} errors, {warning_count} warnings, {info_count} info")
    print("-" * 80)
    for finding in report.findings:
        icon = (
            "FAIL"
            if finding.severity == "error"
            else ("WARN" if finding.severity == "warning" else "INFO")
        )
        print(f"[{icon}] {finding.category} -> {finding.target}")
        print(f"       {finding.message}")
    return 0 if report.passed else 1


def _registry_devloop_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    from evallab.devloop import run_devloop

    return run_devloop(
        root, since=args.since, run=args.run, as_json=args.json, checkpoint=args.checkpoint
    )


def register_parser(commands: argparse._SubParsersAction) -> None:
    registry = commands.add_parser(
        "registry",
        help="Inspect and audit explicit registered tasks",
    )
    registry_commands = registry.add_subparsers(
        dest="registry_command",
        required=True,
    )

    registry_promote = registry_commands.add_parser(
        "promote",
        help="Promote a task package into the explicit task registry",
    )
    registry_promote.add_argument(
        "task_path",
        help="Path to task package directory",
    )
    registry_promote.add_argument(
        "--task-id",
        help="Explicit task identifier (defaults to task.toml name or directory name)",
    )
    registry_promote.add_argument(
        "--task-family",
        help="Immutable benchmark family (or declare task.family in task.toml)",
    )
    registry_promote.add_argument(
        "--version",
        help="Task version string (defaults to task.toml version or 1.0.0)",
    )
    registry_promote.add_argument(
        "--source-uri",
        help="Source URI for task provenance",
    )
    registry_promote.add_argument(
        "--source-ref",
        help="Source revision/commit reference",
    )
    registry_promote.add_argument(
        "--license",
        help="Declared license",
    )
    registry_promote.add_argument(
        "--provenance-zone",
        choices=["01-external", "02-local-evidence", "03-synthetic", "04-curated"],
        help="Provenance zone",
    )
    registry_promote.add_argument(
        "--synthetic",
        action="store_true",
        help="Mark task as synthetic",
    )
    registry_promote.add_argument(
        "--timeout-seconds",
        type=int,
        help="Execution timeout limit in seconds",
    )
    registry_promote.add_argument(
        "--max-memory-mb",
        type=int,
        help="Memory limit in megabytes",
    )
    registry_promote.add_argument(
        "--max-cpus",
        type=float,
        help="CPU limit",
    )
    registry_promote.add_argument(
        "--allowed-uses",
        help="Comma-separated allowed uses (default: measurement,training)",
    )
    registry_promote.add_argument(
        "--human-minutes",
        type=int,
        help="Expert human completion time estimate in minutes",
    )
    registry_promote.add_argument(
        "--state",
        choices=["candidate", "registered"],
        default="candidate",
        help="Admission state (default: candidate)",
    )
    registry_promote.add_argument(
        "--actor",
        help="Approving human actor (required when state is registered)",
    )
    registry_promote.add_argument(
        "--register",
        action="store_true",
        help="Register task immediately (requires --actor)",
    )
    registry_promote.add_argument(
        "--jobs-dir",
        help="Directory containing completed Harbor control runs",
    )
    registry_promote.add_argument(
        "--registry-dir",
        help="Override destination registry directory",
    )
    registry_promote.add_argument(
        "--certification-packet",
        help="Durable native package-check certification.json to bind (not a Gold certificate)",
    )
    registry_promote.add_argument(
        "--json",
        action="store_true",
        help="Emit promoted record as JSON",
    )
    registry_promote.set_defaults(func=_registry_promote_command)

    registry_register = registry_commands.add_parser(
        "register",
        help="Register a candidate task in the explicit task registry with human approval",
    )
    registry_register.add_argument(
        "task_id",
        help="Task ID of candidate record in registry",
    )
    registry_register.add_argument(
        "--actor",
        required=True,
        help="Approving human actor name",
    )
    registry_register.add_argument(
        "--registry-dir",
        help="Override registry directory",
    )
    registry_register.add_argument(
        "--certification-packet",
        help="Durable native package-check certification.json required for new registration",
    )
    registry_register.add_argument(
        "--json",
        action="store_true",
        help="Emit registered record as JSON",
    )
    registry_register.set_defaults(func=_registry_register_command)


    registry_audit = registry_commands.add_parser(
        "audit",
        help="Audit task registry records and queue claims",
    )
    registry_audit.add_argument(
        "--json",
        action="store_true",
        help="Emit audit report as JSON",
    )
    registry_audit.set_defaults(func=_registry_audit_command)


    registry_devloop = registry_commands.add_parser(
        "devloop",
        help="Map working tree changes to affected test modules and run targeted pytest",
    )
    registry_devloop.add_argument(
        "--since",
        default="working",
        help="Revision or baseline to diff against (default: working)",
    )
    registry_devloop.add_argument(
        "--run",
        action="store_true",
        help="Execute the computed pytest command",
    )
    registry_devloop.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON",
    )
    registry_devloop.add_argument(
        "--checkpoint",
        action="store_true",
        help="Run repository contracts and direct consumers; bounded local check, not CI green",
    )
    registry_devloop.set_defaults(func=_registry_devloop_command)
