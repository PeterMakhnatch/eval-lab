"""Read-only task dossiers assembled from the lab's existing evidence (HAR-186)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_STATIC_AUDIT = Path("/tmp/static-task-audit.csv")
_LEAF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def _task_id(selector: str) -> str:
    for prefix in ("mimo-v2.6-rl/", "mimo-v2.6-rl__"):
        if selector.startswith(prefix):
            selector = selector.removeprefix(prefix)
            break
    if not _LEAF_ID.fullmatch(selector):
        raise ValueError("task_id must be a canonical task id, not a path or pattern")
    return selector


def task_dossier(
    task_id: str,
    *,
    repo_root: Path | None = None,
    roots: Sequence[Path] | None = None,
    derived_root: Path | None = None,
    reader_store: Path | None = None,
    static_audit: Path | None = DEFAULT_STATIC_AUDIT,
) -> dict[str, Any]:
    """Return task evidence without publishing, backfilling, or calling a provider.

    Missing task evidence remains null; an empty trial list is scoped to the
    selected local roots, not a claim that the task has never run elsewhere.
    Candidate repairs and independent copy judgments retain their own status.
    """
    from evallab.cli import repo_root as code_root
    from evallab.task_dossier_evidence import task_evidence
    from evallab.task_dossier_trials import page_url_for, task_trials

    canonical_id = _task_id(task_id)
    root = (repo_root if repo_root is not None else code_root()).resolve()
    facets = task_evidence(canonical_id, repo_root=root, static_audit=static_audit)
    history = task_trials(
        canonical_id, repo_root=root, roots=roots, derived_root=derived_root,
        reader_store=reader_store,
    )
    return {
        "schema": "evallab.task_dossier/v1",
        "task_id": canonical_id,
        "page_url": page_url_for(canonical_id),
        **facets,
        "trials": history["trials"],
        "coverage": history["coverage"],
    }


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
    lines.extend(["", f"Trials: {len(dossier['trials'])} (all local attempts, including controls/probes)"])
    for trial in dossier["trials"]:
        lines.extend([
            f"- {_display(trial.get('campaign'))} / {trial['job']} / {trial['trial']}",
            f"  legit={_display(trial.get('legit'))} reward={_display(trial.get('reward'))} "
            f"gated={_display(trial.get('reward_gated'))} stop={_display(trial.get('stop_reason'))}",
            f"  Copy verdicts: {_display(trial.get('copy_verdicts'))}",
            f"  Laminar: {_display(trial.get('laminar_url'))}",
        ])
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
    parser.add_argument("task_id", help="Canonical task id, e.g. format-code-task-000792")
    parser.add_argument("--json", action="store_true", help="Emit the complete source-backed dossier as JSON")
    parser.add_argument("--runs-dir", type=Path, action="append", default=[], help="Restrict local trial roots (repeatable)")
    parser.add_argument("--derived-root", type=Path, help="Read existing projections from this root; never backfill")
    parser.add_argument("--reader-store", type=Path, help="Read existing Laminar/Harbor reader verdicts from this store")
    parser.add_argument("--static-audit", type=Path, default=DEFAULT_STATIC_AUDIT, help="Optional static-audit CSV; absent is unknown")
    parser.set_defaults(func=_command)
