from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from muster.lab.host import Agent, Trial

SCHEMA = "muster-host-trial/v1"
MAX_HOME_FILES = 200
MAX_HOME_BYTES = 5 * 1024 * 1024
MAX_HOME_ENTRIES = 100_000
MAX_WORLD_FILES = 256
MASK = b"***"
GIT_TIMEOUT_SEC = 30
LOGS = ("agent.stdout", "agent.stderr")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SEC, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _sources(root: Path) -> set[str]:
    package = root / "muster"
    found = set()
    for path in package.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[1] != "tasks" and "__pycache__" not in relative.parts:
            found.add(relative.as_posix())
        assert len(found) <= MAX_WORLD_FILES * 4
    return found


def revision() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    head = _git(root, "rev-parse", "HEAD")
    tracked = _git(root, "status", "--porcelain", "--untracked-files=no", "--", "muster", ":!muster/tasks")
    listed = _git(root, "ls-files", "--", "muster", ":!muster/tasks")
    known = tracked is not None and listed is not None
    untracked = _sources(root) - set((listed or "").splitlines())
    return {"revision": (head.strip() or None) if head is not None else None,
            "dirty": bool((tracked or "").strip() or untracked) if known else None}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def world_hashes(task: Path) -> dict[str, str]:
    folder = task / "environment/world"
    files = sorted(path for path in folder.rglob("*")
                   if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts)
    assert len(files) <= MAX_WORLD_FILES
    return {str(path.relative_to(folder)): sha256(path) for path in files}


def base_record(trial: Trial, agent: Agent, revision_info: dict[str, Any]) -> dict[str, Any]:
    seats = list(trial.seats)
    return {
        "schema": SCHEMA, "trial": trial.name, "task": str(trial.task), "task_name": trial.task_name,
        **trial.metadata, "attempt": trial.attempt, "seats": seats, "timeout_sec": trial.timeout_sec,
        "agent": {"file": str(agent.path), "name": agent.name, "about": agent.about, "argv": {},
                  "env_keys": sorted(agent.env), "pass_env": list(agent.pass_env)},
        "exit_codes": dict.fromkeys(seats), "timed_out": dict.fromkeys(seats, False),
        "requests": {seat: {} for seat in seats}, "wall_sec": None,
        "started_at": now(), "finished_at": None, "play_started_at": None, "play_finished_at": None,
        "seat_finished_at": {}, "muster_revision": revision_info["revision"],
        "muster_dirty": revision_info["dirty"], "world_sha256": world_hashes(trial.task),
        "instruction_sha256": {seat: sha256(trial.task / "instructions" / f"{seat}.md") for seat in seats},
        "home_during_run": {}, "home_copy": {}, "reward": None, "rewards": None, "error": None,
    }


def config(trial: Trial, agent: Agent) -> dict[str, Any]:
    participants = [{"seat": seat, "name": agent.name, "import_path": None, "model_name": None,
                     "kwargs": {}} for seat in trial.seats]
    return {"task": {"path": str(trial.task)}, "trial_name": trial.name, "participants": participants,
            "host": {"agent": str(agent.path), "timeout_sec": trial.timeout_sec}}


def write_config(trial: Trial, agent: Agent, directory: Path) -> None:
    write_json(directory / "config.json", config(trial, agent))


def _seat_exception(record: dict[str, Any], seat: str) -> dict[str, str] | None:
    if record["timed_out"][seat]:
        return {"exception_type": "AgentTimeoutError",
                "exception_message": f"the agent ran past {record['timeout_sec']} s"}
    code = record["exit_codes"][seat]
    if code != 0:
        return {"exception_type": "NonZeroAgentExitCodeError", "exception_message": f"exit code {code}"}
    return None


def result(trial: Trial, agent: Agent, record: dict[str, Any]) -> dict[str, Any]:
    assert record["error"] is None and isinstance(record["rewards"], dict)
    seats = [{
        "seat": seat, "under_test": under_test, "agent_info": {"name": agent.name}, "agent_result": None,
        "agent_execution": {"started_at": record["play_started_at"],
                            "finished_at": record["seat_finished_at"].get(seat)},
        "exception_info": _seat_exception(record, seat),
    } for seat, under_test in zip(trial.seats, trial.under_test, strict=True)]
    return {
        "schema_version": 1, "task_name": trial.task_name, "trial_name": trial.name, "task_checksum": None,
        "config": config(trial, agent), "agent_info": {"name": agent.name},
        "verifier_result": {"rewards": record["rewards"]}, "reward_valid": True, "exception_info": None,
        "started_at": record["started_at"], "finished_at": record["finished_at"],
        "agent_execution": {"started_at": record["play_started_at"],
                            "finished_at": record["play_finished_at"]},
        "seat_results": seats, "host": "host.json",
    }


def prepare_seats(trial: Trial, directory: Path, home_root: Path, homes: dict[str, Path],
                  record: dict[str, Any]) -> None:
    assert not homes
    for seat in trial.seats:
        seat_dir = directory / seat
        seat_dir.mkdir()
        shutil.copyfile(trial.task / "instructions" / f"{seat}.md", seat_dir / "instruction.md")
        homes[seat] = Path(tempfile.mkdtemp(prefix="seat-", dir=home_root))
        record["home_during_run"][seat] = str(homes[seat])
    assert set(homes) == set(trial.seats)


def _masked_copy(source: Path, target: Path, tokens: list[str]) -> None:
    secrets = [token.encode() for token in tokens if token]
    with source.open("rb") as reader, target.open("wb") as writer:
        for line in reader:
            for secret in secrets:
                line = line.replace(secret, MASK)
            writer.write(line)


def mask_file(path: Path, tokens: list[str]) -> None:
    temporary = path.with_name(f".{path.name}.masking")
    _masked_copy(path, temporary, tokens)
    os.replace(temporary, path)


def mask_logs(trial: Trial, directory: Path, tokens: list[str]) -> None:
    for seat in trial.seats:
        for name in LOGS:
            path = directory / seat / name
            if path.is_file():
                mask_file(path, tokens)


def _copy_one(source: Path, home: Path, target: Path, tokens: list[str], copy: dict[str, Any]) -> None:
    if source.is_symlink() or not source.is_file():
        return
    try:
        size = source.stat().st_size
        if copy["files"] >= MAX_HOME_FILES or copy["bytes"] + size > MAX_HOME_BYTES:
            copy["truncated"] = True
            return
        destination = target / source.relative_to(home)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _masked_copy(source, destination, tokens)
    except OSError:
        copy["truncated"] = True
        return
    copy["files"] += 1
    copy["bytes"] += size


def _remove(path: Path) -> bool:
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    return not path.exists()


def collect_home(home: Path, target: Path, tokens: list[str]) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    copy: dict[str, Any] = {"files": 0, "bytes": 0, "truncated": False, "removed": False}
    visited = 0
    for root, folders, files in os.walk(home):
        folders.sort()
        for name in sorted(files):
            visited += 1
            if visited > MAX_HOME_ENTRIES:
                break
            _copy_one(Path(root) / name, home, target, tokens, copy)
        if visited > MAX_HOME_ENTRIES:
            copy["truncated"] = True
            break
    copy["removed"] = _remove(home)
    assert copy["files"] <= MAX_HOME_FILES and copy["bytes"] <= MAX_HOME_BYTES
    return copy


def collect_homes(directory: Path, homes: dict[str, Path], tokens: list[str],
                  record: dict[str, Any]) -> None:
    for seat, home in homes.items():
        record["home_copy"][seat] = collect_home(home, directory / seat / "home", tokens)
