#!/usr/bin/env python3
"""Generate HAR-120/G2 data-batch specs (gate G2 of HAR-126).

The 30 tasks of ``research/experiments/python-task-ledger/har120_proposal.csv``
x 2 attempts (60 runs) on the frozen lf2 harness tree, minus any task that
shares a repository with Data's frozen G1 eval list (HAR-127). Each row's
``run_digest`` is the digest to run: the original census digest, or the
leak-closed / repaired variant digest where one is listed.

The route, environment and per-trial limits come from the SAME retained
HAR-110 base spec the HAR-116 runs use (--base-spec); only the harness tree
(the lf2 treatment, passed on the CLI) varies. Task sides and digests come
from the committed proposal CSV, which is the single source of truth;
staging and generation refuse on any drift.

Task bytes are staged into the gitignored tasks/ dir: originals are copied
from the pinned read-only snapshot, variants are rebuilt with
evallab.task_variants.materialize (which proves the record invariant) and
then copied. Nothing under tasks/ is committed.

Usage (from the worktree root):
  uv run --no-sync python research/experiments/har120-data-batch/make_specs.py \\
    --tree <path> --tree-digest sha256:<64hex> \\
    --eval-list research/experiments/ovn-sft-v0/eval_tasks.csv
  uv run --no-sync python research/experiments/har120-data-batch/make_specs.py \\
    --check <specs-dir> --tree <path> --tree-digest sha256:<64hex> \\
    --eval-list research/experiments/ovn-sft-v0/eval_tasks.csv

Submission via `evallab submit` parks specs in queue/waiting/ (runtime
state, never approved here). After submit, record the queue IDs in
<out-dir>/ids.txt.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import stat
import sys
from contextlib import suppress
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har120-data-batch"
PROPOSAL_PATH = REPO / "research/experiments/python-task-ledger/har120_proposal.csv"
BASE_SPEC = (
    REPO
    / "research/experiments/har110-python-gepa/base-specs/student-terminus2-selfhosted-python.json"
)
TASKS = EXP / "tasks"
SNAPSHOT_TASKS = "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
QUESTION_REF = "har120-data-batch"
ATTEMPTS_PER_TASK = 2

sys.path.insert(0, str(REPO / "src"))
from evallab.queue import read_spec  # noqa: E402
from evallab.registry import compute_task_digests, task_directory_digest  # noqa: E402
from evallab.task_variants import materialize  # noqa: E402
from evallab.terminus_harness import load_harness_tree  # noqa: E402

# Fields copied verbatim from the retained HAR-110 base spec, minus the
# harness tree (which is the lf2 treatment, passed on the CLI). Same
# rationale as HAR-110's ROUTE_FIELDS: override_storage_mb is included
# because these code tasks declare no storage_mb, and without the override
# Harbor would request the 3 GiB Daytona default.
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
    "override_storage_mb",
)

RUN_KINDS = ("original", "leak-closed", "repair")


def repo_key(project: str) -> str | None:
    """The repository a task comes from, normalised; None when unknown.

    Same convention as the ledger build: keys are compared by their last
    path segment, ignoring case and treating `-` and `_` as the same, so
    `github.com/psf/black` and `black` count as one repository. A project
    that is just the task id means no repository was found.
    """
    if project.startswith("format-code-task-"):
        return None
    return project.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def spec_name(task_id: str, attempt: int) -> str:
    """Deterministic spec name: har120-<short>-a<attempt> (attempts 1-based)."""
    return f"har120-{task_id.removeprefix('format-code-task-')}-a{attempt}"


def load_proposal(path: Path) -> list[dict]:
    """Read the HAR-120 proposal CSV; refuse on drift from its contract."""
    if not path.is_file():
        raise SystemExit(f"refusing: proposal CSV is missing: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"task_id", "project", "run", "run_digest"}
    missing = required - set(rows[0].keys()) if rows else required
    if missing:
        raise SystemExit(f"refusing: proposal {path} lacks columns {sorted(missing)}")
    for row in rows:
        if row["run"] not in RUN_KINDS:
            raise SystemExit(f"refusing: proposal row {row['task_id']} has run={row['run']!r}")
        digest = row["run_digest"]
        if not (digest.startswith("sha256:") and len(digest) == len("sha256:") + 64):
            raise SystemExit(
                f"refusing: proposal row {row['task_id']} has bad run_digest {digest!r}"
            )
    if len({row["task_id"] for row in rows}) != len(rows):
        raise SystemExit(f"refusing: proposal {path} lists a task twice")
    return rows


def _eval_column(fieldnames: list[str], candidates: tuple[str, ...], what: str) -> str:
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    raise SystemExit(
        f"refusing: eval list has no {what} column {list(candidates)} "
        f"(has {fieldnames}); expected HAR-127 eval_tasks.csv format"
    )


def load_eval_list(path: Path) -> list[dict]:
    """Read Data's frozen G1 eval list; return rows with task id and repo key.

    Expected format is HAR-127's `eval_tasks.csv` (columns task, digest to
    run, repo, image MiB). The repo column is also accepted as `project` or
    `project_key`, and the task column as `task_id`, so the check survives
    header renames. A header-only (empty) list drops nothing.
    """
    if not path.is_file():
        raise SystemExit(
            f"refusing: eval list is missing: {path} "
            "(G2 starts only after Data freezes the G1 eval set)"
        )
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        raise SystemExit(f"refusing: eval list {path} has no header")
    task_col = _eval_column(fieldnames, ("task", "task_id"), "task")
    repo_col = _eval_column(fieldnames, ("repo", "project", "project_key"), "repo")
    eval_rows = []
    for row in rows:
        if not (row.get(task_col) or "").strip():
            continue
        eval_rows.append({"task": row[task_col].strip(), "repo_key": repo_key(row[repo_col])})
    return eval_rows


def select_tasks(proposal: list[dict], eval_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the proposal into kept tasks and contamination drops.

    A proposal task is dropped when its normalised repository matches any
    eval-list task's normalised repository (same `repo_key` convention the
    ledger proposal itself uses: last path segment, case- and `-`/`_`
    insensitive). Each drop records the eval task that forced it.
    """
    by_repo: dict[str, dict] = {}
    for eval_row in eval_rows:
        if eval_row["repo_key"] is not None:
            by_repo.setdefault(eval_row["repo_key"], eval_row)
    kept, dropped = [], []
    for row in proposal:
        key = repo_key(row["project"])
        match = by_repo.get(key) if key is not None else None
        if match is None:
            kept.append(row)
        else:
            dropped.append(
                {
                    "task_id": row["task_id"],
                    "project": row["project"],
                    "repo_key": key,
                    "reason": (
                        f"shares repository {match['repo_key']!r} with eval task {match['task']!r}"
                    ),
                }
            )
    return kept, dropped


