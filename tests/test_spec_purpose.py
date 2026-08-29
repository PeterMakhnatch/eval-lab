"""Every spec must say why it exists, and a purposeless one must not dispatch.

`docs/build-plan.md` WS-E item 1 asks for two things, and they are not the same
guard:

1. `ExperimentSpec.purpose` is **required**, so a purposeless spec cannot be
   built or validated at all. This is the earliest and strongest refusal.
2. **Dispatch-time rejection** in the gate, which is the backstop. It is not
   redundant, because readers that tolerate an unvalidated spec exist on purpose
   — `status.py` reports a bad spec as an error string rather than raising — so a
   spec can be read, listed, and displayed without ever passing validation. This
   file pins both, plus the reason code and the message the operator reads.

Why it matters, from `docs/architecture-review-2026-08-16.md` §4: until now
nothing recorded intent, so the queue could be listed but never grouped,
budgeted, or reviewed by what the night was trying to learn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.queue import (
    DirectoryQueue,
    PolicyGate,
    purposeless_spec_message,
)
from evallab.schemas import (
    EXPERIMENT_PURPOSES,
    AutoRunRule,
    ExperimentMatrix,
    ExperimentSpec,
    StandingApprovalsPolicy,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backfill_spec_purpose import backfill_queue  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The value set fixed by `docs/build-plan.md` WS-E item 1, written out rather
#: than imported, so an edit to the taxonomy has to be made in two places and
#: cannot be a silent one-character change to a Literal.
PLANNED_PURPOSES = (
    "baseline",
    "comparison",
    "elicitation",
    "drift",
    "calibration",
    "craft",
    "practice",
)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


def payload(**overrides) -> dict:
    base = {
        "name": "purpose-probe",
        "hypothesis": "a spec records why it exists",
        "purpose": "baseline",
        "task": "library/tasks/event-summary",
        "agent": "oracle",
        "submitted_by": "test",
    }
    base.update(overrides)
    return base


# --- the field itself --------------------------------------------------------


def test_the_declared_values_are_exactly_the_ones_the_plan_fixed():
    """The taxonomy is Peter's; drift in it is a decision, not a refactor."""
    assert EXPERIMENT_PURPOSES == PLANNED_PURPOSES


@pytest.mark.parametrize("purpose", PLANNED_PURPOSES)
def test_every_planned_purpose_validates(purpose: str):
    assert ExperimentSpec.model_validate(payload(purpose=purpose)).purpose == purpose


def test_purpose_is_a_plain_string_not_an_enum_member():
    """Operator surfaces print it directly, so `Purpose.baseline` would leak."""
    spec = ExperimentSpec.model_validate(payload(purpose="drift"))
    assert isinstance(spec.purpose, str)
    assert f"{spec.purpose}" == "drift"


def test_a_spec_without_a_purpose_cannot_be_validated():
    """Required, so the first refusal happens before the queue is involved."""
    raw = payload()
    del raw["purpose"]
    with pytest.raises(ValidationError, match="purpose"):
        ExperimentSpec.model_validate(raw)


def test_a_purpose_outside_the_taxonomy_is_refused():
    """An invented value would silently corrupt grouping and budgeting."""
    with pytest.raises(ValidationError, match="purpose"):
        ExperimentSpec.model_validate(payload(purpose="exploration"))


def test_the_matrix_deliberately_does_not_carry_a_purpose():
    """The asymmetry is a decision — see the comment on `ExperimentMatrix`.

    A matrix never enters the queue (`cli._matrix_command` calls
    `Executor.execute_direct`, which consults no `PolicyGate`), so a purpose on
    one would be read by nothing. `ContractModel` forbids extras, so this also
    proves the field cannot be set there by accident.
    """
    matrix = {
        "schema_version": 2,
        "matrix_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
        "name": "controls",
        "hypothesis": "oracle passes and nop fails",
        "benchmark_family": "event-summary",
        "task_id": "event-summary",
        "task": "library/tasks/event-summary",
        "task_package_digest": "sha256:" + "1" * 64,
        "verifier_digest": "sha256:" + "2" * 64,
        "runs": [{"name": "oracle-control", "agent": "oracle"}],
    }
    assert ExperimentMatrix.model_validate(matrix).runs[0].agent == "oracle"
    with pytest.raises(ValidationError):
        ExperimentMatrix.model_validate({**matrix, "purpose": "baseline"})


