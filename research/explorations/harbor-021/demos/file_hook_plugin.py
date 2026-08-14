"""Local no-network Harbor job plugin.

Writes a JSONL hook log on on_job_start / on_job_end (and trial events).
Modeled on packages/harbor-langsmith LangSmithPlugin: subclass BaseJobPlugin,
register trial hooks in on_job_start. No network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from harbor.job import Job
from harbor.models.job.plugin import BaseJobPlugin
from harbor.models.job.result import JobResult
from harbor.trial.hooks import TrialHookEvent


class FileHookPlugin(BaseJobPlugin):
    def __init__(self, *, output_dir: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not output_dir:
            raise ValueError("FileHookPlugin requires output_dir")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / "hooks.jsonl"

    def _write(self, event: str, **payload: Any) -> None:
        record = {
            "event": event,
            "ts": datetime.now(UTC).isoformat(),
            **payload,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    @override
    async def on_job_start(self, job: Job) -> None:
        self._write(
            "on_job_start",
            job_name=job.config.job_name,
            job_dir=str(job.job_dir),
            n_tasks=len(getattr(job.config, "tasks", []) or []),
        )
        job.on_trial_started(self._handle_event)
        job.on_trial_ended(self._handle_event)

    @override
    async def on_job_end(self, job_result: JobResult) -> None:
        stats = job_result.stats
        self._write(
            "on_job_end",
            job_id=str(job_result.id),
            n_total_trials=job_result.n_total_trials,
            n_completed=getattr(stats, "n_completed_trials", None),
            n_errored=getattr(stats, "n_errored_trials", None),
            finished_at=str(job_result.finished_at),
        )

    async def _handle_event(self, event: TrialHookEvent) -> None:
        self._write(
            "trial_event",
            trial_event=event.event.value if hasattr(event.event, "value") else str(event.event),
            trial_name=event.trial_name,
            task_name=event.task_name,
        )
