"""One-folder analysis, combining receipt checks with optional analyst passes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from evallab.insights.attribution import load_attribution
from evallab.insights.check import unsupported_numbers
from evallab.insights.config import EngineConfig, SourceOverride, load_config
from evallab.insights.models import ClaimRecord, Lens, PassRecord, RecomputeStatus
from evallab.insights.packs import PassInputs
from evallab.insights.recompute import recompute_claim
from evallab.insights.sources import read_source_text
from evallab.insights.store import InsightStore, default_state_root
from evallab.insights.validity import (
    MAX_BYTES,
    ValidityFinding,
    ValidityReport,
    check_folder_validity,
)
from evallab.schemas import ContractModel


class EngineLike(Protocol):
    store: InsightStore

    def scan(self) -> object: ...

    def run_lens(
        self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
    ) -> PassRecord | None: ...


class EngineFactory(Protocol):
    def __call__(self, state_root: Path, *, config: EngineConfig) -> EngineLike: ...


class ReportClaim(ContractModel):
    claim_id: str
    text: str
    file: str
    line: int | None = None
    stated_number: float | None = None
    recomputed_number: float | None = None
    status: RecomputeStatus
    reason: str
    rederiver_sampled: bool = False
    rederiver_agreement: bool | None = None
    check_status: str = "pass"


class ExperimentAnalysis(ContractModel):
    folder: str
    generated_at: datetime
    source_digests: dict[str, str]
    validity: ValidityReport
    evidence: list[ReportClaim]
    alternative_explanations: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    agents_run: bool
    rederiver_sample: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def experiment_output_dir(folder: Path, state_root: Path | None = None) -> Path:
    root = folder.resolve(strict=True)
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "experiment"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return (state_root or default_state_root()) / "experiments" / f"{slug}-{digest}"


def _engine(state_root: Path, *, config: EngineConfig) -> EngineLike:
    from evallab.insights.engine import Engine

    return Engine(state_root, config=config)


def _reports(folder: Path) -> list[Path]:
    return sorted({
        path for pattern in ("*.md", "scoreboard.json", "summary*.json")
        for path in folder.glob(pattern)
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(folder)
    })


def _from_record(record: ClaimRecord, store: InsightStore, folder: Path) -> ReportClaim:
    source = store.get_source(record.source_id)
    section = store.get_section(record.claim.section_id)
    result = recompute_claim(record.claim, folder)
    status: RecomputeStatus = "not_recomputable" if result is None else result.status
    reason = "No receipt recipe was supplied." if result is None else result.detail
    if record.check.status == "fail":
        status = "not_recomputable"
        reason = "The extracted claim failed its code checks. " + " ".join(record.check.notes)
    stated = record.claim.value
    if stated is None and record.claim.k is not None:
        stated = (record.claim.k / record.claim.n if record.claim.n else float(record.claim.k))
    line = section.start_line if section else None
    if source is not None and section is not None:
        try:
            source_lines = read_source_text(source).splitlines()
        except (OSError, UnicodeError, ValueError):
            source_lines = []
        quote_start = record.claim.quote.splitlines()[0].strip()
        for index in range(section.start_line - 1, min(section.end_line, len(source_lines))):
            if quote_start and quote_start in source_lines[index]:
                line = index + 1
                break
    return ReportClaim(
        claim_id=record.claim_id, text=record.claim.statement,
        file=source.path if source else "unavailable", line=line,
        stated_number=stated if result is None else result.stated,
        recomputed_number=None if result is None else result.value,
        status=status, reason=reason or "Recipe calculation completed.",
        check_status=record.check.status,
    )


def _read_ledger(path: Path) -> list[ReportClaim]:
    if not path.is_file():
        return []
    claims = [ReportClaim.model_validate_json(line) for line in path.read_text("utf-8").splitlines()
              if line.strip()]
    return claims


def _numbers(value: object) -> Iterable[str | float | int]:
    if isinstance(value, bool):
        return
    if isinstance(value, (str, float, int)):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _numbers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _numbers(child)


def _report(
    claims: list[ReportClaim], validity: ValidityReport, audit_id: str, *, agents_run: bool,
) -> str:
    research = claims
    reproduced = [claim for claim in research if claim.status == "reproduced"]
    differs = [claim for claim in research if claim.status == "differs"]
    uncertain = [claim for claim in research if claim.status == "not_recomputable"]
    lines = ["# Experiment review", "", "## What was tested", ""]
    lines.append(f"Local receipts and available claim entries were reviewed [{audit_id}].")
    for claim in [claim for claim in research if claim.check_status != "fail"][:3]:
        # Quote only the first short sentence, and attach its own evidence pointer.
        text = re.split(r"(?<=[.!?])\s+", " ".join(claim.text.split()))[0][:180].rstrip(".!?")
        lines.append(f"- The report says: {text} [{claim.claim_id}].")
    lines.extend(["", "## What held", ""])
    if reproduced:
        for claim in reproduced[:3]:
            lines.append(
                f"- The receipt calculation reproduced the stated number [{claim.claim_id}]."
            )
    else:
        lines.append(f"No report claim has a reproduced receipt calculation yet [{audit_id}].")
    lines.extend(["", "## What did not", ""])
    if differs:
        for claim in differs[:3]:
            lines.append(
                f"- The receipt calculation differs from the stated number [{claim.claim_id}]."
            )
    else:
        lines.append(f"The checked claims do not currently show a receipt mismatch [{audit_id}].")
    for finding in [f for f in validity.findings if f.severity in {"warning", "error"}][:2]:
        lines.append(f"- {finding.message.rstrip('.')} [{finding.finding_id}].")
    lines.extend(["", "## What is uncertain", ""])
    for claim in uncertain[:2]:
        lines.append(
            f"- This claim lacks a successful code-and-receipt check [{claim.claim_id}]."
        )
    for claim in [claim for claim in research if claim.rederiver_agreement is False][:2]:
        lines.append(f"- An independent rederivation disagreed with this claim [{claim.claim_id}].")
    if not agents_run:
        lines.append(
            "No analyst agents ran, so existing claim entries were not newly extracted "
            f"[{audit_id}]."
        )
    lines.append(f"Receipt agreement alone does not establish independent labels or performance "
                 f"on new cases [{audit_id}].")
    lines.extend([
        "", "## Next step", "",
        f"Review validity findings and resolve missing or disagreeing receipt evidence before "
        f"using these results to choose a system [{audit_id}].", "",
    ])
    return "\n".join(lines)


def analyze_experiment(
    folder: Path,
    out_dir: Path | None = None,
    *,
    engine_factory: EngineFactory | None = None,
    now: datetime | None = None,
    run_agents: bool = True,
) -> list[Path]:
    """Write a supported claims ledger, validity audit, analysis and one-page review."""
    root = folder.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    destination = (out_dir or experiment_output_dir(root)).resolve()
    if destination == root:
        raise ValueError("analysis output must not overwrite the source folder")
    timestamp = now or datetime.now(UTC)
    state = destination / ".state"
    validity = check_folder_validity(root, exclude=(destination,))
    attribution = load_attribution(root)
    reports = _reports(root)
    source_paths = set(reports) | {root / receipt.path for receipt in validity.receipts}
    digests = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_paths)
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES
        and not path.is_relative_to(destination)
    }
    sampled: list[str] = []
    limitations: list[str] = []
    if run_agents:
        from evallab.insights.engine import rederive_sample

        config = load_config(state).model_copy(update={
            "sources": [], "overrides": [SourceOverride(
                path=str(path), family="experiment", kind="report" if path.suffix == ".md"
                else "receipt",
            ) for path in reports],
        })
        engine = (engine_factory or _engine)(state, config=config)
        try:
            engine.scan()
            extractor = engine.run_lens("extractor", wait=True, trigger="experiment")
            if extractor is None or extractor.status != "succeeded":
                limitations.append("The extractor did not complete a successful pass.")
            records = engine.store.list_claims(include_failed=True)
            sampled = rederive_sample(record.claim_id for record in records
                                      if record.claim.recipe is not None)
            if sampled:
                engine.run_lens("rederiver", wait=True, trigger="experiment", inputs=PassInputs(
                    lens="rederiver", claim_ids=tuple(sampled),
                ))
            agreements = {record.claim_id: record.agrees
                          for record in engine.store.list_rederivations()}
            claims = [_from_record(record, engine.store, root) for record in records]
            for claim in claims:
                claim.rederiver_sampled = claim.claim_id in sampled
                if claim.rederiver_sampled:
                    claim.rederiver_agreement = agreements.get(claim.claim_id)
            if any(
                claim.rederiver_sampled and claim.rederiver_agreement is None for claim in claims
            ):
                limitations.append(
                    "Some sampled claims have no completed independent rederivation."
                )
            if any(claim.rederiver_agreement is False for claim in claims):
                limitations.append("An independent rederivation disagrees with an extracted claim.")
        finally:
            engine.store.close()
    else:
        ledger = destination / "claims.jsonl"
        claims = _read_ledger(ledger if ledger.exists() else root / "claims.jsonl")
        limitations.append("No analyst agents ran; existing claim entries were not re-extracted.")
    counts = {status: sum(claim.status == status for claim in claims)
              for status in ("reproduced", "differs", "not_recomputable")}
    audit_id = f"V-{len(validity.findings) + 1:02d}"
    validity.findings.append(ValidityFinding(
        finding_id=audit_id, code="claim_summary", severity="info", count=len(claims),
        message=(
            f"Available claim statuses: {json.dumps(counts, sort_keys=True)}. "
            "Receipt agreement does not establish independent labels or performance on new cases. "
            f"Analyst agents ran: {run_agents}."
        ),
    ))
    confidence: Literal["low", "medium", "high"] = "low"
    if (
        counts["reproduced"] and not counts["differs"] and not counts["not_recomputable"]
        and not limitations and not any(f.severity != "info" for f in validity.findings)
    ):
        confidence = "medium"
    analysis = ExperimentAnalysis(
        folder=str(root), generated_at=timestamp, source_digests=digests, validity=validity,
        evidence=claims, confidence=confidence, agents_run=run_agents,
        rederiver_sample=sampled, limitations=limitations,
        alternative_explanations=[
            "Shared or in-sample labels can explain agreement without showing generalization.",
            "Missing receipts or changing model routes can change the apparent result.",
        ],
    )
    report = _report(claims, validity, audit_id, agents_run=run_agents)
    allowed = list(_numbers([claim.model_dump() for claim in claims]))
    allowed.extend(_numbers(validity.model_dump()))
    assert not unsupported_numbers(report, allowed), "report contains unsupported numbers"
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, content in (
        ("claims.jsonl", "".join(claim.model_dump_json() + "\n" for claim in claims)),
        ("validity.json", validity.model_dump_json(indent=2) + "\n"),
        ("analysis.json", analysis.model_dump_json(indent=2) + "\n"),
        ("REPORT.md", report),
    ):
        path = destination / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    if attribution is not None:
        path = destination / "attribution.json"
        path.write_text(attribution.model_dump_json(indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
