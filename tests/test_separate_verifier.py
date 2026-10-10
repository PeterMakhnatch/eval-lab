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
    TRANSFORM_ID_V3,
    TRANSFORM_ID_V4,
    TRANSFORM_ID_V5,
    TRANSFORM_ID_V6,
    V2_GRADE_DIR,
    V2_SETUP_SUBDIR,
    V3_CONFTEST_HOOK,
    V3_NEW_INFRA_BASENAMES,
    V3_STRUCTURED_RUNNERS,
    V3_TAMPER_SIGNATURES,
    V5_ARCHIVE_HOOK,
    addopts_cleared,
    build_changes,
    build_changes_v2,
    build_changes_v3,
    build_changes_v4,
    build_changes_v5,
    build_changes_v6,
    declares_testmain,
    derive_separate_verifier,
    derive_separate_verifier_v2,
    derive_separate_verifier_v3,
    derive_separate_verifier_v4,
    derive_separate_verifier_v5,
    derive_separate_verifier_v6,
    detect_pytest_run,
    detect_runner,
    drop_reason,
    evaluate_cargo_output,
    evaluate_go_output,
    evaluate_go_output_v6,
    evaluate_js_output,
    evaluate_junit,
    evaluate_junit_v4,
    evaluate_junit_v5,
    evaluate_junit_v6,
    evaluate_phpunit_output,
    evaluate_rspec_output,
    evaluate_surefire_reports,
    evaluate_unittest,
    evaluate_unittest_v6,
    is_pytest_config_tamper,
    is_test_infra_filename,
    is_v3_new_infra,
    junit_absence_suspicious,
    junit_case_matches_expected,
    junit_case_status,
    junit_status_map,
    output_is_blank,
    parse_named_pytest_ids,
    parse_pytest_node_id,
    read_parent_info,
    render_wrapper_test_sh_v2,
    render_wrapper_test_sh_v3,
    render_wrapper_test_sh_v4,
    render_wrapper_test_sh_v5,
    render_wrapper_test_sh_v6,
    resolve_command_text,
    tamper_signature_hit,
    v3_config_revert_reason,
    v3_tamper_hit_for_file,
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
    failed = PASS_JUNIT.replace(
        b'name="test_one"/>', b'name="test_one"><failure message="x"/></testcase>'
    )
    assert evaluate_junit(failed, 0, NAMED) == 0
    errored = PASS_JUNIT.replace(
        b'name="test_one"/>', b'name="test_one"><error message="x"/></testcase>'
    )
    assert evaluate_junit(errored, 0, NAMED) == 0
    skipped = PASS_JUNIT.replace(b'name="test_one"/>', b'name="test_one"><skipped/></testcase>')
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
    assert addopts_cleared(
        "export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\nunset PYTEST_ADDOPTS PYTEST_PLUGINS"
    )
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


# --------------------------------------------------------------------------- #
# separate-verifier@3 (multi-runner patch-only verifier)
# --------------------------------------------------------------------------- #


def test_v3_signatures_extend_v2_and_stay_ere_compatible() -> None:
    assert set(TAMPER_SIGNATURES) <= set(V3_TAMPER_SIGNATURES)
    assert len(V3_TAMPER_SIGNATURES) > len(TAMPER_SIGNATURES)
    for signature in V3_TAMPER_SIGNATURES:
        assert "(?" not in signature  # no lookaround: shared with grep -E
        re.compile(signature)


def test_v3_tamper_gate_catches_exit_forcing_per_language() -> None:
    assert v3_tamper_hit_for_file("pkg/x.go", "+  os.Exit(0)\n") is not None
    assert v3_tamper_hit_for_file("lib/a.js", "+process.exit(0)\n") is not None
    assert v3_tamper_hit_for_file("lib/a.ts", "+process.exitCode = 1\n") is not None
    assert v3_tamper_hit_for_file("src/Main.java", "+    System.exit(0);\n") is not None
    assert v3_tamper_hit_for_file("src/lib.rs", "+    std::process::exit(0);\n") is not None
    assert v3_tamper_hit_for_file("lib/x.rb", "+  Kernel.exit(0)\n") is not None
    assert v3_tamper_hit_for_file("jest.setup.js", "+global.it = function () {};\n") is not None
    # Scoped exit(0): Ruby/PHP only; C/shell fixes may legitimately exit.
    assert v3_tamper_hit_for_file("lib/x.rb", "+  exit(0)\n") is not None
    assert v3_tamper_hit_for_file("lib/x.php", "+exit(0);\n") is not None
    assert v3_tamper_hit_for_file("lib/x.php", "+die(0);\n") is not None
    assert v3_tamper_hit_for_file("src/x.c", "+  exit(0);\n") is None
    assert v3_tamper_hit_for_file("run.sh", "+exit 0\n") is None


def test_v3_tamper_gate_passes_plain_fixes() -> None:
    assert v3_tamper_hit_for_file("pkg/x.go", '+  return fmt.Errorf("bad %s", v)\n') is None
    assert v3_tamper_hit_for_file("lib/a.js", "+  return { status: 200 };\n") is None
    assert v3_tamper_hit_for_file("src/x.c", "+  return EXIT_FAILURE;\n") is None
    assert v3_tamper_hit_for_file("lib/x.rb", '+  raise ArgumentError, "bad"\n') is None
    assert (
        v3_tamper_hit_for_file("src/Main.java", "+  throw new IllegalStateException();\n") is None
    )
    assert v3_tamper_hit_for_file("lib/x.py", "+latexit = True\n") is None


def test_v3_new_infra_drops_only_new_files() -> None:
    assert is_v3_new_infra("jest.setup.js", in_base=False)
    assert is_v3_new_infra("config/jest.config.ts", in_base=False)
    assert is_v3_new_infra(".mocharc.json", in_base=False)
    assert is_v3_new_infra("vitest.workspace.ts", in_base=False)
    assert is_v3_new_infra("build.rs", in_base=False)
    assert is_v3_new_infra("phpunit.xml.dist", in_base=False)
    assert is_v3_new_infra("target/surefire-reports/TEST-x.xml", in_base=False)
    assert is_v3_new_infra("build/test-results/test/TEST-x.xml", in_base=False)
    # Tracked files are never name-dropped (hunk rules decide instead).
    assert not is_v3_new_infra("jest.setup.js", in_base=True)
    assert not is_v3_new_infra("build.rs", in_base=True)
    assert not is_v3_new_infra("target/surefire-reports/TEST-x.xml", in_base=True)
    # Source files are never infra.
    assert not is_v3_new_infra("src/main.go", in_base=False)
    assert not is_v3_new_infra("package.json", in_base=False)


def test_v3_config_revert_rules_catch_runner_keys_only() -> None:
    assert (
        v3_config_revert_reason("package.json", '+  "jest": {"setupFiles": ["./evil.js"]}')
        is not None
    )
    assert (
        v3_config_revert_reason("package.json", '+    "mocha": {"require": "./stub.js"}')
        is not None
    )
    assert (
        v3_config_revert_reason(
            "pom.xml", "+<artifactId>maven-surefire-plugin</artifactId>\n+<excludes>"
        )
        is not None
    )
    assert v3_config_revert_reason("build.gradle", '+  exclude "hidden/**"') is not None
    assert v3_config_revert_reason(".rspec", "+--require ./planted_stub") is not None
    assert v3_config_revert_reason("phpunit.xml", '+  bootstrap="tests/evil.php"') is not None
    assert v3_config_revert_reason("go.mod", "+toolchain go1.24.1") is not None
    assert v3_config_revert_reason(".mocharc.json", '+  "spec": "test/empty-*.js"') is not None
    # Honest dependency/source hunks survive.
    assert v3_config_revert_reason("package.json", '+    "lodash": "^4.17.21"') is None
    assert (
        v3_config_revert_reason("pom.xml", "+<artifactId>maven-compiler-plugin</artifactId>")
        is None
    )
    assert v3_config_revert_reason("go.mod", "+require example.com/mod v1.2.3") is None
    assert v3_config_revert_reason("src/main.go", '+  "jest": true') is None


def test_detect_runner_covers_measured_mix() -> None:
    assert detect_runner("go test -v ./...") == "go-test"
    assert detect_runner('exec "$GO_BIN" test -mod=readonly -v ./pkg') == "go-test"
    assert detect_runner("npx jest src/a.test.jsx --verbose") == "jest"
    assert detect_runner("npx vitest run") == "vitest"
    assert detect_runner("npx mocha test/BootBot.spec.js") == "mocha"
    assert detect_runner("./node_modules/.bin/_mocha out/") == "mocha"
    assert detect_runner("node --test out/test.js") == "node-test"
    assert detect_runner("cargo test --offline") == "cargo-test"
    assert detect_runner("bundle exec rspec spec/") == "rspec"
    assert detect_runner("vendor/bin/phpunit --verbose") == "phpunit"
    assert detect_runner("mvn -o -Dtest=XTest test") == "mvn"
    assert detect_runner("./gradlew :mod:test") == "gradle"
    assert detect_runner("python -m pytest tests/ -v") == "pytest"
    assert detect_runner("python -m unittest tests.test_x -v") == "unittest"
    assert detect_runner("forge test -vvv") == "forge"
    assert detect_runner("bash bin/test.sh") == "custom"
    assert detect_runner("bash usercase-test-coderl/usecase.sh") == "custom"


