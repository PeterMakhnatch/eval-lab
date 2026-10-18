"""Build the digest-bound positive key from the complete MiMo probe atlas.

    uv run python research/answer-keys/mimo-weak-tests-v2/build.py [--check]

The committed atlas is the reviewed input, not a claim that unlabelled tasks are
clean. Every exported positive retains an executable, hash-pinned passing patch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from evallab.qa.models import AnswerKeyItem, Finding

KEY_ID = "mimo-weak-tests-v2"
HERE = Path(__file__).resolve().parent
KEYS = HERE.parent
REPO = KEYS.parents[1]
SPLIT_SEED = "mimo-weak-tests-v1:split:20261015"
ROOT_CAUSES = {"block_subset": "untested_reference_block", "line_mutant": "untested_reference_line"}


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render(rows: list[dict]) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def task_name(task_id: str) -> str:
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def locked_splits(additional_keys: tuple[str, ...] = ()) -> dict[str, str]:
    locked: dict[str, str] = {}
    for key in (
        "trace-paired-v1",
        "executed-outcomes-v1",
        "mimo-weak-tests-v1",
        *additional_keys,
    ):
        for item in jsonl(KEYS / key / "items.jsonl"):
            split = "test" if item["split"] in ("test", "heldout") else "dev"
            locked.setdefault(task_name(item["subject"]["task_id"]), split)
    return locked


def split_for(name: str, locked: dict[str, str]) -> str:
    if name in locked:
        return locked[name]
    number = int.from_bytes(hashlib.sha256(f"{SPLIT_SEED}:{name}".encode()).digest()[:4], "big")
    return "dev" if number % 3 == 0 else "test"


def build(
    *,
    here: Path = HERE,
    key_id: str = KEY_ID,
    additional_split_keys: tuple[str, ...] = (),
    findings_name: str = "findings-supported-v2.jsonl",
) -> dict[str, str]:
    atlas = jsonl(here / "atlas.jsonl")
    names = [row["task_id"] for row in atlas]
    if len(atlas) != 567 or len(set(names)) != 567:
        raise ValueError("the frozen primary cohort must contain exactly 567 distinct tasks")
    if sorted(row["selection_index"] for row in atlas) != list(range(1, 568)):
        raise ValueError("selection indices must preserve all 567 frozen cohort positions")
    locked = locked_splits(additional_split_keys)
    items: list[dict] = []
    findings: list[dict] = []
    witness_ids: set[str] = set()
    for row in sorted(atlas, key=lambda value: value["task_id"]):
        name = row["task_id"]
        if row["primary"]["completion"] != "reported":
            raise ValueError(f"{name}: primary continuation is incomplete")
        subject = {
            "kind": "task",
            "task_id": row["task_name"],
            "task_digest": row["package_digest"],
        }
        witnesses = row["witnesses"]
        if row["semantic_verdict"] != ("confirmed_weak" if witnesses else "not_established"):
            raise ValueError(f"{name}: semantic verdict disagrees with accepted witnesses")
        for witness in witnesses:
            identifier = f"{key_id}:{name}:{witness['id']}"
            if identifier in witness_ids:
                raise ValueError(f"duplicate witness {identifier}")
            witness_ids.add(identifier)
            relative = Path(witness["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "witnesses":
                raise ValueError(
                    f"{name}: witness must stay inside the committed witness directory"
                )
            patch = here / relative
            if hashlib.sha256(patch.read_bytes()).hexdigest() != witness["sha256"]:
                raise ValueError(f"{name}: witness bytes do not match their recorded digest")
            if witness["package_digest"] != row["package_digest"]:
                raise ValueError(f"{name}: witness belongs to another task package")
            controls = witness["controls"]
            for label, reward in (("full", 1), ("nop", 0), ("witness", 1)):
                grade = controls[label]
                if grade["status"] != "scored" or grade["reward"] != reward or grade["rc"] != 0:
                    raise ValueError(f"{name}: unusable recorded {label} control")
                expected = label
                if label == "witness":
                    expected = (
                        "mutant:" + witness["id"][5:]
                        if witness["kind"] == "line_mutant"
                        else "confirm"
                    )
                if grade["label"] != expected:
                    raise ValueError(f"{name}: recorded {label} grade belongs to another witness")
            if witness["review_label"] != "required_gap":
                raise ValueError(f"{name}: an unaccepted mutation cannot become a finding")
            cause = ROOT_CAUSES[witness["kind"]]
            finding = {
                "schema": "qa.finding/v1",
                "id": identifier,
                "subject": subject,
                "stage": "S2.ref_subset" if witness["kind"] == "block_subset" else "S2.ref_mutants",
                "verdict": "fail",
                "category": "weak",
                "severity": witness["severity"],
                "fault": "eval",
                "summary": witness["reason"],
                "evidence_refs": witness["evidence_refs"],
                "reproducer": {
                    "kind": "patch",
                    "path": str(patch.relative_to(REPO)),
                    "expected_reward": 1.0,
                },
                "refutation": {
                    "status": "verified",
                    "by": "Traces semantic adjudication",
                    "reason": "Recorded full=1, nop=0, witness=1 controls plus instruction-level "
                    "source review. Not a fresh replay or repaired-package certification.",
                },
                "reviewer": {"agent": "Traces", "model": None, "tokens": None, "cost_usd": 0.0},
                "measurement": "measured",
                "root_cause": [cause],
            }
            Finding.model_validate(finding)
            findings.append(finding)
        if not witnesses:
            continue
        causes = sorted({ROOT_CAUSES[w["kind"]] for w in witnesses})
        item = {
            "schema": "qa.answer_key/v1",
            "key_id": key_id,
            "item_id": f"{key_id}:task:{name}@{row['package_digest'][7:19]}",
            "subject": subject,
            "package": row["package"],
            "expected_verdict": "fail",
            "expected_category": "weak",
            "expected_categories": ["weak"],
            "expected_fault": "eval",
            "expected_root_causes": causes,
            "label_source": "Recorded ref-subset controls and accepted instruction-level "
            "adjudication; see atlas.jsonl and hash-pinned witnesses.",
            "label_strength": "confirmed",
            "selected_by": ["ref-subset"],
            "split": split_for(name, locked),
            "role": None,
            "pair_ids": [],
            "evidence": [w["reason"] for w in witnesses],
            "witness_ids": [f"{key_id}:{name}:{w['id']}" for w in witnesses],
            "notes": "Positive-only source-derived target catalog; not an independent held-out key.",
        }
        AnswerKeyItem.model_validate(item)
        items.append(item)
    items.sort(key=lambda item: item["item_id"])
    findings.sort(key=lambda finding: finding["id"])
    scans = [scan for row in atlas for scan in row["line"]["scans"]]
    survivor_reviews = {
        source: dict(
            sorted(
                Counter(
                    survivor["review"]["label"]
                    for scan in scans
                    if scan["source"] == source
                    for survivor in scan["survivors"]
                ).items()
            )
        )
        for source in sorted({scan["source"] for scan in scans})
    }
    summary = {
        "cohort_tasks": len(atlas),
        "primary_reports": len(atlas),
        "cohort_selection_sha256": hashlib.sha256(
            "".join(
                row["task_id"] + "\n"
                for row in sorted(atlas, key=lambda value: value["selection_index"])
            ).encode()
        ).hexdigest(),
        "primary_source_counts": dict(
            sorted(Counter(row["primary"]["source"] for row in atlas).items())
        ),
        "primary_status_counts": dict(
            sorted(Counter(row["primary"]["status"] for row in atlas).items())
        ),
        "primary_verdict_counts": dict(
            sorted(Counter(row["primary"]["verdict"] for row in atlas).items())
        ),
        "block_review_counts": dict(
            sorted(
                Counter(
                    row["block"]["review"]["label"] for row in atlas if row["block"]["review"]
                ).items()
            )
        ),
        "line_scans_by_source": dict(sorted(Counter(scan["source"] for scan in scans).items())),
        "line_survivor_reviews_by_source": survivor_reviews,
        "tasks_without_any_line_scan": sum(not row["line"]["scans"] for row in atlas),
        "unknown_reason_counts": dict(
            sorted(Counter(reason for row in atlas for reason in row["unknowns"]).items())
        ),
        "confirmed_weak_tasks": len(items),
        "not_established_tasks": len(atlas) - len(items),
        "executable_witnesses": len(findings),
        "witness_kind_counts": dict(
            sorted(
                Counter(witness["kind"] for row in atlas for witness in row["witnesses"]).items()
            )
        ),
        "negative_items": 0,
        "split_counts": {
            split: sum(item["split"] == split for item in items) for split in ("dev", "test")
        },
        "limits": "Source-derived positive target catalog, not detector recall, benchmark prevalence, "
        "a clean-task certificate, fresh witness replay, or repaired-package certification.",
    }
    return {
        "items.jsonl": render(items),
        findings_name: render(findings),
        "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare deterministic outputs")
    args = parser.parse_args()
    outputs = build()
    if args.check:
        stale = [
            name
            for name, text in outputs.items()
            if not (HERE / name).is_file() or (HERE / name).read_text() != text
        ]
        if stale:
            print(f"stale: {stale}", file=sys.stderr)
            return 1
    else:
        for name, text in outputs.items():
            (HERE / name).write_text(text, encoding="utf-8")
    print(outputs["summary.json"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
