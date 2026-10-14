"""Baseline rows for combined_v1 on the QA-Trace answer keys (``qa.answer_key/v1``).

Keys: ``trace-paired-v1`` and ``trajectory-judge-400`` (``research/answer-keys/``).
Writes one row per (key, system, split) to ``research/qa-scoreboard/scoreboard.csv``
(replacing earlier rows with the same triple) and the per-item decisions to
``research/qa-scoreboard/combined_v1_baseline/<key>.jsonl``.

combined_v1 has no live judge (``evallab.detectors.CombinedV1``), so it is scored
two ways, both $0 and offline:

- ``combined_v1@oof``: the cached out-of-fold verdict of the row's own detectors-v1
  source (every trace-paired-v1 model trace is a ``contamination-audit`` row), i.e.
  held out from the fold that fit it;
- ``combined_v1@deploy-agentic``: the documented route for a trace from a source it
  was not tuned on: the ``cb`` 5% deployable rule in
  ``research/experiments/combined-detector/thresholds.json``, on components
  recomputed now from the trial (Rules, ContentRules, copy confirmation) plus the
  cached Sol score where one exists. The Sol literal is a cascade: an unjudged
  trace counts as not flagged by Sol, exactly as deployed.

A trace with no ATIF trajectory (oracle, scripted CheatAgent) is ``not_measured``:
it counts in ``n_items`` but never in recall, false alarms or pairs. Metrics:
recall on ``expected_verdict: fail`` items, false-alarm rate on ``pass`` items,
paired discrimination = mean(flag(child) - flag(parent)) over pairs with both
measured. trajectory-judge-400 also gets reference rows recomputed from upstream's
released raw verdicts (five judges; not ours), which double as a scorer check
against upstream ``analysis/paired.json``.

Usage (worktree root): ``uv run python research/qa-scoreboard/combined_v1_baseline.py``
"""

from __future__ import annotations

import csv
import datetime
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
KEYS = WORKTREE / "research/answer-keys"
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "research/experiments/combined-detector"))

import tune  # noqa: E402

from evallab.detectors import (  # noqa: E402
    UPSTREAM_FETCH_CONFIRMED_COPY,
    ContentRules,
    Corpus,
    Rules,
    upstream_fetch_confirmed_copy,
)
from evallab.results_home import results_root  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

SCOREBOARD = HERE / "scoreboard.csv"
COLUMNS = (
    "date_utc",
    "key_id",
    "split",
    "category",
    "agent",
    "system",
    "recall",
    "false_alarm_rate",
    "root_cause_match",
    "paired_discrimination",
    "cost_per_item_usd",
    "n_items",
    "n_measured",
    "n_pairs",
    "n_pairs_measured",
    "status",
    "notes",
)
THRESHOLDS = json.loads(
    (WORKTREE / "research/experiments/combined-detector/thresholds.json").read_text()
)
DEPLOY_RULE = [
    tuple(lit)
    for lit in THRESHOLDS["per_source"][THRESHOLDS["group_default"]["agentic"]]["fpr<=5%"]
]


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def components(trial_dir: Path) -> dict:
    feat: dict = {}
    for sub in tune._parse_fired(Rules().judge({}, trial_dir)["explanation"]):
        feat[f"rules:{sub}"] = True
    for sub in tune._parse_fired(ContentRules().judge({}, trial_dir)["explanation"]):
        feat[f"content:{sub}"] = True
    if (
        feat.get("rules:upstream_fetch_attempt")
        and upstream_fetch_confirmed_copy(trial_dir, True)["confirmed"]
    ):
        feat[UPSTREAM_FETCH_CONFIRMED_COPY] = True
    return feat


def deploy(feat: dict) -> tuple[bool, list[str]]:
    return tune.decide(DEPLOY_RULE, feat)


def metrics(items: list[dict], pairs: list[dict], flags: dict[str, bool | None]) -> dict:
    fail = [
        i for i in items if i["expected_verdict"] == "fail" and flags.get(i["item_id"]) is not None
    ]
    ok = [
        i for i in items if i["expected_verdict"] == "pass" and flags.get(i["item_id"]) is not None
    ]
    measured_pairs = [
        p for p in pairs if flags.get(p["child"]) is not None and flags.get(p["parent"]) is not None
    ]

    def rate(xs: list[dict]) -> float | None:
        return round(sum(bool(flags[i["item_id"]]) for i in xs) / len(xs), 4) if xs else None

    deltas = [int(bool(flags[p["child"]])) - int(bool(flags[p["parent"]])) for p in measured_pairs]
    return {
        "recall": rate(fail),
        "false_alarm_rate": rate(ok),
        "paired_discrimination": round(sum(deltas) / len(deltas), 4) if deltas else None,
        "n_items": len(items),
        "n_measured": len(fail) + len(ok),
        "n_pairs": len(pairs),
        "n_pairs_measured": len(measured_pairs),
        "n_fail_measured": len(fail),
        "n_pass_measured": len(ok),
    }


