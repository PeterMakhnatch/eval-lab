"""Durable contracts for the insight engine.

Three kinds of records flow through the engine:

* **Claims** are atomic, checkable statements that an experiment report makes
  ("the adaptive attacker passes the original grader on 21 of 21 packages").
  An extractor agent proposes them from report text with a verbatim ``quote``;
  deterministic code checks the quote, the arithmetic and, when a ``recipe`` is
  given, recomputes the number from the experiment's own receipts.
* **Insight cards** are what analyst agents (lenses) write after reading many
  claims: cross-experiment connections, mechanisms, challenges to weak claims,
  anomalies, gaps and new experiment ideas. Every card cites claim ids, so a
  card can be checked against the ledger before anyone reads it.
* **Passes** record each agent invocation: which lens, which model route was
  requested and which model actually served it, and what came back.

Machine judgments (extractor output, cards, referee verdicts, board ranks) are
never acceptance. Only a named human review (``HumanReview``) accepts or
rejects an insight, with the same status vocabulary as ``evallab verdict``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel

SCHEMA_VERSION = 1

SourceKind = Literal["findings_log", "report", "receipt", "readme", "brief", "other"]
ClaimKind = Literal["measurement", "comparison", "qualitative", "correction", "method", "cost"]
ClaimStatus = Literal["author", "independent", "corrected", "refuted", "withdrawn", "unknown"]
Split = Literal["dev", "test", "mixed", "unknown"]
EntityKind = Literal["component", "dataset", "model", "mechanism", "task", "metric", "other"]
CheckStatus = Literal["pass", "warn", "fail"]
RecomputeStatus = Literal["reproduced", "partial", "differs", "not_recomputable"]
RecomputeField = Literal["k", "n", "value"]
Lens = Literal[
    "extractor",
    "connector",
    "skeptic",
    "theorist",
    "anomaly",
    "ideator",
    "referee",
    "synthesizer",
    "rederiver",
    "twinmaker",
]
AnalystLens = Literal["connector", "skeptic", "theorist", "anomaly", "ideator", "twinmaker"]
CorruptionType = Literal[
    "reversed_conclusion",
    "wrong_unit_of_analysis",
    "contradicted_connection",
    "non_discriminating_check",
    "correlation_as_cause",
    "overclaimed_scope",
]
InsightKind = Literal["connection", "mechanism", "challenge", "anomaly", "gap", "idea"]
InsightStatus = Literal[
    "check_failed",
    "checked",
    "refereed",
    "needs_revision",
    "rejected",
    "duplicate",
    "superseded",
]
SignalKind = Literal[
    "value_mismatch",
    "ci_mismatch",
    "p_mismatch",
    "recompute_differs",
    "extreme_small_n",
    "underpowered_null",
    "underpowered_claim",
    "drift",
    "contradiction",
    "correction_cascade",
    "coverage_gap",
    "multiplicity",
    "pseudo_replication",
]
SignalStatus = Literal["open", "consumed", "stale"]
PassStatus = Literal[
    "planned",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "parked_quota",
    "invalid_output",
]
PredictionStatus = Literal["open", "hit", "miss", "untestable"]
ReviewStatus = Literal["accepted", "rejected", "needs_evidence", "pending"]
FlawType = Literal[
    "non_independence",
    "multiple_comparisons",
    "small_n",
    "label_validity",
    "harness_confound",
    "definition_overclaim",
    "arithmetic",
    "missing_control",
    "in_sample",
    "refusal_accounting",
    "prompt_or_config_drift",
    "selection_effect",
    "measurement_validity",
    "leakage",
    "other",
]
StatOp = Literal[
    "wilson",
    "fisher",
    "mcnemar",
    "two_proportion",
    "rule_of_three",
    "bh",
    "min_n_mcnemar",
]

_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


def normalize_text(value: str) -> str:
    """Collapse Unicode and whitespace differences so quotes compare stably."""
    folded = unicodedata.normalize("NFKC", value)
    folded = (
        folded.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
    )
    return " ".join(folded.split())


def short_digest(*parts: str, length: int = 12) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def normalize_experiment_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("experiment id must not be empty")
    return cleaned.upper() if len(cleaned) <= 24 and " " not in cleaned else cleaned


class EntityRef(ContractModel):
    kind: EntityKind
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)


class SourceSection(ContractModel):
    section_id: str
    source_id: str
    heading: str
    ordinal: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    sha256: str
    as_of: datetime | None = None

    @model_validator(mode="after")
    def lines_ordered(self) -> SourceSection:
        if self.end_line < self.start_line:
            raise ValueError("section end_line precedes start_line")
        return self


class SourceRecord(ContractModel):
    source_id: str
    path: str
    family: str = Field(min_length=1)
    kind: SourceKind
    sha256: str
    size: int = Field(ge=0)
    mtime: datetime
    folder: str | None = None
    max_line: int | None = Field(default=None, ge=1)
    max_bytes: int = Field(default=400_000, ge=1)


class ComparisonStats(ContractModel):
    test: Literal[
        "mcnemar_exact",
        "fisher_exact",
        "sign",
        "permutation",
        "chi2",
        "bootstrap",
        "auroc",
        "other",
    ]
    b: int | None = Field(default=None, ge=0)
    c: int | None = Field(default=None, ge=0)
    k2: int | None = Field(default=None, ge=0)
    n2: int | None = Field(default=None, ge=0)
    p_value: float | None = Field(default=None, ge=0, le=1)
    baseline: str | None = None


class Recipe(ContractModel):
    """Declarative recomputation over files inside one experiment folder.

    ``count`` counts JSONL/JSON-array rows (optionally filtered by ``where``);
    ``value`` reads one JSON value by RFC 6901 ``pointer``; ``sum``/``mean``
    aggregate a numeric ``field`` over rows; ``distinct_count`` counts distinct
    ``field`` values; ``ratio`` divides two sub-recipes. ``target`` names the
    stated quantity to compare: ``k``, ``n`` or scalar ``value``. Count recipes
    default to ``k`` and ratio recipes to ``value``. A recipe validates only the
    quantity it targets (a ratio also validates k and n when its numerator and
    denominator equal them); ``also`` lists companion recipes, each with an explicit
    ``target``, that validate the claim's other stated quantities. ``where`` maps a dotted
    field path to an exact value, a list of allowed values, or an
    operator object such as ``{"$ne": x}``, ``{"$in": [...]}``,
    ``{"$exists": true}``, ``{"$gt": n}``, ``{"$contains": "text"}``.
    """

    op: Literal["count", "value", "sum", "mean", "distinct_count", "ratio"]
    target: Literal["k", "n", "value"] | None = None
    file: str | None = None
    pointer: str | None = None
    rows: str | None = None
    where: dict[str, Any] | None = None
    field: str | None = None
    num: Recipe | None = None
    den: Recipe | None = None
    also: list[Recipe] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self) -> Recipe:
        if self.op == "ratio":
            if self.num is None or self.den is None:
                raise ValueError("ratio recipes need num and den")
        elif self.file is None:
            raise ValueError(f"{self.op} recipes need a file")
        if self.op == "value" and self.pointer is None:
            raise ValueError("value recipes need a pointer")
        if self.op in {"sum", "mean", "distinct_count"} and self.field is None:
            raise ValueError(f"{self.op} recipes need a field")
        if any(extra.target is None or extra.also for extra in self.also):
            raise ValueError("companion recipes need a target and no companions of their own")
        if self.op == "ratio" and (
            (self.num is not None and self.num.also) or (self.den is not None and self.den.also)
        ):
            raise ValueError("companion recipes belong on the top-level recipe")
        return self


class ClaimRelation(ContractModel):
    target_experiment: str = Field(min_length=1)
    relation: Literal[
        "corrects",
        "refutes",
        "confirms",
        "replicates",
        "extends",
        "contradicts",
        "supersedes",
    ]
    note: str | None = None


class ExtractedClaim(ContractModel):
    """One claim as an extractor agent proposes it."""

    local_id: str = Field(min_length=1, max_length=40)
    experiment: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=8, max_length=600)
    kind: ClaimKind
    metric: str | None = Field(default=None, max_length=80)
    subject: str | None = Field(default=None, max_length=120)
    dataset: str | None = Field(default=None, max_length=120)
    condition: str | None = Field(default=None, max_length=200)
    models: list[str] = Field(default_factory=list)
    k: int | None = Field(default=None, ge=0)
    n: int | None = Field(default=None, ge=0)
    value: float | None = None
    unit: str | None = Field(default=None, max_length=40)
    ci_low: float | None = None
    ci_high: float | None = None
    comparison: ComparisonStats | None = None
    status: ClaimStatus = "author"
    relations: list[ClaimRelation] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    split: Split | None = None
    in_sample: bool | None = None
    quote: str = Field(min_length=4, max_length=1200)
    section_id: str = Field(min_length=1)
    recipe: Recipe | None = None

    @field_validator("experiment")
    @classmethod
    def experiment_id(cls, value: str) -> str:
        return normalize_experiment_id(value)

    @model_validator(mode="after")
    def counts_consistent(self) -> ExtractedClaim:
        if self.k is not None and self.n is not None and self.k > self.n:
            raise ValueError("k must not exceed n")
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("ci_low and ci_high come together")
        return self


class ExperimentRecord(ContractModel):
    exp_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    lane: str | None = None
    prs: list[str] = Field(default_factory=list)
    question: str | None = Field(default=None, max_length=600)
    models: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    spend_usd: float | None = Field(default=None, ge=0)
    folder: str | None = None
    status_text: str | None = Field(default=None, max_length=300)

    @field_validator("exp_id")
    @classmethod
    def experiment_id(cls, value: str) -> str:
        return normalize_experiment_id(value)


class ClaimExtraction(ContractModel):
    """The file an extractor pass writes (``out/claims.json``)."""

    schema_version: Literal[1] = 1
    source_id: str
    section_ids: list[str] = Field(min_length=1)
    experiments: list[ExperimentRecord] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    skipped_section_ids: list[str] = Field(default_factory=list)


class CheckResult(ContractModel):
    status: CheckStatus
    notes: list[str] = Field(default_factory=list)


class RecomputeResult(ContractModel):
    """What a receipt recipe established about one claim.

    ``reproduced`` means every stated quantity (k, n, value) was validated and the recipe
    has traceable provenance; ``partial`` means a match that leaves a stated quantity
    unchecked (``unchecked``) or whose pointer cannot be tied to the claim
    (``provenance_note``). Ledgers written before ``partial`` existed hold empty
    ``validated``/``unchecked`` lists and keep their stored status.
    """

    status: RecomputeStatus
    value: float | None = None
    stated: float | None = None
    detail: str = ""
    #: ``rounding`` when the receipt differs from the stated value only within its printed
    #: precision; the claim is then reproduced.
    note: Literal["rounding"] | None = None
    #: Size of the gap between receipt and stated value, absolute in compared units
    #: (``difference``) and spelled in the claim's unit (``difference_text``).
    difference: float | None = None
    difference_text: str = ""
    #: Stated quantities the receipt confirmed, and stated quantities it never read.
    validated: list[RecomputeField] = Field(default_factory=list)
    unchecked: list[RecomputeField] = Field(default_factory=list)
    #: ``weak_provenance``: a low-information value (0, 1, 100) matched at a pointer that
    #: names no cohort or metric token from the claim and carries no ``where`` filter.
    provenance_note: Literal["weak_provenance"] | None = None


class ClaimRecord(ContractModel):
    """A stored claim: the extractor's proposal plus deterministic results."""

    claim_id: str
    source_id: str
    section_sha256: str
    fingerprint: str
    as_of: datetime | None = None
    pass_id: str
    extracted_by: str
    created_at: datetime
    claim: ExtractedClaim
    check: CheckResult
    recompute: RecomputeResult | None = None
    stale: bool = False