def test_evaluate_go_output() -> None:
    passing = "=== RUN TestX\n--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/mod/pkg\t0.1s\n"
    assert evaluate_go_output(passing, 0) == 1
    assert evaluate_go_output(passing, 1) == 0
    failing = "--- FAIL: TestX (0.00s)\nFAIL\nexit status 1\nFAIL\texample.com/mod/pkg\n"
    assert evaluate_go_output(failing, 1) == 0
    assert evaluate_go_output("?   \texample.com/mod/pkg\t[no test files]\n", 0) == 0
    assert evaluate_go_output("", 0) == 0
    assert evaluate_go_output("panic: runtime error\n", 0) == 0


def test_evaluate_js_output() -> None:
    jest_pass = "Tests:       4 passed, 4 total\nTest Suites: 1 passed, 1 total\n"
    assert evaluate_js_output("jest", jest_pass, 0) == 1
    assert evaluate_js_output("jest", "Tests: 1 failed, 3 passed, 4 total\n", 0) == 0
    assert evaluate_js_output("jest", "", 0) == 0
    mocha_pass = "  9 passing (20ms)\n"
    assert evaluate_js_output("mocha", mocha_pass, 0) == 1
    assert evaluate_js_output("mocha", "  9 passing (20ms)\n  2 failing\n", 1) == 0
    assert evaluate_js_output("mocha", "  9 passing (20ms)\n  0 failing\n", 0) == 1
    vitest_pass = " Test Files  2 passed (2)\n      Tests  8 passed (8)\n"
    assert evaluate_js_output("vitest", vitest_pass, 0) == 1
    assert evaluate_js_output("vitest", " Tests  1 failed | 7 passed (8)\n", 1) == 0
    tap_pass = "ok 1 - first\nok 2 - second\n# pass 2\n"
    assert evaluate_js_output("tap", tap_pass, 0) == 1
    assert evaluate_js_output("tap", "not ok 1 - first\n", 1) == 0


def test_evaluate_unittest() -> None:
    passing = "Ran 3 tests in 0.01s\n\nOK\n"
    assert evaluate_unittest(passing, 0) == 1
    assert evaluate_unittest(passing, 1) == 0
    assert evaluate_unittest("Ran 3 tests in 0.01s\n\nFAILED (failures=1)\n", 1) == 0
    assert evaluate_unittest("Ran 0 tests in 0.00s\n\nOK\n", 0) == 0
    assert evaluate_unittest("", 0) == 0


def test_evaluate_rspec_phpunit_cargo() -> None:
    assert evaluate_rspec_output("3 examples, 0 failures\n", 0) == 1
    assert evaluate_rspec_output("3 examples, 1 failure\n", 1) == 0
    assert evaluate_rspec_output("", 0) == 0
    assert evaluate_phpunit_output("OK (7 tests, 14 assertions)\n", 0) == 1
    assert evaluate_phpunit_output("FAILURES!\nTests: 7, Assertions: 10, Failures: 1.\n", 1) == 0
    assert evaluate_cargo_output("test result: ok. 5 passed; 0 failed\n", 0) == 1
    assert evaluate_cargo_output("test result: FAILED. 4 passed; 1 failed\n", 101) == 0
    assert evaluate_cargo_output("", 0) == 0


def test_evaluate_surefire_reports() -> None:
    passing = (
        b'<testsuite tests="2"><testcase classname="X" name="a"/>'
        b'<testcase classname="X" name="b"/></testsuite>'
    )
    assert evaluate_surefire_reports([passing], 0) == 1
    assert evaluate_surefire_reports([passing], 1) == 0
    failing = (
        b'<testsuite tests="1"><testcase classname="X" name="a">'
        b'<failure message="x"/></testcase></testsuite>'
    )
    assert evaluate_surefire_reports([failing], 0) == 0
    skipped = (
        b'<testsuite tests="2"><testcase classname="X" name="a"/>'
        b'<testcase classname="X" name="b"><skipped/></testcase></testsuite>'
    )
    assert evaluate_surefire_reports([skipped], 0) == 1
    assert evaluate_surefire_reports([], 0) is None
    assert evaluate_surefire_reports([b"not xml <"], 0) == 0


def _v3_grader_block() -> str:
    """Extract the embedded @3 grading script (last PYEOF heredoc)."""
    import re as _re

    from evallab.separate_verifier import render_wrapper_test_sh_v3 as _render

    blocks = _re.findall(r"<<'PYEOF'.*?\n(.*?)\nPYEOF", _render("/testbed"), _re.S)
    assert len(blocks) == 2
    return blocks[1]


def _run_embedded_grader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_cmd: str,
    output: str,
    rc: int,
    junit: bytes | None = None,
    patch_extra: str = "",
) -> str:
    """Run the shipped @3 grading script against synthetic files; return reward."""
    junit_path = tmp_path / "junit.xml"
    if junit is None:
        if junit_path.exists():
            junit_path.unlink()
    else:
        junit_path.write_bytes(junit)
    patch_path = tmp_path / "test.patch"
    patch_path.write_text(
        f"diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n+{runner_cmd}\n{patch_extra}",
        encoding="utf-8",
    )
    output_path = tmp_path / "test_output.log"
    output_path.write_text(output, encoding="utf-8")
    cmd_path = tmp_path / "test_command.sh"
    cmd_path.write_text(runner_cmd, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "grader",
            str(junit_path),
            str(rc),
            str(patch_path),
            str(output_path),
            str(cmd_path),
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit):
        exec(compile(_v3_grader_block(), "v3grader", "exec"), {"__name__": "v3grader"})
    return capsys.readouterr().out.strip()


def test_embedded_grader_matches_pure_evaluators(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    go_pass = "=== RUN TestX\n--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/m\t0.1s\n"
    assert (
        _run_embedded_grader(
            tmp_path, capsys, monkeypatch, runner_cmd="go test -v ./...", output=go_pass, rc=0
        )
        == "1"
    )
    assert (
        _run_embedded_grader(
            tmp_path, capsys, monkeypatch, runner_cmd="go test -v ./...", output="", rc=0
        )
        == "0"
    )
    jest_pass = "Tests:       4 passed, 4 total\nTest Suites: 1 passed, 1 total\n"
    assert (
        _run_embedded_grader(
            tmp_path, capsys, monkeypatch, runner_cmd="npx jest a.test.jsx", output=jest_pass, rc=0
        )
        == "1"
    )
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="npx jest a.test.jsx",
            output="Tests: 1 failed\n",
            rc=1,
        )
        == "0"
    )
    unit_pass = "Ran 3 tests in 0.01s\n\nOK\n"
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="python -m unittest tests.test_x",
            output=unit_pass,
            rc=0,
        )
        == "1"
    )
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="python -m unittest tests.test_x",
            output="",
            rc=0,
        )
        == "0"
    )
    # pytest branch keeps @2 semantics: structured pass grades 1.
    from evallab.separate_verifier import resolve_command_text as _resolve

    assert _resolve("bash /testbed/mimo_test_command.sh", None, "x") is not None
    pytest_out = "=== test session starts ===\ncollected 1 item\n"
    junit_pass = b'<testsuite tests="1"><testcase classname="t" name="x"/></testsuite>'
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="python -m pytest tests/ -v",
            output=pytest_out,
            rc=0,
            junit=junit_pass,
        )
        == "1"
    )
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="python -m pytest tests/ -v",
            output=pytest_out,
            rc=0,
            junit=None,
        )
        == "0"
    )
    # Silent families keep the exit-code fallback.
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="bash usercase-test-coderl/usecase.sh",
            output="",
            rc=0,
        )
        == "1"
    )
    assert (
        _run_embedded_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="bash usercase-test-coderl/usecase.sh",
            output="boom\n",
            rc=1,
        )
        == "0"
    )


def test_v3_template_pins() -> None:
    wrapper = render_wrapper_test_sh_v3("/testbed")
    assert "@@" not in wrapper
    for name in V3_NEW_INFRA_BASENAMES:
        assert name in wrapper
    for runner in (
        "go-test",
        "jest",
        "vitest",
        "mocha",
        "pytest",
        "unittest",
        "mvn",
        "rspec",
        "phpunit",
        "cargo-test",
    ):
        assert runner in wrapper
    for runner in sorted(V3_STRUCTURED_RUNNERS):
        assert runner in wrapper
    for signature in V3_TAMPER_SIGNATURES:
        assert repr(signature) in wrapper
    assert "hook_installed" in wrapper
    assert "surefire-reports" in wrapper
    assert "verifier conftest hook" in wrapper
    assert JUNIT_MISSING_REASON in wrapper
    assert "test session starts" in wrapper  # pytest @2-parity branch
    assert "RUNNER=$RUNNER" in wrapper
    assert "mimo_build_env.tar.gz.b64" in wrapper  # opaque command resolution
    assert "/testbed" in wrapper
    with pytest.raises(VariantInvalid):
        render_wrapper_test_sh_v3("relative/path")


