from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from evallab import database
from evallab.cohort import NOT_COMPARABLE, CohortMember, bootstrap_mean_interval
from evallab.facts import TrialFact
from evallab.queue import Executor
from evallab.runner import database_url_from_environment
from evallab.schemas import (
    CalibrationRecord,
    CriterionAgreementRate,
    ExperimentSpec,
    JudgeCalibrationRecord,
    JudgeCriterionVerdict,
    JudgeDocumentPrediction,
    JudgePredictionBundle,
)

AGREEMENT_FLOOR = 0.9
FAMILIES = ("checkout-pool-exhaustion", "retry-storm-backlog")
_VARIANT_COMMENT = re.compile(
    r"\A\s*<!--\s*calibration-variant:\s*[a-z0-9-]+\s*-->\s*", re.IGNORECASE
)

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
        "reference facts do not support?"
    ),
    "asserts_unsupported_cause": (
        "Does the document state a causal mechanism contradicted by, or absent from, the "
        "supplied reference facts?"
    ),
    "misstates_a_fact": (
        "Does the document materially misstate a time, count, version, configuration value, "
        "or other supplied reference fact?"
    ),
    "invents_evidence": (
        "Does the document cite a dashboard, ticket, log, metric, meeting, or observation not "
        "present in the supplied reference facts?"
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
                    "Does it ground that mechanism in multiple specific supplied facts rather "
                    "than assertion or fluent generalities?"
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
                    "Are open questions limited to matters not settled by the supplied facts?"
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
                    "Does it ground the mechanism in multiple specific supplied facts rather than "
                    "assertion or generic retry advice?"
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
                    "Are open questions limited to matters not settled by the supplied facts?"
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
class DspyExample:
    document_id: str
    family: str
    rubric_json: str
    document: str
    expected_json: str


@dataclass(frozen=True)
class DspySplit:
    train: tuple[DspyExample, ...]
    optimizer_validation: tuple[DspyExample, ...]
    heldout: tuple[DspyExample, ...]


@dataclass(frozen=True)
class StagedCalibration:
    task_path: Path
    spec_path: Path
    spec: ExperimentSpec


@dataclass(frozen=True)
class CodexCalibrationReadiness:
    codex_auth_present: bool
    docker_reachable: bool
    postgres_reachable: bool
    disk_headroom: bool

    @property
    def healthy(self) -> bool:
        return all(
            (
                self.codex_auth_present,
                self.docker_reachable,
                self.postgres_reachable,
                self.disk_headroom,
            )
        )


class DspyOptimizer(Protocol):
    def compile(
        self, program: Any, *, trainset: Sequence[Any], valset: Sequence[Any]
    ) -> Any: ...


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


def rubric_digest(family: str) -> str:
    _validate_family(family)
    return _sha256(_canonical_json(RUBRICS[family]).encode())


def corpus_digest(repo_root: Path, family: str) -> str:
    documents = load_corpus(repo_root, family)
    base = calibration_root(repo_root) / family
    chunks = [b"corpus.json", (base / "corpus.json").read_bytes()]
    for document in documents:
        chunks.extend(
            [document.path.relative_to(base).as_posix().encode(), document.path.read_bytes()]
        )
    return _sha256(*chunks)


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


def _expected_names(family: str) -> dict[str, set[str]]:
    return {
        dimension: set(criteria)
        for dimension, criteria in RUBRICS[family]["criteria"].items()
    }


def validate_prediction_bundle(
    repo_root: Path, bundle: JudgePredictionBundle
) -> list[CorpusDocument]:
    documents = load_corpus(repo_root, bundle.family)
    if bundle.rubric_digest != rubric_digest(bundle.family):
        raise ValueError("prediction bundle rubric digest does not match the current rubric")
    if bundle.corpus_digest != corpus_digest(repo_root, bundle.family):
        raise ValueError("prediction bundle corpus digest does not match the sealed corpus")
    expected_ids = [document.document_id for document in documents]
    observed_ids = [prediction.document_id for prediction in bundle.predictions]
    if observed_ids != expected_ids:
        raise ValueError("prediction documents must match corpus order exactly")
    expected_names = _expected_names(bundle.family)
    for prediction in bundle.predictions:
        if set(prediction.criteria) != set(expected_names):
            raise ValueError(f"{prediction.document_id} has wrong judge dimensions")
        for dimension, names in expected_names.items():
            if set(prediction.criteria[dimension]) != names:
                raise ValueError(
                    f"{prediction.document_id} has wrong criteria for {dimension}"
                )
    return documents


def load_prediction_bundle(path: Path) -> JudgePredictionBundle:
    try:
        return JudgePredictionBundle.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid judge prediction bundle {path}: {exc}") from exc


def _record_id(bundle: JudgePredictionBundle, evaluated_on: date) -> str:
    model = re.sub(r"[^a-z0-9]+", "-", bundle.judge_model.lower()).strip("-")
    suffix = bundle.corpus_digest.removeprefix("sha256:")[:10]
    return f"{bundle.family}-{evaluated_on:%Y%m%d}-{model[:28]}-{suffix}"


def evaluate_predictions(
    repo_root: Path,
    bundle: JudgePredictionBundle,
    *,
    prediction_artifact: str,
    evaluated_on: date | None = None,
    status: str = "measured",
    pending_backends: Sequence[str] = (),
) -> JudgeCalibrationRecord:
    documents = validate_prediction_bundle(repo_root, bundle)
    counts: dict[str, list[int]] = {}
    key_root = calibration_root(repo_root) / bundle.family / "answer-keys"
    for document, prediction in zip(documents, bundle.predictions, strict=True):
        key = json.loads(
            (key_root / f"{document.document_id}.json").read_text(encoding="utf-8")
        )
        for dimension, names in _expected_names(bundle.family).items():
            for name in names:
                expected = key["criteria"][dimension][name]["verdict"]
                observed = prediction.criteria[dimension][name].verdict
                cell = counts.setdefault(f"{dimension}.{name}", [0, 0])
                cell[1] += 1
                cell[0] += int(observed == expected)
    rates = {
        name: CriterionAgreementRate(agreements=agreed, total=total, rate=agreed / total)
        for name, (agreed, total) in sorted(counts.items())
    }
    agreements = sum(rate.agreements for rate in rates.values())
    total = sum(rate.total for rate in rates.values())
    mean = agreements / total
    target_date = evaluated_on or date.today()
    normalized_status = "stub" if status == "stub" else "measured"
    return JudgeCalibrationRecord(
        record_id=_record_id(bundle, target_date),
        family=bundle.family,
        status=normalized_status,
        judge_backend=bundle.judge_backend,
        judge_model=bundle.judge_model,
        judge_engine_version=bundle.judge_engine_version,
        rubric_digest=bundle.rubric_digest,
        corpus_digest=bundle.corpus_digest,
        per_criterion_agreement=rates,
        mean_agreement=mean,
        agreement_floor=AGREEMENT_FLOOR,
        meets_floor=mean >= AGREEMENT_FLOOR,
        reportable=normalized_status == "measured",
        document_count=len(documents),
        evaluated_on=target_date,
        prediction_artifact=prediction_artifact,
        pending_backends=list(pending_backends),
    )


def write_calibration_record(
    repo_root: Path,
    record: JudgeCalibrationRecord,
    *,
    records_root: Path | None = None,
) -> Path:
    root = (records_root or calibration_root(repo_root) / "records").resolve()
    destination = root / record.family / f"{record.record_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite calibration record: {destination}")
    destination.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return destination