class SignalRecord(ContractModel):
    """A deterministic statistical observation that analysts should look at."""

    signal_id: str
    kind: SignalKind
    score: float = Field(ge=0)
    title: str = Field(min_length=1, max_length=300)
    claim_ids: list[str] = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    status: SignalStatus = "open"
    created_at: datetime


class EvidenceRef(ContractModel):
    claim_id: str = Field(min_length=1)
    role: Literal["supports", "contradicts", "context"] = "supports"
    note: str | None = Field(default=None, max_length=400)


class ComputedStat(ContractModel):
    """A statistic an analyst computed with the engine's own ``stat`` tool."""

    op: StatOp
    args: dict[str, Any]
    result: dict[str, Any]


class RecipeResult(ContractModel):
    """A receipt value recomputed by ``q recipe`` and recorded on a card."""

    recipe: Recipe
    folder: str = Field(min_length=1)
    value: float


class PredictionSpec(ContractModel):
    statement: str = Field(min_length=8, max_length=500)
    metric: str | None = None
    subject: str | None = None
    dataset: str | None = None
    condition: str | None = None
    direction: Literal["higher", "lower", "equal", "at_least", "at_most", "between"]
    low: float | None = None
    high: float | None = None
    resolves_when: str = Field(min_length=8, max_length=400)

    @model_validator(mode="after")
    def bounds(self) -> PredictionSpec:
        if self.direction in {"at_least", "higher"} and self.low is None:
            raise ValueError(f"{self.direction} predictions need low")
        if self.direction in {"at_most", "lower"} and self.high is None:
            raise ValueError(f"{self.direction} predictions need high")
        if self.direction in {"between", "equal"} and (self.low is None or self.high is None):
            raise ValueError(f"{self.direction} predictions need low and high")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("prediction low exceeds high")
        return self


