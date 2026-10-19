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

# Track B LLM arm (ReasoningAblation): one gpt-oss-120b call per (item, view), qa-trace/6
# question texts, sealed before labels were read. Canonical store is eval-lab-results; the
# raw records are copied here unchanged except that the 15 trajectory-judge-400 test items
# that tuned the qa-trace prompts (exposure `qa-trace-tj-prompt-dev`) are dropped, exactly
# as the bound qa-trace/3, /5 and /7 runs left them unmeasured.
OR1P = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-19/reasoning-ablation/test/findings")
OR1P_KEYS = ("cheatbench-actions-v1", "hacktrace-act-v1", "mimo-v7-canonical-pass70",
             "trace-paired-v1", "trajectory-judge-400")


def or1p_runs() -> list[dict]:
    exposure = next(
        record for record in _jsonl(KEYS / "splits/exposure.jsonl")
        if record["exposure_id"] == "qa-trace-tj-prompt-dev"
    )
    tuned = {
        line.strip() for line in (REPO / exposure["items_file"]["trajectory-judge-400"]).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    tuned_trials = {item_id.split(":", 1)[-1] for item_id in tuned}
    runs = []
    for view in ("full", "actions"):
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((OR1P / view).rglob("*.json"))]
        kept = [r for r in records if r["subject"]["trial_id"] not in tuned_trials]
        path = HERE / "findings" / f"qa-trace-6-or1p-{view}.jsonl"
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8")
        for key in OR1P_KEYS:
            runs.append({
                "agent": "trace", "version": f"qa-trace/6-or1p/{view}", "key": key,
                "findings": path.relative_to(HERE).as_posix(),
                "run": f"reasoning-ablation test, sealed 2026-10-19T09:23:13Z ({OR1P / view})",
                "notes": (
                    f"Single-call gpt-oss-120b judge on the {view} view (no tools, no T0/T3). "
                    f"{len(records) - len(kept)} records on the 15 tuned trajectory-judge-400 test items "
                    "(research/experiments/qa-trace/tuned_on/trajectory-judge-400.txt) dropped. "
                    "Per-finding cost fields split each call's OpenRouter cost over its asked questions; across both views "
                    "they sum to the sealed ledger total ($0.6307 before the drop). "
                    "Per-head paired tests: research/experiments/reasoning-ablation/ (eval-lab #1099)."
                ),
            })
    return runs


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
    ("tripwire", "action-rule@88cf3b998fd3/",
     "History-command action rule (eval-lab #1097): per-item booleans in a custom format, no qa.finding/v1 records."),
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
    ("task", "weak-task@atlas-v2.1+sample100",
     "Task-evidence wrong-pass detector (eval-lab #1103): per-item predictions in a custom format, no qa.finding/v1 records."),
    ("task", "weak-task-untouched@atlas-v2.1",
     "Task-evidence wrong-pass detector (eval-lab #1103): per-item predictions in a custom format, no qa.finding/v1 records."),
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


def _rate_of(cell: dict) -> str:
    return f"{cell['rate']:.4f} ({cell['k']}/{cell['n']})" if cell["n"] else "n/a (0/0)"


def experiment_entries() -> list[dict]:
    """Published experiment results with no scoreboard row, copied from their results.json."""
    entries = []
    window3 = "research/experiments/reasoning-ablation/window3-results.json"
    head = json.loads((REPO / window3).read_text())["keys"]["mimo-fresh-window3"]["heads"]["any_finding"]
    for view in ("full", "actions"):
        entries.append({
            "agent": "trace", "system": f"qa-trace/6-or1p/{view} (any finding)", "key": "mimo-fresh-window3",
            "split": "test",
            "reported": f"recall {_rate_of(head['recall'][view])}; false alarms {_rate_of(head['false_alarm'][view])}",
            "reason": "Window-3 judge outputs (eval-lab #1100) were published as compact predictions without qa.finding/v1 records.",
            "source": window3,
        })
    fresh = "research/experiments/cb-fresh-holdout-20261019/results.json"
    pools = json.loads((REPO / fresh).read_text())["pools"]
    for pool in ("test-family", "dev-family"):
        for arm, metrics in sorted(pools[pool]["arms"].items()):
            entries.append({
                "agent": "tripwire",
                "system": f"{arm} (frozen, eval-lab #1102)" + (" [secondary]" if metrics.get("secondary") else ""),
                "key": f"cheatbench fresh {pool} (3,968 key-excluded traces)", "split": "fresh",
                "reported": (
                    f"recall {_rate_of(metrics['recall'])}; false alarms {_rate_of(metrics['false_alarm'])}; "
                    f"precision {_rate_of(metrics['precision'])}"
                ),
                "reason": "Fresh CheatBench pool is not a registered answer key; per-item predictions are a custom format.",
                "source": fresh,
            })
    return entries


