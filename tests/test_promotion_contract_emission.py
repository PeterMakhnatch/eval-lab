"""Gate Zero: promotion emits a registry-bound ``benchmark_contract.json``.

Every newly promoted bundle must carry a per-trial contract that the strict
ingestion path (``parse_benchmark_contract`` / ``load_trial_bundle``) accepts,
bound to the explicit task registry — not to path hints, platform claims, or
mutable metadata. The tests here defeat each Gate Zero adversarial probe:

- forged authority: no platform/isolation/allowlist field is ever minted, and
  a decoy ``evidence/*.json`` in the trial cannot override identity or class;
- digest non-validation: every digest is canonical ``sha256:<64 hex>``, at the
  model boundary and again at emission;
- summary override: identity comes only from the trial ``result.json``
  cross-checked against the registry;
- overwrite: a trial that already carries contract authority is refused,
  never replaced (additive-only);
- partial-load: every refusal happens before a byte is written, so a refused
  promotion leaves no destination behind;
- downstream consumption: ``verify_trial_admissibility`` digests the emitted
  contract, and ``load_trial_bundle`` accepts the promoted bundle.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/promote_codex_bundle.py"
PROMOTED_RUNS = ROOT / "research/evidence/runs"

#: The real registered record the positive tests bind against. The binding is
#: proven against the live registry on purpose: if the registry or the task
#: package drifts, this suite fails loudly — that is the contract.
TASK_ID = "event-summary"
NAMESPACE_TASK_NAME = "local-lab/event-summary"
TRIAL_NAME = "event-summary__fixture"


def _load_promoter():
    spec = importlib.util.spec_from_file_location("eval_lab_promote_codex_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROMOTE = _load_promoter()

import evallab.contract_emission as contract_emission  # noqa: E402
from evallab.contract_emission import (  # noqa: E402
    CONTRACT_FILENAME,
    ContractEmissionRefusal,
    atomic_write_bytes,
    plan_contract_emission,
    require_canonical_digest,
)
from evallab.interpretation.benchmark_events import (  # noqa: E402
    load_trial_bundle,
    parse_benchmark_contract,
)
from evallab.registry import TaskRegistry, compute_task_digests  # noqa: E402
from evallab.schemas import TaskDigests  # noqa: E402
from evallab.trial_admissibility import verify_trial_admissibility  # noqa: E402

# ---- fixtures ---------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _result_payload(**overrides: object) -> dict:
    payload = {
        "id": "00000000-0000-0000-0000-00000000fab1",
        "task_name": NAMESPACE_TASK_NAME,
        "trial_name": TRIAL_NAME,
        "task_id": {"path": str(ROOT / "library/tasks/event-summary")},
        "agent_info": {"name": "fixture-agent"},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-09-01T12:00:00Z",
        "finished_at": "2026-09-01T12:01:00Z",
    }
    payload.update(overrides)
    return payload


def _make_source_job(tmp_path: Path, *, result: dict | None = None) -> Path:
    """A Harbor-shaped source job whose single trial binds to the live registry."""
    job = tmp_path / "fixture-job"
    trial = job / TRIAL_NAME
    trial.mkdir(parents=True)
    _write_json(trial / "result.json", result if result is not None else _result_payload())
    _write_json(
        trial / "agent" / "trajectory.json",
        {"schema_version": "1.0.0", "session_id": "fixture", "steps": []},
    )
    _write_json(trial / "verifier" / "result.json", {"passed": True, "reward": 1.0})
    (trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    events = [
        {
            "event_index": 0,
            "timestamp": "2026-09-01T12:00:30Z",
            "event_type": "mcp_call",
            "payload": {"tool_call_id": "call_1", "tool_name": "read_chunk", "arguments": {}},
        },
        {
            "event_index": 1,
            "timestamp": "2026-09-01T12:00:31Z",
            "event_type": "tool_call_success",
            "payload": {"tool_call_id": "call_1", "result": {"ok": True}},
        },
    ]
    (trial / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    _write_json(
        trial / "final-state.json",
        {"trial_id": TRIAL_NAME, "status": "executed", "invariants_passed": True},
    )
    return job


def _promoted_contract_path(destination: Path) -> Path:
    return destination / TRIAL_NAME / CONTRACT_FILENAME


def _c1_entries(destination: Path) -> list[dict]:
    manifest = json.loads((destination / "PROMOTION.json").read_text(encoding="utf-8"))
    return [e for e in manifest["files"] if e.get("rule") == "C1"]


def _fabricate_task_package(repo: Path, task_id: str) -> Path:
    package = repo / "library" / "tasks" / task_id
    (package / "environment").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "task.toml").write_text(f'[task]\nname = "{task_id}"\n', encoding="utf-8")
    (package / "instruction.md").write_text("do the fixture thing\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (package / "tests" / "test_fixture.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return package


def _control_evidence_payload(task_id: str, digests: TaskDigests, reward: float) -> dict:
    return {
        "job_name": "fixture-job",
        "trial_name": f"{task_id}__oracle" if reward == 1.0 else f"{task_id}__nop",
        "reward": reward,
        "evidence_path": f"research/evidence/runs/fixture-job/{task_id}__oracle",
        "evidence_digest": "sha256:" + "a" * 64,
        "lock_digest": "sha256:" + "b" * 64,
        "observed_at": "2026-09-01T00:00:00Z",
        "task_id": task_id,
        "task_version": "0.1.0",
        "task_digests": digests.model_dump(mode="json"),
        "harbor_task_digest": "sha256:" + "c" * 64,
    }


def _write_registry_record(repo: Path, payload: dict) -> None:
    _write_json(repo / "library" / "registry" / f"{payload['task_id']}.json", payload)


def _write_trial_for(repo: Path, task_id: str) -> Path:
    job = repo / "runs" / "fixture-job"
    trial = job / f"{task_id}__agent"
    trial.mkdir(parents=True)
    _write_json(
        trial / "result.json",
        {
            "id": "00000000-0000-0000-0000-00000000fab2",
            "task_name": task_id,
            "trial_name": trial.name,
            "task_id": {"path": str(repo / "library" / "tasks" / task_id)},
            "finished_at": "2026-09-01T12:01:00Z",
        },
    )
    return job


# ---- the point: promotion emits a loadable, registry-bound contract ----------


def test_promotion_emits_registry_bound_contract_that_the_real_loader_accepts(
    tmp_path: Path,
) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name

    PROMOTE.promote(job, destination)

    contract_path = _promoted_contract_path(destination)
    assert contract_path.is_file(), "every newly promoted trial must carry the contract"
    record = TaskRegistry.from_repo(ROOT).get(TASK_ID)
    assert record is not None and record.state == "registered"

    parsed = parse_benchmark_contract(contract_path)
    assert parsed.task_id == TASK_ID
    assert parsed.task_id_explicit is True
    assert parsed.family == record.task_family
    assert parsed.version == record.version
    assert parsed.verifier_truth_digest == record.digests.verifier
    # No runtime authority was minted into the contract.
    for forbidden in (
        "host_platform",
        "network_isolation_enforced",
        "analysis_eligibility",
        "evidence_class",
        "task_runtime_identity",
        "trial_admissibility",
    ):
        assert forbidden not in parsed.cell_factors
        assert forbidden not in contract_path.read_text(encoding="utf-8")

    bundle = load_trial_bundle(destination / TRIAL_NAME)
    assert bundle.contract.task_id == TASK_ID
    assert bundle.contract.verifier_truth_digest == record.digests.verifier


def test_downstream_admissibility_digests_the_emitted_contract(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name
    PROMOTE.promote(job, destination)

    verified = verify_trial_admissibility(
        trial_dir=destination / TRIAL_NAME,
        trial_id=TRIAL_NAME,
        provenance=None,
        repo_root=ROOT,
    )
    expected = "sha256:" + hashlib.sha256(
        _promoted_contract_path(destination).read_bytes()
    ).hexdigest()
    assert verified.record.source_digests.contract == expected


def test_the_emitted_contract_is_canonical_and_deterministic(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    first = tmp_path / "bundles" / "first"
    second = tmp_path / "bundles" / "second"
    PROMOTE.promote(job, first)
    PROMOTE.promote(job, second)

    a = _promoted_contract_path(first).read_bytes()
    b = _promoted_contract_path(second).read_bytes()
    assert a == b, "re-emission must be byte-identical, not merely equivalent"
    assert parse_benchmark_contract(a.decode("utf-8")).task_id == TASK_ID


def test_the_manifest_records_the_binding_and_the_verifier_accepts_it(
    tmp_path: Path,
) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name
    PROMOTE.promote(job, destination)

    entries = _c1_entries(destination)
    assert len(entries) == 1
    entry = entries[0]
    contract_path = _promoted_contract_path(destination)
    body = contract_path.read_bytes()
    assert entry["promoted_sha256"] == f"sha256:{hashlib.sha256(body).hexdigest()}"
    assert entry["action"] == "emitted"
    assert entry["registry_task_id"] == TASK_ID
    assert entry["promoted_path"] == f"{TRIAL_NAME}/{CONTRACT_FILENAME}"
    record = TaskRegistry.from_repo(ROOT).get(TASK_ID)
    assert entry["certified_runtime_package_digest"] == record.digests.package
    assert entry["certified_environment_digest"] == record.digests.environment
    assert entry["trial_run_id"] == _result_payload()["id"]
    assert list(contract_path.parent.glob(f".{CONTRACT_FILENAME}.tmp-*")) == []

    # The script's own parent-free verifier must accept the new artifact.
    assert PROMOTE.verify(destination.parent) == 0


# ---- adversarial probes ------------------------------------------------------


def test_a_decoy_metadata_file_cannot_override_identity_or_class(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    trial = job / TRIAL_NAME
    _write_json(
        trial / "evidence-summary.json",
        {
            "task_id": "query-optimize",
            "task_name": "query-optimize",
            "benchmark_family": "family_c_fault_recovery",
            "evidence_class": "causal",
            "verifier_truth_digest": "sha256:" + "f" * 64,
        },
    )
    destination = tmp_path / "bundles" / job.name

    PROMOTE.promote(job, destination)

    parsed = parse_benchmark_contract(_promoted_contract_path(destination))
    assert parsed.task_id == TASK_ID
    assert parsed.family == TaskRegistry.from_repo(ROOT).get(TASK_ID).task_family
    assert parsed.verifier_truth_digest != "sha256:" + "f" * 64


def test_a_trial_already_carrying_contract_authority_is_never_replaced(
    tmp_path: Path,
) -> None:
    job = _make_source_job(tmp_path)
    _write_json(
        job / TRIAL_NAME / CONTRACT_FILENAME,
        {"benchmark_family": "pre-existing-authority"},
    )
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="existing_contract_present"):
        PROMOTE.promote(job, destination)
    assert not destination.exists(), "a refused promotion must write nothing"

def test_atomic_publish_never_replaces_an_existing_contract(tmp_path: Path) -> None:
    target = tmp_path / CONTRACT_FILENAME
    target.write_bytes(b"existing-contract-authority")

    with pytest.raises(ContractEmissionRefusal, match="contract_publish_overwrite"):
        atomic_write_bytes(target, b"replacement")
    assert target.read_bytes() == b"existing-contract-authority"

def test_missing_task_identity_refuses(tmp_path: Path) -> None:
    result = _result_payload()
    del result["task_name"]
    job = _make_source_job(tmp_path, result=result)
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="missing_task_identity"):
        PROMOTE.promote(job, destination)
    assert not destination.exists()


def test_an_unregistered_task_refuses(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path, result=_result_payload(task_name="local-lab/no-such-task"))
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="task_not_in_registry"):
        PROMOTE.promote(job, destination)
    assert not destination.exists()


def test_a_contradicted_task_path_refuses(tmp_path: Path) -> None:
    job = _make_source_job(
        tmp_path,
        result=_result_payload(
            **{"task_id": {"path": str(ROOT / "library/tasks/query-optimize")}}
        ),
    )
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="task_path_mismatch"):
        PROMOTE.promote(job, destination)
    assert not destination.exists()

def test_a_symlinked_trial_result_refuses_before_any_dereference(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    trial = job / TRIAL_NAME
    result = trial / "result.json"
    external = tmp_path / "outside-result.json"
    external.write_text(result.read_text(encoding="utf-8"), encoding="utf-8")
    result.unlink()
    result.symlink_to(external)
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="symlinked_trial_result"):
        PROMOTE.promote(job, destination)
    assert not destination.exists()


def test_a_non_registered_admission_state_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = _fabricate_task_package(repo, "fixture-candidate-task")
    digests = compute_task_digests(package)
    _write_registry_record(
        repo,
        {
            "task_id": "fixture-candidate-task",
            "task_family": "fixture-family",
            "version": "0.1.0",
            "task_path": "library/tasks/fixture-candidate-task",
            "digests": digests.model_dump(mode="json"),
            "source_uri": "file://fixture",
            "provenance_zone": "03-synthetic",
            "is_synthetic": True,
            "state": "candidate",
            "state_reason": "fixture_candidate",
            "allowed_uses": ["canary"],
        },
    )
    job = _write_trial_for(repo, "fixture-candidate-task")

    with pytest.raises(ContractEmissionRefusal, match="registry_state_not_registered"):
        plan_contract_emission(job, repo)
    assert not (repo / "bundles").exists()


def test_a_drifted_task_package_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = _fabricate_task_package(repo, "fixture-drift-task")
    digests = compute_task_digests(package)
    evidence = _control_evidence_payload("fixture-drift-task", digests, 1.0)
    nop = _control_evidence_payload("fixture-drift-task", digests, 0.0)
    _write_registry_record(
        repo,
        {
            "task_id": "fixture-drift-task",
            "task_family": "fixture-family",
            "version": "0.1.0",
            "task_path": "library/tasks/fixture-drift-task",
            "digests": digests.model_dump(mode="json"),
            "source_uri": "file://fixture",
            "provenance_zone": "03-synthetic",
            "is_synthetic": True,
            "state": "registered",
            "allowed_uses": ["measurement"],
            "control_evidence": {"oracle": evidence, "nop": nop},
            "approved_by": "fixture-approver",
            "approved_at": "2026-09-01T00:00:00Z",
        },
    )
    job = _write_trial_for(repo, "fixture-drift-task")

    # Before the tamper the binding proves out end-to-end on the fabricated repo.
    plans = plan_contract_emission(job, repo)
    assert [p.task_id for p in plans] == ["fixture-drift-task"]

    # After the tamper the registered digests no longer hold: refuse.
    with (package / "task.toml").open("a", encoding="utf-8") as handle:
        handle.write("# drifted\n")
    with pytest.raises(ContractEmissionRefusal, match="registry_package_drift"):
        plan_contract_emission(job, repo)


def test_noncanonical_digests_are_rejected_everywhere() -> None:
    with pytest.raises(ContractEmissionRefusal, match="noncanonical_digest"):
        require_canonical_digest("sha256:x", "registry digests.verifier")
    with pytest.raises(ContractEmissionRefusal, match="noncanonical_digest"):
        require_canonical_digest("sha256:" + "A" * 64, "uppercase hex")
    with pytest.raises(ContractEmissionRefusal, match="noncanonical_digest"):
        require_canonical_digest(123, "not even a string")
    # The model boundary rejects the same probe, so a tampered registry record
    # cannot even load.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskDigests(
            task_toml="sha256:" + "0" * 64,
            instruction="sha256:" + "0" * 64,
            environment="sha256:" + "0" * 64,
            verifier="sha256:x",
            package="sha256:" + "0" * 64,
        )



# ---- publication and source-boundary failure atomicity -----------------------


def _assert_no_staging(destination: Path) -> None:
    assert not list(destination.parent.glob(f".{destination.name}.staging-*"))


def test_existing_destination_is_never_replaced(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name
    destination.mkdir(parents=True)
    marker = destination / "keep"
    marker.write_bytes(b"immutable")

    with pytest.raises(SystemExit, match="destination_exists"):
        PROMOTE.promote(job, destination)

    assert marker.read_bytes() == b"immutable"
    _assert_no_staging(destination)


def test_inside_source_destination_refuses_without_creating_parent(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    destination = job / "new-parent" / "bundle"

    with pytest.raises(SystemExit, match="destination_path_escape"):
        PROMOTE.promote(job, destination)

    assert not destination.parent.exists()


def test_successful_publish_fsyncs_public_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name
    calls: list[Path] = []
    original = PROMOTE._fsync_directory

    def record(path: Path) -> None:
        calls.append(path)
        original(path)

    monkeypatch.setattr(PROMOTE, "_fsync_directory", record)
    PROMOTE.promote(job, destination)

    assert calls[-1] == destination.parent.absolute()


@pytest.mark.parametrize("failure", ("copy", "contract", "verify"))
def test_render_failure_never_publishes_or_leaks_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    job = _make_source_job(tmp_path)
    destination = tmp_path / "bundles" / job.name
    if failure == "copy":
        original = Path.write_bytes

        def fail_staged_copy(path: Path, body: bytes) -> int:
            if f".{destination.name}.staging-" in str(path):
                raise OSError("injected staged copy failure")
            return original(path, body)

        monkeypatch.setattr(Path, "write_bytes", fail_staged_copy)
    elif failure == "contract":
        monkeypatch.setattr(
            contract_emission,
            "atomic_write_bytes",
            lambda *_: (_ for _ in ()).throw(OSError("injected contract failure")),
        )
    else:
        monkeypatch.setattr(
            PROMOTE,
            "_verify_staged_bundle",
            lambda *_: (_ for _ in ()).throw(SystemExit("injected verify failure")),
        )

    with pytest.raises((OSError, SystemExit), match="injected"):
        PROMOTE.promote(job, destination)

    assert not destination.exists()
    _assert_no_staging(destination)


def test_destination_symlinks_and_parent_symlinks_are_refused(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="symlinked_destination_parent"):
        PROMOTE.promote(job, linked_parent / job.name)
    assert not (outside / job.name).exists()

    destination = tmp_path / "bundles" / job.name
    destination.parent.mkdir()
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match="symlinked_destination"):
        PROMOTE.promote(job, destination)
    assert not list(outside.iterdir())


def test_trial_evidence_directory_without_result_refuses(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path)
    (job / "incomplete-trial" / "agent").mkdir(parents=True)
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="missing_trial_result"):
        PROMOTE.promote(job, destination)

    assert not destination.exists()


def test_only_local_lab_namespace_may_resolve_a_registry_task(tmp_path: Path) -> None:
    job = _make_source_job(
        tmp_path, result=_result_payload(task_name=f"evil/{TASK_ID}")
    )
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="task_not_in_registry"):
        PROMOTE.promote(job, destination)

    assert not destination.exists()


def test_local_lab_identity_requires_a_runtime_task_path(tmp_path: Path) -> None:
    job = _make_source_job(tmp_path, result=_result_payload(task_id=None))
    destination = tmp_path / "bundles" / job.name

    with pytest.raises(SystemExit, match="namespaced_task_path_missing"):
        PROMOTE.promote(job, destination)

    assert not destination.exists()


def test_result_parse_and_identity_hash_share_one_nofollow_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _make_source_job(tmp_path)
    result_path = job / TRIAL_NAME / "result.json"
    original_bytes = result_path.read_bytes()
    original_read = contract_emission._read_trial_result

    def swap_after_read(path: Path) -> tuple[dict, bytes]:
        payload, raw = original_read(path)
        path.write_text(json.dumps(_result_payload(task_name="local-lab/query-optimize")), encoding="utf-8")
        return payload, raw

    monkeypatch.setattr(contract_emission, "_read_trial_result", swap_after_read)
    [plan] = plan_contract_emission(job, ROOT)

    assert plan.identity_source_sha256 == "sha256:" + hashlib.sha256(original_bytes).hexdigest()
    assert plan.task_id == TASK_ID


def test_promotion_refuses_identity_change_after_contract_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _make_source_job(tmp_path)
    result_path = job / TRIAL_NAME / "result.json"
    destination = tmp_path / "bundles" / job.name
    original_plan = PROMOTE._plan_contracts

    def mutate_after_plan(job_dir: Path) -> list:
        plans = original_plan(job_dir)
        result_path.write_text(
            json.dumps(_result_payload(task_name="local-lab/query-optimize")),
            encoding="utf-8",
        )
        return plans

    monkeypatch.setattr(PROMOTE, "_plan_contracts", mutate_after_plan)

    with pytest.raises(SystemExit, match="identity_source_changed"):
        PROMOTE.promote(job, destination)

    assert not destination.exists()
    _assert_no_staging(destination)

# ---- committed evidence: legacy bundles stay untouched -----------------------


def test_committed_bundles_predate_contract_emission_and_gain_no_emitted_entries() -> None:
    """Legacy bundles are immutable: contract emission is new-promotion-only."""
    for manifest_path in sorted(PROMOTED_RUNS.glob("*/PROMOTION.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        emitted = [e for e in manifest["files"] if e.get("rule") == "C1"]
        assert emitted == [], (
            f"{manifest_path.parent.name} predates contract emission; a committed "
            "legacy bundle must never gain emitted entries"
        )
