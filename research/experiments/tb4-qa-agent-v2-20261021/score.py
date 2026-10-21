"""Score a closed TQ-1 campaign with the unmodified tb4-audit-v1 scorer.

Scorer side. This is the only entry that reads the private key and prior TEST baseline outputs, and
only after the actors are closed (``closed.json``). Its first action writes ``scoring-started.json``,
after which ``campaign.py run`` can never restart.

Every row set goes through the committed ``research/answer-keys/tb4-audit-v1/score_audit.py`` CLI
(the same entry v1 used). Headlines are ``category_1to1`` (the v1 primary) with strict
``locus_and_class`` and ``task_any_1to1`` as sensitivities; the legacy broadcast modes are never
reported as recall. Each row is a 1-to-1 matching, so a hit is a key finding matched by one claim.

Reported, never inferred from observed matches:

* recall by taxonomy class and by the direction groups frozen in ``freeze.json``
  (``wrong_pass_oriented``, ``wrong_fail_oriented``, ``mixed_or_unspecified``);
* v2 against every baseline with its model/condition difference and denominators, on the full split
  (tasks v2 did not measure count as misses) and on the tasks both rows measured, with and without
  the disclosed-exposure tasks and a paired bootstrap over tasks;
* per-task and aggregate time, tokens, metered cost and native-execution coverage; unknown stays null;
* every claim not credited at ``category_1to1`` as ``UNVERIFIED`` unless an independent witness ledger
  entry (receipt bytes outside the actor session) says otherwise. A source match is not confirmed
  correctness, and an unmatched claim is not a false positive; a positive-only key supports neither a
  clean-negative claim nor a prevalence claim.

Run from the repository root with the owned interpreter::

    .venv/bin/python research/experiments/tb4-qa-agent-v2-20261021/score.py \\
        --campaign-dir D [--plain-read-dir P] [--witness-ledger L.jsonl] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import campaign as C  # noqa: E402  (actor-side helpers only; no key access)

REPO = C.REPO
KEY_DIR = REPO / "research/answer-keys/tb4-audit-v1"
V1_SCORE = REPO / "research/experiments/tb4-qa-agent-v1-20261020/score.py"
MODES = ("category_1to1", "locus_and_class", "task_any_1to1")
BOOTSTRAP = 10_000
BOOTSTRAP_SEED = 20261021
MIXED = "mixed_or_unspecified"

#: Model/condition of every comparison row, copied from the v1 README and the reader-baseline README.
BASELINES: dict[str, dict[str, Any]] = {
    "v0": {
        "label": "QA agent v0 (frozen QA-2 predictions)", "model": "codex/gpt-6.1-sol",
        "condition": "JSON-action loop on Codex; 8 tool calls and 120 s per task; runtime tools "
                     "listed but unavailable (no Docker admission); at most one claim per task",
    },
    "static_scanner": {
        "label": "static triage scanner (#1118, rerun)", "model": None,
        "condition": "deterministic signal scanner; no model, no execution",
    },
    "plain_read_opus": {
        "label": "plain read, Opus (#1119 static arm)", "model": "anthropic/claude-opus-5-5",
        "condition": "no tools beyond read-only shell, no execution; reader_prompt.md; up to 8 "
                     "findings per task; 6 tasks per subagent",
    },
    "v1_opus": {
        "label": "QA agent v1 (#1121)", "model": "anthropic/claude-opus-5-5",
        "condition": "native OMP subagent with three source tools (task-info, diff, record-finding); "
                     "60 tool calls and 900 s per task; no runtime tools",
    },
}


def _score_module() -> Any:
    return C.load_module(KEY_DIR / "score_audit.py", "_tq1_score_audit")


def run_scorer(rows: list[dict[str, Any]], name: str, work: Path) -> dict[str, Any]:
    pred = work / f"predictions-{name}.jsonl"
    pred.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    out = work / f"score-{name}.json"
    subprocess.run([sys.executable, str(KEY_DIR / "score_audit.py"), "--predictions", str(pred),
                    "--out", str(out)], check=True, capture_output=True, cwd=REPO)
    return json.loads(out.read_text(encoding="utf-8"))


def claim_class(row: dict[str, Any]) -> str | None:
    root = row.get("root_cause")
    if isinstance(root, list) and root:
        return str(root[0])
    return row.get("taxonomy_class")


def analyse(audit: Any, key: Any, groups: dict[str, Any], rows: list[dict[str, Any]],
            result: dict[str, Any], scopes: dict[str, set[str]]) -> dict[str, Any]:
    preds, tally = audit.normalize_predictions(rows)
    labels = [p.label for p in preds]
    unique = len(set(labels)) == len(labels)
    classes = sorted({f.taxonomy_class for f in key.findings})
    group_of = {c: groups["classes"].get(c, {}).get("group", MIXED) for c in classes}
    out: dict[str, Any] = {
        "claims_total": len(preds), "prediction_tally": dict(sorted(tally.items())),
        "claim_labels_unique": unique,
        "unmapped_classes_defaulted_to_mixed": [c for c in classes if c not in groups["classes"]],
        "scorer_headline": {mode: result["modes"][mode]["headline"] for mode in MODES},
        "scopes": {},
    }
    for scope_name, tasks in scopes.items():
        findings = [f for f in key.findings if f.task in tasks]
        scope_preds = [p for p in preds if p.task in tasks]
        cell: dict[str, Any] = {
            "tasks": len(tasks), "key_findings": len(findings), "claims": len(scope_preds),
            "tasks_with_claim": len({p.task for p in scope_preds}),
            "claims_by_class": dict(sorted(Counter(claim_class(rows[p.index]) or "unclassified"
                                                    for p in scope_preds).items())),
            "claims_by_group": dict(sorted(Counter(
                groups["classes"].get(claim_class(rows[p.index]) or "", {}).get("group", MIXED)
                for p in scope_preds).items())),
            "modes": {},
        }
        for mode in MODES:
            matched = {item for _, item in result["matches"][mode]}
            credited = {label for label, _ in result["matches"][mode]}
            hit = [f for f in findings if f.item_id in matched]
            cell["modes"][mode] = {
                "recall": audit.rate(len(hit), len(findings)),
                "source_matched_claims": (audit.rate(sum(p.label in credited for p in scope_preds),
                                                     len(scope_preds)) if unique else None),
                "source_match_note": "share of claims 1-to-1 matched to a key finding; not "
                                     "independently confirmed correctness",
                "by_class": {c: audit.rate(sum(f.taxonomy_class == c for f in hit),
                                           sum(f.taxonomy_class == c for f in findings))
                             for c in classes if any(f.taxonomy_class == c for f in findings)},
                "by_group": {g: audit.rate(sum(group_of[f.taxonomy_class] == g for f in hit),
                                           sum(group_of[f.taxonomy_class] == g for f in findings))
                             for g in ("wrong_pass_oriented", "wrong_fail_oriented", MIXED)},
            }
        out["scopes"][scope_name] = cell
    return out


def per_task_hits(key: Any, result: dict[str, Any]) -> dict[str, dict[str, tuple[int, int]]]:
    """mode -> task -> (hits, key findings)."""
    total = Counter(f.task for f in key.findings)
    item_task = {f.item_id: f.task for f in key.findings}
    table: dict[str, dict[str, tuple[int, int]]] = {}
    for mode in MODES:
        hits = Counter(item_task[item] for _, item in result["matches"][mode])
        table[mode] = {task: (hits.get(task, 0), n) for task, n in total.items()}
    return table


def bootstrap(a: dict[str, tuple[int, int]], b: dict[str, tuple[int, int]], tasks: set[str],
              seed: int) -> dict[str, Any]:
    """Recall difference b - a, paired over tasks (resampling whole tasks)."""
    ordered = sorted(t for t in tasks if t in a and t in b)
    total = sum(a[t][1] for t in ordered)
    if not ordered or total == 0:
        return {"tasks": len(ordered), "difference": None, "ci95": None}
    point = (sum(b[t][0] for t in ordered) - sum(a[t][0] for t in ordered)) / total
    rng = random.Random(seed)
    diffs = []
    for _ in range(BOOTSTRAP):
        sample = [ordered[rng.randrange(len(ordered))] for _ in ordered]
        n = sum(a[t][1] for t in sample)
        diffs.append((sum(b[t][0] for t in sample) - sum(a[t][0] for t in sample)) / n if n else 0.0)
    diffs.sort()
    return {"tasks": len(ordered), "difference": round(point, 6), "resamples": BOOTSTRAP, "seed": seed,
            "ci95": [round(diffs[int(0.025 * BOOTSTRAP)], 6), round(diffs[int(0.975 * BOOTSTRAP)], 6)]}


def total(values: list[float | int | None]) -> dict[str, Any]:
    known = [v for v in values if v is not None]
    return {"total": (sum(known) if len(known) == len(values) and values else None),
            "sum_known": sum(known) if known else None, "known_tasks": len(known),
            "unknown_tasks": len(values) - len(known)}


def witness_ledger(path: Path | None, campaign: Path) -> dict[str, dict[str, Any]]:
    """Accepted independent witnesses by finding sha256; anything else is ignored and reported."""
    accepted: dict[str, dict[str, Any]] = {}
    if path is None:
        return accepted
    for row in C.read_jsonl(path):
        receipt = Path(row.get("receipt", ""))
        valid = (
            row.get("independent") is True and row.get("verdict") in {"confirmed", "refuted", "inconclusive"}
            and isinstance(row.get("finding_sha256"), str) and receipt.is_absolute() and receipt.is_file()
            and not receipt.resolve().is_relative_to(campaign.resolve())
            and C.sha256_file(receipt) == row.get("receipt_sha256")
        )
        if valid:
            accepted[row["finding_sha256"]] = row
    return accepted


def baseline_rows(split: str, work: Path) -> dict[str, Any]:
    """Prior TEST outputs, via the v1 scorer's own loaders; unavailable rows carry no numbers."""
    v1 = C.load_module(V1_SCORE, "_tq1_v1_score")
    loaders = {
        "v0": v1.v0_rows, "static_scanner": lambda: v1.static_rows(work),
        "plain_read_opus": v1.reader_rows, "v1_opus": lambda: v1.v1_rows("test"),
    }
    out: dict[str, Any] = {}
    for name, load in loaders.items():
        try:
            out[name] = {"rows": load()}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            out[name] = {"unavailable": f"{type(exc).__name__}: {exc}"}
    return out


