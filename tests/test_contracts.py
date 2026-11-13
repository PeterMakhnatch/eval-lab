"""Contract boundaries, serialization compatibility, and legacy schema freezes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.schemas import (
    ElicitationSpec,
    ExperimentSpec,
    PowerSpec,
    PreregSpec,
    TaskContamination,
    TaskRegistryRecord,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"








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


def test_task_registry_record_without_family_is_refused():
    """Legacy records cannot enter campaign provenance without an immutable family."""
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

    with pytest.raises(ValidationError, match="schema_version|task_family"):
        TaskRegistryRecord.model_validate(raw_record)


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


def test_toolbox_path_and_sha256_must_be_paired():
    """toolbox_path and toolbox_sha256 must be provided together."""
    assert _s03_spec().toolbox_path is None
    assert _s03_spec().toolbox_sha256 is None

    with pytest.raises(ValidationError, match="must be provided together"):
        _s03_spec(toolbox_path="tools/repl_tools.py")

    with pytest.raises(ValidationError, match="must be provided together"):
        _s03_spec(toolbox_sha256="sha256:" + "a" * 64)

    valid = _s03_spec(
        toolbox_path="tools/repl_tools.py",
        toolbox_sha256="sha256:" + "a" * 64,
    )
    assert valid.toolbox_path == "tools/repl_tools.py"
    assert valid.toolbox_sha256 == "sha256:" + "a" * 64


@pytest.mark.parametrize("escape", ["/etc/passwd", "../../etc/passwd", "tools/../../x"])
def test_toolbox_path_cannot_escape_the_repository(escape):
    """toolbox_path must stay repo-relative."""
    with pytest.raises(ValidationError, match="relative to the repository"):
        _s03_spec(
            toolbox_path=escape,
            toolbox_sha256="sha256:" + "a" * 64,
        )


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












@pytest.mark.parametrize("binding", [
    {"harness_tree_path": "harness/baseline"},
    {"harness_tree_sha256": "sha256:" + "a" * 64},
    {"harness_tree_path": "harness/baseline", "harness_tree_sha256": "not-a-digest"},
])
def test_harness_spec_rejects_incomplete_or_malformed_binding(binding: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _s03_spec(agent="terminus-2", **binding)


def test_harness_spec_rejects_non_terminus_consumers() -> None:
    with pytest.raises(ValidationError):
        _s03_spec(
            agent="mini-swe-agent", harness_tree_path="harness/baseline",
            harness_tree_sha256="sha256:" + "a" * 64,
        )


def test_absent_harness_binding_does_not_change_legacy_serialized_specs() -> None:
    legacy = _s03_spec()
    payload = legacy.model_dump(mode="json")
    assert "harness_tree_path" not in payload
    assert "harness_tree_sha256" not in payload
    explicit_absence = ExperimentSpec.model_validate(
        payload | {"harness_tree_path": None, "harness_tree_sha256": None}
    )
    assert explicit_absence.model_dump_json() == legacy.model_dump_json()


@pytest.mark.parametrize("repeat_n", [0, 1, 11, -3])
def test_verifier_repeat_n_rejects_out_of_range(repeat_n: int) -> None:
    with pytest.raises(ValidationError):
        _s03_spec(verifier_repeat_n=repeat_n)


@pytest.mark.parametrize("repeat_n", [2, 3, 10])
def test_verifier_repeat_n_accepts_bounds(repeat_n: int) -> None:
    assert _s03_spec(verifier_repeat_n=repeat_n).verifier_repeat_n == repeat_n


def test_absent_verifier_repeat_does_not_change_legacy_serialized_specs() -> None:
    legacy = _s03_spec()
    assert legacy.verifier_repeat_n is None
    payload = legacy.model_dump(mode="json")
    assert "verifier_repeat_n" not in payload
    explicit_absence = ExperimentSpec.model_validate(payload | {"verifier_repeat_n": None})
    assert explicit_absence.model_dump_json() == legacy.model_dump_json()


def test_verifier_repeat_n_is_bound_into_spec_digest() -> None:
    from evallab.campaigns import experiment_spec_digest

    base = _s03_spec()
    assert experiment_spec_digest(base) == experiment_spec_digest(
        base.model_copy(update={"verifier_repeat_n": None})
    )
    assert experiment_spec_digest(base) != experiment_spec_digest(
        base.model_copy(update={"verifier_repeat_n": 3})
    )

@pytest.mark.parametrize("storage_mb", [0, 1, 1023, 1048577, -5])
def test_override_storage_mb_rejects_out_of_range(storage_mb: int) -> None:
    with pytest.raises(ValidationError):
        _s03_spec(override_storage_mb=storage_mb)


@pytest.mark.parametrize("storage_mb", [1024, 10240, 1048576])
def test_override_storage_mb_accepts_bounds(storage_mb: int) -> None:
    assert _s03_spec(override_storage_mb=storage_mb).override_storage_mb == storage_mb


def test_absent_override_storage_does_not_change_legacy_serialized_specs() -> None:
    legacy = _s03_spec()
    assert legacy.override_storage_mb is None
    payload = legacy.model_dump(mode="json")
    assert "override_storage_mb" not in payload
    explicit_absence = ExperimentSpec.model_validate(payload | {"override_storage_mb": None})
    assert explicit_absence.model_dump_json() == legacy.model_dump_json()


def test_override_storage_mb_is_bound_into_spec_digest() -> None:
    from evallab.campaigns import experiment_spec_digest

    base = _s03_spec()
    assert experiment_spec_digest(base) == experiment_spec_digest(
        base.model_copy(update={"override_storage_mb": None})
    )
    assert experiment_spec_digest(base) != experiment_spec_digest(
        base.model_copy(update={"override_storage_mb": 10240})
    )
