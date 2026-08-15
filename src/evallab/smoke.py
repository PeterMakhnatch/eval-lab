from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from evallab.atif import (
    IngestProjectionResult,
    ProjectionInvariant,
    check_projection_invariant,
    project_jobs,
)
from evallab.automation import HeadlessDoctor
from evallab.digest import DigestRenderer, DigestTrial
from evallab.facts import AnalyzerCallResult, run_trial_analysis
from evallab.paths import derived_root_from_environment
from evallab.queue import DirectoryQueue, Executor, load_policy, new_ulid
from evallab.results import JobRecord, load_job
from evallab.runner import RunRequest, database_url_from_environment
from evallab.schemas import (
    CanaryDriftObservation,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    TrialAnalysisSidecar,
)
from evallab.status import StatusSnapshot, build_status_snapshot

FIXTURE_JOB = Path("research/evidence/runs/event-summary-oracle-evidence")
SMOKE_TASK = "library/tasks/event-summary"


@dataclass(frozen=True)
class SmokeResult:
    mode: str
    job_name: str
    job_id: str
    trial_count: int
    digest_path: Path
    digest_text: str
    invariant: ProjectionInvariant
    analysis_path: Path | None = None
    analysis_validation: str | None = None
    scratch_dir: Path | None = None
    status: StatusSnapshot | None = None


def _docker_free_report(report_date: date) -> HeadlessDoctorReport:
    checks = HeadlessDoctorChecks(
        keychain_readable=False,
        codex_auth_present=True,
        docker_reachable=True,
        postgres_reachable=True,
        disk_headroom=True,
    )
    return HeadlessDoctorReport(
        checked_at=datetime.combine(report_date, time.min, tzinfo=UTC),
        healthy=True,
        checks=checks,
    )


def _copy_fixture_runner(repo_root: Path):
    source = (repo_root / FIXTURE_JOB).resolve()

    def run(request: RunRequest) -> Path:
        destination = request.jobs_dir / request.name
        shutil.copytree(source, destination)
        return destination

    return run


def _digest_trials(job: JobRecord, report_date: date):
    def load(day: date) -> list[DigestTrial]:
        if day != report_date:
            return []
        return [
            DigestTrial(
                job_name=job.name,
                task_name=str(trial.result.get("task_name") or ""),
                agent_name=str((trial.result.get("agent_info") or {}).get("name") or ""),
                model_name=None,
                reward=trial.primary_reward,
                exception_type=None,
                cost_usd=0.0,
                finished_at=str(trial.result.get("finished_at") or ""),
            )
            for trial in job.trials
        ]

    return load


def _empty_drift(_day: date) -> list[CanaryDriftObservation]:
    return []


