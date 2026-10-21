"""Teacher-data export for the open QA harness (spec ``audit-harness-spec.md`` item 8).

Retained audit sessions, reward-seeker runs and executed-clean package manifests become
teacher rows only when every gate passes:

* **execution** - the run ends in an execution-confirmed finding (a host-validated measured
  ``witness:<id>``, or a differential replay with upstream reward 1.0 and port reward 0.0) or
  in an execution-confirmed *clean* (strict repeat controls: reference all 1.0, nop all 0.0).
  A source-only finding, a refuted finding or an unmeasured run never becomes a row.
* **split guard** - a task that is a reported-split item in any answer key (``test`` or
  ``heldout``), a tb4-audit-v1 TEST task by name or digest, or a listed held-out id is refused.
* **model** - the model that actually answered must equal the requested one
  (``agent_lanes.attest_model``). A mismatch is always refused; an unreported model is refused
  unless ``allow_unattested`` is set, and the row then says so.
* **dedup** - one row per distinct ``(task, body)``; duplicates are reported, not exported.

Every row carries a clearance record with the evidence for each gate. Every refusal is written
with all failing reasons, so counts can be audited. Nothing here runs a model or Docker.

    evallab qa env export-sft --audit-root DIR --seeker-root DIR --executed-clean FILE --out DIR
    evallab qa env attest --session AGENT_DIR | --transcript FILE --requested MODEL
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.qa.agent_lanes import (
    attest_audit_session,
    attest_model,
    attest_transcript,
    read_jsonl,
)
from evallab.qa.models import Finding

ROW_SCHEMA = "qa.teacher_row/v1"
CLEARANCE_SCHEMA = "qa.teacher_clearance/v1"
REFUSAL_SCHEMA = "qa.teacher_refusal/v1"
REPORT_SCHEMA = "qa.teacher_export/v1"
EXPORTER_VERSION = "1"

#: Tools whose measured receipt is an executed witness; mirrors ``qa.env._RUNTIME_TOOLS``.
RUNTIME_TOOLS = frozenset({
    "run-upstream-grader", "run-port-grader", "run-reference", "run-blank", "probe",
})
#: Defect class -> ``qa.finding/v1`` category; mirrors ``qa.env_agent._CLASS_CATEGORY``.
CLASS_CATEGORY = {
    "weak_check": "weak", "unstated_requirement": "prompt_misaligned",
    "instruction_grader_contradiction": "prompt_misaligned",
    "grader_tamperable": "tamperable", "answer_reachable": "reachable",
    "grader_logic_bug": "broken", "overstrict_oracle": "broken",
    "wrong_answer_key": "broken", "env_mismatch_or_flaky": "broken",
    "infeasible_as_written": "broken", "exploitable_llm_judge": "weak",
}
#: Order in which a refused candidate's *primary* reason is chosen; every reason is still kept.
REASON_ORDER = (
    "split_guard", "model_mismatch", "model_unattested", "no_recorded_finding",
    "invalid_finding", "refuted", "no_execution_confirmation", "duplicate_body",
)
GUARD_SCOPES = ("reported", "tb4-heldout")
TB4_KEY = "tb4-audit-v1"
#: Per-observation character budget in exported turns; the full text is hashed, not lost.
OBSERVATION_CLIP_CHARS = 6000
PACKET_CLIP_CHARS = 24_000
_WITNESS = re.compile(r"witness:([0-9a-f]{32})")


class ExportError(ValueError):
    """The export could not be set up (bad guard sources, existing output directory)."""


def task_name(task_id: str) -> str:
    """Bare task name shared across keys: drop ``@pin`` and any benchmark prefix."""
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _file_sha(path: Path) -> str | None:
    return _sha(path.read_bytes()) if path.is_file() else None


# -- split guard -----------------------------------------------------------------------


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    status: str  #: ``refused`` | ``dev_only`` | ``unregistered``
    task_name: str
    reasons: tuple[str, ...]
    registered: tuple[tuple[str, str], ...]


class SplitGuard:
    """Refuses tasks that sit in a reported split of any answer key.

    ``scope="reported"`` (default) refuses a task that is ``test`` or ``heldout`` in *any* key.
    ``scope="tb4-heldout"`` refuses only tb4-audit-v1 ``test`` and any ``heldout`` item.
    Independently of scope: a task whose digest matches a tb4-audit-v1 TEST task, and any task in
    ``refused_ids`` (bare names or full ids), is refused.
    """

    def __init__(
        self,
        *,
        assignments: Iterable[dict[str, str]],
        tb4_test_digests: Iterable[str] = (),
        tb4_test_names: Iterable[str] = (),
        refused_ids: Iterable[str] = (),
        scope: str = "reported",
        sources: dict[str, str | None] | None = None,
    ) -> None:
        if scope not in GUARD_SCOPES:
            raise ExportError(f"guard scope must be one of {GUARD_SCOPES}")
        self.scope = scope
        self.sources = dict(sources or {})
        self._by_name: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for row in assignments:
            self._by_name[task_name(row["task"])].add((row["key_id"], row["split"]))
        self._tb4_digests = frozenset(tb4_test_digests)
        self._tb4_names = frozenset(tb4_test_names)
        self._refused = frozenset(task_name(item) for item in refused_ids)

    @classmethod
    def from_repo(
        cls, repo_root: Path, *, scope: str = "reported", refuse_ids_file: Path | None = None
    ) -> SplitGuard:
        keys = Path(repo_root) / "research" / "answer-keys"
        assignments_path = keys / "splits" / "assignments.csv"
        tb4_path = keys / TB4_KEY / "tasks.jsonl"
        for path in (assignments_path, tb4_path):
            if not path.is_file():
                raise ExportError(f"split guard source missing: {path}")
        with assignments_path.open(encoding="utf-8", newline="") as stream:
            assignments = list(csv.DictReader(stream))
        digests: list[str] = []
        names: list[str] = []
        for row in read_jsonl(tb4_path):
            if isinstance(row, dict) and row.get("split") == "test":
                subject = row.get("subject") or {}
                if subject.get("task_digest"):
                    digests.append(subject["task_digest"])
                names.append(task_name(subject.get("task_id") or (row.get("package") or {}).get("name", "")))
        refused: list[str] = []
        if refuse_ids_file is not None:
            refused = [
                line.strip() for line in Path(refuse_ids_file).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        return cls(
            assignments=assignments, tb4_test_digests=digests, tb4_test_names=names,
            refused_ids=refused, scope=scope,
            sources={
                "assignments_sha256": _file_sha(assignments_path),
                "tb4_tasks_sha256": _file_sha(tb4_path),
                "refuse_ids_sha256": _file_sha(Path(refuse_ids_file)) if refuse_ids_file else None,
            },
        )

    def check(self, task_id: str, task_digest: str | None = None) -> GuardVerdict:
        name = task_name(task_id)
        registered = tuple(sorted(self._by_name.get(name, ())))
        reasons: list[str] = []
        for key, split in registered:
            if split == "heldout":
                reasons.append(f"{key}: heldout")
            elif split == "test" and (self.scope == "reported" or key == TB4_KEY):
                reasons.append(f"{key}: test")
        if name in self._tb4_names and not any(r.startswith(f"{TB4_KEY}: test") for r in reasons):
            reasons.append(f"{TB4_KEY}: test (task list)")
        if task_digest is not None and task_digest in self._tb4_digests:
            reasons.append(f"{TB4_KEY}: test (task digest {task_digest[:19]})")
        if name in self._refused:
            reasons.append("refuse-ids list")
        if reasons:
            return GuardVerdict(False, "refused", name, tuple(reasons), registered)
        status = "dev_only" if registered else "unregistered"
        return GuardVerdict(True, status, name, (), registered)


# -- candidates and gating -------------------------------------------------------------


@dataclass
class Candidate:
    """One run that might become a teacher row; adapters fill it, ``clear`` judges it."""

    family: str
    source: dict[str, Any]
    task_id: str
    task_digest: str | None
    outcome: str  #: ``finding`` | ``clean``
    execution: dict[str, Any]
    attestation: dict[str, Any]
    target: dict[str, Any]
    defect_class: str | None = None
    category: str | None = None
    finding: dict[str, Any] | None = None
    packet: dict[str, Any] = field(default_factory=dict)
    turns: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[tuple[str, str]] = field(default_factory=list)

    @property
    def body_sha256(self) -> str:
        text = " ".join(str(self.target.get("summary") or "").casefold().split())
        label = self.defect_class or self.category or "clean"
        return _sha(f"{self.outcome}\n{label}\n{text}\n{self.target.get('package_digest', '')}".encode())

    @property
    def dedup_key(self) -> str:
        return f"{task_name(self.task_id)}|{self.body_sha256}"


@dataclass(frozen=True)
class Refusal:
    source: dict[str, Any]
    task_id: str | None
    outcome: str | None
    reasons: tuple[tuple[str, str], ...]

    @property
    def primary(self) -> str:
        codes = {code for code, _ in self.reasons}
        return next((code for code in REASON_ORDER if code in codes), self.reasons[0][0])

    def as_record(self) -> dict[str, Any]:
        return {
            "schema": REFUSAL_SCHEMA, "primary": self.primary, "source": self.source,
            "task_id": self.task_id, "outcome": self.outcome,
            "reasons": [{"code": code, "detail": detail} for code, detail in self.reasons],
        }


def _class_label(row: dict[str, Any]) -> str:
    """Report label for a row: its defect class, else ``category:<c>``, else ``clean``."""
    if row["outcome"] == "clean":
        return "clean"
    return row["defect_class"] or f"category:{row['category']}"


def clear(
    candidate: Candidate, guard: SplitGuard, *, allow_unattested: bool = False
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """Run every gate. Returns ``(row, None)`` or ``(None, refusal)`` listing all failures."""
    reasons = list(candidate.refusals)
    verdict = guard.check(candidate.task_id, candidate.task_digest)
    if not verdict.allowed:
        reasons.append(("split_guard", "; ".join(verdict.reasons)))
    status = candidate.attestation.get("status")
    attested = status in ("matched", "not_applicable")
    if status == "mismatch":
        reasons.append(("model_mismatch", str(candidate.attestation.get("reason"))))
    elif not attested and not allow_unattested:
        reasons.append(("model_unattested", str(candidate.attestation.get("reason"))))
    if candidate.execution.get("confirmed") is not True or not candidate.execution.get("evidence"):
        problems = candidate.execution.get("problems") or ["no execution evidence"]
        reasons.append(("no_execution_confirmation", "; ".join(map(str, problems))))
    if reasons:
        return None, Refusal(
            candidate.source, candidate.task_id, candidate.outcome, tuple(reasons)
        )
    name = verdict.task_name
    body = candidate.body_sha256
    row = {
        "schema": ROW_SCHEMA,
        "row_id": _sha(f"{name}|{body}".encode())[:32],
        "family": candidate.family,
        "task": {"task_id": candidate.task_id, "task_name": name,
                 "task_digest": candidate.task_digest},
        "outcome": candidate.outcome,
        "defect_class": candidate.defect_class,
        "category": candidate.category,
        "target": candidate.target,
        "finding": candidate.finding,
        "body_sha256": body,
        "packet": candidate.packet,
        "turns": candidate.turns,
        "source": candidate.source,
        "clearance": {
            "schema": CLEARANCE_SCHEMA,
            "guard": {
                "scope": guard.scope, "status": verdict.status, "task_name": name,
                "registered": [list(pair) for pair in verdict.registered],
                "digest_checked": candidate.task_digest is not None,
                "sources": guard.sources,
            },
            "execution": candidate.execution,
            "model": {**candidate.attestation, "unattested_allowed": status not in ("matched", "not_applicable")},
            "dedup": {"key": candidate.dedup_key, "body_sha256": body},
            "exporter": {"name": "qa.env_export", "version": EXPORTER_VERSION},
        },
    }
    return row, None


@dataclass
class ExportResult:
    rows: list[dict[str, Any]]
    refusals: list[Refusal]
    guard: SplitGuard
    allow_unattested: bool
    candidates: int

    def report(self) -> dict[str, Any]:
        primary = Counter(r.primary for r in self.refusals)
        every = Counter(code for r in self.refusals for code, _ in r.reasons)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            groups[_class_label(row)].append(row)
        per_class = {
            label: {"rows": len(rows), "distinct_tasks": len({r["task"]["task_name"] for r in rows})}
            for label, rows in sorted(groups.items())
        }
        return {
            "schema": REPORT_SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "exporter_version": EXPORTER_VERSION,
            "guard": {"scope": self.guard.scope, **self.guard.sources},
            "allow_unattested": self.allow_unattested,
            "candidates": self.candidates,
            "rows": len(self.rows),
            "distinct_tasks": len({r["task"]["task_name"] for r in self.rows}),
            "distinct_task_body_pairs": len({r["clearance"]["dedup"]["key"] for r in self.rows}),
            "per_class": per_class,
            "per_outcome": dict(sorted(Counter(r["outcome"] for r in self.rows).items())),
            "per_family": dict(sorted(Counter(r["family"] for r in self.rows).items())),
            "rows_by_model_status": dict(sorted(Counter(
                r["clearance"]["model"]["status"] for r in self.rows).items())),
            "refused": len(self.refusals),
            "refused_by_primary_reason": dict(sorted(primary.items())),
            "refused_by_any_reason": dict(sorted(every.items())),
            "refused_by_family": dict(sorted(Counter(
                r.source.get("family", "?") for r in self.refusals).items())),
        }


def export(
    candidates: Iterable[Candidate | Refusal],
    guard: SplitGuard,
    *,
    allow_unattested: bool = False,
) -> ExportResult:
    """Gate every candidate, then keep the first row for each distinct ``(task, body)``."""
    rows: list[dict[str, Any]] = []
    refusals: list[Refusal] = []
    seen: dict[str, str] = {}
    total = 0
    for item in candidates:
        total += 1
        if isinstance(item, Refusal):
            refusals.append(item)
            continue
        row, refusal = clear(item, guard, allow_unattested=allow_unattested)
        if refusal is not None:
            refusals.append(refusal)
            continue
        assert row is not None
        key = row["clearance"]["dedup"]["key"]
        if key in seen:
            refusals.append(Refusal(
                row["source"], row["task"]["task_id"], row["outcome"],
                (("duplicate_body", f"same (task, body) as {seen[key]}"),),
            ))
            continue
        seen[key] = row["row_id"]
        rows.append(row)
    rows.sort(key=lambda r: r["row_id"])
    return ExportResult(rows, refusals, guard, allow_unattested, total)


def write_export(result: ExportResult, out_dir: Path) -> dict[str, Any]:
    """Write ``rows.jsonl``, ``refusals.jsonl`` and ``report.json`` into a new directory."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ExportError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rows.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in result.rows),
        encoding="utf-8")
    (out_dir / "refusals.jsonl").write_text(
        "".join(json.dumps(r.as_record(), sort_keys=True, ensure_ascii=False) + "\n"
                for r in result.refusals), encoding="utf-8")
    report = result.report()
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


