#!/usr/bin/env python3
"""Derive the terminal task variants: guard for all 64, prefetch for two.

* ``terminal-guard-extend@1`` on every terminal snapshot task (62 derive
  straight from the pinned snapshot; 0260 and 2376 derive from their
  prefetch variant so the setup snapshot lands after the fetch).
* ``env-prefetch-network@1`` (HAR-146 shape) for the two missing-module
  graders: 0260 installs ``stevedore``, 2376 installs ``cryptography``.

Writes lineage records to ``library/task-variants/`` and packages to the
shared variants store. Refuses to overwrite: re-running reuses identical
records (derive raises ``VariantExistsError``, collected and reported).

Usage (from the repo root, inside the topic worktree)::

    uv run python research/experiments/terminal-task-ledger/derive.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from evallab.task_variants import VariantExistsError, materialize  # noqa: E402
from evallab.terminal_guard import derive_terminal_guard, derive_terminal_prefetch  # noqa: E402

HF_REPO = "FineEnvs/MiMo-V2.6-RL-harbor-terminal"
HF_REVISION = "fe1c2b665aae1ba7a09a270d979724d32269ae6a"


def _primary_root(start: Path) -> Path:
    """Primary checkout root (the pinned snapshot lives in its derived/)."""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    gitdir = Path(proc.stdout.strip())
    if not gitdir.is_absolute():
        gitdir = start / gitdir
    return gitdir.parent


PRIMARY = _primary_root(ROOT)
SNAPSHOT = PRIMARY / "derived" / "task-store" / "hf" / f"{HF_REPO.replace('/', '__')}@{HF_REVISION[:12]}" / "tasks"

#: Missing-module graders: task -> pip spec installed at setup while open.
PREFETCH: dict[str, str] = {
    "candidate-0260-security-appsec": "stevedore",
    "candidate-2376-security-cryptography": "cryptography",
}


def hf_source(task_id: str) -> dict[str, str]:
    return {"kind": "hf", "repo": HF_REPO, "revision": HF_REVISION, "path": f"tasks/{task_id}"}


def main() -> None:
    tasks = sorted(p.name for p in SNAPSHOT.iterdir() if p.is_dir())
    assert len(tasks) == 64, f"expected 64 snapshot tasks, saw {len(tasks)}"
    derived: dict[str, str] = {}
    reused = 0
    for task_id in tasks:
        parent = SNAPSHOT / task_id
        if task_id in PREFETCH:
            spec = PREFETCH[task_id]
            try:
                prefetch = derive_terminal_prefetch(
                    parent,
                    spec,
                    rationale=(
                        f"Terminal grader imports fail with ModuleNotFoundError "
                        f"({task_id}); install {spec} at setup while the network "
                        f"is open (env-prefetch-network@1, HAR-146 shape)."
                    ),
                    created_by="terminal-prefetch",
                    repo_root=ROOT,
                    parent_source=hf_source(task_id),
                )
            except VariantExistsError:
                reused += 1
                prefetch = None
            if prefetch is None:
                from evallab.task_variants import load_records

                recs = [
                    r
                    for r in load_records(ROOT / "library" / "task-variants")
                    if r.task_name.endswith(f"/{task_id}") and r.transform == "env-prefetch-network@1"
                ]
                prefetch = max(recs, key=lambda r: r.created_at)
            package = materialize(prefetch, parent, repo_root=ROOT)
            record_path = (
                Path("library") / "task-variants" / prefetch.task_name.replace("/", "__")
                / f"{prefetch.variant_digest[:12]}.json"
            )
            try:
                guard = derive_terminal_guard(
                    package,
                    created_by="terminal-guard",
                    repo_root=ROOT,
                    parent_source={"kind": "variant", "record": str(record_path)},
                )
            except VariantExistsError:
                reused += 1
                continue
            derived[task_id] = guard.variant_digest[:12]
            print(f"guard-on-prefetch {task_id} {guard.variant_digest[:12]}")
            continue
        try:
            guard = derive_terminal_guard(
                parent,
                created_by="terminal-guard",
                repo_root=ROOT,
                parent_source=hf_source(task_id),
            )
        except VariantExistsError:
            reused += 1
            continue
        derived[task_id] = guard.variant_digest[:12]
        print(f"guard {task_id} {guard.variant_digest[:12]}")
    print(f"derived {len(derived)} new guard variants; {reused} already existed")


if __name__ == "__main__":
    main()
