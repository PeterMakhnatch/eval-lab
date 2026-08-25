"""Tests for disposable trajectory context pack compiler."""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evallab.behavior_episodes import BehaviorEpisode
from evallab.schemas import (
    AnalysisEvidenceCitation,
    AnalysisProvenance,
    AnalysisReview,
    AnalysisSourceDigests,
    TrialAnalysisOutput,
    TrialAnalysisSidecar,
)
from evallab.semantic_facts import (
    CapabilityOpportunity,
    EvidenceCoverage,
    NormalizedFactBundle,
)
from evallab.trajectory_context import (
    build_trajectory_context,
    latest_review,
    reviews_by_analysis,
)


def _digest(value: str = "seed") -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _make_sidecar(
    *,
    analysis_id: UUID | None = None,
    trial_uuid: UUID | None = None,
    validation_status: str = "valid",
    validation_errors: list[str] | None = None,
    summary: str = "Test analysis summary.",
    evidence: list[AnalysisEvidenceCitation] | None = None,
    files: dict[str, str] | None = None,
) -> TrialAnalysisSidecar:
    a_id = analysis_id or uuid4()
    t_id = trial_uuid or UUID("00000000-0000-0000-0000-000000000001")
    if evidence is None:
        evidence = [
            AnalysisEvidenceCitation(
                path=f"runs/{t_id}/result.json",
                step_id=None,
                tool_call_id=None,
                supports="verdict",
            )
        ]
    if validation_errors is None:
        validation_errors = ["Error found"] if validation_status == "invalid" else []

    return TrialAnalysisSidecar(
        schema_version=1,
        analysis_id=a_id,
        job_id=UUID("00000000-0000-0000-0000-000000000002"),
        source_trial_id=t_id,
        source_trial_path=f"runs/{t_id}",
        source_digests=AnalysisSourceDigests(
            result=_digest("result"),
            task=_digest("task"),
            trajectory=_digest("trajectory"),
            files=files or {},
        ),
        analysis_provenance=AnalysisProvenance(
            agent="test-agent",
            agent_version="1.0.0",
            model="test-model",
            prompt_digest=_digest("prompt"),
            rubric_digest=_digest("rubric"),
            output_schema_digest=_digest("schema"),
            created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        ),
        output=TrialAnalysisOutput(
            validity="valid_agent_attempt",
            primary_category="planning",
            summary=summary,
            earliest_failure_step_id=1,
            evidence=evidence,
            proposed_discriminator="discriminator",
            confidence="high",
        ),
        validation_status=validation_status,  # type: ignore[arg-type]
        validation_errors=validation_errors,
        raw_response_digest=_digest("raw_response"),
    )


def _make_review(
    *,
    review_id: UUID | None = None,
    analysis_id: UUID,
    disposition: str = "accepted",
    reviewer: str = "peter",
    reviewed_at: datetime | None = None,
    superseded_by: UUID | None = None,
    rationale: str = "Review rationale",
) -> AnalysisReview:
    r_id = review_id or uuid4()
    r_at = reviewed_at or datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    return AnalysisReview(
        schema_version=1,
        review_id=r_id,
        analysis_id=analysis_id,
        disposition=disposition,  # type: ignore[arg-type]
        rationale=rationale,
        reviewer=reviewer,
        reviewed_at=r_at,
        superseded_by=superseded_by,
    )


def _make_episode(
    *,
    episode_id: str | None = None,
    trial_id: str = "00000000-0000-0000-0000-000000000001",
    document_id: str = "agent/trajectory.json",
    status: str = "confirmed",
    label: str = "loop_detected",
    rationale: str = "Episode rationale text.",
    evidence_step_ids: tuple[int, ...] = (1,),
    evidence_span_ids: tuple[str, ...] = (),
    source_sha256: str | None = None,
    input_digest: str | None = None,
    reviewed_at: datetime | None = None,
) -> BehaviorEpisode:
    ep_id = episode_id or _digest(f"ep-{trial_id}-{label}")
    created = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    if reviewed_at is None and status in {"reviewed", "confirmed", "rejected"}:
        reviewed_at = datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC)
    return BehaviorEpisode(
        schema_version=1,
        episode_id=ep_id,
        trial_id=trial_id,
        document_id=document_id,
        start_step=1,
        end_step=10,
        label=label,
        status=status,  # type: ignore[arg-type]
        evidence_step_ids=evidence_step_ids,
        evidence_span_ids=evidence_span_ids,
        annotator_kind="code",
        annotator_id="detector",
        detector_version="v1",
        source_sha256=source_sha256 or _digest("source_traj"),
        input_digest=input_digest or _digest("input_span"),
        rationale=rationale,
        created_at=created,
        updated_at=created,
        reviewed_at=reviewed_at,
    )


