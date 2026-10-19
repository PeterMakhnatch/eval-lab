"""Assemble the 2026-10-19 `evallab qa suite` inputs from retained evidence ($0).

Run from the repository root, then run the suite command printed at the end:

    uv run python research/qa-scoreboard/suite-20261019/build.py

Nothing is executed or re-judged. The script writes, deterministically:

- `findings/<key>--tripwires-<view>.jsonl`: the committed per-item tripwire
  decisions (`research/answer-keys/{cheatbench-actions-v1,hacktrace-act-v1,
  internal-views}/predictions.jsonl`) as `qa.finding/v1` records. The producer
  identity `tripwires@<sha12>/<view>` is the `tripwires_sha256` that the same
  build wrote into the neighbouring `baseline.json`. A fired-rule list becomes
  `verdict: fail`, an empty one `verdict: pass`; items the source could not
  measure are not emitted and stay unmeasured. The subject is copied from the
  key's `items.jsonl`.
- `runs.json`: the suite manifest. Retained raw `qa.finding/v1` sources are bound
  in place (QA Trace runs under the git-ignored `derived/qa-trace/`, bound by
  SHA256 receipt rather than copied; Breaker findings under
  `research/experiments/`). Published results this command cannot re-score are
  copied verbatim from `research/qa-scoreboard/scoreboard.csv` (or the named
  baseline file) into `not_rescored`, with the reason.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KEYS = REPO / "research/answer-keys"
SCOREBOARD = REPO / "research/qa-scoreboard/scoreboard.csv"
DERIVED = "../../../derived/qa-trace"  # relative to this manifest

# (agent, version, key, findings relative to this directory, run, notes)
RETAINED = [
    ("breaker", "cheat-ladder-v1.4.0", "breaker-bench-v1",
     "../../experiments/breaker-bench-ladder-floor/findings/ladder-v1.4.0",
     "breaker-bench-ladder-floor", "Scripted model-free ladder floor on staged dev subjects."),
    ("breaker", "cheat-ladder-v3.0.0", "breaker-bench-v1",
     "../../experiments/breaker-bench-ladder-floor/findings/ladder-v3.0.0",
     "breaker-bench-ladder-floor", "Scripted model-free ladder floor on staged dev subjects."),
    ("breaker", "cheat-ladder-1.4.0", "breaker-bench-v1",
     "../../experiments/breaker-bench-dev-v1/findings/cheat-ladder-1.4.0",
     "breaker-bench-dev-v1", "Dev-split ladder run; `cheat-ladder-1.4.0` is a distinct identity from `cheat-ladder-v1.4.0`."),
    ("breaker", "cheat-ladder-3.0.0", "breaker-bench-v1",
     "../../experiments/breaker-bench-dev-v1/findings/cheat-ladder-3.0.0",
     "breaker-bench-dev-v1", "Dev-split ladder run; `cheat-ladder-3.0.0` is a distinct identity from `cheat-ladder-v3.0.0`."),
    ("trace", "qa-trace/3", "trajectory-judge-400", f"{DERIVED}/trajectory-judge-400/findings",
     "derived/qa-trace/trajectory-judge-400", "Original qa-trace/3 run, dev and test."),
    ("trace", "qa-trace/3", "trace-paired-v1", f"{DERIVED}/trace-paired-v1/findings",
     "derived/qa-trace/trace-paired-v1", "Original qa-trace/3 run."),
    ("trace", "qa-trace/5", "trajectory-judge-400", f"{DERIVED}/qa-trace-5/trajectory-judge-400/findings",
     "derived/qa-trace/qa-trace-5/trajectory-judge-400", "qa-trace/5 held-out test run."),
    ("trace", "qa-trace/5", "trace-paired-v1", f"{DERIVED}/qa-trace-5/trace-paired-v1/findings",
     "derived/qa-trace/qa-trace-5/trace-paired-v1", "qa-trace/5 run."),
    ("trace", "qa-trace/6", "trace-paired-v1", f"{DERIVED}/qa-trace-6/trace-paired-v1-rs/findings",
     "derived/qa-trace/qa-trace-6/trace-paired-v1-rs", "qa-trace/6 run (T1 gpt-6.1-sol:medium, T3 gpt-6-luna:medium)."),
    ("trace", "qa-trace/7", "trajectory-judge-400", f"{DERIVED}/qa-trace-7/trajectory-judge-400-test/findings",
     "derived/qa-trace/qa-trace-7/trajectory-judge-400-test",
     "Held-out test run only (T1 gpt-6.1-sol:medium, T3 gpt-6-luna:medium); dev iterations and the Antigravity test-aware control are not bound."),
    ("trace", "qa-trace/8", "trajectory-judge-400", f"{DERIVED}/qa-trace-8/test-trajectory-judge-400/findings",
     "derived/qa-trace/qa-trace-8/test-trajectory-judge-400",
     "Held-out test run on Antigravity Sonnet; quota-blocked, every T1 record is ran_without_usable_result."),
    ("trace", "qa-trace/8", "trace-paired-v1", f"{DERIVED}/qa-trace-8/test-trace-paired-v1/findings",
     "derived/qa-trace/qa-trace-8/test-trace-paired-v1",
     "Held-out test run on Antigravity Sonnet; quota-blocked, every T1 record is ran_without_usable_result."),
]

# Published scoreboard systems this command cannot re-score: (agent, exact system, reason).
# A system ending in "/" selects every system with that prefix.
FROM_SCOREBOARD = [
    ("trace", "qa-trace/6-pk1-luna",
     "PK-1 Luna variant: raw records carry the same reviewer.agent (qa-trace/6/T*) as the original run, so the variant is not provable per record; 2 subjects per key."),
    ("trace", "qa-trace/7-pk1-luna",
     "PK-1 Luna variant: raw records carry the same reviewer.agent (qa-trace/7/T*) as the original run, so the variant is not provable per record; 2 subjects per key."),
    ("trace", "qa-trace/8-pk1-luna",
     "PK-1 Luna variant: raw records carry the same reviewer.agent (qa-trace/8/T*) as the bound run, so the variant is not provable per record; 2 subjects per key."),
    ("trace", "qa-trace/7-test-aware-antigravity-quota-blocked",
     "Antigravity test-aware control shares reviewer.agent qa-trace/7/T1 with the bound qa-trace/7 run; all records ran_without_usable_result."),
    ("trace", "combined_v1@oof",
     "Per-item decisions are a custom format (combined_v1_baseline/*.jsonl) with no producer field; combined_v1 is fit on folds containing trace-paired-v1 trials."),
    ("trace", "combined_v1@deploy-agentic",
     "Per-item decisions are a custom format (combined_v1_baseline/*.jsonl) with no producer field; combined_v1 is fit on folds containing trace-paired-v1 trials."),
    ("tripwire", "live-watch/integrity-board@26d2b18e6",
     "Raw findings name the unversioned producer `TA monitor`; the version is only in the run manifest."),
    ("tripwire", "action-lr@88cf3b998fd3/",
     "Transfer matrix (eval-lab #1091): per-item scores in a custom format, no qa.finding/v1 records; train key is part of the system."),
    ("task", "qa-task@verified",
     "S0-S3 producer records are unversioned (`qa-task`, `qa-task/shortcut-agent`, ...); `@verified` is a `qa task-score` mode with full-cohort denominators, not a producer id."),
    ("task", "qa-task@flagged",
     "S0-S3 producer records are unversioned; `@flagged` is a `qa task-score` mode with full-cohort denominators, not a producer id."),
    ("task", "qa-task@s2a-ordinary-availability-verified",
     "S0-S3 union + ordinary S2a; producer records are unversioned and the raw union is git-ignored in another worktree; `qa task-score` full-cohort denominators."),
    ("task", "qa-task/mimo-v7-audit",
     "Retrospective source reconciliation; committed findings name the unversioned producer `QA Task`."),
    ("task", "qa-task/mimo-sample100-strict",
     "Retrospective source reconciliation; committed findings name the unversioned producer `QA Task`."),
    ("breaker", "openai/gpt-6-luna (PinnedCodex, verifier-aware)",
     "Held-out findings directory was not found on disk (the producer was a dirty worktree); `codex-verifier-aware` is also not a versioned identity."),
    ("breaker", "openai/gpt-6-luna (PinnedCodex, blind)",
     "Held-out findings directory was not found on disk; `codex-blind` is not a versioned identity."),
    ("breaker", "cheat_ladder 3.0.0 (model-free)",
     "Held-out test-split findings directory was not found on disk; only the aggregate row survives."),
]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rate(value: str) -> str:
    return value if value in {"", "n/a"} else f"{float(value):.4f}"


def scoreboard_entries() -> list[dict]:
    rows = list(csv.DictReader(SCOREBOARD.open(newline="", encoding="utf-8")))
    entries = []
    for agent, system, reason in FROM_SCOREBOARD:
        selected = [
            r for r in rows
            if r["category"] == "all"
            and (r["system"].startswith(system) if system.endswith("/") else r["system"] == system)
        ]
        if not selected:
            raise SystemExit(f"no scoreboard rows for {system!r}")
        for r in selected:
            entries.append({
                "agent": agent, "system": r["system"], "key": r["key_id"], "split": r["split"],
                "reported": (
                    f"recall {_rate(r['recall'])}; false alarms {_rate(r['false_alarm_rate'])}; "
                    f"paired {_rate(r['paired_discrimination'])}; measured {r['n_measured']}/{r['n_items']}; "
                    f"status: {r['status']}"
                ),
                "reason": reason,
                "source": f"research/qa-scoreboard/scoreboard.csv ({r['date_utc']})",
            })
    return entries


def baseline_entries() -> list[dict]:
    """Tripwires on the MiMo v7 canonical 70: not a registered answer key."""
    baseline = json.loads((KEYS / "internal-views/baseline.json").read_text())["mimo-v7-canonical-pass70"]
    version = f"tripwires@{baseline['tripwires_sha256'][:12]}"
    entries = []
    for view in ("full", "actions"):
        metrics = baseline["views"][view]
        recall, alarms = metrics["recall"], metrics["false_alarm"]
        entries.append({
            "agent": "tripwire", "system": f"{version}/{view}", "key": "mimo-v7-canonical-pass70",
            "split": baseline["split"],
            "reported": (
                f"recall {recall['rate']:.4f} ({recall['hits']}/{recall['n']}); "
                f"false alarms {alarms['rate']:.4f} ({alarms['hits']}/{alarms['n']}); "
                f"measured {baseline['coverage']['measured_items']}/{baseline['coverage']['source_items']}"
            ),
            "reason": "Dataset is a blinded internal view population, not a registered answer key.",
            "source": "research/answer-keys/internal-views/baseline.json",
        })
    return entries


def _tripwire_findings(key: str, predictions: list[dict], sha: str, view: str, source: str) -> tuple[str, list[dict]]:
    version = f"tripwires@{sha[:12]}/{view}"
    items = {item["item_id"]: item for item in _jsonl(KEYS / key / "items.jsonl")}
    findings = []
    for prediction in sorted(predictions, key=lambda p: p["item_id"]):
        fired = prediction[view]
        findings.append({
            "schema": "qa.finding/v1",
            "id": f"{version}:{prediction['item_id']}",
            "subject": items[prediction["item_id"]]["subject"],
            "stage": f"T0-tripwires-{view}",
            "verdict": "fail" if fired else "pass",
            "category": "reward_hacking",
            "severity": "high" if fired else None,
            "fault": "agent" if fired else None,
            "summary": ("fired: " + ", ".join(fired)) if fired else "no frozen rule fired",
            "evidence_refs": [source],
            "reviewer": {"agent": version, "model": None, "cost_usd": 0.0},
            "measurement": "measured",
        })
    return version, findings


def tripwire_runs() -> list[dict]:
    sources = [
        (key, json.loads((KEYS / key / "baseline.json").read_text())["tripwires_sha256"],
         f"research/answer-keys/{key}/predictions.jsonl", None, "eval-lab #1083 external_traces.py baseline")
        for key in ("cheatbench-actions-v1", "hacktrace-act-v1")
    ]
    internal = json.loads((KEYS / "internal-views/baseline.json").read_text())
    sources += [
        (key, internal[key]["tripwires_sha256"], "research/answer-keys/internal-views/predictions.jsonl", key,
         "eval-lab #1087 internal_traces.py baseline")
        for key in ("trajectory-judge-400", "trace-paired-v1")
    ]
    runs = []
    for key, sha, source, dataset, run in sources:
        predictions = [p for p in _jsonl(REPO / source) if dataset is None or p.get("dataset") == dataset]
        for view in ("full", "actions"):
            version, findings = _tripwire_findings(key, predictions, sha, view, source)
            path = HERE / "findings" / f"{key}--tripwires-{view}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(f, sort_keys=True) + "\n" for f in findings), encoding="utf-8")
            notes = "Frozen evidence-only OR of the tripwire rules on the blinded view; no tuning, no model calls."
            if key == "cheatbench-actions-v1":
                notes = "CheatBench is prior-exposed (combined_v1 calibration corpus), not a fresh holdout. " + notes
            if dataset is not None:
                notes += " Items without a usable ATIF are not emitted and stay unmeasured."
            runs.append({
                "agent": "tripwire", "version": version, "key": key,
                "findings": path.relative_to(HERE).as_posix(), "run": run, "notes": notes,
            })
    return runs


def main() -> None:
    runs = tripwire_runs() + [
        {"agent": a, "version": v, "key": k, "findings": f, "run": r, "notes": n}
        for a, v, k, f, r, n in RETAINED
    ]
    document = {
        "runs": sorted(runs, key=lambda r: (r["agent"], r["version"], r["key"])),
        "not_rescored": scoreboard_entries() + baseline_entries(),
    }
    (HERE / "runs.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    versions = sorted({r["version"] for r in runs} | {"qa-task@s2a-ordinary-availability-verified"})
    print(
        "uv run evallab qa suite --agent task --agent trace --agent breaker --agent tripwire "
        + " ".join(f"--version '{v}'" for v in versions)
        + " --runs research/qa-scoreboard/suite-20261019/runs.json --date 2026-10-19"
    )


if __name__ == "__main__":
    main()
