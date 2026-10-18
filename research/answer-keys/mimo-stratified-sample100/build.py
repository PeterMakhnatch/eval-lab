"""Reconcile the frozen sample100 handoff on its actual raw-probe package bytes.

    uv run python research/answer-keys/mimo-stratified-sample100/build.py [--check]

This is source-derived evidence publishing, not a task execution or a v7 release.
The different-digest historical v6 replays remain diagnostics in audit.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import NormalDist

from evallab.qa.models import AnswerKeyItem, Finding

KEY_ID = "mimo-stratified-sample100"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
HANDOFF_SHA256 = "sha256:e0146b9068b7ef5146976efbdca1647034850e631ba4cfcdcf7eb570c1ba40ce"
ROOT_CAUSES = {"block_subset": "untested_reference_block", "line_mutant": "untested_reference_line"}
Z_95 = NormalDist().inv_cdf(0.975)


def render(rows: list[dict]) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def rate(count: int, total: int) -> dict:
    """Wilson binomial interval: descriptive, not a selection-bias correction."""
    proportion = count / total
    z2 = Z_95 * Z_95
    denominator = 1 + z2 / total
    center = (proportion + z2 / (2 * total)) / denominator
    radius = Z_95 * math.sqrt(proportion * (1 - proportion) / total + z2 / (4 * total**2))
    radius /= denominator
    return {
        "count": count,
        "n": total,
        "rate": round(proportion, 4),
        "wilson_95_ci": [round(max(0.0, center - radius), 4), round(min(1.0, center + radius), 4)],
    }


def strict_positive(row: dict) -> bool:
    return row["qa_task_verdict"] == "confirmed_weak"


def atlas_positive(row: dict) -> bool:
    return row["traces_atlas_verdict"] == "confirmed_weak"


def summarize(audit: dict) -> dict:
    tasks = audit["tasks"]
    total = len(tasks)
    reviewed = [row for row in tasks if row["traces_atlas_verdict"] != "unreviewed_in_atlas"]
    positives = [row for row in tasks if atlas_positive(row)]
    strict = sum(strict_positive(row) for row in tasks)
    raw = sum(row["raw_s2b_candidate"] for row in tasks)
    sensitivity = sum(
        row["qa_task_verdict"] in ("confirmed_weak", "confirmed_weak_from_uncertain")
        for row in tasks
    )
    replays = audit["historical_v6_s3_diagnostics"]["replays"]
    agreement = sum(atlas_positive(row) == strict_positive(row) for row in reviewed)
    both_positive = sum(atlas_positive(row) and strict_positive(row) for row in reviewed)
    both_nonpositive = sum(not atlas_positive(row) and not strict_positive(row) for row in reviewed)
    strict_ci = rate(strict, total)["wilson_95_ci"]
    replay_environments = Counter(row["environment"] for row in replays)
    by_language = {}
    for language in sorted({row["language"] for row in tasks}):
        rows = [row for row in tasks if row["language"] == language]
        by_language[language] = {
            "n": len(rows),
            "raw_s2b_candidate_rate": rate(sum(row["raw_s2b_candidate"] for row in rows), len(rows)),
            "confirmed_weak_strict_rate": rate(sum(strict_positive(row) for row in rows), len(rows)),
            "confirmed_weak_with_resolved_uncertain_rate": rate(
                sum(
                    row["qa_task_verdict"] in ("confirmed_weak", "confirmed_weak_from_uncertain")
                    for row in rows
                ),
                len(rows),
            ),
        }
    notes = (
        "Retrospective source-derived supported-positive reconciliation, not independent detector "
        f"recall: {sum(strict_positive(row) for row in positives)}/{len(positives)} supported positives; "
        f"{total - len(positives)} unknown key labels, no clean negatives, FPR n/a. "
        f"Strict weak prevalence {strict}/{total} (Wilson95 [{strict_ci[0]},{strict_ci[1]}]); "
        f"raw {raw}/{total}; sensitivity {sensitivity}/{total}; frozen atlas strict binary agreement "
        f"{agreement}/{len(reviewed)} ({both_positive} positive,{both_nonpositive} nonpositive). "
        f"Actual source probes bind {total} raw packages, not their v6 mappings; "
        f"{len(replays)} historical exact-v6 S3 diagnostics "
        f"({replay_environments['modal']} Modal,{replay_environments['docker']} Docker) only. "
        "Sample eligible389 previously measured of frame534; "
        "same-source selection/adjudication bias. 001244 remains atlas-unknown despite later promotion. "
        "Reviewer cost zero means no new publishing charge; historical $1.80 reservations are not "
        "actual full total cost. Preserved test splits do not mean unseen; v7 scoring gate false."
    )
    return {
        "key_id": KEY_ID,
        "source_handoff_sha256": audit["source_handoff"]["sha256"],
        "n_items": total,
        "n_measured_decisions": total,
        "expected_verdict_counts": {"fail": len(positives), "unknown": total - len(positives), "pass": 0},
        "finding_verdict_counts": {"fail": strict, "pass": total - strict},
        "supported_positive_reconciliation": {
            "positive_key_items": len(positives),
            "strict_flags_on_positive_key_items": sum(strict_positive(row) for row in positives),
            "strict_flags_on_unknown_key_items": sum(
                strict_positive(row) and not atlas_positive(row) for row in tasks
            ),
            "false_alarm_rate": None,
            "meaning": "Supported-positive rediscovery only; no independent recall or clean-task FPR.",
        },
        "raw_s2b_candidate_rate": rate(raw, total),
        "confirmed_weak_strict_rate": rate(strict, total),
        "confirmed_weak_with_resolved_uncertain_rate": rate(sensitivity, total),
        "by_language": by_language,
        "qa_task_verdict_counts": dict(sorted(Counter(row["qa_task_verdict"] for row in tasks).items())),
        "original_atlas_verdict_counts": dict(
            sorted(Counter(row["traces_atlas_verdict"] for row in tasks).items())
        ),
        "atlas_strict_binary_agreement": {
            "reviewed_overlap": len(reviewed),
            "both_positive": sum(atlas_positive(row) and strict_positive(row) for row in reviewed),
            "both_nonpositive": sum(
                not atlas_positive(row) and not strict_positive(row) for row in reviewed
            ),
            "agreement": agreement,
            "rate": round(agreement / len(reviewed), 4),
            "meaning": "Frozen original-handoff reconciliation; nonpositive does not mean clean.",
        },
        "split_counts": dict(sorted(Counter(row["split"] for row in tasks).items())),
        "subject_digest_scope": {"raw_primary_probe_packages": total, "v6_packages": 0},
        "historical_v6_s3_diagnostics": {
            "count": len(replays),
            "by_environment": dict(sorted(Counter(row["environment"] for row in replays).items())),
            "reward_one": sum(row["status"] == "executed" and row["reward"] == 1 for row in replays),
            "bound_trial_archive_members": sum(len(row["bound_archive_members"]) for row in replays),
            "not_additional_score_findings": True,
        },
        "cost_accounting": audit["cost_accounting"],
        "release_gate": audit["release_gate"],
        "source_issues": audit["source_issues"],
        "suggested_scoreboard": {
            "agent": "QA Task",
            "system": "qa-task/mimo-sample100-strict",
            "split": "all",
            "status": "source-derived supported-positive reconciliation; not independent detection",
            "notes": notes,
        },
    }


def build() -> dict[str, str]:
    audit = json.loads((HERE / "audit.json").read_text(encoding="utf-8"))
    if audit["source_handoff"]["sha256"] != HANDOFF_SHA256:
        raise ValueError("the original sample handoff must not be silently replaced")
    tasks = audit["tasks"]
    names = [row["task_name"] for row in tasks]
    if len(tasks) != 100 or len(set(names)) != 100:
        raise ValueError("the original sample must contain 100 distinct task names")
    if set(names) & set(audit["heldout33_exclusion"]["task_names"]):
        raise ValueError("the sample overlaps the original heldout33")
    if audit["release_gate"]["scoring_allowed"] or audit["release_gate"]["final_manifest_scientific_pass"]:
        raise ValueError("this evidence bundle cannot promote the v7 scientific release gate")

    items = []
    findings = []
    for index, row in enumerate(tasks):
        name = row["task_id"]
        binding = row["s2b_report_binding"]
        if binding["task_id"] != name or binding["package_digest"] != row["raw_package_digest"]:
            raise ValueError(f"{name}: source report does not bind the raw baseline")
        if row["task_name"] != f"mimo-v2.6-rl/{name}":
            raise ValueError(f"{name}: task name differs from the original source subject")
        controls = {control["label"]: control for control in binding["controls"]}
        for label, reward in (("full", 1), ("nop", 0)):
            control = controls[label]
            if (control["status"], control["rc"], control["reward"]) != ("scored", 0, reward):
                raise ValueError(f"{name}: no usable measured {label} control")
        if row["raw_s2b_candidate"] != (
            row["s2b_kept_fraction"] < 1 or row["s2b_mutant_survivors"] > 0
        ):
            raise ValueError(f"{name}: raw candidate flag disagrees with recorded probes")
        if row["split"] not in ("dev", "test"):
            raise ValueError(f"{name}: source split assignment was not preserved")

        subject = {"kind": "task", "task_id": row["task_name"], "task_digest": binding["package_digest"]}
        witness = row["witness"]
        cause = []
        if strict_positive(row):
            if not witness or witness["package_digest"] != subject["task_digest"]:
                raise ValueError(f"{name}: strict positive lacks a same-digest witness")
            relative = Path(witness["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"{name}: witness path must be repository-relative")
            if hashlib.sha256((REPO / relative).read_bytes()).hexdigest() != witness["sha256"]:
                raise ValueError(f"{name}: retained witness hash mismatch")
            if witness["review_label"] != "required_gap":
                raise ValueError(f"{name}: witness lacks supported semantic adjudication")
            for label, reward in (("full", 1), ("nop", 0), ("witness", 1)):
                control = witness["controls"][label]
                if (control["status"], control["rc"], control["reward"]) != ("scored", 0, reward):
                    raise ValueError(f"{name}: unusable recorded witness {label} control")
            cause = [ROOT_CAUSES[witness["kind"]]]
        elif witness is not None:
            raise ValueError(f"{name}: excluded/nonflag task must not publish a strict witness")
        if atlas_positive(row) and not strict_positive(row):
            raise ValueError(f"{name}: frozen atlas positive lost its strict reconciliation")

        item = {
            "schema": "qa.answer_key/v1",
            "key_id": KEY_ID,
            "item_id": f"{KEY_ID}:task:{name}@{subject['task_digest'][7:19]}",
            "subject": subject,
            "package": {
                "name": name,
                "pin": "historical primary-probe raw baseline, not v6",
                "root_hint": str(Path(binding["package_path"]).parent),
            },
            "expected_verdict": "fail" if atlas_positive(row) else "unknown",
            "expected_category": "weak" if atlas_positive(row) else None,
            "expected_categories": ["weak"] if atlas_positive(row) else [],
            "expected_fault": "eval" if atlas_positive(row) else None,
            "expected_root_causes": cause if atlas_positive(row) else [],
            "label_source": "Frozen original atlas status in handoff " + HANDOFF_SHA256
            + "; later atlas used for retained witness custody, not retroactive labels.",
            "label_strength": "confirmed" if atlas_positive(row) else "unknown",
            "selected_by": ["ref-subset", "QA Task retrospective sample"],
            "split": row["split"],
            "role": None,
            "pair_ids": [],
            "original_atlas_verdict": row["traces_atlas_verdict"],
            "qa_task_verdict": row["qa_task_verdict"],
            "source_handoff_sha256": HANDOFF_SHA256,
            "witness_ids": [witness["id"]] if atlas_positive(row) else [],
            "notes": "Source-derived raw-baseline reconciliation, not independent heldout recall. "
            "Unreviewed/nonpositive is unknown, never a synthetic clean negative. "
            "Preserved test split metadata does not establish unseen evaluation.",
        }
        AnswerKeyItem.model_validate(item)
        items.append(item)

        refs = [
            f"research/answer-keys/{KEY_ID}/audit.json#/tasks/{index}",
            f"{row['s2b_report_path']}#{row['s2b_report_sha256']}",
            f"{audit['source_handoff']['path']}#/tasks/{row['handoff_index']}",
        ]
        finding = {
            "schema": "qa.finding/v1",
            "id": f"{KEY_ID}:{name}:strict-decision",
            "subject": subject,
            "stage": "historical.S0_S2b.prompt_adjudication" if strict_positive(row)
            else "historical.S0_S2b.strict_nonflag",
            "verdict": "fail" if strict_positive(row) else "pass",
            "category": "weak",
            "severity": witness["severity"] if witness else None,
            "fault": "eval" if strict_positive(row) else None,
            "summary": witness["reason"] if witness else
            f"Scoped strict nonflag ({row['qa_task_verdict']}); recorded full=1,nop=0 and "
            "S2b probe evidence only. Not a clean-task certificate; sensitivity-only uncertain "
            "resolutions are excluded from the strict rule.",
            "evidence_refs": list(dict.fromkeys(refs + (witness["evidence_refs"] if witness else []))),
            "reproducer": {"kind": "patch", "path": witness["path"], "expected_reward": 1.0}
            if witness else None,
            "refutation": {
                "status": "verified" if witness else "unverified",
                "by": "Historical QA Task handoff and retained Traces source adjudication",
                "reason": "Recorded source controls bind the raw subject digest. Supported "
                "witness adjudication is reused, not blinded or independently replayed here. "
                "The nine different-digest v6 S3 results are separate diagnostics; no outcome transfer."
                if witness else
                "Measured source-probe nonflag under the strict rule only; no whole-task clean label.",
            },
            "reviewer": {"agent": "QA Task", "model": None, "tokens": None, "cost_usd": 0.0},
            "measurement": "measured",
            "root_cause": cause,
        }
        Finding.model_validate(finding)
        findings.append(finding)

    summary = summarize(audit)
    for name in (
        "raw_s2b_candidate_rate",
        "confirmed_weak_strict_rate",
        "confirmed_weak_with_resolved_uncertain_rate",
    ):
        original = audit["original_source_rate_summary"][name]
        if any(summary[name][field] != original[field] for field in ("count", "n", "rate", "wilson_95_ci")):
            raise ValueError(f"{name}: derived inventory disagrees with the frozen source summary")
    original_agreement = audit["original_source_atlas_crosscheck"]
    if summary["atlas_strict_binary_agreement"]["reviewed_overlap"] != original_agreement["atlas_reviewed_overlap"]:
        raise ValueError("reviewed overlap differs from the frozen original atlas crosscheck")
    for replay in audit["historical_v6_s3_diagnostics"]["replays"]:
        task = next(row for row in tasks if row["task_id"] == replay["task_id"])
        if replay["v6_digest"] != task["v6_package_digest"]:
            raise ValueError(f"{replay['task_id']}: v6 replay diagnostic digest mismatch")
    return {
        "items.jsonl": render(sorted(items, key=lambda row: row["item_id"])),
        "findings/decisions.jsonl": render(sorted(findings, key=lambda row: row["id"])),
        "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare deterministic outputs without writing")
    args = parser.parse_args()
    outputs = build()
    if args.check:
        stale = [
            name for name, text in outputs.items()
            if not (HERE / name).is_file() or (HERE / name).read_text(encoding="utf-8") != text
        ]
        if stale:
            print(f"stale: {stale}", file=sys.stderr)
            return 1
    else:
        for name, text in outputs.items():
            path = HERE / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print(outputs["summary.json"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
