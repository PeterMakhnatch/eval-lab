"""Map reading first_mistake quotes to normalized steps; window-score vs hand."""

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
    """Hand-cited ref strings -> normalized step_ids."""
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
            continue
        m = re.search(r"#(\d+)$", r)
        if m:
            nums.add(int(m.group(1)))
    return refs, sorted(nums)


def unwrap(v):
    return v.get("text", "") if isinstance(v, dict) else v


def longest_quote(ev):
    spans = re.findall(r"<RANGE>(.*?)</RANGE>", ev, re.S)
    spans = [s.strip() for s in spans if len(s.strip()) >= 20]
    spans.sort(key=len, reverse=True)
    return spans[:3]


inwin = outwin = unmapped = 0
rows = []
for trial in sorted(results):
    job = manifest[trial]
    ev = unwrap(results[trial]["first_mistake_evidence"])
    refs, nums = hand_numbers(job, trial)
    hits = set()
    detail = []
    for q in longest_quote(ev):
        probe = re.sub(r"\s+", " ", q[:120])
        for s in steps(trial):
            blob = re.sub(r"\s+", " ", json.dumps(s.get("message", ""))[:6000] + " " + json.dumps((s.get("observation") or {}).get("results", ""))[:2000])
            if probe[:80] in blob:
                hits.add(s.get("step_id"))
                detail.append((s.get("step_id"), ref_of(s)))
                break
    if hits and nums:
        d = min(abs(a - b) for a in hits for b in nums)
        verdict = "IN" if d <= 5 else "OUT"
        if verdict == "IN":
            inwin += 1
        else:
            outwin += 1
    elif not hits:
        verdict = "UNMAPPED"
        unmapped += 1
    else:
        verdict = "NO-HAND-REF"
        outwin += 1
    rows.append((verdict, trial, job, sorted(hits)[:4], nums, refs))

print(f"IN-window={inwin} OUT={outwin} UNMAPPED={unmapped} n={len(rows)}")
for verdict, trial, job, hits, _nums, refs in rows:
    if verdict != "IN":
        h = hand[job]
        o = h.get("outcome") or {}
        print(f"{verdict} {job}: reading steps={hits} hand refs={refs} (rule {o.get('rule')})")
        print(f"    quote: {unwrap(results[trial]['first_mistake_evidence'])[:260]!r}")
