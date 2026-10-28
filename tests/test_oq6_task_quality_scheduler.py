"""Filesystem scheduling proofs with synthetic helpers; no corpus, key, or native calls."""
from __future__ import annotations

import copy
import fcntl
import importlib.util
import sys
import types
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "research/experiments/oq6-task-quality-gate-rerun/runner.py"
spec = importlib.util.spec_from_file_location("oq6_task_quality_scheduler", RUNNER_PATH)
assert spec and spec.loader
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)
SONNET, OPUS, KIMI = scheduler.ROUTES
FREEZE, PLAN = "f" * 64, "a" * 64


class Clock:
    def __init__(self, now=1000.0):
        self.now = now
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        assert delay > 0
        self.waits.append(delay)
        self.now += delay


def case(case_id="X1", *, stage="cross", keys=1, priority=0, prepared=True):
    return {"case_id": case_id, "task_id": "synthetic/" + case_id, "stage": stage,
            "key_count": keys, "scheduling_priority": priority,
            "preparation_status": "prepared" if prepared else "unavailable",
            "preparation_error": None if prepared else "frozen source unavailable",
            "source_sha256": "b" * 64, "pinned_native_package_digest": "c" * 64}


def evaluate_cell(case, payloads, judgment, keys, *, audit_status="answered", raw_findings_count=None):
    """Mock only the scientific evaluator, never the scheduler boundary."""
    complete = (audit_status == "answered" and payloads is not None and
                (not payloads or judgment is not None and judgment["status"] == "complete"))
    reason = None if complete else ("union_judgment_missing" if audit_status == "answered" and payloads else "audit_not_answered")
    return {"case_id": case["case_id"], "key_count": case["key_count"], "complete": complete,
            "audit_status": audit_status, "raw_findings": raw_findings_count,
            "parsed_findings": len(payloads) if payloads is not None else None,
            "missing_reason": reason, "gates": {"old_0_8": None, "new_verified_0_5": None}}


class Native:
    OWNED_ARTIFACTS = ("call-consumed.json", "prompt.txt", "response.txt", "sessions")

    def __init__(self, clock):
        self.clock = clock
        self.sent, self.reread = [], []
        self.status = {}
        self.findings = []
        self.on_send = None

    def call(self, directory, *, route, **kwargs):
        directory = Path(directory)
        receipt_path = directory / "receipt.json"
        if receipt_path.exists():
            old = scheduler.read_json(receipt_path)
            assert old["requested_route"] == route
            assert old["timeout_seconds"] == kwargs["timeout_seconds"]
            assert old["prompt_sha256"] == scheduler.sha(kwargs["prompt"].encode())
            assert old["config_sha256"] == kwargs["config_sha256"]
            self.reread.append((directory, copy.deepcopy(kwargs)))
            return old
        assert not (directory / "call-consumed.json").exists(), "a consumed attempt was reissued"
        phase = directory.name
        self.sent.append({"directory": directory, "route": route, "at": self.clock(), **kwargs})
        if self.on_send is not None:
            self.on_send(directory, kwargs)
        directory.mkdir(parents=True, exist_ok=True)
        status = self.status.get(phase, "answered")
        receipt = {"schema": "synthetic.native/v1", "status": status, "attempt_consumed": True,
                   "requested_route": route, "timeout_seconds": kwargs["timeout_seconds"],
                   "prompt_sha256": scheduler.sha(kwargs["prompt"].encode()),
                   "config_sha256": kwargs["config_sha256"], "served_identities": [route],
                   "attestation_source_sha256": "d" * 64,
                   "native_prompt_matches_frozen_bytes": True,
                   "native_response_matches_stdout": True, "isolation_valid": True,
                   "response_sha256": "e" * 64 if status == "answered" else None,
                   "receipt_path": str(receipt_path)}
        if status == "answered":
            receipt["answer"] = {"findings": self.findings} if phase == "audit" else {"judgments": []}
        else:
            receipt["error"] = "sent refusal/error/malformed/timeout; no retry"
        scheduler.write_json(directory / "call-consumed.json", {
            "timeout_seconds": kwargs["timeout_seconds"], "requested_route": route}, immutable=True)
        (directory / "prompt.txt").write_text(kwargs["prompt"])
        (directory / "response.txt").write_text("synthetic attested bytes")
        scheduler.write_json(receipt_path, receipt, immutable=True)
        return receipt


class Quota:
    def __init__(self, lab):
        self.lab, self.calls = lab, []
        self.decide = lambda route, count: {"eligible": True}
        self.on_admit = None

    def admit_route(self, route, *, output_directory):
        # Actual flock ownership is exercised, not an always-successful lock mock.
        provider = route.split("/", 1)[0]
        path = self.lab.root / "locks" / ("provider-" + provider + ".lock")
        with path.open("a") as competing, pytest.raises(BlockingIOError):
            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.calls.append((route, self.lab.clock()))
        value = {"route": route, "pool_values": [], "reasons": [],
                 "fetchedAt": self.lab.clock() * 1000,
                 **self.decide(route, len(self.calls))}
        receipt = Path(output_directory) / ("admission-" + str(len(self.calls))) / "receipt.json"
        value["receipt_path"] = str(receipt)
        scheduler.write_json(receipt, value, immutable=True)
        if self.on_admit:
            self.on_admit()
        return value


