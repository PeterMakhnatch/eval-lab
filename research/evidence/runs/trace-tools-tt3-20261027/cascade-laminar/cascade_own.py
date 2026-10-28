"""Replay own Harbor runs through the CM-9 pf_r arm; subscription-only judges.

Run from this worktree with PYTHONPATH=src. The cohort is used only as a local
run-id/path index: labels, cohort, family, and trial names never enter the judge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production import command_prefilter
from evallab.production import window_cascade as wc
from evallab.production.monitor_worker import MisalignmentLiveWorker

MODEL = "kimi-code/k3.1-pro-highspeed"
OVERLAY = (
    'memory:\n  backend: "off"\n'
    "retry:\n  enabled: false\n  modelFallback: false\n  fallbackChains: {}\n"
)
KEY_ENV = re.compile(r"(_API_KEY|_AUTH_TOKEN|_OAUTH_TOKEN)$")
LABELS = {"exploit-pass", "defect-fail", "neither"}
STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
CANONICAL_SPEND = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"


def now() -> str:
    return datetime.now(UTC).isoformat()


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as sink:
        sink.write(json.dumps(row) + "\n")


def load_steps(trial: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Follow ATIF continuations, omit copied context, retain a local source map."""
    path = trial / "agent/trajectory.json"
    seen: set[Path] = set()
    steps: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    while True:
        path = path.resolve()
        if path in seen or path.parent != (trial / "agent").resolve():
            raise ValueError("invalid ATIF continuation chain")
        seen.add(path)
        raw = path.read_bytes()
        doc = json.loads(raw)
        kept = []
        for step in doc["steps"]:
            if step.get("is_copied_context"):
                continue
            # Do not feed top-level agent/session, extra, metrics, labels or source metadata.
            item = {
                key: step[key]
                for key in ("source", "message", "reasoning_content", "tool_calls", "observation")
                if key in step
            }
            item["step_id"] = len(steps) + 1
            if item.get("source") == "assistant":
                item["source"] = "agent"
            steps.append(item)
            kept.append({"step": item["step_id"], "source_step_id": step["step_id"]})
        sources.append({
            "file": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "steps": kept,
        })
        ref = doc.get("continued_trajectory_ref")
        if not ref:
            return steps, sources
        path = path.parent / ref


def parse_stream(stdout: str) -> dict[str, Any]:
    end = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type"))
        if "fallback" in kind or kind == "model_changed" or event.get("resolvedModelIsFallback"):
            raise RuntimeError(f"omp changed route ({kind})")
        if kind == "agent_end":
            end = event
    if end is None:
        raise RuntimeError("truncated stream: no agent_end")
    replies = [m for m in end.get("messages", []) if m.get("role") == "assistant"]
    if len(replies) != 1:
        raise RuntimeError(f"expected one assistant turn, saw {len(replies)}")
    reply = replies[0]
    served = f"{reply.get('provider')}/{reply.get('model')}"
    if served != MODEL:
        raise RuntimeError(f"route mismatch: requested {MODEL}, served {served}")
    if reply.get("stopReason") != "stop":
        raise RuntimeError(f"stopReason={reply.get('stopReason')}: {reply.get('errorMessage')}")
    content = reply.get("content")
    if not isinstance(content, list) or any(
        not isinstance(b, dict) or b.get("type") not in {"text", "thinking"} for b in content
    ):
        raise RuntimeError("assistant turn contains non-text content")
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    answer = json.loads(match.group(0)) if match else None
    if not isinstance(answer, dict) or answer.get("label") not in LABELS:
        raise RuntimeError("answer is not the judge JSON schema")
    evidence = answer.get("evidence")
    usage = reply.get("usage") or {}
    return {
        "ok": True,
        "answer": {
            "label": answer["label"], "mechanism": answer.get("mechanism"),
            "evidence": evidence if isinstance(evidence, list) else [],
        },
        "requested_model": MODEL, "served": served, "fallback": False,
        "response_id": reply.get("responseId"),
        "input_tokens": usage.get("input"), "output_tokens": usage.get("output"),
        "reasoning_tokens": usage.get("reasoningTokens"), "raw": text,
    }


