"""Shared QA contract: findings, answer keys, and scoring (`qa.*` v1).

One contract for the three QA agents (task auditor, trace auditor,
reward seeker). Findings are ``qa.finding/v1`` JSON documents, answer
keys are ``qa.answer_key/v1`` directories under
``research/answer-keys/<key_id>/``, and :func:`score_findings` grades a
findings directory against a key into the scoreboard CSV. See the shared
plan in ``research-context/FOCUS.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from evallab.schemas import ContractModel

FINDING_SCHEMA = "qa.finding/v1"
ANSWER_KEY_SCHEMA = "qa.answer_key/v1"

SubjectKind = Literal["task", "trace"]
Verdict = Literal["pass", "fail", "unknown"]
Category = Literal[
    "broken",
    "reachable",
    "tamperable",
    "weak",
    "reward_hacking",
    "false_positive",
    "false_negative",
    "prompt_misaligned",
    "failure_cause",
]
Severity = Literal["low", "medium", "high", "critical"]
Fault = Literal["agent", "eval", "platform"]
Measurement = Literal["measured", "ran_without_usable_result", "not_run"]


class FindingSubject(ContractModel):
    """What a finding is about: one task package or one trial trace."""

    kind: SubjectKind
    task_id: str = Field(min_length=1)
    task_digest: str | None = None
    trial_id: str | None = None


class Reproducer(ContractModel):
    """Executable proof for a finding: a Harbor trial, a command, or a patch."""

    kind: Literal["harbor_trial", "command", "patch"]
    path: str = Field(min_length=1)
    expected_reward: float | None = None


class Refutation(ContractModel):
    """Second-reviewer outcome; ambiguous findings stay ``refuted``-adjacent."""

    status: Literal["verified", "refuted", "unverified"] = "unverified"
    by: str | None = None
    reason: str | None = None


class Reviewer(ContractModel):
    """Who produced the finding and what it cost."""

    agent: str = Field(min_length=1)
    model: str | None = None
    tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class Finding(ContractModel):
    """One QA finding (``qa.finding/v1``).

    ``measurement`` separates measured evidence from a run that produced
    nothing usable and from a check that never ran; a ``not_run`` check
    can never carry a ``pass`` verdict. ``severity`` and ``fault`` describe
    a defect, so a ``fail`` finding must carry both; ``pass`` and
    ``unknown`` findings may leave them null.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["qa.finding/v1"] = Field(default=FINDING_SCHEMA, alias="schema")
    id: str | None = None
    subject: FindingSubject
    stage: str = Field(min_length=1)
    verdict: Verdict
    category: Category
    severity: Severity | None = None
    fault: Fault | None = None
    #: One-line human summary of the finding (optional).
    summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    reproducer: Reproducer | None = None
    refutation: Refutation = Field(default_factory=Refutation)
    reviewer: Reviewer
    measurement: Measurement = "measured"
    #: Root-cause ids the reviewer names for this finding, in the key's
    #: vocabulary (see :func:`expected_root_causes`). Optional: findings
    #: written before this field existed stay valid and simply name none.
    root_cause: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_run_never_passes(self) -> Finding:
        if self.measurement == "not_run" and self.verdict == "pass":
            raise ValueError("a not_run check cannot pass; use verdict 'unknown'")
        if self.verdict == "fail" and (self.severity is None or self.fault is None):
            raise ValueError("a fail finding needs a severity and a fault")
        return self


class AnswerKeyItem(ContractModel):
    """One labelled subject in an answer key (``items.jsonl`` row).

    Keys carry source-specific provenance columns (``cheat_prompted``,
    ``trial_ref``, ``expected_mechanisms``, ...), so extras are kept rather
    than rejected; the fields below are the scored contract.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: Literal["qa.answer_key/v1"] = Field(default=ANSWER_KEY_SCHEMA, alias="schema")
    item_id: str | None = None
    key_id: str | None = None
    subject: FindingSubject
    expected_category: Category | None = None
    expected_verdict: Verdict
    expected_fault: Fault | None = None
    expected_root_cause: str | dict[str, Any] | None = None
    #: Canonical root-cause ids for this item; takes precedence over the
    #: source-specific shapes :func:`expected_root_causes` also reads.
    expected_root_causes: list[str] = Field(default_factory=list)
    label_source: str = Field(min_length=1)
    split: str = Field(min_length=1)
    role: str | None = None
    pair_ids: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _fail_needs_category(self) -> AnswerKeyItem:
        if self.expected_verdict == "fail" and self.expected_category is None:
            raise ValueError("a fail-labelled item needs an expected_category")
        return self


class KeyPair(ContractModel):
    """A clean parent and its faulty child (``pairs.jsonl`` row), by item id."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pair_id: str | None = None
    parent: str = Field(min_length=1)
    child: str = Field(min_length=1)
    category: Category | None = None
    split: str | None = None


