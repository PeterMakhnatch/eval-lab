"""D9-PRODUCTION-STAGE1-CHEAP: Production Stage 1 Runner.

Evaluates:
- T1: Teacher rerun on today's production stage-1 code path (gemini-3.8-flash)
- T2: Teacher rerun on stored stage-1 prompt text where recoverable (80 Terminal Wrench cases)
- C1: Cheap arm 1 (gemini-2.5-flash-lite) on today's production stage-1 path
- C2: Cheap arm 2 (gemini-3.1-flash-lite) on today's production stage-1 path
across the 272 covered DEV cases of TraceMonitor-Bench DEV v1.
"""
import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab import autoreview_monitor as am
from evallab.trace_anatomy import LoadedRun, build_anatomy, load_atif_file, load_run

BENCH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
D2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2")
BRIDGE31 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight")
STORED_D9 = Path.home() / "Developer/eval-lab-results/2026-11-02/D9/stored"
TW_DIR = Path.home() / ".local/state/evallab-prod-live/factory/tw-dev-export"
WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/worker.yml"

DURABLE_D9 = Path.home() / "Developer/eval-lab-results/2026-11-02/D9"
RAW_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D9-PRODUCTION-STAGE1-CHEAP/receipts")
SPEND_LOG = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"
APPROVAL_STRING = "Peter 2026-11-02 overnight D9: OpenRouter cap $0.80"

SEED = 20261102
THINKING_LEVEL = "low"
TIMEOUT_SECS = 180
MAX_WORKERS = 4

# Pricing per million tokens (list price)
RATES = {
    "google-antigravity/gemini-3.8-flash": {"input": 0.30, "output": 2.50},
    "google-antigravity/gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "google-antigravity/gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def log_spend(event: dict[str, Any]) -> None:
    try:
        SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SPEND_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"Warning: could not write to spend log: {e}", file=sys.stderr)


def load_manifest() -> dict[str, Any]:
    return {r["case_id"]: r for r in read_jsonl(BENCH / "manifest.jsonl")}


def load_cohort() -> list[dict[str, Any]]:
    return read_jsonl(D2 / "cohort.jsonl")


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
                data = json.loads(text[idx : end + 1])
                return am.parse_verdict(json.dumps(data))
    return None


def extract_attestation(session_dir: Path) -> dict[str, Any]:
    served_models = []
    fallbacks = []
    in_tokens = 0
    out_tokens = 0
    usd = 0.0

    for p in session_dir.rglob("*.jsonl"):
        if p.name.endswith(".lock.os"):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ev_type = ev.get("type")
            if ev_type == "model_call":
                provider = ev.get("provider", "")
                m = ev.get("model", "")
                if provider and m:
                    served_models.append(f"{provider}/{m}")
                elif m:
                    served_models.append(m)
            elif ev_type == "model_change":
                provider = ev.get("provider", "")
                m = ev.get("model", "")
                if provider and m and not m.startswith(f"{provider}/"):
                    served_models.append(f"{provider}/{m}")
                elif m:
                    served_models.append(m)
                if ev.get("resolvedModelIsFallback"):
                    fallbacks.append(str(ev))
            elif ev_type == "message" and ev.get("message", {}).get("role") == "assistant":
                msg = ev["message"]
                p_name = msg.get("provider") or ev.get("provider")
                m_name = msg.get("model") or ev.get("model")
                if p_name and m_name:
                    if not m_name.startswith(f"{p_name}/"):
                        served_models.append(f"{p_name}/{m_name}")
                    else:
                        served_models.append(m_name)
                elif m_name:
                    served_models.append(m_name)
                u = msg.get("usage", {})
                in_tokens += u.get("input", 0)
                out_tokens += u.get("output", 0)
                cost = u.get("cost", {})
                if isinstance(cost, dict):
                    usd += float(cost.get("total", 0.0) or 0.0)
                elif isinstance(cost, (int, float)):
                    usd += float(cost)
            elif ev_type in ("model_fallback", "fallback"):
                fallbacks.append(str(ev))

    return {
        "served_models": sorted(set(served_models)),
        "fallbacks": fallbacks,
        "n_fallbacks": len(fallbacks),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "usd": usd,
    }


