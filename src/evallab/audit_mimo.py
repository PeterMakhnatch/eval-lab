"""Read-only MiMo stored-audit plugin and HAR-186 evidence facets.

MiMo identifiers, the frozen upstream snapshot and ledger routing policy live
here, not in the dataset-neutral audit core. Whole-cohort reads load each
CSV/JSON/lineage source once; single-task dossiers use the same projection.
Nothing here generates variants, materializes packages, executes controls,
or writes stores. ``keep`` is the ledger's routing decision, not certification.
Health/solve/verdict classifiers and probe binding reuse the canonical
HAR-172/177 and HAR-161 implementations; typed lineage retains candidate status.

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
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from evallab.dataset_audit_contracts import (
    ALL_STAGES,
    AuditDataset,
    AuditObservation,
    AuditRecord,
    AuditSource,
    AuditStage,
    AuditTask,
    AuditVerdict,
)
from evallab.exploit_probe import HF_SOURCE, SNAPSHOT, run_package
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.storage.paths import shared_checkout_root
from evallab.task_health_tags import (
    _csv_rows,  # same-package reuse; CSV files keep the canonical validators
    _probe_binding,  # same-package reuse of the HAR-161 binding rules
    health_tag,
    solve_summary,
    verdict_tag,
)
from evallab.task_variants import RECORDS_DIRNAME, VariantRecord

DATASET_ID = "mimo-v2.6-rl"
SOURCE_URI = f"https://huggingface.co/datasets/{HF_SOURCE['repo']}"
_TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
_TASK_PREFIXES = (f"{DATASET_ID}/", f"{DATASET_ID}__")
# Legacy HAR-186 callers may ask for a safe non-MiMo leaf; its facet stays null.
_LEAF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
FAILING_TESTS = Path("research/experiments/python-task-ledger/failing_tests.csv")
EXPLOIT = Path("research/experiments/har161-exploit/probe_verdicts.json")
POOL = Path("research/experiments/har108-python-census/pool.json")
ORACLE_PILOT = LEDGER.with_name("oracle_pilot.csv")
ORACLE_SWEEP = LEDGER.with_name("oracle_sweep.csv")
LEAK_SCANS = tuple(
    Path("research/experiments/har177-leak-scan") / filename
    for filename in ("validation10.csv", "pilot20.csv", "pilot2864.csv", "sample100.csv")
)


def recognizes_dataset(selector: str) -> bool:
    """Recognize explicit aliases of this frozen code-pool dataset only."""
    return selector in {
        DATASET_ID,
        "mimo",
        HF_SOURCE["repo"],
        f"{HF_SOURCE['repo']}@{HF_SOURCE['revision']}",
        f"hf://{HF_SOURCE['repo']}@{HF_SOURCE['revision']}",
        SOURCE_URI,
    }


def recognizes_task(task_id: str) -> bool:
    """Recognize the leaf or either exact native MiMo namespace spelling."""
    for prefix in _TASK_PREFIXES:
        if task_id.startswith(prefix):
            task_id = task_id.removeprefix(prefix)
            break
    return _TASK_ID.fullmatch(task_id) is not None


def normalize_task_id(task_id: str) -> str:
    for prefix in _TASK_PREFIXES:
        if task_id.startswith(prefix):
            task_id = task_id.removeprefix(prefix)
            break
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError(f"invalid MiMo task id {task_id!r}")
    return task_id


def task_aliases(task_id: str) -> list[str]:
    canonical = normalize_task_id(task_id)
    return [canonical, f"{DATASET_ID}/{canonical}", f"{DATASET_ID}__{canonical}"]


def task_page_name(task_id: str) -> str:
    return "task-" + normalize_task_id(task_id).removeprefix("format-code-task-")

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


@dataclass
class _EvidenceInputs:
    root: Path
    tables: dict[str, dict[str, dict[str, str]] | None] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    probes: dict[str, dict[str, Any]] | None = None
    records: dict[str, list[VariantRecord]] = field(default_factory=dict)
    record_sources: dict[str, AuditSource] = field(default_factory=dict)
    record_errors: dict[str, list[str]] = field(default_factory=dict)

    def row(self, source: str, task_id: str) -> dict[str, str] | None:
        table = self.tables.get(source)
        return table.get(task_id) if table is not None else None

    def load_table(self, name: str, path: Path, required: set[str]) -> None:
        self.tables[name], self.sources[name] = _csv_table(
            _resolve(self.root, path), required=required, label=name
        )


def _json_source(path: Path, *, label: str) -> tuple[Any, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, _source_entry(path, None, error=f"{label}: unreadable ({exc})")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, _source_entry(path, raw, error=f"{label}: {exc}")
    return payload, _source_entry(path, raw)


def _audit_source(entry: Mapping[str, Any], *, binding: str) -> AuditSource:
    digest = entry.get("sha256")
    return AuditSource(
        path=entry.get("path") or "not supplied",
        sha256=f"sha256:{digest}" if digest is not None else None,
        available=entry["available"],
        binding=binding,
    )


def _load_records(inputs: _EvidenceInputs, *, task_id: str | None) -> None:
    """Index exact MiMo task names and read every selected lineage file once."""
    base = _resolve(inputs.root, RECORDS_DIRNAME)
    entry = _source_entry(
        base, None, error=None if base.is_dir() else "variant records directory absent"
    )
    entry["available"] = base.is_dir()
    inputs.sources["variants"] = entry
    if not entry["available"]:
        return
    try:
        directories = sorted(path for path in base.iterdir() if path.is_dir())
    except OSError as exc:
        entry["error"] = f"variant records unreadable ({exc})"
        return
    for directory in directories:
        if not recognizes_task(directory.name):
            continue
        canonical = normalize_task_id(directory.name)
        if task_id is not None and canonical != task_id:
            continue
        errors = inputs.record_errors.setdefault(canonical, [])
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError as exc:
            errors.append(f"{directory}: lineage directory unreadable ({exc})")
            continue
        for path in paths:
            try:
                raw = path.read_bytes()
                record = VariantRecord.model_validate_json(raw)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                errors.append(
                    f"{_repo_relpath(inputs.root, path)}: invalid lineage record ({exc})"
                )
                continue
            if record.task_name not in task_aliases(canonical):
                errors.append(
                    f"{_repo_relpath(inputs.root, path)}: task_name "
                    f"{record.task_name!r} does not match {canonical}"
                )
                continue
            inputs.records.setdefault(canonical, []).append(record)
            inputs.record_sources[record.variant_digest] = _audit_source(
                _source_entry(path, raw),
                binding="typed lineage; recorded parent/package and Harbor pins; not revalidation",
            )


def _load_inputs(
    root: Path, *, static_audit: Path | None, task_id: str | None = None
) -> _EvidenceInputs:
    inputs = _EvidenceInputs(root)
    for name, path, required in (
        ("ledger", LEDGER, _LEDGER_REQUIRED),
        ("history", HISTORY, _HISTORY_REQUIRED),
        ("locked_nop", LOCKED_NOP, _LOCKED_REQUIRED),
        ("failing_tests", FAILING_TESTS, {"task_id"}),
    ):
        inputs.load_table(name, path, required)
    payload, inputs.sources["exploit"] = _json_source(root / EXPLOIT, label="exploit")
    if payload is not None:
        if not isinstance(payload, dict) or any(
            not _LEAF_ID.fullmatch(key) or not isinstance(value, dict)
            for key, value in payload.items()
        ):
            inputs.sources["exploit"]["error"] = "exploit: invalid task-id-keyed object"
        else:
            inputs.probes = payload
    _load_records(inputs, task_id=task_id)
    if static_audit is None:
        inputs.sources["static"] = {
            "path": None, "sha256": None, "available": False, "note": "not supplied"
        }
    else:
        inputs.load_table("static", static_audit, {"task_id"})
    return inputs


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
    """Return the unchanged HAR-186 facet, including honest missing evidence."""
    if recognizes_task(task_id):
        task_id = normalize_task_id(task_id)
    elif not _LEAF_ID.fullmatch(task_id):
        raise ValueError(f"invalid task id {task_id!r}")
    root = Path(repo_root).resolve()
    return _task_evidence(
        task_id, _load_inputs(root, static_audit=static_audit, task_id=task_id)
    )


def _task_evidence(task_id: str, inputs: _EvidenceInputs) -> dict[str, Any]:
    root = inputs.root
    errors = list(inputs.record_errors.get(task_id, ()))
    sources = {
        name: dict(inputs.sources[name])
        for name in ("ledger", "history", "locked_nop", "failing_tests", "exploit", "variants", "static")
    }
    records = inputs.records.get(task_id, [])
    ledger_row = inputs.row("ledger", task_id)
    history_row = inputs.row("history", task_id)
    locked_row = inputs.row("locked_nop", task_id)
    failing_row = inputs.row("failing_tests", task_id)
    probe_row = inputs.probes.get(task_id) if inputs.probes is not None else None

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
    static_source = sources["static"]
    if static_source["available"] and static_source.get("error"):
        static_flags = {"error": static_source["error"]}
    else:
        static_row = inputs.row("static", task_id)
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


def _package_task(
    task_id: str,
    row: dict[str, str],
    inputs: _EvidenceInputs,
    *,
    primary: Path,
    pool: Mapping[str, dict[str, Any]],
    provenance: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> AuditTask:
    """Resolve recorded package pins without inventing absent local bytes."""
    digest = row.get("run_digest", "")
    package_digest = digest if _DIGEST.fullmatch(digest) else None
    records = inputs.records.get(task_id, [])
    selected = next((record for record in records if record.variant_digest == digest), None)
    identity_records = [
        record for record in records
        if record.variant_digest == digest or record.parent.digest == digest
    ]
    recorded_harbor = {
        record.variant_harbor_digest if record.variant_digest == digest
        else record.parent.harbor_digest
        for record in identity_records
    }
    harbor_digest = next(iter(recorded_harbor)) if len(recorded_harbor) == 1 else None
    name = selected.task_name if selected is not None else f"{DATASET_ID}/{task_id}"
    parent: dict[str, Any] = {
        "kind": "external_corpus" if row.get("run") == "original" else "curated_derivative",
        "upstream": dict(HF_SOURCE),
        "split": row.get("split") or None,
        "split_group": (pool.get(task_id) or {}).get("split_group"),
        "pool_entry": pool.get(task_id),
        "ledger_selection": dict(row),
        "license": provenance.get("license"),
        "upstream_material_digest": provenance.get("material_digest"),
        "snapshot_provenance": dict(provenance),
        "adapter_source": manifest.get("source"),
        "adapter_revision": manifest.get("revision"),
        "adapter_task_digest": (manifest.get("tasks") or {}).get(task_id),
        "package_binding": "recorded ledger pin; local bytes unavailable",
        "task_name_binding": "typed lineage" if selected is not None else "frozen MiMo namespace",
        "inclusion": "one row per task in the stored Python census ledger",
        "exclusions": "other MiMo domains and code tasks outside this census pool",
    }
    if selected is not None:
        parent["variant"] = _record_summary(inputs.root, selected)
        parent["parent"] = selected.parent.model_dump(mode="json")
    if len(recorded_harbor) > 1:
        parent["conflicting_recorded_harbor_digests"] = sorted(recorded_harbor)
    path: Path | None = None
    if package_digest is not None:
        candidate = run_package(primary, row)
        parent["package_location"] = str(candidate)
        if candidate.is_dir():
            try:
                observed_package = task_directory_digest(candidate)
                observed_harbor = harbor_task_digest(candidate)
                with (candidate / "task.toml").open("rb") as handle:
                    config = tomllib.load(handle)
                native_name = (config.get("task") or {}).get("name")
                actual_name = (
                    native_name.strip()
                    if isinstance(native_name, str) and native_name.strip()
                    else candidate.name
                )
            except (OSError, ValueError) as exc:
                parent["package_binding"] = f"unreadable package: {exc}"
            else:
                parent["observed_package_digest"] = observed_package
                parent["observed_harbor_digest"] = observed_harbor
                parent["observed_task_name"] = actual_name
                if observed_package != package_digest:
                    parent["package_binding"] = "local package digest mismatch"
                elif harbor_digest is not None and observed_harbor != harbor_digest:
                    parent["package_binding"] = "local Harbor digest mismatch"
                elif actual_name not in task_aliases(task_id):
                    parent["package_binding"] = "local native task identity mismatch"
                else:
                    path = candidate.resolve()
                    harbor_digest = observed_harbor
                    name = actual_name
                    parent["package_binding"] = "local bytes match recorded ledger package pin"
                    parent["task_name_binding"] = "local task.toml"
    return AuditTask(
        dataset_id=DATASET_ID,
        task_id=task_id,
        task_name=name,
        path=path,
        package_digest=package_digest,
        harbor_digest=harbor_digest,
        aliases=tuple(task_aliases(task_id)),
        source_uri=SOURCE_URI,
        revision=HF_SOURCE["revision"],
        parent_source=parent,
    )


def _digest_binding(value: str | None, package_digest: str | None) -> dict[str, Any]:
    valid = bool(value and _DIGEST.fullmatch(value))
    return {
        "recorded_package_digest": value or None,
        "current_package_digest": package_digest,
        "digest_match": value == package_digest if valid and package_digest is not None else None,
        "scope": "exact package" if valid else "task-level; package identity unbound",
    }


def _stage_observation(
    stage: AuditStage, task: AuditTask, inputs: _EvidenceInputs, facets: dict[str, Any]
) -> AuditObservation:
    task_id = task.task_id
    row = inputs.row("ledger", task_id)
    facts: dict[str, Any]
    sources: list[AuditSource] = []
    source_names: list[str]
    present: bool
    reason: str
    if stage == "leak":
        scans = []
        source_names = ["ledger", "exploit", *(f"leak_{path.stem}" for path in LEAK_SCANS)]
        for path in LEAK_SCANS:
            scan = inputs.row(f"leak_{path.stem}", task_id)
            if scan is not None:
                scans.append({
                    "source": f"leak_{path.stem}",
                    "row": dict(scan),
                    "binding": _digest_binding(scan.get("ledger_digest"), task.package_digest),
                })
        facts = {
            "leak": facets["leak"],
            "repair": facets["repair"],
            "image_history_scans": scans,
            "coverage": {"ledger_row": row is not None, "image_history_rows": len(scans)},
            "binding": "ledger channel is census-era; image/package mismatches remain historical",
            "limits": "candidate strip records do not certify leak closure; no fresh scan executed",
        }
        present = row is not None or bool(scans)
        reason = "no stored leak row for this task"
        for record in inputs.records.get(task_id, []):
            if record.variant_digest == task.package_digest or record.parent.digest == task.package_digest:
                sources.append(inputs.record_sources[record.variant_digest])
    elif stage == "oracle":
        labels = []
        source_names = ["oracle_pilot", "oracle_sweep"]
        for name in source_names:
            label = inputs.row(name, task_id)
            if label is not None:
                labels.append({
                    "source": name,
                    **dict(label),
                    "binding": _digest_binding(label.get("run_digest"), task.package_digest),
                })
        selected = labels[-1] if labels else None
        facts = {
            "labels": labels,
            "selected": selected,
            "coverage": {"pilot_row": inputs.row("oracle_pilot", task_id) is not None,
                         "sweep_row": inputs.row("oracle_sweep", task_id) is not None},
            "binding": selected["binding"] if selected is not None else None,
            "limits": "stored labels only; no HAR-191 executor or fresh oracle result",
            "routing": "ledger verdict is authoritative; oracle:fail is not proof of a broken task",
        }
        present = bool(labels)
        reason = "no stored oracle pilot/sweep row for this task"
    elif stage == "nop":
        source_names = ["ledger", "locked_nop"]
        locked = inputs.row("locked_nop", task_id)
        note = locked.get("note", "") if locked is not None else ""
        prefix = note.removeprefix("repaired:") if note.startswith("repaired:") else None
        prefix_match = (
            task.package_digest.removeprefix("sha256:").startswith(prefix)
            if prefix and task.package_digest is not None else None
        )
        census_label = row.get("census_label") if row is not None else None
        facts = {
            "census_label": census_label or None,
            "census_job": (row.get("census_nop_job") or None) if row is not None else None,
            "census_evidence": (row.get("census_evidence") or None) if row is not None else None,
            "locked_nop": dict(locked) if locked is not None else None,
            "health": facets["health"],
            "coverage": {"census_label": bool(census_label), "locked_row": locked is not None},
            "binding": {"scope": "historical task cohort; repair notes carry only a digest prefix",
                        "repair_digest_prefix": prefix, "prefix_match": prefix_match,
                        "exact_current_package": None},
            "limits": "recorded controls are not model trajectories or fresh verifier proof",
        }
        present = bool(census_label) or locked is not None
        reason = "no stored nop evidence for this task"
    elif stage == "static":
        source_names = ["static"]
        static_row = inputs.row("static", task_id)
        facts = {
            "static_flags": facets["static_flags"],
            "coverage": {"row": static_row is not None},
            "binding": _digest_binding(
                static_row.get("run_digest") if static_row is not None else None,
                task.package_digest,
            ),
            "limits": "descriptive lexical flags; stale labels ignored; never a quality verdict",
        }
        present = static_row is not None
        reason = "optional static audit not supplied or task row unavailable"
    elif stage == "history":
        source_names = ["history", "failing_tests"]
        history_row = inputs.row("history", task_id)
        try:
            solve = solve_summary(history_row)
            classification_error = None
        except ValueError as exc:
            solve = {"tag": "solve:unknown"}
            classification_error = str(exc)
        facts = {
            "solve": solve,
            "history": dict(history_row) if history_row is not None else None,
            "failing_tests": facets["failing_tests"],
            "coverage": {"history_row": history_row is not None,
                         "failing_tests_row": facets["failing_tests"] is not None},
            "binding": "task history across packages/models/harnesses, not current-package capability",
            "classification_error": classification_error,
        }
        present = history_row is not None or facets["failing_tests"] is not None
        reason = "no stored solve or failing-test history for this task"
    else:
        source_names = ["exploit"]
        facts = {
            "exploit_probes": facets["exploit_probes"],
            "coverage": {"probe_row": facets["exploit_probes"] is not None},
            "binding": "only explicit package-bound probes affect current health; others remain unbound",
            "limits": "not corpus-wide exploit resistance or an independent admission judgment",
        }
        present = facets["exploit_probes"] is not None
        reason = "no stored exploit probe for this task"
    source_errors = {
        name: inputs.sources[name]["error"] for name in source_names
        if inputs.sources[name].get("error") and inputs.sources[name]["available"]
    }
    facts["source_errors"] = source_errors
    for name in source_names:
        sources.append(_audit_source(inputs.sources[name], binding=f"{stage}: see row binding and coverage"))
    malformed = bool(source_errors) or (
        stage == "static" and bool((facets["static_flags"] or {}).get("error"))
    ) or (stage == "history" and facts.get("classification_error") is not None)
    return AuditObservation(
        stage=stage,
        status="failed" if malformed else "recorded" if present else "unavailable",
        facts=facts,
        sources=tuple(sources),
        reason="stored source/classification error; findings remain unknown" if malformed
        else None if present else reason,
    )


def read_stored_audit(
    repo_root: Path, *, stages: Sequence[AuditStage] = ALL_STAGES,
    static_audit: Path | None = None,
) -> tuple[AuditDataset, list[AuditRecord]]:
    """Read the stored MiMo routing ledger once without executing or writing."""
    unknown_stages = set(stages) - set(ALL_STAGES)
    if unknown_stages:
        raise ValueError(f"unsupported audit stages: {sorted(unknown_stages)}")
    root = Path(repo_root).resolve()
    inputs = _load_inputs(root, static_audit=static_audit)
    ledger = inputs.tables["ledger"]
    if ledger is None:
        raise ValueError(inputs.sources["ledger"].get("error", "stored MiMo ledger unavailable"))
    inputs.load_table("oracle_pilot", ORACLE_PILOT, {"task_id", "label", "run_digest", "evidence"})
    inputs.load_table("oracle_sweep", ORACLE_SWEEP, {"task_id", "label"})
    for path in LEAK_SCANS:
        inputs.load_table(f"leak_{path.stem}", path, {"task_id"})
    pool_payload, inputs.sources["pool"] = _json_source(root / POOL, label="pool")
    entries = pool_payload.get("pool") if isinstance(pool_payload, dict) else pool_payload
    pool: dict[str, dict[str, Any]] = {}
    if isinstance(entries, list):
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("task_id"), str)
                or not recognizes_task(entry["task_id"])
            ):
                inputs.sources["pool"]["error"] = "pool: invalid task entry"
                continue
            task_id = normalize_task_id(entry["task_id"])
            if task_id in pool:
                inputs.sources["pool"]["error"] = f"pool: duplicate task_id {task_id!r}"
                continue
            pool[task_id] = entry
    elif pool_payload is not None:
        inputs.sources["pool"]["error"] = "pool: expected entries list"
    primary = shared_checkout_root(root)
    snapshot = primary / SNAPSHOT
    provenance, inputs.sources["snapshot_provenance"] = _json_source(
        snapshot / "provenance.json", label="snapshot provenance"
    )
    manifest, inputs.sources["snapshot_manifest"] = _json_source(
        snapshot / "manifest.json", label="snapshot manifest"
    )
    provenance = provenance if isinstance(provenance, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    tasks = []
    records = []
    for task_id, row in sorted(ledger.items()):
        task = _package_task(
            task_id, row, inputs, primary=primary, pool=pool,
            provenance=provenance, manifest=manifest,
        )
        facets = _task_evidence(task_id, inputs)
        try:
            routing_tag = verdict_tag(row)
            verdict: AuditVerdict = cast(AuditVerdict, row["verdict"])
        except ValueError as exc:
            verdict = "unknown"
            routing_tag = None
            facets["errors"].append(str(exc))
        tags = list(facets["tags"] or [])
        if routing_tag is not None and routing_tag not in tags:
            tags.append(routing_tag)
        records.append(AuditRecord(
            task=task,
            verdict=verdict,
            verdict_reason=row.get("verdict_evidence") or "stored ledger routing decision",
            verdict_sources=(_audit_source(
                inputs.sources["ledger"],
                binding="authoritative stored routing; keep is not certification or admission",
            ),),
            tags=tuple(tags),
            stages={stage: _stage_observation(stage, task, inputs, facets) for stage in stages},
            facets=facets,
        ))
        tasks.append(task)
    dataset = AuditDataset(
        dataset_id=DATASET_ID,
        source_uri=SOURCE_URI,
        revision=HF_SOURCE["revision"],
        license=provenance.get("license"),
        tasks=tuple(tasks),
        sources=tuple(
            _audit_source(entry, binding=f"stored MiMo source: {name}")
            for name, entry in inputs.sources.items()
        ),
        plugin="mimo",
    )
    return dataset, records


def static_observation(task: AuditTask) -> AuditObservation:
    """Inspect MiMo's added-test patch without treating harness text as tests."""
    if task.path is None or not task.path.is_dir():
        return AuditObservation(
            stage="static",
            status="unavailable",
            facts={"coverage": "unknown"},
            reason="selected MiMo package is unavailable",
        )
    from evallab.dataset_audit_checks import static_observation as inspect_static

    return inspect_static(
        task,
        added_patch=task.path / "tests" / "test.patch",
        harness_basenames={"mimo_test_command.sh", "mimo_build_env.tar.gz.b64", "test_commands.json"},
    )


