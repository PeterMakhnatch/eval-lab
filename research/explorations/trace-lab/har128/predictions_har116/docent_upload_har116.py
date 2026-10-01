"""Upload the 40 normalized HAR-116 trials to a NEW PRIVATE Docent collection (blind).

Blind metadata only: trial (OPAQUE id, not the real name), task, reward,
exception_type, n_episodes + assembly provenance (tags={} so no probe-03 rows,
no hand labels). The real trial names contain the arm
(baseline/loopfix/loopfix-r2/leakclosed/original), so trace_lab.trial carries
sha256(trial)[:12} and the local map opaque->real lives ONLY in
predictions_har116/docent_id_map.json (never uploaded). Arm strings are also
scrubbed from run metadata (harbor.* local paths etc.) where the conversion
allows; transcript message text stays verbatim (it is the evidence).
Secret scan aborts with exit 3 before any network call, except the reviewed
false-positive class from har119 (task-hash path fragments).
Collaborators printed before and after upload (must be owner only).

Usage: keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
  python research/explorations/trace-lab/har128/predictions_har116/docent_upload_har116.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PRED = HERE.parent  # predictions_har116
WORKTREE = HERE.parents[5]
NORM = Path("/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/har128/normalized")

sys.path.insert(0, str(WORKTREE / "research" / "explorations" / "trace-lab" / "docent"))

from docent import Docent  # noqa: E402
from export_harbor import SECRET_RE, convert_trial  # noqa: E402

NAME = "HAR-128 HAR-116 blind tool predictions (private, blind)"
# Reviewed false-positive class (2026-09-30, har119): the shared SECRET_RE `sk-`
# alternative matches the tail of `...task-001-<hex>...` job-dir path fragments
# auto-attached by the SDK under metadata.harbor.* (local paths, no creds).
FALSE_POSITIVE_RE = re.compile(r"sk-(00\d-[0-9a-f]{16,})")


def opaque(trial: str) -> str:
    return hashlib.sha256(trial.encode()).hexdigest()[:12]


def scrub(value: object, replacements: list[tuple[str, str]]) -> object:
    if isinstance(value, str):
        for old, new in replacements:
            if old and old in value:
                value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {k: scrub(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, replacements) for v in value]
    return value


def step_of(message: dict) -> int | None:
    md = message.get("metadata") or {}
    if isinstance(md.get("atif_step_id"), int):
        return md["atif_step_id"]
    ids = [i for i in (md.get("atif_step_ids") or []) if isinstance(i, int)]
    return min(ids) if ids else None


def main() -> int:
    trials = json.loads((PRED / "trials.json").read_text())
    assert len(trials) == 40, len(trials)
    id_map = {opaque(t["trial"]): t["trial"] for t in trials}
    assert len(id_map) == 40, "opaque id collision"
    (PRED / "docent_id_map.json").write_text(json.dumps(id_map, indent=1) + "\n")
    print(f"wrote local opaque map for {len(id_map)} trials (NOT uploaded)")

    norm_trials: dict[str, Path] = {}
    for job_dir in sorted(NORM.iterdir()):
        if not job_dir.is_dir():
            continue
        for trial_dir in sorted(job_dir.iterdir()):
            if (trial_dir / "agent" / "trajectory.json").exists():
                norm_trials[trial_dir.name] = trial_dir
    missing = [t["trial"] for t in trials if t["trial"] not in norm_trials]
    if missing:
        print(f"missing normalized trials: {missing}", file=sys.stderr)
        return 2

    client = Docent()
    collection_id = client.create_collection(
        name=NAME,
        description=(
            "40 normalized HAR-116 trials (single-file ATIF, tool calls "
            "restored); blind upload for HAR-128 part 2 tool scoring. "
            "trace_lab.trial carries opaque sha256 ids (real names contain "
            "the arm); metadata carries only trial/task/reward/exception/"
            "n_episodes + assembly provenance; no probe-03 tags, no labels."
        ),
    )
    print(f"created {collection_id}")
    before = client.get_collection_collaborators(collection_id)
    print(f"collaborators before upload: {before}")

    runs = []
    skipped = []
    block_map: dict[str, dict[str, int | None]] = {}
    for t in trials:
        trial = t["trial"]
        oid = opaque(trial)
        try:
            run = convert_trial(norm_trials[trial], NORM, {})
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{trial}: {exc}")
            continue
        # Blind the run: opaque trial id + scrub arm-carrying names from
        # metadata only (transcript text is evidence, stays verbatim).
        repl = [(trial, oid), (t["job"], oid)]
        run.merge_metadata({"trace_lab": {"trial": oid}})
        md = run.metadata or {}
        md = scrub(md, repl)
        # trace_lab.trial must be exactly the opaque id after scrub.
        if isinstance(md, dict) and isinstance(md.get("trace_lab"), dict):
            md["trace_lab"]["trial"] = oid
        run.metadata = md
        runs.append(run)
        # Block map on the same converted payload (single transcript/run).
        p = run.model_dump(mode="json")
        bmap: dict[str, int | None] = {}
        for tr in p.get("transcripts", []):
            for i, m in enumerate(tr.get("messages") or []):
                if str(i) not in bmap:
                    bmap[str(i)] = step_of(m)
        block_map[oid] = bmap
    print(f"{len(runs)} runs, {len(skipped)} skipped")
    for line in skipped:
        print(f"skipped {line}")

    payload = [run.model_dump(mode="json") for run in runs]
    n_messages = sum(
        len(t.get("messages") or []) for r in payload for t in r.get("transcripts", [])
    )
    n_transcripts = sum(len(r.get("transcripts", [])) for r in payload)
    print(f"{len(runs)} runs, {n_transcripts} transcripts, {n_messages} messages")

    # Blindness audit: no real trial name (arm-carrying) may appear in any
    # metadata; transcript text is exempt (verbatim evidence).
    real_names = [t["trial"] for t in trials] + [t["job"] for t in trials]
    leaks = []
    for r in payload:
        for tr in r.get("transcripts", []):
            for k, v in (tr.get("metadata") or {}).items():
                s = json.dumps(v, default=str)
                if any(n in s for n in real_names):
                    leaks.append(f"{k}: {s[:100]}")
        s = json.dumps(r.get("metadata"), default=str)
        if any(n in s for n in real_names):
            leaks.append(f"run metadata: {s[:120]}")
    if leaks:
        print(f"refusing: {len(leaks)} metadata arm leaks", file=sys.stderr)
        for h in leaks[:10]:
            print(f"  {h}", file=sys.stderr)
        return 4
    print("blindness audit: no real trial/job names in any metadata")

    bad: list[str] = []
    jobs = [t["job"] for t in trials] + [t["trial"] for t in trials]

    def walk(value: object) -> None:
        if isinstance(value, str):
            for m in SECRET_RE.finditer(value):
                hit = m.group(0)
                fp = FALSE_POSITIVE_RE.fullmatch(hit)
                if not (fp and any(f"task-{fp.group(1)}" in p for p in jobs)):
                    bad.append(hit[:12] + "...")
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

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
    (PRED / "docent_work_collection.txt").write_text(collection_id + "\n")
    (PRED / "docent" / "block_map.json").write_text(json.dumps(block_map) + "\n")
    print(f"wrote block map for {len(block_map)} opaque trials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
