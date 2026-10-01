#!/usr/bin/env python3
"""Generate OVN G5 paired-eval specs (gate G5 of HAR-126).

Three arms on Data's frozen 20 eval tasks
(``research/experiments/ovn-sft-v0/eval_tasks.csv``, v2,
sha256 ``3b997fdcff048061fd8a05d446948d4d6425bf670eb0d6710989c50dcd0a9219``):

- ``stock``: ``selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B``;
- ``tuned``: that base plus the frozen G4 LoRA adapter, requested as
  ``selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129`` (adapter route
  merged in #604);
- ``gepa`` (optional): stock weights plus HAR-135's candidate addendum,
  passed as ``--gepa-candidate <path> --gepa-sha256 sha256:<hex>`` and
  recorded as ``extra_instruction_path`` / ``extra_instruction_sha256``.

Everything else is identical across arms: the frozen lf2 harness tree
(sha256 ``f18091f3…``), the 120-call / 2.5M-input-token limits, the same
base-spec route fields, and the same per-task digests. Arm execution order
alternates per task following PREREG's fixed six-row cycle and is recorded
in ``cohort.json`` (filenames alone do not enforce the schedule; the
operator ticks position waves serially per task).

Task bytes are staged into the gitignored ``tasks/`` dir exactly like G2:
originals are copied from the pinned read-only snapshot, leak-closed /
repair sides are rebuilt with ``evallab.task_variants.materialize``.
Nothing under ``tasks/`` or the output specs dir is committed.

Usage (from the worktree root):
  uv run --no-sync python research/experiments/ovn-sft-v0/make_g5_specs.py \\
    --tree <LF2_TREE> --tree-digest sha256:<64hex> \\
    [--gepa-candidate <path> --gepa-sha256 sha256:<hex>] \\
    [--out-dir /tmp/ovn-g5-proof]
  uv run --no-sync python research/experiments/ovn-sft-v0/make_g5_specs.py \\
    --check <specs-dir> --tree <path> --tree-digest sha256:<64hex> \\
    [--gepa-candidate <path> --gepa-sha256 sha256:<hex>]

Submission via `evallab submit` parks specs in queue/waiting/ (runtime
state, never approved here). Do NOT submit from this prep.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import sys
from contextlib import suppress
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / "research/experiments/ovn-sft-v0"
EVAL_CSV = EXP / "eval_tasks.csv"
EVAL_SHA256 = "3b997fdcff048061fd8a05d446948d4d6425bf670eb0d6710989c50dcd0a9219"
EVAL_TASKS = 20
BASE_SPEC = (
    REPO
    / "research/experiments/har110-python-gepa/base-specs/student-terminus2-selfhosted-python.json"
)
TASKS = EXP / "tasks"
SNAPSHOT_TASKS = "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
QUESTION_REF = "ovn-g5"
LINEAR_CARD = "HAR-126"

STOCK_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
TUNED_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129"
ARMS_2 = ("stock", "tuned")
ARMS_3 = ("stock", "tuned", "gepa")

# Measured per-trial cost basis (Research-Harbor 08:42Z ruling on HAR-126):
# HAR-116 ran $4.81 for 40 trials (~$0.12 each) and G2 wave 1 ran ~$2.4 for
# 20 (~$0.12 each); $0.35 is ~3x that measured cost. The base spec's $1.85
# worst-case estimate would trip the daily ceiling, so every G5 spec carries
# $0.35 with this basis recorded in its hypothesis and in cohort.json.
EST_COST_USD = 0.35
EST_BASIS = (
    "est $0.35/trial per Research-Harbor 08:42Z ruling "
    "(measured ~$0.12: HAR-116 $4.81/40, G2 wave-1 ~$2.4/20; ~3x margin)"
)

LF2_DIGEST_PREFIX = "sha256:f18091f3"

sys.path.insert(0, str(REPO / "src"))
from evallab.queue import read_spec  # noqa: E402
from evallab.registry import compute_task_digests, task_directory_digest  # noqa: E402
from evallab.task_variants import materialize  # noqa: E402
from evallab.terminus_harness import load_harness_tree  # noqa: E402

# Fields copied verbatim from the retained HAR-110 base spec, minus the
# harness tree (the lf2 treatment, passed on the CLI) and minus model /
# est_cost_usd (the arm treatments, set here). Same rationale as HAR-110's
# ROUTE_FIELDS: override_storage_mb is included because these code tasks
# declare no storage_mb; keep the retained round's explicit allocation instead
# of relying on a server-selected storage default.
ROUTE_FIELDS = (
    "agent",
    "environment",
    "timeout_seconds",
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "cost_limit_usd",
    "override_storage_mb",
)

RUN_KINDS = ("original", "leak-closed", "repair")

# Within one task, arms may differ ONLY in these spec fields. Name and
# hypothesis carry the arm identity; model selects base vs adapter; the
# GEPA arm alone carries the frozen addendum. Anything else drifting
# between arms of the same task (limits, tree, digests, …) fails --check.
ALLOWED_ARM_DIFF = frozenset(
    {
        "name",
        "hypothesis",
        "model",
        "extra_instruction_path",
        "extra_instruction_sha256",
    }
)


def spec_name(task_id: str, arm: str) -> str:
    """Deterministic spec name: ovn-g5-<short>-<arm>."""
    return f"ovn-g5-{task_id.removeprefix('format-code-task-')}-{arm}"


def arm_order(row_1based: int, three_arms: bool) -> list[str]:
    """Per-task arm execution order from PREREG section 3.

    Three arms repeat the fixed six-row cycle through the 20 CSV rows
    (retains alternating stock/tuned order, balances every arm across
    positions). Two arms (no GEPA candidate admitted) alternate stock
    first on odd rows, tuned first on even rows.
    """
    if not three_arms:
        return ["stock", "tuned"] if row_1based % 2 == 1 else ["tuned", "stock"]
    mod = row_1based % 6
    return {
        1: ["stock", "tuned", "gepa"],
        2: ["tuned", "gepa", "stock"],
        3: ["gepa", "stock", "tuned"],
        4: ["gepa", "tuned", "stock"],
        5: ["stock", "gepa", "tuned"],
        0: ["tuned", "stock", "gepa"],
    }[mod]


def load_eval_list(path: Path) -> list[dict]:
    """Read Data's frozen G1 eval list; refuse on drift from its contract."""
    if not path.is_file():
        raise SystemExit(f"refusing: eval list is missing: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != EVAL_SHA256:
        raise SystemExit(f"refusing: eval list {path} sha256 {actual} != frozen v2 {EVAL_SHA256}")
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    required = {"task", "digest", "run", "repo", "image_mib"}
    missing = required - set(fieldnames)
    if missing:
        raise SystemExit(f"refusing: eval list {path} lacks columns {sorted(missing)}")
    if len(rows) != EVAL_TASKS:
        raise SystemExit(f"refusing: eval list {path} has {len(rows)} rows, want {EVAL_TASKS}")
    kept = []
    for row in rows:
        if row["run"] not in RUN_KINDS:
            raise SystemExit(f"refusing: eval row {row['task']} has run={row['run']!r}")
        digest = row["digest"]
        if not (digest.startswith("sha256:") and len(digest) == len("sha256:") + 64):
            raise SystemExit(f"refusing: eval row {row['task']} has bad digest {digest!r}")
        kept.append(
            {
                "task_id": row["task"].strip(),
                "run_digest": digest,
                "run": row["run"],
                "repo": row["repo"],
            }
        )
    if len({row["task_id"] for row in kept}) != len(kept):
        raise SystemExit(f"refusing: eval list {path} lists a task twice")
    return kept


def variant_record(task_id: str, run_digest: str) -> Path:
    """Committed variant record for a leak-closed / repaired eval row."""
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
    if base.get("agent") != "terminus-2":
        raise SystemExit(f"refusing: base spec agent {base.get('agent')!r} != 'terminus-2'")
    route = {field: base[field] for field in ROUTE_FIELDS}
    if route["max_requests"] != 120:
        raise SystemExit(
            f"refusing: base spec max_requests {route['max_requests']} != 120 (PREREG)"
        )
    if route["max_input_tokens"] != 2500000:
        raise SystemExit(
            f"refusing: base spec max_input_tokens {route['max_input_tokens']} != 2500000 (PREREG)"
        )
    return route


def _gepa(path: Path | None, sha: str | None) -> tuple[str | None, str | None]:
    """Validate the optional HAR-135 candidate addendum; return repo-rel path + sha."""
    if path is None and sha is None:
        return None, None
    if path is None or sha is None:
        raise SystemExit(
            "refusing: --gepa-candidate and --gepa-sha256 are required together "
            "(or neither, for the two-arm fallback)"
        )
    if not (sha.startswith("sha256:") and len(sha) == len("sha256:") + 64):
        raise SystemExit(f"refusing: bad --gepa-sha256 {sha!r}")
    if not path.is_file():
        raise SystemExit(f"refusing: GEPA candidate file is missing: {path}")
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != sha:
        raise SystemExit(f"refusing: GEPA candidate digest drift: flag {sha}, disk {actual}")
    try:
        rel = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        # Placeholder proof files under /tmp are never submitted; the real
        # candidate must live in the repo so the queue can jail it.
        rel = str(path)
    return rel, sha


def _assert_staged(task_id: str, dest: Path, expected: str) -> tuple[str, str]:
    """Refuse unless the staged bytes match the eval digest; return digests."""
    try:
        digests = compute_task_digests(dest)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"refusing: staged {task_id} unreadable at {dest}: {exc}") from exc
    if digests.package != expected:
        raise SystemExit(
            f"refusing: staged {task_id} digest drift: eval {expected}, disk {digests.package}"
        )
    return digests.package, digests.verifier


