from pathlib import Path
import importlib.util

def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pkg():
    p = Path(__file__).resolve().parents[1]
    parts = p.name.split("-")
    return p, int(parts[3]), parts[4]


def test_golden_present_and_task_toml_declares_artifacts():
    pkg, _seed, _size = _pkg()
    import tomllib

    data = tomllib.loads((pkg / "task.toml").read_text())
    assert data["verifier"]["environment_mode"] == "separate"
    artifacts = data["artifacts"]
    assert "/app/task_state/agent_workspace/record.csv" in artifacts
    assert "/app/task_state/agent_workspace/promo-assets-for-b.marker" in artifacts
    for a in artifacts:
        assert a.startswith("/app/task_state/agent_workspace/")
        assert "expected" not in a

    assert (pkg / "tests" / "golden" / "expected_record.csv").exists()
    assert (pkg / "tests" / "golden" / "manifest.json").exists()


def test_test_sh_writes_reward():
    pkg, _, _ = _pkg()
    text = (pkg / "tests" / "test.sh").read_text()
    assert "mkdir -p /logs/verifier" in text
    assert "/logs/verifier/reward.txt" in text


def test_no_verify_or_expected_in_environment():
    pkg, _, _ = _pkg()
    for p in (pkg / "environment").rglob("*"):
        if p.is_file():
            assert "expected_record" not in p.name
    assert not (pkg / "environment" / "verify.py").exists()


def test_materialize_verify_and_oracle():
    pkg, seed, size = _pkg()
    if size != "8k":
        return

    import tempfile

    env = _load("adapter", pkg / "environment" / "adapter.py")
    orc = _load("oracle", pkg / "environment" / "oracle.py")
    ver = _load("verify", pkg / "tests" / "verify.py")
    golden = pkg / "tests" / "golden"

    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task"
        env.materialize(task, size, seed, golden_dir=golden)

        # No expected state in the agent-visible tree.
        for f in (task / "files").rglob("*"):
            if f.is_file():
                assert "expected_record" not in f.name
        assert not (task / "files" / "expected_record.csv").exists()
        assert not (task / "agent_workspace" / "expected_record.csv").exists()

        # Fresh header-only workspace verifies to 0.0 without raising.
        result = ver.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 0.0
        assert result["assertions"]["record_exists"]
        assert not result["assertions"]["record_matches_upstream_oracle"]
        assert result["assertions"]["state_is_nonempty"]

        # Oracle computes the answer from clickstream and verifies to 1.0.
        orc.solve(task, task / "agent_workspace")
        result = ver.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 1.0
        assert result["assertions"]["record_matches_upstream_oracle"]
        assert result["assertions"]["all_assertions"]
