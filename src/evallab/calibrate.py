from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


from evallab.schemas import (
    CalibrationRecord,
)

AGREEMENT_FLOOR = 0.9
FAMILIES = ("checkout-pool-exhaustion", "retry-storm-backlog")
_VARIANT_COMMENT = re.compile(
    r"\A\s*<!--\s*calibration-variant:\s*[a-z0-9-]+\s*-->\s*", re.IGNORECASE
)

# "Supplied evidence" below means the family's evidence directory (the files the
# postmortem author had under /app/evidence, shown to the judge verbatim) plus
# the reference facts in the rubric. The sealed keys were decided against those
# files; a judge that only sees the reference facts cannot decide them.
_COMMON = {
    "actions_are_actionable": (
        "Does the corrective-action section name concrete changes with owners, targets, "
        "or objectively checkable completion conditions?"
    ),
    "actions_trace_to_findings": (
        "Does each substantive corrective action address a cause or contributing factor "
        "established earlier in the postmortem?"
    ),
    "proposes_unsupported_work": (
        "Does the document propose material work based on a cause or risk that the supplied "
        "evidence files and reference facts do not support?"
    ),
    "asserts_unsupported_cause": (
        "Does the document state a causal mechanism contradicted by, or absent from, the "
        "supplied evidence files and reference facts?"
    ),
    "misstates_a_fact": (
        "Does the document materially misstate a time, count, version, configuration value, "
        "or other fact established by the supplied evidence files or reference facts?"
    ),
    "invents_evidence": (
        "Does the document cite a dashboard, ticket, log line, metric, meeting, or observation "
        "that appears in neither the supplied evidence files nor the reference facts?"
    ),
}

