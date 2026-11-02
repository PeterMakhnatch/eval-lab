#!/usr/bin/env python3
"""A4B: Gold v1.1 with reference-filtered synthesis (exploratory re-design after A4's pre-registered 0/32).

Differences from A4 (research/experiments/on-gold-v1-20261102/run_gold_batch.py), each a fix for a defect the
diagnosis found (see README.md):
  * the contract test runs under the grader's own interpreter/environment and workdir (``envspec``), not ``python3``
    in ``/testbed`` (A4: ModuleNotFoundError for numpy/flask, wrong Python, /workspace/repo tasks never started);
  * the repair call really re-synthesizes and re-stages (A4 cached the first test under the same key and re-used
    the staged package, so "repair" re-graded the identical test);
  * the model is asked for 12 independent methods; methods failing on the reference are dropped, the rest kept;
  * train mutants are graded (A4 hard-coded ``train_killed = train_total``); base acceptance of every EVAL
    mutant is measured on the original grader (A4 assumed "accepted" when the mutant was not in the census);
  * exceptions are never mapped to a reference verdict: QA-script failures are ``harness_error_qa_script``.

Stages (resume-safe: model output, hardened packages and Modal cells are cached on disk by key):
  accept   reference-filtered round-1 acceptance for every task (gates nothing else)
  full     rounds 1-2 with grading, gates, train/probe/eval mutants, A2 survivors
  control  pipeline control: A3's accepted tests + A4's stored tests through this pipeline
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "research/experiments/on-gold-v1-20261102"))

import run_gold_batch as a4  # noqa: E402
from a4b_synth import (  # noqa: E402
    MIN_KEPT,
    build_filtered_prompt,
    drop_tests,
    failure_blocks,
    list_test_methods,
    parse_verdicts,
    prepare_code,
    repair_section,
)
from envspec import EnvSpec, derive_env_spec  # noqa: E402

from evallab.gold_certifier import (  # noqa: E402
    build_fresh_pools,
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
from evallab.modal_cells import (  # noqa: E402
    CellCancelledError,
    CellCapReachedError,
    CellError,
)
from evallab.mutant_auditor import (  # noqa: E402
    Mutant,
    mutant_script,
    mutants_for_patch,
    parse_probe,
    probe_script,
    python_targets,
)

APPROVAL = "Peter 2026-11-02 overnight A4B: Modal cap $1.50"
CAP_USD = 1.50
MODEL = "google-antigravity/gemini-3.8-flash:medium"
REQUESTED = ("google-antigravity", "gemini-3.8-flash")
WORKER_YML = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/worker.yml")
OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A4B")
A4_OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/gold-v1")
A3_ROUNDS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A3/rounds")
A2_TASKS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks")
LEDGER = Path.home() / ".local/state/evallab-failure-library/a4b-ledger.json"
A4_GENERIC_HOOK = 'cd "$CWD" && cd /testbed && PYTHONPATH=/testbed python3'
TEST_NAME = "test_gold_contract.py"


class HarnessError(RuntimeError):
    """A cell could not run at all (no reward was produced); never a verdict on the test."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


class TaskCtx:
    """Everything derived once per task."""

    def __init__(self, task: dict[str, Any], pkg: Path, spec: EnvSpec, patch: str, sources: dict[str, str]):
        self.task = task
        self.task_id: str = task["task_id"]
        self.pkg = pkg
        self.spec = spec
        self.patch = patch
        self.sources = sources
        self.train: list[Mutant] = []
        self.evals: list[Mutant] = []
        self.probe_pool: list[Mutant] = []
        self.a2_survivors: list[Mutant] = []
        self.synth: dict[int, dict[str, Any]] = {}  # round -> asked/kept/repair record