def write_catalog_record(
    record: JudgeCalibrationRecord,
    record_path: Path,
    *,
    database_url: str | None = None,
) -> None:
    url = database_url_from_environment(database_url)
    with psycopg.connect(url, connect_timeout=2) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS judge_calibrations (
                record_id text PRIMARY KEY,
                family text NOT NULL,
                status text NOT NULL,
                judge_backend text NOT NULL,
                judge_model text NOT NULL,
                judge_engine_version text,
                rubric_digest text NOT NULL,
                corpus_digest text NOT NULL,
                per_criterion_agreement jsonb NOT NULL,
                mean_agreement double precision NOT NULL,
                agreement_floor double precision NOT NULL,
                meets_floor boolean NOT NULL,
                reportable boolean NOT NULL,
                document_count integer NOT NULL,
                evaluated_on date NOT NULL,
                prediction_artifact text NOT NULL,
                record_path text NOT NULL,
                raw_record jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        payload = record.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO judge_calibrations (
                record_id, family, status, judge_backend, judge_model,
                judge_engine_version, rubric_digest, corpus_digest,
                per_criterion_agreement, mean_agreement, agreement_floor,
                meets_floor, reportable, document_count, evaluated_on,
                prediction_artifact, record_path, raw_record
            ) VALUES (
                %(record_id)s, %(family)s, %(status)s, %(judge_backend)s,
                %(judge_model)s, %(judge_engine_version)s, %(rubric_digest)s,
                %(corpus_digest)s, %(per_criterion_agreement)s,
                %(mean_agreement)s, %(agreement_floor)s, %(meets_floor)s,
                %(reportable)s, %(document_count)s, %(evaluated_on)s,
                %(prediction_artifact)s, %(record_path)s, %(raw_record)s
            )
            ON CONFLICT (record_id) DO UPDATE SET
                record_path = EXCLUDED.record_path,
                raw_record = EXCLUDED.raw_record,
                created_at = now()
            """,
            {
                **payload,
                "per_criterion_agreement": Jsonb(payload["per_criterion_agreement"]),
                "record_path": record_path.as_posix(),
                "raw_record": Jsonb(payload),
            },
        )


def make_stub_bundle(repo_root: Path, family: str) -> JudgePredictionBundle:
    documents = load_corpus(repo_root, family)
    criteria = RUBRICS[family]["criteria"]
    predictions = []
    for document in documents:
        predictions.append(
            JudgeDocumentPrediction(
                document_id=document.document_id,
                criteria={
                    dimension: {
                        name: JudgeCriterionVerdict(
                            verdict="no", rationale="deterministic plumbing stub"
                        )
                        for name in names
                    }
                    for dimension, names in criteria.items()
                },
            )
        )
    return JudgePredictionBundle(
        family=family,
        judge_backend="stub",
        judge_model="deterministic-all-no",
        judge_engine_version="1",
        rubric_digest=rubric_digest(family),
        corpus_digest=corpus_digest(repo_root, family),
        generated_at=datetime.now(UTC),
        predictions=predictions,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stage_agent_judge_task(
    repo_root: Path,
    family: str,
    *,
    backend: str,
    judge_model: str,
    task_relative: Path,
) -> Path:
    if backend not in {"harbor-codex-agent", "harbor-claude-agent"}:
        raise ValueError(f"unsupported queued judge backend: {backend}")
    task_root = (repo_root / task_relative).resolve()
    if repo_root.resolve() not in task_root.parents:
        raise ValueError("runtime calibration task must stay inside the repository")
    if task_root.exists():
        raise FileExistsError(f"refusing to overwrite staged calibration task: {task_root}")
    documents = load_corpus(repo_root, family)
    rubric = rubric_payload(family)
    rubric["rubric_digest"] = rubric_digest(family)
    rubric["corpus_digest"] = corpus_digest(repo_root, family)
    rubric["judge_backend"] = backend
    rubric["judge_model"] = judge_model
    environment = task_root / "environment"
    for document in documents:
        _write(environment / "documents" / f"{document.document_id}.md", document.text)
    _write(environment / "rubric.json", json.dumps(rubric, indent=2, sort_keys=True) + "\n")
    _write(
        environment / "Dockerfile",
        """FROM python:3.13-slim-bookworm

WORKDIR /app

COPY documents/ /app/input/documents/
COPY rubric.json /app/input/rubric.json

RUN mkdir -p /app/output
""",
    )
    ids = [document.document_id for document in documents]
    _write(
        task_root / "instruction.md",
        _agent_instruction(family, backend, judge_model, len(ids)),
    )
    _write(task_root / "task.toml", _task_toml(family))
    _write(
        task_root / "tests/Dockerfile",
        """FROM python:3.13-slim-bookworm

COPY . /tests

RUN mkdir -p /app/input /app/output /logs/verifier \\
    && chmod +x /tests/test.sh

WORKDIR /app
""",
    )
    _write(task_root / "tests/test.sh", "#!/bin/sh\nset -eu\nexec python /tests/verify.py\n")
    _write(task_root / "tests/verify.py", _verifier_source(family, ids))
    return task_root


def _agent_instruction(family: str, backend: str, judge_model: str, count: int) -> str:
    return f"""# Sealed-corpus judge calibration

Read `/app/input/rubric.json` and all {count} Markdown documents in
`/app/input/documents/`, in filename order. Apply every named criterion to every
document. The answer keys are intentionally absent and must not be guessed from
filenames, document ordering, style, or presumed variants. Judge only the document
and the reference facts in the rubric.

Write `/app/output/judgments.json` as UTF-8 JSON with this exact top-level shape:

```json
{{
  "schema_version": 1,
  "family": "{family}",
  "judge_backend": "{backend}",
  "judge_model": "{judge_model}",
  "judge_engine_version": null,
  "rubric_digest": "<copy from rubric.json>",
  "corpus_digest": "<copy from rubric.json>",
  "generated_at": "<RFC3339 UTC timestamp>",
  "predictions": [
    {{
      "document_id": "<filename without .md>",
      "criteria": {{
        "<dimension>": {{
          "<criterion>": {{"verdict": "yes|no", "rationale": "concise reason"}}
        }}
      }}
    }}
  ]
}}
```

Preserve document and criterion order from the rubric. Verdicts are Reward Kit's
raw, pre-inversion yes/no answers: for a negated criterion, answer whether the
named flaw is present; do not invert it. Include every document and every criterion
exactly once. Create no other file under `/app/output`.
"""


def _task_toml(family: str) -> str:
    return f'''schema_version = "1.4"
artifacts = ["/app/output/judgments.json"]

[task]
name = "local-lab/judge-calibration-{family}"
version = "1.0.0"
description = "Apply a sealed rubric to the {family} calibration corpus"
keywords = ["judge", "calibration", "sealed-holdout"]

[[task.authors]]
name = "Eval Lab"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = "hard"
category = "evaluation-research"
tags = ["judge-calibration", "rewardkit-semantics"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[agent]
timeout_sec = 1200.0

[environment]
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 2
memory_mb = 2048
storage_mb = 4096
mcp_servers = []
'''


def _verifier_source(family: str, document_ids: list[str]) -> str:
    criteria = {
        dimension: list(names) for dimension, names in RUBRICS[family]["criteria"].items()
    }
    return f'''import json
from pathlib import Path

OUTPUT = Path("/app/output/judgments.json")
LOGS = Path("/logs/verifier")
DOCUMENT_IDS = {document_ids!r}
CRITERIA = {criteria!r}


def valid() -> tuple[bool, str]:
    if not OUTPUT.is_file():
        return False, "judgments.json is missing"
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid JSON: {{type(exc).__name__}}"
    if payload.get("family") != {family!r}:
        return False, "wrong family"
    predictions = payload.get("predictions")
    if not isinstance(predictions, list):
        return False, "predictions must be a list"
    if [item.get("document_id") for item in predictions] != DOCUMENT_IDS:
        return False, "documents are missing, duplicated, or out of order"
    for prediction in predictions:
        blocks = prediction.get("criteria")
        if not isinstance(blocks, dict) or set(blocks) != set(CRITERIA):
            return False, "wrong dimensions"
        for dimension, names in CRITERIA.items():
            if set(blocks[dimension]) != set(names):
                return False, f"wrong criteria for {{dimension}}"
            for cell in blocks[dimension].values():
                if cell.get("verdict") not in {{"yes", "no"}} or not cell.get("rationale"):
                    return False, "invalid verdict cell"
    siblings = sorted(path.name for path in OUTPUT.parent.iterdir())
    if siblings != ["judgments.json"]:
        return False, "judgments.json must be the only output"
    return True, "prediction bundle is structurally complete"


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    passed, message = valid()
    (LOGS / "reward.json").write_text(json.dumps({{"reward": float(passed)}}) + "\\n")
    (LOGS / "checks.json").write_text(
        json.dumps({{"schema": {{"passed": passed, "message": message}}}}, indent=2) + "\\n"
    )
    print(json.dumps({{"passed": passed, "message": message}}))


if __name__ == "__main__":
    main()
'''


def queued_calibration_spec(
    family: str,
    *,
    backend: str,
    task_relative: Path,
    name: str,
    model: str | None,
    est_cost_usd: float,
) -> ExperimentSpec:
    agent = "codex" if backend == "harbor-codex-agent" else "claude-code"
    return ExperimentSpec(
        name=name,
        hypothesis=(
            f"The {backend} judge has measurable criterion agreement on the sealed {family} "
            "corpus without access to answer keys."
        ),
        # Measures a judge against a sealed corpus with answer keys withheld.
        purpose="calibration",
        task=f"registered/judge-calibration/{family}",
        task_path=task_relative.as_posix(),
        agent=agent,
        model=model,
        environment="docker",
        jobs_dir="runs",
        attempts=1,
        concurrency=1,
        submitted_by="judge",
        priority=40,
        est_cost_usd=est_cost_usd,
        policy_rule="researcher-followups",
        requires=["schema_valid", "dedup_pass", "calibrated_judges_only"],
        task_version=f"brief09-{rubric_digest(family)[7:19]}",
        verifier_digest=rubric_digest(family),
    )


def stage_queue_bundle(
    repo_root: Path,
    family: str,
    *,
    backend: str,
    judge_model: str | None = None,
    est_cost_usd: float = 2.75,
    run_date: date | None = None,
) -> StagedCalibration:
    if not 0 < est_cost_usd <= 3:
        raise ValueError("queued calibration estimate must be greater than zero and at most $3")
    target_date = run_date or date.today()
    if backend == "codex":
        if not judge_model:
            raise ValueError("Codex staging requires an explicit --judge-model")
        normalized_backend = "harbor-codex-agent"
        agent_model = judge_model
        recorded_model = judge_model
    elif backend == "anthropic":
        normalized_backend = "harbor-claude-agent"
        agent_model = judge_model or "anthropic/claude-sonnet-4-6"
        recorded_model = agent_model
    else:
        raise ValueError("backend must be codex or anthropic")
    family_token = "checkout" if family == "checkout-pool-exhaustion" else "retry"
    model_token = re.sub(r"[^a-z0-9]+", "-", recorded_model.lower()).strip("-")
    auth_token = "-authjson" if backend == "codex" else ""
    name = (
        f"judge-{family_token}-{backend}-{model_token[:24]}"
        f"{auth_token}-{target_date:%Y%m%d}"
    )
    task_relative = Path("queue/calibration-tasks") / name
    task_path = stage_agent_judge_task(
        repo_root,
        family,
        backend=normalized_backend,
        judge_model=recorded_model,
        task_relative=task_relative,
    )
    spec = queued_calibration_spec(
        family,
        backend=normalized_backend,
        task_relative=task_relative,
        name=name,
        model=agent_model,
        est_cost_usd=est_cost_usd,
    )
    spec_path = repo_root / "queue/calibration-specs" / f"{name}.json"
    if spec_path.exists():
        raise FileExistsError(f"refusing to overwrite staged queue spec: {spec_path}")
    _write(spec_path, spec.model_dump_json(indent=2) + "\n")
    return StagedCalibration(task_path=task_path, spec_path=spec_path, spec=spec)


def codex_calibration_readiness(
    repo_root: Path, *, executor: Executor
) -> CodexCalibrationReadiness:
    runtime_checks = {name: ok for name, ok, _detail in executor.local_runtime_checks()}
    try:
        database.ping(database_url_from_environment())
    except Exception:
        postgres_reachable = False
    else:
        postgres_reachable = True
    usage = shutil.disk_usage(repo_root)
    required_disk = max(5 * 1024**3, int(usage.total * 0.05))
    return CodexCalibrationReadiness(
        codex_auth_present=(Path.home() / ".codex/auth.json").is_file(),
        docker_reachable=runtime_checks.get("docker-daemon", False),
        postgres_reachable=postgres_reachable,
        disk_headroom=usage.free >= required_disk,
    )


def dispatch_approved_codex_calibration(
    repo_root: Path,
    family: str,
    spec_id: str,
) -> tuple[CodexCalibrationReadiness, int]:
    """Dispatch one approved Codex calibration without requiring a Claude credential."""
    executor = Executor.from_repo(repo_root)
    approved = executor.queue.list_specs("approved")
    if len(approved) != 1 or approved[0][1].spec_id != spec_id:
        ids = [spec.spec_id for _path, spec in approved]
        raise ValueError(f"expected only approved spec {spec_id}, found {ids}")
    spec = approved[0][1]
    if spec.agent != "codex" or spec.task != f"registered/judge-calibration/{family}":
        raise ValueError("the approved spec is not the requested Codex calibration")
    decision = executor.gate.decide(
        spec,
        spent_today_usd=executor._catalog_spend(),
        consecutive_harness_failures=executor._consecutive_harness_failures(),
        authorization=executor.queue.authorization_for(spec),
    )
    if not decision.admitted or decision.policy_rule != "human-approval":
        raise ValueError(
            "the approved calibration carries no live human authorisation to spend: "
            f"{decision.message}"
        )
    readiness = codex_calibration_readiness(repo_root, executor=executor)
    if not readiness.healthy:
        failed = [
            name for name, ok in readiness.__dict__.items() if not ok
        ]
        raise RuntimeError("Codex calibration readiness failed: " + ",".join(failed))
    previous_force_auth = os.environ.get("CODEX_FORCE_AUTH_JSON")
    os.environ["CODEX_FORCE_AUTH_JSON"] = "1"
    try:
        dispatched = executor.tick()
    finally:
        if previous_force_auth is None:
            os.environ.pop("CODEX_FORCE_AUTH_JSON", None)
        else:
            os.environ["CODEX_FORCE_AUTH_JSON"] = previous_force_auth
    return readiness, dispatched


def load_dspy_examples(repo_root: Path, family: str) -> list[DspyExample]:
    documents = load_corpus(repo_root, family)
    key_root = calibration_root(repo_root) / family / "answer-keys"
    rubric_json = _canonical_json(rubric_payload(family))
    examples = []
    for document in documents:
        key = json.loads(
            (key_root / f"{document.document_id}.json").read_text(encoding="utf-8")
        )
        expected = {
            dimension: {
                name: cell["verdict"] for name, cell in block.items()
            }
            for dimension, block in key["criteria"].items()
        }
        examples.append(
            DspyExample(
                document_id=document.document_id,
                family=family,
                rubric_json=rubric_json,
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


def build_dspy_program(dspy_module: Any | None = None) -> Any:
    dspy = dspy_module or importlib.import_module("dspy")
    signature = dspy.Signature(
        "family, rubric_json, document -> judgments_json",
        instructions=(
            "Apply every criterion in rubric_json to document. Return judgments_json as a JSON "
            "object mapping dimension to criterion to the raw pre-inversion yes/no verdict."
        ),
    )
    return dspy.ChainOfThought(signature)


def dspy_metric(example: Any, prediction: Any, trace: Any | None = None) -> float:
    del trace
    expected = json.loads(example.expected_json)
    try:
        observed = json.loads(prediction.judgments_json)
    except (AttributeError, TypeError, json.JSONDecodeError):
        return 0.0
    cells = [
        observed.get(dimension, {}).get(name) == verdict
        for dimension, block in expected.items()
        for name, verdict in block.items()
    ]
    return sum(cells) / len(cells) if cells else 0.0


def as_dspy_example(dspy: Any, example: DspyExample) -> Any:
    return dspy.Example(
        document_id=example.document_id,
        family=example.family,
        rubric_json=example.rubric_json,
        document=example.document,
        expected_json=example.expected_json,
    ).with_inputs("family", "rubric_json", "document")


def compile_dspy_program(
    repo_root: Path,
    family: str,
    *,
    optimizer_factory: Callable[..., DspyOptimizer],
    dspy_module: Any | None = None,
) -> tuple[Any, tuple[Any, ...]]:
    dspy = dspy_module or importlib.import_module("dspy")
    split = split_dspy_examples(load_dspy_examples(repo_root, family))
    program = build_dspy_program(dspy)
    trainset = tuple(as_dspy_example(dspy, item) for item in split.train)
    valset = tuple(as_dspy_example(dspy, item) for item in split.optimizer_validation)
    heldout = tuple(as_dspy_example(dspy, item) for item in split.heldout)
    optimizer = optimizer_factory(metric=dspy_metric)
    compiled = optimizer.compile(program, trainset=trainset, valset=valset)
    optimizer_ids = {
        example.document_id for example in (*split.train, *split.optimizer_validation)
    }
    if optimizer_ids & {example.document_id for example in split.heldout}:
        raise AssertionError("held-out controls reached the DSPy optimizer")
    return compiled, heldout


def dspy_split_summary(repo_root: Path, family: str) -> dict[str, Any]:
    split = split_dspy_examples(load_dspy_examples(repo_root, family))
    return {
        "family": family,
        "train_ids": [item.document_id for item in split.train],
        "optimizer_validation_ids": [
            item.document_id for item in split.optimizer_validation
        ],
        "heldout_ids": [item.document_id for item in split.heldout],
        "optimizer_sees_heldout": False,
    }


def remove_staged_task(task_root: Path) -> None:
    """Remove only a generated queue calibration task after resolving its exact marker."""
    marker = task_root / "task.toml"
    if not marker.is_file() or "judge-calibration" not in marker.read_text(encoding="utf-8"):
        raise ValueError(f"not a generated calibration task: {task_root}")
    shutil.rmtree(task_root)


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


class AlwaysFailStubVerifier(StubVerifier):
    """Stub verifier that unconditionally predicts failure (0.0)."""

    def select(self, task: str, candidates: Sequence[str]) -> int:
        return 0

    def score(self, task: str, candidate: str) -> float:
        return 0.0


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


@dataclass(frozen=True)
class TaskAttemptUnit:
    """One task evidence unit containing candidate rollout attempts."""

    task_name: str
    trial_ids: list[str]
    rewards: list[float | None]
    exception_classes: list[str | None]
    task_digest: str | None = None
    candidate_texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClassBalance:
    total: int
    measured: int
    passed: int
    failed: int
    never_measured: int
    pass_prevalence: float | None


@dataclass(frozen=True)
class ConfusionMatrix:
    tp: int
    fp: int
    tn: int
    fn: int


@dataclass(frozen=True)
class AgreementMetrics:
    class_balance: ClassBalance
    confusion: ConfusionMatrix
    raw_agreement: float
    cohens_kappa: float
    balanced_accuracy: float
    pass_sensitivity: float
    fail_specificity: float
    mcc: float
    macro_f1: float


@dataclass(frozen=True)
class SelectionLiftReport:
    n_tasks: int
    k: int
    is_underpowered: bool
    pass_at_1: float | None
    selected_at_k: float | None
    oracle_ceiling: float | None
    selection_lift: float | None
    pass_at_1_interval: tuple[float, float] | None
    selected_at_k_interval: tuple[float, float] | None
    oracle_ceiling_interval: tuple[float, float] | None
    selection_lift_interval: tuple[float, float] | None
    pass_at_1_text: str
    selected_at_k_text: str
    oracle_ceiling_text: str
    selection_lift_text: str
    threats: list[str]
    task_details: list[dict[str, Any]]


@dataclass(frozen=True)
class VerifierAgreementReport:
    metrics: AgreementMetrics
    never_measured_trials: list[dict[str, Any]]
    measured_trial_count: int
    judge_model: str
    rubric_digest: str
    corpus_digest: str


def compute_agreement_metrics(
    ground_truths: Sequence[int],
    predictions: Sequence[int],
    *,
    never_measured_count: int = 0,
) -> AgreementMetrics:
    """Compute chance-corrected and imbalance-robust agreement metrics."""
    if len(ground_truths) != len(predictions):
        raise ValueError("ground_truths and predictions must have identical length")
    n = len(ground_truths)
    tp = sum(1 for y, p in zip(ground_truths, predictions, strict=True) if y == 1 and p == 1)
    fp = sum(1 for y, p in zip(ground_truths, predictions, strict=True) if y == 0 and p == 1)
    tn = sum(1 for y, p in zip(ground_truths, predictions, strict=True) if y == 0 and p == 0)
    fn = sum(1 for y, p in zip(ground_truths, predictions, strict=True) if y == 1 and p == 0)

    passed = tp + fn
    failed = tn + fp
    total = n + never_measured_count
    pass_prevalence = passed / n if n > 0 else None

    raw_agreement = (tp + tn) / n if n > 0 else 0.0
    pass_sensitivity = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    fail_specificity = tn / (tn + fp) if (tn + fp) > 0 else 0.0
    balanced_accuracy = (pass_sensitivity + fail_specificity) / 2.0

    # Cohen's Kappa
    p_o = raw_agreement
    p_yes_true = (tp + fn) / n if n > 0 else 0.0
    p_yes_pred = (tp + fp) / n if n > 0 else 0.0
    p_no_true = (tn + fp) / n if n > 0 else 0.0
    p_no_pred = (tn + fn) / n if n > 0 else 0.0
    p_e = (p_yes_true * p_yes_pred) + (p_no_true * p_no_pred)
    if abs(1.0 - p_e) < 1e-12:
        cohens_kappa = 1.0 if abs(p_o - 1.0) < 1e-12 else 0.0
    else:
        cohens_kappa = (p_o - p_e) / (1.0 - p_e)

    # Matthews Correlation Coefficient (MCC)
    mcc_num = (tp * tn) - (fp * fn)
    mcc_denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = mcc_num / mcc_denom if mcc_denom > 0 else 0.0

    # Macro F1
    f1_pass = (2.0 * tp) / (2.0 * tp + fp + fn) if (2.0 * tp + fp + fn) > 0 else 0.0
    f1_fail = (2.0 * tn) / (2.0 * tn + fp + fn) if (2.0 * tn + fp + fn) > 0 else 0.0
    macro_f1 = (f1_pass + f1_fail) / 2.0

    return AgreementMetrics(
        class_balance=ClassBalance(
            total=total,
            measured=n,
            passed=passed,
            failed=failed,
            never_measured=never_measured_count,
            pass_prevalence=pass_prevalence,
        ),
        confusion=ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn),
        raw_agreement=raw_agreement,
        cohens_kappa=cohens_kappa,
        balanced_accuracy=balanced_accuracy,
        pass_sensitivity=pass_sensitivity,
        fail_specificity=fail_specificity,
        mcc=mcc,
        macro_f1=macro_f1,
    )


def evaluate_selection_lift(
    tasks: Sequence[TaskAttemptUnit],
    verifier: VerifierProtocol,
    *,
    k: int = 3,
    threshold: float = 1.0,
    seed: int = 0,
) -> SelectionLiftReport:
    """Evaluate best-of-k verifier selection lift against random sampling and oracle ceiling."""
    del threshold
    task_details: list[dict[str, Any]] = []
    pass_at_1_values: list[float] = []
    selected_at_k_values: list[float] = []
    oracle_ceiling_values: list[float] = []
    selection_lift_values: list[float] = []
    threats: list[str] = []

    insufficient_count = 0
    for task in tasks:
        valid_indices = [
            i
            for i, (exc, r) in enumerate(zip(task.exception_classes, task.rewards, strict=False))
            if exc is None and r is not None
        ]
        if len(valid_indices) < k:
            insufficient_count += 1
            continue

        selected_indices = valid_indices[:k]
        sub_rewards: list[float] = [
            float(r) for i in selected_indices if (r := task.rewards[i]) is not None
        ]
        sub_candidates = [
            task.candidate_texts[i]
            if i < len(task.candidate_texts)
            else f"trial-{task.trial_ids[i]}"
            for i in selected_indices
        ]

        p1 = statistics.fmean(sub_rewards)
        oracle = max(sub_rewards)

        chosen_idx_in_k = verifier.select(task.task_name, sub_candidates)
        if 0 <= chosen_idx_in_k < len(sub_rewards):
            picked_reward = sub_rewards[chosen_idx_in_k]
        else:
            picked_reward = sub_rewards[0]

        lift = picked_reward - p1

        pass_at_1_values.append(p1)
        selected_at_k_values.append(picked_reward)
        oracle_ceiling_values.append(oracle)
        selection_lift_values.append(lift)

        task_details.append(
            {
                "task_name": task.task_name,
                "task_digest": task.task_digest,
                "pass_at_1": p1,
                "selected_at_k": picked_reward,
                "oracle_ceiling": oracle,
                "selection_lift": lift,
                "picked_index": chosen_idx_in_k,
                "k_rewards": sub_rewards,
            }
        )

    n_tasks = len(pass_at_1_values)
    is_underpowered = n_tasks < 2

    p1_interval = (
        bootstrap_mean_interval(pass_at_1_values, seed=seed) if pass_at_1_values else None
    )
    sel_interval = (
        bootstrap_mean_interval(selected_at_k_values, seed=seed + 1)
        if selected_at_k_values
        else None
    )
    orc_interval = (
        bootstrap_mean_interval(oracle_ceiling_values, seed=seed + 2)
        if oracle_ceiling_values
        else None
    )
    lift_interval = (
        bootstrap_mean_interval(selection_lift_values, seed=seed + 3)
        if selection_lift_values
        else None
    )

    if is_underpowered:
        threats.append(
            f"Underpowered cohort (n_tasks={n_tasks} < 2); results are not distinguishable."
        )
        pass_at_1_text = NOT_COMPARABLE
        selected_at_k_text = NOT_COMPARABLE
        oracle_ceiling_text = NOT_COMPARABLE
        selection_lift_text = NOT_COMPARABLE
    else:
        mean_p1 = statistics.fmean(pass_at_1_values)
        mean_sel = statistics.fmean(selected_at_k_values)
        mean_orc = statistics.fmean(oracle_ceiling_values)
        mean_lift = statistics.fmean(selection_lift_values)
        pass_at_1_text = f"{mean_p1:.3f}"
        selected_at_k_text = f"{mean_sel:.3f}"
        oracle_ceiling_text = f"{mean_orc:.3f}"
        selection_lift_text = f"{mean_lift:.3f}"

    if insufficient_count > 0:
        threats.append(
            f"{insufficient_count} task(s) had fewer than k={k} valid execution attempts."
        )

    return SelectionLiftReport(
        n_tasks=n_tasks,
        k=k,
        is_underpowered=is_underpowered,
        pass_at_1=statistics.fmean(pass_at_1_values) if pass_at_1_values else None,
        selected_at_k=statistics.fmean(selected_at_k_values) if selected_at_k_values else None,
        oracle_ceiling=statistics.fmean(oracle_ceiling_values) if oracle_ceiling_values else None,
        selection_lift=statistics.fmean(selection_lift_values) if selection_lift_values else None,
        pass_at_1_interval=p1_interval,
        selected_at_k_interval=sel_interval,
        oracle_ceiling_interval=orc_interval,
        selection_lift_interval=lift_interval,
        pass_at_1_text=pass_at_1_text,
        selected_at_k_text=selected_at_k_text,
        oracle_ceiling_text=oracle_ceiling_text,
        selection_lift_text=selection_lift_text,
        threats=threats,
        task_details=task_details,
    )


def evaluate_verifier_agreement(
    trials: Sequence[Any],
    verifier: VerifierProtocol,
    *,
    threshold: float = 1.0,
    judge_model: str = "stub-verifier/deterministic",
    rubric_digest: str | None = None,
    corpus_digest: str | None = None,
) -> VerifierAgreementReport:
    """Evaluate verifier agreement against execution reward, excluding never-measured trials."""
    ground_truths: list[int] = []
    predictions: list[int] = []
    never_measured: list[dict[str, Any]] = []

    for trial in trials:
        trial_id: str
        reward: float | None
        exception_class: str | None
        task_name: str
        candidate_text: str = ""

        if isinstance(trial, (tuple, list)):
            trial_id = str(trial[0])
            raw_reward = trial[1] if len(trial) > 1 else None
            reward = float(raw_reward) if raw_reward is not None else None
            raw_exc = trial[2] if len(trial) > 2 else None
            exception_class = str(raw_exc) if raw_exc is not None else None
            task_name = str(trial[3]) if len(trial) > 3 and trial[3] is not None else "unknown-task"
            candidate_text = (
                str(trial[4]) if len(trial) > 4 and trial[4] is not None else f"trial-{trial_id}"
            )
        elif isinstance(trial, dict):
            trial_id = str(trial.get("trial_id") or trial.get("id") or "")
            reward = trial.get("reward") if "reward" in trial else trial.get("primary_reward")
            exception_class = trial.get("exception_class") or trial.get("exception_type")
            task_name = str(trial.get("task_name") or "unknown-task")
            candidate_text = str(
                trial.get("candidate_text") or trial.get("content") or f"trial-{trial_id}"
            )
        elif isinstance(trial, CohortMember):
            trial_id = trial.trial_id
            reward = trial.reward
            exception_class = trial.exception_class
            task_name = trial.task_name or "unknown-task"
            candidate_text = f"trial-{trial.trial_id}"
        elif isinstance(trial, TrialFact):
            trial_id = trial.trial_id
            reward = trial.primary_reward
            exception_class = trial.exception_class
            task_name = trial.task_name or "unknown-task"
            candidate_text = f"trial-{trial.trial_id}"
        else:
            trial_id = getattr(trial, "trial_id", "unknown")
            reward = getattr(trial, "reward", getattr(trial, "primary_reward", None))
            exception_class = getattr(trial, "exception_class", None)
            task_name = getattr(trial, "task_name", "unknown-task")
            candidate_text = f"trial-{trial_id}"

        # Never-measured trials (non-null exception_class or missing reward) are NOT failures
        if exception_class is not None or reward is None:
            never_measured.append(
                {
                    "trial_id": trial_id,
                    "task_name": task_name,
                    "exception_class": exception_class or "MissingReward",
                    "reward": reward,
                }
            )
            continue

        gt = 1 if float(reward) >= threshold else 0
        pred_score = verifier.score(task_name, candidate_text)
        pred = 1 if pred_score >= 0.5 else 0

        ground_truths.append(gt)
        predictions.append(pred)

    metrics = compute_agreement_metrics(
        ground_truths,
        predictions,
        never_measured_count=len(never_measured),
    )

    r_digest = rubric_digest or _sha256(b"execution_ground_truth_binary_reward")
    c_digest = corpus_digest or _sha256(
        json.dumps(
            [t[0] if isinstance(t, tuple) else getattr(t, "trial_id", "") for t in trials]
        ).encode()
    )

    return VerifierAgreementReport(
        metrics=metrics,
        never_measured_trials=never_measured,
        measured_trial_count=len(ground_truths),
        judge_model=judge_model,
        rubric_digest=r_digest,
        corpus_digest=c_digest,
    )


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


def build_verifier_calibration_card(
    lift_report: SelectionLiftReport,
    agreement_report: VerifierAgreementReport,
    *,
    repo_root: Path | None = None,
    title: str = "verifier-calibration-summary",
    is_stubbed: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Build a purpose='calibration' eval card comparing selection lift and verifier agreement."""
    root = (repo_root or Path.cwd()).resolve()
    template_path = root / "research/cards/TEMPLATE.md"
    if not template_path.is_file():
        raise ValueError(f"eval-card template is missing: {template_path}")

    provenance_banner = (
        "INJECTED STUB VERIFIER (Deterministic local control; zero tokens spent)"
        if is_stubbed
        else f"LIVE LLM VERIFIER ({agreement_report.judge_model})"
    )

    p1_int = (
        f"[{lift_report.pass_at_1_interval[0]:.3f}, {lift_report.pass_at_1_interval[1]:.3f}]"
        if lift_report.pass_at_1_interval and not lift_report.is_underpowered
        else "unavailable"
    )
    sel_int = (
        f"[{lift_report.selected_at_k_interval[0]:.3f}, "
        f"{lift_report.selected_at_k_interval[1]:.3f}]"
        if lift_report.selected_at_k_interval and not lift_report.is_underpowered
        else "unavailable"
    )
    orc_int = (
        f"[{lift_report.oracle_ceiling_interval[0]:.3f}, "
        f"{lift_report.oracle_ceiling_interval[1]:.3f}]"
        if lift_report.oracle_ceiling_interval and not lift_report.is_underpowered
        else "unavailable"
    )
    lift_int = (
        f"[{lift_report.selection_lift_interval[0]:.3f}, "
        f"{lift_report.selection_lift_interval[1]:.3f}]"
        if lift_report.selection_lift_interval and not lift_report.is_underpowered
        else "unavailable"
    )

    p_count = agreement_report.metrics.class_balance.passed
    f_count = agreement_report.metrics.class_balance.failed
    u_count = agreement_report.metrics.class_balance.never_measured
    hypothesis_text = f"""Calibration of LLM verifier against execution ground truth.

### Verification Mode & Provenance
- Mode: **{provenance_banner}**
- Zero tokens spent: **{is_stubbed}**

### Selection Lift (Best-of-{lift_report.k})
- pass@1: **{lift_report.pass_at_1_text}** (Interval: {p1_int})
- Selected@k: **{lift_report.selected_at_k_text}** (Interval: {sel_int})
- Oracle ceiling: **{lift_report.oracle_ceiling_text}** (Interval: {orc_int})
- Selection lift: **{lift_report.selection_lift_text}** (Interval: {lift_int})

### Chance-Corrected Verifier Agreement
- Cohen's Kappa: **{agreement_report.metrics.cohens_kappa:.4f}**
- Balanced Accuracy: **{agreement_report.metrics.balanced_accuracy:.4f}**
- Raw Agreement: **{agreement_report.metrics.raw_agreement:.4f}**
- Matthews Correlation (MCC): **{agreement_report.metrics.mcc:.4f}**
- Macro-F1: **{agreement_report.metrics.macro_f1:.4f}**
- Class Balance: **{p_count} passed / {f_count} failed / {u_count} unmeasured**
"""

    threats = list(lift_report.threats)
    if is_stubbed:
        threats.append(
            "Results generated using an injected deterministic stub verifier (zero tokens spent)."
        )
    if agreement_report.metrics.class_balance.never_measured > 0:
        threats.append(
            f"{agreement_report.metrics.class_balance.never_measured} trial(s) had "
            "execution/harness exceptions and were excluded from agreement measurement."
        )
    prev = agreement_report.metrics.class_balance.pass_prevalence
    if prev and prev > 0.8:
        threats.append(
            f"Severe class imbalance in corpus ({prev:.1%} pass prevalence); "
            "rely on Cohen's Kappa and Balanced Accuracy over raw agreement."
        )
    threats = list(dict.fromkeys(threats))

    spec_digest = agreement_report.rubric_digest
    job_lock_digest = agreement_report.corpus_digest

    elicitation_payload = {
        "verifier": agreement_report.judge_model,
        "rubric_digest": agreement_report.rubric_digest,
        "corpus_digest": agreement_report.corpus_digest,
        "k": lift_report.k,
        "is_stubbed": is_stubbed,
    }

    card_data: dict[str, Any] = {
        "schema_version": 1,
        "title": title,
        "purpose": "calibration",
        "spec_path": "research/calibration/specs/verifier-calibration.json",
        "spec_digest": spec_digest,
        "job_path": "research/evidence/runs",
        "job_id": "verifier-calibration",
        "job_lock_digest": job_lock_digest,
        "task": "multi-task-suite",
        "hypothesis": hypothesis_text,
        "numbers": {
            "n_tasks": lift_report.n_tasks,
            "n_trials": agreement_report.metrics.class_balance.total,
            "k": lift_report.k,
            "pass_at_1": lift_report.pass_at_1,
            "selected_at_k": lift_report.selected_at_k,
            "oracle_ceiling": lift_report.oracle_ceiling,
            "selection_lift": lift_report.selection_lift,
            "cohens_kappa": agreement_report.metrics.cohens_kappa,
            "balanced_accuracy": agreement_report.metrics.balanced_accuracy,
            "raw_agreement": agreement_report.metrics.raw_agreement,
            "exceptions": agreement_report.metrics.class_balance.never_measured,
            "is_underpowered": lift_report.is_underpowered,
        },
        "elicitation": elicitation_payload,
        "contamination_note": (
            "Execution ground truth calibration from containerized test suites. "
            "Zero external data leakage."
        ),
        "threats": threats,
    }

    replacements = {
        "{{TITLE}}": title,
        "{{HYPOTHESIS}}": hypothesis_text,
        "{{TASK}}": "multi-task-suite",
        "{{SPEC_PATH}}": "research/calibration/specs/verifier-calibration.json",
        "{{SPEC_DIGEST}}": spec_digest,
        "{{JOB_PATH}}": "research/evidence/runs",
        "{{JOB_ID}}": "verifier-calibration",
        "{{JOB_LOCK_DIGEST}}": job_lock_digest,
        "{{N_TASKS}}": str(lift_report.n_tasks),
        "{{N_TRIALS}}": str(agreement_report.metrics.class_balance.total),
        "{{K}}": str(lift_report.k),
        "{{PASS_AT_K}}": lift_report.selected_at_k_text,
        "{{INTERVAL}}": sel_int,
        "{{EXCEPTIONS}}": str(agreement_report.metrics.class_balance.never_measured),
        "{{ELICITATION}}": json.dumps(elicitation_payload, indent=2, sort_keys=True),
        "{{CONTAMINATION}}": card_data["contamination_note"],
        "{{THREATS}}": "\n".join(f"- {t}" for t in threats),
    }

    rendered = template_path.read_text(encoding="utf-8")
    for marker, val in replacements.items():
        rendered = rendered.replace(marker, str(val))

    if "{{" in rendered:
        raise ValueError("eval-card template contains an unresolved marker")

    return rendered, card_data


def draft_verifier_calibration_card(
    lift_report: SelectionLiftReport,
    agreement_report: VerifierAgreementReport,
    *,
    repo_root: Path | None = None,
    title: str = "verifier-calibration-summary",
    output_path: Path | None = None,
    is_stubbed: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Build and atomically write an eval card for verifier calibration."""
    root = (repo_root or Path.cwd()).resolve()
    rendered, card_data = build_verifier_calibration_card(
        lift_report,
        agreement_report,
        repo_root=root,
        title=title,
        is_stubbed=is_stubbed,
    )
    destination = output_path or (root / "research/cards" / f"{title}.md")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_file = destination.with_suffix(destination.suffix + ".tmp")
    temp_file.write_text(rendered, encoding="utf-8")
    temp_file.replace(destination)
    return destination, card_data
