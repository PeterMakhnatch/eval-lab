"""Live-watch alerts to immutable, bounded, read-only investigations (HAR-151).

This consumes the existing watcher's public JSON contract. It is neither a
second detector nor an execution queue: only derived analysis files are written.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.interpretation.monitor_contracts import (
    InvestigationCase,
    InvestigationLimits,
    InvestigationReport,
    MonitorCorpus,
    TrialSnapshot,
    content_digest,
)
from evallab.storage.fs import durable_mkdir, durable_replace, fsync_directory

MONITOR_SCHEMA = "evallab.monitor_status/v1"
MAX_JSON_BYTES = 160_000_000
_CASE_ID = re.compile(r"^[a-f0-9]{24}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _read_json(path: Path, *, limit: int = MAX_JSON_BYTES) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular JSON file: {path}")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"JSON input exceeds {limit} bytes: {path}")
    return json.loads(data)


def _safe_path(root: Path, path: Path) -> Path:
    """Confine all generated names and reject links inside the output tree."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("analysis output escapes its root") from exc
    current = root
    if current.is_symlink():
        raise ValueError("analysis root cannot be a symlink")
    for part in relative.parts:
        if part in (".", ".."):
            raise ValueError("invalid analysis path")
        current = current / part
        if current.is_symlink():
            raise ValueError(f"analysis output contains a symlink: {current}")
    return path


def _analysis_root(out_dir: Path, roots: Sequence[Path] = ()) -> Path:
    out = out_dir.expanduser().resolve()
    for root in roots:
        source = root.expanduser().resolve()
        if out == source or out.is_relative_to(source) or source.is_relative_to(out):
            raise ValueError("analysis output must not overlap any source root")
    durable_mkdir(out)
    return out


