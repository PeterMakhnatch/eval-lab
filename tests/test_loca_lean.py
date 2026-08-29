from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "loca-lean-v1"


def _benchmark_stem_names() -> set[str]:
    """Top-level module names this benchmark can define (its ``.py`` files)."""
    return {p.stem for p in ROOT.glob("*.py")}


def _restore_benchmark_modules(saved: dict[str, object], stems: set[str]) -> None:
    """Drop this benchmark's own bare imports, then restore the prior entries.

    Sibling benchmark families share generic module names (``state``, ``source``,
    ``templates``, ``verifier``, ...). To load this benchmark we temporarily evict
    any module cached under those bare names so ``from state import ...`` resolves
    to this benchmark's files through the scoped path. After the load we remove
    the modules this benchmark created and restore the exact prior ``sys.modules``
    entries (including absence), so a sibling's modules are never permanently
    displaced.
    """
    root = ROOT.resolve()
    for stem in stems:
        module = sys.modules.get(stem)
        if module is not None and getattr(module, "__file__", None):
            path = Path(module.__file__).resolve()
            if path.is_relative_to(root):
                del sys.modules[stem]
    for stem, prior in saved.items():
        if prior is None:
            sys.modules.pop(stem, None)
        else:
            sys.modules[stem] = prior  # type: ignore[assignment]


def load(name: str, filename: str | None = None):
    module_name = f"loca_lean_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    stems = _benchmark_stem_names()
    # Snapshot prior bare-name entries (including absence) so a sibling's modules
    # are restored after this scoped load rather than permanently displaced.
    saved = {stem: sys.modules.get(stem) for stem in stems}
    for stem in stems:
        sys.modules.pop(stem, None)
    orig_path = list(sys.path)
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{filename or name}.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except BaseException:
        # Never leave a partially-executed module cached under the unique name;
        # a retry must start from a clean spec.
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.path[:] = orig_path
        _restore_benchmark_modules(saved, stems)


def test_pins_are_immutable_and_digest_addressed():
    source = load("source")
    record, pins = source.load_pins()
    digest = source.source_digest(record, pins)
    assert len(digest) == 64
    assert record["license"]["spdx"] == "MIT"
    assert {pin.name for pin in pins} == {
        "final_8k_set_config.json",
        "final_64k_set_config.json",
        "final_128k_set_config.json",
        "generate_ab_data.py",
        "mcp_tool_schemas.json",
    }


def test_cache_fetch_fails_closed_on_mismatch(tmp_path):
    source = load("source_cache", "source")
    pin = source.Pin("fixture", "file:///does/not/matter", "sha256:" + "0" * 64)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ("fixture." + "0" * 64)).write_bytes(b"wrong")
    try:
        source.fetch_pinned(pin, cache, offline=True)
    except source.PinError as exc:
        assert "mismatch" in str(exc)
    else:
        raise AssertionError("tampered cache was accepted")
def test_cached_generator_loader_accepts_digest_suffix(tmp_path):
    state = load("state_cached", "state")
    cached = tmp_path / ("generate_ab_data.py." + "a" * 64)
    cached.write_text(
        "class ABTestingDataGenerator:\n"
        "    def __init__(self, seed): self.seed = seed\n"
        "    def generate_scenarios(self, **kwargs):\n"
        "        return {'scenarios': [{'name': 'canary', 'data_rows': [{'time_window': 't', 'A_clicks': 1, 'A_store_views': 1, 'B_clicks': 1, 'B_store_views': 0}]}]}\n"
    )
    rows = state._upstream_rows(cached, "8k", 42)
    assert rows == [{"scenario": "canary", "time_window": "t", "A_clicks": 1, "A_store_views": 1, "B_clicks": 1, "B_store_views": 0}]



