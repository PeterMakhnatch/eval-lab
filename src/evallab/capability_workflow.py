"""Deterministic, offline M052 integration over four bounded evidence components."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.capability_contract import (
    ArtifactKind,
    BoundArtifactRef,
    CapabilityClaimSpec,
    CapabilityContractReport,
    CapabilityContractSpec,
    ClaimKind,
    IntegrationCostLedger,
    evaluate_capability_contract,
)
from evallab.curve import build_curve, load_curve_spec
from evallab.registry import (
    certification_envelope_from_packet,
    compute_task_digests,
    verify_certification_packet,
)
from evallab.results import load_job
from evallab.schemas import TaskRegistryRecord
from evallab.state_events import load_state_event_facts
from evallab.task_workbench import (
    CandidateSource,
    check_candidate,
    inspect_candidate,
    run_controls,
    write_packet,
)
from evallab.upstream_adapter import import_upstream_file, load_adapter_manifest

JsonObject = dict[str, Any]
_EXPERIMENT = "research/experiments/capability-contracts/free-integrated.json"
_FIXED_TIME = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
_M052_NEW_MODULES = (
    "src/evallab/capability_contract.py",
    "src/evallab/capability_workflow.py",
)
_M052_SCREEN = "src/evallab/screen.py"
_M052_BLOCK_BEGIN = "# M052-INTEGRATION-BEGIN"
_M052_BLOCK_END = "# M052-INTEGRATION-END"


def _canonical(value: Any) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return f"{encoded}\n".encode()


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    return path


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _count_physical_loc(content: bytes) -> int:
    return sum(
        1
        for line in content.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _count_integration_loc(content: bytes) -> int:
    count = 0
    blocks = 0
    inside = False
    for line in content.decode("utf-8").splitlines():
        stripped = line.strip()
        if stripped == _M052_BLOCK_BEGIN:
            if inside:
                raise ValueError("nested M052 integration block")
            inside = True
            blocks += 1
            continue
        if stripped == _M052_BLOCK_END:
            if not inside:
                raise ValueError("unmatched M052 integration block end")
            inside = False
            continue
        if inside and stripped and not line.lstrip().startswith("#"):
            count += 1
    if inside or blocks == 0:
        raise ValueError("screen.py has an incomplete M052 integration block")
    return count


def _integration_measurement(
    repo_root: Path,
) -> tuple[int, int, list[JsonObject]]:
    revisions: list[JsonObject] = []
    added_loc = 0
    for relative in _M052_NEW_MODULES:
        path = _inside(repo_root, repo_root / relative, "M052 production source")
        content = path.read_bytes()
        loc = _count_physical_loc(content)
        added_loc += loc
        revisions.append({
            "path": relative,
            "sha256": _sha256(path),
            "measurement": "new_module_nonblank_noncomment_physical_lines",
            "loc": loc,
        })
    screen = _inside(repo_root, repo_root / _M052_SCREEN, "M052 screen integration")
    screen_content = screen.read_bytes()
    modified_loc = _count_integration_loc(screen_content)
    revisions.append({
        "path": _M052_SCREEN,
        "sha256": _sha256(screen),
        "measurement": "delimited_integration_nonblank_noncomment_physical_lines",
        "loc": modified_loc,
    })
    return added_loc, modified_loc, revisions


def _inside(root: Path, path: Path, label: str) -> Path:
    root = root.resolve()
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes repository") from exc
    return resolved


def _load_experiment(repo_root: Path) -> JsonObject:
    path = _inside(repo_root, repo_root / _EXPERIMENT, "experiment specification")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("experiment_id") != "m052-free-integrated":
        raise ValueError("unexpected free capability experiment identity")
    return value


def _load_fixture_backend(repo_root: Path, relative: str) -> Any:
    path = _inside(repo_root, repo_root / relative, "workbench fixture backend")
    spec = importlib.util.spec_from_file_location("evallab_m052_free_fixture_backend", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load workbench fixture backend")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FreeFixtureBackend()


def _curve_evidence(repo_root: Path, output_dir: Path, experiment: JsonObject) -> Path:
    spec_path = _inside(repo_root, repo_root / str(experiment["curve_spec"]), "curve spec")
    curve = build_curve(load_curve_spec(spec_path), repo_root=repo_root,
                        produced_by="evallab.capability_workflow", produced_at=_FIXED_TIME)
    return _write_json(output_dir / "curve/report.json", curve.model_dump(mode="json"))


def _workbench_evidence(
    repo_root: Path, output_dir: Path, experiment: JsonObject
) -> tuple[Path, Path]:
    task = _inside(repo_root, repo_root / str(experiment["workbench_task"]), "workbench task")
    source = CandidateSource(
        source_uri="local/uppercase-fixture",
        source_ref="local/uppercase-fixture@1.0.0",
        license="MIT",
        provenance_zone="02-local-evidence",
    )
    inspection = inspect_candidate(repo_root=repo_root, task_path=task, source=source)
    candidate_id = str(inspection.candidate["candidate_id"])
    control_run_root = repo_root / "runs/task-workbench" / candidate_id
    control_run_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        control_run_root.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to replace preexisting workbench controls: {control_run_root}"
        ) from exc
    try:
        controls = run_controls(
            inspection=inspection,
            repo_root=repo_root,
            task_path=task,
            backend=_load_fixture_backend(repo_root, str(experiment["workbench_backend"])),
            run_root=control_run_root,
        )
        checked = check_candidate(
            inspection=inspection, controls=controls, repo_root=repo_root
        )
        _, certification_path = write_packet(
            repo_root=repo_root,
            report=checked,
            output_root=output_dir / "workbench/packet",
        )
    finally:
        if control_run_root.exists():
            shutil.rmtree(control_run_root)
    digests = compute_task_digests(task)
    task_path = task.relative_to(repo_root).as_posix()
    envelope = certification_envelope_from_packet(
        repo_root,
        certification_path,
        task_id="uppercase-fixture",
        task_version="1.0.0",
        task_path=task_path,
        package_digest=digests.package,
    )
    record = TaskRegistryRecord(
        task_id="uppercase-fixture",
        version="1.0.0",
        task_path=task_path,
        digests=digests,
        source_uri=source.source_uri,
        source_ref=source.source_ref,
        license=source.license,
        provenance_zone=source.provenance_zone,
        is_synthetic=True,
        certification=envelope,
        state="candidate",
        state_reason="certificate_bound_pending_human_admission",
        allowed_uses=["measurement"],
    )
    verify_certification_packet(repo_root, record)
    registry = _write_json(
        output_dir / "workbench/registry-record.json", record.model_dump(mode="json")
    )
    return certification_path, registry


def _state_evidence(repo_root: Path, output_dir: Path, experiment: JsonObject) -> Path:
    job = output_dir / "state/job"
    trial = job / "sample-task__abc123"
    _write_json(job / "config.json", {"job_name": "m052-state-event-job"})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(job / "result.json", {"id": "00000000-0000-0000-0000-000000000052",
        "started_at": "2026-08-24T12:00:00Z", "finished_at": "2026-08-24T12:00:02Z",
        "n_total_trials": 1, "stats": {"n_completed_trials": 1, "n_errored_trials": 0}})
    _write_json(trial / "config.json", {"agent": {"name": "oracle"}})
    _write_json(trial / "lock.json", {"schema_version": 2})
    _write_json(trial / "result.json", {"id": "00000000-0000-0000-0000-000000000053",
        "trial_name": trial.name, "task_name": "local-lab/sample-task", "task_checksum": "abc",
        "started_at": "2026-08-24T12:00:00Z", "finished_at": "2026-08-24T12:00:01Z",
        "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
        "agent_result": {"n_input_tokens": None, "n_cache_tokens": None,
                         "n_output_tokens": None, "cost_usd": None},
        "verifier_result": {"rewards": {"reward": 1.0}}, "exception_info": None})
    fixture = _inside(
        repo_root,
        repo_root / str(experiment["state_journal"]),
        "state journal fixture",
    )
    shutil.copytree(fixture, trial / "state-journal")
    loaded = load_job(job)
    facts = load_state_event_facts(loaded.trials[0], job_id=str(loaded.id),
                                   experiment_id=str(experiment["experiment_id"]))
    return _write_json(output_dir / "state/facts.json", [asdict(fact) for fact in facts])


def _upstream_evidence(repo_root: Path, output_dir: Path, experiment: JsonObject) -> Path:
    summaries: list[JsonObject] = []
    for entry in experiment["upstream_imports"]:
        manifest_path = _inside(repo_root, repo_root / entry["manifest"], "adapter manifest")
        manifest = load_adapter_manifest(manifest_path, repo_root)
        source = _inside(repo_root, repo_root / entry["source"], "upstream fixture")
        source_root = _inside(repo_root, repo_root / entry["source_root"], "upstream source root")
        imported = import_upstream_file(
            source, output_dir / "upstream/imports" / entry["name"], manifest_path, repo_root,
            source_root=source_root, source_revision=manifest.upstream.revision,
            accepted_licenses=frozenset({entry["accepted_license"]}))
        summaries.append({"name": entry["name"], "revision": imported.revision,
            "raw_path": imported.raw_path.relative_to(repo_root).as_posix(),
            "raw_sha256": _sha256(imported.raw_path),
            "evidence_path": imported.evidence_path.relative_to(repo_root).as_posix(),
            "evidence_sha256": _sha256(imported.evidence_path),
            "atif_path": (
                imported.atif_path.relative_to(repo_root).as_posix()
                if imported.atif_path
                else None
            ),
        })
    return _write_json(output_dir / "upstream/summary.json", summaries)


def _artifact(repo_root: Path, path: Path, *, kind: ArtifactKind) -> BoundArtifactRef:
    return BoundArtifactRef.bind(
        repo_root=repo_root, path=path.relative_to(repo_root).as_posix(), kind=kind
    )


def run_free_capability_workflow(*, repo_root: Path, output_dir: Path) -> CapabilityContractReport:
    """Run the committed free fixtures without network, containers, or model calls."""
    repo_root = repo_root.resolve()
    output_dir = _inside(repo_root, output_dir, "output directory")
    packet_root = (repo_root / "research/registration/candidates").resolve()
    try:
        output_dir.relative_to(packet_root)
    except ValueError as exc:
        raise ValueError(
            "output directory must be under research/registration/candidates "
            "so the bound M049 packet remains verifiable"
        ) from exc
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite capability evidence: {output_dir}")
    output_dir.mkdir(parents=True)
    experiment = _load_experiment(repo_root)
    curve_path = _curve_evidence(repo_root, output_dir, experiment)
    workbench_path, registry_path = _workbench_evidence(repo_root, output_dir, experiment)
    state_path = _state_evidence(repo_root, output_dir, experiment)
    upstream_path = _upstream_evidence(repo_root, output_dir, experiment)
    curve_ref = _artifact(repo_root, curve_path, kind="curve_report")
    workbench_ref = _artifact(
        repo_root, workbench_path, kind="workbench_certificate"
    )
    registry_ref = _artifact(repo_root, registry_path, kind="task_registry_record")
    state_ref = _artifact(repo_root, state_path, kind="state_event_facts")
    upstream_ref = _artifact(repo_root, upstream_path, kind="upstream_imports")


    added_loc, modified_loc, source_revisions = _integration_measurement(repo_root)
    ledger_values = {
        "raw_dependencies": ["M047", "M049", "M048", "M051"],
        "added_loc": added_loc,
        "modified_loc": modified_loc,
        "environment_specific_symbols": ["FreeFixtureBackend"],
        "prompt_tokens": 0,
        "revisions": len(source_revisions),
        "post_trace_fixes": 0,
    }
    ledger_path = _write_json(
        output_dir / "contract-inputs/integration-ledger.json",
        {
            **ledger_values,
            "measurement_units": {
                "added_loc": (
                    "nonblank/noncomment physical lines in M052-owned new "
                    "production modules"
                ),
                "modified_loc": (
                    "nonblank/noncomment physical lines inside explicit "
                    "M052-INTEGRATION blocks in pre-existing production modules"
                ),
                "prompt_tokens": "exact count; free workflow issues no model calls",
                "revisions": (
                    "one current sha256-bound source snapshot per measured "
                    "production path"
                ),
                "post_trace_fixes": (
                    "exact count of fixes after the first workflow trace"
                ),
            },
            "source_revisions": source_revisions,
        },
    )
    ledger_ref = _artifact(repo_root, ledger_path, kind="integration_ledger")
    ledger = IntegrationCostLedger(artifact=ledger_ref, **ledger_values)
    limitations = [
        "component evidence is bounded",
        "no substantive generality",
        "generated tasks remain uncertified (F-SEQGEN-1)",
    ]


    claims = [
        CapabilityClaimSpec(
            kind=ClaimKind.P,
            availability="available",
            statement="Protocol portability is not established by the free component fixture.",
            limitations=limitations,
            evidence=[curve_ref, workbench_ref],
            declared_factor="harness",
        ),
        CapabilityClaimSpec(
            kind=ClaimKind.R,
            availability="available",
            statement="Reliability across frozen domains and environments is not established.",
            limitations=limitations,
            evidence=[curve_ref, state_ref],
            inferential_outcome="inconclusive",
        ),
        CapabilityClaimSpec(
            kind=ClaimKind.U,
            availability="available",
            statement="Unfamiliar-environment adaptation is not established.",
            limitations=limitations,
            evidence=[upstream_ref, registry_ref],
        ),
        CapabilityClaimSpec(
            kind=ClaimKind.C,
            availability="available",
            statement="Continual learning across frozen longitudinal phases is not established.",
            limitations=limitations,
            evidence=[state_ref],
        ),
        CapabilityClaimSpec(
            kind=ClaimKind.Y,
            availability="available",
            statement="Production reliability is not established by fixture controls.",
            limitations=limitations,
            evidence=[workbench_ref, upstream_ref],
            integration_cost=ledger,
        ),
    ]
    contract = CapabilityContractSpec(
        experiment_id=str(experiment["experiment_id"]),
        claims=claims,
        authoring_identity_inputs=[],
        tuning_identity_inputs=[],
    )
    _write_json(output_dir / "contract-spec.json", contract.model_dump(mode="json"))
    return evaluate_capability_contract(contract, repo_root=repo_root)
