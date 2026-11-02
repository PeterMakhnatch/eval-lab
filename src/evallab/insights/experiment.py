"""One-folder analysis, combining receipt checks with optional analyst passes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from evallab.insights.attribution import AttributionReport, load_attribution
from evallab.insights.check import unsupported_numbers
from evallab.insights.config import EngineConfig, SourceOverride, load_config
from evallab.insights.models import (
    ClaimRecord,
    InsightRecord,
    Lens,
    PassRecord,
    RecomputeStatus,
)
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
    experiment: str | None = None
    metric: str | None = None
    subject: str | None = None
    dataset: str | None = None
    k: int | None = None
    n: int | None = None
    recipe_backed: bool = False


class ReportInsight(ContractModel):
    insight_id: str
    lens: str
    status: str
    title: str
    flaw: str | None = None
    severity: str | None = None
    target_claim_ids: list[str] = Field(default_factory=list)
    referee_verdict: str | None = None
    referee_model: str | None = None
    check_status: str


class _ReportMember(ContractModel):
    folder: str
    claims_extracted: int
    report_files_read: int


class ExperimentAnalysis(ContractModel):
    folder: str
    related_folders: list[str] = Field(default_factory=list)
    generated_at: datetime
    source_digests: dict[str, str]
    validity: ValidityReport
    evidence: list[ReportClaim]
    insights: list[ReportInsight] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    agents_run: bool
    rederiver_sample: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def experiment_output_dir(
    folder: Path, state_root: Path | None = None, related: Sequence[Path] = (),
) -> Path:
    root = folder.resolve(strict=True)
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "experiment"
    identity = str(root)
    if related:
        identity += "\x00" + "\x00".join(sorted(str(path.resolve(strict=True)) for path in related))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
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


def _bundle(
    primary: Path, related: Sequence[Path], destination: Path,
) -> tuple[Path, list[Path]]:
    """Rebuild the bounded, symlink-free analysis snapshot with stable member names."""
    bundle = destination / ".bundle"
    if bundle.is_symlink():
        raise ValueError("analysis bundle must not be a symlink")
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    members: list[Path] = []
    ignored = {".git", ".venv", "node_modules", "__pycache__"}
    for source in (primary, *related):
        name = source.name
        index = 2
        while (bundle / name).exists():
            name = f"{source.name}-{index}"
            index += 1
        member = bundle / name
        member.mkdir()
        members.append(member)
        for directory, dirs, files in os.walk(source, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(
                name for name in dirs
                if name not in ignored and not (parent / name).is_symlink()
                and not (parent / name).is_relative_to(destination)
            )
            target = member / parent.relative_to(source)
            target.mkdir(parents=True, exist_ok=True)
            for filename in sorted(files):
                path = parent / filename
                if (
                    not path.is_symlink() and path.is_file()
                    and not path.is_relative_to(destination) and path.stat().st_size <= MAX_BYTES
                ):
                    shutil.copy2(path, target / filename)
    return bundle, members


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
        experiment=record.claim.experiment, metric=record.claim.metric,
        subject=record.claim.subject, dataset=record.claim.dataset,
        k=record.claim.k, n=record.claim.n, recipe_backed=record.claim.recipe is not None,
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


def _cohort_findings(claims: list[ReportClaim], validity: ValidityReport) -> None:
    # Claims without a subject are grouped within their experiment, so reports whose
    # extractor left subject empty still expose pooling across datasets or sample sizes.
    groups: dict[tuple[str, str, str], list[ReportClaim]] = {}
    for claim in claims:
        if (
            claim.check_status != "fail" and claim.k is not None and claim.n is not None
            and claim.metric
        ):
            scope = claim.subject or f"experiment {claim.experiment or 'unknown'}"
            groups.setdefault((claim.metric, claim.subject or "", scope), []).append(claim)
    mixed: list[str] = []
    mixed_ids: set[str] = set()
    for (metric, _subject, scope), group in sorted(groups.items()):
        datasets = {claim.dataset for claim in group}
        unknown_sizes = {claim.n for claim in group if claim.dataset is None}
        if len(datasets) < 2 and len(unknown_sizes) < 2:
            continue
        ids = sorted(claim.claim_id for claim in group)
        mixed.append(f"{metric} for {scope} ({', '.join(ids)})")
        mixed_ids.update(ids)
    if mixed:
        validity.findings.append(ValidityFinding(
            finding_id=f"V-{len(validity.findings) + 1:02d}", code="cohort_mix",
            severity="warning", count=len(mixed_ids), pointers=sorted(mixed_ids),
            message="These claims describe different groups of runs that must not be pooled: "
                    + "; ".join(mixed) + ".",
        ))


def _report_insight(record: InsightRecord) -> ReportInsight:
    challenge = record.card.challenge
    return ReportInsight(
        insight_id=record.insight_id, lens=record.lens, status=record.status,
        title=record.card.title, flaw=challenge.flaw if challenge else None,
        severity=challenge.severity if challenge else None,
        target_claim_ids=(challenge.target_claim_ids if challenge else
                          [ref.claim_id for ref in record.card.evidence]),
        referee_verdict=record.referee.verdict if record.referee else None,
        referee_model=record.referee_model, check_status=record.check.status,
    )


# A pass that fails this quickly died at startup (e.g. SQLITE_BUSY in the worker's own
# state database), not during analysis: run it once more after a short pause.
_RETRY_WITHIN_SECONDS = 30.0
_RETRY_DELAY_SECONDS = 3.0


def _run_pass(
    engine: EngineLike, lens: Lens, inputs: PassInputs, limitations: list[str],
) -> PassRecord | None:
    for attempt in range(2):
        started = time.monotonic()
        error: Exception | None = None
        result: PassRecord | None = None
        try:
            result = engine.run_lens(lens, wait=True, trigger="experiment", inputs=inputs)
        except Exception as exc:
            error = exc
        failed = error is not None or (result is not None and result.status != "succeeded")
        if not failed or attempt == 1 or time.monotonic() - started >= _RETRY_WITHIN_SECONDS:
            break
        time.sleep(_RETRY_DELAY_SECONDS)
    if error is not None:
        limitations.append(f"The {lens} pass failed ({type(error).__name__}).")
        return None
    if result is None or result.status != "succeeded":
        limitations.append(f"The {lens} pass did not complete successfully.")
    return result


def _extract_all(engine: EngineLike, limitations: list[str]) -> None:
    failures = 0
    for _ in range(20):
        try:
            result = engine.run_lens("extractor", wait=True, trigger="experiment")
        except Exception as exc:
            limitations.append(f"An extractor pass failed ({type(exc).__name__}).")
            failures += 1
        else:
            if result is None:
                return
            if result.status == "succeeded":
                failures = 0
            else:
                failures += 1
                limitations.append("An extractor pass did not complete successfully.")
        if failures >= 2:
            limitations.append("Extraction stopped after consecutive unsuccessful passes.")
            return
    limitations.append("Extraction reached the cap of 20 passes; some sections may remain.")


def _short(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))[0]
    if len(first) > 180:
        prefix = first[:181]
        first = prefix.rsplit(" ", 1)[0] if " " in prefix else "Report claim"
    return first.rstrip(".!?")


def _sentence(text: str) -> str:
    """Keep a cited bullet one sentence without dropping its substantive cautions."""
    return re.sub(r"[.!?]\s+", "; ", " ".join(text.split()).rstrip(".!?"))


def _display_number(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value.is_integer():
        return str(int(value))
    # Unit-interval values allow printed rounding; larger values retain guard precision.
    return format(value, ".3g" if abs(value) <= 1 else ".10g")


def _member_reviews(
    claims: Sequence[ReportClaim], reports: Sequence[Path], root: Path,
    members: Sequence[Path], originals: Sequence[Path],
) -> list[_ReportMember]:
    counts = {member: 0 for member in members}
    if len(members) == 1:
        counts[members[0]] = len(claims)
    else:
        for claim in claims:
            path = Path(claim.file)
            candidate = path if path.is_absolute() else root / path
            matches = [
                (len(anchor.parts), member)
                for member, original in zip(members, originals, strict=True)
                for anchor in (member, original)
                if candidate.is_relative_to(anchor)
            ]
            if matches:
                counts[max(matches, key=lambda item: item[0])[1]] += 1
    return [
        _ReportMember(
            folder=member.name, claims_extracted=counts[member],
            report_files_read=sum(path.is_relative_to(member) for path in reports),
        )
        for member in members
    ]


def _report(
    claims: list[ReportClaim], validity: ValidityReport, audit_id: str, *, agents_run: bool,
    primary_name: str, related_names: Sequence[str], members: Sequence[_ReportMember],
    attribution: AttributionReport | None = None, attribution_id: str | None = None,
    insights: Sequence[ReportInsight] = (), limitations: Sequence[str] = (),
) -> str:
    reproduced = [claim for claim in claims if claim.status == "reproduced"]
    differs = [claim for claim in claims if claim.status == "differs"]
    uncertain = [claim for claim in claims if claim.status == "not_recomputable"]
    lines = [f"# Experiment review: {primary_name}"]
    if related_names:
        lines.append(f"Related folders: {', '.join(related_names)} [{audit_id}].")
    lines.append("## What was tested")
    qualifier = "" if agents_run else " (existing ledger)"
    for member in members:
        lines.append(f"- {member.folder}: claims extracted{qualifier} "
                     f"{member.claims_extracted}; report files read {member.report_files_read} "
                     f"[{audit_id}].")
    if agents_run:
        backed = sum(claim.recipe_backed for claim in claims)
        sampled_count = sum(claim.rederiver_sampled for claim in claims)
        lines.append(f"- Recipe-backed claims: {backed} of {len(claims)}; independent "
                     f"rederivation sampled {sampled_count} of {backed} recipe-backed claims "
                     f"and {sampled_count} of {len(claims)} claims [{audit_id}].")
    lines.append("## What held")
    for claim in reproduced[:8]:
        lines.append(f"- {_short(claim.text)}: stated {_display_number(claim.stated_number)}, "
                     f"receipts give {_display_number(claim.recomputed_number)} "
                     f"[{claim.claim_id}].")
    if not reproduced:
        lines.append(f"- No claim has a reproduced receipt calculation yet [{audit_id}].")
    lines.append("## What did not")
    warnings = sorted(
        (finding for finding in validity.findings if finding.severity in {"warning", "error"}),
        key=lambda finding: (
            finding.code not in {"cohort_mix", "missing_label_source", "label_provenance"},
            finding.severity != "error", finding.finding_id,
        ),
    )
    # Share the short section between numerical disagreements and validity warnings.
    for claim in differs[:4 if warnings else 8]:
        lines.append(f"- {_short(claim.text)}: stated {_display_number(claim.stated_number)}, "
                     f"receipts give {_display_number(claim.recomputed_number)} "
                     f"[{claim.claim_id}].")
    for finding in warnings[:8 - min(len(differs), 4 if warnings else 8)]:
        lines.append(f"- {_sentence(finding.message)} (count: {finding.count}) "
                     f"[{finding.finding_id}].")
    if not differs and not warnings:
        lines.append(f"- No receipt mismatch or validity warning was found [{audit_id}].")
    if attribution is not None:
        citation = attribution_id or audit_id
        lines.append("## Layer attribution")
        lines.append(f"- Prevention means reward blocked; detection means run flagged. "
                     f"Prevention layers together block {attribution.prevention_k} of "
                     f"{attribution.n} cases, detection layers together flag "
                     f"{attribution.detection_k}, combined {attribution.union_k} "
                     f"[{citation}].")
        if attribution.n < attribution.min_discordant_cases:
            lines.append(f"- Stage-4 uses exact paired McNemar, p < 0.05: with {attribution.n} "
                         f"cases no layer-value claim can be shown at any rate, because p < 0.05 "
                         f"needs at least {attribution.min_discordant_cases} cases only one layer "
                         f"catches [{citation}].")
        else:
            lines.append(f"- Stage-4 uses exact paired McNemar, p < 0.05: with {attribution.n} "
                         f"cases a layer needs at least {attribution.min_discordant_cases} cases "
                         f"only it catches, all in its favour [{citation}].")
        for layer in attribution.layers:
            low, high = layer.ci["low"], layer.ci["high"]
            interval = ("unavailable" if low is None or high is None else
                        f"{low:.1%}–{high:.1%}")
            p_value = "unavailable" if layer.p_value is None else f"{layer.p_value:.3g}"
            if layer.unique_rate is None:
                power = "the unique rate is unknown, so a sample size cannot be projected"
            elif layer.unique_rate == 0:
                power = "no sample size can show value at a unique rate of 0"
            elif layer.cases_for_80pct_power is None:
                power = "the sample-size search does not reach an 80% chance"
            else:
                power = f"at the observed unique rate about {layer.cases_for_80pct_power} "
                power += "cases give an 80% chance"
            lines.append(f"- {layer.name} ({layer.role}): {layer.k}/{layer.n}, Wilson interval "
                         f"{interval}; unique catches {layer.unique_catches}; exact paired "
                         f"p={p_value}; {power} [{citation}].")
        if attribution.equals_single_layer is not None:
            lines.append(f"- {attribution.equals_single_layer} alone equals the union: "
                         f"{attribution.union_k}/{attribution.n}; these cases do not show "
                         f"that both halves are needed [{citation}].")
        lines.append(f"- {_sentence(attribution.caveat)} [{citation}].")
    lines.append("## Skeptic challenges")
    challenges = [
        item for item in insights if item.lens == "skeptic" and item.flaw
        and item.check_status != "fail"
    ][:4]
    by_id = {claim.claim_id: claim for claim in claims}
    for item in challenges:
        targets = [by_id[claim_id] for claim_id in item.target_claim_ids if claim_id in by_id]
        allowed = list(_numbers([claim.model_dump() for claim in targets]))
        if not unsupported_numbers(item.title, allowed):
            lead = f"{_short(item.title)}; "
        else:
            # The title's numbers are not in the cited claims, so state what the claims say.
            about = " and ".join(_short(claim.text) for claim in targets[:2])
            lead = f"about {about}; " if about else ""
        verdict = item.referee_verdict or "not yet refereed"
        citations = ", ".join([item.insight_id, *item.target_claim_ids])
        flaw = (item.flaw or "other").replace("_", " ")
        lines.append(f"- {lead}{flaw}; severity {item.severity}; "
                     f"referee: {verdict} [{citations}].")
    if not challenges:
        lines.append(f"- No code-checked skeptic challenge is available [{audit_id}].")
    lines.append("## What is uncertain")
    for claim in uncertain[:2]:
        lines.append(f"- This claim lacks a successful receipt check [{claim.claim_id}].")
    for claim in [claim for claim in claims if claim.rederiver_agreement is False][:2]:
        lines.append(f"- An independent rederivation disagreed [{claim.claim_id}].")
    for item in [item for item in challenges if item.referee_verdict is None][:2]:
        lines.append(f"- This challenge has not been refereed [{item.insight_id}].")
    unique_limitations = list(dict.fromkeys(limitations))
    if not agents_run:
        lines.append(f"- No analyst agents ran; claims were not newly extracted [{audit_id}].")
        unique_limitations = [
            item for item in unique_limitations if not item.startswith("No analyst agents ran")
        ]
    for limitation in unique_limitations[:3]:
        lines.append(f"- {_sentence(limitation)} [{audit_id}].")
    lines.append(f"- Receipt agreement does not establish independent labels or performance "
                 f"on new cases [{audit_id}].")
    lines.extend([
        "## Next step",
        f"- Resolve validity warnings and missing or disagreeing receipts before choosing "
        f"a system; collect paired held-out cases before claiming layer value [{audit_id}].",
        "",
    ])
    formatted: list[str] = []
    for line in lines:
        if line.startswith("#"):
            if formatted and formatted[-1]:
                formatted.append("")
            formatted.extend([line, ""])
        elif line or not formatted or formatted[-1]:
            formatted.append(line)
    return "\n".join(formatted)


def analyze_experiment(
    folder: Path,
    out_dir: Path | None = None,
    *,
    related: Sequence[Path] = (),
    attribution_spec: Path | None = None,
    engine_factory: EngineFactory | None = None,
    now: datetime | None = None,
    run_agents: bool = True,
) -> list[Path]:
    """Write a supported claims ledger, validity audit, analysis and one-page review."""
    original = folder.resolve(strict=True)
    if not original.is_dir():
        raise NotADirectoryError(original)
    companions = sorted(path.resolve(strict=True) for path in related)
    for companion in companions:
        if not companion.is_dir():
            raise NotADirectoryError(companion)
    destination = (out_dir or experiment_output_dir(original, related=companions)).resolve()
    if destination == original or destination in companions:
        raise ValueError("analysis output must not overwrite a source folder")
    root, members = (
        _bundle(original, companions, destination) if companions else (original, [original])
    )
    timestamp = now or datetime.now(UTC)
    state = destination / ".state"
    validity = check_folder_validity(root, exclude=() if companions else (destination,))
    attribution = load_attribution(root, spec_path=attribution_spec)
    reports = sorted(
        path for member in members for path in _reports(member)
        if companions or not path.is_relative_to(destination)
    )
    source_paths = set(reports) | {root / receipt.path for receipt in validity.receipts}
    digests = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_paths)
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES
        and (bool(companions) or not path.is_relative_to(destination))
    }
    sampled: list[str] = []
    limitations: list[str] = []
    insights: list[ReportInsight] = []
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
            if companions:
                # Engine imports and rederivations resolve recipes from SourceRecord.folder.
                for source in engine.store.list_sources():
                    engine.store.upsert_source(source.model_copy(update={"folder": str(root)}))
            _extract_all(engine, limitations)
            records = engine.store.list_claims(include_failed=True)
            sampled = rederive_sample(record.claim_id for record in records
                                      if record.claim.recipe is not None
                                      and record.check.status != "fail")
            if sampled:
                _run_pass(engine, "rederiver", PassInputs(
                    lens="rederiver", claim_ids=tuple(sampled),
                ), limitations)
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
            newest = sorted(
                (record for record in records if record.check.status != "fail"),
                key=lambda record: (record.as_of or record.created_at, record.claim_id),
                reverse=True,
            )[:80]
            skeptic = _run_pass(engine, "skeptic", PassInputs(
                lens="skeptic", claim_ids=tuple(record.claim_id for record in newest),
            ), limitations)
            checked = sorted(
                (record for record in engine.store.list_insights(lens="skeptic")
                 if skeptic is not None and record.pass_id == skeptic.pass_id
                 and record.check.status != "fail"),
                key=lambda record: (record.created_at, record.insight_id),
            )
            if checked:
                _run_pass(engine, "referee", PassInputs(
                    lens="referee", insight_ids=tuple(record.insight_id for record in checked),
                ), limitations)
            insights = [_report_insight(record)
                        for record in engine.store.list_insights(lens="skeptic")]
        finally:
            engine.store.close()
    else:
        ledger = destination / "claims.jsonl"
        if ledger.exists():
            claims = _read_ledger(ledger)
        else:
            claims = []
            for member in members:
                for claim in _read_ledger(member / "claims.jsonl"):
                    path = Path(claim.file)
                    if companions and not path.is_absolute() and not any(
                        (root / path).is_relative_to(folder) for folder in members
                    ):
                        claim = claim.model_copy(update={
                            "file": (member / path).relative_to(root).as_posix(),
                        })
                    claims.append(claim)
        limitations.append("No analyst agents ran; existing claim entries were not re-extracted.")
    member_reviews = _member_reviews(
        claims, [path for path in reports if path.relative_to(root).as_posix() in digests],
        root, members, [original, *companions],
    )
    if sum(member.claims_extracted for member in member_reviews) != len(claims):
        limitations.append("Some claim source paths do not identify a member folder.")
    _cohort_findings(claims, validity)
    attribution_id: str | None = None
    if attribution is not None:
        attribution_id = f"V-{len(validity.findings) + 1:02d}"
        validity.findings.append(ValidityFinding(
            finding_id=attribution_id, code="layer_attribution", severity="info",
            count=attribution.n,
            message="Stage-4 requires exact paired McNemar p < 0.05; layer-value planning "
                    "uses an 80% chance of reaching the required unique catches. "
                    "Prevention blocks reward; detection flags a run.",
        ))
    counts = {status: sum(claim.status == status for claim in claims)
              for status in ("reproduced", "differs", "not_recomputable")}
    audit_id = f"V-{len(validity.findings) + 1:02d}"
    validity.findings.append(ValidityFinding(
        finding_id=audit_id, code="claim_summary", severity="info", count=len(claims),
        message=(
            f"Available claim statuses: {json.dumps(counts, sort_keys=True)}. "
            f"Recipe-backed claims: {sum(claim.recipe_backed for claim in claims)} of "
            f"{len(claims)}. Independent rederivation sampled "
            f"{sum(claim.rederiver_sampled for claim in claims)} of "
            f"{sum(claim.recipe_backed for claim in claims)} recipe-backed claims and "
            f"{sum(claim.rederiver_sampled for claim in claims)} of {len(claims)} claims. "
            "Members reviewed: "
            f"{json.dumps([member.model_dump() for member in member_reviews], sort_keys=True)}. "
            f"Related folders: {', '.join(path.name for path in companions)}. "
            "Receipt agreement does not establish independent labels or performance on new cases. "
            f"Analyst agents ran: {run_agents}. Limitations: {' '.join(limitations)}"
        ),
    ))
    confidence: Literal["low", "medium", "high"] = "low"
    if (
        counts["reproduced"] and not counts["differs"] and not counts["not_recomputable"]
        and not limitations and not any(f.severity != "info" for f in validity.findings)
    ):
        confidence = "medium"
    analysis = ExperimentAnalysis(
        folder=str(original), related_folders=[str(path) for path in companions],
        generated_at=timestamp, source_digests=digests, validity=validity,
        evidence=claims, insights=insights, confidence=confidence, agents_run=run_agents,
        rederiver_sample=sampled, limitations=limitations,
        alternative_explanations=[
            "Shared or in-sample labels can explain agreement without showing generalization.",
            "Missing receipts or changing model routes can change the apparent result.",
        ],
    )
    report = _report(
        claims, validity, audit_id, agents_run=run_agents, attribution=attribution,
        attribution_id=attribution_id, insights=insights, limitations=limitations,
        primary_name=original.name, related_names=[path.name for path in companions],
        members=member_reviews,
    )
    allowed = list(_numbers([claim.model_dump() for claim in claims]))
    allowed.extend(_numbers(validity.model_dump()))
    if attribution is not None:
        allowed.extend(_numbers(attribution.model_dump()))
    for insight in insights:
        allowed.append(insight.insight_id)
        allowed.extend(insight.target_claim_ids)
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
