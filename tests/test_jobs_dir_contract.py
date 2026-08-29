"""The ``jobs_dir`` / job-directory-layout contract.

One file, because the contract spans two modules: ``schemas.py`` decides what an
operator may *submit*, and ``results.py`` decides what discovery later *reports*.
Both sides answer the same question — "where does a job directory live" — and
they used to answer it differently.

Deterministic per agents/CHECKS.md: no clock, network, database, Docker, or
credential store. The only I/O is reading committed spec files and writing job
skeletons into pytest's ``tmp_path``.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.results import discover_job_dirs
from evallab.schemas import (
    EXPLORATION_JOBS_ROOT,
    SELF_TEST_JOBS_SCRATCH,
    ExperimentMatrix,
    ExperimentSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def spec(**overrides):
    payload = {
        "name": "layout-contract",
        "hypothesis": "jobs land where the readers look",
        "purpose": "practice",
        "task": "library/tasks/event-summary",
        "agent": "oracle",
        "submitted_by": "test",
    }
    payload.update(overrides)
    return payload


def matrix(**overrides):
    payload = {
        "schema_version": 2,
        "matrix_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
        "name": "layout-contract",
        "hypothesis": "jobs land where the readers look",
        "benchmark_family": "event-summary",
        "task_id": "event-summary",
        "task": "library/tasks/event-summary",
        "task_package_digest": "sha256:" + "1" * 64,
        "verifier_digest": "sha256:" + "2" * 64,
        "runs": [{"name": "oracle-control", "agent": "oracle"}],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# What may be submitted
# ---------------------------------------------------------------------------


def test_the_exploration_root_is_accepted_by_both_declarations():
    """``jobs_dir`` is declared twice and both declarations must honour it.

    ``ExperimentMatrix`` is not built from ``ExperimentSpec`` —
    ``runner.request_from_matrix`` expands a matrix straight into ``RunRequest``
    objects — so a rule applied to one class does not reach the other.
    """
    assert ExperimentSpec.model_validate(spec()).jobs_dir == EXPLORATION_JOBS_ROOT
    assert ExperimentMatrix.model_validate(matrix()).jobs_dir == EXPLORATION_JOBS_ROOT
    assert (
        ExperimentSpec.model_validate(spec(jobs_dir="runs")).jobs_dir == "runs"
    )
    assert ExperimentMatrix.model_validate(matrix(jobs_dir="runs")).jobs_dir == "runs"


@pytest.mark.parametrize("nested", ["runs/nightly/jobs", "runs/m009/jobs", "runs/a/b/c"])
def test_a_nested_jobs_dir_is_refused_at_submission(nested):
    """The F-04 shape, refused where it is cheap instead of hours later.

    A nested value did not merely drop the run: the explorer read the
    intermediate directory as a job, mistook the real job's roll-up
    ``result.json`` for a trial of it, and rendered a fabricated trial while the
    actual run vanished.
    """
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentSpec.model_validate(spec(jobs_dir=nested))
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentMatrix.model_validate(matrix(jobs_dir=nested))


def test_a_flat_root_the_readers_do_not_scan_is_refused_too():
    """Depth is not the rule; being a scanned root is.

    Discovery roots are fixed (``dashboard/explorer.py:80``), so a single-segment
    ``my-runs`` is exactly as invisible as a nested path. A validator that only
    counted path segments would accept this and still lose the run.
    """
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentSpec.model_validate(spec(jobs_dir="my-runs"))
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentMatrix.model_validate(matrix(jobs_dir="my-runs"))


def test_promoted_evidence_is_not_a_spec_target():
    """``research/evidence/runs`` is scanned but must not be written by a run.

    A promoted bundle is immutable (``AGENTS.md``) and is produced by promotion.
    Being readable is not the same as being writable.
    """
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentSpec.model_validate(spec(jobs_dir="research/evidence/runs"))


def test_the_refusal_names_the_expected_layout_and_the_command_to_fix_it():
    """An operator is told the shape and what to write, not just "invalid"."""
    with pytest.raises(ValidationError) as caught:
        ExperimentSpec.model_validate(spec(jobs_dir="runs/nightly/jobs"))
    message = str(caught.value)
    assert "<jobs-root>/<job>/<trial>" in message
    assert '"jobs_dir": "runs"' in message
    assert "uv run evallab submit" in message


@pytest.mark.parametrize("escape", ["/tmp/runs", "../../escape", "runs/../../escape"])
def test_a_matrix_jobs_dir_cannot_escape_the_repository(escape):
    """``ExperimentMatrix`` carried no path validation at all.

    It accepted ``/etc`` and ``../../escape``, which ``runner.py:716`` resolves
    outside the repository — against ``agents/WORKFLOW.md`` ("never point
    jobs_dir outside your worktree"). ``ExperimentSpec`` was already guarded.
    """
    with pytest.raises(ValidationError):
        ExperimentMatrix.model_validate(matrix(jobs_dir=escape))
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(spec(jobs_dir=escape))


def test_the_reserved_self_test_scratch_stays_submittable():
    """``evallab smoke`` deliberately nests, and must keep working.

    It writes ``runs/_smoke/<job>/jobs`` (``smoke.py:167,222``) and reads it back
    by direct path (``smoke.py:232``); those jobs carry the reserved ``smoke-``
    name prefix and are excluded from the digest. Nothing browses that area, so
    nesting under it hides nothing an operator is looking for. This is the one
    nested shape that is legitimate today — refusing it would have broken the
    lab's own self-test for no honesty gain.
    """
    smoke_shape = f"{SELF_TEST_JOBS_SCRATCH}/smoke-oracle-4f2a1c/jobs"
    assert ExperimentSpec.model_validate(spec(jobs_dir=smoke_shape)).jobs_dir == smoke_shape
    # The reservation is a prefix rule, not a blanket escape from the contract:
    # a sibling that merely looks similar is still refused.
    with pytest.raises(ValidationError, match="is not a jobs root this lab reads"):
        ExperimentSpec.model_validate(spec(jobs_dir="runs/_smoky/x/jobs"))


def test_every_committed_spec_and_matrix_still_validates():
    """The constraint is only correct if it breaks nothing already committed."""
    checked = 0
    for path in sorted(
        [*(REPO_ROOT / "research" / "experiments").rglob("*.json"),
         *(REPO_ROOT / "research" / "calibration" / "records" / "queue-specs").rglob("*.json")]
    ):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or "jobs_dir" not in payload:
            continue
        model = ExperimentMatrix if isinstance(payload.get("runs"), list) else ExperimentSpec
        model.model_validate(payload)  # raises with the offending path in the report
        checked += 1
    # 17 committed documents today (12 specs + 5 matrices), every one `runs`. The
    # floor guards against the glob silently matching nothing; a new spec raises it.
    assert checked >= 17, f"expected the committed corpus, validated only {checked}"


# ---------------------------------------------------------------------------
# What discovery reports
# ---------------------------------------------------------------------------


def write_job(job_dir: Path, *, trials=("t1",)) -> Path:
    """A minimally complete Harbor job directory.

    ``result.json`` exists at *both* job and trial level in Harbor's real output
    (job roll-up vs trial verdict), which is why neither reader may use its mere
    presence to decide what a directory is.
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "n_total_trials": len(trials),
                "stats": {"mean_reward": 1.0},
                "finished_at": "2026-08-16T00:00:00+00:00",
            }
        )
    )
    for trial in trials:
        trial_dir = job_dir / trial
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "result.json").write_text(
            json.dumps({"task_name": "event-summary", "trial_name": trial, "reward": 1.0})
        )
    return job_dir


