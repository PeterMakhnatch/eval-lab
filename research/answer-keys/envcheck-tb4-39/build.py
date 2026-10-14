#!/usr/bin/env python3
"""Rebuild items.jsonl for the envcheck-tb4-39 answer key (qa.answer_key/v1).

Deterministic, stdlib + evallab only. Sources:
  - EnvCheck findings (post == "2026-10-grader-defects") for root causes.
  - kev build_task_audit.py stats (derived/kev/sets/task_audit_stats.json)
    for the labelled (pin, task) set: 13 defect + 26 weak_negative = 39.
Re-running with the same inputs reproduces items.jsonl byte-identically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from evallab.registry import task_directory_digest

KEY_ID = "envcheck-tb4-39"
POST = "2026-10-grader-defects"

# Same rules as kev build_task_audit.py CLASS_RULES / FAMILY_RULES,
# then pattern -> qa.answer_key category.
CLASS_RULES = {
    "answer-material-readable-by-candidate": "task_answer_in_env",
    "verifier-state-writable-by-candidate": "task_writable_grader",
    "candidate-code-in-verifier-trust-domain": "task_writable_grader",
}
FAMILY_RULES = {
    "insufficient-checking": "task_weak_assertion",
    "checker-logic": "task_weak_assertion",
}
PATTERN_CATEGORY = {
    "task_answer_in_env": "reachable",
    "task_writable_grader": "tamperable",
    "task_weak_assertion": "weak",
}

TB_ROOTS = {
    "v4.0.0": "~/Developer/eval-lab/derived/kev/raw/terminal-bench-v4.0.0",
    "1dcda8716784": "~/Developer/eval-lab/derived/kev/raw/terminal-bench-1dcda8716784",
}


def finding_pattern(finding: dict) -> str | None:
    defect = finding.get("defect") or {}
    cls, fam = defect.get("class"), defect.get("family")
    if cls in CLASS_RULES:
        return CLASS_RULES[cls]
    if fam in FAMILY_RULES:
        return FAMILY_RULES[fam]
    return None


def task_id_for(package_dir: Path, dir_name: str) -> str:
    toml_path = package_dir / "task.toml"
    if toml_path.is_file():
        with open(toml_path, "rb") as f:
            name = (tomllib.load(f).get("task") or {}).get("name")
        if name:
            return name
    return f"terminal-bench/{dir_name}"


def tilde(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home) :] if s.startswith(home) else s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--findings",
        default="~/Developer/eval-lab/derived/kev/raw/envcheck-findings/data/findings.jsonl",
    )
    ap.add_argument(
        "--stats", default="~/Developer/eval-lab/derived/kev/sets/task_audit_stats.json"
    )
    ap.add_argument("--tb-root-v4", default=TB_ROOTS["v4.0.0"])
    ap.add_argument("--tb-root-1dc", default=TB_ROOTS["1dcda8716784"])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "items.jsonl"))
    args = ap.parse_args()

    findings_path = Path(args.findings).expanduser()
    stats = json.loads(Path(args.stats).expanduser().read_text(encoding="utf-8"))
    roots = {
        "v4.0.0": Path(args.tb_root_v4).expanduser(),
        "1dcda8716784": Path(args.tb_root_1dc).expanduser(),
    }

    findings = {}
    with open(findings_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("post") == POST:
                findings[row["slug"]] = row

    commit = stats["envcheck"]["commit"]
    label_source = (
        f"tokenless-ai/envcheck-findings@{commit} post/{POST} "
        "via kev build_task_audit.py (seed 0 negatives)"
    )

    items = []
    for key in sorted(stats["tasks"]):
        pin, task = key.split("/", 1)
        info = stats["tasks"][key]
        package_dir = roots[pin] / "tasks" / task
        task_id = task_id_for(package_dir, task)
        base = {
            "schema": "qa.answer_key/v1",
            "key_id": KEY_ID,
            "item_id": task_id,
            "subject": {
                "kind": "task",
                "task_id": task_id,
                "task_digest": task_directory_digest(package_dir),
            },
            "package": {
                "pin": pin,
                "name": task,
                "root_hint": tilde(roots[pin] / "tasks"),
            },
        }
        if info["label"] == "defect":
            root_cause = []
            for slug in sorted(info["finding_slugs"]):
                finding = findings[slug]
                defect = finding.get("defect") or {}
                pattern = finding_pattern(finding)
                root_cause.append(
                    {
                        "id": slug,
                        "category": PATTERN_CATEGORY[pattern],
                        "summary": finding.get("title") or slug,
                        "source_class": defect.get("class") or defect.get("family"),
                    }
                )
            items.append(
                {
                    **base,
                    "expected_verdict": "fail",
                    "expected_category": PATTERN_CATEGORY[info["pattern"]],
                    "expected_categories": sorted({r["category"] for r in root_cause}),
                    "root_cause": root_cause,
                    "label_source": label_source,
                    "label_strength": "confirmed",
                    "split": "heldout",  # assigned below
                }
            )
        else:
            items.append(
                {
                    **base,
                    "expected_verdict": "pass",
                    "expected_category": None,
                    "expected_categories": [],
                    "root_cause": [],
                    "label_source": label_source,
                    "label_strength": "weak_negative",
                    "split": "heldout",
                }
            )

    # Split: stratified by label; within each label order by
    # sha256("envcheck-tb4-39:" + item_id) hex ascending; first round(n/3) dev.
    for verdict in ("fail", "pass"):
        group = [it for it in items if it["expected_verdict"] == verdict]
        group.sort(key=lambda it: hashlib.sha256(f"{KEY_ID}:{it['item_id']}".encode()).hexdigest())
        for it in group[: round(len(group) / 3)]:
            it["split"] = "dev"

    items.sort(key=lambda it: it["item_id"])
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, sort_keys=True) + "\n")
    print(f"wrote {len(items)} items to {out}")


if __name__ == "__main__":
    main()
