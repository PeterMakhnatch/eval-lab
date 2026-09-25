"""Pure decision rules for the Reef publication gate.

This module is standard-library-only and side-effect free: it turns two
positionally paired score vectors into a publish-or-reject decision without
importing Reef or Eval Lab, reading files or clocks, or running anything. The
same decision can therefore be replayed offline from a recorded score vector.

Positional layout follows Reef's episode pairing order: scores are grouped
task-major with ``episode_repeats`` contiguous positions per task, so position
``p`` belongs to task ``task_ids[p // episode_repeats]`` at repeat
``p % episode_repeats``. ``task_ids`` carries one group identity per task
(Reef's ``evaluation_tasks``), not one entry per pair, and
``len(candidate_scores) == len(current_scores) == len(task_ids) *
episode_repeats`` must hold exactly.

Fail-closed contract: malformed shapes, groupings, or configuration raise
:class:`ValueError` so a caller translates any structural surprise into a
rejection instead of a guess.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "REASON_INSUFFICIENT_EVIDENCE",
    "REASON_NOT_SIGNIFICANT",
    "REASON_PUBLISH",
    "REASON_REGRESSION_VETO",
    "REASON_WORSE",
    "GateConfig",
    "GatePair",
    "GateResult",
    "GateVeto",
    "decide_pairs",
    "sign_test_p",
]

REASON_PUBLISH = "publish"
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REASON_REGRESSION_VETO = "regression_veto"
REASON_WORSE = "worse"
REASON_NOT_SIGNIFICANT = "not_significant"


@dataclass(frozen=True)
class GateConfig:
    """Validated thresholds for :func:`decide_pairs`; nothing here is optional logic.

    ``alpha`` is the strict one-sided sign-test level (publish requires
    ``p < alpha``, equality rejects). ``min_valid_pairs`` is the least number
    of usable pairs the gate will read as evidence. ``pass_threshold`` is the
    finite score an episode must reach to count as passing; scores are general
    finite numbers, not restricted to fractions. ``regression_failure_threshold``
    is how many below-threshold candidate episodes a single protected task may
    accumulate before it vetoes the decision.
    """

    alpha: float = 0.05
    min_valid_pairs: int = 5
    pass_threshold: float = 1.0
    regression_failure_threshold: int = 1

    def __post_init__(self) -> None:
        for name in ("alpha", "pass_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
            object.__setattr__(self, name, float(value))
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must lie strictly between 0 and 1")
        for name in ("min_valid_pairs", "regression_failure_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "min_valid_pairs": self.min_valid_pairs,
            "pass_threshold": self.pass_threshold,
            "regression_failure_threshold": self.regression_failure_threshold,
        }


def sign_test_p(wins: int, losses: int) -> float:
    """Exact one-sided sign test: P(X >= wins) for X ~ Binomial(wins + losses, 1/2).

    Ties are not an input; the caller drops them from the sign sample before
    counting wins and losses. With no decisive pairs the statistic is vacuous,
    so the p-value is 1.
    """
    for name, value in (("wins", wins), ("losses", losses)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(wins, n + 1)) / 2**n


@dataclass(frozen=True)
class GatePair:
    """One positional candidate/current pairing and how the gate judged it.

    ``candidate_score``/``current_score`` are normalized floats for the sides
    the gate could read and ``None`` only for a side it refused (``None``,
    bool, non-number, or non-finite); a usable score on one side of an invalid
    pair is preserved, not discarded. ``invalid_reason`` names every refused
    side (``missing``, ``not_numeric``, ``not_finite``).
    """

    index: int
    task_id: str
    repeat_index: int
    candidate_score: float | None
    current_score: float | None
    valid: bool
    result: str
    invalid_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task_id": self.task_id,
            "repeat_index": self.repeat_index,
            "candidate_score": self.candidate_score,
            "current_score": self.current_score,
            "valid": self.valid,
            "result": self.result,
            "invalid_reason": self.invalid_reason,
        }


@dataclass(frozen=True)
class GateVeto:
    """One protected task whose candidate regressions reached the veto threshold.

    ``failed_repeat_indices`` are the task-local repeat positions (not global
    pair indices) whose valid candidate scores fell below ``pass_threshold``.
    """

    task_id: str
    failure_count: int
    failed_repeat_indices: tuple[int, ...]
    threshold: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "failure_count": self.failure_count,
            "failed_repeat_indices": list(self.failed_repeat_indices),
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class GateResult:
    """The complete evidence and verdict for one gate decision.

    ``vetoes`` holds one :class:`GateVeto` per protected task whose valid
    below-threshold candidate episodes reached that task's
    ``regression_failure_threshold``; it is non-empty exactly when the
    ``regression_veto`` reason applies.
    """

    selected: bool
    reason_code: str
    pairs: tuple[GatePair, ...]
    valid_pairs: int
    invalid_pairs: int
    wins: int
    losses: int
    ties: int
    p_value: float
    vetoes: tuple[GateVeto, ...]
    config: GateConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "reason_code": self.reason_code,
            "pairs": [pair.to_dict() for pair in self.pairs],
            "valid_pairs": self.valid_pairs,
            "invalid_pairs": self.invalid_pairs,
            "wins": self.wins,
            "losses": self.losses,
            "ties": self.ties,
            "p_value": self.p_value,
            "vetoes": [veto.to_dict() for veto in self.vetoes],
            "config": self.config.to_dict(),
        }


def _score_offense(side: str, value: object) -> str | None:
    """First reason ``value`` is unusable as a score on ``side``, or None when usable."""
    if value is None:
        return f"{side}_score_missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{side}_score_not_numeric"
    if not math.isfinite(value):
        return f"{side}_score_not_finite"
    return None


def _as_tuple(value: object, name: str) -> tuple[Any, ...]:
    """Accept a real sequence (never str/bytes) so element-wise checks see items, not characters."""
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence, not {type(value).__name__}")
    return tuple(value)


def decide_pairs(
    candidate_scores: Sequence[object],
    current_scores: Sequence[object],
    *,
    task_ids: Sequence[str],
    episode_repeats: int,
    config: GateConfig,
) -> GateResult:
    """Judge one gate evaluation from positionally paired episode scores.

    ``candidate_scores`` and ``current_scores`` hold one score per episode in
    Reef's pairing order (task-major, ``episode_repeats`` contiguous positions
    per task); ``task_ids`` names each task group once. ``None`` marks an
    episode that could not run; bools, non-numbers, and non-finite floats are
    refused rather than coerced. A usable zero is an ordinary score, and a
    usable score survives on the valid side of an otherwise invalid pair.

    Precedence, after all evidence is computed: fewer valid pairs than
    ``config.min_valid_pairs`` -> ``insufficient_evidence``; any task-level
    veto -> ``regression_veto``; ``p < alpha`` -> ``publish``; otherwise
    ``losses > wins`` -> ``worse``, else ``not_significant``.
    """
    if not isinstance(config, GateConfig):
        raise ValueError("config must be a GateConfig")
    if isinstance(episode_repeats, bool) or not isinstance(episode_repeats, int) or episode_repeats < 1:
        raise ValueError("episode_repeats must be an integer of at least 1")
    candidate = _as_tuple(candidate_scores, "candidate_scores")
    current = _as_tuple(current_scores, "current_scores")
    tasks = _as_tuple(task_ids, "task_ids")
    for task_id in tasks:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_ids must be non-empty strings")
    if len(candidate) != len(current):
        raise ValueError(
            f"candidate_scores ({len(candidate)}) and current_scores ({len(current)}) must have equal length"
        )
    if len(candidate) != len(tasks) * episode_repeats:
        raise ValueError(
            f"scores ({len(candidate)}) must hold episode_repeats ({episode_repeats}) "
            f"contiguous positions per task, i.e. len(task_ids) ({len(tasks)}) * episode_repeats"
        )

    candidate_offenses = [_score_offense("candidate", value) for value in candidate]
    current_offenses = [_score_offense("current", value) for value in current]

    pairs: list[GatePair] = []
    for index in range(len(candidate)):
        task_id = tasks[index // episode_repeats]
        repeat_index = index % episode_repeats
        offenses = [
            offense for offense in (candidate_offenses[index], current_offenses[index]) if offense is not None
        ]
        # Sanitize only the refused side; an observed score on the other side stays on record.
        candidate_value = None if candidate_offenses[index] is not None else float(candidate[index])
        current_value = None if current_offenses[index] is not None else float(current[index])
        if candidate_value is None or current_value is None:
            result = "invalid"
        elif candidate_value > current_value:
            result = "win"
        elif candidate_value < current_value:
            result = "loss"
        else:
            result = "tie"
        pairs.append(
            GatePair(
                index=index,
                task_id=task_id,
                repeat_index=repeat_index,
                candidate_score=candidate_value,
                current_score=current_value,
                valid=not offenses,
                result=result,
                invalid_reason="; ".join(offenses) if offenses else None,
            )
        )

    wins = sum(1 for pair in pairs if pair.result == "win")
    losses = sum(1 for pair in pairs if pair.result == "loss")
    ties = sum(1 for pair in pairs if pair.result == "tie")
    valid_pairs = wins + losses + ties
    invalid_pairs = len(pairs) - valid_pairs

    # Veto accounting works per task group over the raw vectors: protection
    # depends only on the current side, an invalid candidate episode is
    # missing evidence rather than a fabricated failure, and the failure
    # threshold applies within one protected task, never pooled across tasks.
    vetoes: list[GateVeto] = []
    for task_index, task_id in enumerate(tasks):
        start = task_index * episode_repeats
        positions = range(start, start + episode_repeats)
        protected = all(
            current_offenses[position] is None and float(current[position]) >= config.pass_threshold
            for position in positions
        )
        if not protected:
            continue
        failed_repeats = tuple(
            position - start
            for position in positions
            if candidate_offenses[position] is None and float(candidate[position]) < config.pass_threshold
        )
        if len(failed_repeats) >= config.regression_failure_threshold:
            vetoes.append(
                GateVeto(
                    task_id=task_id,
                    failure_count=len(failed_repeats),
                    failed_repeat_indices=failed_repeats,
                    threshold=config.regression_failure_threshold,
                )
            )

    p_value = sign_test_p(wins, losses)
    if valid_pairs < config.min_valid_pairs:
        selected, reason_code = False, REASON_INSUFFICIENT_EVIDENCE
    elif vetoes:
        selected, reason_code = False, REASON_REGRESSION_VETO
    elif p_value < config.alpha:
        selected, reason_code = True, REASON_PUBLISH
    elif losses > wins:
        selected, reason_code = False, REASON_WORSE
    else:
        selected, reason_code = False, REASON_NOT_SIGNIFICANT

    return GateResult(
        selected=selected,
        reason_code=reason_code,
        pairs=tuple(pairs),
        valid_pairs=valid_pairs,
        invalid_pairs=invalid_pairs,
        wins=wins,
        losses=losses,
        ties=ties,
        p_value=p_value,
        vetoes=tuple(vetoes),
        config=config,
    )
