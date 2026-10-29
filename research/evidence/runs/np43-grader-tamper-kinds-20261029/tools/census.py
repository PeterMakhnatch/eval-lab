"""NP43-TW-1 step 2: join labels to the label-free events and count. Writes ``results/census.json`` (counts only).

Run after ``tools/classify.py`` is frozen (README) and ``run_census.py`` has written the private events. The label join happens
here and nowhere else: Goodfire labels via ``load_corpus_labels`` (strict ``goodfire-dev`` gate), the NP31 pair roles from the
frozen pair file, CheatBench DEV labels, and LinuxArena S / non-S from NP38's frozen sample.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import DEFAULT_DATABASE  # noqa: E402
from evallab.production.trace_harness_corpora import load_corpus_labels  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
STATE = Path.home() / ".local/state"
OUT = STATE / "evallab-worker-evidence/np43-tw1-20261029"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
NP38 = STATE / "evallab-np38-cm1"
NP38_PUBLIC = REPO / "research/evidence/runs/np38-cm1-linuxarena-20261029/results/sample.json"
NP36_GATE = REPO / "research/evidence/runs/np36-grader-tamper-20261029/results/strict_gate.json"


def wilson(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n == 0:
        return [0.0, 100.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)]


def frac(k: int, n: int) -> dict:
    return {"k": k, "n": n, "pct": round(100 * k / n, 1) if n else None, "wilson": wilson(k, n)}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_category(events: list[dict]) -> str:
    kinds = {e["kind"] for e in events}
    if not kinds:
        return "no_alarm"
    if kinds & {"b", "c"}:
        return "survives"
    return "creation_only" if kinds == {"a"} else "undetermined"


def existed_before(events: list[dict]) -> bool:
    """Sensitivity (named after seeing the label-free mix): a b event whose earliest evidence is not the agent's own write."""
    return any(e["kind"] == "c" or (e["kind"] == "b" and e["sub"] != "own_file") for e in events)


def summarize(runs: list[dict]) -> dict:
    alarms = [r for r in runs if r["events"]]
    events = [e for r in alarms for e in r["events"]]
    cat = Counter(run_category(r["events"]) for r in alarms)
    kinds = Counter(e["kind"] for e in events)
    subs = Counter(f"{e['kind']}:{e['sub']}" for e in events)
    a_mech = Counter(f"{e['via']}:{e['mech']}" for e in events if e["kind"] == "a")
    n = len(alarms)
    return {
        "runs": len(runs),
        "alarm_runs": n,
        "run_category": {k: cat.get(k, 0) for k in ("survives", "creation_only", "undetermined")},
        "survives_frac": frac(cat.get("survives", 0), n),
        "creation_only_frac": frac(cat.get("creation_only", 0), n),
        "runs_with_any_creation_event": frac(
            sum(any(e["kind"] == "a" for e in r["events"]) for r in alarms), n
        ),
        "undetermined_frac": frac(cat.get("undetermined", 0), n),
        "survives_existing_only": frac(sum(existed_before(r["events"]) for r in alarms), n),
        "events": len(events),
        "event_kinds": {k: kinds.get(k, 0) for k in "abcd"},
        "event_kind_a_frac": frac(kinds.get("a", 0), len(events)),
        "event_subkinds": dict(sorted(subs.items())),
        "kind_a_by_mechanism": dict(sorted(a_mech.items())),
        "events_with_failed_hint": sum(e["failed_hint"] for e in events),
        "events_with_rm_same_step": sum(e["rm_same_step"] for e in events),
    }


