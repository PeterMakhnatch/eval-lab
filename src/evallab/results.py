from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
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
class SeatRecord:
    name: str
    under_test: bool | None
    result: JsonObject
    participant: JsonObject


@dataclass(frozen=True)
class TrialRecord:
    path: Path
    result: JsonObject
    config: JsonObject
    lock: JsonObject
    rewards: dict[str, float]
    artifacts: tuple[ArtifactRecord, ...]
    seats: tuple[SeatRecord, ...] = ()

    @property
    def id(self) -> str:
        return str(self.result["id"])

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


_SEAT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _seat_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SEAT_NAME.fullmatch(value):
        raise ValueError(f"{where} has no usable seat name: {value!r}")
    return value


def parse_seats(result: JsonObject, config: JsonObject, lock: JsonObject) -> tuple[SeatRecord, ...]:
    result_config = result.get("config")
    sources = (
        result_config.get("participants") if isinstance(result_config, dict) else None,
        config.get("participants"),
        lock.get("participants"),
    )
    raw_participants = next((value for value in sources if isinstance(value, list) and value), [])
    participants: dict[str, JsonObject] = {}
    for index, participant in enumerate(raw_participants):
        if not isinstance(participant, dict):
            raise ValueError(f"participants[{index}] must be an object")
        seat_name = _seat_name(participant.get("seat"), f"participants[{index}]")
        if seat_name in participants:
            raise ValueError(f"participants records seat {seat_name!r} twice")
        participants[seat_name] = participant
    raw_results = result.get("seat_results")
    entries = raw_results if isinstance(raw_results, list) else []
    seats: list[SeatRecord] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"seat_results[{index}] must be an object")
        name = _seat_name(entry.get("seat"), f"seat_results[{index}]")
        if any(seat.name == name for seat in seats):
            raise ValueError(f"seat_results records seat {name!r} twice")
        under_test = entry.get("under_test")
        part_entry = participants.get(name, {})
        if not isinstance(under_test, bool):
            under_test = part_entry.get("under_test")
        seats.append(
            SeatRecord(
                name=name,
                under_test=under_test if isinstance(under_test, bool) else None,
                result=entry,
                participant=part_entry,
            )
        )
    recorded = {seat.name for seat in seats}
    for name, participant in participants.items():
        if name not in recorded:
            raw_ut = participant.get("under_test")
            seats.append(
                SeatRecord(
                    name=name,
                    under_test=raw_ut if isinstance(raw_ut, bool) else None,
                    result={},
                    participant=participant,
                )
            )
    return tuple(sorted(seats, key=lambda seat: seat.name))


def trial_agent_dirs(trial: TrialRecord) -> tuple[tuple[str | None, Path], ...]:
    if not trial.seats:
        return ((None, trial.path / "agent"),)
    return tuple((seat.name, trial.path / "seats" / seat.name / "agent") for seat in trial.seats)


def load_trial(trial_dir: Path) -> TrialRecord:
    result = _load_object(trial_dir / "result.json")
    verifier_result = result.get("verifier_result") or {}
    raw_rewards = verifier_result.get("rewards") or {}
    rewards = {
        str(name): float(value)
        for name, value in raw_rewards.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    }
    config = _load_object(trial_dir / "config.json")
    lock = _load_object(trial_dir / "lock.json")
    return TrialRecord(
        path=trial_dir,
        result=result,
        config=config,
        lock=lock,
        rewards=rewards,
        artifacts=_load_artifacts(trial_dir),
        seats=parse_seats(result, config, lock),
    )


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
            result = _try_load_object(result_path)
            if "n_total_trials" in result and "stats" in result and result.get("finished_at"):
                discovered[candidate] = None
    return sorted(discovered)


def load_jobs(roots: Iterable[Path]) -> list[JobRecord]:
    return [load_job(path) for path in discover_job_dirs(roots)]
