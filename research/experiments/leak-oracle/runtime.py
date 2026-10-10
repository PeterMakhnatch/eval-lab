"""Source-bound HAR-191 direct Modal replay; importing this module allocates nothing.

The caller owns app creation, admission and publication. Cancellation is retained as
an operational receipt after awaited cleanup, so allocation accounting is not lost.
Task code and the portable extractor execute only inside fresh provider sandboxes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shlex
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from budget import (
    CPU_LIMIT,
    CPU_REQUEST,
    MEMORY_LIMIT_MIB,
    MEMORY_REQUEST_MIB,
    SANDBOX_SECONDS,
)

STAGE = "/opt/har191"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SOURCE_FIELDS = ("run_digest", "image", "base", "test_patch_sha256")
SUCCESS = {"ok", "ok-divergent"}


class RuntimeFailure(Exception):
    """An operational failure, never a scientific negative control result."""


class Deadline(RuntimeFailure):
    """The approved provider lifetime censored this arm."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(path: Path, *, complete: bool = True) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return {
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
        "complete": complete,
    }


def _save_json(path: Path, value: dict) -> None:
    # Each invocation owns a new directory. Never overwrite experimental evidence.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _arm(task: dict, network: str, patch_sha: str | None = None) -> dict:
    return {
        "status": "not-run",
        "sandbox_id": None,
        **{key: task.get(key) for key in SOURCE_FIELDS},
        "network_mode": network,
        "test_patch_applied": False,
        "solution_patch_sha256": patch_sha,
        "solution_patch_applied": False,
        "verifier_exit_code": None,
        "evidence_path": None,
        "evidence_sha256": None,
        "artifacts": {},
    }


def _report() -> dict:
    return {
        "sandbox_id": None,
        "creation_attempted": False,
        "terminal_confirmed": True,
        "lifetime_upper_seconds": 0,
        "creation_started_at": None,
        "terminated_at": None,
    }


def _parse_interpreter_probe(text: str) -> tuple[str, int]:
    """Parse the extractor-python probe stdout into (path, version code).

    The probe prints exactly two lines: the chosen interpreter path and its
    numeric version (major * 100 + minor). Anything else — empty output (all
    interpreters too old), extra lines (a merged stream), a relative path —
    fails closed. Pure function; the shell probe itself is validated against
    real task images, not unit tests.
    """
    picked = text.splitlines()
    if len(picked) != 2 or not picked[0].startswith("/") or not picked[1].isdigit():
        raise RuntimeFailure("could not bind existing in-image Python interpreter")
    return picked[0], int(picked[1])




BUILD_ATTEMPTS = 3


async def _build_image(task: dict, app: Any) -> tuple[Any, dict]:
    """Hydrate once before any sandbox clock; cold imports are separate evidence.

    Registry hydration is transiently flaky at this scale (227 wave-1
    ImageBuildErrors on images that pull cleanly elsewhere, zero in wave 2),
    so retry boundedly. Builds are outside the sandbox-reservation fence;
    attempts are recorded, never silently absorbed.
    """
    started = time.monotonic()
    record: dict = {
        "image": task["image"],
        "image_id": None,
        "started_at": _utc(),
        "attempts": 0,
        "attempt_errors": [],
    }
    image = None
    try:
        import modal

        for attempt in range(1, BUILD_ATTEMPTS + 1):
            record["attempts"] = attempt
            try:
                candidate = modal.Image.from_registry(task["image"])
                image = await candidate.build.aio(app)
                record.update({"status": "complete", "image_id": image.object_id})
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded, retried, reported
                record["attempt_errors"].append(f"{type(exc).__name__}: {exc}"[:300])
                if attempt == BUILD_ATTEMPTS:
                    record.update(
                        {
                            "status": "infrastructure-error",
                            "reason": (
                                f"image build: {record['attempt_errors'][-1]} "
                                f"({BUILD_ATTEMPTS} attempts)"
                            ),
                        }
                    )
                else:
                    await asyncio.sleep(min(30.0, 5.0 * attempt))
    except asyncio.CancelledError:
        image = None
        record.update(
            {
                "status": "infrastructure-error",
                "cancelled": True,
                "reason": "cancelled during image build; no sandbox creation attempted",
            }
        )
    record.update({"finished_at": _utc(), "elapsed_seconds": max(0.0, time.monotonic() - started)})
    return image, record