RUBRICS: dict[str, dict[str, Any]] = {
    "checkout-pool-exhaustion": {
        "reference_facts": [
            "checkout-api v4.19.0 raised HTTP workers from 8 to 32 at 14:02 UTC.",
            "The per-instance database pool remained max_connections=10 with a 5000ms timeout.",
            "Pool active connections pinned at 10; waiters and 500 responses rose after deploy.",
            "The payments-vendor advisory began later, latency stayed about 130ms, and every "
            "observed vendor call returned 200.",
            "Rolling back to 8 workers at 15:31 restored pool wait and errors within minutes.",
            "No pool-saturation or acquire-wait alert existed; the 5xx alert was customer-late.",
            "A search-api TLS notice and a later ledger-db CPU warning were unrelated signals.",
        ],
        "criteria": {
            "causal_reasoning": {
                "identifies_the_mechanism": (
                    "Does the document identify contention between 32 workers and a 10-connection "
                    "pool, leading to 5000ms acquire timeouts and checkout 500s?"
                ),
                "grounded_in_evidence": (
                    "Does it ground that mechanism in multiple specific items from the supplied "
                    "evidence files or reference facts rather than assertion or fluent generalities?"
                ),
                "rules_out_the_decoy": (
                    "Does it explicitly reject the payments-vendor advisory using timing, flat "
                    "vendor latency, or successful vendor calls?"
                ),
                "separates_contributing_factors": (
                    "Does it distinguish alerting, canary, review, or response gaps from the "
                    "worker/pool causal mechanism?"
                ),
                "uncertainty_is_genuine": (
                    "Are open questions limited to matters not settled by the supplied evidence "
                    "files or reference facts?"
                ),
            },
            "action_quality": {
                "fixes_the_capacity_coupling": (
                    "Does an action couple pool sizing to worker count or fail startup on an "
                    "unsafe mismatch?"
                ),
                "closes_the_detection_gap": (
                    "Does an action add detection for pool saturation or connection-acquire wait?"
                ),
                "actions_are_actionable": _COMMON["actions_are_actionable"],
                "actions_trace_to_findings": _COMMON["actions_trace_to_findings"],
                "proposes_unsupported_work": _COMMON["proposes_unsupported_work"],
            },
            "evidence_fidelity": {
                "blames_payments_vendor": (
                    "Does the document treat the payments vendor as a cause rather than a rejected "
                    "hypothesis?"
                ),
                "asserts_unsupported_cause": _COMMON["asserts_unsupported_cause"],
                "misstates_a_fact": _COMMON["misstates_a_fact"],
                "invents_evidence": _COMMON["invents_evidence"],
            },
        },
        "negated": [
            "action_quality.proposes_unsupported_work",
            "evidence_fidelity.blames_payments_vendor",
            "evidence_fidelity.asserts_unsupported_cause",
            "evidence_fidelity.misstates_a_fact",
            "evidence_fidelity.invents_evidence",
        ],
    },
    "retry-storm-backlog": {
        "reference_facts": [
            "The SMS gateway returned 503 from 03:11 to 03:17 UTC, then recovered.",
            "Retries were unlimited, fixed at one second, without backoff or jitter.",
            "Retries and first attempts shared all 64 worker slots with no retry budget or lane.",
            "The retry population filled the workers and sustained the backlog after recovery.",
            "Delivery resumed only after a 07:01 drain plus max_attempts=6 and backoff "
            "with jitter.",
            "The v2.8.1 deploy changed log formatting and preceded healthy service by 24 minutes.",
            "A later database CPU warning was below its page threshold and was downstream.",
            "Queue-depth alerting was email-only and far above the healthy baseline.",
        ],
        "criteria": {
            "causal_reasoning": {
                "identifies_the_mechanism": (
                    "Does the document identify unbounded one-second retries sharing all worker "
                    "slots as the self-sustaining amplification mechanism?"
                ),
                "separates_trigger_from_cause": (
                    "Does it distinguish the six-minute gateway 503 trigger from the retry-policy "
                    "cause of the multi-hour outage?"
                ),
                "grounded_in_evidence": (
                    "Does it ground the mechanism in multiple specific items from the supplied "
                    "evidence files or reference facts rather than assertion or generic retry advice?"
                ),
                "rules_out_the_decoys": (
                    "Does it reject both the log-format deploy and database CPU as causes using "
                    "timing or behavior?"
                ),
                "separates_contributing_factors": (
                    "Does it distinguish alert routing, threshold, and missing dead-letter/retry "
                    "isolation from the primary amplification mechanism?"
                ),
                "uncertainty_is_genuine": (
                    "Are open questions limited to matters not settled by the supplied evidence "
                    "files or reference facts?"
                ),
            },
            "action_quality": {
                "bounds_the_amplification": (
                    "Does an action cap attempts, add backoff/jitter, isolate retry capacity, add "
                    "a dead-letter queue, or otherwise bound retry amplification?"
                ),
                "closes_the_detection_gap": (
                    "Does an action page on delivery lag, retry share, slot saturation, or an "
                    "appropriately bounded queue depth?"
                ),
                "actions_are_actionable": _COMMON["actions_are_actionable"],
                "actions_trace_to_findings": _COMMON["actions_trace_to_findings"],
                "proposes_unsupported_work": _COMMON["proposes_unsupported_work"],
            },
            "evidence_fidelity": {
                "blames_the_deploy": (
                    "Does the document treat v2.8.1 as a cause despite its log-only change and the "
                    "healthy interval after deployment?"
                ),
                "treats_db_cpu_as_cause": (
                    "Does the document treat the later database CPU warning as a cause rather than "
                    "a downstream effect?"
                ),
                "asserts_unsupported_cause": _COMMON["asserts_unsupported_cause"],
                "misstates_a_fact": _COMMON["misstates_a_fact"],
                "invents_evidence": _COMMON["invents_evidence"],
            },
        },
        "negated": [
            "action_quality.proposes_unsupported_work",
            "evidence_fidelity.blames_the_deploy",
            "evidence_fidelity.treats_db_cpu_as_cause",
            "evidence_fidelity.asserts_unsupported_cause",
            "evidence_fidelity.misstates_a_fact",
            "evidence_fidelity.invents_evidence",
        ],
    },
}


@dataclass(frozen=True)
class CorpusDocument:
    family: str
    document_id: str
    path: Path
    text: str


@dataclass(frozen=True)
class EvidenceFile:
    name: str
    sha256: str
    text: str


@dataclass(frozen=True)
class EvidencePack:
    """The family's evidence directory, vendored byte-for-byte from its task source.

    ``digest`` binds every judge input, bundle and record to these exact bytes and
    to the upstream revision they were copied from.
    """

    family: str
    source_repository: str
    source_revision: str
    source_path: str
    mount: str
    files: tuple[EvidenceFile, ...]
    digest: str


