"""Live Supervisor adjudication gate: a measurable, digest-pinned reason to allow a real action.

Design: ``docs/live-supervisor.md`` (Safety: "Enable an action only after its precision is 0.9 or
better on adjudicated cases"). :class:`EvidenceGate` reads one evidence document produced from
adjudicated replay/live samples and answers whether **this exact** decision shape has earned the
right to act:

- the scope must match exactly on ``action``, ``detector_version``, ``subject_kind`` and
  ``reader_model`` (``None`` for rules-only decisions), and on ``trigger`` when the scope names
  one — unrelated jobs are never pooled into one precision number. ``subject_kind="*"`` and
  ``reader_model="*"`` are used only by the LS-3 adapter, whose artifact measures an action
  across subjects and across its reader models;
- ``true_positives / (true_positives + false_positives) >= min_precision`` (default 0.9) with at
  least ``min_samples`` adjudicated cases (default 20), and ``true_positives + false_positives``
  must equal the declared ``samples``;
- the evidence file's SHA256 must equal the pinned digest, so an edited or swapped evidence file
  fails closed instead of silently raising precision.

Everything is default-deny: a missing file, an unpinned digest, an unknown schema, no matching
scope, thin samples or low precision all return a refusal reason. The sample floor is
:data:`MIN_SAMPLES` (20 adjudicated cases); an action measured on fewer cases can never match.

:func:`adjudication_scopes` / :func:`gate_from_adjudication` read LS-3's adjudicated
``headline.json`` (per source, per arm) and keep the **worst** measured source per (arm, action),
so an action passes only when every measured source passed it; counts are never pooled.

Note from LS-3's results: the rules ``kill_and_mark_infra`` arm was wrong on all 15 replayed cases,
and its proposed "infra" stops need a *fallback-succeeded* check (did the retry on another backend
actually finish?) before that arm could be trusted. That check is not implemented here; the gate
refuses the arm on its measured precision alone.

Nothing here enables anything by itself; :class:`evallab.production.live_supervisor_act.LabActuator` still requires an operator
registration for the subject identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.live_supervisor import (
    ACTIONS,
    DETECTOR_VERSION,
    TRIGGERS,
    Decision,
    Observation,
)

GATE_SCHEMA = "live-supervisor-gate-evidence/v1"
#: LS-3's adjudication artifact: ``<dir>/headline.json`` with per-source, per-arm ``per_action``
#: counts (``tp``, ``fp``, ``n_decided``); see the ``ls-3`` lane.
ADJUDICATION_HEADLINE = "headline.json"
#: Sources inside the headline. Every source must pass on its own; counts are never pooled.
ADJUDICATION_SECTIONS = ("replay", "seeker_pilot")
#: Arms inside a section: rules-only decisions have no reader model, model arms any reader.
ADJUDICATION_ARMS = ("rules", "model")
GATE_EVIDENCE_FILE = "gate-evidence-v1.json"
GATE_DIGEST_FILE = "gate-evidence-v1.sha256"
#: An action may act only at this adjudicated precision and with at least this many cases.
MIN_PRECISION = 0.9
MIN_SAMPLES = 20


class GateEvidenceError(ValueError):
    """The evidence document is unreadable, off-schema or internally inconsistent."""


def evidence_digest(path: Path) -> str:
    """SHA256 of the evidence file's bytes (lowercase hex)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GateEvidenceError(f"{key} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Scope:
    """One measured decision shape and its adjudicated outcome counts."""

    action: str
    subject_kind: str
    detector_version: str
    reader_model: str | None
    trigger: str | None
    true_positives: int
    false_positives: int
    samples: int
    measured_at: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise GateEvidenceError(f"action {self.action!r} not in {ACTIONS}")
        if self.trigger is not None and self.trigger not in TRIGGERS:
            raise GateEvidenceError(f"trigger {self.trigger!r} not in {TRIGGERS}")
        if not self.subject_kind:
            raise GateEvidenceError("subject_kind is required")
        if self.reader_model == "":
            raise GateEvidenceError("reader_model must be a name, None (rules) or '*' (any)")
        if self.true_positives + self.false_positives != self.samples:
            raise GateEvidenceError(
                f"true_positives + false_positives must equal samples "
                f"({self.true_positives} + {self.false_positives} != {self.samples})")

    @property
    def precision(self) -> float:
        adjudicated = self.true_positives + self.false_positives
        return self.true_positives / adjudicated if adjudicated else 0.0

    def key(self) -> str:
        return (f"{self.action}|{self.detector_version}|{self.subject_kind}|"
                f"{self.reader_model if self.reader_model is not None else 'rules'}|"
                f"{self.trigger or '*'}")

    def matches(self, *, action: str, subject_kind: str, detector_version: str,
                reader_model: str | None, trigger: str | None) -> bool:
        if (self.action, self.detector_version) != (action, detector_version):
            return False
        if self.subject_kind != "*" and self.subject_kind != subject_kind:
            return False
        if self.reader_model != "*" and self.reader_model != reader_model:
            return False
        return self.trigger is None or self.trigger == trigger

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "subject_kind": self.subject_kind,
                "detector_version": self.detector_version, "reader_model": self.reader_model,
                "trigger": self.trigger, "true_positives": self.true_positives,
                "false_positives": self.false_positives, "samples": self.samples,
                "precision": round(self.precision, 4), "measured_at": self.measured_at,
                "source": self.source}


