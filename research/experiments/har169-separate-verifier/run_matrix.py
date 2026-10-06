#!/usr/bin/env python3
"""HAR-169 separate-verifier prototype runner (Harbor 0.24, local Docker, $0).

Subcommands:
  derive    derive the 3 separate-verifier@1 variants via
            evallab.separate_verifier (records -> library/task-variants,
            packages -> shared derived/task-store/variants).
  stage     stage ephemeral baseline packages in /tmp (parent + reference
            solution; harness-only, no lineage records).
  run       run one matrix cell: harbor 0.24, --env docker, oracle|nop.
  regrade   regrade one recorded trial with a variant package.
  collect   scan the jobs dir and emit results.json + a markdown table.

Everything runs locally (no Daytona, no queue, no model spend).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parent.parent.parent  # research/experiments/<exp> -> repo root
LEDGER = REPO_ROOT / "research/experiments/python-task-ledger/ledger.csv"
HF_CODE = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
VARIANTS = Path("/Users/petermakhnatch/Developer/eval-lab/derived/task-store/variants")
HF_REPO = "FineEnvs/MiMo-V2.6-RL-harbor-code"
HF_REVISION = "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"
HARBOR_WHEEL = "/tmp/harbor024/harbor-0.24.0-py3-none-any.whl"

TASKS: dict[str, dict[str, str]] = {
    "format-code-task-001269": {
        "parent": str(VARIANTS / "mimo-v2.6-rl__format-code-task-001269" / "541d416818c7"),
        "parent_source": json.dumps(
            {
                "kind": "variant",
                "record": "library/task-variants/"
                "mimo-v2.6-rl__format-code-task-001269/541d416818c7.json",
            }
        ),
        "marker": "test_activate_strict_unfired_raises",
    },
    "format-code-task-002391": {
        "parent": str(HF_CODE / "format-code-task-002391"),
        "parent_source": json.dumps(
            {
                "kind": "hf",
                "repo": HF_REPO,
                "revision": HF_REVISION,
                "path": "tasks/format-code-task-002391",
            }
        ),
        "marker": "test_audit_collapses_two_records_and_merges_aliases",
    },
    "format-code-task-000905": {
        "parent": str(HF_CODE / "format-code-task-000905"),
        "parent_source": json.dumps(
            {
                "kind": "hf",
                "repo": HF_REPO,
                "revision": HF_REVISION,
                "path": "tasks/format-code-task-000905",
            }
        ),
        "marker": "TestWeightNegativeAndHighFix",
    },
}


def ledger_digest(task_id: str) -> str:
    with open(LEDGER, newline="") as handle:
        for row in csv.DictReader(handle):
            if row["task_id"] == task_id:
                return row["run_digest"]
    raise KeyError(task_id)


def cmd_derive(_args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from evallab.registry import task_directory_digest
    from evallab.separate_verifier import derive_separate_verifier
    from evallab.task_variants import VariantExistsError

    for task_id, spec in TASKS.items():
        parent = Path(spec["parent"])
        actual = task_directory_digest(parent)
        expected = ledger_digest(task_id)
        status = "OK " if actual == expected else "MISMATCH"
        print(f"{status} parent {task_id}: {actual[:19]} (ledger {expected[:19]})")
        if actual != expected:
            return 1
        solution = (EXP_DIR / "solutions" / f"{task_id}.solve.sh").read_bytes()
        solution_digest = f"sha256:{hashlib.sha256(solution).hexdigest()}"
        try:
            record = derive_separate_verifier(
                parent,
                marker=spec["marker"],
                solution_sh=solution,
                rationale=(
                    "HAR-169 prototype: bundle hidden tests into a separate "
                    "verifier environment; pass the agent workspace as a "
                    "declared artifact. Ships an oracle-control reference "
                    "solution for oracle|nop grading comparison."
                ),
                created_by="har169-separate-verifier",
                repo_root=REPO_ROOT,
                parent_source=json.loads(spec["parent_source"]),
            )
            print(f"derived {task_id}: {record.variant_digest} record={record.digest12}")
        except VariantExistsError:
            slug = f"mimo-v2.6-rl__{task_id}"
            records = sorted((REPO_ROOT / "library/task-variants" / slug).glob("*.json"))
            matching = [
                path
                for path in records
                if json.loads(path.read_text()).get("inputs", {}).get("solution") == solution_digest
            ]
            if len(matching) != 1:
                print(f"STALE existing record for {task_id}: {records}")
                return 1
            print(f"exists, inputs match, skipping: {matching[0].name}")
    return 0


def cmd_stage(_args: argparse.Namespace) -> int:
    """Stage ephemeral original-mode baselines (parent + solution, /tmp only)."""
    base = Path("/tmp/har169-sepver-baseline")
    for task_id in TASKS:
        dest = base / task_id
        if dest.exists():
            print(f"exists, skipping: {dest}")
            continue
        shutil.copytree(TASKS[task_id]["parent"], dest, symlinks=False)
        for path in (dest, *dest.rglob("*")):
            path.chmod(path.stat().st_mode | 0o200)
        solve = (EXP_DIR / "solutions" / f"{task_id}.solve.sh").read_bytes()
        sol_dir = dest / "solution"
        sol_dir.mkdir(exist_ok=True)
        (sol_dir / "solve.sh").write_bytes(solve)
        print(f"staged {dest}")
    return 0


def harbor(*args: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uvx", "--from", HARBOR_WHEEL, "harbor", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/tmp",
    )


def cmd_run(args: argparse.Namespace) -> int:
    jobs_dir = Path(args.jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    proc = harbor(
        "run",
        "--env",
        "docker",
        "-a",
        args.agent,
        "-p",
        args.task_pkg,
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        args.job_name,
        "-n",
        "1",
        "-y",
        "-q",
    )
    wall = time.time() - start
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    print(f"rc={proc.returncode} wall_sec={wall:.0f}")
    return proc.returncode


def cmd_regrade(args: argparse.Namespace) -> int:
    trials_dir = Path(args.trials_dir)
    trials_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    proc = harbor(
        "trials",
        "regrade",
        args.source_trial,
        "-p",
        args.task_pkg,
        "--env",
        "docker",
        "--trial-name",
        args.trial_name,
        "-o",
        str(trials_dir),
    )
    wall = time.time() - start
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    print(f"rc={proc.returncode} wall_sec={wall:.0f}")
    return proc.returncode


def trial_reward(trial_dir: Path) -> dict:
    """Extract reward + mode facts from a trial directory (best effort)."""
    out: dict = {"trial_dir": str(trial_dir)}
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            out["result"] = result
        except Exception as exc:  # noqa: BLE001
            out["result_error"] = str(exc)
    verifier_dir = trial_dir / "verifier"
    for name in ("reward.txt", "reward.json"):
        candidate = verifier_dir / name
        if candidate.is_file():
            out[name] = candidate.read_text().strip()[:500]
    manifest = trial_dir / "artifacts" / "manifest.json"
    out["has_manifest"] = manifest.is_file()
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    jobs_dir = Path(args.jobs_dir)
    rows = []
    for trial_dir in sorted(jobs_dir.glob("**/result.json")):
        rows.append(trial_reward(trial_dir.parent))
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(
            {
                "collected_at": datetime.now(UTC).isoformat(),
                "rows": rows,
            },
            indent=2,
        )
    )
    print(f"collected {len(rows)} trials -> {out_path}")
    for row in rows:
        result = row.get("result", {})
        print(f"- {row['trial_dir']}: rewards={result.get('rewards')}")
    return 0


def cmd_compose(_args: argparse.Namespace) -> int:
    """Derive integrity+separate compositions on 001269 (dirty/clean oracles).

    Order is integrity FIRST (appends the scoring tail to tests/test.sh),
    separate SECOND (folds the tailed script into tests/test-orig.sh behind
    the snapshot-restoring wrapper). The reverse order would strand the tail
    after the wrapper's ``exec`` line.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from evallab.integrity_reward import derive_variant as derive_integrity
    from evallab.separate_verifier import derive_separate_verifier

    task_id = "format-code-task-001269"
    spec = TASKS[task_id]
    parent = Path(spec["parent"])
    integrity_record = derive_integrity(
        parent,
        created_by="har169-separate-verifier-compose",
        parent_source=json.loads(spec["parent_source"]),
        repo_root=REPO_ROOT,
    )
    print(f"integrity: {integrity_record.variant_digest} record={integrity_record.digest12}")
    integrity_pkg = (
        Path("/Users/petermakhnatch/Developer/eval-lab/derived/task-store/variants")
        / integrity_record.task_slug
        / integrity_record.digest12
    )
    integrity_ref = (
        f"library/task-variants/{integrity_record.task_slug}/{integrity_record.digest12}.json"
    )
    for flavor in ("dirty", "clean"):
        solution = (EXP_DIR / "solutions" / f"{task_id}-{flavor}.solve.sh").read_bytes()
        record = derive_separate_verifier(
            integrity_pkg,
            marker=spec["marker"],
            solution_sh=solution,
            rationale=(
                "HAR-169 composition proof: rewardkit-integrity@1 scoring "
                f"inside a separate-verifier@1 package ({flavor} trajectory)."
            ),
            created_by="har169-separate-verifier-compose",
            repo_root=REPO_ROOT,
            parent_source={"kind": "variant", "record": integrity_ref},
        )
        print(f"composed-{flavor}: {record.variant_digest} record={record.digest12}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("derive")
    sub.add_parser("stage")
    sub.add_parser("compose")
    run = sub.add_parser("run")
    run.add_argument("--task-pkg", required=True)
    run.add_argument("--agent", required=True, choices=["oracle", "nop"])
    run.add_argument("--job-name", required=True)
    run.add_argument("--jobs-dir", required=True)
    regrade = sub.add_parser("regrade")
    regrade.add_argument("--source-trial", required=True)
    regrade.add_argument("--task-pkg", required=True)
    regrade.add_argument("--trial-name", required=True)
    regrade.add_argument("--trials-dir", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--jobs-dir", required=True)
    collect.add_argument("--out", required=True)
    args = parser.parse_args()
    handlers = {
        "derive": cmd_derive,
        "stage": cmd_stage,
        "compose": cmd_compose,
        "run": cmd_run,
        "regrade": cmd_regrade,
        "collect": cmd_collect,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