class Dispatcher:
    """Mock execute_cell's persisted audit -> prediction -> scorer boundaries."""
    def __init__(self, lab):
        self.lab, self.parses, self.calls = lab, 0, []

    def __call__(self, case, route, arm, protocol, freeze_sha, *, quota_call):
        self.calls.append((case["case_id"], route, arm))
        directory = self.lab.root / "calls" / route.replace("/", "--") / arm / case["stage"] / case["case_id"]
        final = directory / "cell.json"
        if final.exists():
            return scheduler.read_json(final)
        if case["preparation_status"] != "prepared":
            audit = {"status": "unavailable_input", "attempt_consumed": False,
                     "reason": case["preparation_error"]}
            prediction = {"parsed_payloads": None, "raw_findings_count": None}
        else:
            audit = quota_call(directory / "audit", route=route, prompt="frozen auditor " + arm,
                               system="frozen auditor system", expected_array="findings",
                               config=Path("synthetic-config"), config_sha256="config-hash")
            prediction_path = directory / "prediction.json"
            if prediction_path.exists():
                prediction = scheduler.read_json(prediction_path)
            else:
                self.parses += 1
                payloads = copy.deepcopy(audit["answer"]["findings"]) if audit["status"] == "answered" else None
                prediction = {"source_sha256": case["source_sha256"],
                              "audit_receipt_sha256": scheduler.sha((directory / "audit/receipt.json").read_bytes()),
                              "audit_status": audit["status"], "parsed_payloads": payloads,
                              "raw_findings_count": len(payloads) if payloads is not None else None}
                scheduler.write_json(prediction_path, prediction, immutable=True)
        judgment = None
        if prediction["parsed_payloads"]:
            receipt = quota_call(directory / "scorer", route=SONNET, prompt="frozen union scorer",
                                 system="frozen scorer system", expected_array="judgments",
                                 config=Path("synthetic-config"), config_sha256="config-hash", timeout_seconds=600)
            judgment = {"status": "complete" if receipt["status"] == "answered" else "unanswered",
                        "transport_receipt": receipt}
        paired = evaluate_cell(case, prediction["parsed_payloads"], judgment, None,
                               audit_status=audit["status"], raw_findings_count=prediction["raw_findings_count"])
        result = {"route": route, "arm": arm, "case_id": case["case_id"], "stage": case["stage"],
                  "freeze_sha256": freeze_sha, "audit": audit, "prediction": prediction,
                  "scorer": judgment, "paired": paired}
        scheduler.write_json(final, result, immutable=True)
        return result


class Lab:
    def __init__(self, tmp_path, *, cases=None, routes=(SONNET,), route=SONNET, arm="A",
                 all_active=False, cutoff=2000.0, disabled=None):
        self.root, self.science_root = tmp_path / "v3", tmp_path / "v2"
        self.clock, self.verified = Clock(), []
        cases = cases if cases is not None else [case()]
        self.protocol = {"models": list(routes), "code_files": [], "pre_cross_data_files": [],
                         "execution": {"raw_root": str(self.root), "science_root": str(self.science_root),
                                       "plan_path": str(tmp_path / "plan.json"), "cutoff_at": scheduler.utc(cutoff),
                                       "quota_recheck_seconds": 60, "priority_wait_seconds": 5,
                                       "disabled_routes": disabled or {}}}
        self.plan = {"cases": cases, "workers": [{"route": model, "arm": arm,
                     "case_ids": [row["case_id"] for row in cases]} for model in routes for arm in ("A", "B")]}
        self.states = scheduler.initial_states(self.protocol, self.plan, FREEZE, PLAN, now=self.clock())
        for key, state in self.states.items():
            scheduler.write_json(self.root / "pending" / (key + ".json"), state)
        if not all_active:
            for model in routes:
                for other_arm in ("A", "B"):
                    if (model, other_arm) != (route, arm):
                        self.cursor(model, other_arm, len(cases))
        self.native, self.quota = Native(self.clock), Quota(self)
        self.dispatcher = Dispatcher(self)
        self.helpers = types.SimpleNamespace(
            run_slice=types.SimpleNamespace(ROOT=self.science_root), transport=self.native,
            quota=self.quota, paired=types.SimpleNamespace(evaluate_cell=evaluate_cell),
            gold=types.SimpleNamespace(ROOT=self.science_root))
        self.worker = self.make_worker(route, arm)

    def make_worker(self, route=SONNET, arm="A", **overrides):
        kwargs = {"verifier": lambda: self.verified.append(self.clock()), "clock": self.clock,
                  "sleeper": self.clock.sleep, "science_loader": lambda protocol: self.helpers,
                  "dispatcher": self.dispatcher, **overrides}
        return scheduler.Worker(self.protocol, self.plan, route, arm, FREEZE, PLAN, **kwargs)

    def cursor(self, route, arm, index, **fields):
        path = self.root / "pending" / (scheduler.worker_key(route, arm) + ".json")
        state = scheduler.read_json(path)
        ids = next(row["case_ids"] for row in self.plan["workers"] if (row["route"], row["arm"]) == (route, arm))
        row = self.plan["cases"][index] if index < len(ids) else None
        state.update(next_index=index, next_case_id=row["case_id"] if row else None,
                     next_priority=row["scheduling_priority"] if row else None,
                     status="pending" if row else "finished", blocked_until=None,
                     blocked_route=None, quota_admission_path=None, wait_basis=None)
        state.update(fields)
        scheduler.write_json(path, state)
        return state

    def outcome(self, case_id="X1", *, route=SONNET, arm="A"):
        return scheduler.read_json(self.root / "outcomes" / scheduler.worker_key(route, arm) / (case_id + ".json"))