def _make_opportunity(
    *,
    opportunity_id: str = "opp-1",
    trial_id: str = "00000000-0000-0000-0000-000000000001",
    benchmark: str = "bench-1",
    construct: str = "retrieval",
    eligible: bool | None = True,
    required_evidence: tuple[str, ...] = ("doc_id",),
    missing_evidence: tuple[str, ...] = (),
    start_step: int | None = 1,
) -> CapabilityOpportunity:
    return CapabilityOpportunity(
        opportunity_id=opportunity_id,
        trial_id=trial_id,
        benchmark=benchmark,
        construct=construct,
        eligible=eligible,
        required_evidence=required_evidence,
        missing_evidence=missing_evidence,
        start_step=start_step,
        source_ref=f"runs/{trial_id}/trajectory.json",
        source_digest=_digest(f"opp-{opportunity_id}"),
        provenance_kind="mechanical",
    )


def _make_coverage(
    *,
    trial_id: str = "00000000-0000-0000-0000-000000000001",
    benchmark: str = "bench-1",
    construct: str = "planning",
    exposed: bool = True,
    eligible: bool | None = True,
    required_evidence: tuple[str, ...] = ("plan",),
    observed_evidence: tuple[str, ...] = ("plan",),
    missing_evidence: tuple[str, ...] = (),
    analysis_ready: bool | None = True,
) -> EvidenceCoverage:
    return EvidenceCoverage(
        trial_id=trial_id,
        benchmark=benchmark,
        construct=construct,
        exposed=exposed,
        eligible=eligible,
        required_evidence=required_evidence,
        observed_evidence=observed_evidence,
        missing_evidence=missing_evidence,
        analysis_ready=analysis_ready,
        source_ref=f"runs/{trial_id}/coverage.json",
        source_digest=_digest(f"cov-{trial_id}-{construct}"),
        provenance_kind="derived",
    )


def test_review_filtering_default_keeps_only_accepted_valid_analyses() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    a_accepted_valid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000011"),
        trial_uuid=trial_uuid,
        validation_status="valid",
    )
    r_accepted = _make_review(analysis_id=a_accepted_valid.analysis_id, disposition="accepted")

    a_rejected_valid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000012"),
        trial_uuid=trial_uuid,
        validation_status="valid",
    )
    r_rejected = _make_review(analysis_id=a_rejected_valid.analysis_id, disposition="rejected")

    a_needs_revision_valid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000013"),
        trial_uuid=trial_uuid,
        validation_status="valid",
    )
    r_needs_revision = _make_review(
        analysis_id=a_needs_revision_valid.analysis_id, disposition="needs_revision"
    )

    a_unreviewed_valid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000014"),
        trial_uuid=trial_uuid,
        validation_status="valid",
    )

    a_accepted_invalid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000015"),
        trial_uuid=trial_uuid,
        validation_status="invalid",
        validation_errors=["Schema invalid"],
    )
    r_accepted_invalid = _make_review(
        analysis_id=a_accepted_invalid.analysis_id, disposition="accepted"
    )

    pack = build_trajectory_context(
        trial_id=trial_id,
        analyses=[
            a_accepted_valid,
            a_rejected_valid,
            a_needs_revision_valid,
            a_unreviewed_valid,
            a_accepted_invalid,
        ],
        reviews=[r_accepted, r_rejected, r_needs_revision, r_accepted_invalid],
    )

    assert len(pack.entries) == 1
    assert pack.entries[0].entry_id == str(a_accepted_valid.analysis_id)
    assert pack.entries[0].status == "accepted"
    assert pack.entries[0].label is None
    assert pack.entries[0].kind == "analysis"
    assert len(pack.unknowns) == 0
    assert pack.truncation.truncated is False


