"""Publish numerical study results from retained analyses, never raw model text.

--evidence points to the private retention root containing raw/ and analyses/.
Run analyze_native.py/analyze_policy.py first; this command never calls models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native(evidence: Path, name: str) -> dict[str, Any]:
    report = load(evidence / "analyses" / f"{name}.json")
    result = {
        key: report[key]
        for key in (
            "source_summary_sha256",
            "analyzer_sha256",
            "kind",
            "original_cases",
            "arms",
            "candidate_reduction_percent",
            "tasks",
            "quality_limit",
        )
    }
    if report["kind"] == "navigation":
        scores = load(evidence / "raw" / name / "scores.json")
        facts: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
        for task in scores["tasks"]:
            for repeat in task["repeats"]:
                for attempt in repeat["attempts"]:
                    fields = attempt["score"]["fields"]
                    facts[attempt["arm"]]["correct"] += sum(
                        row["exact"] is True for row in fields.values()
                    )
                    facts[attempt["arm"]]["total"] += len(fields)
        result["exact_fact_items"] = dict(facts)
    return result


def policy(evidence: Path) -> dict[str, Any]:
    path = evidence / "analyses/policy/policy-analysis.json"
    report = load(path)
    runs = []
    for run in report["runs"]:
        totals = run["totals"]
        groups = [
            {
                key: group[key]
                for key in (
                    "model",
                    "compiler_arm",
                    "attempted_sdk_generations",
                    "strict",
                    "reported_usage",
                    "observed_efficiency",
                )
            }
            for group in run["by_model_compiler_arm"]
        ]
        runs.append(
            {
                "protocol": run["protocol"],
                "citation_presentation": run["citation_presentation"],
                "response_carrier": run["response_carrier"],
                "source_hashes": run["source_hashes"],
                "strict_scorer_function_sha256": run["strict_scorer_function_sha256"],
                "planned_sdk_generations": totals["planned_sdk_generations"],
                "attempted_sdk_generations": totals["attempted_sdk_generations"],
                "strict": totals["strict"],
                "item_status_counts": totals["item_status_counts"],
                "status_counts": totals["status_counts"],
                "reported_usage": totals["reported_usage"],
                "by_model_compiler_arm": groups,
                "diagnostics_not_acceptance": {
                    key: run["diagnostics_only"][key]
                    for key in ("choice_only", "format_cause_generations")
                },
            }
        )
    return {
        "analysis_artifact_sha256": digest(path),
        "analyzer_source_sha256": report["analyzer_source_sha256"],
        "runs": runs,
        "reconciliation": report["reconciliation"],
        "strict_acceptance_regraded": report["strict_acceptance_regraded"],
        "limits": report["limits"],
    }


def support(raw: Path) -> dict[str, Any]:
    devloop = load(raw / "devloop-real.json")
    judge = load(raw / "judge-binding-footprints-01/results.json")
    judge_counts: dict[str, dict[str, dict[str, int]]] = {}
    for row in judge["rows"]:
        counts = judge_counts.setdefault(row["arm"], {}).setdefault(row["kind"], {})
        for encoding in row["encodings"]:
            counts[encoding["name"]] = counts.get(encoding["name"], 0) + encoding["tokens"]
    ownership = load(raw / "effort-trace-audit/verification-owner/metrics.json")
    scope = load(raw / "code-cases/native-effort-01/source-patch-diagnostics-v1/result.json")
    before = load(raw / "trajectory-test-isolation/before-v2-run/result.json")
    after = load(raw / "trajectory-test-isolation/after-v2-run/result.json")
    style_path = raw / "verification-owner-style-audit-v1.json"
    finishing_path = raw / "verification-owner-parent-finish-v1/result.json"
    style = load(style_path)
    finishing = load(finishing_path)
    finishing_arms = {}
    for arm in ("baseline", "candidate"):
        episodes = [row for row in style["episodes"] if row["arm"] == arm]
        controls = [row for row in finishing["episodes"] if row["arm"] == arm]
        finishing_arms[arm] = {
            "episodes": len(episodes),
            "lint_passed": sum(row["lint_exit"] == 0 for row in episodes),
            "formatting_needed": sum(row["format_check_exit"] == 1 for row in episodes),
            "mechanical_controls": len(controls),
            "ast_preserved": sum(row["ast_equal"] is True for row in controls),
            "finishing_passed": sum(
                row["formatter_exit"] == row["after_lint_exit"] == row["after_format_exit"] == 0
                and row["behavior_passed"] is True
                for row in controls
            ),
            "feature_checks": sum(row["behavior_checks"] for row in controls),
        }
    return {
        "devloop": {
            "identical_plan": devloop["identical_plan"],
            "arms": {
                arm: {key: devloop[arm][key] for key in ("read_text_calls", "ast_parse_calls")}
                for arm in ("baseline", "candidate")
            },
            "artifact_sha256": digest(raw / "devloop-real.json"),
            "token_savings_measured": False,
        },
        "judge_handles": {
            key: judge[key]
            for key in (
                "window_count",
                "source_count",
                "projection_policy",
                "model_calls",
                "canonical_records_identical",
                "quality_scope",
                "token_scope",
            )
        }
        | {
            "offline_token_counts": judge_counts,
            "artifact_sha256": digest(raw / "judge-binding-footprints-01/results.json"),
        },
        "ownership_compliance": {
            key: value
            for key, value in ownership["compliance"].items()
            if isinstance(value, (int, bool))
        },
        "ownership_audit_sha256": digest(
            raw / "effort-trace-audit/verification-owner/metrics.json"
        ),
        "ownership_parent_finishing": {
            "arms": finishing_arms,
            "style_artifact_sha256": digest(style_path),
            "finishing_artifact_sha256": digest(finishing_path),
            "original_trials_modified": finishing["original_trials_modified"],
            "models_or_paid_calls": finishing["models_or_paid_calls"],
            "limits": finishing["limits"],
        },
        "source_patch_diagnostic_not_episode_regrade": {
            key: scope[key]
            for key in (
                "measurement_kind",
                "original_episode_acceptance_unchanged",
                "diagnostics",
                "models_or_paid_calls",
                "limits",
            )
        },
        "test_storage_isolation": {
            "before_pytest_exit": before["returncode"],
            "after_pytest_exit": after["returncode"],
            "before_changed_derived_paths": before["changed_derived_paths"],
            "after_changed_derived_paths": after["changed_derived_paths"],
            "models_or_paid_calls": 0,
        },
        "frozen_context_replay": load(raw / "context-corpus-v1/replay-verification-v1.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    if args.output.exists():
        parser.error("Use a fresh --output; never overwrite a prior result")
    census = load(evidence / "raw/generation-census-v1.json")
    report = {
        "schema": "agent-efficiency-public-results/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "baseline_revision": "5171c20c7d1f5c00d020db6087eac3041f94bf13",
        "publisher_sha256": digest(Path(__file__)),
        "generation_census": {
            key: census[key]
            for key in (
                "attempted_policy_sdk_generations",
                "scored_main_policy_sdk_generations",
                "technical_and_capability_policy_sdk_generations",
                "native_cli_episodes",
                "physical_provider_requests",
                "limits",
            )
        },
        "native": {
            name: native(evidence, name)
            for name in (
                "native-01",
                "native-03",
                "native-effort-01",
                "native-owner-01",
                "trace-main-run",
                "trace-fused-holdout-run",
            )
        },
        "policy": policy(evidence),
        "support": support(evidence / "raw"),
        "private_retention_manifest_sha256": digest(evidence / "retention.json"),
        "limits": [
            "Reported total input includes cached input; uncached input is their compatible difference, not an invoice or quota meter.",
            "SDK generations and native CLI episodes are distinct units; physical upstream attempts and retries remain unknown.",
            "Coding cohorts reuse three original problems; repeats and feature checks are correlated, not independent capability trials.",
            "Navigation cohorts have disjoint native heads but overlap one underlying benchmark task: twelve sources cover nine tasks.",
            "Policy protocols are exploratory fixed-order observations; cache state, timing and framing confound protocol contrasts.",
            "All original failures remain failures. Source-patch diagnostics are separate post-hoc controls, not rehabilitated episodes.",
            "No broad quality non-inferiority, default-effort reduction, cross-family coding-parity or monetary-savings claim is established.",
        ],
    }
    serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    forbidden = (
        "/Users/",
        "/private/tmp/",
        '"final_text"',
        '"command":',
        '"reasoning":',
        '"prompt":',
    )
    if any(marker in serialized for marker in forbidden):
        raise ValueError("Private text/path unexpectedly entered numerical publication")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(serialized)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "bytes": len(serialized.encode()),
                "native_studies": len(report["native"]),
                "policy_protocols": len(report["policy"]["runs"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
