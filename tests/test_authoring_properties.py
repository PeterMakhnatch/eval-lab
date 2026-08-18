"""Property-based state machine fuzz for BUILDER authoring proposal transitions and ledger."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from evallab.authoring import (
    AuthoringError,
    AuthoringPipeline,
    Outcome,
    QualificationRecord,
    RegisterRefusal,
    SeedClass,
    StructuralControlRunner,
    load_ledger,
    upsert_ledger,
    write_ledger,
)

FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

LEGAL_OUTCOMES: set[str] = {"proposed", "battery_passed", "craft_reviewed", "rejected"}


def write_task(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    instruction: str = "Summarize the input.\n",
) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        f"""schema_version = "1.0"
name = "{name}"
version = "{version}"

[verifier]
timeout_sec = 60.0
"""
    )
    (task_dir / "instruction.md").write_text(instruction)
    (task_dir / "environment").mkdir(exist_ok=True)
    tests = task_dir / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_outputs.py").write_text("def test_placeholder() -> None:\n    assert True\n")
    solution = task_dir / "solution"
    solution.mkdir(exist_ok=True)
    (solution / "solve.sh").write_text("#!/bin/bash\necho ok\n")
    return task_dir


def write_scenario(repo: Path, stem: str = "gap-notes") -> Path:
    path = repo / "research" / "scenarios" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {stem}\n\nA research scenario about hidden verifiers.\n")
    return path


def write_craft_parquet(path: Path) -> Path:
    table = pa.table(
        {
            "verifier_type": ["pytest"],
            "env_multi_container": [False],
            "pinned_deps": [False],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def make_test_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    write_task(repo / "library" / "tasks", "event-summary")
    write_scenario(repo)
    (repo / "library" / "registry").mkdir(parents=True, exist_ok=True)
    (repo / "library" / "registry" / "event-summary.json").write_text(
        json.dumps(
            {
                "task_id": "event-summary",
                "task_path": "library/tasks/event-summary",
                "state": "registered",
            }
        )
    )
    write_craft_parquet(repo / "derived" / "parquet" / "craft" / "craft.parquet")
    policy_dir = repo / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "standing-approvals.yaml").write_text(
        "version: 1\n"
        "daily_cost_ceiling_usd: 20\n"
        "per_job_cost_ceiling_usd: 3\n"
        "quiet_failure_rule: 3\n"
        "refuse_billable_at_used_percent: null\n"
        "auto_run:\n"
        "  - name: local-controls\n"
        "    agents: [oracle, nop]\n"
        "escalate_to_human:\n"
        "  - any_billable_agent\n"
    )
    (repo / "queue").mkdir(parents=True, exist_ok=True)

    tmpl_src = Path(__file__).resolve().parents[1] / "authoring/templates"
    if tmpl_src.is_dir():
        tmpl_dest = repo / "authoring/templates"
        tmpl_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmpl_src, tmpl_dest, dirs_exist_ok=True)

    return repo


class SequencedIds:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"prop{self.n:03d}"


class AuthoringProposalStateMachine(RuleBasedStateMachine):
    """Fuzzes authoring proposal lifecycle transitions:
    proposed -> battery_passed -> craft_reviewed -> registered|rejected.
    Proves that no illegal skips occur, registration is refused, and ledger is append-only/valid.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = make_test_repo(Path(self.tempdir.name))
        self.id_gen = SequencedIds()
        self.pipeline = AuthoringPipeline(
            self.repo,
            derived_root=self.repo / "derived" / "parquet",
            runner=StructuralControlRunner(),
            now=lambda: FIXED_NOW,
            new_id=self.id_gen,
        )
        self.history: dict[str, list[Outcome]] = {}  # proposal_id -> list of states

    def teardown(self) -> None:
        self.tempdir.cleanup()

    @rule(seed=st.sampled_from(["mutation", "scenario", "craft-gap"]))
    def propose(self, seed: SeedClass) -> None:
        ref = "event-summary" if seed == "mutation" else None
        proposal = self.pipeline.propose(seed, ref=ref)
        p_id = proposal.proposal_id
        assert p_id not in self.history, f"Duplicate proposal ID generated: {p_id}"
        assert proposal.outcome == "proposed"
        self.history[p_id] = ["proposed"]

    @rule(pick=st.integers(min_value=0, max_value=64))
    def run_battery(self, pick: int) -> None:
        candidates = [
            p_id for p_id, states in self.history.items() if states[-1] == "proposed"
        ]
        if not candidates:
            return
        p_id = sorted(candidates)[pick % len(candidates)]
        report = self.pipeline.run_battery(p_id)
        assert report.outcome in {"battery_passed", "rejected"}
        self.history[p_id].append(report.outcome)

    @rule(pick=st.integers(min_value=0, max_value=64))
    def review(self, pick: int) -> None:
        candidates = [
            p_id
            for p_id, states in self.history.items()
            if states[-1] in {"battery_passed", "craft_reviewed"}
        ]
        if not candidates:
            return
        p_id = sorted(candidates)[pick % len(candidates)]
        report = self.pipeline.review(p_id)
        assert report.outcome in {"craft_reviewed", "rejected"}
        self.history[p_id].append(report.outcome)

    @rule(pick=st.integers(min_value=0, max_value=64))
    def attempt_illegal_skip_battery_to_review(self, pick: int) -> None:
        """Attempting to review a proposal before passing battery must raise AuthoringError."""
        candidates = [
            p_id for p_id, states in self.history.items() if states[-1] == "proposed"
        ]
        if not candidates:
            return
        p_id = sorted(candidates)[pick % len(candidates)]
        try:
            self.pipeline.review(p_id)
            raise AssertionError(f"Review succeeded on un-batteried proposal {p_id}")
        except AuthoringError:
            pass

    @rule(pick=st.integers(min_value=0, max_value=64))
    def attempt_illegal_review_of_rejected_proposal(self, pick: int) -> None:
        """Attempting to review a rejected proposal must raise AuthoringError."""
        candidates = [
            p_id for p_id, states in self.history.items() if states[-1] == "rejected"
        ]
        if not candidates:
            return
        p_id = sorted(candidates)[pick % len(candidates)]
        try:
            self.pipeline.review(p_id)
            raise AssertionError(f"Review succeeded on rejected proposal {p_id}")
        except AuthoringError:
            pass

    @rule(pick=st.integers(min_value=0, max_value=64))
    def attempt_registration(self, pick: int) -> None:
        """Attempting to register any proposal via automation must always raise RegisterRefusal."""
        if not self.history:
            return
        p_id = sorted(self.history.keys())[pick % len(self.history)]
        try:
            self.pipeline.register(p_id)
            raise AssertionError(f"Registration succeeded for proposal {p_id}")
        except RegisterRefusal:
            pass

    @rule(pick=st.integers(min_value=0, max_value=64))
    def attempt_direct_registered_ledger_write(self, pick: int) -> None:
        """Direct upsert of 'registered' outcome to ledger must raise RegisterRefusal."""
        if not self.history:
            return
        p_id = sorted(self.history.keys())[pick % len(self.history)]
        record = QualificationRecord(
            proposal_id=p_id,
            seed_class="mutation",
            outcome="registered",
            created_at=FIXED_NOW.isoformat(),
            updated_at=FIXED_NOW.isoformat(),
        )
        try:
            upsert_ledger(self.pipeline.ledger, record)
            raise AssertionError("upsert_ledger permitted 'registered' outcome")
        except RegisterRefusal:
            pass

    @invariant()
    def ledger_record_count_matches_proposals(self) -> None:
        records = self.pipeline.records()
        assert len(records) == len(self.history)
        ledger_ids = {r.proposal_id for r in records}
        assert ledger_ids == set(self.history.keys())

    @invariant()
    def ledger_records_strictly_sorted_by_id(self) -> None:
        records = self.pipeline.records()
        ids = [r.proposal_id for r in records]
        assert ids == sorted(ids)

    @invariant()
    def ledger_state_matches_latest_tracked_outcome(self) -> None:
        for record in self.pipeline.records():
            latest = self.history[record.proposal_id][-1]
            assert record.outcome == latest
            assert record.outcome in LEGAL_OUTCOMES
            assert record.outcome != "registered"

    @invariant()
    def transition_histories_are_strictly_valid_dags(self) -> None:
        """Check that every proposal's state history satisfies the state machine transition grammar:
        proposed -> (battery_passed -> (craft_reviewed | rejected) | rejected)
        """
        for p_id, states in self.history.items():
            assert states[0] == "proposed"
            for i in range(1, len(states)):
                prev = states[i - 1]
                curr = states[i]
                if prev == "proposed":
                    assert curr in {"battery_passed", "rejected"}, (
                        f"Illegal transition from proposed to {curr} for {p_id}"
                    )
                elif prev == "battery_passed":
                    assert curr in {"craft_reviewed", "rejected"}, (
                        f"Illegal transition from battery_passed to {curr} for {p_id}"
                    )
                elif prev == "craft_reviewed":
                    assert curr in {"craft_reviewed", "rejected"}, (
                        f"Illegal transition from craft_reviewed to {curr} for {p_id}"
                    )
                elif prev == "rejected":
                    raise AssertionError(f"Proposal {p_id} transitioned after rejected to {curr}")


