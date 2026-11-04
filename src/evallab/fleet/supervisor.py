"""Bounded route, quota, recovery and power supervision for owned OMP lanes."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from evallab.fleet.completion import verify
from evallab.fleet.state import Lane, Store, default_state
from evallab.insights.runner import (
    OmpLauncher,
    UsageGate,
    WorkerHandle,
    WorkerSpec,
    attest_session,
    transient_retry_delay,
)

ACTIVE = {"running", "paused", "recovering", "verifying"}
TOOLS = "read,bash,grep,glob,write,edit"


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def note(lane: Lane, text: str) -> None:
    stamp = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    append(lane.directory / "STATUS.md", f"{stamp} | fleet | {text}")


class Supervisor:
    def __init__(self, store: Store, *, omp: str = "omp", clock=time.time) -> None:
        self.store = store
        self.omp = omp
        self.clock = clock
        self.launcher = OmpLauncher(omp=omp)
        self.gate = UsageGate(run=self._usage, ttl_seconds=180)
        self.awake: subprocess.Popen | None = None
        self.children: dict[int, WorkerHandle] = {}
        self.verifiers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fleet-verify")
        self.pending: dict[str, Future[dict]] = {}

    def _usage(
        self,
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.omp, *args[1:]],
            capture_output=capture_output,
            text=text,
            timeout=10,
        )

    def config(self) -> dict:
        path = self.store.directory / "config.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def quota_reason(self, route: str) -> str:
        provider, model = route.split("/", 1)
        threshold = self.config().get("thresholds", {"openai-codex": 75}).get(provider)
        if threshold is None:
            return ""
        used = self.gate.used_percent(provider, model)
        if used is None:
            return f"Quota is unavailable for {provider}"
        return f"{provider} quota {used:g}% reaches {threshold:g}%" if used >= threshold else ""

    def launch(self, lane: Lane, *, resume: Path | None = None) -> None:
        prompt = Path(lane.brief)
        if resume:
            prompt = self.store.directory / f"{lane.name}-resume.md"
            prompt.write_text(
                f"Continue the assigned work from {lane.directory / 'STATUS.md'}. "
                f"Read the original brief at {lane.brief}. Do not repeat completed work. "
                "Finish the acceptance runs and write RESULT.md.\n"
            )
        spec = WorkerSpec(
            pass_id=f"fleet-{lane.name}",
            route=lane.route,
            thinking="medium",
            max_minutes=240,
            pass_dir=lane.directory,
            prompt_file=prompt,
            tools=TOOLS,
            cwd=Path(lane.worktree) if lane.worktree else lane.directory,
            resume=resume,
        )
        handle = self.launcher.launch(spec)
        lane.pid = handle.pid
        lane.pgid = handle.pid
        self.children[handle.pid] = handle
        lane.command = subprocess.run(
            ["ps", "-p", str(handle.pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        lane.status = "running"
        lane.reason = ""
        self.store.event(
            self.clock(),
            lane,
            action="resume" if resume else "launch",
            session=str(resume),
            pid=lane.pid,
            pgid=lane.pgid,
            command=lane.command,
        )
        note(lane, f"{'resume' if resume else 'launch'} pid={lane.pid} route={lane.route}")

    def start(
        self,
        program: Path,
        name: str,
        route: str,
        brief: Path,
        *,
        worktree: Path | None = None,
        approval: str | None = None,
        max_resumes: int = 2,
    ) -> Lane:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or "/" not in route:
            raise ValueError("Use a simple lane name and an explicit provider/model route")
        if not brief.is_file():
            raise ValueError(f"Brief does not exist: {brief}")
        if route.split("/", 1)[0] != "openai-codex" and not (approval and approval.strip()):
            raise ValueError("Paid routes require --approval with the current explicit approval")
        if max_resumes < 0:
            raise ValueError("max-resumes must be nonnegative")
        with self.store.transaction():
            if any(lane.name == name for lane in self.store.lanes()):
                raise ValueError(
                    f"Lane {name} already exists; preserve its evidence and use a new name"
                )
            reason = self.quota_reason(route)
            if reason:
                raise ValueError(reason)
            lane = Lane(
                name,
                str(program.resolve()),
                route,
                str(brief.resolve()),
                str(worktree.resolve()) if worktree else None,
                approval,
                max_resumes=max_resumes,
            )
            lane.directory.mkdir(parents=True, exist_ok=True)
            if (lane.directory / "RESULT.md").exists() or any(
                (lane.directory / "session").glob("*.jsonl")
            ):
                raise ValueError(
                    "Lane contains previous completion or session evidence; use a fresh lane"
                )
            self.store.save(lane)
            self.launch(lane)
            self.store.save(lane)
        return lane

    def signal(self, lane: Lane, sig: int) -> None:
        # Verify ownership before touching a saved PID. Include descendants and
        # helper OMP processes, which can have separate process groups.
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True, timeout=5
        )
        if result.returncode:
            raise ValueError("Cannot verify lane process ownership")
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 3)
            if len(fields) == 4:
                rows[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3])
        root = rows.get(lane.pid)
        session = str(lane.directory / "session")
        if root and not (
            self.launcher._matches_session(root[2], session) or "<defunct>" in root[2]
        ):
            raise ValueError(
                "Saved lane PID no longer belongs to this session; refusing to signal it"
            )
        owned = {lane.pid}
        pattern = re.compile(
            r"(?:^|\s)--session-dir(?:=|\s+)[\"']?" + re.escape(str(lane.directory)) + r"/"
        )
        owned.update(pid for pid, (_, _, command) in rows.items() if pattern.search(command))
        changed = True
        while changed:
            descendants = {pid for pid, (parent, _, _) in rows.items() if parent in owned}
            changed = not descendants <= owned
            owned |= descendants
        groups = {lane.pgid} | {rows[pid][1] for pid in owned if pid in rows}
        for group in groups:
            if group > 1 and group != os.getpgrp():
                with suppress(ProcessLookupError):
                    os.killpg(group, sig)

    def stop(self, lane: Lane, reason: str, status: str = "stopped") -> None:
        self.signal(lane, signal.SIGKILL)
        lane.status, lane.reason = status, reason
        self.store.event(self.clock(), lane, action=status, reason=reason)
        note(lane, reason)

    def attest(self, lane: Lane) -> bool:
        # Persist journal offsets with counts in the same transaction. Partial
        # final records remain pending. New helper/native journals start at 0.
        served: Counter[str] = Counter()
        off_route = False
        for path in lane.directory.rglob("*.jsonl"):
            stat = path.stat()
            stamp = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
            cached: dict | None = lane.files.get(str(path))
            if not cached or cached["stamp"] != stamp:
                if (
                    not cached
                    or "offset" not in cached
                    or cached["stamp"][0] != stat.st_ino
                    or stat.st_size < cached["stamp"][1]
                ):
                    cached = {"served": {}, "off_route": False, "fallback_events": 0, "offset": 0}
                positions = {str(path): cached["offset"]}
                receipt = attest_session(
                    lane.directory,
                    lane.route,
                    paths=[path],
                    include_errors=True,
                    positions=positions,
                )
                counts = Counter(cached["served"])
                counts.update(receipt.served)
                cached.update(
                    stamp=stamp,
                    offset=positions[str(path)],
                    served=dict(counts),
                    off_route=cached["off_route"] or receipt.off_route,
                    fallback_events=cached["fallback_events"] + receipt.fallback_events,
                    diagnostics=receipt.diagnostics,
                )
                lane.files[str(path)] = cached
        for cached in lane.files.values():
            served.update({str(key): int(value) for key, value in cached["served"].items()})
            off_route |= bool(cached["off_route"] or cached["fallback_events"])
        lane.served = dict(served)
        return off_route

    def power(self, active: bool) -> None:
        if active and (self.awake is None or self.awake.poll() is not None):
            self.awake = subprocess.Popen(
                ["caffeinate", "-is", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif not active and self.awake:
            self.awake.terminate()
            self.awake.wait(timeout=5)
            self.awake = None

    def tick(self) -> None:
        now = self.clock()
        with self.store.transaction():
            previous = self.store.meta("heartbeat", now)
            lanes = self.store.lanes()
            active = [lane for lane in lanes if lane.status in ACTIVE]
            self.power(bool(active))
            if now - previous > 45 or now < previous:
                receipt = self.store.directory / f"sleep-{int(now)}.log"
                for lane in active:
                    text = f"ALERT supervisor clock gap {now - previous:.1f} seconds; sleep evidence {receipt}"
                    note(lane, text)
                    append(Path(lane.program) / "ALERTS.md", text)
                    self.store.event(
                        now, lane, action="sleep-alert", gap=now - previous, receipt=str(receipt)
                    )
                with receipt.open("w") as stream:
                    try:
                        result = subprocess.run(
                            ["pmset", "-g", "log"],
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            text=True,
                            timeout=2,
                        )
                        if result.stdout:
                            stream.write(result.stdout)
                        if result.returncode:
                            stream.write(f"\npmset exits with code {result.returncode}\n")
                    except (OSError, subprocess.TimeoutExpired) as error:
                        stream.write(f"\npmset evidence is incomplete: {error}\n")
            for lane in active:
                if self.attest(lane):
                    self.stop(
                        lane,
                        f"Route attestation fails (off-route, unverifiable turn, or fallback): {lane.served}",
                        "route-violation",
                    )
                elif lane.status == "verifying":
                    future = self.pending.get(lane.name)
                    if future is None:
                        self.pending[lane.name] = self.verifiers.submit(
                            verify,
                            lane,
                            hook=self.config().get("verify_command"),
                        )
                    elif future.done():
                        lane.verification = future.result()
                        del self.pending[lane.name]
                        lane.status = (
                            "done-verified" if lane.verification["verified"] else "done-unverified"
                        )
                        lane.reason = "; ".join(lane.verification["reasons"])
                        note(lane, f"{lane.status}: {lane.reason}")
                        self.store.event(
                            now, lane, action=lane.status, verification=lane.verification
                        )
                else:
                    handle = self.children.get(lane.pid) or self.launcher.adopt(
                        lane.pid, lane.directory
                    )
                    alive = handle is not None and handle.poll() is None
                    reason = self.quota_reason(lane.route)
                    if alive:
                        if reason and lane.status != "paused":
                            self.signal(lane, signal.SIGSTOP)
                            lane.status, lane.reason = "paused", reason
                            note(lane, reason)
                            self.store.event(now, lane, action="pause", reason=reason)
                        elif not reason and lane.status == "paused":
                            self.signal(lane, signal.SIGCONT)
                            lane.status, lane.reason = "running", ""
                            self.store.event(now, lane, action="continue")
                    elif (lane.directory / "RESULT.md").exists():
                        self.signal(lane, signal.SIGKILL)
                        lane.status = "verifying"
                        self.pending[lane.name] = self.verifiers.submit(
                            verify,
                            lane,
                            hook=self.config().get("verify_command"),
                        )
                    elif lane.resumes >= lane.max_resumes:
                        self.stop(
                            lane, "Worker exited without RESULT.md; resume limit reached", "failed"
                        )
                    elif not reason:
                        if lane.status != "recovering":
                            self.signal(lane, signal.SIGKILL)
                            lane.status = "recovering"
                            lane.next_launch = (
                                now
                                + transient_retry_delay(
                                    lane.name + str(lane.resumes)
                                ).total_seconds()
                            )
                            note(
                                lane,
                                "Worker exited without RESULT.md; scheduling same-session resume",
                            )
                        elif now >= lane.next_launch:
                            sessions = sorted(
                                (lane.directory / "session").glob("*.jsonl"),
                                key=lambda path: path.stat().st_mtime_ns,
                            )
                            lane.resumes += 1
                            self.launch(lane, resume=sessions[-1] if sessions else None)
                    else:
                        lane.reason = reason
                self.store.save(lane)
            self.store.set_meta("heartbeat", now)
        snapshot = {
            "heartbeat": now,
            "pid": os.getpid(),
            "lanes": [asdict(lane) for lane in self.store.lanes()],
        }
        temporary = self.store.directory / "status.tmp"
        temporary.write_text(json.dumps(snapshot, indent=2))
        temporary.replace(self.store.directory / "status.json")
        ledger = self.store.directory / "spend.jsonl"
        for lane in self.store.lanes():
            append(
                ledger,
                json.dumps(
                    {
                        "at": now,
                        "lane": lane.name,
                        "served_turns": lane.served,
                        "approval": lane.approval,
                        "kind": "cumulative-snapshot",
                    }
                ),
            )
        self.power(any(lane.status in ACTIVE for lane in self.store.lanes()))

    def watch(self, interval: float = 10) -> None:
        if not 0 < interval <= 15:
            raise ValueError("Watch interval must be greater than 0 and at most 15 seconds")
        # One supervisor per machine, even if a caller selects another state DB.
        lockdir = default_state()
        lockdir.mkdir(parents=True, exist_ok=True)
        with (lockdir / "watch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("A fleet supervisor already runs on this machine") from error
            try:
                self.tick()
                print("Fleet supervisor ready.", flush=True)
                while True:
                    started = time.monotonic()
                    self.tick()
                    time.sleep(max(0, interval - (time.monotonic() - started)))
            finally:
                self.power(False)
