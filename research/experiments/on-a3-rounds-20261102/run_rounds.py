#!/usr/bin/env python3
"""A3: gold-certifier feedback rounds (rounds = 1, 2, 3 nested in one run) on the 8 tasks of last night's certifier run.

Stages (every stage is resumable: synthesis, hardened packages and Modal cells are cached by file):

    run_rounds.py prepare                 # local: mutant pools per task -> selection.json (no model, no cloud)
    run_rounds.py base   --tasks a,b      # Modal: grade the eval mutants on the ORIGINAL grader (base-accepted set)
    run_rounds.py rounds --tasks a,b      # model + Modal: the round loop (synthesize, harden, grade reference/probe/eval)
    run_rounds.py gates  --tasks a,b      # Modal: nop / witness / exit0_pth on the round-1 and final packages
    run_rounds.py status                  # local: spend and progress

Needs the lab venv first on PATH (`uv run --extra laminar`) and EVALLAB_DISABLE_LOCAL_DOCKER=1. The synthesizer is
`google-antigravity/gemini-3.8-flash` through `omp -p` (served model attested from each session log).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "research/experiments/ov-gold-certifier-8-20261101"))
import run_gold_certification as R  # noqa: E402

from evallab.gold_certifier import (  # noqa: E402
    DEFAULT_PROBES_PER_ROUND,
    build_fresh_pools,
    build_synthesis_prompt,
    exit0_pth_script,
    extract_code_block,
    mutant_diff,
    nop_script,
    reference_script,
    run_rounds,
    stage_and_harden_package,
    validate_contract_test,
    witness_script,
)
from evallab.mutant_auditor import (  # noqa: E402
    SandboxMeter,
    modal_cell_runner,
    mutant_applied,
    mutant_script,
)

DATE = "2026-11-02"
TAG = "a3-rounds"
RESULTS_HOME = Path("/Users/petermakhnatch/Developer/eval-lab-results")
OUT = RESULTS_HOME / DATE / "A3"
RAW = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/lanes/A3-raw"
)
STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "a3f-rounds-20261102-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
APPROVAL = "Peter 2026-11-02 overnight A3F: Modal cap $1.00"  # A3 used "Peter 2026-11-02 overnight A3: Modal cap $1.50"
PRIOR_USD = 1.5007  # measured estimate of the A3 run (cells present when A3F started)
A3F_CAP_USD = 1.00  # additional cap for the A3F re-measurement only
CAP_USD = PRIOR_USD + A3F_CAP_USD  # cumulative measured cap (see Cells.already_usd)
GATE_CAP_USD = A3F_CAP_USD  # persisted ledger of this campaign
USD_PER_EXECUTION = (
    0.002  # the gate books about 3 units per cell; 3 x 0.002 is about half a measured cell ($0.012)
)
SEED = "gold-rounds-v1"
ROUNDS = 3
PROBES_PER_ROUND = DEFAULT_PROBES_PER_ROUND
EVAL_EXTRA = 2
MODEL = "google-antigravity/gemini-3.8-flash"
WORKER_YML = RAW / "worker.yml"
MODAL_SLOTS = threading.BoundedSemaphore(6)
LOCK = threading.Lock()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def mutant_tag(m: Any) -> str:
    return f"{m.operator}-L{m.line}-{m.sha256[:8]}"


# ------------------------------------------------------------------ pools


def load_tasks() -> dict[str, dict[str, Any]]:
    cohort = R.load_cohort()
    for task in cohort.values():
        pools = build_fresh_pools(
            task["patch"],
            task["sources"],
            train=task["train_mutants"],
            evals=task["eval_mutants"],
            seed=SEED,
            eval_extra_limit=EVAL_EXTRA,
        )
        task["pools"] = pools
        task["evals_all"] = [*task["eval_mutants"], *pools.eval_extra]
        task["eval_old_shas"] = {m.sha256 for m in task["eval_mutants"]}
    return cohort


def prepare(cohort: dict[str, dict[str, Any]]) -> None:
    sel = {
        "seed": SEED,
        "rounds": ROUNDS,
        "probes_per_round": PROBES_PER_ROUND,
        "eval_extra_limit": EVAL_EXTRA,
        "tasks": {
            short: {
                "train": [m.summary() | {"diff": None} for m in t["train_mutants"]],
                "eval_old": [m.summary() | {"diff": None} for m in t["eval_mutants"]],
                "eval_extra": [m.summary() | {"diff": None} for m in t["pools"].eval_extra],
                "probe_pool_size": len(t["pools"].probe_pool),
                "probe_order": [
                    m.sha256 for m in t["pools"].probe_pool[: ROUNDS * PROBES_PER_ROUND]
                ],
            }
            for short, t in cohort.items()
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    text = json.dumps(sel, indent=1, sort_keys=True) + "\n"
    (OUT / "selection.json").write_text(text)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results/selection.json").write_text(text)
    for short, t in sel["tasks"].items():
        print(
            short,
            "train",
            len(t["train"]),
            "eval_old",
            len(t["eval_old"]),
            "eval_extra",
            len(t["eval_extra"]),
            "probe_pool",
            t["probe_pool_size"],
        )
    print("selection.json sha256", sha(text)[:12])


# ------------------------------------------------------------------ Modal cells


class Cells:
    def __init__(self) -> None:
        from evallab.regrade import CloudSpendGate

        gate = CloudSpendGate(
            approval=APPROVAL,
            cap_usd=GATE_CAP_USD,
            usd_per_execution=USD_PER_EXECUTION,
            spend_ledger=LEDGER,
            modal_app_name=TAG,
            billing_tag=TAG,
        )
        already = sum(
            json.loads(p.read_text()).get("est_usd", 0.0)
            for p in [
                *(OUT / "cells").glob("*/*/result.json"),
                *(OUT / "cells").glob("*/*/*/result.json"),
            ]
            if "trial" not in p.parts and "est_usd" in p.read_text()
        )
        self.already_usd = already
        self.meter = SandboxMeter(max(CAP_USD - already, 0.0))
        self.inner = modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=self.meter)

    def run(self, short: str, name: str, package: Path, script: str, marker: str) -> dict[str, Any]:
        cell_dir = OUT / "cells" / short / name
        cell_dir.mkdir(parents=True, exist_ok=True)
        (cell_dir / "agent-script.sh").write_text(script)
        done_path = cell_dir / "result.json"
        if done_path.exists():
            cached = json.loads(done_path.read_text())
            reason = cached.get("reason") or ""
            if cached["status"] == "failed" and any(
                text in reason
                for text in (
                    "Deployment failed",
                    "SpendCapReached",
                    "reservation exceeds",
                    "would reserve",
                )
            ):
                # the sandbox never started (workspace app limit or spend-gate refusal): keep the evidence, run again
                retired = OUT / "retired-infra-failed" / short / name.replace("/", "__")
                retired.parent.mkdir(parents=True, exist_ok=True)
                done_path.rename(retired.with_suffix(".json"))
        fresh = not done_path.exists()
        try:
            with MODAL_SLOTS:
                result = self.inner(cell_dir, package, script)
        except (
            Exception
        ) as exc:  # cap reached, gate refused, infrastructure: the cell stays unmeasured
            return {
                "reward": None,
                "status": f"error: {type(exc).__name__}: {exc}",
                "applied": False,
            }
        if fresh:
            record = {
                "cost_usd": result["est_usd"],
                "job": f"{TAG}:{short}/{name}",
                "note": f"A3 cell status={result['status']} reward={result['reward']} wall_s={result['wall_s']} (Modal rates x1.25 estimate)",
                "route": "modal",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with LOCK, SPEND_LOG.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        applied = marker in result.get("agent_log", "")
        measured = result["status"] == "executed" and applied
        return {
            "reward": result.get("reward") if measured else None,
            "status": result["status"],
            "applied": applied,
        }


def workdir_of(package: Path) -> str:
    return tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]


def mutant_cell(
    cells: Cells, task: dict, short: str, name: str, package: Path, m: Any
) -> dict[str, Any]:
    script = mutant_script(workdir=workdir_of(package), reference_patch=task["patch"], mutant=m)
    out = cells.run(short, name, package, script, "MUTANT_APPLIED")
    if out["reward"] is not None:
        log = json.loads((OUT / "cells" / short / name / "result.json").read_text()).get(
            "agent_log", ""
        )
        if not mutant_applied(log, m):
            out["reward"] = None
    return out


# ------------------------------------------------------------------ synthesis


def make_prompt(
    task: dict, train: list, survivors: list, previous: str | None, feedback: str | None
) -> str:
    base = task["base_package"]
    instruction = (base / "instruction.md").read_text(errors="replace")
    hidden = R.patch_sections(
        (base / "tests/test.patch").read_text(errors="replace"),
        lambda p: not R.STRIP_PATCH_FILES.search(p),
    )
    reference = R.patch_sections(task["patch"], lambda p: p.endswith(".py"))
    existing = (
        (base / "tests/test_outputs.py").read_text(errors="replace")
        if (base / "tests/test_outputs.py").is_file()
        else None
    )
    return build_synthesis_prompt(
        instruction=instruction,
        hidden_tests=hidden,
        reference_patch=reference,
        train_mutants=train,
        train_diffs=[
            mutant_diff(m, task["sources"][m.path]) for m in train if m.path in task["sources"]
        ],
        interpreter_command=task["hook_command"],
        python_version=task["py"],
        existing_tests=existing,
        feedback=feedback,
        previous_test=previous,
        survivor_diffs=[mutant_diff(m, task["sources"][m.path]) for m in survivors] or None,
    )


def synthesize_once(short: str, key: str, prompt: str) -> dict[str, Any]:
    session_dir = OUT / "sessions" / short / key
    session_dir.mkdir(parents=True, exist_ok=True)
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
    served, cost = R.served_model(session_dir)
    code = extract_code_block(proc.stdout)
    valid, reason = validate_contract_test(code or "")
    status = "ok"
    if proc.returncode != 0 or not code:
        status = f"failed rc={proc.returncode} block={'yes' if code else 'no'}"
    elif served != MODEL:
        status = f"rejected: served {served}"
    elif not valid:
        status = f"refused: {reason}"
    return {
        "status": status,
        "served_model": served,
        "requested_model": MODEL,
        "cost_usd": round(cost, 4),
        "wall_s": round(time.monotonic() - started, 1),
        "prompt_sha256": sha(prompt),
        "prompt_bytes": len(prompt),
        "code": code if status == "ok" else None,
        "code_sha256": sha(code) if code and status == "ok" else None,
    }


class TaskRunner:
    def __init__(
        self, short: str, task: dict[str, Any], cells: Cells, base_rewards: dict[str, float | None]
    ) -> None:
        self.short, self.task, self.cells, self.base_rewards = short, task, cells, base_rewards
        self.key = ""
        self.base_key = ""
        self.round_keys: dict[int, str] = {}
        self.package: Path | None = None
        (OUT / "synth" / short).mkdir(parents=True, exist_ok=True)

    def build_prompt(
        self, index: int, train: list, survivors: list, previous: str | None, feedback: str | None
    ) -> str:
        self.base_key = f"r{index}" + ("f" if feedback else "")
        return make_prompt(self.task, train, survivors, previous, feedback)

    def generate(self, index: int, prompt: str) -> str | None:
        base_key = self.base_key
        meta_path = OUT / "synth" / self.short / f"{base_key}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if meta["prompt_sha256"] == sha(prompt) and meta.get("code"):
                self.key = f"{base_key}-{sha(meta['code'])[:8]}"
                return meta["code"]
        meta = {}
        for attempt in (1, 2):
            meta = synthesize_once(self.short, f"{base_key}-a{attempt}", prompt)
            meta["attempt"] = attempt
            if meta["status"] == "ok":
                break
        meta_path.write_text(json.dumps(meta, indent=1) + "\n")
        print(
            f"  {self.short} {base_key}: {meta['status']} model={meta['served_model']} {meta['wall_s']}s",
            flush=True,
        )
        self.key = f"{base_key}-{sha(meta['code'])[:8]}" if meta.get("code") else base_key
        return meta.get("code")

    def harden(self, key: str, code: str) -> Path:
        target = OUT / "packages" / self.short / key
        if not target.exists():
            stage_and_harden_package(
                self.task["base_package"],
                target,
                code,
                self.task["hook_command"],
                single_container=False,
                allow_degraded=True,
            )
        return target

    def grade(self, index: int, code: str, probes: list) -> dict[str, Any]:
        package = self.harden(self.key, code)
        self.package = package
        self.round_keys[index] = self.key
        wd = workdir_of(package)
        reference = self.cells.run(
            self.short,
            f"{self.key}/reference",
            package,
            reference_script(wd, self.task["patch"]),
            "REFERENCE_APPLIED",
        )["reward"]
        out: dict[str, Any] = {"reference": reference}
        if reference != 1.0:
            # probe and eval cells are only paid for a test that passes the reference
            log = OUT / "cells" / self.short / self.key / "reference/trial/verifier/test_output.log"
            if reference is not None and log.exists():
                out["reference_output"] = log.read_text(errors="replace")[-3500:]
            return out
        jobs: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for m in probes:
                jobs[f"probe:{m.sha256}"] = pool.submit(
                    mutant_cell,
                    self.cells,
                    self.task,
                    self.short,
                    f"{self.key}/probe-{mutant_tag(m)}",
                    package,
                    m,
                )
            for m in self.task["evals_all"]:
                jobs[f"eval:{m.sha256}"] = pool.submit(
                    mutant_cell,
                    self.cells,
                    self.task,
                    self.short,
                    f"{self.key}/eval-{mutant_tag(m)}",
                    package,
                    m,
                )
            out.update({label: future.result()["reward"] for label, future in jobs.items()})
        return out


def run_task(
    short: str, task: dict, cells: Cells, base_rewards: dict[str, float | None]
) -> dict[str, Any]:
    runner = TaskRunner(short, task, cells, base_rewards)
    outcome = run_rounds(
        rounds=ROUNDS,
        train=task["train_mutants"],
        probe_pool=list(task["pools"].probe_pool),
        evals=task["evals_all"],
        eval_diffs=[mutant_diff(m, task["sources"][m.path]) for m in task["evals_all"]],
        build_prompt=runner.build_prompt,
        generate=runner.generate,
        grade=runner.grade,
        probes_per_round=PROBES_PER_ROUND,
    )
    record = {
        "task": short,
        "stop_reason": outcome.stop_reason,
        "accepted_rounds": sorted(outcome.accepted_tests),
        "rounds": [{k: v for k, v in r.__dict__.items()} for r in outcome.records],
        "accepted_test_sha256": {str(k): sha(v) for k, v in outcome.accepted_tests.items()},
        "keys": {str(k): v for k, v in runner.round_keys.items()},
    }
    (OUT / "rounds").mkdir(parents=True, exist_ok=True)
    (OUT / "rounds" / f"{short}.json").write_text(json.dumps(record, indent=1) + "\n")
    for k, v in outcome.accepted_tests.items():
        (OUT / "rounds" / f"{short}-r{k}-test.py").write_text(v)
    print(f"{short}: stop={outcome.stop_reason} accepted={record['accepted_rounds']}", flush=True)
    return record


# ------------------------------------------------------------------ stages


def stage_base(cohort: dict, shorts: list[str], cells: Cells) -> None:
    def one(item: tuple[str, Any]) -> None:
        short, m = item
        task = cohort[short]
        out = mutant_cell(cells, task, short, f"base/eval-{mutant_tag(m)}", task["base_package"], m)
        print(f"base {short} {mutant_tag(m)} -> {out['reward']}", flush=True)

    items = [(s, m) for s in shorts for m in cohort[s]["evals_all"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, items))


def read_base(short: str, task: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for m in task["evals_all"]:
        p = OUT / "cells" / short / f"base/eval-{mutant_tag(m)}" / "result.json"
        out[m.sha256] = json.loads(p.read_text())["reward"] if p.exists() else None
    return out


def stage_fit(cohort: dict, shorts: list[str], cells: Cells) -> None:
    """POST-HOC (not in the prereg): does round r's test kill the probes that survived round r-1 and were shown to it?"""
    for short in shorts:
        rec_path = OUT / "rounds" / f"{short}.json"
        if not rec_path.exists():
            continue
        rec = json.loads(rec_path.read_text())
        task = cohort[short]
        pool = {m.sha256: m for m in task["pools"].probe_pool}
        for previous, current in zip(rec["rounds"], rec["rounds"][1:], strict=False):
            key = rec["keys"].get(str(current["round"]))
            if key is None or current["guard"] not in ("passed", "repaired"):
                continue
            package = OUT / "packages" / short / key
            for sha_ in previous["survivors"]:
                m = pool[sha_]
                out = mutant_cell(cells, task, short, f"{key}/fit-{mutant_tag(m)}", package, m)
                print(f"fit {short} {key} {mutant_tag(m)} -> {out['reward']}", flush=True)