def test_supersession_uses_latest_review_not_older_accepted() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    a1 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000021"),
        trial_uuid=trial_uuid,
        summary="Original analysis.",
    )
    a2 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000022"),
        trial_uuid=trial_uuid,
        summary="Replacement analysis.",
    )

    r1_accepted = _make_review(
        review_id=UUID("00000000-0000-0000-0000-000000000031"),
        analysis_id=a1.analysis_id,
        disposition="accepted",
        reviewed_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC),
    )
    r2_superseded = _make_review(
        review_id=UUID("00000000-0000-0000-0000-000000000032"),
        analysis_id=a1.analysis_id,
        disposition="superseded",
        superseded_by=a2.analysis_id,
        reviewed_at=datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC),
    )
    r3_accepted = _make_review(
        review_id=UUID("00000000-0000-0000-0000-000000000033"),
        analysis_id=a2.analysis_id,
        disposition="accepted",
        reviewed_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
    )

    assert latest_review([r1_accepted, r2_superseded]) == r2_superseded
    assert latest_review([r2_superseded, r1_accepted]) == r2_superseded

    grouped = reviews_by_analysis([r2_superseded, r1_accepted, r3_accepted])
    assert grouped[str(a1.analysis_id)] == (r1_accepted, r2_superseded)
    assert grouped[str(a2.analysis_id)] == (r3_accepted,)

    pack = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a1, a2],
        reviews=[r1_accepted, r2_superseded, r3_accepted],
    )
    assert len(pack.entries) == 1
    assert pack.entries[0].entry_id == str(a2.analysis_id)
    assert pack.entries[0].claim == "Replacement analysis."
    assert pack.entries[0].status == "accepted"
    assert pack.entries[0].label is None

    pack_shuffled = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a2, a1],
        reviews=[r3_accepted, r2_superseded, r1_accepted],
    )
    assert pack.entries == pack_shuffled.entries


def test_invalid_analysis_excluded_even_with_accepted_review() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    a_invalid = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000041"),
        trial_uuid=trial_uuid,
        validation_status="invalid",
        validation_errors=["Invalid syntax in output."],
    )
    r_accepted = _make_review(analysis_id=a_invalid.analysis_id, disposition="accepted")

    pack_default = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_invalid],
        reviews=[r_accepted],
    )
    assert len(pack_default.entries) == 0
    assert len(pack_default.unknowns) == 0

    pack_opt_in = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_invalid],
        reviews=[r_accepted],
        include_candidates=True,
        include_rejected=True,
    )
    assert len(pack_opt_in.entries) == 0
    assert len(pack_opt_in.unknowns) == 0


def test_candidate_opt_in_is_labeled() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    ep_candidate = _make_episode(
        episode_id=_digest("candidate_ep"),
        trial_id=trial_id,
        status="candidate",
        label="loop_candidate",
    )
    ep_confirmed = _make_episode(
        episode_id=_digest("confirmed_ep"),
        trial_id=trial_id,
        status="confirmed",
        label="loop_confirmed",
    )
    ep_reviewed = _make_episode(
        episode_id=_digest("reviewed_ep"),
        trial_id=trial_id,
        status="reviewed",
        label="loop_reviewed",
    )

    a_unreviewed = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000051"),
        trial_uuid=trial_uuid,
    )
    a_needs_revision = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000052"),
        trial_uuid=trial_uuid,
    )
    r_needs_revision = _make_review(
        analysis_id=a_needs_revision.analysis_id, disposition="needs_revision"
    )

    pack_default = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_unreviewed, a_needs_revision],
        reviews=[r_needs_revision],
        episodes=[ep_candidate, ep_confirmed, ep_reviewed],
    )
    assert len(pack_default.entries) == 2
    assert {e.entry_id for e in pack_default.entries} == {
        ep_confirmed.episode_id,
        ep_reviewed.episode_id,
    }
    assert all(e.label is None for e in pack_default.entries)

    pack_opt = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_unreviewed, a_needs_revision],
        reviews=[r_needs_revision],
        episodes=[ep_candidate, ep_confirmed, ep_reviewed],
        include_candidates=True,
    )
    assert len(pack_opt.entries) == 5

    ep_cand_entry = next(e for e in pack_opt.entries if e.entry_id == ep_candidate.episode_id)
    assert ep_cand_entry.label == "candidate"
    assert ep_cand_entry.status == "candidate"

    a_unrev_entry = next(
        e for e in pack_opt.entries if e.entry_id == str(a_unreviewed.analysis_id)
    )
    assert a_unrev_entry.label == "candidate"
    assert a_unrev_entry.status == "unreviewed"

    a_rev_entry = next(
        e for e in pack_opt.entries if e.entry_id == str(a_needs_revision.analysis_id)
    )
    assert a_rev_entry.label == "candidate"
    assert a_rev_entry.status == "needs_revision"

    ep_conf_entry = next(e for e in pack_opt.entries if e.entry_id == ep_confirmed.episode_id)
    assert ep_conf_entry.label is None
    assert ep_conf_entry.status == "confirmed"

    ep_rev_entry = next(e for e in pack_opt.entries if e.entry_id == ep_reviewed.episode_id)
    assert ep_rev_entry.label is None
    assert ep_rev_entry.status == "reviewed"


