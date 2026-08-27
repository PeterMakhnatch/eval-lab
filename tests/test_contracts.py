"""Contract freeze tests for E00.

Golden schemas committed so that any field add/rename/retype/reorder fails CI.
Regeneration script documented in docs/contracts.md.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.schemas import (
    AnalysisRecord,
    CalibrationRecord,
    CapabilityCurveReport,
    CapabilityCurveSpec,
    ConfidenceClaim,
    ControlEvidenceRef,
    CriterionAgreement,
    ElicitationSpec,
    EvidenceCitation,
    ExperimentSpec,
    ObservationRecord,
    PowerSpec,
    PreregSpec,
    Suite,
    TaskContamination,
    TaskControlEvidence,
    TaskDigests,
    TaskLimits,
    TaskRegistryRecord,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def _load_golden(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_golden_schemas_match_live():
    """Byte-for-byte golden freeze: changing any contract field fails this."""
    for Model, golden_name in [
        (Suite, "Suite"),
        (AnalysisRecord, "AnalysisRecord"),
        (ObservationRecord, "ObservationRecord"),
        (CalibrationRecord, "CalibrationRecord"),
        (Verdict, "Verdict"),
        (ExperimentSpec, "ExperimentSpec"),
        (TaskRegistryRecord, "TaskRegistryRecord"),
        (CapabilityCurveSpec, "CapabilityCurveSpec"),
        (CapabilityCurveReport, "CapabilityCurveReport"),
    ]:
        live = Model.model_json_schema()
        committed = _load_golden(golden_name)
        assert live == committed, f"{golden_name} schema drift"


def test_suite_frozen_immutable():
    """frozen_at set => mutation rejected (enforced in model)."""
    now = datetime.now(UTC)
    s = Suite(name="baseline", version="v1", frozen_at=now)
    assert s.frozen_at == now
    with pytest.raises(ValueError, match="frozen Suite is immutable"):
        s.name = "other"


def test_roundtrip_all_models():
    """Valid instances survive model_dump -> model_validate unchanged."""
    now = datetime.now(UTC)

    suite = Suite(name="test", version="1", members=["task@1"], frozen_at=now)
    assert Suite.model_validate(suite.model_dump()) == suite

    analysis = AnalysisRecord(
        analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        rubric_digest="sha256:" + "a" * 64,
        model="gpt-4o",
        category="failure",
        evidence=[EvidenceCitation(path="result.json", step=3)],
        confidence=ConfidenceClaim(level="high", n=10, interval=(0.8, 0.95)),
    )
    assert AnalysisRecord.model_validate(analysis.model_dump()) == analysis

    obs = ObservationRecord(
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_name="t1",
        job="j1",
        agent="oracle",
        model="gpt",
        task="event-summary@1",
        reward=1.0,
        steps_taken=5,
        first_failure_step="none",
        loop_detected="no",
        loop_step="none",
        verified_before_done="no",
        tool_errors=0,
        summary="ok",
        evidence_files="result.json",
    )
    assert ObservationRecord.model_validate(obs.model_dump()) == obs

    calib = CalibrationRecord(
        calib_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        judge_model="gpt-4o",
        rubric_digest="sha256:" + "b" * 64,
        corpus_digest="sha256:" + "c" * 64,
        per_criterion_agreement={
            "correctness": CriterionAgreement(agreements=8, total=10, rate=0.8)
        },
        date=now,
    )
    assert CalibrationRecord.model_validate(calib.model_dump()) == calib

    verdict = Verdict(
        discovery_id="D-20260815-KTXJSHGZ",
        status="accepted",
        by="peter",
        at=now,
        note="solid",
    )
    assert Verdict.model_validate(verdict.model_dump()) == verdict

    elicitation = ElicitationSpec(
        preamble_hash="sha256:" + "d" * 64,
        toolset=["bash", "edit"],
        env_overrides={"DEBUG": "1"},
    )
    assert ElicitationSpec.model_validate(elicitation.model_dump()) == elicitation

    prereg = PreregSpec(
        expected="Expected pass@3 >= 0.80",
        decision_rule="Accept if lower bound of delta > 0.0",
    )
    assert PreregSpec.model_validate(prereg.model_dump()) == prereg

    power = PowerSpec(mdd=0.15, planned_n=30)
    assert PowerSpec.model_validate(power.model_dump()) == power

    spec = ExperimentSpec(
        spec_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        name="test-spec-roundtrip",
        hypothesis="Testing all fields roundtrip faithfully",
        purpose="comparison",
        question_ref="EXP-S01-canary-event-summary",
        elicitation=elicitation,
        prereg=prereg,
        power=power,
        task="canary/event-summary",
        task_path="library/tasks/event-summary",
        agent="codex",
        environment="docker",
        jobs_dir="runs",
        attempts=3,
        concurrency=1,
        submitted_by="test-author",
        submitted_at=now,
    )
    assert ExperimentSpec.model_validate(spec.model_dump()) == spec

    contamination = TaskContamination(
        public_since=date(2024, 6, 1),
        in_pretrain="y",
        basis="Public GitHub repository since 2024-06-01",
    )
    assert TaskContamination.model_validate(contamination.model_dump()) == contamination

    registry_digests = TaskDigests(
        task_toml="sha256:" + "1" * 64,
        instruction="sha256:" + "2" * 64,
        environment="sha256:" + "3" * 64,
        verifier="sha256:" + "4" * 64,
        package="sha256:" + "5" * 64,
    )

    registry_record = TaskRegistryRecord(
        task_id="sample-task",
        version="1.0.0",
        task_path="library/tasks/sample-task",
        digests=registry_digests,
        source_uri="https://github.com/example/sample-task",
        provenance_zone="02-local-evidence",
        is_synthetic=False,
        limits=TaskLimits(),
        control_evidence=TaskControlEvidence(
            oracle=ControlEvidenceRef(
                job_name="sample-task-oracle-evidence",
                trial_name="sample-task__oracle",
                reward=1.0,
                evidence_path="research/evidence/runs/sample-task-oracle-evidence/result.json",
                evidence_digest="sha256:" + "6" * 64,
                observed_at=now,
                lock_digest="sha256:" + "8" * 64,
                task_id="sample-task",
                task_version="1.0.0",
                task_digests=registry_digests,
                harbor_task_digest="sha256:" + "9" * 64,
            ),
            nop=ControlEvidenceRef(
                job_name="sample-task-nop-evidence",
                trial_name="sample-task__nop",
                reward=0.0,
                evidence_path="research/evidence/runs/sample-task-nop-evidence/result.json",
                evidence_digest="sha256:" + "7" * 64,
                observed_at=now,
                lock_digest="sha256:" + "a" * 64,
                task_id="sample-task",
                task_version="1.0.0",
                task_digests=registry_digests,
                harbor_task_digest="sha256:" + "9" * 64,
            ),
        ),
        state="registered",
        allowed_uses=["canary", "measurement"],
        contamination=contamination,
        human_minutes=45,
        approved_by="peter",
        approved_at=now,
    )
    assert TaskRegistryRecord.model_validate(registry_record.model_dump()) == registry_record


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-ulid",
        "01ARZ3NDEKTSV4RRFFQ69G5FA",  # too short
        "81ARZ3NDEKTSV4RRFFQ69G5FAV",  # invalid first char
        "01ARZ3NDEKTSV4RRFFQ69G5FAV-extra",
    ],
)
def test_ulid_rejection(bad_id):
    """Non-ULID ids are rejected on construction for every id field."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="identifier must be ULID"):
        AnalysisRecord(
            analysis_id=bad_id,
            trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            rubric_digest="sha256:" + "a" * 64,
            model="m",
            category="c",
            confidence=ConfidenceClaim(level="low"),
        )
    with pytest.raises(ValueError, match="identifier must be ULID"):
        ObservationRecord(
            trial_id=bad_id,
            trial_name="t",
            job="j",
            agent="a",
            task="task@1",
            reward=0,
            steps_taken=0,
            summary="",
        )
    with pytest.raises(ValueError, match="identifier must be ULID"):
        CalibrationRecord(
            calib_id=bad_id,
            judge_model="m",
            rubric_digest="sha256:" + "a" * 64,
            corpus_digest="sha256:" + "b" * 64,
            per_criterion_agreement={},
            date=now,
        )


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-discovery-id",
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",  # bare ULID
        "D-2026081-KTXJSHGZ",  # date too short (7 digits)
        "D-202608151-KTXJSHGZ",  # date too long (9 digits)
        "D-20260815-",  # empty suffix
        "20260815-KTXJSHGZ",  # missing D- prefix
        "D-20260815",  # missing suffix
        "D-20260815-foo!",  # invalid char in suffix
    ],
)
def test_discovery_id_rejection(bad_id: str) -> None:
    """Malformed discovery IDs rejected on Verdict construction."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        Verdict(
            discovery_id=bad_id,
            status="pending",
            by="peter",
            at=now,
        )


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:deadbeef",
        "notsha:" + "a" * 64,
        "sha256:" + "A" * 64,  # upper
        "sha256:" + "g" * 64,  # invalid hex
    ],
)
def test_digest_rejection(bad_digest):
    """Unprefixed or malformed digests rejected."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="digest must be sha256"):
        AnalysisRecord(
            analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            rubric_digest=bad_digest,
            model="m",
            category="c",
            confidence=ConfidenceClaim(level="low"),
        )
    with pytest.raises(ValueError, match="digest must be sha256"):
        CalibrationRecord(
            calib_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            judge_model="m",
            rubric_digest=bad_digest,
            corpus_digest="sha256:" + "a" * 64,
            per_criterion_agreement={},
            date=now,
        )