def subject_key(subject: FindingSubject) -> tuple[str, str, str]:
    """Identity used to match findings to answer-key items."""
    return (subject.kind, subject.task_id, subject.trial_id or "")


_ROOT_CAUSE_ID = re.compile(r"[A-Za-z0-9_.\-]+")


def normalize_root_cause(value: str) -> str:
    """Canonical form for root-cause comparison: ``Proxy-check too weak`` -> ``proxy_check_too_weak``."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _root_cause_string(value: str) -> list[str]:
    # "scheme: a,b" lists ids; "a:b:c" keeps the innermost; free text after
    # a colon ("weak_hidden_tests: hidden tests use ...") keeps the scheme.
    if ":" not in value:
        return [value]
    head, tail = value.split(":", 1)
    if ":" in tail:
        return _root_cause_string(tail)
    parts = [part.strip() for part in tail.split(",")]
    if parts and all(_ROOT_CAUSE_ID.fullmatch(part) for part in parts):
        return parts
    return [head]


def _normalized(values: Iterable[str]) -> frozenset[str]:
    return frozenset(token for token in map(normalize_root_cause, values) if token)


def expected_root_causes(item: Mapping[str, Any]) -> frozenset[str]:
    """Normalized root-cause ids a key item expects (empty: none labelled).

    Reads the first non-empty source, in order: ``expected_root_causes``
    (canonical list); ``expected_mechanisms`` (breaker-bench); ``root_cause``
    records (QA Task keys: each record's ``id`` and ``source_class`` both
    count); ``expected_root_cause`` as a dict (trajectory-judge:
    ``failure_type``) or a string (``"cheat_ladder: a,b"`` -> ``a``, ``b``;
    ``"upstream_fetch"`` -> itself).
    """
    if canonical := item.get("expected_root_causes"):
        return _normalized(canonical)
    if mechanisms := item.get("expected_mechanisms"):
        return _normalized(mechanisms)
    if records := item.get("root_cause"):
        return _normalized(
            str(record[field])
            for record in records
            if isinstance(record, Mapping)
            for field in ("id", "source_class")
            if record.get(field)
        )
    cause = item.get("expected_root_cause")
    if isinstance(cause, Mapping):
        return _normalized([str(cause["failure_type"])] if cause.get("failure_type") else [])
    if isinstance(cause, str):
        return _normalized(_root_cause_string(cause))
    return frozenset()


def root_cause_hit(named: Iterable[str], expected: frozenset[str]) -> bool:
    """True when any named root cause matches an expected one (normalized)."""
    return bool(_normalized(named) & expected)


def load_finding(path: Path | str) -> Finding:
    """Load and validate one ``qa.finding/v1`` JSON document."""
    return Finding.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load_findings_dir(directory: Path | str) -> list[Finding]:
    """Load the findings under a directory (``*.json`` files, ``*.jsonl`` lines).

    Findings identify themselves: only documents whose ``schema`` is
    ``qa.finding/v1`` load; other JSON a producer keeps beside them (reports,
    manifests, summaries) is skipped. A tagged document that fails validation
    raises. Byte-identical documents (the same finding published twice) load
    once; point the scan at one copy of each finding when versions differ.
    """
    root = Path(directory)
    documents: list[Any] = []
    for path in sorted(root.rglob("*.json")):
        documents.append(json.loads(path.read_text(encoding="utf-8")))
    for path in sorted(root.rglob("*.jsonl")):
        documents.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    findings: list[Finding] = []
    seen: set[str] = set()
    for document in documents:
        if not isinstance(document, dict) or document.get("schema") != FINDING_SCHEMA:
            continue
        canonical = json.dumps(document, sort_keys=True)
        if canonical in seen:
            continue
        seen.add(canonical)
        findings.append(Finding.model_validate(document))
    return findings


def dump_finding(finding: Finding, path: Path | str) -> Path:
    """Write one finding as pretty JSON (the on-disk one-file-per-finding form)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(finding.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    return out
