from __future__ import annotations

import csv
import json
import os
import re
import runpy
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from evallab.mimo_clean import LEDGER_REL, SNAPSHOT_REL, VARIANTS_REL
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.strip_future_history import pack_setup, unpack_setup_blob
from evallab.task_variants import VariantInvalid, derive_task, lineage_chain, materialize

DRIVER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/mimo-v6-full-fleet/repair_environments.py"
)
TASK = "format-code-task-000838"
SETUP_REL = "environment/setup/setup.sh"


@pytest.fixture
def repair() -> dict[str, Any]:
    return runpy.run_path(str(DRIVER))


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture
def parent(tmp_path: Path) -> Path:
    package = tmp_path / "source"
    setup = package / "environment/setup"
    (setup / "files").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "solution").mkdir()
    (setup / "setup.sh").write_text(
        '#!/bin/bash\nM=${MIMO_TEST_SETUP_ROOT:?}\n'
        '[ -f "$M/ready" ] && exit 0\n'
        'fail() { echo "setup: $*" >&2; exit 1; }\n'
        'write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }\n'
        '\nwrite_blocklist\ntouch "$M/ready"\n',
        encoding="utf-8",
    )
    (setup / "files/blocklist").write_bytes(b"0.0.0.0 pypi.org\n")
    (package / "environment/Dockerfile").write_bytes(b"FROM example/image@sha256:fixture\n")
    (package / "instruction.md").write_bytes(b"Fix implementation, not tests.\n")
    (package / "tests/test.sh").write_bytes(b"#!/bin/bash\nbash /tests/test_command.sh\n")
    (package / "tests/test_command.sh").write_bytes(b"bash /testbed/mimo_test_command.sh\n")
    (package / "tests/test.patch").write_bytes(b"hidden fixture patch stays unchanged\n")
    (package / "solution/solve.sh").write_bytes(b"#!/bin/bash\nexit 0\n")
    command = (
        "bash -c 'test -f /var/lib/mimo/ready || { mkdir -p /var/lib/mimo && echo "
        + pack_setup(setup)
        + " | base64 -d | tar -xzf - -C /var/lib/mimo && bash /var/lib/mimo/setup.sh; }'"
    )
    (package / "task.toml").write_text(
        'schema_version = "1.4"\n'
        f'[task]\nname = "mimo-v2.6-rl/{TASK}"\ndescription = "fixture"\n'
        '[agent]\nnetwork_mode = "no-network"\ntimeout_sec = 3600.0\n'
        '[verifier]\ntimeout_sec = 2100.0\nuser = "root"\n'
        '[environment]\ndocker_image = "example/image@sha256:fixture"\n'
        'workdir = "/testbed"\ncpus = 2\nmemory_mb = 8192\nnetwork_mode = "public"\n'
        '[environment.healthcheck]\n'
        f'command = {json.dumps(command)}\ntimeout_sec = 1200.0\nretries = 0\n',
        encoding="utf-8",
    )
    return package


def _derive(repair: dict[str, Any], parent: Path, tmp_path: Path):
    return repair["derive_environment_repair"](
        parent, task_id=TASK, repo_root=tmp_path / "repo",
        variants_root=tmp_path / "variants",
    )


def test_real_setup_and_healthcheck_materialize_without_changing_contract(
    repair: dict[str, Any], parent: Path, tmp_path: Path,
) -> None:
    before = _files(parent)
    record = _derive(repair, parent, tmp_path)
    assert record is not None
    assert record.transform == "mimo-v6-environment-repair@1"
    assert record.task_name == f"mimo-v2.6-rl/{TASK}"
    assert record.parent.digest == task_directory_digest(parent)
    assert record.parent.harbor_digest == harbor_task_digest(parent)
    assert record.parent.source.kind == "local"
    assert record.parent.source.path == str(parent.resolve())
    assert set(record.components_changed) == {"task_toml", "environment"}
    assert {change.path for change in record.files} == {SETUP_REL, "task.toml"}
    assert record.inputs["runner_selection"] == {
        "candidate": "/usr/local/bin/python", "expected_python": "3.8",
        "observed_stdlib": "/usr/local/lib/python3.8",
        "observed_unittest_loader": "/usr/local/lib/python3.8/unittest/loader.py",
        "executable_observed_in_retained_log": False,
        "runtime_receipt": "mimo-v6-environment-repair@1: runner_python=<resolved sys.executable>",
    }
    package = materialize(
        record, parent, repo_root=tmp_path / "repo",
        variants_root=tmp_path / "rebuilt-variants",
    )
    assert task_directory_digest(package) == record.variant_digest
    assert harbor_task_digest(package) == record.variant_harbor_digest
    assert record.variant_digest != record.parent.digest
    assert _files(parent) == before
    after = _files(package)
    assert set(after) == set(before)
    assert {path for path in before if before[path] != after[path]} == {SETUP_REL, "task.toml"}
    old_config = tomllib.loads(before["task.toml"].decode())
    new_config = tomllib.loads(after["task.toml"].decode())
    old_command = old_config["environment"]["healthcheck"].pop("command")
    new_command = new_config["environment"]["healthcheck"].pop("command")
    assert new_config == old_config
    blob_pattern = r"echo ([A-Za-z0-9+/=]+) \| base64 -d"
    assert re.sub(blob_pattern, "echo PAYLOAD | base64 -d", old_command) == re.sub(
        blob_pattern, "echo PAYLOAD | base64 -d", new_command,
    )
    match = re.search(blob_pattern, new_command)
    assert match is not None
    assert unpack_setup_blob(match[1]) == {
        "setup.sh": after[SETUP_REL],
        "files/blocklist": before["environment/setup/files/blocklist"],
    }
    assert len(new_command.encode()) < 128 * 1024
    assert repair["YAML_LOCK"].encode() in after[SETUP_REL]
    assert b"--no-deps --require-hashes --only-binary=:all:" in after[SETUP_REL]
    assert b"export PATH" not in after[SETUP_REL]
    persisted = tmp_path / "repo" / record.record_relpath()
    assert json.loads(persisted.read_text())["variant_digest"] == record.variant_digest


