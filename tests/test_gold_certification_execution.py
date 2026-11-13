"""Offline gate, executor-seam and durable spend admission coverage."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import gold_certifier as gold
from evallab.mutant_auditor import PROBE_DONE_MARKER, PROBE_MARKER, mutants_for_patch
from evallab.registry import task_directory_digest
from evallab.regrade import SpendCapReached

SOURCE = "def positive(x):\n    if x > 0:\n        return 1\n    return 0\n"
PATCH = "--- a/widget.py\n+++ b/widget.py\n@@ -0,0 +1,4 @@\n+def positive(x):\n+    if x > 0:\n+        return 1\n+    return 0\n"
CODE = "import unittest\nclass Contract(unittest.TestCase):\n    def test_positive(self):\n        from widget import positive\n        self.assertEqual(positive(1), 1)\n"


def package_at(path: Path) -> Path:
    path.mkdir()
    (path / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    (path / "instruction.md").write_text("Implement positive(x).")
    (path / "tests").mkdir()
    (path / "tests/test.sh").write_text('RC=0\ntail -c 12000 "$V/test_output.log"\n')
    return path


def matrix(tmp_path: Path, alternatives: list[str] | None = None):
    package = package_at(tmp_path / "base")
    upgraded = package_at(tmp_path / "upgraded")
    train, evals = gold.split_mutants(mutants_for_patch(PATCH, {"widget.py": SOURCE}, limit=4))
    cells = gold.plan_certification_cells(
        package=package, upgraded=upgraded, reference_patch=PATCH,
        train=train, evals=evals, alternatives=alternatives or [], witnesses=[],
    )
    # Assessment fixtures represent existing receipts, not executable injection plans.
    cells.extend([
        gold.CertificationCell("witness-recorded", "witness", upgraded, "", "RECORDED"),
        gold.CertificationCell("exit0_pth", "exit0_pth", upgraded, "", "RECORDED"),
    ])
    outcomes = {c.name: {"status": "executed", "agent_log": c.marker,
                        "reward": 1.0 if c.arm in ("reference", "alternative", "evalbase") else 0.0}
                for c in cells}
    return cells, outcomes, upgraded


def summarize(cells, outcomes, upgraded):
    return gold.summarize_certification(task_id="synthetic", upgraded=upgraded,
                                       test_lines=5, cells=cells, outcomes=outcomes)


@pytest.mark.parametrize("alt_rewards,expected,status", [
    ([1.0, 1.0], 2, "certified"), ([1.0, 0.0], 1, "failed"),
    ([], None, "provisional"), ([1.0], 1, "provisional"),
    ([None, None], None, "failed"), ([0.0, 0.0], 0, "failed"),
    ([0.0, None], None, "failed"), ([1.0, None], None, "failed"),
])
def test_gate7_preserves_each_outcome(tmp_path, alt_rewards, expected, status):
    cells, outcomes, upgraded = matrix(tmp_path, [f"correct-{i}" for i in range(len(alt_rewards))])
    for cell, reward in zip((c for c in cells if c.arm == "alternative"), alt_rewards, strict=True):
        outcomes[cell.name]["reward"] = reward
    result = summarize(cells, outcomes, upgraded)
    assert result.alternative_solutions_total == (len(alt_rewards) or None)
    assert result.alternative_solutions_accepted == expected
    assert list(result.alternative_solution_rewards.values()) == alt_rewards
    assert result.status == status
    assert result.reference_reward == 1.0
    assert result.summary()["status"] == status
    if 0.0 in alt_rewards:
        assert result.gate7_alternatives_passed is False
    if alt_rewards and None in alt_rewards:
        assert gold.GoldMatrixSummary([result]).to_report_dict()["gate7_alternative_solutions"]["accepted"] is None


@pytest.mark.parametrize("arm", ["reference", "nop", "witness", "exit0_pth", "train", "eval", "evalbase"])
def test_each_gate_failure_blocks_certification(tmp_path, arm):
    cells, outcomes, upgraded = matrix(tmp_path, ["correct-a", "correct-b"])
    cell = next(c for c in cells if c.arm == arm)
    if arm == "evalbase":
        outcomes[cell.name].update(status="failed", reward=None)
    else:
        outcomes[cell.name]["reward"] = 0.0 if arm == "reference" else 1.0
    result = summarize(cells, outcomes, upgraded)
    assert result.status == "failed"


@pytest.mark.parametrize("bad", ["missing", "unapplied", "infra", "nan", "fraction", "boolean"])
@pytest.mark.parametrize("arm", ["eval", "evalbase"])
def test_bad_cell_never_counts_as_mutant_kill(tmp_path, bad, arm):
    cells, outcomes, upgraded = matrix(tmp_path, ["correct-a", "correct-b"])
    cell = next(c for c in cells if c.arm == arm)
    if bad == "missing":
        del outcomes[cell.name]
    elif bad == "unapplied":
        outcomes[cell.name]["agent_log"] = ""
    elif bad == "infra":
        outcomes[cell.name]["status"] = "failed"
    else:
        outcomes[cell.name]["reward"] = {"nan": float("nan"), "fraction": 0.5, "boolean": False}[bad]
    result = summarize(cells, outcomes, upgraded)
    if arm == "eval":
        assert result.eval_rewards[cell.key] is None
        assert result.eval_mutants_killed == result.eval_mutants_total - 1
    else:
        assert result.eval_base_rewards[cell.key] is None
        assert result.eval_mutants_base_accepted == result.eval_mutants_total - 1
        assert result.eval_mutants_base_accepted_killed == result.eval_mutants_total - 1
    assert result.status == "failed"


def test_multiple_witnesses_all_required(tmp_path):
    cells, outcomes, upgraded = matrix(tmp_path)
    cells.append(gold.CertificationCell("witness-extra", "witness", upgraded, "", "APPLIED"))
    result = summarize(cells, outcomes, upgraded)
    assert not result.gate3_witness_passed
    assert result.witness_rewards["witness-extra"] is None


def fake_cell(cell_dir, package, script):
    if cell_dir.name == "probe":
        log = f"{PROBE_MARKER} widget.py {base64.b64encode(SOURCE.encode()).decode()}\n{PROBE_DONE_MARKER}"
        reward = 1.0
    else:
        markers = re.findall(r"^echo ((?:REFERENCE|NOP|WITNESS|EXIT0_PTH|MUTANT)_APPLIED[^\n]*)", script, re.M)
        log = "\n".join(markers)
        reward = 1.0 if cell_dir.name == "reference" or cell_dir.name.startswith(("alternative", "evalbase")) else 0.0
    return {"reward": reward, "status": "executed", "agent_log": log}


def fake_stage(package, upgraded, code, hook, **options):
    assert options["single_container"] is False
    shutil.copytree(package, upgraded)
    (upgraded / "tests/test_gold_contract.py").write_text(code)
    return SimpleNamespace(to_json=lambda: {"runner": "synthetic", "output": str(upgraded)}), "hook"


def test_certify_executes_and_persists_digest_bound_result(tmp_path):
    base = package_at(tmp_path / "base")
    prompts = []

    def synthesize(prompt, output):
        prompts.append(prompt)
        train, evals = gold.split_mutants(mutants_for_patch(PATCH, {"widget.py": SOURCE}, limit=4))
        assert train and evals
        assert all(gold.mutant_diff(m, SOURCE) not in prompt for m in evals)
        return CODE

    result = gold.certify_task(
        task_id="synthetic", package=base, reference_patch=PATCH, out_dir=tmp_path / "out",
        run_cell=fake_cell, synthesize=synthesize, stage=fake_stage, limit=4,
        alternative_patches=["correct-a", "correct-b"],
    )
    assert len(prompts) == 1
    assert result.status == "failed"
    assert result.gate1_reference_passed and result.gate2_nop_passed
    assert result.train_mutants_killed == result.train_mutants_total
    assert result.eval_mutants_killed == result.eval_mutants_total
    assert not result.gate3_witness_passed and not result.gate4_exit0_pth_passed
    assert result.witness_rewards == {}
    assert result.witness_reward is None and result.exit0_pth_reward is None
    plan = json.loads((tmp_path / "out/plan.json").read_text())
    assert {c["arm"] for c in plan} == {"reference", "nop", "train", "eval", "evalbase", "alternative"}
    assert {p.name for p in (tmp_path / "out/cells").iterdir()} == {"probe", *(c["name"] for c in plan)}
    receipt = json.loads((tmp_path / "out/certification.json").read_text())
    assert receipt == result.summary()
    assert receipt["package_digest"] == task_directory_digest(tmp_path / "out/package")
    assert receipt["alternative_solutions_accepted"] == 2
    assert (tmp_path / "out/cells/probe/result.json").is_file()
    assert json.loads((tmp_path / "out/summary.json").read_text())["gate7_alternative_solutions"]["accepted"] == 2


def test_mid_matrix_failure_persists_unmeasured_cells(tmp_path):
    base = package_at(tmp_path / "base")

    def run(cell_dir, package, script):
        if cell_dir.name.startswith("train"):
            raise SpendCapReached("over cap")
        return fake_cell(cell_dir, package, script)

    with pytest.raises(SpendCapReached, match="over cap"):
        gold.certify_task(task_id="synthetic", package=base, reference_patch=PATCH,
                          out_dir=tmp_path / "out", run_cell=run, synthesize=lambda *_: CODE,
                          stage=fake_stage, limit=4, alternative_patches=["correct-a", "correct-b"])
    result = json.loads((tmp_path / "out/certification.json").read_text())
    assert result["status"] == "failed"
    assert result["eval_mutants_total"] > 0
    assert result["eval_mutants_killed"] == 0
    assert result["alternative_solutions_accepted"] is None
    assert set(result["alternative_solution_rewards"].values()) == {None}


@pytest.mark.parametrize("alternatives", [[PATCH], ["same", "same"]])
def test_duplicate_alternatives_refused_before_execution(tmp_path, alternatives):
    base = package_at(tmp_path / "base")
    with pytest.raises(ValueError, match="distinct"):
        gold.certify_task(task_id="synthetic", package=base, reference_patch=PATCH,
                          out_dir=tmp_path / "out", run_cell=lambda *_: pytest.fail("executed"),
                          synthesize=lambda *_: CODE, alternative_patches=alternatives)
    assert not (tmp_path / "out").exists()


def test_existing_output_is_never_overwritten(tmp_path):
    base = package_at(tmp_path / "base")
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("evidence")
    with pytest.raises(FileExistsError):
        gold.certify_task(task_id="synthetic", package=base, reference_patch=PATCH,
                          out_dir=out, run_cell=fake_cell, synthesize=lambda *_: CODE)
    assert (out / "keep").read_text() == "evidence"


def cli_args(tmp_path, *extra):
    from evallab.reward_hack_parser import register_parser as register_reward_hack

    base = package_at(tmp_path / "base")
    patch = tmp_path / "ref.patch"
    patch.write_text(PATCH)
    parser = argparse.ArgumentParser()
    register_reward_hack(parser.add_subparsers())
    return parser.parse_args(["certify-gold", "--package", str(base), "--reference-patch", str(patch),
                              "--out", str(tmp_path / "out"), *extra])


@pytest.mark.parametrize("extra,message", [([], "--approval"), (["--approval", "approved test"], "--max-usd")])
def test_cli_refuses_unapproved_or_uncapped_execution(tmp_path, extra, message, monkeypatch):
    args = cli_args(tmp_path, *extra)
    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: pytest.fail("executor created"))
    with pytest.raises(ValueError, match=message):
        gold._certify_gold_command(args, tmp_path)
    assert not (tmp_path / "out").exists()


def test_cli_spend_cap_reserves_durably_before_executor(tmp_path, monkeypatch):
    args = cli_args(tmp_path, "--approval", "approved synthetic test", "--max-usd", "0.004")
    started = []

    def runner(**kwargs):
        gate = kwargs["spend_gate"]
        assert gate.spend_ledger == tmp_path / "out.spend.json"
        def run(*cell):
            gate.charge()
            started.append(cell)
            return fake_cell(*cell)
        return run

    monkeypatch.setattr(gold, "modal_cell_runner", runner)
    with pytest.raises(SpendCapReached):
        gold._certify_gold_command(args, tmp_path)
    assert not started


def test_cli_success_uses_hardened_package_and_reports_wrong_rejection(tmp_path, monkeypatch, capsys):
    args = cli_args(tmp_path, "--approval", "approved synthetic test", "--max-usd", "1")
    contract = tmp_path / "contract.py"
    contract.write_text(CODE)
    alt_a, alt_b = tmp_path / "a.patch", tmp_path / "b.patch"
    alt_a.write_text("correct-a")
    alt_b.write_text("correct-b")
    args.contract_test = contract
    args.alt_solution = [alt_a, alt_b]
    real_certify = gold.certify_task

    def runner(**kwargs):
        def run(cell_dir, package, script):
            kwargs["spend_gate"].charge()
            result = fake_cell(cell_dir, package, script)
            if cell_dir.name == "alternative-1":
                assert package == tmp_path / "out/package"
                result["reward"] = 0.0
            return result
        return run

    monkeypatch.setattr(gold, "modal_cell_runner", runner)
    monkeypatch.setattr(gold, "synthesize_contract", lambda *_a, **_kw: pytest.fail("unexpected model call"))
    monkeypatch.setattr(gold, "certify_task", lambda **kw: real_certify(**kw, stage=fake_stage))
    assert gold._certify_gold_command(args, tmp_path) == 1
    assert "WRONG REJECTION" in capsys.readouterr().out
    ledger = json.loads((tmp_path / "out.spend.json").read_text())
    assert ledger["reserved_usd"] > 0
    receipt = json.loads((tmp_path / "out/certification.json").read_text())
    assert receipt["alternative_solutions_total"] == 2
    assert receipt["alternative_solutions_accepted"] == 1


def test_recorded_research_cells_reproduce_eight_task_results():
    receipts = Path(__file__).resolve().parents[1] / "research/experiments/ov-gold-certifier-8-20261101/results"
    with (receipts / "cells.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    expected = json.loads((receipts / "summary.json").read_text())
    results = []
    arms = {"train_mutant": "train", "eval_mutant": "eval", "eval_base_mutant": "evalbase"}
    for task in expected["tasks"]:
        cells, outcomes = [], {}
        for row in rows:
            if row["task"] != task["task"]:
                continue
            arm = arms.get(row["arm_kind"], row["arm_kind"])
            key = row["cell"].split("-", 1)[1] if arm in ("train", "eval", "evalbase") else ""
            cells.append(gold.CertificationCell(row["cell"], arm, Path("unused"), "", "APPLIED", key))
            outcomes[row["cell"]] = {"status": row["status"], "reward": float(row["reward"]),
                                     "agent_log": "APPLIED" if row["applied"] == "True" else ""}
        result = summarize(cells, outcomes, Path("unused"))
        for actual, field in [("gate1_reference_passed", "gate1_ref"), ("gate2_nop_passed", "gate2_nop"),
                              ("gate3_witness_passed", "gate3_wit"), ("gate4_exit0_pth_passed", "gate4_exit0"),
                              ("train_mutants_killed", "train_killed"), ("train_mutants_total", "train_total"),
                              ("eval_mutants_killed", "eval_killed"), ("eval_mutants_total", "eval_total"),
                              ("eval_mutants_base_accepted", "eval_base_accepted"),
                              ("eval_mutants_base_accepted_killed", "eval_base_accepted_killed")]:
            assert getattr(result, actual) == task[field], (task["task"], field)
        results.append(result)
    report = gold.GoldMatrixSummary(results).to_report_dict()
    assert report["total_tasks"] == expected["n_tasks"]
    for actual, field in [("gate1_reference", "gate1_reference"), ("gate2_nop", "gate2_nop"),
                          ("gate3_witness", "gate3_witness"), ("gate4_exit0_pth", "gate4_exit0_pth"),
                          ("gate5_train_mutants", "gate5_train_mutants"),
                          ("gate6_eval_mutants_base_accepted", "gate6_eval_base_accepted"),
                          ("gate6_eval_mutants_all", "gate6_eval_all")]:
        assert report[actual]["formatted"] == expected[field]


@pytest.mark.parametrize("failure", ["infra", "unapplied", "reference_rejected", "too_few_mutants"])
def test_probe_refuses_before_synthesis_or_hardening(tmp_path, failure):
    base = package_at(tmp_path / "base")

    def run(*cell):
        result = fake_cell(*cell)
        if failure == "infra":
            result["status"] = "failed"
        elif failure == "unapplied":
            result["agent_log"] = ""
        elif failure == "reference_rejected":
            result["reward"] = 0.0
        else:
            source = base64.b64encode(b"pass\n").decode()
            result["agent_log"] = f"{PROBE_MARKER} widget.py {source}\n{PROBE_DONE_MARKER}"
        return result

    with pytest.raises((ValueError, RuntimeError)):
        gold.certify_task(task_id="synthetic", package=base, reference_patch=PATCH,
                          out_dir=tmp_path / "out", run_cell=run,
                          synthesize=lambda *_: pytest.fail("synthesized"),
                          stage=lambda *_args, **_kw: pytest.fail("hardened"))
    assert not (tmp_path / "out/certification.json").exists()
    assert (tmp_path / "out/cells/probe/result.json").exists()


def test_unsupported_reward_hook_refuses_instead_of_appending_after_reward(tmp_path):
    base = package_at(tmp_path / "base")
    (base / "tests/test.sh").write_text('echo 1 > /logs/verifier/reward.txt\n')
    with pytest.raises(ValueError, match="cannot place supplementary test"):
        gold.stage_and_harden_package(base, tmp_path / "out", CODE, "python3")
    assert not (tmp_path / "out").exists()


def test_real_cli_missing_approval_has_same_exit_as_audit_mutants(tmp_path, capsys):
    from evallab.cli import run_cli

    args = cli_args(tmp_path)
    common = ["--package", str(args.package), "--reference-patch", str(args.reference_patch),
              "--out", str(args.out)]
    assert run_cli(["certify-gold", *common]) == 2
    assert "--approval" in capsys.readouterr().err
    assert run_cli(["audit-mutants", *common]) == 2
    assert "--approval" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_evalbase_keys_must_match_heldout_mutants(tmp_path, change):
    cells, outcomes, upgraded = matrix(tmp_path, ["correct-a", "correct-b"])
    result = summarize(cells, outcomes, upgraded)
    assert result.status == "certified"
    if change == "missing":
        del result.eval_base_rewards[next(iter(result.eval_base_rewards))]
    else:
        result.eval_base_rewards["unexpected"] = 0.0
    assert result.status == "failed"


def test_evalbase_measured_rejection_is_valid_not_infrastructure(tmp_path):
    cells, outcomes, upgraded = matrix(tmp_path, ["correct-a", "correct-b"])
    for cell in cells:
        if cell.arm == "evalbase":
            outcomes[cell.name]["reward"] = 0.0
    result = summarize(cells, outcomes, upgraded)
    assert result.status == "certified"
    assert result.eval_mutants_base_accepted == 0


@pytest.mark.parametrize("cap,refused_arm", [("0.012", "probe"), ("0.072", "train"), ("0.117", "evalbase")])
def test_real_modal_runner_unwraps_backend_cap_refusal(tmp_path, monkeypatch, capsys, cap, refused_arm):
    from evallab.cli import run_cli

    args = cli_args(tmp_path, "--approval", "approved synthetic cap test", "--max-usd", cap)
    # Keep projected resource cost below the gate cap, so the native-style
    # reservation refusal (not the independent sandbox meter) is exercised.
    (args.package / "task.toml").write_text(
        '[environment]\nworkdir = "/testbed"\ncpus = 0.1\nmemory_mb = 16\n'
    )
    contract = tmp_path / "contract.py"
    contract.write_text(CODE)
    alts = [tmp_path / f"correct-{i}.patch" for i in range(2)]
    for i, path in enumerate(alts):
        path.write_text(f"correct-{i}")
    calls = []

    class Backend:
        def __init__(self, *, repo_root, spend_gate):
            self.gate = spend_gate

        def run_container_trial(self, package, *, script, trial_dir, **kwargs):
            cell_dir = trial_dir.parent
            calls.append(cell_dir.name)
            try:
                # Match the native adapter's three committed reservations per start.
                for _ in range(3):
                    self.gate.charge()
            except SpendCapReached as exc:
                return SimpleNamespace(status="failed", reason=f"SpendCapReached: {exc}", facts={})
            result = fake_cell(cell_dir, package, script)
            (trial_dir / "agent").mkdir(parents=True)
            (trial_dir / "verifier").mkdir()
            (trial_dir / "agent/oracle.txt").write_text(result["agent_log"])
            (trial_dir / "verifier/test_output.log").write_text("synthetic")
            return SimpleNamespace(status=result["status"], reason=None, facts={"reward": result["reward"]})

    # Leave gold.modal_cell_runner and modal_cells.modal_cell_runner untouched.
    # Only its documented backend dependency is replaced, without importing Harbor.
    monkeypatch.setitem(sys.modules, "evallab.qa.task_modal", SimpleNamespace(ModalBackend=Backend))
    real_certify = gold.certify_task
    monkeypatch.setattr(gold, "certify_task", lambda **kw: real_certify(**kw, stage=fake_stage))
    command = ["certify-gold", "--package", str(args.package), "--reference-patch", str(args.reference_patch),
               "--out", str(args.out), "--approval", args.approval, "--max-usd", cap,
               "--contract-test", str(contract), "--limit", "4",
               "--alt-solution", str(alts[0]), "--alt-solution", str(alts[1])]
    assert run_cli(command) == 2
    assert "SpendCapReached" in capsys.readouterr().err
    assert calls[-1].startswith(refused_arm)
    assert len(set(calls)) == len(calls)
    refusal = args.out / "cells" / calls[-1]
    assert not (refusal / "result.json").exists()
    assert json.loads((refusal / "refusal.json").read_text())["status"] == "failed"
    receipt = json.loads((args.out / "certification.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["alternative_solutions_total"] == 2
    assert receipt["alternative_solutions_accepted"] is None
    assert set(receipt["alternative_solution_rewards"].values()) == {None}
    assert json.loads((args.out / "summary.json").read_text())["tasks"][0] == receipt
    # Partial pre-start reservations remain honest; no later cell consumes the remainder.
    ledger = json.loads((tmp_path / "out.spend.json").read_text())
    assert ledger["reserved_usd"] <= float(cap)
    assert ledger["reserved_usd"] == pytest.approx(len(calls) * 0.015 - 0.005)


def test_synthesis_closes_stdin_and_passes_large_prompt_by_file(tmp_path, monkeypatch):
    prompt = "large synthetic prompt\n" * 40_000

    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["OMP_NO_COLOR"] == "1"
        assert max(map(len, argv)) < 4096
        assert argv[-1] == f"@{tmp_path / 'prompt.txt'}"
        assert Path(argv[-1][1:]).read_text() == prompt
        return SimpleNamespace(returncode=0, stdout=f"```python\n{CODE}```", stderr="")

    monkeypatch.setattr(gold.subprocess, "run", run)
    assert gold.synthesize_contract(prompt, tmp_path, model="synthetic") == CODE
    assert (tmp_path / "synthesis.stdout").read_text().startswith("```python")


def test_patch_sections_strips_fixtures_and_caps_code():
    patch = "".join(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1 @@\n+{body}\n"
        for name, body in [("fixture.json", "JSON_FIXTURE"), ("archive.b64", "BASE64_FIXTURE"),
                           ("mimo_test_command.sh", "SHELL_FIXTURE"), ("test.py", "CODE_SENTINEL")]
    )
    cleaned = gold.patch_sections(patch, lambda path: not gold.STRIP_PATCH_FILES.search(path))
    assert "CODE_SENTINEL" in cleaned
    assert "JSON_FIXTURE" not in cleaned
    assert "BASE64_FIXTURE" not in cleaned
    assert "SHELL_FIXTURE" not in cleaned
    assert gold.patch_sections(PATCH, lambda path: path.endswith(".py")) == PATCH
    huge = patch + "x" * 600_000
    bounded = gold.patch_sections(huge, lambda path: path.endswith(".py"))
    assert len(bounded) <= gold.MAX_PROMPT_SECTION + 20
    assert bounded.endswith("[... truncated]\n")


def test_synthesis_prompt_filters_large_reference_and_hidden_fixtures(tmp_path):
    base = package_at(tmp_path / "base")
    fixture = "diff --git a/fixture.json b/fixture.json\n--- a/fixture.json\n+++ b/fixture.json\n@@ -0,0 +1 @@\n+"
    hidden = "diff --git a/test_widget.py b/test_widget.py\n--- a/test_widget.py\n+++ b/test_widget.py\n@@ -0,0 +1 @@\n+HIDDEN_CODE_SENTINEL\n"
    (base / "tests/test.patch").write_text(fixture + "HIDDEN_JSON_SENTINEL" * 30_000 + "\n" + hidden)
    (base / "tests/extra.py").write_text("# bounded\n" * 30_000)
    large_patch = "diff --git a/widget.py b/widget.py\n" + PATCH + fixture + "REFERENCE_JSON_SENTINEL" * 30_000
    prompts = []

    def synthesize(prompt, _out):
        prompts.append(prompt)
        assert "JSON_SENTINEL" not in prompt
        assert len(prompt) < 100_000
        assert "HIDDEN_CODE_SENTINEL" in prompt
        assert "def positive(x)" in prompt
        return CODE

    gold.certify_task(task_id="synthetic", package=base, reference_patch=large_patch,
                      out_dir=tmp_path / "out", run_cell=fake_cell, synthesize=synthesize, stage=fake_stage, limit=4)
    assert len(prompts) == 1


def test_dotted_output_directories_keep_distinct_default_ledgers(tmp_path, monkeypatch):
    args = cli_args(tmp_path, "--approval", "approved synthetic ledger test", "--max-usd", "1")
    ledgers = []

    def runner(**kwargs):
        ledgers.append(kwargs["spend_gate"].spend_ledger)
        return fake_cell

    real_certify = gold.certify_task
    monkeypatch.setattr(gold, "modal_cell_runner", runner)
    monkeypatch.setattr(gold, "certify_task", lambda **kw: real_certify(**kw, stage=fake_stage))
    monkeypatch.setattr(gold, "synthesize_contract", lambda *_args, **_kwargs: CODE)
    for name in ("gold-v1.1", "gold-v1.2"):
        args.out = tmp_path / name
        assert gold._certify_gold_command(args, tmp_path) == 1
    assert ledgers == [tmp_path / "gold-v1.1.spend.json", tmp_path / "gold-v1.2.spend.json"]


def test_ledger_inside_output_refused_before_executor_creation(tmp_path, monkeypatch):
    args = cli_args(tmp_path, "--approval", "approved synthetic ledger test", "--max-usd", "1")
    args.ledger = args.out / "spend.json"
    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: pytest.fail("executor created"))
    with pytest.raises(ValueError, match="outside"):
        gold._certify_gold_command(args, tmp_path)
    assert not args.out.exists()


@pytest.mark.parametrize("options", [{"injection_controls": True}, {"witnesses": ["operator witness"]}])
def test_injection_request_refused_before_any_certifier_side_effect(tmp_path, options):
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="no enforced contained runner"):
        gold.certify_task(
            task_id="synthetic", package=tmp_path / "absent", reference_patch=PATCH,
            out_dir=out, run_cell=lambda *_: pytest.fail("executed"),
            synthesize=lambda *_: pytest.fail("synthesized"),
            stage=lambda *_args, **_kw: pytest.fail("hardened"), **options,
        )
    assert not out.exists()


@pytest.mark.parametrize("options", [{"injection_controls": True}, {"witnesses": ["operator witness"]}])
def test_planner_refuses_explicit_injection_without_reading_package(tmp_path, options):
    with pytest.raises(ValueError, match="no enforced contained runner"):
        gold.plan_certification_cells(
            package=tmp_path / "absent", upgraded=tmp_path / "absent",
            reference_patch=PATCH, train=[], evals=[], alternatives=[],
            **({"witnesses": []} | options),
        )


def test_safe_plan_never_generates_injection_scripts(tmp_path, monkeypatch):
    for name in ("witness_script", "exit0_pth_script"):
        monkeypatch.setattr(gold, name, lambda *_: pytest.fail("injection script generated"))
    base = package_at(tmp_path / "base")
    result = gold.certify_task(
        task_id="synthetic", package=base, reference_patch=PATCH, out_dir=tmp_path / "out",
        run_cell=fake_cell, synthesize=lambda *_: CODE, stage=fake_stage, limit=4,
        alternative_patches=["correct-a", "correct-b"],
    )
    report = gold.GoldMatrixSummary([result]).to_report_dict()
    for gate in ("gate3_witness", "gate4_exit0_pth"):
        assert report[gate]["passed"] == 0
        assert report[gate]["total"] == 1
    assert result.status == "failed"


def contained_runner(tmp_path):
    from evallab.regrade import CloudSpendGate

    gate = CloudSpendGate(
        approval="offline deterministic test", cap_usd=1, usd_per_execution=0.005,
        modal_app_name="gold-offline-test", billing_tag="gold-offline-test",
    )
    return gold.ContainedModalRunner(
        repo_root=tmp_path, spend_gate=gate, meter=gold.SandboxMeter(1),
    )


def native_receipt_cell(cell_dir, package, script):
    """Provider-free native receipt fixture; never executes the candidate script."""
    result = fake_cell(cell_dir, package, script)
    trial = cell_dir / "trial"
    trial.mkdir(parents=True)
    (trial / "qa-execution.json").write_text(json.dumps({
        "environment": "modal", "mode": "shortcut", "job_path": str(trial),
        "package_digest": task_directory_digest(package),
        "exception": None, "reward": result["reward"],
    }))
    (trial / "egress-lock.json").write_text(json.dumps({
        "schema_version": 2, "requested": True, "error": None,
        "applications": [{
            "source": "creation", "network_mode": "public", "sandbox_id": "sb-offline",
        }],
    }))
    return result


def test_contained_explicit_controls_complete_all_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: native_receipt_cell)
    base = package_at(tmp_path / "base")
    runner = contained_runner(tmp_path)
    result = gold.certify_task(
        task_id="synthetic", package=base, reference_patch=PATCH, out_dir=tmp_path / "out",
        run_cell=runner, synthesize=lambda *_: CODE, stage=fake_stage, limit=4,
        alternative_patches=["correct-a", "correct-b"], injection_controls=True,
    )
    assert result.status == "certified"
    assert result.witness_rewards == {"witness-0": 0.0}
    assert result.exit0_pth_reward == 0.0
    plan = json.loads((tmp_path / "out/plan.json").read_text())
    assert {c["arm"] for c in plan} == {
        "reference", "nop", "train", "eval", "evalbase", "alternative", "witness", "exit0_pth",
    }



@pytest.mark.parametrize("alternatives,status,exit_code", [
    (0, "provisional", 1), (1, "provisional", 1), (2, "certified", 0),
])
def test_cli_success_requires_certified_receipt(tmp_path, monkeypatch, alternatives, status, exit_code):
    args = cli_args(tmp_path, "--approval", "offline deterministic test", "--max-usd", "1",
                    "--injection-controls")
    contract = tmp_path / "contract.py"
    contract.write_text(CODE)
    args.contract_test = contract
    for index in range(alternatives):
        patch = tmp_path / f"alternative-{index}.patch"
        patch.write_text(f"correct-{index}")
        args.alt_solution.append(patch)
    real_certify = gold.certify_task
    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: native_receipt_cell)
    monkeypatch.setattr(gold, "certify_task", lambda **kw: real_certify(**kw, stage=fake_stage))
    monkeypatch.setattr(gold, "synthesize_contract", lambda *_a, **_kw: pytest.fail("model call"))

    assert gold._certify_gold_command(args, tmp_path) == exit_code
    receipt = json.loads((args.out / "certification.json").read_text())
    assert receipt["status"] == status

@pytest.mark.parametrize("damage", [
    "missing", "unknown_provider", "unknown_network", "no_creation", "foreign_package",
    "foreign_trial", "error", "cache", "invalid_json",
])
def test_contained_runner_rejects_missing_or_unknown_native_evidence(tmp_path, monkeypatch, damage):
    def execute(cell_dir, package, script):
        result = native_receipt_cell(cell_dir, package, script)
        trial = cell_dir / "trial"
        lock_path = trial / "egress-lock.json"
        lock = json.loads(lock_path.read_text())
        execution_path = trial / "qa-execution.json"
        execution = json.loads(execution_path.read_text())
        if damage == "missing":
            lock_path.unlink()
        elif damage == "invalid_json":
            lock_path.write_text("[]")
        elif damage in ("unknown_provider", "unknown_network", "no_creation"):
            key, value = {
                "unknown_provider": ("sandbox_id", "docker-host"),
                "unknown_network": ("network_mode", "unknown"),
                "no_creation": ("source", "apply"),
            }[damage]
            lock["applications"][0][key] = value
            lock_path.write_text(json.dumps(lock))
        elif damage == "cache":
            result["cache_hit"] = True
        else:
            key, value = {
                "foreign_package": ("package_digest", "different"),
                "foreign_trial": ("job_path", str(tmp_path / "other")),
                "error": ("exception", "provider failure"),
            }[damage]
            execution[key] = value
            execution_path.write_text(json.dumps(execution))
        return result

    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: execute)
    base = package_at(tmp_path / "base")
    result = contained_runner(tmp_path)(tmp_path / "cell", base, "inert fixture")
    assert result["status"] == "failed" and result["reward"] is None


def test_witness_only_refuses_even_with_native_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(gold, "witness_script", lambda *_: pytest.fail("generated"))
    with pytest.raises(ValueError, match="witnesses alone"):
        gold.certify_task(
            task_id="synthetic", package=tmp_path / "absent", reference_patch=PATCH,
            out_dir=tmp_path / "out", run_cell=contained_runner(tmp_path),
            synthesize=lambda *_: pytest.fail("synthesized"), witnesses=["inert"],
        )
    assert not (tmp_path / "out").exists()


def test_arbitrary_callable_containment_attributes_are_not_authority(tmp_path, monkeypatch):
    def arbitrary(*_):
        pytest.fail("executed")

    arbitrary.contained = True
    arbitrary.environment = "modal"
    monkeypatch.setattr(gold, "witness_script", lambda *_: pytest.fail("generated"))
    monkeypatch.setattr(gold, "exit0_pth_script", lambda *_: pytest.fail("generated"))
    with pytest.raises(ValueError, match="no enforced contained runner"):
        gold.certify_task(
            task_id="synthetic", package=tmp_path / "absent", reference_patch=PATCH,
            out_dir=tmp_path / "out", run_cell=arbitrary, injection_controls=True,
            synthesize=lambda *_: pytest.fail("synthesized"),
        )
    assert not (tmp_path / "out").exists()


def test_contained_runner_refuses_cached_trial_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(gold, "modal_cell_runner", lambda **_: pytest.fail("dispatched"))
    cell = tmp_path / "cell"
    (cell / "trial").mkdir(parents=True)
    with pytest.raises(ValueError, match="fresh cell evidence"):
        contained_runner(tmp_path)(cell, tmp_path / "absent", "inert")


@pytest.mark.parametrize("arm", ["witness", "exit0_pth"])
@pytest.mark.parametrize("bad", ["missing", "unapplied", "infra", "boolean"])
def test_integrity_controls_never_pass_without_applied_measured_outcomes(tmp_path, arm, bad):
    cells, outcomes, upgraded = matrix(tmp_path, ["correct-a", "correct-b"])
    cell = next(c for c in cells if c.arm == arm)
    if bad == "missing":
        del outcomes[cell.name]
    elif bad == "unapplied":
        outcomes[cell.name]["agent_log"] = ""
    elif bad == "infra":
        outcomes[cell.name]["status"] = "failed"
    else:
        outcomes[cell.name]["reward"] = False
    result = summarize(cells, outcomes, upgraded)
    if arm == "witness":
        assert result.witness_rewards[cell.name] is None
        assert not result.gate3_witness_passed
    else:
        assert result.exit0_pth_reward is None
        assert not result.gate4_exit0_pth_passed
    assert result.status == "failed"


@pytest.mark.parametrize("extra,message", [
    ([], "--approval"), (["--approval", "offline test"], "--max-usd"),
    (["--approval", "offline test", "--max-usd", "nan"], "positive finite"),
])
def test_cli_contained_controls_validate_approval_and_cap_before_construction(
    tmp_path, monkeypatch, extra, message,
):
    args = cli_args(tmp_path, "--injection-controls", "--witness", "/nonexistent/witness", *extra)
    monkeypatch.setattr(gold, "ContainedModalRunner", lambda **_: pytest.fail("constructed"))
    with pytest.raises(ValueError, match=message):
        gold._certify_gold_command(args, tmp_path)


def test_ev1105_gold_native_mutant_and_integrity_slice():
    exp = Path(__file__).resolve().parents[1] / "research/experiments/ov1106-goldcert"
    freeze = json.loads((exp / "ev1105-gold-freeze.json").read_text())
    aggregate = json.loads((exp / "ev1105-gold-aggregate.json").read_text())
    validity = json.loads((exp / "mutant-validity.json").read_text())

    planned = {j["name"]: j for j in freeze["planned_jobs"]}
    executed = {r["name"]: r for r in aggregate["executed_rows"]}
    assert len(planned) == len(executed) == 16
    assert set(planned) == set(executed)

    validity_by_key = {(c["task_id"], c["control_name"]): c["category"] for c in validity["controls"]}
    for name, row in executed.items():
        plan_row = planned[name]
        for field in ("task", "arm", "control", "package_digest", "script_sha256", "static_category"):
            assert row[field] == plan_row[field]
        assert row["status"] == "executed"
        assert len(row["hashes"]) == 6
        if row["arm"] in ("train", "eval"):
            assert row["static_category"] == validity_by_key[row["task"], row["control"]]
            if row["static_category"] == "DEFECT":
                assert row["reward"] == 0.0
            else:
                assert row["static_category"] == "POSSIBLY_VALID"
                assert row["reward"] == 1.0
        else:
            assert row["arm"] in ("witness", "startup")
            assert row["reward"] == 0.0

    assert aggregate["pooled_counts"]["defect_mutants"]["rejected"] == 7
    assert aggregate["pooled_counts"]["defect_mutants"]["planned"] == 7
    assert aggregate["pooled_counts"]["possibly_valid_mutants"]["accepted"] == 3
    assert aggregate["pooled_counts"]["integrity_controls"]["rejected"] == 6
    assert aggregate["spend"]["reserved_usd"] == pytest.approx(1.92)
    assert aggregate["spend"]["remaining_owned_apps"] == []
    assert aggregate["tasks"]["002139"]["certified"] is True
    assert aggregate["tasks"]["002139"]["certified_strict_nonempty_g5_uninformed_g7"] is True
    assert aggregate["tasks"]["000682"]["certified"] is True
    assert aggregate["tasks"]["000682"]["certified_strict_nonempty_g5_uninformed_g7"] is False
