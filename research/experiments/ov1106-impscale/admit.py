"""Audit the named historical cohort against a fresh exact-transport canary.

This is an explicitly retrospective analysis admission. It does not rewrite old
receipts or claim that their original contemporaneous canary hashes matched.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

TRANSPORT = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
MODELS = {"openai-codex/gpt-6.1-sol", "openai-codex/gpt-6.1-terra"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document(path: Path) -> dict:
    return json.loads(path.read_text())


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--canary", required=True, type=Path)
    parser.add_argument("--quarantine", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require(digest(args.transport) == TRANSPORT, "transport_source_mismatch")
    spec = importlib.util.spec_from_file_location("frozen_transport", args.transport)
    ct = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ct)
    canary = document(args.canary)
    driver = document(args.canary.with_name("driver-receipt.json"))
    require(driver["passed"] is True and driver["receipt_sha256"] == digest(args.canary), "invalid_driver_binding")
    require(canary["runtime"]["transport_sha256"] == TRANSPORT, "canary_transport_mismatch")
    for key, expected in (("passed", True), ("cleanup_ok", True), ("audit_complete", True),
                          ("outside_workspace_access", False), ("secrets_forwarded", False)):
        require(canary[key] is expected, "canary_" + key)
    require(ct.registry_ok(canary["registry"], canary["model"]), "canary_registry")
    for probe in ct.PROBES:
        result = canary["probes"][probe]
        require(result["cwd"] == "/workspace" and result["returncode"] not in (0, 125), "probe_not_executed_and_denied")
    positive = canary["probes"][ct.POSITIVE]
    require(positive["returncode"] == 0 and positive["output"].strip() == "/workspace", "workspace_not_proven")
    quarantined = {str(Path(row["run"]).resolve()) for row in
                   (json.loads(line) for line in args.quarantine.read_text().splitlines() if line.strip())}
    admitted, excluded = [], []
    for rdir in sorted(args.runs.iterdir()):
        if not rdir.is_dir():
            continue
        record = {"run_id": rdir.name, "run_path": str(rdir.resolve())}
        try:
            require(str(rdir.resolve()) not in quarantined, "quarantined")
            receipt_path = rdir / "receipt.json"
            receipt = document(receipt_path)
            require(receipt["runtime"]["transport_sha256"] == TRANSPORT, "transport_mismatch")
            require(receipt_path.read_bytes() == (rdir / "containment.json").read_bytes(), "receipt_copies_differ")
            require(receipt["run_id"] == rdir.name and receipt["policy"] == ct.POLICY, "run_identity")
            for key, expected in (("passed", True), ("cleanup_ok", True), ("audit_complete", True),
                                  ("outside_workspace_access", False), ("unknown_effects", False), ("secrets_forwarded", False)):
                require(receipt[key] is expected, "run_" + key)
            model = receipt["model"]
            require(model in MODELS and receipt["tool_names"] == ["bash"], "model_or_tools")
            registry = document(rdir / "control/registry.json")
            require(registry == receipt["registry"] and ct.registry_ok(registry, model), "registry_mismatch")
            runtime = receipt["runtime"]
            require(runtime["binary_sha256"] == canary["runtime"]["binary_sha256"], "binary_mismatch")
            for key, path in (("extension_sha256", "control/container-bash.ts"),
                              ("config_sha256", "control/isolated.json")):
                require(digest(rdir / path) == runtime[key], "support_hash_mismatch")
            require(document(rdir / "control/isolated.json") == ct.isolated_settings(model), "unsafe_settings")
            config = document(rdir / "control/transport.json")
            container = receipt["container"]
            require(config["container_id"] == container["id"], "container_id")
            require(container["network_mode"] == "none" and container["privileged"] is False
                    and container["no_new_privileges"] is True, "unsafe_container")
            mounts = container["mounts"]
            require(len(mounts) == 1 and mounts[0]["Type"] == "bind", "unexpected_mounts")
            require(Path(mounts[0]["Source"]).resolve() == Path(config["workspace_source"]).resolve()
                    and mounts[0]["Destination"] == config["workdir"], "mount_mismatch")
            proof = ct.session_proof(rdir / "session")
            require(proof["models"] == [model] and proof["session_files"] == receipt["session_files"], "session_binding")
            failed_before_exec = set()
            for session_file in proof["session_files"]:
                for line in (rdir / session_file["path"]).read_text().splitlines():
                    message = json.loads(line).get("message", {})
                    if message.get("role") != "toolResult" or message.get("details") != {}:
                        continue
                    text = "\n".join(block.get("text", "") for block in message.get("content", [])
                                     if block.get("type") == "text")
                    if (message.get("isError") is True
                            and 'line 134, in execute\n    inspect_container(config)' in text
                            and 'line 66, in inspect_container' in text
                            and text.rstrip().endswith("RuntimeError: Docker inspect failed")):
                        failed_before_exec.add(message["toolCallId"])
            for call_id, params in proof["calls"].items():
                result = proof["results"][call_id]
                if call_id in failed_before_exec:
                    continue
                require(result.get("command") == params.get("command") and result.get("policy") == ct.POLICY
                        and result.get("container_id") == container["id"]
                        and type(result.get("returncode")) is int, "tool_result_binding")
            artifacts = receipt["artifacts"]
            require(set(artifacts) == {"run.json", "grade.json", "trajectory.json", "prompt.md", "final.diff"}, "artifact_inventory")
            for path, expected in artifacts.items():
                require(digest(rdir / path) == expected, "artifact_binding_" + path)
            old_canary_path = Path(receipt["canary"]["receipt_path"])
            require(digest(old_canary_path) == receipt["canary"]["sha256"], "historical_canary_reference")
            run = document(rdir / "run.json")
            require(run["id"] == rdir.name and run["served"] == model, "run_model_identity")
            admitted.append({**record, "task_id": receipt["task_id"], "variant": receipt["variant"],
                             "model": model, "receipt_sha256": digest(receipt_path), "artifacts": artifacts,
                             "session_files": proof["session_files"], "n_tool_calls": len(proof["calls"]),
                             "failed_before_exec_calls": len(failed_before_exec),
                             "actual_workdir": config["workdir"], "historical_canary_sha256": receipt["canary"]["sha256"]})
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            excluded.append({**record, "reason": str(error)})
    summary = {"admitted": len(admitted), "excluded": len(excluded),
               "variants": dict(Counter(row["variant"] for row in admitted)),
               "models": dict(Counter(row["model"] for row in admitted)),
               "excluded_reasons": dict(Counter(row["reason"] for row in excluded)),
               "tasks": len({row["task_id"] for row in admitted})}
    output = {"schema": "ov1106-retrospective-exact-transport-v1", "transport_sha256": TRANSPORT,
              "canary_path": str(args.canary.resolve()), "canary_sha256": digest(args.canary),
              "quarantine_sha256": digest(args.quarantine), "audit_script_sha256": digest(Path(__file__)),
              "admission_basis": "user-authorized retrospective exact-transport validation; not contemporaneous recertification",
              "receipt_caveat": "historical workspace_destination summary says /workspace; actual mount/config use /testbed",
              "summary": summary, "runs": admitted, "excluded": excluded}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(output, stream, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
