#!/usr/bin/env python3
"""LS-CHEAT triage, stage A: card inventory and the strict DEV cross-key. No label, no model call.

Reads only (a) the 154 cards' structural fields (run id, detector, version, reader, score, cited step numbers,
header flags; alarm *prose* is never parsed, only compared for equality), (b) the trace-harness findings store's
structural fields (run id, detector, version, reader, verdict, kind, score, steps), and (c) identity rows
(trace id, task id, dataset, split) plus the registry's TEST assignment check. Receipt labels are not read.
Per-run rows go to a private /tmp file, never into git; ``results/stage_a.json`` holds aggregates only
(cells below 3 suppressed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = Path(
    os.environ.get("EVALLAB_REPO")
    or subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)
sys.path.insert(0, str(REPO / "src"))
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True

from evallab.production import run_atlas as ra  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

STATE = Path.home() / ".local/state/evallab-prod-live"
CARDS = STATE / "live-supervisor/cheat-cards"
INDEX = CARDS / "days/2026-10-29.md"
FINDINGS = STATE / "trace-harness/findings-v1.jsonl"
SAMPLE = (
    Path.home() / "Developer/research-context/bridge/2026-10-25-systems-and-swarms"
    / "raw/cm2-monitor-20261026/goodfire-sample.json"
)
PRIVATE = Path("/tmp/ls-cheat-triage-20261029")
RESULTS = HERE.parent / "results"
IDENTITY_SQL = (
    "SELECT a.trace_id, c.task_id, c.dataset_id, c.split FROM agent_traces a JOIN corpus_tasks c USING(task_id)"
)
ALARM = re.compile(r"^- (\S+) \| version (.+?) \| reader (\S+) \| score ([\d.]+) \| steps ([\d,]*) \| ", re.M)
RUN_IN_OPEN_MORE = re.compile(r"`evallab trace card (\S+)`")
FLOOR = 3
COHORT_FLOOR = 10  # cards inside the 200-run cohort: reported as "<10" so the cohort's enacted alarm count cannot expose a complement


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sup(counts: dict[str, int]) -> dict[str, Any]:
    return {k: (v if v >= FLOOR else f"<{FLOOR}") for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))}


def parse_card(path: Path) -> dict[str, Any]:
    text = path.read_text()
    run_id = RUN_IN_OPEN_MORE.search(text).group(1)
    section = text.split("## Alarms", 1)[1].split("\n## ", 1)[0]
    alarm_lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    alarms = []
    for ln in alarm_lines:
        m = ALARM.match(ln)
        steps = [int(s) for s in m.group(5).split(",") if s] if m else []
        alarms.append({"detector": m.group(1), "version": m.group(2), "reader": m.group(3), "score": float(m.group(4)),
                       "steps": steps} if m else {"detector": "?", "version": "?", "reader": "?", "score": None, "steps": []})
    cited = text.split("## Cited steps", 1)[1].split("\n## ", 1)[0]
    cited_lines = [ln for ln in cited.splitlines() if ln.startswith("- step ")]
    window = re.search(r"qa-data window \S+ --around (\d+) --radius (\d+)", text)
    header = text.splitlines()[1]
    return {
        "run_id": run_id,
        "dataset": run_id.split("/")[0],
        "alarms": alarms,
        "alarm_lines": len(alarm_lines),
        "alarm_lines_distinct": len(set(alarm_lines)),
        "cited_total": len(cited_lines),
        "cited_outside_window": sum("outside the live window" in ln for ln in cited_lines),
        "actor_inputs_false": "actor_inputs_available=false" in header,
        "killable_false": "killable=false" in header,
        "owner": re.search(r"owner=(\S+)", header).group(1),
        "around": int(window.group(1)) if window else None,
        "radius": int(window.group(2)) if window else None,
        "card_bytes": len(text.encode()),
        "ends_leads_not_verdicts": text.rstrip().endswith("Leads, not verdicts: confirm in the trace before acting."),
        "shortcut_none": bool(alarm_lines) and (alarm_lines[0].split(" | ", 5)[-1].split(":", 1)[0].strip().lower() == "none"),
    }


def store_alarms(run_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Structural fields of the store's cheat-alarm lines for the carded runs (raw lines, duplicates kept)."""
    out: dict[str, list[dict[str, Any]]] = {}
    with FINDINGS.open() as handle:
        for line in handle:
            if '"alarm"' not in line:
                continue
            rec = json.loads(line)
            if rec.get("verdict") == "alarm" and rec.get("kind") == "cheat" and rec["run_id"] in run_ids:
                out.setdefault(rec["run_id"], []).append({
                    "detector": rec["detector"], "version": rec["detector_version"],
                    "reader": rec.get("reader_model") or "deterministic", "score": rec.get("score"),
                    "steps": rec.get("evidence_steps") or [], "produced_at": rec.get("produced_at"),
                    "line_hash": hashlib.sha256(line.encode()).hexdigest()[:12],
                })
    return out


