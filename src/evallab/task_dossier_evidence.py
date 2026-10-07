"""Read-only evidence slice for ``evallab task <task_id>`` (HAR-186).

:func:`task_evidence` combines already-recorded sources for one canonical
leaf task id (e.g. ``format-code-task-000792``) into a plain-dict facet the
parent facade merges with the trial facet. It never generates variants,
materializes packages, runs probes, or writes stores: health/verdict labels
reuse the pure HAR-172/177 classifiers
(:func:`health_tag`, :func:`solve_summary`, :func:`verdict_tag`), probe
binding reuses the HAR-161 :func:`_probe_binding` rules, and repair lineage
reuses the typed :class:`VariantRecord` loader.

Result keys (all present; ``None`` means absent/unknown, never a negative
claim)::

    health: source-backed dict with ``tag``/``solve``/ledger/lock/bound-probe
        detail, or None when the task has no ledger row.
    verdict: ``{"tag", "verdict", "evidence"}`` from the ledger row, or None.
    tags: ``[health, solve, verdict]`` label strings, or None.
    leak: ``{"found", "channel", "ledger_channel", "evidence", "note"}``.
        A digest-bound probe verdict overrides the stale census-era ledger
        channel; an unbound or digest-mismatched probe never does. A missing
        or ``unknown`` ledger channel is ``found=None``; only a recorded
        ``none_found`` is an explicitly bounded negative.
    repair: strip/repair record detail with candidate-vs-validated status
        preserved exactly (never upgraded). The already-applied repair
        (ledger run_digest equals its variant digest) is preferred over
        parent-matching candidates; ``records`` keeps every matching record.
        None when nothing matching is recorded.
    static_flags: optional static-audit overlay (a..g flags, ev_* excerpts,
        byte counts). Stale health/solve labels are echoed under
        ``ignored_stale`` only. None when no audit was supplied, the file is
        missing, or the task has no row.
    failing_tests: typed HAR-179 row (JSON list cells decoded, counts
        numeric, booleans genuine; empty missing-evidence cells stay None),
        or None.
    exploit_probes: list with the task's probe-verdict row (bound/unbound
        made explicit), or None when the probe source or the task row is
        absent.
    sources: ``{name: {"path", "sha256", "available", ...}}`` provenance.
    errors: list of genuine (non-fatal) read/parse problems; empty normally.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evallab.task_health_tags import (
    _csv_rows,  # same-package reuse; CSV files keep the canonical validators
    _probe_binding,  # same-package reuse of the HAR-161 binding rules
    health_tag,
    solve_summary,
    verdict_tag,
)
from evallab.task_variants import RECORDS_DIRNAME, resolve_record

#: Public selector: any safe canonical leaf id, not just the MiMo
#: ``format-code-task-NNNNNN`` family. Path separators, globs and control
#: characters are rejected; CSV row validation keeps the existing canonical
#: constraints via the shared ``_csv_rows``.
_LEAF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
FAILING_TESTS = Path("research/experiments/python-task-ledger/failing_tests.csv")
EXPLOIT = Path("research/experiments/har161-exploit/probe_verdicts.json")

_LEDGER_REQUIRED = {
    "task_id",
    "status",
    "reason",
    "run",
    "run_digest",
    "run_variant_status",
    "verdict",
    "verdict_evidence",
}
_HISTORY_REQUIRED = {"task_id", "runs", "clean_pass", "copied_pass", "fail", "infra"}
_LOCKED_REQUIRED = {"task_id", "locked_nop", "note"}

_FAILING_INT_COLS = (
    "runs",
    "unique_runs",
    "models",
    "unknown_model_runs",
    "ctrf_runs",
    "verifier_runs",
    "no_agent_runs",
    "no_agent_ctrf_runs",
    "no_agent_verifier_runs",
)
_FAILING_LIST_COLS = (
    "model_names",
    "always_failing_tests",
    "observed_common_failing_tests",
    "verifier_sources",
    "test_evidence",
    "candidate_tests",
    "model_trial_paths",
    "no_agent_trial_paths",
    "notes",
)
_FAILING_BOOL_COL = "candidate_broken_test"

_STATIC_FLAG_COLS = (
    "a_unstated_literal",
    "b_network",
    "c_nondeterminism",
    "d_env_coupled",
    "e_tiny_suite",
    "f_repo_file_read",
    "g_polyglot_toolchain",
)
_STATIC_EVIDENCE_COLS = ("ev_a", "ev_b", "ev_c", "ev_d", "ev_e", "ev_f", "ev_g")

_PROBE_LEAK_VERDICTS = {"leak-found-not-cracked", "cracked"}


def _resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _source_entry(path: Path | None, raw: bytes | None, error: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": str(path) if path is not None else None,
        "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "available": raw is not None,
    }
    if error is not None:
        entry["error"] = error
    return entry


def _csv_table(path: Path, *, required: set[str], label: str) -> tuple[dict[str, dict[str, str]] | None, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, _source_entry(path, None, error=f"{label}: unreadable ({exc})")
    try:
        rows = _csv_rows(raw, required=required, label=label)
    except ValueError as exc:
        return None, _source_entry(path, raw, error=str(exc))
    return rows, _source_entry(path, raw)




def _parse_int_cell(value: str | None) -> tuple[int | None, bool]:
    if value is None or value == "":
        return None, False
    try:
        return int(value), False
    except ValueError:
        return None, True


def _parse_list_cell(value: str | None) -> tuple[list[Any] | None, bool]:
    # Empty cell = missing evidence (None), never a recorded empty list.
    if value is None or value == "":
        return None, False
    try:
        parsed = json.loads(value)
    except ValueError:
        return None, True
    if not isinstance(parsed, list):
        return None, True
    return parsed, False


def _parse_bool_cell(value: str | None) -> tuple[bool | None, bool]:
    if value is None or value == "":
        return None, False
    if value == "True":
        return True, False
    if value == "False":
        return False, False
    return None, True


def _typed_failing_row(row: Mapping[str, str]) -> dict[str, Any]:
    typed: dict[str, Any] = {"task_id": row.get("task_id")}
    parse_errors: list[str] = []
    for column in _FAILING_INT_COLS:
        value, failed = _parse_int_cell(row.get(column))
        typed[column] = value
        if failed:
            parse_errors.append(column)
    for column in _FAILING_LIST_COLS:
        value, failed = _parse_list_cell(row.get(column))
        typed[column] = value
        if failed:
            parse_errors.append(column)
    value, failed = _parse_bool_cell(row.get(_FAILING_BOOL_COL))
    typed[_FAILING_BOOL_COL] = value
    if failed:
        parse_errors.append(_FAILING_BOOL_COL)
    evidence_state = row.get("model_evidence_state")
    typed["model_evidence_state"] = evidence_state if evidence_state != "" else None
    typed["parse_errors"] = parse_errors
    return typed


def _static_flags_row(row: Mapping[str, str]) -> dict[str, Any]:
    flags: dict[str, bool | None] = {}
    problems: list[str] = []
    for column in _STATIC_FLAG_COLS:
        cell = row.get(column)
        if cell == "1":
            flags[column] = True
        elif cell == "0":
            flags[column] = False
        else:
            flags[column] = None
            problems.append(f"{column}={cell!r}")
    evidence = {column: (row.get(column) or None) for column in _STATIC_EVIDENCE_COLS}
    instr_bytes, instr_failed = _parse_int_cell(row.get("instr_bytes"))
    patch_bytes, patch_failed = _parse_int_cell(row.get("patch_bytes"))
    if instr_failed:
        problems.append(f"instr_bytes={row.get('instr_bytes')!r}")
    if patch_failed:
        problems.append(f"patch_bytes={row.get('patch_bytes')!r}")
    result: dict[str, Any] = {
        "flags": flags,
        "evidence": evidence,
        "instr_bytes": instr_bytes,
        "patch_bytes": patch_bytes,
        # Stale labels are task authority nowhere; echoed only so a reader
        # can see what was ignored.
        "ignored_stale": {
            "health": row.get("health") or None,
            "solve": row.get("solve") or None,
            "ledger_status": row.get("ledger_status") or None,
        },
    }
    if problems:
        result["error"] = f"malformed static-audit cells: {', '.join(problems)}"
    return result


def _repo_relpath(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except (OSError, ValueError):
        return str(path)


def _scan_task_records(
    root: Path, task_id: str, errors: list[str]
) -> tuple[list[Any], dict[str, Any]]:
    """Typed variant records whose task name is this leaf task id.

    Only slug directories containing the task id are opened, so a dossier
    read never walks the whole lineage library.
    """
    base = _resolve(root, RECORDS_DIRNAME)
    entry = _source_entry(
        base,
        None,
        error=None if base.is_dir() else "variant records directory absent",
    )
    entry["available"] = base.is_dir()
    records: list[Any] = []
    if not base.is_dir():
        return records, entry
    try:
        dirs = sorted(p for p in base.iterdir() if p.is_dir() and task_id in p.name)
    except OSError as exc:
        entry["error"] = f"variant records unreadable ({exc})"
        return records, entry
    for directory in dirs:
        for path in sorted(directory.glob("*.json")):
            try:
                record = resolve_record(path, repo_root=root)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                errors.append(f"{_repo_relpath(root, path)}: invalid lineage record ({exc})")
                continue
            name = record.task_name or ""
            if name == task_id or name.endswith("/" + task_id):
                records.append(record)
            else:
                errors.append(
                    f"{_repo_relpath(root, path)}: task_name {name!r} does not match {task_id}"
                )
    return records, entry


def _record_summary(root: Path, record: Any) -> dict[str, Any]:
    return {
        "variant_digest": record.variant_digest,
        "variant_harbor_digest": record.variant_harbor_digest,
        "parent_digest": record.parent.digest,
        "status": record.status,
        "transform": record.transform,
        "record": _repo_relpath(root, _resolve(root, record.record_relpath())),
        "created_by": record.created_by,
        "created_at": record.created_at,
    }


def task_evidence(
    task_id: str, *, repo_root: Path, static_audit: Path | None = None
) -> dict[str, Any]:
    """Return the read-only recorded-evidence facet for one leaf task id."""
    if not _LEAF_ID.fullmatch(task_id):
        raise ValueError(f"invalid task id {task_id!r}")
    root = Path(repo_root).resolve()
    errors: list[str] = []
    sources: dict[str, Any] = {}

    ledger, sources["ledger"] = _csv_table(
        _resolve(root, LEDGER), required=_LEDGER_REQUIRED, label="ledger"
    )
    history, sources["history"] = _csv_table(
        _resolve(root, HISTORY), required=_HISTORY_REQUIRED, label="history"
    )
    locked, sources["locked_nop"] = _csv_table(
        _resolve(root, LOCKED_NOP), required=_LOCKED_REQUIRED, label="locked_nop"
    )
    failing, sources["failing_tests"] = _csv_table(
        _resolve(root, FAILING_TESTS), required={"task_id"}, label="failing_tests"
    )

    exploit_path = _resolve(root, EXPLOIT)
    try:
        exploit_raw = exploit_path.read_bytes()
    except OSError as exc:
        probes: dict[str, dict[str, Any]] | None = None
        sources["exploit"] = _source_entry(
            exploit_path, None, error=f"exploit: unreadable ({exc})"
        )
    else:
        try:
            payload = json.loads(exploit_raw)
            if not isinstance(payload, dict):
                raise ValueError("exploit verdicts must be a task-id-keyed JSON object")
            probes = {
                key: value
                for key, value in payload.items()
                if _LEAF_ID.fullmatch(key) and isinstance(value, dict)
            }
            if len(probes) != len(payload):
                raise ValueError("exploit: invalid task row")
            sources["exploit"] = _source_entry(exploit_path, exploit_raw)
        except ValueError as exc:
            probes = None
            sources["exploit"] = _source_entry(exploit_path, exploit_raw, error=str(exc))

    records, sources["variants"] = _scan_task_records(root, task_id, errors)

    ledger_row = ledger.get(task_id) if ledger is not None else None
    history_row = history.get(task_id) if history is not None else None
    locked_row = locked.get(task_id) if locked is not None else None
    failing_row = failing.get(task_id) if failing is not None else None
    probe_row = probes.get(task_id) if probes is not None else None

    run_digest = ledger_row.get("run_digest") if ledger_row is not None else None
    if run_digest is not None and not _DIGEST.fullmatch(run_digest):
        errors.append(f"ledger: invalid run_digest {run_digest!r} for {task_id}")
        run_digest = None

    binding: dict[str, Any] | None = None
    if probe_row is not None:
        try:
            binding = dict(_probe_binding(probe_row, input_dir=root))
        except ValueError as exc:
            errors.append(f"exploit binding for {task_id}: {exc}")
            binding = None

    bound: bool | None = None
    digest_match: bool | None = None
    if probe_row is not None:
        if run_digest is not None and binding is not None:
            package = binding.get("package")
            if package is not None:
                digest_match = package == run_digest
                bound = digest_match
            else:
                # A lock-derived harbor digest cannot be compared without the
                # parent package bytes; report it unbound, never current.
                bound = False
                digest_match = None
        else:
            bound = None
            digest_match = None

    health: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    tags: list[str] | None = None
    if ledger_row is not None:
        try:
            exploit_verdict = (
                probe_row.get("verdict") if (bound and probe_row) else None
            )
            health_label = health_tag(
                ledger_row, locked_row, exploit_verdict=exploit_verdict
            )
            solve = solve_summary(history_row)
            verdict_label = verdict_tag(ledger_row)
            tags = [health_label, solve["tag"], verdict_label]
            manifest_ref: dict[str, Any] | None = None
            if run_digest is not None:
                manifests = [
                    record
                    for record in records
                    if record.transform == "task-health-tags@1"
                    and record.parent.digest == run_digest
                ]
                if manifests:
                    newest = max(manifests, key=lambda record: record.created_at)
                    manifest_ref = {
                        "record": _repo_relpath(
                            root, _resolve(root, newest.record_relpath())
                        ),
                        "variant_digest": newest.variant_digest,
                        "status": newest.status,
                    }
            health = {
                "tag": health_label,
                "solve": solve,
                "ledger_status": ledger_row.get("status"),
                "ledger_reason": ledger_row.get("reason"),
                "census_label": ledger_row.get("census_label"),
                "locked_nop": dict(locked_row) if locked_row is not None else None,
                "exploit_verdict": exploit_verdict,
                "digest_match": digest_match,
                "binding": binding,
                "manifest": manifest_ref,
            }
            verdict = {
                "tag": verdict_label,
                "verdict": ledger_row.get("verdict"),
                "evidence": ledger_row.get("verdict_evidence") or None,
            }
        except ValueError as exc:
            errors.append(f"health classifiers for {task_id}: {exc}")
            health = None
            verdict = None
            tags = None

    exploit_probes: list[dict[str, Any]] | None = None
    if probe_row is not None:
        exploit_probes = [
            {
                "verdict": probe_row.get("verdict"),
                "bound": bound,
                "digest_match": digest_match,
                "binding": binding,
                "task_package_digest": probe_row.get("task_package_digest"),
                "trial": probe_row.get("trial"),
                "probe_job": probe_row.get("probe_job"),
                "reward": probe_row.get("reward"),
                "copied": probe_row.get("copied"),
                "image": probe_row.get("image"),
                "signals": probe_row.get("signals"),
            }
        ]

    leak: dict[str, Any] | None = None
    if ledger_row is not None:
        ledger_channel = ledger_row.get("leak_channel") or None
        reason = ledger_row.get("reason", "")
        reason_leak = reason.casefold().startswith("image leaks the fix:") or (
            ledger_row.get("status") == "review"
            and "with no leak-closed variant" in reason
        )
        evidence_refs: list[str] = []
        ledger_evidence = ledger_row.get("evidence") or None
        if ledger_evidence:
            evidence_refs.append(ledger_evidence)
        if bound and probe_row is not None and probe_row.get("verdict") in _PROBE_LEAK_VERDICTS:
            found: bool | None = True
            channel = f"observed:{probe_row.get('verdict')}"
            if probe_row.get("trial"):
                evidence_refs.append(str(probe_row.get("trial")))
            for line in probe_row.get("image") or []:
                evidence_refs.append(f"probe image: {line}")
        elif reason_leak:
            found = True
            channel = ledger_channel
        elif ledger_channel in (None, "", "unknown"):
            # An unrecorded or explicitly unknown channel is unknown, never a
            # negative: only a recorded ``none_found`` bounds the negative.
            found = None
            channel = ledger_channel or None
        elif ledger_channel != "none_found":
            found = True
            channel = ledger_channel
        else:
            # Census-era ``none_found`` predates strip/probe evidence; it is
            # the recorded channel, not proof against every exploit.
            found = False
            channel = ledger_channel
        leak = {
            "found": found,
            "channel": channel,
            "ledger_channel": ledger_channel,
            "evidence": evidence_refs,
            "note": (
                "ledger channel is census-era and covers only the recorded "
                "nop-gradability/package channels; a digest-bound probe "
                "overrides it without changing task qualification."
            ),
        }

    repair: dict[str, Any] | None = None
    if run_digest is not None:
        # The already-applied repair (ledger run_digest is its variant
        # digest) is the current one; parent-matching strip/repair records
        # are older candidates. Statuses are reported verbatim, never
        # upgraded.
        applied = [
            record
            for record in records
            if record.variant_digest == run_digest
            and ("strip" in record.transform or "repair" in record.transform)
        ]
        parent_matched = [
            record
            for record in records
            if record.parent.digest == run_digest
            and ("strip" in record.transform or "repair" in record.transform)
        ]
        combined = {record.variant_digest: record for record in parent_matched}
        for record in applied:
            combined.setdefault(record.variant_digest, record)
        repair_like = sorted(combined.values(), key=lambda r: r.created_at)
        current = applied[0] if applied else None
        newest = (
            current
            if current is not None
            else max(parent_matched, key=lambda r: r.created_at)
            if parent_matched
            else None
        )
        if newest is not None:
            repair = {
                **_record_summary(root, newest),
                "leak_patterns": list((newest.inputs or {}).get("leak_patterns") or []),
                "evidence": [
                    {
                        "at": item.at,
                        "by": item.by,
                        "status": item.status,
                        "evidence": item.evidence,
                    }
                    for item in (newest.evidence or [])
                ],
                "records": [_record_summary(root, record) for record in repair_like],
            }

    if probe_row is not None and leak is not None and bound:
        record_paths = [
            item["record"]
            for item in (repair.get("records", []) if repair else [])
        ]
        leak["evidence"].extend(
            path for path in record_paths if path not in leak["evidence"]
        )
        patterns: list[str] = []
        if repair:
            patterns = list(repair.get("leak_patterns", []))
        if patterns:
            leak["evidence"].append(f"strip repair leak_patterns: {patterns}")

    failing_tests: dict[str, Any] | None = None
    if failing_row is not None:
        failing_tests = _typed_failing_row(failing_row)

    static_flags: dict[str, Any] | None = None
    if static_audit is None:
        sources["static"] = {
            "path": None,
            "sha256": None,
            "available": False,
            "note": "not supplied",
        }
    else:
        audit_path = static_audit if static_audit.is_absolute() else root / static_audit
        try:
            static_raw = audit_path.read_bytes()
        except OSError as exc:
            sources["static"] = _source_entry(
                audit_path, None, error=f"static audit: unreadable ({exc})"
            )
        else:
            sources["static"] = _source_entry(audit_path, static_raw)
            try:
                static_rows = _csv_rows(
                    static_raw, required={"task_id"}, label="static audit"
                )
            except ValueError as exc:
                static_flags = {"error": str(exc)}
            else:
                static_row = static_rows.get(task_id)
                if static_row is not None:
                    static_flags = _static_flags_row(static_row)

    return {
        "health": health,
        "verdict": verdict,
        "tags": tags,
        "leak": leak,
        "repair": repair,
        "static_flags": static_flags,
        "failing_tests": failing_tests,
        "exploit_probes": exploit_probes,
        "sources": sources,
        "errors": errors,
    }


__all__ = ["task_evidence"]
