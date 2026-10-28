#!/usr/bin/env python3
"""CM-19: CM-13's frozen facet-cited windows, re-judged unchanged by Opus 5.5.

The CM-13 plan and its 418 unique prompts are copied byte-for-byte into a private cm19 state directory (the
sha256 pins in the CM-13 freeze are re-checked), the judge is swapped, the Anthropic quota gate replaces the
Codex one, and the CM-13 scorer runs unchanged on the new judge cache. Prompts and replies stay outside git.

    cm19_opus.py prepare            # copy plan/prompts, verify the CM-13 hashes
    cm19_opus.py run --jobs 6 [--limit N]
    cm19_opus.py score              # writes results/result.json (CM-13's aggregate on the Opus cache)

Preregistration: research-context causal-prefix-monitor.md, CM-19 (commit ed33906).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "src"))
CM13_TOOL = REPO / "research/evidence/runs/cm13-facet-windows-20261028/tools/facet_windows.py"
spec = importlib.util.spec_from_file_location("facet_windows", CM13_TOOL)
assert spec and spec.loader
fw = importlib.util.module_from_spec(spec)
sys.modules["facet_windows"] = fw
spec.loader.exec_module(fw)

CM13_STATE = fw.STATE
CM13_RESULTS = fw.RESULTS
STATE = CM13_STATE.parent / "cm19"
RESULTS = HERE.parent / "results"
OPUS = "anthropic/claude-opus-5-5"
ANTHROPIC_STOP = 0.85


class AnthropicGate(fw.QuotaGate):
    """Stop at 85% of any Anthropic window; an empty or unreadable report stops the run."""

    def check(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                raise fw.CallError("quota_gate_stopped", quota=True)
            if time.monotonic() - self.checked < 60:
                return
            try:
                windows: list[dict[str, Any]] = []
                for _ in range(4):
                    proc = subprocess.run(
                        ["omp", "usage", "--json", "--provider", "anthropic"],
                        capture_output=True, text=True, timeout=90,
                        env=fw.subscription_env(), check=True,
                    )  # fmt: skip
                    for report in json.loads(proc.stdout).get("reports", []):
                        if report.get("provider") != "anthropic":
                            continue
                        for limit in report.get("limits", []):
                            amount = limit.get("amount", {})
                            fraction = amount.get("usedFraction")
                            if not isinstance(fraction, int | float) or not 0 <= fraction <= 1:
                                raise fw.CallError("quota_window_unavailable", quota=True)
                            windows.append(
                                {"window": limit.get("window", {}).get("id"), "used": fraction}
                            )
                            if fraction >= ANTHROPIC_STOP or limit.get("status") == "exhausted":
                                raise fw.CallError("quota_85_percent_stop", quota=True)
                    if windows:
                        break
                    time.sleep(5)
                if not windows:
                    raise fw.CallError("quota_windows_missing", quota=True)
                self.observations.append({"at": fw.now(), "windows": windows})
                self.checked = time.monotonic()
            except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
                self.stopped.set()
                raise fw.CallError("quota_probe_failed", quota=True) from exc
            except fw.CallError:
                self.stopped.set()
                raise


def usage_with_cache(stdout: str) -> dict[str, int]:
    """Route-reported prompt tokens: uncached input plus cache reads and writes (Anthropic reports them apart)."""
    tokens = {"input_tokens": 0, "output_tokens": 0}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "agent_end":
            for message in event.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    usage = message.get("usage") or {}
                    for field in ("input", "cacheRead", "cacheWrite"):
                        value = usage.get(field)
                        if type(value) is int and value >= 0:
                            tokens["input_tokens"] += value
                    value = usage.get("output")
                    if type(value) is int and value >= 0:
                        tokens["output_tokens"] += value
    return tokens


def configure() -> None:
    fw.MODEL = OPUS
    fw.usage_of = usage_with_cache
    fw.QuotaGate = AnthropicGate
    fw.STATE = STATE
    fw.RESULTS = RESULTS


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
        "judge": OPUS,
        "thinking": fw.THINKING,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    runner = sub.add_parser("run")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--jobs", type=int, default=6)
    sub.add_parser("score")
    args = parser.parse_args()
    configure()
    if args.command == "prepare":
        print(json.dumps(prepare(), indent=1))
    elif args.command == "run":
        print(json.dumps(fw.run(args.limit, args.jobs), indent=1))
    else:
        result = fw.score()
        fw.write_json(RESULTS / "result.json", result)
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in {"per_run", "meta"}},
                indent=1,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    threading.current_thread().name = "cm19"
    raise SystemExit(main())
