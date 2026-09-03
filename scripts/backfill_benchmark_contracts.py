#!/usr/bin/env python3
"""Gate Zero backfill: emit registry-bound ``benchmark_contract.json`` into
promoted evidence bundles so ``load_trial_bundle`` can consume them.

Additive-only by construction:

- Planning is delegated to :func:`evallab.contract_emission.plan_contract_emission`
  (registry-bound, fail-closed, never replaces an existing contract).
- Publishing goes through :func:`evallab.contract_emission.atomic_write_bytes`
  (no-replace) into the trial's own directory.
- A whole-tree digest snapshot taken before any write proves that no
  pre-existing evidence byte changed; any drift aborts with a nonzero exit.

Verification mode (``--verify``) then loads every backfilled trial through
``load_trial_bundle`` and finalizes trial admissibility, reporting
``artifact_present`` per trial.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

from evallab.contract_emission import (
    CONTRACT_FILENAME,
    ContractEmissionRefusal,
    atomic_write_bytes,
    plan_contract_emission,
)
from evallab.interpretation.benchmark_events import load_trial_bundle
from evallab.results import TrialRecord, load_job, load_trial
from evallab.trial_admissibility import finalize_trial_admissibility


def tree_state(root: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            state[rel] = f"symlink:{path.readlink()}"
        elif path.is_file():
            state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            state[rel] = "dir"
    return state


def selftest_symlink_refusal() -> None:
    """Negative control: a symlinked destination must refuse, never be replaced."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        trial = tmp / "trial"
        trial.mkdir()
        target = tmp / "real.json"
        target.write_bytes(b"{}\n")
        (trial / CONTRACT_FILENAME).symlink_to(target)
        try:
            atomic_write_bytes(trial / CONTRACT_FILENAME, b"{}\n")
        except ContractEmissionRefusal:
            print(f"symlink negative: OK ({CONTRACT_FILENAME} symlink destination refused)")
            return
        raise SystemExit("symlink negative FAILED: symlinked destination was accepted")


def backfill(job_dir: Path, repo_root: Path, apply: bool) -> int:
    before = tree_state(job_dir)
    plans = plan_contract_emission(job_dir, repo_root)
    print(f"{job_dir.name}: {len(plans)} contract plan(s)")
    if apply:
        for plan in plans:
            destination = job_dir / plan.trial_name / CONTRACT_FILENAME
            atomic_write_bytes(destination, plan.body)
            print(f"  emitted {plan.trial_name}/{CONTRACT_FILENAME} "
                  f"(task_id={plan.task_id})")
        after = tree_state(job_dir)
        drifted = [
            rel for rel, digest in before.items()
            if after.get(rel) != digest
        ]
        if drifted:
            raise SystemExit(f"MUTATION DETECTED in {job_dir.name}: {drifted}")
        added = sorted(set(after) - set(before))
        expected = sorted(
            f"{plan.trial_name}/{CONTRACT_FILENAME}" for plan in plans
        )
        if added != expected:
            raise SystemExit(f"UNEXPECTED NEW FILES in {job_dir.name}: {added}")
        print(f"  digest guard: {len(before)} pre-existing paths unchanged, "
              f"{len(added)} additive file(s)")
    return len(plans)


def verify(job_dirs: list[Path], repo_root: Path) -> int:
    failures = 0
    for job_dir in job_dirs:
        job = load_job(job_dir)
        for trial_dir in sorted(p for p in job_dir.iterdir() if p.is_dir()):
            if (trial_dir / CONTRACT_FILENAME).is_symlink():
                continue
            if not (trial_dir / CONTRACT_FILENAME).is_file():
                continue
            trial: TrialRecord = load_trial(trial_dir)
            try:
                bundle = load_trial_bundle(trial_dir, repo_root=repo_root)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"FAIL {job_dir.name}/{trial_dir.name}: load_trial_bundle: {exc}")
                failures += 1
                continue
            authority = finalize_trial_admissibility(
                job=job,
                trial=trial,
                repo_root=repo_root,
                trial_dir=trial_dir,
            )
            artifact_present = bool(authority and authority.artifact_present)
            status = "OK" if artifact_present else "NO-AUTHORITY"
            print(
                f"{status} {job_dir.name}/{trial_dir.name}: "
                f"task={bundle.contract.task_id} "
                f"registry_binding={bundle.registry_binding_verified} "
                f"admissibility_artifact_present={artifact_present}"
            )
            if not artifact_present:
                failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dirs", nargs="*", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--selftest-symlink", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    if args.selftest_symlink:
        selftest_symlink_refusal()
        if not args.job_dirs:
            return 0

    if not args.job_dirs:
        parser.error("at least one job directory is required")

    if not args.verify:
        for job_dir in args.job_dirs:
            backfill(job_dir, repo_root, apply=args.apply)

    if args.verify:
        failures = verify(args.job_dirs, repo_root)
        if failures:
            print(f"{failures} verification failure(s)")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
