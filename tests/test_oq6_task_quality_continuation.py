"""Closed-parent preparation proofs using synthetic files only; no model/data calls."""
from __future__ import annotations

import copy
import json
import stat
import subprocess
import types
from pathlib import Path

import pytest

PRODUCER_PATH = (Path(__file__).resolve().parents[1] /
                 "research/experiments/oq6-task-quality-gate-rerun/continue_dev.py")
producer = types.ModuleType("oq6_continuation_fixture")
producer.__file__ = str(PRODUCER_PATH)
exec(compile(PRODUCER_PATH.read_bytes(), str(PRODUCER_PATH), "exec"), producer.__dict__)
SONNET, KIMI = producer.ACTIVE_ROUTES
OWNED = ("call-consumed.json", "prompt.txt", "system.txt", "controlled-subscription.yml",
         "stdout.jsonl", "stderr.txt", "response.txt", "sessions")


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def dump(path, value):
    return write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def footprint(root):
    return {str(path.relative_to(root)): (str(path.readlink()) if path.is_symlink() else
                                         (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))
            for path in root.rglob("*") if path.is_file() or path.is_symlink()}


class Lab:
    def __init__(self, tmp_path, monkeypatch):
        tmp_path = tmp_path.resolve()
        self.tmp, self.now = tmp_path, 1000.0
        self.immutable = tmp_path / "immutable"
        self.science = self.immutable / "science"
        self.root = self.immutable / "v4"
        self.output = tmp_path / "v5"
        self.runner = producer._load_runner()
        self.coverage = write(self.immutable / "selectors" / "coverage.py", b"# synthetic coverage selector\n")
        runtime = write(self.immutable / "native-runtime", b"synthetic runtime; never executed\n")
        self.invocation = self.immutable / "omp"
        self.invocation.symlink_to(runtime)
        monkeypatch.setattr(self.runner.shutil, "which", lambda name: str(self.invocation) if name == "omp" else None)
        monkeypatch.setattr(producer.time, "time", lambda: self.now)
        monkeypatch.setattr(subprocess, "Popen", self.forbidden)
        transport = write(self.science / "transport.py", (
            "SCHEMA = 'oq6.native-transport/v1'\nOWNED_ARTIFACTS = " + repr(OWNED) +
            "\nraise AssertionError('preparation imported a scientific helper')\n").encode())
        attestation = write(self.immutable / "retained" / "attestation.py", b"# synthetic immutable attestation\n")
        self.scheduling_runner = write(
            self.immutable / "old-runtime" / "research" / "experiments" / "oq6-task-quality-gate-rerun" / "runner.py",
            b"# original frozen scheduler\n")
        self.production_runner = write(
            self.immutable / "detached-runtime" / "src" / "evallab" / "runner.py",
            b"# original immutable production runtime; must not be replaced\n")
        old_snapshot = write(self.immutable / "old-runtime" / "frozen-runner.py", self.scheduling_runner.read_bytes())
        self.config = write(self.science / "controlled-subscription.yml", b"synthetic controlled config\n")
        contract = dump(self.science / "transport-contract.json", {"session_attestation": str(attestation)})
        package = self.science / "package"
        package_file = write(package / "task.py", b"# synthetic admitted package bytes\n")
        source = write(self.science / "source-view.txt", b"synthetic bounded source view\n")
        prompts = {arm: write(self.science / ("prompt-" + arm + ".txt"), ("synthetic prompt " + arm).encode())
                   for arm in ("A", "B")}
        self.dev = []
        for index in range(producer.DEV_CASES):
            cid = f"D{index:04}"
            keys = write(self.science / "scorer-only-keys" / (cid + ".json"), b"[]\n")
            provenance = dump(self.science / "scorer-only-keys" / (cid + ".provenance.json"),
                              {"schema": "synthetic.provenance/v1", "case_id": cid})
            self.dev.append({"case_id": cid, "task_id": "synthetic/" + cid, "stage": "dev",
                             "key_count": 1 if index < 44 else 0, "scheduling_priority": index,
                             "preparation_status": "prepared", "preparation_error": None,
                             "package": str(package), "source_path": str(source),
                             "source_sha256": producer._digest(source),
                             "files": [{"path": "task.py", "sha256": producer._digest(package_file)}],
                             "keys_path": str(keys), "keys_sha256": producer._digest(keys),
                             "keys_provenance_path": str(provenance),
                             "keys_provenance_sha256": producer._digest(provenance),
                             "prompts": {arm: {"path": str(path), "sha256": producer._digest(path)}
                                         for arm, path in prompts.items()},
                             "pinned_native_package_digest": "c" * 64})
        self.cross = []
        for index in range(producer.CROSS_CASES):
            cid = f"X{index:04}"
            keys = write(self.science / "scorer-only-keys" / (cid + ".json"), b"[]\n")
            provenance = dump(self.science / "scorer-only-keys" / (cid + ".provenance.json"),
                              {"schema": "synthetic.provenance/v1", "case_id": cid})
            case = copy.deepcopy(self.dev[0])
            case.update(case_id=cid, task_id="synthetic/" + cid, stage="cross", scheduling_priority=index - 15,
                        keys_path=str(keys), keys_sha256=producer._digest(keys),
                        keys_provenance_path=str(provenance), keys_provenance_sha256=producer._digest(provenance))
            if index >= 12:
                case.update(preparation_status="unavailable", preparation_error="frozen missing cross source")
                for field in ("package", "files", "source_path", "source_sha256", "prompts"):
                    case.pop(field)
            self.cross.append(case)
        cases = [*self.cross, *self.dev]
        ids = [case["case_id"] for case in cases]
        self.plan = {"cases": cases, "workers": [{"route": route, "arm": arm, "case_ids": ids}
                                                for route in self.runner.ROUTES for arm in ("A", "B")]}
        self.plan_path = dump(self.root / "plan.json", self.plan)
        self.plan_sha = producer._digest(self.plan_path)
        runtime_stat = runtime.stat()
        code = [producer._pin(path) for path in (transport, attestation, self.scheduling_runner)]
        code[-1]["snapshot_path"] = str(old_snapshot)
        code.append(producer._pin(self.production_runner))
        self.protocol = {"schema": "oq6.prospective-frozen-study/v1", "study_version": "oq6-gate-rerun-v4",
                         "frozen_at": self.runner.utc(800), "config": producer._pin(self.config),
                         "runtime": {"invocation_path": str(self.invocation), "path": str(runtime),
                                     "sha256": producer._digest(runtime), "inode": runtime_stat.st_ino,
                                     "bytes": runtime_stat.st_size, "mtime_ns": runtime_stat.st_mtime_ns},
                         "code_files": code, "pre_cross_data_files": [producer._pin(contract), producer._pin(source)],
                         "models": [*self.runner.ROUTES, "meta/synthetic-disabled"],
                         "active_worker_models_at_freeze": list(self.runner.ROUTES),
                         "fixed_scorer": SONNET, "scorer": {"route": SONNET, "union_once": True},
                         "gates": {"old_0_8": "frozen old policy", "new_verified_0_5": "frozen new policy"},
                         "source_bounds": {"package_characters": 180000},
                         "auditor_system_sha256": "b" * 64, "dev_census": {"tasks": 399, "keyed_tasks": 44},
                         "cross": {"tasks": 15, "original_outputs_immutable": True},
                         "execution": {"raw_root": str(self.root), "science_root": str(self.science),
                                       "plan_path": str(self.plan_path), "plan_sha256": self.plan_sha,
                                       "cutoff_at": self.runner.utc(900), "route_order": list(self.runner.ROUTES),
                                       "quota_recheck_seconds": 60, "priority_wait_seconds": 5,
                                       "disabled_routes": {self.runner.ROUTES[1]: "synthetic offline-disabled"}}}
        self.protocol_path = dump(self.root / "protocol.json", self.protocol)
        self.protocol_sha = producer._digest(self.protocol_path)
        write(self.protocol_path.with_suffix(".sha256"), (self.protocol_sha + "\n").encode())
        states = self.runner.initial_states(self.protocol, self.plan, self.protocol_sha, self.plan_sha, now=800)
        for key, state in states.items():
            state.update(status="finished", next_index=len(ids), next_case_id=None, next_priority=None)
            dump(self.root / "pending" / (key + ".json"), state)

    @staticmethod
    def forbidden(*args, **kwargs):
        raise AssertionError("preparation attempted a command/provider call")

    def directory(self, cid, route=SONNET, arm="A", *, stage="dev"):
        return self.root / "calls" / route.replace("/", "--") / arm / stage / cid

    def snapshot(self, **kwargs):
        return producer.capture_prior_snapshot(self.protocol_path, now=self.now, **kwargs)

    def prepare(self, **kwargs):
        values = {"parent_protocol_path": self.protocol_path, "output_root": self.output,
                  "checkpoint_at": self.runner.utc(2000), "now": self.now, "coverage_path": self.coverage}
        values.update(kwargs)
        return producer.prepare_continuation(**values)

    def row(self, snapshot, cid, route=SONNET, arm="A"):
        return snapshot["workers"][self.runner.worker_key(route, arm)]["cases"][cid]

    def audit(self, cid, *, route=SONNET, arm="A", stage="dev", status="answered", prediction=True, parse_error=None):
        directory = self.directory(cid, route, arm, stage=stage) / "audit"
        case = next(case for case in self.plan["cases"] if case["case_id"] == cid)
        response = json.dumps({"findings": [{"synthetic": True}]}).encode()
        write(directory / "response.txt", response)
        write(directory / "prompt.txt", Path(case["prompts"][arm]["path"]).read_bytes())
        write(directory / "system.txt", b"synthetic frozen system\n")
        write(directory / "controlled-subscription.yml", self.config.read_bytes())
        write(directory / "stdout.jsonl", b'{"synthetic":true}\n')
        write(directory / "stderr.txt", b"")
        write(directory / "sessions" / "session.jsonl", b'{"synthetic_session":true}\n')
        request = {"requested_route": route, "requested_model": route,
                   "requested_provider": route.split("/", 1)[0], "requested_model_id": route.split("/", 1)[1],
                   "expected_array": "findings", "timeout_seconds": 517,
                   "config_sha256": self.protocol["config"]["sha256"],
                   "prompt_sha256": case["prompts"][arm]["sha256"],
                   "system_sha256": self.protocol["auditor_system_sha256"],
                   "contract_sha256": self.protocol["pre_cross_data_files"][0]["sha256"],
                   "attestation_source_sha256": self.protocol["code_files"][1]["sha256"]}
        dump(directory / "call-consumed.json", request)
        artifacts = {str(path.relative_to(directory)): {"sha256": producer._digest(path), "bytes": path.stat().st_size}
                     for path in directory.rglob("*") if path.is_file()}
        receipt = {**request, "schema": "oq6.native-transport/v1", "status": status, "attempt_consumed": True,
                   "receipt_path": str(directory / "receipt.json"), "response_path": str(directory / "response.txt"),
                   "call_consumed_path": str(directory / "call-consumed.json"),
                   "call_consumed_sha256": producer._digest(directory / "call-consumed.json"),
                   "response_sha256": producer._sha(response), "served_identities": [route],
                   "native_response_matches_stdout": True, "native_prompt_matches_frozen_bytes": True,
                   "isolation_valid": True, "errors": [], "memory_events": [], "tool_events": [],
                   "retry_events": [], "context_violations": [], "artifacts": artifacts,
                   "model_change_events": [], "fallback_events": [], "model_fallback": False,
                   "answer": {"findings": [{"synthetic": True}]}, "irrelevant_quality_label": "do-not-select-on-this"}
        dump(directory / "receipt.json", receipt)
        if prediction:
            value = {"schema": "oq6.frozen-prediction/v1", "case_id": cid, "route": route, "arm": arm,
                     "audit_status": status, "source_sha256": case["source_sha256"],
                     "audit_receipt_sha256": producer._digest(directory / "receipt.json"),
                     "response_sha256": receipt["response_sha256"], "parse_error": parse_error,
                     "parsed_payloads": [{"opaque_synthetic_payload": True}], "raw_findings_count": 1,
                     "irrelevant_quality_label": "labels are never selection inputs"}
            # Deliberately noncanonical bytes: a resume must copy, not serialize.
            write(directory.parent / "prediction.json", b"  " + json.dumps(value, indent=1).encode() + b"  \n")
        return receipt


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(tmp_path, monkeypatch)


