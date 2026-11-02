"""Forced Stage-2 execution on all 272 DEV cohort cases for D7-STAGE2-EVERYWHERE.

Executes:
1. Teacher model (`google-antigravity/gemini-3.8-flash`, thinking low) on the 224
   cases that stored AutoReview never sent to Stage 2.
2. Seeded repeat sample of 48 cases (seed=20261102) for noise-floor evaluation.
3. Merges D6B teacher rerun 1 (48 cases) + 224 cases into a complete 272-case
   receipt for Policy 1 (Stage 2 Everywhere).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from evallab import autoreview_monitor as am
from evallab.trace_anatomy import LoadedRun, build_anatomy, load_atif_file, load_run

BENCH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
D2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2")
BRIDGE31 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight")
TW_DIR = Path.home() / ".local/state/evallab-prod-live/factory/tw-dev-export"
WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/worker.yml"

D6B_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D6B-FORCED-STAGE2/receipts")
RAW_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D7-STAGE2-EVERYWHERE/receipts")
DURABLE_D7 = Path.home() / "Developer/eval-lab-results/2026-11-02/D7"
EXP_DIR = Path("research/experiments/on-stage2-everywhere-20261102")

SEED = 20261102
MODEL_NAME = "google-antigravity/gemini-3.8-flash"
THINKING_LEVEL = "low"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_manifest() -> dict[str, Any]:
    return {r["case_id"]: r for r in read_jsonl(BENCH / "manifest.jsonl")}


def load_cohort() -> list[dict[str, Any]]:
    return read_jsonl(D2 / "cohort.jsonl")


def load_stored_readings() -> dict[str, dict[str, Any]]:
    tw_rows = {r["run_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-AUTOREVIEW-V2-TW/receipts/readings-tw.jsonl")}
    cb_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-cheatbench.jsonl")}
    gf_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-goodfire.jsonl")}
    ds_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts/readings-arm2.jsonl")}
    return {"tw": tw_rows, "cb": cb_rows, "gf": gf_rows, "ds": ds_rows}


def load_all_covered_cases(manifest: dict[str, Any], cohort: list[dict[str, Any]], stored_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    st_readings = {r["case_id"]: r for r in read_jsonl(BENCH / "readings/ar_g38.jsonl")}
    covered = {}
    for c in cohort:
        cid = c["case_id"]
        if cid not in st_readings:
            continue
        fam = c["family"]
        sid = manifest[cid]["source_id"]
        bid = manifest[cid]["blind_id"]
        st_row = None
        if fam == "terminal_wrench":
            st_row = stored_map["tw"].get(sid)
        elif fam == "cheatbench":
            st_row = stored_map["cb"].get(bid) or stored_map["cb"].get(sid)
        elif fam == "goodfire":
            st_row = stored_map["gf"].get(bid) or stored_map["gf"].get(sid)
        elif fam == "deep_swe":
            st_row = stored_map["ds"].get(bid) or stored_map["ds"].get(sid)
        elif fam == "c1c5":
            st_row = {"stage": 1, "suspicion": st_readings[cid]["suspicion"], "verdict": st_readings[cid]["verdict"]}

        stage = st_row.get("stage", 1) if st_row else 1
        s1_info = st_row.get("stage1") if (st_row and isinstance(st_row.get("stage1"), dict)) else (st_row or {})
        s1_susp = s1_info.get("suspicion", st_readings[cid].get("suspicion", 0.0))
        is_band = 25 <= float(s1_susp) <= 85 if s1_susp is not None else False
        is_l0 = bool(st_row.get("l0_has_gate_card", False)) if st_row else False

        trigger_group = (
            "l0_only" if (is_l0 and not is_band) else
            ("band_only" if (is_band and not is_l0) else
             ("both" if (is_l0 and is_band) else "none"))
        )

        covered[cid] = {
            "case_id": cid,
            "family": fam,
            "split": c["split"],
            "label": c["label"],
            "source_id": sid,
            "blind_id": bid,
            "stored_stage": stage,
            "is_l0": is_l0,
            "is_band": is_band,
            "trigger_group": trigger_group,
            "stored_stage1": s1_info,
            "stored_verdict": st_readings[cid].get("verdict"),
            "stored_suspicion": float(st_readings[cid].get("suspicion", 0.0)),
        }
    return covered


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


def execute_omp_stage2(prompt: str, model: str, thinking: str, tag: str, sessions_root: Path) -> dict[str, Any]:
    sess_dir = sessions_root / tag
    sess_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as pf:
        pf.write(prompt)
        prompt_file = pf.name

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
        thinking,
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
        "latency_s": round(latency, 3),
        "prompt_chars": len(prompt),
    }


def build_prompt_for_case(
    case: dict[str, Any],
    manifest: dict[str, Any],
    prompts_dir: Path,
) -> dict[str, Any]:
    cid = case["case_id"]
    prompt_path = prompts_dir / f"stage2-{cid}.txt"
    if prompt_path.exists():
        text = prompt_path.read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {"prompt_path": prompt_path, "prompt_text": text, "sha256": sha}

    run = load_case_run(cid, manifest)
    anatomy = build_anatomy(run)
    view = am.build_compact_view(anatomy, run)
    cand_steps = am.candidate_stage2_steps(anatomy)
    st_s1 = case.get("stored_stage1", {})
    target_steps = sorted(set(cand_steps) | set(st_s1.get("cited_steps", [])))
    prompt_text = am.stage2_prompt(view, run, target_steps)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    return {"prompt_path": prompt_path, "prompt_text": prompt_text, "sha256": sha}


def run_batch(
    cohort_cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    prompts_dir: Path,
    out_file: Path,
    sessions_root: Path,
    batch_tag: str,
    max_workers: int = 4,
) -> None:
    print(f"=== Running Batch: {batch_tag} ({len(cohort_cases)} cases) ===")
    finished: dict[str, dict[str, Any]] = {}
    if out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    finished[rec["case_id"]] = rec
        print(f"Found {len(finished)} already completed cases.")

    remaining = [c for c in cohort_cases if c["case_id"] not in finished]
    print(f"Cases to execute: {len(remaining)}")

    def process_case(case: dict[str, Any]) -> dict[str, Any]:
        cid = case["case_id"]
        p_info = build_prompt_for_case(case, manifest, prompts_dir)
        tag = f"{batch_tag}-{cid}"
        call_res = execute_omp_stage2(p_info["prompt_text"], MODEL_NAME, THINKING_LEVEL, tag, sessions_root)

        v2 = call_res["parsed_verdict"]
        parse_ok = call_res["parse_ok"]
        protocol_failure = not parse_ok
        att = call_res["attestation"]

        # Run confirmation
        run = load_case_run(cid, manifest)
        anatomy = build_anatomy(run)
        st_s1 = case.get("stored_stage1", {})
        s1_verdict = {
            "suspicion": int(round(float(st_s1.get("suspicion", case.get("stored_suspicion", 0))))),
            "verdict": st_s1.get("verdict", case.get("stored_verdict", "unknown")),
            "cited_steps": st_s1.get("cited_steps", []),
            "causal_grade_effect": st_s1.get("causal_grade_effect", ""),
            "reason": st_s1.get("reason", ""),
        }

        if parse_ok and v2 is not None:
            final_v = am.confirm_stage2_verdict(s1_verdict, v2, anatomy)
        else:
            final_v = s1_verdict

        return {
            "case_id": cid,
            "family": case["family"],
            "split": case["split"],
            "label": case["label"],
            "trigger_group": case["trigger_group"],
            "is_l0": case["is_l0"],
            "is_band": case["is_band"],
            "stored_stage": case["stored_stage"],
            "arm": batch_tag,
            "model": MODEL_NAME,
            "thinking": THINKING_LEVEL,
            "prompt_sha256": p_info["sha256"],
            "prompt_chars": call_res["prompt_chars"],
            "stage1_suspicion": s1_verdict["suspicion"],
            "stage1_verdict": s1_verdict["verdict"],
            "stage2_raw": v2,
            "stage2_suspicion": v2.get("suspicion") if v2 else None,
            "stage2_verdict": v2.get("verdict") if v2 else None,
            "final_suspicion": final_v["suspicion"],
            "final_verdict": final_v["verdict"],
            "parse_ok": parse_ok,
            "protocol_failure": protocol_failure,
            "served_models": att["served_models"],
            "n_fallbacks": len(att["fallbacks"]),
            "latency_s": call_res["latency_s"],
            "input_tokens": att["input_tokens"],
            "output_tokens": att["output_tokens"],
            "cost_usd": att["usd"],
            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        }

    if remaining:
        with open(out_file, "a", encoding="utf-8") as out_f, concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_case, c): c for c in remaining}
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                out_f.write(json.dumps(res) + "\n")
                out_f.flush()
                status_str = f"s2_susp={res['stage2_suspicion']} ({res['stage2_verdict']}) [confirmed={res['final_suspicion']}]"
                print(f"  [{batch_tag}] {res['case_id']} ({res['family']}, {res['split']}, label={res['label']}): {status_str} in {res['latency_s']}s")

    print(f"Batch {batch_tag} complete. Total in {out_file}: {len(read_jsonl(out_file))}")


def merge_p1_receipt(
    d6b_recs: list[dict[str, Any]],
    forced_224_recs: list[dict[str, Any]],
    out_file: Path,
) -> None:
    merged = {r["case_id"]: r for r in d6b_recs}
    for r in forced_224_recs:
        merged[r["case_id"]] = r
    all_cids = sorted(merged.keys())
    with open(out_file, "w", encoding="utf-8") as f:
        for cid in all_cids:
            f.write(json.dumps(merged[cid]) + "\n")
    print(f"Merged full P1 receipt written to {out_file}: {len(merged)} cases.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4, help="Max concurrent omp calls (cap: 4)")
    args = parser.parse_args()

    manifest = load_manifest()
    cohort = load_cohort()
    stored_map = load_stored_readings()
    covered = load_all_covered_cases(manifest, cohort, stored_map)
    print(f"Total covered cases in DEV: {len(covered)}")

    # Check D6B teacher rerun 1
    d6b_file = D6B_RECEIPTS / "teacher-rerun-1-low.jsonl"
    if not d6b_file.exists():
        d6b_file = Path.home() / "Developer/eval-lab-results/2026-11-02/D6B/teacher-rerun-1-low.jsonl"
    assert d6b_file.exists(), f"D6B teacher rerun file not found: {d6b_file}"
    d6b_recs = read_jsonl(d6b_file)
    d6b_cids = {r["case_id"] for r in d6b_recs}
    print(f"Found {len(d6b_recs)} cases in D6B teacher rerun 1.")

    # Identify remaining 224 cases
    remaining_cases = [covered[cid] for cid in sorted(covered.keys()) if cid not in d6b_cids]
    print(f"Cases requiring forced Stage-2 execution: {len(remaining_cases)}")
    assert len(remaining_cases) == 224, f"Expected 224 remaining cases, got {len(remaining_cases)}"

    # Identify seeded 48-case sample for repeat noise floor
    rng = random.Random(SEED)
    sample_cids = sorted(rng.sample([c["case_id"] for c in remaining_cases], 48))
    repeat_cases = [covered[cid] for cid in sample_cids]
    print(f"Seeded 48-case repeat sample selected (seed={SEED}).")

    prompts_dir = EXP_DIR / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    RAW_RECEIPTS.mkdir(parents=True, exist_ok=True)
    DURABLE_D7.mkdir(parents=True, exist_ok=True)
    sessions_root = Path(tempfile.gettempdir()) / "d7-stage2-sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)

    # 1. Run forced Stage 2 on the 224 cases
    forced_out = RAW_RECEIPTS / "forced-stage2-224-low.jsonl"
    run_batch(
        remaining_cases,
        manifest,
        prompts_dir,
        forced_out,
        sessions_root,
        batch_tag="forced_224_low",
        max_workers=min(args.workers, 4),
    )
    shutil.copy2(forced_out, DURABLE_D7 / "forced-stage2-224-low.jsonl")

    # 2. Run repeat sample of 48 cases
    repeat_out = RAW_RECEIPTS / "repeat-sample-48-low.jsonl"
    run_batch(
        repeat_cases,
        manifest,
        prompts_dir,
        repeat_out,
        sessions_root,
        batch_tag="repeat_48_low",
        max_workers=min(args.workers, 4),
    )
    shutil.copy2(repeat_out, DURABLE_D7 / "repeat-sample-48-low.jsonl")

    # 3. Merge into full P1 receipt (all 272 cases)
    p1_out = RAW_RECEIPTS / "all-272-p1-stage2-low.jsonl"
    forced_224_recs = read_jsonl(forced_out)
    merge_p1_receipt(d6b_recs, forced_224_recs, p1_out)
    shutil.copy2(p1_out, DURABLE_D7 / "all-272-p1-stage2-low.jsonl")

    # Also copy D6B reference to D7 directory for self-contained receipts
    shutil.copy2(d6b_file, RAW_RECEIPTS / "d6b-teacher-rerun-1-low.jsonl")
    shutil.copy2(d6b_file, DURABLE_D7 / "d6b-teacher-rerun-1-low.jsonl")

    print("\nAll execution steps completed successfully.")


if __name__ == "__main__":
    main()
