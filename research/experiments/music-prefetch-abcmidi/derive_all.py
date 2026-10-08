#!/usr/bin/env python3
"""Derive music-prefetch-abcmidi@1 variants for all 1,000 music tasks.

Lineage records land in this worktree's library/task-variants/; variant
packages materialize into the shared derived/ store. Resumes: existing
records/packages are kept, never overwritten.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.music_prefetch import derive_music_prefetch  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402
from evallab.task_variants import VariantExistsError  # noqa: E402

SNAP = (
    shared_checkout_root(REPO)
    / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-music@e1a66d4553ee/tasks"
)
REVISION = "e1a66d4553ee20b26c571de1bc2f4193d4a32c3c"

RATIONALE = (
    "Grading apt-installs abcmidi at grade time and fails unscored once the "
    "egress lock blocks the network; setup installs the same pinned abcmidi "
    "version before the agent starts, while the network is open, failing "
    "setup if it cannot. The grader already skips its install when abc2midi "
    "is present, so grading never touches the network. Grading, instruction "
    "and image are unchanged."
)


def main() -> None:
    tasks = sorted(p for p in SNAP.iterdir() if p.is_dir())
    print(f"{len(tasks)} parent tasks in {SNAP}")
    rows = []
    created = 0
    for i, parent in enumerate(tasks, 1):
        try:
            record = derive_music_prefetch(
                parent,
                rationale=RATIONALE,
                created_by="music-prefetch",
                repo_root=REPO,
                parent_source={
                    "kind": "hf",
                    "repo": "FineEnvs/MiMo-V2.6-RL-harbor-music",
                    "revision": REVISION,
                    "path": f"tasks/{parent.name}",
                },
            )
            created += 1
        except VariantExistsError:
            record = None
        rows.append(
            {
                "task_id": parent.name,
                "record": (
                    f"library/task-variants/{record.task_name.replace('/', '__')}/"
                    f"{record.variant_digest.split(':')[1][:12]}.json"
                    if record is not None
                    else "exists"
                ),
                "variant_digest": record.variant_digest if record is not None else None,
                "status": "derived" if record is not None else "exists",
            }
        )
        if i % 100 == 0:
            print(f"{i}/{len(tasks)} (new this run: {created})", flush=True)
    out = Path(__file__).resolve().parent / "derive-manifest.json"
    out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print(f"done: {len(tasks)} tasks, {created} newly derived; manifest {out}")


if __name__ == "__main__":
    main()
