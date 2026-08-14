from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from evallab.queue import QUEUE_STATES, Executor
from evallab.schemas import CanarySuite, ExperimentSpec


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
        self._validate_suite()
        existing_names = {
            spec.name
            for state in QUEUE_STATES
            for _, spec in self.executor.queue.list_specs(state)
        }
        submitted = 0
        for member in self.suite.members:
            for agent in self.suite.agents:
                job_name = f"canary-{member.name}-{agent}-{run_date.strftime('%Y%m%d')}"
                if job_name in existing_names:
                    continue
                destination, decision = self.executor.submit(
                    ExperimentSpec(
                        name=job_name,
                        hypothesis=(
                            f"Pinned canary {member.name} remains stable on {agent}; "
                            "any excursion is a harness-drift suspect."
                        ),
                        task=f"canary/{member.name}",
                        task_path=member.task_path,
                        agent=agent,
                        attempts=self.suite.attempts,
                        submitted_by="nightly-canary",
                        priority=50,
                        est_cost_usd=member.est_cost_usd,
                        policy_rule="canary",
                        task_version=member.task_version,
                        verifier_digest=member.task_digest,
                    )
                )
                if not decision.admitted or destination.parent.name != "approved":
                    raise RuntimeError(
                        f"standing policy refused configured canary {job_name}: "
                        f"{decision.reason_code or 'unknown'}"
                    )
                existing_names.add(job_name)
                submitted += 1
        return submitted

    def _validate_suite(self) -> None:
        estimated_total = sum(member.est_cost_usd for member in self.suite.members) * len(
            self.suite.agents
        )
        ceiling = self.executor.gate.policy.daily_cost_ceiling_usd
        if estimated_total > ceiling:
            raise ValueError(
                f"canary suite estimate {estimated_total:.2f} exceeds daily ceiling {ceiling:.2f}"
            )
        for member in self.suite.members:
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
                available = sorted(
                    path.parent.name for path in downloaded.rglob("task.toml")
                )
                raise ValueError(
                    f"expected one downloaded task named {task_name}, found {len(matches)}; "
                    f"available: {', '.join(available)}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(matches[0], target)
        return target
