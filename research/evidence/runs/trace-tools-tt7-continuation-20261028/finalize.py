"""Freeze this separate continuation campaign without opening labels.

The original finalized arms and scorer are immutable inputs, not output targets.
Only the two Claude arms change; their null scan selector intentionally admits
both the original and post-merge canonical records in the combined snapshot.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

OUT = Path(__file__).resolve().parent
NAME = "trace-tools-tt7-continuation-20261028"
NAMESPACE = "tt7-continuation-20261028"
ORIGINAL = OUT.with_name("trace-tools-tt7-20261027")
ORIGINAL_REF = "research/evidence/runs/trace-tools-tt7-20261027"
PUBLIC_REF = f"research/evidence/runs/{NAME}"
STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
PRIVATE = STATE / NAMESPACE
SNAPSHOT = PRIVATE / "results-snapshot.jsonl"
PRIVACY_AUDIT = PRIVATE / "privacy-audit.json"
MANIFEST = OUT / "readings-finalized.json"
ROUTES = ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5")
SAMPLE_SHA256 = "f5e1fcd0e7095fe409ad4e5a2d3ab6df42f8b07e4f4204641d8d1094d5271e0a"
BASELINE_COMMIT = "25f63a3c28a3e02474eeb3014715158ba2b20493"
BASELINE_TREE = "c1cd22eef0da5da24486ead7c72e6f3266a74bb2"
SOURCE_SHA256 = {
    "score.py": "2325d447046b2cd9989f3bdab858126dbbbe689335a25bd5798c6a604986b440",
    "finalize.py": "ff3baf06d5550e8e73315b522f25af4cb538dab5a17962228d0f6ff8246303eb",
    "common.py": "bdf0dd5c3afc29a2c81282f3b7dd8ad7a70b9f0de81a7d1c4db5aa90b37a9fea",
    "canonical_secret_gate.py": "14b8489b4065955cdd7a91dce768738d54f3fc90a46bdfe0afb3197fa6420a4f",
}
ORIGINAL_MANIFEST_SHA256 = "9fb06a2fb3d965e36e8bda5c9846bb02a0ebe200f00b6642c5e41cfeb01663a1"
TERMINAL = {"complete", "partial", "deferred", "failed", "blocked", "cancelled", "unavailable"}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_object(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path.name}")
    return value, digest(payload)


def pinned_source(name: str) -> bytes:
    payload = (ORIGINAL / name).read_bytes()
    if digest(payload) != SOURCE_SHA256[name]:
        raise ValueError(f"immutable original source changed: {name}")
    return payload


def load_original(name: str, module_name: str) -> ModuleType:
    path = ORIGINAL / name
    payload = pinned_source(name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot bind pinned original module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # Required by dataclasses during execution.
    # Compile exactly the verified bytes at their real filename. No source or
    # __file__ rewriting, cached bytecode reads, or writes into the original D.
    exec(compile(payload, str(path), "exec", dont_inherit=True), module.__dict__)
    return module


@contextmanager
def original_modules() -> Iterator[tuple[ModuleType, ModuleType]]:
    names = ("common", "tt7_continuation_original_common", "tt7_continuation_original_score")
    previous = {name: sys.modules.get(name) for name in names}
    bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        common = load_original("common.py", names[1])
        sys.modules["common"] = common  # Original main imports this known sibling.
        scorer = load_original("score.py", names[2])
        yield common, scorer
    finally:
        sys.dont_write_bytecode = bytecode
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def campaign_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if OUT.name != NAME or PRIVATE.resolve() != STATE.resolve() / NAMESPACE:
        raise ValueError("continuation paths must remain in the separate public/private namespaces")
    for name in SOURCE_SHA256:
        pinned_source(name)
    original, original_hash = load_object(ORIGINAL / "readings-finalized.json")
    if original_hash != ORIGINAL_MANIFEST_SHA256:
        raise ValueError("immutable original finalized manifest changed")
    campaign, campaign_hash = load_object(OUT / "campaign.json")
    required = {
        "schema": "tt7.postmerge_campaign/v1", "sample_sha256": SAMPLE_SHA256,
        "baseline_commit": BASELINE_COMMIT, "baseline_tree": BASELINE_TREE,
        "namespace": NAMESPACE, "requested_run_count": 200, "allowed_new_per_route": 125,
        "routes": list(ROUTES), "admission_fraction": 0.65,
        "hard_stop_fraction": 0.70, "concurrency": 2,
    }
    if any(campaign.get(key) != value for key, value in required.items()):
        raise ValueError("campaign does not match the authorized frozen post-merge protocol")
    source_pins = campaign.get("original_source_sha256")
    if not isinstance(source_pins, dict) or any(
        source_pins.get(name) != sha256 for name, sha256 in SOURCE_SHA256.items()
    ):
        raise ValueError("campaign original source pins differ from the immutable source")
    receipt, receipt_hash = load_object(OUT / "postmerge-continuation-receipts.json")
    if (receipt.get("schema") != "tt7.postmerge_continuation/v1"
            or receipt.get("sample_sha256") != SAMPLE_SHA256
            or receipt.get("requested_run_count") != 200
            or receipt.get("status") not in TERMINAL):
        raise ValueError("post-merge continuation has not reached a frozen terminal receipt")
    finished = datetime.fromisoformat(receipt["finished_at"])
    if (finished.utcoffset() is None
            or finished < datetime.fromisoformat(original["at"])):
        raise ValueError("continuation must finish after the original finalized readings")
    phases = receipt.get("routes")
    if not isinstance(phases, dict) or set(phases) != set(ROUTES):
        raise ValueError("continuation must account for exactly the two authorized Claude routes")
    for route, phase in phases.items():
        if not isinstance(phase, dict) or phase.get("status") not in TERMINAL:
            raise ValueError(f"continuation route is not terminal: {route}")
        counts = [phase.get(field) for field in (
            "original_terminal_records", "new_terminal_records", "unattempted_positions")]
        if (any(type(count) is not int or count < 0 for count in counts)
                or counts[0] != 75 or counts[1] > 125 or sum(counts) != 200
                or (phase["status"] == "complete" and counts[2] != 0)):
            raise ValueError(f"original 75 + new + remaining must equal the frozen 200: {route}")
        if not isinstance(phase.get("reason"), str) or not phase["reason"]:
            raise ValueError(f"continuation must explain its terminal status: {route}")
        if counts[2] and (
            not isinstance(phase.get("unattempted_reason"), str) or not phase["unattempted_reason"]
        ):
            raise ValueError(f"continuation must explain unattempted positions: {route}")
    snapshot = receipt.get("results_snapshot")
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("path"), str)
            or Path(snapshot["path"]).expanduser().resolve() != SNAPSHOT.resolve()
            or not SNAPSHOT.is_file() or SNAPSHOT.is_symlink()
            or snapshot.get("sha256") != digest(SNAPSHOT.read_bytes())):
        raise ValueError("terminal receipt must bind the existing private combined snapshot and its exact hash")
    lineage = {
        "baseline_commit": BASELINE_COMMIT, "baseline_tree": BASELINE_TREE,
        "original_directory": ORIGINAL_REF, "original_source_sha256": dict(SOURCE_SHA256),
        "original_manifest": {"path": f"{ORIGINAL_REF}/readings-finalized.json",
                              "sha256": ORIGINAL_MANIFEST_SHA256},
        "campaign": {"path": f"{PUBLIC_REF}/campaign.json", "sha256": campaign_hash},
        "continuation_receipt": {"path": f"{PUBLIC_REF}/postmerge-continuation-receipts.json",
                                 "sha256": receipt_hash},
        "results_snapshot": {"path": str(SNAPSHOT), "sha256": snapshot["sha256"]},
        "phase_separation": {
            "campaign": NAMESPACE, "original_terminal_records_per_route": 75,
            "allowed_new_per_route": 125, "original_evidence_mutated": False,
            "canonical_result_store": str(STATE / "results-v1.jsonl"),
            "scoring_input": "immutable combined snapshot, not the live append-only store",
            "original_prefix_60": "unchanged paired supplemental view; never a recommendation cohort",
        },
    }
    return campaign, receipt, lineage


def build_manifest(common: ModuleType, scorer: ModuleType, campaign: dict[str, Any],
                   receipt: dict[str, Any], lineage: dict[str, Any], *, at: str) -> dict[str, Any]:
    # Reuse the original terminal gate and finalized arms, never original main()
    # (which would overwrite the original campaign's manifest).
    manifest, _, _ = scorer._manifest(ORIGINAL / "readings-finalized.json")
    changed = set()
    for arm in manifest["arms"]:
        route = arm.get("model")
        if arm["tool"] != "omp" or route not in ROUTES:
            continue
        phase = receipt["routes"][route]
        arm.update({
            "status": "complete" if phase["unattempted_positions"] == 0 else "partial",
            "reason": (f"{phase['reason']}; preserved original 75 terminal cells plus "
                       f"{phase['new_terminal_records']} separate post-merge terminal cells"),
            "scan": None,
            "scope": "full-200", "target_run_count": 200, "expected_runs": 200, "total_sample": 200,
            "observed_batch_scope": "original-75+separate-postmerge-continuation",
            "unattempted_reason": phase["unattempted_reason"],
            "effective_admission_threshold": campaign["admission_fraction"],
            "continuation_status": phase["status"],
            "phase_records": {key: phase[key] for key in (
                "original_terminal_records", "new_terminal_records", "unattempted_positions")},
        })
        changed.add(route)
    if changed != set(ROUTES) or common.SAMPLE_SHA256 != SAMPLE_SHA256:
        raise ValueError("original arms/sample contract does not contain both frozen Claude routes")
    for field in ("subscription_completed_batch_receipt", "privacy_gate_source_snapshot",
                  "privacy_receipt", "pricing_receipt"):
        manifest[field] = f"{ORIGINAL_REF}/{Path(manifest[field]).name}"
    manifest["financial_basis"]["receipt"] = f"{ORIGINAL_REF}/luna-financial-basis.json"
    manifest.update({
        "at": at, "labels_read": False, "expected_arm_count": len(manifest["arms"]),
        "campaign": NAMESPACE, "lineage": lineage,
        "results_snapshot": lineage["results_snapshot"],
        "subscription_scope_authority": "separate post-merge campaign; original 75 preserved, only frozen unattempted 125 authorized",
        "subscription_admission_fraction": campaign["admission_fraction"],
        "original_subscription_continuation_receipt": f"{ORIGINAL_REF}/continuation-200-receipts.json",
        "subscription_continuation_receipt": f"{PUBLIC_REF}/postmerge-continuation-receipts.json",
        "original_privacy_audit_receipt": f"{ORIGINAL_REF}/privacy-audit.json",
        "privacy_audit_receipt": f"{PUBLIC_REF}/privacy-audit.json",
        "private_privacy_audit": str(PRIVACY_AUDIT),
        "scoring_source": {"path": f"{ORIGINAL_REF}/score.py", "sha256": SOURCE_SHA256["score.py"]},
    })
    return manifest


def main() -> None:
    campaign, receipt, lineage = campaign_inputs()
    with original_modules() as (common, scorer):
        common.load_sample()  # Frozen identities only; no labels or passage annotations.
        manifest = build_manifest(common, scorer, campaign, receipt, lineage, at=common.now())
    # Exclusive creation also refuses existing symlinks/hardlinks to old evidence.
    with MANIFEST.open("x") as stream:
        stream.write(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"finalized": True, "arms": len(manifest["arms"]),
                      "manifest": str(MANIFEST), "labels_read": False,
                      "results_snapshot_sha256": lineage["results_snapshot"]["sha256"]}))


if __name__ == "__main__":
    main()