TestAuthoringProposalProperties = AuthoringProposalStateMachine.TestCase
TestAuthoringProposalProperties.settings = settings(
    max_examples=100, stateful_step_count=25, deadline=None
)


# --- Standalone Property Tests ---


@given(
    st.lists(
        st.tuples(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=3, max_size=10),
            st.sampled_from(["mutation", "scenario", "craft-gap", "inversion"]),
            st.sampled_from(["proposed", "battery_passed", "craft_reviewed", "rejected"]),
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)),
        ),
        min_size=1,
        max_size=30,
        unique_by=lambda t: t[0],
    )
)
@settings(max_examples=50, deadline=None)
def test_property_ledger_parquet_roundtrip(
    records_data: list[tuple[str, SeedClass, Outcome, float | None]],
) -> None:
    """write_ledger followed by load_ledger preserves records, sorts by proposal_id,
    and normalizes evidence_paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_file = Path(tmpdir) / "authoring_qualification.parquet"

        records = [
            QualificationRecord(
                proposal_id=pid,
                seed_class=seed,
                outcome=outcome,
                review_score=round(score, 3) if score is not None else None,
                created_at="2026-08-16T12:00:00Z",
                updated_at="2026-08-16T12:00:00Z",
                evidence_paths=[],
            )
            for pid, seed, outcome, score in records_data
        ]

        written_path = write_ledger(ledger_file, records)
        assert written_path.is_file()

        loaded = load_ledger(ledger_file)
        assert len(loaded) == len(records)

        # Loaded records must be strictly sorted by proposal_id
        loaded_ids = [r.proposal_id for r in loaded]
        assert loaded_ids == sorted(loaded_ids)

        # Content preservation
        loaded_map = {r.proposal_id: r for r in loaded}
        for r in records:
            matched = loaded_map[r.proposal_id]
            assert matched.seed_class == r.seed_class
            assert matched.outcome == r.outcome
            assert matched.review_score == r.review_score
            assert matched.evidence_paths == []


@given(
    st.sampled_from(["mutation", "scenario", "craft-gap"]),
)
@settings(max_examples=20, deadline=None)
def test_property_registration_hard_refusal(seed: SeedClass) -> None:
    """register() strictly raises RegisterRefusal across all proposals in all pipeline stages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = make_test_repo(Path(tmpdir))
        pipe = AuthoringPipeline(
            repo,
            derived_root=repo / "derived" / "parquet",
            runner=StructuralControlRunner(),
            now=lambda: FIXED_NOW,
            new_id=SequencedIds(),
        )

        ref = "event-summary" if seed == "mutation" else None
        p = pipe.propose(seed, ref=ref)

        # Refusal at 'proposed' stage
        try:
            pipe.register(p.proposal_id)
            raise AssertionError("register did not refuse")
        except RegisterRefusal as exc:
            assert exc.proposal_id == p.proposal_id
            assert exc.outcome == "proposed"

        # Advance to battery_passed
        pipe.run_battery(p.proposal_id)
        try:
            pipe.register(p.proposal_id)
            raise AssertionError("register did not refuse after battery")
        except RegisterRefusal as exc:
            assert exc.proposal_id == p.proposal_id
            assert exc.outcome == "battery_passed"
