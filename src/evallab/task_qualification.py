"""Backend qualification: per-trial task health on docker vs Daytona (HAR-88).

A qualification row says whether one Harbor trial proves its task version
*runnable* on the backend it ran on. It is the Stage 1 (controls) ledger from
``docs/task-quality.md``: a nop trial with reward exactly 0 and a completed
verifier is ``ok``; anything that shows the task cannot be set up, graded, or
trusted on that backend is ``broken`` with machine-readable reasons.

``evallab tasks qualify-collect <job_dir>...`` writes
``task_qualification.parquet`` into the task-catalog directory; ``evallab
tasks catalog export-broken`` publishes the per-backend broken list.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.evidence.facts import exception_phase_for, trial_environment_type
from evallab.results import duration_seconds, load_job
from evallab.task_stability import iter_trial_dirs, parse_stability_file

#: Catalog table written by :func:`write_task_qualification_parquet`.
TABLE_FILENAME = "task_qualification.parquet"
TABLE = Path(TABLE_FILENAME).stem

#: Content-addressed broken-list export schema.
BROKEN_EXPORT_SCHEMA = "evallab.task_broken/v1"

STATUSES = ("ok", "broken", "inconclusive")

REASONS = (
    "setup_failed",
    "backend_quota",
    "verifier_error",
    "verifier_timeout",
    "reward_missing",
    "nop_passes",
    "unstable_verifier",
    "grader_broken",
)

CONTROL_AGENTS = frozenset({"nop", "oracle"})

#: Daytona list prices (https://www.daytona.io/pricing, retrieved 2026-09-28):
#: vCPU $0.0504/h, memory $0.0162/GiB-h, storage $0.000108/GiB-h after the
#: first 5 GiB free. Cost is only computed for trials whose backend matches
#: the active rate card; every other backend gets null.
DAYTONA_RATE_CARD = {
    "backend": "daytona",
    "vcpu_usd_per_hour": 0.0504,
    "memory_usd_per_gib_hour": 0.0162,
    "storage_usd_per_gib_hour": 0.000108,
    "storage_free_gib": 5.0,
    "source": "https://www.daytona.io/pricing",
    "retrieved": "2026-09-28",
}

RATE_CARDS = {"daytona": DAYTONA_RATE_CARD}

#: Case-insensitive quota indicators matched against
#: ``exception_type + exception_message``. Bare ``429``/``403`` only count
#: with quota wording nearby (tracebacks routinely cite line numbers), and
#: bare ``disk``/``storage`` never count: only precise capacity markers do
#: (Daytona sandboxes are capped at 4 vCPU / 8 GiB RAM / 10 GiB disk whatever
#: the org tier, so a setup-phase capacity failure is provider quota, never
#: a task defect).
_QUOTA_SUBSTRINGS = (
    "quota",
    "limit exceeded",
    "total cpu",
    "memory limit",
    "disk limit",
    "storage limit",
    "disk quota",
    "no space left on device",
    "insufficient disk",
    "insufficient storage",
    "rate limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
    "daytonaratelimit",
)
_HTTP_STATUS_RE = re.compile(r"\b(429|403)\b")
#: ``exceed(s|ed|ing)`` within ~40 chars of ``disk``/``storage`` (either
#: order), e.g. "disk size exceeds the allowed limit".
_DISK_EXCEEDS_RE = re.compile(
    r"\bexceed\w*\b.{0,40}\b(disk|storage)\b|\b(disk|storage)\b.{0,40}\bexceed\w*\b"
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def is_backend_quota_failure(
    exception_type: str | None, exception_message: str | None
) -> bool:
    """True when the exception text blames a provider quota/limit, not the task."""
    text = f"{exception_type or ''} {exception_message or ''}".lower()
    if any(marker in text for marker in _QUOTA_SUBSTRINGS):
        return True
    if _DISK_EXCEEDS_RE.search(text):
        return True
    return bool(_HTTP_STATUS_RE.search(text)) and any(
        word in text for word in ("limit", "quota", "rate")
    )

#: Pytest collection-failure markers: the grader's own test modules failed to
#: import, so every run scores 0 whatever the agent does.
_COLLECTION_MARKERS = (
    "error collecting",
    "error during collection",
    "errors during collection",
)
#: Error lines proving the collection failure is a broken test module
#: (a missing third-party dependency, a syntax error, or an ``ImportError``
#: while importing the test module), in pytest's ``E``-prefixed or plain
#: form. Ordered by specificity: the reported ``grader_error`` is the most
#: specific (root-cause) match, e.g. the ``ModuleNotFoundError`` line rather
#: than the ``ImportError while importing`` header above it.
_GRADER_ERROR_RES = (
    (0, re.compile(r"^\s*E?\s*ModuleNotFoundError: No module named '(?P<module>[^']+)'.*$")),
    (1, re.compile(r"^\s*E?\s*SyntaxError(?::|\s).*$")),
    (2, re.compile(r"^\s*E?\s*ImportError while importing test module\b.*$")),
)
_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _mentioned_as_word(text: str, word: str) -> bool:
    """True when ``word`` appears as a whole word in ``text`` (nop-guard check)."""
    try:
        pattern = _WORD_RE_CACHE[word]
    except KeyError:
        pattern = _WORD_RE_CACHE[word] = re.compile(r"\b" + re.escape(word) + r"\b")
    return pattern.search(text) is not None


def detect_grader_collection_failure(
    stdout_texts: Sequence[str], *, instruction_text: str | None
) -> str | None:
    """Error line when grader pytest collection is broken, else null.

    Flags pytest collection failures (``ERROR collecting`` /
    ``error during collection``) whose cause is an ``ImportError`` /
    ``ModuleNotFoundError`` / ``SyntaxError`` inside a test module, and
    returns the most specific matched error line (e.g.
    ``ModuleNotFoundError: No module named 'stevedore'``).

    Nop false-positive guard: some graders import a module the agent is
    supposed to create (e.g. ``solution``). When the missing module's
    top-level name appears as a word in the task's ``instruction.md``, the
    import failure is expected under nop — not a grader defect — and that
    match is skipped. A missing instruction (unreadable task dir) never
    guards: without the task text there is no evidence the import is
    expected.
    """
    combined = "\n".join(stdout_texts)
    if not any(marker in combined.lower() for marker in _COLLECTION_MARKERS):
        return None
    candidates: list[tuple[int, int, str, str | None]] = []
    for lineno, line in enumerate(combined.splitlines()):
        for specificity, pattern in _GRADER_ERROR_RES:
            match = pattern.match(line)
            if match is None:
                continue
            error = re.sub(r"^E\s+", "", line.strip())
            candidates.append((specificity, lineno, error, match.groupdict().get("module")))
            break
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    specific = [candidate for candidate in candidates if candidate[0] < 2]
    if specific:
        # A root-cause line (missing dependency, syntax error) decides: the
        # ``ImportError while importing`` header for the same collection
        # error carries no module name of its own, so when every root cause
        # is expected under nop the header must not flag on its own.
        for _, _, error, module in specific:
            if (
                module is not None
                and instruction_text is not None
                and _mentioned_as_word(instruction_text, module.split(".")[0])
            ):
                continue  # expected under nop; keep scanning for real defects
            return error
        return None
    for _, lineno, error, _module in candidates:
        if _import_fails_in_agent_code(combined.splitlines(), lineno):
            continue
        return error
    return None


#: A pytest traceback frame line: ``<path>.py:<line>: in <name>``.
_FRAME_RE = re.compile(r"^(?P<path>\S+\.py):\d+: in \S+")


def _import_fails_in_agent_code(lines: Sequence[str], header_lineno: int) -> bool:
    """True when the header's ImportError is raised inside agent-editable code.

    With no missing-module or syntax root cause, ``ImportError while
    importing test module`` means a name failed to import from code that
    exists. When the innermost traceback frame is in the task workspace (a
    path relative to the pytest rootdir, or under ``/app/``) rather than in
    the grader (``/tests/``) or the interpreter/site-packages, repairing that
    code can be part of the task (e.g. vendored sources the instruction asks
    the agent to fix), so a nop failure there is not a grader defect.
    """
    innermost: str | None = None
    for line in lines[header_lineno + 1 :]:
        if re.match(r"^\s*E\s", line) or line.startswith("=") or "ERROR collecting" in line:
            break
        match = _FRAME_RE.match(line.strip())
        if match is not None:
            innermost = match.group("path")
    if innermost is None:
        return False
    return not innermost.startswith("/") or innermost.startswith("/app/")
    return None


def grader_stdout_texts(trial_dir: Path) -> list[str]:
    """Verifier stdout for grader-defect scan: top-level plus repeat snapshots.

    Reads ``<trial>/verifier/test-stdout.txt`` and any per-repeat stdout the
    ``RepeatVerifier`` snapshotted under ``<trial>/verifier/repeat/*/``.
    Missing files contribute nothing (never a crash).
    """
    texts: list[str] = []
    verifier_dir = trial_dir / "verifier"
    candidates = [verifier_dir / "test-stdout.txt"]
    repeat_dir = verifier_dir / "repeat"
    if repeat_dir.is_dir():
        candidates.extend(sorted(repeat_dir.rglob("test-stdout.txt")))
    for path in candidates:
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def read_task_instruction(trial_dir: Path) -> str | None:
    """The task's ``instruction.md`` (same resolution as :func:`task_dir_for_trial`)."""
    task_dir = task_dir_for_trial(trial_dir)
    if task_dir is None:
        return None
    try:
        return (task_dir / "instruction.md").read_text(encoding="utf-8")
    except OSError:
        return None


def _timing_complete(timing: Any) -> bool:
    return (
        isinstance(timing, dict)
        and duration_seconds(
            timing.get("started_at") if isinstance(timing.get("started_at"), str) else None,
            timing.get("finished_at")
            if isinstance(timing.get("finished_at"), str)
            else None,
        )
        is not None
    )


def _timing_seconds(timing: Any) -> float | None:
    if not isinstance(timing, dict):
        return None
    started = timing.get("started_at")
    finished = timing.get("finished_at")
    return duration_seconds(
        started if isinstance(started, str) else None,
        finished if isinstance(finished, str) else None,
    )


def _repeat_verdict(rewards: Sequence[float | None]) -> str:
    if not rewards or any(reward is None for reward in rewards):
        return "errored"
    first = rewards[0]
    return "flipped" if any(reward != first for reward in rewards[1:]) else "stable"


def classify_trial(
    *,
    agent_name: str | None,
    reward: float | None,
    verifier_completed: bool,
    exception_type: str | None,
    exception_message: str | None,
    agent_execution_started: bool,
    repeat_rewards: Sequence[float | None] | None,
) -> list[str]:
    """Map one trial's outcome to qualification reasons (possibly empty).

    Setup-phase exceptions (the agent never started) blame setup, unless the
    exception text shows a provider quota problem (``backend_quota``).
    Verifier-phase exceptions blame the grader, with timeouts named
    separately. Otherwise a missing reward is ``reward_missing``. Clean runs
    add ``nop_passes`` when a nop/oracle control scores above 0 and
    ``unstable_verifier`` when repeat evidence disagrees.
    """
    reasons: list[str] = []
    if exception_type is not None and not agent_execution_started:
        if is_backend_quota_failure(exception_type, exception_message):
            reasons.append("backend_quota")
        else:
            reasons.append("setup_failed")
    elif exception_type is not None and exception_phase_for(exception_type) == "verifier":
        if "timeout" in exception_type.lower():
            reasons.append("verifier_timeout")
        else:
            reasons.append("verifier_error")
    elif reward is None:
        # No setup or verifier exception explains the trial, yet no usable
        # reward exists (agent-phase failure, lost telemetry, unscored run).
        reasons.append("reward_missing")
    if exception_type is None and reward is not None:
        if agent_name in CONTROL_AGENTS and reward > 0:
            reasons.append("nop_passes")
        if repeat_rewards is not None and _repeat_verdict(repeat_rewards) != "stable":
            reasons.append("unstable_verifier")
    for reason in reasons:
        assert reason in REASONS, reason
    return reasons


def status_for(reasons: Sequence[str]) -> str:
    """Trial status: ``ok``, ``broken``, or ``inconclusive``.

    A trial whose only reason is ``backend_quota`` failed on our provider
    tier, not on the task: it is inconclusive, never broken.
    """
    if not reasons:
        return "ok"
    if set(reasons) == {"backend_quota"}:
        return "inconclusive"
    return "broken"


def estimate_cost_usd(
    *,
    backend: str | None,
    sandbox_seconds: float | None,
    cpus: int | None,
    memory_mb: int | None,
    storage_mb: int | None,
    rate_backend: str = "daytona",
) -> float | None:
    """Sandbox cost under the active rate card, else null.

    Only the rate-card backend is priced (today: Daytona list prices, see
    ``DAYTONA_RATE_CARD``). Missing cpus/memory count as 0, and missing
    storage counts as 0 billable GiB: the lab never invents a disk size to
    make a trial look more expensive.
    """
    rates = RATE_CARDS.get(rate_backend)
    if rates is None or backend != rate_backend:
        return None
    if sandbox_seconds is None:
        return None
    vcpu = float(cpus or 0)
    mem_gib = float(memory_mb or 0) / 1024.0
    storage_gib = float(storage_mb or 0) / 1024.0
    billable_storage_gib = max(0.0, storage_gib - float(rates["storage_free_gib"]))
    hourly = (
        vcpu * float(rates["vcpu_usd_per_hour"])
        + mem_gib * float(rates["memory_usd_per_gib_hour"])
        + billable_storage_gib * float(rates["storage_usd_per_gib_hour"])
    )
    return sandbox_seconds / 3600.0 * hourly


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_staging(job_dir: Path) -> dict[str, Any]:
    """Package digests the queue staged (exploit-collect attribution)."""
    metadata = _read_json(job_dir / "lab-metadata.json")
    staging = metadata.get("task_staging")
    return staging if isinstance(staging, dict) else {}


def _manifest_entry(job_dir: Path, trial_name: str) -> dict[str, Any]:
    """Trial digests from a stability manifest (stability-collect fallback)."""
    manifest = _read_json(job_dir / "stability-manifest.json")
    trials = manifest.get("trials")
    if not isinstance(trials, dict):
        return {}
    entry = trials.get(trial_name)
    return entry if isinstance(entry, dict) else {}


def task_dir_for_trial(trial_dir: Path) -> Path | None:
    """The task directory a trial ran, even after the queue removed its stage.

    Harbor's lock points at the executor's staged copy
    (``<jobs>/.exec-stage/<job>``), which the queue deletes once the run
    finishes. The job's ``experiment-spec.json`` keeps the source task path
    (checkout-relative, e.g. ``derived/task-store/...``); it is resolved
    against the checkout that owns the jobs directory.
    """
    lock = _read_json(trial_dir / "lock.json")
    task = lock.get("task")
    staged = task.get("path") if isinstance(task, dict) else None
    if isinstance(staged, str) and staged and (Path(staged) / "task.toml").is_file():
        return Path(staged)
    job_dir = trial_dir.parent
    source = _read_json(job_dir / "experiment-spec.json").get("task")
    if not isinstance(source, str) or not source:
        return None
    path = Path(source)
    candidates = [path] if path.is_absolute() else [job_dir.parent.parent / path, job_dir.parent / path]
    return next((c for c in candidates if (c / "task.toml").is_file()), None)


def _task_toml(trial_dir: Path) -> dict[str, Any]:
    """The task.toml Harbor ran (see :func:`task_dir_for_trial`)."""
    task_dir = task_dir_for_trial(trial_dir)
    if task_dir is None:
        return {}
    try:
        return tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def collect_trial(
    job_dir: Path,
    trial_dir: Path,
    *,
    rate_backend: str = "daytona",
    produced_at: str | None = None,
) -> dict[str, Any]:
    """Build one ``task_qualification.parquet`` row from a finished trial."""
    trial_name = trial_dir.name
    result = _read_json(trial_dir / "result.json")
    config = _read_json(trial_dir / "config.json")
    lock = _read_json(trial_dir / "lock.json")
    result_config = result.get("config")
    backend = trial_environment_type(
        config or None,
        result_config if isinstance(result_config, dict) else None,
        lock or None,
    )
    agent_info = result.get("agent_info")
    agent_name = (
        agent_info.get("name")
        if isinstance(agent_info, dict) and isinstance(agent_info.get("name"), str)
        else None
    )
    if agent_name is None:
        agent = config.get("agent")
        if isinstance(agent, dict) and isinstance(agent.get("name"), str):
            agent_name = agent["name"]
    exception = result.get("exception_info")
    exception_type = (
        exception.get("exception_type")
        if isinstance(exception, dict) and isinstance(exception.get("exception_type"), str)
        else None
    )
    exception_message = (
        exception.get("exception_message")
        if isinstance(exception, dict)
        and isinstance(exception.get("exception_message"), str)
        else None
    )
    verifier_result = result.get("verifier_result")
    raw_rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    raw_reward = raw_rewards.get("reward") if isinstance(raw_rewards, dict) else None
    reward = (
        float(raw_reward)
        if isinstance(raw_reward, (int, float)) and not isinstance(raw_reward, bool)
        else None
    )
    if reward is None:
        reward = _trial_reward_file(trial_dir)
    agent_execution_started = (
        isinstance(result.get("agent_execution"), dict)
        and isinstance(result["agent_execution"].get("started_at"), str)
    )
    verifier_completed = _timing_complete(result.get("verifier")) or (
        reward is not None and exception_phase_for(exception_type) != "verifier"
    )
    stability: Sequence[float | None] | None
    try:
        parsed = parse_stability_file(trial_dir / "verifier" / "stability.json")
    except ValueError:
        parsed = None
    stability = parsed.rewards if parsed is not None else None
    reasons = classify_trial(
        agent_name=agent_name,
        reward=reward,
        verifier_completed=verifier_completed,
        exception_type=exception_type,
        exception_message=exception_message,
        agent_execution_started=agent_execution_started,
        repeat_rewards=list(stability) if stability is not None else None,
    )
    grader_error: str | None = None
    if agent_name in CONTROL_AGENTS:
        grader_error = detect_grader_collection_failure(
            grader_stdout_texts(trial_dir),
            instruction_text=read_task_instruction(trial_dir),
        )
        if grader_error is not None and "grader_broken" not in reasons:
            reasons.append("grader_broken")
    started_at = result.get("started_at")
    finished_at = result.get("finished_at")
    trial_seconds = duration_seconds(
        started_at if isinstance(started_at, str) else None,
        finished_at if isinstance(finished_at, str) else None,
    )
    toml = _task_toml(trial_dir)
    environment = toml.get("environment")
    env_map = environment if isinstance(environment, dict) else {}
    metadata = toml.get("metadata")
    meta_map = metadata if isinstance(metadata, dict) else {}
    cpus = _int_or_none(env_map.get("cpus"))
    memory_mb = _int_or_none(env_map.get("memory_mb"))
    storage_mb = _int_or_none(env_map.get("storage_mb"))
    # Harbor Daytona staging overrides the task.toml sizes in the trial's own
    # config: when present they are what the sandbox actually got (and cost).
    for source in (config, result_config):
        if not isinstance(source, dict):
            continue
        env = source.get("environment")
        if not isinstance(env, dict):
            continue
        override = _int_or_none(env.get("override_cpus"))
        if override is not None:
            cpus = override
        override = _int_or_none(env.get("override_memory_mb"))
        if override is not None:
            memory_mb = override
        override = _int_or_none(env.get("override_storage_mb"))
        if override is not None:
            storage_mb = override
    staging = _job_staging(job_dir)
    manifest = _manifest_entry(job_dir, trial_name)
    task_version_digest = staging.get("source_package_digest") or manifest.get(
        "task_version_digest"
    )
    harbor_digest = staging.get("source_harbor_digest") or manifest.get("harbor_digest")
    lock_task = lock.get("task")
    lock_task_name = (
        lock_task.get("name") if isinstance(lock_task, dict) else None
    )
    result_task_name = result.get("task_name")
    task_id = (
        meta_map.get("source_id")
        or staging.get("source_task_basename")
        or (lock_task_name if isinstance(lock_task_name, str) else None)
        or (
            result_task_name.rsplit("/", 1)[-1]
            if isinstance(result_task_name, str) and result_task_name
            else None
        )
    )
    domain = meta_map.get("domain")
    return {
        "task_version_digest": task_version_digest
        if isinstance(task_version_digest, str)
        else None,
        "harbor_digest": harbor_digest if isinstance(harbor_digest, str) else None,
        "task_id": task_id if isinstance(task_id, str) else None,
        "domain": domain if isinstance(domain, str) else None,
        "backend": backend,
        "environment_import_path": _environment_import_path(config, result_config, lock),
        "job_name": job_dir.name,
        "trial_name": trial_name,
        "agent_name": agent_name,
        "started_at": started_at if isinstance(started_at, str) else None,
        "finished_at": finished_at if isinstance(finished_at, str) else None,
        "environment_setup_seconds": _timing_seconds(result.get("environment_setup")),
        "agent_setup_seconds": _timing_seconds(result.get("agent_setup")),
        "verifier_seconds": _timing_seconds(result.get("verifier")),
        "trial_seconds": trial_seconds,
        "setup_ok": exception_type is None or agent_execution_started,
        "verifier_completed": verifier_completed,
        "reward": reward,
        "repeat_rewards": list(stability) if stability is not None else None,
        "infra_error_class": exception_type,
        "infra_error_phase": exception_phase_for(exception_type),
        "grader_error": grader_error,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "storage_mb": storage_mb,
        "sandbox_seconds": trial_seconds,
        "est_cost_usd": estimate_cost_usd(
            backend=backend,
            sandbox_seconds=trial_seconds,
            cpus=cpus,
            memory_mb=memory_mb,
            storage_mb=storage_mb,
            rate_backend=rate_backend,
        ),
        "status": status_for(reasons),
        "reasons": reasons,
        "produced_at": produced_at or utc_now_iso(),
    }


def _environment_import_path(
    config: Mapping[str, Any], result_config: Any, lock: Mapping[str, Any]
) -> str | None:
    for source in (config, result_config, lock):
        if not isinstance(source, dict):
            continue
        env = source.get("environment")
        if not isinstance(env, dict):
            continue
        import_path = env.get("import_path")
        if isinstance(import_path, str) and import_path:
            return import_path
    return None


def _trial_reward_file(trial_dir: Path) -> float | None:
    """Reward from verifier output files when result.json carries none."""
    try:
        return float((trial_dir / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        payload = json.loads((trial_dir / "verifier" / "reward.json").read_text())
    except (OSError, ValueError):
        return None
    raw = payload.get("reward") if isinstance(payload, dict) else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def collect_jobs(
    jobs: Sequence[Path],
    *,
    rate_backend: str = "daytona",
    produced_at: str | None = None,
) -> list[dict[str, Any]]:
    """Collect every trial of the given Harbor job directories into table rows."""
    rows: list[dict[str, Any]] = []
    for job_dir in jobs:
        if not job_dir.is_dir():
            raise ValueError(f"job directory is missing: {job_dir}")
        for trial_dir in iter_trial_dirs(job_dir):
            rows.append(
                collect_trial(
                    job_dir, trial_dir, rate_backend=rate_backend, produced_at=produced_at
                )
            )
    rows.sort(key=lambda row: (str(row["job_name"]), str(row["trial_name"])))
    return rows


def qualification_schema() -> Any:
    import pyarrow as pa

    return pa.schema([
        ("task_version_digest", pa.string()),
        ("harbor_digest", pa.string()),
        ("task_id", pa.string()),
        ("domain", pa.string()),
        ("backend", pa.string()),
        ("environment_import_path", pa.string()),
        ("job_name", pa.string()),
        ("trial_name", pa.string()),
        ("agent_name", pa.string()),
        ("started_at", pa.string()),
        ("finished_at", pa.string()),
        ("environment_setup_seconds", pa.float64()),
        ("agent_setup_seconds", pa.float64()),
        ("verifier_seconds", pa.float64()),
        ("trial_seconds", pa.float64()),
        ("setup_ok", pa.bool_()),
        ("verifier_completed", pa.bool_()),
        ("reward", pa.float64()),
        ("repeat_rewards", pa.list_(pa.float64())),
        ("infra_error_class", pa.string()),
        ("infra_error_phase", pa.string()),
        ("grader_error", pa.string()),
        ("cpus", pa.int64()),
        ("memory_mb", pa.int64()),
        ("storage_mb", pa.int64()),
        ("sandbox_seconds", pa.float64()),
        ("est_cost_usd", pa.float64()),
        ("status", pa.string()),
        ("reasons", pa.list_(pa.string())),
        ("produced_at", pa.string()),
    ])


def write_task_qualification_parquet(rows: Sequence[dict[str, Any]], path: Path) -> Path:
    """Write collected rows to ``task_qualification.parquet`` (contract schema)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = qualification_schema()
    columns = {field.name: [row.get(field.name) for row in rows] for field in schema}
    table = pa.table(columns, schema=schema)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out)
    return out


def read_task_qualification_parquet(path: Path) -> list[dict[str, Any]]:
    """Read back ``task_qualification.parquet`` rows (used by tests and the CLI)."""
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def summarize_qualification(rows: Sequence[dict[str, Any]]) -> str:
    """Per-domain qualification summary for the ``qualify-collect`` report."""
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_domain.setdefault(str(row.get("domain") or "unknown"), []).append(row)
    lines = [f"qualification: {len(rows)} trials"]
    for domain in sorted(by_domain):
        group = by_domain[domain]
        tasks = {
            str(row.get("task_version_digest") or row.get("task_id") or row["trial_name"])
            for row in group
        }
        ok = sum(1 for row in group if row.get("status") == "ok")
        broken = [row for row in group if row.get("status") == "broken"]
        inconclusive = sum(1 for row in group if row.get("status") == "inconclusive")
        reason_counts: Counter[str] = Counter()
        for row in broken:
            for reason in row.get("reasons") or []:
                reason_counts[str(reason)] += 1
        setup_seconds = sorted(
            row["environment_setup_seconds"]
            for row in group
            if isinstance(row.get("environment_setup_seconds"), (int, float))
        )
        trial_seconds = sorted(
            row["trial_seconds"]
            for row in group
            if isinstance(row.get("trial_seconds"), (int, float))
        )
        sandbox_hours = sum(
            row["sandbox_seconds"]
            for row in group
            if isinstance(row.get("sandbox_seconds"), (int, float))
        ) / 3600.0
        total_cost = sum(
            row["est_cost_usd"]
            for row in group
            if isinstance(row.get("est_cost_usd"), (int, float))
        )
        broken_detail = (
            ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
            or "none"
        )
        lines.append(
            f"domain={domain} tasks={len(tasks)} trials={len(group)} "
            f"ok={ok} broken={len(broken)} inconclusive (backend_quota)={inconclusive} "
            f"broken_by_reason: {broken_detail}"
        )
        setup_med = _percentile(setup_seconds, 0.5)
        setup_p90 = _percentile(setup_seconds, 0.9)
        trial_med = _percentile(trial_seconds, 0.5)
        trial_p90 = _percentile(trial_seconds, 0.9)
        lines.append(
            "  environment_setup_seconds median="
            f"{setup_med if setup_med is None else round(setup_med, 3)} "
            f"p90={setup_p90 if setup_p90 is None else round(setup_p90, 3)}; "
            f"trial_seconds median={trial_med if trial_med is None else round(trial_med, 3)} "
            f"p90={trial_p90 if trial_p90 is None else round(trial_p90, 3)}"
        )
        lines.append(
            f"  sandbox_hours={sandbox_hours:.4f} est_cost_usd={total_cost:.6f}"
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class BrokenExportResult:
    """Outcome of one broken-task export."""

    path: Path
    sha256: str
    backend: str
    n_broken: int


def _latest_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    finished = row.get("finished_at")
    has_finished = isinstance(finished, str) and bool(finished)
    return (
        1 if has_finished else 0,
        finished if has_finished else "",
        str(row.get("job_name") or ""),
        str(row.get("trial_name") or ""),
    )


def export_broken(
    out: Path,
    *,
    backend: str,
    repo_root: Path,
    derived_root: Path | None = None,
) -> BrokenExportResult:
    """Write the content-addressed broken-task JSON for one backend.

    A task version counts as broken on a backend when its *latest*
    qualification trial on that backend has status ``broken``
    (``inconclusive`` backend-quota trials never mark a task broken: the
    task is unrun at this tier, not guilty). Versions carrying an
    ``error``-severity row in the catalog ``task_findings`` table are
    listed as well, whatever the backend: a curated hard defect outranks
    any single trial outcome. The digest covers ``schema`` + ``backend`` +
    ``items`` only, so re-exporting the same broken set gives the same
    digest.
    """
    from evallab.storage.paths import derived_root_from_environment
    from evallab.task_catalog import CatalogError, catalog_dir

    derived = derived_root or derived_root_from_environment(repo_root)
    table_path = catalog_dir(derived) / TABLE_FILENAME
    has_table = table_path.is_file()
    error_findings = _error_findings(catalog_dir(derived))
    if not has_table and not error_findings:
        raise CatalogError(
            f"no qualification table or error-severity finding under {catalog_dir(derived)}; "
            "run tasks qualify-collect first"
        )
    rows = read_task_qualification_parquet(table_path) if has_table else []
    scoped = [row for row in rows if (row.get("backend") or "unknown") == backend]
    by_version: dict[str, list[dict[str, Any]]] = {}
    for row in scoped:
        key = str(row.get("task_version_digest") or row.get("task_id") or row["trial_name"])
        by_version.setdefault(key, []).append(row)
    items: list[dict[str, Any]] = []
    item_by_digest: dict[str, dict[str, Any]] = {}
    for key in sorted(by_version):
        group = by_version[key]
        latest = max(group, key=_latest_key)
        if latest.get("status") != "broken":
            continue
        trials = sorted(
            {
                f"{row.get('job_name')}/{row.get('trial_name')}"
                for row in group
                if row.get("job_name") and row.get("trial_name")
            }
        )
        item = {
            "task_id": latest.get("task_id"),
            "task_version_digest": latest.get("task_version_digest"),
            "harbor_digest": latest.get("harbor_digest"),
            "domain": latest.get("domain"),
            "reasons": list(latest.get("reasons") or []),
            "trials": trials,
            "source": "qualification",
        }
        items.append(item)
        digest = latest.get("task_version_digest")
        if isinstance(digest, str) and digest:
            item_by_digest[digest] = item
    for finding in error_findings:
        digest = str(finding["task_version_digest"])
        reason = f"finding:{finding['rule']}"
        item = item_by_digest.get(digest)
        if item is None:
            item = {
                "task_id": finding["task_id"],
                "task_version_digest": digest,
                "harbor_digest": None,
                "domain": finding["domain"],
                "reasons": [],
                "trials": [],
                "source": "finding",
            }
            items.append(item)
            item_by_digest[digest] = item
        reasons = item["reasons"]
        assert isinstance(reasons, list)
        if reason not in reasons:
            reasons.append(reason)
            reasons.sort()
        item["source"] = "finding"
    items.sort(key=lambda item: str(item.get("task_version_digest") or item.get("task_id")))
    identity = {"schema": BROKEN_EXPORT_SCHEMA, "backend": backend, "items": items}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    sha = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    meta = {
        "created_at": datetime.now(UTC).isoformat(),
        "table": TABLE_FILENAME,
        "table_digest": (
            f"sha256:{hashlib.sha256(table_path.read_bytes()).hexdigest()}" if has_table else None
        ),
        "n_broken": len(items),
        "n_considered": len(scoped),
    }
    payload = {**identity, "meta": meta}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({**payload, "sha256": sha}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BrokenExportResult(path=out, sha256=sha, backend=backend, n_broken=len(items))


def _error_findings(catalog: Path) -> list[dict[str, Any]]:
    """Error-severity catalog findings, sorted by ``(digest, rule)``.

    A missing findings table means no curated defects (never a crash): the
    export then lists qualification-broken versions only.
    """
    from evallab.task_catalog import TASK_FINDINGS_SCHEMA

    path = catalog / "task_findings.parquet"
    if not path.is_file():
        return []
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, schema=TASK_FINDINGS_SCHEMA)
    except Exception:
        return []
    findings = [
        {
            "task_version_digest": str(row["task_version_digest"]),
            "task_id": row["task_id"],
            "domain": row["domain"],
            "rule": str(row["rule"]),
        }
        for row in table.to_pylist()
        if row.get("severity") == "error"
        and isinstance(row.get("task_version_digest"), str)
        and isinstance(row.get("rule"), str)
    ]
    findings.sort(key=lambda row: (str(row["task_version_digest"]), str(row["rule"])))
    return findings


def load_job_staging(job_dir: Path) -> dict[str, Any]:
    """Trial-name keyed digests for one job (re-ingest helper; smoke only)."""
    job = load_job(job_dir)
    staging = _job_staging(job_dir)
    return {
        "job_name": job.name,
        "task_version_digest": staging.get("source_package_digest"),
        "harbor_digest": staging.get("source_harbor_digest"),
        "n_trials": len(job.trials),
    }
