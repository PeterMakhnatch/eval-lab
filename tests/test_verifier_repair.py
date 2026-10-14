"""Consumer-visible repair boundaries; all model, quota and runtime I/O is injected."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from evallab.registry import compute_task_digests
from evallab.task_variants import derive_task, materialize
from evallab.verifier_repair import (
    QuotaBlocked,
    QuotaStatus,
    RepairError,
    RepairProposal,
    RepairTest,
    build_command_wrapper,
    ensure_quota,
    execute_repair,
    persisted_quota_record,
    plan_repair,
    validate_proposal,
)

MUTANT = "abc123def4"


def _fixture(tmp_path: Path):
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    (package / "solution").mkdir()
    (package / "task.toml").write_text(
        '[task]\nname="demo/repair"\n[environment]\nworkdir="/testbed"\n'
    )
    (package / "instruction.md").write_text("Add integers correctly.\n")
    (package / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (package / "tests/test.sh").write_text(
        'git apply /tests/test.patch\nsh -c "$(cat /tests/test_command.sh)"\n'
    )
    (package / "tests/test.patch").write_text(
        "diff --git a/tests/test_old.py b/tests/test_old.py\n--- /dev/null\n+++ b/tests/test_old.py\n@@ -0,0 +1 @@\n+def test_old(): pass\n"
    )
    (package / "tests/test_command.sh").write_text("python -m pytest tests/test_old.py\n")
    run = tmp_path / "runs/audit"
    (run / "scripts").mkdir(parents=True)
    (run / "capture/testbed").mkdir(parents=True)
    (run / "capture/testbed/answer.py").write_text("def add(a, b):\n    return a + b\n")
    runs = []
    for label, role, reward, script in (
        ("oracle", "capture-oracle", 1.0, "#!/bin/sh\n# oracle\n"),
        ("base", "capture-base", 0.0, "#!/bin/sh\n# base\n"),
        ("mutant", "mutant", 1.0, "#!/bin/sh\n# mutant\n"),
    ):
        path = run / "scripts" / f"{label}.sh"
        path.write_text(script)
        runs.append(
            {
                "name": label,
                "role": role,
                "reward": reward,
                "status": "ok",
                "script": str(path.relative_to(tmp_path)),
                "script_sha256": "sha256:" + hashlib.sha256(script.encode()).hexdigest(),
                "mutant": MUTANT if role == "mutant" else None,
                "outcome": "survived" if role == "mutant" else "control",
            }
        )
    report = {
        "schema": "evallab.verifier_mutation.report/v1",
        "task": "demo/repair",
        "package": str(package),
        "package_digest": compute_task_digests(package).package,
        "run_dir": str(run),
        "coverage": "guided",
        "executed": True,
        "verifier_mode": "separate",
        "verdict": "survivors_found",
        "limits": "Fixture",
        "targets": ["/testbed/answer.py"],
        "runs": runs,
        "mutants": [
            {
                "id": MUTANT,
                "file": "/testbed/answer.py",
                "language": "python",
                "changed_line": True,
                "mutated_sha256": "0" * 64,
                "line": 2,
                "operator": "arithmetic",
                "original": "+",
                "replacement": "-",
                "patch": "patch",
                "executed": True,
            }
        ],
    }
    report_path = run / "mutation-report.json"
    report_path.write_text(json.dumps(report))
    return plan_repair(report_path), report


def _proposal() -> RepairProposal:
    return RepairProposal(
        repairable=True,
        rationale="Exercise unequal operands rather than only zeros.",
        tests=[
            RepairTest(
                id="sum",
                repo_path="tests/test_repair_sum.py",
                code="from answer import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n",
            )
        ],
        command="python -m pytest tests/test_repair_sum.py -q",
        mutant_ids=[MUTANT],
    )


def _execute(
    tmp_path, plan, *, outcomes=(1.0, 0.0, 0.0), status="ok", declared=False, response=None,
    survivor_rewards=None,
):
    proposal = _proposal()
    survivor_rewards = survivor_rewards or {}
    if declared:
        from evallab.verifier_repair import RepairUnrepairable

        proposal.unrepairable = [
            RepairUnrepairable(mutant_id=MUTANT, reason="Equivalent input domain")
        ]
    calls = []

    def caller(prompt, context):
        calls.append("model")
        return response or proposal.model_dump_json(by_alias=True), {"input_tokens": 10}

    def runner(matrix, root):
        calls.append("runner")
        from contextlib import ExitStack

        from evallab.runner import staged_matrix_request

        with ExitStack() as staged:
            for item in matrix.runs:
                staged.enter_context(staged_matrix_request(matrix, item, repo_root=root))
        return [
            {
                "name": item.name,
                "status": status,
                "rewards": [] if reward is None else [reward],
                "error": None,
            }
            for item in matrix.runs
            for reward in [
                survivor_rewards.get(
                    item.name.rsplit("-survivor-", 1)[-1],
                    outcomes[
                        0
                        if item.name.endswith("-reference")
                        else 1
                        if item.name.endswith("-nop")
                        else 2
                    ],
                )
            ]
        ]

    result, directory = execute_repair(
        plan,
        model="gpt-6.1-sol",
        repo_root=tmp_path,
        output_dir=tmp_path / "attempt",
        quota_checker=lambda: QuotaStatus(True, 0.2),
        model_caller=caller,
        runner=runner,
        derive_fn=lambda parent, **kwargs: derive_task(
            parent, variants_root=tmp_path / "variants", **kwargs
        ),
        materialize_fn=lambda record, parent, **kwargs: materialize(
            record, parent, variants_root=tmp_path / "variants", **kwargs
        ),
    )
    return result, directory, calls


def test_nullable_model_metadata_does_not_block_valid_assertions(tmp_path):
    plan, _ = _fixture(tmp_path)
    payload = _proposal().model_dump(mode="json", by_alias=True)
    payload["model"] = None
    result, _, _ = _execute(tmp_path, plan, response=json.dumps(payload))
    assert result.verdict == "repaired"


def test_unrepairable_response_needs_no_command_or_variant(tmp_path):
    plan, _ = _fixture(tmp_path)
    payload = _proposal().model_dump(mode="json", by_alias=True)
    payload.update(
        repairable=False,
        command=None,
        model=None,
        tests=[],
        mutant_ids=[],
        unrepairable=[
            {"mutant_id": MUTANT, "kind": "equivalent", "reason": "Same task behavior"}
        ],
    )
    result, _, calls = _execute(tmp_path, plan, response=json.dumps(payload))
    assert result.verdict == "unrepairable"
    assert result.variant_record is None
    assert calls == ["model"]


@pytest.mark.parametrize("invalid", ["missing-command", "unsafe-call"])
def test_invalid_assertion_proposal_is_blocked_not_task_unrepairable(tmp_path, invalid):
    plan, _ = _fixture(tmp_path)
    payload = _proposal().model_dump(mode="json", by_alias=True)
    if invalid == "missing-command":
        payload["command"] = None
    else:
        payload["tests"][0]["code"] = (
            "import os\n\ndef test_command():\n    assert os.system('true') == 0\n"
        )
    result, _, calls = _execute(tmp_path, plan, response=json.dumps(payload))
    assert result.verdict == "blocked"
    assert result.variant_record is None
    assert calls == ["model"]


def test_benign_identifier_suffix_is_not_an_os_system_call(tmp_path):
    plan, _ = _fixture(tmp_path)
    payload = _proposal().model_dump(mode="json", by_alias=True)
    payload["tests"][0]["code"] = (
        "from types import SimpleNamespace\nfrom answer import add\n\n"
        "def test_sum():\n    infos = SimpleNamespace(system=add(2, 3))\n"
        "    assert infos.system == 5\n"
    )
    result, _, _ = _execute(tmp_path, plan, response=json.dumps(payload))
    assert result.verdict == "repaired"


@pytest.mark.parametrize(
    ("outcomes", "status", "declared", "verdict"),
    [
        ((1.0, 0.0, 0.0), "ok", False, "repaired"),
        ((0.0, 0.0, 0.0), "mismatch", False, "rejected"),
        ((1.0, 1.0, 0.0), "mismatch", False, "rejected"),
        ((1.0, 0.0, 1.0), "mismatch", False, "rejected"),
        ((1.0, 0.0, 1.0), "mismatch", True, "partial"),
        ((1.0, 0.0, None), "infra", False, "blocked"),
        ((1.0, 0.0, 0.5), "mismatch", False, "blocked"),
        ((1.0, 0.0, 0.0), "planned", False, "blocked"),
    ],
)
def test_real_derivation_obeys_complete_validation_gate(
    tmp_path, outcomes, status, declared, verdict
):
    plan, _ = _fixture(tmp_path)
    before = compute_task_digests(plan.package_dir)
    result, _, _ = _execute(tmp_path, plan, outcomes=outcomes, status=status, declared=declared)
    assert result.verdict == verdict
    assert compute_task_digests(plan.package_dir) == before
    record = json.loads((tmp_path / result.variant_record).read_text())
    assert record["components_changed"] == ["verifier"]
    assert record["parent"]["digest"] == before.package
    expected_status = (
        "validated"
        if verdict == "repaired"
        else "rejected"
        if verdict == "rejected"
        else "candidate"
    )
    assert record["status"] == expected_status


@pytest.mark.parametrize(
    ("executed", "outcome", "prior_reward"),
    [(False, "survived", 1.0), (True, "partial", 0.5)],
)
def test_repair_cannot_certify_only_the_ranked_survivor_subset(
    tmp_path, executed, outcome, prior_reward
):
    plan, report = _fixture(tmp_path)
    extra_id = "def456abc7"
    (plan.run_dir / "capture/testbed/answer.py").write_text(
        "def add(a, b):\n    if a == 0:\n        return b\n    return a + b\n"
    )
    script = "#!/bin/sh\n# omit the zero-input result\n"
    script_path = plan.run_dir / "scripts/zero-input.sh"
    script_path.write_text(script)
    report["mutants"][0]["line"] = 4
    report["mutants"].append(
        {
            **report["mutants"][0],
            "id": extra_id,
            "line": 3,
            "original": "b",
            "replacement": "0",
            "patch": "--- a/answer.py\n+++ b/answer.py\n@@ -3 +3 @@\n-        return b\n+        return 0\n",
            "executed": executed,
        }
    )
    report["runs"].append(
        {
            **report["runs"][-1],
            "name": "zero-input",
            "mutant": extra_id,
            "script": str(script_path.relative_to(tmp_path)),
            "script_sha256": "sha256:" + hashlib.sha256(script.encode()).hexdigest(),
            "outcome": outcome,
            "reward": prior_reward,
        }
    )
    plan.report_path.write_text(json.dumps(report))
    plan = plan_repair(plan.report_path)
    proposal = _proposal()
    proposal.mutant_ids.append(extra_id)
    result, _, _ = _execute(
        tmp_path,
        plan,
        response=proposal.model_dump_json(by_alias=True),
        survivor_rewards={extra_id: 1.0},
    )
    assert result.verdict == "rejected"
    record = json.loads((tmp_path / result.variant_record).read_text())
    assert record["status"] == "rejected"


def test_unexecuted_survivors_alone_do_not_admit_a_task_for_repair(tmp_path):
    plan, report = _fixture(tmp_path)
    report["mutants"][0]["executed"] = False
    plan.report_path.write_text(json.dumps(report))
    with pytest.raises(RepairError, match="no executed guided survivor"):
        plan_repair(plan.report_path)


def test_report_without_trusted_coverage_or_controls_cannot_be_repaired(tmp_path):
    plan, report = _fixture(tmp_path)
    report["runs"][0]["reward"] = 0.0
    plan.report_path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        plan_repair(plan.report_path)
    report["runs"][0]["reward"] = 1.0
    report["coverage"] = "unavailable"
    plan.report_path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        plan_repair(plan.report_path)


def test_drifted_parent_is_refused_before_model_call(tmp_path):
    plan, _ = _fixture(tmp_path)
    (plan.package_dir / "instruction.md").write_text("A different task")
    with pytest.raises(RepairError):
        execute_repair(
            plan,
            model="gpt-6.1-sol",
            repo_root=tmp_path,
            output_dir=tmp_path / "attempt",
            quota_checker=lambda: QuotaStatus(True, 0.0),
            model_caller=lambda *_: pytest.fail("drift must not spend"),
        )


def test_tampered_survivor_is_not_replayed(tmp_path):
    plan, _ = _fixture(tmp_path)
    (plan.run_dir / "scripts/mutant.sh").write_text("a different submission")
    result, _, calls = _execute(tmp_path, plan)
    assert result.verdict == "blocked"
    assert calls == ["model"]


def test_finished_attempt_cannot_spend_or_overwrite_evidence_again(tmp_path):
    plan, _ = _fixture(tmp_path)
    _, directory, _ = _execute(tmp_path, plan)
    receipt = (directory / "repair-result.json").read_bytes()
    with pytest.raises(RepairError):
        execute_repair(
            plan,
            model="gpt-6.1-sol",
            repo_root=tmp_path,
            output_dir=directory,
            quota_checker=lambda: QuotaStatus(True, 0.0),
            model_caller=lambda *_: pytest.fail("repeat must not spend"),
        )
    assert (directory / "repair-result.json").read_bytes() == receipt


@pytest.mark.parametrize("usage", [0.60001, 1.0, float("nan"), -0.1])
def test_quota_unavailable_or_above_ceiling_refuses(usage):
    with pytest.raises(QuotaBlocked):
        ensure_quota(lambda: QuotaStatus(True, usage))


def test_exact_quota_boundary_is_allowed_but_overflow_is_not():
    assert ensure_quota(lambda: QuotaStatus(True, 0.6)).weekly_used == 0.6
    with pytest.raises(QuotaBlocked):
        ensure_quota(lambda: QuotaStatus(False, 0.1))


def test_quota_receipt_does_not_export_account_details():
    record = persisted_quota_record(QuotaStatus(True, 0.1, detail="account-secret"), "gpt-6.1-sol")
    assert "account-secret" not in json.dumps(record)


@pytest.mark.parametrize(
    ("original", "new_code", "expected"),
    [("exit 0", 0, 0), ("exit 9", 0, 9), ("exit 0", 7, 1), ("set -e; false", 0, 1)],
)
def test_wrapper_runs_additions_despite_original_exit_and_preserves_failures(
    tmp_path, original, new_code, expected
):
    wrapper = build_command_wrapper(original, f"touch ran; exit {new_code}", workdir=str(tmp_path))
    # The proposed command is a child interpreter invocation in production.
    wrapper = wrapper.replace(f"touch ran; exit {new_code}", f"sh -c 'touch ran; exit {new_code}'")
    result = subprocess.run(["sh", "-c", wrapper], cwd=tmp_path, check=False)
    assert result.returncode == expected
    assert (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    "path",
    ["../test_bad.py", "/tmp/test_bad.py", "src/answer.py", "tests/test_old.py", "test_bad.py\nx"],
)
def test_proposal_cannot_replace_production_or_existing_tests(tmp_path, path):
    plan, _ = _fixture(tmp_path)
    proposal = _proposal()
    proposal.tests[0].repo_path = path
    proposal.command = f"python -m pytest {path}"
    with pytest.raises(RepairError):
        validate_proposal(proposal, mutant_ids=[MUTANT], package_dir=plan.package_dir)


@pytest.mark.parametrize(
    "code",
    [
        "import inspect\ndef test_fp():\n    assert '+' in inspect.getsource(add)\n",
        "from transformers import AutoModel\n"
        "def test_weights():\n    assert AutoModel.from_pretrained('weights') is not None\n",
    ],
)
def test_proposals_cannot_fingerprint_source_or_load_model_weights(tmp_path, code):
    plan, _ = _fixture(tmp_path)
    proposal = _proposal()
    proposal.tests[0].code = code
    with pytest.raises(RepairError):
        validate_proposal(proposal, mutant_ids=[MUTANT], package_dir=plan.package_dir)


def test_failed_reference_prevents_survivor_trials(tmp_path):
    plan, _ = _fixture(tmp_path)
    result, _, calls = _execute(tmp_path, plan, outcomes=(0.0, 0.0, 0.0))
    assert result.verdict == "rejected"
    assert calls == ["model", "runner"]
    assert result.checks[-1].status == "skipped"