# --- dispatch-time rejection -------------------------------------------------


def purposeless(**overrides) -> ExperimentSpec:
    """A spec that reached the gate without passing validation.

    `model_construct` is the honest way to build this: it is exactly what a
    tolerant reader or a future back-compat path would produce, and it is the
    only way to prove the gate's own check rather than pydantic's.
    """
    raw = payload(**overrides)
    raw.pop("purpose", None)
    return ExperimentSpec.model_construct(**raw)


def test_the_gate_refuses_a_purposeless_spec():
    decision = PolicyGate(policy()).decide(purposeless(), spent_today_usd=0.0)
    assert decision.admitted is False
    assert decision.reason_code == "purposeless_spec"


def test_the_refusal_names_every_allowed_value():
    """The operator's next action is to pick one, so the list must be present."""
    decision = PolicyGate(policy()).decide(purposeless(), spent_today_usd=0.0)
    for value in PLANNED_PURPOSES:
        assert value in decision.message
    assert "purpose" in decision.message


def test_the_refusal_names_the_spec_and_the_fix():
    decision = PolicyGate(policy()).decide(
        purposeless(name="unnamed-intent"), spent_today_usd=0.0
    )
    assert "unnamed-intent" in decision.message
    assert "evallab submit" in decision.message


def test_the_gate_refuses_a_purpose_outside_the_taxonomy():
    """A bypass can carry a plausible-looking value; the gate still refuses."""
    spec = ExperimentSpec.model_construct(**payload(purpose="exploration"))
    decision = PolicyGate(policy()).decide(spec, spent_today_usd=0.0)
    assert decision.reason_code == "purposeless_spec"
    assert "'exploration'" in decision.message
    assert "not a purpose this lab recognises" in decision.message


def test_a_spec_that_declares_a_purpose_is_not_refused_for_it():
    """The guard must not refuse the ordinary case it is guarding."""
    decision = PolicyGate(policy()).decide(
        ExperimentSpec.model_validate(payload(policy_rule="local-controls")),
        spent_today_usd=0.0,
    )
    assert decision.admitted is True
    assert decision.policy_rule == "local-controls"


def test_a_purposeless_billable_spec_is_still_refused():
    """#65 is not weakened: a paid agent without a purpose still cannot run.

    It is named by the more proximate defect, which is the point of ordering the
    check first — but nothing is admitted that was refused before.
    """
    decision = PolicyGate(policy()).decide(
        purposeless(agent="codex", est_cost_usd=2.5), spent_today_usd=0.0
    )
    assert decision.admitted is False
    assert decision.reason_code == "purposeless_spec"


def test_the_message_helper_reports_a_missing_and_a_wrong_value_differently():
    """Two different operator mistakes, so two different first lines."""
    missing = purposeless_spec_message(purposeless())
    wrong = purposeless_spec_message(
        ExperimentSpec.model_construct(**payload(purpose="exploration"))
    )
    assert "declares no purpose" in missing
    assert "declares no purpose" not in wrong
    assert "'exploration'" in wrong


# --- what a pre-existing queue file does ------------------------------------
#
# Making a field required is a migration, and `queue/` is live runtime state
# that predates it. These two tests pin what actually happens, because the
# answer decided that this PR ships `scripts/backfill_spec_purpose.py`.


def legacy_queue(root: Path, state: str = "waiting") -> tuple[DirectoryQueue, Path]:
    """A queue holding one spec written before `purpose` existed."""
    queue = DirectoryQueue(root / "queue")
    stale = payload(name="pre-purpose-spec")
    del stale["purpose"]
    stale["spec_id"] = "01LEGACYSPECID0000000000000"
    path = queue.state_dir(state) / "oracle-01LEGACYSPECID0000000000000.json"
    path.write_text(json.dumps(stale, indent=2) + "\n")
    return queue, path


