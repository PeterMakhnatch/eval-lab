"""Plan-only held-out pair projection for trajectory-to-training local controls.

This module deliberately cannot construct a scientific held-out freeze.  That
boundary remains on ``TrainingSplit.ownership_domain`` and
``SftSignalFreezeV1`` after their F3/F4 producer fields land.  The only public
builder here emits a claim-ineligible, non-submittable local-control projection
using the existing task, checkpoint, and selection-recipe digest identities.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.benchmark_program_contracts import compute_prefixed_sha256
from evallab.schemas import ContractModel, Digest
from evallab.sft_signal import SftCheckpointIdentityV1
from evallab.trainer_bundle import TrainerEvaluationSetV1, TrainerTaskIdentityV1

_ZERO_DIGEST = "sha256:" + "0" * 64
_PAIR_ID_PATTERN = r"^pair-[0-9a-f]{20}$"
_SEED_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SftFrozenEvalPairV1(_FrozenContract):
    """One exact baseline/candidate pair with no outcome or run authority."""

    schema_version: Literal["sft-frozen-eval-pair/v1"] = "sft-frozen-eval-pair/v1"
    pair_id: str = Field(pattern=_PAIR_ID_PATTERN)
    pair_ordinal: int = Field(ge=1)
    task: TrainerTaskIdentityV1
    generator_seed: int | str
    selection_recipe_digest: Digest
    baseline_checkpoint: SftCheckpointIdentityV1
    candidate_checkpoint: SftCheckpointIdentityV1
    harness_identity_digest: Digest
    runtime_identity_digest: Digest
    arms: tuple[Literal["baseline"], Literal["candidate"]] = (
        "baseline",
        "candidate",
    )
    pair_digest: Digest

    @field_validator("generator_seed")
    @classmethod
    def seed_is_canonical(cls, value: int | str) -> int | str:
        if isinstance(value, bool):
            raise ValueError("generator_seed cannot be boolean")
        if isinstance(value, int):
            if value < 0:
                raise ValueError("integer generator_seed must be non-negative")
            return value
        if _SEED_PATTERN.fullmatch(value) is None:
            raise ValueError("string generator_seed must be a canonical identifier")
        return value

    @model_validator(mode="after")
    def identities_and_digest_match(self) -> SftFrozenEvalPairV1:
        if self.selection_recipe_digest == _ZERO_DIGEST:
            raise ValueError("selection recipe digest cannot be all-zero")
        if self.harness_identity_digest == _ZERO_DIGEST:
            raise ValueError("harness identity digest cannot be all-zero")
        if self.runtime_identity_digest == _ZERO_DIGEST:
            raise ValueError("runtime identity digest cannot be all-zero")
        if (
            self.baseline_checkpoint.role != "baseline"
            or self.candidate_checkpoint.role != "candidate"
        ):
            raise ValueError("checkpoint roles must match pair arms")
        if (
            self.baseline_checkpoint.model_revision
            != self.candidate_checkpoint.model_revision
            or self.baseline_checkpoint.model_digest
            != self.candidate_checkpoint.model_digest
        ):
            raise ValueError("baseline and candidate must share model identity")
        if (
            self.baseline_checkpoint.checkpoint_artifact_digest
            == self.candidate_checkpoint.checkpoint_artifact_digest
        ):
            raise ValueError("baseline and candidate checkpoints must differ")
        expected_id = _pair_id(
            task=self.task,
            generator_seed=self.generator_seed,
            selection_recipe_digest=self.selection_recipe_digest,
        )
        if self.pair_id != expected_id:
            raise ValueError("pair_id does not match task, seed, and recipe identity")
        expected_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"pair_digest"})
        )
        if self.pair_digest != expected_digest:
            raise ValueError("pair_digest does not match pair content")
        return self


class SftEvalPairProjectionV1(_FrozenContract):
    """Claim-ineligible local projection of exact SFT evaluation pairs.

    This is not a manifest, evidence authority, scientific freeze, or execution
    request.  Its literal gates make it structurally incapable of submission or
    outcome claims.
    """

    schema_version: Literal["sft-eval-pair-projection/v1"] = (
        "sft-eval-pair-projection/v1"
    )
    scope: Literal["local-control-only"] = "local-control-only"
    evaluation_set: TrainerEvaluationSetV1
    selection_recipe_digest: Digest
    pairs: tuple[SftFrozenEvalPairV1, ...] = Field(min_length=1)
    pair_set_digest: Digest
    submission_permitted: Literal[False] = False
    scientific_claim_permitted: Literal[False] = False
    outcomes_present: Literal[False] = False
    projection_digest: Digest

    @model_validator(mode="after")
    def membership_and_digest_match(self) -> SftEvalPairProjectionV1:
        expected_ordinals = tuple(range(1, len(self.pairs) + 1))
        if tuple(pair.pair_ordinal for pair in self.pairs) != expected_ordinals:
            raise ValueError("pair ordinals must be contiguous and canonical")
        pair_ids = tuple(pair.pair_id for pair in self.pairs)
        if len(set(pair_ids)) != len(pair_ids):
            raise ValueError("pair IDs must be unique")
        if any(
            pair.selection_recipe_digest != self.selection_recipe_digest
            for pair in self.pairs
        ):
            raise ValueError("every pair must bind the projection selection recipe")
        if tuple(pair.task for pair in self.pairs) != self.evaluation_set.tasks:
            raise ValueError("pairs must exactly cover the canonical evaluation task set")
        expected_pair_set_digest = _canonical_digest(
            [pair.model_dump(mode="json") for pair in self.pairs]
        )
        if self.pair_set_digest != expected_pair_set_digest:
            raise ValueError("pair_set_digest does not match exact pair membership")
        expected_projection_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"projection_digest"})
        )
        if self.projection_digest != expected_projection_digest:
            raise ValueError("projection_digest does not match projection content")
        return self


def _canonical_digest(value: object) -> str:
    return str(compute_prefixed_sha256(value))


def _pair_id(
    *,
    task: TrainerTaskIdentityV1,
    generator_seed: int | str,
    selection_recipe_digest: str,
) -> str:
    identity_digest = _canonical_digest(
        {
            "generator_seed": generator_seed,
            "selection_recipe_digest": selection_recipe_digest,
            "task": task.model_dump(mode="json"),
        }
    )
    return "pair-" + identity_digest.removeprefix("sha256:")[:20]


def _frozen_pair(
    *,
    pair_ordinal: int,
    task: TrainerTaskIdentityV1,
    generator_seed: int | str,
    selection_recipe_digest: Digest,
    baseline_checkpoint: SftCheckpointIdentityV1,
    candidate_checkpoint: SftCheckpointIdentityV1,
    harness_identity_digest: Digest,
    runtime_identity_digest: Digest,
) -> SftFrozenEvalPairV1:
    seeded = SftFrozenEvalPairV1.model_construct(
        pair_id=_pair_id(
            task=task,
            generator_seed=generator_seed,
            selection_recipe_digest=selection_recipe_digest,
        ),
        pair_ordinal=pair_ordinal,
        task=task,
        generator_seed=generator_seed,
        selection_recipe_digest=selection_recipe_digest,
        baseline_checkpoint=baseline_checkpoint,
        candidate_checkpoint=candidate_checkpoint,
        harness_identity_digest=harness_identity_digest,
        runtime_identity_digest=runtime_identity_digest,
        pair_digest=_ZERO_DIGEST,
    )
    payload = seeded.model_dump(mode="json")
    payload["pair_digest"] = _canonical_digest(
        seeded.model_dump(mode="json", exclude={"pair_digest"})
    )
    return SftFrozenEvalPairV1.model_validate(payload)


def build_local_eval_pair_projection(
    *,
    evaluation_set: TrainerEvaluationSetV1,
    selection_recipe_digest: Digest,
    task_seeds: Mapping[str, int | str],
    baseline_checkpoint: SftCheckpointIdentityV1,
    candidate_checkpoint: SftCheckpointIdentityV1,
    harness_identity_digest: Digest,
    runtime_identity_digest: Digest,
) -> SftEvalPairProjectionV1:
    """Freeze exact local-control pairs without reading data or authorizing a run."""

    canonical_evaluation_set = TrainerEvaluationSetV1.model_validate(
        evaluation_set.model_dump(mode="json")
    )
    baseline = SftCheckpointIdentityV1.model_validate(
        baseline_checkpoint.model_dump(mode="json")
    )
    candidate = SftCheckpointIdentityV1.model_validate(
        candidate_checkpoint.model_dump(mode="json")
    )
    task_ids = tuple(task.task_id for task in canonical_evaluation_set.tasks)
    if set(task_seeds) != set(task_ids) or len(task_seeds) != len(task_ids):
        raise ValueError("task_seeds must exactly cover the evaluation task set")
    pairs = tuple(
        _frozen_pair(
            pair_ordinal=ordinal,
            task=task,
            generator_seed=task_seeds[task.task_id],
            selection_recipe_digest=selection_recipe_digest,
            baseline_checkpoint=baseline,
            candidate_checkpoint=candidate,
            harness_identity_digest=harness_identity_digest,
            runtime_identity_digest=runtime_identity_digest,
        )
        for ordinal, task in enumerate(canonical_evaluation_set.tasks, start=1)
    )
    pair_set_digest = _canonical_digest(
        [pair.model_dump(mode="json") for pair in pairs]
    )
    seeded = SftEvalPairProjectionV1.model_construct(
        evaluation_set=canonical_evaluation_set,
        selection_recipe_digest=selection_recipe_digest,
        pairs=pairs,
        pair_set_digest=pair_set_digest,
        projection_digest=_ZERO_DIGEST,
    )
    payload = seeded.model_dump(mode="json")
    payload["projection_digest"] = _canonical_digest(
        seeded.model_dump(mode="json", exclude={"projection_digest"})
    )
    return SftEvalPairProjectionV1.model_validate(payload)


__all__ = [
    "SftEvalPairProjectionV1",
    "SftFrozenEvalPairV1",
    "build_local_eval_pair_projection",
]