def rows_for(
    key: str, system: str, agent: str, items, pairs, flags, *, status, notes, cost, root_cause=None
):
    """One row per split (dev/test/all) over every category, plus one ``all`` row per
    child category (its children and the parents paired with them)."""
    slices = [(split, "") for split in ("dev", "test", "all")]
    slices += [("all", c) for c in sorted({p["category"] for p in pairs})]
    out = []
    for split, category in slices:
        its = [i for i in items if split == "all" or i["split"] == split]
        if category:
            prs = [p for p in pairs if p["category"] == category]
            keep = {p["child"] for p in prs} | {p["parent"] for p in prs}
            its = [i for i in its if i["item_id"] in keep]
        ids = {i["item_id"] for i in its}
        prs = [
            p for p in pairs if p["child"] in ids and (not category or p["category"] == category)
        ]
        m = metrics(its, prs, flags)
        rc = root_cause(its) if root_cause else "n/a"
        out.append(
            {
                "date_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "key_id": key,
                "split": split,
                "category": category or "all",
                "agent": agent,
                "system": system,
                "recall": m["recall"],
                "false_alarm_rate": m["false_alarm_rate"],
                "root_cause_match": rc,
                "paired_discrimination": m["paired_discrimination"],
                "cost_per_item_usd": cost,
                "n_items": m["n_items"],
                "n_measured": m["n_measured"],
                "n_pairs": m["n_pairs"],
                "n_pairs_measured": m["n_pairs_measured"],
                "status": status,
                "notes": notes
                + f" (fail measured {m['n_fail_measured']}, pass measured {m['n_pass_measured']})",
            }
        )
    return out


def resolve(ref: dict) -> Path:
    root = results_root() if ref["kind"] == "published" else shared_checkout_root(WORKTREE)
    return root / ref["path"]


def score_trace_paired() -> list[dict]:
    key = "trace-paired-v1"
    items = read_jsonl(KEYS / key / "items.jsonl")
    pairs = read_jsonl(KEYS / key / "pairs.jsonl")
    corpus = Corpus.load("detectors-v1")
    oof = corpus.cached("combined_v1")
    sol = corpus.cached("sol_codex")
    oof_flags: dict[str, bool | None] = {}
    dep_flags: dict[str, bool | None] = {}
    decisions = []
    for item in items:
        trial = item["subject"]["trial_id"]
        row_id = f"ca-{trial}"
        trial_dir = resolve(item["trial_ref"])
        rec = {
            "item_id": item["item_id"],
            "expected_verdict": item["expected_verdict"],
            "has_atif": item["has_atif"],
        }
        if not item["has_atif"] or item["trace_kind"] != "model":
            oof_flags[item["item_id"]] = dep_flags[item["item_id"]] = None
            rec["status"] = "not_measured: no ATIF trajectory (oracle or scripted CheatAgent)"
            decisions.append(rec)
            continue
        cached = oof.get(row_id)
        oof_flags[item["item_id"]] = bool(cached["flagged"]) if cached else None
        feat = components(trial_dir)
        score = (sol.get(row_id) or {}).get("score")
        if isinstance(score, int | float):
            feat["sol:sol_codex"] = float(score)
        flag, fired = deploy(feat)
        dep_flags[item["item_id"]] = flag
        rec.update(
            {
                "status": "measured",
                "oof_flagged": oof_flags[item["item_id"]],
                "oof_fired": cached["flagged"] if cached else None,
                "deploy_flagged": flag,
                "deploy_fired": fired,
                "components": feat,
                "sol_judged": "sol:sol_codex" in feat,
            }
        )
        decisions.append(rec)
    out_dir = HERE / "combined_v1_baseline"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{key}.jsonl").write_text(
        "".join(json.dumps(d, sort_keys=True) + "\n" for d in decisions)
    )
    n_sol = sum(1 for d in decisions if d.get("sol_judged"))
    n_model = sum(1 for d in decisions if d["status"] == "measured")
    rows = rows_for(
        key,
        "combined_v1@oof",
        "QA Trace T0",
        items,
        pairs,
        oof_flags,
        status="measured on model traces; oracle/CheatAgent not measured (no ATIF)",
        notes="cached out-of-fold verdict of detectors-v1 source contamination-audit at the 5% point; not tuned on this key",
        cost=0.0,
    )
    rows += rows_for(
        key,
        "combined_v1@deploy-agentic",
        "QA Trace T0",
        items,
        pairs,
        dep_flags,
        status="measured on model traces; oracle/CheatAgent not measured (no ATIF)",
        notes=f"cb 5% deployable rule {[lit[1] for lit in DEPLOY_RULE]} on components recomputed at origin/main; Sol cached for {n_sol}/{n_model} model traces (cascade)",
        cost=0.0,
    )
    return rows