def _make_writable(dest: Path) -> None:
    """Give the owner write permission over a staged copy (exec bits kept)."""
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
    """Verify the scratch copy, then swap it into place."""
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
    """Stage every eval task into tasks/; return staged dirs with digests."""
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


def _hypothesis(task_id: str, arm: str, order: list[str]) -> str:
    desc = {
        "stock": "stock distill on the lf2 harness tree (base model name, no adapter)",
        "tuned": "LoRA-SFT distill on the lf2 harness tree (adapter :har129)",
        "gepa": "stock weights plus the frozen HAR-135 prompt addendum on lf2",
    }[arm]
    return (
        f"OVN G5 paired eval, task={task_id}, arm={arm}: {desc}; "
        f"task order {'->'.join(order)}; {EST_BASIS}"
    )


def _spec(
    name: str,
    task_id: str,
    task_rel: str,
    package_digest: str,
    verifier_digest: str,
    harness_rel: str,
    harness_digest: str,
    route: dict,
    arm: str,
    order: list[str],
    gepa_path: str | None,
    gepa_sha: str | None,
) -> dict:
    model = TUNED_MODEL if arm == "tuned" else STOCK_MODEL
    extra_path = gepa_path if arm == "gepa" else None
    extra_sha = gepa_sha if arm == "gepa" else None
    return {
        "schema_version": 1,
        "name": name,
        "hypothesis": _hypothesis(task_id, arm, order),
        "purpose": "comparison",
        "question_ref": QUESTION_REF,
        "linear_card": LINEAR_CARD,
        "agent": "terminus-2",
        "task": task_rel,
        "task_path": task_rel,
        "task_id": task_id,
        "verifier_digest": verifier_digest,
        "task_package_digest": package_digest,
        "extra_instruction_path": extra_path,
        "extra_instruction_sha256": extra_sha,
        "harness_tree_path": harness_rel,
        "harness_tree_sha256": harness_digest,
        "jobs_dir": "runs",
        "attempts": 1,
        "concurrency": 1,
        "submitted_by": "operator",
        "priority": 100,
        "requires": [],
        "model": model,
        "est_cost_usd": EST_COST_USD,
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
    arms: tuple[str, ...],
    gepa_path: str | None,
    gepa_sha: str | None,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    harness_rel, harness_digest = tree
    names: list[str] = []
    three = "gepa" in arms
    for index, row in enumerate(kept, start=1):
        task_id = row["task_id"]
        info = staged[task_id]
        task_rel = info["staged_dir"].relative_to(REPO).as_posix()
        order = arm_order(index, three)
        for arm in arms:
            name = spec_name(task_id, arm)
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
                    arm,
                    order,
                    gepa_path,
                    gepa_sha,
                ),
            )
            names.append(name)
    return names


