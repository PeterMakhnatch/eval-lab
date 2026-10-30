#!/usr/bin/env python3
"""DT-12 duplication: fingerprints from normalised tool-call sequences, clusters at
three thresholds, effective sample size, and DT-8 numbers re-scored per cluster.

Reads only the reader prompt files and the two readings files. Labels are used for
scoring, never in a prompt.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
DT12 = RAW / "DT-12"
PROMPTS = Path("/tmp/dt8/prompts")
OUT = DT12 / "receipts"

CALL_RE = re.compile(r"<call ([A-Za-z0-9_\-]+)>\s*(.*?)\s*</call>", re.S)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# parse + normalise
# --------------------------------------------------------------------------- #


def call_args(raw: str) -> str:
    """The command-ish string of a call: JSON 'command'/'cmd'/'code', else raw."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(obj, dict):
        for key in ("command", "cmd", "code", "script", "content"):
            if isinstance(obj.get(key), str):
                return obj[key]
    return raw


def norm_cmds(text: str) -> list[str]:
    """Normalise a shell-ish command into 'head1 head2' segments (skeleton form)."""
    s = text.lower()
    s = re.sub(r"\$\(([^)]*)\)", r" ; \1", s)
    s = re.sub(r"`([^`]*)`", r" ; \1", s)
    out = []
    for seg in re.split(r"&&|\|\||;|\||\n", s):
        toks = [t for t in seg.split() if t and not t.startswith("#")]
        if not toks:
            continue
        head = toks[:2]
        cleaned = []
        for t in head:
            t = re.sub(r"\b[0-9a-f]{7,}\b", "<h>", t)
            t = re.sub(r"(/tmp|/var/folders|/private/var)\S*", "<p>", t)
            t = re.sub(r"\d+", "<n>", t)
            t = t.strip("\"'`(),[]{}")
            if t:
                cleaned.append(t)
        if cleaned:
            out.append(" ".join(cleaned))
    return out


def norm_tokens(text: str) -> list[str]:
    """Every token of a shell-ish command, normalised (full form, not just heads)."""
    s = text.lower()
    s = re.sub(r"\$\(([^)]*)\)", r" ; \1", s)
    s = re.sub(r"`([^`]*)`", r" ; \1", s)
    toks = []
    for seg in re.split(r"&&|\|\||;|\||\n", s):
        seg_toks = []
        for t in seg.split():
            if not t or t.startswith("#"):
                continue
            t = re.sub(r"\b[0-9a-f]{7,}\b", "<h>", t)
            t = re.sub(r"(/tmp|/var/folders|/private/var)\S*", "<p>", t)
            t = re.sub(r"\d+", "<n>", t)
            t = t.strip("\"'`(),[]{}")
            if t:
                seg_toks.append(t)
        if seg_toks:
            toks.append(";".join(seg_toks))
    return toks


def fingerprint(text: str, mode: str = "full") -> tuple[str, list[str], int]:
    """(fingerprint sha, token list used for tf-idf, n pieces).

    mode='full'    every token of the command (default; the duplication tiers)
    mode='skeleton' the prereg's 2-token command heads (structure measure only)
    """
    if "<run>" in text:
        text = text.split("<run>", 1)[1]
    if "</run>" in text:
        text = text.rsplit("</run>", 1)[0]
    rendered: list[str] = []
    tokens: list[str] = []
    for match in CALL_RE.finditer(text):
        name = match.group(1).lower()
        body = call_args(match.group(2))
        if mode == "skeleton":
            for head in norm_cmds(body):
                rendered.append(f"{name}:{head}")
                tokens.append(f"{name}:{head}")
        else:
            for seg in norm_tokens(body):
                rendered.append(f"{name}:{seg}")
                tokens.extend(f"{name}:{t}" for t in seg.split(";"))
    return sha("\x1f".join(rendered)), tokens, len(rendered)


# --------------------------------------------------------------------------- #
# tf-idf cosine
# --------------------------------------------------------------------------- #


def tfidf(docs: list[list[str]]):
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


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def components(ids: list[str], sim: dict[tuple[str, str], float], thr: float) -> list[list[str]]:
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), s in sim.items():
        if s >= thr:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0]))


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


