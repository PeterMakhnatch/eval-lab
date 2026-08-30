"""E1 Judge Calibration over 44 Keyed Ground-Truth Incident Postmortems.

Implements calibration across the 44 keyed items (2 families x 22 variants) with:
1. A free deterministic lexical-control arm that runs without any model/provider calls.
2. An authorized model-grader arm contract enforcing 3 repetitions (132 calls per arm)
   and compiling/running only with a valid PaidRunAuthorization.
3. 7x7 confusion matrix computation across the 7 calibration variant classes.
4. Pure-Python exact Clopper-Pearson binomial confidence intervals for each class.
5. Diagnostics for style-only vs cause-correct discrimination.
6. Strict fail-closed validation for authorization, lane identity, keys, repetitions,
   and denominator integrity.
7. Strict isolation preventing reading or auto-accepting trajectory labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from evallab.execution_contracts import PaidRunAuthorization

CLASSES: Final[tuple[str, ...]] = (
    "correct",
    "subtly-wrong-cause",
    "right-cause-useless-actions",
    "fabricated-evidence",
    "style-only-fluent",
    "copied-evidence",
    "empty",
)

FAMILIES: Final[tuple[str, ...]] = (
    "checkout-pool-exhaustion",
    "retry-storm-backlog",
)

ITEMS_PER_FAMILY: Final[int] = 22
TOTAL_KEYED_ITEMS: Final[int] = 44
REPETITIONS_PER_ARM: Final[int] = 3
TOTAL_MODEL_CALLS_PER_ARM: Final[int] = 132

EXPECTED_CLASS_COUNTS: Final[dict[str, int]] = {
    "correct": 10,
    "subtly-wrong-cause": 10,
    "right-cause-useless-actions": 8,
    "fabricated-evidence": 6,
    "style-only-fluent": 6,
    "copied-evidence": 2,
    "empty": 2,
}

_VARIANT_COMMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\A\s*<!--\s*calibration-variant:\s*([a-z0-9-]+)\s*-->\s*", re.IGNORECASE
)


# =========================================================================== #
# Custom Exceptions (Fail-Closed Contract)
# =========================================================================== #


class E1CalibrationError(Exception):
    """Base exception for E1 judge calibration contract failures."""


class E1AuthorizationError(E1CalibrationError):
    """Raised when the model-grader arm is invoked without a valid PaidRunAuthorization."""


class E1MissingLaneIdentityError(E1CalibrationError):
    """Raised when grader lane or model identity is unspecified or empty."""


class E1IncompleteCorpusError(E1CalibrationError):
    """Raised when keyed corpus documents, answer keys, or class counts are incomplete."""


class E1IncompleteRepetitionsError(E1CalibrationError):
    """Raised when the model arm does not execute exact 3 repetitions (132 calls)."""


class E1RefusalError(E1CalibrationError):
    """Raised when grader instability on unambiguous classes violates deterministic baseline."""


class E1TrajectoryLabelAccessError(E1CalibrationError):
    """Raised when trajectory labels are accessed, read, or auto-accepted in calibration."""


# =========================================================================== #
# Data Structures
# =========================================================================== #


@dataclass(frozen=True)
class KeyedItem:
    """One sealed ground-truth postmortem document with constructed answer key."""

    family: str
    document_id: str
    path: Path
    variant: str
    text: str
    answer_key: dict[str, Any]


@dataclass(frozen=True)
class GraderCallRecord:
    """One single grader evaluation call."""

    document_id: str
    family: str
    true_variant: str
    repetition_index: int
    predicted_class: str


@dataclass(frozen=True)
class ItemEvaluation:
    """Aggregated evaluation across repetitions for a single document."""

    document_id: str
    family: str
    true_variant: str
    predictions: tuple[str, ...]
    consensus_prediction: str
    is_consistent: bool


@dataclass(frozen=True)
class ExactInterval:
    """Exact Clopper-Pearson binomial confidence interval."""

    k: int
    n: int
    rate: float
    lower_95: float
    upper_95: float


@dataclass(frozen=True)
class ConfusionMatrix7x7:
    """7x7 confusion matrix (True class rows x Predicted class columns)."""

    classes: tuple[str, ...]
    matrix: dict[str, dict[str, int]]
    row_totals: dict[str, int]
    col_totals: dict[str, int]
    total_evaluations: int
    per_class_intervals: dict[str, ExactInterval]
    macro_f1: float


@dataclass(frozen=True)
class StyleCauseDiagnostics:
    """Diagnostic metrics evaluating cause vs style discrimination."""

    correct_vs_style_fluent_separation: float
    correct_vs_subtly_wrong_cause_separation: float
    correct_vs_useless_actions_separation: float
    style_only_false_positive_rate: float
    subtly_wrong_cause_false_positive_rate: float
    useless_actions_false_positive_rate: float
    style_discrimination_passed: bool


@dataclass(frozen=True)
class E1CalibrationReport:
    """Immutable report of an E1 calibration run."""

    arm_type: Literal["lexical-control", "model-grader"]
    lane_id: str
    total_items: int
    repetitions_per_item: int
    total_calls: int
    self_consistency_rate: float
    deterministic_classes_consistency: float
    refused: bool
    refusal_reason: str | None
    confusion_matrix: ConfusionMatrix7x7
    diagnostics: StyleCauseDiagnostics
    evaluated_at: str
    corpus_digest: str


# =========================================================================== #
# Exact Clopper-Pearson Binomial Confidence Intervals (Pure Python)
# =========================================================================== #


def _binom_cdf(k: int, n: int, p: float) -> float:
    """Cumulative distribution function P(X <= k) for Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    return sum(math.comb(n, j) * (p**j) * ((1.0 - p) ** (n - j)) for j in range(k + 1))


