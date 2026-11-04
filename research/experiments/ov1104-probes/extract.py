"""Modal app `ov1104-probes-extract`: DetBench views -> pooled Qwen3-8B hidden states.

Usage (from this folder):
  modal run extract.py::run_set --views-dir V --runs-jsonl R --set-name S --out-npz O [--limit N] [--dry-run]

GPU selection: env OV_GPU (default "L4"); e.g. OV_GPU=A10G modal run ...
Nothing from the corpus is written or logged on Modal; text travels in the
request payload only, vectors come back.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import modal

APP_NAME = "ov1104-probes-extract"
MODEL = "Qwen/Qwen3-8B"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"  # resolved 2026-11-03
LAYERS = (9, 18, 27)
WINDOW = 4096
MAX_WINDOWS = 12
MODEL_BATCH = 2          # windows per forward
RUNS_PER_CALL = 8        # runs per extract_batch call
HIDDEN = 4096
GPU = os.environ.get("OV_GPU", "L4,A10G,L40S,A100-40GB")
GPU_USD_PER_SEC = {      # https://modal.com/pricing read 2026-11-03
    "L4": 0.000222, "A10G": 0.000306, "A10": 0.000306,
    "A100-40GB": 0.000583, "A100-80GB": 0.000694, "L40S": 0.000542, "H100": 0.001097,
}
CACHE_DIR = "/cache"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("torch", "transformers>=4.51", "accelerate", "numpy", "hf_transfer")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "HF_HOME": CACHE_DIR, "TOKENIZERS_PARALLELISM": "false"})
)
hf_cache = modal.Volume.from_name("ov1104-probes-hf-cache", create_if_missing=True)
app = modal.App(APP_NAME)


# ----------------------------------------------------------------------------- shared helpers
def select_windows(n_tokens: int) -> list[tuple[int, int]]:
    """Token spans [s, e) of kept windows (CONTRACT: first 4, 4 evenly spaced middle, last 4)."""
    n_total = max(1, -(-n_tokens // WINDOW))
    if n_total <= MAX_WINDOWS:
        idx = list(range(n_total))
    else:
        mid = list(range(4, n_total - 4))
        pick = sorted({mid[round(k * (len(mid) - 1) / 3)] for k in range(4)})
        idx = list(range(4)) + pick + list(range(n_total - 4, n_total))
        idx = sorted(set(idx))
    return [(i * WINDOW, min((i + 1) * WINDOW, n_tokens)) for i in idx]


def window_steps(cs: int, ce: int, step_spans, step_ids) -> tuple[int, int]:
    """Step range [first_i, last_i] whose char spans intersect [cs, ce)."""
    if not step_ids:
        return (-1, -1)
    first = next((i for (s, e), i in zip(step_spans, step_ids, strict=False) if e > cs), step_ids[-1])
    last = next((i for (s, e), i in reversed(list(zip(step_spans, step_ids, strict=False))) if s < ce), step_ids[0])
    if last < first:
        first = last
    return (first, last)


def tokenize_with_offsets(tok, text: str):
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True,
              return_attention_mask=False, truncation=False, verbose=False)
    return enc["input_ids"], enc["offset_mapping"]


# ----------------------------------------------------------------------------- Modal functions
@app.function(image=image, volumes={CACHE_DIR: hf_cache}, timeout=3600, cpu=4, memory=8192)
def prefetch_weights() -> dict:
    """Download weights to the cache volume on a CPU container (cheaper than doing it at GPU enter)."""
    import shutil

    from huggingface_hub import snapshot_download
    t0 = time.time()
    snap = Path(f"{CACHE_DIR}/hub/models--Qwen--Qwen3-8B/snapshots/{REVISION}")
    if (snap / "model-00005-of-00005.safetensors").exists() and (snap / "config.json").exists():
        n = sum(f.stat().st_size for f in snap.rglob("*") if f.is_file())
        return {"path": str(snap), "bytes": n, "secs": time.time() - t0, "cached": True}
    p = snapshot_download(MODEL, revision=REVISION, allow_patterns=["*.json", "*.safetensors", "*.txt"])
    shutil.rmtree(f"{CACHE_DIR}/xet", ignore_errors=True)
    hf_cache.commit()
    n = sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())
    return {"path": p, "bytes": n, "secs": time.time() - t0, "cached": False}


MAX_CONTAINERS = int(os.environ.get("OV_MAX_CONTAINERS", "4"))
GPU_SPEC = [g.strip() for g in GPU.split(",") if g.strip()] if "," in GPU else GPU


def _price_for_gpu(gpu_name: str | None) -> float:
    if not gpu_name:
        return GPU_USD_PER_SEC.get(GPU, 0.000222)
    u = gpu_name.upper()
    if "H100" in u:
        return GPU_USD_PER_SEC["H100"]
    if "A100" in u and "80" in u:
        return GPU_USD_PER_SEC["A100-80GB"]
    if "A100" in u:
        return GPU_USD_PER_SEC["A100-40GB"]
    if "L40" in u:
        return GPU_USD_PER_SEC["L40S"]
    if "A10" in u:
        return GPU_USD_PER_SEC["A10G"]
    if "L4" in u:
        return GPU_USD_PER_SEC["L4"]
    return GPU_USD_PER_SEC.get(GPU, 0.000222)


@app.cls(image=image, gpu=GPU_SPEC, cpu=4, memory=32768, volumes={CACHE_DIR: hf_cache}, timeout=3600,
         scaledown_window=60, max_containers=MAX_CONTAINERS)
class Extractor:
    @modal.enter()
    def load(self):
        import shutil
        from concurrent.futures import ThreadPoolExecutor
        t0 = time.time()
        self.task_id = os.environ.get("MODAL_TASK_ID", "")
        print(f"[Extractor.load] enter task={self.task_id}", flush=True)
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.gpu_name = torch.cuda.get_device_name(0)
        print(f"[Extractor.load] torch imported gpu={self.gpu_name} t={time.time()-t0:.1f}s", flush=True)
        snap = Path(f"{CACHE_DIR}/hub/models--Qwen--Qwen3-8B/snapshots/{REVISION}")
        local_dir = Path("/tmp/qwen3-8b")
        if (snap / "config.json").exists():
            local_dir.mkdir(parents=True, exist_ok=True)
            files = [p for p in snap.iterdir() if not p.name.startswith(".")]
            def _cp(p: Path):
                dst = local_dir / p.name
                target = p.resolve()
                if not dst.exists() or dst.stat().st_size != target.stat().st_size:
                    shutil.copyfile(target, dst)
                return dst.stat().st_size
            with ThreadPoolExecutor(max_workers=4) as pool:
                tot_b = sum(pool.map(_cp, files))
            print(f"[Extractor.load] staged {len(files)} files ({tot_b/1e9:.2f} GB) to {local_dir} t={time.time()-t0:.1f}s", flush=True)
            src, kw = str(local_dir), {"local_files_only": True}
        else:
            src, kw = MODEL, {"revision": REVISION}
        self.tok = AutoTokenizer.from_pretrained(src, **kw)
        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                src, dtype=torch.bfloat16, attn_implementation="sdpa", device_map="cuda", **kw)
        except TypeError:
            self.model = AutoModelForCausalLM.from_pretrained(
                src, torch_dtype=torch.bfloat16, attn_implementation="sdpa", device_map="cuda", **kw)
        self.model.eval()
        self.pad_id = self.tok.pad_token_id if self.tok.pad_token_id is not None else 0
        self.load_s = time.time() - t0
        print(f"[Extractor.load] ready task={self.task_id} gpu={self.gpu_name} load_s={self.load_s:.1f}s", flush=True)
    def _forward(self, windows: list[list[int]]):
        """windows: list of token-id lists (<= MODEL_BATCH). Returns (mean[B,3,H], max[B,3,H]) float32 tensors on CPU."""
        torch = self.torch
        B = len(windows)
        T = max(len(w) for w in windows)
        ids = torch.full((B, T), self.pad_id, dtype=torch.long)
        mask = torch.zeros((B, T), dtype=torch.long)
        for b, w in enumerate(windows):
            ids[b, :len(w)] = torch.tensor(w, dtype=torch.long)
            mask[b, :len(w)] = 1
        ids, mask = ids.cuda(), mask.cuda()
        with torch.inference_mode():
            out = self.model(input_ids=ids, attention_mask=mask, output_hidden_states=True, use_cache=False)
            hs = torch.stack([out.hidden_states[lyr] for lyr in LAYERS], dim=1).float()  # [B,3,T,H]
            del out
            m = mask[:, None, :, None].bool()
            cnt = mask.sum(1).float()[:, None, None]
            mean = (hs * m).sum(2) / cnt
            mx = hs.masked_fill(~m, float("-inf")).amax(2)
            res = (mean.cpu(), mx.cpu())
            del hs, m
        return res

    @modal.method()
    def extract_batch(self, items: list[dict]) -> list[dict]:
        import numpy as np
        torch = self.torch
        t_start = time.time()
        results = []
        for it in items:
            t0 = time.time()
            ids, offs = tokenize_with_offsets(self.tok, it["text"])
            n_tok = len(ids)
            spans = select_windows(n_tok)
            n_win = len(spans)
            win_mean = np.zeros((n_win, 3, HIDDEN), dtype=np.float32)
            win_max = np.zeros((n_win, 3, HIDDEN), dtype=np.float32)
            win_steps = np.full((n_win, 2), -1, dtype=np.int32)
            win_tok = np.zeros((n_win,), dtype=np.int64)
            for k, (s, e) in enumerate(spans):
                cs, ce = offs[s][0], offs[e - 1][1]
                win_steps[k] = window_steps(cs, ce, it["step_spans"], it["step_ids"])
                win_tok[k] = e - s
            for k0 in range(0, n_win, MODEL_BATCH):
                chunk = spans[k0:k0 + MODEL_BATCH]
                mean, mx = self._forward([ids[s:e] for s, e in chunk])
                win_mean[k0:k0 + len(chunk)] = mean.numpy()
                win_max[k0:k0 + len(chunk)] = mx.numpy()
            w = win_tok.astype(np.float64) / max(1, win_tok.sum())
            run_mean = (win_mean.astype(np.float64) * w[:, None, None]).sum(0)
            run_max = win_max.max(0)
            results.append({
                "run_key": it["run_key"],
                "run_mean": run_mean.astype(np.float16),
                "run_max": run_max.astype(np.float16),
                "win_mean": win_mean.astype(np.float16),
                "win_max": win_max.astype(np.float16),
                "n_windows": int(n_win),
                "win_steps": win_steps,
                "win_tokens": win_tok.astype(np.int32),
                "n_tokens": int(n_tok),
                "secs": time.time() - t0,
            })
            del ids, offs
        torch.cuda.empty_cache()
        return [{"_call": {"task_id": self.task_id, "gpu_name": self.gpu_name, "load_s": self.load_s,
                           "secs": time.time() - t_start, "n_runs": len(items)}}] + results


# ----------------------------------------------------------------------------- local side
def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load_rows(runs_jsonl: Path, set_name: str, limit):
    rows = []
    with open(runs_jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("set") == set_name:
                rows.append(r)
    if limit:
        rows = rows[:limit]
    return rows


def _view_path(row: dict, views_dir: Path, set_name: str) -> Path:
    v = row.get("view")
    if v:
        p = Path(v)
        if not p.is_absolute():
            p = views_dir / p
        if p.exists():
            return p
    nid = row.get("native_id") or row["run_key"].split(":", 1)[-1]
    return views_dir / set_name / f"{nid}.json"


def _ledger(what: str, est_usd: float, receipt: dict):
    state = Path.home() / ".local/state/evallab-ov1104"
    state.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    amt = round(est_usd, 4)
    line = {"ts": ts, "lane": "probes", "what": what, "provider": "modal",
            "est_usd": amt, "actual_usd": amt, "receipt": receipt}
    with open(state / "spend.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    sp = state / "probes-spend.json"
    d = json.loads(sp.read_text()) if sp.exists() else {"cap_usd": 8, "est_usd": 0.0, "actual_usd": 0.0, "items": []}
    d["items"].append(line)
    d["est_usd"] = round(sum(i.get("est_usd") or 0 for i in d["items"]), 4)
    d["actual_usd"] = round(sum(i.get("actual_usd") or 0 for i in d["items"]), 4)
    sp.write_text(json.dumps(d, indent=1))
    return line

def assemble(partial_dir: Path, run_keys: list[str], out_npz: Path, meta: dict) -> int:
    import numpy as np
    keep = [rk for rk in run_keys if (partial_dir / f"{_sha16(rk)}.npz").exists()]
    N = len(keep)
    run_mean = np.zeros((N, 3, HIDDEN), np.float16)
    run_max = np.zeros((N, 3, HIDDEN), np.float16)
    win_mean = np.zeros((N, MAX_WINDOWS, 3, HIDDEN), np.float16)
    win_max = np.zeros((N, MAX_WINDOWS, 3, HIDDEN), np.float16)
    n_windows = np.zeros((N,), np.int16)
    win_steps = np.full((N, MAX_WINDOWS, 2), -1, np.int32)
    win_tokens = np.zeros((N, MAX_WINDOWS), np.int32)
    n_tokens = np.zeros((N,), np.int32)
    for j, rk in enumerate(keep):
        z = np.load(partial_dir / f"{_sha16(rk)}.npz")
        nw = int(z["n_windows"])
        run_mean[j], run_max[j] = z["run_mean"], z["run_max"]
        win_mean[j, :nw], win_max[j, :nw] = z["win_mean"], z["win_max"]
        n_windows[j], n_tokens[j] = nw, int(z["n_tokens"])
        win_steps[j, :nw], win_tokens[j, :nw] = z["win_steps"], z["win_tokens"]
    out_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out_npz, run_keys=np.array(keep, dtype=str), run_mean=run_mean, run_max=run_max,
             win_mean=win_mean, win_max=win_max, n_windows=n_windows, win_steps=win_steps,
             win_tokens=win_tokens, n_tokens=n_tokens, meta=json.dumps(meta))
    return N


def _run_one_set(ex, views_dir: Path, runs_jsonl: Path, set_name: str, out_npz: Path,
                 limit: int = 0, dry_run: bool = False, seen_tasks: set | None = None):
    import numpy as np
    from render import render_view, step_ids

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = _load_rows(runs_jsonl, set_name, limit)
    print(f"[run_set] set={set_name} rows={len(rows)} gpu={GPU} dry_run={dry_run}", flush=True)
    items, run_keys, missing, empty = [], [], 0, 0
    for r in rows:
        p = _view_path(r, views_dir, set_name)
        if not p.exists():
            missing += 1
            continue
        view = json.loads(p.read_text(encoding="utf-8"))
        rk = view.get("run_key") or r.get("run_key")
        text, spans = render_view(view)
        if not text.strip():
            empty += 1
            continue
        items.append({"run_key": rk, "text": text, "step_spans": [list(s) for s in spans], "step_ids": step_ids(view)})
        run_keys.append(rk)
    print(f"[run_set] set={set_name} rendered={len(items)} missing_views={missing} empty_views={empty} chars_total={sum(len(i['text']) for i in items)}", flush=True)

    if dry_run:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(MODEL, revision=REVISION)
        toks = [len(tok(i["text"], add_special_tokens=False, verbose=False)["input_ids"]) for i in items]
        wins = [len(select_windows(t)) for t in toks]
        tot_kept = sum(min(t, w * WINDOW) for t, w in zip(toks, wins, strict=False))
        print(json.dumps({"set": set_name, "runs": len(items), "tokens_total": sum(toks), "tokens_kept": tot_kept,
                          "windows_total": sum(wins), "tokens_max": max(toks, default=0),
                          "runs_truncated": sum(1 for t in toks if t > WINDOW * MAX_WINDOWS),
                          "hist_windows": {str(k): wins.count(k) for k in sorted(set(wins))}}), flush=True)
        return

    partial = Path(str(out_npz) + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    todo = [i for i in items if not (partial / f"{_sha16(i['run_key'])}.npz").exists()]
    todo.sort(key=lambda i: -len(i["text"]))
    print(f"[run_set] set={set_name} already_done={len(items) - len(todo)} todo={len(todo)}", flush=True)
    calls, gpu_secs, n_done, tok_done = [], 0.0, 0, 0
    t0 = time.time()
    if todo:
        batches = [todo[k:k + RUNS_PER_CALL] for k in range(0, len(todo), RUNS_PER_CALL)]
        for res in ex.extract_batch.map(batches, order_outputs=False):
            call = res[0]["_call"]
            calls.append(call)
            gpu_secs += call["secs"]
            for r in res[1:]:
                np.savez(partial / f"{_sha16(r['run_key'])}.npz",
                         **{k: v for k, v in r.items() if k not in ("run_key", "secs")})
                n_done += 1
                tok_done += r["n_tokens"]
            print(f"[run_set] set={set_name} done={n_done}/{len(todo)} tokens={tok_done} gpu_secs={gpu_secs:.0f} wall={time.time() - t0:.0f}s", flush=True)
        del todo, items, batches
    tasks = sorted({c["task_id"] for c in calls})
    new_tasks = [t for t in tasks if seen_tasks is None or t not in seen_tasks]
    if seen_tasks is not None:
        seen_tasks.update(tasks)
    load_s = max((c["load_s"] for c in calls), default=0.0)
    gpu_name = calls[0]["gpu_name"] if calls else None
    # billed estimate: processing time + per new container (load + 60 s scaledown)
    est_secs = gpu_secs + len(new_tasks) * (load_s + 60.0)
    price = _price_for_gpu(gpu_name)
    est_usd = est_secs * price
    wall_s = round(time.time() - t0, 1)
    meta = {"model": MODEL, "revision": REVISION, "layers": list(LAYERS), "window": WINDOW,
            "max_windows": MAX_WINDOWS, "render_sha": _sha_file(HERE / "render.py"),
            "extract_sha": _sha_file(Path(__file__).resolve()), "gpu": GPU,
            "gpu_name": gpu_name, "modal_app": APP_NAME,
            "started_utc": started, "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "set": set_name, "n_calls": len(calls), "n_containers": len(tasks),
            "n_new_containers": len(new_tasks), "gpu_proc_secs": round(gpu_secs, 1),
            "wall_secs": wall_s, "est_billed_secs": round(est_secs, 1),
            "usd_per_gpu_sec": price, "est_usd": round(est_usd, 4)}
    N = assemble(partial, run_keys, out_npz, meta)
    print(f"[run_set] wrote {out_npz} N={N} est_usd={est_usd:.4f} containers={len(tasks)} (new={len(new_tasks)}) load_s={load_s:.0f} wall_s={wall_s}", flush=True)
    if calls:
        line = _ledger(f"extract {set_name} runs={n_done} tokens={tok_done} gpu={GPU}", est_usd,
                       {"app": APP_NAME, "tasks": tasks, "new_tasks": new_tasks, "gpu_proc_secs": round(gpu_secs, 1),
                        "wall_secs": wall_s, "est_billed_secs": round(est_secs, 1), "usd_per_gpu_sec": price, "out": str(out_npz)})
        print(f"[run_set] ledger: {json.dumps(line)}", flush=True)


@app.local_entrypoint()
def run_set(views_dir: str, runs_jsonl: str, set_name: str, out_npz: str,
            limit: int = 0, dry_run: bool = False, prefetch: bool = False):
    if prefetch and not dry_run:
        pf = prefetch_weights.remote()
        print(f"[run_set] prefetch bytes={pf['bytes']} secs={pf['secs']:.1f} cached={pf.get('cached')}", flush=True)
    sets = [s.strip() for s in set_name.split(",") if s.strip()]
    ex = None if dry_run else Extractor()
    seen_tasks: set[str] = set()
    for s in sets:
        if len(sets) == 1 and out_npz.endswith(".npz"):
            target = Path(out_npz)
        else:
            target = Path(out_npz) / f"{s}.npz"
        _run_one_set(ex, Path(views_dir), Path(runs_jsonl), s, target, limit=limit, dry_run=dry_run, seen_tasks=seen_tasks)