def load_units():
    d8 = [json.loads(l) for l in (RAW / "DT-8/receipts/readings-new.jsonl").open()]
    d2 = [json.loads(l) for l in (RAW / "DT-2/receipts/readings.jsonl").open()]
    units = {}
    for r in d8:
        units[r["blind_id"]] = {
            "blind_id": r["blind_id"], "run_id": r["run_id"], "corpus": r["corpus"],
            "positive": bool(r["positive"]), "status": r.get("status"),
            "alert": (r.get("status") == "ok" and (r.get("n_verified") or 0) >= 1),
            "n_verified": r.get("n_verified") or 0, "source": "DT-8",
        }
    for r in d2:
        units[r["blind_id"]] = {
            "blind_id": r["blind_id"], "run_id": r["run_id"], "corpus": "goodfire",
            "positive": bool(r["enacted"]), "status": r.get("status"),
            "alert": (r.get("status") == "ok" and (r.get("n_verified") or 0) >= 1),
            "n_verified": r.get("n_verified") or 0, "source": "DT-2",
        }
    return units


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def run():
    units = load_units()
    files = sorted(PROMPTS.glob("*.txt"))
    missing = [f.stem for f in files if f.stem not in units]
    extra = [b for b in units if b not in {f.stem for f in files}]
    rows = []
    for f in files:
        blind = f.stem
        text = f.read_text(errors="replace")
        fp_full, tokens, npieces = fingerprint(text, "full")
        fp_skel, _, _ = fingerprint(text, "skeleton")
        rows.append({"blind_id": blind, "corpus": units[blind]["corpus"], **units[blind],
                     "fingerprint": fp_full, "skeleton": fp_skel,
                     "ncalls": npieces, "tokens": tokens})
    ids = [r["blind_id"] for r in rows]
    by_tok = {r["blind_id"]: r["tokens"] for r in rows}
    docs = [by_tok[i] for i in ids]
    vecs = {i: v for i, v in zip(ids, tfidf(docs))}
    sim = {}
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            sim[(ids[a], ids[b])] = cosine(vecs[ids[a]], vecs[ids[b]])

    by_id = {r["blind_id"]: r for r in rows}
    out = {"n_files": len(files), "n_units": len(units),
           "missing_from_units": missing, "units_without_prompt": extra,
           "n_no_tool_call": sum(1 for r in rows if r["ncalls"] == 0)}

    def group_within(sub_ids, label):
        """Clusters computed inside one corpus only."""
        fp_groups = defaultdict(list)
        for i in sub_ids:
            fp_groups[by_id[i]["fingerprint"]].append(i)
        ident = sorted(fp_groups.values(), key=lambda g: (-len(g), sorted(g)[0]))
        sk_groups = defaultdict(list)
        for i in sub_ids:
            sk_groups[by_id[i]["skeleton"]].append(i)
        skel = sorted(sk_groups.values(), key=lambda g: (-len(g), sorted(g)[0]))
        sub_sim = {k: v for k, v in sim.items() if k[0] in sub_ids and k[1] in sub_ids}
        c09 = components(sub_ids, sub_sim, 0.90)
        c08 = components(sub_ids, sub_sim, 0.80)
        return {"identical": ident, "skeleton": skel,
                f"{label}_cosine_0.90": c09, f"{label}_cosine_0.80": c08}

    def summarise(groups, label, n_runs):
        per = {"n_runs": n_runs, "clusters": len(groups),
               "effective_fraction": len(groups) / n_runs if n_runs else None,
               "mean_size": n_runs / len(groups) if groups else None,
               "max_size": max((len(g) for g in groups), default=0),
               "n_redundant_runs_removed": n_runs - len(groups),
               "by_label": {}}
        for lab in (True, False):
            runs = [i for i in ids if by_id[i]["corpus"] == label and by_id[i]["positive"] is lab]
            cl = [g for g in groups if any(by_id[i]["positive"] is lab for i in g)]
            per["by_label"]["pos" if lab else "neg"] = {
                "n_runs": len(runs), "n_clusters": len(cl),
                "effective_fraction": len(cl) / len(runs) if runs else None}
        sizes = Counter(len(g) for g in groups)
        per["size_histogram"] = {str(k): v for k, v in sorted(sizes.items())}
        return per

    def cluster_numbers(groups, label):
        """DT-8 alert numbers over representatives vs runs, for one corpus."""
        reps, anyalm, runs, mixed_pos = [], [], [], 0
        for g in groups:
            members = sorted(i for i in g if by_id[i]["status"] == "ok")
            if not members:
                continue
            pos = any(by_id[i]["positive"] for i in g)
            if pos:
                mixed_pos += 1
            reps.append({"pos": pos, "alert": by_id[members[0]]["alert"]})
            anyalm.append({"pos": pos, "alert": any(by_id[i]["alert"] for i in g)})
            for i in members:
                runs.append({"pos": by_id[i]["positive"], "alert": by_id[i]["alert"]})

        def rate(obj):
            return {"pos_alert": sum(1 for x in obj if x["pos"] and x["alert"]),
                    "n_pos": sum(1 for x in obj if x["pos"]),
                    "neg_alert": sum(1 for x in obj if not x["pos"] and x["alert"]),
                    "n_neg": sum(1 for x in obj if not x["pos"])}
        return {"representative": rate(reps), "any_member": rate(anyalm),
                "run_level": rate(runs), "n_clusters": len(reps),
                "n_clusters_with_pos": mixed_pos}

    out["by_corpus"] = {}
    out["cluster_level_dt8"] = {}
    saved = {}
    for corpus in ("goodfire", "cheatbench"):
        sub = [i for i in ids if by_id[i]["corpus"] == corpus]
        g = group_within(sub, corpus)
        saved[corpus] = {"identical": g["identical"],
                         "skeleton": g["skeleton"],
                         "cosine_0.90": g[f"{corpus}_cosine_0.90"],
                         "cosine_0.80": g[f"{corpus}_cosine_0.80"]}
        out["by_corpus"][corpus] = {
            "identical": summarise(g["identical"], corpus, len(sub)),
            "skeleton": summarise(g["skeleton"], corpus, len(sub)),
            "cosine_0.90": summarise(g[f"{corpus}_cosine_0.90"], corpus, len(sub)),
            "cosine_0.80": summarise(g[f"{corpus}_cosine_0.80"], corpus, len(sub)),
        }
        out["cluster_level_dt8"][corpus] = {
            "run_level": cluster_numbers(g["identical"], corpus)["run_level"],
            "identical": cluster_numbers(g["identical"], corpus),
            "skeleton": cluster_numbers(g["skeleton"], corpus),
            "cosine_0.90": cluster_numbers(g[f"{corpus}_cosine_0.90"], corpus),
            "cosine_0.80": cluster_numbers(g[f"{corpus}_cosine_0.80"], corpus),
        }

    out["tier_semantics"] = {
        "identical": "same fingerprint: the whole normalised command of every call, in order",
        "cosine_0.90": "connected components of pairwise TF-IDF cosine >= 0.90 over all command tokens",
        "cosine_0.80": "same at 0.80",
        "skeleton": "DEVIATION from prereg's 2-token command heads, kept as a structure measure: with 2-token heads every single-call CheatBench run collapses to 'bash:forge --verbose' and manufactures duplicates, because the 2-token head drops the payload. The duplication tiers therefore use the full normalised command; the 2-token-head clustering is reported as 'skeleton' only.",
    }

    out["similarity"] = {}
    for corpus in ("goodfire", "cheatbench"):
        vals = [v for (a, b), v in sim.items()
                if by_id[a]["corpus"] == corpus and by_id[b]["corpus"] == corpus]
        vals.sort()
        n = len(vals)
        out["similarity"][corpus] = {
            "n_pairs": n,
            "max": vals[-1] if vals else None,
            "p99": vals[int(0.99 * (n - 1))] if vals else None,
            "median": vals[n // 2] if vals else None,
            "n_pairs_ge_0.6": sum(1 for v in vals if v >= 0.6),
            "n_pairs_ge_0.7": sum(1 for v in vals if v >= 0.7),
            "n_pairs_ge_0.8": sum(1 for v in vals if v >= 0.8),
            "n_pairs_ge_0.9": sum(1 for v in vals if v >= 0.9),
        }

    # cross-corpus exact duplication (same fingerprint in both corpora)
    fp_corpora = defaultdict(set)
    for r in rows:
        fp_corpora[r["fingerprint"]].add(r["corpus"])
    shared = [fp for fp, cs in fp_corpora.items() if len(cs) == 2]
    out["cross_corpus"] = {
        "identical_fingerprints_in_both_corpora": len(shared),
        "runs_in_shared_fingerprints": sum(1 for r in rows if r["fingerprint"] in set(shared)),
    }

    # cluster ids and sizes only (no text)
    def dump(groups):
        return [{"size": len(g), "n_pos": sum(1 for i in g if by_id[i]["positive"]),
                 "ids": sorted(g)} for g in groups if len(g) > 1]
    out["clusters"] = {c: {t: dump(v) for t, v in thresholds.items()}
                       for c, thresholds in saved.items()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "duplication.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "clusters"}, indent=1)[:6000])


if __name__ == "__main__":
    run()
