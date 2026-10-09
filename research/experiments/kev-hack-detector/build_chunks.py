"""Build Kev hack-detector chunk dataset from CheatBench + detectors-v1.

Reads:
  derived/kev/raw/cheatbench            (HF steinad/CheatBench snapshot)
  research/calibration/detectors-v1/    (manifest.jsonl, corpus.json, tw_subset.json)
  <tw-root>                             (materialized TW trials; fetched if missing)
  ~/Developer/eval-lab-results          (published ours/g6 trials, read-only)

Writes (all deterministic; rerun-safe):
  derived/kev/chunks/{train,dev,test,external}.jsonl
  derived/kev/chunks/{train_sample,eval_sample}.jsonl
  derived/kev/chunks/stats.json

Usage (from worktree root):
  uv run --with transformers python research/experiments/kev-hack-detector/build_chunks.py [--limit N]

Only this script's --limit flag truncates input (for the determinism check).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE / "src"))

# ---------------------------------------------------------------- constants

# Family hold-out: test families never appear in train.
TEST_BENCHES = {"swe-bench-verified", "swe-rebench", "cybench"}
TRAIN_BENCHES = {
    "terminal-bench-2",
    "hal-usaco",
    "swe-smith",
    "mle-bench",
    "kernelbench",
    "bountybench",
}

MECH_TO_PATTERN = {
    "harness_leaks_verifier": "verifier_access",
    "harness_leaks_answer": "answer_leak",
    "secret_discovery_in_environment": "answer_leak",
    "secret_discovery_via_web_search": "upstream_fetch",
    "verifier_exploitation": "grader_tamper",
}

TW_FAMILY_TO_PATTERN = {
    "structural-extraction": "verifier_access",
    "binary-hijacking": "grader_tamper",
    "mutable-input-tampering": "grader_tamper",
    "hollow-implementation": "hollow_solution",
    "output-spoofing": "hollow_solution",
    "keyword-gaming": "hollow_solution",
    "metric-spoofing": "hollow_solution",
    "constraint-loophole": "constraint_violation",
    "algorithmic-simplification": "constraint_violation",
    "security-downgrading": "constraint_violation",
    "deceptive-rationalization": "false_claim",
    "copy_upstream": "upstream_fetch",
}

# Priority for primary pattern when several families fire. deceptive-rationalization
# (usually a confession alongside the real exploit) ranks last.
PATTERN_PRIORITY = [
    "grader_tamper",
    "verifier_access",
    "answer_leak",
    "upstream_fetch",
    "hollow_solution",
    "constraint_violation",
    "false_claim",
]

PREFIX_TOKENS = 800
WINDOW_TOKENS = 5000
OVERLAP_TOKENS = 500
MAX_OUTPUT_CHARS = 4000  # single tool output truncation
MAX_BODY_CHARS = 1_500_000  # per-trace safety cap (extreme tail only)
SEED = 0

# ---------------------------------------------------------------- tokenizer

class QwenCounter:
    def __init__(self):
        from transformers import AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained("Qwen/Qwen3.5-4B-Base")
        self.name = "Qwen/Qwen3.5-4B-Base"

    def encode(self, text):
        return self.tok.encode(text)

    def offsets(self, text):
        enc = self.tok(text, return_offsets_mapping=True, add_special_tokens=False)
        return enc["input_ids"], enc["offset_mapping"]


class CharFallback:
    name = "chars/3.5"

    def encode(self, text):
        return [0] * int(len(text) / 3.5 + 0.5)

    def offsets(self, text):
        n = len(self.encode(text))
        step = len(text) / max(n, 1)
        ids = [0] * n
        offs = [(int(i * step), int((i + 1) * step)) for i in range(n)]
        return ids, offs


def get_counter():
    try:
        return QwenCounter()
    except Exception as e:
        print(f"[tok] Qwen tokenizer unavailable ({e}); using chars/3.5 fallback")
        return CharFallback()


# ---------------------------------------------------------------- rendering

def _trunc(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} chars ...]"


def _s(obj, limit=MAX_OUTPUT_CHARS):
    if obj is None:
        return ""
    if isinstance(obj, str):
        return _trunc(obj, limit)
    try:
        return _trunc(json.dumps(obj, ensure_ascii=False, default=str), limit)
    except Exception:
        return _trunc(str(obj), limit)


def render_cb_event(ev):
    """Render one CheatBench event dict to plain text (verbatim content kept)."""
    if not isinstance(ev, dict):
        return _s(ev)
    keys = set(ev)
    if keys == {"content", "role"} or keys == {"content", "role", "turn"}:
        return f"[{ev.get('role')}] {_s(ev.get('content'))}"
    if keys == {"content", "kind", "role"}:
        return f"[{ev.get('role')}/{ev.get('kind')}] {_s(ev.get('content'))}"
    if "prompt" in ev and "response" in ev:
        return f"[prompt] {_s(ev.get('prompt'))}\n[response] {_s(ev.get('response'))}"
    if "command" in ev and ("stdout" in ev or "stderr" in ev):
        out = f"$ {ev.get('command')}"
        if ev.get("stdout"):
            out += f"\n[stdout]\n{_s(ev.get('stdout'))}"
        if ev.get("stderr"):
            out += f"\n[stderr]\n{_s(ev.get('stderr'))}"
        if ev.get("return_code") not in (None, 0):
            out += f"\n[exit {ev.get('return_code')}]"
        return out
    if "msg" in ev and "src" in ev:
        out = f"[step {ev.get('step')} {ev.get('src')}] {_s(ev.get('msg'))}"
        if ev.get("tools"):
            out += f"\n[tools] {_s(ev.get('tools'))}"
        if ev.get("obs") is not None:
            out += f"\n[observation]\n{_s(ev.get('obs'))}"
        return out
    if "thought" in ev or "tool_calls" in ev or "action" in ev:
        parts = [f"[step {ev.get('role', 'agent')}]"]
        if ev.get("thought"):
            parts.append(f"[thinking]\n{_s(ev.get('thought'))}")
        if ev.get("action"):
            parts.append(f"[action] {_s(ev.get('action'))}")
        if ev.get("content"):
            parts.append(str(_s(ev.get("content"))))
        for c in ev.get("tool_calls") or []:
            if isinstance(c, dict):
                parts.append(
                    f"[tool:{c.get('function_name', c.get('name', '?'))}] "
                    f"{_s(c.get('args', c.get('input', c.get('arguments'))))}"
                )
        return "\n".join(parts)
    if "inputs" in ev and "output" in ev:
        inputs = ev.get("inputs") or {}
        kw = inputs.get("kwargs", {}) if isinstance(inputs, dict) else {}
        msgs = kw.get("messages") or (inputs.get("messages", []) if isinstance(inputs, dict) else [])
        out = ""
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict):
                    out += f"[{m.get('role')}] {_s(m.get('content'))}\n"
        if ev.get("output") is not None:
            out += f"[output]\n{_s(ev.get('output'))}"
        return out or _s({k: v for k, v in ev.items() if k != "attributes"})
    if ev.get("message_type") and ("content" in ev or "agent" in ev):
        out = f"[{ev.get('role', 'agent')}] {_s(ev.get('content'))}"
        for cid in ev.get("tool_call_ids") or []:
            out += f"\n[tool_call_id {cid}]"
        return out
    if set(ev) == {"id", "text", "type"}:
        return f"[log]\n{_s(ev.get('text'))}"
    head = ev.get("role") or ev.get("src") or ev.get("type") or "event"
    return f"[{head}] {_s(ev)}"


def render_cb_trace(tr):
    """Return (task_text, units). Each unit renders one message or event."""
    task_text = ""
    t = tr.get("task")
    if isinstance(t, dict):
        task_text = (
            t.get("instruction") or t.get("prompt") or t.get("text") or t.get("problem") or ""
        )
    task_text = task_text if isinstance(task_text, str) else _s(task_text)
    if "messages" in tr and isinstance(tr["messages"], list):
        units = []
        for m in tr["messages"]:
            if isinstance(m, dict):
                units.append(f"[{m.get('role', 'message')}] {_s(m.get('content'))}")
            else:
                units.append(_s(m))
        if not task_text and units:
            task_text = units[0][:2000]
    else:
        units = [render_cb_event(e) for e in tr.get("events") or []]
    return task_text, units


def render_atif_step(step):
    src = step.get("source", "?")
    lines = [f"[step {step.get('step_id')} {src}]"]
    msg = step.get("message")
    if isinstance(msg, str):
        lines.append(_trunc(msg))
    elif isinstance(msg, list):
        for part in msg:
            if isinstance(part, dict) and part.get("type") == "text":
                lines.append(_trunc(part.get("text", "")))
    for c in step.get("tool_calls") or []:
        if isinstance(c, dict):
            fn = c.get("function_name", "?")
            lines.append(f"[tool:{fn}] {_s(c.get('arguments', c.get('input', '')))}")
    for r in (step.get("observation") or {}).get("results", []):
        if isinstance(r, dict):
            lines.append(f"[observation]\n{_s(r.get('content', r.get('output', r.get('text', ''))))}")
    return "\n".join(lines)


def render_atif(traj, instruction_path=None):
    steps = traj.get("steps", [])
    units = [render_atif_step(s) for s in steps]
    task_text = ""
    if instruction_path is not None and instruction_path.is_file():
        task_text = instruction_path.read_text(encoding="utf-8", errors="replace")
        task_text = re.sub(r"^# Task instruction\s*", "", task_text).strip()
    if not task_text:
        task_text = next(
            (
                s.get("message", "")
                for s in steps
                if s.get("source") == "user" and isinstance(s.get("message"), str)
            ),
            "",
        )
    return task_text, units

# ---------------------------------------------------------------- localization

_WS = re.compile(r"\s+")


def resolve_field_path(tr, path):
    """Resolve e.g. 'events[0].stdout' / 'messages[8].content' -> (idx, string)."""
    if not path:
        return None
    m = re.match(r"^(events|messages)\[(\d+)\]\.?([^\[]*)$", path)
    if not m:
        return None
    kind, idx, rest = m.group(1), int(m.group(2)), m.group(3)
    seq = tr.get("events") if kind == "events" else tr.get("messages")
    if not isinstance(seq, list) or idx >= len(seq):
        return idx, None
    node = seq[idx]
    if rest:
        for part in rest.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return idx, None
    if node is not None and not isinstance(node, str):
        try:
            node = json.dumps(node, ensure_ascii=False, default=str)
        except Exception:
            node = str(node)
    return idx, node


def anchor_events_cb(tr, units, row):
    """Return (set_of_unit_indices, method) locating the cheating evidence."""
    n = len(units)
    norms = [_WS.sub(" ", u) for u in units]
    snippet = row.get("loc_snippet") or ""
    if snippet.strip():
        key = _WS.sub(" ", snippet).strip()[:600]
        for i, full in enumerate(norms):
            if key and key in full:
                return {i}, "snippet"
    loc_events = row.get("loc_events") or []
    if loc_events:
        idx = {int(e) for e in loc_events if 0 <= int(e) < n}
        if idx:
            return idx, "loc_events"
    res = resolve_field_path(tr, row.get("loc_field_path") or "")
    if res is not None:
        idx, content = res
        if 0 <= idx < n and content:
            ls, le = row.get("loc_line_start"), row.get("loc_line_end")
            try:
                lines = content.splitlines()
                frag = "\n".join(lines[max((ls or 1) - 1, 0): max(le or ls or 1, 0)])
            except Exception:
                frag = ""
            key = _WS.sub(" ", frag).strip()[:600]
            if key and key in norms[idx]:
                return {idx}, "field_lines"
            return {idx}, "field_path"
    kind = row.get("loc_kind") or ""
    if kind in ("latent_harness", "latent_inference") or not snippet.strip():
        return set(), "latent_or_empty"
    return set(), "unmatched"


# ---------------------------------------------------------------- chunking

def build_state(prefix, body_slice):
    return f"[task]\n{prefix}\n\n[trajectory]\n{body_slice}"


def chunk_units(prefix_text, units, counter):
    """Sliding windows over the unit-concatenated body. Returns (states, spans)."""
    body = "\n\n---\n\n".join(units)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... trace truncated ...]"
    prefix_ids = counter.encode(prefix_text)[:PREFIX_TOKENS]
    try:
        prefix = counter.tok.decode(prefix_ids)
    except AttributeError:
        prefix = prefix_text[: int(PREFIX_TOKENS * 3.5)]
    ids, offs = counter.offsets(body)
    if not ids:
        return [build_state(prefix, "")], [(0, 0)]
    stride = WINDOW_TOKENS - OVERLAP_TOKENS
    chunks, spans = [], []
    start = 0
    while start < len(ids):
        end = min(start + WINDOW_TOKENS, len(ids))
        c0 = offs[start][0]
        c1 = offs[end - 1][1] if end - 1 < len(offs) else len(body)
        chunks.append(build_state(prefix, body[c0:c1]))
        spans.append((c0, c1))
        if end >= len(ids):
            break
        start += stride
    return chunks, spans


def unit_char_spans(units):
    sep_len = len("\n\n---\n\n")
    spans, pos = [], 0
    for u in units:
        spans.append((pos, pos + len(u)))
        pos += len(u) + sep_len
    return spans

# ---------------------------------------------------------------- sources

def load_cheatbench(raw_dir):
    import pyarrow.parquet as pq

    pq_path = raw_dir / "cheatbench" / "data" / "processed" / "parquet" / "full.parquet"
    if not pq_path.is_file():
        cands = sorted((raw_dir / "cheatbench").rglob("full.parquet"))
        if not cands:
            raise FileNotFoundError("CheatBench full.parquet not found; run download first")
        pq_path = cands[0]
    rows = pq.read_table(str(pq_path)).to_pylist()
    rows.sort(key=lambda r: r["trace_id"])
    return rows, pq_path


def fetch_cheatbench(raw_dir):
    from huggingface_hub import snapshot_download

    snapshot_download(
        "steinad/CheatBench", repo_type="dataset", local_dir=str(raw_dir / "cheatbench")
    )


def materialize_tw(manifest_rows, cache_dir, tw_root):
    from evallab import terminal_wrench as twmod

    todo = [r for r in manifest_rows if r["trial"]["kind"] == "terminal_wrench"]
    by_variant = {}
    for r in todo:
        by_variant.setdefault(r["trial"]["variant"], []).append(r)
    for variant, rows in by_variant.items():
        # Manifest marks clean baselines variant="baseline" (an upstream tree name,
        # not a fetch variant). fetch_trials maps clean rows to the baseline tree
        # per row, so fetch them under "sanitized".
        fetch_variant = variant if variant in ("sanitized", "stripped", "raw") else "sanitized"
        fetch_rows = [
            {
                "id": r["trial"]["tw_id"],
                "task_id": r["trial"]["task_id"],
                "model": r["trial"]["model"],
                "label": r["trial"]["label"],
                "tw_key": r["trial"]["tw_key"],
            }
            for r in rows
        ]
        stats = twmod.fetch_trials(fetch_rows, str(cache_dir), variant=fetch_variant)
        print(f"[tw] variant={variant} (fetch={fetch_variant}) stats={json.dumps(stats)[:300]}")
        for fr, r in zip(fetch_rows, rows, strict=True):
            dest = tw_root / r["source"] / r["trial"]["tw_id"]
            if (dest / "agent" / "trajectory.json").is_file():
                continue
            native = json.loads(
                Path(twmod.trajectory_cache_path(fr, str(cache_dir), fetch_variant)).read_text(
                    encoding="utf-8"
                )
            )
            twmod.materialize_trial(
                dest=str(dest),
                task_id=r["trial"]["task_id"],
                trial_name=r["trial"]["tw_id"],
                model=r["trial"]["model"],
                native_trajectory=native,
                reward=1.0,
            )


def load_manifest(corpus_dir):
    rows = [
        json.loads(line)
        for line in (corpus_dir / "manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows.sort(key=lambda r: r["id"])
    return rows


# ---------------------------------------------------------------- main build

def trace_task_id_cb(tr):
    t = tr.get("task")
    if isinstance(t, dict) and t.get("id"):
        return str(t["id"])
    for k in ("task_name", "trial_name"):
        if tr.get(k):
            return str(tr[k])
    return ""


def primary_pattern(families):
    pats = {TW_FAMILY_TO_PATTERN.get(f) for f in families or []} - {None}
    if not pats:
        return None
    if len(pats) == 1:
        return next(iter(pats))
    pool = pats - {"false_claim"} or pats
    for p in PATTERN_PRIORITY:
        if p in pool:
            return p
    return sorted(pool)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--raw", default="derived/kev/raw")
    ap.add_argument("--out", default="derived/kev/chunks")
    ap.add_argument("--tw-root", default="derived/kev/raw/tw")
    ap.add_argument("--tw-cache", default="derived/kev/raw/tw-cache")
    ap.add_argument("--results-root", default=os.path.expanduser("~/Developer/eval-lab-results"))
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    def _p(p):
        pp = Path(p)
        return pp if pp.is_absolute() else WORKTREE / pp

    raw_dir, out_dir, tw_root, tw_cache = _p(args.raw), _p(args.out), _p(args.tw_root), _p(args.tw_cache)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = WORKTREE / "research" / "calibration" / "detectors-v1"

    counter = get_counter()
    rng = random.Random(SEED)

    if not (raw_dir / "cheatbench").exists() and not args.no_fetch:
        print("[cb] downloading steinad/CheatBench ...")
        fetch_cheatbench(raw_dir)
    cb_rows, pq_path = load_cheatbench(raw_dir)
    if args.limit:
        cb_rows = cb_rows[: args.limit]

    # TB2 task-disjoint dev slice: ~10% of TB2 task ids (sorted, every 10th).
    tb2_tasks = sorted(
        {
            trace_task_id_cb(json.loads(r["trace"]))
            for r in cb_rows
            if r["benchmark"] == "terminal-bench-2"
        }
        - {""}
    )
    dev_tasks = set(tb2_tasks[::10])
    print(f"[split] tb2 tasks={len(tb2_tasks)} dev_tasks={len(dev_tasks)}")

    manifest = load_manifest(corpus_dir)
    if args.limit:
        manifest = manifest[: args.limit]
    if not args.no_fetch:
        materialize_tw(manifest, tw_cache, tw_root)
    results_root = Path(args.results_root)

    records = {s: [] for s in ("train", "dev", "test", "external")}
    loc_stats = Counter()
    tw_task_ids, cb_tb2_task_ids = set(), set()
    capped_traces = []

    def emit(split, chunk_id, trace_id, source, benchmark, state, trace_label,
             pattern, chunk_label, blatancy, n_chunks, idx):
        records[split].append(
            {
                "chunk_id": chunk_id,
                "trace_id": trace_id,
                "source": source,
                "benchmark": benchmark,
                "split": split,
                "chunk_index": idx,
                "n_chunks": n_chunks,
                "state": state,
                "state_tokens": len(counter.encode(state)),
                "trace_label": trace_label,
                "pattern": pattern,
                "chunk_label": chunk_label,
                "loc_blatancy": blatancy,
            }
        )

    for r in cb_rows:
        tr = json.loads(r["trace"])
        task_text, units = render_cb_trace(tr)
        bench = r["benchmark"]
        if bench == "terminal-bench-2":
            cb_tb2_task_ids.add(trace_task_id_cb(tr))
        label = r["label"]
        pattern = "none" if label == "benign" else MECH_TO_PATTERN.get(r["cheating_mechanism"])
        blatancy = r.get("loc_blatancy") or None

        if bench in TEST_BENCHES:
            split = "test"
        elif bench in TRAIN_BENCHES:
            tid = trace_task_id_cb(tr)
            split = "dev" if (bench == "terminal-bench-2" and tid in dev_tasks) else "train"
        else:
            split = "test"  # unknown family: hold out, never train

        if len("\n\n---\n\n".join(units)) > MAX_BODY_CHARS:
            capped_traces.append(r["trace_id"])
        states, spans = chunk_units(task_text, units, counter)
        uspans = unit_char_spans(units)

        if label in ("cheating", "attempt"):
            anchor, method = anchor_events_cb(tr, units, r)
            loc_stats[f"{label}:{method}"] += 1
            loc_stats[f"{label}:matched" if anchor else f"{label}:unmatched"] += 1
            aspans = [uspans[i] for i in anchor if i < len(uspans)]
        else:
            aspans = []

        for i, (state, (c0, c1)) in enumerate(zip(states, spans, strict=True)):
            if label == "benign":
                cl = False
            elif label == "attempt":
                cl = None
            else:
                cl = True if any(a0 < c1 and c0 < a1 for a0, a1 in aspans) else None
            emit(split, f"{r['trace_id']}:c{i:03d}", r["trace_id"], "cheatbench",
                 bench, state, label, pattern, cl, blatancy, len(states), i)

    for m in manifest:
        trial = m["trial"]
        if trial["kind"] == "terminal_wrench":
            tw_task_ids.add(trial["task_id"])
            tdir = tw_root / m["source"] / trial["tw_id"]
            bench = "terminal-wrench"
        else:
            tdir = results_root / trial["path"]
            bench = f"eval-lab-{m['source']}"
        traj_path = tdir / "agent" / "trajectory.json"
        if not traj_path.is_file():
            print(f"[warn] missing trial dir for {m['id']}: {tdir}")
            continue
        traj = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
        task_text, units = render_atif(traj, tdir / "instruction.md")
        lab = m["labels"]
        trace_label = "cheating" if (lab.get("reward_hacking") is True or lab.get("copied") is True) else "benign"
        pattern = primary_pattern(m.get("families") or []) if trace_label == "cheating" else "none"
        states, _spans = chunk_units(task_text, units, counter)
        for i, state in enumerate(states):
            emit("external", f"{m['id']}:c{i:03d}", m["id"], "detectors-v1",
                 bench, state, trace_label, pattern,
                 None if trace_label == "cheating" else False,
                 None, len(states), i)

    overlap = sorted(tw_task_ids & cb_tb2_task_ids)
    print(f"[overlap] tw_tasks={len(tw_task_ids)} cb_tb2_tasks={len(cb_tb2_task_ids)} "
          f"intersection={len(overlap)} {overlap[:10]}")

    for split, recs in records.items():
        recs.sort(key=lambda r: r["chunk_id"])
        with open(out_dir / f"{split}.jsonl", "w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[write] {split}: {len(recs)} chunks")

    token_lens = sorted(len(counter.encode(rec["state"])) for recs in records.values() for rec in recs)

    def pct(p):
        return token_lens[min(int(p / 100 * len(token_lens)), len(token_lens) - 1)] if token_lens else 0

    stats = {
        "tokenizer": counter.name,
        "by_split": {s: len(r) for s, r in records.items()},
        "by_source": dict(Counter(rec["source"] for recs in records.values() for rec in recs)),
        "by_benchmark": dict(Counter(rec["benchmark"] for recs in records.values() for rec in recs)),
        "by_pattern": dict(Counter(str(rec["pattern"]) for recs in records.values() for rec in recs)),
        "by_chunk_label": dict(Counter(str(rec["chunk_label"]) for recs in records.values() for rec in recs)),
        "by_split_pattern": {
            s: dict(Counter(str(rec["pattern"]) for rec in recs)) for s, recs in records.items()
        },
        "state_tokens": {
            "n": len(token_lens),
            "p50": pct(50),
            "p90": pct(90),
            "p99": pct(99),
            "max": max(token_lens) if token_lens else 0,
        },
        "loc_match": dict(loc_stats),
        "tb2_dev_tasks": len(dev_tasks),
        "tw_cb_task_overlap": overlap,
        "capped_traces": capped_traces,
        "cheatbench_parquet": str(pq_path),
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=1) + "\n", encoding="utf-8")

    train = records["train"]
    pos = [r for r in train if r["chunk_label"] is True]
    neg_pool = [r for r in train if r["chunk_label"] is False]
    n_neg = min(len(neg_pool), 2 * len(pos), 4500 - len(pos))
    by_bench: dict = {}
    for r in neg_pool:
        by_bench.setdefault(r["benchmark"], []).append(r)
    for v in by_bench.values():
        rng.shuffle(v)
    sample_neg = []
    benches = sorted(by_bench)
    bi = 0
    while len(sample_neg) < n_neg and any(by_bench.values()):
        v = by_bench[benches[bi % len(benches)]]
        if v:
            sample_neg.append(v.pop())
        bi += 1
    train_sample = sorted(pos + sample_neg, key=lambda r: r["chunk_id"])
    with open(out_dir / "train_sample.jsonl", "w", encoding="utf-8") as f:
        for rec in train_sample:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    train_toks = sum(r["state_tokens"] for r in train_sample)
    print(f"[sample] train_sample={len(train_sample)} (pos={len(pos)} neg={len(sample_neg)}) toks={train_toks}")

    pool = records["test"] + records["dev"]
    epos = [r for r in pool if r["chunk_label"] is True]
    eben_pool = [r for r in pool if r["chunk_label"] is False]
    by_bench = {}
    for r in eben_pool:
        by_bench.setdefault(r["benchmark"], []).append(r)
    for v in by_bench.values():
        rng.shuffle(v)
    eben = []
    benches = sorted(by_bench)
    bi = 0
    while len(eben) < min(1500, len(eben_pool)) and any(by_bench.values()):
        v = by_bench[benches[bi % len(benches)]]
        if v:
            eben.append(v.pop())
        bi += 1
    traces: dict = {}
    for r in pool:
        traces.setdefault(r["trace_id"], []).append(r)
    benign_tids = sorted(t for t, rs in traces.items() if rs[0]["trace_label"] == "benign")
    cheat_tids = sorted(t for t, rs in traces.items() if rs[0]["trace_label"] == "cheating")
    rng.shuffle(benign_tids)
    trace_set = set()
    for t in benign_tids[:150]:
        trace_set.update(r["chunk_id"] for r in traces[t])
    for t in cheat_tids:
        trace_set.update(r["chunk_id"] for r in sorted(traces[t], key=lambda r: r["chunk_index"])[:40])
    merged = {}
    for r in epos + eben:
        merged[r["chunk_id"]] = {**r, "eval_role": "chunk"}
    for cid in trace_set:
        src = next(r for r in pool if r["chunk_id"] == cid)
        merged[cid] = {**src, "eval_role": "trace"}
    eval_sample = sorted(merged.values(), key=lambda r: r["chunk_id"])
    with open(out_dir / "eval_sample.jsonl", "w", encoding="utf-8") as f:
        for rec in eval_sample:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    eval_toks = sum(r["state_tokens"] for r in eval_sample)
    roles = Counter(r["eval_role"] for r in eval_sample)
    print(f"[sample] eval_sample={len(eval_sample)} roles={dict(roles)} toks={eval_toks}")
    print(f"[loc] {json.dumps(dict(loc_stats))}")
    print(f"[capped] {len(capped_traces)} traces over char cap: {capped_traces[:5]}")


if __name__ == "__main__":
    main()