def _inputs(task: dict) -> dict:
    from evallab.registry import task_directory_digest

    if not re.fullmatch(r"format-code-task-[0-9]{6}", task.get("task_id", "")):
        raise RuntimeFailure("invalid task identity")
    directory = Path(task["task_path"])
    if task_directory_digest(directory) != task["run_digest"]:
        raise RuntimeFailure("selected package digest changed after preparation")
    files = {}
    for name in (
        "task.toml",
        "instruction.md",
        "tests/test.patch",
        "tests/test.sh",
        "tests/test_command.sh",
    ):
        data = (directory / name).read_bytes()
        expected = task.get("files_sha256", {}).get(name)
        if expected is None or _sha(data) != expected:
            raise RuntimeFailure(f"selected file hash mismatch: {name}")
        files[name] = data
    if _sha(files["tests/test.patch"]) != task["test_patch_sha256"]:
        raise RuntimeFailure("selected test patch identity mismatch")
    config = tomllib.loads(files["task.toml"].decode("utf-8"))
    environment = config["environment"]
    if environment["docker_image"] != task["image"] or environment["workdir"] != task["workdir"]:
        raise RuntimeFailure("selected image/workdir differ from frozen manifest")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", task["image"]):
        raise RuntimeFailure("image is not digest pinned")
    if task["workdir"] not in ("/testbed", "/workspace/repo"):
        raise RuntimeFailure("unsupported workdir or unsafe staging overlap")
    command = environment.get("healthcheck", {}).get("command")
    if not isinstance(command, str) or not command:
        raise RuntimeFailure("selected package has no setup healthcheck")
    # This corpus has one grading protocol; don't infer results for another one.
    grader = files["tests/test.sh"].decode("utf-8")
    if any(
        fragment not in grader
        for fragment in (
            "git apply --verbose /tests/test.patch",
            "RC=$?",
            'echo "test command exited $RC"',
            '"$V/reward.txt"',
            '"$V/test_output.log"',
        )
    ):
        raise RuntimeFailure("selected verifier has an unsupported result protocol")
    return {"files": files, "healthcheck": command, "config": config}


def _verifier_result(
    outer_exit: int | None, tail: bytes, reward: bytes | None, *, complete: bool
) -> dict:
    """Interpret the known grader's final echo, not its misleading shell exit."""
    result = {
        "status": "infrastructure-error",
        "verifier_exit_code": None,
        "test_patch_applied": False,
    }
    if not complete:
        return {**result, "reason": "verifier output is incomplete"}
    if type(outer_exit) is not int or outer_exit < 0 or outer_exit >= 128:
        return {**result, "reason": "verifier shell was killed or has no exit status"}
    lines = tail.rstrip(b"\r\n").splitlines()
    match = re.fullmatch(rb"test command exited (-?[0-9]+)", lines[-1]) if lines else None
    if match is None:
        return {
            **result,
            "reason": "missing inner test-command exit marker (setup or test-patch failure)",
        }
    code = int(match.group(1))
    result.update({"verifier_exit_code": code, "test_patch_applied": True})
    if reward is None:
        return {**result, "reason": "verifier wrote no reward"}
    if reward.strip() not in (b"0", b"1"):
        return {**result, "reason": "verifier reward is malformed"}
    observed_reward = int(reward.strip())
    result["reward"] = observed_reward
    if observed_reward != int(code == 0):
        return {**result, "reason": "reward disagrees with inner test-command exit"}
    if code == 124 or outer_exit == 124:
        return {
            **result,
            "status": "timeout",
            "timed_out": True,
            "reason": "test command timed out",
        }
    if code < 0 or code >= 128:
        return {**result, "killed": True, "reason": "inner test command has a signal-shaped exit"}
    if code in (125, 126, 127):
        return {**result, "reason": "test-command launcher failed or executable is unavailable"}
    if outer_exit != 0:
        return {**result, "reason": "grading shell failed after the inner command"}
    return {**result, "status": "complete", "termination_reason": "exited"}