def _tripwire_findings(key: str, decisions: list[tuple[str, list[str]]], version: str, view: str, source: str) -> list[dict]:
    items = {item["item_id"]: item for item in _jsonl(KEYS / key / "items.jsonl")}
    findings = []
    for item_id, fired in sorted(decisions):
        findings.append({
            "schema": "qa.finding/v1",
            "id": f"{version}:{item_id}",
            "subject": items[item_id]["subject"],
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
    return findings


def _tripwire_sources() -> list[tuple[str, str, str, list[dict], object, str]]:
    """(key, tripwires sha256, source file, records, fired(record, view) -> rules, run label)."""
    sources = []
    for key in ("cheatbench-actions-v1", "hacktrace-act-v1"):
        source = f"research/answer-keys/{key}/predictions.jsonl"
        sha = json.loads((KEYS / key / "baseline.json").read_text())["tripwires_sha256"]
        sources.append((key, sha, source, _jsonl(REPO / source), lambda p, v: p[v],
                        "eval-lab #1083 external_traces.py baseline (rules v1)"))
    internal = json.loads((KEYS / "internal-views/baseline.json").read_text())
    source = "research/answer-keys/internal-views/predictions.jsonl"
    records = _jsonl(REPO / source)
    for key in ("trajectory-judge-400", "trace-paired-v1", "mimo-v7-canonical-pass70"):
        sources.append((key, internal[key]["tripwires_sha256"], source,
                        [p for p in records if p["dataset"] == key], lambda p, v: p[v],
                        "eval-lab #1087 internal_traces.py baseline (rules v1)"))
    rebaseline = "research/experiments/tripwires-v2-rebaseline-20261019"
    sha = json.loads((REPO / rebaseline / "results.json").read_text())["tripwires_sha256"]
    records = _jsonl(REPO / rebaseline / "predictions.jsonl")
    for key in sorted({p["key"] for p in records}):
        sources.append((key, sha, f"{rebaseline}/predictions.jsonl", [p for p in records if p["key"] == key],
                        lambda p, v: p["v2"][v], "eval-lab #1094 tripwires rules v2 re-baseline"))
    window3 = "research/experiments/history-cmds-window3-20261019"
    sha = json.loads((REPO / window3 / "results.json").read_text())["tripwires_sha256"]
    sources.append(("mimo-fresh-window3", sha, f"{window3}/predictions.jsonl", _jsonl(REPO / window3 / "predictions.jsonl"),
                    lambda p, v: p[v]["tripwire_rules"], "eval-lab #1097 one-shot blind window-3 test (rules v2)"))
    return sources


def tripwire_runs() -> list[dict]:
    runs = []
    for key, sha, source, records, fired, run in _tripwire_sources():
        for view in ("full", "actions"):
            version = f"tripwires@{sha[:12]}/{view}"
            findings = _tripwire_findings(key, [(p["item_id"], fired(p, view)) for p in records], version, view, source)
            path = HERE / "findings" / f"{key}--tripwires-{sha[:12]}-{view}.jsonl"
            path.write_text("".join(json.dumps(f, sort_keys=True) + "\n" for f in findings), encoding="utf-8")
            notes = "Frozen evidence-only OR of the tripwire rules on the blinded view; no tuning, no model calls."
            if key == "cheatbench-actions-v1":
                notes = "CheatBench is prior-exposed (combined_v1 calibration corpus), not a fresh holdout. " + notes
            runs.append({
                "agent": "tripwire", "version": version, "key": key,
                "findings": path.relative_to(HERE).as_posix(), "run": run,
                "notes": notes + " Items the source could not measure are not emitted and stay unmeasured.",
            })
    return runs


def main() -> None:
    findings_dir = HERE / "findings"
    findings_dir.mkdir(exist_ok=True)
    for stale in findings_dir.glob("*.jsonl"):
        stale.unlink()
    runs = tripwire_runs() + or1p_runs() + [
        {"agent": a, "version": v, "key": k, "findings": f, "run": r, "notes": n}
        for a, v, k, f, r, n in RETAINED
    ]
    document = {
        "runs": sorted(runs, key=lambda r: (r["agent"], r["version"], r["key"])),
        "not_rescored": scoreboard_entries() + experiment_entries(),
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
