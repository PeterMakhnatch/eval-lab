from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights import experiment as experiment_module
from evallab.insights.check import unsupported_numbers
from evallab.insights.commands import experiment as experiment_command
from evallab.insights.config import EngineConfig
from evallab.insights.engine import rederive_sample
from evallab.insights.experiment import ReportClaim, analyze_experiment, experiment_output_dir
from evallab.insights.models import (
    ChallengeSpec,
    CheckResult,
    ClaimRecord,
    EvidenceRef,
    ExtractedClaim,
    InsightCard,
    InsightRecord,
    Lens,
    PassRecord,
    Recipe,
    RederivationRecord,
    RederivedClaim,
    RefereeVerdict,
    SourceRecord,
    SourceSection,
)
from evallab.insights.packs import PassInputs
from evallab.insights.store import InsightStore

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


class FakeEngine:
    def __init__(self, state: Path, *, config: EngineConfig) -> None:
        self.store = InsightStore.open(state)
        self.config = config
        self.calls: list[tuple[Lens, PassInputs | None]] = []
        self.extractor_remaining = 1
        self.receipt_file = "summary.json"

    def scan(self) -> object:
        path = next(Path(item.path) for item in self.config.overrides
                    if Path(item.path).name == "report.md")
        self.store.upsert_source(SourceRecord(
            source_id="F-report", path=str(path), family="experiment", kind="report",
            sha256="source-digest", size=path.stat().st_size, mtime=NOW, folder=str(path.parent),
        ))
        self.store.replace_sections("F-report", [SourceSection(
            source_id="F-report", section_id="X-report", heading="Results", ordinal=0,
            start_line=1, end_line=2, sha256="section-digest",
        )])
        return None

    def run_lens(
        self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
    ) -> PassRecord | None:
        assert wait and trigger == "experiment"
        self.calls.append((lens, inputs))
        if lens == "extractor":
            if self.extractor_remaining == 0:
                return None
            self.extractor_remaining -= 1
            self.store.insert_claims([ClaimRecord(
                claim_id=f"C-case-{index}", source_id="F-report", section_sha256="section-digest",
                fingerprint=f"fp-{index}", pass_id="P-extract", extracted_by="test", created_at=NOW,
                check=CheckResult(status="pass"), claim=ExtractedClaim(
                    local_id=f"case-{index}", experiment="synthetic", kind="measurement", value=5,
                    statement="The detector caught 5 cheating cases.",
                    quote="The detector caught 5 cheating cases.", section_id="X-report",
                    recipe=Recipe(op="value", file=self.receipt_file, pointer="/caught"),
                ),
            ) for index in range(10)])
        elif lens == "rederiver":
            assert inputs is not None
            for claim_id in inputs.claim_ids:
                self.store.save_rederivation(RederivationRecord(
                    claim_id=claim_id, pass_id="P-rederive", model_served="independent-family",
                    rederived=RederivedClaim(claim_id=claim_id, value=5), agrees=True, created_at=NOW,
                ))
        return PassRecord(
            pass_id=f"P-{lens}", lens=lens, route="fake", trigger=trigger, status="succeeded",
            pass_dir="/unused", created_at=NOW, started_at=NOW, finished_at=NOW,
        )


def _input(folder: Path, caught: int = 5) -> None:
    folder.mkdir()
    (folder / "report.md").write_text(
        "# Results\nThe detector caught 5 cheating cases.\n", encoding="utf-8",
    )
    (folder / "summary.json").write_text(json.dumps({"caught": caught, "n": 6}), encoding="utf-8")


