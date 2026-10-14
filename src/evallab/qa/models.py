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
from pathlib import Path
from typing import Literal

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
    can never carry a ``pass`` verdict.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["qa.finding/v1"] = Field(default=FINDING_SCHEMA, alias="schema")
    id: str | None = None
    subject: FindingSubject
    stage: str = Field(min_length=1)
    verdict: Verdict
    category: Category
    severity: Severity
    fault: Fault
    evidence_refs: list[str] = Field(default_factory=list)
    reproducer: Reproducer | None = None
    refutation: Refutation = Field(default_factory=Refutation)
    reviewer: Reviewer
    measurement: Measurement = "measured"

    @model_validator(mode="after")
    def _not_run_never_passes(self) -> Finding:
        if self.measurement == "not_run" and self.verdict == "pass":
            raise ValueError("a not_run check cannot pass; use verdict 'unknown'")
        return self


class AnswerKeyItem(ContractModel):
    """One labelled subject in an answer key (``items.jsonl`` row)."""

    subject: FindingSubject
    expected_category: Category | None = None
    expected_verdict: Verdict
    label_source: str = Field(min_length=1)
    split: str = Field(min_length=1)
    pair_id: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _fail_needs_category(self) -> AnswerKeyItem:
        if self.expected_verdict == "fail" and self.expected_category is None:
            raise ValueError("a fail-labelled item needs an expected_category")
        return self


def subject_key(subject: FindingSubject) -> tuple[str, str, str]:
    """Identity used to match findings to answer-key items."""
    return (subject.kind, subject.task_id, subject.trial_id or "")


def load_finding(path: Path | str) -> Finding:
    """Load and validate one ``qa.finding/v1`` JSON document."""
    return Finding.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load_findings_dir(directory: Path | str) -> list[Finding]:
    """Load every finding under a directory (``*.json`` files, ``*.jsonl`` lines)."""
    root = Path(directory)
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.json")):
        findings.append(load_finding(path))
    for path in sorted(root.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                findings.append(Finding.model_validate(json.loads(line)))
    return findings


def dump_finding(finding: Finding, path: Path | str) -> Path:
    """Write one finding as pretty JSON (the on-disk one-file-per-finding form)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(finding.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    return out
