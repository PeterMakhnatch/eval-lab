"""Contract tests for `evallab preflight` (WS-E item 2).

Every external probe is injected: job directories and queues are built under
``tmp_path``, and the instant is passed in, so nothing here depends on
``~/.codex``, the Keychain, Docker, the network, the database, or the wall
clock (`agents/CHECKS.md`).

The defects these tests exist to prevent, in order of how expensive they would
be:

1. **An unavailable reading rendering as plenty.** This is the original defect
   `docs/quota-accounting.md` was written to close, reproduced in a new unit.
   `test_unavailable_headroom_never_renders_as_plenty` asserts that no number
   at all reaches the operator when the allowance could not be measured, and
   that the sentence saying so does reach them.
2. **A stale reading passing as current.** The reading exists only because a
   paid trial recorded it, so it can be arbitrarily old.
3. **A missing field being filled in with a guess.** `ExperimentSpec.purpose`
   is WS-E item 1 and may not exist. Both shapes are covered.
4. **A power warning manufactured out of nothing**, and its opposite: silence
   about a comparison that genuinely cannot reach an interval.
5. **A surface that costs money or blocks.** `test_the_render_path_makes_no_
   subprocess_or_network_call` makes both impossible and then renders anyway.
"""

from __future__ import annotations

import argparse
import ast
import json
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

import evallab.digest as digest_module
import evallab.preflight as preflight_module
from evallab.cli import _preflight_command, run_cli
from evallab.digest import DigestRenderer
from evallab.preflight import (
    PURPOSE_UNAVAILABLE,
    build_preflight_report,
    digest_section,
    preflight_at_tick_start,
    render_preflight,
    survey_queue,
)
from evallab.queue import DirectoryQueue, provider_reported_exhaustion
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

ROOT = Path(__file__).resolve().parents[1]

#: Fixed instant. Every staleness assertion is a difference against this.
NOW = datetime(2026, 8, 16, 18, 0, 0, tzinfo=UTC)

#: 2026-08-20T18:32:49Z, the reset the committed evidence actually reports.
RESETS_AT_EPOCH = 1_787_250_769


class SpecWithoutPurpose(BaseModel):
    """`ExperimentSpec` before WS-E item 1 made `purpose` required."""

    schema_version: int = 1
    spec_id: str | None = None
    name: str = "unnamed"
    hypothesis: str = ""
    task: str = "task"
    agent: str = "codex"
    submitted_by: str = "tester"
    attempts: int = 1
    expected_reward: float | None = None
    policy_rule: str | None = None

    @property
    def billable(self) -> bool:
        return self.agent not in {"oracle", "nop"}

SpecWithPurpose = ExperimentSpec

def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_paid_trial(
    root: Path,
    *,
    agent: str = "codex",
    job_name: str = "canary-event-summary-codex-20260816",
    trial_name: str = "event-summary__1",
) -> Path:
    """One completed paid trial, the only thing that can carry a quota reading."""
    job = root / "runs" / job_name
    write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-0000000000ff",
            "started_at": "2026-08-16T05:00:00Z",
            "finished_at": "2026-08-16T06:00:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1},
        },
    )
    write_json(job / "lab-metadata.json", {"command": ["harbor", "run", "--agent", agent]})
    trial = job / trial_name
    write_json(
        trial / "result.json",
        {
            "id": f"trial-{trial_name}",
            "trial_name": trial_name,
            "task_name": "canary/event-summary",
            "started_at": "2026-08-16T05:30:00Z",
            "finished_at": "2026-08-16T05:40:00Z",
            "agent_info": {"name": agent, "model_info": {"name": "gpt-5.6-terra"}},
            "agent_result": {
                "n_input_tokens": 1_000,
                "n_cache_tokens": 800,
                "n_output_tokens": 20,
            },
        },
    )
    return trial


