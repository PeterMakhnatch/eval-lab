#!/usr/bin/env python3
"""Fan-out runner for the fix-content census (scratch; see receipt README).

Usage: run_census.py <tasks-file> <results-dir>
  tasks-file: JSON {task_id: {fix, fix_source}} (fix = full sha or "" if none)
Reads /tmp/census-meta.json for image/workdir/language/published_pkg.
Stages published + clean setups, runs both probes sequentially per task;
parallelize across tasks with xargs -P.
"""

import json
import sys
from pathlib import Path

WT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/vals-routes-v2")
sys.path.insert(0, str(WT / "src"))
from evallab.exploit_probe import PURGE_INAPPLICABLE  # noqa: E402
from evallab.fix_content_census import (  # noqa: E402
    compose_clean_setup,
    run_probe,
    stage_probe,
)

PRIMARY = Path("/Users/petermakhnatch/Developer/eval-lab")
#: Census-measured purge-inapplicable beyond exploit_probe.PURGE_INAPPLICABLE:
#: 002938 (krakenex), 000666 (pyromat) and 000324 (missing CHANGES.txt) fail
#: the editable reinstall; 000905 (no project name) and 001198 (PEP 668, no
#: worktree source) fail the purge resolve/link steps. All fail closed.
NO_PURGE_EXTRA = frozenset(
    {
        "format-code-task-002938",
        "format-code-task-000666",
        "format-code-task-000905",
        "format-code-task-001198",
        "format-code-task-000324",
    }
)


def main() -> int:
    tasks_file, results = sys.argv[1], Path(sys.argv[2])
    meta = json.loads(Path("/tmp/census-meta.json").read_text(encoding="utf-8"))
    fixes = json.loads(Path(tasks_file).read_text(encoding="utf-8"))
    for task_id, fix in fixes.items():
        m = meta[task_id]
        tdir = results / task_id
        tdir.mkdir(parents=True, exist_ok=True)
        pub_setup = PRIMARY / m["published_pkg"] / "environment/setup"
        root_setup = (
            PRIMARY
            / f"derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/{task_id}"
            / "environment/setup"
        )
        # published mode
        stage = tdir / "published-stage"
        out = tdir / "published"
        (tdir / "published.meta.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "language": m["language"],
                    "image12": m["image12"],
                    "fix_source": fix["source"],
                }
            )
            + "\n"
        )
        stage_probe(stage, pub_setup)
        try:
            run_probe(m["image"], m["workdir"], fix["sha"], stage, out)
        except Exception as e:  # noqa: BLE001 -- record and continue the sweep
            (out / "FANOUT-ERROR").write_text(f"{type(e).__name__}: {e}\n")
        # clean mode
        root_sh = (root_setup / "setup.sh").read_text(encoding="utf-8")
        with_purge = (
            m["language"] == "Python"
            and task_id not in PURGE_INAPPLICABLE
            and task_id not in NO_PURGE_EXTRA
        )
        clean_sh, applied = compose_clean_setup(root_sh, with_purge=with_purge)
        (tdir / "clean.applied.json").write_text(json.dumps(applied) + "\n")
        stage = tdir / "clean-stage"
        out = tdir / "clean"
        (tdir / "clean.meta.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "language": m["language"],
                    "image12": m["image12"],
                    "fix_source": fix["source"],
                }
            )
            + "\n"
        )
        stage_probe(stage, root_setup, clean_sh.encode())
        try:
            run_probe(m["image"], m["workdir"], fix["sha"], stage, out)
        except Exception as e:  # noqa: BLE001
            (out / "FANOUT-ERROR").write_text(f"{type(e).__name__}: {e}\n")
        print(f"{task_id} done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
