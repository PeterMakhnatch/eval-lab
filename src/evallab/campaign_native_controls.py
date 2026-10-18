"""Control-backed admission for the approved fresh repaired-data generation campaign.

This is not lineage validation, a task-ledger registration, or a scoring release.
The original candidate records and their open-egress control observations remain
unchanged. A named profile pins the independent current-reader custody audit;
only fresh, locked, single-attempt BASE generation can use that audit as its
explicit admission basis.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTROLS_SHA256 = "ca3d4460296cdfa017741d3557f3455bccf74b8f26f24643ff4a2c1f50501e80"
BASE_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
PURPOSE = "fresh_native_repaired"


def _digest(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("artifact digest must be a SHA-256 string")
    result = value.removeprefix("sha256:")
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError("artifact digest must be a full lowercase SHA-256")
    return result


def _artifact(ref: dict[str, Any], repo_root: Path) -> tuple[Path, bytes]:
    path = Path(ref["path"])
    if not path.is_absolute():
        path = repo_root / path
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _digest(ref["sha256"]):
        raise ValueError(f"control admission artifact changed: {path}")
    return path.resolve(), raw


def campaign_native_control_binding(
    *,
    repo_root: Path,
    profile: dict[str, Any],
    fingerprint: dict[str, Any],
    spec: Any,
    request: Any = None,
) -> dict[str, Any]:
    """Admit only the exact nineteen control-qualified fresh-generation tasks.

    Ordinary ledger/lineage callers never use this alternative. No returned
    field changes ``ledger_match`` or the original VariantRecord status.
    """
    refused: dict[str, Any] = {
        "admitted": False,
        "admission_basis": "campaign_native_controls",
        "ledger_root_reached": False,
        "purpose": PURPOSE,
    }
    try:
        scope = profile.get("campaign_native_controls")
        if not isinstance(scope, dict) or scope.get("purpose") != PURPOSE:
            raise ValueError("profile has no fresh-generation control scope")
        actual = request if request is not None else spec
        if (
            getattr(actual, "agent", None) != "mimoagent"
            or getattr(actual, "model", None) != BASE_MODEL
            or getattr(actual, "environment", None) != "modal"
            or getattr(actual, "egress_lock", None) is not True
            or getattr(actual, "modal_resource_policy", None) != "limit"
            or type(getattr(actual, "modal_sandbox_create_max_attempts", None)) is not int
            or actual.modal_sandbox_create_max_attempts != 1
            or getattr(actual, "attempts", None) != 1
            or getattr(actual, "diff_sources", None) not in ([], ())
            or getattr(spec, "attempts", None) != 1
            or getattr(spec, "diff_sources", None) != []
            or fingerprint["lock"]["mode"] != "locked"
        ):
            raise ValueError("control scope requires fresh one-attempt BASE/mimoagent with locked, resource-limited, single-create Modal execution")

        _, audit_raw = _artifact(scope["audit"], repo_root)
        audit = json.loads(audit_raw)
        if audit.get("schema") != "repaired_data_sft_v2.control_admission/v1":
            raise ValueError("unsupported current-reader control audit schema")
        source_ref = audit["source_controls_handoff"]
        if _digest(source_ref["sha256"]) != CONTROLS_SHA256:
            raise ValueError("control scope is not the approved nineteen-task handoff")
        _, source_raw = _artifact(source_ref, repo_root)
        source = json.loads(source_raw)
        qualified = {
            row["task_id"]: row
            for row in source["tasks"]
            if row.get("current_reader_gate_pass") is True
            and row.get("strict_assertion_scope_admission") is True
        }
        if len(qualified) != 19 or {"001478", "002486"} & qualified.keys():
            raise ValueError("approved control handoff does not identify the exact nineteen train tasks")
        audited = {
            row["task_id"]: row
            for row in audit["tasks"]
            if row.get("controls_qualified") is True
            and row.get("strict_assertion_scope_admission") is True
        }
        if audited.keys() != qualified.keys():
            raise ValueError("current-reader audit and approved train cohort differ")

        task_name = fingerprint["subject"]["task"]
        short_id = str(task_name).removeprefix("format-code-task-")
        if short_id not in audited or task_name != f"format-code-task-{short_id}":
            raise ValueError("task identity is outside the approved fresh-generation cohort")
        row, retained = audited[short_id], qualified[short_id]
        package_digest = _digest(fingerprint["task"]["digest"])
        retained_identity = retained["identity"]
        retained_digest = retained_identity.get("package_digest", retained_identity.get("package"))
        if (
            _digest(retained_digest) != package_digest
            or _digest(row["repaired_package"]["sha256"]) != package_digest
            or row.get("ledger_root_reached") is not False
            or row.get("admission_basis") != "control_qualified"
        ):
            raise ValueError("current package is not the exact control-qualified candidate")
        canonical_path, record_raw = _artifact(row["canonical_record"], repo_root)
        if not canonical_path.is_relative_to((repo_root / "library/task-variants").resolve()):
            raise ValueError("candidate record is not in the executing checkout's canonical record store")
        _, source_record_raw = _artifact(row["source_record"], repo_root)
        if record_raw != source_record_raw:
            raise ValueError("canonical registration changed the original candidate record bytes")
        record = json.loads(record_raw)
        if (
            _digest(record["variant_digest"]) != package_digest
            or str(record["task_name"]).split("/", 1)[-1] != task_name
            or record.get("status") != "candidate"
        ):
            raise ValueError("registered candidate identity/status differs from its retained controls")
        return {
            **refused,
            "admitted": True,
            "reason": "exact current-reader control audit admits fresh generation only; retained nop controls remain open and ledger ancestry remains unreached",
            "audit": scope["audit"],
            "source_controls": source_ref,
            "canonical_record": row["canonical_record"],
            "record_status": record["status"],
            "task_id": short_id,
            "package_digest": "sha256:" + package_digest,
            "retained_nop_lock_mode": "open",
            "fresh_agent_lock_requirement": "locked",
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {**refused, "reason": str(exc)}