def parse_evidence(payload: Any) -> list[Scope]:
    """Validate one evidence document into scopes (raises :class:`GateEvidenceError`)."""
    if not isinstance(payload, dict):
        raise GateEvidenceError("evidence document must be an object")
    if payload.get("schema") != GATE_SCHEMA:
        raise GateEvidenceError(f"schema must be {GATE_SCHEMA}, got {payload.get('schema')!r}")
    rows = payload.get("scopes")
    if not isinstance(rows, list) or not rows:
        raise GateEvidenceError("scopes must be a non-empty list")
    scopes: list[Scope] = []
    for row in rows:
        if not isinstance(row, dict):
            raise GateEvidenceError("each scope must be an object")
        reader = row.get("reader_model")
        trigger = row.get("trigger")
        scopes.append(Scope(
            action=str(row.get("action") or ""),
            subject_kind=str(row.get("subject_kind") or ""),
            detector_version=str(row.get("detector_version") or ""),
            reader_model=None if reader is None else str(reader),
            trigger=None if trigger is None else str(trigger),
            true_positives=_int(row, "true_positives"),
            false_positives=_int(row, "false_positives"),
            samples=_int(row, "samples"),
            measured_at=str(row.get("measured_at") or ""),
            source=str(row.get("source") or ""),
        ))
    return scopes


def adjudication_scopes(headline: Mapping[str, Any], *,
                        sections: Sequence[str] = ADJUDICATION_SECTIONS) -> list[Scope]:
    """LS-3's adjudicated ``headline.json`` as evidence scopes, conservatively.

    Counts are **never pooled across sources**: for each (arm, action) the scope carries the
    *worst* measured source (lowest precision, then fewest cases), so the gate can only pass an
    action that every measured source passed. ``subject_kind="*"`` (any subject) and
    ``trigger=None`` because the artifact measures the action, not a subject kind or trigger;
    the model arm is written as ``"*"`` because the artifact aggregates its readers.
    """
    if not isinstance(headline, Mapping):
        raise GateEvidenceError("adjudication headline must be an object")
    worst: dict[tuple[str, str], Scope] = {}
    for section in sections:
        block = headline.get(section)
        if not isinstance(block, Mapping):
            continue
        for arm in ADJUDICATION_ARMS:
            stats = (block.get(arm) or {}).get("per_action")
            if not isinstance(stats, Mapping):
                continue
            for action, row in stats.items():
                if action not in ACTIONS or action == "continue" or not isinstance(row, Mapping):
                    continue
                scope = _scope_from_row(action, arm, section, row)
                if scope is None:
                    continue
                key = (arm, action)
                current = worst.get(key)
                if current is None or (scope.precision, scope.samples) < (
                        current.precision, current.samples):
                    worst[key] = scope
    return sorted(worst.values(), key=lambda s: s.key())


def _scope_from_row(action: str, arm: str, section: str,
                    row: Mapping[str, Any]) -> Scope | None:
    """One ``per_action`` row as a scope (``None`` when it is empty or inconsistent)."""
    try:
        tp = _int(row, "tp")
        fp = _int(row, "fp")
        decided = _int(row, "n_decided")
    except GateEvidenceError:
        return None
    if decided == 0 or tp + fp != decided:
        return None
    return Scope(action=action, subject_kind="*", detector_version=DETECTOR_VERSION,
                 reader_model=None if arm == "rules" else "*", trigger=None,
                 true_positives=tp, false_positives=fp, samples=decided,
                 measured_at=str(row.get("measured_at") or ""),
                 source=f"{section}/{arm}")


