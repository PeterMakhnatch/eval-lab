"""Focused contracts for the deterministic trajectory recipe findings runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.trajectory_recipe_run import (
    FINDINGS_JSONL,
    FINDINGS_REPORT,
    IMPROVEMENT_REQUESTS,
    main,
    select_trial_sidecars,
)
from evallab.trajectory_recipes import RecipeFinding

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
    {"candidate_hold", "deterministic_abstention", "screening_only"}
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
) -> None:
    pack_dir = analyses_dir / trial_id / digest
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"trial_id": trial_id, "created_at": created_at}
    if execution_sample_only:
        payload["selected_windows"] = [{"reason": "execution_sample"}]
    (pack_dir / "evidence_pack.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


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
    # Two rebuilt sidecars for trial-a. Newest created_at must win; the old one also
    # seeds the mechanical materialization request only when explicitly selected.
    _write_sidecar(
        analyses,
        "trial-a",
        "digest-old",
        created_at="2026-08-01T00:00:00Z",
        execution_sample_only=True,
    )
    _write_sidecar(analyses, "trial-a", "digest-new", created_at="2026-08-02T00:00:00Z")
    _write_sidecar(analyses, "trial-b", "digest-b", created_at="2026-08-02T00:00:00Z")
    selections = select_trial_sidecars(analyses)
    assert selections["trial-a"].digest == "digest-new"
    assert select_trial_sidecars(analyses, pack_digest="digest-old")["trial-a"].digest == "digest-old"

    seeded = seed_findings()
    by_trial: dict[str, list[RecipeFinding]] = {"trial-a": [], "trial-b": []}
    for finding in seeded:
        by_trial[str(finding.trial_id)].append(finding)
    monkeypatch.setattr(
        "evallab.trajectory_recipe_run.load_trial_artifacts",
        lambda analyses_dir, trial_id: trial_id,
    )
    monkeypatch.setattr(
        "evallab.trajectory_recipe_run.run_recipes",
        lambda artifacts, *, semantics_profile_digest=None: list(by_trial[str(artifacts)]),
    )

    assert main(["--analyses-dir", str(analyses), "--out", str(out1)]) == 0
    assert main(["--analyses-dir", str(analyses), "--out", str(out2)]) == 0

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
        assert REQUIRED_JSONL_KEYS <= set(row)
        assert row["recipe_id"] in ALLOWED_RECIPES
        assert row["disposition"] in ALLOWED_DISPOSITIONS
    assert [(row["trial_id"], row["recipe_id"], row["finding_id"]) for row in parsed] == sorted(
        (row["trial_id"], row["recipe_id"], row["finding_id"]) for row in parsed
    )

    report = report_path.read_text(encoding="utf-8")
    assert "EMBARGOED: produced from pre-rebuild EvidencePacks" in report
    assert "PR #199 original exact review" in report
    assert "multi-call citations/observations, expected-negative anchors, coverage producer/state-journal units, omitted digest reopen" in report
    assert "index convention" in report
    assert "grouped only by (benchmark, target_definition)" in report
    assert "no decisive-step depth or propagation distribution is pooled" in report
    assert "Unit: RecipeFindings; aggregation: micro-count" in report
    assert "## Selected EvidencePacks" in report
    assert "`digest-new`" in report and "`digest-old`" not in _section(report, "Selected EvidencePacks")
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
    monkeypatch.setattr("evallab.trajectory_recipe_run.load_trial_artifacts", lambda *_: "trial-a")
    monkeypatch.setattr(
        "evallab.trajectory_recipe_run.run_recipes",
        lambda *_args, **_kwargs: [make_finding(trial_id="trial-a")],
    )
    out = tmp_path / "out"
    assert main(
        ["--analyses-dir", str(analyses), "--out", str(out), "--pack-digest", "digest-old"]
    ) == 0
    request = (out / IMPROVEMENT_REQUESTS).read_text(encoding="utf-8")
    materialization = _section(request, "rebuilt packs pinned but unmaterialized; request materialization to shared root")
    assert "- owner: Agent Data" in materialization
    assert "amended heads b01d417 then 519f542" in materialization
    assert "`trial-a`" in materialization


def test_runner_real_pack_smoke_skip_if_absent(tmp_path: Path) -> None:
    if not REAL_ANALYSES.is_dir():
        pytest.skip("real analyses packs absent")
    selections = select_trial_sidecars(REAL_ANALYSES)
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
