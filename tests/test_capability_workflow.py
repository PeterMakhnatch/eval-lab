from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import evallab.capability_workflow as capability_workflow
from evallab.capability_contract import (
    BoundArtifactRef,
    CapabilityClaimSpec,
    CapabilityContractSpec,
    ClaimKind,
    evaluate_capability_contract,
)
from evallab.capability_workflow import run_free_capability_workflow

ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    copies = (
        "tests/fixtures/curve",
        "tests/fixtures/task_workbench/valid",
        "tests/fixtures/capability_workflow",
        "tests/fixtures/state_events",
        "tests/fixtures/upstream_adapters",
        "library/adapters",
        "research/experiments/capability-contracts",
    )
    for relative in copies:
        shutil.copytree(ROOT / relative, repo / relative)
    source_files = (
        "src/evallab/upstream_adapter.py",
        "src/evallab/capability_contract.py",
        "src/evallab/capability_workflow.py",
        "src/evallab/screen.py",
    )
    for relative in source_files:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return repo


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _output(repo: Path) -> Path:
    return repo / "research/registration/candidates/m052-free-integrated"


def _spec(repo: Path) -> CapabilityContractSpec:
    return CapabilityContractSpec.model_validate_json(
        (_output(repo) / "contract-spec.json").read_bytes()
    )


def _bound_json(
    repo: Path,
    relative: str,
    kind: str,
    payload: dict[str, object],
) -> BoundArtifactRef:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")
    return BoundArtifactRef.bind(  # type: ignore[arg-type]
        repo_root=repo, path=relative, kind=kind
    )


