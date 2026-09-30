#!/usr/bin/env python3
"""Generate the 8 paired seed-vs-GEPA specs for the HAR-110 final held-out eval.

4 held-out tasks x 2 arms (seed addendum vs best-GEPA addendum), 1 attempt
each (Terminus-2 binds exactly one trial). The student route, execution
environment, harness tree and per-trial limits are read from the SAME retained
base spec the train search replays (--base-spec, default
base-specs/student-terminus2-selfhosted-python.json), so swapping the route
there swaps it here; arms differ ONLY in extra_instruction_path.

Package digests are asserted against the committed split.json at generation
time (split_digest pinned below -- never copied); the script refuses on drift
or on a missing worktree-local held-out materialization (see README recipe --
held-out bytes are NEVER committed and NEVER enter the train search). Output
specs go to paired-specs/ (committed sources); submission via `evallab submit`
parks them in queue/waiting/ (runtime state, never approved here). After
submit, record the 8 queue IDs in paired-specs/ids.txt.
paired-specs/cohort.json lists the 4 held-out packages.

Usage:
  uv run python research/experiments/har110-python-gepa/make_paired_specs.py \
    --winner runs/<search>/lab/candidates/<sha>.txt --winner-sha256 sha256:<64hex>
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har110-python-gepa"
TASKS = EXP / "tasks"
SEED = EXP / "candidates/seed-addendum-v1.txt"
BASE_SPEC = EXP / "base-specs/student-terminus2-selfhosted-python.json"
SPLIT_PATH = EXP / "split.json"

PINNED_SPLIT_DIGEST = (
    "sha256:4e9861fd34b58675929c6bcc36b85a95571864f7821411e9ec13be18ef29f2ed"
)

sys.path.insert(0, str(REPO / "src"))
from evallab.registry import (  # noqa: E402
    compute_task_digests,
    harbor_task_digest,
    task_directory_digest,
)

# Fields copied verbatim from the retained base spec: the student route, where
# it runs, and every per-trial limit. Nothing here is a second source of truth.
# override_storage_mb is included (unlike HAR-85's terminal tasks, these code
# tasks declare no storage_mb, and without the override Harbor would request
# the 3 GiB Daytona default; HAR-88 + HAR-104 pin 10240).
ROUTE_FIELDS = (
    "agent",
    "model",
    "environment",
    "harness_tree_path",
    "harness_tree_sha256",
    "timeout_seconds",
    "est_cost_usd",
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "cost_limit_usd",
    "override_storage_mb",
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
            f"HAR-110 paired held-out trial, task={task_id}, arm={arm}: "
            f"{route['agent']} student + {route['model']} on {route['environment']} with "
            f"{'best-GEPA addendum' if arm == 'gepa' else 'seed addendum'}; "
            "arms differ only in extra_instruction_path"
        ),
        "purpose": "comparison",
        "question_ref": "har110-python-gepa",
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

    split = json.loads(SPLIT_PATH.read_text())
    if split["split_digest"] != PINNED_SPLIT_DIGEST:
        raise SystemExit(
            f"refusing: split digest {split['split_digest']} "
            f"!= pinned {PINNED_SPLIT_DIGEST} (re-run fill_refs + qualification first)"
        )
    dev, heldout = split["development"], split["heldout"]
    if len(dev) != 6 or len(heldout) != 4:
        raise SystemExit("refusing: split.json is not a 6/4 split")
    if set(dev) & set(heldout):
        raise SystemExit("refusing: split development/heldout overlap (leak into search)")

    winner_abs = (Path.cwd() / args.winner).resolve()
    winner_rel = winner_abs.relative_to(REPO).as_posix()
    winner_bytes = (REPO / winner_rel).read_bytes()
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
        task_dir = TASKS / task_id
        if not task_dir.is_dir():
            raise SystemExit(
                f"refusing: held-out task not materialized: {task_dir} "
                "(run the README recipe; bytes never committed)"
            )
        digest = task_directory_digest(task_dir)
        verifier_digest = compute_task_digests(task_dir).verifier
        short = task_id.removeprefix("format-code-task-")
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
            name = f"har110-{short}-{arm}"
            (out_dir / f"{name}.json").write_text(
                json.dumps(
                    _spec(name, arm, task_id, task_dir.relative_to(REPO).as_posix(),
                          digest, verifier_digest, addendum_rel, addendum_sha, route),
                    indent=2,
                )
                + "\n"
            )
            names.append(name)
    (out_dir / "cohort.json").write_text(
        json.dumps({"experiment": "HAR-110 held-out comparison", "cohort": cohort}, indent=2)
        + "\n"
    )
    print(f"wrote {len(names)} specs + cohort.json ({len(cohort)} tasks) to {out_dir}")
    print(f"route {route['agent']} + {route['model']} on {route['environment']}; "
          f"per-trial est ${route['est_cost_usd']:.2f}, model ceiling "
          f"${route['cost_limit_usd']:.2f}; total est ${len(names) * route['est_cost_usd']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
