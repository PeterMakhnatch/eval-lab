"""Deterministic small-sample statistical primitives for Analysis Pipeline v1."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from evallab.cohort import wilson_interval as _cohort_wilson_interval
from evallab.schemas import ContractModel

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class AnalysisStatus(StrEnum):
    VALID = "VALID"
    REFUSAL = "REFUSAL"


class RefusalCode(StrEnum):
    DUPLICATE_ASSIGNMENT_UNIT = "DUPLICATE_ASSIGNMENT_UNIT"
    MISSING_PAIR_ARM = "MISSING_PAIR_ARM"
    UNDERFILLED_REPEATS = "UNDERFILLED_REPEATS"
    INVALID_BINARY_INPUT = "INVALID_BINARY_INPUT"
    ZERO_OPPORTUNITY = "ZERO_OPPORTUNITY"
    CAPTURE_INCOMPLETE = "CAPTURE_INCOMPLETE"
    ZERO_VARIANCE = "ZERO_VARIANCE"
    UNDERPOWERED = "UNDERPOWERED"


def canonical_digest(value: object) -> str:
    payload_value: object = (
        value.model_dump(mode="json", exclude_none=False) if isinstance(value, BaseModel) else value
    )
    payload = json.dumps(
        payload_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class PairedBinaryInput(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_unit_id: str = Field(min_length=1)
    arm_a_outcome: bool | int | None
    arm_b_outcome: bool | int | None
    capture_complete: bool = True


class BinaryArmObservation(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_unit_id: str = Field(min_length=1)
    arm_id: str = Field(min_length=1)
    outcome: bool | int | None
    capture_complete: bool = True


class PairedBinaryContrastResult(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    refusal_code: RefusalCode | None = None
    n_pairs: int = 0
    n_discordant: int = 0
    concordant_success_a_b: int = 0
    discordant_a_only: int = 0
    discordant_b_only: int = 0
    concordant_failure_a_b: int = 0
    risk_difference: float | None = None
    risk_difference_interval_lower: float | None = None
    risk_difference_interval_upper: float | None = None
    exact_p_value: float | None = None
    min_attainable_p_value: float | None = None
    design_floor_p_value: float | None = None
    is_design_floor: bool = False
    design_floor_limited: bool = False
    result_digest: Digest


def _paired_refusal(code: RefusalCode) -> PairedBinaryContrastResult:
    body = {
        "status": AnalysisStatus.REFUSAL,
        "refusal_code": code,
        "n_pairs": 0,
        "n_discordant": 0,
        "concordant_success_a_b": 0,
        "discordant_a_only": 0,
        "discordant_b_only": 0,
        "concordant_failure_a_b": 0,
        "risk_difference": None,
        "risk_difference_interval_lower": None,
        "risk_difference_interval_upper": None,
        "exact_p_value": None,
        "min_attainable_p_value": None,
        "design_floor_p_value": None,
        "is_design_floor": False,
        "design_floor_limited": False,
    }
    return PairedBinaryContrastResult(**body, result_digest=canonical_digest(body))


def exact_paired_binary_contrast(
    observations: Iterable[PairedBinaryInput | BinaryArmObservation],
    arm_a_id: str = "a",
    arm_b_id: str = "b",
    confidence_level: float = 0.95,
) -> PairedBinaryContrastResult:
    obs_list = list(observations)
    if not obs_list:
        return _paired_refusal(RefusalCode.ZERO_OPPORTUNITY)

    pairs: list[tuple[str, int, int]] = []

    if isinstance(obs_list[0], PairedBinaryInput):
        seen_units: set[str] = set()
        for item in obs_list:
            if not isinstance(item, PairedBinaryInput):
                return _paired_refusal(RefusalCode.INVALID_BINARY_INPUT)
            if not item.capture_complete:
                return _paired_refusal(RefusalCode.CAPTURE_INCOMPLETE)
            if item.assignment_unit_id in seen_units:
                return _paired_refusal(RefusalCode.DUPLICATE_ASSIGNMENT_UNIT)
            seen_units.add(item.assignment_unit_id)
            if item.arm_a_outcome is None or item.arm_b_outcome is None:
                return _paired_refusal(RefusalCode.MISSING_PAIR_ARM)
            if item.arm_a_outcome not in (True, False, 0, 1) or item.arm_b_outcome not in (
                True,
                False,
                0,
                1,
            ):
                return _paired_refusal(RefusalCode.INVALID_BINARY_INPUT)
            pairs.append(
                (
                    item.assignment_unit_id,
                    int(bool(item.arm_a_outcome)),
                    int(bool(item.arm_b_outcome)),
                )
            )
    else:
        by_unit: dict[str, dict[str, Any]] = defaultdict(dict)
        for item in obs_list:
            if not isinstance(item, BinaryArmObservation):
                return _paired_refusal(RefusalCode.INVALID_BINARY_INPUT)
            if not item.capture_complete:
                return _paired_refusal(RefusalCode.CAPTURE_INCOMPLETE)
            if item.arm_id in by_unit[item.assignment_unit_id]:
                return _paired_refusal(RefusalCode.DUPLICATE_ASSIGNMENT_UNIT)
            by_unit[item.assignment_unit_id][item.arm_id] = item.outcome

        for unit_id, arms in by_unit.items():
            if arm_a_id not in arms or arm_b_id not in arms:
                return _paired_refusal(RefusalCode.MISSING_PAIR_ARM)
            out_a = arms[arm_a_id]
            out_b = arms[arm_b_id]
            if out_a is None or out_b is None:
                return _paired_refusal(RefusalCode.MISSING_PAIR_ARM)
            if out_a not in (True, False, 0, 1) or out_b not in (True, False, 0, 1):
                return _paired_refusal(RefusalCode.INVALID_BINARY_INPUT)
            pairs.append((unit_id, int(bool(out_a)), int(bool(out_b))))

    if not pairs:
        return _paired_refusal(RefusalCode.ZERO_OPPORTUNITY)

    a = sum(1 for _, out_a, out_b in pairs if out_a == 1 and out_b == 1)
    b = sum(1 for _, out_a, out_b in pairs if out_a == 1 and out_b == 0)
    c = sum(1 for _, out_a, out_b in pairs if out_a == 0 and out_b == 1)
    d = sum(1 for _, out_a, out_b in pairs if out_a == 0 and out_b == 0)

    n_pairs = a + b + c + d
    n_discordant = b + c
    risk_diff = (b - c) / n_pairs if n_pairs > 0 else 0.0

    # Newcombe paired score interval
    p1 = (a + b) / n_pairs
    p2 = (a + c) / n_pairs
    w1 = _cohort_wilson_interval(a + b, n_pairs) or (p1, p1)
    w2 = _cohort_wilson_interval(a + c, n_pairs) or (p2, p2)
    denom = math.sqrt((a + b) * (c + d) * (a + c) * (b + d))
    phi = (a * d - b * c) / denom if denom > 0 else 0.0
    l1, u1 = w1
    l2, u2 = w2
    d_hat = p1 - p2
    lower_term = (p1 - l1) ** 2 + (u2 - p2) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2)
    upper_term = (u1 - p1) ** 2 + (p2 - l2) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2)
    ci_lower = max(-1.0, d_hat - math.sqrt(max(0.0, lower_term)))
    ci_upper = min(1.0, d_hat + math.sqrt(max(0.0, upper_term)))

    # Exact McNemar / sign calculation
    if n_discordant == 0:
        exact_p = 1.0
        min_attainable_p = 1.0
        is_design_floor = False
        design_floor_limited = False
    else:
        k = min(b, c)
        prob_k = sum(math.comb(n_discordant, i) for i in range(k + 1)) * (0.5**n_discordant)
        exact_p = min(1.0, 2.0 * prob_k)
        min_attainable_p = 2.0 * (0.5**n_discordant)
        is_design_floor = k == 0
        design_floor_limited = min_attainable_p > 0.05

    design_floor_p = (2.0 * (0.5**n_pairs)) if n_pairs > 0 else 1.0

    body = {
        "status": AnalysisStatus.VALID,
        "refusal_code": None,
        "n_pairs": n_pairs,
        "n_discordant": n_discordant,
        "concordant_success_a_b": a,
        "discordant_a_only": b,
        "discordant_b_only": c,
        "concordant_failure_a_b": d,
        "risk_difference": risk_diff,
        "risk_difference_interval_lower": ci_lower,
        "risk_difference_interval_upper": ci_upper,
        "exact_p_value": exact_p,
        "min_attainable_p_value": min_attainable_p,
        "design_floor_p_value": design_floor_p,
        "is_design_floor": is_design_floor,
        "design_floor_limited": design_floor_limited,
    }
    return PairedBinaryContrastResult(**body, result_digest=canonical_digest(body))


class FisherExact2x2Result(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    refusal_code: RefusalCode | None = None
    table: tuple[tuple[int, int], tuple[int, int]]
    odds_ratio: float | None = None
    exact_p_value: float | None = None
    result_digest: Digest


def fisher_exact_2x2(
    table: Sequence[Sequence[int]] | tuple[tuple[int, int], tuple[int, int]],
) -> FisherExact2x2Result:
    a, b = table[0][0], table[0][1]
    c, d = table[1][0], table[1][1]
    if any(x < 0 for x in (a, b, c, d)):
        body = {
            "status": AnalysisStatus.REFUSAL,
            "refusal_code": RefusalCode.INVALID_BINARY_INPUT,
            "table": ((a, b), (c, d)),
            "odds_ratio": None,
            "exact_p_value": None,
        }
        return FisherExact2x2Result(**body, result_digest=canonical_digest(body))

    r1 = a + b
    r2 = c + d
    c1 = a + c
    n = r1 + r2

    if n == 0:
        body = {
            "status": AnalysisStatus.REFUSAL,
            "refusal_code": RefusalCode.ZERO_OPPORTUNITY,
            "table": ((a, b), (c, d)),
            "odds_ratio": None,
            "exact_p_value": None,
        }
        return FisherExact2x2Result(**body, result_digest=canonical_digest(body))

    # Odds ratio
    odds_ratio = (1.0 if a * d == 0 else float("inf")) if b * c == 0 else (a * d) / (b * c)

    # Hypergeometric exact test
    min_x = max(0, c1 - r2)
    max_x = min(r1, c1)

    def hypergeom_pmf(x: int) -> float:
        return (math.comb(r1, x) * math.comb(r2, c1 - x)) / math.comb(n, c1)

    p_obs = hypergeom_pmf(a)
    p_sum = sum(
        hypergeom_pmf(x) for x in range(min_x, max_x + 1) if hypergeom_pmf(x) <= p_obs + 1e-12
    )
    exact_p = min(1.0, p_sum)

    body = {
        "status": AnalysisStatus.VALID,
        "refusal_code": None,
        "table": ((a, b), (c, d)),
        "odds_ratio": odds_ratio,
        "exact_p_value": exact_p,
    }
    return FisherExact2x2Result(**body, result_digest=canonical_digest(body))


class WilsonIntervalResult(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    successes: int
    denominator: int
    proportion: float | None
    lower: float | None
    upper: float | None
    confidence_level: float


def wilson_score_interval(
    successes: int,
    denominator: int,
    confidence_level: float = 0.95,
) -> WilsonIntervalResult:
    if denominator <= 0:
        return WilsonIntervalResult(
            successes=successes,
            denominator=denominator,
            proportion=None,
            lower=None,
            upper=None,
            confidence_level=confidence_level,
        )
    z = 1.959963984540054 if abs(confidence_level - 0.95) < 1e-4 else 1.959963984540054
    bounds = _cohort_wilson_interval(successes, denominator, z=z)
    prop = successes / denominator
    lower, upper = bounds if bounds is not None else (None, None)
    return WilsonIntervalResult(
        successes=successes,
        denominator=denominator,
        proportion=prop,
        lower=lower,
        upper=upper,
        confidence_level=confidence_level,
    )


class RepeatCellInput(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: str = Field(min_length=1)
    successes: int = Field(ge=0)
    repeats: int = Field(gt=0)
    capture_complete: bool = True


class RepeatHeterogeneityReport(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    refusal_code: RefusalCode | None = None
    n_cells: int = 0
    repeats_per_cell: int = 0
    total_observations: int = 0
    total_successes: int = 0
    pooled_probability: float | None = None
    observed_success_distribution: dict[int, int] = Field(default_factory=dict)
    expected_success_distribution: dict[int, float] = Field(default_factory=dict)
    pearson_dispersion_statistic: float | None = None
    pearson_degrees_of_freedom: int | None = None
    dispersion_ratio: float | None = None
    dispersion_p_value: float | None = None
    raw_icc: float | None = None
    icc: float | None = None
    icc_clamped: bool = False
    no_detectable_heterogeneity: bool = False
    design_effect: float | None = None
    effective_n: float | None = None
    result_digest: Digest


def compute_design_effect(repeats_per_cell: int, icc: float) -> float:
    clamped_icc = max(0.0, min(1.0, icc))
    return 1.0 + (repeats_per_cell - 1) * clamped_icc


def _repeat_refusal(code: RefusalCode) -> RepeatHeterogeneityReport:
    body = {
        "status": AnalysisStatus.REFUSAL,
        "refusal_code": code,
        "n_cells": 0,
        "repeats_per_cell": 0,
        "total_observations": 0,
        "total_successes": 0,
        "pooled_probability": None,
        "observed_success_distribution": {},
        "expected_success_distribution": {},
        "pearson_dispersion_statistic": None,
        "pearson_degrees_of_freedom": None,
        "dispersion_ratio": None,
        "dispersion_p_value": None,
        "raw_icc": None,
        "icc": None,
        "icc_clamped": False,
        "no_detectable_heterogeneity": False,
        "design_effect": None,
        "effective_n": None,
    }
    return RepeatHeterogeneityReport(**body, result_digest=canonical_digest(body))


def analyze_repeat_heterogeneity(
    cells: Iterable[RepeatCellInput],
) -> RepeatHeterogeneityReport:
    cell_list = list(cells)
    if not cell_list:
        return _repeat_refusal(RefusalCode.ZERO_OPPORTUNITY)
    if any(not c.capture_complete for c in cell_list):
        return _repeat_refusal(RefusalCode.CAPTURE_INCOMPLETE)

    m = cell_list[0].repeats
    if m < 2 or any(c.repeats != m for c in cell_list):
        return _repeat_refusal(RefusalCode.UNDERFILLED_REPEATS)
    if any(c.successes > m for c in cell_list):
        return _repeat_refusal(RefusalCode.INVALID_BINARY_INPUT)

    n_cells = len(cell_list)
    total_obs = n_cells * m
    total_succ = sum(c.successes for c in cell_list)
    p_bar = total_succ / total_obs

    # Observed distribution
    observed_dist: dict[int, int] = {k: 0 for k in range(m + 1)}
    for c in cell_list:
        observed_dist[c.successes] += 1

    # Expected distribution under binomial
    expected_dist: dict[int, float] = {}
    for k in range(m + 1):
        prob_k = math.comb(m, k) * (p_bar**k) * ((1.0 - p_bar) ** (m - k))
        expected_dist[k] = round(n_cells * prob_k, 6)

    # Variance and dispersion
    if p_bar == 0.0 or p_bar == 1.0 or n_cells < 2:
        raw_icc = 0.0
        icc = 0.0
        icc_clamped = False
        no_detectable_het = True
        deff = 1.0
        eff_n = float(total_obs)
        body = {
            "status": AnalysisStatus.VALID,
            "refusal_code": None,
            "n_cells": n_cells,
            "repeats_per_cell": m,
            "total_observations": total_obs,
            "total_successes": total_succ,
            "pooled_probability": p_bar,
            "observed_success_distribution": observed_dist,
            "expected_success_distribution": expected_dist,
            "pearson_dispersion_statistic": 0.0,
            "pearson_degrees_of_freedom": n_cells - 1,
            "dispersion_ratio": 0.0,
            "dispersion_p_value": 1.0,
            "raw_icc": raw_icc,
            "icc": icc,
            "icc_clamped": icc_clamped,
            "no_detectable_heterogeneity": no_detectable_het,
            "design_effect": deff,
            "effective_n": eff_n,
        }
        return RepeatHeterogeneityReport(**body, result_digest=canonical_digest(body))

    # Pearson dispersion
    pearson_stat = sum(
        ((c.successes - m * p_bar) ** 2) / (m * p_bar * (1.0 - p_bar)) for c in cell_list
    )
    df = n_cells - 1
    disp_ratio = pearson_stat / df if df > 0 else 1.0

    # MoM ICC
    msb = (1.0 / (m * (n_cells - 1))) * sum((c.successes - m * p_bar) ** 2 for c in cell_list)
    msw = (1.0 / (n_cells * (m - 1))) * sum(c.successes * (m - c.successes) / m for c in cell_list)
    denom = msb + (m - 1) * msw
    raw_icc = (msb - msw) / denom if denom > 0 else 0.0

    if raw_icc <= 0.0:
        icc = 0.0
        icc_clamped = raw_icc < 0.0
        no_detectable_het = True
    elif raw_icc > 1.0:
        icc = 1.0
        icc_clamped = True
        no_detectable_het = False
    else:
        icc = raw_icc
        icc_clamped = False
        no_detectable_het = False

    deff = compute_design_effect(m, icc)
    eff_n = total_obs / deff

    body = {
        "status": AnalysisStatus.VALID,
        "refusal_code": None,
        "n_cells": n_cells,
        "repeats_per_cell": m,
        "total_observations": total_obs,
        "total_successes": total_succ,
        "pooled_probability": p_bar,
        "observed_success_distribution": observed_dist,
        "expected_success_distribution": expected_dist,
        "pearson_dispersion_statistic": pearson_stat,
        "pearson_degrees_of_freedom": df,
        "dispersion_ratio": disp_ratio,
        "dispersion_p_value": None,
        "raw_icc": raw_icc,
        "icc": icc,
        "icc_clamped": icc_clamped,
        "no_detectable_heterogeneity": no_detectable_het,
        "design_effect": deff,
        "effective_n": eff_n,
    }
    return RepeatHeterogeneityReport(**body, result_digest=canonical_digest(body))


class SequenceFidelityReport(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    refusal_code: RefusalCode | None = None
    sequence_a_len: int = 0
    sequence_b_len: int = 0
    is_identical: bool = False
    first_mismatch_index: int | None = None
    common_prefix_length: int = 0
    spearman_footrule_distance: float | None = None
    normalized_footrule_distance: float | None = None
    kendall_inversion_count: int | None = None
    normalized_kendall_distance: float | None = None
    set_intersection_size: int = 0
    set_union_size: int = 0
    jaccard_similarity: float | None = None
    coverage_a_in_b: float | None = None
    coverage_b_in_a: float | None = None
    duplicates_a_count: int = 0
    duplicates_b_count: int = 0
    duplicate_items_a: dict[str, int] = Field(default_factory=dict)
    duplicate_items_b: dict[str, int] = Field(default_factory=dict)
    result_digest: Digest


def compute_sequence_fidelity(
    seq_a: Sequence[Any],
    seq_b: Sequence[Any],
) -> SequenceFidelityReport:
    len_a, len_b = len(seq_a), len(seq_b)
    is_ident = list(seq_a) == list(seq_b)

    # First mismatch and prefix
    first_mismatch: int | None = None
    prefix_len = 0
    min_len = min(len_a, len_b)
    for i in range(min_len):
        if seq_a[i] == seq_b[i]:
            prefix_len += 1
        else:
            first_mismatch = i
            break
    if first_mismatch is None and len_a != len_b:
        first_mismatch = min_len

    # Duplicate tracking
    counts_a = Counter(str(x) for x in seq_a)
    counts_b = Counter(str(x) for x in seq_b)
    dup_a = {k: v for k, v in counts_a.items() if v > 1}
    dup_b = {k: v for k, v in counts_b.items() if v > 1}

    # Set coverage
    set_a = set(str(x) for x in seq_a)
    set_b = set(str(x) for x in seq_b)
    inter = set_a & set_b
    union = set_a | set_b
    jaccard = len(inter) / len(union) if union else 1.0
    cov_a = len(inter) / len(set_a) if set_a else 1.0
    cov_b = len(inter) / len(set_b) if set_b else 1.0

    # Footrule & Kendall
    pos_b: dict[Any, int] = {}
    for idx, item in enumerate(seq_b):
        if item not in pos_b:
            pos_b[item] = idx

    footrule: float | None = None
    norm_footrule: float | None = None
    kendall_inv: int | None = None
    norm_kendall: float | None = None

    if len_a > 0 and len_a == len_b and len(set_a) == len_a and set_a == set_b:
        # Exact permutation metrics
        fr = sum(abs(i - pos_b[item]) for i, item in enumerate(seq_a))
        footrule = float(fr)
        max_fr = math.floor((len_a**2) / 2) if len_a > 1 else 1.0
        norm_footrule = footrule / max_fr if max_fr > 0 else 0.0

        b_indices = [pos_b[x] for x in seq_a]
        inv_count = 0
        for i in range(len_a):
            for j in range(i + 1, len_a):
                if b_indices[i] > b_indices[j]:
                    inv_count += 1
        kendall_inv = inv_count
        max_inv = (len_a * (len_a - 1)) / 2 if len_a > 1 else 1.0
        norm_kendall = inv_count / max_inv if max_inv > 0 else 0.0

    body = {
        "status": AnalysisStatus.VALID,
        "refusal_code": None,
        "sequence_a_len": len_a,
        "sequence_b_len": len_b,
        "is_identical": is_ident,
        "first_mismatch_index": first_mismatch,
        "common_prefix_length": prefix_len,
        "spearman_footrule_distance": footrule,
        "normalized_footrule_distance": norm_footrule,
        "kendall_inversion_count": kendall_inv,
        "normalized_kendall_distance": norm_kendall,
        "set_intersection_size": len(inter),
        "set_union_size": len(union),
        "jaccard_similarity": jaccard,
        "coverage_a_in_b": cov_a,
        "coverage_b_in_a": cov_b,
        "duplicates_a_count": len_a - len(set_a),
        "duplicates_b_count": len_b - len(set_b),
        "duplicate_items_a": dup_a,
        "duplicate_items_b": dup_b,
    }
    return SequenceFidelityReport(**body, result_digest=canonical_digest(body))
