"""Scheduler behaviour with scripted workers: the full claim -> card -> grade -> board loop.

Workers are fakes that write the lens's output file and an OMP-shaped session
record, so these tests exercise the real pass directories, imports, checks,
routing and recovery without launching a model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights.config import (
    EngineConfig,
    LensSchedule,
    RouteSpec,
    RouteTable,
    SourceOverride,
)
from evallab.insights.engine import Engine, EngineBusy, engine_lock, rederive_sample
from evallab.insights.models import (
    BoardEntry,
    BoardUpdate,
    ChallengeSpec,
    ClaimExtraction,
    EvidenceRef,
    ExtractedClaim,
    InsightBatch,
    InsightCard,
    Lens,
    PredictionSpec,
    ProposedExperiment,
    RefereeBatch,
    RefereeVerdict,
)
from evallab.insights.runner import WorkerSpec

FINDINGS = """# Overnight findings

## Update 03:25 ET
| Item | Claim |
|---|---|
| K7B | the adaptive attacker passes the ORIGINAL grader 21/21 and the separate-verifier output 0/21 (b=21, c=0) |
| D7 | forcing stage 2 on every case does NOT help (recall 26.9% vs 29.8%, p=0.63) |
"""


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@dataclass
class FakeHandle:
    pid: int
    polls_until_exit: int = 0
    exit_code: int = 0
    killed: bool = False

    def poll(self) -> int | None:
        if self.killed:
            return -9
        if self.polls_until_exit > 0:
            self.polls_until_exit -= 1
            return None
        return self.exit_code

    def kill(self) -> None:
        self.killed = True


Script = Callable[[WorkerSpec, dict[str, object]], str | None]


@dataclass
class FakeLauncher:
    """Writes each lens's output like a worker would, plus an attestation record."""

    scripts: dict[str, Script]
    served: dict[str, tuple[str, str]] = field(default_factory=dict)
    launched: list[WorkerSpec] = field(default_factory=list)
    hang: set[str] = field(default_factory=set)
    log_text: dict[str, str] = field(default_factory=dict)
    next_pid: int = 4000

    def launch(self, spec: WorkerSpec) -> FakeHandle:
        self.launched.append(spec)
        lens = spec.pass_id.split("-")[2]
        inputs = json.loads((spec.pass_dir / "pack.json").read_text("utf-8"))
        requested_provider, requested_model = spec.route.split("/", 1)
        provider, model = self.served.get(lens, (requested_provider, requested_model))
        session = spec.pass_dir / "session"
        session.mkdir(exist_ok=True)
        record = {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": model,
                "usage": {"input": 100, "output": 20, "cacheRead": 0},
            },
        }
        (session / "s.jsonl").write_text(json.dumps(record) + "\n", "utf-8")
        if lens in self.log_text:
            (spec.pass_dir / "worker.log").write_text(self.log_text[lens], "utf-8")
        script = self.scripts.get(lens)
        output = None if script is None else script(spec, inputs)
        if output is not None:
            target = spec.pass_dir / "out" / _output_name(lens)
            target.write_text(output, "utf-8")
        self.next_pid += 1
        return FakeHandle(pid=self.next_pid, polls_until_exit=10**6 if lens in self.hang else 0)

    def adopt(self, pid: int, pass_dir: Path) -> FakeHandle | None:
        return None


def _output_name(lens: str) -> str:
    return {
        "extractor": "claims.json",
        "referee": "verdicts.json",
        "synthesizer": "board.json",
        "rederiver": "rederive.json",
    }.get(lens, "cards.json")


class FixedUsage:
    def __init__(self, percents: dict[str, float] | None = None) -> None:
        self.percents = percents or {}

    def used_percent(self, provider: str, model: str | None = None) -> float | None:
        return self.percents.get(model or provider, 10.0)


def _schedule(**overrides: object) -> LensSchedule:
    values: dict[str, object] = {"cooldown_minutes": 0, "daily_cap": 20}
    values.update(overrides)
    return LensSchedule.model_validate(values)


