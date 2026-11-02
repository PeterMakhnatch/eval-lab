"""Dry run script for D6-STAGE2-CHEAP: 5-case validation across models.

Validates that cheap models (gemini-2.5-flash-lite, gemini-3.1-flash-lite)
successfully receive Stage 2 prompts, parse verdicts, output proper JSON,
and record 100% attestation with 0 fallbacks.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from evallab import autoreview_monitor as am
from evallab.trace_anatomy import LoadedRun, build_anatomy, load_atif_file, load_run

BENCH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
D2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2")
TW_DIR = Path.home() / ".local/state/evallab-prod-live/factory/tw-dev-export"
WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/worker.yml"

DRY_CASES = [
    "tmb1-10bd2dc8c283",  # terminal_wrench, L0 triggered
    "tmb1-137205d7b0b1",  # terminal_wrench, L0 triggered
    "tmb1-032047ab7c59",  # cheatbench, suspicion 50 triggered
    "tmb1-c04ad53b6dfd",  # c1c5, suspicion 40 triggered
    "tmb1-8186f6fa7877",  # deep_swe, suspicion 50 triggered
]


def load_case_run(cid: str, manifest: dict[str, Any]) -> LoadedRun:
    row = manifest[cid]
    fam = row["family"]
    sid = row["source_id"]
    if fam in ("cheatbench", "goodfire", "deep_swe"):
        return load_run(sid, repo_root=Path("."))
    elif fam == "terminal_wrench":
        return load_atif_file(TW_DIR / f"{sid}.json")
    elif fam == "c1c5":
        traj = json.loads((BENCH / "own-trajectories" / f"{sid}.json").read_text())
        doc = {"metadata": {"split": "dev"}, "steps": traj["steps"]}
        return LoadedRun(sid, "atif", doc, None, None, len(json.dumps(traj["steps"])))
    else:
        raise ValueError(f"Unknown family: {fam}")


def parse_verdict_robust(text: str) -> dict[str, Any] | None:
    v = am.parse_verdict(text)
    if v is not None:
        return v
    text = (text or "").strip()
    idx = text.rfind('{"suspicion"')
    if idx >= 0:
        end = text.rfind("}")
        if end > idx:
            with contextlib.suppress(ValueError, TypeError):
                return am.parse_verdict(text[idx : end + 1])
    return None


def extract_attestation(session_dir: Path) -> dict[str, Any]:
    served: list[str] = []
    fallbacks: list[str] = []
    inp_tok = 0
    out_tok = 0
    usd = 0.0
    for p in session_dir.rglob("*.jsonl"):
        if p.name.endswith(".lock.os"):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "message" and ev.get("message", {}).get("role") == "assistant":
                msg = ev["message"]
                p_name = msg.get("provider")
                m_name = msg.get("model")
                if p_name and m_name:
                    served.append(f"{p_name}/{m_name}")
                elif m_name:
                    served.append(m_name)
                u = msg.get("usage", {})
                inp_tok += int(u.get("input", 0))
                out_tok += int(u.get("output", 0))
                cost = u.get("cost", {})
                if isinstance(cost, dict):
                    usd += float(cost.get("total", 0.0) or 0.0)
            elif ev.get("type") == "fallback":
                fallbacks.append(str(ev))
    return {
        "served_models": sorted(set(served)),
        "fallbacks": fallbacks,
        "input_tokens": inp_tok,
        "output_tokens": out_tok,
        "usd": usd,
    }


def run_stage2_call(prompt: str, model: str, tag: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as pf:
        pf.write(prompt)
        prompt_file = pf.name

    sess_dir = Path("/tmp/d6-dry-sessions") / tag
    sess_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "omp",
        "-p",
        f"@{prompt_file}",
        "--model",
        model,
        "--config",
        WORKER_YML,
        "--no-skills",
        "--no-tools",
        "--thinking",
        "low",
        "--session-dir",
        str(sess_dir),
        "--no-title",
        "--mode",
        "text",
    ]

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        retcode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        retcode, stdout, stderr = -9, "", str(exc)
    finally:
        with contextlib.suppress(OSError):
            os.remove(prompt_file)
    latency = time.perf_counter() - t0

    v = parse_verdict_robust(stdout)
    att = extract_attestation(sess_dir)

    return {
        "returncode": retcode,
        "stdout": stdout,
        "stderr": stderr[-500:],
        "parsed_verdict": v,
        "parse_ok": v is not None,
        "attestation": att,
        "latency_s": round(latency, 2),
    }


def main() -> None:
    with open(BENCH / "manifest.jsonl", encoding="utf-8") as f:
        manifest = {r["case_id"]: r for r in [json.loads(line) for line in f if line.strip()]}
    models = [
        ("gemini-2.5-flash-lite", "google-antigravity/gemini-2.5-flash-lite"),
        ("gemini-3.1-flash-lite", "google-antigravity/gemini-3.1-flash-lite"),
    ]

    print("=== D6-STAGE2-CHEAP 5-CASE DRY RUN ===")
    results = {}
    for short_name, full_model in models:
        print(f"\n--- Testing model: {short_name} ({full_model}) ---")
        results[short_name] = []
        for cid in DRY_CASES:
            run = load_case_run(cid, manifest)
            anatomy = build_anatomy(run)
            view = am.build_compact_view(anatomy, run)
            target_steps = am.candidate_stage2_steps(anatomy)
            p2 = am.stage2_prompt(view, run, target_steps)
            tag = f"dry-{short_name}-{cid}"
            print(f"Running {cid} (prompt {len(p2)} chars)...", end=" ", flush=True)
            res = run_stage2_call(p2, full_model, tag)
            v = res["parsed_verdict"]
            att = res["attestation"]
            susp = v["suspicion"] if v else None
            verdict = v["verdict"] if v else "FAIL"
            print(f"done in {res['latency_s']}s | parsed={res['parse_ok']} | susp={susp} verdict={verdict} | served={att['served_models']}")
            results[short_name].append({
                "case_id": cid,
                "latency_s": res["latency_s"],
                "parse_ok": res["parse_ok"],
                "verdict": v,
                "attestation": att,
            })

    # Summary
    print("\n=== DRY RUN SUMMARY ===")
    for short_name, _ in models:
        recs = results[short_name]
        n_ok = sum(1 for r in recs if r["parse_ok"])
        all_attested = all(r["attestation"]["served_models"] != [] and len(r["attestation"]["fallbacks"]) == 0 for r in recs)
        print(f"{short_name}: {n_ok}/{len(recs)} parsed OK, all attested={all_attested}")


if __name__ == "__main__":
    main()
