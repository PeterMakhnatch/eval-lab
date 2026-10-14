#!/usr/bin/env python3
"""Rebuild items.jsonl for the delphik-open-defect-tb2 answer key (qa.answer_key/v1).

Deterministic, stdlib + evallab only. Sources:
  - Delphik open-defect clone (defects/terminal-bench/tasks/*/*.json).
  - Terminal-Bench 2 packages checked out at the pinned TB 2.0 commit
    (a detached git worktree of harbor-framework/terminal-bench-2).
All items are heldout. Re-running reproduces items.jsonl byte-identically.
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import tomllib
from pathlib import Path

from evallab.registry import task_directory_digest

KEY_ID = "delphik-open-defect-tb2"
PIN = "f5b891cb4f7c20e306f9d05887628b43af740f43"

# artifact suffix -> (category, one-line rationale). Judged from
# title + summary + decision_note of each artifact, not defect_type_main alone.
CATEGORIES = {
    "pr-82": (
        "broken",
        "Debian 11 EOL breaks apt in the qemu verifier/image setup, so grading cannot run as designed.",
    ),
    "pr-36": ("broken", "Verifier points at wrong test file paths, so correct work is not graded."),
    "issue-75": (
        "broken",
        "Unpinned planarity drift makes the oracle fail and the verifier hit KeyError: 'pos'.",
    ),
    "pr-71": (
        "broken",
        "Verifier runs the full upstream pyknotid suite and fails on a flaky upstream test unrelated to the task.",
    ),
    "issue-59": ("broken", "Task fails even with the oracle solution."),
    "pr-38": (
        "prompt_misaligned",
        "Instruction is ambiguous about the build-pmars source directory.",
    ),
    "issue-76": (
        "broken",
        "Oracle solve.sh downloads POV-Ray sources from povray.org, which returns HTTP 403 (external drift at run time).",
    ),
    "pr-39": (
        "broken",
        "Verifier auth/username defect prevents correct configure-git-webserver work from passing.",
    ),
    "pr-28": ("broken", "Oracle/verifier defect in crack-7z-hash requires an oracle update."),
    "issue-46": (
        "broken",
        "Verifier uses BeautifulSoup normalization as ground truth, rejecting correct filter output.",
    ),
    "issue-89": (
        "weak",
        "A batch page that fails to load is silently counted as blocked, so batches that never ran can pass.",
    ),
    "issue-52": (
        "broken",
        "Oracle solve.sh calls tmux without installing it, so the oracle cannot run.",
    ),
    "issue-62": (
        "weak",
        "Verifier does not check the no-#includes rule, so gpt2-codegolf accepts submissions that bypass it.",
    ),
    "issue-67": (
        "tamperable",
        "Verifier executes agent-controlled vm.js, so the agent controls code in the verifier trust domain.",
    ),
    "issue-78": (
        "broken",
        "Broken setup at the pin: 404 dependency URL, missing MIPS/LLVM toolchain, nondeterministic frame comparison.",
    ),
    "pr-72": ("broken", "Oracle cannot build rstan because the environment lacks cmake."),
    "issue-60": (
        "reachable",
        "forward.py (the solution model) is left readable by the agent; no attempt to hide it.",
    ),
    "pr-40": (
        "prompt_misaligned",
        "Instruction underspecifies the required BGE query/passage prompt types the verifier assumes.",
    ),
    "pr-68": (
        "broken",
        "Verifier filesystem move is not cross-filesystem robust, so grading can crash.",
    ),
    "issue-61": ("reachable", "The reference `orig` binary is exposed to the agent."),
    "pr-48": (
        "broken",
        "Verifier speedup expectation is fragile/miscalibrated for correct portfolio-optimization work.",
    ),
    "pr-57": ("broken", "External data drift breaks the protein-assembly task as packaged."),
    "pr-83": (
        "broken",
        "Oracle exits before guest SSH setup finishes, causing intermittent verifier failure.",
    ),
    "issue-85": (
        "prompt_misaligned",
        "Verifier locks data-format, unit-conversion, peak-model and gamma conventions the prompt never states.",
    ),
    "pr-47": ("broken", "Oracle reliability defect in rstan-to-pystan."),
    "issue-45": (
        "prompt_misaligned",
        "Prompt specifies positional args but the verifier invokes named flags.",
    ),
    "issue-84": (
        "broken",
        "Verifier crashes with GitPython ValueError when the agent legitimately rewrites git history to remove secrets.",
    ),
}

# Artifacts excluded: defect absent / not a grader defect at the pin.
EXCLUDED = {
    "issue-44",  # not_counted for TB 2.0 health; resource infeasibility, covered by PR #50; no qa category
    "pr-14",  # not_counted; repo upkeep (uv upgrade), not a health/grader defect
}

REFUTED_TASK = "torch-pipeline-parallelism"  # candidate issue #88, terminal_status not_present


def suffix(artifact_id: str) -> str:
    return artifact_id.rsplit("__", 1)[-1]


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
    ap.add_argument("--open-defect", default="/tmp/open-defect")
    ap.add_argument("--tb2-root", default="~/Developer/lab-extras/upstream/terminal-bench-2-tb20")
    ap.add_argument("--tb2-pin", default=PIN)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "items.jsonl"))
    args = ap.parse_args()

    od = Path(args.open_defect).expanduser()
    tb2 = Path(args.tb2_root).expanduser()

    # Task dirs present at the pin.
    tasks = sorted(p.name for p in tb2.iterdir() if p.is_dir() and (p / "task.toml").is_file())
    task_set = set(tasks)

    # Defect artifacts grouped by task. all_artifact_tasks includes tasks
    # whose only artifact is excluded below (they still have an artifact,
    # so they are never eligible as negatives).
    by_task: dict[str, list[dict]] = {}
    all_artifact_tasks: set[str] = set()
    for path in sorted(glob.glob(str(od / "defects/terminal-bench/tasks/*/*.json"))):
        art = json.loads(Path(path).read_text(encoding="utf-8"))
        all_artifact_tasks.update(art.get("task_names") or [])
        if suffix(art["id"]) in EXCLUDED:
            continue
        for task in art.get("task_names") or []:
            by_task.setdefault(task, []).append(art)

    items = []
    for task in sorted(by_task):
        if task not in task_set:
            continue
        package_dir = tb2 / task
        task_id = task_id_for(package_dir, task)
        root_cause = []
        for art in sorted(by_task[task], key=lambda a: a["id"]):
            category, rationale = CATEGORIES[suffix(art["id"])]
            root_cause.append(
                {
                    "id": art["id"],
                    "category": category,
                    "summary": f"{art.get('title')}: {rationale}",
                    "source_class": art.get("defect_type_main") or art.get("audit_level"),
                }
            )
        items.append(
            {
                "schema": "qa.answer_key/v1",
                "key_id": KEY_ID,
                "item_id": task_id,
                "subject": {
                    "kind": "task",
                    "task_id": task_id,
                    "task_digest": task_directory_digest(package_dir),
                },
                "package": {"pin": args.tb2_pin, "name": task, "root_hint": tilde(tb2)},
                "expected_verdict": "fail",
                "expected_category": root_cause[0]["category"],
                "expected_categories": sorted({r["category"] for r in root_cause}),
                "root_cause": root_cause,
                "label_source": "Delphik open-defect defects/terminal-bench (local clone 49c659b) source-read audit",
                "label_strength": "confirmed",
                "split": "heldout",
            }
        )

    n_defect = len(items)

    # Negatives: no defect artifact and no candidate thread naming the task
    # (task_names, or the task name appearing in title/body).
    candidate_named = set()
    for path in glob.glob(
        str(od / "candidates/**/harbor-framework__terminal-bench-2__*.json"), recursive=True
    ):
        cand = json.loads(Path(path).read_text(encoding="utf-8"))
        candidate_named.update(cand.get("task_names") or [])
        text = f"{cand.get('title') or ''} {cand.get('body') or ''}"
        for task in tasks:
            if task in text:
                candidate_named.add(task)

    pool = sorted(task_set - all_artifact_tasks - candidate_named)
    for task in random.Random(0).sample(pool, n_defect):
        package_dir = tb2 / task
        task_id = task_id_for(package_dir, task)
        items.append(
            {
                "schema": "qa.answer_key/v1",
                "key_id": KEY_ID,
                "item_id": task_id,
                "subject": {
                    "kind": "task",
                    "task_id": task_id,
                    "task_digest": task_directory_digest(package_dir),
                },
                "package": {"pin": args.tb2_pin, "name": task, "root_hint": tilde(tb2)},
                "expected_verdict": "pass",
                "expected_category": None,
                "expected_categories": [],
                "root_cause": [],
                "label_source": "Delphik open-defect: no defect artifact or candidate thread; random.Random(0) sample",
                "label_strength": "weak_negative",
                "split": "heldout",
            }
        )

    # Refuted claim: candidate issue #88 audited as not_present.
    if REFUTED_TASK in task_set:
        package_dir = tb2 / REFUTED_TASK
        task_id = task_id_for(package_dir, REFUTED_TASK)
        items.append(
            {
                "schema": "qa.answer_key/v1",
                "key_id": KEY_ID,
                "item_id": task_id,
                "subject": {
                    "kind": "task",
                    "task_id": task_id,
                    "task_digest": task_directory_digest(package_dir),
                },
                "package": {"pin": args.tb2_pin, "name": REFUTED_TASK, "root_hint": tilde(tb2)},
                "expected_verdict": "pass",
                "expected_category": None,
                "expected_categories": [],
                "root_cause": [],
                "label_source": "Delphik open-defect candidate issue #88 terminal_status=not_present (source-read audit)",
                "label_strength": "refuted_claim",
                "split": "heldout",
            }
        )

    items.sort(key=lambda it: it["item_id"])
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, sort_keys=True) + "\n")
    print(f"wrote {len(items)} items ({n_defect} defect) to {out}")


if __name__ == "__main__":
    main()
