"""Consumer-visible repair boundaries; all model, quota and runtime I/O is injected."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
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
    RepairTestEdit,
    build_command_wrapper,
    derive_repair_variant,
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
    tmp_path,
    plan,
    *,
    outcomes=(1.0, 0.0, 0.0),
    status="ok",
    declared=False,
    response=None,
    survivor_rewards=None,
):
    proposal = _proposal()
    survivor_rewards = survivor_rewards or {}
    if declared:
        from evallab.verifier_repair import RepairUnrepairable

        proposal.mutant_ids = []
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
        unrepairable=[{"mutant_id": MUTANT, "kind": "equivalent", "reason": "Same task behavior"}],
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


def test_wrapper_runs_bare_python_with_the_task_interpreter(tmp_path):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    interpreter = bindir / "python3"
    interpreter.write_text("#!/bin/sh\nprintf '%s\\n' task-venv > \"$1\"\n", encoding="utf-8")
    interpreter.chmod(0o755)
    marker = tmp_path / "which"
    wrapper = build_command_wrapper(
        "exit 0",
        f"python {marker}",
        workdir=str(tmp_path),
        interpreter=str(interpreter),
    )
    result = subprocess.run(["sh", "-c", wrapper], cwd=tmp_path, check=False)
    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "task-venv\n"


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


@pytest.mark.parametrize("defect", ["duplicate", "unknown", "overlap"])
def test_survivor_dispositions_must_be_unique_known_and_disjoint(tmp_path, defect):
    from evallab.verifier_repair import RepairUnrepairable

    plan, _ = _fixture(tmp_path)
    proposal = _proposal()
    if defect == "duplicate":
        proposal.mutant_ids.append(MUTANT)
    elif defect == "unknown":
        proposal.mutant_ids.append("unknown-survivor")
    else:
        proposal.unrepairable = [
            RepairUnrepairable(mutant_id=MUTANT, reason="Equivalent input domain")
        ]
    with pytest.raises(RepairError):
        validate_proposal(proposal, mutant_ids=[MUTANT], package_dir=plan.package_dir)


def _overconstrained_fixture(tmp_path):
    """A real patch/command fixture: either list or tuple is prompt-valid."""
    package = tmp_path / "overconstrained"
    (package / "tests").mkdir(parents=True)
    (package / "solution").mkdir()
    (package / "task.toml").write_text(
        '[task]\nname="demo/overconstrained"\n[environment]\nworkdir="/testbed"\n'
    )
    requirement = "Return sorted unique integers as a list or tuple, without changing the input."
    (package / "instruction.md").write_text(requirement + "\n")
    (package / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (package / "tests/test.sh").write_text("git apply /tests/test.patch\n")
    before = (
        "def test_unique_sorted():\n"
        "    result = normalize([3, 1, 3, 2])\n"
        "    assert result == [1, 2, 3]\n"
    )
    after = (
        "def test_unique_sorted():\n"
        "    values = [3, 1, 3, 2]\n"
        "    result = normalize(values)\n"
        "    assert list(result) == [1, 2, 3]\n"
    )
    imports = "from answer import normalize\n\n"
    # Separate hunks exercise count changes and subsequent destination offsets.
    unrelated = (
        "# Baseline file spacing\n" * 24
        + "\ndef test_unrelated_input_is_not_changed():\n"
        "    values = [3, 1]\n"
        "    normalize(values)\n"
        "    assert values == [3, 1]\n"
    )
    base = imports + unrelated
    fixed = imports + before + "\n" + unrelated + "\n\ndef test_empty():\n    assert not normalize([])\n"
    patch = "diff --git a/tests/test_contract.py b/tests/test_contract.py\n" + "".join(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile="a/tests/test_contract.py",
            tofile="b/tests/test_contract.py",
        )
    )
    (package / "tests/test.patch").write_text(patch)
    (package / "tests/test_command.sh").write_text(
        f"{shlex.quote(sys.executable)} -m pytest tests/test_contract.py -q\n"
        "CHECK_RC=$?\nprintf 'full command ran\\n'\nexit \"$CHECK_RC\"\n"
    )
    proposal = RepairProposal(
        repairable=True,
        rationale="The instruction permits tuple output, not just list output.",
        edits=[
            RepairTestEdit(
                repo_path="tests/test_contract.py",
                before=before,
                after=after,
                prompt_requirement=requirement,
                rationale="Compare the required values without forcing the optional list representation.",
            )
        ],
        test_patch_sha256="sha256:" + hashlib.sha256(patch.encode()).hexdigest(),
    )
    return package, base, proposal


def _derive_overconstraint(tmp_path, package, proposal):
    return derive_repair_variant(
        package,
        proposal,
        allow_test_edits=True,
        repo_root=tmp_path,
        derive_fn=lambda parent, **kwargs: derive_task(
            parent, variants_root=tmp_path / "variants", **kwargs
        ),
    )


def _grade_contract_fixture(root, package, base, implementation):
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_contract.py").write_text(base)
    (root / "answer.py").write_text(implementation)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "apply", str(package / "tests/test.patch")],
        cwd=root, check=True, capture_output=True, text=True,
    )
    return subprocess.run(
        ["sh", "-c", (package / "tests/test_command.sh").read_text()],
        cwd=root,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )


def test_replacement_accepts_prompt_valid_alternate_but_rejects_wrong_behavior(tmp_path):
    package, base, proposal = _overconstrained_fixture(tmp_path)
    parent_digest = compute_task_digests(package)
    alternate = "def normalize(values):\n    return tuple(sorted(set(values)))\n"
    original = _grade_contract_fixture(tmp_path / "original", package, base, alternate)
    assert original.returncode != 0, original.stdout + original.stderr
    validate_proposal(proposal, mutant_ids=[], package_dir=package, allow_test_edits=True)
    record = _derive_overconstraint(tmp_path, package, proposal)
    variant = materialize(
        record, package, repo_root=tmp_path, variants_root=tmp_path / "variants"
    )
    for name, implementation, expected in (
        ("reference", "def normalize(values):\n    return sorted(set(values))\n", 0),
        ("alternate", alternate, 0),
        ("wrong", "def normalize(values):\n    return tuple(sorted(values))\n", 1),
        (
            "break-unrelated",
            "def normalize(values):\n"
            "    result = sorted(set(values))\n"
            "    values.clear()\n"
            "    return tuple(result)\n",
            1,
        ),
    ):
        result = _grade_contract_fixture(tmp_path / name, variant, base, implementation)
        assert result.returncode == expected, result.stdout + result.stderr
        assert "full command ran" in result.stdout
    assert compute_task_digests(package) == parent_digest
    assert record.components_changed == ["verifier"]
    assert record.status == "candidate"  # Local fixture execution is not native certification.
    assert record.transform == "verifier-repair@1"
    assert record.inputs["edits"][0] == proposal.edits[0].model_dump(mode="json")
    assert record.inputs["test_patch_sha256"] == proposal.test_patch_sha256
    assert (variant / "tests/test_command.sh").read_bytes() == (
        package / "tests/test_command.sh"
    ).read_bytes()


@pytest.mark.parametrize("boundary", ["validate", "derive"])
def test_existing_test_edits_need_opt_in_at_every_public_boundary(tmp_path, boundary):
    package, _, proposal = _overconstrained_fixture(tmp_path)
    with pytest.raises(RepairError, match="opt-in"):
        if boundary == "validate":
            validate_proposal(proposal, mutant_ids=[], package_dir=package)
        else:
            derive_repair_variant(package, proposal, repo_root=tmp_path)
    assert not (tmp_path / "library/task-variants").exists()


@pytest.mark.parametrize(
    "defect",
    [
        "stale-digest", "missing-digest", "wrong-preimage", "unquoted-requirement",
        "blank-rationale", "production", "command-artifact", "unknown-test",
        "duplicate", "rename", "signature", "decorator", "extra-module-code",
        "no-checks", "literal-check", "constant-comparison", "skip", "catch",
        "early-return", "generator", "source-fingerprint", "command-replacement",
        "too-large", "no-op",
    ],
)
def test_overconstraint_edits_fail_closed_before_derivation(tmp_path, defect):
    package, _, proposal = _overconstrained_fixture(tmp_path)
    edit = proposal.edits[0]
    if defect == "stale-digest":
        proposal.test_patch_sha256 = "sha256:" + "0" * 64
    elif defect == "missing-digest":
        proposal.test_patch_sha256 = None
    elif defect == "wrong-preimage":
        edit.before = edit.before.replace("[3, 1, 3, 2]", "[9, 8]")
    elif defect == "unquoted-requirement":
        edit.prompt_requirement = "Require only tuple output."
    elif defect == "blank-rationale":
        edit.rationale = " "
    elif defect == "production":
        edit.repo_path = "answer.py"
    elif defect == "command-artifact":
        edit.repo_path = "tests/test_command.sh"
    elif defect == "unknown-test":
        edit.repo_path = "tests/test_unknown.py"
    elif defect == "duplicate":
        proposal.edits.append(edit.model_copy(deep=True))
    elif defect == "rename":
        edit.after = edit.after.replace("test_unique_sorted", "test_other")
    elif defect == "signature":
        edit.after = edit.after.replace("test_unique_sorted()", "test_unique_sorted(fixture)")
    elif defect == "decorator":
        edit.after = "@pytest.mark.skip\n" + edit.after
    elif defect == "extra-module-code":
        edit.after += "\nnormalize = lambda values: [1, 2, 3]\n"
    elif defect == "no-checks":
        edit.after = "def test_unique_sorted():\n    pass\n"
    elif defect == "literal-check":
        edit.after = "def test_unique_sorted():\n    assert True\n"
    elif defect == "constant-comparison":
        edit.after = "def test_unique_sorted():\n    assert 1 == 1\n"
    elif defect == "skip":
        edit.after = edit.after.replace("    values =", "    pytest.skip('optional')\n    values =")
    elif defect == "catch":
        edit.after = (
            "def test_unique_sorted():\n    try:\n"
            "        assert list(normalize([3, 1, 3, 2])) == [1, 2, 3]\n"
            "    except AssertionError:\n        pass\n"
        )
    elif defect == "early-return":
        edit.after = edit.after.replace("    values =", "    if True:\n        return\n    values =")
    elif defect == "generator":
        edit.after = edit.after.replace("    values =", "    yield None\n    values =")
    elif defect == "source-fingerprint":
        edit.after = edit.after.replace(
            "    values =", "    import inspect\n    inspect.getsource(normalize)\n    values ="
        )
    elif defect == "command-replacement":
        proposal.command = "python -m pytest tests/test_contract.py -k nothing"
    elif defect == "too-large":
        edit.after += "#" * 8192 + "\n"
    elif defect == "no-op":
        edit.after = edit.before
    with pytest.raises(RepairError):
        _derive_overconstraint(tmp_path, package, proposal)
    assert not (tmp_path / "library/task-variants").exists()


@pytest.mark.parametrize("defect", ["bad-counts", "symlink", "context-only"])
def test_edit_rewriter_rejects_unsupported_or_unbound_patch_preimages(tmp_path, defect):
    package, _, proposal = _overconstrained_fixture(tmp_path)
    path = package / "tests/test.patch"
    patch = path.read_text()
    if defect == "bad-counts":
        patch = re.sub(r"(@@ -\d+),\d+", r"\1,99999", patch, count=1)
    elif defect == "symlink":
        patch = patch.replace(
            "diff --git a/tests/test_contract.py b/tests/test_contract.py\n",
            "diff --git a/tests/test_contract.py b/tests/test_contract.py\n"
            "new file mode 120000\n",
        )
    else:
        # A same-named baseline test is not an authorized hidden addition.
        proposal.edits[0].before = (
            "def test_unrelated_input_is_not_changed():\n"
            "    values = [3, 1]\n"
            "    normalize(values)\n"
            "    assert values == [3, 1]\n"
        )
        proposal.edits[0].after = proposal.edits[0].before.replace("[3, 1]", "[4, 2]")
    path.write_text(patch)
    proposal.test_patch_sha256 = "sha256:" + hashlib.sha256(patch.encode()).hexdigest()
    with pytest.raises(RepairError):
        _derive_overconstraint(tmp_path, package, proposal)


def test_edits_and_additions_both_run_with_original_command_and_failure_semantics(tmp_path):
    package, base, proposal = _overconstrained_fixture(tmp_path)
    root = tmp_path / "mixed-grade"
    (package / "task.toml").write_text(
        f'[task]\nname="demo/overconstrained"\n[environment]\nworkdir="{root}"\n'
    )
    proposal.edits[0].after = (
        "def test_unique_sorted():\n"
        "    values = [3, 1, 3, 2]\n"
        "    try:\n"
        "        assert list(normalize(values)) == [1, 2, 3]\n"
        "    finally:\n"
        "        values.clear()\n"
    )
    proposal.tests = [
        RepairTest(
            id="negatives",
            repo_path="tests/test_negative.py",
            code=(
                "from answer import normalize\n\n"
                "def test_negative_values():\n"
                "    assert list(normalize([-3, -1, -3])) == [-3, -1]\n"
            ),
        )
    ]
    proposal.command = f"{shlex.quote(sys.executable)} -m pytest tests/test_negative.py -q"
    record = _derive_overconstraint(tmp_path, package, proposal)
    variant = materialize(
        record, package, repo_root=tmp_path, variants_root=tmp_path / "variants"
    )
    good = _grade_contract_fixture(
        root, variant, base, "def normalize(values):\n    return tuple(sorted(set(values)))\n"
    )
    assert good.returncode == 0, good.stdout + good.stderr
    (root / "answer.py").write_text(
        "def normalize(values):\n    return tuple(sorted({abs(value) for value in values}))\n"
    )
    wrong = subprocess.run(
        ["sh", "-c", (variant / "tests/test_command.sh").read_text()],
        cwd=root,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode != 0, wrong.stdout + wrong.stderr
    assert "full command ran" in wrong.stdout