def variant_record(task_id: str, run_digest: str) -> Path:
    """Committed variant record for a leak-closed / repaired proposal row."""
    short = run_digest.removeprefix("sha256:")[:12]
    return REPO / f"library/task-variants/mimo-v2.6-rl__{task_id}/{short}.json"


def _snapshot_tasks(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_dir():
            raise SystemExit(f"refusing: snapshot tasks dir is missing: {explicit}")
        return explicit
    candidates = [REPO / SNAPSHOT_TASKS]
    try:
        from evallab.storage.paths import shared_checkout_root  # noqa: E402

        primary = shared_checkout_root(REPO)
        if primary != REPO:
            candidates.append(primary / SNAPSHOT_TASKS)
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise SystemExit(
        "refusing: no task snapshot found; pass --snapshot "
        "(read-only copy source, e.g. the primary checkout's derived/task-store/hf/…/tasks)"
    )


def _route(base_spec: Path) -> dict:
    base = json.loads(base_spec.read_text())
    missing = [field for field in ROUTE_FIELDS if base.get(field) is None]
    if missing:
        raise SystemExit(f"refusing: base spec {base_spec} lacks {', '.join(missing)}")
    return {field: base[field] for field in ROUTE_FIELDS}


def _assert_staged(task_id: str, dest: Path, expected: str) -> tuple[str, str]:
    """Refuse unless the staged bytes match the proposal digest; return digests."""
    try:
        digests = compute_task_digests(dest)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"refusing: staged {task_id} unreadable at {dest}: {exc}") from exc
    if digests.package != expected:
        raise SystemExit(
            f"refusing: staged {task_id} digest drift: proposal {expected}, disk {digests.package}"
        )
    return digests.package, digests.verifier