def _write_json(root: Path, path: Path, value: Any, *, immutable: bool = False) -> None:
    path = _safe_path(root, path)
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    durable_mkdir(path.parent)
    if immutable and path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"immutable analysis identity conflicts with existing bytes: {path}")
        return
    fd, name = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise ValueError(f"concurrent analysis identity conflict: {path}") from None
            fsync_directory(path.parent)
        else:
            durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _preparation_lock(root: Path) -> Iterator[None]:
    path = _safe_path(root, root / ".prepare.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _related_trials(corpus: MonitorCorpus, primary: TrialSnapshot, count: int) -> tuple[str, ...]:
    candidates = [trial for trial in corpus.trials if trial.trial_key != primary.trial_key]
    # Same-task contrasts first; an unflagged control before another positive.
    # Stable identity ordering prevents changing the sample on every poll.
    candidates.sort(key=lambda trial: (
        not (primary.task is not None and trial.task == primary.task),
        bool(trial.alerts),
        content_digest(trial.trial_key),
    ))
    return tuple(trial.trial_key for trial in candidates[:count])


def select_cases(
    corpus: MonitorCorpus,
    *,
    unflagged: int = 1,
    related: int = 2,
    max_cases: int = 200,
) -> list[tuple[InvestigationCase, MonitorCorpus]]:
    """Choose flagged trials and a reproducible explicitly unflagged sample."""
    if unflagged < 0 or related < 0 or max_cases < 1:
        raise ValueError("invalid investigation selection limits")
    flagged = sorted(
        (trial for trial in corpus.trials if trial.alerts),
        key=lambda trial: (
            not any(alert.severity == "high" for alert in trial.alerts),
            trial.trial_key,
        ),
    )
    controls = sorted(
        (trial for trial in corpus.trials if not trial.alerts),
        key=lambda trial: content_digest(trial.trial_key),
    )[:unflagged]
    selections = [(trial, "alert") for trial in flagged]
    selections.extend((trial, "unflagged_control") for trial in controls)
    cases: list[tuple[InvestigationCase, MonitorCorpus]] = []
    for primary, selection in selections[:max_cases]:
        related_keys = _related_trials(corpus, primary, related)
        selected_keys = {primary.trial_key, *related_keys}
        subset = MonitorCorpus(
            trials=tuple(sorted(
                (trial for trial in corpus.trials if trial.trial_key in selected_keys),
                key=lambda trial: trial.trial_key,
            )),
            limitations=corpus.limitations,
        )
        case = InvestigationCase(
            case_id=content_digest({"primary": primary.trial_key, "selection": selection})[:24],
            snapshot_id=subset.digest,
            primary_trial=primary.trial_key,
            related_trials=related_keys,
            alerts=primary.alerts,
            selection=selection,
        )
        cases.append((case, subset))
    return cases


def _step_count(trial: TrialSnapshot) -> int:
    return max((record.ordinal or 0 for record in trial.records), default=0)


def _alert_signature(trial: TrialSnapshot) -> str:
    return content_digest([
        (alert.rule, alert.step_ref, alert.quote, alert.target) for alert in trial.alerts
    ])


def _needs_revision(previous: TrialSnapshot, current: TrialSnapshot, min_new_steps: int) -> bool:
    if content_digest(previous) == content_digest(current):
        return False
    if (
        previous.state != current.state
        or previous.reward != current.reward
        or previous.complete != current.complete
        or _alert_signature(previous) != _alert_signature(current)
    ):
        return True
    old_count, new_count = _step_count(previous), _step_count(current)
    # A terminal artifact correction or same-length prefix correction matters
    # immediately; ordinary live growth is coalesced to avoid one paid call/step.
    return current.state != "running" or new_count <= old_count or new_count - old_count >= min_new_steps


def _load_case(root: Path, case_id: str, snapshot_id: str) -> tuple[InvestigationCase, MonitorCorpus, Path]:
    if not _CASE_ID.fullmatch(case_id) or not _DIGEST.fullmatch(snapshot_id):
        raise ValueError("invalid stored case or snapshot identity")
    revision = _safe_path(root, root / "cases" / case_id / "revisions" / snapshot_id)
    case = InvestigationCase.model_validate(_read_json(_safe_path(root, revision / "case.json")))
    corpus = MonitorCorpus.model_validate(_read_json(
        _safe_path(root, root / "snapshots" / f"{snapshot_id}.json")
    ))
    if case.case_id != case_id or case.snapshot_id != snapshot_id or corpus.digest != snapshot_id:
        raise ValueError("stored case evidence digest mismatch")
    known = {trial.trial_key for trial in corpus.trials}
    if case.primary_trial not in known or not set(case.related_trials).issubset(known):
        raise ValueError("stored case refers to absent trials")
    return case, corpus, revision


def prepare_watch(
    status_path: Path,
    roots: Sequence[Path],
    out_dir: Path,
    *,
    unflagged: int = 1,
    related: int = 2,
    max_cases: int = 200,
    min_new_steps: int = 20,
) -> dict[str, Any]:
    """Freeze new significant prefixes; unchanged bytes produce no new request."""
    from evallab.interpretation.monitor_evidence import snapshot_watch

    if not roots:
        raise ValueError("at least one explicit source root is required")
    if min_new_steps < 1:
        raise ValueError("min_new_steps must be positive")
    root = _analysis_root(out_dir, roots)
    status = _read_json(status_path, limit=16_000_000)
    if not isinstance(status, dict):
        raise ValueError("watch status must be an object")
    corpus = snapshot_watch(status, roots)
    selected = select_cases(corpus, unflagged=unflagged, related=related, max_cases=max_cases)
    created, unchanged, deferred = 0, 0, 0
    current_ids: list[str] = []
    with _preparation_lock(root):
        status_file = _safe_path(root, root / "status.json")
        prior_ids: list[str] = []
        if status_file.exists():
            # Verify existing identities before preserving them. A vanished
            # source must not make a prior concern disappear from the report.
            prior_ids = [case.case_id for case, _, _ in latest_cases(root)]
        for case, subset in selected:
            current_ids.append(case.case_id)
            case_root = root / "cases" / case.case_id
            latest_path = _safe_path(root, case_root / "latest.json")
            primary = next(trial for trial in subset.trials if trial.trial_key == case.primary_trial)
            if latest_path.exists():
                latest = _read_json(latest_path)
                old_id = latest["snapshot_id"]
                if old_id == case.snapshot_id:
                    unchanged += 1
                    continue
                old_case, old_corpus, _ = _load_case(root, case.case_id, old_id)
                old_primary = next(
                    trial for trial in old_corpus.trials if trial.trial_key == old_case.primary_trial
                )
                if not _needs_revision(old_primary, primary, min_new_steps):
                    deferred += 1
                    continue
            _write_json(root, root / "snapshots" / f"{case.snapshot_id}.json",
                        subset.model_dump(mode="json"), immutable=True)
            revision = case_root / "revisions" / case.snapshot_id
            _write_json(root, revision / "case.json", case.model_dump(mode="json"), immutable=True)
            _write_json(root, latest_path, {"case_id": case.case_id, "snapshot_id": case.snapshot_id})
            created += 1
        summary = {
            "schema": MONITOR_SCHEMA,
            "source_watch": str(status_path.resolve()),
            "source_roots": [str(path.resolve()) for path in roots],
            "source_trials": len(corpus.trials),
            "selected_cases": len(selected),
            "new_revisions": created,
            "unchanged": unchanged,
            "coalesced_live_updates": deferred,
            "case_ids": current_ids + sorted(set(prior_ids) - set(current_ids)),
            "active_case_ids": current_ids,
            "inactive_case_ids": sorted(set(prior_ids) - set(current_ids)),
            "limitations": list(corpus.limitations),
            "policy": {
                "unflagged_sample": unflagged,
                "related_trials": related,
                "min_new_steps": min_new_steps,
                "max_cases": max_cases,
                "findings_are_hypotheses": True,
                "mutates_source_runs": False,
            },
        }
        _write_json(root, root / "status.json", summary)
    return summary


def latest_cases(
    root: Path, *, active_only: bool = False,
) -> list[tuple[InvestigationCase, MonitorCorpus, Path]]:
    status = _read_json(_safe_path(root, root / "status.json"))
    if status.get("schema") != MONITOR_SCHEMA:
        raise ValueError("unsupported monitor status schema")
    cases = []
    seen: set[str] = set()
    identities = status.get("active_case_ids" if active_only else "case_ids", [])
    for case_id in identities:
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id) or case_id in seen:
            raise ValueError("invalid or duplicate monitor case identity")
        seen.add(case_id)
        latest = _read_json(_safe_path(root, root / "cases" / case_id / "latest.json"))
        cases.append(_load_case(root, case_id, latest["snapshot_id"]))
    return cases


