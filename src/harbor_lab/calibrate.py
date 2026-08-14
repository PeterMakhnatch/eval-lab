from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from harbor_lab import database
from harbor_lab.queue import Executor
from harbor_lab.runner import database_url_from_environment
from harbor_lab.schemas import (
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
name = "Harbor Experiment Lab"
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
            if list(blocks[dimension]) != names:
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
    name = f"judge-{family_token}-{backend}-{model_token[:24]}-{target_date:%Y%m%d}"
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
    )
    if not decision.admitted or decision.policy_rule != "researcher-followups":
        raise ValueError(f"approved calibration no longer passes policy: {decision.message}")
    readiness = codex_calibration_readiness(repo_root, executor=executor)
    if not readiness.healthy:
        failed = [
            name for name, ok in readiness.__dict__.items() if not ok
        ]
        raise RuntimeError("Codex calibration readiness failed: " + ",".join(failed))
    return readiness, executor.tick()


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
