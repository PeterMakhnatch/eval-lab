#!/usr/bin/env python3
"""B5 (CM-19 with another judge): CM-13's frozen facet-cited windows, re-judged unchanged by Sonnet 5.5.

The CM-13 plan and its 418 unique prompts are copied byte-for-byte into a private cm19-sonnet state directory (the
sha256 pins in the CM-13 freeze are re-checked), the judge is swapped, the Anthropic quota gate replaces the
Codex one, and the CM-13 scorer runs unchanged on the new judge cache. Copied from CM-19's cm19_opus.py;
only the judge constant, the state directory and this docstring differ. Prompts and replies stay outside git.

    cm19_sonnet.py prepare            # copy plan/prompts, verify the CM-13 hashes
    cm19_sonnet.py run --jobs 4 [--limit N]
    cm19_sonnet.py score              # writes results/result.json (CM-13's aggregate on the Sonnet cache)

Preregistration: research-context causal-prefix-monitor.md, CM-19 and BURST slice B5.
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
STATE = CM13_STATE.parent / "cm19-sonnet"
RESULTS = HERE.parent / "results"
SONNET = "anthropic/claude-sonnet-5-5"
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
    fw.MODEL = SONNET
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
        "judge": SONNET,
        "thinking": fw.THINKING,
    }


def verify_admission(plan: dict[str, Any]) -> dict[str, Any]:
    """Strict DEV admission (assignments.csv cross-key) before any frozen prompt is sent.

    The CM-13 plan was built after ``admit()``; this re-runs it now and refuses to judge unless the admitted
    cohort is still exactly the frozen plan's 150 runs (80 enacted / 70 clean).
    """
    data = fw.admit()
    strata = Counter(label.stratum for label in data["labels"].values())
    frozen = set(plan["runs"])
    admitted = set(data["cohort"])
    report = {
        "admission_checked": data["admission_checked"],
        "admitted": len(admitted),
        "strata": dict(strata),
        "plan_runs": len(frozen),
        "plan_equals_admitted": frozen == admitted,
    }
    if len(admitted) != 150 or strata != {"enacted_yes": 80, "clean_no": 70} or frozen != admitted:
        raise SystemExit(f"DEV admission does not reproduce the frozen plan: {report}")
    return report


def judge_report() -> dict[str, Any]:
    """Served-model attestation and token accounting over every ledger row in the Sonnet cache."""
    rows = [
        json.loads(line)
        for line in (STATE / "judge-cache.jsonl").read_text().splitlines()
        if line.strip()
    ]
    ok = [r for r in rows if r["ok"]]
    failed = Counter(error for r in rows if not r["ok"] for error in r["errors"])
    spend = [
        entry
        for entry in (
            json.loads(line) for line in fw.SPEND.read_text().splitlines() if line.strip()
        )
        if entry.get("model") == SONNET and entry.get("tool") == "cm13-facet-windows"
    ]
    return {
        "requested_model": SONNET,
        "thinking": fw.THINKING,
        "served_model_attestation": (
            "enforced per call: the assistant turn's provider/model must equal the requested model, "
            "else the call is recorded as failed (parse_stream)"
        ),
        "ledger_rows": len(rows),
        "calls_ok": len(ok),
        "served_models_ok": dict(Counter(r["model"] for r in ok)),
        "attestation_mismatches": failed.get("served_model_attestation_mismatch", 0),
        "fallback_or_model_change": failed.get("fallback_or_model_change_refused", 0),
        "attempts_ok": dict(Counter(r["attempts"] for r in ok)),
        "failed_rows_not_sent": dict(failed),
        "tokens_ok": {
            "input": sum(r["input_tokens"] for r in ok),
            "output": sum(r["output_tokens"] for r in ok),
        },
        "tokens_all_attempts": {
            "input": sum(r.get("input_tokens") or 0 for r in rows),
            "output": sum(r.get("output_tokens") or 0 for r in rows),
        },
        "spend_entries": len(spend),
        "spend_tokens_all_attempts": {
            "input": sum(e["input_tokens"] for e in spend),
            "output": sum(e["output_tokens"] for e in spend),
        },
        "usd": 0.0,
        "first_call": min(r["at"] for r in ok) if ok else None,
        "last_call": max(r["at"] for r in ok) if ok else None,
        "quota_observations_last": (spend[-1]["basis"]["quota_observations"] if spend else []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    runner = sub.add_parser("run")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--jobs", type=int, default=4)
    sub.add_parser("score")
    sub.add_parser("attest")
    args = parser.parse_args()
    configure()
    if args.command == "prepare":
        print(json.dumps(prepare(), indent=1))
    elif args.command == "run":
        plan = json.loads((STATE / "plan.json").read_text())
        admission = verify_admission(plan)
        fw.write_json(RESULTS / "admission.json", admission)
        print(json.dumps(admission, indent=1))
        print(json.dumps(fw.run(args.limit, args.jobs), indent=1))
    elif args.command == "attest":
        report = judge_report()
        fw.write_json(RESULTS / "judge.json", report)
        print(json.dumps(report, indent=1))
    else:
        result = fw.score()
        # outer fields only: the frozen CM-13 plan's meta still names the CM-13 judge, so record the real
        # one beside it rather than editing the plan or the scorer's aggregate.
        result["judge"] = {
            "requested_model": SONNET,
            "thinking": fw.THINKING,
            "attestation": judge_report(),
        }
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
    threading.current_thread().name = "b5-cm19-sonnet"
    raise SystemExit(main())
