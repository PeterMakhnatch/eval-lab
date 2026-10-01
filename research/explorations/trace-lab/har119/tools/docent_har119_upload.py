"""Upload the 12 normalized HAR-119 trials to a NEW PRIVATE Docent collection (blind).

Blind metadata only: trial, task, reward, exception_type, n_episodes +
assembly provenance (tags={} so no probe-03 rows, no hand labels).
Secret scan aborts with exit 3 before any network call, except for one
reviewed false-positive class (see FALSE_POSITIVE_RE below): the shared
SECRET_RE `sk-` alternative matches `...task-001-<32hex>...` job-dir path
fragments auto-attached by the SDK under metadata.harbor.*. Every hit must
provably be that shape AND a substring of a known job/trial path, else refuse.
Collaborators printed before and after upload (must be owner only).

Usage: keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
  python research/explorations/trace-lab/har119/tools/docent_har119_upload.py \
  [--collection CID to reuse, else a new private collection is created]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]
HAR119 = HERE.parent.parent
NORM = HAR119 / "predictions" / "scout_work" / "normalized"

sys.path.insert(0, str(WORKTREE / "research" / "explorations" / "trace-lab" / "docent"))

from docent import Docent  # noqa: E402
from export_harbor import SECRET_RE, convert_trial  # noqa: E402

NAME = "HAR-119 trace review (private, blind)"
# Reviewed false-positive class (2026-09-30): the shared SECRET_RE `sk-`
# alternative matches the tail of `...task-001-<hex>...` job-dir path
# fragments that the SDK auto-attaches under metadata.harbor.* (local paths,
# no credentials). A hit is excused ONLY if it fullmatches `sk-` + job-hash
# tail AND `task-<that tail>` is a substring of a known job/trial string
# (the substantive guard: a real credential tail never appears in job names).
FALSE_POSITIVE_RE = re.compile(r"sk-(00\d-[0-9a-f]{16,})")


def main() -> int:
    sel = json.loads((HAR119 / "selection.json").read_text())
    trials: list[str] = [r["trial"] for r in sel["runs"]]
    norm_trials: dict[str, Path] = {}
    for job_dir in sorted(NORM.iterdir()):
        if not job_dir.is_dir():
            continue
        for trial_dir in sorted(job_dir.iterdir()):
            if (trial_dir / "agent" / "trajectory.json").exists():
                norm_trials[trial_dir.name] = trial_dir
    missing = [t for t in trials if t not in norm_trials]
    if missing:
        print(f"missing normalized trials: {missing}", file=sys.stderr)
        return 2

    jobs = [r["job"] for r in sel["runs"]]
    reuse = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--collection" else None

    client = Docent()
    if reuse:
        collection_id = reuse
        print(f"reusing {collection_id}")
    else:
        collection_id = client.create_collection(
            name=NAME,
            description=(
                "12 normalized HAR-110 v2 trials (single-file ATIF, tool calls "
                "restored); blind upload for HAR-119 part 2 tool scoring. Metadata "
                "carries only trial/task/reward/exception/n_episodes + assembly "
                "provenance; no probe-03 tags, no hand labels."
            ),
        )
        print(f"created {collection_id}")
    before = client.get_collection_collaborators(collection_id)
    print(f"collaborators before upload: {before}")

    runs = []
    skipped = []
    for t in trials:
        try:
            runs.append(convert_trial(norm_trials[t], NORM, {}))
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{t}: {exc}")
    print(f"{len(runs)} runs, {len(skipped)} skipped")
    for line in skipped:
        print(f"skipped {line}")

    payload = [run.model_dump(mode="json") for run in runs]
    n_messages = sum(
        len(t.get("messages") or []) for r in payload for t in r.get("transcripts", [])
    )
    n_transcripts = sum(len(r.get("transcripts", [])) for r in payload)
    print(f"{len(runs)} runs, {n_transcripts} transcripts, {n_messages} messages")
    bad: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            for m in SECRET_RE.finditer(value):
                hit = m.group(0)
                fp = FALSE_POSITIVE_RE.fullmatch(hit)
                if not (fp and any(f"task-{fp.group(1)}" in p for p in paths)):
                    bad.append(hit[:12] + "...")
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    paths = jobs + trials
    walk(payload)
    if bad:
        print(f"refusing: {len(bad)} non-false-positive secret-like strings", file=sys.stderr)
        for h in bad[:20]:
            print(f"  {h}", file=sys.stderr)
        return 3
    print("secret scan: all hits (if any) are reviewed task-hash path fragments")

    client.add_agent_runs(collection_id, runs)
    after = client.get_collection_collaborators(collection_id)
    print(f"collaborators after upload: {after}")
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    (HAR119 / "predictions" / "docent_work_collection.txt").write_text(collection_id + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