def test_v3_conftest_hook_collects_a_junit_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit_path = tmp_path / "junit.xml"
    monkeypatch.setenv("MIMO_VERIFIER_JUNIT", str(junit_path))
    namespace: dict[str, object] = {}
    exec(compile(V3_CONFTEST_HOOK, "v3hook", "exec"), namespace)
    logreport = namespace["pytest_runtest_logreport"]  # type: ignore[operator]
    sessionfinish = namespace["pytest_sessionfinish"]  # type: ignore[operator]

    class _Report:
        def __init__(self, nodeid: str, when: str, outcome: str, skipped: bool = False) -> None:
            self.nodeid = nodeid
            self.when = when
            self.outcome = "skipped" if skipped else outcome
            self.skipped = skipped

    logreport(_Report("test_x.py::test_a", "call", "passed"))
    logreport(_Report("test_x.py::test_b", "call", "failed"))
    logreport(_Report("test_x.py::test_c", "setup", "passed", skipped=True))
    sessionfinish(None, 0)
    import xml.etree.ElementTree as _ET

    cases = list(_ET.parse(str(junit_path)).getroot().iter("testcase"))
    assert len(cases) == 3
    assert sum(1 for c in cases if c.find("failure") is not None) == 1
    assert sum(1 for c in cases if c.find("skipped") is not None) == 1


def test_v3_variant_bundles_setup_and_records_runner(parent_dir_v2: Path) -> None:
    changes, inputs = build_changes_v3(parent_dir_v2, marker=MARKER)
    assert set(changes) == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    assert "tests/test-orig.sh" not in changes
    wrapper = changes["tests/test.sh"].decode("utf-8")
    assert "@@" not in wrapper
    assert "RUNNER=$RUNNER" in wrapper
    assert inputs["runner"] in ("custom", "pytest", "unittest")
    assert len(inputs["setup_sha256"]) == 64


def test_v3_solution_injected_only_when_parent_has_none(parent_dir_v2: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, _ = build_changes_v3(parent_dir_v2, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    (parent_dir_v2 / "solution").mkdir()
    (parent_dir_v2 / "solution" / "solve.sh").write_bytes(b"#!/bin/bash\n")
    with pytest.raises(VariantInvalid):
        build_changes_v3(parent_dir_v2, marker=MARKER, solution_sh=solve)


def test_v3_derive_records_transform_id(parent_dir_v2: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier_v3(
        parent_dir_v2,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir_v2)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID_V3
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


def test_v3_refuses_without_a_setup_chain(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes_v3(parent_dir, marker=MARKER)
    with pytest.raises(VariantInvalid):
        build_changes_v3(parent_dir, marker="  ")


# --------------------------------------------------------------------------- #
# separate-verifier@4 (rootdir-robust junit matching)
# --------------------------------------------------------------------------- #
#
# The fixtures below mirror the Daytona oracle evidence for the
# cloud-sql-connector family (representative: format-code-task-000102):
# the test command pins ``tests/unit/test_iam_user_format.py::test_*`` ids
# while pytest runs with rootdir=/testbed/tests, so junit classnames carry
# no ``tests/`` segment (``unit.test_iam_user_format``). @3 exact-matching
# graded that 8/8-passing oracle 0 (missing=all); @4 suffix-matching
# grades it 1.

_V4_SHIFTED_TESTS = (
    "test_postgres_iam_user_with_gserviceaccount_suffix_is_truncated",
    "test_postgres_iam_user_without_suffix_is_unchanged",
    "test_postgres_iam_bare_username_is_unchanged",
    "test_mysql_iam_user_with_at_sign_is_truncated",
    "test_mysql_iam_service_account_user_is_truncated",
    "test_mysql_iam_bare_username_is_unchanged",
    "test_postgres_no_iam_auth_user_is_unchanged",
    "test_mysql_no_iam_auth_user_is_unchanged",
)


def _shifted_junit(*, with_file: bool = False) -> bytes:
    """Rootdir-shifted junit: classnames without the ``tests/`` segment."""
    cases = "".join(
        "<testcase"
        + f' classname="unit.test_iam_user_format" name="{test}"'
        + (' file="tests/unit/test_iam_user_format.py"' if with_file else "")
        + "/>"
        for test in _V4_SHIFTED_TESTS
    )
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="8">'
        + cases.encode("utf-8")
        + b"</testsuite>"
    )


def _shifted_named() -> set[str]:
    return {f"tests/unit/test_iam_user_format.py::{test}" for test in _V4_SHIFTED_TESTS}


def test_parse_pytest_node_id_splits_module_classes_and_test() -> None:
    assert parse_pytest_node_id("pkg/test_a.py::test_one") == (
        ["pkg", "test_a"],
        [],
        "test_one",
    )
    assert parse_pytest_node_id("pkg/test_b.py::TestC::test_two[k]") == (
        ["pkg", "test_b"],
        ["TestC"],
        "test_two[k]",
    )
    assert parse_pytest_node_id("./tests/unit/test_x.py::test_y") == (
        ["tests", "unit", "test_x"],
        [],
        "test_y",
    )


def test_v4_matcher_accepts_rootdir_shifted_ids() -> None:
    for test in _V4_SHIFTED_TESTS:
        assert junit_case_matches_expected(
            "unit.test_iam_user_format",
            test,
            None,
            f"tests/unit/test_iam_user_format.py::{test}",
        )
    # Unshifted reports keep matching too.
    assert junit_case_matches_expected(
        "tests.unit.test_iam_user_format",
        _V4_SHIFTED_TESTS[0],
        None,
        f"tests/unit/test_iam_user_format.py::{_V4_SHIFTED_TESTS[0]}",
    )


def test_v4_matcher_handles_file_attribute_nested_classes_and_params() -> None:
    # junit file attribute locates the module; the classname prefix locates classes.
    assert junit_case_matches_expected(
        "test_b.TestC",
        "test_two",
        "tests/unit/test_b.py",
        "tests/unit/test_b.py::TestC::test_two",
    )
    # Absolute file paths (rootdir-prefixed) align by suffix.
    assert junit_case_matches_expected(
        "unit.test_b.TestC",
        "test_two",
        "/testbed/tests/unit/test_b.py",
        "tests/unit/test_b.py::TestC::test_two",
    )
    # Nested class via classname, with a rootdir shift on top.
    assert junit_case_matches_expected(
        "test_b.TestC",
        "test_two[k]",
        None,
        "pkg/test_b.py::TestC::test_two[k]",
    )
    # Expected id without params covers parametrized cases (the @3 prefix rule).
    assert junit_case_matches_expected(
        "pkg.test_b.TestC", "test_two[k2]", None, "pkg/test_b.py::TestC::test_two"
    )
    # Expected id with params needs the exact params.
    assert not junit_case_matches_expected(
        "pkg.test_b.TestC", "test_two[k2]", None, "pkg/test_b.py::TestC::test_two[k1]"
    )


def test_v4_matcher_never_matches_a_different_test_name() -> None:
    expected = "tests/unit/test_iam_user_format.py::test_mysql_iam_bare_username_is_unchanged"
    assert not junit_case_matches_expected(
        "unit.test_iam_user_format", "test_postgres_iam_bare_username_is_unchanged", None, expected
    )
    # Prefix and superstring names are different tests.
    assert not junit_case_matches_expected(
        "unit.test_iam_user_format",
        "test_mysql_iam_bare_username_is_unchanged_extra",
        None,
        expected,
    )
    assert not junit_case_matches_expected(
        "unit.test_iam_user_format", "test_mysql_iam_bare_username", None, expected
    )
    # Same test name in another module or another class is a different test.
    assert not junit_case_matches_expected(
        "other.test_iam_user_format",
        "test_mysql_iam_bare_username_is_unchanged",
        None,
        expected,
    )
    assert not junit_case_matches_expected(
        "unit.test_iam_user_format.TestC",
        "test_mysql_iam_bare_username_is_unchanged",
        None,
        expected,
    )
    assert not junit_case_matches_expected(
        "unit.test_iam_user_format",
        "test_mysql_iam_bare_username_is_unchanged",
        None,
        "tests/unit/test_iam_user_format.py::TestC::test_mysql_iam_bare_username_is_unchanged",
    )


def test_v4_matcher_handles_class_level_expected_ids() -> None:
    # 000242's shape: the command pins the class, whose member tests pass.
    assert junit_case_matches_expected(
        "cartopy.tests.test_polygon.TestDatelineRepeatedVertex",
        "test_no_polygon_fills_entire_domain",
        None,
        "lib/cartopy/tests/test_polygon.py::TestDatelineRepeatedVertex",
    )
    # A member of another class never satisfies a class-level id.
    assert not junit_case_matches_expected(
        "cartopy.tests.test_polygon.TestOther",
        "test_something",
        None,
        "lib/cartopy/tests/test_polygon.py::TestDatelineRepeatedVertex",
    )
    # A bare function with exactly the expected name is the named test
    # (function-level exact match; the id is ambiguous, the match is sound).
    assert junit_case_matches_expected(
        "cartopy.tests.test_polygon",
        "TestDatelineRepeatedVertex",
        None,
        "lib/cartopy/tests/test_polygon.py::TestDatelineRepeatedVertex",
    )
    # A `test_`-prefixed id names a function, never a class.
    assert not junit_case_matches_expected(
        "pkg.test_b.test_two",
        "test_three",
        None,
        "pkg/test_b.py::test_two",
    )


def test_v4_grading_fixes_the_daytona_oracle_while_v3_stays_exact() -> None:
    shifted = _shifted_junit()
    # The Daytona oracle shape: rc 0, all 8 passing, @3 grades 0 (missing=all).
    assert evaluate_junit(shifted, 0, _shifted_named()) == 0
    assert evaluate_junit_v4(shifted, 0, _shifted_named()) == 1
    # Same with the junit file attribute present.
    assert evaluate_junit_v4(_shifted_junit(with_file=True), 0, _shifted_named()) == 1


def test_v4_grading_keeps_every_v3_rejection() -> None:
    shifted = _shifted_junit()
    assert evaluate_junit_v4(shifted, 1, _shifted_named()) == 0
    assert evaluate_junit_v4(shifted, 0, {"tests/unit/test_iam_user_format.py::test_missing"}) == 0
    failed = shifted.replace(
        b'name="%s"/>' % _V4_SHIFTED_TESTS[0].encode(),
        b'name="%s"><failure message="x"/></testcase>' % _V4_SHIFTED_TESTS[0].encode(),
    )
    assert evaluate_junit_v4(failed, 0, _shifted_named()) == 0
    assert evaluate_junit_v4(b'<testsuite tests="0"></testsuite>', 0, set()) == 0
    # Absence semantics unchanged: exit-code fallback, suspicion grades 0.
    assert evaluate_junit_v4(None, 0, _shifted_named()) == 1
    assert evaluate_junit_v4(None, 1, _shifted_named()) == 0
    assert evaluate_junit_v4(None, 0, set(), suspicious_absence=True) == 0
    assert evaluate_junit_v4(b"not xml <", 0, _shifted_named()) == 1
    assert evaluate_junit_v4(b"not xml <", 2, _shifted_named()) == 0


def _v4_grader_block() -> str:
    """Extract the embedded @4 grading script (last PYEOF heredoc)."""
    import re as _re

    blocks = _re.findall(
        r"<<'PYEOF'.*?\n(.*?)\nPYEOF", render_wrapper_test_sh_v4("/testbed"), _re.S
    )
    assert len(blocks) == 2
    return blocks[1]


def _run_embedded_v4_grader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_cmd: str,
    output: str,
    rc: int,
    junit: bytes | None = None,
    patch_extra: str = "",
) -> str:
    """Run the shipped @4 grading script against synthetic files; return reward."""
    junit_path = tmp_path / "junit.xml"
    if junit is None:
        if junit_path.exists():
            junit_path.unlink()
    else:
        junit_path.write_bytes(junit)
    patch_path = tmp_path / "test.patch"
    patch_path.write_text(
        f"diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n+{runner_cmd}\n{patch_extra}",
        encoding="utf-8",
    )
    output_path = tmp_path / "test_output.log"
    output_path.write_text(output, encoding="utf-8")
    cmd_path = tmp_path / "test_command.sh"
    cmd_path.write_text(runner_cmd, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "grader",
            str(junit_path),
            str(rc),
            str(patch_path),
            str(output_path),
            str(cmd_path),
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit):
        exec(compile(_v4_grader_block(), "v4grader", "exec"), {"__name__": "v4grader"})
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize(
    ("actual_name", "expected_reward"), [("test_selected", "1"), ("test_different", "0")]
)
def test_embedded_v4_ignores_option_values_but_requires_selected_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    actual_name: str,
    expected_reward: str,
) -> None:
    # 000114 runs base tests with --deselect, then selects new tests from an array.
    # Attached option values must never become additional required node IDs.
    module = "hypothesis-python/tests/py3/test_lookup.py"
    runner_cmd = (
        f"python -m pytest {module} --deselect={module}::test_excluded\n"
        'python -m pytest "${NEW_TESTS[@]}"'
    )
    junit = (
        '<testsuite><testcase classname="hypothesis-python.tests.py3.test_lookup" '
        f'name="{actual_name}"/></testsuite>'
    ).encode()
    assert (
        _run_embedded_v4_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=runner_cmd,
            output="1 passed",
            rc=0,
            junit=junit,
            patch_extra=f"+  {module}::test_selected\n",
        )
        == expected_reward
    )


