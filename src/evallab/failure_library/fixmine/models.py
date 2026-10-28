"""Pinned candidates and machine labels; neither alone is a published finding."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from evallab.failure_library.records import DefectClass, Direction, Split
from evallab.schemas import ContractModel


class Candidate(ContractModel):
    """One existing task changed by a non-merge upstream commit."""

    schema_version: Literal["fixmine/v1"] = "fixmine/v1"
    candidate_id: str
    repo_url: str
    repo_slug: str
    parent_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    fix_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    task_id: str
    task_path: str
    family: str
    changed_files: list[str]
    diff_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diff_ref: str
    commit_message: str
    committed_at: date
    split: Split
    license: str | None = None


class Judgment(ContractModel):
    """A classifier's answer about a single candidate, not proof of a defect."""

    candidate_id: str
    is_task_fix: Literal["yes", "no"]
    defect_class: DefectClass
    direction: Direction
    summary: str = Field(min_length=1, max_length=1000)


class Consensus(ContractModel):
    """Two-family acceptance with explicit class/direction disagreements."""

    candidate_id: str
    accepted: bool
    defect_class: DefectClass
    direction: Direction
    summary: str
    disagreement: bool
    models: list[str]
    label_refs: list[str]
