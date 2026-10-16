"""Automatic missing-test-assertion fixer for EnvCheck mutation survivors.

Reads a ``evallab.verifier_mutation.report/v1`` report, ranks only survivors
with ``executed=True`` from ``coverage='guided'`` coverage, asks a model for a
bounded structured proposal of additional semantic Python tests (or an
explicit not-repairable rationale), derives a test-only variant through the
existing :func:`evallab.task_variants.derive_task` path, and validates it by
running the actual reference / nop / previous-survivor scripts through
``runner.run_matrix`` against the repaired package.

Fails closed everywhere: missing reports, skips, infrastructure outcomes and
partial rewards never certify a repair. No paid API is touched in preview
mode; every model call is preceded by a live subscription quota check
(weekly consumption must stay ``<= 60%``). No silent retries, no model
fallback.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import re
import shlex
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.execution_contracts import new_ulid
from evallab.schemas import ContractModel, ExperimentMatrix, MatrixRun

SCHEMA_PROPOSAL = "evallab.verifier_repair.proposal/v1"
SCHEMA_RESULT = "evallab.verifier_repair.result/v1"

#: Lineage transform recorded by :func:`derive_task`.
TRANSFORM_ID = "verifier-repair@1"

#: Models the repair proposer may address. Passed through verbatim; there is
#: no fallback and no silent substitution.
ALLOWED_MODELS = frozenset({"gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"})

#: Weekly subscription consumption ceiling: refuse every model call above it.
MAX_WEEKLY_USED = 0.60

#: Bounds keeping a model proposal small and reviewable.
MAX_TESTS = 8
MAX_TEST_BYTES = 8 * 1024
MAX_TOTAL_TEST_BYTES = 32 * 1024
MAX_COMMAND_BYTES = 300
MAX_RATIONALE_CHARS = 2_000

#: Context truncation bounds (bytes).
MAX_CAPTURE_FILE_BYTES = 32 * 1024
MAX_TOTAL_CAPTURE_BYTES = 256 * 1024
MAX_HIDDEN_TEST_BYTES = 128 * 1024
MAX_PATCH_CHARS = 8_000

#: Conservative source-fingerprint and resource checks, not an OS sandbox.
#: Proposed code executes only inside an isolated task-verifier container.
_DISALLOWED_TEST_FEATURES = (
    "__file__",
    "inspect.",
    "hashlib",
    "test.patch",
    ".patch",
    "fingerprint",
    "solve.sh",
    "inputs/",
    "subprocess",
    "os.system",
    "os.exec",
    "socket",
    "urllib",
    ".from_pretrained(",
    "torch.hub.load",
    "llama_cpp",
    "import vllm",
    "import ollama",
)

#: String/bytes literals longer than this compared with ``==``/``!=``/``in``
#: count as hardcoded reference byte checks.
MAX_LITERAL_COMPARE = 200

RepairVerdict = Literal["repaired", "partial", "rejected", "unrepairable", "blocked"]


class RepairError(ValueError):
    """Raised when a repair step violates its contract (fail closed)."""


class QuotaBlocked(RepairError):
    """Raised when the subscription quota gate refuses a model call."""


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


class RepairTest(ContractModel):
    """One additional semantic test file, appended as a new-file diff to ``tests/test.patch``."""

    id: str = Field(min_length=1, max_length=64)
    #: New path inside the testbed repository, not a Harbor-package member.
    repo_path: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1)


class RepairUnrepairable(ContractModel):
    """One ranked survivor the model declines to kill, with its reason.

    ``kind`` is ``equivalent`` (the mutant is behaviour-equivalent) or
    ``out-of-scope`` (the instruction does not require the changed
    behaviour, e.g. incidental immutability). Declared survivors may still
    escape validation; they yield ``partial``, never ``repaired``.
    """

    mutant_id: str = Field(min_length=1, max_length=64)
    kind: Literal["equivalent", "out-of-scope"] = "equivalent"
    reason: str = Field(min_length=1, max_length=MAX_RATIONALE_CHARS)


class RepairProposal(ContractModel):
    """Bounded structured model output: new tests plus how to run them."""

    schema_: Literal["evallab.verifier_repair.proposal/v1"] = Field(
        alias="schema", default=SCHEMA_PROPOSAL
    )
    repairable: bool
    rationale: str = Field(min_length=1, max_length=MAX_RATIONALE_CHARS)
    tests: list[RepairTest] = Field(default_factory=list)
    command: str | None = None
    model: str | None = None
    mutant_ids: list[str] = Field(default_factory=list)
    unrepairable: list[RepairUnrepairable] = Field(default_factory=list)


class RepairCheck(ContractModel):
    """One validation-trial outcome with its preserved evidence."""

    name: str
    role: str
    script: str | None
    status: str
    reward: float | None = None
    error: str | None = None


class RepairResult(ContractModel):
    """Durable outcome of one repair attempt."""

    schema_: Literal["evallab.verifier_repair.result/v1"] = Field(
        alias="schema", default=SCHEMA_RESULT
    )
    task: str = ""
    verdict: RepairVerdict = "blocked"
    reason: str = ""
    proposal: RepairProposal | None = None
    variant_digest: str | None = None
    variant_record: str | None = None
    checks: list[RepairCheck] = Field(default_factory=list)


@dataclass(frozen=True)
class QuotaStatus:
    """Observed subscription state before a model call.

    Carries only the gate facts. Account identifiers, credit balances and
    credit reset IDs from the quota account response must never enter this
    object; see :func:`persisted_quota_record`.
    """

    allowed: bool
    weekly_used: float
    detail: str = ""
    window: str = ""
    observed_at: str = ""


def persisted_quota_record(status: QuotaStatus, model: str) -> dict[str, object]:
    """Receipt-safe quota facts: percent, window, observed time, model only."""
    return {
        "allowed": status.allowed,
        "weekly_used": status.weekly_used,
        "window": status.window,
        "observed_at": status.observed_at,
        "model": model,
    }


#: Returns the live subscription state; injected so tests never touch auth.
QuotaChecker = Callable[[], QuotaStatus]

#: Runs one proposal turn; returns the raw model text plus usage facts.
#: Injected so the module itself never spends.
ModelCaller = Callable[[str, Mapping[str, Any]], tuple[str, dict[str, Any]]]

#: Grades a matrix; defaults to ``runner.run_matrix`` via the validator.
MatrixRunner = Callable[[ExperimentMatrix, Path], list[dict[str, Any]]]

#: Derives a test-only variant; defaults to ``task_variants.derive_task``.
DeriveFn = Callable[..., Any]


# --------------------------------------------------------------------------- #
# Quota gate
# --------------------------------------------------------------------------- #


def ensure_quota(checker: QuotaChecker) -> QuotaStatus:
    """Fail closed unless ordinary usage is allowed and weekly use is <= 60%."""
    status = checker()
    if not status.allowed:
        raise QuotaBlocked(f"subscription usage not allowed: {status.detail}")
    if not (0.0 <= status.weekly_used <= 1.0):
        raise QuotaBlocked(f"unparsable weekly usage: {status.weekly_used!r}")
    if status.weekly_used > MAX_WEEKLY_USED:
        raise QuotaBlocked(
            f"weekly subscription consumption {status.weekly_used:.0%} exceeds "
            f"{MAX_WEEKLY_USED:.0%} cap; refusing model call"
        )
    return status


def read_codex_quota() -> QuotaStatus:
    """Read live account-wide weekly usage without an inference or credential export."""
    import asyncio
    import os

    from evallab.gepa_optimizer.codex_transport import _scrub_env

    async def read_account() -> QuotaStatus:
        process = await asyncio.create_subprocess_exec(
            str(_codex_executable()),
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_scrub_env(dict(os.environ)),
        )
        assert process.stdin is not None and process.stdout is not None

        async def request(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            async with asyncio.timeout(30):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if message.get("id") == identifier:
                        if "error" in message:
                            raise QuotaBlocked(f"Codex {method} failed")
                        return message["result"]
            raise QuotaBlocked("Codex account reader ended without a response")

        try:
            await request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "evallab-repair-quota", "version": "1"},
                    "capabilities": {},
                },
            )
            account = await request(2, "account/read", {"refreshToken": False})
            usage = await request(3, "account/rateLimits/read", {})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise QuotaBlocked("Codex is not authenticated through ChatGPT subscription")
            limits = usage.get("rateLimits") or {}
            windows = [limits.get("primary"), limits.get("secondary")]
            weekly = [
                window
                for window in windows
                if isinstance(window, dict) and window.get("windowDurationMins") == 10080
            ]
            if not weekly:
                raise QuotaBlocked("live weekly quota is unavailable")
            return QuotaStatus(
                allowed=usage.get("ordinaryUsageAllowed") is True,
                weekly_used=max(float(window["usedPercent"]) for window in weekly) / 100,
                window="10080 minutes",
                observed_at=datetime.now(UTC).isoformat(),
            )
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()

    try:
        return asyncio.run(read_account())
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        raise QuotaBlocked(f"live subscription quota unavailable: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# Subscription Codex proposer (no paid-credit fallback, no retries)
# --------------------------------------------------------------------------- #


#: Codex CLI used for repair proposals. Resolved at call time so tests can
#: substitute it; must be a ChatGPT-subscription login, never API-key auth.
def _codex_executable() -> Path:
    from evallab.gepa_optimizer.codex_transport import CODEX_EXECUTABLE

    return CODEX_EXECUTABLE.resolve()


def codex_model_caller(
    prompt: str,
    context: Mapping[str, Any],
    *,
    model: str,
    output_dir: Path,
    timeout_seconds: int = 900,
    executable: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """One text-only subscription proposal, with durable private transport receipts."""
    import os
    import subprocess

    from evallab.execution_contracts import persist_private_bytes
    from evallab.gepa_optimizer import codex_transport

    del context
    if model not in ALLOWED_MODELS:
        raise RepairError(f"model {model!r} is not an approved repair model; no fallback")
    exe = executable or _codex_executable()
    env = codex_transport._scrub_env(dict(os.environ))
    directory = output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    cwd = directory / "cwd"
    cwd.mkdir(mode=0o700)
    prompt_file = directory / "prompt.txt"
    persist_private_bytes(prompt_file, prompt.encode(), secrets=())
    version = (
        subprocess.run(
            [str(exe), "--version"], env=env, capture_output=True, timeout=20, check=True
        )
        .stdout.decode()
        .strip()
    )
    argv = [
        str(exe),
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-m",
        model,
        "-c",
        f'model_provider="{codex_transport.MODEL_PROVIDER}"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'web_search="disabled"',
        "-c",
        'model_reasoning_effort="medium"',
        "-C",
        str(cwd),
        "-o",
        str(directory / "last-message.txt"),
        "-",
    ]
    for feature in codex_transport.DISABLED_FEATURES:
        argv.extend(("--disable", feature))
    events = directory / "events.jsonl"
    with (
        prompt_file.open("rb") as stdin,
        events.open("xb") as stdout,
        (directory / "stderr.log").open("xb") as stderr,
    ):
        try:
            completed = subprocess.run(
                argv,
                env=env,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepairError(
                f"Codex timed out; retained at {directory}; no automatic retry"
            ) from exc
    if completed.returncode:
        raise RepairError(
            f"Codex exited {completed.returncode}; retained at {directory}; no automatic retry"
        )
    try:
        response, usage = codex_transport.parse_transcript(events, allow_preamble=True)
    except codex_transport.CodexTransportError as exc:
        raise RepairError(f"unusable Codex response: {exc}; retained at {directory}") from exc
    return response, {**usage, "requested_model": model, "codex_version": version}


# --------------------------------------------------------------------------- #
# Report intake and survivor ranking
# --------------------------------------------------------------------------- #


def load_mutation_report(path: Path) -> dict[str, Any]:
    """Use the mutation producer's persisted contract, not a second schema."""
    from evallab.verifier_mutation import MutationReport

    try:
        report = MutationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairError(f"cannot load mutation report {path}: {exc}") from exc
    return report.model_dump(mode="json", by_alias=True)