def blocked(reset=1100.0, *, used=0.99):
    return {"eligible": False, "reasons": [{"code": "quota_threshold", "pool": "subscription-5h"}],
            "pool_values": [{"pool": "subscription-5h", "used_fraction": used,
                             "status": "exhausted", "resets_at_ms": reset * 1000}]}


def footprint(path):
    return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def test_import_never_reads_private_data_or_starts_native_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("import touched filesystem data")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    fresh_spec = importlib.util.spec_from_file_location("isolated_oq6_scheduler_import", RUNNER_PATH)
    module = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(module)
    assert module.Worker and module.QuotaPending


def test_initialization_schema_and_not_started_workers_are_required(tmp_path):
    lab = Lab(tmp_path, all_active=True)
    state = lab.states[scheduler.worker_key(SONNET, "A")]
    assert state == {"schema": "oq6.scheduler-worker/v1", "route": SONNET, "arm": "A",
                     "freeze_sha256": FREEZE, "plan_sha256": PLAN, "next_index": 0,
                     "next_case_id": "X1", "next_priority": 0, "status": "pending",
                     "blocked_until": None, "blocked_route": None, "quota_admission_path": None,
                     "wait_basis": None, "updated_at": scheduler.utc(1000)}
    (lab.root / "pending" / (scheduler.worker_key(SONNET, "B") + ".json")).unlink()
    with pytest.raises(ValueError, match="preinitialized"):
        lab.make_worker()


def test_pending_reset_requires_fresh_admission_then_exactly_one_request(tmp_path):
    lab = Lab(tmp_path)
    lab.quota.decide = lambda route, count: blocked() if count == 1 else {"eligible": True}
    state = lab.worker.step()
    assert (state["next_case_id"], state["status"], state["blocked_until"], state["wait_basis"]) == (
        "X1", "quota_pending", scheduler.utc(1100), "provider_reset")
    assert Path(state["quota_admission_path"]).is_file()
    assert not lab.native.sent
    assert not (lab.root / "calls" / SONNET.replace("/", "--") / "A/cross/X1/cell.json").exists()
    assert lab.worker.snapshot()["pending_cells"] == 1
    assert lab.worker.step()["status"] == "quota_pending"
    assert len(lab.quota.calls) == 1  # no metadata spin while waiting
    summary = lab.worker.run()
    assert lab.clock.waits == [100]
    assert len(lab.quota.calls) == 2
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["at"] == 1100
    assert summary["status"] == "complete" and summary["attempted_calls"] == 1
    assert any(at == 1100 for at in lab.verified)
    assert len(list((lab.root / "quota-admissions").rglob("receipt.json"))) == 2


def test_reset_timestamp_never_assumes_provider_replenishment(tmp_path):
    lab = Lab(tmp_path)
    lab.quota.decide = lambda route, count: blocked(1100 if count == 1 else 1200)
    lab.worker.step()
    lab.clock.now = 1100
    state = lab.worker.step()
    assert state["status"] == "quota_pending" and state["blocked_until"] == scheduler.utc(1200)
    assert len(lab.quota.calls) == 2 and not lab.native.sent
    assert not (lab.root / "outcomes").exists()


def test_missing_reset_uses_conservative_recheck_not_busy_spin(tmp_path):
    lab = Lab(tmp_path)
    lab.quota.decide = lambda route, count: {"eligible": False, "reasons": [{"code": "metadata_unavailable"}]}
    state = lab.worker.step()
    assert state["blocked_until"] == scheduler.utc(1060) and state["wait_basis"] == "quota_recheck"
    for _ in range(3):
        lab.worker.step()
    assert len(lab.quota.calls) == 1


def test_every_blocking_window_must_reset_before_recheck(tmp_path):
    lab = Lab(tmp_path)
    value = blocked(1100)
    value["pool_values"].append({"pool": "weekly", "used_fraction": 0.9,
                                 "status": "warning", "resets_at_ms": 1300000})
    lab.quota.decide = lambda route, count: value
    assert lab.worker.step()["blocked_until"] == scheduler.utc(1300)
    value["pool_values"][1]["resets_at_ms"] = None
    lab.clock.now = 1300
    state = lab.worker.step()
    assert state["wait_basis"] == "quota_recheck" and state["blocked_until"] == scheduler.utc(1360)


