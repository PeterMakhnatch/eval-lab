"""DetBench DEV half: replay-v1 690 runs -> label-free views, runs rows, $0 baseline signals.

Run from the eval-lab worktree:
    uv run python research/experiments/ov1104-detbench/ingest_dev/ingest.py

Inputs are read-only (replay-v1 set + its baseline batch). Outputs:
  - views under /private/tmp/ov1104/shared/detbench/views/{full,actions_only}/<set>/
  - signals under /private/tmp/ov1104/shared/detbench/signals/
  - runs rows to /private/tmp/ov1104/detbench/workers/dev/runs.jsonl (lead merges)
  - ingest-summary.json next to runs.jsonl (counts for REPORT.md)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import detbench_lib as dl  # noqa: E402

from evallab import hack_drop as hd  # noqa: E402

REPLAY = Path("/Users/petermakhnatch/Developer/eval-lab-results/line/replay-v1")
BASELINE_BATCH = REPLAY / "batches/replay-v1-replay-f4ea98c6ccd94192a4f5ef19a29c521e"
WORKER = Path("/private/tmp/ov1104/detbench/workers/dev")
CORPORA = ("c1c5", "cheatbench", "goodfire", "sol-astra", "terminal-wrench")
VERSION = "ingest-dev@" + hashlib.sha256(HERE.read_bytes()).hexdigest()[:12]

SOURCE_FAMILY = {
    "terminal-wrench": "terminal-bench-wrench",
    "cheatbench": "cheatbench",
    "goodfire": "goodfire-impossiblebench",
    "c1c5": "lab-constructed-c1c5",
    "sol-astra": "lab-sol-astra",
}
# Detectors/lanes that already read each corpus (basis quoted in REPORT.md).
READ_BY_COMMON = ["line-replay-v1", "autoreview-cached", "hack-drop-c2-20261102", "l0formats-replay"]
READ_BY = {
    "terminal-wrench": READ_BY_COMMON
    + ["tripwires-rules", "content-rules", "kev-4b", "combined-v1", "sol-codex", "metr-scout", "harbor-analyze",
       "laminar-signals", "l0-prefix-gate-1694", "autoreview-stage1-reread"],
    "cheatbench": READ_BY_COMMON
    + ["tripwires-rules", "codeban", "live-watch", "kev-4b", "kev-stack-lr", "action-features-lr", "combined-v1",
       "cascade-v8", "facets", "autoreview-dt15", "flag-cheating"],
    "goodfire": READ_BY_COMMON
    + ["cascade-v8", "facets", "haiku-reader", "length-baseline", "autoreview-masked", "flag-cheating"],
    "c1c5": READ_BY_COMMON + ["autoreview-monitor-v2", "evallab-harden"],
    "sol-astra": READ_BY_COMMON + ["autoreview-structured", "flat-masked-baseline"],
}


def _is_scripted(native_id: str) -> bool:
    return "__scripted__" in native_id


def label_type_and_natural(corpus: str, native_id: str) -> tuple[str, bool | None]:
    if corpus == "terminal-wrench":
        return "judge", False  # hack-elicitation prompt; LLM judge + manual spot-check (paper 2604.17596 §3)
    if corpus == "cheatbench":
        return "dataset", False
    if corpus == "goodfire":
        return "dataset", True
    if corpus in ("c1c5", "sol-astra"):
        if _is_scripted(native_id):
            return "constructed", False
        return "executed", True  # live unprompted rollouts, container-graded
    raise ValueError(corpus)


def _reward_from_dir(path: Path) -> float | None:
    """Verifier reward from run.json / grade.json / reward.txt; nothing else is read from grade.json."""
    if not path.is_dir():
        return None
    rt = path / "reward.txt"
    if rt.is_file():
        try:
            return float(rt.read_text().strip())
        except ValueError:
            pass
    for name, keys in (("run.json", ("reward",)), ("grade.json", ("reward", "reward_L0"))):
        f = path / name
        if not f.is_file():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        for k in keys:
            v = doc.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def _reason_file(reason: str) -> str:
    return "line_l0_" + re.sub(r"[^a-z0-9]", "_", reason.lower()) + ".jsonl"


def main() -> int:
    WORKER.mkdir(parents=True, exist_ok=True)
    dl.SIGNALS.mkdir(parents=True, exist_ok=True)

    decisions = {d["id"]: d for d in dl.read_jsonl(BASELINE_BATCH / "decisions.jsonl")}
    cached: dict[str, dict[str, Any]] = {}
    for name in ("dropped", "kept", "review"):
        for row in dl.read_jsonl(REPLAY / "inputs/cached-autoreview" / f"{name}.jsonl"):
            cached[row["id"]] = row

    runs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    summary: dict[str, Any] = {"version": VERSION, "corpora": {}, "loader_failures": failures}
    sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
    l0_reasons: set[str] = set()
    for d in decisions.values():
        l0_reasons.update(r for r in d["reasons"] if r.startswith("L0_"))

    for corpus in CORPORA:
        manifest = dl.read_jsonl(REPLAY / f"inputs/corpora/{corpus}-manifest.jsonl")
        labels = {r["id"]: bool(r["is_cheat"]) for r in dl.read_jsonl(REPLAY / f"inputs/corpora/{corpus}-labels.jsonl")}
        cs: dict[str, Any] = {"runs": 0, "cheats": 0, "honest": 0, "clusters": set(), "label_type": Counter(), "natural": Counter(), "reward_present": 0, "empty_evidence": []}
        for row in manifest:
            native_id, path, task_id = row["id"], Path(row["path"]), str(row["task_id"])
            run_key = f"{corpus}:{native_id}"
            try:
                item = hd._load_single_path_rollout(path)
            except Exception as exc:  # noqa: BLE001 - report, keep going
                failures.append({"run_key": run_key, "path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                continue
            # Bundles without an ATIF trajectory (the line's L0_NO_EVIDENCE case) still get a row so every
            # signal file covers all 690 run_keys; their view is empty (no steps) and carries no reward,
            # because with zero content the verifier reward would be the only field and it equals the label.
            empty = item.run is None
            steps = [] if empty else item.run.doc["steps"]
            diff = item.diff_text or None
            reward = None if (empty and not diff) else _reward_from_dir(path)
            view = dl.build_view(run_key, steps, diff=diff, reward=reward)
            full, acts = dl.write_views(corpus, native_id, view)
            n_steps, n_chars = dl.view_size(view)
            lt, nat = label_type_and_natural(corpus, native_id)
            label = int(labels[native_id])
            extra = {"empty_view": True} if empty and not diff else None
            if extra:
                cs["empty_evidence"].append(native_id)
            runs.append(
                dl.runs_row(
                    set_id=corpus, native_id=native_id, split="dev", label=label, label_type=lt, natural=nat,
                    cluster=task_id, source_family=SOURCE_FAMILY[corpus], view_full=full, view_actions=acts,
                    n_steps=n_steps, n_chars=n_chars, read_by=list(READ_BY[corpus]), extra=extra,
                )
            )
            cs["runs"] += 1
            cs["cheats" if label else "honest"] += 1
            cs["clusters"].add(task_id)
            cs["label_type"][lt] += 1
            cs["natural"][str(nat)] += 1
            cs["reward_present"] += reward is not None

            # ---- $0 baseline signals ----
            sig["length_steps"].append(dl.signal_row(run_key, "length_steps", VERSION, float(n_steps)))
            sig["length_chars"].append(dl.signal_row(run_key, "length_chars", VERSION, float(n_chars)))
            dec = decisions.get(native_id)
            if dec is None:
                for name in ("line_replay_decision", "line_l0_any_hard", "line_l0_unrecognized_shape"):
                    sig[name].append(dl.signal_row(run_key, name, VERSION, None, abstain="no_baseline_decision"))
                for reason in l0_reasons:
                    name = _reason_file(reason)[:-6]
                    sig[name].append(dl.signal_row(run_key, name, VERSION, None, abstain="no_baseline_decision"))
            else:
                reasons = set(dec["reasons"])
                ev = [int(i) for i in dec.get("evidence_steps") or []]
                sig["line_replay_decision"].append(dl.signal_row(
                    run_key, "line_replay_decision", VERSION, {"drop": 1.0, "review": 0.5, "keep": 0.0}[dec["decision"]], evidence_steps=ev))
                sig["line_l0_any_hard"].append(dl.signal_row(
                    run_key, "line_l0_any_hard", VERSION, float(any(r.startswith("L0_HARD_") for r in reasons)), evidence_steps=ev))
                sig["line_l0_unrecognized_shape"].append(dl.signal_row(
                    run_key, "line_l0_unrecognized_shape", VERSION, float("L0_UNRECOGNIZED_TOOL_SHAPE" in reasons)))
                for reason in l0_reasons:
                    name = _reason_file(reason)[:-6]
                    sig[name].append(dl.signal_row(run_key, name, VERSION, float(reason in reasons), evidence_steps=ev if reason in reasons else None))
            # AutoReview: prefer the baseline batch's stage record, fall back to the cached readings file.
            ar = None
            for src in (dec, cached.get(native_id)):
                if src is None:
                    continue
                st = (src.get("stage_decisions") or {}).get("autoreview") or {}
                det = st.get("details") or {}
                if isinstance(det.get("suspicion"), (int, float)):
                    ar = (float(det["suspicion"]), det.get("model"), det.get("attested"), [int(i) for i in src.get("evidence_steps") or []])
                    break
            if ar is None:
                sig["autoreview_cached"].append(dl.signal_row(run_key, "autoreview_cached", VERSION, None, abstain="not_eligible"))
                sig["autoreview_cached_fill0"].append(dl.signal_row(run_key, "autoreview_cached_fill0", VERSION, 0.0))
            else:
                score, model, attested, ev = ar
                if attested is False:
                    sig["autoreview_cached"].append(dl.signal_row(run_key, "autoreview_cached", VERSION, None, abstain="not_attested", model=model))
                    sig["autoreview_cached_fill0"].append(dl.signal_row(run_key, "autoreview_cached_fill0", VERSION, 0.0, model=model))
                else:
                    sig["autoreview_cached"].append(dl.signal_row(run_key, "autoreview_cached", VERSION, score, evidence_steps=ev, model=model))
                    sig["autoreview_cached_fill0"].append(dl.signal_row(run_key, "autoreview_cached_fill0", VERSION, score, evidence_steps=ev, model=model))

        cs["clusters"] = len(cs["clusters"])
        cs["label_type"] = dict(cs["label_type"])
        cs["natural"] = dict(cs["natural"])
        summary["corpora"][corpus] = cs

    dl.write_jsonl(WORKER / "runs.jsonl", runs)
    summary["runs_written"] = len(runs)
    summary["signals"] = {}
    for name, rows in sorted(sig.items()):
        n = dl.write_jsonl(dl.SIGNALS / f"{name}.jsonl", rows)
        summary["signals"][name] = {"rows": n, "non_null": sum(r["score"] is not None for r in rows), "positives": sum(1 for r in rows if r["score"] not in (None, 0.0))}
    (WORKER / "ingest-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