class A4BDriver(a4.GoldBatchDriver):
    def __init__(self, out: Path = OUT, cap_usd: float = CAP_USD, rounds: int = 2, **kw: Any):
        out.mkdir(parents=True, exist_ok=True)
        super().__init__(
            out_dir=out,
            approval=APPROVAL,
            cap_usd=cap_usd,
            spend_ledger=LEDGER,
            rounds=rounds,
            timeout_seconds=300,
            model=MODEL,
            **kw,
        )
        self.work_dir = out / "work"
        self.work_dir.mkdir(exist_ok=True)
        self.ctxs: dict[str, TaskCtx] = {}
        self.ctx_lock = threading.Lock()

    # ------------------------------------------------------------------ cells
    def cell(self, ctx_task: str, variant: str, package: Path, script: str) -> dict[str, Any]:
        try:
            res = self._run_cell_unit(ctx_task, variant, package, script)
        except CellCapReachedError:
            raise
        except CellCancelledError:
            raise
        except CellError as exc:
            msg = str(exc)
            reason = "harness_error_qa_script" if "QA script exited" in msg else "harness_error_cell"
            raise HarnessError(reason, msg[:300]) from exc
        return res

    def _run_cell_unit(self, task_id: str, variant: str, package: Path, script: str) -> dict[str, Any]:
        cell = a4.CellSpec(package=package, variant=variant, submission=script, task_id=task_id,
                           timeout_seconds=self.timeout_seconds)
        with a4.GLOBAL_MODAL_SLOTS:
            res = self.cell_runner.run_cell(cell)
        if not res.get("cache_hit") and res.get("est_usd", 0.0) > 0:
            rec = {"cost_usd": res["est_usd"], "job": f"a4b-gold:{task_id}/{variant}", "route": "modal",
                   "note": f"A4B cell status={res.get('status')} reward={res.get('reward')} wall_s={res.get('wall_s')}",
                   "ts": datetime.now(UTC).isoformat(timespec="seconds")}
            with a4.LOG_LOCK, self.spend_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        return res

    def cell_log(self, res: dict[str, Any]) -> str:
        p = self.cells_dir / res["cell_key"] / "trial/verifier/test_output.log"
        return p.read_text(errors="replace") if p.is_file() else res.get("test_output_tail", "")

    # ------------------------------------------------------------------ context
    def context(self, task: dict[str, Any]) -> TaskCtx:
        tid = task["task_id"]
        with self.ctx_lock:
            if tid in self.ctxs:
                return self.ctxs[tid]
        pkg = Path(task["package_path"])
        spec = derive_env_spec((pkg / "tests/test.patch").read_text(errors="replace"))
        patch_p = Path(task["reference_patch_path"])
        patch = (patch_p if patch_p.is_absolute() else REPO / patch_p).read_text(encoding="utf-8")
        sources: dict[str, str] = {}
        a2 = A2_TASKS / tid / "probe/result.json"
        if a2.is_file():
            sources = parse_probe(json.loads(a2.read_text()).get("agent_log", ""))
        if not sources:
            targets = sorted(python_targets(patch))
            if targets:
                res = self.cell(tid, "probe", pkg, probe_script(workdir=spec.workdir, reference_patch=patch, targets=targets))
                sources = parse_probe(res.get("agent_log", ""))
        ctx = TaskCtx(task, pkg, spec, patch, sources)
        if sources:
            mutants = mutants_for_patch(patch, sources, limit=8)
            ctx.train = mutants[:4]
            pools = build_fresh_pools(
                patch, sources, train=ctx.train, evals=mutants[4:], seed="gold-rounds-v1", eval_extra_limit=self.eval_extra
            )
            ctx.evals = [*mutants[4:], *pools.eval_extra]
            ctx.probe_pool = list(pools.probe_pool)
            a2_audit = A2_TASKS / tid / "audit.json"
            if a2_audit.is_file():
                shas = {
                    o["mutant"]["sha256"]
                    for o in json.loads(a2_audit.read_text()).get("original", [])
                    if o.get("reward") == 1.0 and o.get("applied") and o.get("mutant")
                }
                ctx.a2_survivors = [m for m in mutants_for_patch(patch, sources) if m.sha256 in shas]
        with self.ctx_lock:
            self.ctxs[tid] = ctx
        return ctx

    # ------------------------------------------------------------------ model
    def synthesize(self, tid: str, key: str, prompt: str) -> str | None:
        """One model call (plus one retry when the process fails or returns no valid code block). Cached by key."""
        session_dir = self.sessions_dir / tid / key
        meta = session_dir / "meta.json"
        if meta.is_file():
            code = json.loads(meta.read_text()).get("code")
            if code:
                return code
        session_dir.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            with a4.GLOBAL_MODEL_SLOTS:
                proc = subprocess.run(
                    ["omp", "-p", "--model", MODEL, "--config", str(WORKER_YML), "--no-tools", "--no-skills",
                     "--session-dir", str(session_dir), prompt],
                    capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=900, cwd="/tmp",
                    env={**os.environ, "OMP_NO_COLOR": "1"},
                )
            code = extract_code_block(proc.stdout)
            if proc.returncode == 0 and code and validate_contract_test(code)[0]:
                meta.write_text(json.dumps({"status": "ok", "attempt": attempt, "code": code,
                                            "ts": datetime.now(UTC).isoformat()}, indent=2))
                return code
        return None

    # ------------------------------------------------------------------ filtered acceptance
    def stage(self, ctx: TaskCtx, code: str, name: str) -> Path:
        d = self.packages_dir / ctx.task_id / name
        stage_and_harden_package(
            ctx.pkg, d, code, ctx.spec.hook_command, single_container=True, allow_degraded=True
        )
        return d

    def run_ref(self, ctx: TaskCtx, code: str, name: str, variant: str) -> tuple[dict[str, Any], str, Path]:
        d = self.stage(ctx, code, name)
        res = self.cell(ctx.task_id, variant, d, reference_script(ctx.spec.workdir, ctx.patch))
        return res, self.cell_log(res), d

    def filtered(self, ctx: TaskCtx, r: int, prompt: str) -> str | None:
        """Reference-filtered synthesis for round ``r``; returns the confirmed kept file or None."""
        rec: dict[str, Any] = {"asked": 0, "kept": 0, "repair_used": False, "status": "synthesis_failed"}
        ctx.synth[r] = rec
        tid = ctx.task_id
        code = self.synthesize(tid, f"r{r}a1", prompt)
        if code is None:
            return None
        attempts: list[tuple[str, list[str], dict[str, str], str]] = []
        for n in (1, 2):
            code = prepare_code(code)
            try:
                names = [m[0] for m in list_test_methods(code)]
            except SyntaxError:
                rec["status"] = f"unparsable_attempt{n}"
                names = []
            if not names:
                rec.setdefault("asked_by_attempt", []).append(0)
                log = ""
                verdicts: dict[str, str] = {}
            else:
                res, log, _ = self.run_ref(ctx, code, f"r{r}-a{n}", f"filter/r{r}a{n}")
                verdicts = parse_verdicts(log)
                if not verdicts and "/tests/test_gold_contract.py" not in log:
                    # the grader never reached the contract test: the hidden tests failed on the reference
                    rec.update(status="base_tests_fail_reference", asked=len(names), kept=0)
                    return None
                rec.setdefault("asked_by_attempt", []).append(len(names))
            kept = list(names) if names and res.get("reward") == 1.0 else [x for x in names if verdicts.get(x) == "ok"]
            rec.setdefault("kept_by_attempt", []).append(len(kept))
            attempts.append((code, kept, verdicts, log))
            if len(kept) >= MIN_KEPT or n == 2:
                break
            rec["repair_used"] = True
            failing = {x for x in names if verdicts.get(x) != "ok"}
            msgs = failure_blocks(log, failing) or log[-1500:]
            repaired = self.synthesize(tid, f"r{r}a2", prompt + repair_section(code, msgs))
            if repaired is None:
                rec["status"] = "repair_synthesis_failed"
                return None
            code = repaired
        best = attempts[-1]
        if len(best[1]) < MIN_KEPT:
            rec.update(status="fewer_than_3_kept", asked=len(list_test_methods(best[0])) if best[0] else 0,
                       kept=len(best[1]))
            if not best[2]:
                rec["status"] = "harness_error_contract_import"
            return None
        final_code, kept_names = best[0], best[1]
        names = [m[0] for m in list_test_methods(final_code)]
        final = drop_tests(final_code, set(names) - set(kept_names)) if len(kept_names) < len(names) else final_code
        res, _log, _d = self.run_ref(ctx, final, f"r{r}", f"r{r}/reference")
        rec.update(asked=len(names), kept=len(kept_names), confirm_reward=res.get("reward"))
        if res.get("reward") != 1.0:
            rec["status"] = "confirm_failed"
            return None
        rec["status"] = "accepted"
        return final

    # ------------------------------------------------------------------ prompts
    def prompt_builder(self, ctx: TaskCtx):
        pkg = ctx.pkg
        instruction = (pkg / "instruction.md").read_text(errors="replace") if (pkg / "instruction.md").is_file() else ""
        hp = pkg / "tests/test.patch"
        hidden = a4.patch_sections(hp.read_text(errors="replace"), lambda p: not a4.STRIP_PATCH_FILES.search(p)) if hp.is_file() else ""
        ref = a4.patch_sections(ctx.patch, lambda p: p.endswith(".py"))

        def build(r: int, tr: list[Mutant], surv: list[Mutant], prev: str | None, fb: str | None) -> str:
            return build_filtered_prompt(
                instruction=instruction,
                hidden_tests=hidden,
                reference_patch=ref,
                train_diffs=[mutant_diff(m, ctx.sources.get(m.path, "")) for m in tr if m.path in ctx.sources],
                survivor_diffs=[mutant_diff(m, ctx.sources.get(m.path, "")) for m in surv if m.path in ctx.sources] or None,
                interpreter_command=ctx.spec.hook_command,
                python_version=ctx.spec.python_version,
                test_filename=TEST_NAME,
                previous_test=prev,
            )

        return build

    # ------------------------------------------------------------------ stage A
    def accept(self, task: dict[str, Any]) -> dict[str, Any]:
        tid = task["task_id"]
        path = self.tasks_dir / tid / "accept.json"
        if path.is_file():
            return json.loads(path.read_text())
        out: dict[str, Any] = {"task_id": tid, "priority_rank": task["priority_rank"], "is_weak": task["is_weak"]}
        try:
            ctx = self.context(task)
            out["env"] = ctx.spec.summary()
            if not ctx.train:
                out.update(accepted=False, reason="no_mutants_or_sources")
            else:
                prompt = self.prompt_builder(ctx)(1, ctx.train, [], None, None)
                code = self.filtered(ctx, 1, prompt)
                rec = ctx.synth[1]
                out.update(accepted=code is not None, reason=rec["status"], asked=rec["asked"], kept=rec["kept"],
                           repair_used=rec["repair_used"], by_attempt={k: rec.get(k) for k in ("asked_by_attempt", "kept_by_attempt")})
        except HarnessError as exc:
            out.update(accepted=False, reason=exc.reason, detail=str(exc)[:300])
        except CellCapReachedError:
            return {**out, "accepted": None, "reason": "spend_cap_exhausted"}  # not persisted: resumable
        (self.tasks_dir / tid).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2) + "\n")
        return out

    # ------------------------------------------------------------------ stage B
    def full(self, task: dict[str, Any]) -> dict[str, Any]:
        tid = task["task_id"]
        path = self.tasks_dir / tid / f"a4b-r{self.rounds}.json"
        if path.is_file():
            return json.loads(path.read_text())
        acc = self.accept(task)
        base = {"task_id": tid, "priority_rank": task["priority_rank"], "is_weak": task["is_weak"], "env": acc.get("env")}
        if acc.get("accepted") is None:
            return {**base, "status": "not_run", "reason": "spend_cap_exhausted"}
        if not acc["accepted"]:
            res = {**base, "status": "not_gold", "reason": acc["reason"], "accepted_test": False,
                   "functions": {"asked": acc.get("asked", 0), "kept": acc.get("kept", 0)}}
            if acc["reason"].startswith("harness_error") or acc["reason"] in ("no_mutants_or_sources",):
                res["status"] = "not_run"
            path.write_text(json.dumps(res, indent=2) + "\n")
            return res
        try:
            res = self._full_accepted(task, base)
        except CellCapReachedError:
            return {**base, "status": "not_run", "reason": "spend_cap_exhausted", "accepted_test": True}
        except HarnessError as exc:
            res = {**base, "status": "not_run", "reason": exc.reason, "accepted_test": True, "detail": str(exc)[:300]}
        path.write_text(json.dumps(res, indent=2) + "\n")
        return res

    def _kill(self, grades: dict[str, Any], key: str) -> bool | None:
        r = grades.get(key)
        return None if r is None else r == 0.0

    def _full_accepted(self, task: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        tid = task["task_id"]
        ctx = self.context(task)
        wd, patch = ctx.spec.workdir, ctx.patch
        build = self.prompt_builder(ctx)
        counters = {"cells": 0, "usd": 0.0}

        def run(variant: str, d: Path, script: str) -> dict[str, Any]:
            res = self.cell(tid, variant, d, script)
            counters["cells"] += 1
            counters["usd"] += res.get("est_usd", 0.0) if not res.get("cache_hit") else 0.0
            return res

        def generate(r: int, prompt: str) -> str | None:
            return self.filtered(ctx, r, prompt)

        def grade(r: int, code: str, probes: list[Mutant]) -> dict[str, Any]:
            d = self.packages_dir / tid / f"r{r}"
            if not d.exists():
                self.stage(ctx, code, f"r{r}")
            grades: dict[str, Any] = {"reference": run(f"r{r}/reference", d, reference_script(wd, patch)).get("reward")}
            if grades["reference"] != 1.0:
                return grades
            for tag, group in (("probe", probes), ("eval", ctx.evals)):
                for m in group:
                    grades[f"{tag}:{m.sha256}"] = run(f"r{r}/{tag}-{m.sha256[:8]}", d, mutant_script(workdir=wd, reference_patch=patch, mutant=m)).get("reward")
            return grades

        outcome = run_rounds(
            rounds=self.rounds, train=ctx.train, probe_pool=ctx.probe_pool, evals=ctx.evals,
            eval_diffs=[mutant_diff(m, ctx.sources[m.path]) for m in ctx.evals if m.path in ctx.sources],
            build_prompt=build, generate=generate, grade=grade, probes_per_round=self.probes_per_round,
        )
        arm1 = outcome.final_round(1)
        arm2 = outcome.final_round(2)
        if arm1 is None or arm2 is None:  # accept() said round 1 was accepted; a cache miss changed the outcome
            return {**base, "status": "not_run", "reason": f"round1_not_reproduced:{outcome.stop_reason}", "accepted_test": True}
        rec_by_round = {r.round: r for r in outcome.records}
        best = self.packages_dir / tid / f"r{arm2}"
        nop = run("gates/nop", best, nop_script(wd)).get("reward")
        wit = run("gates/witness", best, witness_script(wd, None)).get("reward")
        ex0 = run("gates/exit0_pth", best, exit0_pth_script(wd)).get("reward")
        gates = {"reference_passed": rec_by_round[arm2].grades.get("reference") == 1.0, "nop_passed": nop == 0.0,
                 "witness_passed": wit == 0.0, "exit0_passed": ex0 == 0.0}

        # train (+ earlier survivors shown to the shipped round) are graded on the shipped package
        by_sha = {m.sha256: m for m in [*ctx.train, *ctx.probe_pool]}
        shown = [by_sha[s] for s in rec_by_round[arm2].train_shas if s in by_sha]
        train_rewards = {m.sha256: run(f"final/train-{m.sha256[:8]}", best, mutant_script(workdir=wd, reference_patch=patch, mutant=m)).get("reward") for m in shown}
        probe_killed = len(rec_by_round[arm2].killed)
        probe_total = len(rec_by_round[arm2].probe_shas)

        # base acceptance of each EVAL mutant, measured on the original grader
        base_accept: dict[str, bool | None] = {}
        for e in ctx.evals:
            rew = run(f"base/eval-{e.sha256[:8]}", ctx.pkg, mutant_script(workdir=wd, reference_patch=patch, mutant=e)).get("reward")
            base_accept[e.sha256] = None if rew is None else rew == 1.0

        def kills(r: int) -> dict[str, bool | None]:
            g = rec_by_round[r].grades
            return {e.sha256: self._kill(g, f"eval:{e.sha256}") for e in ctx.evals}

        k1, k2 = kills(arm1), kills(arm2)
        accepted_base = [e.sha256 for e in ctx.evals if base_accept[e.sha256]]
        pairs = [{"sha8": s[:8], "arm1_killed": k1[s], "arm2_killed": k2[s]} for s in accepted_base if k1[s] is not None and k2[s] is not None]

        # A2 survivors on the shipped package
        a2 = []
        for m in ctx.a2_survivors:
            rew = run(f"final/a2-{m.sha256[:8]}", best, mutant_script(workdir=wd, reference_patch=patch, mutant=m)).get("reward")
            a2.append({"sha8": m.sha256[:8], "killed": None if rew is None else rew == 0.0})

        train_total = len(shown)
        train_killed = sum(1 for v in train_rewards.values() if v == 0.0)
        structural = all(gates.values()) and probe_killed == probe_total and train_killed == train_total
        n_acc = len(accepted_base)
        kb = sum(1 for s in accepted_base if k2[s])
        status, reason = "not_gold", "ok"
        if not gates["nop_passed"]:
            reason = "gate_failed_nop"
        elif not gates["witness_passed"]:
            reason = "gate_failed_witness"
        elif not gates["exit0_passed"]:
            reason = "gate_failed_exit0"
        elif train_killed != train_total:
            reason = "train_mutants_survived"
        elif probe_killed != probe_total:
            reason = "probe_mutants_survived"
        elif n_acc < 3:
            status, reason = "gold_structural", "heldout_insufficient_eval"
        elif kb / n_acc >= 0.75:
            status = "gold_heldout"
        else:
            status, reason = "gold_structural", "heldout_kill_rate_low"
        if status != "not_gold" and not structural:
            status, reason = "not_gold", "structural_check_failed"
        test_file = self.packages_dir / tid / f"r{arm2}" / "tests" / TEST_NAME
        return {
            **base, "status": status, "reason": reason, "accepted_test": True, "stop_reason": outcome.stop_reason,
            "rounds_executed": len(outcome.records), "arm1_round": arm1, "arm2_round": arm2,
            "functions": {f"round{r}": {k: v[k] for k in ("asked", "kept", "repair_used") if k in v} for r, v in ctx.synth.items()},
            "counts": {"train_total": train_total, "train_killed": train_killed, "probe_total": probe_total,
                       "probe_killed": probe_killed, "eval_total": len(ctx.evals),
                       "eval_base_accepted": n_acc, "eval_base_accepted_killed_arm2": kb,
                       "eval_base_accepted_killed_arm1": sum(1 for s in accepted_base if k1[s])},
            "paired_eval_base_accepted": pairs, "gates": gates,
            "a2_survivors": a2,
            "cells_executed": counters["cells"], "cost_usd_new_cells": round(counters["usd"], 6),
            "upgraded_package_sha256": a4.sha256_text(test_file.read_text(encoding="utf-8")) if test_file.is_file() else None,
            "package_dir": str(self.packages_dir / tid / f"r{arm2}"),
        }

    # ------------------------------------------------------------------ batch
    def batch(self, stage: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fn = self.accept if stage == "accept" else self.full
        out: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.max_task_concurrency) as pool:
            futs = {pool.submit(fn, t): t for t in tasks}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    r = fut.result()
                except (KeyboardInterrupt, SystemExit, CellCancelledError):
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:  # a driver defect, recorded as such and never as a verdict
                    r = {"task_id": t["task_id"], "status": "not_run", "reason": "driver_exception", "detail": repr(exc)[:300]}
                    print("DRIVER_EXCEPTION", t["task_id"], repr(exc), flush=True)
                out.append(r)
                print(f"[{stage}] {r['task_id']} {r.get('status', r.get('accepted'))} {r.get('reason')} "
                      f"settled=${self.spend_gate.settled_usd:.4f}", flush=True)
        return out

    # ------------------------------------------------------------------ control
    def control(self) -> dict[str, Any]:
        """Pipeline control: A3-accepted tests and A4's stored tests through this pipeline, A4 hook vs fixed hook."""
        tasks = {t["task_id"]: t for t in a4.load_universe_tasks()}
        rows: list[dict[str, Any]] = []

        def one(label: str, tid: str, code: str, hook: str, workdir: str) -> dict[str, Any]:
            task = tasks[tid]
            ctx = self.context(task)
            name = f"control-{label}"
            d = self.packages_dir / tid / name
            stage_and_harden_package(ctx.pkg, d, prepare_code(code), hook, single_container=True, allow_degraded=True)
            try:
                res = self.cell(tid, f"control/{label}", d, reference_script(workdir, ctx.patch))
                log = self.cell_log(res)
                return {"task": tid, "arm": label, "reward": res.get("reward"), "usd": res.get("est_usd"),
                        "contract_ran": "/tests/test_gold_contract.py" in log or bool(parse_verdicts(log)),
                        "tail": log[-300:]}
            except HarnessError as exc:
                return {"task": tid, "arm": label, "reward": None, "harness_error": exc.reason, "detail": str(exc)[:200]}

        jobs: list[tuple[str, str, str, str, str]] = []
        for short, fname in (("000079", "000079-r1-test.py"), ("000079", "000079-r2-test.py"),
                             ("000232", "000232-r1-test.py"), ("001267", "001267-r1-test.py")):
            tid = f"format-code-task-{short}"
            if tid not in tasks:
                continue
            code = (A3_ROUNDS / fname).read_text()
            spec = self.context(tasks[tid]).spec
            tag = fname.replace("-test.py", "")
            jobs.append((f"a3-{tag}-a4hook", tid, code, A4_GENERIC_HOOK, "/testbed"))
            jobs.append((f"a3-{tag}-fixedhook", tid, code, spec.hook_command, spec.workdir))
        for tid, task in tasks.items():
            stored = A4_OUT / "packages" / tid / "r1" / "tests" / TEST_NAME
            if stored.is_file():
                spec = self.context(task).spec
                jobs.append(("a4stored-fixedhook", tid, stored.read_text(), spec.hook_command, spec.workdir))
        with ThreadPoolExecutor(max_workers=self.max_task_concurrency) as pool:
            for fut in as_completed([pool.submit(one, *j) for j in jobs]):
                with contextlib.suppress(Exception):
                    rows.append(fut.result())
                    print("[control]", {k: rows[-1].get(k) for k in ("task", "arm", "reward", "harness_error")}, flush=True)
        (self.work_dir / "control.json").write_text(json.dumps(sorted(rows, key=lambda r: (r["arm"], r["task"])), indent=2))
        return {"rows": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["control", "accept", "full"])
    ap.add_argument("--tasks", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--cap-usd", type=float, default=CAP_USD)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    d = A4BDriver(out=args.out, cap_usd=args.cap_usd, rounds=args.rounds, max_task_concurrency=args.concurrency)
    tasks = a4.load_universe_tasks()
    if args.tasks:
        want = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["task_id"] in want or t["task_id"][-6:] in want]
    if args.limit:
        tasks = tasks[: args.limit]
    if args.stage == "control":
        d.control()
    else:
        d.batch(args.stage, tasks)
    print(f"settled=${d.spend_gate.settled_usd:.4f} cap=${d.cap_usd:.2f}")


if __name__ == "__main__":
    main()