def test_scorer_pending_resumes_consumed_audit_without_reparse_or_retry(tmp_path):
    lab = Lab(tmp_path, cutoff=1250)
    lab.native.findings = [{"candidate_id": "C1", "mechanism": "synthetic defensive finding"}]
    lab.quota.decide = lambda route, count: blocked() if count == 2 else {"eligible": True}
    state = lab.worker.step()
    assert state["status"] == "quota_pending" and state["blocked_route"] == SONNET
    cell = lab.root / "calls" / SONNET.replace("/", "--") / "A/cross/X1"
    frozen_audit = footprint(cell / "audit")
    frozen_prediction = (cell / "prediction.json").read_bytes()
    assert not (cell / "scorer").exists() and not (cell / "cell.json").exists()
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["timeout_seconds"] == 250
    lab.clock.now = 1100
    resumed = lab.make_worker()  # process restart, same persisted cell/clock
    summary = resumed.run()
    assert [row["directory"].name for row in lab.native.sent] == ["audit", "scorer"]
    assert len(lab.native.reread) == 1 and lab.native.reread[0][1]["timeout_seconds"] == 250
    assert lab.native.sent[1]["timeout_seconds"] == 150
    assert lab.dispatcher.parses == 1
    assert footprint(cell / "audit") == frozen_audit
    assert (cell / "prediction.json").read_bytes() == frozen_prediction
    outcome = lab.outcome()
    assert outcome["audit"] == scheduler.read_json(cell / "audit/receipt.json")
    assert outcome["audit"]["attestation_source_sha256"] == "d" * 64
    assert summary["attempted_calls"] == summary["answered_calls"] == 2
    assert lab.root == lab.helpers.run_slice.ROOT
    assert lab.science_root == lab.helpers.gold.ROOT


@pytest.mark.parametrize("status", ["unanswered", "refusal", "malformed"])
def test_sent_failures_are_terminal_missing_not_pending_or_retried(tmp_path, status):
    lab = Lab(tmp_path)
    lab.native.status["audit"] = status
    assert lab.worker.step()["status"] == "finished"
    outcome = lab.outcome()
    assert outcome["status"] == "missing" and outcome["paired"]["parsed_findings"] is None
    assert outcome["audit"]["attempt_consumed"] is True
    lab.worker.step()
    lab.make_worker().run()
    assert len(lab.native.sent) == len(lab.quota.calls) == 1
    summary = lab.worker.snapshot()
    assert (summary["status"], summary["attempted_cells"], summary["answered_cells"], summary["missing_cells"]) == (
        "checkpoint_partial", 1, 0, 1)


def test_sent_scorer_error_is_not_retried(tmp_path):
    lab = Lab(tmp_path)
    lab.native.findings = [{"candidate_id": "C1"}]
    lab.native.status["scorer"] = "unanswered"
    lab.worker.run()
    lab.make_worker().run()
    assert len(lab.native.sent) == 2 and len(lab.quota.calls) == 2
    assert lab.outcome()["status"] == "missing"
    assert lab.worker.snapshot()["answered_cells"] == 1


def test_strict_route_cohort_case_arm_order_and_unkeyed_last(tmp_path):
    cases = [case("X1"), case("X2", priority=1), case("D1", stage="dev", priority=2),
             case("U1", stage="dev", keys=0, priority=3)]
    lab = Lab(tmp_path, cases=cases, routes=scheduler.ROUTES, all_active=True)
    workers = {(route, arm): lab.make_worker(route, arm) for route in scheduler.ROUTES for arm in ("A", "B")}
    assert workers[KIMI, "A"].step()["status"] == "priority_wait"
    assert not lab.native.sent and not lab.quota.calls
    expected = []
    for route in scheduler.ROUTES:
        for case_id in ("X1", "X2", "D1"):
            for arm in ("A", "B"):
                lab.clock.now += 5
                workers[route, arm].step()
                expected.append((route, arm, case_id))
    # All keyed/cross queues are now exhausted, but all 6 unkeyed cells remain.
    for route in scheduler.ROUTES:
        for arm in ("A", "B"):
            lab.clock.now += 5
            workers[route, arm].step()
            expected.append((route, arm, "U1"))
    actual = [(row["route"], row["directory"].parts[-4], row["directory"].parts[-2]) for row in lab.native.sent]
    assert actual == expected
    assert all(worker.snapshot()["planned_cells"] == 4 for worker in workers.values())


