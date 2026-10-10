"""Behavioural tests for the mimo-clean-v4 clean-set builder.

Covers the pure helpers (marker, solution, selection, language, reference
index, manifest, acceptance) plus the real derive chain on synthetic run
and snapshot packages with hermetic record/variant roots. No Docker, no
Harbor runs.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

from evallab import mimo_clean
from evallab.mimo_clean import (
    AGENT_NETWORK_NONE_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    NONPYTHON_CHAIN,
    PURGE_ID,
    PYTHON_CHAIN,
    SEPARATE_V6_ID,
    STATUS_BUILT,
    STATUS_SKIPPED,
    STRIP_ID,
    VERIFY_UNVERIFIED,
    ChainBuilder,
    ChainResult,
    OracleInfo,
    acceptance_pass,
    build_solution_sh,
    chain_for_language,
    derive_marker,
    load_manifest,
    load_reference_index,
    oracle_info_from_index,
    resolve_language,
    select_usable,
    snapshot_category,
    snapshot_pool,
    summarize_trials,
    write_manifest,
)
from evallab.strip_future_history import pack_setup
from evallab.task_variants import VariantInvalid, task_directory_digest


def test_active_cache_generation_is_v4() -> None:
    from evallab import purge_build_caches

    assert mimo_clean.CACHE_ACTIVE_ID == purge_build_caches.TRANSFORM_ID_V4


def test_active_mtime_generation_tracks_module() -> None:
    from evallab import mtime_normalize

    expected = getattr(mtime_normalize, "TRANSFORM_ID_V2", mtime_normalize.TRANSFORM_ID)
    assert expected == mimo_clean.MTIME_ACTIVE_ID


TASK_ID = "format-code-task-000000"

SETUP_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
:[ -f "$M/ready" ] && exit 0
chmod 700 "$M"
fail() { echo "setup: $*" >&2; exit 1; }
write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }
CWD=/testbed
git config --global --add safe.directory "$CWD"
cd "$CWD" || fail "no working directory $CWD"
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  git init -q && git add -A && git -c user.name=mimo -c user.email=mimo@localhost commit -q -m baseline --allow-empty
fi
BASE=$(git rev-parse HEAD 2>/dev/null)
:[ ${#BASE} -eq 40 ] || fail "could not resolve the base commit in $CWD"
echo "$BASE" > "$M/base"
# Images are built with history truncated at the base. If one is not, the fix could be read out of git log,
# so .git is hidden while the agent works and put back for grading.
LATER=$(git rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)
git clean -fdx >/dev/null 2>&1
if [ "$LATER" -gt 0 ]; then
  mv "$CWD/.git" "$M/git-hidden"
  echo "history not truncated at ${BASE:0:12}: .git hidden while the agent works"
fi
write_blocklist
touch "$M/ready"
echo "setup done"
"""

TOML_TEMPLATE = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/__TASK__"
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

[environment.healthcheck]
command = "bash -c 'test -f /var/lib/mimo/ready || { mkdir -p /var/lib/mimo && echo __BLOB__ | base64 -d | tar -xzf - -C /var/lib/mimo && bash /var/lib/mimo/setup.sh; }'"
timeout_sec = 1200.0
retries = 0
interval_sec = 5.0
"""

PATCH_TEXT = """diff --git a/pkg/test_hidden.py b/pkg/test_hidden.py
index 1111111..2222222 100644
--- a/pkg/test_hidden.py
+++ b/pkg/test_hidden.py
@@ -1,2 +1,4 @@
 import pkg.core
