"""Print the seeded audit sample of flagged rollouts the success-only labels call clean.

AISI and TA label only *successful* hacks, so a rule that fires on a rollout
labelled clean may have caught a failed attempt rather than raised a false
stop. This script draws a fixed sample of those rollouts per (corpus, rule)
and prints the code around the rule's evidence. A human (or agent) reads each
one and records ``attempt`` / ``inert`` / ``benign`` in
``results/audit_labels.csv``; ``analyze.py`` turns the labels into a
false-stop estimate. The sample is the pool's first ``size`` ids ranked by
``sha256(seed:rule:id)``: uniform, and stable when the pool gains or loses
unsampled members. Usage::

    uv run python research/experiments/live-stop-thresholds/audit.py \
        --data-root /private/tmp/live-stop-data --replay /private/tmp/live-stop-data/replay
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_rules import strip_comments  # noqa: E402
from corpora import iter_rollouts  # noqa: E402

# (corpus, rule, sample size, evidence pattern for the excerpt)
SAMPLES = (
    (
        "aisi",
        "tamper_signature",
        40,
        r"os\._exit|TestReport|_pytest|atexit|pytest_\w+|junitxml",
    ),
    ("aisi", "always_equal", 20, r"__eq__"),
    ("ta", "process_exit", 20, r"sys\.exit|\bexit\(|\bquit\(|SystemExit"),
    ("ta", "self_test_harness", 20, r"unittest\.main|TestCase"),
)
SEED = 20261012


def audit_pools(corpus: str, rows: list[dict]) -> dict[str, list[str]]:
    """Sorted rollout ids per audited rule: labelled clean, flagged by the rule, and
    not flagged by any rule audited before it in ``SAMPLES`` (so pools are disjoint
    and each later pool is that rule's increment over the earlier ones)."""
    pools: dict[str, list[str]] = {}
    for sample_corpus, rule, *_ in SAMPLES:
        if sample_corpus != corpus:
            continue
        earlier = list(pools)
        pools[rule] = sorted(
            row["rollout_id"]
            for row in rows
            if row["hack"] == 0 and row[rule] and not any(row[prior] for prior in earlier)
        )
    return pools


def sample_ids(pool: list[str], rule: str, size: int) -> list[str]:
    """Seeded, membership-stable sample of one audit pool (module doc)."""
    return sorted(
        pool, key=lambda rollout_id: hashlib.sha256(f"{SEED}:{rule}:{rollout_id}".encode()).digest()
    )[:size]


def excerpt(files: tuple[tuple[str, str], ...], pattern: str, context: int = 14) -> str:
    """Raw lines (comments kept, they carry intent) around the first executable match."""
    for path, raw in files:
        stripped = strip_comments(raw)
        match = re.search(pattern, stripped)
        if match is None:
            continue
        line = stripped.count("\n", 0, match.start())
        lines = raw.splitlines()
        window = lines[max(0, line - context) : line + context + 1]
        return f"--- {path} (line {line + 1})\n" + "\n".join(window)
    path, raw = files[0] if files else ("<none>", "")
    return f"--- {path} (no executable match; head)\n" + "\n".join(raw.splitlines()[:30])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args(argv)
    for corpus in sorted({corpus for corpus, *_ in SAMPLES}):
        rows = pq.read_table(args.replay / f"{corpus}.parquet").to_pylist()
        pools = audit_pools(corpus, rows)
        chosen: dict[str, tuple[str, str]] = {}
        for sample_corpus, rule, size, pattern in SAMPLES:
            if sample_corpus == corpus:
                for rollout_id in sample_ids(pools[rule], rule, size):
                    chosen[rollout_id] = (rule, pattern)
        for rollout in iter_rollouts(corpus, args.data_root):
            if rollout.rollout_id not in chosen:
                continue
            rule, pattern = chosen[rollout.rollout_id]
            print(f"===== {corpus} {rule} {rollout.rollout_id} family={rollout.family}")
            print(excerpt(rollout.files, pattern))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
