"""Repeat the frozen transport canary with explicit container-local tool cwd.

The transport stays byte-identical. The launch adds cwd guidance and the
required no-fallback overlay. Receipt and cleanup checks remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import types
from pathlib import Path

EXPECTED = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
NOFALLBACK = Path("/private/tmp/ov1106/shared/nofallback.yml")
GUIDANCE = ("\nFor EACH of the four bash tool calls, set cwd explicitly to /workspace. "
            "The host control directory is not a container directory. "
            "Do not change any of the four commands.\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--omp", required=True, type=Path)
    parser.add_argument("--docker", required=True, type=Path)
    parser.add_argument("--model", default="openai-codex/gpt-6.1-terra")
    args = parser.parse_args()
    if sha256(args.transport) != EXPECTED:
        raise RuntimeError("Frozen transport hash mismatch")
    if NOFALLBACK.read_bytes() != b"retry:\n  modelFallback: false\n":
        raise RuntimeError("Required no-fallback overlay changed")
    spec = importlib.util.spec_from_file_location("frozen_container_tool", args.transport)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)

    class CanaryProcess(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if input is not None:
                input += GUIDANCE.encode()
            return super().communicate(input=input, timeout=timeout)

    def popen(argv, **kwargs):
        if argv[0] == str(args.omp):
            argv = [*argv, "--config", str(NOFALLBACK)]
            (args.output / "actual-launch.json").write_text(json.dumps({
                "argv": argv, "nofallback_sha256": sha256(NOFALLBACK)}, indent=2))
            return CanaryProcess(argv, **kwargs)
        return subprocess.Popen(argv, **kwargs)

    # This proxy changes launch settings and input, never tool execution.
    transport.subprocess = types.SimpleNamespace(**{
        name: popen if name == "Popen" else getattr(subprocess, name)
        for name in dir(subprocess) if not name.startswith("__")
    })
    transport.canary(args.output, args.image, args.omp, args.docker, args.model)
    receipt = json.loads((args.output / "receipt.json").read_text())
    proof = transport.session_proof(args.output / "session")
    if any(params.get("cwd") != "/workspace" for params in proof["calls"].values()):
        raise RuntimeError("Every probe must explicitly execute from /workspace")
    if any(receipt["probes"][probe]["returncode"] == 125 for probe in transport.PROBES):
        raise RuntimeError("A forbidden probe failed at cwd, not at its path")
    if sha256(args.transport) != EXPECTED:
        raise RuntimeError("Transport changed during canary")
    result = {
        "passed": True,
        "transport_sha256": EXPECTED,
        "driver_sha256": sha256(Path(__file__)),
        "nofallback_sha256": sha256(NOFALLBACK),
        "prompt_addition": GUIDANCE,
        "receipt_path": str((args.output / "receipt.json").resolve()),
        "receipt_sha256": sha256(args.output / "receipt.json"),
        "probe_cwd": "/workspace",
        "forbidden_returncodes": {p: receipt["probes"][p]["returncode"] for p in transport.PROBES},
        "workspace_returncode": receipt["probes"][transport.POSITIVE]["returncode"],
        "cleanup_ok": receipt["cleanup_ok"],
        "scope": "fresh retrospective exact-transport validation; historical receipts unchanged",
    }
    with (args.output / "driver-receipt.json").open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
