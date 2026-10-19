#!/usr/bin/env python3
"""Build the source-visible TB2.1 weak-test catalog; never infer clean labels.

The frozen cohort is the denominator. Atlas rows carry actual search coverage
and instruction adjudications. Only fully bound, instruction-violating native
witnesses become positive findings; every other task remains explicitly unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.qa.models import AnswerKeyItem, Finding

HERE = Path(__file__).resolve().parent
KEY_ID = "tb21-weak-tests-v1"
PIN = "7131e4375048a0e408a8fb404b5f499d726b695b"
COHORT_SIZE = 89


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _checked_file(path: str, digest: str) -> Path:
    file = (HERE / path).resolve()
    if not file.is_relative_to(HERE) or not file.is_file():
        raise ValueError(f"missing or escaping evidence file: {path}")
    actual = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    if actual != digest:
        raise ValueError(f"evidence digest mismatch: {path}")
    return file


def _control(control: dict[str, Any], reward: float, package: str) -> None:
    observed = control.get("reward")
    if (
        control.get("status") != "scored"
        or isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
        or observed != reward
        or control.get("package_digest") != package
        or control.get("host_isolation_applied") is not True
        or control.get("agent_success") is not True
        or control.get("task_timeout_preserved") is not True
    ):
        raise ValueError(f"invalid native control: {control.get('label')}")
    evidence = json.loads(_checked_file(control["evidence_path"], control["evidence_sha256"]).read_text())
    _checked_file(control["script_path"], control["script_sha256"])
    if not control.get("staged_task_digest"):
        raise ValueError("native control lacks its staged task binding")
    matrix, result = evidence["matrix"], evidence["matrix_result"]
    trial, executor = evidence["trial_result"], evidence["executor"]
    config = trial["config"]
    oracle = evidence["oracle_execution"]
    _checked_file(oracle["log_path"], oracle["log_sha256"])
    if (
        matrix["task_package_digest"] != package
        or result["status"] not in {"ok", "mismatch"}
        or result["rewards"] != [reward]
        or result["staged_task_digest"] != control["staged_task_digest"]
        or result["solution_sha256"] != control["script_sha256"].removeprefix("sha256:")
        or trial.get("exception_info") is not None
        or trial["verifier_result"]["rewards"]["reward"] != reward
        or executor["exit_code"] != 0
        or executor["timed_out"]
        or executor["task_staging"]["source_package_digest"] != result["staged_task_digest"]
        or evidence["host_isolation"]["applied"] is not True
        or evidence["host_isolation"].get("error_type") is not None
        or config["agent"]["name"] != "oracle"
        or config["agent"]["model_name"] is not None
        or config["environment"]["import_path"]
        != "evallab.harbor_host_isolation:HostIsolatedDockerEnvironment"
        or config["timeout_multiplier"] != 1.0
        or config["agent_timeout_multiplier"] != 1.0
        or config["verifier_timeout_multiplier"] not in (None, 1.0)
        or config["verifier"]["override_timeout_sec"] is not None
        or any(
            config["environment"][field] is not None
            for field in ("override_cpus", "override_memory_mb", "override_storage_mb", "override_gpus")
        )
        or oracle["exit_code_evidence"] not in (None, 0)
        or not trial["agent_execution"].get("started_at")
        or not trial["agent_execution"].get("finished_at")
    ):
        raise ValueError(f"native evidence contradicts its published control: {control.get('label')}")


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cohort = json.loads((HERE / "cohort.json").read_text())
    if cohort["source"]["commit"] != PIN or cohort["full_cohort_tasks"] != COHORT_SIZE:
        raise ValueError("the frozen source or full-cohort denominator changed")
    source = {row["task_id"]: row for row in cohort["tasks"]}
    rows = _jsonl(HERE / "atlas.jsonl")
    by_task = {row["task_id"]: row for row in rows}
    if len(rows) != COHORT_SIZE or len(by_task) != COHORT_SIZE or by_task.keys() != source.keys():
        raise ValueError("atlas must account for every frozen task exactly once")
    splits = json.loads((HERE / "selection-splits.json").read_text())
    if splits.keys() != source.keys():
        raise ValueError("split inventory does not match the frozen cohort")
    items: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    witnesses = 0
    for name in sorted(source):
        row, original = by_task[name], source[name]
        package = original["package_digest"]
        if row["package_digest"] != package:
            raise ValueError(f"source package mismatch: {name}")
        if row["source_pin"] != PIN:
            raise ValueError(f"source pin mismatch: {name}")
        status = row["search_status"]
        counts[status] += 1
        confirmed = []
        for witness in row.get("witnesses", []):
            review = witness["review"]
            if review["label"] != "required_gap":
                continue
            if not review.get("instruction_refs") or not review.get("rationale"):
                raise ValueError(f"missing instruction adjudication: {name}")
            if not review.get("consumer_refs") or not review.get("root_cause"):
                raise ValueError(f"missing consumer or root-cause adjudication: {name}")
            if witness.get("reference_replayed") is not False:
                raise ValueError(f"not a base-applicable witness: {name}")
            controls = witness["controls"]
            for key, reward in (("original_reference", 1.0), ("nop", 0.0), ("full_targets", 1.0)):
                _control(controls[key], reward, package)
            _control(controls["witness"], 1.0, package)
            _checked_file(witness["patch_path"], witness["patch_sha256"])
            _checked_file(witness["script_path"], witness["script_sha256"])
            if witness["script_sha256"] != controls["witness"]["script_sha256"]:
                raise ValueError(f"witness differs from the graded native script: {name}")
            confirmed.append(witness)
        weak = bool(confirmed)
        if row["semantic_verdict"] != ("weak" if weak else "not_established"):
            raise ValueError(f"semantic verdict contradicts adjudication: {name}")
        witnesses += len(confirmed)
        subject = {"kind": "task", "task_id": original["task_name"], "task_digest": package}
        item = {
            "schema": "qa.answer_key/v1",
            "key_id": KEY_ID,
            "item_id": f"{KEY_ID}:task:{name}",
            "subject": subject,
            "expected_verdict": "fail" if weak else "unknown",
            "expected_category": "weak" if weak else None,
            "expected_fault": "eval" if weak else None,
            "expected_root_causes": sorted({w["review"]["root_cause"] for w in confirmed}),
            "label_source": f"{KEY_ID}:atlas.jsonl:{name}; original TB2.1 package @{PIN}",
            "split": splits[name]["split"],
            "source_set": KEY_ID,
            "source_pin": PIN,
            "search_status": status,
            "witness_ids": [w["id"] for w in confirmed],
            "prior_development_exposure": splits[name]["prior_development_exposure"],
            "source_visible": True,
            "notes": (
                "Measured, instruction-adjudicated weakness; method-selected, not an independent detector evaluation."
                if weak else
                "No confirmed weakness established by this bounded search; unknown is not a clean negative."
            ),
        }
        AnswerKeyItem.model_validate(item)
        items.append(item)
        for witness in confirmed:
            finding = {
                "schema": "qa.finding/v1",
                "id": witness["id"],
                "subject": subject,
                "stage": "S2.ref_subset",
                "verdict": "fail",
                "category": "weak",
                "severity": "high",
                "fault": "eval",
                "summary": witness["review"]["rationale"],
                "root_cause": [witness["review"]["root_cause"]],
                "evidence_refs": [
                    f"research/answer-keys/{KEY_ID}/atlas.jsonl#{name}",
                    f"research/answer-keys/{KEY_ID}/{witness['patch_path']}",
                    *witness["review"]["instruction_refs"],
                    *witness["review"]["consumer_refs"],
                ],
                "reproducer": {
                    "kind": "command",
                    "path": f"research/answer-keys/{KEY_ID}/{witness['script_path']}",
                    "expected_reward": 1.0,
                },
                "reviewer": {"agent": "Traces", "model": None, "cost_usd": 0.0},
                "measurement": "measured",
                "refutation": {
                    "status": "verified",
                    "by": "instruction-and-verifier-consumer adjudication",
                    "reason": witness["review"]["rationale"],
                },
            }
            Finding.model_validate(finding)
            findings.append(finding)
    confirmed_tasks = sum(item["expected_verdict"] == "fail" for item in items)
    summary = {
        "schema": "evallab.tb21_weak_tests.summary/v1",
        "key_id": KEY_ID,
        "source_pin": PIN,
        "cohort_tasks": COHORT_SIZE,
        "confirmed_weak_tasks": confirmed_tasks,
        "confirmed_witnesses": witnesses,
        "unknown_tasks": COHORT_SIZE - confirmed_tasks,
        "clean_negative_tasks": 0,
        "observed_full_cohort_weak_rate": confirmed_tasks / COHORT_SIZE,
        "rate_interpretation": "Observed lower bound under the recorded eligibility and search budget; not population prevalence.",
        "search_status_counts": dict(sorted(counts.items())),
        "new_paid_experimental_calls": 0,
        "codex_cli_calls": 0,
        "independent_detector_evaluation": False,
    }
    return items, findings, summary


def _render(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    items, findings, summary = build()
    outputs = {
        HERE / "items.jsonl": _render(items),
        HERE / "findings.jsonl": _render(findings),
        HERE / "summary.json": (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode(),
    }
    drift = []
    for path, expected in outputs.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                drift.append(path.name)
        else:
            path.write_bytes(expected)
    if drift:
        raise SystemExit("stale catalog outputs: " + ", ".join(drift))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
