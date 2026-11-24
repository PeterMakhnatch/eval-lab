import argparse
import fcntl
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path.home() / "Developer/muster-runs/day-2026-11-24/mac-run"
BESPOKE = Path.home() / "Developer/bespoke-macbench-lab"
CHECKS = ("O1", "O2", "O3", "O4", "O5", "O6", "game-call-log")
GROUPS = (
    ("proxy_proof", ("O2",)),
    ("holdout_proof", ("O3", "O4", "O5", "game-call-log")),
    ("cli_proof", ("O1", "O6")),
)


def docker_ready(path: Path) -> str | None:
    try:
        result = subprocess.run(["docker", "ps"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        path.write_text(f"docker ps: {type(error).__name__}\n", encoding="utf-8")
        return "Cannot inspect Docker before the proof."
    path.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        return "docker ps did not succeed."
    for _ in range(12):
        if os.getloadavg()[0] <= 20:
            return None
        time.sleep(10)
    return "The 1-minute load average stayed above 20 for the 120 s wait."


def execute(module_name: str, folder: Path, checks: set[str]) -> dict[str, tuple[str, str]]:
    module = importlib.import_module(module_name)
    if module_name == "proxy_proof":
        assert checks == {"O2"}, checks
        return {"O2": module.run(folder)}
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "proofs.docker.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict.fromkeys(checks, ("not settled", "Another proof holds the Docker lock."))
        obstacle = docker_ready(folder / "docker-ps-before.txt")
        if obstacle is not None:
            return dict.fromkeys(checks, ("not settled", obstacle))
        return module.run(folder, checks)


def record(root: Path, check: str, folder: Path, result: tuple[str, str]) -> str:
    verdict, reason = result
    assert verdict in {"pass", "fail", "not settled"}, verdict
    assert reason and folder.is_dir()
    row = {"check": check, "verdict": verdict, "evidence": str(folder), "reason": reason}
    text = json.dumps(row, indent=2, sort_keys=True) + "\n"
    (folder / f"{check}.json").write_text(text, encoding="utf-8")
    temporary = root / f".{folder.parent.name}-{check}.json"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(root / f"{check}.json")
    print(f"{check}: {verdict}: {reason} ({folder})", flush=True)
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", choices=CHECKS, action="append")
    parser.add_argument("--proofs-dir", type=Path, default=ROOT / "proofs")
    args = parser.parse_args()
    if not sys.dont_write_bytecode or Path(sys.executable).parent != BESPOKE / ".venv/bin":
        parser.error("use B/.venv/bin/python with PYTHONDONTWRITEBYTECODE=1")
    root = args.proofs_dir.expanduser().resolve()
    allowed = (ROOT.resolve(), Path("/tmp/fable-day/macrun").resolve())
    if not any(root.is_relative_to(base) for base in allowed):
        parser.error("use the mac-run artifact or scratch folder for proofs")
    root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="proof-", dir=root))
    requested = set(args.check or CHECKS)
    verdicts = []
    for name, group in GROUPS:
        selected = requested.intersection(group)
        if not selected:
            continue
        folder = evidence / name
        folder.mkdir()
        try:
            outcomes = execute(name, folder, selected)
            assert set(outcomes) == selected, (name, outcomes.keys(), selected)
        except Exception as error:
            reason = f"The proof raised {type(error).__name__} before recording an outcome."
            frames = traceback.extract_tb(error.__traceback__)
            (folder / "proof-error.txt").write_text(
                reason + "\n" + "".join(traceback.format_list(frames)), encoding="utf-8"
            )
            outcomes = dict.fromkeys(selected, ("fail", reason))
        verdicts.extend(record(root, check, folder, outcomes[check]) for check in sorted(selected))
    assert len(verdicts) == len(requested)
    return 1 if "fail" in verdicts else 2 if "not settled" in verdicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