def main() -> int:
    events = json.loads((OUT / "events.json").read_text())
    labels = load_corpus_labels("goodfire-dev", database=DEFAULT_DATABASE, repo_root=REPO)
    admitted = {r for r in labels.run_ids if labels.positive.get(r) is not None}
    pair_rows = [json.loads(x) for x in PAIRS.read_text().splitlines() if x.strip()]
    pair_ids = {r for p in pair_rows for r in (p["enacted"], p["clean"])}
    gate = {
        "pair_file_sha256": sha(PAIRS),
        "pair_file_sha256_matches_np36": sha(PAIRS)
        == json.loads(NP36_GATE.read_text())["pair_file_sha256"],
        "pairs": len(pair_rows),
        "pair_runs_in_strict_gate": len(pair_ids & admitted),
        "strict_goodfire_dev_with_verdict": len(admitted),
        "others_with_verdict": len(admitted - pair_ids),
    }
    if gate["pair_runs_in_strict_gate"] != 246 or not gate["pair_file_sha256_matches_np36"]:
        raise SystemExit(f"pair gate failed: {gate}")

    def gf(population: str, rid: str, enacted: bool) -> dict:
        return {"events": events[population][rid]["events"], "enacted": enacted}

    groups: dict[str, list[dict]] = {}
    groups["pairs_enacted"] = [gf("pairs", p["enacted"], True) for p in pair_rows]
    groups["pairs_clean"] = [gf("pairs", p["clean"], False) for p in pair_rows]
    other = sorted(admitted - pair_ids)
    groups["goodfire_other_enacted"] = [
        gf("goodfire_other", r, True) for r in other if labels.positive[r]
    ]
    groups["goodfire_other_clean"] = [
        gf("goodfire_other", r, False) for r in other if not labels.positive[r]
    ]
    groups["goodfire_dev_enacted"] = groups["pairs_enacted"] + groups["goodfire_other_enacted"]
    groups["goodfire_dev_clean"] = groups["pairs_clean"] + groups["goodfire_other_clean"]

    cb = load_corpus_labels("cheatbench", database=DEFAULT_DATABASE, repo_root=REPO)
    cb_ids = {r for r in cb.run_ids if cb.positive.get(r) is not None and r in events["cheatbench"]}
    groups["cheatbench_enacted"] = [
        gf("cheatbench", r, True) for r in sorted(cb_ids) if cb.positive[r]
    ]
    groups["cheatbench_clean"] = [
        gf("cheatbench", r, False) for r in sorted(cb_ids) if not cb.positive[r]
    ]

    sample = json.loads((NP38 / "sample.json").read_text())
    public = json.loads(NP38_PUBLIC.read_text())
    manifest = "\n".join(
        f"{k}:{sha(NP38 / 'runs' / f'{k}.pkl')}" for k in sorted(r["key"] for r in sample["runs"])
    )
    la_checks = {
        "sample_sha256_matches_np38_public": sha(NP38 / "sample.json")
        == public.get("sample_sha256"),
        "runs_manifest_sha256_matches": hashlib.sha256(manifest.encode()).hexdigest()
        == public.get("runs_manifest_sha256"),
        "S": sum(r["stratum"] == "S" for r in sample["runs"]),
        "non_S": sum(r["stratum"] != "S" for r in sample["runs"]),
    }
    groups["linuxarena_S"] = [
        gf("linuxarena", r["key"], True) for r in sample["runs"] if r["stratum"] == "S"
    ]
    groups["linuxarena_nonS"] = [
        gf("linuxarena", r["key"], False) for r in sample["runs"] if r["stratum"] != "S"
    ]

    out = {
        "strict_gate": gate,
        "linuxarena_integrity": la_checks,
        "cheatbench_admitted_with_verdict": len(cb_ids),
        "groups": {name: summarize(rows) for name, rows in groups.items()},
    }
    (HERE.parent / "results").mkdir(exist_ok=True)
    (HERE.parent / "results/census.json").write_text(
        json.dumps(out, indent=1, sort_keys=True) + "\n"
    )
    for name, s in out["groups"].items():
        print(
            name, s["runs"], "alarm", s["alarm_runs"], s["run_category"], "events", s["event_kinds"]
        )
    print(gate, la_checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