def add_quota_snapshot(
    trial: Path,
    *,
    observed_at: datetime,
    used_percent: float | None = 92.0,
    has_credits: bool = False,
    unlimited: bool = False,
    balance: str = "0",
    resets_at: int | None = RESETS_AT_EPOCH,
    rate_limit_reached_type: str | None = None,
) -> None:
    """The `rate_limits` block the Codex CLI attaches to a `token_count` event."""
    event = {
        "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1_000, "output_tokens": 20}},
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10_080,
                    "resets_at": resets_at,
                },
                "secondary": None,
                "credits": {
                    "has_credits": has_credits,
                    "unlimited": unlimited,
                    "balance": balance,
                },
                "plan_type": "prolite",
                "rate_limit_reached_type": rate_limit_reached_type,
            },
        },
    }
    rollout = trial / "agent/sessions/2026/08/16/rollout-2026-08-16T05-30-00-abc.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps(event) + "\n")


def queue_spec(
    queue_root: Path,
    *,
    state: str = "approved",
    name: str,
    task: str = "canary/event-summary",
    agent: str = "codex",
    attempts: int = 1,
    expected_reward: float | None = None,
    purpose: str | None = "drift",
) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "spec_id": name.upper().replace("-", ""),
        "name": name,
        "hypothesis": "a hypothesis",
        "task": task,
        "agent": agent,
        "attempts": attempts,
        "submitted_by": "tester",
    }
    if expected_reward is not None:
        payload["expected_reward"] = expected_reward
    if purpose is not None:
        payload["purpose"] = purpose
    path = queue_root / state / f"{agent}-{name}.json"
    write_json(path, payload)
    return path


# --- trap one: unavailable is not plenty ---------------------------------


def test_unavailable_headroom_never_renders_as_plenty(tmp_path: Path) -> None:
    """No number, and an explicit warning, when the allowance cannot be read.

    A blank or a bare "0 snapshots" would let a reader supply their own
    optimism. `availability` is checked before `remaining_percent` is touched,
    so `remaining_percent` (which is `None` here) can never be formatted at all.
    """
    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)
    codex = next(provider for provider in report.providers if provider.agent == "codex")

    assert codex.observed is False
    assert codex.headroom.remaining_percent is None
    assert codex.refusal is None, "an unmeasured allowance is not evidence of exhaustion"

    rendered = render_preflight(report)
    assert "UNKNOWN [unavailable]" in rendered
    assert "UNKNOWN is not 'plenty left'" in rendered
    assert "remaining_percent" not in rendered
    assert "used_percent" not in rendered
    assert "VERDICT: nothing in these readings refuses billable work" in rendered
    assert "not the same as headroom being confirmed" in rendered


def test_a_failed_quota_scan_is_an_unavailable_reading_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise OSError("runs/ is not readable")

    monkeypatch.setattr(preflight_module, "load_quota_report", explode)
    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)

    codex = next(provider for provider in report.providers if provider.agent == "codex")
    assert codex.observed is False
    assert codex.headroom.reason == "the quota scan failed (OSError: runs/ is not readable)"
    assert "UNKNOWN is not 'plenty left'" in render_preflight(report)


# --- trap two: staleness ---------------------------------------------------


def test_a_stale_reading_shows_its_age(tmp_path: Path) -> None:
    """Age is printed because the reader, not this surface, judges freshness."""
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(hours=11, minutes=53, seconds=30))

    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)
    codex = next(provider for provider in report.providers if provider.agent == "codex")

    assert codex.observed is True
    assert codex.headroom.staleness_seconds == pytest.approx(11 * 3600 + 53 * 60 + 30)

    rendered = render_preflight(report)
    assert "staleness                11h53m old" in rendered
    assert "observed_at              2026-08-16T06:06:30+00:00" in rendered
    assert "used_percent             92.0 [observed]" in rendered
    assert "remaining_percent        8.0 [observed]" in rendered


def test_exhausted_credits_are_reported_as_a_lockout_not_a_charge(tmp_path: Path) -> None:
    """`credits.balance` 0 with neither credits nor unlimited is a hard stop."""
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(minutes=5))

    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)
    codex = next(provider for provider in report.providers if provider.agent == "codex")

    assert codex.headroom.hard_stop is True
    assert report.hard_stopped() == ("codex",)

    rendered = render_preflight(report)
    assert "hard stop                True" in rendered
    assert "no overflow credits: reaching 100% blocks every paid agent" in rendered
    assert "credits_balance          0" in rendered
    assert "is a lockout until it resets, not an extra charge" in rendered