def test_a_pre_purpose_queue_file_fails_loudly_and_names_the_field(tmp_path: Path):
    """It must not be silently skipped, and the error must be diagnosable.

    `list_specs` deliberately re-raises anything that is not a vanished file
    (`queue.py:964-976`): a malformed spec in the queue is corruption, and
    failing closed is the designed response. This mission does not weaken that.
    What it owes instead is an error an operator can act on — so this pins that
    the message names both the offending path and the missing field.
    """
    queue, path = legacy_queue(tmp_path)
    with pytest.raises(ValueError) as caught:
        queue.list_specs("waiting")
    assert path.name in str(caught.value)
    assert "purpose" in str(caught.value)


def test_the_backfill_makes_a_pre_purpose_queue_readable_again(tmp_path: Path):
    """The migration is the supported answer, and it must actually work.

    Proven end to end on a real queue directory: unreadable before, readable
    after, and the value it wrote is one the taxonomy recognises.
    """
    queue, path = legacy_queue(tmp_path)
    report = backfill_queue(tmp_path / "queue", apply=True)

    assert report.updated == [path]
    assert queue.list_specs("waiting")[0][1].purpose in EXPERIMENT_PURPOSES
    assert json.loads(path.read_text())["purpose"] in EXPERIMENT_PURPOSES


def test_the_backfill_defaults_to_a_dry_run(tmp_path: Path):
    """A migration that writes before it is asked is not a migration."""
    _, path = legacy_queue(tmp_path)
    before = path.read_text()
    report = backfill_queue(tmp_path / "queue", apply=False)
    assert report.updated == [path]
    assert path.read_text() == before


def test_the_backfill_derives_intent_from_evidence_not_from_a_default(tmp_path: Path):
    """A canary record must not be relabelled as something it never was.

    The whole risk of a backfill is fabricating research intent. Where the spec
    self-identifies, the value is derived from that evidence; the fallback is
    only for records that carry none, and those are reported separately so they
    can be corrected rather than silently trusted.
    """
    queue = DirectoryQueue(tmp_path / "queue")
    cases = {
        "canary": (payload(name="canary-a", policy_rule="canary", task="canary/x"), "drift"),
        "judge": (payload(name="judge-a", submitted_by="judge"), "calibration"),
        "researcher": (
            payload(name="research-a", submitted_by="autopilot-researcher"),
            "comparison",
        ),
        "smoke": (payload(name="smoke-oracle-a", submitted_by="solidify-smoke"), "practice"),
    }
    for key, (raw, _expected) in cases.items():
        del raw["purpose"]
        (queue.state_dir("done") / f"oracle-{key}.json").write_text(
            json.dumps(raw, indent=2) + "\n"
        )

    backfill_queue(tmp_path / "queue", apply=True)
    written = {
        key: json.loads((queue.state_dir("done") / f"oracle-{key}.json").read_text())["purpose"]
        for key in cases
    }
    assert written == {key: expected for key, (_raw, expected) in cases.items()}


def test_the_backfill_reports_records_it_could_not_derive(tmp_path: Path):
    """Silence about a guess is the failure mode; naming it is the feature."""
    queue = DirectoryQueue(tmp_path / "queue")
    raw = payload(name="anonymous-spec", submitted_by="someone", agent="custom-model")
    del raw["purpose"]
    path = queue.state_dir("done") / "custom-anonymous.json"
    path.write_text(json.dumps(raw, indent=2) + "\n")

    report = backfill_queue(tmp_path / "queue", apply=True)
    assert report.undeclared == [path]
    assert json.loads(path.read_text())["purpose"] in EXPERIMENT_PURPOSES