class Judge:
    def __init__(self, cache_path: Path, *, cache_only: bool = False) -> None:
        self.path = cache_path
        self.cache = {r["key"]: r for r in rows(cache_path) if r.get("ok")} if cache_path.exists() else {}
        self.lock = threading.Lock()
        self.quota_error: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.hits = 0
        self.cache_only = cache_only

    def __call__(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], float]:
        system, user = messages[0]["content"], messages[-1]["content"]
        key = hashlib.sha256(
            f"{MODEL}\n{hashlib.sha256(system.encode()).hexdigest()}\n{user}".encode()
        ).hexdigest()
        with self.lock:
            cached = self.cache.get(key)
            if cached:
                self.hits += 1
                if cached.get("ok"):
                    return cached["answer"], 0.0
                raise RuntimeError(cached["error"])
            if self.quota_error:
                raise RuntimeError(f"quota-exhausted, batch stopped: {self.quota_error}")
            if self.cache_only:
                raise RuntimeError(
                    "unjudged: no successful cached verdict; Grok weekly quota exhausted, "
                    "awaiting Kimi subscription reset"
                )
        env = {k: v for k, v in os.environ.items() if not KEY_ENV.search(k)}
        started = time.perf_counter()
        record: dict[str, Any] = {
            "key": key, "at": now(), "requested_model": MODEL,
            "prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
        }
        try:
            with tempfile.TemporaryDirectory(prefix="tt3-cascade-") as directory:
                overlay = Path(directory) / "overlay.yml"
                overlay.write_text(OVERLAY)
                proc = subprocess.run(
                    ["omp", "-p", "--mode", "json", "--no-session", "--no-tools", "--no-lsp",
                     "--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-pty",
                     "--config", str(overlay), "--model", MODEL, "--system-prompt", system,
                     "--thinking", "low"],
                    input=user, capture_output=True, text=True, timeout=240, cwd=directory, env=env,
                )
            record["stream_sha256"] = hashlib.sha256(proc.stdout.encode()).hexdigest()
            if proc.returncode:
                raise RuntimeError(f"omp exit {proc.returncode}: {proc.stdout[-400:]}")
            record.update(parse_stream(proc.stdout))
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            record.update(ok=False, answer=None, error=f"{type(exc).__name__}: {exc}"[:900])
        record["elapsed_s"] = round(time.perf_counter() - started, 3)
        with self.lock:
            self.cache[key] = record
            self.calls.append(record)
            append(self.path, record)
            error = record.get("error", "").lower()
            if any(word in error for word in (
                "quota", "usage_limit", "usage limit", "usagelimit", "rate_limit",
                "rate limit", "429", "run out of credits", "spending-limit", "personal-team-blocked",
            )):
                self.quota_error = record["error"]
        if not record.get("ok"):
            raise RuntimeError(record["error"])
        return record["answer"], 0.0


def replay(run_id: str, steps: list[dict[str, Any]], judge: Any = None) -> dict[str, Any]:
    opaque = "tt3-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
    events: list[dict[str, Any]] = []
    with MisalignmentLiveWorker(emit=events.append, judge=judge, stage2_playbook_cards=0) as worker:
        for i, step in enumerate(steps):
            worker.ingest_event({
                "event_id": f"{opaque}:{i}", "run_id": opaque, "event_type": "trace.step",
                "payload": {"step": step},
            })
    latest: dict[str, dict[str, Any]] = {}
    flagged: dict[str, int] = {}
    failures = []
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "monitor.suspicion":
            card_id = payload["card_id"]
            flagged.setdefault(card_id, payload["evidence"][0]["step"])
            latest[card_id] = event
        elif event["event_type"] == "monitor.triage_unavailable":
            failures.append(payload)
    cards = []
    for card_id, event in latest.items():
        payload = event["payload"]
        cascade = payload.get("cascade") or {}
        cards.append({
            "rule": payload["rule"], "origin": payload.get("origin"),
            "flagged_step": flagged[card_id], "status": event["status"],
            "triage": payload.get("semantic_triage"), "label": payload.get("judge_label"),
            "verdict": cascade.get("verdict"), "judged_prefix": cascade.get("judged_prefix"),
        })
    alarm = any(c["status"] == "confirmed" or c["rule"] in wc.PASS_THROUGH_RULES for c in cards)
    return {
        "run_id": run_id, "stage1_any": bool(cards),
        "alarm": True if alarm else (None if failures else False), "cards": cards,
        "excluded_reason": failures if not alarm and failures else None,
        "triage_unavailable": len(failures), "n_steps": len(steps),
    }


