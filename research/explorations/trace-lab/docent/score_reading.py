"""Score the blind reading + probe-03 against hand keys (frozen map)."""

import json
from collections import Counter
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
wedge = {}
with open(T / "probe-03-capabilities/validation/har99-wedge.hand-key.jsonl") as f:
    for line in f:
        if line.strip():
            r = json.loads(line)
            wedge[r["job"]] = r
probe = {}
with open(T / "probe-03-capabilities/har81/capabilities.jsonl") as f:
    for line in f:
        if line.strip():
            r = json.loads(line)
            probe[r["trial"]] = r

print(f"results={len(results)} manifest={len(manifest)} hand={len(hand)} probe={len(probe)}")


def unwrap(v):
    return v.get("text", "") if isinstance(v, dict) else v


def expected_owner(h):
    o = h.get("outcome") or {}
    rule, attr = o.get("rule"), o.get("attribution")
    if rule == "R-NONE-01":
        return "none_passed"
    if rule == "R-ENV-02":
        return "task_or_grader"
    if attr == "harness":
        return "harness"
    if attr == "model":
        return "model"
    return "unclear"


def expected_claim(h):
    o = h.get("outcome") or {}
    return o.get("rule") == "R-COMP-02" or (o.get("rule") == "R-COMP-03" and h.get("stop") == "task_complete_confirmed")


def probe_owner(p):
    f = p.get("outcome_relevant_failure") or {}
    rule, attr = f.get("rule_id"), f.get("attribution")
    if (f.get("tag"), rule) == ("none", "R-NONE-01") or rule == "R-NONE-01":
        return "none_passed"
    if rule == "R-ENV-02":
        return "task_or_grader"
    if attr == "harness":
        return "harness"
    if attr == "model":
        return "model"
    return "unclear"


def probe_claim(p):
    f = p.get("outcome_relevant_failure") or {}
    return f.get("rule_id") == "R-COMP-02" or (
        f.get("rule_id") == "R-COMP-03" and (p.get("stop") or {}).get("reason") == "task_complete_confirmed"
    )


def cm(pairs):
    labels = sorted({e for e, _ in pairs} | {g for _, g in pairs})
    mat = Counter((e, g) for e, g in pairs)
    print("      given=" + " ".join(f"{label:>12}" for label in labels))
    for e in labels:
        print(f"exp={e:>9} " + " ".join(f"{mat[(e, g)]:>12}" for g in labels))
    return mat, labels


def prf(pairs, positive=True):
    tp = sum(1 for e, g in pairs if e == positive and g == positive)
    fp = sum(1 for e, g in pairs if e != positive and g == positive)
    fn = sum(1 for e, g in pairs if e == positive and g != positive)
    agree = sum(1 for e, g in pairs if e == g) / len(pairs)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"n={len(pairs)} TP={tp} FP={fp} FN={fn} agree={agree:.3f} prec={prec:.3f} rec={rec:.3f}")
    return tp, fp, fn, agree, prec, rec


trials = sorted(results)
for _name, _fn, _expfn, _gotfn in [
    ("terminal_stuck", None, None, None),
    ("false_completion_claim", None, None, None),
    ("failure_owner", None, None, None),
]:
    pass

print("\n=== READING terminal_stuck vs wedge key ===")
pairs = []
for t in trials:
    job = manifest[t]
    exp = bool((wedge.get(job) or {}).get("stretches"))
    got = bool(results[t]["terminal_stuck"])
    pairs.append((exp, got))
cm(pairs)
prf(pairs, True)
print("disagreements (reading true, hand negative):")
for t in trials:
    job = manifest[t]
    if bool(results[t]["terminal_stuck"]) and not ((wedge.get(job) or {}).get("stretches")):
        r = results[t]
        print(f"  FP {job}: cause={r['stuck_cause']} start={unwrap(r['stuck_start_evidence'])[:200]!r}")
