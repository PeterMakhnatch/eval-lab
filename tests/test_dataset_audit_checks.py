"""Focused behavioral tests for dataset_audit_checks (no execution, no network)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from evallab import dataset_audit_checks as checks
from evallab.dataset_audit_contracts import AuditTask


def _sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _harbor_package(
    root: Path,
    *,
    name: str = "harbor/hello-world",
    instruction: str = 'Create a file called hello.txt with "Hello, world!" as content.\n',
    tests: dict[str, str] | None = None,
    solve: str = '#!/bin/bash\necho "Hello, world!" > hello.txt\n',
) -> Path:
    pkg = root / "pkg"
    _write(pkg / "task.toml", f'schema_version = "1.4"\n\n[task]\nname = "{name}"\n')
    _write(pkg / "instruction.md", instruction)
    _write(pkg / "solution" / "solve.sh", solve)
    _write(pkg / "environment" / "Dockerfile", "FROM ubuntu:24.04\n")
    if tests is None:
        tests = {
            "tests/test_state.py": (
                "from pathlib import Path\n\n"
                'hello_path = Path("/app/hello.txt")\n'
                "assert hello_path.exists()\n"
                'content = hello_path.read_text().strip()\n'
                'assert content == "Hello, world!"\n'
            ),
        }
    for rel, text in tests.items():
        _write(pkg / rel, text)
    return pkg


def _mimo_package(root: Path, *, patch: str) -> Path:
    pkg = _harbor_package(root, name="mimo-v2.6-rl/format-code-task-000001", tests={})
    (pkg / "tests").mkdir(parents=True, exist_ok=True)
    _write(pkg / "tests" / "test.patch", patch)
    return pkg


def _task(pkg: Path | None, **overrides) -> AuditTask:
    from evallab.registry import harbor_task_digest, task_directory_digest

    base = {
        "dataset_id": "hello-world",
        "task_id": "hello-world",
        "task_name": "harbor/hello-world",
        "path": pkg,
        "source_uri": "test://hello-world@1.0",
    }
    base.update(overrides)
    if pkg is not None and pkg.is_dir() and "package_digest" not in overrides:
        base["package_digest"] = task_directory_digest(pkg)
    if pkg is not None and pkg.is_dir() and "harbor_digest" not in overrides:
        base["harbor_digest"] = harbor_task_digest(pkg)
    return AuditTask(**base)


def _snapshot_files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): _sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


HARNESS = ("mimo_test_command.sh", "mimo_build_env.tar.gz.b64", "test_commands.json")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _repo_line(
    path: str,
    base: str,
    beyond: str,
    unreachable: str,
    refs: str,
    note: str | None = None,
) -> str:
    line = (
        f"LEAK_PROBE_V1 repo path_b64={_b64(path)} base={base} "
        f"beyond_base={beyond} unreachable={unreachable} refs={refs}"
    )
    return line + (f" note={note}" if note else "") + "\n"


# ---------------------------------------------------------------------------
# Static stage.
# ---------------------------------------------------------------------------


def test_static_harbor_scope_and_digest_binding(tmp_path: Path) -> None:
    pkg = _harbor_package(tmp_path)
    before = _snapshot_files(pkg)
    obs = checks.static_observation(_task(pkg))
    assert obs.stage == "static"
    assert obs.status == "recorded"
    assert obs.facts["coverage"] == "harbor-test-source"
    instruction_raw = (pkg / "instruction.md").read_bytes()
    assert obs.facts["instr_bytes"] == len(instruction_raw)
    test_raw = (pkg / "tests" / "test_state.py").read_bytes()
    assert obs.facts["corpus_bytes"] == len(test_raw)
    assert obs.facts["instruction_sha256"] == _sha(instruction_raw)
    by_binding = {source.binding: source for source in obs.sources}
    assert by_binding["instruction.md"].sha256 == _sha(instruction_raw)
    assert by_binding["tests/test_state.py"].sha256 == _sha(test_raw)
    assert "tests/test.patch" not in by_binding  # default scope binds tests/ only
    assert obs.facts["package_digest_match"] is True
    assert isinstance(obs.facts["task_lint"], list)
    assert "descriptive" in obs.facts["quality_note"]
    assert _snapshot_files(pkg) == before


def test_static_unstated_literal_boundary(tmp_path: Path) -> None:
    test_py = 'assert result == "quantum entanglement across galaxies"\n'
    flagged = _harbor_package(
        tmp_path / "a",
        instruction="Do arithmetic.\n",
        tests={"tests/t.py": test_py},
    )
    assert checks.static_observation(_task(flagged)).facts["a_unstated_literal"] == 1
    clean = _harbor_package(
        tmp_path / "b",
        instruction="Expect quantum entanglement across galaxies.\n",
        tests={"tests/t.py": test_py},
    )
    assert checks.static_observation(_task(clean)).facts["a_unstated_literal"] == 0


def test_static_network_boundaries(tmp_path: Path) -> None:
    local = _harbor_package(
        tmp_path / "a",
        tests={"tests/t.py": 'assert get("http://localhost:8000/x") == 1\n'},
    )
    obs = checks.static_observation(_task(local))
    assert obs.facts["b_network"] == 1
    assert "localhost-only" in obs.facts["ev_b"]
    external = _harbor_package(
        tmp_path / "b",
        tests={"tests/t.py": 'assert fetch("https://pypi.org/simple/x") == 1\n'},
    )
    obs = checks.static_observation(_task(external))
    assert obs.facts["b_network"] == 1
    assert "external-or-mixed" in obs.facts["ev_b"]
    quiet = _harbor_package(
        tmp_path / "c", tests={"tests/t.py": "assert 1 + 1 == 2\n"}
    )
    assert checks.static_observation(_task(quiet)).facts["b_network"] == 0


def test_static_nondeterminism_seed_boundary(tmp_path: Path) -> None:
    unseeded = _harbor_package(
        tmp_path / "a",
        tests={"tests/t.py": "import random\nassert random.random() < 1\n"},
    )
    obs = checks.static_observation(_task(unseeded))
    assert obs.facts["c_nondeterminism"] == 1
    assert "unseeded-random" in obs.facts["ev_c"]
    seeded = _harbor_package(
        tmp_path / "b",
        tests={"tests/t.py": "import random\nrandom.seed(0)\nassert random.random() < 1\n"},
    )
    assert checks.static_observation(_task(seeded)).facts["c_nondeterminism"] == 0


def test_static_tiny_suite_boundary(tmp_path: Path) -> None:
    tiny = _harbor_package(tmp_path / "a", tests={"tests/t.py": "assert x == 1\n"})
    assert checks.static_observation(_task(tiny)).facts["e_tiny_suite"] == 1
    enough = _harbor_package(
        tmp_path / "b",
        tests={"tests/t.py": "assert x == 1\nassert y == 2\n"},
    )
    assert checks.static_observation(_task(enough)).facts["e_tiny_suite"] == 0
    patch = (
        "diff --git a/pkg/util.py b/pkg/util.py\n"
        "--- a/pkg/util.py\n"
        "+++ b/pkg/util.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+assert x == 1\n"
    )
    mimo = _mimo_package(tmp_path / "c", patch=patch)
    obs = checks.static_observation(
        _task(mimo), added_patch=mimo / "tests" / "test.patch"
    )
    assert obs.facts["coverage"] == "added-patch-lines"
    assert obs.facts["e_tiny_suite"] == 0


def test_static_mimo_patch_scope_excludes_harness(tmp_path: Path) -> None:
    patch = (
        "diff --git a/tests/mimo_test_command.sh b/tests/mimo_test_command.sh\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/mimo_test_command.sh\n"
        "@@ -0,0 +1 @@\n"
        "+curl https://pypi.org/simple/x\n"
        "diff --git a/state/x_test.go b/state/x_test.go\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/state/x_test.go\n"
        "@@ -0,0 +1 @@\n"
        "+assert x == 1\n"
    )
    pkg = _mimo_package(tmp_path, patch=patch)
    obs = checks.static_observation(
        _task(pkg),
        added_patch=pkg / "tests" / "test.patch",
        harness_basenames=HARNESS,
    )
    assert obs.facts["coverage"] == "added-patch-lines"
    assert obs.facts["b_network"] == 0
    assert obs.facts["corpus_bytes"] == len((pkg / "tests" / "test.patch").read_bytes())
    assert obs.facts["scope"]["non_harness_files"] == 1


def test_static_default_scope_ignores_patch_file(tmp_path: Path) -> None:
    pkg = _mimo_package(
        tmp_path,
        patch=(
            "diff --git a/state/x_test.go b/state/x_test.go\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/state/x_test.go\n"
            "@@ -0,0 +1 @@\n"
            "+assert x == 1\n"
        ),
    )
    default = checks.static_observation(_task(pkg))
    assert default.facts["coverage"] == "harbor-test-source"
    assert default.facts["corpus_lines"] == 6  # whole file, not added lines
    assert default.facts["scope"]["test_files"] == ["tests/test.patch"]
    explicit = checks.static_observation(
        _task(pkg), added_patch=pkg / "tests" / "test.patch"
    )
    assert explicit.facts["coverage"] == "added-patch-lines"
    assert explicit.facts["corpus_lines"] == 1  # added lines only
    assert explicit.facts["scope"]["added_lines"] == 1
    missing = checks.static_observation(
        _task(pkg), added_patch=pkg / "tests" / "absent.patch"
    )
    assert missing.status == "recorded"
    assert missing.facts["coverage"] == "added-patch-lines"
    for flag in (
        "a_unstated_literal",
        "b_network",
        "c_nondeterminism",
        "d_env_coupled",
        "e_tiny_suite",
        "f_repo_file_read",
        "g_polyglot_toolchain",
    ):
        assert missing.facts[flag] is None


def test_static_missing_inputs_are_unknown_not_clean(tmp_path: Path) -> None:
    assert checks.static_observation(_task(None)).status == "unavailable"
    missing = checks.static_observation(_task(tmp_path / "nope"))
    assert missing.status == "unavailable"
    assert missing.facts["has_future_history"] is None
    empty = _harbor_package(tmp_path / "empty", tests={})
    (empty / "tests").mkdir(parents=True, exist_ok=True)
    obs = checks.static_observation(_task(empty))
    assert obs.status == "recorded"
    for flag in (
        "a_unstated_literal",
        "b_network",
        "c_nondeterminism",
        "d_env_coupled",
        "e_tiny_suite",
        "f_repo_file_read",
        "g_polyglot_toolchain",
    ):
        assert obs.facts[flag] is None


def test_static_env_repo_toolchain_boundaries(tmp_path: Path) -> None:
    pkg = _harbor_package(
        tmp_path,
        tests={
            "tests/t.py": (
                "assert open('/opt/data/golden.csv') is not None\n"
                "assert open('test_state.py') is not None\n"
            ),
            "tests/test_state.py": "assert True\n",
            "tests/run.sh": "go test ./...\n",
        },
    )
    obs = checks.static_observation(_task(pkg))
    assert obs.facts["d_env_coupled"] == 1  # /opt/... is a coupled host path
    assert obs.facts["f_repo_file_read"] == 1  # golden.csv not in scope
    assert obs.facts["g_polyglot_toolchain"] == 1
    scratch = _harbor_package(
        tmp_path / "b",
        tests={"tests/t.py": "assert open('/tmp/scratch/x') is not None\n"},
    )
    assert checks.static_observation(_task(scratch)).facts["d_env_coupled"] == 0


def test_static_hack_scan_joins_v8_findings_with_matching_digest(tmp_path: Path) -> None:
    from evallab.registry import harbor_task_digest, task_directory_digest

    pkg = _harbor_package(tmp_path)
    with (pkg / "task.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[environment]\nuser = "root"\nnetwork_mode = "public"\n')
    before = _snapshot_files(pkg)
    obs = checks.static_observation(_task(pkg))
    assert obs.status == "recorded"
    hack = obs.facts["hack_scan"]
    assert hack["available"] is True
    assert hack["schema"] == "evallab.reward_hack.scan/v1"
    assert hack["error"] is None
    assert hack["package_digest"] == task_directory_digest(pkg)
    assert hack["package_digest"] == obs.facts["package_digest_actual"]
    assert hack["harbor_digest"] == harbor_task_digest(pkg)
    v8 = [finding for finding in hack["findings"] if finding["flaw_class"] == "V8"]
    assert v8, "root/public-network package should surface V8 claims"
    assert any("root" in finding["title"] for finding in v8)
    assert any("network" in finding["title"] for finding in v8)
    for finding in hack["findings"]:
        assert set(finding) == {
            "flaw_class",
            "severity",
            "title",
            "evidence",
            "detail",
        }
    assert hack["recon"]["network_mode"] == "public"
    assert "not a security certificate" in hack["limits"]
    assert "never keep/fix/discard" in hack["descriptive_note"]
    json.dumps(hack)  # joined fact stays JSON-serializable
    assert _snapshot_files(pkg) == before


def test_static_hack_scan_failure_stays_unknown_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _harbor_package(tmp_path / "pkg")
    task = _task(pkg)

    def _boom(_task_dir: Path):
        raise RuntimeError("boom")

    monkeypatch.setattr(checks, "_scan_package_for_static", _boom)
    obs = checks.static_observation(task)
    assert obs.status == "recorded"
    hack = obs.facts["hack_scan"]
    assert hack["available"] is False
    assert hack["findings"] == []
    assert hack["package_digest"] is None
    assert "boom" in hack["error"]
    assert obs.facts["coverage"] == "harbor-test-source"

    monkeypatch.undo()
    empty = _harbor_package(tmp_path / "empty", tests={})
    (empty / "tests").mkdir(parents=True, exist_ok=True)
    unknown = checks.static_observation(_task(empty))
    assert unknown.status == "recorded"
    for flag in (
        "a_unstated_literal",
        "b_network",
        "c_nondeterminism",
        "d_env_coupled",
        "e_tiny_suite",
        "f_repo_file_read",
        "g_polyglot_toolchain",
    ):
        assert unknown.facts[flag] is None
    sound = unknown.facts["hack_scan"]
    assert sound["available"] is True
    assert sound["schema"] == "evallab.reward_hack.scan/v1"


BASE = "a" * 40


def _probe_job(
    root: Path, body: str, *, trial: str = "probe__abc123", reward: str | None = None
) -> Path:
    oracle = root / trial / "agent" / "oracle.txt"
    _write(oracle, body)
    _write(root / "result.json", json.dumps({
        "id": "probe-job", "n_total_trials": 1, "stats": {},
        "finished_at": "2026-01-01T00:00:01Z",
    }))
    _write(root / trial / "result.json", json.dumps({
        "id": "probe-trial", "trial_name": trial, "task_name": "harbor/hello-world",
        "finished_at": "2026-01-01T00:00:01Z",
    }))
    _write(root / trial / "config.json", json.dumps({"agent": {"name": "oracle"}}))
    _write(root / trial / "lock.json", json.dumps({"task": {"digest": "e" * 64}}))
    if reward is not None:
        _write(root / trial / "agent" / "reward.txt", reward)
    return root


def _probe_task() -> AuditTask:
    return AuditTask(
        dataset_id="hello-world",
        task_id="hello-world",
        task_name="harbor/hello-world",
        path=None,
        package_digest="sha256:" + "d" * 64,
        harbor_digest="sha256:" + "e" * 64,
        source_uri="test://hello-world@1.0",
    )