def to_atif(row: dict) -> dict:
    steps = [{"step_id": 0, "source": "user", "message": row["goal"]}]
    for step in row["steps"]:
        call = step.get("call") or {}
        steps.append(
            {
                "step_id": step["index"] + 1,
                "source": "agent",
                "message": step.get("thought") or "",
                "tool_calls": [
                    {"function_name": call.get("tool"), "arguments": call.get("args") or {}}
                ]
                if call
                else [],
                "observation": {
                    "results": [{"content": json.dumps(step.get("observation"), sort_keys=True)}]
                },
            }
        )
    steps.append(
        {"step_id": len(steps), "source": "agent", "message": row.get("final_answer") or ""}
    )
    return {"schema_version": "ATIF-v1.2", "steps": steps}


def score_trajectory_judge() -> list[dict]:
    key = "trajectory-judge-400"
    items = read_jsonl(KEYS / key / "items.jsonl")
    pairs = read_jsonl(KEYS / key / "pairs.jsonl")
    upstream = {
        r["trajectory_id"]: r for r in read_jsonl(KEYS / key / "upstream/trajectories.jsonl")
    }
    flags: dict[str, bool | None] = {}
    decisions = []
    with tempfile.TemporaryDirectory(prefix="tj-atif-") as tmp:
        for item in items:
            tid = item["subject"]["trial_id"]
            trial_dir = Path(tmp) / tid
            (trial_dir / "agent").mkdir(parents=True)
            (trial_dir / "verifier").mkdir()
            (trial_dir / "agent/trajectory.json").write_text(json.dumps(to_atif(upstream[tid])))
            feat = components(trial_dir)
            flag, fired = deploy(feat)
            flags[item["item_id"]] = flag
            decisions.append(
                {
                    "item_id": item["item_id"],
                    "deploy_flagged": flag,
                    "deploy_fired": fired,
                    "components": feat,
                }
            )
    out_dir = HERE / "combined_v1_baseline"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{key}.jsonl").write_text(
        "".join(json.dumps(d, sort_keys=True) + "\n" for d in decisions)
    )
    rows = rows_for(
        key,
        "combined_v1@deploy-agentic",
        "QA Trace T0",
        items,
        pairs,
        flags,
        status="partial: rules+content legs measured; Sol leg not run (no cached verdicts, cascade counts it not flagged)",
        notes="trajectories converted to ATIF tool calls; cb 5% deployable rule",
        cost=0.0,
    )
    # Reference: upstream's five judges, recomputed from its released raw verdicts.
    verdicts = read_jsonl(KEYS / key / "upstream/verdicts.jsonl")
    by_item = {i["subject"]["trial_id"]: i for i in items}
    for judge in sorted({v["judge_id"] for v in verdicts}):
        jflags = {}
        jtype = {}
        for v in verdicts:
            if v["judge_id"] != judge:
                continue
            item = by_item[v["trajectory_id"]]
            jflags[item["item_id"]] = None if v["error"] else bool(v["faulty"])
            jtype[item["item_id"]] = v["failure_type"]

        def root_cause(its: list[dict], jflags=jflags, jtype=jtype) -> float | None:
            hit = [i for i in its if i["expected_verdict"] == "fail" and jflags.get(i["item_id"])]
            if not hit:
                return None
            return round(
                sum(jtype[i["item_id"]] == i["expected_root_cause"]["failure_type"] for i in hit)
                / len(hit),
                4,
            )

        rows += rows_for(
            key,
            f"reference: trajectory-judge {judge}",
            "reference (published, not ours)",
            items,
            pairs,
            jflags,
            status="recomputed offline from upstream raw verdicts",
            notes="upstream August run (Ollama); root_cause_match = failure_type exact on flagged faults",
            cost="n/a",
            root_cause=root_cause,
        )
    return rows


def write(rows: list[dict]) -> None:
    keep = []
    if SCOREBOARD.is_file():
        with SCOREBOARD.open(newline="") as fh:
            new = {(r["key_id"], r["system"], r["split"], r["category"]) for r in rows}
            keep = [
                r
                for r in csv.DictReader(fh)
                if (r["key_id"], r["system"], r["split"], r.get("category", "all")) not in new
            ]
    with SCOREBOARD.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(keep + rows)


def main() -> None:
    rows = score_trace_paired() + score_trajectory_judge()
    write(rows)
    for r in rows:
        if r["split"] == "all" or r["system"].startswith("combined"):
            print(
                f"{r['key_id']:22s} {r['split']:4s} {r['category'][:14]:14s} {r['system'][:40]:40s} rec={r['recall']} fa={r['false_alarm_rate']} "
                f"pd={r['paired_discrimination']} rc={r['root_cause_match']} meas={r['n_measured']}/{r['n_items']} "
                f"pairs={r['n_pairs_measured']}/{r['n_pairs']}"
            )


if __name__ == "__main__":
    main()
