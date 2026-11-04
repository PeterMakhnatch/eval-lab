#!/usr/bin/env python3
"""Export only currently admitted, independently certified DEV labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from admission import (  # noqa: E402
    CONTRACT_VERSION,
    RUNS,
    admitted_runs,
    analysis_admission,
    object_digest,
)

SET = "impossible-v1"


_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_id(native_id: str) -> str:
    cleaned = _SAFE.sub("_", native_id)
    if cleaned == native_id and len(cleaned) <= 120:
        return cleaned
    return f"{cleaned[:100]}__{hashlib.sha256(native_id.encode()).hexdigest()[:12]}"


def view_for(run: dict[str, Any], rdir: Path) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    decision = analysis_admission(rdir)
    if not decision["analysis_eligible"] or decision["label"] is None:
        raise ValueError("view requires current admission and semantic certification")
    if run != decision["run"]:
        raise ValueError("run metadata changed after admission")
    traj = json.loads((rdir / "trajectory.json").read_text())
    prompt = (rdir / "prompt.md").read_text(encoding="utf-8", errors="replace")
    steps_full = []
    steps_actions = []
    n_chars = 0
    for idx, s in enumerate(traj["steps"]):
        obs = "\n".join(r.get("content", "") for r in s["observation"]["results"] if r.get("content"))
        obs_val = obs or None
        msg = s.get("message") or ""
        reasoning = s.get("reasoning_content") or ""
        if reasoning:
            text_val: str | None = f"<reasoning>\n{reasoning}\n</reasoning>\n{msg}"
        else:
            text_val = msg or None
        calls = [
            {
                "name": str(c.get("function_name") or ""),
                "args": c["arguments"] if isinstance(c.get("arguments"), str) else json.dumps(c.get("arguments") or {}, ensure_ascii=False, sort_keys=True),
            }
            for c in (s.get("tool_calls") or [])
        ]
        n_chars += len(text_val or "") + len(obs_val or "") + sum(len(c["name"]) + len(c["args"]) for c in calls)
        steps_full.append({"i": idx, "role": "agent", "text": text_val, "tool_calls": calls, "observation": obs_val})
        steps_actions.append({"i": idx, "role": "agent", "text": None, "tool_calls": calls, "observation": obs_val})
    diff = (rdir / "final.diff").read_text(errors="replace") if (rdir / "final.diff").exists() else None
    run_key = f"{SET}:{run['id']}"
    full = {"run_key": run_key, "task_text": prompt, "steps": steps_full, "final": {"reward": None, "diff": diff}}
    acts = {"run_key": run_key, "task_text": None, "steps": steps_actions, "final": {"reward": None, "diff": diff}}
    return full, acts, len(steps_full), n_chars


def main() -> None:
    from datetime import UTC, datetime

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/private/tmp/ov1104/impossible/detbench-set")
    args = ap.parse_args()
    out = Path(args.out)
    generation = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    generation_dir = out / "generations" / generation
    generation_dir.mkdir(parents=True, exist_ok=False)
    decisions = admitted_runs()
    rows, excluded, extended = [], [], []
    for decision in decisions:
        # Do not open or copy excluded bodies, even for extended output.
        extended.append({k: v for k, v in decision.items() if k != "run"})
        if not decision["analysis_eligible"] or decision["label"] is None:
            excluded.append({k: v for k, v in decision.items() if k != "run"})
            continue
        run = decision["run"]
        full, acts, n_steps, n_chars = view_for(run, RUNS / decision["run_id"])
        stem = safe_id(run["id"])
        views = []
        for name, value in (("full", full), ("actions_only", acts)):
            directory = generation_dir / "views" / name
            directory.mkdir(exist_ok=True)
            path = directory / f"{stem}.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            views.append(str(path))
        rows.append({
            "run_key": f"{SET}:{run['id']}", "set": SET, "split": "dev",
            "label": decision["label"], "label_type": "executed", "label_basis": decision["label_basis"],
            "natural": None, "cluster": run["task_id"],
            "source_family": "mimo-v2.6-rl-python/omp-headless-" + run["model_slug"],
            "view": views[0], "view_actions_only": views[1],
            "n_steps": n_steps, "n_chars": n_chars, "read_by": [],
            "generation": generation, "admission_sha256": decision["admission_sha256"],
        })
    current = admitted_runs()
    if [d["admission_sha256"] for d in current] != [d["admission_sha256"] for d in decisions]:
        raise ValueError("admission changed during export; generation is not published")
    manifest = {
        "generation": generation, "contract_version": CONTRACT_VERSION,
        "available": bool(rows), "rows": len(rows),
        "corpus_sha256": object_digest(extended),
        "notice": "Only this generation is active. Historical views and rows remain revoked evidence.",
    }
    for name in ("README.md", "manifest.json"):
        active = out / name
        if active.is_file():
            (generation_dir / ("previous-" + name)).write_bytes(active.read_bytes())
    for name, values in (("runs.jsonl", rows), ("excluded.jsonl", excluded), ("runs-extended.jsonl", extended)):
        data = "".join(json.dumps(row) + "\n" for row in values)
        (generation_dir / name).write_text(data, encoding="utf-8")
        # Preserve existing derived bytes before replacing active pointers.
        active = out / name
        if active.exists():
            (generation_dir / ("previous-" + name)).write_bytes(active.read_bytes())
        temp = out / (name + ".pending")
        temp.write_text(data, encoding="utf-8")
        temp.replace(active)
    (generation_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "README.md").write_text(
        f"# {SET}: admitted generation {generation}\n\n"
        f"Available: {bool(rows)}. Admitted labels: {len(rows)}.\n"
        "No labels means unavailable results, not a 0% rate. "
        "A positive requires independent conflict certification. "
        "A negative is operational evidence for the original specification, not universal honesty. "
        "Historical views are revoked and must not be scored directly.\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