def test_one_provider_reading_is_never_attributed_to_the_other(tmp_path: Path) -> None:
    """`codex` and `claude-code` are separate subscriptions, so separate readings."""
    trial = make_paid_trial(tmp_path, agent="codex")
    add_quota_snapshot(trial, observed_at=NOW - timedelta(minutes=5))

    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)
    by_agent = {provider.agent: provider for provider in report.providers}

    assert by_agent["codex"].observed is True
    assert by_agent["codex"].paid_trials == 1
    assert by_agent["claude-code"].observed is False
    assert by_agent["claude-code"].paid_trials == 0
    assert by_agent["claude-code"].headroom.remaining_percent is None


def test_the_providers_own_exhaustion_statement_is_surfaced(tmp_path: Path) -> None:
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(
        trial,
        observed_at=NOW - timedelta(minutes=5),
        used_percent=100.0,
        rate_limit_reached_type="primary",
    )

    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)

    assert report.refusals() == (
        "codex: the provider reports rate_limit_reached_type 'primary' on limit codex",
    )
    assert "VERDICT: billable work would be refused" in render_preflight(report)


def test_a_lab_ceiling_is_labelled_as_lab_policy_not_the_provider(tmp_path: Path) -> None:
    """#70: a lab ceiling carries its own reason code, never the provider's."""
    unset = render_preflight(build_preflight_report(tmp_path, now=NOW))
    assert "lab refusal ceiling      unset, so no lab ceiling refuses anything" in unset

    configured = render_preflight(
        build_preflight_report(tmp_path, now=NOW, refuse_at_used_percent=80.0)
    )
    assert "lab refusal ceiling      80.0 percent used" in configured
    assert "reason code subscription_quota_ceiling" in configured
    assert "never as the provider's statement" in configured


# --- the queue, grouped by purpose -----------------------------------------


def test_purpose_absent_is_reported_as_absent_and_never_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field does not exist in this build, and the surface says exactly that."""
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithoutPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="baseline-run", state="approved", purpose=None)
    queue_spec(queue_root, name="second-run", state="waiting", attempts=3, purpose=None)

    survey = survey_queue(queue_root)

    assert survey.purpose_available is False
    assert set(survey.groups) == {PURPOSE_UNAVAILABLE}
    assert survey.total == 2
    assert survey.comparisons() == ()

    rendered = render_preflight(
        build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)
    )
    assert "purpose not declared [unavailable]: 2 spec(s), 2 billable" in rendered
    assert "ExperimentSpec has no `purpose` field in this build" in rendered
    # No bucket was invented for specs that cannot declare one.
    assert "purpose baseline" not in rendered
    assert "purpose comparison" not in rendered
def test_purpose_present_groups_by_declared_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="base-a", purpose="baseline")
    queue_spec(queue_root, name="base-b", purpose="baseline", state="waiting")
    queue_spec(queue_root, name="cmp-a", purpose="comparison", task="t/one")

    survey = survey_queue(queue_root)

    assert survey.purpose_available is True
    assert set(survey.groups) == {"baseline", "comparison"}
    assert len(survey.groups["baseline"]) == 2
    assert [view.name for view in survey.comparisons()] == ["cmp-a"]

    rendered = render_preflight(
        build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)
    )
    assert "purpose baseline: 2 spec(s), 2 billable" in rendered
    assert "purpose comparison: 1 spec(s), 1 billable" in rendered
    assert "ExperimentSpec has no `purpose` field" not in rendered


def test_a_spec_missing_a_now_required_purpose_is_named_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact state WS-E item 1 creates: specs queued before it landed.

    Silently dropping them would under-report the queue at the moment an
    operator most needs to see it, and raising would take the surface down.
    """
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="legacy-run", purpose=None)
    queue_spec(queue_root, name="modern-run", purpose="drift")
    survey = survey_queue(queue_root)
    assert "purpose" in (survey.unreadable[0].error or "")

    rendered = render_preflight(
        build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)
    )
    assert "unreadable: 1 spec file(s) this build cannot parse" in rendered
    assert "codex-legacy-run.json" in rendered


