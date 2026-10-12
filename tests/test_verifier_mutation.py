"""Verifier mutation testing: mutant generation, control scripts, and audit classification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.verifier_coverage import (
    SCHEMA_TRACE,
    executed_lines,
    load_trace,
    targets_document,
    tracer_source,
)
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
    assert generate_mutants("/app/package.json", source.encode()) == []


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
    """Writes the trial layout Harbor leaves behind and returns a scripted reward.

    ``coverage`` is the list of tracer records the instrumented verifier writes.
    """

    def __init__(
        self,
        root: Path,
        rewards: dict[str, float],
        *,
        oracle: str = _PLAN,
        base: str | None = None,
        coverage: list[dict] | None = None,
    ) -> None:
        self.root = root
        self.rewards = rewards
        self.oracle = oracle
        self.base = _PLAN.replace("min(", "max(") if base is None else base
        self.coverage = coverage or []
        self.seen: list[str] = []
        self.packages: list[Path] = []

    def __call__(self, matrix, repo_root: Path) -> list[dict]:
        results = []
        for run in matrix.runs:
            script = (repo_root / run.solution).read_text()
            role = script.splitlines()[1].removeprefix("# envcheck: ").split()[0]
            self.seen.append(role)
            self.packages.append(Path(matrix.task))
            trial = repo_root / "runs" / run.name / "trial__1"
            capture = trial / "artifacts" / CAPTURE_ROOT.lstrip("/") / "app"
            if role == "capture-oracle":
                capture.mkdir(parents=True)
                (capture / "plan.py").write_text(self.oracle)
            elif role == "capture-base":
                capture.mkdir(parents=True)
                (capture / "plan.py").write_text(self.base)
            elif role == "coverage":
                records = trial / "verifier" / "envcheck-coverage"
                records.mkdir(parents=True)
                for index, record in enumerate(self.coverage):
                    (records / f"{index}.json").write_text(json.dumps(record))
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


# --------------------------------------------------------------------------- #
# Coverage-guided mutation
# --------------------------------------------------------------------------- #

# The reference fix rewrites line 2 (tested) and adds ``merge`` (never called
# by the tests): the shape of a half-fix a grader cannot see.
_BEFORE = """\
def plan(legs, limit):
    watermark = max(leg["wm"] for leg in legs)
    return 1 if watermark > limit else 0
