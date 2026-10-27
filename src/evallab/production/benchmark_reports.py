"""Strict, versioned retained-report schemas shared by the cloud API and the exporter.

``validate_benchmark_document`` is the single validator behind
``/api/cloud-db/benchmarks`` and ``evallab prod export-qc-reports``: a document the
exporter writes is accepted by the dashboard because both run this code on the
same exact bytes. Schemas are consumer contracts documented in
``docs/live-watch.md``; validation checks counts and invariants, never the
truth of a producer's claims.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BENCHMARK_BYTES = 2 * 1024 * 1024

_Count = Annotated[int, Field(ge=0)]
_Fraction = Annotated[float, Field(ge=0, le=1)]
_Spend = Annotated[float, Field(ge=0)]
_Label = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _BenchmarkRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class _BenchmarkCohort(_BenchmarkRecord):
    dataset_id: _Label
    split: Literal["dev"]
    task_count: _Count


class _ArmMetrics(_BenchmarkRecord):
    primary_hits_n: _Count
    primary_total_n: _Count
    precision: _Fraction
    recall: _Fraction
    f1: _Fraction

    @model_validator(mode="after")
    def validate_hits(self) -> Self:
        if self.primary_hits_n > self.primary_total_n:
            raise ValueError("primary_hits_n must not exceed primary_total_n")
        return self


class _ComparisonArm(_BenchmarkRecord):
    arm: Literal["A", "B", "C"]
    label: _Label
    served_models: Annotated[
        list[Annotated[str, Field(pattern=r"^[^\s/]+/[^\s]+$")]], Field(min_length=1)
    ]
    harness: _Label
    retrieval: bool
    metrics: _ArmMetrics
    spend_usd: _Spend
    model_calls: _Count
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _ArmComparison(_BenchmarkRecord):
    schema_: Literal["evallab.arm_comparison/v1"] = Field(alias="schema")
    cohort: _BenchmarkCohort
    arms: Annotated[list[_ComparisonArm], Field(min_length=1, max_length=3)]
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or "T" not in value:
            raise ValueError("generated_at must be an ISO-8601 timestamp with timezone")
        return value

    @model_validator(mode="after")
    def validate_arms(self) -> Self:
        if len({arm.arm for arm in self.arms}) != len(self.arms):
            raise ValueError("arm identifiers must be unique")
        return self


class _NamedCohort(_BenchmarkCohort):
    task_count: Annotated[int, Field(gt=0)]
    eligible_n: _Count
    blocked_n: _Count

    @model_validator(mode="after")
    def validate_denominator(self) -> Self:
        if self.eligible_n + self.blocked_n != self.task_count:
            raise ValueError("eligible_n + blocked_n must equal task_count")
        return self


class _ArmCoverage(_BenchmarkRecord):
    answered_n: _Count
    judged_n: _Count
    missing_n: _Count
    blocked_n: _Count
    refused_n: _Count
    unavailable_n: _Count
    malformed_n: _Count
    unattested_n: _Count

    @model_validator(mode="after")
    def validate_judgments(self) -> Self:
        if self.judged_n > self.answered_n:
            raise ValueError("judged_n must not exceed answered_n")
        return self


class _NullableArmMetrics(_BenchmarkRecord):
    primary_hits_n: _Count | None
    primary_total_n: _Count | None
    precision: _Fraction | None
    recall: _Fraction | None
    f1: _Fraction | None

    @model_validator(mode="after")
    def validate_hits(self) -> Self:
        hits, total = self.primary_hits_n, self.primary_total_n
        if (hits is None) != (total is None):
            raise ValueError("primary hits and total must both be recorded or both be null")
        if hits is not None and total is not None and hits > total:
            raise ValueError("primary_hits_n must not exceed primary_total_n")
        return self


class _NamedComparisonArm(_BenchmarkRecord):
    arm: _Label
    label: _Label
    harness: _Label
    retrieval: bool
    requested_models: list[Annotated[str, Field(pattern=r"^[^\s/]+/[^\s]+$")]]
    served_models: list[Annotated[str, Field(pattern=r"^[^\s/]+/[^\s]+$")]]
    coverage: _ArmCoverage
    metrics: _NullableArmMetrics
    spend_usd: _Spend | None
    model_calls: _Count | None
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None

    @field_validator("arm")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("arm identifiers must not have surrounding whitespace")
        return value

    @field_validator("requested_models", "served_models")
    @classmethod
    def validate_distinct_models(cls, value: list[str]) -> list[str]:
        if len({model.casefold() for model in value}) != len(value):
            raise ValueError("model identities must be distinct")
        return value

    @model_validator(mode="after")
    def validate_answer_backing(self) -> Self:
        if self.coverage.answered_n and (not self.served_models or self.artifact_sha256 is None):
            raise ValueError(
                "answered cells require actual served identities and an artifact digest"
            )
        return self


class _NamedArmComparison(_BenchmarkRecord):
    schema_: Literal["evallab.arm_comparison/v2"] = Field(alias="schema")
    protocol: _Label
    status: Literal["blocked", "incomplete", "complete"]
    cohort: _NamedCohort
    arms: Annotated[list[_NamedComparisonArm], Field(min_length=1)]
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or "T" not in value:
            raise ValueError("generated_at must be an ISO-8601 timestamp with timezone")
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if len({arm.arm.casefold() for arm in self.arms}) != len(self.arms):
            raise ValueError("arm identifiers must be unique")
        blocked = self.cohort.blocked_n > 0
        complete = True
        for arm in self.arms:
            coverage = arm.coverage
            answer_total = (
                coverage.answered_n
                + coverage.missing_n
                + coverage.blocked_n
                + coverage.refused_n
                + coverage.unavailable_n
                + coverage.malformed_n
                + coverage.unattested_n
            )
            if answer_total != self.cohort.task_count:
                raise ValueError("terminal answer-state counts must equal cohort.task_count")
            if coverage.blocked_n < self.cohort.blocked_n:
                raise ValueError("arm blocked_n must include every cohort blocked identity")
            blocked |= coverage.blocked_n > 0
            complete &= (
                coverage.answered_n == self.cohort.task_count
                and coverage.judged_n == self.cohort.task_count
            )
            metrics = arm.metrics
            if self.status != "complete" and any(
                value is not None
                for value in (
                    metrics.primary_hits_n,
                    metrics.primary_total_n,
                    metrics.precision,
                    metrics.recall,
                    metrics.f1,
                )
            ):
                raise ValueError("full-comparison metrics must be null until coverage is complete")
        expected = "blocked" if blocked else "complete" if complete else "incomplete"
        if self.status != expected:
            raise ValueError(f"status must be {expected} for the reported coverage")
        return self


_ModelId = Annotated[str, Field(pattern=r"^[^\s/]+/[^\s]+$")]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _SeekerCell(_BenchmarkRecord):
    """One model's retained outcome counts under one prompt condition."""

    condition: _Label
    prompt_sha256: _Sha256 | None
    served_models: list[_ModelId]
    attested_n: _Count
    unattested_n: _Count
    refused_n: _Count
    unavailable_n: _Count
    malformed_n: _Count
    blocked_n: _Count
    missing_n: _Count
    graded_n: _Count
    original_pass_n: _Count
    verified_differential_n: _Count
    spend_usd: _Spend | None
    model_calls: _Count | None
    artifact_sha256: _Sha256 | None

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("condition names must not have surrounding whitespace")
        return value

    @field_validator("served_models")
    @classmethod
    def validate_distinct_served(cls, value: list[str]) -> list[str]:
        if len({model.casefold() for model in value}) != len(value):
            raise ValueError("served model identities must be distinct")
        return value

    @property
    def terminal_total(self) -> int:
        return (
            self.attested_n
            + self.unattested_n
            + self.refused_n
            + self.unavailable_n
            + self.malformed_n
            + self.blocked_n
            + self.missing_n
        )

    @model_validator(mode="after")
    def validate_funnel(self) -> Self:
        if not self.graded_n <= self.attested_n:
            raise ValueError("graded_n must not exceed attested_n")
        if not self.verified_differential_n <= self.original_pass_n <= self.graded_n:
            raise ValueError(
                "verified_differential_n must not exceed original_pass_n, "
                "which must not exceed graded_n"
            )
        if self.attested_n and (not self.served_models or self.artifact_sha256 is None):
            raise ValueError(
                "attested cells require actual served identities and an artifact digest"
            )
        return self