def test_materializer_oracle_nop_and_mutants(tmp_path):
    materializer = load("materializer_test", "materializer")
    templates = load("templates_test", "templates")
    verifier = load("verifier_test", "verifier")
    verifier.oracle_bytes = templates.oracle_bytes
    materializer.DERIVED = tmp_path / "derived"
    target = materializer.output_path()
    materializer.materialize(target, verify_sources=False)
    first = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    materializer.materialize(target, verify_sources=False)
    second = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert first == second
    for required in ("task.toml", "instruction.md", "environment", "solution", "tests", "state_manifest.json", "files", "local_db"):
        assert (target / required).exists()
    assert (target / "environment" / "Dockerfile").is_file()
    assert (target / "solution" / "solve.sh").is_file()
    task_config = tomllib.loads((target / "task.toml").read_text(encoding="utf-8"))
    assert task_config["environment"]["network_mode"] == "no-network"
    assert (target / "tests" / "test.sh").is_file()
    for script in (target / "environment" / "entrypoint.sh", target / "solution" / "solve.sh", target / "tests" / "test.sh"):
        text = script.read_text(encoding="utf-8")
        assert "LOCA_BENCHMARK_ROOT" not in text
        assert "/repo/" not in text
    for referenced in (
        target / "environment" / "runtime.py",
        target / "environment" / "oracle.py",
        target / "environment" / "templates.py",
        target / "tests" / "verifier.py",
        target / "tests" / "templates.py",
        target / "tests" / "Dockerfile",
    ):
        assert referenced.is_file()
    assert (target / "environment" / "task_state" / "state_manifest.json").is_file()
    assert (target / "environment" / "task_state" / "files" / "environment_description.json").is_file()
    assert (target / "tests" / "task_state" / "state_manifest.json").is_file()
    assert (target / "tests" / "task_state" / "files" / "clickstream.csv").is_file()
    assert 'CMD ["sleep", "infinity"]' in (target / "tests" / "Dockerfile").read_text(encoding="utf-8")
    templates.nop(target, target / "agent_workspace")
    assert verifier.verify(target)["reward"] == 0.0
    templates.oracle(target)
    assert verifier.verify(target)["reward"] == 1.0
    for mutant in templates.mutants().values():
        materializer.materialize(target, verify_sources=False)
        mutant(target, target / "agent_workspace")
        assert verifier.verify(target)["reward"] == 0.0


def test_context_curve_contract_rejects_drift(tmp_path):
    materializer = load("materializer_context", "materializer")
    curve = load("context_curve")
    materializer.DERIVED = tmp_path / "derived"
    target = materializer.output_path()
    materializer.materialize(target, verify_sources=False)
    rows_path = tmp_path / "rows.jsonl"
    curve.emit(target, "canary", rows_path)
    projected = curve.project(rows_path)
    assert projected["contract"] == curve.CONTEXT_CONTRACT
    assert projected["curve"][0]["after_tokens"]
    assert (tmp_path / "semantic-projection" / "context_operation_facts.parquet").is_file()
    lines = rows_path.read_text().splitlines()
    row = json.loads(lines[0])
    row["contract"]["measurement"] = "padding"
    rows_path.write_text(json.dumps(row) + "\n")
    try:
        curve.project(rows_path)
    except ValueError:
        pass
    else:
        raise AssertionError("drifted context contract was accepted")

def test_verifier_robustness_and_reward_file_generation(tmp_path):
    materializer = load("materializer_robust", "materializer")
    templates = load("templates_robust", "templates")
    verifier = load("verifier_robust", "verifier")
    verifier.oracle_bytes = templates.oracle_bytes
    materializer.DERIVED = tmp_path / "derived"
    target = materializer.output_path()
    materializer.materialize(target, verify_sources=False)

    # 1. Oracle execution with reward directory
    reward_dir = tmp_path / "logs" / "verifier"
    templates.oracle(target)
    result = verifier.verify(target, reward_dir=reward_dir)
    assert result["reward"] == 1.0
    assert (reward_dir / "reward.txt").read_text(encoding="utf-8").strip() == "1.0"
    assert (reward_dir / "verify.json").is_file()

    # 2. CRLF line endings compatibility
    workspace = target / "agent_workspace"
    crlf_record = (workspace / "record.csv").read_bytes().replace(b"\n", b"\r\n")
    (workspace / "record.csv").write_bytes(crlf_record)
    result_crlf = verifier.verify(target, reward_dir=reward_dir)
    assert result_crlf["reward"] == 1.0
    assert (reward_dir / "reward.txt").read_text(encoding="utf-8").strip() == "1.0"

    # 3. Corrupted / garbage record
    (workspace / "record.csv").write_bytes(b"\xff\xfe\x00\x00corrupt binary data")
    result_corrupt = verifier.verify(target, reward_dir=reward_dir)
    assert result_corrupt["reward"] == 0.0
    assert (reward_dir / "reward.txt").read_text(encoding="utf-8").strip() == "0.0"

    # 4. Missing workspace / NOP
    templates.nop(target, workspace)
    result_nop = verifier.verify(target, reward_dir=reward_dir)
    assert result_nop["reward"] == 0.0
    assert (reward_dir / "reward.txt").read_text(encoding="utf-8").strip() == "0.0"

    # 5. Missing / non-existent task dir handles gracefully without crash
    non_existent = tmp_path / "does_not_exist"
    result_missing = verifier.verify(non_existent, reward_dir=reward_dir)
    assert result_missing["reward"] == 0.0
    assert (reward_dir / "reward.txt").read_text(encoding="utf-8").strip() == "0.0"

    # 6. Task toml schema 1.4 validation
    task_toml = tomllib.loads((target / "task.toml").read_text(encoding="utf-8"))
    assert task_toml["schema_version"] == "1.4"
    assert "/app/task_state/agent_workspace/record.csv" in task_toml["artifacts"]
    assert task_toml["verifier"]["environment_mode"] == "separate"
    assert task_toml["task"]["authors"][0]["name"] == "LOCA-bench Contributors"


