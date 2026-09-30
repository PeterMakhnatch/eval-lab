#!/usr/bin/env python3
"""Fixed 6/4 development-vs-heldout split for the HAR-110 Python code tasks.

Split v2 (Research-Harbor decision HAR-110 2026-09-30T07:00Z): the v1 seed
ran on 3 unusable tasks -- 002259 broken by both raters, 002407 suspect by
one rater and broken by the other, 000226 leaking via pypi_fix_released --
so the card re-split to sound tasks only.

Development is fixed by that decision (exact list below). The held-out side
is the first 4, ordered by sha256("har110-py-v2:"+task_id), from the
ELIGIBLE pool: tasks in
research/explorations/trace-lab/har111/census_labels.jsonl with
in_python_pool true and both hand labels 'sound', minus the development
set, minus HAR-108 pypi_fix_released tasks.

``split.json`` is the single source of truth: ``fill_refs.py`` regenerates
the campaign examples from it, and ``make_paired_specs.py`` reads both
sides from it. Every consumer re-asserts ``split_digest`` on load.

Usage:
  uv run python research/experiments/har110-python-gepa/make_split.py
  uv run python research/experiments/har110-python-gepa/make_split.py --check

The split must never change once the live search has started.
"""

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har110-python-gepa"
SPLIT_PATH = EXP / "split.json"
CENSUS_PATH = REPO / "research/explorations/trace-lab/har111/census_labels.jsonl"

SALT = "har110-py-v2"
N_DEVELOPMENT = 6
N_HELDOUT = 4
DECISION_SOURCE = "HAR-110 Research-Harbor 2026-09-30T07:00Z"

# Exact development set fixed by the Research-Harbor decision above.
DEVELOPMENT = [
    "format-code-task-000383",
    "format-code-task-002256",
    "format-code-task-002391",
    "format-code-task-001832",
    "format-code-task-001896",
    "format-code-task-002864",
]

# Eligible held-out pool (sound x both raters, in the python pool, not in
# development, not pypi_fix_released). Asserted against the census file on
# every run; hardcoded so drift is loud, not silent.
ELIGIBLE_POOL = [
    "format-code-task-000328",
    "format-code-task-000495",
    "format-code-task-000587",
    "format-code-task-001161",
    "format-code-task-001181",
    "format-code-task-001373",
    "format-code-task-001689",
    "format-code-task-002532",
    "format-code-task-002961",
]

# HAR-108 pypi_fix_released tasks excluded from the held-out pool. The
# HAR-108 source (task_health.parquet column leak_channel) is not on main,
# so the exclusion is hardcoded; at split time it flagged
# format-code-task-000226 (sound x both raters, hence otherwise eligible):
# .worktrees/har108-census-20260930/research/experiments/har108-python-census/task_health.parquet
PYPI_FIX_RELEASED = {"format-code-task-000226"}


def _rank(task_id: str) -> str:
    return hashlib.sha256(f"{SALT}:{task_id}".encode()).hexdigest()


def load_sound_pool() -> tuple[set[str], str]:
    """Sound x both raters, in the python pool, per census_labels.jsonl.

    Returns (task ids, sha256 of the census file bytes).
    """
    if not CENSUS_PATH.is_file():
        raise SystemExit(f"refusing: census file missing: {CENSUS_PATH}")
    raw = CENSUS_PATH.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    pool = set()
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (
            row.get("in_python_pool")
            and row.get("hand_label") == "sound"
            and row.get("hand_label_rater2") == "sound"
        ):
            pool.add(row["task_id"])
    return pool, digest


def compute() -> dict:
    sound_pool, census_digest = load_sound_pool()
    # The census, minus the fixed development set, must yield exactly the
    # eligible 9 plus the pypi exclusion: any new sound task or label change
    # fails here instead of silently reshaping the split. (Three development
    # tasks -- 001832, 001896, 002391 -- are themselves sound x both raters
    # and appear in the census pool.)
    if sound_pool - set(DEVELOPMENT) != set(ELIGIBLE_POOL) | PYPI_FIX_RELEASED:
        raise SystemExit(
            "refusing: census sound pool drifted: "
            f"{sorted(sound_pool - set(DEVELOPMENT))} != eligible + "
            f"{sorted(PYPI_FIX_RELEASED)}"
        )
    eligible = [t for t in ELIGIBLE_POOL if t not in DEVELOPMENT]
    if set(eligible) != set(ELIGIBLE_POOL):
        raise SystemExit("refusing: development overlaps the eligible pool")
    held = sorted(eligible, key=_rank)[:N_HELDOUT]
    assert len(DEVELOPMENT) == N_DEVELOPMENT and len(held) == N_HELDOUT
    assert not (set(DEVELOPMENT) & set(held))
    body = {
        "experiment": "har110-python-gepa",
        "salt": SALT,
        "decision_source": DECISION_SOURCE,
        "census_labels_sha256": census_digest,
        "eligible_pool": sorted(eligible, key=_rank),
        "development": list(DEVELOPMENT),
        "heldout": held,
        "pypi_fix_released_excluded": sorted(PYPI_FIX_RELEASED),
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {"split_digest": digest, **body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify split.json matches recomputation, write nothing",
    )
    args = parser.parse_args()

    doc = compute()

    if args.check:
        if not SPLIT_PATH.is_file():
            raise SystemExit("refusing: no split.json to check")
        current = json.loads(SPLIT_PATH.read_text())
        if current == doc:
            print(f"split.json matches recomputation ({doc['split_digest'][:16]}...)")
            return 0
        print(
            "split.json DIFFERS from recomputation "
            f"(committed {current.get('split_digest')} vs recomputed {doc['split_digest']})"
        )
        print(f"committed development: {' '.join(current.get('development', []))}")
        print(f"recomputed development: {' '.join(doc['development'])}")
        return 2

    SPLIT_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {SPLIT_PATH.relative_to(REPO)} {doc['split_digest'][:16]}...")
    print(f"development ({len(doc['development'])}): {' '.join(doc['development'])}")
    print(f"heldout ({len(doc['heldout'])}): {' '.join(doc['heldout'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