class _Session:
    def __init__(
        self, task: dict, app: Any, directory: Path, network: str, patch_sha: str | None = None
    ):
        self.task = task
        self.app = app
        self.directory = directory
        self.arm = _arm(task, network, patch_sha)
        self.report = _report()
        self.sandbox = None
        self.image = None
        self.started = None
        self.deadline = None
        self.cancelled = False
        self.extraction = {
            "status": "input-error",
            "fix": None,
            "rationale": "extraction not reached",
        }
        self.patch = None
        self.git_dir = None
        self._base_dirt: set[str] | None = None
        self._last_dirt: set[str] | None = None
        self.log_path = directory / "evidence.log"
        self.log_path.touch(exist_ok=False)

    def remaining(self) -> float:
        if self.deadline is None:
            return float(SANDBOX_SECONDS)
        return max(0.0, self.deadline - time.monotonic())

    async def bounded(self, operation):
        remaining = self.remaining()
        if remaining <= 0:
            operation.close()
            raise Deadline("approved 180-second sandbox deadline expired")
        try:
            return await asyncio.wait_for(operation, timeout=remaining)
        except TimeoutError as exc:
            raise Deadline("approved 180-second sandbox deadline expired") from exc

    def append(self, label: str, path: Path) -> None:
        with self.log_path.open("ab") as output, path.open("rb") as source:
            output.write((f"\n--- {label} ---\n").encode())
            while block := source.read(1024 * 1024):
                output.write(block)

    async def create(self) -> None:
        import modal

        if self.image is None:
            raise RuntimeFailure("pinned image must be hydrated before sandbox creation")
        self.started = time.monotonic()
        self.deadline = self.started + SANDBOX_SECONDS
        self.report.update(
            {
                "creation_attempted": True,
                "terminal_confirmed": False,
                "lifetime_upper_seconds": SANDBOX_SECONDS,
                "creation_started_at": _utc(),
            }
        )
        self.sandbox = await self.bounded(
            modal.Sandbox.create.aio(
                "sleep",
                str(SANDBOX_SECONDS),
                app=self.app,
                image=self.image,
                cpu=(CPU_REQUEST, CPU_LIMIT),
                memory=(MEMORY_REQUEST_MIB, MEMORY_LIMIT_MIB),
                timeout=SANDBOX_SECONDS,
                workdir=self.task["workdir"],
                block_network=self.arm["network_mode"] == "locked",
            )
        )
        self.report["sandbox_id"] = self.sandbox.object_id
        self.arm["sandbox_id"] = self.sandbox.object_id
        self.arm["image_id"] = self.image.object_id
        self.arm["resource_limits"] = {
            "cpu_physical": [CPU_REQUEST, CPU_LIMIT],
            "memory_mib": [MEMORY_REQUEST_MIB, MEMORY_LIMIT_MIB],
            "provider_lifetime_seconds": SANDBOX_SECONDS,
        }

    async def command(self, phase: str, command: str) -> dict:
        remaining = self.remaining()
        if remaining < 1:
            raise Deadline("less than one second remains for provider exec")
        paths = {name: self.directory / f"{phase}.{name}.log" for name in ("stdout", "stderr")}
        for path in paths.values():
            path.touch(exist_ok=False)
        complete = False
        try:

            async def execute():
                # Streams stay separate: machine-parsed probes read stdout only,
                # so interpreter warnings and .pth tracebacks on stderr can no
                # longer pollute the parsed value (152-task interp-bind class).
                # Both streams remain retained as separate artifacts.
                proc = await self.sandbox.exec.aio(
                    "bash",
                    "-c",
                    command,
                    workdir=self.task["workdir"],
                    timeout=max(1, math.floor(self.remaining())),
                    text=False,
                )

                async def consume(stream, path):
                    size = 0
                    with path.open("ab") as output:
                        async for block in stream:
                            if not isinstance(block, bytes):
                                raise RuntimeFailure(
                                    "provider returned decoded rather than raw log bytes"
                                )
                            available = MAX_ARTIFACT_BYTES - size
                            output.write(block[:available])
                            size += len(block)
                            if size > MAX_ARTIFACT_BYTES:
                                raise RuntimeFailure(
                                    f"{phase} output exceeded retained-artifact limit"
                                )

                jobs = [
                    asyncio.create_task(consume(proc.stdout, paths["stdout"])),
                    asyncio.create_task(consume(proc.stderr, paths["stderr"])),
                    asyncio.create_task(proc.wait.aio()),
                ]
                try:
                    outcomes = await asyncio.gather(*jobs)
                    return outcomes[-1]
                finally:
                    for job in jobs:
                        if not job.done():
                            job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)

            code = await self.bounded(execute())
            complete = code != -1
            if code == -1:
                raise Deadline(f"{phase}: Modal exec wait returned timeout/unknown -1")
            return {"exit_code": code, "stdout": paths["stdout"], "stderr": paths["stderr"]}
        finally:
            for name, path in paths.items():
                self.arm["artifacts"][f"{phase}.{name}"] = _file_record(path, complete=complete)
                self.append(f"{phase} {name}", path)

    async def require(self, phase: str, command: str) -> dict:
        value = await self.command(phase, command)
        if value["exit_code"] == 124:
            raise Deadline(f"{phase} timed out")
        if value["exit_code"] != 0:
            raise RuntimeFailure(
                f"{phase} failed with exit {value['exit_code']}; see retained logs"
            )
        return value

    async def upload(self, remote: str, data: bytes) -> None:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise RuntimeFailure(f"input exceeds retained-artifact limit: {remote}")
        await self.bounded(self.sandbox.filesystem.write_bytes.aio(data, remote))

    async def download(self, phase: str, remote: str, *, optional: bool = False) -> Path | None:
        # exec+raw streaming preserves partial bytes even if provider retrieval fails.
        probe = await self.command(
            phase,
            f"if [ -f {shlex.quote(remote)} ]; then cat -- {shlex.quote(remote)}; else exit 44; fi",
        )
        if probe["exit_code"] == 44 and optional:
            return None
        if probe["exit_code"] != 0:
            raise RuntimeFailure(f"missing/unreadable remote artifact: {remote}")
        return probe["stdout"]

    async def probe_base(self, phase: str) -> str:
        workdir = shlex.quote(self.task["workdir"])
        result = await self.require(
            phase,
            "if [ -d /var/lib/mimo/git-hidden ]; then G=/var/lib/mimo/git-hidden; "
            f"else G=$(git -c safe.directory={workdir} rev-parse --absolute-git-dir) || exit 45; fi\n"
            "printf '%s\\n' \"$G\"\n"
            f"git -c safe.directory={workdir} --git-dir=\"$G\" rev-parse --verify 'HEAD^{{commit}}'",
        )
        data = result["stdout"].read_bytes().decode("utf-8").splitlines()
        if len(data) != 2 or not data[0].startswith("/") or not SHA40.fullmatch(data[1]):
            raise RuntimeFailure("image Git directory/base cannot be positively bound")
        self.git_dir = data[0]
        # Worktree dirt baseline: images ship with tracked files already
        # modified or deleted (108-task setup-clean-base class). Setup must not
        # add NEW tracked dirt; pre-existing dirt is recorded and tolerated
        # because both arms observe the identical image worktree.
        dirt = await self.command(
            f"{phase}-dirt",
            f"git -c safe.directory={workdir} --git-dir={shlex.quote(self.git_dir)} "
            f"--work-tree={workdir} status --porcelain=v1 --untracked-files=all",
        )
        if dirt["exit_code"] == 0:
            lines = dirt["stdout"].read_bytes().decode("utf-8", errors="replace").splitlines()
            self.arm[f"{phase}_dirt_lines"] = len(lines)
            if self._base_dirt is None:
                self._base_dirt = set(lines)
            self._last_dirt = set(lines)
        return data[1]

    async def extract(self, inputs: dict, extractor: bytes) -> None:
        remote_task = f"{STAGE}/task/{self.task['task_id']}"
        await self.upload(f"{STAGE}/extract.py", extractor)
        for name, data in inputs["files"].items():
            await self.upload(f"{remote_task}/{name}", data)
        # Probe every visible interpreter and bind the newest >= 3.7. PATH order
        # is not a version order (3.6-first images that also ship 3.9+), and
        # startup warnings on stderr must not pollute the parsed value now that
        # streams are separate. Exit 46: no interpreter; exit 47: all < 3.7.
        version = await self.require(
            "extractor-python",
            "best=''; bestv=0\n"
            "for P in $(command -v python3 2>/dev/null; command -v python 2>/dev/null; "
            "ls -d /usr/bin/python3* /usr/local/bin/python3* /opt/*/bin/python3* "
            "/root/.pyenv/versions/*/bin/python3* 2>/dev/null); do\n"
            '  v=$("$P" -c \'import sys; v=sys.version_info; '
            "print(v[0]*100+v[1]) if v >= (3, 7) else exit(47)' 2>/dev/null) || continue\n"
            '  case "$v" in *[!0-9]*) continue;; esac\n'
            '  if [ "$v" -gt "$bestv" ]; then best="$P"; bestv="$v"; fi\n'
            "done\n"
            '[ -n "$best" ] || exit 46\n'
            'printf \'%s\\n%s\\n\' "$best" "$bestv"',
        )
        picked = version["stdout"].read_bytes().decode()
        python, _version_code = _parse_interpreter_probe(picked)
        self.arm["interpreter"] = python
        self.arm["interpreter_version_code"] = _version_code
        process = await self.command(
            "extraction",
            f"{shlex.quote(python)} {STAGE}/extract.py --task {shlex.quote(remote_task)} "
            f"--git-dir {shlex.quote(self.git_dir)} --out {STAGE}/extraction",
        )
        evidence_path = await self.download(
            "extractor-evidence", f"{STAGE}/extraction/evidence.json"
        )
        evidence = json.loads(evidence_path.read_bytes())
        if not isinstance(evidence, dict):
            raise RuntimeFailure("extractor evidence is not an object")
        self.extraction = evidence
        self.extraction.update(
            {
                "task_id": self.task["task_id"],
                "run_digest": self.task["run_digest"],
                "image": self.task["image"],
                "test_patch_sha256": self.task["test_patch_sha256"],
                "evidence_path": str(evidence_path.resolve()),
                "evidence_sha256": _file_record(evidence_path)["sha256"],
                "extractor_sha256": _sha(extractor),
            }
        )
        # The extractor's own failure statuses (unsupported-tree,
        # unsupported-path, git-error, ...) are terminal evidence, not a
        # binding violation: preserve them so the sweep classifies each
        # distinctly instead of relabeling all 42 as "evidence differs".
        # Enforce the base/task binding only when the evidence carries a base.
        status = evidence.get("status")
        if status not in SUCCESS:
            if evidence.get("task") != self.task["task_id"]:
                raise RuntimeFailure(
                    "extractor evidence differs from actual selected task/image base"
                )
            if evidence.get("base") is not None and evidence.get("base") != self.arm["base"]:
                raise RuntimeFailure(
                    "extractor evidence differs from actual selected task/image base"
                )
            return
        if evidence.get("status") in SUCCESS:
            if process["exit_code"] != 0 or evidence.get("apply_check_on_base") is not True:
                raise RuntimeFailure(
                    "successful extractor status lacks successful execution/base apply check"
                )
            patch_path = await self.download("solution-patch", f"{STAGE}/extraction/solution.patch")
            patch = patch_path.read_bytes()
            if not patch.strip():
                raise RuntimeFailure("successful extractor returned an empty patch")
            self.patch = patch
            self.extraction.update(
                {
                    "solution_patch_path": str(patch_path.resolve()),
                    "solution_patch_sha256": _sha(patch),
                    "solution_patch_bytes": len(patch),
                }
            )
            self.arm["solution_patch_sha256"] = _sha(patch)

    async def setup(self, inputs: dict) -> None:
        value = await self.require("setup", inputs["healthcheck"])
        self.arm["setup_exit_code"] = value["exit_code"]
        before = self.arm["base"]
        after = await self.probe_base("setup-base")
        base_path = await self.download("setup-recorded-base", "/var/lib/mimo/base")
        recorded = base_path.read_bytes().decode("ascii").strip()
        if before != after or recorded != before:
            raise RuntimeFailure("selected setup changed or misrecorded the image's actual base")
        git = (
            f"git -c safe.directory={shlex.quote(self.task['workdir'])} "
            f"--git-dir={shlex.quote(self.git_dir)} --work-tree={shlex.quote(self.task['workdir'])}"
        )
        # The setup-base probe above already snapshotted post-setup dirt. Fail
        # only on NEW tracked dirt versus the image-base baseline: pre-existing
        # image dirt is identical in both arms, so the control stays balanced.
        # Untracked setup outputs were never gated (diff ignores them).
        current = await self.command("setup-clean-base", f"{git} diff --quiet HEAD --")
        post = self._last_dirt if self._last_dirt is not None else set()
        baseline = self._base_dirt if self._base_dirt is not None else set()
        new_tracked = sorted(
            line for line in (post - baseline) if line.strip() and not line.startswith("??")
        )
        self.arm["setup_new_tracked_dirt"] = new_tracked[:20]
        if current["exit_code"] != 0 and new_tracked:
            raise RuntimeFailure(
                "setup introduced new tracked worktree dirt: " + "; ".join(new_tracked[:10])
            )
        if current["exit_code"] not in (0, 1):
            raise RuntimeFailure(
                f"setup-clean-base failed with exit {current['exit_code']}; see retained logs"
            )
        self.arm["setup_base_bound"] = True

    async def apply(self, patch: bytes) -> None:
        await self.upload(f"{STAGE}/solution.patch", patch)
        git = (
            f"git -c safe.directory={shlex.quote(self.task['workdir'])} "
            f"--git-dir={shlex.quote(self.git_dir)} --work-tree={shlex.quote(self.task['workdir'])}"
        )
        await self.require("solution-check", f"{git} apply --check {STAGE}/solution.patch")
        await self.require("solution-apply", f"{git} apply {STAGE}/solution.patch")
        self.arm["solution_patch_applied"] = True
        self.arm["solution_patch_sha256"] = _sha(patch)

    async def grade(self, inputs: dict) -> None:
        # Setup deletes /tests and verifier logs; upload only after it finishes.
        await self.require(
            "verifier-clean",
            "mkdir -p /tests /logs/verifier && rm -f /logs/verifier/reward.txt /logs/verifier/test_output.log /logs/verifier/apply.log",
        )
        for name, data in inputs["files"].items():
            if name.startswith("tests/"):
                await self.upload("/" + name, data)
        process = await self.command("verifier", "bash /tests/test.sh")
        # Download full bytes, not test.sh's last-12000-byte console excerpt.
        reward_path = await self.download(
            "verifier-reward", "/logs/verifier/reward.txt", optional=True
        )
        output_path = await self.download(
            "verifier-full-output", "/logs/verifier/test_output.log", optional=True
        )
        apply_path = await self.download(
            "verifier-test-apply", "/logs/verifier/apply.log", optional=True
        )
        with process["stdout"].open("rb") as stream:
            stream.seek(max(0, process["stdout"].stat().st_size - 4096))
            tail = stream.read()
        outcome = _verifier_result(
            process["exit_code"],
            tail,
            reward_path.read_bytes() if reward_path is not None else None,
            complete=output_path is not None and apply_path is not None,
        )
        self.arm.update(outcome)
        self.arm["outer_verifier_exit_code"] = process["exit_code"]
        for name, path in (
            ("reward", reward_path),
            ("test_output", output_path),
            ("test_apply", apply_path),
        ):
            if path is not None:
                self.arm["artifacts"][name] = _file_record(path)

    def confirm_terminal(self, code: int | None) -> bool:
        if type(code) is not int:
            return False
        elapsed = (
            SANDBOX_SECONDS if self.started is None else math.ceil(time.monotonic() - self.started)
        )
        self.report.update(
            {
                "terminal_confirmed": True,
                "terminated_at": _utc(),
                "lifetime_upper_seconds": min(SANDBOX_SECONDS, max(0, elapsed)),
                "sandbox_exit_code": code,
            }
        )
        return True

    async def cleanup(self) -> None:
        if self.sandbox is None:
            return
        # Control-plane termination is shielded from caller cancellation. Process
        # waits have already been bounded by remaining lifetime. After expiry we
        # still attempt one termination RPC, without pretending expiry confirms it.
        cleanup_deadline = time.monotonic() + 30.0
        job = asyncio.create_task(self.sandbox.terminate.aio(wait=True))
        try:
            while True:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(job), max(0.001, cleanup_deadline - time.monotonic())
                    )
                    if not self.confirm_terminal(result):
                        raise RuntimeFailure("termination returned no confirmed terminal exit code")
                    return
                except asyncio.CancelledError:
                    self.cancelled = True
                    if time.monotonic() >= cleanup_deadline:
                        raise RuntimeFailure(
                            "cancellation interrupted terminal confirmation"
                        ) from None
        except Exception as exc:
            # Modal wait() caches an actual terminal result before raising
            # SandboxTimeoutError. Its public returncode is positive custody;
            # elapsed wall time alone remains insufficient.
            if self.confirm_terminal(getattr(self.sandbox, "returncode", None)):
                self.report["cleanup_notice"] = (
                    f"provider terminal result after {type(exc).__name__}: {exc}"
                )
            else:
                self.report["error"] = f"cleanup: {type(exc).__name__}: {exc}"
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    def retain(self) -> None:
        self.arm["cancelled"] = self.cancelled
        try:
            evidence = _file_record(
                self.log_path,
                complete=all(value["complete"] for value in self.arm["artifacts"].values()),
            )
            self.arm.update(
                {
                    "evidence_path": evidence["path"],
                    "evidence_sha256": evidence["sha256"],
                    "evidence_complete": evidence["complete"],
                }
            )
            _save_json(self.directory / "arm.json", self.arm)
            _save_json(self.directory / "sandbox.json", self.report)
        except Exception as exc:
            # Return allocation/cleanup metadata even when the local evidence
            # filesystem fails; admission must never mistake this for no spend.
            self.arm.update(
                {
                    "status": "infrastructure-error",
                    "evidence_complete": False,
                    "retention_error": f"{type(exc).__name__}: {exc}",
                    "reason": f"local evidence retention failed: {type(exc).__name__}: {exc}",
                }
            )

    async def run(
        self,
        inputs: dict,
        *,
        extractor: bytes | None = None,
        patch: bytes | None = None,
        expected_base: str | None = None,
        skip: str | None = None,
    ) -> tuple[dict, dict]:
        try:
            if skip is not None:
                self.arm["reason"] = skip
                return self.arm, self.report
            await self.create()
            try:
                self.arm["base"] = await self.probe_base("image-base")
            except Deadline:
                self.extraction = {
                    "status": "git-timeout",
                    "fix": None,
                    "rationale": "image Git probe censored by sandbox deadline",
                }
                raise
            except RuntimeFailure:
                self.extraction = {
                    "status": "unsupported-tree",
                    "fix": None,
                    "rationale": "image has no positively bound original Git base; setup-created baselines are not evidence",
                }
                raise
            if expected_base is not None and self.arm["base"] != expected_base:
                raise RuntimeFailure("fresh sandbox base differs from locked/extraction source")
            if extractor is not None:
                try:
                    await self.extract(inputs, extractor)
                except Deadline:
                    self.extraction = {
                        "status": "git-timeout",
                        "fix": None,
                        "rationale": "extraction censored by sandbox deadline",
                    }
                    raise
                except Exception as exc:
                    self.extraction = {
                        "status": "git-error",
                        "fix": None,
                        "rationale": f"{type(exc).__name__}: {exc}",
                    }
                    # Extraction failure must not fabricate a no-fix result, but
                    # setup still exercises the actual selected package.
            await self.setup(inputs)
            if extractor is not None:
                patch = self.patch
                if patch is None:
                    self.arm["reason"] = (
                        "extractor provided no usable source patch: " + self.extraction["status"]
                    )
                    if self.extraction["status"] not in {
                        "no-identifiable-fix",
                        "test-only-fix",
                        "empty-diff",
                        "patch-no-apply",
                        "needs-tip-decision",
                    }:
                        self.arm["status"] = "infrastructure-error"
                    return self.arm, self.report
            if patch is not None:
                await self.apply(patch)
            await self.grade(inputs)
        except asyncio.CancelledError:
            self.cancelled = True
            self.arm.update(
                {
                    "status": "infrastructure-error",
                    "reason": "cancelled before complete verifier evidence",
                }
            )
        except Deadline as exc:
            self.arm.update({"status": "timeout", "timed_out": True, "reason": str(exc)})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.arm.update({"status": "infrastructure-error", "reason": error})
            if self.report["creation_attempted"] and self.sandbox is None:
                self.report["error"] = "allocation unknown: " + error
        finally:
            if (
                self.report["creation_attempted"]
                and self.sandbox is None
                and "error" not in self.report
            ):
                self.report["error"] = "allocation unknown: " + self.arm.get(
                    "reason", "no provider receipt"
                )
            await self.cleanup()
            self.retain()
        return self.arm, self.report


