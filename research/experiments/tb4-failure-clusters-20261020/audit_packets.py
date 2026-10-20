"""Blind audit packets: does a correct solution risk failing on an unstated rule?

Audited tasks: every scored task PM lists no ambiguity for (the candidates), plus a seeded
sample of PM-listed ambiguity tasks as a sensitivity control. Packets carry the task's own
files and leaderboard failure evidence, never the key, PM text or the flag that selected
the task. Task ids are opaque in the packet directory names and the batch order is shuffled.

Run after ``run.py --phase score`` (needs results.json and features-full.json).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import run  # sibling module: labels(), RESULTS, SEED

HERE = Path(__file__).resolve().parent
TASKS = Path.home() / "Developer/eval-lab/derived/kev/raw/terminal-bench-v4.0.0/tasks"
AUDIT = run.RESULTS / "audit"
CONTROLS = 10
BATCHES = 4
TOP_TESTS = 3
EXCERPTS = 3
MAX_FILE = 200_000
SKIP_TOP = {"README.md", "cheat"}


def aid(task: str) -> str:
    return "a-" + hashlib.sha256(f"tb4-audit:{run.SEED}:{task}".encode()).hexdigest()[:8]


def copy_text_tree(src: Path, dest: Path, omitted: list[str]) -> None:
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if path.stat().st_size > MAX_FILE:
            omitted.append(f"{rel} ({path.stat().st_size} bytes)")
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            omitted.append(f"{rel} (binary)")
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def failure_block(stdout: str, test: str) -> str:
    short = test.split("::")[-1]
    match = re.search(rf"^_+ .*{re.escape(short)}.* _+$", stdout, re.M)
    if not match:
        return "(no failure block found in verifier output)"
    return stdout[match.start(): match.start() + 1800]


def failure_evidence(task: str, trials: list[dict[str, Any]]) -> str:
    """Public-leaderboard failure evidence for one task, as markdown."""
    fails = [t for t in trials if t["task"] == task and t["reward"] == 0 and t["failed"]]
    counts = Counter(name for t in fails for name in t["failed"])
    passes = [t for t in trials if t["task"] == task and t["reward"] is not None]
    rate = sum(t["reward"] >= 1 for t in passes) / max(1, len(passes))
    lines = ["# Failure evidence (public leaderboard trials)", "",
             f"- scored trials: {len(passes)}; pass rate {rate:.0%}; failed trials with per-test evidence: {len(fails)}",
             f"- distinct leaderboard rows with at least one failed trial: {len({t['row_id'] for t in fails})} of 35",
             "", "## Most common failed tests", ""]
    for test, n in counts.most_common(TOP_TESTS):
        rows = len({t["row_id"] for t in fails if test in t["failed"]})
        lines += [f"### `{test}`: failed in {n} of {len(fails)} failed trials, {rows} rows", ""]
        rng = random.Random(f"{run.SEED}:{task}:{test}")
        sample = rng.sample([t for t in fails if test in t["failed"]], min(EXCERPTS, n))
        for t in sample:
            stdout_path = run.RESULTS / "trials-lite" / task / t["trial_name"] / "verifier/test-stdout.txt"
            text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
            lines += [f"Trial sample (failed tests in that trial: {len(t['failed'])}):", "```",
                      failure_block(text, test), "```", ""]
    return "\n".join(lines)


def packet(task: str, feature: dict[str, Any], trials: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    omitted: list[str] = []
    src = TASKS / task
    shutil.copy2(src / "instruction.md", out / "instruction.md")
    shutil.copy2(src / "task.toml", out / "task.toml")
    for name in ("tests", "solution", "environment"):
        if (src / name).is_dir():
            copy_text_tree(src / name, out / name, omitted)
    (out / "failure-evidence.md").write_text(failure_evidence(task, trials), encoding="utf-8")
    (out / "OMITTED.txt").write_text("\n".join(omitted) + ("\n" if omitted else ""), encoding="utf-8")
    return {"aid": aid(task), "files_omitted": len(omitted)}


def main() -> None:
    results = json.loads((HERE / "results.json").read_text())
    labelled = run.labels()
    inventory = json.loads((run.RESULTS / "inventory-all.json").read_text())
    trials = run.load_trials(inventory)
    scored = [t for t, f in results["features"].items() if f["informative"] and labelled[t]["reviewed"]]
    candidates = sorted(t for t in scored if not labelled[t]["pm_ambiguity"])
    positives = sorted(t for t in scored if labelled[t]["pm_ambiguity"])
    controls = random.Random(run.SEED).sample(positives, CONTROLS)
    audited = sorted(candidates + controls, key=lambda t: aid(t))
    random.Random(run.SEED).shuffle(audited)
    if (AUDIT / "packets").exists():  # never touch verdict files
        shutil.rmtree(AUDIT / "packets")
    AUDIT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, task in enumerate(audited):
        packet(task, results["features"][task], trials, AUDIT / "packets" / aid(task))
        manifest.append({"aid": aid(task), "batch": i % BATCHES, "task": task,
                         "role": "candidate" if task in candidates else "control_pm_listed"})
    for b in range(BATCHES):
        ids = [m["aid"] for m in manifest if m["batch"] == b]
        (AUDIT / f"batch-{b}.json").write_text(json.dumps(ids) + "\n")
    # independent second pass over the same packets: fresh auditors, different order and split
    again = [m["aid"] for m in manifest]
    random.Random(run.SEED + 1).shuffle(again)
    for b in range(2):
        (AUDIT / f"rerun-batch-{b}.json").write_text(json.dumps(again[b::2]) + "\n")
    # the private join stays out of the packets directory
    (AUDIT / "private-manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(len(candidates), "candidates +", len(controls), "controls =", len(audited), "packets;",
          Counter(m["batch"] for m in manifest))


if __name__ == "__main__":
    main()
