#!/usr/bin/env python3
"""Fixed 6/4 optimisation-vs-heldout split for the 10 HAR-110 Python code tasks.

The split is fixed BEFORE any live GEPA result is seen and is deterministic:
a seeded hash over task ids decides the order. When HAR-104 baseline rewards
are available (``--rewards`` JSON mapping task_id -> reward), the split is
stratified so passes are spread over both sides; the same salt breaks ties
within each stratum, so the output is still fully determined by its inputs.

``split.json`` is the single source of truth: ``fill_refs.py`` regenerates the
campaign examples from it, and ``make_paired_specs.py`` reads the held-out
side from it. Every consumer re-asserts ``split_digest`` on load.

Usage:
  uv run python research/experiments/har110-python-gepa/make_split.py
  uv run python research/experiments/har110-python-gepa/make_split.py \\
      --rewards <task-id-to-reward.json>
  uv run python research/experiments/har110-python-gepa/make_split.py --check

--rewards overwrites split.json with the stratified version. If the
development set changes relative to the already-run nop qualification,
re-run the qualification on the new development set before the live
campaign (one command, ~$0.05; see README). The split itself must never
change once the live search has started.
"""

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har110-python-gepa"
SPLIT_PATH = EXP / "split.json"

SALT = "har110-py-v1"
N_DEVELOPMENT = 6
N_HELDOUT = 4

TASK_IDS = [
    "format-code-task-000226",
    "format-code-task-000383",
    "format-code-task-000927",
    "format-code-task-001832",
    "format-code-task-001896",
    "format-code-task-002256",
    "format-code-task-002259",
    "format-code-task-002391",
    "format-code-task-002407",
    "format-code-task-002864",
]


def _rank(task_id: str) -> str:
    return hashlib.sha256(f"{SALT}:{task_id}".encode()).hexdigest()


def split_without_rewards() -> tuple[list[str], list[str]]:
    ordered = sorted(TASK_IDS, key=_rank)
    return ordered[:N_DEVELOPMENT], ordered[N_DEVELOPMENT:]


def split_with_rewards(rewards: dict[str, float]) -> tuple[list[str], list[str]]:
    unknown = set(TASK_IDS) - set(rewards)
    if unknown:
        raise SystemExit(f"refusing: rewards missing tasks: {sorted(unknown)}")
    extra = set(rewards) - set(TASK_IDS)
    if extra:
        raise SystemExit(f"refusing: rewards list unknown tasks: {sorted(extra)}")
    passes = sorted(
        [t for t in TASK_IDS if float(rewards[t]) > 0], key=_rank
    )
    fails = sorted([t for t in TASK_IDS if float(rewards[t]) <= 0], key=_rank)
    # Spread passes over both sides proportionally, then fill with fails;
    # within each stratum the seeded hash decides, so this stays deterministic.
    n_pass_dev = round(len(passes) * N_DEVELOPMENT / len(TASK_IDS))
    dev = passes[:n_pass_dev]
    held = passes[n_pass_dev:]
    dev += [t for t in fails if t not in dev][: N_DEVELOPMENT - len(dev)]
    held += [t for t in fails if t not in dev and t not in held]
    dev = sorted(dev, key=_rank)
    held = sorted(held, key=_rank)
    assert len(dev) == N_DEVELOPMENT and len(held) == N_HELDOUT
    assert not (set(dev) & set(held)) and set(dev) | set(held) == set(TASK_IDS)
    return dev, held


def payload(dev: list[str], held: list[str], rewards_digest: str | None) -> dict:
    body = {
        "experiment": "har110-python-gepa",
        "salt": SALT,
        "development": dev,
        "heldout": held,
        "stratified_by_har104_rewards": rewards_digest,
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()
    ).hexdigest()
    return {"split_digest": digest, **body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rewards", type=Path, default=None,
                        help="JSON mapping task_id -> HAR-104 reward (stratify passes)")
    parser.add_argument("--check", action="store_true",
                        help="verify split.json matches recomputation, write nothing")
    args = parser.parse_args()

    if args.rewards is not None:
        rewards = json.loads((Path.cwd() / args.rewards).read_text())
        rewards_digest = "sha256:" + hashlib.sha256(
            json.dumps(rewards, sort_keys=True).encode()
        ).hexdigest()
        dev, held = split_with_rewards(rewards)
    else:
        rewards_digest = None
        dev, held = split_without_rewards()
    doc = payload(dev, held, rewards_digest)

    if args.check:
        # Verify-only: recompute from the same inputs and compare against the
        # committed split.json; write nothing. With --rewards this previews the
        # stratified split and reports whether adopting it would change the sets.
        if not SPLIT_PATH.is_file():
            raise SystemExit("refusing: no split.json to check")
        current = json.loads(SPLIT_PATH.read_text())
        if current == doc:
            print(f"split.json matches recomputation ({doc['split_digest'][:16]}...)")
            return 0
        print(
            f"split.json DIFFERS from recomputation "
            f"(committed {current.get('split_digest')} vs recomputed {doc['split_digest']})"
        )
        print(f"committed development: {' '.join(current.get('development', []))}")
        print(f"recomputed development: {' '.join(dev)}")
        return 2


    SPLIT_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {SPLIT_PATH.relative_to(REPO)} {doc['split_digest'][:16]}...")
    print(f"development ({len(dev)}): {' '.join(dev)}")
    print(f"heldout ({len(held)}): {' '.join(held)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
