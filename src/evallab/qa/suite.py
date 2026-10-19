"""Retrospective version x registered-key x frozen-split QA comparisons ($0)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_serializer, field_validator

from evallab.qa.answer_key import AnswerKey, item_id_of, load_answer_key
from evallab.qa.models import FINDING_SCHEMA, Finding, FindingSubject
from evallab.qa.score import ScoreRow, score_findings
from evallab.schemas import ContractModel
from evallab.storage.fs import durable_replace

Agent = Literal["task", "trace", "breaker", "tripwire"]
_AGENT_LABELS = {
    "task": "QA Task",
    "trace": "QA Trace",
    "breaker": "Breaker",
    "tripwire": "TA monitor",
}
_AGENT_PREFIXES = {
    "task": ("qa-task/", "qa-task@"),
    "trace": ("qa-trace/", "qa-trace@"),
    "breaker": ("qa-breaker/", "qa-breaker@", "breaker/", "breaker@", "cheat-ladder-"),
    "tripwire": ("tripwire/", "tripwire@", "tripwires/", "tripwires@", "qa-tripwire/", "live-watch/"),
}
_TRACE_STAGES = {"T0", "T1", "T2", "T3"}


class _Run(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent: Agent
    version: str = Field(min_length=1)
    key: str = Field(min_length=1)
    findings: str = Field(min_length=1)
    run: str | None = None
    notes: str | None = None

    @field_validator("version", "key", "findings")
    @classmethod
    def _not_padded(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("binding values must not have surrounding whitespace")
        return value


class _Manifest(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runs: list[_Run]


class SourceReceipt(ContractModel):
    """Raw source custody, not a replacement for per-item findings."""

    path: str
    sha256: str
    n_findings: int
    run: str | None = None
    notes: str | None = None


class SuiteRow(ContractModel):
    """A shared-score row plus the evidence denominators it cannot represent."""

    agent: Agent
    version: str
    score: ScoreRow
    n_applicable: int
    n_positive_measured: int
    n_negative_measured: int
    n_unknown_labels: int
    n_unknown_measured: int
    n_unknown_decisions: int
    n_not_run: int
    n_unusable: int
    n_no_evidence: int
    n_identity_unavailable: int
    n_outside_findings: int
    n_cost_findings: int
    n_recorded_costs: int
    recorded_reviewer_cost_usd: float | None
    recorded_cost_subtotal_usd: float | None
    cost_per_measured_item_usd: float | None
    sources: list[str]
    notes: list[str]

    @field_serializer("score")
    def _shared_metrics(self, score: ScoreRow) -> dict[str, Any]:
        # The legacy scorer defaults absent cost to zero; suite cost fields below
        # retain measuredness instead of exporting that misleading default.
        return score.model_dump(exclude={"cost_per_item_usd"})


class SuiteReport(ContractModel):
    agents: list[Agent]
    versions: list[str]
    date_utc: str
    new_benchmark_executions: int = 0
    new_rescore_cost_usd: float = 0.0
    frozen_custody: dict[str, str]
    bound_sources: list[str]
    sources: list[SourceReceipt]
    rows: list[SuiteRow]


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _canonical(finding: Finding) -> str:
    # exclude_unset retains the distinction between an omitted cost and recorded $0.
    return json.dumps(finding.model_dump(by_alias=True, exclude_unset=True), sort_keys=True)


def _unique(findings: list[Finding], source: Path) -> list[Finding]:
    seen: set[str] = set()
    ids: dict[str, str] = {}
    result: list[Finding] = []
    for finding in findings:
        canonical = _canonical(finding)
        if finding.id:
            previous = ids.setdefault(finding.id, canonical)
            if previous != canonical:
                raise ValueError(f"{source}: conflicting finding id {finding.id!r}")
        if canonical not in seen:
            seen.add(canonical)
            result.append(finding)
    return result


def _load_source(path: Path) -> tuple[list[Finding], SourceReceipt]:
    if path.is_dir():
        files = sorted(
            file
            for file in path.rglob("*")
            if file.is_file()
            and file.suffix in {".json", ".jsonl"}
            and not file.name.startswith("._")
        )
    elif path.is_file() and path.suffix == ".jsonl":
        files = [path]
    else:
        raise ValueError(f"findings source {path} must be an existing directory or JSONL file")
    findings: list[Finding] = []
    fingerprints: list[tuple[str, str]] = []
    for file in files:
        content = file.read_bytes()
        try:
            documents = (
                [
                    json.loads(line, object_pairs_hook=_json_object)
                    for line in content.decode("utf-8").splitlines()
                    if line.strip()
                ]
                if file.suffix == ".jsonl"
                else [json.loads(content, object_pairs_hook=_json_object)]
            )
            tagged = False
            for document in documents:
                schema = document.get("schema") if isinstance(document, dict) else None
                if schema != FINDING_SCHEMA:
                    if not path.is_dir() or (
                        isinstance(schema, str) and schema.startswith("qa.finding/")
                    ):
                        raise ValueError(f"expected schema {FINDING_SCHEMA!r}, got {schema!r}")
                    continue
                tagged = True
                finding = Finding.model_validate(document)
                if not math.isfinite(finding.reviewer.cost_usd):
                    raise ValueError("reviewer.cost_usd must be finite")
                findings.append(finding)
            if tagged:
                relative = file.relative_to(path).as_posix() if path.is_dir() else file.name
                fingerprints.append((relative, _sha256(content)))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"invalid findings in {file}: {exc}") from exc
    findings = _unique(findings, path)
    # Directory hashes bind the sorted relative-path/file-byte-hash inventory.
    digest = (
        _sha256(json.dumps(fingerprints, separators=(",", ":")).encode())
        if path.is_dir()
        else fingerprints[0][1]
        if fingerprints
        else _sha256(path.read_bytes())
    )
    return findings, SourceReceipt(path=str(path), sha256=digest, n_findings=len(findings))


def _validate_version(agent: str, version: str) -> None:
    if version != version.strip() or not any(
        version.startswith(prefix) and len(version) > len(prefix)
        for prefix in _AGENT_PREFIXES[agent]
    ):
        raise ValueError(
            f"{agent} version {version!r} must be the exact versioned reviewer.agent identity "
            f"(for example {_AGENT_PREFIXES[agent][0]}VERSION); unversioned labels are unavailable"
        )
    if agent == "trace" and version.rsplit("/", 1)[-1] in _TRACE_STAGES:
        raise ValueError("trace --version names the pipeline, not a /T0, /T1, /T2 or /T3 stage")


def _agent_of(version: str, agents: list[Agent]) -> Agent:
    """The requested agent whose identity prefixes name this version (prefixes are disjoint)."""
    owners = [
        agent for agent in agents
        if any(version.startswith(prefix) and len(version) > len(prefix) for prefix in _AGENT_PREFIXES[agent])
    ]
    if len(owners) != 1:
        if len(agents) == 1:
            _validate_version(agents[0], version)
        raise ValueError(
            f"version {version!r} is not an exact versioned identity of the requested agent(s) {agents}; "
            "unversioned labels are unavailable"
        )
    _validate_version(owners[0], version)
    return owners[0]


def _version_matches(finding: Finding, agent: str, version: str) -> bool:
    producer = finding.reviewer.agent
    return producer == version or (
        agent == "trace"
        and finding.stage in _TRACE_STAGES
        and producer == f"{version}/{finding.stage}"
    )


def _identity(subject: FindingSubject) -> tuple[str, str, str] | None:
    if subject.kind == "task":
        return ("task", subject.task_id, subject.task_digest) if subject.task_digest else None
    return ("trace", subject.task_id, subject.trial_id) if subject.trial_id else None


def _project_subject(subject: FindingSubject) -> FindingSubject:
    # The legacy scorer does not include task_digest in subject_key. A private,
    # lossless scoring projection keeps two digests of the same name separate.
    return subject.model_copy(update={"task_id": json.dumps(_identity(subject))})


def _load_split_guard(root: Path) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "research/answer-keys/splits/guard.py"
    spec = importlib.util.spec_from_file_location("_qa_suite_split_guard", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load the established split guard: {path}")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    # Re-root the established guard's module-level paths at the requested repository.
    keys = root / "research/answer-keys"
    vars(guard).update(
        REPO=root,
        KEYS=keys,
        EXPOSURE=keys / "splits/exposure.jsonl",
        ASSIGNMENTS=keys / "splits/assignments.csv",
        REPORT=keys / "splits/report.md",
    )
    return guard


def _frozen_keys(root: Path) -> tuple[list[AnswerKey], ModuleType, list[dict], dict[str, dict], dict[str, str]]:
    registry = root / "research/answer-keys"
    keys = [load_answer_key(path.parent) for path in sorted(registry.glob("*/items.jsonl"))]
    if not keys:
        raise ValueError(f"no registered answer keys under {registry}")
    raw_keys: dict[str, dict] = {}
    custody: dict[str, str] = {}
    for key in keys:
        for item in key.items:
            if item.key_id is not None and item.key_id != key.key_id:
                raise ValueError(f"{key.key_id}: item {item.item_id!r} names key {item.key_id!r}")
        raw_keys[key.key_id] = {
            "items": [
                item.model_dump(by_alias=True) | {"item_id": item_id_of(key.key_id, item)}
                for item in key.items
            ],
            "pairs": [pair.model_dump() for pair in key.pairs],
        }
        for name in ("README.md", "items.jsonl", "pairs.jsonl"):
            path = key.directory / name
            if path.is_file():
                custody[path.relative_to(root).as_posix()] = _sha256(path.read_bytes())
    guard = _load_split_guard(root)
    try:
        exposures = guard._jsonl(guard.EXPOSURE)
        errors = guard.validate_exposures(exposures, raw_keys)
        assignments, report, straddles = guard.build(raw_keys, exposures)
        errors += straddles
        for path, expected in ((guard.ASSIGNMENTS, assignments), (guard.REPORT, report)):
            if path.read_text(encoding="utf-8") != expected:
                errors.append(f"frozen split file {path} is stale; reconcile with splits/guard.py")
        if errors:
            raise ValueError("split/exposure guard: " + "; ".join(errors))
        for path in (guard.EXPOSURE, guard.ASSIGNMENTS, guard.REPORT):
            custody[path.relative_to(root).as_posix()] = _sha256(path.read_bytes())
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed split/exposure evidence: {exc}") from exc
    return keys, guard, exposures, raw_keys, custody


def _bindings(manifest: Path | None, keys: list[AnswerKey]) -> dict[tuple[str, str, str], tuple[_Run, Path]]:
    if manifest is None:
        return {}
    parsed = _Manifest.model_validate(
        json.loads(manifest.read_bytes(), object_pairs_hook=_json_object)
    )
    known = {key.key_id for key in keys}
    bindings: dict[tuple[str, str, str], tuple[_Run, Path]] = {}
    for run in parsed.runs:
        _validate_version(run.agent, run.version)
        if run.key not in known:
            raise ValueError(f"runs manifest {manifest}: unknown registered key {run.key!r}")
        identity = (run.agent, run.version, run.key)
        if identity in bindings:
            raise ValueError(f"runs manifest {manifest}: duplicate/conflicting run binding {identity}")
        source = Path(run.findings)
        source = (source if source.is_absolute() else manifest.parent / source).resolve()
        if not source.is_dir() and not (source.is_file() and source.suffix == ".jsonl"):
            raise ValueError(f"runs manifest {manifest}: missing/invalid findings source {source}")
        bindings[identity] = (run, source)
    return bindings


def _exposure_notes(
    guard: ModuleType,
    exposures: list[dict],
    exposure_hits: dict[str, dict[str, set[str]]],
    key: AnswerKey,
    score: ScoreRow,
    measured_ids: set[str],
) -> list[str]:
    notes: list[str] = []
    raw_row = score.model_dump()
    in_split = {item_id_of(key.key_id, item) for item in key.items if item.split == score.split}
    for record in exposures:
        if not guard._applies(record, raw_row):
            continue
        touched = exposure_hits[record["exposure_id"]].get(key.key_id, set()) & in_split
        if not touched:
            continue
        ack = next(
            (
                ack
                for ack in record.get("acknowledged_rows") or []
                if ack["key_id"] == key.key_id
                and score.system.startswith(ack.get("system_prefix", ""))
            ),
            None,
        )
        # Unlike a scoreboard aggregate, we can verify the *actual* excluded IDs.
        if score.split in guard.REPORTED and ack and ack["how"] == "excluded" and touched & measured_ids:
            raise ValueError(
                f"split/exposure guard: {key.key_id}/{score.split} actually measured excluded "
                f"items for {record['exposure_id']}: {sorted(touched & measured_ids)}"
            )
        notes.append(
            f"{record['exposure_id']}: {record['kind']} exposure on {len(touched)} item(s)"
            + (f"; {ack['reason']}" if ack and ack.get("reason") else "")
            + "; not independent held-out evidence"
        )
    return list(dict.fromkeys(notes))


def _comparison_row(
    key: AnswerKey,
    findings: list[Finding],
    sources: list[str],
    *,
    agent: Agent,
    version: str,
    split: str,
    day: str,
    guard: ModuleType,
    exposures: list[dict],
    exposure_hits: dict[str, dict[str, set[str]]],
    unavailable: int,
) -> SuiteRow:
    kind = "task" if agent in {"task", "breaker"} else "trace"
    identities = {_identity(item.subject) for item in key.items} - {None}
    matched = [finding for finding in findings if _identity(finding.subject) in identities]
    outside = len(findings) - len(matched)
    by_subject: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for finding in matched:
        identity = _identity(finding.subject)
        if identity is not None:
            by_subject[identity].append(finding)
    items = [item for item in key.items if item.split == split]
    projected = AnswerKey(
        key_id=key.key_id,
        items=tuple(
            item.model_copy(
                update={"item_id": item_id_of(key.key_id, item), "subject": _project_subject(item.subject)}
            )
            for item in key.items
        ),
        directory=key.directory,
        pairs=key.pairs,
    )
    projected_findings = [
        finding.model_copy(update={"subject": _project_subject(finding.subject)}) for finding in matched
    ]
    score = next(
        row
        for row in score_findings(
            projected,
            projected_findings,
            split=split,
            agent=_AGENT_LABELS[agent],
            system=version,
            scored_at=f"{day}T00:00:00Z",
        )
        if row.category == "all"
    )
    measured_ids: set[str] = set()
    measured_labels: dict[str, int] = defaultdict(int)
    states: dict[str, int] = defaultdict(int)
    selected: dict[str, Finding] = {}
    measured_findings: dict[str, Finding] = {}
    for item in items:
        identity = _identity(item.subject)
        matches = by_subject.get(identity, []) if identity is not None else []
        for finding in matches:
            selected[_canonical(finding)] = finding
        measured = [f for f in matches if f.measurement == "measured" and f.verdict != "unknown"]
        if measured:
            measured_ids.add(item_id_of(key.key_id, item))
            measured_labels[item.expected_verdict] += 1
            for finding in measured:
                measured_findings[_canonical(finding)] = finding
        elif any(f.measurement == "measured" for f in matches):
            states["unknown"] += 1
        elif any(f.measurement == "ran_without_usable_result" for f in matches):
            states["unusable"] += 1
        elif matches:
            states["not_run"] += 1
        else:
            states["no_evidence"] += 1
    n_applicable = sum(item.subject.kind == kind for item in items)
    if not n_applicable:
        score = score.model_copy(update={"status": "inapplicable"})
    elif not sources:
        score = score.model_copy(update={"status": "no-run"})
    elif not score.n_measured:
        score = score.model_copy(update={"status": "unmeasured"})
    recorded = [f for f in selected.values() if "cost_usd" in f.reviewer.model_fields_set]
    subtotal = round(sum(f.reviewer.cost_usd for f in recorded), 8) if recorded else None
    complete_cost = bool(selected) and len(recorded) == len(selected)
    measured_cost_complete = bool(measured_findings) and all(
        "cost_usd" in finding.reviewer.model_fields_set for finding in measured_findings.values()
    )
    notes = _exposure_notes(guard, exposures, exposure_hits, key, score, measured_ids)
    if unavailable:
        notes.append(f"{unavailable} key-local finding(s) have another or unproved producer/version")
    if outside:
        notes.append(f"{outside} finding(s) outside the exact key subjects (including digest/trial mismatches)")
    if not n_applicable:
        notes.append(f"{agent} evaluates {kind} subjects; this split has none")
    if len(selected) != len(recorded):
        notes.append(f"reviewer cost unrecorded on {len(selected) - len(recorded)} matched finding(s)")
    if not selected:
        notes.append("no matching recorded reviewer cost; historical cost unavailable")
    return SuiteRow(
        agent=agent,
        version=version,
        score=score,
        n_applicable=n_applicable,
        n_positive_measured=measured_labels["fail"],
        n_negative_measured=measured_labels["pass"],
        n_unknown_labels=sum(item.expected_verdict == "unknown" for item in items),
        n_unknown_measured=measured_labels["unknown"],
        n_unknown_decisions=states["unknown"],
        n_not_run=states["not_run"],
        n_unusable=states["unusable"],
        n_no_evidence=states["no_evidence"],
        n_identity_unavailable=sum(_identity(item.subject) is None for item in items),
        n_outside_findings=outside,
        n_cost_findings=len(selected),
        n_recorded_costs=len(recorded),
        recorded_reviewer_cost_usd=subtotal if complete_cost else None,
        recorded_cost_subtotal_usd=subtotal,
        cost_per_measured_item_usd=score.cost_per_item_usd if measured_cost_complete else None,
        sources=sources,
        notes=notes,
    )


def build_suite(
    root: Path, *, agents: list[Agent], versions: list[str], day: str, manifest: Path | None = None
) -> SuiteReport:
    """Read surviving evidence, enforce custody, and reuse the shared QA scorer."""
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("--date must be an ISO YYYY-MM-DD date")
    if not versions or len(set(versions)) != len(versions):
        raise ValueError("supply at least one --version; repeated identical versions are invalid")
    if not agents or len(set(agents)) != len(agents):
        raise ValueError("supply at least one --agent; repeated identical agents are invalid")
    owner = {version: _agent_of(version, agents) for version in versions}
    versions = sorted(versions)
    keys, guard, exposures, raw_keys, custody = _frozen_keys(root)
    exposure_hits = {
        record["exposure_id"]: guard.exposed_items(record, raw_keys) for record in exposures
    }
    bindings = _bindings(manifest, keys)
    if manifest is not None:
        custody[str(manifest)] = _sha256(manifest.read_bytes())
    receipts: dict[str, SourceReceipt] = {}
    source_cache: dict[Path, tuple[list[Finding], SourceReceipt]] = {}

    def load(path: Path) -> tuple[list[Finding], SourceReceipt]:
        if path not in source_cache:
            source_cache[path] = _load_source(path)
        return source_cache[path]

    rows: list[SuiteRow] = []
    for version in versions:
        agent = owner[version]
        for key in keys:
            bound = bindings.get((agent, version, key.key_id))
            candidates: list[Finding] = []
            source_paths: list[str] = []
            unavailable = 0
            if bound is not None:
                run, path = bound
                candidates, receipt = load(path)
                if not candidates:
                    raise ValueError(f"run {key.key_id}/{version}: {path} contains no qa.finding/v1 records")
                wrong = sorted({f.reviewer.agent for f in candidates if not _version_matches(f, agent, version)})
                kind = "task" if agent in {"task", "breaker"} else "trace"
                if wrong or any(f.subject.kind != kind for f in candidates):
                    raise ValueError(
                        f"run {key.key_id}/{version}: source {path} does not prove the selected "
                        f"producer/version and {kind} subject kind; reviewers={wrong}. "
                        "Select a correctly attributed source; manifest labels cannot relabel findings."
                    )
                receipt = receipt.model_copy(update={"run": run.run, "notes": run.notes})
                receipts[str(path)] = receipt
                source_paths.append(str(path))
            else:
                local_sources = sorted(key.directory.glob("findings*.jsonl"))
                if (key.directory / "findings").is_dir():
                    local_sources.append(key.directory / "findings")
                for path in sorted(local_sources):
                    loaded, receipt = load(path.resolve())
                    kind = "task" if agent in {"task", "breaker"} else "trace"
                    selected = [
                        f for f in loaded
                        if _version_matches(f, agent, version) and f.subject.kind == kind
                    ]
                    unavailable += len(loaded) - len(selected)
                    if selected:
                        candidates.extend(selected)
                        receipts[receipt.path] = receipt
                        source_paths.append(receipt.path)
            candidates = _unique(candidates, key.directory)
            for split in sorted({item.split for item in key.items}):
                rows.append(
                    _comparison_row(
                        key, candidates, sorted(source_paths), agent=agent, version=version,
                        split=split, day=day, guard=guard, exposures=exposures,
                        exposure_hits=exposure_hits, unavailable=unavailable,
                    )
                )
    violations, _ = guard.audit_scoreboard(
        raw_keys, exposures,
        [row.score.model_dump() for row in rows if row.score.n_measured],
    )
    if violations:
        raise ValueError("split/exposure guard: " + "; ".join(violations))
    return SuiteReport(
        agents=sorted(agents), versions=versions, date_utc=day,
        frozen_custody=dict(sorted(custody.items())),
        bound_sources=sorted({str(path) for _run, path in bindings.values()}),
        sources=[receipts[path] for path in sorted(receipts)], rows=rows,
    )


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _shown(path: str, root: Path | None) -> str:
    """Repository-relative display path; JSON receipts keep the resolved path."""
    if root is None:
        return path
    try:
        return Path(path).relative_to(root.resolve()).as_posix()
    except ValueError:
        return path


_UNMEASURED_STATUSES = ("no-run", "inapplicable")


def render_suite(report: SuiteReport, root: Path | None = None) -> str:
    """Deterministic Markdown; every rate is beside its measured denominator."""
    agents = ", ".join(f"`{agent}`" for agent in report.agents)
    lines = [
        f"# QA suite — {report.date_utc}", "",
        f"Agents: {agents}. Retrospective label agreement on every registered key's native splits; category=all.", "",
        "**New benchmark executions: 0. New re-score cost: $0.** No agents, solvers, models or repair loops were run.",
        "Missing evidence is not a pass. Unknown labels never supply a false-alarm denominator; incomplete pairs do not become measured pairs.",
        "Coverage states are item-level: usable measured decisions take precedence over unknown decisions, then unusable, explicit not-run, and missing evidence.",
        "These are not new blinded tests or independent generalization estimates. Source-derived labels, inspected outputs, weak negatives and overlapping keys retain their original limits.",
        "Recorded reviewer USD below is only the explicit charge on matching raw findings, including unusable/unknown decisions. It is not total historical audit, solver or model cost; even an explicit $0 may describe republication only. Missing cost fields are n/a, never $0.", "",
        "## Cells with version-proved findings", "",
    ]
    bound = [row for row in report.rows if row.score.status not in _UNMEASURED_STATUSES]
    if bound:
        lines += [
            "| Agent | Version | Key | Split | Status | Recall (measured +) | False alarms (measured −) | Paired Δ (measured / pairs) | Measured / items | Unknown labels (measured / total) | Recorded reviewer USD (cost records / matched) | USD / measured item | Sources |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    else:
        lines.append("None: no requested version has raw findings on any registered key.")
    for row in bound:
        score = row.score
        cost = "n/a" if row.recorded_reviewer_cost_usd is None else f"${row.recorded_reviewer_cost_usd:.6f}"
        if cost == "n/a" and row.recorded_cost_subtotal_usd is not None:
            cost += f" (known subtotal ${row.recorded_cost_subtotal_usd:.6f})"
        values = [
            row.agent, row.version, score.key_id, score.split, score.status,
            f"{_number(score.recall)} (n={row.n_positive_measured})",
            f"{_number(score.false_alarm_rate)} (n={row.n_negative_measured})",
            f"{_number(score.paired_discrimination)} ({score.n_pairs_measured}/{score.n_pairs})",
            f"{score.n_measured}/{score.n_items}",
            f"{row.n_unknown_measured}/{row.n_unknown_labels}",
            f"{cost} ({row.n_recorded_costs}/{row.n_cost_findings})",
            "n/a" if row.cost_per_measured_item_usd is None else f"${row.cost_per_measured_item_usd:.6f}",
            "; ".join(_shown(source, root) for source in row.sources) or "none",
        ]
        lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines += [
        "", "## Unmeasured cells", "",
        "`no-run`: no version-proved raw finding exists for that key/split, so measured = 0 and every rate is n/a. "
        "`inapplicable`: the split has no subjects of the agent's kind (task vs trace).", "",
        "| Agent | Version | Status | Key/split cells (items) |",
        "|---|---|---|---|",
    ]
    for version in report.versions:
        rows = [row for row in report.rows if row.version == version]
        for status in _UNMEASURED_STATUSES:
            cells = [f"{row.score.key_id}/{row.score.split} ({row.score.n_items})" for row in rows if row.score.status == status]
            if cells:
                values = [rows[0].agent, version, status, ", ".join(cells)]
                lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines += ["", "## Coverage and interpretation", ""]
    for row in bound:
        score = row.score
        notes = [
            f"applicable subjects {row.n_applicable}/{score.n_items}",
            f"unknown decisions {row.n_unknown_decisions}; explicit not-run {row.n_not_run}; unusable {row.n_unusable}; no evidence {row.n_no_evidence}",
        ]
        if row.n_identity_unavailable:
            notes.append(f"{row.n_identity_unavailable} key item(s) lack the required digest/trial identity")
        notes += row.notes
        if score.notes:
            notes.append(score.notes)
        lines.append(f"- **{_cell(row.version)} / {_cell(score.key_id)} / {_cell(score.split)}:** " + _cell("; ".join(notes)))
    lines += ["", "## Source bindings", ""]
    if not report.sources:
        lines.append("No version-proved raw findings were available; all applicable cells remain no-run.")
    for source in report.sources:
        lines.append(f"- `{_cell(_shown(source.path, root))}` — `{source.sha256}`, {source.n_findings} unique raw findings.")
        if source.run is not None or source.notes is not None:
            lines.append(f"  Metadata only (not score authority): run={_cell(source.run)}; notes={_cell(source.notes)}")
    lines += ["", "Directory source hashes bind the sorted (relative path, file-byte SHA256) inventory of files containing findings; file source hashes bind the original bytes.", "", "## Frozen key and split custody", ""]
    for path, digest in report.frozen_custody.items():
        lines.append(f"- `{_cell(_shown(path, root))}` — `{digest}`")
    lines += ["", "Label provenance and limitations remain in each `research/answer-keys/<key>/README.md` and its `items.jsonl`; the suite neither changes labels/splits/exposures nor qualifies repaired graders.", ""]
    return "\n".join(lines)


def _suite_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    staged: Path | None = None
    try:
        manifest = args.runs
        if manifest is not None:
            manifest = (manifest if manifest.is_absolute() else root / manifest).resolve()
        report = build_suite(root, agents=args.agent, versions=args.version, day=args.date, manifest=manifest)
        destination = args.output or Path(f"research/qa-scoreboard/suite-{args.date}.md")
        destination = destination if destination.is_absolute() else root / destination
        # Do not let --output turn a successful comparison into destruction of its inputs.
        protected = {(root / path).resolve() for path in report.frozen_custody}
        source_paths = {Path(source.path) for source in report.sources}
        source_paths.update(Path(path) for path in report.bound_sources)
        for path in source_paths:
            if destination.resolve() == path or (
                path.is_dir() and destination.resolve().is_relative_to(path)
            ):
                raise ValueError("--output must not overwrite findings evidence")
        if destination.resolve() in protected or destination.resolve().is_relative_to(
            (root / "research/answer-keys").resolve()
        ):
            raise ValueError("--output must not overwrite frozen keys, splits or the runs manifest")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
            staged = Path(handle.name)
            handle.write(render_suite(report, root))
        durable_replace(staged, destination)
        staged = None
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: evallab qa suite: {exc}", file=sys.stderr)
        return 2
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"{len(report.rows)} comparison rows; new benchmark executions=0; report: {destination}")
    return 0


def add_suite_command(commands: argparse._SubParsersAction) -> None:
    suite = commands.add_parser("suite", help="Compare retained versions on every frozen answer-key split ($0)")
    suite.add_argument(
        "--agent", choices=tuple(_AGENT_LABELS), action="append", required=True,
        help="QA agent kind; repeat to put several agents' versions in one report",
    )
    suite.add_argument(
        "--version", action="append", required=True,
        help="Exact versioned producer identity of one requested agent; repeat to compare versions",
    )
    suite.add_argument("--runs", type=Path, help="JSON runs manifest; paths are relative to its directory")
    suite.add_argument("--date", default=datetime.now(UTC).date().isoformat(), help="Report date (YYYY-MM-DD, UTC)")
    suite.add_argument("--output", type=Path, help="Atomic report destination (default: research/qa-scoreboard/suite-DATE.md)")
    suite.add_argument("--json", action="store_true", help="Also print the full comparison and source custody as JSON")
    suite.set_defaults(func=_suite_command)