class ProposedExperiment(ContractModel):
    hypothesis: str = Field(min_length=8, max_length=600)
    primary_variable: str = Field(min_length=2, max_length=200)
    fixed_variables: list[str] = Field(min_length=1)
    arms: list[str] = Field(min_length=1)
    sample_size: int | None = Field(default=None, ge=1)
    est_cost_usd: float | None = Field(default=None, ge=0)
    cost_basis: str = Field(min_length=2, max_length=300)
    needs_approval: bool
    reuses_existing_data: bool
    expected_if_true: str = Field(min_length=4, max_length=400)
    expected_if_false: str = Field(min_length=4, max_length=400)
    stop_conditions: list[str] = Field(default_factory=list)


class ChallengeSpec(ContractModel):
    target_claim_ids: list[str] = Field(min_length=1)
    flaw: FlawType
    severity: Literal["blocker", "major", "minor"]
    check: str = Field(min_length=8, max_length=600)
    decision_at_risk: str = Field(min_length=4, max_length=400)


class InsightCard(ContractModel):
    """One analyst finding, as the lens writes it."""

    local_id: str = Field(min_length=1, max_length=40)
    kind: InsightKind
    title: str = Field(min_length=4, max_length=140)
    statement: str = Field(min_length=20, max_length=1200)
    evidence: list[EvidenceRef] = Field(min_length=1)
    observations: list[str] = Field(min_length=1)
    interpretation: str = Field(min_length=10, max_length=1500)
    why_not_obvious: str = Field(min_length=10, max_length=800)
    alternatives: list[str] = Field(min_length=1)
    computed: list[ComputedStat] = Field(default_factory=list)
    recipes: list[RecipeResult] = Field(default_factory=list)
    predictions: list[PredictionSpec] = Field(default_factory=list)
    proposed_experiment: ProposedExperiment | None = None
    challenge: ChallengeSpec | None = None
    builds_on: list[str] = Field(default_factory=list)
    external_refs: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def kind_requirements(self) -> InsightCard:
        if self.kind == "challenge" and self.challenge is None:
            raise ValueError("challenge cards need a challenge block")
        if self.kind != "challenge" and self.challenge is not None:
            raise ValueError("only challenge cards carry a challenge block")
        if self.kind in {"mechanism", "idea"} and not self.predictions:
            raise ValueError(f"{self.kind} cards need at least one prediction")
        if self.kind == "idea" and self.proposed_experiment is None:
            raise ValueError("idea cards need a proposed experiment")
        return self


