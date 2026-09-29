"""Window-score first_mistake via citation block_idx -> step map (step ~= B//2+1)."""

import json
import re
from pathlib import Path

T = Path(__file__).resolve().parent.parent
D = T / "docent"
DATA = Path.home() / "Developer" / "eval-lab" / "derived" / "trace-lab"
with open(D / "har81_reading_results.json") as f:
    results = {r["trial"]: r["output"] for r in json.load(f)}
with open(DATA / "normalized/har81/manifest.json") as f:
    manifest = {t["trial"]: t["job"] for t in json.load(f)["trials"]}
hand = {}
for fn in ["har81-wave-a", "har81-holdout", "har81-holdout2", "har81-late"]:
    with open(T / f"probe-03-capabilities/validation/{fn}.hand-key.jsonl") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                hand[r["job"]] = r

traj_cache = {}


def steps(trial):
    if trial not in traj_cache:
        job = manifest[trial]
        p = DATA / f"normalized/har81/{job}/{trial}/agent/trajectory.json"
        with open(p) as f:
            traj_cache[trial] = json.load(f)["steps"]
    return traj_cache[trial]


def ref_of(s):
    return ((s.get("extra") or {}).get("trace_lab") or {}).get("ref", "")


def hand_numbers(job, trial):
    h = hand[job]
    refs = []
    o = h.get("outcome") or {}
    if o.get("step"):
        refs.append(o["step"])
    ff = h.get("first_failure") or {}
    if ff.get("step"):
        refs.append(ff["step"])
    nums = set()
    byref = {ref_of(s): s.get("step_id") for s in steps(trial)}
    for r in refs:
        if r in byref and byref[r] is not None:
            nums.add(byref[r])
        elif (m := re.search(r"#(\d+)$", r)):
            nums.add(int(m.group(1)))
    return refs, sorted(nums)


def unwrap(v):
    return v.get("text", "") if isinstance(v, dict) else v


def blocks(v):
    cs = v.get("citations", []) if isinstance(v, dict) else []
    return sorted({c.get("target", {}).get("item", {}).get("block_idx") for c in cs if isinstance(c, dict) and c.get("target", {}).get("item", {}).get("block_idx") is not None})


inwin = out = 0
for trial in sorted(results):
    job = manifest[trial]
    fm = results[trial]["first_mistake_evidence"]
    bs = blocks(fm)
    est = sorted({b // 2 + 1 for b in bs})
    refs, nums = hand_numbers(job, trial)
    if est and nums:
        d = min(abs(a - b) for a in est for b in nums)
        verdict = "IN" if d <= 5 else "OUT"
    else:
        verdict = "NOCOMP"
    if verdict == "IN":
        inwin += 1
    else:
        out += 1
    h = hand[job]
    o = h.get("outcome") or {}
    if verdict != "IN":
        print(f"{verdict} {job} (hand {o.get('rule')} refs={refs}): blocks={bs} est_steps={est} hand_steps={nums}")
        print(f"    quote: {unwrap(fm)[:240]!r}")
print(f"\nIN-window={inwin}/44 (window +/-5 agent steps; block->step map verified on 1 anchor, +/-1 jitter)")
