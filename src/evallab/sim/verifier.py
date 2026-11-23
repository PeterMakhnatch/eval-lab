from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from random import Random

from evallab.separate_verifier import (
    SNAP_DIR,
    TRAJECTORY_ARTIFACT,
    V3_RUNNER_PATTERNS,
    V3_TAMPER_SIGNATURES,
    build_changes,
    build_changes_v10,
    detect_runner,
    drop_reason,
    evaluate_unittest,
    is_test_infra_filename,
    read_parent_info,
    resolve_command_text,
    v3_tamper_hit_for_file,
)
from evallab.sim.engine import Simulation
from evallab.task_variants import VariantInvalid

_STEP_BUDGET = 20
_STEP_CEILING = 140
_HEX = "0123456789abcdef"
_WORKDIRS = ("/app", "/workspace", "/src/project", "/home/agent/task")
_TEST_SH = b"#!/bin/sh\nset -eu\npython -m unittest discover -s tests\n"
_SETUP_SH = b"#!/bin/sh\nset -eu\nmkdir -p /opt/verifier\n"
_COMMAND_SH = b"#!/bin/sh\nset -eu\ncd /app\n"
_PATCH = b"diff --git a/tests/test.sh b/tests/test.sh\n+python -m unittest discover -v\n"
_SOLUTION = b"#!/bin/sh\nset -eu\nprintf '%s\\n' ok > /app/out.txt\n"


def _hex(rng: Random) -> str:
    return "".join(rng.choice(_HEX) for _ in range(64))


def _toml(*, name: str, workdir: str, image: str, extra: str = "") -> str:
    return (
        'schema_version = "1"\n'
        "\n"
        "[task]\n"
        f'name = "{name}"\n'
        "\n"
        "[environment]\n"
        'build_context = "environment"\n'
        f'docker_image = "{image}"\n'
        f'workdir = "{workdir}"\n'
        "\n"
        "[verifier]\n"
        "timeout_sec = 900.0\n"
        f"{extra}"
    )


def _write(root: Path, files: dict[str, bytes]) -> Path:
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def _parent(
    root: Path,
    *,
    name: str,
    workdir: str,
    image: str,
    verifier_extra: str = "",
    task_name: bool = True,
    solution: bytes | None = None,
    patch: bytes | None = _PATCH,
) -> Path:
    toml = _toml(name=name, workdir=workdir, image=image, extra=verifier_extra)
    if not task_name:
        toml = "\n".join(line for line in toml.splitlines() if not line.startswith("name ="))
        toml += "\n"
    files: dict[str, bytes] = {
        "task.toml": toml.encode(),
        "tests/test.sh": _TEST_SH,
        "tests/test_command.sh": _COMMAND_SH,
        "environment/setup/setup.sh": _SETUP_SH,
    }
    if patch is not None:
        files["tests/test.patch"] = patch
    if solution is not None:
        files["solution/solve.sh"] = solution
    return _write(root, files)