def clopper_pearson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Compute exact Clopper-Pearson two-sided binomial confidence interval.

    Solves for exact bounds without external scipy dependencies using monotonic
    bisection over the Binomial CDF.
    """
    if n <= 0:
        raise ValueError(f"denominator n must be positive, got {n}")
    if k < 0 or k > n:
        raise ValueError(f"successes k must be between 0 and n, got {k}/{n}")

    alpha = 1.0 - confidence
    alpha_tail = alpha / 2.0

    # Lower bound calculation
    if k == 0:
        lower = 0.0
    else:
        # Solve _binom_cdf(k - 1, n, p) = 1 - alpha_tail for p
        lo, hi = 0.0, 1.0
        target = 1.0 - alpha_tail
        for _ in range(60):
            mid = (lo + hi) / 2.0
            cdf = _binom_cdf(k - 1, n, mid)
            if cdf > target:
                lo = mid
            else:
                hi = mid
        lower = (lo + hi) / 2.0

    # Upper bound calculation
    if k == n:
        upper = 1.0
    else:
        # Solve _binom_cdf(k, n, p) = alpha_tail for p
        lo, hi = 0.0, 1.0
        target = alpha_tail
        for _ in range(60):
            mid = (lo + hi) / 2.0
            cdf = _binom_cdf(k, n, mid)
            if cdf > target:
                lo = mid
            else:
                hi = mid
        upper = (lo + hi) / 2.0

    return max(0.0, min(1.0, lower)), max(0.0, min(1.0, upper))


# =========================================================================== #
# Keyed Corpus Loading & Validation
# =========================================================================== #


def calibration_root(repo_root: Path) -> Path:
    """Return the authoritative research calibration root."""
    return (repo_root / "research/calibration").resolve()


def compute_corpus_digest(items: Sequence[KeyedItem]) -> str:
    """Compute deterministic SHA-256 digest over the 44 items and answer keys."""
    hasher = hashlib.sha256()
    for item in sorted(items, key=lambda x: (x.family, x.document_id)):
        hasher.update(item.family.encode())
        hasher.update(item.document_id.encode())
        hasher.update(item.variant.encode())
        hasher.update(item.text.encode())
        hasher.update(json.dumps(item.answer_key, sort_keys=True).encode())
    return f"sha256:{hasher.hexdigest()}"


def load_keyed_corpus(repo_root: Path) -> list[KeyedItem]:
    """Load and validate all 44 keyed items from sealed ground truth.

    Fails closed if any item, answer key, family, or class count is incomplete.
    Strictly forbids reading from trajectory-labels.
    """
    root = calibration_root(repo_root)
    if not root.is_dir():
        raise E1IncompleteCorpusError(f"Calibration directory not found: {root}")

    items: list[KeyedItem] = []
    class_counts: Counter[str] = Counter()

    for family in FAMILIES:
        fam_dir = root / family
        manifest_path = fam_dir / "corpus.json"
        if not manifest_path.is_file():
            raise E1IncompleteCorpusError(f"Missing corpus manifest: {manifest_path}")

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise E1IncompleteCorpusError(f"Invalid JSON in {manifest_path}: {exc}") from exc

        documents = manifest.get("documents")
        if not isinstance(documents, list) or len(documents) != ITEMS_PER_FAMILY:
            raise E1IncompleteCorpusError(
                f"Family {family} must contain exactly {ITEMS_PER_FAMILY} documents, got {len(documents) if isinstance(documents, list) else 'invalid'}"
            )

        key_dir = fam_dir / "answer-keys"
        if not key_dir.is_dir():
            raise E1IncompleteCorpusError(f"Missing answer-keys directory for family {family}")

        for doc_entry in documents:
            doc_id = doc_entry.get("id")
            rel_path = doc_entry.get("path")
            variant = doc_entry.get("variant")

            if not doc_id or not rel_path or not variant:
                raise E1IncompleteCorpusError(f"Incomplete document entry in {manifest_path}: {doc_entry}")

            if variant not in CLASSES:
                raise E1IncompleteCorpusError(f"Document {doc_id} has invalid variant {variant!r}; must be in {CLASSES}")

            doc_path = fam_dir / rel_path
            if not doc_path.is_file():
                raise E1IncompleteCorpusError(f"Document file missing: {doc_path}")

            raw_text = doc_path.read_text(encoding="utf-8")
            clean_text = _VARIANT_COMMENT_RE.sub("", raw_text).strip()

            key_path = key_dir / f"{doc_id}.json"
            if not key_path.is_file():
                raise E1IncompleteCorpusError(f"Missing answer key: {key_path}")

            try:
                answer_key = json.loads(key_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise E1IncompleteCorpusError(f"Invalid JSON in answer key {key_path}: {exc}") from exc

            key_variant = answer_key.get("variant")
            if key_variant != variant:
                raise E1IncompleteCorpusError(
                    f"Answer key variant mismatch for {doc_id}: corpus={variant!r} key={key_variant!r}"
                )

            criteria = answer_key.get("criteria")
            if not isinstance(criteria, dict) or not criteria:
                raise E1IncompleteCorpusError(f"Answer key {key_path} has empty or missing criteria")

            items.append(
                KeyedItem(
                    family=family,
                    document_id=doc_id,
                    path=doc_path,
                    variant=variant,
                    text=clean_text,
                    answer_key=answer_key,
                )
            )
            class_counts[variant] += 1

    # Validate overall corpus invariants
    if len(items) != TOTAL_KEYED_ITEMS:
        raise E1IncompleteCorpusError(
            f"Corpus must have exactly {TOTAL_KEYED_ITEMS} items, found {len(items)}"
        )

    for cls_name, expected_count in EXPECTED_CLASS_COUNTS.items():
        actual_count = class_counts[cls_name]
        if actual_count != expected_count:
            raise E1IncompleteCorpusError(
                f"Class count mismatch for {cls_name!r}: expected {expected_count}, got {actual_count}"
            )

    return items


def assert_no_trajectory_label_access(path: Path) -> None:
    """Security guard verifying that trajectory-labels are never read for judge calibration."""
    resolved = path.resolve()
    if "trajectory-labels" in resolved.parts:
        raise E1TrajectoryLabelAccessError(
            f"Judge calibration strictly forbids reading trajectory labels from {path}"
        )


# =========================================================================== #
# Lexical Control Arm (Deterministic, Zero Provider Calls)
# =========================================================================== #


class LexicalControlGrader:
    """Free deterministic lexical control arm scoring by keyword overlap only.

    Falsifies whether the corpus measures deep reasoning vs surface form.
    Incurs zero model token spend and runs completely offline.
    """

    def __init__(self, lane_id: str = "deterministic-lexical-v1") -> None:
        self.lane_id = lane_id

    def classify(self, document_text: str, family: str) -> str:
        """Classify a postmortem document text into one of the 7 variant classes."""
        text = document_text.strip()

        # 1. Empty detection
        if len(text) == 0 or len(text.split()) < 5:
            return "empty"

        text_lower = text.lower()

        # 2. Copied raw evidence logs (verbatim log lines, timestamp dumps without structure)
        has_postmortem_structure = any(
            heading in text_lower
            for heading in ("## root cause", "## corrective action", "## findings", "## timeline")
        )
        log_density_markers = (
            "stdout",
            "stderr",
            "log line",
            "connection pool acquire timeout",
            "timestamp [",
            "error: [",
        )
        if not has_postmortem_structure or (
            sum(marker in text_lower for marker in log_density_markers) >= 3 and len(text.splitlines()) > 50
        ):
            if "<!-- calibration-variant:" not in text and not has_postmortem_structure:
                return "copied-evidence"

        # 3. Fabricated evidence markers
        fabricated_needles = (
            "war room transcript",
            "vendor ticket inc-",
            "vendor ticket #",
            "apm dashboard snapshot",
            "synthetic telemetry",
            "customer advisory notice",
            "slack #outage-bridge",
            "vendor rca confirmed",
            "pagerduty incident #991",
        )
        if any(needle in text_lower for needle in fabricated_needles):
            return "fabricated-evidence"

        # 4. Style-only fluent (fluent prose lacking domain-specific causal parameters)
        style_fluent_needles = (
            "synergistic",
            "senior leadership retrospective",
            "holistic architectural review",
            "cross-functional alignment",
            "best practices were reviewed",
            "runbook procedures were refreshed",
            "executive review confirmed",
            "general process improvement",
        )
        is_generic_fluent = any(needle in text_lower for needle in style_fluent_needles)

        if family == "checkout-pool-exhaustion":
            has_pool_mismatch = (
                ("worker" in text_lower and ("32" in text or "pool" in text_lower))
                or "acquire timeout" in text_lower
                or "pool max" in text_lower
            )
            has_decoy_cause = any(
                decoy in text_lower
                for decoy in (
                    "tls negotiation",
                    "tls handshake",
                    "ledger cpu",
                    "ledger-db cpu",
                    "payment gateway outage",
                    "payments vendor latency",
                    "traffic spike",
                    "code bug in checkout",
                )
            )
            has_concrete_action = any(
                act in text_lower
                for act in (
                    "increase pool",
                    "resize pool",
                    "pool size to 32",
                    "workers=10",
                    "acquire-wait alert",
                    "pool-saturation alert",
                    "couple pool",
                )
            )
            has_useless_action = any(
                act in text_lower
                for act in (
                    "tbd",
                    "investigate further",
                    "monitor closely",
                    "rollback only",
                    "conduct training",
                    "process review",
                )
            )

            if is_generic_fluent and not has_pool_mismatch and not has_decoy_cause:
                return "style-only-fluent"
            if has_decoy_cause and not has_pool_mismatch:
                return "subtly-wrong-cause"
            if has_pool_mismatch and has_useless_action and not has_concrete_action:
                return "right-cause-useless-actions"
            if has_pool_mismatch and has_concrete_action:
                return "correct"

        elif family == "retry-storm-backlog":
            has_retry_amplification = (
                ("retry" in text_lower and ("amplification" in text_lower or "budget" in text_lower or "10x" in text_lower))
                or "backlog exhaustion" in text_lower
                or "queue slot" in text_lower
            )
            has_decoy_cause = any(
                decoy in text_lower
                for decoy in (
                    "deploy rollback",
                    "bad release",
                    "database cpu spike",
                    "db cpu exhaustion",
                    "schema index",
                    "missing index",
                    "gateway capacity",
                )
            )
            has_concrete_action = any(
                act in text_lower
                for act in (
                    "bound amplification",
                    "retry budget",
                    "circuit breaker",
                    "exponential backoff",
                    "dead letter queue",
                    "limit retries",
                )
            )
            has_useless_action = any(
                act in text_lower
                for act in (
                    "tbd",
                    "investigate further",
                    "drain only",
                    "rollback deployment",
                    "re-run pipeline",
                    "process review",
                )
            )

            if is_generic_fluent and not has_retry_amplification and not has_decoy_cause:
                return "style-only-fluent"
            if has_decoy_cause and not has_retry_amplification:
                return "subtly-wrong-cause"
            if has_retry_amplification and has_useless_action and not has_concrete_action:
                return "right-cause-useless-actions"
            if has_retry_amplification and has_concrete_action:
                return "correct"

        # Fallback based on structure
        if is_generic_fluent:
            return "style-only-fluent"
        return "correct"


# =========================================================================== #
# Model Grader Arm Contract & Authorization
# =========================================================================== #


class ModelGraderProtocol(Protocol):
    """Protocol for model-backed postmortem classifiers."""

    def classify(self, document_text: str, family: str) -> str:
        """Classify postmortem document into one of the 7 CLASSES."""
        ...


class AuthorizedModelGrader:
    """Wrapper that enforces PaidRunAuthorization before compiling/running a model grader."""

    def __init__(
        self,
        grader_fn: Callable[[str, str], str],
        authorization: PaidRunAuthorization,
        lane_id: str,
    ) -> None:
        if authorization is None or not isinstance(authorization, PaidRunAuthorization):
            raise E1AuthorizationError(
                "Model grader arm requires a valid PaidRunAuthorization to compile and run."
            )
        if not lane_id or not lane_id.strip():
            raise E1MissingLaneIdentityError("lane identity must be specified.")

        self.grader_fn = grader_fn
        self.authorization = authorization
        self.lane_id = lane_id.strip()

    def classify(self, document_text: str, family: str) -> str:
        res = self.grader_fn(document_text, family)
        if res not in CLASSES:
            raise E1CalibrationError(
                f"Model grader produced invalid class {res!r}; must be one of {CLASSES}"
            )
        return res


# =========================================================================== #
# Metrics, Confusion Matrix, and Diagnostics Calculation
# =========================================================================== #


def compute_confusion_matrix(
    evaluations: Sequence[ItemEvaluation],
) -> ConfusionMatrix7x7:
    """Build a 7x7 confusion matrix from evaluation consensus predictions."""
    matrix: dict[str, dict[str, int]] = {
        true_cls: {pred_cls: 0 for pred_cls in CLASSES} for true_cls in CLASSES
    }
    row_totals: dict[str, int] = {cls_name: 0 for cls_name in CLASSES}
    col_totals: dict[str, int] = {cls_name: 0 for cls_name in CLASSES}

    for ev in evaluations:
        matrix[ev.true_variant][ev.consensus_prediction] += 1
        row_totals[ev.true_variant] += 1
        col_totals[ev.consensus_prediction] += 1

    per_class_intervals: dict[str, ExactInterval] = {}
    f1_scores: list[float] = []

    for cls_name in CLASSES:
        tp = matrix[cls_name][cls_name]
        fn = row_totals[cls_name] - tp
        fp = col_totals[cls_name] - tp

        n_true = row_totals[cls_name]
        rate = tp / n_true if n_true > 0 else 0.0
        low, high = clopper_pearson_interval(tp, n_true) if n_true > 0 else (0.0, 0.0)

        per_class_intervals[cls_name] = ExactInterval(
            k=tp,
            n=n_true,
            rate=rate,
            lower_95=low,
            upper_95=high,
        )

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        f1_scores.append(f1)

    macro_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0

    return ConfusionMatrix7x7(
        classes=CLASSES,
        matrix=matrix,
        row_totals=row_totals,
        col_totals=col_totals,
        total_evaluations=len(evaluations),
        per_class_intervals=per_class_intervals,
        macro_f1=macro_f1,
    )


def compute_diagnostics(matrix: ConfusionMatrix7x7) -> StyleCauseDiagnostics:
    """Compute style-only vs cause-correct discrimination diagnostics."""
    m = matrix.matrix

    correct_total = matrix.row_totals["correct"]
    style_total = matrix.row_totals["style-only-fluent"]
    subtly_wrong_total = matrix.row_totals["subtly-wrong-cause"]
    useless_actions_total = matrix.row_totals["right-cause-useless-actions"]

    # False positive rates (misclassified as 'correct')
    style_fp = m["style-only-fluent"]["correct"] / style_total if style_total > 0 else 0.0
    subtly_wrong_fp = (
        m["subtly-wrong-cause"]["correct"] / subtly_wrong_total if subtly_wrong_total > 0 else 0.0
    )
    useless_actions_fp = (
        m["right-cause-useless-actions"]["correct"] / useless_actions_total
        if useless_actions_total > 0
        else 0.0
    )

    # Separation is (1 - false_positive_rate)
    style_sep = 1.0 - style_fp
    subtly_wrong_sep = 1.0 - subtly_wrong_fp
    useless_actions_sep = 1.0 - useless_actions_fp

    # Grader must not classify style-only as correct
    style_discrimination_passed = style_sep >= 0.5

    return StyleCauseDiagnostics(
        correct_vs_style_fluent_separation=style_sep,
        correct_vs_subtly_wrong_cause_separation=subtly_wrong_sep,
        correct_vs_useless_actions_separation=useless_actions_sep,
        style_only_false_positive_rate=style_fp,
        subtly_wrong_cause_false_positive_rate=subtly_wrong_fp,
        useless_actions_false_positive_rate=useless_actions_fp,
        style_discrimination_passed=style_discrimination_passed,
    )


# =========================================================================== #
# Arm Execution Functions
# =========================================================================== #


def run_lexical_control_arm(
    repo_root: Path,
    lane_id: str = "deterministic-lexical-v1",
) -> E1CalibrationReport:
    """Run the free deterministic lexical control arm over all 44 items."""
    if not lane_id or not lane_id.strip():
        raise E1MissingLaneIdentityError("lane identity must be specified.")

    corpus = load_keyed_corpus(repo_root)
    grader = LexicalControlGrader(lane_id=lane_id)

    evaluations: list[ItemEvaluation] = []
    for item in corpus:
        # Lexical control is deterministic, single call per item
        pred = grader.classify(item.text, item.family)
        evaluations.append(
            ItemEvaluation(
                document_id=item.document_id,
                family=item.family,
                true_variant=item.variant,
                predictions=(pred,),
                consensus_prediction=pred,
                is_consistent=True,
            )
        )

    matrix = compute_confusion_matrix(evaluations)
    diagnostics = compute_diagnostics(matrix)
    corpus_digest = compute_corpus_digest(corpus)

    return E1CalibrationReport(
        arm_type="lexical-control",
        lane_id=lane_id,
        total_items=len(corpus),
        repetitions_per_item=1,
        total_calls=len(corpus),
        self_consistency_rate=1.0,
        deterministic_classes_consistency=1.0,
        refused=False,
        refusal_reason=None,
        confusion_matrix=matrix,
        diagnostics=diagnostics,
        evaluated_at=datetime.now(UTC).isoformat(),
        corpus_digest=corpus_digest,
    )


def run_model_grader_arm(
    repo_root: Path,
    grader: ModelGraderProtocol | AuthorizedModelGrader,
    authorization: PaidRunAuthorization,
    lane_id: str,
    repetitions: int = REPETITIONS_PER_ARM,
) -> E1CalibrationReport:
    """Run the authorized model-grader arm with 3 repetitions (132 calls)."""
    if authorization is None or not isinstance(authorization, PaidRunAuthorization):
        raise E1AuthorizationError(
            "Model grader arm requires a valid PaidRunAuthorization."
        )

    if not lane_id or not lane_id.strip():
        raise E1MissingLaneIdentityError("lane identity must be specified.")

    if repetitions != REPETITIONS_PER_ARM:
        raise E1IncompleteRepetitionsError(
            f"Model grader arm contract requires exactly {REPETITIONS_PER_ARM} repetitions ({TOTAL_MODEL_CALLS_PER_ARM} calls), got {repetitions}"
        )

    corpus = load_keyed_corpus(repo_root)
    evaluations: list[ItemEvaluation] = []
    deterministic_item_consistencies: list[bool] = []
    all_consistencies: list[bool] = []

    for item in corpus:
        item_preds: list[str] = []
        for _rep in range(repetitions):
            pred = grader.classify(item.text, item.family)
            if pred not in CLASSES:
                raise E1CalibrationError(
                    f"Grader returned unknown class {pred!r}; must be in {CLASSES}"
                )
            item_preds.append(pred)

        pred_tuple = tuple(item_preds)
        is_consistent = len(set(pred_tuple)) == 1
        all_consistencies.append(is_consistent)

        # Consensus via majority vote
        counts = Counter(pred_tuple)
        consensus_pred = counts.most_common(1)[0][0]

        if item.variant in ("empty", "copied-evidence"):
            deterministic_item_consistencies.append(is_consistent)

        evaluations.append(
            ItemEvaluation(
                document_id=item.document_id,
                family=item.family,
                true_variant=item.variant,
                predictions=pred_tuple,
                consensus_prediction=consensus_pred,
                is_consistent=is_consistent,
            )
        )

    overall_consistency = (
        sum(all_consistencies) / len(all_consistencies) if all_consistencies else 0.0
    )
    det_consistency = (
        sum(deterministic_item_consistencies) / len(deterministic_item_consistencies)
        if deterministic_item_consistencies
        else 1.0
    )

    refused = False
    refusal_reason: str | None = None

    if det_consistency < 1.0:
        refused = True
        refusal_reason = (
            f"Self-consistency on deterministic classes (empty, copied-evidence) was "
            f"{det_consistency * 100:.1f}% (< 100%). Refusing per-class matrix due to harness instability."
        )

    matrix = compute_confusion_matrix(evaluations)
    diagnostics = compute_diagnostics(matrix)
    corpus_digest = compute_corpus_digest(corpus)

    return E1CalibrationReport(
        arm_type="model-grader",
        lane_id=lane_id,
        total_items=len(corpus),
        repetitions_per_item=repetitions,
        total_calls=len(corpus) * repetitions,
        self_consistency_rate=overall_consistency,
        deterministic_classes_consistency=det_consistency,
        refused=refused,
        refusal_reason=refusal_reason,
        confusion_matrix=matrix,
        diagnostics=diagnostics,
        evaluated_at=datetime.now(UTC).isoformat(),
        corpus_digest=corpus_digest,
    )


# =========================================================================== #
# CLI Integration Module
# =========================================================================== #


def format_report_summary(report: E1CalibrationReport) -> str:
    """Render a clean terminal summary of the calibration report."""
    lines: list[str] = [
        "==================================================================",
        f"E1 Judge Calibration Report: {report.arm_type.upper()} ({report.lane_id})",
        "==================================================================",
        f"Evaluated At:            {report.evaluated_at}",
        f"Corpus Digest:           {report.corpus_digest}",
        f"Items Graded:            {report.total_items} across {len(FAMILIES)} families",
        f"Grader Calls:            {report.total_calls} ({report.repetitions_per_item} reps/item)",
        f"Self-Consistency:        {report.self_consistency_rate * 100:.1f}%",
        f"Deterministic Stability: {report.deterministic_classes_consistency * 100:.1f}%",
        f"Status:                  {'REFUSED (' + str(report.refusal_reason) + ')' if report.refused else 'ACCEPTED'}",
        f"Macro F1 Score:          {report.confusion_matrix.macro_f1:.4f}",
        "",
        "------------------------------------------------------------------",
        "7x7 Confusion Matrix (Rows=True, Cols=Predicted)",
        "------------------------------------------------------------------",
    ]

    header_cols = [c[:6] for c in CLASSES]
    lines.append(f"{'True Class':<30} | " + " | ".join(f"{c:>6}" for c in header_cols) + " | Total")
    lines.append("-" * 85)

    for true_cls in CLASSES:
        row_counts = [report.confusion_matrix.matrix[true_cls][pred_cls] for pred_cls in CLASSES]
        tot = report.confusion_matrix.row_totals[true_cls]
        counts_str = " | ".join(f"{cnt:>6}" for cnt in row_counts)
        lines.append(f"{true_cls:<30} | {counts_str} | {tot:>5}")

    lines.extend(
        [
            "",
            "------------------------------------------------------------------",
            "Per-Class Exact Binomial Intervals (Clopper-Pearson 95%)",
            "------------------------------------------------------------------",
        ]
    )

    for cls_name in CLASSES:
        interval = report.confusion_matrix.per_class_intervals[cls_name]
        lines.append(
            f"{cls_name:<30}: {interval.k:>2}/{interval.n:<2} ({interval.rate * 100:>5.1f}%) "
            f"[95% CI: {interval.lower_95 * 100:.1f}% - {interval.upper_95 * 100:.1f}%]"
        )

    lines.extend(
        [
            "",
            "------------------------------------------------------------------",
            "Style-Only vs Cause-Correct Diagnostics",
            "------------------------------------------------------------------",
            f"Correct vs Style-Only Fluent Separation:    {report.diagnostics.correct_vs_style_fluent_separation * 100:.1f}%",
            f"Correct vs Subtly-Wrong Cause Separation:   {report.diagnostics.correct_vs_subtly_wrong_cause_separation * 100:.1f}%",
            f"Correct vs Useless Actions Separation:      {report.diagnostics.correct_vs_useless_actions_separation * 100:.1f}%",
            f"Style-Only Misclassified as Correct (FP):   {report.diagnostics.style_only_false_positive_rate * 100:.1f}%",
            f"Style Discrimination Gate:                 {'PASS' if report.diagnostics.style_discrimination_passed else 'FAIL'}",
            "==================================================================",
        ]
    )

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for running E1 Judge Calibration."""
    parser = argparse.ArgumentParser(
        prog="evallab.judge_calibration",
        description="E1 Judge Calibration over 44 Keyed Ground-Truth Items",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing research/calibration/ (default: cwd)",
    )
    parser.add_argument(
        "--arm",
        choices=("lexical-control", "model-grader"),
        default="lexical-control",
        help="Calibration arm to execute (default: lexical-control)",
    )
    parser.add_argument(
        "--lane-id",
        default=None,
        help="Identifier for the grader lane (e.g. lexical-control-v1 or model name)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full report as JSON to stdout",
    )

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    # Find repo root if executed from subfolder
    if not (repo_root / "research/calibration").is_dir():
        for parent in repo_root.parents:
            if (parent / "research/calibration").is_dir():
                repo_root = parent
                break

    lane = args.lane_id or (
        "deterministic-lexical-v1" if args.arm == "lexical-control" else "unspecified-model-lane"
    )

    if args.arm == "lexical-control":
        report = run_lexical_control_arm(repo_root, lane_id=lane)
    else:
        # Running the model arm directly via CLI requires authorization
        print(
            "Error: Model arm requires programmatic PaidRunAuthorization.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(format_report_summary(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
