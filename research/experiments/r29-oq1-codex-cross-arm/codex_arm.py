#!/usr/bin/env python3
"""R29-OQ-1: run `openai-codex/gpt-6.1-terra` once over the frozen OQ-10 study-v2 cohort.

The frozen native transport only routes Sonnet, Haiku and Kimi, so this side driver sends the exact frozen prompt
bytes (verified by the study pins), the frozen detector system text, `thinking high`, no tools and the frozen
controlled config to Codex, then builds each cell with the frozen parser (`study.parse_flags`) so flags, gates and
the later scorer are unchanged. One send per slot; a consumed slot is never resent. Codex 7-day usage >= 85% or a
STOP file halts new sends. Subscription only; no paid route.

Modes: --selftest (offline parity replay of a stored Sonnet response, then one DEV-prompt smoke call), --run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

STUDY = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-28/oq10-task-quality-cross/study-v2")
OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-29/r29-oq1-codex-cross-arm")
CODE_SHA = "3d5fa88f84c9e14ace5b1527cf6c8e5fb1ca20604730b873c7ac01d74e0d0d00"
INPUTS_SHA = "a0c459f4b1903153c996b7a602ce17846c6b97e441b99f0158c869581702b14c"
ROUTE = "openai-codex/gpt-6.1-terra"
ROLE = "codex"
QUOTA_STOP = 0.85
DEV_SMOKE_PROMPT = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun-v4/calls/"
    "anthropic--claude-sonnet-5-5/B/dev/D0001/audit/prompt.txt"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Frozen:
    """The frozen study modules and inputs, loaded the way the frozen driver loads them."""

    def __init__(self) -> None:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(STUDY / "frozen-code"))
        spec = importlib.util.spec_from_file_location("run_study", STUDY / "frozen-code/run_study.py")
        self.run_study = importlib.util.module_from_spec(spec)
        sys.modules["run_study"] = self.run_study
        spec.loader.exec_module(self.run_study)
        self.freeze, self.study = self.run_study.freeze, self.run_study.study
        self.code, self.inputs, self.cases, self.sealed = self.run_study.load_inputs(
            STUDY / "code-freeze.json", CODE_SHA, INPUTS_SHA
        )
        import evallab  # noqa: PLC0415

        if Path(evallab.__file__).resolve() != Path(self.code["config"]["runtime_root"]) / "src/evallab/__init__.py":
            raise ValueError("parser import root differs from the frozen runtime")
        self.native = self.freeze.load_module("oq10_driver_native", self.code["own"]["native_route.py"])
        self.paired = self.freeze.load_module("oq10_driver_paired", self.code["retained"]["paired"])
        retained = self.freeze.load_module("oq10_driver_retained", self.code["retained"]["prepare_inputs"])
        self.system = retained.SYSTEM
        manifest = json.loads(Path(self.code["native_runtime"]["path"]).read_bytes())
        _, self.transport = self.native.load_runtime(manifest)
        self.config = Path(manifest["controlled_config"]["path"])
        self.config_sha = manifest["controlled_config"]["sha256"]
        from evallab.production.tq_worker import TaskQualityLiveWorker  # noqa: PLC0415

        self.worker = TaskQualityLiveWorker


class Stopped(RuntimeError):
    """No new send may start; in-flight sends finish."""


class Quota:
    def __init__(self, stop: Path) -> None:
        self.stop = stop
        self.lock = threading.Lock()
        self.last = 0.0
        self.fraction: float | None = None

    @staticmethod
    def weekly(report: dict) -> float:
        fractions = []
        for entry in report.get("reports", []):
            if entry.get("provider") != "openai-codex":
                continue
            if entry.get("error"):
                raise RuntimeError("quota report error")
            for limit in entry.get("limits", []):
                window = limit.get("window", {})
                if window.get("id") == "7d" or window.get("durationMs") == 604800000:
                    amount = limit["amount"]
                    fraction = amount.get("usedFraction")
                    if fraction is None:
                        fraction = amount["used"] / amount["limit"]
                    fractions.append(float(fraction))
        if not fractions or any(not 0 <= f <= 1 for f in fractions):
            raise RuntimeError("weekly quota unavailable")
        return max(fractions)

    def admit(self) -> float:
        if self.stop.exists():
            raise Stopped("stop file present")
        with self.lock:
            if self.fraction is None or time.monotonic() - self.last > 15:
                done = subprocess.run(
                    ["omp", "usage", "--provider", "openai-codex", "--json", "--redact", "--no-extensions"],
                    capture_output=True, text=True, check=True, timeout=60,
                )
                self.fraction = self.weekly(json.loads(done.stdout))
                self.last = time.monotonic()
            fraction = self.fraction
        if fraction >= QUOTA_STOP:
            raise Stopped(f"Codex weekly quota {fraction:.0%} >= 85%")
        return fraction


def write_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def session_receipt(frozen: Frozen, directory: Path, prompt: bytes, wall: float | None, returncode: int | None) -> dict:
    transport = frozen.transport
    receipt = {
        "route": ROUTE, "status": "unanswered", "answer": None, "wall_seconds": wall,
        "billing_mode": "subscription", "cost_usd": None, "model_fallback": False, "retries": 0,
        "tool_calls": 0, "prompt_sha256": sha(prompt), "attempt_consumed": True,
    }
    sessions = list((directory / "sessions").rglob("*.jsonl"))
    if len(sessions) != 1:
        receipt["error"] = "expected exactly one session JSONL"
        return receipt
    rows, errors = transport._rows(sessions[0].read_bytes())
    messages = [r["message"] for r in rows if r.get("type") == "message" and r["message"].get("role") == "assistant"]
    receipt["native_terminal_assistant_count"] = len(messages)
    receipt["model_fallback"] = any(
        n.get("resolvedModelIsFallback") is True or n.get("isFallback") is True for _, n in transport._walk(rows)
    )
    for message in messages:
        receipt["tool_calls"] += sum(
            1 for part in message.get("content", [])
            if isinstance(part, dict) and part.get("type") in ("toolCall", "tool_call", "toolUse", "tool_use")
        )
    transport._account(receipt, messages)
    served = sorted({transport._identity(m) or "unknown" for m in messages})
    receipt["served_identities"] = served
    if errors or receipt["model_fallback"] or receipt["tool_calls"] or served != [ROUTE]:
        receipt["status"] = "invalid"
        receipt["error"] = "session identity, fallback or tool protocol violation"
    elif messages and messages[-1].get("stopReason") not in ("error", "aborted", "toolUse") and returncode in (0, None):
        try:
            response = transport._text(messages[-1])
            receipt["answer"] = transport._answer(response, "findings")
            receipt["status"] = "answered"
            receipt["response_sha256"] = sha(response)
            receipt["response_path"] = str(directory / "response.txt")
            if not (directory / "response.txt").exists():
                (directory / "response.txt").write_bytes(response)
        except (ValueError, TypeError) as exc:
            receipt.update(status="invalid", error=str(exc))
    receipt["session_path"] = str(sessions[0])
    return receipt


def send(frozen: Frozen, quota: Quota, directory: Path, prompt: str, tool_free_label: str) -> dict:
    """One consumed Codex send; an interrupted session is finalized from its log, never resent."""
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / "receipt.json"
    if final.exists():
        return json.loads(final.read_text())
    raw = prompt.encode()
    if (directory / "call-consumed.json").exists() or list((directory / "sessions").rglob("*.jsonl")):
        receipt = session_receipt(frozen, directory, raw, None, None)
        write_new(final, receipt)
        return receipt
    fraction = quota.admit()
    (directory / "sessions").mkdir(exist_ok=True)
    (directory / "prompt.txt").write_bytes(raw)
    (directory / "system.txt").write_text(frozen.system)
    argv = [
        "omp", "-p", "--config", str(frozen.config), "--model", ROUTE, "--thinking", "high", "--no-tools",
        "--no-skills", "--no-rules", "--no-extensions", "--no-lsp", "--no-title", "--no-prewalk", "--no-pty",
        "--mode", "json", "--session-dir", str(directory / "sessions"), "--max-time", "600s",
        "--system-prompt", frozen.system, "@" + str(directory / "prompt.txt"),
    ]
    write_new(directory / "call-consumed.json", {"argv": argv, "started_at": time.time(), "quota_fraction": fraction,
                                                 "label": tool_free_label})
    env, _ = frozen.transport._environment()
    env["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    started = time.monotonic()
    with (directory / "stdout.jsonl").open("x") as out, (directory / "stderr.txt").open("x") as err:
        try:
            returncode = subprocess.run(argv, cwd=directory, env=env, stdout=out, stderr=err, timeout=660).returncode
        except subprocess.TimeoutExpired:
            returncode = -1
    receipt = session_receipt(frozen, directory, raw, time.monotonic() - started, returncode)
    write_new(final, receipt)
    return receipt


def native_result(receipt: dict, receipt_path: Path) -> dict:
    answered = receipt["status"] == "answered"
    return {
        "disposition": "answered" if answered else "missing", "terminal": True, "sent": True,
        "reason": None if answered else receipt.get("error") or receipt["status"],
        "answer": receipt["answer"] if answered else None, "receipt": receipt, "route": ROUTE,
        "receipt_file_sha256": sha(receipt_path.read_bytes()),
        "findings_count": len(receipt["answer"]["findings"]) if answered else None,
    }


def build_cell(frozen: Frozen, case: dict, native: dict, role: str, route: str) -> dict:
    study = frozen.study
    value = {
        "case_id": case["case_id"], "role": role, "route": route, "terminal": native["terminal"],
        "status": native["disposition"], "sent": native["sent"], "reason": native["reason"], "window": None,
        "flags": {gate: None for gate in study.GATES}, "native": native, **study.source_binding(case),
    }
    value["prediction"] = study.parse_flags(
        case, native, route=route, task_quality_worker=frozen.worker, paired=frozen.paired, native_helper=frozen.native,
    )
    value["flags"] = value["prediction"]["flags"]
    value["native_disposition"] = native["disposition"]
    value["status"] = value["prediction"]["status"]
    return value


def execute(frozen: Frozen, quota: Quota, case: dict) -> dict | None:
    """Terminal cell for one slot, or None when admission stopped before any send."""
    study = frozen.study
    final = OUT / "cells" / ROLE / (case["case_id"] + ".json")
    study.verify_case(case)
    directory = OUT / "calls" / ROLE / case["case_id"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "driver.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if final.exists():
            return json.loads(final.read_bytes())
        if case["preparation_status"] != "prepared":
            value = {
                "case_id": case["case_id"], "role": ROLE, "route": ROUTE, "terminal": True,
                "status": "source_unavailable", "sent": False, "flags": {g: None for g in study.GATES},
                "reason": case.get("preparation_error"), **study.source_binding(case),
            }
            study.dump_new(final, value)
            return value
        prompt = Path(case["prompt"]["path"]).read_text(encoding="utf-8")
        try:
            receipt = send(frozen, quota, directory, prompt, "r29-oq1-codex-arm")
        except Stopped:
            return None
        native = native_result(receipt, directory / "receipt.json")
        value = build_cell(frozen, case, native, ROLE, ROUTE)
        study.dump_new(final, value)
        return value


def selftest(frozen: Frozen, quota: Quota) -> None:
    """Parity replay of stored Sonnet responses (no send), then one DEV-prompt smoke call."""
    sonnet_route = frozen.code["roles"]["sonnet"]
    checked = 0
    for case in frozen.cases:
        cell_path = STUDY / "cells/sonnet" / (case["case_id"] + ".json")
        if case["preparation_status"] != "prepared" or not cell_path.exists():
            continue
        stored = json.loads(cell_path.read_bytes())
        if stored["status"] != "ok":
            continue
        replay = build_cell(frozen, case, stored["native"], "sonnet", sonnet_route)
        same = replay["flags"] == stored["flags"] and replay["prediction"]["memberships"] == stored["prediction"]["memberships"]
        if not same:
            raise SystemExit(f"parity replay differs for {case['case_id']}")
        checked += 1
        if checked == 6:
            break
    print(json.dumps({"parity_replays_identical": checked}))
    directory = OUT / "selftest-dev-smoke"
    receipt = send(frozen, quota, directory, DEV_SMOKE_PROMPT.read_text(), "dev-smoke")
    summary = {k: receipt.get(k) for k in ("status", "served_identities", "model_fallback", "tool_calls", "tokens_in",
                                          "tokens_out", "wall_seconds", "error")}
    summary["findings"] = len(receipt["answer"]["findings"]) if receipt.get("answer") else None
    print(json.dumps(summary))
    if receipt["status"] != "answered" or receipt["served_identities"] != [ROUTE] or receipt["tool_calls"]:
        raise SystemExit("DEV smoke call failed the attestation checks")


def progress(done: int, total: int, quota: Quota, stopped: str | None) -> None:
    path = OUT / "progress.json"
    path.write_text(json.dumps({"updated_at": time.time(), "cells_done": done, "total": total,
                                "quota_fraction": quota.fraction, "stopped": stopped}, indent=2))


def run(frozen: Frozen, quota: Quota, concurrency: int) -> None:
    cases = list(frozen.cases)
    done = 0
    stopped = None
    with ThreadPoolExecutor(concurrency) as pool:
        pending = {}
        queue = iter(cases)

        def feed() -> None:
            while len(pending) < concurrency and stopped is None:
                case = next(queue, None)
                if case is None:
                    return
                pending[pool.submit(execute, frozen, quota, case)] = case["case_id"]

        feed()
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                case_id = pending.pop(future)
                value = future.result()
                if value is None:
                    stopped = "admission stopped before a send"
                else:
                    done += 1
                    print(json.dumps({"case_id": case_id, "status": value["status"]}), flush=True)
            progress(done, len(cases), quota, stopped)
            feed()
    progress(done, len(cases), quota, stopped)
    print(json.dumps({"cells_done": done, "total": len(cases), "stopped": stopped}))


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selftest", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--concurrency", type=int, default=7)
    parser.add_argument("--stop-file", type=Path, default=OUT / "STOP")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    frozen = Frozen()
    quota = Quota(args.stop_file)
    if args.selftest:
        selftest(frozen, quota)
    else:
        run(frozen, quota, args.concurrency)


if __name__ == "__main__":
    main()
