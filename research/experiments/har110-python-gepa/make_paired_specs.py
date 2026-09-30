#!/usr/bin/env python3
"""Generate HAR-110 eval specs from the retained student base spec.

Held-out comparison (default): the 8 paired seed-vs-GEPA specs for the
final held-out eval -- 4 held-out tasks x 2 arms (seed addendum vs
best-GEPA addendum), 1 attempt each (Terminus-2 binds exactly one trial).

Plain-dev baseline (`--plain-dev`): 6 specs, one per development task, for
the PLAIN Terminus prompt -- same route fields/limits from the base spec
and the per-task package digest, with NO extra instruction/addendum. This
is the second seed/baseline the Research-Harbor decision ordered for the
new dev split.

The student route, execution environment, harness tree and per-trial
limits are read from the SAME retained base spec the train search replays
(--base-spec, default
base-specs/student-terminus2-selfhosted-python.json), so swapping the route
there swaps it here; held-out arms differ ONLY in extra_instruction_path.

Package digests are asserted against the committed split.json at generation
time (split_digest pinned below -- never copied); the script refuses on drift
or on a missing worktree-local task materialization (see README recipe --
task bytes are NEVER committed and NEVER enter the train search unless they
are development tasks). Output specs go to paired-specs/ (held-out) or
plain-dev-specs/ (plain baseline); neither is committed (see .gitignore --
plain-dev-specs/ carries a .gitkeep only). Submission via `evallab submit`
parks them in queue/waiting/ (runtime state, never approved here). After
submit, record the queue IDs in <out-dir>/ids.txt.
paired-specs/cohort.json lists the 4 held-out packages.

The held-out arms can be generated separately: `--arm seed` writes only
the 4 seed-addendum specs (no winner needed, so the seed arm can run
early); `--arm winner` writes only the 4 candidate specs. Every spec in
the same arm/task is byte-identical however it is generated (same names,
same digests), so a seed arm run early still pairs with a winner arm run
later.

Usage:
  uv run python research/experiments/har110-python-gepa/make_paired_specs.py \
    --winner runs/<search>/lab/candidates/<sha>.txt --winner-sha256 sha256:<64hex>
  uv run python research/experiments/har110-python-gepa/make_paired_specs.py --arm seed
  uv run python research/experiments/har110-python-gepa/make_paired_specs.py --arm winner \
    --winner <path> --winner-sha256 sha256:<64hex>
  uv run python research/experiments/har110-python-gepa/make_paired_specs.py --plain-dev
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

PINNED_SPLIT_DIGEST = "sha256:8bf591e4d08e4630395a906ff341290e63e4068007aff9d307ffdb7168617c4b"

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
    name: str,
    arm: str,
    task_id: str,
    task_rel: str,
    package_digest: str,
    verifier_digest: str,
    addendum_rel: str | None,
    addendum_sha256: str | None,
    route: dict,
) -> dict:
    if arm == "gepa":
        instruction = "best-GEPA addendum"
    elif arm == "seed":
        instruction = "seed addendum"
    else:
        instruction = "plain Terminus prompt (no addendum)"
    return {
        "schema_version": 1,
        "name": name,
        "hypothesis": (
            f"HAR-110 paired held-out trial, task={task_id}, arm={arm}: "
            f"{route['agent']} student + {route['model']} on {route['environment']} with "
            f"{instruction}; "
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


def _materials(task_id: str, side: str) -> tuple[str, str, str]:
    task_dir = TASKS / task_id
    if not task_dir.is_dir():
        raise SystemExit(
            f"refusing: {side} task not materialized: {task_dir} "
            "(run the README recipe; bytes never committed)"
        )
    digest = task_directory_digest(task_dir)
    verifier_digest = compute_task_digests(task_dir).verifier
    return task_dir.relative_to(REPO).as_posix(), digest, verifier_digest


def _cohort_entry(task_id: str, package_digest: str, split: str) -> dict:
    return {
        "task_id": task_id,
        "package_digest": package_digest,
        "harbor_digest": harbor_task_digest(TASKS / task_id),
        "split": split,
    }


def _write(out_dir: Path, name: str, spec: dict) -> None:
    (out_dir / f"{name}.json").write_text(json.dumps(spec, indent=2) + "\n")


def _load_split() -> tuple[list[str], list[str]]:
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
    return dev, heldout


def _winner_ref(winner: Path | None, winner_sha256: str | None) -> tuple[str, str]:
    if winner is None or not winner_sha256:
        raise SystemExit("refusing: this mode needs --winner <path> --winner-sha256 sha256:<64hex>")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", winner_sha256):
        raise SystemExit("refusing: --winner-sha256 must be sha256:<64hex>")
    winner_abs = (Path.cwd() / winner).resolve()
    winner_rel = winner_abs.relative_to(REPO).as_posix()
    actual = "sha256:" + hashlib.sha256((REPO / winner_rel).read_bytes()).hexdigest()
    if actual != winner_sha256:
        raise SystemExit(f"refusing: winner digest {actual} != {winner_sha256}")
    return winner_rel, winner_sha256


def _report(route: dict, names: list[str], out_dir: Path, cohort_n: int) -> None:
    print(f"wrote {len(names)} specs + cohort.json ({cohort_n} tasks) to {out_dir}")
    print(
        f"route {route['agent']} + {route['model']} on {route['environment']}; "
        f"per-trial est ${route['est_cost_usd']:.2f}, model ceiling "
        f"${route['cost_limit_usd']:.2f}; total est ${len(names) * route['est_cost_usd']:.2f}"
    )


def run_heldout(
    route: dict, heldout: list[str], arms: list[str], winner: Path | None, winner_sha256: str | None
) -> int:
    seed_rel = SEED.relative_to(REPO).as_posix()
    seed_sha256 = "sha256:" + hashlib.sha256(SEED.read_bytes()).hexdigest()
    arm_refs: dict[str, tuple[str | None, str | None]] = {"seed": (seed_rel, seed_sha256)}
    if "gepa" in arms:
        arm_refs["gepa"] = _winner_ref(winner, winner_sha256)
    out_dir = EXP / "paired-specs"
    out_dir.mkdir(exist_ok=True)
    names = []
    cohort = []
    for task_id in heldout:
        task_rel, digest, verifier_digest = _materials(task_id, "held-out")
        short = task_id.removeprefix("format-code-task-")
        cohort.append(_cohort_entry(task_id, digest, "heldout"))
        for arm in arms:
            addendum_rel, addendum_sha = arm_refs[arm]
            name = f"har110-{short}-{arm}"
            _write(
                out_dir,
                name,
                _spec(
                    name,
                    arm,
                    task_id,
                    task_rel,
                    digest,
                    verifier_digest,
                    addendum_rel,
                    addendum_sha,
                    route,
                ),
            )
            names.append(name)
    (out_dir / "cohort.json").write_text(
        json.dumps({"experiment": "HAR-110 held-out comparison", "cohort": cohort}, indent=2) + "\n"
    )
    _report(route, names, out_dir, len(cohort))
    return 0


def run_plain_dev(route: dict, dev: list[str]) -> int:
    out_dir = EXP / "plain-dev-specs"
    out_dir.mkdir(exist_ok=True)
    names = []
    cohort = []
    for task_id in dev:
        task_rel, digest, verifier_digest = _materials(task_id, "development")
        short = task_id.removeprefix("format-code-task-")
        cohort.append(_cohort_entry(task_id, digest, "development"))
        name = f"har110-{short}-plain"
        _write(
            out_dir,
            name,
            _spec(name, "plain", task_id, task_rel, digest, verifier_digest, None, None, route),
        )
        names.append(name)
    (out_dir / "cohort.json").write_text(
        json.dumps({"experiment": "HAR-110 plain-dev baseline", "cohort": cohort}, indent=2) + "\n"
    )
    _report(route, names, out_dir, len(cohort))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--winner",
        type=Path,
        default=None,
        help="worktree path to the reviewed best-GEPA addendum text",
    )
    parser.add_argument(
        "--winner-sha256", default=None, help="sha256:<64hex> digest of the winner file (asserted)"
    )
    parser.add_argument(
        "--base-spec",
        type=Path,
        default=BASE_SPEC,
        help="retained student base spec shared with the train search",
    )
    parser.add_argument(
        "--arm",
        choices=("seed", "winner", "both"),
        default="both",
        help="held-out arms to write (default both; "
        "seed needs no winner, winner = candidate arm only)",
    )
    parser.add_argument(
        "--plain-dev",
        action="store_true",
        help="write the 6 plain-prompt development specs instead of the held-out comparison",
    )
    args = parser.parse_args()
    route = _route(args.base_spec)

    dev, heldout = _load_split()

    if args.plain_dev:
        if args.arm != "both":
            raise SystemExit("refusing: --plain-dev takes no --arm (it is its own mode)")
        if args.winner is not None or args.winner_sha256 is not None:
            raise SystemExit(
                "refusing: --plain-dev takes no --winner (plain prompt has no addendum)"
            )
        return run_plain_dev(route, dev)

    arms = (
        ["seed"] if args.arm == "seed" else ["gepa"] if args.arm == "winner" else ["seed", "gepa"]
    )
    return run_heldout(route, heldout, arms, args.winner, args.winner_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