def test_higher_quota_pending_does_not_starve_other_route_but_blocks_unkeyed(tmp_path):
    cases = [case("X1"), case("U1", stage="dev", keys=0, priority=1)]
    lab = Lab(tmp_path, cases=cases, routes=scheduler.ROUTES, route=KIMI, all_active=True)
    for route in (SONNET, OPUS):
        for arm in ("A", "B"):
            lab.cursor(route, arm, 0, status="quota_pending", blocked_until=scheduler.utc(1500),
                       blocked_route=route, wait_basis="provider_reset")
    lab.cursor(KIMI, "B", len(cases))
    kimi = lab.make_worker(KIMI)
    assert kimi.step()["next_case_id"] == "U1"
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["route"] == KIMI
    assert kimi.step()["status"] == "priority_wait"
    assert scheduler.read_json(kimi.state_path)["wait_basis"] == "keyed_cross_barrier"
    assert len(lab.native.sent) == 1


def test_disabled_opus_records_every_missing_denominator_without_admission(tmp_path):
    cases = [case("X1"), case("D1", stage="dev", priority=1), case("U1", stage="dev", keys=0, priority=2)]
    lab = Lab(tmp_path, cases=cases, routes=scheduler.ROUTES, route=OPUS, all_active=True,
              disabled={OPUS: "credential usage coverage unavailable; no fallback"})
    for arm in ("A", "B"):
        summary = lab.make_worker(OPUS, arm).run()
        assert summary["status"] == "blocked" and summary["planned_cells"] == summary["missing_cells"] == 3
        assert summary["attempted_cells"] == summary["answered_cells"] == summary["pending_cells"] == 0
        for row in cases:
            missing = lab.outcome(row["case_id"], route=OPUS, arm=arm)
            assert missing["status"] == "missing" and "route_disabled" in missing["reason"]
            assert missing["paired"]["parsed_findings"] is None and missing["cell_path"] is None
    assert not lab.quota.calls and not lab.native.sent
    assert not (lab.root / "calls").exists()


def test_source_unavailable_is_terminal_without_a_request(tmp_path):
    lab = Lab(tmp_path, cases=[case(prepared=False)])
    summary = lab.worker.run()
    assert summary["missing_cells"] == 1 and summary["planned_cells"] == 1
    assert lab.outcome()["audit"]["status"] == "unavailable_input"
    assert lab.outcome()["paired"]["raw_findings"] is None
    assert not lab.quota.calls and not lab.native.sent


def test_pending_at_cutoff_is_explicit_missing_and_never_sent_late(tmp_path):
    cases = [case("X1"), case("D1", stage="dev", priority=1), case("U1", stage="dev", keys=0, priority=2)]
    lab = Lab(tmp_path, cases=cases)
    lab.quota.decide = lambda route, count: blocked(5000)
    lab.worker.step()
    summary = lab.worker.run()
    assert lab.clock.waits == [1000] and lab.clock.now == 2000
    assert summary["status"] == "blocked" and summary["missing_cells"] == 3
    assert summary["pending_cells"] == summary["attempted_cells"] == summary["answered_cells"] == 0
    assert len(lab.quota.calls) == 1 and not lab.native.sent
    for row in cases:
        outcome = lab.outcome(row["case_id"])
        assert outcome["reason"] == "cutoff_unsent_or_pending"
        assert outcome["paired"]["parsed_findings"] is None and outcome["cell_path"] is None
    assert not (lab.root / "calls").exists()


def test_scorer_pending_cutoff_preserves_answered_audit_not_zero_findings(tmp_path):
    lab = Lab(tmp_path, cutoff=1200)
    lab.native.findings = [{"candidate_id": "C1"}]
    lab.quota.decide = lambda route, count: blocked(5000) if count == 2 else {"eligible": True}
    lab.worker.step()
    summary = lab.worker.run()
    outcome = lab.outcome()
    assert summary["status"] == "checkpoint_partial"
    assert summary["attempted_cells"] == summary["answered_cells"] == summary["missing_cells"] == 1
    assert outcome["audit"]["status"] == "answered"
    assert outcome["paired"]["parsed_findings"] == 1 and outcome["paired"]["complete"] is False
    assert len(lab.native.sent) == 1 and outcome["cell_path"] is None


def test_admission_finishing_at_cutoff_cannot_authorize_late_request(tmp_path):
    lab = Lab(tmp_path, cutoff=1010)
    lab.quota.on_admit = lambda: setattr(lab.clock, "now", 1010)
    assert lab.worker.run()["missing_cells"] == 1
    assert len(lab.quota.calls) == 1 and not lab.native.sent


def test_timeout_is_remaining_integer_seconds_and_sent_timeout_never_retries(tmp_path):
    lab = Lab(tmp_path, cases=[case("X1"), case("X2", priority=1)], cutoff=1001.8)
    lab.native.status["audit"] = "unanswered"
    lab.native.on_send = lambda directory, kwargs: setattr(lab.clock, "now", 1001.8)
    summary = lab.worker.run()
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["timeout_seconds"] == 1
    assert lab.native.sent[0]["at"] < 1001.8
    assert summary["missing_cells"] == 2 and summary["attempted_cells"] == 1
    lab.make_worker().run()
    assert len(lab.native.sent) == 1


def test_less_than_one_second_remaining_never_sends(tmp_path):
    lab = Lab(tmp_path, cutoff=1000.8)
    assert lab.worker.run()["missing_cells"] == 1
    assert not lab.native.sent


