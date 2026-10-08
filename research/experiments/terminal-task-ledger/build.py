#!/usr/bin/env python3
"""Build the terminal task ledger: one row per MiMo terminal task (64).

Inputs are committed files plus the pinned snapshot and the derived lineage
records; re-run after any snapshot, variant or validation change:

    uv run python research/experiments/terminal-task-ledger/build.py

Decisions:

* ``keep`` -- run the ``terminal-guard-extend@1`` variant (honest grading
  preserved; hook-planting hole closed).
* ``fix`` -- 0260/2376 run the guard-on-prefetch composition (missing module
  fetched at setup); 0109/2836 need a compiler-prefetch variant that is not
  built in this change (native-compile test fails with ``FileNotFoundError:
  'g++'`` as shipped).
* ``discard`` -- 0674 imports ``torch`` at collection and the image has no
  torch; baking ~800 MB of torch is rejected in favour of dropping one task.

Only ``--snapshot``, ``--records``, ``--validation`` and ``--output`` vary;
the decision tables below are the reviewable exclusion record.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

HF_REPO = "FineEnvs/MiMo-V2.6-RL-harbor-terminal"
HF_REVISION = "fe1c2b665aae1ba7a09a270d979724d32269ae6a"

GUARD_TRANSFORM = "terminal-guard-extend@1"
PREFETCH_TRANSFORM = "env-prefetch-network@1"

#: Missing-module graders repaired by a setup-time fetch: task -> pip spec.
PREFETCH: dict[str, str] = {
    "candidate-0260-security-appsec": "stevedore",
    "candidate-2376-security-cryptography": "cryptography",
}

#: Native-compile tests that cannot pass as shipped (no compiler in image).
NEEDS_COMPILER: dict[str, str] = {
    "candidate-0109-science-robotics": "test_core_native_cpp_sampler_compiles_and_runs",
    "candidate-2836-ml-kernels": "test_core_changed_cpu_modules_compile_as_cxx20",
}

#: Tasks with no validated repair: task -> reason.
DISCARD: dict[str, str] = {
    "candidate-0674-ml-evaluation": (
        "grader imports torch at collection; torch is not in the image "
        "(curated library/task-findings/terminal/candidate-0674-ml-evaluation.json, "
        "HAR-88); baking torch (~800 MB) for one task is rejected"
    ),
}

COLUMNS = (
    "task_id",
    "image_12",
    "decision",
    "reason",
    "guard_variant",
    "run_package",
    "run_transform",
    "run_variant_status",
    "validation",
    "evidence",
)

COMPILER_TOKENS = ("g++", "gcc", "clang", "nvcc")


def task_image_12(task_dir: Path) -> str:
    text = (task_dir / "task.toml").read_text(encoding="utf-8")
    match = re.search(r"docker_image = \"[^\"]*sha256:([0-9a-f]{12})", text)
    return match.group(1) if match else ""


def grader_imports(task_dir: Path) -> list[str]:
    """Third-party top-level imports of tests/test_outputs.py."""
    text = (task_dir / "tests" / "test_outputs.py").read_text(encoding="utf-8")
    mods = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", text, re.M))
    return sorted(m for m in mods if m not in sys.stdlib_module_names)


def grader_uses_compiler(task_dir: Path) -> bool:
    text = (task_dir / "tests" / "test_outputs.py").read_text(encoding="utf-8")
    return "subprocess" in text and any(tok in text for tok in COMPILER_TOKENS)


def guard_record(records: list[dict]) -> dict | None:
    for record in records:
        if record.get("transform") == GUARD_TRANSFORM:
            return record
    return None


def decide(task_id: str, *, has_compiler_test: bool) -> tuple[str, str]:
    """Ledger decision and reason for one task (pure; tested)."""
    if task_id in DISCARD:
        return "discard", DISCARD[task_id]
    if task_id in PREFETCH:
        return (
            "fix",
            f"grader collection needs {PREFETCH[task_id]} (missing from image); "
            f"run the guard-on-prefetch composition ({PREFETCH_TRANSFORM} + {GUARD_TRANSFORM})",
        )
    if task_id in NEEDS_COMPILER or has_compiler_test:
        test = NEEDS_COMPILER.get(task_id, "native-compile test")
        return (
            "fix",
            f"{test} fails as shipped with FileNotFoundError: 'g++' (no compiler "
            f"in image); needs a setup-time compiler prefetch, not built in this change",
        )
    return (
        "keep",
        f"run the {GUARD_TRANSFORM} variant (hook-planting hole closed; honest grading unchanged)",
    )


def load_guard_records(records_dir: Path) -> dict[str, list[dict]]:
    """Latest-first guard/prefetch records per task id suffix."""
    out: dict[str, list[dict]] = {}
    for record_path in sorted(records_dir.glob("mimo-v2.6-rl__candidate-*/*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("transform") not in (GUARD_TRANSFORM, PREFETCH_TRANSFORM):
            continue
        task_name = record.get("task_name", "")
        task_id = task_name.split("/")[-1]
        out.setdefault(task_id, []).append(record)
    for records in out.values():
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def build(
    snapshot: Path,
    records_dir: Path,
    validation: dict,
    out_path: Path,
) -> list[dict]:
    tasks = sorted(p.name for p in snapshot.iterdir() if p.is_dir())
    records = load_guard_records(records_dir)
    rows: list[dict] = []
    for task_id in tasks:
        task_dir = snapshot / task_id
        recs = records.get(task_id, [])
        guard = guard_record(recs)
        if guard is None:
            raise SystemExit(f"no {GUARD_TRANSFORM} record for {task_id}")
        digest12 = guard["variant_digest"][:12]
        decision, reason = decide(task_id, has_compiler_test=grader_uses_compiler(task_dir))
        validation_note = validation.get(task_id, "static-only (image not cached)")
        status = guard.get("status", "candidate")
        rows.append(
            {
                "task_id": task_id,
                "image_12": task_image_12(task_dir),
                "decision": decision,
                "reason": reason,
                "guard_variant": digest12,
                "run_package": digest12,
                "run_transform": GUARD_TRANSFORM,
                "run_variant_status": status,
                "validation": validation_note,
                "evidence": f"library/task-variants/mimo-v2.6-rl__{task_id}/{digest12}.json",
            }
        )
    with out_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--records", default=str(ROOT / "library" / "task-variants"))
    ap.add_argument("--validation", default=str(HERE / "validation.json"))
    ap.add_argument("--output", default=str(HERE / "ledger.csv"))
    args = ap.parse_args(argv)
    snapshot = Path(args.snapshot) if args.snapshot else _default_snapshot()
    validation = json.loads(Path(args.validation).read_text(encoding="utf-8"))
    rows = build(snapshot, Path(args.records), validation, Path(args.output))
    decisions: dict[str, int] = {}
    for row in rows:
        decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
    print(f"{len(rows)} ledger rows -> {args.output}: {decisions}")


def _default_snapshot() -> Path:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    gitdir = Path(proc.stdout.strip())
    if not gitdir.is_absolute():
        gitdir = ROOT / gitdir
    primary = gitdir.parent
    short = HF_REVISION[:12]
    return primary / "derived" / "task-store" / "hf" / f"{HF_REPO.replace('/', '__')}@{short}" / "tasks"


if __name__ == "__main__":
    main()
