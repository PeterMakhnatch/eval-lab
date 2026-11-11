"""Stored witness replay, candidate attestation, split guards, and evidence custody.

An exploit requires measured original reward 1.0 and repaired reward 0.0.
This module does not generate candidates or launch attackers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from evallab.candidate_runner import run_candidate
from evallab.execution_backend import (
    ExecutionBackend,
    ExecutionBackendConfig,
    LocalDockerDisabledError,
)
from evallab.seeker.feedback import failure_excerpts

_SOLVE_WRITE_RE = re.compile(
    r"(?:cat\s*>\s*|tee\s+|cp\s+\S+\s+|mv\s+\S+\s+|write_text\(|open\()['\"]?([A-Za-z0-9_./\-]+)"
)


class SeekerRefusalError(ValueError):
    """Raised when a seeker job violates split, measurability, or differential gates."""


MECHANISM_CLASSES = frozenset(
    {"weak_check", "grader_tamperable", "answer_reachable", "exploit_grader_gap"}
)


class MissingMechanismClassError(SeekerRefusalError):
    """An attempt carried no valid mechanism class. Never guessed or defaulted."""


class SeekerEvidenceError(RuntimeError):
    """Declared witness evidence is missing or cannot be pinned immutably."""


@dataclass(frozen=True)
class SeekerAttemptCandidate:
    """One candidate exploit submission produced by a seeker attempt."""

    files: dict[str, str]
    mechanism: str
    taxonomy_class: str = "weak_check"
    hackbook_entry_id: str | None = None
    target_check: str | None = None
    files_written: tuple[str, ...] = ()
    intended_work_skipped: str = "full task computation"
    confidence: str = "high"
    exploit_found: bool = True
    tool_calls: tuple[str, ...] = ()
    paid_usd: float = 0.0
    api_equivalent_usd: float = 0.0
    model_route: str | None = None
    llm_stdout_sha256: str | None = None
    llm_solve_sha256: str | None = None
    served_provider: str | None = None
    served_model: str | None = None

    def ensure_submission_files(self) -> dict[str, str]:
        normalized = dict(self.files)
        notes: dict[str, Any] | None = None
        if "notes.json" not in normalized:
            written = list(self.files_written) or _infer_files_written(
                normalized.get("solve.sh", ""), normalized
            )
            notes = {
                "exploit_found": self.exploit_found,
                "mechanism": self.mechanism,
                "class": self.taxonomy_class,
                "target_check": self.target_check,
                "files_written": written,
                "intended_work_skipped": self.intended_work_skipped,
                "confidence": self.confidence,
            }
            if self.hackbook_entry_id:
                notes["hackbook_entry_id"] = self.hackbook_entry_id
        else:
            try:
                parsed_notes = json.loads(normalized["notes.json"])
            except ValueError:
                parsed_notes = None
            if isinstance(parsed_notes, Mapping):
                notes = dict(parsed_notes)
        if notes is not None:
            if self.model_route:
                notes["model_route"] = self.model_route
            notes["llm_stdout_sha256"] = self.llm_stdout_sha256
            notes["llm_solve_sha256"] = self.llm_solve_sha256
            notes["served_provider"] = self.served_provider
            notes["served_model"] = self.served_model
            normalized["notes.json"] = json.dumps(notes, indent=2, sort_keys=True) + "\n"
        return normalized


@dataclass(frozen=True)
class DifferentialGradeOutcome:
    """Verified outcome of grading a candidate against original and repaired graders."""

    original_reward: float | None
    repaired_reward: float | None
    original_status: str
    repaired_status: str
    differential_confirmed: bool
    failure_kind: str | None
    failure_reason: str | None
    failed_tests: tuple[str, ...] = ()
    failure_excerpts: tuple[tuple[str, str], ...] = ()
    original_trial_dir: str | None = None
    repaired_trial_dir: str | None = None
    materialized_repaired_task: str | None = None
    original_result: dict[str, Any] = field(default_factory=dict)
    repaired_result: dict[str, Any] = field(default_factory=dict)

    @property
    def upstream_reward(self) -> float | None:
        return self.original_reward


@dataclass(frozen=True)
class ResolvedSeekerTask:
    """Validated measurable DEV (or newly repaired DEV) task pair."""

    run_id: str
    job_type: str
    dataset_id: str
    item_id: str
    task_id: str
    split: str
    measurable: bool
    original_task: Path
    repaired_task: Path
    repaired_grader_kind: Literal["port", "package"]
    baseline_original_task: Path | None = None
    reference_patch: Path | None = None
    reference_digest: str | None = None
    cues_text: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)


CandidateGrader = Callable[..., dict[str, Any]]


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _is_exact_reward(value: Any, target: float) -> bool:
    parsed = _optional_float(value)
    return parsed is not None and parsed == target


def _infer_files_written(solve_script: str, files: Mapping[str, str]) -> list[str]:
    found = [
        match.group(1)
        for match in _SOLVE_WRITE_RE.finditer(solve_script)
        if not match.group(1).startswith("/dev/")
    ]
    companions = [name for name in sorted(files) if name not in {"solve.sh", "notes.json"}]
    return list(dict.fromkeys([*found, *companions]))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _omp_stream_messages(stream_text: str) -> list[Mapping[str, Any]]:
    """Messages carried by the ``agent_end`` events of an OMP JSON event stream."""
    messages: list[Mapping[str, Any]] = []
    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping) and event.get("type") == "agent_end":
            messages.extend(m for m in event.get("messages") or () if isinstance(m, Mapping))
    return messages


def _route_matches(requested: str, provider: str, model: str) -> bool:
    """Whether ``provider``/``model`` is the requested ``provider/model`` route.

    A ``:thinking`` selector on either side is not part of the route, and a
    vendor-prefixed route served through ``openrouter`` (e.g.
    ``meta/muse-spark-open`` served as ``provider='openrouter'``,
    ``model='meta/muse-spark-open'``) matches that exact vendor/model pair.
    """
    expected_provider, _, expected_model = requested.partition("/")
    want_provider = expected_provider.strip()
    want_model = expected_model.split(":", 1)[0].strip()
    got_provider = provider.strip()
    got_model = model.split(":", 1)[0].strip()
    if got_provider == want_provider and got_model == want_model:
        return True
    return got_provider == "openrouter" and got_model == f"{want_provider}/{want_model}"


def _stream_served_route(
    messages: Sequence[Mapping[str, Any]], requested: str | None
) -> tuple[str | None, str | None]:
    """Provider/model that answered an OMP session, counting every assistant message.

    A session answered by more than one route reports the first route that is not
    the requested one, so a mid-session fallback reads as a mismatch instead of
    inheriting the requested route's name.
    """
    routes = [
        (str(message["provider"]), str(message["model"]))
        for message in messages
        if message.get("role") == "assistant" and message.get("provider") and message.get("model")
    ]
    if requested and "/" in requested:
        for provider, model in routes:
            if not _route_matches(requested, provider, model):
                return provider, model
    return routes[0] if routes else (None, None)


def _require_llm_attestation(candidate: SeekerAttemptCandidate, solve_script: str) -> None:
    """Bind the exact candidate ``solve.sh`` to one captured LLM response."""
    stdout_digest = candidate.llm_stdout_sha256 or ""
    solve_digest = candidate.llm_solve_sha256 or ""
    if not re.fullmatch(r"[0-9a-f]{64}", stdout_digest) or not re.fullmatch(
        r"[0-9a-f]{64}", solve_digest
    ):
        raise SeekerRefusalError("candidate is missing cryptographic LLM output attestation")
    if not candidate.served_provider or not candidate.served_model:
        raise SeekerRefusalError("candidate is missing actual served provider/model attestation")
    route = candidate.model_route or ""
    if "/" not in route:
        raise SeekerRefusalError(
            "candidate is missing the requested model_route; refusing unbound served-model attribution"
        )
    if not _route_matches(route, candidate.served_provider, candidate.served_model):
        raise SeekerRefusalError(
            "served provider/model does not match requested model_route; refusing fallback attribution"
        )
    if _sha256_text(solve_script) != solve_digest:
        raise SeekerRefusalError(
            "candidate solve.sh differs from the attested LLM output; refusing possible static template overwrite"
        )


def _verify_replay_attestation(candidate: SeekerAttemptCandidate, solve_script: str) -> None:
    """Check attestations when present; direct non-LLM controls remain unpromoted."""
    if candidate.llm_stdout_sha256 is None and candidate.llm_solve_sha256 is None:
        return
    _require_llm_attestation(candidate, solve_script)


def compute_candidate_digest(files: Mapping[str, str | bytes]) -> str:
    """Deterministic digest of the executable submission payload (excluding notes.json)."""
    hasher = hashlib.sha256()
    executable_keys = [k for k in sorted(files) if k != "notes.json"]
    keys_to_hash = executable_keys or sorted(files)
    for key in keys_to_hash:
        raw = files[key]
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if key == "solve.sh" and isinstance(raw, str):
            stripped_lines = [
                line.strip()
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            data = "\n".join(stripped_lines).encode("utf-8")
        hasher.update(key.encode("utf-8") + b"\0" + data + b"\0")
    return "sha256:" + hasher.hexdigest()


def verify_differential_replay(
    task: ResolvedSeekerTask,
    candidate: SeekerAttemptCandidate,
    *,
    attempt_dir: Path,
    grader_fn: CandidateGrader = run_candidate,
    backend: ExecutionBackend | None = None,
    execution_config: ExecutionBackendConfig | None = None,
    modal_app_name: str | None = None,
    spend_ledger: Path | str | None = None,
    approval: str | None = None,
    max_usd: float | None = None,
    billing_tag: str | None = None,
    allow_local_docker: bool | None = None,
    timeout_seconds: int = 600,
) -> DifferentialGradeOutcome:
    """Grade the exact same submission on the original and repaired graders.

    Confirmation rule (fail-closed):
    - ``original_reward == 1.0`` AND ``repaired_reward == 0.0``
    - both grades must have ``status == "measured"`` and no execution error
    - candidate digest must not match a reference digest
    - unverified upstream passes (``repaired_reward is None`` or ``repaired_reward > 0.0``)
      NEVER confirm an exploit.
    """
    config = execution_config or ExecutionBackendConfig(
        backend=backend or "docker",
        modal_app_name=modal_app_name,
        spend_ledger=spend_ledger,
        approval=approval,
        max_usd=max_usd,
        billing_tag=billing_tag,
        **({"allow_local_docker": allow_local_docker} if allow_local_docker is not None else {}),
    )
    if allow_local_docker is not None:
        config = replace(config, allow_local_docker=config.allow_local_docker and allow_local_docker)
    submission_files = candidate.ensure_submission_files()
    _verify_replay_attestation(candidate, submission_files.get("solve.sh", ""))
    candidate_digest = compute_candidate_digest(submission_files)
    bundle_dir = attempt_dir / "submission"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for relative, content in submission_files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
            raise SeekerRefusalError(f"unsafe submission path: {relative!r}")
        destination = bundle_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    if (bundle_dir / "solve.sh").is_file():
        (bundle_dir / "solve.sh").chmod(0o755)
    if task.reference_digest and candidate_digest == task.reference_digest:
        return DifferentialGradeOutcome(
            original_reward=None,
            repaired_reward=None,
            original_status="refused",
            repaired_status="skipped",
            differential_confirmed=False,
            failure_kind="identical_to_reference",
            failure_reason="candidate digest matches reference solution; excluded by gym contract",
        )

    def _call_grader(target_task: Path, out_dir: Path, **kwargs: Any) -> dict[str, Any]:
        res: dict[str, Any] = {}
        max_tries = 60 if grader_fn is run_candidate else 1
        for attempt in range(max_tries):
            try_dir = (
                out_dir if attempt == 0 else out_dir.with_name(f"{out_dir.name}-retry-{attempt}")
            )
            if attempt > 0:
                time.sleep(3.0)
            try:
                policy = config.candidate_kwargs()
            except LocalDockerDisabledError as exc:
                return {
                    "status": "refused",
                    "reward": None,
                    "executed": False,
                    "reason_code": "execution_backend_refused",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            res = dict(grader_fn(target_task, output_dir=try_dir, **kwargs, **policy))
            err_text = str(res.get("error") or "")
            if (
                res.get("status") in {"refused", "error"}
                and "admitted after controller margin" in err_text
                and policy["backend"] != "modal"
            ):
                if (
                    grader_fn is run_candidate
                    and "solve.sh" in submission_files
                ):
                    from evallab.qa.task import HarborBackend
                    from evallab.registry import task_directory_digest

                    hb_target = target_task
                    provenance: dict[str, Any] | None = None
                    if kwargs.get("grader") == "port":
                        pt = try_dir / "port-task" / target_task.name
                        if not (pt / "task.toml").is_file():
                            pt = try_dir / "port-task"
                        if not (pt / "task.toml").is_file() and kwargs.get("port_task"):
                            from evallab.candidate_port import prepare_port_task

                            prep = prepare_port_task(
                                target_task, Path(kwargs["port_task"]), try_dir / "port-task-hb"
                            )
                            pt = Path(prep["task_dir"])
                        hb_target = pt
                        provenance = {"task_dir": str(hb_target)}

                    hb_dir = out_dir.with_name(f"{out_dir.name}-hb")
                    shutil.rmtree(hb_dir, ignore_errors=True)
                    config.require_backend()
                    hb = HarborBackend(
                        repo_root=Path(__file__).resolve().parents[3],
                        allow_local_docker=config.allow_local_docker,
                    )
                    obs = hb.run_container_trial(
                        hb_target,
                        digest=task_directory_digest(hb_target),
                        mode="shortcut",
                        script=submission_files["solve.sh"],
                        trial_dir=hb_dir,
                        timeout_seconds=timeout_seconds,
                    )
                    rew = _optional_float(obs.facts.get("reward"))
                    ok = obs.status == "executed" and rew is not None
                    v_dir = hb_dir / "verifier"
                    v_dir.mkdir(parents=True, exist_ok=True)
                    ctrf_path = v_dir / "ctrf.json"
                    log_text = ""
                    for log_name in ("test_output.log", "test-stdout.txt"):
                        lp = v_dir / log_name
                        if lp.is_file():
                            log_text = lp.read_text(encoding="utf-8", errors="replace")
                            if log_text.strip():
                                break
                    tests_list: list[dict[str, str]] = []
                    ctrf_tests: list[dict[str, str]] = []
                    for m in re.finditer(r"(?m)^(?:FAILED|ERROR|FAIL:)\s+(\S+)", log_text):
                        tid = m.group(1).strip()
                        tests_list.append({"id": tid, "status": "failed"})
                        ctrf_tests.append(
                            {"name": tid, "status": "failed", "trace": log_text[-900:]}
                        )
                    if not tests_list and rew == 0.0:
                        tests_list.append({"id": "verifier_test_command", "status": "failed"})
                        ctrf_tests.append(
                            {
                                "name": "verifier_test_command",
                                "status": "failed",
                                "trace": log_text[-900:] or "verifier returned reward 0.0",
                            }
                        )
                    if not ctrf_path.is_file():
                        ctrf_path.write_text(
                            json.dumps({"results": {"tests": ctrf_tests}}, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    return {
                        "status": "measured" if ok else "error",
                        "reward": rew,
                        "error": None if ok else (obs.reason or "container trial failed"),
                        "output_dir": str(hb_dir),
                        "trial_dir": str(hb_dir),
                        "grader_provenance": provenance,
                        "per_test": {
                            "tests": tests_list,
                            "sources": [str(ctrf_path)],
                        },
                    }
                continue
            return res
        return res
    up_dir = attempt_dir / "original"
    rep_dir = attempt_dir / "repaired"
    use_bundle = grader_fn is run_candidate and "solve.sh" in submission_files
    if use_bundle:
        bundle_dir = attempt_dir / "solution-bundle"
        shutil.rmtree(bundle_dir, ignore_errors=True)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in submission_files.items():
            dest = bundle_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        (bundle_dir / "solve.sh").chmod(0o755)
        _verify_replay_attestation(
            candidate, (bundle_dir / "solve.sh").read_text(encoding="utf-8")
        )
        original_res = _call_grader(
            task.original_task,
            up_dir,
            solution_bundle=bundle_dir,
            grader="upstream",
            timeout_seconds=timeout_seconds,
        )
    else:
        bundle_dir = None
        original_res = _call_grader(
            task.original_task,
            up_dir,
            files=submission_files,
            grader="upstream",
            timeout_seconds=timeout_seconds,
        )
    orig_reward = _optional_float(original_res.get("reward"))
    orig_status = str(
        original_res.get("status") or ("measured" if orig_reward is not None else "error")
    )
    failed_tests = _extract_failed_test_ids(original_res)
    excerpts = tuple(failure_excerpts(original_res))
    orig_trial = str(
        original_res.get("trial_dir")
        or original_res.get("native_output_dir")
        or original_res.get("output_dir")
        or up_dir
    )

    if (
        orig_status != "measured"
        or original_res.get("error")
        or not _is_exact_reward(orig_reward, 1.0)
    ):
        reason = str(
            original_res.get("error")
            or f"original grader returned reward={orig_reward} (status={orig_status})"
        )
        candidate_exit = _candidate_exit_code(original_res)
        if original_res.get("reason_code") == "execution_backend_refused":
            failure_kind = "execution_backend_refused"
        elif (
            orig_status == "measured"
            and not original_res.get("error")
            and orig_reward is not None
        ):
            failure_kind = "upstream_failed"
        elif candidate_exit not in (None, 0):
            # The submitted script exited nonzero, so no reward was produced. The
            # grader ran; the candidate failed. Counting this as an unavailable
            # grader hides a model failure inside infrastructure.
            failure_kind = "candidate_failed"
            reason = f"candidate solution exited {candidate_exit}; {reason}"
        else:
            failure_kind = "original_grader_unavailable"
        return DifferentialGradeOutcome(
            original_reward=orig_reward,
            repaired_reward=None,
            original_status=orig_status,
            repaired_status="not_run",
            differential_confirmed=False,
            failure_kind=failure_kind,
            failure_reason=reason,
            failed_tests=failed_tests,
            failure_excerpts=excerpts,
            original_trial_dir=orig_trial,
            repaired_trial_dir=None,
            original_result=original_res,
            repaired_result={},
        )

    if task.repaired_grader_kind == "port":
        if use_bundle and bundle_dir is not None:
            repaired_res = _call_grader(
                task.original_task,
                rep_dir,
                solution_bundle=bundle_dir,
                grader="port",
                port_task=task.repaired_task,
                timeout_seconds=timeout_seconds,
            )
        else:
            repaired_res = _call_grader(
                task.original_task,
                rep_dir,
                files=submission_files,
                grader="port",
                port_task=task.repaired_task,
                timeout_seconds=timeout_seconds,
            )
    else:
        if use_bundle and bundle_dir is not None:
            repaired_res = _call_grader(
                task.repaired_task,
                rep_dir,
                solution_bundle=bundle_dir,
                grader="upstream",
                timeout_seconds=timeout_seconds,
            )
        else:
            repaired_res = _call_grader(
                task.repaired_task,
                rep_dir,
                files=submission_files,
                grader="upstream",
                timeout_seconds=timeout_seconds,
            )
    rep_reward = _optional_float(repaired_res.get("reward"))
    rep_status = str(
        repaired_res.get("status") or ("measured" if rep_reward is not None else "error")
    )
    rep_trial = str(
        repaired_res.get("trial_dir")
        or repaired_res.get("native_output_dir")
        or repaired_res.get("output_dir")
        or rep_dir
    )
    rep_failed = _extract_failed_test_ids(repaired_res)
    rep_excerpts = tuple(failure_excerpts(repaired_res))
    materialized_repaired_task: str | None = None
    if task.repaired_grader_kind == "port":
        provenance = repaired_res.get("grader_provenance")
        if isinstance(provenance, Mapping) and provenance.get("task_dir"):
            materialized_repaired_task = str(provenance["task_dir"])

    if rep_status != "measured" or repaired_res.get("error") or rep_reward is None:
        reason = str(
            repaired_res.get("error")
            or f"repaired grader unverified: status={rep_status}, reward={rep_reward}"
        )
        return DifferentialGradeOutcome(
            original_reward=orig_reward,
            repaired_reward=rep_reward,
            original_status=orig_status,
            repaired_status=rep_status,
            differential_confirmed=False,
            failure_kind="execution_backend_refused"
            if repaired_res.get("reason_code") == "execution_backend_refused"
            else "repaired_grader_unverified",
            failure_reason=reason,
            failed_tests=rep_failed or failed_tests,
            failure_excerpts=rep_excerpts or excerpts,
            original_trial_dir=orig_trial,
            repaired_trial_dir=rep_trial,
            materialized_repaired_task=materialized_repaired_task,
            original_result=original_res,
            repaired_result=repaired_res,
        )

    if _is_exact_reward(orig_reward, 1.0) and _is_exact_reward(rep_reward, 0.0):
        executed_solve = (
            (bundle_dir / "solve.sh").read_text(encoding="utf-8")
            if use_bundle and bundle_dir is not None
            else candidate.ensure_submission_files().get("solve.sh", "")
        )
        _verify_replay_attestation(candidate, executed_solve)
        return DifferentialGradeOutcome(
            original_reward=1.0,
            repaired_reward=0.0,
            original_status=orig_status,
            repaired_status=rep_status,
            differential_confirmed=True,
            failure_kind=None,
            failure_reason=None,
            failed_tests=rep_failed,
            failure_excerpts=rep_excerpts,
            original_trial_dir=orig_trial,
            repaired_trial_dir=rep_trial,
            materialized_repaired_task=materialized_repaired_task,
            original_result=original_res,
            repaired_result=repaired_res,
        )

    return DifferentialGradeOutcome(
        original_reward=orig_reward,
        repaired_reward=rep_reward,
        original_status=orig_status,
        repaired_status=rep_status,
        differential_confirmed=False,
        failure_kind="repaired_grader_also_passed",
        failure_reason=(
            f"repaired grader returned reward={rep_reward}; passing both graders is not a "
            "differential exploit"
        ),
        failed_tests=rep_failed,
        failure_excerpts=rep_excerpts,
        original_trial_dir=orig_trial,
        repaired_trial_dir=rep_trial,
        materialized_repaired_task=materialized_repaired_task,
        original_result=original_res,
        repaired_result=repaired_res,
    )


def _candidate_exit_code(result: Mapping[str, Any]) -> int | None:
    """Exit code the trial recorded for the candidate's own solution.

    ``None`` means the trial recorded none (the usual case for a measured grade),
    so callers must not read it as success. A nonzero value means the submitted
    script failed before any grader assertion ran.
    """
    trial = result.get("trial_dir")
    if not isinstance(trial, str) or not trial.strip():
        return None
    try:
        text = (Path(trial) / "agent" / "exit-code.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _extract_failed_test_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
    per_test = result.get("per_test")
    if not isinstance(per_test, Mapping):
        return ()
    tests = per_test.get("tests")
    if not isinstance(tests, Sequence):
        return ()
    failed: list[str] = []
    for item in tests:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        if status not in {"passed", "skipped"}:
            name = str(item.get("id") or item.get("name") or "").strip()
            if name:
                failed.append(name)
    return tuple(failed)


def _evidence_content_digest(path: Path) -> str:
    """Full sha256 over a file's bytes or a tree's canonical manifest.

    Every member's relative path and file digest participates, so changed
    receipt bytes always change the digest. Symlinks and non-file members are
    refused outright: evidence custody must name real immutable bytes, never
    an indirection that can drift after the pin.
    """
    if path.is_symlink():
        raise SeekerEvidenceError(f"evidence path is a symlink: {path}")
    if path.is_file():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise SeekerEvidenceError(f"declared evidence source is missing: {path}")
    entries: list[tuple[str, str]] = []
    for member in sorted(path.rglob("*")):
        relative = member.relative_to(path).as_posix()
        if member.is_symlink():
            raise SeekerEvidenceError(f"evidence tree contains a symlink: {member}")
        if member.is_dir():
            entries.append((relative + "/", "-"))
        elif member.is_file():
            entries.append((relative, hashlib.sha256(member.read_bytes()).hexdigest()))
        else:
            raise SeekerEvidenceError(f"evidence tree holds a non-file member: {member}")
    aggregate = hashlib.sha256()
    for relative, digest in entries:
        aggregate.update(f"{digest}  ./{relative}\n".encode())
    return "sha256:" + aggregate.hexdigest()


def _evidence_custody_key(
    *,
    job_event_id: str,
    run_id: str,
    task_id: str,
    candidate_digest: str,
    original_task: Path,
    repaired_task: str,
    sources: Mapping[str, str | None],
) -> str:
    """Witness-custody key binding job, package, candidate, and source bytes.

    The pinned witness directory is keyed on the full source content digests
    plus the job and package identities — never on the truncated candidate
    digest alone — so the same candidate replayed against changed original or
    repaired packages, or under another job, can never silently reuse an
    earlier pin's receipts. A missing or undigestable declared source fails
    closed: execution proof stays unavailable rather than a confirmed asset
    carrying invalid references.
    """
    from evallab.registry import task_directory_digest

    material: dict[str, Any] = {
        "job": job_event_id,
        "run_id": run_id,
        "task_id": task_id,
        "candidate_digest": candidate_digest,
        "original_task": str(original_task),
        "repaired_task": repaired_task,
        "package_digests": {},
        "sources": {},
    }
    for name, package in (("original_task", original_task), ("repaired_task", Path(repaired_task))):
        package_path = Path(package)
        try:
            if package_path.is_dir():
                material["package_digests"][name] = task_directory_digest(package_path)
            else:
                material["package_digests"][name] = _evidence_content_digest(package_path)
        except (OSError, ValueError, SeekerEvidenceError) as exc:
            raise SeekerEvidenceError(f"{name} package is undigestable: {exc}") from exc
    for slot, source in sources.items():
        if not isinstance(source, str) or not source:
            raise SeekerEvidenceError(f"{slot} evidence is not declared")
        material["sources"][slot] = _evidence_content_digest(Path(source))
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _copy_evidence_tree(source: Path, stage: Path) -> None:
    """Copy a tree into ``stage``, refusing symlinked or non-file members."""
    stage.mkdir(parents=True)
    for member in sorted(source.rglob("*")):
        target = stage / member.relative_to(source)
        if member.is_symlink():
            raise SeekerEvidenceError(f"evidence tree contains a symlink: {member}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(member, target)
        else:
            raise SeekerEvidenceError(f"evidence tree holds a non-file member: {member}")


def _publish_evidence_tree(source: Path, destination: Path) -> None:
    """Publish a tree at ``destination`` atomically under content custody.

    The copy is staged on a sibling temporary path and renamed, so an
    interrupted publish leaves no partial destination for a later retry to
    accept. An existing destination is kept only when it holds byte-identical
    content; a symlink, a non-directory, or different bytes are refused —
    first-writer wins only when both writers carried the same evidence.
    """
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SeekerEvidenceError(f"refusing to pin over non-directory: {destination}")
    if destination.exists():
        if _evidence_content_digest(destination) == _evidence_content_digest(source):
            return
        raise SeekerEvidenceError(
            f"pinned evidence at {destination} conflicts with new source bytes"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _copy_evidence_tree(source, stage)
        os.replace(stage, destination)
    except OSError as exc:
        # A concurrent pinner may have published between the check and the
        # rename; identical bytes are a no-op, anything else is refused.
        if destination.is_dir() and not destination.is_symlink():
            if _evidence_content_digest(destination) == _evidence_content_digest(source):
                return
            raise SeekerEvidenceError(
                f"pinned evidence at {destination} conflicts with new source bytes"
            ) from exc
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _publish_evidence_file(source: Path, target: Path) -> None:
    """Publish one file atomically; identical bytes are a no-op, others refuse."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SeekerEvidenceError(f"refusing to pin over non-file: {target}")
    data = source.read_bytes()
    if target.is_file() and target.read_bytes() == data:
        return
    if target.exists():
        raise SeekerEvidenceError(
            f"pinned evidence at {target} conflicts with new source bytes"
        )
    stage = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with stage.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hardlink publication can never overwrite an existing entry.
            os.link(stage, target)
        except FileExistsError as exc:
            if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
                return
            raise SeekerEvidenceError(
                f"pinned evidence at {target} conflicts with new source bytes"
            ) from exc
        except OSError as exc:
            # Filesystems without hardlink support fall back to rename after a
            # final existence check.
            if target.exists():
                if (
                    target.is_file()
                    and not target.is_symlink()
                    and target.read_bytes() == data
                ):
                    return
                raise SeekerEvidenceError(
                    f"pinned evidence at {target} conflicts with new source bytes"
                ) from exc
            os.replace(stage, target)
    finally:
        stage.unlink(missing_ok=True)