@contextlib.contextmanager
def _case_lock(root: Path, case_id: str) -> Iterator[None]:
    path = _safe_path(root, root / "cases" / case_id / ".investigate.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"case {case_id} is already being investigated") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ambiguous_prior_request(root: Path, case_id: str, current: Path) -> bool:
    revisions = _safe_path(root, root / "cases" / case_id / "revisions")
    for revision in revisions.iterdir():
        if not _DIGEST.fullmatch(revision.name):
            continue
        analyses = _safe_path(root, revision / "analyses")
        if not analyses.is_dir():
            continue
        for work_dir in analyses.iterdir():
            if not _DIGEST.fullmatch(work_dir.name) or work_dir == current:
                continue
            journal = _safe_path(root, work_dir / "journal.jsonl")
            report_path = _safe_path(root, work_dir / "report.json")
            if journal.exists() and not report_path.exists():
                return True
            if report_path.exists():
                report = InvestigationReport.model_validate(_read_json(report_path))
                if report.error == "ambiguous_prior_request":
                    return True
    return False


def run_prepared(
    out_dir: Path,
    *,
    transport: Any,
    budget: Any,
    profile: dict[str, Any],
    limits: InvestigationLimits | None = None,
    max_cases: int = 3,
) -> list[InvestigationReport]:
    """Run at most max_cases new investigations, retaining earlier outputs."""
    from evallab.interpretation import monitor_agent, monitor_contracts, monitor_evidence

    if max_cases < 1:
        raise ValueError("max_cases must be positive")
    root = _analysis_root(out_dir)
    effective = limits or InvestigationLimits()
    source_hashes = {
        module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (monitor_agent, monitor_contracts, monitor_evidence)
    }
    identity = {
        "profile": profile,
        "limits": effective.model_dump(mode="json"),
        "implementation": source_hashes,
    }
    profile_key = content_digest(identity)
    _write_json(root, root / "profiles" / f"{profile_key}.json", identity, immutable=True)
    reports: list[InvestigationReport] = []
    started = 0
    for case, corpus, revision in latest_cases(root, active_only=True):
        work_dir = _safe_path(root, revision / "analyses" / profile_key)
        existing = _safe_path(root, work_dir / "report.json")
        if existing.exists():
            report = InvestigationReport.model_validate(_read_json(existing))
            if report.case_id != case.case_id or report.snapshot_id != case.snapshot_id:
                raise ValueError("analysis report identity mismatch")
            continue
        if started >= max_cases:
            break
        with _case_lock(root, case.case_id):
            if _ambiguous_prior_request(root, case.case_id, work_dir):
                raise ValueError(
                    f"case {case.case_id} has an ambiguous prior request; "
                    "inspect the retained provider journal before authorizing further analysis"
                )
            report = monitor_agent.investigate(case, corpus, transport=transport, budget=budget,
                                               work_dir=work_dir, limits=effective)
            if report.case_id != case.case_id or report.snapshot_id != case.snapshot_id:
                raise ValueError("investigator returned a different case identity")
            # The engine publishes before returning; no inferred success.
            if not existing.is_file():
                raise ValueError("investigator did not persist its report")
        reports.append(report)
        started += 1
        if report.status == "budget_exhausted":
            break
    return reports


def _text(value: Any) -> str:
    return html.escape(_CONTROL_CHARS.sub("", str(value)), quote=False).replace("`", "&#96;")


def render_monitor_report(root: Path) -> str:
    """Render source-cited hypotheses, never rewrite the underlying verdict."""
    root = root.expanduser().resolve()
    lines = [
        "# Monitor investigations", "",
        "Read-only analysis hypotheses, not adjudicated hacks or revised rewards.", "",
    ]
    status = _read_json(_safe_path(root, root / "status.json"))
    inactive = set(status.get("inactive_case_ids", []))
    for case, corpus, revision in latest_cases(root):
        primary = next(trial for trial in corpus.trials if trial.trial_key == case.primary_trial)
        records = {record.record_id: record for trial in corpus.trials for record in trial.records}
        if case.case_id in inactive:
            lines.extend([
                "**Source not present in the current selection.** Retained evidence only; "
                "no new provider call is dispatched for this case.", "",
            ])
        lines.extend([
            f"## {_text(primary.job)} / {_text(primary.trial)}", "",
            f"Case `{case.case_id}` · snapshot `{case.snapshot_id}`", "",
            f"Selection: **{case.selection}**. Source state: **{primary.state}**. "
            f"Recorded reward: **{primary.reward if primary.reward is not None else 'unavailable'}**.",
            "",
            "Triggers: " + (", ".join(_text(alert.rule) for alert in case.alerts) or "unflagged sample"),
            "",
        ])
        analyses = _safe_path(root, revision / "analyses")
        found = False
        if analyses.is_dir():
            for directory in sorted(analyses.iterdir()):
                if not _DIGEST.fullmatch(directory.name):
                    continue
                report_path = _safe_path(root, directory / "report.json")
                if not report_path.is_file():
                    continue
                report = InvestigationReport.model_validate(_read_json(report_path))
                if report.case_id != case.case_id or report.snapshot_id != case.snapshot_id:
                    raise ValueError("report does not match the selected source snapshot")
                found = True
                lines.extend([
                    f"### {_text(report.model)} — {report.status}", "",
                    f"Calls: {report.calls}; conservative reservation: ${report.reserved_usd:.6f}; "
                    "usage-priced estimate: " + (
                        f"${report.estimated_usage_usd:.6f}" if report.estimated_usage_usd is not None
                        else "unavailable"
                    ) + " (not an invoice).", "",
                ])
                if report.finding is not None:
                    finding = report.finding
                    lines.extend([f"**{finding.disposition} / {finding.category}**", "",
                                  _text(finding.summary), ""])
                    for title, citations in (("Evidence", finding.evidence),
                                             ("Counterevidence", finding.counterevidence)):
                        if citations:
                            lines.extend([f"#### {title}", ""])
                        for citation in citations:
                            record = records.get(citation.record_id)
                            if record is None or citation.quote not in record.text:
                                raise ValueError("stored finding citation no longer grounds to snapshot")
                            lines.extend([
                                f"- `{_text(record.record_id)}` — `{_text(record.document)}` "
                                f"{_text(record.step_ref or '')}; SHA-256 `{record.source_sha256}`",
                                f"  > {_text(citation.quote).replace(chr(10), chr(10) + '  > ')}",
                            ])
                    if finding.alternatives:
                        lines.extend(["", "Alternatives:", *(
                            f"- {_text(value)}" for value in finding.alternatives
                        )])
                    if finding.missing_evidence:
                        lines.extend(["", "Missing evidence:", *(
                            f"- {_text(value)}" for value in finding.missing_evidence
                        )])
                    if finding.proposed_actions:
                        lines.extend(["", "Proposed actions — approval required:"])
                        for action in finding.proposed_actions:
                            lines.append(f"- **{action.kind}**: {_text(action.description)}")
                            lines.append(f"  Validation: {_text(action.validation)}")
                if report.error:
                    lines.extend(["", f"Unavailable: {_text(report.error)}"])
                lines.extend(f"- Limit: {_text(value)}" for value in report.limitations)
                lines.append("")
        if not found:
            lines.extend(["Prepared; no investigator has been invoked for this snapshot.", ""])
        lines.extend(f"- Source limitation: {_text(value)}" for value in primary.limitations)
        lines.append("")
    return "\n".join(lines)


def _resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    try:
        if args.investigate_command == "report":
            print(render_monitor_report(_resolve(root, args.out)))
            return 0
        if args.investigate_command == "score":
            from evallab.interpretation.monitor_calibration import score_monitor_reports

            labels = _read_json(_resolve(root, args.labels), limit=8_000_000)
            reports = [InvestigationReport.model_validate(_read_json(_resolve(root, path)))
                       for path in args.report]
            print(json.dumps(score_monitor_reports(labels, reports, category=args.category), indent=2))
            return 0
        roots = [_resolve(root, path) for path in args.runs_dir]
        status_path, out_dir = _resolve(root, args.watch_state), _resolve(root, args.out)
        if args.cycles < 1 or (args.cycles > 1 and args.interval <= 0):
            raise ValueError("cycles must be positive; repeated cycles require a positive interval")
        transport = budget = None
        limits = InvestigationLimits()
        profile: dict[str, Any] = {}
        if args.investigate_command == "run":
            from evallab.interpretation.monitor_agent import InvestigationBudget, OpenAIInvestigator

            if not args.allow_model:
                raise ValueError("run requires --allow-model; prepare never calls a provider")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.api_key_env):
                raise ValueError("api-key-env must name an environment variable, never a key value")
            key = os.environ.get(args.api_key_env)
            if not key:
                raise ValueError(f"missing {args.api_key_env}; load it through your credential store")
            limits = InvestigationLimits(max_calls=args.calls_per_case,
                                         max_output_tokens=args.max_output_tokens,
                                         timeout_seconds=args.timeout)
            profile = {"model": args.model, "endpoint": args.endpoint,
                       "input_usd_per_million": args.input_price,
                       "output_usd_per_million": args.output_price,
                       "disable_thinking": args.disable_thinking}
            # Validate hosted transport before creating a persistent budget.
            transport = OpenAIInvestigator(endpoint=args.endpoint, model=args.model,
                                           api_key=key, timeout_seconds=limits.timeout_seconds,
                                           disable_thinking=args.disable_thinking)
            output_root = _analysis_root(out_dir, roots)
            budget = InvestigationBudget(_safe_path(output_root, output_root / "spend.jsonl"),
                                         budget_usd=args.budget_usd, max_calls=args.max_calls,
                                         input_usd_per_million=args.input_price,
                                         output_usd_per_million=args.output_price)
        for cycle in range(args.cycles):
            summary = prepare_watch(status_path, roots, out_dir, unflagged=args.unflagged,
                                    related=args.related, min_new_steps=args.min_new_steps)
            if transport is not None and budget is not None:
                reports = run_prepared(out_dir, transport=transport, budget=budget,
                                       profile=profile, limits=limits, max_cases=args.max_cases)
                summary["investigations"] = [report.model_dump(mode="json") for report in reports]
            print(json.dumps(summary, indent=2), flush=True)
            if cycle + 1 < args.cycles:
                time.sleep(args.interval)
        return 0
    except (OSError, ValueError, ValidationError) as exc:
        print(f"investigate: {exc}")
        return 2