def write_cohort(
    out_dir: Path,
    kept: list[dict],
    staged: dict[str, dict],
    tree: tuple[str, str],
    arms: tuple[str, ...],
    gepa_path: str | None,
    gepa_sha: str | None,
) -> None:
    three = "gepa" in arms
    cohort = []
    for index, row in enumerate(kept, start=1):
        task_id = row["task_id"]
        cohort.append(
            {
                "task_id": task_id,
                "repo": row["repo"],
                "run": row["run"],
                "package_digest": staged[task_id]["package_digest"],
                "verifier_digest": staged[task_id]["verifier"],
                "order": arm_order(index, three),
            }
        )
    (out_dir / "cohort.json").write_text(
        json.dumps(
            {
                "experiment": "OVN G5 paired eval (PREREG ovn-sft-v0)",
                "eval_list": {
                    "path": "research/experiments/ovn-sft-v0/eval_tasks.csv",
                    "sha256": EVAL_SHA256,
                },
                "harness_tree": {"path": tree[0], "sha256": tree[1]},
                "arms": list(arms),
                "models": {
                    "stock": STOCK_MODEL,
                    "tuned": TUNED_MODEL,
                    "gepa": STOCK_MODEL,
                },
                "gepa_candidate": {"path": gepa_path, "sha256": gepa_sha},
                "est_cost_usd": EST_COST_USD,
                "est_basis": EST_BASIS,
                "cohort": cohort,
            },
            indent=2,
        )
        + "\n"
    )