def test_embedded_v4_grader_grades_rootdir_shifted_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest_cmd = "python -m pytest -v " + " ".join(sorted(_shifted_named()))
    pytest_out = "=== test session starts ===\ncollected 8 items\n"
    patch_extra = "".join(f"+{node}\n" for node in sorted(_shifted_named()))
    assert (
        _run_embedded_v4_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=pytest_cmd,
            output=pytest_out,
            rc=0,
            junit=_shifted_junit(),
            patch_extra=patch_extra,
        )
        == "1"
    )
    # A different test name in the report still grades 0 (no cross-test match).
    renamed = _shifted_junit().replace(_V4_SHIFTED_TESTS[0].encode(), b"test_some_other_name")
    assert (
        _run_embedded_v4_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=pytest_cmd,
            output=pytest_out,
            rc=0,
            junit=renamed,
            patch_extra=patch_extra,
        )
        == "0"
    )
    # Class-level expected ids (000242's shape) grade through the embedded script.
    class_junit = (
        b'<testsuite tests="1">'
        b'<testcase classname="cartopy.tests.test_polygon.TestDatelineRepeatedVertex"'
        b' name="test_no_polygon_fills_entire_domain"/>'
        b"</testsuite>"
    )
    assert (
        _run_embedded_v4_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd="python -m pytest lib/cartopy/tests/test_polygon.py::TestDatelineRepeatedVertex",
            output="=== test session starts ===\ncollected 1 item\n",
            rc=0,
            junit=class_junit,
            patch_extra="+lib/cartopy/tests/test_polygon.py::TestDatelineRepeatedVertex\n",
        )
        == "1"
    )
    # Non-pytest runners keep @3 semantics through the @4 template.
    go_pass = "=== RUN TestX\n--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/m\t0.1s\n"
    assert (
        _run_embedded_v4_grader(
            tmp_path, capsys, monkeypatch, runner_cmd="go test -v ./...", output=go_pass, rc=0
        )
        == "1"
    )
    assert (
        _run_embedded_v4_grader(
            tmp_path, capsys, monkeypatch, runner_cmd="go test -v ./...", output="", rc=0
        )
        == "0"
    )


def test_v4_template_pins() -> None:
    wrapper = render_wrapper_test_sh_v4("/testbed")
    assert "@@" not in wrapper
    assert TRANSFORM_ID_V4 not in wrapper  # transform ids live in lineage, not the grader
    for runner in (
        "go-test",
        "jest",
        "vitest",
        "mocha",
        "pytest",
        "unittest",
        "mvn",
        "rspec",
        "phpunit",
        "cargo-test",
    ):
        assert runner in wrapper
    assert "hook_installed" in wrapper
    assert JUNIT_MISSING_REASON in wrapper
    assert "test session starts" in wrapper  # pytest @2-parity branch
    assert "mimo_build_env.tar.gz.b64" in wrapper  # opaque command resolution
    assert "/testbed" in wrapper
    with pytest.raises(VariantInvalid):
        render_wrapper_test_sh_v4("relative/path")


def test_v4_grader_differs_from_v3_only_in_the_junit_matcher() -> None:
    from evallab import separate_verifier as sv

    assert sv._V4_WRAPPER_C != sv._V3_WRAPPER_C
    assert sv._V4_WRAPPER_C.replace(sv._V4_JUNIT_MATCH_BLOCK, "JUNIT_MATCH") == (
        sv._V3_WRAPPER_C.replace(sv._V3_JUNIT_MATCH_BLOCK, "JUNIT_MATCH")
    )


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("edit", ["nop", "oracle", "tamper"])
def test_v4_uses_pristine_untracked_dependencies_as_the_diff_base(
    tmp_path: Path, ignored: bool, edit: str
) -> None:
    import re
    import subprocess
    import tarfile

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "core.py").write_text("VALUE = 1\n")
    (repo / ".gitignore").write_text(".venv/\n" if ignored else "")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    baked = repo / ".venv/lib/python3.11/site-packages/_pytest/__init__.py"
    baked.parent.mkdir(parents=True)
    baked.write_text("# baked runner\nimport _pytest\n")
    original_baked = baked.read_bytes()
    agent = tmp_path / "agent"
    import shutil

    shutil.copytree(repo, agent, ignore=shutil.ignore_patterns(".git"))
    if edit == "oracle":
        (agent / "core.py").write_text("VALUE = 2\n")
    if edit == "tamper":
        (agent / baked.relative_to(repo)).write_text(
            "# baked runner\nimport _pytest\n_pytest.skip_everything = True\n"
        )
    snap = tmp_path / "snapshot"
    snap.mkdir()
    with tarfile.open(snap / "workspace.tgz", "w:gz") as archive:
        archive.add(agent, arcname=".")
    tests = tmp_path / "tests"
    setup = tests / "_verifier-setup"
    setup.mkdir(parents=True)
    state = tmp_path / "state"
    (setup / "setup.sh").write_text(f"mkdir -p '{state}'\necho '{base}' > '{state}/base'\n")
    (tests / "test.patch").write_text(
        "diff --git a/hidden.txt b/hidden.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/hidden.txt\n@@ -0,0 +1 @@\n+hidden\n"
    )
    (tests / "test_command.sh").write_text(
        "python3 -c 'from core import VALUE; assert VALUE == 2'\n"
    )
    script = render_wrapper_test_sh_v4(str(repo))
    for before, after in [
        ("/var/tmp/mimo-separate", str(snap)),
        ("/var/lib/mimo-grade", str(tmp_path / "grade")),
        ("/var/lib/mimo", str(state)),
        ("/logs/verifier", str(tmp_path / "verifier")),
        ("/tests", str(tests)),
        ("/tmp/agentcopy", str(tmp_path / "agentcopy")),
        ("/tmp/agent.index", str(tmp_path / "agent.index")),
        ("/tmp/pristine.index", str(tmp_path / "pristine.index")),
        ("/tmp/kept.index", str(tmp_path / "kept.index")),
        ("/tmp/base.gitignore", str(tmp_path / "base.gitignore")),
        ("/tmp/keep.list", str(tmp_path / "keep.list")),
        ("/tmp/revert.list", str(tmp_path / "revert.list")),
    ]:
        script = re.sub(r"(?<![\w$])" + re.escape(before), lambda _, after=after: after, script)
    # Linux's coreutils timeout is not installed on the macOS test host.
    script = script.replace("timeout 1800 sh -c", "sh -c")
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    verifier = tmp_path / "verifier"
    assert (verifier / "reward.txt").read_text().strip() == ("1" if edit == "oracle" else "0")
    if edit == "tamper":
        assert "tamper" in (verifier / "tamper.log").read_text()
    else:
        assert baked.read_bytes() == original_baked
        assert ".venv" not in (verifier / "agent.kept.diff").read_text()
        assert "tamper" not in (verifier / "tamper.log").read_text()
    assert baked.relative_to(repo).as_posix() in (verifier / "pristine-extra-files.log").read_text()


