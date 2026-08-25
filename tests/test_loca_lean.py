from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "loca-lean-v1"
sys.path.insert(0, str(ROOT))


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    assert (target / "tests" / "test.sh").is_file()
    templates.nop(target, target / "agent_workspace")
    assert verifier.verify(target)["reward"] == 0.0
    materializer.materialize(target, verify_sources=False)
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
