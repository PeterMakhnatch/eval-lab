"""Focused cover for `evallab approve-all-waiting`.

The command is the only path that authorises more than one billable spec at
once, so these tests pin its firewall: a named actor, an explicit yes (a real
terminal prompt or `--yes`), a per-id re-check against `waiting/` before each
authorisation, and the untouched single-spec `DirectoryQueue.approve` call
underneath.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import run_cli
from evallab.queue import DirectoryQueue, load_events, new_ulid
from evallab.schemas import ExperimentSpec, PolicyDecision, QueueEvent


class _FakeTty(io.StringIO):
    """stdin that reads a scripted answer but claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def make_queue(tmp_path: Path) -> DirectoryQueue:
    return DirectoryQueue(tmp_path / "queue")


def write_waiting_spec(
    queue: DirectoryQueue, name: str, **overrides: object
) -> ExperimentSpec:
    fields: dict[str, Any] = {
        "name": name,
        "hypothesis": "batch approval cover",
        "purpose": "practice",
        "task": "canary/event-summary",
        "agent": "codex",
        "model": "gpt-5.6",
        "attempts": 2,
        "est_cost_usd": 1.5,
        "submitted_by": "test-agent",
    }
    fields.update(overrides)
    spec = ExperimentSpec(**fields).model_copy(update={"spec_id": new_ulid()})
    path = queue.state_dir("waiting") / f"{spec.agent}-{spec.spec_id}.json"
    path.write_text(
        json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2) + "\n"
    )
    return spec


def approved_events(queue: DirectoryQueue) -> list[QueueEvent]:
    return [
        event
        for event in load_events(queue.events_path)
        if event.event == "human_approved"
    ]


def test_confirmed_batch_approves_every_waiting_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = make_queue(tmp_path)
    spec_a = write_waiting_spec(queue, "batch-a")
    spec_b = write_waiting_spec(queue, "batch-b", attempts=3, est_cost_usd=2.0)
    queue.write_reason(
        spec_a,
        PolicyDecision(
            admitted=False,
            reason_code="paid_run_unauthorized",
            message="codex is a billable agent; a named human must authorise it",
        ),
    )

    monkeypatch.setattr("sys.stdin", _FakeTty("y\n"))
    code = run_cli(
        ["approve-all-waiting", "--actor", "peter-test"], workspace=tmp_path
    )

    out = capsys.readouterr().out
    assert code == 0
    # Rows: id, name, task, agent/model, attempts, est cost, reason.
    assert f"{spec_a.spec_id}  batch-a  task=canary/event-summary" in out
    assert "agent=codex/gpt-5.6  attempts=2  est=$1.50  reason=paid_run_unauthorized" in out
    assert "agent=codex/gpt-5.6  attempts=3  est=$2.00  reason=unspecified" in out
    assert "2 waiting spec(s), estimated $3.50 total:" in out
    assert f"approved: {spec_a.spec_id}" in out
    assert f"approved: {spec_b.spec_id}" in out
    assert "done: 2 approved, 0 skipped, 0 failed" in out
    assert "next: uv run evallab tick" in out
    # Both specs physically moved to approved/ by the per-spec approval path.
    assert queue.list_specs("waiting") == []
    assert {path.name for path in queue.state_dir("approved").glob("*.json")} == {
        f"codex-{spec_a.spec_id}.json",
        f"codex-{spec_b.spec_id}.json",
    }
    grants = approved_events(queue)
    assert {str(event.spec_id) for event in grants} == {
        str(spec_a.spec_id),
        str(spec_b.spec_id),
    }
    assert {event.actor for event in grants} == {"peter-test"}
    assert {event.policy_rule for event in grants} == {"human-approval"}


def test_noninteractive_run_requires_yes_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = make_queue(tmp_path)
    spec = write_waiting_spec(queue, "batch-a")
    # A piped "y" is not consent: only a real terminal prompt or --yes counts.
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))

    code = run_cli(["approve-all-waiting", "--actor", "peter-test"], workspace=tmp_path)

    assert code == 2
    assert "explicit yes" in capsys.readouterr().err
    assert queue.locate(str(spec.spec_id), ("waiting",)).is_file()
    assert approved_events(queue) == []


def test_yes_flag_confirms_noninteractively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = make_queue(tmp_path)
    spec = write_waiting_spec(queue, "batch-a")

    code = run_cli(
        ["approve-all-waiting", "--actor", "peter-test", "--yes"], workspace=tmp_path
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"approved: {spec.spec_id}" in out
    assert queue.locate(str(spec.spec_id), ("approved",)).is_file()


def test_declined_prompt_approves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = make_queue(tmp_path)
    spec = write_waiting_spec(queue, "batch-a")
    monkeypatch.setattr("sys.stdin", _FakeTty("n\n"))

    code = run_cli(
        ["approve-all-waiting", "--actor", "peter-test"], workspace=tmp_path
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "aborted: nothing approved" in out
    assert queue.locate(str(spec.spec_id), ("waiting",)).is_file()
    assert approved_events(queue) == []


def test_spec_claimed_after_snapshot_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = make_queue(tmp_path)
    write_waiting_spec(queue, "batch-a")
    write_waiting_spec(queue, "batch-b")
    real_list_specs = DirectoryQueue.list_specs
    claimed: list[str] = []

    def list_specs_claiming_one(
        self: DirectoryQueue, state: Any
    ) -> list[tuple[Path, ExperimentSpec]]:
        records = real_list_specs(self, state)
        if state == "waiting":
            # Simulate a concurrent tick claiming a spec after this command
            # snapshotted waiting/: the snapshot row has already gone stale.
            stale_path, stale_spec = records[0]
            claimed.append(str(stale_spec.spec_id))
            stale_path.rename(self.state_dir("approved") / stale_path.name)
        return records

    monkeypatch.setattr(DirectoryQueue, "list_specs", list_specs_claiming_one)

    code = run_cli(
        ["approve-all-waiting", "--actor", "peter-test", "--yes"], workspace=tmp_path
    )

    out = capsys.readouterr().out
    assert code == 0
    assert len(claimed) == 1  # the claiming hook fired for exactly one snapshot row
    assert f"skipped: {claimed[0]} (no longer waiting)" in out
    assert "done: 1 approved, 1 skipped, 0 failed" in out
    # Only the still-waiting spec was authorised, exactly once.
    grants = approved_events(queue)
    assert len(grants) == 1
    assert str(grants[0].spec_id) != claimed[0]


def test_actor_is_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_queue(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run_cli(["approve-all-waiting", "--yes"], workspace=tmp_path)

    assert exit_info.value.code == 2
    assert "--actor" in capsys.readouterr().err
    assert approved_events(make_queue(tmp_path)) == []


def test_empty_queue_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_queue(tmp_path)

    code = run_cli(
        ["approve-all-waiting", "--actor", "peter-test", "--yes"], workspace=tmp_path
    )

    assert code == 0
    assert "nothing waiting" in capsys.readouterr().out
    assert approved_events(make_queue(tmp_path)) == []