class InsightBatch(ContractModel):
    """The file an analyst pass writes (``out/cards.json``)."""

    schema_version: Literal[1] = 1
    lens: AnalystLens
    cards: list[InsightCard] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=4000)


class RefereeVerdict(ContractModel):
    insight_id: str
    supported: Literal["yes", "partial", "no"]
    novel: bool
    nearest_existing: str | None = None
    falsifiable: bool
    actionable: bool
    importance: int = Field(ge=1, le=5)
    non_obviousness: int = Field(ge=1, le=5)
    verdict: Literal["accept", "revise", "reject"]
    notes: str = Field(min_length=4, max_length=1500)


class RefereeBatch(ContractModel):
    schema_version: Literal[1] = 1
    verdicts: list[RefereeVerdict] = Field(default_factory=list)


class TwinCard(ContractModel):
    """One deliberately flawed control and the valid card it was derived from."""

    source_id: str
    corruption: CorruptionType
    card: InsightCard


class TwinBatch(ContractModel):
    """The file a twinmaker pass writes (``out/twins.json``)."""

    schema_version: Literal[1] = 1
    twins: list[TwinCard] = Field(default_factory=list)


class CalibrationRecord(ContractModel):
    """A blind referee outcome for a corrupted twin or its original control."""

    canary_id: str
    insight_id: str
    pass_id: str
    arm: Literal["twin", "original"]
    corruption: CorruptionType | None = None
    verdict: Literal["accept", "revise", "reject"]
    created_at: datetime


class ScopeAudit(ContractModel):
    count: int = Field(default=0, ge=0)
    examples: list[str] = Field(default_factory=list, max_length=5)


class BoardEntry(ContractModel):
    insight_id: str
    rank: int = Field(ge=1)
    rationale: str = Field(min_length=4, max_length=600)


class BoardMerge(ContractModel):
    keep: str
    duplicates: list[str] = Field(min_length=1)


