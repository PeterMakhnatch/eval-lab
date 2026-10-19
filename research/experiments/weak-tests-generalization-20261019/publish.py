#!/usr/bin/env python3
"""Retain native survey evidence and publish only instruction-adjudicated labels.

Collection reads completed campaigns, never their active worktrees. Building is
purely local and deterministic from the retained inventory plus reviews. Missing
controls, source bindings, reviews or test outputs cannot become clean labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.qa.models import AnswerKeyItem, Finding

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RUNS = REPO / "runs/weak-tests-generalization-20261019"
EVIDENCE = REPO / "research/evidence/runs/weak-tests-generalization-20261019"
KEY_ID = "weak-tests-generalization-20261019"
KEY = REPO / "research/answer-keys" / KEY_ID


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def candidates(report: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    if report.get("dropped_code") and any(
        grade["label"] == "confirm" and grade["status"] == "scored" and grade["reward"] == 1
        for grade in report.get("grades", [])
    ):
        rows.append({"id": "subset", "grade": "confirm", "patch": "minimal.patch", "script": "minimal.sh"})
    rows.extend({
        "id": survivor["id"], "grade": "mutant:" + survivor["id"],
        "patch": "mutants/" + survivor["id"] + ".patch",
        "script": "mutants/" + survivor["id"] + ".sh",
    } for survivor in report.get("mutants", {}).get("survivors", []))
    return rows


def collect(include_prior: bool, owned_attempts: list[str] | None = None) -> None:
    if (HERE / "inventory.json").exists():
        raise ValueError("refusing to replace a retained evidence inventory")
    if not (RUNS / "completion.json").is_file():
        raise ValueError("the owned survey must finish before evidence collection")
    plan = json.loads((HERE / "cohort.json").read_text())
    sources = [("tb21", row, REPO, RUNS / "tb21" / row["task_id"], "Auditor")
               for row in plan["tb21"]["selected"]]
    sources += [("humanevalfix", row, REPO, RUNS / "humanevalfix" / row["task_id"], "Auditor")
                for row in plan["humanevalfix"]["frame"] if row["selected"]]
    selected_jobs = {(family, row["task_id"]): row for family, row, *_ in sources}
    attempts = owned_attempts or []
    if len(set(attempts)) != len(attempts):
        raise ValueError("an owned attempt may be retained only once")
    for attempt in attempts:
        if Path(attempt).name != attempt or attempt in {"", ".", ".."}:
            raise ValueError("attempt must be a single nonempty path component")
        attempt_root = RUNS / "attempts" / attempt
        snapshot = json.loads((attempt_root / "attempt.json").read_text())
        expected = {(row["benchmark"], row["task_id"]) for row in snapshot["selected"]}
        # An interrupted attempt keeps its per-task completed.json; only those reports are used.
        interrupted = not (attempt_root / "completion.json").is_file()
        results = (json.loads((attempt_root / "completed.json").read_text()) if interrupted
                   else json.loads((attempt_root / "completion.json").read_text())["results"])
        actual = [(row["benchmark"], row["task_id"]) for row in results]
        if (
            snapshot["cohort_sha256"] != digest(HERE / "cohort.json")
            or snapshot["limits"] != plan["limits"]
            or len(set(actual)) != len(actual)
            or not (set(actual) <= expected if interrupted else set(actual) == expected)
            or not expected <= selected_jobs.keys()
        ):
            raise ValueError("a completed attempt differs from the unchanged frozen plan")
        sources.extend((family, selected_jobs[family, name], REPO,
                        attempt_root / family / name, "Auditor") for family, name in actual)
    missing: list[str] = []
    if include_prior:
        root = Path(plan["tb21"]["prior_worktree"])
        prior_runs = root / "runs/px1-tb21-weak-tests-v1-20261019"
        if not (prior_runs / "campaign-results.json").is_file():
            raise ValueError("the Traces driver is not finished; do not inspect or take it over")
        missing = [name for name in plan["tb21"]["prior_live_campaign"]
                   if not (prior_runs / "campaign" / name / "report.json").is_file()]
        sources += [("tb21", {"task_id": name}, root, prior_runs / "campaign" / name,
                     "trials run by Traces campaign traces-tb21-weak-tests-v1-20261019, adjudicated by Auditor")
                    for name in plan["tb21"]["prior_live_campaign"] if name not in missing]
    retained: dict[tuple[str, str], dict[str, Any]] = {}
    for benchmark, selected, root, report_dir, producer in sources:
        name = selected["task_id"]
        report = json.loads((report_dir / "report.json").read_text())
        previous = retained.get((benchmark, name))
        if previous is not None and previous["candidates"]:
            raise ValueError("a candidate-bearing attempt cannot be superseded without review")
        files: dict[str, dict[str, str]] = previous["files"] if previous is not None else {}
        history = [] if previous is None else [
            *previous["prior_attempts"], {"source_report": previous["source_report"]},
        ]
        destination = EVIDENCE / benchmark / name / "files"

        def retain(
            path: Path, root: Path = root, destination: Path = destination,
            files: dict[str, dict[str, str]] = files,
        ) -> None:
            if not path.is_file():
                return
            source = path.resolve()
            if not source.is_relative_to(root) or path.is_symlink():
                raise ValueError(f"evidence escapes its source root: {path}")
            relative = source.relative_to(root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files[str(relative)] = {"path": str(target.relative_to(REPO)), "sha256": digest(target)}

        retain(report_dir / "report.json")
        attempt_root = report_dir.parent.parent
        retain(attempt_root / "attempt.json")
        retain(attempt_root / "completion.json")
        retain(attempt_root / "completed.json")
        retain(report_dir / "grades.log")
        for path in sorted(report_dir.glob("*.patch")) + sorted(report_dir.glob("*.sh")):
            retain(path)
        for path in sorted((report_dir / "mutants").glob("*")):
            retain(path)
        for grade in report.get("grades", []):
            native = grade.get("native") or {}
            for key in ("script", "matrix", "receipt", "invocations"):
                if native.get(key):
                    retain(root / native[key])
            if native.get("job_dir"):
                job = root / native["job_dir"]
                retain(job / "lab-metadata.json")
                retain(job / "result.json")
                for path in sorted((root / "runs/.executor").glob(native["name"] + ".*")):
                    retain(path)
            for trial_name in native.get("trials", []):
                trial = root / trial_name
                for path in sorted(trial.glob("*.json")):
                    retain(path)
                for path in sorted((trial / "verifier").glob("*")):
                    retain(path)
                retain(trial / "agent/oracle.txt")
                retain(trial / "agent/exit-code.txt")
        package = Path(report.get("package") or root / selected.get("package", ""))
        if package.is_dir() and package.resolve().is_relative_to(root):
            retain(package / "instruction.md")
            retain(package / "task.toml")
            retain(package / "solution/solve.sh")
            retain(package / "environment/Dockerfile")
            for path in sorted((package / "tests").rglob("*")):
                retain(path)
            for path in sorted((package / "environment/workspace").glob("Python__*.py")):
                retain(path)
        relative_report = str((report_dir / "report.json").relative_to(root))
        retained[benchmark, name] = {
            "benchmark": benchmark, "task_id": name, "producer": producer,
            "source_root": str(root), "source_report": relative_report,
            "source_package": str(package.relative_to(root)),
            "source_report_dir": str(report_dir.relative_to(root)),
            "files": files, "candidates": candidates(report), "prior_attempts": history,
        }
    inventory = list(retained.values())
    dump(HERE / "inventory.json", {
        "schema": "evallab.weak_tests_generalization.inventory/v1",
        "owned_attempts": ["first-pass", *attempts], "tasks": inventory,
        "prior_reports_absent_at_collection": missing,
    })
    queue = [
        {"benchmark": row["benchmark"], "task_id": row["task_id"], "candidate_id": candidate["id"]}
        for row in inventory for candidate in row["candidates"]
    ]
    dump(HERE / "review-queue.json", queue)
    print(json.dumps({"retained_tasks": len(inventory), "survivors_requiring_review": len(queue)}))


def checked(row: dict[str, Any], source_path: str) -> Path:
    entry = row["files"][source_path]
    path = (REPO / entry["path"]).resolve()
    if not path.is_relative_to(EVIDENCE) or not path.is_file() or digest(path) != entry["sha256"]:
        raise ValueError(f"missing or changed retained evidence: {source_path}")
    return path


def grade_proof(row: dict[str, Any], report: dict[str, Any], label: str, reward: float) -> dict[str, Any]:
    grade = next(grade for grade in report["grades"] if grade["label"] == label)
    native = grade["native"]
    if grade["status"] != "scored" or len(native.get("trials", [])) != 1:
        raise ValueError(f"native proof is not a scored single trial: {row['task_id']} {label}")
    script = checked(row, native["script"])
    matrix = json.loads(checked(row, native["matrix"]).read_text())
    result = native["result"]
    receipt = json.loads(checked(row, native["receipt"]).read_text())
    recorded = next(entry for entry in receipt["results"] if entry["name"] == native["name"])
    trial_name, = native["trials"]
    trial = json.loads(checked(row, trial_name + "/result.json").read_text())
    isolation = json.loads(checked(row, trial_name + "/host-isolation.json").read_text())
    executor = json.loads(checked(row, native["job_dir"] + "/lab-metadata.json").read_text())
    oracle = checked(row, trial_name + "/agent/oracle.txt")
    config = trial["config"]
    trial_reward = trial["verifier_result"]["rewards"]["reward"]
    if (
        isinstance(grade["reward"], bool) or grade["reward"] != reward
        or grade["rc"] not in (None, 0) or recorded != result
        or native["package_digest"] != report["package_digest"]
        or matrix["task_package_digest"] != report["package_digest"]
        or digest(script) != native["script_sha256"]
        or result["status"] not in {"ok", "mismatch"} or result["rewards"] != [reward]
        or result["solution_sha256"] != digest(script).removeprefix("sha256:")
        or not result.get("staged_task_digest")
        or executor["task_staging"]["source_package_digest"] != result["staged_task_digest"]
        or executor["exit_code"] != 0 or executor["timed_out"]
        or trial.get("exception_info") is not None
        or isinstance(trial_reward, bool) or trial_reward != reward
        or not trial["agent_execution"].get("started_at") or not trial["agent_execution"].get("finished_at")
        or isolation["applied"] is not True or isolation.get("error_type") is not None
        or config["agent"]["name"] != "oracle" or config["agent"]["model_name"] is not None
        or config["agent"].get("override_timeout_sec") is not None
        or config["environment"]["import_path"] != "evallab.harbor_host_isolation:HostIsolatedDockerEnvironment"
        or config["timeout_multiplier"] != 1.0 or config["agent_timeout_multiplier"] != 1.0
        or config["verifier_timeout_multiplier"] not in (None, 1.0)
        or config["verifier"]["override_timeout_sec"] is not None
        or any(config["environment"][field] is not None for field in (
            "override_cpus", "override_memory_mb", "override_storage_mb", "override_gpus"
        ))
    ):
        raise ValueError(f"native proof does not support its label: {row['task_id']} {label}")
    verifier_files = [name for name in row["files"] if name.startswith(trial_name + "/verifier/")]
    if not any("stdout" in name for name in verifier_files):
        raise ValueError("a scored label must retain the verifier's test output")
    for name in verifier_files:
        checked(row, name)
    return {"label": label, "reward": reward, "script": str(script.relative_to(REPO)),
            "oracle_output": str(oracle.relative_to(REPO)), "trial": trial_name,
            "verifier_files": [row["files"][name]["path"] for name in verifier_files]}


def wilson(count: int, n: int) -> dict[str, Any]:
    if not 0 <= count <= n:
        raise ValueError("invalid binomial count")
    if n == 0:
        return {"count": count, "n": n, "rate": None, "wilson_95_ci": None}
    z = 1.959963984540054
    p = count / n
    scale = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / scale
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / scale
    return {"count": count, "n": n, "rate": p,
            "wilson_95_ci": [max(0.0, centre - margin), min(1.0, centre + margin)]}


def _execution_counts(report: dict[str, Any]) -> dict[str, int]:
    grades = report.get("grades", [])
    return {
        "grade_records": len(grades),
        "scored_grades": sum(grade["status"] == "scored" for grade in grades),
        "native_trials": len({
            trial for grade in grades for trial in (grade.get("native") or {}).get("trials", [])
        }),
    }


def build() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    plan = json.loads((HERE / "cohort.json").read_text())
    bundle = json.loads((HERE / "inventory.json").read_text())
    inventory = bundle["tasks"]
    reviews = jsonl(HERE / "reviews.jsonl")
    by_review = {(r["benchmark"], r["task_id"], r["candidate_id"]): r for r in reviews}
    expected = {(r["benchmark"], r["task_id"], c["id"]) for r in inventory for c in r["candidates"]}
    if len(by_review) != len(reviews) or by_review.keys() != expected:
        raise ValueError("every survivor needs exactly one explicit instruction-level review")
    observed = {(r["benchmark"], r["task_id"]): r for r in inventory}
    frames = {"tb21": plan["tb21"]["frame"], "humanevalfix": plan["humanevalfix"]["frame"]}
    atlas, items, findings = [], [], []
    for benchmark, frame in frames.items():
        for original in frame:
            name = original["task_id"]
            row = observed.get((benchmark, name))
            entry: dict[str, Any] = {"benchmark": benchmark, "task_id": name,
                "package_digest": original["package_digest"], "status": "not_run",
                "baseline_qualified": False, "witnesses": [], "reviews": []}
            if row is not None:
                report = json.loads(checked(row, row["source_report"]).read_text())
                if report.get("package_digest") not in (None, original["package_digest"]):
                    raise ValueError("an observed package differs from its frozen sampling frame")
                entry.update(status=report["status"], reason=report.get("reason"),
                    producer=row["producer"], coverage=report.get("coverage"),
                    report=row["files"][row["source_report"]]["path"],
                    **_execution_counts(report),
                    reference_code_blocks=report.get("code_blocks"),
                    reference_code_lines=report.get("code_lines"),
                    mutants_generated=report.get("mutants", {}).get("generated"),
                    mutants_tried=report.get("mutants", {}).get("tried", 0))
                try:
                    controls = [grade_proof(row, report, label, reward)
                                for label, reward in (("oracle", 1.0), ("nop", 0.0), ("full", 1.0))]
                    entry.update(baseline_qualified=True, controls=controls)
                except (ValueError, KeyError, StopIteration) as exc:
                    entry["qualification_error"] = str(exc)
                entry["prior_attempts"] = []
                for attempt in row.get("prior_attempts", []):
                    old = json.loads(checked(row, attempt["source_report"]).read_text())
                    if old.get("package_digest") not in (None, original["package_digest"]):
                        raise ValueError("a retained prior attempt differs from the frozen task")
                    if candidates(old):
                        raise ValueError("a candidate-bearing prior attempt must not be hidden")
                    historical: dict[str, Any] = {
                        "report": row["files"][attempt["source_report"]]["path"],
                        "status": old["status"], "reason": old.get("reason"),
                        "baseline_qualified": False, **_execution_counts(old),
                    }
                    try:
                        historical["controls"] = [
                            grade_proof(row, old, label, reward)
                            for label, reward in (("oracle", 1.0), ("nop", 0.0), ("full", 1.0))
                        ]
                        historical["baseline_qualified"] = True
                    except (ValueError, KeyError, StopIteration) as exc:
                        historical["qualification_error"] = str(exc)
                    entry["prior_attempts"].append(historical)
                entry["ever_baseline_qualified"] = entry["baseline_qualified"] or any(
                    attempt["baseline_qualified"] for attempt in entry["prior_attempts"]
                )
                entry["execution_all_attempts"] = {
                    key: entry[key] + sum(attempt[key] for attempt in entry["prior_attempts"])
                    for key in ("grade_records", "scored_grades", "native_trials")
                }
                retained_refs = {value["path"]: source for source, value in row["files"].items()}
                for candidate in row["candidates"]:
                    review = by_review[benchmark, name, candidate["id"]]
                    if review["label"] not in {"required_gap", "equivalent_or_allowed", "outside_explicit_requirements", "unresolved"}:
                        raise ValueError("unknown semantic review label")
                    if not review.get("rationale") or not review.get("instruction_refs") or not review.get("consumer_refs"):
                        raise ValueError("every survivor review needs its requirement and verifier-consumer evidence")
                    for ref in review["instruction_refs"] + review["consumer_refs"]:
                        source = retained_refs.get(ref.partition("#")[0])
                        if source is None:
                            raise ValueError("a semantic review cites evidence outside its retained task")
                        checked(row, source)
                    entry["reviews"].append(review)
                    if review["label"] != "required_gap":
                        continue
                    if not entry["baseline_qualified"] or not review.get("root_cause"):
                        raise ValueError("a positive lacks valid controls or its precise missing requirement")
                    proof = grade_proof(row, report, candidate["grade"], 1.0)
                    script_source = str(Path(row["source_report_dir"]) / candidate["script"])
                    patch_source = str(Path(row["source_report_dir"]) / candidate["patch"])
                    script, patch = checked(row, script_source), checked(row, patch_source)
                    if digest(script) != digest(REPO / proof["script"]):
                        raise ValueError("the published defect reproducer differs from the graded script")
                    entry["witnesses"].append({"candidate_id": candidate["id"], "proof": proof,
                        "script": str(script.relative_to(REPO)), "patch": str(patch.relative_to(REPO)),
                        "review": review, "reference_replayed": False})
            weak = bool(entry["witnesses"])
            entry["semantic_verdict"] = "confirmed_weak" if weak else "not_established"
            subject = {"kind": "task", "task_id": benchmark + "/" + name,
                       "task_digest": original["package_digest"]}
            roots = sorted({w["review"]["root_cause"] for w in entry["witnesses"]})
            item = {"schema": "qa.answer_key/v1", "key_id": KEY_ID,
                    "item_id": KEY_ID + ":" + benchmark + ":" + name, "subject": subject,
                    "expected_verdict": "fail" if weak else "unknown",
                    "expected_category": "weak" if weak else None,
                    "expected_fault": "eval" if weak else None, "expected_root_causes": roots,
                    "label_source": "native source-visible reference-subset/line-mutation survey; instruction-adjudicated",
                    "split": "dev", "source_visible": row is not None,
                    "selected_by": ["ref-subset"] if weak else [],
                    "notes": "Frozen frame item; only established positives are output-selected. No independent detector evaluation. Unknown is not a clean negative."}
            AnswerKeyItem.model_validate(item)
            items.append(item)
            for witness in entry["witnesses"]:
                review = witness["review"]
                finding = {"schema": "qa.finding/v1", "id": KEY_ID + ":" + benchmark + ":" + name + ":" + witness["candidate_id"],
                    "subject": subject, "stage": "S2.ref_subset" if witness["candidate_id"] == "subset" else "S2.ref_mutants",
                    "verdict": "fail", "category": "weak", "severity": "medium", "fault": "eval",
                    "summary": review["rationale"], "root_cause": [review["root_cause"]],
                    "evidence_refs": [entry["report"], witness["patch"], *review["instruction_refs"], *review["consumer_refs"]],
                    "reproducer": {"kind": "command", "path": witness["script"], "expected_reward": 1.0},
                    "reviewer": {"agent": "Auditor", "model": None, "cost_usd": 0.0},
                    "measurement": "measured", "refutation": {"status": "verified",
                    "by": "instruction and verifier-consumer source adjudication of native fresh-base evidence; no independent S3 replay",
                    "reason": review["rationale"]}}
                Finding.model_validate(finding)
                findings.append(finding)
            atlas.append(entry)
    summaries = {}
    for benchmark, frame in frames.items():
        rows = [row for row in atlas if row["benchmark"] == benchmark]
        attempted = [row for row in rows if row["status"] != "not_run"]
        qualified = [row for row in attempted if row["ever_baseline_qualified"]]
        count = sum(bool(row["witnesses"]) for row in rows)
        by_producer = {}
        for producer in sorted({row["producer"] for row in attempted}):
            subset = [row for row in attempted if row["producer"] == producer]
            valid = [row for row in subset if row["ever_baseline_qualified"]]
            positives = sum(bool(row["witnesses"]) for row in subset)
            by_producer[producer] = {
                "attempted_n": len(subset), "baseline_qualified_n": len(valid),
                "confirmed_rate_attempted": wilson(positives, len(subset)),
                "confirmed_rate_baseline_qualified": wilson(positives, len(valid)),
            }
        summaries[benchmark] = {"frame_n": len(frame), "attempted_n": len(attempted),
            "baseline_qualified_n": len(qualified), "confirmed_weak_tasks": count,
            "confirmed_witnesses": sum(len(row["witnesses"]) for row in rows),
            **{key: sum(row.get("execution_all_attempts", {}).get(key, 0) for row in rows)
               for key in ("grade_records", "scored_grades", "native_trials")},
            "primary_attempt_baseline_qualified_n": sum(row["baseline_qualified"] for row in rows),
            "prior_attempts_retained": sum(len(row.get("prior_attempts", [])) for row in rows),
            "by_producer": by_producer,
            "primary_search": {
                "subset_coverage_counts": dict(Counter(
                    (row.get("coverage") or {}).get("subset", "unreported") for row in attempted)),
                "line_coverage_counts": dict(Counter(
                    (row.get("coverage") or {}).get("mutations", "unreported") for row in attempted)),
                "line_candidates_generated_recorded": sum(row.get("mutants_generated") or 0 for row in attempted),
                "line_generation_unreported_tasks": sum(row.get("mutants_generated") is None for row in attempted),
                "line_candidates_tried": sum(row["mutants_tried"] for row in attempted),
                "candidate_bearing_tasks": sum(bool(row["reviews"]) for row in attempted),
            },
            "confirmed_rate_attempted": wilson(count, len(attempted)),
            "confirmed_rate_baseline_qualified": wilson(count, len(qualified)),
            "observed_frame_lower_bound": count / len(frame),
            "unknown_tasks": len(frame) - count, "clean_negative_tasks": 0,
            "status_counts": dict(Counter(row["status"] for row in rows)),
            "semantic_review_counts": dict(Counter(r["label"] for row in rows for r in row["reviews"]))}
    comparison = REPO / "research/answer-keys/mimo-stratified-sample100/summary.json"
    mimo = json.loads(comparison.read_text())
    results = {"schema": "evallab.weak_tests_generalization.results/v1", "benchmarks": summaries,
        "paid_experiment_spend_usd": 0, "new_experimental_model_calls": 0,
        "method_reuse": {
            "engine": "src/evallab/ref_subset.py",
            "mutation_operators": "Existing evallab.verifier_mutation line operators; no new minimization method.",
            "native_transport_source_commits": [
                "6fc2e8389f9c7ea73f116adc0b4003b5e8e66ca6",
                "e116b577bc2c3916217e6cc06caefcba0c71c903",
                "60f04ba627a0c54be3fdaf252be302d8ab677fb4",
            ],
            "first_pass_native_source_archive": {
                "path": "research/evidence/runs/weak-tests-generalization-20261019-method-first-pass.tar.gz",
                "sha256": digest(REPO / "research/evidence/runs/weak-tests-generalization-20261019-method-first-pass.tar.gz"),
            },
            "transport_schema_fixes": [
                "Normalize explicitly declared legacy memory units using locked Harbor; never assume missing limits.",
                "Bind the validated source/staged snapshot before execution, not by rereading a temporary source after the trial.",
                "Repair native-transport type annotations and lint only; existing mutation and minimization operators unchanged.",
            ],
            "archival_mimo_comparison": {
                "source": str(comparison.relative_to(REPO)), "sha256": digest(comparison),
                "raw_candidate_rate": mimo["raw_s2b_candidate_rate"],
                "strict_confirmed_rate": mimo["confirmed_weak_strict_rate"],
                "resolved_uncertain_sensitivity": mimo["confirmed_weak_with_resolved_uncertain_rate"],
                "published_key_verdict_counts": mimo["expected_verdict_counts"],
                "new_executions": 0,
                "caveat": "Historical raw-package sample from 389 previously measured eligible tasks; neither fresh v6 grades nor an independent detector comparison. Its 23 supported key positives differ from its 24 later strict adjudications.",
                "rate_comparison": "Context only, not a matched cross-dataset prevalence comparison: eligibility, sampling and bounded-search censoring differ. No claim of matched search effort across datasets.",
            },
        },
        "survivors_reviewed": len(reviews), "survivors_unreviewed": 0,
        "independent_detector_evaluation": False, "independent_s3_replay": False,
        "owned_attempts": bundle.get("owned_attempts", ["first-pass"]),
        "primary_attempt_rule": "For each unchanged frozen task, use the last explicitly named completed owned attempt for candidate search. Prior attempts remain retained and included in execution counts. Candidate-bearing prior attempts cannot be superseded. Baseline-qualified denominators include valid controls from any retained attempt; positive witnesses require their own primary attempt's controls.",
        "interpretation": "Rates count method-established instruction violations under bounded search, not all true weaknesses. Wilson intervals are nominal binomial intervals for these observed rates, not corrections for purposive TB2.1 selection, stratified sampling or search censoring. Tiny reference edits may have no proper code subset or applicable line operator; absence of applicable probes is missing measurement, not evidence of a good grader. Search counts describe primary attempts; execution totals also include retained prior attempts. Unmeasured/nonpositive tasks remain unknown, never clean.",
        "selection": {"tb21": plan["tb21"]["own_sampling"], "humanevalfix": plan["humanevalfix"]["sampling"]},
        "resource_limit": "One owned lane; shared two-slot Docker gate with the Traces campaign. Native task limits unchanged. Capacity-minus-reserve fallback with unbounded services is disclosed in raw matrix receipts.",
        "source_provenance": sorted({row["producer"] for row in inventory})}
    return results, atlas, items, findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "build"))
    parser.add_argument("--include-prior", action="store_true")
    parser.add_argument("--owned-attempt", action="append", default=[])
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.include_prior, args.owned_attempt)
        return
    results, atlas, items, findings = build()
    dump(HERE / "results.json", results)
    (HERE / "atlas.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in atlas))
    if findings:
        KEY.mkdir(exist_ok=True)
        (KEY / "items.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in items))
        (KEY / "findings.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in findings))
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