def _config(findings: Path, **lens_overrides: LensSchedule) -> EngineConfig:
    lenses: dict[Lens, LensSchedule] = {
        "extractor": _schedule(batch_size=10),
        "skeptic": _schedule(min_new_claims=1),
        "connector": _schedule(enabled=False),
        "anomaly": _schedule(enabled=False),
        "theorist": _schedule(enabled=False),
        "ideator": _schedule(enabled=False),
        "referee": _schedule(min_pending=1),
        "synthesizer": _schedule(min_new_insights=1),
        "rederiver": _schedule(enabled=False),
    }
    lenses.update(lens_overrides)  # type: ignore[arg-type]
    return EngineConfig(
        overrides=[SourceOverride(path=str(findings), family="test-findings", kind="findings_log")],
        lenses=lenses,
        max_concurrent_passes=4,
    )


def _routes() -> RouteTable:
    analysts: list[Lens] = ["skeptic", "connector", "anomaly", "theorist", "ideator"]
    return RouteTable(
        lenses={
            "extractor": RouteSpec(routes=["anthropic/claude-sonnet-5-5"]),
            "skeptic": RouteSpec(routes=["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5"]),
            "connector": RouteSpec(routes=["anthropic/claude-opus-5-5"]),
            "anomaly": RouteSpec(routes=["anthropic/claude-sonnet-5-5"]),
            "theorist": RouteSpec(routes=["anthropic/claude-opus-5-5"]),
            "ideator": RouteSpec(routes=["anthropic/claude-opus-5-5"]),
            "referee": RouteSpec(
                routes=["google-antigravity/gemini-3.8-flash", "openai-codex/gpt-6.1-terra"],
                independent_of=analysts,
            ),
            "synthesizer": RouteSpec(routes=["anthropic/claude-opus-5-5"]),
            "rederiver": RouteSpec(routes=["openai-codex/gpt-6.1-terra"], independent_of=["extractor"]),
        }
    )


def _extract(spec: WorkerSpec, inputs: dict[str, object]) -> str:
    section_ids = list(inputs["section_ids"])  # type: ignore[call-overload]
    pack = (spec.pass_dir / "pack.md").read_text("utf-8")
    section_id = next(sid for sid in section_ids if "K7B" in _section_block(pack, sid))
    claims = [
        ExtractedClaim(
            local_id="c1",
            experiment="K7B",
            statement="The adaptive attacker passes the original grader on 21 of 21 packages.",
            kind="measurement",
            metric="attack_success_rate",
            subject="adaptive-attacker",
            dataset="mimo-dev",
            condition="original grader",
            k=21,
            n=21,
            value=1.0,
            quote="the adaptive attacker passes the ORIGINAL grader 21/21",
            section_id=section_id,
        ),
        ExtractedClaim(
            local_id="c2",
            experiment="K7B",
            statement="The same attacks pass the separate-verifier output on 0 of 21 packages.",
            kind="measurement",
            metric="attack_success_rate",
            subject="adaptive-attacker",
            dataset="mimo-dev",
            condition="separate verifier",
            k=0,
            n=21,
            value=0.0,
            quote="the separate-verifier output 0/21",
            section_id=section_id,
        ),
        ExtractedClaim(
            local_id="c3",
            experiment="K7B",
            statement="An invented claim whose quote is not in the report.",
            kind="measurement",
            k=3,
            n=4,
            quote="3 of 4 packages were attacked twice",
            section_id=section_id,
        ),
    ]
    return ClaimExtraction(
        source_id="ignored-by-engine", section_ids=section_ids, claims=claims
    ).model_dump_json()


def _section_block(pack: str, section_id: str) -> str:
    start = pack.find(f"## Section {section_id}")
    end = pack.find("## Section ", start + 1)
    return pack[start : end if end != -1 else len(pack)]


def _first_claim(engine: Engine, statement_start: str) -> str:
    for record in engine.store.list_claims():
        if record.claim.statement.startswith(statement_start):
            return record.claim_id
    raise AssertionError(f"no claim starting with {statement_start!r}")