def _fresh_routing_label(task: AuditTask, observation: AuditObservation) -> str | None:
    facts = observation.facts
    if (
        observation.status != "executed"
        or facts.get("completed") is not True
        or facts.get("exception")
        or task.package_digest is None
        or facts.get("package_digest") != task.package_digest
    ):
        return None
    observed_harbor = facts.get("harbor_digest")
    if observed_harbor is not None and observed_harbor != task.harbor_digest:
        return None
    label = facts.get("label")
    if observation.stage == "nop":
        reward = facts.get("reward")
        if label == "nop:pass" or (
            isinstance(reward, (int, float)) and not isinstance(reward, bool) and reward == 1.0
        ):
            return "nop:pass"
    elif observation.stage == "oracle" and label in (
        "nop:pass", "oracle:none", "oracle:fail-network"
    ):
        return str(label)
    return None


def finalize_record(record: AuditRecord) -> AuditRecord:
    """Apply MiMo routing precedence to fresh, completed, package-bound controls.

    This is the existing ledger's ORACLE_NOT_KEEP policy, not an admission or
    candidate-validation decision. Neutral oracle failures, missing controls
    and static findings never replace a stored route; discard takes priority.
    """
    if record.verdict in ("discard", "fix"):
        return record
    findings: list[tuple[str, AuditObservation]] = []
    for stage in ("nop", "oracle"):
        observation = record.stages.get(stage)
        if observation is not None:
            label = _fresh_routing_label(record.task, observation)
            if label is not None:
                findings.append((label, observation))
    if not findings:
        return record
    reason = "fresh package-bound " + ", ".join(label for label, _ in findings)
    reason += f"; supersedes stored {record.verdict} routing, not task admission"
    sources = list(record.verdict_sources)
    for _, observation in findings:
        for source in observation.sources:
            if source not in sources:
                sources.append(source)
    tags = tuple(tag for tag in record.tags if not tag.startswith("verdict:")) + ("verdict:fix",)
    facets = dict(record.facets)
    facets["verdict"] = {"tag": "verdict:fix", "verdict": "fix", "evidence": reason}
    facets["tags"] = list(tags)
    return record.model_copy(update={
        "verdict": "fix",
        "verdict_reason": reason,
        "verdict_sources": tuple(sources),
        "tags": tags,
        "facets": facets,
    })


__all__ = [
    "recognizes_dataset", "recognizes_task", "normalize_task_id", "task_aliases",
    "task_page_name", "task_evidence", "read_stored_audit", "finalize_record", "static_observation",
]
