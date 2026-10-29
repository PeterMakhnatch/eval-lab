#!/usr/bin/env python3
"""B4: CM-13's frozen facet-cited windows, re-judged unchanged by the production Kimi Stage-2 route.

A copy of CM-19's ``cm19_opus.py`` with three changes: the judge is ``kimi-code/k3.1-pro-highspeed``
(thinking low, ``omp -p`` subscription route, served model attested per call), a Kimi quota gate
(``omp usage --json --provider kimi-code``, stop at 85% of any window) replaces the Anthropic one,
and the private state directory is ``comparisons/cm19-kimi``. The CM-13 plan and its 418 unique
prompts are copied byte-for-byte (the CM-13 freeze sha256 pins are re-checked) and the CM-13 scorer
runs unchanged on the Kimi cache. Prompts and replies stay outside git.

    cm19_kimi.py prepare            # copy plan/prompts, verify the CM-13 hashes
    cm19_kimi.py run --jobs 4 [--limit N]
    cm19_kimi.py score              # writes results/result.json (CM-13's aggregate on the Kimi cache)

Preregistration: research-context causal-prefix-monitor.md, BURST slice B4 (Peter, 4:52 PM ET).
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
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
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
STATE = CM13_STATE.parent / "cm19-kimi"
RESULTS = HERE.parent / "results"
KIMI = "kimi-code/k3.1-pro-highspeed"
PROVIDER = "kimi-code"
KIMI_STOP = 0.85


def reset_stamp(window: Mapping[str, Any]) -> str | None:
    """The window's reset time, in UTC, for the receipt."""
    for field in ("resetsAt", "resets_at", "resetAt"):
        value = window.get(field)
        if isinstance(value, int | float) and value > 0:
            seconds = value / 1000 if value > 10**11 else value
            return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")
    return None


class KimiGate(fw.QuotaGate):
    """Stop at 85% of any Kimi window; an empty or unreadable report stops the run."""

    def check(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                raise fw.CallError("quota_gate_stopped", quota=True)
            if time.monotonic() - self.checked < 60:
                return
            try:
                env = fw.subscription_env()
                subprocess.run(
                    ["omp", "usage", "invalidate", "--provider", PROVIDER],
                    capture_output=True, text=True, timeout=60, env=env, check=False,
                )  # fmt: skip
                windows: list[dict[str, Any]] = []
                exhausted = False
                for _ in range(4):
                    proc = subprocess.run(
                        ["omp", "usage", "--json", "--provider", PROVIDER],
                        capture_output=True, text=True, timeout=120, env=env, check=True,
                    )  # fmt: skip
                    for report in json.loads(proc.stdout).get("reports", []):
                        if report.get("provider") != PROVIDER:
                            continue
                        if report.get("metadata", {}).get("allowed") is False:
                            raise fw.CallError("route_quota_disallowed", quota=True)
                        for limit in report.get("limits", []):
                            amount = limit.get("amount", {})
                            fraction = amount.get("usedFraction")
                            if fraction is None and amount.get("limit"):
                                fraction = amount.get("used", 0) / amount["limit"]
                            if not isinstance(fraction, int | float) or not 0 <= fraction <= 1:
                                raise fw.CallError("quota_window_unavailable", quota=True)
                            windows.append(
                                {
                                    "window": limit.get("window", {}).get("id"),
                                    "used": fraction,
                                    "resets_utc": reset_stamp(limit.get("window", {})),
                                    "status": limit.get("status"),
                                }
                            )
                            if fraction >= KIMI_STOP or limit.get("status") == "exhausted":
                                exhausted = True
                    if windows:
                        break
                    time.sleep(5)
                if not windows:
                    raise fw.CallError("quota_windows_missing", quota=True)
                # CM-19's gate raised on the first hot window; recording every window keeps the stop receipt complete.
                self.observations.append({"at": fw.now(), "windows": windows})
                if exhausted:
                    raise fw.CallError("quota_85_percent_stop", quota=True)
                self.checked = time.monotonic()
            except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
                self.stopped.set()
                raise fw.CallError("quota_probe_failed", quota=True) from exc
            except fw.CallError:
                self.stopped.set()
                raise


def usage_with_cache(stdout: str) -> dict[str, int]:
    """Route-reported prompt tokens: uncached input plus cache reads and writes (as CM-19 counted them)."""
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
    fw.MODEL = KIMI
    fw.usage_of = usage_with_cache
    fw.QuotaGate = KimiGate
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
        "judge": KIMI,
        "thinking": fw.THINKING,
    }


def guard() -> dict[str, Any]:
    """Strict DEV admission before any prompt is sent: the frozen plan must be exactly the admitted cohort."""
    plan = json.loads((STATE / "plan.json").read_text())
    prompts = json.loads((STATE / "prompts.json").read_text())
    data = fw.admit()
    strata = Counter(data["labels"][i].stratum for i in data["cohort"])
    if set(data["cohort"]) != set(plan["runs"]):
        raise SystemExit("frozen plan runs differ from the strict DEV-admitted cohort")
    if strata != {"enacted_yes": 80, "clean_no": 70}:
        raise SystemExit(f"DEV cohort is not 80 enacted / 70 clean: {dict(strata)}")
    plan_keys = {
        w["key"] for run in plan["runs"].values() for arm in ("A", "B") for w in run[arm]["windows"]
    }
    if not plan_keys <= set(prompts):
        raise SystemExit("the frozen plan references prompts missing from the frozen prompt set")
    return {
        "runs": len(data["cohort"]),
        "strata": dict(strata),
        "admission_checked": data["admission_checked"],
        "plan_prompts": len(plan_keys),
        "unique_prompts": len(prompts),
    }


def smoke() -> dict[str, Any]:
    """No model call: the admission guard plus one real quota probe, written to results/smoke.json."""
    report: dict[str, Any] = {"at": fw.now(), "model_calls": 0, "admission": guard()}
    gate = KimiGate()
    try:
        gate.check()
        report["quota"] = {"outcome": "open", "windows": gate.observations[-1]["windows"]}
    except fw.CallError as error:
        report["quota"] = {
            "outcome": "stopped",
            "code": str(error),
            "windows": gate.observations[-1]["windows"] if gate.observations else None,
        }
    fw.write_json(RESULTS / "smoke.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    runner = sub.add_parser("run")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--jobs", type=int, default=4)
    sub.add_parser("score")
    sub.add_parser("smoke")
    args = parser.parse_args()
    configure()
    if args.command == "prepare":
        print(json.dumps(prepare(), indent=1))
    elif args.command == "smoke":
        print(json.dumps(smoke(), indent=1))
    elif args.command == "run":
        print(json.dumps(guard(), indent=1))
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
    threading.current_thread().name = "b4-cm19-kimi"
    raise SystemExit(main())