def test_snapshot_exact_schema_excludes_consumed_failures_and_finalizations(lab):
    lab.audit("D0000", status="unanswered")
    lab.audit("D0001", status="quarantined_identity")
    lab.audit("D0002", parse_error="synthetic malformed parse")
    write(lab.directory("D0003") / "audit" / "receipt.json", b"{malformed receipt")
    write(lab.directory("D0004") / "audit" / "prompt.txt", b"orphan consumed prompt")
    lab.audit("D0005")
    write(lab.directory("D0005") / "scorer" / "call-consumed.json", b"orphan consumed scorer")
    write(lab.directory("D0006") / "cell.json", b"opaque terminal cell; no quality parsing")
    write(lab.directory("D0007") / "prediction.json", b"orphan prediction without auditor")
    snapshot = lab.snapshot()
    assert set(snapshot) == {"schema", "stage", "parent_root", "parent_protocol_sha256", "parent_plan_sha256",
                             "captured_at", "workers"}
    assert snapshot["schema"] == producer.SNAPSHOT_SCHEMA
    assert snapshot["stage"] == "dev"
    assert len(snapshot["workers"]) == 4
    for index in range(8):
        row = lab.row(snapshot, f"D{index:04}")
        assert row["action"] == "prior_terminal"
        assert set(row) == {"action", "audit", "scorer", "cell_path", "cell_sha256", "prediction_path", "prediction_sha256"}
        assert set(row["audit"]) == {"directory", "consumed", "receipt_path", "receipt_sha256", "status", "owned_files"}
    assert lab.row(snapshot, "D0004")["audit"]["consumed"] is True
    assert lab.row(snapshot, "D0004")["audit"]["receipt_path"] is None
    assert lab.row(snapshot, "D0008")["action"] == "audit"
    assert lab.row(snapshot, "D0000", arm="B")["action"] == "audit"
    assert all(len(worker["cases"]) == 399 for worker in snapshot["workers"].values())
    assert not lab.output.exists()