def test_v4_variant_bundles_setup_and_records_runner(parent_dir_v2: Path) -> None:
    changes, inputs = build_changes_v4(parent_dir_v2, marker=MARKER)
    assert set(changes) == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    assert "tests/test-orig.sh" not in changes
    wrapper = changes["tests/test.sh"].decode("utf-8")  # type: ignore[union-attr]
    assert "@@" not in wrapper
    assert "RUNNER=$RUNNER" in wrapper
    assert inputs["runner"] in ("custom", "pytest", "unittest")
    assert len(inputs["setup_sha256"]) == 64


def test_v4_solution_injected_only_when_parent_has_none(parent_dir_v2: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, _ = build_changes_v4(parent_dir_v2, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    (parent_dir_v2 / "solution").mkdir()
    (parent_dir_v2 / "solution" / "solve.sh").write_bytes(b"#!/bin/bash\n")
    with pytest.raises(VariantInvalid):
        build_changes_v4(parent_dir_v2, marker=MARKER, solution_sh=solve)


def test_v4_derive_records_transform_id(parent_dir_v2: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier_v4(
        parent_dir_v2,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir_v2)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID_V4
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


def test_v4_refuses_without_a_setup_chain(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes_v4(parent_dir, marker=MARKER)
    with pytest.raises(VariantInvalid):
        build_changes_v4(parent_dir, marker="  ")


# --------------------------------------------------------------------------- #
# separate-verifier@5 (skip-tolerant grading, multi-phase junit union)
# --------------------------------------------------------------------------- #
#
# Fixtures mirror the census oracle evidence: 000163 (passing tests plus
# reference-declared skips, rc 0), 000203 (passing tests plus environmental
# skips, rc 0), 000200 (base && new phases where the last-phase-only report
# misses the base named IDs).

_V5_SKIP_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite tests="3">'
    b'<testcase classname="pkg.test_a" name="test_one"/>'
    b'<testcase classname="pkg.test_a" name="test_two"><skipped message="reference"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_three"><skipped message="env"/></testcase>'
    b"</testsuite>"
)
_V5_SKIP_NAMED = {"pkg/test_a.py::test_one"}

_V5_BASE_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite tests="2">'
    b'<testcase classname="ops.test_ops" name="test_base_one"/>'
    b'<testcase classname="ops.test_ops" name="test_base_two"/>'
    b"</testsuite>"
)
_V5_NEW_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite tests="1">'
    b'<testcase classname="ops.test_ops" name="test_new_only"/>'
    b"</testsuite>"
)
_V5_MULTI_NAMED = {"ops/test_ops.py::test_base_one", "ops/test_ops.py::test_new_only"}


def test_v5_grading_accepts_skips_while_v4_rejects() -> None:
    # The 000163/000203 oracle shape: passing tests plus skips, exit code 0.
    assert evaluate_junit_v4(_V5_SKIP_JUNIT, 0, _V5_SKIP_NAMED) == 0
    assert evaluate_junit_v5([_V5_SKIP_JUNIT], 0, _V5_SKIP_NAMED) == 1
    assert evaluate_junit_v5([_V5_SKIP_JUNIT], 0, set()) == 1


def test_v5_grading_keeps_every_v4_rejection() -> None:
    failed = _V5_SKIP_JUNIT.replace(b"<skipped", b"<failure")
    assert evaluate_junit_v5([failed], 0, _V5_SKIP_NAMED) == 0
    errored = _V5_SKIP_JUNIT.replace(b"<skipped", b"<error")
    assert evaluate_junit_v5([errored], 0, _V5_SKIP_NAMED) == 0
    assert evaluate_junit_v5([_V5_SKIP_JUNIT], 1, _V5_SKIP_NAMED) == 0
    assert evaluate_junit_v5([_V5_SKIP_JUNIT], 0, {"pkg/test_a.py::test_missing"}) == 0
    assert evaluate_junit_v5([b'<testsuite tests="0"></testsuite>'], 0, set()) == 0
    assert evaluate_junit_v5([], 0, _V5_SKIP_NAMED) == 0
    assert evaluate_junit_v5([None, b"not xml <"], 0, _V5_SKIP_NAMED) == 0


def test_v5_grading_unions_multi_phase_reports() -> None:
    # The 000200 oracle shape: base classes pass in phase one, new tests pass
    # in phase two; neither report alone covers every named ID.
    assert evaluate_junit_v4(_V5_NEW_JUNIT, 0, _V5_MULTI_NAMED) == 0
    assert evaluate_junit_v5([_V5_BASE_JUNIT, _V5_NEW_JUNIT], 0, _V5_MULTI_NAMED) == 1
    assert evaluate_junit_v5([_V5_NEW_JUNIT, _V5_BASE_JUNIT], 0, _V5_MULTI_NAMED) == 1
    # A failure in any phase still grades 0, as does a missing ID.
    failed_base = _V5_BASE_JUNIT.replace(
        b'name="test_base_one"/>', b'name="test_base_one"><failure/></testcase>'
    )
    assert evaluate_junit_v5([failed_base, _V5_NEW_JUNIT], 0, _V5_MULTI_NAMED) == 0
    assert evaluate_junit_v5([_V5_BASE_JUNIT], 0, _V5_MULTI_NAMED) == 0
    assert evaluate_junit_v5([_V5_BASE_JUNIT, None], 0, _V5_MULTI_NAMED) == 0


def _v5_hook_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv("MIMO_VERIFIER_ARCHIVE_DIR", str(tmp_path))
    namespace: dict[str, object] = {}
    exec(compile(V5_ARCHIVE_HOOK, "v5hook", "exec"), namespace)
    return namespace


def test_v5_archive_hook_collects_per_phase_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "phases"
    namespace = _v5_hook_namespace(archive, monkeypatch)
    logreport = namespace["pytest_runtest_logreport"]  # type: ignore[operator]
    sessionfinish = namespace["pytest_sessionfinish"]  # type: ignore[operator]

    class _Report:
        def __init__(self, nodeid: str, when: str, outcome: str, skipped: bool = False) -> None:
            self.nodeid = nodeid
            self.when = when
            self.outcome = "skipped" if skipped else outcome
            self.skipped = skipped

    logreport(_Report("tests/ops/test_ops.py::TestC::test_m[param]", "call", "passed"))
    logreport(_Report("tests/ops/test_ops.py::test_plain", "call", "failed"))
    logreport(_Report("tests/ops/test_ops.py::test_env", "setup", "passed", skipped=True))
    sessionfinish(None, 0)
    # Second session in a fresh process reuses the counter file.
    namespace["_V5_COLLECTED"] = []
    logreport(_Report("tests/ops/test_ops.py::TestC::test_second", "call", "passed"))
    sessionfinish(None, 0)

    import xml.etree.ElementTree as _ET

    first = list(_ET.parse(str(archive / "junit-hook-0.xml")).getroot().iter("testcase"))
    assert len(first) == 3
    by_name = {c.get("name"): c for c in first}
    assert by_name["test_m[param]"].get("classname") == "tests.ops.test_ops.TestC"
    assert by_name["test_m[param]"].get("file") == "tests/ops/test_ops.py"
    assert by_name["test_plain"].find("failure") is not None
    assert by_name["test_env"].find("skipped") is not None
    second = list(_ET.parse(str(archive / "junit-hook-1.xml")).getroot().iter("testcase"))
    assert [c.get("name") for c in second] == ["test_second"]
    assert (archive / "count").read_text(encoding="utf-8") == "2"
    # Without the env dir a freshly loaded hook is a silent no-op.
    monkeypatch.delenv("MIMO_VERIFIER_ARCHIVE_DIR")
    bare: dict[str, object] = {}
    exec(compile(V5_ARCHIVE_HOOK, "v5hook-bare", "exec"), bare)
    bare_finish = bare["pytest_sessionfinish"]  # type: ignore[operator]
    bare_finish(None, 0)
    assert (archive / "count").read_text(encoding="utf-8") == "2"
    assert not (archive / "junit-hook-2.xml").exists()


def test_v5_archive_hook_results_grade_through_v5_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "phases"
    namespace = _v5_hook_namespace(archive, monkeypatch)
    logreport = namespace["pytest_runtest_logreport"]  # type: ignore[operator]
    sessionfinish = namespace["pytest_sessionfinish"]  # type: ignore[operator]

    class _Report:
        def __init__(self, nodeid: str, when: str, outcome: str) -> None:
            self.nodeid = nodeid
            self.when = when
            self.outcome = outcome
            self.skipped = outcome == "skipped"

    logreport(_Report("tests/ops/test_ops.py::test_base_one", "call", "passed"))
    sessionfinish(None, 0)
    phase = (archive / "junit-hook-0.xml").read_bytes()
    # The 000200 shape grades 1 only on the union of the hook archive (base
    # phase) with the plugin report (new phase).
    assert evaluate_junit_v5([_V5_NEW_JUNIT], 0, _V5_MULTI_NAMED) == 0
    assert evaluate_junit_v5([_V5_NEW_JUNIT, phase], 0, _V5_MULTI_NAMED) == 1


def _v5_grader_block() -> str:
    """Extract the embedded @5 grading script (last PYEOF heredoc)."""
    import re as _re

    from evallab.separate_verifier import render_wrapper_test_sh_v5 as _render

    blocks = _re.findall(r"<<'PYEOF'.*?\n(.*?)\nPYEOF", _render("/testbed"), _re.S)
    assert len(blocks) == 3
    return blocks[2]


def _run_embedded_v5_grader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_cmd: str,
    output: str,
    rc: int,
    junit: bytes | None = None,
    phase_xmls: list[bytes] | None = None,
    patch_extra: str = "",
) -> str:
    """Run the shipped @5 grading script against synthetic files; return reward."""
    junit_path = tmp_path / "junit.xml"
    if junit is None:
        if junit_path.exists():
            junit_path.unlink()
    else:
        junit_path.write_bytes(junit)
    phases_dir = tmp_path / "junit-phases"
    if phase_xmls:
        phases_dir.mkdir(exist_ok=True)
        for index, raw in enumerate(phase_xmls):
            (phases_dir / f"junit-hook-{index}.xml").write_bytes(raw)
    patch_path = tmp_path / "test.patch"
    patch_path.write_text(
        f"diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n+{runner_cmd}\n{patch_extra}",
        encoding="utf-8",
    )
    output_path = tmp_path / "test_output.log"
    output_path.write_text(output, encoding="utf-8")
    cmd_path = tmp_path / "test_command.sh"
    cmd_path.write_text(runner_cmd, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "grader",
            str(junit_path),
            str(rc),
            str(patch_path),
            str(output_path),
            str(cmd_path),
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit):
        exec(compile(_v5_grader_block(), "v5grader", "exec"), {"__name__": "v5grader"})
    return capsys.readouterr().out.strip()


