from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if (start.tzinfo is None) != (finish.tzinfo is None):
        return None
    return (finish - start).total_seconds()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_file(relative_path: Path) -> str:
    parts = relative_path.parts
    name = relative_path.name
    if name == "lab-metadata.json":
        return "provenance"
    if name == "manifest.json" and "artifacts" in parts:
        return "artifact_manifest"
    if "artifacts" in parts:
        return "artifact"
    if "agent" in parts:
        return "agent_log"
    if "verifier" in parts:
        return "verifier_evidence"
    if name == "config.json":
        return "config"
    if name == "lock.json":
        return "lock"
    if name == "result.json":
        return "result"
    if name.endswith(".log") or name == "exception.txt":
        return "log"
    return "other"


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    kind: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactRecord:
    source: str
    destination: str | None
    artifact_type: str | None
    status: str | None
    service: str | None
    host_relative_path: str | None
    exists: bool
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True)
class TrialRecord:
    path: Path
    result: JsonObject
    config: JsonObject
    lock: JsonObject
    rewards: dict[str, float]
    artifacts: tuple[ArtifactRecord, ...]
    is_regrade: bool = False
    source_trial_id: str | None = None
    valid_fraction: float | None = None
    verifier_status: str | None = None

    @property
    def id(self) -> str:
        return str(self.result.get("id", self.path.name))

    @property
    def name(self) -> str:
        return str(self.result["trial_name"])

    @property
    def primary_reward(self) -> float | None:
        reward = self.rewards.get("reward")
        return float(reward) if reward is not None else None


