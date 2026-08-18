"""Property-based state machine fuzz for DirectoryQueue and Executor using Hypothesis."""

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from evallab.credentials import CLAUDE_OAUTH, CODEX_AUTH
from evallab.queue import (
    QUEUE_STATES,
    DirectoryQueue,
    Executor,
    PolicyGate,
    load_events,
)
from evallab.runner import RunRequest
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
    # dispatch refusal at tick
    ("approved", "waiting"),
    # terminal states from running
    ("running", "done"),
    ("running", "failed"),
}


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=5,
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
    priority: int = 100,
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
        priority=priority,
    )


class DirectoryQueueStateMachine(RuleBasedStateMachine):
    """Fuzzes DirectoryQueue transitions, conservation, admission, credential deferral,
    vanished files, and mid-tick quota enforcement."""

    def __init__(self) -> None:
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.queue_root = self.root / "queue"
        self.queue = DirectoryQueue(self.queue_root)
        self.gate = PolicyGate(policy())

        self.current_spent: float = 0.0
        self.active_credentials: set[str] = {CLAUDE_OAUTH, CODEX_AUTH}
        self.dispatched_specs: list[str] = []

        def stub_runner(request: RunRequest) -> Path:
            self.dispatched_specs.append(request.name)
            destination = request.jobs_dir / request.name
            destination.mkdir(parents=True, exist_ok=True)
            return destination

        def stub_ingester(path: Path) -> None:
            # When run completes, record catalog spend
            for _sid, spec_obj in self.spec_objects.items():
                if spec_obj.name == path.name:
                    self.current_spent += spec_obj.est_cost_usd
                    break

        self.executor = Executor(
            repo_root=self.root,
            queue=self.queue,
            policy=policy(),
            runner=stub_runner,
            ingester=stub_ingester,
            spent_today=lambda: self.current_spent,
            consecutive_harness_failures=lambda: 0,
            credential_probe=lambda: frozenset(self.active_credentials),
            sleeper=lambda _s: None,
        )

        self.live_specs: dict[str, str] = {}  # spec_id -> current_state
        self.spec_agents: dict[str, str] = {}
        self.spec_costs: dict[str, float] = {}
        self.spec_objects: dict[str, ExperimentSpec] = {}
        self.billable_approved: set[str] = set()
        self.seen_running: set[str] = set()
        self.vanished_specs: set[str] = set()
        self.submitted_count = 0
        self.next_name = 0

    def teardown(self) -> None:
        self.tempdir.cleanup()

    def _new_name(self) -> str:
        self.next_name += 1
        return f"spec-{self.next_name}"

    @rule(
        agent=st.sampled_from(list(FREE_AGENTS | BILLABLE_AGENTS)),
        cost=st.floats(min_value=0.5, max_value=8.0),
        priority=st.integers(min_value=1, max_value=200),
    )
    def submit(self, agent: str, cost: float, priority: int) -> None:
        name = self._new_name()
        is_free = agent in FREE_AGENTS
        est_cost = 0.0 if is_free else round(cost, 2)
        s = spec(
            name,
            agent=agent,
            est_cost_usd=est_cost,
            priority=priority,
        )
        dest, _ = self.queue.submit(s, gate=self.gate, spent_today_usd=self.current_spent)
        spec_id = str(self.queue.load(dest).spec_id)
        state = dest.parent.name
        self.live_specs[spec_id] = state
        self.spec_agents[spec_id] = agent
        self.spec_costs[spec_id] = est_cost
        self.spec_objects[spec_id] = s
        self.submitted_count += 1

    @rule(pick=st.integers(min_value=0, max_value=64))
    def approve(self, pick: int) -> None:
        candidates = [
            sid
            for sid, state in self.live_specs.items()
            if state in {"proposed", "pending", "waiting"}
            and sid not in self.vanished_specs
            and self.spec_agents.get(sid) in BILLABLE_AGENTS
        ]
        if not candidates:
            return
        spec_id = sorted(candidates)[pick % len(candidates)]
        try:
            new_path = self.queue.approve(spec_id, actor="fuzz-actor")
            new_state = new_path.parent.name
            self.live_specs[spec_id] = new_state
            self.billable_approved.add(spec_id)
        except (ValueError, FileNotFoundError):
            pass

    @rule(pick=st.integers(min_value=0, max_value=64))
    def reject(self, pick: int) -> None:
        candidates = [
            sid
            for sid, state in self.live_specs.items()
            if state in {"proposed", "pending", "approved", "waiting"}
            and sid not in self.vanished_specs
        ]
        if not candidates:
            return
        spec_id = sorted(candidates)[pick % len(candidates)]
        try:
            new_path = self.queue.reject(
                spec_id, actor="fuzz-actor", message="property test reject"
            )
            new_state = new_path.parent.name
            self.live_specs[spec_id] = new_state
        except (ValueError, FileNotFoundError):
            pass

    @rule(
        has_claude=st.booleans(),
        has_codex=st.booleans(),
    )
    def tick(self, has_claude: bool, has_codex: bool) -> None:
        creds: set[str] = set()
        if has_claude:
            creds.add(CLAUDE_OAUTH)
        if has_codex:
            creds.add(CODEX_AUTH)
        self.active_credentials = creds

        self.executor.tick()

        # Update live_specs from actual queue state
        for state in QUEUE_STATES:
            for _path, s in self.queue.list_specs(state):
                sid = str(s.spec_id)
                if sid not in self.vanished_specs:
                    self.live_specs[sid] = state
                    if state in {"running", "done"}:
                        self.seen_running.add(sid)

    @rule(pick=st.integers(min_value=0, max_value=64))
    def vanish_file(self, pick: int) -> None:
        """Simulate unexpected removal / disappearance of a spec file."""
        candidates = [
            sid
            for sid, state in self.live_specs.items()
            if sid not in self.vanished_specs
            and state in {"proposed", "pending", "approved", "waiting"}
        ]
        if not candidates:
            return
        spec_id = sorted(candidates)[pick % len(candidates)]
        state = self.live_specs[spec_id]
        try:
            path = self.queue.locate(spec_id, (state,))  # type: ignore[arg-type]
            if path.is_file():
                path.unlink()
            self.vanished_specs.add(spec_id)
            del self.live_specs[spec_id]
        except (ValueError, FileNotFoundError):
            pass

    @invariant()
    def conservation(self) -> None:
        found: dict[str, str] = {}
        for state in QUEUE_STATES:
            for _, s in self.queue.list_specs(state):
                sid = str(s.spec_id)
                assert sid not in found, f"duplicate spec id {sid}"
                found[sid] = state
        # All non-vanished specs must be accounted for
        active_live = {
            sid: state
            for sid, state in self.live_specs.items()
            if sid not in self.vanished_specs
        }
        assert set(found.keys()) == set(active_live.keys()), (
            f"Mismatched specs: found={set(found.keys())} vs live={set(active_live.keys())}"
        )
        for sid, state in found.items():
            assert active_live[sid] == state

    def _recorded_transitions(self) -> list[tuple[str, tuple[str | None, str | None]]]:
        path = self.queue.events_path
        if not path.is_file():
            return []
        return [
            (event.spec_id, (event.from_state, event.to_state))
            for event in load_events(path)
            if event.to_state is not None
        ]

    @invariant()
    def legal_transitions_only(self) -> None:
        for spec_id, (source, destination) in self._recorded_transitions():
            if source is None:
                assert destination == "pending", (
                    f"spec {spec_id} entered queue at {destination}, not pending"
                )
                continue
            assert (source, destination) in ALLOWED_TRANSITIONS, (
                f"spec {spec_id} made an illegal transition {source} -> {destination}"
            )

    @invariant()
    def no_double_dispatch(self) -> None:
        dispatches: dict[str, int] = {}
        for spec_id, (_source, destination) in self._recorded_transitions():
            if destination == "running":
                dispatches[spec_id] = dispatches.get(spec_id, 0) + 1
        repeated = {sid: n for sid, n in dispatches.items() if n > 1}
        assert not repeated, f"specs dispatched more than once: {repeated}"

    @invariant()
    def admission_respected(self) -> None:
        for sid, state in self.live_specs.items():
            if state in {"running", "done"}:
                agent = self.spec_agents.get(sid, "")
                if agent in BILLABLE_AGENTS:
                    assert (
                        sid in self.billable_approved
                    ), f"billable spec {sid} reached {state} without explicit approve()"

    @invariant()
    def credential_deferral_preserves_approved(self) -> None:
        """Specs deferred due to missing credentials must remain in approved/."""
        path = self.queue.events_path
        if not path.is_file():
            return
        deferred_specs = {
            event.spec_id
            for event in load_events(path)
            if event.event == "dispatch_deferred"
        }
        for sid in deferred_specs:
            if sid in self.live_specs and sid not in self.vanished_specs:
                state = self.live_specs[sid]
                assert state in {"approved", "running", "done", "waiting", "rejected"}, (
                    f"Deferred spec {sid} is in invalid state {state}"
                )