def test_materializer_unaffected_by_sibling_benchmark_state_module(monkeypatch):
    """A sibling benchmark's bare `state` module must not shadow loca-lean's materializer.

    Regression for the CI Python 3.12 failure where mcp-recovery-v1's `state.py`
    was cached under the shared bare name ``state`` and loca-lean's materializer
    then raised ``ImportError: cannot import name 'CANARY' from 'state'``.
    """
    mcp_root = Path(__file__).parents[1] / "library" / "benchmarks" / "mcp-recovery-v1"
    monkeypatch.syspath_prepend(str(mcp_root))
    state_spec = importlib.util.spec_from_file_location(
        "mcp_recovery_v1_state_probe", mcp_root / "state.py"
    )
    assert state_spec is not None and state_spec.loader is not None
    mcp_state = importlib.util.module_from_spec(state_spec)
    sys.modules["mcp_recovery_v1_state_probe"] = mcp_state
    state_spec.loader.exec_module(mcp_state)
    # Deterministically cache mcp-recovery-v1/state.py under the shared bare `state`.
    sys.modules["state"] = mcp_state
    assert Path(sys.modules["state"].__file__).resolve() == mcp_root.resolve() / "state.py"

    # loca-lean's materializer must still resolve its own `state`/`source` modules.
    materializer = load("materializer_after_mcp_state", "materializer")
    assert callable(materializer.materialize)
    assert set(materializer.SIZES) == {"8k", "64k", "128k"}
    assert materializer.INSTRUCTION
    # The displaced sibling `state` module is restored, not permanently clobbered.
    assert sys.modules["state"] is mcp_state


def test_failed_load_clears_partial_module_cache(monkeypatch):
    """A load whose exec fails must not leave a partial module cached for retry.

    If ``sys.modules[module_name]`` survived a failed ``exec_module``, a retry of
    the same name would return the half-initialized module instead of reloading.
    """
    real_spec = importlib.util.spec_from_file_location

    def failing_spec(name, path, *args, **kwargs):
        spec = real_spec(name, path, *args, **kwargs)
        loader = spec.loader

        def boom(_module):
            raise RuntimeError("synthetic import failure")

        loader.exec_module = boom  # type: ignore[method-assign,assignment]
        return spec

    monkeypatch.setattr(importlib.util, "spec_from_file_location", failing_spec)
    module_name = "loca_lean_never_loaded_partial"
    with pytest.raises(RuntimeError, match="synthetic import failure"):
        load("never_loaded_partial", "source")
    # The partial unique module was purged, so a retry reloads from scratch.
    assert module_name not in sys.modules

    # Disable the failure injection and confirm the same name now loads cleanly.
    monkeypatch.setattr(importlib.util, "spec_from_file_location", real_spec)
    source = load("never_loaded_partial", "source")
    assert callable(source.load_pins)
    assert callable(source.fetch_pinned)