def _skeptic(engine_ref: list[Engine]) -> Script:
    def script(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        engine = engine_ref[0]
        target = _first_claim(engine, "The adaptive attacker passes")
        other = _first_claim(engine, "The same attacks pass")
        card = InsightCard(
            local_id="k1",
            kind="challenge",
            title="Refusals may be hidden in the attacker denominator",
            statement="The 21 of 21 attack rate covers only measured packages; refused packages "
            "may be missing from the denominator.",
            evidence=[EvidenceRef(claim_id=target), EvidenceRef(claim_id=other, role="context")],
            observations=["The report gives 21 measured packages."],
            interpretation="If refused packages were dropped, the rate overstates attack power.",
            why_not_obvious="The headline counts look complete unless refusals are counted.",
            alternatives=["All packages produced a parseable attack."],
            challenge=ChallengeSpec(
                target_claim_ids=[target],
                flaw="refusal_accounting",
                severity="major",
                check="Count refused packages as no-win and recompute over the full cohort.",
                decision_at_risk="Calling the separate verifier a complete defence.",
            ),
            confidence="medium",
        )
        idea = InsightCard(
            local_id="k2",
            kind="idea",
            title="Replay attacks with refusals counted",
            statement="Re-running the attack accounting with refusals as losses tells us the "
            "honest attack rate at no cost.",
            evidence=[EvidenceRef(claim_id=target)],
            observations=["21 of 21 measured packages were beaten."],
            interpretation="The honest rate is at most the measured rate.",
            why_not_obvious="Needs the refusal count, which the headline omits.",
            alternatives=["There were no refusals."],
            predictions=[
                PredictionSpec(
                    statement="The honest attack rate over the full cohort is below 90%.",
                    metric="attack_success_rate",
                    direction="at_most",
                    high=0.9,
                    resolves_when="A recount over all packages is reported.",
                )
            ],
            proposed_experiment=ProposedExperiment(
                hypothesis="Refusals were excluded from the denominator.",
                primary_variable="denominator rule",
                fixed_variables=["attacker prompt", "packages"],
                arms=["measured-only", "refusals-as-losses"],
                cost_basis="reanalysis of existing receipts",
                est_cost_usd=0.0,
                needs_approval=False,
                reuses_existing_data=True,
                expected_if_true="rate drops below 90%",
                expected_if_false="rate stays at 100%",
            ),
            confidence="medium",
        )
        return InsightBatch(lens="skeptic", cards=[card, idea]).model_dump_json()

    return script


def _referee(spec: WorkerSpec, inputs: dict[str, object]) -> str:
    verdicts = [
        RefereeVerdict(
            insight_id=insight_id,
            supported="yes",
            novel=True,
            falsifiable=True,
            actionable=True,
            importance=4,
            non_obviousness=3,
            verdict="accept",
            notes="Cited claims match the ledger.",
        )
        for insight_id in inputs["insight_ids"]  # type: ignore[union-attr]
    ]
    return RefereeBatch(verdicts=verdicts).model_dump_json()


def _synthesizer(engine_ref: list[Engine]) -> Script:
    def script(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        ids = list(inputs["insight_ids"])  # type: ignore[call-overload]
        ideas = [
            i
            for i in ids
            if (record := engine_ref[0].store.get_insight(i)) is not None
            and record.card.kind == "idea"
        ]
        return BoardUpdate(
            headline="Attack accounting needs a recount before calling the verifier complete",
            summary="The skeptic found that the attack rate may exclude refused packages. "
            "A free recount settles it.",
            ranked=[BoardEntry(insight_id=i, rank=n + 1, rationale="Cheap and decisive.")
                    for n, i in enumerate(sorted(ids))],
            promote=ideas,
        ).model_dump_json()

    return script


def _engine(tmp_path: Path, clock: Clock, launcher: FakeLauncher, **config: LensSchedule) -> Engine:
    findings = tmp_path / "FINDINGS.md"
    if not findings.exists():
        findings.write_text(FINDINGS, "utf-8")
    return Engine(
        tmp_path / "state",
        config=_config(findings, **config),
        routes=_routes(),
        launcher=launcher,
        usage=FixedUsage(),
        host_check=lambda: True,
        clock=clock,
        python_executable="/usr/bin/python3",
        mirror_refresher=lambda repo, mirror: "unused",
    )


def test_full_loop_turns_a_report_into_a_ranked_checked_insight(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(
        scripts={
            "extractor": _extract,
            "skeptic": _skeptic(ref),
            "referee": _referee,
            "synthesizer": _synthesizer(ref),
        },
        served={"referee": ("google-antigravity", "gemini-3.8-flash")},
    )
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)

    first = engine.tick()
    assert [item["lens"] for item in first.launched] == ["extractor"]
    clock.advance(1)
    engine.tick()  # collect claims -> skeptic starts
    claims = {c.claim.local_id: c for c in engine.store.list_claims(include_failed=True)}
    assert claims["c1"].check.status != "fail"
    assert claims["c2"].check.status != "fail"
    # The invented quote is caught by code, not trusted.
    assert claims["c3"].check.status == "fail"
    assert engine.store.section_status_counts().get("pending", 0) == 0
    clock.advance(1)
    engine.tick()  # collect cards -> referee starts
    clock.advance(1)
    engine.tick()  # collect verdicts -> synthesizer starts
    clock.advance(1)
    engine.tick()  # collect board

    insights = engine.store.list_insights()
    assert {i.status for i in insights} == {"refereed"}
    assert all(i.referee_model == "google-antigravity/gemini-3.8-flash" for i in insights)
    assert sorted(i.rank for i in insights if i.rank is not None) == [1, 2]
    assert (engine.store.get_meta("board_headline") or "").startswith("Attack accounting")
    proposals = list((tmp_path / "state" / "proposals").glob("*.json"))
    assert len(proposals) == 1
    assert json.loads(proposals[0].read_text("utf-8"))["execution_authorized"] is False
    predictions = engine.store.list_predictions()
    assert len(predictions) == 1 and predictions[0].status == "open"
    assert (tmp_path / "state" / "board" / "board.md").exists()
    passes = engine.store.list_passes()
    assert {p.lens for p in passes} == {"extractor", "skeptic", "referee", "synthesizer"}
    assert all(p.status == "succeeded" for p in passes)
    referee_pass = next(p for p in passes if p.lens == "referee")
    assert referee_pass.served == {"google-antigravity/gemini-3.8-flash": 1}
    assert not referee_pass.off_route


def test_referee_from_the_authors_family_is_not_applied(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(
        scripts={"extractor": _extract, "skeptic": _skeptic(ref), "referee": _referee},
        # The gateway quietly served a Claude model for the referee route.
        served={"referee": ("anthropic", "claude-opus-5-5")},
    )
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)
    for _ in range(4):
        engine.tick()
        clock.advance(1)
    insights = engine.store.list_insights()
    assert insights and all(i.status == "checked" and i.referee is None for i in insights)
    referee_passes = sorted(
        (p for p in engine.store.list_passes() if p.lens == "referee"), key=lambda p: p.created_at
    )
    assert referee_passes[0].status == "succeeded" and referee_passes[0].off_route is True
    # The route that served another model is parked, so the retry takes the next route.
    assert referee_passes[1].route == "openai-codex/gpt-6.1-terra"
    assert "Parked" in referee_passes[1].route_reason


def test_claims_for_sections_outside_the_pass_are_refused(tmp_path: Path) -> None:
    clock = Clock()

    def rogue(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        section_ids = list(inputs["section_ids"])  # type: ignore[call-overload]
        claim = ExtractedClaim(
            local_id="x",
            experiment="K7B",
            statement="A claim attached to a section this pass was not given.",
            kind="measurement",
            k=21,
            n=21,
            quote="passes the ORIGINAL grader 21/21",
            section_id="X-not-an-input",
        )
        return ClaimExtraction(
            source_id="s", section_ids=section_ids[:1], claims=[claim]
        ).model_dump_json()

    engine = _engine(
        tmp_path,
        clock,
        FakeLauncher(scripts={"extractor": rogue}),
        extractor=_schedule(batch_size=10, cooldown_minutes=30),
    )
    engine.tick()
    clock.advance(1)
    engine.tick()
    assert engine.store.list_claims(include_failed=True) == []
    extractor = next(p for p in engine.store.list_passes() if p.lens == "extractor")
    assert extractor.outputs["refused"] == 1
    # The section the worker did not declare covered goes back to the queue.
    counts = engine.store.section_status_counts()
    assert counts.get("extracted") == 1 and counts.get("pending") == 1


def test_invalid_output_releases_sections_until_three_attempts(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"extractor": lambda spec, inputs: "{not json"})
    engine = _engine(tmp_path, clock, launcher)
    statuses = []
    for _ in range(4):
        engine.tick()
        clock.advance(1)
        statuses.append(engine.store.section_status_counts())
    extractor_passes = [p for p in engine.store.list_passes() if p.lens == "extractor"]
    assert len(extractor_passes) == 3
    assert all(p.status == "invalid_output" for p in extractor_passes)
    assert statuses[-1].get("failed", 0) >= 1 and statuses[-1].get("pending", 0) == 0


def test_quota_failure_parks_the_route_and_the_next_pass_falls_back(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    calls = {"skeptic": 0}

    def skeptic(spec: WorkerSpec, inputs: dict[str, object]) -> str | None:
        calls["skeptic"] += 1
        return None if calls["skeptic"] == 1 else _skeptic(ref)(spec, inputs)

    launcher = FakeLauncher(
        scripts={"extractor": _extract, "skeptic": skeptic},
        log_text={"skeptic": "Error: 429 Too Many Requests: rate limit, retry after 7200 seconds"},
    )
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)
    engine.tick()
    clock.advance(1)
    engine.tick()  # skeptic launched on opus; it writes no output
    clock.advance(1)
    engine.tick()  # collected: parked -> new skeptic pass on the fallback route
    skeptic_passes = sorted(
        (p for p in engine.store.list_passes() if p.lens == "skeptic"), key=lambda p: p.created_at
    )
    assert skeptic_passes[0].status == "parked_quota"
    assert skeptic_passes[1].route == "anthropic/claude-sonnet-5-5"
    assert "Parked" in skeptic_passes[1].route_reason


def test_cooldown_and_daily_cap_hold_back_a_lens(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": _skeptic(ref)})
    engine = _engine(tmp_path, clock, launcher, skeptic=_schedule(cooldown_minutes=30, daily_cap=2))
    ref.append(engine)
    engine.tick()  # extractor
    clock.advance(1)
    engine.tick()  # claims in, skeptic #1
    clock.advance(1)
    report = engine.tick()
    assert "cooling down" in report.waiting["skeptic"]
    clock.advance(60)
    (tmp_path / "FINDINGS.md").write_text(
        FINDINGS + "\n## Update 04:25 ET\nK7B rerun: the adaptive attacker passes the ORIGINAL "
        "grader 21/21 again.\n",
        "utf-8",
    )
    report = engine.tick()  # new section -> extractor; skeptic has nothing new yet
    assert report.waiting["skeptic"].startswith("waiting: 0 new claims")
    clock.advance(1)
    report = engine.tick()  # new claims -> skeptic #2
    assert "skeptic" in [item["lens"] for item in report.launched]
    clock.advance(1)
    report = engine.tick()
    assert "daily cap reached (2 of 2" in report.waiting["skeptic"]


def test_timeout_kills_the_worker_and_releases_its_sections(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={}, hang={"extractor"})
    engine = _engine(tmp_path, clock, launcher)
    engine.tick()
    clock.advance(60)
    collected = engine.collect()
    assert collected == [
        {"pass_id": collected[0]["pass_id"], "lens": "extractor", "status": "timed_out"}
    ]
    assert engine.store.section_status_counts().get("pending", 0) >= 1


def test_restart_collects_a_finished_worker_it_no_longer_tracks(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"extractor": _extract})
    engine = _engine(tmp_path, clock, launcher)
    engine.tick()
    restarted = _engine(tmp_path, clock, FakeLauncher(scripts={}))
    collected = restarted.recover()
    assert [item["status"] for item in collected] == ["succeeded"]
    assert len(restarted.store.list_claims()) == 2


def test_dry_run_reports_without_starting_anything(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"extractor": _extract})
    engine = _engine(tmp_path, clock, launcher)
    engine.scan()
    report = engine.tick(dry_run=True)
    assert report.waiting["extractor"].startswith("would launch")
    assert launcher.launched == []
    assert engine.store.list_passes() == []


def test_rederive_sample_is_stable_as_the_ledger_grows() -> None:
    ids = [f"C-{n:04d}" for n in range(200)]
    small = set(rederive_sample(ids[:100]))
    large = set(rederive_sample(ids))
    assert small <= large
    assert 0.1 < len(large) / len(ids) < 0.3
    assert rederive_sample(["C-only"]) == ["C-only"]
    assert rederive_sample([]) == []


def test_second_engine_on_the_same_state_is_refused(tmp_path: Path) -> None:
    with engine_lock(tmp_path), pytest.raises(EngineBusy), engine_lock(tmp_path):
        pass
