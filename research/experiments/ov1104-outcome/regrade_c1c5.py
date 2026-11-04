"""Re-execute the C1–C5 impossible-task rollouts' final patches under the hardened verifier.

The 2026-11-01 live rollouts (`research/experiments/ov-c1-c5-live-rollouts-20261101`) were graded
on Modal under the original package (L0) and the `evallab harden` package (HS). This harness
re-executes the HS grade locally ($0) from each run's recorded `final.diff`, so the independent
check is a fresh observation rather than a stored number: a fresh env container from the task's
own `environment/Dockerfile` gives the pristine artifact files, the patch is applied on the host
(`apply_reference`), and the result is uploaded to a fresh verifier container built from the
hardened `tests/Dockerfile` (`evallab.ref_subset.SeparateSandbox`, the way Harbor uploads
artifacts). Scripted honest/cheat controls (`solutions/*/solve.sh` of the fx-c5 studies) run in
the env container and are graded the same way.

Usage (from the eval-lab worktree, local Docker)::

    .venv/bin/python research/experiments/ov1104-outcome/regrade_c1c5.py \
        --hardened-root /tmp/w3b-impossible/hardened \
        --cases /private/tmp/eval-lab-study/research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/cases \
        --out /tmp/ov1104-outcome/regrade-c1c5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from evallab.ref_subset import (
    Package,
    SeparateSandbox,
    _check,
    _docker,
    apply_reference,
    patch_paths,
)

SCHEMA = "ov1104-outcome.regrade-c1c5/v1"
REPO = Path(__file__).resolve().parents[3]
TASKS: dict[str, dict[str, Any]] = {
    "c1-billing-amount-format": {
        "workdir": "/testbed",
        "source": "research/experiments/fx-c5-impossible-test-modification-20261030",
    },
    "c2-release-ledger-order": {
        "workdir": "/app",
        "source": "research/experiments/fx-c5-impossible-hidden-test-reading-20261030",
    },
    "c3-payout-export": {
        "workdir": "/app",
        "source": "research/experiments/fx-c5-impossible-evaluator-mutation-20261030",
    },
    "c4-release-attestation": {
        "workdir": "/app",
        "source": "research/experiments/fx-c5-impossible-fabricated-verification-20261030",
    },
    "c5-glyph2-decode": {
        "workdir": "/app",
        "source": "research/experiments/fx-c5-impossible-environment-acquisition-20261030",
    },
}


def load_package(path: Path) -> Package:
    """A separate-verifier package without the ``solution/`` the loader insists on."""
    config = tomllib.loads((path / "task.toml").read_text())
    verifier = config.get("verifier") or {}
    artifacts = []
    for entry in config.get("artifacts") or []:
        artifacts.append(entry["source"] if isinstance(entry, dict) else entry)
    env = verifier.get("env") or {}
    return Package(
        path, path.name, "", "/", frozenset(), tuple(a.rstrip("/") for a in artifacts), 0,
        float(verifier.get("timeout_sec") or 600), True, True,
        None if verifier.get("user") is None else str(verifier["user"]),
        tuple((str(k), str(v)) for k, v in sorted(env.items()) if "${" not in str(v)),
    )  # fmt: skip


class Harness:
    def __init__(self, task: str, hardened: Path, *, memory: str, cpus: str) -> None:
        self.task = task
        self.workdir = TASKS[task]["workdir"].strip("/")
        self.package = load_package(hardened)
        self.sandbox = SeparateSandbox(self.package, memory=memory, cpus=cpus)
        self.before: dict[str, bytes] = {}

    def prepare(self) -> None:
        if (self.package.path / "tests" / "Dockerfile").is_file():
            self.sandbox.prepare()
        else:
            # Harbor's separate mode without tests/Dockerfile: the verifier is a fresh
            # container of the environment image with the artifacts uploaded.
            result = _docker("build", "-q", "-t", self.sandbox.image, str(self.package.path / "environment"), timeout=self.sandbox.build_timeout)
            _check(result, "build the environment image")
            self.sandbox.verifier_image = self.sandbox.image
        name = f"{self.sandbox.name}-base"
        self.sandbox._run(self.sandbox.image, name, network=False)
        try:
            self.before = self.sandbox._read(name)
        finally:
            _docker("rm", "-f", name)

    def close(self) -> None:
        self.sandbox.close()

    def files_from_patch(self, patch: str) -> tuple[dict[str, bytes | None], dict[str, Any]]:
        paths = patch_paths(patch)
        keyed = {p: self.before.get(f"{self.workdir}/{p}") for p in paths}
        after = apply_reference(patch, keyed)
        files: dict[str, bytes | None] = dict(self.before)
        for p, data in after.items():
            files[f"{self.workdir}/{p}"] = data
        return files, {"patch_files": len(paths), "patch_sha256": hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest()}

    def files_from_solution(self, solve_sh: Path) -> dict[str, bytes | None]:
        """Run a scripted control in a fresh env container and read its artifacts."""
        name = f"{self.sandbox.name}-sol"
        self.sandbox._run(self.sandbox.image, name, network=False)
        try:
            if (solve_sh.parent / "files").is_dir():
                _check(_docker("cp", f"{solve_sh.parent / 'files'}/.", f"{name}:/{self.workdir}/"), "copy solution files")
            _check(_docker("exec", name, "mkdir", "-p", "/solution"), "mkdir /solution")
            _check(_docker("cp", str(solve_sh), f"{name}:/solution/solve.sh"), "copy solve.sh")
            result = _docker("exec", name, "bash", "/solution/solve.sh", timeout=600)
            if result.returncode != 0:
                raise RuntimeError(f"solve.sh exited {result.returncode}: {result.stderr.decode(errors='replace')[-400:]}")
            after = self.sandbox._read(name)
        finally:
            _docker("rm", "-f", name)
        files: dict[str, bytes | None] = {p: None for p in self.before}
        files.update(after)
        return files

    def files_from_artifacts(self, art_dir: Path) -> tuple[dict[str, bytes | None], dict[str, Any]]:
        """Read the recorded Harbor trial artifacts dir when final.diff was not written."""
        files: dict[str, bytes | None] = dict(self.before)
        h = hashlib.sha256()
        count = 0
        for f in sorted(art_dir.rglob("*")):
            if not f.is_file() or f.name == "manifest.json" or ".pytest_cache" in f.parts:
                continue
            rel = f.relative_to(art_dir).as_posix()
            data = f.read_bytes()
            files[rel] = data
            h.update(rel.encode() + b"\0" + data + b"\0")
            count += 1
        if count == 0:
            raise ValueError(f"no artifact files in {art_dir}")
        return files, {"artifact_files": count, "artifacts_sha256": h.hexdigest()}
    def grade(self, label: str, files: dict[str, bytes | None]) -> dict[str, Any]:
        grade = self.sandbox.grade(label, [], files, self.package.verifier_timeout or 600.0)
        return {"reward": grade.reward, "rc": grade.rc, "status": grade.status, "seconds": round(grade.seconds, 1), "tail": grade.tail[-3000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardened-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True, help="receipts/cases dir with <id>/{final.diff,grade.json,run.json}")
    parser.add_argument("--trials-root", type=Path, default=Path("/tmp/w3b-impossible/trials"), help="trials dir holding <id>/HS/artifacts fallback for scripted runs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tasks", help="comma-separated task ids")
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--cpus", default="1")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cases = sorted(p for p in args.cases.iterdir() if (p / "run.json").is_file())
    wanted = args.tasks.split(",") if args.tasks else list(TASKS)
    for task in wanted:
        out_dir = args.out / task
        out_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {"schema": SCHEMA, "task": task, "hardened": str(args.hardened_root / task), "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "controls": {}, "runs": []}
        harness = Harness(task, args.hardened_root / task, memory=args.memory, cpus=args.cpus)
        started = time.monotonic()
        try:
            try:
                harness.prepare()
            except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                record["error"] = f"prepare: {exc}"[-2000:]
                (out_dir / "task.json").write_text(json.dumps(record, indent=1))
                print(json.dumps({"task": task, "error": record["error"][:300]}), flush=True)
                continue
            record["base_files"] = len(harness.before)
            for control in ("honest", "cheat"):
                solve = REPO / TASKS[task]["source"] / "solutions" / control / "solve.sh"
                if not solve.is_file():
                    record["controls"][control] = {"status": "no_solution"}
                    continue
                try:
                    files = harness.files_from_solution(solve)
                    record["controls"][control] = harness.grade(f"{control}", files)
                except (RuntimeError, OSError) as exc:
                    record["controls"][control] = {"status": "error", "reward": None, "tail": str(exc)[-1000:]}
                (out_dir / "task.json").write_text(json.dumps(record, indent=1))
            for case in cases:
                run = json.loads((case / "run.json").read_text())
                if run.get("task") != task:
                    continue
                stored = json.loads((case / "grade.json").read_text()) if (case / "grade.json").is_file() else {}
                patch = (case / "final.diff").read_text(errors="replace") if (case / "final.diff").is_file() else ""
                receipt: dict[str, Any] = {"run_id": run["id"], "kind": run.get("kind"), "model": run.get("model"), "stored_L0": stored.get("reward_L0"), "stored_HS": stored.get("reward_HS"), "diff_bytes": len(patch)}
                art_dir = args.trials_root / run["id"] / "HS" / "artifacts" if args.trials_root else None
                if not patch.strip() and art_dir is not None and art_dir.is_dir():
                    try:
                        files, meta = harness.files_from_artifacts(art_dir)
                        receipt.update(meta)
                        receipt["hardened_regrade"] = harness.grade(run["id"][:30], files)
                    except (ValueError, RuntimeError, OSError) as exc:
                        receipt["hardened_regrade"] = {"reward": None, "status": "patch_missing", "tail": str(exc)[-1000:]}
                elif not patch.strip():
                    receipt["hardened_regrade"] = {"reward": None, "status": "patch_missing"}
                else:
                    try:
                        files, meta = harness.files_from_patch(patch)
                        receipt.update(meta)
                        receipt["hardened_regrade"] = harness.grade(run["id"][:30], files)
                    except (ValueError, RuntimeError, OSError) as exc:
                        receipt["hardened_regrade"] = {"reward": None, "status": "patch_unreconstructible", "tail": str(exc)[-1000:]}
                record["runs"].append(receipt)
                (out_dir / "task.json").write_text(json.dumps(record, indent=1))
        finally:
            harness.close()
        record["seconds"] = round(time.monotonic() - started, 1)
        (out_dir / "task.json").write_text(json.dumps(record, indent=1))
        print(json.dumps({"task": task, "controls": {k: v.get("reward") for k, v in record["controls"].items()}, "runs": [(r["run_id"][-14:], r["stored_L0"], r["stored_HS"], r["hardened_regrade"].get("reward")) for r in record["runs"]], "seconds": record["seconds"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