@dataclass(frozen=True)
class JobRecord:
    path: Path
    result: JsonObject
    config: JsonObject
    lock: JsonObject
    metadata: JsonObject
    trials: tuple[TrialRecord, ...] = field(default_factory=tuple)
    files: tuple[FileRecord, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return str(self.result["id"])

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def harbor_version(self) -> str | None:
        harbor = self.lock.get("harbor") or {}
        version = harbor.get("version")
        return str(version) if version is not None else None


def _load_object(path: Path) -> JsonObject:
    if not path.is_file():
        return {}
    value = read_json(path)
    return value if isinstance(value, dict) else {}


def _try_load_object(path: Path) -> JsonObject:
    """Best-effort object load used only by job *discovery*.

    A candidate ``result.json`` that is not valid JSON is not a job-shaped
    Harbor result, and discovery must skip it and keep scanning rather than
    abort the whole tree. The canonical example is a faithfully-promoted failed
    trial artifact: an agent wrote a diagnostic scalar before the JSON document
    (``3\n{...}``), and that malformed file must stay as evidence while every
    default scan (``status``, ``trajectories``, ``nightly``, ``cards``,
    ``screen``, ``quota``, ``ingest``, ``analyst``, ``lance``) still finds the
    real jobs around it. ``load_job``/``load_trial`` deliberately keep calling
    ``_load_object`` and fail closed on malformed JSON for a *selected* job.
    """
    try:
        return _load_object(path)
    except (json.JSONDecodeError, UnicodeError):
        return {}


def _load_artifacts(trial_dir: Path) -> tuple[ArtifactRecord, ...]:
    manifest_path = trial_dir / "artifacts" / "manifest.json"
    if not manifest_path.is_file():
        return ()
    manifest = read_json(manifest_path)
    entries = manifest if isinstance(manifest, list) else manifest.get("entries", [])
    records: list[ArtifactRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        destination = entry.get("destination")
        host_path: Path | None = None
        if destination is not None:
            destination_path = Path(str(destination))
            if destination_path.is_absolute():
                raise ValueError(
                    f"artifact destination must be relative to the trial directory: {destination}"
                )
            candidate = (trial_dir / destination_path).resolve()
            try:
                candidate.relative_to(trial_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"artifact destination escapes the trial directory: {destination}"
                ) from exc
            host_path = candidate
        exists = bool(host_path and host_path.exists())
        size_bytes: int | None = None
        digest: str | None = None
        if host_path and host_path.is_file():
            size_bytes = host_path.stat().st_size
            digest = sha256_file(host_path)
        records.append(
            ArtifactRecord(
                source=str(entry.get("source", "")),
                destination=str(destination) if destination is not None else None,
                artifact_type=entry.get("type"),
                status=entry.get("status"),
                service=entry.get("service"),
                host_relative_path=(
                    host_path.relative_to(trial_dir.resolve()).as_posix() if host_path else None
                ),
                exists=exists,
                size_bytes=size_bytes,
                sha256=digest,
            )
        )
    return tuple(records)


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _is_regrade_trial_result(result: JsonObject) -> bool:
    """Detect a standalone verifier-only regrade trial result."""
    if not isinstance(result, dict):
        return False
    if "task_name" not in result or "trial_name" not in result:
        return False
    # Job-level roll-ups carry n_total_trials and stats; regrades do not.
    if "n_total_trials" in result or "stats" in result:
        return False
    if result.get("is_regrade"):
        return True
    if result.get("source_trial_id") or result.get("source_trial"):
        return True
    return str(result.get("trial_name", "")).endswith("_regrade")


def _source_trial_id_for_regrade(trial_dir: Path, result: JsonObject) -> str | None:
    source = result.get("source_trial_id") or result.get("source_trial")
    if source:
        return str(source)
    trial_name = str(result.get("trial_name", trial_dir.name))
    if trial_name.endswith("_regrade"):
        return trial_name[: -len("_regrade")]
    return None


def load_trial(trial_dir: Path) -> TrialRecord:
    result = _load_object(trial_dir / "result.json")
    verifier_result = result.get("verifier_result") or {}
    raw_rewards = verifier_result.get("rewards") or {}
    rewards = {
        str(name): float(value)
        for name, value in raw_rewards.items()
        if isinstance(value, int | float)
    }
    is_regrade = _is_regrade_trial_result(result)
    source_trial_id = _source_trial_id_for_regrade(trial_dir, result) if is_regrade else None
    return TrialRecord(
        path=trial_dir,
        result=result,
        config=_load_object(trial_dir / "config.json"),
        lock=_load_object(trial_dir / "lock.json"),
        rewards=rewards,
        artifacts=_load_artifacts(trial_dir),
        is_regrade=is_regrade,
        source_trial_id=source_trial_id,
        valid_fraction=_optional_float(verifier_result.get("valid_fraction")),
        verifier_status=verifier_result.get("status"),
    )


def load_regrade_trial(trial_dir: Path) -> TrialRecord:
    """Load a standalone verifier-only regrade trial and link it to its source."""
    trial = load_trial(trial_dir)
    updates: dict[str, Any] = {"is_regrade": True}
    if trial.source_trial_id is None:
        source_id = _source_trial_id_for_regrade(trial_dir, trial.result)
        if source_id is not None:
            updates["source_trial_id"] = source_id
    if not trial.result.get("id"):
        # Regrades may not carry a UUID; derive a stable id from the path.
        result = dict(trial.result)
        result["id"] = trial.path.name
        updates["result"] = result
    if updates:
        trial = replace(trial, **updates)
    return trial


def load_job(job_dir: Path) -> JobRecord:
    result = _load_object(job_dir / "result.json")
    if "n_total_trials" not in result or "stats" not in result or not result.get("finished_at"):
        raise ValueError(f"Not a completed Harbor job directory: {job_dir}")

    trials: list[TrialRecord] = []
    for candidate in sorted(job_dir.iterdir()):
        if not candidate.is_dir() or not (candidate / "result.json").is_file():
            continue
        candidate_result = _load_object(candidate / "result.json")
        if "task_name" in candidate_result and "trial_name" in candidate_result:
            trials.append(load_trial(candidate))
    expected_trials = result.get("n_total_trials")
    if (
        not isinstance(expected_trials, int)
        or isinstance(expected_trials, bool)
        or expected_trials < 0
        or expected_trials != len(trials)
    ):
        raise ValueError(
            "Completed Harbor job trial count mismatch: "
            f"expected {expected_trials!r}, indexed {len(trials)} in {job_dir}"
        )

    files = tuple(
        FileRecord(
            relative_path=path.relative_to(job_dir).as_posix(),
            kind=classify_file(path.relative_to(job_dir)),
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
        )
        for path in sorted(job_dir.rglob("*"))
        if path.is_file()
    )
    return JobRecord(
        path=job_dir,
        result=result,
        config=_load_object(job_dir / "config.json"),
        lock=_load_object(job_dir / "lock.json"),
        metadata=_load_object(job_dir / "lab-metadata.json"),
        trials=tuple(trials),
        files=files,
    )


def _is_bookkeeping(relative: Path) -> bool:
    """True for a path inside a dot-prefixed bookkeeping directory.

    Discovery here is depth-agnostic (``rglob``), which is deliberate — it is the
    one reader that finds a job wherever it landed. The cost is that it also
    finds directories that are job-*shaped* but are not evaluation output:

    - the queue archives an abandoned transient attempt by *moving* the whole job
      directory, job-level ``result.json`` and all, to
      ``<jobs-root>/.transient-attempts/<name>/attempt-<n>`` (``queue.py:1053``);
    - the executor keeps its logs under ``<jobs-root>/.executor``
      (``runner.py:540``);
    - Harbor caches regrade sources as a complete job under
      ``<jobs-root>/.sources/<uuid>/<job>`` (Harbor 0.21.0
      ``trial/regrade.py:175``, ``download/downloader.py:118``).

    Without this, one real job with two retried attempts is reported as three
    completed jobs, inflating ``evallab status``, the consumption ledger
    (``quota.py:717``), ``compare``, and every cohort built from these roots.
    ``explorer.py`` already excludes dot-prefixed directories (``_is_job_dir``);
    this is the same rule, for the same reason, on the other discovery path.

    A root named *explicitly* is never filtered — naming a path is a request, not
    a discovery, which is how ``harbor view <dir>`` behaves too.
    """
    return any(part.startswith(".") for part in relative.parts)


def discover_job_dirs(roots: Iterable[Path]) -> list[Path]:
    discovered: dict[Path, None] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if root.is_dir() and (root / "result.json").is_file():
            result = _try_load_object(root / "result.json")
            if "n_total_trials" in result and "stats" in result and result.get("finished_at"):
                discovered[root] = None
                continue
        if not root.exists():
            continue
        for result_path in root.rglob("result.json"):
            candidate = result_path.parent
            if _is_bookkeeping(candidate.relative_to(root)):
                continue
            # A candidate that fails to parse (e.g. a malformed trial artifact
            # faithfully preserved as evidence) is not a job; skip it, never
            # abort the scan.
            result = _try_load_object(result_path)
            if "n_total_trials" in result and "stats" in result and result.get("finished_at"):
                discovered[candidate] = None
    return sorted(discovered)


def discover_regrade_trials(roots: Iterable[Path]) -> list[Path]:
    """Discover standalone verifier-only regrade trial directories.

    Regrade directories are not Harbor jobs: they have no ``n_total_trials``
    roll-up and are linked to a source trial by ``source_trial_id`` or by a
    ``_regrade`` trial-name suffix.
    """
    discovered: dict[Path, None] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.exists():
            continue
        for result_path in root.rglob("result.json"):
            candidate = result_path.parent
            if _is_bookkeeping(candidate.relative_to(root)):
                continue
            result = _try_load_object(result_path)
            if _is_regrade_trial_result(result):
                discovered[candidate] = None
    return sorted(discovered)


def load_jobs(roots: Iterable[Path]) -> list[JobRecord]:
    return [load_job(path) for path in discover_job_dirs(roots)]
