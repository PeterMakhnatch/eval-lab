from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path

import yaml
from pydantic import ValidationError

from evallab.queue import QUEUE_STATES, Executor
from evallab.schemas import CanaryMember, CanarySuite, ExperimentSpec


def load_canary_suite(path: Path) -> CanarySuite:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load canary suite: {exc}") from exc
    try:
        return CanarySuite.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid canary suite: {exc}") from exc


def task_directory_digest(path: Path) -> str:
    """Digest sorted relative paths and file digests, independent of checkout location."""
    if not path.is_dir():
        raise ValueError(f"canary task directory is missing: {path}")
    aggregate = hashlib.sha256()
    files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    if not files:
        raise ValueError(f"canary task directory is empty: {path}")
    for candidate in files:
        relative = candidate.relative_to(path).as_posix()
        file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        aggregate.update(f"{file_digest}  ./{relative}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


class CanaryEnqueuer:
    def __init__(self, *, repo_root: Path, executor: Executor, suite: CanarySuite) -> None:
        self.repo_root = repo_root.resolve()
        self.executor = executor
        self.suite = suite

    @classmethod
    def from_repo(cls, root: Path, executor: Executor) -> CanaryEnqueuer:
        return cls(
            repo_root=root,
            executor=executor,
            suite=load_canary_suite(root / "policy/canary-suite.yaml"),
        )

    def enqueue(self, run_date: date) -> int:
        return self._enqueue_cycle(run_date, cycle=0)

    def enqueue_due(self, now: datetime | None = None) -> int:
        if now is None:
            now = datetime.now(UTC)
        elif now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        else:
            now = now.astimezone(UTC)
        midnight = datetime.combine(now.date(), time.min, tzinfo=UTC)
        cycle = int((now - midnight).total_seconds()) // self.suite.interval_seconds
        return self._enqueue_cycle(now.date(), cycle)

    def _enqueue_cycle(self, run_date: date, cycle: int) -> int:
        with self.executor.queue.tick_lock() as acquired:
            if not acquired or self.executor.queue.stop_path.exists():
                return 0
            if self.executor.queue.list_specs("running"):
                return 0
            existing_names = {
                spec.name
                for state in QUEUE_STATES
                for _, spec in self.executor.queue.list_specs(state)
            }
            day_str = run_date.strftime("%Y%m%d")
            base_names = {
                f"canary-{member.name}-{agent}-{day_str}": (member, agent)
                for member in self.suite.members
                for agent in self.suite.agents
            }
            enrolled_cycles: set[int] = set()
            for name in existing_names:
                if name in base_names:
                    enrolled_cycles.add(0)
                else:
                    base, separator, suffix = name.rpartition("-cycle")
                    if separator and base in base_names and suffix.isdecimal():
                        enrolled_cycles.add(int(suffix))
            if (
                cycle not in enrolled_cycles
                and len(enrolled_cycles) >= self.suite.max_cycles_per_day
            ):
                return 0
            candidates: list[tuple[CanaryMember, str, str]] = []
            needed_members: set[str] = set()
            for base_name, (member, agent) in base_names.items():
                job_name = base_name if cycle == 0 else f"{base_name}-cycle{cycle}"
                if job_name not in existing_names:
                    candidates.append((member, agent, job_name))
                    needed_members.add(member.name)
            if not candidates:
                return 0
            self._validate_suite_metadata()
            self._verify_task_digests(needed_members)
            return self._submit_candidates(candidates=candidates, existing_names=existing_names)

    def _submit_candidates(
        self,
        *,
        candidates: list[tuple[CanaryMember, str, str]],
        existing_names: set[str],
    ) -> int:
        submitted = 0
        for member, agent, job_name in candidates:
            if job_name in existing_names:
                continue
            destination, decision = self.executor.submit(
                ExperimentSpec(
                    name=job_name,
                    hypothesis=(
                        f"Pinned canary {member.name} remains stable on {agent}; "
                        "any excursion is a harness-drift suspect."
                    ),
                    purpose="drift",
                    task=f"canary/{member.name}",
                    task_path=member.task_path,
                    agent=agent,
                    attempts=self.suite.attempts,
                    submitted_by="nightly-canary",
                    priority=50,
                    est_cost_usd=member.est_cost_usd,
                    task_version=member.task_version,
                    verifier_digest=member.task_digest,
                )
            )
            staged_for_authorization = (
                decision.reason_code == "paid_run_unauthorized"
                and destination.parent.name == "waiting"
            )
            if not staged_for_authorization and (
                not decision.admitted or destination.parent.name != "approved"
            ):
                raise RuntimeError(
                    f"standing policy refused configured canary {job_name}: "
                    f"{decision.reason_code or 'unknown'}"
                )
            existing_names.add(job_name)
            submitted += 1
        return submitted

    def _validate_suite_metadata(self) -> None:
        estimated_per_cycle = sum(member.est_cost_usd for member in self.suite.members) * len(
            self.suite.agents
        )
        estimated_total = estimated_per_cycle * self.suite.max_cycles_per_day
        ceiling = self.executor.gate.policy.daily_cost_ceiling_usd
        if estimated_total > ceiling:
            raise ValueError(
                f"canary suite estimate {estimated_total:.2f} exceeds daily ceiling {ceiling:.2f}"
            )

    def _verify_task_digests(self, needed_members: set[str]) -> None:
        for member in self.suite.members:
            if member.name not in needed_members:
                continue
            task_path = (self.repo_root / member.task_path).resolve()
            if self.repo_root not in task_path.parents:
                raise ValueError(f"canary task escapes repository: {member.task_path}")
            actual = task_directory_digest(task_path)
            if actual != member.task_digest:
                raise ValueError(
                    f"canary task digest mismatch for {member.name}; "
                    "update the pinned version and digest through human review"
                )


class TerminalBenchCanaryImporter:
    """Import one task from a version-pinned Harbor dataset download."""

    def __init__(self, *, executor: Executor, repo_root: Path) -> None:
        self.executor = executor
        self.repo_root = repo_root.resolve()

    def import_task(
        self,
        *,
        dataset_ref: str,
        task_name: str,
        destination: Path,
    ) -> Path:
        target = destination.resolve()
        if target != self.repo_root and self.repo_root not in target.parents:
            raise ValueError("import destination must stay inside the repository")
        if target.exists():
            raise FileExistsError(f"import destination already exists: {target}")
        with tempfile.TemporaryDirectory(prefix="evallab-canary-") as temporary:
            downloaded = self.executor.download_dataset(dataset_ref, Path(temporary))
            matches = [
                path.parent
                for path in downloaded.rglob("task.toml")
                if path.parent.name == task_name
            ]
            if len(matches) != 1:
                available = sorted(path.parent.name for path in downloaded.rglob("task.toml"))
                raise ValueError(
                    f"expected one downloaded task named {task_name}, found {len(matches)}; "
                    f"available: {', '.join(available)}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(matches[0], target)
        return target
