"""Resume only oracle/nop repeat-3 controls; never execute attack probes.

Run with ``uv run --extra laminar python .../resume_controls.py --help``.
The time limit is a trial-admission deadline, not permission to kill an
in-flight Harbor process. Every FIFO acquisition is independently bounded.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

from evallab.docker_lock import DockerLockTimeout, docker_slot
from evallab.execution_contracts import RunRequest
from evallab.mimo_census import trial_tests_executed
from evallab.queue import Executor
from evallab.runner import ExecutionFailure
from evallab.task_variants import task_directory_digest

PROTECTED_TASKS = {
    "000041", "000128", "000236", "000792", "001269", "002139",
    "002308", "002391", "002402", "002486", "002866", "002947",
}


def scored_cell(record: dict[str, Any]) -> bool:
    rewards = record.get("rewards")
    return (
        isinstance(rewards, list)
        and len(rewards) == 3
        and all(reward in (0.0, 1.0) for reward in rewards)
        and record.get("test_execution") == [True] * 3
        and record.get("run_errors") == [None] * 3
        and record.get("exception_info") is None
        and record.get("returncode") in (0, None)
    )


def reusable_cells(receipts: Path, journal: Path) -> set[tuple[str, str, str]]:
    """Reuse scored positive or negative controls, never another digest."""
    done: set[tuple[str, str, str]] = set()
    if receipts.is_file():
        with receipts.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                for agent in ("oracle", "nop"):
                    record = {
                        "rewards": json.loads(row.get(f"{agent}_repeat_rewards") or "null"),
                        "test_execution": json.loads(row.get(f"{agent}_repeat_execution") or "null"),
                        "run_errors": json.loads(row.get(f"{agent}_repeat_errors") or "null"),
                        "exception_info": json.loads(row.get(f"{agent}_exception") or "null"),
                        "returncode": json.loads(row.get(f"{agent}_process_rc") or "null"),
                    }
                    if scored_cell(record):
                        done.add((row["task_id"], row["final_digest"], agent))
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            agent = row.get("agent")
            if row.get("kind") == "control" and agent in ("oracle", "nop") and scored_cell(row):
                done.add((row["task_id"], row["final_digest"], agent))
    return done


def append_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps(record, sort_keys=True), flush=True)


def image_cached(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return True
    if "No such image" not in result.stderr:
        raise RuntimeError(f"docker image inspect failed: {result.stderr.strip()}")
    return False


def execute(args: argparse.Namespace) -> None:
    with args.manifest.open(encoding="utf-8", newline="") as handle:
        manifest = {row["task_id"]: row for row in csv.DictReader(handle)}
    pending = json.loads(args.pending.read_text(encoding="utf-8"))
    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    args.journal.parent.mkdir(parents=True, exist_ok=True)
    done = reusable_cells(args.receipts, args.journal)
    deadline = time.monotonic() + args.seconds
    repo_root = Path(__file__).resolve().parents[3]
    for target in pending["tasks"]:
        if not target["runnable"]:
            continue
        task_id = target["task_id"]
        row = manifest[task_id]
        digest = row["final_digest"]
        if digest != target["final_digest"]:
            raise ValueError(f"pending inventory digest changed for {task_id}; regenerate inventory")
        if row["status"] != "built":
            continue
        if all(
            (task_id, digest, agent) in done
            or (agent == "oracle" and row["reference_fix"] == "none")
            for agent in ("oracle", "nop")
        ):
            continue
        package = args.package_root / row["package_path"]
        if not package.is_dir():
            raise FileNotFoundError(f"missing immutable package: {package}; derive the exact manifest digest first")
        if task_directory_digest(package) != digest:
            raise ValueError(f"materialized package digest changed for {task_id}; refusing execution")
        config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
        image = config["environment"]["docker_image"]
        cached_before: bool | None = None
        try:
            for agent in ("oracle", "nop"):
                key = (task_id, digest, agent)
                if key in done or (agent == "oracle" and row["reference_fix"] == "none"):
                    continue
                if agent == "oracle" and not (package / "solution/solve.sh").is_file():
                    raise FileNotFoundError(f"indexed reference fix missing in package: {package}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                queue_started = time.monotonic()
                try:
                    with docker_slot(
                        label=f"v6-controls:{task_id}:{agent}",
                        timeout=min(args.lock_wait_seconds, remaining),
                    ):
                        queue_wait = time.monotonic() - queue_started
                        if time.monotonic() >= deadline:
                            return
                        if cached_before is None:
                            cached_before = image_cached(image)
                        job_name = f"v6-{task_id.rsplit('-', 1)[1]}-{agent}-x3-{uuid.uuid4().hex[:8]}"
                        request = RunRequest(
                            task=package,
                            agent=agent,
                            name=job_name,
                            jobs_dir=args.jobs_dir,
                            environment="docker",
                            concurrency=1,
                            attempts=1,
                            verifier_repeat_n=3,
                        )
                        started = time.monotonic()
                        # Image inspection can consume the remaining admission window.
                        if started >= deadline:
                            return
                        execution_error = None
                        try:
                            Executor.from_repo(repo_root).execute_direct(request, ingest=False)
                            returncode = 0
                        except ExecutionFailure as exc:
                            returncode = 1
                            execution_error = str(exc)
                        active_wall = time.monotonic() - started
                        record: dict[str, Any] = {
                            "kind": "control", "task_id": task_id, "final_digest": digest,
                            "agent": agent, "job": job_name, "returncode": returncode,
                            "active_wall_sec": round(active_wall, 3),
                            "queue_wait_sec": round(queue_wait, 3),
                            "rewards": None, "test_execution": None,
                            "image": image, "cached_before": cached_before,
                            "execution_error": execution_error,
                        }
                        paths = list((args.jobs_dir / job_name).glob("*/verifier/stability.json"))
                        if len(paths) == 1:
                            stability_path = paths[0]
                            stability = json.loads(stability_path.read_text(encoding="utf-8"))
                            record["stability_path"] = str(stability_path)
                            record["rewards"] = [run.get("reward") for run in stability["runs"]]
                            record["run_errors"] = [run.get("error") for run in stability["runs"]]
                            record["test_execution"] = [
                                trial_tests_executed(stability_path.parent / "repeat" / str(index))
                                for index in range(len(stability["runs"]))
                            ]
                            native = json.loads((stability_path.parent.parent / "result.json").read_text(encoding="utf-8"))
                            record["exception_info"] = native.get("exception_info")
                        append_record(args.journal, record)
                        if scored_cell(record):
                            done.add(key)
                except DockerLockTimeout:
                    append_record(args.journal, {
                        "kind": "fifo-timeout", "task_id": task_id, "final_digest": digest,
                        "agent": agent, "queue_wait_sec": round(time.monotonic() - queue_started, 3),
                    })
                    return
        finally:
            if (
                cached_before is False
                and task_id.rsplit("-", 1)[1] not in PROTECTED_TASKS
                and image_cached(image)
            ):
                cleanup = subprocess.run(["docker", "rmi", image], capture_output=True, text=True, check=False)
                append_record(args.journal, {
                    "kind": "image-cleanup", "task_id": task_id, "final_digest": digest,
                    "image": image, "returncode": cleanup.returncode,
                    "output": cleanup.stdout + cleanup.stderr,
                })
                if cleanup.returncode:
                    raise RuntimeError(f"new image cleanup failed for {task_id}; see {args.journal}")


def main() -> None:
    home = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=home / "manifest.csv")
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--pending", type=Path, default=home / "pending-controls.json")
    parser.add_argument("--receipts", type=Path, default=home / "results.csv")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=7200)
    parser.add_argument("--lock-wait-seconds", type=float, default=60)
    args = parser.parse_args()
    if args.seconds <= 0 or args.lock_wait_seconds <= 0:
        parser.error("--seconds and --lock-wait-seconds must be positive")
    os.environ["EVALLAB_STATE_JOURNAL"] = "off"
    execute(args)


if __name__ == "__main__":
    main()