def test_an_abandoned_transient_attempt_is_not_a_completed_job(tmp_path):
    """One real job with two retries was reported as three completed jobs.

    ``queue.py:1053`` archives a transient attempt by *moving* the whole job
    directory — job-level ``result.json`` included — to
    ``.transient-attempts/<name>/attempt-<n>``. Depth-agnostic discovery then
    counted each archived attempt as a finished job, inflating ``evallab
    status``, the consumption ledger (``quota.py:717``), and every cohort built
    from these roots.
    """
    runs = tmp_path / "runs"
    live = write_job(runs / "nightly-codex")
    write_job(runs / ".transient-attempts" / "nightly-codex" / "attempt-1")
    write_job(runs / ".transient-attempts" / "nightly-codex" / "attempt-2")

    assert discover_job_dirs([runs]) == [live.resolve()]


def test_executor_and_regrade_bookkeeping_are_not_reported_as_jobs(tmp_path):
    """Both writers of job-shaped bookkeeping are excluded.

    ``runner.py:540`` keeps executor logs under ``.executor``; Harbor 0.21.0
    caches a regrade source as a *complete* job under
    ``.sources/<uuid>/<job>`` (``trial/regrade.py:175``). ``explorer.py``
    already skipped dot-prefixed directories; this is the same rule on the other
    discovery path.
    """
    runs = tmp_path / "runs"
    live = write_job(runs / "nightly-oracle")
    write_job(runs / ".sources" / "0f3a-uuid" / "regraded-nightly-oracle")
    (runs / ".executor").mkdir(parents=True, exist_ok=True)
    (runs / ".executor" / "nightly-oracle.log").write_text("dispatched\n")

    assert discover_job_dirs([runs]) == [live.resolve()]


def test_an_explicitly_named_root_is_still_honoured(tmp_path):
    """Naming a path is a request, not a discovery.

    An operator who points at an archived attempt on purpose still gets it —
    the same distinction ``harbor view <dir>`` makes. Filtering here would make
    a retried attempt unreadable rather than merely uncounted.
    """
    archived = write_job(
        tmp_path / "runs" / ".transient-attempts" / "nightly-codex" / "attempt-1"
    )
    assert discover_job_dirs([archived]) == [archived.resolve()]


def test_a_job_nested_below_a_jobs_root_is_still_found_when_it_exists(tmp_path):
    """Discovery stays depth-agnostic for real output.

    The schema now refuses a *new* nested spec, but it cannot retro-fix a
    directory already written that way. This reader is the one surface that
    still finds such a run, and it must keep doing so.
    """
    runs = tmp_path / "runs"
    stranded = write_job(runs / "m009" / "jobs" / "event-summary-oracle")
    assert discover_job_dirs([runs]) == [stranded.resolve()]