# -- adapter: qa env audit sessions ----------------------------------------------------


def _clip(value: Any, limit: int) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return {"text": text[:limit], "chars": len(text), "sha256": _sha(text.encode()),
            "clipped": len(text) > limit}


def journal_turns(
    agent_dir: Path, clip_chars: int = OBSERVATION_CLIP_CHARS
) -> list[dict[str, Any]]:
    """Turns from a session journal: model reply, calls, clipped observations, final."""
    journal = Path(agent_dir) / "journal.jsonl"
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for event in read_jsonl(journal) if journal.is_file() else []:
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "model_turn":
            prompt = None
            for directory in (event.get("directory"), None):
                path = Path(directory) / "prompt.txt" if directory else (
                    Path(agent_dir) / "model" / f"turn-{int(event.get('invocation') or 0):03d}" / "prompt.txt"
                )
                if path.is_file():
                    prompt = _sha(path.read_bytes())
                    break
            current = {"invocation": event.get("invocation"), "prompt_sha256": prompt,
                       "response": event.get("response"), "usage": event.get("usage"),
                       "calls": [], "observations": [], "final": None}
            turns.append(current)
        elif current is None:
            continue
        elif kind == "action":
            current["calls"] = event.get("calls") or []
        elif kind == "tool_call":
            current["observations"].append({
                "tool": event.get("tool"), "args": event.get("args"),
                "observation": _clip(event.get("observation"), clip_chars),
            })
        elif kind == "final":
            current["final"] = event.get("final")
    return turns