class _SeekerArm(_BenchmarkRecord):
    arm: _Label
    label: _Label
    requested_model: _ModelId
    cells: Annotated[list[_SeekerCell], Field(min_length=1)]

    @field_validator("arm")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("arm identifiers must not have surrounding whitespace")
        return value


class _SeekerComparison(_BenchmarkRecord):
    """Grader-weakness discovery by named model across paired prompt conditions."""

    schema_: Literal["evallab.seeker_comparison/v1"] = Field(alias="schema")
    protocol: _Label
    status: Literal["blocked", "incomplete", "complete"]
    cohort: _NamedCohort
    arms: Annotated[list[_SeekerArm], Field(min_length=1)]
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or "T" not in value:
            raise ValueError("generated_at must be an ISO-8601 timestamp with timezone")
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if len({arm.arm.casefold() for arm in self.arms}) != len(self.arms):
            raise ValueError("arm identifiers must be unique")
        if len({arm.requested_model.casefold() for arm in self.arms}) != len(self.arms):
            raise ValueError("requested model identities must be unique across arms")
        conditions = {cell.condition.casefold() for cell in self.arms[0].cells}
        blocked = self.cohort.blocked_n > 0
        complete = True
        for arm in self.arms:
            names = [cell.condition.casefold() for cell in arm.cells]
            if len(set(names)) != len(names):
                raise ValueError("condition names must be unique within an arm")
            if set(names) != conditions:
                raise ValueError("every arm must report the same paired prompt conditions")
            for cell in arm.cells:
                if cell.terminal_total != self.cohort.task_count:
                    raise ValueError("terminal outcome counts must equal cohort.task_count")
                if cell.blocked_n < self.cohort.blocked_n:
                    raise ValueError("cell blocked_n must include every cohort blocked identity")
                blocked |= cell.blocked_n > 0
                complete &= cell.graded_n == self.cohort.task_count
        expected = "blocked" if blocked else "complete" if complete else "incomplete"
        if self.status != expected:
            raise ValueError(f"status must be {expected} for the reported coverage")
        return self


