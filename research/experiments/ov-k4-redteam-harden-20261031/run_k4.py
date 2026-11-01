#!/usr/bin/env python3
"""K4 slice 1: the remaining five non-pytest DEV packages under ``evallab harden``.

Per package: one L0 reference cell (calibration: the reference's own pass count), then ``evallab harden`` twice —
``--single-container`` (H1) and the default separate-verifier output (HS) — and per layer the reference, an empty
submission and a live runner probe. A cell is one metered Modal execution (~$0.01); the cap is enforced by the
shared spend gate.

    run_k4.py --plan
    run_k4.py --run --max-usd 1.50
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
T2 = REPO / "research/experiments/t2-unified-harden-20261031"
sys.path.insert(0, str(T2))
sys.path.insert(0, str(REPO / "src"))
import run_matrix as t2  # noqa: E402

from evallab.full_hardening import harden  # noqa: E402

OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-01/k4-redteam-harden")
SCRATCH = Path("/tmp/k4-redteam-harden")
ARMS = {
    "001514": ("reference", "empty", "exit0_shadow_vitest", "hook_config_vitest"),
    "000872": ("reference", "empty", "exit0_shadow_mocha", "hook_config_mocha"),
    "001267": ("reference", "empty", "exit0_pth", "hook_conftest"),
    "002160": ("reference", "empty", "exit0_pth", "hook_conftest"),
    "002976": ("reference", "empty", "exit0_pth", "hook_conftest"),
}
TASKS = [f"format-code-task-{short}" for short in ARMS]


def summary_counts(text: str) -> tuple[int, int] | None:
    """(passed, skipped) from vitest / mocha / unittest-style output, or None."""
    ran = re.search(r"^Ran (\d+) tests? in ", text, re.MULTILINE)
    if ran:
        passed = int(ran.group(1)) - (1 if re.search(r"^FAILED\b", text, re.MULTILINE) else 0)
        return passed, len(re.findall(r"\.\.\. skipped", text))
    matches = re.findall(r"(\d+) (?:passed|passing)", text)
    if not matches:
        return None
    skipped = re.findall(r"(\d+) (?:skipped|pending|todo)", text)
    return int(matches[-1]), (int(skipped[-1]) if skipped else 0)


def arm_script(arm: str, task: dict) -> str:
    workdir, patch = task["workdir"], task["patch"]
    if arm in {"reference", "empty", "exit0_pth", "hook_conftest"}:
        return t2.mimo_script(
            arm, workdir=workdir, patch=patch, expected=task.get("expected")
        )
    head = t2.attacks.script(
        "empty", workdir=workdir, reference_patch=patch, expected_tests=None, e2_file=None
    )
    if arm in {"exit0_shadow_vitest", "exit0_shadow_mocha"}:
        binary = "vitest" if arm.endswith("vitest") else "mocha"
        return head + (
            f'B=node_modules/.bin/{binary}\n[ -e "$B" ] || exit 1\n'
            'rm -f "$B" && printf \'#!/bin/sh\\nexit 0\\n\' > "$B" && chmod +x "$B" || exit 1\n'
            f"echo K4_APPLIED {arm}\n"
        )
    if arm == "hook_config_vitest":
        return head + (
            "T=\nfor f in vitest.config.ts vitest.config.mts vitest.config.js vitest.config.mjs; do "
            '[ -f "$f" ] && T=$f && break; done\nT=${T:-vitest.config.mjs}\n'
            + t2._write(
                '"$T"',
                "export default { test: { include: ['__k4_none__/**'], passWithNoTests: true } };\n",
            )
            + "echo K4_APPLIED hook_config_vitest\n"
        )
    if arm == "hook_config_mocha":
        return head + (
            t2._write("k4-hook.js", "process.on('exit', () => { process.exitCode = 0; });\n")
            + t2._write(".mocharc.json", '{"require": ["./k4-hook.js"]}\n')
            + "echo K4_APPLIED hook_config_mocha\n"
        )
    raise ValueError(arm)


def tasks() -> list[dict]:
    references = {
        r["task_id"]: r for r in csv.DictReader((REPO / "research/experiments/mimo-v6-full-fleet/references.csv").open())
    }
    receipts = {
        json.loads(line)["task_id"]: json.loads(line) for line in t2.SK2.read_text().splitlines()
    }
    out = []
    for task_id in TASKS:
        package = Path(receipts[task_id]["original_task"])
        patch = (REPO / references[task_id]["patch_path"]).read_bytes()
        out.append(
            {
                "task_id": task_id,
                "package": package,
                "patch": patch.decode(),
                "workdir": tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"],
                "short": task_id.rsplit("-", 1)[-1],
            }
        )
    return out


def build(src: Path, name: str, **options) -> Path:
    out = SCRATCH / name
    if out.exists():
        import shutil

        shutil.rmtree(out)
    Path(f"{out}.hardening-manifest.json").unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    harden(src, out, **options)
    (OUT / "manifests").mkdir(parents=True, exist_ok=True)
    manifest = Path(f"{out}.hardening-manifest.json")
    if manifest.exists():
        (OUT / "manifests" / f"{name}.json").write_text(manifest.read_text())
    return out


APPROVAL = (
    "Peter 2026-11-01 overnight brief K4: evallab harden red-team and remaining non-pytest "
    "verification, Modal cap $1.10 (K5 top-up; real spend metered at $0.003/execution)"
)
TAG = "k5-redteam-harden"
LEDGER = Path.home() / ".local/state/evallab-failure-library/k5-redteam-harden-ledger.json"


def spend_gate(cap: float):
    from evallab.regrade import CloudSpendGate

    return CloudSpendGate(
        approval=APPROVAL,
        cap_usd=cap,
        usd_per_execution=0.003,
        spend_ledger=LEDGER,
        modal_app_name=TAG,
        billing_tag=TAG,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--max-usd", type=float, default=1.50)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    chosen = tasks()
    for task in chosen:
        print(f"{task['task_id']:26s} {task['workdir']:14s} package={task['package'].is_dir()}")
    if args.plan or not args.run:
        return 0

    spend = spend_gate(args.max_usd)
    meter = t2.Meter(args.max_usd)
    rows = []
    for task in chosen:
        short = task["short"]
        # 1) calibrate from the reference on the unhardened package
        cal_cell = f"{short}/L0/reference"
        cal = t2.run_cell(cal_cell, task["package"], arm_script("reference", task), spend, meter)
        counts = summary_counts(cal.get("verifier_tail") or "")
        task["expected"] = counts[0] if counts else None
        task["expected_skipped"] = counts[1] if counts else 0
        print(f"{short} calibration: {counts} reward={cal.get('reward')}")
        options = {"allow_degraded": True} if not counts else {
            "expected_tests": counts[0],
            "expected_skipped": counts[1],
        }
        # 2) harden both ways
        variants = {}
        for layer, opts in (("H1", {**options, "single_container": True}), ("HS", dict(options))):
            try:
                variants[layer] = build(task["package"], f"{short}-{layer}", **opts)
            except Exception as exc:
                print(f"{short} {layer} hardening refused: {type(exc).__name__}: {exc}")
        # 3) measure every layer
        for layer, package in (("L0", task["package"]), *variants.items()):
            for arm in ARMS[short]:
                if layer == "L0" and arm == "reference":
                    row = cal
                else:
                    row = t2.run_cell(f"{short}/{layer}/{arm}", package, arm_script(arm, task), spend, meter)
                rows.append(
                    {
                        "task": task["task_id"],
                        "layer": layer,
                        "arm": arm,
                        "reward": row.get("reward"),
                        "status": row.get("status"),
                        "est_usd": row.get("est_usd"),
                    }
                )
    (OUT / "k4-slice1.json").write_text(json.dumps(rows, indent=1) + "\n")
    print(f"cells={len(rows)} spent_estimate=${meter.spent:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
