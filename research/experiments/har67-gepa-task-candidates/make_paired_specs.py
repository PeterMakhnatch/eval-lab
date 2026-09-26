#!/usr/bin/env python3
"""Generate the 16 paired original-vs-candidate model specs for HAR-67 step 4 (v2 arm).

Eight repeats per arm (sign test on decisive pairs: 7+ wins of 8 decisive
pairs clears one-sided p < 0.05; ties dropped). Stock mini-SWE-agent with
zai/glm-5.3-flash on local Docker, matched model/harness/resource settings;
the ONLY difference between arms is the task package (original vs the
validated instruction-clarified candidate).

Digests are recomputed from disk at generation time and asserted against the
pinned values -- the script refuses to generate on drift. Output specs go to
paired-specs/ (committed source); submission via `evallab submit` parks them
in queue/waiting/ (runtime state, never approved here).

Usage: uv run python research/experiments/har67-gepa-task-candidates/make_paired_specs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from evallab.registry import compute_task_digests, task_directory_digest

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har67-gepa-task-candidates"
ORIGINAL = EXP / "original"
CANDIDATE = EXP / "candidates/db-wal-recovery-format-clarify-v2"

PINNED_ORIGINAL_PACKAGE = "sha256:ce293e56ed1af10b86c6e953c41cdf451b1760b3203b41ec319c2e6b1d33cdc7"
PINNED_CANDIDATE_PACKAGE = (
    "sha256:38062bf1f7e824ced2d7fcda40afd66b5316aef81e3c0104c9a005fdc15188b8"
)
PINNED_VERIFIER = "sha256:be88472ef34579421fd57b23807bbab5768f18293c6f3ad83addf186ec305354"

SELECTION_RULE = "selection-rule.json in this directory"

REPEATS = 8
AGENT = "mini-swe-agent"
MODEL = "zai/glm-5.3-flash"
PER_TRIAL_COST_CEILING_USD = 0.40


def _spec(name: str, arm: str, task_dir: Path, package_digest: str, rep: int) -> dict:
    rel = task_dir.relative_to(REPO).as_posix()
    return {
        "schema_version": 1,
        "name": name,
        "hypothesis": (
            f"HAR-67 paired trial rep {rep}/8, arm={arm}: stock {AGENT} + {MODEL} on "
            f"{'format-clarify-v2 candidate (public-inputs spec clarification)' if arm == 'c2' else 'original'} "
            "db-wal-recovery; arms differ only in task package; decided under selection-rule.json"
        ),
        "purpose": "comparison",
        "question_ref": "har67-selection-rule",
        "task": rel,
        "task_path": rel,
        "task_id": "db-wal-recovery",
        "task_version": "1.0",
        "verifier_digest": PINNED_VERIFIER,
        "task_package_digest": package_digest,
        "agent": AGENT,
        "model": MODEL,
        "environment": "docker",
        "jobs_dir": "runs",
        "attempts": 1,
        "concurrency": 1,
        "timeout_seconds": 600,
        "submitted_by": "operator",
        "priority": 100,
        "est_cost_usd": PER_TRIAL_COST_CEILING_USD,
        "requires": [],
        "max_requests": 200,
        "max_input_tokens": 5000000,
        "max_output_tokens": 131072,
        "max_total_tokens": 5131072,
        "cost_limit_usd": PER_TRIAL_COST_CEILING_USD,
    }


def main() -> int:
    original_digest = task_directory_digest(ORIGINAL)
    candidate_digest = task_directory_digest(CANDIDATE)
    assert original_digest == PINNED_ORIGINAL_PACKAGE, f"original drift: {original_digest}"
    assert candidate_digest == PINNED_CANDIDATE_PACKAGE, f"candidate drift: {candidate_digest}"
    for label, path in (("original", ORIGINAL), ("candidate", CANDIDATE)):
        verifier = compute_task_digests(path).verifier
        assert verifier == PINNED_VERIFIER, f"{label} verifier drift: {verifier}"
    out_dir = EXP / "paired-specs"
    out_dir.mkdir(exist_ok=True)
    names = []
    for rep in range(1, REPEATS + 1):
        for arm, path, digest in (
            ("orig", ORIGINAL, original_digest),
            ("c2", CANDIDATE, candidate_digest),
        ):
            name = f"har67-dbwal-{arm}-r{rep}"
            (out_dir / f"{name}.json").write_text(
                json.dumps(_spec(name, arm, path, digest, rep), indent=2) + "\n"
            )
            names.append(name)
    total = REPEATS * 2 * PER_TRIAL_COST_CEILING_USD
    print(f"wrote {len(names)} specs to {out_dir}")
    print(f"per-trial ceiling ${PER_TRIAL_COST_CEILING_USD:.2f}; total ceiling ${total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
