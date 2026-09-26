"""Reef-process evaluation through Eval Lab's own CLI and retained Harbor evidence.

No Reef, Harbor or Eval Lab imports belong here. The CLI validates and freezes
HAR-71 trees. This adapter owns the committed split, candidate-id journal and
paired dispatch order; the normal queue owns approval and execution policy.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

SIDES = ("candidate", "current")
QUEUE_STATES = ("proposed", "pending", "waiting", "approved", "running", "done", "failed", "rejected")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
TASK_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


class LabGateError(ValueError):
    """Refuse an invalid request or contradictory retained evidence."""


@dataclass(frozen=True)
class LabConfig:
    lab_root: Path
    split_path: Path
    model: str
    record_dir: Path
    python_executable: Path | None = None
    agent: str = "terminus-2"
    environment: str = "docker"
    episode_repeats: int = 1
    timeout_seconds: int | None = None
    cost_limit_usd: float | None = None
    est_cost_usd: float | None = None
    max_requests: int = 200
    max_input_tokens: int = 5_000_000
    max_output_tokens: int = 131_072
    max_total_tokens: int | None = None
    submitted_by: str = "reef-gate"
    tick_timeout_seconds: float = 3600.0
    tick_poll_seconds: float = 5.0

    def __post_init__(self) -> None:
        root = Path(self.lab_root).expanduser().resolve()
        object.__setattr__(self, "lab_root", root)
        for name in ("split_path", "record_dir"):
            path = Path(getattr(self, name)).expanduser()
            object.__setattr__(self, name, (path if path.is_absolute() else root / path).resolve())
        python = self.python_executable or root / ".venv/bin/python"
        # Do not resolve the venv interpreter symlink into the system Python.
        object.__setattr__(self, "python_executable", Path(python).expanduser().absolute())
        if self.agent != "terminus-2":
            raise LabGateError("only terminus-2 supports this pinned-harness gate")
        for name in ("model", "environment", "submitted_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise LabGateError(f"{name} must be an explicit nonempty string")
        for name in ("episode_repeats", "max_requests", "max_input_tokens", "max_output_tokens",
                     "max_total_tokens", "timeout_seconds"):
            value = getattr(self, name)
            if value is None and name in ("max_total_tokens", "timeout_seconds"):
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LabGateError(f"{name} must be a positive integer")
        for name in ("cost_limit_usd", "est_cost_usd", "tick_timeout_seconds", "tick_poll_seconds"):
            value = getattr(self, name)
            if value is None and name in ("cost_limit_usd", "est_cost_usd"):
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0
                    or (name.startswith("tick_") and value == 0)):
                raise LabGateError(f"{name} must be finite and nonnegative (waits strictly positive)")

    @classmethod
    def from_dict(cls, raw: dict) -> LabConfig:
        if not isinstance(raw, dict):
            raise LabGateError("lab configuration must be an object")
        if set(raw) - {field.name for field in fields(cls)}:
            raise LabGateError("unknown lab configuration fields")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise LabGateError("missing or invalid lab configuration fields") from exc


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LabGateError(f"missing or corrupt JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LabGateError(f"expected JSON object: {path}")
    return value


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def tree_digest(files: Mapping[str, str]) -> str:
    # HAR-71 evidence_tree_digest: length-prefixed relative path and exact bytes.
    result = hashlib.sha256()
    for name, text in sorted(files.items()):
        path, content = name.encode(), text.encode()
        result.update(len(path).to_bytes(8, "big"))
        result.update(path)
        result.update(len(content).to_bytes(8, "big"))
        result.update(content)
    return "sha256:" + result.hexdigest()


def checked_files(files: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(files, Mapping) or not files:
        raise LabGateError("harness files must be a nonempty mapping")
    for name, text in files.items():
        if (not isinstance(name, str) or not name or name.startswith("/") or "\\" in name
                or "\x00" in name or any(part in ("", ".", "..") for part in name.split("/"))
                or not isinstance(text, str)):
            raise LabGateError("unsafe harness file mapping")
    return dict(files)


def retain_tree(path: Path, files: Mapping[str, str]) -> None:
    if path.exists():
        actual: dict[str, str] = {}
        if path.is_symlink():
            raise LabGateError("retained harness is a symlink")
        for child in path.rglob("*"):
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise LabGateError("retained harness contains a symlink or special file")
            if child.is_file():
                actual[child.relative_to(path).as_posix()] = child.read_text(encoding="utf-8")
        if actual != files:
            raise LabGateError("retained harness content changed; refusing overwrite")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".tree-", dir=path.parent))
    try:
        for relative, text in files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        staging.rename(path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def task_digest(path: Path) -> str:
    # Same package algorithm/ignored files as evallab.registry.task_directory_digest;
    # no import across the process boundary. Prepared CLI specs verify it again.
    if not path.is_dir() or path.is_symlink():
        raise LabGateError("dev task package must be a real directory")
    result = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_symlink() or not (child.is_file() or child.is_dir()):
            raise LabGateError("dev task package contains a symlink or special file")
        if (not child.is_file() or child.name in {".DS_Store", ".git", "__pycache__", ".pytest_cache"}
                or child.suffix in {".pyc", ".pyo", ".tmp"}):
            continue
        relative = child.relative_to(path).as_posix()
        result.update(f"{hashlib.sha256(child.read_bytes()).hexdigest()}  ./{relative}\n".encode())
    return "sha256:" + result.hexdigest()


class SubprocessRunner:
    def run(self, argv: list[str], *, cwd: Path, timeout: float, python: Path) -> tuple[int, str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PYTHON", "VIRTUAL_ENV", "UV_PROJECT", "UV_PYTHON"))}
        env.update({
            "PATH": f"{python.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(cwd / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "EVALLAB_DERIVED_ROOT": str(cwd / "derived/parquet"),
        })
        completed = subprocess.run(
            [str(python), "-m", "evallab.cli", *argv], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr


class LabEvaluator:
    def __init__(self, config: LabConfig, *, runner: SubprocessRunner | None = None) -> None:
        self.config = config
        self.runner = runner or SubprocessRunner()

    def cli(self, argv: list[str], timeout: float = 120) -> str:
        code, stdout, _stderr = self.runner.run(
            argv, cwd=self.config.lab_root, timeout=timeout,
            python=self.config.python_executable,
        )
        if code:
            # Provider errors can contain request contents. Keep those in the
            # native run, not the gate's shared decision/error text.
            raise LabGateError(f"Lab CLI {argv[0]} failed with exit {code}")
        return stdout

    @contextmanager
    def lock(self, candidate_id: str):
        if not isinstance(candidate_id, str) or not candidate_id:
            raise LabGateError("candidate_id must be a nonempty string")
        directory = self.config.record_dir / hashlib.sha256(candidate_id.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "candidate.lock").open("a+b") as stream:
            deadline = time.monotonic() + self.config.tick_timeout_seconds
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LabGateError("candidate evaluation is already in progress") from None
                    time.sleep(min(self.config.tick_poll_seconds, max(0, deadline - time.monotonic())))
            try:
                yield directory
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def split(self, task_ids: tuple[str, ...]) -> tuple[dict, str, str]:
        root, path = self.config.lab_root, self.config.split_path
        if not path.is_relative_to(root):
            raise LabGateError("split must be committed in the Lab checkout")
        relative = path.relative_to(root).as_posix()
        committed = subprocess.run(
            ["git", "show", f"HEAD:{relative}"], cwd=root, capture_output=True, timeout=10,
        )
        if committed.returncode or committed.stdout != path.read_bytes():
            raise LabGateError("split must match its committed HEAD bytes before evaluation")
        revision = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", relative], cwd=root,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        split = read_object(path)
        if type(split.get("schema_version")) is not int or split["schema_version"] != 1:
            raise LabGateError("split schema_version must be 1")
        sets: dict[str, tuple[set, set, set]] = {}
        for side in ("dev", "held_out"):
            rows = split.get(side)
            if not isinstance(rows, list) or not rows:
                raise LabGateError("split requires nonempty dev and held_out lists")
            ids, paths, packages = set(), set(), set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"task_id", "task_path", "package_digest"}:
                    raise LabGateError("split task rows require id, path and package digest")
                identity, task_path, package = row["task_id"], row["task_path"], row["package_digest"]
                if not isinstance(identity, str) or not TASK_ID.fullmatch(identity):
                    raise LabGateError("invalid split task identity")
                if not isinstance(package, str) or not DIGEST.fullmatch(package):
                    raise LabGateError("invalid split package digest")
                if not isinstance(task_path, str) or not task_path:
                    raise LabGateError("invalid split task path")
                raw = Path(task_path).expanduser()
                resolved = (raw if raw.is_absolute() else root / raw).resolve()
                if side == "dev" and (raw.is_absolute() or not resolved.is_relative_to(root)):
                    raise LabGateError("dev task path must stay inside the Lab checkout")
                if identity in ids or resolved in paths or package in packages:
                    raise LabGateError("duplicate task identity, path or package in split")
                ids.add(identity)
                paths.add(resolved)
                packages.add(package)
            sets[side] = ids, paths, packages
        if any(left & right for left, right in zip(sets["dev"], sets["held_out"], strict=True)):
            raise LabGateError("held-out identities, paths and packages must be disjoint from dev")
        if tuple(row["task_id"] for row in split["dev"]) != tuple(task_ids):
            raise LabGateError("candidate tasks must match the committed dev set exactly")
        for row in split["dev"]:
            record = read_object(root / "library/registry" / (row["task_id"] + ".json"))
            uses = record.get("allowed_uses")
            if (not isinstance(uses, list) or "heldout" in uses
                    or not {"measurement", "training"}.issubset(uses)):
                raise LabGateError("dev task requires measurement/training and must not be heldout")
            if (record.get("state") != "registered" or record.get("task_id") != row["task_id"]
                    or record.get("task_path") != row["task_path"]):
                raise LabGateError("dev task registry identity, state or path mismatch")
            package = (record.get("digests") or {}).get("package")
            task_path = root / row["task_path"]
            # Resolve aliases only for comparison; reject a linked package or ancestor.
            if any(parent.is_symlink() for parent in (task_path, *task_path.parents) if parent != root):
                raise LabGateError("dev task path contains a symlink")
            if package != row["package_digest"] or task_digest(task_path) != package:
                raise LabGateError("dev task package digest drift")
        return split, digest(committed.stdout), revision

    def locate(self, spec_id: str) -> tuple[str, Path] | None:
        if not isinstance(spec_id, str) or not re.fullmatch(r"[A-Za-z0-9-]+", spec_id):
            raise LabGateError("invalid queued spec ID")
        matches = [(state, path) for state in QUEUE_STATES
                   for path in (self.config.lab_root / "queue" / state).glob(f"*-{spec_id}.json")]
        if len(matches) > 1:
            raise LabGateError("spec ID has duplicate queue entries")
        return matches[0] if matches else None

    def recover(self, name: str) -> tuple[str, str, Path] | None:
        matches = []
        for state in QUEUE_STATES:
            for path in (self.config.lab_root / "queue" / state).glob("*.json"):
                value = read_object(path)
                if value.get("name") == name:
                    matches.append((value["spec_id"], state, path))
        if len(matches) > 1:
            raise LabGateError("job identity has duplicate submitted specs")
        return matches[0] if matches else None

    @staticmethod
    def spec_contract(spec: dict) -> dict:
        return {key: value for key, value in spec.items()
                if value is not None and key not in {"spec_id", "submitted_at", "policy_rule"}}

    def prepare(self, candidate_id: str, current_files: Mapping[str, str],
                candidate_files: Mapping[str, str], task_ids: tuple[str, ...]) -> dict:
        with self.lock(candidate_id) as directory:
            manifest = self.prepare_locked(directory, candidate_id, current_files, candidate_files, task_ids)
            return self.prepared_result(directory, manifest)

    def prepared_result(self, directory: Path, manifest: dict) -> dict:
        commands = [f"cd {shlex.quote(str(self.config.lab_root))} && uv run evallab approve "
                    f"{row['spec_id']} --actor peter" for row in manifest["pairings"]]
        for command in commands:
            print(command, flush=True)
        return {
            "manifest_path": str(directory / "manifest.json"), "manifest": manifest,
            "spec_details": manifest["pairings"], "approve_commands": commands,
            "spec_ids": [row["spec_id"] for row in manifest["pairings"]],
        }

    def prepare_locked(self, directory: Path, candidate_id: str, current_files: Mapping[str, str],
                       candidate_files: Mapping[str, str], task_ids: tuple[str, ...]) -> dict:
        split, split_digest, split_commit = self.split(task_ids)
        files = {"current": checked_files(current_files), "candidate": checked_files(candidate_files)}
        binding = {key: str(value) if isinstance(value, Path) else value
                   for key, value in asdict(self.config).items()}
        request = {"files": files, "binding": binding, "split_digest": split_digest}
        request_digest = digest(canonical(request))
        path = directory / "manifest.json"
        manifest = read_object(path) if path.exists() else {
            "schema_version": 1, "candidate_id": candidate_id, "request_digest": request_digest,
            "split_digest": split_digest, "split_commit": split_commit, "binding": binding,
            "tree_digests": {side: tree_digest(files[side]) for side in SIDES}, "pairings": [],
        }
        if manifest.get("candidate_id") != candidate_id or manifest.get("request_digest") != request_digest:
            raise LabGateError("conflicting content or configuration for candidate_id")
        expected_pairings = []
        for task in split["dev"]:
            for repeat in range(self.config.episode_repeats):
                for side in SIDES:
                    identity = hashlib.sha256(canonical([candidate_id, task["task_id"], repeat, side])).hexdigest()[:32]
                    name = f"reef-gate-{identity}"
                    expected_pairings.append({
                        "task_id": task["task_id"], "repeat": repeat, "side": side,
                        "job_name": name, "spec_path": f"derived/prepared/{name}.json",
                    })
        if not path.exists():
            manifest["pairings"] = [{**row, "spec_id": None} for row in expected_pairings]
        actual_pairings = [
            {key: value for key, value in row.items() if key not in {"spec_id", "spec_digest"}}
            for row in manifest["pairings"]
        ]
        if (actual_pairings != expected_pairings
                or manifest["tree_digests"] != {side: tree_digest(files[side]) for side in SIDES}):
            raise LabGateError("retained campaign pairing or tree identity changed")
        for side in SIDES:
            retain_tree(directory / side, files[side])
        save_json(path, manifest)  # Durable intent precedes every CLI mutation.
        entries = {row["task_id"]: row for row in split["dev"]}
        # Validate/freeze ALL task+tree combinations before submitting any of them.
        for row in manifest["pairings"]:
            spec_path = self.config.lab_root / row["spec_path"]
            tree = manifest["tree_digests"][row["side"]]
            if not spec_path.exists():
                args = ["tasks", "prepare", entries[row["task_id"]]["task_path"],
                        "--name", row["job_name"], "--agent", self.config.agent,
                        "--model", self.config.model, "--environment", self.config.environment,
                        "--harness-tree", str(directory / row["side"]),
                        "--harness-tree-sha256", tree, "--output", row["spec_path"],
                        "--submitted-by", self.config.submitted_by, "--json"]
                for field, flag in (("timeout_seconds", "timeout-seconds"),
                                    ("cost_limit_usd", "cost-limit-usd"),
                                    ("est_cost_usd", "estimated-cost-usd"),
                                    ("max_requests", "max-requests"),
                                    ("max_input_tokens", "max-input-tokens"),
                                    ("max_output_tokens", "max-output-tokens"),
                                    ("max_total_tokens", "max-total-tokens")):
                    value = getattr(self.config, field)
                    if value is not None:
                        args.extend(["--" + flag, str(value)])
                self.cli(args)
            spec = read_object(spec_path)
            expected = {"name": row["job_name"], "agent": self.config.agent,
                        "model": self.config.model, "environment": self.config.environment,
                        "harness_tree_sha256": tree,
                        "task_package_digest": entries[row["task_id"]]["package_digest"],
                        "attempts": 1, "concurrency": 1, "jobs_dir": "runs"}
            for field in ("timeout_seconds", "cost_limit_usd", "est_cost_usd", "max_requests",
                          "max_input_tokens", "max_output_tokens", "max_total_tokens"):
                value = getattr(self.config, field)
                if value is not None:
                    expected[field] = value
            if any(spec.get(key) != value for key, value in expected.items()):
                raise LabGateError("prepared spec differs from the pinned campaign contract")
            # tasks prepare freezes a NEW task path; require the content digest,
            # not equality with the original source path.
            for field in ("task_path", "harness_tree_path"):
                frozen = self.config.lab_root / spec[field]
                if not frozen.resolve().is_relative_to(self.config.lab_root):
                    raise LabGateError("prepared snapshot escapes Lab checkout")
            if task_digest(self.config.lab_root / spec["task_path"]) != expected["task_package_digest"]:
                raise LabGateError("prepared task package digest drift")
            fingerprint = digest(canonical(self.spec_contract(spec)))
            if row.get("spec_digest") not in (None, fingerprint):
                raise LabGateError("retained prepared spec changed")
            row["spec_digest"] = fingerprint
        save_json(path, manifest)
        for row in manifest["pairings"]:
            found = self.locate(row["spec_id"]) if row["spec_id"] else None
            if row["spec_id"] and found is None:
                raise LabGateError("previously submitted spec disappeared; refusing resubmission")
            if found is None:
                recovered = self.recover(row["job_name"])
                if recovered:
                    row["spec_id"], state, queued = recovered
                    found = state, queued
                else:
                    self.cli(["submit", row["spec_path"]])
                    recovered = self.recover(row["job_name"])
                    if recovered is None:
                        raise LabGateError("submit produced no retained queue spec")
                    row["spec_id"], state, queued = recovered
                    found = state, queued
            queued_spec = read_object(found[1])
            if (queued_spec.get("spec_id") != row["spec_id"]
                    or digest(canonical(self.spec_contract(queued_spec))) != row["spec_digest"]):
                raise LabGateError("submitted spec differs from the retained prepared spec")
            save_json(path, manifest)
        manifest["approve_commands"] = [
            f"cd {shlex.quote(str(self.config.lab_root))} && uv run evallab approve {row['spec_id']} --actor peter"
            for row in manifest["pairings"]
        ]
        save_json(path, manifest)
        return manifest

    def evaluate(self, candidate_id: str, current_files: Mapping[str, str],
                 candidate_files: Mapping[str, str], task_ids: tuple[str, ...]) -> dict:
        started = time.monotonic()
        with self.lock(candidate_id) as directory:
            manifest = self.prepare_locked(directory, candidate_id, current_files, candidate_files, task_ids)
            self.prepared_result(directory, manifest)
            deadline = time.monotonic() + self.config.tick_timeout_seconds
            incomplete = self.dispatch(manifest, deadline)
            result = self.collect(directory, manifest, incomplete)
            result["metadata"]["evaluation_seconds"] = time.monotonic() - started
            save_json(directory / "evaluation.json", result)
            return result

    def dispatch(self, manifest: dict, deadline: float) -> str | None:
        # Each episode must finish before the next side/repeat starts, including
        # a retry that discovers an already-running prior episode.
        for row in manifest["pairings"]:
            attempted = False
            while True:
                found = self.locate(row["spec_id"])
                if found is None:
                    return "spec_missing"
                state, path = found
                if digest(canonical(self.spec_contract(read_object(path)))) != row["spec_digest"]:
                    raise LabGateError("queued spec changed before dispatch")
                if state == "done":
                    break
                if state in ("failed", "rejected"):
                    return "withdrawn" if state == "rejected" else "spec_failed"
                if (self.config.lab_root / "queue/STOP").exists():
                    return "budget_stopped"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "approval_timeout" if state in ("waiting", "pending", "proposed") else "spec_timeout"
                if state == "approved" and not attempted:
                    attempted = True
                    try:
                        self.cli(["tick", "--spec-id", row["spec_id"]], timeout=remaining)
                    except subprocess.TimeoutExpired:
                        return "tick_timeout"
                    except (LabGateError, OSError):
                        return "dispatch_failed"
                    continue
                if attempted and state in ("approved", "waiting", "pending", "proposed"):
                    return "dispatch_blocked"
                time.sleep(min(self.config.tick_poll_seconds, remaining))
        return None

    def collect(self, directory: Path, manifest: dict, incomplete: str | None) -> dict:
        scores: dict[str, list] = {side: [] for side in SIDES}
        failures: dict[str, list] = {side: [] for side in SIDES}
        evidence = []
        for row in manifest["pairings"]:
            observed = {**row, "reward": None, "trial_id": None, "trial_dir": None}
            located = self.locate(row["spec_id"])
            state = located[0] if located else "missing"
            observed["state"] = state
            job_dir = self.config.lab_root / "runs" / row["job_name"]
            observed["job_dir"] = str(job_dir)
            cause = "episode_not_completed"
            if state == "done":
                try:
                    job = read_object(job_dir / "result.json")
                    trials = [child for child in job_dir.iterdir()
                              if child.is_dir() and (child / "result.json").is_file()]
                    if not job.get("finished_at") or job.get("exception_info") or len(trials) != 1:
                        raise LabGateError("incomplete or ambiguous native job")
                    trial = read_object(trials[0] / "result.json")
                    observed.update(trial_id=trial.get("id") or trials[0].name, trial_dir=str(trials[0]))
                    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
                    reward = rewards.get("reward")
                    if trial.get("exception_info"):
                        cause = "native_trial_exception"
                    elif not trial.get("finished_at"):
                        cause = "native_trial_incomplete"
                    elif isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
                        cause = "native_reward_missing_or_invalid"
                    else:
                        observed["reward"] = float(reward)
                        cause = None
                except (LabGateError, OSError, TypeError, AttributeError):
                    cause = "native_evidence_missing_or_invalid"
            observed["failure_reason"] = cause
            side = row["side"]
            scores[side].append(observed["reward"])
            if cause:
                failures[side].append({"task": row["task_id"], "stage": "trajectory", "cause": cause})
            evidence.append(observed)
        if incomplete is None and any(row["reward"] is None for row in evidence):
            incomplete = "incomplete_evidence"
        metrics: dict[str, Any] = {
            "candidate_scores": tuple(scores["candidate"]), "current_scores": tuple(scores["current"]),
            "episode_failures": sum(row["reward"] is None for row in evidence),
            "episode_repeats": self.config.episode_repeats,
            "evaluation_sides": [side for side in SIDES if all(value is not None for value in scores[side])],
        }
        for side in SIDES:
            metrics.update({
                f"{side}_failures": tuple(failures[side]),
                f"{side}_score": sum(value for value in scores[side] if value is not None),
                # Native Terminus ATIF has no Reef native-jsonl stage/residue
                # observations. These are the empty aggregations used by Reef's
                # Terminus reader, not fabricated native-agent counters.
                f"{side}_residue": 0, f"{side}_agents": {},
                f"{side}_paths": tuple({} for _ in scores[side]),
            })
        return {"metrics": metrics, "metadata": {
            "candidate_id": manifest["candidate_id"], "model": self.config.model,
            "task_ids": [row["task_id"] for row in read_object(self.config.split_path)["dev"]],
            "split_path": str(self.config.split_path), "split_digest": manifest["split_digest"],
            "split_commit": manifest["split_commit"],
            "candidate_tree_sha256": manifest["tree_digests"]["candidate"],
            "current_tree_sha256": manifest["tree_digests"]["current"],
            "spec_ids": [row["spec_id"] for row in manifest["pairings"]],
            "manifest_path": str(directory / "manifest.json"), "evidence": evidence,
            "complete": incomplete is None, "incomplete_reason": incomplete,
            "native_stage_observations_available": False,
        }}