def test_attested_audit_resumes_only_unsent_scorer_ignoring_quality_labels(lab):
    lab.audit("D0000")
    lab.audit("D0001", prediction=False)
    snapshot = lab.snapshot()
    assert lab.row(snapshot, "D0000")["action"] == "resume_scorer"
    assert lab.row(snapshot, "D0001")["action"] == "resume_scorer"
    assert lab.row(snapshot, "D0000")["prediction_sha256"] == producer._digest(lab.directory("D0000") / "prediction.json")
    assert lab.row(snapshot, "D0001")["prediction_path"] is None
    assert lab.row(snapshot, "D0000")["scorer"]["consumed"] is False


@pytest.mark.parametrize("mutation", ["identity", "isolation", "attestation", "artifacts", "prediction_custody", "prediction_json"])
def test_invalid_audit_or_prediction_custody_is_terminal_without_retry(lab, mutation):
    lab.audit("D0000")
    receipt_path = lab.directory("D0000") / "audit" / "receipt.json"
    if mutation in {"identity", "isolation", "attestation", "artifacts"}:
        receipt = producer._object(receipt_path)
        if mutation == "identity":
            receipt["served_identities"] = [KIMI]
        elif mutation == "isolation":
            receipt["isolation_valid"] = False
        elif mutation == "attestation":
            receipt["attestation_source_sha256"] = "0" * 64
        else:
            receipt["artifacts"] = {}
        dump(receipt_path, receipt)
    elif mutation == "prediction_custody":
        prediction_path = lab.directory("D0000") / "prediction.json"
        prediction = producer._object(prediction_path)
        prediction["audit_receipt_sha256"] = "0" * 64
        dump(prediction_path, prediction)
    else:
        write(lab.directory("D0000") / "prediction.json", b"not JSON")
    before = footprint(lab.immutable)
    assert lab.row(lab.snapshot(), "D0000")["action"] == "prior_terminal"
    assert footprint(lab.immutable) == before