def plain_control(campaign: Path, freeze: dict[str, Any], plain_dir: Path) -> dict[str, Any]:
    plain_freeze = C.load_freeze(plain_dir)
    checks = {
        "condition": plain_freeze["condition"] == "plain_read",
        "same_split": plain_freeze["split"] == freeze["split"],
        "same_model": plain_freeze["settings"]["model"] == freeze["settings"]["model"],
        "same_view_bundle": plain_freeze["inputs"]["view_bundle"]["sha256"] == freeze["inputs"]["view_bundle"]["sha256"],
        "same_upstream_pin": plain_freeze["inputs"]["upstream"]["declared_pin"] == freeze["inputs"]["upstream"]["declared_pin"],
    }
    if not all(checks.values()) or not (plain_dir / "closed.json").is_file():
        return {"unavailable": f"plain-read control does not match or is not closed: {checks}"}
    collected = C.read_json(plain_dir / "plain/collected/collected.json")
    if collected["freeze_sha256"] != C.freeze_digest(plain_dir):
        return {"unavailable": "collected.json belongs to another freeze"}
    findings = plain_dir / "plain/collected/findings.jsonl"
    if C.sha256_file(findings) != collected["findings_sha256"]:
        return {"unavailable": "collected findings changed after collection"}
    manifest = C.read_json(plain_dir / "plain/private-manifest.json")
    scored = {n for n, m in manifest["tasks"].items() if "aid" in m and n in collected["outputs"]}
    return {"rows": C.read_jsonl(findings), "scored_tasks": scored, "freeze": plain_freeze,
            "collected": {k: collected[k] for k in ("findings", "files", "missing", "invalid", "problems", "usage_note")},
            "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--plain-read-dir", help="closed plain_read campaign (separate frozen condition)")
    parser.add_argument("--witness-ledger", help="JSONL of independent witnesses (see module docs)")
    parser.add_argument("--no-baselines", action="store_true")
    parser.add_argument("--out", help="also copy results.json here")
    args = parser.parse_args(argv)

    campaign = C.owned_path(args.campaign_dir, must_exist=True)
    freeze = C.load_freeze(campaign)
    if freeze["condition"] != "agent_v2":
        raise ValueError("--campaign-dir must be the agent_v2 campaign")
    closed_path = campaign / "closed.json"
    if not closed_path.is_file():
        raise ValueError("the campaign is not closed: actors must finish before any key access")
    closed = C.read_json(closed_path)
    if closed["freeze_sha256"] != C.freeze_digest(campaign):
        raise ValueError("closed.json belongs to another freeze")
    marker = campaign / "scoring-started.json"
    if not marker.exists():
        C.write_new(marker, {"at": C.now(), "freeze_sha256": C.freeze_digest(campaign), "argv": sys.argv})
    work = campaign / "scoring"
    work.mkdir(exist_ok=True)
    with (work / "scoring-log.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"at": C.now(), "argv": sys.argv}) + "\n")

    audit = _score_module()
    key = audit.load_key(KEY_DIR, KEY_DIR.parent)
    split = freeze["split"]
    split_tasks = {name for name, row in key.tasks.items() if row["split"] == split}
    plan = C.read_json(campaign / "plan.json")
    plan_tasks = {t["task"] for t in plan["tasks"]}
    disclosed = set(freeze["registry"]["disclosed_names"])
    groups = freeze["direction_groups"]

    # ---- custody and per-task execution coverage -------------------------------------------
    custody: dict[str, dict[str, Any]] = {}
    for name, state in closed["tasks"].items():
        if state["exposed"]:
            custody[name] = C.validate_run(freeze, campaign, name)
    scored = {name for name, report in custody.items() if report["ok"]}
    v2_rows: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for name in sorted(scored):
        for finding in custody[name]["findings"]:
            v2_rows.append(finding["row"])
            refs.append({"task": name, "finding_sha256": finding["sha256"], "file": finding["file"],
                         "self_executed_witness": finding["self_executed_witness"]})
    derived_rows = []
    for row in v2_rows:
        cls = claim_class(row)
        derived_rows.append({**row, "category": C.CATEGORY[cls]} if cls in C.CATEGORY else row)

    results: dict[str, Any] = {
        "schema": "tb4.qa.v2.score/v1", "campaign_dir": str(campaign), "system_id": freeze["system_id"],
        "split": split, "freeze_sha256": C.freeze_digest(campaign), "scored_at": C.now(),
        "model": freeze["settings"]["model"], "lane": freeze["settings"].get("lane"),
        "population_check": {
            "key_split_tasks": len(split_tasks), "plan_tasks": len(plan_tasks),
            "in_key_not_plan": sorted(split_tasks - plan_tasks),
            "in_plan_not_key": sorted(plan_tasks - split_tasks),
        },
        "task_status": closed["status_counts"],
        "registry": {"excluded_unmeasured": sorted(n for n, t in closed["tasks"].items()
                                                  if t["status"] == "excluded_registry_exposure"),
                     "disclosed": freeze["registry"]["disclosed_names"]},
        "scored_tasks": sorted(scored),
        "custody_failed": {n: r["problems"] for n, r in sorted(custody.items()) if not r["ok"]},
        "direction_groups": groups,
        "notes": [
            "Headline: category_1to1 (v1 primary); locus_and_class and task_any_1to1 are sensitivities. "
            "Legacy broadcast credit is never reported as recall.",
            "Tasks v2 did not score (registry-excluded, failed custody, never started) stay in the "
            "split_all denominator as misses and are excluded from every common_with_v2 scope.",
            "v2 category is the model's own field, preserved. The *_class_derived_category row replaces "
            "only `category` with the class mapping v1 and the plain read applied, for a like-for-like "
            "comparison.",
            "Baselines use different models and conditions; a difference to v2 is not attributable "
            "to the harness alone. Only plain_read_sonnet shares the model.",
            "No clean-negative, false-positive-rate or bad-data-prevalence claim follows from a "
            "positive-only key.",
        ],
        "rows": {},
    }

    # ---- v2 per-task table and aggregates -----------------------------------------------------
    v2_result = run_scorer(v2_rows, "v2", work)
    derived_result = run_scorer(derived_rows, "v2_class_derived_category", work)
    hits = per_task_hits(key, v2_result)
    per_task = {}
    for name in sorted(plan_tasks):
        report = custody.get(name)
        state = closed["tasks"].get(name, {"status": "not_in_closed", "exposed": False})
        per_task[name] = {
            "status": state["status"], "registry": state.get("registry"), "exposed": state["exposed"],
            "custody_ok": None if report is None else report["ok"],
            "custody_problems": None if report is None else report["problems"],
            "session_status": report.get("status") if report else None,
            "stop_reason": report.get("stop_reason") if report else None,
            "claims": None if report is None or not report["ok"] else len(report["findings"]),
            "session_seconds": report.get("session_seconds") if report else None,
            "controller_wall_seconds": report.get("controller_wall_seconds") if report else None,
            "tokens": report.get("tokens") if report else None,
            "metered_usd": report.get("metered_usd") if report else None,
            "spend": report.get("spend") if report else None,
            "quota_weekly_first": report.get("quota_weekly_first") if report else None,
            "quota_weekly_last": report.get("quota_weekly_last") if report else None,
            "tool_calls_used": report.get("tool_calls_used") if report else None,
            "tool_calls_by_tool": report.get("tool_calls_by_tool") if report else None,
            "native_execution": report.get("native_execution") if report else None,
            "actual_model_observed": report.get("actual_model_observed") if report else None,
            "journal_sha256": report.get("journal_sha256") if report else None,
            "key_findings": hits["category_1to1"].get(name, (0, 0))[1] if name in split_tasks else None,
            "matched": {m: hits[m].get(name, (0, 0))[0] for m in MODES} if name in scored else None,
        }
    exposed_reports = [r for r in custody.values()]
    results["per_task"] = per_task
    results["aggregate"] = {
        "tasks_exposed": len(exposed_reports), "tasks_scored": len(scored),
        "session_seconds": total([r.get("session_seconds") for r in exposed_reports]),
        "controller_wall_seconds": total([r.get("controller_wall_seconds") for r in exposed_reports]),
        "tokens": total([r.get("tokens") for r in exposed_reports]),
        "metered_usd": total([r.get("metered_usd") for r in exposed_reports]),
        "metered_note": "$0 metered subscription cost is not zero quota consumption; see the per-task "
                        "weekly quota readings. Native reservations are not invoices. Unknown is null.",
        "native_execution": {
            "tasks_with_a_measured_execution": sum(r.get("native_measured", 0) > 0 for r in exposed_reports),
            "tasks_with_only_unmeasured_or_plan_only": sum(
                r.get("native_measured", 0) == 0 and r.get("native_not_measured", 0) > 0 for r in exposed_reports),
            "tasks_without_any_native_execution": sum(
                not r.get("native_measured", 0) and not r.get("native_not_measured", 0) for r in exposed_reports),
            "measured": sum(r.get("native_measured", 0) for r in exposed_reports),
            "not_measured": sum(r.get("native_not_measured", 0) for r in exposed_reports),
            "by_tool": {t: dict(c) for t, c in sorted(_sum_native(exposed_reports).items())},
            "note": "unmeasured/unadmitted native work is reported as such, never as zero reward",
        },
        "findings_with_self_executed_witness": sum(r["self_executed_witness"] for r in refs),
        "self_witness_note": "a witness recorded by the same session is not independent confirmation",
        "session_status_counts": dict(sorted(Counter(str(r.get("status")) for r in exposed_reports).items())),
    }

    # ---- row sets and baselines ---------------------------------------------------------------
    sets: dict[str, dict[str, Any]] = {
        "v2": {"label": f"Task QA v2 ({freeze['settings']['model']}, lane {freeze['settings'].get('lane')})",
               "model": freeze["settings"]["model"], "rows": v2_rows, "result": v2_result,
               "scored_tasks": scored, "condition": "this campaign; original Finding rows preserved"},
        "v2_class_derived_category": {
            "label": "Task QA v2 with class-derived category", "model": freeze["settings"]["model"],
            "rows": derived_rows, "result": derived_result, "scored_tasks": scored,
            "condition": "same findings; category replaced by the v1/plain-read class mapping"},
    }
    if not args.no_baselines and split == "test":
        for name, loaded in baseline_rows(split, work).items():
            meta = BASELINES[name]
            if "rows" not in loaded:
                results["rows"][name] = {**meta, "unavailable": loaded["unavailable"]}
                continue
            sets[name] = {**meta, "rows": loaded["rows"], "result": run_scorer(loaded["rows"], name, work),
                          "scored_tasks": set(split_tasks)}
    if args.plain_read_dir:
        plain = plain_control(campaign, freeze, C.owned_path(args.plain_read_dir, must_exist=True))
        if "rows" in plain:
            sets["plain_read_sonnet"] = {
                "label": "plain read, same model as v2 (separate frozen condition)",
                "model": plain["freeze"]["settings"]["model"],
                "condition": "no tools, no execution; plain_read_prompt.md; up to 8 findings per task; "
                             "instruction-confined subagent directories",
                "rows": plain["rows"], "result": run_scorer(plain["rows"], "plain_read_sonnet", work),
                "scored_tasks": plain["scored_tasks"], "collected": plain["collected"],
                "system_id": plain["freeze"]["system_id"]}
        else:
            results["rows"]["plain_read_sonnet"] = {"unavailable": plain["unavailable"]}

    task_hits = {name: per_task_hits(key, s["result"]) for name, s in sets.items()}
    for name, s in sets.items():
        common = scored & s["scored_tasks"] & split_tasks
        scopes = {"split_all": set(split_tasks), "common_with_v2": common,
                  "common_with_v2_excluding_disclosed": common - disclosed}
        row = {k: s[k] for k in ("label", "model", "condition", "system_id", "collected") if k in s}
        row["model_differs_from_v2"] = s.get("model") != freeze["settings"]["model"]
        row["tasks_scored_by_row"] = len(s["scored_tasks"] & split_tasks)
        row |= analyse(audit, key, groups, s["rows"], s["result"], scopes)
        if name not in {"v2", "v2_class_derived_category"}:
            row["paired_bootstrap_v2_minus_row"] = {
                mode: {scope: bootstrap(task_hits[name][mode], task_hits["v2"][mode], scopes[scope],
                                        BOOTSTRAP_SEED)
                       for scope in ("common_with_v2", "common_with_v2_excluding_disclosed")}
                for mode in MODES}
        results["rows"][name] = row

    # ---- unmatched v2 claims: UNVERIFIED unless independently witnessed ------------------------
    ledger = witness_ledger(Path(args.witness_ledger) if args.witness_ledger else None, campaign)
    preds, _ = audit.normalize_predictions(v2_rows)
    credited = {label for label, _ in v2_result["matches"]["category_1to1"]}
    unique = len({p.label for p in preds}) == len(preds)
    unmatched = []
    for pred in preds:
        if unique and pred.label in credited:
            continue
        ref = refs[pred.index]
        witness = ledger.get(ref["finding_sha256"])
        status = {"confirmed": "CONFIRMED_BY_INDEPENDENT_WITNESS",
                  "refuted": "REFUTED_BY_INDEPENDENT_WITNESS"}.get(witness["verdict"] if witness else "",
                                                                    "UNVERIFIED")
        row = v2_rows[pred.index]
        unmatched.append({
            **ref, "class": claim_class(row), "category": row.get("category"),
            "evidence_refs": row.get("evidence_refs"), "summary": str(row.get("summary"))[:400],
            "verification": {"status": status, "witness": witness},
        })
    results["unmatched_claims"] = {
        "basis": "not credited at category_1to1; being unmatched says nothing about correctness",
        "labels_unique": unique, "count": len(unmatched) if unique else None,
        "status_counts": dict(sorted(Counter(u["verification"]["status"] for u in unmatched).items())),
        "witness_ledger_entries_accepted": len(ledger), "claims": unmatched if unique else [],
    }

    target = work / "results.json"
    target.write_text(json.dumps(results, indent=1, sort_keys=True, default=str, allow_nan=False) + "\n",
                      encoding="utf-8")
    if args.out:
        Path(args.out).write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    for name, row in results["rows"].items():
        if "scopes" not in row:
            print(f"{name}: unavailable: {row.get('unavailable')}")
            continue
        parts = [f"{name} ({row['model']})", f"claims {row['claims_total']}"]
        for scope in ("split_all", "common_with_v2"):
            recall = row["scopes"][scope]["modes"]["category_1to1"]["recall"]
            strict = row["scopes"][scope]["modes"]["locus_and_class"]["recall"]
            task = row["scopes"][scope]["modes"]["task_any_1to1"]["recall"]
            parts.append(f"{scope}: category {recall['k']}/{recall['n']}, strict {strict['k']}/{strict['n']}, "
                         f"task-level {task['k']}/{task['n']}")
        print(" | ".join(parts))
    print(f"results: {target}")
    return 0


def _sum_native(reports: list[dict[str, Any]]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for report in reports:
        for tool, counts in (report.get("native_execution") or {}).items():
            out.setdefault(tool, Counter()).update(counts)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
