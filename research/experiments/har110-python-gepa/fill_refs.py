#!/usr/bin/env python3
"""Regenerate HAR-110 campaign examples from split.json (single source of truth).

Rewrites ONLY the ``examples`` list of ``campaign-train.json`` (6 development
tasks) and ``qualification-campaign.json`` (same 6, never with prior-run
references: the qualification uses the deterministic proposer, and prior runs
are reflection context). Every other campaign field is preserved byte for
byte, so the staged proposer binding is unaffected.

Without ``--trials`` the examples carry no ``prior_run_reference`` (valid for
``load_campaign``; the live search still needs them). With ``--trials <dir>``
each development task maps to the single finished trial subdir of
``<dir>/har104-d-<suffix>/har104-d-<suffix>__<trial>`` (HAR-104 job layout;
entries starting ``_aborted-`` are ignored, and zero or several finished
trials refuse). The script records ``{trial_path, result_sha256,
task_package_digest}`` over the *trial-level* result.json after asserting the
trial finished and the task bytes match the current digest. Trial paths are
stored repo-relative when they live under this repository, verbatim
otherwise (load-time ``validate_prior_run_reference`` judges them: refs
outside the repo do not load, so stage the trials under ``prior-trials/``
per the README recipe before filling).

Usage:
  uv run python research/experiments/har110-python-gepa/fill_refs.py
  uv run python research/experiments/har110-python-gepa/fill_refs.py \\
      --trials /path/to/har104-runs/runs
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har110-python-gepa"
TASKS = EXP / "tasks"
SPLIT_PATH = EXP / "split.json"
TRAIN_PATH = EXP / "campaign-train.json"
QUAL_PATH = EXP / "qualification-campaign.json"

sys.path.insert(0, str(REPO / "src"))
from evallab.registry import task_directory_digest  # noqa: E402


def _example(task_id: str) -> dict:
    task_dir = TASKS / task_id
    if not task_dir.is_dir():
        raise SystemExit(
            f"refusing: task not materialized: {task_dir} (see README recipe)"
        )
    return {
        "task_id": task_id,
        "task_path": (EXP / "tasks" / task_id).relative_to(REPO).as_posix(),
        "task_package_digest": task_directory_digest(task_dir),
        "split": "development",
    }


def _prior_ref(task_id: str, trials_root: Path, package_digest: str) -> dict:
    suffix = task_id.removeprefix("format-code-task-")
    job_dir = trials_root / f"har104-d-{suffix}"
    if not job_dir.is_dir():
        raise SystemExit(
            f"refusing: no HAR-104 job for {task_id} at {job_dir} "
            "(run fill_refs again once the HAR-104 batch has finished)"
        )
    finished: list[Path] = []
    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir() or trial_dir.is_symlink():
            continue
        if not trial_dir.name.startswith(job_dir.name + "__"):
            continue
        if trial_dir.name.startswith("_aborted-"):
            continue
        result = trial_dir / "result.json"
        if not result.is_file():
            continue
        payload = json.loads(result.read_text())
        if payload.get("finished_at"):
            finished.append(trial_dir)
    if not finished:
        raise SystemExit(f"refusing: no finished HAR-104 trial for {task_id} under {job_dir}")
    if len(finished) > 1:
        names = ", ".join(t.name for t in finished)
        raise SystemExit(f"refusing: ambiguous finished HAR-104 trials for {task_id}: {names}")
    trial_dir = finished[0]
    result = trial_dir / "result.json"
    res_bytes = result.read_bytes()
    try:
        rel = trial_dir.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        rel = trial_dir.resolve().as_posix()
    return {
        "trial_path": rel,
        "result_sha256": "sha256:" + hashlib.sha256(res_bytes).hexdigest(),
        "task_package_digest": package_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=Path, default=None,
                        help="root holding HAR-104 har104-d-<suffix>/ job dirs")
    args = parser.parse_args()

    split = json.loads(SPLIT_PATH.read_text())
    dev = split["development"]
    if len(dev) != 6 or len(split["heldout"]) != 4:
        raise SystemExit("refusing: split.json is not a 6/4 split")

    trials_root = (Path.cwd() / args.trials).resolve() if args.trials else None
    train_examples = []
    qual_examples = []
    for task_id in dev:
        base = _example(task_id)
        qual_examples.append(dict(base))
        if trials_root is not None:
            base["prior_run_reference"] = _prior_ref(
                task_id, trials_root, base["task_package_digest"]
            )
        train_examples.append(base)

    for path, examples in ((TRAIN_PATH, train_examples), (QUAL_PATH, qual_examples)):
        if not path.is_file():
            raise SystemExit(f"refusing: campaign file missing: {path}")
        doc = json.loads(path.read_text())
        doc["examples"] = examples
        path.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"wrote {len(examples)} examples to {path.relative_to(REPO)}")

    print(f"split {split['split_digest'][:16]}...; "
          f"prior refs: {'attached (--trials)' if trials_root else 'absent (loader-valid, not live-ready)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
