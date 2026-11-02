"""Runner for 8-Task 6-Gate Gold Certification on Modal (W3B-EXHAUSTIVE-GOLD-CERTIFIER)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.gold_certifier import (
    GOLD_CONTRACT_TEST_NAME,
    build_synthesis_prompt,
    exit0_pth_script,
    extract_code_block,
    extract_hook_command_from_test_sh,
    format_wilson,
    mutant_diff,
    nop_script,
    reference_script,
    split_mutants,
    stage_and_harden_package,
    validate_contract_test,
    witness_script,
)
from evallab.mutant_auditor import (
    SandboxMeter,
    modal_cell_runner,
    mutant_script,
    mutants_for_patch,
    parse_probe,
)
from evallab.production.corpus_queue import CorpusRegistry

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RESULTS_HOME = Path("/Users/petermakhnatch/Developer/eval-lab-results")
DATE_STR = "2026-11-01"
TAG = "w3b-exhaustive-gold-certifier"
OUT = RESULTS_HOME / DATE_STR / TAG
RESULTS = HERE / "results"
STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "w3b-exhaustive-gold-certifier-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
APPROVAL = "Peter 2026-10-31 overnight factory W3B-EXHAUSTIVE-GOLD-CERTIFIER: Modal cap $0.95"
CAP_USD = 0.95

DATASET = "mimo-defect-map-v1"
MODEL = "google-antigravity/gemini-3.8-flash"
WORKER_YML = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/g38-flash/worker.yml")
STING_RESULTS = RESULTS_HOME / "2026-11-01/w2-sting-mutants"
REFERENCES_FILE = REPO / "research/experiments/mimo-v6-full-fleet/references.csv"

TASKS: dict[str, dict[str, str]] = {
    "000079": {"kind": "repaired", "py": "3"},
    "000232": {"kind": "repaired", "py": "3"},
    "001267": {"kind": "repaired", "py": "2.7"},
    "001850": {"kind": "repaired", "py": "3"},
    "000682": {"kind": "original", "py": "3", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python3'},
    "000931": {"kind": "original", "py": "3", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python3'},
    "000484": {
        "kind": "original",
        "py": "2.7",
        "hook": 'cd "$CWD" && export PATH="$HOME/.pyenv/bin:$PATH" && eval "$(pyenv init -)" '
        "&& MPLBACKEND=Agg PYTHONPATH=/testbed python",
    },
    "003005": {"kind": "original", "py": "3", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python'},
}

STRIP_PATCH_FILES = re.compile(r"\.b64$|\.json$|^mimo_test_command\.sh$")
MAX_PROMPT_SECTION = 40_000


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def task_id(short: str) -> str:
    return f"format-code-task-{short}"


def repaired_snapshot(short: str) -> Path:
    root = RESULTS_HOME / "2026-10-24/mimo-task-quality-30-20261024/evidence/packages" / task_id(short)
    snapshots = sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file())
    return snapshots[-1]


def original_package(short: str) -> Path:
    p = RESULTS_HOME / f"2026-10-24/mimo-task-quality-30-20261024/original-packages/{task_id(short)}"
    if not p.exists():
        p = Path(f"/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/{task_id(short)}")
    return p


def patch_sections(patch: str, keep: Any) -> str:
    out: list[str] = []
    capture = False
    for line in patch.splitlines(keepends=True):
        header = re.match(r"diff --git a/(\S+) b/", line)
        if header:
            capture = keep(header.group(1))
        if capture:
            out.append(line)
    text = "".join(out)
    return text if len(text) <= MAX_PROMPT_SECTION else text[:MAX_PROMPT_SECTION] + "\n[... truncated]\n"


def load_cohort() -> dict[str, dict[str, Any]]:
    registry = CorpusRegistry(repo_root=REPO)
    references = {r["task_id"]: r for r in csv.DictReader(REFERENCES_FILE.open())}
    cohort: dict[str, dict[str, Any]] = {}

    for short, spec in TASKS.items():
        tid = task_id(short)
        if registry.assigned_split(DATASET, task_id=tid) != "dev" or registry.assigned_test_anywhere(task_id=tid):
            raise SystemExit(f"{tid} is not strictly DEV")

        patch_path = REPO / references[tid]["patch_path"]
        patch = patch_path.read_text(encoding="utf-8")
        if sha256_text(patch) != references[tid]["patch_sha256"]:
            raise SystemExit(f"{tid}: patch sha256 mismatch")

        base_pkg = repaired_snapshot(short) if spec["kind"] == "repaired" else original_package(short)
        probe_path = STING_RESULTS / "tasks" / tid / "probe/result.json"
        probe_data = json.loads(probe_path.read_text())
        sources = parse_probe(probe_data["agent_log"])

        mutants = mutants_for_patch(patch, sources, limit=8)
        train_mutants, eval_mutants = split_mutants(mutants)

        cand_dir = RESULTS_HOME / "2026-10-24/mimo-task-quality-30-20261024/evidence/candidates" / tid
        witnesses = sorted(cand_dir.glob("*/solve.sh")) if spec["kind"] == "repaired" else []

        hook_cmd = spec.get("hook") or extract_hook_command_from_test_sh((base_pkg / "tests/test.sh").read_text())

        cohort[short] = {
            "task_id": tid,
            "short": short,
            "kind": spec["kind"],
            "py": spec["py"],
            "base_package": base_pkg,
            "patch": patch,
            "patch_sha256": references[tid]["patch_sha256"],
            "sources": sources,
            "all_mutants": mutants,
            "train_mutants": train_mutants,
            "eval_mutants": eval_mutants,
            "witnesses": witnesses,
            "hook_command": hook_cmd,
        }

    return cohort


def served_model(session_dir: Path) -> tuple[str | None, float]:
    model, cost = None, 0.0
    for log in session_dir.glob("*.jsonl"):
        for line in log.read_text().splitlines():
            row = json.loads(line)
            message = row.get("message") or {}
            if row.get("type") == "message" and message.get("role") == "assistant":
                model = f"{message.get('provider')}/{message.get('model')}"
                cost += float((message.get("usage") or {}).get("cost", {}).get("total", 0.0))
    return model, cost


def synthesize_task(short: str, task: dict[str, Any], feedback: str | None = None) -> dict[str, Any]:
    session_dir = OUT / "sessions" / short
    session_dir.mkdir(parents=True, exist_ok=True)

    base = task["base_package"]
    instruction = (base / "instruction.md").read_text(errors="replace")
    hidden_tests = patch_sections(
        (base / "tests/test.patch").read_text(errors="replace"),
        lambda p: not STRIP_PATCH_FILES.search(p),
    )
    reference = patch_sections(task["patch"], lambda p: p.endswith(".py"))
    existing_tests = (base / "tests/test_outputs.py").read_text(errors="replace") if (base / "tests/test_outputs.py").is_file() else None

    # ONLY train mutants diffs shown to the model!
    train_diffs = [
        mutant_diff(m, task["sources"][m.path]) for m in task["train_mutants"] if m.path in task["sources"]
    ]

    prompt = build_synthesis_prompt(
        instruction=instruction,
        hidden_tests=hidden_tests,
        reference_patch=reference,
        train_mutants=task["train_mutants"],
        train_diffs=train_diffs,
        interpreter_command=task["hook_command"],
        python_version=task["py"],
        existing_tests=existing_tests,
        feedback=feedback,
    )

    (RESULTS / "prompts").mkdir(parents=True, exist_ok=True)
    (RESULTS / "prompts" / f"{short}.md").write_text(prompt, encoding="utf-8")

    started = time.monotonic()
    proc = subprocess.run(
        [
            "omp",
            "-p",
            "--model",
            MODEL,
            "--thinking",
            "high",
            "--config",
            str(WORKER_YML),
            "--no-tools",
            "--no-skills",
            "--session-dir",
            str(session_dir),
            prompt,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=900,
        cwd="/tmp",
        env={**os.environ, "OMP_NO_COLOR": "1"},
    )
    wall_s = time.monotonic() - started
    model_attested, cost = served_model(session_dir)

    code = extract_code_block(proc.stdout)
    valid, reason = validate_contract_test(code or "")

    status = "ok"
    if proc.returncode != 0 or not code:
        status = f"failed (rc={proc.returncode}, block={'yes' if code else 'no'})"
    elif not model_attested or "argon" in model_attested:
        status = f"rejected: served {model_attested}"
    elif not valid:
        status = f"refused: {reason}"

    record = {
        "task": short,
        "status": status,
        "served_model": model_attested,
        "cost_usd": round(cost, 4),
        "wall_s": round(wall_s, 1),
        "test_lines": len(code.splitlines()) if code else 0,
        "test_sha256": sha256_text(code) if code else None,
        "reason": reason,
    }

    if code:
        (RESULTS / "tests").mkdir(parents=True, exist_ok=True)
        (RESULTS / "tests" / f"{short}-{GOLD_CONTRACT_TEST_NAME}.txt").write_text(code, encoding="utf-8")

    return record, code


def spend_logging_runner(inner: Any, lock: threading.Lock) -> Any:
    def run(cell_dir: Path, package: Path, script: str) -> dict[str, Any]:
        fresh = not (cell_dir / "result.json").exists()
        result = inner(cell_dir, package, script)
        if fresh:
            record = {
                "cost_usd": result["est_usd"],
                "job": f"{TAG}:{cell_dir.parent.name}/{cell_dir.name}",
                "note": (
                    f"W3B-EXHAUSTIVE-GOLD-CERTIFIER cell {cell_dir.parent.name}/{cell_dir.name} "
                    f"status={result['status']} reward={result['reward']} wall_s={result['wall_s']} "
                    "(Modal rates x1.25 estimate)"
                ),
                "route": "modal",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with lock, SPEND_LOG.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return result

    return run


def plan_cells_for_task(
    short: str,
    task: dict[str, Any],
    upgraded_pkg: Path,
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    wd = tomllib.loads((upgraded_pkg / "task.toml").read_text())["environment"]["workdir"]
    base_pkg = task["base_package"]

    def add(name: str, pkg: Path, script_text: str, marker: str, target_reward: str, arm_kind: str) -> None:
        cells.append({
            "task": short,
            "cell": name,
            "package": str(pkg),
            "script": script_text,
            "marker": marker,
            "target": target_reward,
            "arm_kind": arm_kind,
        })

    # Gate 1: Reference
    add("reference", upgraded_pkg, reference_script(wd, task["patch"]), "REFERENCE_APPLIED", "1.0", "reference")

    # Gate 2: Nop
    add("nop", upgraded_pkg, nop_script(wd), "NOP_APPLIED", "0.0", "nop")

    # Gate 3: Witness / Conftest tamper
    if task["witnesses"]:
        for solve in task["witnesses"]:
            add(f"witness-{solve.parent.name}", upgraded_pkg, witness_script(wd, solve.read_text()), "WITNESS_APPLIED", "0.0", "witness")
    else:
        add("witness-conftest", upgraded_pkg, witness_script(wd, None), "WITNESS_APPLIED conftest", "0.0", "witness")

    # Gate 4: exit0_pth
    add("exit0_pth", upgraded_pkg, exit0_pth_script(wd), "EXIT0_PTH_APPLIED", "0.0", "exit0_pth")

    # Gate 5: Train mutants on upgraded package
    for m in task["train_mutants"]:
        script = mutant_script(workdir=wd, reference_patch=task["patch"], mutant=m)
        add(f"train-{m.operator}-L{m.line}", upgraded_pkg, script, "MUTANT_APPLIED", "0.0", "train_mutant")

    # Gate 6: Eval mutants on upgraded package
    for m in task["eval_mutants"]:
        script = mutant_script(workdir=wd, reference_patch=task["patch"], mutant=m)
        add(f"eval-{m.operator}-L{m.line}", upgraded_pkg, script, "MUTANT_APPLIED", "0.0", "eval_mutant")

    # Eval mutants on BASE package (to see which ones base grader accepted)
    for m in task["eval_mutants"]:
        script = mutant_script(workdir=wd, reference_patch=task["patch"], mutant=m)
        add(f"evalbase-{m.operator}-L{m.line}", base_pkg, script, "MUTANT_APPLIED", "measured", "eval_base_mutant")

    return cells


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthesize", action="store_true", help="Run OMP synthesis with gemini-3.8-flash")
    parser.add_argument("--harden", action="store_true", help="Run evallab harden L1-L4 on upgraded packages")
    parser.add_argument("--plan", action="store_true", help="Generate cell plan")
    parser.add_argument("--run", action="store_true", help="Execute planned cells on Modal")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Modal cell executions")
    parser.add_argument("--summarize", action="store_true", help="Generate summary reports")
    parser.add_argument("--task", type=str, help="Filter to specific task")
    parser.add_argument("--cell", type=str, help="Filter to specific cell name substring")
    args = parser.parse_args()

    cohort = load_cohort()
    print(f"Loaded cohort: {len(cohort)} strictly-DEV tasks.")

    OUT.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    # Save selection.json
    selection = {
        "tasks": {
            short: {
                "task_id": t["task_id"],
                "kind": t["kind"],
                "py": t["py"],
                "train_mutants": [m.summary() for m in t["train_mutants"]],
                "eval_mutants": [m.summary() for m in t["eval_mutants"]],
            }
            for short, t in cohort.items()
        }
    }
    (RESULTS / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")

    # 1. Synthesis
    synthesis_file = RESULTS / "synthesis.json"
    synthesis: dict[str, Any] = json.loads(synthesis_file.read_text()) if synthesis_file.exists() else {}

    if args.synthesize:
        todo_tasks = [
            (short, task)
            for short, task in cohort.items()
            if not (RESULTS / "tests" / f"{short}-{GOLD_CONTRACT_TEST_NAME}.txt").exists()
            or synthesis.get(short, {}).get("status") != "ok"
        ]
        print(f"Tasks to synthesize: {[s for s, _ in todo_tasks]} (already done: {[s for s in cohort if s not in dict(todo_tasks)]})")

        synth_lock = threading.Lock()

        def synth_one(item: tuple[str, dict[str, Any]]) -> None:
            short, task = item
            print(f"Synthesizing for {short} ({task['task_id']})...", flush=True)
            feedback_file = RESULTS / "feedback" / f"{short}.md"
            feedback = feedback_file.read_text() if feedback_file.exists() else None
            rec, code = synthesize_task(short, task, feedback=feedback)
            with synth_lock:
                synthesis[short] = rec
                synthesis_file.write_text(json.dumps(synthesis, indent=2) + "\n")
            print(f"  {short}: status={rec['status']} model={rec['served_model']} cost=${rec['cost_usd']}", flush=True)

        if todo_tasks:
            with ThreadPoolExecutor(max_workers=min(2, len(todo_tasks))) as pool:
                list(pool.map(synth_one, todo_tasks))

    # 2. Harden
    manifests: dict[str, Any] = {}
    upgraded_pkgs: dict[str, Path] = {
        short: OUT / "packages" / short
        for short in cohort
        if (OUT / "packages" / short).exists()
    }
    if args.harden or (args.plan and not upgraded_pkgs) or (args.run and not upgraded_pkgs):
        (OUT / "packages").mkdir(parents=True, exist_ok=True)
        for short, task in cohort.items():
            test_file = RESULTS / "tests" / f"{short}-{GOLD_CONTRACT_TEST_NAME}.txt"
            if not test_file.exists():
                print(f"Skipping hardening for {short}: test not found.")
                continue
            code = test_file.read_text(encoding="utf-8")
            target_dir = OUT / "packages" / short
            manifest, hook_block = stage_and_harden_package(
                task["base_package"],
                target_dir,
                code,
                task["hook_command"],
                single_container=False,  # default separate-verifier L1-L4!
                allow_degraded=True,
            )
            manifests[short] = {
                "manifest": manifest.to_json(),
                "hook_block": hook_block,
                "output": str(target_dir),
            }
            upgraded_pkgs[short] = target_dir
            (RESULTS / "manifests" / f"{short}.json").write_text(
                json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n"
            )
            print(f"Hardened {short}: runner={manifest.runner} layers={[layer.layer for layer in manifest.layers]}")

    # 3. Plan cells
    cells: list[dict[str, Any]] = []
    for short, task in cohort.items():
        if short in upgraded_pkgs:
            cells.extend(plan_cells_for_task(short, task, upgraded_pkgs[short]))

    (RESULTS / "plan.json").write_text(
        json.dumps([{k: v for k, v in c.items() if k != "script"} for c in cells], indent=2) + "\n"
    )
    print(f"Total planned cells: {len(cells)}")

    # 4. Run on Modal
    if args.run:
        from evallab.modal_cells import ReserveSettleSpendGate

        gate = ReserveSettleSpendGate(
            approval=APPROVAL,
            cap_usd=CAP_USD,
            default_reservation_usd=0.003,
            spend_ledger=LEDGER,
            modal_app_name="t2-unified-harden",
            billing_tag="t2-unified-harden",
        )
        meter = SandboxMeter(CAP_USD)
        run_cell = spend_logging_runner(
            modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=meter, timeout_seconds=240),
            threading.Lock(),
        )

        def execute_one(c: dict[str, Any]) -> None:
            cell_dir = OUT / "cells" / c["task"] / c["cell"]
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "agent-script.sh").write_text(c["script"])
            try:
                res = run_cell(cell_dir, Path(c["package"]), c["script"])
                applied = c["marker"] in res.get("agent_log", "")
                print(
                    f"{c['task']}/{c['cell']}: reward={res.get('reward')} applied={applied} cost=${res.get('est_usd', 0):.4f}",
                    flush=True,
                )
            except Exception as e:
                print(f"{c['task']}/{c['cell']}: ERROR {type(e).__name__}: {e}", flush=True)

        cells_to_run = cells
        if args.task:
            cells_to_run = [c for c in cells_to_run if c["task"] == args.task]
        if args.cell:
            cells_to_run = [c for c in cells_to_run if args.cell in c["cell"]]
        print(f"Executing {len(cells_to_run)} cells on Modal with {args.workers} workers...")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(execute_one, cells_to_run))

        print(f"Modal execution finished: spent ${meter.spent:.4f} across {meter.cells} sandboxes ({meter.seconds:.1f}s).")

    # 5. Summarize
    if args.summarize or args.run:
        generate_summary(cohort, cells)


def generate_summary(cohort: dict[str, dict[str, Any]], cells: list[dict[str, Any]]) -> None:
    cell_results: dict[str, dict[str, Any]] = {}
    cells_csv_rows: list[dict[str, Any]] = []

    for c in cells:
        result_file = OUT / "cells" / c["task"] / c["cell"] / "result.json"
        if result_file.exists():
            res = json.loads(result_file.read_text())
            key = f"{c['task']}/{c['cell']}"
            cell_results[key] = res
            cells_csv_rows.append({
                "task": c["task"],
                "cell": c["cell"],
                "arm_kind": c["arm_kind"],
                "reward": res.get("reward"),
                "status": res.get("status"),
                "applied": c["marker"] in res.get("agent_log", ""),
                "est_usd": res.get("est_usd"),
                "wall_s": res.get("wall_s"),
            })

    # Write cells.csv
    if cells_csv_rows:
        with (RESULTS / "cells.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(cells_csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(cells_csv_rows)

    task_results: list[dict[str, Any]] = []

    for short, task in cohort.items():
        ref_res = cell_results.get(f"{short}/reference", {})
        nop_res = cell_results.get(f"{short}/nop", {})
        exit0_res = cell_results.get(f"{short}/exit0_pth", {})

        wit_rewards = [
            res.get("reward")
            for k, res in cell_results.items()
            if k.startswith(f"{short}/witness")
        ]

        train_cells = [
            (k, res)
            for k, res in cell_results.items()
            if k.startswith(f"{short}/train-")
        ]
        train_killed = sum(1 for _, res in train_cells if res.get("reward") == 0.0)

        eval_cells = [
            (k, res)
            for k, res in cell_results.items()
            if k.startswith(f"{short}/eval-")
        ]
        evalbase_cells = {
            k.replace("evalbase-", "eval-"): res
            for k, res in cell_results.items()
            if k.startswith(f"{short}/evalbase-")
        }

        eval_killed = sum(1 for _, res in eval_cells if res.get("reward") == 0.0)

        # Base accepted: base grader gave reward 1.0 (mutant survived base grader!)
        base_accepted = [
            k for k, res in eval_cells
            if evalbase_cells.get(k, {}).get("reward") == 1.0
        ]
        base_accepted_killed = sum(
            1 for k in base_accepted
            if cell_results.get(k, {}).get("reward") == 0.0
        )

        task_results.append({
            "task": short,
            "kind": task["kind"],
            "reference_reward": ref_res.get("reward"),
            "nop_reward": nop_res.get("reward"),
            "witness_rewards": wit_rewards,
            "exit0_pth_reward": exit0_res.get("reward"),
            "gate1_ref": ref_res.get("reward") == 1.0,
            "gate2_nop": nop_res.get("reward") == 0.0,
            "gate3_wit": all(r == 0.0 for r in wit_rewards) if wit_rewards else False,
            "gate4_exit0": exit0_res.get("reward") == 0.0,
            "train_killed": train_killed,
            "train_total": len(train_cells),
            "eval_killed": eval_killed,
            "eval_total": len(eval_cells),
            "eval_base_accepted": len(base_accepted),
            "eval_base_accepted_killed": base_accepted_killed,
        })

    n_tasks = len(task_results)
    g1 = sum(1 for t in task_results if t["gate1_ref"])
    g2 = sum(1 for t in task_results if t["gate2_nop"])
    g3 = sum(1 for t in task_results if t["gate3_wit"])
    g4 = sum(1 for t in task_results if t["gate4_exit0"])
    train_k = sum(t["train_killed"] for t in task_results)
    train_tot = sum(t["train_total"] for t in task_results)
    eval_base_acc = sum(t["eval_base_accepted"] for t in task_results)
    eval_base_k = sum(t["eval_base_accepted_killed"] for t in task_results)
    eval_tot_k = sum(t["eval_killed"] for t in task_results)
    eval_tot = sum(t["eval_total"] for t in task_results)

    summary = {
        "n_tasks": n_tasks,
        "gate1_reference": format_wilson(g1, n_tasks),
        "gate2_nop": format_wilson(g2, n_tasks),
        "gate3_witness": format_wilson(g3, n_tasks),
        "gate4_exit0_pth": format_wilson(g4, n_tasks),
        "gate5_train_mutants": format_wilson(train_k, train_tot),
        "gate6_eval_base_accepted": format_wilson(eval_base_k, eval_base_acc),
        "gate6_eval_all": format_wilson(eval_tot_k, eval_tot),
        "tasks": task_results,
    }

    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Write table.md
    lines = [
        "| task | kind | ref / nop | witness | exit0_pth (L4) | train killed | eval killed (all) | eval killed (base-survived) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in task_results:
        lines.append(
            f"| {t['task']} | {t['kind']} | {t['reference_reward']} / {t['nop_reward']} | "
            f"{'PASS' if t['gate3_wit'] else 'FAIL'} | {'PASS' if t['gate4_exit0'] else 'FAIL'} | "
            f"{t['train_killed']}/{t['train_total']} | {t['eval_killed']}/{t['eval_total']} | "
            f"{t['eval_base_accepted_killed']}/{t['eval_base_accepted']} |"
        )
    (RESULTS / "table.md").write_text("\n".join(lines) + "\n")
    print("Summary reports written to results/!")


if __name__ == "__main__":
    main()
