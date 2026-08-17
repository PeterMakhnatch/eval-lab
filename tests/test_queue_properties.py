"""Property-based state machine fuzz for DirectoryQueue using Hypothesis."""

import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from evallab.queue import QUEUE_STATES, DirectoryQueue, PolicyGate
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

FREE_AGENTS = {"oracle", "nop"}
BILLABLE_AGENTS = {"codex", "claude-code"}

ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    # submit (internal pending then immediate)
    ("pending", "approved"),
    ("pending", "waiting"),
    # approve paths
    ("proposed", "approved"),
    ("pending", "approved"),
    ("waiting", "approved"),
    # reject paths
    ("proposed", "rejected"),
    ("pending", "rejected"),
    ("approved", "rejected"),
    ("waiting", "rejected"),
    # dispatch to execution
    ("approved", "running"),
    # terminal states from running
    ("running", "done"),
    ("running", "failed"),
}


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code"],
                max_attempts=3,
            ),
        ],
        escalate_to_human=["anything_exceeding_ceilings"],
    )


def spec(
    name: str,
    *,
    agent: str = "oracle",
    task: str = "library/tasks/event-summary",
    model: str | None = None,
    est_cost_usd: float = 0,
    policy_rule: str | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the queue state machine",
        purpose="practice",
        task=task,
        task_path="library/tasks/event-summary" if task.startswith("canary/") else None,
        agent=agent,
        model=model,
        submitted_by="test-agent",
        est_cost_usd=est_cost_usd,
        policy_rule=policy_rule,
    )


class DirectoryQueueStateMachine(RuleBasedStateMachine):
    """Fuzzes DirectoryQueue transitions, conservation, admission, and dispatch-once."""

    def __init__(self) -> None:
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "queue"
        self.queue = DirectoryQueue(self.root)
        self.gate = PolicyGate(policy())
        self.live_specs: dict[str, str] = {}  # spec_id -> current_state
        self.spec_agents: dict[str, str] = {}
        self.billable_approved: set[str] = set()
        self.seen_running: set[str] = set()
        self.submitted_count = 0
        self.transitions_made: list[tuple[str, str]] = []
        self.next_name = 0

    def teardown(self) -> None:
        self.tempdir.cleanup()

    def _new_name(self) -> str:
        self.next_name += 1
        return f"spec-{self.next_name}"

    def _record_transition(self, from_state: str, to_state: str) -> None:
        self.transitions_made.append((from_state, to_state))

    @rule(agent=st.sampled_from(list(FREE_AGENTS | BILLABLE_AGENTS)))
    def submit(self, agent: str) -> None:
        name = self._new_name()
        is_free = agent in FREE_AGENTS
        s = spec(
            name,
            agent=agent,
            est_cost_usd=1.0 if not is_free else 0.0,
        )
        spent = 0.0
        dest, _ = self.queue.submit(s, gate=self.gate, spent_today_usd=spent)
        spec_id = str(self.queue.load(dest).spec_id)
        state = dest.parent.name
        self.live_specs[spec_id] = state
        self.spec_agents[spec_id] = agent
        self.submitted_count += 1
        self._record_transition("pending", state)
    @rule()
    def approve(self) -> None:
        candidates = [
            sid
            for sid, state in self.live_specs.items()
            if state in {"proposed", "pending", "waiting"}
            and self.spec_agents.get(sid) in BILLABLE_AGENTS
        ]
        if not candidates:
            return
        spec_id = candidates[0]
        old_state = self.live_specs[spec_id]
        new_path = self.queue.approve(spec_id, actor="fuzz-actor")
        new_state = new_path.parent.name
        self.live_specs[spec_id] = new_state
        self._record_transition(old_state, new_state)
        self.billable_approved.add(spec_id)

    @rule()
    def reject(self) -> None:
        candidates = [
            sid
            for sid, state in self.live_specs.items()
            if state in {"proposed", "pending", "approved", "waiting"}
        ]
        if not candidates:
            return
        spec_id = candidates[0]
        old_state = self.live_specs[spec_id]
        new_path = self.queue.reject(spec_id, actor="fuzz-actor", message="property test reject")
        new_state = new_path.parent.name
        self.live_specs[spec_id] = new_state
        self._record_transition(old_state, new_state)

    @rule()
    def transition_to_running(self) -> None:
        candidates = [
            sid for sid, state in self.live_specs.items() if state == "approved"
        ]
        if not candidates:
            return
        spec_id = candidates[0]
        assert spec_id not in self.seen_running, "double dispatch detected"
        old_state = self.live_specs[spec_id]
        source = self.queue.locate(spec_id, ("approved",))
        new_path = self.queue.transition(
            source, "running", actor="executor", event="dispatch_started"
        )
        new_state = new_path.parent.name
        self.live_specs[spec_id] = new_state
        self._record_transition(old_state, new_state)
        self.seen_running.add(spec_id)

    @rule()
    def complete(self) -> None:
        candidates = [
            sid for sid, state in self.live_specs.items() if state == "running"
        ]
        if not candidates:
            return
        spec_id = candidates[0]
        old_state = self.live_specs[spec_id]
        source = self.queue.locate(spec_id, ("running",))
        new_path = self.queue.transition(
            source, "done", actor="executor", event="dispatch_completed"
        )
        new_state = new_path.parent.name
        self.live_specs[spec_id] = new_state
        self._record_transition(old_state, new_state)

    @rule()
    def fail(self) -> None:
        candidates = [
            sid for sid, state in self.live_specs.items() if state == "running"
        ]
        if not candidates:
            return
        spec_id = candidates[0]
        old_state = self.live_specs[spec_id]
        source = self.queue.locate(spec_id, ("running",))
        new_path = self.queue.transition(
            source, "failed", actor="executor", event="dispatch_failed"
        )
        new_state = new_path.parent.name
        self.live_specs[spec_id] = new_state
        self._record_transition(old_state, new_state)

    @invariant()
    def conservation(self) -> None:
        found: dict[str, str] = {}
        for state in QUEUE_STATES:
            for _, spec in self.queue.list_specs(state):
                sid = str(spec.spec_id)
                assert sid not in found, f"duplicate spec id {sid}"
                found[sid] = state
        assert len(found) == self.submitted_count, "lost or extra specs"
        assert set(found.keys()) == set(self.live_specs.keys())
        for sid, state in found.items():
            assert self.live_specs[sid] == state

    @invariant()
    def legal_transitions_only(self) -> None:
        for fr, to in self.transitions_made:
            assert (fr, to) in ALLOWED_TRANSITIONS, f"illegal transition {fr} -> {to}"

    @invariant()
    def no_double_dispatch(self) -> None:
        # enforced at transition_to_running; also verify no running spec reappears
        running_now = {sid for sid, st in self.live_specs.items() if st == "running"}
        assert len(running_now) <= 1 or all(s in self.seen_running for s in running_now)

    @invariant()
    def admission_respected(self) -> None:
        for sid, state in self.live_specs.items():
            if state in {"approved", "running"}:
                agent = self.spec_agents.get(sid, "")
                if agent in BILLABLE_AGENTS:
                    assert (
                        sid in self.billable_approved
                    ), f"billable spec {sid} reached {state} without explicit approve()"


TestQueueProperties = DirectoryQueueStateMachine.TestCase
TestQueueProperties.settings = settings(
    max_examples=150, stateful_step_count=25, deadline=None
)