def _aware_timestamp(value: str) -> str:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or "T" not in value:
        raise ValueError("timestamps must be ISO-8601 with a timezone")
    return value


class _TierRow(_BenchmarkRecord):
    tier: Literal["tier1_container_verified", "tier2_consensus_labeled", "tier3_provisional"]
    source: _Label
    basis: _Label
    rows_n: _Count
    prompt_eligible_n: _Count

    @model_validator(mode="after")
    def validate_prompt_eligibility(self) -> Self:
        if self.prompt_eligible_n > self.rows_n:
            raise ValueError("prompt_eligible_n must not exceed rows_n")
        if self.tier == "tier3_provisional" and self.prompt_eligible_n:
            raise ValueError("tier3_provisional rows must never be prompt eligible")
        return self


class _TrustTierCensus(_BenchmarkRecord):
    """Producer-reported memory tier counts over admitted DEV rows."""

    schema_: Literal["evallab.trust_tier_census/v1"] = Field(alias="schema")
    split: Literal["dev"]
    unit: _Label
    admitted_rows_n: _Count
    unclassified_n: _Count
    tiers: list[_TierRow]
    source_sha256: _Sha256 | None
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        keys = [(row.tier, row.source.casefold()) for row in self.tiers]
        if len(set(keys)) != len(keys):
            raise ValueError("each tier/source pair may appear once")
        if sum(row.rows_n for row in self.tiers) + self.unclassified_n != self.admitted_rows_n:
            raise ValueError("tier rows plus unclassified_n must equal admitted_rows_n")
        return self


class _QcCohort(_BenchmarkRecord):
    dataset_id: _Label
    split: Literal["dev"]
    positive_n: _Count
    clean_n: _Count
    unscored_n: _Count

    @model_validator(mode="after")
    def validate_scored(self) -> Self:
        if self.positive_n + self.clean_n == 0:
            raise ValueError("a QC cohort needs at least one scored case")
        return self


