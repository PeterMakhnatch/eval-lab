#!/usr/bin/env python3
"""SK-7 runner: arms R1C / R1CS, two samples, headless omp per attempt, attested route."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw/SK-5/receipts/code")
import parser as answer_parser  # noqa: E402

BASE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
OUT = BASE / "SK-7" / "receipts"
WORK = Path("/tmp/sk7")
SESS = WORK / "sessions"
CONFIG = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/worker.yml")
MODEL = "deepseek/deepseek-flash"
PRICE_IN, PRICE_OUT, PRICE_CACHE = 0.30 / 1e6, 1.20 / 1e6, 0.006 / 1e6
LOCK = threading.Lock()
TARGET = OUT / "attempts.jsonl"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stream_messages(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def final_assistant(text: str):
    out, provider, model = "", None, None
    for event in stream_messages(text):
        msgs = []
        if isinstance(event, dict) and event.get("type") == "agent_end":
            msgs = event.get("messages") or []
        elif isinstance(event, dict) and event.get("role") == "assistant":
            msgs = [event]
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            parts = [p.get("text", "") for p in (m.get("content") or [])
                     if isinstance(p, dict) and p.get("type") == "text"]
            if any(parts):
                out, provider, model = "".join(parts), m.get("provider"), m.get("model")
    return out, provider, model


def usage_of(text: str):
    inp = out = cached = 0
    cost = None
    seen = False
    for event in stream_messages(text):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "agent_end":
            msgs = [m for m in (event.get("messages") or []) if isinstance(m, dict)]
        elif event.get("role") == "assistant":
            msgs = [event]
        else:
            msgs = []
        for m in msgs:
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            i = u.get("input") or u.get("input_tokens") or u.get("prompt_tokens")
            o = u.get("output") or u.get("output_tokens") or u.get("completion_tokens")
            c = u.get("cacheRead") or 0
            if isinstance(i, (int, float)) and isinstance(o, (int, float)):
                inp, out = i, o
                cached = c if isinstance(c, (int, float)) else 0
                seen = True
                cst = u.get("cost")
                if isinstance(cst, dict) and isinstance(cst.get("total"), (int, float)):
                    cost = float(cst["total"])
    return (inp, out, cached, cost) if seen else (None, None, 0, None)


def session_attestation(session_dir: Path):
    models, assistant, fallback, nfiles = set(), set(), [], 0
    if not session_dir.exists():
        return {"session_models": [], "session_assistant_routes": [], "fallback_flag_any": False,
                "session_log_files": 0}
    for path in sorted(session_dir.glob("*.jsonl")):
        nfiles += 1
        for event in stream_messages(path.read_text(encoding="utf-8", errors="replace")):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "model_change":
                models.add(event.get("model"))
                fallback.append(bool(event.get("resolvedModelIsFallback")))
            elif event.get("type") == "message":
                m = event.get("message") or {}
                if isinstance(m, dict) and m.get("role") == "assistant" and m.get("model"):
                    assistant.add(f"{m.get('provider')}/{m.get('model')}")
    return {"session_models": sorted(x for x in models if x),
            "session_assistant_routes": sorted(assistant),
            "fallback_flag_any": any(fallback), "session_log_files": nfiles}


def run_one(item: dict) -> dict:
    key = f"{item['task_id']}|{item['arm']}|{item['sample']}|{item.get('attempt', 1)}"
    session_dir = SESS / key.replace("|", "_")
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = Path(item["prompt_path"]).read_text(encoding="utf-8")
    argv = ["omp", "-p", "--mode", "json", "--no-tools", "--no-skills", "--no-rules",
            "--no-extensions", "--session-dir", str(session_dir), "--no-title", "--no-lsp",
            "--thinking", os.environ.get("SK7_THINKING", "low"), "--model", MODEL,
            "--max-time", os.environ.get("SK7_MAX_TIME", "240"),
            "--config", str(CONFIG), prompt]
    t0 = time.monotonic()
    rec = {"task_id": item["task_id"], "arm": item["arm"], "sample": item["sample"],
           "attempt": item.get("attempt", 1), "half": item["half"],
           "requested_model": MODEL, "thinking": os.environ.get("SK7_THINKING", "low"), "prompt_sha256": item["prompt_sha256"],
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        proc = subprocess.run(argv, cwd="/tmp", capture_output=True, text=True, timeout=400)
        stdout, stderr, rc = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr, rc = "timeout", None
    rec["wall_seconds"] = round(time.monotonic() - t0, 2)
    rec["returncode"] = rc
    raw, provider, model = final_assistant(stdout)
    session = session_attestation(session_dir)
    rec["served_provider_stream"], rec["served_model_stream"] = provider, model
    rec["session_models"] = session["session_models"]
    rec["session_assistant_routes"] = session["session_assistant_routes"]
    rec["fallback_flag_any"] = session["fallback_flag_any"]
    rec["session_log_files"] = session["session_log_files"]
    rec["attested"] = bool(
        provider == "deepseek" and model == "deepseek-flash"
        and session["session_models"] == ["deepseek/deepseek-flash"]
        and not session["fallback_flag_any"]
        and session["session_log_files"] > 0
    )
    inp, outp, cached, cost = usage_of(stdout)
    rec["input_tokens"], rec["output_tokens"], rec["cache_read_tokens"] = inp, outp, cached
    rec["dollars_provider"] = cost
    if inp is None:
        rec["dollars"] = None
    elif cost is not None:
        rec["dollars"] = round(cost, 6)
    else:
        rec["dollars"] = round((inp or 0) * PRICE_IN + cached * PRICE_CACHE + (outp or 0) * PRICE_OUT, 6)
    rec["raw_response"] = raw
    rec["raw_response_sha256"] = sha(raw)
    p = answer_parser.parse(raw)
    rec["answer_status"] = p["status"]
    rec["proposed_files"] = sorted(p["files"])
    rec["proposed_solve_chars"] = len(p["files"].get("solve.sh", ""))
    rec["unterminated_blocks"] = p["unterminated"]
    rec["stray_chars"] = p["stray_chars"]
    rec["note"] = p["note"]
    rec["stderr_head"] = (stderr or "")[:400]
    return rec


def build_items() -> list[dict]:
    prompts = json.loads((OUT / "prompts.json").read_text())
    tasks = [json.loads(line) for line in (BASE / "SK-2" / "receipts" / "tasks.jsonl").read_text().splitlines() if line.strip()]
    items = []
    for sample in (1, 2):
        for t in tasks:
            tid = t["task_id"]
            for arm in ("R1C", "R1CS"):
                p = prompts[f"{tid}|{arm}"]
                items.append({"task_id": tid, "arm": arm, "sample": sample,
                              "half": p["half"], "prompt_path": p["path"], "prompt_sha256": p["sha256"]})
    items.sort(key=lambda it: (it["sample"], sha(it["task_id"]), it["arm"]))
    return items


def main() -> int:
    workers = int(os.environ.get("SK7_JOBS", "4"))
    cap = float(os.environ.get("SK7_CAP", "0.95"))
    items = build_items()
    done = set()
    if TARGET.exists():
        for line in TARGET.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    if r.get("attested"):
                        done.add((r["task_id"], r["arm"], r["sample"]))
                except ValueError:
                    pass
    else:
        TARGET.write_text("")
    todo = [it for it in items if (it["task_id"], it["arm"], it["sample"]) not in done]
    print(f"planned {len(items)}, todo {len(todo)}", flush=True)
    spent = sum(json.loads(line).get("dollars") or 0 for line in TARGET.read_text().splitlines() if line.strip())
    n, failures = 0, 0
    for start in range(0, len(todo), workers):
        if spent >= cap:
            print(f"cap reached ${spent:.3f}", flush=True)
            break
        chunk = todo[start:start + workers]
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            recs = [f.result() for f in cf.as_completed([pool.submit(run_one, it) for it in chunk])]
        retry = []
        for rec in recs:
            spent += rec.get("dollars") or 0
            if not rec["attested"] and rec["attempt"] < 2:
                retry.append({**{k: rec[k] for k in ("task_id", "arm", "sample", "half")},
                              "prompt_path": str(Path(OUT / "prompts.json")),
                              "prompt_sha256": rec["prompt_sha256"], "attempt": 2})
            with LOCK, TARGET.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            n += 1
        for it in retry:
            prompts = json.loads((OUT / "prompts.json").read_text())
            p = prompts[f"{it['task_id']}|{it['arm']}"]
            it["prompt_path"], it["prompt_sha256"] = p["path"], p["sha256"]
            rec = run_one(it)
            spent += rec.get("dollars") or 0
            with LOCK, TARGET.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            n += 1
            if not rec["attested"]:
                failures += 1
        print(f"{n}/{len(todo)} spent=${spent:.3f} unattested_retried_failed={failures}", flush=True)
    print(f"done {n} of {len(todo)} spent=${spent:.3f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