def _stub_analyzer(repo_root: Path):
    stub = repo_root / "research/analysis/stub-oracle-analysis.json"

    def analyzer(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        return AnalyzerCallResult(
            raw_output=stub.read_text(),
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )

    return analyzer


def _attach_stub_analysis(
    root: Path, job: JobRecord, destination: Path
) -> tuple[Path, TrialAnalysisSidecar]:
    prompt = root / "research/analysis/stage5-prompt.md"
    rubric = root / "research/analysis/stage5-rubric.json"
    stub = root / "research/analysis/stub-oracle-analysis.json"
    missing = [path.name for path in (prompt, rubric, stub) if not path.is_file()]
    if missing:
        raise RuntimeError("smoke missing committed stage-5 inputs: " + ",".join(missing))
    if not job.trials:
        raise RuntimeError("smoke oracle produced no trials")
    return run_trial_analysis(
        job,
        job.trials[0],
        analyzer=_stub_analyzer(root),
        repo_root=root,
        destination_root=destination,
        prompt_path=prompt,
        rubric_path=rubric,
        agent="stub",
        agent_version="1",
        model="saved-response",
    )


def _assert_oracle_job(job: JobRecord) -> None:
    if not job.trials:
        raise RuntimeError("smoke oracle produced no trials")
    rewards = [trial.primary_reward for trial in job.trials]
    if any(reward != 1.0 for reward in rewards):
        raise RuntimeError(f"smoke oracle reward mismatch: {rewards}")


def _assert_both_stores(job: JobRecord, invariant: ProjectionInvariant) -> None:
    if job.id not in invariant.catalog_job_ids:
        raise RuntimeError(f"smoke job {job.id} is absent from the catalog")
    if job.id not in invariant.projected_job_ids:
        raise RuntimeError(f"smoke job {job.id} has no complete Parquet projection")
    if job.id in invariant.missing_job_ids or job.id in invariant.excepted_job_ids:
        raise RuntimeError(f"smoke job {job.id} is not a clean two-store record")


def run_smoke(
    repo_root: Path,
    *,
    docker_free: bool,
    run_token: str | None = None,
    report_date: date | None = None,
) -> SmokeResult:
    root = repo_root.resolve()
    token = (run_token or new_ulid()).lower()
    job_name = f"smoke-oracle-{token[-12:]}"
    relative_scratch = Path("runs/_smoke") / job_name
    scratch = root / relative_scratch
    queue = DirectoryQueue(scratch / "queue")
    derived_root = (
        scratch / "parquet" if docker_free else derived_root_from_environment(root)
    )
    target_date = report_date or date.today()

    if docker_free:
        report = _docker_free_report(target_date)
        catalog_rows: list[tuple[str, str, str | None]] = []

        def ingest(job_dir: Path) -> IngestProjectionResult:
            job = load_job(job_dir)
            catalog_rows.extend(
                (job.id, job.name, trial.id) for trial in job.trials
            )
            if not job.trials:
                catalog_rows.append((job.id, job.name, None))
            tables, failures = project_jobs([job], derived_root)
            return IngestProjectionResult(
                cataloged_jobs=1,
                tables=tables,
                failures=failures,
            )

        executor = Executor(
            repo_root=root,
            queue=queue,
            policy=load_policy(root / "policy/standing-approvals.yaml"),
            runner=_copy_fixture_runner(root),
            ingester=ingest,
            spent_today=lambda: 0.0,
            consecutive_harness_failures=lambda: 0,
            credential_probe=lambda: frozenset(),
        )
    else:
        report = HeadlessDoctor(root).run()
        executor = Executor(
            repo_root=root,
            queue=queue,
            policy=load_policy(root / "policy/standing-approvals.yaml"),
            credential_probe=lambda: frozenset(),
        )

    if not report.healthy:
        failed = [name for name, ok in report.checks.model_dump().items() if not ok]
        raise RuntimeError("smoke doctor failed: " + ",".join(failed))

    approved, decision = executor.submit(
        ExperimentSpec(
            name=job_name,
            hypothesis="prove the free composed evaluation path",
            task=SMOKE_TASK,
            agent="oracle",
            jobs_dir=(relative_scratch / "jobs").as_posix(),
            submitted_by="solidify-smoke",
            est_cost_usd=0,
        )
    )
    if not decision.admitted or approved.parent.name != "approved":
        raise RuntimeError(f"smoke submission was not admitted: {decision.reason_code}")
    if executor.tick() != 1:
        raise RuntimeError("smoke tick did not dispatch exactly one control")

    job_dir = root / relative_scratch / "jobs" / job_name
    job = load_job(job_dir)
    _assert_oracle_job(job)

    if docker_free:
        invariant = check_projection_invariant(
            "postgresql://docker-free-smoke",
            derived_root,
            queue.events_path,
            catalog_rows_loader=lambda _url: list(catalog_rows),
        )
        trial_loader = _digest_trials(job, target_date)
        drift_loader = _empty_drift
    else:
        invariant = check_projection_invariant(
            database_url_from_environment(),
            derived_root,
            queue.events_path,
        )
        trial_loader = None
        drift_loader = None
    _assert_both_stores(job, invariant)
    if not docker_free and not invariant.ok:
        raise RuntimeError(
            "shared catalog/Parquet invariant failed after smoke: " + invariant.detail
        )

    renderer = DigestRenderer(
        repo_root=scratch,
        queue=queue,
        policy=load_policy(root / "policy/standing-approvals.yaml"),
        trial_loader=trial_loader,
        drift_loader=drift_loader,
    )
    digest_path = renderer.write(
        report_date=target_date,
        health_report=report,
        dispatched=1,
    )
    digest_text = digest_path.read_text()
    if job.name not in digest_text or "Dispatches in this nightly cycle: 1" not in digest_text:
        raise RuntimeError("smoke digest does not contain the completed control")

    analysis_path: Path | None = None
    analysis_validation: str | None = None
    snapshot: StatusSnapshot | None = None
    if docker_free:
        analysis_path, sidecar = _attach_stub_analysis(root, job, scratch / "analyses")
        analysis_validation = sidecar.validation_status
        snapshot = build_status_snapshot(
            scratch,
            postgres_probe=lambda: False,
            phoenix_probe=lambda: False,
        )
        if snapshot.Analysis.availability == "unavailable":
            raise RuntimeError("smoke status snapshot is missing the stage-5 sidecar")

    return SmokeResult(
        mode="docker-free" if docker_free else "full",
        job_name=job.name,
        job_id=job.id,
        trial_count=len(job.trials),
        digest_path=digest_path,
        digest_text=digest_text,
        invariant=invariant,
        analysis_path=analysis_path,
        analysis_validation=analysis_validation,
        scratch_dir=scratch,
        status=snapshot,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prove doctor + free oracle + queue + catalog + Parquet + "
            "stub stage-5 sidecar + digest + status"
        )
    )
    parser.add_argument(
        "--docker-free",
        action="store_true",
        help="use committed evidence and an in-memory catalog while writing real Parquet",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd().resolve()
    result = run_smoke(root, docker_free=args.docker_free)
    print(f"PASS doctor mode={result.mode}")
    print(f"PASS submit->tick job={result.job_name} trials={result.trial_count}")
    print(f"PASS catalog job_id={result.job_id}")
    print(f"PASS parquet job_id={result.job_id}")
    print(f"PASS digest path={result.digest_path.relative_to(root)}")
    if result.analysis_path is not None:
        print(
            f"PASS analysis sidecar={result.analysis_path.relative_to(root)} "
            f"validation={result.analysis_validation}"
        )
    if result.status is not None:
        print(
            "PASS status snapshot sections="
            + ",".join(result.status.section_map())
            + f" analysis={result.status.Analysis.availability}"
        )
    print("SMOKE PASS both-stores-agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
