"""Analysis-worker contract tests (M006). Deterministic: injected adapter,
policy, probes, clock, health, spend — zero real credentials, DB, or model.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.analysis_worker import (
    AdmissionContext,
    AnalysisWorker,
    RequestStore,
)
from evallab.facts import AnalyzerCallResult
from evallab.profiles import AgentProfile, ProbeResult
from evallab.schemas import StandingApprovalsPolicy

FIXTURES = Path(__file__).parent / "fixtures" / "analysis_worker"
FROZEN = datetime(2026, 8, 15, 20, 0, 0, tzinfo=UTC)

PROFILE = AgentProfile(
    profile_id="codex-gpt-5.6-terra",
    adapter="codex",
    model="gpt-5.6-terra",
    auth_mode="subscription-auth-file",
    secret_source="file:.codex/auth.json",
    verified_facts=("2026-08-06: proven run",),
)

POLICY = StandingApprovalsPolicy(
    version=1,
    daily_cost_ceiling_usd=20.0,
    per_job_cost_ceiling_usd=3.0,
    quiet_failure_rule=3,
    auto_run=[
        {"name": "local-controls", "agents": ["oracle", "nop"]},
        {"name": "researcher-followups", "agents": ["codex", "claude-code"],
         "max_attempts": 5,
         "requires": ["schema_valid", "dedup_pass", "calibrated_judges_only"]},
    ],
    escalate_to_human=["new_task_registration", "cloud_or_remote_environment"],
)


class CountingAdapter:
    def __init__(self, response_file="saved-response.json"):
        self.calls = 0
        self.response = (FIXTURES / response_file).read_text()

    def __call__(self, prompt: str, schema: dict) -> AnalyzerCallResult:
        self.calls += 1
        return AnalyzerCallResult(
            raw_output=self.response, input_tokens=100, output_tokens=50,
            cost_usd=0.01,
        )


def make_worker(tmp_path: Path, *, adapter=None, indexer=None, stop=False,
                probe_ok=True, healthy=True, spent=0.0, est_cost=0.01,
                requirements_ok=True, profile=PROFILE,
                response="saved-response.json"):
    root = tmp_path / "repo"
    if not (root / "jobs").exists():
        shutil.copytree(FIXTURES / "jobs", root / "jobs")
        shutil.copy(FIXTURES / "stage5-prompt.md", root / "prompt.md")
        shutil.copy(FIXTURES / "stage5-rubric.json", root / "rubric.json")
    ok = requirements_ok
    context = AdmissionContext(
        stop_present=lambda: stop,
        policy=POLICY,
        profile=profile,
        probe=(lambda p: ProbeResult(ok=probe_ok,
                                     reason=None if probe_ok else "auth file missing")),
        spent_today_usd=lambda: spent,
        est_call_cost_usd=est_cost,
        services_healthy=lambda: healthy,
        requirement_checks={
            "schema_valid": lambda: ok,
            "dedup_pass": lambda: ok,
            "calibrated_judges_only": lambda: ok,
        },
    )
    worker = AnalysisWorker(
        repo_root=root,
        store=RequestStore(root / "derived" / "analyses" / "worker"),
        context=context,
        adapter=adapter or CountingAdapter(response),
        prompt_path=root / "prompt.md",
        rubric_path=root / "rubric.json",
        indexer=indexer,
        clock=lambda: FROZEN,
    )
    return worker, root


# ---- completion hook / staging ----------------------------------------------


def test_stage_freezes_every_completed_trial_once(tmp_path):
    worker, root = make_worker(tmp_path)
    report = worker.stage([root / "jobs"])
    assert report.discovered == 3 and report.staged == 3 and report.calls == 0
    again = worker.stage([root / "jobs"])
    assert again.staged == 0  # idempotent: identical identity, no duplicates


def test_harness_exception_is_deferred_never_agent_failure(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    states = {rid: worker.store.transitions(rid)[-1] for rid in worker.store.all_request_ids()}
    exc = [t for t in states.values()
           if t.reason and "harness_exception" in t.reason]
    assert len(exc) == 1
    assert exc[0].state == "deferred"
    assert "not_agent_failure" in exc[0].reason


def test_request_identity_is_frozen_and_reconstructible(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    for rid in worker.store.all_request_ids():
        request = worker.store.load(rid)
        assert request.result_sha256.startswith("sha256:")
        assert request.prompt_sha256.startswith("sha256:")
        assert request.profile_digest == PROFILE.digest
        assert request.identity_digest.startswith("sha256:")
        # transitions reconstruct state from the append-only log alone
        assert worker.store.state(rid) in {"pending", "deferred", "quarantined"}


# ---- the full path with a saved-response adapter -----------------------------


def test_cycle_completes_eligible_trials_with_immutable_sidecars(tmp_path):
    adapter = CountingAdapter()
    calls_indexed: list[Path] = []
    worker, root = make_worker(tmp_path, adapter=adapter,
                               indexer=calls_indexed.append)
    report = worker.run_cycle([root / "jobs"])
    assert report.completed == 2 and adapter.calls == 2  # pass + fail trials
    assert len(calls_indexed) == 2
    for rid in worker.store.all_request_ids():
        if worker.store.state(rid) == "completed":
            sidecar = json.loads(worker.store.sidecar_path(rid).read_text())
            prov = sidecar["analysis_provenance"]
            assert prov["model"] == "gpt-5.6-terra"
            assert prov["cost_usd"] == 0.01
            assert sidecar["output"]["evidence"]
            assert sidecar["source_digests"]["result"].startswith("sha256:")
            assert sidecar["validation_status"] == "valid"


def test_cycle_x3_is_idempotent_no_duplicate_calls_or_sidecars(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    first = worker.run_cycle([root / "jobs"])
    second = worker.run_cycle([root / "jobs"])
    third = worker.run_cycle([root / "jobs"])
    assert first.calls == 2 and adapter.calls == 2
    assert second.calls == 0 and third.calls == 0
    sidecars = list((root / "derived").rglob("analysis.json"))
    assert len(sidecars) == 2


def test_invalid_citations_produce_invalid_sidecar_not_crash(tmp_path):
    worker, root = make_worker(tmp_path, response="saved-response-badcite.json")
    worker.run_cycle([root / "jobs"])
    invalid = [
        json.loads(worker.store.sidecar_path(rid).read_text())
        for rid in worker.store.all_request_ids()
        if worker.store.sidecar_path(rid).is_file()
    ]
    assert invalid and all(s["validation_status"] == "invalid" for s in invalid)
    assert all(s["validation_errors"] for s in invalid)


# ---- admission gates: zero calls each ---------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason_prefix"),
    [
        ({"stop": True}, "queue_stop"),
        ({"probe_ok": False}, "credential:"),
        ({"healthy": False}, "services_unhealthy"),
        ({"est_cost": 5.0}, "cost_ceiling:call"),
        ({"spent": 19.999}, "cost_ceiling:daily"),
        ({"requirements_ok": False}, "policy_requirement_unmet"),
    ],
)
def test_each_gate_defers_with_zero_calls(tmp_path, kwargs, reason_prefix):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter, **kwargs)
    report = worker.run_cycle([root / "jobs"])
    assert adapter.calls == 0 and report.calls == 0
    reasons = [t.reason for rid in worker.store.all_request_ids()
               for t in [worker.store.transitions(rid)[-1]]
               if t.state == "deferred"]
    assert any(r and r.startswith(reason_prefix) for r in reasons), reasons


def test_unqualified_profile_defers_before_any_call(tmp_path):
    unproven = PROFILE.model_copy(update={"verified_facts": ()})
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter, profile=unproven)
    worker.run_cycle([root / "jobs"])
    assert adapter.calls == 0
    reasons = {worker.store.transitions(rid)[-1].reason
               for rid in worker.store.all_request_ids()}
    assert any(r and r.startswith("profile_not_qualified") for r in reasons)


def test_tampered_evidence_quarantines_before_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    victim = root / "jobs" / "job-pass" / "join-trial" / "result.json"
    payload = json.loads(victim.read_text())
    payload["verifier_result"]["rewards"]["reward"] = 0.0  # post-freeze tamper
    victim.write_text(json.dumps(payload, indent=1))
    worker.run_cycle([root / "jobs"])
    assert adapter.calls == 1  # only the untampered eligible trial ran
    quarantined = [worker.store.transitions(rid)[-1]
                   for rid in worker.store.all_request_ids()
                   if worker.store.state(rid) == "quarantined"]
    assert any(t.reason == "evidence_tampered:result.json" for t in quarantined)


def test_missing_evidence_quarantines_before_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    (root / "jobs" / "job-pass" / "join-trial" / "agent" / "trajectory.json").unlink()
    worker.run_cycle([root / "jobs"])
    reasons = {worker.store.transitions(rid)[-1].reason
               for rid in worker.store.all_request_ids()}
    assert "evidence_missing:trajectory.json" in reasons


def test_stale_profile_identity_defers(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    changed = PROFILE.model_copy(update={"model": "gpt-5.6-sol"})
    worker.context = AdmissionContext(
        **{**worker.context.__dict__, "profile": changed}
    )
    worker.run_cycle([root / "jobs"])
    assert adapter.calls == 0
    reasons = {worker.store.transitions(rid)[-1].reason
               for rid in worker.store.all_request_ids()}
    assert any(r and r.startswith("stale_identity") for r in reasons)


# ---- crash safety ------------------------------------------------------------


def test_crash_after_call_before_transition_adopts_sidecar(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    report = worker.run_cycle([root / "jobs"])
    assert report.calls == 2
    # simulate crash-after-sidecar: erase the completed transitions, keep files
    for rid in worker.store.all_request_ids():
        if worker.store.state(rid) != "completed":
            continue
        transitions = worker.store.request_dir(rid) / "transitions.jsonl"
        lines = [ln for ln in transitions.read_text().splitlines()
                 if '"completed"' not in ln]
        transitions.write_text("\n".join(lines) + "\n")
        assert worker.store.state(rid) != "completed"
    recovery = worker.run_cycle([root / "jobs"])
    assert adapter.calls == 2  # ZERO new calls
    assert recovery.adopted == 2
    assert all(worker.store.state(rid) in {"completed", "deferred", "quarantined"}
               for rid in worker.store.all_request_ids())


def test_crash_before_index_retries_indexing_idempotently(tmp_path):
    indexed: list[Path] = []

    def flaky_indexer(path: Path) -> None:
        if not indexed:
            indexed.append(path)
            raise RuntimeError("catalog down")
        indexed.append(path)

    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter, indexer=flaky_indexer)
    with pytest.raises(RuntimeError):
        worker.run_cycle([root / "jobs"])
    calls_after_crash = adapter.calls
    worker.run_cycle([root / "jobs"])  # recovery
    assert adapter.calls == calls_after_crash + 1  # only the un-run trial calls
    completed = [rid for rid in worker.store.all_request_ids()
                 if worker.store.state(rid) == "completed"]
    assert len(completed) == 2


def test_concurrent_worker_lease_prevents_double_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    eligible = [rid for rid in worker.store.all_request_ids()
                if worker.store.state(rid) == "pending"]
    rid = eligible[0]
    held = worker.store.acquire_lease(rid)  # a second worker holds the lease
    assert held is not None
    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "lease_held_by_another_worker"
    assert adapter.calls == 0
    worker.store.release_lease(rid, held)
    assert worker.run_one(rid).state == "completed"
    assert adapter.calls == 1


# ---- rebuild + status --------------------------------------------------------


def test_store_rebuilds_from_files_alone(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.run_cycle([root / "jobs"])
    fresh = RequestStore(root / "derived" / "analyses" / "worker")
    assert fresh.all_request_ids() == worker.store.all_request_ids()
    for rid in fresh.all_request_ids():
        assert fresh.load(rid).request_id == rid
        assert fresh.state(rid) is not None


def test_status_shape_is_m005_compatible(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.run_cycle([root / "jobs"])
    status = worker.status()
    assert status["provenance"] == "observed"
    assert set(status["counts"]) <= {
        "pending", "admitted", "running", "completed", "deferred", "quarantined"
    }
    for row in status["requests"]:
        assert row["provenance"] in {"observed", "derived", "draft", "unavailable"}
        assert row["state"] in {"pending", "admitted", "running", "completed",
                                "deferred", "quarantined"}


def test_plan_is_read_only(tmp_path):
    worker, root = make_worker(tmp_path)
    rows = worker.plan([root / "jobs"])
    assert len(rows) == 3
    assert worker.store.all_request_ids() == []  # plan froze nothing
    assert {r["eligibility"] for r in rows} == {
        "eligible", "defer:harness_exception_not_agent_failure",
    }


def test_source_evidence_bytes_are_never_modified(tmp_path):
    worker, root = make_worker(tmp_path)
    jobs_root = root / "jobs"
    before = {p: p.read_bytes() for p in jobs_root.rglob("*") if p.is_file()}
    worker.run_cycle([jobs_root])
    after = {p: p.read_bytes() for p in jobs_root.rglob("*") if p.is_file()}
    assert before == after


# ============================================================================
# M006 repair regressions (integrator review on PR #47)
# ============================================================================


# ---- (1) real nightly composition stages after successful ingest -------------


def _nightly(tmp_path, *, stager, ingester):

    from evallab.automation import NightlyCycle
    from evallab.digest import DigestRenderer
    from evallab.queue import DirectoryQueue, Executor
    from evallab.schemas import (
        HeadlessDoctorChecks,
        HeadlessDoctorReport,
    )

    class StaticDoctor:
        def run(self):
            checks = HeadlessDoctorChecks(
                keychain_readable=True, codex_auth_present=True,
                docker_reachable=True, postgres_reachable=True,
                disk_headroom=True,
            )
            return HeadlessDoctorReport(
                checked_at=FROZEN, healthy=True, checks=checks
            )

    queue = DirectoryQueue(tmp_path / "queue")
    service = Executor(
        repo_root=tmp_path, queue=queue, policy=POLICY,
        runner=lambda request: request.jobs_dir / request.name,
        ingester=lambda job_dir: None,
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
    )
    renderer = DigestRenderer(
        repo_root=tmp_path, queue=queue, policy=POLICY,
        trial_loader=lambda day: [], drift_loader=lambda day: [],
    )
    cycle = NightlyCycle(
        doctor=StaticDoctor(),  # type: ignore[arg-type]
        executor=service,
        renderer=renderer,
        committer=lambda path: True,
        completed_job_ingester=ingester,
        analysis_stager=stager,
    )
    return cycle, queue


def _ok_ingest(order):
    from evallab.atif import IngestProjectionResult

    def ingest():
        order.append("ingest")
        return IngestProjectionResult(cataloged_jobs=0, tables=(), failures=())

    return ingest


def test_nightly_stages_only_after_successful_ingest(tmp_path):
    order: list[str] = []
    cycle, _queue = _nightly(
        tmp_path,
        ingester=_ok_ingest(order),
        stager=lambda: order.append("stage"),
    )
    from datetime import date as _date

    cycle.run(report_date=_date(2026, 8, 15))
    assert order == ["ingest", "stage"]  # staging strictly after ingest


def test_nightly_skips_staging_when_ingest_fails(tmp_path):
    order: list[str] = []

    def bad_ingester():
        order.append("ingest")
        raise RuntimeError("catalog down")

    cycle, _queue = _nightly(
        tmp_path, ingester=bad_ingester, stager=lambda: order.append("stage"),
    )
    from datetime import date as _date

    cycle.run(report_date=_date(2026, 8, 15))
    assert order == ["ingest"]  # no staging on failed ingest


def test_nightly_staging_failure_emits_durable_event(tmp_path):
    from datetime import date as _date

    from evallab.queue import load_events

    def bad_stager():
        raise RuntimeError("stage exploded")

    order: list[str] = []
    cycle, queue = _nightly(tmp_path, ingester=_ok_ingest(order), stager=bad_stager)
    cycle.run(report_date=_date(2026, 8, 15))
    events = load_events(queue.events_path)
    assert any(
        e.event == "analysis_stage_failed"
        and "RuntimeError" in (e.reason_code or "")
        for e in events
    ), [e.event for e in events]


def test_cli_nightly_stager_composition_stages_real_requests(tmp_path):
    """The exact callable the CLI wires (not a unit seam) stages requests."""
    from evallab.cli import _nightly_analysis_stager

    root = tmp_path / "repo"
    (root / "runs").mkdir(parents=True)
    shutil.copytree(FIXTURES / "jobs" / "job-pass", root / "runs" / "job-pass")
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text(
        "version: 1\ndaily_cost_ceiling_usd: 20\nper_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "auto_run:\n  - name: local-controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - new_task_registration\n"
    )
    (root / "research/analysis").mkdir(parents=True)
    shutil.copy(FIXTURES / "stage5-prompt.md",
                root / "research/analysis/stage5-prompt.md")
    shutil.copy(FIXTURES / "stage5-rubric.json",
                root / "research/analysis/stage5-rubric.json")
    report = _nightly_analysis_stager(root)()
    assert report.staged == 1 and report.calls == 0
    store_dir = root / "derived/analyses/worker/requests"
    assert len(list(store_dir.iterdir())) == 1


# ---- (2) every frozen input reverified before admission ----------------------


def test_prompt_change_defers_before_any_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    (root / "prompt.md").write_text("changed prompt\n")
    worker.run_cycle([root / "jobs"])
    assert adapter.calls == 0
    reasons = {worker.store.transitions(rid)[-1].reason
               for rid in worker.store.all_request_ids()}
    assert "stale_identity:prompt_changed" in reasons


def test_rubric_missing_quarantines_before_any_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    (root / "rubric.json").unlink()
    worker.run_cycle([root / "jobs"])
    assert adapter.calls == 0
    reasons = {worker.store.transitions(rid)[-1].reason
               for rid in worker.store.all_request_ids()}
    assert "evidence_missing:rubric" in reasons


def test_lock_tamper_quarantines_task_verifier_truth(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    lock = root / "jobs" / "job-pass" / "join-trial" / "lock.json"
    lock.write_text('{"task": {"digest": "sha256:' + "e" * 64 + '"}}')
    worker.run_cycle([root / "jobs"])
    quarantined = {worker.store.transitions(rid)[-1].reason
                   for rid in worker.store.all_request_ids()
                   if worker.store.state(rid) == "quarantined"}
    assert "evidence_tampered:lock.json" in quarantined
    assert adapter.calls == 1  # only the untampered eligible trial ran


def test_frozen_request_records_lock_and_task_digests(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    for rid in worker.store.all_request_ids():
        request = worker.store.load(rid)
        assert request.lock_sha256 and request.lock_sha256.startswith("sha256:")
        assert request.task_digest and request.task_digest.startswith("sha256:")
        assert request.verifier_digest


# ---- (3) crash-recoverable lease ownership -----------------------------------


def _write_lease(worker, rid, *, pid, age_seconds=0.0):
    from datetime import timedelta

    path = worker.store.request_dir(rid) / "lease"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "acquired_at": (FROZEN - timedelta(seconds=age_seconds)).isoformat(),
               "host": "testhost"}
    path.write_text(json.dumps(payload))


def _pending(worker):
    return [rid for rid in worker.store.all_request_ids()
            if worker.store.state(rid) == "pending"]


def test_dead_owner_lease_is_reclaimed_before_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    _write_lease(worker, rid, pid=99999999)  # no such process
    transition = worker.run_one(rid)
    assert transition.state == "completed"
    assert adapter.calls == 1  # reclaimed and ran exactly once


def test_live_owner_lease_is_never_reclaimed_even_when_old(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    held = worker.store.acquire_lease(rid, owner_token="live-owner")
    assert held is not None
    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "lease_held_by_another_worker"
    assert adapter.calls == 0
    worker.store.release_lease(rid, held)


def test_corrupt_lease_is_reclaimed(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    path = worker.store.request_dir(rid) / "lease"
    path.write_text("{not json")
    assert worker.run_one(rid).state == "completed"
    assert adapter.calls == 1


def test_crash_during_call_dead_lease_plus_sidecar_adopts_without_second_call(tmp_path):
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    worker.run_one(rid)  # produces the durable sidecar
    assert adapter.calls == 1
    # simulate: process died mid-completion — lease left by a dead pid, and
    # the completed transition never landed
    transitions = worker.store.request_dir(rid) / "transitions.jsonl"
    lines = [ln for ln in transitions.read_text().splitlines()
             if '"completed"' not in ln]
    transitions.write_text("\n".join(lines) + "\n")
    _write_lease(worker, rid, pid=99999999)
    transition = worker.run_one(rid)
    assert transition.state == "completed"
    assert transition.reason == "adopted_existing_sidecar"
    assert adapter.calls == 1  # ZERO additional calls


# ---- (4) default composition indexes through the catalog ---------------------


def test_default_worker_has_catalog_indexer(monkeypatch, tmp_path):
    from evallab import analysis_worker as aw

    root = tmp_path / "repo"
    (root / "policy").mkdir(parents=True)
    (root / "policy/standing-approvals.yaml").write_text(
        "version: 1\ndaily_cost_ceiling_usd: 20\nper_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "auto_run:\n  - name: local-controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - new_task_registration\n"
    )
    worker = aw.default_worker(root)
    assert worker.indexer is not None

    indexed: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "evallab.database.initialize", lambda url: indexed.append(("init", Path(url)))
    )
    monkeypatch.setattr(
        aw, "ingest_analysis_sidecar",
        lambda url, path, root: indexed.append(("ingest", path)),
    )
    sidecar = tmp_path / "analysis.json"
    sidecar.write_text("{}")
    worker.indexer(sidecar)
    assert [k for k, _ in indexed] == ["init", "ingest"]
    assert indexed[1][1] == sidecar


def test_default_indexer_failure_leaves_sidecar_adoptable(monkeypatch, tmp_path):

    adapter = CountingAdapter()

    def failing_indexer(path: Path) -> None:
        raise RuntimeError("catalog offline")

    worker, root = make_worker(tmp_path, adapter=adapter, indexer=failing_indexer)
    with pytest.raises(RuntimeError):
        worker.run_cycle([root / "jobs"])
    # sidecar durable, state not completed -> retryable
    stuck = [rid for rid in worker.store.all_request_ids()
             if worker.store.sidecar_path(rid).is_file()
             and worker.store.state(rid) != "completed"]
    assert stuck
    worker.indexer = None  # catalog comes back (no-op indexer for the test)
    worker.run_cycle([root / "jobs"])
    assert worker.store.state(stuck[0]) == "completed"
    assert adapter.calls <= 2  # adoption, never a re-call for the stuck one


# ============================================================================
# M006 second repair regressions (integrator re-review on PR #47)
# ============================================================================


def test_call_returned_without_sidecar_is_ambiguous_and_never_replayed(
    monkeypatch, tmp_path
):
    """A provider may have charged even when sidecar construction then crashes."""
    from evallab import analysis_worker as aw

    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]

    def returned_then_crashed(job, trial, *, analyzer, **_kwargs):
        analyzer("provider received request", {})
        raise RuntimeError("crash after provider return, before sidecar")

    monkeypatch.setattr(aw, "run_trial_analysis", returned_then_crashed)
    with pytest.raises(RuntimeError, match="before sidecar"):
        worker.run_one(rid)
    assert adapter.calls == 1
    assert not worker.store.sidecar_path(rid).exists()

    transition = worker.run_one(rid)
    assert adapter.calls == 1  # an ambiguous possibly-paid call is NEVER replayed
    assert transition.state == "deferred"
    assert transition.reason == "ambiguous_invocation_requires_operator_resolution"


def test_ambiguous_invocation_requires_explicit_operator_retry(monkeypatch, tmp_path):
    from evallab import analysis_worker as aw

    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    real_run = aw.run_trial_analysis

    def returned_then_crashed(job, trial, *, analyzer, **_kwargs):
        analyzer("provider received request", {})
        raise RuntimeError("lost response")

    monkeypatch.setattr(aw, "run_trial_analysis", returned_then_crashed)
    with pytest.raises(RuntimeError, match="lost response"):
        worker.run_one(rid)
    assert worker.run_one(rid).reason == (
        "ambiguous_invocation_requires_operator_resolution"
    )

    transition = worker.resolve_ambiguous(rid, action="retry", actor="operator-test")
    assert transition.state == "pending"
    assert transition.reason == "operator_retry_authorized:operator-test"
    monkeypatch.setattr(aw, "run_trial_analysis", real_run)
    assert worker.run_one(rid).state == "completed"
    assert adapter.calls == 2  # only the explicit resolution permits another call


def test_operator_can_quarantine_ambiguity_without_retry(monkeypatch, tmp_path):
    from evallab import analysis_worker as aw

    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]

    def returned_then_crashed(job, trial, *, analyzer, **_kwargs):
        analyzer("provider received request", {})
        raise RuntimeError("lost response")

    monkeypatch.setattr(aw, "run_trial_analysis", returned_then_crashed)
    with pytest.raises(RuntimeError, match="lost response"):
        worker.run_one(rid)
    transition = worker.resolve_ambiguous(
        rid, action="quarantine", actor="operator-test"
    )
    assert transition.state == "quarantined"
    assert adapter.calls == 1
    assert worker.run_one(rid).state == "quarantined"
    assert adapter.calls == 1


def test_operator_cannot_resolve_while_original_owner_is_live(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    held = worker.store.acquire_lease(rid, owner_token="live-call")
    assert held is not None
    worker.store.begin_invocation(rid, owner_token=held.owner_token, at=FROZEN)

    with pytest.raises(RuntimeError, match="another worker is live"):
        worker.resolve_ambiguous(rid, action="retry", actor="operator-test")
    assert worker.store.unresolved_invocation(rid) is not None
    worker.store.release_lease(rid, held)


def test_cli_requires_explicit_actor_and_action_for_ambiguous_resolution():
    from evallab.cli import parser

    args = parser().parse_args(
        [
            "analyze",
            "worker-resolve-ambiguous",
            "deadbeefdeadbeef",
            "--action",
            "quarantine",
            "--actor",
            "operator-test",
        ]
    )
    assert args.action == "quarantine"
    assert args.actor == "operator-test"


def test_kernel_lease_allows_only_one_live_owner(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]

    first = worker.store.acquire_lease(rid, owner_token="owner-a")
    assert first is not None
    assert worker.store.acquire_lease(rid, owner_token="owner-b") is None
    worker.store.release_lease(rid, first)


def test_release_never_deletes_a_replacement_owners_lease(tmp_path):
    """Deterministically simulate replacement between ownership check/release."""
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    path = worker.store.request_dir(rid) / "lease"

    old = worker.store.acquire_lease(rid, owner_token="old-owner")
    assert old is not None
    path.unlink()  # simulate another implementation replacing the path inode
    replacement = worker.store.acquire_lease(rid, owner_token="new-live-owner")
    assert replacement is not None

    assert worker.store.release_lease(rid, old) is False
    assert json.loads(path.read_text())["owner_token"] == "new-live-owner"
    assert worker.store.acquire_lease(rid, owner_token="third-owner") is None
    worker.store.release_lease(rid, replacement)


def test_two_stale_reclaimers_cannot_both_acquire(tmp_path):
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    path = worker.store.request_dir(rid) / "lease"
    path.write_text(json.dumps({"owner_token": "dead", "pid": 99999999}))

    winner = worker.store.acquire_lease(rid, owner_token="winner")
    assert winner is not None
    assert worker.store.acquire_lease(rid, owner_token="loser") is None
    assert json.loads(path.read_text())["owner_token"] == "winner"
    worker.store.release_lease(rid, winner)


def test_real_nightly_stager_persists_bounded_returned_quarantine(tmp_path):
    """A normal stage report with freeze-time failures must not disappear."""
    from datetime import date as _date

    from evallab.cli import _nightly_analysis_stager
    from evallab.queue import load_events

    root = tmp_path / "repo"
    (root / "runs").mkdir(parents=True)
    shutil.copytree(FIXTURES / "jobs" / "job-pass", root / "runs" / "job-pass")
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text(
        "version: 1\ndaily_cost_ceiling_usd: 20\nper_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "auto_run:\n  - name: local-controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - new_task_registration\n"
    )
    (root / "research/analysis").mkdir(parents=True)
    shutil.copy(
        FIXTURES / "stage5-rubric.json",
        root / "research/analysis/stage5-rubric.json",
    )
    # Intentionally omit stage5-prompt.md: stage() returns a quarantine count.
    order: list[str] = []
    cycle, queue = _nightly(
        root,
        ingester=_ok_ingest(order),
        stager=_nightly_analysis_stager(root),
    )
    cycle.run(report_date=_date(2026, 8, 15))

    events = load_events(queue.events_path)
    event = next(e for e in events if e.event == "analysis_stage_reported_issues")
    assert event.reason_code == (
        "analysis_stage_reported_issues:quarantined=1;errors=0;"
        "reasons=evidence_unreadable=1"
    )
    assert len(event.reason_code) <= 512



# ============================================================================
# M006 third repair regressions (independent exact-head review of 1f4cf6f)
# ============================================================================


def _record_fsyncs(monkeypatch) -> list[tuple[int, int]]:
    """Record (device, inode) of every fsynced fd; a dirent needs its parent."""
    seen: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def recording(fd: int) -> None:
        stat = os.fstat(fd)
        seen.append((stat.st_dev, stat.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)
    return seen


def _ident(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino)


def test_frozen_request_dirents_are_durable(monkeypatch, tmp_path):
    """request.json is the recovery root: bytes AND dirents must be durable."""
    worker, root = make_worker(tmp_path)
    seen = _record_fsyncs(monkeypatch)
    worker.stage([root / "jobs"])

    rid = _pending(worker)[0]
    request_dir = worker.store.request_dir(rid)
    assert _ident(request_dir / "request.json") in seen  # the bytes
    assert _ident(request_dir) in seen  # the request.json dirent
    assert _ident(request_dir.parent) in seen  # the request directory dirent


def test_invocation_journal_dirent_is_durable(monkeypatch, tmp_path):
    """A journal whose dirent is lost lets a crash re-issue a paid call."""
    worker, root = make_worker(tmp_path)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    request_dir = worker.store.request_dir(rid)

    seen = _record_fsyncs(monkeypatch)
    worker.store.begin_invocation(rid, owner_token="owner-a", at=FROZEN)
    assert _ident(request_dir / "invocations.jsonl") in seen  # the bytes
    assert _ident(request_dir) in seen  # the newly created journal dirent


def _record_durability_events(monkeypatch) -> list[tuple[str, tuple[int, int]]]:
    """Record mkdir and fsync calls in issue order, keyed by (device, inode).

    Dirent durability is an ordering property, not just a set membership one:
    the fsync that persists a name must happen after the name exists.
    """
    events: list[tuple[str, tuple[int, int]]] = []
    real_fsync = os.fsync
    real_mkdir = Path.mkdir

    def recording_fsync(fd: int) -> None:
        stat = os.fstat(fd)
        events.append(("fsync", (stat.st_dev, stat.st_ino)))
        real_fsync(fd)

    def recording_mkdir(self: Path, *args, **kwargs) -> None:
        real_mkdir(self, *args, **kwargs)
        events.append(("mkdir", _ident(self)))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(Path, "mkdir", recording_mkdir)
    return events


def test_sidecar_directory_dirent_is_durable(monkeypatch, tmp_path):
    """The name proving a paid result exists must outlive a host crash.

    ``_durable_replace`` fsyncs the sidecar bytes and ``sidecar/`` itself, which
    persists ``analysis.json`` *within* ``sidecar/`` — not the ``sidecar/`` entry
    in the request directory. The only request-directory fsync otherwise on this
    path is the journal's ``O_CREAT`` branch inside ``begin_invocation``, which
    runs strictly before ``sidecar/`` exists. Lose that dirent after a durably
    resolved invocation and both recovery guards go quiet —
    ``unresolved_invocation`` is None and ``sidecar_path.is_file()`` is False —
    so ``run_one`` re-admits and issues a second billable provider call.

    fixture-proven only: this asserts fsync syscalls by (device, inode); it does
    not stage a real host crash.
    """
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    request_dir = worker.store.request_dir(rid)
    sidecar_dir = worker.store.sidecar_path(rid).parent

    events = _record_durability_events(monkeypatch)
    assert worker.run_one(rid).state == "completed"
    assert adapter.calls == 1

    sidecar_dir_id = _ident(sidecar_dir)
    created = [
        index for index, (kind, ident) in enumerate(events)
        if kind == "mkdir" and ident == sidecar_dir_id
    ]
    assert created, "run_one must create the per-request sidecar directory"
    request_dir_id = _ident(request_dir)
    assert any(
        kind == "fsync" and ident == request_dir_id
        for kind, ident in events[created[0] + 1:]
    ), "the sidecar/ dirent is never fsynced into the request directory"


def test_unwired_adapter_defers_without_arming_the_ambiguity_journal(tmp_path):
    """A locally provable misconfiguration is never a possibly-paid call."""
    from evallab import analysis_worker as aw

    worker, root = make_worker(tmp_path, adapter=aw._no_adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]

    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "adapter_not_wired"
    # No journal entry, so no operator ceremony is required to move on.
    assert worker.store.invocation_events(rid) == []
    assert worker.store.unresolved_invocation(rid) is None
    # And it stays retryable: wiring an adapter is all it takes.
    adapter = CountingAdapter()
    worker.adapter = adapter
    assert worker.run_one(rid).state == "completed"
    assert adapter.calls == 1


def test_run_one_never_reruns_a_permanent_evidence_deferral(tmp_path):
    """run_one is the only production entrypoint; permanence must hold there."""
    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    permanent = [
        rid for rid in worker.store.all_request_ids()
        if (worker.store.transitions(rid)[-1].reason or "").startswith(
            "harness_exception"
        )
    ]
    assert permanent, "fixture must contain a harness-exception trial"
    rid = permanent[0]

    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "harness_exception_not_agent_failure"
    assert adapter.calls == 0  # a harness failure is never an agent analysis
    assert not worker.store.sidecar_path(rid).exists()
    assert worker.store.invocation_events(rid) == []


def _fail_closed_root(tmp_path: Path) -> Path:
    """A repository root shaped exactly like the one the CLI composes from."""
    root = tmp_path / "repo"
    (root / "runs").mkdir(parents=True)
    shutil.copytree(FIXTURES / "jobs" / "job-pass", root / "runs" / "job-pass")
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text(
        "version: 1\ndaily_cost_ceiling_usd: 20\nper_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "auto_run:\n  - name: local-controls\n    agents: [oracle, nop]\n"
        "  - name: researcher-followups\n    agents: [codex, claude-code]\n"
        "    max_attempts: 5\n"
        "    requires: [schema_valid, dedup_pass, calibrated_judges_only]\n"
        "escalate_to_human:\n  - new_task_registration\n"
    )
    (root / "research/analysis").mkdir(parents=True)
    shutil.copy(FIXTURES / "stage5-prompt.md", root / "research/analysis/stage5-prompt.md")
    shutil.copy(
        FIXTURES / "stage5-rubric.json", root / "research/analysis/stage5-rubric.json"
    )
    return root


def test_default_worker_stays_fail_closed_for_live_analysis(tmp_path):
    """The two defaults that keep this PR from enabling unattended live calls.

    Opening the calibration gate or defaulting the adapter to a live one must
    be a deliberate, reviewed change — not a quiet edit that CI accepts.
    """
    from evallab import analysis_worker as aw

    root = _fail_closed_root(tmp_path)
    worker = aw.default_worker(root)

    assert worker.adapter is aw._no_adapter
    with pytest.raises(RuntimeError, match="no analysis adapter is wired"):
        worker.adapter("prompt", {})
    assert worker.context.requirement_checks["calibrated_judges_only"]() is False

    # The closed gate is enforced, not merely declared: a fully eligible trial
    # defers on the policy requirement with zero calls and no armed journal.
    worker.stage([root / "runs"])
    rid = _pending(worker)[0]
    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "policy_requirement_unmet:calibrated_judges_only"
    assert worker.store.invocation_events(rid) == []
    assert not worker.store.sidecar_path(rid).exists()

    # An adapter supplied explicitly by a caller is still honoured.
    assert aw.default_worker(root, adapter=CountingAdapter()).adapter is not aw._no_adapter


def test_lease_replacement_during_execution_is_durably_recorded(monkeypatch, tmp_path):
    """A false release result means flock stopped serializing the request."""
    from evallab import analysis_worker as aw

    adapter = CountingAdapter()
    worker, root = make_worker(tmp_path, adapter=adapter)
    worker.stage([root / "jobs"])
    rid = _pending(worker)[0]
    replacements: list[object] = []

    def steal_the_lease(job, trial, *, analyzer, **_kwargs):
        (worker.store.request_dir(rid) / "lease").unlink()
        replacements.append(worker.store.acquire_lease(rid, owner_token="replacement"))
        raise RuntimeError("crashed while a replacement owner held the lease")

    monkeypatch.setattr(aw, "run_trial_analysis", steal_the_lease)
    with pytest.raises(RuntimeError, match="replacement owner"):
        worker.run_one(rid)

    events = worker.store.invocation_events(rid)
    assert [e["event"] for e in events] == [
        "invocation_started",
        "lease_replaced_during_execution",
    ]
    # The note is an audit record only: state stays where the machine left it.
    assert worker.store.state(rid) == "running"
    assert worker.store.unresolved_invocation(rid) is not None
    assert replacements[0] is not None
    worker.store.release_lease(rid, replacements[0])