class EvidencePackError(ValueError):
    """The vendored evidence pack is missing, incomplete, or does not match its manifest."""


@dataclass(frozen=True)
class DspyExample:
    document_id: str
    family: str
    rubric_json: str
    evidence: str
    document: str
    expected_json: str


@dataclass(frozen=True)
class DspySplit:
    train: tuple[DspyExample, ...]
    optimizer_validation: tuple[DspyExample, ...]
    heldout: tuple[DspyExample, ...]






class DspyOptimizer(Protocol):
    def compile(self, program: Any, *, trainset: Sequence[Any], valset: Sequence[Any]) -> Any: ...


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _sha256(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(len(chunk).to_bytes(8, "big"))
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _validate_family(family: str) -> None:
    if family not in FAMILIES:
        raise ValueError(f"unknown calibration family {family!r}; choose one of {FAMILIES}")


def calibration_root(repo_root: Path) -> Path:
    return repo_root / "research/calibration"


def load_corpus(repo_root: Path, family: str) -> list[CorpusDocument]:
    _validate_family(family)
    base = calibration_root(repo_root) / family
    manifest_path = base / "corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("family") != family or not isinstance(manifest.get("documents"), list):
        raise ValueError(f"invalid calibration manifest: {manifest_path}")
    documents = []
    for entry in manifest["documents"]:
        path = base / entry["path"]
        documents.append(
            CorpusDocument(
                family=family,
                document_id=entry["id"],
                path=path,
                text=_VARIANT_COMMENT.sub("", path.read_text(encoding="utf-8"), count=1),
            )
        )
    return documents






def evidence_root(repo_root: Path) -> Path:
    return repo_root / "research/calibration-evidence"


def load_evidence_pack(repo_root: Path, family: str) -> EvidencePack:
    """Load and verify the family's vendored evidence directory.

    Every file named in ``manifest.json`` must exist with the recorded sha256, and
    no extra files may sit beside them; anything else raises ``EvidencePackError``
    naming the offending file, so a judge can never be measured against a partial
    or edited evidence directory.
    """
    _validate_family(family)
    base = evidence_root(repo_root) / family
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        raise EvidencePackError(f"{family}: evidence manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest.get("source") or {}
    if (
        manifest.get("family") != family
        or not isinstance(manifest.get("files"), list)
        or not all(
            isinstance(source.get(k), str) for k in ("repository", "revision", "path", "mount")
        )
    ):
        raise EvidencePackError(f"{family}: invalid evidence manifest: {manifest_path}")
    files: list[EvidenceFile] = []
    chunks = [source["repository"].encode(), source["revision"].encode(), source["path"].encode()]
    for entry in manifest["files"]:
        name = entry.get("name")
        if not isinstance(name, str) or "/" in name or name in {"", ".", "..", "manifest.json"}:
            raise EvidencePackError(f"{family}: invalid evidence file name {name!r}")
        path = base / name
        if not path.is_file():
            raise EvidencePackError(f"{family}: evidence file missing: {name}")
        data = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != entry.get("sha256"):
            raise EvidencePackError(
                f"{family}: evidence file {name} does not match its manifest digest"
            )
        files.append(EvidenceFile(name=name, sha256=digest, text=data.decode("utf-8")))
        chunks.extend([name.encode(), data])
    expected = {file.name for file in files} | {"manifest.json"}
    extra = sorted(p.name for p in base.iterdir() if p.name not in expected)
    if extra:
        raise EvidencePackError(f"{family}: unlisted files in evidence directory: {extra}")
    return EvidencePack(
        family=family,
        source_repository=source["repository"],
        source_revision=source["revision"],
        source_path=source["path"],
        mount=source["mount"],
        files=tuple(files),
        digest=_sha256(*chunks),
    )




def render_evidence_pack(pack: EvidencePack) -> str:
    """The judge-facing text of the evidence directory: every file, verbatim, named."""
    parts = [
        f"Evidence directory {pack.mount} ({len(pack.files)} files; source "
        f"{pack.source_repository}@{pack.source_revision[:12]} {pack.source_path}). "
        "These are the only files the postmortem author had. A cited artifact that is "
        "not in this directory and not in the reference facts does not exist."
    ]
    for file in pack.files:
        body = file.text if file.text.endswith("\n") else file.text + "\n"
        parts.append(f"### {pack.mount}/{file.name} ({file.sha256[:19]})\n```\n{body}```")
    return "\n\n".join(parts) + "\n"


def rubric_payload(family: str) -> dict[str, Any]:
    _validate_family(family)
    return {
        "schema_version": 1,
        "family": family,
        "verdict_convention": (
            "Return the raw pre-inversion yes/no answer. A negated criterion asks whether the "
            "named flaw is present; do not invert its answer."
        ),
        **RUBRICS[family],
    }






































def load_dspy_examples(repo_root: Path, family: str) -> list[DspyExample]:
    """One example per sealed document: rubric, the family's evidence pack, the
    document, and the sealed verdicts (never an input)."""
    documents = load_corpus(repo_root, family)
    key_root = calibration_root(repo_root) / family / "answer-keys"
    rubric_json = _canonical_json(rubric_payload(family))
    evidence = render_evidence_pack(load_evidence_pack(repo_root, family))
    examples = []
    for document in documents:
        key = json.loads((key_root / f"{document.document_id}.json").read_text(encoding="utf-8"))
        expected = {
            dimension: {name: cell["verdict"] for name, cell in block.items()}
            for dimension, block in key["criteria"].items()
        }
        examples.append(
            DspyExample(
                document_id=document.document_id,
                family=family,
                rubric_json=rubric_json,
                evidence=evidence,
                document=document.text,
                expected_json=_canonical_json(expected),
            )
        )
    return examples


def split_dspy_examples(examples: Sequence[DspyExample]) -> DspySplit:
    if len(examples) < 12:
        raise ValueError("DSPy calibration requires at least 12 examples")
    heldout_positions = {0, 5, 10, 14, 17, 20}
    heldout = tuple(item for index, item in enumerate(examples) if index in heldout_positions)
    optimizer_visible = [
        item for index, item in enumerate(examples) if index not in heldout_positions
    ]
    optimizer_validation = tuple(
        item for index, item in enumerate(optimizer_visible) if index % 4 == 0
    )
    train = tuple(item for index, item in enumerate(optimizer_visible) if index % 4 != 0)
    visible_ids = {item.document_id for item in (*train, *optimizer_validation)}
    heldout_ids = {item.document_id for item in heldout}
    if visible_ids & heldout_ids:
        raise AssertionError("DSPy optimizer-visible and held-out ids overlap")
    return DspySplit(train=train, optimizer_validation=optimizer_validation, heldout=heldout)


DSPY_JUDGE_INSTRUCTIONS = (
    "Judge an incident postmortem against every criterion in the rubric.\n\n"
    "For each dimension and criterion in rubric_json.criteria, answer the criterion's "
    "question about the document with a raw yes/no. Negated criteria ask whether a flaw "
    "is PRESENT; answer yes when the flaw is present and do not invert. Decide evidence "
    "questions against the supplied evidence files and rubric_json.reference_facts: a "
    "cited artifact, value, or event that appears in neither does not exist, and a value "
    "that disagrees with those files is misstated. Do not assume facts that are not "
    "supplied. Include every criterion in rubric_json.criteria exactly once."
)




def check_dspy_program_state(program: Any, path: Path) -> None:
    """Refuse saved program state whose fields do not match the live signature.

    ``dspy.Signature.load_state`` pairs saved field prefixes with live fields
    positionally, so state saved before the ``evidence`` input existed would load
    without error and silently shift every description onto the wrong field.
    """
    state = json.loads(path.read_text(encoding="utf-8"))
    for name, predictor in program.named_predictors():
        saved = state.get(name)
        if not isinstance(saved, dict) or "signature" not in saved:
            raise ValueError(f"{path}: no saved state for predictor {name!r}")
        live = [field.json_schema_extra["prefix"] for field in predictor.signature.fields.values()]
        stored = [field.get("prefix") for field in saved["signature"].get("fields", [])]
        if stored != live:
            raise ValueError(
                f"{path}: saved fields {stored} do not match the judge signature {live}; "
                "this program was compiled against a different input contract"
            )












def dspy_split_summary(repo_root: Path, family: str) -> dict[str, Any]:
    split = split_dspy_examples(load_dspy_examples(repo_root, family))
    return {
        "family": family,
        "train_ids": [item.document_id for item in split.train],
        "optimizer_validation_ids": [item.document_id for item in split.optimizer_validation],
        "heldout_ids": [item.document_id for item in split.heldout],
        "optimizer_sees_heldout": False,
    }


# =========================================================================== #
# SG-4: LLM-as-a-Verifier Selection Lift & Verifier Agreement vs Execution GT
# =========================================================================== #


class MissingVerifierDependencyError(ImportError):
    """Raised when an operation requires the optional 'verifier' extra."""


class PaidModelAuthorizationError(RuntimeError):
    """Raised when attempting to invoke a paid model verifier without authorization."""


class VerifierProtocol(Protocol):
    """Contract for selection and scoring verifiers."""

    def select(self, task: str, candidates: Sequence[str]) -> int: ...

    def score(self, task: str, candidate: str) -> float: ...


class StubVerifier:
    """Deterministic local stub verifier incurring zero model token spend."""

    def __init__(
        self,
        select_fn: Callable[[str, Sequence[str]], int] | None = None,
        score_fn: Callable[[str, str], float] | None = None,
    ) -> None:
        self._select_fn = select_fn
        self._score_fn = score_fn

    def select(self, task: str, candidates: Sequence[str]) -> int:
        if self._select_fn is not None:
            return self._select_fn(task, candidates)
        return 0

    def score(self, task: str, candidate: str) -> float:
        if self._score_fn is not None:
            return self._score_fn(task, candidate)
        return 1.0


class AlwaysPassStubVerifier(StubVerifier):
    """Stub verifier that unconditionally predicts pass (1.0)."""

    def select(self, task: str, candidates: Sequence[str]) -> int:
        return 0

    def score(self, task: str, candidate: str) -> float:
        return 1.0




def load_llm_verifier() -> Any:
    """Dynamically import llm_verifier or raise a graceful degradation error."""
    try:
        return importlib.import_module("llm_verifier")
    except ImportError as exc:
        raise MissingVerifierDependencyError(
            "llm-verifier is not installed. Install with: uv add --extra verifier llm-verifier "
            "or pip install 'eval-lab[verifier]'"
        ) from exc


class LlmVerifier:
    """LLM-backed verifier wrapper guarding paid token execution behind explicit opt-in."""

    def __init__(
        self,
        model: str = "gpt-4o",
        *,
        allow_paid_tokens: bool = False,
        rubric: str | None = None,
    ) -> None:
        self.model = model
        self.allow_paid_tokens = allow_paid_tokens
        self.rubric = rubric
        self._backend: Any | None = None

    def _ensure_authorized(self) -> Any:
        if not self.allow_paid_tokens:
            raise PaidModelAuthorizationError(
                "Real LLM verifiers require explicit authorization; "
                "pass allow_paid_tokens=True to spend tokens."
            )
        if self._backend is None:
            mod = load_llm_verifier()
            self._backend = mod.Verifier(model=self.model, rubric=self.rubric)
        return self._backend

    def select(self, task: str, candidates: Sequence[str]) -> int:
        backend = self._ensure_authorized()
        return int(backend.select(task, candidates))

    def score(self, task: str, candidate: str) -> float:
        backend = self._ensure_authorized()
        return float(backend.score(task, candidate))




















def save_calibration_record(records_root: Path, record: CalibrationRecord) -> Path:
    """Save a schema-conforming CalibrationRecord to disk."""
    records_root = records_root.resolve()
    records_root.mkdir(parents=True, exist_ok=True)
    destination = records_root / f"{record.calib_id}.json"
    destination.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return destination


def load_calibration_records(records_root: Path) -> list[CalibrationRecord]:
    """Load all CalibrationRecord instances found under records_root."""
    records_root = records_root.resolve()
    if not records_root.is_dir():
        return []
    records: list[CalibrationRecord] = []
    for candidate in sorted(records_root.rglob("*.json")):
        try:
            records.append(
                CalibrationRecord.model_validate_json(candidate.read_text(encoding="utf-8"))
            )
        except Exception:
            continue
    return records




