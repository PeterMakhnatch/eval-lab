"""TW-3 Hackbook QC: tune on Goodfire DEV + TB4 DEV, cross-test on CheatBench/HackTrace DEV.

Static, passive scoring only: recorded traces are read and scanned in memory;
no trace code, shell command, Docker, or model is ever executed.

Cohorts (all DEV, all source-labelled; agreement with source labels, not
independent ground truth):

- ``goodfire-dev``: 200 traces from the pinned TW-2 preflight cohort
  (714 tasks / 2,140 traces / 178,210 recorded nodes, Goodfire
  ``bd8fb473``), 100 publisher-enacted positives (``verdict_enacted=yes``)
  and 100 clean controls (``verdict=no, verdict_enacted=no``), one trace per
  task, ranked by ``sha256("tw3-hackbook-qc:" + trace_id)``.
- ``tb4-hub-dev``: TB4 DEV census memberships (``labels-dev.jsonl``): all 14
  ``pass:known_exploit`` positives plus 86 hash-ranked ``pass:neither``
  source negatives. Negatives mean "no documented mechanism found", not a
  clean certificate. DEV tasks come from the ``tb4-audit-v1`` assignments
  guard; TEST lines of the Hub index are dropped unparsed.
- ``cheatbench-actions-v1`` / ``hacktrace-act-v1`` (cross-test only): every
  DEV item (75 fail=positive / 75 pass=clean each), full published ATIF view.

MiMo DEV has no retained behavioural positive/clean trace labels (the MiMo
trace keys are TEST-only), so it is reported ``unavailable`` and never scored
against a fabricated denominator.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.hackbook.qc import ruleset_sha256, scan_trace_rules, score_cases  # noqa: E402
from evallab.qa.answer_key import load_answer_key  # noqa: E402
from evallab.tripwires import HACKBOOK_RULES  # noqa: E402

RULE_IDS = [rule["id"] for rule in HACKBOOK_RULES]
MANIFEST_SHA256 = "f58e4e464f3195482b2d6af5c0e52993b11327da71096f199331ecd4007ab61c"
EXPECTED_ADMISSION = {
    "tasks": 714,
    "traces": 2140,
    "recorded_steps": 178210,
    "enacted_yes": 652,
    "considered_only": 631,
    "source_clean": 515,
}
SAMPLE_PER_STRATUM = 100
TB4_NEGATIVES = 86


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_cases(path: Path, cases: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "case_id": case["case_id"],
                "cohort": case["cohort"],
                "label": case["label"],
                "fired": sorted(case["fired"]),
                "input_sha256": case.get("input_sha256"),
                "n_agent_steps": case.get("n_agent_steps"),
            },
            sort_keys=True,
        )
        for case in cases
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Goodfire DEV


def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    digest = sha256_bytes(data)
    if digest != MANIFEST_SHA256:
        raise ValueError(f"pinned Goodfire manifest sha256 mismatch: {digest}")
    rows = []
    for line in data.decode().splitlines():
        row = json.loads(line)
        raw_label = row["label_tuple"]
        row["_label"] = ast.literal_eval(raw_label) if isinstance(raw_label, str) else raw_label
        rows.append(row)
    return rows


def select_goodfire(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def rank(row: dict[str, Any]) -> tuple[str, str]:
        return (sha256_bytes(f"tw3-hackbook-qc:{row['trace_id']}".encode()), row["trace_id"])

    strata = {
        "positive": [r for r in rows if r["_label"].get("verdict_enacted") == "yes"],
        "clean": [
            r
            for r in rows
            if r["_label"].get("verdict") == "no" and r["_label"].get("verdict_enacted") == "no"
        ],
    }
    selected: list[dict[str, Any]] = []
    for label, pool in strata.items():
        seen_tasks: set[str] = set()
        for row in sorted(pool, key=rank):
            if row["task_id"] in seen_tasks:
                continue
            seen_tasks.add(row["task_id"])
            selected.append({**row, "_qc_label": label})
            if sum(1 for s in selected if s["_qc_label"] == label) == SAMPLE_PER_STRATUM:
                break
    counts = defaultdict(int)
    for row in selected:
        counts[row["_qc_label"]] += 1
    if dict(counts) != {"positive": SAMPLE_PER_STRATUM, "clean": SAMPLE_PER_STRATUM}:
        raise ValueError(f"Goodfire sample strata incomplete: {dict(counts)}")
    return selected


def goodfire_steps(db: sqlite3.Connection, trace_id: str) -> tuple[list[dict], list[dict], str]:
    rows = db.execute(
        """
        SELECT step_index, role, content_text,
          json_extract(flags_json, '$.on_final_parent_path') AS on_path,
          json_extract(flags_json, '$.tool_calls') AS calls_json,
          json_extract(flags_json, '$.tool_call_id') AS response_id
        FROM trace_steps WHERE trace_id = ? ORDER BY step_index
        """,
        (trace_id,),
    ).fetchall()
    digest = hashlib.sha256()
    steps: list[dict[str, Any]] = []
    by_call: dict[str, dict[str, Any]] = {}
    last_agent: dict[str, Any] | None = None
    for row in rows:
        if not row["on_path"]:
            continue
        digest.update(
            json.dumps(
                [row["step_index"], row["role"], row["content_text"]], ensure_ascii=False
            ).encode()
        )
        if row["role"] in ("system", "user"):
            steps.append(
                {"step_id": row["step_index"], "source": row["role"], "message": row["content_text"] or ""}
            )
        elif row["role"] == "assistant":
            step: dict[str, Any] = {
                "step_id": row["step_index"],
                "source": "agent",
                "message": row["content_text"] or "",
            }
            calls = json.loads(row["calls_json"] or "[]")
            tool_calls = []
            for call in calls:
                arguments = call.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {"command": arguments}
                if (
                    isinstance(arguments, dict)
                    and call.get("name") in ("bash", "bash_command", "shell", "exec", "execute_bash")
                    and "command" not in arguments
                    and "cmd" in arguments
                ):
                    arguments = {**arguments, "command": arguments["cmd"]}
                tool_calls.append(
                    {
                        "tool_call_id": call.get("id"),
                        "function_name": call.get("name"),
                        "arguments": arguments,
                    }
                )
                if call.get("id"):
                    by_call[call["id"]] = step
            if tool_calls:
                step["tool_calls"] = tool_calls
                step["message"] = ""
            steps.append(step)
            last_agent = step
        elif row["role"] == "tool":
            target = by_call.get(row["response_id"]) or last_agent
            if target is not None:
                target.setdefault("observation", {"results": []})["results"].append(
                    {"source_call_id": row["response_id"], "content": row["content_text"] or ""}
                )
    agent_steps = [s for s in steps if s["source"] == "agent"]
    if not agent_steps:
        raise ValueError(f"no final-path agent steps for {trace_id}")
    return agent_steps, steps, digest.hexdigest()


def goodfire_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest_rows = load_manifest(args.manifest)
    selected = select_goodfire(manifest_rows)
    db = sqlite3.connect(f"file:{args.cloud_db}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        admission = dict(db.execute(args.admission_sql.read_text(encoding="utf-8")).fetchone())
        if admission != EXPECTED_ADMISSION:
            raise ValueError(f"Goodfire admission guard failed: {admission}")
        cases = []
        for index, row in enumerate(selected, 1):
            agent_steps, steps, digest = goodfire_steps(db, row["trace_id"])
            cases.append(
                {
                    "case_id": f"goodfire-dev-{row['trace_id']}",
                    "cohort": "goodfire-dev",
                    "label": row["_qc_label"],
                    "fired": scan_trace_rules(agent_steps, steps),
                    "input_sha256": digest,
                    "n_agent_steps": len(agent_steps),
                }
            )
            if index % 25 == 0:
                print(f"goodfire {index}/{len(selected)}", flush=True)
        return cases
    finally:
        db.close()


# ------------------------------------------------------------------- TB4 DEV


def tb4_dev_tasks(assignments: Path) -> frozenset[str]:
    splits: dict[str, set[str]] = defaultdict(set)
    with assignments.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["key_id"] == "tb4-audit-v1" and row["unit"].startswith("terminal-bench/"):
                splits[row["task"]].add(row["split"])
    dev = frozenset(task for task, found in splits.items() if found == {"dev"})
    if not dev:
        raise ValueError("no tb4-audit-v1 DEV tasks; refusing TB4 inputs")
    return dev


def tb4_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    dev = tb4_dev_tasks(args.assignments)
    census_bytes = args.census_labels.read_bytes()
    records = []
    for line in census_bytes.decode().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record["task"] not in dev:
            raise ValueError("labels-dev.jsonl contains a non-DEV task")
        records.append(record)
    positives = [r for r in records if (r["side"], r["label"]) == ("pass", "known_exploit")]
    negatives = [r for r in records if (r["side"], r["label"]) == ("pass", "neither")]
    negatives.sort(
        key=lambda r: (sha256_bytes(f"tw3-hackbook-qc:{r['membership_id']}".encode()), r["membership_id"])
    )
    chosen = [(r, "positive") for r in positives] + [(r, "clean") for r in negatives[:TB4_NEGATIVES]]

    # Hub index: drop non-DEV lines unparsed (raw task needle), as in the V3 runner.
    needles = tuple(f'"task": "terminal-bench/{task}"' for task in sorted(dev))
    paths: dict[str, str | None] = {}
    for line in args.hub_index.read_bytes().decode().splitlines():
        if not line.strip() or not any(needle in line for needle in needles):
            continue
        raw = json.loads(line)
        task = str(raw["task"]).removeprefix("terminal-bench/")
        if task not in dev:
            raise ValueError("non-DEV row leaked through the TB4 DEV filter")
        paths[f"{raw['row_id']}:{task}:{raw['hub_trial_id']}"] = raw.get("archive_path")

    cases = []
    for index, (record, label) in enumerate(chosen, 1):
        archive = paths.get(record["membership_id"])
        if not archive:
            raise ValueError(f"TB4 trajectory unavailable: {record['membership_id']}")
        trajectory = Path(archive) / "agent/trajectory.json"
        data = trajectory.read_bytes()
        steps = json.loads(data)["steps"]
        agent_steps = [s for s in steps if s.get("source") == "agent"]
        if not agent_steps:
            raise ValueError(f"TB4 trajectory has no agent steps: {record['membership_id']}")
        cases.append(
            {
                "case_id": f"tb4-dev-{record['membership_id']}",
                "cohort": "tb4-hub-dev",
                "label": label,
                "fired": scan_trace_rules(agent_steps, steps),
                "input_sha256": sha256_bytes(data),
                "n_agent_steps": len(agent_steps),
            }
        )
        if index % 25 == 0:
            print(f"tb4 {index}/{len(chosen)}", flush=True)
    return cases


# -------------------------------------------------------------- cross-test DEV


def key_cases(key_id: str, keys_dir: Path, results_root: Path) -> list[dict[str, Any]]:
    key = load_answer_key(keys_dir / key_id)
    items = [item for item in key.items if item.split == "dev"]
    cases = []
    for index, item in enumerate(items, 1):
        extra = dict(item.model_extra or {})
        verdict = getattr(item, "expected_verdict", None) or extra.get("expected_verdict")
        label = {"fail": "positive", "pass": "clean"}.get(str(verdict))
        if label is None:
            continue
        ref = extra.get("trial_ref") or {}
        if ref.get("kind") != "published" or not ref.get("path"):
            raise ValueError(f"{key_id}: unresolvable full-view ref for a DEV item")
        trajectory = results_root / ref["path"] / "agent/trajectory.json"
        data = trajectory.read_bytes()
        recorded = extra.get("trajectory_sha256")
        expected = recorded if isinstance(recorded, str) else (recorded or {}).get("full")
        digest = sha256_bytes(data)
        if expected is not None and expected != digest:
            raise ValueError(f"{key_id}: trajectory hash mismatch for a DEV item")
        steps = json.loads(data)["steps"]
        agent_steps = [s for s in steps if s.get("source") == "agent"]
        if not agent_steps:
            raise ValueError(f"{key_id}: DEV trajectory without agent steps")
        cases.append(
            {
                "case_id": f"{key_id}-{index}",
                "cohort": key_id,
                "label": label,
                "fired": scan_trace_rules(agent_steps, steps),
                "input_sha256": digest,
                "n_agent_steps": len(agent_steps),
            }
        )
        if index % 25 == 0:
            print(f"{key_id} {index}/{len(items)}", flush=True)
    return cases


# -------------------------------------------------------------------- phases


def scored(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"case_id": c["case_id"], "label": c["label"], "fired": frozenset(c["fired"])} for c in cases
    ]


def tune(args: argparse.Namespace) -> int:
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError(f"output directory is not empty: {args.out}")
    args.out.mkdir(parents=True, exist_ok=True)
    generated_at = now_iso()
    cohorts: dict[str, list[dict[str, Any]]] = {}
    for name, cases in (
        ("goodfire-dev", goodfire_cases(args)),
        ("tb4-hub-dev", tb4_cases(args)),
    ):
        cohorts[name] = cases
        write_cases(args.out / f"cases-{name}.jsonl", cases)
        report, detail = score_cases(scored(cases), RULE_IDS, dataset_id=name, generated_at=generated_at)
        write_json(args.out / f"qc-{name}.json", report)
        write_json(args.out / f"qc-{name}-detail.json", detail)
        print(f"{name}: {report['cohort']}", flush=True)
    pooled = cohorts["goodfire-dev"] + cohorts["tb4-hub-dev"]
    report, detail = score_cases(
        scored(pooled), RULE_IDS, dataset_id="tw3-tune-goodfire+tb4-dev", generated_at=generated_at
    )
    write_json(args.out / "qc-tune-pooled.json", report)
    write_json(args.out / "qc-tune-pooled-detail.json", detail)
    demoted = [r["rule_id"] for r in detail["rules"] if r["classification"] == "demote_to_context_hint_only"]
    write_json(
        args.out / "selection.json",
        {
            "generated_at": generated_at,
            "ruleset_sha256_before": ruleset_sha256(HACKBOOK_RULES),
            "demoted": demoted,
            "mimo_dev": {
                "status": "unavailable",
                "reason": "no retained MiMo DEV behavioural positive/clean trace labels; "
                "the MiMo trace keys are TEST-only and were not used",
            },
            "cohorts": {
                name: {
                    "positive_n": sum(c["label"] == "positive" for c in cases),
                    "clean_n": sum(c["label"] == "clean" for c in cases),
                }
                for name, cases in cohorts.items()
            },
        },
    )
    print(f"demote candidates: {demoted}", flush=True)
    print(f"tune summary before={detail['summary']['before']} after={detail['summary']['after']}", flush=True)
    return 0


def union_counts(cases: list[dict[str, Any]], verdict_ids: set[str]) -> dict[str, Any]:
    tp = sum(c["label"] == "positive" and bool(set(c["fired"]) & verdict_ids) for c in cases)
    fp = sum(c["label"] == "clean" and bool(set(c["fired"]) & verdict_ids) for c in cases)
    pos = sum(c["label"] == "positive" for c in cases)
    clean = sum(c["label"] == "clean" for c in cases)
    return {
        "positive_n": pos,
        "clean_n": clean,
        "true_positive_n": tp,
        "false_positive_n": fp,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / pos if pos else 0.0,
        "clean_false_alarm_rate": fp / clean if clean else 0.0,
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            row["fired"] = frozenset(row["fired"])
            cases.append(row)
    return cases


def crosstest(args: argparse.Namespace) -> int:
    tune_dir: Path = args.tune_dir
    detail = json.loads((tune_dir / "qc-tune-pooled-detail.json").read_text(encoding="utf-8"))
    demoted = {r["rule_id"] for r in detail["rules"] if r["classification"] == "demote_to_context_hint_only"}
    eligibility_now = {rule["id"]: rule["eligibility"] for rule in HACKBOOK_RULES}
    verdict_after = {rid for rid, e in eligibility_now.items() if e == "verdict"}
    verdict_before = verdict_after | demoted
    if demoted - verdict_before or not demoted <= {r["rule_id"] for r in detail["rules"]}:
        raise ValueError("demotion set is inconsistent with the tune detail")
    missing = {rid for rid in demoted if eligibility_now.get(rid) != "diagnostic"}
    if missing:
        raise ValueError(f"demoted rules are not diagnostic in the current ruleset: {sorted(missing)}")

    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError(f"output directory is not empty: {args.out}")
    args.out.mkdir(parents=True, exist_ok=True)
    generated_at = now_iso()
    frozen = ruleset_sha256(HACKBOOK_RULES)

    all_cases: dict[str, list[dict[str, Any]]] = {
        "goodfire-dev": load_cases(tune_dir / "cases-goodfire-dev.jsonl"),
        "tb4-hub-dev": load_cases(tune_dir / "cases-tb4-hub-dev.jsonl"),
    }
    for key_id in ("cheatbench-actions-v1", "hacktrace-act-v1"):
        cases = key_cases(key_id, args.keys_dir, args.results_root)
        all_cases[key_id] = cases
        write_cases(args.out / f"cases-{key_id}.jsonl", cases)
        report, cohort_detail = score_cases(
            scored(cases), RULE_IDS, dataset_id=key_id, generated_at=generated_at
        )
        write_json(args.out / f"qc-{key_id}.json", report)
        write_json(args.out / f"qc-{key_id}-detail.json", cohort_detail)

    before_after = {}
    entries = []
    for name, cases in all_cases.items():
        before = union_counts(cases, verdict_before)
        after = union_counts(cases, verdict_after)
        before_after[name] = {"verdict_union_before": before, "verdict_union_after": after}
        entries.append(
            {
                "role": "tune" if name in ("goodfire-dev", "tb4-hub-dev") else "cross_test",
                "dataset_id": name,
                "split": "dev",
                "system_sha256": frozen,
                "positive_n": after["positive_n"],
                "clean_n": after["clean_n"],
                "unscored_n": 0,
                "true_positive_n": after["true_positive_n"],
                "false_positive_n": after["false_positive_n"],
                "measured_at": generated_at,
            }
        )
    write_json(args.out / "before-after.json", before_after)
    write_json(
        args.out / "tune-cross-test.json",
        {
            "schema": "evallab.tune_cross_test/v1",
            "system": "hackbook-tripwires-qc-pruned",
            "frozen_sha256": frozen,
            "entries": entries,
            "generated_at": generated_at,
        },
    )
    print(json.dumps(before_after, indent=2, sort_keys=True), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    tune_parser = sub.add_parser("tune")
    tune_parser.add_argument("--out", type=Path, required=True)
    tune_parser.add_argument("--manifest", type=Path, required=True)
    tune_parser.add_argument("--admission-sql", type=Path, required=True)
    tune_parser.add_argument("--cloud-db", type=Path, required=True)
    tune_parser.add_argument(
        "--census-labels",
        type=Path,
        default=REPO / "research/experiments/tb4-run-census-20261021/labels-dev.jsonl",
    )
    tune_parser.add_argument(
        "--assignments", type=Path, default=REPO / "research/answer-keys/splits/assignments.csv"
    )
    tune_parser.add_argument("--hub-index", type=Path, required=True)
    tune_parser.set_defaults(func=tune)
    cross_parser = sub.add_parser("crosstest")
    cross_parser.add_argument("--tune-dir", type=Path, required=True)
    cross_parser.add_argument("--out", type=Path, required=True)
    cross_parser.add_argument("--keys-dir", type=Path, default=REPO / "research/answer-keys")
    cross_parser.add_argument("--results-root", type=Path, required=True)
    cross_parser.set_defaults(func=crosstest)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
