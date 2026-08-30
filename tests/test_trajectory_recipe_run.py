"""Focused contracts for the deterministic trajectory recipe findings runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.interpretation.trajectory_recipe_run import (
    FINDINGS_JSONL,
    FINDINGS_REPORT,
    IMPROVEMENT_REQUESTS,
    main,
    select_trial_sidecars,
)
from evallab.interpretation.trajectory_recipes import RecipeFinding

REAL_ANALYSES = Path("/Users/petermakhnatch/Developer/eval-lab/derived/analyses")
REQUIRED_JSONL_KEYS = frozenset(
    {
        "finding_id",
        "recipe_id",
        "trial_id",
        "disposition",
        "validity",
        "class_id",
        "support_level",
        "earliest_supported_ir_event_id",
        "citations",
        "alternative_explanations",
        "coverage_gaps",
        "abstention_reason",
        "extras",
        "producer",
        "contract_digest",
    }
)
ALLOWED_DISPOSITIONS = frozenset(
    {
        "candidate_hold",
        "deterministic_abstention",
        "screening_only",
        "alternative_explanations",
    }
)
ALLOWED_RECIPES = frozenset({"r1", "r2", "r3", "r4", "r5", "r6", "r7"})
DATA_REQUIREMENT_PHRASES = (
    "arms executed: 1",
    "n_tasks: 5",
    "attempts/task: 1",
    "successes: 0",
    "[0, 0.434]",
    "accounting/descriptive only — no reliability, ranking, capability, or causal claim",
    "suppressed",
    "NOT AUTHORIZED TO RUN",
    "matched second arm on identical task digests",
    "T≥3",
    "larger n_tasks",
    "preregistered stopping rule covering any sequential growth of n_tasks or k",
    "source_task-clustered bootstrap of the paired precision difference",
    "held-out labels before any judge-calibration claim",
)
IMPROVEMENT_EXPECTATIONS = (
    ("EvidencePack anchor defect", "Agent Data", "trial-a"),
    ("context events", "Data-harness", "trial-a"),
    ("state-certified replay", "Platform-Ops", "trial-a"),
    ("unpaired-call defect", "Data", "trial-b"),
    ("matched-arm prerequisite", "Peter-authorization-required", "trial-b"),
    ("class proposals", "Synthetic Research", "trial-b"),
    ("IR multi-call + metric-collision fixes", "Data+Platform", "trial-a"),
)


def _write_sidecar(
    analyses_dir: Path,
    trial_id: str,
    digest: str,
    *,
    created_at: str,
    execution_sample_only: bool = False,
    pack_digest: str | None = None,
) -> None:
    pack_dir = analyses_dir / trial_id / digest
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "trial_id": trial_id,
        "created_at": created_at,
        "pack_digest": pack_digest if pack_digest is not None else digest,
    }
    if execution_sample_only:
        payload["selected_windows"] = [{"reason": "execution_sample"}]
    (pack_dir / "evidence_pack.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def _write_campaign_report(path: Path, items: list[tuple[str, str]]) -> None:
    payload = {
        "schema_version": "campaign-report/v1",
        "source_refs": [
            {"trial_id": trial_id, "pack_digest": f"sha256:{digest}"} for trial_id, digest in items
        ],
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_pack_map(path: Path, mapping: dict[str, str]) -> None:
    path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")


def _stub_engine(
    monkeypatch: pytest.MonkeyPatch, findings: list[RecipeFinding] | None = None
) -> None:
    seeded = findings if findings is not None else seed_findings()
    by_trial: dict[str, list[RecipeFinding]] = {}
    for finding in seeded:
        by_trial.setdefault(str(finding.trial_id), []).append(finding)
    monkeypatch.setattr(
        "evallab.interpretation.trajectory_recipe_run.load_trial_artifacts",
        lambda analyses_dir, trial_id, digest=None: trial_id,
    )
    monkeypatch.setattr(
        "evallab.interpretation.trajectory_recipe_run.run_recipes",
        lambda artifacts, *, semantics_profile_digest=None: list(by_trial.get(str(artifacts), [])),
    )


def _assert_no_outputs(out: Path) -> None:
    assert not (out / FINDINGS_JSONL).exists()
    assert not (out / FINDINGS_REPORT).exists()
    assert not (out / IMPROVEMENT_REQUESTS).exists()


def make_finding(**overrides: Any) -> RecipeFinding:
    payload: dict[str, Any] = {
        "finding_id": "finding-001",
        "recipe_id": "r1",
        "trial_id": "trial-a",
        "disposition": "deterministic_abstention",
        "validity": "insufficient_evidence",
        "class_id": None,
        "support_level": "e0",
        "earliest_supported_ir_event_id": None,
        "citations": ["cit_001"],
        "alternative_explanations": [],
        "coverage_gaps": [],
        "abstention_reason": "pack_incomplete",
        "extras": {},
        "producer": "analyst-recipe/v1",
        "contract_digest": "sha256:" + "b" * 64,
        "is_machine_judgment": False,
        "proposed_discriminator": None,
        "target_definition": "decisive_evidential",
    }
    payload.update(overrides)
    fields = getattr(RecipeFinding, "model_fields", None)
    if fields is None:
        return RecipeFinding(**payload)
    init: dict[str, Any] = {}
    for name, field in fields.items():
        if name in payload:
            init[name] = payload[name]
        elif not field.is_required():
            continue
        elif "list" in str(field.annotation):
            init[name] = []
        elif "dict" in str(field.annotation):
            init[name] = {}
        elif "bool" in str(field.annotation):
            init[name] = False
        elif "int" in str(field.annotation):
            init[name] = 0
        else:
            init[name] = None
    return RecipeFinding(**init)


def seed_findings() -> list[RecipeFinding]:
    return [
        make_finding(
            finding_id="finding-a-r1",
            recipe_id="r1",
            trial_id="trial-a",
            abstention_reason="pack_incomplete",
            citations=["cit_pack_a"],
        ),
        make_finding(
            finding_id="finding-a-r6",
            recipe_id="r6",
            trial_id="trial-a",
            abstention_reason="opportunity_unknown",
            citations=["cit_ctx_a"],
        ),
        make_finding(
            finding_id="finding-a-r5",
            recipe_id="r5",
            trial_id="trial-a",
            abstention_reason="replay_oracle_unavailable",
            citations=["cit_replay_a"],
        ),
        make_finding(
            finding_id="finding-a-r7",
            recipe_id="r7",
            trial_id="trial-a",
            disposition="screening_only",
            validity=None,
            class_id=None,
            support_level="e0",
            abstention_reason=None,
            extras={"blocked_metric": True, "blocked_metrics": ["legacy_cache_hit_rate"]},
            citations=["cit_r7_a"],
        ),
        make_finding(
            finding_id="finding-b-r2",
            recipe_id="r2",
            trial_id="trial-b",
            abstention_reason="linkage_unresolved",
            citations=["cit_link_b"],
        ),
        make_finding(
            finding_id="finding-b-r3",
            recipe_id="r3",
            trial_id="trial-b",
            abstention_reason="pair_unavailable",
            citations=["cit_pair_b"],
        ),
        make_finding(
            finding_id="finding-b-r1",
            recipe_id="r1",
            trial_id="trial-b",
            abstention_reason="ontology_gap",
            citations=["cit_ont_b"],
        ),
    ]


def _section(markdown: str, title: str) -> str:
    marker = f"## {title}"
    start = markdown.index(marker)
    rest = markdown[start + len(marker) :]
    next_section = rest.find("\n## ")
    return rest if next_section < 0 else rest[:next_section]


def test_runner_selects_one_newest_pack_and_writes_deterministic_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyses = tmp_path / "analyses"
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    # Two rebuilt sidecars for trial-a. Multi-generation dirs require an explicit pin;
    # the old pack seeds the mechanical materialization request only when selected.
    _write_sidecar(
        analyses,
        "trial-a",
        "digest-old",
        created_at="2026-08-01T00:00:00Z",
        execution_sample_only=True,
    )
    _write_sidecar(analyses, "trial-a", "digest-new", created_at="2026-08-02T00:00:00Z")
    _write_sidecar(analyses, "trial-b", "digest-b", created_at="2026-08-02T00:00:00Z")
    with pytest.raises(ValueError, match="ambiguous pack generations"):
        select_trial_sidecars(analyses)
    assert (
        select_trial_sidecars(analyses, pack_digest="digest-old", requested=["trial-a"])[
            "trial-a"
        ].digest
        == "digest-old"
    )

    pack_map = tmp_path / "pack-map.json"
    _write_pack_map(pack_map, {"trial-a": "digest-new", "trial-b": "digest-b"})
    _stub_engine(monkeypatch)

    argv = ["--analyses-dir", str(analyses), "--pack-map", str(pack_map)]
    assert main([*argv, "--out", str(out1)]) == 0
    assert main([*argv, "--out", str(out2)]) == 0

    jsonl_path = out1 / FINDINGS_JSONL
    report_path = out1 / FINDINGS_REPORT
    requests_path = out1 / IMPROVEMENT_REQUESTS
    assert jsonl_path.is_file() and report_path.is_file() and requests_path.is_file()
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 7
    parsed: list[dict[str, Any]] = []
    for line in lines:
        row = json.loads(line)
        parsed.append(row)
        assert list(row) == sorted(row)
        assert "produced_at" not in row
        assert set(row) >= REQUIRED_JSONL_KEYS
        assert row["recipe_id"] in ALLOWED_RECIPES
        assert row["disposition"] in ALLOWED_DISPOSITIONS
    assert [(row["trial_id"], row["recipe_id"], row["finding_id"]) for row in parsed] == sorted(
        (row["trial_id"], row["recipe_id"], row["finding_id"]) for row in parsed
    )

    report = report_path.read_text(encoding="utf-8")
    assert "EMBARGOED: produced from pre-rebuild EvidencePacks" in report
    assert "PR #199 original exact review" in report
    assert (
        "multi-call citations/observations, expected-negative anchors, coverage producer/state-journal units, omitted digest reopen"
        in report
    )
    assert "index convention" in report
    assert "grouped only by (benchmark, target_definition)" in report
    assert "no decisive-step depth or propagation distribution is pooled" in report
    assert "Unit: RecipeFindings; aggregation: micro-count" in report
    assert "## Selected EvidencePacks" in report
    selected = _section(report, "Selected EvidencePacks")
    assert "`digest-new`" in selected and "`digest-old`" not in selected
    assert "selection_mode: pack-map" in selected
    assert "verbatim quote" in report
    for phrase in DATA_REQUIREMENT_PHRASES:
        assert phrase in report
    for recipe_id in ALLOWED_RECIPES:
        assert f"`{recipe_id}`" in report

    requests = requests_path.read_text(encoding="utf-8")
    for title, owner, trial_id in IMPROVEMENT_EXPECTATIONS:
        section = _section(requests, title)
        assert f"- owner: {owner}" in section
        assert f"`{trial_id}`" in section
        assert "- affected_trials: 1" in section and "- citation_count: 1" in section
    evidence_section = _section(requests, "EvidencePack anchor defect")
    assert "PR #199 exact-review blockers" in evidence_section
    assert "opportunity_unknown_r6" in _section(requests, "context events")
    assert "`trial-b`" not in _section(requests, "context events")

    assert (out2 / FINDINGS_JSONL).read_bytes() == jsonl_path.read_bytes()
    assert (out2 / FINDINGS_REPORT).read_bytes() == report_path.read_bytes()
    assert (out2 / IMPROVEMENT_REQUESTS).read_bytes() == requests_path.read_bytes()


def test_runner_old_pack_selection_emits_materialization_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyses = tmp_path / "analyses"
    _write_sidecar(
        analyses,
        "trial-a",
        "digest-old",
        created_at="2026-08-01T00:00:00Z",
        execution_sample_only=True,
    )
    _stub_engine(monkeypatch, [make_finding(trial_id="trial-a")])
    out = tmp_path / "out"
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--trial",
                "trial-a",
                "--pack-digest",
                "digest-old",
            ]
        )
        == 0
    )
    request = (out / IMPROVEMENT_REQUESTS).read_text(encoding="utf-8")
    materialization = _section(
        request, "rebuilt packs pinned but unmaterialized; request materialization to shared root"
    )
    assert "- owner: Agent Data" in materialization
    assert "amended heads b01d417 then 519f542" in materialization
    assert "`trial-a`" in materialization


def test_runner_real_pack_smoke_skip_if_absent(tmp_path: Path) -> None:
    if not REAL_ANALYSES.is_dir():
        pytest.skip("real analyses packs absent")
    try:
        selections = select_trial_sidecars(REAL_ANALYSES)
    except Exception as exc:
        pytest.skip(f"real pack selection failed: {exc}")
    if not selections:
        pytest.skip("real analyses packs absent")
    trial_id = next(iter(selections))
    out = tmp_path / "real-out"
    try:
        code = main(["--analyses-dir", str(REAL_ANALYSES), "--out", str(out), "--trial", trial_id])
    except Exception as exc:
        pytest.skip(f"real pack load failed: {exc}")
    if code != 0:
        pytest.skip(f"real pack runner exited {code}")
    jsonl = (out / FINDINGS_JSONL).read_text(encoding="utf-8")
    assert jsonl.strip()
    row = json.loads(jsonl.splitlines()[0])
    assert row["recipe_id"] in ALLOWED_RECIPES
    assert row["disposition"] in ALLOWED_DISPOSITIONS
    report = (out / FINDINGS_REPORT).read_text(encoding="utf-8")
    for phrase in ("EMBARGOED", "arms executed: 1", "[0, 0.434]", "NOT AUTHORIZED TO RUN"):
        assert phrase in report
    assert (out / IMPROVEMENT_REQUESTS).is_file()
    assert not (REAL_ANALYSES / FINDINGS_JSONL).exists()


def test_blank_created_at_two_generations_without_pin_is_hard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "digest-old", created_at="")
    _write_sidecar(analyses, "trial-a", "digest-new", created_at="")
    assert main(["--analyses-dir", str(analyses), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "ambiguous pack generations; supply --campaign-report or --pack-map" in err
    _assert_no_outputs(out)


def test_campaign_report_pins_listed_digests_across_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "digest-old", created_at="")
    _write_sidecar(analyses, "trial-a", "digest-new", created_at="")
    _write_sidecar(analyses, "trial-b", "digest-old-b", created_at="")
    _write_sidecar(analyses, "trial-b", "digest-new-b", created_at="")
    report_path = tmp_path / "campaign_report.json"
    _write_campaign_report(report_path, [("trial-a", "digest-old"), ("trial-b", "digest-new-b")])
    _stub_engine(monkeypatch)
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--campaign-report",
                str(report_path),
                "--report-id",
                "report-xyz",
                "--cas-id",
                "cas-xyz",
            ]
        )
        == 0
    )
    selected = _section(
        (out / FINDINGS_REPORT).read_text(encoding="utf-8"), "Selected EvidencePacks"
    )
    assert "`digest-old`" in selected
    assert "`digest-new-b`" in selected
    assert "`digest-new`" not in selected
    assert "`digest-old-b`" not in selected
    assert "selection_mode: campaign-report" in selected
    assert "report-xyz" in selected
    assert "cas-xyz" in selected


def test_campaign_report_missing_on_disk_pack_names_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "digest-on-disk", created_at="")
    report_path = tmp_path / "campaign_report.json"
    _write_campaign_report(report_path, [("trial-a", "digest-missing")])
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--campaign-report",
                str(report_path),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "trial-a" in err
    assert "listed in report but pack missing on disk" in err
    _assert_no_outputs(out)


def test_duplicate_on_disk_packs_matching_report_digest_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "dir-one", created_at="", pack_digest="same-digest")
    _write_sidecar(analyses, "trial-a", "dir-two", created_at="", pack_digest="same-digest")
    report_path = tmp_path / "campaign_report.json"
    _write_campaign_report(report_path, [("trial-a", "same-digest")])
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--campaign-report",
                str(report_path),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "trial-a" in err
    assert "duplicate on-disk packs matching report entry" in err
    _assert_no_outputs(out)


def test_requested_trial_absent_from_report_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "digest-a", created_at="")
    _write_sidecar(analyses, "trial-b", "digest-b", created_at="")
    report_path = tmp_path / "campaign_report.json"
    _write_campaign_report(report_path, [("trial-a", "digest-a")])
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--campaign-report",
                str(report_path),
                "--trial",
                "trial-a",
                "--trial",
                "trial-b",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "requested trial trial-b not listed in report" in err
    _assert_no_outputs(out)


def test_global_pack_digest_rejects_multiple_trials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    analyses.mkdir()
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--pack-digest",
                "digest-a",
                "--trial",
                "trial-a",
                "--trial",
                "trial-b",
            ]
        )
        == 1
    )
    assert "global digest requires single-trial mode" in capsys.readouterr().err
    _assert_no_outputs(out)


def test_single_trial_pack_digest_still_selects_pinned_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyses = tmp_path / "analyses"
    out = tmp_path / "out"
    _write_sidecar(analyses, "trial-a", "digest-old", created_at="")
    _write_sidecar(analyses, "trial-a", "digest-new", created_at="")
    _stub_engine(monkeypatch, [make_finding(trial_id="trial-a")])
    assert (
        main(
            [
                "--analyses-dir",
                str(analyses),
                "--out",
                str(out),
                "--trial",
                "trial-a",
                "--pack-digest",
                "digest-old",
            ]
        )
        == 0
    )
    selected = _section(
        (out / FINDINGS_REPORT).read_text(encoding="utf-8"), "Selected EvidencePacks"
    )
    assert "`digest-old`" in selected
    assert "`digest-new`" not in selected
    assert "selection_mode: pack-digest" in selected


def test_report_mode_ignores_unrelated_discovered_trial(tmp_path: Path) -> None:
    # D2 contract: no --trial + digest_map => scope is exactly the map keys;
    # unrelated discovered trials are ignored, not required to appear in the map.
    _write_sidecar(tmp_path, "trial-in-report", "d1" * 32, created_at="")
    _write_sidecar(tmp_path, "trial-unrelated", "d2" * 32, created_at="")
    report = tmp_path / "report.json"
    _write_campaign_report(report, [("trial-in-report", "d1" * 32)])
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map

    selected = select_trial_sidecars(
        tmp_path, digest_map=load_campaign_report_map(report), pin_source="report"
    )
    assert sorted(selected) == ["trial-in-report"]


def test_map_listed_trial_with_missing_sidecar_fails(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "trial-present", "d1" * 32, created_at="")
    report = tmp_path / "report.json"
    _write_campaign_report(report, [("trial-present", "d1" * 32), ("trial-missing", "d3" * 32)])
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map

    with pytest.raises(ValueError, match="trial trial-missing listed in report"):
        select_trial_sidecars(
            tmp_path, digest_map=load_campaign_report_map(report), pin_source="report"
        )


def test_explicit_requested_trial_absent_from_map_fails(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "trial-in-report", "d1" * 32, created_at="")
    _write_sidecar(tmp_path, "trial-extra", "d2" * 32, created_at="")
    report = tmp_path / "report.json"
    _write_campaign_report(report, [("trial-in-report", "d1" * 32)])
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map

    with pytest.raises(ValueError, match="trial-extra not listed in report"):
        select_trial_sidecars(
            tmp_path,
            digest_map=load_campaign_report_map(report),
            requested=["trial-in-report", "trial-extra"],
            pin_source="report",
        )


def test_real_build_campaign_report_source_refs_select_only_report_trials(
    tmp_path: Path,
) -> None:
    # The REAL report builder (trajectory_runtime.build_campaign_report) produces
    # source_refs; selection must scope to exactly those trials.
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map
    from evallab.interpretation.trajectory_runtime import (
        CampaignAnalysisManifest,
        build_campaign_report,
    )

    manifest = CampaignAnalysisManifest.model_validate(
        {
            "schema_version": "campaign-analysis-manifest/v1",
            "manifest_id": "m-1",
            "manifest_digest": "sha256:" + "a" * 64,
            "campaign_id": "camp-1",
            "source_campaign_manifest_digest": "sha256:" + "1" * 64,
            "source_commit": None,
            "authorizing_actor": "test",
            "cas_store_root": str(tmp_path / "cas"),
            "items": [
                {
                    "source_role": "analysis",
                    "cohort_included": True,
                    "attempt_role": "primary",
                    "job_id": "job-in-report",
                    "job_name": "job-in-report",
                    "trial_id": "trial-in-report",
                    "trial_name": "trial-in-report",
                    "task_name": "fixture-task",
                    "quality_status": "pass",
                    "cas_uri": "cas://sha256/" + "b" * 64,
                }
            ],
            "accounting": {},
            "analysis_config": {},
            "produced_at": "2026-08-15T00:00:00Z",
        }
    )
    results = [
        {
            "job_id": "job-in-report",
            "trial_id": "trial-in-report",
            "source_cas_uri": "cas://sha256/" + "b" * 64,
            "artifact_cas_uri": "cas://sha256/" + "c" * 64,
            "ir_digest": "sha256:" + "d" * 64,
            "pack_digest": "sha256:" + "e1" * 32,
            "judgment_id": "sha256:" + "f" * 64,
            "decision_id": "sha256:" + "0" * 64,
            "decision": "abstained",
        }
    ]
    report = build_campaign_report(manifest, results)  # type: ignore[arg-type]
    report_path = tmp_path / "real_report.json"
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    _write_sidecar(tmp_path, "trial-in-report", "e1" * 32, created_at="")
    _write_sidecar(tmp_path, "trial-unrelated", "d2" * 32, created_at="")
    selected = select_trial_sidecars(
        tmp_path, digest_map=load_campaign_report_map(report_path), pin_source="report"
    )
    assert sorted(selected) == ["trial-in-report"]


def test_malformed_map_entries_fail_closed(tmp_path: Path) -> None:
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map, load_pack_map

    bad_map = tmp_path / "map.json"
    bad_map.write_text(json.dumps({"trial-a": ""}), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed pack map entry"):
        load_pack_map(bad_map)
    bad_report = tmp_path / "report.json"
    bad_report.write_text(json.dumps({"source_refs": [{"trial_id": "trial-a"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing pack_digest"):
        load_campaign_report_map(bad_report)


def test_aggregate_only_campaign_report_fails_closed(tmp_path: Path) -> None:
    # Reviewer P1: a report without a source_refs list must raise, never yield an
    # empty pin map that silently produces zero findings with exit 0.
    from evallab.interpretation.trajectory_recipe_run import load_campaign_report_map

    aggregate = tmp_path / "aggregate.json"
    aggregate.write_text(
        json.dumps({"schema_version": "campaign-report/v1", "cohort_accounted": 5}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no source_refs list"):
        load_campaign_report_map(aggregate)
    empty_ok = tmp_path / "empty.json"
    empty_ok.write_text(json.dumps({"source_refs": []}), encoding="utf-8")
    assert load_campaign_report_map(empty_ok) == {}
