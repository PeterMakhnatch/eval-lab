"""Scheduler behaviour with scripted workers: the full claim -> card -> grade -> board loop.

Workers are fakes that write the lens's output file and an OMP-shaped session
record, so these tests exercise the real pass directories, imports, checks,
routing and recovery without launching a model.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights import duplicates as duplicates_mod
from evallab.insights import engine as engine_mod
from evallab.insights import recompute as recompute_mod
from evallab.insights import scope as scope_mod
from evallab.insights.commands.status import collect_status
from evallab.insights.config import (
    EngineConfig,
    LensSchedule,
    RepoMirror,
    RouteSpec,
    RouteTable,
    SourceFamily,
    SourceOverride,
    write_defaults,
)
from evallab.insights.engine import Engine, EngineBusy, engine_lock, rederive_sample
from evallab.insights.models import (
    BoardEntry,
    BoardUpdate,
    ChallengeSpec,
    CheckResult,
    ClaimExtraction,
    ClaimRecord,
    EvidenceRef,
    ExtractedClaim,
    InsightBatch,
    InsightCard,
    InsightRecord,
    Lens,
    PassRecord,
    PredictionSpec,
    ProposedExperiment,
    Recipe,
    RecomputeResult,
    RederiveBatch,
    RederivedClaim,
    RefereeBatch,
    RefereeVerdict,
    TwinBatch,
    TwinCard,
)
from evallab.insights.packs import PassInputs, referee_pack_size
from evallab.insights.runner import (
    TRANSIENT_ERROR,
    TRANSIENT_EXHAUSTED_ERROR,
    WorkerSpec,
    transient_retry_delay,
)

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
    #: omp's own stderr (the only place provider errors are read from)
    log_text: dict[str, str] = field(default_factory=dict)
    #: the analyst's answer text (worker.log); must never be read as a failure
    answer_text: dict[str, str] = field(default_factory=dict)
    #: worker died at startup before any model response
    no_session: dict[str, str] = field(default_factory=dict)
    exit_after: dict[str, int] = field(default_factory=dict)
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
                "usage": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 7},
            },
        }
        if lens in self.no_session:
            (spec.pass_dir / "worker.log").write_text(self.no_session[lens], "utf-8")
        else:
            (session / "s.jsonl").write_text(json.dumps(record) + "\n", "utf-8")
        if lens in self.log_text:
            (spec.pass_dir / "worker.stderr").write_text(self.log_text[lens], "utf-8")
        if lens in self.answer_text:
            (spec.pass_dir / "worker.log").write_text(self.answer_text[lens], "utf-8")
        script = self.scripts.get(lens)
        output = None if script is None else script(spec, inputs)
        if output is not None:
            target = spec.pass_dir / "out" / _output_name(lens)
            target.write_text(output, "utf-8")
        self.next_pid += 1
        return FakeHandle(
            pid=self.next_pid,
            polls_until_exit=10**6 if lens in self.hang else self.exit_after.get(lens, 0),
        )

    def adopt(self, pid: int, pass_dir: Path) -> FakeHandle | None:
        return None


def _output_name(lens: str) -> str:
    return {
        "extractor": "claims.json",
        "referee": "verdicts.json",
        "synthesizer": "board.json",
        "rederiver": "rederive.json",
        "twinmaker": "twins.json",
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
        launch_sleep=lambda _seconds: None,
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
    # A failing pass backs off exponentially on the same inputs, so give each
    # round enough time for the backoff to lapse.
    for _ in range(8):
        engine.tick()
        clock.advance(60)
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
    assert report.waiting["skeptic"].startswith("waiting: 0 experiments not yet reviewed")
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


# --------------------------------------------------------------------------
# Robustness: one bad pass, a failing mirror or a failing tick never stops the engine
# --------------------------------------------------------------------------


def _query(engine: Engine, sql: str) -> list[dict[str, object]]:
    return engine.store.query(sql)


def test_a_vanished_source_fails_one_pass_and_the_engine_keeps_ticking(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.tick()  # extractor starts and writes its output
    (tmp_path / "FINDINGS.md").unlink()  # the report folder was moved away
    clock.advance(1)
    report = engine.tick()
    extractor = next(p for p in engine.store.list_passes() if p.lens == "extractor")
    assert extractor.status == "failed"
    assert extractor.error is not None and "collect failed" in extractor.error
    assert engine.store.list_passes(statuses=["planned", "running"]) == []
    assert (tmp_path / "state" / "heartbeat.json").exists()
    assert report.finished_at
    # The next tick runs normally; nothing is stuck in flight.
    clock.advance(1)
    engine.tick()
    assert engine.store.section_status_counts().get("in_flight", 0) == 0


def test_a_failing_step_still_writes_the_heartbeat_with_the_error(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={}))

    def boom() -> int:
        raise OSError("signals exploded")

    engine.refresh_signals = boom  # type: ignore[method-assign]
    report = engine.tick()
    assert any("signals failed" in error for error in report.errors)
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text("utf-8"))
    assert any("signals failed" in error for error in heartbeat["errors"])
    # jobs-health reads job/ok/detail/max_age_minutes from every ~/.local/state/*/heartbeat.json.
    assert heartbeat["job"] == "evallab-insights"
    assert heartbeat["ok"] is False
    assert "signals failed" in heartbeat["detail"]


def test_a_clean_tick_writes_an_ok_heartbeat_for_jobs_health(tmp_path: Path) -> None:
    engine = _engine(tmp_path, Clock(), FakeLauncher(scripts={}))
    report = engine.tick()
    assert report.errors == []
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text("utf-8"))
    assert heartbeat["job"] == "evallab-insights"
    assert heartbeat["ok"] is True
    assert heartbeat["detail"] == ""
    assert heartbeat["max_age_minutes"] >= 3 * heartbeat["tick_seconds"] / 60


def test_watch_survives_a_failing_recovery_and_failing_ticks(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={}))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk went away")

    engine.recover = boom  # type: ignore[method-assign]
    engine.tick = boom  # type: ignore[method-assign]
    ticks = engine.watch(max_ticks=2, sleep=lambda _seconds: None)
    assert ticks == 2
    heartbeat = json.loads((tmp_path / "state" / "heartbeat.json").read_text("utf-8"))
    assert any("tick failed" in error for error in heartbeat["errors"])
    kinds = {event["kind"] for event in engine.store.events(limit=50)}
    assert "tick_failed" in kinds


def test_mirror_failure_backs_off_and_scanning_continues(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.config = engine.config.model_copy(
        update={
            "repo_mirror": RepoMirror(
                repo=str(tmp_path / "repo"), path=str(tmp_path / "mirror"), refresh_minutes=60
            )
        }
    )
    calls: list[datetime] = []

    def failing(repo: Path, mirror: Path) -> str:
        calls.append(clock())
        raise subprocess.CalledProcessError(128, ["git", "fetch"])

    engine.mirror_refresher = failing
    report = engine.tick()
    assert any("mirror refresh failed" in error for error in report.errors)
    assert [item["lens"] for item in report.launched] == ["extractor"]  # scanning went on
    assert (tmp_path / "state" / "heartbeat.json").exists()
    clock.advance(2)
    engine.tick()
    assert len(calls) == 1  # backing off, not retrying every tick
    clock.advance(4)
    engine.tick()
    assert len(calls) == 2


def test_mirror_git_calls_have_a_deadline_and_never_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "abc123\n", "")

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_run)
    assert engine_mod._safe_mirror_refresh(tmp_path / "repo", tmp_path / "mirror") == "abc123"
    assert seen and all(kwargs["timeout"] == engine_mod.MIRROR_GIT_TIMEOUT for kwargs in seen)
    assert all(kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0" for kwargs in seen)  # type: ignore[index]


def test_pack_json_is_never_trusted_and_tampering_fails_the_pass(tmp_path: Path) -> None:
    clock = Clock()

    def tamper(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        output = _extract(spec, inputs)
        (spec.pass_dir / "pack.json").write_text(json.dumps({"lens": "skeptic"}), "utf-8")
        return output

    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": tamper}))
    engine.tick()
    clock.advance(1)
    engine.tick()
    extractor = next(p for p in engine.store.list_passes() if p.lens == "extractor")
    assert extractor.status == "failed"
    assert extractor.error is not None and "changed after launch" in extractor.error
    assert engine.store.list_claims(include_failed=True) == []
    assert engine.store.section_status_counts().get("extracted", 0) == 0


def test_dry_run_is_read_only_even_with_unreadable_sections(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.scan()
    (tmp_path / "FINDINGS.md").unlink()
    sections = "SELECT section_id, status, attempts FROM sections ORDER BY section_id"
    before = _query(engine, sections)
    report = engine.tick(dry_run=True)
    assert _query(engine, sections) == before
    assert report.waiting["extractor"].startswith("waiting: pending sections unreadable")
    assert engine.store.list_passes() == []
    with pytest.raises(FileNotFoundError):
        Engine(tmp_path / "mistyped", readonly=True)
    assert not (tmp_path / "mistyped").exists()


def test_config_edits_are_picked_up_and_a_bad_edit_keeps_the_old_values(tmp_path: Path) -> None:
    clock = Clock()
    state = tmp_path / "state"
    write_defaults(state)
    engine = Engine(
        state, routes=_routes(), launcher=FakeLauncher(scripts={}), usage=FixedUsage(),
        host_check=lambda: True, clock=clock, mirror_refresher=lambda repo, mirror: "unused",
    )
    config_file = state / "config.yaml"
    config_file.write_text(config_file.read_text("utf-8").replace("board_top_n: 12", "board_top_n: 5"))
    engine.tick(dry_run=True)
    assert engine.config.board_top_n == 5
    config_file.write_text("schema_version: 1\nboard_top_n: -3\n")
    report = engine.tick(dry_run=True)
    assert any("config reload failed" in error for error in report.errors)
    assert engine.config.board_top_n == 5


# --------------------------------------------------------------------------
# Scheduling: source priority over the whole backlog, failure classification, backoff
# --------------------------------------------------------------------------


def test_extractor_picks_the_highest_priority_source_over_the_whole_backlog(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={}))
    low = tmp_path / "low.md"
    high = tmp_path / "high.md"
    # More than the old 500-row newest-first window, all newer than the priority source.
    low.write_text("\n".join(f"## Low {n:04d}\nline {n}\n" for n in range(520)), "utf-8")
    high.write_text("## High one\nthe important result 3/4\n\n## High two\nanother 1/2\n", "utf-8")
    old = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    os.utime(high, (old, old))
    engine.config = engine.config.model_copy(
        update={
            "overrides": [],
            "sources": [
                SourceFamily(family="low", kind="report", globs=[str(low)], priority=10),
                SourceFamily(family="high", kind="report", globs=[str(high)], priority=90),
            ],
        }
    )
    engine.scan()
    inputs, why = engine.build_inputs("extractor", engine.config.schedule("extractor"))
    assert inputs is not None
    section = engine.store.get_section(inputs.section_ids[0])
    assert section is not None
    source = engine.store.get_source(section.source_id)
    assert source is not None and source.path.endswith("high.md")
    assert "522 report sections pending" in why


def test_the_analysts_own_words_about_quotas_do_not_park_the_route(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(
        scripts={"extractor": _extract},
        answer_text={"skeptic": "n=429 trials hit the rate-limit; the daily quota ceiling was reached"},
    )
    engine = _engine(tmp_path, clock, launcher)
    for _ in range(3):
        engine.tick()
        clock.advance(1)
    skeptic = next(p for p in engine.store.list_passes() if p.lens == "skeptic")
    assert skeptic.status == "failed"
    assert skeptic.error is not None and "quota" not in skeptic.error
    assert engine._parked() == {}


def test_an_auth_error_parks_the_provider_and_blocked_routes_leave_no_pass_records(
    tmp_path: Path,
) -> None:
    clock = Clock()
    launcher = FakeLauncher(
        scripts={"extractor": _extract}, log_text={"skeptic": "HTTP 401 invalid_grant"}
    )
    engine = _engine(tmp_path, clock, launcher)
    for _ in range(8):
        engine.tick()
        clock.advance(1)
    skeptic = [p for p in engine.store.list_passes() if p.lens == "skeptic"]
    assert len(skeptic) == 1 and skeptic[0].status == "failed"
    assert skeptic[0].error is not None and "authentication failed" in skeptic[0].error
    assert "anthropic" in engine._parked()
    report = engine.tick()
    assert "no route available" in report.waiting["skeptic"] or "routes are blocked" in report.waiting["skeptic"]
    assert len([p for p in engine.store.list_passes() if p.lens == "skeptic"]) == 1


def test_no_route_waits_with_backoff_instead_of_a_failed_pass_every_tick(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.usage = FixedUsage({"anthropic/claude-sonnet-5-5": 99.0})
    report = engine.tick()
    for _ in range(4):
        clock.advance(1)
        report = engine.tick()
    assert engine.store.list_passes() == []
    assert "no route available" in report.waiting["extractor"]
    assert len([e for e in engine.store.events(limit=50) if e["kind"] == "no_route"]) == 1


def test_repeated_failures_with_the_same_inputs_back_off_exponentially(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": lambda spec, inputs: "{not json"})
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)
    engine.tick()
    clock.advance(1)
    engine.tick()  # skeptic #1 launched
    clock.advance(1)
    report = engine.tick()  # #1 invalid -> one failure on these inputs
    assert "backing off after 1 failures" in report.waiting["skeptic"]
    state = engine._backoff_state()["skeptic"]
    assert [entry["failures"] for entry in state.values()] == [1]
    clock.advance(2)
    engine.tick()  # backoff over: #2 launches
    clock.advance(1)
    report = engine.tick()  # #2 invalid -> two failures, twice the delay
    assert "backing off after 2 failures" in report.waiting["skeptic"]
    launched_before = len(launcher.launched)
    clock.advance(1)
    engine.tick()
    assert len(launcher.launched) == launched_before  # still inside the longer delay


def _skeptic_passes(engine: Engine) -> list[PassRecord]:
    return sorted(engine.store.list_passes(lens="skeptic"), key=lambda p: p.created_at)


BUSY = "SQLiteError: Database agent.db: database is locked (SQLITE_BUSY)"


def test_a_busy_omp_database_is_retried_twice_after_jittered_pauses_then_fails_as_transient(
    tmp_path: Path,
) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"extractor": _extract}, no_session={"skeptic": BUSY})
    engine = _engine(tmp_path, clock, launcher, skeptic=_schedule(cooldown_minutes=30))
    engine.tick()
    clock.advance(1)
    engine.tick()  # skeptic #1 starts
    clock.advance(1)
    report = engine.tick()  # #1 died at startup: retry 1 of 2
    first = _skeptic_passes(engine)[0]
    assert first.error == TRANSIENT_ERROR
    assert engine._backoff_state() == {}  # a transient start is not a content failure
    assert "transient" in report.waiting["skeptic"]
    first_delay = transient_retry_delay(first.pass_id)
    assert timedelta(seconds=15) <= first_delay <= timedelta(seconds=45)
    assert engine.store.get_meta("transient_until:skeptic") == (clock() + first_delay).isoformat()

    clock.now += first_delay - timedelta(seconds=1)
    engine.tick()
    assert len(_skeptic_passes(engine)) == 1  # still inside the jittered pause
    clock.now += timedelta(seconds=1)
    engine.tick()  # the retry ignores the 30 minute cooldown
    assert len(_skeptic_passes(engine)) == 2
    clock.advance(1)
    engine.tick()  # #2 hits the same collision: retry 2 of 2, with its own jitter
    second = _skeptic_passes(engine)[1]
    assert second.error == TRANSIENT_ERROR
    second_delay = transient_retry_delay(second.pass_id)
    assert engine.store.get_meta("transient_until:skeptic") == (clock() + second_delay).isoformat()
    clock.now += second_delay
    engine.tick()
    assert len(_skeptic_passes(engine)) == 3
    clock.advance(1)
    report = engine.tick()  # #3: the retries are spent
    third = _skeptic_passes(engine)[2]
    # Still a startup collision, never "no output file (other; exit 0)".
    assert third.error == TRANSIENT_EXHAUSTED_ERROR
    assert engine._backoff_state()  # now it backs off like any failure
    assert "cooling down" in report.waiting["skeptic"]  # ordinary cooldown, no more retries
    clock.advance(1)
    engine.tick()
    assert len(_skeptic_passes(engine)) == 3
    events = engine.store.events(limit=100)
    retries = [e for e in events if e["kind"] == "transient_startup_failure"]
    assert [event["detail"]["retry"] for event in reversed(retries)] == [1, 2]


def test_a_successful_retry_forgets_the_transient_count(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    calls = {"n": 0}

    def flaky_skeptic(spec: WorkerSpec, inputs: dict[str, object]) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            (spec.pass_dir / "worker.stderr").write_text(BUSY, "utf-8")
            (spec.pass_dir / "session" / "s.jsonl").unlink()
            return None
        return _skeptic(ref)(spec, inputs)

    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": flaky_skeptic})
    engine = _engine(tmp_path, clock, launcher, skeptic=_schedule(cooldown_minutes=30))
    ref.append(engine)
    engine.tick()
    clock.advance(1)
    engine.tick()
    clock.advance(1)
    engine.tick()  # #1 busy at startup
    assert _skeptic_passes(engine)[0].error == TRANSIENT_ERROR
    clock.advance(1)  # past the longest 45 s jitter
    engine.tick()
    clock.advance(1)
    engine.tick()
    assert [p.status for p in _skeptic_passes(engine)] == ["failed", "succeeded"]
    left = _query(engine, "SELECT key FROM meta WHERE key LIKE 'transient:%' AND value != ''")
    assert left == []


def test_a_failure_that_is_not_a_startup_collision_is_never_retried(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(
        scripts={"extractor": _extract},
        no_session={"skeptic": "Error: the model returned nothing useful"},
    )
    engine = _engine(tmp_path, clock, launcher, skeptic=_schedule(cooldown_minutes=0))
    engine.tick()
    clock.advance(1)
    engine.tick()
    clock.advance(1)
    report = engine.tick()
    first = _skeptic_passes(engine)[0]
    assert first.error is not None and first.error.startswith("no output file")
    assert not engine.store.get_meta("transient_until:skeptic")
    assert "retrying shortly" not in report.waiting["skeptic"]
    events = engine.store.events(limit=100)
    assert not [e for e in events if e["kind"] == "transient_startup_failure"]
    clock.now += timedelta(seconds=50)  # beyond any transient pause
    engine.tick()
    assert len(_skeptic_passes(engine)) == 1  # ordinary exponential backoff, not a retry
    assert engine._backoff_state()


def test_worker_starts_in_one_refill_are_staggered(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"skeptic": _empty_skeptic}, exit_after={"skeptic": 5})
    engine = _coverage_engine(tmp_path, clock, launcher, max_parallel=2)
    assert engine.config.launch_stagger_seconds == 4
    sleeps: list[float] = []
    engine._launch_sleep = sleeps.append
    _seed_claims(engine, clock, {f"E{n}": n for n in range(1, 8)})
    report = engine.tick()
    assert len([item for item in report.launched if item["lens"] == "skeptic"]) == 2
    assert sleeps == [4.0]  # none before the first start, one stagger before the second
    # Starts that are already further apart than the stagger are not delayed again.
    clock.now += timedelta(seconds=10)
    engine._stagger_launch()
    assert sleeps == [4.0]
    engine._last_launch_at = clock() - timedelta(seconds=1)
    engine._stagger_launch()
    assert sleeps == [4.0, 3.0]
    engine.config = engine.config.model_copy(update={"launch_stagger_seconds": 0})
    engine._last_launch_at = clock()
    engine._stagger_launch()
    assert sleeps == [4.0, 3.0]


def test_quota_and_timeout_failures_do_not_use_up_section_attempts(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={}, log_text={"extractor": "HTTP 429 quota exhausted"})
    engine = _engine(tmp_path, clock, launcher)
    engine.scan()
    schedule = engine.config.schedule("extractor")
    for _ in range(4):
        inputs, _why = engine.build_inputs("extractor", schedule)
        assert inputs is not None
        record = engine.launch("extractor", inputs, trigger="test")
        assert record.status == "running"
        clock.advance(1)
        engine.collect()
        clock.advance(61)  # the parked route is free again
    passes = [p for p in engine.store.list_passes() if p.lens == "extractor"]
    assert [p.status for p in passes] == ["parked_quota"] * 4
    assert {row["attempts"] for row in _query(engine, "SELECT attempts FROM sections")} == {0}
    assert engine.store.section_status_counts().get("failed", 0) == 0


def test_editing_a_section_resets_its_attempts(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"extractor": lambda spec, inputs: "{not json"})
    engine = _engine(tmp_path, clock, launcher)
    engine.scan()
    schedule = engine.config.schedule("extractor")
    for _ in range(2):
        inputs, _why = engine.build_inputs("extractor", schedule)
        assert inputs is not None
        engine.launch("extractor", inputs, trigger="test")
        clock.advance(1)
        engine.collect()
    assert {row["attempts"] for row in _query(engine, "SELECT attempts FROM sections")} == {2}
    (tmp_path / "FINDINGS.md").write_text(FINDINGS + "\nK7B follow-up: 3/4 repeated.\n", "utf-8")
    engine.scan()
    rows = _query(engine, "SELECT status, attempts FROM sections")
    assert {"status": "pending", "attempts": 0} in rows


def test_a_section_edited_mid_pass_is_not_given_to_a_second_extractor(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.tick()  # extractor running on the original text
    in_flight = engine.store.section_status_counts().get("in_flight", 0)
    assert in_flight >= 1
    (tmp_path / "FINDINGS.md").write_text(FINDINGS + "\nA late edit to the same section.\n", "utf-8")
    engine.scan()
    assert engine.store.section_status_counts().get("in_flight", 0) == in_flight
    launched, _waiting = engine.plan_and_launch()
    assert [item for item in launched if item["lens"] == "extractor"] == []
    clock.advance(1)
    engine.collect()
    # Claims read from the old text are not stored against the new text.
    assert engine.store.list_claims(include_failed=True) == []
    extractor = next(p for p in engine.store.list_passes() if p.lens == "extractor")
    assert extractor.outputs["refused"] == 3
    assert _query(engine, "SELECT 1 FROM sections WHERE status = 'pending' AND attempts = 0")


def test_newly_graded_cards_are_counted_by_when_they_were_graded(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": _skeptic(ref)})
    engine = _engine(tmp_path, clock, launcher, referee=_schedule(enabled=False))
    ref.append(engine)
    for _ in range(3):
        engine.tick()
        clock.advance(1)
    cards = engine.store.list_insights(statuses=["checked"])
    assert cards
    engine.store.save_pass(
        PassRecord(
            pass_id="P-seed-synthesizer", lens="synthesizer", route="anthropic/claude-opus-5-5",
            trigger="seed", status="succeeded", pass_dir=str(tmp_path),
            created_at=clock() + timedelta(minutes=5),
        )
    )
    # Cards created before the synthesizer's last pass but graded after it still count.
    for card in cards:
        engine.store.save_insight(
            card.model_copy(update={"status": "refereed"}), graded_at=clock() + timedelta(minutes=10)
        )
    inputs, why = engine.build_inputs("synthesizer", engine.config.schedule("synthesizer"))
    assert inputs is not None and "newly graded" in why


def test_a_rederiver_from_the_extractors_family_is_not_counted(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.tick()
    clock.advance(1)
    engine.tick()
    claim = engine.store.list_claims()[0]
    record = PassRecord(
        pass_id="P-seed-rederiver", lens="rederiver", route="r/x", trigger="seed",
        status="running", pass_dir=str(tmp_path), created_at=clock(),
    )
    inputs = PassInputs(lens="rederiver", claim_ids=(claim.claim_id,))
    batch = RederiveBatch(
        claims=[RederivedClaim(claim_id=claim.claim_id, not_recomputable_reason="no receipts")]
    )
    same = engine._import_rederivations(record, inputs, batch, claim.extracted_by)
    assert same == {"rederived": 0, "agree": 0, "not_independent": 1}
    other = engine._import_rederivations(record, inputs, batch, "google-antigravity/gemini-3.8-flash")
    assert other["rederived"] == 1 and other["not_independent"] == 0


def test_agreement_compares_values_within_printed_rounding() -> None:
    agree = RecomputeResult(status="reproduced", value=48.0)
    claim = ExtractedClaim(
        local_id="c", experiment="E1", statement="48 packages were dropped overall.",
        kind="measurement", k=48, value=48.0, quote="48 dropped", section_id="s",
    )
    # Same number, different status wording: values decide.
    assert engine_mod._agreement(RecomputeResult(status="differs", value=48.0), agree, claim) is True
    assert engine_mod._agreement(RecomputeResult(status="reproduced", value=47.0), agree, claim) is False
    # One side failed, the other did not: disagreement.
    failed = RecomputeResult(status="not_recomputable")
    assert engine_mod._agreement(failed, agree, claim) is False
    # Neither side produced a number: not comparable.
    assert engine_mod._agreement(failed, RecomputeResult(status="not_recomputable"), claim) is None
    assert engine_mod._agreement(None, agree, claim) is None


def test_agreement_compares_a_count_recipe_with_a_ratio_recipe_by_the_claim(
    tmp_path: Path,
) -> None:
    rows = [{"cheat": index < 4, "case": index} for index in range(10)]
    (tmp_path / "rows.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    claim = ExtractedClaim(
        local_id="c", experiment="E1", statement="The agent cheated in 4 of 10 cases (0.4).",
        kind="measurement", k=4, n=10, value=0.4, quote="4 of 10 cases (0.4)", section_id="s",
    )

    def recomputed(recipe: Recipe) -> tuple[ExtractedClaim, RecomputeResult]:
        candidate = claim.model_copy(update={"recipe": recipe})
        result = recompute_mod.recompute_claim(candidate, tmp_path)
        assert result is not None
        return candidate, result

    count = Recipe(op="count", target="k", file="rows.jsonl", where={"cheat": True})
    ratio = Recipe(
        op="ratio", target="value",
        num=Recipe(op="count", file="rows.jsonl", where={"cheat": True}),
        den=Recipe(op="count", file="rows.jsonl"),
    )
    original_claim, original = recomputed(count)
    _, rederived = recomputed(ratio)
    # The count recipe checks k only (n is not checked), so it is partial, not reproduced.
    assert (original.status, original.value) == ("partial", 4.0)
    assert (rederived.status, rederived.value) == ("reproduced", 0.4)
    # 4 against 0.4 is a unit mismatch, not a disagreement: each side reproduces the claim.
    assert engine_mod._agreement(original, rederived, original_claim, ratio) is True
    # A rederivation that reproduces the claim differently is still checked on its own side.
    wrong_ratio = Recipe(
        op="ratio", target="value",
        num=Recipe(op="count", file="rows.jsonl", where={"cheat": False}),
        den=Recipe(op="count", file="rows.jsonl"),
    )
    _, off = recomputed(wrong_ratio)
    assert off.status == "differs"
    assert engine_mod._agreement(original, off, original_claim, wrong_ratio) is False
    # Same target on both sides keeps comparing the numbers themselves.
    other_count = Recipe(op="count", target="k", file="rows.jsonl", where={"cheat": False})
    _, other = recomputed(other_count)
    assert engine_mod._agreement(original, other, original_claim, other_count) is False


def test_fast_cycle_collects_an_exited_worker_without_waiting_for_the_next_tick(
    tmp_path: Path,
) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.config = engine.config.model_copy(update={"tick_seconds": 10})
    states: list[list[str]] = []

    def sleep(_seconds: float) -> None:
        states.append([p.status for p in engine.store.list_passes(lens="extractor")])

    engine.watch(max_ticks=2, sleep=sleep, collect_seconds=1)
    assert states[0] == ["running"]
    assert "succeeded" in states[1]  # collected one poll later, not a full tick later


def test_status_explains_each_lens_in_plain_words(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    for _ in range(3):
        engine.tick()
        clock.advance(1)
    status = collect_status(tmp_path / "state", now=clock())
    assert status["engine_alive"] is True
    lenses = status["lenses"]
    assert isinstance(lenses, dict)
    assert "experiments reviewed" in lenses["skeptic"]["text"]
    assert "no receipt-backed claims yet" in lenses["rederiver"]["text"]
    assert "/" in lenses["theorist"]["text"]
    coverage = status["coverage"]
    assert isinstance(coverage, dict) and coverage["recipe_backed"] == 0
    stale = collect_status(tmp_path / "state", now=clock() + timedelta(hours=2))
    assert stale["engine_alive"] is False


# --------------------------------------------------------------------------
# Systematic skeptic coverage
# --------------------------------------------------------------------------


def _empty_skeptic(spec: WorkerSpec, inputs: dict[str, object]) -> str:
    return InsightBatch(lens="skeptic", cards=[]).model_dump_json()


def _seed_claims(engine: Engine, clock: Clock, counts: dict[str, int], *, tag: str = "a") -> None:
    records = [
        ClaimRecord(
            claim_id=f"C-{exp}-{tag}{n:02d}", source_id="S-seed", section_sha256=f"sha-{exp}",
            fingerprint=f"FP-{exp}-{tag}{n}", pass_id="P-seed",
            extracted_by="anthropic/claude-sonnet-5-5", created_at=clock(),
            claim=ExtractedClaim(
                local_id=f"c{n}", experiment=exp, statement=f"{exp} claim {tag}{n} measured a rate",
                kind="measurement", quote=f"quote {exp} {tag}{n}", section_id=f"sec-{exp}",
            ),
            check=CheckResult(status="pass"),
        )
        for exp, count in counts.items()
        for n in range(count)
    ]
    engine.store.insert_claims(records)


def _reviewed(engine: Engine, pass_id: str) -> list[str]:
    stored = engine.store.pass_inputs(pass_id)
    assert stored is not None
    return list(stored["metadata"]["experiment_ids"])


def _coverage_engine(tmp_path: Path, clock: Clock, launcher: FakeLauncher, **schedule: object) -> Engine:
    return _engine(
        tmp_path, clock, launcher,
        extractor=_schedule(enabled=False),
        skeptic=_schedule(experiments_per_pass=3, **schedule),
    )


def test_seven_experiments_are_fully_covered_by_three_skeptic_passes(tmp_path: Path) -> None:
    clock = Clock()
    engine = _coverage_engine(tmp_path, clock, FakeLauncher(scripts={"skeptic": _empty_skeptic}))
    _seed_claims(engine, clock, {f"E{n}": n for n in range(1, 8)})
    for _ in range(8):
        engine.tick()
        clock.advance(1)
    passes = sorted(engine.store.list_passes(lens="skeptic"), key=lambda p: p.created_at)
    assert [p.status for p in passes] == ["succeeded"] * 3
    reviewed = [_reviewed(engine, p.pass_id) for p in passes]
    assert [len(r) for r in reviewed] == [3, 3, 1]
    assert set(reviewed[0]) == {"E7", "E6", "E5"}  # most claims first
    assert sorted(e for r in reviewed for e in r) == [f"E{n}" for n in range(1, 8)]
    assert engine.store.coverage_count("skeptic") == 7
    report = engine.tick()
    assert report.waiting["skeptic"].startswith("waiting: 0 experiments not yet reviewed")
    assert "experiments not yet reviewed" in passes[0].trigger


def test_a_changed_claim_requeues_only_its_experiment(tmp_path: Path) -> None:
    clock = Clock()
    engine = _coverage_engine(tmp_path, clock, FakeLauncher(scripts={"skeptic": _empty_skeptic}))
    _seed_claims(engine, clock, {"E1": 2, "E2": 2, "E3": 2})
    for _ in range(4):
        engine.tick()
        clock.advance(1)
    assert engine.store.coverage_count("skeptic") == 3
    _seed_claims(engine, clock, {"E2": 1}, tag="b")
    report = engine.tick()
    launched = [item for item in report.launched if item["lens"] == "skeptic"]
    assert len(launched) == 1
    assert _reviewed(engine, launched[0]["pass_id"]) == ["E2"]
    clock.advance(1)
    engine.tick()
    assert engine.tick().waiting["skeptic"].startswith("waiting: 0 experiments not yet reviewed")


def test_a_claim_added_while_a_pass_runs_is_reviewed_in_the_next_pass(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"skeptic": _empty_skeptic}, exit_after={"skeptic": 1})
    engine = _coverage_engine(tmp_path, clock, launcher)
    _seed_claims(engine, clock, {"E1": 2})
    engine.tick()  # pass #1 captures E1's digest at launch
    _seed_claims(engine, clock, {"E1": 1}, tag="late")
    clock.advance(1)
    engine.tick()  # still running
    clock.advance(1)
    report = engine.tick()  # #1 collected with the launch-time digest; E1 changed since
    assert [item["lens"] for item in report.launched] == ["skeptic"]


def test_parallel_skeptic_passes_never_share_an_experiment(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"skeptic": _empty_skeptic}, exit_after={"skeptic": 2})
    engine = _coverage_engine(tmp_path, clock, launcher, max_parallel=2)
    _seed_claims(engine, clock, {f"E{n}": n for n in range(1, 8)})
    report = engine.tick()
    first = [item["pass_id"] for item in report.launched if item["lens"] == "skeptic"]
    assert len(first) == 2
    assert not set(_reviewed(engine, first[0])) & set(_reviewed(engine, first[1]))
    seen: list[set[str]] = []
    for _ in range(10):
        clock.advance(1)
        engine.tick()
        running = [
            set(_reviewed(engine, p.pass_id))
            for p in engine.store.list_passes(lens="skeptic", statuses=["running"])
        ]
        for index, group in enumerate(running):
            assert all(not group & other for other in running[index + 1 :])
        seen.extend(running)
    assert engine.store.coverage_count("skeptic") == 7


def test_a_failed_skeptic_pass_leaves_coverage_untouched(tmp_path: Path) -> None:
    clock = Clock()
    launcher = FakeLauncher(scripts={"skeptic": lambda spec, inputs: "{not json"})
    engine = _coverage_engine(tmp_path, clock, launcher)
    _seed_claims(engine, clock, {"E1": 1, "E2": 1, "E3": 1})
    engine.tick()
    clock.advance(1)
    engine.tick()
    failed = engine.store.list_passes(lens="skeptic")[0]
    assert failed.status == "invalid_output"
    assert engine.store.coverage_count("skeptic") == 0
    assert engine.store.coverage_digests("skeptic") == {}
    launcher.scripts["skeptic"] = _empty_skeptic
    clock.advance(5)
    engine.tick()
    clock.advance(1)
    engine.tick()
    assert engine.store.coverage_count("skeptic") == 3


def test_cards_citing_claims_that_went_stale_are_rechecked_and_fail(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": _skeptic(ref)})
    engine = _engine(tmp_path, clock, launcher, referee=_schedule(enabled=False))
    ref.append(engine)
    for _ in range(3):
        engine.tick()
        clock.advance(1)
    assert {c.status for c in engine.store.list_insights()} == {"checked"}
    # The report is rewritten: the cited claims' section changed, so those claims are stale.
    (tmp_path / "FINDINGS.md").write_text(FINDINGS.replace("21/21", "20/21"), "utf-8")
    engine.scan()
    assert engine.store.insight_recheck_count() == 2
    assert engine.recheck_insights() == 2
    cards = engine.store.list_insights()
    assert {c.status for c in cards} == {"check_failed"}
    assert all("stale" in " ".join(c.check.notes) for c in cards)
    assert engine.store.insight_recheck_count() == 0


def test_status_command_prints_plain_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from evallab.insights.commands import status as status_command

    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.tick()
    args = argparse.Namespace(state_dir=tmp_path / "state", json=False)
    assert status_command._cmd(args, tmp_path) == 0
    out = capsys.readouterr().out
    assert "What each analyst is waiting for:" in out
    assert "rederiver (independent receipt check)" in out
    assert "Skeptic coverage:" in out


def _checked_cards(engine: Engine) -> list[InsightRecord]:
    engine.scan()
    engine.run_lens("extractor", poll_seconds=0, sleep=lambda _: None)
    engine.run_lens("skeptic", poll_seconds=0, sleep=lambda _: None)
    return engine.store.list_insights(statuses=["checked"])


def test_referee_batches_by_rendered_size_not_card_count(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={
        "extractor": _extract, "skeptic": _skeptic(ref), "referee": _referee,
    })
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)
    cards = _checked_cards(engine)
    assert len(cards) == 2
    sizes = [referee_pack_size(engine.store, [card.insight_id]) for card in cards]
    budget = max(sizes)
    schedule = engine.config.schedule("referee").model_copy(update={
        "max_pack_chars": budget, "batch_size": 12,
    })
    engine.config.lenses["referee"] = schedule
    inputs, _ = engine.build_inputs("referee", schedule)
    assert inputs is not None
    # A generous card-count cap does not drive selection; the second card cannot fit.
    assert len(inputs.insight_ids) == 1
    assert referee_pack_size(engine.store, inputs.insight_ids) <= budget
    record = engine.run_lens(
        "referee", inputs=inputs, poll_seconds=0, sleep=lambda _: None
    )
    assert record is not None and record.status == "succeeded"
    pack = (Path(record.pass_dir) / "pack.md").read_text()
    assert len(pack) <= budget and "more omitted" not in pack


def test_exact_and_near_duplicates_are_archived_before_refereeing(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={"extractor": _extract, "skeptic": _skeptic(ref)})
    engine = _engine(tmp_path, clock, launcher)
    ref.append(engine)
    originals = _checked_cards(engine)
    inputs = PassInputs(
        lens="skeptic", claim_ids=tuple(c.claim_id for c in engine.store.list_claims())
    )
    duplicate_pass = engine.run_lens(
        "skeptic", inputs=inputs, poll_seconds=0, sleep=lambda _: None
    )
    assert duplicate_pass is not None and duplicate_pass.outputs["duplicates"] == 2
    duplicates = engine.store.list_insights(statuses=["duplicate"])
    assert len(duplicates) == 2
    assert {card.duplicate_of for card in duplicates} == {c.insight_id for c in originals}
    referee_inputs = engine.build_inputs("referee", engine.config.schedule("referee"))[0]
    assert referee_inputs is not None
    assert set(referee_inputs.insight_ids) == {c.insight_id for c in originals}
    assert not set(referee_inputs.insight_ids).intersection(c.insight_id for c in duplicates)
    assert not any(spec.pass_id.split("-")[2] == "referee" for spec in launcher.launched)

    original = originals[0]
    near_card = original.card.model_copy(update={
        "statement": original.card.statement.replace("only measured", "only the measured")
    })
    assert duplicates_mod.text_similarity(original.card, near_card) >= duplicates_mod.DUPLICATE_THRESHOLD
    assert duplicates_mod.duplicate_of(near_card, [original]) is not None
    opposite = original.card.model_copy(update={
        "title": "The attack denominator is certainly complete",
        "statement": "The measured attack rate covers every package and establishes a complete result.",
    })
    assert duplicates_mod.text_similarity(original.card, opposite) < duplicates_mod.DUPLICATE_THRESHOLD
    assert duplicates_mod.duplicate_of(opposite, [original]) is None


def test_scope_audit_ignores_writes_but_finds_state_and_absolute_reads(tmp_path: Path) -> None:
    state = tmp_path / "state"
    own = state / "passes" / "P-own"
    session = own / "session"
    session.mkdir(parents=True)
    events = [
        {"type": "tool/call", "data": {"name": "read", "arguments": {"path": str(own / "pack.md")}}},
        {"type": "tool/call", "data": {"name": "write", "arguments": {"path": "/outside/ignored"}}},
        {"type": "tool/call", "data": {"name": "read", "arguments": {"path": str(state / "passes" / "P-other" / "out" / "cards.json")}}},
        {"type": "tool/call", "data": {"name": "bash", "arguments": {"command": "cat /outside/secret.json"}}},
    ]
    (session / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    audit = scope_mod.audit_session(
        session, state_root=state, pass_dir=own, roots=[own, tmp_path / "sources"]
    )
    assert audit.count == 2
    assert any("P-other" in example for example in audit.examples)
    assert any("/outside/secret.json" in example for example in audit.examples)


def _scope_script(engine_ref: list[Engine]) -> Script:
    base = _skeptic(engine_ref)
    def script(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        with (spec.pass_dir / "session" / "s.jsonl").open("a") as stream:
            stream.write(json.dumps({
                "type": "tool/call",
                "data": {"name": "read", "arguments": {"path": "/outside/private.json"}},
            }) + "\n")
        return base(spec, inputs)
    return script


@pytest.mark.parametrize("policy", ["flag", "reject"])
def test_scope_policy_flags_or_rejects_worker_outputs(tmp_path: Path, policy: str) -> None:
    clock = Clock()
    ref: list[Engine] = []
    launcher = FakeLauncher(scripts={
        "extractor": _extract, "skeptic": _scope_script(ref),
    })
    engine = _engine(tmp_path, clock, launcher, referee=_schedule(enabled=False))
    engine.config = engine.config.model_copy(update={"scope_policy": policy})
    ref.append(engine)
    engine.scan()
    engine.run_lens("extractor", poll_seconds=0, sleep=lambda _: None)
    record = engine.run_lens("skeptic", poll_seconds=0, sleep=lambda _: None)
    assert record is not None and record.scope_violations.count == 1
    cards = engine.store.list_insights()
    if policy == "reject":
        assert record.status == "failed" and not cards
        assert "read scope rejected" in (record.error or "")
    else:
        assert record.status == "succeeded" and len(cards) == 2
        assert all(card.check.status == "warn" for card in cards)
        assert all("Read-scope warning" in " ".join(card.check.notes) for card in cards)
        engine.render_board()
        board = (tmp_path / "state" / "board" / "board.md").read_text()
        assert "read scope:" in board and "Read-scope warning" in board


def test_scope_flag_marks_extracted_claims(tmp_path: Path) -> None:
    clock = Clock()
    def extract_with_read(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        with (spec.pass_dir / "session" / "s.jsonl").open("a") as stream:
            stream.write(json.dumps({
                "type": "tool/call",
                "data": {"name": "bash", "arguments": {"command": "cat /outside/claim-source"}},
            }) + "\n")
        return _extract(spec, inputs)

    engine = _engine(
        tmp_path, clock, FakeLauncher(scripts={"extractor": extract_with_read}),
        referee=_schedule(enabled=False),
    )
    engine.scan()
    record = engine.run_lens("extractor", poll_seconds=0, sleep=lambda _: None)
    assert record is not None and record.status == "succeeded"
    claims = engine.store.list_claims(include_failed=True)
    assert len(claims) == 3
    assert all("Read-scope warning" in " ".join(claim.check.notes) for claim in claims)
    assert any(claim.check.status == "warn" for claim in claims)


def test_planning_reports_the_specific_memory_block(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock, FakeLauncher(scripts={"extractor": _extract}))
    engine.host_check = lambda: "swap grew 3.1 GB in 5 min"
    engine.scan()
    _, waiting = engine.plan_and_launch(dry_run=True)
    assert waiting["extractor"] == "ready but swap grew 3.1 GB in 5 min"


def _accepted_source(engine: Engine, index: int) -> InsightRecord:
    claims = engine.store.list_claims()
    identity = f"I-source-{index}"
    card = InsightCard(
        local_id=identity, kind="gap", title=f"Unresolved comparison {index}",
        statement=f"Evidence group {index} leaves its own held-out comparison unresolved.",
        evidence=[EvidenceRef(claim_id=claims[index % len(claims)].claim_id)],
        observations=["The cited report leaves a separate comparison unresolved."],
        interpretation="A held-out result could change the decision for this evidence group.",
        why_not_obvious="The report emphasizes aggregate results rather than this missing cell.",
        alternatives=["The missing comparison would agree with the aggregate."],
        confidence="low",
    )
    verdict = RefereeVerdict(
        insight_id=identity, supported="yes", novel=True, falsifiable=True,
        actionable=True, importance=3, non_obviousness=3, verdict="accept",
        notes="The original card is supported by its cited claim.",
    )
    return InsightRecord(
        insight_id=identity, lens="skeptic", pass_id="P-seed",
        model_served="anthropic/claude-sonnet-5-5", created_at=engine.clock(),
        card=card, check=CheckResult(status="pass"), status="refereed",
        referee=verdict, referee_model="google-antigravity/gemini-3.8-flash",
    )


def test_hidden_twins_calibrate_referee_without_leaking_into_findings(tmp_path: Path) -> None:
    clock = Clock()
    ref: list[Engine] = []

    def twins(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        output = []
        for source_id in inputs["insight_ids"]:  # type: ignore[union-attr]
            source = ref[0].store.get_insight(str(source_id))
            assert source is not None
            card = source.card.model_copy(update={
                "local_id": source.insight_id,
                "title": "A settled conclusion for every population",
                "statement": "The cited evidence establishes this conclusion for every population.",
                "interpretation": "No further scope check is needed for this general conclusion.",
            })
            output.append(TwinCard(
                source_id=source.insight_id, corruption="overclaimed_scope", card=card,
            ))
        return TwinBatch(twins=output).model_dump_json()

    def calibration_referee(spec: WorkerSpec, inputs: dict[str, object]) -> str:
        verdicts = []
        for insight_id in inputs["insight_ids"]:  # type: ignore[union-attr]
            insight = ref[0].store.get_insight(str(insight_id))
            assert insight is not None
            flawed = insight.hidden
            verdicts.append(RefereeVerdict(
                insight_id=insight.insight_id,
                supported="no" if flawed else "yes", novel=True,
                falsifiable=not flawed, actionable=not flawed,
                importance=3, non_obviousness=3,
                verdict="reject" if flawed else "accept",
                notes="Scope is unsupported." if flawed else "Original evidence checks out.",
            ))
        return RefereeBatch(verdicts=verdicts).model_dump_json()

    launcher = FakeLauncher(scripts={
        "extractor": _extract, "twinmaker": twins, "referee": calibration_referee,
    })
    engine = _engine(
        tmp_path, clock, launcher,
        twinmaker=_schedule(min_new_insights=4, batch_size=2),
        synthesizer=_schedule(min_new_insights=1),
    )
    ref.append(engine)
    engine.scan()
    engine.run_lens("extractor", poll_seconds=0, sleep=lambda _: None)
    engine.store.insert_insights([_accepted_source(engine, index) for index in range(3)])
    schedule = engine.config.schedule("twinmaker")
    assert engine.build_inputs("twinmaker", schedule)[0] is None
    engine.store.insert_insights([_accepted_source(engine, 3)])
    for _ in range(2):
        made = engine.run_lens("twinmaker", poll_seconds=0, sleep=lambda _: None)
        assert made is not None and made.status == "succeeded"
        assert made.outputs["planted"] == 2
        assert made.route == "anthropic/claude-sonnet-5-5"  # extractor's route family
    pending = engine.store.pending_canaries()
    assert len(pending) == 4 and all(card.hidden for card in pending)
    assert len(engine.store.list_insights()) == 4  # hidden rows are not findings

    referee_inputs, _ = engine.build_inputs("referee", engine.config.schedule("referee"))
    assert referee_inputs is not None and len(referee_inputs.insight_ids) == 8
    graded = engine.run_lens(
        "referee", inputs=referee_inputs, poll_seconds=0, sleep=lambda _: None
    )
    assert graded is not None and graded.status == "succeeded"
    assert graded.outputs["calibration"] == 8
    rates = engine.store.calibration_rates(now=clock())
    assert rates["twin"]["k"] == rates["twin"]["n"] == 4  # type: ignore[index]
    assert rates["original"]["k"] == rates["original"]["n"] == 4  # type: ignore[index]
    assert rates["twin"]["wilson"]["p"] == 1.0  # type: ignore[index]
    assert rates["original"]["wilson"]["p"] == 1.0  # type: ignore[index]

    pack = (Path(graded.pass_dir) / "pack.md").read_text()
    assert '"hidden"' not in pack and '"canary_of"' not in pack
    assert '"corruption"' not in pack and '"twinmaker"' not in pack
    assert '"model_served"' not in pack and "P-seed" not in pack
    for canary in pending:
        line = next(
            line for line in pack.splitlines()
            if line.startswith('{"card":')
            and f'"insight_id":"{canary.insight_id}"' in line
        )
        anonymous = json.loads(line)
        nearest = anonymous["nearest_existing"]
        assert canary.canary_of not in {item["insight_id"] for item in nearest}

    synth, _ = engine.build_inputs("synthesizer", engine.config.schedule("synthesizer"))
    assert synth is not None
    assert not {card.insight_id for card in pending}.intersection(synth.insight_ids)
    engine.render_board()
    board = (tmp_path / "state" / "board" / "board.md").read_text()
    assert "referee caught 4 of 4 planted flawed cards" in board
    assert "accepted 4 of 4 original controls" in board
    assert all(card.insight_id not in board for card in pending)
    assert all(card.card.title not in board for card in pending)