def test_exact_citation_and_digest_preservation() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    custom_path = "custom/trial_evidence.txt"
    custom_digest = _digest("custom_file_contents")

    sidecar = _make_sidecar(
        trial_uuid=trial_uuid,
        files={custom_path: custom_digest},
        evidence=[
            AnalysisEvidenceCitation(
                path=custom_path,
                step_id=5,
                tool_call_id="tool_call_99",
                supports="custom_criterion",
            )
        ],
    )
    review = _make_review(analysis_id=sidecar.analysis_id, disposition="accepted")

    ep_src_digest = _digest("source_trajectory_digest")
    ep_inp_digest = _digest("input_span_digest")
    episode = _make_episode(
        trial_id=trial_id,
        document_id="agent/trajectory.json",
        evidence_step_ids=(2, 6),
        evidence_span_ids=("span_42", "span_43"),
        source_sha256=ep_src_digest,
        input_digest=ep_inp_digest,
        label="loop_detected",
    )

    pack = build_trajectory_context(
        trial_id=trial_id,
        analyses=[sidecar],
        reviews=[review],
        episodes=[episode],
    )

    analysis_entry = next(e for e in pack.entries if e.kind == "analysis")
    assert len(analysis_entry.citations) == 1
    cit = analysis_entry.citations[0]
    assert cit.path == custom_path
    assert cit.digest == custom_digest
    assert cit.step_id == 5
    assert cit.tool_call_id == "tool_call_99"
    assert cit.supports == "custom_criterion"

    episode_entry = next(e for e in pack.entries if e.kind == "episode")
    assert len(episode_entry.citations) == 4

    assert episode_entry.citations[0].path == "agent/trajectory.json"
    assert episode_entry.citations[0].digest == ep_src_digest
    assert episode_entry.citations[0].step_id == 2
    assert episode_entry.citations[0].tool_call_id is None
    assert episode_entry.citations[0].supports == "loop_detected"

    assert episode_entry.citations[1].path == "agent/trajectory.json"
    assert episode_entry.citations[1].digest == ep_src_digest
    assert episode_entry.citations[1].step_id == 6
    assert episode_entry.citations[1].tool_call_id is None
    assert episode_entry.citations[1].supports == "loop_detected"

    assert episode_entry.citations[2].path == "agent/trajectory.json"
    assert episode_entry.citations[2].digest == ep_inp_digest
    assert episode_entry.citations[2].step_id is None
    assert episode_entry.citations[2].tool_call_id == "span_42"
    assert episode_entry.citations[2].supports == "loop_detected"

    assert episode_entry.citations[3].path == "agent/trajectory.json"
    assert episode_entry.citations[3].digest == ep_inp_digest
    assert episode_entry.citations[3].step_id is None
    assert episode_entry.citations[3].tool_call_id == "span_43"
    assert episode_entry.citations[3].supports == "loop_detected"


