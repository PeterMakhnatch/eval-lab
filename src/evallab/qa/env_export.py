"""Stored QA candidate records, split guards, and strict repeat-control adapters."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from evallab.qa.agent_lanes import (
    read_jsonl,
)

REFUSAL_SCHEMA = "qa.teacher_refusal/v1"

#: Tools whose measured receipt is an executed witness; mirrors ``qa.env._RUNTIME_TOOLS``.
RUNTIME_TOOLS = frozenset({
    "run-upstream-grader", "run-port-grader", "run-reference", "run-blank", "probe",
})
#: Defect class -> ``qa.finding/v1`` category; mirrors ``qa.env_agent._CLASS_CATEGORY``.
#: Order in which a refused candidate's *primary* reason is chosen; every reason is still kept.
REASON_ORDER = (
    "split_guard", "model_mismatch", "model_unattested", "no_recorded_finding",
    "invalid_finding", "refuted", "no_execution_confirmation", "duplicate_body",
)
GUARD_SCOPES = ("reported", "tb4-heldout")
TB4_KEY = "tb4-audit-v1"
#: Per-observation character budget in exported turns; the full text is hashed, not lost.
OBSERVATION_CLIP_CHARS = 6000
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


# -- adapter: qa env audit sessions ----------------------------------------------------


# -- adapter: reward-seeker runs -------------------------------------------------------


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