+
+def test_hidden_example():
+    assert pkg.core.answer() == 42
diff --git a/mimo_test_command.sh b/mimo_test_command.sh
--- a/mimo_test_command.sh
+++ b/mimo_test_command.sh
@@ -1 +1 @@
-python -m pytest pkg -q
+python -m pytest -v pkg/test_hidden.py::test_hidden_example
"""

ORACLE_DIFF = """diff --git a/pkg/core.py b/pkg/core.py
index 3333333..4444444 100644
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1 +1 @@
-def answer(): return 0
+def answer(): return 42
"""


def _write_run_package(root: Path, task_id: str) -> Path:
    package = (
        root
        / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6"
        / "tasks"
        / task_id
    )
    setup = package / "environment" / "setup"
    (setup / "files").mkdir(parents=True)
    (setup / "setup.sh").write_text(SETUP_TEMPLATE, encoding="utf-8")
    (setup / "files" / "blocklist").write_text("0.0.0.0 example.com\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text(
        "FROM docker.io/example/repo@sha256:0000\n", encoding="utf-8"
    )
    (package / "tests").mkdir(parents=True)
    (package / "tests" / "test.sh").write_text("#!/bin/bash\necho grading\n", encoding="utf-8")
    (package / "tests" / "test.patch").write_text(PATCH_TEXT, encoding="utf-8")
    (package / "tests" / "test_command.sh").write_text("true\n", encoding="utf-8")
    (package / "instruction.md").write_text("fix it", encoding="utf-8")
    blob = pack_setup(setup)
    (package / "task.toml").write_text(
        TOML_TEMPLATE.replace("__TASK__", task_id).replace("__BLOB__", blob),
        encoding="utf-8",
    )
    return package


def _row(package: Path, task_id: str, **overrides: str) -> dict[str, str]:
    row = {
        "task_id": task_id,
        "split": "train",
        "project": "synthetic",
        "image_mib": "10",
        "status": "usable",
        "reason": "synthetic",
        "run": "original",
        "run_digest": task_directory_digest(package),
        "run_transform": "",
        "run_variant_status": "",
        "census_label": "sound",
        "census_nop_job": "",
        "census_evidence": "",
        "leak_channel": "none_found",
        "evidence": "",
        "verdict": "keep",
        "verdict_evidence": "",
    }
    row.update(overrides)
    return row


@pytest.fixture()
def oracle() -> OracleInfo:
    return OracleInfo(
        label="oracle:pass+nop:fail",
        fix_commit="abc1234",
        patch_path="/tmp/solution-patch.stdout.log",
        patch_bytes=ORACLE_DIFF.encode("utf-8"),
    )


def test_marker_prefers_new_test_name() -> None:
    assert derive_marker(PATCH_TEXT, TASK_ID) == "test_hidden_example"


def test_marker_falls_back_to_patch_filename_then_task() -> None:
    assert derive_marker("diff --git a/x.py b/y.py\n+++ b/y.py\n", TASK_ID) == "y.py"
    assert derive_marker("", TASK_ID) == TASK_ID
    assert derive_marker("+def test_a-b c()", TASK_ID) == "test_a"


def test_solution_sh_roundtrip_is_deterministic(oracle: OracleInfo) -> None:
    first = build_solution_sh(
        task_id=TASK_ID,
        label=oracle.label,
        fix_commit=oracle.fix_commit,
        patch_bytes=oracle.patch_bytes,
    )
    second = build_solution_sh(
        task_id=TASK_ID,
        label=oracle.label,
        fix_commit=oracle.fix_commit,
        patch_bytes=oracle.patch_bytes,
    )
    assert first == second
    text = first.decode("utf-8")
    assert text.startswith("#!/bin/bash")
    assert TASK_ID in text and oracle.fix_commit in text
    encoded = next(line for line in text.splitlines() if line.startswith('echo "')).split('"')[1]
    assert base64.b64decode(encoded) == oracle.patch_bytes


def test_solution_sh_rejects_oversize_patch() -> None:
    with pytest.raises(VariantInvalid):
        build_solution_sh(
            task_id=TASK_ID,
            label="oracle:pass",
            fix_commit="abc",
            patch_bytes=b"x" * (2 * 1024 * 1024),
        )


def test_select_usable_keeps_keep_and_fix_only() -> None:
    rows = [
        {"task_id": "a", "verdict": "keep"},
        {"task_id": "b", "verdict": "fix"},
        {"task_id": "c", "verdict": "discard"},
        {"task_id": "d", "verdict": ""},
    ]
    assert [row["task_id"] for row in select_usable(rows)] == ["a", "b"]


def test_resolve_language_ledger_wins_over_category() -> None:
    assert resolve_language(ledger_row=True, category="go") == "python"
    assert resolve_language(ledger_row=False, category="go") == "go"
    assert resolve_language(ledger_row=False, category=None) is None


def test_chain_for_language_matrix() -> None:
    assert chain_for_language("python") == PYTHON_CHAIN
    assert chain_for_language("go") == NONPYTHON_CHAIN
    assert chain_for_language("unknown") == NONPYTHON_CHAIN
    assert chain_for_language(None) is None
    assert PURGE_ID in PYTHON_CHAIN
    assert PURGE_ID not in NONPYTHON_CHAIN
    assert NONPYTHON_CHAIN[-2:] == (SEPARATE_V6_ID, AGENT_NETWORK_NONE_ID)


def test_snapshot_category_reads_task_toml(tmp_path: Path) -> None:
    task_dir = tmp_path / "format-code-task-1"
    task_dir.mkdir()
    assert snapshot_category(task_dir) is None
    (task_dir / "task.toml").write_text('[metadata]\ncategory = "Go"\n', encoding="utf-8")
    assert snapshot_category(task_dir) == "go"
    (task_dir / "task.toml").write_text("not toml [[[\n", encoding="utf-8")
    assert snapshot_category(task_dir) is None


def test_snapshot_pool_skips_ledger_members(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    for task_id, category in (("a", "Go"), ("b", "Python"), ("c", None)):
        task_dir = snap / task_id
        task_dir.mkdir(parents=True)
        if category is not None:
            (task_dir / "task.toml").write_text(
                f'[metadata]\ncategory = "{category}"\n', encoding="utf-8"
            )
    pool = snapshot_pool(snap, {"b"})
    assert pool == {"a": "go", "c": None}
    assert snapshot_pool(tmp_path / "missing", set()) == {}


def test_manifest_write_load_roundtrip(tmp_path: Path) -> None:
    rows = [
        ChainResult(
            task_id="format-code-task-000002",
            status=STATUS_BUILT,
            chain=[STRIP_ID],
            final_digest="sha256:" + "0" * 64,
            package_path="derived/task-store/variants/x/y",
            reference_fix="none",
            reason="ok",
            run_digest="sha256:" + "1" * 64,
            oracle_label="",
        ).manifest_row(),
        ChainResult(
            task_id="format-code-task-000001", status=STATUS_SKIPPED, reason="discard"
        ).manifest_row(),
    ]
    out = write_manifest(rows, tmp_path / "manifest.csv")
    loaded = load_manifest(out)
    assert [row["task_id"] for row in loaded] == [
        "format-code-task-000001",
        "format-code-task-000002",
    ]
    assert set(loaded[0]) == set(mimo_clean.MANIFEST_COLUMNS)
    assert loaded[1]["chain"] == STRIP_ID


def test_acceptance_pass_matrix() -> None:
    assert acceptance_pass(oracle_rewards=[1], has_reference_fix=True, nop_rewards=[0], cracked=0)
    assert not acceptance_pass(
        oracle_rewards=[0], has_reference_fix=True, nop_rewards=[0], cracked=0
    )
    assert not acceptance_pass(
        oracle_rewards=[1], has_reference_fix=True, nop_rewards=[1], cracked=0
    )
    assert not acceptance_pass(
        oracle_rewards=[1], has_reference_fix=True, nop_rewards=[0], cracked=1
    )
    # No reference fix: the oracle cell is vacuously satisfied.
    assert acceptance_pass(oracle_rewards=[], has_reference_fix=False, nop_rewards=[0], cracked=0)
    # Missing nop evidence never passes.
    assert not acceptance_pass(
        oracle_rewards=[], has_reference_fix=False, nop_rewards=[], cracked=0
    )


def test_summarize_trials_reads_result_rewards(tmp_path: Path) -> None:
    import json as jsonlib

    job = tmp_path / "job"
    first = job / "trial-a"
    first.mkdir(parents=True)
    (first / "result.json").write_text(
        jsonlib.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    assert summarize_trials(job) == [1.0]
    assert summarize_trials(tmp_path / "missing") == []


def _builder(tmp_path: Path, **kwargs: Any) -> ChainBuilder:
    kwargs.setdefault("purge_skip", set())
    kwargs.setdefault("snapshot_root", tmp_path / "snap")
    return ChainBuilder(
        repo_root=tmp_path,
        primary=tmp_path,
        variants_root=tmp_path / "store",
        **kwargs,
    )


def _write_snapshot_package(root: Path, task_id: str, category: str) -> Path:
    package = root / "snap" / task_id
    setup = package / "environment" / "setup"
    (setup / "files").mkdir(parents=True)
    (setup / "setup.sh").write_text(SETUP_TEMPLATE, encoding="utf-8")
    (setup / "files" / "blocklist").write_text("0.0.0.0 example.com\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text(
        "FROM docker.io/example/repo@sha256:0000\n", encoding="utf-8"
    )
    (package / "tests").mkdir(parents=True)
    (package / "tests" / "test.sh").write_text("#!/bin/bash\necho grading\n", encoding="utf-8")
    (package / "tests" / "test.patch").write_text(PATCH_TEXT, encoding="utf-8")
    (package / "tests" / "test_command.sh").write_text("true\n", encoding="utf-8")
    (package / "instruction.md").write_text("fix it", encoding="utf-8")
    blob = pack_setup(setup)
    (package / "task.toml").write_text(
        TOML_TEMPLATE.replace("__TASK__", task_id).replace("__BLOB__", blob)
        + f'\n[metadata]\ncategory = "{category}"\n',
        encoding="utf-8",
    )
    return package


def _final(tmp_path: Path, result) -> Path:
    slug = f"mimo-v2.6-rl__{result.task_id}"
    return tmp_path / "store" / slug / result.final_digest.removeprefix("sha256:")[:12]


def test_build_chain_end_to_end(tmp_path: Path, oracle: OracleInfo) -> None:
    package = _write_run_package(tmp_path, TASK_ID)
    builder = _builder(tmp_path)
    result = builder.build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    assert result.status == STATUS_BUILT
    # Purge is out of scope for unconfirmed projects (HAR-194 stance).
    assert result.chain == [
        STRIP_ID,
        CACHE_ACTIVE_ID,
        MTIME_ACTIVE_ID,
        SEPARATE_V6_ID,
        AGENT_NETWORK_NONE_ID,
    ]
    assert "purge-installed-copies@1 skipped" in result.reason
    final = _final(tmp_path, result)
    assert (final / "solution" / "solve.sh").is_file()
    assert (final / "tests" / "test.sh").is_file()
    config = tomllib.loads((final / "task.toml").read_text())
    assert config["agent"]["network_mode"] == "no-network"
    assert config["environment"]["network_mode"] == "public"
    assert "network_mode" not in config["verifier"]
    assert config["verifier"]["environment_mode"] == "separate"
    assert result.reference_fix == oracle.patch_path
    assert result.oracle_label == oracle.label
    row = result.manifest_row()
    assert row["language"] == "python" and row["domain"] == "code"
    assert row["verify"] == VERIFY_UNVERIFIED


def test_build_snapshot_chain_end_to_end(tmp_path: Path) -> None:
    task_id = "format-code-task-000045"
    _write_snapshot_package(tmp_path, task_id, "JavaScript")
    result = _builder(tmp_path).build_task(task_id, None, oracle=None)
    assert result.status == STATUS_BUILT
    assert result.chain == [
        STRIP_ID,
        CACHE_ACTIVE_ID,
        MTIME_ACTIVE_ID,
        SEPARATE_V6_ID,
        AGENT_NETWORK_NONE_ID,
    ]
    assert result.language == "javascript"
    assert "purge-installed-copies@1 n/a to javascript" in result.reason
    assert result.reference_fix == "none"
    final = _final(tmp_path, result)
    assert (final / "tests" / "test.sh").is_file()
    assert not (final / "solution" / "solve.sh").exists()
    config = tomllib.loads((final / "task.toml").read_text())
    assert config["agent"]["network_mode"] == "no-network"
    assert config["environment"]["network_mode"] == "public"
    row = result.manifest_row()
    assert row["language"] == "javascript" and row["domain"] == "code"
    assert row["verify"] == VERIFY_UNVERIFIED


def test_build_chain_is_idempotent(tmp_path: Path, oracle: OracleInfo) -> None:
    package = _write_run_package(tmp_path, TASK_ID)
    builder = _builder(tmp_path)
    first = builder.build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    before = sorted((tmp_path / "library").rglob("*.json"))
    # A fresh builder sees only the written records, like a re-run would.
    second = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    after = sorted((tmp_path / "library").rglob("*.json"))
    assert first.final_digest == second.final_digest
    assert [path.name for path in before] == [path.name for path in after]


def test_rebuild_restores_a_missing_package(tmp_path: Path, oracle: OracleInfo) -> None:
    import shutil

    package = _write_run_package(tmp_path, TASK_ID)
    first = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    assert first.status == STATUS_BUILT
    store = tmp_path / "store" / f"mimo-v2.6-rl__{TASK_ID}"
    victim = next(path for path in store.iterdir() if path.is_dir())
    shutil.rmtree(victim)
    second = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    assert second.final_digest == first.final_digest
    assert victim.is_dir()


def test_build_without_oracle_has_no_solution(tmp_path: Path) -> None:
    package = _write_run_package(tmp_path, TASK_ID)
    result = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=None)
    assert result.status == STATUS_BUILT
    assert result.reference_fix == "none"
    assert not (_final(tmp_path, result) / "solution" / "solve.sh").exists()


def test_build_confirmed_purge_task_carries_purge(tmp_path: Path, oracle: OracleInfo) -> None:
    task_id = "format-code-task-001269"
    package = _write_run_package(tmp_path, task_id)
    result = _builder(tmp_path).build_task(task_id, _row(package, task_id), oracle=oracle)
    assert result.status == STATUS_BUILT
    assert result.chain == [
        STRIP_ID,
        PURGE_ID,
        CACHE_ACTIVE_ID,
        MTIME_ACTIVE_ID,
        SEPARATE_V6_ID,
        AGENT_NETWORK_NONE_ID,
    ]


def test_build_fail_closed_task_skips_purge_with_reason(tmp_path: Path, oracle: OracleInfo) -> None:
    task_id = "format-code-task-002552"
    package = _write_run_package(tmp_path, task_id)
    builder = _builder(tmp_path, purge_skip={task_id})
    result = builder.build_task(task_id, _row(package, task_id), oracle=oracle)
    assert result.status == STATUS_BUILT
    assert PURGE_ID not in result.chain
    assert "fail-closed" in result.reason


def test_build_unresolvable_snapshot_task_skipped(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    result = builder.build_task("format-code-task-999999", None, oracle=None)
    assert result.status == STATUS_SKIPPED
    assert "no clean chain" in result.reason
    assert result.language == ""


def test_build_missing_run_package_skipped(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    result = builder.build_task(
        "format-code-task-999999",
        {"task_id": "format-code-task-999999", "run": "original", "run_digest": "x"},
        oracle=None,
    )
    assert result.status == STATUS_SKIPPED
    assert "run package missing" in result.reason
    assert result.language == "python"
    bare = builder.build_task("format-code-task-9", {"task_id": "x"}, oracle=None)
    assert bare.status == STATUS_SKIPPED


def test_reference_index_oracle_resolution(tmp_path: Path) -> None:
    patch = tmp_path / "fix.patch"
    patch.write_bytes(b"diff --git a/x b/x\n")
    sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    index_file = tmp_path / "index.csv"
    with index_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["task_id", "label", "fix_commit", "patch_path", "patch_sha256", "source"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "task_id": "t-pass",
                "label": "oracle:pass+nop:fail",
                "fix_commit": "abc",
                "patch_path": str(patch),
                "patch_sha256": sha,
                "source": "sweep-2026-10-09",
            }
        )
        writer.writerow(
            {
                "task_id": "t-fail",
                "label": "oracle:fail",
                "fix_commit": "def",
                "patch_path": "",
                "patch_sha256": "",
                "source": "sweep-2026-10-09",
            }
        )
        writer.writerow(
            {
                "task_id": "t-sha",
                "label": "oracle:pass+nop:fail",
                "fix_commit": "ghi",
                "patch_path": str(patch),
                "patch_sha256": "0" * 64,
                "source": "sweep-2026-10-09",
            }
        )
    index = load_reference_index(index_file)
    assert set(index) == {"t-pass", "t-fail", "t-sha"}
    oracle = oracle_info_from_index("t-pass", index, repo_root=tmp_path)
    assert oracle is not None and oracle.patch_bytes == b"diff --git a/x b/x\n"
    assert oracle_info_from_index("t-fail", index, repo_root=tmp_path) is None
    assert oracle_info_from_index("t-sha", index, repo_root=tmp_path) is None
    assert oracle_info_from_index("t-missing", index, repo_root=tmp_path) is None


def test_rebuild_past_orphan_packages(tmp_path: Path, oracle: OracleInfo) -> None:
    import shutil

    package = _write_run_package(tmp_path, TASK_ID)
    first = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    assert first.status == STATUS_BUILT
    # Records gone, packages kept: the orphan path must re-derive the same bytes.
    shutil.rmtree(tmp_path / "library")
    second = _builder(tmp_path).build_task(TASK_ID, _row(package, TASK_ID), oracle=oracle)
    assert second.status == STATUS_BUILT
    assert second.final_digest == first.final_digest
    assert second.chain == first.chain
    leftovers = [
        path
        for path in (tmp_path / "store" / f"mimo-v2.6-rl__{TASK_ID}").iterdir()
        if path.suffix == ".orphan"
    ]
    assert leftovers == []


def test_next_free_name_skips_taken_dirs(tmp_path: Path) -> None:
    from evallab.mimo_clean import _next_free_name

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    assert _next_free_name(jobs, "base", task_id="t") == ("base", True)
    (jobs / "base").mkdir()
    assert _next_free_name(jobs, "base", task_id="t") == ("base-attempt2", False)
    (jobs / "base-attempt2").mkdir()
    assert _next_free_name(jobs, "base", task_id="t") == ("base-attempt3", False)


def test_parser_registers_build_and_verify() -> None:
    from evallab.cli import parser

    parsed = parser().parse_args(["mimo-clean", "build", "--tasks", "a,b"])
    assert parsed.mimo_clean_cmd == "build"
    assert parsed.tasks == "a,b"
    assert parsed.workers == 8
    parsed = parser().parse_args(["mimo-clean", "verify-local", "--tasks", "a"])
    assert parsed.mimo_clean_cmd == "verify-local"
    assert parsed.timeout_seconds == 1800


def test_run_cli_build_writes_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from evallab.cli import run_cli

    package = _write_run_package(tmp_path, TASK_ID)
    ledger = tmp_path / "ledger.csv"
    with ledger.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_row(package, TASK_ID)))
        writer.writeheader()
        writer.writerow(_row(package, TASK_ID))
    (tmp_path / "sweep.csv").write_text("task_id,label,fix_commit\n", encoding="utf-8")
    assert (
        run_cli(
            [
                "mimo-clean",
                "build",
                "--tasks",
                TASK_ID,
                "--ledger",
                "ledger.csv",
                "--sweep",
                "sweep.csv",
                "--manifest",
                "out/manifest.csv",
                "--workers",
                "1",
            ],
            workspace=tmp_path,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "built 1/1" in out
    loaded = load_manifest(tmp_path / "out" / "manifest.csv")
    assert len(loaded) == 1
    assert loaded[0]["status"] == STATUS_BUILT
    assert loaded[0]["chain"].endswith(f"{SEPARATE_V6_ID}>{AGENT_NETWORK_NONE_ID}")