def test_preparation_has_unequal_arm_partitions_all399_and_no_cross(lab):
    lab.audit("D0000", status="unanswered")
    lab.audit("D0001")
    lab.audit("D0002", route=KIMI, arm="B", status="unanswered")
    original_plan = copy.deepcopy(lab.plan)
    manifest = lab.prepare()
    assert manifest["dispatch_performed"] is False
    assert manifest["checkpoint_is_completion"] is False
    assert manifest["represented_cases_per_worker"] == 399
    assert {row["route"] for row in manifest["routes"]} == {SONNET, KIMI}
    for route in manifest["routes"]:
        protocol, plan = producer._object(route["protocol_path"]), producer._object(route["plan_path"])
        assert protocol["execution"]["route_order"] == [route["route"]]
        assert protocol["execution"]["raw_root"] == str(lab.output)
        assert protocol["models"] == lab.protocol["models"]
        assert protocol["scorer"] == lab.protocol["scorer"]
        assert protocol["fixed_scorer"] == SONNET
        assert protocol["config"] == lab.protocol["config"]
        assert protocol["runtime"] == lab.protocol["runtime"]
        assert protocol["gates"] == lab.protocol["gates"]
        assert protocol["source_bounds"] == lab.protocol["source_bounds"]
        assert plan["cases"] == lab.dev
        assert {case["stage"] for case in plan["cases"]} == {"dev"}
        assert len(plan["cases"]) == 399
        assert set(plan["continuation"]) == {"schema", "stage", "parent_protocol_path", "parent_protocol_sha256",
                                             "parent_plan_path", "parent_plan_sha256", "parent_root",
                                             "prior_snapshot_path", "prior_snapshot_sha256"}
        assert plan["continuation"]["schema"] == producer.PLAN_SCHEMA
        assert plan["continuation"]["stage"] == "dev"
        all_ids = {case["case_id"] for case in lab.dev}
        for worker in plan["workers"]:
            remaining, prior = set(worker["case_ids"]), set(worker["prior_case_ids"])
            assert not remaining & prior
            assert remaining | prior == all_ids
            assert set(worker["resume_scorer_case_ids"]) <= remaining
            assert "X0000" not in remaining | prior
            key = lab.runner.worker_key(worker["route"], worker["arm"])
            state = producer._object(lab.output / "pending" / (key + ".json"))
            expected = lab.runner.initial_states(protocol, plan, route["protocol_sha256"], route["plan_sha256"], now=lab.now)
            assert state == expected[key]
        a, b = plan["workers"]
        assert len(a["case_ids"]) != len(b["case_ids"])
        if route["route"] == SONNET:
            assert a["prior_case_ids"] == ["D0000"]
            assert a["resume_scorer_case_ids"] == ["D0001"]
            assert not b["prior_case_ids"]
    assert producer._object(lab.plan_path) == original_plan
    assert not (lab.output / "calls" / SONNET.replace("/", "--") / "A" / "dev" / "D0000").exists()
    assert not (lab.output / "calls" / SONNET.replace("/", "--") / "A" / "cross").exists()


def test_seed_only_audit_symlink_and_exact_prediction_copy_never_changes_parent(lab):
    lab.audit("D0000")
    lab.audit("D0001", prediction=False)
    lab.audit("D0002", status="unanswered")
    original_prediction = lab.directory("D0000") / "prediction.json"
    prior_bytes = original_prediction.read_bytes()
    before = footprint(lab.immutable)
    lab.prepare()
    assert footprint(lab.immutable) == before
    new_base = lab.output / "calls" / SONNET.replace("/", "--") / "A" / "dev"
    for cid in ("D0000", "D0001"):
        directory = new_base / cid
        assert not directory.is_symlink()
        assert (directory / "audit").is_symlink()
        assert (directory / "audit").resolve() == lab.directory(cid) / "audit"
        assert not (directory / "scorer").exists()
        assert not (directory / "cell.json").exists()
    prediction = new_base / "D0000" / "prediction.json"
    assert not prediction.is_symlink()
    assert prediction.read_bytes() == prior_bytes
    assert prediction.stat().st_ino != original_prediction.stat().st_ino
    assert stat.S_IMODE(prediction.stat().st_mode) & 0o222 == 0
    assert not (new_base / "D0001" / "prediction.json").exists()
    assert not (new_base / "D0002").exists()
    assert not (new_base / "D0003").exists()


