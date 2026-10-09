"""Verifier mutation testing: mutant generation, control scripts, and audit classification."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evallab.verifier_mutation import (
    CAPTURE_ROOT,
    blank_script,
    generate_mutants,
    mutant_script,
    run_mutation_audit,
    select_mutants,
)

# Shapes of three published EnvCheck mutants (Terminal-Bench 4.0): an inverted
# flag in an output record, min -> max on a watermark, and a constant exit code.
_PLAN = '''\
def plan(legs, data):
    """Docstring constants are not behaviour: 1 + 2."""
    out = []
    for leg in legs:
        tow, ldw, tok, lok = check(leg)
        out.append({"takeoff_weight_ok": tok, "landing_weight_ok": lok})
    watermark = min(leg["wm"] for leg in legs)
    any_alert = watermark > data["limit"]
    return 1 if any_alert else 0
'''


def _by_edit(path: str, source: str, base: str | None = None):
    mutants = generate_mutants(path, source.encode(), base.encode() if base else None)
    return {(m.operator, m.original, m.replacement): (m, content) for m, content in mutants}


def test_python_mutants_reproduce_published_envcheck_edits() -> None:
    mutants = _by_edit("/app/plan.py", _PLAN)
    flag, flag_bytes = mutants[("negate-boolean", "lok", "not (lok)")]
    assert b'"landing_weight_ok": not (lok)' in flag_bytes
    minmax, _ = mutants[("min-max", "min", "max")]
    branch, branch_bytes = mutants[("conditional-branch", "1 if any_alert else 0", "(1)")]
    assert b"return (1)\n" in branch_bytes
    for mutant in (flag, minmax, branch):
        changed = [
            line
            for line in mutant.patch.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
        assert len(changed) == 2  # one line out, one line in
    # Every mutant compiles; docstring arithmetic is never mutated.
    for _, content in mutants.values():
        compile(content, "<mutant>", "exec")
    assert all(m.line != 2 for m, _ in mutants.values())


def test_changed_lines_follow_the_reference_solution_diff() -> None:
    base = _PLAN.replace("min(", "max(")
    mutants = _by_edit("/app/plan.py", _PLAN, base)
    minmax, _ = mutants[("min-max", "min", "max")]
    branch, _ = mutants[("conditional-branch", "1 if any_alert else 0", "(1)")]
    assert minmax.changed_line is True
    assert branch.changed_line is False
    # A file the solution created counts every line as written by it.
    assert all(m.changed_line for m, _ in _by_edit("/app/new.py", _PLAN).values())


def test_selection_is_bounded_deterministic_and_prefers_solution_lines() -> None:
    base = _PLAN.replace("min(", "max(")
    pool = [m for m, _ in generate_mutants("/app/plan.py", _PLAN.encode(), base.encode())]
    first = select_mutants(pool, limit=4, seed="s")
    assert [m.id for m in first] == [m.id for m in select_mutants(pool, limit=4, seed="s")]
    assert len(first) == 4
    assert len({(m.file, m.line) for m in first}) == 4  # one per line before repeating
    assert any(m.changed_line for m in first)


def test_selection_gives_every_file_a_turn() -> None:
    small = "def f(a, b):\n    return a - b\n"
    big = [m for m, _ in generate_mutants("/app/big.py", _PLAN.encode())]
    pool = big + [m for m, _ in generate_mutants("/app/small.py", small.encode())]
    picked = select_mutants(pool, limit=2)
    assert {m.file for m in picked} == {"/app/big.py", "/app/small.py"}
    assert len(select_mutants(pool, limit=10_000)) == len(pool)
    assert select_mutants(pool, limit=0) == []


def test_json_mutants_keep_valid_documents_and_indentation() -> None:
    source = '{\n  "feasible": true,\n  "legs": [\n    {"fuel": 12.5, "id": 3}\n  ]\n}\n'
    mutants = generate_mutants("/output/plan.json", source.encode())
    operators = {m.operator for m, _ in mutants}
    assert {"boolean-constant", "drop-key", "number-scale", "integer-shift"} <= operators
    for mutant, content in mutants:
        document = json.loads(content)
        assert content.decode().startswith('{\n  "')
        assert content.endswith(b"\n")
        assert document != json.loads(source), mutant.operator
    flipped = next(c for m, c in mutants if m.operator == "boolean-constant")
    assert json.loads(flipped)["feasible"] is False


def test_c_family_mutants_skip_strings_and_comments() -> None:
    source = 'const s = "a <= b && c";\n// x <= y\nif (a <= b && ok) { return true; }\n'
    mutants = generate_mutants("/app/src/check.ts", source.encode())
    edited = {(m.operator, m.line) for m, _ in mutants}
    assert edited == {("boundary", 3), ("boolean-operator", 3), ("boolean-constant", 3)}


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_mutant_script_runs_the_reference_then_writes_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "app" / "plan.py"
    solve = (
        f"#!/bin/bash\nset -euo pipefail\nmkdir -p {target.parent}\necho ref > {target}\nexit 0\n"
    )
    mutated = b"x = 1\ny = 'ENVCHECK'\n"
    result = _bash(mutant_script(solve, str(target), mutated, "t"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == mutated

    failing = f"#!/bin/bash\necho ref > {target}\nexit 7\n"
    target.write_text("untouched")
    result = _bash(mutant_script(failing, str(target), mutated, "t"), tmp_path)
    assert result.returncode == 7
    assert target.read_text() == "ref\n"

    result = _bash(blank_script(solve, str(target)), tmp_path)
    assert result.returncode == 0 and target.read_bytes() == b""


# --------------------------------------------------------------------------- #
# Audit classification against a recorded-result runner (the Harbor boundary)
# --------------------------------------------------------------------------- #


def _package(root: Path) -> Path:
    package = root / "pkg"
    (package / "solution").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "environment").mkdir()
    (package / "task.toml").write_text(
        'schema_version = "1.0"\nartifacts = ["/app/plan.py"]\n'
        '[task]\nname = "bench/plan-task"\n[verifier]\nenvironment_mode = "separate"\n'
    )
    (package / "solution" / "solve.sh").write_text("#!/bin/bash\necho solved\n")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    (package / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (package / "instruction.md").write_text("Plan the legs.\n")
    return package


class _Recorded:
    """Writes the trial layout Harbor leaves behind and returns a scripted reward."""

    def __init__(self, root: Path, rewards: dict[str, float]) -> None:
        self.root = root
        self.rewards = rewards
        self.seen: list[str] = []

    def __call__(self, matrix, repo_root: Path) -> list[dict]:
        results = []
        for run in matrix.runs:
            script = (repo_root / run.solution).read_text()
            role = script.splitlines()[1].removeprefix("# envcheck: ").split()[0]
            self.seen.append(role)
            trial = repo_root / "runs" / run.name / "trial__1"
            capture = trial / "artifacts" / CAPTURE_ROOT.lstrip("/") / "app"
            if role == "capture-oracle":
                capture.mkdir(parents=True)
                (capture / "plan.py").write_text(_PLAN)
            elif role == "capture-base":
                capture.mkdir(parents=True)
                (capture / "plan.py").write_text(_PLAN.replace("min(", "max("))
            trial.mkdir(parents=True, exist_ok=True)
            reward = self.rewards.get(role, self.rewards.get("mutant", 0.0))
            results.append({"name": run.name, "status": "ok", "rewards": [reward], "error": None})
        return results


def test_audit_reports_survivors_only_for_graded_files(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(
        tmp_path, {"capture-oracle": 1.0, "capture-base": 0.0, "blank": 0.0, "mutant": 1.0}
    )
    report, run_dir = run_mutation_audit(
        package, repo_root=tmp_path, execute=True, max_mutants=3, runner=runner
    )
    assert report.verdict == "survivors_found"
    assert report.mutation_score == 0.0
    assert report.counts == {
        "total": 3, "killed": 0, "survived": 3, "partial": 0, "uninformative": 0, "unscored": 0
    }  # fmt: skip
    assert report.survivors == [m.id for m in report.mutants]
    assert runner.seen.count("mutant") == 3
    (summary,) = report.files
    assert summary.graded is True and summary.survived == 3 and summary.oracle_changed
    records = [json.loads(line) for line in (run_dir / "hypotheses.jsonl").read_text().splitlines()]
    assert len(records) == 3 and {r["status"] for r in records} == {"candidate"}
    patch = run_dir / records[0]["case"]["path"]
    assert patch.is_file() and records[0]["case"]["grader_verdict"]["reward"] == 1.0


def test_ungraded_file_stops_before_mutants(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(tmp_path, {"capture-oracle": 1.0, "capture-base": 0.0, "blank": 1.0})
    report, run_dir = run_mutation_audit(package, repo_root=tmp_path, execute=True, runner=runner)
    assert "mutant" not in runner.seen
    assert report.verdict == "no_mutants"
    assert "/app/plan.py" in report.verdict_reason
    assert report.files[0].graded is False
    assert not (run_dir / "hypotheses.jsonl").exists()


def test_broken_controls_outrank_mutation(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(tmp_path, {"capture-oracle": 1.0, "capture-base": 1.0})
    report, _ = run_mutation_audit(package, repo_root=tmp_path, execute=True, runner=runner)
    assert report.verdict == "task_broken"
    assert runner.seen == ["capture-oracle", "capture-base"]


def test_killed_mutants_give_a_full_score(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(
        tmp_path, {"capture-oracle": 1.0, "capture-base": 0.0, "blank": 0.0, "mutant": 0.0}
    )
    report, _ = run_mutation_audit(
        package, repo_root=tmp_path, execute=True, max_mutants=2, runner=runner
    )
    assert report.verdict == "no_survivors" and report.mutation_score == 1.0


def test_mutate_cli_json_is_one_document_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    package = _package(tmp_path)
    code = cli.run_cli(["hack", "mutate", str(package), "--json"], workspace=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "evallab.verifier_mutation.report/v1"
    assert payload["verdict"] == "planned" and payload["targets"] == ["/app/plan.py"]
    assert "run directory:" in captured.err


def test_audit_refuses_packages_it_cannot_mutate(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "x/y"\n')
    with pytest.raises(ValueError, match="declares no artifacts"):
        run_mutation_audit(package, repo_root=tmp_path)
    (package / "solution" / "solve.sh").unlink()
    with pytest.raises(ValueError, match="no reference solution"):
        run_mutation_audit(package, repo_root=tmp_path, targets=["/app/plan.py"])