def _physical_loc(path: Path) -> int:
    return sum(
        1
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _integration_loc(path: Path) -> int:
    count = 0
    inside = False
    blocks = 0
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped == "# M052-INTEGRATION-BEGIN":
            assert not inside
            inside = True
            blocks += 1
        elif stripped == "# M052-INTEGRATION-END":
            assert inside
            inside = False
        elif inside and stripped and not line.lstrip().startswith("#"):
            count += 1
    assert blocks > 0
    assert not inside
    return count


def test_free_workflow_is_deterministic_bounded_and_uses_all_components(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    output = _output(repo)
    report = run_free_capability_workflow(repo_root=repo, output_dir=output)
    control_runs = repo / "runs/task-workbench"
    ledger = json.loads(
        (output / "contract-inputs/integration-ledger.json").read_bytes()
    )
    expected_added = sum(
        _physical_loc(repo / relative)
        for relative in (
            "src/evallab/capability_contract.py",
            "src/evallab/capability_workflow.py",
        )
    )
    expected_modified = _integration_loc(repo / "src/evallab/screen.py")
    assert ledger["added_loc"] == expected_added > 0
    assert ledger["modified_loc"] == expected_modified > 0
    assert ledger["revisions"] == len(ledger["source_revisions"]) == 3
    assert ledger["prompt_tokens"] == 0
    assert ledger["post_trace_fixes"] == 0
    for revision in ledger["source_revisions"]:
        content = (repo / revision["path"]).read_bytes()
        assert revision["sha256"] == f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert not control_runs.exists() or not any(control_runs.iterdir())
    first = _tree_bytes(output)
    shutil.rmtree(output)
    rerun = run_free_capability_workflow(repo_root=repo, output_dir=output)
    assert not control_runs.exists() or not any(control_runs.iterdir())

    assert _tree_bytes(output) == first
    assert rerun == report
    assert report.status == "valid_insufficient"
    assert report.refuse_substantive_generality is True
    assert [claim.kind for claim in report.claims] == list(ClaimKind)
    assert {ref.kind for claim in report.claims for ref in claim.evidence} >= {
        "curve_report",
        "workbench_certificate",
        "state_event_facts",
        "upstream_imports",
    }
    for claim in report.claims:
        assert claim.status in {"insufficient", "unavailable"}
        assert "component evidence is bounded" in claim.reasons
        assert "no substantive generality" in claim.reasons
        assert "generated tasks remain uncertified (F-SEQGEN-1)" in claim.reasons


def test_free_workflow_refuses_and_preserves_preexisting_candidate_runs(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    output = _output(repo)
    run_free_capability_workflow(repo_root=repo, output_dir=output)
    packet_dirs = list((output / "workbench/packet").iterdir())
    assert len(packet_dirs) == 1
    candidate_run = repo / "runs/task-workbench" / packet_dirs[0].name
    marker = candidate_run / "user-data.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("retain\n")
    shutil.rmtree(output)

    with pytest.raises(FileExistsError, match="preexisting workbench controls"):
        run_free_capability_workflow(repo_root=repo, output_dir=output)

    assert marker.read_text() == "retain\n"


def test_free_workflow_executes_real_dependency_apis(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    calls = {
        "build_curve": 0,
        "inspect_candidate": 0,
        "load_state_event_facts": 0,
        "import_upstream_file": 0,
    }
    originals = {name: getattr(capability_workflow, name) for name in calls}

    def tracking(name):
        def call(*args, **kwargs):
            calls[name] += 1
            return originals[name](*args, **kwargs)

        return call

    for name in calls:
        monkeypatch.setattr(capability_workflow, name, tracking(name))

    report = run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))

    assert report.status == "valid_insufficient"
    assert calls == {
        "build_curve": 1,
        "inspect_candidate": 1,
        "load_state_event_facts": 1,
        "import_upstream_file": 2,
    }


def test_current_artifact_bytes_are_reread_after_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    spec = _spec(repo)
    curve = repo / spec.claims[0].evidence[0].path
    curve.write_bytes(curve.read_bytes() + b" ")

    report = evaluate_capability_contract(spec, repo_root=repo)

    assert report.status == "invalid"
    assert any(
        "changed after binding" in reason
        for claim in report.claims
        for reason in claim.reasons
    )


def test_protocol_equivalence_uses_preregistration_and_curve_bytes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    spec = _spec(repo)
    claim = spec.claims[0]
    curve = json.loads((repo / claim.evidence[0].path).read_bytes())
    prereg = _bound_json(
        repo,
        "research/registration/candidates/m052-free-integrated/contract-inputs/"
        "equivalence-preregistration.json",
        "equivalence_preregistration",
        {
            "schema_version": 1,
            "claim_kind": "P",
            "metric": "paired_delta",
            "direction": "equivalence",
            "primary_k": curve["primary_contrast"]["k"],
            "equivalence_margin": 0.25,
        },
    )
    self_consistent = claim.model_copy(update={
        "evidence": [*claim.evidence, prereg],
        "preregistered_equivalence_margin": 9.0,
        "equivalence_interval_95": (-9.0, 9.0),
    })
    changed = spec.model_copy(update={
        "claims": [self_consistent, *spec.claims[1:]],
    })

    report = evaluate_capability_contract(changed, repo_root=repo)

    assert report.status == "invalid"
    assert any(
        "margin contradicts preregistration bytes" in reason
        for reason in report.claims[0].reasons
    )

    (repo / prereg.path).write_bytes((repo / prereg.path).read_bytes() + b" ")
    tampered = evaluate_capability_contract(changed, repo_root=repo)
    assert tampered.status == "invalid"
    assert any(
        "changed after binding" in reason
        for reason in tampered.claims[0].reasons
    )


def test_multiple_curve_reports_are_invalid_not_insufficient(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    spec = _spec(repo)
    claim = spec.claims[0]
    original = claim.evidence[0]
    duplicate_path = (
        "research/registration/candidates/m052-free-integrated/curve/duplicate.json"
    )
    (repo / duplicate_path).write_bytes((repo / original.path).read_bytes())
    duplicate = BoundArtifactRef.bind(
        repo_root=repo, path=duplicate_path, kind="curve_report"
    )
    changed_claim = claim.model_copy(
        update={"evidence": [original, duplicate, *claim.evidence[1:]]}
    )
    changed = spec.model_copy(
        update={"claims": [changed_claim, *spec.claims[1:]]}
    )

    report = evaluate_capability_contract(changed, repo_root=repo)

    assert report.status == "invalid"
    assert "exactly one curve_report is required" in report.claims[0].reasons


def test_cross_experiment_path_replay_is_invalid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    spec = _spec(repo)
    claim = spec.claims[0]
    original = claim.evidence[0]
    replay_path = repo / "other-experiment/curve.json"
    replay_path.parent.mkdir()
    shutil.copy2(repo / original.path, replay_path)
    replay = original.model_copy(
        update={"path": replay_path.relative_to(repo).as_posix()}
    )
    changed_claim = claim.model_copy(
        update={"evidence": [replay, *claim.evidence[1:]]}
    )
    changed = spec.model_copy(
        update={
            "experiment_id": "other-experiment",
            "claims": [changed_claim, *spec.claims[1:]],
        }
    )

    report = evaluate_capability_contract(changed, repo_root=repo)

    assert report.status == "invalid"
    assert any(
        "replay/identity mismatch" in reason
        for reason in report.claims[0].reasons
    )
    assert [ref.path for ref in report.claims[0].evidence] == [
        ref.path for ref in claim.evidence[1:]
    ]


def test_symlinked_artifact_is_invalid_even_with_identical_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    spec = _spec(repo)
    artifact = repo / spec.claims[0].evidence[0].path
    retained = repo / "retained-curve.json"
    shutil.copy2(artifact, retained)
    artifact.unlink()
    artifact.symlink_to(retained)

    report = evaluate_capability_contract(spec, repo_root=repo)

    assert report.status == "invalid"
    assert any("symlink" in reason for reason in report.claims[0].reasons)


def test_one_component_does_not_imply_other_capability_types(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_free_capability_workflow(repo_root=repo, output_dir=_output(repo))
    curve = _spec(repo).claims[0].evidence[0]
    spec = CapabilityContractSpec(
        experiment_id="curve-only",
        claims=[
            CapabilityClaimSpec(
                kind=ClaimKind.R,
                availability="available",
                statement="A curve component alone is bounded evidence.",
                limitations=["component evidence is bounded", "no substantive generality"],
                evidence=[curve],
            )
        ],
    )

    report = evaluate_capability_contract(spec, repo_root=repo)

    assert report.status == "valid_insufficient"
    assert next(item for item in report.claims if item.kind == ClaimKind.R).status == "insufficient"
    assert all(
        next(item for item in report.claims if item.kind == kind).status == "unavailable"
        for kind in (ClaimKind.P, ClaimKind.U, ClaimKind.C, ClaimKind.Y)
    )
