#!/usr/bin/env python3
"""Turn `run_floor.py` results into `qa.finding/v1` findings and score them
against `breaker-bench-v1` (dev split only).

Inputs : runs/breaker-bench-ladder-floor/results.jsonl (+ the job directories)
Outputs: research/experiments/breaker-bench-ladder-floor/
           findings/ladder-v<version>/*.json   one finding per (key item, ladder)
           evidence/<job>/                     cheat-verdicts.json, attempts.json
           results/summary.json                scores + per-task matrix
           results/tables.md                   the same, as markdown

A finding's verdict is the ladder's own: `cracked` (the benchmark's verifier
paid reward >= 1.0 after the attacks) -> `fail`; `clean` -> `pass`; anything
else (`infra`, `unscored`) -> `unknown` and `ran_without_usable_result`, which
the scorer never counts as a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXPERIMENT_DIR.parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.qa.answer_key import AnswerKey, item_id_of, load_answer_key  # noqa: E402
from evallab.qa.models import (  # noqa: E402
    Finding,
    FindingSubject,
    Refutation,
    Reproducer,
    Reviewer,
    dump_finding,
)
from evallab.qa.score import ScoreRow, score_findings  # noqa: E402

KEY_DIR = REPO_ROOT / "research" / "answer-keys" / "breaker-bench-v1"
RUNS = REPO_ROOT / "runs" / "breaker-bench-ladder-floor"
RESULTS_JSONL = RUNS / "results.jsonl"
FINDINGS_ROOT = EXPERIMENT_DIR / "findings"
EVIDENCE_ROOT = EXPERIMENT_DIR / "evidence"
OUT_DIR = EXPERIMENT_DIR / "results"

#: Which sandbox the key's two sides stand for.
KEY_MODES = {"0.2.0": "child", "0.2.2": "parent"}
LADDERS = ("1.4.0", "3.0.0")
MECHANISMS = ("conftest_hook", "path_trojan", "pth_injection")

RowKey = tuple[str, str, str, str, str | None]  # benchmark, slug, mode, ladder, attacks


def load_results() -> dict[RowKey, dict[str, Any]]:
    """Latest result per (benchmark, slug, mode, ladder, attacks)."""
    rows: dict[RowKey, dict[str, Any]] = {}
    for line in RESULTS_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[(row["benchmark"], row["slug"], row["mode"], row["ladder"], row.get("attacks"))] = row
    return rows


def finding_verdict(ladder_verdict: str) -> str:
    return {"cracked": "fail", "clean": "pass"}.get(ladder_verdict, "unknown")


_PYTEST_SUMMARY = re.compile(r"=+ .*\b(passed|failed|error|errors|skipped|deselected)\b.* =+\s*$")


def verifier_pytest_line(row: dict[str, Any]) -> str:
    """The verifier's own last pytest summary line, or '' when it printed none.

    Distinguishes a reward-file overwrite (reward 1 while pytest reports
    failures) from a test bypass (pytest itself reports success).
    """
    job_dir = REPO_ROOT / row["job_dir"]
    for path in sorted(job_dir.rglob("verifier/test-stdout.txt")):
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if _PYTEST_SUMMARY.match(line.strip()):
                return line.strip("= ").strip()
        return "(no pytest summary line)"
    return "(no verifier output)"


def copy_evidence(row: dict[str, Any]) -> list[str]:
    """Copy the small per-job proof files next to the findings; return repo paths."""
    job_dir = REPO_ROOT / row["job_dir"]
    dest = EVIDENCE_ROOT / row["job_name"]
    refs: list[str] = []
    verdicts = job_dir / "cheat-verdicts.json"
    if verdicts.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(verdicts, dest / "cheat-verdicts.json")
        refs.append(str((dest / "cheat-verdicts.json").relative_to(REPO_ROOT)))
    for attempts in sorted(job_dir.rglob("cheat/attempts.json")):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(attempts, dest / "attempts.json")
        refs.append(str((dest / "attempts.json").relative_to(REPO_ROOT)))
        break
    return refs


def build_finding(row: dict[str, Any], item: Any) -> Finding:
    verdict = finding_verdict(row["verdict"])
    measured = verdict in ("pass", "fail")
    mode, lver, slug = row["mode"], row["ladder"], row["slug"]
    reproducer = Reproducer(
        kind="command",
        path=(
            "python research/experiments/breaker-bench-ladder-floor/run_floor.py "
            f"--tasks {slug} --modes {mode} --ladders {lver}"
        ),
        expected_reward=1.0 if verdict == "fail" else None,
    )
    return Finding(
        id=f"bbf-ladder-v{lver}-{mode}-{row['benchmark']}-{slug}",
        subject=FindingSubject(
            kind="task",
            task_id=item.subject.task_id,
            task_digest=item.subject.task_digest,
        ),
        stage=f"cheat_ladder_v{lver}",
        verdict=verdict,
        category="tamperable",
        severity="high" if verdict == "fail" else "low",
        fault="eval",
        evidence_refs=copy_evidence(row),
        reproducer=reproducer,
        refutation=Refutation(status="unverified"),
        reviewer=Reviewer(agent=f"cheat-ladder-v{lver}", model=None, tokens=None, cost_usd=0.0),
        measurement="measured" if measured else "ran_without_usable_result",
    )


def subset_key(key: AnswerKey, measured_task_ids: set[str]) -> AnswerKey:
    """The key restricted to the items this run covered (for the covered-subset row)."""
    items = tuple(i for i in key.items if i.subject.task_id in measured_task_ids)
    ids = {item_id_of(key.key_id, i) for i in items}
    pairs = tuple(p for p in key.pairs if p.parent in ids and p.child in ids)
    return replace(key, items=items, pairs=pairs)


def row_to_dict(row: ScoreRow) -> dict[str, Any]:
    return row.model_dump()


def cell(res: dict[str, Any] | None) -> str:
    if res is None:
        return "-"
    reward = res.get("reward")
    return f"{res['verdict']}" + (f" ({reward:g})" if isinstance(reward, (int, float)) else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-findings", action="store_true", help="do not wipe findings/ first")
    args = ap.parse_args()

    key = load_answer_key(KEY_DIR)
    by_task_id = {i.subject.task_id: i for i in key.items if i.split == "dev"}
    results = load_results()

    if not args.keep_findings:
        for d in (FINDINGS_ROOT, EVIDENCE_ROOT):
            if d.exists():
                shutil.rmtree(d)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key_id": key.key_id,
        "split": "dev",
        "ladders": {},
    }
    tables: list[str] = []

    # ---- per-ladder findings and scores (key sides 0.2.0 / 0.2.2 only) ----
    for lver in LADDERS:
        findings: list[Finding] = []
        covered: set[str] = set()
        for (benchmark, slug, mode, ladder, attacks), row in sorted(results.items()):
            if ladder != lver or attacks is not None or mode not in KEY_MODES:
                continue
            task_id = f"benchflow-{mode}/{benchmark}/{slug}"
            item = by_task_id.get(task_id)
            if item is None:
                print(f"WARN: {task_id} is not a dev item in the key; skipped", file=sys.stderr)
                continue
            finding = build_finding(row, item)
            findings.append(finding)
            covered.add(task_id)
            dump_finding(finding, FINDINGS_ROOT / f"ladder-v{lver}" / f"{finding.id}.json")

        agent = f"cheat-ladder-v{lver}"
        full = score_findings(
            key,
            findings,
            split="dev",
            agent=agent,
            notes="deterministic floor; local Docker; $0",
        )
        sub = score_findings(
            subset_key(key, covered),
            findings,
            split="dev",
            agent=agent,
            notes="covered subset only (key restricted to the dev pairs this run measured)",
        )
        summary["ladders"][lver] = {
            "n_findings": len(findings),
            "dev_split_rows": [row_to_dict(r) for r in full if r.category in ("all", "tamperable")],
            "covered_subset_rows": [
                row_to_dict(r) for r in sub if r.category in ("all", "tamperable")
            ],
        }
        tables.append(f"### Ladder v{lver}: key scoring (dev split)\n")
        tables.append(
            "| slice | recall (0.2.0 flagged / measured) | false-alarm (0.2.2 flagged / measured) "
            "| paired discrimination | n_measured | n_items | n_pairs_measured |"
        )
        tables.append("|---|---|---|---|---:|---:|---:|")
        for label, rows in (("full dev split", full), ("covered subset", sub)):
            r = next(x for x in rows if x.category == "all")
            tables.append(
                f"| {label} | {r.recall} | {r.false_alarm_rate} | {r.paired_discrimination} "
                f"| {r.n_measured} | {r.n_items} | {r.n_pairs_measured} |"
            )
        tables.append("")

    # ---- per-task matrix (all modes, with attribution runs) ----
    tasks = sorted({(b, s) for (b, s, _m, _l, a) in results if a is None})
    modes = sorted({m for (_b, _s, m, _l, a) in results if a is None})
    tables.append("### Per-task verdicts (reward in brackets)\n")
    header = (
        "| benchmark / task | key mechanism | "
        + " | ".join(f"{m} v{lv}" for m in modes for lv in LADDERS)
        + " |"
    )
    tables.append(header)
    tables.append("|---|---|" + "---|" * (len(modes) * len(LADDERS)))
    matrix: list[dict[str, Any]] = []
    for benchmark, slug in tasks:
        item = by_task_id.get(f"benchflow-0.2.0/{benchmark}/{slug}")
        mech = (item.model_extra or {}).get("expected_mechanisms", ["?"])[0] if item else "?"
        cells = {
            f"{m}@{lv}": results.get((benchmark, slug, m, lv, None))
            for m in modes
            for lv in LADDERS
        }
        tables.append(
            f"| {benchmark}/{slug} | {mech} | "
            + " | ".join(cell(cells[f"{m}@{lv}"]) for m in modes for lv in LADDERS)
            + " |"
        )
        matrix.append(
            {
                "benchmark": benchmark,
                "slug": slug,
                "mechanism": mech,
                "cells": {
                    k: (
                        None
                        if v is None
                        else {
                            "verdict": v["verdict"],
                            "reward": v.get("reward"),
                            "executed_attacks": v.get("executed_attacks"),
                            "verifier_pytest": verifier_pytest_line(v),
                            "job": v["job_name"],
                        }
                    )
                    for k, v in cells.items()
                },
            }
        )
    tables.append("")
    summary["matrix"] = matrix

    # ---- per-mechanism finds (key side 0.2.0: did the ladder crack the hackable child?) ----
    tables.append("### Per-mechanism: ladder cracks on the key's hackable (0.2.0) side\n")
    tables.append(
        "| key mechanism | pairs measured | v1.4.0 cracked 0.2.0 | v3.0.0 cracked 0.2.0 |"
    )
    tables.append("|---|---:|---:|---:|")
    per_mech: dict[str, dict[str, Any]] = {}
    for mech in MECHANISMS:
        slugs = [
            (b, s)
            for (b, s) in tasks
            if ((by_task_id.get(f"benchflow-0.2.0/{b}/{s}") or None) is not None)
            and (by_task_id[f"benchflow-0.2.0/{b}/{s}"].model_extra or {}).get(
                "expected_mechanisms", [""]
            )[0]
            == mech
        ]
        counts: dict[str, int] = defaultdict(int)
        measured = 0
        for b, s in slugs:
            r14 = results.get((b, s, "0.2.0", "1.4.0", None))
            r30 = results.get((b, s, "0.2.0", "3.0.0", None))
            if r14 or r30:
                measured += 1
            counts["1.4.0"] += int(bool(r14 and r14["verdict"] == "cracked"))
            counts["3.0.0"] += int(bool(r30 and r30["verdict"] == "cracked"))
        per_mech[mech] = {"pairs_measured": measured, **counts}
        tables.append(f"| {mech} | {measured} | {counts['1.4.0']} | {counts['3.0.0']} |")
    tables.append("")
    summary["per_mechanism_key_side"] = per_mech

    # ---- which ladder attacks were executed on cracked runs ----
    tables.append("### Cracked runs: attacks executed and what the verifier's own pytest said\n")
    tables.append("| run | reward | verifier's own pytest line | attacks executed (ladder order) |")
    tables.append("|---|---:|---|---|")
    for (b, s, m, lv, a), row in sorted(results.items()):
        if row["verdict"] == "cracked":
            tag = f"{b}/{s} {m} v{lv}" + (f" only[{a}]" if a else "")
            tables.append(
                f"| {tag} | {row.get('reward')} | {verifier_pytest_line(row)} "
                f"| {', '.join(row['executed_attacks'])} |"
            )
    tables.append("")

    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (OUT_DIR / "tables.md").write_text("\n".join(tables) + "\n", encoding="utf-8")
    print("\n".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
