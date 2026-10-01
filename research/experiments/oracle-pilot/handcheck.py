#!/usr/bin/env python3
"""Oracle-pilot step 1: what the original MiMo data carries, and whether the
upstream fix commit can be identified reliably.

Reads (committed inputs only):
  research/experiments/python-task-ledger/ledger.csv
  research/experiments/har108-python-census/task_health.parquet
  derived/task-store/hf/<code snapshot>/tasks/*/tests/test.patch
  derived/task-store/hf/<code snapshot>/tasks/*/instruction.md

Network (evidence gathering, $0): downloads XiaomiMiMo/MiMo-V2.6-RL-oss
``code`` parquet once (13 MB, cached at --parquet), clones five upstream
repos (or reuses --clone-dir), and queries the GitHub REST API (unauthenticated).

Writes handcheck_results.json next to this file and prints the numbers.

Usage (from the checkout root):
    uv run python research/experiments/oracle-pilot/handcheck.py [--clone-dir /tmp] [--parquet /tmp/mimo_code.parquet]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
#: derived/ is machine-local (gitignored): the snapshot lives in the primary
#: checkout, overridable with --snapshot.
SNAPSHOT_DEFAULT = (
    Path.home()
    / "Developer/eval-lab/derived/task-store/hf"
    / "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6"
)
CENSUS = ROOT / "research/experiments/har108-python-census/task_health.parquet"
LEDGER = ROOT / "research/experiments/python-task-ledger/ledger.csv"
PARQUET_URL = "https://huggingface.co/api/datasets/XiaomiMiMo/MiMo-V2.6-RL-oss/parquet/code/train/0.parquet"

#: The five hand-checked tasks: (task_id, repo, issue, distinctive hidden-test string).
HAND = [
    ("format-code-task-001647", "joke2k/django-environ", 173, "test_custom_db_scheme_no_engine_arg"),
    ("format-code-task-002408", "python-hyper/h2", 510, "TestPseudoHeadersWrongDirection"),
    ("format-code-task-000803", "didix21/mdutils", 42, "test_table_default_align"),
    ("format-code-task-001870", "matthewwithanm/python-markdownify", 170, "empty_line_optimization"),
    ("format-code-task-002470", "r1chardj0n3s/parse", 88, "test_sign_plus_issue_reproduction"),
]

PATCHED = re.compile(r"^diff --git a/(\S+)", re.M)
GITHUB_REPO = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+")
SHA40 = re.compile(r"\b[0-9a-f]{40}\b")


def gh_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def git_log_s(clone: Path, needle: str) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--oneline", "--all", "-S", needle, "--"],
        cwd=clone, capture_output=True, text=True, timeout=300,
    )
    out.check_returncode()
    return [line for line in out.stdout.splitlines() if line.strip()]


def main() -> None:
    import pyarrow.parquet as pq  # noqa: E402

    ap = argparse.ArgumentParser()
    ap.add_argument("--clone-dir", default="/tmp")
    ap.add_argument("--parquet", default="/tmp/mimo_code.parquet")
    ap.add_argument("--snapshot", default=str(SNAPSHOT_DEFAULT))
    args = ap.parse_args()
    snapshot = Path(args.snapshot)
    parq = Path(args.parquet)
    if not parq.exists():
        urllib.request.urlretrieve(PARQUET_URL, parq)
    table = pq.read_table(parq, columns=["extra_info", "reward_model"])
    rows = table.to_pylist()

    keysets: set[tuple[str, ...]] = set()
    nonempty_gt = 0
    n_gh = n_sha = 0
    for row in rows:
        ij = json.loads(row["extra_info"]["instance_json"])
        keysets.add(tuple(sorted(ij.keys())))
        if (row["reward_model"] or {}).get("ground_truth"):
            nonempty_gt += 1
        ps = ij.get("problem_statement", "")
        if GITHUB_REPO.search(ps):
            n_gh += 1
        if SHA40.search(ps):
            n_sha += 1

    ledger = list(csv.DictReader(LEDGER.open()))
    census = {
        r["task_id"]: r
        for r in pq.read_table(CENSUS, columns=["task_id", "leak_issue_url"]).to_pylist()
    }
    pool_issue = sum(1 for r in ledger if (census.get(r["task_id"]) or {}).get("leak_issue_url"))

    checks = []
    for task_id, repo, issue, needle in HAND:
        clone = Path(args.clone_dir) / repo.split("/")[1]
        if not clone.is_dir():
            subprocess.run(
                ["git", "clone", "-q", f"https://github.com/{repo}", str(clone)],
                check=True, timeout=600,
            )
            subprocess.run(
                ["git", "fetch", "-q", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*"],
                cwd=clone, check=False, timeout=600,
            )
        hits = git_log_s(clone, needle)
        tp = (snapshot / "tasks" / task_id / "tests" / "test.patch").read_text(errors="replace")
        files = [
            f for f in PATCHED.findall(tp)
            if "mimo_test_command" not in f and "test_commands.json" not in f
        ]
        checks.append({
            "task_id": task_id,
            "repo": repo,
            "issue_url": f"https://github.com/{repo}/issues/{issue}",
            "test_patch_files": files,
            "distinctive_hidden_test": needle,
            "upstream_commits_containing_it": hits,
            "synthetic": not hits,
        })

    pool_gh = 0
    pool_only_add = 0
    for r in ledger:
        tdir = snapshot / "tasks" / r["task_id"]
        ins = (tdir / "instruction.md").read_text(errors="replace")
        if GITHUB_REPO.search(ins):
            pool_gh += 1
        tp = (tdir / "tests" / "test.patch").read_text(errors="replace")
        files = PATCHED.findall(tp)
        if files and len(re.findall(r"^new file mode", tp, re.M)) == len(files):
            pool_only_add += 1
    result = {
        "source_dataset": "XiaomiMiMo/MiMo-V2.6-RL-oss code.parquet",
        "source_revision": "639865fd3374018d6cb29b9fb82dd531406fcf5f",
        "snapshot": SNAPSHOT_DEFAULT.name,
        "source_rows": len(rows),
        "instance_json_keysets": sorted("+".join(k) for k in keysets),
        "rows_with_nonempty_ground_truth": nonempty_gt,
        "problem_statements_with_github_repo_url": n_gh,
        "problem_statements_with_40hex_sha": n_sha,
        "pool_tasks": len(ledger),
        "pool_tasks_with_leak_issue_url": pool_issue,
        "pool_instructions_with_github_repo_url": pool_gh,
        "pool_patches_only_add_files": pool_only_add,
        "hand_checks": checks,
        "hand_synthetic_rate": f"{sum(c['synthetic'] for c in checks)}/{len(checks)}",
    }
    (HERE / "handcheck_results.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "hand_checks"}, indent=1))
    for c in checks:
        print(f"{c['task_id']} {c['repo']}#{c['issue_url'].rsplit('/', 1)[-1]} "
              f"synthetic={c['synthetic']} hits={len(c['upstream_commits_containing_it'])}")


if __name__ == "__main__":
    sys.exit(main())
