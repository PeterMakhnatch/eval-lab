from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest

from evallab import candidate_port as port
from evallab import upstream_grader as grader


def put(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.fixture
def packages(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original" / "native-task"
    fixed = tmp_path / "fixed" / "native-task"
    put(original / "task.toml", '''
        schema_version = "1.0"
        artifacts = ["/app/result.txt"]
        [task]
        name = "terminal-bench/native-task"
        [agent]
        timeout_sec = 60.0
        [verifier]
        timeout_sec = 60.0
        environment_mode = "shared"
        [environment]
        cpus = 2
        memory_mb = 4096
        storage_mb = 10240
        gpus = 0
        build_timeout_sec = 600.0
    ''')
    put(original / "instruction.md", "Produce the requested business result.\n")
    put(original / "environment/Dockerfile", "FROM scratch\nWORKDIR /app\n")
    put(original / "environment/seed.txt", "immutable pinned setup data\n")
    put(original / "tests/test.sh", "#!/bin/sh\nprintf 1 > /logs/verifier/reward.txt\n")
    put(original / "solution/solve.sh", "#!/bin/sh\nprintf good > /app/result.txt\n")
    put(fixed / "Containerfile", "FROM scratch\nCOPY src/ /root/src/\nCOPY root_data/ /root_data/\n")
    put(fixed / "pyproject.toml", '[project]\nname="environment"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=["karotte==3.0.75"]\n')
    put(fixed / "uv.lock", 'version = 1\nrevision = 1\nrequires-python = ">=3.12"\n')
    put(fixed / ".manifest.json", '{"karotte_version":"3.0.75","agents":[],"templates":["default"]}\n')
    digest = hashlib.sha256((original / "environment/seed.txt").read_bytes()).hexdigest()
    put(fixed / "setup_data.py", f'''
        HUB_TASK = "native-task"
        HUB_REV = "4.0.0"
        ROOT_FILES = {{"environment/seed.txt": ("seed.txt", "{digest}")}}
        def setup(ctx):
            hidden = ctx.root_data
            files = {{source: (hidden / destination, sha) for source, (destination, sha) in ROOT_FILES.items()}}
            ctx.fetch_hub(HUB_TASK, HUB_REV, files)
    ''')
    put(fixed / "src/environment/__init__.py", '''
        from environment.tasks.native_task import NativeTask
        def get_tasks():
            return [NativeTask]
    ''')
    put(fixed / "src/environment/tasks/__init__.py", "")
    put(fixed / "src/environment/tasks/native_task/__init__.py", '''
        import os
        import shutil
        import sys
        from pathlib import Path
        from karotte import Task, Step
        from karotte.judges import ExecutableJudge

        class NativeTask(Task):
            id = "native-task"
            @property
            def required_hardware(self):
                return "cpus=2,memory_mb=4096,storage_mb=10240"
            @property
            def steps(self):
                return [FileStep(config=self.config)]

        class FileStep(Step):
            @property
            def submission_paths(self):
                return (Path(os.environ["FIXTURE_NATIVE_ROOT"]) / "workdir/result.txt",)
            def pre_scoring_hook(self):
                source = self.submission_paths[0]
                self.saved = Path(os.environ["FIXTURE_NATIVE_ROOT"]) / "custody/result.txt"
                self.saved.parent.mkdir(parents=True)
                if source.exists():
                    shutil.copy2(source, self.saved)
                (self.saved.parent / "pre-scoring-hook-ran").write_text("real custody hook")
            @property
            def judge(self):
                return ExecutableJudge([sys.executable, str(Path(__file__).with_name("scoring_script.py")), str(self.saved), "/tmp/score_output.json"])
    ''')
    put(fixed / "src/environment/tasks/native_task/scoring_script.py", '''
        import json
        import os
        import sys
        from pathlib import Path
        if os.environ.get("FIXTURE_PORT_FAILURE") == "crash":
            raise RuntimeError("an infrastructure dependency failed")
        if os.environ.get("FIXTURE_PORT_FAILURE") == "wrapped-crash":
            Path(sys.argv[-1]).write_text(json.dumps({"score": 0, "metadata": {"error": "Grading crashed: missing runtime"}}))
            raise SystemExit(0)
        candidate = Path(sys.argv[1])
        value = candidate.read_text() if candidate.is_file() else ""
        checks = {f"case-{i}": "passed" if value == "good" or (i < 2 and value.startswith("good")) else "failed: incorrect business output" for i in range(5)}
        Path(sys.argv[-1]).write_text(json.dumps({"score": float(all(status == "passed" for status in checks.values())), "metadata": {"mode": "full", "checks": checks, "checks_total": 5, "unexpanded_count": 99}}))
        print("the whole fixed scorer ran")
    ''')
    # A dependency sentinel must survive verbatim; native image preparation
    # must not replace the task with a handpicked source/test subset.
    put(fixed / "venvs/student/uv.lock", "locked-student-interpreter-and-all-dependencies\n")
    return original, fixed


@pytest.fixture
def inert_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Replace the SDK boundary, not the adapter or scorer, with local I/O."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(grader, "docker_slot", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(grader, "_admit_docker", lambda _: {"platform": "linux/amd64", "available_cpus": 8, "available_memory_mb": 16384, "emulated": False})

    async def execute(config: dict[str, Any], active: grader._Execution) -> dict[str, Any]:
        trial = Path(config["trials_dir"]) / config["trial_name"]
        verifier, agent = trial / "verifier", trial / "agent"
        verifier.mkdir(parents=True)
        agent.mkdir()
        root = tmp_path / "inert-native" / str(len(calls))
        (root / "app").mkdir(parents=True)
        (root / "workdir").mkdir()
        staged = Path(config["task"]["path"])
        calls.append({"config": config, "root": root, "preexisting_candidate": (root / "app/result.txt").exists()})
        solution = config["agent"]["kwargs"]["qa_solution_dir"]
        if solution:
            shutil.copytree(solution, root / "solution")
            candidate = (root / "solution/solve.sh").read_text().replace("/app", str(root / "app"))
            finished = subprocess.run(["bash", "-c", candidate], cwd=root / "app", capture_output=True, text=True, timeout=5, check=False)
            (agent / "oracle.txt").write_text(finished.stdout + finished.stderr)
            if finished.returncode:
                return {"exception_info": {"exception_message": "candidate failed"}, "verifier_result": None}
        shutil.copytree(staged / "tests", root / "tests")
        stub = root / "stub/karotte"
        put(stub / "__init__.py", '''
            from types import SimpleNamespace
            EvaluationRunConfig = SimpleNamespace
            class Task:
                def __init__(self, config): self.config = config
            class Step:
                def __init__(self, config): self.config = config
        ''')
        put(stub / "judges.py", '''
            class ExecutableJudge:
                def __init__(self, subprocess_run_args):
                    self.subprocess_run_args = subprocess_run_args
                    self.cwd = None
        ''')
        # This relocation is exclusively the inert transport's filesystem
        # namespace. Production adapter/scorer bytes are not changed or mocked.
        driver = (root / "tests/candidate-port-driver.py").read_text()
        driver = driver.replace('Path("/logs/verifier")', f"Path({str(verifier)!r})")
        driver = driver.replace('Path("/tests/candidate-port-config.json")', f"Path({str(root / 'tests/candidate-port-config.json')!r})")
        driver = driver.replace('Path("/workdir")', f"Path({str(root / 'workdir')!r})")
        driver = driver.replace("matches.append(Path(source))", f"matches.append(Path({str(root)!r}) / source.lstrip('/'))")
        driver = driver.replace("matches.append(Path(source) / relative)", f"matches.append(Path({str(root)!r}) / source.lstrip('/') / relative)")
        transport_driver = root / "transport-driver.py"
        transport_driver.write_text(driver)
        environment = dict(os.environ, FIXTURE_NATIVE_ROOT=str(root), PYTHONPATH=os.pathsep.join((str(root / "stub"), str(root / "tests/src"))))
        finished = subprocess.run([sys.executable, str(transport_driver)], cwd=root, env=environment, capture_output=True, text=True, timeout=10, check=False)
        (verifier / "test-stdout.txt").write_text(finished.stdout + finished.stderr)
        active.elapsed_seconds = 1.0
        if finished.returncode:
            payload = {"exception_info": {"exception_message": "fixed scorer infrastructure failure"}, "verifier_result": None}
        else:
            payload = {"exception_info": None, "verifier_result": {"rewards": grader._read_native_rewards(trial)}}
        (trial / "result.json").write_text(json.dumps(payload))
        return payload

    monkeypatch.setattr(grader, "_execute_native", execute)
    return calls


def test_description_is_pure_and_preserves_exact_source_identity(packages: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    original, fixed = packages
    before = [grader._tree_digest(path) for path in packages]
    before_modules = set(sys.modules)
    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("a local fixed-port description crossed a native runtime boundary")
    monkeypatch.setattr(grader, "_execute_native", forbidden)
    monkeypatch.setattr(grader, "_admit_docker", forbidden)
    monkeypatch.setattr(grader, "_reserve_spend", forbidden)
    described = port.describe_port_task(original, fixed)
    assert described["task_dir"] == str(original)
    assert described["mode"] == "full"
    assert described["upstream_identity"]["exact_tree_digest"] == before[0]
    assert described["port_identity"]["exact_tree_digest"] == before[1]
    assert described["port_identity"]["source_pin"] is None
    assert described["support"]["runtime_qualified"] is False
    assert described["native_image_identity"]["verifier"]["materialization"] == "pending"
    assert described["native_image_identity"]["verifier"]["exact_context_digest"] is None
    assert [grader._tree_digest(path) for path in packages] == before
    assert not any(name.startswith(("modal.", "harbor.", "karotte.")) for name in set(sys.modules) - before_modules)


def test_preparation_keeps_full_dependencies_and_both_immutable_inputs(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    before = [grader._tree_digest(path) for path in packages]
    described = port.describe_port_task(original, fixed)
    first = port.prepare_port_task(original, fixed, tmp_path / "evidence-one/port-task")
    second = port.prepare_port_task(original, fixed, tmp_path / "evidence-two/port-task")
    native = Path(first["task_dir"])
    config = tomllib.loads((native / "task.toml").read_text())
    assert config["environment"] == tomllib.loads((original / "task.toml").read_text())["environment"]
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["network_mode"] == "no-network"
    assert config["verifier"]["environment"]["cpus"] == 2
    assert config["verifier"]["environment"]["memory_mb"] == 4096
    assert grader._tree_digest(native / "environment") == grader._tree_digest(original / "environment")
    assert (native / "tests/root_data/seed.txt").read_bytes() == (original / "environment/seed.txt").read_bytes()
    assert (native / "tests/venvs/student/uv.lock").read_bytes() == (fixed / "venvs/student/uv.lock").read_bytes()
    assert (native / "tests/uv.lock").read_bytes() == (fixed / "uv.lock").read_bytes()
    assert (native / "tests/src/environment/tasks/native_task/scoring_script.py").read_bytes() == (fixed / "src/environment/tasks/native_task/scoring_script.py").read_bytes()
    assert (native / "tests/Dockerfile").read_text().startswith((fixed / "Containerfile").read_text())
    assert (native / "tests/test.sh").read_text() != (original / "tests/test.sh").read_text()
    assert "step.pre_scoring_hook()" in (native / "tests/candidate-port-driver.py").read_text()
    assert "task.pre_hook()" not in (native / "tests/candidate-port-driver.py").read_text().replace("# Do not run task.pre_hook()", "")
    assert first["grader_digest"] == described["grader_digest"] == second["grader_digest"]
    assert first["native_image_identity"]["verifier"]["logical_digest"] == described["native_image_identity"]["verifier"]["logical_digest"]
    assert first["native_image_identity"]["verifier"]["exact_context_digest"] == second["native_image_identity"]["verifier"]["exact_context_digest"]
    assert str(tmp_path) not in (native / "tests/candidate-port-config.json").read_text()
    assert [grader._tree_digest(path) for path in packages] == before
    plan = grader.plan_upstream_grader(native, blank=True, backend="auto")
    assert plan["verifier_environment_mode"] == "separate"
    assert [resource["build_context"] for resource in plan["resources"]] == ["environment", "tests"]


@pytest.mark.parametrize("contents,reward,statuses", [("good", 1.0, ["passed"] * 5), ("good-but-invalid", 0.0, ["passed", "passed", "failed", "failed", "failed"])])
def test_native_package_runs_the_whole_fixed_scorer_not_upstream_or_selected_assertions(packages: tuple[Path, Path], tmp_path: Path, inert_native: list[dict[str, Any]], contents: str, reward: float, statuses: list[str]) -> None:
    original, fixed = packages
    prepared = port.prepare_port_task(original, fixed, tmp_path / "native-task")
    solution = tmp_path / "candidate"
    put(solution / "solve.sh", f"#!/bin/sh\nprintf %s '{contents}' > /app/result.txt\n")
    before = [grader._tree_digest(path) for path in (*packages, solution)]
    result = grader.run_upstream_grader(prepared["task_dir"], solution, backend="docker", output_dir=tmp_path / "native-evidence")
    assert result["status"] == "measured"
    assert result["reward"] == reward
    assert [test["id"] for test in result["per_test"]["tests"]] == [f"case-{index}" for index in range(5)]
    assert [test["status"] for test in result["per_test"]["tests"]] == statuses
    verifier = Path(result["trial_dir"]) / "verifier"
    score = json.loads((verifier / "port-score.json").read_text())
    assert score["metadata"]["checks_total"] == 5
    assert score["metadata"]["unexpanded_count"] == 99
    invocation = json.loads((verifier / "port-invocation.json").read_text())
    assert invocation["mode"] == "full"
    assert invocation["grader_digest"] == prepared["grader_digest"]
    assert "the whole fixed scorer ran" in (verifier / "port-stdout.txt").read_text()
    assert (inert_native[0]["root"] / "custody/pre-scoring-hook-ran").is_file()
    assert inert_native[0]["preexisting_candidate"] is False
    assert [grader._tree_digest(path) for path in (*packages, solution)] == before


@pytest.mark.parametrize("failure", ["crash", "wrapped-crash"])
def test_scorer_infrastructure_failure_is_unmeasured_not_a_synthetic_zero(packages: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inert_native: list[dict[str, Any]], failure: str) -> None:
    prepared = port.prepare_port_task(*packages, tmp_path / "native-task")
    monkeypatch.setenv("FIXTURE_PORT_FAILURE", failure)
    result = grader.run_upstream_grader(prepared["task_dir"], blank=True, backend="docker", output_dir=tmp_path / "native-evidence")
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["rewards"] is None
    verifier = Path(result["trial_dir"]) / "verifier"
    assert not (verifier / "reward.json").exists()
    assert (verifier / "port-infrastructure-error.txt").is_file()


def test_explicit_full_scorer_results_are_preserved_without_inventing_aggregate_passes() -> None:
    namespace: dict[str, Any] = {"__name__": "candidate_port_driver_contract"}
    exec(port._DRIVER, namespace)
    metadata = {
        "tests_passed": "27/27",
        "checks": {"business-check": "passed"},
        "deterministic": False,
        "hidden_data_sets": [{"variant": "dataset", "failed_tests": {"rerun-check": "failed: mismatch"}}],
        "limit_breaking_data_sets": [{"kind": "limit", "ok": False}],
    }
    records = namespace["records"](metadata)
    assert {(item["name"], item["status"]) for item in records} == {
        ("business-check", "passed"),
        ("deterministic", "failed"),
        ("hidden_data_sets/dataset/rerun-check", "failed"),
        ("limit_breaking_data_sets/limit/ok", "failed"),
    }


@pytest.mark.parametrize("mutation,reason", [
    ("compose", "Compose"),
    ("gpu", "accelerator"),
    ("zero-cpu", "cpus"),
    ("missing-lock", "uv.lock"),
    ("mismatched-asset", "differs from pinned"),
    ("external-fetch", "nonlocal/transforming"),
    ("escape-destination", "unsafe setup-data path"),
    ("external-model", "external model/provider"),
    ("unbound-artifact", "explicit port binding"),
    ("custom-judge", "custom/composite/model-backed"),
])
def test_invalid_or_unqualified_packages_refuse_before_creating_output(packages: tuple[Path, Path], tmp_path: Path, mutation: str, reason: str) -> None:
    original, fixed = packages
    source = fixed / "src/environment/tasks/native_task/__init__.py"
    if mutation == "compose":
        put(original / "environment/compose.yaml", "services: {}\n")
    elif mutation == "gpu":
        put(original / "task.toml", (original / "task.toml").read_text().replace("gpus = 0", "gpus = 1"))
    elif mutation == "zero-cpu":
        put(source, source.read_text().replace("cpus=2", "cpus=0"))
    elif mutation == "missing-lock":
        (fixed / "uv.lock").unlink()
    elif mutation == "mismatched-asset":
        put(original / "environment/seed.txt", "changed underlying source\n")
    elif mutation == "external-fetch":
        put(fixed / "setup_data.py", 'def setup(ctx):\n    ctx.fetch("https://example.invalid/asset", ctx.root_data / "asset", "0" * 64)\n')
    elif mutation == "escape-destination":
        path = fixed / "setup_data.py"
        put(path, path.read_text().replace('(\"seed.txt\",', '(\"../escaped.txt\",'))
    elif mutation == "external-model":
        put(fixed / "src/environment/provider.py", "import openai\n")
    elif mutation == "unbound-artifact":
        path = original / "task.toml"
        put(path, path.read_text().replace("/app/result.txt", "/state/result.txt"))
    elif mutation == "custom-judge":
        put(source, source.read_text().replace("ExecutableJudge(", "RubricJudge("))
    output = tmp_path / "must-not-exist"
    with pytest.raises(grader.GraderRefusal, match=reason):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


def test_fresh_output_and_no_source_overlap_are_required(packages: tuple[Path, Path], tmp_path: Path) -> None:
    for output in (packages[0], packages[1] / "generated", tmp_path):
        before = [grader._tree_digest(path) for path in packages]
        with pytest.raises(grader.GraderRefusal, match="fresh and outside"):
            port.prepare_port_task(*packages, output)
        assert [grader._tree_digest(path) for path in packages] == before


def test_symlinks_refuse_instead_of_following_or_mutating_inputs(packages: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = packages
    (fixed / "source-alias").symlink_to(original / "environment/seed.txt")
    with pytest.raises(grader.GraderRefusal, match="symlink"):
        port.prepare_port_task(original, fixed, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()