def test_status_rejection():
    """status outside the literal set rejected."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        Verdict(
            discovery_id="D-20260815-KTXJSHGZ",
            status="maybe",  # type: ignore[arg-type]
            by="x",
            at=now,
        )


def test_frozen_suite_rejects_mutation_after_construction():
    """Explicit test that frozen instance cannot be mutated."""
    now = datetime.now(UTC)
    s = Suite(name="s", version="1", frozen_at=now)
    with pytest.raises(ValueError, match="frozen Suite is immutable"):
        s.version = "2"


def test_observation_factual_fields_roundtrip():
    """ObservationRecord accepts the exact factual field list from TEMPLATE.md."""
    obs = ObservationRecord(
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_name="event-summary__foo",
        job="job1",
        agent="nop",
        model=None,
        task="event-summary@1",
        reward=0.0,
        steps_taken=0,
        first_failure_step="none",
        loop_detected="no",
        loop_step="none",
        verified_before_done="no",
        tool_errors=0,
        summary="Nop finished",
        evidence_files="result.json,verifier/reward.json",
    )
    dumped = obs.model_dump()
    assert dumped["template_version"] == "observatory-1"
    assert dumped["reward"] == 0.0
    assert ObservationRecord.model_validate(dumped) == obs


def test_experiment_spec_new_fields_roundtrip_and_prereg_verbatim():
    """Each new ExperimentSpec field roundtrips; prereg text survives byte-identically."""
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    raw_expected = "  Treatment achieves pass@3 >= 0.80\n  with exact indentation\n\tand tab\n"
    raw_decision_rule = "\n- Accept if lower bound > 0.05\n- Reject otherwise  "

    spec = ExperimentSpec(
        spec_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        name="exp-prereg-verbatim-test",
        hypothesis="Verbatim text in prereg survives roundtrip unmodified.",
        purpose="comparison",
        question_ref="EXP-S01-canary-event-summary",
        elicitation=ElicitationSpec(
            preamble_hash="sha256:" + "e" * 64,
            toolset=["bash", "read", "edit"],
            env_overrides={"DEBUG": "1", "SEED": "42"},
        ),
        prereg=PreregSpec(
            expected=raw_expected,
            decision_rule=raw_decision_rule,
        ),
        power=PowerSpec(mdd=0.125, planned_n=24),
        task="canary/event-summary",
        task_path="library/tasks/event-summary",
        agent="codex",
        environment="docker",
        jobs_dir="runs",
        attempts=3,
        concurrency=1,
        submitted_by="peter",
        submitted_at=now,
    )

    dumped = spec.model_dump()
    loaded = ExperimentSpec.model_validate(dumped)

    assert loaded == spec
    assert loaded.question_ref == "EXP-S01-canary-event-summary"
    assert loaded.elicitation is not None
    assert loaded.elicitation.preamble_hash == "sha256:" + "e" * 64
    assert loaded.elicitation.toolset == ["bash", "read", "edit"]
    assert loaded.elicitation.env_overrides == {"DEBUG": "1", "SEED": "42"}
    assert loaded.power is not None
    assert loaded.power.mdd == 0.125
    assert loaded.power.planned_n == 24

    # Exact byte-identity check for verbatim prereg text
    assert loaded.prereg is not None
    assert loaded.prereg.expected == raw_expected
    assert loaded.prereg.expected.encode("utf-8") == raw_expected.encode("utf-8")
    assert loaded.prereg.decision_rule == raw_decision_rule
    assert loaded.prereg.decision_rule.encode("utf-8") == raw_decision_rule.encode("utf-8")


def test_spec_pre_dating_new_fields_loads_with_defaults():
    """A spec written before question_ref/elicitation/prereg/power loads with defaults."""
    real_legacy_spec = {
        "schema_version": 1,
        "spec_id": "01M04N5A8ENV22FF5ER70B5MQZ",
        "name": "canary-event-summary-codex-20260816",
        "hypothesis": (
            "Pinned canary event-summary remains stable on codex; any excursion "
            "is a harness-drift suspect."
        ),
        "purpose": "drift",
        "task": "canary/event-summary",
        "task_path": "library/tasks/event-summary",
        "agent": "codex",
        "model": None,
        "environment": "docker",
        "jobs_dir": "runs",
        "attempts": 3,
        "concurrency": 1,
        "timeout_seconds": 1800,
        "submitted_by": "nightly-canary",
        "priority": 100,
        "est_cost_usd": 0.0,
        "policy_rule": None,
        "requires": [],
        "expected_reward": None,
        "task_version": None,
        "verifier_digest": None,
        "submitted_at": "2026-08-16T12:00:00Z",
        "grid_id": None,
        "grid_point": None,
    }

    spec = ExperimentSpec.model_validate(real_legacy_spec)
    assert spec.question_ref is None
    assert spec.elicitation is None
    assert spec.prereg is None
    assert spec.power is None
    assert spec.name == "canary-event-summary-codex-20260816"
    assert spec.attempts == 3


def test_task_registry_record_pre_dating_optional_metadata_loads_with_defaults():
    """A record written before contamination/human_minutes loads with defaults."""
    raw_record = {
        "schema_version": 1,
        "task_id": "legacy-task",
        "version": "1.0.0",
        "task_path": "library/tasks/legacy-task",
        "digests": {
            "task_toml": "sha256:" + "1" * 64,
            "instruction": "sha256:" + "2" * 64,
            "environment": "sha256:" + "3" * 64,
            "verifier": "sha256:" + "4" * 64,
            "package": "sha256:" + "5" * 64,
        },
        "source_uri": "https://github.com/example/legacy-task",
        "provenance_zone": "02-local-evidence",
        "is_synthetic": False,
        "limits": {"timeout_seconds": 1800},
        "control_evidence": {
            "oracle": {
                "job_name": "legacy-task-oracle",
                "reward": 1.0,
                "evidence_path": "research/evidence/runs/legacy-task-oracle/result.json",
                "evidence_digest": "sha256:" + "6" * 64,
                "observed_at": "2026-08-15T12:00:00Z",
            },
            "nop": {
                "job_name": "legacy-task-nop",
                "reward": 0.0,
                "evidence_path": "research/evidence/runs/legacy-task-nop/result.json",
                "evidence_digest": "sha256:" + "7" * 64,
                "observed_at": "2026-08-15T12:00:00Z",
            },
        },
        "state": "registered",
        "allowed_uses": ["measurement"],
        "approved_by": "peter",
        "approved_at": "2026-08-15T12:00:00Z",
    }
    for agent in ("oracle", "nop"):
        ref = raw_record["control_evidence"][agent]
        ref.update(
            {
                "trial_name": f"legacy-task__{agent}",
                "lock_digest": "sha256:" + "8" * 64,
                "task_id": "legacy-task",
                "task_version": "1.0.0",
                "task_digests": raw_record["digests"],
                "harbor_task_digest": "sha256:" + "9" * 64,
            }
        )

    record = TaskRegistryRecord.model_validate(raw_record)
    assert record.contamination is None
    assert record.human_minutes is None
    assert record.task_id == "legacy-task"


@pytest.mark.parametrize("status", ["y", "n", "unknown"])
def test_in_pretrain_valid_literals(status):
    """'y', 'n', and 'unknown' are all valid in_pretrain values."""
    c = TaskContamination(in_pretrain=status)
    assert c.in_pretrain == status


@pytest.mark.parametrize(
    "bad_status", ["maybe", "yes", "no", "true", "false", "UNKNOWN", "Y", "N", "", 123]
)
def test_in_pretrain_rejection(bad_status):
    """Values outside {'y', 'n', 'unknown'} are rejected."""
    with pytest.raises(ValidationError):
        TaskContamination(in_pretrain=bad_status)  # type: ignore[arg-type]


def test_elicitation_one_variable_difference_expressible():
    """Elicitation differing in 1 field is distinguishable from 0 or multi-field diffs."""
    base = ElicitationSpec(
        preamble_hash="sha256:aaaa",
        toolset=["bash", "read"],
        env_overrides={"FLAG": "1"},
    )

    identical = ElicitationSpec(
        preamble_hash="sha256:aaaa",
        toolset=["bash", "read"],
        env_overrides={"FLAG": "1"},
    )
    diff_preamble = ElicitationSpec(
        preamble_hash="sha256:bbbb",
        toolset=["bash", "read"],
        env_overrides={"FLAG": "1"},
    )
    diff_tools = ElicitationSpec(
        preamble_hash="sha256:aaaa",
        toolset=["bash", "read", "edit"],
        env_overrides={"FLAG": "1"},
    )
    diff_env = ElicitationSpec(
        preamble_hash="sha256:aaaa",
        toolset=["bash", "read"],
        env_overrides={"FLAG": "2"},
    )
    diff_two_a = ElicitationSpec(
        preamble_hash="sha256:bbbb",
        toolset=["bash"],
        env_overrides={"FLAG": "1"},
    )
    diff_two_b = ElicitationSpec(
        preamble_hash="sha256:aaaa",
        toolset=["bash"],
        env_overrides={"FLAG": "2"},
    )
    diff_three = ElicitationSpec(
        preamble_hash="sha256:bbbb",
        toolset=["bash"],
        env_overrides={"FLAG": "2"},
    )

    assert base.diff_fields(identical) == []
    assert base.diff_fields(diff_preamble) == ["preamble_hash"]
    assert base.diff_fields(diff_tools) == ["toolset"]
    assert base.diff_fields(diff_env) == ["env_overrides"]

    # Exactly one field differs
    assert len(base.diff_fields(diff_preamble)) == 1
    assert len(base.diff_fields(diff_tools)) == 1
    assert len(base.diff_fields(diff_env)) == 1

    # Multiple fields differ
    assert set(base.diff_fields(diff_two_a)) == {"preamble_hash", "toolset"}
    assert len(base.diff_fields(diff_two_a)) == 2
    assert set(base.diff_fields(diff_two_b)) == {"toolset", "env_overrides"}
    assert len(base.diff_fields(diff_two_b)) == 2
    assert set(base.diff_fields(diff_three)) == {"preamble_hash", "toolset", "env_overrides"}
    assert len(base.diff_fields(diff_three)) == 3

    # The §4 comparison check: exactly one field differs
    def is_one_variable_elicitation(a: ElicitationSpec, b: ElicitationSpec) -> bool:
        return len(a.diff_fields(b)) == 1

    assert is_one_variable_elicitation(base, diff_preamble) is True
    assert is_one_variable_elicitation(base, diff_tools) is True
    assert is_one_variable_elicitation(base, diff_env) is True
    assert is_one_variable_elicitation(base, identical) is False
    assert is_one_variable_elicitation(base, diff_two_a) is False
    assert is_one_variable_elicitation(base, diff_two_b) is False
    assert is_one_variable_elicitation(base, diff_three) is False


def test_golden_freeze_detects_injected_field():
    """The golden freeze fails when any schema has an unexpected/injected field."""
    schema = ExperimentSpec.model_json_schema()
    mutated = json.loads(json.dumps(schema))
    mutated["properties"]["injected_unapproved_field"] = {"type": "string"}

    with pytest.raises(AssertionError, match="schema drift"):
        live = mutated
        committed = _load_golden("ExperimentSpec")
        assert live == committed, "ExperimentSpec schema drift"


def _s03_spec(**overrides: object) -> ExperimentSpec:
    fields: dict[str, object] = {
        "name": "exp-s03-treatment",
        "hypothesis": "an elicitation preamble raises the pass rate",
        "purpose": "elicitation",
        "task": "canary/event-summary",
        "agent": "codex",
        "submitted_by": "test-author",
    }
    fields.update(overrides)
    return ExperimentSpec(**fields)  # type: ignore[arg-type]


def test_extra_instruction_path_defaults_to_none():
    """The control arm leaves the preamble unset; absence must be the default."""
    assert _s03_spec().extra_instruction_path is None


def test_extra_instruction_path_accepts_a_repo_relative_file():
    spec = _s03_spec(extra_instruction_path="library/preambles/s03-treatment.md")
    assert spec.extra_instruction_path == "library/preambles/s03-treatment.md"


@pytest.mark.parametrize("escape", ["/etc/passwd", "../../etc/passwd", "library/../../x"])
def test_extra_instruction_path_cannot_escape_the_repository(escape):
    """The preamble is a path the dispatcher forwards to Harbor, so it must be
    fenced by the same repo-relative rule as task_path and jobs_dir."""
    with pytest.raises(ValidationError):
        _s03_spec(extra_instruction_path=escape)


def test_factor_provenance_schema_migrates_preexisting_fact_table_additively() -> None:
    schema = (Path(__file__).parents[1] / "sql" / "schema.sql").read_text()
    columns = (
        "grid_id",
        "point_id",
        "arm_id",
        "factor_values_json",
        "factor_values_digest",
        "factor_bindings_json",
        "factor_bindings_digest",
        "bound_execution_values_json",
        "bound_execution_values_digest",
        "preamble_path",
        "preamble_content_sha256",
        "task_family",
        "task_id",
        "task_instance_id",
        "generator_seed_json",
        "task_block_inputs_json",
        "task_block_id",
    )
    for column in columns:
        assert (
            f"ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS {column} text;"
        ) in schema
        assert f"{column} text NOT NULL" not in schema
