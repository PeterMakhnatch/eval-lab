#!/usr/bin/env python3
"""K3 executed matrix: the eight non-pytest DEV packages, original (L0) vs ``nonpytest-hardening@1`` (H).

Reuses Stream A's metered Modal path (``research/experiments/h1-grader-hardening-20261030``): one cell is one
container trial, admitted against a sandbox-second cost estimate, so a cell costs ~$0.002 rather than the $1.06
reservation floor a campaign cell carries.

Arms per package:
``correct``  the reference patch (must score 1.0 at both layers: zero false rejects)
``empty``    nothing (must score 0.0 at both layers)
``J1``       ``package.json`` test script replaced with ``exit 0``
``J2``       runner config override: vitest ``passWithNoTests`` empty include / mocha require hook that forces exit 0
``U1``       ``sitecustomize.py`` that calls ``os._exit(0)`` (unittest/site-based runners)
``P_UNSET``  ``conftest.py`` whose ``pytest_sessionfinish`` forces exit status 0

    run_matrix_k3.py --plan
    run_matrix_k3.py --run --max-usd 2.00
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
H1 = REPO / "research/experiments/h1-grader-hardening-20261030"
sys.path.insert(0, str(H1))
sys.path.insert(0, str(REPO / "src"))
import attacks  # noqa: E402
import run_matrix  # noqa: E402

from evallab.nonpytest_hardening import derive_nonpytest_hardening  # noqa: E402
from evallab.task_variants import default_variants_root  # noqa: E402

PACKAGES = Path.home() / "Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024/original-packages"
FAMILIES = {
    "000603": "vitest",
    "001514": "vitest",
    "000872": "mocha",
    "000838": "unittest",
    "002139": "pytest",
    "001267": "pytest",
    "002160": "pytest",
    "002976": "pytest",
}
OUT = Path.home() / ".local/state/evallab-prod-live/k3-nonpytest-matrix"
LEDGER = "k3-nonpytest-ledger.json"

_J1 = '{"scripts": {"test": "exit 0"}}\n'
_J2_VITEST = "export default { test: { include: [], passWithNoTests: true } };\n"
_J2_MOCHA_HOOK = "process.on('exit', () => { process.exitCode = 0; });\n"
_J2_MOCHA = '{"require": ["./k3-hook.js"]}\n'
_U1 = "import atexit, os\n\natexit.register(lambda: os._exit(0))\n"
_P_UNSET = "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n"


def _write(arm: str, family: str) -> str:
    """Extra agent-side files for one probe arm (appended to the no-op 'empty' script)."""
    if arm == "J1":
        return f'printf %s {json.dumps(_J1)} > package.json\n'
    if arm == "J2":
        if family == "vitest":
            return f'printf %s {json.dumps(_J2_VITEST)} > vitest.config.ts\n'
        return (
            f'printf %s {json.dumps(_J2_MOCHA_HOOK)} > k3-hook.js\n'
            + f'printf %s {json.dumps(_J2_MOCHA)} > .mocharc.json\n'
        )
    if arm == "U1":
        return f'printf %s {json.dumps(_U1)} > sitecustomize.py\n'
    if arm == "P_UNSET":
        return f'printf %s {json.dumps(_P_UNSET)} > conftest.py\n'
    raise ValueError(f"unknown probe arm {arm!r}")


def arm_script(arm: str, *, workdir: str, reference_patch: str, family: str) -> str:
    if arm in {"correct", "empty"}:
        return attacks.script(
            arm, workdir=workdir, reference_patch=reference_patch, expected_tests=None, e2_file=None
        )
    body = attacks.script(
        "empty", workdir=workdir, reference_patch=reference_patch, expected_tests=None, e2_file=None
    )
    return body + _write(arm, family) + f"echo K3_APPLIED {arm}\n"


def select() -> list[dict]:
    references = {
        r["task_id"]: r
        for r in csv.DictReader((REPO / "research/experiments/mimo-v6-full-fleet/references.csv").open())
    }
    chosen = []
    for short, family in FAMILIES.items():
        task_id = f"format-code-task-{short}"
        package = PACKAGES / task_id
        if not package.is_dir():
            for alt in sorted(
                (Path.home() / "Developer/eval-lab-results").glob(f"*/*/{task_id}")
            ):
                if (alt / "tests" / "test.sh").is_file():
                    package = alt
                    break
        ref = references.get(task_id)
        if not package.is_dir() or not ref:
            print(f"skip {task_id}: package or reference missing", file=sys.stderr)
            continue
        patch_path = REPO / ref["patch_path"]
        if not patch_path.is_file():
            print(f"skip {task_id}: reference patch absent", file=sys.stderr)
            continue
        chosen.append(
            {
                "task_id": task_id,
                "family": family,
                "package": package,
                "patch": patch_path.read_text(),
                "workdir": "WORKDIR",
            }
        )
    return chosen


def variant_dir(task_id: str, record) -> Path:
    slug = f"mimo-v2.6-rl__{task_id}"
    digest = record.variant_digest.split(":", 1)[1][:12]
    return default_variants_root(REPO) / slug / digest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--max-usd", type=float, default=2.00)
    ap.add_argument("--arms", default="correct,empty,J1,J2,U1,P_UNSET")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    arms = [a for a in args.arms.split(",") if a]
    tasks = [t for t in select() if not args.only or t["task_id"] in args.only.split(",")]
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 1
    for task in tasks:
        try:
            record = derive_nonpytest_hardening(
                task["package"], family=task["family"], expected_passed=None,
                allow_degraded=True, repo_root=REPO,
            )
        except Exception as exc:  # already derived, or refused: report and continue
            print(f"derive {task['task_id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if "already" in str(exc).lower():
                record = None
            else:
                continue
        if record is not None:
            task["hardened"] = variant_dir(task["task_id"], record)
        else:
            # resolve strictly by lineage record: never guess a variant directory
            base = REPO / "library/task-variants" / f"mimo-v2.6-rl__{task['task_id']}"
            rec = None
            candidates = []
            for path in sorted(base.glob("*.json")):
                data = json.loads(path.read_text())
                if data.get("transform") == "nonpytest-hardening@1":
                    candidates.append(data)
            # a calibrated variant (degraded_count_gate False) is the one the matrix must test
            calibrated = [
                d for d in candidates
                if "'degraded_count_gate': False" in json.dumps(d) or '"degraded_count_gate": false' in json.dumps(d)
            ]
            pool = calibrated or candidates
            rec = max(pool, key=lambda d: d.get("created_at", "")) if pool else None
            if rec is None:
                print(f"{task['task_id']}: no nonpytest-hardening@1 lineage record", file=sys.stderr)
                continue
            digest = rec["variant_digest"].split(":", 1)[1][:12]
            task["hardened"] = (
                default_variants_root(REPO) / f"mimo-v2.6-rl__{task['task_id']}" / digest
            )
        print(
            f"{task['task_id']:26s} family={task['family']:9s} L0={task['package'].exists()} "
            f"H={bool(task['hardened'] and task['hardened'].is_dir())} -> {task['hardened']}"
        )
    if args.plan or not args.run:
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    spend = run_matrix.gate(args.max_usd, 0.002, LEDGER)
    meter = run_matrix.Meter(args.max_usd)
    results = []
    for task in tasks:
        for layer, package in (("L0", task["package"]), ("H", task["hardened"])):
            if package is None or not Path(package).is_dir():
                continue
            for arm in arms:
                if arm in {"U1", "P_UNSET"} and task["family"] not in {"unittest", "pytest"}:
                    continue
                if arm in {"J1", "J2"} and task["family"] not in {"vitest", "mocha"}:
                    continue
                cell = OUT / f"{task['task_id']}/{layer}/{arm}"
                if (cell / "result.json").exists():
                    results.append(json.loads((cell / "result.json").read_text()))
                    continue
                workdir = "/workspace/repo" if task["task_id"] == "format-code-task-000603" else "/testbed"
                script = arm_script(
                    arm, workdir=workdir, reference_patch=task["patch"], family=task["family"]
                )
                try:
                    row = run_matrix.run_cell(Path(cell), Path(package), script, spend, meter)
                except Exception as exc:
                    row = {"error": f"{type(exc).__name__}: {exc}"}
                    (cell).mkdir(parents=True, exist_ok=True)
                    (cell / "result.json").write_text(json.dumps(row, indent=1) + "\n")
                row.update({"task_id": task["task_id"], "family": task["family"], "layer": layer, "arm": arm})
                results.append(row)
                print(f"{task['task_id']} {layer} {arm:8s} -> reward={row.get('reward')} err={row.get('error','')}"[:160])
                if meter.spent > args.max_usd * 0.95:
                    print(f"stopping: spent ${meter.spent:.3f}", file=sys.stderr)
                    break
    (OUT / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    print(f"cells={len(results)} spent_estimate=${meter.spent:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
