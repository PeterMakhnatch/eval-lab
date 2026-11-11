"""Read-only QC and paired summaries of stored Seeker campaign results.

Historical cells retain their attestation, digest, condition, and reward contracts.
This module does not generate submissions or execute campaigns.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evallab.production.actor_cas import read_owned_file
from evallab.production.seeker_worker import _extract_failed_test_ids, _sha256_text
from evallab.seeker.feedback import failure_excerpts

COHORT_SCHEMA = "evallab.sb1_clean_seeker_set/v1"
SUMMARY_SCHEMA = "evallab.sb1_error_first_summary/v1"

TURNS: tuple[str, str] = ("turn-1", "turn-2")
DEFAULT_WORKERS = 6
MAX_WORKERS = 8
DEFAULT_PER_MODEL_CONCURRENCY = 2
_MAX_EVIDENCE_FILES = 3
_EVIDENCE_READ_BYTES = 65_536
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _served_requested_route(cell: dict[str, Any], route: str) -> bool:
    provider, model = cell.get("served_provider"), cell.get("served_model")
    return bool(model) and route in (model, f"{provider}/{model}")


def _ts4_outcome(cell: dict[str, Any], model: str) -> tuple[bool, bool]:
    """(attested, executed differential); a differential only counts on an attested cell."""
    ok = cell.get("attestation_ok") is True and _served_requested_route(cell, model)
    hit = ok and cell.get("original_reward") == 1.0 and cell.get("repaired_reward") == 0.0
    return ok, hit


def _verify_factorial_cell_digests(cell: dict[str, Any], cell_directory: Path) -> None:
    """Refuse a factorial cell whose recorded LLM stdout digest does not bind its file."""
    expected = cell.get("llm_stdout_sha256")
    if expected is None:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"{cell_directory} has a malformed llm_stdout_sha256")
    artifact = cell_directory / "response.jsonl"
    try:
        payload = read_owned_file(artifact)
    except FileNotFoundError as exc:
        raise ValueError(
            f"{cell_directory} records llm_stdout_sha256 but response.jsonl is missing"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"{artifact} does not match the recorded llm_stdout_sha256")


class CohortError(ValueError):
    """The cohort manifest does not satisfy ``evallab.sb1_clean_seeker_set/v1``."""


# --------------------------------------------------------------------------- cohort


@dataclass(frozen=True)
class CohortTask:
    """One qualified clean-set task: an original/repaired grader pair."""

    task_id: str
    dataset_id: str
    original_task: Path
    repaired_task: Path
    repaired_grader_kind: Literal["port", "package"]
    original_package_digest: str
    repaired_package_digest: str
    original_reservation_usd: float
    repaired_reservation_usd: float

    @property
    def name(self) -> str:
        return self.task_id.split("@", 1)[0].rsplit("/", 1)[-1]

    @property
    def pair_reservation_usd(self) -> float:
        """Upper bound for grading one submission on both graders."""
        return self.original_reservation_usd + self.repaired_reservation_usd


@dataclass(frozen=True)
class Cohort:
    tasks: tuple[CohortTask, ...]
    assignments_csv_sha256: str | None = None
    git_head: str | None = None


def _text(row: Mapping[str, Any], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CohortError(f"{where}: {key} must be a nonblank string")
    return value


def _usd(row: Mapping[str, Any], key: str, where: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CohortError(f"{where}: {key} must be a number")
    if not math.isfinite(value) or value < 0:
        raise CohortError(f"{where}: {key} must be finite and non-negative")
    return float(value)


def parse_cohort(payload: Mapping[str, Any]) -> Cohort:
    """Validate a decoded cohort manifest; every qualified task needs both grader roots."""
    if payload.get("schema") != COHORT_SCHEMA:
        raise CohortError(f"cohort schema must be {COHORT_SCHEMA!r}, got {payload.get('schema')!r}")
    rows = payload.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise CohortError("cohort tasks must be a nonempty list")
    tasks: list[CohortTask] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        where = f"tasks[{index}]"
        if not isinstance(row, Mapping):
            raise CohortError(f"{where} must be an object")
        kind = row.get("repaired_grader_kind")
        if kind not in {"port", "package"}:
            raise CohortError(f"{where}: repaired_grader_kind must be 'port' or 'package'")
        digests = {}
        for key in ("original_package_digest", "repaired_package_digest"):
            digests[key] = _text(row, key, where)
            if not _SHA256.fullmatch(digests[key]):
                raise CohortError(f"{where}: {key} must be sha256:<64 hex>")
        roots = {}
        for key in ("original_task", "repaired_task"):
            roots[key] = Path(_text(row, key, where)).expanduser()
            if not roots[key].is_absolute():
                raise CohortError(f"{where}: {key} must be an absolute path")
        task = CohortTask(
            task_id=_text(row, "task_id", where),
            dataset_id=_text(row, "dataset_id", where),
            original_task=roots["original_task"],
            repaired_task=roots["repaired_task"],
            repaired_grader_kind=kind,
            original_package_digest=digests["original_package_digest"],
            repaired_package_digest=digests["repaired_package_digest"],
            original_reservation_usd=_usd(row, "original_reservation_usd", where),
            repaired_reservation_usd=_usd(row, "repaired_reservation_usd", where),
        )
        identity = (task.dataset_id, task.name)
        if identity in seen:
            raise CohortError(f"{where}: duplicate cell identity {identity}")
        seen.add(identity)
        tasks.append(task)
    csv_digest = payload.get("assignments_csv_sha256")
    git_head = payload.get("git_head")
    return Cohort(
        tasks=tuple(tasks),
        assignments_csv_sha256=(
            csv_digest.removeprefix("sha256:") if isinstance(csv_digest, str) else None
        ),
        git_head=git_head if isinstance(git_head, str) else None,
    )


def load_cohort(path: Path) -> Cohort:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CohortError(f"cannot read cohort {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise CohortError("cohort must be a JSON object")
    return parse_cohort(payload)


# ------------------------------------------------------------------ small utilities


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def model_slug(route: str) -> str:
    """Directory name shared with ``qc_reports`` readers."""
    return route.replace("/", "_")


def exact_binomial_two_sided(left: int, right: int) -> float:
    """Two-sided exact McNemar p-value on ``left``/``right`` discordant pairs."""
    total = left + right
    if total == 0:
        return 1.0
    smaller = min(left, right)
    tail = sum(math.comb(total, i) for i in range(smaller + 1)) / 2**total
    return min(1.0, 2 * tail)


def exact_binomial_improvement(turn_1_only: int, turn_2_only: int) -> float:
    """One-sided exact p-value that turn 2 beats turn 1 (P[X >= turn_2_only], X~Bin(n, 1/2))."""
    total = turn_1_only + turn_2_only
    if total == 0:
        return 1.0
    return sum(math.comb(total, i) for i in range(turn_2_only, total + 1)) / 2**total


@dataclass(frozen=True)
class RunnerError:
    """The real ORIGINAL-grader output of one submission, bounded but unparaphrased."""

    status: str
    reward: float | None
    error: str | None
    sandbox_exit_codes: tuple[int, ...]
    failed_tests: tuple[str, ...]
    excerpts: tuple[tuple[str, str], ...]
    verifier_output: str
    verifier_files: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "grader": "original",
            "status": self.status,
            "reward": self.reward,
            "error": self.error,
            "sandbox_exit_codes": list(self.sandbox_exit_codes),
            "failed_tests": list(self.failed_tests),
            "excerpts": [[name, trace] for name, trace in self.excerpts],
            "verifier_output": self.verifier_output,
            "verifier_files": list(self.verifier_files),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> RunnerError:
        return cls(
            status=str(payload["status"]),
            reward=payload.get("reward"),
            error=payload.get("error"),
            sandbox_exit_codes=tuple(int(code) for code in payload.get("sandbox_exit_codes", [])),
            failed_tests=tuple(str(item) for item in payload.get("failed_tests", [])),
            excerpts=tuple((str(a), str(b)) for a, b in payload.get("excerpts", [])),
            verifier_output=str(payload.get("verifier_output", "")),
            verifier_files=tuple(str(item) for item in payload.get("verifier_files", [])),
        )

    @property
    def sha256(self) -> str:
        return _sha256_text(_canonical(self.to_json()))


def _evidence_entries(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    logs = result.get("logs")
    entries = logs.get("verifier") if isinstance(logs, Mapping) else None
    if not entries:
        entries = result.get("raw_verifier_evidence")
    return [item for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []


def _evidence_rank(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    if name in {"test-stdout.txt", "test_output.log"}:
        return (0, name)
    if name.endswith((".log", ".txt")):
        return (1, name)
    return (2, name)


def _read_evidence_tail(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    """(basename, decoded tail) of one emitted verifier log, or None if unusable."""
    raw = entry.get("path")
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    if path.is_symlink() or not path.is_file() or path.suffix.lower() in {".json", ".toml"}:
        return None
    digest = entry.get("digest")
    with path.open("rb") as stream:
        if isinstance(digest, str) and digest.removeprefix("sha256:") != hashlib.file_digest(
            stream, "sha256"
        ).hexdigest():
            return None
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(size - _EVIDENCE_READ_BYTES, 0))
        data = stream.read()
    return path.name, data.decode("utf-8", errors="replace")


def _sandbox_exit_codes(result: Mapping[str, Any]) -> tuple[int, ...]:
    lifecycle = result.get("lifecycle")
    closure = lifecycle.get("closure") if isinstance(lifecycle, Mapping) else None
    sandboxes = closure.get("sandboxes") if isinstance(closure, Mapping) else None
    if not isinstance(sandboxes, list):
        return ()
    return tuple(
        item["exit_code"]
        for item in sandboxes
        if isinstance(item, Mapping) and type(item.get("exit_code")) is int
    )


def extract_runner_error(original_result: Mapping[str, Any]) -> RunnerError:
    """Collect the native ORIGINAL-grade output; reads only emitted verifier evidence."""
    entries = sorted(
        (item for item in _evidence_entries(original_result) if isinstance(item.get("path"), str)),
        key=lambda item: _evidence_rank(Path(str(item["path"]))),
    )
    chunks: list[tuple[str, str]] = []
    for entry in entries:
        tail = _read_evidence_tail(entry)
        if tail is not None and tail[1].strip():
            chunks.append(tail)
        if len(chunks) >= _MAX_EVIDENCE_FILES:
            break
    output = "\n".join(f"--- {name} ---\n{body.strip()}" for name, body in chunks)
    reward = original_result.get("reward")
    error = original_result.get("error") or original_result.get("native_error")
    return RunnerError(
        status=str(original_result.get("status") or "unknown"),
        reward=float(reward) if isinstance(reward, (int, float)) else None,
        error=str(error) if error else None,
        sandbox_exit_codes=_sandbox_exit_codes(original_result),
        failed_tests=_extract_failed_test_ids(original_result),
        excerpts=tuple(failure_excerpts(dict(original_result))),
        verifier_output=_tail(output, 8000),
        verifier_files=tuple(name for name, _ in chunks),
    )


def result_is_evaluated(cell: Mapping[str, Any]) -> bool:
    """A terminal grading verdict exists: attested, original measured, repaired measured if run."""
    if cell.get("attestation_ok") is not True or cell.get("original_status") != "measured":
        return False
    reward = cell.get("original_reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        return False
    return reward != 1.0 or cell.get("repaired_status") == "measured"


def _differential_hit(cell: Mapping[str, Any], route: str) -> bool:
    return bool(
        result_is_evaluated(cell)
        and _ts4_outcome(dict(cell), route)[1]
        and cell.get("repaired_status") == "measured"
        and cell.get("verified_differential") is True
    )


def read_turn(
    turn_dir: Path, route: str, turn: int, *, hide_tests: bool = True
) -> tuple[str, dict[str, Any] | None]:
    """Classify one retained turn: missing | invalid | unevaluated | evaluated."""
    result_path = turn_dir / "result.json"
    if not result_path.is_file():
        return "missing", None
    try:
        cell = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(cell, dict):
            return "invalid", None
        if (
            cell.get("requested_model_route") != route
            or cell.get("hide_tests") is not hide_tests
            or cell.get("turn") != turn
        ):
            return "invalid", cell
        _verify_factorial_cell_digests(cell, turn_dir)
        solve, executed = cell.get("llm_solve_sha256"), cell.get("executed_solve_sha256")
        if executed is not None and executed != solve:
            return "invalid", cell
        rewards_hit = cell.get("original_reward") == 1.0 and cell.get("repaired_reward") == 0.0
        if cell.get("verified_differential") is True and not (
            rewards_hit and cell.get("attestation_ok") is True
        ):
            return "invalid", cell
        if turn == 2 and cell.get("outcome") != "not_evaluated_no_runner_error":
            block = turn_dir / "feedback.md"
            if (
                cell.get("feedback_source") != "runner_error"
                or not block.is_file()
                or _sha256_bytes(block.read_bytes()) != cell.get("feedback_block_sha256")
            ):
                return "invalid", cell
    except (OSError, ValueError):
        return "invalid", None
    return ("evaluated" if result_is_evaluated(cell) else "unevaluated"), cell


# ------------------------------------------------------------------- campaign


@dataclass(frozen=True)
class CampaignConfig:
    cohort: Cohort
    models: tuple[str, ...]
    out: Path
    workers: int = DEFAULT_WORKERS
    per_model_concurrency: int = DEFAULT_PER_MODEL_CONCURRENCY
    resume: bool = False
    assignments_csv: Path | None = None
    omp_timeout: float = 300.0
    grader_timeout: int = 600
    omp_config: Path | None = None
    probe_routes: bool = True
    hide_tests: bool = True

    def __post_init__(self) -> None:
        if not self.models or len(set(self.models)) != len(self.models):
            raise ValueError("models must be a nonempty list without duplicates")
        if not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
        if not 1 <= self.per_model_concurrency <= self.workers:
            raise ValueError("per-model concurrency must be between 1 and workers")


def _cell_dir(out: Path, task: CohortTask, model: str) -> Path:
    return out / task.dataset_id / task.name / model_slug(model)


def summarize_campaign(
    config: CampaignConfig, *, route_probes: Sequence[Any] = ()
) -> dict[str, Any]:
    """Per-model turn-1/turn-2 counts over one denominator plus exact paired statistics.

    A route whose probe failed planned cells that were never attempted. It is
    reported as ``route_unavailable`` with its probe, never as a run of missing or
    zero-valued cells: an unservable route is not evidence about the model named.
    """
    blocked = {
        getattr(probe, "route", None): probe
        for probe in route_probes
        if not getattr(probe, "ok", True)
    }
    models: list[dict[str, Any]] = []
    for route in config.models:
        if route in blocked:
            probe = blocked[route]
            models.append(
                {
                    "model": route,
                    "cells_planned": len(config.cohort.tasks),
                    "route_unavailable": True,
                    "route_probe": (
                        probe.to_json() if hasattr(probe, "to_json") else {"route": route}
                    ),
                    "status_counts": {"route_unavailable": len(config.cohort.tasks)},
                    "attested_turn_1": 0,
                    "attested_turn_2": 0,
                    "attested_tasks_turn_1": [],
                    "attested_tasks_turn_2": [],
                    "paired": {
                        "denominator": 0,
                        "turn_1_executed_differentials": 0,
                        "turn_2_executed_differentials": 0,
                        "turn_1_only": 0,
                        "turn_2_only": 0,
                        "both": 0,
                        "neither": 0,
                        "delta_turn_2_minus_turn_1": None,
                        "mcnemar_exact_p_two_sided": exact_binomial_two_sided(0, 0),
                        "exact_p_one_sided_turn_2_better": exact_binomial_improvement(0, 0),
                        "turn_1_only_tasks": [],
                        "turn_2_only_tasks": [],
                        "both_tasks": [],
                    },
                    "differential_tasks_turn_1": [],
                    "differential_tasks_turn_2": [],
                    "unevaluated_cells": [],
                    "turn_1_evaluated_without_pair": [],
                    "missing_cells": [],
                }
            )
            continue
        counts: Counter[str] = Counter()
        attested: dict[int, list[str]] = {1: [], 2: []}
        differential: dict[int, list[str]] = {1: [], 2: []}
        pairs: list[tuple[str, bool, bool]] = []
        unevaluated: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        unpaired: list[dict[str, Any]] = []
        for task in config.cohort.tasks:
            cell_dir = _cell_dir(config.out, task, route)
            label = f"{task.dataset_id}/{task.name}"
            reads = {
                turn: read_turn(
                cell_dir / TURNS[turn - 1], route, turn, hide_tests=config.hide_tests
            )
            for turn in (1, 2)
            }
            for turn, (status, cell) in reads.items():
                counts[f"turn_{turn}_{status}"] += 1
                if (
                    cell is not None
                    and status in {"evaluated", "unevaluated"}
                    and _ts4_outcome(cell, route)[0]
                ):
                    attested[turn].append(label)
                if status == "evaluated" and cell is not None and _differential_hit(cell, route):
                    differential[turn].append(label)
            statuses = (reads[1][0], reads[2][0])
            if statuses == ("evaluated", "evaluated"):
                assert reads[1][1] is not None and reads[2][1] is not None
                pairs.append(
                    (
                        label,
                        _differential_hit(reads[1][1], route),
                        _differential_hit(reads[2][1], route),
                    )
                )
                continue
            if "missing" in statuses:
                missing.append({"task": label, "turns": ",".join(statuses)})
                continue
            unevaluated.append(
                {
                    "task": label,
                    "turn_1": statuses[0],
                    "turn_1_outcome": (reads[1][1] or {}).get("outcome"),
                    "turn_2": statuses[1],
                    "turn_2_outcome": (reads[2][1] or {}).get("outcome"),
                }
            )
            if statuses[0] == "evaluated" and reads[1][1] is not None:
                unpaired.append({"task": label, "turn_1_differential": _differential_hit(reads[1][1], route)})
        turn_1_only = [label for label, one, two in pairs if one and not two]
        turn_2_only = [label for label, one, two in pairs if two and not one]
        both = [label for label, one, two in pairs if one and two]
        denominator = len(pairs)
        hits_1 = len(turn_1_only) + len(both)
        hits_2 = len(turn_2_only) + len(both)
        models.append(
            {
                "model": route,
                "cells_planned": len(config.cohort.tasks),
                "status_counts": dict(sorted(counts.items())),
                "attested_turn_1": len(attested[1]),
                "attested_turn_2": len(attested[2]),
                "attested_tasks_turn_1": attested[1],
                "attested_tasks_turn_2": attested[2],
                "paired": {
                    "denominator": denominator,
                    "turn_1_executed_differentials": hits_1,
                    "turn_2_executed_differentials": hits_2,
                    "turn_1_only": len(turn_1_only),
                    "turn_2_only": len(turn_2_only),
                    "both": len(both),
                    "neither": denominator - hits_1 - len(turn_2_only),
                    "delta_turn_2_minus_turn_1": (
                        (hits_2 - hits_1) / denominator if denominator else None
                    ),
                    "mcnemar_exact_p_two_sided": exact_binomial_two_sided(
                        len(turn_1_only), len(turn_2_only)
                    ),
                    "exact_p_one_sided_turn_2_better": exact_binomial_improvement(
                        len(turn_1_only), len(turn_2_only)
                    ),
                    "turn_1_only_tasks": turn_1_only,
                    "turn_2_only_tasks": turn_2_only,
                    "both_tasks": both,
                },
                "differential_tasks_turn_1": differential[1],
                "differential_tasks_turn_2": differential[2],
                "unevaluated_cells": unevaluated,
                "turn_1_evaluated_without_pair": unpaired,
                "missing_cells": missing,
            }
        )
    return {
        "schema": SUMMARY_SCHEMA,
        "hide_tests": config.hide_tests,
        "denominator_rule": "cells where both turns are evaluated (attested, original measured, "
        "repaired measured when run); unevaluated or missing cells are listed, never zero-filled",
        "models": models,
    }


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# SB-1 error-first summary",
        "",
        str(summary["denominator_rule"]),
        "",
        "| model | paired n | turn 1 | turn 2 | delta | t1 only | t2 only | p (2-sided) "
        "| attested t1/t2 | unevaluated | missing |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in summary["models"]:
        paired = item["paired"]
        delta = paired["delta_turn_2_minus_turn_1"]
        lines.append(
            f"| {item['model']} | {paired['denominator']} | "
            f"{paired['turn_1_executed_differentials']} | {paired['turn_2_executed_differentials']} | "
            f"{'n/a' if delta is None else f'{delta:+.3f}'} | {paired['turn_1_only']} | "
            f"{paired['turn_2_only']} | {paired['mcnemar_exact_p_two_sided']:.4f} | "
            f"{item['attested_turn_1']}/{item['attested_turn_2']} | "
            f"{len(item['unevaluated_cells'])} | {len(item['missing_cells'])} |"
        )
    return "\n".join(lines) + "\n"


