#!/usr/bin/env python3
"""DT-12 family naming: one deepseek/deepseek-flash call per selected cluster.

Selection (fixed now, after clustering, before any call): the 20 largest clusters of
each set, ties broken by cluster index. 40 calls total, at most 4 concurrent.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
DT12 = RAW / "DT-12"
OUT = DT12 / "receipts"
WORK = Path("/tmp/dt12")
MODEL = "deepseek/deepseek-flash"
WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/worker.yml"

INSTRUCTION = (
    "Name the shared mechanism family of the agent-run evidence excerpts above in at "
    "most 8 words. Reply with the name only: no punctuation, no explanation, no quotes."
)


def served(session_dir: Path) -> dict:
    requested, fallbacks = [], []
    for path in sorted(session_dir.rglob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "model_change":
                requested.append(e.get("model"))
                if e.get("resolvedModelIsFallback"):
                    fallbacks.append(e)
            elif e.get("type") in ("model_fallback", "fallback"):
                fallbacks.append(e)
    models = sorted({str(m) for m in requested})
    return {"served_models": models, "n_model_changes": len(requested),
            "n_fallbacks": len(fallbacks),
            "attested": bool(models) and models == [MODEL] and not fallbacks}


def prompt_for(cluster: dict) -> str:
    parts = []
    for k, ex in enumerate(cluster["exemplars"], 1):
        parts.append(f"[{k}] {' '.join(ex.split())[:1200]}")
    return "<excerpts>\n" + "\n\n".join(parts) + "\n</excerpts>\n\n" + INSTRUCTION + "\n"


def main():
    selected = []
    for name in ("quotes", "seeker"):
        data = json.loads((OUT / f"families-{name}.json").read_text())
        clusters = sorted(data["clusters"], key=lambda c: (-c["size"], c["cluster"]))[:20]
        for c in clusters:
            selected.append({"set": name, **c})
    (WORK / "prompts").mkdir(parents=True, exist_ok=True)
    (WORK / "sessions").mkdir(parents=True, exist_ok=True)
    (WORK / "names").mkdir(parents=True, exist_ok=True)
    jobs = []
    for c in selected:
        key = f"{c['set']}-{c['cluster']}"
        p = WORK / "prompts" / f"{key}.txt"
        p.write_text(prompt_for(c))
        jobs.append((key, p, c))
    print(f"{len(jobs)} naming calls", flush=True)

    def one(job):
        key, p, c = job
        sdir = WORK / "sessions" / key
        sdir.mkdir(parents=True, exist_ok=True)
        cmd = ["omp", "-p", f"@{p}", "--model", MODEL, "--config", WORKER_YML,
               "--no-tools", "--thinking", "low", "--session-dir", str(sdir),
               "--no-title", "--mode", "text"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                                  cwd=str(WORK))
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            stdout, stderr, rc = "", f"timeout {e}", -9
        name = " ".join(stdout.split())
        name = re.sub(r"^[\"'`*]+|[\"'`*.]+$", "", name)
        (WORK / "names" / f"{key}.json").write_text(json.dumps({
            "key": key, "set": c["set"], "cluster": c["cluster"], "size": c["size"],
            "name": name[:120], "returncode": rc, "stderr": stderr[-2000:],
            "served": served(sdir), "prompt_sha256": __import__("hashlib").sha256(
                p.read_bytes()).hexdigest()}, indent=1))
        return key, name

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, name in pool.map(one, jobs):
            print(f"{key} -> {name[:70]}", flush=True)


if __name__ == "__main__":
    main()
