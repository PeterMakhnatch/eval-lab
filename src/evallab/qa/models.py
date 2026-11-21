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

    kind: SubjectKind
    task_id: str = Field(min_length=1)
    task_digest: str | None = None
    trial_id: str | None = None


class Reproducer(ContractModel):

    kind: Literal["harbor_trial", "command", "patch"]
    path: str = Field(min_length=1)
    expected_reward: float | None = None


class Refutation(ContractModel):

    status: Literal["verified", "refuted", "unverified"] = "unverified"
    by: str | None = None
    reason: str | None = None


class Reviewer(ContractModel):

    agent: str = Field(min_length=1)
    model: str | None = None
    tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class Finding(ContractModel):

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["qa.finding/v1"] = Field(default=FINDING_SCHEMA, alias="schema")
    id: str | None = None
    subject: FindingSubject
    stage: str = Field(min_length=1)
    verdict: Verdict
    category: Category
    severity: Severity | None = None
    fault: Fault | None = None
    summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    reproducer: Reproducer | None = None
    refutation: Refutation = Field(default_factory=Refutation)
    reviewer: Reviewer
    measurement: Measurement = "measured"
    root_cause: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_run_never_passes(self) -> Finding:
        if self.measurement == "not_run" and self.verdict == "pass":
            raise ValueError("a not_run check cannot pass; use verdict 'unknown'")
        if self.verdict == "fail" and (self.severity is None or self.fault is None):
            raise ValueError("a fail finding needs a severity and a fault")
        return self


class AnswerKeyItem(ContractModel):

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: Literal["qa.answer_key/v1"] = Field(default=ANSWER_KEY_SCHEMA, alias="schema")
    item_id: str | None = None
    key_id: str | None = None
    subject: FindingSubject
    expected_category: Category | None = None
    expected_verdict: Verdict
    expected_fault: Fault | None = None
    expected_root_cause: str | dict[str, Any] | None = None
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

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pair_id: str | None = None
    parent: str = Field(min_length=1)
    child: str = Field(min_length=1)
    category: Category | None = None
    split: str | None = None


def subject_key(subject: FindingSubject) -> tuple[str, str, str]:
    return (subject.kind, subject.task_id, subject.trial_id or "")


_ROOT_CAUSE_ID = re.compile(r"[A-Za-z0-9_.\-]+")


def normalize_root_cause(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _root_cause_string(value: str) -> list[str]:
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
    return bool(_normalized(named) & expected)


def load_findings_dir(directory: Path | str) -> list[Finding]:
    root = Path(directory)

    def scan(pattern: str) -> list[Path]:
        return [p for p in sorted(root.rglob(pattern)) if not p.name.startswith("._")]

    documents: list[Any] = []
    for path in scan("*.json"):
        documents.append(json.loads(path.read_text(encoding="utf-8")))
    for path in scan("*.jsonl"):
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
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(finding.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    return out
