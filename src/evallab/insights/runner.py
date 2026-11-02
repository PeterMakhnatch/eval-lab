"""Headless analyst processes, quota gates and served-model receipts.

A pass owns its process group and files. No fallback is allowed: session records,
not the requested route, establish which model actually answered.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import yaml

from evallab.insights.config import model_family

_MODEL_ROLES = (
    "default", "plan", "slow", "vision", "advisor", "task", "smol", "tiny",
    "commit", "memory", "judge",
)
_THINKING_SUFFIX = re.compile(r":(?:off|minimal|low|medium|high|xhigh|max)$")
QUOTA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"quota\s+exhausted", r"quota-exhausted", r"resource_exhausted",
        r"resourceexhausted", r"\b429\b", r"rate[-_ ]limit", r"ratelimit",
        r"too\s+many\s+requests", r"exceeded\s+(?:your\s+)?(?:current\s+)?quota",
        r"quota\s+ceiling", r"ceiling\s+reached", r"daily\s+quota", r"weekly\s+quota",
    )
)


@dataclass(frozen=True)
class WorkerSpec:
    pass_id: str
    route: str
    thinking: str
    max_minutes: float
    pass_dir: Path
    prompt_file: Path
    tools: str


class WorkerHandle(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


class Launcher(Protocol):
    def launch(self, spec: WorkerSpec) -> WorkerHandle: ...

    def adopt(self, pid: int, pass_dir: Path) -> WorkerHandle | None: ...


class _ChildProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...


class _Popen(Protocol):
    def __call__(
        self, args: list[str], *, cwd: Path, env: dict[str, str], stdin: int,
        stdout: BinaryIO, stderr: BinaryIO | int, start_new_session: bool,
    ) -> _ChildProcess: ...


class _Run(Protocol):
    def __call__(
        self, args: list[str], *, capture_output: bool, text: bool, timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout if result.returncode == 0 else ""


class _ProcessHandle:
    def __init__(
        self,
        pid: int,
        poll: Callable[[], int | None],
        *,
        grace_seconds: float = 5,
        verify: Callable[[], bool] | None = None,
    ) -> None:
        self.pid = pid
        self._poll = poll
        self._grace_seconds = grace_seconds
        self._verify = verify

    def poll(self) -> int | None:
        if self._verify is not None and not self._verify():
            # A reused PID is not our worker. Unknown exit must never be success.
            return 1
        return self._poll()

    def kill(self) -> None:
        if self.poll() is not None:
            return
        if self._verify is not None and not self._verify():
            return
        # Workers start new sessions, so PID is also the group ID. Never look up
        # a reused PID's group and accidentally signal the supervisor's group.
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self._grace_seconds
        while time.monotonic() < deadline:
            if self.poll() is not None:
                return
            time.sleep(0.1)
        if self._verify is not None and not self._verify():
            return
        with suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGKILL)


class OmpLauncher:
    def __init__(
        self,
        omp: str = "omp",
        popen: _Popen = subprocess.Popen,
        ps: Callable[[int], str] = _process_command,
    ) -> None:
        self.omp = omp
        self._popen = popen
        self._ps = ps

    def launch(self, spec: WorkerSpec) -> WorkerHandle:
        pass_dir = spec.pass_dir.resolve()
        pass_dir.mkdir(parents=True, exist_ok=True)
        session_dir = pass_dir / "session"
        session_dir.mkdir(exist_ok=True)
        config_file = pass_dir / "worker.yml"
        overlay = {
            "retry": {"modelFallback": False, "maxRetries": 4},
            "modelRoles": {key: f"{spec.route}:{spec.thinking}" for key in _MODEL_ROLES},
            "mnemopi": {"autoRetain": False, "autoRecall": False},
            "autolearn": {"enabled": False},
            "bash": {"autoBackground": {"enabled": False}},
        }
        config_file.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
        argv = [
            self.omp, "-p", "--model", spec.route, "--thinking", spec.thinking,
            "--session-dir", str(session_dir), "--no-skills", "--tools", spec.tools,
            "--max-time", f"{spec.max_minutes * 60:g}", "--config", str(config_file),
            f"@{spec.prompt_file.resolve()}",
        ]
        env = {
            **{key: value for key, value in os.environ.items() if key != "EVALLAB_INSIGHTS_STATE"},
            "HARBOR_TELEMETRY": "off",
            "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
            "EVALLAB_INSIGHTS_PASS": spec.pass_id,
        }
        # Closing the parent's descriptors is safe: Popen duplicated them for the child.
        # Answer text and OMP diagnostics stay separate so prose about quotas can
        # never be mistaken for a provider error.
        with (pass_dir / "worker.log").open("ab") as log, (
            pass_dir / "worker.stderr"
        ).open("ab") as errors:
            child = self._popen(
                argv, cwd=pass_dir, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=errors, start_new_session=True,
            )
        return _ProcessHandle(child.pid, child.poll)

    @staticmethod
    def _matches_session(command: str, session: str) -> bool:
        # ps output is not shell-quoted. Match the raw path (including spaces)
        # with an argument boundary; also accept a quoted path for compatibility.
        pattern = (
            r"(?:^|\s)--session-dir(?:=|\s+)(?P<quote>['\"]?)"
            + re.escape(session)
            + r"(?P=quote)(?=$|\s)"
        )
        return re.search(pattern, command) is not None

    def _adopted_command_matches(self, pid: int, session: str) -> bool:
        try:
            return _alive(pid) and self._matches_session(self._ps(pid), session)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def adopt(self, pid: int, pass_dir: Path) -> WorkerHandle | None:
        session = str(pass_dir.resolve() / "session")
        if not self._adopted_command_matches(pid, session):
            return None
        # A non-child's exit code is unavailable. Never invent a successful exit.
        return _ProcessHandle(
            pid,
            lambda: None if _alive(pid) else 1,
            verify=lambda: self._adopted_command_matches(pid, session),
        )


@dataclass(frozen=True)
class Attestation:
    served: dict[str, int]
    off_route: bool
    fallback_events: int
    error_records: int
    tokens: dict[str, int]
    model_changes: tuple[str, ...] = ()
    diagnostics: str = ""


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _fallback(value: object) -> bool:
    if isinstance(value, dict):
        obj = _object(value)
        return obj.get("resolvedModelIsFallback") is True or any(
            _fallback(child) for child in obj.values()
        )
    return isinstance(value, list) and any(_fallback(child) for child in _items(value))


def _route(route: str) -> str:
    return _THINKING_SUFFIX.sub("", route)


def attest_session(session_dir: Path, requested: str) -> Attestation:
    served: Counter[str] = Counter()
    tokens = {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "reasoningTokens": 0,
    }
    fallback_events = errors = 0
    changes: list[str] = []
    diagnostics: list[str] = []
    for path in sorted(session_dir.rglob("*.jsonl")):
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = _object(json.loads(line))
                    except ValueError:
                        # A running worker may still be appending its final record.
                        continue
                    fallback_events += int(_fallback(event))
                    if event.get("type") == "model_change":
                        model = event.get("model")
                        if isinstance(model, str):
                            changes.append(_route(model))
                    message = _object(event.get("message"))
                    if message.get("role") != "assistant":
                        if event.get("type") == "error":
                            errors += 1
                            diagnostics.append(json.dumps(event, default=str))
                        continue
                    is_error = message.get("stopReason") == "error"
                    errors += int(is_error)
                    if is_error:
                        diagnostics.append(json.dumps(message, default=str))
                    provider, model = message.get("provider"), message.get("model")
                    if not is_error and isinstance(provider, str) and isinstance(model, str):
                        served[_route(f"{provider}/{model}")] += 1
                    usage = _object(message.get("usage"))
                    for name in tokens:
                        count = usage.get(name)
                        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                            tokens[name] += count
        except OSError:
            continue
    return Attestation(
        served=dict(served), off_route=any(model != _route(requested) for model in served),
        fallback_events=fallback_events, error_records=errors, tokens=tokens,
        model_changes=tuple(changes), diagnostics="\n".join(diagnostics)[-20_000:],
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quota_reset(text: str) -> datetime | None:
    iso = re.search(
        r"resets?\s+(?:at|on)\s+(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", text, re.IGNORECASE,
    )
    if iso:
        try:
            parsed = datetime.fromisoformat(iso[1].replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            pass
    relative = re.search(
        r"(?:resets?\s+in|retry[- ]after:?|reset[- ]after:?)\s+(\d+(?:\.\d+)?)"
        r"\s*(?:s|sec|seconds)?", text, re.IGNORECASE,
    )
    if relative:
        try:
            return _utc_now() + timedelta(seconds=float(relative[1]))
        except (ValueError, OverflowError):
            pass
    epoch = re.search(r"resets?\s+at\s+(\d{10}(?:\.\d+)?)", text, re.IGNORECASE)
    if epoch:
        try:
            return datetime.fromtimestamp(float(epoch[1]), UTC)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def classify_failure(log_text: str) -> tuple[str, datetime | None]:
    if any(pattern.search(log_text) for pattern in QUOTA_PATTERNS):
        return "quota", _quota_reset(log_text)
    if re.search(r"\b401\b|invalid_grant|login\s+expired", log_text, re.IGNORECASE):
        return "auth", None
    if re.search(r"\b403\b|permission\s+denied|not\s+allowed", log_text, re.IGNORECASE):
        return "permission", None
    if re.search(
        r"\brefus(?:al|ed|ing)\b|cannot\s+(?:assist|comply)|can't\s+(?:assist|comply)",
        log_text, re.IGNORECASE,
    ):
        return "refusal", None
    return "other", None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _limit_percent(limit: dict[str, object]) -> float | None:
    amount = _object(limit.get("amount"))
    fraction = _number(amount.get("usedFraction"))
    if fraction is not None:
        return max(0.0, fraction * 100)
    used, ceiling = _number(amount.get("used")), _number(amount.get("limit"))
    if used is not None and ceiling is not None and ceiling > 0:
        return max(0.0, used / ceiling * 100)
    if used is not None and amount.get("unit") == "percent":
        return max(0.0, used)
    return None


def _antigravity_bucket(limit: dict[str, object]) -> str | None:
    label = str(limit.get("label", "")).lower()
    identity = str(limit.get("id", "")).lower()
    if "gemini" in label or ":google:" in identity:
        return "google"
    if "claude" in label or "gpt" in label or "3p-" in identity:
        return "third_party"
    return None


class UsageGate:
    """Cache one full usage report; choose antigravity's bucket by model family."""

    def __init__(
        self, run: _Run = subprocess.run, clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 600,
    ) -> None:
        self._run = run
        self._clock = clock
        self._ttl = ttl_seconds
        self._fetched: float | None = None
        self._usage: dict[tuple[str, str | None], float] = {}

    def _refresh(self) -> None:
        now = self._clock()
        if self._fetched is not None and 0 <= now - self._fetched < self._ttl:
            return
        self._fetched = now
        self._usage = {}
        try:
            result = self._run(
                ["omp", "usage", "--json"], capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return
            payload = _object(json.loads(result.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        for raw_report in _items(payload.get("reports")):
            report = _object(raw_report)
            provider = report.get("provider")
            if not isinstance(provider, str):
                continue
            for raw_limit in _items(report.get("limits")):
                limit = _object(raw_limit)
                used = _limit_percent(limit)
                if used is None:
                    continue
                keys: list[tuple[str, str | None]] = [(provider, None)]
                if provider == "google-antigravity":
                    bucket = _antigravity_bucket(limit)
                    if bucket is not None:
                        keys.append((provider, bucket))
                for key in keys:
                    self._usage[key] = max(used, self._usage.get(key, 0.0))

    def used_percent(self, provider: str, model: str | None = None) -> float | None:
        self._refresh()
        if provider == "google-antigravity" and model is not None:
            family = model_family(model)
            if family == "google":
                return self._usage.get((provider, "google"))
            if family in {"anthropic", "openai"}:
                return self._usage.get((provider, "third_party"))
        return self._usage.get((provider, None))


class MemoryGate:
    """Track current macOS pressure and swap growth between engine checks."""

    def __init__(
        self, *, run: _Run = subprocess.run, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._run = run
        self._clock = clock
        self._swap_samples: list[tuple[float, float]] = []

    def _value(self, key: str) -> str | None:
        try:
            result = self._run(
                ["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, timeout=5
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    @staticmethod
    def _swap_gb(text: str | None) -> float | None:
        if text is None:
            return None
        matched = re.search(r"used\s*=\s*([\d.]+)([MG])", text)
        if matched is None:
            return None
        try:
            return float(matched[1]) / (1024 if matched[2] == "M" else 1)
        except ValueError:
            return None

    def status(self, min_memory_level: int, max_swap_growth_gb: float) -> str | None:
        pressure_text = self._value("kern.memorystatus_vm_pressure_level")
        free_text = self._value("kern.memorystatus_level")
        swap_gb = self._swap_gb(self._value("vm.swapusage"))
        try:
            pressure = int(pressure_text.strip()) if pressure_text is not None else None
            free = int(free_text.strip()) if free_text is not None else None
        except ValueError:
            pressure = free = None
        if pressure == 4:
            return "critical memory pressure"
        if pressure == 2 and free is not None and free < min_memory_level:
            return f"warn memory pressure with {free}% free memory (need {min_memory_level}%)"

        now = self._clock()
        if swap_gb is not None:
            self._swap_samples.append((now, swap_gb))
            self._swap_samples = [sample for sample in self._swap_samples if now - sample[0] <= 900]
            cutoff = now - 300
            earlier = [sample for sample in self._swap_samples[:-1] if sample[0] <= cutoff]
            if earlier:
                stamp, previous = earlier[-1]
                growth = swap_gb - previous
                elapsed_minutes = max(1, round((now - stamp) / 60))
                if growth > max_swap_growth_gb:
                    return f"swap grew {growth:.1f} GB in {elapsed_minutes} min"
        return None


def host_ok(
    min_memory_level: int, max_swap_growth_gb: float, run: _Run = subprocess.run,
) -> bool:
    """One stateless probe; the engine uses MemoryGate to measure swap growth."""
    return MemoryGate(run=run).status(min_memory_level, max_swap_growth_gb) is None
