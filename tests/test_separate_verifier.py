"""Behavioural tests for the separate-verifier transforms.

Covers the transform output contract only (task.toml fields, agent build
context free of tests, artifact declaration, grader behaviour on synthetic
diffs/reports). No Docker, no Harbor runs.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from evallab.separate_verifier import (
    JUNIT_MISSING_REASON,
    PYTEST_START_MARKERS,
    SNAP_DIR,
    TAMPER_SIGNATURES,
    TRAJECTORY_ARTIFACT,
    TRANSFORM_ID,
    TRANSFORM_ID_V2,
    V2_GRADE_DIR,
    V2_SETUP_SUBDIR,
    addopts_cleared,
    build_changes,
    build_changes_v2,
    declares_testmain,
    derive_separate_verifier,
    derive_separate_verifier_v2,
    detect_pytest_run,
    drop_reason,
    evaluate_junit,
    is_pytest_config_tamper,
    is_test_infra_filename,
    junit_absence_suspicious,
    output_is_blank,
    parse_named_pytest_ids,
    read_parent_info,
    render_wrapper_test_sh_v2,
    resolve_command_text,
    tamper_signature_hit,
)
from evallab.task_variants import VariantInvalid

MARKER = "test_hidden_example"

PARENT_TOML = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/format-code-task-000000"
description = "synthetic parent"

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 2100.0
user = "root"

[environment]
docker_image = "docker.io/example/repo@sha256:0000000000000000000000000000000000000000000000000000000000000000"
workdir = "/testbed"
cpus = 2
memory_mb = 8192
network_mode = "public"
build_timeout_sec = 1800.0
"""

