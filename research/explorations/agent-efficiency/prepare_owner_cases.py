"""Prepare a paired verification-ownership probe without changing its code grader.

Preparation is local and non-generative. The original preparation manifest and
prompts stay immutable; the derived manifest pins new ownership instructions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

OWNERSHIP = {
    "baseline": """You are the primary implementation owner for this isolated task.
You own focused verification before returning the completed source patch. Use
existing checks or a compact behavioral smoke with the provided interpreter;
do not expand the source-edit scope or install/build anything. Report only
checks you actually performed. The parent will independently apply the same
frozen external grader used in the other arm.
""",
    "candidate": """You are a background implementation worker for this isolated task.
The parent is the sole verification owner and will run the frozen external
behavioral grader after you return. Follow the repository's worker skip-check
rule: do not run tests, lint, formatting, builds, syntax/import smokes, custom
behavioral assertions, or execute the changed program for verification. Do not
create temporary validation tests or fixtures. Passive source/test inspection,
including reading or extracting declarations, remains permitted. Finish the
complete source implementation, then return a brief behavior summary stating
that verification belongs to the parent; do not claim unrun checks passed.
""",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reuse-prepared",
        action="store_true",
        help="Reuse immutable base snapshots; only derive new owner prompts.",
    )
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    python = args.python.absolute()  # Preserve the existing venv launcher.
    if not output.is_relative_to(Path("/tmp/agent-efficiency/code-cases").resolve()):
        parser.error("output must be owned study scratch")
    if output.exists() and not args.reuse_prepared:
        parser.error("output already exists; do not overwrite prepared evidence")
    if args.reuse_prepared:
        if not (output / "manifest.json").is_file():
            parser.error("reuse requires an existing base manifest")
    else:
        preparer = Path(__file__).with_name("prepare_code_cases.py")
        subprocess.run(
            [
                str(python),
                "-B",
                str(preparer),
                "--root",
                str(root),
                "--output",
                str(output),
                "--repeats",
                "1",
                "--fixed-compiler",
                "candidate",
                "--python",
                str(python),
            ],
            check=True,
        )
    original_path = output / "manifest.json"
    original_bytes = original_path.read_bytes()
    manifest = json.loads(original_bytes)
    if manifest["fixed_compiler"] != "candidate" or manifest["repeats"] != 1:
        raise ValueError("ownership study requires one repeat and a fixed candidate compiler")
    for task in manifest["tasks"]:
        task_cases = [case for case in manifest["cases"] if case["task"] == task]
        if len(task_cases) != 2 or len({case["prompt_sha256"] for case in task_cases}) != 1:
            raise ValueError("base prompts differ across the ownership arms")
    capabilities = f"""
BEGIN SNAPSHOT CAPABILITIES AND DIAGNOSTIC BOUNDARY
This snapshot contains the complete pinned tracked sources but has no .git
metadata and no snapshot-local .venv. Do not probe Git, create an environment,
or install dependencies. If verification is your responsibility, the existing
dependency-complete interpreter is {python}; use PYTHONPATH=src to import this
snapshot rather than the host checkout. Do not inspect host source or external
evaluation material. Keep diagnostic output compact: named invariants,
pass/fail, selected IDs/counts or exact digests where sufficient, not complete
context-pack bodies or metadata dumps.
END SNAPSHOT CAPABILITIES AND DIAGNOSTIC BOUNDARY
"""
    for case in manifest["cases"]:
        original_prompt = Path(case["prompt_file"]).read_bytes()
        if sha256(original_prompt) != case["prompt_sha256"]:
            raise ValueError("base prompt changed before ownership derivation")
        owner = "solver" if case["arm"] == "baseline" else "parent"
        assignment = (
            capabilities
            + "\nBEGIN VERIFICATION OWNERSHIP\n"
            + OWNERSHIP[case["arm"]]
            + "END VERIFICATION OWNERSHIP\n"
        )
        prompt = original_prompt + assignment.encode()
        path = Path(case["prompt_file"]).with_name("owner-v2-prompt.txt")
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(prompt)
        case["base_prompt_sha256"] = case["prompt_sha256"]
        case["prompt_file"] = str(path)
        case["prompt_sha256"] = sha256(prompt)
        case["verification_owner"] = owner
        case["verification_assignment_sha256"] = sha256(assignment.encode())
    manifest["verification_assignment"] = {"baseline": "solver", "candidate": "parent"}
    manifest["only_experimental_change"] = (
        "Both arms use the same candidate compiler pack and receive identical "
        "snapshot-capability/compact-diagnostic instructions. Only explicit "
        "verification ownership differs. The controller must request high effort "
        "for both arms; the unchanged external grader judges every source patch."
    )
    manifest["derivation"] = {
        "kind": "explicit-verification-owner/v2-neutral-runtime",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "original_manifest": str(original_path),
        "original_manifest_sha256": sha256(original_bytes),
        "preparation_script_sha256": sha256(Path(__file__).read_bytes()),
        "grading_manifest": str(original_path),
        "grading_manifest_sha256": sha256(original_bytes),
        "grading_contract_changed": False,
        "reused_base_preparation": args.reuse_prepared,
    }
    destination = output / "owner-manifest-v2.json"
    manifest["derivation"]["reproduce_command"] = shlex.join(
        [
            str(python),
            "-B",
            str(Path(__file__).absolute()),
            "--root",
            str(root),
            "--output",
            str(output),
            "--python",
            str(python),
        ]
        + (["--reuse-prepared"] if args.reuse_prepared else [])
    )
    with destination.open("x") as stream:
        os.chmod(destination, 0o600)
        json.dump(manifest, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "manifest": str(destination),
                "manifest_sha256": sha256(destination.read_bytes()),
                "cases": len(manifest["cases"]),
                "original_manifest_unchanged": original_path.read_bytes() == original_bytes,
                "model_calls": 0,
                "grading_executed": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
