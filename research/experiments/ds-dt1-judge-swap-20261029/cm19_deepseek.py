#!/usr/bin/env python3
"""DT-1: CM-13's frozen facet-cited windows, re-judged unchanged by DeepSeek-V4.1-Flash.

A copy of CM-19's ``cm19_opus.py`` with three changes and nothing else:

1. the route is ``deepseek/deepseek-flash`` (a paid BYOK route, so the Anthropic/Codex quota gate is
   replaced by a dollar budget gate);
2. the call mechanism drops the subscription-env scrub and refuses the subscription-only route check
   (the pinned transport is otherwise identical: same ``omp -p --mode json`` flags, same one-assistant-turn,
   served-model attestation, fallback and tool-call checks);
3. cost accounting reads ``usage.cost.total`` from each call's own usage record and writes real USD,
   instead of $0 for a subscription.

Prompts, plan, windows and the scorer are untouched. ``prepare`` copies the CM-13 plan and prompts into a
private state directory and re-checks the CM-13 sha256 pins.

    cm19_deepseek.py prepare
    cm19_deepseek.py run --jobs 24 [--limit N] [--replicate K] [--cap 2.70]
    cm19_deepseek.py score [--replicate K]

Preregistration: research-context bridge 2026-10-29-ds-squad/raw/DT-1/prereg.md.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
CM13_TOOL = REPO / "research/evidence/runs/cm13-facet-windows-20261028/tools/facet_windows.py"
spec = importlib.util.spec_from_file_location("facet_windows", CM13_TOOL)
assert spec and spec.loader
fw = importlib.util.module_from_spec(spec)
sys.modules["facet_windows"] = fw
spec.loader.exec_module(fw)

CM13_STATE = fw.STATE
CM13_RESULTS = fw.RESULTS
STATE = CM13_STATE.parent / "cm20-deepseek"
RESULTS = HERE / "results"
MODEL = "deepseek/deepseek-flash"
CONFIG = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/worker.yml"
)
CAP_USD = 2.70
OVERLAY_EXTRA = (
    'memory:\n  backend: "off"\n'
    "retry:\n  enabled: false\n  modelFallback: false\n  fallbackChains: {}\n"
)
SPEND = STATE / "spend.jsonl"


def cost_of(stdout: str) -> tuple[dict[str, Any], float]:
    """Tokens and USD from every assistant record of the agent_end message list."""
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    usd = 0.0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "agent_end":
            continue
        for message in event.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            for field, key in (
                ("input", "input_tokens"),
                ("output", "output_tokens"),
                ("cacheRead", "cache_read_tokens"),
                ("cacheWrite", "cache_write_tokens"),
            ):
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    tokens[key] += value
            total = (usage.get("cost") or {}).get("total")
            if isinstance(total, int | float) and total >= 0:
                usd += float(total)
    return tokens, usd


class BudgetGate:
    """Stop issuing calls once the run's own ledger reaches the dollar cap."""

    def __init__(self, cap: float = CAP_USD) -> None:
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.cap = cap
        self.spent = ledger_total()
        self.observations: list[dict[str, Any]] = []

    def add(self, usd: float) -> None:
        with self.lock:
            self.spent += usd

    def check(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                raise fw.CallError("budget_gate_stopped", quota=True)
            if self.spent >= self.cap:
                self.stopped.set()
                self.observations.append({"at": fw.now(), "spent_usd": round(self.spent, 4)})
                raise fw.CallError("budget_cap_reached", quota=True)


def ledger_total() -> float:
    if not SPEND.exists():
        return 0.0
    usd = 0.0
    for line in SPEND.read_text().splitlines():
        if line.strip():
            usd += float(json.loads(line).get("usd") or 0.0)
    return usd


def call_model(messages, gate: BudgetGate) -> dict[str, Any]:
    """The pinned transport, on a paid route: no subscription-only check, real cost per attempt."""
    system, user = messages[0]["content"], messages[-1]["content"]
    errors: list[str] = []
    spent = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    cost = 0.0
    for attempt in range(1, fw.ATTEMPTS + 1):
        try:
            gate.check()
            with tempfile.TemporaryDirectory(prefix="ds-dt1-") as work:
                overlay = Path(work) / "overlay.yml"
                overlay.write_text(CONFIG.read_text() + OVERLAY_EXTRA)
                proc = subprocess.run(
                    [
                        "omp",
                        "-p",
                        "--mode",
                        "json",
                        "--no-session",
                        "--no-tools",
                        "--no-lsp",
                        "--no-extensions",
                        "--no-skills",
                        "--no-rules",
                        "--no-title",
                        "--no-pty",
                        "--config",
                        str(overlay),
                        "--model",
                        MODEL,
                        "--system-prompt",
                        system,
                        "--thinking",
                        fw.THINKING,
                    ],
                    input=user,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=work,
                )  # fmt: skip
            tokens, usd = cost_of(proc.stdout)
            for field, value in tokens.items():
                spent[field] += value
            cost += usd
            gate.add(usd)
            try:
                answer, final = fw.parse_stream(proc.stdout)
            except fw.CallError as error:
                if str(error) == "missing_agent_end" and proc.returncode:
                    quota = bool(fw.QUOTA.search(proc.stderr))
                    raise fw.CallError(
                        "omp_nonzero_exit",
                        retryable=bool(fw.TRANSPORT.search(proc.stderr)) and not quota,
                        quota=quota,
                    ) from None
                raise
            if proc.returncode:
                raise fw.CallError("omp_nonzero_exit")
            return {
                "ok": True,
                "answer": answer,
                "attempts": attempt,
                "errors": errors,
                **spent,
                "cost_usd": round(cost, 8),
                "final_input_tokens": final["input_tokens"],
                "final_output_tokens": final["output_tokens"],
            }
        except subprocess.TimeoutExpired:
            error = fw.CallError("transport_timeout", retryable=True)
        except OSError:
            error = fw.CallError("transport_os_error")
        except fw.CallError as exc:
            error = exc
        errors.append(str(error))
        if error.quota:
            gate.stopped.set()
        if not error.retryable or attempt == fw.ATTEMPTS:
            return {
                "ok": False,
                "attempts": attempt,
                "errors": errors,
                "quota": error.quota,
                **spent,
                "cost_usd": round(cost, 8),
            }
        time.sleep(4 * attempt)
    raise AssertionError("unreachable")


def run(limit: int | None, jobs: int, replicate: int = 1) -> dict[str, Any]:
    """``fw.run`` with per-call USD and a budget gate; prompts are hash-checked before any call."""
    suffix = f"#r{replicate}" if replicate > 1 else ""
    freeze = json.loads((RESULTS / "freeze.json").read_text())
    if (
        fw.sha((STATE / "plan.json").read_bytes()) != freeze["plan_sha256"]
        or fw.sha((STATE / "prompts.json").read_bytes()) != freeze["prompts_sha256"]
    ):
        raise SystemExit("plan or prompts changed after freeze; refuse to judge")
    prompts: dict[str, list[dict[str, str]]] = json.loads((STATE / "prompts.json").read_text())
    cache = fw.load_cache()
    pending = [k for k in prompts if not cache.get(k + suffix, {}).get("ok")]
    if limit is not None:
        pending = pending[:limit]
    gate = BudgetGate()
    gate.check()
    lock = threading.Lock()
    started = fw.now()
    sink = (STATE / "judge-cache.jsonl").open("a")
    done = {"ok": 0, "failed": 0}

    def work(key: str) -> None:
        try:
            result = call_model(prompts[key], gate)
        except fw.CallError as error:
            result = {
                "ok": False,
                "attempts": 0,
                "errors": [str(error)],
                "quota": error.quota,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": 0.0,
            }
        row = {"key": key + suffix, "at": fw.now(), "model": MODEL, **result}
        with lock:
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            done["ok" if row["ok"] else "failed"] += 1
            cache[key + suffix] = row

    try:
        with fw.ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(work, pending))
    finally:
        sink.close()
        fresh = [cache[k + suffix] for k in pending if k + suffix in cache]
        entry = {
            "at": fw.now(),
            "tool": "ds-dt1-judge-swap",
            "route": "omp -p --mode json (paid BYOK, deepseek)",
            "model": MODEL,
            "replicate": replicate,
            "calls": len(fresh),
            "successes": sum(bool(r["ok"]) for r in fresh),
            "input_tokens": sum(r.get("input_tokens") or 0 for r in fresh),
            "output_tokens": sum(r.get("output_tokens") or 0 for r in fresh),
            "cache_read_tokens": sum(r.get("cache_read_tokens") or 0 for r in fresh),
            "cache_write_tokens": sum(r.get("cache_write_tokens") or 0 for r in fresh),
            "usd": round(sum(r.get("cost_usd") or 0.0 for r in fresh), 6),
            "basis": {
                "usd": "sum of usage.cost.total over every assistant record of every attempt",
                "started": started,
                "cap_usd": gate.cap,
                "gate_observations": gate.observations[-3:],
            },
        }
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        with SPEND.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
    return {
        "replicate": replicate,
        "pending": len(pending),
        **done,
        "usd": entry["usd"],
        "budget_stopped": gate.stopped.is_set(),
    }


