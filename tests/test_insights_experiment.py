from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.check import unsupported_numbers
from evallab.insights.config import EngineConfig
from evallab.insights.engine import rederive_sample
from evallab.insights.experiment import ReportClaim, analyze_experiment, experiment_output_dir
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    Lens,
    PassRecord,
    Recipe,
    RederivationRecord,
    RederivedClaim,
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
    ) -> PassRecord:
        assert wait and trigger == "experiment"
        self.calls.append((lens, inputs))
        if lens == "extractor":
            self.store.insert_claims([ClaimRecord(
                claim_id=f"C-case-{index}", source_id="F-report", section_sha256="section-digest",
                fingerprint=f"fp-{index}", pass_id="P-extract", extracted_by="test", created_at=NOW,
                check=CheckResult(status="pass"), claim=ExtractedClaim(
                    local_id=f"case-{index}", experiment="synthetic", kind="measurement", value=5,
                    statement="The detector caught 5 cheating cases.",
                    quote="The detector caught 5 cheating cases.", section_id="X-report",
                    recipe=Recipe(op="value", file="summary.json", pointer="/caught"),
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
    assert unsupported_numbers(report, numbers) == []
    for line in report.splitlines():
        if line and not line.startswith("#"):
            assert re.search(r"\[(?:C-[a-z0-9-]+|V-[0-9]+)\]\.$", line)
    assert len(report.split()) < 500
    assert "The receipt calculation reproduced the stated number" in report
    assert len(claims) == 10
    assert all(claim.file.endswith("report.md") and claim.line == 2 for claim in claims)
    assert not any(claim.claim_id.startswith("C-audit-") for claim in claims)
    expected = rederive_sample(f"C-case-{index}" for index in range(10))
    assert engines[0].calls[1][1] == PassInputs(lens="rederiver", claim_ids=tuple(expected))
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
    assert "differs from the stated number [C-existing]" in report
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
    assert "No analyst agents ran" in report
    first = json.loads((output / "validity.json").read_text("utf-8"))["files_checked"]
    analyze_experiment(folder, output, now=NOW, run_agents=False)
    second = json.loads((output / "validity.json").read_text("utf-8"))["files_checked"]
    assert second == first