def stage_gates(cohort: dict, shorts: list[str], cells: Cells) -> None:
    jobs = []
    for short in shorts:
        rec_path = OUT / "rounds" / f"{short}.json"
        if not rec_path.exists():
            continue
        rec = json.loads(rec_path.read_text())
        rounds = (
            sorted({rec["accepted_rounds"][0], rec["accepted_rounds"][-1]})
            if rec["accepted_rounds"]
            else []
        )
        task = cohort[short]
        for r in rounds:
            package = OUT / "packages" / short / f"r{r}"
            if not package.exists():
                package = OUT / "packages" / short / f"r{r}f"
            wd = workdir_of(package)
            jobs.append((short, f"gates-r{r}/nop", package, nop_script(wd), "NOP_APPLIED"))
            if task["witnesses"]:
                for solve in task["witnesses"]:
                    jobs.append(
                        (
                            short,
                            f"gates-r{r}/witness-{solve.parent.name}",
                            package,
                            witness_script(wd, solve.read_text()),
                            "WITNESS_APPLIED",
                        )
                    )
            else:
                jobs.append(
                    (
                        short,
                        f"gates-r{r}/witness-conftest",
                        package,
                        witness_script(wd, None),
                        "WITNESS_APPLIED conftest",
                    )
                )
            jobs.append(
                (short, f"gates-r{r}/exit0_pth", package, exit0_pth_script(wd), "EXIT0_PTH_APPLIED")
            )
    with ThreadPoolExecutor(max_workers=6) as pool:
        for (short, name, *_), out in zip(
            jobs, pool.map(lambda j: cells.run(*j), jobs), strict=True
        ):
            print(f"{short}/{name}: reward={out['reward']} applied={out['applied']}", flush=True)