def write_adjudication_evidence(headline_path: Path, evidence_path: Path, *,
                                sections: Sequence[str] = ADJUDICATION_SECTIONS) -> str:
    """Convert an LS-3 headline into gate evidence; returns the digest to pin."""
    try:
        headline = json.loads(Path(headline_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateEvidenceError(f"cannot read {headline_path}: {type(exc).__name__}: {exc}") from exc
    return write_evidence(evidence_path, adjudication_scopes(headline, sections=sections))


def gate_from_adjudication(root: Path, headline_path: Path, *,
                           min_precision: float = MIN_PRECISION,
                           min_samples: int = MIN_SAMPLES) -> EvidenceGate:
    """Publish LS-3 evidence under ``root`` and return a gate pinned to it.

    A new measurement must be re-published (and therefore re-approved); an action that no source
    measured to ``min_precision`` over ``min_samples`` cases simply never matches and is refused.
    """
    root = Path(root)
    digest = write_adjudication_evidence(headline_path, root / GATE_EVIDENCE_FILE)
    (root / GATE_DIGEST_FILE).write_text(f"{digest}\n", encoding="utf-8")
    return EvidenceGate(root / GATE_EVIDENCE_FILE, expected_sha256=digest,
                        min_precision=min_precision, min_samples=min_samples)


def write_evidence(path: Path, scopes: Sequence[Scope]) -> str:
    """Write an evidence document and return its SHA256 (pin this digest in the gate)."""
    payload = {"schema": GATE_SCHEMA, "scopes": [s.as_dict() for s in scopes]}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_digest(path)


class EvidenceGate:
    """Allow an action only where adjudicated precision and sample count are both sufficient.

    ``expected_sha256`` is the pinned digest of ``path``; any change on disk fails closed.
    """

    def __init__(self, path: Path, *, expected_sha256: str, detector_version: str = DETECTOR_VERSION,
                 min_precision: float = MIN_PRECISION, min_samples: int = MIN_SAMPLES) -> None:
        self.path = Path(path)
        self.expected_sha256 = expected_sha256.strip().lower()
        self.detector_version = detector_version
        self.min_precision = min_precision
        self.min_samples = min_samples

    def refusal(self, observation: Observation, decision: Decision) -> str | None:
        """Why this decision may not act (``None`` when the evidence covers it)."""
        if not self.path.is_file():
            return f"gate evidence missing: {self.path}"
        try:
            digest = evidence_digest(self.path)
        except OSError as exc:
            return f"gate evidence unreadable: {type(exc).__name__}: {exc}"[:200]
        if digest != self.expected_sha256:
            return f"gate evidence changed: {digest} != pinned {self.expected_sha256}"
        try:
            scopes = parse_evidence(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            return f"gate evidence malformed: {type(exc).__name__}: {exc}"[:200]
        trigger = reason_trigger(decision.reason)
        matches = [s for s in scopes if s.matches(
            action=decision.action, subject_kind=observation.subject.kind,
            detector_version=self.detector_version, reader_model=decision.reader_model,
            trigger=trigger)]
        if not matches:
            return (f"gate scope not measured: {decision.action} on {observation.subject.kind} "
                    f"by {decision.reader_model or 'rules'} (detector {self.detector_version})")
        best = max(matches, key=lambda s: (s.precision, s.samples))
        if best.samples < self.min_samples:
            return (f"gate samples thin: {best.samples} < {self.min_samples} for {best.key()}")
        if best.precision < self.min_precision:
            return (f"gate precision low: {best.precision:.3f} < {self.min_precision:.2f} "
                    f"on {best.samples} adjudicated cases for {best.key()}")
        return None

    def __call__(self, observation: Observation, decision: Decision) -> bool:
        return self.refusal(observation, decision) is None


def reason_trigger(reason: str) -> str | None:
    """The trigger a rule-table reason names (``"<trigger>: ..."``), if any."""
    head, sep, _rest = reason.partition(":")
    return head if sep and head in TRIGGERS else None


def read_pinned_digest(root: Path) -> str | None:
    """The pinned digest written next to the actuator state (``gate-evidence-v1.sha256``)."""
    path = Path(root) / GATE_DIGEST_FILE
    try:
        text = path.read_text(encoding="utf-8").strip().split()
    except OSError:
        return None
    return text[0].lower() if text else None


class DeniedGate:
    """A gate that always refuses, with the reason (used when no evidence is pinned)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def refusal(self, observation: Observation, decision: Decision) -> str | None:
        del observation, decision
        return self.reason

    def __call__(self, observation: Observation, decision: Decision) -> bool:
        return False


def gate_for_root(root: Path, *, detector_version: str = DETECTOR_VERSION,
                  min_precision: float = MIN_PRECISION,
                  min_samples: int = MIN_SAMPLES) -> EvidenceGate | DeniedGate:
    """The gate an actuator should use for ``root``: evidence + pinned digest, else deny."""
    evidence = Path(root) / GATE_EVIDENCE_FILE
    digest = read_pinned_digest(root)
    if digest is None:
        return DeniedGate(f"gate evidence digest not pinned: write {Path(root) / GATE_DIGEST_FILE}")
    return EvidenceGate(evidence, expected_sha256=digest, detector_version=detector_version,
                        min_precision=min_precision, min_samples=min_samples)
