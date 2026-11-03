from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

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
from evallab.insights.runner import TRANSIENT_ERROR
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
    assert "stated 5, receipts give 4, off by 1 [C-existing]" in report
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


def _rate(
    claim_id: str, dataset: str | None, k: int, n: int, *, metric: str = "recall",
    status: Literal["reproduced", "differs", "not_recomputable"] = "reproduced",
    **changes: object,
) -> ReportClaim:
    fields: dict[str, object] = {
        "claim_id": claim_id, "text": f"The {metric} was {k} of {n}.", "file": "report.md",
        "status": status, "reason": "Synthetic receipt.", "experiment": "synthetic",
        "metric": metric, "dataset": dataset, "k": k, "n": n,
        "stated_number": k / n, "recomputed_number": k / n, **changes,
    }
    return ReportClaim.model_validate(fields)


def _cohort_findings(tmp_path: Path, claims: list[ReportClaim]) -> list[dict[str, object]]:
    """The cohort_mix findings of an existing ledger, re-checked without any agents."""
    folder = tmp_path / "experiment"
    _input(folder)
    (folder / "claims.jsonl").write_text(
        "".join(claim.model_dump_json() + "\n" for claim in claims), encoding="utf-8",
    )
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    findings = json.loads((output / "validity.json").read_text("utf-8"))["findings"]
    return [finding for finding in findings if finding["code"] == "cohort_mix"]


def test_a_pooled_rate_is_flagged_but_shared_words_and_one_cohort_splits_are_not(
    tmp_path: Path,
) -> None:
    claims = [
        _rate("C-alpha", "Corpus alpha", 3, 10, metric="drop recall", subject="Review"),
        _rate("C-beta", "Corpus beta", 4, 20, metric="drop recall", subject="Review"),
        _rate("C-pool", "Pooled corpora", 7, 30, metric="drop recall", subject="POOLED"),
        # The same word in the metric is no pooling: sizes that do not add up, no pooled claim.
        _rate("C-lab-gamma", "Corpus gamma", 5, 12, metric="cheat-labelled rollouts"),
        _rate("C-lab-delta", "Corpus delta", 6, 15, metric="cheat-labelled rollouts"),
        _rate("C-lab-epsilon", "Corpus epsilon", 9, 21, metric="cheat-labelled rollouts"),
        # Parts of ONE cohort add up to their whole without mixing anything.
        _rate("C-split-a", "Corpus zeta", 2, 5, metric="precision"),
        _rate("C-split-b", "Corpus zeta", 1, 5, metric="precision"),
        _rate("C-split-all", "Corpus zeta", 3, 10, metric="precision"),
    ]
    cohort = _cohort_findings(tmp_path, claims)
    assert len(cohort) == 1
    assert cohort[0]["pointers"] == ["C-alpha", "C-beta", "C-pool"]
    message = str(cohort[0]["message"])
    assert "C-pool (7/30) is the sum of C-alpha (3/10), C-beta (4/20)" in message
    assert "cheat-labelled" not in message


@pytest.mark.parametrize(("pool_experiment", "other_experiment", "mixed"), [
    ("w3", "w2", True),   # W2 and W3 are different experiments pooled into one 0/42
    ("w3", "w3", False),  # the same experiment and unnamed dataset: one cohort
])
def test_a_pool_named_in_its_own_statement_is_checked_against_the_named_rates(
    tmp_path: Path, pool_experiment: str, other_experiment: str, mixed: bool,
) -> None:
    pool = _rate("C-pool", None, 0, 42, metric="subversions combined", experiment=pool_experiment,
                 text="The author combines W2 (0/24) and W3 (0/18) into 0/42 subversions.")
    claims = [
        pool,
        _rate("C-w2", None, 0, 24, metric="w2 subversions", experiment=other_experiment),
        _rate("C-w3", None, 0, 18, metric="w3 subversions", experiment="w3"),
    ]
    cohort = _cohort_findings(tmp_path, claims)
    assert bool(cohort) is mixed
    if mixed:
        assert cohort[0]["pointers"] == ["C-pool", "C-w2", "C-w3"]


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