def test_provider_lock_wait_is_outside_call_artifacts_and_then_sends_once(tmp_path):
    lab = Lab(tmp_path)
    path = lab.root / "locks/provider-anthropic.lock"
    with scheduler.file_lock(path):
        state = lab.worker.step()
    assert state["status"] == "provider_wait" and state["blocked_until"] == scheduler.utc(1005)
    assert not lab.quota.calls and not lab.native.sent
    assert not (lab.root / "calls").exists()
    lab.worker.run()
    assert lab.clock.waits == [5] and len(lab.native.sent) == 1


def test_runtime_is_rechecked_after_admission_and_after_reset_wait(tmp_path):
    lab = Lab(tmp_path)
    changed = False

    def verify():
        if changed:
            raise ValueError("frozen native runtime changed")
        lab.verified.append(lab.clock())

    worker = lab.make_worker(verifier=verify)
    lab.quota.decide = lambda route, count: blocked() if count == 1 else {"eligible": True}
    worker.step()
    changed = True
    lab.clock.now = 1100
    with pytest.raises(ValueError, match="runtime changed"):
        worker.step()
    assert not lab.native.sent and len(lab.quota.calls) == 1
    changed = False
    lab.quota.on_admit = lambda: setattr(lab.clock, "now", 1101)

    def verify_after_admission():
        if lab.clock() == 1101:
            raise ValueError("frozen invocation controls drifted")

    worker.verifier = verify_after_admission
    with pytest.raises(ValueError, match="controls drifted"):
        worker.step()
    assert not lab.native.sent and len(lab.quota.calls) == 2


def test_unfinished_consumed_attempt_is_terminal_without_reissue(tmp_path):
    lab = Lab(tmp_path)
    directory = lab.root / "calls" / SONNET.replace("/", "--") / "A/cross/X1/audit"
    scheduler.write_json(directory / "call-consumed.json", {"timeout_seconds": 600}, immutable=True)
    summary = lab.worker.run()
    assert summary["attempted_cells"] == summary["missing_cells"] == 1
    assert lab.outcome()["reason"] == "consumed_unfinished_no_retry"
    assert not lab.native.sent and not lab.quota.calls


def test_identity_quarantine_stops_subsequent_route_consumption(tmp_path):
    lab = Lab(tmp_path, cases=[case("X1"), case("X2", priority=1)])
    lab.native.status["audit"] = "quarantined_identity"
    summary = lab.worker.run()
    assert len(lab.native.sent) == len(lab.quota.calls) == 1
    assert summary["missing_cells"] == 2 and summary["attempted_cells"] == 1
    assert "native_identity_or_isolation_quarantine" in lab.outcome("X2")["reason"]


def test_outcomes_are_immutable_and_restart_recovers_cursor_without_resend(tmp_path):
    lab = Lab(tmp_path)
    before = copy.deepcopy(scheduler.read_json(lab.worker.state_path))
    lab.worker.run()
    path = lab.worker._outcome_path(case())
    content = path.read_bytes()
    with pytest.raises(FileExistsError):
        scheduler.write_json(path, {"replacement": True}, immutable=True)
    assert path.read_bytes() == content
    scheduler.write_json(lab.worker.state_path, before)  # crash after outcome publication, before cursor commit
    lab.make_worker().run()
    assert len(lab.native.sent) == 1 and path.read_bytes() == content
    assert not list(lab.root.rglob(".write-*"))


def test_plan_refuses_scope_reduction_and_wrong_priority_order(tmp_path):
    lab = Lab(tmp_path, cases=[case("X1"), case("D1", stage="dev", priority=1),
                               case("U1", stage="dev", keys=0, priority=2)])
    reduced = copy.deepcopy(lab.plan)
    reduced["workers"][0]["case_ids"].remove("U1")
    with pytest.raises(ValueError, match="every case denominator"):
        scheduler.validate_plan(lab.protocol, reduced)
    reordered = copy.deepcopy(lab.plan)
    reordered["workers"][0]["case_ids"] = ["U1", "X1", "D1"]
    with pytest.raises(ValueError, match="ordered"):
        scheduler.validate_plan(lab.protocol, reordered)
    with pytest.raises(ValueError, match="frozen plan"):
        lab.make_worker("anthropic/claude-sonnet-latest")


def test_full_all_dev_and_source_unavailable_cross_denominators_survive_plan(tmp_path):
    rows = [case(f"X{index:04}", keys=2 if index < 14 else 0, prepared=index < 12)
            for index in range(15)]
    rows += [case(f"D{index:04}", stage="dev", keys=1 if index < 44 else 0, priority=index)
             for index in range(399)]
    lab = Lab(tmp_path, cases=rows, routes=scheduler.ROUTES, all_active=True)
    assert all(len(worker["case_ids"]) == 414 for worker in lab.plan["workers"])
    assert len(lab.worker.cases) == 414
    assert sum(row["stage"] == "dev" and row["key_count"] == 0 for row in lab.worker.cases.values()) == 355