def test_semantic_opportunity_unknowns_are_preserved() -> None:
    trial_id = "00000000-0000-0000-0000-000000000001"

    opp_eligible = _make_opportunity(
        opportunity_id="opp-eligible",
        trial_id=trial_id,
        benchmark="bench-1",
        construct="retrieval",
        eligible=True,
        required_evidence=("doc_id",),
        missing_evidence=(),
    )
    opp_missing = _make_opportunity(
        opportunity_id="opp-missing",
        trial_id=trial_id,
        benchmark="bench-1",
        construct="retrieval",
        eligible=True,
        required_evidence=("doc_id", "trace_id"),
        missing_evidence=("trace_id",),
    )
    cov_unready = _make_coverage(
        trial_id=trial_id,
        benchmark="bench-1",
        construct="planning",
        exposed=False,
        eligible=None,
        required_evidence=("plan",),
        observed_evidence=(),
        missing_evidence=("plan",),
        analysis_ready=None,
    )

    bundle = NormalizedFactBundle(
        capability_opportunities=(opp_eligible, opp_missing),
        evidence_coverage=(cov_unready,),
    )

    pack = build_trajectory_context(trial_id=trial_id, facts=bundle)

    assert len(pack.entries) == 1
    assert pack.entries[0].kind == "semantic_fact"
    assert pack.entries[0].entry_id == "opp-eligible"
    assert pack.entries[0].label is None
    assert pack.entries[0].status == "eligible"

    assert len(pack.unknowns) == 2
    unk_opp = next(u for u in pack.unknowns if u.entry_id == "opp-missing")
    assert unk_opp.kind == "unknown"
    assert unk_opp.label == "unknown"
    assert unk_opp.status == "missing_evidence"
    assert unk_opp.provenance["missing_evidence"] == ("trace_id",)

    unk_cov = next(u for u in pack.unknowns if u.source == "EvidenceCoverage")
    assert unk_cov.kind == "unknown"
    assert unk_cov.label == "unknown"
    assert unk_cov.status == "unexposed"
    assert unk_cov.provenance["exposed"] is False
    assert unk_cov.provenance["analysis_ready"] is None
    assert unk_cov.provenance["missing_evidence"] == ("plan",)


def test_deterministic_output_under_input_shuffling() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    a1 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000061"),
        trial_uuid=trial_uuid,
        summary="Summary A1",
        evidence=[
            AnalysisEvidenceCitation(
                path=f"runs/{trial_id}/result.json",
                step_id=None,
                tool_call_id=None,
                supports="s1",
            ),
            AnalysisEvidenceCitation(
                path=f"runs/{trial_id}/agent/trajectory.json",
                step_id=2,
                tool_call_id=None,
                supports="s2",
            ),
        ],
    )
    r1 = _make_review(analysis_id=a1.analysis_id, disposition="accepted")

    a2 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000062"),
        trial_uuid=trial_uuid,
        summary="Summary A2",
    )
    r2 = _make_review(analysis_id=a2.analysis_id, disposition="rejected")

    ep1 = _make_episode(
        episode_id=_digest("ep1"),
        trial_id=trial_id,
        status="confirmed",
        evidence_step_ids=(1, 3),
        evidence_span_ids=("span-1",),
    )
    ep2 = _make_episode(
        episode_id=_digest("ep2"),
        trial_id=trial_id,
        status="candidate",
    )

    opp1 = _make_opportunity(
        opportunity_id="opp-1",
        trial_id=trial_id,
        eligible=True,
        missing_evidence=(),
    )
    opp2 = _make_opportunity(
        opportunity_id="opp-2",
        trial_id=trial_id,
        eligible=True,
        required_evidence=("e1", "e2"),
        missing_evidence=("e2",),
    )
    cov1 = _make_coverage(
        trial_id=trial_id,
        construct="planning",
        exposed=True,
        eligible=True,
        missing_evidence=(),
        analysis_ready=True,
    )
    bundle = NormalizedFactBundle(
        capability_opportunities=(opp1, opp2),
        evidence_coverage=(cov1,),
    )

    analyses = [a1, a2]
    reviews = [r1, r2]
    episodes = [ep1, ep2]

    pack1 = build_trajectory_context(
        trial_id=trial_id,
        analyses=analyses,
        reviews=reviews,
        episodes=episodes,
        facts=bundle,
        include_candidates=True,
        include_rejected=True,
    )

    shuffled_analyses = list(analyses)
    random.Random(0).shuffle(shuffled_analyses)
    shuffled_reviews = list(reviews)
    random.Random(1).shuffle(shuffled_reviews)
    shuffled_episodes = list(episodes)
    random.Random(2).shuffle(shuffled_episodes)

    pack2 = build_trajectory_context(
        trial_id=trial_id,
        analyses=shuffled_analyses,
        reviews=shuffled_reviews,
        episodes=shuffled_episodes,
        facts=bundle,
        include_candidates=True,
        include_rejected=True,
    )

    json1 = json.dumps(pack1.to_dict(), sort_keys=True, separators=(",", ":"))
    json2 = json.dumps(pack2.to_dict(), sort_keys=True, separators=(",", ":"))
    assert json1 == json2

    md1 = pack1.to_markdown()
    md2 = pack2.to_markdown()
    assert md1 == md2

    for e1, e2 in zip(pack1.entries, pack2.entries, strict=True):
        assert e1.citations == e2.citations


