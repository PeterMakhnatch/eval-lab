#!/usr/bin/env python3
"""HAR-105 part 2: a nop before and after each task fix.

The fixes are the task variants this card derived (lineage records under
``library/task-variants/`` with ``created_by`` ``har105-taskfix``). For each one
this writes a Daytona nop spec for the fixed package in ``specs/``. Once those
jobs have run, it writes ``fixes.json``, which pairs the parent's existing nop
(HAR-88 or HAR-95) with the variant's nop.

A fix counts as nop-clean when the variant nop grades with reward 0, raises no
trial exception, and its verifier output has no setup error (``SETUP_ERROR``).

Superseded derivations keep their nops as ``earlier_attempts``, since each one
shows why the fix had to change.
"""

from __future__ import annotations

import json
import re

from nopspec import (
    OUT,
    ROOT,
    SETUP_ERROR,
    nop_spec,
    nop_trial_dir,
    nop_verifier_text,
)

CREATED_BY = "har105-taskfix"
#: The parent's existing Daytona nop job for each fixed task.
BEFORE = {
    "candidate-1634-software-databases": "mimo-qual-t-candidate-1634-software-databases",
    "candidate-1789-security-appsec": "mimo-qual-t-candidate-1789-security-appsec",
    "candidate-1702-ml-inference": "mimo-qual-t-candidate-1702-ml-inference",
    "arvo_18737": "har95-qual-y-arvo-18737",
    "arvo_57589": "har95-qual-y-arvo-57589",
}
_SETUP_ONLY = (
    "edited environment/setup/setup.sh only; the healthcheck runs the copy embedded in "
    "task.toml (setup_payload.py), so the nop matched the parent exactly"
)
#: Nops of superseded derivations, with what each one showed.
EARLIER = {
    "candidate-1634-software-databases": [
        ("har105-fix-candidate-1634-software-databases", _SETUP_ONLY),
    ],
    "candidate-1702-ml-inference": [
        ("har105-fix-candidate-1702-ml-inference", _SETUP_ONLY),
    ],
    "candidate-1789-security-appsec": [
        ("har105-fix-candidate-1789-security-appsec", _SETUP_ONLY),
        (
            "har105-fix-candidate-1789-security-appsec-3e09b8e67f4f",
            "installed stevedore only: `import bandit` then fails on missing package "
            "metadata, because the vendored Bandit was never installed",
        ),
    ],
}


def job_name(task_id: str, variant_digest: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", task_id.lower())
    return f"har105-fix-{slug}-{variant_digest[7:19]}"


def nop_result(job: str) -> dict | None:
    trial = nop_trial_dir(job)
    if trial is None:
        return None
    result = json.loads((trial / "result.json").read_text())
    text = nop_verifier_text(job) or ""
    lines = [line for line in text.splitlines() if line.strip()]
    setup_errors = [line.strip()[:200] for line in lines if SETUP_ERROR.search(line)]
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exception = (result.get("exception_info") or {}).get("exception_type")
    return {
        "job_name": job,
        "reward": reward,
        "exception": exception,
        "setup_error_lines": len(setup_errors),
        "first_setup_error": setup_errors[0] if setup_errors else None,
        "verifier_last_line": lines[-1][:200] if lines else None,
        "clean": reward == 0 and exception is None and not setup_errors,
    }


def main() -> None:
    records = [
        json.loads(path.read_text())
        for path in sorted((ROOT / "library/task-variants").rglob("*.json"))
    ]
    records = [r for r in records if r["created_by"] == CREATED_BY]
    specs = OUT / "specs"
    specs.mkdir(exist_ok=True)
    for old in specs.glob("har105-fix-*.json"):
        old.unlink()
    fixes = []
    for record in records:
        task_id = record["task_name"].split("/", 1)[1]
        slug = record["task_name"].replace("/", "__")
        package = f"derived/task-store/variants/{slug}/{record['variant_digest'][7:19]}"
        name = job_name(task_id, record["variant_digest"])
        spec = nop_spec(
            package,
            name,
            "The fixed variant sets up and grades on Daytona: a nop control completes the "
            "verifier with reward 0 and no setup or import error.",
        )
        (specs / f"{name}.json").write_text(json.dumps(spec, indent=2) + "\n")
        fixes.append(
            {
                "task_id": task_id,
                "transform": record["transform"],
                "components_changed": record["components_changed"],
                "rationale": record["rationale"],
                "parent_digest": record["parent"]["digest"],
                "variant_digest": record["variant_digest"],
                "files": [f["path"] for f in record["files"]],
                "package": package,
                "spec": f"specs/{name}.json",
                "nop_before": nop_result(BEFORE[task_id]),
                "nop_after": nop_result(name),
                "earlier_attempts": [
                    {"what": what, "nop": nop_result(job)} for job, what in EARLIER.get(task_id, [])
                ],
            }
        )
    (OUT / "fixes.json").write_text(
        json.dumps({"card": "HAR-105", "fixes": fixes}, indent=2) + "\n"
    )
    for fix in fixes:
        before, after = fix["nop_before"], fix["nop_after"]
        print(
            fix["task_id"],
            "before:",
            before and (before["reward"], before["setup_error_lines"]),
            "after:",
            after and (after["reward"], after["setup_error_lines"], after["clean"]),
        )


if __name__ == "__main__":
    main()
