#!/usr/bin/env python3
"""Pick 20 sound Python tasks for Engineering (HAR-104) from the census table.

Rules, applied to ``task_health.parquet`` and ``pypi.json``:
- ``label == "sound"`` and ``split == "train"``;
- clean evidence only: the nop hit no setup error, even an excused one; the
  hidden tests import no project name the instruction leaves out
  (``undisclosed_names`` empty); the test command names a runner;
- light image: ``image_mib`` known and at most ``--max-mib``;
- a confirmed upstream repo from ``leak_check.py``: the task title is an
  issue or PR title in that repo (``issue``), or the hidden tests import the
  PyPI project whose URLs name it (``match == "name"``). A split group alone
  does not confirm (002413's names pypa/packaging, a repo its issue mentions;
  its tests are poetry-plugin-export's). Repos are canonicalised through the
  GitHub repos API so a renamed repo (``apache/incubator-airflow``) counts
  once;
- one task per repo, and neither a repo nor a ``project_key`` already used by
  the HAR-105 part-3 set or its three rejected tasks;
- never ``leak_channel == "pypi_fix_released"`` (a PyPI release followed the
  upstream fix, so ``pip download`` very likely fetches it). The other
  channels are not ranked: ``git_only`` only means no PyPI project matched
  the repo name, and large projects publish under other names
  (``apache-airflow``, ``homeassistant``);
- lighter images first, then pool rank (seeded order), so the pick is
  deterministic.

Writes ``handoff.json`` with each task's evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyarrow.parquet as pq
from leak_check import GitHubSearch

HERE = Path(__file__).resolve().parent
PART3 = HERE.parent / "har105-exploration" / "python_selection.json"


def upstream_repo(leak: dict, search: GitHubSearch) -> str | None:
    """Canonical lower-case ``owner/name`` from one pypi.json entry, or None."""
    repo = (leak.get("repo_url") or "").removeprefix("https://github.com/")
    if not repo:
        return None
    return (search.canonical_repo(repo) or repo).lower()


def confirmed(leak: dict) -> bool:
    """The repo is the task's own: its title is there, or its tests import it."""
    return bool(leak.get("issue")) or leak.get("match") == "name"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", type=Path, default=HERE / "task_health.parquet")
    parser.add_argument("--pypi", type=Path, default=HERE / "pypi.json")
    parser.add_argument("--out", type=Path, default=HERE / "handoff.json")
    parser.add_argument("--cache", type=Path, default=Path("/private/tmp/har108/leak-cache"))
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--max-mib", type=int, default=2048)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    search = GitHubSearch(args.cache)
    rows = pq.read_table(args.table).to_pylist()
    by_id = {row["task_id"]: row for row in rows}
    leaks = json.loads(args.pypi.read_text())["tasks"]
    rank = {e["task_id"]: e["rank"] for e in json.loads((HERE / "pool.json").read_text())["pool"]}
    # All 13 entries: the 10 in the part-3 set and the 3 rejected as broken.
    used_ids = sorted(entry["task_id"] for entry in json.loads(PART3.read_text())["selection"])
    used_repos = {
        repo: task_id
        for task_id in used_ids
        if (repo := upstream_repo(leaks[task_id], search)) is not None
    }
    used_keys = {by_id[t]["project_key"] for t in used_ids}
    eligible = [
        row
        for row in rows
        if row["label"] == "sound"
        and row["split"] == "train"
        and row["task_id"] not in used_ids
        and not row["nop_setup_error"]
        and not row["undisclosed_names"]
        and row["test_runner"] != "none"
        and row["image_mib"] is not None
        and row["image_mib"] <= args.max_mib
        and row["leak_channel"] != "pypi_fix_released"
    ]
    eligible.sort(key=lambda r: (r["image_mib"], rank[r["task_id"]]))
    picked: list[dict] = []
    repos = set(used_repos)
    unresolved: list[str] = []
    duplicate: list[str] = []
    for row in eligible:
        leak = leaks[row["task_id"]]
        repo = upstream_repo(leak, search) if confirmed(leak) else None
        if repo is None:
            unresolved.append(row["task_id"])
            continue
        if repo in repos or row["project_key"] in used_keys:
            duplicate.append(row["task_id"])
            continue
        repos.add(repo)
        picked.append(
            {
                **row,
                "upstream_repo": repo,
                "repo_source": leak["repo_source"],
                "upstream_issue": (leak.get("issue") or {}).get("url"),
            }
        )
        if len(picked) == args.n:
            break
    fields = (
        "task_id",
        "task_version_digest",
        "upstream_repo",
        "repo_source",
        "upstream_issue",
        "image_mib",
        "test_runner",
        "leak_channel",
        "leak_pypi_project",
        "nop_job_name",
        "nop_reward",
        "evidence",
    )
    rules = __doc__.split("``pypi.json``:")[1].split("Writes")[0]
    args.out.write_text(
        json.dumps(
            {
                "card": "HAR-108",
                "rules": rules.strip(),
                "max_mib": args.max_mib,
                "eligible": len(eligible),
                "skipped_unresolved_repo": unresolved,
                "skipped_same_repo": duplicate,
                "excluded_repos": used_repos,
                "excluded_project_keys": sorted(used_keys),
                "selected": [{k: row[k] for k in fields} for row in picked],
            },
            indent=1,
        )
        + "\n"
    )
    print(
        f"{len(picked)} picked from {len(eligible)} eligible "
        f"({len(unresolved)} unresolved, {len(duplicate)} same-repo skipped) -> {args.out}"
    )


if __name__ == "__main__":
    main()