def select_repair_survivors(report: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Admit by traced survivors, then retain every previous survivor.

    Executed-line coverage selects the task, not its acceptance subset.
    Untested fix-omission probes and partial survivors still need an outcome.
    """
    if not report.get("executed"):
        return [], "report is a plan (executed=false); nothing to repair"
    if report.get("coverage") != "guided":
        return [], (
            f"coverage={report.get('coverage')!r}: unrankable fallback, not zero; "
            "rerun the audit with coverage before repairing"
        )
    controls = {run.get("role"): run for run in report.get("runs", [])}
    for role, reward in (("capture-oracle", 1.0), ("capture-base", 0.0)):
        control = controls.get(role, {})
        if control.get("status") not in {"ok", "mismatch"} or control.get("reward") != reward:
            return [], f"unrankable {role} control"
    by_id = {m["id"]: m for m in report.get("mutants", []) if isinstance(m, dict)}
    ranked: list[dict[str, Any]] = []
    for run in report.get("runs", []):
        if not isinstance(run, dict):
            continue
        if run.get("role") != "mutant" or run.get("outcome") not in {"survived", "partial"}:
            continue
        mutant = by_id.get(run.get("mutant") or "")
        if mutant is None:
            return [], f"survivor {run.get('mutant')!r} has no mutation descriptor"
        ranked.append({"mutant": mutant, "run": run})
    if not any(
        row["mutant"].get("executed") is True and row["run"].get("outcome") == "survived"
        for row in ranked
    ):
        return [], "no executed guided survivor to repair"
    return ranked, ""


# --------------------------------------------------------------------------- #
# Repair context and prompt
# --------------------------------------------------------------------------- #


def _read_bounded(path: Path, limit: int) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return "", False
    truncated = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), truncated


def build_repair_context(
    report: Mapping[str, Any],
    ranked: Sequence[Mapping[str, Any]],
    *,
    package_dir: Path,
    mutation_run_dir: Path,
) -> dict[str, Any]:
    """Assemble the model context: instruction, captures, tests, patches."""
    package = Path(package_dir)
    run_dir = Path(mutation_run_dir)
    instruction, _ = _read_bounded(package / "instruction.md", MAX_HIDDEN_TEST_BYTES)
    hidden: dict[str, str] = {}
    hidden_total = 0
    for name in ("test.patch", "test_command.sh"):
        child = package / "tests" / name
        data = child.read_bytes()
        if hidden_total + len(data) > MAX_HIDDEN_TEST_BYTES:
            raise RepairError("hidden-test context exceeds the proposal bound")
        hidden_total += len(data)
        hidden[f"tests/{name}"] = data.decode("utf-8")
    captures: dict[str, str] = {}
    capture_total = 0
    capture_root = run_dir / "capture"
    if capture_root.is_dir():
        for child in sorted(capture_root.rglob("*")):
            if not child.is_file() or child.is_symlink():
                continue
            try:
                data = child.read_bytes()
            except OSError:
                continue
            clipped = data[:MAX_CAPTURE_FILE_BYTES]
            if capture_total + len(clipped) > MAX_TOTAL_CAPTURE_BYTES:
                break
            capture_total += len(clipped)
            captures[child.relative_to(capture_root).as_posix()] = clipped.decode(
                "utf-8", errors="replace"
            )
    survivors: list[dict[str, Any]] = []
    for entry in ranked:
        mutant = dict(entry["mutant"])
        patch = str(mutant.get("patch", ""))[:MAX_PATCH_CHARS]
        survivors.append(
            {
                "id": mutant.get("id"),
                "file": mutant.get("file"),
                "line": mutant.get("line"),
                "operator": mutant.get("operator"),
                "executed": mutant.get("executed"),
                "original": mutant.get("original"),
                "replacement": mutant.get("replacement"),
                "reward": entry["run"].get("reward"),
                "outcome": entry["run"].get("outcome"),
                "patch": patch,
            }
        )
    test_sh = (package / "tests/test.sh").read_text()
    test_patch = hidden.get("tests/test.patch", "")
    return {
        "task": report.get("task"),
        "workdir": read_task_workdir(package),
        "targets": list(report.get("targets", [])),
        "instruction": instruction,
        "hidden_tests": hidden,
        "captures": captures,
        "survivors": survivors,
        "test_command": hidden.get("tests/test_command.sh", ""),
        "patched_files": sorted(patched_paths(test_patch)),
        "reset_files": sorted(read_reset_list(test_sh)),
    }


def render_repair_prompt(context: Mapping[str, Any]) -> str:
    """Prompt the model for semantic assertions; bans fingerprint checks."""
    lines = [
        "You repair a weak hidden-test suite. Propose additional semantic Python",
        "tests that pass on the reference solution and fail on each surviving",
        "mutant below. Reply with exactly one JSON object matching the repair",
        "proposal schema: {schema, repairable, rationale, tests, command, model,",
        "mutant_ids, unrepairable}. Each test: {id, repo_path, code}. Each",
        "unrepairable entry: {mutant_id, kind, reason} with kind equivalent or",
        "out-of-scope.",
        f"The schema field is exactly {SCHEMA_PROPOSAL!r}; no markdown fences.",
        "The model field may be null; execution records the caller-selected model.",
        "Command may be null only when repairable is false and no tests will run.",
        "All code runs only inside the task's isolated verifier, not on the host.",
        "Rules:",
        "- Test observable behaviour of the task artifacts only.",
        "- Every asserted behavior must follow from the task contract, not merely",
        "  from a public API or a surviving edit elsewhere in the same module.",
        "- Address every listed survivor, including previously unexecuted fix",
        "  omissions. Executed-line coverage chooses the task, not which old",
        "  survivors must be rejected by its repaired verifier.",
        "- Keep tests offline. Use synthetic fixtures for external services and",
        "  ML plumbing; never load or run language models or download model weights.",
        "- NEVER inspect source or patch fingerprints: no __file__, inspect,",
        "  hashlib, .patch reads, solve.sh reads, or subprocess/network use.",
        "- Do not test incidental properties just to kill a mutant. For example,",
        "  immutability assertions are legitimate only if the task requires immutability.",
        "- NEVER hardcode reference bytes: no ==/!=/in against literals longer",
        "  than 200 characters.",
        "- repo_path is a NEW repository Python test file named test_*.py.",
        "  It may be inside test/ or tests/: these are TESTBED repository paths,",
        "  not the Harbor package. Never replace a file the hidden patch already",
        f"  carries. At most {MAX_TESTS} files, {MAX_TEST_BYTES} bytes each.",
        "- command runs every new test file with the task Python interpreter,",
        '  e.g. "python -m pytest test/contrib/test_repair_x.py -q"; it must',
        "  name each repo_path, stay under 300 chars, no shell metacharacters.",
        "  Use -m unittest and TestCase assertions if pytest is not part of this task's environment.",
        "- Every ranked survivor needs a disposition: list genuinely fixed IDs",
        "  in mutant_ids; list behaviour-equivalent or out-of-scope IDs in",
        "  unrepairable with a per-ID reason instead of pinning implementation",
        "  behaviour. Genuine holes keep their tests even when other survivors",
        "  are unrepairable; do not abandon useful assertions wholesale.",
        "",
        f"Task: {context.get('task')}",
        f"Targets: {context.get('targets')}",
        f"Testbed working directory: {context.get('workdir')}",
        f"Test command: {context.get('test_command')}",
        f"Resolved command from the scored reference: {context.get('resolved_test_command', '[unavailable]')}",
        f"Patched files: {context.get('patched_files')}",
        f"Reset files: {context.get('reset_files')}",
        "",
        "=== instruction.md ===",
        str(context.get("instruction") or ""),
    ]
    for survivor in context.get("survivors", []):  # type: ignore[union-attr]
        lines += [
            "",
            f"=== survivor {survivor['id']} ({survivor['outcome']}, "
            f"reward {survivor['reward']}) ===",
            f"{survivor['file']}:{survivor['line']} {survivor['operator']}: "
            f"{survivor['original']!r} -> {survivor['replacement']!r}",
            str(survivor["patch"]),
        ]
    captures = context.get("captures", {})  # type: ignore[union-attr]
    for name, body in list(captures.items())[:12]:
        lines += ["", f"=== capture {name} ===", str(body)[:MAX_CAPTURE_FILE_BYTES]]
    hidden = context.get("hidden_tests", {})  # type: ignore[union-attr]
    lines += ["", f"=== hidden tests ({len(hidden)} files) ==="]
    for name, body in hidden.items():
        lines += ["", f"=== {name} ===", body]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Proposal validation
# --------------------------------------------------------------------------- #


_COMMAND_FORBIDDEN = re.compile(r"[;&|`$><\n]")


def _literal_is_hardcoded(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        encoded = node.value if isinstance(node.value, bytes) else node.value.encode()
        return len(encoded) > MAX_LITERAL_COMPARE
    return False


def _has_hardcoded_reference(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            candidates = [node.left, *node.comparators]
            if any(_literal_is_hardcoded(child) for child in candidates):
                return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, bytes)
            and len(node.value) > MAX_LITERAL_COMPARE
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Patch-appendix and command-wrapper builders (v5 harness shape)
# --------------------------------------------------------------------------- #
# The v5 grader (``tests/test.sh``) applies ``/tests/test.patch`` with
# ``git apply`` in every graded phase and rewards exit 0 of
# ``sh -c "$(cat /tests/test_command.sh)"``. A repair therefore appends
# new-file diffs to ``tests/test.patch`` and wraps ``tests/test_command.sh``
# so the original command runs first, verbatim, with its exit status
# preserved, and the new tests run after it with the task interpreter.
# Both baseline and candidate phases see the same assertions because both
# apply the same patch and run the same command.


def read_task_workdir(package_dir: Path | str) -> str:
    """Absolute testbed workdir from the parent ``task.toml``."""
    path = Path(package_dir) / "task.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RepairError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RepairError(f"{path}: invalid TOML: {exc}") from exc
    workdir = (config.get("environment") or {}).get("workdir")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise RepairError(f"{path}: environment.workdir must be an absolute path")
    return workdir


def read_reset_list(test_sh: str) -> set[str]:
    """Repo paths ``tests/test.sh`` restores before applying the hidden patch."""
    match = re.search(r"<<'EOF_RESET_TEST_FILES'\n(.*?)EOF_RESET_TEST_FILES", test_sh, re.DOTALL)
    if match is None:
        return set()
    return {line.strip() for line in match.group(1).splitlines() if line.strip()}


def patched_paths(test_patch: str) -> set[str]:
    """Destination paths already carried by ``tests/test.patch``."""
    paths: set[str] = set()
    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[len("+++ b/") :])
    return paths


def build_patch_appendix(tests: Sequence[RepairTest]) -> str:
    """New-file ``git apply`` diffs for the proposed tests, in order."""
    chunks: list[str] = []
    for test in tests:
        if not test.code.endswith("\n"):
            raise RepairError(f"{test.repo_path}: code must end with a newline")
        lines = test.code.splitlines()
        if not lines:
            raise RepairError(f"{test.repo_path}: code must not be empty")
        body = "".join(f"+{line}\n" for line in lines)
        chunks.append(
            f"diff --git a/{test.repo_path} b/{test.repo_path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{test.repo_path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
            f"{body}"
        )
    return "".join(chunks)


def build_command_wrapper(
    original: str,
    new_command: str,
    *,
    workdir: str,
    interpreter: str = "/opt/test_venv/bin/python3",
) -> str:
    """Original command verbatim, then the new tests; original status kept.

    Runs under ``sh -c`` (POSIX only): the original line runs first and its
    status is saved; the new tests run next from the task workdir; a failing
    new-test run exits 1, otherwise the original status is returned exactly.
    Bare ``python`` / ``python3`` in the new command use the task venv when
    that interpreter exists. The check runs in the grader, not on the host.
    """
    base = original if original.endswith("\n") else original + "\n"
    quoted = shlex.quote(interpreter)
    bin_dir = shlex.quote(str(Path(interpreter).parent))
    return (
        f"(\n{base})\n"
        "REPAIR_RC=$?\n"
        f"cd {shlex.quote(workdir)} || exit 1\n"
        f"if [ -x {quoted} ]; then\n"
        f"  PATH={bin_dir}:$PATH\n"
        "  export PATH\n"
        f'  python() {{ {quoted} "$@"; }}\n'
        f'  python3() {{ {quoted} "$@"; }}\n'
        "fi\n"
        f"{new_command}\n"
        "REPAIR_NEW_RC=$?\n"
        'if [ "$REPAIR_NEW_RC" -ne 0 ]; then exit 1; fi\n'
        'exit "$REPAIR_RC"\n'
    )


def validate_proposal(
    proposal: RepairProposal, *, mutant_ids: Sequence[str], package_dir: Path | str
) -> None:
    """Reject fingerprint tests, hardcoded bytes, collisions, bound violations.

    Every ranked survivor needs a disposition: either addressed by the new
    tests (``mutant_ids``) or declared unrepairable with a per-ID reason
    (``unrepairable``); undeclared survivors fail the proposal.
    """
    if not proposal.repairable:
        if not proposal.rationale.strip():
            raise RepairError("not-repairable proposal needs an explicit rationale")
        return
    wanted = set(mutant_ids)
    tested = set(proposal.mutant_ids)
    declared_ids = [entry.mutant_id for entry in proposal.unrepairable]
    declared = set(declared_ids)
    if len(tested) != len(proposal.mutant_ids) or len(declared) != len(declared_ids):
        raise RepairError("proposal duplicates survivor dispositions")
    if tested & declared or (tested | declared) - wanted:
        raise RepairError("proposal survivor dispositions overlap or name unknown survivors")
    covered = tested | declared
    if wanted and not (covered & wanted):
        raise RepairError("proposal gives no disposition for the ranked survivors")
    if wanted - covered:
        raise RepairError(
            "proposal leaves survivors without a disposition: "
            + ", ".join(sorted(wanted - covered))
        )
    for entry in proposal.unrepairable:
        if not entry.reason.strip():
            raise RepairError(f"unrepairable {entry.mutant_id} needs an explicit reason")
    if not proposal.tests:
        raise RepairError("repairable proposal carries no tests")
    if len(proposal.tests) > MAX_TESTS:
        raise RepairError(f"proposal has {len(proposal.tests)} tests; max {MAX_TESTS}")
    parent = Path(package_dir)
    try:
        original_patch = (parent / "tests" / "test.patch").read_text(encoding="utf-8")
        original_command = (parent / "tests" / "test_command.sh").read_text(encoding="utf-8")
        test_sh = (parent / "tests" / "test.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise RepairError(f"parent lacks the v5 test harness: {exc}") from exc
    if not original_command.strip():
        raise RepairError("parent tests/test_command.sh is empty")
    reset = read_reset_list(test_sh)
    taken = patched_paths(original_patch)
    total = 0
    seen: set[str] = set()
    for test in proposal.tests:
        name = test.repo_path
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or name in seen
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
        ):
            raise RepairError(f"test path is unsafe or duplicated: {name!r}")
        seen.add(name)
        if not name.endswith(".py"):
            raise RepairError(f"test path must end with .py, got {name!r}")
        if not relative.name.startswith("test_") or ".git" in relative.parts:
            raise RepairError(f"repair must write a new test_*.py file: {name!r}")
        if name in reset:
            raise RepairError(f"{name!r} is reset by tests/test.sh; pick a new file")
        if name in taken:
            raise RepairError(f"{name!r} is already carried by tests/test.patch")
        if not test.code.endswith("\n"):
            raise RepairError(f"{name}: code must end with a newline")
        raw = test.code.encode("utf-8")
        total += len(raw)
        if len(raw) > MAX_TEST_BYTES:
            raise RepairError(f"{name} exceeds {MAX_TEST_BYTES} bytes")
        lowered = test.code.lower()
        for marker in _DISALLOWED_TEST_FEATURES:
            prefix = r"(?<!\w)" if marker[0].isalnum() or marker[0] == "_" else ""
            suffix = r"(?!\w)" if marker[-1].isalnum() or marker[-1] == "_" else ""
            if re.search(prefix + re.escape(marker) + suffix, lowered):
                raise RepairError(f"{name} uses a disallowed test feature ({marker})")
        try:
            tree = ast.parse(test.code, filename=name)
        except SyntaxError as exc:
            raise RepairError(f"{name} does not parse: {exc}") from exc
        if _has_hardcoded_reference(tree):
            raise RepairError(f"{name} hardcodes reference bytes")
    if total > MAX_TOTAL_TEST_BYTES:
        raise RepairError(f"proposal totals {total} bytes; max {MAX_TOTAL_TEST_BYTES}")
    if proposal.command is None:
        raise RepairError("repairable proposal needs a test command")
    command = proposal.command.strip()
    if not command or len(command.encode()) > MAX_COMMAND_BYTES:
        raise RepairError("proposal command is missing or too long")
    tokens = shlex.split(command)
    if (
        len(tokens) < 4
        or not re.fullmatch(r"(?:/[A-Za-z0-9_./-]+/)?python[0-9.]*", tokens[0])
        or tokens[1] != "-m"
        or tokens[2] not in {"pytest", "unittest"}
    ):
        raise RepairError(
            "proposal command must invoke the task Python interpreter with -m pytest or -m unittest"
        )
    if _COMMAND_FORBIDDEN.search(command):
        raise RepairError("proposal command carries shell metacharacters")
    missing = [name for name in seen if name not in tokens[3:]]
    if missing:
        raise RepairError(f"proposal command never runs: {', '.join(sorted(missing))}")


def parse_proposal(text: str) -> RepairProposal:
    """Parse exactly one JSON proposal object; fail closed on any deviation."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RepairError(f"model proposal is not JSON: {exc}") from exc
    try:
        return RepairProposal.model_validate(document)
    except ValueError as exc:
        raise RepairError(f"model proposal violates the schema: {exc}") from exc


# --------------------------------------------------------------------------- #
# Variant derivation (test artifacts only, via existing derive_task)
# --------------------------------------------------------------------------- #


def derive_repair_variant(
    parent_dir: Path | str,
    proposal: RepairProposal,
    *,
    repo_root: Path | str | None = None,
    created_by: str = "verifier-repair",
    rationale: str = "Add semantic assertions covering EnvCheck survivors.",
    derive_fn: DeriveFn | None = None,
) -> Any:
    """Derive a test-only variant: patch appendix plus command wrapper.

    Only ``tests/test.patch`` (appended new-file diffs; existing bytes are a
    strict prefix of the new content) and ``tests/test_command.sh`` (original
    command verbatim first, then the new tests) change. Solution, existing
    tests, and harness semantics stay untouched.
    """
    if not proposal.repairable:
        raise RepairError("cannot derive a variant from a not-repairable proposal")
    if proposal.command is None:
        raise RepairError("repairable proposal needs a test command")
    parent = Path(parent_dir)
    try:
        original_patch = (parent / "tests" / "test.patch").read_text(encoding="utf-8")
        original_command = (parent / "tests" / "test_command.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise RepairError(f"parent lacks the v5 test harness: {exc}") from exc
    workdir = read_task_workdir(parent)
    appendix = build_patch_appendix(proposal.tests)
    if not appendix:
        raise RepairError("repair adds no files; refusing no-op derivation")
    new_patch = original_patch + ("" if original_patch.endswith("\n") else "\n") + appendix
    if not new_patch.startswith(original_patch):
        raise RepairError("repair must only append to tests/test.patch")
    wrapper = build_command_wrapper(original_command, proposal.command.strip(), workdir=workdir)
    changes: dict[str, bytes | None] = {
        "tests/test.patch": new_patch.encode("utf-8"),
        "tests/test_command.sh": wrapper.encode("utf-8"),
    }
    derive = derive_fn if derive_fn is not None else _default_derive
    from evallab.registry import compute_task_digests

    digest = compute_task_digests(parent).package
    config = tomllib.loads((parent / "task.toml").read_text())
    task_name = (config.get("task") or {}).get("name", parent.name)
    record_path = (
        Path("library/task-variants")
        / task_name.replace("/", "__")
        / f"{digest.split(':')[-1][:12]}.json"
    )
    source = (
        {"kind": "variant", "record": record_path.as_posix()}
        if repo_root is not None and (Path(repo_root) / record_path).is_file()
        else {"kind": "local", "path": str(parent.resolve())}
    )
    return derive(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs={
            "mutant_ids": list(proposal.mutant_ids),
            "unrepairable": [entry.model_dump(mode="json") for entry in proposal.unrepairable],
            "command": proposal.command,
            "repo_paths": [test.repo_path for test in proposal.tests],
        },
        parent_source=source,
        repo_root=repo_root,
    )


def _default_derive(parent: Path | str, **kwargs: Any) -> Any:
    from evallab.task_variants import derive_task

    return derive_task(parent, **kwargs)


# --------------------------------------------------------------------------- #
# Validation through runner.run_matrix with the existing mutant scripts
# --------------------------------------------------------------------------- #


def _script_text(run_dir: Path, script_ref: str) -> str:
    """Read the archived staged script, independent of the former worktree root."""
    path = run_dir / "scripts" / Path(script_ref).name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RepairError(f"missing control script {script_ref}: {exc}") from exc


def plan_validation_matrix(
    *,
    variant_dir: Path,
    variant_digest: str,
    verifier_digest: str,
    report: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    run_dir: Path,
    output_dir: Path,
    repo_root: Path,
    timeout_seconds: int = 1_800,
) -> ExperimentMatrix:
    """Reference + nop + each survivor as oracle trials on the repaired package."""
    from evallab.verifier_mutation import _slug  # reuse boring slug helper

    task_name = str(report.get("task") or Path(variant_dir).name)
    slug = _slug(task_name.rsplit("/", 1)[-1], limit=28)
    runs: list[MatrixRun] = []
    nonce = new_ulid().lower()[-8:]
    scripts = output_dir / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    by_role = {str(r.get("role")): r for r in report.get("runs", []) if isinstance(r, dict)}
    oracle = by_role.get("capture-oracle")
    base = by_role.get("capture-base")
    if oracle is None or base is None:
        raise RepairError("report lacks capture-oracle/capture-base controls")
    specs: list[tuple[str, float, Mapping[str, Any]]] = [("reference", 1.0, oracle)]
    specs.append(("nop", 0.0, base))
    wanted = {str(e["mutant"]["id"]): e for e in entries}
    for run in report.get("runs", []):
        if not isinstance(run, dict) or run.get("role") != "mutant":
            continue
        if str(run.get("mutant")) in wanted:
            specs.append((f"survivor-{run.get('mutant')}", 0.0, run))
    missing = wanted.keys() - {s[0].removeprefix("survivor-") for s in specs[2:]}
    if missing:
        raise RepairError(f"survivor scripts missing from report: {sorted(missing)}")
    for label, expect, run in specs:
        script = str(run.get("script") or "")
        if not script:
            raise RepairError(f"{label} control has no staged script")
        text = _script_text(run_dir, script)
        digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        if digest != run.get("script_sha256"):
            raise RepairError(f"{label}: staged control script digest mismatch")
        staged = scripts / f"{label}.sh"
        staged.write_text(text, encoding="utf-8")
        name = f"vr-{nonce}-{label}"
        runs.append(
            MatrixRun(
                name=name,
                agent="oracle",
                expect_reward=expect,
                solution=staged.relative_to(repo_root).as_posix(),
            )
        )
    return ExperimentMatrix(
        matrix_id=new_ulid(),
        name=f"repair-{slug}",
        hypothesis=(
            f"The repaired verifier of {slug} still grades reference 1 and nop 0, "
            "and now rejects every previous survivor."
        ),
        benchmark_family=slug,
        task_id=slug if len(slug) >= 3 else f"{slug}-task",
        task=str(variant_dir),
        task_package_digest=variant_digest,
        verifier_digest=verifier_digest,
        environment="docker",
        concurrency=1,
        timeout_seconds=timeout_seconds,
        runs=runs,
    )


def classify_validation(
    matrix: ExperimentMatrix,
    results: Sequence[Mapping[str, Any]],
    *,
    declared_unrepairable: Mapping[str, str] | None = None,
) -> tuple[list[RepairCheck], RepairVerdict, str]:
    """Gate validation: ``repaired`` needs complete 1/0/all-0.

    Infra, missing results and partial rewards fail closed as ``blocked``. A
    broken reference or nop is ``rejected``. Escaping survivors are
    ``rejected`` unless every one of them was declared unrepairable
    (equivalent or out-of-scope) with a per-ID reason, in which case the
    retained genuine assertions report ``partial`` — never ``repaired``.
    """
    declared = dict(declared_unrepairable or {})
    by_name = {str(r.get("name")): r for r in results}
    rejected_controls = any(
        row.get("status") in {"ok", "mismatch"}
        and row.get("rewards") in ([0.0], [1.0])
        and row.get("rewards") != [run.expect_reward]
        for run in matrix.runs[:2]
        if (row := by_name.get(run.name)) is not None
    )
    checks: list[RepairCheck] = []
    failures: list[str] = []
    blocked: list[str] = []
    escaped_ids: list[str] = []
    for run in matrix.runs:
        result = by_name.get(run.name)
        if result is None:
            skipped = rejected_controls and "-survivor-" in run.name
            if not skipped:
                blocked.append(f"{run.name}: no matrix result")
            checks.append(
                RepairCheck(
                    name=run.name,
                    role="survivor" if skipped else "missing",
                    script=run.solution,
                    status="skipped" if skipped else "infra",
                    error="not run after failed controls" if skipped else "no matrix result",
                )
            )
            continue
        status = str(result.get("status") or "infra")
        rewards = result.get("rewards") or []
        error = result.get("error")
        try:
            reward = float(rewards[0]) if len(rewards) == 1 else None
        except (ValueError, TypeError):
            reward = None
        checks.append(
            RepairCheck(
                name=run.name,
                role="reference" if run.expect_reward == 1.0 else "candidate",
                script=run.solution,
                status=status,
                reward=reward,
                error=str(error) if error else None,
            )
        )
        if (
            status not in {"ok", "mismatch"}
            or reward is None
            or not math.isfinite(reward)
            or reward not in {0.0, 1.0}
        ):
            blocked.append(f"{run.name}: infrastructure or invalid reward ({error or reward})")
            continue
        expected = float(run.expect_reward or 0.0)
        if reward != expected:
            failures.append(f"{run.name}: expected {expected:g}, got {reward:g}")
            if "-survivor-" in run.name:
                escaped_ids.append(run.name.split("-survivor-", 1)[1])
    reference_failed = [f for f in failures if "-reference" in f or "-nop" in f]
    survivor_failures = [f for f in failures if f not in reference_failed]
    if reference_failed:
        return checks, "rejected", "repair broke the suite: " + "; ".join(reference_failed)
    if blocked:
        return checks, "blocked", "infrastructure or missing evidence: " + "; ".join(blocked)
    if not survivor_failures:
        return checks, "repaired", "reference 1, nop 0, every previous survivor 0"
    undeclared = [mid for mid in escaped_ids if mid not in declared]
    if undeclared:
        return (
            checks,
            "rejected",
            "repaired verifier still escapes: "
            + "; ".join(survivor_failures)
            + "; undeclared: "
            + ", ".join(sorted(undeclared)),
        )
    return (
        checks,
        "partial",
        "genuine assertions retained and validated, but declared "
        "equivalent/out-of-scope survivors still escape: "
        + ", ".join(f"{mid} ({declared[mid]})" for mid in sorted(set(escaped_ids))),
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class RepairPlan:
    """Preview artefacts: context and prompt, no model call, no spend."""

    report_path: Path
    package_dir: Path
    run_dir: Path
    report: dict[str, Any]
    ranked_ids: list[str]
    context: dict[str, Any]
    prompt: str


def _report_run_dir(report: Mapping[str, Any], report_path: Path) -> Path:
    raw = str(report.get("run_dir") or "")
    candidate = Path(raw) if Path(raw).is_absolute() else report_path.parent / raw
    if not candidate.is_dir():
        candidate = report_path.parent
    return candidate


def plan_repair(
    report_path: Path | str,
    *,
    package_dir: Path | str | None = None,
    feedback: Path | None = None,
    repo_root: Path | None = None,
) -> RepairPlan:
    """Prepare the repair context and prompt without calling any model."""
    path = Path(report_path)
    report = load_mutation_report(path)
    package = Path(package_dir) if package_dir is not None else Path(str(report.get("package", "")))
    if not package.is_dir():
        raise RepairError(f"task package is missing: {package}")
    run_dir = _report_run_dir(report, path)
    ranked, reason = select_repair_survivors(report)
    if reason:
        raise RepairError(reason)
    context = build_repair_context(report, ranked, package_dir=package, mutation_run_dir=run_dir)
    if repo_root is not None:
        oracle = next(run for run in report["runs"] if run["role"] == "capture-oracle")
        job = (repo_root / str(oracle.get("job_dir") or "")).resolve()
        if oracle.get("job_dir") and job.is_relative_to(repo_root.resolve()):
            for command_file in sorted(job.glob("*/verifier/resolved_command.txt")):
                command, truncated = _read_bounded(command_file, 16 * 1024)
                context["resolved_test_command"] = command
                context["resolved_command_truncated"] = truncated
                break
    plan = RepairPlan(
        report_path=path,
        package_dir=package,
        run_dir=run_dir,
        report=report,
        ranked_ids=[str(e["mutant"]["id"]) for e in ranked],
        context=context,
        prompt=render_repair_prompt(context),
    )
    if feedback is not None:
        if repo_root is None:
            raise RepairError("feedback needs the repository root for retained verifier logs")
        previous = RepairResult.model_validate_json(feedback.read_text())
        if previous.task != report["task"]:
            raise RepairError("feedback belongs to another task")
        logs: dict[str, str] = {}
        remaining = 64 * 1024
        for check in previous.checks:
            if not re.fullmatch(r"vr-[A-Za-z0-9_.-]+", check.name):
                continue
            expected = 1.0 if check.role == "reference" else 0.0
            if check.reward == expected:
                continue
            for log_path in (repo_root / "runs" / check.name).glob("*/verifier/test_output.log"):
                text = log_path.read_text(errors="replace")[-min(8192, remaining) :]
                logs[check.name] = text
                remaining -= len(text)
                if remaining <= 0:
                    break
            if remaining <= 0:
                break
        context["validation_feedback"] = {
            "source": str(feedback),
            "previous": previous.model_dump(mode="json", by_alias=True),
            "verifier_output": logs,
        }
        plan.prompt += (
            "\n\n=== Measured feedback on the previous proposal ===\n"
            + json.dumps(context["validation_feedback"], indent=2)
            + "\nRevise the assertion proposal using this actual feedback. A reference failure "
            "may be an invalid test setup or a separate reference defect; do not hide a real "
            "defect by pinning accidental output. Cover genuine survivors with valid inputs, "
            "and explicitly disposition those that cannot be killed within the task contract. "
            "Return a complete replacement proposal against the original parent package."
        )
    return plan


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_runner(matrix: ExperimentMatrix, repo_root: Path) -> list[dict[str, Any]]:
    from evallab.runner import run_matrix

    return run_matrix(matrix, root=repo_root).results


def validate_matrix(
    matrix: ExperimentMatrix, repo: Path, runner: MatrixRunner
) -> list[dict[str, Any]]:
    """Establish reference/nop first; do not run mutants against broken assertions."""
    controls = matrix.model_copy(
        update={
            "matrix_id": new_ulid(),
            "name": matrix.name + "-controls",
            "runs": matrix.runs[:2],
        }
    )
    results = runner(controls, repo)
    by_name = {row["name"]: row for row in results}
    if not all(
        (row := by_name.get(run.name)) is not None
        and row.get("status") in {"ok", "mismatch"}
        and row.get("rewards") == [run.expect_reward]
        for run in controls.runs
    ):
        return results
    survivors = matrix.model_copy(
        update={
            "matrix_id": new_ulid(),
            "name": matrix.name + "-survivors",
            "runs": matrix.runs[2:],
        }
    )
    return results + runner(survivors, repo)


def _materialize_for_validation(
    record: Any,
    repo: Path,
    parent: Path,
    *,
    materialize_fn: Callable[..., Path] | None,
    digests_fn: Callable[[Path], Any] | None,
) -> tuple[Path, Any]:
    """Resolve the variant package dir plus digests (seams injectable for tests)."""
    if materialize_fn is None:
        from evallab.task_variants import materialize

        materialize_fn = materialize
    if digests_fn is None:
        from evallab.registry import compute_task_digests

        digests_fn = compute_task_digests
    variant_dir = materialize_fn(record, parent, repo_root=repo)
    return variant_dir, digests_fn(variant_dir)


def execute_repair(
    plan: RepairPlan,
    *,
    model: str,
    repo_root: Path,
    output_dir: Path,
    quota_checker: QuotaChecker,
    model_caller: ModelCaller,
    runner: MatrixRunner | None = None,
    derive_fn: DeriveFn | None = None,
    materialize_fn: Callable[..., Path] | None = None,
    digests_fn: Callable[[Path], Any] | None = None,
    timeout_seconds: int = 1_800,
) -> tuple[RepairResult, Path]:
    """Run one repair: quota-gated model call, derive, validate, receipts."""
    if model not in ALLOWED_MODELS:
        raise RepairError(f"model {model!r} is not an approved repair model; no fallback")
    repo = Path(repo_root).resolve()
    out = Path(output_dir).expanduser().resolve()
    if not out.is_relative_to(repo):
        raise RepairError(f"run directory must stay inside the repository ({repo}); got {out}")
    out.mkdir(parents=True, exist_ok=True)
    if (out / "model-request.json").exists():
        raise RepairError(
            "this attempt already requested a proposal; no automatic retry or overwrite"
        )
    from evallab.registry import compute_task_digests

    if compute_task_digests(plan.package_dir).package != plan.report.get("package_digest"):
        raise RepairError("task package differs from the audited digest")
    quota = ensure_quota(quota_checker)  # before every model call; exactly one below
    _write_json(
        out / "model-request.json",
        {
            "model": model,
            "prompt_sha256": hashlib.sha256(plan.prompt.encode()).hexdigest(),
            "quota": persisted_quota_record(quota, model),
        },
    )
    (out / "repair-prompt.txt").write_text(plan.prompt)
    _write_json(out / "repair-context.json", plan.context)
    try:
        raw_text, usage = model_caller(plan.prompt, plan.context)
    except (RepairError, OSError, RuntimeError) as exc:
        result = RepairResult(
            task=str(plan.report.get("task", "")), verdict="blocked", reason=str(exc)
        )
        _write_result(out, result)
        return result, out
    _write_json(
        out / "model_response.json",
        {
            "quota": persisted_quota_record(quota, model),
            "usage": usage,
            "response": raw_text,
        },
    )
    try:
        proposal = parse_proposal(raw_text)
    except RepairError as exc:
        result = RepairResult(
            task=str(plan.report.get("task", "")), verdict="blocked", reason=str(exc)
        )
        _write_result(out, result)
        return result, out
    proposal.model = model
    try:
        validate_proposal(proposal, mutant_ids=plan.ranked_ids, package_dir=plan.package_dir)
    except RepairError as exc:
        result = RepairResult(
            task=str(plan.report.get("task", "")),
            verdict="blocked",
            reason=str(exc),
            proposal=proposal,
        )
        _write_result(out, result)
        return result, out
    if not proposal.repairable:
        result = RepairResult(
            task=str(plan.report.get("task", "")),
            verdict="unrepairable",
            reason=proposal.rationale,
            proposal=proposal,
        )
        _write_result(out, result)
        return result, out
    try:
        record = derive_repair_variant(
            plan.package_dir, proposal, repo_root=repo, derive_fn=derive_fn
        )
    except (RepairError, ValueError) as exc:
        result = RepairResult(
            task=str(plan.report.get("task", "")),
            verdict="blocked",
            reason=f"variant derivation failed: {exc}",
            proposal=proposal,
        )
        _write_result(out, result)
        return result, out
    variant_digest = str(getattr(record, "variant_digest", ""))
    variant_record = str(record.record_relpath())
    by_id = {str(m.get("id")): m for m in plan.report.get("mutants", []) if isinstance(m, dict)}
    entries: list[dict[str, Any]] = []
    for run in plan.report.get("runs", []):
        if not isinstance(run, dict):
            continue
        mutant = by_id.get(str(run.get("mutant")))
        if mutant is not None and str(run.get("mutant")) in set(plan.ranked_ids):
            entries.append({"mutant": mutant, "run": run})
    try:
        variant_dir, digests = _materialize_for_validation(
            record, repo, plan.package_dir, materialize_fn=materialize_fn, digests_fn=digests_fn
        )
        matrix = plan_validation_matrix(
            variant_dir=variant_dir,
            variant_digest=digests.package,
            verifier_digest=digests.verifier,
            report=plan.report,
            entries=entries,
            run_dir=plan.run_dir,
            output_dir=out,
            repo_root=repo,
            timeout_seconds=timeout_seconds,
        )
        _write_json(out / "validation-matrix.json", matrix.model_dump(mode="json"))
        run = runner or _default_runner
        results = validate_matrix(matrix, repo, run)
    except (RepairError, ValueError, OSError) as exc:
        result = RepairResult(
            task=str(plan.report.get("task", "")),
            verdict="blocked",
            reason=f"validation could not run: {exc}",
            proposal=proposal,
            variant_digest=variant_digest or None,
            variant_record=variant_record or None,
        )
        _write_result(out, result)
        return result, out
    declared = {entry.mutant_id: entry.kind for entry in proposal.unrepairable}
    checks, verdict, reason = classify_validation(matrix, results, declared_unrepairable=declared)
    result = RepairResult(
        task=str(plan.report.get("task", "")),
        verdict=verdict,
        reason=reason,
        proposal=proposal,
        variant_digest=variant_digest or None,
        variant_record=variant_record or None,
        checks=checks,
    )
    _write_result(out, result)
    from evallab.task_variants import append_status_evidence

    append_status_evidence(
        record,
        "validated"
        if verdict == "repaired"
        else "rejected"
        if verdict == "rejected"
        else "candidate",
        evidence=f"{verdict}: {reason}; receipt={out / 'repair-result.json'}",
        by="verifier-repair",
        repo_root=repo,
    )
    return result, out


def _write_result(out: Path, result: RepairResult) -> None:
    (out / "repair-result.json").write_text(
        json.dumps(result.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# CLI (parent registers this parser under ``evallab hack``)
# --------------------------------------------------------------------------- #


def _repair_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    output_dir = args.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = root / output_dir
    try:
        plan = plan_repair(
            args.report, package_dir=args.package, feedback=args.feedback, repo_root=root
        )
    except RepairError as exc:
        print(f"hack repair: {exc}", file=sys.stderr)
        return 1
    target_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else root / "runs" / ".envcheck" / f"repair-{new_ulid().lower()[-8:]}"
    )
    if (target_dir / "model-request.json").exists():
        print(
            "hack repair: existing attempted proposal is immutable; use a new output directory",
            file=sys.stderr,
        )
        return 1
    progress = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
    if not args.execute:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "repair-prompt.txt").write_text(plan.prompt, encoding="utf-8")
        _write_json(target_dir / "repair-context.json", plan.context)
        if args.json:
            print(json.dumps({"preview": True, "ranked": plan.ranked_ids}, sort_keys=True))
        else:
            print(f"Task: {plan.report.get('task')}")
            print(f"Ranked survivors: {', '.join(plan.ranked_ids)}")
            print("Preview only: no model call. Rerun with --execute to propose.")
        print(f"run directory: {target_dir}", file=sys.stderr if args.json else sys.stdout)
        return 0
    try:
        with progress:
            result, _ = execute_repair(
                plan,
                model=args.model,
                repo_root=root,
                output_dir=target_dir,
                quota_checker=read_codex_quota,
                model_caller=lambda prompt, context: codex_model_caller(
                    prompt,
                    context,
                    model=args.model,
                    output_dir=target_dir / "codex",
                ),
                timeout_seconds=args.timeout_seconds,
            )
    except (RepairError, OSError) as exc:
        print(f"hack repair: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(result.model_dump(mode="json", by_alias=True), indent=2)
        if args.json
        else f"{result.verdict}: {result.reason}"
    )
    return 0 if result.verdict == "repaired" else 1


def add_repair_parser(hack_commands: Any) -> None:
    """Register ``evallab hack repair``; the parent owns CLI integration."""
    repair = hack_commands.add_parser(
        "repair",
        help=(
            "Propose missing test assertions for mutation survivors and validate "
            "them against prior controls (preview by default, no paid API)"
        ),
    )
    repair.add_argument("report", type=Path, help="Mutation report JSON to repair")
    repair.add_argument(
        "--package", type=Path, default=None, help="Task package (default: report package)"
    )
    repair.add_argument(
        "--feedback", type=Path, help="Prior repair-result.json and retained verifier errors"
    )
    repair.add_argument("--model", default="gpt-6.1-sol", help="Repair proposer model")
    repair.add_argument("--execute", action="store_true", help="Call the model and validate")
    repair.add_argument("--timeout-seconds", type=int, default=1_800)
    repair.add_argument("--output-dir", type=Path)
    repair.add_argument("--json", action="store_true")
    repair.set_defaults(func=_repair_command)


__all__ = [
    "ALLOWED_MODELS",
    "MAX_WEEKLY_USED",
    "QuotaBlocked",
    "QuotaChecker",
    "ModelCaller",
    "MatrixRunner",
    "DeriveFn",
    "QuotaStatus",
    "RepairCheck",
    "RepairPlan",
    "RepairProposal",
    "RepairResult",
    "RepairTest",
    "RepairUnrepairable",
    "SCHEMA_PROPOSAL",
    "SCHEMA_RESULT",
    "TRANSFORM_ID",
    "add_repair_parser",
    "build_command_wrapper",
    "build_patch_appendix",
    "build_repair_context",
    "classify_validation",
    "codex_model_caller",
    "derive_repair_variant",
    "ensure_quota",
    "execute_repair",
    "load_mutation_report",
    "parse_proposal",
    "persisted_quota_record",
    "patched_paths",
    "plan_repair",
    "plan_validation_matrix",
    "select_repair_survivors",
    "read_reset_list",
    "read_task_workdir",
    "render_repair_prompt",
    "validate_proposal",
]