def _make_writable(dest: Path) -> None:
    """Give the owner write permission over a staged copy (exec bits kept).

    The pinned snapshot is read-only, and copytree preserves that; without
    this, re-staging (rmtree) fails with PermissionError. This mirrors the
    task-variants convention (read-only parents, owner-writable copies).
    """
    for path in [dest, *dest.rglob("*")]:
        with suppress(OSError):
            os.chmod(path, path.stat().st_mode | stat.S_IWUSR)


def _tree(path: Path, digest: str | None) -> tuple[str, str]:
    """Validate the lf2 harness tree and return its repo-relative path and digest."""
    try:
        tree = load_harness_tree(path, digest, repo_root=REPO)
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(f"refusing: lf2 harness tree invalid: {exc}") from exc
    try:
        rel = tree.root.resolve().relative_to(REPO).as_posix()
    except ValueError:
        raise SystemExit(
            f"refusing: lf2 harness tree is outside the repo: {tree.root} "
            "(spec task/harness paths must be repo-relative)"
        ) from None
    return rel, tree.sha256


def _fresh_dest(dest: Path) -> Path:
    """A scratch sibling for atomic staging; the live dir is never mutated."""
    tmp = dest.parent / f".{dest.name}.tmp"
    if tmp.is_dir():
        _make_writable(tmp)
        shutil.rmtree(tmp)
    return tmp


def _publish(task_id: str, tmp: Path, dest: Path, expected: str) -> tuple[str, str]:
    """Verify the scratch copy, then swap it into place.

    The live dir is removed only with verified bytes in hand; a failure
    leaves it untouched, and a re-run restores anything lost mid-swap from
    the snapshot, so staging stays idempotent.
    """
    published = False
    try:
        _make_writable(tmp)
        digests = _assert_staged(task_id, tmp, expected)
        if dest.is_dir():
            _make_writable(dest)
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        published = True
        return digests
    finally:
        if not published:
            _make_writable(tmp)
            shutil.rmtree(tmp, ignore_errors=True)


def _stage_original(task_id: str, snapshot: Path, dest: Path, expected: str) -> tuple[str, str]:
    src = snapshot / task_id
    if not src.is_dir():
        raise SystemExit(f"refusing: {task_id} missing from snapshot {snapshot}")
    if dest.is_dir():
        try:
            if task_directory_digest(dest) == expected:
                return _assert_staged(task_id, dest, expected)
        except (ValueError, OSError):
            pass
    tmp = _fresh_dest(dest)
    shutil.copytree(src, tmp)
    return _publish(task_id, tmp, dest, expected)


def _stage_variant(task_id: str, snapshot: Path, dest: Path, expected: str) -> tuple[str, str]:
    record = variant_record(task_id, expected)
    if not record.is_file():
        raise SystemExit(f"refusing: variant record is missing: {record}")
    package_dir = materialize(record, snapshot / task_id, repo_root=REPO)
    if dest.is_dir():
        try:
            if task_directory_digest(dest) == expected:
                return _assert_staged(task_id, dest, expected)
        except (ValueError, OSError):
            pass
    tmp = _fresh_dest(dest)
    shutil.copytree(package_dir, tmp)
    return _publish(task_id, tmp, dest, expected)


def stage_tasks(kept: list[dict], snapshot: Path) -> dict[str, dict]:
    """Stage every kept task into tasks/; return staged dirs with digests."""
    TASKS.mkdir(exist_ok=True)
    staged: dict[str, dict] = {}
    for row in kept:
        task_id = row["task_id"]
        dest = TASKS / task_id
        if row["run"] == "original":
            package, verifier = _stage_original(task_id, snapshot, dest, row["run_digest"])
        else:
            package, verifier = _stage_variant(task_id, snapshot, dest, row["run_digest"])
        staged[task_id] = {"staged_dir": dest, "package_digest": package, "verifier": verifier}
    return staged


def _spec(
    name: str,
    task_id: str,
    task_rel: str,
    package_digest: str,
    verifier_digest: str,
    harness_rel: str,
    harness_digest: str,
    route: dict,
) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "hypothesis": (
            f"HAR-120/G2 data batch, task={task_id}, {name.rsplit('-a', 1)[1]} of "
            f"{ATTEMPTS_PER_TASK} attempts: plain terminus-2 student on the lf2 "
            "harness tree; two attempts per task find sometimes-solved tasks"
        ),
        "purpose": "comparison",
        "question_ref": QUESTION_REF,
        "task": task_rel,
        "task_path": task_rel,
        "task_id": task_id,
        "verifier_digest": verifier_digest,
        "task_package_digest": package_digest,
        "extra_instruction_path": None,
        "extra_instruction_sha256": None,
        "harness_tree_path": harness_rel,
        "harness_tree_sha256": harness_digest,
        "jobs_dir": "runs",
        "attempts": 1,
        "concurrency": 1,
        "submitted_by": "operator",
        "priority": 100,
        "requires": [],
        **route,
    }