PARENT_TEST_SH = """#!/bin/bash
M=/var/lib/mimo
echo grading
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    (root / "environment").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "task.toml").write_text(PARENT_TOML, encoding="utf-8")
    (root / "instruction.md").write_text("fix it", encoding="utf-8")
    (root / "environment" / "Dockerfile").write_text(
        "FROM docker.io/example/repo@sha256:0000\n", encoding="utf-8"
    )
    (root / "tests" / "test.sh").write_text(PARENT_TEST_SH, encoding="utf-8")
    (root / "tests" / "test.patch").write_text("fake patch", encoding="utf-8")
    (root / "tests" / "test_command.sh").write_text("true\n", encoding="utf-8")
    return root


def test_task_toml_declares_separate_mode_and_snapshot_artifact(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    config = tomllib.loads(changes["task.toml"].decode("utf-8"))

    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["timeout_sec"] == 2100.0
    assert config["verifier"]["user"] == "root"
    assert "environment" not in config["verifier"]

    assert config["artifacts"] == [SNAP_DIR, TRAJECTORY_ARTIFACT]
    assert SNAP_DIR.startswith("/var/tmp/")
    assert SNAP_DIR.rstrip("/") != "/logs/artifacts"

    hooks = config["verifier"]["collect"]
    assert len(hooks) == 2
    assert all(hook["service"] == "main" for hook in hooks)
    assert all(hook["user"] == "root" for hook in hooks)
    # Probe runs before the snapshot so tarballs are not scanned.
    assert MARKER in hooks[0]["command"]
    assert "workspace.tgz" in hooks[1]["command"]
    assert MARKER not in hooks[1]["command"]
    assert "workspace.tgz" not in hooks[0]["command"]
    # Untouched sections survive byte-identical in spirit.
    assert config["environment"]["docker_image"].startswith("docker.io/example")
    assert config["environment"]["workdir"] == "/testbed"
    assert config["task"]["name"] == "mimo-v2.6-rl/format-code-task-000000"


def test_trajectory_artifact_reaches_integrity_default_path(parent_dir: Path) -> None:
    """The declared trajectory lands where the integrity runner looks by default."""
    changes, inputs = build_changes(parent_dir, marker=MARKER)
    config = tomllib.loads(changes["task.toml"].decode("utf-8"))
    assert TRAJECTORY_ARTIFACT == "/logs/agent/trajectory.json"
    assert TRAJECTORY_ARTIFACT in config["artifacts"]
    # Outside the convention dir: no overlap/skip against the implicit entry,
    # so regrade coverage can be satisfied by a collected manifest entry.
    assert not TRAJECTORY_ARTIFACT.startswith("/logs/artifacts/")
    assert inputs["trajectory_artifact"] == TRAJECTORY_ARTIFACT


def test_composes_after_integrity_tail(parent_dir: Path) -> None:
    """Integrity-first ordering: the scoring tail must land inside test-orig.sh."""
    from evallab.integrity_reward import build_test_sh

    tailed = build_test_sh((parent_dir / "tests" / "test.sh").read_bytes())
    (parent_dir / "tests" / "test.sh").write_bytes(tailed)
    changes, _ = build_changes(parent_dir, marker=MARKER)
    orig = changes["tests/test-orig.sh"].decode("utf-8")
    assert "rewardkit-integrity@1" in orig
    assert "run_integrity.py" in orig
    wrapper = changes["tests/test.sh"].decode("utf-8")
    assert "exec /tests/test-orig.sh" in wrapper
    # Applying separate first would strand the tail after an exec line.
    assert "rewardkit-integrity@1" not in wrapper


def test_agent_build_context_unchanged_but_verifier_bundles_tests(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    # No change touches environment/: the agent image build is untouched.
    assert not any(key.startswith("environment/") for key in changes)
    dockerfile = changes["tests/Dockerfile"].decode("utf-8")
    assert "docker.io/example/repo@sha256:" in dockerfile
    assert "COPY . /tests" in dockerfile
    assert "--platform=linux/amd64" in dockerfile
    assert "chmod +x /tests/test.sh /tests/test-orig.sh" in dockerfile


def test_wrapper_restores_snapshot_and_orig_is_verbatim(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    assert changes["tests/test-orig.sh"].decode("utf-8") == PARENT_TEST_SH

    wrapper = changes["tests/test.sh"].decode("utf-8")
    assert "workspace.tgz" in wrapper
    assert "git-hidden.tgz" in wrapper
    assert "exec /tests/test-orig.sh" in wrapper
    assert MARKER in wrapper  # verifier-side probe
    # The wrapper carries no hidden-test content beyond the marker.
    assert "Weight calculate failed" not in wrapper


def test_solution_injected_only_when_parent_has_none(parent_dir: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, inputs = build_changes(parent_dir, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    assert inputs["solution"].startswith("sha256:")

    (parent_dir / "solution").mkdir()
    (parent_dir / "solution" / "solve.sh").write_text("existing", encoding="utf-8")
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir, marker=MARKER, solution_sh=solve)


def test_refuses_already_separate_or_missing_pieces(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir, marker="  ")
    with pytest.raises(VariantInvalid):
        read_parent_info(parent_dir / "missing")

    text = (parent_dir / "task.toml").read_text(encoding="utf-8")
    (parent_dir / "task.toml").write_text(
        text.replace(
            "[verifier]\ntimeout_sec", '[verifier]\nenvironment_mode = "separate"\ntimeout_sec'
        ),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid):
        read_parent_info(parent_dir)


def test_derive_records_transform_id(parent_dir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier(
        parent_dir,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID
    assert record.task_name == "mimo-v2.6-rl/format-code-task-000000"
    assert {change.path for change in record.files} == {
        "task.toml",
        "tests/test.sh",
        "tests/test-orig.sh",
        "tests/Dockerfile",
    }
    expected_record = (
        tmp_path / "library" / "task-variants" / record.task_slug / f"{record.digest12}.json"
    )
    assert expected_record.is_file()
    assert (store / record.task_slug / record.digest12).is_dir()


# --------------------------------------------------------------------------- #
# separate-verifier@2
# --------------------------------------------------------------------------- #


@pytest.fixture()
def parent_dir_v2(parent_dir: Path) -> Path:
    setup = parent_dir / "environment" / "setup"
    (setup / "files").mkdir(parents=True)
    (setup / "setup.sh").write_text("#!/bin/bash\necho setup\n", encoding="utf-8")
    (setup / "files" / "blocklist").write_text("block\n", encoding="utf-8")
    return parent_dir


def test_tamper_gate_flags_runner_reaches_but_passes_plain_fixes() -> None:
    # In-source exit-code forcing and pytest internals: not fixes.
    assert tamper_signature_hit("+import atexit, os\n+atexit.register(lambda: os._exit(0))")
    assert tamper_signature_hit("+from _pytest.reports import TestReport")
    assert tamper_signature_hit("+def pytest_configure(config):\n+    os._exit(0)")
    assert tamper_signature_hit("+junitxml.write(os.devnull)")
    assert tamper_signature_hit("+def pytest_runtest_setup(item):\n+    pass")
    assert tamper_signature_hit("+from _pytest.runner import runtestprotocol")
    # A plain bug fix carries none of the signatures ...
    assert (
        tamper_signature_hit("+    if value is None:\n+        return []\n+    return list(value)")
        is None
    )
    # ... nor do near-miss words without the runner boundary.
    assert tamper_signature_hit("+latexit = True\n+exit_code = rc") is None


def test_tamper_list_is_one_constant_enforced_by_the_grader() -> None:
    assert len(TAMPER_SIGNATURES) >= 6
    for signature in TAMPER_SIGNATURES:
        re.compile(signature)
    wrapper = render_wrapper_test_sh_v2("/testbed")
    assert "|".join(TAMPER_SIGNATURES) in wrapper


def test_drop_filter_keeps_source_fixes_drops_infra_and_hidden_paths() -> None:
    hidden = {"miio/tests/test_miot_models.py", "mimo_test_command.sh"}
    assert drop_reason("miio/miot_models.py", hidden) is None
    assert drop_reason("miio/tests/conftest.py", hidden) == "test-infra"
    assert drop_reason("conftest.py", hidden) == "test-infra"
    assert drop_reason("pytest.ini", hidden) == "test-infra"
    assert drop_reason("tox.ini", hidden) == "test-infra"
    assert drop_reason("pkg/sitecustomize.py", hidden) == "test-infra"
    assert drop_reason("pkg/usercustomize.py", hidden) == "test-infra"
    assert drop_reason("pkg/hooks.pth", hidden) == "test-infra"
    assert drop_reason("pkg/x_test.go", hidden) == "test-infra"
    assert drop_reason("miio/tests/test_miot_models.py", hidden) == "hidden-test path"
    assert drop_reason("mimo_test_command.sh", hidden) == "hidden-test path"
    assert is_test_infra_filename("src/main.go") is False


def test_pytest_config_and_testmain_drops_need_content() -> None:
    assert is_pytest_config_tamper("setup.cfg", "+[tool:pytest]\n+addopts = -p no:randomly")
    assert is_pytest_config_tamper("pyproject.toml", "+requires = [pytest]")
    assert not is_pytest_config_tamper("setup.cfg", "+timeout = 5\n+retries = 0")
    assert not is_pytest_config_tamper("setup.py", "+import pytest")
    assert declares_testmain("package foo\nfunc TestMain(m *testing.M) { os.Exit(0) }")
    assert not declares_testmain("package foo\nfunc TestHelper(t *testing.T) {}")


def test_named_ids_parse_only_from_the_command_section() -> None:
    patch = (
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "+++ b/mimo_test_command.sh\n"
        "+python -m pytest -v pkg/test_a.py::test_one pkg/test_b.py::TestC::test_two[k]\n"
    )
    assert parse_named_pytest_ids(patch) == {
        "pkg/test_a.py::test_one",
        "pkg/test_b.py::TestC::test_two[k]",
    }
    # Base64-encoded commands carry no parseable id: the presence check is skipped.
    assert parse_named_pytest_ids("+echo aGVsbG8= | base64 -d | sh\n") == set()


PASS_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite tests="2">'
    b'<testcase classname="pkg.test_a" name="test_one"/>'
    b'<testcase classname="pkg.test_b.TestC" name="test_two" />'
    b"</testsuite>"
)
NAMED = {"pkg/test_a.py::test_one"}


def test_junit_grading_rewards_clean_structured_passes() -> None:
    assert evaluate_junit(PASS_JUNIT, 0, NAMED) == 1
    assert evaluate_junit(PASS_JUNIT, 0, set()) == 1


def test_junit_grading_rejects_failures_skips_and_missing_ids() -> None:
    failed = PASS_JUNIT.replace(b"name=\"test_one\"/>", b'name="test_one"><failure message="x"/></testcase>')
    assert evaluate_junit(failed, 0, NAMED) == 0
    errored = PASS_JUNIT.replace(b"name=\"test_one\"/>", b'name="test_one"><error message="x"/></testcase>')
    assert evaluate_junit(errored, 0, NAMED) == 0
    skipped = PASS_JUNIT.replace(b"name=\"test_one\"/>", b'name="test_one"><skipped/></testcase>')
    assert evaluate_junit(skipped, 0, NAMED) == 0
    assert evaluate_junit(PASS_JUNIT, 1, NAMED) == 0
    assert evaluate_junit(PASS_JUNIT, 0, {"pkg/test_a.py::test_missing"}) == 0
    assert evaluate_junit(b'<testsuite tests="0"></testsuite>', 0, set()) == 0


def test_junit_grading_falls_back_to_exit_code_without_a_report() -> None:
    assert evaluate_junit(None, 0, NAMED) == 1
    assert evaluate_junit(None, 1, NAMED) == 0
    assert evaluate_junit(b"not xml <", 0, NAMED) == 1
    assert evaluate_junit(b"not xml <", 2, NAMED) == 0


def test_junit_grading_accepts_parametrized_id_prefixes() -> None:
    parametrized = PASS_JUNIT.replace(b'name="test_one"', b'name="test_one[k]"')
    assert evaluate_junit(parametrized, 0, NAMED) == 1


def test_v2_snapshot_carries_workspace_only(parent_dir_v2: Path) -> None:
    changes, _ = build_changes_v2(parent_dir_v2, marker=MARKER)
    config = tomllib.loads(changes["task.toml"].decode("utf-8"))
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["artifacts"] == ["/var/tmp/mimo-separate", "/logs/agent/trajectory.json"]
    hooks = config["verifier"]["collect"]
    assert MARKER in hooks[0]["command"]
    snapshot = hooks[1]["command"]
    assert "workspace.tgz" in snapshot
    assert "git-hidden" not in snapshot
    assert "/base" not in snapshot


def test_v2_variant_bundles_setup_and_grades_without_agent_state(
    parent_dir_v2: Path,
) -> None:
    changes, inputs = build_changes_v2(parent_dir_v2, marker=MARKER)
    assert set(changes) == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    assert "tests/test-orig.sh" not in changes
    wrapper = changes["tests/test.sh"].decode("utf-8")
    # (the bundle lives at the /tests copy, so the in-image relative name).
    assert V2_SETUP_SUBDIR.split("/", 1)[1] in wrapper
    assert 'bash "$M/setup.sh"' in wrapper
    # It never restores the agent's git objects or base sha.
    # The structured report lands outside the repo, out of the code's reach,
    # and the grader persists its junit verdict next to the reward.
    assert V2_GRADE_DIR in wrapper
    assert "junit-grade.log" in wrapper
    assert inputs["setup_files"] == sorted(inputs["setup_files"])
    assert len(inputs["setup_sha256"]) == 64


def test_v2_solution_injected_only_when_parent_has_none(parent_dir_v2: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, _ = build_changes_v2(parent_dir_v2, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    (parent_dir_v2 / "solution").mkdir()
    (parent_dir_v2 / "solution" / "solve.sh").write_bytes(b"#!/bin/bash\n")
    with pytest.raises(VariantInvalid):
        build_changes_v2(parent_dir_v2, marker=MARKER, solution_sh=solve)


def test_v2_refuses_without_a_setup_chain(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes_v2(parent_dir, marker=MARKER)
    with pytest.raises(VariantInvalid):
        render_wrapper_test_sh_v2("relative/path")


def test_v2_derive_records_transform_id(parent_dir_v2: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier_v2(
        parent_dir_v2,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir_v2)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID_V2
    assert {change.path for change in record.files} == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    expected_record = (
        tmp_path / "library" / "task-variants" / record.task_slug / f"{record.digest12}.json"
    )
    assert expected_record.is_file()
    assert (store / record.task_slug / record.digest12).is_dir()


# --------------------------------------------------------------------------- #
# junit absence after a pytest start (obfuscated exit-forcing close)
# --------------------------------------------------------------------------- #

PYTEST_CMD = "bash /testbed/mimo_test_command.sh"
PYTEST_OUT = "=== test session starts ===\ncollected 4 items\n"


def test_addopts_cleared_detects_unset_and_override() -> None:
    assert addopts_cleared("export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\nunset PYTEST_ADDOPTS PYTEST_PLUGINS")
    assert addopts_cleared("PYTEST_ADDOPTS='' python -m pytest")
    assert addopts_cleared("env -u PYTEST_ADDOPTS python -m pytest")
    assert not addopts_cleared("python -m pytest tests/ -v")
    assert not addopts_cleared(PYTEST_CMD)


def test_detect_pytest_run_needs_markers_and_a_pytest_command() -> None:
    assert detect_pytest_run(PYTEST_OUT, "python -m pytest tests/ -v")
    assert detect_pytest_run("collected 13 items\n", "python -m pytest -k x")
    assert not detect_pytest_run(PYTEST_OUT, "python -m unittest discover")
    assert not detect_pytest_run("ok\n", "python -m pytest tests/")
    assert not detect_pytest_run("", "python -m pytest tests/")


def test_resolve_command_text_decodes_base64_forms() -> None:
    import base64

    hidden = base64.b64encode(b"exec python -m pytest tests/ -v; unset PYTEST_ADDOPTS").decode()
    resolved = resolve_command_text(
        "bash /testbed/mimo_test_command.sh",
        f"echo {hidden} | base64 -d | sh",
        "diff --git a/mimo_test_command.sh",
    )
    assert "pytest" in resolved
    assert addopts_cleared(resolved)
    plain = resolve_command_text(PYTEST_CMD, "python -m pytest tests/ -v", "")
    assert "pytest" in plain
    assert not addopts_cleared(plain)


def test_missing_junit_after_pytest_start_grades_zero() -> None:
    assert evaluate_junit(None, 0, set(), suspicious_absence=True) == 0
    assert evaluate_junit(b"not xml <", 0, set(), suspicious_absence=True) == 0
    assert evaluate_junit(None, 0, set(), suspicious_absence=False) == 1
    assert evaluate_junit(None, 1, set(), suspicious_absence=True) == 0


def test_obfuscated_exit_forcing_needs_the_junit_rule() -> None:
    # No literal signature survives string-splitting, so the gate passes ...
    added = "+getattr(__import__('o'+'s'), '_e'+'xit')(0)\n"
    assert tamper_signature_hit(added) is None
    # ... but the missing report after a pytest start still grades 0.
    assert evaluate_junit(None, 0, set(), suspicious_absence=True) == 0


def test_missing_reason_is_logged_by_the_grader() -> None:
    assert JUNIT_MISSING_REASON == "junit missing after pytest start"
    assert len(PYTEST_START_MARKERS) >= 1
    wrapper = render_wrapper_test_sh_v2("/testbed")
    assert JUNIT_MISSING_REASON in wrapper
    assert "PYTHONUNBUFFERED=1" in wrapper


def test_junit_absence_rule_covers_markers_and_blank_output() -> None:
    cmd = "python -m pytest tests/ -v"
    assert output_is_blank("")
    assert output_is_blank("  \n ")
    assert not output_is_blank(PYTEST_OUT)
    # Markers: pytest ran, report suppressed.
    assert junit_absence_suspicious(PYTEST_OUT, cmd)
    # Blank: pytest should have printed at least its header.
    assert junit_absence_suspicious("", cmd)
    assert junit_absence_suspicious("  \n", cmd)
    # Non-blank markerless output: pytest never started (custom commands).
    assert not junit_absence_suspicious("hello\n", cmd)
    # Unset addopts or non-pytest commands keep the fallback.
    assert not junit_absence_suspicious(PYTEST_OUT, "unset PYTEST_ADDOPTS\n" + cmd)
    assert not junit_absence_suspicious("", "python -m unittest discover")
    assert not junit_absence_suspicious(PYTEST_OUT, "python -m unittest discover")


def test_blank_kill_grades_zero_through_the_full_rule() -> None:
    cmd = "python -m pytest tests/ -v"
    assert junit_absence_suspicious("", cmd)
    assert evaluate_junit(None, 0, set(), suspicious_absence=True) == 0