def test_experiment_reports_supported_numbers_and_cites_claims(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    engines: list[FakeEngine] = []

    def factory(state_root: Path, *, config: EngineConfig) -> FakeEngine:
        engine = FakeEngine(state_root, config=config)
        engines.append(engine)
        return engine

    output = tmp_path / "output"
    paths = analyze_experiment(folder, output, engine_factory=factory, now=NOW)
    assert {path.name for path in paths} == {
        "claims.jsonl", "validity.json", "analysis.json", "REPORT.md",
    }
    report = (output / "REPORT.md").read_text("utf-8")
    claims = [ReportClaim.model_validate_json(line)
              for line in (output / "claims.jsonl").read_text("utf-8").splitlines()]
    numbers = [value for claim in claims for value in (
        claim.text, claim.claim_id, claim.stated_number, claim.recomputed_number,
    ) if value is not None]
    numbers.append((output / "validity.json").read_text("utf-8"))
    assert unsupported_numbers(report, numbers) == []
    for line in report.splitlines():
        if line and not line.startswith("#"):
            assert re.search(r"\[(?:C-[a-z0-9-]+|V-[0-9]+)\]\.$", line)
    assert len(report.split()) < 500
    assert report.startswith("# Experiment review: experiment\n\n")
    assert "experiment: claims extracted 10; report files read 2" in report
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#"):
            assert index == 0 or lines[index - 1] == ""
            assert lines[index + 1] == ""
    assert "stated 5, receipts give 5" in report
    assert len(claims) == 10
    assert all(claim.file.endswith("report.md") and claim.line == 2 for claim in claims)
    assert not any(claim.claim_id.startswith("C-audit-") for claim in claims)
    expected = rederive_sample(f"C-case-{index}" for index in range(10))
    assert engines[0].calls[2][1] == PassInputs(lens="rederiver", claim_ids=tuple(expected))
    assert engines[0].calls[-1][1] == PassInputs(
        lens="skeptic", claim_ids=tuple(reversed([f"C-case-{index}" for index in range(10)])),
    )
    sampled = [claim for claim in claims if claim.rederiver_sampled]
    assert {claim.claim_id for claim in sampled} == set(expected)
    assert all(claim.rederiver_agreement is True for claim in sampled)
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert analysis["source_digests"].keys() == {"report.md", "summary.json"}
    assert analysis["confidence"] != "high"
    assert (output / ".state" / "insights.sqlite").exists()


def test_no_agents_uses_existing_ledger_without_factory(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    claim = ReportClaim(
        claim_id="C-existing", text="The report records 5 catches.", file="report.md", line=2,
        stated_number=5, recomputed_number=4, status="differs", reason="Receipt differs.",
    )
    (folder / "claims.jsonl").write_text(claim.model_dump_json() + "\n", encoding="utf-8")

    def forbidden(state_root: Path, *, config: EngineConfig) -> FakeEngine:
        raise AssertionError("No agents means no engine construction")

    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=forbidden, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert "stated 5, receipts give 4 [C-existing]" in report
    assert not (output / ".state").exists()
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert analysis["agents_run"] is False
    assert analysis["limitations"]


def test_sampling_is_order_independent_and_nonempty() -> None:
    identities = [f"C-synthetic-{index}" for index in range(100)]
    assert rederive_sample(identities) == rederive_sample(reversed(identities))
    assert rederive_sample(["C-only"]) == ["C-only"]
    assert rederive_sample([]) == []
    assert 0 < len(rederive_sample(identities)) < len(identities)


def test_default_output_is_stable_and_outside_source(tmp_path: Path) -> None:
    folder = tmp_path / "Some Experiment"
    folder.mkdir()
    output = experiment_output_dir(folder, tmp_path / "state")
    assert output == experiment_output_dir(folder, tmp_path / "state")
    assert output.parent == tmp_path / "state" / "experiments"
    assert re.fullmatch(r"some-experiment-[0-9a-f]{8}", output.name)
    assert not output.is_relative_to(folder)


def test_no_agents_without_claims_writes_only_validity_citations(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    (folder / "cases.json").write_text(
        '[{"id":"a","caught":true},{"id":"b","caught":false}]', encoding="utf-8",
    )
    (folder / "attribution.json").write_text(json.dumps({
        "cases_file": "cases.json", "rows": "", "case_id_field": "id",
        "layers": {"Review": {"field": "caught", "truthy": [True]}},
    }), encoding="utf-8")
    output = folder / "output"
    paths = analyze_experiment(folder, output, now=NOW, run_agents=False)
    assert "attribution.json" in {path.name for path in paths}
    assert (output / "claims.jsonl").read_text("utf-8") == ""
    report = (output / "REPORT.md").read_text("utf-8")
    assert "[V-" in report and "[C-" not in report
    assert report.count("No analyst agents ran") == 1
    first = json.loads((output / "validity.json").read_text("utf-8"))["files_checked"]
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    second = json.loads((output / "validity.json").read_text("utf-8"))["files_checked"]
    assert second == first


def test_related_bundle_is_bounded_recomputable_and_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary, related = tmp_path / "primary", tmp_path / "related"
    _input(primary)
    _input(related)
    (related / "linked.json").symlink_to(related / "summary.json")
    (related / "linked-dir").symlink_to(primary, target_is_directory=True)
    (related / "large.json").write_bytes(b"x" * 1025)
    for name in (".git", ".venv", "node_modules", "__pycache__"):
        (related / name).mkdir()
        (related / name / "skip.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(experiment_module, "MAX_BYTES", 1024)
    configs: list[EngineConfig] = []

    def factory(state_root: Path, *, config: EngineConfig) -> FakeEngine:
        configs.append(config)
        engine = FakeEngine(state_root, config=config)
        engine.receipt_file = "related/summary.json"
        return engine

    output = primary / "output"
    analyze_experiment(primary, output, related=[related], engine_factory=factory, now=NOW)
    first = (output / "claims.jsonl").read_bytes()
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    bundle = output / ".bundle"
    assert not (bundle / "related" / "linked.json").exists()
    assert not (bundle / "related" / "linked-dir").exists()
    assert not (bundle / "related" / "large.json").exists()
    assert not (bundle / "primary" / "output").exists()
    for name in (".git", ".venv", "node_modules", "__pycache__"):
        assert not (bundle / "related" / name).exists()
    assert {item["status"] for item in analysis["evidence"]} == {"reproduced"}
    assert analysis["related_folders"] == [str(related)]
    assert "related/summary.json" in analysis["source_digests"]
    assert {Path(item.path).parent.name for item in configs[0].overrides} == {
        "primary", "related",
    }
    assert (bundle / "related" / "summary.json").stat().st_mtime_ns == (
        related / "summary.json"
    ).stat().st_mtime_ns
    analyze_experiment(primary, output, related=[related], engine_factory=factory, now=NOW)
    second = json.loads((output / "analysis.json").read_text("utf-8"))
    assert (output / "claims.jsonl").read_bytes() == first
    assert second["source_digests"] == analysis["source_digests"]
    assert experiment_output_dir(primary) != experiment_output_dir(primary, related=[related])


def test_bundle_collision_names_and_related_order_are_stable(tmp_path: Path) -> None:
    primary = tmp_path / "a" / "same"
    related = tmp_path / "b" / "same"
    other = tmp_path / "c" / "same"
    for folder in (primary, related, other):
        folder.parent.mkdir()
        _input(folder)
    output = tmp_path / "output"
    analyze_experiment(primary, output, related=[other, related], now=NOW, run_agents=False)
    assert sorted(path.name for path in (output / ".bundle").iterdir()) == [
        "same", "same-2", "same-3",
    ]
    assert experiment_output_dir(primary, related=[other, related]) == experiment_output_dir(
        primary, related=[related, other],
    )


@pytest.mark.parametrize(("passes", "expected_calls", "capped"), [(3, 4, False), (25, 20, True)])
def test_extractor_drains_pending_sections_and_caps(
    tmp_path: Path, passes: int, expected_calls: int, capped: bool,
) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    engines: list[FakeEngine] = []

    def factory(state_root: Path, *, config: EngineConfig) -> FakeEngine:
        engine = FakeEngine(state_root, config=config)
        engine.extractor_remaining = passes
        engines.append(engine)
        return engine

    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=factory, now=NOW)
    assert sum(lens == "extractor" for lens, _ in engines[0].calls) == expected_calls
    limitations = json.loads((output / "analysis.json").read_text("utf-8"))["limitations"]
    assert any("cap of 20" in item for item in limitations) is capped


def test_extractor_stops_after_consecutive_failures(tmp_path: Path) -> None:
    class FailedEngine(FakeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            result = super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)
            return result.model_copy(update={"status": "failed"}) if (
                result is not None and lens == "extractor"
            ) else result

    folder = tmp_path / "experiment"
    _input(folder)
    engines: list[FailedEngine] = []

    def factory(state_root: Path, *, config: EngineConfig) -> FailedEngine:
        engine = FailedEngine(state_root, config=config)
        engine.extractor_remaining = 20
        engines.append(engine)
        return engine

    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=factory, now=NOW)
    assert sum(lens == "extractor" for lens, _ in engines[0].calls) == 2
    assert "consecutive unsuccessful" in (output / "analysis.json").read_text("utf-8")


@pytest.mark.parametrize(("datasets", "sizes", "mixed"), [
    (["C-pool", "Sol-pool"], [6, 6], True),
    (["C-pool", "C-pool"], [6, 6], False),
    ([None, None], [6, 8], True),
    ([None, None], [6, 6], False),
])
def test_cohort_findings_keep_datasets_apart(
    tmp_path: Path, datasets: list[str | None], sizes: list[int], mixed: bool,
) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    claims = [
        ReportClaim(
            claim_id=f"C-cohort-{index}", text="The recall was measured on this pool.",
            file="report.md", status="reproduced", reason="Synthetic receipt.",
            experiment="synthetic", metric="recall", subject="Review",
            dataset=dataset, k=5, n=sizes[index],
            stated_number=5 / sizes[index], recomputed_number=5 / sizes[index],
        )
        for index, dataset in enumerate(datasets)
    ]
    (folder / "claims.jsonl").write_text(
        "".join(claim.model_dump_json() + "\n" for claim in claims), encoding="utf-8",
    )
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    findings = json.loads((output / "validity.json").read_text("utf-8"))["findings"]
    cohort = [finding for finding in findings if finding["code"] == "cohort_mix"]
    assert bool(cohort) is mixed
    if mixed:
        assert cohort[0]["pointers"] == ["C-cohort-0", "C-cohort-1"]
        assert "different groups of runs" in (output / "REPORT.md").read_text("utf-8")
    ledger = [json.loads(line) for line in (output / "claims.jsonl").read_text().splitlines()]
    assert ledger[0]["dataset"] == datasets[0]
    assert ledger[0]["metric"] == "recall" and ledger[0]["n"] == sizes[0]


def test_external_attribution_reports_stage_rule_and_union(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    cases = [
        {"id": str(index), "review": index < 5, "blocked": index < 4}
        for index in range(6)
    ]
    (folder / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    spec = tmp_path / "operator.json"
    spec.write_text(json.dumps({
        "cases_file": "cases.json", "case_id_field": "id",
        "layers": {
            "Review": {"field": "review", "role": "detection"},
            "Container": {"field": "blocked", "role": "prevention"},
        },
    }), encoding="utf-8")
    output = tmp_path / "output"
    analyze_experiment(folder, output, attribution_spec=spec, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert "Stage-4 uses exact paired McNemar, p < 0.05: with 6 cases" in report
    assert "needs at least 6 cases only it catches" in report
    assert "Review alone equals the union: 5/6" in report
    assert "no sample size can show value at a unique rate of 0" in report
    assert "80% chance" in report
    assert "Wilson interval 43.6%–97.0%" in report
    assert "exact paired p=1;" in report
    assert len(report.splitlines()) <= 60


class ChallengeEngine(FakeEngine):
    def run_lens(
        self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
    ) -> PassRecord | None:
        result = super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)
        if lens == "skeptic":
            self.store.insert_insights([InsightRecord(
                insight_id="I-challenge", lens="skeptic", pass_id="P-skeptic", created_at=NOW,
                status="checked", check=CheckResult(status="pass"), card=InsightCard(
                    local_id="challenge", kind="challenge", title="Needs 987654 new cases",
                    statement="These measurements may not use independent label sources.",
                    evidence=[EvidenceRef(claim_id="C-case-0")],
                    observations=["The measurement uses shared labels."],
                    interpretation="Shared labels can conceal measurement error.",
                    why_not_obvious="Receipt agreement is not label independence.",
                    alternatives=["Labels could be independently audited."],
                    confidence="medium", challenge=ChallengeSpec(
                        target_claim_ids=["C-case-0"], flaw="label_validity", severity="major",
                        check="Audit label provenance separately.",
                        decision_at_risk="Selecting the detector.",
                    ),
                ),
            )])
        elif lens == "referee":
            assert inputs == PassInputs(lens="referee", insight_ids=("I-challenge",))
            record = self.store.list_insights()[0]
            record.referee = RefereeVerdict(
                insight_id=record.insight_id, supported="yes", novel=True, falsifiable=True,
                actionable=True, importance=4, non_obviousness=3, verdict="accept",
                notes="Provenance audit is warranted.",
            )
            record.referee_model = "independent-referee"
            record.status = "refereed"
            self.store.save_insight(record)
        return result


def test_skeptic_referee_inputs_and_unsupported_title_omission(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=ChallengeEngine, now=NOW)
    report = (output / "REPORT.md").read_text("utf-8")
    assert "987654" not in report
    assert "label validity; severity major; referee: accept [I-challenge, C-case-0]" in report
    insights = json.loads((output / "analysis.json").read_text("utf-8"))["insights"]
    assert insights[0]["referee_verdict"] == "accept"
    assert insights[0]["referee_model"] == "independent-referee"
    assert insights[0]["target_claim_ids"] == ["C-case-0"]
    assert insights[0]["check_status"] == "pass"


@pytest.mark.parametrize("failed_lens", ["skeptic", "referee"])
def test_analyst_pass_failures_become_limitations(tmp_path: Path, failed_lens: str) -> None:
    class FailedEngine(ChallengeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            if lens == failed_lens:
                raise RuntimeError("synthetic failure")
            return super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=FailedEngine, now=NOW)
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert any(f"The {failed_lens} pass failed" in item for item in analysis["limitations"])
    if failed_lens == "referee":
        assert "not been refereed [I-challenge]" in (output / "REPORT.md").read_text("utf-8")


def test_failed_skeptic_checks_are_not_sent_to_referee(tmp_path: Path) -> None:
    class FailedCheckEngine(ChallengeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            assert lens != "referee"
            result = super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)
            if lens == "skeptic":
                record = self.store.list_insights()[0]
                record.check = CheckResult(status="fail", notes=["Synthetic check failure."])
                record.status = "check_failed"
                self.store.save_insight(record)
            return result

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=FailedCheckEngine, now=NOW)
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert analysis["insights"][0]["check_status"] == "fail"
    assert analysis["insights"][0]["referee_verdict"] is None
    assert not any("referee" in item for item in analysis["limitations"])
    assert "I-challenge" not in (output / "REPORT.md").read_text("utf-8")


def test_experiment_cli_accepts_related_and_attribution(tmp_path: Path) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    experiment_command.register(parser.add_subparsers())
    args = parser.parse_args([
        "experiment", str(tmp_path), "--with", str(tmp_path / "one"),
        "--with", str(tmp_path / "two"), "--attribution", str(tmp_path / "spec.json"),
        "--out", str(tmp_path / "output"), "--no-agents",
    ])
    assert args.related == [tmp_path / "one", tmp_path / "two"]
    assert args.attribution == tmp_path / "spec.json"
    assert args.no_agents


def test_bundle_report_names_members_and_counts_existing_claims(tmp_path: Path) -> None:
    primary, related = tmp_path / "Primary 17", tmp_path / "Related 23"
    _input(primary)
    _input(related)
    for count, folder in enumerate((primary, related), 1):
        claims = [
            ReportClaim(
                claim_id=f"C-{count}-{index}", text="A qualitative observation was recorded.",
                file="report.md", status="not_recomputable", reason="No numeric recipe.",
            )
            for index in range(count)
        ]
        (folder / "claims.jsonl").write_text(
            "".join(claim.model_dump_json() + "\n" for claim in claims), encoding="utf-8",
        )
    output = primary / "output"
    analyze_experiment(primary, output, related=[related], now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert report.startswith("# Experiment review: Primary 17\n\nRelated folders: Related 23")
    assert "Primary 17: claims extracted (existing ledger) 1; report files read 2" in report
    assert "Related 23: claims extracted (existing ledger) 2; report files read 2" in report
    assert report.count("No analyst agents ran") == 1
    validity = json.loads((output / "validity.json").read_text("utf-8"))
    summary = next(item for item in validity["findings"] if item["code"] == "claim_summary")
    assert report.count(f"[{summary['finding_id']}].") >= 3
    warning_codes: set[str] = set()
    for finding in validity["findings"]:
        if finding["severity"] in {"warning", "error"}:
            assert finding["code"] not in warning_codes
            warning_codes.add(finding["code"])
            assert f"(count: {finding['count']}) [{finding['finding_id']}]." in report
    first_claims = (output / "claims.jsonl").read_bytes()
    analyze_experiment(primary, output, related=[related], now=NOW, run_agents=False)
    assert (output / "REPORT.md").read_text("utf-8") == report
    assert (output / "claims.jsonl").read_bytes() == first_claims


def test_report_formats_p_values_without_long_floats(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    (folder / "cases.json").write_text(json.dumps([
        {"id": str(index), "caught": index < 6} for index in range(10)
    ]), encoding="utf-8")
    (folder / "attribution.json").write_text(json.dumps({
        "cases_file": "cases.json", "case_id_field": "id",
        "layers": {"Review": {"field": "caught"}},
    }), encoding="utf-8")
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert "exact paired p=0.0312;" in report
    assert "p=0.03125" not in report
    assert re.search(r"Wilson interval \d+\.\d%–\d+\.\d%", report)


def test_multiple_cohort_groups_emit_one_counted_warning(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    claims = [
        ReportClaim(
            claim_id=f"C-{metric}-{dataset}", text="The metric was measured on this pool.",
            file="report.md", status="reproduced", reason="Synthetic receipt.",
            metric=metric, subject="Review", dataset=dataset, k=5, n=6,
            stated_number=5 / 6, recomputed_number=5 / 6,
        )
        for metric in ("recall", "accuracy") for dataset in ("first", "second")
    ]
    (folder / "claims.jsonl").write_text(
        "".join(claim.model_dump_json() + "\n" for claim in claims), encoding="utf-8",
    )
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    findings = json.loads((output / "validity.json").read_text("utf-8"))["findings"]
    cohort = [finding for finding in findings if finding["code"] == "cohort_mix"]
    assert len(cohort) == 1 and cohort[0]["count"] == 4
    assert set(cohort[0]["pointers"]) == {claim.claim_id for claim in claims}
    report = (output / "REPORT.md").read_text("utf-8")
    assert report.count("different groups of runs") == 1
    assert "stated 0.833, receipts give 0.833" in report
    assert "0.8333333333333334" not in report
    assert f"(count: 4) [{cohort[0]['finding_id']}]." in report
