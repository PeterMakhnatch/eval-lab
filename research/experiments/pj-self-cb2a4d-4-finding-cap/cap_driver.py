#!/usr/bin/env python3
"""pJ-SELF-cb2a4d-4 driver: does raising the per-audit finding budget from three to six raise key yield?

Arms (44 keyed DEV tasks, frozen OQ-6 B prompt, no tools): T3 = terra unchanged (noise control), T6 = terra with the
single edit THREE -> SIX, S6 = Sonnet 5.5 with the same edit. Production parse and gate are reused; only the
transport's three-finding budget check is relaxed, and only for the SIX arms. Interrupted sessions are consumed,
never retried. Judging is a separate step (`judge_arms.py`). Preregistered in the bridge before any send.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "oq8_ab_driver", HERE.parent / "oq8-data-access-ab/ab_driver.py"
)
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)

ROOT = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-29/pj-self-cb2a4d-4-finding-cap"
)
CONFIG = ab.CONFIG
OLD = "Report at most THREE distinct concrete semantic grading defects"
NEW = OLD.replace("THREE", "SIX")
ARMS = {
    "T3": {
        "route": "openai-codex/gpt-6.1-terra",
        "thinking": "medium",
        "cap": 3,
        "provider": "openai-codex",
    },
    "T6": {
        "route": "openai-codex/gpt-6.1-terra",
        "thinking": "medium",
        "cap": 6,
        "provider": "openai-codex",
    },
    "S6": {
        "route": "anthropic/claude-sonnet-5-5",
        "thinking": "high",
        "cap": 6,
        "provider": "anthropic",
    },
}
ANTHROPIC_SLOTS = threading.BoundedSemaphore(3)


def prompt_for(case: dict, cap: int) -> str:
    text = ab.arm_prompt(case, "A")  # frozen B prompt bytes, drift-checked
    if cap == 3:
        return text
    if text.count(OLD) != 1:
        raise ValueError("frozen prompt does not contain the budget sentence exactly once")
    return text.replace(OLD, NEW)


def weekly_fraction(provider: str) -> float:
    result = subprocess.run(
        ["omp", "usage", "--provider", provider, "--json", "--redact", "--no-extensions"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    reports = json.loads(result.stdout).get("reports", [])
    fractions = [
        float(limit["amount"]["usedFraction"])
        for report in reports
        if report.get("provider") == provider
        for limit in report.get("limits", [])
        if limit.get("window", {}).get("id") == "7d"
        and "fable" not in limit.get("id", "")
        and limit["amount"].get("usedFraction") is not None
    ]
    if not fractions:
        raise RuntimeError(f"weekly quota unavailable for {provider}")
    return max(fractions)


def admit(stop: Path, provider: str) -> float:
    if stop.exists():
        raise ab.AdmissionStopped("stop file present")
    fraction = weekly_fraction(provider)
    if fraction >= 0.85:
        raise ab.AdmissionStopped(f"{provider} weekly quota reached 85%")
    return fraction


def budgeted_answer(h: dict, response: bytes, cap: int) -> dict:
    try:
        return h["transport"]._answer(response, "findings")
    except ValueError as exc:
        if str(exc) != "finding budget exceeded" or cap <= 3:
            raise
    text = response.decode().strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\s*```", text, flags=re.DOTALL)
        if not match:
            raise ValueError("malformed fenced response")
        text = match.group(1)
    full = h["transport"]._json(text)
    rows = full["findings"]
    if len(rows) > cap:
        raise ValueError("finding budget exceeded")
    # All other validation (status, refusal, object rows) is the production check, run on a three-row view.
    h["transport"]._answer(json.dumps({**full, "findings": rows[:3]}).encode(), "findings")
    return full


def proposals(h: dict, case: dict, response: str, route: str, cap: int) -> tuple:
    """`TaskQualityLiveWorker.audit_proposals` with the parser's per-model candidate limit set to the arm's cap.

    The production parser keeps at most five candidates; everything else (citation verification, nonblank quote rule,
    zero-floor eligibility) is the production code path.
    """
    tq = importlib.import_module(h["worker"].__module__)

    def verify(citation):
        check = tq.verify_citation(Path(case["package"]), citation)
        if citation.quote is None or not citation.quote.strip():
            return tq.replace(
                check,
                quote_found=False,
                detail="a nonblank quoted snippet is required for proposal verification",
            )
        return check

    candidates = tq.parse_candidates(
        response,
        model=route,
        rank=1,
        work_kind=tq.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=case["task_id"],
        verify=verify,
        limit=cap,
    )
    return tuple(
        c
        for c in candidates
        if tq.is_proposal_eligible(
            c.confidence,
            c.citation_verified,
            min_verified_confidence=0,
            min_unverified_confidence=0,
        )
    )


def session_receipt(
    directory: Path, route: str, cap: int, h: dict, wall=None, returncode=None
) -> dict:
    sessions = list((directory / "sessions").rglob("*.jsonl"))
    receipt = {
        "route": route,
        "status": "unanswered",
        "tokens_in": None,
        "tokens_out": None,
        "wall_seconds": wall,
        "assistant_records": 0,
        "fallback": False,
        "tool_calls": [],
        "protocol_violations": 0,
        "answer": None,
    }
    if len(sessions) != 1:
        receipt["error"] = "expected exactly one session JSONL"
        return receipt
    transport = h["transport"]
    rows, errors = transport._rows(sessions[0].read_bytes())
    messages = [
        r["message"]
        for r in rows
        if r.get("type") == "message" and r.get("message", {}).get("role") == "assistant"
    ]
    receipt["assistant_records"] = len(messages)
    receipt["fallback"] = any(
        n.get("resolvedModelIsFallback") is True or n.get("isFallback") is True
        for _, n in transport._walk(rows)
    )
    for message in messages:
        for part in message.get("content", []):
            if part.get("type") in ("toolCall", "tool_call", "toolUse", "tool_use"):
                receipt["protocol_violations"] += 1
    transport._account(receipt, messages)
    if (
        errors
        or receipt["fallback"]
        or receipt["protocol_violations"]
        or any(transport._identity(m) != route for m in messages)
    ):
        receipt.update(
            status="invalid", error="session identity, fallback, or tool protocol violation"
        )
    elif (
        messages
        and messages[-1].get("stopReason") not in ("error", "aborted", "toolUse")
        and returncode in (0, None)
    ):
        try:
            response = transport._text(messages[-1])
            receipt["answer"] = budgeted_answer(h, response, cap)
            receipt["status"] = "answered"
            receipt["response_sha256"] = ab.sha(response)
            if not (directory / "response.txt").exists():
                (directory / "response.txt").write_bytes(response)
        except (ValueError, TypeError) as exc:
            receipt.update(status="invalid", error=str(exc))
    receipt["session_path"] = str(sessions[0])
    return receipt


def call(directory: Path, *, arm: str, prompt: str, system: str, stop: Path, h: dict) -> dict:
    spec_ = ARMS[arm]
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / "receipt.json"
    if final.exists():
        return json.loads(final.read_text())
    if (directory / "consumed.json").exists() or list((directory / "sessions").rglob("*.jsonl")):
        result = session_receipt(directory, spec_["route"], spec_["cap"], h)
        ab.dump(final, result)
        return result
    admit(stop, spec_["provider"])
    (directory / "sessions").mkdir(exist_ok=True)
    (directory / "prompt.txt").write_text(prompt)
    (directory / "system.txt").write_text(system)
    argv = [
        "omp",
        "-p",
        "--config",
        str(CONFIG),
        "--model",
        spec_["route"],
        "--thinking",
        spec_["thinking"],
        "--no-prewalk",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--no-lsp",
        "--no-title",
        "--no-pty",
        "--mode",
        "json",
        "--session-dir",
        str(directory / "sessions"),
        "--max-time",
        "900s",
        "--no-tools",
        "--system-prompt",
        system,
        "@" + str(directory / "prompt.txt"),
    ]
    ab.dump(directory / "consumed.json", {"argv": argv, "started_at": time.time()})
    env, _ = h["transport"]._environment()
    env["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    started = time.monotonic()
    with (
        (directory / "stdout.jsonl").open("x") as stdout,
        (directory / "stderr.txt").open("x") as stderr,
    ):
        try:
            code = subprocess.run(
                argv, cwd=directory, env=env, stdout=stdout, stderr=stderr, timeout=960
            ).returncode
        except subprocess.TimeoutExpired:
            code = -1
    result = session_receipt(
        directory, spec_["route"], spec_["cap"], h, time.monotonic() - started, code
    )
    ab.dump(final, result)
    return result


def execute_cell(case: dict, arm: str, stop: Path, h: dict) -> dict:
    directory = ROOT / "cells" / arm / case["case_id"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "driver.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        final = directory / "cell.json"
        if final.exists():
            return json.loads(final.read_text())
        h["scorer"]._checked_source(case)
        spec_ = ARMS[arm]
        slot = ANTHROPIC_SLOTS if spec_["provider"] == "anthropic" else None
        if slot:
            slot.acquire()
        try:
            audit = call(
                directory / "auditor",
                arm=arm,
                prompt=prompt_for(case, spec_["cap"]),
                system=ab.auditor_system(),
                stop=stop,
                h=h,
            )
        finally:
            if slot:
                slot.release()
        raw = None if audit["status"] != "answered" else audit["answer"]["findings"]
        eligible, parse_error = None, None
        if raw is not None:
            try:
                candidates = proposals(
                    h,
                    case,
                    (directory / "auditor/response.txt").read_text(),
                    spec_["route"],
                    spec_["cap"],
                )
                payloads = [c.as_payload() for c in candidates]
                if len(payloads) != len(raw):
                    raise ValueError("raw/parsed finding counts differ")
                eligible = h["paired"].gate_memberships(payloads)["new_verified_0_5"]
            except (ValueError, TypeError, KeyError) as exc:
                parse_error = str(exc)
        result = {
            "case_id": case["case_id"],
            "arm": arm,
            "route": spec_["route"],
            "cap": spec_["cap"],
            "auditor": {
                k: audit.get(k)
                for k in (
                    "status",
                    "tokens_in",
                    "tokens_out",
                    "wall_seconds",
                    "fallback",
                    "protocol_violations",
                    "error",
                )
            },
            "raw_findings": None if raw is None else len(raw),
            "eligible_ids": eligible,
            "parse_error": parse_error,
            "findings": None
            if raw is None
            else [
                {
                    "id": f"C{i}",
                    "direction": f.get("direction"),
                    "mechanism": f.get("mechanism"),
                    "confidence": f.get("confidence"),
                    "citations": f.get("citations"),
                }
                for i, f in enumerate(raw, start=1)
            ],
        }
        ab.dump(final, result)
        print(
            json.dumps(
                {
                    "case": case["case_id"],
                    "arm": arm,
                    "status": audit["status"],
                    "raw": result["raw_findings"],
                    "eligible": None if eligible is None else len(eligible),
                    "parse_error": parse_error,
                }
            ),
            flush=True,
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--concurrency", type=int, default=7)
    parser.add_argument("--arms", default="T3,T6,S6")
    args = parser.parse_args()
    h = ab.helpers()
    cases = [c for c in ab.population() if c["stage"] == "dev" and c["key_count"] > 0]
    if len(cases) != 44:
        raise ValueError("population differs from contract")
    if not (args.smoke or args.run):
        print("44 cases; no model calls")
        return
    selected = cases[:2] if args.smoke else cases
    stop = ROOT / "STOP"
    ROOT.mkdir(parents=True, exist_ok=True)
    jobs = [(case, arm) for case in selected for arm in args.arms.split(",")]
    failures = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(execute_cell, case, arm, stop, h) for case, arm in jobs]
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # admission stop or driver error: report, keep the rest
                failures.append(f"{type(exc).__name__}: {exc}")
    print(json.dumps({"jobs": len(jobs), "failures": failures[:5]}))


if __name__ == "__main__":
    main()