def test_bounded_max_entries_does_not_truncate_citations() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    a1 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000071"),
        trial_uuid=trial_uuid,
        summary="Analysis 1 with multiple citations.",
        evidence=[
            AnalysisEvidenceCitation(
                path=f"runs/{trial_id}/result.json",
                step_id=None,
                tool_call_id=None,
                supports="s1",
            ),
            AnalysisEvidenceCitation(
                path=f"runs/{trial_id}/agent/trajectory.json",
                step_id=1,
                tool_call_id=None,
                supports="s2",
            ),
            AnalysisEvidenceCitation(
                path=f"runs/{trial_id}/agent/trajectory.json",
                step_id=2,
                tool_call_id=None,
                supports="s3",
            ),
        ],
    )
    r1 = _make_review(analysis_id=a1.analysis_id, disposition="accepted")

    a2 = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000072"),
        trial_uuid=trial_uuid,
        summary="Analysis 2.",
    )
    r2 = _make_review(analysis_id=a2.analysis_id, disposition="accepted")

    ep1 = _make_episode(
        episode_id=_digest("ep-bounded"),
        trial_id=trial_id,
        status="confirmed",
    )

    opp_unknown = _make_opportunity(
        opportunity_id="opp-unknown",
        trial_id=trial_id,
        eligible=True,
        required_evidence=("x", "y"),
        missing_evidence=("y",),
    )
    bundle = NormalizedFactBundle(capability_opportunities=(opp_unknown,))

    pack = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a1, a2],
        reviews=[r1, r2],
        episodes=[ep1],
        facts=bundle,
        max_entries=1,
    )

    assert pack.truncation.truncated is True
    assert pack.truncation.max_entries == 1
    assert pack.truncation.included_count == 1
    assert pack.truncation.omitted_count == 2
    assert pack.truncation.omitted_entry_ids == (str(a2.analysis_id), ep1.episode_id)

    assert len(pack.entries) == 1
    assert pack.entries[0].entry_id == str(a1.analysis_id)
    assert len(pack.entries[0].citations) == 3

    assert len(pack.unknowns) == 1
    assert pack.unknowns[0].entry_id == "opp-unknown"


def test_rejected_opt_in_is_labeled() -> None:
    trial_uuid = UUID("00000000-0000-0000-0000-000000000001")
    trial_id = str(trial_uuid)

    ep_rejected = _make_episode(
        episode_id=_digest("ep-rejected"),
        trial_id=trial_id,
        status="rejected",
        label="rejected_behavior",
    )

    a_rejected = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000081"),
        trial_uuid=trial_uuid,
    )
    r_rejected = _make_review(analysis_id=a_rejected.analysis_id, disposition="rejected")

    a_superseded = _make_sidecar(
        analysis_id=UUID("00000000-0000-0000-0000-000000000082"),
        trial_uuid=trial_uuid,
    )
    r_superseded = _make_review(
        analysis_id=a_superseded.analysis_id,
        disposition="superseded",
        superseded_by=uuid4(),
    )

    pack_default = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_rejected, a_superseded],
        reviews=[r_rejected, r_superseded],
        episodes=[ep_rejected],
    )
    assert len(pack_default.entries) == 0

    pack_opt = build_trajectory_context(
        trial_id=trial_id,
        analyses=[a_rejected, a_superseded],
        reviews=[r_rejected, r_superseded],
        episodes=[ep_rejected],
        include_rejected=True,
    )
    assert len(pack_opt.entries) == 3

    ep_entry = next(e for e in pack_opt.entries if e.entry_id == ep_rejected.episode_id)
    assert ep_entry.label == "rejected"
    assert ep_entry.status == "rejected"

    a_rej_entry = next(e for e in pack_opt.entries if e.entry_id == str(a_rejected.analysis_id))
    assert a_rej_entry.label == "rejected"
    assert a_rej_entry.status == "rejected"

    a_sup_entry = next(e for e in pack_opt.entries if e.entry_id == str(a_superseded.analysis_id))
    assert a_sup_entry.label == "superseded"
    assert a_sup_entry.status == "superseded"