def _copy(base: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    return Path(shutil.copytree(base, dest))


def _replace(root: Path, relative: str, old: str, new: str) -> Path:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


class VerifierSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= _STEP_CEILING
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        self.workdir = self.rng.choice(_WORKDIRS)
        self.image = f"python:3.12-slim@sha256:{_hex(self.rng)}"
        self.task_name = f"sim-parent-{self.rng.randrange(1000, 10_000)}"
        self.marker = f"SIM-HIDDEN-{_hex(self.rng)[:12]}"

    def run(self) -> None:
        base = _parent(
            self.root / "parent",
            name=self.task_name,
            workdir=self.workdir,
            image=self.image,
        )
        self.check_parent_info(base)
        self.check_parent_refusals(base)
        self.check_build_changes(base)
        self.check_build_solution(base)
        self.check_build_refusals(base)
        self.check_determinism(base)
        self.check_v10_runner(base)
        self.check_v10_unknown(base)
        self.check_unittest()
        self.check_test_infra()
        self.check_drop_reason()
        self.check_runner_order()
        self.check_scoped_signatures()
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "verifier_step_budget")

    def check_parent_info(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-parent-info", workdir=self.workdir)
        info = read_parent_info(base)
        sim.require(info.task_name == self.task_name, "verifier_parent_task_name")
        sim.require(info.workdir == self.workdir, "verifier_parent_workdir")
        sim.require(info.docker_image == self.image, "verifier_parent_docker_image")
        sim.require(info.has_solution is False, "verifier_parent_no_solution")
        with_solution = _parent(
            self.root / "parent-solution",
            name=self.task_name,
            workdir=self.workdir,
            image=self.image,
            solution=_SOLUTION,
        )
        sim.require(
            read_parent_info(with_solution).has_solution is True, "verifier_parent_has_solution"
        )

    def check_parent_refusals(self, base: Path) -> None:
        cases = self.refusal_cases(base)
        self.sim.require(len(cases) == 7, "verifier_parent_refusal_case_count")
        for label, root, expected in cases:
            self.sim.begin("verifier-parent-refusal", case=label)
            message = self.invalid(root)
            self.sim.require(message is not None, f"verifier_parent_refusal_{label}")
            self.sim.require(expected in (message or ""), f"verifier_parent_refusal_{label}_reason")

    def refusal_cases(self, base: Path) -> list[tuple[str, Path, str]]:
        refusals = self.root / "refusals"
        cases: list[tuple[str, Path, str]] = []
        no_verifier = _copy(base, refusals / "no_verifier")
        _replace(no_verifier, "task.toml", "\n[verifier]\ntimeout_sec = 900.0\n", "\n")
        cases.append(("no_verifier", no_verifier, "no [verifier]"))
        separate = _copy(base, refusals / "separate_mode")
        _replace(separate, "task.toml", "timeout_sec = 900.0", 'environment_mode = "separate"')
        cases.append(("separate_mode", separate, "separate-verifier mode"))
        dockerfile = _copy(base, refusals / "dockerfile")
        (dockerfile / "tests/Dockerfile").write_bytes(b"FROM scratch\n")
        cases.append(("dockerfile", dockerfile, "tests/Dockerfile"))
        no_test_sh = _copy(base, refusals / "no_test_sh")
        (no_test_sh / "tests/test.sh").unlink()
        cases.append(("no_test_sh", no_test_sh, "no tests/test.sh"))
        relative = _copy(base, refusals / "relative_workdir")
        _replace(relative, "task.toml", f'workdir = "{self.workdir}"', 'workdir = "app"')
        cases.append(("relative_workdir", relative, "absolute path"))
        empty_image = _copy(base, refusals / "empty_image")
        _replace(empty_image, "task.toml", f'docker_image = "{self.image}"', 'docker_image = ""')
        cases.append(("empty_image", empty_image, "docker_image"))
        no_name = _copy(base, refusals / "no_task_name")
        _replace(no_name, "task.toml", f'name = "{self.task_name}"', 'name = ""')
        cases.append(("no_task_name", no_name, "[task].name"))
        return cases

    def invalid(self, root: Path) -> str | None:
        try:
            read_parent_info(root)
        except VariantInvalid as exc:
            return str(exc)
        except Exception as exc:
            return f"unexpected {type(exc).__name__}: {exc}"
        return None

    def check_build_changes(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-build-changes", marker=self.marker)
        changes, inputs = build_changes(base, marker=self.marker)
        sim.require(
            set(changes)
            == {"task.toml", "tests/test.sh", "tests/test-orig.sh", "tests/Dockerfile"},
            "verifier_build_change_keys",
        )
        sim.require(changes["tests/test-orig.sh"] == _TEST_SH, "verifier_build_test_orig_bytes")
        wrapper = changes["tests/test.sh"]
        assert wrapper is not None
        sim.require(self.marker.encode() in wrapper, "verifier_build_wrapper_marker")
        task_toml = changes["task.toml"]
        assert task_toml is not None
        text = task_toml.decode()
        sim.require(f'"{SNAP_DIR}"' in text, "verifier_build_snapshot_artifact")
        sim.require(f'"{TRAJECTORY_ARTIFACT}"' in text, "verifier_build_trajectory_artifact")
        sim.require('environment_mode = "separate"' in text, "verifier_build_separate_mode")
        sim.require(inputs["parent_task"] == self.task_name, "verifier_build_inputs_task")
        sim.require(inputs["workdir"] == self.workdir, "verifier_build_inputs_workdir")
        sim.require(inputs["docker_image"] == self.image, "verifier_build_inputs_image")
        sim.require(inputs["marker"] == self.marker, "verifier_build_inputs_marker")
        sim.require(inputs["solution"] == "absent", "verifier_build_solution_absent")

    def check_build_solution(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-build-solution", bytes=len(_SOLUTION))
        changes, inputs = build_changes(base, marker=self.marker, solution_sh=_SOLUTION)
        sim.require(
            set(changes)
            == {
                "task.toml",
                "tests/test.sh",
                "tests/test-orig.sh",
                "tests/Dockerfile",
                "solution/solve.sh",
            },
            "verifier_build_solution_keys",
        )
        sim.require(changes["solution/solve.sh"] == _SOLUTION, "verifier_build_solution_bytes")
        digest = hashlib.sha256(_SOLUTION).hexdigest()
        sim.require(inputs["solution"] == f"sha256:{digest}", "verifier_build_solution_digest")

    def check_build_refusals(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-build-refusal", case="blank_marker")
        message = self.build_invalid(base, marker="   ", solution_sh=None)
        sim.require(message is not None, "verifier_build_refusal_blank_marker")
        sim.require("nonempty" in (message or ""), "verifier_build_refusal_blank_marker_reason")
        sim.begin("verifier-build-refusal", case="overwrite_solution")
        with_solution = _parent(
            self.root / "parent-overwrite",
            name=self.task_name,
            workdir=self.workdir,
            image=self.image,
            solution=_SOLUTION,
        )
        message = self.build_invalid(with_solution, marker=self.marker, solution_sh=b"#!/bin/sh\n")
        sim.require(message is not None, "verifier_build_refusal_overwrite")
        sim.require(
            "refusing overwrite" in (message or ""), "verifier_build_refusal_overwrite_reason"
        )

    def build_invalid(self, root: Path, *, marker: str, solution_sh: bytes | None) -> str | None:
        try:
            build_changes(root, marker=marker, solution_sh=solution_sh)
        except VariantInvalid as exc:
            return str(exc)
        except Exception as exc:
            return f"unexpected {type(exc).__name__}: {exc}"
        return None

    def check_determinism(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-repeat-identical", marker=self.marker)
        first_changes, first_inputs = build_changes(base, marker=self.marker)
        second_changes, second_inputs = build_changes(base, marker=self.marker)
        sim.require(first_changes == second_changes, "verifier_repeat_changes")
        sim.require(first_inputs == second_inputs, "verifier_repeat_inputs")
        first_v10, first_v10_inputs = build_changes_v10(base, marker=self.marker)
        second_v10, second_v10_inputs = build_changes_v10(base, marker=self.marker)
        sim.require(first_v10 == second_v10, "verifier_repeat_v10_changes")
        sim.require(first_v10_inputs == second_v10_inputs, "verifier_repeat_v10_inputs")

    def check_v10_runner(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-v10-runner", marker=self.marker)
        changes, inputs = build_changes_v10(base, marker=self.marker)
        command_text = resolve_command_text(_COMMAND_SH.decode(), None, _PATCH.decode())
        sim.require(inputs["runner"] == detect_runner(command_text), "verifier_v10_runner")
        sim.require(inputs["runner"] == "unittest", "verifier_v10_runner_value")
        sim.require("tests/test.sh" in changes, "verifier_v10_wrapper")
        sim.require("tests/_verifier-setup/setup.sh" in changes, "verifier_v10_setup_bundle")
        sim.require(
            inputs["setup_files"] == ["tests/_verifier-setup/setup.sh"], "verifier_v10_setup_files"
        )
        wrapper = changes["tests/test.sh"]
        assert wrapper is not None
        sim.require(
            inputs["wrapper_sha256"] == hashlib.sha256(wrapper).hexdigest(),
            "verifier_v10_wrapper_digest",
        )

    def check_v10_unknown(self, base: Path) -> None:
        sim = self.sim
        sim.begin("verifier-v10-unknown", marker=self.marker)
        root = _parent(
            self.root / "parent-no-patch",
            name=self.task_name,
            workdir=self.workdir,
            image=self.image,
            patch=None,
        )
        _, inputs = build_changes_v10(root, marker=self.marker)
        sim.require(inputs["runner"] == "unknown", "verifier_v10_unknown_runner")
        _, base_inputs = build_changes(base, marker=self.marker)
        sim.require(base_inputs.get("runner") is None, "verifier_v10_runner_absent_in_v1")

    def check_unittest(self) -> None:
        cases = (
            ("ok", "Ran 3 tests in 0.001s\n\nOK\n", 0, 1),
            ("single", "Ran 1 test in 0.001s\n\nOK\n", 0, 1),
            ("nonzero_rc", "Ran 3 tests\n\nOK\n", 1, 0),
            ("missing_ran", "OK\n", 0, 0),
            ("zero_ran", "Ran 0 tests\n\nOK\n", 0, 0),
            ("missing_ok", "Ran 3 tests\n", 0, 0),
            ("failed_line", "Ran 3 tests\n\nOK\nFAILED (failures=1)\n", 0, 0),
            ("error_line", "Ran 4 tests\n\nOK\nERROR: tearDownClass\n", 0, 0),
        )
        self.sim.begin("verifier-unittest", cases=len(cases))
        for label, output, rc, expected in cases:
            self.sim.require(
                evaluate_unittest(output, rc) == expected,
                f"verifier_unittest_{label}",
            )

    def check_test_infra(self) -> None:
        positives = (
            "conftest.py",
            "tests/conftest.py",
            "pytest.ini",
            "tox.ini",
            "sitecustomize.py",
            "usercustomize.py",
            "tests/_verifier-setup/patch.pth",
            "pkg/hooks/run_test.go",
        )
        negatives = ("test.py", "conftest.py.bak", "go.mod", "readme_test.go.txt", "x.pth.bak")
        self.sim.begin("verifier-test-infra", positives=len(positives), negatives=len(negatives))
        for path in positives:
            self.sim.require(is_test_infra_filename(path), "verifier_test_infra_positive")
        for path in negatives:
            self.sim.require(not is_test_infra_filename(path), "verifier_test_infra_negative")

    def check_drop_reason(self) -> None:
        self.sim.begin("verifier-drop-reason")
        hidden = ("tests/hidden/check.py", "tests/conftest.py")
        self.sim.require(
            drop_reason("tests/conftest.py", hidden) == "test-infra",
            "verifier_drop_reason_precedence",
        )
        self.sim.require(
            drop_reason("tests/hidden/check.py", hidden) == "hidden-test path",
            "verifier_drop_reason_hidden",
        )
        self.sim.require(drop_reason("tests/other.py", hidden) is None, "verifier_drop_reason_none")

    def check_runner_order(self) -> None:
        lines = (
            ("go test ./... && pytest -q\n", "go-test"),
            ("pytest -q && jest --ci\n", "jest"),
            ("python -m unittest discover\n", "unittest"),
        )
        self.sim.begin("verifier-runner-order", lines=len(lines))
        for text, expected in lines:
            found = detect_runner(text)
            first = next(
                (runner for runner, pattern in V3_RUNNER_PATTERNS if pattern.search(text)),
                "custom",
            )
            self.sim.require(found == expected, "verifier_runner_value")
            self.sim.require(found == first, "verifier_runner_first_match")
        self.sim.require(detect_runner("bash ./run_all.sh\n") == "custom", "verifier_runner_custom")

    def check_scoped_signatures(self) -> None:
        self.sim.begin("verifier-scoped-signatures")
        ruby = v3_tamper_hit_for_file("spec/foo_spec.rb", "  exit(0)\n")
        php = v3_tamper_hit_for_file("src/Foo.php", "  die(0);\n")
        self.sim.require(ruby is not None, "verifier_scoped_ruby_hit")
        self.sim.require(php is not None, "verifier_scoped_php_hit")
        self.sim.require(ruby not in V3_TAMPER_SIGNATURES, "verifier_scoped_ruby_not_universal")
        self.sim.require(php not in V3_TAMPER_SIGNATURES, "verifier_scoped_php_not_universal")
        self.sim.require(
            v3_tamper_hit_for_file("src/app.py", "exit(0)\n") is None, "verifier_scoped_python_miss"
        )
        self.sim.require(
            v3_tamper_hit_for_file("src/app.sh", "exit(0)\n") is None, "verifier_scoped_shell_miss"
        )
        self.sim.require(
            v3_tamper_hit_for_file("src/app.rb", "die(0)\n") is None,
            "verifier_scoped_ruby_php_only",
        )
        self.sim.require(
            v3_tamper_hit_for_file("src/app.php", "exit!(0)\n") is None,
            "verifier_scoped_php_ruby_only",
        )
        universal = v3_tamper_hit_for_file("src/app.rb", "process.exit(0)\n")
        self.sim.require(universal in V3_TAMPER_SIGNATURES, "verifier_scoped_universal_wins")