def test_hash_checked_science_loader_keeps_gold_contract_and_module_identity(tmp_path, monkeypatch):
    root = tmp_path / "immutable-v2"
    root.mkdir()
    sources = {name: "from pathlib import Path\nROOT = Path(__file__).parent\n" for name in scheduler.HELPERS}
    sources["run_slice"] += "import transport, gold, paired, prepare_inputs\n"
    sources["transport"] += "CONTRACT_PATH = ROOT / 'transport-contract.json'\n"
    rows = []
    for name, source in sources.items():
        path = root / (name + ".py")
        path.write_text(source)
        rows.append({"path": str(path), "sha256": scheduler.sha(source.encode())})
    sentinel = types.ModuleType("transport")
    monkeypatch.setitem(sys.modules, "transport", sentinel)
    protocol = {"code_files": rows, "execution": {"science_root": str(root)}}
    before = footprint(root)
    science = scheduler.load_science(protocol)
    assert science.run_slice.transport is science.transport
    assert science.run_slice.gold is science.gold and root == science.gold.ROOT
    assert root / "transport-contract.json" == science.transport.CONTRACT_PATH
    assert sys.modules["transport"] is sentinel
    assert footprint(root) == before and not list(root.rglob("__pycache__"))
    (root / "quota.py").write_text("raise AssertionError('must fail hash check before execution')\n")
    with pytest.raises(ValueError, match="source/input drift"):
        scheduler.load_science(protocol)


