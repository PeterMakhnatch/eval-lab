"""Run three public-example cells with the unchanged native DEV verifier."""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import cells

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent / "audit/existing-alternatives.json"
OUT = Path.home() / "Developer/eval-lab-results/2026-11-04/ov1104-wave2-grader/2391-public"
CAP_USD = 0.10
APP = "ov1104-wave2-grader-cert3"
EXPECTED_PATCHES = {
    "real-2": "36732e9915745a857f0369c29feb83934f3f7aba927f852c02895aff820aa003",
    "real-3": "53787d3060a3197f0cb6689d95916c462496ee11bf0e0894577221d21cce8624",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    with path.open("x") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def before_deadline():
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.date().isoformat() != "2026-11-04" or (now.hour, now.minute) >= (5, 40):
        raise RuntimeError("Insufficient time before the 5:45 AM ET execution deadline")


def main():
    before_deadline()
    sys.path.insert(0, str(cells.REPO / "src"))
    from evallab import modal_cells
    from evallab.registry import task_directory_digest

    binding = json.loads((HERE / "package-bindings.json").read_text())["002391"]
    package = Path(binding["original_package"])
    if task_directory_digest(package) != binding["original_digest"]:
        raise RuntimeError("Original package digest differs")
    instruction_hash = sha(package / "instruction.md")
    if instruction_hash != "070adfd6a3bad8de52967db202ed20fa542b9551e77e7bd36608621ba1bb0ef6":
        raise RuntimeError("Public instruction digest differs")
    workdir = tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]
    candidates = {}
    for row in json.loads(AUDIT.read_text())["candidates"]:
        candidate = row["candidate"]
        path = Path(candidate["path"])
        name = path.stem
        if name not in EXPECTED_PATCHES:
            continue
        digest = sha(path)
        if digest != candidate["sha256"] or digest != EXPECTED_PATCHES[name]:
            raise RuntimeError(f"Audited patch hash differs: {name}")
        text = path.read_text()
        headers = [line for line in text.splitlines() if line.startswith("diff --git ")]
        if headers != ["diff --git a/pip_audit/_audit.py b/pip_audit/_audit.py"]:
            raise RuntimeError(f"Candidate changes non-allowlisted paths: {name}")
        candidates[name] = {"path": str(path), "sha256": digest, "text": text}
    if set(candidates) != set(EXPECTED_PATCHES):
        raise RuntimeError("Both audited candidates are required")

    current_ledger = json.loads(cells.LEDGER.read_text())
    if current_ledger.get("reserved_usd"):
        raise RuntimeError("Shared ledger has outstanding reservations")
    OUT.mkdir(parents=True, exist_ok=True)
    before_path = OUT / "spend-before.json"
    if not before_path.exists():
        write_new(before_path, current_ledger)
    initial_ledger = json.loads(before_path.read_text())
    initial_spend = initial_ledger["settled_usd"]
    freeze = {
        "frozen_at": datetime.now(UTC).isoformat(),
        "files": {name: sha(HERE / name) for name in (
            "PREREG-2391-PUBLIC.md", "public_2391.py", "evaluate_2391_public.py", "cells.py",
            "PUBLIC-2391-EXECUTION-NOTE.json",
        )},
        "audit_sha256": sha(AUDIT), "instruction_sha256": instruction_hash,
        "package": str(package), "package_digest": binding["original_digest"],
        "patches": {name: {k: v for k, v in candidate.items() if k != "text"}
                    for name, candidate in candidates.items()},
        "modal_app_name": APP, "spend_ledger": str(cells.LEDGER),
        "incremental_cap_usd": CAP_USD, "initial_settled_usd": initial_spend,
        "shared_cap_usd": initial_ledger["max_usd"],
        "raw_evidence": str(OUT),
    }
    write_new(HERE / "PUBLIC-2391-FREEZE.json", freeze)
    with (HERE / "PREREG-2391-PUBLIC.sha256").open("x") as handle:
        handle.write(f"{freeze['files']['PREREG-2391-PUBLIC.md']}  PREREG-2391-PUBLIC.md\n")

    original_gate = modal_cells.ReserveSettleSpendGate

    class PublicSpendGate(original_gate):
        """Keep the shared ceiling and enforce the smaller experiment ceiling."""

        def __init__(self, *args, **kwargs):
            kwargs["modal_app_name"] = APP
            super().__init__(*args, **kwargs)

        def reserve(self, *args, **kwargs):
            before_deadline()
            ledger = json.loads(cells.LEDGER.read_text())
            # Reserve the full kill-time estimate, not merely projected runtime.
            worst_case = self.estimate_cost(kwargs["package"], kwargs["timeout_seconds"])
            if ledger["effective_spend_usd"] - initial_spend + worst_case > CAP_USD:
                raise RuntimeError("Public experiment would exceed its $0.10 incremental cap")
            return super().reserve(*args, **kwargs)

    payload = base64.b64encode((HERE / "public_2391.py").read_bytes()).decode()
    suffix = (
        f"printf %s {payload} | base64 -d > /tmp/public_2391.py\n"
        f"PYTHONPATH={shlex.quote(workdir)} python3 /tmp/public_2391.py\n"
    )
    output = {"schema": "ov1104.public-2391/v1", "freeze_sha256": sha(HERE / "PUBLIC-2391-FREEZE.json"),
              "rows": [], "negative_control_valid": False, "accepted": None, "denominator": 2,
              "native_reward_is_incidental": True, "certification_or_gate7_claim": False}
    with patch.object(cells, "ROOT", OUT), patch.object(modal_cells, "ReserveSettleSpendGate", PublicSpendGate):
        for name in ("unpatched", "real-2", "real-3"):
            before_deadline()
            for filename, digest in freeze["files"].items():
                if sha(HERE / filename) != digest:
                    raise RuntimeError(f"Frozen input differs: {filename}")
            if name == "unpatched":
                script = f"set -eu\ncd {shlex.quote(workdir)}\n" + suffix
                patch_hash = None
            else:
                candidate = candidates[name]
                if sha(Path(candidate["path"])) != candidate["sha256"]:
                    raise RuntimeError(f"Patch drift: {name}")
                script = cells.source_patch_script(candidate["text"], workdir) + suffix
                patch_hash = candidate["sha256"]
            cell_name = f"002391-public-{name}"
            result = cells.run(cell_name, package, script)
            log_lines = result.get("agent_log", "").splitlines()
            receipt = OUT / "cells" / cell_name / "result.json"
            row = {
                "candidate": name, "status": result.get("status"), "reward": result.get("reward"),
                "public_marker": "PUBLIC_2391_PASSED" in log_lines,
                "public_ready": "PUBLIC_2391_READY" in log_lines,
                "public_assertion_failed": any(line.startswith("PUBLIC_2391_FAILED:") for line in log_lines),
                "source_patch_applied": "SOURCE_PATCH_APPLIED" in log_lines,
                "receipt": str(receipt), "receipt_sha256": sha(receipt),
                "patch_sha256": patch_hash, "est_usd": result.get("est_usd"),
                "cache_hit": result.get("cache_hit", False),
            }
            row["accepted"] = name != "unpatched" and row["status"] == "executed" and row["public_marker"] and row["source_patch_applied"]
            output["rows"].append(row)
            if name == "unpatched":
                output["negative_control_valid"] = row["public_ready"] and row["public_assertion_failed"] and not row["public_marker"]
            print(json.dumps(row, sort_keys=True), flush=True)
            if name == "unpatched" and not output["negative_control_valid"]:
                write_new(HERE / "evaluation-2391-public.json", output)
                raise RuntimeError("Negative control is invalid; no acceptance claim is allowed")
    final_ledger = json.loads(cells.LEDGER.read_text())
    write_new(OUT / "spend-after.json", final_ledger)
    output["accepted"] = sum(row["accepted"] for row in output["rows"])
    output["estimated_spend_delta_usd"] = round(final_ledger["settled_usd"] - initial_spend, 6)
    output["sum_receipt_est_usd"] = round(sum(row["est_usd"] for row in output["rows"]), 5)
    output["finished_at"] = datetime.now(UTC).isoformat()
    output["ledger_before"] = {"path": str(OUT / "spend-before.json"), "sha256": sha(OUT / "spend-before.json")}
    output["ledger_after"] = {"path": str(OUT / "spend-after.json"), "sha256": sha(OUT / "spend-after.json")}
    write_new(HERE / "evaluation-2391-public.json", output)
    print(json.dumps({k: v for k, v in output.items() if k != "rows"}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
