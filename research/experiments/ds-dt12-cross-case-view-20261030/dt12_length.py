#!/usr/bin/env python3
"""DT-12 length control: cluster-level DT-8 alerts by run-length tertile."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad")
sys.path.insert(0, str(Path(__file__).parent))
from dt12_dup import components, cosine, fingerprint, load_units, tfidf  # noqa: E402,I001
from stats import tertiles, wilson  # noqa: E402,I001

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
PROMPTS = Path("/tmp/dt8/prompts")


def fmt(k, n):
    if not n:
        return f"{k}/{n}"
    lo, hi = wilson(k, n)
    return f"{k}/{n}={100 * k / n:.1f}%({100 * lo:.1f}-{100 * hi:.1f})"


def main():
    units = load_units()
    length = {}
    for p in ("DT-8/receipts/readings-new.jsonl", "DT-2/receipts/readings.jsonl"):
        for line in (RAW / p).open():
            r = json.loads(line)
            length[r["blind_id"]] = r.get("length")
    rows = []
    for b, u in units.items():
        fp, toks, _ = fingerprint((PROMPTS / f"{b}.txt").read_text(errors="replace"), "full")
        rows.append({**u, "fp": fp, "toks": toks, "length": length.get(b)})
    ids = [r["blind_id"] for r in rows]
    by = {r["blind_id"]: r for r in rows}
    vecs = dict(zip(ids, tfidf([by[i]["toks"] for i in ids]), strict=True))
    sim = {(a, b): cosine(vecs[a], vecs[b]) for x, a in enumerate(ids) for b in ids[x + 1:]}

    print("corpus stratum len_lo len_hi | run-level pos | run-level neg | cluster0.80 pos | cluster0.80 neg")
    for corpus in ("goodfire", "cheatbench"):
        sub = [i for i in ids if by[i]["corpus"] == corpus and by[i]["status"] == "ok"]
        cut = tertiles([by[i]["length"] for i in sub])
        groups = components(sub, {k: v for k, v in sim.items() if k[0] in sub and k[1] in sub}, 0.80)
        for s in (0, 1, 2):
            if s == 0:
                mem = [i for i in sub if by[i]["length"] < cut[0]]
            elif s == 1:
                mem = [i for i in sub if cut[0] <= by[i]["length"] < cut[1]]
            else:
                mem = [i for i in sub if by[i]["length"] >= cut[1]]
            lo = min(by[i]["length"] for i in mem)
            hi = max(by[i]["length"] for i in mem)
            pr = sum(1 for i in mem if by[i]["positive"] and by[i]["alert"])
            pn = sum(1 for i in mem if by[i]["positive"])
            nr = sum(1 for i in mem if not by[i]["positive"] and by[i]["alert"])
            nn = sum(1 for i in mem if not by[i]["positive"])
            reps = []
            for g in groups:
                members = sorted([i for i in g if i in mem])
                if not members:
                    continue
                reps.append({"pos": any(by[i]["positive"] for i in g),
                             "alert": by[members[0]]["alert"]})
            cp = sum(1 for x in reps if x["pos"] and x["alert"])
            cpn = sum(1 for x in reps if x["pos"])
            cn = sum(1 for x in reps if not x["pos"] and x["alert"])
            cnn = sum(1 for x in reps if not x["pos"])
            print(f"{corpus} {s} {lo} {hi} | {fmt(pr, pn)} | {fmt(nr, nn)} | "
                  f"{fmt(cp, cpn)} | {fmt(cn, cnn)}")
    print("cuts", {c: tertiles([by[i]["length"] for i in ids
                                if by[i]["corpus"] == c and by[i]["status"] == "ok"])
                   for c in ("goodfire", "cheatbench")})


if __name__ == "__main__":
    main()
