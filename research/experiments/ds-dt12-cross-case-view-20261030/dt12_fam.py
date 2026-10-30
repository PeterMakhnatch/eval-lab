#!/usr/bin/env python3
"""DT-12 mechanism families: TF-IDF average-linkage clustering of (a) verified quotes
and (b) v1 seeker candidate scripts; naming by one deepseek call per cluster.

Subcommands: cluster | prompt | name | assemble
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
DT12 = RAW / "DT-12"
OUT = DT12 / "receipts"
WORK = Path("/tmp/dt12")
MODEL = "deepseek/deepseek-flash"
WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/worker.yml"

STOP = set("""a an the this that these those to of in on at for from with and or but if then
than is are was were be been being it its as by not no do does did has have had can could
would should may might will shall we you i he she they them their our your my me so such
there here what which who whom when where why how all any both each few more most other
some only own same very s t just now also into over under again further once about after
before above below up down out off then once""".split())

TOKEN = re.compile(r"[a-z_]{2,}|\d+")


def tokens(text: str) -> list[str]:
    toks = []
    for t in TOKEN.findall(text.lower()):
        if t.isdigit():
            toks.append("<n>")
        elif t in STOP:
            continue
        else:
            toks.append(t)
    return toks


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_quotes():
    units = []
    for line in (RAW / "DT-8/receipts/readings-new.jsonl").open():
        r = json.loads(line)
        for i, q in enumerate(r.get("evidence") or []):
            if q.get("verified") and (q.get("quote") or "").strip():
                units.append({"id": f"Q8:{r['blind_id']}:{i}", "source": "DT-8",
                              "corpus": r["corpus"], "label": bool(r["positive"]),
                              "text": q["quote"].strip(), "kind": q.get("kind")})
    for line in (RAW / "DT-2/receipts/readings.jsonl").open():
        r = json.loads(line)
        for i, q in enumerate(r.get("evidence") or []):
            if q.get("verified") and (q.get("quote") or "").strip():
                units.append({"id": f"Q2:{r['blind_id']}:{i}", "source": "DT-2",
                              "corpus": "goodfire", "label": bool(r["enacted"]),
                              "text": q["quote"].strip(), "kind": q.get("kind")})
    return units


def load_seeker():
    meta = {}
    for line in (RAW / "DA-3b/receipts/seeker-attempts-v1.jsonl").open():
        r = json.loads(line)
        meta[r["attempt_id"]] = r
    bodies = {}
    for line in (RAW / "DA-3b/receipts/seeker-attempts-v1.text.jsonl").open():
        r = json.loads(line)
        bodies[r["attempt_id"]] = r["text"]
    units = []
    for aid, r in meta.items():
        text = bodies.get(aid) or ""
        if r.get("answer_status") != "ok" or not text.strip():
            continue
        units.append({"id": f"S:{aid}", "source": "seeker", "text": text,
                      "classes": r.get("classes") or [], "regime": r.get("regime"),
                      "task_id": r.get("task_id"), "n_chars": len(text)})
    return units, meta, bodies


# --------------------------------------------------------------------------- #
# tf-idf + average linkage
# --------------------------------------------------------------------------- #


def tfidf(docs):
    n = len(docs)
    df = Counter()
    for d in docs:
        df.update(set(d))
    idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}
    vecs = []
    for d in docs:
        tf = Counter(d)
        v = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})
    return vecs


def cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def average_linkage(vecs, labels):
    """Lance-Williams average linkage on cosine distance; returns merge order."""
    n = len(vecs)
    d = {}
    for i in range(n):
        for j in range(i + 1, n):
            d[(i, j)] = 1.0 - cos(vecs[i], vecs[j])
    clusters = {i: [i] for i in range(n)}
    merges = []
    next_id = n
    while len(clusters) > 1:
        best = min(d.items(), key=lambda kv: kv[1])
        (a, b), dist = best
        members = clusters[a] + clusters[b]
        merges.append((a, b, dist, len(members)))
        for k in list(clusters):
            if k in (a, b):
                continue
            da = d.get((min(a, k), max(a, k)))
            db = d.get((min(b, k), max(b, k)))
            na, nb = len(clusters[a]), len(clusters[b])
            if da is None or db is None:
                continue
            d[(min(next_id, k), max(next_id, k))] = (na * da + nb * db) / (na + nb)
        clusters[next_id] = members
        del clusters[a], clusters[b]
        for key in [k for k in d if k[0] in (a, b) or k[1] in (a, b)]:
            del d[key]
        next_id += 1
    return merges


def cut(merges, n, threshold):
    """Cluster partition at a distance threshold (replay merges below threshold)."""
    parent = list(range(n + len(merges) + 1))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for idx, (a, b, dist, _) in enumerate(merges):
        if dist <= threshold:
            new = n + idx
            ra = find(a)
            parent[ra] = new
            parent[new] = new
            rb = find(b)
            parent[rb] = new
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


LADDER = [0.60, 0.70, 0.80, 0.85]


def build(name):
    if name == "quotes":
        units = load_quotes()
    else:
        units, _, _ = load_seeker()
    docs = [tokens(u["text"]) for u in units]
    vecs = tfidf(docs)
    merges = average_linkage(vecs, [u["id"] for u in units])
    chosen = None
    for thr in LADDER:
        groups = cut(merges, len(units), thr)
        if len(groups) <= 40:
            chosen = (thr, groups)
            break
    if chosen is None:
        groups = cut(merges, len(units), LADDER[-1])
        chosen = (LADDER[-1], groups[:40])
    thr, groups = chosen
    res = []
    for gi, g in enumerate(groups):
        centroid = {}
        for i in g:
            for t, x in vecs[i].items():
                centroid[t] = centroid.get(t, 0.0) + x
        members = sorted(g, key=lambda i: -cos(vecs[i], centroid))
        res.append({
            "cluster": gi, "size": len(g),
            "ids": [units[i]["id"] for i in members],
            "top_terms": [t for t, _ in sorted(
                ((t, sum(vecs[i].get(t, 0) for i in g)) for t in centroid),
                key=lambda kv: -kv[1])[:20]],
            "n_pos_quotes": sum(1 for i in g if units[i].get("label")),
            "corpora": dict(Counter(units[i].get("corpus") for i in g)),
            "seeker_classes": dict(Counter(c for i in g for c in (units[i].get("classes") or []))),
            "exemplars": [units[i]["text"][:1200] for i in members[:3]],
        })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"families-{name}.json").write_text(json.dumps(
        {"units": len(units), "threshold": thr, "n_clusters": len(groups),
         "clusters": res}, indent=1))
    print(json.dumps({"name": name, "units": len(units), "threshold": thr,
                      "n_clusters": len(groups),
                      "sizes": [c["size"] for c in res]}))


if __name__ == "__main__":
    build(sys.argv[1])