TestQueueProperties = DirectoryQueueStateMachine.TestCase
TestQueueProperties.settings = settings(
    max_examples=100, stateful_step_count=20, deadline=None
)


# --- Standalone Invariant Properties ---


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["oracle", "nop", "codex", "claude-code"]),
            st.floats(min_value=0.5, max_value=4.0),
            st.booleans(),  # vanish before list_specs
        ),
        min_size=1,
        max_size=15,
    )
)
@settings(max_examples=50, deadline=None)
def test_property_vanished_file_tolerance(specs_data: list[tuple[str, float, bool]]) -> None:
    """DirectoryQueue.list_specs and tick tolerate vanished files without crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        queue = DirectoryQueue(root / "queue")
        gate = PolicyGate(policy())

        submitted_paths = []
        for i, (agent, cost, vanish) in enumerate(specs_data):
            s = spec(f"spec-{i}", agent=agent, est_cost_usd=cost)
            dest, _ = queue.submit(s, gate=gate, spent_today_usd=0.0)
            submitted_paths.append((dest, vanish))

        # Vanish marked files
        for dest, vanish in submitted_paths:
            if vanish and dest.exists():
                dest.unlink()

        # list_specs must never raise FileNotFoundError or crash
        for state in QUEUE_STATES:
            records = queue.list_specs(state)
            for path, _item in records:
                assert path.exists(), f"Listed non-existent file {path}"


@given(
    st.lists(
        st.sampled_from(["codex", "claude-code"]),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=50, deadline=None)
def test_property_credential_deferral_preserves_approved_state(agents: list[str]) -> None:
    """When credentials are missing, approved specs stay in approved/ and log dispatch_deferred."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        queue = DirectoryQueue(root / "queue")
        exec_service = Executor(
            repo_root=root,
            queue=queue,
            policy=policy(),
            runner=lambda req: req.jobs_dir / req.name,
            ingester=lambda _path: None,
            spent_today=lambda: 0.0,
            consecutive_harness_failures=lambda: 0,
            credential_probe=lambda: frozenset(),  # No credentials available
            sleeper=lambda _s: None,
        )

        spec_ids = []
        for i, agent in enumerate(agents):
            s = spec(f"cred-spec-{i}", agent=agent, est_cost_usd=1.0)
            waiting, _ = queue.submit(s, gate=exec_service.gate, spent_today_usd=0.0)
            approved = queue.approve(str(queue.load(waiting).spec_id), actor="peter")
            spec_ids.append(str(queue.load(approved).spec_id))

        # First tick with NO credentials -> 0 dispatched
        assert exec_service.tick() == 0

        # Every spec MUST still be in approved/
        approved_specs = {str(s.spec_id) for _, s in queue.list_specs("approved")}
        for sid in spec_ids:
            assert sid in approved_specs, (
                f"Spec {sid} was lost from approved/ on credential deferral"
            )

        # Events must record dispatch_deferred
        events = load_events(queue.events_path)
        deferred_events = [e for e in events if e.event == "dispatch_deferred"]
        assert len(deferred_events) == len(agents)
        for e in deferred_events:
            assert e.reason_code is not None and e.reason_code.startswith("missing_credential:")