def test_freeze_binds_original_inputs_and_all_opaque_prior_artifacts(lab):
    lab.audit("D0000")
    manifest = lab.prepare()
    snapshot = producer._object(manifest["prior_snapshot_path"])
    row = lab.row(snapshot, "D0000")
    required = {row["audit"]["receipt_path"], row["prediction_path"],
                *(pin["path"] for pin in row["audit"]["owned_files"])}
    assert str(lab.directory("D0000") / "audit" / "sessions" / "session.jsonl") in required
    for route in manifest["routes"]:
        protocol = producer._object(route["protocol_path"])
        pins = {pin["path"]: pin["sha256"] for pin in protocol["pre_cross_data_files"]}
        assert required <= set(pins)
        assert pins[str(lab.protocol_path)] == lab.protocol_sha
        assert pins[str(lab.plan_path)] == lab.plan_sha
        assert pins[manifest["prior_snapshot_path"]] == manifest["prior_snapshot_sha256"]
        assert pins[route["plan_path"]] == route["plan_sha256"]
        assert all(pin in protocol["pre_cross_data_files"] for pin in lab.protocol["pre_cross_data_files"])
        code = {pin["path"]: pin for pin in protocol["code_files"]}
        for original in lab.protocol["code_files"]:
            if original["path"] == str(lab.scheduling_runner):
                assert original["path"] not in code
            else:
                assert code[original["path"]] == original
        assert str(PRODUCER_PATH) in code
        assert str(PRODUCER_PATH.with_name("runner.py")) in code
        assert str(lab.coverage) in code
        assert protocol["continuation"]["schema"] == producer.FREEZE_SCHEMA
        assert protocol["continuation"]["checkpoint_is_completion"] is False
        for source in protocol["continuation"]["source_snapshots"]:
            assert Path(source["path"]).read_bytes() == Path(source["source_path"]).read_bytes()
            assert producer._digest(source["path"]) == source["sha256"]
        for name in ("protocol", "plan"):
            path = Path(route[name + "_path"])
            assert producer._digest(path) == route[name + "_sha256"]
            assert path.with_suffix(".sha256").read_text().strip() == route[name + "_sha256"]
            assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0


@pytest.mark.parametrize("failure", ["before_cutoff", "unfinished", "missing_state", "extra_state", "wrong_freeze", "wrong_cursor"])
def test_parent_close_barrier_rejects_before_any_output_creation(lab, failure):
    state_path = lab.root / "pending" / (lab.runner.worker_key(KIMI, "B") + ".json")
    state = producer._object(state_path)
    now = lab.now
    if failure == "before_cutoff":
        now = 899
    elif failure == "unfinished":
        state.update(status="quota_pending", next_index=0, next_case_id="X0000", next_priority=-1)
        dump(state_path, state)
    elif failure == "missing_state":
        state_path.unlink()
    elif failure == "extra_state":
        dump(lab.root / "pending" / "unexpected.json", state)
    elif failure == "wrong_freeze":
        state["freeze_sha256"] = "0" * 64
        dump(state_path, state)
    else:
        state["next_index"] -= 1
        dump(state_path, state)
    before = footprint(lab.immutable)
    with pytest.raises(ValueError, match="parent|initialized"):
        lab.prepare(now=now)
    assert not lab.output.exists()
    assert footprint(lab.immutable) == before


@pytest.mark.parametrize("checkpoint", [900, 1000])
def test_checkpoint_is_required_to_be_future_not_mission_completion(lab, checkpoint):
    with pytest.raises(ValueError, match="future"):
        lab.prepare(checkpoint_at=lab.runner.utc(checkpoint))
    assert not lab.output.exists()


def test_supplied_snapshot_is_exactly_bound_and_stale_consumption_is_rejected(lab):
    lab.audit("D0000")
    snapshot = lab.snapshot()
    snapshot_path = dump(lab.tmp / "prior-snapshot.json", snapshot)
    raw = snapshot_path.read_bytes()
    manifest = lab.prepare(prior_snapshot_path=snapshot_path)
    assert Path(manifest["prior_snapshot_path"]).read_bytes() == raw
    assert manifest["prior_snapshot_sha256"] == producer._sha(raw)
    # New orphan native evidence after a snapshot must not silently become fresh work.
    write(lab.directory("D0001") / "audit" / "prompt.txt", b"consumed after snapshot")
    other = lab.tmp / "stale-output"
    with pytest.raises(ValueError, match="snapshot.*malformed, stale"):
        lab.prepare(output_root=other, prior_snapshot_path=snapshot_path)
    assert not other.exists()


