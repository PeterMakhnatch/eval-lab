"""Eval Lab CLI evaluator for the Reef publication gate (HAR-73).

Stdlib only: this module NEVER imports ``reef`` or ``evallab``. It drives the
normal Lab CLI (``tasks prepare --json`` / ``submit`` / ``tick --spec-id``) in
the Lab's own interpreter via subprocess, reads retained Harbor job evidence
and scoped queue state files directly, and returns a Reef-shaped
``{"metrics": ..., "metadata": ...}`` dict that the parent plugin settles.

Public contract
---------------
``LabConfig.from_dict(raw)`` builds a validated binding from the nested
``lab`` object of the gate JSON. ``LabEvaluator(config)`` exposes:

* ``prepare(candidate_id, current_files, candidate_files, task_ids)`` — run
  every fail-closed validation, materialize both harness trees, prepare and
  submit every task-repeat-side spec through the normal CLI, and return
  ``{"manifest_path", "manifest", "content_hash", "spec_details",
  "approve_commands", "tick_commands", "recovered"}``. It NEVER ticks,
  executes, approves, or waits: a named human approves the listed spec IDs.
* ``evaluate(...)`` — the same prepare path and manifest (idempotent reuse),
  then prints the actual approve commands to stdout, waits for approval,
  ticks approved specs serially (``--spec-id`` only), waits for terminal
  states, and collects retained evidence into the settlement shape.

Metrics mirror ``CordisBackend.evaluate`` (Reef ``backend.py`` 1261-1279):
``candidate_scores`` / ``current_scores`` are positional ``float | None``
vectors of length ``len(dev) * episode_repeats``; plus ``episode_failures``,
``episode_repeats``, per-side ``*_failures`` / ``*_residue`` / ``*_score`` /
``*_agents`` / ``*_paths``. ``evaluation_sides`` is omitted when both sides
have complete episode coverage and otherwise lists exactly the covered
sides, so settlement never clears a failure manifest for tasks with missing
evidence.

Native Lab evidence carries no Reef residue/agent/stage-path observations:
residues are ``0``, agents are ``{}``, and paths are the retained trial
directories, with ``metadata`` marking ``residue_unknown`` /
``agents_unknown`` / ``paths_are_trial_dirs``. Failure observations are built
only from actual retained evidence (exception text, missing-reward cause,
unobserved cause); nothing is invented.

``metadata["complete"]`` is ``False`` (with ``incomplete_reason``) when ANY
episode is unobserved or unscored, when approval/tick waits bound out, or
when the campaign stops/withdraws; the parent holds publication on
``complete=False`` while the observed partial scores stay in both metric
vectors and ``metadata["evidence"]``.

Fail-closed validation problems (bad split, task mismatch, heldout use,
missing/corrupt registration, digest drift, candidate-id conflict, vanished
spec) raise :class:`LabGateError` BEFORE any CLI submit/execution (and after
submission only for durable-state contradictions). Campaign aborts return
``complete=False`` instead of raising so partial evidence is never lost.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "EVALUATOR_NAME",
    "EVALUATOR_VERSION",
    "LabConfig",
    "LabEvaluator",
    "LabGateError",
]

EVALUATOR_NAME = "evallab_cli"
EVALUATOR_VERSION = "1"

#: Reef HEAD pinned by the parent Factory (recorded verbatim, never re-derived
#: here: the Factory already binds the actual clean Reef checkout).
EXPECTED_REEF_COMMIT = "2a1864d4158de8a24e00ae777e9ff0501f49a97f"

#: Queue states mirrored from ``evallab.queue.QUEUE_STATES`` (read-only use).
QUEUE_STATES = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
)
TERMINAL_QUEUE_STATES = frozenset({"done", "failed"})

#: Committed split envelope: exact known root keys; anything else refuses.
SPLIT_KNOWN_ROOT_KEYS = frozenset({
    "schema_version", "name", "status", "authority", "rationale",
    "dev", "held_out", "limits",
})
SPLIT_ROW_KEYS = frozenset({"task_id", "task_path", "package_digest"})

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REEF_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")

#: Exact behavior-knob contract mirrored from HAR-71 ``terminus_harness``.
ALLOWED_KNOBS = frozenset({
    "enable_summarize",
    "interleaved_thinking",
    "llm_call_kwargs",
    "max_thinking_tokens",
    "max_turns",
    "parser_name",
    "proactive_summarization_threshold",
    "reasoning_effort",
    "temperature",
})
_EXPLICIT_BINDING_KEYS = frozenset({
    "model_name", "api_base", "llm_kwargs", "llm_backend", "model",
    "custom_llm_provider", "provider", "extra_headers", "headers",
})
_BINDING_SUBSTRINGS = ("api_key", "api_base", "base_url", "credential", "secret")

#: Reef side names, in pairing order.
SIDES = ("candidate", "current")

#: Pilot ceilings mirrored from ``task_prepare``; passed explicitly so every
#: task-repeat-side spec pins the exact same model/config/resources.
DEFAULT_MAX_REQUESTS = 200
DEFAULT_MAX_INPUT_TOKENS = 5_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 131_072

#: Machine-readable incomplete reasons (metadata["incomplete_reason"]).
REASON_APPROVAL_TIMEOUT = "approval_timeout"
REASON_TICK_TIMEOUT = "tick_timeout"
REASON_SPEC_TIMEOUT = "spec_timeout"
REASON_WITHDRAWN = "withdrawn"
REASON_STOPPED = "budget_stopped"
REASON_FAILED = "spec_failed"
REASON_UNOBSERVED = "incomplete_evidence"
REASON_ERROR = "campaign_error"


class LabGateError(ValueError):
    """Fail-closed refusal: no fabricated score, nothing published."""


def _fail(message: str) -> LabGateError:
    return LabGateError(message)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_FIELDS = (
    "lab_root", "split_path", "model", "record_dir",
    "agent", "environment", "python_executable",
    "timeout_seconds", "cost_limit_usd", "est_cost_usd",
    "max_requests", "max_input_tokens", "max_output_tokens",
    "max_total_tokens", "episode_repeats", "reef_commit",
    "submitted_by", "tick_timeout_seconds", "tick_poll_seconds",
)


@dataclass(frozen=True)
class LabConfig:
    """Binding for one gate evaluation campaign.

    Required keys: ``lab_root`` (Lab checkout owning the CLI/queue),
    ``split_path`` (committed split JSON; relative resolves against
    ``lab_root``), ``model`` (exact selector; always explicit and never taken
    from candidate files), ``record_dir`` (durable manifests/locks/trees;
    relative resolves against ``lab_root``).

    Only ``terminus-2`` is admitted as ``agent``: controls cannot carry a
    harness tree, so no oracle/nop compatibility exists here.
    ``python_executable`` defaults to ``<lab_root>/.venv/bin/python`` — the
    Lab's own interpreter — never the importing (Reef) interpreter.
    ``reef_commit`` is optional: the parent Factory already binds the clean
    Reef checkout and adds record provenance; when present it must be a
    complete lowercase commit and is recorded verbatim.
    """

    lab_root: str
    split_path: str
    model: str
    record_dir: str
    agent: str = "terminus-2"
    environment: str = "docker"
    python_executable: str | None = None
    timeout_seconds: int | None = None
    cost_limit_usd: float | None = None
    est_cost_usd: float | None = None
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_total_tokens: int | None = None
    episode_repeats: int = 1
    reef_commit: str | None = None
    submitted_by: str = "reef-gate"
    tick_timeout_seconds: float = 3600.0
    tick_poll_seconds: float = 5.0

    @classmethod
    def from_dict(cls, raw: dict) -> "LabConfig":
        """Build a validated config from the nested ``lab`` gate-JSON object."""
        if not isinstance(raw, dict):
            raise _fail("lab config must be a JSON object")
        unknown = set(raw) - set(_CONFIG_FIELDS)
        if unknown:
            raise _fail(
                "unknown lab config keys: " + ", ".join(sorted(str(k) for k in unknown))
            )
        values: dict[str, Any] = dict(raw)
        if values.get("agent", "terminus-2") != "terminus-2":
            raise _fail(
                f"lab agent must be 'terminus-2', got {values.get('agent')!r}: "
                "only terminus-2 carries a HAR-71 harness tree"
            )
        values.setdefault("agent", "terminus-2")
        for key in ("lab_root", "split_path", "model", "record_dir"):
            value = values.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _fail(f"lab config {key!r} must be a non-empty string")
        model = values["model"]
        if not model.strip():
            raise _fail("lab config 'model' must be an explicit non-empty selector")
        repeats = values.get("episode_repeats", 1)
        if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
            raise _fail("lab config 'episode_repeats' must be an integer >= 1")
        timeout = values.get("timeout_seconds")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int)
            or not 1 <= timeout <= 28_800
        ):
            raise _fail("lab config 'timeout_seconds' must be None or 1..28800")
        for key in ("max_requests", "max_input_tokens", "max_output_tokens"):
            value = values.get(key, 1)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise _fail(f"lab config {key!r} must be a positive integer")
        if values.get("max_total_tokens") is not None:
            total = values["max_total_tokens"]
            if isinstance(total, bool) or not isinstance(total, int) or total < 1:
                raise _fail("lab config 'max_total_tokens' must be None or a positive integer")
        for key in ("cost_limit_usd", "est_cost_usd"):
            value = values.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            ):
                raise _fail(f"lab config {key!r} must be None or >= 0")
        for key in ("tick_timeout_seconds", "tick_poll_seconds"):
            value = values.get(key, 1.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise _fail(f"lab config {key!r} must be a positive number")
        reef_commit = values.get("reef_commit")
        if reef_commit is not None and (
            not isinstance(reef_commit, str) or _REEF_COMMIT_RE.fullmatch(reef_commit) is None
        ):
            raise _fail("lab config 'reef_commit' must be None or a complete lowercase commit")
        kwargs = {k: values[k] for k in _CONFIG_FIELDS if k in values}
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise _fail(f"invalid lab config: {exc}") from exc

    def resolved_paths(self) -> tuple[Path, Path, Path, Path]:
        """Return ``(lab_root, split_file, record_dir, python)`` without shell."""
        lab_root = Path(self.lab_root).resolve()
        split_file = Path(self.split_path)
        split_file = split_file if split_file.is_absolute() else lab_root / split_file
        record_dir = Path(self.record_dir)
        record_dir = record_dir if record_dir.is_absolute() else lab_root / record_dir
        if self.python_executable:
            python = Path(self.python_executable)
        else:
            python = lab_root / ".venv" / "bin" / "python"
        return lab_root, split_file, record_dir, python


# ---------------------------------------------------------------------------
# Small stdlib helpers
# ---------------------------------------------------------------------------

def _read_json_file(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _fail(f"{label} not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise _fail(f"{label} is corrupt: {path}: {exc}") from exc


def _slug(text: str, max_len: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:max_len].rstrip("-") or "x"


def _digest_entries(entries: list[tuple[str, bytes]]) -> str:
    """Canonical content digest; mirrors ``evidence_store._content_digest``."""
    digest = hashlib.sha256()
    for relative, content in sorted(entries, key=lambda item: item[0]):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _dir_digest(root: Path) -> str:
    entries: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise _fail(f"harness tree contains unsupported symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise _fail(f"harness tree contains unsupported special file: {path}")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise _fail(f"harness tree file unavailable: {path}: {exc}") from exc
        entries.append((path.relative_to(root).as_posix(), content))
    return _digest_entries(entries)


def _is_binding_key(key: str) -> bool:
    folded = str(key).casefold()
    if folded in _EXPLICIT_BINDING_KEYS:
        return True
    if "model" in folded:
        return True
    return any(fragment in folded for fragment in _BINDING_SUBSTRINGS)


def _check_tree_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise _fail(f"harness tree refuses non-string path: {key!r}")
    if key.startswith("/") or "\\" in key:
        raise _fail(f"harness tree refuses unsafe relative path: {key!r}")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _fail(f"harness tree refuses unsafe relative path: {key!r}")
    if key == "terminus/context" or key.startswith("terminus/context/"):
        raise _fail(
            f"terminus code_extension at {key!r} is not supported: "
            "Eval Lab never imports candidate code"
        )
    for prefix in ("terminus/sessions/", "terminus/trials/"):
        if key.startswith(prefix):
            raise _fail(
                f"harness tree contains reserved runtime path {key!r}: "
                "sessions/trials are written during runs, never pinned"
            )


def _validate_tree_config(mapping: Mapping[str, str]) -> None:
    """Mirror HAR-71 config validation so binding never reaches the CLI."""
    raw = mapping.get("terminus/config.json")
    if raw is None:
        return
    try:
        config = json.loads(raw)
    except ValueError as exc:
        raise _fail(f"terminus/config.json is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise _fail("terminus/config.json must be an object")
    bound = sorted(str(key) for key in config if _is_binding_key(str(key)))
    if bound:
        raise _fail(
            "terminus config " + ", ".join(repr(key) for key in bound)
            + " is model/transport binding, not candidate behavior: "
            "candidates vary Terminus behavior, never provider routing"
        )
    unknown = sorted(set(config) - ALLOWED_KNOBS)
    if unknown:
        raise _fail(
            "terminus config sets keys that are not Terminus 2 arguments: "
            + ", ".join(unknown)
        )
    turns = config.get("max_turns")
    if turns is not None and (isinstance(turns, bool) or not isinstance(turns, int) or turns < 1):
        raise _fail("terminus config max_turns must be a positive integer")
    if "llm_call_kwargs" in config:
        nested = config["llm_call_kwargs"]
        if not isinstance(nested, dict):
            raise _fail("terminus config llm_call_kwargs must be an object")
        nested_bound = sorted(str(key) for key in nested if _is_binding_key(str(key)))
        if nested_bound:
            raise _fail(
                "terminus llm_call_kwargs " + ", ".join(repr(key) for key in nested_bound)
                + " is model/transport binding, not candidate behavior"
            )


def _norm_split_path(task_path: str) -> str:
    return posixpath.normpath(task_path.strip().rstrip("/"))


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [clipped {len(text) - limit}]"


# ---------------------------------------------------------------------------
# Subprocess seam (fakeable; never drains shared state in tests)
# ---------------------------------------------------------------------------

class SubprocessRunner:
    """Run the Lab CLI in the Lab's own interpreter and clean environment.

    The child never inherits the Reef runtime: ``PYTHONPATH`` /
    ``VIRTUAL_ENV`` are stripped and ``PATH`` is replaced with the Lab venv
    bin plus system directories, so ``uv run evallab`` resolves the Lab
    package and nothing from the importing (Reef) interpreter. Credential
    variables (``*_API_KEY`` etc.) stay process environment — they are never
    written into the manifest, spec paths, or argv.
    """

    #: Environment keys removed so the Reef runtime cannot leak into the Lab.
    STRIP_ENV_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "UV_PROJECT", "UV_PYTHON")

    def _env(self, python: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if any(upper == prefix or upper.startswith(prefix + "_") for prefix in self.STRIP_ENV_PREFIXES):
                continue
            if upper == "PATH":
                continue
            env[key] = value
        env["PATH"] = f"{python.parent}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        return env

    def run(
        self, cmd: list[str], *, cwd: Path, timeout: float, python: Path,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=self._env(python),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except OSError as exc:
            raise _fail(f"lab CLI could not start {cmd[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise _fail(f"lab CLI timed out after {timeout}s: {' '.join(cmd[:6])}…") from exc
        return proc.returncode, proc.stdout, proc.stderr


def _resolve_cli(python: Path) -> list[str]:
    """Prefer the venv console script; fall back to ``run_cli`` with argv."""
    script = python.parent / "evallab"
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    boot = "import sys\nfrom evallab.cli import run_cli\nsys.exit(run_cli(sys.argv[1:]))"
    return [str(python), "-c", boot]


def _parse_cli_json(stdout: str, stderr: str, *, what: str) -> dict:
    """Parse the last pretty-printed JSON object a Lab command printed."""
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("{"):
            try:
                payload = json.loads("\n".join(lines[index:]))
            except ValueError:
                break
            if isinstance(payload, dict):
                return payload
            break
    raise _fail(f"lab CLI {what} printed no JSON payload; stderr: {_truncate(stderr)}")


def _parse_spec_id(stdout: str, stderr: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("spec_id:"):
            spec_id = line.split(":", 1)[1].strip()
            if spec_id:
                return spec_id
    raise _fail(f"lab CLI submit printed no spec_id; stderr: {_truncate(stderr)}")


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

@dataclass
class LabEvaluator:
    """Evaluate one candidate through the normal Eval Lab CLI submit path."""

    config: LabConfig
    runner: Any = None
    _cli: list[str] = field(default_factory=list, init=False, repr=False)
    _python: Path = field(default_factory=Path, init=False, repr=False)

    #: Bounded wall-clock for one CLI process (prepare/submit/tick each).
    cli_timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = SubprocessRunner()

    @contextmanager
    def _lock(self, candidate_id: str):
        """Persistent exclusive lock per candidate id, held across submit.

        The lock file lives under ``<record_dir>/locks/<sha256(id)>.lock`` so
        concurrent gate processes cannot double-submit one candidate id; the
        durable manifest (written before each submit advances) plus the
        deterministic spec identity closes the crash window.
        """
        record_dir = self.config.resolved_paths()[2]
        lock_dir = record_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        candidate_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        stream = (lock_dir / f"{candidate_hash}.lock").open("a+b")
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield stream
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    # -- public API --------------------------------------------------------

    def prepare(
        self,
        candidate_id: str,
        current_files: Mapping[str, str],
        candidate_files: Mapping[str, str],
        task_ids: tuple[str, ...],
    ) -> dict:
        """Validate, materialize, prepare, and submit every spec; never run.

        Returns ``{"manifest_path", "manifest", "content_hash",
        "spec_details", "approve_commands", "tick_commands", "recovered"}``.
        No tick, no execution, no approval, no waiting: the listed approve
        commands are exactly what a named human must run.
        """
        with self._lock(candidate_id):
            ctx = self._plan(
                candidate_id=candidate_id,
                current_files=current_files,
                candidate_files=candidate_files,
                task_ids=task_ids,
            )
            recovered = self._submit_all(ctx)
        manifest = ctx["manifest"]
        manifest["approve_commands"] = (
            [f"uv run evallab approve {p['spec_id']} --actor <you>" for p in ctx["pairings"]]
            + [f"uv run evallab tick --spec-id {p['spec_id']}" for p in ctx["pairings"]]
        )
        manifest["outcome"] = "awaiting_approval"
        self._write_manifest(ctx["manifest_path"], manifest)
        return {
            "manifest_path": ctx["manifest_path"].as_posix(),
            "manifest": manifest,
            "content_hash": ctx["content_hash"],
            "spec_details": [
                {
                    "task_id": p["task_id"], "repeat": p["repeat"], "side": p["side"],
                    "job_name": p["job_name"], "spec_path": p["spec_path"],
                    "spec_id": p["spec_id"], "state": p["state"],
                }
                for p in ctx["pairings"]
            ],
            "approve_commands": [
                f"uv run evallab approve {p['spec_id']} --actor <you>" for p in ctx["pairings"]
            ],
            "tick_commands": [
                f"uv run evallab tick --spec-id {p['spec_id']}" for p in ctx["pairings"]
            ],
            "recovered": recovered,
        }

    def evaluate(
        self,
        candidate_id: str,
        current_files: Mapping[str, str],
        candidate_files: Mapping[str, str],
        task_ids: tuple[str, ...],
    ) -> dict:
        started = time.monotonic()
        with self._lock(candidate_id):
            return self._evaluate_locked(
                candidate_id=candidate_id,
                current_files=current_files,
                candidate_files=candidate_files,
                task_ids=task_ids,
                started=started,
            )

    def _evaluate_locked(
        self, *, candidate_id: str, current_files: Mapping[str, str],
        candidate_files: Mapping[str, str], task_ids: tuple[str, ...],
        started: float,
    ) -> dict:
        ctx = self._plan(
            candidate_id=candidate_id,
            current_files=current_files,
            candidate_files=candidate_files,
            task_ids=task_ids,
        )
        self._submit_all(ctx)
        manifest = ctx["manifest"]
        manifest["approve_commands"] = (
            [f"uv run evallab approve {p['spec_id']} --actor <you>" for p in ctx["pairings"]]
            + [f"uv run evallab tick --spec-id {p['spec_id']}" for p in ctx["pairings"]]
        )
        self._write_manifest(ctx["manifest_path"], manifest)
        for line in manifest["approve_commands"]:
            print(line, flush=True)
        print(f"candidate manifest: {ctx['manifest_path']}", flush=True)

        pairings = ctx["pairings"]
        deadline = started + float(self.config.tick_timeout_seconds)
        self._refresh_states(ctx["lab_root"], pairings)
        incomplete = self._wait_for_approval(ctx["lab_root"], pairings, deadline)
        if incomplete is None:
            incomplete = self._tick_serial(ctx["lab_root"], pairings, deadline)
        if incomplete is None:
            incomplete = self._wait_for_terminal(ctx["lab_root"], pairings, deadline)
        self._refresh_states(ctx["lab_root"], pairings)
        manifest["outcome"] = "evaluated"
        self._write_manifest(ctx["manifest_path"], manifest)
        return self._collect(ctx=ctx, started=started, incomplete=incomplete)

    # -- planning (validation, trees, manifest) ----------------------------

    def _plan(
        self, *, candidate_id: str, current_files: Mapping[str, str],
        candidate_files: Mapping[str, str], task_ids: tuple[str, ...],
    ) -> dict:
        if not isinstance(candidate_id, str) or not candidate_id:
            raise _fail("candidate_id must be a non-empty string")
        tasks = tuple(task_ids)
        if not tasks or any(not isinstance(t, str) or not t for t in tasks):
            raise _fail("task_ids must be a non-empty tuple of task identities")

        lab_root, split_file, record_dir, python = self.config.resolved_paths()
        if not lab_root.is_dir():
            raise _fail(f"lab root is not a directory: {lab_root}")
        if not python.is_file():
            raise _fail(f"lab interpreter is not installed: {python}")
        record_dir.mkdir(parents=True, exist_ok=True)
        self._python = python
        self._cli = _resolve_cli(python)

        # 1. Committed split first: no evaluation until the split is committed.
        split, split_digest = self._load_split(split_file)
        dev_ids = [entry["task_id"] for entry in split["dev"]]
        if list(tasks) != dev_ids:
            raise _fail(
                "candidate task identities must match the committed dev set exactly "
                f"(got {list(tasks)!r}, want {dev_ids!r}); this retains the "
                "unchanged rule/settlement identity"
            )
        # 2. Registry allow-list + digest drift, before any CLI execution.
        self._check_registry(lab_root, split)
        # 3. Safe harness materialization (caller holds the candidate-id lock).
        candidate_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        trees = {
            "current": self._materialize_tree(record_dir, candidate_hash, "current", current_files),
            "candidate": self._materialize_tree(record_dir, candidate_hash, "candidate", candidate_files),
        }
        content_hash = self._content_hash(
            current_files, candidate_files, tasks, split_digest, trees,
        )
        manifest_path = record_dir / "manifests" / f"{candidate_hash}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest(manifest_path, candidate_id, content_hash)
        if manifest is None:
            manifest = {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "content_hash": content_hash,
                "created_at": datetime.now(UTC).isoformat(),
                "lab_root": lab_root.as_posix(),
                "split_path": self.config.split_path,
                "split_digest": split_digest,
                "agent": self.config.agent,
                "model": self.config.model,
                "environment": self.config.environment,
                "reef_commit": self.config.reef_commit,
                "episode_repeats": self.config.episode_repeats,
                "task_ids": list(tasks),
                "candidate_tree_sha256": trees["candidate"],
                "current_tree_sha256": trees["current"],
                "approve_commands": [],
                "pairings": [],
                "outcome": None,
            }
        by_key = {(p["task_id"], p["repeat"], p["side"]): p for p in manifest["pairings"]}
        pairings: list[dict[str, Any]] = []
        for index, entry in enumerate(split["dev"]):
            for repeat in range(self.config.episode_repeats):
                for side in SIDES:
                    key = (entry["task_id"], repeat, side)
                    job_name = self._job_name(candidate_hash, entry["task_id"], repeat, side)
                    spec_rel = f"derived/prepared/{job_name}.json"
                    pairing = by_key.get(key)
                    if pairing is None:
                        pairing = {
                            "index": index, "task_id": entry["task_id"],
                            "repeat": repeat, "side": side,
                            "job_name": job_name, "spec_path": spec_rel,
                            "spec_id": None, "state": None,
                        }
                        manifest["pairings"].append(pairing)
                    else:
                        # Deterministic spec identity: names/paths never change.
                        pairing["job_name"] = job_name
                        pairing["spec_path"] = spec_rel
                    pairings.append(pairing)
        return {
            "candidate_id": candidate_id, "candidate_hash": candidate_hash,
            "tasks": tasks, "lab_root": lab_root, "record_dir": record_dir,
            "split": split, "split_digest": split_digest, "trees": trees,
            "manifest": manifest, "manifest_path": manifest_path,
            "pairings": pairings, "content_hash": content_hash,
        }

    def _load_split(self, split_file: Path) -> tuple[dict[str, Any], str]:
        try:
            raw_bytes = split_file.read_bytes()
        except OSError as exc:
            raise _fail(f"committed split is missing, no evaluation: {split_file}: {exc}") from exc
        try:
            split = json.loads(raw_bytes.decode("utf-8"))
        except ValueError as exc:
            raise _fail(f"committed split is corrupt, no evaluation: {split_file}: {exc}") from exc
        if not isinstance(split, dict):
            raise _fail(f"committed split must be a JSON object: {split_file}")
        unknown_root = set(split) - SPLIT_KNOWN_ROOT_KEYS
        if unknown_root:
            raise _fail(
                "committed split carries unknown root keys "
                f"({', '.join(sorted(str(k) for k in unknown_root))}); refusing drift"
            )
        if split.get("schema_version") != 1:
            raise _fail("committed split schema_version must be 1")
        for section in ("dev", "held_out"):
            rows = split.get(section)
            if not isinstance(rows, list) or not rows:
                raise _fail(f"committed split {section!r} must be a non-empty list")
            for row in rows:
                if not isinstance(row, dict):
                    raise _fail(f"committed split {section!r} rows must be objects")
                unknown_row = set(row) - SPLIT_ROW_KEYS
                if unknown_row:
                    raise _fail(
                        f"committed split {section!r} row carries unknown keys "
                        f"({', '.join(sorted(str(k) for k in unknown_row))})"
                    )
                for key in ("task_id", "task_path", "package_digest"):
                    value = row.get(key)
                    if not isinstance(value, str) or not value.strip():
                        raise _fail(f"committed split {section!r} row {key!r} must be non-empty")
                if _SHA256_RE.fullmatch(row["package_digest"]) is None:
                    raise _fail(
                        f"committed split {section!r} package_digest is malformed: "
                        f"{row['package_digest']!r}"
                    )
                self._check_split_path(row["task_path"], section)
        dev, held_out = split["dev"], split["held_out"]
        dev_ids = [r["task_id"] for r in dev]
        held_ids = [r["task_id"] for r in held_out]
        if len(set(dev_ids)) != len(dev_ids):
            raise _fail("committed split dev task_ids must be unique")
        if set(dev_ids) & set(held_ids):
            raise _fail("committed split dev and held_out task_ids must be disjoint")
        dev_digests = [r["package_digest"] for r in dev]
        held_digests = [r["package_digest"] for r in held_out]
        if len(set(dev_digests)) != len(dev_digests):
            raise _fail("committed split dev package_digests must be unique")
        if set(dev_digests) & set(held_digests):
            raise _fail("committed split dev and held_out package_digests must be disjoint")
        dev_paths = {_norm_split_path(r["task_path"]) for r in dev}
        held_paths = {_norm_split_path(r["task_path"]) for r in held_out}
        if dev_paths & held_paths:
            raise _fail("committed split dev and held_out task_paths must be disjoint")
        digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
        return split, digest

    @staticmethod
    def _check_split_path(task_path: str, section: str) -> None:
        if task_path.startswith("/") or "\\" in task_path:
            raise _fail(f"committed split {section!r} task_path must be relative: {task_path!r}")
        parts = task_path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise _fail(f"committed split {section!r} task_path escapes: {task_path!r}")
        if section == "dev" and task_path.startswith("~"):
            raise _fail(f"committed split dev task_path must be repo-relative: {task_path!r}")

    def _check_registry(self, lab_root: Path, split: dict[str, Any]) -> None:
        """Dev optimization requires registered + measurement/training uses.

        Heldout tasks are pinned separately without registration and are never
        executed here: any ``heldout`` allow-listing on a dev task refuses.
        Missing or corrupt records fail closed.
        """
        for entry in split["dev"]:
            task_id = entry["task_id"]
            record_path = lab_root / "library" / "registry" / f"{task_id}.json"
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise _fail(
                    f"task {task_id!r} has no registry record, no evaluation: {exc}"
                ) from exc
            except (OSError, ValueError) as exc:
                raise _fail(f"task {task_id!r} registry record is corrupt: {exc}") from exc
            if not isinstance(record, dict):
                raise _fail(f"task {task_id!r} registry record must be an object")
            if record.get("state") != "registered":
                raise _fail(
                    f"task {task_id!r} is not registered "
                    f"(state={record.get('state')!r}); dev optimization needs registration"
                )
            uses = record.get("allowed_uses")
            if not isinstance(uses, list) or not uses:
                raise _fail(f"task {task_id!r} registry record has no allowed_uses")
            if "heldout" in uses:
                raise _fail(
                    f"task {task_id!r} is heldout-allow-listed and must never run "
                    "through the dev gate"
                )
            for required in ("measurement", "training"):
                if required not in uses:
                    raise _fail(
                        f"task {task_id!r} lacks {required!r} allow-listing "
                        f"(has {uses!r}); dev optimization needs both"
                    )
            digests = record.get("digests")
            package = digests.get("package") if isinstance(digests, dict) else None
            if package != entry["package_digest"]:
                raise _fail(
                    f"task {task_id!r} dev digest drift: split "
                    f"{entry['package_digest']!r} != registry {package!r}"
                )
            if record.get("task_path") != entry["task_path"]:
                raise _fail(
                    f"task {task_id!r} path drift: split {entry['task_path']!r} != "
                    f"registry {record.get('task_path')!r}"
                )

    # -- trees / identity --------------------------------------------------

    @staticmethod
    def _checked_mapping(mapping: Mapping[str, str], *, label: str) -> dict[str, str]:
        if not isinstance(mapping, Mapping):
            raise _fail(f"{label} files must be a mapping of path to text")
        checked: dict[str, str] = {}
        for key, value in mapping.items():
            _check_tree_key(key)
            if not isinstance(value, str):
                raise _fail(f"{label} file {key!r} must be text")
            if key in checked:
                raise _fail(f"{label} file {key!r} is duplicated")
            checked[key] = value
        return checked

    def _materialize_tree(
        self, record_dir: Path, candidate_hash: str, side: str,
        mapping: Mapping[str, str],
    ) -> str:
        """Freeze one mapping into an immutable tree dir; return its digest."""
        checked = self._checked_mapping(mapping, label=side)
        _validate_tree_config(checked)
        entries = sorted(
            ((rel, text.encode("utf-8")) for rel, text in checked.items()),
            key=lambda item: item[0],
        )
        expected = _digest_entries(entries)
        tree_root = record_dir / "trees" / candidate_hash / side
        if tree_root.is_dir():
            try:
                if _dir_digest(tree_root) == expected:
                    return expected
            except LabGateError:
                pass
            self._remove_tree(tree_root)
        tree_root.mkdir(parents=True, exist_ok=True)
        for relative, blob in entries:
            target = tree_root / Path(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            target.chmod(0o444)
        actual = _dir_digest(tree_root)
        if actual != expected:
            raise _fail(f"{side} tree digest drift after materialization: {actual}")
        return actual

    @staticmethod
    def _remove_tree(tree_root: Path) -> None:
        for child in sorted(tree_root.iterdir()):
            if child.is_dir() and not child.is_symlink():
                for sub in sorted(child.rglob("*"), reverse=True):
                    if sub.is_symlink() or sub.is_file():
                        sub.unlink()
                    elif sub.is_dir():
                        sub.rmdir()
                child.rmdir()
            else:
                child.unlink()

    def _content_hash(
        self, current_files: Mapping[str, str], candidate_files: Mapping[str, str],
        tasks: tuple[str, ...], split_digest: str, trees: dict[str, str],
    ) -> str:
        payload = {
            "current_files": sorted(self._checked_mapping(current_files, label="current").items()),
            "candidate_files": sorted(self._checked_mapping(candidate_files, label="candidate").items()),
            "task_ids": list(tasks),
            "agent": self.config.agent,
            "model": self.config.model,
            "environment": self.config.environment,
            "timeout_seconds": self.config.timeout_seconds,
            "cost_limit_usd": self.config.cost_limit_usd,
            "est_cost_usd": self.config.est_cost_usd,
            "max_requests": self.config.max_requests,
            "max_input_tokens": self.config.max_input_tokens,
            "max_output_tokens": self.config.max_output_tokens,
            "max_total_tokens": self.config.max_total_tokens,
            "episode_repeats": self.config.episode_repeats,
            "split_digest": split_digest,
            "trees": trees,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _load_manifest(self, path: Path, candidate_id: str, content_hash: str) -> dict | None:
        if not path.is_file():
            return None
        manifest = _read_json_file(path, label="candidate manifest")
        if not isinstance(manifest, dict):
            raise _fail(f"candidate manifest is corrupt: {path}")
        if manifest.get("candidate_id") != candidate_id:
            raise _fail(f"candidate manifest identity mismatch: {path}")
        if manifest.get("content_hash") != content_hash:
            raise _fail(
                f"conflicting content for candidate_id {candidate_id!r}: the durable "
                "manifest already binds this id to different files/tasks/binding; "
                "reuse the id only for the identical candidate"
            )
        if not isinstance(manifest.get("pairings"), list):
            raise _fail(f"candidate manifest pairings are corrupt: {path}")
        return manifest

    def _write_manifest(self, path: Path, manifest: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _job_name(candidate_hash: str, task_id: str, repeat: int, side: str) -> str:
        short = candidate_hash[:12]
        fixed = len(f"rg-{short}") + len(f"-{repeat}-{side}")
        task_slug = _slug(task_id, max(1, 80 - fixed - 1))
        name = f"rg-{short}-{task_slug}-{repeat}-{side}"
        if _JOB_NAME_RE.fullmatch(name) is None:  # pragma: no cover - defensive
            raise _fail(f"derived job name is invalid: {name!r}")
        return name

    # -- queue reads (scoped; exact spec IDs / job names only) --------------

    def _find_spec(self, lab_root: Path, spec_id: str) -> tuple[str, Path]:
        if not re.fullmatch(r"[0-9A-Za-z-]+", spec_id or ""):
            raise _fail(f"refusing to scan queue with malformed spec_id: {spec_id!r}")
        matches: list[tuple[str, Path]] = []
        for state in QUEUE_STATES:
            for path in (lab_root / "queue" / state).glob(f"*-{spec_id}.json"):
                matches.append((state, path))
        if len(matches) != 1:
            raise _fail(
                f"queued spec {spec_id!r} is not uniquely present "
                f"({len(matches)} matches); refusing to proceed"
            )
        return matches[0]

    def _find_by_job_name(self, lab_root: Path, job_name: str) -> tuple[str, str, Path] | None:
        """Recover (spec_id, state, path) for one job name across the queue."""
        for state in QUEUE_STATES:
            directory = lab_root / "queue" / state
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("name") == job_name:
                    spec_id = payload.get("spec_id")
                    if isinstance(spec_id, str) and spec_id:
                        return spec_id, state, path
        return None

    def _read_state(self, lab_root: Path, spec_id: str) -> str | None:
        if not spec_id:
            return None
        matches = []
        for state in QUEUE_STATES:
            for _ in (lab_root / "queue" / state).glob(f"*-{spec_id}.json"):
                matches.append(state)
        return matches[0] if len(matches) == 1 else None

    def _refresh_states(self, lab_root: Path, pairings: list[dict]) -> None:
        for pairing in pairings:
            if pairing.get("spec_id"):
                pairing["state"] = self._read_state(lab_root, pairing["spec_id"])

    # -- prepare / submit (deterministic identity closes the crash window) --

    def _prepare_argv(
        self, *, task_path: str, job_name: str, spec_rel: str, tree_dir: Path, tree_digest: str,
    ) -> list[str]:
        cfg = self.config
        argv = list(self._cli) + [
            "tasks", "prepare", task_path,
            "--name", job_name,
            "--agent", cfg.agent,
            "--model", cfg.model,
            "--environment", cfg.environment,
            "--max-requests", str(cfg.max_requests),
            "--max-input-tokens", str(cfg.max_input_tokens),
            "--max-output-tokens", str(cfg.max_output_tokens),
            "--harness-tree", tree_dir.as_posix(),
            "--harness-tree-sha256", tree_digest,
            "--output", spec_rel,
            "--submitted-by", cfg.submitted_by,
            "--json",
        ]
        if cfg.timeout_seconds is not None:
            argv += ["--timeout-seconds", str(cfg.timeout_seconds)]
        if cfg.cost_limit_usd is not None:
            argv += ["--cost-limit-usd", str(cfg.cost_limit_usd)]
        if cfg.est_cost_usd is not None:
            argv += ["--estimated-cost-usd", str(cfg.est_cost_usd)]
        if cfg.max_total_tokens is not None:
            argv += ["--max-total-tokens", str(cfg.max_total_tokens)]
        return argv

    def _run_cli(self, lab_root: Path, argv: list[str]) -> tuple[int, str, str]:
        try:
            return self.runner.run(
                argv, cwd=lab_root, timeout=self.cli_timeout_seconds, python=self._python,
            )
        except LabGateError:
            raise
        except Exception as exc:  # fake runners may raise arbitrary errors
            raise _fail(f"lab CLI failed: {type(exc).__name__}: {exc}") from exc

    def _verify_spec_payload(
        self, payload: dict, pairing: dict, entry: dict, tree_digest: str,
    ) -> None:
        """A queued/prepared spec must still be exactly this campaign's spec."""
        expected = {
            "name": pairing["job_name"],
            "agent": self.config.agent,
            "model": self.config.model,
            "environment": self.config.environment,
        }
        for key, want in expected.items():
            if payload.get(key) != want:
                raise _fail(
                    f"spec {pairing['job_name']!r} drifts on {key!r}: "
                    f"{payload.get(key)!r} != {want!r}"
                )
        task_field = payload.get("task_path") or payload.get("task")
        if task_field != entry["task_path"]:
            raise _fail(
                f"spec {pairing['job_name']!r} drifts on task: "
                f"{task_field!r} != {entry['task_path']!r}"
            )
        if payload.get("harness_tree_sha256") != tree_digest:
            raise _fail(
                f"spec {pairing['job_name']!r} drifts on harness tree digest: "
                f"{payload.get('harness_tree_sha256')!r} != {tree_digest!r}"
            )
        package = payload.get("task_package_digest")
        if package is not None and package != entry["package_digest"]:
            raise _fail(
                f"spec {pairing['job_name']!r} drifts on package digest: "
                f"{package!r} != {entry['package_digest']!r}"
            )

    def _submit_all(self, ctx: dict) -> bool:
        """Prepare+submit every pairing; return whether any spec was recovered."""
        recovered = False
        manifest = ctx["manifest"]
        entry_by_id = {e["task_id"]: e for e in ctx["split"]["dev"]}
        tree_dirs = {
            side: ctx["record_dir"] / "trees" / ctx["candidate_hash"] / side
            for side in SIDES
        }
        for pairing in ctx["pairings"]:
            entry = entry_by_id[pairing["task_id"]]
            side = pairing["side"]
            tree_digest = ctx["trees"][side]
            spec_path = ctx["lab_root"] / pairing["spec_path"]
            if pairing.get("spec_id") is not None:
                state, path = self._find_spec(ctx["lab_root"], pairing["spec_id"])
                payload = _read_json_file(path, label=f"queued spec {pairing['spec_id']}")
                self._verify_spec_payload(payload, pairing, entry, tree_digest)
                pairing["state"] = state
                continue
            # Crash window: submitted before the manifest learned the spec_id.
            found = self._find_by_job_name(ctx["lab_root"], pairing["job_name"])
            payload: dict | None = None
            if found is not None:
                spec_id, state, path = found
                payload = _read_json_file(path, label=f"queued spec {spec_id}")
                self._verify_spec_payload(payload, pairing, entry, tree_digest)
                pairing["spec_id"] = spec_id
                pairing["state"] = state
                recovered = True
                self._write_manifest(ctx["manifest_path"], manifest)
                continue
            if not spec_path.is_file():
                argv = self._prepare_argv(
                    task_path=entry["task_path"], job_name=pairing["job_name"],
                    spec_rel=pairing["spec_path"], tree_dir=tree_dirs[side],
                    tree_digest=tree_digest,
                )
                code, stdout, stderr = self._run_cli(ctx["lab_root"], argv)
                if code != 0:
                    raise _fail(
                        f"lab CLI tasks prepare failed for {pairing['job_name']!r} "
                        f"(exit {code}); stderr: {_truncate(stderr)}"
                    )
                prepared = _parse_cli_json(stdout, stderr, what="tasks prepare")
                payload = prepared.get("spec")
                if not isinstance(payload, dict):
                    raise _fail(
                        f"lab CLI tasks prepare returned no spec for {pairing['job_name']!r}"
                    )
            else:
                payload = _read_json_file(spec_path, label=f"prepared spec {pairing['spec_path']}")
            self._verify_spec_payload(payload, pairing, entry, tree_digest)
            code, stdout, stderr = self._run_cli(
                ctx["lab_root"], list(self._cli) + ["submit", pairing["spec_path"]],
            )
            if code != 0:
                raise _fail(
                    f"lab CLI submit failed for {pairing['job_name']!r} (exit {code}); "
                    f"stderr: {_truncate(stderr)}"
                )
            pairing["spec_id"] = _parse_spec_id(stdout, stderr)
            pairing["state"] = self._read_state(ctx["lab_root"], pairing["spec_id"]) or "waiting"
            self._write_manifest(ctx["manifest_path"], manifest)
        return recovered

    # -- waiting / ticking (scoped to these exact spec IDs) -----------------

    def _wait_for_approval(
        self, lab_root: Path, pairings: list[dict], deadline: float,
    ) -> str | None:
        """Wait until every spec is approved/terminal or the deadline bounds."""
        while True:
            self._refresh_states(lab_root, pairings)
            states = [p["state"] for p in pairings]
            if all(s in ("approved", "running", "done", "failed") for s in states):
                if any(s == "failed" for s in states):
                    return REASON_FAILED
                return None
            if any(s in ("rejected",) for s in states):
                return REASON_WITHDRAWN
            if time.monotonic() >= deadline:
                return REASON_APPROVAL_TIMEOUT
            time.sleep(self._poll())

    def _tick_serial(self, lab_root: Path, pairings: list[dict], deadline: float) -> str | None:
        """Tick approved specs one at a time, in pairing (interleaved) order."""
        for pairing in pairings:
            self._refresh_states(lab_root, pairings)
            state = pairing["state"]
            if state in ("done", "failed"):
                if state == "failed":
                    return REASON_FAILED
                continue
            if state == "running":
                continue
            if state != "approved":
                return REASON_STOPPED
            if time.monotonic() >= deadline:
                return REASON_TICK_TIMEOUT
            code, _stdout, stderr = self._run_cli(
                lab_root,
                list(self._cli) + ["tick", "--spec-id", pairing["spec_id"]],
            )
            self._refresh_states(lab_root, pairings)
            ticked = pairing["state"]
            if code == 0 and ticked == "done":
                continue
            if code != 0 and "stop" in _truncate(stderr, 400).lower():
                return REASON_STOPPED
            if ticked in ("running", "waiting", "approved"):
                # Bounded: tick returned without finishing this episode.
                return REASON_TICK_TIMEOUT
            if ticked == "failed":
                return REASON_FAILED
            if ticked == "done":
                continue
            return REASON_SPEC_TIMEOUT
        return None

    def _wait_for_terminal(
        self, lab_root: Path, pairings: list[dict], deadline: float,
    ) -> str | None:
        while True:
            self._refresh_states(lab_root, pairings)
            states = [p["state"] for p in pairings]
            if all(s == "done" for s in states):
                return None
            if any(s == "failed" for s in states):
                return REASON_FAILED
            if time.monotonic() >= deadline:
                return REASON_SPEC_TIMEOUT
            time.sleep(self._poll())

    def _poll(self) -> float:
        return max(0.01, float(self.config.tick_poll_seconds))

    # -- evidence collection -------------------------------------------------

    def _job_dir(self, lab_root: Path, pairing: dict) -> Path | None:
        job_dir = lab_root / "runs" / pairing["job_name"]
        return job_dir if job_dir.is_dir() else None

    def _read_job(self, job_dir: Path) -> dict:
        result = _read_json_file(job_dir / "result.json", label=f"job {job_dir.name}")
        if not isinstance(result, dict):
            raise _fail(f"job result is not an object: {job_dir}")
        return result

    def _trial_dirs(self, job_dir: Path) -> list[Path]:
        trials: list[Path] = []
        for child in sorted(job_dir.iterdir()):
            if child.is_dir() and (child / "result.json").is_file():
                trials.append(child)
        return trials

    @staticmethod
    def _scored_reward(trial_result: dict) -> tuple[float | None, str | None]:
        """The reward iff the trial scored with no infrastructure failure.

        Mirrors the Lab's own refusal (``matrix_run_outcome``): job/trial
        exceptions, missing ``finished_at``, boolean or non-finite or absent
        verifier reward all yield ``(None, cause)`` — never a fabricated 0.
        """
        if trial_result.get("exception_info"):
            info = trial_result["exception_info"]
            cause = info.get("message") if isinstance(info, dict) else str(info)
            return None, _truncate(f"exception: {cause}", 500)
        if not trial_result.get("finished_at"):
            return None, "trial did not finish"
        verifier = trial_result.get("verifier_result") or {}
        rewards = verifier.get("rewards") or {}
        raw = rewards.get("reward")
        if raw is None:
            return None, "verifier produced no reward"
        if isinstance(raw, bool):
            return None, "verifier reward is a boolean, not a number"
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None, "verifier reward is not numeric"
        if not math.isfinite(value):
            return None, "verifier reward is not finite"
        return value, None

    def _collect(self, *, ctx: dict, started: float, incomplete: str | None) -> dict:
        lab_root = ctx["lab_root"]
        repeats = self.config.episode_repeats
        scores: dict[str, list[float | None]] = {"candidate": [], "current": []}
        failures: dict[str, list[dict]] = {"candidate": [], "current": []}
        evidence: list[dict] = []
        observed: dict[str, list[bool]] = {"candidate": [], "current": []}
        unobserved_cause: dict[str, str] = {}

        for pairing in ctx["pairings"]:
            side = pairing["side"]
            episode: dict[str, Any] = {
                "task_id": pairing["task_id"], "repeat": pairing["repeat"],
                "side": side, "job_name": pairing["job_name"],
                "spec_id": pairing["spec_id"], "spec_path": pairing["spec_path"],
                "state": pairing["state"],
            }
            job_dir = self._job_dir(lab_root, pairing)
            reward: float | None = None
            status = "unobserved"
            failure: dict | None = None
            if job_dir is None:
                cause = unobserved_cause.setdefault(
                    side, "no retained job directory; the campaign did not finish"
                )
                failure = {"task": pairing["task_id"], "stage": "campaign", "cause": cause}
                episode["job_dir"] = None
            else:
                episode["job_dir"] = job_dir.as_posix()
                try:
                    job_result = self._read_job(job_dir)
                except LabGateError as exc:
                    job_result = None
                    episode["job_read_error"] = str(exc)
                trials = self._trial_dirs(job_dir) if job_result is not None else []
                expected_trials = 1
                total = job_result.get("n_total_trials") if job_result else None
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                    expected_trials = total
                finished = bool(job_result and job_result.get("finished_at"))
                if job_result is None or len(trials) != expected_trials or not finished:
                    cause = unobserved_cause.setdefault(
                        side,
                        "retained job is incomplete; the campaign did not finish",
                    )
                    failure = {"task": pairing["task_id"], "stage": "campaign", "cause": cause}
                    status = "incomplete_job"
                elif len(trials) == 1:
                    trial_dir = trials[0]
                    episode["trial_dir"] = trial_dir.as_posix()
                    trial_result = _read_json_file(
                        trial_dir / "result.json", label=f"trial {trial_dir.name}",
                    )
                    reward, cause = self._scored_reward(trial_result)
                    if reward is None:
                        failure = {"task": pairing["task_id"], "stage": "trial", "cause": cause}
                        status = "infra_failure"
                    else:
                        status = "scored"
                else:
                    cause = unobserved_cause.setdefault(
                        side, "unexpected trial count in retained job"
                    )
                    failure = {"task": pairing["task_id"], "stage": "campaign", "cause": cause}
                    status = "incomplete_job"
            scores[side].append(reward)
            observed[side].append(status == "scored")
            episode["status"] = status
            episode["reward"] = reward
            episode["failure"] = failure
            evidence.append(episode)
            if failure is not None:
                failures[side].append(failure)

        # Complete evidence requires every episode observed AND scored; any
        # infra/unscored episode keeps complete=False so a minimum-valid pair
        # subset can never publish through the missing-evidence case.
        unobserved_any = any(not flag for flags in observed.values() for flag in flags)
        if incomplete is None and unobserved_any:
            incomplete = REASON_UNOBSERVED

        metrics: dict[str, Any] = {
            "candidate_scores": tuple(scores["candidate"]),
            "current_scores": tuple(scores["current"]),
            "episode_failures": sum(
                score is None for side in SIDES for score in scores[side]
            ),
            "episode_repeats": repeats,
        }
        for side in SIDES:
            metrics[f"{side}_failures"] = tuple(failures[side])
            metrics[f"{side}_residue"] = 0
            metrics[f"{side}_score"] = float(
                sum(score for score in scores[side] if score is not None)
            )
            metrics[f"{side}_agents"] = {}
            metrics[f"{side}_paths"] = tuple(
                episode["trial_dir"] if episode.get("trial_dir") else episode.get("job_dir")
                for episode in evidence if episode["side"] == side
            )
        covered = [
            side for side in SIDES
            if all(
                episode["status"] == "scored"
                for episode in evidence if episode["side"] == side
            )
        ]
        # Omit evaluation_sides when both sides are fully covered (Reef's own
        # default); otherwise list exactly the covered sides so settlement
        # never clears a failure manifest for tasks with missing evidence.
        if len(covered) != len(SIDES):
            metrics["evaluation_sides"] = covered

        lab_root, split_file, record_dir, python = self.config.resolved_paths()
        metadata: dict[str, Any] = {
            "evaluator": EVALUATOR_NAME,
            "evaluator_version": EVALUATOR_VERSION,
            "candidate_id": ctx["candidate_id"],
            "task_ids": list(ctx["tasks"]),
            "dev_task_ids": [e["task_id"] for e in ctx["split"]["dev"]],
            "heldout_task_ids": [e["task_id"] for e in ctx["split"]["held_out"]],
            "agent": self.config.agent,
            "model": self.config.model,
            "environment": self.config.environment,
            "reef_commit": self.config.reef_commit or EXPECTED_REEF_COMMIT,
            "lab_root": lab_root.as_posix(),
            "split_path": split_file.as_posix(),
            "split_digest": ctx["split_digest"],
            "candidate_tree_sha256": ctx["trees"]["candidate"],
            "current_tree_sha256": ctx["trees"]["current"],
            "manifest_path": ctx["manifest_path"].as_posix(),
            "spec_ids": [p["spec_id"] for p in ctx["pairings"]],
            "spec_details": [
                {
                    "task_id": p["task_id"], "repeat": p["repeat"], "side": p["side"],
                    "job_name": p["job_name"], "spec_path": p["spec_path"],
                    "spec_id": p["spec_id"], "state": p["state"],
                }
                for p in ctx["pairings"]
            ],
            "approve_commands": ctx["manifest"].get("approve_commands", []),
            "evidence": evidence,
            "complete": incomplete is None,
            "incomplete_reason": incomplete,
            "observed_pairs": sum(
                1 for index in range(len(scores["candidate"]))
                if observed["candidate"][index] and observed["current"][index]
            ),
            "residue_unknown": True,
            "agents_unknown": True,
            "paths_are_trial_dirs": True,
            "evaluation_seconds": time.monotonic() - started,
        }
        return {"metrics": metrics, "metadata": metadata}
