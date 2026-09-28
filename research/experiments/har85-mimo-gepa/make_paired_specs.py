#!/usr/bin/env python3
"""Generate the 32 paired seed-vs-GEPA model specs for the HAR-85 final held-out eval.

16 held-out tasks x 2 arms (seed addendum vs best-GEPA addendum), 1 attempt
each (Terminus-2 binds exactly one trial). The student route, execution
environment and per-trial limits are read from the SAME retained base spec the
train search replays (--base-spec, default base-specs/student-terminus2-
provisional.json), so swapping the route there swaps it here; arms differ ONLY
in extra_instruction_path. Decided under selection-rule.json.

Package digests are asserted against split.provisional.json at generation time;
the script refuses on drift or on a missing worktree-local held-out
materialization (see README recipe -- held-out bytes are NEVER committed and
NEVER enter the train search). Output specs go to paired-specs/ (committed
sources); submission via `evallab submit` parks them in queue/waiting/
(runtime state, never approved here). After submit, record the 32 queue IDs
in paired-specs/ids.txt for run-after-approval.sh. paired-specs/cohort.json
lists the 16 held-out packages for the selection rule's exploit screen
(`evallab tasks exploit-collect --cohort`, criterion 4).

Usage:
  uv run python research/experiments/har85-mimo-gepa/make_paired_specs.py \
    --winner runs/<search>/lab/candidates/<sha>.txt --winner-sha256 sha256:<64hex>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from evallab.registry import compute_task_digests, harbor_task_digest, task_directory_digest

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har85-mimo-gepa"
TASKS = EXP / "tasks"
SEED = EXP / "candidates/seed-addendum-v1.txt"
SPLIT = EXP / "split.provisional.json"
BASE_SPEC = EXP / "base-specs/student-terminus2-provisional.json"

PINNED_MANIFEST_DIGEST = (
    "sha256:fb645fed8acf01a1df3eddcf3d235c0d72b8afba353993170923e43560052dab"
)

# Fields copied verbatim from the retained base spec: the student route, where
# it runs, and every per-trial limit. Nothing here is a second source of truth.
ROUTE_FIELDS = (
    "agent",
    "model",
    "environment",
    "timeout_seconds",
    "est_cost_usd",
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "cost_limit_usd",
)


def _route(base_spec: Path) -> dict:
    base = json.loads(base_spec.read_text())
    missing = [field for field in ROUTE_FIELDS if base.get(field) is None]
    if missing:
        raise SystemExit(f"refusing: base spec {base_spec} lacks {', '.join(missing)}")
    return {field: base[field] for field in ROUTE_FIELDS}


def _spec(
    name: str, arm: str, task_id: str, task_rel: str, package_digest: str,
    verifier_digest: str, addendum_rel: str, addendum_sha256: str, route: dict,
) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "hypothesis": (
            f"HAR-85 paired held-out trial, task={task_id}, arm={arm}: provisional "
            f"{route['agent']} student + {route['model']} on {route['environment']} with "
            f"{'best-GEPA addendum' if arm == 'gepa' else 'seed addendum'}; "
            "arms differ only in extra_instruction_path; decided under selection-rule.json"
        ),
        "purpose": "comparison",
        "question_ref": "har85-selection-rule",
        "task": task_rel,
        "task_path": task_rel,
        "task_id": task_id,
        "verifier_digest": verifier_digest,
        "task_package_digest": package_digest,
        "extra_instruction_path": addendum_rel,
        "extra_instruction_sha256": addendum_sha256,
        "jobs_dir": "runs",
        "attempts": 1,
        "concurrency": 1,
        "submitted_by": "operator",
        "priority": 100,
        "requires": [],
        **route,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--winner", type=Path, required=True,
                        help="worktree path to the reviewed best-GEPA addendum text")
    parser.add_argument("--winner-sha256", required=True,
                        help="sha256:<64hex> digest of the winner file (asserted)")
    parser.add_argument("--base-spec", type=Path, default=BASE_SPEC,
                        help="retained student base spec shared with the train search")
    args = parser.parse_args()
    route = _route(args.base_spec)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.winner_sha256):
        raise SystemExit("refusing: --winner-sha256 must be sha256:<64hex>")

    manifest = json.loads(SPLIT.read_text())
    if manifest.get("manifest_digest") != PINNED_MANIFEST_DIGEST:
        raise SystemExit(
            f"refusing: split manifest digest {manifest.get('manifest_digest')} "
            f"!= pinned {PINNED_MANIFEST_DIGEST} (HAR-81 may have sealed a new split)"
        )
    by_id = {row["task_id"]: row for row in manifest["tasks"]}
    heldout = manifest["heldout_task_ids"]
    if len(heldout) != 16:
        raise SystemExit(f"refusing: expected 16 held-out ids, got {len(heldout)}")
    winner_abs = (Path.cwd() / args.winner).resolve()
    winner_rel = winner_abs.relative_to(REPO).as_posix()
    winner_bytes = (REPO / winner_rel).read_bytes()
    import hashlib
    actual = "sha256:" + hashlib.sha256(winner_bytes).hexdigest()
    if actual != args.winner_sha256:
        raise SystemExit(f"refusing: winner digest {actual} != {args.winner_sha256}")
    seed_rel = SEED.relative_to(REPO).as_posix()
    seed_sha256 = "sha256:" + hashlib.sha256(SEED.read_bytes()).hexdigest()

    out_dir = EXP / "paired-specs"
    out_dir.mkdir(exist_ok=True)
    names = []
    cohort = []
    for task_id in heldout:
        row = by_id[task_id]
        task_dir = TASKS / task_id
        if not task_dir.is_dir():
            raise SystemExit(
                f"refusing: held-out task not materialized: {task_dir} "
                "(run the README recipe; bytes never committed)"
            )
        digest = task_directory_digest(task_dir)
        if digest != row["task_package_digest"]:
            raise SystemExit(f"refusing: {task_id} drift: {digest}")
        task_rel = task_dir.relative_to(REPO).as_posix()
        verifier_digest = compute_task_digests(task_dir).verifier
        short = task_id.replace("candidate-", "")
        cohort.append({
            "task_id": task_id,
            "package_digest": digest,
            "harbor_digest": harbor_task_digest(task_dir),
            "split": "heldout",
        })
        for arm, addendum_rel, addendum_sha in (
            ("seed", seed_rel, seed_sha256),
            ("gepa", winner_rel, args.winner_sha256),
        ):
            name = f"har85-{short}-{arm}"
            (out_dir / f"{name}.json").write_text(
                json.dumps(
                    _spec(name, arm, task_id, task_rel, digest,
                          verifier_digest, addendum_rel, addendum_sha, route),
                    indent=2,
                )
                + "\n"
            )
            names.append(name)
    (out_dir / "cohort.json").write_text(
        json.dumps({"experiment": "HAR-85 held-out exploit screen", "cohort": cohort}, indent=2)
        + "\n"
    )
    print(f"wrote {len(names)} specs + cohort.json ({len(cohort)} tasks) to {out_dir}")
    print(f"route {route['agent']} + {route['model']} on {route['environment']}; "
          f"per-trial est ${route['est_cost_usd']:.2f}, model ceiling "
          f"${route['cost_limit_usd']:.2f}; total est ${len(names) * route['est_cost_usd']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
