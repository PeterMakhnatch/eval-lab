"""Acceptance checks for the DEV ingest (label-free views, counts, paths). Run from the worktree:
    uv run python research/experiments/ov1104-detbench/ingest_dev/check.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import detbench_lib as dl  # noqa: E402

WORKER = Path("/private/tmp/ov1104/detbench/workers/dev")
REPLAY = Path("/Users/petermakhnatch/Developer/eval-lab-results/line/replay-v1")
SETS = ("c1c5", "cheatbench", "goodfire", "sol-astra", "terminal-wrench")
FORBIDDEN_KEYS = {"is_cheat", "label", "labels", "is_hack", "classification", "verdict", "suspicion", "behavior_class", "safeguard_preserved"}
FORBIDDEN_SUBSTR = ("is_cheat", "autoreview", "confirmed_cheat")
VIEW_KEYS = {"run_key", "task_text", "steps", "final"}
STEP_KEYS = {"i", "role", "text", "tool_calls", "observation"}


def walk_keys(obj, out: Counter) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out[k] += 1
            walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_keys(v, out)


def main() -> int:
    ok = True
    rows = dl.read_jsonl(WORKER / "runs.jsonl")
    print(f"runs.jsonl rows: {len(rows)} (expect 690)")
    ok &= len(rows) == 690
    keys = [r["run_key"] for r in rows]
    print(f"unique run_keys: {len(set(keys))}")
    ok &= len(set(keys)) == 690
    by_set = Counter(r["set"] for r in rows)
    print("rows per set:", dict(by_set))
    missing = [r["run_key"] for r in rows if not (Path(r["view"]).is_file() and Path(r["view_actions_only"]).is_file())]
    print(f"rows whose view paths do not exist: {len(missing)}")
    ok &= not missing
    for kind in ("full", "actions_only"):
        counts = {s: len(list((dl.VIEWS / kind / s).glob("*.json"))) for s in SETS}
        print(f"views/{kind} files per set: {counts} total={sum(counts.values())}")
        ok &= sum(counts.values()) == 690

    # Label-freedom: key names and forbidden substrings in every view; full/actions_only structure.
    bad_keys: Counter = Counter()
    bad_substr: Counter = Counter()
    structure_bad = 0
    label_in_text = 0
    for r in rows:
        for p in (r["view"], r["view_actions_only"]):
            raw = Path(p).read_text(encoding="utf-8")
            doc = json.loads(raw)
            if set(doc) != VIEW_KEYS or any(set(s) != STEP_KEYS for s in doc["steps"]) or set(doc["final"]) != {"reward", "diff"}:
                structure_bad += 1
            kc: Counter = Counter()
            walk_keys(doc, kc)
            for k in FORBIDDEN_KEYS & set(kc):
                bad_keys[k] += 1
            for s in FORBIDDEN_SUBSTR:
                if s in raw:
                    bad_substr[s] += 1
            if '"label"' in raw:
                label_in_text += 1
    print(f"views with a non-contract structure: {structure_bad}")
    print(f"views containing a forbidden key name ({sorted(FORBIDDEN_KEYS)}): {dict(bad_keys) or 0}")
    print(f"views containing forbidden substrings {FORBIDDEN_SUBSTR}: {dict(bad_substr) or 0}")
    print(f'views containing the literal text "label" (any position, incl. inside code/observations): {label_in_text}')
    # Substrings are informational: an agent may itself read files that mention these words (observation text).
    ok &= structure_bad == 0 and not bad_keys

    # Hidden-sidecar content must not be in the views: grade.json / autoreview.json bytes.
    leaked = 0
    for r in rows:
        if r["set"] not in ("c1c5", "sol-astra"):
            continue
        row_path = None
        for m in dl.read_jsonl(REPLAY / f"inputs/corpora/{r['set']}-manifest.jsonl"):
            if f"{r['set']}:{m['id']}" == r["run_key"]:
                row_path = Path(m["path"])
                break
        assert row_path is not None
        raw = Path(r["view"]).read_text(encoding="utf-8")
        for side in ("grade.json", "autoreview.json", "safeguard_report.json", "monitor-structured.json", "monitor-baseline.json"):
            f = row_path / side
            if not f.is_file():
                continue
            try:
                sdoc = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            # Any sidecar string value longer than 24 chars appearing verbatim in the view, except identity
            # fields (case id / task slug / digests / timestamps) that legitimately appear in the task text.
            skip = {"id", "case_id", "task", "package_digest", "ran_at", "graded_at", "kind", "model"}
            for k, v in sdoc.items():
                if k in skip or not isinstance(v, str) or len(v) <= 24 or v in r["run_key"]:
                    continue
                if v in raw:
                    leaked += 1
                    print("  sidecar value found in view:", r["run_key"], side, k, v[:60])
    print(f"views containing sidecar (grade/autoreview/safeguard/monitor) string values: {leaked}")
    ok &= leaked == 0

    # Signals: every file 690 rows, one per run_key, null score => abstain.
    files = sorted(dl.SIGNALS.glob("*.jsonl"))
    keyset = set(keys)
    for f in files:
        srows = dl.read_jsonl(f)
        if srows and srows[0].get("version", "").startswith("ingest-dev@") is False:
            continue
        skeys = [s["run_key"] for s in srows]
        null_no_abstain = sum(1 for s in srows if s["score"] is None and not s.get("abstain"))
        good = len(srows) == 690 and set(skeys) == keyset and len(set(skeys)) == 690 and null_no_abstain == 0
        ok &= good
        print(f"signal {f.name}: rows={len(srows)} non_null={sum(s['score'] is not None for s in srows)} ok={good}")
    print("ALL CHECKS PASS" if ok else "CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