def status() -> None:
    total = 0.0
    n = 0
    for p in (OUT / "cells").glob("*/**/result.json"):
        total += json.loads(p.read_text()).get("est_usd", 0.0)
        n += 1
    print(json.dumps({"cells": n, "est_usd": round(total, 4), "cap": CAP_USD}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "base", "rounds", "gates", "fit", "status"])
    parser.add_argument("--tasks", default="")
    args = parser.parse_args()
    if args.stage == "status":
        status()
        return
    cohort = load_tasks()
    if args.stage == "prepare":
        prepare(cohort)
        return
    shorts = [s for s in (args.tasks.split(",") if args.tasks else list(cohort)) if s]
    cells = Cells()
    if args.stage == "base":
        stage_base(cohort, shorts, cells)
    elif args.stage == "rounds":
        with ThreadPoolExecutor(max_workers=min(3, len(shorts))) as pool:
            list(pool.map(lambda s: run_task(s, cohort[s], cells, read_base(s, cohort[s])), shorts))
    elif args.stage == "gates":
        stage_gates(cohort, shorts, cells)
    elif args.stage == "fit":
        stage_fit(cohort, shorts, cells)
    print(
        json.dumps(
            {"meter_est_usd": round(cells.meter.spent, 4), "cells_this_process": cells.meter.cells}
        )
    )


if __name__ == "__main__":
    main()
