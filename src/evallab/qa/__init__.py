"""Shared QA contract for the task/trace/breaker agents (FOCUS plan)."""

from __future__ import annotations

from evallab.qa.answer_key import AnswerKey, load_answer_key, resolve_answer_key
from evallab.qa.models import (
    ANSWER_KEY_SCHEMA,
    FINDING_SCHEMA,
    AnswerKeyItem,
    Finding,
    FindingSubject,
    Refutation,
    Reproducer,
    Reviewer,
    dump_finding,
    load_finding,
    load_findings_dir,
    subject_key,
)
from evallab.qa.score import ScoreRow, append_scoreboard, score_findings

__all__ = [
    "ANSWER_KEY_SCHEMA",
    "FINDING_SCHEMA",
    "AnswerKey",
    "AnswerKeyItem",
    "Finding",
    "FindingSubject",
    "Refutation",
    "Reproducer",
    "Reviewer",
    "ScoreRow",
    "append_scoreboard",
    "dump_finding",
    "load_answer_key",
    "load_finding",
    "load_findings_dir",
    "resolve_answer_key",
    "score_findings",
    "subject_key",
]
