"""Native package security regressions retained after Workbench retirement."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures/task_workbench"


@pytest.fixture
def task(tmp_path: Path) -> Path:
    destination = tmp_path / "task"
    shutil.copytree(FIXTURES / "valid", destination)
    return destination


def _packet_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _packet_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(_packet_bytes(value)).hexdigest()


@pytest.fixture
def retained_packet(task: Path):
    """Historical registration binding fixture, never a Gold certificate."""
    from evallab.registry import compute_task_digests

    repo = task.parent
    root = repo / "research/registration/candidates/historical"
    root.mkdir(parents=True)
    binding = {
        "task_id": "uppercase-fixture",
        "task_version": "1.0.0",
        "task_path": "task",
        "package_digest": compute_task_digests(task).package,
    }
    candidate = {
        **{key: binding[key] for key in ("task_id", "task_version", "task_path")},
        "candidate_id": "candidate-" + "0" * 24,
        "workbench_version": "m049-v1",
        "digests": {"package": "sha256:" + "a" * 64, "registry_package": binding["package_digest"]},
    }
    candidate["candidate_record_digest"] = _packet_digest(candidate)
    (root / "candidate.json").write_bytes(_packet_bytes(candidate))
    (root / "retained.json").write_bytes(b"historical test evidence\n")
    artifact = {
        "path": (root / "retained.json").relative_to(repo).as_posix(),
        "digest": "sha256:" + hashlib.sha256(b"historical test evidence\n").hexdigest(),
    }
    plan = [
        *({"control_id": f"oracle-{i}", "kind": "oracle"} for i in range(3)),
        *({"control_id": f"nop-{i}", "kind": "nop"} for i in range(2)),
        *({"control_id": f"invalid-{i}", "kind": "adversarial"} for i in range(3)),
        {"control_id": "fair-alternative", "kind": "fair_alternative"},
        {"control_id": "please-hack", "kind": "please_hack"},
    ]
    observations = [
        {
            "control_id": entry["control_id"],
            "status": "completed",
            "exception_type": None,
            "reward": 1.0 if entry["kind"] in {"oracle", "fair_alternative"} else 0.0,
            "verifier_output_digest": "sha256:" + "c" * 64,
            "evidence_digest": artifact["digest"],
        }
        for entry in plan
    ]
    body = {
        "kind": "task_workbench_certification",
        "certified": True,
        "status": "certified_for_review",
        "workbench_version": "m049-v1",
        "candidate_id": candidate["candidate_id"],
        "candidate_record_digest": candidate["candidate_record_digest"],
        "task_binding": {**binding, "candidate_package_digest": candidate["digests"]["package"]},
        "generator_identity": {
            "execution": "local",
            "model": None,
            "code_digest": "sha256:" + "a" * 64,
        },
        "validator_identity": {
            "execution": "local",
            "model": None,
            "code_digest": "sha256:" + "b" * 64,
        },
        "retained_evidence": [{**artifact, "control_id": "please-hack"}],
        "retained_replays": [
            {**artifact, "control_id": name} for name in ("fair-alternative", "please-hack")
        ],
        "control_plan": plan,
        "control_bundle": {"observations": observations},
        "control_summary": {
            "oracle_runs": 3,
            "nop_runs": 2,
            "invalid_probe_runs": 3,
            "fair_alternative_runs": 1,
            "please_hack_runs": 1,
            "result_digests": [_packet_digest(item) for item in observations],
        },
        "diagnostics": [],
        "check_vector": {
            **dict.fromkeys(
                (
                    "all_controls_completed",
                    "static",
                    "oracle_exact_1_x3",
                    "oracle_stable_output",
                    "nop_exact_0_x2",
                    "invalid_outputs_rejected",
                    "fair_alternative_exact_1",
                    "please_hack_executed",
                    "leakage_scan_clean",
                    "isolation",
                ),
                True,
            ),
            "hack_detected": False,
        },
        "axes": {
            name: {"status": "passed", "reason": "historical package check"}
            for name in (
                "task_correctness",
                "verifier_soundness",
                "verifier_completeness",
                "solvability",
            )
        }
        | {
            name: {"status": "not_assessed", "reason": "not a Gold certificate"}
            for name in ("difficulty_calibration", "realism_review")
        },
    }
    packet = root / "certification.json"
    _rewrite_packet(packet, body)
    return repo, packet, binding


def _rewrite_packet(packet: Path, body: dict) -> None:
    body.pop("certification_id", None)
    body["certification_id"] = "cert-" + hashlib.sha256(_packet_bytes(body)).hexdigest()[:24]
    packet.write_bytes(_packet_bytes(body))


def test_retained_packet_binding_survives_registry_reload_and_detects_byte_tamper(
    task: Path, retained_packet
) -> None:
    from evallab.registry import (
        TaskCertificationError,
        certification_envelope_from_packet,
        compute_task_digests,
        verify_certification_packet,
    )
    from evallab.schemas import TaskRegistryRecord

    repo, packet, binding = retained_packet
    envelope = certification_envelope_from_packet(repo, packet, **binding)
    record = TaskRegistryRecord(
        task_id="uppercase-fixture",
        task_family="uppercase-fixture",
        version="1.0.0",
        task_path="task",
        digests=compute_task_digests(task),
        source_uri="local/fixture",
        source_ref="v1",
        license="MIT",
        provenance_zone="03-synthetic",
        is_synthetic=True,
        certification=envelope,
        state="candidate",
        state_reason="historical_packet_bound_pending_admission",
        allowed_uses=["measurement"],
    )
    reloaded = TaskRegistryRecord.model_validate_json(record.model_dump_json())
    verify_certification_packet(repo, reloaded)
    assert reloaded.state == "candidate"
    packet.write_bytes(packet.read_bytes() + b" ")
    with pytest.raises(TaskCertificationError):
        verify_certification_packet(repo, reloaded)


@pytest.mark.parametrize("field", ["task_id", "task_version", "task_path", "package_digest"])
def test_retained_packet_cannot_be_replayed_for_a_different_identity(
    retained_packet, field: str
) -> None:
    from evallab.registry import TaskCertificationError, certification_envelope_from_packet

    repo, packet, binding = retained_packet
    binding[field] = "different-identity"
    with pytest.raises(TaskCertificationError):
        certification_envelope_from_packet(repo, packet, **binding)


@pytest.mark.parametrize(
    "mutation",
    ["circular", "missing-replay", "vector", "summary", "version", "leakage", "evidence"],
)
def test_retained_packet_rejects_tampered_or_contradictory_claims(
    retained_packet, mutation: str
) -> None:
    from evallab.registry import TaskCertificationError, certification_envelope_from_packet

    repo, packet, binding = retained_packet
    body = json.loads(packet.read_bytes())
    if mutation == "circular":
        body["generator_identity"] = body["validator_identity"]
    elif mutation == "missing-replay":
        body["retained_replays"] = [
            item for item in body["retained_replays"] if item["control_id"] != "please-hack"
        ]
    elif mutation == "vector":
        body["check_vector"]["oracle_exact_1_x3"] = False
    elif mutation == "summary":
        body["control_summary"]["oracle_runs"] = 99
    elif mutation == "version":
        body["workbench_version"] = "m999-v1"
    elif mutation == "leakage":
        body["diagnostics"].append(
            {
                "severity": "error",
                "code": "golden_data_leak",
                "classification": "task_defect",
                "path": "instruction.md",
                "message": "semantic tamper",
            }
        )
    else:
        (packet.parent / "retained.json").write_text("tampered")
    _rewrite_packet(packet, body)
    with pytest.raises(TaskCertificationError):
        certification_envelope_from_packet(repo, packet, **binding)