def test_explicit_protocol_filename_rechecks_runtime_config_and_plan(tmp_path, monkeypatch):
    lab = Lab(tmp_path)
    config, runtime = tmp_path / "controlled.yml", tmp_path / "omp-binary"
    config.write_text("frozen controls\n")
    runtime.write_bytes(b"synthetic executable identity, never executed")
    stat = runtime.stat()
    protocol = copy.deepcopy(lab.protocol)
    protocol.update(config={"path": str(config), "sha256": scheduler.sha(config.read_bytes())},
                    runtime={"path": str(runtime), "invocation_path": str(runtime), "inode": stat.st_ino,
                             "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    plan_path = Path(protocol["execution"]["plan_path"])
    scheduler.write_json(plan_path, lab.plan)
    plan_sha = scheduler.sha(plan_path.read_bytes())
    protocol["execution"]["plan_sha256"] = plan_sha
    protocol["pre_cross_data_files"] = [{"path": str(plan_path), "sha256": plan_sha}]
    path = tmp_path / "prospective-protocol.json"
    scheduler.write_json(path, protocol)
    freeze_sha = scheduler.sha(path.read_bytes())
    path.with_suffix(".sha256").write_text(freeze_sha + "\n")
    for key, state in scheduler.initial_states(protocol, lab.plan, freeze_sha, plan_sha, now=1000).items():
        scheduler.write_json(lab.root / "pending" / (key + ".json"), state)
    monkeypatch.setattr(scheduler.shutil, "which", lambda name: str(runtime))
    worker = scheduler.Worker.from_protocol(path, SONNET, "A", science_loader=lambda protocol: lab.helpers,
                                           clock=lab.clock, sleeper=lab.clock.sleep, dispatcher=lab.dispatcher)
    worker.verifier()
    original_plan = plan_path.read_bytes()
    plan_path.write_bytes(original_plan + b"\n")
    with pytest.raises(ValueError, match="plan changed"):
        worker.verifier()
    plan_path.write_bytes(original_plan)
    config.write_text("changed controls\n")
    with pytest.raises(ValueError, match="controls drifted"):
        worker.verifier()
    config.write_text("frozen controls\n")
    runtime.write_bytes(b"different frozen runtime")
    with pytest.raises(ValueError, match="runtime changed"):
        worker.verifier()
    assert not lab.native.sent and not lab.quota.calls


def bind_synthetic_cache(lab):
    row = lab.plan["cases"][0]
    directory = lab.science_root / "scorer-only-keys"
    keys, provenance = directory / (row["case_id"] + ".json"), directory / (row["case_id"] + ".provenance.json")
    scheduler.write_json(keys, [{"key_id": "synthetic-key"}], immutable=True)
    scheduler.write_json(provenance, {"keys_sha256": scheduler.sha(keys.read_bytes()),
                                      "task_id": row["task_id"]}, immutable=True)
    row.update(keys_path=str(keys), keys_sha256=scheduler.sha(keys.read_bytes()),
               keys_provenance_path=str(provenance), keys_provenance_sha256=scheduler.sha(provenance.read_bytes()))
    lab.helpers.run_slice.execute_cell = lambda *args: lab.dispatcher(
        *args, quota_call=lab.helpers.run_slice.quota_call)
    return keys, provenance


@pytest.mark.parametrize("mutation", ["keys", "provenance", "joint", "missing"])
def test_default_dispatch_rejects_missing_or_jointly_tampered_v2_cache(tmp_path, mutation):
    lab = Lab(tmp_path)
    keys, provenance = bind_synthetic_cache(lab)
    if mutation == "missing":
        keys.unlink()
    elif mutation in {"keys", "joint"}:
        scheduler.write_json(keys, [{"key_id": "different-synthetic-key"}])
        if mutation == "joint":
            scheduler.write_json(provenance, {"keys_sha256": scheduler.sha(keys.read_bytes()),
                                              "task_id": lab.plan["cases"][0]["task_id"]})
    else:
        scheduler.write_json(provenance, {"changed": True})
    with pytest.raises(ValueError, match="cache"):
        lab.make_worker(dispatcher=None).step()
    assert not lab.native.sent and not lab.quota.calls and not lab.dispatcher.calls


def test_default_execute_cell_bridge_reuses_read_only_v2_cache(tmp_path):
    lab = Lab(tmp_path)
    bind_synthetic_cache(lab)
    before = footprint(lab.science_root)
    summary = lab.make_worker(dispatcher=None).run()
    assert summary["complete_paired_cells"] == 1 and len(lab.native.sent) == 1
    assert footprint(lab.science_root) == before
    assert lab.science_root == lab.helpers.gold.ROOT


def test_scientific_input_hashes_are_checked_before_any_helper_executes(tmp_path):
    root = tmp_path / "v2"
    root.mkdir()
    rows = []
    for name in scheduler.HELPERS:
        path = root / (name + ".py")
        source = "raise AssertionError('helper executed before deep input verification')\n"
        path.write_text(source)
        rows.append({"path": str(path), "sha256": scheduler.sha(source.encode())})
    source_path = root / "synthetic-canonical-source.json"
    source_path.write_text("changed source bytes")
    protocol = {"code_files": rows,
                "pre_cross_data_files": [{"path": str(source_path), "sha256": "0" * 64}],
                "execution": {"science_root": str(root)}}
    with pytest.raises(ValueError, match="source/input drift"):
        scheduler.load_science(protocol)


def test_configured_fourth_model_is_after_keyed_strata_before_every_unkeyed(tmp_path):
    extra = "openai-codex/gpt-6.1-sol"
    rows = [case("X1"), case("D1", stage="dev", priority=1),
            case("U1", stage="dev", keys=0, priority=2)]
    lab = Lab(tmp_path, cases=rows, routes=scheduler.ROUTES, all_active=True)
    lab.protocol["models"].append(extra)
    lab.protocol["execution"]["route_order"] = [*scheduler.ROUTES, extra]
    for arm in ("A", "B"):
        lab.plan["workers"].append({"route": extra, "arm": arm, "case_ids": ["X1", "D1", "U1"]})
    states = scheduler.initial_states(lab.protocol, lab.plan, FREEZE, PLAN, now=1000)
    for key, state in states.items():
        scheduler.write_json(lab.root / "pending" / (key + ".json"), state)
    for route in scheduler.ROUTES:
        for arm in ("A", "B"):
            lab.cursor(route, arm, 2)
    # Unkeyed work cannot jump ahead even though the added route has lower rank.
    assert lab.make_worker(SONNET).step()["status"] == "priority_wait"
    added = lab.make_worker(extra)
    assert added.step()["next_case_id"] == "D1"
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["route"] == extra


def test_shared_route_reset_does_not_let_unstarted_sibling_starve_another_route(tmp_path):
    lab = Lab(tmp_path, routes=(SONNET, OPUS), all_active=True)
    lab.cursor(SONNET, "A", 0, status="quota_pending", blocked_until=scheduler.utc(1500),
               blocked_route=SONNET, quota_admission_path="synthetic-authoritative-admission",
               wait_basis="provider_reset")
    # B has never started, but its exact route is authoritatively quota blocked.
    assert lab.make_worker(OPUS).step()["status"] == "finished"
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["route"] == OPUS
    sibling = lab.make_worker(SONNET, "B")
    pending = sibling.step()
    assert pending["status"] == "quota_pending" and pending["blocked_until"] == scheduler.utc(1500)
    assert pending["quota_admission_path"] == "synthetic-authoritative-admission"
    assert len(lab.quota.calls) == 1  # shared reset never authorizes or repeats metadata work


def test_unfinalized_empty_audit_at_cutoff_does_not_become_a_completed_cell(tmp_path):
    lab = Lab(tmp_path)
    lab.worker.current = row = lab.plan["cases"][0]
    directory = lab.worker._cell_directory(row)
    audit = {"status": "answered", "attempt_consumed": True}
    scheduler.write_json(directory / "audit/receipt.json", audit, immutable=True)
    scheduler.write_json(directory / "prediction.json", {
        "audit_receipt_sha256": scheduler.sha((directory / "audit/receipt.json").read_bytes()),
        "source_sha256": row["source_sha256"], "audit_status": "answered",
        "parsed_payloads": [], "raw_findings_count": 0}, immutable=True)
    lab.clock.now = lab.worker.cutoff
    lab.worker.run()
    outcome = lab.outcome()
    assert outcome["status"] == "missing" and outcome["paired"]["complete"] is False
    assert outcome["prediction"]["raw_findings_count"] == 0  # observed, never fabricated
    assert not lab.native.sent and not lab.quota.calls