class BoardSupersede(ContractModel):
    new: str
    old: str


class PredictionResolution(ContractModel):
    prediction_id: str
    status: PredictionStatus
    claim_ids: list[str] = Field(default_factory=list)
    note: str = Field(min_length=4, max_length=600)


class BoardUpdate(ContractModel):
    """The file a synthesizer pass writes (``out/board.json``)."""

    schema_version: Literal[1] = 1
    headline: str = Field(min_length=8, max_length=300)
    summary: str = Field(min_length=20, max_length=6000)
    ranked: list[BoardEntry] = Field(default_factory=list)
    merges: list[BoardMerge] = Field(default_factory=list)
    supersedes: list[BoardSupersede] = Field(default_factory=list)
    promote: list[str] = Field(default_factory=list)
    resolutions: list[PredictionResolution] = Field(default_factory=list)


class RederivedClaim(ContractModel):
    claim_id: str
    recipe: Recipe | None = None
    value: float | None = None
    not_recomputable_reason: str | None = None


class RederiveBatch(ContractModel):
    """The file a rederiver pass writes (``out/rederive.json``)."""

    schema_version: Literal[1] = 1
    claims: list[RederivedClaim] = Field(default_factory=list)


class RederivationRecord(ContractModel):
    """An independent re-derivation of one claim's number by another model family."""

    claim_id: str
    pass_id: str
    model_served: str | None = None
    rederived: RederivedClaim
    recompute: RecomputeResult | None = None
    agrees: bool | None = None
    created_at: datetime


class InsightRecord(ContractModel):
    insight_id: str
    lens: AnalystLens
    pass_id: str
    model_served: str | None = None
    created_at: datetime
    card: InsightCard
    check: CheckResult
    status: InsightStatus
    referee: RefereeVerdict | None = None
    referee_model: str | None = None
    rank: int | None = None
    score: float | None = None
    duplicate_of: str | None = None
    superseded_by: str | None = None
    #: Hidden controls are ledger evidence for referee calibration, never findings.
    hidden: bool = False
    canary_of: str | None = None
    corruption: CorruptionType | None = None


class PredictionRecord(ContractModel):
    prediction_id: str
    insight_id: str
    spec: PredictionSpec
    status: PredictionStatus = "open"
    created_at: datetime
    resolved_at: datetime | None = None
    resolution: PredictionResolution | None = None


class PassRecord(ContractModel):
    pass_id: str
    lens: Lens
    route: str
    route_reason: str = ""
    trigger: str
    status: PassStatus
    pass_dir: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    attempt: int = Field(default=0, ge=0)
    served: dict[str, int] = Field(default_factory=dict)
    off_route: bool = False
    tokens: dict[str, int] = Field(default_factory=dict)
    inputs_digest: str = ""
    outputs: dict[str, int] = Field(default_factory=dict)
    scope_violations: ScopeAudit = Field(default_factory=ScopeAudit)
    error: str | None = None


class HumanReview(ContractModel):
    review_id: str
    insight_id: str
    status: ReviewStatus
    by: str
    note: str | None = None
    at: datetime


def safe_identifier(value: str) -> str:
    if not _ID_SAFE.match(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def claim_identity(source_id: str, section_id: str, quote: str, statement: str) -> str:
    return "C-" + short_digest(source_id, section_id, normalize_text(quote), normalize_text(statement))


def claim_fingerprint(claim: ExtractedClaim) -> str:
    """Same measurement reported in two places shares a fingerprint."""
    parts = [
        claim.experiment,
        (claim.metric or "").lower(),
        (claim.subject or "").lower(),
        (claim.dataset or "").lower(),
        normalize_text(claim.condition or "").lower(),
        "" if claim.k is None else str(claim.k),
        "" if claim.n is None else str(claim.n),
        "" if claim.value is None else f"{claim.value:.4g}",
    ]
    return "FP-" + short_digest(*parts)


def insight_identity(pass_id: str, card: InsightCard) -> str:
    return "I-" + short_digest(pass_id, card.local_id, normalize_text(card.statement), length=10)


def prediction_identity(insight_id: str, index: int) -> str:
    return f"{insight_id}.p{index + 1}"


def signal_identity(kind: str, claim_ids: list[str], key: str = "") -> str:
    return "S-" + short_digest(kind, *sorted(claim_ids), key)


def source_identity(path: str) -> str:
    return "F-" + short_digest(path)


def section_identity(source_id: str, heading_path: str, ordinal: int) -> str:
    return "X-" + short_digest(source_id, heading_path, str(ordinal))