def test_existing_variant_parent_keeps_its_real_lineage_edge(
    repair: dict[str, Any], parent: Path, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    variants = tmp_path / "variants"
    previous = derive_task(
        parent, changes={"environment/Dockerfile": b"# preserved earlier derivation\nFROM fixture\n"},
        transform="fixture-parent@1", rationale="test existing parent edge", created_by="pytest",
        repo_root=repo, variants_root=variants,
    )
    previous_package = variants / previous.task_slug / previous.digest12
    record = _derive(repair, previous_package, tmp_path)
    assert record is not None
    assert record.parent.source.kind == "variant"
    assert record.parent.source.record == previous.record_relpath().as_posix()
    assert record.parent.digest == previous.variant_digest
    assert record.parent.harbor_digest == previous.variant_harbor_digest
    assert [edge.record.transform for edge in lineage_chain(record, repo_root=repo)] == [
        "mimo-v6-environment-repair@1", "fixture-parent@1",
    ]


@pytest.mark.parametrize(("probe_rc", "pip_rc"), [(0, 0), (37, 0), (0, 38)])
def test_setup_targets_native_candidate_despite_helper_path_and_preserves_failures(
    repair: dict[str, Any], parent: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, probe_rc: int, pip_rc: int,
) -> None:
    # Shell double only: never install or download a wheel while testing setup.
    native = tmp_path / "native runner/python"
    native.parent.mkdir()
    native.write_text(
        '#!/bin/bash\n'
        'if [ "$1" = "-" ]; then\n'
        '  cat > "$MIMO_TEST_PROBE"\n'
        '  printf "%s\\n" "$0"\n'
        '  exit "$MIMO_TEST_PROBE_RC"\n'
        'fi\n'
        'printf "%s\\n" "$@" > "$MIMO_TEST_PIP_ARGS"\n'
        'printf "%s" "$PATH" > "$MIMO_TEST_PIP_PATH"\n'
        'cat > "$MIMO_TEST_LOCK"\n'
        'exit "$MIMO_TEST_PIP_RC"\n',
        encoding="utf-8",
    )
    native.chmod(0o755)
    wrong_bin = tmp_path / "helper-bin"
    wrong_bin.mkdir()
    for name in ("python", "python3"):
        wrong_python = wrong_bin / name
        wrong_python.write_text('#!/bin/bash\ntouch "$MIMO_TEST_WRONG_PYTHON"\nexit 99\n')
        wrong_python.chmod(0o755)
    monkeypatch.setitem(
        repair["derive_environment_repair"].__globals__, "RUNNER_PYTHON_CANDIDATE", str(native),
    )
    record = _derive(repair, parent, tmp_path)
    assert record is not None
    package = tmp_path / "variants" / record.task_slug / record.digest12
    setup_root = package / "environment/setup"
    helper_path = f"{wrong_bin}:/usr/bin:/bin"
    outputs = {name: tmp_path / name.lower() for name in (
        "PROBE", "PIP_ARGS", "PIP_PATH", "LOCK", "WRONG_PYTHON",
    )}
    result = subprocess.run(
        ["/bin/bash", str(setup_root / "setup.sh")],
        env={
            **os.environ, "PATH": helper_path, "MIMO_TEST_SETUP_ROOT": str(setup_root),
            "MIMO_TEST_PROBE_RC": str(probe_rc), "MIMO_TEST_PIP_RC": str(pip_rc),
            **{f"MIMO_TEST_{name}": str(path) for name, path in outputs.items()},
        },
        capture_output=True, text=True, check=False,
    )
    assert not outputs["WRONG_PYTHON"].exists()
    probe = outputs["PROBE"].read_text()
    assert "sys.version_info[:2] != (3, 8)" in probe
    assert 'sysconfig.get_path("stdlib")' in probe
    assert "from unittest import loader" in probe
    assert "/usr/local/lib/python3.8/unittest/loader.py" in probe
    assert "print(os.path.realpath(sys.executable))" in probe
    if probe_rc:
        assert result.returncode != 0
        assert "cannot resolve the native Python 3.8 runner" in result.stderr
        assert not outputs["PIP_ARGS"].exists()
    else:
        args = outputs["PIP_ARGS"].read_text().splitlines()
        assert args[:5] == ["-m", "pip", "--isolated", "--disable-pip-version-check", "install"]
        assert "--no-index" in args
        assert "--no-deps" in args
        assert "--require-hashes" in args
        assert "--only-binary=:all:" in args
        assert args[args.index("--retries") + 1] == "0"
        assert args[-2:] == ["--requirement", "/dev/stdin"]
        assert outputs["LOCK"].read_text() == repair["YAML_LOCK"]
        assert outputs["PIP_PATH"].read_text() == helper_path
        assert f"runner_python={native}" in result.stdout
        if pip_rc:
            assert result.returncode != 0
            assert "YAML install failed" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
    assert (setup_root / "ready").exists() is (probe_rc == 0 and pip_rc == 0)


def test_unaffected_task_has_no_filesystem_side_effects(
    repair: dict[str, Any], tmp_path: Path,
) -> None:
    assert repair["derive_environment_repair"](
        tmp_path / "missing-parent", task_id="format-code-task-000001",
        repo_root=tmp_path / "missing-repo", variants_root=tmp_path / "missing-store",
    ) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        ("task.toml", f"/{TASK}", "/wrong-task", "identity"),
        ("task.toml", "[verifier]\n", '[verifier]\nenvironment_mode = "separate"\n', "before separate"),
        (
            "task.toml", 'memory_mb = 8192\nnetwork_mode = "public"',
            'memory_mb = 8192\nnetwork_mode = "no-network"', "startup policy",
        ),
        (
            "task.toml", "[verifier]\n", '[verifier]\nnetwork_mode = "no-network"\n',
            "verifier policy",
        ),
        (SETUP_REL, "\nwrite_blocklist\n", "\ntrue\n", "write_blocklist"),
        (SETUP_REL, "#!/bin/bash\n", "#!/bin/bash\n# mimo-v6-environment-repair@1\n", "already carries"),
    ],
)
def test_unsupported_parent_fails_without_materializing_a_fake_repair(
    repair: dict[str, Any], parent: Path, tmp_path: Path,
    path: str, old: str, new: str, message: str,
) -> None:
    target = parent / path
    target.write_text(target.read_text().replace(old, new, 1))
    before = _files(parent)
    with pytest.raises(VariantInvalid, match=message):
        _derive(repair, parent, tmp_path)
    assert _files(parent) == before
    assert not (tmp_path / "variants").exists()
    assert not (tmp_path / "repo").exists()