def test_embedded_v5_grader_matches_v5_mirror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest_out = "=== test session starts ===\ncollected 3 items\n"
    cmd = "python -m pytest tests/ -v"
    # Skip-tolerant pass grades 1 in both the mirror and the shipped grader.
    assert evaluate_junit_v5([_V5_SKIP_JUNIT], 0, _V5_SKIP_NAMED) == 1
    assert (
        _run_embedded_v5_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=pytest_out,
            rc=0,
            junit=_V5_SKIP_JUNIT,
        )
        == "1"
    )
    # Failures still grade 0 in both.
    failed = _V5_SKIP_JUNIT.replace(b"<skipped", b"<failure")
    assert evaluate_junit_v5([failed], 0, _V5_SKIP_NAMED) == 0
    assert (
        _run_embedded_v5_grader(
            tmp_path, capsys, monkeypatch, runner_cmd=cmd, output=pytest_out, rc=0, junit=failed
        )
        == "0"
    )
    # Multi-phase union: the main report alone misses the base ID, the union
    # of the main report with the archived base phase grades 1.
    multi_out = "=== test session starts ===\ncollected 2 items\n"
    multi_named = (
        "+    python -m pytest ops/test_ops.py::test_base_one ops/test_ops.py::test_new_only\n"
    )
    assert (
        _run_embedded_v5_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=multi_out,
            rc=0,
            junit=_V5_NEW_JUNIT,
            patch_extra=multi_named,
        )
        == "0"
    )
    assert (
        _run_embedded_v5_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=multi_out,
            rc=0,
            junit=_V5_NEW_JUNIT,
            phase_xmls=[_V5_BASE_JUNIT],
            patch_extra=multi_named,
        )
        == "1"
    )


def test_v5_template_pins() -> None:
    wrapper = render_wrapper_test_sh_v5("/testbed")
    assert "@@" not in wrapper
    assert TRANSFORM_ID_V5 not in wrapper  # transform ids live in lineage, not the grader
    assert "MIMO_VERIFIER_ARCHIVE_DIR" in wrapper
    assert "junit-hook-" in wrapper
    assert "hook_installed" in wrapper
    assert JUNIT_MISSING_REASON in wrapper
    assert "/testbed" in wrapper
    with pytest.raises(VariantInvalid):
        render_wrapper_test_sh_v5("relative/path")


def test_v5_grader_differs_from_v4_only_in_pinned_blocks() -> None:
    from evallab import separate_verifier as sv

    def _neutralize_c(text: str) -> str:
        return (
            text.replace(sv._V5_BAD_LINE, "BAD")
            .replace(sv._V4_BAD_LINE, "BAD")
            .replace(sv._V5_PYTEST_LOAD_BLOCK, "LOAD")
            .replace(sv._V4_PYTEST_LOAD_BLOCK, "LOAD")
            .replace(sv._V5_PYTEST_LOG_LINE, "LOG")
            .replace(sv._V4_PYTEST_LOG_LINE, "LOG")
            .replace(sv._V5_SUREFIRE_LOG_LINE, "SLOG")
            .replace(sv._V4_SUREFIRE_LOG_LINE, "SLOG")
            .replace(sv._V5_RUN_HEAD, "HEAD")
            .replace(sv._V4_RUN_HEAD, "HEAD")
            .replace(sv._V5_RUN_LINE, "RUN")
            .replace(sv._V4_RUN_LINE, "RUN")
        )

    assert _neutralize_c(sv._V5_WRAPPER_C) == _neutralize_c(sv._V4_WRAPPER_C)
    assert sv._V5_WRAPPER_B != sv._V4_WRAPPER_B
    assert sv._V5_WRAPPER_B.replace(sv._V5_HOOK_INSTALL_BLOCK, "HOOK") == (
        sv._V4_WRAPPER_B.replace(sv._V4_HOOK_INSTALL_BLOCK, "HOOK")
    )
    assert sv._V5_WRAPPER_A == sv._V4_WRAPPER_A


def _v5_target_script() -> str:
    """Extract the embedded conftest-target computer from the @5 wrapper."""
    import re as _re

    from evallab.separate_verifier import render_wrapper_test_sh_v5 as _render

    blocks = _re.findall(
        r"python3 - \"\$RESOLVED\" \"\$CWD\" > \"\$V/conftest-targets.txt\".*?\n(.*?)\nPYEOF",
        _render("/testbed"),
        _re.S,
    )
    assert len(blocks) == 1
    return blocks[0]


def test_v5_hook_targets_cover_named_test_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Existence-grounded: only test files present under the workdir yield
    # install targets, always including the root.
    cwd = tmp_path / "testbed"
    (cwd / "tests" / "ops" / "qubit").mkdir(parents=True)
    (cwd / "tests" / "ops" / "qubit" / "test_non_parametric_ops.py").write_text(
        "", encoding="utf-8"
    )
    (cwd / "tests" / "other").mkdir(parents=True)
    (cwd / "tests" / "other" / "test_other.py").write_text("", encoding="utf-8")
    resolved = (
        "bash /testbed/mimo_test_command.sh base && bash /testbed/mimo_test_command.sh new\n"
        "python3 -m pytest tests/ops/qubit/test_non_parametric_ops.py::TestOperations "
        "tests/ops/qubit/test_non_parametric_ops.py::TestDecompositions -x -v\n"
        "python3 -m pytest missing_dir/test_ghost.py -q\n"
    )
    monkeypatch.setattr("sys.argv", ["targets", resolved, str(cwd)])
    exec(compile(_v5_target_script(), "v5targets", "exec"), {"__name__": "v5targets"})
    assert capsys.readouterr().out.split() == [".", "tests/ops/qubit"]


def test_v5_variant_bundles_setup_and_records_runner(parent_dir_v2: Path) -> None:
    changes, inputs = build_changes_v5(parent_dir_v2, marker=MARKER)
    assert set(changes) == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    assert "tests/test-orig.sh" not in changes
    wrapper = changes["tests/test.sh"].decode("utf-8")  # type: ignore[union-attr]
    assert "@@" not in wrapper
    assert "RUNNER=$RUNNER" in wrapper
    assert "MIMO_VERIFIER_ARCHIVE_DIR" in wrapper
    assert inputs["runner"] in ("custom", "pytest", "unittest")
    assert len(inputs["setup_sha256"]) == 64