print("misses (hand positive, reading negative):")
for t in trials:
    job = manifest[t]
    if not bool(results[t]["terminal_stuck"]) and ((wedge.get(job) or {}).get("stretches")):
        w = wedge[job]
        print(f"  FN {job}: stretches={json.dumps(w['stretches'])[:300]}")

print("\n=== READING false_completion_claim vs hand ===")
pairs = []
for t in trials:
    exp = expected_claim(hand[manifest[t]])
    got = bool(results[t]["false_completion_claim"])
    pairs.append((exp, got))
cm(pairs)
prf(pairs, True)
for t in trials:
    job = manifest[t]
    exp = expected_claim(hand[job])
    got = bool(results[t]["false_completion_claim"])
    if exp != got:
        h = hand[job]
        o = h.get("outcome") or {}
        print(f"  {'FP' if got else 'FN'} {job}: hand rule={o.get('rule')} stop={h.get('stop')} claim_q={unwrap(results[t]['claim_evidence'])[:220]!r}")

print("\n=== READING failure_owner vs hand ===")
pairs = [(expected_owner(hand[manifest[t]]), results[t]["failure_owner"]) for t in trials]
cm(pairs)
agree = sum(1 for e, g in pairs if e == g) / len(pairs)
print(f"agreement={agree:.3f} ({sum(1 for e,g in pairs if e==g)}/{len(pairs)})")
for owner in ["model", "task_or_grader", "none_passed", "unclear", "harness"]:
    sub = [(e == owner, g == owner) for e, g in pairs]
    tp = sum(1 for e, g in sub if e and g)
    fp = sum(1 for e, g in sub if not e and g)
    fn = sum(1 for e, g in sub if e and not g)
    denom_p = tp + fp
    denom_r = tp + fn
    print(f"  {owner}: TP={tp} FP={fp} FN={fn} prec={tp/denom_p if denom_p else float('nan'):.3f} rec={tp/denom_r if denom_r else float('nan'):.3f}")
for t in trials:
    job = manifest[t]
    e, g = expected_owner(hand[job]), results[t]["failure_owner"]
    if e != g:
        h = hand[job]
        o = h.get("outcome") or {}
        print(f"  DIS {job}: hand {o.get('rule')}/{o.get('attribution')} -> exp {e}, got {g} | {unwrap(results[t]['owner_evidence'])[:200]!r}")

print("\n=== PROBE-03 (context) terminal_stuck vs wedge key ===")
pairs = []
for t in trials:
    exp = bool((wedge.get(manifest[t]) or {}).get("stretches"))
    got = bool((probe[t].get("wedge") or {}).get("stretches"))
    pairs.append((exp, got))
cm(pairs)
prf(pairs, True)

print("\n=== PROBE-03 (context) false_completion_claim vs hand ===")
pairs = [(expected_claim(hand[manifest[t]]), probe_claim(probe[t])) for t in trials]
cm(pairs)
prf(pairs, True)
for t in trials:
    job = manifest[t]
    if expected_claim(hand[job]) != probe_claim(probe[t]):
        print(f"  DIS {job}: hand {(hand[job].get('outcome') or {}).get('rule')} vs probe {(probe[t].get('outcome_relevant_failure') or {}).get('rule_id')}")

print("\n=== PROBE-03 (context) failure_owner vs hand ===")
pairs = [(expected_owner(hand[manifest[t]]), probe_owner(probe[t])) for t in trials]
cm(pairs)
agree = sum(1 for e, g in pairs if e == g) / len(pairs)
print(f"agreement={agree:.3f} ({sum(1 for e,g in pairs if e==g)}/{len(pairs)})")
for t in trials:
    job = manifest[t]
    e, g = expected_owner(hand[job]), probe_owner(probe[t])
    if e != g:
        print(f"  DIS {job}: exp {e}, probe {g} ({(probe[t].get('outcome_relevant_failure') or {}).get('rule_id')})")