def test_fleet_environment_root_rebuild_reuses_exact_immutable_derivation(
    parent: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(DRIVER.parent))
    build = runpy.run_path(str(DRIVER.with_name("build_fleet.py")))
    repo, primary = tmp_path / "repo", tmp_path / "primary"
    source = primary / SNAPSHOT_REL / "tasks" / TASK
    shutil.copytree(parent, source)
    ledger = repo / LEDGER_REL
    ledger.parent.mkdir(parents=True)
    row = {
        "task_id": TASK, "verdict": "keep", "run": "original",
        "run_digest": task_directory_digest(source), "run_transform": "",
        "run_variant_status": "", "evidence": "retained-import-failure",
    }
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    release = repo / build["RELEASE"]
    release.mkdir(parents=True)
    first = build["prepare_ledger"](repo, primary, {TASK})
    record_path = repo / first["environment_repair_records"][0]
    record_bytes = record_path.read_bytes()
    ledger_bytes = (release / "release-ledger.csv").read_bytes()

    build["prepare_ledger"](repo, primary, {TASK})

    assert record_path.read_bytes() == record_bytes
    assert (release / "release-ledger.csv").read_bytes() == ledger_bytes
    record = json.loads(record_bytes)
    package = (
        primary / VARIANTS_REL / f"mimo-v2.6-rl__{TASK}"
        / record["variant_digest"].removeprefix("sha256:")[:12]
    )
    assert task_directory_digest(package) == record["variant_digest"]
    assert harbor_task_digest(package) == record["variant_harbor_digest"]
