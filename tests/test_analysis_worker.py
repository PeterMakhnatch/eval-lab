"""Analysis-worker contract tests (M006). Deterministic: injected adapter,
policy, probes, clock, health, spend — zero real credentials, DB, or model.
"""

from __future__ import annotations

import json
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
    assert worker.store.acquire_lease(rid)  # a second worker holds the lease
    transition = worker.run_one(rid)
    assert transition.state == "deferred"
    assert transition.reason == "lease_held_by_another_worker"
    assert adapter.calls == 0
    worker.store.release_lease(rid)
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
