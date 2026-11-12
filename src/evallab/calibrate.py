from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

AGREEMENT_FLOOR = 0.9
FAMILIES = ("checkout-pool-exhaustion", "retry-storm-backlog")

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


class DspyOptimizer(Protocol):
    def compile(self, program: Any, *, trainset: Sequence[Any], valset: Sequence[Any]) -> Any: ...


def _sha256(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(len(chunk).to_bytes(8, "big"))
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _validate_family(family: str) -> None:
    if family not in FAMILIES:
        raise ValueError(f"unknown calibration family {family!r}; choose one of {FAMILIES}")


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


# =========================================================================== #
# SG-4: LLM-as-a-Verifier Selection Lift & Verifier Agreement vs Execution GT
# =========================================================================== #


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