def execute_omp_stage1(
    prompt: str,
    model: str,
    tag: str,
    session_root: Path,
) -> dict[str, Any]:
    session_dir = session_root / tag
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir = DURABLE_D9 / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompt_dir / f"{tag}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    cmd = [
        "omp",
        "-p",
        f"@{prompt_file}",
        "--model",
        model,
        "--thinking",
        THINKING_LEVEL,
        "--no-tools",
        "--no-skills",
        "--config",
        WORKER_YML,
        "--session-dir",
        str(session_dir),
        "--no-title",
        "--mode",
        "text",
    ]

    t0 = time.perf_counter()
    env = os.environ.copy()
    env["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"

    proc = subprocess.run(
        cmd,
        cwd=session_dir,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECS,
        env=env,
        check=False,
    )
    latency = time.perf_counter() - t0

    att = extract_attestation(session_dir)
    verdict = parse_verdict_robust(proc.stdout)

    # Check for argon
    has_argon = any("argon" in m.lower() for m in att["served_models"])
    if has_argon:
        raise RuntimeError(f"FATAL: gemini-4-argon was served in {tag}!")

    attested = (
        bool(att["served_models"])
        and att["served_models"] == [model]
        and att["n_fallbacks"] == 0
        and not has_argon
    )

    rates = RATES.get(model, {"input": 0.30, "output": 2.50})
    list_price_usd = (
        att["input_tokens"] * rates["input"] + att["output_tokens"] * rates["output"]
    ) / 1_000_000.0

    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
        "latency_s": round(latency, 3),
        "attestation": att,
        "attested": attested,
        "verdict": verdict,
        "prompt_sha256": prompt_sha,
        "prompt_chars": len(prompt),
        "list_price_usd": round(list_price_usd, 6),
    }