def check(
    out_dir: Path,
    kept: list[dict],
    route: dict,
    tree: tuple[str, str],
    arms: tuple[str, ...],
    gepa_path: str | None,
    gepa_sha: str | None,
) -> int:
    """Validate generated specs without submitting (submit has no dry-run).

    Reuses the exact admission-time checks: read_spec (the pydantic model
    submit parses), digest-vs-disk (the queue's frozen-digest check), and
    load_harness_tree (the dispatch-time tree pin). Every spec must match
    its eval row (task_id, task path, run_digest) and the --tree /
    --tree-digest arguments. Arms of one task must differ ONLY in model,
    extra_instruction_path/sha256 and name/hypothesis.
    """
    failures: list[str] = []
    harness_rel, harness_digest = tree
    three = "gepa" in arms
    expected: dict[str, dict] = {}
    for _index, row in enumerate(kept, start=1):
        task_rel = (TASKS / row["task_id"]).relative_to(REPO).as_posix()
        for arm in arms:
            expected[spec_name(row["task_id"], arm)] = {
                "task_rel": task_rel,
                "package_digest": row["run_digest"],
                "task_id": row["task_id"],
                "arm": arm,
            }

    found = sorted(p.stem for p in out_dir.glob("ovn-g5-*.json"))
    if found != sorted(expected):
        failures.append(
            f"spec set mismatch: missing {sorted(set(expected) - set(found))}, "
            f"extra {sorted(set(found) - set(expected))}"
        )
    by_task: dict[str, dict[str, dict]] = {}
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
        if spec.agent != "terminus-2":
            failures.append(f"{name}: agent {spec.agent!r} != 'terminus-2'")
        if spec.question_ref != QUESTION_REF:
            failures.append(f"{name}: question_ref {spec.question_ref!r} != {QUESTION_REF!r}")
        if spec.linear_card != LINEAR_CARD:
            failures.append(f"{name}: linear_card {spec.linear_card!r} != {LINEAR_CARD!r}")
        if (spec.attempts, spec.concurrency) != (1, 1):
            failures.append(f"{name}: attempts/concurrency != 1/1")
        if spec.task_id != want["task_id"]:
            failures.append(f"{name}: task_id {spec.task_id!r} != eval {want['task_id']!r}")
        if spec.harness_tree_path != harness_rel:
            failures.append(
                f"{name}: harness path {spec.harness_tree_path!r} != --tree {harness_rel!r}"
            )
        if spec.harness_tree_sha256 != harness_digest:
            failures.append(f"{name}: harness digest {spec.harness_tree_sha256!r} != --tree-digest")
        want_model = TUNED_MODEL if want["arm"] == "tuned" else STOCK_MODEL
        if spec.model != want_model:
            failures.append(f"{name}: model {spec.model!r} != arm {want['arm']} {want_model!r}")
        if want["arm"] == "gepa":
            if spec.extra_instruction_path != gepa_path:
                failures.append(f"{name}: GEPA instruction path != --gepa-candidate")
            if spec.extra_instruction_sha256 != gepa_sha:
                failures.append(f"{name}: GEPA instruction sha != --gepa-sha256")
        elif spec.extra_instruction_path is not None or spec.extra_instruction_sha256 is not None:
            failures.append(f"{name}: non-GEPA arm must not carry an instruction addendum")
        if spec.est_cost_usd != EST_COST_USD:
            failures.append(
                f"{name}: est_cost_usd {spec.est_cost_usd} != {EST_COST_USD} ({EST_BASIS})"
            )
        if EST_BASIS not in (spec.hypothesis or ""):
            failures.append(f"{name}: hypothesis misses the est-cost basis")
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
        if spec.task_package_digest != want["package_digest"]:
            failures.append(f"{name}: spec package digest != eval list digest")
        by_task.setdefault(want["task_id"], {})[want["arm"]] = json.loads(path.read_text())
        checked += 1
    for task_id, arm_specs in sorted(by_task.items()):
        arms_here = sorted(arm_specs)
        reference = arm_specs[arms_here[0]]
        for other in arms_here[1:]:
            candidate = arm_specs[other]
            for key in set(reference) | set(candidate):
                if key in ALLOWED_ARM_DIFF:
                    continue
                if reference.get(key) != candidate.get(key):
                    failures.append(
                        f"{task_id}: arm {arms_here[0]} vs {other} differ in {key} "
                        "(only model, extra_instruction_path/sha256 and name/hypothesis may differ)"
                    )
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
        if cohort_doc.get("harness_tree", {}).get("sha256") != harness_digest:
            failures.append("cohort harness digest != tree digest")
        if cohort_doc.get("eval_list", {}).get("sha256") != EVAL_SHA256:
            failures.append("cohort eval sha != frozen v2")
        for index, row in enumerate(kept, start=1):
            want_order = arm_order(index, three)
            entries = [
                e for e in cohort_doc.get("cohort", []) if e.get("task_id") == row["task_id"]
            ]
            if not entries or entries[0].get("order") != want_order:
                failures.append(f"cohort order for {row['task_id']} != {'->'.join(want_order)}")
    else:
        failures.append("cohort.json is missing")
    if failures:
        print(f"CHECK FAILED ({checked} specs read):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"check ok: {checked} specs + cohort.json in {out_dir}")
    print(
        f"arms {'/'.join(arms)} on terminus-2; "
        f"per-trial est ${EST_COST_USD:.2f}; total est ${checked * EST_COST_USD:.2f}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=None, help="lf2 harness tree path")
    parser.add_argument("--tree-digest", default=None, help="pinned lf2 tree digest")
    parser.add_argument(
        "--eval-list",
        type=Path,
        default=EVAL_CSV,
        help="frozen G1 eval list CSV (default: ovn-sft-v0/eval_tasks.csv)",
    )
    parser.add_argument("--base-spec", type=Path, default=BASE_SPEC)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--gepa-candidate", type=Path, default=None)
    parser.add_argument("--gepa-sha256", default=None)
    parser.add_argument("--out-dir", type=Path, default=EXP / "g5-specs")
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
    kept = load_eval_list(args.eval_list)
    route = _route(args.base_spec)
    tree = _tree(args.tree, args.tree_digest)
    gepa_path, gepa_sha = _gepa(args.gepa_candidate, args.gepa_sha256)
    arms: tuple[str, ...] = ARMS_3 if gepa_path is not None else ARMS_2

    if args.check is not None:
        return check(args.check, kept, route, tree, arms, gepa_path, gepa_sha)

    snapshot = _snapshot_tasks(args.snapshot)
    staged = stage_tasks(kept, snapshot)
    names = generate(kept, staged, route, tree, args.out_dir, arms, gepa_path, gepa_sha)
    write_cohort(args.out_dir, kept, staged, tree, arms, gepa_path, gepa_sha)
    print(f"eval: {len(kept)} tasks v2 {EVAL_SHA256[:12]}…; arms {'/'.join(arms)}")
    print(f"wrote {len(names)} specs + cohort.json to {args.out_dir}")
    print(
        f"per-trial est ${EST_COST_USD:.2f}; total est ${len(names) * EST_COST_USD:.2f} ({EST_BASIS})"
    )
    return check(args.out_dir, kept, route, tree, arms, gepa_path, gepa_sha)


if __name__ == "__main__":
    raise SystemExit(main())