def test_report_formats_rates_without_long_floats(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    claim = _rate("C-five-sixths", "Pool", 5, 6, metric="recall")
    (folder / "claims.jsonl").write_text(claim.model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert "stated 0.833, receipts give 0.833" in report
    assert "0.8333333333333334" not in report


def test_report_shows_rounding_notes_and_the_size_of_every_difference(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    rounded = _rate("C-rounded", "Pool", 215, 254, metric="drop precision")
    rounded.stated_number, rounded.recomputed_number = 0.847, 215 / 254
    rounded.note = "rounding"
    rounded.difference = 0.000543
    rounded.difference_text = "0.000543 (0.0543 percentage points)"
    off = _rate("C-off", "Pool", 215, 254, metric="drop recall")
    off.status, off.stated_number, off.recomputed_number = "differs", 0.85, 215 / 254
    off.difference, off.difference_text = 0.003543, "0.00354 (0.354 percentage points)"
    legacy = _rate("C-legacy", "Pool", 1, 4, metric="precision")  # a ledger without gap fields
    legacy.status, legacy.stated_number, legacy.recomputed_number = "differs", 0.3, 0.25
    (folder / "claims.jsonl").write_text(
        "".join(claim.model_dump_json() + "\n" for claim in (rounded, off, legacy)),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    lines = (output / "REPORT.md").read_text("utf-8").splitlines()
    held = next(line for line in lines if "[C-rounded]" in line)
    assert "1 claim matches its receipts only within the printed rounding" in held
    assert "off by 0.000543 (0.0543 percentage points)" in held
    missed = next(line for line in lines if "[C-off]" in line)
    assert "stated 0.85, receipts give 0.846, off by 0.00354 (0.354 percentage points)" in missed
    assert "off by 0.05 [C-legacy]" in next(line for line in lines if "[C-legacy]" in line)
    summary = next(
        finding for finding in json.loads((output / "validity.json").read_text("utf-8"))["findings"]
        if finding["code"] == "claim_summary"
    )
    assert "1 reproduced only within printed rounding" in summary["message"]


def test_report_stays_short_when_the_ledger_is_large(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    claims = [_rate(f"C-held-{index}", f"Corpus {index}", 5, 20 + index, metric=f"metric {index}")
              for index in range(40)]
    for index in range(6):
        miss = _rate(f"C-miss-{index}", "Pool", 5, 10, metric=f"miss {index}")
        miss.status, miss.stated_number, miss.recomputed_number = "differs", 0.6, 0.5
        miss.difference, miss.difference_text = 0.1, "0.1 (10 percentage points)"
        claims.append(miss)
    claims += [
        ReportClaim(claim_id=f"C-open-{index}", text="A qualitative remark.", file="report.md",
                    status="not_recomputable", reason="No receipt recipe.")
        for index in range(12)
    ]
    (folder / "claims.jsonl").write_text(
        "".join(claim.model_dump_json() + "\n" for claim in claims), encoding="utf-8",
    )
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert len(report.splitlines()) < 45
    assert report.count("C-held-") == 3 and report.count("C-miss-") == 3
    assert "More reproduced claims are listed in claims.jsonl" in report
    assert ("12 claims have no successful receipt check, "
            "for example [C-open-0, C-open-1, C-open-2]") in report


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment_module, "_RETRY_DELAY_SECONDS", 0.0)


def test_stage_four_says_plainly_when_the_sample_is_below_the_minimum(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    (folder / "cases.json").write_text(json.dumps([
        {"id": str(index), "review": index < 3, "blocked": index < 2} for index in range(4)
    ]), encoding="utf-8")
    (folder / "attribution.json").write_text(json.dumps({
        "cases_file": "cases.json", "case_id_field": "id", "layers": {
            "Review": {"field": "review"},
            "Container": {"field": "blocked", "role": "prevention"},
        },
    }), encoding="utf-8")
    output = tmp_path / "output"
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    report = (output / "REPORT.md").read_text("utf-8")
    assert ("with 4 cases no layer-value claim can be shown at any rate, because p < 0.05 "
            "needs at least 6 cases only one layer catches") in report
    assert "a layer needs at least" not in report
    assert "Prevention layers together block 2 of 4 cases, detection layers together flag 3" in report


def test_a_transient_pass_failure_is_retried_once(tmp_path: Path) -> None:
    class FlakyEngine(ChallengeEngine):
        failures_left = 1

        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            if lens == "skeptic" and self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("SQLITE_BUSY")
            return super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)

    folder = tmp_path / "experiment"
    _input(folder)
    engines: list[FlakyEngine] = []

    def factory(state_root: Path, *, config: EngineConfig) -> FlakyEngine:
        engines.append(FlakyEngine(state_root, config=config))
        return engines[0]

    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=factory, now=NOW)
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert not any("skeptic" in item for item in analysis["limitations"])
    assert [lens for lens, _ in engines[0].calls].count("skeptic") == 1  # the retry reached it
    assert analysis["insights"][0]["referee_verdict"] == "accept"


@pytest.mark.parametrize(("message", "attempts"), [
    ("database is locked (SQLITE_BUSY)", 2), ("synthetic failure", 1),
])
def test_only_transient_startup_failures_are_retried_and_only_once(
    tmp_path: Path, message: str, attempts: int,
) -> None:
    seen: list[str] = []

    class AlwaysFails(ChallengeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            if lens == "skeptic":
                seen.append(lens)
                raise RuntimeError(message)
            return super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=AlwaysFails, now=NOW)
    assert len(seen) == attempts
    limitations = json.loads((output / "analysis.json").read_text("utf-8"))["limitations"]
    assert sum("The skeptic pass failed" in item for item in limitations) == 1


@pytest.mark.parametrize("lens", ["extractor", "rederiver", "skeptic", "referee"])
def test_every_lens_retries_a_pass_the_engine_marked_transient(
    tmp_path: Path, lens: str,
) -> None:
    """The engine records TRANSIENT_ERROR on a pass that died at startup; a long-running
    pass setup (a big referee pack) must not turn that into a lost lens."""
    runs: list[str] = []

    class BusyOnce(ChallengeEngine):
        busy = True

        def run_lens(
            self, lens_name: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            result = super().run_lens(lens_name, wait=wait, trigger=trigger, inputs=inputs)
            if lens_name == lens:
                runs.append(lens_name)
                if self.busy and result is not None:
                    self.busy = False
                    return result.model_copy(update={
                        "status": "failed", "error": TRANSIENT_ERROR,
                        "started_at": NOW, "finished_at": NOW + timedelta(minutes=3),
                    })
            return result

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=BusyOnce, now=NOW)
    analysis = json.loads((output / "analysis.json").read_text("utf-8"))
    assert not any("did not complete" in item for item in analysis["limitations"])
    assert runs.count(lens) >= 2
    if lens == "referee":
        assert analysis["insights"][0]["referee_verdict"] == "accept"


def test_a_second_transient_failure_is_reported_not_retried_again(tmp_path: Path) -> None:
    runs: list[str] = []

    class AlwaysBusy(ChallengeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            result = super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)
            if lens == "referee" and result is not None:
                runs.append(lens)
                return result.model_copy(update={"status": "failed", "error": TRANSIENT_ERROR})
            return result

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=AlwaysBusy, now=NOW)
    assert len(runs) == 2
    limitations = json.loads((output / "analysis.json").read_text("utf-8"))["limitations"]
    assert limitations.count("The referee pass did not complete successfully.") == 1


@pytest.mark.parametrize(("outlier_elsewhere", "second_home", "flagged"), [
    (False, True, True),    # a dataset this experiment never measures, beside its own rates
    (True, True, False),    # the other dataset is measured elsewhere: reported cohort by cohort
    (False, False, False),  # no dataset clearly carries the section
])
def test_cohort_mix_flags_a_headline_that_imports_another_cohorts_rate(
    tmp_path: Path, outlier_elsewhere: bool, second_home: bool, flagged: bool,
) -> None:
    home = [("C-home-a", 6, 20), ("C-home-b", 2, 20), ("C-home-c", 4, 10)]
    claims = [
        _rate(claim_id, "Impossible tasks", k, n, metric="reward rate", section_id="S-head",
              experiment="OV")
        for claim_id, k, n in home[: 3 if second_home else 1]
    ] + [
        _rate("C-away", "Earlier solvable benchmarks", 0, 42, metric="subversion rate",
              section_id="S-head", experiment="OV"),
    ]
    if not second_home:
        claims.append(_rate("C-away-too", "Other cohort", 1, 9, metric="rate",
                            section_id="S-head", experiment="OV"))
    if outlier_elsewhere:
        claims.append(_rate("C-away-detail", "Earlier solvable benchmarks", 0, 18,
                            metric="subversion rate", section_id="S-detail", experiment="OV"))
    cohort = _cohort_findings(tmp_path, claims)
    assert bool(cohort) is flagged
    if flagged:
        # The imported 0/42 sits beside the experiment's own largest-n rates.
        assert cohort[0]["pointers"] == ["C-away", "C-home-a", "C-home-b"]
        assert "C-away (0/42)" in cohort[0]["message"]


def test_skeptic_bullet_is_one_clean_sentence_without_spliced_claim_titles(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=ChallengeEngine, now=NOW)
    bullet = next(
        line for line in (output / "REPORT.md").read_text("utf-8").splitlines()
        if "[I-challenge" in line and "severity major" in line
    )
    # The title's number is nowhere in the ledger, so the card's statement speaks instead.
    assert bullet == (
        "- These measurements may not use independent label sources; label validity; "
        "severity major; referee: accept [I-challenge, C-case-0]."
    )
    assert "987654" not in bullet and "about" not in bullet


def test_skeptic_bullet_without_supported_words_names_only_the_flaw(tmp_path: Path) -> None:
    class Numbered(ChallengeEngine):
        def run_lens(
            self, lens: Lens, *, wait: bool, trigger: str, inputs: PassInputs | None = None,
        ) -> PassRecord | None:
            result = super().run_lens(lens, wait=wait, trigger=trigger, inputs=inputs)
            if lens == "skeptic":
                record = self.store.list_insights()[0]
                record.card.statement += " Only 987654 cases back this."
                self.store.save_insight(record)
            return result

    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=Numbered, now=NOW)
    bullet = next(
        line for line in (output / "REPORT.md").read_text("utf-8").splitlines()
        if "[I-challenge" in line and "referee" in line
    )
    assert bullet == (
        "- A major label validity challenge to the cited claims; referee: accept "
        "[I-challenge, C-case-0]."
    )


def test_report_states_recipe_and_rederiver_coverage(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    _input(folder)
    output = tmp_path / "output"
    analyze_experiment(folder, output, engine_factory=FakeEngine, now=NOW)
    sampled = len(rederive_sample(f"C-case-{index}" for index in range(10)))
    report = (output / "REPORT.md").read_text("utf-8")
    assert (f"Recipe-backed claims: 10 of 10; independent rederivation sampled {sampled} of 10 "
            f"recipe-backed claims and {sampled} of 10 claims") in report
    ledger = (output / "validity.json").read_text("utf-8")
    assert f"sampled {sampled} of 10 recipe-backed claims and {sampled} of 10 claims" in ledger