def configure() -> None:
    fw.MODEL = MODEL
    fw.STATE = STATE
    fw.RESULTS = RESULTS
    fw.call_model = call_model


def prepare() -> dict[str, Any]:
    freeze = json.loads((CM13_RESULTS / "freeze.json").read_text())
    STATE.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    for name in ("plan.json", "prompts.json"):
        target = STATE / name
        if not target.exists():
            shutil.copyfile(CM13_STATE / name, target)
    ok = (
        fw.sha((STATE / "plan.json").read_bytes()) == freeze["plan_sha256"]
        and fw.sha((STATE / "prompts.json").read_bytes()) == freeze["prompts_sha256"]
    )
    if not ok:
        raise SystemExit("copied plan or prompts differ from the CM-13 freeze")
    shutil.copyfile(CM13_RESULTS / "freeze.json", RESULTS / "freeze.json")
    prompts = json.loads((STATE / "prompts.json").read_text())
    return {
        "plan_sha256": freeze["plan_sha256"],
        "prompts_sha256": freeze["prompts_sha256"],
        "unique_prompts": len(prompts),
        "judge": MODEL,
        "thinking": fw.THINKING,
        "state": str(STATE),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    runner = sub.add_parser("run")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--jobs", type=int, default=24)
    runner.add_argument("--replicate", type=int, default=1)
    scorer = sub.add_parser("score")
    scorer.add_argument("--replicate", type=int, default=1)
    args = parser.parse_args()
    configure()
    if args.command == "prepare":
        print(json.dumps(prepare(), indent=1))
    elif args.command == "run":
        print(json.dumps(run(args.limit, args.jobs, args.replicate), indent=1))
    else:
        fw.SUFFIX = f"#r{args.replicate}" if args.replicate > 1 else ""
        try:
            result = fw.score()
        finally:
            fw.SUFFIX = ""
        name = "result-single.json" if args.replicate == 1 else f"result-r{args.replicate}.json"
        fw.write_json(RESULTS / name, result)
        print(
            json.dumps(
                {
                    "arms": {
                        arm: {
                            "operating_point": v["operating_point"],
                            "localization": v["localization_on_80_enacted"],
                            "cost": v["cost"],
                        }
                        for arm, v in result["arms"].items()
                    },
                    "replicate": args.replicate,
                },
                indent=1,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    threading.current_thread().name = "ds-dt1"
    raise SystemExit(main())