def run_arm(
    arm_name: str,
    model_name: str,
    cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    prompt_source: str = "today",  # "today" or "stored"
    dry_run_limit: int | None = None,
) -> None:
    print("\n=======================================================")
    print(f"RUNNING ARM: {arm_name} ({model_name}, prompt={prompt_source})")
    print("=======================================================")

    durable_out = DURABLE_D9 / f"{arm_name}.jsonl"
    raw_out = RAW_RECEIPTS / f"{arm_name}.jsonl"
    DURABLE_D9.mkdir(parents=True, exist_ok=True)
    RAW_RECEIPTS.mkdir(parents=True, exist_ok=True)

    session_root = DURABLE_D9 / "sessions" / arm_name
    session_root.mkdir(parents=True, exist_ok=True)

    existing_rows = {r["case_id"]: r for r in read_jsonl(durable_out) if r.get("parse_ok")}
    print(f"Already completed {len(existing_rows)} cases for {arm_name}.")

    target_cases = [c for c in cases if c["case_id"] not in existing_rows]
    if prompt_source == "stored":
        # Only cases with stored prompts
        stored_dir = STORED_D9 / "w3ar-g38/prompts"
        target_cases = [
            c for c in target_cases
            if (stored_dir / f"s1-tw-{manifest[c['case_id']]['source_id']}.txt").exists()
        ]
        print(f"Recoverable stored prompt cases: {len(target_cases)} remaining.")

    if dry_run_limit:
        target_cases = target_cases[:dry_run_limit]
        print(f"DRY RUN: Limiting to {len(target_cases)} cases.")

    print(f"Cases to execute: {len(target_cases)}")

    # Pre-build prompts and L0 scan
    run_payloads = []
    for c in target_cases:
        cid = c["case_id"]
        sid = manifest[cid]["source_id"]
        run = load_case_run(cid, manifest)
        anatomy = build_anatomy(run)
        l0_res = am.scan_l0(run)

        if prompt_source == "today":
            view = am.build_compact_view(anatomy, run)
            prompt = am.stage1_prompt(view)
        elif prompt_source == "stored":
            prompt_file = STORED_D9 / "w3ar-g38/prompts" / f"s1-tw-{sid}.txt"
            prompt = prompt_file.read_text(encoding="utf-8")
        else:
            raise ValueError(prompt_source)

        run_payloads.append({
            "case": c,
            "prompt": prompt,
            "l0_res": l0_res,
            "tag": f"{arm_name}-{cid}",
        })

    def worker(item: dict[str, Any]) -> dict[str, Any]:
        c = item["case"]
        cid = c["case_id"]
        tag = item["tag"]
        prompt = item["prompt"]
        l0_res = item["l0_res"]

        res = execute_omp_stage1(prompt, model_name, tag, session_root)
        verd = res["verdict"]

        parse_ok = verd is not None
        susp = int(verd.get("suspicion", 50)) if parse_ok else 50
        v_str = str(verd.get("verdict", "unparsed")) if parse_ok else "unparsed"

        record = {
            "case_id": cid,
            "family": c["family"],
            "split": c["split"],
            "label": c["label"],
            "arm": arm_name,
            "model": model_name,
            "prompt_source": prompt_source,
            "stage": 1,
            "suspicion": susp,
            "verdict": v_str,
            "cited_steps": verd.get("cited_steps", []) if parse_ok else [],
            "causal_grade_effect": verd.get("causal_grade_effect", "") if parse_ok else "unparsed",
            "reason": verd.get("reason", "") if parse_ok else res["stdout"][:200],
            "l0_hit": bool(l0_res.get("has_gate_card")),
            "l0_gate_rules": l0_res.get("gate_rules", []),
            "l0_hit_steps": l0_res.get("hit_steps", []),
            "parse_ok": parse_ok,
            "protocol_failure": not parse_ok or not res["attested"],
            "served_models": res["attestation"]["served_models"],
            "n_fallbacks": res["attestation"]["n_fallbacks"],
            "latency_s": res["latency_s"],
            "prompt_chars": res["prompt_chars"],
            "prompt_sha256": res["prompt_sha256"],
            "input_tokens": res["attestation"]["input_tokens"],
            "output_tokens": res["attestation"]["output_tokens"],
            "cost_usd": res["attestation"]["usd"],
            "list_price_usd": res["list_price_usd"],
            "ts": datetime.now(UTC).isoformat(),
        }

        # Log to spend ledger
        log_spend({
            "ts": record["ts"],
            "job": f"D9:{arm_name}:{cid}",
            "arm": arm_name,
            "model": model_name,
            "in_tokens": record["input_tokens"],
            "out_tokens": record["output_tokens"],
            "cost_usd": record["cost_usd"],
            "list_price_usd": record["list_price_usd"],
            "approval": APPROVAL_STRING,
        })

        return record

    done_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for rec in pool.map(worker, run_payloads):
            done_count += 1
            line = json.dumps(rec) + "\n"
            with open(durable_out, "a", encoding="utf-8") as f_dur, open(raw_out, "a", encoding="utf-8") as f_raw:
                f_dur.write(line)
                f_raw.write(line)
            print(
                f"[{arm_name}] ({done_count}/{len(run_payloads)}) {rec['case_id']} ({rec['family']}, {rec['split']}) -> "
                f"susp={rec['suspicion']} verd={rec['verdict']} attested={rec['served_models']==[model_name]} "
                f"tokens={rec['input_tokens']}+{rec['output_tokens']} in {rec['latency_s']}s"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Production Stage 1 evaluation.")
    parser.add_argument("--arm", choices=["all", "dry-run", "T1", "T2", "C1", "C2"], default="all")
    args = parser.parse_args()

    manifest = load_manifest()
    cohort = load_cohort()
    st_readings = {r["case_id"]: r for r in read_jsonl(BENCH / "readings/ar_g38.jsonl")}
    cov_cases = [c for c in cohort if c["case_id"] in st_readings]

    print(f"Loaded {len(cov_cases)} covered cases.")

    if args.arm == "dry-run":
        print("\n--- RUNNING 5-CASE DRY RUN FOR ATTESTATION & INTEGRITY ---")
        run_arm("dry_run_t1", "google-antigravity/gemini-3.8-flash", cov_cases, manifest, "today", dry_run_limit=2)
        run_arm("dry_run_c1", "google-antigravity/gemini-2.5-flash-lite", cov_cases, manifest, "today", dry_run_limit=2)
        run_arm("dry_run_c2", "google-antigravity/gemini-3.1-flash-lite", cov_cases, manifest, "today", dry_run_limit=1)
        return

    if args.arm in ("all", "T1"):
        run_arm("T1_teacher_today", "google-antigravity/gemini-3.8-flash", cov_cases, manifest, "today")
    if args.arm in ("all", "T2"):
        run_arm("T2_teacher_stored", "google-antigravity/gemini-3.8-flash", cov_cases, manifest, "stored")
    if args.arm in ("all", "C1"):
        run_arm("C1_gemini_25_lite", "google-antigravity/gemini-2.5-flash-lite", cov_cases, manifest, "today")
    if args.arm in ("all", "C2"):
        run_arm("C2_gemini_31_lite", "google-antigravity/gemini-3.1-flash-lite", cov_cases, manifest, "today")


if __name__ == "__main__":
    main()