def test_malformed_supplied_snapshot_cannot_relabel_consumed_auditor(lab):
    lab.audit("D0000", status="unanswered")
    snapshot = lab.snapshot()
    lab.row(snapshot, "D0000")["action"] = "audit"
    path = dump(lab.tmp / "relabelled-snapshot.json", snapshot)
    with pytest.raises(ValueError, match="snapshot.*conflicts"):
        lab.prepare(prior_snapshot_path=path)
    assert not lab.output.exists()


@pytest.mark.parametrize("mutation", ["numeric_consumed", "pre_close_capture", "future_capture"])
def test_supplied_snapshot_has_exact_boolean_and_closed_parent_time_schema(lab, mutation):
    lab.audit("D0000")
    snapshot = lab.snapshot()
    if mutation == "numeric_consumed":
        lab.row(snapshot, "D0000")["audit"]["consumed"] = 1
    else:
        snapshot["captured_at"] = lab.runner.utc(899 if mutation == "pre_close_capture" else 1001)
    path = dump(lab.tmp / "invalid-snapshot.json", snapshot)
    with pytest.raises(ValueError, match="snapshot"):
        lab.prepare(prior_snapshot_path=path)
    assert not lab.output.exists()


def test_parent_role_alias_cannot_duplicate_consumed_slot(lab):
    lab.audit("D0000")
    duplicate = lab.directory("D0001") / "audit"
    duplicate.parent.mkdir(parents=True)
    duplicate.symlink_to(lab.directory("D0000") / "audit", target_is_directory=True)
    with pytest.raises(ValueError, match="alias"):
        lab.prepare()
    assert not lab.output.exists()


def test_preparation_never_overwrites_existing_root_or_scientific_source(lab):
    lab.output.mkdir()
    sentinel = write(lab.output / "keep", b"another writer owns this root")
    before = footprint(lab.immutable)
    with pytest.raises(ValueError, match="new raw root"):
        lab.prepare()
    assert sentinel.read_bytes() == b"another writer owns this root"
    assert footprint(lab.immutable) == before