@given(
    st.lists(
        st.floats(min_value=3.0, max_value=8.0),
        min_size=3,
        max_size=10,
    )
)
@settings(max_examples=50, deadline=None)
def test_property_quota_never_exceeded_mid_tick(costs: list[float]) -> None:
    """When daily quota is exhausted mid-tick, subsequent specs move to waiting/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        queue = DirectoryQueue(root / "queue")
        ceiling = 20.0
        p = StandingApprovalsPolicy(
            daily_cost_ceiling_usd=ceiling,
            per_job_cost_ceiling_usd=10.0,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
            escalate_to_human=["anything_exceeding_ceilings"],
        )

        total_catalog_spent = [0.0]

        def stub_runner(req: RunRequest) -> Path:
            destination = req.jobs_dir / req.name
            destination.mkdir(parents=True, exist_ok=True)
            return destination

        def stub_ingester(path: Path) -> None:
            for _pth, s in queue.list_specs("running"):
                if s.name == path.name:
                    total_catalog_spent[0] += s.est_cost_usd
                    break

        exec_service = Executor(
            repo_root=root,
            queue=queue,
            policy=p,
            runner=stub_runner,
            ingester=stub_ingester,
            spent_today=lambda: total_catalog_spent[0],
            consecutive_harness_failures=lambda: 0,
            credential_probe=lambda: frozenset({CLAUDE_OAUTH, CODEX_AUTH}),
            sleeper=lambda _s: None,
        )

        for i, cost in enumerate(costs):
            s = spec(f"quota-spec-{i}", agent="codex", est_cost_usd=round(cost, 2), priority=i)
            waiting, _ = queue.submit(s, gate=exec_service.gate, spent_today_usd=0.0)
            queue.approve(str(queue.load(waiting).spec_id), actor="peter")

        dispatched = exec_service.tick()

        assert total_catalog_spent[0] <= ceiling, (
            f"Catalog spend {total_catalog_spent[0]} exceeded daily ceiling {ceiling}"
        )

        waiting_specs = queue.list_specs("waiting")
        if dispatched < len(costs):
            assert len(waiting_specs) > 0, "Undispatched specs did not land in waiting/"
            for _, ws in waiting_specs:
                reason_path = root / "queue" / "reasons" / f"{ws.spec_id}.json"
                if reason_path.is_file():
                    assert "daily_spend_limit" in reason_path.read_text()