def test_surveying_a_missing_queue_creates_nothing(tmp_path: Path) -> None:
    """A read-only surface must not bring a queue into existence by reporting it."""
    queue_root = tmp_path / "queue"

    survey = survey_queue(queue_root)

    assert survey.present is False
    assert queue_root.exists() is False
    assert "no queue directory at" in render_preflight(
        build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)
    )


def test_finished_states_are_not_counted_as_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="live-run", purpose="baseline", state="approved")
    queue_spec(queue_root, name="old-run", purpose="comparison", state="done")
    queue_spec(queue_root, name="dead-run", purpose="comparison", state="failed")

    survey = survey_queue(queue_root)

    assert survey.total == 1
    assert survey.comparisons() == ()


# --- power warnings --------------------------------------------------------


def test_no_queued_comparison_produces_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="base-a", purpose="baseline")

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.evaluated is False
    assert report.power.warnings == ()
    assert "none: no comparison is queued, so no power warning applies" in render_preflight(
        report
    )


def test_no_purpose_field_asserts_no_power_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of the field is not evidence that a comparison is under-powered."""
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithoutPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(queue_root, name="base-a", purpose=None)

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.evaluated is False
    assert report.power.warnings == ()
    assert "`ExperimentSpec.purpose` does not exist in this build" in render_preflight(report)

def test_a_one_task_comparison_cannot_reach_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(
        queue_root, name="cmp-a", purpose="comparison", task="t/one", expected_reward=0.4
    )
    queue_spec(
        queue_root,
        name="cmp-b",
        purpose="comparison",
        task="t/one",
        expected_reward=0.6,
        state="waiting",
    )

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.evaluated is True
    assert report.power.n_tasks == 1
    assert any("a task-paired interval needs at least two" in w for w in report.power.warnings)
    assert "WARNING:" in render_preflight(report)


def test_a_comparison_without_a_declared_baseline_warns_before_the_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    for index in range(4):
        queue_spec(queue_root, name=f"cmp-{index}", purpose="comparison", task=f"t/{index}")

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.baseline is None
    assert report.power.minimum_detectable_effect is None
    assert any("`expected_reward`" in warning for warning in report.power.warnings)


def test_an_adequately_powered_comparison_raises_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    for index in range(200):
        queue_spec(
            queue_root,
            name=f"cmp-{index:03d}",
            purpose="comparison",
            task=f"t/{index}",
            attempts=4,
            expected_reward=0.5,
        )

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.n_tasks == 200
    assert report.power.k == 4
    assert report.power.baseline == pytest.approx(0.5)
    assert report.power.minimum_detectable_effect is not None
    assert report.power.warnings == ()
    assert "no warning: the queued comparison reaches an interval" in render_preflight(report)


def test_useful_effect_is_only_a_warning_when_an_operator_supplies_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Useful" is a spend judgement, so no number is committed for it.

    30 paired tasks at k=1 is a real comparison with a real interval — and a
    smallest detectable per-attempt difference of about 0.32, which is enormous.
    Nothing warns about that by default, because whether 0.32 is good enough is
    the Sponsor's call, not this module's.
    """
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    for index in range(30):
        queue_spec(
            queue_root,
            name=f"cmp-{index:02d}",
            purpose="comparison",
            task=f"t/{index}",
            expected_reward=0.5,
        )

    unset = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)
    assert unset.power.minimum_detectable_effect == pytest.approx(0.322, abs=0.01)
    assert unset.power.warnings == ()

    supplied = build_preflight_report(
        tmp_path, now=NOW, queue_root=queue_root, useful_effect=0.01
    )
    assert any("larger than the 0.0100 supplied as useful" in w for w in supplied.power.warnings)


def test_the_weakest_arm_binds_the_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "ExperimentSpec", SpecWithPurpose)
    queue_root = tmp_path / "queue"
    queue_spec(
        queue_root, name="cmp-a", purpose="comparison", task="t/a", attempts=8, expected_reward=0.5
    )
    queue_spec(
        queue_root, name="cmp-b", purpose="comparison", task="t/b", attempts=2, expected_reward=0.5
    )

    report = build_preflight_report(tmp_path, now=NOW, queue_root=queue_root)

    assert report.power.k == 2
    assert "Pooling can only overstate n_tasks" in render_preflight(report)


# --- costs nothing, blocks on nothing --------------------------------------