def witness_evidence(
    run_dir: Path, finding: dict[str, Any], task_digest: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-validate each ``witness:<id>`` a finding cites against this run's observations.

    A witness counts only if its receipt exists in the run, is bound to this task digest and a
    runtime tool, and measured something. The recording host already checks this; the export does
    not trust a stored finding's claim that it did.
    """
    evidence: list[dict[str, Any]] = []
    problems: list[str] = []
    refs = [ref for ref in finding.get("evidence_refs") or [] if str(ref).startswith("witness:")]
    if not refs:
        return [], ["source-only finding: no witness:<id> evidence"]
    for ref in refs:
        match = _WITNESS.fullmatch(str(ref))
        if match is None:
            problems.append(f"{ref}: malformed witness id")
            continue
        path = Path(run_dir) / "observations" / f"{match[1]}.json"
        if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
            problems.append(f"{ref}: no receipt in this run")
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{ref}: unreadable receipt")
            continue
        if receipt.get("id") != match[1] or receipt.get("tool") not in RUNTIME_TOOLS:
            problems.append(f"{ref}: receipt is not a runtime-tool witness")
        elif task_digest is not None and receipt.get("task_digest") != task_digest:
            problems.append(f"{ref}: receipt belongs to another task digest")
        elif receipt.get("execution_status") != "measured":
            problems.append(f"{ref}: execution_status={receipt.get('execution_status')!r}")
        else:
            result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            evidence.append({
                "kind": "measured_witness", "witness": ref, "tool": receipt["tool"],
                "reward": result.get("reward"),
                "observed_native_rewards": result.get("observed_native_rewards"),
                "receipt_sha256": _file_sha(path),
            })
    return evidence, problems


def _finding_class(finding: dict[str, Any]) -> tuple[str | None, str | None]:
    for item in finding.get("root_cause") or []:
        if item in CLASS_CATEGORY:
            return item, CLASS_CATEGORY[item]
    return None, finding.get("category")


def audit_session_candidates(
    root: Path, *, clip_chars: int = OBSERVATION_CLIP_CHARS
) -> Iterator[Candidate | Refusal]:
    """Candidates from every ``qa env audit`` session below ``root`` (v1 and v2 layouts)."""
    for session_json in sorted(Path(root).rglob("session.json")):
        if session_json.parent.name != "agent":
            continue
        try:
            record = json.loads(session_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        schema = str(record.get("schema", ""))
        if not schema.startswith("qa.env.audit"):
            continue
        agent_dir = session_json.parent
        run_dir = agent_dir.parent
        source = {"family": "audit_session", "path": str(session_json), "schema": schema,
                  "model": record.get("model"), "lane": record.get("lane"),
                  "status": record.get("status") or record.get("stop")}
        identity = record.get("task_identity") or {}
        task_id = identity.get("task_id") or record.get("task_id") or record.get("task")
        digest = identity.get("task_digest") or record.get("task_digest")
        findings = sorted((run_dir / "findings").glob("*.json"))
        if not findings or not task_id:
            yield Refusal(source, task_id, None, (
                ("no_recorded_finding", "session ended with no recorded finding"),))
            continue
        attestation = attest_audit_session(agent_dir)
        turns = journal_turns(agent_dir, clip_chars)
        for path in findings:
            finding_source = {**source, "finding_path": str(path)}
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                Finding.model_validate(raw)
            except (OSError, ValueError, ValidationError) as exc:
                yield Refusal(finding_source, task_id, "finding",
                              (("invalid_finding", f"{type(exc).__name__}"),))
                continue
            refusals: list[tuple[str, str]] = []
            if raw.get("verdict") != "fail":
                refusals.append(("no_execution_confirmation", f"verdict={raw.get('verdict')!r}"))
            if (raw.get("refutation") or {}).get("status") == "refuted":
                refusals.append(("refuted", str((raw.get("refutation") or {}).get("reason"))))
            evidence, problems = witness_evidence(
                run_dir, raw, (raw.get("subject") or {}).get("task_digest") or digest)
            if raw.get("measurement") != "measured" and evidence:
                problems.append(f"measurement={raw.get('measurement')!r} despite a witness")
                evidence = []
            defect_class, category = _finding_class(raw)
            yield Candidate(
                family="audit_session", source=finding_source, task_id=task_id,
                task_digest=(raw.get("subject") or {}).get("task_digest") or digest,
                outcome="finding",
                execution={"confirmed": bool(evidence) and not problems, "kind": "measured_witness",
                           "evidence": evidence, "problems": problems},
                attestation=attestation,
                target={"verdict": "fail", "class": defect_class, "category": category,
                        "summary": raw.get("summary"),
                        "evidence_refs": list(raw.get("evidence_refs") or [])},
                defect_class=defect_class, category=category, finding=raw,
                packet={"task_id": task_id, "task_digest": digest, "stage": raw.get("stage"),
                        "tools_offered": _tools_offered(agent_dir)},
                turns=turns, refusals=refusals,
            )


def _tools_offered(agent_dir: Path) -> list[str]:
    journal = Path(agent_dir) / "journal.jsonl"
    for event in read_jsonl(journal)[:3] if journal.is_file() else []:
        if isinstance(event, dict) and event.get("event") == "session_started":
            return list(event.get("tools") or [])
    return []


# -- adapter: reward-seeker runs -------------------------------------------------------


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
    return "\n".join(parts)


def transcript_turns(
    path: Path, clip_chars: int = OBSERVATION_CLIP_CHARS
) -> tuple[str | None, list[dict[str, Any]]]:
    """``(first user message, turns)`` from an OMP stream transcript (``message_end`` events)."""
    packet: str | None = None
    turns: list[dict[str, Any]] = []
    for event in read_jsonl(path):
        if not isinstance(event, dict) or event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        role = message.get("role")
        if role == "user" and packet is None:
            packet = _text_parts(message.get("content"))[:PACKET_CLIP_CHARS]
        elif role == "assistant":
            calls = [
                {"tool": part.get("name"), "args": _clip(part.get("arguments"), clip_chars)}
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "toolCall"
            ]
            turns.append({"role": "assistant", "text": _text_parts(message.get("content")),
                          "calls": calls})
        elif role == "toolResult":
            turns.append({"role": "tool", "tool": message.get("toolName"),
                          "observation": _clip(_text_parts(message.get("content")), clip_chars)})
    return packet, turns


def _replay_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def seeker_replay(root: Path, driver: str, task: str, digest: str | None) -> dict[str, Any]:
    """Find a run's upstream and port replay records; supports the v0 and v3+ layouts."""
    root = Path(root)
    found: dict[str, Any] = {"upstream": None, "port": None}
    for side in ("upstream", "port"):
        candidate = _replay_json(root / "replay" / f"{driver}-{side}" / "candidate.json")
        path_hint = f"submissions/{driver}/{task}"
        if candidate is not None and str((candidate.get("input") or {}).get("path", "")).endswith(path_hint):
            found[side] = {
                "layout": "candidate", "status": candidate.get("status"),
                "reward": candidate.get("reward"), "executed": candidate.get("executed"),
                "error": candidate.get("error"), "backend": candidate.get("backend"),
                "grader": candidate.get("grader"),
                "task": candidate.get("task"),
                "path": str(root / "replay" / f"{driver}-{side}" / "candidate.json"),
            }
    if found["upstream"] is None and digest:
        record = _replay_json(root / "replay" / "upstream" / f"{digest.removeprefix('sha256:')}.json")
        result = (record or {}).get("result")
        if isinstance(result, dict):
            found["upstream"] = {
                "layout": "digest", "status": result.get("status"), "reward": result.get("reward"),
                "executed": result.get("executed"), "error": result.get("error"),
                "backend": result.get("backend"), "grader": "upstream", "task": result.get("task"),
                "path": str(root / "replay" / "upstream" / f"{digest.removeprefix('sha256:')}.json"),
            }
    if found["port"] is None:
        record = _replay_json(root / "replay" / "port" / f"{driver}-{task}.json")
        if record is not None:
            found["port"] = {
                "layout": "static_extract",
                "status": "measured" if record.get("error") is None and record.get("reward") is not None else "unmeasured",
                "reward": record.get("reward"), "executed": None, "error": record.get("error"),
                "backend": record.get("mode"), "grader": "port", "task": None,
                "path": str(root / "replay" / "port" / f"{driver}-{task}.json"),
            }
    return found


def seeker_confirmation(replay: dict[str, Any]) -> tuple[bool, list[str]]:
    """Differential confirmation: upstream measured 1.0 AND port measured 0.0, both exact."""
    problems: list[str] = []
    for side, want in (("upstream", 1.0), ("port", 0.0)):
        record = replay.get(side)
        if record is None:
            problems.append(f"no {side} replay")
        elif record.get("status") != "measured" or record.get("error") is not None:
            problems.append(
                f"{side} replay {record.get('status')!r}"
                + (f" ({str(record['error'])[:80]})" if record.get("error") else ""))
        elif record.get("reward") != want or isinstance(record.get("reward"), bool):
            problems.append(f"{side} reward {record.get('reward')!r}, need {want}")
    return not problems, problems


def seeker_candidates(
    root: Path, *, clip_chars: int = OBSERVATION_CLIP_CHARS
) -> Iterator[Candidate | Refusal]:
    """Candidates from ``<root>/runs/<driver>/<task>.json`` seeker records and their replays."""
    for run_path in sorted((Path(root) / "runs").glob("*/*.json")):
        run = _replay_json(run_path)
        if run is None or "driver" not in run or "task" not in run:
            continue
        driver, task = str(run["driver"]), str(run["task"])
        source = {"family": "seeker", "path": str(run_path), "driver": driver,
                  "model": run.get("model"), "root": str(root)}
        notes_path = Path(root) / "submissions" / driver / task / "notes.json"
        notes = (_replay_json(notes_path) or (run.get("validation") or {}).get("notes") or {})
        digest = (run.get("validation") or {}).get("digest")
        replay = seeker_replay(root, driver, task, digest)
        confirmed, problems = seeker_confirmation(replay)
        if run.get("error") or not (run.get("validation") or {}).get("valid", True):
            problems = [f"run error: {run.get('error') or run['validation'].get('problems')}", *problems]
            confirmed = False
        if not notes.get("exploit_found"):
            problems.append("no exploit claimed in notes.json")
            confirmed = False
        up_task = (replay["upstream"] or {}).get("task") or {}
        task_id = up_task.get("id") or task
        task_digest = up_task.get("digest")
        workspace = Path(str(run.get("workspace") or ""))
        transcript = workspace / "transcript.jsonl"
        if transcript.is_file():
            attestation = attest_transcript(run.get("model"), transcript)
            packet_text, turns = transcript_turns(transcript, clip_chars)
        else:
            attestation = attest_model(run.get("model"), [])
            attestation["reason"] = "no retained transcript beside the run record"
            packet_text, turns = None, [
                {"role": "assistant", "text": str(run.get("text") or ""),
                 "calls": [{"tool": str(call).split(":", 1)[0], "args": _clip(call, clip_chars)}
                           for call in run.get("tool_calls") or []]}]
        defect_class = notes.get("class") if notes.get("class") in CLASS_CATEGORY else None
        category = CLASS_CATEGORY.get(defect_class) if defect_class else None
        evidence = [
            {"kind": "differential_replay", "side": side, **{
                k: replay[side].get(k) for k in ("status", "reward", "backend", "grader", "path", "layout")}}
            for side in ("upstream", "port") if replay.get(side)
        ] if confirmed else []
        yield Candidate(
            family="seeker", source=source, task_id=task_id, task_digest=task_digest,
            outcome="finding",
            execution={"confirmed": confirmed, "kind": "differential_replay",
                       "evidence": evidence, "problems": problems},
            attestation=attestation,
            target={"verdict": "fail", "class": defect_class, "category": category,
                    "summary": notes.get("mechanism"),
                    "target_check": notes.get("target_check"),
                    "files_written": notes.get("files_written"),
                    "intended_work_skipped": notes.get("intended_work_skipped"),
                    "evidence_refs": [f"replay:{e['side']}" for e in evidence]},
            defect_class=defect_class, category=category,
            packet={"task_id": task_id, "task_digest": task_digest, "prompt": packet_text,
                    "hackbook_entry_ids": run.get("hackbook_entry_ids"),
                    "submission_digest": digest},
            turns=turns,
        )


# -- adapter: executed-clean package manifests -----------------------------------------


def executed_clean_candidates(manifest_path: Path) -> Iterator[Candidate | Refusal]:
    """Clean rows from a ``mimo.executed-clean/v1`` manifest (strict repeat-3 controls)."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    for member in manifest.get("members") or []:
        task_id = member.get("task_id")
        source = {"family": "executed_clean", "path": str(manifest_path),
                  "record_id": member.get("record_id"), "cohort": member.get("cohort"),
                  "dataset_id": manifest.get("dataset_id")}
        controls = member.get("controls") or {}
        reference, nop = controls.get("reference") or [], controls.get("nop") or []
        problems: list[str] = []
        if len(reference) < 3 or not all(v == 1.0 and not isinstance(v, bool) for v in reference):
            problems.append(f"reference controls {reference}, need repeated 1.0")
        if len(nop) < 3 or not all(v == 0.0 and not isinstance(v, bool) for v in nop):
            problems.append(f"nop controls {nop}, need repeated 0.0")
        refusals: list[tuple[str, str]] = []
        if member.get("heldout_excluded_from_training"):
            refusals.append(("split_guard", "manifest flags this package as held out"))
        digest = member.get("package_digest")
        yield Candidate(
            family="executed_clean", source=source, task_id=task_id, task_digest=digest,
            outcome="clean",
            execution={"confirmed": not problems, "kind": "executed_controls",
                       "evidence": [{"kind": "executed_controls", "reference": reference,
                                     "nop": nop}] if not problems else [],
                       "problems": problems},
            attestation={"status": "not_applicable",
                         "reason": "label comes from executed controls; no model produced it"},
            target={"verdict": "clean", "class": None, "category": None, "findings": [],
                    "summary": "clean: strict repeat controls passed on this exact package",
                    "package_digest": digest,
                    "not_semantic_certification": bool(manifest.get("not_semantic_certification", True))},
            packet={"task_id": task_id, "package_digest": digest,
                    "harbor_digest": member.get("harbor_digest"),
                    "verifier_digest": member.get("verifier_digest"), "cohort": member.get("cohort")},
            refusals=refusals,
        )


# -- CLI -------------------------------------------------------------------------------


def _export_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        guard = SplitGuard.from_repo(
            args.repo_root or root, scope=args.guard_scope, refuse_ids_file=args.refuse_ids)

        def sources() -> Iterator[Candidate | Refusal]:
            for path in args.audit_root:
                yield from audit_session_candidates(path, clip_chars=args.clip_chars)
            for path in args.seeker_root:
                yield from seeker_candidates(path, clip_chars=args.clip_chars)
            for path in args.executed_clean:
                yield from executed_clean_candidates(path)

        result = export(sources(), guard, allow_unattested=args.allow_unattested)
        report = write_export(result, args.out)
    except (OSError, ValueError) as exc:
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "setup_error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _attest_command(args: argparse.Namespace, **_: Any) -> int:
    if args.session is not None:
        attestation = attest_audit_session(args.session)
    elif args.transcript is not None and args.requested:
        attestation = attest_transcript(args.requested, args.transcript)
    else:
        print("attest needs --session, or --transcript with --requested", file=sys.stderr)
        return 2
    print(json.dumps(attestation, indent=2, sort_keys=True))
    ok = attestation["status"] == "matched" or (
        args.allow_unreported and attestation["status"] == "unreported")
    if not ok:
        print(f"qa env attest: {attestation['status']}: {attestation['reason']}", file=sys.stderr)
    return 0 if ok else 1


def add_export_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register ``export-sft`` and ``attest`` inside the ``evallab qa env`` namespace."""
    export_cmd = subparsers.add_parser(
        "export-sft",
        help="Export execution-confirmed, model-attested, split-guarded teacher rows",
        description=(
            "Only runs ending in an execution-confirmed finding or an execution-confirmed clean "
            "become rows. Refuses reported-split tasks, mismatched models and duplicate "
            "(task, body) pairs; writes rows.jsonl, refusals.jsonl and report.json."
        ),
    )
    export_cmd.add_argument("--audit-root", type=Path, action="append", default=[],
                            help="Directory tree of qa env audit sessions (repeatable)")
    export_cmd.add_argument("--seeker-root", type=Path, action="append", default=[],
                            help="Seeker root with runs/, submissions/, replay/ (repeatable)")
    export_cmd.add_argument("--executed-clean", type=Path, action="append", default=[],
                            help="mimo.executed-clean/v1 manifest (repeatable)")
    export_cmd.add_argument("--out", type=Path, required=True, help="New output directory")
    export_cmd.add_argument("--guard-scope", choices=GUARD_SCOPES, default="reported")
    export_cmd.add_argument("--refuse-ids", type=Path,
                            help="File of extra task names or ids to refuse, one per line")
    export_cmd.add_argument("--allow-unattested", action="store_true",
                            help="Keep runs whose served model was never reported (never mismatches)")
    export_cmd.add_argument("--clip-chars", type=int, default=OBSERVATION_CLIP_CHARS)
    export_cmd.add_argument("--repo-root", type=Path, help="Checkout holding research/answer-keys")
    export_cmd.set_defaults(func=_export_command)

    attest = subparsers.add_parser(
        "attest", help="Compare a run's requested model with the model its transcript shows")
    attest.add_argument("--session", type=Path, help="A qa env audit session agent/ directory")
    attest.add_argument("--transcript", type=Path, help="An OMP stream or saved-session JSONL")
    attest.add_argument("--requested", help="Requested model id (with --transcript)")
    attest.add_argument("--allow-unreported", action="store_true")
    attest.set_defaults(func=_attest_command)


__all__ = [
    "Candidate", "ExportError", "ExportResult", "GuardVerdict", "Refusal", "SplitGuard",
    "add_export_commands", "audit_session_candidates", "clear", "executed_clean_candidates",
    "export", "seeker_candidates", "write_export",
]
