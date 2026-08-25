from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "library/benchmarks/tau-knowledge/cohort.manifest.json"
PREFLIGHT = ROOT / "scripts/tau_knowledge/preflight.py"
MATERIALIZER = ROOT / "scripts/tau_knowledge/materialize.py"
CONTROLS = ROOT / "scripts/tau_knowledge/controls.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_cohort_preserves_immutable_pins_and_selected_order() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["immutable"] is True
    assert manifest["selection"]["task_ids"] == [row["task_id"] for row in manifest["tasks"]]
    assert manifest["required_upstream"]["commit"] == "fc0055dc4e0a316c3f83133267fbd6faaa770992"
    assert manifest["adapter_evidence"]["commit"] == "636a2d0295d3ee233666bcd7d77fa81f7f090a19"


def test_source_digest_is_stable_and_bounded() -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_materialize")
    manifest = json.loads(MANIFEST.read_text())
    digest = materializer.source_digest(manifest)
    assert digest == "2519b16fa4ffc1b755a7b0ae63d0fa2b363450ccdff2fd284e1e5c60f1a4864c"
    assert len(digest) == 64


def test_missing_source_and_credentials_fail_closed_without_trial() -> None:
    preflight = _load(PREFLIGHT, "tau_knowledge_preflight")
    manifest = json.loads(MANIFEST.read_text())
    source = preflight.preflight_tau_phase(
        "oracle", env={}, home=Path("/tmp/does-not-exist"), source_root=Path("/tmp/missing"), manifest=manifest
    )
    assert source.proceed is False
    assert source.reason_code == "blocked:missing_source_checkout"
    credential = preflight.credential_preflight("reference", env={}, home=Path("/tmp/empty-home"))
    assert credential.reason_code == "blocked:missing_openai_api_key_for_simulated_user"
    assert credential.to_dict()["created_trial"] is False

def test_harbor_repository_layout_resolves_nested_tau_adapter(tmp_path: Path) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_nested_adapter")
    package = tmp_path / "harbor/adapters/tau3-bench/src/tau3_bench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "class Tau3BenchAdapter: pass\n", encoding="utf-8"
    )
    sys.modules.pop("tau3_bench", None)
    adapter = materializer._load_adapter(tmp_path / "harbor")
    assert adapter.__name__ == "Tau3BenchAdapter"


def test_controls_have_observable_oracle_nop_and_mutant_plans(tmp_path: Path) -> None:
    controls = _load(CONTROLS, "tau_knowledge_controls")
    task = tmp_path / "tau3-banking_knowledge-task-001"
    (task / "solution").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n", encoding="utf-8")
    (task / "solution/solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    for mode in ("oracle", "nop", "mutant"):
        command = controls.run_control(task, mode, dry_run=True)
        assert command[:6] == ["uv", "run", "harbor", "trial", "start", "-p"]
        assert command[-1] in {"oracle", "nop"}


def test_generated_corpus_is_not_tracked() -> None:
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    forbidden = ("library/benchmarks/tau-knowledge/generated/", "library/benchmarks/tau-knowledge/evidence/trials/", "library/benchmarks/tau-knowledge/evidence/luna/")
    assert not [path for path in tracked if path.startswith(forbidden) or path.endswith(".parquet") and "tau-knowledge" in path]
