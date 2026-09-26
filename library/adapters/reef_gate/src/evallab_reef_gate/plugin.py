"""Reef-process adapter for the paired sign-test publication rule."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import reef
from reef.core.evaluation import (
    CandidateEvaluationPlugin,
    CandidateEvaluator,
    EvaluationResult,
    SelectionDecision,
    UpdateCandidate,
)
from reef.train.cordis_backend.backend import HarnessCandidate
from reef.train.evaluation.evaluators import BackendEvaluateMixin, CandidatePluginFactory

from .lab import LabConfig, LabEvaluator
from .rules import GateConfig, decide_pairs

CONFIG_ENV = "EVALLAB_REEF_GATE_CONFIG"
METADATA_KEY = "evallab_reef_gate"
POLICY = "paired_sign_test"
POLICY_VERSION = "1"
RULE_KEYS = frozenset({
    "alpha", "min_valid_pairs", "pass_threshold", "regression_failure_threshold",
})


def reef_source_identity(expected: str | None) -> tuple[Path, str]:
    """Identify the actual clean checkout supplying the imported Reef runtime."""
    if reef.__file__ is None:
        raise ValueError("Reef source checkout identity is unavailable")
    package = Path(reef.__file__).resolve().parent
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(package), "rev-parse", "HEAD", "--show-toplevel"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    revision, checkout_text = result.stdout.strip().splitlines()
    checkout = Path(checkout_text).resolve()
    if checkout / "reef" != package or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("imported Reef is not bound to an identifiable source checkout")
    if expected is not None and expected != revision:
        raise ValueError(f"Reef revision differs from the gate configuration: {revision}")
    dirty = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(checkout), "diff", "--quiet", "HEAD", "--", "reef", "tutorials"],
        check=False, capture_output=True, timeout=5,
    )
    if dirty.returncode != 0:
        raise ValueError("Reef runtime or tutorial sources differ from their recorded commit")
    return checkout, revision


class Factory(CandidatePluginFactory):
    """Configured by an optional JSON file, or explicit Python constructor values."""

    def __init__(
        self,
        *,
        config: GateConfig | None = None,
        record_dir: Path | None = None,
        reef_commit: str | None = None,
        lab: LabConfig | None = None,
    ) -> None:
        raw: dict[str, Any] = {}
        config_path = os.environ.get(CONFIG_ENV)
        if config_path:
            loaded = json.loads(Path(config_path).read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{CONFIG_ENV} must name a JSON object")
            raw = loaded
        unknown = set(raw) - RULE_KEYS - {"record_dir", "reef_commit", "lab"}
        if unknown:
            raise ValueError("unknown gate configuration keys: " + ", ".join(sorted(unknown)))
        self.config = config if config is not None else GateConfig(
            **{key: value for key, value in raw.items() if key in RULE_KEYS}
        )
        configured_dir = raw.get("record_dir")
        if configured_dir is not None and not isinstance(configured_dir, str):
            raise ValueError("record_dir must be a path string")
        self.record_dir = (
            Path(record_dir) if record_dir is not None
            else Path(configured_dir) if configured_dir else Path.cwd() / "runs" / "reef-gate-decisions"
        ).resolve()
        expected_revision = reef_commit if reef_commit is not None else raw.get("reef_commit")
        if expected_revision is not None and (
            not isinstance(expected_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", expected_revision) is None
        ):
            raise ValueError("reef_commit must be a complete lowercase Git commit")
        self.reef_root, self.reef_commit = reef_source_identity(expected_revision)
        if self.record_dir.is_relative_to(self.reef_root):
            raise ValueError("gate records must not modify the Reef checkout or its environment")
        lab_raw = raw.get("lab")
        if lab_raw is not None and not isinstance(lab_raw, dict):
            raise ValueError("lab must be a configuration object")
        self.lab = lab if lab is not None else (
            LabConfig.from_dict(lab_raw) if lab_raw is not None else None
        )
        if self.lab is not None and (
            self.lab.record_dir.is_relative_to(self.reef_root)
            or self.lab.lab_root.is_relative_to(self.reef_root)
        ):
            raise ValueError("Lab evaluation must not write inside the Reef checkout")
        self.record_dir.mkdir(parents=True, exist_ok=True)

    def build(self, candidate_backend: CandidateEvaluator) -> CandidateEvaluationPlugin:
        return GatePlugin(
            candidate_backend,
            config=self.config,
            record_dir=self.record_dir,
            reef_commit=self.reef_commit,
            lab=self.lab,
        )


class GatePlugin(BackendEvaluateMixin):
    """Use native Reef or configured Lab evidence with the same publication rule."""

    def __init__(
        self,
        candidate_backend: CandidateEvaluator,
        *,
        config: GateConfig,
        record_dir: Path,
        reef_commit: str,
        lab: LabConfig | None = None,
    ) -> None:
        super().__init__()
        self._candidate_backend = candidate_backend
        self.config = config
        self.record_dir = record_dir
        self.reef_commit = reef_commit
        self.lab_evaluator = LabEvaluator(lab) if lab is not None else None

    def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
        started = time.perf_counter()
        try:
            if self.lab_evaluator is not None:
                if not isinstance(candidate, HarnessCandidate):
                    raise ValueError("Lab evaluation requires a HarnessCandidate")
                result = self.lab_evaluator.evaluate(
                    candidate.candidate_id,
                    candidate.current_files,
                    candidate.candidate_files,
                    candidate.evaluation_tasks or candidate.gate_tasks,
                )
                return EvaluationResult(
                    evaluator="evallab_harness_pairs",
                    evaluator_version="1",
                    metrics=result["metrics"],
                    metadata={METADATA_KEY: {
                        "evaluation_seconds": time.perf_counter() - started,
                        "lab": result["metadata"],
                    }},
                )
            measured = super().evaluate(candidate)
            return replace(measured, metadata={
                **measured.metadata,
                METADATA_KEY: {"evaluation_seconds": time.perf_counter() - started},
            })
        except Exception as error:
            # No fabricated score or side observation: settlement must preserve
            # the current failure manifest when the evaluator did not finish.
            return EvaluationResult(
                evaluator="backend_evaluation_error",
                evaluator_version="1",
                metrics={"candidate_scores": (), "current_scores": (), "evaluation_sides": []},
                metadata={METADATA_KEY: {
                    "evaluation_seconds": time.perf_counter() - started,
                    "evaluation_error_type": type(error).__name__,
                }},
            )

    def decide(self, candidate: UpdateCandidate, evaluation: EvaluationResult) -> SelectionDecision:
        started = time.perf_counter()
        evaluation_seconds: float | None = None
        error_type: str | None = None
        lab_evidence: Mapping[str, Any] | None = None
        reason_code = "invalid_evaluation"
        metrics: dict[str, Any]
        try:
            if not isinstance(evaluation.metadata, Mapping) or not isinstance(evaluation.metrics, Mapping):
                raise ValueError("evaluation metadata and metrics must be mappings")
            metadata = evaluation.metadata.get(METADATA_KEY, {})
            if not isinstance(metadata, Mapping):
                raise ValueError("gate timing metadata must be a mapping")
            evaluation_seconds = metadata.get("evaluation_seconds")
            raw_lab = metadata.get("lab")
            if raw_lab is not None and not isinstance(raw_lab, Mapping):
                raise ValueError("Lab evidence must be a mapping")
            lab_evidence = raw_lab
            if metadata.get("evaluation_error_type"):
                reason_code = "evaluator_error"
                error_type = str(metadata["evaluation_error_type"])
                raise ValueError("backend evaluation did not complete")
            if not isinstance(candidate, HarnessCandidate):
                raise ValueError("paired harness gate requires a HarnessCandidate")
            tasks = candidate.evaluation_tasks or candidate.gate_tasks
            if not tasks or any(not isinstance(task, str) or not task for task in tasks):
                raise ValueError("candidate has no valid evaluation task identities")
            candidate_scores = evaluation.metrics["candidate_scores"]
            current_scores = evaluation.metrics["current_scores"]
            if (
                not isinstance(candidate_scores, Sequence)
                or isinstance(candidate_scores, (str, bytes))
                or not isinstance(current_scores, Sequence)
                or isinstance(current_scores, (str, bytes))
            ):
                raise ValueError("evaluation must contain positional score vectors")
            task_ids = tuple(
                f"task-{index}:{hashlib.sha256(task.encode()).hexdigest()}"
                for index, task in enumerate(tasks)
            )
            result = decide_pairs(
                candidate_scores, current_scores, task_ids=task_ids,
                episode_repeats=evaluation.metrics["episode_repeats"], config=self.config,
            )
            metrics = result.to_dict()
            if lab_evidence is not None and lab_evidence.get("complete") is not True:
                # Preserve measured pairs, but never publish a partially observed
                # campaign after an approval, infrastructure or budget stop.
                metrics.update({
                    "selected": False,
                    "reason_code": "insufficient_evidence",
                    "incomplete_reason": lab_evidence.get("incomplete_reason"),
                })
        except Exception as error:
            metrics = {
                "selected": False, "reason_code": reason_code,
                "pairs": [], "valid_pairs": 0, "invalid_pairs": 0,
                "wins": 0, "losses": 0, "ties": 0, "p_value": None,
                "vetoes": [], "config": asdict(self.config),
                "error_type": error_type or type(error).__name__,
            }
        metrics.update({
            "reef_commit": self.reef_commit,
            "evaluation_seconds": evaluation_seconds,
            "decision_seconds": time.perf_counter() - started,
            "timing_scope": "evaluation and decision computation; record persistence excluded",
        })
        if lab_evidence is not None:
            metrics["lab"] = dict(lab_evidence)
        record_path = self.record_dir / (
            hashlib.sha256(candidate.candidate_id.encode()).hexdigest() + ".json"
        )
        metrics["decision_record"] = str(record_path)
        record = {
            "schema_version": 1,
            "candidate_id": candidate.candidate_id,
            "created_at": datetime.now(UTC).isoformat(),
            "policy": POLICY, "policy_version": POLICY_VERSION,
            "evaluator": evaluation.evaluator,
            "evaluator_version": evaluation.evaluator_version,
            **metrics,
        }
        try:
            encoded = json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
            # Exclusive creation makes the candidate's first decision durable.
            with record_path.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, TypeError, ValueError) as error:
            metrics = {
                **metrics, "selected": False, "reason_code": "decision_record_error",
                "error_type": type(error).__name__,
            }
        return SelectionDecision(
            outcome="select" if metrics["selected"] else "reject",
            policy=POLICY,
            policy_version=POLICY_VERSION,
            reason=str(metrics["reason_code"]),
            evaluation=evaluation,
            metrics=metrics,
        )