def test_v5_solution_injected_only_when_parent_has_none(parent_dir_v2: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, _ = build_changes_v5(parent_dir_v2, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    (parent_dir_v2 / "solution").mkdir()
    (parent_dir_v2 / "solution" / "solve.sh").write_bytes(b"#!/bin/bash\n")
    with pytest.raises(VariantInvalid):
        build_changes_v5(parent_dir_v2, marker=MARKER, solution_sh=solve)


def test_v5_derive_records_transform_id(parent_dir_v2: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier_v5(
        parent_dir_v2,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir_v2)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID_V5
    assert {change.path for change in record.files} == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }


def test_v5_refuses_without_a_setup_chain(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes_v5(parent_dir, marker=MARKER)
    with pytest.raises(VariantInvalid):
        build_changes_v5(parent_dir, marker="  ")


# --------------------------------------------------------------------------- #
# separate-verifier@6: pristine-baseline fail-to-pass grading
# --------------------------------------------------------------------------- #

_V6_BASE_JUNIT = (
    b'<testsuite tests="4">'
    b'<testcase classname="pkg.test_a" name="test_fail_one"><failure message="x"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_fail_two"><error message="x"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_pass_one"/>'
    b'<testcase classname="pkg.test_a" name="test_skip_one"><skipped message="x"/></testcase>'
    b"</testsuite>"
)

_V6_ORACLE_JUNIT = (
    b'<testsuite tests="4">'
    b'<testcase classname="pkg.test_a" name="test_fail_one"/>'
    b'<testcase classname="pkg.test_a" name="test_fail_two"/>'
    b'<testcase classname="pkg.test_a" name="test_pass_one"/>'
    b'<testcase classname="pkg.test_a" name="test_skip_one"><skipped message="x"/></testcase>'
    b"</testsuite>"
)

_V6_SKIP_JUNIT = (
    b'<testsuite tests="4">'
    b'<testcase classname="pkg.test_a" name="test_fail_one"><skipped message="x"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_fail_two"><skipped message="x"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_pass_one"><skipped message="x"/></testcase>'
    b'<testcase classname="pkg.test_a" name="test_skip_one"><skipped message="x"/></testcase>'
    b"</testsuite>"
)

_V6_NAMED = {
    "pkg/test_a.py::test_fail_one",
    "pkg/test_a.py::test_fail_two",
    "pkg/test_a.py::test_pass_one",
    "pkg/test_a.py::test_skip_one",
}


def test_v6_status_map_keeps_the_worst_outcome() -> None:
    import xml.etree.ElementTree as ET

    assert junit_case_status(ET.fromstring(
        b'<testcase classname="X" name="a"><failure/></testcase>'
    )) == "bad"
    assert junit_case_status(ET.fromstring(
        b'<testcase classname="X" name="a"><skipped/></testcase>'
    )) == "skipped"
    assert junit_case_status(ET.fromstring(
        b'<testcase classname="X" name="a"/>'
    )) == "passed"
    key = ("pkg.test_a", "test_fail_one", "")
    assert junit_status_map([_V6_BASE_JUNIT]) == {
        ("pkg.test_a", "test_fail_one", ""): "bad",
        ("pkg.test_a", "test_fail_two", ""): "bad",
        ("pkg.test_a", "test_pass_one", ""): "passed",
        ("pkg.test_a", "test_skip_one", ""): "skipped",
    }
    assert junit_status_map([_V6_BASE_JUNIT])[key] == "bad"
    assert junit_status_map([]) == {}
    assert junit_status_map([None, b"not xml <"]) == {}


def test_v6_grading_requires_fail_to_pass() -> None:
    # Oracle: pristine failures now pass, passes still pass, pristine skips
    # still skipped.
    assert evaluate_junit_v6([_V6_ORACLE_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 1
    # Skip attack: pristine failures skipped instead of passing.
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0
    # Nop: pristine failures still failing.
    assert evaluate_junit_v6([_V6_BASE_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0
    # Pass-to-pass broken: a pristine pass now fails.
    broken = _V6_ORACLE_JUNIT.replace(
        b'name="test_pass_one"/>', b'name="test_pass_one"><failure message="x"/></testcase>'
    )
    assert evaluate_junit_v6([broken], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0
    # New skip: a pristine pass skipped instead.
    reskipped = _V6_ORACLE_JUNIT.replace(
        b'name="test_pass_one"/>', b'name="test_pass_one"><skipped message="x"/></testcase>'
    )
    assert evaluate_junit_v6([reskipped], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0
    # Pristine skip now passing is fine.
    unskipped = _V6_ORACLE_JUNIT.replace(
        b'name="test_skip_one"><skipped message="x"/></testcase>',
        b'name="test_skip_one"/>',
    )
    assert evaluate_junit_v6([unskipped], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 1


def test_v6_grading_without_baseline_is_v5() -> None:
    # No baseline artifacts: the skip attack grades like @5 (documents the
    # fallback; the shipped grader always records a baseline for pytest).
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, None) == 1
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, []) == 1
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, [None]) == 1
    assert evaluate_junit_v6([_V6_ORACLE_JUNIT], 0, _V6_NAMED, None) == 1
    assert evaluate_junit_v6([_V6_BASE_JUNIT], 0, _V6_NAMED, None) == 0
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 1, _V6_NAMED, None) == 0


def test_v6_unittest_counts() -> None:
    base_fail = "Ran 8 tests in 0.01s\n\nFAILED (failures=3)\n"
    oracle = "Ran 8 tests in 0.01s\n\nOK\n"
    attack = "Ran 8 tests in 0.01s\n\nOK (skipped=8)\n"
    assert evaluate_unittest_v6(oracle, 0, base_fail) == 1
    assert evaluate_unittest_v6(attack, 0, base_fail) == 0
    assert evaluate_unittest_v6(base_fail, 1, base_fail) == 0
    # Skips tolerated only up to the pristine count; runs must not shrink.
    base_skip = "Ran 8 tests in 0.01s\n\nOK (skipped=2)\n"
    assert evaluate_unittest_v6("Ran 8 tests in 0.01s\n\nOK (skipped=2)\n", 0, base_skip) == 1
    assert evaluate_unittest_v6("Ran 8 tests in 0.01s\n\nOK (skipped=3)\n", 0, base_skip) == 0
    assert evaluate_unittest_v6("Ran 7 tests in 0.01s\n\nOK (skipped=2)\n", 0, base_skip) == 0
    # No (or unparsable) baseline: exactly @5.
    assert evaluate_unittest_v6(attack, 0, None) == 1
    assert evaluate_unittest_v6(attack, 0, "traceback noise") == 1
    assert evaluate_unittest_v6(oracle, 0, None) == 1
    assert evaluate_unittest_v6(attack, 1, base_fail) == 0


def test_v6_go_empty_run_grades_zero() -> None:
    empty = "ok  \texample.com/mod/pkg\t0.026s [no tests to run]\n"
    assert evaluate_go_output_v6(empty, 0) == 0
    assert evaluate_go_output(empty, 0) == 1  # @5 still grades the shape 1
    passing = "--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/mod/pkg\t0.1s\n"
    assert evaluate_go_output_v6(passing, 0) == 1
    assert evaluate_go_output_v6(passing, 1) == 0
    assert evaluate_go_output_v6("FAIL\n", 1) == 0


def _v6_grader_block() -> str:
    """Extract the embedded @6 grading script (last PYEOF heredoc)."""
    import re as _re

    from evallab.separate_verifier import render_wrapper_test_sh_v6 as _render

    blocks = _re.findall(r"<<'PYEOF'.*?\n(.*?)\nPYEOF", _render("/testbed"), _re.S)
    assert len(blocks) == 3
    return blocks[2]


def _run_embedded_v6_grader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_cmd: str,
    output: str,
    rc: int,
    junit: bytes | None = None,
    phase_xmls: list[bytes] | None = None,
    baseline_junit: bytes | None = None,
    baseline_phases: list[bytes] | None = None,
    baseline_output: str | None = None,
    patch_extra: str = "",
) -> str:
    """Run the shipped @6 grading script against synthetic files; return reward."""
    junit_path = tmp_path / "junit.xml"
    if junit is None:
        if junit_path.exists():
            junit_path.unlink()
    else:
        junit_path.write_bytes(junit)
    phases_dir = tmp_path / "junit-phases"
    if phase_xmls:
        phases_dir.mkdir(exist_ok=True)
        for index, raw in enumerate(phase_xmls):
            (phases_dir / f"junit-hook-{index}.xml").write_bytes(raw)
    baseline_dir = tmp_path / "baseline"
    if baseline_junit is not None:
        baseline_dir.mkdir(exist_ok=True)
        (baseline_dir / "junit.xml").write_bytes(baseline_junit)
    if baseline_phases:
        (baseline_dir / "junit-phases").mkdir(exist_ok=True)
        for index, raw in enumerate(baseline_phases):
            (baseline_dir / "junit-phases" / f"junit-hook-{index}.xml").write_bytes(raw)
    if baseline_output is not None:
        baseline_dir.mkdir(exist_ok=True)
        (baseline_dir / "output.log").write_text(baseline_output, encoding="utf-8")
    if baseline_junit is None and not baseline_phases and baseline_output is None:
        # Absent baseline must keep @5 rules: drop artifacts staged by an
        # earlier case sharing tmp_path.
        import shutil

        shutil.rmtree(baseline_dir, ignore_errors=True)
    patch_path = tmp_path / "test.patch"
    patch_path.write_text(
        f"diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n+{runner_cmd}\n{patch_extra}",
        encoding="utf-8",
    )
    output_path = tmp_path / "test_output.log"
    output_path.write_text(output, encoding="utf-8")
    cmd_path = tmp_path / "test_command.sh"
    cmd_path.write_text(runner_cmd, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "grader",
            str(junit_path),
            str(rc),
            str(patch_path),
            str(output_path),
            str(cmd_path),
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit):
        exec(compile(_v6_grader_block(), "v6grader", "exec"), {"__name__": "v6grader"})
    return capsys.readouterr().out.strip()


def _v6_pytest_case(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    agent: bytes,
    baseline: bytes | None,
) -> str:
    cmd = "python -m pytest pkg/test_a.py -v"
    out = "=== test session starts ===\ncollected 4 items\n"
    named = "".join(f"+    {node}\n" for node in sorted(_V6_NAMED))
    return _run_embedded_v6_grader(
        tmp_path,
        capsys,
        monkeypatch,
        runner_cmd=cmd,
        output=out,
        rc=0,
        junit=agent,
        baseline_junit=baseline,
        patch_extra=named,
    )


def test_embedded_v6_pytest_fail_to_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _v6_pytest_case(tmp_path, capsys, monkeypatch, _V6_ORACLE_JUNIT, _V6_BASE_JUNIT) == "1"
    assert _v6_pytest_case(tmp_path, capsys, monkeypatch, _V6_SKIP_JUNIT, _V6_BASE_JUNIT) == "0"
    assert _v6_pytest_case(tmp_path, capsys, monkeypatch, _V6_BASE_JUNIT, _V6_BASE_JUNIT) == "0"
    # Without baseline artifacts the embedded grader keeps @5 rules.
    assert _v6_pytest_case(tmp_path, capsys, monkeypatch, _V6_SKIP_JUNIT, None) == "1"
    # Mirror and embedded agree on every shape above.
    assert evaluate_junit_v6([_V6_ORACLE_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 1
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0
    assert evaluate_junit_v6([_V6_SKIP_JUNIT], 0, _V6_NAMED, None) == 1


def test_embedded_v6_grades_from_archives_without_plugin_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 000666 ADDOPTS-cleared shape: no plugin junit, per-phase archives
    # only. @5 takes the missing-report path (grade 0); @6 grades the union.
    cmd = "unset PYTEST_ADDOPTS; python -m pytest pkg/test_a.py -v"
    out = "=== test session starts ===\ncollected 4 items\n"
    named = "".join(f"+    {node}\n" for node in sorted(_V6_NAMED))
    assert (
        _run_embedded_v6_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=out,
            rc=0,
            junit=None,
            phase_xmls=[_V6_ORACLE_JUNIT],
            baseline_junit=_V6_BASE_JUNIT,
            patch_extra=named,
        )
        == "1"
    )
    assert (
        _run_embedded_v6_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=out,
            rc=0,
            junit=None,
            phase_xmls=[_V6_SKIP_JUNIT],
            baseline_junit=_V6_BASE_JUNIT,
            patch_extra=named,
        )
        == "0"
    )
    # Mirror agrees (missing plugin report is simply absent from the union).
    assert evaluate_junit_v6([None, _V6_ORACLE_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 1
    assert evaluate_junit_v6([None, _V6_SKIP_JUNIT], 0, _V6_NAMED, [_V6_BASE_JUNIT]) == 0


def test_embedded_v6_unittest_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = "python -m unittest tests.test_tools -v"
    oracle_out = (
        "test_one (tests.test_tools.TestX) ... ok\n"
        "Ran 8 tests in 0.01s\n\nOK\n"
    )
    attack_out = (
        "test_one (tests.test_tools.TestX) ... skipped 'x'\n"
        "Ran 8 tests in 0.01s\n\nOK (skipped=8)\n"
    )
    base_out = "Ran 8 tests in 0.01s\n\nFAILED (failures=3)\n"
    assert (
        _run_embedded_v6_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=oracle_out,
            rc=0,
            baseline_output=base_out,
        )
        == "1"
    )
    assert (
        _run_embedded_v6_grader(
            tmp_path,
            capsys,
            monkeypatch,
            runner_cmd=cmd,
            output=attack_out,
            rc=0,
            baseline_output=base_out,
        )
        == "0"
    )
    # Mirror agrees.
    assert evaluate_unittest_v6(oracle_out, 0, base_out) == 1
    assert evaluate_unittest_v6(attack_out, 0, base_out) == 0


def test_embedded_v6_go_empty_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = "go test ./..."
    empty = "ok  \texample.com/mod/pkg\t0.026s [no tests to run]\n"
    passing = "--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/mod/pkg\t0.1s\n"
    assert (
        _run_embedded_v6_grader(
            tmp_path, capsys, monkeypatch, runner_cmd=cmd, output=empty, rc=0
        )
        == "0"
    )
    assert (
        _run_embedded_v6_grader(
            tmp_path, capsys, monkeypatch, runner_cmd=cmd, output=passing, rc=0
        )
        == "1"
    )
    assert evaluate_go_output_v6(empty, 0) == 0
    assert evaluate_go_output_v6(passing, 0) == 1


def test_v6_template_pins() -> None:
    wrapper = render_wrapper_test_sh_v6("/testbed")
    assert "@@" not in wrapper
    assert TRANSFORM_ID_V6 not in wrapper  # transform ids live in lineage, not the grader
    assert "RUNNER_B=custom" in wrapper
    assert "baseline/junit.xml" in wrapper
    assert "fail-to-pass" in wrapper
    assert "no tests to run" in wrapper
    assert "timing.log" in wrapper
    assert "PYEOF_BASELINE" in wrapper
    assert "mimo-baseline-noexitfirst/1" in wrapper
    import re as _re

    assert len(_re.findall(r"<<'PYEOF'.*?\n(.*?)\nPYEOF", wrapper, _re.S)) == 3
    with pytest.raises(VariantInvalid):
        render_wrapper_test_sh_v6("relative/path")


def test_v6_grader_differs_from_v5_only_in_pinned_blocks() -> None:
    from evallab import separate_verifier as sv

    def _neutralize_c(text: str) -> str:
        return (
            text.replace(sv._V6_PYTEST_FTP_BLOCK, "FTP")
            .replace(sv._V5_PYTEST_PRINT, "FTP")
            .replace(sv._V6_UNITTEST_OK, "UOK")
            .replace(sv._V5_UNITTEST_OK, "UOK")
            .replace(sv._V6_GO_OK, "GOK")
            .replace(sv._V5_GO_OK, "GOK")
            .replace(sv._V6_RUN_TIMED, "RUN")
            .replace(sv._V5_RUN_LINE_TIMED, "RUN")
            .replace(sv._V6_PLUGIN_READ, "LOAD")
            .replace(sv._V5_PLUGIN_READ, "LOAD")
        )

    assert _neutralize_c(sv._V6_WRAPPER_C) == _neutralize_c(sv._V5_WRAPPER_C)
    assert sv._V6_WRAPPER_B == sv._V5_WRAPPER_B
    assert sv._V6_WRAPPER_A.count(sv._V6_BASELINE_BLOCK) == 1
    assert sv._V6_WRAPPER_A.replace(sv._V6_BASELINE_BLOCK, "") == sv._V5_WRAPPER_A


def test_v6_variant_bundles_setup_and_records_runner(parent_dir_v2: Path) -> None:
    changes, inputs = build_changes_v6(parent_dir_v2, marker=MARKER)
    assert set(changes) == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }
    assert "tests/test-orig.sh" not in changes
    wrapper = changes["tests/test.sh"].decode("utf-8")  # type: ignore[union-attr]
    assert "@@" not in wrapper
    assert "RUNNER=$RUNNER" in wrapper
    assert "MIMO_VERIFIER_ARCHIVE_DIR" in wrapper
    assert "fail-to-pass" in wrapper
    assert inputs["runner"] in ("custom", "pytest", "unittest")
    assert len(inputs["setup_sha256"]) == 64


def test_v6_solution_injected_only_when_parent_has_none(parent_dir_v2: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, _ = build_changes_v6(parent_dir_v2, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    (parent_dir_v2 / "solution").mkdir()
    (parent_dir_v2 / "solution" / "solve.sh").write_bytes(b"#!/bin/bash\n")
    with pytest.raises(VariantInvalid):
        build_changes_v6(parent_dir_v2, marker=MARKER, solution_sh=solve)


def test_v6_derive_records_transform_id(parent_dir_v2: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier_v6(
        parent_dir_v2,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir_v2)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID_V6
    assert {change.path for change in record.files} == {
        "task.toml",
        "tests/test.sh",
        "tests/Dockerfile",
        f"{V2_SETUP_SUBDIR}/setup.sh",
        f"{V2_SETUP_SUBDIR}/files/blocklist",
    }


def test_v6_refuses_without_a_setup_chain(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes_v6(parent_dir, marker=MARKER)
    with pytest.raises(VariantInvalid):
        build_changes_v6(parent_dir, marker="  ")