class _QcRule(_BenchmarkRecord):
    rule_id: _Label
    positives_hit_n: _Count
    clean_hit_n: _Count
    marginal_true_positive_n: _Count

    @field_validator("rule_id")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("rule identifiers must not have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_marginal(self) -> Self:
        if self.marginal_true_positive_n > self.positives_hit_n:
            raise ValueError("marginal_true_positive_n must not exceed positives_hit_n")
        return self


class _HackbookQc(_BenchmarkRecord):
    """Per-rule hit counts on labelled positives and clean controls; rates are derived."""

    schema_: Literal["evallab.hackbook_qc/v1"] = Field(alias="schema")
    ruleset_sha256: _Sha256
    cohort: _QcCohort
    rules: Annotated[list[_QcRule], Field(min_length=1)]
    union_positives_hit_n: _Count
    union_clean_hit_n: _Count
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if len({rule.rule_id.casefold() for rule in self.rules}) != len(self.rules):
            raise ValueError("rule identifiers must be unique")
        for label, hits, union, total in (
            (
                "positive",
                [rule.positives_hit_n for rule in self.rules],
                self.union_positives_hit_n,
                self.cohort.positive_n,
            ),
            (
                "clean",
                [rule.clean_hit_n for rule in self.rules],
                self.union_clean_hit_n,
                self.cohort.clean_n,
            ),
        ):
            if union > total:
                raise ValueError(f"union {label} hits must not exceed the {label} cohort")
            if not max(hits) <= union <= sum(hits):
                raise ValueError(
                    f"union {label} hits must lie between the largest rule and the sum"
                )
        if sum(rule.marginal_true_positive_n for rule in self.rules) > self.union_positives_hit_n:
            raise ValueError("marginal true positives must not exceed the union of positives hit")
        return self


class _TuneCrossEntry(_BenchmarkRecord):
    role: Literal["tune", "cross_test"]
    dataset_id: _Label
    split: Literal["dev"]
    system_sha256: _Sha256
    positive_n: _Count
    clean_n: _Count
    unscored_n: _Count
    true_positive_n: _Count
    false_positive_n: _Count
    measured_at: str

    @field_validator("measured_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.positive_n + self.clean_n == 0:
            raise ValueError("an entry needs at least one scored case")
        if self.true_positive_n > self.positive_n or self.false_positive_n > self.clean_n:
            raise ValueError("hits must not exceed the scored cohort they are drawn from")
        return self


class _TuneCrossTest(_BenchmarkRecord):
    """One frozen system measured on tuning cohorts and on unseen cross-test cohorts."""

    schema_: Literal["evallab.tune_cross_test/v1"] = Field(alias="schema")
    system: _Label
    frozen_sha256: _Sha256
    entries: Annotated[list[_TuneCrossEntry], Field(min_length=1)]
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_separation(self) -> Self:
        if any(entry.system_sha256 != self.frozen_sha256 for entry in self.entries):
            raise ValueError("every entry must measure the same frozen system digest")
        tune = [entry for entry in self.entries if entry.role == "tune"]
        cross = [entry for entry in self.entries if entry.role == "cross_test"]
        if not tune:
            raise ValueError("a scoreboard needs at least one tune entry")
        names = [entry.dataset_id.casefold() for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("a dataset may appear once, so none is both tuned and cross-tested")
        latest_tune = max(datetime.fromisoformat(entry.measured_at) for entry in tune)
        if any(datetime.fromisoformat(entry.measured_at) < latest_tune for entry in cross):
            raise ValueError("cross-test measurements must not predate the last tuning measurement")
        return self


class _DatasetCount(_BenchmarkRecord):
    dataset_id: _Label
    count_n: _Count


class _ExecutedCohort(_BenchmarkRecord):
    """Tasks left after the cross-key audit: DEV under every benchmark key."""

    split: Literal["dev"]
    key_count: Annotated[int, Field(gt=0)]
    audited_n: Annotated[int, Field(gt=0)]
    cross_key_excluded_n: _Count
    task_count: Annotated[int, Field(gt=0)]
    datasets: Annotated[list[_DatasetCount], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_audit(self) -> Self:
        if self.cross_key_excluded_n + self.task_count != self.audited_n:
            raise ValueError("task_count plus cross_key_excluded_n must equal audited_n")
        names = [row.dataset_id.casefold() for row in self.datasets]
        if len(set(names)) != len(names):
            raise ValueError("dataset identifiers must be unique")
        if sum(row.count_n for row in self.datasets) != self.task_count:
            raise ValueError("dataset task counts must sum to task_count")
        return self


class _CellCounts(_BenchmarkRecord):
    """Evaluated, attested and executed-differential cells; each count bounds the next."""

    cells_n: _Count
    attested_n: _Count
    executed_differential_n: _Count

    @model_validator(mode="after")
    def validate_funnel(self) -> Self:
        if not self.executed_differential_n <= self.attested_n <= self.cells_n:
            raise ValueError(
                "executed_differential_n must not exceed attested_n, "
                "which must not exceed the evaluated cells_n"
            )
        return self


class _DatasetCells(_CellCounts):
    dataset_id: _Label


class _ExecutedCondition(_CellCounts):
    condition: _Label
    by_dataset: list[_DatasetCells]

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("condition names must not have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_split_of_cells(self) -> Self:
        for field in ("cells_n", "attested_n", "executed_differential_n"):
            if sum(getattr(row, field) for row in self.by_dataset) != getattr(self, field):
                raise ValueError(f"per-dataset {field} must sum to the condition total")
        return self


class _ExecutedArm(_BenchmarkRecord):
    arm: _Label
    requested_model: _ModelId
    conditions: Annotated[list[_ExecutedCondition], Field(min_length=1)]

    @field_validator("arm")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("arm identifiers must not have surrounding whitespace")
        return value


class _SeekerExecutedDifferentials(_BenchmarkRecord):
    """Executed paired-grader differentials per model and prompt condition.

    An executed differential is a cell whose original grader passed (reward 1.0) and
    whose repaired grader failed it (reward 0.0) when run. Only attested cells, whose
    output matched its recorded digest and whose served model was the requested one,
    can count. A condition's counts can never exceed the cells actually evaluated for
    that arm, so a partial arm (or one with no cells) cannot be reported at full scale.
    """

    schema_: Literal["evallab.seeker_executed_differentials/v2"] = Field(alias="schema")
    protocol: _Label
    cohort: _ExecutedCohort
    arms: Annotated[list[_ExecutedArm], Field(min_length=1)]
    source_sha256: _Sha256
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_arms(self) -> Self:
        if len({arm.arm.casefold() for arm in self.arms}) != len(self.arms):
            raise ValueError("arm identifiers must be unique")
        if len({arm.requested_model.casefold() for arm in self.arms}) != len(self.arms):
            raise ValueError("requested model identities must be unique across arms")
        capacity = {row.dataset_id.casefold(): row.count_n for row in self.cohort.datasets}
        names = [condition.condition.casefold() for condition in self.arms[0].conditions]
        for arm in self.arms:
            found = [condition.condition.casefold() for condition in arm.conditions]
            if len(set(found)) != len(found):
                raise ValueError("condition names must be unique within an arm")
            if found != names:
                raise ValueError("every arm must report the same conditions in the same order")
            for condition in arm.conditions:
                if condition.cells_n > self.cohort.task_count:
                    raise ValueError("evaluated cells must not exceed the cohort task_count")
                split = {row.dataset_id.casefold(): row for row in condition.by_dataset}
                if len(split) != len(condition.by_dataset) or set(split) != set(capacity):
                    raise ValueError("every cohort dataset must appear once in by_dataset")
                if any(split[name].cells_n > capacity[name] for name in split):
                    raise ValueError("per-dataset cells must not exceed that dataset's tasks")
        return self


class _CascadeCohort(_BenchmarkRecord):
    role: Literal["tune", "cross_test"]
    dataset_id: _Label
    split: Literal["dev"]
    positive_n: _Count
    clean_n: _Count
    unscored_n: _Count
    identities_checked_n: _Count
    non_dev_assignment_n: Literal[0]
    stage1_positive_hit_n: _Count
    stage1_clean_hit_n: _Count
    true_positive_n: _Count
    false_positive_n: _Count
    judge_calls_n: _Count
    input_tokens_n: _Count
    output_tokens_n: _Count

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.positive_n + self.clean_n == 0:
            raise ValueError("a cohort needs at least one scored case")
        if self.identities_checked_n != self.positive_n + self.clean_n + self.unscored_n:
            raise ValueError(
                "every cohort identity, scored or not, must have been cross-key checked"
            )
        if (
            max(self.stage1_positive_hit_n, self.true_positive_n) > self.positive_n
            or max(self.stage1_clean_hit_n, self.false_positive_n) > self.clean_n
        ):
            raise ValueError("hits must not exceed the scored cohort they are drawn from")
        return self


class _MonitorCascade(_BenchmarkRecord):
    """A frozen two-stage monitor measured on tuning and cross-test cohorts, per stage.

    ``freeze_statement`` is the producer's own account of when the system was frozen;
    no per-cohort measurement times were recorded, so ordering is not validated here.
    """

    schema_: Literal["evallab.monitor_cascade/v1"] = Field(alias="schema")
    system: _Label
    cascade_version: _Label
    arm: _Label
    judge_models: Annotated[list[_ModelId], Field(min_length=1)]
    frozen_sha256: _Sha256
    frozen_recorded_at: str
    freeze_statement: _Label
    cohorts: Annotated[list[_CascadeCohort], Field(min_length=1)]
    generated_at: str

    @field_validator("frozen_recorded_at", "generated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value)

    @field_validator("judge_models")
    @classmethod
    def validate_distinct_judges(cls, value: list[str]) -> list[str]:
        if len({model.casefold() for model in value}) != len(value):
            raise ValueError("judge model identities must be distinct")
        return value

    @model_validator(mode="after")
    def validate_separation(self) -> Self:
        if not any(cohort.role == "tune" for cohort in self.cohorts):
            raise ValueError("a cascade report needs at least one tune cohort")
        names = [cohort.dataset_id.casefold() for cohort in self.cohorts]
        if len(set(names)) != len(names):
            raise ValueError("a dataset may appear once, so none is both tuned and cross-tested")
        return self


class _SwarmScore(_BenchmarkRecord):
    # run_benchmark also retains selections, attribution, and descriptive deltas.
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)

    selected_count: _Count
    primary_hits_n: _Count
    primary_total_n: _Count
    secondary_hits_n: _Count
    secondary_total_n: _Count
    unique_key_precision: _Fraction
    secondary_f1: _Fraction
    within_task_budget: bool

    @model_validator(mode="after")
    def validate_hits(self) -> Self:
        for prefix in ("primary", "secondary"):
            hits = getattr(self, prefix + "_hits_n")
            if hits > getattr(self, prefix + "_total_n") or hits > self.selected_count:
                raise ValueError(f"{prefix} hits must not exceed total or selected_count")
        return self


class _SwarmBenchmark(_BenchmarkRecord):
    # Preserve producer-owned tracks/telemetry without inventing comparison arms.
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)

    schema_: Literal["evallab.swarm_bench/v1"] = Field(alias="schema")
    split: Literal["dev"]
    cohort: _BenchmarkCohort | None = None
    strategies: Annotated[dict[_Label, _SwarmScore], Field(min_length=1)]
    worker_telemetry: dict[str, Any]
    paid_api_spend_usd: _Spend
    model_calls: _Count
    budget_per_task: Annotated[int, Field(ge=1)]


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number: {value}")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite range")
    return number


def validate_benchmark_document(raw: bytes) -> dict[str, Any]:
    """Parse exact bytes strictly and validate them against their declared schema."""
    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
    )
    if not isinstance(document, dict):
        raise ValueError("Benchmark document must be a JSON object")
    schema = document.get("schema")
    if schema == "evallab.arm_comparison/v1":
        _ArmComparison.model_validate(document)
    elif schema == "evallab.arm_comparison/v2":
        _NamedArmComparison.model_validate(document)
    elif schema == "evallab.seeker_comparison/v1":
        _SeekerComparison.model_validate(document)
    elif schema == "evallab.trust_tier_census/v1":
        _TrustTierCensus.model_validate(document)
    elif schema == "evallab.hackbook_qc/v1":
        _HackbookQc.model_validate(document)
    elif schema == "evallab.tune_cross_test/v1":
        _TuneCrossTest.model_validate(document)
    elif schema == "evallab.seeker_executed_differentials/v2":
        _SeekerExecutedDifferentials.model_validate(document)
    elif schema == "evallab.monitor_cascade/v1":
        _MonitorCascade.model_validate(document)
    elif schema == "evallab.swarm_bench/v1":
        if "arms" in document:
            raise ValueError("Comparison arms require an evallab.arm_comparison schema")
        _SwarmBenchmark.model_validate(document)
    else:
        raise ValueError("Unknown benchmark schema")
    return document
