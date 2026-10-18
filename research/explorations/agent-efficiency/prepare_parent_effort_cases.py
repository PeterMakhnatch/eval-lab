"""Derive identical parent-verified prompts; request effort remains the only treatment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner_manifest", type=Path)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.owner_manifest.resolve()
    args.output = args.output.resolve()
    if args.output.parent != original.parent or any(
        (parent / ".git").exists() for parent in args.output.parents
    ):
        parser.error("Derive within the existing private case root, outside Git")
    if args.output.exists():
        parser.error("Use a fresh output manifest")
    manifest = json.loads(original.read_bytes())
    registration = json.loads(args.registration.read_bytes())
    if (
        manifest["repeats"] != 1
        or manifest["fixed_compiler"] != "candidate"
        or registration["model_episodes_before_registration"] != 0
    ):
        raise ValueError("Unexpected preparation or registration contract")
    if registration["effort_by_arm"] != {"baseline": "high", "candidate": "medium"}:
        raise ValueError("This protocol compares high with medium effort")
    pairs = []
    for task in manifest["tasks"]:
        cases = [case for case in manifest["cases"] if case["task"] == task]
        if len(cases) != 2 or {case["arm"] for case in cases} != {"baseline", "candidate"}:
            raise ValueError("Expected one paired attempt per task")
        parent_case = next(case for case in cases if case["arm"] == "candidate")
        if parent_case.get("verification_owner") != "parent":
            raise ValueError("Source candidate prompt is not parent-owned")
        parent_prompt = Path(parent_case["prompt_file"])
        if sha(parent_prompt) != parent_case["prompt_sha256"]:
            raise ValueError("Parent-owned prompt changed")
        for case in cases:
            case["prompt_file"] = str(parent_prompt)
            case["prompt_sha256"] = parent_case["prompt_sha256"]
            case["verification_owner"] = "parent"
            case["verification_assignment_sha256"] = parent_case["verification_assignment_sha256"]
        if len({Path(case["prompt_file"]).read_bytes() for case in cases}) != 1:
            raise ValueError("Complete prompts differ")
        pairs.append(
            {"task": task, "complete_prompts_identical": True, "prompt_sha256": sha(parent_prompt)}
        )
    manifest["verification_assignment"] = {"baseline": "parent", "candidate": "parent"}
    manifest["only_experimental_change"] = (
        "Requested high versus medium effort. Complete per-task prompts, fixed candidate context packs, neutral runtime capabilities, edit-only ownership, source snapshots and frozen external graders are identical in both arms."
    )
    manifest["requested_effort_by_arm"] = registration["effort_by_arm"]
    manifest["derivation"] = {
        **manifest["derivation"],
        "kind": "parent-owned-effort-composition/v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "owner_preparation_manifest": str(original),
        "owner_preparation_manifest_sha256": sha(original),
        "composition_script_sha256": sha(Path(__file__)),
        "registration_sha256": sha(args.registration),
        "grading_contract_changed": False,
        "complete_prompt_equivalence": pairs,
    }
    with args.output.open("x") as stream:
        os.chmod(args.output, 0o600)
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(
        json.dumps(
            {
                "manifest": str(args.output.resolve()),
                "manifest_sha256": sha(args.output),
                "cases": len(manifest["cases"]),
                "equivalence": pairs,
                "model_episodes": 0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