def sup_n(value: int) -> int | str:
    return value if value >= FLOOR else f"<{FLOOR}"


def bucket(counts: Counter[int]) -> dict[str, int | str]:
    """Alarm-line counts: 1, 2 and 3 or more (a cell below 3 is suppressed)."""
    merged = {"1": counts.get(1, 0), "2": counts.get(2, 0), "3+": sum(v for k, v in counts.items() if k >= 3)}
    return {k: sup_n(v) for k, v in merged.items()}


def bucket_steps(counts: Counter[int]) -> dict[str, int | str]:
    merged = {"4_or_fewer": sum(v for k, v in counts.items() if k <= 4), "5": counts.get(5, 0),
              "6_or_more": sum(v for k, v in counts.items() if k >= 6)}
    return {k: sup_n(v) for k, v in merged.items()}



def cross_key(run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Identity-only strict DEV check (no receipt label read): the same gates ``run_atlas_score._read_labels`` applies."""
    registry = CorpusRegistry(REPO)
    verdict: dict[str, dict[str, Any]] = {}
    with closing(ra.connect_readonly(ra.DEFAULT_DATABASE)) as connection:
        admitted = ra.admitted_task_ids(connection, registry)
        for run_id in run_ids:
            row = connection.execute(IDENTITY_SQL + " WHERE a.trace_id = ?", (run_id,)).fetchone()
            if row is None:
                verdict[run_id] = {"class": "unknown_not_in_store"}
                continue
            effective = connection.execute(
                "SELECT json_extract(verified_receipt_json, '$.admission.effective_split') "
                "FROM agent_traces WHERE trace_id = ?", (run_id,)
            ).fetchone()[0]
            test_any = registry.assigned_test_anywhere(item_id=run_id, task_id=row["task_id"])
            split = str(row["split"]).casefold()
            eff = None if effective is None else str(effective).casefold()
            strict = (
                split == "dev" and row["task_id"] in admitted and not test_any
                and eff in (None, "dev") and row["dataset_id"] == ra.GOODFIRE_DATASET
            )
            cls = "strict_dev" if strict else ("test_or_sealed" if (test_any or split != "dev" or eff not in (None, "dev"))
                                              else "dev_not_admitted_or_other_dataset")
            verdict[run_id] = {"class": cls, "split": split, "effective_split": eff, "assigned_test": bool(test_any),
                               "admitted_task": row["task_id"] in admitted, "dataset": row["dataset_id"]}
    return verdict


def step_geometry(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Cited-step validity and position against the stored run length (``agent_traces.step_count``; no label)."""
    with closing(ra.connect_readonly(ra.DEFAULT_DATABASE)) as connection:
        counts = {c["run_id"]: int(connection.execute(
            "SELECT step_count FROM agent_traces WHERE trace_id = ?", (c["run_id"],)).fetchone()[0] or 0)
            for c in cards}
    quart = lambda xs: [sorted(xs)[int(q * (len(xs) - 1))] for q in (0, 0.25, 0.5, 0.75, 1)]  # noqa: E731
    cited = {c["run_id"]: sorted({s for a in c["alarms"] for s in a["steps"]}) for c in cards}
    valid = sum(all(s < counts[r] for s in v) for r, v in cited.items())
    rel = [s / counts[r] for r, v in cited.items() if counts[r] for s in v]
    first = [v[0] / counts[r] for r, v in cited.items() if v and counts[r]]
    return {
        "run_step_count_quartiles_min_q1_med_q3_max": quart(list(counts.values())),
        "cards_with_all_cited_steps_below_step_count": valid,
        "cards_with_a_cited_step_at_or_past_step_count": len(cited) - valid,
        "cards_with_no_cited_step": sum(not v for v in cited.values()),
        "cited_step_relative_position_quartiles": [round(x, 3) for x in quart(rel)],
        "first_cited_relative_position_quartiles": [round(x, 3) for x in quart(first)],
    }


BATCH_STAMP = "2026-10-29T01:53:24Z"


def batch_population(carded: set[str]) -> dict[str, Any]:
    """The one bulk write that produced the cards: every findings line stamped ``BATCH_STAMP``, and the facet store."""
    batch: list[dict[str, Any]] = []
    with FINDINGS.open() as handle:
        for line in handle:
            if BATCH_STAMP in line:
                rec = json.loads(line)
                if rec["produced_at"] == BATCH_STAMP:
                    batch.append(rec)
    ids = sorted({r["run_id"] for r in batch})
    ck = cross_key(ids)
    strict = {r for r, v in ck.items() if v["class"] == "strict_dev"}
    by_score = Counter((r["detector"], r.get("reader_model"), r["verdict"], r["score"]) for r in batch)
    # facet store: stream only strict-DEV runs (cross-key first)
    haiku = "anthropic/claude-haiku-5-5"
    readers: dict[str, set[str]] = {}
    last: dict[str, str] = {}
    suspicion_of_haiku_last: dict[str, int] = {}
    with ra.DEFAULT_FACETS.open() as handle:
        for line in handle:
            m = re.search(r'"run_id"\s*:\s*"([^"]+)"', line)
            if not m or m.group(1) not in strict:
                continue
            rec = json.loads(line)
            if not all(f in rec for f in ("facet_model", "suspicion", "evidence_steps")):
                continue
            readers.setdefault(rec["facet_model"], set()).add(rec["run_id"])
            last[rec["run_id"]] = rec["facet_model"]
            if rec["facet_model"] == haiku:
                suspicion_of_haiku_last[rec["run_id"]] = int(rec["suspicion"])
    finding_score = {r["run_id"]: r["score"] for r in batch}
    agree = sum(suspicion_of_haiku_last.get(r) == int(finding_score[r]) for r in strict if r in suspicion_of_haiku_last)
    carded_in_batch = carded & set(ids)
    finding_verdict = {r["run_id"]: r["verdict"] for r in batch}
    older: set[str] = set()
    with FINDINGS.open() as handle:
        for line in handle:
            if '"alarm"' in line and haiku in line:
                rec = json.loads(line)
                if (rec["detector"] == "facets" and rec.get("reader_model") == haiku and rec["verdict"] == "alarm"
                        and rec["kind"] == "cheat" and rec["produced_at"] != BATCH_STAMP and rec["run_id"] in finding_verdict):
                    older.add(rec["run_id"])
    return {
        "batch_lines": len(batch), "batch_runs": len(ids), "crosskey": dict(Counter(v["class"] for v in ck.values())),
        "by_detector_reader_verdict_score": {f"{k[0]}|{k[1]}|{k[2]}|{k[3]}": v for k, v in sorted(by_score.items(), key=str)},
        "carded_runs_in_batch": len(carded_in_batch), "alarm_runs_in_batch": sum(r["verdict"] == "alarm" for r in batch),
        "strict_dev_batch_runs": len(strict),
        "facet_store_last_wins_reader_over_batch_strict_dev": dict(Counter(last.get(r, "none") for r in strict)),
        "facet_store_runs_with_a_haiku_line": len(suspicion_of_haiku_last),
        "haiku_last_haiku_line_suspicion_equals_finding_score": agree,
        "haiku_suspicion_ge2_in_facet_store": sum(v >= 2 for v in suspicion_of_haiku_last.values()),
        "carded_runs_whose_last_wins_reader": sup(dict(Counter(last.get(r, "none") for r in carded_in_batch))),
        "batch_runs_in_goodfire_dev_sample_200": len(set(ids) & {row["trace_id"] for row in json.loads(SAMPLE.read_text())}),
        "batch_runs_with_an_older_haiku_facets_alarm_record": len(older),
        "of_which_current_batch_record_is_alarm": sum(finding_verdict[r] == "alarm" for r in older),
        "of_which_current_batch_record_is_clear": sum(finding_verdict[r] == "clear" for r in older),
    }


def main() -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    index_paths = re.findall(r"`([^`]+\.md)`", INDEX.read_text())
    files = sorted(p for p in CARDS.glob("*.md"))
    cards = [parse_card(p) for p in files]
    run_ids = [c["run_id"] for c in cards]
    stored = store_alarms(set(run_ids))
    ck = cross_key(sorted(set(run_ids)))
    sample_ids = {row["trace_id"] for row in json.loads(SAMPLE.read_text())}
    # private per-run table (never committed)
    for c in cards:
        c["crosskey"] = ck[c["run_id"]]
        c["store_alarms"] = stored.get(c["run_id"], [])
        c["in_goodfire_dev_sample"] = c["run_id"] in sample_ids
    (PRIVATE / "stage_a_cards.json").write_text(json.dumps(cards, indent=1))

    fam = Counter(a["detector"] for c in cards for a in c["alarms"])
    fam_reader = Counter(f'{a["detector"]}|{a["reader"]}|score {a["score"]:g}' for c in cards for a in c["alarms"])
    runs_by_dets = Counter("+".join(sorted({a["detector"] for a in c["alarms"]})) for c in cards)
    steps_n = Counter(len({s for a in c["alarms"] for s in a["steps"]}) for c in cards)
    per_run = Counter(Counter(run_ids).values())
    store_lines_per_card = Counter(len(c["store_alarms"]) for c in cards)
    # does the store hold exact duplicate lines or only the same key twice?
    dup_exact = sum(len({a["line_hash"] for a in c["store_alarms"]}) < len(c["store_alarms"]) for c in cards)
    key_dup = sum(
        len({(a["detector"], a["version"], a["reader"]) for a in c["store_alarms"]}) < len(c["store_alarms"])
        for c in cards
    )
    produced = sorted({a["produced_at"] for c in cards for a in c["store_alarms"] if a["produced_at"]})
    doc = {
        "stage": "A (label-free)",
        "index_paths": len(index_paths), "index_distinct": len(set(index_paths)),
        "card_files": len(files), "index_equals_files": set(index_paths) == {str(p) for p in files},
        "distinct_runs": len(set(run_ids)), "cards_per_run": dict(per_run),
        "card_bytes": {"min": min(c["card_bytes"] for c in cards), "max": max(c["card_bytes"] for c in cards)},
        "header": {
            "actor_inputs_available_false": sum(c["actor_inputs_false"] for c in cards),
            "killable_false": sum(c["killable_false"] for c in cards),
            "owner": dict(Counter(c["owner"] for c in cards)),
            "ends_leads_not_verdicts": sum(c["ends_leads_not_verdicts"] for c in cards),
        },
        "dataset_prefix": sup(dict(Counter(c["dataset"] for c in cards))),
        "alarm_lines_per_card": bucket(Counter(c["alarm_lines"] for c in cards)),
        "cards_whose_first_facets_alarm_names_no_shortcut": sup_n(sum(c["shortcut_none"] for c in cards)),
        "cards_with_repeated_identical_alarm_line": sum(c["alarm_lines"] != c["alarm_lines_distinct"] for c in cards),
        "alarm_lines_by_detector": sup(dict(fam)),
        "alarm_lines_by_detector_reader_score": sup(dict(fam_reader)),
        "cards_by_detector_set": sup(dict(runs_by_dets)),
        "distinct_cited_steps_per_card": bucket_steps(steps_n),
        "cited_steps_outside_live_window": {
            "cited_total": sum(c["cited_total"] for c in cards),
            "outside": sum(c["cited_outside_window"] for c in cards),
        },
        "open_more_window_present": sum(c["around"] is not None for c in cards),
        "open_more_radius_values": dict(Counter(c["radius"] for c in cards)),
        "store": {
            "cheat_alarm_lines_per_carded_run": bucket(store_lines_per_card),
            "carded_runs_with_exact_duplicate_store_line": sup_n(dup_exact),
            "carded_runs_with_same_key_twice_in_store": key_dup,
            "produced_at_range": [produced[0], produced[-1]] if produced else None,
            "produced_at_distinct": len(produced),
        },
        "store_scope": "no whole-store alarm tallies: they include runs outside the strict DEV cross-key; the store block above is the 154 carded runs",
        "crosskey": dict(Counter(v["class"] for v in ck.values())),
        "crosskey_detail": sup(dict(Counter(
            f'{v["class"]}|assigned_test={v.get("assigned_test")}|admitted={v.get("admitted_task")}' for v in ck.values()
        ))),
        "cards_in_goodfire_dev_sample_200": f"<{COHORT_FLOOR}" if sum(c["in_goodfire_dev_sample"] for c in cards) < COHORT_FLOOR else sum(c["in_goodfire_dev_sample"] for c in cards),
        "sample_ids": len(sample_ids),
        "steps": step_geometry(cards),
        "batch": batch_population(set(run_ids)),
        "sha256": {"findings": sha256(FINDINGS), "index": sha256(INDEX), "sample": sha256(SAMPLE)},
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "stage_a.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