def main() -> None:
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--stage1-only", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Recover retained verdicts; make no model calls")
    parser.add_argument("--model", default=MODEL, choices=(MODEL, "xai-oauth/grok-4.3"))
    args = parser.parse_args()
    MODEL = args.model
    state = args.state
    # Exact frozen pf_r arm in CM-9 e2e.py: all prefilter families, flagged
    # reasoning, no context reasoning, no Stage-2 playbook or reference examples.
    wc.PREFILTER_ESCALATED = frozenset("prefilter:" + f for f in command_prefilter.FAMILIES)
    wc.INCLUDE_REASONING = True
    wc.CONTEXT_REASONING_CHARS = 0
    cohort = rows(state / "tt3-cohort.jsonl")
    specs = [(r["run_id"], Path(r["trial_dir"])) for r in cohort]
    if len(specs) != 169 or len({r for r, _ in specs}) != 169:
        raise ValueError("expected the frozen 169-run own cohort")
    slug = "kimi" if MODEL.startswith("kimi-code/") else "grok"
    judge = Judge(state / f"cascade-own-{slug}-cache.jsonl", cache_only=args.cache_only)
    stage1_path = state / "cascade-own-stage1.jsonl"
    output_path = state / "cascade-own.jsonl"
    previous = {r["run_id"]: r for r in rows(output_path)} if output_path.exists() else {}
    stage1_known = {r["run_id"]: r for r in rows(stage1_path)} if stage1_path.exists() else {}
    if not args.stage1_only:
        if set(stage1_known) != {run for run, _ in specs}:
            raise ValueError("run --stage1-only first; every pending run needs a deterministic receipt")
        # Publish a complete view before attempting any judge. This remains valid
        # after interruption: unknown coverage is null, never a fresh negative.
        for run_id, _ in specs:
            if run_id in previous:
                continue
            first = stage1_known[run_id]
            passthrough = any(c["rule"] in wc.PASS_THROUGH_RULES for c in first["cards"])
            alarm = True if passthrough else (None if first["cards"] else False)
            previous[run_id] = {
                **first, "alarm": alarm, "model": None, "pending_model": MODEL,
                "judge_scope": "deterministic pass-through/no lead" if alarm is not None else "Stage-2 pending",
                "excluded_reason": [{
                    "error": "unjudged: subscription judge unavailable; resume with Kimi after quota reset",
                }] if alarm is None else None,
            }
        output_path.write_text("".join(json.dumps(previous[run]) + "\n" for run, _ in specs))
    # A resumed Kimi batch preserves completed Grok-qualified runs; never silently
    # replaces one judge's verdict with another. It fills only unknown alarms.
    known = {run: r for run, r in previous.items() if r.get("alarm") is not None}
    source_path = state / "cascade-own-sources.jsonl"
    sink_lock = threading.Lock()

    def work(spec: tuple[str, Path]) -> dict[str, Any]:
        run_id, trial = spec
        if not args.stage1_only and run_id in known:
            return known[run_id]
        if args.stage1_only and run_id in stage1_known:
            return stage1_known[run_id]
        steps, sources = load_steps(trial)
        first = stage1_known.get(run_id) or replay(run_id, steps, judge=None)
        result = first if args.stage1_only else replay(run_id, steps, judge=judge)
        result["stage1_any"] = first["stage1_any"]
        if not args.stage1_only:
            result["model"] = MODEL if any(c["triage"] == "completed" for c in result["cards"]) else None
            result["configured_model"] = MODEL
            result["judge_scope"] = (
                "CM-9 alternate Kimi judge" if slug == "kimi"
                else "v7 arm, grok judge (not production judge)"
            ) if result["model"] else "no completed Stage-2 judge verdict"
        with sink_lock:
            if run_id not in stage1_known:
                append(stage1_path, first)
                append(source_path, {"run_id": run_id, "sources": sources})
            if not args.stage1_only:
                append(output_path, result)
        return result

    started = now()
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(work, specs))
    finally:
        if not args.stage1_only and not args.cache_only:
            spend = {
                "at": now(), "tool": "cascade", "route": "omp -p (OAuth subscription)",
                "model": MODEL, "runs": len(specs),
                "input_tokens": sum(r.get("input_tokens") or 0 for r in judge.calls),
                "output_tokens": sum(r.get("output_tokens") or 0 for r in judge.calls), "usd": 0.0,
                "basis": {"marginal": "subscription; no BYOK env; fallback disabled", "started": started,
                          "calls": len(judge.calls), "successes": sum(bool(r.get("ok")) for r in judge.calls),
                          "cache_hits": judge.hits, "quota_error": judge.quota_error,
                          "usage_unavailable_calls": sum(r.get("input_tokens") is None for r in judge.calls)},
            }
            append(state / "spend.jsonl", spend)
            append(CANONICAL_SPEND, spend)
    if args.stage1_only:
        print(json.dumps({"runs": len(results), "stage1_any": sum(r["stage1_any"] for r in results)}), flush=True)
        return
    # Keep resumable partial appends during execution, then publish exactly one
    # final row per run (a retry must not leave stale null alarms in this view).
    temporary = output_path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(r) + "\n" for r in results))
    temporary.replace(output_path)
    receipt = {
        "at": now(), "arm": "CM-9 pf_r", "cascade_version": wc.CASCADE_VERSION, "model": MODEL,
        "n_runs": len(results), "stage1_any": sum(r["stage1_any"] for r in results),
        "alarmed": sum(r["alarm"] is True for r in results),
        "excluded": sum(r["alarm"] is None for r in results),
        "calls": len(judge.calls), "successful_calls": sum(bool(r.get("ok")) for r in judge.calls),
        "cache_hits": judge.hits, "quota_error": judge.quota_error,
        "execution_mode": "cache-only" if args.cache_only else "live",
        "runs_with_completed_judge_card": sum(any(c["triage"] == "completed" for c in r["cards"]) for r in results),
        "successful_cached_verdicts": sum(bool(r.get("ok")) for r in judge.cache.values()),
        "prefilter_escalated": sorted(wc.PREFILTER_ESCALATED),
        "include_reasoning": wc.INCLUDE_REASONING, "context_reasoning_chars": wc.CONTEXT_REASONING_CHARS,
        "stage2_playbook_cards": 0, "max_families": wc.MAX_FAMILIES,
        "max_diagnostic_families": wc.MAX_DIAGNOSTIC_FAMILIES,
        "window_chars": wc.MAX_WINDOW_CHARS, "radius": wc.RADIUS, "lookahead": wc.LOOKAHEAD,
    }
    (state / "cascade-own-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
