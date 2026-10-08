#!/usr/bin/env python3
"""mtime-normalize@1 local-Docker validation (Harbor 0.24, $0).

Chain: snapshot -> strip-future-history@1 -> purge-installed-copies@1 ->
mtime-normalize@1, derived into an isolated store under /tmp (validation
only; no lineage lands in the repo). Then stage Harbor packages:

* ``<task>-oracle``: mtime listing first (post-setup evidence), then the
  reference-fix bytes as solution/solve.sh.
* ``<task>-nop``: mtime listing only (read-only; the grade is the nop).

Runs use local Docker only: ``harbor run --env docker -a oracle|nop``.
No model call, no Modal, no Daytona.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parent.parent.parent  # research/experiments/<exp> -> repo root
HF_CODE = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
HF_SOURCE = {
    "repo": "FineEnvs/MiMo-V2.6-RL-harbor-code",
    "revision": "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
}
STORE = Path("/tmp/mtime-val/store")
STAGE = Path("/tmp/mtime-val/stage")
FIXDIR = Path("/tmp/mtime-val/fix")
HARBOR_WHEEL = "/tmp/harbor024/harbor-0.24.0-py3-none-any.whl"

# Fix provenance: the exact upstream commits the hidden tests exercise.
# Only non-test source files ship in the oracle solve.sh.
TASKS: dict[str, dict] = {
    "format-code-task-002402": {
        "workdir": "/testbed",
        "image": "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:6d706ce6902c3e83e1a524469b0ac43e6dbb57fbe69f30f079c7d1d43b0f6c1c",
        "fix_sha": "56f63eb604787879a4943008edeb9c50bd9207b6",
        "fix_files": {
            "numpyro/distributions/batch_util.py": "002402_batch_util.py"
        },
        # Full chain: purge applies to the setuptools-layout numpyro tree.
        "purge": True,
    },
    "format-code-task-002552": {
        "workdir": "/testbed",
        "image": "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:2dcbed218c61011d847ef2e3b37aa70f153a3f2f833f5cc1c85ea8b48860f207",
        "fix_sha": "e88159fbac2f2f2673641586fa46517632e7b1f2",
        "fix_files": {"miio/miot_models.py": "002552_miot_models.py"},
        # Strip + mtime only: purge-installed-copies@1 is scoped to its
        # CONFIRMED_PURGE tasks and fail-closes on the poetry-backend miio
        # tree (offline editable install unavailable, no linkable source).
        "purge": False,
    },
}

MTIME_LISTING = """#!/bin/bash
# mtime-normalize validation listing: post-setup work-tree mtime distribution.
# Read-only; changes nothing, so the grade is a nop control.
M=/var/lib/mimo
CWD={workdir}
BASE=$(cat "$M/base" 2>/dev/null)
echo "== cwd $CWD base ${{BASE:-none}}"
echo "== mtime_distinct $(find "$CWD" -printf '%T+\\n' | sort -u | wc -l | tr -d ' ')"
echo "== mtime_top $(find "$CWD" -printf '%T+ %p\\n' | sort -r | head -n 6 | tr '\\n' ';')"
echo "== mtime_newer_than_fixed_count $(find "$CWD" -newermt '2000-01-01' -print | wc -l | tr -d ' ')"
"""


def sh(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def find_record(task_id: str, transform: str, parent_digest: str) -> tuple[Path, str]:
    slug = f"mimo-v2.6-rl__{task_id}"
    for path in (STORE / "library" / "task-variants" / slug).glob("*.json"):
        record = json.loads(path.read_text())
        if (
            record.get("transform") == transform
            and record.get("parent", {}).get("digest") == parent_digest
        ):
            return path, record["variant_digest"].removeprefix("sha256:")[:12]
    raise RuntimeError(f"no {transform} record for {task_id} off {parent_digest[:12]}")


def variant_pkg(task_id: str, digest12: str) -> Path:
    return STORE / "variants" / f"mimo-v2.6-rl__{task_id}" / digest12


def cmd_derive(_args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from evallab.mtime_normalize import MARKER as MTIME_MARKER
    from evallab.mtime_normalize import derive_mtime_normalize
    from evallab.purge_installed_copies import MARKER as PURGE_MARKER
    from evallab.purge_installed_copies import derive_purge_installed_copies
    from evallab.registry import task_directory_digest
    from evallab.strip_future_history import STRIP_MARKER, derive_strip_future_history
    from evallab.task_variants import VariantExistsError
    for task_id in TASKS:
        snapshot = HF_CODE / task_id
        snapshot_digest = task_directory_digest(snapshot)
        print(f"snapshot {task_id}: {snapshot_digest[:19]}")
        hf_source = {
            "kind": "hf",
            "repo": HF_SOURCE["repo"],
            "revision": HF_SOURCE["revision"],
            "path": f"tasks/{task_id}",
        }
        try:
            strip = derive_strip_future_history(
                snapshot,
                rationale="mtime validation: strip future git history first.",
                created_by="mtime-normalize-validation",
                repo_root=STORE,
                variants_root=STORE / "variants",
                parent_source=hf_source,
            )
            print(f"  strip: {strip.variant_digest[:19]} {strip.digest12}")
        except VariantExistsError as exc:
            print(f"  strip exists: {exc}")
        strip_record, strip_digest12 = find_record(task_id, "strip-future-history@1", snapshot_digest)
        strip_digest = task_directory_digest(variant_pkg(task_id, strip_digest12))
        if TASKS[task_id]["purge"]:
            try:
                purge = derive_purge_installed_copies(
                    variant_pkg(task_id, strip_digest12),
                    repairs_digest=snapshot_digest,
                    rationale="mtime validation: purge installed copies second.",
                    created_by="mtime-normalize-validation",
                    repo_root=STORE,
                    variants_root=STORE / "variants",
                    parent_source={"kind": "variant", "record": str(strip_record)},
                )
                print(f"  purge: {purge.variant_digest[:19]} {purge.digest12}")
            except VariantExistsError as exc:
                print(f"  purge exists: {exc}")
            purge_record, purge_digest12 = find_record(task_id, "purge-installed-copies@1", strip_digest)
            mtime_parent = variant_pkg(task_id, purge_digest12)
            mtime_source: dict[str, str] = {"kind": "variant", "record": str(purge_record)}
            mtime_parent_digest = task_directory_digest(mtime_parent)
            expected_order = (STRIP_MARKER, PURGE_MARKER, MTIME_MARKER)
        else:
            mtime_parent = variant_pkg(task_id, strip_digest12)
            mtime_source = {"kind": "variant", "record": str(strip_record)}
            mtime_parent_digest = strip_digest
            expected_order = (STRIP_MARKER, MTIME_MARKER)
        try:
            mtime = derive_mtime_normalize(
                mtime_parent,
                rationale="mtime validation: normalize work-tree mtimes last.",
                created_by="mtime-normalize-validation",
                repo_root=STORE,
                variants_root=STORE / "variants",
                parent_source=mtime_source,
            )
            print(f"  mtime: {mtime.variant_digest[:19]} {mtime.digest12}")
        except VariantExistsError as exc:
            print(f"  mtime exists: {exc}")
        _, mtime_digest12 = find_record(task_id, "mtime-normalize@1", mtime_parent_digest)
        # Composition proof on the final setup.sh.
        setup = (variant_pkg(task_id, mtime_digest12) / "environment" / "setup" / "setup.sh").read_text()
        order = [setup.index(m) for m in expected_order]
        assert order == sorted(order), f"transform order wrong for {task_id}"
        if TASKS[task_id]["purge"]:
            assert PURGE_MARKER in setup
        else:
            assert PURGE_MARKER not in setup
        print(f"  order {'<'.join(expected_order)} OK for {task_id} ({mtime_digest12})")


def extract_fixes(spec: dict) -> None:
    FIXDIR.mkdir(parents=True, exist_ok=True)
    for repo_path, staged in spec["fix_files"].items():
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--platform", "linux/amd64",
                "--workdir", spec["workdir"], "--entrypoint", "git",
                spec["image"], "show", f"{spec['fix_sha']}:{repo_path}",
            ],
            capture_output=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cannot extract oracle source: {proc.stderr[-500:]}")
        (FIXDIR / staged).write_bytes(proc.stdout)


def oracle_script(workdir: str, fix_files: dict[str, str]) -> bytes:
    lines = [
        MTIME_LISTING.format(workdir=workdir).rstrip("\n"),
        "set -eu",
        f'CWD="{workdir}"',
        'cd "$CWD"',
    ]
    for repo_path, staged in fix_files.items():
        blob = base64.b64encode((FIXDIR / staged).read_bytes()).decode("ascii")
        lines += [
            f'base64 -d > "{repo_path}" <<\'MTIME_EOF\'',
            blob,
            "MTIME_EOF",
        ]
    lines.append('echo "mtime-validation oracle: reference fix applied"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        print(f"exists, skipping: {dest}")
        return
    shutil.copytree(src, dest, symlinks=False)


def mtime_pkg(task_id: str) -> Path:
    slug = f"mimo-v2.6-rl__{task_id}"
    matches = [
        path
        for path in (STORE / "library" / "task-variants" / slug).glob("*.json")
        if json.loads(path.read_text()).get("transform") == "mtime-normalize@1"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"need exactly 1 mtime record for {task_id}: {matches}")
    digest12 = json.loads(matches[0].read_text())["variant_digest"].removeprefix("sha256:")[:12]
    return STORE / "variants" / slug / digest12


def cmd_stage(_args: argparse.Namespace) -> int:
    for task_id, spec in TASKS.items():
        extract_fixes(spec)
        pkg = mtime_pkg(task_id)
        staged = {
            f"mtime-{task_id}-oracle": oracle_script(spec["workdir"], spec["fix_files"]),
            f"mtime-{task_id}-nop": MTIME_LISTING.format(workdir=spec["workdir"]).encode(),
        }
        for dirname, solve in staged.items():
            dest = STAGE / dirname
            _copy_tree(pkg, dest)
            sol_dir = dest / "solution"
            sol_dir.mkdir(exist_ok=True)
            (sol_dir / "solve.sh").write_bytes(solve)
            print(f"staged {dest} sha={hashlib.sha256(solve).hexdigest()[:12]}")
    return 0


def harbor(*args: str, timeout: int = 5400) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uvx", "--from", HARBOR_WHEEL, "harbor", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/tmp",
    )


def cmd_run(args: argparse.Namespace) -> int:
    jobs_dir = Path(args.jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    proc = harbor(
        "run", "--env", "docker", "-a", args.agent, "-p", args.task_pkg,
        "--jobs-dir", str(jobs_dir), "--job-name", args.job_name, "-n", "1", "-y", "-q",
    )
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:], file=sys.stderr)
    print(f"rc={proc.returncode}")
    return proc.returncode


def trial_summary(trial_dir: Path) -> dict:
    out: dict = {"trial_dir": str(trial_dir)}
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            out["reward"] = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
        except Exception as exc:  # noqa: BLE001
            out["result_error"] = str(exc)
    reward_path = trial_dir / "verifier" / "reward.txt"
    if reward_path.is_file():
        out["reward_txt"] = reward_path.read_text().strip()[:50]
    for log_name in ("oracle.txt", "nop.txt"):
        oracle_log = trial_dir / "agent" / log_name
        if oracle_log.is_file():
            text = oracle_log.read_text(errors="replace")
            out["listing"] = [line for line in text.splitlines() if line.startswith("==")][:8]
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    jobs_dir = Path(args.jobs_dir)
    rows = [trial_summary(trial_dir.parent) for trial_dir in sorted(jobs_dir.glob("*/*/result.json"))]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"collected_at": datetime.now(UTC).isoformat(), "rows": rows}, indent=2),
        encoding="utf-8",
    )
    print(f"collected {len(rows)} trials -> {out_path}")
    for row in rows:
        print(f"- {row['trial_dir']}: reward={row.get('reward')}")
        for line in row.get("listing", []):
            print(f"    {line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("derive")
    sub.add_parser("stage")
    run = sub.add_parser("run")
    run.add_argument("--task-pkg", required=True)
    run.add_argument("--agent", required=True, choices=["oracle", "nop"])
    run.add_argument("--job-name", required=True)
    run.add_argument("--jobs-dir", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--jobs-dir", required=True)
    collect.add_argument("--out", required=True)
    args = parser.parse_args()
    return {"derive": cmd_derive, "stage": cmd_stage, "run": cmd_run, "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