def test_cli_requires_explicit_checkpoint_and_prints_prepared_not_complete(lab, capsys):
    with pytest.raises(SystemExit) as missing:
        producer.main(["--parent-protocol", str(lab.protocol_path), "--output-root", str(lab.output)])
    assert missing.value.code == 2
    assert not lab.output.exists()
    assert producer.main(["--parent-protocol", str(lab.protocol_path), "--output-root", str(lab.output),
                          "--checkpoint-at", lab.runner.utc(2000), "--coverage-source", str(lab.coverage)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["dispatch_performed"] is False
    assert manifest["checkpoint_is_completion"] is False
    assert manifest["schema"] == "oq6.stage-continuation-preparation/v1"


def test_cross_stage_only_finishes_original_never_sent_roles_and_skips_unavailable(lab):
    lab.audit("X0000", stage="cross", status="unanswered")
    lab.audit("X0001", stage="cross")
    write(lab.directory("X0002", stage="cross") / "scorer" / "prompt.txt", b"orphan consumed cross scorer")
    write(lab.directory("X0012", stage="cross") / "cell.json", b"opaque prior unavailable-input finalization")
    snapshot = lab.snapshot(stage="cross")
    assert snapshot["stage"] == "cross"
    assert all(len(worker["cases"]) == 15 for worker in snapshot["workers"].values())
    assert lab.row(snapshot, "X0000")["action"] == "prior_terminal"
    assert lab.row(snapshot, "X0001")["action"] == "resume_scorer"
    assert lab.row(snapshot, "X0002")["action"] == "prior_terminal"
    assert lab.row(snapshot, "X0003")["action"] == "audit"
    assert lab.row(snapshot, "X0012")["action"] == "prior_terminal"
    for cid in ("X0013", "X0014"):
        row = lab.row(snapshot, cid)
        assert row["action"] == "unavailable_input"
        assert row["audit"]["consumed"] is False
        assert row["scorer"]["consumed"] is False
    before = footprint(lab.immutable)
    output = lab.tmp / "v5-cross"
    manifest = lab.prepare(stage="cross", output_root=output)
    assert footprint(lab.immutable) == before
    assert manifest["stage"] == "cross"
    assert manifest["represented_cases_per_worker"] == 15
    for route in manifest["routes"]:
        plan = producer._object(route["plan_path"])
        assert plan["continuation"]["stage"] == "cross"
        assert plan["cases"] == lab.cross
        assert {case["stage"] for case in plan["cases"]} == {"cross"}
        for worker in plan["workers"]:
            assert all(cid.startswith("X") for cid in worker["case_ids"] + worker["prior_case_ids"])
            assert len(worker["case_ids"]) + len(worker["prior_case_ids"]) == 15
            assert not {"X0012", "X0013", "X0014"} & set(worker["case_ids"])
            assert {"X0012", "X0013", "X0014"} <= set(worker["prior_case_ids"])
        if route["route"] == SONNET:
            a, b = plan["workers"]
            assert not {"X0000", "X0002"} & set(a["case_ids"])
            assert a["resume_scorer_case_ids"] == ["X0001"]
            assert not b["resume_scorer_case_ids"]
            assert len(a["case_ids"]) != len(b["case_ids"])
    new_base = output / "calls" / SONNET.replace("/", "--") / "A" / "cross"
    assert (new_base / "X0001" / "audit").is_symlink()
    assert (new_base / "X0001" / "prediction.json").read_bytes() == (
        lab.directory("X0001", stage="cross") / "prediction.json").read_bytes()
    assert {path.name for path in new_base.iterdir()} == {"X0001"}
    assert not (output / "calls" / SONNET.replace("/", "--") / "A" / "dev").exists()


def test_stage_roots_keep_separate_censuses_and_share_provider_locks(lab):
    locks = lab.tmp / "shared-provider-locks"
    before = footprint(lab.immutable)
    dev = lab.prepare(provider_lock_root=locks)
    cross = lab.prepare(stage="cross", output_root=lab.tmp / "v5-cross", provider_lock_root=locks)
    assert dev["raw_root"] != cross["raw_root"]
    assert dev["provider_lock_root"] == cross["provider_lock_root"] == str(locks)
    assert not locks.exists(), "preparation must not acquire provider locks or run workers"
    assert footprint(lab.immutable) == before
    for manifest, stage, count in ((dev, "dev", 399), (cross, "cross", 15)):
        assert manifest["represented_cases_per_worker"] == count
        snapshot = producer._object(manifest["prior_snapshot_path"])
        assert snapshot["stage"] == stage
        for row in manifest["routes"]:
            protocol = producer._object(row["protocol_path"])
            assert protocol["execution"]["provider_lock_root"] == str(locks)
            assert protocol["execution"]["route_order"] == [row["route"]]
            assert protocol["execution"]["raw_root"] == manifest["raw_root"]
            assert protocol["continuation"]["stage"] == stage


def test_dev_snapshot_cannot_be_relabelled_as_cross_intake(lab):
    path = dump(lab.tmp / "dev-snapshot.json", lab.snapshot())
    with pytest.raises(ValueError, match="snapshot"):
        lab.prepare(stage="cross", prior_snapshot_path=path)
    assert not lab.output.exists()


def test_provider_coordination_cannot_mutate_parent_root(lab):
    before = footprint(lab.immutable)
    with pytest.raises(ValueError, match="provider locks"):
        lab.prepare(provider_lock_root=lab.root / "locks")
    assert footprint(lab.immutable) == before
    assert not lab.output.exists()


def test_cross_cli_uses_existing15_and_explicit_shared_lock_root(lab, capsys):
    locks = lab.tmp / "shared-provider-locks"
    assert producer.main(["--parent-protocol", str(lab.protocol_path), "--output-root", str(lab.output),
                          "--checkpoint-at", lab.runner.utc(2000), "--coverage-source", str(lab.coverage),
                          "--stage", "cross", "--provider-lock-root", str(locks)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["stage"] == "cross"
    assert manifest["represented_cases_per_worker"] == 15
    assert manifest["provider_lock_root"] == str(locks)
    assert manifest["dispatch_performed"] is False
    assert manifest["checkpoint_is_completion"] is False


@pytest.mark.parametrize("mutation", ["truncated", "boolean_count", "missing_parse_status"])
def test_incomplete_prediction_parse_metadata_cannot_resume_scorer(lab, mutation):
    lab.audit("D0000")
    path = lab.directory("D0000") / "prediction.json"
    prediction = producer._object(path)
    if mutation == "truncated":
        prediction["raw_findings_count"] = 2
    elif mutation == "boolean_count":
        prediction["raw_findings_count"] = True
    else:
        prediction.pop("parse_error")
    dump(path, prediction)
    assert lab.row(lab.snapshot(), "D0000")["action"] == "prior_terminal"


def test_fully_consumed_arm_is_represented_but_preinitialized_finished(lab):
    for case in lab.dev:
        write(lab.directory(case["case_id"]) / "cell.json", b"opaque immutable final cell")
    manifest = lab.prepare()
    sonnet = next(row for row in manifest["routes"] if row["route"] == SONNET)
    plan = producer._object(sonnet["plan_path"])
    a, b = plan["workers"]
    assert a["case_ids"] == []
    assert len(a["prior_case_ids"]) == 399
    assert a["resume_scorer_case_ids"] == []
    assert len(b["case_ids"]) == 399
    state = producer._object(lab.output / "pending" / (lab.runner.worker_key(SONNET, "A") + ".json"))
    assert state["status"] == "finished"
    assert state["next_case_id"] is None
    assert state["next_index"] == 0
    assert not (lab.output / "calls").exists()


def test_only_experiment_scheduler_is_replaced_when_parent_pins_two_runners(lab):
    original = {row["path"]: row for row in lab.protocol["code_files"]}
    assert len([path for path in original if Path(path).name == "runner.py"]) == 2
    production_pin = copy.deepcopy(original[str(lab.production_runner)])
    before = footprint(lab.immutable)
    manifest = lab.prepare()
    assert footprint(lab.immutable) == before
    for route in manifest["routes"]:
        protocol = producer._object(route["protocol_path"])
        code = {row["path"]: row for row in protocol["code_files"]}
        assert str(lab.scheduling_runner) not in code
        assert code[str(lab.production_runner)] == production_pin
        prospective = str(PRODUCER_PATH.with_name("runner.py"))
        assert code[prospective]["sha256"] == producer._digest(prospective)
        assert {path for path in code if Path(path).name == "runner.py"} == {
            str(lab.production_runner), prospective}
        snapshots = protocol["continuation"]["source_snapshots"]
        production_snapshot = next(row for row in snapshots if row["source_path"] == str(lab.production_runner))
        assert production_snapshot["sha256"] == production_pin["sha256"]
        assert Path(production_snapshot["path"]).read_bytes() == lab.production_runner.read_bytes()


@pytest.mark.parametrize("mutation", ["marker_digest", "marker_path", "request_mismatch", "malformed_marker"])
def test_auditor_marker_custody_drift_is_consumed_terminal_not_resume(lab, mutation):
    lab.audit("D0000", prediction=False)
    directory = lab.directory("D0000") / "audit"
    receipt_path, marker_path = directory / "receipt.json", directory / "call-consumed.json"
    receipt = producer._object(receipt_path)
    if mutation == "marker_digest":
        receipt["call_consumed_sha256"] = "0" * 64
    elif mutation == "marker_path":
        receipt["call_consumed_path"] = str(directory / "different-marker.json")
    else:
        if mutation == "request_mismatch":
            marker = producer._object(marker_path)
            marker["timeout_seconds"] = receipt["timeout_seconds"] - 1
            dump(marker_path, marker)
        else:
            write(marker_path, b"malformed consumed marker")
        # Keep artifact/marker hashes self-consistent, isolating structural or
        # request-identity drift rather than merely an opaque hash mismatch.
        receipt["call_consumed_sha256"] = producer._digest(marker_path)
        receipt["artifacts"]["call-consumed.json"] = {
            "sha256": producer._digest(marker_path), "bytes": marker_path.stat().st_size}
    dump(receipt_path, receipt)
    before = footprint(lab.immutable)
    row = lab.row(lab.snapshot(), "D0000")
    assert row["action"] == "prior_terminal"
    assert row["audit"]["consumed"] is True
    assert footprint(lab.immutable) == before


def test_same_route_native_initial_selection_preserves_scorer_only_resume(lab):
    receipt = lab.audit("D0000", prediction=False)
    receipt["model_change_events"] = [{"source": "native", "row": 1, "pointer": "",
                                       "identity": SONNET, "resolvedModelIsFallback": False}]
    dump(lab.directory("D0000") / "audit" / "receipt.json", receipt)
    before = footprint(lab.immutable)
    manifest = lab.prepare()
    assert footprint(lab.immutable) == before
    snapshot = producer._object(manifest["prior_snapshot_path"])
    assert lab.row(snapshot, "D0000")["action"] == "resume_scorer"
    sonnet = next(row for row in manifest["routes"] if row["route"] == SONNET)
    assert producer._object(sonnet["plan_path"])["workers"][0]["resume_scorer_case_ids"] == ["D0000"]
    directory = lab.output / "calls" / SONNET.replace("/", "--") / "A" / "dev" / "D0000"
    assert (directory / "audit").is_symlink()
    assert not (directory / "scorer").exists()


@pytest.mark.parametrize("mutation", [
    "different_identity", "fallback_event", "fallback_flag", "model_fallback",
    "non_list", "non_dict", "missing_identity", "malformed_fallback_flag",
])
def test_model_change_identity_fallback_and_shape_violations_stay_terminal(lab, mutation):
    receipt = lab.audit("D0000", prediction=False)
    event = {"source": "native", "row": 1, "pointer": "",
             "identity": SONNET, "resolvedModelIsFallback": False}
    receipt["model_change_events"] = [event]
    if mutation == "different_identity":
        event["identity"] = KIMI
    elif mutation == "fallback_event":
        receipt["fallback_events"] = [{"source": "native", "row": 1, "pointer": ""}]
    elif mutation == "fallback_flag":
        event["resolvedModelIsFallback"] = True
    elif mutation == "model_fallback":
        receipt["model_fallback"] = True
    elif mutation == "non_list":
        receipt["model_change_events"] = event
    elif mutation == "non_dict":
        receipt["model_change_events"] = ["malformed native model selection"]
    elif mutation == "missing_identity":
        event.pop("identity")
    else:
        event["resolvedModelIsFallback"] = "false"
    dump(lab.directory("D0000") / "audit" / "receipt.json", receipt)
    assert lab.row(lab.snapshot(), "D0000")["action"] == "prior_terminal"


@pytest.mark.parametrize("errors,expected", [
    ([], "resume_scorer"),
    (["unbound_model_change_identity"], "prior_terminal"),
])
def test_null_model_change_identity_uses_frozen_transport_error_metadata(lab, errors, expected):
    receipt = lab.audit("D0000", prediction=False)
    receipt["model_change_events"] = [{"source": "native", "row": 1, "pointer": "",
                                       "identity": None, "resolvedModelIsFallback": None}]
    receipt["errors"] = errors
    dump(lab.directory("D0000") / "audit" / "receipt.json", receipt)
    assert lab.row(lab.snapshot(), "D0000")["action"] == expected