def build_investigate_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("investigate", help="Investigate live-watch alerts with source-bound evidence")
    sub = parser.add_subparsers(dest="investigate_command", required=True)
    for name in ("prepare", "run"):
        command = sub.add_parser(name, help="Freeze evidence without model calls" if name == "prepare"
                                else "Run bounded hosted investigations (explicit opt-in)")
        command.add_argument("--watch-state", type=Path, required=True, help="Existing watch status.json")
        command.add_argument("--runs-dir", type=Path, action="append", required=True,
                             help="Explicit trial/job/runs source root (repeatable)")
        command.add_argument("--out", type=Path, default=Path("derived/analyses/monitor"))
        command.add_argument("--unflagged", type=int, default=1, help="Unflagged control cases to sample")
        command.add_argument("--related", type=int, default=2, help="Related trials available per case")
        command.add_argument("--min-new-steps", type=int, default=20,
                             help="Coalesce running prefixes; new alerts and terminal evidence bypass this")
        command.add_argument("--cycles", type=int, default=1, help="Finite number of watch-state refreshes")
        command.add_argument("--interval", type=float, default=0, help="Seconds between refreshes")
        command.set_defaults(func=_command)
        if name == "run":
            command.add_argument("--allow-model", action="store_true")
            command.add_argument("--model", required=True, help="Explicit hosted model selector")
            command.add_argument("--endpoint", required=True, help="HTTPS OpenAI-compatible API base")
            command.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Credential environment variable name")
            command.add_argument("--disable-thinking", action="store_true",
                                 help="Explicit thinking.type=disabled for compatible providers")
            command.add_argument("--budget-usd", type=float, required=True, help="Lifetime aggregate reservation ceiling")
            command.add_argument("--input-price", type=float, required=True, help="Pinned USD per million input tokens")
            command.add_argument("--output-price", type=float, required=True, help="Pinned USD per million output tokens")
            command.add_argument("--max-calls", type=int, default=24, help="Lifetime aggregate provider request ceiling")
            command.add_argument("--calls-per-case", type=int, default=8)
            command.add_argument("--max-cases", type=int, default=3, help="New investigations per cycle")
            command.add_argument("--max-output-tokens", type=int, default=2000)
            command.add_argument("--timeout", type=float, default=90)
    report = sub.add_parser("report", help="Render source-cited investigation reports")
    report.add_argument("--out", type=Path, default=Path("derived/analyses/monitor"))
    report.set_defaults(func=_command)
    score = sub.add_parser("score", help="Score reports against frozen explicit labels")
    score.add_argument("--labels", type=Path, required=True, help="JSON array of provenance-bound labels")
    score.add_argument("--report", type=Path, action="append", required=True, help="Report JSON (repeatable)")
    score.add_argument("--category", default="reward_hacking")
    score.set_defaults(func=_command)