def test_the_render_path_makes_no_subprocess_or_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#69's property, extended to cover the block this mission added.

    Both a paid call and a provider request would go through one of these, and
    `agents/CHECKS.md` forbids a test that depends on a developer's credentials
    or network. Making them impossible and then rendering anyway is the proof.
    """

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the preflight render path must not leave the process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(hours=2))
    queue_spec(tmp_path / "queue", name="base-a")

    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)
    assert "used_percent             92.0 [observed]" in render_preflight(report)

    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: report,
    )
    assert "## Preflight" in renderer.write(report_date=NOW.date()).read_text()


def test_preflight_never_imports_the_queue_at_module_scope() -> None:
    """The tick wiring is one line only while this stays true.

    `Executor._tick_locked` calls into this module, so a module-level
    `from evallab.queue import ...` here would be a circular import. The default
    refusal reader is resolved by a deferred import for exactly this reason.
    """
    tree = ast.parse((ROOT / "src/evallab/preflight.py").read_text())
    module_level = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("evallab")
    ]
    imported = {node.module for node in module_level}

    assert "evallab.queue" not in imported
    assert imported == {"evallab.cohort", "evallab.quota", "evallab.schemas"}


def test_the_default_refusal_reader_is_the_dispatch_gates_own(tmp_path: Path) -> None:
    """No caller has to remember to pass it, and nothing restates its logic."""
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(
        trial,
        observed_at=NOW - timedelta(minutes=5),
        used_percent=100.0,
        rate_limit_reached_type="primary",
    )

    report = build_preflight_report(tmp_path, now=NOW)

    assert report.refusals() == (
        "codex: the provider reports rate_limit_reached_type 'primary' on limit codex",
    )


# --- the tick-start entry point and the digest -----------------------------


def test_preflight_at_tick_start_emits_and_returns(tmp_path: Path) -> None:
    """The exact call another mission adds to `Executor._tick_locked`."""
    emitted: list[str] = []

    report = preflight_at_tick_start(tmp_path, now=NOW, emit=emitted.append)

    assert len(emitted) == 1
    assert emitted[0] == render_preflight(report)
    assert emitted[0].startswith("evallab preflight —")


def test_the_digest_embeds_the_same_block_byte_for_byte(tmp_path: Path) -> None:
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(hours=3))
    report = build_preflight_report(tmp_path, now=NOW, refusal=provider_reported_exhaustion)

    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: report,
    )
    text = renderer.write(report_date=NOW.date()).read_text()

    assert "\n".join(digest_section(report)) in text
    assert render_preflight(report) in text
    assert "- Providers whose exhaustion is a lockout, not a charge: codex" in text


def test_a_broken_preflight_never_costs_the_digest_its_other_sections(
    tmp_path: Path,
) -> None:
    def explode() -> object:
        raise RuntimeError("quota roots vanished")

    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=explode,
    )
    text = renderer.write(report_date=NOW.date()).read_text()

    assert "Unavailable: the preflight could not be built (RuntimeError:" in text
    assert "That is not a statement that quota, queue, or power are fine." in text
    assert "## Completed trials" in text
    assert "## Queue" in text


def test_a_stale_queued_spec_never_takes_down_the_digest(tmp_path: Path) -> None:
    """WS-E item 1 turns every pre-existing waiting spec into an unparseable file."""
    queue_root = tmp_path / "queue"
    queue = DirectoryQueue(queue_root)
    (queue_root / "waiting" / "codex-legacy.json").write_text('{"schema_version": 1}')

    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: build_preflight_report(tmp_path, now=NOW),
    )
    text = renderer.write(report_date=NOW.date()).read_text()

    assert "- Waiting proposals: unreadable (ValueError:" in text
    assert "## Queue events" in text


def test_the_digest_reports_the_latest_recorded_refusal_not_the_first_filename(
    tmp_path: Path,
) -> None:
    """Reason files are ordered by `occurred_at`, not by their ULID suffix.

    `write_reason` names them `{spec_id}-{ULID}.json`. A ULID sorts lexically
    only down to its 48-bit millisecond; inside one millisecond the low 80 bits
    are random, so a reverse filename sort picks at random. Since #65 made two
    reasons per spec ordinary, the digest could print the superseded one. The
    filenames below are deliberately in the wrong order relative to the times
    they record, which is what a same-millisecond pair looks like.
    """
    queue = DirectoryQueue(tmp_path / "queue")
    spec_id = "01M00850MSD5QB6NEKRXSGAMVX"
    for suffix, occurred_at, code in (
        ("ZZZZZZZZZZZZZZZZZZZZZZZZZZ", "2026-08-16T10:00:00Z", "paid_run_unauthorized"),
        ("AAAAAAAAAAAAAAAAAAAAAAAAAA", "2026-08-16T10:00:01Z", "daily_cost_ceiling"),
    ):
        write_json(
            queue.reasons_dir / f"{spec_id}-{suffix}.json",
            {
                "spec_id": spec_id,
                "occurred_at": occurred_at,
                "code": code,
                "message": "recorded by the gate",
            },
        )
    queue_spec(tmp_path / "queue", state="waiting", name="canary-run")
    (tmp_path / "queue/waiting/codex-canary-run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_id": spec_id,
                "name": "canary-run",
                "hypothesis": "a hypothesis",
                "task": "canary/event-summary",
                "agent": "codex",
                "submitted_by": "tester",
                "purpose": "drift",
            }
        )
    )

    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: build_preflight_report(tmp_path, now=NOW),
    )
    text = renderer.write(report_date=NOW.date()).read_text()

    assert "| daily_cost_ceiling |" in text
    assert "paid_run_unauthorized" not in text


# --- the CLI surface -------------------------------------------------------


def test_the_cli_prints_the_surface_and_exits_zero_when_nothing_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy/standing-approvals.yaml").write_text(
        policy().model_dump_json(indent=2)
    )
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(trial, observed_at=NOW - timedelta(hours=1))

    assert run_cli(["preflight"], workspace=tmp_path) == 0

    printed = capsys.readouterr().out
    assert "PER-PROVIDER REMAINING QUOTA" in printed
    assert "QUEUE BY PURPOSE" in printed
    assert "POWER WARNINGS (queued comparisons only)" in printed
    assert "used_percent             92.0 [observed]" in printed


def test_the_cli_exits_non_zero_when_a_provider_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scriptable: `evallab preflight && evallab tick` stops before it spends.

    The snapshot is placed relative to the process clock rather than at a fixed
    instant, because `provider_reported_exhaustion` compares `resets_at` against
    the reading's own clock. Only the relationship is asserted, so the outcome
    is fixed forever; no host state is read.
    """
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy/standing-approvals.yaml").write_text(
        policy().model_dump_json(indent=2)
    )
    now = datetime.now(UTC)
    trial = make_paid_trial(tmp_path)
    add_quota_snapshot(
        trial,
        observed_at=now - timedelta(minutes=1),
        used_percent=100.0,
        resets_at=int((now + timedelta(days=2)).timestamp()),
    )

    arguments = argparse.Namespace(preflight_from=None, useful_effect=None)
    assert _preflight_command(arguments, tmp_path) == 1

    printed = capsys.readouterr().out
    assert "VERDICT: billable work would be refused" in printed
    assert "the provider reports used_percent 100.0 of the window" in printed


def test_the_operator_tick_prints_the_preflight_before_dispatch() -> None:
    """WS-E item 2 says the preflight runs at tick start; the CLI path does."""
    source = (ROOT / "src/evallab/cli.py").read_text()
    tick_branch = source.split('if args.command == "tick":', 1)[1]
    before_executor = tick_branch.split("executor = Executor.from_repo(root)", 1)[0]

    assert "render_preflight(" in before_executor
    assert "build_preflight_report(" in before_executor


def test_digest_module_reaches_the_preflight_through_an_injected_loader() -> None:
    """The renderer's one clock read stays behind a seam the tests can fix."""
    assert digest_module.DigestRenderer._load_preflight is not None
    renderer = DigestRenderer(
        repo_root=ROOT,
        queue=DirectoryQueue(ROOT / "queue"),
        policy=policy(),
        preflight_loader=lambda: build_preflight_report(ROOT, now=NOW),
    )
    assert renderer._preflight_loader() .generated_at == NOW