def _pin_evidence(source: str | None, destination: Path) -> str:
    """Pin witness evidence outside the mutable attempt workspace.

    Confirmed ``seeker.exploit_confirmed``/``asset.verified`` payloads
    reference run receipts and the solution bundle from the durable
    journal, but ``attempt-<slot>`` workspaces are wiped and reused by
    later jobs for the same task — confirmed evidence is published under a
    custody-keyed ``witness-*/`` sibling instead. Publication is staged
    then renamed atomically, an existing destination must hold identical
    bytes, and symlinks or conflicting bytes are refused. A declared
    source that is missing or unreadable raises ``SeekerEvidenceError``:
    execution proof stays unavailable rather than the confirmation
    falling back to a mutable workspace path with invalid references.
    """
    if not isinstance(source, str) or not source:
        raise SeekerEvidenceError("declared evidence source is empty")
    src = Path(source)
    if src.is_symlink() or not src.exists():
        raise SeekerEvidenceError(f"declared evidence source is missing: {source}")
    if src.is_dir():
        _publish_evidence_tree(src, destination)
        return str(destination)
    if src.is_file():
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise SeekerEvidenceError(
                f"refusing to pin under non-directory: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / src.name
        _publish_evidence_file(src, target)
        return str(target)
    raise SeekerEvidenceError(f"declared evidence source is unusable: {source}")


@lru_cache(maxsize=8)
def _load_assignments_csv_index(csv_path: str) -> dict[str, tuple[tuple[str, str], ...]]:
    import csv as _csv

    by_bare: dict[str, set[tuple[str, str]]] = {}
    path = Path(csv_path)
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = _csv.DictReader(handle)
        for row in reader:
            key_id = str(row.get("key_id") or "").strip()
            split = str(row.get("split") or "").strip().casefold()
            if not key_id or not split:
                continue
            for col in ("item_id", "task", "unit"):
                val = str(row.get(col) or "").strip()
                if not val:
                    continue
                bare = val.split("@", 1)[0].rsplit("/", 1)[-1]
                by_bare.setdefault(bare, set()).add((key_id, split))
                if "__" in bare:
                    by_bare.setdefault(bare.split("__")[-1], set()).add((key_id, split))
    return {k: tuple(sorted(v)) for k, v in by_bare.items()}


def find_cross_key_non_dev_assignments(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``(key_id, split)`` pairs where ``task_id`` is non-DEV in ``assignments.csv``."""
    csv_file = (
        Path(assignments_csv)
        if assignments_csv is not None
        else Path(__file__).resolve().parents[3] / "research/answer-keys/splits/assignments.csv"
    )
    index = _load_assignments_csv_index(str(csv_file.resolve()))
    raw = str(task_id or "").strip()
    bare = raw.split("@", 1)[0].rsplit("/", 1)[-1]
    candidates = {bare}
    if "__" in bare:
        candidates.add(bare.split("__")[-1])
    non_dev: set[tuple[str, str]] = set()
    for cand in candidates:
        for key_id, split in index.get(cand, ()):
            if split not in {"dev", "train"}:
                non_dev.add((key_id, split))
    return tuple(sorted(non_dev))


def find_cross_key_dev_assignments(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``(key_id, split)`` pairs where ``task_id`` is affirmatively DEV/train in ``assignments.csv``."""
    csv_file = (
        Path(assignments_csv)
        if assignments_csv is not None
        else Path(__file__).resolve().parents[3] / "research/answer-keys/splits/assignments.csv"
    )
    index = _load_assignments_csv_index(str(csv_file.resolve()))
    raw = str(task_id or "").strip()
    bare = raw.split("@", 1)[0].rsplit("/", 1)[-1]
    candidates = {bare}
    if "__" in bare:
        candidates.add(bare.split("__")[-1])
    dev_rows: set[tuple[str, str]] = set()
    for cand in candidates:
        for key_id, split in index.get(cand, ()):
            if split in {"dev", "train"}:
                dev_rows.add((key_id, split))
    return tuple(sorted(dev_rows))


def require_cross_key_dev_only(
    task_id: str,
    *,
    assignments_csv: Path | None = None,
    require_affirmative_dev: bool = False,
) -> None:
    """Raise ``SeekerRefusalError`` if ``task_id`` has any test/heldout row in ``assignments.csv``."""
    non_dev = find_cross_key_non_dev_assignments(task_id, assignments_csv=assignments_csv)
    if non_dev:
        raise SeekerRefusalError(
            f"task {task_id!r} has non-DEV split in secondary assignments.csv keys: {list(non_dev)}"
        )
    if require_affirmative_dev and not find_cross_key_dev_assignments(
        task_id, assignments_csv=assignments_csv
    ):
        raise SeekerRefusalError(
            f"task {task_id!r} has no affirmative DEV assignment in assignments.csv"
        )



def _parse_omp_json_stdout(raw_text: str) -> tuple[str, str | None, str | None]:
    """Return final text and actual provider/model from OMP's JSON event stream."""
    text = ""
    served_provider: str | None = None
    served_model: str | None = None
    for message in _omp_stream_messages(raw_text):
        if message.get("role") != "assistant":
            continue
        parts = [
            part.get("text", "")
            for part in message.get("content") or []
            if part.get("type") == "text"
        ]
        if any(parts):
            text = "".join(parts)
            served_provider = str(message["provider"]) if message.get("provider") else None
            served_model = str(message["model"]) if message.get("model") else None
    return text, served_provider, served_model