"""
_FIXED = '''\
def plan(legs, limit):
    watermark = min(leg["wm"] for leg in legs)
    return 1 if watermark > limit else 0


def merge(own, other):
    """Union of aliases without our own id."""
    return own | other - {0}
'''


def _record(source: str, lines: list[int], *, intact: bool = True) -> dict:
    return {
        "schema": SCHEMA_TRACE,
        "mode": "sys.monitoring",
        "intact": intact,
        "files": [
            {
                "path": "/app/plan.py",
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
                "lines": lines,
            }
        ],
    }


def test_reference_hunks_are_reverted_one_at_a_time() -> None:
    mutants = generate_mutants("/app/plan.py", _FIXED.encode(), _BEFORE.encode())
    reverts = {m.line: content for m, content in mutants if m.operator == "revert-hunk"}
    # Reverting line 2 is the same bytes as the min -> max point mutant, so it is
    # generated once; the added ``merge`` hunk is reverted on its own.
    assert list(reverts) == [4]
    assert reverts[4].decode() == _FIXED[: _FIXED.index("\n\n\ndef merge") + 1]
    # Dropping one operand of the alias expression is a mutant of its own.
    dropped = {m.replacement for m, _ in mutants if m.operator == "drop-operand"}
    assert "(other)" in dropped


def test_tracer_records_only_lines_the_process_ran(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    home.mkdir()
    target = tmp_path / "app" / "plan.py"
    target.parent.mkdir()
    target.write_text(_FIXED)
    elsewhere = tmp_path / "copy" / "renamed.py"
    elsewhere.parent.mkdir()
    elsewhere.write_text(_FIXED)
    (home / "envcheck_coverage.py").write_text(tracer_source(str(home), str(out)))
    (home / "targets.json").write_text(targets_document({str(target): _FIXED.encode()}))
    for mode in ("default", "settrace"):
        env = dict(os.environ, ENVCHECK_COVERAGE_TRACER=mode)
        code = (
            f"import sys; sys.path.append({str(home)!r}); import envcheck_coverage, runpy; "
            f"runpy.run_path({str(elsewhere)!r})['plan']([{{'wm': 3}}], 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
        )
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
        trial = tmp_path / mode
        shutil.copytree(out, trial / "verifier" / "envcheck-coverage")
        shutil.rmtree(out)
        trace = load_trace([trial])
        assert trace is not None and trace.intact
        # The copy at another path is matched by content and filed under the target.
        hits = executed_lines(trace, str(target), _FIXED.encode())
        assert {2, 3} <= hits and 8 not in hits, (mode, hits)
        assert executed_lines(trace, str(target), _BEFORE.encode()) == set()


def test_coverage_skips_unexecuted_lines_and_probes_the_untested_fix(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(
        tmp_path,
        {"capture-oracle": 1.0, "capture-base": 0.0, "coverage": 1.0, "blank": 0.0, "mutant": 1.0},
        oracle=_FIXED,
        base=_BEFORE,
        # Lines 1, 2, 3, 6 ran on the fixed file; a baseline run on the original
        # file (different digest) must not count.
        coverage=[_record(_FIXED, [1, 2, 3, 6]), _record(_BEFORE, [1, 2, 3, 6, 8])],
    )
    report, run_dir = run_mutation_audit(
        package, repo_root=tmp_path, execute=True, max_mutants=50, runner=runner
    )
    assert report.coverage == "guided"
    (summary,) = report.files
    assert summary.coverage == "executed" and summary.untested_fix_lines == [8]
    assert summary.unexecuted > 0
    probe, *rest = report.mutants
    assert probe.operator == "partial-fix" and probe.executed is False
    assert rest and all(m.executed and not 6 < m.line <= 8 for m in rest)
    patch = (run_dir / "inputs" / probe.id / f"{probe.id}.patch").read_text()
    assert "-def merge(own, other):" in patch and "-    watermark" not in patch
    assert probe.id in report.survivors and report.verdict == "survivors_found"
    # Only the coverage run grades the instrumented copy; mutants grade the package.
    graded = dict(zip(runner.seen, runner.packages, strict=True))
    assert graded["mutant"] == package.resolve() != graded["coverage"]
    assert "envcheck coverage" in (graded["coverage"] / "tests" / "test.sh").read_text()
    assert "envcheck" not in (package / "tests" / "test.sh").read_text()


def test_untrusted_coverage_falls_back_to_every_line(tmp_path: Path) -> None:
    package = _package(tmp_path)
    runner = _Recorded(
        tmp_path,
        {"capture-oracle": 1.0, "capture-base": 0.0, "coverage": 1.0, "blank": 0.0, "mutant": 0.0},
        oracle=_FIXED,
        base=_BEFORE,
        # The tracer lost its hook in the test process: lines may be missing.
        coverage=[_record(_FIXED, [1, 2, 3, 6], intact=False)],
    )
    report, _ = run_mutation_audit(
        package, repo_root=tmp_path, execute=True, max_mutants=50, runner=runner
    )
    assert report.coverage == "unavailable" and report.notes
    assert any(m.line == 8 for m in report.mutants)
    assert all(m.executed is None and m.operator != "partial-fix" for m in report.mutants)
    assert report.verdict == "no_survivors"

    disabled, _ = run_mutation_audit(
        package, repo_root=tmp_path, execute=True, runner=runner, coverage=False
    )
    assert disabled.coverage == "disabled" and runner.seen.count("coverage") == 1
