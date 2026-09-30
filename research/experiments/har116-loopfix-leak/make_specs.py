#!/usr/bin/env python3
"""Generate HAR-116 loop-fix + leak-study eval specs.

Part A (loop fix, 20 specs): the 10 HAR-110 v2 tasks (dev 000383, 002256,
002391, 001832, 001896, 002864; held-out 001161, 000495, 001181, 000587),
each in two arms -- plain Terminus on the fresh baseline harness tree vs
plain Terminus on the loop-fix tree. Both arms of a task run the SAME task
bytes: the HAR-113 leak-closed variant where one exists (002256, 002864),
else the original.

Part B (leak study, 10 specs): 5 tasks x {original (PyPI open),
leak-closed variant}, both on the baseline tree.

The route, environment and per-trial limits come from the SAME retained
HAR-110 base spec the student runs use (--base-spec); only the harness
tree (Part A arms) and the task bytes (Part B arms) vary. Task sides and
digests come from the committed tasks.json manifest, which is the single
source of truth; staging and generation refuse on any drift.

Task bytes are staged into the gitignored tasks/ dir: originals are copied
from the pinned read-only snapshot, variants are rebuilt with
evallab.task_variants.materialize (which proves the record invariant) and
then copied. Nothing under tasks/ is committed.

Usage (from the worktree root):
  uv run --no-sync python research/experiments/har116-loopfix-leak/make_specs.py \\
    --baseline-tree <path> --baseline-digest sha256:<64hex> \\
    --loopfix-tree <path> --loopfix-digest sha256:<64hex>
  uv run --no-sync python research/experiments/har116-loopfix-leak/make_specs.py --check <specs-dir>

Submission via `evallab submit` parks specs in queue/waiting/ (runtime
state, never approved here). After submit, record the queue IDs in
<out-dir>/ids.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from contextlib import suppress
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/har116-loopfix-leak"
MANIFEST_PATH = EXP / "tasks.json"
BASE_SPEC = (
    REPO
    / "research/experiments/har110-python-gepa/base-specs/student-terminus2-selfhosted-python.json"
)
TASKS = EXP / "tasks"
SNAPSHOT_TASKS = "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
QUESTION_REF = "har116-loopfix-leak"

sys.path.insert(0, str(REPO / "src"))
from evallab.queue import read_spec  # noqa: E402
from evallab.registry import compute_task_digests, task_directory_digest  # noqa: E402
from evallab.task_variants import materialize  # noqa: E402
from evallab.terminus_harness import load_harness_tree  # noqa: E402

# Fields copied verbatim from the retained HAR-110 base spec, minus the
# harness tree (which is the Part A treatment, passed per arm on the CLI).
# Same rationale as HAR-110's ROUTE_FIELDS: override_storage_mb is included
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

PART_A_ARMS = ("baseline", "loopfix")
PART_B_ARMS = ("original", "leakclosed")


def rank_pool(pool_ids: list[str], salt: str) -> list[str]:
    """Deterministic order for the Part B pool (same convention as HAR-110)."""
    return sorted(pool_ids, key=lambda tid: hashlib.sha256(f"{salt}:{tid}".encode()).hexdigest())


def select_part_b(
    census_rows: list[dict],
    *,
    part_a: list[str],
    forced: list[str],
    salt: str,
    take: int = 3,
) -> tuple[list[str], list[str]]:
    """Pick the Part B hash-selected tasks from census rows.

    Eligible: label sound AND leak_channel pypi_fix_released, minus Part A
    tasks, minus the forced includes. Returns (eligible_pool, selected).
    """
    part_a_set = set(part_a)
    forced_set = set(forced)
    pool = sorted(
        row["task_id"]
        for row in census_rows
        if row.get("label") == "sound"
        and row.get("leak_channel") == "pypi_fix_released"
        and row["task_id"] not in part_a_set
        and row["task_id"] not in forced_set
    )
    return pool, rank_pool(pool, salt)[:take]


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


def _assert_staged(task_id: str, dest: Path, package_digest: str, verifier_digest: str) -> None:
    """Refuse unless the staged bytes match the manifest on both digests."""
    try:
        digests = compute_task_digests(dest)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"refusing: staged {task_id} unreadable at {dest}: {exc}") from exc
    if digests.package != package_digest or digests.verifier != verifier_digest:
        listing = []
        for path in sorted(dest.rglob("*")):
            if path.is_symlink():
                listing.append(f"link {path.relative_to(dest).as_posix()}")
            elif path.is_file():
                listing.append(
                    f"file {path.relative_to(dest).as_posix()} "
                    f"{path.stat().st_size} {hashlib.sha256(path.read_bytes()).hexdigest()[:12]}"
                )
        raise SystemExit(
            f"refusing: staged {task_id} digest drift: manifest "
            f"{package_digest}/{verifier_digest}, disk {digests.package}/{digests.verifier}\n"
            + "\n".join(f"  {line}" for line in listing)
        )


def _make_writable(dest: Path) -> None:
    """Give the owner write permission over a staged copy (exec bits kept).

    The pinned snapshot is read-only, and copytree preserves that; without
    this, re-staging (rmtree) fails with PermissionError. This mirrors the
    task-variants convention (read-only parents, owner-writable copies).
    """
    for path in [dest, *dest.rglob("*")]:
        with suppress(OSError):
            os.chmod(path, path.stat().st_mode | stat.S_IWUSR)


def _tree(name: str, path: Path, digest: str | None) -> tuple[str, str]:
    """Validate a harness tree and return its repo-relative path and digest."""
    try:
        tree = load_harness_tree(path, digest, repo_root=REPO)
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(f"refusing: {name} harness tree invalid: {exc}") from exc
    try:
        rel = tree.root.resolve().relative_to(REPO).as_posix()
    except ValueError:
        raise SystemExit(
            f"refusing: {name} harness tree is outside the repo: {tree.root} "
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


def _publish(task_id: str, tmp: Path, dest: Path, expected: str, verifier: str) -> None:
    """Verify the scratch copy, then swap it into place.

    The live dir is removed only with verified bytes in hand; a failure
    leaves it untouched, and a re-run restores anything lost mid-swap from
    the snapshot, so staging stays idempotent.
    """
    published = False
    try:
        _make_writable(tmp)
        _assert_staged(task_id, tmp, expected, verifier)
        if dest.is_dir():
            _make_writable(dest)
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        published = True
    finally:
        if not published:
            _make_writable(tmp)
            shutil.rmtree(tmp, ignore_errors=True)


def _stage_original(task_id: str, snapshot: Path, dest: Path, expected: str, verifier: str) -> None:
    src = snapshot / task_id
    if not src.is_dir():
        raise SystemExit(f"refusing: {task_id} missing from snapshot {snapshot}")
    if dest.is_dir():
        try:
            if task_directory_digest(dest) == expected:
                _assert_staged(task_id, dest, expected, verifier)
                _make_writable(dest)
                return
        except (ValueError, OSError):
            pass
    tmp = _fresh_dest(dest)
    shutil.copytree(src, tmp)
    _publish(task_id, tmp, dest, expected, verifier)


def _stage_variant(
    task_id: str, record_rel: str, snapshot: Path, dest: Path, expected: str, verifier: str
) -> None:
    record = REPO / record_rel
    if not record.is_file():
        raise SystemExit(f"refusing: variant record is missing: {record}")
    package_dir = materialize(record, snapshot / task_id, repo_root=REPO)
    if dest.is_dir():
        try:
            if task_directory_digest(dest) == expected:
                _assert_staged(task_id, dest, expected, verifier)
                _make_writable(dest)
                return
        except (ValueError, OSError):
            pass
    tmp = _fresh_dest(dest)
    shutil.copytree(package_dir, tmp)
    _publish(task_id, tmp, dest, expected, verifier)


def _dest(manifest_dir: str) -> Path:
    """Manifest task dirs are relative to the experiment dir, not the repo."""
    return EXP / manifest_dir


def _repo_rel(manifest_dir: str) -> str:
    """Repo-relative path a spec records for a staged task dir."""
    return _dest(manifest_dir).relative_to(REPO).as_posix()


def stage_tasks(manifest: dict, snapshot: Path) -> dict[str, dict]:
    """Stage every manifest side into tasks/; return staged dirs with digests."""
    TASKS.mkdir(exist_ok=True)
    staged: dict[str, dict] = {}
    for entry in manifest["part_a"]:
        dest = _dest(entry["staged_dir"])
        if entry["uses"] == "variant":
            _stage_variant(
                entry["task"],
                entry["variant_record"],
                snapshot,
                dest,
                entry["package_digest"],
                entry["verifier_digest"],
            )
        elif entry["uses"] == "original":
            _stage_original(
                entry["task"], snapshot, dest, entry["package_digest"], entry["verifier_digest"]
            )
        else:
            raise SystemExit(f"refusing: unknown side {entry['uses']} for {entry['task']}")
        staged[entry["staged_dir"]] = entry
    for entry in manifest["part_b"]["tasks"]:
        _stage_original(
            entry["task"],
            snapshot,
            _dest(entry["original_dir"]),
            entry["original_digest"],
            entry["original_verifier"],
        )
        staged[entry["original_dir"]] = entry
        _stage_variant(
            entry["task"],
            entry["variant_record"],
            snapshot,
            _dest(entry["variant_dir"]),
            entry["variant_digest"],
            entry["variant_verifier"],
        )
        staged[entry["variant_dir"]] = entry
    return staged


def _spec(
    name: str,
    hypothesis: str,
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
        "hypothesis": hypothesis,
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
    manifest: dict,
    route: dict,
    trees: dict[str, tuple[str, str]],
    out_dir: Path,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    cohort: list[dict] = []
    for entry in manifest["part_a"]:
        short = entry["task"].removeprefix("format-code-task-")
        for arm in PART_A_ARMS:
            harness_rel, harness_digest = trees[arm]
            name = f"har116-a-{short}-{arm}"
            other = "loop-fix" if arm == "baseline" else "baseline"
            _write(
                out_dir,
                name,
                _spec(
                    name,
                    f"HAR-116 Part A loop-fix trial, task={entry['task']}, arm={arm}: "
                    f"plain terminus-2 student on the {arm} harness tree "
                    f"({entry['uses']} task bytes); arms differ only in the harness tree, "
                    f"paired against {other}",
                    entry["task"],
                    _repo_rel(entry["staged_dir"]),
                    entry["package_digest"],
                    entry["verifier_digest"],
                    harness_rel,
                    harness_digest,
                    route,
                ),
            )
            names.append(name)
        cohort.append(
            {
                "task_id": entry["task"],
                "part": "A",
                "side": entry["uses"],
                "package_digest": entry["package_digest"],
            }
        )
    for entry in manifest["part_b"]["tasks"]:
        short = entry["task"].removeprefix("format-code-task-")
        sides = (
            (
                "original",
                _repo_rel(entry["original_dir"]),
                entry["original_digest"],
                entry["original_verifier"],
            ),
            (
                "leakclosed",
                _repo_rel(entry["variant_dir"]),
                entry["variant_digest"],
                entry["variant_verifier"],
            ),
        )
        for arm, task_rel, package_digest, verifier_digest in sides:
            harness_rel, harness_digest = trees["baseline"]
            name = f"har116-b-{short}-{arm}"
            other = "leak-closed variant" if arm == "original" else "original (PyPI open)"
            _write(
                out_dir,
                name,
                _spec(
                    name,
                    f"HAR-116 Part B leak-study trial, task={entry['task']}, arm={arm}: "
                    f"plain terminus-2 student on the baseline harness tree with {other} "
                    f"paired against {other}; arms differ only in the task bytes",
                    entry["task"],
                    task_rel,
                    package_digest,
                    verifier_digest,
                    harness_rel,
                    harness_digest,
                    route,
                ),
            )
            names.append(name)
        cohort.append(
            {
                "task_id": entry["task"],
                "part": "B",
                "original_digest": entry["original_digest"],
                "variant_digest": entry["variant_digest"],
                "variant_status": entry["variant_status"],
            }
        )
    (out_dir / "cohort.json").write_text(
        json.dumps({"experiment": "HAR-116 loop-fix + leak study", "cohort": cohort}, indent=2)
        + "\n"
    )
    return names


def check(out_dir: Path, manifest: dict, route: dict, trees: dict[str, tuple[str, str]]) -> int:
    """Validate generated specs without submitting (submit has no dry-run).

    Reuses the exact admission-time checks: read_spec (the pydantic model
    submit parses), digest-vs-disk (the queue's frozen-digest check), and
    load_harness_tree (the dispatch-time tree pin).
    """
    failures: list[str] = []
    expected: dict[str, dict] = {}
    for entry in manifest["part_a"]:
        short = entry["task"].removeprefix("format-code-task-")
        for arm in PART_A_ARMS:
            harness_rel, harness_digest = trees[arm]
            expected[f"har116-a-{short}-{arm}"] = {
                "task_rel": _repo_rel(entry["staged_dir"]),
                "package_digest": entry["package_digest"],
                "verifier_digest": entry["verifier_digest"],
                "harness_rel": harness_rel,
                "harness_digest": harness_digest,
            }
    for entry in manifest["part_b"]["tasks"]:
        short = entry["task"].removeprefix("format-code-task-")
        expected[f"har116-b-{short}-original"] = {
            "task_rel": _repo_rel(entry["original_dir"]),
            "package_digest": entry["original_digest"],
            "verifier_digest": entry["original_verifier"],
            "harness_rel": trees["baseline"][0],
            "harness_digest": trees["baseline"][1],
        }
        expected[f"har116-b-{short}-leakclosed"] = {
            "task_rel": _repo_rel(entry["variant_dir"]),
            "package_digest": entry["variant_digest"],
            "verifier_digest": entry["variant_verifier"],
            "harness_rel": trees["baseline"][0],
            "harness_digest": trees["baseline"][1],
        }

    found = sorted(p.stem for p in out_dir.glob("har116-*.json"))
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
            failures.append(f"{name}: package digest != manifest")
        if spec.verifier_digest != want["verifier_digest"]:
            failures.append(f"{name}: verifier digest != manifest")
        if spec.harness_tree_path != want["harness_rel"]:
            failures.append(f"{name}: harness path != {want['harness_rel']!r}")
        if spec.harness_tree_sha256 != want["harness_digest"]:
            failures.append(f"{name}: harness digest != {want['harness_digest']}")
        task_dir = REPO / want["task_rel"]
        if not task_dir.is_dir():
            failures.append(f"{name}: staged task dir is missing: {want['task_rel']}")
            continue
        try:
            digests = compute_task_digests(task_dir)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: digest computation refused: {exc}")
            continue
        if digests.package != want["package_digest"]:
            failures.append(f"{name}: disk package digest != spec")
        if digests.verifier != want["verifier_digest"]:
            failures.append(f"{name}: disk verifier digest != spec")
        checked += 1
    try:
        load_harness_tree(REPO / trees["baseline"][0], trees["baseline"][1], repo_root=REPO)
        load_harness_tree(REPO / trees["loopfix"][0], trees["loopfix"][1], repo_root=REPO)
    except (ValueError, FileNotFoundError) as exc:
        failures.append(f"harness tree pin refused: {exc}")
    cohort_path = out_dir / "cohort.json"
    if cohort_path.is_file():
        cohort = json.loads(cohort_path.read_text())["cohort"]
        if len(cohort) != 15:
            failures.append(f"cohort has {len(cohort)} entries, want 15")
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


def _census_rows() -> list[dict]:
    import pyarrow.parquet as pq  # noqa: E402

    census_path = REPO / "research/experiments/har108-python-census/task_health.parquet"
    if not census_path.is_file():
        raise SystemExit(f"refusing: census is missing: {census_path}")
    return pq.read_table(census_path).to_pylist()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-tree", type=Path, default=None)
    parser.add_argument("--baseline-digest", default=None)
    parser.add_argument("--loopfix-tree", type=Path, default=None)
    parser.add_argument("--loopfix-digest", default=None)
    parser.add_argument("--base-spec", type=Path, default=BASE_SPEC)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
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

    manifest = json.loads(args.manifest.read_text())
    route = _route(args.base_spec)

    if args.check is not None:
        if args.baseline_tree is None or args.loopfix_tree is None:
            raise SystemExit("refusing: --check needs the same --baseline/--loopfix tree args")
        trees = {
            "baseline": _tree("baseline", args.baseline_tree, args.baseline_digest),
            "loopfix": _tree("loopfix", args.loopfix_tree, args.loopfix_digest),
        }
        return check(args.check, manifest, route, trees)

    for flag in ("baseline_tree", "baseline_digest", "loopfix_tree", "loopfix_digest"):
        if getattr(args, flag) is None:
            raise SystemExit(f"refusing: --{flag.replace('_', '-')} is required to generate")
    trees = {
        "baseline": _tree("baseline", args.baseline_tree, args.baseline_digest),
        "loopfix": _tree("loopfix", args.loopfix_tree, args.loopfix_digest),
    }
    if trees["baseline"] == trees["loopfix"]:
        print("warning: baseline and loopfix trees are identical (placeholder proof only)")

    # The Part B selection re-derives from the census on every run and must
    # match the manifest; adopting a new selection is a manifest edit, not
    # silent drift.
    part_b = manifest["part_b"]
    pool, selected = select_part_b(
        _census_rows(),
        part_a=[entry["task"] for entry in manifest["part_a"]],
        forced=part_b["forced"],
        salt=part_b["salt"],
    )
    if selected != part_b["selected"]:
        raise SystemExit(
            f"refusing: census-derived Part B selection {selected} != manifest {part_b['selected']}"
        )
    print(f"part B pool: {len(pool)} eligible; selected: {', '.join(selected)}")

    snapshot = _snapshot_tasks(args.snapshot)
    stage_tasks(manifest, snapshot)
    names = generate(manifest, route, trees, args.out_dir)
    print(f"wrote {len(names)} specs + cohort.json to {args.out_dir}")
    print(
        f"route {route['agent']} + {route['model']} on {route['environment']}; "
        f"per-trial est ${route['est_cost_usd']:.2f}; total est ${len(names) * route['est_cost_usd']:.2f}"
    )
    return check(args.out_dir, manifest, route, trees)


if __name__ == "__main__":
    raise SystemExit(main())
