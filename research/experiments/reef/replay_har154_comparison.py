#!/usr/bin/env python3
"""HAR-154 saved-run comparison replay (CPU-only, $0 external spend).

Reads the published HAR-110 split/table (RESULTS.md, split.json,
results-v2-trials.jsonl) from this repository, resolves the 18 development
cells (3 arms x 6 tasks) to retained published jobs under
``<results-root>/2026-09-30/<CARD>-<job>``, verifies each cell's retained
trial identity, verifier reward and stop class, then runs the existing
``evallab.cohort`` ``write_comparison``/``compare`` API rooted at
``--results-root`` for (plain, candidate) and (seed-addendum, candidate) with
k=1, pairing ``task_digest`` and declared variable
``preamble_content_sha256`` in causal mode. All eligibility/refusal guards
stay intact; a setup mismatch surfaces as a causal refusal, never a relaxed
rerun.

The comparison outputs are a raw-verifier-reward audit only. The source
study's GEPA policy scores (upstream-fetch-zero) are reported separately from
retained evidence without rewriting raw jobs and without treating the two
scores as interchangeable. A missing infra reward stays null. No new
statistics are implemented and no adoption is claimed from development data.

Typical invocation from the repository root::

    uv run --no-sync python research/experiments/reef/replay_har154_comparison.py \
      --results-root ~/Developer/eval-lab-results --out <new-path>/har154-replay
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parents[3]
HAR110 = REPO / "research/experiments/har110-python-gepa"
SPLIT_NAME = "split.json"
TABLE_NAME = "results-v2-trials.jsonl"
RESULTS_NAME = "RESULTS.md"

PUBLISHED_DATE = "2026-09-30"
KNOWN_ARMS = ("plain", "seed-addendum", "gepa-candidate")
CANDIDATE_ARM = "gepa-candidate"
BASELINE_ARMS = ("plain", "seed-addendum")
EXTERNAL_SPEND_USD = 0.0
LIMITATIONS = (
    "n=1 per task and arm on development tasks only; a first-look audit, "
    "not evidence of an effect and not an adoption claim. "
    "002256/gepa-candidate has no verifier reward (infra BadGatewayError); "
    "it stays null, never zero-filled. GEPA policy scores apply the "
    "upstream-fetch-zero rule and are not interchangeable with raw verifier "
    "rewards. Comparisons run in causal mode so setup mismatches refuse "
    "instead of relaxing."
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"replay_har154_comparison: refusing: {message}")


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_table(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            fail(f"{path.name} line {lineno} is not valid JSON")
        if not isinstance(value, dict):
            fail(f"{path.name} line {lineno} is not an object")
        rows.append(value)
    return rows


def source_card(row: dict) -> str:
    source = row.get("source")
    if isinstance(source, str) and source.startswith("HAR-104"):
        return "HAR-104"
    if isinstance(source, str) and source.startswith("HAR-110"):
        return "HAR-110"
    fail(f"unknown source {source!r} for task {row.get('task')!r}")


def expected_job_name(row: dict) -> str:
    arm, task, job = row.get("arm"), row.get("task"), row.get("job")
    if arm == "plain":
        if job != f"har104-d-{task}":
            fail(f"plain row for task {task!r} has unexpected job {job!r}")
        return str(job)
    if arm in ("seed-addendum", CANDIDATE_ARM):
        if not isinstance(job, str) or not job:
            fail(f"{arm} row for task {task!r} has no full job name")
        return job
    fail(f"unknown arm {arm!r} for task {task!r}")


def retained_trial(trial_dir: Path) -> dict:
    try:
        return json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read retained trial result in {trial_dir}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-root", required=True, help="canonical published-results root")
    parser.add_argument("--out", required=True, help="new output directory (must not exist)")
    args = parser.parse_args()

    results_root = Path(args.results_root).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if out.exists():
        fail(f"--out already exists: {out}")
    if not results_root.is_dir():
        fail(f"--results-root is not a directory: {results_root}")
    if out.is_relative_to(results_root):
        fail("--out must be outside the read-only published-results root")

    split_path, table_path, results_path = (
        HAR110 / SPLIT_NAME,
        HAR110 / TABLE_NAME,
        HAR110 / RESULTS_NAME,
    )
    for path in (split_path, table_path, results_path):
        if not path.is_file():
            fail(f"missing source evidence: {path}")
    try:
        split = json.loads(split_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"split.json is not valid JSON: {exc}")
    development = split.get("development", [])
    dev_tasks = sorted(task.removeprefix("format-code-task-") for task in development)
    if len(dev_tasks) != 6 or any(not task for task in dev_tasks):
        fail(f"split.json development is not the declared 6-task set: {development!r}")

    rows = [row for row in load_table(table_path) if row.get("split") == "development"]
    arms = sorted({row.get("arm") for row in rows})
    if arms != sorted(KNOWN_ARMS):
        fail(f"development rows cover arms {arms!r}, want {sorted(KNOWN_ARMS)!r}")
    cells = {(row.get("arm"), row.get("task")) for row in rows}
    want = {(arm, task) for arm in KNOWN_ARMS for task in dev_tasks}
    if cells != want or len(rows) != len(want):
        fail(f"development cells {sorted(cells)!r} are not the unique 3x6 set {sorted(want)!r}")

    provenance: list[dict] = []
    by_arm: dict[str, list[dict]] = {arm: [] for arm in KNOWN_ARMS}
    for row in sorted(rows, key=lambda r: (r["arm"], r["task"])):
        arm, task = row["arm"], row["task"]
        card = source_card(row)
        job_name = expected_job_name(row)
        job_dir = results_root / PUBLISHED_DATE / f"{card}-{job_name}"
        if not job_dir.is_dir():
            fail(f"missing published job for ({arm}, {task}): {job_dir}")
        job_config_path = job_dir / "config.json"
        try:
            job_config = json.loads(job_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read retained job identity {job_config_path}: {exc}")
        if job_config.get("job_name") != job_name:
            fail(f"retained job name does not match source row: {job_dir}")
        job_result_path = job_dir / "result.json"
        try:
            job_result = json.loads(job_result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read published job result {job_result_path}: {exc}")
        if not job_result.get("finished_at"):
            fail(f"published job is not finished: {job_dir}")
        nested = sorted(
            p for p in job_dir.iterdir() if p.is_dir() and (p / "result.json").is_file()
        )
        if len(nested) != 1:
            fail(f"ambiguous published job match for ({arm}, {task}): {len(nested)} nested trials")
        trial = retained_trial(nested[0])
        trial_name = trial.get("trial_name")
        if not isinstance(trial_name, str) or trial_name != nested[0].name:
            fail(f"retained trial identity mismatch in {nested[0]}")
        task_name = trial.get("task_name") or ""
        if not task_name.endswith(f"format-code-task-{task}"):
            fail(f"retained trial {trial_name!r} runs {task_name!r}, want task {task!r}")
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        retained_reward = rewards.get("reward")
        if retained_reward is not None and (
            isinstance(retained_reward, bool)
            or not isinstance(retained_reward, (int, float))
            or not math.isfinite(retained_reward)
        ):
            fail(f"retained trial {trial_name!r} carries a non-numeric reward")
        if (retained_reward is None) != (row.get("reward") is None) or (
            retained_reward is not None and float(retained_reward) != float(row["reward"])
        ):
            fail(
                f"grade discrepancy for ({arm}, {task}): table reward "
                f"{row.get('reward')!r} vs retained {retained_reward!r}"
            )
        retained_stop = (
            (trial.get("exception_info") or {}).get("exception_type")
        ) or "agent_finished"
        if retained_stop != row.get("stop"):
            fail(
                f"stop discrepancy for ({arm}, {task}): table stop "
                f"{row.get('stop')!r} vs retained {retained_stop!r}"
            )
        fetch = row.get("fetch") or []
        if retained_reward is None:
            expected_score = None
        else:
            expected_score = 0.0 if fetch else float(retained_reward)
        if (expected_score is None) != (row.get("gepa_score") is None) or (
            expected_score is not None and float(row["gepa_score"]) != expected_score
        ):
            fail(
                f"policy-score discrepancy for ({arm}, {task}): table gepa_score "
                f"{row.get('gepa_score')!r} vs fetch-rule {expected_score!r}"
            )
        cell = {
            "arm": arm,
            "task": task,
            "source": row["source"],
            "job": job_name,
            "job_dir": f"{PUBLISHED_DATE}/{card}-{job_name}",
            "trial_name": trial_name,
            "trial_result_sha256": sha256_file(nested[0] / "result.json"),
            "job_config_sha256": sha256_file(job_config_path),
            "job_result_sha256": sha256_file(job_result_path),
            "retained_reward": retained_reward,
            "retained_stop": retained_stop,
            "table_gepa_score": row.get("gepa_score"),
            "table_fetch": fetch,
        }
        provenance.append(cell)
        by_arm[arm].append(cell)

    from evallab.cohort import write_comparison
    from evallab.schemas import CohortComparisonSpec, CohortSelector

    out.mkdir(parents=True)
    comparison_summaries = []
    for baseline in BASELINE_ARMS:
        comparison_id = f"har154-{baseline}-vs-candidate"
        cohorts = []
        for label in (baseline, CANDIDATE_ARM):
            cells_sorted = sorted(by_arm[label], key=lambda c: c["task"])
            cohorts.append(
                CohortSelector(
                    label=label,
                    paths=[cell["job_dir"] for cell in cells_sorted],
                    trial_names=[cell["trial_name"] for cell in cells_sorted],
                )
            )
        spec = CohortComparisonSpec(
            comparison_id=comparison_id,
            experiment_id="har154-comparison-replay",
            declared_variable="preamble_content_sha256",
            mode="causal",
            reward_name="reward",
            pass_threshold=1.0,
            pass_k=[1],
            pairing_key="task_digest",
            cohorts=cohorts,
        )
        spec_path = out / f"{comparison_id}.spec.json"
        spec_path.write_text(
            json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        json_path, markdown_path, report = write_comparison(
            spec_path, repo_root=results_root, output_root=out
        )
        paired = report["paired"][0] if report["paired"] else {}
        comparison_summaries.append(
            {
                "comparison_id": comparison_id,
                "baseline": baseline,
                "comparison": CANDIDATE_ARM,
                "spec_digest": report["spec_digest"],
                "spec_path": spec_path.name,
                "json_path": json_path.name,
                "markdown_path": markdown_path.name,
                "validity_warnings": report["validity_warnings"],
                "n_pairs": paired.get("n_pairs"),
                "rankable": paired.get("rankable"),
                "ranking": paired.get("ranking"),
                "refusal_reasons": paired.get("refusal_reasons"),
                "cohort_outcomes": [
                    {
                        key: cohort[key]
                        for key in (
                            "label",
                            "n_total",
                            "capability_denominator",
                            "trial_pass_count",
                            "exception_count",
                            "missing_reward_count",
                        )
                    }
                    for cohort in report["cohorts"]
                ],
                "statement": paired.get("statement"),
                "wins": paired.get("wins"),
                "ties": paired.get("ties"),
                "losses": paired.get("losses"),
                "paired_exact_p_value": paired.get("paired_exact_p_value"),
            }
        )

    def arm_summary(key: str) -> dict:
        scored = [c["table_gepa_score"] for c in provenance if c["arm"] == key]
        raw = [c["retained_reward"] for c in provenance if c["arm"] == key]
        return {
            "n": len(scored),
            "raw_verifier_rewards": sorted(raw, key=lambda v: (v is None, v)),
            "raw_passes": sum(1 for v in raw if v is not None and v >= 1.0),
            "raw_unscored_null": sum(1 for v in raw if v is None),
            "policy_scores": sorted(scored, key=lambda v: (v is None, v)),
            "policy_passes": sum(1 for v in scored if v is not None and v >= 1.0),
            "fetch_flagged_trials": sum(
                bool(c["table_fetch"]) for c in provenance if c["arm"] == key
            ),
        }

    for cell in provenance:
        job = results_root / cell["job_dir"]
        for path, key in (
            (job / cell["trial_name"] / "result.json", "trial_result_sha256"),
            (job / "config.json", "job_config_sha256"),
            (job / "result.json", "job_result_sha256"),
        ):
            if sha256_file(path) != cell[key]:
                fail(f"retained source changed during comparison: {path}")

    receipt = {
        "experiment": "har154-comparison-replay",
        "repo_sources": {
            "split": {
                "path": split_path.relative_to(REPO).as_posix(),
                "sha256": sha256_file(split_path),
                "split_digest": split.get("split_digest"),
            },
            "table": {
                "path": table_path.relative_to(REPO).as_posix(),
                "sha256": sha256_file(table_path),
                "development_cells": len(rows),
            },
            "results": {
                "path": results_path.relative_to(REPO).as_posix(),
                "sha256": sha256_file(results_path),
            },
        },
        "results_root": str(results_root),
        "out": str(out),
        "cells": provenance,
        "raw_verifier_reward_summary": {arm: arm_summary(arm) for arm in KNOWN_ARMS},
        "policy_score_note": (
            "GEPA policy score equals the retained verifier reward except that "
            "a run with upstream fetch evidence scores 0 (upstream_fetch_zero). "
            "Scores are reported side by side and are not interchangeable; "
            "comparisons use raw verifier rewards only."
        ),
        "comparisons": comparison_summaries,
        "source_preservation": {
            "files_checked": 3 * len(provenance),
            "scope": "trial results, job results, job configs",
            "unchanged": True,
        },
        "code_hashes": {
            "script": sha256_file(Path(__file__).resolve()),
            "evallab_cohort": sha256_file(Path(importlib.util.find_spec("evallab.cohort").origin)),
        },
        "external_spend_usd": EXTERNAL_SPEND_USD,
        "limitations": LIMITATIONS,
    }
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    outcome = {
        "out": str(out),
        "development_cells": len(provenance),
        "comparisons": [c["comparison_id"] for c in comparison_summaries],
        "external_spend_usd": EXTERNAL_SPEND_USD,
    }
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