def _write(out_dir: Path, name: str, spec: dict) -> None:
    (out_dir / f"{name}.json").write_text(json.dumps(spec, indent=2) + "\n")


def generate(
    kept: list[dict],
    staged: dict[str, dict],
    route: dict,
    tree: tuple[str, str],
    out_dir: Path,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    harness_rel, harness_digest = tree
    names: list[str] = []
    for row in kept:
        task_id = row["task_id"]
        info = staged[task_id]
        task_rel = info["staged_dir"].relative_to(REPO).as_posix()
        for attempt in range(1, ATTEMPTS_PER_TASK + 1):
            name = spec_name(task_id, attempt)
            _write(
                out_dir,
                name,
                _spec(
                    name,
                    task_id,
                    task_rel,
                    info["package_digest"],
                    info["verifier"],
                    harness_rel,
                    harness_digest,
                    route,
                ),
            )
            names.append(name)
    return names


def write_cohort(
    out_dir: Path,
    kept: list[dict],
    staged: dict[str, dict],
    dropped: list[dict],
    tree: tuple[str, str],
) -> None:
    cohort = [
        {
            "task_id": row["task_id"],
            "project": row["project"],
            "repo_key": repo_key(row["project"]),
            "run": row["run"],
            "package_digest": staged[row["task_id"]]["package_digest"],
            "verifier_digest": staged[row["task_id"]]["verifier"],
        }
        for row in kept
    ]
    (out_dir / "cohort.json").write_text(
        json.dumps(
            {
                "experiment": "HAR-120/G2 data batch",
                "harness_tree": {"path": tree[0], "sha256": tree[1]},
                "cohort": cohort,
                "dropped": dropped,
            },
            indent=2,
        )
        + "\n"
    )


def check(
    out_dir: Path,
    kept: list[dict],
    dropped: list[dict],
    route: dict,
    tree: tuple[str, str],
) -> int:
    """Validate generated specs without submitting (submit has no dry-run).

    Reuses the exact admission-time checks: read_spec (the pydantic model
    submit parses), digest-vs-disk (the queue's frozen-digest check), and
    load_harness_tree (the dispatch-time tree pin).
    """
    failures: list[str] = []
    harness_rel, harness_digest = tree
    expected: dict[str, dict] = {}
    for row in kept:
        task_rel = (TASKS / row["task_id"]).relative_to(REPO).as_posix()
        for attempt in range(1, ATTEMPTS_PER_TASK + 1):
            expected[spec_name(row["task_id"], attempt)] = {
                "task_rel": task_rel,
                "package_digest": row["run_digest"],
                "task_id": row["task_id"],
            }

    found = sorted(p.stem for p in out_dir.glob("har120-*.json"))
    if found != sorted(expected):
        failures.append(
            f"spec set mismatch: missing {sorted(set(expected) - set(found))}, "
            f"extra {sorted(set(found) - set(expected))}"
        )
    checked = 0
    for name, want in sorted(expected.items()):
        path = out_dir / f"{name}.json"
        if not path.is_file():
            continue
        try:
            spec = read_spec(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: read_spec refused: {exc}")
            continue
        for field in ROUTE_FIELDS:
            if getattr(spec, field) != route[field]:
                failures.append(f"{name}: route field {field} differs from base spec")
        if spec.question_ref != QUESTION_REF:
            failures.append(f"{name}: question_ref {spec.question_ref!r} != {QUESTION_REF!r}")
        if (spec.attempts, spec.concurrency) != (1, 1):
            failures.append(f"{name}: attempts/concurrency != 1/1")
        if spec.task_path != want["task_rel"] or spec.task != want["task_rel"]:
            failures.append(f"{name}: task path {spec.task_path!r} != {want['task_rel']!r}")
        if spec.task_package_digest != want["package_digest"]:
            failures.append(f"{name}: package digest != proposal")
        task_dir = REPO / want["task_rel"]
        if not task_dir.is_dir():
            failures.append(f"{name}: staged task dir is missing: {want['task_rel']}")
            continue
        try:
            digests = compute_task_digests(task_dir)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: digest computation refused: {exc}")
            continue
        if digests.package != spec.task_package_digest:
            failures.append(f"{name}: disk package digest != spec")
        if digests.verifier != spec.verifier_digest:
            failures.append(f"{name}: disk verifier digest != spec")
        checked += 1
    try:
        load_harness_tree(REPO / harness_rel, harness_digest, repo_root=REPO)
    except (ValueError, FileNotFoundError) as exc:
        failures.append(f"harness tree pin refused: {exc}")
    cohort_path = out_dir / "cohort.json"
    if cohort_path.is_file():
        cohort_doc = json.loads(cohort_path.read_text())
        if len(cohort_doc.get("cohort", [])) != len(kept):
            failures.append(
                f"cohort has {len(cohort_doc.get('cohort', []))} entries, want {len(kept)}"
            )
        if len(cohort_doc.get("dropped", [])) != len(dropped):
            failures.append(
                f"dropped has {len(cohort_doc.get('dropped', []))} entries, want {len(dropped)}"
            )
        if cohort_doc.get("harness_tree", {}).get("sha256") != harness_digest:
            failures.append("cohort harness digest != tree digest")
    else:
        failures.append("cohort.json is missing")
    if failures:
        print(f"CHECK FAILED ({checked} specs read):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"check ok: {checked} specs + cohort.json in {out_dir}")
    print(
        f"route {route['agent']} + {route['model']} on {route['environment']}; "
        f"per-trial est ${route['est_cost_usd']:.2f}; "
        f"total est ${checked * route['est_cost_usd']:.2f}"
    )
    return 0


def _report_drops(dropped: list[dict]) -> None:
    if not dropped:
        print("drop check: 0 of the proposal tasks share a repository with the eval list")
        return
    print(f"drop check: {len(dropped)} proposal task(s) share a repository with the eval list:")
    for drop in dropped:
        print(f"  - {drop['task_id']} ({drop['project']}): {drop['reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=None, help="lf2 harness tree path")
    parser.add_argument("--tree-digest", default=None, help="pinned lf2 tree digest")
    parser.add_argument(
        "--eval-list",
        type=Path,
        default=None,
        help="frozen G1 eval list CSV (HAR-127 eval_tasks.csv)",
    )
    parser.add_argument("--proposal", type=Path, default=PROPOSAL_PATH)
    parser.add_argument("--base-spec", type=Path, default=BASE_SPEC)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=EXP / "specs")
    parser.add_argument(
        "--check",
        type=Path,
        default=None,
        metavar="SPECS_DIR",
        help="validate specs in SPECS_DIR without staging or writing",
    )
    args = parser.parse_args(argv)

    if args.tree is None or args.tree_digest is None:
        raise SystemExit("refusing: --tree and --tree-digest are required (lf2, frozen by G0)")
    if args.eval_list is None:
        raise SystemExit(
            "refusing: --eval-list is required (Data's frozen G1 eval list; "
            "G2 starts only after it is committed)"
        )
    proposal = load_proposal(args.proposal)
    eval_rows = load_eval_list(args.eval_list)
    kept, dropped = select_tasks(proposal, eval_rows)
    route = _route(args.base_spec)
    tree = _tree(args.tree, args.tree_digest)

    if args.check is not None:
        _report_drops(dropped)
        return check(args.check, kept, dropped, route, tree)

    if not kept:
        raise SystemExit("refusing: the eval list drops every proposal task; nothing to run")
    snapshot = _snapshot_tasks(args.snapshot)
    staged = stage_tasks(kept, snapshot)
    names = generate(kept, staged, route, tree, args.out_dir)
    write_cohort(args.out_dir, kept, staged, dropped, tree)
    print(f"proposal: {len(proposal)} tasks; kept {len(kept)}, dropped {len(dropped)}")
    _report_drops(dropped)
    print(f"wrote {len(names)} specs + cohort.json to {args.out_dir}")
    print(
        f"route {route['agent']} + {route['model']} on {route['environment']}; "
        f"per-trial est ${route['est_cost_usd']:.2f}; total est ${len(names) * route['est_cost_usd']:.2f}"
    )
    return check(args.out_dir, kept, dropped, route, tree)


if __name__ == "__main__":
    raise SystemExit(main())