async def run_pair(task: dict, app: Any, output_dir: Path, extractor_path: Path) -> dict:
    """Replay two fresh locked arms and retain a complete source-bound receipt."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    oracle_dir, nop_dir = directory / "oracle", directory / "nop"
    oracle_dir.mkdir(exist_ok=False)
    nop_dir.mkdir(exist_ok=False)
    receipt = {
        "schema_version": 1,
        "task_id": task["task_id"],
        **{key: task.get(key) for key in SOURCE_FIELDS},
        "selected_run": task.get("selected_run"),
        "original_run_digest": task.get("original_run_digest"),
        "method": "direct-modal-verifier-replay",
        "arms": {},
        "sandboxes": [],
    }
    oracle = _Session(task, app, oracle_dir, "locked")
    nop = _Session(task, app, nop_dir, "locked")
    try:
        inputs = _inputs(task)
        extractor = Path(extractor_path).read_bytes()
        if not extractor:
            raise RuntimeFailure("portable extractor file is empty")
        image, build = await _build_image(task, app)
        receipt["image_build"] = build
        receipt["cancelled"] = build.get("cancelled", False)
        if image is None:
            raise RuntimeFailure(build["reason"])
        oracle.image = nop.image = image
    except Exception as exc:
        reason = f"input preflight: {type(exc).__name__}: {exc}"
        receipt["extraction"] = {"status": "input-error", "fix": None, "rationale": reason}
        for name, session in (("oracle", oracle), ("nop", nop)):
            session.arm.update({"status": "infrastructure-error", "reason": reason})
            session.retain()
            receipt["arms"][name] = session.arm
            receipt["sandboxes"].append(session.report)
    else:
        oracle_arm, oracle_report = await oracle.run(inputs, extractor=extractor)
        nop_arm, nop_report = await nop.run(
            inputs, skip="pair cancelled before nop started" if oracle.cancelled else None
        )
        receipt["arms"] = {"oracle": oracle_arm, "nop": nop_arm}
        receipt["sandboxes"] = [oracle_report, nop_report]
        receipt["extraction"] = oracle.extraction
        # A successful nop remains usable when oracle extraction/setup fails.
        receipt["base"] = nop_arm.get("base") or oracle_arm.get("base")
        receipt["cancelled"] = oracle.cancelled or nop.cancelled
    receipt["execution_status"] = (
        "cancelled"
        if receipt.get("cancelled")
        else "timeout"
        if any(arm["status"] == "timeout" for arm in receipt["arms"].values())
        else "infrastructure-error"
        if any(arm["status"] == "infrastructure-error" for arm in receipt["arms"].values())
        else "complete"
    )
    receipt["execution_reason"] = (
        "; ".join(
            f"{name}: {arm['reason']}" for name, arm in receipt["arms"].items() if arm.get("reason")
        )
        or None
    )
    try:
        _save_json(directory / "runtime-receipt.json", receipt)
    except OSError as exc:
        receipt.update(
            {
                "execution_status": "infrastructure-error",
                "retention_error": f"{type(exc).__name__}: {exc}",
            }
        )
    return receipt


async def run_open(task: dict, locked_receipt: dict, app: Any, output_dir: Path) -> dict:
    """Confirm the exact retained locked patch in one fresh unlocked sandbox."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    extraction = locked_receipt.get("extraction", {})
    patch_sha = extraction.get("solution_patch_sha256") if isinstance(extraction, dict) else None
    session = _Session(task, app, directory, "open", patch_sha)
    try:
        if not isinstance(extraction, dict):
            raise RuntimeFailure("locked extraction is not an object")
        inputs = _inputs(task)
        for field in ("task_id", "run_digest", "image", "test_patch_sha256"):
            if task.get(field) != locked_receipt.get(field):
                raise RuntimeFailure(f"open confirmation source mismatch: {field}")
        base = locked_receipt.get("base")
        if not isinstance(base, str) or not SHA40.fullmatch(base):
            raise RuntimeFailure("locked receipt has no actual base")
        if (
            extraction.get("status") not in SUCCESS
            or extraction.get("apply_check_on_base") is not True
        ):
            raise RuntimeFailure("locked extraction is not a valid source patch")
        if extraction.get("base") != base:
            raise RuntimeFailure("locked extraction base differs from the retained source")
        for field in ("run_digest", "image", "test_patch_sha256"):
            if field in extraction and extraction[field] != locked_receipt.get(field):
                raise RuntimeFailure(f"locked extraction source mismatch: {field}")
        patch = Path(extraction["solution_patch_path"]).read_bytes()
        if not patch.strip() or _sha(patch) != extraction.get("solution_patch_sha256"):
            raise RuntimeFailure("retained locked patch is empty or differs from its digest")
        locked = locked_receipt.get("arms", {}).get("oracle", {})
        if (
            locked.get("solution_patch_sha256") != _sha(patch)
            or locked.get("solution_patch_applied") is not True
        ):
            raise RuntimeFailure("open patch differs from actually applied locked patch")
        for field in SOURCE_FIELDS:
            if locked.get(field) != locked_receipt.get(field):
                raise RuntimeFailure(f"locked oracle source mismatch: {field}")
        if locked.get("network_mode") != "locked":
            raise RuntimeFailure("open confirmation requires an actually locked source arm")
        image, build = await _build_image(task, app)
        session.arm["image_build"] = build
        if image is None:
            raise RuntimeFailure(build["reason"])
        session.image = image
    except Exception as exc:
        session.arm.update(
            {
                "status": "infrastructure-error",
                "reason": f"open preflight: {type(exc).__name__}: {exc}",
            }
        )
        session.retain()
    else:
        await session.run(inputs, patch=patch, expected_base=base)
        if session.arm["sandbox_id"] is not None and session.arm["sandbox_id"] in (
            locked.get("sandbox_id"),
            locked_receipt.get("arms", {}).get("nop", {}).get("sandbox_id"),
        ):
            session.arm.update(
                {
                    "status": "infrastructure-error",
                    "reason": "provider returned a nonfresh sandbox identity",
                }
            )
    result = {"arm": session.arm, "sandbox": session.report}
    try:
        _save_json(directory / "runtime-receipt.json", result)
    except OSError as exc:
        session.arm.update(
            {"status": "infrastructure-error", "retention_error": f"{type(exc).__name__}: {exc}"}
        )
    return result
