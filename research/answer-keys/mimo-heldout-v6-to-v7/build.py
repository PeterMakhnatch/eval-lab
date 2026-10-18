"""Publish source-derived decisions for the original 33 MiMo held-out task IDs.

    uv run python research/answer-keys/mimo-heldout-v6-to-v7/build.py [--check]

Only the compact inventory and two committed cohort manifests are read. External
archive paths are custody references, never imports or fresh verifier executions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from evallab.qa.models import AnswerKeyItem, Finding

KEY_ID = "mimo-heldout-v6-to-v7"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RELATIVE = HERE.relative_to(REPO).as_posix()
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SETUP_FIXED_TASK = "mimo-v2.6-rl/format-code-task-001948"
SETUP_FIXED_DIGEST = "sha256:ec0208624b8ae606c83d7ecefdd404bb16b2bf4560dc276ac5f13bb8a3cca1df"
PINNED_SOURCES = {
    "original": "sha256:2f085e317114c9d13dd75613c7f69224d59a2d4b99aef0c63ede59a4bc863910",
    "verification": "sha256:3347cb819a2ee46c419ea4b2c9a78d74fe92b6eacf43361b20fc471edd3c272e",
    "extra": "sha256:dfbacfcb9c44b3a014d97dfb152d3007434cdfa7cc56aa699d35c25ce9c71bf7",
    "replacements": "sha256:1e2d063d9afd0be271b62f19c67fb99d3937b5d91105d666c2ee629c38ef817a",
}


def source_ref(audit: dict, source: str, pointer: str = "") -> str:
    binding = audit["sources"][source]
    return f"{binding['path']}#{pointer} ({binding['sha256']})"


def archive_ref(audit: dict, binding: dict) -> str:
    archive = audit["archives"][binding["archive"]]
    return f"{archive['path']}:{binding['member']} ({binding['sha256']})"


def cohort_source(audit: dict, source: str) -> dict:
    binding = audit["sources"][source]
    payload = (REPO / binding["path"]).read_bytes()
    if "sha256:" + hashlib.sha256(payload).hexdigest() != binding["sha256"]:
        raise ValueError(f"{source}: frozen cohort source bytes changed")
    return json.loads(payload)


def build() -> tuple[dict[str, str], dict]:
    audit = json.loads((HERE / "audit.json").read_text(encoding="utf-8"))
    if audit["schema"] != "qa-task.mimo-heldout-evidence/v1" or audit["key_id"] != KEY_ID:
        raise ValueError("unexpected immutable inventory contract")
    for source, digest in PINNED_SOURCES.items():
        if audit["sources"][source]["sha256"] != digest:
            raise ValueError(f"{source}: original handoff digest changed")
    diagnostics = audit["v7_diagnostics"]
    if any(
        record[field] is not False
        for record in (diagnostics, diagnostics["summary"])
        for field in ("scoring_allowed", "final_manifest_scientific_pass")
    ):
        raise ValueError("this historical evidence bundle must not promote the v7 release gate")
    manifest = cohort_source(audit, "original_manifest")
    metadata = cohort_source(audit, "split")["heldout_task_metadata"]
    frozen = {"mimo-v2.6-rl/" + row["task_id"]: row["v6_package_digest"] for row in metadata}
    rows = audit["tasks"]
    canonical = ["mimo-v2.6-rl/" + name for name in manifest["heldout"]]
    if (
        len(rows) != 33
        or len(set(canonical)) != 33
        or len(frozen) != 33
        or [row["task_id"] for row in rows] != canonical
        or manifest["heldout"] != audit["cohort"]["original_task_order"]
        or [row["ordinal"] for row in rows] != list(range(1, 34))
    ):
        raise ValueError("preserve the canonical original33 cohort, order, and membership without swaps")
    if Counter(row["expected_verdict"] for row in rows) != {"fail": 11, "unknown": 22}:
        raise ValueError("strict reconciliation has11 supported tasks and22 unknowns, not12 positives")
    items: list[dict] = []
    findings: list[dict] = []
    for row in rows:
        task = row["task_id"]
        original = row["original_package"]["digest"]
        scored = row["scored_package"]["digest"]
        if not DIGEST.fullmatch(original) or not DIGEST.fullmatch(scored) or original != frozen[task]:
            raise ValueError(f"{task}: missing or mismatched full original package digest")
        if task == SETUP_FIXED_TASK:
            if scored != SETUP_FIXED_DIGEST or scored == original:
                raise ValueError("001948 must use its measured setup-fixed baseline, not failing v6")
        elif scored != original:
            raise ValueError(f"{task}: original labels cannot transfer to a repaired digest")
        witness = row["witness"]
        decision = row["decision"]
        fail = row["expected_verdict"] == "fail"
        if (
            bool(witness) != fail
            or decision["verdict"] != ("fail" if fail else "pass")
            or decision["measurement"] != "measured"
        ):
            raise ValueError(f"{task}: inconsistent scoped task decision")
        references = [
            f"{RELATIVE}/audit.json#/tasks/{row['ordinal'] - 1}",
            archive_ref(audit, row["original_report"]),
        ]
        if same := row["same_digest_reaudit"]:
            if same["package_digest"] != original:
                raise ValueError(f"{task}: nonflag re-audit belongs to another package")
            references.append(source_ref(audit, same["source"], same["source_pointer"]))
        reproducer = None
        if witness:
            if (
                witness["package_digest"] != scored
                or witness["recorded_replay"]["package_digest"] != scored
                or witness["expected_reward"] != 1.0
                or witness["recorded_replay"]["reward"] != 1.0
                or witness["recorded_replay"]["verified"] is not True
                or not DIGEST.fullmatch(witness["sha256"])
                or not witness["reason"]
                or not witness["requirement"]
                or not any(
                    candidate["finding_id"] == witness["id"] and candidate["role"] == "wrong"
                    for candidate in row["candidate_roles"]
                )
            ):
                raise ValueError(f"{task}: unsupported or cross-digest wrong witness")
            references.extend(
                [
                    source_ref(audit, witness["source"], witness["source_pointer"]),
                    f"{witness['path']} ({witness['sha256']})",
                    witness["requirement"],
                ]
            )
            if "archive_patch" in witness:
                references.append(archive_ref(audit, witness["archive_patch"]))
                references.append(archive_ref(audit, witness["archive_finding"]))
            if "original_source_pointer" in witness:
                references.append(source_ref(audit, "original", witness["original_source_pointer"]))
            if "result" in witness["recorded_replay"]:
                for field in ("result", "lock"):
                    binding = witness["recorded_replay"][field]
                    references.append(f"{binding['path']} ({binding['sha256']})")
            reproducer = {"kind": "patch", "path": witness["path"], "expected_reward": 1.0}
        elif not (
            any(row["original_probes"][name]["status"] == "measured" for name in ("ref-subset", "ref-mutants"))
            or (row["same_digest_reaudit"] and row["same_digest_reaudit"]["quality_complete"])
        ):
            raise ValueError(f"{task}: a nonflag needs recorded measured probe evidence")
        subject = {"kind": "task", "task_id": task, "task_digest": scored}
        identifier = f"{KEY_ID}:{task.rsplit('/', 1)[1]}:decision"
        item = {
            "schema": "qa.answer_key/v1",
            "key_id": KEY_ID,
            "item_id": f"{KEY_ID}:{task}@{scored[7:19]}",
            "subject": subject,
            "original_subject": {"kind": "task", "task_id": task, "task_digest": original},
            "package": row["scored_package"],
            "expected_verdict": row["expected_verdict"],
            "expected_category": "weak" if fail else None,
            "expected_fault": "eval" if fail else None,
            "expected_root_causes": [],
            "label_source": "Retained original QA audit plus sealed candidate-role reconciliation; "
            "audit.json pins the sources and exact measured package, including the001948 exception.",
            "split": "heldout",
            "role": None,
            "pair_ids": [],
            "witness_ids": [witness["id"]] if fail else [],
            "notes": audit["suggested_score"]["notes"],
        }
        finding = {
            "schema": "qa.finding/v1",
            "id": identifier,
            "subject": subject,
            "stage": decision["stage"],
            "verdict": decision["verdict"],
            "category": "weak",
            "severity": witness["severity"] if witness else None,
            "fault": "eval" if fail else None,
            "summary": decision["summary"],
            "evidence_refs": references,
            "reproducer": reproducer,
            "refutation": {
                "status": "verified" if fail else "unverified",
                "by": "Retained QA Task and sealed source reconciliation",
                "reason": "Source-derived recorded evidence only; not a new replay, independent "
                "detector evaluation, exhaustive weakness search, or full grader certification.",
            },
            "reviewer": {"agent": "QA Task", "model": None, "tokens": None, "cost_usd": 0.0},
            "measurement": "measured",
            "root_cause": [],
        }
        AnswerKeyItem.model_validate(item)
        Finding.model_validate(finding)
        items.append(item)
        findings.append(finding)
    outputs = {
        "items.jsonl": "".join(
            json.dumps(item, sort_keys=True) + "\n"
            for item in sorted(items, key=lambda item: item["item_id"])
        ),
        "findings/decisions.jsonl": "".join(
            json.dumps(finding, sort_keys=True) + "\n"
            for finding in sorted(findings, key=lambda finding: finding["id"])
        ),
    }
    return outputs, audit["counts"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare deterministic committed outputs")
    args = parser.parse_args()
    outputs, counts = build()
    if args.check:
        stale = [
            name
            for name, text in outputs.items()
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
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
