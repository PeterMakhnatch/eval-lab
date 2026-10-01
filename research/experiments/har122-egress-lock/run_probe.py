#!/usr/bin/env python3
"""HAR-122 egress-lock proof: the no-model probe on 000226 and 000927.

Each task's HAR-113 leak-closed variant gets a probe copy (the package plus
``solution/solve.sh`` = ``probe_solve.sh``) under the gitignored
``derived/har122-probes/``. Harbor's oracle agent runs it on Daytona through
``evallab.harbor_daytona:BoundedDaytonaEnvironment`` twice: with
``egress_lock=true`` and, as the control, without it. Nothing else differs.

Usage (worktree root; needs DAYTONA_API_KEY, e.g. ``keys run --``):
    uv run python research/experiments/har122-egress-lock/run_probe.py run
    uv run python research/experiments/har122-egress-lock/run_probe.py report
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from evallab.storage.paths import shared_checkout_root  # noqa: E402

VARIANTS = shared_checkout_root(ROOT) / "derived/task-store/variants"
PROBES = ROOT / "derived/har122-probes"
RUNS = ROOT / "runs"
HARBOR = shutil.which("harbor") or "harbor"
ENV = "evallab.harbor_daytona:BoundedDaytonaEnvironment"

#: task -> (HAR-113 leak variant digest12, PyPI project, fixed release, upstream repo)
TASKS = {
    "000226": ("9666803bf548", "waitress", "1.3.1", "https://github.com/Pylons/waitress.git"),
    "000927": ("0b8d70b58210", "soupsieve", "1.9", "https://github.com/facelessuser/soupsieve.git"),
}
ARMS = {"lock": True, "open": False}


def job_name(task: str, arm: str) -> str:
    return f"har122-probe-{task}-{arm}"


def probe_package(task: str, pypi_ip: str) -> Path:
    digest, project, release, repo = TASKS[task]
    source = VARIANTS / f"mimo-v2.6-rl__format-code-task-{task}" / digest
    target = PROBES / task
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    for path in (target, *target.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)
    solve = (HERE / "probe_solve.sh").read_text()
    for key, value in {"PKG": project, "VER": release, "REPO": repo, "PYPI_IP": pypi_ip}.items():
        solve = solve.replace(f"__{key}__", value)
    (target / "solution").mkdir()
    (target / "solution/solve.sh").write_text(solve)
    (target / "solution/solve.sh").chmod(0o755)
    return target


def run() -> None:
    if not os.environ.get("DAYTONA_API_KEY"):
        raise SystemExit("DAYTONA_API_KEY is required (keys run -- ...)")
    pypi_ip = socket.gethostbyname("pypi.org")
    print("pypi.org resolves on the controller to", pypi_ip)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["PYTHONPATH"] = str(ROOT / "src")
    for task in TASKS:
        package = probe_package(task, pypi_ip)
        for arm, locked in ARMS.items():
            name = job_name(task, arm)
            if (RUNS / name).exists():
                print("skip, exists:", name)
                continue
            command = [
                HARBOR,
                "run",
                "--path",
                str(package),
                "--agent",
                "oracle",
                "--env",
                ENV,
                "--job-name",
                name,
                "--jobs-dir",
                str(RUNS),
                "--n-concurrent",
                "1",
                "--n-attempts",
                "1",
                "--environment-kwarg",
                "ttl_minutes=60",
                "--override-storage-mb",
                "10240",
            ]
            if locked:
                command += ["--environment-kwarg", "egress_lock=true"]
            print("$", " ".join(command[1:]), flush=True)
            subprocess.run(command, cwd=ROOT, env=env, check=False, timeout=3600)


def _trial_dir(name: str) -> Path | None:
    trials = [p for p in (RUNS / name).glob("*__*") if p.is_dir()]
    return trials[0] if len(trials) == 1 else None


def _tests_line(trial: Path) -> str | None:
    """The runner's summary lines (unittest ``Ran N`` / ``FAILED (...)``, pytest ``== ... ==``)."""
    path = trial / "verifier/test-stdout.txt"
    if not path.is_file():
        return None
    pattern = re.compile(r"^(Ran \d+ tests?.*|OK.*|FAILED \(.*\)|=+ .*(passed|failed|error).* =+)$")
    lines = [line for line in path.read_text(errors="replace").splitlines() if pattern.match(line)]
    return " | ".join(lines[-3:]) or None


def report() -> None:
    out: dict[str, dict] = {}
    for task in TASKS:
        for arm in ARMS:
            name = job_name(task, arm)
            trial = _trial_dir(name)
            if trial is None:
                out[name] = {"missing": True}
                continue
            result = json.loads((trial / "result.json").read_text())
            probe = trial / "agent/oracle.txt"
            lock = trial / "egress-lock.json"
            out[name] = {
                "trial": trial.name,
                "reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get(
                    "reward"
                ),
                "exception": (result.get("exception_info") or {}).get("exception_type"),
                "tests": _tests_line(trial),
                "egress_lock": json.loads(lock.read_text()) if lock.is_file() else None,
                "probe": [
                    line
                    for line in probe.read_text(errors="replace").splitlines()
                    if line.startswith(("==", "   "))
                ]
                if probe.is_file()
                else None,
            }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    {"run": run, "report": report}[sys.argv[1]]()