def test_the_backfill_leaves_a_spec_that_already_declares_one_alone(tmp_path: Path):
    """Idempotent, and it never overwrites a value someone chose."""
    queue = DirectoryQueue(tmp_path / "queue")
    path = queue.state_dir("done") / "oracle-declared.json"
    path.write_text(json.dumps(payload(purpose="elicitation"), indent=2) + "\n")
    before = path.read_text()

    report = backfill_queue(tmp_path / "queue", apply=True)
    assert report.updated == []
    assert path.read_text() == before


def test_the_backfill_never_writes_a_file_it_would_reject(tmp_path: Path):
    """It validates before writing, so it cannot produce a second broken file."""
    queue = DirectoryQueue(tmp_path / "queue")
    broken = queue.state_dir("done") / "oracle-broken.json"
    broken.write_text(json.dumps({"name": "no", "not_a_spec": True}, indent=2) + "\n")
    before = broken.read_text()

    report = backfill_queue(tmp_path / "queue", apply=True)
    assert broken in report.skipped
    assert broken.read_text() == before


# --- the committed corpus ----------------------------------------------------


def test_every_committed_spec_declares_a_recognised_purpose():
    """`purpose` is required, so an unmigrated committed spec is a broken spec.

    Mirrors the corpus sweep in `test_jobs_dir_contract.py`: the floor guards
    against the glob silently matching nothing, and a new spec raises it.
    """
    roots = [
        REPO_ROOT / "research" / "experiments",
        REPO_ROOT / "research" / "calibration" / "records" / "queue-specs",
    ]
    checked = 0
    for root in roots:
        for path in sorted(root.rglob("*.json")):
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict) or isinstance(raw.get("runs"), list):
                continue  # matrices carry no purpose, by decision
            if "jobs_dir" not in raw:
                continue
            spec = ExperimentSpec.model_validate(raw)
            assert spec.purpose in EXPERIMENT_PURPOSES, path
            checked += 1
    assert checked >= 12, f"expected the committed spec corpus, validated only {checked}"


def test_the_committed_matrices_still_validate_without_a_purpose():
    """The other half of the asymmetry, proven on the real committed files."""
    checked = 0
    for path in sorted((REPO_ROOT / "research" / "experiments").rglob("*.json")):
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get("runs"), list):
            continue
        assert "purpose" not in raw, path
        ExperimentMatrix.model_validate(raw)
        checked += 1
    assert checked >= 5, f"expected the committed matrices, validated only {checked}"


# --- the reserved self-test path keeps working ------------------------------


def test_the_smoke_self_test_spec_shape_is_admissible():
    """`smoke.py` submits through the real gate, so its purpose must be admitted.

    Pinned because the value there is provisional: `practice` is the least-wrong
    member of a taxonomy with no "infrastructure self-verification" entry. If it
    is ever retaxonomised, this is the test that says the self-test still runs.
    """
    spec = ExperimentSpec.model_validate(
        payload(
            name="smoke-oracle-4f2a1c",
            purpose="practice",
            jobs_dir="runs/_smoke/smoke-oracle-4f2a1c/jobs",
            policy_rule="local-controls",
        )
    )
    decision = PolicyGate(policy()).decide(spec, spent_today_usd=0.0)
    assert decision.admitted is True


def test_a_submitted_spec_keeps_its_purpose_through_the_queue(tmp_path: Path):
    """Grouping the queue by intent only works if the value survives a round trip.

    `submit` writes to `pending/`, then `transition` re-reads and re-validates
    the file on the way to `approved/`, so this exercises the load path a stale
    spec would fail on — not just in-memory field access.
    """
    queue = DirectoryQueue(tmp_path / "queue")
    gate = PolicyGate(policy())
    path, decision = queue.submit(
        ExperimentSpec.model_validate(payload(purpose="drift", policy_rule="local-controls")),
        gate=gate,
        spent_today_usd=0.0,
    )
    assert decision.admitted is True
    assert path.parent.name == "approved"
    assert queue.load(path).purpose == "drift"
    assert json.loads(path.read_text())["purpose"] == "drift"
